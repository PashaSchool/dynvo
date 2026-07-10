"""B22a — cross-app fold guard for the mint's ancestor-walk rung.

Forensic (documenso, b19r keyed OFF pair, 2026-07-10): the terminal
``fold:walk`` rung votes by plurality of ALL assigned files at the first
populated ancestor level. A dev whose files span several workspace units
(embed: apps/remix + packages/auth) has common dir "" → the vote runs at
repo ROOT immediately, where the repo's biggest anchor wins — the
``ws:packages/trpc`` PF annexed ~90 product files / 11,783 journey
span-lines outside ``packages/trpc``, poisoning every downstream
owner-map ruler (I16 re-home, journey homing, lane decisions).

The guard voids walk votes for anchors whose evidence sits wholly in a
FOREIGN workspace unit (one holding none of the dev's own files):

  * cross-unit annexation → blocked; the dev re-homes to a unit it
    actually lives in (test a), or lanes with its own unit when that
    unit holds no minted anchor at all (the apps/docs class);
  * same-unit folds untouched (test b);
  * a package folding within ITS OWN subtree stays allowed (test c);
  * ``FAULTLINE_FOLD_CROSSAPP_GUARD=0`` restores the annexation
    byte-identically (test d).

Unit roots come from the repo's own manifests (``ctx.workspaces``);
no directory-name vocabulary, no thresholds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.models.types import Feature, Flow, MemberFile
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    _SHARED_REASON_CROSS_UNIT,
    build_platform_infrastructure_lane,
    run_anchored_mint,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def flow(name: str, entry: str, paths: list[str] | None = None) -> Flow:
    return Flow(
        name=name, entry_point_file=entry, paths=paths or [entry],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def dev(name: str, paths: list[str], flows: list[Flow] | None = None,
        **kw) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        member_files=[
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
            for p in paths
        ],
        flows=flows or [],
        product_feature_id="old-pf",
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, **kw,
    )


def ws(path: str) -> SimpleNamespace:
    return SimpleNamespace(name=path.rsplit("/", 1)[-1], path=path, files=[])


def ctx_of(workspaces=None, tracked=None, repo_path=".") -> SimpleNamespace:
    return SimpleNamespace(
        workspaces=workspaces, tracked_files=tracked or [],
        repo_path=Path(repo_path), monorepo=bool(workspaces),
    )


_WORKSPACES = [ws("apps/web"), ws("apps/docs"),
               ws("packages/api"), ws("packages/lib")]


def _monorepo_fixture() -> tuple[list[Feature], list[dict]]:
    """Two minting route anchors in different units + walk-bound devs.

    * ``dash`` — page capability in apps/web (2 assigned files);
    * ``metrics`` — page capability in packages/api (10 assigned files:
      the repo's plurality winner at root level, the trpc analogue);
    * walk-bound devs are added per test.
    """
    routes = [
        {"pattern": "/dash", "method": "PAGE",
         "file": "apps/web/src/pages/dash.tsx"},
        {"pattern": "/metrics", "method": "PAGE",
         "file": "packages/api/src/pages/metrics.tsx"},
    ]
    dash = dev(
        "dash",
        ["apps/web/src/pages/dash.tsx", "apps/web/src/pages/dash-detail.tsx"],
        flows=[flow("view-dash-flow", "apps/web/src/pages/dash.tsx")])
    metrics = dev(
        "metrics",
        ["packages/api/src/pages/metrics.tsx"]
        + [f"packages/api/src/rules/r{i}.ts" for i in range(9)],
        flows=[flow("view-metrics-flow",
                    "packages/api/src/pages/metrics.tsx")])
    return [dash, metrics], routes


def _victim_multi_unit() -> Feature:
    """The documenso ``embed`` shape: files span the dev's own app +
    a shared package (common dir = repo root), no anchor lineage, span
    votes resolve nowhere → the walk decides at ROOT plurality."""
    paths = ["apps/web/src/embed/frame.ts", "packages/lib/src/embed-kit.ts"]
    return dev("embed", paths,
               flows=[flow("embed-doc-flow", paths[0], paths=list(paths))])


def _victim_isolated() -> Feature:
    """The documenso apps/docs class: wholly inside a unit that mints
    NO anchor — every walk target is cross-unit."""
    paths = ["apps/docs/src/guide/one.ts", "apps/docs/src/guide/two.ts"]
    return dev("guide", paths,
               flows=[flow("read-guide-flow", paths[0], paths=list(paths))])


# ── (a) cross-unit annexation blocked ────────────────────────────────────


def test_cross_unit_annexation_blocked_rehomes_within_own_unit() -> None:
    """The annexation-mass fix: the walk's ROOT plurality points at the
    foreign packages/api anchor (10 votes vs dash's 2); the guard voids
    the foreign votes and the dev re-homes inside apps/web, a unit it
    actually lives in."""
    devs, routes = _monorepo_fixture()
    dash, metrics = devs
    victim = _victim_multi_unit()
    all_devs = devs + [victim]
    pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
    assert dash.product_feature_id is not None
    assert metrics.product_feature_id is not None
    assert victim.product_feature_id == dash.product_feature_id, (
        "B22a REGRESSION: cross-unit walk annexation not blocked — the "
        "dev landed on {!r} instead of its own unit's anchor".format(
            victim.product_feature_id))
    assert (victim.anchor_id or "").startswith("fold:walk->")
    assert tele.get("fold_walk_crossunit_rehomed", 0) == 1
    assert tele.get("law_flowful_in_lane", 0) == 0


def test_isolated_cross_unit_dev_lanes_with_its_unit() -> None:
    """The apps/docs / apps/openpage-api class: the dev's unit mints no
    anchor, so EVERY walk target is foreign — the dev lanes with its
    unit (reason ``cross_unit_isolation``) instead of poisoning a
    foreign PF's owner map."""
    devs, routes = _monorepo_fixture()
    victim = _victim_isolated()
    all_devs = devs + [victim]
    pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
    assert victim.product_feature_id is None
    assert victim.shared_reason == _SHARED_REASON_CROSS_UNIT
    assert tele.get("fold_walk_crossunit_laned", 0) == 1
    # the guard's refusal is NOT the degenerate-scan law breach
    assert tele.get("law_flowful_in_lane", 0) == 0
    lane = build_platform_infrastructure_lane(all_devs)
    assert [r["name"] for r in lane] == ["guide"]
    assert lane[0]["shared_reason"] == _SHARED_REASON_CROSS_UNIT


# ── (b) same-unit fold untouched (anti-case) ─────────────────────────────


def test_same_unit_fold_untouched(monkeypatch) -> None:
    """A dev wholly inside apps/web walking to the apps/web anchor is
    the guard's explicit non-target: ON and OFF agree exactly."""
    def _run(flag: str):
        monkeypatch.setenv("FAULTLINE_FOLD_CROSSAPP_GUARD", flag)
        devs, routes = _monorepo_fixture()
        dash, metrics = devs
        paths = ["apps/web/src/widgets/a.ts", "apps/web/src/widgets/b.ts"]
        victim = dev("widgets", paths,
                     flows=[flow("use-widget-flow", paths[0],
                                 paths=list(paths))])
        all_devs = devs + [victim]
        pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
        return victim, dash, tele

    v_on, dash_on, tele_on = _run("1")
    v_off, dash_off, tele_off = _run("0")
    assert v_on.product_feature_id == dash_on.product_feature_id
    assert v_off.product_feature_id == dash_off.product_feature_id
    assert v_on.product_feature_id == v_off.product_feature_id
    assert v_on.anchor_id == v_off.anchor_id
    assert tele_on.get("fold_walk_crossunit_rehomed", 0) == 0
    assert tele_on.get("fold_walk_crossunit_laned", 0) == 0


# ── (c) package folding inside its own subtree allowed (anti-case) ───────


def test_package_folding_inside_own_subtree_allowed() -> None:
    """A dev under packages/api folding into the packages/api anchor is
    a SAME-unit fold — the guard must not touch it."""
    devs, routes = _monorepo_fixture()
    dash, metrics = devs
    paths = ["packages/api/src/util/norm.ts", "packages/api/src/util/fmt.ts"]
    victim = dev("util", paths,
                 flows=[flow("normalize-flow", paths[0], paths=list(paths))])
    all_devs = devs + [victim]
    pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
    assert victim.product_feature_id == metrics.product_feature_id
    assert (victim.anchor_id or "").startswith("fold:walk->")
    assert tele.get("fold_walk_crossunit_rehomed", 0) == 0
    assert tele.get("fold_walk_crossunit_laned", 0) == 0


# ── (d) kill-switch restores the annexation ──────────────────────────────


def test_flag_off_restores_cross_unit_annexation(monkeypatch) -> None:
    """FAULTLINE_FOLD_CROSSAPP_GUARD=0 must reproduce the OLD walk: the
    multi-unit dev annexes to the foreign root-plurality winner and the
    isolated dev annexes too — no guard telemetry, no cross-unit lane."""
    monkeypatch.setenv("FAULTLINE_FOLD_CROSSAPP_GUARD", "0")
    devs, routes = _monorepo_fixture()
    dash, metrics = devs
    victim = _victim_multi_unit()
    isolated = _victim_isolated()
    all_devs = devs + [victim, isolated]
    pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(_WORKSPACES))
    assert victim.product_feature_id == metrics.product_feature_id, (
        "flag=0 must restore the pre-B22a annexation (root plurality)")
    # the isolated dev annexes at the ``apps`` super-level (dash is the
    # only anchor with files under apps/) — cross-unit all the same.
    assert isolated.product_feature_id == dash.product_feature_id
    assert "fold_walk_crossunit_rehomed" not in tele
    assert "fold_walk_crossunit_laned" not in tele
    assert build_platform_infrastructure_lane(all_devs) == []


def test_no_workspace_units_guard_inert() -> None:
    """A repo with no workspace manifests (no ctx.workspaces, no
    top-level unit manifests) leaves the guard fully inert — the walk
    behaves exactly as before."""
    devs, routes = _monorepo_fixture()
    dash, metrics = devs
    victim = _victim_multi_unit()
    all_devs = devs + [victim]
    pfs, tele = run_anchored_mint(all_devs, routes, ctx_of(None))
    assert victim.product_feature_id == metrics.product_feature_id
    assert "fold_walk_crossunit_rehomed" not in tele
    assert "fold_walk_crossunit_laned" not in tele
