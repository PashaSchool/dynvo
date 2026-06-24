"""Tests for Stage 5.5 — bipartite feature ↔ flow store + blast-radius.

Covers the four cases called out in the Sprint B1 spec plus invariants
the orchestrator relies on (one primary edge per flow, secondary
features never include primary, telemetry sums add up).

Pure unit tests — no LLM, no git, no filesystem.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, Flow
from faultline.pipeline_v2.stage_5_5_bipartite import (
    Stage5_5Result,
    stage_5_5_bipartite,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _feat(name: str, paths: list[str], flows: list[Flow] | None = None) -> Feature:
    """Minimal :class:`Feature` factory — only the fields the stage reads."""
    return Feature(
        name=name,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=flows or [],
        layer="developer",
    )


def _flow(name: str, paths: list[str]) -> Flow:
    """Minimal :class:`Flow` factory — only what the stage reads."""
    return Flow(
        name=name,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
    )


# ── Case 1 — flow contained entirely within its primary ──────────────────


def test_flow_only_in_primary_has_zero_secondaries():
    """A flow whose paths live entirely under its primary feature
    must emit 0 secondary features and 0 shared_with_flows."""
    billing = _feat(
        "billing",
        paths=["app/billing/route.ts", "app/billing/charge.ts"],
        flows=[_flow("charge-customer-flow", ["app/billing/route.ts"])],
    )

    result = stage_5_5_bipartite([billing])

    assert isinstance(result, Stage5_5Result)
    assert len(result.flows) == 1
    flow = result.flows[0]
    assert flow.primary_feature == "billing"
    assert flow.id == "billing::charge-customer-flow"
    assert flow.secondary_features == []
    assert flow.shared_with_flows_count == 0
    assert flow.shared_with_features_count == 0
    assert flow.cross_cutting is False

    # Exactly one primary edge, no secondaries.
    assert [e.type for e in result.edges] == ["primary"]
    assert result.edges[0].feature == "billing"
    assert result.edges[0].flow_id == "billing::charge-customer-flow"
    assert result.edges[0].reason is None


# ── Case 2 — flow spans two features → one secondary ─────────────────────


def test_flow_spanning_two_features_emits_one_secondary():
    """A flow whose paths span two features must emit one secondary
    edge with reason='path-overlap'."""
    billing = _feat(
        "billing",
        paths=["app/billing/charge.ts"],
        flows=[
            _flow(
                "charge-customer-flow",
                paths=["app/billing/charge.ts", "lib/auth/check.ts"],
            ),
        ],
    )
    auth = _feat("auth", paths=["lib/auth/check.ts"])

    result = stage_5_5_bipartite([billing, auth])

    flow = result.flows[0]
    assert flow.primary_feature == "billing"
    assert flow.secondary_features == ["auth"]
    assert flow.shared_with_features_count == 1
    assert flow.cross_cutting is True

    # One primary edge + one secondary edge.
    types = sorted(e.type for e in result.edges)
    assert types == ["primary", "secondary"]
    secondary = next(e for e in result.edges if e.type == "secondary")
    assert secondary.feature == "auth"
    assert secondary.reason == "path-overlap"
    assert secondary.flow_id == "billing::charge-customer-flow"


# ── Case 3 — two flows sharing one path → blast-radius = 1 each ─────────


def test_two_flows_sharing_path_get_shared_count_one():
    """Two flows that share a single path must each report
    shared_with_flows_count == 1."""
    shared_path = "lib/auth/check.ts"
    billing = _feat(
        "billing",
        paths=["app/billing/charge.ts", shared_path],
        flows=[_flow("charge-customer-flow", ["app/billing/charge.ts", shared_path])],
    )
    settings = _feat(
        "settings",
        paths=["app/settings/page.ts", shared_path],
        flows=[_flow("update-settings-flow", ["app/settings/page.ts", shared_path])],
    )

    result = stage_5_5_bipartite([billing, settings])

    assert len(result.flows) == 2
    counts = {f.id: f.shared_with_flows_count for f in result.flows}
    assert counts["billing::charge-customer-flow"] == 1
    assert counts["settings::update-settings-flow"] == 1

    # max_shared_with_flows telemetry must agree.
    assert result.telemetry["max_shared_with_flows"] == 1


# ── Case 4 — shared_attributions DO create secondary edges ──────────────


def test_shared_attributions_create_secondary_edges():
    """Per spec: a flow whose primary feature owns ALL its paths plus
    another feature reaching in via shared_attributions still counts
    as cross-cutting through that other feature."""
    from faultline.models.types import SymbolAttribution

    billing = _feat(
        "billing",
        paths=["app/billing/charge.ts"],
        flows=[_flow("charge-customer-flow", ["app/billing/charge.ts"])],
    )
    # ``auth`` reaches into billing's path via shared_attributions
    # (symbol-scoped reach into a file it doesn't own). For blast-
    # radius purposes that's a real cross-cutting attachment.
    auth = _feat("auth", paths=["lib/auth/check.ts"])
    auth.shared_attributions = [
        SymbolAttribution(
            file_path="app/billing/charge.ts",
            symbols=["requireUser"],
            line_ranges=[(1, 10)],
            attributed_lines=10,
            total_file_lines=50,
        ),
    ]

    result = stage_5_5_bipartite([billing, auth])

    flow = result.flows[0]
    assert flow.secondary_features == ["auth"]
    assert flow.cross_cutting is True
    # Edge has the expected reason.
    sec = next(e for e in result.edges if e.type == "secondary")
    assert sec.feature == "auth"
    assert sec.reason == "path-overlap"


# ── Invariants ───────────────────────────────────────────────────────────


def test_invariant_one_primary_edge_per_flow():
    """Every flow must contribute exactly one primary edge."""
    a = _feat(
        "a",
        paths=["a/1.ts", "a/2.ts"],
        flows=[
            _flow("first-flow", ["a/1.ts"]),
            _flow("second-flow", ["a/2.ts"]),
        ],
    )
    b = _feat("b", paths=["b/1.ts"], flows=[_flow("third-flow", ["b/1.ts"])])

    result = stage_5_5_bipartite([a, b])

    primary_edges = [e for e in result.edges if e.type == "primary"]
    assert len(primary_edges) == len(result.flows) == 3
    # IDs are unique.
    assert len({e.flow_id for e in primary_edges}) == 3


def test_invariant_secondary_never_includes_primary():
    """A flow's primary feature must never appear in its secondaries
    even when its primary's paths overlap its own paths."""
    billing = _feat(
        "billing",
        paths=["app/billing/a.ts", "app/billing/b.ts"],
        flows=[_flow("flow-a", ["app/billing/a.ts", "app/billing/b.ts"])],
    )
    result = stage_5_5_bipartite([billing])
    assert "billing" not in result.flows[0].secondary_features


def test_invariant_telemetry_sums():
    """``bipartite_edges_total == primary + secondary`` and primary ==
    flows_total."""
    billing = _feat(
        "billing",
        paths=["app/billing/charge.ts"],
        flows=[
            _flow(
                "charge-customer-flow",
                ["app/billing/charge.ts", "lib/auth/check.ts", "lib/log/info.ts"],
            ),
        ],
    )
    auth = _feat("auth", paths=["lib/auth/check.ts"])
    logging = _feat("logging", paths=["lib/log/info.ts"])

    result = stage_5_5_bipartite([billing, auth, logging])
    t = result.telemetry
    assert t["bipartite_edges_total"] == t["bipartite_edges_primary"] + t["bipartite_edges_secondary"]
    assert t["bipartite_edges_primary"] == t["flows_total"] == 1
    assert t["bipartite_edges_secondary"] == 2
    assert t["cross_cutting_flows_count"] == 1
    assert t["max_shared_with_features"] == 2


def test_invariant_empty_features_yields_empty_result():
    """Defensive — no features means no edges, no flows, all-zero telemetry."""
    result = stage_5_5_bipartite([])
    assert result.flows == []
    assert result.edges == []
    assert result.telemetry["flows_total"] == 0
    assert result.telemetry["bipartite_edges_total"] == 0
    assert result.telemetry["max_shared_with_flows"] == 0


def test_flow_with_no_paths_is_isolated():
    """A flow with empty paths can't be cross-cutting and reports 0 shared."""
    billing = _feat(
        "billing",
        paths=["app/billing/charge.ts"],
        flows=[_flow("name-only-flow", paths=[])],
    )
    result = stage_5_5_bipartite([billing])
    flow = result.flows[0]
    assert flow.secondary_features == []
    assert flow.shared_with_flows_count == 0
    assert flow.cross_cutting is False


# ── Stable ordering ──────────────────────────────────────────────────────


def test_flow_top_level_list_sorted_by_id():
    """Top-level flows[] is sorted by id for stable diffing across rescans."""
    a = _feat(
        "zeta",
        paths=["zeta/1.ts"],
        flows=[_flow("z-flow", ["zeta/1.ts"])],
    )
    b = _feat(
        "alpha",
        paths=["alpha/1.ts"],
        flows=[_flow("a-flow", ["alpha/1.ts"])],
    )
    result = stage_5_5_bipartite([a, b])
    assert [f.id for f in result.flows] == ["alpha::a-flow", "zeta::z-flow"]


# ── Step 0 — dedup provably-identical flows ─────────────────────────────────


def _flow_at(name: str, paths: list[str], entry_file: str, entry_line: int) -> Flow:
    f = _flow(name, paths)
    f.entry_point_file = entry_file
    f.entry_point_line = entry_line
    return f


def test_dedup_identical_flows_within_feature() -> None:
    """Flows with identical (name, entry_point_file, entry_point_line) inside one
    feature collapse to ONE before id/uuid stamping (the infisical route-flow
    duplication: feature-merge stages concatenate the same flow N times)."""
    routes = _feat(
        "server-v1-routes",
        paths=["backend/routes/index.ts"],
        flows=[
            _flow_at("authenticate-integration-flow", ["a.ts"], "backend/routes/auth.ts", 13),
            _flow_at("authenticate-integration-flow", ["a.ts"], "backend/routes/auth.ts", 13),  # dup
            _flow_at("authenticate-integration-flow", ["a.ts"], "backend/routes/auth.ts", 13),  # dup
            _flow_at("register-project-flow", ["b.ts"], "backend/routes/project.ts", 86),
        ],
    )
    result = stage_5_5_bipartite([routes])
    feat = result.features[0]
    names = [f.name for f in feat.flows]
    assert names == ["authenticate-integration-flow", "register-project-flow"]
    assert len(result.flows) == 2
    assert result.telemetry["duplicate_flows_dropped"] == 2
    # the surviving flow got exactly one id
    assert len({f.id for f in result.flows}) == 2


def test_dedup_keeps_same_entry_different_name() -> None:
    """Two DIFFERENTLY-named flows sharing an entry point are NOT duplicates."""
    feat = _feat(
        "f",
        paths=["x.ts"],
        flows=[
            _flow_at("view-x-flow", ["x.ts"], "x.ts", 1),
            _flow_at("edit-x-flow", ["x.ts"], "x.ts", 1),  # same entry, different name
        ],
    )
    result = stage_5_5_bipartite([feat])
    assert len(result.features[0].flows) == 2
    assert result.telemetry["duplicate_flows_dropped"] == 0


def test_dedup_entryless_flows_collapse_only_by_name() -> None:
    """Entry-less flows collapse only against a same-name entry-less flow."""
    feat = _feat(
        "f",
        paths=["x.ts"],
        flows=[_flow("a-flow", ["x.ts"]), _flow("a-flow", ["x.ts"]), _flow("b-flow", ["x.ts"])],
    )
    result = stage_5_5_bipartite([feat])
    assert {f.name for f in result.features[0].flows} == {"a-flow", "b-flow"}
    assert result.telemetry["duplicate_flows_dropped"] == 1


# ── Step 0.5 — cross-feature duplicate collapse (the dup_flow_rate bug) ─────


def _flow_lr(
    name: str,
    paths: list[str],
    entry_file: str,
    entry_line: int,
    ranges: list[tuple[str, int, int]],
) -> Flow:
    """Flow factory carrying an entry point AND explicit line_ranges."""
    from faultline.models.types import FlowLineRange

    f = _flow(name, paths)
    f.entry_point_file = entry_file
    f.entry_point_line = entry_line
    f.line_ranges = [
        FlowLineRange(path=p, start_line=s, end_line=e) for (p, s, e) in ranges
    ]
    return f


def _feat_anchored(name: str, paths: list[str], flows: list[Flow], anchor_file: str) -> Feature:
    """Feature whose ``member_files`` anchors ``anchor_file`` (confidence 1.0)."""
    from faultline.models.types import MemberFile

    feat = _feat(name, paths=paths, flows=flows)
    feat.member_files = [
        MemberFile(path=anchor_file, role="anchor", confidence=1.0, primary=True),
    ]
    return feat


def test_cross_feature_identical_flows_collapse_to_one() -> None:
    """The hub-file bug: ONE physical flow at the same entry+line_ranges is
    attributed once to EACH of N features that contain the entry file. They
    must collapse to a SINGLE top-level flow, the losing features folded into
    secondary_features — not N duplicate rows."""
    entry = "demo_api/main.go"
    ranges = [(entry, 61, 70)]
    feats = []
    for owner in ["abrupt-shutdown", "health", "v2-accounts"]:
        feats.append(
            _feat_anchored(
                owner,
                paths=[entry],
                flows=[_flow_lr("create-account-flow", [entry], entry, 61, ranges)],
                anchor_file=entry,
            ),
        )

    result = stage_5_5_bipartite(feats)

    # Exactly one surviving create-account-flow in the top-level projection.
    cab = [f for f in result.flows if f.name == "create-account-flow"]
    assert len(cab) == 1
    survivor = cab[0]
    # Deterministic primary = lexicographically smallest among equal anchors.
    assert survivor.primary_feature == "abrupt-shutdown"
    # The other two owners are preserved as secondary attachments.
    assert survivor.secondary_features == ["health", "v2-accounts"]
    assert survivor.cross_cutting is True
    # Containment invariant: every flow appears exactly once across features.
    total_contained = sum(len(f.flows) for f in result.features)
    assert total_contained == len(result.flows) == 1
    # Exactly one primary edge for the survivor + secondary edges.
    primary_edges = [e for e in result.edges if e.type == "primary"]
    assert len(primary_edges) == 1
    assert result.telemetry["duplicate_flows_dropped_cross_feature"] == 2
    assert result.telemetry["duplicate_flows_dropped_within_feature"] == 0


def test_cross_feature_distinct_entry_line_survive_with_line_ranges() -> None:
    """Same NAME, same entry FILE, but DIFFERENT entry lines = genuinely
    distinct flows (e.g. two tutorials in one module). Both survive the
    byte-identical collapse (nothing MERGED) and are then RENAMED to unique
    names by the Step-0.6 naming-collision disambiguation (the dup_flow_rate
    kill) — distinct flows are never left sharing a generic name."""
    entry = "demo_api/main.go"
    a = _feat_anchored(
        "feat-a",
        paths=[entry],
        flows=[_flow_lr("create-account-flow", [entry], entry, 49, [(entry, 49, 60)])],
        anchor_file=entry,
    )
    b = _feat_anchored(
        "feat-b",
        paths=[entry],
        flows=[_flow_lr("create-account-flow", [entry], entry, 61, [(entry, 61, 70)])],
        anchor_file=entry,
    )
    result = stage_5_5_bipartite([a, b])
    # Both distinct flows preserved (count unchanged, nothing merged) …
    assert len(result.flows) == 2
    assert result.telemetry["duplicate_flows_dropped_cross_feature"] == 0
    # … but disambiguated to distinct names, all still kebab + verb-led + -flow.
    names = sorted(f.name for f in result.flows)
    assert len(set(names)) == 2
    for n in names:
        assert n.startswith("create-") and n.endswith("-flow")


def test_cross_feature_distinct_entry_line_survive() -> None:
    """REAL runtime discriminator: same name + same entry FILE but DIFFERENT
    entry_point_line are distinct and BOTH survive (the unkey
    create-account-flow @line49 vs @line61 shape). This is the conservative
    guard that actually fires at the Stage-5.5 call site, where line_ranges
    is still empty. They survive the collapse (nothing merged) and are renamed
    to distinct names by Step-0.6 disambiguation."""
    entry = "demo_api/main.go"
    a = _feat_anchored(
        "feat-a", paths=[entry],
        flows=[_flow_lr("create-account-flow", [entry], entry, 49, [])],
        anchor_file=entry,
    )
    b = _feat_anchored(
        "feat-b", paths=[entry],
        flows=[_flow_lr("create-account-flow", [entry], entry, 61, [])],
        anchor_file=entry,
    )
    result = stage_5_5_bipartite([a, b])
    assert len(result.flows) == 2  # both distinct flows preserved
    assert result.telemetry["duplicate_flows_dropped_cross_feature"] == 0
    names = sorted(f.name for f in result.flows)
    assert len(set(names)) == 2  # disambiguated to unique names
    for n in names:
        assert n.startswith("create-") and n.endswith("-flow")


def test_cross_feature_distinct_line_ranges_survive() -> None:
    """FORWARD-SAFETY: if line_ranges were ever populated before Stage 5.5
    (today they are NOT — the LOC expander runs in phase_finalize, later),
    differing spans would still produce distinct keys. Guards against a future
    stage reorder silently over-collapsing; inert at the current call site.
    Both flows survive the collapse and are renamed to distinct names by
    Step-0.6 disambiguation."""
    entry = "router.ts"
    a = _feat_anchored(
        "feat-a",
        paths=[entry],
        flows=[_flow_lr("list-flow", [entry], entry, 10, [(entry, 10, 20)])],
        anchor_file=entry,
    )
    b = _feat_anchored(
        "feat-b",
        paths=[entry],
        flows=[_flow_lr("list-flow", [entry], entry, 10, [(entry, 10, 40)])],
        anchor_file=entry,
    )
    result = stage_5_5_bipartite([a, b])
    assert len(result.flows) == 2  # both distinct flows preserved (not merged)
    assert result.telemetry["duplicate_flows_dropped_cross_feature"] == 0
    names = sorted(f.name for f in result.flows)
    assert len(set(names)) == 2  # disambiguated to unique names
    for n in names:
        assert n.startswith("list-") and n.endswith("-flow")


def test_cross_feature_anchor_owner_beats_non_anchor() -> None:
    """When the entry file is ANCHORED by one feature and merely reached
    (closure / no member_files) by another, the anchor owner wins the
    primary regardless of feature-name ordering."""
    entry = "backend/router.ts"
    ranges = [(entry, 21, 30)]
    # ``zzz-owner`` anchors the entry file; ``aaa-reacher`` has no member_files
    # (a Stage-4 residual). Lexicographic order would pick ``aaa-reacher`` but
    # the anchor rule must elect ``zzz-owner``.
    anchored = _feat_anchored(
        "zzz-owner",
        paths=[entry],
        flows=[_flow_lr("rotate-secret-flow", [entry], entry, 21, ranges)],
        anchor_file=entry,
    )
    reacher = _feat(
        "aaa-reacher",
        paths=[entry],
        flows=[_flow_lr("rotate-secret-flow", [entry], entry, 21, ranges)],
    )
    result = stage_5_5_bipartite([anchored, reacher])
    survivor = next(f for f in result.flows if f.name == "rotate-secret-flow")
    assert survivor.primary_feature == "zzz-owner"
    assert survivor.secondary_features == ["aaa-reacher"]
    assert result.telemetry["duplicate_flows_dropped_cross_feature"] == 1


def test_cross_feature_tiebreak_is_lexicographic_when_anchors_equal() -> None:
    """All copies anchor the entry file with equal confidence → the primary
    is the lexicographically smallest feature name (stable across rescans)."""
    entry = "main.go"
    ranges = [(entry, 1, 5)]
    feats = [
        _feat_anchored(o, paths=[entry], flows=[_flow_lr("greet-user-flow", [entry], entry, 1, ranges)], anchor_file=entry)
        for o in ["mike", "alice", "bob"]
    ]
    result = stage_5_5_bipartite(feats)
    survivor = next(f for f in result.flows if f.name == "greet-user-flow")
    assert survivor.primary_feature == "alice"
    assert survivor.secondary_features == ["bob", "mike"]


def test_cross_feature_collapse_preserves_path_overlap_secondaries() -> None:
    """The folded loser-feature secondaries must be UNIONED with the
    path-overlap secondaries, not clobbered by Step 2."""
    entry = "hub.ts"
    other = "lib/shared.ts"
    ranges = [(entry, 1, 9)]
    # Both owners anchor the hub file; the survivor's flow ALSO reaches a path
    # owned by a third feature, which must remain a path-overlap secondary.
    a = _feat_anchored("a-owner", paths=[entry], flows=[], anchor_file=entry)
    a.flows = [_flow_lr("do-thing-flow", [entry, other], entry, 1, ranges)]
    b = _feat_anchored("b-owner", paths=[entry], flows=[], anchor_file=entry)
    b.flows = [_flow_lr("do-thing-flow", [entry, other], entry, 1, ranges)]
    third = _feat("shared-feat", paths=[other])
    result = stage_5_5_bipartite([a, b, third])
    survivor = next(f for f in result.flows if f.name == "do-thing-flow")
    assert survivor.primary_feature == "a-owner"
    # b-owner (folded) AND shared-feat (path-overlap) both present.
    assert survivor.secondary_features == ["b-owner", "shared-feat"]


def test_cross_feature_no_dup_is_noop() -> None:
    """A repo with no cross-feature duplicates is left byte-for-byte unchanged
    and reports zero cross-feature drops."""
    a = _feat_anchored(
        "a",
        paths=["a.ts"],
        flows=[_flow_lr("a-flow", ["a.ts"], "a.ts", 1, [("a.ts", 1, 5)])],
        anchor_file="a.ts",
    )
    b = _feat_anchored(
        "b",
        paths=["b.ts"],
        flows=[_flow_lr("b-flow", ["b.ts"], "b.ts", 1, [("b.ts", 1, 5)])],
        anchor_file="b.ts",
    )
    result = stage_5_5_bipartite([a, b])
    assert len(result.flows) == 2
    assert result.telemetry["duplicate_flows_dropped_cross_feature"] == 0
    assert result.telemetry["duplicate_flows_dropped"] == 0
