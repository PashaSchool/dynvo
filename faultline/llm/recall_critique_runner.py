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
DEFAULT_MAX_TOKENS = 32_768


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


def gather_signals(
    repo_root: Path, *, repo_structure=None,
) -> list[Signal]:
    """Instantiate every known extractor, keep applicable ones, run
    them, and concatenate signals.

    Order is deterministic but not load-bearing — the aggregator
    dedupes on category key.

    Per-extractor exceptions are swallowed and logged; one broken
    extractor must not poison the critique pass.

    ``repo_structure`` (Sprint 9a): when supplied, passed through to
    extractors that gate on library-vs-SaaS (currently
    ``TestFileExtractor``). Existing extractors ignore the kwarg.
    """
    # Import lazily so test envs that don't have the optional
    # dependencies for one extractor still load the runner.
    from faultline.extractors.jsx_nav import JsxNavExtractor
    from faultline.extractors.mvc_controller import RailsControllerExtractor
    from faultline.extractors.package_anchor import PackageAnchorExtractor
    from faultline.extractors.plugin_module import PluginModuleExtractor
    from faultline.extractors.route_file import (
        NextPagesRouteFileExtractor,
        NextRouteFileExtractor,
    )
    from faultline.extractors.route_segment import RouteSegmentExtractor
    from faultline.extractors.schema_domain import SchemaDomainExtractor
    from faultline.extractors.python_package_subdir import (
        PythonPackageSubdirExtractor,
    )
    from faultline.extractors.schema_relations import SchemaRelationsExtractor
    from faultline.extractors.server_actions import ServerActionsExtractor
    from faultline.extractors.go_module import (
        GoPerFileFolderExtractor,
        GoSubpackageExtractor,
        GoTestFileExtractor,
        GoTopLevelFileExtractor,
    )
    from faultline.extractors.test_file import TestFileExtractor
    from faultline.extractors.trpc_router import TrpcRouterExtractor
    from faultline.extractors.ts_library_exports import (
        TsLibraryExportsExtractor,
    )

    candidates = [
        PackageAnchorExtractor(),
        SchemaDomainExtractor(),
        SchemaRelationsExtractor(),
        RailsControllerExtractor(),
        NextRouteFileExtractor(),
        NextPagesRouteFileExtractor(),
        RouteSegmentExtractor(),
        PluginModuleExtractor(),
        JsxNavExtractor(),
        ServerActionsExtractor(),
        TrpcRouterExtractor(),
        PythonPackageSubdirExtractor(),
        TsLibraryExportsExtractor(),
        TestFileExtractor(),
        GoTopLevelFileExtractor(),
        GoSubpackageExtractor(),
        GoTestFileExtractor(),
        GoPerFileFolderExtractor(),
    ]

    out: list[Signal] = []
    for ext in candidates:
        try:
            if not ext.applicable(repo_root):
                continue
            # Pass repo_structure when the extractor accepts it
            # (TestFileExtractor uses it for the is_library safety
            # gate). Other extractors don't accept the kwarg, so we
            # introspect.
            import inspect as _insp
            sig = _insp.signature(ext.extract)
            if "repo_structure" in sig.parameters:
                sigs = ext.extract(
                    repo_root, files=(), repo_structure=repo_structure,
                )
            else:
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
    repo_structure=None,
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

    try:
        signals = gather_signals(repo_root, repo_structure=repo_structure)
    except Exception as exc:  # noqa: BLE001 — opportunistic
        logger.warning("critique-recall: gather_signals failed (%s)", exc)
        return result

    if not signals:
        logger.info("critique-recall: no extractor signals; skipping")
        return result

    from faultline.llm.factory import make_client
    try:
        llm = make_client("critique", tracker=tracker, api_key=api_key)
    except RuntimeError as exc:
        logger.warning("critique-recall: %s", exc)
        return result

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
    repo_structure=None,
):
    """Sprint 10a — pure-function. Returns ``(new_feature_map,
    added_count)``. Input ``feature_map`` is NEVER mutated.

    Phase 5 Layer A entry point — runs the critique pass against
    a built ``FeatureMap`` and appends findings as new Feature
    objects with ``discovery_method="critique"``.
    """
    new_fm = feature_map.model_copy(deep=True)
    feature_map = new_fm  # operate on the copy below

    if repo_root is None:
        logger.info("critique-recall: repo_root=None; skipping")
        return new_fm, 0

    try:
        signals = gather_signals(repo_root, repo_structure=repo_structure)
    except Exception as exc:  # noqa: BLE001 — opportunistic
        logger.warning("critique-recall: gather_signals failed (%s)", exc)
        return new_fm, 0

    if not signals:
        logger.info("critique-recall: no extractor signals; skipping")
        return new_fm, 0

    from faultline.llm.factory import make_client
    try:
        llm = make_client("critique", tracker=tracker, api_key=api_key)
    except RuntimeError as exc:
        logger.warning("critique-recall: %s", exc)
        return new_fm, 0

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
        return new_fm, 0

    if not findings:
        logger.info("critique-recall: no findings produced")
        return new_fm, 0

    added = apply_findings_to_feature_map(feature_map, findings)
    logger.info(
        "critique-recall: appended %d new feature(s) to FeatureMap",
        added,
    )
    return new_fm, added


__all__ = [
    "is_enabled",
    "gather_signals",
    "run_recall_critique",
    "apply_critique_to_feature_map",
]
