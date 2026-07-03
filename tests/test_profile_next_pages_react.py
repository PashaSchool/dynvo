"""NextPagesReactProfile — detects() grades, pages buckets, SPA routes.

StackProfile Phase B profile #3 (``profiles/next_pages_react.py``).
Fixtures are SYNTHETIC framework-convention trees (never corpus paths):
detection fingerprints per grade (incl. the bare-tag guard, the
app-over-pages precedence, the framework-mode react-router exclusion,
and the embedded-frontend guard — a ``go.mod``-rooted repo with a React
webui must score 0.0), the pages bucket index + app-shell rule, the
``feature_of`` ↔ extractor-anchor name alignment contract, react-router
route-element resolution (lazy imports, wrapper elements), page-URL
derivation (``pageExtensions``, dynamic segments, api pages), and the
Stage-1 override merge seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.profiles.base import FileRole
from faultline.pipeline_v2.profiles.next_pages_react import (
    NextPagesReactProfile,
    ReactRouterSpaExtractor,
    _ProfileActivatedPagesRouteExtractor,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_1_extractors import merge_profile_extractors


# ── fixture helpers ──────────────────────────────────────────────────────────


def _write(root: Path, files: dict[str, str]) -> list[str]:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return sorted(files)


def _ctx(
    root: Path,
    files: dict[str, str],
    *,
    stack: str | None = "js-generic",
    audited: str | None = None,
) -> ScanContext:
    tracked = _write(root, files)
    return ScanContext(
        repo_path=root,
        stack=stack,
        monorepo=False,
        workspaces=None,
        tracked_files=tracked,
        commits=[],
        audited_stack=audited,
    )


_PKG_NEXT = '{"name": "web", "dependencies": {"next": "13.5.6"}}'
_PKG_SPA = (
    '{"name": "spa", "dependencies": {"react": "^19", '
    '"react-router-dom": "^6.30.0"}}'
)
_PAGE = "export default function Dashboard() {\n  return null;\n}\n"

_APP_JSX = (
    'import { Routes, Route, BrowserRouter } from "react-router-dom";\n'
    'import Dashboard from "./pages/Dashboard";\n'
    'import Lazy from "./primitives/LazyPage";\n'
    'const Settings = lazyWithRetry(() => import("./pages/Settings"));\n'
    "function App() {\n"
    "  return (\n"
    "    <BrowserRouter>\n"
    "      <Routes>\n"
    '        <Route path="/dashboard/:id" element={<Dashboard />} />\n'
    "        <Route\n"
    '          path="/settings"\n'
    "          element={<Lazy Page={Settings} />}\n"
    "        />\n"
    '        <Route element={<Lazy Page={Settings} />} />\n'
    "      </Routes>\n"
    "    </BrowserRouter>\n"
    "  );\n"
    "}\n"
    "export default App;\n"
)

_MAIN_TSX = (
    'import { createRoot } from "react-dom/client";\n'
    'import App from "./App";\n'
    'createRoot(document.getElementById("root")).render(<App />);\n'
)


def _pages_fixture(tmp_path: Path, **kwargs) -> ScanContext:  # noqa: ANN003
    return _ctx(tmp_path, {
        "package.json": '{"name": "root", "private": true}',
        "apps/web/package.json": _PKG_NEXT,
        "apps/web/next.config.js": "module.exports = {};\n",
        "apps/web/src/pages/_app.tsx": "export default function A() {}\n",
        "apps/web/src/pages/_document.tsx": "export default function D() {}\n",
        "apps/web/src/pages/404.tsx": "export default function NF() {}\n",
        "apps/web/src/pages/index.tsx": _PAGE,
        "apps/web/src/pages/dashboard.tsx": _PAGE,
        "apps/web/src/pages/settings/profile.tsx": _PAGE,
        "apps/web/src/pages/settings/billing.tsx": _PAGE,
        "apps/web/src/pages/users/[id].tsx": _PAGE,
        "apps/web/src/pages/api/webhooks/stripe.ts": (
            "export default function handler(req, res) {}\n"
        ),
        "apps/web/src/components/Button.tsx": "export const B = 1;\n",
        "apps/web/src/lib/db.ts": "export const db = 1;\n",
    }, **kwargs)


def _spa_fixture(tmp_path: Path, **kwargs) -> ScanContext:  # noqa: ANN003
    return _ctx(tmp_path, {
        "package.json": _PKG_SPA,
        "vite.config.ts": "export default {};\n",
        "index.html": "<div id='root'></div>",
        "src/main.tsx": _MAIN_TSX,
        "src/App.jsx": _APP_JSX,
        "src/pages/Dashboard.jsx": _PAGE,
        "src/pages/Settings.jsx": _PAGE,
        "src/primitives/LazyPage.jsx": "export default function L() {}\n",
    }, **kwargs)


# ── detects() fingerprint grades ─────────────────────────────────────────────


def test_detects_tag_plus_structure_is_strongest(tmp_path: Path) -> None:
    ctx = _pages_fixture(tmp_path, stack="next-pages")
    assert NextPagesReactProfile().detects(ctx) == pytest.approx(0.95)


def test_detects_bare_tag_without_structure_is_zero(tmp_path: Path) -> None:
    """The litestar lesson: a stack tag with NO structural confirmation
    (no pages tree, no next dep) must never win a selection."""
    ctx = _ctx(tmp_path, {
        "package.json": '{"name": "x"}',
        "src/x.ts": "export const V = 1;\n",
    }, stack="next-pages")
    assert NextPagesReactProfile().detects(ctx) == 0.0


def test_detects_structural_pages_is_090(tmp_path: Path) -> None:
    """Keyless monorepo mis-tag (``js-generic``): the whole-tree
    structural fingerprint claims the repo anyway."""
    ctx = _pages_fixture(tmp_path)
    assert NextPagesReactProfile().detects(ctx) == pytest.approx(0.9)


def test_detects_page_extensions_convention(tmp_path: Path) -> None:
    """Next ``pageExtensions: ['page.tsx']`` (the dittofeed shape)."""
    ctx = _ctx(tmp_path, {
        "package.json": _PKG_NEXT,
        "src/pages/_app.page.tsx": "export default function A() {}\n",
        "src/pages/broadcasts.page.tsx": _PAGE,
        "src/pages/journeys.page.tsx": _PAGE,
    })
    assert NextPagesReactProfile().detects(ctx) == pytest.approx(0.9)


def test_detects_defers_to_app_router_tree(tmp_path: Path) -> None:
    """Next's own precedence: a real App Router tree anywhere means the
    repo belongs to ``next-app-router`` — pages remnants (the cal.com
    shape) must not fire the structural grade."""
    ctx = _pages_fixture(tmp_path)
    extra = tmp_path / "apps/web/app/dashboard/page.tsx"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("export default function P() {}\n", encoding="utf-8")
    ctx.tracked_files = sorted(
        [*ctx.tracked_files, "apps/web/app/dashboard/page.tsx"],
    )
    assert NextPagesReactProfile().detects(ctx) == 0.0


def test_detects_ignores_example_app_router_tree(tmp_path: Path) -> None:
    """An App Router EXAMPLE (``examples/``, ``example-apps/``) is
    scaffolding, not the product — the pages grade still fires."""
    ctx = _pages_fixture(tmp_path)
    for rel in (
        "examples/supabase/app/account/page.tsx",
        "example-apps/demo/app/x/page.tsx",
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export default function P() {}\n", encoding="utf-8")
    ctx.tracked_files = sorted([
        *ctx.tracked_files,
        "examples/supabase/app/account/page.tsx",
        "example-apps/demo/app/x/page.tsx",
    ])
    assert NextPagesReactProfile().detects(ctx) == pytest.approx(0.9)


def test_detects_workspace_fraction(tmp_path: Path) -> None:
    from faultline.pipeline_v2.stage_0_intake import Workspace

    ctx = _ctx(tmp_path, {"package.json": '{"name": "x"}'})
    ctx.workspaces = [
        Workspace(name="dash", path="packages/dash", stack="next-pages"),
        Workspace(name="api", path="packages/api", stack="express"),
    ]
    score = NextPagesReactProfile().detects(ctx)
    assert score == pytest.approx(0.6 + 0.35 * 0.5)


def test_detects_workspace_grade_defers_to_app_router(tmp_path: Path) -> None:
    """ANY App-Router-tagged workspace hands the repo to next-app-router."""
    from faultline.pipeline_v2.stage_0_intake import Workspace

    ctx = _ctx(tmp_path, {"package.json": '{"name": "x"}'})
    ctx.workspaces = [
        Workspace(name="dash", path="packages/dash", stack="next-pages"),
        Workspace(name="web", path="apps/web", stack="next-app-router"),
    ]
    assert NextPagesReactProfile().detects(ctx) == 0.0


def test_detects_react_router_spa_is_085(tmp_path: Path) -> None:
    ctx = _spa_fixture(tmp_path, stack="vite")
    assert NextPagesReactProfile().detects(ctx) == pytest.approx(0.85)


def test_detects_react_router_framework_mode_is_excluded(
    tmp_path: Path,
) -> None:
    """``@react-router/*`` / ``react-router.config.*`` is the Remix
    successor (file routing) — a DIFFERENT stack, never claimed here."""
    files = {
        "package.json": (
            '{"name": "x", "dependencies": {"react-router": "^7"},'
            ' "devDependencies": {"@react-router/dev": "^7"}}'
        ),
        "react-router.config.ts": "export default {};\n",
        "app/routes/home.tsx": "export default function H() {}\n",
        "src/App.tsx": _APP_JSX,
    }
    ctx = _ctx(tmp_path, files)
    assert NextPagesReactProfile().detects(ctx) == 0.0


def test_detects_embedded_webui_is_zero(tmp_path: Path) -> None:
    """The traefik lesson: a ``go.mod``-rooted repo with a react-router
    webui is that backend's product — the SPA grades never fire."""
    ctx = _ctx(tmp_path, {
        "go.mod": "module example.com/daemon\n",
        "main.go": "package main\n",
        "webui/package.json": _PKG_SPA,
        "webui/vite.config.ts": "export default {};\n",
        "webui/index.html": "<div id='root'></div>",
        "webui/src/main.tsx": _MAIN_TSX,
        "webui/src/App.jsx": _APP_JSX,
        "webui/src/pages/Dashboard.jsx": _PAGE,
        "webui/src/pages/Settings.jsx": _PAGE,
    }, stack="go")
    assert NextPagesReactProfile().detects(ctx) == 0.0


def test_detects_render_entry_is_055(tmp_path: Path) -> None:
    """Vite + ReactDOM render entry WITHOUT router grammar: the weakest
    grade — deliberately below the 0.6 workspace floor of every
    framework-tag grade."""
    ctx = _ctx(tmp_path, {
        "package.json": '{"name": "x", "dependencies": {"react": "^19"}}',
        "vite.config.ts": "export default {};\n",
        "index.html": "<div id='root'></div>",
        "src/main.tsx": _MAIN_TSX,
        "src/App.tsx": "export default function App() { return null; }\n",
    }, stack="vite")
    assert NextPagesReactProfile().detects(ctx) == pytest.approx(0.55)


def test_detects_zero_on_python_repo(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "requirements.txt": "flask\n",
        "src/app.py": "def create_app():\n    return None\n",
    }, stack="flask")
    assert NextPagesReactProfile().detects(ctx) == 0.0


def test_detects_zero_on_next_app_router_repo(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "package.json": _PKG_NEXT,
        "next.config.js": "module.exports = {};\n",
        "app/dashboard/page.tsx": "export default function P() {}\n",
    }, stack="next-app-router")
    assert NextPagesReactProfile().detects(ctx) == 0.0


# ── pages route extractor (buckets + app-shell rule) ─────────────────────────


def _pages_anchors(ctx: ScanContext) -> dict[str, AnchorCandidate]:
    profile = NextPagesReactProfile()
    extractor = _ProfileActivatedPagesRouteExtractor(profile._pages)
    return {a.name: a for a in extractor.extract(ctx)}


def test_pages_extractor_buckets(tmp_path: Path) -> None:
    ctx = _pages_fixture(tmp_path)
    anchors = _pages_anchors(ctx)

    assert "settings" in anchors
    assert set(anchors["settings"].paths) == {
        "apps/web/src/pages/settings/profile.tsx",
        "apps/web/src/pages/settings/billing.tsx",
    }
    assert "dashboard" in anchors
    assert "users" in anchors
    # pages/api/<seg> buckets under the segment after the noise ``api``.
    assert "webhooks" in anchors
    for a in anchors.values():
        assert a.source == "route"


def test_pages_extractor_applies_app_shell_rule(tmp_path: Path) -> None:
    """``_app``/``_document``/``404`` never become or join a capability."""
    ctx = _pages_fixture(tmp_path)
    anchors = _pages_anchors(ctx)
    assert "404" not in anchors
    all_paths = {p for a in anchors.values() for p in a.paths}
    assert "apps/web/src/pages/_app.tsx" not in all_paths
    assert "apps/web/src/pages/_document.tsx" not in all_paths
    assert "apps/web/src/pages/404.tsx" not in all_paths


def test_pages_extractor_ignores_non_router_pages_dirs(
    tmp_path: Path,
) -> None:
    """A directory merely NAMED ``pages`` deeper in the source tree
    (``lib/pages/``) is not a Pages Router."""
    ctx = _ctx(tmp_path, {
        "package.json": _PKG_NEXT,
        "pages/_app.tsx": "export default function A() {}\n",
        "pages/checkout.tsx": _PAGE,
        "pages/cart.tsx": _PAGE,
        "lib/pages/render.ts": "export const r = 1;\n",
    })
    anchors = _pages_anchors(ctx)
    assert set(anchors) == {"checkout", "cart"}


# ── react-router SPA extractor ───────────────────────────────────────────────


def _spa_anchors(ctx: ScanContext) -> dict[str, AnchorCandidate]:
    profile = NextPagesReactProfile()
    return {
        a.name: a
        for a in ReactRouterSpaExtractor(profile._router).extract(ctx)
    }


def test_spa_extractor_resolves_route_branches(tmp_path: Path) -> None:
    ctx = _spa_fixture(tmp_path)
    anchors = _spa_anchors(ctx)

    # Direct element: <Route path="/dashboard/:id" element={<Dashboard/>}
    assert set(anchors["dashboard"].paths) == {"src/pages/Dashboard.jsx"}
    # Wrapper element + lazy import: element={<Lazy Page={Settings}/>}
    # resolves the INNER component (last resolvable ident), through the
    # dynamic-import binding.
    assert set(anchors["settings"].paths) == {"src/pages/Settings.jsx"}
    for a in anchors.values():
        assert a.source == "react-router-spa"


def test_spa_router_shell_never_a_capability(tmp_path: Path) -> None:
    ctx = _spa_fixture(tmp_path)
    all_paths = {p for a in _spa_anchors(ctx).values() for p in a.paths}
    assert "src/App.jsx" not in all_paths
    assert "src/main.tsx" not in all_paths


# ── feature_of ↔ anchor alignment ────────────────────────────────────────────


def test_feature_of_aligns_with_pages_anchors(tmp_path: Path) -> None:
    ctx = _pages_fixture(tmp_path)
    profile = NextPagesReactProfile()
    anchor_names = set(_pages_anchors(ctx))

    claimed = profile.feature_of(
        "apps/web/src/pages/settings/profile.tsx", ctx,
    )
    assert claimed == "settings"
    assert claimed in anchor_names  # byte-equal alignment contract
    assert profile.feature_of(
        "apps/web/src/pages/api/webhooks/stripe.ts", ctx,
    ) == "webhooks"
    # Shell + shared primitives → no opinion (fall through unchanged).
    assert profile.feature_of("apps/web/src/pages/_app.tsx", ctx) is None
    assert profile.feature_of("apps/web/src/pages/404.tsx", ctx) is None
    assert profile.feature_of("apps/web/src/components/Button.tsx", ctx) is (
        None
    )
    assert profile.feature_of("apps/web/src/lib/db.ts", ctx) is None


def test_feature_of_aligns_with_spa_anchors(tmp_path: Path) -> None:
    ctx = _spa_fixture(tmp_path)
    profile = NextPagesReactProfile()
    anchor_names = set(_spa_anchors(ctx))

    claimed = profile.feature_of("src/pages/Settings.jsx", ctx)
    assert claimed == "settings"
    assert claimed in anchor_names
    # The root router shell has no owner.
    assert profile.feature_of("src/App.jsx", ctx) is None
    assert profile.feature_of("src/main.tsx", ctx) is None


# ── flow entries ─────────────────────────────────────────────────────────────


def test_flow_entries_pages_urls(tmp_path: Path) -> None:
    ctx = _pages_fixture(tmp_path)
    entries = {
        (e.route, e.kind): e
        for e in NextPagesReactProfile().flow_entries(ctx)
    }
    assert ("/", "page") in entries
    assert ("/dashboard", "page") in entries
    assert ("/settings/profile", "page") in entries
    assert ("/users/:id", "page") in entries
    assert ("/api/webhooks/stripe", "http") in entries
    assert entries[("/dashboard", "page")].symbol == "Dashboard"
    # Shell files seed nothing.
    routes = {e.route for e in entries.values()}
    assert not any(r.endswith(("_app", "_document", "404")) for r in routes)


def test_flow_entries_page_extensions_url(tmp_path: Path) -> None:
    """The ``pageExtensions`` marker token is not part of the URL."""
    ctx = _ctx(tmp_path, {
        "package.json": _PKG_NEXT,
        "src/pages/_app.page.tsx": "export default function A() {}\n",
        "src/pages/broadcasts.page.tsx": _PAGE,
        "src/pages/api/index.page.ts": "export default function h() {}\n",
    })
    routes = {
        e.route: e.kind for e in NextPagesReactProfile().flow_entries(ctx)
    }
    assert routes == {"/broadcasts": "page", "/api": "http"}


def test_flow_entries_spa_routes(tmp_path: Path) -> None:
    ctx = _spa_fixture(tmp_path)
    entries = {
        e.route: e for e in NextPagesReactProfile().flow_entries(ctx)
    }
    assert entries["/dashboard/:id"].path == "src/pages/Dashboard.jsx"
    assert entries["/settings"].path == "src/pages/Settings.jsx"
    assert entries["/settings"].kind == "page"


# ── classify_file ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("path", "role"), [
    ("apps/web/src/pages/dashboard.tsx", FileRole.PAGE),
    ("apps/web/src/pages/api/webhooks/stripe.ts", FileRole.API),
    ("apps/web/src/pages/_app.tsx", FileRole.CONFIG),
    ("apps/web/src/pages/404.tsx", FileRole.CONFIG),
    ("src/components/Button.tsx", FileRole.COMPONENT),
    ("src/hooks/useAuth.ts", FileRole.HOOK),
    ("src/lib/db.ts", FileRole.LIB),
    ("src/redux/store.ts", FileRole.SERVICE),
    ("src/constant/appinfo.js", FileRole.DOMAIN),
    ("next.config.js", FileRole.CONFIG),
    ("vite.config.ts", FileRole.CONFIG),
    ("src/pages/__tests__/dashboard.test.tsx", FileRole.TEST),
    ("src/weird/thing.ts", FileRole.UNKNOWN),
])
def test_classify_file(path: str, role: FileRole) -> None:
    assert NextPagesReactProfile().classify_file(path) == role


# ── Stage-1 override merge seam ──────────────────────────────────────────────


class _StubExtractor:
    def __init__(self, name: str) -> None:
        self.name = name

    def extract(self, ctx: ScanContext) -> list[AnchorCandidate]:  # noqa: ARG002
        return []


def test_merge_profile_extractors_replaces_and_appends(
    tmp_path: Path,
) -> None:
    ctx = _pages_fixture(tmp_path)
    profile = NextPagesReactProfile()
    base = [_StubExtractor("route"), _StubExtractor("schema")]
    merged = merge_profile_extractors(base, profile, ctx)

    by_name = {e.name: e for e in merged}
    assert isinstance(
        by_name["route"], _ProfileActivatedPagesRouteExtractor,
    )
    assert isinstance(by_name["react-router-spa"], ReactRouterSpaExtractor)
    assert isinstance(by_name["schema"], _StubExtractor)  # untouched
    assert [e.name for e in merged] == ["route", "schema", "react-router-spa"]
