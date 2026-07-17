"""B65-v3 — client-side SPA router extractor unit pack.

Covers the mechanism (2 segments) + the SACRED anti-cases (spec §"SACRED
анти-кейси"):
  * flag default OFF (kill-switch) + byte-identical inert when unset, AND
    unregistered at the registry surface (extractor_hits key parity — the
    B67 lesson);
  * Seg A vue file-based pages — the hoppscotch pages set POIMENNO
    (live-repo census 2026-07-16: packages/hoppscotch-common/src/pages =
    20 .vue files incl. nuxt-style ``_id`` dynamics + realtime/ subtree +
    profile/ subtree + root ``_.vue`` catch-all), nuxt-style ``_param``
    AND bracket ``[param]`` dynamics, index leaves, monorepo prefixes;
  * Seg A anti-cases: no vue-router/vite-plugin-pages dep -> inert; a
    Nuxt tree (nuxt.config.ts) -> inert (RouteFileExtractor owns it);
    stories/__tests__ files are never entries (test-strip law);
  * determinism: two extracts emit identical candidates.

Seg B (react-router code config) + Seg C (routes_index kind=spa-page)
units live further down as their commits land.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.extractors.spa_router import (
    SPA_PAGE_SOURCE,
    SPA_ROUTER_ENTRIES_ENV,
    SpaRouterExtractor,
    spa_router_entries_enabled,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext


def _ctx(repo: Path, files: list[str], **kw) -> ScanContext:
    return ScanContext(
        repo_path=repo,
        stack=kw.get("stack", "node"),
        monorepo=kw.get("monorepo", False),
        workspaces=kw.get("workspaces"),
        tracked_files=files,
        commits=[],
        secondary_stacks=kw.get("secondary_stacks", ()),
        audited_stack=kw.get("audited_stack"),
    )


@pytest.fixture
def spa_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SPA_ROUTER_ENTRIES_ENV, "1")


def _write(tmp_path: Path, rel: str, body: str) -> str:
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    return rel


def _vue_pkg(tmp_path: Path, rel: str = "package.json") -> str:
    return _write(
        tmp_path, rel,
        '{"name": "app", "dependencies": {"vue": "^3.4.0", '
        '"vue-router": "4.6.4"}, '
        '"devDependencies": {"vite-plugin-pages": "0.33.3"}}',
    )


def _routes_of(anchors) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for a in anchors:
        out.update(a.routes)
    return out


# ── flag / kill-switch ───────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SPA_ROUTER_ENTRIES_ENV, raising=False)
    assert spa_router_entries_enabled() is False
    for falsy in ("0", "false", "off", "no", ""):
        monkeypatch.setenv(SPA_ROUTER_ENTRIES_ENV, falsy)
        assert spa_router_entries_enabled() is False, falsy
    for truthy in ("1", "true", "True", "yes", "on"):
        monkeypatch.setenv(SPA_ROUTER_ENTRIES_ENV, truthy)
        assert spa_router_entries_enabled() is True, truthy


def test_off_is_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset flag -> zero candidates even with real vue pages present."""
    monkeypatch.delenv(SPA_ROUTER_ENTRIES_ENV, raising=False)
    files = [
        _vue_pkg(tmp_path),
        _write(tmp_path, "src/pages/settings.vue", "<template/>"),
    ]
    assert SpaRouterExtractor().extract(_ctx(tmp_path, files)) == []


def test_off_not_registered_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF byte-identity at the REGISTRY surface: scan_meta.extractor_hits
    serializes every registered source key, so with the flag unset the
    extractor must not even REGISTER (B67 kill-switch lesson). With the
    flag set it must appear, and nothing else changes."""
    from faultline.pipeline_v2.stage_1_extractors import (
        _load_default_extractors,
    )

    monkeypatch.delenv(SPA_ROUTER_ENTRIES_ENV, raising=False)
    names_off = {e.name for e in _load_default_extractors()}
    assert SPA_PAGE_SOURCE not in names_off

    monkeypatch.setenv(SPA_ROUTER_ENTRIES_ENV, "1")
    names_on = {e.name for e in _load_default_extractors()}
    assert SPA_PAGE_SOURCE in names_on
    assert names_on - {SPA_PAGE_SOURCE} == names_off


# ── Seg A — vue file-based pages (hoppscotch set, live census) ──────────────


#: The hoppscotch-common pages tree POIMENNO (live repo, 2026-07-16).
_HOPP_COMMON_PAGES = [
    "_.vue",
    "e/_id.vue",
    "enter.vue",
    "graphql.vue",
    "import.vue",
    "index.vue",
    "join-team.vue",
    "oauth.vue",
    "profile.vue",
    "profile/index.vue",
    "profile/teams.vue",
    "profile/tokens.vue",
    "r/_id.vue",
    "realtime.vue",
    "realtime/mqtt.vue",
    "realtime/socketio.vue",
    "realtime/sse.vue",
    "realtime/websocket.vue",
    "settings.vue",
    "view/_id/_version.vue",
]


def test_seg_a_hoppscotch_pages_set(tmp_path: Path, spa_on) -> None:
    """The exact hoppscotch-common pages tree under a monorepo prefix maps
    to its URL set: 19 page routes (iter-3: the root ``index.vue`` emits
    ``/`` — the flagship REST page — and ``view/_id/_version.vue`` emits
    ``/view/:id/:version``, both via the enclosing-package slug fallback).
    The bare ``_.vue`` catch-all stays an honest skip (pure-dynamic URL,
    not a product surface)."""
    prefix = "packages/hoppscotch-common/src/pages/"
    files = [_vue_pkg(tmp_path, "packages/hoppscotch-common/package.json")]
    for rel in _HOPP_COMMON_PAGES:
        files.append(_write(tmp_path, prefix + rel, "<template/>"))

    anchors = SpaRouterExtractor().extract(_ctx(tmp_path, files))
    routes = _routes_of(anchors)
    patterns = {r[0] for r in routes}

    assert patterns == {
        "/",                 # index.vue — iter-3 root fallback
        "/e/:id",
        "/enter",
        "/graphql",
        "/import",
        "/join-team",
        "/oauth",
        "/profile",          # profile.vue AND profile/index.vue
        "/profile/teams",
        "/profile/tokens",
        "/r/:id",
        "/realtime",
        "/realtime/mqtt",
        "/realtime/socketio",
        "/realtime/sse",
        "/realtime/websocket",
        "/settings",
        "/view/:id/:version",  # iter-3 noise-chain fallback
    }
    # the bare catch-all is still never a surface
    assert not any("catchAll" in p for p in patterns)
    # 19 file-rows (both /profile files kept — distinct (pattern, file)).
    assert len(routes) == 19
    # every route is a PAGE (the GET-equivalent client surface)
    assert {r[1] for r in routes} == {"PAGE"}
    # dynamics never leak into slugs; realtime children share the anchor
    names = {a.name for a in anchors}
    assert "realtime" in names and "profile" in names
    assert all(not n.startswith(":") for n in names)
    assert all(a.source == SPA_PAGE_SOURCE for a in anchors)


def test_seg_a_bracket_dynamics_and_index(tmp_path: Path, spa_on) -> None:
    """vite-plugin-pages default bracket style: ``[id]`` + ``[...all]``
    dynamics, ``index.vue`` leaves take the directory URL."""
    files = [
        _vue_pkg(tmp_path),
        _write(tmp_path, "src/pages/users/[id].vue", "<template/>"),
        _write(tmp_path, "src/pages/users/index.vue", "<template/>"),
        _write(tmp_path, "src/pages/docs/[...all].vue", "<template/>"),
    ]
    routes = _routes_of(SpaRouterExtractor().extract(_ctx(tmp_path, files)))
    assert ("/users/:id", "PAGE", "src/pages/users/[id].vue") in routes
    assert ("/users", "PAGE", "src/pages/users/index.vue") in routes
    assert ("/docs/:all", "PAGE", "src/pages/docs/[...all].vue") in routes


def test_seg_a_no_dep_is_inert(tmp_path: Path, spa_on) -> None:
    """pages/**/*.vue WITHOUT a vue-router / vite-plugin-pages dep -> no
    candidates (mechanism activation is corroborated, never assumed)."""
    files = [
        _write(tmp_path, "package.json", '{"name": "x", "dependencies": {}}'),
        _write(tmp_path, "src/pages/settings.vue", "<template/>"),
    ]
    assert SpaRouterExtractor().extract(_ctx(tmp_path, files)) == []


def test_seg_a_nuxt_tree_is_skipped(tmp_path: Path, spa_on) -> None:
    """A nuxt.config.* tree is ALREADY covered by the RouteFileExtractor
    ``nuxt`` grammar — the spa extractor must not double it (SACRED)."""
    files = [
        _vue_pkg(tmp_path),
        _write(tmp_path, "nuxt.config.ts", "export default {}"),
        _write(tmp_path, "pages/settings.vue", "<template/>"),
    ]
    assert SpaRouterExtractor().extract(_ctx(tmp_path, files)) == []


def test_seg_a_nuxt_workspace_scoped_skip(tmp_path: Path, spa_on) -> None:
    """In a monorepo only the NUXT workspace's pages are skipped — a
    sibling vue-SPA workspace still emits."""
    files = [
        _vue_pkg(tmp_path, "apps/spa/package.json"),
        _write(tmp_path, "apps/docs/nuxt.config.ts", "export default {}"),
        _write(tmp_path, "apps/docs/pages/guide.vue", "<template/>"),
        _write(tmp_path, "apps/spa/src/pages/board.vue", "<template/>"),
    ]
    routes = _routes_of(SpaRouterExtractor().extract(_ctx(tmp_path, files)))
    assert ("/board", "PAGE", "apps/spa/src/pages/board.vue") in routes
    assert all("docs" not in r[2] for r in routes)


def test_seg_a_stories_and_tests_never_entries(tmp_path: Path, spa_on) -> None:
    """Storybook / __tests__ artifacts under pages/ are never entries
    (chinny test-strip law + B58 dev_artifact)."""
    files = [
        _vue_pkg(tmp_path),
        _write(tmp_path, "src/pages/settings.vue", "<template/>"),
        _write(tmp_path, "src/pages/settings.stories.vue", "<template/>"),
        _write(tmp_path, "src/pages/__tests__/settings.vue", "<template/>"),
        _write(tmp_path, "src/pages/settings.spec.vue", "<template/>"),
    ]
    routes = _routes_of(SpaRouterExtractor().extract(_ctx(tmp_path, files)))
    assert {r[2] for r in routes} == {"src/pages/settings.vue"}


def test_seg_a_next_repo_untouched(tmp_path: Path, spa_on) -> None:
    """A Next repo (no .vue, no vue deps) gets ZERO spa candidates — its
    pages stay covered by the stock extractors alone (SACRED no-dup)."""
    files = [
        _write(
            tmp_path, "package.json",
            '{"name": "x", "dependencies": {"next": "14.0.0", '
            '"react": "18.0.0"}}',
        ),
        _write(tmp_path, "pages/index.tsx", "export default () => null"),
        _write(tmp_path, "pages/teams/[id].tsx", "export default () => null"),
    ]
    assert SpaRouterExtractor().extract(_ctx(tmp_path, files)) == []


def test_seg_a_determinism(tmp_path: Path, spa_on) -> None:
    files = [_vue_pkg(tmp_path)]
    for rel in ("a.vue", "b/_id.vue", "c/index.vue"):
        files.append(_write(tmp_path, "src/pages/" + rel, "<template/>"))
    ex = SpaRouterExtractor()
    ctx = _ctx(tmp_path, files)
    assert ex.extract(ctx) == ex.extract(ctx)


# ── Seg B — react-router code config (Soc0-shaped, live census) ─────────────


def _rr_pkg(tmp_path: Path, rel: str = "frontend/package.json") -> str:
    return _write(
        tmp_path, rel,
        '{"name": "web", "dependencies": {"react": "^18.0.0", '
        '"react-router-dom": "^7.6.1"}}',
    )


_SOC0_APP_TSX = """
import { Routes, Route, Navigate } from 'react-router-dom';
import { HomePage } from '@/pages/HomePage';
import { InvestigationsPage } from '@/pages/InvestigationsPage';
import { InvestigationDetailPage } from '@/pages/InvestigationDetailPage';
import { CasesPage } from '@/pages/CasesPage';
import { TrialGuard } from '@/components/TrialGuard';
import MitreCoveragePage from '@/pages/MitreCoveragePage';

export function App() {
  return (
    <Routes>
      <Route
        element={
          <Shell />
        }
      >
        <Route path="/" element={<HomePage />} />
        <Route path="/investigations" element={<InvestigationsPage />} />
        <Route path="/investigations/:investigationId" element={<InvestigationDetailPage />} />
        <Route path="/cases" element={<TrialGuard><CasesPage /></TrialGuard>} />
        <Route path="/detectors/mitre-coverage" element={<TrialGuard><MitreCoveragePage /></TrialGuard>} />
        <Route path="/executive-brief" element={<Navigate to="/autonomous-soc/overview" replace />} />
        <Route path="/policy" element={<TrialGuard><Navigate to="/knowledge" replace /></TrialGuard>} />
      </Route>
    </Routes>
  );
}
"""


def _soc0_files(tmp_path: Path) -> list[str]:
    files = [
        _rr_pkg(tmp_path),
        _write(tmp_path, "frontend/src/App.tsx", _SOC0_APP_TSX),
        _write(tmp_path, "frontend/src/pages/HomePage.tsx", "export const HomePage = () => null"),
        _write(tmp_path, "frontend/src/pages/InvestigationsPage.tsx", "export const InvestigationsPage = () => null"),
        _write(tmp_path, "frontend/src/pages/InvestigationDetailPage.tsx", "export const InvestigationDetailPage = () => null"),
        _write(tmp_path, "frontend/src/pages/CasesPage.tsx", "export const CasesPage = () => null"),
        _write(tmp_path, "frontend/src/pages/MitreCoveragePage.tsx", "export default () => null"),
        _write(tmp_path, "frontend/src/components/TrialGuard.tsx", "export const TrialGuard = ({children}) => children"),
    ]
    return files


def test_seg_b_soc0_jsx_routes(tmp_path: Path, spa_on) -> None:
    """The Soc0 App.tsx shape POIMENNO: pathless layout wrapper transparent,
    @/-alias entry resolution, guard-wrapped innermost component wins,
    :param paths, Navigate redirects skipped (bare AND guard-wrapped)."""
    anchors = SpaRouterExtractor().extract(_ctx(tmp_path, _soc0_files(tmp_path)))
    routes = _routes_of(anchors)

    assert ("/", "PAGE", "frontend/src/pages/HomePage.tsx") in routes
    assert ("/investigations", "PAGE",
            "frontend/src/pages/InvestigationsPage.tsx") in routes
    assert ("/investigations/:investigationId", "PAGE",
            "frontend/src/pages/InvestigationDetailPage.tsx") in routes
    # guard wrapper: the INNERMOST component is the entry, not TrialGuard
    assert ("/cases", "PAGE", "frontend/src/pages/CasesPage.tsx") in routes
    assert ("/detectors/mitre-coverage", "PAGE",
            "frontend/src/pages/MitreCoveragePage.tsx") in routes
    # redirects are not pages — bare Navigate AND guard-wrapped Navigate
    assert all("/executive-brief" != r[0] for r in routes)
    assert all("/policy" != r[0] for r in routes)
    # slugs: URL-segment first, component fallback for "/"
    names = {a.name for a in anchors}
    assert {"home", "investigations", "cases", "detectors"} <= names


def test_seg_b_component_slug_fallback(tmp_path: Path, spa_on) -> None:
    """A '/' route has no static segment — the resolved component name
    (suffix-stripped) is the slug: HomePage -> home."""
    anchors = SpaRouterExtractor().extract(_ctx(tmp_path, _soc0_files(tmp_path)))
    by_name = {a.name: a for a in anchors}
    assert "home" in by_name
    assert by_name["home"].paths == ("frontend/src/pages/HomePage.tsx",)


def test_seg_b_create_browser_router_with_lazy(tmp_path: Path, spa_on) -> None:
    """createBrowserRouter object arrays: nested children join parent paths;
    a route-level lazy(() => import(...)) target IS the entry (B37 bridge);
    a lazy const binding resolves through the import map."""
    router = """
import { createBrowserRouter } from 'react-router-dom';
import { lazy } from 'react';
const SettingsPage = lazy(() => import('./pages/SettingsPage'));
export const router = createBrowserRouter([
  {
    path: '/app',
    element: <Shell />,
    children: [
      { path: 'dashboard', lazy: () => import('./pages/DashboardPage') },
      { path: 'settings', element: <SettingsPage /> },
      { index: true, element: <SettingsPage /> },
    ],
  },
]);
"""
    files = [
        _rr_pkg(tmp_path, "package.json"),
        _write(tmp_path, "src/router.tsx", router),
        _write(tmp_path, "src/pages/DashboardPage.tsx", "export default () => null"),
        _write(tmp_path, "src/pages/SettingsPage.tsx", "export default () => null"),
    ]
    routes = _routes_of(SpaRouterExtractor().extract(_ctx(tmp_path, files)))
    assert ("/app/dashboard", "PAGE", "src/pages/DashboardPage.tsx") in routes
    assert ("/app/settings", "PAGE", "src/pages/SettingsPage.tsx") in routes
    # index route rides the parent path
    assert ("/app", "PAGE", "src/pages/SettingsPage.tsx") in routes


def test_seg_b_nested_relative_jsx_paths(tmp_path: Path, spa_on) -> None:
    """Nested JSX <Route> with RELATIVE child paths joins through the tag
    stack: path="settings" under path="/account" -> /account/settings."""
    app = """
import { Route, Routes } from 'react-router-dom';
import { ProfilePane } from './panes/ProfilePane';
export const App = () => (
  <Routes>
    <Route path="/account">
      <Route path="settings" element={<ProfilePane />} />
    </Route>
  </Routes>
);
"""
    files = [
        _rr_pkg(tmp_path, "package.json"),
        _write(tmp_path, "src/App.tsx", app),
        _write(tmp_path, "src/panes/ProfilePane.tsx", "export const ProfilePane = () => null"),
    ]
    routes = _routes_of(SpaRouterExtractor().extract(_ctx(tmp_path, files)))
    assert ("/account/settings", "PAGE", "src/panes/ProfilePane.tsx") in routes


def test_seg_b_no_dep_is_inert(tmp_path: Path, spa_on) -> None:
    """<Route> JSX WITHOUT a react-router(-dom) dep -> no candidates."""
    files = [
        _write(tmp_path, "package.json", '{"name": "x", "dependencies": {}}'),
        _write(
            tmp_path, "src/App.tsx",
            'import { X } from "./X";\n'
            '<Routes><Route path="/x" element={<X />} /></Routes>',
        ),
        _write(tmp_path, "src/X.tsx", "export const X = () => null"),
    ]
    assert SpaRouterExtractor().extract(_ctx(tmp_path, files)) == []


def test_seg_b_next_and_framework_repos_disqualify(
    tmp_path: Path, spa_on,
) -> None:
    """SACRED no-dup: a Next repo (embedded react-router widget) and a
    react-router FRAMEWORK-mode repo (config file / @react-router/*) never
    activate Seg B — their pages are covered by filesystem extractors."""
    app_body = (
        'import { W } from "./W";\n'
        '<Routes><Route path="/widget" element={<W />} /></Routes>'
    )
    # Next repo with an embedded react-router widget
    files = [
        _write(
            tmp_path, "package.json",
            '{"name": "x", "dependencies": {"next": "14.0.0", '
            '"react-router-dom": "^6.0.0"}}',
        ),
        _write(tmp_path, "src/App.tsx", app_body),
        _write(tmp_path, "src/W.tsx", "export const W = () => null"),
    ]
    assert SpaRouterExtractor().extract(_ctx(tmp_path, files)) == []

    # react-router framework mode (the Remix successor)
    fw = tmp_path / "fw"
    files_fw = [
        _write(
            fw, "package.json",
            '{"name": "x", "dependencies": {"react-router": "^7.0.0"}, '
            '"devDependencies": {"@react-router/dev": "^7.0.0"}}',
        ),
        _write(fw, "react-router.config.ts", "export default {}"),
        _write(fw, "app/App.tsx", app_body),
        _write(fw, "app/W.tsx", "export const W = () => null"),
    ]
    assert SpaRouterExtractor().extract(_ctx(fw, files_fw)) == []


def test_seg_b_stories_and_tests_never_entries(tmp_path: Path, spa_on) -> None:
    files = [
        _rr_pkg(tmp_path, "package.json"),
        _write(
            tmp_path, "src/App.stories.tsx",
            'import { P } from "./P";\n'
            '<Routes><Route path="/story" element={<P />} /></Routes>',
        ),
        _write(
            tmp_path, "src/__tests__/App.tsx",
            'import { P } from "../P";\n'
            '<Routes><Route path="/tested" element={<P />} /></Routes>',
        ),
        _write(tmp_path, "src/P.tsx", "export const P = () => null"),
    ]
    assert SpaRouterExtractor().extract(_ctx(tmp_path, files)) == []


def test_seg_b_unresolvable_entry_falls_back_to_config_file(
    tmp_path: Path, spa_on,
) -> None:
    """An element whose import cannot be resolved (external package /
    ambiguous alias) still emits the route — entry = the config file."""
    files = [
        _rr_pkg(tmp_path, "package.json"),
        _write(
            tmp_path, "src/App.tsx",
            'import { VendorPage } from "some-external-kit";\n'
            '<Routes><Route path="/vendor" element={<VendorPage />} /></Routes>',
        ),
    ]
    routes = _routes_of(SpaRouterExtractor().extract(_ctx(tmp_path, files)))
    assert ("/vendor", "PAGE", "src/App.tsx") in routes


def test_seg_b_determinism(tmp_path: Path, spa_on) -> None:
    files = _soc0_files(tmp_path)
    ex = SpaRouterExtractor()
    ctx = _ctx(tmp_path, files)
    assert ex.extract(ctx) == ex.extract(ctx)


# ── Seg C — routes_index kind=spa-page + downstream visibility ──────────────


def test_seg_c_routes_index_kind_stamp(tmp_path: Path, spa_on) -> None:
    """spa-page rows land in routes_index with kind='spa-page'; rows from
    every other source stay byte-identical (no kind key)."""
    from faultline.pipeline_v2.indexes import build_routes_index

    anchors = SpaRouterExtractor().extract(_ctx(tmp_path, _soc0_files(tmp_path)))
    signals = {SPA_PAGE_SOURCE: anchors}
    rows = build_routes_index([], signals)
    assert rows, "spa candidates must populate routes_index"
    assert all(r.get("kind") == "spa-page" for r in rows)
    assert ("/investigations", "PAGE") in {
        (r["pattern"], r["method"]) for r in rows
    }


def test_seg_c_existing_source_wins_identical_triple(tmp_path: Path) -> None:
    """(file,path) idempotency, SACRED: a triple already emitted by an
    EXISTING route source keeps its kind-less row byte-identical; the spa
    duplicate folds away (Pass C runs last). Genuinely new spa rows append
    WITH kind."""
    from faultline.pipeline_v2.extractors.base import AnchorCandidate
    from faultline.pipeline_v2.indexes import build_routes_index

    existing = AnchorCandidate(
        name="teams", paths=("apps/web/x.ts",), source="fastapi-route",
        confidence_self=0.9,
        routes=(("/teams", "PAGE", "apps/web/x.ts"),),
    )
    spa = AnchorCandidate(
        name="teams", paths=("apps/web/x.ts",), source=SPA_PAGE_SOURCE,
        confidence_self=0.8,
        routes=(
            ("/teams", "PAGE", "apps/web/x.ts"),      # identical triple
            ("/teams/:id", "PAGE", "apps/web/x.ts"),  # genuinely new
        ),
    )
    rows = build_routes_index(
        [], {"fastapi-route": [existing], SPA_PAGE_SOURCE: [spa]},
    )
    by_key = {(r["pattern"], r["method"], r["file"]): r for r in rows}
    assert len(rows) == 2
    dup = by_key[("/teams", "PAGE", "apps/web/x.ts")]
    assert "kind" not in dup, "existing source's row must stay byte-identical"
    new = by_key[("/teams/:id", "PAGE", "apps/web/x.ts")]
    assert new.get("kind") == "spa-page"


def test_seg_c_off_routes_index_byte_identical(tmp_path: Path,
                                               monkeypatch) -> None:
    """Kill-switch at the routes_index surface: flag unset -> the signals
    dict carries no spa key (extractor unregistered+inert) and the built
    index is byte-identical to pre-B65-v3."""
    from faultline.pipeline_v2.extractors.base import AnchorCandidate
    from faultline.pipeline_v2.indexes import build_routes_index

    monkeypatch.delenv(SPA_ROUTER_ENTRIES_ENV, raising=False)
    files = [
        _vue_pkg(tmp_path),
        _write(tmp_path, "src/pages/settings.vue", "<template/>"),
    ]
    spa_off = SpaRouterExtractor().extract(_ctx(tmp_path, files))
    assert spa_off == []
    other = AnchorCandidate(
        name="api", paths=("api/x.py",), source="fastapi-route",
        confidence_self=0.9, routes=(("/api/x", "GET", "api/x.py"),),
    )
    rows_off = build_routes_index([], {"fastapi-route": [other]})
    rows_pre = build_routes_index([], {"fastapi-route": [other],
                                       SPA_PAGE_SOURCE: []})
    assert rows_off == rows_pre
    assert all("kind" not in r for r in rows_off)


def test_seg_c_file_lane_surface_sees_spa_page(tmp_path: Path, spa_on) -> None:
    """B65 partition surface-detect (S3 no-product-surface prong): a
    spa-page routes_index row makes its entry file a PRODUCT SURFACE —
    the B65-v2 killer (no_product_surface gasped every candidate) is
    structurally cured for SPA repos."""
    from faultline.pipeline_v2.file_lane import _surface_paths

    routes_index = [{
        "pattern": "/investigations", "method": "PAGE",
        "feature_uuid": "", "file": "frontend/src/pages/InvestigationsPage.tsx",
        "kind": "spa-page",
    }]
    surface = _surface_paths([], routes_index)
    assert "frontend/src/pages/InvestigationsPage.tsx" in surface


def test_seg_c_spine_anchor_page_evidence(tmp_path: Path, spa_on) -> None:
    """6.86 mint chain: a spa-page row (method=PAGE) lands in the spine
    route anchor's page_route_files — the PAGE-SURFACE rule sees SPA
    pages with zero new wiring."""
    from faultline.pipeline_v2.spine_anchors import (
        _build_route_anchors,
        load_spine_vocab,
    )

    routes_index = [{
        "pattern": "/investigations/:investigationId", "method": "PAGE",
        "feature_uuid": "",
        "file": "frontend/src/pages/InvestigationDetailPage.tsx",
        "kind": "spa-page",
    }]
    anchors = _build_route_anchors(routes_index, load_spine_vocab())
    # normalize_anchor_key singularizes: investigations -> investigation
    inv = [a for a in anchors if a.key == "investigation"]
    assert inv, "spa route must build a route: spine anchor"
    assert any(
        "frontend/src/pages/InvestigationDetailPage.tsx" in a.page_route_files
        for a in inv
    )


# ── fix-iteration 1 — delivery through the per-workspace merge ───────────────


def _spa_cand(slug: str, file: str, pattern: str):
    from faultline.pipeline_v2.extractors.base import AnchorCandidate

    return AnchorCandidate(
        name=slug, paths=(file,), source=SPA_PAGE_SOURCE,
        confidence_self=0.8, routes=((pattern, "PAGE", file),),
    )


def _hopp_twin_results():
    """The live hoppscotch loss shape (ON forensics 2026-07-16): same-slug
    1-path spa candidates — across workspaces ('enter') AND within one
    workspace ('profile' — per-(file,slug) emission makes twins by
    construction)."""
    common = {
        SPA_PAGE_SOURCE: [
            _spa_cand("enter", "packages/common/src/pages/enter.vue", "/enter"),
            _spa_cand("profile", "packages/common/src/pages/profile.vue",
                      "/profile"),
            _spa_cand("profile", "packages/common/src/pages/profile/teams.vue",
                      "/profile/teams"),
            _spa_cand("graphql", "packages/common/src/pages/graphql.vue",
                      "/graphql"),
        ],
    }
    admin = {
        SPA_PAGE_SOURCE: [
            _spa_cand("enter", "packages/admin/src/pages/enter.vue", "/enter"),
        ],
    }
    return [("common", common), ("admin", admin)]


def test_ws_merge_same_slug_twins_survive_when_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPA flag ON: the spa-page source is ARMED (ce821a5/B66 origin-gate
    pattern) — same-slug coalesced groups keep the routes UNION, so every
    emitted row reaches routes_index."""
    from faultline.pipeline_v2.stage_1_per_workspace import (
        _merge_anchors_across_workspaces,
    )

    monkeypatch.setenv(SPA_ROUTER_ENTRIES_ENV, "1")
    merged = _merge_anchors_across_workspaces(_hopp_twin_results())
    routes = {r for c in merged[SPA_PAGE_SOURCE] for r in (c.routes or ())}
    assert {r[0] for r in routes} == {
        "/enter", "/profile", "/profile/teams", "/graphql",
    }
    # both workspaces' /enter files survive the coalesce
    enter_files = {r[2] for r in routes if r[0] == "/enter"}
    assert enter_files == {
        "packages/common/src/pages/enter.vue",
        "packages/admin/src/pages/enter.vue",
    }


def test_ws_merge_same_slug_twins_lose_routes_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPA flag OFF: the source is NOT armed — coalesced groups drop routes
    exactly as pre-B65-v3 (OFF byte-identity of the merge layer). Only the
    size-1 group ('graphql') keeps its route."""
    from faultline.pipeline_v2.stage_1_per_workspace import (
        _merge_anchors_across_workspaces,
    )

    monkeypatch.delenv(SPA_ROUTER_ENTRIES_ENV, raising=False)
    merged = _merge_anchors_across_workspaces(_hopp_twin_results())
    routes = {r for c in merged[SPA_PAGE_SOURCE] for r in (c.routes or ())}
    assert {r[0] for r in routes} == {"/graphql"}


def test_ws_merge_arming_is_origin_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The onyx lesson: arming spa-page must NOT preserve routes for
    UNARMED sources' twins in the same merge."""
    from faultline.pipeline_v2.extractors.base import AnchorCandidate
    from faultline.pipeline_v2.stage_1_per_workspace import (
        _merge_anchors_across_workspaces,
    )

    monkeypatch.setenv(SPA_ROUTER_ENTRIES_ENV, "1")

    def _route_cand(file: str) -> AnchorCandidate:
        return AnchorCandidate(
            name="teams", paths=(file,), source="route",
            confidence_self=0.7, routes=(("/teams", "GET", file),),
        )

    merged = _merge_anchors_across_workspaces([
        ("a", {"route": [_route_cand("a/teams.py")]}),
        ("b", {"route": [_route_cand("b/teams.py")]}),
    ])
    routes = {r for c in merged["route"] for r in (c.routes or ())}
    assert routes == set(), "unarmed 'route' twins must still drop routes"


# ── fix-iteration 2 — SPA-mint containment fence + floor (6.86) ──────────────


def _mint_fixture():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from faultline.models.types import Feature, Flow, MemberFile

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def flow(name, entry):
        return Flow(
            name=name, entry_point_file=entry, paths=[entry], authors=["a"],
            total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
            last_modified=now, health_score=100.0,
        )

    def dev(name, paths, flows=None):
        return Feature(
            name=name, paths=list(paths),
            member_files=[
                MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
                for p in paths
            ],
            flows=flows or [], product_feature_id="old-pf",
            authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
            last_modified=now, health_score=100.0,
        )

    def ctx_of(repo_path, tracked):
        return SimpleNamespace(
            workspaces=None, tracked_files=tracked,
            repo_path=Path(repo_path), monorepo=False,
        )

    return flow, dev, ctx_of


def _write_loc(tmp_path: Path, rel: str, lines: int) -> str:
    return _write(tmp_path, rel, "\n".join("const x%d = 1;" % i
                                           for i in range(lines)) + "\n")


def test_mint_fence_soc0_shape_no_annexation(tmp_path: Path) -> None:
    """The Soc0 wave exhibit POIMENNO: a broad mixed dev (spa page +
    foreign-module mass) must NOT bind to the spa-paged route: anchor via
    ANY rung — the mass stays off the PF; a server-router dev (non-spa
    evidence of the SAME anchor) binds exactly as before."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        run_anchored_mint,
    )

    flow, dev, ctx_of = _mint_fixture()
    page = "frontend/src/pages/AdminPage.tsx"
    router = "backend/routers/admin.py"
    chat = "frontend/src/components/chat/chart-block.tsx"
    api = "frontend/src/api/autopilot.ts"
    for rel, n in ((page, 200), (router, 200), (chat, 300), (api, 120)):
        _write_loc(tmp_path, rel, n)

    routes = [
        {"pattern": "/admin", "method": "PAGE", "file": page,
         "kind": "spa-page"},
        # the api row shares the 'admin' chain key — the real Soc0 shape
        # (route:admin = spa page + backend router files)
        {"pattern": "/api/admin/migrate", "method": "POST", "file": router},
    ]
    broad = dev("api-admin", [page, chat, api],
                flows=[flow("admin-flow", page)])
    server = dev("backend-admin", [router])
    pfs, tele = run_anchored_mint(
        [broad, server], routes,
        ctx_of(tmp_path, [page, router, chat, api]),
    )

    assert tele.get("spa_fence_blocked", 0) >= 1
    admin_pfs = [p for p in pfs if p.anchor_id == "route:admin"]
    assert admin_pfs, "server evidence still mints the anchor (anti-case)"
    # the server dev binds; the broad dev's foreign mass stays OFF the PF
    assert server.product_feature_id == admin_pfs[0].name
    assert broad.product_feature_id != admin_pfs[0].name
    member_paths = {
        m.path for p in admin_pfs for m in (p.member_files or [])
    } | {q for p in admin_pfs for q in (p.paths or [])}
    assert chat not in member_paths and api not in member_paths


def test_mint_fence_floor_kills_thin_page(tmp_path: Path) -> None:
    """The hoppscotch 'Enter' class: a contained page dev below the husk
    floor (96 LOC < 150) never mints a PF — but the routes_index row
    lives on (journey seed + partition surface, the Seg C value)."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        run_anchored_mint,
    )

    flow, dev, ctx_of = _mint_fixture()
    # the REAL hoppscotch shape: TWO enter.vue twins (common + sh-admin),
    # 96+38 LOC — above the single-file bar's reach, below the husk floor
    page = "packages/common/src/pages/enter.vue"
    page2 = "packages/admin/src/pages/enter.vue"
    _write(tmp_path, page, "<template>\n" + "a\n" * 94 + "</template>\n")
    _write(tmp_path, page2, "<template>\n" + "a\n" * 36 + "</template>\n")
    routes = [
        {"pattern": "/enter", "method": "PAGE", "file": page,
         "kind": "spa-page"},
        {"pattern": "/enter", "method": "PAGE", "file": page2,
         "kind": "spa-page"},
    ]
    enter = dev("enter", [page, page2])
    pfs, tele = run_anchored_mint([enter], list(routes),
                                  ctx_of(tmp_path, [page, page2]))

    assert tele.get("mint_bar_spa_page_floor", 0) == 1
    assert not [p for p in pfs if p.anchor_id == "route:enter"]
    # routes_index rows are untouched by the mint
    assert routes[0]["file"] == page and routes[0]["kind"] == "spa-page"


def test_mint_fence_contained_page_module_still_mints(
    tmp_path: Path,
) -> None:
    """A page whose module subtree is real (page file + co-located dir,
    over the floor) mints a normally-membered PF — the fence only blocks
    OUTSIDE mass, never the page's own module."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        run_anchored_mint,
    )

    flow, dev, ctx_of = _mint_fixture()
    page = "src/pages/settings.vue"
    sub = "src/pages/settings/profile.vue"
    _write(tmp_path, page, "<template>\n" + "a\n" * 120 + "</template>\n")
    _write(tmp_path, sub, "<template>\n" + "a\n" * 120 + "</template>\n")
    routes = [
        {"pattern": "/settings", "method": "PAGE", "file": page,
         "kind": "spa-page"},
        {"pattern": "/settings/profile", "method": "PAGE", "file": sub,
         "kind": "spa-page"},
    ]
    d = dev("settings", [page, sub],
            flows=[flow("edit-settings-flow", page)])
    pfs, tele = run_anchored_mint([d], routes, ctx_of(tmp_path, [page, sub]))

    minted = [p for p in pfs if str(p.anchor_id or "").startswith("route:")]
    assert minted, "contained page module must still mint"
    assert d.product_feature_id == minted[0].name
    assert tele.get("mint_bar_spa_page_floor", 0) == 0


def test_mint_fence_inert_without_spa_rows(tmp_path: Path) -> None:
    """Server-route mint unchanged (anti-case + kill-switch law): with NO
    kind=spa-page rows the fence and floor never fire — no fence
    telemetry, no spa bars."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        run_anchored_mint,
    )

    flow, dev, ctx_of = _mint_fixture()
    router = "backend/routers/admin.py"
    _write_loc(tmp_path, router, 200)
    routes = [
        {"pattern": "/api/_admin/migrate", "method": "POST", "file": router},
    ]
    server = dev("backend-admin", [router])
    pfs, tele = run_anchored_mint([server], routes,
                                  ctx_of(tmp_path, [router]))

    assert "spa_fence_blocked" not in tele
    assert "mint_bar_spa_page_floor" not in tele


# ── B65-v4 Seg A — spa-mint priority (spa never wins a feature-dir) ──────────


def test_segA_detectors_shape_fdir_mass_binds_with_import_closure(
    tmp_path: Path,
) -> None:
    """The Soc0 Detectors husk POIMENNO: a spa page ``/detectors`` merges
    (same key) into the authored ``fdir:frontend/src/features/detectors``
    anchor. The feature-dir owns the subtree, so its whole component mass
    — INCLUDING files reached only through import-closure of shared UI —
    binds to ``route:detector``; the spa page merely ADDS the route row.
    Before the Seg A fix the merged anchor was spa-FENCED and orphaned
    its own 16.8K subtree (loc=0 husk, 37 members)."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        run_anchored_mint,
    )

    flow, dev, ctx_of = _mint_fixture()
    page = "frontend/src/pages/DetectorsPage.tsx"
    fdir = "frontend/src/features/detectors"
    comps = [f"{fdir}/components/DetectorCard.tsx",
             f"{fdir}/components/DetectorEditForm.tsx",
             f"{fdir}/components/DetectorRunsTab.tsx",
             f"{fdir}/lookback.ts"]
    for rel in (page, *comps):
        _write_loc(tmp_path, rel, 120)  # 4×120 fdir mass >> husk floor
    routes = [
        {"pattern": "/detectors", "method": "PAGE", "file": page,
         "kind": "spa-page"},
    ]
    # ONE dev owns the page + the whole feature-dir subtree (the real
    # shape: the page component pulls the feature-dir components).
    detectors = dev("detectors-ui", [page, *comps],
                    flows=[flow("browse-detectors", page)])
    pfs, tele = run_anchored_mint(
        [detectors], routes, ctx_of(tmp_path, [page, *comps]))

    det = [p for p in pfs if p.anchor_id == "route:detector"]
    assert det, "the feature-dir anchor must mint (no husk)"
    assert detectors.product_feature_id == det[0].name
    members = {m.path for m in (det[0].member_files or [])} | set(
        det[0].paths or [])
    # the fdir component mass — including the import-closure leaf — is
    # OWNED, not orphaned (husk fixed)
    for c in comps:
        assert c in members, f"fdir mass {c} must bind (husk root)"
    # the spa route row is preserved (journey seed)
    assert routes[0]["kind"] == "spa-page"


def test_segA_pure_spa_page_still_fenced_no_annexation(
    tmp_path: Path,
) -> None:
    """Anti-case (the fence stays for PURELY spa anchors): a spa page with
    NO authored (fdir/hub/ws/schema/pypkg/svc) backer must NOT annex a
    broad dev's foreign import-closure mass — Seg A lifts the fence ONLY
    where a feature-dir/hub already owns the subtree. Mirrors the iter-2
    ``route:admin`` exhibit at the Seg A boundary."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        run_anchored_mint,
    )

    flow, dev, ctx_of = _mint_fixture()
    page = "frontend/src/pages/ReportsPage.tsx"          # spa page, no fdir
    foreign = "frontend/src/lib/shared-charts.ts"        # import-closure
    foreign2 = "frontend/src/api/telemetry.ts"
    for rel, n in ((page, 200), (foreign, 300), (foreign2, 120)):
        _write_loc(tmp_path, rel, n)
    routes = [
        {"pattern": "/reports", "method": "PAGE", "file": page,
         "kind": "spa-page"},
    ]
    broad = dev("reports", [page, foreign, foreign2],
                flows=[flow("reports-flow", page)])
    pfs, tele = run_anchored_mint(
        [broad], routes, ctx_of(tmp_path, [page, foreign, foreign2]))

    assert tele.get("spa_fence_blocked", 0) >= 1, "pure-spa stays fenced"
    minted = [p for p in pfs if p.anchor_id == "route:report"]
    member_paths = {m.path for p in minted for m in (p.member_files or [])} | {
        q for p in minted for q in (p.paths or [])}
    assert foreign not in member_paths and foreign2 not in member_paths


# ── B65-v4 iter-2 — authored-fdir floor gate + barrel-hop (Ticketing) ────────


def test_iter2_ticketing_shape_authored_fdir_survives_floor(
    tmp_path: Path,
) -> None:
    """The Soc0 Ticketing census exhibit POIMENNO: a TINY authored
    feature-dir (68 LOC — under the 150 spa floor) whose page also rides
    a coincident spa route. The merged anchor (route:ticketing = fdir +
    spa page) is AUTHORED, so the spa_page_floor never fires: the PF
    mints exactly as in the flag-OFF world, BOTH devs (the authored
    sub-domain dev + the spa echo dev) bind to it, zero
    sub_mint_bar_surface lanes, zero dev twins left laned, and no
    member-ful 0-LOC husk row (LOC doctrine)."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        run_anchored_mint,
    )

    flow, dev, ctx_of = _mint_fixture()
    page = "frontend/src/features/ticketing/TicketingPage.tsx"
    barrel = "frontend/src/features/ticketing/index.ts"
    _write_loc(tmp_path, page, 68)   # < 150 — the floor would kill it
    _write(tmp_path, barrel,
           "export { TicketingPage } from './TicketingPage';\n")
    routes = [
        # post-barrel-hop world: the spa row rides the REAL page file
        {"pattern": "/ticketing", "method": "PAGE", "file": page,
         "kind": "spa-page"},
    ]
    fdir_dev = dev("ticketing", [page, barrel])       # authored sub-domain
    spa_dev = dev("ticketing-2", [page])              # spa echo dev
    pfs, tele = run_anchored_mint(
        [fdir_dev, spa_dev], routes, ctx_of(tmp_path, [page, barrel]))

    tick = [p for p in pfs if p.anchor_id == "route:ticketing"]
    assert tick, "authored fdir PF must survive the spa floor (iter-2)"
    assert tele.get("mint_bar_spa_page_floor", 0) == 0
    # both devs bind — no sub_mint_bar_surface lane demotes, no twins
    # stranded in the lane
    assert fdir_dev.product_feature_id == tick[0].name
    assert spa_dev.product_feature_id == tick[0].name
    assert fdir_dev.shared_reason is None
    assert spa_dev.shared_reason is None


def test_iter2_spa_only_thin_anchor_still_floors(tmp_path: Path) -> None:
    """Anti-case (the floor survives for PURELY spa anchors): the
    hoppscotch 'Enter' class — a thin page with NO authored backer —
    still never mints. The iter-2 gate must not lift the floor
    wholesale."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        run_anchored_mint,
    )

    flow, dev, ctx_of = _mint_fixture()
    # NOT under a feature-dir container — no fdir anchor forms; two thin
    # twins (the real hoppscotch shape) get past the single-file bar so
    # the FLOOR is the deciding rung.
    page = "packages/common/src/views/enter.vue"
    page2 = "packages/admin/src/views/enter.vue"
    _write(tmp_path, page, "<template>\n" + "a\n" * 60 + "</template>\n")
    _write(tmp_path, page2, "<template>\n" + "a\n" * 30 + "</template>\n")
    routes = [
        {"pattern": "/enter", "method": "PAGE", "file": page,
         "kind": "spa-page"},
        {"pattern": "/enter", "method": "PAGE", "file": page2,
         "kind": "spa-page"},
    ]
    enter = dev("enter", [page, page2])
    pfs, tele = run_anchored_mint([enter], routes,
                                  ctx_of(tmp_path, [page, page2]))

    assert tele.get("mint_bar_spa_page_floor", 0) == 1
    assert not [p for p in pfs if p.anchor_id == "route:enter"]


def test_iter2_barrel_hop_entry_is_real_page(tmp_path: Path, spa_on) -> None:
    """The 0-LOC husk root POIMENNO: the router element resolves to the
    1-line re-export barrel ``features/ticketing/index.ts`` — the entry
    must hop THROUGH it to the real page file (TicketingPage.tsx), so
    no member-ful 0-LOC dev row is ever seeded and the route row rides
    the page surface."""
    files = [
        _rr_pkg(tmp_path, "package.json"),
        _write(tmp_path, "src/App.tsx", """
import { Routes, Route } from 'react-router-dom';
import { TicketingPage } from '@/features/ticketing';
export const App = () => (
  <Routes>
    <Route path="/ticketing" element={<TicketingPage />} />
  </Routes>
);
"""),
        _write(tmp_path, "src/features/ticketing/index.ts",
               "export { TicketingPage } from './TicketingPage';\n"),
        _write(tmp_path, "src/features/ticketing/TicketingPage.tsx",
               "export const TicketingPage = () => <div>tickets</div>;\n"),
    ]
    anchors = SpaRouterExtractor().extract(_ctx(tmp_path, files))
    routes = _routes_of(anchors)
    assert ("/ticketing", "PAGE",
            "src/features/ticketing/TicketingPage.tsx") in routes
    barrel_paths = [a for a in anchors
                    if "index.ts" in "".join(a.paths)]
    assert not barrel_paths, "a pure barrel is never the entry"


def test_iter2_barrel_multi_target_no_hop(tmp_path: Path, spa_on) -> None:
    """Anti-case: a MULTI-target feature index (re-exports from two
    files) is not a page echo — the entry honestly stays the barrel
    (no guessing which target is the page)."""
    files = [
        _rr_pkg(tmp_path, "package.json"),
        _write(tmp_path, "src/App.tsx", """
import { Routes, Route } from 'react-router-dom';
import { CasesPage } from '@/features/cases';
export const App = () => (
  <Routes>
    <Route path="/cases" element={<CasesPage />} />
  </Routes>
);
"""),
        _write(tmp_path, "src/features/cases/index.ts",
               "export { CasesPage } from './CasesPage';\n"
               "export { CaseCard } from './CaseCard';\n"),
        _write(tmp_path, "src/features/cases/CasesPage.tsx",
               "export const CasesPage = () => <div>cases</div>;\n"),
        _write(tmp_path, "src/features/cases/CaseCard.tsx",
               "export const CaseCard = () => <div>card</div>;\n"),
    ]
    anchors = SpaRouterExtractor().extract(_ctx(tmp_path, files))
    routes = _routes_of(anchors)
    assert ("/cases", "PAGE", "src/features/cases/index.ts") in routes


def test_iter2_barrel_chain_and_cycle_guard(tmp_path: Path, spa_on) -> None:
    """A barrel chaining to another barrel hops the chain (bounded); a
    self-cycle terminates honestly at the last non-cyclic file."""
    files = [
        _rr_pkg(tmp_path, "package.json"),
        _write(tmp_path, "src/App.tsx", """
import { Routes, Route } from 'react-router-dom';
import { DeepPage } from '@/features/deep';
export const App = () => (
  <Routes>
    <Route path="/deep" element={<DeepPage />} />
  </Routes>
);
"""),
        _write(tmp_path, "src/features/deep/index.ts",
               "export { DeepPage } from './inner';\n"),
        _write(tmp_path, "src/features/deep/inner.ts",
               "export { DeepPage } from './DeepPage';\n"),
        _write(tmp_path, "src/features/deep/DeepPage.tsx",
               "export const DeepPage = () => <div>deep</div>;\n"),
    ]
    anchors = SpaRouterExtractor().extract(_ctx(tmp_path, files))
    routes = _routes_of(anchors)
    assert ("/deep", "PAGE", "src/features/deep/DeepPage.tsx") in routes


# ── B65-v4 iter-3 — spa-born mass fence + template-literal honest skip ───────


def test_iter3_compliance_shape_schema_file_merge_stays_fenced(
    tmp_path: Path,
) -> None:
    """The wave-17 Soc0 'Compliance' annexation POIMENNO: a spa page
    merges (same key) with a bare backend MODEL file
    (``backend/models/compliance.py`` — schema source, prefixes=()). A
    file names a resource, not a subtree, so the anchor stays SPA-ONLY
    and fenced: a broad dev carrying foreign api mass never binds — the
    14.5K/100%-outside PF never forms."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        run_anchored_mint,
    )

    flow, dev, ctx_of = _mint_fixture()
    page = "frontend/src/pages/CompliancePage.tsx"
    model = "backend/models/compliance.py"
    api1 = "frontend/src/api/audit-events.ts"
    api2 = "frontend/src/api/event-routing.ts"
    for rel, n in ((page, 150), (model, 40), (api1, 200), (api2, 180)):
        _write_loc(tmp_path, rel, n)
    routes = [
        {"pattern": "/compliance", "method": "PAGE", "file": page,
         "kind": "spa-page"},
    ]
    broad = dev("api-compliance", [page, model, api1, api2],
                flows=[flow("compliance-flow", page)])
    pfs, tele = run_anchored_mint(
        [broad], routes, ctx_of(tmp_path, [page, model, api1, api2]))

    assert tele.get("spa_fence_blocked", 0) >= 1
    comp = [p for p in pfs if p.anchor_id == "route:compliance"]
    member_paths = {m.path for p in comp for m in (p.member_files or [])} | {
        q for p in comp for q in (p.paths or [])}
    assert api1 not in member_paths and api2 not in member_paths, (
        "a schema FILE merge must not lift the fence (no subtree)")


def test_iter3_spa_born_walk_rehomes_neighborhood(tmp_path: Path) -> None:
    """The wave-17 'findings' cascade POIMENNO: a spa page births a PF
    (winner formed only WITH the spa row — spa-BORN by the
    counterfactual), and a flowful NEIGHBOR dev's ancestor-walk
    plurality would pour into it. The walk must RE-VOTE without the
    spa-born target and land the neighbor on its flag-OFF-world home."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        run_anchored_mint,
    )

    flow, dev, ctx_of = _mint_fixture()
    core1 = "frontend/src/features/billing/a.tsx"
    core2 = "frontend/src/features/billing/b.tsx"
    core3 = "frontend/src/features/billing/c.tsx"
    page = "frontend/src/pages/WidgetsPage.tsx"
    stray1 = "frontend/src/pages/util-a.ts"
    stray2 = "frontend/src/pages/util-b.ts"
    for rel in (core1, core2, core3, page, stray1, stray2):
        _write_loc(tmp_path, rel, 200)
    routes = [
        {"pattern": "/widgets", "method": "PAGE", "file": page,
         "kind": "spa-page"},
    ]
    files = [core1, core2, core3, page, stray1, stray2]
    core = dev("billing", [core1, core2, core3],
               flows=[flow("billing-flow", core1)])
    widgets = dev("widgets", [page],
                  flows=[flow("browse-widgets", page)])
    stray = dev("api-stray", [stray1, stray2],
                flows=[flow("stray-flow", stray1)])
    pfs, tele = run_anchored_mint(
        [core, widgets, stray], routes, ctx_of(tmp_path, files))

    assert "route:widget" in (tele.get("spa_born_anchors") or []), (
        "the widgets mint exists only through the spa row — spa-born")
    by_name = {p.name: p for p in pfs}
    assert widgets.product_feature_id in by_name, (
        "the spa-born PF itself lives (its OWN winner binds)")
    # the neighbor re-homes to the OFF-world plurality target (core),
    # never the spa-born PF
    assert stray.product_feature_id == core.product_feature_id, (
        f"stray must re-home to core, got {stray.product_feature_id!r}")
    assert stray.product_feature_id != widgets.product_feature_id
    assert tele.get("spa_born_walk_skips", 0) >= 1


def test_iter3_template_literal_paths_honest_skip(
    tmp_path: Path, spa_on,
) -> None:
    """The wave-17 twenty SettingsRoutes exhibit POIMENNO: an
    INTERPOLATED template-literal path (JSX
    ``path={`${SettingsPath.GraphQLPlayground}`}`` and object
    ``path: `${ROUTES.SIGN_IN}/*```) is never a literal — the raw
    ``/${…}`` garbage minted parent-root settings anchors. Both
    grammars skip; a SIBLING literal route (the novu ``/onboarding``
    shape) survives."""
    files = [
        _rr_pkg(tmp_path, "package.json"),
        _write(tmp_path, "src/App.tsx", """
import { Routes, Route } from 'react-router-dom';
import { PlaygroundPage } from '@/pages/PlaygroundPage';
import { OnboardingPage } from '@/pages/OnboardingPage';
export const App = () => (
  <Routes>
    <Route path={`${SettingsPath.GraphQLPlayground}`} element={<PlaygroundPage />} />
    <Route path="/onboarding" element={<OnboardingPage />} />
  </Routes>
);
"""),
        _write(tmp_path, "src/router.ts", """
import { createBrowserRouter } from 'react-router-dom';
import { SignInPage } from '@/pages/SignInPage';
export const router = createBrowserRouter([
  { path: `${ROUTES.SIGN_IN}/*`, element: SignInPage },
  { path: '/pricing', element: SignInPage },
]);
"""),
        _write(tmp_path, "src/pages/PlaygroundPage.tsx",
               "export const PlaygroundPage = () => <div>p</div>;\n"),
        _write(tmp_path, "src/pages/OnboardingPage.tsx",
               "export const OnboardingPage = () => <div>o</div>;\n"),
        _write(tmp_path, "src/pages/SignInPage.tsx",
               "export const SignInPage = () => <div>s</div>;\n"),
    ]
    anchors = SpaRouterExtractor().extract(_ctx(tmp_path, files))
    routes = _routes_of(anchors)
    patterns = {r[0] for r in routes}
    assert not any("${" in p for p in patterns), (
        "interpolated template literals must never become routes")
    assert "/onboarding" in patterns, "sibling literal survives (novu)"
    assert "/pricing" in patterns, "object-grammar literal survives"


# ── fix-iteration 3 — panel grammar holes (root index + noise chain) ─────────


def test_iter3_root_index_emits_slash(tmp_path: Path, spa_on) -> None:
    """The flagship-page hole: a TOP-LEVEL ``pages/index.vue`` emits ``/``
    with the enclosing-package slug (hoppscotch REST page)."""
    files = [
        _vue_pkg(tmp_path, "packages/hoppscotch-common/package.json"),
        _write(tmp_path,
               "packages/hoppscotch-common/src/pages/index.vue",
               "<template/>"),
    ]
    anchors = SpaRouterExtractor().extract(_ctx(tmp_path, files))
    routes = _routes_of(anchors)
    assert ("/", "PAGE",
            "packages/hoppscotch-common/src/pages/index.vue") in routes
    assert {a.name for a in anchors} == {"hoppscotch-common"}


def test_iter3_noise_chain_dynamic_emits(tmp_path: Path, spa_on) -> None:
    """The double-dynamic hole: ``view/_id/_version.vue`` emits
    ``/view/:id/:version`` (noise-only static chain falls back to the
    enclosing package slug; the URL keeps the real segments)."""
    files = [
        _vue_pkg(tmp_path, "packages/hoppscotch-common/package.json"),
        _write(tmp_path,
               "packages/hoppscotch-common/src/pages/view/_id/_version.vue",
               "<template/>"),
    ]
    routes = _routes_of(SpaRouterExtractor().extract(_ctx(tmp_path, files)))
    assert ("/view/:id/:version", "PAGE",
            "packages/hoppscotch-common/src/pages/view/_id/_version.vue"
            ) in routes


def test_iter3_regressions_hold(tmp_path: Path, spa_on) -> None:
    """Existing grammar unchanged: nested index keeps the directory URL
    and its own slug; single dynamics keep their static-segment slug;
    the bare catch-all still never emits."""
    files = [
        _vue_pkg(tmp_path),
        _write(tmp_path, "src/pages/profile/index.vue", "<template/>"),
        _write(tmp_path, "src/pages/e/_id.vue", "<template/>"),
        _write(tmp_path, "src/pages/_.vue", "<template/>"),
    ]
    anchors = SpaRouterExtractor().extract(_ctx(tmp_path, files))
    routes = _routes_of(anchors)
    by_pattern = {r[0]: r for r in routes}
    assert set(by_pattern) == {"/profile", "/e/:id"}
    names = {a.name for a in anchors}
    assert names == {"profile", "e"}, "fallback never overrides real slugs"


def test_iter3_single_app_root_index_honest_skip(
    tmp_path: Path, spa_on,
) -> None:
    """A single-app repo (``pages/`` at the repo root) has NO enclosing
    package segment — the root index stays an honest skip."""
    files = [
        _vue_pkg(tmp_path),
        _write(tmp_path, "pages/index.vue", "<template/>"),
    ]
    assert SpaRouterExtractor().extract(_ctx(tmp_path, files)) == []
