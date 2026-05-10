"""Top-level enrichment: feature-map.json + analytics → enriched feature-map.

The faultline CLI writes a `feature-map.json`. This module reads that
JSON, fetches metrics from any configured providers, computes impact
scores, and writes a new JSON document that adds two top-level keys:

    {
        ...original feature map fields...,
        "impact_scores": [...],     # list of ImpactScore dicts
        "analytics_meta": {
            "providers": ["posthog", "sentry"],
            "days": 30,
            "page_metrics_count": 142,
            "error_metrics_count": 38
        }
    }

The original feature map is left untouched — enrichment is purely
additive so older consumers keep working.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .models import (
    AnalyticsProvider,
    ErrorMetrics,
    ImpactScore,
    PageMetrics,
    compute_impact_scores,
)


def enrich_feature_map(
    feature_map: dict,
    *,
    providers: list[AnalyticsProvider],
    days: int = 30,
) -> dict:
    """Synchronous wrapper around the async enrichment pipeline.

    Returns a new dict with `impact_scores` + `analytics_meta` attached.
    """
    return asyncio.run(
        enrich_feature_map_async(feature_map, providers=providers, days=days)
    )


async def enrich_feature_map_async(
    feature_map: dict,
    *,
    providers: list[AnalyticsProvider],
    days: int = 30,
) -> dict:
    """Async enrichment — preferred when caller already has a loop."""
    traffic: list[PageMetrics] = []
    errors: list[ErrorMetrics] = []
    used: list[str] = []

    for provider in providers:
        try:
            ok = await provider.validate_connection()
        except Exception:  # noqa: BLE001 - opportunistic
            ok = False
        if not ok:
            continue
        used.append(provider.name)
        try:
            traffic.extend(await provider.get_page_traffic(days=days))
        except Exception:  # noqa: BLE001
            pass
        try:
            errors.extend(await provider.get_error_counts(days=days))
        except Exception:  # noqa: BLE001
            pass
        try:
            await provider.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    flows = _flatten_flows(feature_map)
    scores = compute_impact_scores(flows, traffic, errors)

    return {
        **feature_map,
        "impact_scores": [s.model_dump() for s in scores],
        "analytics_meta": {
            "providers": used,
            "days": days,
            "page_metrics_count": len(traffic),
            "error_metrics_count": len(errors),
            "scored_flows": len(scores),
        },
    }


def enrich_feature_map_from_metrics(
    feature_map: dict,
    *,
    traffic: list[PageMetrics] | None = None,
    errors: list[ErrorMetrics] | None = None,
) -> dict:
    """Pure-Python variant — no providers, just precomputed metrics.

    Useful for tests, replays, or callers that fetch their own data.
    """
    flows = _flatten_flows(feature_map)
    scores: list[ImpactScore] = compute_impact_scores(
        flows, traffic or [], errors or [],
    )
    return {
        **feature_map,
        "impact_scores": [s.model_dump() for s in scores],
        "analytics_meta": {
            "providers": [],
            "days": 0,
            "page_metrics_count": len(traffic or []),
            "error_metrics_count": len(errors or []),
            "scored_flows": len(scores),
        },
    }


def _flatten_flows(feature_map: dict) -> list[dict[str, Any]]:
    """Pull (name, health_score, paths) tuples out of the feature map.

    A feature with `flows` contributes one entry per flow; a feature
    without flows contributes itself.
    """
    flows_data: list[dict[str, Any]] = []
    for feature in feature_map.get("features", []):
        feature_paths = feature.get("paths", [])
        feature_flows = feature.get("flows") or []
        if feature_flows:
            for flow in feature_flows:
                flows_data.append({
                    "name": flow.get("name", ""),
                    "health_score": float(flow.get("health_score", 0)),
                    "paths": flow.get("paths") or feature_paths,
                })
        else:
            flows_data.append({
                "name": feature.get("name", ""),
                "health_score": float(feature.get("health_score", 0)),
                "paths": feature_paths,
            })
    return flows_data


# ── Disk I/O helpers (used by CLI) ─────────────────────────────────────


def load_feature_map(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_feature_map(feature_map: dict, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(feature_map, indent=2, default=str),
        encoding="utf-8",
    )
