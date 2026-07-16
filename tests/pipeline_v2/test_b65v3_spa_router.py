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
    to its URL set: 17 page routes. Honest skips, each by a standing law:
    root ``index.vue`` + the bare ``_.vue`` catch-all (no static segment to
    anchor on) and ``view/_id/_version.vue`` (its only static segment
    ``view`` is a universal noise token — the same law the stock route
    extractor applies; a per-repo exception would be magic tuning)."""
    prefix = "packages/hoppscotch-common/src/pages/"
    files = [_vue_pkg(tmp_path, "packages/hoppscotch-common/package.json")]
    for rel in _HOPP_COMMON_PAGES:
        files.append(_write(tmp_path, prefix + rel, "<template/>"))

    anchors = SpaRouterExtractor().extract(_ctx(tmp_path, files))
    routes = _routes_of(anchors)
    patterns = {r[0] for r in routes}

    assert patterns == {
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
    }
    # noise-token-only static segment -> honest skip (never an anchor)
    assert "/view/:id/:version" not in patterns
    # 17 file-rows (both /profile files kept — distinct (pattern, file)).
    assert len(routes) == 17
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
