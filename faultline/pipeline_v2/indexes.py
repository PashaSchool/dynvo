"""path_index + routes_index — additive scan-output surfaces.

Sprint 1 (2026-05-23). Both indexes are deterministic projections of
the existing Feature + Flow + route-extractor outputs. They live as
top-level keys on the FeatureMap JSON so MCP tools, the Sentry/PostHog
attribution worker, and the incremental scan merger can do O(1) file →
feature lookups without re-walking ``features[*].paths``.

Schema
======

::

    path_index = {
        "<repo-relative-path>": {
            "feature_uuid": "<uuid hex or empty>",
            "flow_uuids": ["<uuid hex>", ...],
        },
        ...
    }

    routes_index = [
        {
            "pattern": "/api/products",
            "method": "GET",       # or "PAGE" for filesystem routes
            "feature_uuid": "<uuid hex>",
            "file": "src/app/api/products/route.ts",
        },
        ...
    ]

Notes
-----

* A path can be claimed by at most ONE feature (the
  ``Feature.paths`` semantics — owned source code). A path can be
  attached to multiple flows.
* When two features both list the same path (rare — sibling-collapse
  generally prevents this), the first feature in the input order
  wins. We log a warning and continue.
* Empty ``feature_uuid`` is allowed for routes that don't map to a
  feature yet (extractor emitted a route but Stage 2 didn't attribute
  it). The MCP tool reads "empty → unknown ownership".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_path_index(
    features: list[dict[str, Any]],
    flows: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build ``{path: {feature_uuid, flow_uuids}}`` from features + flows.

    Args:
        features: list of feature dicts; each must carry ``uuid`` (set
            by lineage assignment) and ``paths``.
        flows: optional list of flow dicts; each carries ``uuid`` and
            ``paths``.

    Returns:
        A dict keyed by path. Stable order via sorted keys is left to
        the JSON writer.
    """
    index: dict[str, dict[str, Any]] = {}

    for feat in features:
        f_uuid = str(feat.get("uuid") or "")
        if not f_uuid:
            continue
        for raw in (feat.get("paths") or []):
            path = str(raw)
            entry = index.setdefault(
                path, {"feature_uuid": "", "flow_uuids": []},
            )
            if entry["feature_uuid"] and entry["feature_uuid"] != f_uuid:
                logger.debug(
                    "path_index: %s already owned by %s; ignoring %s",
                    path, entry["feature_uuid"], f_uuid,
                )
                continue
            entry["feature_uuid"] = f_uuid

    for flow in (flows or []):
        fl_uuid = str(flow.get("uuid") or "")
        if not fl_uuid:
            continue
        for raw in (flow.get("paths") or []):
            path = str(raw)
            entry = index.setdefault(
                path, {"feature_uuid": "", "flow_uuids": []},
            )
            if fl_uuid not in entry["flow_uuids"]:
                entry["flow_uuids"].append(fl_uuid)

    return index


def build_routes_index(
    features: list[dict[str, Any]],
    extractor_signals: dict[str, list[Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a flat route registry from route-extractor signals.

    The route extractor (``faultline/pipeline_v2/extractors/route.py``)
    emits ``RouteSignal`` candidates with ``pattern``, ``method``, and
    a source ``file``. We map each one to the owning feature via the
    feature's ``paths`` list (the route file must appear in exactly one
    feature). Routes that don't match any feature get
    ``feature_uuid=""`` so the dashboard can surface "orphan route".

    Args:
        features: lineage-assigned features (already carry ``uuid``).
        extractor_signals: Stage 1 output dict (``{extractor_name:
            [Signal, ...]}``). When ``None`` or missing the ``route``
            key, returns an empty list.

    Returns:
        Flat list of route dicts.
    """
    if not extractor_signals:
        return []
    route_signals = extractor_signals.get("route") or []
    if not route_signals:
        return []

    # file -> feature_uuid lookup (first-write-wins, matches path_index)
    file_owner: dict[str, str] = {}
    for feat in features:
        f_uuid = str(feat.get("uuid") or "")
        if not f_uuid:
            continue
        for raw in (feat.get("paths") or []):
            file_owner.setdefault(str(raw), f_uuid)

    out: list[dict[str, Any]] = []
    for sig in route_signals:
        # RouteSignal duck-typing — tolerate dict OR dataclass
        pattern = getattr(sig, "pattern", None) or (
            sig.get("pattern") if isinstance(sig, dict) else None
        )
        if not pattern:
            continue
        method = getattr(sig, "method", None) or (
            sig.get("method") if isinstance(sig, dict) else "GET"
        )
        source_file = getattr(sig, "file", None) or (
            sig.get("file") if isinstance(sig, dict) else None
        )
        file_str = str(source_file) if source_file else ""
        out.append({
            "pattern": str(pattern),
            "method": str(method or "GET"),
            "feature_uuid": file_owner.get(file_str, ""),
            "file": file_str,
        })
    return out


__all__ = ["build_path_index", "build_routes_index"]
