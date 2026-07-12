"""B44 — React-Router-framework / Remix profile + SPA alias resolution.

Two Stage-3 keyless-surface mechanisms, both flag-gated (default OFF ⇒
byte-identical):

  1. ``ReactRouterFrameworkProfile`` (``FAULTLINE_REACT_ROUTER_FW_PROFILE``)
     — detects framework mode (``react-router.config.*`` / ``@react-router/*``
     / ``@remix-run/*`` + ``app/routes/**``) and seeds ONE flow per route
     file so a documenso-``apps/remix``-shaped unit stops shipping flow-less.
  2. SPA alias resolution (``FAULTLINE_ROUTER_ALIAS_RESOLVE``) — resolves
     ``~/`` / ``@/`` against the tsconfig-declared root (outline: ``~/`` →
     ``app/``) instead of a hard-coded ``src/``, and stamps
     ``AnchorCandidate.routes`` so react-router SPA branches populate
     ``routes_index``.

Fixtures are SYNTHETIC framework-convention trees (never corpus paths);
every mechanism has an anti-case that MUST stay inert (the classic
``react-router-dom`` library SPA and a Next App Router repo must NOT be
claimed by the framework profile; a ``src/``-aliased SPA and the flag-off
path must resolve byte-identically).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.profiles.base import FileRole
from faultline.pipeline_v2.profiles.next_pages_react import (
    NextPagesReactProfile,
    ReactRouterSpaExtractor,
    _RouterIndex,
)
from faultline.pipeline_v2.profiles.react_router_fw import (
    ReactRouterFrameworkProfile,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext


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


_PKG_FW = (
    '{"name": "remix-app", "dependencies": {"react-router": "^7.1.0"}, '
    '"devDependencies": {"@react-router/dev": "^7.1.0"}}'
)
_RR_CONFIG = (
    "import type { Config } from '@react-router/dev/config';\n"
    "export default { appDirectory: 'app', ssr: true } satisfies Config;\n"
)
_ROUTE_PAGE = "export default function DashboardPage() {\n  return null;\n}\n"
_ROUTE_NAMED = "export function InboxPage() {\n  return null;\n}\n"


# ── 1. framework-profile detection ───────────────────────────────────────────


def test_detects_framework_mode_via_config(tmp_path: Path) -> None:
    """``react-router.config.*`` + ``app/routes/`` tree → 0.9."""
    ctx = _ctx(tmp_path, {
        "package.json": '{"name": "x", "dependencies": {"react-router": "^7"}}',
        "react-router.config.ts": _RR_CONFIG,
        "app/routes/dashboard.tsx": _ROUTE_PAGE,
    })
    assert ReactRouterFrameworkProfile().detects(ctx) == pytest.approx(0.9)


def test_detects_framework_mode_via_dep(tmp_path: Path) -> None:
    """``@react-router/*`` dep + ``app/routes/`` tree → 0.9 (no config file)."""
    ctx = _ctx(tmp_path, {
        "package.json": _PKG_FW,
        "app/routes/dashboard.tsx": _ROUTE_PAGE,
    })
    assert ReactRouterFrameworkProfile().detects(ctx) == pytest.approx(0.9)


def test_detects_zero_without_routes_tree(tmp_path: Path) -> None:
    """A framework dep but NO ``app/routes/`` tree → 0.0 (never wins)."""
    ctx = _ctx(tmp_path, {
        "package.json": _PKG_FW,
        "app/root.tsx": _ROUTE_PAGE,
    })
    assert ReactRouterFrameworkProfile().detects(ctx) == 0.0


def test_anticase_library_spa_not_claimed(tmp_path: Path) -> None:
    """ANTI-CASE: a classic ``react-router-dom`` library SPA (in-source
    ``<Route>``, no framework config/dep, no ``app/routes/`` files) must
    score 0.0 — it belongs to NextPagesReactProfile, not this profile."""
    ctx = _ctx(tmp_path, {
        "package.json": (
            '{"name": "spa", "dependencies": {"react-router-dom": "^6.30"}}'
        ),
        "src/App.tsx": (
            'import { Routes, Route } from "react-router-dom";\n'
            "export default function App() { return <Routes />; }\n"
        ),
    })
    assert ReactRouterFrameworkProfile().detects(ctx) == 0.0


def test_anticase_next_app_router_not_claimed(tmp_path: Path) -> None:
    """ANTI-CASE: a Next App Router repo (``app/page.tsx``, next dep, no
    ``app/routes/`` file convention) must score 0.0."""
    ctx = _ctx(tmp_path, {
        "package.json": '{"name": "web", "dependencies": {"next": "14"}}',
        "app/page.tsx": _ROUTE_PAGE,
        "app/dashboard/page.tsx": _ROUTE_PAGE,
    })
    assert ReactRouterFrameworkProfile().detects(ctx) == 0.0


# ── 2. framework-profile flow entries ────────────────────────────────────────


def test_flow_entries_one_per_route_with_symbol(tmp_path: Path) -> None:
    """One entry per ``app/routes/**`` file; api segment → http; the
    default (and B41 named) export resolves the anchor symbol."""
    ctx = _ctx(tmp_path, {
        "package.json": _PKG_FW,
        "react-router.config.ts": _RR_CONFIG,
        "app/routes/_authenticated+/dashboard.tsx": _ROUTE_PAGE,
        "app/routes/inbox.tsx": _ROUTE_NAMED,
        "app/routes/api+/webhook.ts": "export const action = () => null;\n",
    })
    entries = ReactRouterFrameworkProfile().flow_entries(ctx)
    by_path = {e.path: e for e in entries}
    assert set(by_path) == {
        "app/routes/_authenticated+/dashboard.tsx",
        "app/routes/inbox.tsx",
        "app/routes/api+/webhook.ts",
    }
    assert by_path["app/routes/_authenticated+/dashboard.tsx"].symbol == (
        "DashboardPage"
    )
    # B41 named-export fallback recovers a symbol on a non-default page.
    assert by_path["app/routes/inbox.tsx"].symbol == "InboxPage"
    assert by_path["app/routes/api+/webhook.ts"].kind == "http"
    assert by_path["app/routes/inbox.tsx"].kind == "page"


def test_anticase_shell_and_layout_seed_nothing(tmp_path: Path) -> None:
    """ANTI-CASE: the app shell (``root``/``entry.*``), the flat-routes
    config (``routes.ts``), and pathless ``_layout`` wrappers are framework
    wiring — they must seed NO flow entries."""
    ctx = _ctx(tmp_path, {
        "package.json": _PKG_FW,
        "react-router.config.ts": _RR_CONFIG,
        "app/root.tsx": _ROUTE_PAGE,
        "app/routes.ts": "export default [];\n",
        "app/entry.client.tsx": _ROUTE_PAGE,
        "app/entry.server.tsx": _ROUTE_PAGE,
        "app/routes/_layout.tsx": _ROUTE_PAGE,
        "app/routes/_authenticated+/_layout.tsx": _ROUTE_PAGE,
        "app/routes/_index.tsx": _ROUTE_PAGE,  # index route — DOES seed
    })
    paths = {e.path for e in ReactRouterFrameworkProfile().flow_entries(ctx)}
    assert paths == {"app/routes/_index.tsx"}
    prof = ReactRouterFrameworkProfile()
    # Pathless layout wrappers under the routes tree classify as CONFIG
    # (shell), never PAGE; the navigable index route stays PAGE.
    assert prof.classify_file("app/routes/_layout.tsx") == FileRole.CONFIG
    assert prof.classify_file("app/routes/_index.tsx") == FileRole.PAGE


# ── 3. SPA alias resolution (the outline ~/ → app/ fix) ───────────────────────


def _spa_alias_files() -> dict[str, str]:
    """A Vite SPA that maps ``~/`` → ``./app/`` (outline shape): the router
    lives under ``app/`` (NOT ``src/``) and mounts a lazily-imported page
    through the ``~/`` alias."""
    return {
        "package.json": (
            '{"name": "outlineish", "dependencies": '
            '{"react-router-dom": "^6.30"}}'
        ),
        "tsconfig.json": '{"compilerOptions": {"paths": {"~/*": ["./app/*"]}}}',
        "app/routes/index.tsx": (
            'import { Switch } from "react-router-dom";\n'
            'import Route from "~/components/ProfiledRoute";\n'
            'const Dash = lazy(() => import("~/scenes/Dash"));\n'
            "export default function Routes() {\n"
            "  return <Switch><Route exact path=\"/dash\" component={Dash} />"
            "</Switch>;\n}\n"
        ),
        "app/scenes/Dash.tsx": _ROUTE_PAGE,
        "app/components/ProfiledRoute.tsx": "export default function R() {}\n",
    }


def test_alias_resolves_declared_root_when_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag ON: ``~/scenes/Dash`` resolves against the declared ``app/``
    root → a real bucket + a route tuple for ``routes_index``."""
    monkeypatch.setenv("FAULTLINE_ROUTER_ALIAS_RESOLVE", "1")
    ctx = _ctx(tmp_path, _spa_alias_files())
    idx = _RouterIndex(ctx)
    assert idx.buckets, "alias-resolved SPA must produce at least one bucket"
    assert any(
        "app/scenes/Dash.tsx" in paths for paths in idx.buckets.values()
    )
    # Route tuples feed build_routes_index Pass A.
    all_routes = [r for rs in idx.route_tuples_by_slug.values() for r in rs]
    assert ("/dash", "PAGE", "app/scenes/Dash.tsx") in all_routes


def test_anticase_alias_off_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTI-CASE: flag OFF ⇒ the ``~/`` → ``app/`` resolution does NOT
    happen (``src/``-only) and NO route tuples are recorded — the pre-B44
    empty-board behaviour is preserved byte-identically."""
    monkeypatch.delenv("FAULTLINE_ROUTER_ALIAS_RESOLVE", raising=False)
    ctx = _ctx(tmp_path, _spa_alias_files())
    idx = _RouterIndex(ctx)
    assert idx.buckets == {}
    assert idx.route_tuples_by_slug == {}
    # The router file IS still recognised (grammar unchanged) — only the
    # resolution of its aliased component fails, exactly as before B44.
    assert idx.router_files


def test_anticase_src_aliased_spa_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTI-CASE: a conventional ``@/`` → ``src/`` SPA resolves to the SAME
    bucket whether the flag is ON (via the tsconfig map) or OFF (via the
    legacy ``src/`` heuristic) — no regression for src-rooted repos."""
    files = {
        "package.json": (
            '{"name": "srcspa", "dependencies": {"react-router-dom": "^6.30"}}'
        ),
        "tsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}',
        "src/App.tsx": (
            'import { Switch, Route } from "react-router-dom";\n'
            'const Dash = lazy(() => import("@/scenes/Dash"));\n'
            "export default function App() {\n"
            "  return <Switch><Route exact path=\"/dash\" component={Dash} />"
            "</Switch>;\n}\n"
        ),
        "src/scenes/Dash.tsx": _ROUTE_PAGE,
    }
    monkeypatch.delenv("FAULTLINE_ROUTER_ALIAS_RESOLVE", raising=False)
    off = _RouterIndex(_ctx(tmp_path / "off", files)).buckets
    monkeypatch.setenv("FAULTLINE_ROUTER_ALIAS_RESOLVE", "1")
    on = _RouterIndex(_ctx(tmp_path / "on", files)).buckets
    assert off == on
    assert any("src/scenes/Dash.tsx" in p for p in off.values())


def test_spa_extractor_routes_gated_by_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SPA extractor carries ``.routes`` when ON, empty when OFF."""
    prof = NextPagesReactProfile()
    ctx = _ctx(tmp_path, _spa_alias_files())

    monkeypatch.setenv("FAULTLINE_ROUTER_ALIAS_RESOLVE", "1")
    prof._router_index = None  # bust the single-slot memo
    cands_on = ReactRouterSpaExtractor(prof._router).extract(ctx)
    assert cands_on and any(c.routes for c in cands_on)

    monkeypatch.delenv("FAULTLINE_ROUTER_ALIAS_RESOLVE", raising=False)
    prof._router_index = None
    cands_off = ReactRouterSpaExtractor(prof._router).extract(ctx)
    # OFF ⇒ no buckets resolved for this ``app/``-aliased SPA ⇒ no anchors.
    assert all(not c.routes for c in cands_off)
