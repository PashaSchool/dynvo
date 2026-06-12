#!/usr/bin/env python
"""Score feature NAME quality of a scan against curated ground truth.

For each truth feature, the best-matching scan feature is found by
REUSING the file-membership matching from ``score_membership.py``
(greedy 1:1 Jaccard over file sets — imported, not duplicated). The
matched scan feature's NAME is then scored against the truth feature's
``display_name`` + ``aliases``:

1. **Deterministic recognizability** (default, $0): token-overlap F1
   between the scan name and each reference label (display_name and
   every alias); the best F1 wins. Tokenization mirrors the engine's
   naming validator: kebab/snake/space/camelCase split, lowercase,
   light singularization, stop-words and pure numbers dropped.

2. **LLM-judge mode** (``--judge``, requires ANTHROPIC_API_KEY): ONE
   batched Haiku call scoring every matched pair on
   (a) recognizability 0-2 — "would a PM recognize the feature?" and
   (b) ``unsupported_claim`` — does the name claim anything absent
   from the evidence keywords (tokens of the truth feature's files)?
   Without the explicit flag, no LLM client is ever constructed.

Unmatched truth features score 0 (an unfound feature has no name to
recognize). ``--json`` emits machine-readable output for regression
diffing.

Usage:
    python eval/membership/score_names.py \
        <feature-map.json> <ground-truth.yaml> \
        [--layer dev|product|both] [--repo /path/to/repo] \
        [--json] [--judge]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

# ── reuse the membership scorer (same dir, not a package) ──────────


def _load_membership_module() -> ModuleType:
    path = Path(__file__).resolve().parent / "score_membership.py"
    spec = importlib.util.spec_from_file_location("score_membership", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sm = _load_membership_module()

# ── tokenizer (same spirit as faultline/pipeline_v2/naming_validator) ──

# Grammatical glue + generic product fillers a name may carry without
# penalty. Universal English, not tuned to any repo.
STOP_WORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "of", "on", "or", "over", "per", "the", "their", "through", "to",
    "via", "with", "without", "your",
    "core", "support", "management", "manage", "system", "service",
    "services", "feature", "features", "module", "tool", "tools",
    "platform", "engine", "functionality", "misc", "other", "general",
    "common",
})

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ses") or word.endswith("xes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def tokenize(name: str) -> list[str]:
    """Content tokens: kebab/snake/space/camelCase split, lowercase,
    singularized, stop-words + pure numbers + 1-char tokens dropped.
    Order-preserving, deduped."""
    spaced = _CAMEL_RE.sub(" ", name)
    out: list[str] = []
    seen: set[str] = set()
    for raw in _SPLIT_RE.split(spaced):
        if not raw:
            continue
        t = _singular(raw.lower())
        if t in STOP_WORDS or t.isdigit() or len(t) < 2:
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


_MIN_STEM_LEN = 4


def _token_matches(token: str, others: set[str]) -> bool:
    """Equal, or shares a ≥4-char prefix-stem with another token —
    same rule as the engine's naming validator ("auth" matches
    "authentication", "embed" matches "embedding")."""
    if token in others:
        return True
    for other in others:
        shorter, longer = sorted((token, other), key=len)
        if len(shorter) >= _MIN_STEM_LEN and longer.startswith(shorter):
            return True
    return False


def token_f1(candidate: str, reference: str) -> float:
    """Set-based token-overlap F1 between two names (stem-tolerant)."""
    cand = set(tokenize(candidate))
    ref = set(tokenize(reference))
    if not cand or not ref:
        # degenerate names (all stop-words) — exact-string fallback
        return 1.0 if candidate.strip().lower() == reference.strip().lower() else 0.0
    p = sum(1 for t in cand if _token_matches(t, ref)) / len(cand)
    r = sum(1 for t in ref if _token_matches(t, cand)) / len(ref)
    if p + r == 0:
        return 0.0
    return 2 * p * r / (p + r)


def best_f1(scan_name: str, display_name: str, aliases: list[str]) -> tuple[float, str]:
    """Best token F1 of the scan name against display_name + aliases."""
    best, best_ref = 0.0, display_name
    for ref in [display_name, *aliases]:
        f1 = token_f1(scan_name, ref)
        if f1 > best:
            best, best_ref = f1, ref
    return best, best_ref


# ── ground truth with names ────────────────────────────────────────


def load_name_truth(path: Path) -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for f in doc["features"]:
        out[f["name"]] = {
            "files": list(f["files"]),
            "display_name": f["display_name"],
            "aliases": list(f.get("aliases") or []),
        }
    return out


def evidence_keywords(files: list[str], top_n: int = 15) -> list[str]:
    """Most common path-derived tokens of a truth feature's files —
    the legitimate vocabulary a name may draw from (judge mode)."""
    counts: Counter[str] = Counter()
    for f in files:
        counts.update(tokenize(f))
    generic = {"apps", "packages", "src", "app", "server", "lib", "web",
               "ts", "tsx", "js", "jsx", "py", "index", "type", "util",
               "page", "route", "router", "component"}
    return [t for t, _ in counts.most_common(top_n * 2) if t not in generic][:top_n]


# ── deterministic scoring ──────────────────────────────────────────


def score_names(
    truth: dict[str, dict[str, Any]],
    matched: dict[str, str | None],
) -> dict[str, Any]:
    """``matched`` maps truth name -> matched scan feature name (or None)."""
    rows: list[dict[str, Any]] = []
    f1s: list[float] = []
    for tname in sorted(truth):
        entry = truth[tname]
        sname = matched.get(tname)
        if sname is None:
            f1, ref = 0.0, entry["display_name"]
        else:
            f1, ref = best_f1(sname, entry["display_name"], entry["aliases"])
        f1s.append(f1)
        rows.append(
            {
                "truth_feature": tname,
                "display_name": entry["display_name"],
                "matched_scan_feature": sname,
                "best_reference": ref if sname is not None else None,
                "token_f1": round(f1, 4),
            }
        )
    n = len(rows)
    return {
        "n_truth_features": n,
        "n_matched": sum(1 for r in rows if r["matched_scan_feature"]),
        "mean_token_f1": round(sum(f1s) / n, 4) if n else 0.0,
        "per_feature": rows,
    }


# ── LLM judge (only with --judge + ANTHROPIC_API_KEY) ──────────────

JUDGE_MODEL = "claude-haiku-4-5"
JUDGE_MAX_TOKENS = 1500


def build_judge_prompt(pairs: list[dict[str, Any]]) -> str:
    lines = [
        "You are scoring how well machine-generated feature names match",
        "a product's real features. For each numbered pair, output:",
        '  "recognizable": 0 (a PM would not connect the scan name to the',
        "  feature), 1 (recognizable with effort), 2 (immediately recognizable);",
        '  "unsupported_claim": true if the scan name asserts a technology,',
        "  vendor or concept absent from BOTH the reference labels and the",
        "  evidence keywords, else false.",
        "Respond with ONLY a JSON array like:",
        '[{"i": 1, "recognizable": 2, "unsupported_claim": false}, ...]',
        "",
    ]
    for i, p in enumerate(pairs, 1):
        refs = ", ".join([p["display_name"], *p["aliases"]])
        lines.append(
            f"{i}. scan name: {p['scan_name']!r} | reference labels: {refs}"
            f" | evidence keywords: {', '.join(p['keywords'])}"
        )
    return "\n".join(lines)


def run_judge(
    truth: dict[str, dict[str, Any]],
    matched: dict[str, str | None],
) -> dict[str, Any]:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "--judge requires ANTHROPIC_API_KEY; refusing to run without it "
            "(default deterministic mode is $0 — drop the flag)."
        )
    import anthropic  # deferred: never imported in deterministic mode

    pairs = [
        {
            "truth_feature": tname,
            "scan_name": matched[tname],
            "display_name": truth[tname]["display_name"],
            "aliases": truth[tname]["aliases"],
            "keywords": evidence_keywords(truth[tname]["files"]),
        }
        for tname in sorted(truth)
        if matched.get(tname) is not None
    ]
    if not pairs:
        return {"judge_model": JUDGE_MODEL, "per_feature": []}

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=JUDGE_MAX_TOKENS,
        messages=[{"role": "user", "content": build_judge_prompt(pairs)}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise SystemExit(f"judge returned unparseable output: {text[:200]}")
    verdicts = {v["i"]: v for v in json.loads(m.group(0))}

    rows = []
    for i, p in enumerate(pairs, 1):
        v = verdicts.get(i, {})
        rows.append(
            {
                "truth_feature": p["truth_feature"],
                "scan_name": p["scan_name"],
                "recognizable": v.get("recognizable"),
                "unsupported_claim": v.get("unsupported_claim"),
            }
        )
    scored = [r["recognizable"] for r in rows if r["recognizable"] is not None]
    return {
        "judge_model": JUDGE_MODEL,
        "mean_recognizable": round(sum(scored) / len(scored), 4) if scored else None,
        "n_unsupported_claims": sum(1 for r in rows if r["unsupported_claim"]),
        "per_feature": rows,
    }


# ── glue ────────────────────────────────────────────────────────────


def match_features(
    scan: dict[str, Any],
    truth_files: dict[str, list[str]],
    layer: str,
    repo: Path | None,
) -> dict[str, str | None]:
    """Reuse score_membership's loaders + greedy Jaccard matcher."""
    tracked = sm._git_ls_files(repo) if repo else None
    truth_universe = {f for fs in truth_files.values() for f in fs}
    repo_prefixes: tuple[str, ...] = ()
    if repo:
        repo_prefixes = (str(repo.resolve()) + "/", repo.name + "/")
    raw = sm.load_scan_features(scan, layer)
    feats = [
        (name, sm.expand_feature_paths(paths, tracked, truth_universe, repo_prefixes))
        for name, paths in raw
    ]
    result = sm.score(truth_files, feats)
    return {
        row["truth_feature"]: row["matched_scan_feature"]
        for row in result["per_feature"]
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("feature_map", type=Path)
    ap.add_argument("ground_truth", type=Path)
    ap.add_argument("--layer", choices=["dev", "product", "both"], default="dev")
    ap.add_argument("--repo", type=Path, default=None)
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--judge", action="store_true")
    args = ap.parse_args(argv)

    truth = load_name_truth(args.ground_truth)
    scan = json.loads(args.feature_map.read_text(encoding="utf-8"))
    truth_files = {n: e["files"] for n, e in truth.items()}

    matched = match_features(scan, truth_files, args.layer, args.repo)
    result = score_names(truth, matched)
    if args.judge:
        result["judge"] = run_judge(truth, matched)

    if args.as_json:
        print(json.dumps(result, indent=2))
        return 0

    print(
        f"truth features: {result['n_truth_features']}  "
        f"matched: {result['n_matched']}  "
        f"mean token-F1: {result['mean_token_f1']:.3f}"
    )
    print()
    print(f"{'truth feature':<24} {'display name':<22} {'F1':>5}  matched scan feature")
    for row in result["per_feature"]:
        print(
            f"{row['truth_feature']:<24} {row['display_name']:<22} "
            f"{row['token_f1']:>5.3f}  {row['matched_scan_feature'] or '—'}"
        )
    if args.judge:
        j = result["judge"]
        print(
            f"\njudge ({j['judge_model']}): mean recognizable "
            f"{j.get('mean_recognizable')} / 2, "
            f"unsupported claims: {j.get('n_unsupported_claims')}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
