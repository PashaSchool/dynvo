"""One-off: replay ``post_process.run()`` against the cached A/B scan
artefacts and report before/after anti-pattern counts.

This script lets us validate the deterministic naming-quality fixes
(empty-name drop, multi-slash uncategorized, demo/example drop,
final-emit slugifier) without paying for full rescans — the bugs all
manifest in post-processing, so replaying that stage in isolation is
sufficient and faithful.

Usage::

    .venv/bin/python eval/_replay_post_process.py

Outputs a JSON summary to stdout plus a pretty per-file table.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Iterable

from faultline.analyzer import post_process
from faultline.models.types import FeatureMap

# Cached scan JSONs from the A/B run (2026-05-18).
ARTEFACTS = [
    Path.home() / ".faultline" / "feature-map-formbricks-ab-V1-20260518-111648.json",
    Path.home() / ".faultline" / "feature-map-formbricks-ab-V2-20260518-111958.json",
    Path.home() / ".faultline" / "feature-map-trigger.dev-ab-V1-20260518-111701.json",
    Path.home() / ".faultline" / "feature-map-trigger.dev-ab-V2-20260518-112013.json",
]

_TITLECASE_RE = re.compile(r"[A-Z]| ")
_DEMO_PREFIXES = (
    "references-", "references/",
    "examples-", "examples/", "example-",
    "demo-", "demos-", "demos/",
    "samples-", "samples/",
)


def _classify(names: Iterable[str]) -> dict[str, int]:
    """Count occurrences of each anti-pattern in a feature-name list."""
    counts = {
        "empty": 0,
        "folder_uncategorized": 0,
        "dot_in_name": 0,
        "titlecase_in_name": 0,
        "demo_package": 0,
        "total": 0,
    }
    for name in names:
        counts["total"] += 1
        if not name or not name.strip():
            counts["empty"] += 1
            continue
        if name == "uncategorized" or name.endswith("/uncategorized"):
            counts["folder_uncategorized"] += 1
        if "." in name:
            counts["dot_in_name"] += 1
        if _TITLECASE_RE.search(name):
            counts["titlecase_in_name"] += 1
        if any(
            name == p.rstrip("-/") or name.startswith(p)
            for p in _DEMO_PREFIXES
        ):
            counts["demo_package"] += 1
    return counts


def _run_one(path: Path) -> dict:
    raw = json.loads(path.read_text())
    fm_before = FeatureMap.model_validate(raw)
    before_names = [f.name for f in fm_before.features]
    before = _classify(before_names)

    fm_after = post_process.run(fm_before)
    after_names = [f.name for f in fm_after.features]
    after = _classify(after_names)

    return {
        "file": path.name,
        "before": before,
        "after": after,
        # Names that still match an anti-pattern after the pass —
        # useful to inspect when a count is non-zero.
        "residual_examples": {
            "empty": [n for n in after_names if not (n and n.strip())][:3],
            "folder_uncategorized": [
                n for n in after_names
                if n == "uncategorized" or n.endswith("/uncategorized")
            ][:3],
            "titlecase_in_name": [
                n for n in after_names if _TITLECASE_RE.search(n or "")
            ][:3],
            "demo_package": [
                n for n in after_names
                if any(
                    n == p.rstrip("-/") or n.startswith(p)
                    for p in _DEMO_PREFIXES
                )
            ][:3],
        },
    }


def main() -> int:
    results = []
    for p in ARTEFACTS:
        if not p.exists():
            print(f"SKIP missing artefact: {p}", file=sys.stderr)
            continue
        results.append(_run_one(p))

    print(json.dumps({"replay": results}, indent=2, default=str))

    # Pretty summary table on stderr so stdout stays machine-readable.
    print("\n=== Anti-pattern delta ===", file=sys.stderr)
    header = f"{'file':<55}  {'feat':>10}  {'empty':>7}  {'unc':>5}  {'dot':>5}  {'TC':>5}  {'demo':>5}"
    print(header, file=sys.stderr)
    for r in results:
        b, a = r["before"], r["after"]
        print(
            f"{r['file']:<55}  {b['total']:>4}→{a['total']:<5}  "
            f"{b['empty']}→{a['empty']:<3}  {b['folder_uncategorized']}→{a['folder_uncategorized']:<3}  "
            f"{b['dot_in_name']}→{a['dot_in_name']:<3}  {b['titlecase_in_name']}→{a['titlecase_in_name']:<3}  "
            f"{b['demo_package']}→{a['demo_package']:<3}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
