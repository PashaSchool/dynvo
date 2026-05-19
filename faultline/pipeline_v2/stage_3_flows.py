"""Stage 3 — flow detection (Haiku 4.5, parallel).

Per :class:`DeveloperFeature` from Stage 2, attach one small Haiku call
that enumerates the user-action flows that live inside that feature.
Cheap, parallel across features. The prompt is intentionally narrow:

  - Input: feature slug + paths sample + AST-extracted exported symbols
    + extracted route signatures.
  - Output: ``{flows: [{name, description}]}`` with kebab verb-phrase
    flow slugs.

Output is attached as ``feature.flows: list[Flow]`` on a v2-friendly
:class:`FeatureWithFlows` wrapper. We do NOT mutate the input
:class:`DeveloperFeature` (frozen dataclass-like); instead we emit a
new record so the caller can decide what to do.

Determinism / cost
------------------

  - Default model: ``claude-haiku-4-5-20251001`` (cheap, sufficient for
    structured 1k-in / 1k-out tasks).
  - ``max_tokens=2000`` (≈1k flow JSON output).
  - ``temperature=0`` when supported (Haiku does support it).
  - Parallelism: :class:`ThreadPoolExecutor` with ``max_workers=8``.
  - Wall-time cap: ``timeout`` arg (default 5 minutes).

Features with <3 exports skip the LLM call entirely and receive
``flows=[]`` — small features rarely have user-action flows worth
enumerating, and the LLM tends to hallucinate them.

Line-range attribution
----------------------

Each emitted flow is associated with the AST :class:`SymbolRange`
entries whose names appear in the flow's prompt context. We use the
deterministic match (substring of the flow name vs. the symbol name)
to bridge LLM output → line ranges without a second LLM call.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from faultline.analyzer.ast_extractor import (
    FileSignature,
    extract_signatures,
)
from faultline.llm.cost import CostTracker, deterministic_params

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext
    from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature

logger = logging.getLogger(__name__)


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 2000
DEFAULT_MAX_WORKERS = 8
DEFAULT_WALL_TIMEOUT_S = 300  # 5 minutes
MIN_EXPORTS_FOR_FLOW_DETECTION = 3

# Prompt sample caps — keep the request small.
MAX_PATHS_IN_PROMPT = 20
MAX_EXPORTS_IN_PROMPT = 30
MAX_ROUTES_IN_PROMPT = 20

# Output validation
MAX_FLOWS_PER_FEATURE = 12
MIN_FLOWS_PER_FEATURE_HINT = 3  # only a hint to the LLM; not enforced post-hoc

# Per [[rule-flow-naming]]: ``manage-X-flow`` shape, NEVER ``use-X-flow``.
_USE_PREFIX_PATTERN = re.compile(r"^use-", re.IGNORECASE)
_KEBAB_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*-flow$")


# ── Output dataclasses ─────────────────────────────────────────────────────


@dataclass
class FlowSpec:
    """One flow detected by Stage 3.

    Mirrors the public :class:`faultline.models.types.Flow` shape but
    stays as a plain dataclass so Stage 3 has no Pydantic dependency.
    Stage 7 (output assembly) converts these to ``Flow`` instances.

    Attributes:
        name: kebab-case verb-phrase slug, ending in ``-flow``.
        description: one-sentence human-readable summary.
        entry_point_file: optional path that originated the flow.
        entry_point_line: optional 1-indexed start line of the symbol
            the LLM grounded the flow on.
        symbol_names: list of symbol names the LLM grounded the flow on.
    """

    name: str
    description: str = ""
    entry_point_file: str | None = None
    entry_point_line: int | None = None
    symbol_names: list[str] = field(default_factory=list)
    # Sprint C1 — call-graph reach enrichment.
    # ``reach_paths`` is ALWAYS a superset of ``[entry_point_file]``
    # when populated (it includes the entry plus BFS-reachable callees,
    # capped at flow_reach.DEFAULT_MAX_PATHS). Stage 5 prefers this
    # over the legacy single-path fallback when populated.
    # ``depth_reached`` records the BFS depth actually walked (1..N).
    reach_paths: tuple[str, ...] = ()
    depth_reached: int = 0
    # Sprint C2 — per-flow line-range symbol attribution. Built by
    # ``_enrich_flow_symbols`` post-pass after reach is populated.
    # ``entry_detection_failed`` propagates upward into telemetry.
    symbol_attributions: tuple[Any, ...] = ()  # tuple[FlowSymbolAttribution, ...]
    entry_detection_failed: bool = False


@dataclass
class FeatureWithFlows:
    """A :class:`DeveloperFeature` plus its detected flows.

    Stage 3 returns a list of these; the caller wires them through to
    Stage 5/7 which converts them into :class:`Feature` records.
    """

    feature: "DeveloperFeature"
    flows: list[FlowSpec]
    rationale: str = ""


@dataclass
class Stage3Result:
    """Aggregate output of :func:`stage_3_flows`."""

    features_with_flows: list[FeatureWithFlows]
    cost_usd: float
    llm_calls: int
    warnings: list[str] = field(default_factory=list)
    # Sprint C1 — call-graph reach telemetry. Folded into scan_meta.
    reach_telemetry: dict[str, Any] = field(default_factory=dict)


# ── Anthropic client protocol (for tests) ──────────────────────────────────


class _AnthropicLike:
    """Duck-typed protocol for the parts of the Anthropic client we
    use, so tests can pass a fake without importing ``anthropic``.
    """

    class _Messages:
        def create(self, **kwargs: Any) -> Any:  # pragma: no cover - protocol
            raise NotImplementedError

    messages: _Messages


# ── Prompt builders ────────────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You are a flow detector. Given a feature's exported functions, "
    "route handlers, and sample paths, return the user-action flows "
    "that this feature implements. Output STRICT JSON only — no prose, "
    "no markdown fences, no commentary.\n\n"
    "Schema: {\"flows\": [{\"name\": \"<kebab-verb-phrase>-flow\", "
    "\"description\": \"<one sentence>\", \"symbols\": [\"<exported "
    "symbol the flow corresponds to>\"]}]}\n\n"
    "Rules:\n"
    "- Names MUST be kebab-case, MUST end in -flow, MUST start with a "
    "verb (e.g. \"create-monitor-flow\", \"manage-billing-flow\").\n"
    "- NEVER start a flow name with \"use-\" — that reads like a React "
    "hook, not a user action.\n"
    f"- Return between {MIN_FLOWS_PER_FEATURE_HINT} and "
    f"{MAX_FLOWS_PER_FEATURE} flows. Fewer is fine for small features.\n"
    "- Each flow's symbols MUST be a subset of the exports listed in the "
    "prompt. Do not invent symbol names.\n"
    "- If the feature has no real user-action flows, return {\"flows\": []}."
)


def _build_user_prompt(
    feature_name: str,
    paths: list[str],
    exports: list[str],
    routes: list[str],
) -> str:
    """Build the user-message payload for one feature."""
    paths_sample = paths[:MAX_PATHS_IN_PROMPT]
    exports_sample = exports[:MAX_EXPORTS_IN_PROMPT]
    routes_sample = routes[:MAX_ROUTES_IN_PROMPT]

    lines = [
        f"Feature slug: {feature_name}",
        "",
        "Sample paths:",
        *(f"  - {p}" for p in paths_sample),
    ]
    if exports_sample:
        lines.extend(["", "Exported symbols:",
                      *(f"  - {s}" for s in exports_sample)])
    if routes_sample:
        lines.extend(["", "Routes:", *(f"  - {r}" for r in routes_sample)])
    lines.extend(["", "Return JSON only."])
    return "\n".join(lines)


# ── Flow candidate enumeration from FileSignature ──────────────────────────


def _enumerate_candidates(
    feature: "DeveloperFeature",
    repo_path: str,
) -> tuple[list[str], list[str], dict[str, tuple[str, int]]]:
    """Walk a feature's paths via :func:`extract_signatures` and pull
    exports + routes + a (symbol → (file, start_line)) lookup map.

    Returns ``(exports, routes, symbol_to_loc)``. The lookup map is used
    after the LLM responds to re-attach line ranges deterministically.
    """
    sigs: dict[str, FileSignature] = extract_signatures(
        list(feature.paths), repo_path,
    )

    exports: list[str] = []
    routes: list[str] = []
    symbol_to_loc: dict[str, tuple[str, int]] = {}
    seen_exports: set[str] = set()

    for rel, sig in sigs.items():
        for sym in sig.exports:
            if sym in seen_exports:
                continue
            seen_exports.add(sym)
            exports.append(sym)
            # Pull start line from symbol_ranges when present.
            start_line = next(
                (sr.start_line for sr in sig.symbol_ranges if sr.name == sym),
                1,
            )
            symbol_to_loc[sym] = (rel, start_line)
        for r in sig.routes:
            if r not in routes:
                routes.append(r)

    return exports, routes, symbol_to_loc


# ── LLM invocation ─────────────────────────────────────────────────────────


def _parse_response_text(text: str) -> list[dict[str, Any]]:
    """Extract the ``flows`` array from a Haiku response.

    Tolerant of fenced JSON, leading prose, or trailing trailing
    whitespace. Returns an empty list on any parse failure.
    """
    if not text:
        return []
    s = text.strip()
    if s.startswith("```"):
        # Strip ```json ... ``` fences.
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        # Salvage attempt: find the first balanced ``{...}`` block.
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    flows = data.get("flows") if isinstance(data, dict) else None
    if not isinstance(flows, list):
        return []
    return [f for f in flows if isinstance(f, dict)]


def _validate_and_attach_lines(
    raw_flows: list[dict[str, Any]],
    symbol_to_loc: dict[str, tuple[str, int]],
) -> tuple[list[FlowSpec], list[str]]:
    """Filter naming-discipline violations + attach line ranges.

    Returns ``(valid_flows, drop_notes)``.
    """
    out: list[FlowSpec] = []
    notes: list[str] = []
    for raw in raw_flows:
        name = (raw.get("name") or "").strip().lower()
        if not name:
            notes.append("dropped flow with empty name")
            continue
        if not _KEBAB_SLUG_PATTERN.match(name):
            notes.append(f"dropped flow with non-kebab/-flow name: {name!r}")
            continue
        if _USE_PREFIX_PATTERN.match(name):
            notes.append(f"dropped use-prefixed flow: {name!r}")
            continue
        description = (raw.get("description") or "").strip()

        symbols_raw = raw.get("symbols") or []
        symbols = [
            s for s in symbols_raw
            if isinstance(s, str) and s in symbol_to_loc
        ]
        entry_file: str | None = None
        entry_line: int | None = None
        if symbols:
            entry_file, entry_line = symbol_to_loc[symbols[0]]

        out.append(
            FlowSpec(
                name=name,
                description=description,
                entry_point_file=entry_file,
                entry_point_line=entry_line,
                symbol_names=symbols,
            ),
        )
        if len(out) >= MAX_FLOWS_PER_FEATURE:
            break
    return out, notes


def _default_client_factory() -> Any | None:  # pragma: no cover — IO
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


def _call_haiku(
    client: Any,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
) -> tuple[str, int, int]:
    """Single Haiku call. Returns ``(text, in_tokens, out_tokens)``.

    On failure returns ``("", 0, 0)`` (caller decides what to do).
    """
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal, swallow
        logger.warning("stage_3_flows: Haiku call failed: %s", exc)
        return "", 0, 0

    # The Anthropic SDK returns ``content`` as a list of blocks.
    try:
        text_parts = []
        for block in msg.content:
            t = getattr(block, "text", None)
            if t:
                text_parts.append(t)
        text = "\n".join(text_parts)
    except Exception:  # noqa: BLE001
        text = ""

    in_tokens = int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0)
    out_tokens = int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)
    return text, in_tokens, out_tokens


# ── Public entry point ─────────────────────────────────────────────────────


def stage_3_flows(
    features: list["DeveloperFeature"],
    ctx: "ScanContext",
    *,
    model: str = DEFAULT_MODEL,
    max_workers: int = DEFAULT_MAX_WORKERS,
    timeout: float = DEFAULT_WALL_TIMEOUT_S,
    cost_tracker: CostTracker | None = None,
    client: Any | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> Stage3Result:
    """Detect user-action flows per developer feature, in parallel.

    Args:
        features: Stage 2 output.
        ctx: Stage 0 context (for ``repo_path``).
        model: Haiku model id. Default Haiku 4.5.
        max_workers: ThreadPool size. 8 is a good default for Haiku.
        timeout: Total wall-time cap, seconds. Features still pending
            when the timeout fires receive ``flows=[]`` + a warning.
        cost_tracker: optional shared tracker; one is created if None.
        client: optional preconstructed Anthropic-like client. Mostly
            useful for tests. Skips ``_client_factory`` when present.
        _client_factory: injection hook for the default client builder.

    Returns:
        :class:`Stage3Result` with the per-feature flow lists, total
        cost, and total LLM call count.
    """
    tracker = cost_tracker or CostTracker(max_cost=None)
    warnings: list[str] = []
    llm_calls = 0
    llm_call_lock = threading.Lock()

    # Build the client lazily; if it's None and there are features that
    # need detection, we record one warning and return empty flows for
    # all of them.
    if client is None:
        client = _client_factory()
    if client is None and any(
        len(_safe_exports(f, ctx)) >= MIN_EXPORTS_FOR_FLOW_DETECTION
        for f in features
    ):
        warnings.append(
            "no Anthropic client available; all features default to flows=[]"
        )
        return Stage3Result(
            features_with_flows=[
                FeatureWithFlows(feature=f, flows=[], rationale="no-client")
                for f in features
            ],
            cost_usd=0.0,
            llm_calls=0,
            warnings=warnings,
        )

    repo_path_str = str(ctx.repo_path)
    out: dict[int, FeatureWithFlows] = {}

    def _process(idx: int, feature: "DeveloperFeature") -> FeatureWithFlows:
        nonlocal llm_calls
        exports, routes, sym_loc = _enumerate_candidates(feature, repo_path_str)
        if len(exports) < MIN_EXPORTS_FOR_FLOW_DETECTION:
            return FeatureWithFlows(
                feature=feature,
                flows=[],
                rationale=f"skipped: {len(exports)} exports < "
                          f"{MIN_EXPORTS_FOR_FLOW_DETECTION}",
            )

        user_prompt = _build_user_prompt(
            feature.name, list(feature.paths), exports, routes,
        )
        text, in_tok, out_tok = _call_haiku(
            client,
            model=model,
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
        with llm_call_lock:
            llm_calls += 1
        if in_tok or out_tok:
            tracker.record(
                provider="anthropic",
                model=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                label="stage-3-flows",
            )
        if not text:
            return FeatureWithFlows(
                feature=feature, flows=[], rationale="llm-empty-or-failed",
            )

        raw_flows = _parse_response_text(text)
        valid, drop_notes = _validate_and_attach_lines(raw_flows, sym_loc)
        rationale = f"detected {len(valid)} flows"
        if drop_notes:
            rationale += f" ({'; '.join(drop_notes)})"
        return FeatureWithFlows(
            feature=feature, flows=valid, rationale=rationale,
        )

    if not features:
        return Stage3Result(
            features_with_flows=[], cost_usd=0.0,
            llm_calls=0, warnings=warnings,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(_process, idx, f): idx
            for idx, f in enumerate(features)
        }
        try:
            for fut in as_completed(future_to_idx, timeout=timeout):
                idx = future_to_idx[fut]
                try:
                    out[idx] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "stage_3_flows: feature %r raised: %s",
                        features[idx].name, exc,
                    )
                    out[idx] = FeatureWithFlows(
                        feature=features[idx],
                        flows=[],
                        rationale=f"error: {exc!r}",
                    )
        except TimeoutError:
            warnings.append(
                f"stage_3_flows wall-time {timeout}s exceeded; "
                f"{len(features) - len(out)} feature(s) defaulted to flows=[]"
            )

    # Fill in any features that didn't complete in time.
    ordered: list[FeatureWithFlows] = []
    for idx, f in enumerate(features):
        if idx in out:
            ordered.append(out[idx])
        else:
            ordered.append(
                FeatureWithFlows(
                    feature=f, flows=[], rationale="timed-out",
                ),
            )

    # ── Sprint C1 — deterministic call-graph reach enrichment ──────
    reach_telemetry, rctx = _enrich_flow_reach(ordered, ctx)
    # ── Sprint C2 — per-flow line-range symbol attribution ─────────
    symbol_telemetry = _enrich_flow_symbols(ordered, rctx)
    if symbol_telemetry:
        # Merge into reach_telemetry so a single dict carries the
        # Stage-3-deterministic enrichment surface.
        reach_telemetry = {**reach_telemetry, **symbol_telemetry}

    return Stage3Result(
        features_with_flows=ordered,
        cost_usd=tracker.total_cost_usd,
        llm_calls=llm_calls,
        warnings=warnings,
        reach_telemetry=reach_telemetry,
    )


# ── Sprint C1 — call-graph reach enrichment ────────────────────────────


def _enrich_flow_reach(
    features_with_flows: list[FeatureWithFlows],
    ctx: "ScanContext",
) -> tuple[dict[str, Any], Any]:
    """Populate ``FlowSpec.reach_paths`` + ``depth_reached`` in place.

    Builds a single :class:`ReachContext` for the whole scan, then
    runs BFS per flow. Pure deterministic (no LLM, no network). Caps:
    ``max_depth=3``, ``max_paths=8`` — see ``flow_reach.py`` docstring
    for rationale.

    Returns ``(telemetry_dict, reach_context)``. The reach_context is
    re-used by the Sprint C2 symbol-attribution post-pass; it may be
    ``None`` when construction fails or there are no flows.
    """
    # Local import keeps the legacy callers free of new transitive deps
    # if they construct Stage3Result manually for tests.
    from faultline.pipeline_v2.flow_reach import (
        DEFAULT_MAX_DEPTH,
        DEFAULT_MAX_PATHS,
        build_reach_context,
        compute_flow_reach,
    )

    flows_to_enrich = [
        flow
        for fwf in features_with_flows
        for flow in fwf.flows
        if flow.entry_point_file
    ]
    if not flows_to_enrich:
        return ({
            "stage_3_flow_reach_avg_paths": 0.0,
            "stage_3_flow_reach_max_paths": 0,
            "stage_3_flow_reach_p50_depth": 0,
            "stage_3_flow_reach_total_paths": 0,
            "stage_3_flow_reach_enriched_count": 0,
        }, None)

    try:
        rctx = build_reach_context(ctx)
    except Exception as exc:  # noqa: BLE001 — defensive, never break Stage 3
        logger.warning("flow_reach: build_reach_context failed: %s", exc)
        return ({
            "stage_3_flow_reach_avg_paths": 0.0,
            "stage_3_flow_reach_max_paths": 0,
            "stage_3_flow_reach_p50_depth": 0,
            "stage_3_flow_reach_total_paths": 0,
            "stage_3_flow_reach_enriched_count": 0,
        }, None)

    path_counts: list[int] = []
    depths: list[int] = []
    for flow in flows_to_enrich:
        # entry_point_file is guaranteed non-None by the filter above.
        try:
            reach = compute_flow_reach(
                rctx,
                flow.entry_point_file or "",
                flow.entry_point_line or 1,
                max_depth=DEFAULT_MAX_DEPTH,
                max_paths=DEFAULT_MAX_PATHS,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "flow_reach: compute failed for %s: %s", flow.name, exc,
            )
            # Fallback: single-path reach so downstream still sees something.
            flow.reach_paths = (flow.entry_point_file or "",)
            flow.depth_reached = 0
            path_counts.append(1)
            depths.append(0)
            continue
        flow.reach_paths = reach.reached_paths
        flow.depth_reached = reach.depth_reached
        path_counts.append(len(reach.reached_paths))
        depths.append(reach.depth_reached)

    n = len(path_counts)
    avg_paths = round(sum(path_counts) / n, 2) if n else 0.0
    max_paths = max(path_counts) if path_counts else 0
    depths_sorted = sorted(depths)
    p50_depth = depths_sorted[n // 2] if n else 0
    total_paths = sum(path_counts)

    return ({
        "stage_3_flow_reach_avg_paths": avg_paths,
        "stage_3_flow_reach_max_paths": max_paths,
        "stage_3_flow_reach_p50_depth": p50_depth,
        "stage_3_flow_reach_total_paths": total_paths,
        "stage_3_flow_reach_enriched_count": n,
    }, rctx)


# ── Sprint C2 — per-flow line-range symbol attribution ────────────────


def _enrich_flow_symbols(
    features_with_flows: list[FeatureWithFlows],
    rctx: Any | None,
) -> dict[str, Any]:
    """Populate ``FlowSpec.symbol_attributions`` in place.

    Reuses the :class:`ReachContext` built by :func:`_enrich_flow_reach`
    so we don't walk every file's signatures twice. When ``rctx`` is
    ``None`` (no flows OR reach context build failed earlier), returns
    an empty telemetry surface that the orchestrator treats as
    "feature disabled this scan".

    Telemetry shape — surfaced into ``scan_meta``:

      - ``stage_3_symbol_attributions_total``
      - ``stage_3_avg_symbols_per_flow``  — total / flows_enriched
      - ``stage_3_entry_detection_failure_rate``
      - ``stage_3_symbol_role_breakdown`` — ``{"entry": N, "called": N, "support": N}``
    """
    if rctx is None:
        return {
            "stage_3_symbol_attributions_total": 0,
            "stage_3_avg_symbols_per_flow": 0.0,
            "stage_3_entry_detection_failure_rate": 0.0,
            "stage_3_symbol_role_breakdown": {
                "entry": 0, "called": 0, "support": 0,
            },
        }

    # Local import so the test-suite stub-construction path (which
    # never hits run.py) doesn't pull in flow_symbols transitively.
    from faultline.pipeline_v2.flow_symbols import (
        DEFAULT_MAX_SYMBOLS_PER_FLOW,
        compute_flow_symbols,
    )

    flows_to_enrich = [
        flow
        for fwf in features_with_flows
        for flow in fwf.flows
        if flow.entry_point_file
    ]
    if not flows_to_enrich:
        return {
            "stage_3_symbol_attributions_total": 0,
            "stage_3_avg_symbols_per_flow": 0.0,
            "stage_3_entry_detection_failure_rate": 0.0,
            "stage_3_symbol_role_breakdown": {
                "entry": 0, "called": 0, "support": 0,
            },
        }

    role_counts = {"entry": 0, "called": 0, "support": 0}
    failure_count = 0
    total_attributions = 0

    for flow in flows_to_enrich:
        try:
            result = compute_flow_symbols(
                rctx,
                flow.entry_point_file or "",
                flow.entry_point_line or 1,
                tuple(flow.reach_paths),
                max_symbols_per_flow=DEFAULT_MAX_SYMBOLS_PER_FLOW,
            )
        except Exception as exc:  # noqa: BLE001 — never break Stage 3
            logger.debug(
                "flow_symbols: compute failed for %s: %s", flow.name, exc,
            )
            # Empty attribution; flag detection failure so telemetry
            # reflects reality.
            flow.symbol_attributions = ()
            flow.entry_detection_failed = True
            failure_count += 1
            continue
        flow.symbol_attributions = result.attributions
        flow.entry_detection_failed = result.entry_detection_failed
        total_attributions += len(result.attributions)
        if result.entry_detection_failed:
            failure_count += 1
        for attr in result.attributions:
            role_counts[attr.role] = role_counts.get(attr.role, 0) + 1

    n = len(flows_to_enrich)
    avg_symbols = round(total_attributions / n, 2) if n else 0.0
    failure_rate = round(failure_count / n, 4) if n else 0.0

    return {
        "stage_3_symbol_attributions_total": total_attributions,
        "stage_3_avg_symbols_per_flow": avg_symbols,
        "stage_3_entry_detection_failure_rate": failure_rate,
        "stage_3_symbol_role_breakdown": role_counts,
    }


def _safe_exports(feature: "DeveloperFeature", ctx: "ScanContext") -> list[str]:
    """Best-effort, side-effect-free export enumeration used by the
    pre-flight no-client gate. Returns an empty list on any failure.
    """
    try:
        exports, _, _ = _enumerate_candidates(feature, str(ctx.repo_path))
    except Exception:  # noqa: BLE001
        return []
    return exports


__all__ = [
    "FlowSpec",
    "FeatureWithFlows",
    "Stage3Result",
    "stage_3_flows",
]
