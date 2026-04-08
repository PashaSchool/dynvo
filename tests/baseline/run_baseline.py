#!/usr/bin/env python3
"""Baseline harness for the pipeline rewrite mission.

Runs `faultline analyze` against each test repo and captures:
  - feature-map JSON (output file)
  - runtime in seconds
  - tracked file count
  - feature count
  - flow count
  - stderr (for cost lines if present)

Outputs go to tests/baseline/before/ (current pipeline) or
tests/baseline/after/ (post-rewrite) depending on --label.

Usage:
    # No LLM, heuristic only (free, fast)
    python tests/baseline/run_baseline.py --label before

    # With LLM (needs ANTHROPIC_API_KEY)
    python tests/baseline/run_baseline.py --label before --llm --flows

    # Single repo
    python tests/baseline/run_baseline.py --label before --repo documenso

Results directory layout:
    tests/baseline/before/
        documenso/
            feature-map.json
            metadata.json        # runtime, file count, feature count, cost
            stderr.log
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Repo registry — add new repos here
REPOS: dict[str, Path] = {
    "documenso": Path("/Users/pkuzina/workspace/_faultlines-testrepos/documenso"),
    "cal.com": Path("/Users/pkuzina/workspace/_faultlines-testrepos/cal.com"),
    "trpc": Path("/Users/pkuzina/workspace/_faultlines-testrepos/trpc"),
    "gin": Path("/Users/pkuzina/workspace/_faultlines-testrepos/gin"),
    "fastapi": Path("/Users/pkuzina/workspace/fastapi"),
}

BASELINE_ROOT = Path(__file__).parent


def count_tracked_files(repo: Path) -> int:
    r = subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, text=True, timeout=30
    )
    if r.returncode != 0:
        return 0
    return sum(1 for line in r.stdout.splitlines() if line.strip())


def summarize_feature_map(path: Path) -> dict:
    """Extract high-signal metrics from a feature-map JSON."""
    if not path.exists():
        return {"error": "feature-map.json not produced"}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return {"error": f"invalid JSON: {exc}"}

    features = data.get("features", []) or []
    feature_count = len(features)
    flow_count = sum(len(f.get("flows", []) or []) for f in features)
    test_feature_names = [
        f.get("name", "") for f in features
        if any(
            token in (f.get("name", "").lower())
            for token in ("test", "tests", "__tests__", "e2e", "integration")
        )
    ]

    # Feature size distribution
    sizes = [len(f.get("paths", []) or []) for f in features]
    sizes.sort(reverse=True)

    return {
        "feature_count": feature_count,
        "flow_count": flow_count,
        "test_features": test_feature_names,
        "feature_sizes_top10": sizes[:10],
        "feature_names": [f.get("name", "") for f in features][:50],
    }


def run_one(
    name: str,
    repo: Path,
    out_dir: Path,
    use_llm: bool,
    use_flows: bool,
    model: str | None,
    timeout_s: int = 600,
) -> dict:
    """Run faultline against one repo, return result metadata."""
    if not repo.exists():
        return {"status": "missing", "repo_path": str(repo)}

    out_dir.mkdir(parents=True, exist_ok=True)
    feature_map_path = out_dir / "feature-map.json"
    stderr_path = out_dir / "stderr.log"

    cmd = [
        "faultline", "analyze", str(repo),
        "--output", str(feature_map_path),
        "--days", "365",
    ]
    if use_llm:
        cmd.append("--llm")
        if use_flows:
            cmd.append("--flows")
        if model:
            cmd.extend(["--model", model])

    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "runtime_s": timeout_s,
            "cmd": " ".join(cmd),
        }
    elapsed = time.time() - start

    stderr_path.write_text(proc.stderr or "")

    result = {
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "runtime_s": round(elapsed, 1),
        "tracked_files": count_tracked_files(repo),
        "cmd": " ".join(cmd),
        "llm_enabled": use_llm,
        "flows_enabled": use_flows,
    }
    result.update(summarize_feature_map(feature_map_path))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--label",
        required=True,
        help="Snapshot directory name under tests/baseline/ (e.g. before, before-llm, after, after-llm)",
    )
    ap.add_argument(
        "--repo",
        help="Run on a single repo (default: all)",
        choices=list(REPOS.keys()),
    )
    ap.add_argument("--llm", action="store_true", help="Enable --llm flag")
    ap.add_argument("--flows", action="store_true", help="Enable --flows flag")
    ap.add_argument("--model", help="Override LLM model")
    ap.add_argument(
        "--timeout", type=int, default=600,
        help="Per-repo timeout in seconds (default 600, raise to 1800 for cal.com).",
    )
    args = ap.parse_args()

    if args.llm and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: --llm requires ANTHROPIC_API_KEY in env", file=sys.stderr)
        return 2

    selected = [args.repo] if args.repo else list(REPOS.keys())
    results: dict[str, dict] = {}
    label_dir = BASELINE_ROOT / args.label

    for name in selected:
        repo = REPOS[name]
        out_dir = label_dir / name
        print(f"\n{'=' * 60}")
        print(f"▶ {name} ({repo})")
        print(f"{'=' * 60}")
        result = run_one(
            name=name,
            repo=repo,
            out_dir=out_dir,
            use_llm=args.llm,
            use_flows=args.flows,
            model=args.model,
            timeout_s=args.timeout,
        )
        results[name] = result
        # Write per-repo metadata
        (out_dir / "metadata.json").write_text(json.dumps(result, indent=2))
        # Print summary line
        status = result.get("status", "?")
        fc = result.get("feature_count", "-")
        flc = result.get("flow_count", "-")
        rt = result.get("runtime_s", "-")
        print(f"  → status={status} features={fc} flows={flc} runtime={rt}s")

    # Write aggregate summary
    summary_path = label_dir / "summary.json"
    summary_path.write_text(json.dumps(results, indent=2))
    print(f"\n{'=' * 60}")
    print(f"Summary written to {summary_path}")
    print(f"{'=' * 60}")

    # Print comparison table
    print(f"\n{'repo':<12} {'status':<10} {'files':>7} {'features':>9} {'flows':>6} {'runtime':>9}")
    print("-" * 60)
    for name, r in results.items():
        print(
            f"{name:<12} {r.get('status', '?'):<10} "
            f"{r.get('tracked_files', '-'):>7} "
            f"{r.get('feature_count', '-'):>9} "
            f"{r.get('flow_count', '-'):>6} "
            f"{r.get('runtime_s', '-'):>8}s"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
