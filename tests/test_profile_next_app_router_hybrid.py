"""NextAppRouterProfile — HYBRID ``pages/`` + ``app/`` tree support
(MISSION-92 recall-at-depth fix 2).

Next.js routes BOTH trees when a package carries a ``pages/`` root
alongside ``app/`` (app-over-pages precedence is per-conflicting-route,
not per-repo). A repo the App Router profile wins — e.g. a vestigial
``app/`` dir next to a large ``pages/`` surface (the supabase-studio
class) — previously produced ZERO routes/flows for the whole pages
surface. Fixtures are SYNTHETIC framework-convention trees (never
corpus paths — ``rule-no-repo-specific-paths``).

Covers: the hybrid Stage-1 route extractor (stock output byte-identical
on pure App Router trees; pages buckets merged in on hybrid trees; the
app-shell rule; slug-collision union), ``flow_entries`` seeding both
surfaces, ``feature_of`` alignment with the pages buckets, and the
override merge seam.
"""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2.extractors.route import RouteFileExtractor
from faultline.pipeline_v2.profiles.next_app_router import NextAppRouterProfile
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_1_extractors import merge_profile_extractors


# ── fixture helpers ──────────────────────────────────────────────────────────


def _write(root: Path, files: dict[str, str]) -> list[str]:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return sorted(files)


def _ctx(root: Path, files: dict[str, str]) -> ScanContext:
    tracked = _write(root, files)
    return ScanContext(
        repo_path=root,
        stack="next-app-router",
        monorepo=False,
        workspaces=None,
        tracked_files=tracked,
        commits=[],
        audited_stack=None,
    )


_PAGE = "export default function Screen() {\n  return null;\n}\n"
_ROUTE = "export async function GET(req) {}\n"

#: A hybrid single-package tree: a small (vestigial) App Router surface
#: plus the dominant Pages-Router surface — the supabase-studio SHAPE
#: (synthetic names).
_HYBRID_FILES = {
    "package.json": '{"name": "studio", "dependencies": {"next": "14.0.0"}}',
    "next.config.js": "module.exports = {};\n",
    # app/ tree (vestigial but real)
    "app/(misc)/health/page.tsx": _PAGE,
    "app/api/status/route.ts": _ROUTE,
    # pages/ tree — the dominant surface
    "pages/_app.tsx": "export default function A() {}\n",
    "pages/_document.tsx": "export default function D() {}\n",
    "pages/404.tsx": "export default function NF() {}\n",
    "pages/index.tsx": _PAGE,
    "pages/database/tables.tsx": _PAGE,
    "pages/database/functions.tsx": _PAGE,
    "pages/auth/users.tsx": _PAGE,
    "pages/auth/settings/[id].tsx": _PAGE,
    "pages/api/profile.ts": "export default function handler(req, res) {}\n",
    # slug collision with the app tree: pages/health/* joins app health
    "pages/health/report.tsx": _PAGE,
    # shared (never routed)
    "components/Button.tsx": "export const B = 1;\n",
    "lib/pages/not-a-router.ts": "export const x = 1;\n",
}

#: Pure App Router tree — the hybrid arm must be a no-op here.
_PURE_FILES = {
    "package.json": '{"name": "web", "dependencies": {"next": "14.0.0"}}',
    "next.config.js": "module.exports = {};\n",
    "app/(dashboard)/billing/page.tsx": _PAGE,
    "app/(dashboard)/team/page.tsx": _PAGE,
    "app/api/webhooks/route.ts": _ROUTE,
    "components/Button.tsx": "export const B = 1;\n",
    # a deep dir merely NAMED pages is not a router root
    "lib/pages/util.ts": "export const x = 1;\n",
}


def _route_extractor(profile: NextAppRouterProfile, ctx: ScanContext):
    merged = merge_profile_extractors([RouteFileExtractor()], profile, ctx)
    assert len(merged) == 1 and merged[0].name == "route"
    return merged[0]


# ── hybrid tree: both surfaces extracted ────────────────────────────────────


def test_hybrid_extractor_emits_pages_buckets(tmp_path: Path) -> None:
    profile = NextAppRouterProfile()
    ctx = _ctx(tmp_path, _HYBRID_FILES)
    cands = _route_extractor(profile, ctx).extract(ctx)
    by_name = {c.name: c for c in cands}
    # pages/ buckets surfaced ("api" is a noise segment, so the api page
    # buckets under its filename stem — stock next-pages behaviour)
    assert set(by_name) >= {"database", "auth", "profile", "health", "status"}
    assert "pages/database/tables.tsx" in by_name["database"].paths
    assert "pages/database/functions.tsx" in by_name["database"].paths
    assert "pages/auth/users.tsx" in by_name["auth"].paths
    assert "pages/auth/settings/[id].tsx" in by_name["auth"].paths


def test_hybrid_extractor_strips_shell_and_ignores_fake_root(tmp_path: Path) -> None:
    profile = NextAppRouterProfile()
    ctx = _ctx(tmp_path, _HYBRID_FILES)
    cands = _route_extractor(profile, ctx).extract(ctx)
    all_paths = {p for c in cands for p in c.paths}
    assert "pages/_app.tsx" not in all_paths
    assert "pages/_document.tsx" not in all_paths
    assert "pages/404.tsx" not in all_paths           # app-shell rule
    assert "lib/pages/not-a-router.ts" not in all_paths  # not a router root
    assert not any(c.name in ("404", "500") for c in cands)


def test_hybrid_slug_collision_unions_paths(tmp_path: Path) -> None:
    """A pages bucket whose slug the app tree also emits (health) UNIONS
    paths instead of duplicating the anchor."""
    profile = NextAppRouterProfile()
    ctx = _ctx(tmp_path, _HYBRID_FILES)
    cands = _route_extractor(profile, ctx).extract(ctx)
    health = [c for c in cands if c.name == "health"]
    assert len(health) == 1
    assert "app/(misc)/health/page.tsx" in health[0].paths
    assert "pages/health/report.tsx" in health[0].paths


def test_hybrid_flow_entries_seed_both_surfaces(tmp_path: Path) -> None:
    profile = NextAppRouterProfile()
    ctx = _ctx(tmp_path, _HYBRID_FILES)
    entries = profile.flow_entries(ctx)
    by_path = {}
    for e in entries:
        by_path.setdefault(e.path, []).append(e)
    # app surface unchanged
    assert "app/(misc)/health/page.tsx" in by_path
    assert "app/api/status/route.ts" in by_path
    # pages surface now seeds entries
    assert by_path["pages/database/tables.tsx"][0].kind == "page"
    assert by_path["pages/database/tables.tsx"][0].route == "/database/tables"
    assert by_path["pages/auth/settings/[id].tsx"][0].route == "/auth/settings/:id"
    assert by_path["pages/api/profile.ts"][0].kind == "http"
    # shell files seed nothing
    assert "pages/_app.tsx" not in by_path
    assert "pages/404.tsx" not in by_path


def test_hybrid_feature_of_aligns_with_pages_buckets(tmp_path: Path) -> None:
    profile = NextAppRouterProfile()
    ctx = _ctx(tmp_path, _HYBRID_FILES)
    cands = _route_extractor(profile, ctx).extract(ctx)
    owned = {p: c.name for c in cands for p in c.paths if p.startswith("pages/")}
    assert owned  # sanity: pages files are anchored
    for path, slug in owned.items():
        assert profile.feature_of(path, ctx) == slug
    # shell + shared stay unowned
    assert profile.feature_of("pages/_app.tsx", ctx) is None
    assert profile.feature_of("components/Button.tsx", ctx) is None
    # app-tree ownership unchanged
    assert profile.feature_of("app/(misc)/health/page.tsx", ctx) == "health"


# ── pure App Router tree: byte-identical no-op ──────────────────────────────


def test_pure_app_router_extraction_byte_identical(tmp_path: Path) -> None:
    """No accepted pages root → the hybrid extractor returns the STOCK
    output (same candidates, same order) — G4 inertness for every
    already-pinned App Router repo."""
    profile = NextAppRouterProfile()
    ctx = _ctx(tmp_path, _PURE_FILES)
    stock = RouteFileExtractor().extract(ctx)
    hybrid = _route_extractor(profile, ctx).extract(ctx)
    assert [(c.name, c.paths, c.source, c.confidence_self) for c in hybrid] \
        == [(c.name, c.paths, c.source, c.confidence_self) for c in stock]


def test_pure_app_router_flow_entries_unchanged(tmp_path: Path) -> None:
    profile = NextAppRouterProfile()
    ctx = _ctx(tmp_path, _PURE_FILES)
    entries = profile.flow_entries(ctx)
    assert {e.path for e in entries} == {
        "app/(dashboard)/billing/page.tsx",
        "app/(dashboard)/team/page.tsx",
        "app/api/webhooks/route.ts",
    }


def test_pure_app_router_feature_of_unchanged(tmp_path: Path) -> None:
    profile = NextAppRouterProfile()
    ctx = _ctx(tmp_path, _PURE_FILES)
    assert profile.feature_of("app/(dashboard)/billing/page.tsx", ctx) == "billing"
    assert profile.feature_of("lib/pages/util.ts", ctx) is None
    assert profile.feature_of("components/Button.tsx", ctx) is None
