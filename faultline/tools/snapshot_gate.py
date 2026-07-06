"""Snapshot gate — machine proof that old stacks did not move (G3 of
the StackProfile architecture spec, Phase A).

Runs a **deterministic-only** scan (every LLM stage hard-off: no
``ANTHROPIC_API_KEY`` in the child env → every LLM client factory
returns ``None`` and the stage takes its deterministic fallback; all
LLM caches disabled; scan-result cache off) of each pinned repo,
normalizes the output JSON (:mod:`faultline.tools.normalize_scan`) and
compares its SHA256 against the checked-in lock file
``faultline/pipeline_v2/profiles/snapshots.lock.json``.

Modes::

    python -m faultline.tools.snapshot_gate --check              # CI gate
    python -m faultline.tools.snapshot_gate --check --profile next-app-router
    python -m faultline.tools.snapshot_gate --check --workers 4  # parallel
    python -m faultline.tools.snapshot_gate --update             # re-pin
    python -m faultline.tools.snapshot_gate --update --only formbricks

Two-speed rule (see ``profiles/README.md``): a PR touching only
``profiles/<x>`` runs ``--check --profile <x>``; a PR touching any
shared stage runs the full matrix (plain ``--check``).

Design points:

* Each repo scans in a **subprocess** with a scrubbed environment and a
  fresh ``FAULTLINES_RUN_DIR`` temp dir — no key, no caches, no state
  bleed between repos, and module-level env reads in the engine are
  re-evaluated per scan.
* The pinned clone is treated **read-only**; the gate verifies its HEAD
  commit against the lock and refuses to compare digests across a
  drifted checkout (fail loud, never mask).
* ``days`` is pinned generously (3650) in the lock: a sliding 365-day
  window would silently rot digests as commits age out of range even
  though the clone itself is frozen.
* Wall-time stage budgets are pinned very high so a slow CI box can
  never truncate enrichment output and fake a diff.
* The scan's ``scan_meta.framework_profile`` must equal the lock's
  ``profile`` — the gate doubles as an end-to-end selection fixture.

**Parallelism.** The per-repo scans are independent subprocesses, so the
gate runs up to ``--workers N`` of them concurrently (default
``min(3, #repos)``; override with ``--workers`` or the
``FAULTLINE_GATE_WORKERS`` env). Isolation is *absolute* and unchanged
by concurrency: every worker owns its own ``tempfile.TemporaryDirectory``
(state dir) and builds its subprocess env from a fresh ``dict(os.environ)``
snapshot passed per-subprocess — no shared mutable state, no ``os.environ``
mutation. Output is **scheduling-independent**: each repo's console block
and any failures are *buffered* and emitted in the canonical serial order
(``sorted`` by slug) regardless of completion order, and under
``--update`` the lock is written exactly **once**, after every repo has
finished, in the same byte-identical serialization as the serial path.
``--workers 1`` takes a literal serial code path (no executor).

*Memory assumption:* the pinned set holds no ``supabase``/Soc0-class
giants, so a small fixed worker cap keeps concurrent memory bounded — a
handful of deterministic-only scans fit comfortably. Raise the cap only
after confirming the pinned clones stay modest.

$0, no network, no API key. Runs anywhere the pinned clones exist.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultline.tools.normalize_scan import scan_digest

LOCK_PATH = (
    Path(__file__).resolve().parents[1]
    / "pipeline_v2"
    / "profiles"
    / "snapshots.lock.json"
)

#: Default concurrent-worker cap when neither ``--workers`` nor
#: ``FAULTLINE_GATE_WORKERS`` is set. Kept small on purpose (see the
#: module docstring's memory assumption).
_DEFAULT_MAX_WORKERS = 3

#: Env var override for the worker count (lower precedence than the
#: ``--workers`` CLI flag, higher than the built-in default).
_WORKERS_ENV = "FAULTLINE_GATE_WORKERS"

#: Env that must NOT reach the scan subprocess — with no key every LLM
#: client factory returns ``None`` and stages degrade deterministically.
_ENV_STRIP = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)

#: Deterministic-only scan environment. Caches off (belt) on top of a
#: fresh FAULTLINES_RUN_DIR (braces); budgets pinned high so wall-clock
#: truncation can never alter content.
_ENV_SET = {
    "FAULTLINES_CACHE_BACKEND": "fs",
    "FAULTLINE_SCAN_CACHE": "0",
    "FAULTLINE_SCAN_CACHE_BYPASS": "1",
    "FAULTLINE_STAGE_0_5_CACHE": "0",
    "FAULTLINE_STAGE_6_7B_CACHE": "0",
    "FAULTLINE_STAGE_6_7C_CACHE": "0",
    "FAULTLINE_STAGE_8_CACHE": "0",
    "FAULTLINE_STAGE_6_3_BUDGET_SEC": "100000",
    "FAULTLINE_STAGE_6_4_BUDGET_SEC": "100000",
    "FAULTLINE_STAGE_6_6_BUDGET_SEC": "100000",
    "FAULTLINE_IMPACT_BUDGET_SEC": "100000",
}


@dataclass
class _RepoResult:
    """Scheduling-independent outcome of one repo's gate scan.

    A worker returns exactly what the serial loop body *would have*
    printed and mutated, as plain data — never touching shared state or
    stdout itself. The main thread then replays these in canonical order:
    prints ``lines``, extends the failures list with ``failures``, and
    applies ``new_commit_sha`` / ``new_digest`` to the lock pin. This is
    what makes the console output and the written lock byte-identical to
    the serial path irrespective of which worker finishes first.
    """

    slug: str
    lines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    new_commit_sha: str | None = None
    new_digest: str | None = None


def _load_lock(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_lock(lock: dict[str, Any], lock_path: Path) -> None:
    """Serialize the lock exactly as the serial gate always has.

    Isolated behind a helper so the "written once, after every repo
    finished, byte-identical format" property is a single call site the
    tests can spy on.
    """
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _scan_env(state_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    for key in _ENV_STRIP:
        env.pop(key, None)
    env.update(_ENV_SET)
    env["FAULTLINES_RUN_DIR"] = str(state_dir)
    return env


def _run_scan(repo: Path, days: int, out_json: Path, state_dir: Path) -> None:
    """Foreground, blocking, deterministic-only scan in a subprocess."""
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--scan-one",
        str(repo),
        "--days",
        str(days),
        "--out",
        str(out_json),
    ]
    proc = subprocess.run(cmd, env=_scan_env(state_dir))
    if proc.returncode != 0:
        raise RuntimeError(f"deterministic scan failed for {repo} (rc={proc.returncode})")


def _scan_one(repo: str, days: int, out: str) -> int:
    """Child-process body: run the pipeline, write JSON to ``out``."""
    from faultline.pipeline_v2.run import run_pipeline_v2

    run_pipeline_v2(
        repo,
        days=days,
        out_path=Path(out),
        run_id="snapshot-gate",
    )
    return 0


def _scan_repo_worker(
    slug: str,
    pin: dict[str, Any],
    days: int,
    update: bool,
) -> _RepoResult:
    """One repo's gate scan, as a **pure** unit of work.

    Mirrors the serial loop body exactly but touches no shared state:
    reads only from ``pin``, returns a :class:`_RepoResult` carrying the
    console lines, failures, and any lock mutations for the main thread
    to replay in canonical order. Safe to run concurrently — the only
    external effects are its own git ``rev-parse`` (read-only) and its
    own subprocess scan in a private temp dir.
    """
    res = _RepoResult(slug=slug)
    repo = Path(pin["path"]).expanduser()
    if not repo.is_dir():
        res.failures.append(f"{slug}: pinned clone missing at {repo}")
        return res

    head = _head_sha(repo)
    if head != pin["commit_sha"]:
        if update:
            res.new_commit_sha = head
            res.lines.append(f"{slug}: re-pinning commit_sha → {head[:10]}")
        else:
            res.failures.append(
                f"{slug}: clone HEAD {head[:10]} != pinned "
                f"{pin['commit_sha'][:10]} — refusing to compare digests "
                "across a drifted checkout (re-pin with --update)"
            )
            return res

    with tempfile.TemporaryDirectory(prefix=f"snapgate-{slug}-") as tmp:
        out_json = Path(tmp) / "scan.json"
        res.lines.append(f"{slug}: deterministic scan of {repo} (days={days}) ...")
        # Live progress on stderr only — never the canonical stdout stream.
        print(f"[snapshot-gate] {slug}: scanning …", file=sys.stderr, flush=True)
        _run_scan(repo, days, out_json, Path(tmp) / "state")
        doc = json.loads(out_json.read_text(encoding="utf-8"))

    got_profile = (doc.get("scan_meta") or {}).get("framework_profile")
    if got_profile != pin["profile"]:
        res.failures.append(
            f"{slug}: selected profile {got_profile!r} != pinned {pin['profile']!r}"
        )
        return res

    digest = scan_digest(doc)
    if update:
        res.new_digest = digest
        res.lines.append(f"{slug}: digest {digest[:23]}… profile={got_profile}")
    elif digest != pin["digest"]:
        res.failures.append(
            f"{slug}: digest drift\n    got      {digest}\n    expected {pin['digest']}"
        )
    else:
        res.lines.append(f"{slug}: OK ({digest[:23]}…, profile={got_profile})")
    return res


def _resolve_workers(cli_workers: int | None, n_repos: int) -> int:
    """Precedence: ``--workers`` flag > ``FAULTLINE_GATE_WORKERS`` env >
    ``min(_DEFAULT_MAX_WORKERS, n_repos)``. Clamped to ``[1, n_repos]``;
    a non-positive or unparseable value falls back to the default."""
    candidate: int | None = None
    if cli_workers is not None:
        candidate = cli_workers
    else:
        env_val = os.environ.get(_WORKERS_ENV)
        if env_val:
            try:
                candidate = int(env_val)
            except ValueError:
                candidate = None
    if candidate is None or candidate <= 0:
        candidate = min(_DEFAULT_MAX_WORKERS, n_repos)
    return max(1, min(candidate, n_repos))


def _collect_results(
    selected: dict[str, dict[str, Any]],
    days: int,
    update: bool,
    workers: int,
) -> dict[str, _RepoResult | BaseException]:
    """Run every selected repo's worker and key the outcomes by slug.

    ``workers <= 1`` takes a literal serial path (no executor) so it is
    indistinguishable from the historical single-threaded gate. For
    ``workers > 1`` a :class:`ThreadPoolExecutor` runs up to ``workers``
    scans at once — threads suffice because each scan blocks in a
    subprocess (GIL released). A worker exception is captured (not
    raised here) so the pool drains fully and cleanly; the main thread
    re-raises it deterministically at the failing repo's canonical
    position.
    """
    slugs = sorted(selected)
    results: dict[str, _RepoResult | BaseException] = {}
    if workers <= 1:
        for slug in slugs:
            try:
                results[slug] = _scan_repo_worker(slug, selected[slug], days, update)
            except BaseException as exc:  # noqa: BLE001 — replayed in canonical order
                results[slug] = exc
        return results

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_scan_repo_worker, slug, selected[slug], days, update): slug
            for slug in slugs
        }
        for future in as_completed(futures):
            slug = futures[future]
            try:
                results[slug] = future.result()
            except BaseException as exc:  # noqa: BLE001 — replayed in canonical order
                results[slug] = exc
    return results


def _gate(
    lock_path: Path,
    *,
    update: bool,
    only: str | None,
    profile: str | None,
    workers: int | None = None,
) -> int:
    lock = _load_lock(lock_path)
    days = int(lock["scan_config"]["days"])
    repos: dict[str, dict[str, Any]] = lock["repos"]

    selected = {
        slug: pin
        for slug, pin in repos.items()
        if (only is None or slug == only)
        and (profile is None or pin["profile"] == profile)
    }
    if not selected:
        print(f"snapshot-gate: no pinned repos match only={only!r} profile={profile!r}")
        return 2

    n_workers = _resolve_workers(workers, len(selected))
    results = _collect_results(selected, days, update, n_workers)

    # Replay every repo's outcome in the canonical (serial) order so the
    # console stream, the failure report, and the written lock are all
    # scheduling-independent.
    failures: list[str] = []
    for slug in sorted(selected):
        res = results[slug]
        if isinstance(res, BaseException):
            # A scan-subprocess crash: preserve today's semantics — surface
            # it (exit nonzero with the same exception) at this repo's
            # canonical position, exactly where the serial gate would crash.
            raise res
        for line in res.lines:
            print(line, flush=True)
        failures.extend(res.failures)
        if res.new_commit_sha is not None:
            selected[slug]["commit_sha"] = res.new_commit_sha
        if res.new_digest is not None:
            selected[slug]["digest"] = res.new_digest

    if update and not failures:
        _write_lock(lock, lock_path)
        print(f"snapshot-gate: lock updated → {lock_path}")

    if failures:
        print("\nsnapshot-gate FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m faultline.tools.snapshot_gate",
        description="Deterministic-only per-profile snapshot gate.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare digests (CI mode)")
    mode.add_argument("--update", action="store_true", help="regenerate the lock file")
    parser.add_argument("--only", metavar="SLUG", help="restrict to one pinned repo")
    parser.add_argument(
        "--profile", metavar="NAME", help="restrict to one profile's snapshot set"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help=(
            "concurrent repo scans (default min(3, #repos); also settable "
            f"via {_WORKERS_ENV}; --workers 1 forces the serial path)"
        ),
    )
    parser.add_argument("--lock", type=Path, default=LOCK_PATH, help=argparse.SUPPRESS)
    # Internal child-process mode.
    parser.add_argument("--scan-one", metavar="REPO", help=argparse.SUPPRESS)
    parser.add_argument("--days", type=int, default=3650, help=argparse.SUPPRESS)
    parser.add_argument("--out", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.scan_one:
        return _scan_one(args.scan_one, args.days, args.out)

    return _gate(
        args.lock,
        update=args.update,
        only=args.only,
        profile=args.profile,
        workers=args.workers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
