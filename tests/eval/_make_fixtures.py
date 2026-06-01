"""Materialize an eval fixture source tree into a real git repo.

The committed fixtures under ``tests/eval/fixtures/<name>/`` are PLAIN
source trees — they deliberately do NOT carry a nested ``.git`` dir
(that would make the outer faultline repo a confusing repo-in-repo and
break ``git`` tooling). Instead each fixture ships a ``_history.json``
commit plan. At test time :func:`materialize_fixture` copies the source
tree into a throwaway temp dir and replays that plan as REAL commits, so
Faultlines' git-history stages (churn, co-change, bug-fix ratio,
freshness) have genuine signal.

Determinism
-----------
Every knob that could make ``git log`` non-reproducible is pinned:

  * author + committer name / email come from ``_history.json``,
  * author + committer DATES are the fixed per-commit ISO timestamps,
  * ``commit.gpgsign`` is forced off,
  * ``core.autocrlf`` is forced off so file hashes match across OSes.

Two runs of the same fixture therefore produce byte-identical history,
which the determinism test relies on.

No network, no LLM, no API key — pure local git plumbing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

HISTORY_FILENAME = "_history.json"


def available_fixtures() -> list[str]:
    """Names of every fixture that ships a ``_history.json`` plan."""
    return sorted(
        p.parent.name
        for p in FIXTURES_DIR.glob(f"*/{HISTORY_FILENAME}")
    )


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )


def _iter_source_files(src: Path) -> list[Path]:
    """Every fixture file except the history plan (relative paths)."""
    out: list[Path] = []
    for p in src.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(src)
        if rel.name == HISTORY_FILENAME:
            continue
        out.append(rel)
    return out


def materialize_fixture(name: str, dest: Path) -> Path:
    """Copy fixture ``name`` to ``dest`` and replay its commit history.

    Args:
        name: a directory under ``tests/eval/fixtures/``.
        dest: destination directory (created if missing, must be empty
            or non-existent). Typically a pytest ``tmp_path`` subdir.

    Returns:
        The path to the materialized git repo (``dest``).
    """
    src = FIXTURES_DIR / name
    if not src.is_dir():
        raise FileNotFoundError(f"unknown fixture: {name} (looked in {src})")
    plan_path = src / HISTORY_FILENAME
    if not plan_path.exists():
        raise FileNotFoundError(f"fixture {name} has no {HISTORY_FILENAME}")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    author_name = plan.get("author_name", "Faultlines Eval")
    author_email = plan.get("author_email", "eval@faultlines.test")
    commits = plan.get("commits", [])
    if not commits:
        raise ValueError(f"fixture {name}: _history.json has no commits")

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    # Sanity: the commit plan must cover every source file exactly.
    all_source = {str(p) for p in _iter_source_files(src)}
    planned: set[str] = set()
    for c in commits:
        planned.update(c.get("files", []))
    missing = all_source - planned
    if missing:
        raise ValueError(
            f"fixture {name}: files not covered by any commit: "
            f"{sorted(missing)}"
        )
    unknown = planned - all_source
    if unknown:
        raise ValueError(
            f"fixture {name}: commit plan references missing files: "
            f"{sorted(unknown)}"
        )

    # ── init repo with pinned, reproducible config ──────────────────
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "config", "user.name", author_name)
    _git(dest, "config", "user.email", author_email)
    _git(dest, "config", "commit.gpgsign", "false")
    _git(dest, "config", "core.autocrlf", "false")
    _git(dest, "config", "core.fileMode", "false")

    # The FINAL state of every file must be its exact source content, so
    # the scanned tree == the committed fixture tree. A file may appear in
    # several commits (to create churn + co-change signal); only its LAST
    # occurrence writes the real source bytes. Earlier occurrences write a
    # deterministic "in-progress" stub (real content + a churn marker) so
    # each re-touch is a genuine, reproducible diff rather than a no-op.
    last_commit_idx: dict[str, int] = {}
    for idx, commit in enumerate(commits):
        for rel in commit.get("files", []):
            last_commit_idx[rel] = idx

    for idx, commit in enumerate(commits):
        files: list[str] = commit.get("files", [])
        date: str = commit["date"]
        message: str = commit["message"]

        for rel in files:
            src_file = src / rel
            dst_file = dest / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            if idx == last_commit_idx[rel]:
                # Final, real content.
                shutil.copy2(src_file, dst_file)
            else:
                # Deterministic in-progress stub: the real bytes plus a
                # commit-keyed churn marker so this is a real diff and the
                # NEXT touch of this file is also a real diff.
                dst_file.write_bytes(
                    _stub_bytes(src_file, marker_idx=idx)
                )
            _git(dest, "add", "--", rel)

        # Pin BOTH author and committer dates so the SHA + git-log order
        # are byte-stable across runs and machines.
        env = {
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
            # Inherit PATH etc. — git needs them — but the four DATE/NAME
            # vars above fully determine the commit identity.
            "PATH": _inherited_path(),
        }
        _git(dest, "commit", "-q", "-m", message, env=env)

    return dest


def _inherited_path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


# Comment leader per file extension, so the churn marker is a valid
# no-op line in that language (keeps intermediate revisions parseable
# if anything ever reads them — though only the final revision is
# scanned).
_COMMENT_LEADER: dict[str, str] = {
    ".py": "# ",
    ".ts": "// ",
    ".tsx": "// ",
    ".js": "// ",
    ".jsx": "// ",
    ".txt": "# ",
}


def _stub_bytes(src_file: Path, *, marker_idx: int) -> bytes:
    """Real source bytes + a deterministic, commit-keyed churn marker.

    For JSON (and any unknown extension) we cannot append a comment
    without breaking the file, so we vary a leading byte-stable form
    instead: a single trailing newline count keyed off ``marker_idx``.
    The marker is fully determined by the file + commit index, so two
    runs produce identical intermediate blobs.
    """
    raw = src_file.read_bytes()
    leader = _COMMENT_LEADER.get(src_file.suffix)
    if leader is not None:
        marker = f"{leader}wip r{marker_idx}\n".encode("utf-8")
        if raw and not raw.endswith(b"\n"):
            raw += b"\n"
        return raw + marker
    # JSON / unknown: append a deterministic number of trailing newlines
    # (whitespace-only diff — still a real, reproducible change).
    return raw.rstrip(b"\n") + (b"\n" * (marker_idx + 1))
