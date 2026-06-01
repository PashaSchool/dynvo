"""Deterministic, LLM-free scan runner + output normalizer for the eval.

Why this is deterministic
-------------------------
``run_pipeline_v2`` only reaches the network in three places — the Stage
0.5 stack auditor, Stage 3 flow naming, and Stage 4 residual clustering —
and every one of them builds its Anthropic client from
``ANTHROPIC_API_KEY``. With no key the client factory returns ``None`` and
each stage short-circuits to a deterministic, empty-LLM result (the
auditor returns an echo-of-Stage-0 fallback verdict). So the ENTIRE
deterministic extractor pipeline (Stage 0/0.6/1/2 + metrics) runs, while
the LLM stages contribute nothing. We additionally force the key out of
the environment so a developer with a key set locally still gets the same
LLM-free run as CI.

We also pin ``$HOME`` to a temp dir so the per-run artifact tree
(``~/.faultline/logs/...``) and the assignments cache never leak between
runs or pick up a developer's stale cache.

The normalizer drops every volatile field (timestamps, health/coverage
scores that depend on the wall clock, absolute paths, run ids, costs) and
keeps only what a detection regression would actually change: the set of
developer-feature names, each feature's sorted file paths, and the sorted
flow names. That normalized view is what the golden snapshots store.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def run_deterministic_scan(repo_path: Path | str, *, home: Path) -> dict[str, Any]:
    """Run the LLM-free deterministic pipeline and return the FeatureMap.

    Args:
        repo_path: a materialized fixture git repo.
        home: a temp dir to use as ``$HOME`` so artifacts + caches are
            isolated per run.
    """
    # Import lazily so collection doesn't pay the import cost if the
    # eval is deselected.
    from faultline.pipeline_v2.run import run_pipeline_v2

    repo_path = Path(repo_path).resolve()
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    out_path = home / "feature-map.json"

    # Force a hermetic, key-less, network-free environment for the call.
    saved = {
        k: os.environ.get(k)
        for k in ("ANTHROPIC_API_KEY", "HOME", "FAULTLINE_HOME")
    }
    try:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ["HOME"] = str(home)
        result = run_pipeline_v2(
            repo_path,
            model="haiku",
            out_path=out_path,
            # Deterministic name picking — no LLM reconcile.
            llm_reconcile=False,
            # Pin the history window so "days since" math can't drift the
            # set of in-window commits as real time passes.
            days=36500,
            # Stable run id → stable artifact dir name.
            run_id="eval-fixed",
        )
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # Belt-and-suspenders: a deterministic run must never spend money.
    assert result.get("calls", 0) == 0, (
        f"eval scan made {result.get('calls')} LLM calls — not deterministic"
    )
    assert float(result.get("cost_usd", 0.0)) == 0.0, (
        f"eval scan cost ${result.get('cost_usd')} — not LLM-free"
    )

    import json

    return json.loads(out_path.read_text(encoding="utf-8"))


# ── Normalization ────────────────────────────────────────────────────


def normalize(feature_map: dict[str, Any]) -> dict[str, Any]:
    """Reduce a FeatureMap to a stable, comparison-only subset.

    Keeps ONLY:
      * ``stack`` (the detected stack tag — a regression here is real),
      * ``features``: a sorted list of ``{name, paths}`` where ``paths``
        is sorted, drawn from the developer-feature layer,
      * ``flows``: the sorted set of flow names across all features.

    Everything volatile (scores, timestamps, run ids, costs, symbol
    line ranges, health) is dropped so the golden only trips on a
    genuine detection change.
    """
    dev_features = feature_map.get("developer_features") or []

    norm_features: list[dict[str, Any]] = []
    flow_names: set[str] = set()
    for feat in dev_features:
        name = feat.get("name")
        if not name:
            continue
        paths = sorted(str(p) for p in (feat.get("paths") or []))
        norm_features.append({"name": str(name), "paths": paths})
        for fl in feat.get("flows") or []:
            fname = fl.get("name") if isinstance(fl, dict) else fl
            if fname:
                flow_names.add(str(fname))

    # Also fold in any top-level flows[] (bipartite store), if present.
    for fl in feature_map.get("flows") or []:
        fname = fl.get("name") if isinstance(fl, dict) else fl
        if fname:
            flow_names.add(str(fname))

    norm_features.sort(key=lambda f: f["name"])

    return {
        "stack": feature_map.get("scan_meta", {}).get("stack")
        or feature_map.get("stack"),
        "features": norm_features,
        "flows": sorted(flow_names),
    }


def detected_feature_names(normalized: dict[str, Any]) -> list[str]:
    """Convenience accessor for the precision/recall scorer."""
    return [f["name"] for f in normalized.get("features", [])]
