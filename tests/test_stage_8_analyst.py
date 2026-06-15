"""Tests for Stage 8 — Sonnet-as-analyst Layer 2 clusterer (Sprint M4).

Hermetic: all Anthropic calls + marketing-page fetches are stubbed via
fakes / monkeypatch so the suite stays fast and network-free.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from faultline.analyzer.marketing_fetcher import MarketingTaxonomy
from faultline.models.types import Feature
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_8_analyst import (
    DEFAULT_ANALYST_MODEL,
    Stage8Result,
    _anchor_blob_owner,
    _emit_product_features_from_analyst,
    _parse_analyst_response,
    _strip_code_fences,
    _validate_pf_names,
    build_analyst_payload,
    build_user_prompt,
    run_stage_8_analyst,
)


# ── Fixtures ────────────────────────────────────────────────────────────


def _feat(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=["alice"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=[],
        layer="developer",
    )


def _product(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        display_name=name,
        paths=paths,
        authors=["alice"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=[],
        layer="product",
    )


def _ctx(repo_path: Path) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack="next-monorepo",
        monorepo=True,
        workspaces=None,
        tracked_files=[],
        commits=[],
        audited_stack="next-app-router-monorepo",
        secondary_stacks=("next", "react"),
        extractor_hints=("route-file", "package-anchor"),
        workspace_manager="pnpm",
    )


class _FakeUsage:
    def __init__(self, in_t: int, out_t: int) -> None:
        self.input_tokens = in_t
        self.output_tokens = out_t


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMessage:
    def __init__(
        self, text: str, in_t: int = 5000, out_t: int = 1500,
    ) -> None:
        self.content = [_FakeContent(text)]
        self.usage = _FakeUsage(in_t, out_t)


class _FakeMessages:
    """Returns scripted responses in order; each `create` call advances."""

    def __init__(self, *response_texts: str) -> None:
        self._texts: list[str] = list(response_texts)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._texts) - 1)
        return _FakeMessage(self._texts[idx])


class _FakeClient:
    def __init__(self, *response_texts: str) -> None:
        self.messages = _FakeMessages(*response_texts)


class _FakeRaisingClient:
    """Client that raises on every Sonnet call — exercises bailout."""

    class _Msgs:
        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("simulated 500")

    def __init__(self) -> None:
        self.messages = self._Msgs()


@pytest.fixture(autouse=True)
def _isolated_marketing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redirect engine base dir so tests don't poison the user cache.

    The default ``FilesystemCacheBackend`` resolves its base from
    ``FAULTLINES_RUN_DIR`` → marketing cache lands under tmp_path."""
    monkeypatch.setenv("FAULTLINES_RUN_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _block_real_marketing_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op every external fetch + discovery hook by default.

    Individual tests re-monkeypatch as needed to inject taxonomies.
    """
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_analyst.fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: None,
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_analyst.discover_marketing_site",
        lambda repo_path: None,
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_analyst.fetch_page_text",
        lambda url, timeout_s=15: None,
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_analyst.fetch_llms_txt_urls",
        lambda primary: [],
    )


def _good_response(features: list[dict[str, Any]]) -> str:
    return json.dumps({"product_features": features})


# ── Test 1: payload builder produces the documented shape ──────────────


def test_build_analyst_payload_shape(tmp_path: Path) -> None:
    feats = [
        _feat(
            "auth-handlers",
            [
                "apps/web/app/api/auth/route.ts",
                "packages/core/src/auth.ts",
            ],
        ),
        _feat("billing", ["apps/billing/src/stripe.ts"]),
        _feat("scratch", ["scripts/build.ts"]),  # not under workspace
    ]
    payload = build_analyst_payload(_ctx(tmp_path), feats)

    # Shape
    for k in (
        "slug",
        "audited_stack",
        "secondary_stacks",
        "workspace_manager",
        "root_package",
        "auditor_hints",
        "workspace_packages",
        "developer_features",
        "marketing_text",
        "marketing_url",
        "taxonomy_size",
    ):
        assert k in payload, f"payload missing key {k}"

    # ScanContext fields flow through
    assert payload["audited_stack"] == "next-app-router-monorepo"
    assert payload["secondary_stacks"] == ["next", "react"]
    assert payload["workspace_manager"] == "pnpm"
    assert payload["auditor_hints"] == ["route-file", "package-anchor"]

    # Workspace packages derived from path prefixes
    assert "apps/web" in payload["workspace_packages"]
    assert "apps/billing" in payload["workspace_packages"]
    assert "packages/core" in payload["workspace_packages"]
    # Non-workspace path NOT promoted
    assert all(
        not p.startswith("scripts/") for p in payload["workspace_packages"]
    )

    # Dev features compacted to required keys
    df_payload = payload["developer_features"]
    assert {f["name"] for f in df_payload} == {
        "auth-handlers", "billing", "scratch",
    }
    auth_entry = next(f for f in df_payload if f["name"] == "auth-handlers")
    assert auth_entry["n_paths"] == 2
    assert "apps/web/app/api/auth/route.ts" in auth_entry["sample_paths"]


# ── Test 2: user prompt body contains all key sections ─────────────────


def test_build_user_prompt_contains_all_sections(tmp_path: Path) -> None:
    feats = [_feat("auth", ["packages/core/auth.ts"])]
    payload = build_analyst_payload(_ctx(tmp_path), feats)
    payload["marketing_text"] = "== Homepage ==\nProduct landing copy."
    prompt = build_user_prompt(payload)

    assert "REPO SLUG:" in prompt
    assert "STACK (audited):" in prompt
    assert "AUDITOR HINTS" in prompt
    assert "WORKSPACE PACKAGES" in prompt
    assert "DEVELOPER FEATURES" in prompt
    assert "MARKETING SURFACES" in prompt
    # Marketing text must be embedded verbatim
    assert "Product landing copy." in prompt


# ── Test 3: parse helpers handle valid + invalid JSON ──────────────────


def test_parse_handles_code_fences_and_invalid_input() -> None:
    raw_fenced = (
        "```json\n"
        '{"product_features": [{"name": "X", "member_dev_features": []}]}\n'
        "```"
    )
    assert _strip_code_fences(raw_fenced).startswith("{")
    obj = _parse_analyst_response(raw_fenced)
    assert obj is not None
    assert obj["product_features"][0]["name"] == "X"

    # Plain object
    assert _parse_analyst_response('{"product_features": []}') is not None

    # Garbage returns None (not raises)
    assert _parse_analyst_response("not json at all") is None
    assert _parse_analyst_response("") is None
    assert _parse_analyst_response('{"foo": []}') is None  # missing key
    assert _parse_analyst_response('"a string"') is None  # not a dict


# ── Test 4: emit_product_features filters invented dev names ───────────


def test_emit_product_features_skips_invented_dev_features() -> None:
    feats = [
        _feat("auth", ["packages/core/auth.ts"]),
        _feat("billing", ["apps/billing/stripe.ts"]),
    ]
    parsed = {
        "product_features": [
            {
                "name": "Authentication",
                "description": "Login + sessions",
                "member_dev_features": ["auth", "totally-made-up"],
                "confidence": 0.9,
            },
            {
                "name": "Billing",
                "description": "Stripe + invoices",
                "member_dev_features": ["billing"],
                "confidence": 0.85,
            },
            {
                "name": "Phantom",
                "member_dev_features": ["nothing-real"],
            },
        ]
    }
    out, dev_map, member_flows_map, aux = _emit_product_features_from_analyst(parsed, feats)
    names = {pf.name for pf in out}
    assert names == {"authentication", "billing"}
    # Phantom dropped (no real members)
    assert aux["product_features_skipped_no_members"] == 1
    assert aux["invented_dev_features_skipped"] == 2  # "totally-made-up" + "nothing-real"
    # Mapping preserved (dev → product name slug)
    assert dev_map["auth"] == ("authentication",)
    assert dev_map["billing"] == ("billing",)
    # All emitted are Layer 2
    assert all(pf.layer == "product" for pf in out)


# ── Test 5: end-to-end happy path with mocked Sonnet ───────────────────


def test_run_stage_8_analyst_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Paths carry every content token of the analyst's PF names so the
    # post-LLM anti-hallucination validator passes without a retry —
    # this test exercises the HAPPY path (one call, no retry).
    feats = [
        _feat("auth-handlers", [
            "packages/core/auth.ts",
            "packages/core/oauth.ts",
            "packages/core/email-login.ts",
        ]),
        _feat("billing", [
            "apps/billing/stripe.ts",
            "apps/billing/subscriptions.ts",
        ]),
        _feat("survey-builder", ["apps/web/surveys/builder.tsx"]),
    ]
    pre_products = [_product("legacy-pf", ["apps/billing/stripe.ts"])]

    # Inject a marketing taxonomy so the payload has marketing context
    fake_tax = MarketingTaxonomy(
        repo_slug="myrepo",
        source_url="https://example.com",
        fetched_at="2026-05-21T00:00:00+00:00",
        product_features=("Authentication", "Billing", "Surveys"),
        confidence=0.9,
        notes="test",
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_analyst.fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: fake_tax,
    )

    response = _good_response([
        {
            "name": "OAuth + Email Auth",
            "description": "Sessions, magic link, OAuth",
            "member_dev_features": ["auth-handlers"],
            "confidence": 0.92,
            "grounded_in": ["packages/core", "marketing:Authentication"],
        },
        {
            "name": "Billing & Subscriptions",
            "description": "Stripe integration",
            "member_dev_features": ["billing"],
            "confidence": 0.88,
            "grounded_in": ["apps/billing", "marketing:Billing"],
        },
        {
            "name": "Survey Builder",
            "description": "Form builder",
            "member_dev_features": ["survey-builder"],
            "confidence": 0.9,
            "grounded_in": ["apps/web/surveys", "marketing:Surveys"],
        },
    ])
    client = _FakeClient(response)

    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()

    result = run_stage_8_analyst(
        _ctx(repo_root),
        feats,
        pre_products,
        dev_to_product_map_pre={"billing": ("legacy-pf",)},
        source_breakdown_pre={"rule:workspace": 1},
        client=client,
        cost_tracker=None,
    )

    assert isinstance(result, Stage8Result)
    assert result.telemetry["mode"] == "analyst"
    assert result.telemetry["source"] == "analyst:sonnet"
    assert result.telemetry["analyst_called"] is True
    assert result.telemetry["fallback_used"] is False
    assert result.telemetry["taxonomy_size"] == 3
    assert result.telemetry["product_features_emitted"] == 3

    # ONE Sonnet call, no retry
    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    assert call["model"] == DEFAULT_ANALYST_MODEL

    pf_names = {pf.name for pf in result.product_features}
    assert "oauth-+-email-auth" in pf_names
    assert "billing-&-subscriptions" in pf_names
    assert "survey-builder" in pf_names
    # Dev → product mapping replaces legacy
    assert result.dev_to_product_map["billing"] == ("billing-&-subscriptions",)


# ── Test 6: parse failure → retry → fallback to Haiku on second failure ─


def test_parse_failure_triggers_retry_then_haiku_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    feats = [_feat("auth-handlers", ["packages/core/auth.ts"])]
    pre_products = [_product("Auth (legacy)", ["packages/core/auth.ts"])]
    pre_map = {"auth-handlers": ("Auth (legacy)",)}

    # Both Sonnet calls return garbage
    client = _FakeClient("garbage response 1", "still not json 2")

    # Block Haiku's fetch_marketing_taxonomy + ensure deterministic
    # fallback path is taken inside run_stage_8 (Haiku module)
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: None,
    )

    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()

    result = run_stage_8_analyst(
        _ctx(repo_root),
        feats,
        pre_products,
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={"rule:workspace": 1},
        client=client,
        cost_tracker=None,
    )

    # TWO Sonnet calls (initial + retry)
    assert len(client.messages.calls) == 2
    # Fallback was used
    assert result.telemetry["mode"] == "analyst"
    assert result.telemetry["fallback_used"] is True
    assert result.telemetry["fallback_reason"] == "analyst-parse-failed"
    # Pre-products preserved by deterministic fallback
    assert result.product_features == pre_products
    assert result.dev_to_product_map == pre_map


# ── Test 7: customer YAML short-circuit ─────────────────────────────────


def test_customer_yaml_short_circuits_without_calling_sonnet(
    tmp_path: Path,
) -> None:
    feats = [_feat("auth-handlers", ["packages/core/auth.ts"])]
    pre_products = [_product("Authentication", ["packages/core/auth.ts"])]
    pre_map = {"auth-handlers": ("Authentication",)}
    client = _FakeClient("should-never-be-called")

    result = run_stage_8_analyst(
        _ctx(tmp_path),
        feats,
        pre_products,
        dev_to_product_map_pre=pre_map,
        source_breakdown_pre={"rule:customer-yaml": 1},
        client=client,
        cost_tracker=None,
    )
    assert result.telemetry["source"] == "customer-yaml"
    assert result.telemetry["analyst_called"] is False
    assert result.telemetry["fallback_used"] is False
    assert client.messages.calls == []


# ── Test 8: no Anthropic client → graceful Haiku fallback ──────────────


def test_no_client_falls_back_to_haiku_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    feats = [_feat("auth-handlers", ["packages/core/auth.ts"])]
    pre_products = [_product("Auth (legacy)", ["packages/core/auth.ts"])]
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: None,
    )

    result = run_stage_8_analyst(
        _ctx(tmp_path),
        feats,
        pre_products,
        dev_to_product_map_pre={"auth-handlers": ("Auth (legacy)",)},
        source_breakdown_pre={"rule:workspace": 1},
        client=None,
        cost_tracker=None,
    )
    assert result.telemetry["mode"] == "analyst"
    assert result.telemetry["fallback_used"] is True
    assert result.telemetry["fallback_reason"] == "no-client"
    assert result.product_features == pre_products


# ── Test 9: SDK exception (e.g. 500) → empty text → fallback ───────────


def test_sonnet_500_falls_back_to_haiku(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    feats = [_feat("auth-handlers", ["packages/core/auth.ts"])]
    pre_products = [_product("Auth (legacy)", ["packages/core/auth.ts"])]
    client = _FakeRaisingClient()
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: None,
    )

    result = run_stage_8_analyst(
        _ctx(tmp_path),
        feats,
        pre_products,
        dev_to_product_map_pre={"auth-handlers": ("Auth (legacy)",)},
        source_breakdown_pre={"rule:workspace": 1},
        client=client,
        cost_tracker=None,
    )

    # When Sonnet returns empty text we treat parse as None and don't
    # retry (retry only fires when text is present but invalid).
    assert result.telemetry["mode"] == "analyst"
    assert result.telemetry["fallback_used"] is True
    # Pre-products are preserved.
    assert result.product_features == pre_products


# ── Workspace-anchor blob name guard (lever 2) ──────────────────────────

# Real marker text set by PackageAnchorExtractor (see stage_8_7_anchor_desink).
_WS_MARK = "workspace anchor 'soc0-frontend' from monorepo package 'frontend/'"
_PKG_MARK = "package anchor 'auth' from dependency '@clerk/nextjs'"


def _dev_desc(name: str, paths: list[str], desc: str) -> Feature:
    f = _feat(name, paths)
    f.description = desc
    return f


def test_anchor_blob_owner_returns_dominant_workspace_anchor() -> None:
    anchor = _dev_desc(
        "soc0-frontend",
        [f"frontend/src/f{i}.tsx" for i in range(450)],
        _WS_MARK,
    )
    specific = _dev_desc("date-range", ["frontend/src/DateRange.tsx"], "")
    # sole contributor, and anchor-dominated when a minority feature joins
    assert _anchor_blob_owner([anchor]) is anchor
    assert _anchor_blob_owner([anchor, specific]) is anchor


def test_anchor_blob_owner_none_when_specific_feature_dominates() -> None:
    anchor = _dev_desc(
        "soc0-frontend", ["frontend/src/a.tsx", "frontend/src/b.tsx"], _WS_MARK,
    )
    billing = _dev_desc(
        "billing", [f"frontend/src/billing/f{i}.tsx" for i in range(10)], "",
    )
    assert _anchor_blob_owner([anchor, billing]) is None


def test_anchor_blob_owner_ignores_package_anchors() -> None:
    # Package anchors legitimately own their consumers → never guarded.
    pkg = _dev_desc(
        "auth", [f"frontend/src/auth/f{i}.tsx" for i in range(98)], _PKG_MARK,
    )
    other = _dev_desc("x", ["frontend/src/x.tsx"], "")
    assert _anchor_blob_owner([pkg, other]) is None


def test_anchor_blob_owner_requires_strict_majority() -> None:
    # Tie (anchor == others combined) is NOT a strict majority → keep name.
    anchor = _dev_desc("soc0-frontend", ["a.tsx", "b.tsx"], _WS_MARK)
    other = _dev_desc("x", ["c.tsx", "d.tsx"], "")
    assert _anchor_blob_owner([anchor, other]) is None


def test_anchor_blob_owner_none_without_anchor() -> None:
    a = _dev_desc("page-a", ["src/a.tsx"], "")
    b = _dev_desc("page-b", ["src/b.tsx"], "")
    assert _anchor_blob_owner([a, b]) is None


def test_validate_pf_names_guards_anchor_blob(tmp_path: Path) -> None:
    """A workspace-anchor-dominated PF is reslugged to the anchor (honest
    structural name) and the rename propagates into dev_map / member_flows_map,
    without any LLM retry."""
    anchor = _dev_desc(
        "soc0-frontend",
        [f"frontend/src/f{i}.tsx" for i in range(50)],
        _WS_MARK,
    )
    blob = _product("custom-date-range-and-preset-filters", list(anchor.paths))
    blob.name_confidence = "high"
    dev_map = {"soc0-frontend": ("custom-date-range-and-preset-filters",)}
    member_flows_map = {"custom-date-range-and-preset-filters": ["flow-1"]}

    telem = _validate_pf_names(
        _ctx(tmp_path),
        [blob],
        dev_map,
        member_flows_map,
        [anchor],
        {"workspace_packages": ["frontend/"], "marketing_text": ""},
        None,
        client=None,
        model="m",
        cost_tracker=None,
        llm_health=None,
        log=None,
    )

    assert telem["pf_names_anchor_guarded"] == 1
    assert telem["validator_retry_called"] is False
    assert blob.name == "soc0-frontend"
    assert blob.name_confidence == "low"
    # slug rename kept the maps consistent
    assert dev_map["soc0-frontend"] == ("soc0-frontend",)
    assert member_flows_map.get("soc0-frontend") == ["flow-1"]
    assert "custom-date-range-and-preset-filters" not in member_flows_map


def test_validate_pf_names_leaves_real_product_feature(tmp_path: Path) -> None:
    """A PF with several specific contributors and no anchor dominance keeps
    its LLM name (guard does not fire)."""
    a = _dev_desc("cases-page", [f"frontend/src/cases/f{i}.tsx" for i in range(12)], "")
    b = _dev_desc("bulk-actions", [f"frontend/src/bulk/f{i}.tsx" for i in range(10)], "")
    pf = _product("case-management-with-bulk-actions", a.paths + b.paths)
    pf.name_confidence = "high"
    dev_map = {
        "cases-page": ("case-management-with-bulk-actions",),
        "bulk-actions": ("case-management-with-bulk-actions",),
    }
    telem = _validate_pf_names(
        _ctx(tmp_path),
        [pf],
        dev_map,
        {},
        [a, b],
        {"workspace_packages": [], "marketing_text": ""},
        None,
        client=None,
        model="m",
        cost_tracker=None,
        llm_health=None,
        log=None,
    )
    assert telem["pf_names_anchor_guarded"] == 0
    assert pf.name == "case-management-with-bulk-actions"


def test_validate_pf_names_anchor_guard_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FAULTLINE_PF_ANCHOR_NAME_GUARD=0`` disables the guard entirely."""
    monkeypatch.setenv("FAULTLINE_PF_ANCHOR_NAME_GUARD", "0")
    anchor = _dev_desc(
        "soc0-frontend",
        [f"frontend/src/f{i}.tsx" for i in range(50)],
        _WS_MARK,
    )
    blob = _product("custom-date-range-and-preset-filters", list(anchor.paths))
    dev_map = {"soc0-frontend": ("custom-date-range-and-preset-filters",)}
    telem = _validate_pf_names(
        _ctx(tmp_path),
        [blob],
        dev_map,
        {},
        [anchor],
        {"workspace_packages": ["frontend/"], "marketing_text": ""},
        None,
        client=None,
        model="m",
        cost_tracker=None,
        llm_health=None,
        log=None,
    )
    # Guard did not fire (the env switch is off). Any rename now would come
    # only from the orthogonal token-validation fallback, not this guard.
    assert telem["pf_names_anchor_guarded"] == 0
