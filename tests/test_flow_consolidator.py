"""Tests for the per-feature flow consolidator (Sprint 1)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from faultline.aggregators.flow_consolidator import (
    FlowConsolidator,
    _is_pure_page_flow,
    _is_redundant_with_feature,
    _signature,
)


@dataclass
class _Flow:
    name: str


@dataclass
class _Feat:
    name: str
    flows: list = field(default_factory=list)


@dataclass
class _Map:
    features: list = field(default_factory=list)


def _f(name: str) -> _Flow:
    return _Flow(name=name)


# ── _signature ────────────────────────────────────────────────────────


def test_signature_basic():
    assert _signature("create-rule-flow") == ("create", "rule")


def test_signature_collapses_synonyms():
    """edit and update both → "edit" canonical verb."""
    assert _signature("edit-rule-flow")[0] == "edit"
    assert _signature("update-rule-flow")[0] == "edit"


def test_signature_strips_stop_words():
    assert _signature("configure-the-assistant-settings-flow") == ("configure", "assistant")


def test_signature_returns_none_for_single_token():
    assert _signature("submit-flow") is None


def test_signature_returns_none_for_pure_stops():
    assert _signature("the-page-flow") is None


def test_signature_login_synonym():
    assert _signature("login-flow") is None  # single content token after stop removal
    assert _signature("login-user-flow") == ("authenticate", "user")


# ── _is_pure_page_flow ────────────────────────────────────────────────


def test_pure_page_flow_view_x_page():
    assert _is_pure_page_flow("view-dashboard-page-flow") is True
    assert _is_pure_page_flow("open-settings-screen-flow") is True


def test_pure_page_flow_negative():
    assert _is_pure_page_flow("create-rule-flow") is False
    assert _is_pure_page_flow("submit-form-flow") is False


# ── _is_redundant_with_feature ────────────────────────────────────────


def test_redundant_when_flow_is_just_feature_slug():
    assert _is_redundant_with_feature(
        "inbox-zero-ai-elements-flow", "inbox-zero-ai/ai-elements",
    ) is True


def test_not_redundant_when_flow_adds_action():
    assert _is_redundant_with_feature(
        "create-rule-flow", "rules",
    ) is False


# ── consolidator behaviour ────────────────────────────────────────────


def test_consolidator_groups_synonym_flows():
    """edit and update flows for the same noun collapse to one."""
    fm = _Map(features=[_Feat(name="rules", flows=[
        _f("edit-rule-flow"),
        _f("update-rule-flow"),
        _f("modify-rule-flow"),
    ])])
    out = FlowConsolidator().consolidate(fm)
    assert out == (3, 1)
    assert len(fm.features[0].flows) == 1
    # Shortest name wins.
    assert fm.features[0].flows[0].name == "edit-rule-flow"


def test_consolidator_keeps_distinct_journeys():
    fm = _Map(features=[_Feat(name="rules", flows=[
        _f("create-rule-flow"),
        _f("delete-rule-flow"),
        _f("test-rule-flow"),
    ])])
    out = FlowConsolidator().consolidate(fm)
    assert out == (3, 3)


def test_consolidator_drops_pure_page_flows():
    fm = _Map(features=[_Feat(name="rules", flows=[
        _f("view-rules-page-flow"),
        _f("open-rule-screen-flow"),
        _f("create-rule-flow"),
    ])])
    FlowConsolidator().consolidate(fm)
    names = {fl.name for fl in fm.features[0].flows}
    assert names == {"create-rule-flow"}


def test_consolidator_drops_feature_redundant_flow():
    fm = _Map(features=[_Feat(name="ai-elements", flows=[
        _f("ai-elements-flow"),
        _f("create-element-flow"),
    ])])
    FlowConsolidator().consolidate(fm)
    names = {fl.name for fl in fm.features[0].flows}
    assert names == {"create-element-flow"}


def test_consolidator_caps_at_max():
    """After grouping, cap removes the longest names first."""
    fm = _Map(features=[_Feat(name="x", flows=[
        _f("aa-flow"),
        _f("bbb-flow"),
        _f("cccc-flow"),
        _f("ddddd-flow"),
        _f("eeeeee-flow"),
        _f("fffffff-flow"),
        _f("gggggggg-flow"),
    ])])
    FlowConsolidator(max_flows_per_feature=3).consolidate(fm)
    assert len(fm.features[0].flows) == 3
    # Shortest 3 kept.
    assert {fl.name for fl in fm.features[0].flows} == {
        "aa-flow", "bbb-flow", "cccc-flow",
    }


def test_consolidator_handles_empty_flows_list():
    fm = _Map(features=[_Feat(name="x", flows=[])])
    out = FlowConsolidator().consolidate(fm)
    assert out == (0, 0)


def test_consolidator_handles_no_features():
    fm = _Map(features=[])
    out = FlowConsolidator().consolidate(fm)
    assert out == (0, 0)


def test_consolidator_realistic_inbox_zero_pattern():
    """Reproduces the over-decomposition pattern observed on
    inbox-zero baseline: many configure-X-Y variants for one feature.
    """
    fm = _Map(features=[_Feat(name="ai-assistant", flows=[
        _f("configure-assistant-settings-flow"),
        _f("configure-assistant-flow"),
        _f("configure-assistant-channel-flow"),
        _f("update-assistant-flow"),
        _f("modify-assistant-flow"),
        _f("chat-with-assistant-flow"),
        _f("delete-chat-flow"),
        _f("rename-chat-flow"),
        _f("view-assistant-page-flow"),  # pure page → drop
    ])])
    n_before, n_after = FlowConsolidator(max_flows_per_feature=5).consolidate(fm)
    assert n_before == 9
    # configure*assistant collapses, update+modify*assistant collapses
    # with configure (different verb), chat-flow / delete-chat /
    # rename-chat are distinct, view-page dropped.
    assert n_after <= 5
    names = {fl.name for fl in fm.features[0].flows}
    assert "view-assistant-page-flow" not in names


def test_consolidator_default_has_no_cap():
    """Per memory/rule-no-magic-tuning, the consolidator default has
    NO numeric cap on flows-per-feature. Output count is whatever
    the (verb, noun) signature dedup yields — that IS the
    structural truth. The optional ``max_flows_per_feature`` knob
    stays for ad-hoc CLI workflows but is None by default.
    """
    assert FlowConsolidator().max_flows_per_feature is None


def test_consolidator_no_op_when_under_threshold():
    fm = _Map(features=[_Feat(name="x", flows=[
        _f("create-x-flow"),
        _f("delete-x-flow"),
    ])])
    n_before, n_after = FlowConsolidator().consolidate(fm)
    assert n_before == 2 and n_after == 2
