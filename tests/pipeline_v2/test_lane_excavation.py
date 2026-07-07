"""Product-Spine W4.3 — lane excavation (lane_excavation.py).

Fixtures are DISTILLED from the w43-diagnosis measurement (wave4-out,
2026-07-07): an app-shell lane group whose flows live in domain dirs
(the supabase ``studio``+``data`` shape), an existing same-key PF to
WIDEN, an unminted route family to MINT, a technology-instrument dir
that must STAY laned (W4.2 detector at the shared mint bar), a sub-floor
micro-dir the husk floor sieves, and a giant residual that must never
attach to a tiny sub-anchor (anti-sink).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.models.types import Feature, FeatureFlowEdge, Flow, MemberFile
from faultline.pipeline_v2.lane_excavation import (
    lane_excavation_enabled,
    run_lane_excavation,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def flow(name: str, entry: str, paths: list[str] | None = None,
         fid: str | None = None) -> Flow:
    fl = Flow(
        name=name, entry_point_file=entry, paths=paths or [entry],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )
    if fid:
        fl.id = fid
        fl.primary_feature = fid.split("::", 1)[0]
    return fl


def lane_dev(name: str, paths: list[str],
             flows: list[Flow] | None = None) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        member_files=[
            MemberFile(path=p, role="shared", confidence=1.0, primary=False)
            for p in paths
        ],
        flows=flows or [],
        product_feature_id=None,
        shared_reason="shell_lineage_only",
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def assigned_dev(name: str, paths: list[str], pf: str) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        member_files=[
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
            for p in paths
        ],
        flows=[],
        product_feature_id=pf,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def product_feature(name: str, anchor_id: str, paths: list[str]) -> Feature:
    pf = Feature(
        name=name, paths=list(paths),
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )
    pf.layer = "product"
    pf.anchor_id = anchor_id
    return pf


def _write(repo: Path, rel: str, lines: int) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"const x{i} = {i};\n" for i in range(lines)))
    return rel


def studio_fixture(tmp_path: Path):
    """The distilled supabase-studio shape."""
    repo = tmp_path / "repo"

    # domain AUTH — existing PF (route:…/pages/auth) to WIDEN.
    auth_pages = [_write(repo, "apps/studio/pages/auth/sign-in.tsx", 40)]
    auth_data = [
        _write(repo, "apps/studio/data/auth/users.ts", 120),
        _write(repo, "apps/studio/data/auth/sessions.ts", 100),
    ]
    auth_ui = [_write(repo, "apps/studio/components/interfaces/Auth/list.tsx", 80)]

    # domain REPORTS — unminted route family → MINT (route + excav).
    reports_page = [_write(repo, "apps/studio/pages/reports/index.tsx", 30)]
    reports_data = [
        _write(repo, "apps/studio/data/reports/queries.ts", 150),
        _write(repo, "apps/studio/data/reports/charts.ts", 90),
    ]

    # technology-instrument dir — MUST stay laned (W4.2 bar).
    telem = [
        _write(repo, "apps/studio/telemetry/client.ts", 100),
        _write(repo, "apps/studio/telemetry/events.ts", 100),
    ]

    # sub-floor micro-dir — husk floor sieves it.
    tiny = [
        _write(repo, "apps/studio/components/interfaces/Tiny/a.tsx", 20),
        _write(repo, "apps/studio/components/interfaces/Tiny/b.tsx", 20),
    ]

    # honest residual (structural).
    residual = [
        _write(repo, "apps/studio/components/layouts/Base.tsx", 60),
        _write(repo, "apps/studio/components/layouts/Wide.tsx", 60),
    ]

    auth_flows = [
        flow("list-users-flow", auth_data[0], fid="studio::list-users-flow"),
        flow("list-sessions-flow", auth_data[1]),
    ]
    telem_flow = [flow("send-event-flow", telem[0])]
    tiny_flow = [flow("tiny-flow", tiny[0])]

    shell = lane_dev(
        "studio",
        auth_data + auth_ui + telem + tiny + residual,
        flows=auth_flows + telem_flow + tiny_flow,
    )
    donor = lane_dev("data", reports_data)  # flowless content donor
    pages_dev = assigned_dev("auth-pages", auth_pages, pf="auth")
    reports_pages_dev = lane_dev("reports-page", reports_page,
                                 flows=[flow("view-reports-flow",
                                             reports_page[0])])

    features = [shell, donor, pages_dev, reports_pages_dev]
    pfs = [product_feature("auth", "route:apps/studio/pages/auth",
                           auth_pages)]
    routes_index = [
        {"pattern": "/auth/sign-in", "method": "PAGE",
         "file": auth_pages[0]},
        {"pattern": "/reports", "method": "PAGE",
         "file": reports_page[0]},
    ]
    tracked = [str(p.relative_to(repo))
               for p in repo.rglob("*") if p.is_file()]
    ctx = SimpleNamespace(
        workspaces=None, tracked_files=tracked, repo_path=repo,
        monorepo=False,
    )
    edges = [FeatureFlowEdge(feature="studio",
                             flow_id="studio::list-users-flow",
                             type="primary")]
    return repo, features, pfs, routes_index, ctx, edges, shell, donor


def _run(features, pfs, routes_index, ctx, edges,
         instrument_dirs=frozenset()):
    return run_lane_excavation(
        features, pfs, routes_index, ctx,
        extractor_signals=None,
        instrument_dirs=instrument_dirs,
        feature_flow_edges=edges,
    )


def test_widen_existing_pf_and_mint_new(tmp_path):
    (_repo, features, pfs, ri, ctx, edges, shell,
     donor) = studio_fixture(tmp_path)
    tele = _run(features, pfs, ri, ctx, edges,
                instrument_dirs=frozenset({"apps/studio/telemetry"}))
    assert tele["applied"] is True

    # AUTH: carved chunk joined the EXISTING PF (widen, not a twin).
    auth_chunks = [f for f in features
                   if getattr(f, "product_feature_id", None) == "auth"
                   and "lane-excavation" in (f.description or "")]
    assert auth_chunks, "auth chunk must join the existing auth PF"
    assert len([p for p in pfs if p.name == "auth"]) == 1
    chunk = auth_chunks[0]
    assert set(chunk.paths) == {
        "apps/studio/data/auth/users.ts",
        "apps/studio/data/auth/sessions.ts",
        "apps/studio/components/interfaces/Auth/list.tsx",
    }
    assert chunk.anchor_id.startswith("fold:excavation->")
    # both auth flows moved with their entry files.
    assert {fl.name for fl in chunk.flows} == {
        "list-users-flow", "list-sessions-flow"}

    # REPORTS: new PF minted from route+excav merge; the flowless donor
    # dev moved whole (multi-source 0-flow allowance).
    report_pfs = [p for p in pfs if p.anchor_id and "reports" in p.anchor_id]
    assert len(report_pfs) == 1
    assert donor.product_feature_id == report_pfs[0].name
    assert donor.shared_reason is None

    assert tele["pfs_widened"] == 1
    assert tele["pfs_minted"] >= 1


def test_instrument_dir_stays_laned(tmp_path):
    (_repo, features, pfs, ri, ctx, edges, shell,
     _donor) = studio_fixture(tmp_path)
    tele = _run(features, pfs, ri, ctx, edges,
                instrument_dirs=frozenset({"apps/studio/telemetry"}))
    # telemetry files never left the shell; the W4.2 bar blocked them.
    assert "apps/studio/telemetry/client.ts" in shell.paths
    assert tele["guard_blocks"].get("bar:technology_instrument", 0) >= 1
    assert not any(
        "telemetry" in (getattr(p, "anchor_id", "") or "") for p in pfs)


def test_husk_floor_sieves_micro_dirs(tmp_path):
    (_repo, features, pfs, ri, ctx, edges, shell,
     _donor) = studio_fixture(tmp_path)
    tele = _run(features, pfs, ri, ctx, edges)
    # Tiny (40 code-LOC) stayed on the shell.
    assert "apps/studio/components/interfaces/Tiny/a.tsx" in shell.paths
    assert tele["guard_blocks"].get("code_loc_floor", 0) >= 1


def test_conservation_and_no_foreign_claims(tmp_path):
    (_repo, features, pfs, ri, ctx, edges, shell,
     donor) = studio_fixture(tmp_path)
    before_owned = set(shell.paths) | set(donor.paths)
    before_flow_count = len(shell.flows) + len(donor.flows)
    foreign = {"apps/studio/pages/auth/sign-in.tsx"}
    _run(features, pfs, ri, ctx, edges,
         instrument_dirs=frozenset({"apps/studio/telemetry"}))
    chunks = [f for f in features
              if "lane-excavation" in (getattr(f, "description", "") or "")]
    after_owned = set(shell.paths) | set(donor.paths)
    for c in chunks:
        after_owned |= set(c.paths)
    assert after_owned == before_owned  # nothing lost, nothing invented
    for c in chunks:
        assert not (set(c.paths) & foreign)  # PF-owned files untouched
    after_flow_count = (
        len(shell.flows) + len(donor.flows)
        + sum(len(c.flows) for c in chunks))
    assert after_flow_count == before_flow_count


def test_edges_and_flow_identity_restamped(tmp_path):
    (_repo, features, pfs, ri, ctx, edges, _shell,
     _donor) = studio_fixture(tmp_path)
    _run(features, pfs, ri, ctx, edges)
    moved = [f for f in features
             if "lane-excavation" in (getattr(f, "description", "") or "")
             and any(fl.name == "list-users-flow" for fl in f.flows)]
    assert moved
    chunk = moved[0]
    fl = next(f for f in chunk.flows if f.name == "list-users-flow")
    assert fl.primary_feature == chunk.name
    assert fl.id == f"{chunk.name}::list-users-flow"
    edge = edges[0]
    assert edge.feature == chunk.name
    assert edge.flow_id == fl.id


def test_anti_sink_giant_residual_never_attaches(tmp_path):
    """A giant shell whose files only SLIVER-overlap a tiny anchor must
    not whole-dev move; only the subtree chunk leaves; the residual
    stays honestly laned (the D1 capacity discipline)."""
    repo = tmp_path / "repo"
    auth_page = [_write(repo, "apps/big/pages/auth/sign-in.tsx", 40)]
    inside = [
        _write(repo, "apps/big/data/auth/a.ts", 120),
        _write(repo, "apps/big/data/auth/b.ts", 80),
    ]
    outside = [
        _write(repo, f"apps/big/engine/part{i}/mod{i}.ts", 60)
        for i in range(20)
    ]
    giant = lane_dev("big", inside + outside,
                     flows=[flow("auth-flow", inside[0]),
                            flow("engine-flow", outside[0])])
    pages_dev = assigned_dev("auth-pages", auth_page, pf="auth")
    pfs = [product_feature("auth", "route:apps/big/pages/auth", auth_page)]
    ri = [{"pattern": "/auth/sign-in", "method": "PAGE",
           "file": auth_page[0]}]
    tracked = [str(p.relative_to(repo))
               for p in repo.rglob("*") if p.is_file()]
    ctx = SimpleNamespace(workspaces=None, tracked_files=tracked,
                          repo_path=repo, monorepo=False)
    features = [giant, pages_dev]
    _run(features, pfs, ri, ctx, [])
    # giant itself is NOT bound to auth…
    assert giant.product_feature_id is None
    assert giant.shared_reason == "shell_lineage_only"
    # …its auth subtree chunk is, and the engine mass stayed behind.
    chunks = [f for f in features
              if "lane-excavation" in (getattr(f, "description", "") or "")]
    assert len(chunks) >= 1
    assert set(chunks[0].paths) == set(inside)
    assert all(p in giant.paths for p in outside)
    assert any(fl.name == "engine-flow" for fl in giant.flows)


def test_idempotent_second_run_is_noop(tmp_path):
    (_repo, features, pfs, ri, ctx, edges, _shell,
     _donor) = studio_fixture(tmp_path)
    _run(features, pfs, ri, ctx, edges,
         instrument_dirs=frozenset({"apps/studio/telemetry"}))
    pf_count = len(pfs)
    dev_count = len(features)
    tele2 = _run(features, pfs, ri, ctx, edges,
                 instrument_dirs=frozenset({"apps/studio/telemetry"}))
    assert len(pfs) == pf_count
    assert len(features) == dev_count
    assert tele2["chunks"] == 0
    assert tele2["devs_moved"] == 0


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LANE_EXCAVATION", "0")
    assert lane_excavation_enabled() is False
    monkeypatch.setenv("FAULTLINE_LANE_EXCAVATION", "1")
    assert lane_excavation_enabled() is True


def test_ui_shape_token_key_never_candidates(tmp_path):
    """openstatus finding: a FLOWFUL `data-table` component dir must not
    excavate — the key's last token is a UI shape (widget, not
    capability)."""
    repo = tmp_path / "repo"
    page = [_write(repo, "apps/dash/pages/home/index.tsx", 30)]
    dt = [
        _write(repo, "apps/dash/src/components/data-table/core.tsx", 120),
        _write(repo, "apps/dash/src/components/data-table/rows.tsx", 100),
    ]
    shell = lane_dev("dash", dt, flows=[flow("sort-flow", dt[0])])
    pages_dev = assigned_dev("home-pages", page, pf="home")
    pfs = [product_feature("home", "route:apps/dash/pages/home", page)]
    ri = [{"pattern": "/home", "method": "PAGE", "file": page[0]}]
    tracked = [str(p.relative_to(repo))
               for p in repo.rglob("*") if p.is_file()]
    ctx = SimpleNamespace(workspaces=None, tracked_files=tracked,
                          repo_path=repo, monorepo=False)
    features = [shell, pages_dev]
    _run(features, pfs, ri, ctx, [])
    assert all(p in shell.paths for p in dt)  # nothing carved
    assert not any(
        "data-table" in (getattr(p, "anchor_id", "") or "") for p in pfs)


def test_zero_flow_needs_authored_source(tmp_path):
    """A 0-flow domain dir with only DERIVED evidence (excav alone, or
    excav+interior) never mints; the same dir with a same-key ROUTE
    partner may (the donor-dev path in the studio fixture covers the
    positive case)."""
    repo = tmp_path / "repo"
    page = [_write(repo, "apps/app/pages/home/index.tsx", 30)]
    repl = [
        _write(repo, "apps/app/src/data/replication/a.ts", 150),
        _write(repo, "apps/app/src/data/replication/b.ts", 120),
    ]
    shell = lane_dev("app", repl,
                     flows=[flow("boot-flow", page[0])])  # flow OUTSIDE
    pages_dev = assigned_dev("home-pages", page, pf="home")
    pfs = [product_feature("home", "route:apps/app/pages/home", page)]
    ri = [{"pattern": "/home", "method": "PAGE", "file": page[0]}]
    tracked = [str(p.relative_to(repo))
               for p in repo.rglob("*") if p.is_file()]
    ctx = SimpleNamespace(workspaces=None, tracked_files=tracked,
                          repo_path=repo, monorepo=False)
    features = [shell, pages_dev]
    tele = _run(features, pfs, ri, ctx, [])
    assert all(p in shell.paths for p in repl)
    assert tele["guard_blocks"].get("zero_flow_unauthored", 0) >= 1
