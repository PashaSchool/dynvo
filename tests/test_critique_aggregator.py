"""Tests for the recall-critique aggregator (Phase 5).

Pure-derivation logic and the orchestrator's LLM call (via a stub
client). No live API access — tests run offline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import MappingProxyType

import pytest

from faultline.aggregators.critique import (
    CritiqueAggregator,
    CritiqueFinding,
    ExpectedCategory,
    apply_findings_to_deepscan,
    build_critique_prompt,
    derive_expected_categories,
    find_missing_categories,
    is_category_covered,
    parse_critique_response,
)
from faultline.signals import LlmResponse, Signal


# ── Helpers ───────────────────────────────────────────────────────────


def _sig(sig_kind: str, source: str, **payload) -> Signal:
    return Signal(kind=sig_kind, source=source, payload=payload)


@dataclass
class _FakeLlm:
    """Stub LlmClient that returns a queued response and records calls."""

    response_text: str
    calls: list[dict] = field(default_factory=list)
    name: str = "fake"

    def complete(self, *, system, user, max_tokens, tools=None):
        self.calls.append({
            "system": system, "user": user,
            "max_tokens": max_tokens, "tools": tools,
        })
        return LlmResponse(text=self.response_text)


@dataclass
class _FakeResult:
    """Minimal DeepScanResult-shaped stub for apply_findings tests."""

    features: dict = field(default_factory=dict)
    descriptions: dict = field(default_factory=dict)


# ── derive_expected_categories ────────────────────────────────────────


def test_expected_from_package_anchor_must():
    sig = _sig(
        "expected-feature", "package-anchor-extractor",
        feature_category="Billing",
        severity="must",
        evidence=("dep:stripe",),
        manifest="package.json",
    )
    out = derive_expected_categories([sig])
    assert len(out) == 1
    ec = out[0]
    assert ec.display == "Billing"
    assert ec.severity == "must"
    assert "dep:stripe" in ec.evidence[0]
    assert ec.category == "billing"


def test_expected_from_controller_action_strips_suffix():
    sig = _sig(
        "controller-action", "mvc-controller-extractor:rails",
        controller_name="MfaController",
        action="show",
        controller_file="app/controllers/mfa_controller.rb",
    )
    out = derive_expected_categories([sig])
    assert len(out) == 1
    assert out[0].display == "Mfa"
    assert out[0].severity == "should"
    assert "mfa_controller.rb" in out[0].evidence[0]


def test_expected_from_domain_model_skips_when_hint_missing():
    sig_no_hint = _sig(
        "domain-model", "schema-domain-extractor",
        name="WidgetRow", feature_hint=None, file="app/models/widget.rb",
    )
    sig_with_hint = _sig(
        "domain-model", "schema-domain-extractor",
        name="Subscription", feature_hint="Billing",
        file="prisma/schema.prisma",
    )
    out = derive_expected_categories([sig_no_hint, sig_with_hint])
    assert len(out) == 1
    assert out[0].display == "Billing"
    assert out[0].severity == "heuristic"


def test_expected_from_route_strips_parens_in_parent_hint():
    sig = _sig(
        "route", "route-file-extractor:nextjs-app-router",
        framework="nextjs-app-router",
        method="GET", methods=("GET",),
        path="/dashboard/teams",
        handler_file="app/(dashboard)/teams/page.tsx",
        kind="page",
        parent_hint="(dashboard)",
    )
    out = derive_expected_categories([sig])
    assert len(out) == 1
    assert out[0].display == "dashboard"


def test_expected_dedupes_across_sources_and_promotes_severity():
    sigs = [
        _sig("expected-feature", "package-anchor-extractor",
             feature_category="Billing", severity="must",
             evidence=("dep:stripe",), manifest="package.json"),
        _sig("domain-model", "schema-domain-extractor",
             name="Subscription", feature_hint="Billing",
             file="prisma/schema.prisma"),
        _sig("controller-action", "mvc-controller-extractor:rails",
             controller_name="BillingController", action="index",
             controller_file="app/controllers/billing_controller.rb"),
    ]
    out = derive_expected_categories(sigs)
    assert len(out) == 1, "all three should collapse onto one category"
    ec = out[0]
    assert ec.severity == "must"
    assert len(ec.evidence) == 3
    assert {"expected-feature", "domain-model", "controller-action"} \
        <= ec.source_kinds


def test_expected_sorted_severity_desc():
    sigs = [
        _sig("expected-feature", "package-anchor-extractor",
             feature_category="Analytics", severity="may",
             evidence=("dep:posthog",), manifest="package.json"),
        _sig("expected-feature", "package-anchor-extractor",
             feature_category="Billing", severity="must",
             evidence=("dep:stripe",), manifest="package.json"),
        _sig("expected-feature", "package-anchor-extractor",
             feature_category="Email", severity="should",
             evidence=("dep:resend",), manifest="package.json"),
    ]
    out = derive_expected_categories(sigs)
    assert [e.display for e in out] == ["Billing", "Email", "Analytics"]


def test_expected_ignores_unknown_signal_kinds():
    sig = _sig("llm-feature-cluster", "sonnet-scanner", name="X")
    assert derive_expected_categories([sig]) == []


# ── matching ──────────────────────────────────────────────────────────


def test_is_category_covered_exact_normalised_match():
    ec = ExpectedCategory(
        category="billing", display="Billing",
        evidence=("dep:stripe",), severity="must",
    )
    detected = {"Billing": {"billing"}}
    assert is_category_covered(ec, detected) == "Billing"


def test_is_category_covered_token_subset():
    ec = ExpectedCategory(
        category="auth", display="Auth",
        evidence=("dep:nextauth",), severity="must",
    )
    # Detected feature is more specific ("Auth Sessions") — its tokens
    # are a superset of the expected category's tokens.
    detected = {"Auth Sessions": {"auth", "sessions"}}
    assert is_category_covered(ec, detected) == "Auth Sessions"


def test_is_category_covered_returns_none_when_no_overlap():
    ec = ExpectedCategory(
        category="billing", display="Billing",
        evidence=("dep:stripe",), severity="must",
    )
    detected = {"Dashboard": {"dashboard"}, "Settings": {"settings"}}
    assert is_category_covered(ec, detected) is None


def test_find_missing_categories_filters_covered():
    expected = [
        ExpectedCategory(category="billing", display="Billing",
                         evidence=("dep:stripe",), severity="must"),
        ExpectedCategory(category="auth", display="Auth",
                         evidence=("dep:nextauth",), severity="must"),
    ]
    missing = find_missing_categories(expected, ["Billing", "Dashboard"])
    assert [m.display for m in missing] == ["Auth"]


# ── prompt ────────────────────────────────────────────────────────────


def test_build_critique_prompt_returns_json_user_payload():
    missing = [
        ExpectedCategory(category="auth", display="Auth",
                         evidence=("dep:nextauth (package.json, severity=must)",),
                         severity="must"),
    ]
    system, user = build_critique_prompt(
        detected_features=["Dashboard", "Billing"],
        missing=missing,
    )
    assert "JSON" in system
    # The JSON body should be parseable on its own.
    json_start = user.index("{")
    payload = json.loads(user[json_start:])
    assert payload["detected_features"] == ["Billing", "Dashboard"]
    assert payload["candidate_missing_categories"][0]["category"] == "auth"
    assert payload["candidate_missing_categories"][0]["severity"] == "must"


# ── response parsing ──────────────────────────────────────────────────


def _missing_idx(*displays):
    return {
        d.lower(): ExpectedCategory(
            category=d.lower(), display=d,
            evidence=(f"dep:{d}",), severity="must",
        )
        for d in displays
    }


def test_parse_critique_response_happy_path():
    raw = json.dumps({
        "missed": [
            {"feature_name": "Two-Factor Authentication",
             "matched_category": "mfa",
             "files": ["app/controllers/mfa_controller.rb"],
             "rationale": "Dedicated MFA flow not covered."},
        ],
        "covered": [],
    })
    findings = parse_critique_response(
        raw, missing_by_key=_missing_idx("mfa"),
    )
    assert len(findings) == 1
    assert findings[0].feature_name == "Two-Factor Authentication"
    assert findings[0].files == ("app/controllers/mfa_controller.rb",)
    assert findings[0].matched_categories == ("mfa",)


def test_parse_critique_response_strips_markdown_fence():
    raw = "```json\n" + json.dumps({"missed": []}) + "\n```"
    assert parse_critique_response(raw, missing_by_key={}) == []


def test_parse_critique_response_drops_unknown_category():
    raw = json.dumps({"missed": [
        {"feature_name": "X", "matched_category": "ghost",
         "files": ["a.rb"], "rationale": ""},
    ]})
    assert parse_critique_response(
        raw, missing_by_key=_missing_idx("mfa"),
    ) == []


def test_parse_critique_response_handles_malformed_json():
    assert parse_critique_response("not json {{", missing_by_key={}) == []


def test_parse_critique_response_filters_nonexistent_files(tmp_path):
    """When repo_root is supplied, files not on disk get dropped."""
    (tmp_path / "real.rb").write_text("x")
    raw = json.dumps({"missed": [
        {"feature_name": "Mfa", "matched_category": "mfa",
         "files": ["real.rb", "fake.rb"], "rationale": ""},
    ]})
    findings = parse_critique_response(
        raw, missing_by_key=_missing_idx("mfa"), repo_root=tmp_path,
    )
    assert len(findings) == 1
    assert findings[0].files == ("real.rb",)


def test_parse_critique_response_skips_file_check_when_repo_root_none():
    raw = json.dumps({"missed": [
        {"feature_name": "Mfa", "matched_category": "mfa",
         "files": ["any/path.rb"], "rationale": ""},
    ]})
    findings = parse_critique_response(
        raw, missing_by_key=_missing_idx("mfa"),
    )
    assert findings[0].files == ("any/path.rb",)


def test_parse_critique_response_drops_finding_with_blank_name():
    raw = json.dumps({"missed": [
        {"feature_name": "  ", "matched_category": "mfa",
         "files": ["a.rb"], "rationale": ""},
    ]})
    assert parse_critique_response(
        raw, missing_by_key=_missing_idx("mfa"),
    ) == []


# ── orchestrator ──────────────────────────────────────────────────────


def test_orchestrator_skips_llm_when_no_missing():
    sigs = [
        _sig("expected-feature", "package-anchor-extractor",
             feature_category="Billing", severity="must",
             evidence=("dep:stripe",), manifest="package.json"),
    ]
    llm = _FakeLlm(response_text="should-not-be-called")
    agg = CritiqueAggregator()

    out = agg.run(
        detected_features=["Billing"],
        signals=sigs,
        llm=llm,
    )
    assert out == []
    assert llm.calls == []


def test_orchestrator_calls_llm_and_returns_findings():
    sigs = [
        _sig("controller-action", "mvc-controller-extractor:rails",
             controller_name="MfaController", action="show",
             controller_file="app/controllers/mfa_controller.rb"),
    ]
    raw = json.dumps({"missed": [
        {"feature_name": "Two-Factor Authentication",
         "matched_category": "mfa",
         "files": ["app/controllers/mfa_controller.rb"],
         "rationale": "MFA setup + verification flow."},
    ]})
    llm = _FakeLlm(response_text=raw)

    out = CritiqueAggregator().run(
        detected_features=["Dashboard"],
        signals=sigs,
        llm=llm,
    )
    assert len(out) == 1
    assert out[0].feature_name == "Two-Factor Authentication"
    assert len(llm.calls) == 1
    # The system prompt is the recall-critique one (not naming).
    assert "missed" in llm.calls[0]["system"].lower()


def test_orchestrator_caps_candidate_count():
    # 20 must-severity signals, max_candidates=3 → only 3 reach the
    # prompt. We verify by inspecting the recorded user payload.
    sigs = [
        _sig("expected-feature", "package-anchor-extractor",
             feature_category=f"Cat{i:02d}", severity="must",
             evidence=(f"dep:lib{i}",), manifest="package.json")
        for i in range(20)
    ]
    llm = _FakeLlm(response_text=json.dumps({"missed": []}))
    agg = CritiqueAggregator(max_candidates=3)
    agg.run(detected_features=[], signals=sigs, llm=llm)

    user = llm.calls[0]["user"]
    payload = json.loads(user[user.index("{"):])
    assert len(payload["candidate_missing_categories"]) == 3


def test_orchestrator_swallows_llm_exception():
    class _Boom:
        def complete(self, **_kw):
            raise RuntimeError("transport failure")

    sigs = [
        _sig("controller-action", "mvc-controller-extractor:rails",
             controller_name="MfaController", action="show",
             controller_file="x.rb"),
    ]
    out = CritiqueAggregator().run(
        detected_features=[], signals=sigs, llm=_Boom(),
    )
    assert out == []


# ── apply_findings_to_deepscan ────────────────────────────────────────


def test_apply_findings_merges_into_result():
    result = _FakeResult(features={"Dashboard": ["app/page.tsx"]})
    findings = [CritiqueFinding(
        feature_name="Two-Factor Authentication",
        files=("app/controllers/mfa_controller.rb",),
        rationale="MFA flow.",
        matched_categories=("mfa",),
    )]
    apply_findings_to_deepscan(result, findings)
    assert "Two-Factor Authentication" in result.features
    assert result.features["Two-Factor Authentication"] \
        == ["app/controllers/mfa_controller.rb"]
    assert result.descriptions["Two-Factor Authentication"] \
        .startswith("[critique]")


def test_apply_findings_skips_existing_feature_name():
    result = _FakeResult(features={"Auth": ["app/auth.rb"]})
    findings = [CritiqueFinding(
        feature_name="Auth", files=("a.rb",),
        rationale="x", matched_categories=("auth",),
    )]
    apply_findings_to_deepscan(result, findings)
    assert result.features["Auth"] == ["app/auth.rb"]  # untouched
    assert "Auth" not in result.descriptions


def test_apply_findings_skips_finding_with_no_files():
    result = _FakeResult()
    findings = [CritiqueFinding(
        feature_name="Empty", files=(),
        rationale="x", matched_categories=("x",),
    )]
    apply_findings_to_deepscan(result, findings)
    assert result.features == {}
