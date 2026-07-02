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

import hashlib
import json
import logging
import math
import os
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from faultline.analyzer.ast_extractor import (
    FileSignature,
    extract_signatures,
)
from faultline.cache.backend import CacheKind
from faultline.llm.cost import CostTracker, deterministic_params

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext
    from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature

logger = logging.getLogger(__name__)
from faultline.llm.model_gateway import resolve_model as gateway_model
from faultline.pipeline_v2.degradations import flow_walltime_exceeded
from faultline.pipeline_v2.llm_health import LlmHealth

# Stage-8.9 OVERSIZED contract — imported, not duplicated
# (rule-no-magic-tuning): a feature is oversized iff it owns
# > max(_OVERSIZED_MEDIAN_MULT * median, ceil(_OVERSIZED_SHARE * total))
# files, computed over the CURRENT feature set's owned sizes.
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    _MIN_DOMAINS,
    _OVERSIZED_MEDIAN_MULT,
    _OVERSIZED_SHARE,
)
from faultline.pipeline_v2.profiles._flow_lines import resolve_handler_line

if TYPE_CHECKING:
    from faultline.pipeline_v2.profiles.base import FrameworkProfile


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 2000
# ThreadPool size for the per-feature flow-detection LLM calls. Tunable via
# env so a deploy can raise concurrency WITHOUT an engine release — on giant
# repos (lobe-chat: 939 features) the per-call latency through the AI Gateway
# dominates wall-clock, and more parallelism cuts it. Bounded [1, 32] to avoid
# blowing the provider's rate limit. Default 8 (good for Haiku, direct).
DEFAULT_MAX_WORKERS = max(
    1, min(32, int(os.environ.get("FAULTLINES_STAGE3_MAX_WORKERS", "8") or "8"))
)
# Wall-time cap MUST scale with feature count. Old fixed 300s left
# chatwoot (330 features) and directus (242 features) with 189/231
# features defaulted to flows=[] because there literally weren't
# enough seconds for the 8-worker pool to drain the queue.
#
# Formula (scale-invariant per [[rule-no-magic-tuning]]):
#   timeout = max(MIN_WALL_TIMEOUT_S, ceil(N_features * PER_CALL_BUDGET_S / max_workers))
# with PER_CALL_BUDGET_S = 15s (Haiku 4.5 p99 latency observed across the
# 24-repo corpus) and MIN_WALL_TIMEOUT_S = 300s (small repos still get
# a generous floor). Caller can override via explicit `timeout=`.
MIN_WALL_TIMEOUT_S = 300
# Default stays 15s — the Haiku 4.5 p99 latency observed across the 24-repo
# corpus, the value production scans run at. The subscription-proxy DEV env
# observes much higher per-call latency (Max-subscription queueing + overload
# backoff), so dev sets FAULTLINE_FLOW_PER_CALL_BUDGET_S to size the wall-time
# to that latency and stop big repos silently defaulting to flows=[]. The env
# override is the only knob; the in-code default is unchanged from production.
PER_CALL_BUDGET_S = int(os.environ.get("FAULTLINE_FLOW_PER_CALL_BUDGET_S", "15"))
MIN_EXPORTS_FOR_FLOW_DETECTION = 3

# Features anchored by a DECLARED-route extractor are entry points by
# definition — the export-count heuristic misjudges them (a typical
# FastAPI/fastify route file has 1-2 exports, so before 2026-06-12 a
# repo like infisical silently got flows=[] on 340 of its 416 features
# and the dashboard lost flows + LOC entirely). Such features bypass
# the MIN_EXPORTS floor whenever they carry at least one export or one
# declared route. The floor still guards everything else (LLM cost).
_ROUTE_ANCHOR_SOURCES = frozenset({
    "route", "fastapi-route", "route-fastify", "route-express",
    "django-route", "rails-routes", "go-router", "mvc",
})


def _passes_flow_gate(feature, exports, routes) -> bool:
    if len(exports) >= MIN_EXPORTS_FOR_FLOW_DETECTION:
        return True
    if not (set(getattr(feature, "sources", ()) or ()) & _ROUTE_ANCHOR_SOURCES):
        return False
    return bool(exports) or bool(routes)


def _compute_wall_timeout(
    n_call_units: int,
    max_workers: int,
    max_units_one_feature: int = 1,
) -> int:
    """Universal wall-time cap that scales with LLM-call-unit count.

    ``n_call_units`` is the number of per-feature LLM calls the pool will
    issue: 1 per ordinary feature, plus the planned chunk count for each
    OVERSIZED (chunked) feature. ``max_units_one_feature`` is the largest
    unit count any single feature carries — a chunked feature runs its
    chunks SERIALLY inside one worker thread, so the wall-time can never be
    below that feature's own serial budget regardless of pool width.
    """
    if n_call_units <= 0 or max_workers <= 0:
        return MIN_WALL_TIMEOUT_S
    needed = math.ceil(n_call_units * PER_CALL_BUDGET_S / max_workers)
    needed = max(needed, max_units_one_feature * PER_CALL_BUDGET_S)
    return max(MIN_WALL_TIMEOUT_S, needed)

# Prompt sample caps — keep the request small.
MAX_PATHS_IN_PROMPT = 20
MAX_EXPORTS_IN_PROMPT = 30
MAX_ROUTES_IN_PROMPT = 20

# Output validation
MAX_FLOWS_PER_FEATURE = 12
MIN_FLOWS_PER_FEATURE_HINT = 3  # only a hint to the LLM; not enforced post-hoc

# ── Chunked flow detection for OVERSIZED features ───────────────────────────
#
# A giant feature (supabase "studio": 2000+ files, ~40 pages) previously got
# ONE Haiku call capped at MAX_FLOWS_PER_FEATURE=12 flows for dozens of real
# journeys — the golden journeys living in its subtrees were unreachable for
# the UF rollup. For features that pass the Stage-8.9 OVERSIZED contract
# (same constants, imported — rule-no-magic-tuning) we deterministically
# partition the feature's files by product-domain directory fan-out and run
# the EXISTING per-feature LLM machinery once per chunk:
#
#   * same prompt shape + naming discipline + per-call MAX_FLOWS cap;
#   * S7-B ``seen_entries`` SHARED across all chunks of one feature (no
#     same-entry twins across chunks);
#   * each chunk call content-hash cached via the SAME CacheKind.LLM_FLOWS
#     key derivation (the key hashes paths + exports + routes + content_sig,
#     so a chunk key — a proper subset of the feature's paths — can never
#     collide with a whole-feature key, and non-oversized features keep
#     byte-identical keys → warm caches still hit; no version bump needed).
#
# Flows therefore enter the pipeline at the normal Stage-3 point, so
# expansion / testmap / bipartite / rollup downstream just work.
#
# Kill switch: FAULTLINE_STAGE3_CHUNKED=0 restores the single-call path.
_CHUNKING_ENV = "FAULTLINE_STAGE3_CHUNKED"
_RESIDUAL_CHUNK = "__residual__"


def _chunking_enabled() -> bool:
    return (os.environ.get(_CHUNKING_ENV, "1") or "1").strip().lower() not in {
        "0", "false", "no", "off",
    }

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
    # Structured machine-readable degradation events (the typed sibling of
    # ``warnings``) — folded into ``scan_meta.degradations[]`` for the
    # worker / observability board. See :mod:`faultline.pipeline_v2.degradations`.
    degradations: list[dict[str, Any]] = field(default_factory=list)
    # Sprint C1 — call-graph reach telemetry. Folded into scan_meta.
    reach_telemetry: dict[str, Any] = field(default_factory=dict)
    # Warm-cache telemetry — per-feature flow-detection units served from
    # the content-hash cache (CacheKind.LLM_FLOWS) instead of a Haiku call.
    # ``cache_hits`` are NOT counted in ``llm_calls`` and cost $0. Mirrors
    # Stage 4's ``Stage4Result.cache_hits``.
    cache_hits: int = 0
    # Chunked oversized-feature flow-detection telemetry. Keys:
    #   features_chunked   — features routed through the chunked path
    #   chunks_total       — chunks planned across all chunked features
    #   chunk_llm_calls    — Haiku calls issued for chunks (subset of llm_calls)
    #   chunk_cache_hits   — chunk units served from cache (subset of cache_hits)
    #   flows_from_chunks  — validated flows produced by chunk calls
    chunk_telemetry: dict[str, int] = field(default_factory=dict)


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


# ── Flow-detection LLM cache (content-hash short-circuit) ───────────────────
#
# Each per-feature flow-detection LLM output is a pure function of its input:
#   (system prompt, feature slug, feature paths, extracted exports + routes,
#    per-file source-content signature, canonical model).
# We cache the PARSED ``flows[]`` array keyed on a sha256 of exactly those
# inputs so an unchanged feature never re-issues a Haiku call. This makes a
# re-scan of an unchanged repo REPLAY the identical flows → the downstream
# product_features[]/user_flows[] are reproducible (temp=0 on Anthropic is not
# bit-exact for complex generations, so re-running Stage 3 uncached was the
# reproducibility gap). Content-keyed (same input → same answer): a
# deterministic short-circuit, NOT per-repo memory — compliant with
# rule-cold-scan. Mirrors Stage 4's CacheKind.LLM_RESIDUAL cache.
#
# STAGE3_CACHE_VERSION is the manual invalidation lever required by
# rule-cache-invalidation: bump it whenever the prompt template, the parse
# logic, or the cached-value shape changes in a way that must NOT serve a
# stale answer. (The system-prompt text is ALSO hashed into the key, but the
# version constant is the documented, explicit control surface.)
STAGE3_CACHE_VERSION = "v1"


def _flow_cache_key(
    feature: "DeveloperFeature",
    *,
    model: str,
    system: str,
    exports: list[str],
    routes: list[str],
    content_sig: dict[str, str],
    paths: list[str] | None = None,
) -> str:
    """Content-hash key for one feature's flow-detection LLM call.

    Components (every input that affects the LLM's raw output):
      * ``STAGE3_CACHE_VERSION`` — manual invalidation lever.
      * canonical ``model`` id (pre-gateway, so the key is stable whether
        or not the AI-Gateway model shim is active).
      * the ``system`` prompt text (auto-invalidates on prompt edits).
      * ``feature.name`` — the slug the LLM is told to reason about.
      * ``feature.paths`` — SORTED so the key is stable regardless of the
        order Stage 2 happened to assemble the path tuple.
      * ``exports`` + ``routes`` — the extracted symbols/route signatures
        the prompt shows the LLM (deterministically ordered by
        :func:`_enumerate_candidates`).
      * ``content_sig`` — ``{path: sha256(source)}`` for every parsed file,
        so a byte-change to a member file misses the cache even when its
        exports/routes are unchanged.

    Deliberately EXCLUDED: run_id, timestamps, clone/job dir, feature
    ordering, thread identity, or any other run-varying value. The key is a
    pure function of the repo's content + the model + the prompt version.

    Chunked flow detection (oversized features) passes ``paths`` — the
    chunk's file subset — so a chunk's key hashes exactly the chunk's
    content (paths + exports + routes + content_sig all differ per chunk).
    ``paths=None`` (the whole-feature default) produces a payload that is
    BYTE-IDENTICAL to the pre-chunking derivation, so existing warm-cache
    entries for non-oversized features still hit, and no
    ``STAGE3_CACHE_VERSION`` bump is needed: a chunk is a PROPER SUBSET of
    its feature's paths (chunking requires ≥2 chunks), so a chunk key can
    never equal a stale whole-feature key for the same feature.
    """
    payload = json.dumps(
        {
            "version": STAGE3_CACHE_VERSION,
            "model": model,
            "system": system,
            "name": feature.name,
            "paths": sorted(paths if paths is not None else feature.paths),
            "exports": list(exports),
            "routes": list(routes),
            "content_sig": content_sig,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _flow_order_key(flow: "FlowSpec") -> tuple[str, int, str]:
    """Deterministic stable-sort key for a feature's final ``flows[]``.

    Belt-and-suspenders reproducibility: even on a COLD (cache-miss) scan —
    where the LLM's emission order is not guaranteed run-to-run — the final
    flow list is ordered by a pure content key ``(entry_point_file,
    entry_point_line, name)``. Applied AFTER detection/dedup/merge, so it
    only REORDERS the surviving flows; it never drops, adds, or re-selects
    any flow (flow DETECTION logic is untouched).
    """
    return (
        flow.entry_point_file or "",
        flow.entry_point_line or 0,
        flow.name,
    )


def _sorted_flows(flows: list["FlowSpec"]) -> list["FlowSpec"]:
    """Stable content-ordered copy of a feature's final flow list.

    Python's ``sorted`` is stable, so flows that tie on the content key keep
    their pre-sort relative order. Pure reordering — the returned list has
    exactly the same members as the input.
    """
    return sorted(flows, key=_flow_order_key)


# ── Oversized-feature chunk planning (deterministic) ───────────────────────


def _oversized_cut(features: list["DeveloperFeature"]) -> tuple[int, int] | None:
    """``(cut, median)`` per the Stage-8.9 oversized contract, computed over
    the CURRENT Stage-3 feature set's owned sizes (``len(f.paths)``).

    A feature is oversized iff ``len(f.paths) > cut``. Returns ``None`` when
    no feature owns any path (degenerate input). Mirrors
    ``stage_8_9_anchor_subdecompose.stage_8_9_anchor_subdecompose``'s
    median/cut computation exactly — the constants are imported from there so
    the two stages can never drift apart.
    """
    sizes = [len(f.paths) for f in features if f.paths]
    if not sizes:
        return None
    median = max(2, int(statistics.median(sizes)))
    total_owned = len({p for f in features for p in f.paths})
    cut = max(
        _OVERSIZED_MEDIAN_MULT * median,
        math.ceil(_OVERSIZED_SHARE * total_owned),
    )
    return cut, median


def _widest_fanout(paths: list[str]) -> tuple[str, dict[str, list[str]]] | None:
    """Locate the widest distinct-child directory level within *paths*.

    Every directory prefix of every path is a candidate CONTAINER; a child
    qualifies only when it is itself a directory (a deeper segment follows),
    so a bare file directly under the container never becomes a chunk. The
    container with the MOST distinct child dirs is the product-domain fan-out
    level (same idea as ``stage_8_9_5_llm_component_split._component_fanout``,
    generalised beyond ``components`` subtrees: the deeper-but-thinner
    grouping level loses to the wider product-area level).

    Deterministic: ties break to the shallower container, then lexicographic.
    Returns ``(container_prefix, {child_dir: [files]})`` — ``""`` container
    means the repo root — or ``None`` when no level fans out into at least
    ``_MIN_DOMAINS`` children (a fan-out of 1 is not a split, per Stage 8.9).
    """
    cand: dict[str, dict[str, list[str]]] = {}
    for p in sorted(paths):
        segs = p.split("/")
        # Child at index d is a directory iff it is not the basename
        # (index len(segs)-1) — i.e. d <= len(segs)-2, which is exactly
        # ``range(len(segs) - 1)``.
        for d in range(len(segs) - 1):
            container = "/".join(segs[:d])
            child = segs[d]
            cand.setdefault(container, {}).setdefault(child, []).append(p)
    if not cand:
        return None
    best = min(
        cand,
        key=lambda k: (-len(cand[k]), len(k.split("/")) if k else 0, k),
    )
    children = cand[best]
    if len(children) < _MIN_DOMAINS:
        return None
    return best, children


def _plan_chunks(
    paths: list[str], max_chunks: int,
) -> list[tuple[str, list[str]]] | None:
    """Deterministic chunk partition of an oversized feature's *paths*.

    Each child dir at the widest fan-out level becomes one chunk; files not
    under the fan-out (or directly at the container level) fall into a
    ``__residual__`` chunk. The number of chunks is bounded by *max_chunks*
    (see the call site for the scale-invariant formula): chunks are ordered
    largest-first (ties lexicographic) and the SMALLEST overflow chunks are
    merged into the residual — so a pathological 200-child fan-out coarsens
    gracefully instead of firing 200 LLM calls.

    File conservation: every input path lands in exactly one chunk. Returns
    ``None`` (caller falls back to the single-call path, byte-identical to
    the non-oversized behaviour) when no usable fan-out exists or fewer than
    2 chunks would result.
    """
    if max_chunks < 2:
        return None
    fan = _widest_fanout(paths)
    if fan is None:
        return None
    _container, children = fan
    buckets = {child: sorted(files) for child, files in children.items()}
    claimed = {p for files in buckets.values() for p in files}
    residual = sorted(p for p in paths if p not in claimed)
    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    # Reserve one slot for the residual chunk whenever one will exist.
    if residual or len(ordered) > max_chunks:
        keep_n = max_chunks - 1
    else:
        keep_n = max_chunks
    kept = ordered[:keep_n]
    overflow = [p for _name, files in ordered[keep_n:] for p in files]
    residual = sorted(residual + overflow)
    chunks: list[tuple[str, list[str]]] = [(name, files) for name, files in kept]
    if residual:
        chunks.append((_RESIDUAL_CHUNK, residual))
    if len(chunks) < 2:
        return None
    return chunks


# ── Flow candidate enumeration from FileSignature ──────────────────────────


def _enumerate_candidates(
    feature: "DeveloperFeature",
    repo_path: str,
) -> tuple[list[str], list[str], dict[str, tuple[str, int]], dict[str, str]]:
    """Whole-feature wrapper over :func:`_enumerate_candidates_paths` —
    byte-identical to the pre-chunking behaviour (same code path, same
    ordering)."""
    return _enumerate_candidates_paths(list(feature.paths), repo_path)


def _enumerate_candidates_paths(
    paths: list[str],
    repo_path: str,
) -> tuple[list[str], list[str], dict[str, tuple[str, int]], dict[str, str]]:
    """Walk *paths* via :func:`extract_signatures` and pull
    exports + routes + a (symbol → (file, start_line)) lookup map + a
    per-file source-content signature.

    Returns ``(exports, routes, symbol_to_loc, content_sig)``.

    * ``symbol_to_loc`` bridges LLM output → line ranges deterministically
      after the LLM responds.
    * ``content_sig`` maps ``rel_path → sha256(source)[:16]`` for every
      parsed file. It is a *content* signature — it changes iff a file's
      bytes change — and feeds the Stage 3 flow-detection cache key so an
      unchanged feature replays its cached flows and a changed file misses
      the cache (belt-and-suspenders beyond the exports/routes already in
      the prompt). Only parsed files (TS/JS/PY/Go/Rust/Ruby) appear, which
      is exactly the set that can influence the LLM's flow output.
    """
    sigs: dict[str, FileSignature] = extract_signatures(
        list(paths), repo_path,
    )

    exports: list[str] = []
    routes: list[str] = []
    symbol_to_loc: dict[str, tuple[str, int]] = {}
    content_sig: dict[str, str] = {}
    seen_exports: set[str] = set()

    for rel, sig in sigs.items():
        content_sig[rel] = hashlib.sha256(
            (sig.source or "").encode("utf-8", "ignore"),
        ).hexdigest()[:16]
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
            # Wrapped-handler fix (P4): when ``sym`` is a thin
            # higher-order wrapper export (``export const POST =
            # withAuth(handler)``) the start line points at the 2-LOC
            # wrapper, not the real handler body — the Formbricks
            # 449/449-flows-at-0-LOC bug. Resolve to the inner local
            # handler's definition line. No-op (identity) when ``sym`` is
            # not a wrapper, so non-wrapped flows are untouched.
            start_line = resolve_handler_line(sig, sym, start_line)
            symbol_to_loc[sym] = (rel, start_line)
        for r in sig.routes:
            if r not in routes:
                routes.append(r)

    return exports, routes, symbol_to_loc, content_sig


def _merge_seed_and_llm_flows(
    seeded: list[FlowSpec],
    llm_flows: list[FlowSpec],
) -> list[FlowSpec]:
    """AUGMENT the LLM's flows with a feature's profile seed (gap-fill).

    The LLM is the PRIMARY source of a flow's *name and description* — its
    semantic, capability-level names ("accept-invitation-flow",
    "freeze-dataroom-flow") are what downstream naming + user-flow recall
    are scored against, and they outperform the seed's mechanical,
    route-derived names ("delete-api-teams-teamid-saml-flow"). So on a
    collision the LLM flow WINS and the seed copy is dropped — but it is
    still exactly ONE flow per entry-point, so the dup-flow kill is
    preserved (two LLM variants of one page collapse against each other
    upstream; a seed copy of an LLM-detected page collapses here).

    The seed's value is COVERAGE, not naming: capabilities the LLM never
    surfaced (a filesystem route with no LLM-emitted flow) are real and
    must not be lost. We therefore:

      1. Keep ALL LLM flows (they win on name -> naming + UF recall).
      2. Append only the seeded flows whose ``(entry_point_file,
         entry_point_line)`` was NOT already produced by the LLM — the
         genuinely-additional deterministic coverage.

    This is the true AUGMENT: dedup direction is LLM-primary (recovers the
    pre-profile semantic names that the old seed-wins/skip-LLM gate
    regressed), seed-secondary (adds only the gaps). Line-span accuracy is
    handled uniformly downstream by the call-graph LOC stage (D1), so it
    is not a reason to prefer the seed copy here.

    Universal: the only key is the deterministic ``(file, line)`` pair
    that BOTH paths resolve through the SAME ``resolve_handler_line``
    helper — no profile name, no repo path, no magic number. Identical
    for every profile; never reached under the DefaultProfile (which
    seeds nothing, so this function is never called with a non-empty
    ``seeded``).
    """
    if not seeded:
        return llm_flows
    llm_keys: set[tuple[str, int]] = {
        (f.entry_point_file, f.entry_point_line)
        for f in llm_flows
        if f.entry_point_file is not None and f.entry_point_line is not None
    }
    merged: list[FlowSpec] = list(llm_flows)
    for flow in seeded:
        if (
            flow.entry_point_file is not None
            and flow.entry_point_line is not None
            and (flow.entry_point_file, flow.entry_point_line) in llm_keys
        ):
            # The LLM already produced a flow for this entry-point — its
            # semantic name wins; drop the seed copy (the dup-flow kill).
            continue
        # Seed-only capability (no LLM flow at this entry-point) OR a seed
        # flow without an entry-point key (cannot be deduped) — append it
        # as genuinely-additional deterministic coverage.
        merged.append(flow)
    return merged


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
    *,
    seen_entries: set[tuple[str, int]] | None = None,
) -> tuple[list[FlowSpec], list[str]]:
    """Filter naming-discipline violations + attach line ranges.

    Returns ``(valid_flows, drop_notes)``.

    ``seen_entries`` (optional) is the S7-B entry-point dedup set. When the
    caller passes one — the chunked oversized-feature path shares ONE set
    across all chunks of a feature — flows colliding with entries kept by an
    earlier chunk are dropped too (no same-entry twins across chunks). The
    default ``None`` builds a fresh per-call set: byte-identical to the
    pre-chunking single-call behaviour.

    Sprint S7-B — Stage 3 entry-point dedup
    ----------------------------------------
    After name validation we collapse flows that share the SAME
    ``(entry_point_file, entry_point_line)``. The LLM occasionally
    hallucinates 5–9 distinct flow names from a single endpoint (e.g.
    one ``route.ts`` exporting ``GET`` → "Configure SAML",
    "Manage Invites", "Manage Billing", "View Invoices", ...). These
    are the same flow under different names and confuse downstream
    rollup + landing UI. The first surviving flow per entry-key wins
    (most-specific symbol-list first because the LLM tends to emit the
    primary flow first).
    """
    out: list[FlowSpec] = []
    notes: list[str] = []
    if seen_entries is None:
        seen_entries = set()
    dup_count = 0
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

        # Sprint S7-B: collapse duplicates at same entry-point. Only
        # dedups when BOTH entry_file AND entry_line are populated —
        # flows without an entry-point are kept as-is (they have no
        # collision key).
        if entry_file is not None and entry_line is not None:
            key = (entry_file, entry_line)
            if key in seen_entries:
                dup_count += 1
                continue
            seen_entries.add(key)

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
    if dup_count:
        notes.append(
            f"deduped {dup_count} flow(s) sharing entry-point with a "
            "previously kept flow"
        )
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
    llm_health: LlmHealth | None = None,
) -> tuple[str, int, int]:
    """Single Haiku call. Returns ``(text, in_tokens, out_tokens)``.

    On failure returns ``("", 0, 0)`` (caller decides what to do).
    Consults the shared :class:`LlmHealth`: once any stage has hit an
    auth-class error the call is skipped entirely (dead key — no point
    firing hundreds more doomed requests).
    """
    if llm_health is not None and not llm_health.should_call():
        return "", 0, 0
    try:
        msg = client.messages.create(
            model=gateway_model(model),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal, swallow
        if llm_health is not None and llm_health.record_failure(
            exc, stage="stage_3_flows",
        ):
            logger.error(
                "stage_3_flows: LLM authentication failed — skipping all "
                "remaining LLM calls this scan: %s", exc,
            )
        else:
            logger.warning("stage_3_flows: Haiku call failed: %s", exc)
        return "", 0, 0
    if llm_health is not None:
        llm_health.record_success()

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


# ── Profile-driven flow seeding (P4 framework-awareness) ───────────────────


def _slug_flow_name(kind: str, route: str, symbol: str, path: str) -> str:
    """Deterministic kebab ``*-flow`` name for a profile FlowEntry.

    Prefers the route pattern, then the symbol, then the file stem —
    so two entries describing the SAME capability (same route) collapse
    to one name (the reset-password-flow x17 fix is UPSTREAM: one entry
    per capability + one stable name, not downstream name-dedup).
    """
    import re as _re

    basis = route or symbol or path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    slug = _re.sub(r"[^a-z0-9]+", "-", basis.lower()).strip("-")
    slug = slug or "flow"
    if not slug.endswith("-flow"):
        slug = f"{slug}-flow"
    return slug


def _profile_flows_by_feature(
    profile: "FrameworkProfile | None",
    ctx: "ScanContext",
    features: list["DeveloperFeature"],
) -> dict[str, list[FlowSpec]]:
    """Seed deterministic flows from ``profile.flow_entries`` per feature.

    Returns ``{feature_name: [FlowSpec, ...]}``. Empty for the
    DefaultProfile / None (``flow_entries`` returns ``[]``), so the
    LLM path is fully in charge and behaviour is unchanged.

    DEDUP DISCIPLINE: entries are collapsed to ONE flow per
    ``(owning_feature, flow_name)`` — the profile's ``flow_entries`` +
    naming are the source of truth for "this is the same capability".
    This kills duplicate flows (e.g. reset-password-flow x17) at the
    SOURCE rather than via a downstream name-dedup pass.

    Line ranges resolve to the REAL handler body via
    :func:`resolve_handler_line` using ``FlowEntry.symbol`` — fixing the
    wrapped-handler 0-LOC bug for the deterministic path too.
    """
    from faultline.pipeline_v2.profiles._attribution import is_active

    if not is_active(profile):
        return {}
    assert profile is not None
    entries = profile.flow_entries(ctx)
    if not entries:
        return {}

    # path -> owning feature name (primary attribution from Stage 2/2.6).
    owner_by_path: dict[str, str] = {}
    for f in features:
        for p in f.paths:
            owner_by_path.setdefault(p, f.name)

    repo_path_str = str(ctx.repo_path)
    # Cache FileSignature per entry path so we resolve lines once.
    sig_cache: dict[str, FileSignature] = {}

    out: dict[str, list[FlowSpec]] = {}
    seen: set[tuple[str, str]] = set()  # (feature, flow_name)
    for entry in entries:
        owner = owner_by_path.get(entry.path)
        if owner is None:
            # The profile named an entry whose file no feature owns —
            # leave it to the LLM/residual path; do not invent a flow.
            continue
        flow_name = _slug_flow_name(
            entry.kind, entry.route, entry.symbol, entry.path,
        )
        key = (owner, flow_name)
        if key in seen:
            continue  # one flow per capability per feature
        seen.add(key)

        entry_line: int | None = None
        if entry.symbol:
            if entry.path not in sig_cache:
                sigs = extract_signatures([entry.path], repo_path_str)
                sig = sigs.get(entry.path)
                if sig is not None:
                    sig_cache[entry.path] = sig
            sig = sig_cache.get(entry.path)
            if sig is not None:
                start = next(
                    (r.start_line for r in sig.symbol_ranges
                     if r.name == entry.symbol),
                    None,
                )
                if start is not None:
                    entry_line = resolve_handler_line(sig, entry.symbol, start)

        out.setdefault(owner, []).append(
            FlowSpec(
                name=flow_name,
                description=entry.route or entry.kind or "",
                entry_point_file=entry.path,
                entry_point_line=entry_line,
                symbol_names=[entry.symbol] if entry.symbol else [],
            ),
        )
    return out


# ── Public entry point ─────────────────────────────────────────────────────


def stage_3_flows(
    features: list["DeveloperFeature"],
    ctx: "ScanContext",
    *,
    model: str = DEFAULT_MODEL,
    max_workers: int = DEFAULT_MAX_WORKERS,
    timeout: float | None = None,
    cost_tracker: CostTracker | None = None,
    client: Any | None = None,
    llm_health: LlmHealth | None = None,
    profile: "FrameworkProfile | None" = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> Stage3Result:
    """Detect user-action flows per developer feature, in parallel.

    Args:
        features: Stage 2 output.
        ctx: Stage 0 context (for ``repo_path``).
        model: Haiku model id. Default Haiku 4.5.
        max_workers: ThreadPool size. 8 is a good default for Haiku.
        timeout: Total wall-time cap, seconds. When ``None`` (default),
            computed dynamically via ``_compute_wall_timeout`` so big
            repos (300+ features) don't hit a static cap that scales
            below their feature count. Features still pending when the
            timeout fires receive ``flows=[]`` + a warning.
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
    degradations: list[dict[str, Any]] = []
    llm_calls = 0
    cache_hits = 0
    llm_call_lock = threading.Lock()
    cost_cap_warned = False

    # Content-hash flow cache (CacheKind.LLM_FLOWS). Defensive: a missing
    # backend behaves exactly as pre-cache (no short-circuit; every eligible
    # feature issues its Haiku call). Read once here so the per-feature
    # ``_process`` closure captures it. Mirrors Stage 4's ``ctx.cache_backend``
    # read.
    cache_backend = getattr(ctx, "cache_backend", None)

    # Profile-driven deterministic flow seeding (P4). Empty for the
    # DefaultProfile / None, so the LLM path stays fully in charge and
    # the legacy behaviour is byte-for-byte preserved (regression guard).
    profile_flows = _profile_flows_by_feature(profile, ctx, features)

    # Chunked flow detection for OVERSIZED features (Stage-8.9 contract,
    # constants imported — see module docs at ``_CHUNKING_ENV``). The cut
    # and median are computed ONCE over the CURRENT feature set and shared
    # by every worker. ``chunk_cut is None`` ⇒ chunking fully disabled and
    # every feature takes the pre-existing single-call path.
    chunk_cut: int | None = None
    chunk_median: int = 2
    if _chunking_enabled():
        _oc = _oversized_cut(features)
        if _oc is not None:
            chunk_cut, chunk_median = _oc
    chunk_stats: dict[str, int] = {
        "features_chunked": 0,
        "chunks_total": 0,
        "chunk_llm_calls": 0,
        "chunk_cache_hits": 0,
        "flows_from_chunks": 0,
    }
    # Pre-plan chunks ONCE for every oversized feature (deterministic, pure
    # path-string work — no IO). Keyed by object identity: the features list
    # is fixed for the lifetime of this call. Doing it up front (a) gives the
    # wall-time formula EXACT per-feature call-unit counts instead of the
    # loose ``paths // median`` upper bound, and (b) keeps worker threads
    # free of planning work. The chunk-call cap is SCALE-INVARIANT
    # (rule-no-magic-tuning): at ``len(paths) // median`` chunks, each chunk
    # averages at least one repo-median-feature worth of files — so the
    # LLM-call count is bounded by the number of median-grain features this
    # oversized feature is "worth" (the cost it would have incurred had
    # Stage 2 emitted it at the repo's own grain). A pathological 200-child
    # fan-out on a feature worth 10 median features fires ≤10 calls:
    # ``_plan_chunks`` merges the smallest children into the residual chunk.
    chunk_plans: dict[int, list[tuple[str, list[str]]]] = {}
    if chunk_cut is not None:
        for f in features:
            if len(f.paths) > chunk_cut:
                plan = _plan_chunks(
                    sorted(f.paths),
                    max_chunks=len(f.paths) // chunk_median,
                )
                if plan is not None:
                    chunk_plans[id(f)] = plan

    # Scale wall-time cap to LLM-call-unit count when caller didn't pin it:
    # 1 unit per ordinary feature, the EXACT planned chunk count per chunked
    # oversized feature (its chunks run serially inside one worker thread).
    # With chunking disabled ``chunk_plans`` is empty and every feature is
    # exactly 1 unit — identical to the pre-chunking formula.
    if timeout is not None:
        effective_timeout: float = timeout
    else:
        call_units = 0
        max_units_one = 1
        for f in features:
            units = len(chunk_plans.get(id(f), ())) or 1
            call_units += units
            max_units_one = max(max_units_one, units)
        effective_timeout = _compute_wall_timeout(
            call_units, max_workers, max_units_one,
        )

    # Build the client lazily; if it's None and there are features that
    # need detection, we record one warning and return empty flows for
    # all of them.
    if client is None:
        client = _client_factory()
    # Features the LLM would inspect. D2: profile-seeded features are NOW
    # also LLM-eligible — the seed AUGMENTS the LLM (merge+dedup) rather
    # than replacing it, so we never skip the LLM just because a seed
    # exists. A feature only skips the LLM when it has neither a seed nor
    # enough structural surface; seeded-but-LLM-eligible features both run
    # the LLM AND keep their seed (merged downstream). The no-client
    # short-circuit below still preserves seeds with zero LLM cost.
    needs_llm = [
        f for f in features
        if len(_safe_exports(f, ctx)) >= MIN_EXPORTS_FOR_FLOW_DETECTION
        or (set(getattr(f, "sources", ()) or ()) & _ROUTE_ANCHOR_SOURCES)
    ]
    if client is None and needs_llm:
        if profile_flows:
            # Some features are profile-seeded (no client needed); only the
            # LLM-needing remainder default to flows=[].
            warnings.append(
                "no Anthropic client available; non-profile-seeded features "
                "default to flows=[]"
            )
            return Stage3Result(
                features_with_flows=[
                    FeatureWithFlows(
                        feature=f,
                        flows=list(profile_flows[f.name]),
                        rationale=f"profile-seeded {len(profile_flows[f.name])} "
                                  f"flow(s) (profile="
                                  f"{getattr(profile, 'name', 'default')})",
                    )
                    if profile_flows.get(f.name)
                    else FeatureWithFlows(feature=f, flows=[], rationale="no-client")
                    for f in features
                ],
                cost_usd=0.0,
                llm_calls=0,
                warnings=warnings,
            )
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

    def _process_chunked(
        feature: "DeveloperFeature",
        seeded: list[FlowSpec],
        chunks: list[tuple[str, list[str]]],
    ) -> FeatureWithFlows:
        """Chunked flow detection for one OVERSIZED feature.

        Runs the SAME per-feature LLM machinery once per chunk — same prompt
        shape, same naming discipline, same per-call
        :data:`MAX_FLOWS_PER_FEATURE` cap, same ``CacheKind.LLM_FLOWS``
        content-hash cache — with the chunk's file digest instead of the
        whole feature's. The S7-B entry dedup set is SHARED across all
        chunks (no same-entry twins across chunks), so the per-feature
        total is naturally bounded by ``MAX_FLOWS_PER_FEATURE *
        len(chunks)`` minus cross-chunk collisions. Chunk order is
        deterministic (largest-first from :func:`_plan_chunks`).
        """
        nonlocal llm_calls, cache_hits, cost_cap_warned
        with llm_call_lock:
            chunk_stats["features_chunked"] += 1
            chunk_stats["chunks_total"] += len(chunks)
        seen_entries: set[tuple[str, int]] = set()  # S7-B, shared over chunks
        all_flows: list[FlowSpec] = []
        notes: list[str] = []
        for label, chunk_paths in chunks:
            exports, routes, sym_loc, content_sig = (
                _enumerate_candidates_paths(chunk_paths, repo_path_str)
            )
            if not _passes_flow_gate(feature, exports, routes):
                continue
            # Budget guard — identical semantics to the single-call path,
            # applied per chunk so a mid-feature cap stops further calls.
            if (
                tracker.max_cost is not None
                and tracker.total_cost_usd >= tracker.max_cost
            ):
                with llm_call_lock:
                    if not cost_cap_warned:
                        cost_cap_warned = True
                        warnings.append(
                            f"stage_3_flows: shared cost cap "
                            f"${tracker.max_cost:.2f} hit; remaining "
                            f"features default to flows=[]",
                        )
                notes.append(f"cost-cap-hit at chunk {label!r}")
                break
            user_prompt = _build_user_prompt(
                feature.name, list(chunk_paths), exports, routes,
            )
            cache_key: str | None = None
            cached_raw: list[dict[str, Any]] | None = None
            if cache_backend is not None:
                cache_key = _flow_cache_key(
                    feature,
                    model=model,
                    system=_SYSTEM_PROMPT,
                    exports=exports,
                    routes=routes,
                    content_sig=content_sig,
                    paths=chunk_paths,
                )
                try:
                    stored = cache_backend.get(
                        CacheKind.LLM_FLOWS.value, cache_key,
                    )
                except Exception as exc:  # noqa: BLE001 — never break a scan
                    logger.warning("stage_3_flows: cache get failed: %s", exc)
                    stored = None
                if isinstance(stored, dict) and isinstance(
                    stored.get("flows"), list,
                ):
                    cached_raw = [
                        f for f in stored["flows"] if isinstance(f, dict)
                    ]
            if cached_raw is not None:
                with llm_call_lock:
                    cache_hits += 1
                    chunk_stats["chunk_cache_hits"] += 1
                raw_flows = cached_raw
            else:
                text, in_tok, out_tok = _call_haiku(
                    client,
                    model=model,
                    system=_SYSTEM_PROMPT,
                    user=user_prompt,
                    max_tokens=DEFAULT_MAX_TOKENS,
                    llm_health=llm_health,
                )
                with llm_call_lock:
                    llm_calls += 1
                    chunk_stats["chunk_llm_calls"] += 1
                if in_tok or out_tok:
                    tracker.record(
                        provider="anthropic",
                        model=model,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        label="stage-3-flows",
                    )
                if not text:
                    # Do NOT cache a failure (same rule as single-call path).
                    notes.append(f"chunk {label!r}: llm-empty-or-failed")
                    continue
                raw_flows = _parse_response_text(text)
                if cache_backend is not None and cache_key is not None:
                    try:
                        cache_backend.set(
                            CacheKind.LLM_FLOWS.value,
                            cache_key,
                            {
                                "version": STAGE3_CACHE_VERSION,
                                "flows": raw_flows,
                            },
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "stage_3_flows: cache set failed: %s", exc,
                        )
            valid, drop_notes = _validate_and_attach_lines(
                raw_flows, sym_loc, seen_entries=seen_entries,
            )
            all_flows.extend(valid)
            if drop_notes:
                notes.append(f"chunk {label!r}: {'; '.join(drop_notes)}")
        with llm_call_lock:
            chunk_stats["flows_from_chunks"] += len(all_flows)
        merged = _merge_seed_and_llm_flows(seeded, all_flows)
        rationale = (
            f"chunked oversized feature into {len(chunks)} chunk(s); "
            f"detected {len(all_flows)} flow(s)"
        )
        if seeded:
            rationale += (
                f"; merged with {len(seeded)} profile-seeded "
                f"-> {len(merged)} flow(s)"
            )
        if notes:
            rationale += f" ({'; '.join(notes)})"
        return FeatureWithFlows(
            feature=feature, flows=_sorted_flows(merged), rationale=rationale,
        )

    def _process(idx: int, feature: "DeveloperFeature") -> FeatureWithFlows:
        nonlocal llm_calls, cache_hits, cost_cap_warned
        # D2: the profile seed AUGMENTS the LLM, it does NOT replace it.
        # When the active profile seeded flows for this feature we still
        # run the LLM and MERGE (seed + LLM, deduped on entry-point) so
        # the dup-flow kill comes from the merge — not from suppressing
        # the richer LLM flows. ``seeded`` is empty under the
        # DefaultProfile, so the legacy LLM-only path is byte-identical.
        seeded = profile_flows.get(feature.name) or []

        # OVERSIZED features route through chunked detection so their flow
        # yield is no longer capped at one MAX_FLOWS_PER_FEATURE call. Plans
        # are precomputed (see ``chunk_plans`` above for the scale-invariant
        # cap rationale); a feature without a usable fan-out has no plan and
        # falls through to the byte-identical single-call path below.
        chunks = chunk_plans.get(id(feature))
        if chunks is not None and client is not None:
            return _process_chunked(feature, seeded, chunks)

        exports, routes, sym_loc, content_sig = _enumerate_candidates(
            feature, repo_path_str,
        )
        if not _passes_flow_gate(feature, exports, routes):
            # No LLM surface. If the profile seeded flows we still emit
            # them (deterministic, authoritative); otherwise flows=[].
            if seeded:
                return FeatureWithFlows(
                    feature=feature,
                    flows=_sorted_flows(list(seeded)),
                    rationale=f"profile-seeded {len(seeded)} flow(s), "
                              f"no LLM surface "
                              f"(profile={getattr(profile, 'name', 'default')})",
                )
            return FeatureWithFlows(
                feature=feature,
                flows=[],
                rationale=f"skipped: {len(exports)} exports < "
                          f"{MIN_EXPORTS_FOR_FLOW_DETECTION} and no "
                          f"route anchor",
            )

        # Budget guard: a shared tracker may carry a scan-wide cost
        # cap (see Stage 4's identical guard). Already-submitted
        # threads may still complete their in-flight call; everything
        # not yet sent to the API degrades to flows=[].
        if (
            tracker.max_cost is not None
            and tracker.total_cost_usd >= tracker.max_cost
        ):
            with llm_call_lock:
                if not cost_cap_warned:
                    cost_cap_warned = True
                    warnings.append(
                        f"stage_3_flows: shared cost cap "
                        f"${tracker.max_cost:.2f} hit; remaining "
                        f"features default to flows=[]",
                    )
            # Seed survives a budget-cap degrade (deterministic, free).
            return FeatureWithFlows(
                feature=feature,
                flows=_sorted_flows(list(seeded)),
                rationale="cost-cap-hit"
                          + (f"; kept {len(seeded)} seeded flow(s)"
                             if seeded else ""),
            )

        user_prompt = _build_user_prompt(
            feature.name, list(feature.paths), exports, routes,
        )

        # ── Cache lookup (content-hash short-circuit) ──────────────────
        # The cached value is the PARSED ``flows[]`` array (not raw text /
        # tokens), so a hit replays byte-identically through the SAME
        # deterministic ``_validate_and_attach_lines`` + merge below — line
        # attribution is recomputed each run, so a cache hit never freezes
        # stale line numbers.
        cache_key: str | None = None
        cached_raw: list[dict[str, Any]] | None = None
        if cache_backend is not None:
            cache_key = _flow_cache_key(
                feature,
                model=model,
                system=_SYSTEM_PROMPT,
                exports=exports,
                routes=routes,
                content_sig=content_sig,
            )
            try:
                stored = cache_backend.get(CacheKind.LLM_FLOWS.value, cache_key)
            except Exception as exc:  # noqa: BLE001 — cache must never break a scan
                logger.warning("stage_3_flows: cache get failed: %s", exc)
                stored = None
            if isinstance(stored, dict) and isinstance(stored.get("flows"), list):
                cached_raw = [f for f in stored["flows"] if isinstance(f, dict)]

        if cached_raw is not None:
            # HIT: no LLM call, no token cost recorded — reproducibility +
            # warm-cache token savings. ``raw_flows`` is byte-identical to
            # what a fresh ``_parse_response_text`` produced on the cold run.
            with llm_call_lock:
                cache_hits += 1
            raw_flows = cached_raw
        else:
            text, in_tok, out_tok = _call_haiku(
                client,
                model=model,
                system=_SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=DEFAULT_MAX_TOKENS,
                llm_health=llm_health,
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
                # LLM empty/failed — fall back to the seed (never lose it).
                # Do NOT cache a failure: only real responses are stored so a
                # transient outage never poisons future reproducible replays.
                return FeatureWithFlows(
                    feature=feature,
                    flows=_sorted_flows(list(seeded)),
                    rationale="llm-empty-or-failed"
                              + (f"; kept {len(seeded)} seeded flow(s)"
                                 if seeded else ""),
                )

            raw_flows = _parse_response_text(text)
            # MISS → persist the parsed flows for future runs. Store only the
            # deterministic parse output (no raw text / tokens / timestamps)
            # so a hit replays byte-identically downstream.
            if cache_backend is not None and cache_key is not None:
                try:
                    cache_backend.set(
                        CacheKind.LLM_FLOWS.value,
                        cache_key,
                        {"version": STAGE3_CACHE_VERSION, "flows": raw_flows},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("stage_3_flows: cache set failed: %s", exc)

        valid, drop_notes = _validate_and_attach_lines(raw_flows, sym_loc)
        # D2: AUGMENT — merge seed (source of truth) with LLM flows,
        # deduping LLM duplicates against the seed's entry-points. No-op
        # passthrough of ``valid`` when there is no seed (default path).
        merged = _merge_seed_and_llm_flows(seeded, valid)
        if seeded:
            rationale = (
                f"profile-seeded {len(seeded)} + LLM {len(valid)} "
                f"-> merged {len(merged)} flow(s)"
            )
        else:
            rationale = f"detected {len(valid)} flows"
        if drop_notes:
            rationale += f" ({'; '.join(drop_notes)})"
        # Deterministic final ordering (belt-and-suspenders reproducibility):
        # reorder AFTER merge/dedup so the surviving flow SET is unchanged.
        return FeatureWithFlows(
            feature=feature, flows=_sorted_flows(merged), rationale=rationale,
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
            for fut in as_completed(future_to_idx, timeout=effective_timeout):
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
            affected = len(features) - len(out)
            warnings.append(
                f"stage_3_flows wall-time {effective_timeout}s exceeded; "
                f"{affected} feature(s) defaulted to flows=[]"
            )
            degradations.append(
                flow_walltime_exceeded(
                    budget_s=int(effective_timeout),
                    affected=affected,
                    total=len(features),
                ),
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
        degradations=degradations,
        reach_telemetry=reach_telemetry,
        cache_hits=cache_hits,
        chunk_telemetry=dict(chunk_stats),
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
        exports, _, _, _ = _enumerate_candidates(feature, str(ctx.repo_path))
    except Exception:  # noqa: BLE001
        return []
    return exports


__all__ = [
    "STAGE3_CACHE_VERSION",
    "FlowSpec",
    "FeatureWithFlows",
    "Stage3Result",
    "stage_3_flows",
]
