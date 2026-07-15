"""B58-v3 Seg A — fdir internal-lib candidacy in the B48 ws-library lane
(FAULTLINE_GRAIN_WAVE, default OFF).

Exhibit classes (B68-census internal-lib rows, verified fdir-anchored on
the key_schema-29 boards):
  * twenty-front ``src/modules/apollo`` — an fdir module NAMED after its
    own external dependency family (@apollo/client) → B48-fdir:name-dep.
  * cal.com ``apps/web/modules/data-table`` — a generically-named,
    broadly-imported, zero-surface fdir module → B48-fdir:library.

SACRED anti-cases (B53-SegB canon + the B48 vetoes at fdir grain):
  * website-with-routes analog: an fdir CONTAINING a route file never
    lanes (route_surface veto) — the cal.com features/event-types
    flowful-neighbor protection at this grain.
  * nav-confirmed fdir: the author's own IA declares a product area.
  * narrow-imported leaf module: breadth bars unchanged (inf>=5,inu>=3).
  * ws-pkg grain is UNTOUCHED: OFF==ON for every ws-pkg verdict
    (zapier-shape integration stays product in both worlds).

Candidates ride the SAME transport_candidates channel — journey
conservation belongs to the 6.985 handoff (all-or-nothing), which
resolves the ``fdir:<unit>`` PF anchor for grain-wave candidates.

Fixtures are neutral mini-monorepos on tmp_path (the SHAPE carries the
signal); synthetic per the fix-cycle law — engine signal is the
authority, the lead's re-scored wave is the gate.
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2.schema_member_strip import GRAIN_WAVE_ENV
from faultline.pipeline_v2.technology_instruments import (
    detect_technology_instruments,
)

ENV = GRAIN_WAVE_ENV


def _write(repo: Path, rel: str, text: str = "") -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel


def _manifest(repo: Path, rel_dir: str, name: str, *,
              deps: dict | None = None, dev_deps: dict | None = None,
              private: bool | None = None) -> str:
    doc: dict = {"name": name}
    if deps:
        doc["dependencies"] = deps
    if dev_deps:
        doc["devDependencies"] = dev_deps
    if private is not None:
        doc["private"] = private
    rel = f"{rel_dir}/package.json" if rel_dir else "package.json"
    return _write(repo, rel, json.dumps(doc))


def _detect(repo, tracked, routes=None, fdirs=(), hubs=(), navs=()):
    return detect_technology_instruments(
        repo, tracked, routes or [], fdir_units=fdirs, hub_dirs=hubs,
        nav_prefixes=navs,
    )


# ── exhibit 1: twenty apollo shape — name-dep fdir module ────────────────

PKG = "packages/appfront"
APOLLO = f"{PKG}/src/modules/apollo"
SIBLINGS = ("records", "settings", "boards")


def _twenty_apollo_repo(repo: Path) -> tuple[list[str], list[str]]:
    """``modules/apollo`` named after @apollo/client (declared by its own
    package), imported by 3 sibling fdir modules (6 files) — the
    twenty-front internal-lib row shape."""
    tracked = [
        _manifest(repo, "", "root", private=True),
        _manifest(repo, PKG, "@acme/appfront",
                  deps={"@apollo/client": "3.0.0", "react": "18"},
                  private=True),
        # the package's OWN app shell outside modules/ (twenty-front has
        # plenty of non-module source — the pkg is never config-only).
        _write(repo, f"{PKG}/src/index.ts",
               'import "./bootstrap";\nexport const App = () => null;\n'),
        _write(repo, f"{PKG}/src/bootstrap.ts",
               "export const boot = () => null;\n"),
        _write(repo, f"{APOLLO}/client.ts",
               "export const client = () => null;\n"),
        _write(repo, f"{APOLLO}/provider.ts",
               'export * from "./client";\n'),
    ]
    fdirs = [APOLLO]
    for sib in SIBLINGS:
        d = f"{PKG}/src/modules/{sib}"
        fdirs.append(d)
        tracked.append(_write(
            repo, f"{d}/store.ts",
            'import { client } from "../apollo/client";\n'))
        tracked.append(_write(
            repo, f"{d}/view.ts",
            'import { client } from "../apollo/client";\n'))
    return tracked, fdirs


def test_exhibit_twenty_apollo_fdir_name_dep(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    tracked, fdirs = _twenty_apollo_repo(tmp_path)
    tele = _detect(tmp_path, tracked, fdirs=fdirs)
    tc = tele.get("transport_candidates") or {}
    assert tc.get(APOLLO) == "B48-fdir:name-dep"
    # candidate, never an instrument dir (mint suppression is the wrong
    # channel — journeys must re-home via the handoff first).
    assert APOLLO not in (tele.get("dirs") or [])


def test_exhibit_twenty_apollo_off_is_noop(tmp_path, monkeypatch):
    """Kill-switch law: unset and =0 both leave the fdir minting — no
    grain-wave key anywhere in the telemetry."""
    for off in (None, "0"):
        if off is None:
            monkeypatch.delenv(ENV, raising=False)
        else:
            monkeypatch.setenv(ENV, off)
        tracked, fdirs = _twenty_apollo_repo(tmp_path)
        tele = _detect(tmp_path, tracked, fdirs=fdirs)
        tc = tele.get("transport_candidates") or {}
        assert APOLLO not in tc
        assert not any("B48-fdir" in str(v) for v in tc.values())


# ── exhibit 2: cal.com data-table shape — library fdir module ────────────

WEB = "apps/web"
DATAKIT = f"{WEB}/modules/datakit"
WEB_SIBS = ("invoices", "customers", "reports")


def _calcom_datakit_repo(repo: Path) -> tuple[list[str], list[str], list]:
    """``apps/web/modules/datakit`` — no dep-name echo, imports nothing
    in-repo (dou=0), imported by 3 sibling feature modules; app routes
    live OUTSIDE the module (zero surface)."""
    tracked = [
        _manifest(repo, "", "root", private=True),
        _manifest(repo, WEB, "@acme/web", deps={"react": "18"},
                  private=True),
        _write(repo, f"{DATAKIT}/table.ts",
               "export const Table = () => null;\n"),
        _write(repo, f"{DATAKIT}/filters.ts",
               'export * from "./table";\n'),
        _write(repo, f"{WEB}/pages/dashboard.tsx",
               "export default function D() { return null; }\n"),
    ]
    fdirs = [DATAKIT]
    for sib in WEB_SIBS:
        d = f"{WEB}/modules/{sib}"
        fdirs.append(d)
        tracked.append(_write(
            repo, f"{d}/list.ts",
            'import { Table } from "../datakit/table";\n'))
        tracked.append(_write(
            repo, f"{d}/detail.ts",
            'import { Table } from "../datakit/table";\n'))
    routes = [{"file": f"{WEB}/pages/dashboard.tsx",
               "pattern": "/dashboard"}]
    return tracked, fdirs, routes


def test_exhibit_calcom_datakit_fdir_library(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    tracked, fdirs, routes = _calcom_datakit_repo(tmp_path)
    tele = _detect(tmp_path, tracked, routes=routes, fdirs=fdirs)
    tc = tele.get("transport_candidates") or {}
    assert tc.get(DATAKIT) == "B48-fdir:library"


def test_anticase_flowful_neighbor_modules_untouched(tmp_path, monkeypatch):
    """The exhibit's SIBLING feature modules (the cal.com features /
    event-types flowful-neighbor analog) never become candidates —
    they are the importers, not the library."""
    monkeypatch.setenv(ENV, "1")
    tracked, fdirs, routes = _calcom_datakit_repo(tmp_path)
    tele = _detect(tmp_path, tracked, routes=routes, fdirs=fdirs)
    tc = tele.get("transport_candidates") or {}
    for sib in WEB_SIBS:
        assert f"{WEB}/modules/{sib}" not in tc


# ── SACRED anti-cases ────────────────────────────────────────────────────


def test_anticase_website_with_routes_fdir_vetoed(tmp_path, monkeypatch):
    """B53-SegB canon at fdir grain: a module CONTAINING a route file is
    a product surface — route_surface veto, never a candidate."""
    monkeypatch.setenv(ENV, "1")
    tracked, fdirs, routes = _calcom_datakit_repo(tmp_path)
    booking = f"{WEB}/modules/booking"
    fdirs = list(fdirs) + [booking]
    tracked.append(_write(tmp_path, f"{booking}/page.tsx",
                          "export default function B() { return null; }\n"))
    for sib in WEB_SIBS:
        tracked.append(_write(
            tmp_path, f"{WEB}/modules/{sib}/book.ts",
            'import B from "../booking/page";\n'))
    routes = list(routes) + [
        {"file": f"{booking}/page.tsx", "pattern": "/booking"},
    ]
    tele = _detect(tmp_path, tracked, routes=routes, fdirs=fdirs)
    tc = tele.get("transport_candidates") or {}
    assert booking not in tc
    # the datakit library itself still lanes — the veto is surgical.
    assert tc.get(DATAKIT) == "B48-fdir:library"


def test_anticase_nav_confirmed_fdir_survives(tmp_path, monkeypatch):
    """The author's own IA (nav-declared prefix) makes the module a
    product area — S3 nav veto."""
    monkeypatch.setenv(ENV, "1")
    tracked, fdirs, routes = _calcom_datakit_repo(tmp_path)
    tele = _detect(tmp_path, tracked, routes=routes, fdirs=fdirs,
                   navs=[DATAKIT])
    tc = tele.get("transport_candidates") or {}
    assert DATAKIT not in tc


def test_anticase_leaf_module_below_breadth_survives(tmp_path, monkeypatch):
    """A feature module imported by ONE sibling (inu<3) is a leaf
    feature, not an internal library — breadth bars unchanged."""
    monkeypatch.setenv(ENV, "1")
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, WEB, "@acme/web", private=True),
        _write(tmp_path, f"{DATAKIT}/table.ts",
               "export const Table = () => null;\n"),
        _write(tmp_path, f"{WEB}/modules/invoices/list.ts",
               'import { Table } from "../datakit/table";\n'),
    ]
    fdirs = [DATAKIT, f"{WEB}/modules/invoices"]
    tele = _detect(tmp_path, tracked, fdirs=fdirs)
    tc = tele.get("transport_candidates") or {}
    assert DATAKIT not in tc


def test_anticase_ws_pkg_grain_off_on_identical(tmp_path, monkeypatch):
    """The extension NEVER changes ws-pkg verdicts: a zapier-shape
    integration SDK (zero in-repo importers — twenty-zapier canon) and a
    ws-pkg library lane identically OFF and ON."""
    def scene(repo: Path) -> list[str]:
        tracked = [
            _manifest(repo, "", "root", private=True),
            # zapier-shape: nothing in-repo imports an integration SDK.
            _manifest(repo, "packages/zapkit", "zapkit",
                      deps={"zapier-platform-core": "15"}),
            _write(repo, "packages/zapkit/src/index.ts",
                   "export const trigger = 1;\n"),
            # a ws-pkg library the EXISTING B48 lanes (both worlds).
            _manifest(repo, "packages/widgetkit", "widgetkit"),
            _write(repo, "packages/widgetkit/src/button.ts",
                   "export const Button = () => null;\n"),
        ]
        for unit, files in (("web", ("a", "b", "c")),
                            ("admin", ("x",)), ("mobile", ("y",))):
            tracked.append(_manifest(repo, f"apps/{unit}",
                                     f"@acme/{unit}", private=True))
            for fn in files:
                tracked.append(_write(
                    repo, f"apps/{unit}/src/{fn}.ts",
                    'import { Button } from "widgetkit";\n'))
        return tracked

    monkeypatch.delenv(ENV, raising=False)
    off_repo = tmp_path / "off"
    tele_off = _detect(off_repo, scene(off_repo))
    monkeypatch.setenv(ENV, "1")
    on_repo = tmp_path / "on"
    tele_on = _detect(on_repo, scene(on_repo))

    assert (tele_off.get("transport_candidates")
            == tele_on.get("transport_candidates"))
    assert (tele_off.get("instruments") == tele_on.get("instruments"))
    tc = tele_on.get("transport_candidates") or {}
    assert "packages/zapkit" not in tc            # SACRED: integration
    assert tc.get("packages/widgetkit") == "B48:library"  # unchanged sig


# ── satellite double-mark guard ──────────────────────────────────────────


def test_candidate_fdir_never_also_satellite(tmp_path, monkeypatch):
    """An fdir that is BOTH a b48 candidate and a name-match importer of
    an instrument must ride the handoff channel only — not the
    instrument-dirs channel (mint suppression + lane would conflict)."""
    monkeypatch.setenv(ENV, "1")
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        # S1b instrument: the ORM package (schema.prisma inside).
        _manifest(tmp_path, "packages/ormkit", "@acme/ormkit",
                  deps={"@prisma/client": "5"}, dev_deps={"prisma": "5"}),
        _write(tmp_path, "packages/ormkit/schema.prisma", "model A {}\n"),
        _write(tmp_path, "packages/ormkit/index.ts", "export const db=1;\n"),
        _manifest(tmp_path, WEB, "@acme/web", private=True),
        # the fdir shares the instrument's name AND imports it.
        _write(tmp_path, f"{WEB}/modules/ormkit/helpers.ts",
               'import { db } from "@acme/ormkit";\n'),
    ]
    fdirs = [f"{WEB}/modules/ormkit"]
    for sib in WEB_SIBS:
        d = f"{WEB}/modules/{sib}"
        fdirs.append(d)
        tracked.append(_write(
            tmp_path, f"{d}/q.ts",
            'import { h } from "../ormkit/helpers";\n'))
        tracked.append(_write(
            tmp_path, f"{d}/w.ts",
            'import { h } from "../ormkit/helpers";\n'))
    tele = _detect(tmp_path, tracked, fdirs=fdirs)
    tc = tele.get("transport_candidates") or {}
    fdir = f"{WEB}/modules/ormkit"
    assert fdir in tc
    assert fdir not in (tele.get("satellites") or {})
    assert fdir not in (tele.get("dirs") or [])


# ── 6.985 handoff: fdir anchor resolution ────────────────────────────────


def _handoff_scene(unit: str):
    """Minimal handoff scene: one fdir-anchored candidate PF + one dev."""
    from datetime import datetime, timezone

    class Dev:
        def __init__(self, name, pfid, paths):
            self.name = name
            self.uuid = f"dev-{name}"
            self.layer = "developer"
            self.product_feature_id = pfid
            self.paths = list(paths)
            self.member_files = []
            self.flows = []
            self.shared_reason = None
            self.anchor_id = None
            self.authors = []
            self.total_commits = 0
            self.bug_fixes = 0
            self.coverage_pct = None
            self.last_modified = datetime.fromtimestamp(0, timezone.utc)
            self.health_score = 0.0

    class PF:
        def __init__(self, name, anchor_id):
            self.name = name
            self.uuid = f"pf-{name}"
            self.layer = "product"
            self.anchor_id = anchor_id

    class Ctx:
        repo_path = "."
        tracked_files = []

    devs = [Dev("datakit", "datakit", [f"{unit}/table.ts"])]
    pfs = [PF("datakit", f"fdir:{unit}"),
           PF("invoices", "route:apps/web/modules/invoices")]
    return devs, pfs, Ctx()


def test_handoff_resolves_fdir_anchor_on(monkeypatch):
    """ON: a grain-wave fdir candidate's ``fdir:<unit>`` PF resolves in
    candidate_pfs — the lane machinery downstream is anchor-shape
    agnostic (the existing B22 suite owns it)."""
    from faultline.pipeline_v2.transport_handoff import (
        TargetGrainIndex,
        run_transport_handoff,
    )
    monkeypatch.setenv(ENV, "1")
    devs, pfs, ctx = _handoff_scene(DATAKIT)
    grain = TargetGrainIndex([], pfs, routes_index=[],
                             excluded_units=[DATAKIT],
                             candidate_pf_keys={"datakit"})
    tele = run_transport_handoff(
        devs, pfs, [], [], [], ctx,
        {DATAKIT: "B48-fdir:library"},
        grain_index=grain,
    )
    assert tele["candidate_pfs"] == {DATAKIT: "datakit"}


def test_handoff_ignores_fdir_anchor_off(monkeypatch):
    """OFF: the ws:-only contract holds — the fdir candidate does not
    resolve, the handoff stays inert (byte-identical world)."""
    from faultline.pipeline_v2.transport_handoff import (
        TargetGrainIndex,
        run_transport_handoff,
    )
    monkeypatch.delenv(ENV, raising=False)
    devs, pfs, ctx = _handoff_scene(DATAKIT)
    grain = TargetGrainIndex([], pfs, routes_index=[],
                             excluded_units=[DATAKIT],
                             candidate_pf_keys={"datakit"})
    tele = run_transport_handoff(
        devs, pfs, [], [], [], ctx,
        {DATAKIT: "B48-fdir:library"},
        grain_index=grain,
    )
    assert tele["candidate_pfs"] == {}
    assert tele["laned"] == []


# ── iter-2 MODE 2: twin-unit resolution (typebot variables class) ────────


def _twin_scene(pf_anchor: str, pf_name: str = "variables",
                extra_pfs=()):
    from datetime import datetime, timezone

    class Dev:
        def __init__(self, name, pfid, paths):
            self.name = name
            self.uuid = f"dev-{name}"
            self.layer = "developer"
            self.product_feature_id = pfid
            self.paths = list(paths)
            self.member_files = []
            self.flows = []
            self.shared_reason = None
            self.anchor_id = None
            self.authors = []
            self.total_commits = 0
            self.bug_fixes = 0
            self.coverage_pct = None
            self.last_modified = datetime.fromtimestamp(0, timezone.utc)
            self.health_score = 0.0

    class PF:
        def __init__(self, name, anchor_id):
            self.name = name
            self.uuid = f"pf-{name}"
            self.layer = "product"
            self.anchor_id = anchor_id

    class Ctx:
        repo_path = "."
        tracked_files = []

    unit = "packages/variables"
    devs = [Dev("variables-app", pf_name,
                ["apps/builder/src/features/variables/store.ts"])]
    pfs = [PF(pf_name, pf_anchor)] + [PF(n, a) for n, a in extra_pfs]
    return unit, devs, pfs, Ctx()


def _run_twin(unit, devs, pfs, ctx, routes=None, flag="1", monkeypatch=None):
    from faultline.pipeline_v2.transport_handoff import (
        TargetGrainIndex,
        run_transport_handoff,
    )
    if flag is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, flag)
    grain = TargetGrainIndex([], pfs, routes_index=routes or [],
                             excluded_units=[unit],
                             candidate_pf_keys=set())
    return run_transport_handoff(
        devs, pfs, [], [], routes or [], ctx,
        {unit: "B48:library"},
        grain_index=grain,
    )


def test_exhibit_typebot_variables_twin_resolves(monkeypatch):
    """THE MODE-2 exhibit: candidate packages/variables has no own PF;
    the row's PF is the app-side twin fdir:apps/builder/src/features/
    variables — the twin resolves, the lane machinery engages
    (PF-scoped downstream)."""
    unit, devs, pfs, ctx = _twin_scene(
        "fdir:apps/builder/src/features/variables")
    tele = _run_twin(unit, devs, pfs, ctx, monkeypatch=monkeypatch)
    assert tele["candidate_pfs"] == {unit: "variables"}
    assert tele["twin_resolutions"] == {
        unit: "fdir-twin:apps/builder/src/features/variables"}


def test_twin_route_anchored_pf_refused(monkeypatch):
    """typebot theme/settings shape: a route:-anchored same-name PF is
    a REAL product surface — never a twin lane target."""
    unit, devs, pfs, ctx = _twin_scene("route:variables")
    tele = _run_twin(unit, devs, pfs, ctx, monkeypatch=monkeypatch)
    assert tele["candidate_pfs"] == {}


def test_twin_with_route_file_inside_refused(monkeypatch):
    """website-with-routes analog: an fdir twin whose dir carries a
    route file is a surface — refused."""
    unit, devs, pfs, ctx = _twin_scene(
        "fdir:apps/builder/src/features/variables")
    routes = [{"file": "apps/builder/src/features/variables/page.tsx",
               "pattern": "/variables"}]
    tele = _run_twin(unit, devs, pfs, ctx, routes=routes,
                     monkeypatch=monkeypatch)
    assert tele["candidate_pfs"] == {}


def test_twin_ambiguous_refused(monkeypatch):
    """TWO same-basename fdir PFs — ambiguous, honest abstain."""
    unit, devs, pfs, ctx = _twin_scene(
        "fdir:apps/builder/src/features/variables",
        extra_pfs=[("variables-2", "fdir:apps/viewer/src/variables")],
    )
    tele = _run_twin(unit, devs, pfs, ctx, monkeypatch=monkeypatch)
    assert tele["candidate_pfs"] == {}


def test_twin_off_world_inert(monkeypatch):
    """Kill-switch: OFF — no twin resolution, candidate stays
    unresolved (byte-identical world)."""
    unit, devs, pfs, ctx = _twin_scene(
        "fdir:apps/builder/src/features/variables")
    tele = _run_twin(unit, devs, pfs, ctx, flag=None,
                     monkeypatch=monkeypatch)
    assert tele["candidate_pfs"] == {}
    assert "twin_resolutions" not in tele


# ── iter-2 MODE 1: abstain telemetry + breadth/root/subpath fixes ────────


def test_abstain_telemetry_records_breadth_and_norung(tmp_path, monkeypatch):
    """Census adjudicability: every considered-but-abstained unit
    carries its exact gate in b48_abstains (the wave's 15/16 silent
    abstains were un-diagnosable without it)."""
    monkeypatch.setenv(ENV, "1")
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, WEB, "@acme/web", private=True),
        # narrow leaf: 1 importer file / 1 unit → breadth abstain.
        _write(tmp_path, f"{DATAKIT}/table.ts",
               "export const Table = () => null;\n"),
        _write(tmp_path, f"{WEB}/modules/invoices/list.ts",
               'import { Table } from "../datakit/table";\n'),
    ]
    fdirs = [DATAKIT, f"{WEB}/modules/invoices"]
    tele = _detect(tmp_path, tracked, fdirs=fdirs)
    ab = tele.get("b48_abstains") or {}
    assert ab.get(DATAKIT, "").startswith("breadth:inf=1,inu=1")


def test_abstain_telemetry_absent_when_off(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    tracked, fdirs, routes = _calcom_datakit_repo(tmp_path)
    tele = _detect(tmp_path, tracked, routes=routes, fdirs=fdirs)
    assert "b48_abstains" not in tele


def test_exhibit_midday_bot_two_unit_breadth(tmp_path, monkeypatch):
    """midday packages/bot class (inf=12, inu=2): a zero-surface lib
    heavily consumed by TWO units lanes under the grain breadth
    (inu>=2); OFF keeps the ws bar (inu>=3) — byte-identical."""
    def scene(repo: Path) -> list[str]:
        tracked = [
            _manifest(repo, "", "root", private=True),
            _manifest(repo, "packages/botkit", "botkit"),
            _write(repo, "packages/botkit/src/bot.ts",
                   "export const bot = () => null;\n"),
        ]
        for unit, files in (("dashboard", ("a", "b", "c")),
                            ("api", ("x", "y", "z"))):
            tracked.append(_manifest(repo, f"apps/{unit}",
                                     f"@acme/{unit}", private=True))
            for fn in files:
                tracked.append(_write(
                    repo, f"apps/{unit}/src/{fn}.ts",
                    'import { bot } from "botkit";\n'))
        return tracked

    monkeypatch.setenv(ENV, "1")
    on_repo = tmp_path / "on"
    tele_on = _detect(on_repo, scene(on_repo))
    assert (tele_on.get("transport_candidates") or {}).get(
        "packages/botkit") == "B48:library"

    monkeypatch.delenv(ENV, raising=False)
    off_repo = tmp_path / "off"
    tele_off = _detect(off_repo, scene(off_repo))
    assert "packages/botkit" not in (
        tele_off.get("transport_candidates") or {})


def test_anticase_single_unit_consumer_never_lanes(tmp_path, monkeypatch):
    """rr packages/import class (inf=1, inu=1): a narrow lib stays —
    the inf>=5 file bar and the 2-unit floor both hold even ON."""
    monkeypatch.setenv(ENV, "1")
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/importkit", "importkit"),
        _write(tmp_path, "packages/importkit/src/parse.ts",
               "export const parse = () => null;\n"),
        _manifest(tmp_path, "apps/server", "@acme/server", private=True),
    ] + [
        _write(tmp_path, f"apps/server/src/{fn}.ts",
               'import { parse } from "importkit";\n')
        for fn in ("a", "b", "c", "d", "e", "f")
    ]
    tele = _detect(tmp_path, tracked)
    assert "packages/importkit" not in (
        tele.get("transport_candidates") or {})
    ab = tele.get("b48_abstains") or {}
    assert ab.get("packages/importkit", "").startswith("breadth:inf=6,inu=1")


def test_exhibit_novu_ee_root_container(tmp_path, monkeypatch):
    """novu ee-auth/ee-billing class: enterprise/packages/<pkg> is a
    legal grain candidate root (parallel-EE convention); OFF it stays
    not_shared_container-vetoed."""
    def scene(repo: Path) -> list[str]:
        tracked = [
            _manifest(repo, "", "root", private=True),
            _manifest(repo, "enterprise/packages/authkit",
                      "@acme/ee-authkit", private=True),
            _write(repo, "enterprise/packages/authkit/src/index.ts",
                   "export const auth = () => null;\n"),
        ]
        for unit, files in (("web", ("a", "b", "c")),
                            ("admin", ("x",)), ("worker", ("y",))):
            tracked.append(_manifest(repo, f"apps/{unit}",
                                     f"@acme/{unit}", private=True))
            for fn in files:
                tracked.append(_write(
                    repo, f"apps/{unit}/src/{fn}.ts",
                    'import { auth } from "@acme/ee-authkit";\n'))
        return tracked

    monkeypatch.setenv(ENV, "1")
    on_repo = tmp_path / "on"
    tele_on = _detect(on_repo, scene(on_repo))
    assert (tele_on.get("transport_candidates") or {}).get(
        "enterprise/packages/authkit") == "B48:library"

    monkeypatch.delenv(ENV, raising=False)
    off_repo = tmp_path / "off"
    tele_off = _detect(off_repo, scene(off_repo))
    assert "enterprise/packages/authkit" not in (
        tele_off.get("transport_candidates") or {})


def test_anticase_ee_root_with_routes_vetoed(tmp_path, monkeypatch):
    """An EE package carrying a route file is a surface — vetoed even
    under the grain roots."""
    monkeypatch.setenv(ENV, "1")
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "enterprise/packages/portal",
                  "@acme/ee-portal", private=True),
        _write(tmp_path, "enterprise/packages/portal/pages/index.tsx",
               "export default function P() { return null; }\n"),
    ]
    for unit, files in (("web", ("a", "b", "c")),
                        ("admin", ("x",)), ("worker", ("y",))):
        tracked.append(_manifest(tmp_path, f"apps/{unit}",
                                 f"@acme/{unit}", private=True))
        for fn in files:
            tracked.append(_write(
                tmp_path, f"apps/{unit}/src/{fn}.ts",
                'import P from "@acme/ee-portal";\n'))
    routes = [{"file": "enterprise/packages/portal/pages/index.tsx",
               "pattern": "/portal"}]
    tele = _detect(tmp_path, tracked, routes=routes)
    assert "enterprise/packages/portal" not in (
        tele.get("transport_candidates") or {})
    ab = tele.get("b48_abstains") or {}
    assert ab.get("enterprise/packages/portal") == "veto:route_surface"


def test_exhibit_calcom_pbac_subpath_deepening(tmp_path, monkeypatch):
    """cal.com packages/features/pbac class: consumers import ONLY via
    the parent's name channel (@acme/features/pbac/...) — OFF the
    credit lands on packages/features (fdir measures inf=0); ON the
    subpath deepens to the pbac fdir, which lanes as a library."""
    def scene(repo: Path) -> tuple[list[str], list[str]]:
        tracked = [
            _manifest(repo, "", "root", private=True),
            _manifest(repo, "packages/features", "@acme/features",
                      private=True),
            # the features package's own shell (never config-only).
            _write(repo, "packages/features/index.ts",
                   'export * from "./pbac/service";\n'),
            _write(repo, "packages/features/shell.ts",
                   "export const shell = 1;\n"),
            _write(repo, "packages/features/pbac/service.ts",
                   "export const check = () => null;\n"),
            _write(repo, "packages/features/pbac/registry.ts",
                   'export * from "./service";\n'),
        ]
        fdirs = ["packages/features/pbac"]
        for unit, files in (("web", ("a", "b", "c")),
                            ("admin", ("x",)), ("worker", ("y",))):
            tracked.append(_manifest(repo, f"apps/{unit}",
                                     f"@acme/{unit}", private=True))
            for fn in files:
                tracked.append(_write(
                    repo, f"apps/{unit}/src/{fn}.ts",
                    'import { check } from '
                    '"@acme/features/pbac/service";\n'))
        return tracked, fdirs

    monkeypatch.setenv(ENV, "1")
    on_repo = tmp_path / "on"
    tracked, fdirs = scene(on_repo)
    tele_on = _detect(on_repo, tracked, fdirs=fdirs)
    assert (tele_on.get("transport_candidates") or {}).get(
        "packages/features/pbac") == "B48-fdir:library"

    monkeypatch.delenv(ENV, raising=False)
    off_repo = tmp_path / "off"
    tracked, fdirs = scene(off_repo)
    tele_off = _detect(off_repo, tracked, fdirs=fdirs)
    assert "packages/features/pbac" not in (
        tele_off.get("transport_candidates") or {})


def test_anticase_library_cluster_honest_abstain(tmp_path, monkeypatch):
    """twenty metadata-store / cal.com data-table class: a module
    importing >1 sibling PRODUCT-standing units keeps the fan-out guard
    (dou>1) — recorded as no_rung in the abstain channel, never forced.
    The cluster-SCC question is a doctrine decision, not a rung."""
    monkeypatch.setenv(ENV, "1")
    tracked, fdirs, routes = _calcom_datakit_repo(tmp_path)
    # datakit now imports TWO sibling modules → dou=2 → no rung.
    tracked.append(_write(
        tmp_path, f"{DATAKIT}/glue.ts",
        'import { a } from "../invoices/list";\n'
        'import { b } from "../customers/list";\n'))
    tele = _detect(tmp_path, tracked, routes=routes, fdirs=fdirs)
    assert DATAKIT not in (tele.get("transport_candidates") or {})
    ab = tele.get("b48_abstains") or {}
    assert ab.get(DATAKIT, "").startswith("no_rung:dou=2")
