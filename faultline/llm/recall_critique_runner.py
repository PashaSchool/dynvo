"""Wiring layer between the legacy ``pipeline.run`` and the new
``CritiqueAggregator`` (Phase 5).

This module owns:

  - the env-var opt-in (``FAULTLINE_CRITIQUE_RECALL``), matching the
    Phase 3a / 3b opt-in pattern;
  - the Anthropic-SDK → ``LlmClient`` adapter (so the aggregator never
    imports ``anthropic`` directly);
  - extractor discovery (instantiate everything in
    ``faultline.extractors``, keep only the ones whose ``applicable``
    returns true for this repo);
  - cost tracking, so the critique pass shows up in ``cost_summary``
    just like the primary scan.

Failure mode is opportunistic on every layer — a missing API key, an
extractor exception, or a malformed model response all return the
input ``DeepScanResult`` unchanged so the rest of the pipeline keeps
going.

This module is the ONLY place that wires the aggregator to a real
LLM. Tests stay against ``CritiqueAggregator`` with a fake client.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.aggregators.critique import (
    CritiqueAggregator,
    apply_findings_to_deepscan,
    apply_findings_to_feature_map,
)
from faultline.signals import LlmResponse, Signal

if TYPE_CHECKING:  # pragma: no cover
    from faultline.llm.cost import CostTracker
    from faultline.llm.sonnet_scanner import DeepScanResult


logger = logging.getLogger(__name__)


# Haiku is enough for the critique JSON output — it's a classification
# + light extraction task, not architecture reasoning.
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS = 2_048


def is_enabled() -> bool:
    """True iff the ``FAULTLINE_CRITIQUE_RECALL`` env var is set.

    Matches the Phase 3a / 3b opt-in convention so all the
    extractor-driven levers share one truthy convention.
    """
    return os.environ.get("FAULTLINE_CRITIQUE_RECALL", "").lower() in {
        "1", "true", "yes", "on",
    }


# ── Anthropic adapter ─────────────────────────────────────────────────


class _AnthropicLlmClient:
    """Minimal ``LlmClient`` shim over the Anthropic Python SDK.

    Only implements the surface the recall-critique aggregator needs
    (``complete(system=, user=, max_tokens=)``). Records token usage to
    a passed-in ``CostTracker`` when provided so critique cost is
    visible alongside the primary scan.
    """

    name = "anthropic-critique-recall"

    def __init__(
        self,
        *,
        client,
        model: str,
        tracker: "CostTracker | None" = None,
    ) -> None:
        self._client = client
        self._model = model
        self._tracker = tracker

    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        tools=None,
    ) -> LlmResponse:
        _ = tools  # critique pass uses no tools
        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = ""
        for block in getattr(response, "content", []) or []:
            block_text = getattr(block, "text", None)
            if isinstance(block_text, str):
                text += block_text

        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        if self._tracker is not None and (in_tok or out_tok):
            self._tracker.record(
                provider="anthropic",
                model=self._model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                label="critique-recall",
            )

        return LlmResponse(
            text=text,
            input_tokens=in_tok,
            output_tokens=out_tok,
            stop_reason=str(getattr(response, "stop_reason", "end_turn")),
        )


# ── Extractor discovery ───────────────────────────────────────────────


def gather_signals(repo_root: Path) -> list[Signal]:
    """Instantiate every known extractor, keep applicable ones, run
    them, and concatenate signals.

    Order is deterministic but not load-bearing — the aggregator
    dedupes on category key.

    Per-extractor exceptions are swallowed and logged; one broken
    extractor must not poison the critique pass.
    """
    # Import lazily so test envs that don't have the optional
    # dependencies for one extractor still load the runner.
    from faultline.extractors.mvc_controller import RailsControllerExtractor
    from faultline.extractors.package_anchor import PackageAnchorExtractor
    from faultline.extractors.plugin_module import PluginModuleExtractor
    from faultline.extractors.route_file import (
        NextPagesRouteFileExtractor,
        NextRouteFileExtractor,
    )
    from faultline.extractors.schema_domain import SchemaDomainExtractor

    candidates = [
        PackageAnchorExtractor(),
        SchemaDomainExtractor(),
        RailsControllerExtractor(),
        NextRouteFileExtractor(),
        NextPagesRouteFileExtractor(),
        PluginModuleExtractor(),
    ]

    out: list[Signal] = []
    for ext in candidates:
        try:
            if not ext.applicable(repo_root):
                continue
            sigs = ext.extract(repo_root, files=())
        except Exception as exc:  # noqa: BLE001 — opportunistic
            logger.warning(
                "critique-recall: extractor %s failed (%s) — skipping",
                ext.name, exc,
            )
            continue
        out.extend(sigs)
        logger.info(
            "critique-recall: extractor %s emitted %d signals",
            ext.name, len(sigs),
        )
    return out


# ── Pipeline entry point ──────────────────────────────────────────────


def run_recall_critique(
    *,
    result: "DeepScanResult",
    repo_root: Path | None,
    api_key: str | None,
    model: str | None = None,
    tracker: "CostTracker | None" = None,
) -> "DeepScanResult":
    """Run the Phase 5 recall critique pass. Returns ``result``,
    mutated in place when findings were merged.

    Skips silently when:
      - the opt-in env var is not set (call this only when enabled),
      - ``repo_root`` is None (no extractor input),
      - no API key is available,
      - extractors emitted no signals,
      - the diff produced no missing categories,
      - the LLM call or response parsing failed.
    """
    if repo_root is None:
        logger.info("critique-recall: repo_root=None; skipping")
        return result

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logger.warning("critique-recall: no API key; skipping")
        return result

    try:
        signals = gather_signals(repo_root)
    except Exception as exc:  # noqa: BLE001 — opportunistic
        logger.warning("critique-recall: gather_signals failed (%s)", exc)
        return result

    if not signals:
        logger.info("critique-recall: no extractor signals; skipping")
        return result

    try:
        import anthropic
    except ImportError:  # pragma: no cover
        logger.warning("critique-recall: anthropic SDK not available")
        return result

    sdk_client = anthropic.Anthropic(api_key=key)
    llm = _AnthropicLlmClient(
        client=sdk_client,
        model=model or DEFAULT_MODEL,
        tracker=tracker,
    )

    try:
        findings = CritiqueAggregator(
            max_tokens=DEFAULT_MAX_TOKENS,
        ).run(
            detected_features=list(result.features.keys()),
            signals=signals,
            llm=llm,
            repo_root=repo_root,
        )
    except Exception as exc:  # noqa: BLE001 — opportunistic
        logger.warning("critique-recall: aggregator failed (%s)", exc)
        return result

    if not findings:
        logger.info("critique-recall: no findings produced")
        return result

    apply_findings_to_deepscan(result, findings)
    logger.info(
        "critique-recall: merged %d new feature(s) from critique",
        len(findings),
    )
    return result


def apply_critique_to_feature_map(
    *,
    feature_map,
    repo_root: Path | None,
    api_key: str | None,
    model: str | None = None,
    tracker: "CostTracker | None" = None,
) -> int:
    """Phase 5 Layer A entry point — runs the critique pass against
    a built ``FeatureMap`` and appends findings as new Feature
    objects with ``discovery_method="critique"``.

    Called from ``cli.py`` AFTER ``build_feature_map`` and the
    primary noise-drop filter, so critique findings reach the final
    JSON without being dropped by safety heuristics designed for
    primary-scan content.

    Returns the number of features actually appended (0 when
    skipped for any reason — missing API key, no signals, no
    findings, all findings already-owned, etc.). The function never
    raises on operational failure; the caller's FeatureMap is left
    unchanged if anything goes wrong.
    """
    if repo_root is None:
        logger.info("critique-recall: repo_root=None; skipping")
        return 0
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logger.warning("critique-recall: no API key; skipping")
        return 0

    try:
        signals = gather_signals(repo_root)
    except Exception as exc:  # noqa: BLE001 — opportunistic
        logger.warning("critique-recall: gather_signals failed (%s)", exc)
        return 0

    if not signals:
        logger.info("critique-recall: no extractor signals; skipping")
        return 0

    try:
        import anthropic
    except ImportError:  # pragma: no cover
        logger.warning("critique-recall: anthropic SDK not available")
        return 0

    sdk_client = anthropic.Anthropic(api_key=key)
    llm = _AnthropicLlmClient(
        client=sdk_client,
        model=model or DEFAULT_MODEL,
        tracker=tracker,
    )

    detected = [f.name for f in feature_map.features]
    try:
        findings = CritiqueAggregator(
            max_tokens=DEFAULT_MAX_TOKENS,
        ).run(
            detected_features=detected,
            signals=signals,
            llm=llm,
            repo_root=repo_root,
        )
    except Exception as exc:  # noqa: BLE001 — opportunistic
        logger.warning("critique-recall: aggregator failed (%s)", exc)
        return 0

    if not findings:
        logger.info("critique-recall: no findings produced")
        return 0

    added = apply_findings_to_feature_map(feature_map, findings)
    logger.info(
        "critique-recall: appended %d new feature(s) to FeatureMap",
        added,
    )
    return added


__all__ = [
    "is_enabled",
    "gather_signals",
    "run_recall_critique",
    "apply_critique_to_feature_map",
]
