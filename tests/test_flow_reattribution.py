"""Tests for the flow-reattribution aggregator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from faultline.aggregators.flow_reattribution import (
    FlowReattribution,
    _score,
    _tokens,
)


def _flow(name):
    from faultline.models.types import Flow
    return Flow(
        name=name, paths=[], authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=99.0,
    )


def _feat(name, flows=None):
    from faultline.models.types import Feature
    return Feature(
        name=name, paths=["x.ts"], authors=[],
        total_commits=10, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=99.0, flows=flows or [],
    )


def _fm(features):
    from faultline.models.types import FeatureMap
    return FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=datetime.now(tz=timezone.utc),
        total_commits=0, date_range_days=365,
        features=features,
    )


# ── _tokens ───────────────────────────────────────────────────────────


def test_tokens_drops_flow_suffix():
    assert "flow" not in _tokens("manage-billing-flow")


def test_tokens_splits_camel_and_kebab():
    assert _tokens("EnterpriseBilling") == {"enterprise", "billing"}
    assert _tokens("enterprise-billing") == {"enterprise", "billing"}
    assert _tokens("ee/enterprise-billing") == {"enterprise", "billing"}


def test_tokens_drops_short_words():
    assert "ee" not in _tokens("ee/enterprise-billing")


# ── _score ────────────────────────────────────────────────────────────


def test_score_zero_when_no_overlap():
    assert _score("organisation-management", {"enterprise", "billing"}) == 0


def test_score_counts_each_overlapping_token():
    assert _score("ee/enterprise-billing", {"enterprise", "billing"}) == 2


def test_score_partial_match():
    assert _score("billing", {"enterprise", "billing"}) == 1


# ── FlowReattribution.reattribute ─────────────────────────────────────


def test_moves_flow_to_strictly_better_feature():
    """The papermark/documenso bug — enterprise-billing-flow on
    organisation-management should move to ee/enterprise-billing.
    """
    f_org = _feat("organisation-management", flows=[
        _flow("invite-org-member"),         # legitimately org-management
        _flow("enterprise-billing-flow"),   # MIS-attributed
    ])
    f_billing = _feat("ee/enterprise-billing", flows=[
        _flow("manage-billing-portal"),
    ])
    fm = _fm([f_org, f_billing])
    n = FlowReattribution().reattribute(fm)
    assert n == 1
    org_flows = {fl.name for fl in f_org.flows}
    billing_flows = {fl.name for fl in f_billing.flows}
    assert "enterprise-billing-flow" not in org_flows
    assert "enterprise-billing-flow" in billing_flows
    assert "invite-org-member" in org_flows  # untouched
    assert "manage-billing-portal" in billing_flows  # untouched


def test_keeps_flow_when_current_owner_wins():
    f_billing = _feat("ee/enterprise-billing", flows=[
        _flow("manage-enterprise-billing-portal"),
    ])
    f_org = _feat("organisation-management", flows=[])
    fm = _fm([f_billing, f_org])
    n = FlowReattribution().reattribute(fm)
    assert n == 0
    assert len(f_billing.flows) == 1


def test_keeps_flow_when_no_alt_better_than_current():
    """Tie does NOT trigger move — we keep existing placement."""
    f_a = _feat("billing", flows=[_flow("billing-action")])
    f_b = _feat("invoices-billing", flows=[])
    fm = _fm([f_a, f_b])
    n = FlowReattribution().reattribute(fm)
    # `billing-action` tokens = {billing, action}.
    # billing scores 1, invoices-billing scores 1 → tie → keep.
    assert n == 0
    assert len(f_a.flows) == 1


def test_skips_flows_with_no_meaningful_tokens():
    """Flow named purely from stop words / short tokens shouldn't move."""
    f_a = _feat("settings", flows=[_flow("flow-the-flow")])
    f_b = _feat("billing", flows=[])
    fm = _fm([f_a, f_b])
    n = FlowReattribution().reattribute(fm)
    assert n == 0


def test_no_features_or_single_feature_short_circuits():
    fm = _fm([_feat("only", flows=[_flow("create-thing-flow")])])
    assert FlowReattribution().reattribute(fm) == 0


def test_handles_multiple_moves_in_one_pass():
    f_settings = _feat("settings", flows=[
        _flow("create-billing-invoice"),
        _flow("send-email-receipt"),
    ])
    f_billing = _feat("billing", flows=[])
    f_email = _feat("email", flows=[])
    fm = _fm([f_settings, f_billing, f_email])
    n = FlowReattribution().reattribute(fm)
    assert n == 2
    assert {fl.name for fl in f_billing.flows} == {"create-billing-invoice"}
    assert {fl.name for fl in f_email.flows} == {"send-email-receipt"}
    assert f_settings.flows == []
