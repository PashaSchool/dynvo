"""Sprint G2 — BlameIndex cache-driven HEAD SHA resolution.

Verifies the Sprint G2 perf refactor: when ``BlameIndex`` is built
with a ``commits=`` cache, the per-file ``_git_head_for_file`` call
must resolve from memory and MUST NOT spawn a ``git log`` subprocess.

These tests are pure unit — no real git repo needed for the cache
path. The fall-back behaviour (cache miss → subprocess) is still
covered by ``tests/test_blame_index.py::TestBlameIndexBasics``.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from faultline.analyzer.blame_index import BlameIndex
from faultline.models.types import Commit


def _commit(
    sha: str,
    files: list[str],
    *,
    msg: str = "feat: x",
    when: datetime | None = None,
) -> Commit:
    return Commit(
        sha=sha,
        message=msg,
        author="Alice",
        date=when or datetime.now(tz=timezone.utc),
        files_changed=list(files),
        is_bug_fix=False,
        pr_number=None,
    )


class TestHeadShaCacheBuild:
    def test_empty_commits_builds_empty_cache(self, tmp_path: Path):
        idx = BlameIndex(tmp_path, commits=[])
        assert idx._head_sha_cache == {}
        idx.close()

    def test_none_commits_builds_empty_cache(self, tmp_path: Path):
        idx = BlameIndex(tmp_path, commits=None)
        assert idx._head_sha_cache == {}
        idx.close()

    def test_single_commit_one_file(self, tmp_path: Path):
        idx = BlameIndex(
            tmp_path,
            commits=[_commit("aaaaaaaa", ["src/a.ts"])],
        )
        assert idx._head_sha_cache == {"src/a.ts": "aaaaaaaa"}
        idx.close()

    def test_newest_first_wins_per_path(self, tmp_path: Path):
        # Sprint G commits are newest-first. The first commit that
        # touches a file is the HEAD-touching one.
        idx = BlameIndex(
            tmp_path,
            commits=[
                _commit("newer001", ["src/a.ts"]),
                _commit("older001", ["src/a.ts", "src/b.ts"]),
            ],
        )
        assert idx._head_sha_cache == {
            "src/a.ts": "newer001",
            "src/b.ts": "older001",
        }
        idx.close()

    def test_commits_with_missing_sha_skipped(self, tmp_path: Path):
        bad = Commit(
            sha="",
            message="",
            author="x",
            date=datetime.now(tz=timezone.utc),
            files_changed=["src/x.ts"],
        )
        idx = BlameIndex(tmp_path, commits=[bad])
        assert idx._head_sha_cache == {}
        idx.close()


class TestHeadShaCacheLookup:
    def test_cache_hit_no_subprocess_spawn(self, tmp_path: Path):
        """The single load-bearing assertion of this sprint.

        With commits passed in, ``_git_head_for_file`` MUST NOT
        spawn a subprocess for paths present in the cache.
        """
        idx = BlameIndex(
            tmp_path,
            commits=[_commit("abc12345", ["src/a.ts", "src/b.ts"])],
        )
        with patch("subprocess.run") as mock_run:
            sha_a = idx._git_head_for_file("src/a.ts")
            sha_b = idx._git_head_for_file("src/b.ts")
        assert sha_a == "abc12345"
        assert sha_b == "abc12345"
        # Hard requirement — zero subprocess calls when every path
        # is cached. This is the Sprint G2 perf gate in unit form.
        assert mock_run.call_count == 0
        idx.close()

    def test_cache_miss_falls_back_to_subprocess(self, tmp_path: Path):
        idx = BlameIndex(
            tmp_path,
            commits=[_commit("abc12345", ["src/cached.ts"])],
        )

        class FakeResult:
            stdout = "deadbeef\n"
            returncode = 0

        with patch(
            "faultline.analyzer.blame_index.subprocess.run",
            return_value=FakeResult(),
        ) as mock_run:
            sha = idx._git_head_for_file("src/uncached.ts")
        assert sha == "deadbeef"
        # Only the uncached path goes to subprocess.
        assert mock_run.call_count == 1
        idx.close()

    def test_no_commits_passed_subprocess_always_used(self, tmp_path: Path):
        """Backward compat — old call sites that don't pass commits
        keep the original behaviour.
        """
        idx = BlameIndex(tmp_path)  # no commits=

        class FakeResult:
            stdout = "abc12345\n"
            returncode = 0

        with patch(
            "faultline.analyzer.blame_index.subprocess.run",
            return_value=FakeResult(),
        ) as mock_run:
            sha = idx._git_head_for_file("src/x.ts")
        assert sha == "abc12345"
        assert mock_run.call_count == 1
        idx.close()


class TestIsUpToDatePrefixMatch:
    """A subtle correctness gate: the SQLite cache may contain a
    40-char sha from a previous run while the new code resolves to
    an 8-char short sha. Both must be considered equivalent.
    """

    def test_short_sha_matches_stored_long_sha(self, tmp_path: Path):
        idx = BlameIndex(tmp_path)
        # Manually seed the file_state row with a 40-char sha (as
        # legacy runs did).
        idx._conn.execute(
            "INSERT INTO file_state (file_path, head_sha, indexed_at) "
            "VALUES (?, ?, ?)",
            ("src/a.ts", "abc12345" + "0" * 32, "2026-05-21T00:00:00Z"),
        )
        idx._conn.commit()
        # Sprint G2 short-sha lookup should return True.
        assert idx._is_up_to_date("src/a.ts", "abc12345") is True
        idx.close()

    def test_long_sha_matches_stored_short_sha(self, tmp_path: Path):
        idx = BlameIndex(tmp_path)
        idx._conn.execute(
            "INSERT INTO file_state (file_path, head_sha, indexed_at) "
            "VALUES (?, ?, ?)",
            ("src/a.ts", "abc12345", "2026-05-21T00:00:00Z"),
        )
        idx._conn.commit()
        # And the converse — old-call-site full-sha asking about a
        # new-format stored short sha.
        assert (
            idx._is_up_to_date("src/a.ts", "abc12345" + "0" * 32) is True
        )
        idx.close()

    def test_unrelated_shas_dont_collide(self, tmp_path: Path):
        idx = BlameIndex(tmp_path)
        idx._conn.execute(
            "INSERT INTO file_state (file_path, head_sha, indexed_at) "
            "VALUES (?, ?, ?)",
            ("src/a.ts", "abc12345", "2026-05-21T00:00:00Z"),
        )
        idx._conn.commit()
        assert idx._is_up_to_date("src/a.ts", "def67890") is False
        idx.close()


class TestBulkIndexNoSubprocessForHeadLookup:
    """End-to-end: ``index_files`` over N paths from the cache
    spawns zero ``git log`` head-sha lookups. The blame call
    itself still runs (unavoidable for first-time line-level data)
    but that's tested elsewhere.
    """

    def test_all_paths_cached_zero_head_lookups(self, tmp_path: Path):
        paths = [f"src/file{i}.ts" for i in range(50)]
        idx = BlameIndex(
            tmp_path,
            commits=[_commit("cafef00d", paths)],
        )

        head_calls: list[list[str]] = []
        blame_calls: list[list[str]] = []

        class FakeResult:
            def __init__(self, kind: str):
                self.kind = kind
                self.returncode = 0
                self.stdout = "" if kind == "blame" else "cafef00d\n"
                self.stderr = ""

        def fake_run(cmd, **_kwargs):
            # cmd looks like ["git", "-C", repo, "log"|"blame", ...]
            if "blame" in cmd:
                blame_calls.append(cmd)
                return FakeResult("blame")
            head_calls.append(cmd)
            return FakeResult("head")

        with patch(
            "faultline.analyzer.blame_index.subprocess.run",
            side_effect=fake_run,
        ):
            stats = idx.index_files(paths)
        # Indexed all 50 — none failed.
        assert stats.failed == 0
        assert stats.indexed + stats.cached == 50
        # Hard gate: zero per-file ``git log`` head-sha subprocesses.
        assert len(head_calls) == 0, head_calls[:3]
        idx.close()
