"""Stage 6.4 — Framework-aware enrichment (Sprint C4, deterministic).

Why this stage exists
=====================

After Sprint C3 the pipeline resolves cross-file relationships via
the JS/TS import graph (forward + reverse). That covers everything
the language can express via ``import`` statements. It does NOT
cover:

  * **HTTP route handlers** reached via fetch URL strings — the URL is
    a string literal, not an import. A page that calls
    ``fetch("/api/rules", {method: "POST"})`` shares no symbol with
    ``app/api/rules/route.ts`` so the import graph cannot link them.
  * **Server Actions** reached via Next runtime magic — the
    ``"use server"`` callsite is invoked across the network boundary
    without an explicit symbol import on the client side.
  * **Store mutations** dispatched by action-type string (Zustand,
    Redux, Pinia, ...).
  * **tRPC procedures** referenced by namespace string.

Each pattern is framework-specific. v1 (Sprint C4) ships ONE linker
covering the biggest gap — Next.js App Router fetch URL → route.ts
handler resolution. The orchestrator is built to support N linkers
plugged in via Python entry-points without touching this file.

Algorithm
=========

1. Discover all registered :class:`FrameworkLinker` instances via
   :func:`faultline.framework_linkers._discovery.discover_linkers`.
2. Filter by :meth:`is_active` — each linker self-skips when its
   framework doesn't apply.
3. For each active linker × each feature, call
   :meth:`link_for_feature` and collect :class:`FrameworkLink` records.
4. Convert each link into a :class:`FlowSymbolAttribution` and append
   to ``feature.symbol_attributions`` so downstream consumers (the
   landing app, MCP queries, coverage rollup) see the new line-range
   surface alongside the C3-enriched payload.

Determinism
===========

Pure file IO + regex. NO LLM. NO network. NO mutation of any feature
field except ``symbol_attributions``.

Telemetry
=========

Each linker contributes a sub-dict to the artifact under
``per_linker[<name>]``. Skipped linkers appear under ``skipped_linkers``
with the reason ("is_active returned False"). The orchestrator surfaces
``active_linkers`` / ``links_emitted_total`` into ``scan_meta.stage_6_4``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from faultline.framework_linkers._discovery import discover_linkers
from faultline.framework_linkers.base import FrameworkLink, FrameworkLinker
from faultline.models.types import FlowSymbolAttribution

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Result types ────────────────────────────────────────────────────────────


@dataclass
class EnrichmentResult:
    """Outcome of one Stage 6.4 run."""

    enriched_features: list["Feature"]
    active_linkers: list[str]
    skipped_linkers: list[dict[str, str]]
    per_linker: dict[str, dict[str, Any]]
    links_emitted_total: int
    elapsed_sec: float

    def telemetry(self) -> dict[str, Any]:
        return {
            "stage": "6.4-framework-enrich",
            "elapsed_sec": round(self.elapsed_sec, 3),
            "active_linkers": list(self.active_linkers),
            "skipped_linkers": list(self.skipped_linkers),
            "per_linker": dict(self.per_linker),
            "links_emitted_total": self.links_emitted_total,
        }


# ── Helpers ────────────────────────────────────────────────────────────────


def _link_to_symbol_attribution(link: FrameworkLink) -> FlowSymbolAttribution:
    """Project a :class:`FrameworkLink` into the shared symbol-attribution
    schema so existing consumers (landing app, MCP) can render it without
    a new pydantic model.

    Role string format: ``framework-link:<kind>`` so consumers can filter
    on the prefix to find framework links specifically.
    """
    # Sprint C4 — extend the closed set of FlowSymbolAttribution roles
    # with a single new bucket: ``framework-link``. The link_kind is
    # encoded into the symbol name so we don't fork the pydantic enum.
    role_tag = "framework-link"
    return FlowSymbolAttribution(
        file=link.target_file,
        symbol=f"{role_tag}:{link.link_kind}:{link.target_symbol}",
        line_start=link.target_line_start,
        line_end=link.target_line_end,
        role=role_tag,
    )


def _attach_links_to_feature(
    feature: "Feature", links: list[FrameworkLink],
) -> int:
    """Append link-derived :class:`FlowSymbolAttribution`s to ``feature``.

    Returns the count of NEW attributions added (deduplicated by
    ``(target_file, target_symbol, line_start, line_end)``).
    """
    if not links:
        return 0

    existing_keys: set[tuple[str, str, int, int]] = set()
    for attr in feature.symbol_attributions or []:
        existing_keys.add(
            (attr.file, attr.symbol, attr.line_start, attr.line_end),
        )

    added = 0
    for link in links:
        attr = _link_to_symbol_attribution(link)
        key = (attr.file, attr.symbol, attr.line_start, attr.line_end)
        if key in existing_keys:
            continue
        existing_keys.add(key)
        feature.symbol_attributions = list(feature.symbol_attributions or []) + [attr]
        added += 1
    return added


# ── Public entry point ─────────────────────────────────────────────────────


def run_stage_6_4(
    ctx: "ScanContext",
    features: list["Feature"],
    log: "StageLogger",
    *,
    linkers: list[FrameworkLinker] | None = None,
) -> EnrichmentResult:
    """Run Stage 6.4 framework-aware enrichment.

    Args:
        ctx: scan context from Stage 0.
        features: post-Stage-6.3 developer features (mutated in place:
            ``feature.symbol_attributions`` may grow).
        log: stage logger (orchestrator-owned).
        linkers: optional override (tests pass canned linkers). When
            ``None`` we discover via entry-points / built-in registry.

    Returns:
        :class:`EnrichmentResult` with per-linker telemetry.
    """
    t0 = time.monotonic()

    available = linkers if linkers is not None else discover_linkers()
    if not available:
        log.info("no framework linkers registered")
        return EnrichmentResult(
            enriched_features=features,
            active_linkers=[],
            skipped_linkers=[],
            per_linker={},
            links_emitted_total=0,
            elapsed_sec=round(time.monotonic() - t0, 3),
        )

    active: list[FrameworkLinker] = []
    skipped: list[dict[str, str]] = []
    for linker in available:
        try:
            if linker.is_active(ctx):
                active.append(linker)
                log.info(f"linker-active: {linker.name}", linker=linker.name)
            else:
                skipped.append({"name": linker.name, "reason": "is_active=False"})
                log.info(f"linker-skipped: {linker.name}", linker=linker.name)
        except Exception as exc:  # noqa: BLE001 — non-fatal
            skipped.append({"name": linker.name, "reason": f"is_active raised: {type(exc).__name__}"})
            log.warn(
                f"linker {linker.name} is_active raised: {exc}",
                linker=linker.name,
            )
            logger.warning("linker.is_active raised", exc_info=True)

    per_linker: dict[str, dict[str, Any]] = {}
    links_total = 0
    for linker in active:
        linker_total = 0
        for feature in features:
            try:
                links = linker.link_for_feature(feature, ctx, log)
            except Exception as exc:  # noqa: BLE001 — non-fatal
                log.warn(
                    f"linker {linker.name} raised on feature "
                    f"{feature.name}: {exc}",
                    linker=linker.name, feature=feature.name,
                )
                logger.warning(
                    "framework linker raised on feature %s",
                    feature.name, exc_info=True,
                )
                continue
            if not links:
                continue
            added = _attach_links_to_feature(feature, links)
            linker_total += added
        links_total += linker_total

        # Pull rich telemetry from the linker if it exposes a ``telemetry``
        # attribute (the v1 linker does); otherwise emit a minimal summary.
        if hasattr(linker, "telemetry"):
            tel_obj = getattr(linker, "telemetry")
            if hasattr(tel_obj, "as_dict"):
                per_linker[linker.name] = tel_obj.as_dict()
            else:
                per_linker[linker.name] = {"links_emitted": linker_total}
        else:
            per_linker[linker.name] = {"links_emitted": linker_total}
        # Always overlay the orchestrator-counted total so the artifact
        # captures the actual attributions added (vs the linker's own
        # raw emit count, which may exceed it after dedup).
        per_linker[linker.name]["links_attached_to_features"] = linker_total

    return EnrichmentResult(
        enriched_features=features,
        active_linkers=[lk.name for lk in active],
        skipped_linkers=skipped,
        per_linker=per_linker,
        links_emitted_total=links_total,
        elapsed_sec=round(time.monotonic() - t0, 3),
    )


__all__ = ["EnrichmentResult", "run_stage_6_4"]
