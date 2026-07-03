"""Historical git-snapshot machinery for impact-over-time (Stage 6.96).

Three small, composable pieces — all deterministic, $0 LLM:

  1. :func:`select_snapshot_commits` — pick N commits evenly spaced over
     the scan window from the commit list ALREADY in memory
     (``ScanContext.commits``; no new ``git log``).
  2. :func:`run_snapshots` — materialise each snapshot with
     ``git worktree add --detach``, run a caller-supplied computation
     against the checked-out tree, and ALWAYS clean the worktree up.
     Robust by contract: per-snapshot failure skips that point with a
     logged warning (never fails the scan); a wall-clock budget skips
     the remaining snapshots when exceeded.
  3. :func:`build_snapshot_import_index` / :func:`impact_reach` — a lean
     reverse-import index over one snapshot's file tree, answering
     "how many source files OUTSIDE this entity import >=1 member
     file?".

Resolver design — lean re-use, not a re-implementation
======================================================
TS/JS import RESOLUTION is delegated verbatim to the engine's existing
machinery (:func:`faultline.analyzer.import_graph._resolve_import` with
``build_path_alias_map`` / ``detect_monorepo_packages`` /
``detect_workspace_package_map`` pointed at the SNAPSHOT tree, so each
snapshot is resolved against its OWN manifests). Only import-specifier
EXTRACTION is local: the pipeline's ``extract_signatures`` keeps whole
file sources in memory and computes exports/symbol ranges we don't
need, which is wasteful x N snapshots. Python imports get a small
dotted-path resolver (relative dots + repo-root / ``src/`` roots) —
``FileSignature.imports`` never carried Python imports, so there is
nothing upstream to reuse there.

CONSISTENCY over absolute precision: the SAME extraction regexes and
the SAME resolver rules run at every snapshot of a scan, so the reach
SERIES is comparable point-to-point even where any single point
under-resolves an exotic import style.

Budget choice (mirrors Stage 6.3's scale-invariant pattern)
===========================================================
The wall budget is a PER-SNAPSHOT time allowance multiplied by the
number of planned snapshots — not a flat wall a big repo blows
(``rule-no-magic-tuning``: the budget scales with the work unit count,
exactly like Stage 6.3's per-feature allowance). The allowance itself
(60 s) is an order-of-magnitude safety ceiling: the validated
prototype needed ~1.3 s/snapshot on documenso, so the budget only ever
fires on a pathological repo (multi-GB checkouts, network filesystems).
Override the resolved wall with ``FAULTLINE_IMPACT_BUDGET_SEC``
(absolute seconds; 0 disables the guard) or the per-snapshot allowance
with ``FAULTLINE_IMPACT_PER_SNAPSHOT_SEC``.

Multi-subpath scans (follow-up noted)
=====================================
Each per-subpath run currently selects + materialises its own
snapshots (cheap: the worktree checkout is the dominant cost and
hard-links pack data). Sharing one worktree set across the subpath
runs of a single multi-scan is a known optimisation, deferred until
the multi-runner grows a cross-subpath context to host it.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, TypeVar

from faultline.analyzer.import_graph import (
    _resolve_import,
    detect_monorepo_packages,
    detect_workspace_package_map,
)
from faultline.analyzer.tsconfig_paths import build_path_alias_map
from faultline.analyzer.reverse_imports import (
    _MAX_FILE_BYTES,
    _TEST_PATH_MARKERS,
    _VENDOR_PATH_MARKERS,
)
from faultline.models.types import Commit

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SNAPSHOT_COUNT",
    "DEFAULT_PER_SNAPSHOT_BUDGET_SEC",
    "SnapshotImportIndex",
    "select_snapshot_commits",
    "resolve_snapshot_budget_sec",
    "run_snapshots",
    "list_snapshot_files",
    "build_snapshot_import_index",
    "impact_reach",
]

# Default number of historical snapshots per scan. 8 points draw a
# readable trend line (the Phase-0 prototype validated decision-grade
# stories at 8) while keeping the added wall time in single-digit
# seconds; ceil'd down to the available history when shorter.
DEFAULT_SNAPSHOT_COUNT: int = 8

# Per-snapshot wall-time allowance (seconds). Effective budget =
# allowance * planned snapshots — see the module docstring for the
# rationale and the env overrides.
DEFAULT_PER_SNAPSHOT_BUDGET_SEC: float = 60.0

_ENV_BUDGET_ABS = "FAULTLINE_IMPACT_BUDGET_SEC"
_ENV_BUDGET_PER_SNAPSHOT = "FAULTLINE_IMPACT_PER_SNAPSHOT_SEC"

# Languages the lean indexer parses. TS/JS + Python — the same set the
# engine's reverse-import seeding scans (reverse_imports), minus
# languages whose import graphs the engine doesn't resolve to files yet.
_TS_JS_EXTENSIONS: frozenset[str] = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs", ".cts", ".cjs",
})
_PY_EXTENSIONS: frozenset[str] = frozenset({".py"})
_SCANNED_EXTENSIONS: frozenset[str] = _TS_JS_EXTENSIONS | _PY_EXTENSIONS

# TS/JS import-specifier extraction. One pass per file; matches the
# union of the shapes the engine recognises elsewhere
# (ast_extractor._RE_IMPORT + reverse_imports' require/dynamic forms):
#   import X from "mod"   /  export { X } from "mod"  → from "mod"
#   import "mod"          (bare side-effect import)
#   require("mod")        /  import("mod")            (dynamic)
_RE_TS_IMPORT = re.compile(
    r"""(?:\bfrom\s*|^\s*import\s+|\brequire\s*\(\s*|\bimport\s*\(\s*)
        ['"]([^'"\n]+)['"]""",
    re.MULTILINE | re.VERBOSE,
)

# Python import extraction: ``from .a.b import X`` / ``import a.b, c``.
_RE_PY_FROM = re.compile(r"^\s*from\s+([.\w]+)\s+import\s", re.MULTILINE)
_RE_PY_IMPORT = re.compile(
    r"^\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)", re.MULTILINE,
)


def _is_vendor_or_test(path: str) -> bool:
    """Same skip set the engine's reverse-import seeding uses, so
    "external consumer" means the same thing here as in Stage 6.3."""
    needle = "/" + path
    return any(m in needle for m in _VENDOR_PATH_MARKERS) or any(
        m in needle for m in _TEST_PATH_MARKERS
    )


# ── 1. Snapshot selection ────────────────────────────────────────────────


def _week_label(commit: Commit) -> str:
    iso = commit.date.date().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def select_snapshot_commits(
    commits: list[Commit],
    n: int = DEFAULT_SNAPSHOT_COUNT,
) -> list[Commit]:
    """Pick up to ``n`` commits evenly spaced over the scan window.

    Input is ``ScanContext.commits`` (any order); output is ascending
    by date. Spacing is index-based over the date-sorted list — even in
    COMMIT-ACTIVITY terms, which matches what the reach series narrates
    (codebase states between which development happened). The first and
    last commits of the window are always included so the series spans
    the whole window. When several picked commits fall in the same ISO
    week, only the LATEST survives (one ImpactPoint per week — the
    series is week-keyed downstream). Short history (``len <= n``)
    keeps every distinct-week commit. Deterministic: ties on date break
    on sha.
    """
    if n <= 0 or not commits:
        return []
    ordered = sorted(commits, key=lambda c: (c.date, c.sha))
    m = len(ordered)
    if m <= n:
        picked = ordered
    else:
        idxs = sorted({round(i * (m - 1) / (n - 1)) for i in range(n)})
        picked = [ordered[i] for i in idxs]
    # Collapse same-ISO-week picks to the latest commit of that week.
    by_week: dict[str, Commit] = {}
    for c in picked:
        by_week[_week_label(c)] = c  # ascending order → latest wins
    return sorted(by_week.values(), key=lambda c: (c.date, c.sha))


# ── 2. Worktree runner ───────────────────────────────────────────────────


T = TypeVar("T")


def resolve_snapshot_budget_sec(n_snapshots: int) -> float:
    """Resolve the wall budget for ``n_snapshots`` (see module docstring).

    Returns ``0.0`` to mean "disabled" (only via the absolute env
    override).
    """
    abs_override = os.environ.get(_ENV_BUDGET_ABS)
    if abs_override is not None:
        try:
            return max(0.0, float(abs_override))
        except ValueError:
            logger.warning(
                "ignoring non-numeric %s=%r", _ENV_BUDGET_ABS, abs_override,
            )
    per = DEFAULT_PER_SNAPSHOT_BUDGET_SEC
    per_override = os.environ.get(_ENV_BUDGET_PER_SNAPSHOT)
    if per_override is not None:
        try:
            per = max(0.0, float(per_override))
        except ValueError:
            logger.warning(
                "ignoring non-numeric %s=%r",
                _ENV_BUDGET_PER_SNAPSHOT, per_override,
            )
    return per * max(1, n_snapshots)


def _git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )


def run_snapshots(
    repo_path: Path,
    shas: list[str],
    compute: Callable[[Path, str], T],
    *,
    budget_sec: float | None = None,
    log: Any = None,
) -> tuple[dict[str, T], dict[str, Any]]:
    """Materialise each sha as a detached worktree and run ``compute``.

    ``compute(worktree_root, sha)`` receives the CHECKED-OUT tree root
    (the caller appends any subpath itself). Returns
    ``(results_by_sha, telemetry)`` where telemetry carries
    ``impact_snapshots`` (completed), ``impact_skipped_snapshots``
    (failed + budget-skipped), ``impact_budget_exceeded`` and
    ``budget_sec``.

    Robustness contract (never fails the scan):
      - ``git worktree prune`` first — clears stale registrations from
        a previously interrupted run before we add new worktrees.
      - per-snapshot try/finally → ``git worktree remove --force`` +
        ``rmtree`` fallback; a failing snapshot is skipped with a
        warning.
      - the wall budget is checked BEFORE each checkout; once exceeded,
        every remaining snapshot is skipped and telemetry notes it.
      - a closing ``git worktree prune`` drops any registration the
        per-snapshot cleanup could not remove.
    """
    if budget_sec is None:
        budget_sec = resolve_snapshot_budget_sec(len(shas))
    telemetry: dict[str, Any] = {
        "impact_snapshots": 0,
        "impact_skipped_snapshots": 0,
        "impact_budget_exceeded": False,
        "budget_sec": round(budget_sec, 1),
        "planned_snapshots": len(shas),
    }
    results: dict[str, T] = {}
    if not shas:
        return results, telemetry

    def _info(msg: str) -> None:
        if log is not None:
            log.info(msg)
        logger.info(msg)

    def _warn(msg: str) -> None:
        if log is not None:
            log.info(f"WARN {msg}")
        logger.warning(msg)

    try:
        _git(repo_path, "worktree", "prune")
    except (subprocess.SubprocessError, OSError) as exc:
        # Not fatal — `worktree add` may still succeed.
        _warn(f"impact: initial `git worktree prune` failed: {exc}")

    t0 = time.monotonic()
    try:
        for sha in shas:
            elapsed = time.monotonic() - t0
            if budget_sec > 0 and elapsed > budget_sec:
                telemetry["impact_budget_exceeded"] = True
                remaining = len(shas) - len(results) - (
                    telemetry["impact_skipped_snapshots"]
                )
                telemetry["impact_skipped_snapshots"] += remaining
                _warn(
                    f"impact: wall budget {budget_sec:.0f}s exceeded after "
                    f"{elapsed:.1f}s — skipping {remaining} remaining "
                    f"snapshot(s)",
                )
                break
            tmpdir = tempfile.mkdtemp(prefix="faultline-impact-")
            try:
                _git(
                    repo_path, "worktree", "add",
                    "--detach", "--force", tmpdir, sha,
                )
                results[sha] = compute(Path(tmpdir), sha)
                telemetry["impact_snapshots"] += 1
            except Exception as exc:  # noqa: BLE001 — never fail the scan
                telemetry["impact_skipped_snapshots"] += 1
                detail: object = exc
                if isinstance(exc, subprocess.CalledProcessError):
                    detail = (exc.stderr or "").strip() or exc
                _warn(f"impact: snapshot {sha[:12]} skipped: {detail}")
            finally:
                try:
                    _git(repo_path, "worktree", "remove", "--force", tmpdir)
                except (subprocess.SubprocessError, OSError):
                    pass
                shutil.rmtree(tmpdir, ignore_errors=True)
    finally:
        try:
            _git(repo_path, "worktree", "prune")
        except (subprocess.SubprocessError, OSError) as exc:
            _warn(f"impact: closing `git worktree prune` failed: {exc}")
    _info(
        f"impact: snapshots done ok={telemetry['impact_snapshots']} "
        f"skipped={telemetry['impact_skipped_snapshots']} "
        f"elapsed={time.monotonic() - t0:.1f}s",
    )
    return results, telemetry


# ── 3. Reverse-import index + reach ──────────────────────────────────────


def list_snapshot_files(scan_root: Path) -> list[str]:
    """Sorted relative paths of every regular file under ``scan_root``.

    A detached worktree contains exactly the tracked tree, so a
    filesystem walk IS the tracked-file list at that commit (no extra
    git call). ``.git`` entries and symlinks are skipped. Forward-slash
    relative paths, sorted — deterministic.
    """
    out: list[str] = []
    root = str(scan_root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        rel_dir = os.path.relpath(dirpath, root)
        for fn in sorted(filenames):
            if fn == ".git":
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            out.append(rel.replace("\\", "/"))
    out.sort()
    return out


@dataclass
class SnapshotImportIndex:
    """Reverse import map of one snapshot's file tree.

    ``importers_of[target]`` = set of (vendor/test-filtered) source
    files that import ``target``. ``files`` is the FULL tracked set at
    the snapshot (all extensions) so member presence can be checked for
    non-code members too.
    """

    files: frozenset[str]
    importers_of: dict[str, set[str]] = field(default_factory=dict)


def _python_candidates(importer: str, spec: str) -> list[str]:
    """Candidate relative paths for a Python import specifier.

    Relative (``.x`` / ``..pkg.mod``): resolved against the importer's
    package directory per the standard semantics (one leading dot =
    the importer's own package). Absolute (``a.b.c``): tried from the
    repo root and under the conventional ``src/`` layout root — lean by
    design (no sys.path emulation); unresolved imports add no edge.
    """
    if spec.startswith("."):
        dots = len(spec) - len(spec.lstrip("."))
        remainder = spec[dots:]
        base = PurePosixPath(importer).parent
        for _ in range(dots - 1):
            base = base.parent
        parts = [p for p in remainder.split(".") if p]
        rel = "/".join([str(base), *parts]) if str(base) != "." else "/".join(parts)
        rel = rel.lstrip("/")
        if not rel:
            return []
        return [f"{rel}.py", f"{rel}/__init__.py"]
    rel = spec.replace(".", "/")
    return [
        f"{rel}.py", f"{rel}/__init__.py",
        f"src/{rel}.py", f"src/{rel}/__init__.py",
    ]


def _extract_specs(rel_path: str, text: str) -> list[str]:
    """Import specifiers found in one source file (order-preserving)."""
    suffix = PurePosixPath(rel_path).suffix.lower()
    specs: list[str] = []
    if suffix in _PY_EXTENSIONS:
        for m in _RE_PY_FROM.finditer(text):
            specs.append(m.group(1))
        for m in _RE_PY_IMPORT.finditer(text):
            for part in m.group(1).split(","):
                name = part.strip().split(" as ")[0].strip()
                if name:
                    specs.append(name)
    else:
        for m in _RE_TS_IMPORT.finditer(text):
            specs.append(m.group(1))
    return specs


def build_snapshot_import_index(
    scan_root: Path,
    files: list[str] | None = None,
) -> SnapshotImportIndex:
    """Build the reverse-import index for one snapshot tree.

    TS/JS resolution reuses the engine's resolver with the SNAPSHOT's
    own manifests (tsconfig paths, workspace package map, monorepo
    package dirs read from ``scan_root``) so alias targets move with
    history. Python uses the lean dotted resolver above. Importers in
    vendor/test paths are excluded (same markers as the engine's
    reverse-import seeding); RESOLUTION TARGETS are not filtered — an
    external test importer is excluded as an importer, but a member
    file is always a valid target.
    """
    if files is None:
        files = list_snapshot_files(scan_root)
    file_set = frozenset(files)
    root = str(scan_root)
    alias_entries = build_path_alias_map(scan_root)
    monorepo_packages = detect_monorepo_packages(root)
    workspace_map = detect_workspace_package_map(root)

    importers_of: dict[str, set[str]] = {}
    for rel in files:
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix not in _SCANNED_EXTENSIONS or _is_vendor_or_test(rel):
            continue
        abs_path = scan_root / rel
        try:
            if abs_path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        is_py = suffix in _PY_EXTENSIONS
        for spec in _extract_specs(rel, text):
            resolved: str | None = None
            if is_py:
                for cand in _python_candidates(rel, spec):
                    if cand in file_set:
                        resolved = cand
                        break
            else:
                resolved = _resolve_import(
                    rel, spec, file_set,
                    alias_entries=alias_entries,
                    monorepo_packages=monorepo_packages,
                    workspace_package_map=workspace_map,
                    repo_root=root,
                )
            if resolved and resolved != rel:
                importers_of.setdefault(resolved, set()).add(rel)
    return SnapshotImportIndex(files=file_set, importers_of=importers_of)


def impact_reach(
    member_paths: Iterable[str],
    index: SnapshotImportIndex,
) -> tuple[int, int]:
    """``(reach, members_present)`` of one entity at one snapshot.

    ``reach`` = count of source files OUTSIDE the member set that
    import at least one member file (member→member imports never
    count). ``members_present`` = how many of TODAY's member paths
    exist in the snapshot tree — the retroactive-projection honesty
    counter (members that don't exist yet contribute nothing).
    """
    members = set(member_paths)
    importers: set[str] = set()
    for m in members:
        importers.update(index.importers_of.get(m, ()))
    present = sum(1 for m in members if m in index.files)
    return len(importers - members), present


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile of a NONEMPTY list (same
    semantics as Stage 6.95's helper, over floats)."""
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(xs[lo])
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac
