#!/usr/bin/env python3
"""Faithful, $0, no-LLM simulation of Stage 8.9 oversized-feature decomposition.

What "faithful" means (and why the previous version was NOT)
===========================================================

This harness must report the SAME ``owned_max_feature_share`` the real
blob gate (``eval/cold_eval.py:g3_blob``) would report — before and after
the stage runs. The previous implementation did not: it

  1. hand-reconstructed each feature's owned-set with a buggy
     ``if owned: return owned else paths`` fallback that counted
     **de-owned (role=shared) files as owned**, and
  2. NEVER ran the real stage nor ``cold_eval`` — it re-implemented the
     split with ``_plan_split`` and divided set sizes by hand.

So it diverged from the gate (it reported inbox-zero AFTER 0.388 while
the real stage + the fixed ``cold_eval`` report 0.063). An independent
audit (memory/finding-coldeval-blob-broken-2026-06-19 → "Blob audit
RESET 2026-06-20") flagged it as untrustworthy.

How this version stays honest
-----------------------------

It performs the EXACT production data path, end to end:

  (a) load a cold scan ``~/.faultline/cold/<slug>.json``;
  (b) measure BEFORE = ``cold_eval.g3_blob(scan)`` on the raw scan dict
      — literally what the gate sees pre-stage;
  (c) hydrate ``developer_features`` dicts into real ``Feature`` objects;
  (d) run the REAL stage ``subdecompose_oversized_features(features)`` —
      mutating ``member_files`` roles + appending sub-features exactly as
      production does (no divergent copy of the logic lives here);
  (e) serialise the mutated features back to dicts
      (``Feature.model_dump(mode="json")``) into a shallow copy of the scan;
  (f) measure AFTER = ``cold_eval.g3_blob(after_scan)``.

Because BEFORE/AFTER both come from the real ``cold_eval.g3_blob``, the
numbers this prints ARE the numbers the gate would report. Because it
imports the production stage, any change to the stage's vocabulary /
recursion / gate is reflected automatically — no sim drift.

It does NOT run the rest of the pipeline and does NOT mutate the artifact
on disk (it deep-hydrates and serialises a copy). It needs no API key and
no network (operator forbids real-key scans).

Usage
-----
    PYTHONPATH=. .venv/bin/python tools/sim_stage_8_9_subdecompose.py \
        [slug ...]            # default: caddy inbox-zero formbricks meilisearch

    # JSON for machine consumption / reconciliation note:
    PYTHONPATH=. .venv/bin/python tools/sim_stage_8_9_subdecompose.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# The REAL stage — imported, never copied. Mutates Feature objects in place
# (member_files roles flipped to shared, sub-features appended) exactly as the
# pipeline does.
from faultline.models.types import Feature
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    subdecompose_oversized_features,
)

# The REAL blob gate. We add the faultlines-app eval dir to sys.path so we
# measure with the SAME g3_blob the cold-corpus evaluator uses — not a
# hand-rolled owned-set reconstruction. (cold_eval lives in the app repo, not
# the engine repo, so it is imported by path, not as an engine module.)
_EVAL_DIR = os.environ.get(
    "FAULTLINE_EVAL_DIR", "/Users/pkuzina/workspace/faultlines-app/eval"
)
if _EVAL_DIR not in sys.path:
    sys.path.insert(0, _EVAL_DIR)
try:
    import cold_eval  # type: ignore
except Exception as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        f"FATAL: cannot import the real cold_eval from {_EVAL_DIR!r} ({exc}). "
        "Set FAULTLINE_EVAL_DIR to the faultlines-app/eval directory."
    )

_DEFAULT_SLUGS = ("caddy", "inbox-zero", "formbricks", "meilisearch")
_COLD = os.path.expanduser("~/.faultline/cold")


def _hydrate(feat_dicts: list[dict[str, Any]]) -> tuple[list[Feature], list[dict]]:
    """Hydrate ``developer_features`` dicts into ``Feature`` objects.

    Returns ``(features, skipped)`` — any dict that fails validation is left
    untouched (carried through verbatim) so the AFTER scan still contains it
    and the metric stays comparable. In practice every cold-scan dev feature
    hydrates cleanly (verified on the corpus); the guard exists so a single
    odd feature can never silently vanish from the measurement.
    """
    features: list[Feature] = []
    skipped: list[dict] = []
    for d in feat_dicts:
        try:
            features.append(Feature(**d))
        except Exception:
            skipped.append(d)
    return features, skipped


def _owned_max(scan: dict[str, Any]) -> tuple[float, str | None, dict[str, Any]]:
    """``(owned_max_feature_share, owned_biggest, full_g3)`` from the REAL gate."""
    g3 = cold_eval.g3_blob(scan)
    return g3.get("owned_max_feature_share", 0.0), g3.get("owned_biggest"), g3


def simulate(slug: str) -> dict[str, Any] | None:
    path = os.path.join(_COLD, f"{slug}.json")
    if not os.path.exists(path):
        return {"slug": slug, "skip": f"no cold artifact at {path}"}
    with open(path) as fh:
        scan = json.load(fh)

    mtime = os.path.getmtime(path)
    feat_dicts = scan.get("developer_features") or scan.get("features") or []

    # (b) BEFORE — the real gate on the untouched scan.
    before_max, before_big, before_g3 = _owned_max(scan)

    # (c) hydrate → (d) run the REAL stage in place.
    features, skipped = _hydrate(feat_dicts)
    result = subdecompose_oversized_features(features)

    # (e) serialise the mutated features back into a COPY of the scan.
    after_scan = dict(scan)
    after_scan["developer_features"] = [
        f.model_dump(mode="json") for f in features
    ] + skipped
    after_scan.pop("features", None)  # canonical key only, avoid double-count

    # (f) AFTER — the real gate on the post-stage scan.
    after_max, after_big, after_g3 = _owned_max(after_scan)

    return {
        "slug": slug,
        "artifact_mtime": __import__("datetime").datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
        "n_dev_features_before": len(feat_dicts),
        "n_dev_features_after": len(after_scan["developer_features"]),
        "hydrate_skipped": len(skipped),
        "owned_max_before": before_max,
        "owned_biggest_before": before_big,
        "owned_max_after": after_max,
        "owned_biggest_after": after_big,
        "owned_max_delta": round(after_max - before_max, 3),
        "all_max_before": before_g3.get("max_feature_share"),
        "all_max_after": after_g3.get("max_feature_share"),
        "stage": result.as_telemetry(),
    }


def _print_human(r: dict[str, Any]) -> None:
    slug = r["slug"]
    if r.get("skip"):
        print(f"\n===== {slug}: SKIP ({r['skip']})")
        return
    st = r["stage"]
    print(f"\n===== {slug}  (artifact {r['artifact_mtime']})")
    print(
        f"  owned_max  BEFORE={r['owned_max_before']:.3f} ({r['owned_biggest_before']})"
        f"  ->  AFTER={r['owned_max_after']:.3f} ({r['owned_biggest_after']})"
        f"   [delta {r['owned_max_delta']:+.3f}]"
    )
    print(
        f"  (all-files max_share {r['all_max_before']} -> {r['all_max_after']};"
        f" dev_features {r['n_dev_features_before']} -> {r['n_dev_features_after']})"
    )
    print(
        f"  stage: oversized={st['oversized_total']} split={st['features_split']}"
        f" subs={st['subfeatures_created']} paths_moved={st['paths_moved']}"
        f" members_deowned={st['members_deowned']}"
    )
    for s in st.get("split_sample", [])[:8]:
        print(
            f"    [{s['feature']}] -> {s['domains']} domains,"
            f" moved={s['moved']} residual={s['residual']}: {s['names'][:12]}"
        )


_NOTE = """\
RECONCILIATION NOTE
===================
The before/after numbers above are produced by the REAL eval/cold_eval.py
g3_blob (the cold-corpus gate), measured before vs after running the REAL
faultline.pipeline_v2.stage_8_9_anchor_subdecompose.subdecompose_oversized_features
on hydrated Feature objects from fresh (mtime-today) cold scans. They are the
ONE authoritative set; earlier commit-message / report figures (e.g.
0.717->0.076, 0.498->0.388, 0.498->0.063) predate either the cold_eval
_owned_file_set fix (faultlines-app e28ef47: a fully de-owned feature now
contributes 0 owned files) or the depth-recurse stage rewrite, OR came from
the old sim's hand-rolled owned-set reconstruction. Trust THIS output.
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("slugs", nargs="*", help="cold-scan slugs (default: 4 demos)")
    ap.add_argument("--json", action="store_true", help="emit machine JSON")
    a = ap.parse_args(argv[1:])

    slugs = a.slugs or list(_DEFAULT_SLUGS)
    results = [simulate(s) for s in slugs]
    results = [r for r in results if r is not None]

    if a.json:
        print(json.dumps(results, indent=2))
        return 0

    for r in results:
        _print_human(r)
    print("\n" + _NOTE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
