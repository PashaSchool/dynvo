"""Tests for Stage 8.6 — universal non-source scaffold/docs drop.

Synthetic, neutral fixture names only (per memory/rule-no-repo-specific-
paths). Verifies the all-or-nothing predicate, the extensionless→source
conservatism, schema-source treatment, and Layer-2 reconciliation.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature
from faultline.pipeline_v2.stage_8_6_nonsource_drop import (
    _path_is_source,
    drop_all_nonsource_features,
    drop_phantom_product_features,
    reconcile_product_features,
)


def _feat(name: str, paths: list[str], *, layer: str = "developer",
          product_feature_id: str | None = None) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=100.0,
        layer=layer,
        product_feature_id=product_feature_id,
    )


# ── _path_is_source unit cases ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("alpha/beta.ts", True),
        ("alpha/beta.tsx", True),
        ("schema.prisma", True),       # schema is source
        ("db/migrate.sql", True),
        ("api/types.graphql", True),
        ("ui/theme.css", True),
        ("packages/widget", True),      # bare dir → source (rule 1)
        ("packages/widget/", True),     # trailing slash → source
        ("Dockerfile", True),
        ("alpha/Makefile", True),
        (".gitignore", True),           # dotfile, no real ext → source (conservative)
        ("docs/guide.md", False),
        ("config/data.json", False),
        ("notes.txt", False),
        ("certs/server.pem", False),
        ("assets/logo.png", False),
        ("pnpm-lock.yaml", False),
    ],
)
def test_path_is_source(path: str, expected: bool) -> None:
    assert _path_is_source(path) is expected


# ── drop predicate (all-or-nothing) ─────────────────────────────────────────


def test_all_markdown_drops() -> None:
    f = _feat("docs-only", ["a.md", "b.md", "guide.md"])
    kept, dropped = drop_all_nonsource_features([f])
    assert kept == []
    assert dropped == ["docs-only"]


def test_markdown_plus_one_ts_keeps() -> None:
    f = _feat("mixed", ["a.md", "core.ts"])
    kept, dropped = drop_all_nonsource_features([f])
    assert [k.name for k in kept] == ["mixed"]
    assert dropped == []


def test_bare_dir_keeps() -> None:
    f = _feat("bare", ["packages/widget"])
    kept, dropped = drop_all_nonsource_features([f])
    assert [k.name for k in kept] == ["bare"]
    assert dropped == []


def test_all_prisma_keeps() -> None:
    f = _feat("schema-only", ["prisma/schema.prisma"])
    kept, dropped = drop_all_nonsource_features([f])
    assert [k.name for k in kept] == ["schema-only"]
    assert dropped == []


def test_all_cert_json_txt_drops() -> None:
    f = _feat("junk", ["certs/s.pem", "config/x.json", "notes.txt"])
    kept, dropped = drop_all_nonsource_features([f])
    assert kept == []
    assert dropped == ["junk"]


def test_empty_paths_not_dropped() -> None:
    f = _feat("no-paths", [])
    kept, dropped = drop_all_nonsource_features([f])
    assert [k.name for k in kept] == ["no-paths"]
    assert dropped == []


def test_product_features_untouched_by_drop() -> None:
    pf = _feat("Some Product", ["a.md"], layer="product")
    kept, dropped = drop_all_nonsource_features([pf])
    assert [k.name for k in kept] == ["Some Product"]
    assert dropped == []


def test_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_8_6_NONSOURCE_DROP", "0")
    f = _feat("docs-only", ["a.md"])
    kept, dropped = drop_all_nonsource_features([f])
    assert [k.name for k in kept] == ["docs-only"]
    assert dropped == []


# ── Layer-2 reconciliation ──────────────────────────────────────────────────


def test_reconcile_recomputes_paths_and_drops_empty() -> None:
    # Two dev features both pointed at product "P1"; one survives.
    survivor = _feat("alpha", ["alpha/core.ts"], product_feature_id="P1")
    # P2 has no surviving members → must be dropped.
    p1 = _feat("P1", ["alpha/core.ts", "beta/old.ts"], layer="product")
    p2 = _feat("P2", ["beta/old.ts"], layer="product")

    kept_pfs, telem = reconcile_product_features([survivor], [p1, p2])
    names = {pf.name for pf in kept_pfs}
    assert names == {"P1"}
    # P1's paths recomputed to surviving member union.
    p1_out = next(pf for pf in kept_pfs if pf.name == "P1")
    assert p1_out.paths == ["alpha/core.ts"]
    assert telem["recomputed"] == 1
    assert telem["dropped_empty"] == 1


# ── drop_phantom_product_features (always-on empty-PF drop) ──────────────────


def test_phantom_pf_with_no_members_is_dropped() -> None:
    # "P1" has a dev member; "P2" (a phantom) has none — its paths are already
    # owned by P1's member. P2 must be dropped, P1 kept untouched.
    dev = _feat("alpha", ["alpha/core.ts"], product_feature_id="P1")
    p1 = _feat("P1", ["alpha/core.ts"], layer="product")
    p2 = _feat("P2", ["alpha/core.ts", "beta/x.ts"], layer="product")

    kept, dropped = drop_phantom_product_features([dev], [p1, p2])
    assert {pf.name for pf in kept} == {"P1"}
    assert dropped == 1
    # Paths of the survivor are NOT recomputed (path-preserving, unlike reconcile).
    assert next(pf for pf in kept if pf.name == "P1").paths == ["alpha/core.ts"]


def test_phantom_drop_keeps_all_when_every_pf_has_members() -> None:
    d1 = _feat("alpha", ["alpha/core.ts"], product_feature_id="P1")
    d2 = _feat("beta", ["beta/core.ts"], product_feature_id="P2")
    p1 = _feat("P1", ["alpha/core.ts"], layer="product")
    p2 = _feat("P2", ["beta/core.ts"], layer="product")

    kept, dropped = drop_phantom_product_features([d1, d2], [p1, p2])
    assert {pf.name for pf in kept} == {"P1", "P2"}
    assert dropped == 0


def test_phantom_drop_handles_no_dev_features() -> None:
    # No developer features at all → every product feature is phantom.
    p1 = _feat("P1", ["x.ts"], layer="product")
    kept, dropped = drop_phantom_product_features([], [p1])
    assert kept == []
    assert dropped == 1


def test_phantom_drop_ignores_dev_features_without_pfid() -> None:
    # A dev feature with no product_feature_id can't keep any PF alive.
    orphan = _feat("alpha", ["alpha/core.ts"], product_feature_id=None)
    p1 = _feat("P1", ["alpha/core.ts"], layer="product")
    kept, dropped = drop_phantom_product_features([orphan], [p1])
    assert kept == []
    assert dropped == 1


def test_phantom_drop_is_path_preserving_and_order_stable() -> None:
    # Two real PFs + one phantom interleaved — survivors keep original order.
    d1 = _feat("a", ["a.ts"], product_feature_id="P1")
    d2 = _feat("b", ["b.ts"], product_feature_id="P3")
    p1 = _feat("P1", ["a.ts", "z.ts"], layer="product")
    p2 = _feat("P2", ["ghost.ts"], layer="product")
    p3 = _feat("P3", ["b.ts"], layer="product")
    kept, dropped = drop_phantom_product_features([d1, d2], [p1, p2, p3])
    assert [pf.name for pf in kept] == ["P1", "P3"]
    assert dropped == 1
    # original (non-recomputed) paths retained
    assert next(pf for pf in kept if pf.name == "P1").paths == ["a.ts", "z.ts"]


# ── Increment-4 LEVER A — non-source member strip + anchor scaffold de-own ──

from faultline.models.types import MemberFile  # noqa: E402
from faultline.pipeline_v2.stage_8_6_nonsource_drop import (  # noqa: E402
    _is_deown_scaffold_path,
    deown_anchor_scaffold,
    strip_nonsource_members,
)

_WSA = "workspace anchor 'web' from monorepo package 'apps/web/'"


def _anchor(name: str, members: list[MemberFile], *,
            paths: list[str] | None = None,
            description: str = _WSA) -> Feature:
    """A WORKSPACE-ANCHOR feature (description carries the marker)."""
    f = _feat(name, paths if paths is not None else [m.path for m in members])
    f.description = description
    f.member_files = members
    return f


def _leaf(name: str, members: list[MemberFile], *,
          paths: list[str] | None = None) -> Feature:
    """A real leaf feature (NOT a workspace anchor — no marker)."""
    f = _feat(name, paths if paths is not None else [m.path for m in members])
    f.description = "auth route group"
    f.member_files = members
    return f


def _mf(path: str, *, role: str = "anchor", primary: bool = True) -> MemberFile:
    return MemberFile(path=path, role=role, confidence=1.0, primary=primary)


def _owned_paths(feat: Feature) -> set[str]:
    """Mirror cold_eval._owned_file_set: primary OR role in {anchor, owner}."""
    return {
        m.path for m in feat.member_files
        if m.primary or m.role in ("anchor", "owner")
    }


# ── _is_deown_scaffold_path vocabulary ──────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("apps/web/app/lib/api/response.ts", True),
        ("apps/web/types/survey.ts", True),
        ("apps/web/utils/format.ts", True),
        ("apps/web/hooks/use-thing.ts", True),
        ("packages/constants/index.ts", True),
        ("src/shared/client.ts", True),
        ("src/common/logger.ts", True),
        # excluded-from-de-own (ambiguous product surfaces) → not scaffold here
        ("apps/web/components/Button.tsx", False),
        ("apps/web/ui/Card.tsx", False),
        ("apps/web/i18n/en.ts", False),
        # real route source → not scaffold
        ("apps/web/app/(app)/settings/page.tsx", False),
    ],
)
def test_is_deown_scaffold_path(path: str, expected: bool) -> None:
    assert _is_deown_scaffold_path(path) is expected


# ── Part 1 — non-source member strip ────────────────────────────────────────


def test_strip_nonsource_members_mixed_feature_keeps_source() -> None:
    """A source/non-source-mix feature sheds assets, keeps source members."""
    f = _anchor("web", [
        _mf("apps/web/app/page.tsx"),
        _mf("apps/web/app/settings/page.tsx"),
        _mf("apps/web/images/logo.webp"),
        _mf("apps/web/videos/demo.mp4"),
        _mf("apps/web/i18n.json"),
    ])
    res = strip_nonsource_members([f])
    assert res.features_trimmed == 1
    assert res.members_removed == 3
    surviving = {m.path for m in f.member_files}
    assert surviving == {"apps/web/app/page.tsx", "apps/web/app/settings/page.tsx"}
    # paths projection pruned in lock-step
    assert set(f.paths) == surviving


def test_strip_nonsource_members_noop_on_all_source() -> None:
    f = _anchor("web", [
        _mf("a/x.ts"), _mf("a/y.tsx"), _mf("a/schema.prisma"), _mf("a/theme.css"),
    ])
    before = {m.path for m in f.member_files}
    res = strip_nonsource_members([f])
    assert res.features_trimmed == 0
    assert res.members_removed == 0
    assert {m.path for m in f.member_files} == before


def test_strip_nonsource_members_disabled(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_8_6_NONSOURCE_STRIP", "0")
    f = _anchor("web", [_mf("a/page.tsx"), _mf("a/logo.png")])
    res = strip_nonsource_members([f])
    assert res.enabled is False
    assert len(f.member_files) == 2  # untouched


def test_strip_nonsource_members_scale_invariant() -> None:
    """Tiny / medium / large mixed anchors all shed exactly their non-source."""
    for n_src, n_asset in [(1, 1), (20, 8), (400, 120)]:
        members = [_mf(f"a/src{i}.ts") for i in range(n_src)]
        members += [_mf(f"a/img{i}.png") for i in range(n_asset)]
        f = _anchor("web", members)
        res = strip_nonsource_members([f])
        assert res.members_removed == n_asset
        assert all(m.path.endswith(".ts") for m in f.member_files)
        assert len(f.member_files) == n_src


# ── Part 2 — workspace-anchor scaffold de-own ───────────────────────────────


def test_deown_anchor_scaffold_reclassifies_to_shared() -> None:
    """Scaffold members on a workspace anchor flip to role=shared/non-primary."""
    f = _anchor("web", [
        _mf("apps/web/app/page.tsx"),
        _mf("apps/web/app/lib/api/response.ts"),
        _mf("apps/web/types/survey.ts"),
        _mf("apps/web/utils/date.ts"),
    ])
    before_owned = _owned_paths(f)
    assert len(before_owned) == 4

    res = deown_anchor_scaffold([f])
    assert res.anchors_total == 1
    assert res.anchors_deowned == 1
    assert res.members_reclassified == 3

    # The 3 scaffold files are now shared/non-primary; the route page stays.
    by_path = {m.path: m for m in f.member_files}
    for p in ("apps/web/app/lib/api/response.ts",
              "apps/web/types/survey.ts", "apps/web/utils/date.ts"):
        assert by_path[p].role == "shared"
        assert by_path[p].primary is False
    page = by_path["apps/web/app/page.tsx"]
    assert page.role == "anchor" and page.primary is True

    # Files are NOT lost from the ledger (path-keyed _file_set unchanged) …
    assert set(by_path) == {
        "apps/web/app/page.tsx", "apps/web/app/lib/api/response.ts",
        "apps/web/types/survey.ts", "apps/web/utils/date.ts",
    }
    # … but the OWNED set shrank to just the route page.
    assert _owned_paths(f) == {"apps/web/app/page.tsx"}
    # exclusive paths projection pruned too
    assert set(f.paths) == {"apps/web/app/page.tsx"}


def test_deown_anchor_scaffold_noop_on_real_lib_feature() -> None:
    """A genuine leaf 'lib' feature (NOT a workspace anchor) is never gutted."""
    f = _leaf("shared-utils", [
        _mf("packages/shared-utils/lib/format.ts"),
        _mf("packages/shared-utils/utils/parse.ts"),
        _mf("packages/shared-utils/index.ts"),
    ])
    before_owned = _owned_paths(f)
    res = deown_anchor_scaffold([f])
    assert res.anchors_total == 0           # not a workspace anchor
    assert res.members_reclassified == 0
    assert _owned_paths(f) == before_owned   # fully intact
    assert all(m.primary for m in f.member_files)


def test_deown_anchor_scaffold_excludes_components_ui() -> None:
    """components/ / ui/ are NOT in the de-own set (no fan-in guard here)."""
    f = _anchor("web", [
        _mf("apps/web/components/Button.tsx"),
        _mf("apps/web/ui/Card.tsx"),
        _mf("apps/web/lib/api.ts"),
    ])
    res = deown_anchor_scaffold([f])
    assert res.members_reclassified == 1     # only lib/api.ts
    by_path = {m.path: m for m in f.member_files}
    assert by_path["apps/web/components/Button.tsx"].primary is True
    assert by_path["apps/web/ui/Card.tsx"].primary is True
    assert by_path["apps/web/lib/api.ts"].role == "shared"


def test_deown_anchor_scaffold_idempotent() -> None:
    """Already-shared members are left as-is; second pass is a no-op."""
    f = _anchor("web", [
        _mf("apps/web/app/page.tsx"),
        _mf("apps/web/lib/api.ts"),
    ])
    deown_anchor_scaffold([f])
    res2 = deown_anchor_scaffold([f])
    assert res2.members_reclassified == 0
    assert res2.anchors_deowned == 0


def test_deown_anchor_scaffold_disabled(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_8_6_ANCHOR_SCAFFOLD_DEOWN", "0")
    f = _anchor("web", [_mf("apps/web/lib/api.ts")])
    res = deown_anchor_scaffold([f])
    assert res.enabled is False
    assert f.member_files[0].primary is True  # untouched


def test_deown_anchor_scaffold_scale_invariant() -> None:
    """Tiny / medium / large anchors de-own exactly their scaffold members."""
    for n_route, n_scaffold in [(1, 1), (30, 10), (200, 275)]:
        members = [_mf(f"apps/web/app/r{i}/page.tsx") for i in range(n_route)]
        members += [_mf(f"apps/web/lib/m{i}.ts") for i in range(n_scaffold)]
        f = _anchor("web", members)
        res = deown_anchor_scaffold([f])
        assert res.members_reclassified == n_scaffold
        assert _owned_paths(f) == {
            f"apps/web/app/r{i}/page.tsx" for i in range(n_route)
        }
