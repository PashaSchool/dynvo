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

$0, no network, no API key. Runs anywhere the pinned clones exist.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from faultline.tools.normalize_scan import scan_digest

LOCK_PATH = (
    Path(__file__).resolve().parents[1]
    / "pipeline_v2"
    / "profiles"
    / "snapshots.lock.json"
)

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


def _load_lock(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def _gate(
    lock_path: Path,
    *,
    update: bool,
    only: str | None,
    profile: str | None,
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

    failures: list[str] = []
    for slug in sorted(selected):
        pin = selected[slug]
        repo = Path(pin["path"]).expanduser()
        if not repo.is_dir():
            failures.append(f"{slug}: pinned clone missing at {repo}")
            continue

        head = _head_sha(repo)
        if head != pin["commit_sha"]:
            if update:
                pin["commit_sha"] = head
                print(f"{slug}: re-pinning commit_sha → {head[:10]}")
            else:
                failures.append(
                    f"{slug}: clone HEAD {head[:10]} != pinned "
                    f"{pin['commit_sha'][:10]} — refusing to compare digests "
                    "across a drifted checkout (re-pin with --update)"
                )
                continue

        with tempfile.TemporaryDirectory(prefix=f"snapgate-{slug}-") as tmp:
            out_json = Path(tmp) / "scan.json"
            print(f"{slug}: deterministic scan of {repo} (days={days}) ...", flush=True)
            _run_scan(repo, days, out_json, Path(tmp) / "state")
            doc = json.loads(out_json.read_text(encoding="utf-8"))

        got_profile = (doc.get("scan_meta") or {}).get("framework_profile")
        if got_profile != pin["profile"]:
            failures.append(
                f"{slug}: selected profile {got_profile!r} != pinned {pin['profile']!r}"
            )
            continue

        digest = scan_digest(doc)
        if update:
            pin["digest"] = digest
            print(f"{slug}: digest {digest[:23]}… profile={got_profile}")
        elif digest != pin["digest"]:
            failures.append(
                f"{slug}: digest drift\n    got      {digest}\n    expected {pin['digest']}"
            )
        else:
            print(f"{slug}: OK ({digest[:23]}…, profile={got_profile})")

    if update and not failures:
        lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
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
    )


if __name__ == "__main__":
    raise SystemExit(main())
