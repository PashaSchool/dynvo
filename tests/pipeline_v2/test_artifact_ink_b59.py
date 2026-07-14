"""B59 — artifact-ink accounting drain.

Stage 6.97 reclassifies a feature's OWNED non-authorial "ink" LOC (locale
catalogs / generated schemas / test data / dev seeders) OUT of product
``loc`` into ``artifact_ink_loc`` + a ``scan_meta.artifact_ink`` lane
aggregate — ACCOUNTING ONLY (membership / path_index / line coordinates /
flows / user_flows untouched). Behind ``FAULTLINE_ARTIFACT_INK_LANE``
(default OFF → byte-identical to main).

SACRED anti-cases (must survive as PRODUCT, classify -> None):
``packages/i18n/package.json`` (config blocklist, the known false-positive),
``tsconfig.json``, ``prisma/schema.prisma``, a functional ``*.config.ts``, a
product JSON fixture NOT under a locale dir, and ``sign-in-background-mock/**``
(name suffix, not a ``mocks`` dir segment).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature
from faultline.pipeline_v2.artifact_ink import (
    ARTIFACT_INK_ENV,
    artifact_ink_enabled,
    classify_artifact,
)
from faultline.pipeline_v2.stage_6_97_feature_loc import apply_feature_loc


def _feature(name: str, paths: list[str], **kw) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        health_score=100.0,
        **kw,
    )


def _write(root: Path, rel: str, lines: int) -> None:
    """Write ``rel`` with exactly ``lines`` non-blank executable lines."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"line{i} = {i}\n" for i in range(lines)), encoding="utf-8")


# ── (1) classifier: positive classes ────────────────────────────────────


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("packages/front/src/locales/en.po", "locale"),
        ("packages/front/src/locales/de.pot", "locale"),
        ("apps/web/messages/fr.json", "locale"),
        ("src/i18n/translations/es.json", "locale"),
        ("src/lang/pt.json", "locale"),
        ("src/__generated__/graphql.ts", "generated"),
        ("packages/sdk/generated/client.ts", "generated"),
        ("src/api/schema.generated.ts", "generated"),
        ("src/foo/mocks/company.ts", "testing"),
        ("packages/front/src/testing/mock-data/company.ts", "testing"),
        ("packages/server/src/database/seeds/user.ts", "seed"),
        ("packages/server/src/dev-seeder/data.ts", "seed"),
        ("db/seed-data/rows.ts", "seed"),
    ],
)
def test_classify_positive(rel: str, expected: str) -> None:
    assert classify_artifact(rel) == expected


# ── (2) classifier: SACRED anti-cases (survive as product -> None) ───────


@pytest.mark.parametrize(
    "rel",
    [
        "packages/i18n/package.json",          # THE caught false-positive
        "packages/i18n/locales/tsconfig.json",  # config even under /locales/
        "tsconfig.json",
        "tsconfig.build.json",
        "vitest.config.json",
        "apps/web/next.config.json",
        "components.json",
        "turbo.json",
        "nx.json",
        "prisma/schema.prisma",
        "src/app.config.ts",                   # functional config (.ts, not .json)
        "config/product-data.json",            # product JSON fixture NOT under locales
        "sign-in-background-mock/foo.ts",      # name suffix, NOT a /mocks/ segment
        "src/components/Button.tsx",           # plain product source
        "src/schema.generated.go",             # compiled codegen: guard -> None (0 LOC upstream)
        "app/__tests__/foo.test.ts",           # test CODE: guard -> None (already stripped)
    ],
)
def test_classify_sacred_survivors(rel: str) -> None:
    assert classify_artifact(rel) is None


def test_classify_empty_and_nonstring() -> None:
    assert classify_artifact("") is None
    assert classify_artifact("   ") is None


@pytest.mark.parametrize(
    "rel",
    [
        "packages/server/src/__mocks__/user.ts",     # is_test_path dir segment
        "packages/server/src/__fixtures__/user.ts",  # is_test_path dir segment
        "app/foo.test.ts",                           # test CODE (marker suffix)
        "pkg/db/queries_generated.go",               # compiled codegen basename
        "pnpm-lock.yaml",                            # lockfile
        "assets/logo.png",                           # binary ext
    ],
)
def test_classify_double_count_guard(rel: str) -> None:
    """Files the LOC census already zeroes (test code via is_test_path,
    compiled codegen via is_generated_path, lockfiles/binaries via
    _is_excluded_name) return None — they contribute 0 LOC, so classifying
    them as ink would be meaningless. NOTE: ``__mocks__``/``__fixtures__``
    are test-strip dir segments, so they resolve here (guard), NOT via the
    testing class — the accounting is identical either way (0 LOC)."""
    assert classify_artifact(rel) is None


# ── (3) accounting + conservation (flag ON) ─────────────────────────────


def _fixture(root: Path) -> None:
    _write(root, "app/main.ts", 10)          # product code
    _write(root, "app/locales/en.po", 50)    # locale ink
    _write(root, "app/locales/fr.po", 40)    # locale ink


def test_drain_partitions_owned_and_conserves(tmp_path, monkeypatch):
    monkeypatch.setenv(ARTIFACT_INK_ENV, "1")
    assert artifact_ink_enabled()
    root = tmp_path / "repo"
    _fixture(root)
    dev = _feature("front", ["app/main.ts", "app/locales/en.po",
                             "app/locales/fr.po"], product_feature_id="pf")
    pf = _feature("pf", [], layer="product")
    tele = apply_feature_loc([dev], [pf], root)

    # PF product loc drops to the code-only mass; ink drains to the counter.
    assert pf.loc == 10
    assert pf.artifact_ink_loc == 90
    # Conservation: product + ink == pre-drain owned total (mirror b6.97:203).
    assert (pf.loc or 0) + (pf.artifact_ink_loc or 0) == 100
    # Dev feature partitioned identically.
    assert dev.loc == 10
    assert dev.artifact_ink_loc == 90

    art = tele["artifact_ink"]
    assert art["enabled"] is True and art["applied"] is True
    assert art["total_loc"] == 90
    assert art["total_files"] == 2
    assert art["by_class"] == {"locale": 90, "generated": 0, "testing": 0,
                               "seed": 0}
    assert art["by_pf"] == {"pf": 90}
    assert art["conservation_ok"] is True
    assert sorted(art["samples"]) == ["app/locales/en.po", "app/locales/fr.po"]
    # loc_accounting carries the drain total; repo_loc is unchanged (100).
    assert tele["loc_accounting"]["sum_artifact_ink"] == 90
    assert tele["loc_accounting"]["repo_loc"] == 100
    assert tele["loc_accounting"]["sum_pf_owned"] == 10


def test_config_blocklist_stays_product_under_locale(tmp_path, monkeypatch):
    """SACRED accounting: a package.json under a locale dir keeps its LOC in
    product ``loc`` (never drains) — the packages/i18n/package.json class."""
    monkeypatch.setenv(ARTIFACT_INK_ENV, "1")
    root = tmp_path / "repo"
    _write(root, "packages/i18n/package.json", 8)
    _write(root, "packages/i18n/locales/en.po", 30)
    dev = _feature("i18n", ["packages/i18n/package.json",
                            "packages/i18n/locales/en.po"],
                   product_feature_id="i18n")
    pf = _feature("i18n", [], layer="product")
    apply_feature_loc([dev], [pf], root)
    assert pf.loc == 8            # package.json stays product
    assert pf.artifact_ink_loc == 30   # only the .po drains


# ── (4) flag OFF -> byte-identical (no drain, no field, no telemetry) ────


def test_flag_off_noop(tmp_path, monkeypatch):
    monkeypatch.delenv(ARTIFACT_INK_ENV, raising=False)
    assert not artifact_ink_enabled()
    root = tmp_path / "repo"
    _fixture(root)
    dev = _feature("front", ["app/main.ts", "app/locales/en.po",
                             "app/locales/fr.po"], product_feature_id="pf")
    pf = _feature("pf", [], layer="product")
    tele = apply_feature_loc([dev], [pf], root)

    assert pf.loc == 100                       # nothing drained
    assert pf.artifact_ink_loc is None
    assert dev.artifact_ink_loc is None
    assert "artifact_ink" not in tele          # telemetry key ABSENT when OFF
    assert "sum_artifact_ink" not in tele["loc_accounting"]
    # Serializer pops the None field -> byte-identical shape to pre-B59.
    assert "artifact_ink_loc" not in pf.model_dump()
    assert "artifact_ink_loc" not in dev.model_dump()


# ── (5) idempotency / determinism ───────────────────────────────────────


def test_idempotent_determinism(tmp_path, monkeypatch):
    monkeypatch.setenv(ARTIFACT_INK_ENV, "1")
    root = tmp_path / "repo"
    _fixture(root)
    dev = _feature("front", ["app/main.ts", "app/locales/en.po",
                             "app/locales/fr.po"], product_feature_id="pf")
    pf = _feature("pf", [], layer="product")
    t1 = apply_feature_loc([dev], [pf], root)
    loc1, ink1 = pf.loc, pf.artifact_ink_loc
    t2 = apply_feature_loc([dev], [pf], root)   # second run: same inputs
    assert (pf.loc, pf.artifact_ink_loc) == (loc1, ink1) == (10, 90)
    assert t1["artifact_ink"] == t2["artifact_ink"]


# ── (6) phantom-immunity — the first gate-race collateral (twenty) ─────────
# emission_integrity dropped the pure-artifact dev "locales" (10,017 LOC of
# .po, sole owner -> loc=0 after the drain) as a phantom, costing 65
# path_index coordinate entries. artifact_ink_loc>0 is an accounting
# channel: reclassified ink is NOT absent code.


def test_pure_artifact_dev_is_not_phantom():
    from faultline.pipeline_v2.emission_integrity import _is_phantom

    drained = _feature("locales", ["emails/locales/en.po"])
    drained.loc = 0
    drained.loc_shared = 0
    drained.artifact_ink_loc = 10017
    assert not _is_phantom(drained)


def test_true_phantom_still_drops():
    from faultline.pipeline_v2.emission_integrity import _is_phantom

    empty = _feature("ghost", ["."])
    empty.loc = 0
    empty.loc_shared = 0
    empty.artifact_ink_loc = None       # flag-OFF world / no ink
    assert _is_phantom(empty)


def test_pure_artifact_pf_is_not_phantom():
    from faultline.pipeline_v2.emission_integrity import _is_phantom

    pf = _feature("i18n", ["packages/i18n/locales/ar/common.json"],
                  layer="product")
    pf.loc = 0
    pf.loc_shared = 0
    pf.artifact_ink_loc = 173442
    assert not _is_phantom(pf)
