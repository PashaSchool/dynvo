"""Tests for the flow-attribution-critique aggregator (Sprint 8h)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from faultline.aggregators.flow_attribution_critique import (
    _parse_verdicts,
    apply_verdicts,
    critique_flow_attribution,
)


# ── helpers ──────────────────────────────────────────────────────────


def _flow(name, paths=None):
    from faultline.models.types import Flow
    return Flow(
        name=name, paths=paths or [], authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=99.0,
    )


def _feat(name, flows=None, display=None):
    from faultline.models.types import Feature
    return Feature(
        name=name, paths=["x.ts"], authors=[], total_commits=10,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=99.0, flows=flows or [], display_name=display,
    )


def _fm(features):
    from faultline.models.types import FeatureMap
    return FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=datetime.now(tz=timezone.utc),
        total_commits=0, date_range_days=365,
        features=features,
    )


class _FakeLlm:
    name = "fake"
    def __init__(self, responses_by_feature):
        self._responses = responses_by_feature
    def complete(self, *, system, user, max_tokens, tools=None):
        from faultline.signals import LlmResponse
        # parse the user payload to discover feature
        payload = json.loads(user.split("\n\n", 1)[1])
        feat = payload["current_feature"]
        text = self._responses.get(feat, '{"verdicts":[]}')
        return LlmResponse(text=text, input_tokens=10, output_tokens=10, stop_reason="end_turn")


# ── _parse_verdicts ──────────────────────────────────────────────────


def test_parse_handles_clean_json():
    raw = json.dumps({"verdicts": [
        {"flow": "f1", "verdict": "keep", "suggested_feature": None, "reason": "ok"},
        {"flow": "f2", "verdict": "move", "suggested_feature": "Billing", "reason": "fits billing"},
    ]})
    out = _parse_verdicts(raw)
    assert len(out) == 2
    assert out[0]["verdict"] == "keep"
    assert out[1]["suggested_feature"] == "Billing"


def test_parse_strips_markdown_fences():
    raw = "```json\n" + json.dumps({"verdicts":[
        {"flow": "f1", "verdict": "new", "suggested_feature": "Background Jobs", "reason": "queue"},
    ]}) + "\n```"
    out = _parse_verdicts(raw)
    assert out[0]["verdict"] == "new"


def test_parse_drops_invalid_entries():
    raw = json.dumps({"verdicts": [
        {"flow": "f1", "verdict": "wat", "suggested_feature": None},
        {"flow": "f2", "verdict": "keep", "suggested_feature": None},
        "not-an-object",
    ]})
    out = _parse_verdicts(raw)
    assert len(out) == 1
    assert out[0]["flow"] == "f2"


def test_parse_handles_garbage():
    assert _parse_verdicts("not json at all") == []


# ── apply_verdicts: keep / move / new ─────────────────────────────────


def test_keep_is_no_op():
    f1 = _feat("billing", flows=[_flow("create-invoice")])
    fm = _fm([f1])
    stats = apply_verdicts(fm, {"billing": [
        {"flow": "create-invoice", "verdict": "keep", "suggested_feature": None, "reason": ""},
    ]})
    assert stats.kept == 1
    assert stats.moved == 0
    assert [fl.name for fl in f1.flows] == ["create-invoice"]


def test_move_relocates_to_existing_feature():
    fl = _flow("send-receipt")
    src = _feat("billing", flows=[fl])
    dst = _feat("notifications", flows=[])
    fm = _fm([src, dst])
    stats = apply_verdicts(fm, {"billing": [
        {"flow": "send-receipt", "verdict": "move", "suggested_feature": "Notifications", "reason": ""},
    ]})
    assert stats.moved == 1
    assert src.flows == []
    assert [fl.name for fl in dst.flows] == ["send-receipt"]


def test_move_falls_back_to_keep_when_target_unknown():
    fl = _flow("send-receipt")
    src = _feat("billing", flows=[fl])
    fm = _fm([src])
    stats = apply_verdicts(fm, {"billing": [
        {"flow": "send-receipt", "verdict": "move", "suggested_feature": "Mars Colonies", "reason": ""},
    ]})
    assert stats.moved == 0
    assert [fl.name for fl in src.flows] == ["send-receipt"]


def test_new_creates_feature_when_support_threshold_met():
    flows = [_flow(f"job-{i}") for i in range(3)]
    src = _feat("translations", flows=flows)
    fm = _fm([src])
    stats = apply_verdicts(fm, {"translations": [
        {"flow": f"job-{i}", "verdict": "new", "suggested_feature": "Background Jobs", "reason": ""}
        for i in range(3)
    ]}, new_feature_min_support=3)
    assert stats.new_features_added == 1
    assert src.flows == []
    new_feat = next(f for f in fm.features if f.discovery_method == "flow-critique")
    assert new_feat.display_name == "Background Jobs"
    assert {fl.name for fl in new_feat.flows} == {"job-0", "job-1", "job-2"}


def test_new_dropped_below_threshold_redistributes():
    flows = [_flow("billing-helper")]
    src = _feat("translations", flows=flows)
    other = _feat("billing", flows=[])
    fm = _fm([src, other])
    stats = apply_verdicts(fm, {"translations": [
        {"flow": "billing-helper", "verdict": "new", "suggested_feature": "Tax Reports", "reason": ""},
    ]}, new_feature_min_support=3)
    assert stats.new_features_added == 0
    assert stats.new_feature_proposals_dropped == 1
    # Flow falls back to billing via token-overlap restoration
    assert any(fl.name == "billing-helper" for fl in other.flows)


def test_new_with_existing_target_treated_as_move():
    fl = _flow("audit-record")
    src = _feat("documents", flows=[fl])
    dst = _feat("compliance", flows=[])
    fm = _fm([src, dst])
    stats = apply_verdicts(fm, {"documents": [
        {"flow": "audit-record", "verdict": "new", "suggested_feature": "Compliance", "reason": ""},
    ]})
    assert stats.moved == 1
    assert stats.new_features_added == 0
    assert [fl.name for fl in dst.flows] == ["audit-record"]


# ── end-to-end orchestrator ──────────────────────────────────────────


def test_critique_orchestrator_routes_each_feature():
    fl = _flow("queue-job")
    src = _feat("translations", flows=[fl])
    dst = _feat("background-jobs", flows=[])
    fm = _fm([src, dst])
    fake = _FakeLlm({
        "translations": json.dumps({"verdicts": [
            {"flow": "queue-job", "verdict": "move", "suggested_feature": "background-jobs", "reason": "queue"},
        ]}),
    })
    fm, stats = critique_flow_attribution(fm, llm=fake, concurrency=2)
    assert stats.moved == 1
    # Input stays untouched (pure-function contract)
    assert [fl.name for fl in src.flows] == ["queue-job"]
    # Output reflects the move
    bn = {f.name: f for f in fm.features}
    assert bn["translations"].flows == []
    assert [fl.name for fl in bn["background-jobs"].flows] == ["queue-job"]


def test_critique_skips_features_with_no_flows():
    a = _feat("empty", flows=[])
    b = _feat("billing", flows=[_flow("invoice")])
    fm = _fm([a, b])
    fake = _FakeLlm({
        "billing": json.dumps({"verdicts": [
            {"flow": "invoice", "verdict": "keep", "suggested_feature": None, "reason": ""},
        ]}),
    })
    fm, stats = critique_flow_attribution(fm, llm=fake, concurrency=2)
    assert stats.kept == 1
