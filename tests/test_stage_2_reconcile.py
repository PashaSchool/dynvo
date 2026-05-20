"""Tests for ``faultline.pipeline_v2.stage_2_reconcile``.

We construct :class:`AnchorCandidate` lists directly to exercise the
merge / priority / attribution / LLM-2nd-opinion paths in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2 import (
    AnchorCandidate,
    ScanContext,
    stage_2_reconcile,
)
from faultline.pipeline_v2.stage_2_reconcile import (
    DeveloperFeature,
    _jaccard,
    _slug_tokens,
)


def _ctx(tmp_path: Path, files: list[str]) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


def _cand(name: str, source: str, paths: tuple[str, ...] = (),
          confidence_self: float = 0.7) -> AnchorCandidate:
    return AnchorCandidate(
        name=name, source=source, paths=paths, confidence_self=confidence_self,
    )


# ── merge by name ──────────────────────────────────────────────────────────


def test_two_sources_same_name_merge_to_high_confidence(tmp_path: Path) -> None:
    """Route + package both say "billing" → one merged feature, sources
    listed in priority order (package first)."""
    cands = {
        "route":   [_cand("billing", "route", ("app/billing/page.tsx",))],
        "package": [_cand("billing", "package", (".",))],
    }
    ctx = _ctx(tmp_path, files=["app/billing/page.tsx", "package.json"])

    result = stage_2_reconcile(cands, ctx)

    assert len(result.features) == 1
    f = result.features[0]
    assert f.name == "billing"
    assert f.confidence == "high"
    # package outranks route per priority — listed first.
    assert f.sources == ["package", "route"]


def test_single_source_yields_medium_confidence(tmp_path: Path) -> None:
    cands = {
        "schema": [_cand("subscription", "schema", ("db/schema.ts",))],
    }
    ctx = _ctx(tmp_path, files=["db/schema.ts"])
    result = stage_2_reconcile(cands, ctx)
    assert len(result.features) == 1
    assert result.features[0].confidence == "medium"
    assert result.features[0].sources == ["schema"]


def test_conflicting_slugs_pick_by_priority(tmp_path: Path) -> None:
    """``users`` (route) and ``user-api`` (package) overlap on tokens.

    Default Jaccard for {"users"} vs {"user", "api"} is 0/3 = 0.0 —
    they will NOT merge under threshold 0.7. To exercise the priority
    rule we use slugs that DO share tokens: ``user-mgmt`` (mvc) vs
    ``user-mgmt-api`` (package). Jaccard = 2/3 = 0.67 → no merge at
    default threshold, but at threshold 0.66 they merge. So we pass
    a lower threshold here to force the merge and check priority wins.
    """
    cands = {
        "mvc":     [_cand("user-mgmt", "mvc", ("app/controllers/user_mgmt_controller.rb",))],
        "package": [_cand("user-mgmt-api", "package", (".",))],
    }
    ctx = _ctx(tmp_path, files=["app/controllers/user_mgmt_controller.rb", "package.json"])

    result = stage_2_reconcile(cands, ctx, jaccard_threshold=0.66)

    assert len(result.features) == 1
    f = result.features[0]
    # Package wins the slug because of priority
    assert f.name == "user-mgmt-api"
    assert f.confidence == "high"
    assert f.sources[0] == "package"


def test_disjoint_slugs_stay_separate(tmp_path: Path) -> None:
    cands = {
        "route":   [_cand("billing", "route", ("app/billing/page.tsx",))],
        "package": [_cand("auth",   "package", (".",))],
    }
    ctx = _ctx(tmp_path, files=["app/billing/page.tsx", "package.json"])
    result = stage_2_reconcile(cands, ctx)
    assert {f.name for f in result.features} == {"billing", "auth"}
    # Both single-source ⇒ medium each
    assert all(f.confidence == "medium" for f in result.features)


# ── cross-feature path attribution ────────────────────────────────────────


def test_file_claimed_by_two_features_goes_to_higher_priority(tmp_path: Path) -> None:
    """``app/billing/handler.ts`` claimed by both ``billing`` (route)
    and ``payments`` (package). Package outranks → payments keeps it,
    billing drops it."""
    cands = {
        "route":   [_cand(
            "billing", "route",
            ("app/billing/handler.ts", "app/billing/page.tsx"),
        )],
        "package": [_cand(
            "payments", "package",
            ("app/billing/handler.ts",),
        )],
    }
    ctx = _ctx(
        tmp_path,
        files=["app/billing/handler.ts", "app/billing/page.tsx"],
    )

    result = stage_2_reconcile(cands, ctx)

    by_name = {f.name: f for f in result.features}
    assert set(by_name) == {"billing", "payments"}
    # payments wins the contested file
    assert "app/billing/handler.ts" in by_name["payments"].paths
    # billing dropped it but kept its other path
    assert "app/billing/handler.ts" not in by_name["billing"].paths
    assert "app/billing/page.tsx" in by_name["billing"].paths


def test_unattributed_paths_returned(tmp_path: Path) -> None:
    cands = {
        "route": [_cand("billing", "route", ("app/billing/page.tsx",))],
    }
    files = ["app/billing/page.tsx", "lib/random-util.ts", "README.md"]
    ctx = _ctx(tmp_path, files=files)
    result = stage_2_reconcile(cands, ctx)
    assert set(result.unattributed) == {"lib/random-util.ts", "README.md"}


def test_empty_candidates_yields_all_files_unattributed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, files=["a.ts", "b.ts"])
    result = stage_2_reconcile({}, ctx)
    assert result.features == []
    assert result.unattributed == ["a.ts", "b.ts"]


def test_errors_sentinel_is_ignored(tmp_path: Path) -> None:
    """Stage 2 must drop the ``_errors`` key without exploding."""
    cands = {
        "route": [_cand("auth", "route", ("app/auth/page.tsx",))],
        "_errors": {"boom": "RuntimeError: x"},  # type: ignore[dict-item]
    }
    ctx = _ctx(tmp_path, files=["app/auth/page.tsx"])
    result = stage_2_reconcile(cands, ctx)
    assert [f.name for f in result.features] == ["auth"]


# ── LLM 2nd-opinion ────────────────────────────────────────────────────────


def test_llm_reconcile_disabled_by_default_no_llm_calls(tmp_path: Path) -> None:
    """Default ``llm_reconcile=False`` MUST NOT invoke the LLM stub."""
    call_log: list[tuple] = []

    def _llm_stub(a, b):  # type: ignore[no-untyped-def]
        call_log.append((a.name, b.name))
        return "should-not-fire"

    cands = {
        # Two slugs in the ambiguous Jaccard band (0.3..0.6)
        "mvc":     [_cand("user-profile", "mvc", ("app/controllers/user_profile_controller.rb",))],
        "package": [_cand("user-account", "package", (".",))],
    }
    ctx = _ctx(tmp_path, files=["app/controllers/user_profile_controller.rb", "package.json"])

    # Force them into the same group by lowering threshold below their Jaccard:
    j = _jaccard(_slug_tokens("user-profile"), _slug_tokens("user-account"))
    assert 0.3 <= j <= 0.6  # sanity — this is exactly the ambiguous band
    result = stage_2_reconcile(
        cands, ctx, jaccard_threshold=j, _llm_call=_llm_stub,
    )

    # No LLM calls
    assert call_log == []
    # Priority rule wins → package's slug
    assert any(f.name == "user-account" for f in result.features)


def test_llm_reconcile_enabled_overrides_priority(tmp_path: Path) -> None:
    """When ``llm_reconcile=True`` and Jaccard is in the ambiguous band,
    the LLM stub's choice wins — even over the priority rule."""
    cands = {
        "mvc":     [_cand("user-profile", "mvc",
                          ("app/controllers/user_profile_controller.rb",))],
        "package": [_cand("user-account", "package", (".",))],
    }
    ctx = _ctx(tmp_path, files=["app/controllers/user_profile_controller.rb", "package.json"])
    j = _jaccard(_slug_tokens("user-profile"), _slug_tokens("user-account"))

    def _llm_stub(a, b):  # type: ignore[no-untyped-def]
        # Pick the LOWER-priority candidate's name to prove the 2nd
        # opinion overrides the priority rule.
        return "user-profile"

    result = stage_2_reconcile(
        cands, ctx,
        jaccard_threshold=j,
        llm_reconcile=True,
        _llm_call=_llm_stub,
    )
    assert any(f.name == "user-profile" for f in result.features)
    # The reconciliation note records the LLM decision
    assert any("llm picked" in n for n in result.notes)


def test_llm_reconcile_returns_none_falls_back_to_priority(tmp_path: Path) -> None:
    """When the LLM stub returns ``None`` (inconclusive), the priority
    rule still wins."""
    cands = {
        "mvc":     [_cand("user-profile", "mvc",
                          ("app/controllers/user_profile_controller.rb",))],
        "package": [_cand("user-account", "package", (".",))],
    }
    ctx = _ctx(tmp_path, files=["app/controllers/user_profile_controller.rb", "package.json"])
    j = _jaccard(_slug_tokens("user-profile"), _slug_tokens("user-account"))

    def _llm_stub(a, b):  # type: ignore[no-untyped-def]
        return None  # inconclusive

    result = stage_2_reconcile(
        cands, ctx,
        jaccard_threshold=j,
        llm_reconcile=True,
        _llm_call=_llm_stub,
    )
    # priority rule → package
    assert any(f.name == "user-account" for f in result.features)


# ── Sprint S4b — URL-ghost purge ──────────────────────────────────────────


def test_s4b_shared_route_file_zero_path_protection(tmp_path: Path) -> None:
    """Three Fastify routes share one source file (the real-world
    pattern from infisical's ``secret-scanner-v2.ts`` declaring
    ``/bitbucket`` + ``/gitlab`` + ``/github``).

    Without the Sprint S4b zero-path protection, two slugs would lose
    the contested file and end up with empty paths → "URL ghost"
    features. With the fix all three keep the file so each remains
    attributable downstream.
    """
    shared_file = "backend/src/server/plugins/secret-scanner-v2.ts"
    cands = {
        "route-fastify": [
            _cand("bitbucket", "route-fastify", (shared_file,)),
            _cand("gitlab", "route-fastify", (shared_file,)),
            _cand("github", "route-fastify", (shared_file,)),
        ],
    }
    ctx = _ctx(tmp_path, files=[shared_file])

    result = stage_2_reconcile(cands, ctx)

    by_name = {f.name: f for f in result.features}
    # All three survive (no zero-path drops).
    assert set(by_name) == {"bitbucket", "gitlab", "github"}
    # Each shares the file rather than one stealing it. Provenance
    # preserved for every slug.
    for name in ("bitbucket", "gitlab", "github"):
        assert shared_file in by_name[name].paths, (
            f"{name} lost its shared file"
        )
    assert result.zero_path_drops_count == 0
    assert result.zero_path_drops_sample == []


def test_s4b_path_contention_winner_still_wins_when_loser_has_other_paths(
    tmp_path: Path,
) -> None:
    """Zero-path protection must NOT regress the existing "loser
    drops contested path" semantics when the loser has fallback paths.

    Replays
    ``test_file_claimed_by_two_features_goes_to_higher_priority`` to
    verify the new pass-2 only fires when the loser would be orphaned.
    """
    cands = {
        "route":   [_cand(
            "billing", "route",
            ("app/billing/handler.ts", "app/billing/page.tsx"),
        )],
        "package": [_cand(
            "payments", "package",
            ("app/billing/handler.ts",),
        )],
    }
    ctx = _ctx(
        tmp_path,
        files=["app/billing/handler.ts", "app/billing/page.tsx"],
    )

    result = stage_2_reconcile(cands, ctx)

    by_name = {f.name: f for f in result.features}
    assert set(by_name) == {"billing", "payments"}
    # payments wins outright — the loser ('billing') has another path
    # to fall back on, so the strip applies normally.
    assert "app/billing/handler.ts" in by_name["payments"].paths
    assert "app/billing/handler.ts" not in by_name["billing"].paths
    assert "app/billing/page.tsx" in by_name["billing"].paths


def test_s4b_zero_path_features_dropped_with_telemetry(tmp_path: Path) -> None:
    """Defensive backstop: a candidate that somehow arrives with no
    paths gets dropped after reconciliation and surfaces in telemetry.

    Constructs a candidate with empty paths directly — simulating any
    future extractor that emits a name but loses path attribution.
    """
    cands = {
        "config": [_cand("phantom-feature", "config", ())],
        "route":  [_cand("billing", "route", ("app/billing.tsx",))],
    }
    ctx = _ctx(tmp_path, files=["app/billing.tsx"])

    result = stage_2_reconcile(cands, ctx)

    # billing survives; phantom-feature dropped.
    names = {f.name for f in result.features}
    assert names == {"billing"}
    assert result.zero_path_drops_count == 1
    assert "phantom-feature" in result.zero_path_drops_sample
    # Note logged for downstream telemetry consumers.
    assert any("zero-path" in n for n in result.notes)


def test_s4b_no_zero_path_drops_in_normal_case(tmp_path: Path) -> None:
    """Sanity: a healthy reconciliation reports zero-path-drops = 0."""
    cands = {
        "route":   [_cand("billing", "route", ("app/billing/page.tsx",))],
        "package": [_cand("payments", "package", (".",))],
    }
    ctx = _ctx(
        tmp_path,
        files=["app/billing/page.tsx", "package.json"],
    )

    result = stage_2_reconcile(cands, ctx)

    assert result.zero_path_drops_count == 0
    assert result.zero_path_drops_sample == []
