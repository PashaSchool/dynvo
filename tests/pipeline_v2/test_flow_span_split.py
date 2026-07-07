"""W4 — cross-PF flow-attribution split: conservation + labeling.

Gate tests (W4 brief): span-split conservation — no file lost, dual-LOC
channels respected; primary = entry-anchor PF; other PFs' files become
labeled secondary/shared attributions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import (
    Feature,
    Flow,
    FlowNode,
    FlowSymbolAttribution,
)
from faultline.pipeline_v2.flow_span_split import (
    flow_span_split_enabled,
    split_cross_pf_flow_attribution,
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _dev(name: str, pfid: str | None, paths: list[str],
         flows: list[Flow] | None = None) -> Feature:
    return Feature(
        name=name, display_name=name, paths=paths, authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="developer", product_feature_id=pfid,
        flows=flows or [],
        member_files=[
            {"path": p, "primary": True, "role": "anchor",
             "confidence": 0.9}
            for p in paths
        ],
    )


def _pf(name: str) -> Feature:
    return Feature(
        name=name, display_name=name.title(), paths=[], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="product",
    )


def _flow(entry: str, paths: list[str], **kw) -> Flow:
    base = dict(
        name="view-x-flow", paths=paths, authors=["a"], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, entry_point_file=entry,
    )
    base.update(kw)
    return Flow(**base)


def _world():
    """PF billing owns billing/*; PF docs owns docs/*; lib/* is unowned."""
    flow = _flow(
        "billing/page.tsx",
        ["billing/page.tsx", "billing/invoice.ts",
         "docs/render.ts", "lib/util.ts"],
        nodes=[
            FlowNode(id="billing/page.tsx#Page", kind="entry",
                     file="billing/page.tsx", symbol="Page", lines=(1, 20),
                     role="entry", confidence="high"),
            FlowNode(id="docs/render.ts#render", kind="function",
                     file="docs/render.ts", symbol="render", lines=(5, 30),
                     role="called", confidence="high"),
            FlowNode(id="docs/render.ts", kind="file", file="docs/render.ts",
                     symbol=None, lines=(1, 500), role="support",
                     confidence="medium"),
        ],
        flow_symbol_attributions=[
            FlowSymbolAttribution(file="billing/page.tsx", symbol="Page",
                                  line_start=1, line_end=20, role="entry"),
            FlowSymbolAttribution(file="docs/render.ts", symbol="render",
                                  line_start=5, line_end=30, role="called"),
            FlowSymbolAttribution(file="docs/render.ts", symbol="<file>",
                                  line_start=1, line_end=500, role="support"),
        ],
    )
    billing_dev = _dev("billing", "billing",
                       ["billing/page.tsx", "billing/invoice.ts"], [flow])
    docs_dev = _dev("docs", "docs", ["docs/render.ts"])
    lane_dev = _dev("lib", None, ["lib/util.ts"])
    return flow, [billing_dev, docs_dev, lane_dev], [_pf("billing"), _pf("docs")]


def test_split_moves_foreign_files_to_shared_ledger() -> None:
    flow, features, pfs = _world()
    tele = split_cross_pf_flow_attribution(features, pfs)
    assert tele["flows_split"] == 1
    assert tele["conservation_ok"] is True
    # Primary projection: home-PF files + unowned lane file stay.
    assert flow.paths == ["billing/page.tsx", "billing/invoice.ts",
                          "lib/util.ts"]
    # Conservation: the foreign file is re-labeled, never lost.
    assert [s.path for s in flow.shared_paths] == ["docs/render.ts"]
    s = flow.shared_paths[0]
    assert s.owner_product_feature == "docs"
    assert s.owner_display == "Docs"
    assert s.reason == "cross_pf_span"
    assert tele["files_moved"] == tele["shared_rows"] == 1


def test_split_conserves_total_file_set() -> None:
    flow, features, pfs = _world()
    before = set(flow.paths)
    split_cross_pf_flow_attribution(features, pfs)
    after = set(flow.paths) | {s.path for s in flow.shared_paths}
    assert after == before


def test_split_node_surface_labeled_sharing() -> None:
    flow, features, pfs = _world()
    tele = split_cross_pf_flow_attribution(features, pfs)
    by_id = {n.id: n for n in flow.nodes}
    # Whole-file guess on the foreign file is GONE from the node surface.
    assert "docs/render.ts" not in by_id
    assert tele["foreign_file_nodes_dropped"] == 1
    # The symbol-grain foreign node stays as labeled sharing.
    assert by_id["docs/render.ts#render"].role == "shared"
    assert tele["nodes_retagged_shared"] == 1
    # Phase-5 projections rebuilt without the 500-line guess.
    spans = {(r.path, r.start_line, r.end_line) for r in flow.line_ranges}
    assert ("docs/render.ts", 1, 500) not in spans
    assert ("docs/render.ts", 5, 30) in spans
    # Summary follows the tightened node surface.
    assert flow.summary is None or flow.summary.total_nodes == len(flow.nodes)


def test_split_attribution_roles() -> None:
    flow, features, pfs = _world()
    split_cross_pf_flow_attribution(features, pfs)
    roles = {(a.file, a.symbol): a.role
             for a in flow.flow_symbol_attributions}
    assert roles[("billing/page.tsx", "Page")] == "entry"
    assert roles[("docs/render.ts", "render")] == "shared"
    # Foreign whole-file support attribution dropped.
    assert ("docs/render.ts", "<file>") not in roles


def test_lane_dev_flows_untouched() -> None:
    flow = _flow("lib/a.ts", ["lib/a.ts", "billing/invoice.ts"])
    lane_dev = _dev("lib", None, ["lib/a.ts"], [flow])
    billing_dev = _dev("billing", "billing", ["billing/invoice.ts"])
    tele = split_cross_pf_flow_attribution(
        [lane_dev, billing_dev], [_pf("billing")])
    assert tele["flows_split"] == 0
    assert flow.paths == ["lib/a.ts", "billing/invoice.ts"]
    assert flow.shared_paths == []


def test_entry_file_never_moves() -> None:
    # Entry file owned by ANOTHER PF than the hosting dev: home follows
    # the ENTRY owner (entry-anchor PF is primary per the W4 brief), so
    # the dev's own other files become the labeled shared side.
    flow = _flow("docs/render.ts", ["docs/render.ts", "billing/invoice.ts"])
    billing_dev = _dev("billing", "billing", ["billing/invoice.ts"], [flow])
    docs_dev = _dev("docs", "docs", ["docs/render.ts"])
    split_cross_pf_flow_attribution(
        [billing_dev, docs_dev], [_pf("billing"), _pf("docs")])
    assert "docs/render.ts" in flow.paths
    assert [s.path for s in flow.shared_paths] == ["billing/invoice.ts"]


def test_dual_loc_channels_untouched() -> None:
    flow, features, pfs = _world()
    member_files_before = [list(f.member_files or []) for f in features]
    split_cross_pf_flow_attribution(features, pfs)
    assert [list(f.member_files or []) for f in features] \
        == member_files_before


def test_kill_switch(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("FAULTLINE_FLOW_SPAN_SPLIT", "0")
    assert flow_span_split_enabled() is False
    monkeypatch.setenv("FAULTLINE_FLOW_SPAN_SPLIT", "1")
    assert flow_span_split_enabled() is True


def test_last_span_never_dropped_only_retagged() -> None:
    """Evidence conservation (supabase I4 blast): a flow whose ONLY
    lined node is a foreign whole-file support keeps it as labeled
    sharing — a split must never zero a flow's LOC surface."""
    from faultline.models.types import FlowNode

    flow = _flow(
        "billing/page.tsx",
        ["billing/page.tsx", "docs/render.ts"],
        nodes=[FlowNode(id="docs/render.ts", kind="file",
                        file="docs/render.ts", symbol=None,
                        lines=(1, 300), role="support",
                        confidence="medium")],
    )
    billing_dev = _dev("billing", "billing", ["billing/page.tsx"], [flow])
    docs_dev = _dev("docs", "docs", ["docs/render.ts"])
    tele = split_cross_pf_flow_attribution(
        [billing_dev, docs_dev], [_pf("billing"), _pf("docs")])
    assert tele["foreign_file_nodes_dropped"] == 0
    (node,) = flow.nodes
    assert node.role == "shared"          # labeled, never lost
    assert node.lines == (1, 300)


def test_interior_and_shared_spans_never_vote_in_conservation() -> None:
    """W4 §4.6 + supabase smoke 2026-07-07: interior/shared node spans
    are labeled sharing — conservation span votes must ignore them, or
    UFs get dragged to component-owning PFs (I15 0.875 -> 0.25 class)."""
    from faultline.models.types import FlowNode
    from faultline.pipeline_v2.conservation import _flow_span_weights

    flow = _flow("billing/page.tsx", ["billing/page.tsx"], nodes=[
        FlowNode(id="billing/page.tsx#Page", kind="entry",
                 file="billing/page.tsx", symbol="Page", lines=(1, 30),
                 role="entry", confidence="high"),
        FlowNode(id="components/Big.tsx#Big", kind="function",
                 file="components/Big.tsx", symbol="Big", lines=(1, 500),
                 role="interior", confidence="high"),
        FlowNode(id="lib/db.ts#open", kind="function",
                 file="lib/db.ts", symbol="open", lines=(1, 400),
                 role="shared", confidence="high"),
    ])
    weights = _flow_span_weights(flow)
    assert weights == {"billing/page.tsx": 30}


def test_legacy_line_ranges_still_vote_without_nodes() -> None:
    from faultline.models.types import FlowLineRange
    from faultline.pipeline_v2.conservation import _flow_span_weights

    flow = _flow("a.ts", ["a.ts"], line_ranges=[
        FlowLineRange(path="a.ts", start_line=1, end_line=10),
    ])
    assert _flow_span_weights(flow) == {"a.ts": 10}
