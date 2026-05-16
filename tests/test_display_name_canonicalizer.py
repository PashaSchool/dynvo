"""Tests for the Sprint 5 display-name canonicalizer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from faultline.aggregators.display_name_canonicalizer import (
    _looks_engine_generated,
    apply_llm_canonicalization,
    apply_nav_labels,
    build_nav_label_index,
    is_enabled_llm_canonicalize,
)
from faultline.signals import LlmResponse, Signal


@dataclass
class _Feat:
    name: str
    display_name: str | None = None
    description: str = ""


@dataclass
class _Map:
    features: list = field(default_factory=list)


def _nav_sig(href: str, label: str) -> Signal:
    return Signal(
        kind="nav-link", source="jsx-nav-extractor",
        payload={"href": href, "label": label, "file": "x.tsx"},
    )


# ── _looks_engine_generated ──────────────────────────────────────────


def test_looks_generated_when_display_name_missing():
    assert _looks_engine_generated(None, "billing") is True
    assert _looks_engine_generated("", "billing") is True


def test_looks_generated_when_display_name_is_titlecased_slug():
    assert _looks_engine_generated("Api Access", "api-access") is True
    assert _looks_engine_generated("User Management", "user-management") is True


def test_doesnt_look_generated_when_clearly_better():
    """Author/LLM-set names shouldn't be overwritten."""
    assert _looks_engine_generated(
        "Plugin Extensibility", "notification-plugins",
    ) is False
    assert _looks_engine_generated(
        "Multi-Factor Authentication", "mfa",
    ) is False


# ── nav label index ──────────────────────────────────────────────────


def test_index_picks_shortest_label_on_conflict():
    """Two nav links to /billing with different labels — shortest
    wins (presumed canonical)."""
    sigs = [
        _nav_sig("/billing", "Billing & Invoices"),
        _nav_sig("/billing", "Billing"),
    ]
    idx = build_nav_label_index(sigs)
    assert idx == {"billing": "Billing"}


def test_index_skips_empty_labels():
    sigs = [_nav_sig("/billing", "")]
    assert build_nav_label_index(sigs) == {}


def test_index_uses_last_url_segment():
    sigs = [_nav_sig("/dashboard/billing", "Billing")]
    idx = build_nav_label_index(sigs)
    assert "billing" in idx


# ── nav label match ──────────────────────────────────────────────────


def test_apply_nav_labels_canonicalizes_slug_to_label():
    fm = _Map(features=[
        _Feat(name="billing", display_name="Billing"),  # auto-derived
    ])
    sigs = [_nav_sig("/billing", "Billing & Subscriptions")]
    n = apply_nav_labels(fm, sigs)
    assert n == 1
    assert fm.features[0].display_name == "Billing & Subscriptions"


def test_apply_nav_labels_preserves_author_set_display_name():
    """Display names that look meaningfully different from the slug
    are preserved (Pass A doesn't overwrite richer names).
    """
    fm = _Map(features=[
        _Feat(name="notification-plugins",
              display_name="Plugin Extensibility"),
    ])
    sigs = [_nav_sig("/notification-plugins", "Notify")]
    n = apply_nav_labels(fm, sigs)
    assert n == 0
    assert fm.features[0].display_name == "Plugin Extensibility"


def test_apply_nav_labels_skips_when_no_match():
    fm = _Map(features=[_Feat(name="billing", display_name="Billing")])
    sigs = [_nav_sig("/dashboard", "Dashboard")]
    assert apply_nav_labels(fm, sigs) == 0
    assert fm.features[0].display_name == "Billing"


def test_apply_nav_labels_matches_via_path_segment():
    """Feature slug (auth)/billing → nav match on 'billing'."""
    fm = _Map(features=[_Feat(name="(dashboard)/billing",
                              display_name="(dashboard)/billing")])
    sigs = [_nav_sig("/billing", "Billing")]
    n = apply_nav_labels(fm, sigs)
    assert n == 1
    assert fm.features[0].display_name == "Billing"


def test_apply_nav_labels_returns_zero_with_no_signals():
    fm = _Map(features=[_Feat(name="billing")])
    assert apply_nav_labels(fm, []) == 0


# ── env var gate ─────────────────────────────────────────────────────


def test_llm_gate_off_by_default(monkeypatch):
    monkeypatch.delenv("FAULTLINE_LLM_CANONICALIZE", raising=False)
    assert is_enabled_llm_canonicalize() is False


@pytest.mark.parametrize("v", ["1", "true", "yes", "on", "TRUE"])
def test_llm_gate_truthy(monkeypatch, v):
    monkeypatch.setenv("FAULTLINE_LLM_CANONICALIZE", v)
    assert is_enabled_llm_canonicalize() is True


# ── LLM pass ─────────────────────────────────────────────────────────


@dataclass
class _StubLLM:
    response_text: str
    calls: list = field(default_factory=list)
    name: str = "fake"

    def complete(self, *, system, user, max_tokens, tools=None):
        self.calls.append({"system": system, "user": user})
        return LlmResponse(text=self.response_text)


def test_llm_pass_replaces_engine_generated_names():
    fm = _Map(features=[
        _Feat(name="notification-plugins",
              display_name="Notification Plugins",  # auto from slug
              description="Pluggable adapters for email/SMS/Slack."),
    ])
    raw = json.dumps({"names": [
        {"slug": "notification-plugins",
         "display_name": "Plugin Extensibility"},
    ]})
    n = apply_llm_canonicalization(fm, _StubLLM(response_text=raw))
    assert n == 1
    assert fm.features[0].display_name == "Plugin Extensibility"


def test_llm_pass_skips_when_suggestion_is_just_respaced_slug():
    """Don't update when the LLM's "suggestion" is structurally the
    same as the slug (no real value added).
    """
    fm = _Map(features=[
        _Feat(name="user-management",
              display_name="User Management"),
    ])
    raw = json.dumps({"names": [
        {"slug": "user-management",
         "display_name": "User Management"},
    ]})
    n = apply_llm_canonicalization(fm, _StubLLM(response_text=raw))
    assert n == 0


def test_llm_pass_skips_when_existing_name_already_clean():
    """Pass A (or LLM detection) already gave a good name → skip."""
    fm = _Map(features=[
        _Feat(name="mfa",
              display_name="Multi-Factor Authentication"),
    ])
    raw = json.dumps({"names": [
        {"slug": "mfa", "display_name": "MFA"},
    ]})
    n = apply_llm_canonicalization(fm, _StubLLM(response_text=raw))
    # Existing name is rich; canonicalizer leaves it alone.
    assert n == 0
    assert fm.features[0].display_name == "Multi-Factor Authentication"


def test_llm_pass_handles_malformed_json():
    fm = _Map(features=[_Feat(name="x", description="y")])
    n = apply_llm_canonicalization(fm, _StubLLM(response_text="not json"))
    assert n == 0


def test_llm_pass_handles_null_suggestion_in_response():
    fm = _Map(features=[_Feat(name="x", description="y")])
    raw = json.dumps({"names": [
        {"slug": "x", "display_name": None},
    ]})
    n = apply_llm_canonicalization(fm, _StubLLM(response_text=raw))
    assert n == 0


def test_llm_pass_caps_batch():
    """When more features than max_batch need help, only the first
    max_batch go to the LLM."""
    fm = _Map(features=[_Feat(name=f"f{i}", description=f"d{i}")
                        for i in range(20)])
    llm = _StubLLM(response_text=json.dumps({"names": []}))
    apply_llm_canonicalization(fm, llm, max_batch=3)
    assert len(llm.calls) == 1
    payload = llm.calls[0]["user"]
    json_start = payload.index("{")
    parsed = json.loads(payload[json_start:])
    assert len(parsed["features"]) == 3


# ── strip_page_suffix (Sprint 5) ─────────────────────────────────────


def _strip_fm(features):
    """Local map factory for strip_page_suffix tests. Sprint 10a —
    uses REAL ``FeatureMap`` + ``Feature`` so the pure-function
    ``strip_page_suffix`` can call ``model_copy(deep=True)`` on it.
    """
    from datetime import datetime, timezone
    from faultline.models.types import Feature, FeatureMap
    real_feats = [
        Feature(
            name=f.name,
            display_name=getattr(f, "display_name", None),
            paths=[], authors=[], total_commits=0, bug_fixes=0,
            bug_fix_ratio=0.0,
            last_modified=datetime.now(tz=timezone.utc),
            health_score=99.0, flows=[],
        )
        for f in features
    ]
    return FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=datetime.now(tz=timezone.utc),
        total_commits=0, date_range_days=365,
        features=real_feats,
    )


def test_strip_page_suffix_removes_trailing_page():
    from faultline.aggregators.display_name_canonicalizer import strip_page_suffix
    feat = _Feat(name="documents", display_name="Documents Page")
    fm = _strip_fm([feat])
    new_fm, n = strip_page_suffix(fm)
    assert n == 1
    assert new_fm.features[0].display_name == "Documents"


def test_strip_page_suffix_removes_trailing_pages_plural():
    from faultline.aggregators.display_name_canonicalizer import strip_page_suffix
    feat = _Feat(name="auth", display_name="Authentication Pages")
    fm = _strip_fm([feat])
    new_fm, n = strip_page_suffix(fm)
    assert n == 1
    assert new_fm.features[0].display_name == "Authentication"


def test_strip_page_suffix_removes_trailing_screen():
    from faultline.aggregators.display_name_canonicalizer import strip_page_suffix
    feat = _Feat(name="settings", display_name="Settings Screen")
    fm = _strip_fm([feat])
    new_fm, n = strip_page_suffix(fm)
    assert n == 1
    assert new_fm.features[0].display_name == "Settings"


def test_strip_page_suffix_removes_trailing_view():
    from faultline.aggregators.display_name_canonicalizer import strip_page_suffix
    feat = _Feat(name="inbox", display_name="Inbox View")
    fm = _strip_fm([feat])
    new_fm, n = strip_page_suffix(fm)
    assert n == 1
    assert new_fm.features[0].display_name == "Inbox"


def test_strip_page_suffix_noop_when_no_suffix():
    from faultline.aggregators.display_name_canonicalizer import strip_page_suffix
    feat = _Feat(name="billing", display_name="Billing")
    fm = _strip_fm([feat])
    new_fm, n = strip_page_suffix(fm)
    assert n == 0
    assert new_fm.features[0].display_name == "Billing"


def test_strip_page_suffix_noop_when_display_name_none():
    from faultline.aggregators.display_name_canonicalizer import strip_page_suffix
    feat = _Feat(name="billing", display_name=None)
    fm = _strip_fm([feat])
    new_fm, n = strip_page_suffix(fm)
    assert n == 0
    assert new_fm.features[0].display_name is None


def test_strip_page_suffix_does_not_strip_mid_word():
    """``"Page Builder"`` must stay — Page is the noun, not a suffix."""
    from faultline.aggregators.display_name_canonicalizer import strip_page_suffix
    feat = _Feat(name="page-builder", display_name="Page Builder")
    fm = _strip_fm([feat])
    new_fm, n = strip_page_suffix(fm)
    assert n == 0
    assert new_fm.features[0].display_name == "Page Builder"
