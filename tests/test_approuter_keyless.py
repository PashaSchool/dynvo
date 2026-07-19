"""App-Router keyless route extractor (S4-a).

The App-Router keyless evidence gap: an ``app/**/page.tsx`` + ``app/**/route.ts``
tree never reaches ``routes_index`` when its scope is not cleanly
``next-app-router``-tagged — the monorepo residue (a ``next-pages`` sibling
unit makes the composite replace the stock ``route`` source, dropping the
App-Router residue) and the polyglot leftover (a non-workspace ``web/``
scanned with the ``js-generic`` root tag). The fix is a dedicated
convention-keyed extractor (source ``route-approuter``) folded LAST into
``routes_index`` so it only ADDS.

Fixtures are SYNTHETIC framework-convention trees (never corpus paths —
``rule-no-repo-specific-paths``). NAMED exhibits:
  * cal-shape — apps/web (App Router) + apps/api/v1 (Pages Router) monorepo:
    the composite-replace drop.
  * onyx-shape — polyglot Python backend + non-workspace web/src/app tree:
    the leftover-tag drop.

NAMED anti-cases (must survive):
  * ``(marketing)`` route-group classification is UNCHANGED (group dropped
    from the URL, carried as ``route_groups`` metadata).
  * no twin-extractor — a CLEAN ``next-app-router`` repo emits each app route
    exactly ONCE with the flag ON (no duplicate ``(pattern, method, file)``
    rows), byte-identical to the flag-OFF stock ``route`` rows.
  * kill-switch — flag OFF / ``=0`` -> extractor inert AND unregistered ->
    ``routes_index`` byte-identical to pre-S4a.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.extractors.approuter_keyless import (
    APPROUTER_SOURCE,
    AppRouterKeylessExtractor,
    approuter_keyless_enabled,
)
from faultline.pipeline_v2.indexes import (
    build_routes_index,
    derive_app_router_route,
)
from faultline.pipeline_v2.profiles._per_unit import select_scan_profile
from faultline.pipeline_v2.stage_0_intake import ScanContext, stage_0_intake
from faultline.pipeline_v2.stage_1_extractors import stage_1_extractors
from faultline.pipeline_v2.stage_1_per_workspace import (
    run_stage_1_per_workspace,
    should_activate_per_workspace,
)

_PAGE = "export default function Screen() {\n  return null;\n}\n"
_ROUTE = "export async function GET(req) {}\n"


# ── fixture helpers ──────────────────────────────────────────────────────────


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _ctx(root: Path, stack: str | None) -> ScanContext:
    tracked = sorted(
        str(p.relative_to(root)).replace("\\", "/")
        for p in root.rglob("*")
        if p.is_file()
    )
    return ScanContext(
        repo_path=root,
        stack=stack,
        monorepo=False,
        workspaces=None,
        tracked_files=tracked,
        commits=[],
        audited_stack=None,
    )


def _routes_index_for(repo: Path) -> list[dict]:
    """Mirror phase_extract: profile-selected Stage 1 (per-workspace or
    global) -> routes_index. Deterministic, no LLM."""
    ctx = stage_0_intake(repo, skip_git=True)
    profile = select_scan_profile(ctx)
    if should_activate_per_workspace(ctx):
        res = run_stage_1_per_workspace(ctx, profile=profile)
        stage1 = (
            res.stage1_out
            if res.workspaces_used
            else stage_1_extractors(ctx, profile=profile)
        )
    else:
        stage1 = stage_1_extractors(ctx, profile=profile)
    return build_routes_index([], stage1)


def _under(rows: list[dict], prefix: str) -> list[dict]:
    return [r for r in rows if str(r.get("file", "")).startswith(prefix)]


# ── deriver mechanism ────────────────────────────────────────────────────────


def test_derive_app_router_route_conventions() -> None:
    d = derive_app_router_route
    # monorepo prefix transparent; route group dropped from the URL
    assert d("apps/web/app/(marketing)/pricing/page.tsx") == ("/pricing", "PAGE")
    # src/app root; dynamic segment -> :param
    assert d("web/src/app/chat/[chatId]/page.tsx") == ("/chat/:chatId", "PAGE")
    # route.ts handler -> GET (verb-unknown read default)
    assert d("apps/web/app/api/teams/[id]/route.ts") == ("/api/teams/:id", "GET")
    # root page
    assert d("app/page.tsx") == ("/", "PAGE")


def test_derive_app_router_route_rejects_non_app_router() -> None:
    d = derive_app_router_route
    # Pages Router (a DIFFERENT root) never resolves here
    assert d("apps/api/v1/pages/api/teams/[id]/_get.ts") is None
    assert d("src/pages/dashboard.tsx") is None
    # ``app`` INSIDE a pages tree is owned by Pages Router, not App Router
    assert d("web/src/pages/app/page.tsx") is None
    # a plain shared component is not a route
    assert d("packages/ui/src/components/Button.tsx") is None


# ── extractor mechanism + kill-switch ────────────────────────────────────────


def test_extractor_inert_when_flag_off(tmp_path: Path, monkeypatch) -> None:
    # MECHANICAL flip migration (2026-07-19 S*-pack, KEY_SCHEMA 32): flag-OFF
    # is now the explicit kill-switch (unset arms the extractor).
    monkeypatch.setenv("FAULTLINE_APPROUTER_KEYLESS", "0")
    _write(tmp_path, {"apps/web/app/bookings/page.tsx": _PAGE})
    ctx = _ctx(tmp_path, stack="js-generic")
    assert approuter_keyless_enabled() is False
    assert AppRouterKeylessExtractor().extract(ctx) == []


@pytest.mark.parametrize("off_value", ["0", "false", "off", ""])
def test_kill_switch_values_keep_extractor_inert(
    tmp_path: Path, monkeypatch, off_value: str,
) -> None:
    monkeypatch.setenv("FAULTLINE_APPROUTER_KEYLESS", off_value)
    _write(tmp_path, {"apps/web/app/bookings/page.tsx": _PAGE})
    ctx = _ctx(tmp_path, stack="js-generic")
    assert AppRouterKeylessExtractor().extract(ctx) == []


def test_extractor_emits_app_router_routes_when_armed(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_APPROUTER_KEYLESS", "1")
    _write(tmp_path, {
        # js-generic scope (the leftover-tag drop) still yields routes:
        "web/src/app/bookings/page.tsx": _PAGE,
        "web/src/app/settings/profile/page.tsx": _PAGE,
        "web/src/app/api/teams/[id]/route.ts": _ROUTE,
        # layout is NOT a routes_index surface (YAML suffix gate)
        "web/src/app/bookings/layout.tsx": _PAGE,
        # not App Router — excluded
        "web/src/components/Button.tsx": "export const B = 1;\n",
    })
    ctx = _ctx(tmp_path, stack="js-generic")
    cands = AppRouterKeylessExtractor().extract(ctx)
    assert cands, "armed extractor must emit candidates on js-generic scope"
    assert all(c.source == APPROUTER_SOURCE for c in cands)
    routes = {(pat, meth, f) for c in cands for (pat, meth, f) in c.routes}
    assert ("/bookings", "PAGE", "web/src/app/bookings/page.tsx") in routes
    assert ("/settings/profile", "PAGE",
            "web/src/app/settings/profile/page.tsx") in routes
    assert ("/api/teams/:id", "GET",
            "web/src/app/api/teams/[id]/route.ts") in routes
    # layout.tsx must NOT produce a row
    assert not any("layout.tsx" in f for (_p, _m, f) in routes)


# ── anti-case: (marketing) route-group classification unchanged ──────────────


def test_marketing_route_group_classification_unchanged(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_APPROUTER_KEYLESS", "1")
    _write(tmp_path, {"apps/web/app/(marketing)/pricing/page.tsx": _PAGE})
    ctx = _ctx(tmp_path, stack="js-generic")
    cands = AppRouterKeylessExtractor().extract(ctx)
    ri = build_routes_index([], {APPROUTER_SOURCE: cands})
    assert len(ri) == 1
    row = ri[0]
    # group is URL-invisible …
    assert row["pattern"] == "/pricing"
    # … but its NAME is carried as route-group metadata (Wave-2a law)
    assert row.get("route_groups") == ["marketing"]


# ── anti-case: no twin-extractor on a clean next-app-router repo ─────────────


_CLEAN_APP_REPO = {
    "package.json": '{"name": "web", "dependencies": {"next": "14.0.0"}}',
    "next.config.js": "module.exports = {};\n",
    "app/(dashboard)/billing/page.tsx": _PAGE,
    "app/(dashboard)/settings/page.tsx": _PAGE,
    "app/bookings/[id]/page.tsx": _PAGE,
    "app/api/health/route.ts": _ROUTE,
    "app/layout.tsx": _PAGE,
    "components/Button.tsx": "export const B = 1;\n",
}


def test_no_twin_rows_on_clean_next_app_router_repo(
    tmp_path: Path, monkeypatch,
) -> None:
    """A CLEAN next-app-router repo (stock ``route`` pass fires): the flag-ON
    routes_index must be byte-identical to flag-OFF — the approuter rows fold
    LAST and dedup against the stock rows, so no twin and no drift."""
    _write(tmp_path, _CLEAN_APP_REPO)

    # MECHANICAL flip migration (flip32): OFF leg = explicit kill-switch.
    monkeypatch.setenv("FAULTLINE_APPROUTER_KEYLESS", "0")
    off = _routes_index_for(tmp_path)

    monkeypatch.setenv("FAULTLINE_APPROUTER_KEYLESS", "1")
    on = _routes_index_for(tmp_path)

    def _key(r: dict) -> tuple:
        return (r["pattern"], r["method"], r["file"])

    off_keys = [_key(r) for r in off]
    on_keys = [_key(r) for r in on]
    # stock pass already covered the app tree -> no NEW rows, no duplicates
    assert sorted(on_keys) == sorted(off_keys)
    assert len(on_keys) == len(set(on_keys)), "duplicate (twin) rows emitted"
    # and the app routes are actually present (the fixture is real)
    assert ("/billing", "PAGE", "app/(dashboard)/billing/page.tsx") in off_keys


# ── end-to-end exhibit: cal-shape (composite-replace drop) ───────────────────


_CAL_SHAPE = {
    "pnpm-workspace.yaml": "packages:\n  - apps/*\n",
    "package.json": '{"name": "cal-shape", "private": true}',
    # apps/web — Next App Router (the residue that gets dropped)
    "apps/web/package.json": '{"name": "web", "dependencies": {"next": "14.0.0"}}',
    "apps/web/next.config.js": "module.exports = {};\n",
    "apps/web/app/bookings/page.tsx": _PAGE,
    "apps/web/app/(marketing)/pricing/page.tsx": _PAGE,
    "apps/web/app/teams/[id]/page.tsx": _PAGE,
    "apps/web/app/api/webhook/route.ts": _ROUTE,
    # apps/api — Next Pages Router (the sibling that makes the composite
    # replace the stock ``route`` source)
    "apps/api/package.json": '{"name": "api", "dependencies": {"next": "14.0.0"}}',
    "apps/api/pages/api/users/[id].ts": "export default function h(q, r) {}\n",
    "apps/api/pages/api/teams/index.ts": "export default function h(q, r) {}\n",
}


def test_cal_shape_app_router_residue_recovered(
    tmp_path: Path, monkeypatch,
) -> None:
    _write(tmp_path, _CAL_SHAPE)

    # MECHANICAL flip migration (flip32): OFF leg = explicit kill-switch.
    monkeypatch.setenv("FAULTLINE_APPROUTER_KEYLESS", "0")
    off = _routes_index_for(tmp_path)
    monkeypatch.setenv("FAULTLINE_APPROUTER_KEYLESS", "1")
    on = _routes_index_for(tmp_path)

    off_web = _under(off, "apps/web/app/")
    on_web = _under(on, "apps/web/app/")
    # the drop: the composite replaced the stock route source, so OFF has 0
    assert len(off_web) == 0
    # armed: the App-Router residue is recovered
    assert len(on_web) >= 3
    on_routes = {(r["pattern"], r["method"]) for r in on_web}
    assert ("/bookings", "PAGE") in on_routes
    assert ("/pricing", "PAGE") in on_routes  # route-group dropped from URL
    assert ("/teams/:id", "PAGE") in on_routes
    # entry-conservation: every OFF row survives byte-identically in ON
    off_keys = {(r["pattern"], r["method"], r["file"]) for r in off}
    on_keys = {(r["pattern"], r["method"], r["file"]) for r in on}
    assert off_keys <= on_keys


# ── end-to-end exhibit: onyx-shape (leftover-tag drop) ───────────────────────


_ONYX_SHAPE = {
    # polyglot: a Python backend at the root (js-generic root package.json)
    "pyproject.toml": "[project]\nname = 'onyx-shape'\n",
    "package.json": '{"name": "onyx-shape", "workspaces": ["widget"]}',
    "backend/onyx/main.py": "app = object()\n",
    # a declared workspace that is NOT the frontend
    "widget/package.json": '{"name": "widget"}',
    "widget/index.ts": "export const w = 1;\n",
    # web/ — Next App Router, NOT a declared workspace -> leftover pass
    "web/package.json": '{"name": "web", "dependencies": {"next": "14.0.0"}}',
    "web/next.config.js": "module.exports = {};\n",
    "web/src/app/chat/page.tsx": _PAGE,
    "web/src/app/admin/settings/page.tsx": _PAGE,
    "web/src/app/api/query/route.ts": _ROUTE,
}


def test_onyx_shape_leftover_app_router_recovered(
    tmp_path: Path, monkeypatch,
) -> None:
    _write(tmp_path, _ONYX_SHAPE)

    # MECHANICAL flip migration (flip32): OFF leg = explicit kill-switch.
    monkeypatch.setenv("FAULTLINE_APPROUTER_KEYLESS", "0")
    off = _routes_index_for(tmp_path)
    monkeypatch.setenv("FAULTLINE_APPROUTER_KEYLESS", "1")
    on = _routes_index_for(tmp_path)

    off_web = _under(off, "web/src/app/")
    on_web = _under(on, "web/src/app/")
    # the drop: leftover pass ran the stock route with the js-generic root tag
    assert len(off_web) == 0
    # armed: recovered via the convention
    assert len(on_web) >= 3
    on_routes = {(r["pattern"], r["method"]) for r in on_web}
    assert ("/chat", "PAGE") in on_routes
    assert ("/admin/settings", "PAGE") in on_routes
    assert ("/api/query", "GET") in on_routes
    off_keys = {(r["pattern"], r["method"], r["file"]) for r in off}
    on_keys = {(r["pattern"], r["method"], r["file"]) for r in on}
    assert off_keys <= on_keys
