"""Golden-FREE structural quality metrics for a feature-map scan.

Motivation
----------
Recall/precision against a curated answer-key (``eval/membership/``) is the
gold standard, but it only exists for a few repos and it bakes in one
curator's judgement. This module measures *structural pathologies that are
wrong on ANY repo regardless of ground truth* — so it runs on the whole
corpus and catches regressions an answer-key can't.

The pathology it was built for (observed identically on Soc0 AND infisical):
a single developer feature, named after a package root (``backend``,
``frontend-v2``, ``soc0-frontend``), absorbs a huge share of the repo's files
into one blob — the engine never attributed the service/model/page long-tail
to the features that actually use it, so it fell to the package node. The
signal of that defect is **file-share concentration**:

* ``max_feature_share``  — the largest feature's share of all attributed files.
  A healthy decomposition spreads files; a blob spikes this.
* ``top3_share`` / ``gini`` — overall concentration / inequality.
* ``blob_features``      — features that are BOTH oversized (≫ their fair
  share) AND path-concentrated under one top-level dir (the package-node
  fingerprint). Scale-invariant: "oversized" is relative to ``1/n_features``,
  not an absolute count (see ``rule-no-magic-tuning``).

These are *measurements*, not classifiers — the continuous shares are the
regression signal; the blob flag is a human-readable summary.

CLI
---
    python -m eval.structural_audit scan_a.json scan_b.json ...
    python -m eval.structural_audit corpus/*.json --json baseline.json
    python -m eval.structural_audit corpus/*.json --compare baseline.json

``--compare`` exits non-zero when any repo regresses past the tolerance, so it
drops straight into CI / a pre-merge gate for attribution changes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Names that denote a PACKAGE / structural container rather than a product
# domain. A feature named one of these is the package-node blob fingerprint:
# the engine fell back to the package root instead of naming the code. Universal
# (a vocabulary of structural words, not repo-specific paths).
_CONTAINER_NAMES = frozenset({
    "backend", "frontend", "server", "client", "web", "www", "app", "apps",
    "src", "source", "core", "lib", "libs", "packages", "package",
    "root", "monorepo", "codebase",
})


def _is_container_name(name: str, top_dir: str) -> bool:
    """True when a feature name reads like a package/container node rather than a
    product domain (``backend``, ``frontend-v2``, ``src``), or simply echoes its
    own dominant top-level directory (``frontend-v2`` over ``frontend/``)."""
    base = re.sub(r"[-_\s]?v?\d+$", "", name.strip().lower()).replace(" ", "-")
    if base in _CONTAINER_NAMES:
        return True
    td = top_dir.strip("/").lower()
    return bool(td) and base == td

# A feature is a structural blob when it owns more than this multiple of its
# "fair share" (1 / n_features) AND its files are concentrated under one
# top-level directory. Both are scale-invariant ratios, not corpus-tuned
# absolute counts. 3x fair-share = "this one feature is doing the job of three".
_BLOB_FAIR_SHARE_MULT = 3.0
# ...or simply owning a quarter of the whole repo — oversized by any standard,
# and the only criterion that fires on a small feature count (where 3x fair
# share exceeds 100% and can never trip).
_BLOB_ABS_SHARE = 0.25
# A feature owning this much of a repo under ONE top-level dir is a blob even
# without a container-y name (catches package nodes named after the repo, e.g.
# ``dify-web``, ``inbox-zero-ai``) — no legitimate single product feature spans
# 40% of a codebase.
_BLOB_SEVERE_SHARE = 0.40
# Path concentration: a blob's files cluster under a single top-level dir (the
# package-node fingerprint — ``backend/...``, ``frontend/...``).
_BLOB_CONCENTRATION = 0.70
# A tiny scan can't have a "blob"; ignore features below this absolute floor so
# 2-feature toy repos don't trip the flag. Structural floor, not a tuned knob.
_BLOB_MIN_FILES = 25

# Regression tolerances for --compare (absolute, on 0..1 shares). A change is a
# regression if concentration WORSENS (rises) past these.
_TOL_MAX_SHARE = 0.03
_TOL_TOP3_SHARE = 0.04
_TOL_GINI = 0.03


def _gini(values: list[int]) -> float:
    """Gini coefficient of a non-negative distribution (0 = perfectly even,
    →1 = all mass in one bucket). Measures how unevenly files are spread."""
    xs = sorted(v for v in values if v > 0)
    n = len(xs)
    if n == 0:
        return 0.0
    if n == 1:
        return 0.0
    cum = 0
    total = 0
    for i, x in enumerate(xs, start=1):
        cum += i * x
        total += x
    if total == 0:
        return 0.0
    return (2.0 * cum) / (n * total) - (n + 1.0) / n


def _top_dir(path: str, depth: int = 1) -> str:
    parts = path.split("/")
    return "/".join(parts[:depth]) if parts else path


@dataclass
class FeatureAudit:
    name: str
    files: int
    share: float
    top_dir: str
    top_dir_share: float
    is_blob: bool


@dataclass
class ScanAudit:
    label: str
    total_files: int
    n_dev_features: int
    n_product_features: int
    max_feature_share: float
    top3_share: float
    gini: float
    median_feature_files: float
    largest_feature: str
    blob_count: int
    files_under_blobs_pct: float
    # Attribution completeness — only meaningful on a keyed scan (a keyless scan
    # has no product clustering, so this reads 0 and is ignored in --compare).
    dev_features_with_pf_pct: float
    blobs: list[FeatureAudit] = field(default_factory=list)


def audit_scan(scan: dict[str, Any], label: str = "") -> ScanAudit:
    """Compute golden-free structural metrics for one scan dict."""
    dev = scan.get("developer_features") or scan.get("features") or []
    pfs = scan.get("product_features") or []

    per_feature: list[tuple[str, set[str]]] = []
    all_files: set[str] = set()
    for f in dev:
        paths = {p for p in (f.get("paths") or []) if isinstance(p, str) and p}
        name = str(f.get("display_name") or f.get("name") or "?")
        per_feature.append((name, paths))
        all_files |= paths

    total = len(all_files)
    n = len(per_feature)
    by_size = sorted(per_feature, key=lambda x: -len(x[1]))
    sizes = [len(p) for _, p in by_size]
    # Distinct union of the three biggest features (≤ 100%, unlike a raw sum —
    # features share files via shared_attributions).
    top3_union: set[str] = set()
    for _, paths in by_size[:3]:
        top3_union |= paths

    fair_share = (1.0 / n) if n else 0.0
    blobs: list[FeatureAudit] = []
    blob_files: set[str] = set()
    for name, paths in per_feature:
        cnt = len(paths)
        share = (cnt / total) if total else 0.0
        if cnt:
            dir_counts = Counter(_top_dir(p) for p in paths)
            top_dir, top_cnt = dir_counts.most_common(1)[0]
            dir_share = top_cnt / cnt
        else:
            top_dir, dir_share = "", 0.0
        # A blob is OVERSIZED (≫ its fair share, above the small-scan floor),
        # path-CONCENTRATED under one top-level dir, AND named like a package
        # container (the package-node fingerprint — distinguishes ``backend``
        # from a legitimately large domain feature like ``cert-manager``).
        oversized = share >= _BLOB_ABS_SHARE or share >= _BLOB_FAIR_SHARE_MULT * fair_share
        is_blob = (
            cnt >= _BLOB_MIN_FILES
            and dir_share >= _BLOB_CONCENTRATION
            and (
                share >= _BLOB_SEVERE_SHARE  # any name — too big to be one feature
                or (oversized and _is_container_name(name, top_dir))
            )
        )
        if is_blob:
            blobs.append(
                FeatureAudit(name, cnt, round(share, 4), top_dir, round(dir_share, 3), True)
            )
            blob_files |= paths

    pf_ids = {str(pf.get("id") or pf.get("name") or "") for pf in pfs}
    with_pf = sum(1 for f in dev if str(f.get("product_feature_id") or "") in pf_ids and pf_ids)

    median = float(sorted(s for _, s in ((nm, len(p)) for nm, p in per_feature))[n // 2]) if n else 0.0

    return ScanAudit(
        label=label,
        total_files=total,
        n_dev_features=n,
        n_product_features=len(pfs),
        max_feature_share=round(sizes[0] / total, 4) if total and sizes else 0.0,
        top3_share=round(len(top3_union) / total, 4) if total else 0.0,
        gini=round(_gini(sizes), 4),
        median_feature_files=median,
        largest_feature=max(per_feature, key=lambda x: len(x[1]))[0] if per_feature else "",
        blob_count=len(blobs),
        files_under_blobs_pct=round(len(blob_files) / total, 4) if total else 0.0,
        dev_features_with_pf_pct=round(with_pf / n, 4) if n else 0.0,
        blobs=sorted(blobs, key=lambda b: -b.files),
    )


# ── reporting ────────────────────────────────────────────────────────────────


def _fmt_table(audits: list[ScanAudit]) -> str:
    cols = [
        ("repo", lambda a: a.label[:24]),
        ("files", lambda a: str(a.total_files)),
        ("feats", lambda a: str(a.n_dev_features)),
        ("max%", lambda a: f"{a.max_feature_share:.0%}"),
        ("top3%", lambda a: f"{a.top3_share:.0%}"),
        ("gini", lambda a: f"{a.gini:.2f}"),
        ("blobs", lambda a: str(a.blob_count)),
        ("blob%", lambda a: f"{a.files_under_blobs_pct:.0%}"),
        ("largest", lambda a: a.largest_feature[:30]),
    ]
    widths = [max(len(h), *(len(fn(a)) for a in audits)) if audits else len(h) for h, fn in cols]
    head = "  ".join(h.ljust(w) for (h, _), w in zip(cols, widths))
    rows = [head, "  ".join("-" * w for w in widths)]
    for a in audits:
        rows.append("  ".join(fn(a).ljust(w) for (_, fn), w in zip(cols, widths)))
    return "\n".join(rows)


def _compare(curr: list[ScanAudit], baseline: dict[str, Any]) -> int:
    """Return non-zero exit code if any repo's concentration regressed."""
    base = {b["label"]: b for b in baseline.get("scans", [])}
    regressions: list[str] = []
    for a in curr:
        b = base.get(a.label)
        if not b:
            print(f"  (new) {a.label} — no baseline")
            continue
        checks = [
            ("max_feature_share", a.max_feature_share, b["max_feature_share"], _TOL_MAX_SHARE),
            ("top3_share", a.top3_share, b["top3_share"], _TOL_TOP3_SHARE),
            ("gini", a.gini, b["gini"], _TOL_GINI),
        ]
        for metric, now, was, tol in checks:
            if now - was > tol:
                regressions.append(f"  REGRESS {a.label}: {metric} {was:.3f} → {now:.3f} (+{now - was:.3f})")
        # Improvements are reported too (this is the point of the change).
        if b["max_feature_share"] - a.max_feature_share > _TOL_MAX_SHARE:
            print(f"  IMPROVE {a.label}: max_feature_share {b['max_feature_share']:.3f} → {a.max_feature_share:.3f}")
    if regressions:
        print("\n".join(regressions))
        return 1
    print("\nNo structural regressions past tolerance.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Golden-free structural audit of feature-map scans.")
    ap.add_argument("scans", nargs="+", help="scan JSON file(s)")
    ap.add_argument("--json", dest="out", help="write the audit as JSON (e.g. a baseline)")
    ap.add_argument("--compare", help="compare against a baseline JSON and fail on regression")
    args = ap.parse_args(argv)

    audits: list[ScanAudit] = []
    for p in args.scans:
        path = Path(p)
        try:
            scan = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"skip {p}: {e}", file=sys.stderr)
            continue
        audits.append(audit_scan(scan, label=path.stem))

    print(_fmt_table(audits))
    for a in audits:
        if a.blobs:
            print(f"\n{a.label} blob features:")
            for b in a.blobs:
                print(f"  {b.name[:40]:40s} {b.files:5d} files  {b.share:.0%}  under {b.top_dir}/ ({b.top_dir_share:.0%})")

    if args.out:
        Path(args.out).write_text(json.dumps({"scans": [asdict(a) for a in audits]}, indent=2))
        print(f"\nwrote {args.out}")

    if args.compare:
        baseline = json.loads(Path(args.compare).read_text())
        return _compare(audits, baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
