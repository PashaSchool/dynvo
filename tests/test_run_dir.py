"""Tests for ``faultline.pipeline_v2.run_dir`` — run-id assignment,
per-run directory isolation, and the ``latest`` symlink.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from faultline.pipeline_v2 import run_dir as rd


# ── Helpers ────────────────────────────────────────────────────────────


def _git_init_with_commit(repo: Path) -> str:
    """Init a fixture git repo with one commit; return the HEAD sha."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    (repo / "README.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo,
    ).decode().strip()
    return sha


# ── Slug + dir helpers ──────────────────────────────────────────────────


def test_repo_slug_kebab_cases_name() -> None:
    assert rd.repo_slug("/x/y/My-Cool_Repo") == "my-cool-repo"
    assert rd.repo_slug("/x/y/") == "y"  # trailing slash → use 'y'
    assert rd.repo_slug("/") == "repo"  # empty name → 'repo' fallback


def test_slug_log_dir_creates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    d = rd.slug_log_dir(tmp_path / "demo")
    assert d.is_dir()
    assert d == tmp_path / ".faultline" / "logs" / "demo"


# ── Run-id format ───────────────────────────────────────────────────────


def test_generate_run_id_format(tmp_path: Path) -> None:
    """Auto run-id is ``<utc-ts>-<sha8>`` for a real git repo."""
    repo = tmp_path / "demo"
    sha = _git_init_with_commit(repo)
    rid = rd.generate_run_id(repo)
    assert re.match(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$", rid), rid
    # SHA component must match HEAD's first 8 chars.
    assert rid.endswith("-" + sha[:8])


def test_generate_run_id_no_git_falls_back(tmp_path: Path) -> None:
    """A non-git directory still gets a valid run-id with 'nogit' tag."""
    repo = tmp_path / "no-git"
    repo.mkdir()
    rid = rd.generate_run_id(repo)
    assert rid.endswith("-nogit")
    assert re.match(r"^\d{8}T\d{6}Z-nogit$", rid)


def test_sanitize_run_id_accepts_safe_names() -> None:
    assert rd.sanitize_run_id("baseline") == "baseline"
    assert rd.sanitize_run_id("exp-1.2_v3") == "exp-1.2_v3"
    assert rd.sanitize_run_id("20260519T103045Z-abc12345") == (
        "20260519T103045Z-abc12345"
    )


def test_sanitize_run_id_rejects_unsafe_names() -> None:
    with pytest.raises(ValueError):
        rd.sanitize_run_id("../escape")
    with pytest.raises(ValueError):
        rd.sanitize_run_id("with space")
    with pytest.raises(ValueError):
        rd.sanitize_run_id("")


# ── run_artifact_dir + latest symlink ───────────────────────────────────


def test_run_artifact_dir_creates_nested_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    d = rd.run_artifact_dir(tmp_path / "demo", "run-1")
    assert d.is_dir()
    assert d == tmp_path / ".faultline" / "logs" / "demo" / "run-1"


def test_update_latest_symlink_points_to_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    repo = tmp_path / "demo"
    repo.mkdir()
    # Create the run dir first (otherwise the symlink target wouldn't
    # resolve — but `os.symlink` doesn't validate the target on POSIX).
    rd.run_artifact_dir(repo, "first")
    link = rd.update_latest_symlink(repo, "first")
    assert link is not None
    assert link.is_symlink()
    assert os.readlink(link) == "first"
    assert link.resolve() == (
        tmp_path / ".faultline" / "logs" / "demo" / "first"
    )


def test_update_latest_symlink_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two sequential runs → ``latest`` swaps to the newer run-id."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    repo = tmp_path / "demo"
    repo.mkdir()
    rd.run_artifact_dir(repo, "first")
    rd.run_artifact_dir(repo, "second")
    rd.update_latest_symlink(repo, "first")
    rd.update_latest_symlink(repo, "second")
    link = tmp_path / ".faultline" / "logs" / "demo" / "latest"
    assert link.is_symlink()
    assert os.readlink(link) == "second"
