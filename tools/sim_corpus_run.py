#!/usr/bin/env python3
"""Run the faithful Stage 8.9 sim (``sim_stage_8_9_subdecompose.simulate``)
across the WHOLE ``~/.faultline/cold`` corpus, in-process, and emit a compact
machine-readable summary. No argv-splitting pitfalls; one slug at a time so a
single huge artifact (twenty ~99MB) cannot truncate the whole run.

This is a measurement harness only — it imports the REAL stage + REAL
cold_eval via the sim module, never re-implements the split. Used to capture
the single-pass baseline and the post-recursion result identically.

Usage:
    PYTHONPATH=. FAULTLINE_EVAL_DIR=<app>/eval \
        .venv/bin/python tools/sim_corpus_run.py [--out PATH] [slug ...]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import traceback

from sim_stage_8_9_subdecompose import simulate  # type: ignore

_COLD = os.path.expanduser("~/.faultline/cold")


def _all_slugs() -> list[str]:
    out = []
    for f in sorted(glob.glob(os.path.join(_COLD, "*.json"))):
        base = os.path.basename(f)[:-5]
        if base.startswith("_"):
            continue
        out.append(base)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slugs", nargs="*", help="default: whole cold corpus")
    ap.add_argument("--out", default="/tmp/sim_corpus.json")
    a = ap.parse_args(argv[1:])

    slugs = a.slugs or _all_slugs()
    results = []
    for slug in slugs:
        try:
            r = simulate(slug)
        except Exception as exc:  # pragma: no cover - measurement guard
            r = {"slug": slug, "error": f"{type(exc).__name__}: {exc}",
                 "tb": traceback.format_exc()[-600:]}
        if r is not None:
            results.append(r)
        # progress to stderr so a long run is observable
        if r and not r.get("skip") and not r.get("error"):
            print(
                f"  {slug}: owned_max {r['owned_max_before']:.3f}"
                f" -> {r['owned_max_after']:.3f}"
                f"  (split={r['stage']['features_split']}"
                f" subs={r['stage']['subfeatures_created']})",
                file=sys.stderr, flush=True,
            )
        else:
            tag = (r or {}).get("skip") or (r or {}).get("error") or "?"
            print(f"  {slug}: {tag}", file=sys.stderr, flush=True)

    with open(a.out, "w") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nwrote {len(results)} results -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
