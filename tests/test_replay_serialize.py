"""Round-trip unit tests for the replay-v2 tagged serializer.

One test per stage-input dataclass family (WS1 ship-gate c):
serialize → json dumps/loads → deserialize → same object.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import (
    Commit,
    Feature,
    FeatureFlowEdge,
    Flow,
    MemberFile,
    UserFlow,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature
from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows, FlowSpec
from faultline.replay.serialize import (
    SerializationError,
    from_jsonable,
    to_jsonable,
)


def roundtrip(obj):
    encoded = to_jsonable(obj)
    # Force a real disk-shaped round trip (str keys, no tuples).
    return from_jsonable(json.loads(json.dumps(encoded)))


def _commit(sha: str = "a" * 40) -> Commit:
    return Commit(
        sha=sha,
        message="fix: something",
        author="dev@example.com",
        date=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        files_changed=["a.py", "b.py"],
    )


# ── primitives / containers ─────────────────────────────────────────────


def test_primitives_and_containers():
    obj = {
        "s": "x", "i": 3, "f": 1.5, "b": True, "n": None,
        "list": [1, "two", None],
        "nested": {"k": [{"deep": (1, 2)}]},
    }
    out = roundtrip(obj)
    assert out["nested"]["k"][0]["deep"] == (1, 2)
    assert out["s"] == "x" and out["list"] == [1, "two", None]


def test_sets_sorted_deterministically():
    a = to_jsonable({"paths": {"b", "a", "c"}})
    b = to_jsonable({"paths": {"c", "a", "b"}})
    assert a == b
    assert roundtrip({"x": frozenset({1, 2})})["x"] == frozenset({1, 2})


def test_path_and_datetime():
    out = roundtrip({"p": Path("/tmp/x"), "d": datetime(2026, 1, 1)})
    assert out["p"] == Path("/tmp/x")
    assert out["d"] == datetime(2026, 1, 1)


def test_non_str_dict_keys():
    out = roundtrip({("a", 1): "v"})
    assert out == {("a", 1): "v"}


def test_dict_with_reserved_type_key():
    out = roundtrip({"__type__": "not-a-tag", "x": 1})
    assert out == {"__type__": "not-a-tag", "x": 1}


def test_unencodable_object_raises():
    with pytest.raises(SerializationError):
        to_jsonable(object())


def test_decoder_allowlist():
    with pytest.raises(SerializationError):
        from_jsonable({"__type__": "dataclass:os:path", "value": {}})


# ── pydantic family (Feature / Flow / UserFlow / Commit / edges) ────────


def test_commit_roundtrip():
    c = _commit()
    out = roundtrip(c)
    assert out == c and isinstance(out, Commit)


def test_feature_with_flows_pydantic_roundtrip():
    metric_defaults = dict(
        authors=["dev@example.com"],
        total_commits=3,
        bug_fixes=1,
        bug_fix_ratio=0.33,
        last_modified=datetime(2026, 1, 2, tzinfo=timezone.utc),
        health_score=90.0,
    )
    flow = Flow(
        name="create-item-flow",
        description="creates an item",
        paths=["app/items/new/page.tsx"],
        entry_point_file="app/items/new/page.tsx",
        **metric_defaults,
    )
    feat = Feature(
        name="items",
        description="item management",
        paths=["app/items/new/page.tsx"],
        flows=[flow],
        member_files=[
            MemberFile(path="lib/items.ts", role="closure", confidence=0.8),
        ],
        **metric_defaults,
    )
    out = roundtrip(feat)
    assert isinstance(out, Feature)
    assert out.model_dump() == feat.model_dump()


def test_userflow_and_edge_roundtrip():
    uf = UserFlow(id="uf-1", name="manage-items", intent="manage",
                  resource="items")
    edge = FeatureFlowEdge(feature="items", flow_id="fl-1", type="primary")
    out = roundtrip({"user_flows": [uf], "edges": [edge]})
    assert out["user_flows"][0].model_dump() == uf.model_dump()
    assert out["edges"][0].model_dump() == edge.model_dump()


# ── dataclass families ──────────────────────────────────────────────────


def test_scan_context_roundtrip_excludes_cache_backend():
    ctx = ScanContext(
        repo_path=Path("/repo"),
        stack="next-app-router",
        monorepo=True,
        workspaces=[
            Workspace(name="web", path="apps/web",
                      package_json={"name": "web"}, stack="next-app-router",
                      files=["apps/web/a.tsx"]),
        ],
        tracked_files=["apps/web/a.tsx"],
        commits=[_commit()],
        stack_signals=["next.config.mjs"],
        workspace_manager="pnpm",
        run_id="orig-run",
        run_dir=Path("/tmp/orig-run"),
        audited_stack="next-app-router",
        secondary_stacks=("fastapi",),
        extractor_hints=("route",),
        auditor_confidence=0.9,
        repo_shape="turborepo-monorepo",
        shape_confidence=0.8,
        shape_rationale="workspaces",
        cache_backend=object(),  # live handle — must be DROPPED, not encoded
        subpath=None,
    )
    out = roundtrip(ctx)
    assert isinstance(out, ScanContext)
    assert out.cache_backend is None
    assert out.repo_path == ctx.repo_path
    assert out.workspaces[0].package_json == {"name": "web"}
    assert out.commits[0] == ctx.commits[0]
    assert out.secondary_stacks == ("fastapi",)
    assert out.repo_shape == "turborepo-monorepo"


def test_anchor_candidate_roundtrip():
    cand = AnchorCandidate(
        name="billing",
        paths=("app/billing/page.tsx",),
        source="route",
        confidence_self=0.9,
        display_name="Billing",
        rationale="route dir",
        routes=(("/billing", "GET", "app/billing/page.tsx"),),
    )
    out = roundtrip({"route": [cand]})
    assert out["route"][0] == cand


def test_developer_feature_roundtrip():
    df = DeveloperFeature(
        name="billing",
        paths=("app/billing/page.tsx", "lib/billing.ts"),
        sources=["route", "package"],
        confidence="high",
        display_name="Billing",
        rationale="merged",
        source_confidences={"route": 0.9, "package": 0.8},
        member_files=[
            MemberFile(path="lib/billing.ts", role="closure", confidence=0.8),
        ],
        merged_from=["billing-api"],
    )
    out = roundtrip(df)
    assert out == df and isinstance(out, DeveloperFeature)


def test_feature_with_flows_dataclass_roundtrip():
    df = DeveloperFeature(
        name="billing", paths=("a.ts",), sources=["route"], confidence="medium",
    )
    fwf = FeatureWithFlows(
        feature=df,
        flows=[
            FlowSpec(
                name="pay-invoice-flow",
                description="pays",
                entry_point_file="a.ts",
                entry_point_line=10,
                symbol_names=["payInvoice"],
                reach_paths=("a.ts", "b.ts"),
                depth_reached=1,
            ),
        ],
        rationale="llm",
    )
    out = roundtrip(fwf)
    assert out == fwf and isinstance(out, FeatureWithFlows)
