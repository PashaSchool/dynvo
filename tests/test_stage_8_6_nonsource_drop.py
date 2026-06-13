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
