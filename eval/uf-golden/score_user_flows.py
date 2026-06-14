#!/usr/bin/env python
"""Score engine ``user_flows[]`` against a curated UF golden.

Measures how completely the golden's user journeys are recovered, with a
dedicated focus on the SYSTEM (background-job) split — the Stage 6.8b deliverable.
Pure measurement, no pass/fail thresholds baked in.

Usage:
    python eval/uf-golden/score_user_flows.py <feature-map.json> \
        eval/uf-golden/<slug>/user-flows.yaml [--json]

Matching: greedy 1:1 by name-token Jaccard (verb-synonym folding + light
singularization + stop-word drop), tie-broken by a small resource-overlap bonus.
A golden UF with zero name-token overlap against every engine UF is UNMATCHED.
This mirrors score_membership.py's greedy-Jaccard design (no match threshold —
best pairs win first).

Reported:
- UF recall (golden journeys matched) / precision (engine UFs that matched)
- SYSTEM-flow recall: golden system journeys matched to an engine *system* UF
- category confusion on matched pairs (interactive vs system)
- raw engine system-UF count vs golden system count
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

_STOP = {"a", "an", "the", "and", "or", "of", "to", "for", "with", "your",
         "my", "in", "on", "all", "due"}
# Fold near-synonym verbs so "Run digests" ~ "Send scheduled digests".
_VERB_SYN = {
    "create": "author", "add": "author", "new": "author", "compose": "author",
    "draft": "author", "edit": "manage", "update": "manage", "manage": "manage",
    "configure": "manage", "set": "manage", "enforce": "manage",
    "view": "browse", "browse": "browse", "list": "browse", "see": "browse",
    "read": "browse", "run": "execute", "execute": "execute", "process": "execute",
    "send": "execute", "generate": "execute", "renew": "execute", "handle": "execute",
    "delete": "delete", "remove": "delete", "clean": "execute", "cleanup": "execute",
}


def _toks(s: str) -> set[str]:
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9]+", (s or "").lower()):
        if w in _STOP:
            continue
        w = _VERB_SYN.get(w, w)
        if len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


def _golden_ufs(doc: dict) -> list[dict[str, Any]]:
    ufs: list[dict[str, Any]] = []
    for d in doc.get("domains") or []:
        for uf in d.get("user_flows") or []:
            ufs.append({
                "name": uf.get("name", ""),
                "resource": uf.get("resource", ""),
                "category": (uf.get("category") or "interactive"),
            })
    return ufs


def _engine_ufs(scan: dict) -> list[dict[str, Any]]:
    return [
        {
            "name": uf.get("name", ""),
            "resource": uf.get("resource", ""),
            "category": (uf.get("category") or "interactive"),
            "trigger": uf.get("trigger"),
        }
        for uf in (scan.get("user_flows") or [])
    ]


def _sim(g: dict, e: dict) -> float:
    gt, et = _toks(g["name"]), _toks(e["name"])
    if not gt or not et:
        return 0.0
    jac = len(gt & et) / len(gt | et)
    if g["resource"] and e["resource"] and (_toks(g["resource"]) & _toks(e["resource"])):
        jac += 0.15
    return jac


def score(golden: list[dict], engine: list[dict]) -> dict[str, Any]:
    pairs = []
    for gi, g in enumerate(golden):
        for ei, e in enumerate(engine):
            s = _sim(g, e)
            if s > 0:
                pairs.append((s, gi, ei))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))

    matched: dict[int, int] = {}
    used_e: set[int] = set()
    for _s, gi, ei in pairs:
        if gi in matched or ei in used_e:
            continue
        matched[gi] = ei
        used_e.add(ei)

    gold_sys = [gi for gi, g in enumerate(golden) if g["category"] == "system"]
    sys_matched = [gi for gi in gold_sys if gi in matched]
    sys_correct = [gi for gi in sys_matched if engine[matched[gi]]["category"] == "system"]
    cat_agree = sum(
        1 for gi, ei in matched.items()
        if golden[gi]["category"] == engine[ei]["category"]
    )
    n_eng_sys = sum(1 for e in engine if e["category"] == "system")

    return {
        "golden_ufs": len(golden),
        "engine_ufs": len(engine),
        "matched": len(matched),
        "uf_recall": round(len(matched) / len(golden), 4) if golden else 0.0,
        "uf_precision": round(len(matched) / len(engine), 4) if engine else 0.0,
        "golden_system": len(gold_sys),
        "engine_system": n_eng_sys,
        "system_recall": round(len(sys_matched) / len(gold_sys), 4) if gold_sys else 0.0,
        "system_recall_correct_category": round(len(sys_correct) / len(gold_sys), 4) if gold_sys else 0.0,
        "category_agreement_on_matched": round(cat_agree / len(matched), 4) if matched else 0.0,
        "unmatched_golden": [golden[gi]["name"] for gi in range(len(golden)) if gi not in matched],
        "matched_pairs": [
            {"golden": golden[gi]["name"], "engine": engine[ei]["name"],
             "golden_cat": golden[gi]["category"], "engine_cat": engine[ei]["category"]}
            for gi, ei in sorted(matched.items())
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("feature_map", type=Path)
    ap.add_argument("golden", type=Path)
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    scan = json.loads(args.feature_map.read_text(encoding="utf-8"))
    doc = yaml.safe_load(args.golden.read_text(encoding="utf-8"))
    result = score(_golden_ufs(doc), _engine_ufs(scan))

    if args.as_json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"golden UFs: {result['golden_ufs']}  engine UFs: {result['engine_ufs']}  "
          f"matched: {result['matched']}")
    print(f"UF recall   {result['uf_recall']:.3f}   precision {result['uf_precision']:.3f}")
    print(f"system: golden={result['golden_system']} engine={result['engine_system']}  "
          f"system_recall={result['system_recall']:.3f}  "
          f"(correct category {result['system_recall_correct_category']:.3f})")
    print(f"category agreement on matched: {result['category_agreement_on_matched']:.3f}")
    if result["unmatched_golden"]:
        print(f"\nunmatched golden ({len(result['unmatched_golden'])}): "
              + ", ".join(result["unmatched_golden"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
