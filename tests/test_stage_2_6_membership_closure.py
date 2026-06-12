"""Tests for :mod:`faultline.pipeline_v2.stage_2_6_membership_closure`.

Covers every rule of the Phase-1 membership pass:

  - closure attach (BFS over static imports from ALL anchor files)
  - fan-in exclusion (shared infra never attaches; provenance only)
  - primary/shared tie-break (depth → source priority → name)
  - co-commit gate (majority share + count floor + sweep-commit guard)
  - residual-pool shrink (attached files leave ``unattributed``)
  - anchor provenance + back-compat (paths stays primary-only)
  - FastAPI-shaped fixture with <3 exports still gets members
    (the Stage 3 MIN_EXPORTS gate must not starve closure)
  - determinism (two runs → identical output)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.models.types import Commit
from faultline.pipeline_v2.stage_2_6_membership_closure import (
    ANCHOR_SOURCES,
    run_membership_closure,
)
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature


# ── Helpers ──────────────────────────────────────────────────────────────


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _ctx(repo: Path, commits: list[Commit] | None = None) -> SimpleNamespace:
    tracked: list[str] = []
    for f in repo.rglob("*"):
        if f.is_file() and "/.git" not in str(f):
            tracked.append(f.relative_to(repo).as_posix())
    return SimpleNamespace(
        repo_path=repo,
        tracked_files=tuple(sorted(tracked)),
        commits=commits or [],
        run_dir=None,
        stack="next-app-router",
        monorepo=False,
        workspaces=[],
    )


def _feat(
    name: str,
    paths: tuple[str, ...],
    sources: list[str] | None = None,
) -> DeveloperFeature:
    return DeveloperFeature(
        name=name,
        paths=paths,
        sources=sources or ["route"],
        confidence="high",
    )


def _commit(
    sha: str, files: list[str], *, days_ago: int = 0,
) -> Commit:
    return Commit(
        sha=sha,
        message=f"chore: {sha}",
        author="dev@example.com",
        date=datetime.now(timezone.utc) - timedelta(days=days_ago),
        files_changed=files,
    )


def _member(feature: DeveloperFeature, path: str):
    for m in feature.member_files:
        if m.path == path:
            return m
    return None


# ── Closure attach ───────────────────────────────────────────────────────


def test_closure_attaches_imported_service_chain(tmp_path: Path) -> None:
    """Route file → service → util chain attaches both orphans."""
    _w(tmp_path / "api/route.ts",
       'import {svc} from "../lib/service";\n'
       "export async function POST(req: Request) { return svc(req); }\n")
    _w(tmp_path / "lib/service.ts",
       'import {helper} from "./util";\n'
       "export function svc(r: Request) { return helper(r); }\n")
    _w(tmp_path / "lib/util.ts",
       "export function helper(r: Request) { return r; }\n")
    ctx = _ctx(tmp_path)
    feat = _feat("documents", ("api/route.ts",))
    unattributed = ["lib/service.ts", "lib/util.ts"]

    res = run_membership_closure([feat], unattributed, ctx)

    assert set(feat.paths) == {
        "api/route.ts", "lib/service.ts", "lib/util.ts",
    }
    assert res.unattributed == []
    svc = _member(feat, "lib/service.ts")
    util = _member(feat, "lib/util.ts")
    assert svc is not None and svc.role == "closure" and svc.primary
    assert util is not None and util.role == "closure" and util.primary
    # Depth-decaying confidence: direct import beats two hops.
    assert svc.confidence > util.confidence
    assert res.telemetry.closure_attached == 2


def test_anchor_provenance_recorded_for_all_features(tmp_path: Path) -> None:
    """Every pre-existing path gets a primary role='anchor' record —
    even on non-anchor-sourced features (member_files is a full
    ledger, not a delta)."""
    _w(tmp_path / "pkg/index.ts", "export const x = 1;\n")
    ctx = _ctx(tmp_path)
    feat = _feat("pkg", ("pkg/index.ts",), sources=["package"])

    run_membership_closure([feat], [], ctx)

    m = _member(feat, "pkg/index.ts")
    assert m is not None
    assert m.role == "anchor" and m.primary and m.confidence == 1.0


def test_non_anchor_sources_do_not_seed_closure(tmp_path: Path) -> None:
    """A package-sourced feature never BFS-claims orphans (Stage 6.3
    owns that expansion)."""
    _w(tmp_path / "pkg/index.ts",
       'import {h} from "./helper";\nexport const x = h();\n')
    _w(tmp_path / "pkg/helper.ts", "export function h() { return 1; }\n")
    ctx = _ctx(tmp_path)
    feat = _feat("pkg", ("pkg/index.ts",), sources=["package"])
    unattributed = ["pkg/helper.ts"]

    res = run_membership_closure([feat], unattributed, ctx)

    assert feat.paths == ("pkg/index.ts",)
    assert res.unattributed == ["pkg/helper.ts"]
    assert res.telemetry.anchor_features == 0


def test_owned_files_are_waypoints_not_claims(tmp_path: Path) -> None:
    """Closure traverses THROUGH files owned by another feature but
    never steals them; orphans behind them still attach."""
    _w(tmp_path / "api/route.ts",
       'import {owned} from "../lib/owned";\n'
       "export function GET() { return owned(); }\n")
    _w(tmp_path / "lib/owned.ts",
       'import {orphan} from "./orphan";\n'
       "export function owned() { return orphan(); }\n")
    _w(tmp_path / "lib/orphan.ts", "export function orphan() { return 1; }\n")
    ctx = _ctx(tmp_path)
    route = _feat("docs", ("api/route.ts",))
    owner = _feat("libpkg", ("lib/owned.ts",), sources=["js-library"])
    unattributed = ["lib/orphan.ts"]

    run_membership_closure([route, owner], unattributed, ctx)

    assert "lib/owned.ts" not in route.paths      # not stolen
    assert "lib/orphan.ts" in route.paths          # reached through it
    assert owner.paths == ("lib/owned.ts",)


def test_vendor_and_test_files_never_attach(tmp_path: Path) -> None:
    _w(tmp_path / "api/route.ts",
       'import {t} from "../__tests__/helper";\n'
       "export function GET() { return t(); }\n")
    _w(tmp_path / "__tests__/helper.ts", "export function t() { return 1; }\n")
    ctx = _ctx(tmp_path)
    feat = _feat("docs", ("api/route.ts",))
    unattributed = ["__tests__/helper.ts"]

    res = run_membership_closure([feat], unattributed, ctx)

    assert feat.paths == ("api/route.ts",)
    assert res.unattributed == ["__tests__/helper.ts"]


# ── Fan-in exclusion ─────────────────────────────────────────────────────


def test_fan_in_shared_infra_excluded(tmp_path: Path) -> None:
    """A util imported by 3+ features (= the structural floor, and at
    the top of this tiny repo's own claim distribution) is shared
    infra: provenance recorded, paths untouched, file stays orphan."""
    for i in range(3):
        _w(tmp_path / f"api/r{i}.ts",
           'import {db} from "../lib/db";\n'
           f"export function GET{i}() {{ return db(); }}\n")
    _w(tmp_path / "lib/db.ts", "export function db() { return 1; }\n")
    ctx = _ctx(tmp_path)
    feats = [_feat(f"f{i}", (f"api/r{i}.ts",)) for i in range(3)]
    unattributed = ["lib/db.ts"]

    res = run_membership_closure(feats, unattributed, ctx)

    for f in feats:
        assert "lib/db.ts" not in f.paths
        m = _member(f, "lib/db.ts")
        assert m is not None and m.role == "shared" and not m.primary
    assert res.unattributed == ["lib/db.ts"]
    assert res.telemetry.shared_infra_files == 1
    assert res.telemetry.fan_in_threshold == 3


def test_pairwise_share_attaches_below_fan_in_floor(tmp_path: Path) -> None:
    """Two claimants are below the structural floor (3) — the file
    attaches to exactly one primary; the loser keeps provenance."""
    _w(tmp_path / "api/a.ts",
       'import {u} from "../lib/u";\nexport function A() { return u(); }\n')
    _w(tmp_path / "api/b/deep.ts",
       'import {mid} from "./mid";\nexport function B() { return mid(); }\n')
    _w(tmp_path / "api/b/mid.ts",
       'import {u} from "../../lib/u";\nexport function mid() { return u(); }\n')
    _w(tmp_path / "lib/u.ts", "export function u() { return 1; }\n")
    ctx = _ctx(tmp_path)
    fa = _feat("alpha", ("api/a.ts",))
    fb = _feat("beta", ("api/b/deep.ts",))
    unattributed = ["api/b/mid.ts", "lib/u.ts"]

    res = run_membership_closure([fa, fb], unattributed, ctx)

    # alpha reaches lib/u.ts at depth 1; beta at depth 2 → alpha wins.
    assert "lib/u.ts" in fa.paths
    assert "lib/u.ts" not in fb.paths
    loser = _member(fb, "lib/u.ts")
    assert loser is not None and loser.role == "closure" and not loser.primary
    assert "primary=alpha" in loser.evidence
    # mid.ts is solely beta's.
    assert "api/b/mid.ts" in fb.paths
    assert res.unattributed == []


def test_tie_break_source_priority_then_name(tmp_path: Path) -> None:
    """Equal depth → higher source priority wins; equal priority →
    lexicographically smaller name wins."""
    _w(tmp_path / "a/route.ts",
       'import {u} from "../lib/u";\nexport function A() { return u(); }\n')
    _w(tmp_path / "b/ctrl.ts",
       'import {u} from "../lib/u";\nexport function B() { return u(); }\n')
    _w(tmp_path / "lib/u.ts", "export function u() { return 1; }\n")
    ctx = _ctx(tmp_path)
    # route (priority 4) vs mvc (priority 3), both depth 1.
    route = _feat("zzz-route", ("a/route.ts",), sources=["route"])
    mvc = _feat("aaa-ctrl", ("b/ctrl.ts",), sources=["mvc"])

    run_membership_closure([route, mvc], ["lib/u.ts"], ctx)
    assert "lib/u.ts" in route.paths
    assert "lib/u.ts" not in mvc.paths


# ── Co-commit signal ─────────────────────────────────────────────────────


def test_co_commit_attaches_config_invisible_to_imports(
    tmp_path: Path,
) -> None:
    """A migration-ish file co-committed with one feature's anchors in
    a majority of its commits attaches with role='co-commit'."""
    _w(tmp_path / "api/route.ts", "export function GET() { return 1; }\n")
    _w(tmp_path / "migrations/001.sql", "CREATE TABLE docs (id int);\n")
    commits = [
        _commit("c1", ["api/route.ts", "migrations/001.sql"], days_ago=9),
        _commit("c2", ["api/route.ts", "migrations/001.sql"], days_ago=8),
        _commit("c3", ["api/route.ts", "migrations/001.sql"], days_ago=7),
        _commit("c4", ["migrations/001.sql"], days_ago=6),
        # unrelated noise commits so the P95 size guard has a distribution
        _commit("c5", ["api/route.ts"], days_ago=5),
        _commit("c6", ["api/route.ts"], days_ago=4),
    ]
    ctx = _ctx(tmp_path, commits)
    feat = _feat("docs", ("api/route.ts",))
    unattributed = ["migrations/001.sql"]

    res = run_membership_closure([feat], unattributed, ctx)

    assert "migrations/001.sql" in feat.paths
    m = _member(feat, "migrations/001.sql")
    assert m is not None and m.role == "co-commit" and m.primary
    assert m.confidence <= 0.45          # always below a depth-1 import
    assert res.unattributed == []
    assert res.telemetry.co_commit_attached == 1


def test_co_commit_minority_share_does_not_attach(tmp_path: Path) -> None:
    """2 shared commits out of 6 total for the file (<50%) → no attach."""
    _w(tmp_path / "api/route.ts", "export function GET() { return 1; }\n")
    _w(tmp_path / "conf/settings.ts", "export const s = 1;\n")
    commits = [
        _commit("c1", ["api/route.ts", "conf/settings.ts"]),
        _commit("c2", ["api/route.ts", "conf/settings.ts"]),
        _commit("c3", ["conf/settings.ts"]),
        _commit("c4", ["conf/settings.ts"]),
        _commit("c5", ["conf/settings.ts"]),
        _commit("c6", ["conf/settings.ts"]),
    ]
    ctx = _ctx(tmp_path, commits)
    feat = _feat("docs", ("api/route.ts",))

    res = run_membership_closure([feat], ["conf/settings.ts"], ctx)

    assert "conf/settings.ts" not in feat.paths
    assert res.unattributed == ["conf/settings.ts"]


def test_co_commit_single_shared_commit_below_floor(tmp_path: Path) -> None:
    """One co-commit is no evidence (count floor 2), even at 100% share."""
    _w(tmp_path / "api/route.ts", "export function GET() { return 1; }\n")
    _w(tmp_path / "conf/x.ts", "export const x = 1;\n")
    commits = [
        _commit("c1", ["api/route.ts", "conf/x.ts"]),
        _commit("c2", ["api/route.ts"]),
        _commit("c3", ["api/route.ts"]),
    ]
    ctx = _ctx(tmp_path, commits)
    feat = _feat("docs", ("api/route.ts",))

    res = run_membership_closure([feat], ["conf/x.ts"], ctx)

    assert "conf/x.ts" not in feat.paths
    assert res.unattributed == ["conf/x.ts"]


def test_co_commit_ambiguous_tie_skips(tmp_path: Path) -> None:
    """Identical (share, count) against two features → no attach."""
    _w(tmp_path / "a/route.ts", "export function A() { return 1; }\n")
    _w(tmp_path / "b/route.ts", "export function B() { return 1; }\n")
    _w(tmp_path / "conf/x.ts", "export const x = 1;\n")
    commits = [
        _commit("c1", ["a/route.ts", "b/route.ts", "conf/x.ts"]),
        _commit("c2", ["a/route.ts", "b/route.ts", "conf/x.ts"]),
        _commit("c3", ["a/route.ts"]),
        _commit("c4", ["b/route.ts"]),
    ]
    ctx = _ctx(tmp_path, commits)
    fa = _feat("alpha", ("a/route.ts",))
    fb = _feat("beta", ("b/route.ts",))

    res = run_membership_closure([fa, fb], ["conf/x.ts"], ctx)

    assert "conf/x.ts" not in fa.paths and "conf/x.ts" not in fb.paths
    assert res.unattributed == ["conf/x.ts"]


# ── Residual-pool contract / Stage 3 gate interplay ──────────────────────


def test_fastapi_router_with_few_exports_gets_members(tmp_path: Path) -> None:
    """A FastAPI-shaped router (1 export — under Stage 3's
    MIN_EXPORTS_FOR_FLOW_DETECTION=3) still gets deterministic closure
    membership."""
    _w(tmp_path / "app/routers/teams.py",
       "from app.services.teams import create_team\n\n"
       "def register(router):\n"
       "    router.add_api_route('/teams', create_team)\n")
    _w(tmp_path / "app/services/teams.py",
       "from app.db.session import get_session\n\n"
       "def create_team(payload):\n"
       "    return get_session().add(payload)\n")
    _w(tmp_path / "app/db/session.py",
       "def get_session():\n    return None\n")
    _w(tmp_path / "app/__init__.py", "")
    _w(tmp_path / "app/services/__init__.py", "")
    _w(tmp_path / "app/db/__init__.py", "")
    ctx = _ctx(tmp_path)
    feat = _feat(
        "teams", ("app/routers/teams.py",), sources=["fastapi-route"],
    )
    unattributed = ["app/services/teams.py", "app/db/session.py"]

    res = run_membership_closure([feat], unattributed, ctx)

    assert "app/services/teams.py" in feat.paths
    assert "app/db/session.py" in feat.paths
    assert res.unattributed == []


def test_unattributed_pool_only_shrinks_and_keeps_order(
    tmp_path: Path,
) -> None:
    _w(tmp_path / "api/route.ts",
       'import {s} from "../lib/s";\nexport function GET() { return s(); }\n')
    _w(tmp_path / "lib/s.ts", "export function s() { return 1; }\n")
    _w(tmp_path / "other/loose.ts", "export const loose = 1;\n")
    ctx = _ctx(tmp_path)
    feat = _feat("docs", ("api/route.ts",))
    unattributed = ["other/loose.ts", "lib/s.ts"]

    res = run_membership_closure([feat], unattributed, ctx)

    assert res.unattributed == ["other/loose.ts"]


def test_directory_anchor_paths_seed_files_under_them(tmp_path: Path) -> None:
    _w(tmp_path / "src/controllers/users.ts",
       'import {repo} from "../repo/users";\n'
       "export function index() { return repo(); }\n")
    _w(tmp_path / "src/repo/users.ts", "export function repo() { return 1; }\n")
    ctx = _ctx(tmp_path)
    feat = _feat("users", ("src/controllers",), sources=["mvc"])

    run_membership_closure([feat], ["src/repo/users.ts"], ctx)

    assert "src/repo/users.ts" in feat.paths


# ── Determinism ──────────────────────────────────────────────────────────


def test_determinism_two_runs_identical(tmp_path: Path) -> None:
    _w(tmp_path / "a/route.ts",
       'import {u} from "../lib/u";\nexport function A() { return u(); }\n')
    _w(tmp_path / "b/route.ts",
       'import {u} from "../lib/u";\n'
       'import {v} from "../lib/v";\n'
       "export function B() { return u() + v(); }\n")
    _w(tmp_path / "lib/u.ts", "export function u() { return 1; }\n")
    _w(tmp_path / "lib/v.ts", "export function v() { return 2; }\n")
    commits = [
        _commit("c1", ["a/route.ts", "conf/x.ts"]),
        _commit("c2", ["a/route.ts", "conf/x.ts"]),
        _commit("c3", ["a/route.ts"]),
    ]
    _w(tmp_path / "conf/x.ts", "export const x = 1;\n")

    def _run() -> tuple:
        ctx = _ctx(tmp_path, list(commits))
        fa = _feat("alpha", ("a/route.ts",))
        fb = _feat("beta", ("b/route.ts",))
        res = run_membership_closure(
            [fa, fb], ["lib/u.ts", "lib/v.ts", "conf/x.ts"], ctx,
        )
        return (
            tuple(fa.paths), tuple(fb.paths), tuple(res.unattributed),
            tuple((m.path, m.role, m.primary, m.confidence)
                  for m in fa.member_files),
            tuple((m.path, m.role, m.primary, m.confidence)
                  for m in fb.member_files),
        )

    assert _run() == _run()


def test_anchor_sources_set_matches_review_list() -> None:
    assert ANCHOR_SOURCES == frozenset({
        "route", "fastapi-route", "route-fastify", "route-express",
        "go-router", "django-route", "rails-routes", "mvc",
    })


def test_dir_grained_owned_files_are_reclaimable(tmp_path: Path) -> None:
    """A file covered only by another feature's DIRECTORY path is weak
    ownership (junk-drawer directories swallow whole trees) — the
    closure may claim it; explicitly-listed files stay waypoints.

    Regression for the corpus no-op: on real monorepos nearly every
    file sits under some dir-grained feature, so an orphans-only pool
    yielded candidate_files=0 (documenso/inbox-zero validation).
    """
    _w(tmp_path / "api/route.ts",
       'import {svc} from "../lib/service";\n'
       "export async function POST(req: Request) { return svc(req); }\n")
    _w(tmp_path / "lib/service.ts",
       "export function svc(r: Request) { return r; }\n")
    route = _feat("documents", ("api/route.ts",))
    # Junk-drawer feature claims the whole lib/ DIRECTORY (no exact file).
    drawer = _feat("lib", ("lib",), sources=("js-library",))

    res = run_membership_closure([route, drawer], [], _ctx(tmp_path))

    assert "lib/service.ts" in route.paths
    m = _member(route, "lib/service.ts")
    assert m is not None and m.role == "closure" and m.primary
    assert res.telemetry.reclaimed_dir_grained == 1
    # The drawer keeps its directory entry untouched.
    assert drawer.paths == ("lib",)


def test_majority_claim_drawer_does_not_shield_files(tmp_path: Path) -> None:
    """A feature explicitly listing >= half the repo's files is a junk
    drawer — its explicit claims must not block closure attachment.

    Regression for inbox-zero: a package-anchor feature listed 2,327 of
    2,625 files, so exact-ownership shielded the whole repo and the
    closure attached nothing (candidate_files=0).
    """
    _w(tmp_path / "api/route.ts",
       'import {svc} from "../lib/service";\n'
       "export async function POST(req: Request) { return svc(req); }\n")
    _w(tmp_path / "lib/service.ts",
       "export function svc(r: Request) { return r; }\n")
    _w(tmp_path / "lib/other.ts", "export const x = 1;\n")
    route = _feat("documents", ("api/route.ts",))
    # Drawer explicitly lists 3 of 3 tracked files (>= half the repo).
    drawer = _feat(
        "mega", ("api/route.ts", "lib/service.ts", "lib/other.ts"),
        sources=("package",),
    )

    res = run_membership_closure([route, drawer], [], _ctx(tmp_path))

    assert "lib/service.ts" in route.paths
    m = _member(route, "lib/service.ts")
    assert m is not None and m.role == "closure" and m.primary
    assert res.telemetry.reclaimed_dir_grained == 1


def test_workspace_grained_lister_does_not_shield_files(
    tmp_path: Path,
) -> None:
    """The majority-claim guard applies per WORKSPACE in monorepos.

    Regression for documenso: each workspace-package feature
    (``remix`` = all 513 files of apps/remix, ``lib`` = all 493 of
    packages/lib) sat below the repo-level half mark, so exact
    ownership shielded the whole monorepo and the closure attached
    nothing (candidate_files=0) even after route anchors appeared.
    A feature listing >= half of one workspace's files is
    workspace-grained — claimable; on reclaim it LOSES the file from
    ``paths`` (primary stays exclusive) but keeps non-primary
    provenance.
    """
    _w(tmp_path / "apps/web/app/routes/billing.tsx",
       'import {charge} from "../lib/charge";\n'
       "export default function Billing() { return charge(); }\n")
    _w(tmp_path / "apps/web/app/lib/charge.ts",
       "export function charge() { return 1; }\n")
    _w(tmp_path / "packages/lib/util.ts", "export const u = 1;\n")
    route = _feat("billing", ("apps/web/app/routes/billing.tsx",))
    # Workspace feature explicitly lists ALL files of apps/web
    # (2 of 3 tracked files — BELOW the repo half mark of 1.5? no:
    # 2 >= 1.5, so keep a 4th file to stay below repo-half).
    _w(tmp_path / "packages/lib/extra.ts", "export const e = 1;\n")
    ws_feat = _feat(
        "web",
        ("apps/web/app/routes/billing.tsx", "apps/web/app/lib/charge.ts"),
        sources=["package"],
    )
    ctx = _ctx(tmp_path)
    ctx.monorepo = True
    ctx.workspaces = [
        SimpleNamespace(name="web", path="apps/web", stack="remix"),
        SimpleNamespace(name="lib", path="packages/lib", stack=None),
    ]

    res = run_membership_closure([route, ws_feat], [], ctx)

    assert "apps/web/app/lib/charge.ts" in route.paths
    m = _member(route, "apps/web/app/lib/charge.ts")
    assert m is not None and m.role == "closure" and m.primary
    # Workspace-grained lister loses the reclaimed file from paths…
    assert "apps/web/app/lib/charge.ts" not in ws_feat.paths
    # …but its own anchor file that the route ALSO explicitly lists
    # stays (the route's listing is focused → settled).
    assert "apps/web/app/routes/billing.tsx" in ws_feat.paths
    # Non-primary provenance retained on the loser.
    lm = _member(ws_feat, "apps/web/app/lib/charge.ts")
    assert lm is not None and lm.primary is False
    assert res.telemetry.reclaimed_dir_grained == 1
