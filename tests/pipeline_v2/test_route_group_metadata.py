"""Product-Spine Wave 2a — route-group metadata carry (spec §4.2, RC4).

Stage 1 used to STRIP Next route-groups (``(marketing)`` / ``(dashboard)``)
as "organisational only", discarding the author's own surface declaration.
Wave 2a keeps the URL/slug semantics byte-identical but carries the group
NAMES as metadata:

  * ``AnchorCandidate.route_groups`` — groups observed on the anchor's
    routing paths (route extractor);
  * ``routes_index[].route_groups`` — per-route entry key, present ONLY
    when the route file sits under a group (groupless stacks unchanged).
"""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2.extractors.route import (
    RouteFileExtractor,
    route_groups_of,
)
from faultline.pipeline_v2.indexes import build_routes_index
from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── route_groups_of helper ─────────────────────────────────────────────


def test_route_groups_of_extracts_sorted_lowercase_names() -> None:
    assert route_groups_of(
        "apps/web/app/(Marketing)/pricing/page.tsx"
    ) == ("marketing",)
    assert route_groups_of(
        "app/(dashboard)/(settings)/billing/page.tsx"
    ) == ("dashboard", "settings")


def test_route_groups_of_skips_intercepting_markers_and_plain_paths() -> None:
    # ``(.)photo`` / ``(..)photo`` are intercepting-route markers, not groups.
    assert route_groups_of("app/feed/(.)photo/page.tsx") == ()
    assert route_groups_of("app/feed/(..)photo/page.tsx") == ()
    assert route_groups_of("src/app/api/users/route.ts") == ()
    assert route_groups_of("") == ()


# ── RouteFileExtractor carries groups on candidates ────────────────────


def _ctx(tmp_path: Path, files: list[str]) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack="next-app-router",
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
    )


def test_extractor_carries_route_groups_without_changing_slugs(
    tmp_path: Path,
) -> None:
    files = [
        "app/(marketing)/pricing/page.tsx",
        "app/(marketing)/blog/page.tsx",
        "app/(dashboard)/settings/page.tsx",
        "app/checkout/page.tsx",
    ]
    cands = RouteFileExtractor().extract(_ctx(tmp_path, files))
    by_name = {c.name: c for c in cands}
    # Slug semantics unchanged: groups still never become anchor names.
    assert "marketing" not in by_name and "dashboard" not in by_name
    assert by_name["pricing"].route_groups == ("marketing",)
    assert by_name["blog"].route_groups == ("marketing",)
    assert by_name["settings"].route_groups == ("dashboard",)
    # Groupless anchor carries the empty default.
    assert by_name["checkout"].route_groups == ()


def test_extractor_unions_groups_across_anchor_paths(tmp_path: Path) -> None:
    files = [
        "app/(marketing)/pricing/page.tsx",
        "app/(promo)/pricing/annual/page.tsx",
    ]
    cands = RouteFileExtractor().extract(_ctx(tmp_path, files))
    by_name = {c.name: c for c in cands}
    assert by_name["pricing"].route_groups == ("marketing", "promo")


# ── routes_index entries carry groups ──────────────────────────────────


class _Sig:
    """Duck-typed AnchorCandidate carrying only paths (fs-routing shape)."""

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        self.pattern = None
        self.routes = ()


def test_routes_index_entries_carry_route_groups_only_when_present() -> None:
    features = [
        {"uuid": "A" * 32, "paths": [
            "app/(marketing)/pricing/page.tsx",
            "app/checkout/page.tsx",
        ]},
    ]
    sig = _Sig(["app/(marketing)/pricing/page.tsx", "app/checkout/page.tsx"])
    routes = build_routes_index(features, {"route": [sig]})
    by_file = {r["file"]: r for r in routes}
    grouped = by_file["app/(marketing)/pricing/page.tsx"]
    plain = by_file["app/checkout/page.tsx"]
    assert grouped["route_groups"] == ["marketing"]
    # Groupless entry stays byte-identical (no key at all).
    assert "route_groups" not in plain
    # URL semantics unchanged: the group never appears in the pattern.
    assert "(marketing)" not in grouped["pattern"]
    assert "marketing" not in grouped["pattern"]
