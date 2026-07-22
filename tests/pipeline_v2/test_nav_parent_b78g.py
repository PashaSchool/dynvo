"""B78 Seg G — nav-parent display grouping (FAULTLINE_NAV_PARENT).

Named exhibits + anti-cases (fixb78 §Seg G + forensics-canon):
  1. Soc0-fixture — a PF anchored at the module-registry page ``/detectors/
     overview`` receives ``nav_parent`` {parent_label='Detector Studio',
     parent_id='detector-studio', source_file, line} via the ROUTE channel.
  2. twenty-fixture — the SettingsPath-referenced nav (billing / members /
     objects) homes under 'Workspace' via the SLUG channel; ``objects`` +
     ``data-model`` are TWO PFs on ONE nav position — a duplicate VISIBLE
     in ``nav_tree.duplicates``.
  3. Anti-case openstatus — a repo with no nav registry: ``run_nav_parent``
     returns None, every PF's ``nav_parent`` stays None (byte-ident).
  4. Anti-case config-not-consumed — a ``{path, size}`` array (no label /
     icon), and any array in a non-nav file, is never a nav category.
  5. Flag default OFF ⇒ inert (None, no field).
  6. Determinism ⇒ two runs identical.
  7. DISPLAY-only ⇒ the product-feature list (identity / membership /
     order) is untouched; nothing is merged, moved, or minted.
  8. Live category with no PF ⇒ nav_tree.unrepresented (never minted).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.nav_parent import (
    NAV_PARENT_ENV,
    build_nav_tree,
    nav_parent_enabled,
    run_nav_parent,
)


# ── stubs ────────────────────────────────────────────────────────────────────


class MF:
    def __init__(self, path: str, role: str = "anchor") -> None:
        self.path = path
        self.role = role


class PF:
    def __init__(self, name, *, paths=(), anchors=(), display_name=None,
                 layer="product"):
        self.name = name
        self.display_name = display_name
        self.layer = layer
        self.paths = list(paths)
        self.member_files = [MF(a, "anchor") for a in anchors]
        self.nav_parent = None


class UF:
    def __init__(self, pfid, routes=()):
        self.product_feature_id = pfid
        self.routes = list(routes)


class Ctx:
    def __init__(self, repo_path: Path, tracked: list[str]) -> None:
        self.repo_path = str(repo_path)
        self.tracked_files = tracked


def _write(root: Path, rel: str, body: str) -> str:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body, encoding="utf-8")
    return rel


@pytest.fixture
def nav_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NAV_PARENT_ENV, "1")


# ── fixtures: nav registries modeled faithfully on the real repos ────────────

_MODULE_REGISTRY = """\
import { AlarmSmoke, LayoutDashboard } from 'icons';

interface ModulePage { key: string; labelKey: string; path: string; icon: unknown; }
interface ModuleDefinition {
  id: string; nameKey: string; icon: unknown; pages: ModulePage[];
}

const DETECTOR_STUDIO: ModuleDefinition = {
  id: 'detector-studio',
  nameKey: 'modules.detectorStudio.name',
  icon: AlarmSmoke,
  pages: [
    { key: 'ds-overview', labelKey: 'modules.detectorStudio.pages.overview', path: '/detectors/overview', icon: LayoutDashboard },
    { key: 'ds-cases', labelKey: 'modules.detectorStudio.pages.cases', path: '/cases', icon: AlarmSmoke },
    { key: 'ds-findings', labelKey: 'modules.detectorStudio.pages.findings', path: '/findings', icon: AlarmSmoke },
  ],
};

const NETWORK_SECURITY: ModuleDefinition = {
  id: 'network-security',
  nameKey: 'modules.networkSecurity.name',
  icon: LayoutDashboard,
  pages: [
    { key: 'ns-overview', labelKey: 'modules.networkSecurity.pages.overview', path: '/network-security/overview', icon: LayoutDashboard },
    { key: 'ns-graph', labelKey: 'modules.networkSecurity.pages.graph', path: '/network-security/graph', icon: LayoutDashboard },
  ],
};

export const MODULES: readonly ModuleDefinition[] = [DETECTOR_STUDIO, NETWORK_SECURITY] as const;
"""

_SETTINGS_NAV = """\
import { SettingsPath } from 'shared/types';
import { t } from '@lingui/core/macro';

export type SettingsNavigationSection = { label: string; items: SettingsNavigationItem[]; };

const useSettingsNavigationItems = (): SettingsNavigationSection[] => {
  return [
    {
      label: t`Workspace`,
      items: [
        { label: t`General`, path: SettingsPath.General, Icon: IconSettings },
        { label: t`Data model`, path: SettingsPath.Objects, Icon: IconHierarchy2 },
        { label: t`Members`, path: SettingsPath.WorkspaceMembersPage, Icon: IconUsers },
        { label: t`Billing`, path: SettingsPath.Billing, Icon: IconCurrencyDollar },
      ],
    },
    {
      label: t`Other`,
      items: [
        { label: t`Releases`, path: SettingsPath.Releases, Icon: IconTag },
      ],
    },
  ];
};
"""


def _soc_repo(tmp: Path) -> Ctx:
    rel = _write(tmp, "frontend/src/lib/module-registry.ts", _MODULE_REGISTRY)
    return Ctx(tmp, [rel, "package.json"])


def _twenty_repo(tmp: Path) -> Ctx:
    rel = _write(
        tmp,
        "packages/twenty-front/src/modules/settings/hooks/"
        "useSettingsNavigationItems.tsx",
        _SETTINGS_NAV,
    )
    return Ctx(tmp, [rel, "package.json"])


# ── 5. flag default OFF ──────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(NAV_PARENT_ENV, raising=False)
    assert nav_parent_enabled() is False


def test_off_is_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(NAV_PARENT_ENV, raising=False)
    ctx = _soc_repo(tmp_path)
    pf = PF("detectors-overview", anchors=["frontend/src/pages/detectors/overview.tsx"])
    ri = [{"file": "frontend/src/pages/detectors/overview.tsx",
           "pattern": "/detectors/overview"}]
    assert run_nav_parent([pf], ri, ctx) is None
    assert pf.nav_parent is None  # no field written


# ── 1. Soc0-fixture: detection-studio via ROUTE channel ──────────────────────


def test_soc0_detector_studio_route_match(tmp_path: Path, nav_on) -> None:
    ctx = _soc_repo(tmp_path)
    pf = PF("detectors-overview",
            anchors=["frontend/src/pages/detectors/overview.tsx"])
    ri = [{"file": "frontend/src/pages/detectors/overview.tsx",
           "pattern": "/detectors/overview"}]
    tele = run_nav_parent([pf], ri, ctx)
    assert tele is not None
    assert pf.nav_parent is not None
    assert pf.nav_parent["parent_label"] == "Detector Studio"
    assert pf.nav_parent["parent_id"] == "detector-studio"
    assert pf.nav_parent["via"] == "route"
    # file:line points at the CATEGORY declaration (module-registry.ts).
    assert pf.nav_parent["source_file"].endswith("module-registry.ts")
    assert isinstance(pf.nav_parent["line"], int) and pf.nav_parent["line"] > 0
    assert tele["matched_via"]["route"] >= 1


# ── 8. live category with no PF ⇒ unrepresented (never minted) ────────────────


def test_unrepresented_category(tmp_path: Path, nav_on) -> None:
    ctx = _soc_repo(tmp_path)
    # A PF only for Detector Studio — Network Security stays unrepresented.
    pf = PF("detectors-overview",
            anchors=["frontend/src/pages/detectors/overview.tsx"])
    ri = [{"file": "frontend/src/pages/detectors/overview.tsx",
           "pattern": "/detectors/overview"}]
    before = len([pf])
    tele = run_nav_parent([pf], ri, ctx)
    assert tele is not None
    labels = {u["label"] for u in tele["unrepresented"]}
    assert "Network Security" in labels
    assert before == 1  # no PF minted for the unrepresented category


# ── 2. twenty-fixture: SLUG channel + objects/data-model duplicate ───────────


def test_twenty_slug_and_duplicate(tmp_path: Path, nav_on) -> None:
    ctx = _twenty_repo(tmp_path)
    pfs = [
        PF("billing"),
        PF("members"),
        PF("objects"),
        PF("data-model"),
    ]
    tele = run_nav_parent(pfs, [], ctx)
    assert tele is not None
    by_name = {p.name: p for p in pfs}
    for name in ("billing", "members", "objects", "data-model"):
        assert by_name[name].nav_parent is not None, name
        assert by_name[name].nav_parent["parent_label"] == "Workspace", name
        assert by_name[name].nav_parent["via"] == "slug", name
    # objects + data-model land on the SAME nav position ("Data model").
    dups = tele["duplicates"]
    assert dups, "expected a visible duplicate nav position"
    dup_pf_sets = [set(d["pfs"]) for d in dups]
    assert {"objects", "data-model"} in dup_pf_sets


# ── 3. anti-case openstatus: no nav registry ⇒ byte-ident inert ──────────────


def test_anti_case_no_nav_config(tmp_path: Path, nav_on) -> None:
    # openstatus-shape: real code files, but NO nav registry anywhere.
    _write(tmp_path, "app/(dashboard)/monitors/page.tsx", "export default function P(){}")
    _write(tmp_path, "packages/db/schema.ts", "export const monitor = {};")
    ctx = Ctx(tmp_path, [
        "app/(dashboard)/monitors/page.tsx", "packages/db/schema.ts",
    ])
    pfs = [PF("monitors", anchors=["app/(dashboard)/monitors/page.tsx"]),
           PF("status-pages")]
    assert build_nav_tree(ctx) == []
    assert run_nav_parent(pfs, [], ctx) is None
    assert all(p.nav_parent is None for p in pfs)


# ── 4. anti-case: config arrays not consumed by navigation ───────────────────


def test_anti_case_config_array_not_nav(tmp_path: Path, nav_on) -> None:
    # (a) a {path,size} data array in a NON-nav file: never read.
    _write(tmp_path, "src/data/seed.ts",
           "export const SEED = [{ path: '/a', size: 5 }, { path: '/b', size: 9 }];")
    # (b) an array of bare {path} rows (no label/icon) inside a nav-named
    #     file: not a category (fails the rendered-by-nav item gate).
    _write(tmp_path, "src/lib/nav-extras.ts",
           "export const RAW = [{ path: '/x' }, { path: '/y' }, { path: '/z' }];")
    ctx = Ctx(tmp_path, ["src/data/seed.ts", "src/lib/nav-extras.ts"])
    assert build_nav_tree(ctx) == []
    pf = PF("a-thing", anchors=["src/data/seed.ts"])
    assert run_nav_parent([pf], [], ctx) is None


# ── 6. determinism ───────────────────────────────────────────────────────────


def test_determinism(tmp_path: Path, nav_on) -> None:
    ctx = _twenty_repo(tmp_path)
    pfs1 = [PF("billing"), PF("members"), PF("objects"), PF("data-model")]
    pfs2 = [PF("billing"), PF("members"), PF("objects"), PF("data-model")]
    t1 = run_nav_parent(pfs1, [], ctx)
    t2 = run_nav_parent(pfs2, [], ctx)
    assert t1 == t2
    assert [p.nav_parent for p in pfs1] == [p.nav_parent for p in pfs2]


# ── 7. display-only: PF identity / membership / order untouched ──────────────


def test_display_only_no_structure_change(tmp_path: Path, nav_on) -> None:
    ctx = _soc_repo(tmp_path)
    pf = PF("detectors-overview",
            anchors=["frontend/src/pages/detectors/overview.tsx"],
            paths=["frontend/src/pages/detectors/overview.tsx"])
    pfs = [pf]
    names_before = [p.name for p in pfs]
    members_before = list(pf.member_files)
    paths_before = list(pf.paths)
    ri = [{"file": "frontend/src/pages/detectors/overview.tsx",
           "pattern": "/detectors/overview"}]
    run_nav_parent(pfs, ri, ctx)
    assert [p.name for p in pfs] == names_before          # order + identity
    assert pf.member_files == members_before              # membership
    assert pf.paths == paths_before                       # paths
    # ONLY nav_parent was added.
    assert pf.nav_parent is not None


# ── route beats slug; UF-route enrichment ────────────────────────────────────


def test_route_beats_slug(tmp_path: Path, nav_on) -> None:
    ctx = _soc_repo(tmp_path)
    # PF named 'cases' (would slug-match the 'Cases' sub-item) AND anchored
    # at /findings — the route wins over the slug.
    pf = PF("cases", anchors=["frontend/src/pages/findings.tsx"])
    ri = [{"file": "frontend/src/pages/findings.tsx", "pattern": "/findings"}]
    run_nav_parent([pf], ri, ctx)
    assert pf.nav_parent is not None
    assert pf.nav_parent["via"] == "route"


def test_uf_routes_enrich_match(tmp_path: Path, nav_on) -> None:
    ctx = _soc_repo(tmp_path)
    pf = PF("ns-graph")  # no anchor routes on the PF itself
    ufs = [UF("ns-graph", routes=["/network-security/graph"])]
    tele = run_nav_parent([pf], [], ctx, user_flows=ufs)
    assert tele is not None
    assert pf.nav_parent is not None
    assert pf.nav_parent["parent_label"] == "Network Security"
    assert pf.nav_parent["via"] == "route"
