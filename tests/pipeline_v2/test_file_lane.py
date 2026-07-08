"""FILELANE — file-level shared-infrastructure lane (post-emission Stage 7).

Fixtures distilled from the wave7 forensics: unowned shared-infra files
(``lib/prisma.ts``, ``components/ui/button.tsx``, ``configs/constants.py``)
imported across many product features with no product surface of their own.
The mandatory anti-cases (feedback-mechanisms-over-vocabularies) are the spine
of this suite: a product SURFACE is never laned however widely imported; a file
a PF CLAIMS is never laned; a SINGLE-PF util is never laned; an owned file is
never contested.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature
from faultline.pipeline_v2 import file_lane as FL
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    _SHARED_REASON_INFRA_FANIN,
    build_platform_infrastructure_lane,
)
from faultline.pipeline_v2.ts_ast.adapter import ProvenanceView
from faultline.pipeline_v2.ts_ast.shapes import ResolvedEdge

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── builders ─────────────────────────────────────────────────────────────


def _dev(name, paths, *, pfid=None, reason=None):
    return Feature(
        name=name, paths=list(paths), flows=[],
        product_feature_id=pfid,
        shared_reason=reason if pfid is None else None,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def _pf(slug, paths):
    f = Feature(
        name=slug, paths=list(paths), flows=[],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, loc=10,
    )
    f.layer = "product"
    f.anchor_id = f"route:{slug}"
    return f


def _view(edges_by_src: dict[str, list[tuple[str, str | None, str]]]) -> ProvenanceView:
    """``{src: [(target_file, resolution, raw), ...]}`` → ProvenanceView.

    ``target_file=None`` models an unresolved/external edge (excluded by
    ``in_repo_targets``).
    """
    by_src: dict[str, dict[str, list[ResolvedEdge]]] = {}
    for src, edges in edges_by_src.items():
        slot = by_src.setdefault(src, {})
        for tgt, res, raw in edges:
            slot.setdefault(raw, []).append(ResolvedEdge(
                src_file=src, raw_target=raw, target_file=tgt,
                resolution=res, via_barrels=(), names=("X",), kind="named",
            ))
    files = frozenset(by_src)
    return ProvenanceView(
        tracked_key=files, files=files, _by_src=by_src, _weights={})


class _Ctx:
    def __init__(self, tracked, repo="/repo"):
        self.tracked_files = list(tracked)
        self.repo_path = repo


def _imports(src: str, *targets: str) -> list[tuple[str, str, str]]:
    """A source importing each target via a workspace (in-repo) edge."""
    return [(t, "workspace", f"@x/{t}") for t in targets]


def _world(num_extra_pfs: int = 10, infra="lib/prisma.ts"):
    """A repo where ``infra`` is imported by ``num_extra_pfs`` distinct PF-owned
    files, plus one lane dev (so a template exists). Returns (devs, pfs, ctx,
    views)."""
    pfs = []
    devs = []
    edges: dict[str, list] = {}
    for i in range(num_extra_pfs):
        pf = f"pf{i}"
        owner_file = f"features/{pf}/page.ts"
        pfs.append(_pf(pf, [owner_file]))
        devs.append(_dev(f"{pf}-owner", [owner_file], pfid=pf))
        edges[owner_file] = _imports(owner_file, infra)
    lane = _dev("existing-lane", ["lib/legacy.ts"], reason="no_anchor_lineage")
    devs.append(lane)
    edges.setdefault(infra, [])  # infra itself imports nothing product
    ctx = _Ctx(list(edges) + [infra])
    return devs, pfs, ctx, [_view(edges)]


def _run(devs, pfs, ctx, views, routes=None):
    return FL.run_file_lane_infra(devs, pfs, routes, ctx, views=views)


# ── POSITIVE: genuine shared infra is laned ───────────────────────────────


def test_genuine_shared_infra_is_laned(monkeypatch):
    monkeypatch.setenv(FL.FILE_LANE_PCT_ENV, "0.10")
    devs, pfs, ctx, views = _world(num_extra_pfs=10)   # thr=ceil(.1*10)=2? floor2
    before = len(devs)
    tele = _run(devs, pfs, ctx, views)
    assert tele["applied"] is True
    assert tele["laned_files"] == 1
    assert len(devs) == before + tele["laned_devs"]
    lane = next(d for d in devs if d.shared_reason == _SHARED_REASON_INFRA_FANIN)
    assert "lib/prisma.ts" in lane.paths
    assert lane.product_feature_id is None
    # closes the loop: the lane surface emits it
    rows = build_platform_infrastructure_lane(devs)
    assert any("lib/prisma.ts" in r["paths"] for r in rows)


def test_lane_dev_fields_are_clean(monkeypatch):
    monkeypatch.setenv(FL.FILE_LANE_PCT_ENV, "0.10")
    devs, pfs, ctx, views = _world(num_extra_pfs=8)
    _run(devs, pfs, ctx, views)
    lane = next(d for d in devs if d.shared_reason == _SHARED_REASON_INFRA_FANIN)
    assert lane.authors == [] and lane.total_commits == 0
    assert lane.flows == [] and lane.layer == "developer"
    assert lane.member_files and all(m.primary for m in lane.member_files)
    assert all(m.role == "anchor" for m in lane.member_files)


# ── ANTI-CASE 1: a product SURFACE is never laned (S3 hard) ────────────────


def test_product_surface_never_laned(monkeypatch):
    monkeypatch.setenv(FL.FILE_LANE_PCT_ENV, "0.10")
    devs, pfs, ctx, views = _world(num_extra_pfs=10, infra="app/shared/page.tsx")
    routes = [{"file": "app/shared/page.tsx", "pattern": "/shared", "method": "PAGE"}]
    tele = _run(devs, pfs, ctx, views, routes=routes)
    assert tele["laned_files"] == 0
    assert tele["blocked_surface"] >= 1
    assert not any(d.shared_reason == _SHARED_REASON_INFRA_FANIN for d in devs)


# ── ANTI-CASE 2: a file a PF CLAIMS in pf.paths is never laned (guard) ─────


def test_pf_claimed_file_never_laned(monkeypatch):
    monkeypatch.setenv(FL.FILE_LANE_PCT_ENV, "0.10")
    devs, pfs, ctx, views = _world(num_extra_pfs=10)
    pfs[0].paths.append("lib/prisma.ts")   # a PF now claims the infra file
    tele = _run(devs, pfs, ctx, views)
    assert tele["laned_files"] == 0
    assert tele["blocked_pf_paths"] >= 1


def test_owned_file_never_contested(monkeypatch):
    monkeypatch.setenv(FL.FILE_LANE_PCT_ENV, "0.10")
    devs, pfs, ctx, views = _world(num_extra_pfs=10)
    devs.append(_dev("prisma-owner", ["lib/prisma.ts"], pfid="pf0"))
    tele = _run(devs, pfs, ctx, views)
    assert tele["laned_files"] == 0
    assert tele["blocked_owned"] >= 1


# ── ANTI-CASE 3: a SINGLE-PF util / under-attributed product file ──────────


def test_single_pf_util_not_laned(monkeypatch):
    # imported by only ONE PF's code → fan-in 1 < shared-floor 2 → private util.
    monkeypatch.setenv(FL.FILE_LANE_PCT_ENV, "0.10")
    infra = "lib/pf0-only-util.ts"
    pfs = [_pf("pf0", ["features/pf0/page.ts"])]
    devs = [_dev("pf0-owner", ["features/pf0/page.ts"], pfid="pf0"),
            _dev("lane", ["lib/legacy.ts"], reason="no_anchor_lineage")]
    edges = {"features/pf0/page.ts": _imports("features/pf0/page.ts", infra)}
    ctx = _Ctx(list(edges) + [infra])
    tele = _run(devs, pfs, ctx, [_view(edges)])
    assert tele["laned_files"] == 0
    assert tele["blocked_low_fanin"] >= 1


def test_two_sibling_flows_one_pf_not_laned(monkeypatch):
    # imported by TWO files but both owned by the SAME PF → distinct-PF fan-in
    # is 1, not 2 → not shared across the product (feature-local).
    monkeypatch.setenv(FL.FILE_LANE_PCT_ENV, "0.10")
    infra = "lib/pf0-helper.ts"
    pfs = [_pf("pf0", ["features/pf0/a.ts", "features/pf0/b.ts"])]
    devs = [_dev("pf0-owner", ["features/pf0/a.ts", "features/pf0/b.ts"], pfid="pf0"),
            _dev("lane", ["lib/legacy.ts"], reason="no_anchor_lineage")]
    edges = {
        "features/pf0/a.ts": _imports("features/pf0/a.ts", infra),
        "features/pf0/b.ts": _imports("features/pf0/b.ts", infra),
    }
    ctx = _Ctx(list(edges) + [infra])
    tele = _run(devs, pfs, ctx, [_view(edges)])
    assert tele["laned_files"] == 0


# ── SCALE-INVARIANCE: threshold tracks product breadth, not an absolute ────


@pytest.mark.parametrize("n_pfs,expect_laned", [(20, True), (60, False)])
def test_threshold_is_scale_invariant(monkeypatch, n_pfs, expect_laned):
    # pct=0.10: 20 PFs → thr=2; 60 PFs → thr=6. The SAME infra file imported by
    # exactly 3 distinct PFs is laned in the small repo (3>=2) but NOT in the
    # large repo (3<6): "shared" is relative to product breadth.
    monkeypatch.setenv(FL.FILE_LANE_PCT_ENV, "0.10")
    infra = "lib/x.ts"
    pfs, devs, edges = [], [], {}
    for i in range(n_pfs):
        pf = f"pf{i}"
        of = f"features/{pf}/p.ts"
        pfs.append(_pf(pf, [of]))
        devs.append(_dev(f"{pf}-o", [of], pfid=pf))
        edges[of] = _imports(of, infra) if i < 3 else []  # only 3 PFs import it
    devs.append(_dev("lane", ["lib/legacy.ts"], reason="no_anchor_lineage"))
    ctx = _Ctx(list(edges) + [infra])
    tele = _run(devs, pfs, ctx, [_view(edges)])
    assert tele["threshold"] == max(FL.SHARED_FLOOR, -(-n_pfs // 10))
    assert (tele["laned_files"] == 1) is expect_laned


# ── DETERMINISM ────────────────────────────────────────────────────────────


def test_determinism_double_run(monkeypatch):
    monkeypatch.setenv(FL.FILE_LANE_PCT_ENV, "0.10")

    def snapshot():
        devs, pfs, ctx, views = _world(num_extra_pfs=8)
        # two infra files in two dirs → two lane devs, order must be stable
        views[0]._by_src["features/pf0/page.ts"].setdefault(
            "@x/components/ui/button.tsx", []).append(ResolvedEdge(
                src_file="features/pf0/page.ts", raw_target="@x/b",
                target_file="components/ui/button.tsx", resolution="workspace",
                via_barrels=(), names=("B",), kind="named"))
        for i in range(1, 8):
            views[0]._by_src[f"features/pf{i}/page.ts"].setdefault(
                "@x/b", []).append(ResolvedEdge(
                    src_file=f"features/pf{i}/page.ts", raw_target="@x/b",
                    target_file="components/ui/button.tsx",
                    resolution="workspace", via_barrels=(), names=("B",),
                    kind="named"))
        _run(devs, pfs, ctx, views)
        lanes = [d for d in devs
                 if d.shared_reason == _SHARED_REASON_INFRA_FANIN]
        return [(d.name, d.uuid, tuple(d.paths)) for d in
                sorted(lanes, key=lambda d: d.name)]

    assert snapshot() == snapshot()


# ── KILL-SWITCH + degenerate no-ops ────────────────────────────────────────


def test_kill_switch_no_op(monkeypatch):
    monkeypatch.setenv(FL.FILE_LANE_ENV, "0")
    assert FL.file_lane_enabled() is False
    devs, pfs, ctx, views = _world(num_extra_pfs=10)
    before = len(devs)
    tele = _run(devs, pfs, ctx, views)
    assert tele["enabled"] is False
    assert tele["applied"] is False
    assert len(devs) == before


def test_no_product_features_no_op():
    devs = [_dev("lane", ["lib/x.ts"], reason="no_anchor_lineage")]
    tele = FL.run_file_lane_infra(devs, [], None, _Ctx(["lib/x.ts"]),
                                  views=[_view({})])
    assert tele["applied"] is False
    assert tele["num_product_features"] == 0


# ── ACCESSOR: in_repo_targets returns ALL in-repo resolutions only ─────────


def test_in_repo_targets_accessor():
    view = _view({
        "a.ts": [
            ("b.ts", "relative", "./b"),
            ("c.ts", "tsconfig_alias", "@/c"),
            ("pkg/d.ts", "workspace", "@scope/d"),
            (None, "package_external", "react"),   # external → excluded
            (None, "unresolved", "./missing"),      # unresolved → excluded
        ],
    })
    assert view.in_repo_targets("a.ts") == frozenset({"b.ts", "c.ts", "pkg/d.ts"})
    assert view.in_repo_targets("unknown.ts") == frozenset()
    # workspace_targets stays the narrow (workspace-only) accessor
    assert view.workspace_targets("a.ts") == frozenset({"pkg/d.ts"})
