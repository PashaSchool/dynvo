"""Stage 6.97 — deterministic feature-level LOC.

Operator invariant (validate_scan.py I2): every dev feature with >=1
non-empty non-test owned file must emit ``loc > 0``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from faultline.models.types import Feature
from faultline.pipeline_v2.stage_6_97_feature_loc import (
    STAGE_6_97_ENV_FLAG,
    apply_feature_loc,
    count_file_loc,
    stage_6_97_enabled,
)


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


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "app/auth/login.ts", "// comment\nconst a = 1;\n\nexport {a};\n")
    _write(root, "app/auth/login.test.ts", "test('x', () => {});\n" * 50)
    _write(root, "app/i18n/messages.json", '{\n "hello": "world"\n}\n')
    _write(root, "app/empty.ts", "")
    _write(root, "pkg/db/queries_generated.go", "package db\n" * 100)
    _write(root, "pnpm-lock.yaml", "lockfileVersion: 9\n" * 100)
    _write(root, "assets/bundle.min.js", "var a=1;" * 200)
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets/logo.png").write_bytes(b"\x89PNG\x00\x00binary")
    _write(root, "app/billing/invoice.ts", "export const x = 1;\nexport const y = 2;\n")
    _write(root, "app/billing/shared.ts", "export const s = 1;\n")
    return root


# ── per-file counting ───────────────────────────────────────────────────


def test_counts_executable_lines_not_comments(tmp_path):
    root = _repo(tmp_path)
    n = count_file_loc(root / "app/auth/login.ts", "app/auth/login.ts")
    assert n == 2  # comment + blank excluded


def test_test_generated_lockfile_minified_binary_excluded(tmp_path):
    root = _repo(tmp_path)
    for rel in (
        "app/auth/login.test.ts",
        "pkg/db/queries_generated.go",
        "pnpm-lock.yaml",
        "assets/bundle.min.js",
        "assets/logo.png",
        "app/empty.ts",
        "does/not/exist.ts",
    ):
        assert count_file_loc(root / rel, rel) == 0, rel


def test_unknown_text_ext_counts_nonblank_lines(tmp_path):
    # config-as-product guarantee: .json/.yaml features still get loc>0
    root = _repo(tmp_path)
    n = count_file_loc(root / "app/i18n/messages.json", "app/i18n/messages.json")
    assert n == 3


# ── feature-level emission ──────────────────────────────────────────────


def test_flowless_feature_with_paths_gets_positive_loc(tmp_path):
    root = _repo(tmp_path)
    feat = _feature("auth", ["app/auth/login.ts", "app/auth/login.test.ts"])
    assert feat.flows == []  # flowless — the I2 bug class
    telemetry = apply_feature_loc([feat], [], root)
    assert feat.loc == 2  # test file listed but NOT counted
    assert feat.paths == ["app/auth/login.ts", "app/auth/login.test.ts"]
    assert telemetry["features_with_loc"] == 1
    assert telemetry["features_zero_loc_with_paths"] == 0


def test_feature_with_only_excluded_paths_gets_zero(tmp_path):
    root = _repo(tmp_path)
    feat = _feature("test-only", ["app/auth/login.test.ts", "assets/logo.png"])
    telemetry = apply_feature_loc([feat], [], root)
    assert feat.loc == 0
    assert telemetry["features_zero_loc_with_paths"] == 1


def test_directory_path_counts_recursively(tmp_path):
    root = _repo(tmp_path)
    feat = _feature("auth-dir", ["app/auth"])
    apply_feature_loc([feat], [], root)
    assert feat.loc == 2  # login.ts only; the test twin is excluded


def test_missing_paths_are_silently_zero(tmp_path):
    root = _repo(tmp_path)
    feat = _feature("ghost", ["app/removed-in-refactor.ts", "app/auth/login.ts"])
    apply_feature_loc([feat], [], root)
    assert feat.loc == 2


# ── product-feature rollup ──────────────────────────────────────────────


def test_pf_rollup_dedups_shared_files(tmp_path):
    root = _repo(tmp_path)
    d1 = _feature(
        "billing-invoices",
        ["app/billing/invoice.ts", "app/billing/shared.ts"],
        product_feature_id="billing",
    )
    d2 = _feature(
        "billing-shared",
        ["app/billing/shared.ts"],
        product_feature_id="billing",
    )
    pf = _feature("billing", [], layer="product")
    apply_feature_loc([d1, d2], [pf], root)
    assert d1.loc == 3
    assert d2.loc == 1
    assert pf.loc == 3  # shared.ts counted ONCE, not d1.loc + d2.loc == 4


def test_pf_without_members_falls_back_to_own_paths(tmp_path):
    root = _repo(tmp_path)
    pf = _feature("standalone", ["app/billing/invoice.ts"], layer="product")
    apply_feature_loc([], [pf], root)
    assert pf.loc == 2


# ── stage plumbing ──────────────────────────────────────────────────────


def test_env_kill_switch(monkeypatch):
    monkeypatch.delenv(STAGE_6_97_ENV_FLAG, raising=False)
    assert stage_6_97_enabled()
    monkeypatch.setenv(STAGE_6_97_ENV_FLAG, "0")
    assert not stage_6_97_enabled()


def test_loc_field_defaults_none_for_old_scans():
    feat = _feature("legacy", ["a.ts"])
    assert feat.loc is None  # pre-stage scans rehydrate unchanged
    dumped = feat.model_dump()
    assert dumped["loc"] is None
