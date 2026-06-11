#!/usr/bin/env python
"""Score file-level feature MEMBERSHIP of a scan against ground truth.

Measures how completely each ground-truth feature's member FILES are
recovered by the best-matching scan feature. Pure measurement — no
pass/fail thresholds.

Usage:
    python eval/membership/score_membership.py \
        <feature-map.json> <ground-truth.yaml> \
        [--layer dev|product|both] [--repo /path/to/repo] [--json]

Matching (documented design choice):
- Candidate scan features come from ``developer_features`` and/or
  ``product_features`` (``--layer``, default ``dev``).
- Each scan feature's ``paths`` may contain files OR directories.
  Directories are expanded to concrete files via ``git ls-files`` when
  ``--repo`` is given; without ``--repo`` a directory entry contributes
  exactly the truth files that fall under it (recall is then exact, but
  precision treats the directory as containing only those files, i.e.
  it is an upper bound — a warning is printed).
- Greedy 1:1 best-match by JACCARD overlap of file sets: all
  (truth, scan) pairs are sorted by Jaccard descending and assigned
  greedily so each truth feature and each scan feature is used at most
  once. Jaccard (not raw intersection / truth-recall) is used for
  MATCHING so a giant junk-drawer scan feature cannot absorb every
  truth feature; the reported per-feature metrics are then plain
  precision/recall of the matched pair. Ties break deterministically
  by (truth name, scan name).
- A truth feature with zero file overlap against every scan feature is
  UNMATCHED (P = R = 0).

Reported:
- per-truth-feature file precision / recall (+ matched scan feature)
- micro average (pooled file counts) and macro average (mean of
  per-feature values; unmatched features count as 0)
- list of unmatched truth features
- ``--json`` machine-readable output of all of the above
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

Truth = dict[str, list[str]]  # name -> files


def load_truth(path: Path) -> Truth:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {f["name"]: list(f["files"]) for f in doc["features"]}


def _git_ls_files(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def _normalize(path: str, repo_prefixes: tuple[str, ...]) -> str:
    p = path.replace("\\", "/").lstrip("./")
    for pref in repo_prefixes:
        if pref and p.startswith(pref):
            p = p[len(pref):]
    return p.strip("/")


def expand_feature_paths(
    paths: list[str],
    tracked: list[str] | None,
    truth_universe: set[str],
    repo_prefixes: tuple[str, ...] = (),
) -> set[str]:
    """Resolve a scan feature's ``paths`` to a concrete file set."""
    tracked_set = set(tracked) if tracked is not None else None
    files: set[str] = set()
    for raw in paths:
        p = _normalize(raw, repo_prefixes)
        if not p:
            continue
        if tracked_set is not None:
            if p in tracked_set:
                files.add(p)
            else:
                prefix = p + "/"
                expanded = {f for f in tracked_set if f.startswith(prefix)}
                files.update(expanded if expanded else {p})
        else:
            # no repo: file entries pass through; directory entries
            # contribute the truth files under them (precision upper bound)
            if p in truth_universe or "." in p.rsplit("/", 1)[-1]:
                files.add(p)
            prefix = p + "/"
            files.update(f for f in truth_universe if f.startswith(prefix))
    return files


def load_scan_features(
    scan: dict[str, Any], layer: str
) -> list[tuple[str, list[str]]]:
    keys = {
        "dev": ["developer_features"],
        "product": ["product_features"],
        "both": ["developer_features", "product_features"],
    }[layer]
    feats: list[tuple[str, list[str]]] = []
    for key in keys:
        for f in scan.get(key) or []:
            name = f.get("name", "?")
            if key == "product_features":
                name = f"product:{name}"
            feats.append((name, list(f.get("paths") or [])))
    return feats


def score(
    truth: Truth,
    scan_features: list[tuple[str, set[str]]],
) -> dict[str, Any]:
    truth_sets = {n: set(fs) for n, fs in truth.items()}

    pairs: list[tuple[float, str, str]] = []
    for tname, tfiles in truth_sets.items():
        for sname, sfiles in scan_features:
            inter = len(tfiles & sfiles)
            if inter == 0:
                continue
            jac = inter / len(tfiles | sfiles)
            pairs.append((jac, tname, sname))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

    scan_sets = dict(scan_features)
    assigned_truth: dict[str, str] = {}
    used_scan: set[str] = set()
    for jac, tname, sname in pairs:
        if tname in assigned_truth or sname in used_scan:
            continue
        assigned_truth[tname] = sname
        used_scan.add(sname)

    per_feature: list[dict[str, Any]] = []
    sum_inter = sum_truth = sum_scan = 0
    macro_p: list[float] = []
    macro_r: list[float] = []
    unmatched: list[str] = []
    for tname in sorted(truth_sets):
        tfiles = truth_sets[tname]
        sum_truth += len(tfiles)
        sname = assigned_truth.get(tname)
        if sname is None:
            unmatched.append(tname)
            per_feature.append(
                {
                    "truth_feature": tname,
                    "matched_scan_feature": None,
                    "truth_files": len(tfiles),
                    "scan_files": 0,
                    "intersection": 0,
                    "precision": 0.0,
                    "recall": 0.0,
                }
            )
            macro_p.append(0.0)
            macro_r.append(0.0)
            continue
        sfiles = scan_sets[sname]
        inter = len(tfiles & sfiles)
        prec = inter / len(sfiles) if sfiles else 0.0
        rec = inter / len(tfiles)
        sum_inter += inter
        sum_scan += len(sfiles)
        macro_p.append(prec)
        macro_r.append(rec)
        per_feature.append(
            {
                "truth_feature": tname,
                "matched_scan_feature": sname,
                "truth_files": len(tfiles),
                "scan_files": len(sfiles),
                "intersection": inter,
                "precision": round(prec, 4),
                "recall": round(rec, 4),
            }
        )

    n = len(truth_sets)
    return {
        "n_truth_features": n,
        "n_scan_features": len(scan_features),
        "n_matched": len(assigned_truth),
        "unmatched_truth_features": unmatched,
        "micro": {
            "precision": round(sum_inter / sum_scan, 4) if sum_scan else 0.0,
            "recall": round(sum_inter / sum_truth, 4) if sum_truth else 0.0,
        },
        "macro": {
            "precision": round(sum(macro_p) / n, 4) if n else 0.0,
            "recall": round(sum(macro_r) / n, 4) if n else 0.0,
        },
        "per_feature": per_feature,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("feature_map", type=Path)
    ap.add_argument("ground_truth", type=Path)
    ap.add_argument("--layer", choices=["dev", "product", "both"], default="dev")
    ap.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="repo root for expanding directory paths via git ls-files",
    )
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    truth = load_truth(args.ground_truth)
    scan = json.loads(args.feature_map.read_text(encoding="utf-8"))

    tracked = _git_ls_files(args.repo) if args.repo else None
    if tracked is None:
        print(
            "warning: no --repo given; directory paths in scan features "
            "expand only against truth files (precision is an upper bound)",
            file=sys.stderr,
        )

    truth_universe = {f for fs in truth.values() for f in fs}
    repo_prefixes: tuple[str, ...] = ()
    if args.repo:
        repo_prefixes = (str(args.repo.resolve()) + "/", args.repo.name + "/")

    raw_features = load_scan_features(scan, args.layer)
    scan_features = [
        (name, expand_feature_paths(paths, tracked, truth_universe, repo_prefixes))
        for name, paths in raw_features
    ]

    result = score(truth, scan_features)

    if args.as_json:
        print(json.dumps(result, indent=2))
        return 0

    print(
        f"truth features: {result['n_truth_features']}  "
        f"scan features ({args.layer}): {result['n_scan_features']}  "
        f"matched: {result['n_matched']}"
    )
    print(
        f"micro  file P={result['micro']['precision']:.3f} "
        f"R={result['micro']['recall']:.3f}"
    )
    print(
        f"macro  file P={result['macro']['precision']:.3f} "
        f"R={result['macro']['recall']:.3f}"
    )
    print()
    hdr = f"{'truth feature':<34} {'P':>6} {'R':>6} {'∩':>4} {'|T|':>4} {'|S|':>5}  matched scan feature"
    print(hdr)
    for row in result["per_feature"]:
        print(
            f"{row['truth_feature']:<34} "
            f"{row['precision']:>6.3f} {row['recall']:>6.3f} "
            f"{row['intersection']:>4} {row['truth_files']:>4} "
            f"{row['scan_files']:>5}  {row['matched_scan_feature'] or '—'}"
        )
    if result["unmatched_truth_features"]:
        print(
            f"\nunmatched truth features "
            f"({len(result['unmatched_truth_features'])}): "
            + ", ".join(result["unmatched_truth_features"])
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
