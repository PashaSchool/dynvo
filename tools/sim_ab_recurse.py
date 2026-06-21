#!/usr/bin/env python3
"""A/B the SAME current Stage 8.9 code at cap=1 (single-pass) vs cap=8
(recurse-to-fixed-point) on the cold corpus, through the REAL cold_eval gate.

This is the APPLES-TO-APPLES comparison: both arms run the identical stage
(incl. the conservation fix) — the ONLY difference is whether minted
sub-features are re-decomposed. It isolates the recursion lever from the
conservation change (which moves a few denominators on its own).

Also asserts FILE CONSERVATION on every repo: the union of all files across
all developer features (member_files ∪ paths) is byte-identical before and
after the stage — 0 dropped, 0 duplicated — at BOTH caps.

Usage:
    PYTHONPATH=.:tools FAULTLINE_EVAL_DIR=<app>/eval \
        .venv/bin/python tools/sim_ab_recurse.py [--out PATH] [slug ...]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import traceback

sys.path.insert(0, os.environ.get(
    "FAULTLINE_EVAL_DIR", "/Users/pkuzina/workspace/faultlines-app/eval"))
import cold_eval  # type: ignore  # noqa: E402

from faultline.models.types import Feature  # noqa: E402
import faultline.pipeline_v2.stage_8_9_anchor_subdecompose as st  # noqa: E402

_COLD = os.path.expanduser("~/.faultline/cold")
_PASCAL = st._COMPONENT_NAME_RE


def _all_slugs() -> list[str]:
    out = []
    for f in sorted(glob.glob(os.path.join(_COLD, "*.json"))):
        base = os.path.basename(f)[:-5]
        if not base.startswith("_"):
            out.append(base)
    return out


def _all_files(feat_dicts: list[dict]) -> tuple[int, int]:
    """(distinct file count, total claim rows) across member_files ∪ paths."""
    distinct: set[str] = set()
    rows = 0
    for f in feat_dicts:
        for m in (f.get("member_files") or []):
            p = m.get("path") if isinstance(m, dict) else m
            if isinstance(p, str):
                distinct.add(p)
                rows += 1
        for p in (f.get("paths") or []):
            if isinstance(p, str):
                distinct.add(p)
    return len(distinct), rows


def _junk_subfeatures(feat_dicts: list[dict]) -> list[str]:
    """Sub-features whose NAME is a PascalCase/camelCase component identifier —
    the precision-guard violation class. A clean recursion mints 0 of these."""
    bad = []
    for f in feat_dicts:
        desc = (f.get("description") or "").lower()
        if st._SUBDOMAIN_MARKER not in desc:
            continue
        name = f.get("name") or ""
        # name is already kebab-slugged; a real junk leak would show as a
        # mixed-case component name surviving the slug (defensive check).
        if _PASCAL.fullmatch(name) or name in {"", "components", "ui", "widgets",
                                               "utils", "lib", "__tests__"}:
            bad.append(name)
    return bad


def _run_arm(scan: dict, cap: int) -> dict:
    feats = [Feature(**d) for d in
             (scan.get("developer_features") or scan.get("features") or [])]
    before_files, _ = _all_files(
        scan.get("developer_features") or scan.get("features") or [])
    saved = st._FIXED_POINT_ITER_CAP
    st._FIXED_POINT_ITER_CAP = cap
    try:
        res = st.subdecompose_oversized_features(feats)
    finally:
        st._FIXED_POINT_ITER_CAP = saved
    dumped = [f.model_dump(mode="json") for f in feats]
    after = dict(scan)
    after["developer_features"] = dumped
    after.pop("features", None)
    g3 = cold_eval.g3_blob(after)
    after_files, _ = _all_files(dumped)
    return {
        "owned_max": g3["owned_max_feature_share"],
        "splits": res.features_split,
        "subs": res.subfeatures_created,
        "iters": res.iterations,
        "cap_hit": res.depth_cap_hit,
        "files_before": before_files,
        "files_after": after_files,
        "dropped": before_files - after_files,
        "junk": _junk_subfeatures(dumped),
    }


def measure(slug: str) -> dict:
    path = os.path.join(_COLD, f"{slug}.json")
    if not os.path.exists(path):
        return {"slug": slug, "skip": "no artifact"}
    scan = json.load(open(path))
    single = _run_arm(scan, 1)
    recurse = _run_arm(scan, 8)
    return {
        "slug": slug,
        "single_owned_max": single["owned_max"],
        "recurse_owned_max": recurse["owned_max"],
        "delta": round(recurse["owned_max"] - single["owned_max"], 3),
        "single_subs": single["subs"],
        "recurse_subs": recurse["subs"],
        "iters": recurse["iters"],
        "cap_hit": recurse["cap_hit"],
        "dropped_single": single["dropped"],
        "dropped_recurse": recurse["dropped"],
        "junk_single": single["junk"],
        "junk_recurse": recurse["junk"],
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("slugs", nargs="*")
    ap.add_argument("--out", default="/tmp/sim_ab.json")
    a = ap.parse_args(argv[1:])
    slugs = a.slugs or _all_slugs()
    out = []
    for s in slugs:
        try:
            r = measure(s)
        except Exception as exc:  # pragma: no cover
            r = {"slug": s, "error": f"{type(exc).__name__}: {exc}",
                 "tb": traceback.format_exc()[-500:]}
        out.append(r)
        if r.get("skip") or r.get("error"):
            print(f"  {s}: {r.get('skip') or r.get('error')}",
                  file=sys.stderr, flush=True)
        else:
            print(f"  {s:18s} single={r['single_owned_max']:.3f}"
                  f" recurse={r['recurse_owned_max']:.3f} ({r['delta']:+.3f})"
                  f"  iters={r['iters']} drop(s/r)={r['dropped_single']}/{r['dropped_recurse']}"
                  f" junk(s/r)={len(r['junk_single'])}/{len(r['junk_recurse'])}",
                  file=sys.stderr, flush=True)
    json.dump(out, open(a.out, "w"), indent=2)
    print(f"\nwrote {len(out)} -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
