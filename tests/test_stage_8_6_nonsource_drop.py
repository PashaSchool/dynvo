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
