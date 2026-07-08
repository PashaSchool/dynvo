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
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


# ── Sprint F (2026-05-20) — concurrency + budget tunables ───────────────────
#
# Per (linker × feature) link emission is independent file IO + regex,
# so a small thread pool reclaims wall time on monorepos with hundreds
# of features.
#
# The wall budget is SCALE-INVARIANT: it is a per-(feature×linker) time
# allowance multiplied by the number of (active-linker × feature) work
# units, NOT a flat wall. A repo with 10x the features gets 10x the
# budget, so in the normal case every feature is enriched by every
# active linker and ``features_budget_skipped`` is 0. The guard only
# fires on a genuinely pathological linker. This honours
# ``rule-no-magic-tuning`` — there is no fixed wall a big repo blows.
# An external runner can still enforce a hard ceiling via
# ``WORKER_TIMEOUT_SEC``; set the per-unit allowance to 0 to disable.
DEFAULT_MAX_WORKERS = 4
# Per (feature × active-linker) time allowance (seconds). Effective
# wall = DEFAULT_PER_UNIT_BUDGET_SEC * len(features) * len(active_linkers).
# Override the resolved wall directly with FAULTLINE_STAGE_6_4_BUDGET_SEC
# (absolute seconds) or the per-unit allowance with
# FAULTLINE_STAGE_6_4_PER_FEATURE_SEC.
DEFAULT_PER_UNIT_BUDGET_SEC = 0.5
# Backward-compat alias for callers/tests importing the old flat default.
DEFAULT_WALL_BUDGET_SEC = DEFAULT_PER_UNIT_BUDGET_SEC


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
    # Sprint F (2026-05-20) — graceful degradation telemetry.
    budget_exceeded: bool = False
    budget_sec: float = 0.0
    features_budget_skipped: int = 0
    max_workers: int = 1

    def telemetry(self) -> dict[str, Any]:
        return {
            "stage": "6.4-framework-enrich",
            "elapsed_sec": round(self.elapsed_sec, 3),
            "active_linkers": list(self.active_linkers),
            "skipped_linkers": list(self.skipped_linkers),
            "per_linker": dict(self.per_linker),
            "links_emitted_total": self.links_emitted_total,
            "concurrency": {
                "max_workers": self.max_workers,
                "budget_sec": self.budget_sec,
                "budget_exceeded": self.budget_exceeded,
                "features_budget_skipped": self.features_budget_skipped,
            },
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
        role="framework-link",
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


def _resolve_int_env(env_name: str, default: int) -> int:
    """Read ``env_name`` from the environment, coerce to int, clamp to ≥1."""
    raw = os.environ.get(env_name)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _resolve_float_env(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _compute_links_for_feature(
    linker: FrameworkLinker,
    feature: "Feature",
    ctx: "ScanContext",
    log: "StageLogger",
) -> tuple[list[FrameworkLink], str | None]:
    """Worker function: call linker for ONE feature, catching exceptions.

    Returns ``(links, error_msg_or_None)``. Pure-compute: does not
    mutate ``feature``. The main thread is responsible for the
    subsequent :func:`_attach_links_to_feature` call so attribution
    lists are mutated under serial ordering.
    """
    try:
        return linker.link_for_feature(feature, ctx, log), None
    except Exception as exc:  # noqa: BLE001 — non-fatal
        return [], f"{type(exc).__name__}: {exc}"


def run_stage_6_4(
    ctx: "ScanContext",
    features: list["Feature"],
    log: "StageLogger",
    *,
    linkers: list[FrameworkLinker] | None = None,
    max_workers: int | None = None,
    wall_budget_sec: float | None = None,
) -> EnrichmentResult:
    """Run Stage 6.4 framework-aware enrichment.

    Args:
        ctx: scan context from Stage 0.
        features: post-Stage-6.3 developer features (mutated in place:
            ``feature.symbol_attributions`` may grow).
        log: stage logger (orchestrator-owned).
        linkers: optional override (tests pass canned linkers). When
            ``None`` we discover via entry-points / built-in registry.
        max_workers: thread-pool size for per-feature link computation.
            Defaults to :data:`DEFAULT_MAX_WORKERS` (=4). Set to 1 for
            the legacy serial path (used in tests asserting log order).
        wall_budget_sec: graceful-degradation wall clock budget per
            linker. When exceeded, remaining features for that linker
            are recorded as ``budget_skipped``. Defaults to
            :data:`DEFAULT_WALL_BUDGET_SEC`. Set to 0 to disable.

    Returns:
        :class:`EnrichmentResult` with per-linker telemetry.
    """
    t0 = time.monotonic()

    if max_workers is None:
        max_workers = _resolve_int_env(
            "FAULTLINE_STAGE_6_4_WORKERS", DEFAULT_MAX_WORKERS,
        )
    # Absolute-seconds override (caller arg or env) wins; the
    # scale-invariant default is computed below once ``active`` is known.
    budget_override = wall_budget_sec
    if budget_override is None:
        env_budget = os.environ.get("FAULTLINE_STAGE_6_4_BUDGET_SEC")
        if env_budget:
            try:
                budget_override = float(env_budget)
            except ValueError:
                budget_override = None

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
            budget_sec=budget_override or 0.0,
            max_workers=max_workers,
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

    # Per-(feature × active-linker) time allowance — always resolved; the
    # factor of the default scale-invariant wall AND the divisor of the
    # deterministic seconds→count cut below.
    per_unit_sec = _resolve_float_env(
        "FAULTLINE_STAGE_6_4_PER_FEATURE_SEC",
        DEFAULT_PER_UNIT_BUDGET_SEC,
    )
    n_features = len(features)
    n_active = len(active)
    total_units = n_features * n_active

    # Resolve the wall budget + the deterministic degradation boundary.
    # Absolute override (caller/env) is interpreted as a budget of per-unit
    # allowances: keep_units = floor(budget / per_unit_sec). The kept
    # work-units are the canonical PREFIX of the linker-major order
    # ((linker0 × all features), then (linker1 × all features), …); the
    # skipped ones are the complementary SUFFIX — a pure function of input,
    # never of thread-completion timing (D-CLUSTER fix). The default wall is
    # scale-invariant so nothing is skipped on a healthy repo.
    if budget_override is not None:
        wall_budget_sec = budget_override
        if budget_override <= 0 or per_unit_sec <= 0:
            keep_units = total_units
        else:
            keep_units = min(total_units, int(budget_override // per_unit_sec))
    else:
        wall_budget_sec = per_unit_sec * (max(1, n_features) * max(1, n_active))
        keep_units = total_units

    per_linker: dict[str, dict[str, Any]] = {}
    links_total = 0
    budget_exceeded = keep_units < total_units
    total_budget_skipped = 0

    # Canonical feature order (stable content key = name + sorted paths) so the
    # per-linker cut keeps the SAME features every run, independent of the input
    # list order AND of thread-completion timing. Computed once; reused per
    # linker. Output/attach order stays the original feature order.
    canonical_order = sorted(
        range(n_features),
        key=lambda i: (features[i].name or "", tuple(sorted(features[i].paths or []))),
    )

    for l_idx, linker in enumerate(active):
        # Deterministic cut over the linker-major work-unit order: this
        # linker occupies work-units [l_idx*n_features, (l_idx+1)*n_features).
        units_before = l_idx * n_features
        keep_in_linker = max(0, min(n_features, keep_units - units_before))
        per_linker_skipped = n_features - keep_in_linker

        if keep_in_linker <= 0:
            # Whole linker is past the budget cut → skipped wholesale
            # (a canonical suffix of the work-unit order; timing-free).
            per_linker[linker.name] = {
                "links_emitted": 0,
                "links_attached_to_features": 0,
                "budget_skipped": n_features,
            }
            total_budget_skipped += n_features
            log.warn(
                f"linker-budget-skipped: {linker.name} "
                f"budget_sec={wall_budget_sec}",
                linker=linker.name,
            )
            continue

        # Keep the canonically-lowest ``keep_in_linker`` features for this linker.
        kept_indices = set(canonical_order[:keep_in_linker])
        linker_total = 0

        if max_workers <= 1:
            # Serial path.
            for index in range(n_features):
                if index not in kept_indices:
                    continue
                feature = features[index]
                links, err = _compute_links_for_feature(
                    linker, feature, ctx, log,
                )
                if err is not None:
                    log.warn(
                        f"linker {linker.name} raised on feature "
                        f"{feature.name}: {err}",
                        linker=linker.name, feature=feature.name,
                    )
                    continue
                if not links:
                    continue
                added = _attach_links_to_feature(feature, links)
                linker_total += added
        else:
            # Parallel path — dispatch the kept features; apply serially in
            # original order so attribution dedup is deterministic.
            with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix=f"stage6_4_{linker.name}",
            ) as pool:
                future_to_index: dict[Any, int] = {
                    pool.submit(
                        _compute_links_for_feature,
                        linker, features[index], ctx, log,
                    ): index
                    for index in range(n_features)
                    if index in kept_indices
                }
                links_by_index: dict[int, list[FrameworkLink]] = {}
                for fut in as_completed(future_to_index):
                    idx = future_to_index[fut]
                    try:
                        links, err = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        err = f"{type(exc).__name__}: {exc}"
                        links = []
                    if err is not None:
                        log.warn(
                            f"linker {linker.name} raised on feature "
                            f"{features[idx].name}: {err}",
                            linker=linker.name,
                            feature=features[idx].name,
                        )
                    links_by_index[idx] = links

                for index in range(n_features):
                    if index not in kept_indices:
                        continue
                    feat_links = links_by_index.get(index)
                    if not feat_links:
                        continue
                    added = _attach_links_to_feature(features[index], feat_links)
                    linker_total += added

        links_total += linker_total
        total_budget_skipped += per_linker_skipped

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
        if per_linker_skipped:
            per_linker[linker.name]["budget_skipped"] = per_linker_skipped

    if budget_exceeded:
        log.warn(
            f"stage_6_4_budget_exceeded budget_sec={wall_budget_sec} "
            f"elapsed_sec={round(time.monotonic() - t0, 3)} "
            f"features_skipped={total_budget_skipped}",
        )

    return EnrichmentResult(
        enriched_features=features,
        active_linkers=[lk.name for lk in active],
        skipped_linkers=skipped,
        per_linker=per_linker,
        links_emitted_total=links_total,
        elapsed_sec=round(time.monotonic() - t0, 3),
        budget_exceeded=budget_exceeded,
        budget_sec=wall_budget_sec or 0.0,
        features_budget_skipped=total_budget_skipped,
        max_workers=max_workers,
    )


__all__ = ["EnrichmentResult", "run_stage_6_4"]
