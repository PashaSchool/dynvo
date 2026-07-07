"""W4 — interior sub-anchors: provenance, merge-widening, mint-bar fit.

Gate coverage: interior-anchor provenance (only ≥2-page PRODUCT
families become anchors), the never-mint-by-default posture (rank LAST,
same-key merge widens instead of twinning), and the existing-mint-bar
allowance (page evidence via ``page_route_files``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.pipeline_v2 import spine_anchors as sa
from faultline.pipeline_v2.stage_6_55_page_interior import (
    InteriorFamily,
    InteriorResult,
)


def _fam(family_dir: str, pages: tuple[str, ...],
         label: str = "Database Backups",
         dir_owned: bool = True) -> InteriorFamily:
    return InteriorFamily(
        family_dir=family_dir,
        component_names=("DatabaseBackups",),
        page_files=pages,
        source_files=(family_dir + "/index.tsx",),
        label=label,
        dir_owned=dir_owned,
    )


@pytest.fixture()
def interior(monkeypatch: pytest.MonkeyPatch):
    """Patch the Stage-6.55 feed with a deterministic fake result."""
    def _install(families: tuple[InteriorFamily, ...]) -> None:
        result = InteriorResult(active=True, families=families)
        monkeypatch.setattr(
            "faultline.pipeline_v2.stage_6_55_page_interior."
            "get_page_interiors",
            lambda ctx, routes_index: result,
        )
    return _install


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(repo_path=Path("."), tracked_files=[],
                           workspaces=None, cache_backend=None)


def test_family_becomes_interior_anchor(interior) -> None:  # noqa: ANN001
    pages = ("apps/studio/pages/database/backups.tsx",
             "apps/studio/pages/project/settings.tsx")
    interior((_fam("apps/studio/components/interfaces/Database", pages),))
    anchors = sa.build_spine_anchors([], [], _ctx())
    by_id = {a.canonical_id: a for a in anchors}
    a = by_id["interior:apps/studio/components/interfaces/Database"]
    assert a.source == "interior"
    assert a.key == "database"
    assert a.display == "Database Backups"          # label candidate
    assert a.page_route_files == frozenset(pages)   # page evidence
    assert a.rank == sa.SOURCE_RANK["interior"]     # LAST in near-ties


def test_same_key_merge_widens_route_anchor(interior) -> None:  # noqa: ANN001
    pages = ("apps/studio/pages/database/backups.tsx",
             "apps/studio/pages/database/pooling.tsx")
    interior((_fam("apps/studio/components/interfaces/Database", pages),))
    routes = [
        {"pattern": "/database/backups", "method": "PAGE",
         "file": "apps/studio/pages/database/backups.tsx"},
    ]
    anchors = sa.build_spine_anchors([], routes, _ctx())
    db = [a for a in anchors if a.key == "database"]
    # ONE capability — the route anchor widened by the interior subtree,
    # never a twin.
    assert len(db) == 1
    a = db[0]
    assert a.source == "route"                       # head = higher rank
    assert "apps/studio/components/interfaces/Database" in a.prefixes
    assert a.matches(
        "apps/studio/components/interfaces/Database/Backups/index.tsx")


def test_stoplisted_or_short_keys_never_anchor(interior) -> None:  # noqa: ANN001
    pages = ("a/pages/x.tsx", "a/pages/y.tsx")
    interior((
        _fam("apps/web/src/components", pages, label="Components"),
        _fam("apps/web/src/ui", pages, label="UI"),
    ))
    anchors = sa.build_spine_anchors([], [], _ctx())
    assert [a for a in anchors if a.source == "interior"] == []


def test_kill_switch(interior, monkeypatch) -> None:  # noqa: ANN001
    pages = ("a/pages/x.tsx", "a/pages/y.tsx")
    interior((_fam("apps/web/src/features/billing", pages),))
    monkeypatch.setenv("FAULTLINE_INTERIOR_ANCHORS", "0")
    anchors = sa.build_spine_anchors([], [], _ctx())
    assert [a for a in anchors if a.source == "interior"] == []


def test_inactive_result_yields_no_anchors(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_6_55_page_interior.get_page_interiors",
        lambda ctx, routes_index: InteriorResult(active=False, reason="x"),
    )
    anchors = sa.build_spine_anchors([], [], _ctx())
    assert [a for a in anchors if a.source == "interior"] == []


def test_mint_bar_accepts_page_evidence(tmp_path: Path) -> None:
    """The EXISTING Stage-6.86 bar adjudicates an interior anchor
    without any 6.86 change: page evidence passes the PAGE-SURFACE
    rule; stripping it fails ``api_only_surface``. (Read-only use of
    the bar — W3.2 owns the mint internals.)"""
    from datetime import datetime, timezone

    from faultline.models.types import Feature
    from faultline.pipeline_v2.stage_6_86_anchored_mint import _mint_bar

    dev = Feature(
        name="database-ui", display_name="database-ui",
        paths=["apps/studio/components/interfaces/Database/Backups/index.tsx"],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        health_score=90.0, layer="developer",
    )
    anchor = sa.SpineAnchor(
        canonical_id="interior:apps/studio/components/interfaces/Database",
        key="database", source="interior", display="Database",
        prefixes=("apps/studio/components/interfaces/Database",),
        sources=frozenset({"interior"}),
        page_route_files=frozenset({"apps/studio/pages/database/x.tsx",
                                    "apps/studio/pages/database/y.tsx"}),
    )
    ok = _mint_bar(anchor, [dev], {}, True, (".tsx",), tmp_path, {})
    assert ok is None  # page-hosted interior family may mint
    bare = sa.SpineAnchor(
        canonical_id=anchor.canonical_id, key=anchor.key, source="interior",
        display=anchor.display, prefixes=anchor.prefixes,
        sources=anchor.sources,
    )
    assert _mint_bar(bare, [dev], {}, True, (".tsx",), tmp_path, {}) \
        == "api_only_surface"


def test_file_family_claims_only_its_file(interior) -> None:  # noqa: ANN001
    """Container-sink guard (supabase interfaces smoke): a component
    living DIRECTLY in a container dir claims its own file, never the
    container subtree."""
    pages = ("a/pages/x.tsx", "a/pages/y.tsx")
    fam = InteriorFamily(
        family_dir="apps/studio/components/interfaces",
        component_names=("HomeOverview",),
        page_files=pages,
        source_files=("apps/studio/components/interfaces/HomeOverview.tsx",),
        label="Home Overview",
        dir_owned=False,
    )
    interior((fam,))
    anchors = sa.build_spine_anchors([], [], _ctx())
    ia = [a for a in anchors if a.source == "interior"]
    assert len(ia) == 1
    a = ia[0]
    assert a.prefixes == ()  # NEVER a container prefix claim
    assert a.files == frozenset(
        {"apps/studio/components/interfaces/HomeOverview.tsx"})
    assert a.key == "home-overview"  # keyed by the COMPONENT, not the dir


def test_families_collapse_to_finest_grain() -> None:
    """A barrel index at a container level must not swallow child
    families — nested dir families collapse to the finest grain."""
    from faultline.pipeline_v2.stage_6_55_page_interior import (
        InteriorNode,
        PageInterior,
        _build_families,
    )

    def node(name, src):  # noqa: ANN001
        return InteriorNode(
            kind="component", name=name, label=None,
            usage_line_start=1, usage_line_end=1, source_kind="local",
            provenance="product", source_file=src,
            def_line_start=1, def_line_end=10,
        )

    container_idx = "apps/web/components/interfaces/index.tsx"
    leaf_idx = "apps/web/components/interfaces/Database/index.tsx"
    pages = {
        f"apps/web/pages/{i}.tsx": PageInterior(
            file=f"apps/web/pages/{i}.tsx", page_kind="page",
            nodes=(node("Everything", container_idx),
                   node("Database", leaf_idx)),
        )
        for i in ("a", "b")
    }
    fams = _build_families(pages)
    dirs = {(f.family_dir, f.dir_owned) for f in fams}
    assert ("apps/web/components/interfaces/Database", True) in dirs
    # The container barrel family is DROPPED (contains the leaf claim).
    assert ("apps/web/components/interfaces", True) not in dirs
