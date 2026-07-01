"""Unit tests for the top-level scan-result cache.

Covers the four properties the reproducibility guarantee rests on:

  * KEY is STABLE for identical (repo-state, engine-version, config) and
    DIFFERS when any of a tracked file, the config, or the engine version
    changes.
  * A HIT returns the EXACT stored JSON bytes (byte-identical replay).
  * A MISS runs (nothing to load) then stores.
  * A cache fault (corrupt / unwritable) falls through to a normal scan.
  * The gate defaults OFF.

No LLM, no network — pure git + local-disk. The pipeline wiring itself is
exercised by a focused short-circuit test that proves the HIT returns
BEFORE intake runs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from faultline.pipeline_v2 import scan_result_cache as src


# ── fixtures ─────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.dev")
    _git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text("print('hi')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture()
def cfg() -> dict:
    return src.scan_config_signature(
        model="claude-haiku-4-5-20251001",
        days=365,
        subpath=None,
        max_tree_depth=8,
        llm_reconcile=False,
        feature_history=True,
    )


def _key(repo: Path, cfg: dict, *, version: str = "1.39.0") -> str:
    return src.compute_scan_cache_key(
        repo, engine_version=version, config_signature=cfg,
    )


# ── key stability ────────────────────────────────────────────────────────


def test_key_stable_for_identical_inputs(git_repo: Path, cfg: dict) -> None:
    assert _key(git_repo, cfg) == _key(git_repo, cfg)


def test_key_is_hex_sha256(git_repo: Path, cfg: dict) -> None:
    k = _key(git_repo, cfg)
    assert len(k) == 64 and all(c in "0123456789abcdef" for c in k)


def test_clean_checkout_hashes_to_head(git_repo: Path) -> None:
    ident = src.repo_content_identity(git_repo)
    assert ident["vcs"] == "git"
    assert ident["head"]  # a real sha
    assert ident["dirty"] == ""  # clean tree → empty dirty component


# ── key sensitivity ──────────────────────────────────────────────────────


def test_key_differs_when_tracked_file_changes(git_repo: Path, cfg: dict) -> None:
    before = _key(git_repo, cfg)
    (git_repo / "app.py").write_text("print('changed')\n")  # dirty tracked file
    after = _key(git_repo, cfg)
    assert before != after


def test_key_differs_after_new_commit(git_repo: Path, cfg: dict) -> None:
    before = _key(git_repo, cfg)
    (git_repo / "b.py").write_text("x = 1\n")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "second")
    after = _key(git_repo, cfg)
    assert before != after  # HEAD moved


def test_key_differs_when_model_changes(git_repo: Path, cfg: dict) -> None:
    other = dict(cfg, model="claude-sonnet-4-6")
    assert _key(git_repo, cfg) != _key(git_repo, other)


def test_key_differs_when_days_changes(git_repo: Path, cfg: dict) -> None:
    other = dict(cfg, days=90)
    assert _key(git_repo, cfg) != _key(git_repo, other)


def test_key_differs_when_abstraction_flag_changes(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(src.ENV_6_7D_ABSTRACTION, raising=False)
    off = src.scan_config_signature(
        model="m", days=365, subpath=None, max_tree_depth=8,
        llm_reconcile=False, feature_history=True,
    )
    monkeypatch.setenv(src.ENV_6_7D_ABSTRACTION, "1")
    on = src.scan_config_signature(
        model="m", days=365, subpath=None, max_tree_depth=8,
        llm_reconcile=False, feature_history=True,
    )
    assert on != off
    assert _key(git_repo, off) != _key(git_repo, on)


def test_key_differs_when_engine_version_changes(git_repo: Path, cfg: dict) -> None:
    assert _key(git_repo, cfg, version="1.39.0") != _key(
        git_repo, cfg, version="1.40.0",
    )


# ── non-git fallback ─────────────────────────────────────────────────────


def test_non_git_dir_falls_back_to_tree_hash(tmp_path: Path, cfg: dict) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("a = 1\n")
    ident = src.repo_content_identity(plain)
    assert ident["vcs"] == "none"
    assert ident["dirty"]  # non-empty tree hash
    k1 = _key(plain, cfg)
    (plain / "a.py").write_text("a = 2\n")  # change a source file
    assert _key(plain, cfg) != k1  # tree hash reacts


# ── store / load round-trip (byte-exact) ─────────────────────────────────


def test_miss_then_store_then_hit(tmp_path: Path) -> None:
    base = tmp_path / "state"
    result = tmp_path / "feature-map.json"
    payload = {"repo_path": "r", "scan_meta": {"engine_version": "1.39.0"}}
    result.write_text(json.dumps(payload, indent=2))
    key = "deadbeef"

    # MISS: nothing stored yet.
    assert src.load_cached_scan(key, base_dir=base) is None

    # STORE succeeds.
    assert src.store_scan_result(key, result, base_dir=base) is True

    # HIT: byte-identical to the written file.
    raw = src.load_cached_scan(key, base_dir=base)
    assert raw is not None
    assert raw == result.read_text()


def test_serve_writes_byte_identical_bytes(tmp_path: Path) -> None:
    base = tmp_path / "state"
    src_file = tmp_path / "run-a.json"
    raw_text = json.dumps(
        {"repo_path": "r", "scan_meta": {"scan_id": "A", "cost_usd": 0.42}},
        indent=2,
    )
    src_file.write_text(raw_text)
    key = "cafef00d"
    assert src.store_scan_result(key, src_file, base_dir=base)
    stored = src.load_cached_scan(key, base_dir=base)

    out = tmp_path / "run-b.json"
    served = src.serve_from_cache(
        stored, key=key, repo_path=tmp_path, out_path=out,
    )
    assert served is not None
    # Byte-identical replay of run A's file.
    assert out.read_text() == raw_text
    # Return shape mirrors run_pipeline_v2 + carries the HIT marker.
    assert served["path"] == str(out.resolve())
    assert served["scan_cache"]["served_from_cache"] is True
    assert served["scan_cache"]["key"] == key
    # scan_meta from the stored map is surfaced on the return dict.
    assert served["scan_id"] == "A"


# ── fault tolerance ──────────────────────────────────────────────────────


def test_corrupt_entry_is_a_miss(tmp_path: Path) -> None:
    base = tmp_path / "state"
    path = base / "scan-cache" / "bad.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not valid json ")  # truncated / partial
    assert src.load_cached_scan("bad", base_dir=base) is None


def test_store_to_unwritable_path_returns_false(tmp_path: Path) -> None:
    # Point the base dir at a *file* so mkdir of scan-cache/ fails → the
    # store swallows the OSError and reports False (no raise).
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    result = tmp_path / "r.json"
    result.write_text("{}")
    assert src.store_scan_result("k", result, base_dir=blocker) is False


def test_store_missing_source_returns_false(tmp_path: Path) -> None:
    assert src.store_scan_result(
        "k", tmp_path / "does-not-exist.json", base_dir=tmp_path / "s",
    ) is False


def test_serve_with_corrupt_text_returns_none(tmp_path: Path) -> None:
    assert src.serve_from_cache(
        "not json", key="k", repo_path=tmp_path, out_path=tmp_path / "o.json",
    ) is None


# ── gate ─────────────────────────────────────────────────────────────────


def test_gate_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(src.ENV_ENABLE, raising=False)
    assert src.is_enabled() is False


def test_gate_on_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(src.ENV_ENABLE, "1")
    assert src.is_enabled() is True


def test_bypass_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(src.ENV_BYPASS, raising=False)
    assert src.is_bypassed() is False
    monkeypatch.setenv(src.ENV_BYPASS, "1")
    assert src.is_bypassed() is True


# ── pipeline wiring: HIT short-circuits BEFORE intake ────────────────────


def test_pipeline_hit_short_circuits_before_intake(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the gate ON and a pre-seeded entry, run_pipeline_v2 returns the
    served result WITHOUT running intake (proven by making intake raise)."""
    from faultline.pipeline_v2 import run as run_mod
    from faultline.pipeline_v2.stage_6_3_import_tree import (
        DEFAULT_MAX_DEPTH as _DEPTH,
    )

    base = tmp_path / "state"
    monkeypatch.setenv("FAULTLINES_RUN_DIR", str(base))
    monkeypatch.setenv(src.ENV_ENABLE, "1")
    monkeypatch.delenv(src.ENV_BYPASS, raising=False)
    monkeypatch.delenv(src.ENV_6_7D_ABSTRACTION, raising=False)

    # Compute the key exactly as run_pipeline_v2 will (default args).
    cfg = src.scan_config_signature(
        model=run_mod.DEFAULT_MODEL, days=365, subpath=None,
        max_tree_depth=_DEPTH, llm_reconcile=False, feature_history=True,
    )
    key = src.compute_scan_cache_key(
        git_repo.resolve(), engine_version=src.engine_version(),
        config_signature=cfg,
    )
    seed = tmp_path / "seed.json"
    raw_text = json.dumps(
        {"repo_path": str(git_repo), "scan_meta": {"marker": "SEEDED"}},
        indent=2,
    )
    seed.write_text(raw_text)
    assert src.store_scan_result(key, seed, base_dir=base)

    # Any attempt to run intake would blow up — a HIT must not reach it.
    def _boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("intake ran — HIT did not short-circuit")

    monkeypatch.setattr(run_mod, "run_intake_phase", _boom)

    out = tmp_path / "served.json"
    result = run_mod.run_pipeline_v2(
        git_repo, model=run_mod.DEFAULT_MODEL, days=365, out_path=out,
    )
    assert result["scan_cache"]["served_from_cache"] is True
    assert result["marker"] == "SEEDED"
    assert out.read_text() == raw_text  # byte-identical replay


# ── audit fixes: untracked content (Bug 1) + stage env flags (Bug 2) ─────────

def test_key_differs_when_untracked_file_content_changes(git_repo: Path, cfg: dict) -> None:
    """Editing an UNTRACKED new file must change the key (audit Bug 1) — else a
    stub→populated file serves a stale zero-flow result."""
    (git_repo / "new_untracked.py").write_text("# stub\n")
    k_stub = _key(git_repo, cfg)
    (git_repo / "new_untracked.py").write_text("def f():\n    return 300\n" * 20)
    k_full = _key(git_repo, cfg)
    assert k_stub != k_full


def test_key_differs_when_output_env_flag_toggled(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stage-gating env flag (e.g. FAULTLINE_SEED_SYSTEM_UFS kill-switch)
    toggled between scans must change the key (audit Bug 2)."""
    monkeypatch.delenv("FAULTLINE_SEED_SYSTEM_UFS", raising=False)
    k_on = _key(git_repo, src.scan_config_signature(
        model="haiku", days=365, subpath=None, max_tree_depth=8,
        llm_reconcile=False, feature_history=True))
    monkeypatch.setenv("FAULTLINE_SEED_SYSTEM_UFS", "0")
    k_off = _key(git_repo, src.scan_config_signature(
        model="haiku", days=365, subpath=None, max_tree_depth=8,
        llm_reconcile=False, feature_history=True))
    assert k_on != k_off
