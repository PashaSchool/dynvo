#!/usr/bin/env python3
"""Deterministic simulation of Stage 8.9 oversized-feature sub-decomposition.

Replays the REAL stage logic (imported from
``faultline.pipeline_v2.stage_8_9_anchor_subdecompose`` — NOT a divergent
copy) over cached cold-scan artifacts in ``~/.faultline/cold/<slug>.json`` and
reports the ``cold_eval.owned_max_feature_share`` blob signal BEFORE vs AFTER
the split, the number of sub-features minted, and the per-feature domain
breakdown.

This is the local, $0, no-LLM, no-network harness used to validate the
blob-decomposer WITHOUT a full scan (operator forbids real-key scans). It is a
SIMULATION of the metric only: it does not mutate the artifacts and does not
run the rest of the pipeline. It mirrors ``eval/cold_eval``'s owned-file model
by reusing the stage's own ``_owned_paths`` so the gate and the metric agree.

Usage
-----
    PYTHONPATH=. .venv/bin/python tools/sim_stage_8_9_subdecompose.py \
        [slug ...]            # default: caddy inbox-zero formbricks

Because it imports the production functions, any change to the stage's
vocabulary / recursion / gate is reflected here automatically — keeping the
sim honest (no drift between the sim and the shipped code).
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
from collections import defaultdict

from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    _OVERSIZED_MEDIAN_MULT,
    _OVERSIZED_SHARE,
    _common_segments,
    _domain_key,
    _plan_split,
    _strip_route_group,
)

_DEFAULT_SLUGS = ("caddy", "inbox-zero", "formbricks")
_COLD = os.path.expanduser("~/.faultline/cold")


def _owned(feature: dict) -> list[str]:
    """Owned files of a feature dict — mirrors the stage's ``_owned_paths``
    on the raw JSON shape (``member_files`` dicts first, else ``paths``)."""
    mf = feature.get("member_files")
    if mf and isinstance(mf[0], dict):
        owned = [
            m["path"]
            for m in mf
            if m.get("primary") or m.get("role") in ("anchor", "owner")
        ]
        if owned:
            return owned
    return feature.get("paths") or []


def _leaf(domain_key: str) -> str:
    return _strip_route_group(domain_key.rsplit("/", 1)[-1]).replace("_", "-").lower()


def simulate(slug: str) -> None:
    path = os.path.join(_COLD, f"{slug}.json")
    if not os.path.exists(path):
        print(f"\n===== {slug}: SKIP (no cold artifact at {path})")
        return
    with open(path) as fh:
        data = json.load(fh)
    feats = data.get("developer_features") or []

    sizes = [len(_owned(f)) for f in feats if _owned(f)]
    if not sizes:
        print(f"\n===== {slug}: SKIP (no owned-file features)")
        return
    median = max(2, int(statistics.median(sizes)))
    osets = [(set(_owned(f)), f.get("name")) for f in feats if _owned(f)]
    total_owned = len(set().union(*[s for s, _ in osets]))
    cut = max(_OVERSIZED_MEDIAN_MULT * median, math.ceil(_OVERSIZED_SHARE * total_owned))
    floor = median

    before_max = max(len(s) for s, _ in osets) / (total_owned or 1)
    before_big = max(osets, key=lambda x: len(x[0]))[1]

    new_owned: list[tuple[set[str], str]] = []
    subs_total = 0
    report: list[tuple[str, int, int, list[str]]] = []
    for f in feats:
        owned = _owned(f)
        if len(owned) <= cut:
            if owned:
                new_owned.append((set(owned), f.get("name")))
            continue
        domains, residual = _plan_split(owned, floor)
        if not domains:
            if owned:
                new_owned.append((set(owned), f.get("name")))
            continue
        # The source keeps ONLY its (de-owned, role=shared) residual; the
        # sub-features own their domain files. Sub-floor / non-domain files
        # fall to the residual and stop counting toward owned_max.
        if residual:
            new_owned.append((set(residual), f"{f.get('name')}~residual"))
        subs_total += len(domains)
        report.append(
            (f.get("name"), len(owned), len(residual), sorted({_leaf(k) for k in domains}))
        )
        for key, files in domains.items():
            new_owned.append((set(files), _leaf(key)))

    new_total = len(set().union(*[s for s, _ in new_owned])) or 1
    after_max = max(len(s) for s, _ in new_owned) / new_total
    after_big = max(new_owned, key=lambda x: len(x[0]))[1]

    print(f"\n===== {slug}: median={median} total_owned={total_owned} cut={cut} floor={floor}")
    print(f"  BEFORE owned_max={before_max:.3f} ({before_big})")
    print(f"  AFTER  owned_max={after_max:.3f} ({after_big})   subs_created={subs_total}")
    for name, n_owned, n_res, leaves in report:
        print(f"    [{name} owned={n_owned}] -> {len(leaves)} subs, residual(->shared)={n_res}")
        print(f"       {leaves}")


def main(argv: list[str]) -> int:
    slugs = argv[1:] or list(_DEFAULT_SLUGS)
    for slug in slugs:
        simulate(slug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
