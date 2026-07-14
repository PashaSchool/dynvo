"""B64 — dynamic-dispatch resolver (``FAULTLINE_DISPATCH_RESOLVER``, OFF).

Three additive mechanisms, each proven with its anti-cases and a
first-class flag-OFF byte-identity invariant:

  (a) lazy dynamic-import consts (``const X = lazy(() => import("./Y"))``)
      become ordinary Stage 6.3 import edges (subtree reachability);
  (b) one-level const-fold of PURE literal-returning route helpers/consts
      (``draftsPath()`` → ``"/drafts"``, ``{path: ROUTES.home}``) so
      react-router SPA routes whose path is a call/const/member resolve;
  (c) [measured via census — see the fix report] registry ``{key: X}``.

Anti-cases (free vars, call args, conditionals, concatenation, objects,
interpolated templates, multi-statement bodies) are an HONEST SKIP — no
edge and no route path is invented. The residual is the B63 metric.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.lazy_imports import (
    DISPATCH_RESOLVER_ENV,
    dispatch_resolver_enabled,
    ts_lazy_binding_specs,
)
from faultline.pipeline_v2.stage_6_3_import_tree import _extract_ts_imports
from faultline.pipeline_v2.profiles.next_pages_react import (
    ReactRouterSpaExtractor,
    NextPagesReactProfile,
    _RouterIndex,
    _extract_object_member,
    _extract_pure_def,
    _make_path_folder,
    _pure_string_value,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── flag plumbing ─────────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DISPATCH_RESOLVER_ENV, raising=False)
    assert dispatch_resolver_enabled() is False
    monkeypatch.setenv(DISPATCH_RESOLVER_ENV, "1")
    assert dispatch_resolver_enabled() is True
    # unset == "0" == "false" — all OFF (the never-worse flag law).
    for off in ("0", "false", "off", "no", ""):
        monkeypatch.setenv(DISPATCH_RESOLVER_ENV, off)
        assert dispatch_resolver_enabled() is False


# ── (a) lazy dynamic-import edges ─────────────────────────────────────────


_LAZY_SRC = (
    'import Foo from "./foo";\n'
    'const Drafts = lazy(() => import("~/scenes/Drafts"));\n'
    'const Home = lazyWithRetry(() => import("./scenes/Home"));\n'
    'const Api = createLazyComponent(() => import("./scenes/Api"));\n'
    "export function App(){ return <Route component={Drafts}/>; }\n"
)


def test_lazy_binding_specs() -> None:
    got = ts_lazy_binding_specs(_LAZY_SRC)
    assert got == {
        "Drafts": "~/scenes/Drafts",
        "Home": "./scenes/Home",
        "Api": "./scenes/Api",
    }
    # bare unbound dynamic import has no local name → not returned
    assert ts_lazy_binding_specs('import("./x");') == {}


def test_import_tree_off_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DISPATCH_RESOLVER_ENV, raising=False)
    # OFF: only the static import is bound — lazy consts are invisible.
    assert _extract_ts_imports(_LAZY_SRC) == {"Foo": "./foo"}


def test_import_tree_on_binds_lazy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DISPATCH_RESOLVER_ENV, "1")
    got = _extract_ts_imports(_LAZY_SRC)
    assert got == {
        "Foo": "./foo",
        "Drafts": "~/scenes/Drafts",
        "Home": "./scenes/Home",
        "Api": "./scenes/Api",
    }


def test_static_import_wins_over_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DISPATCH_RESOLVER_ENV, "1")
    # A re-exported name must never be repointed by a lazy binding.
    src = (
        'import Page from "./static/Page";\n'
        'const Page = lazy(() => import("./lazy/Page"));\n'
    )
    assert _extract_ts_imports(src)["Page"] == "./static/Page"


# ── (b) const-fold: pure-literal predicate + def extraction ───────────────


def test_pure_string_value() -> None:
    assert _pure_string_value('"/drafts"') == "/drafts"
    assert _pure_string_value("'/x'") == "/x"
    assert _pure_string_value("`/archive`") == "/archive"
    # anti-cases — everything below is NOT a pure literal → None
    assert _pure_string_value('"/a" + "/b"') is None
    assert _pure_string_value("`/doc/${slug}`") is None
    assert _pure_string_value('cond ? "/" : "/home"') is None
    assert _pure_string_value("ROUTES.home") is None
    assert _pure_string_value("helper()") is None


_HELPERS = (
    'export function draftsPath(): string { return "/drafts"; }\n'
    "export function archivePath(): string { return `/archive`; }\n"
    "export function homePath(): string "
    '{ return env.ROOT_SHARE_ID ? "/" : "/home"; }\n'
    "export function settingsPath(...a: string[]): string "
    '{ return "/settings" + (a.length ? "/x" : ""); }\n'
    "export function logoutPath() { return { pathname: '/' }; }\n"
    "export function documentPath(slug: string): string "
    "{ return `/doc/${slug}`; }\n"
    'export const HOME_ROUTE = "/home";\n'
    'export const arrowPath = (): string => "/arrow";\n'
    'export const ROUTES = { home: "/home", billing: "/settings/billing" };\n'
)


def test_extract_pure_def_folds_pure() -> None:
    assert _extract_pure_def(_HELPERS, "draftsPath") == "/drafts"
    assert _extract_pure_def(_HELPERS, "archivePath") == "/archive"
    assert _extract_pure_def(_HELPERS, "HOME_ROUTE") == "/home"
    assert _extract_pure_def(_HELPERS, "arrowPath") == "/arrow"


def test_extract_pure_def_anticases() -> None:
    # conditional on a free var
    assert _extract_pure_def(_HELPERS, "homePath") is None
    # concatenation + conditional + rest param
    assert _extract_pure_def(_HELPERS, "settingsPath") is None
    # returns an object, not a string
    assert _extract_pure_def(_HELPERS, "logoutPath") is None
    # interpolated template (free var)
    assert _extract_pure_def(_HELPERS, "documentPath") is None
    # undefined name
    assert _extract_pure_def(_HELPERS, "nope") is None


def test_extract_object_member() -> None:
    assert _extract_object_member(_HELPERS, "ROUTES", "home") == "/home"
    assert (
        _extract_object_member(_HELPERS, "ROUTES", "billing")
        == "/settings/billing"
    )
    assert _extract_object_member(_HELPERS, "ROUTES", "missing") is None
    assert _extract_object_member(_HELPERS, "NOPE", "home") is None


# ── (b) const-fold: _route_pairs OFF byte-identity + ON fold ──────────────


_ROUTES_SRC = (
    'import { draftsPath, homePath, settingsPath, ROUTES } '
    'from "./routeHelpers";\n'
    'const Drafts = lazy(() => import("./scenes/Drafts"));\n'
    'const Home = lazy(() => import("./scenes/Home"));\n'
    'const Billing = lazy(() => import("./scenes/Billing"));\n'
    "export function App(){ return (<Switch>\n"
    '  <Route exact path="/lit" component={Litpage} />\n'
    "  <Route exact path={draftsPath()} component={Drafts} />\n"
    "  <Route path={homePath()} component={Home} />\n"
    '  <Route path={settingsPath("templates")} component={Sett} />\n'
    "</Switch>); }\n"
    "const router = createBrowserRouter([\n"
    "  { path: ROUTES.billing, element: <Billing/> },\n"
    "  { path: homePath(), element: <Home/> },\n"
    "]);\n"
)


def _folder():
    return _make_path_folder(
        _HELPERS, {}, "r.tsx", frozenset(), None, None,
    )


def test_route_pairs_off_ignores_folder_arg() -> None:
    # folder=None ⇒ non-literal-path routes dropped exactly as pre-B64;
    # only the quoted literal survives.
    off = _RouterIndex._route_pairs(_ROUTES_SRC, None)
    assert ("/lit", "Litpage") in off
    assert all(p != "/drafts" for p, _ in off)


def test_route_pairs_on_folds_additively() -> None:
    off = _RouterIndex._route_pairs(_ROUTES_SRC, None)
    on = _RouterIndex._route_pairs(_ROUTES_SRC, _folder())
    # additive: every OFF pair survives ON
    assert all(pair in on for pair in off)
    added = sorted(set(p for p, _ in on) - set(p for p, _ in off))
    assert "/drafts" in added  # JSX pure helper folds
    assert "/settings/billing" in added  # route-object member folds
    # anti-cases: conditional + arg helpers never fold
    assert "/home" not in added
    assert not any("settings/x" in p or p == "/settings" for p in added)


# ── integration: SPA extractor OFF hides / ON surfaces helper routes ──────


def _ctx(root: Path, files: dict[str, str]) -> ScanContext:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return ScanContext(
        repo_path=root,
        stack="js-generic",
        monorepo=False,
        workspaces=None,
        tracked_files=sorted(files),
        commits=[],
        audited_stack=None,
    )


_PAGE = "export default function P(){ return null; }\n"

_SPA_FILES = {
    "package.json": (
        '{"name": "spa", "dependencies": {"react": "^19", '
        '"react-router-dom": "^6"}}'
    ),
    "src/routeHelpers.ts": (
        'export function draftsPath(): string { return "/drafts"; }\n'
        "export function homePath(): string "
        '{ return env.X ? "/" : "/home"; }\n'
    ),
    "src/App.tsx": (
        'import { draftsPath, homePath } from "./routeHelpers";\n'
        'const Drafts = lazy(() => import("./scenes/Drafts"));\n'
        'const Home = lazy(() => import("./scenes/Home"));\n'
        "export default function App(){ return (\n"
        "  <Routes>\n"
        "    <Route path={draftsPath()} element={<Drafts/>} />\n"
        "    <Route path={homePath()} element={<Home/>} />\n"
        "  </Routes>); }\n"
    ),
    "src/scenes/Drafts.tsx": _PAGE,
    "src/scenes/Home.tsx": _PAGE,
    "src/main.tsx": (
        'import { createRoot } from "react-dom/client";\n'
        'import App from "./App";\n'
        "createRoot(document.getElementById('root')).render(<App/>);\n"
    ),
}


def _spa_anchors(ctx: ScanContext) -> dict:
    profile = NextPagesReactProfile()
    return {
        a.name: a
        for a in ReactRouterSpaExtractor(profile._router).extract(ctx)
    }


def test_spa_off_drops_helper_path_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DISPATCH_RESOLVER_ENV, raising=False)
    # B62 flip isolation: ROUTER_ALIAS_RESOLVE defaults ON since KEY_SCHEMA
    # 29; this test's world is the alias-arm-OFF one, so pin explicitly.
    monkeypatch.setenv("FAULTLINE_ROUTER_ALIAS_RESOLVE", "0")
    ctx = _ctx(tmp_path, _SPA_FILES)
    anchors = _spa_anchors(ctx)
    # Both routes use a helper-fn path ⇒ dropped ⇒ scenes invisible.
    assert "drafts" not in anchors
    assert "src/scenes/Drafts.tsx" not in {
        p for a in anchors.values() for p in a.paths
    }


def test_spa_on_folds_pure_helper_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DISPATCH_RESOLVER_ENV, "1")
    # B62 flip isolation: ROUTER_ALIAS_RESOLVE defaults ON since KEY_SCHEMA
    # 29; this test's world is the alias-arm-OFF one, so pin explicitly.
    monkeypatch.setenv("FAULTLINE_ROUTER_ALIAS_RESOLVE", "0")
    ctx = _ctx(tmp_path, _SPA_FILES)
    anchors = _spa_anchors(ctx)
    # draftsPath() folds ⇒ Drafts scene surfaces under the "drafts" branch
    # with a route tuple; the conditional homePath() route is skipped.
    assert "drafts" in anchors
    assert set(anchors["drafts"].paths) == {"src/scenes/Drafts.tsx"}
    assert ("/drafts", "PAGE", "src/scenes/Drafts.tsx") in tuple(
        anchors["drafts"].routes
    )
    # homePath is a conditional ⇒ no "home" route bucket from folding.
    assert "home" not in anchors


# ── (b2) hygiene iteration — wave-17 outline adjudication fixes ───────────


def test_template_interpolation_folds_when_pure() -> None:
    f = _folder()
    # every ${…} folds -> literal path
    assert f("`${draftsPath()}/changesets`") == "/drafts/changesets"
    # unfoldable interpolation (conditional helper) -> honest ""
    assert f("`${homePath()}/:tab?`") == ""
    # free-var interpolation -> ""
    assert f("`/doc/${slug}`") == ""


def test_route_slug_interp_segment_dynamic() -> None:
    from faultline.pipeline_v2.profiles.next_pages_react import _route_slug
    # OFF law (default): interpolation text minted a garbage slug
    assert _route_slug("${debugPath()}/changesets") == "debug-path"
    # B64 law: ${…} segment is DYNAMIC like :param — next static wins
    assert _route_slug("${debugPath()}/changesets", skip_interp=True) == (
        "changesets"
    )
    # all-dynamic path -> no slug
    assert _route_slug("${homePath()}/:tab?", skip_interp=True) == ""


_HYGIENE_FILES = {
    "package.json": (
        '{"name": "spa", "dependencies": {"react": "^19", '
        '"react-router-dom": "^6"}}'
    ),
    "src/routeHelpers.ts": (
        'export function debugPath(): string { return "/debug"; }\n'
        "export function searchPath({q}: {q?: string} = {}): string "
        '{ return "/search" + (q ? "?q=" + q : ""); }\n'
    ),
    "src/App.tsx": (
        'import { debugPath, searchPath } from "./routeHelpers";\n'
        'const Search = lazy(() => import("./scenes/Search"));\n'
        'const Changesets = lazy(() => import("./scenes/Changesets"));\n'
        'const Error404 = lazy(() => import("./scenes/Error404"));\n'
        "export default function App(){ return (\n"
        "  <Routes>\n"
        "    <Route path={`${debugPath()}/changesets`} element={<Changesets/>} />\n"
        "    <Route path={`${searchPath()}/:query?`} element={<Search/>} />\n"
        '    <Route path="/404" element={<Error404/>} />\n'
        "  </Routes>); }\n"
    ),
    "src/scenes/Search/index.tsx": _PAGE,
    "src/scenes/Changesets.tsx": _PAGE,
    "src/scenes/Error404.tsx": _PAGE,
    "src/main.tsx": (
        'import { createRoot } from "react-dom/client";\n'
        'import App from "./App";\n'
        "createRoot(document.getElementById('root')).render(<App/>);\n"
    ),
}


def test_hygiene_rules_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DISPATCH_RESOLVER_ENV, "1")
    ctx = _ctx(tmp_path, _HYGIENE_FILES)
    anchors = _spa_anchors(ctx)
    # template folds -> honest 'debug' branch holds Changesets
    assert "debug" in anchors
    assert set(anchors["debug"].paths) == {"src/scenes/Changesets.tsx"}
    # unfoldable searchPath + index-file stem -> IDENT names the branch
    assert "search" in anchors
    assert set(anchors["search"].paths) == {"src/scenes/Search/index.tsx"}
    # garbage interp slugs never mint
    assert "debug-path" not in anchors
    assert "search-path" not in anchors
    # error-route shell rule: 404 never a capability
    assert "404" not in anchors
    assert not any(
        "Error404" in p for a in anchors.values() for p in a.paths
    )


def test_hygiene_rules_off_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DISPATCH_RESOLVER_ENV, raising=False)
    # B62 flip isolation: ROUTER_ALIAS_RESOLVE defaults ON since KEY_SCHEMA
    # 29; this test's world is the alias-arm-OFF one, so pin explicitly.
    monkeypatch.setenv("FAULTLINE_ROUTER_ALIAS_RESOLVE", "0")
    ctx = _ctx(tmp_path, _HYGIENE_FILES)
    anchors = _spa_anchors(ctx)
    # Pre-B64 behaviour preserved: raw template text mints its face-value
    # slugs and 404 mints (the OFF-world debt is untouched by the flag).
    assert "debug-path" in anchors
    assert "404" in anchors
