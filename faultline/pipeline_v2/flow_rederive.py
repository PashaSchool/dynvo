"""B74 Seg B — Stage 6.865: post-grain flow re-derivation (form F3′).

The disease (twenty forensics 2026-07-19, probe-canon 2026-07-20):
capability-grain dev features BORN at Stage 8 decomposition
(``object-record-2`` 49K loc / ``activities`` / ``workflow-2``) — or
RE-MEMBERED after it (path-set ≠ the unit Stage 3 derived flows on) —
never get their flows re-derived, so the journey mint has no raw
material for the product core («Create a record» / «Manage tasks»).

Form F3′ (re-entry probe SHIP/high): births PRECEDE the 6.7-family
mint inside ``phase_finalize``, so no re-entry channel is needed —
this stage runs BETWEEN Stage 6.86 anchored-mint / lane-excavation and
the Stage 6.7 user-flow rollup, re-runs the EXISTING Stage-3 machinery
over the causal cohort, and writes the resulting flows into
``feature.flows[]`` + the Stage-5.5 bipartite mirror with Stage-3
writer identity. The single existing mint then sees them naturally —
ZERO new mint channels, zero dup-guards.

Cohort selector (probe-canon pt.1 — CAUSAL, not density-first):

* candidate iff its non-test path-set is NOT one of the Stage-3 units'
  path-sets (``stage3_unit_snapshot``, captured in ``run.py`` right
  after Stage 3):
  - name unknown at Stage 3            → **birth** (stage-8 family);
  - name known, path-set gained files  → **re-membered**;
  - name known, removal-only delta     → NOT causal (test/generated
    strips, donor sheds — the surviving flows were derived on a
    superset of the unit; grain did not change under them).
* product-layer twin rows are filtered out (``layer == "developer"``).
* flow-density (flows/KLOC < repo flowful median /
  ``_OVERSIZED_MEDIAN_MULT``) is a SECONDARY filter only: it EXCLUDES
  healthy-density features (probe: density-first over-fires — 717
  qualifiers / 22 repos incl. anti-cases). It never includes anything
  the causal gate did not select. HEALTH-scoped, not repo-scoped:
  papermark ``datarooms-2`` (6,210 loc / 0 flows) is the same disease
  and MUST fire (probe-canon pt.4).
* re-membered rows already AT the ``MAX_FLOWS_PER_FEATURE`` cap are
  excluded unless chunkable (their starvation may be cap-induced;
  probe-canon pt.5 tail).
* locale-births die on the EXISTING ``_passes_flow_gate`` MIN_EXPORTS
  gate — not duplicated here (probe-canon pt.4).

Chunk eligibility (probe-canon pt.2) is INDEPENDENT of the global
oversized cut: the ratio trigger ``len(exports) > MAX_EXPORTS_IN_PROMPT
∨ len(paths) > MAX_PATHS_IN_PROMPT`` routes a candidate through the
EXISTING ``_plan_chunks`` mechanism (otherwise the 1-call window is
blind on 2/3 of the twenty targets). The chunk-count cap keeps the
Stage-3 scale-invariant cost law (``len(paths) // repo-grain-median``).

Cache (probe-canon pt.3): the SAME ``CacheKind.LLM_FLOWS`` content-hash
key derivation as Stage 3 — a changed unit hashes to a fresh key by
construction (0% reuse on a NEW grain is legitimate; a re-scan of the
same grain replays its cached flows).

Keyless law: no Anthropic client → the deterministic part (cohort
selection + candidate enumeration + chunk planning) still runs and is
fully reported in telemetry; zero flows are derived (Stage-3 no-client
identity — the cache is deliberately NOT consulted either, mirroring
``stage_3_flows``).

Inertness law (Seg C precedent, openstatus): the telemetry key /
artifact appear ONLY when the causal gate selected at least one
candidate — an armed scan of a repo without post-Stage-3 grain changes
is byte-identical to unset.

Flag: ``FAULTLINE_FLOW_REDERIVE_POSTGRAIN`` — default **OFF**; unset /
``0`` keeps the stage un-entered, byte-identical. Registered in
``scan_result_cache.ENV_OUTPUT_FLAGS`` WITHOUT a KEY_SCHEMA bump (the
bump rides the separate default-flip commit — flip-protocol).
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from faultline.analyzer.validation import is_test_file
from faultline.cache.backend import CacheKind
from faultline.llm.cost import CostTracker
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.stage_3_flows import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_WORKERS,
    MAX_EXPORTS_IN_PROMPT,
    MAX_FLOWS_PER_FEATURE,
    MAX_PATHS_IN_PROMPT,
    STAGE3_CACHE_VERSION,
    FeatureWithFlows,
    FlowSpec,
    _build_user_prompt,
    _call_haiku,
    _compute_wall_timeout,
    _default_client_factory,
    _enrich_flow_reach,
    _enrich_flow_symbols,
    _enumerate_candidates_paths,
    _flow_cache_key,
    _parse_response_text,
    _passes_flow_gate,
    _plan_chunks,
    _sorted_flows,
    _validate_and_attach_lines,
)
from faultline.pipeline_v2.stage_3_flows import (
    _SYSTEM_PROMPT as _S3_SYSTEM_PROMPT,
)
from faultline.pipeline_v2.stage_5_5_bipartite import _flow_id
from faultline.pipeline_v2.stage_5_postprocess import _flow_spec_to_flow
from faultline.pipeline_v2.stage_6_97_feature_loc import count_file_loc
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    _OVERSIZED_MEDIAN_MULT,
)

logger = logging.getLogger(__name__)

#: B74 Seg B gate — default OFF (unset/``0`` ⇒ the stage is never
#: entered and the scan is byte-identical to main). Flip rides its own
#: later commit per flip-protocol.
FLOW_REDERIVE_ENV = "FAULTLINE_FLOW_REDERIVE_POSTGRAIN"

_TELEMETRY_SAMPLE_CAP = 25


def flow_rederive_enabled() -> bool:
    """Seg B — default **OFF** (unset/``0`` ⇒ byte-identical to main)."""
    return os.environ.get(FLOW_REDERIVE_ENV, "").strip().lower() in {
        "1", "true",
    }


# ── Cohort selection (deterministic, $0) ───────────────────────────────


@dataclass
class RederiveCandidate:
    """One causally-qualified cohort member (post secondary filters)."""

    feature: Any
    kind: str                     # "birth" | "re_membered"
    chunk_plan: list[tuple[str, list[str]]] | None = None
    # Whole-feature enumeration (computed once at selection time and
    # reused by the LLM pass for the single-call path).
    exports: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    symbol_to_loc: dict[str, tuple[str, int]] = field(default_factory=dict)
    content_sig: dict[str, str] = field(default_factory=dict)

    @property
    def call_units(self) -> int:
        return len(self.chunk_plan) if self.chunk_plan else 1


@dataclass
class CohortSelection:
    """Deterministic selection outcome — fully reported in telemetry."""

    candidates: list[RederiveCandidate] = field(default_factory=list)
    births: int = 0
    re_membered: int = 0
    excluded_healthy_density: int = 0
    excluded_flow_gate: int = 0
    excluded_at_cap: int = 0
    excluded_flow_gate_names: list[str] = field(default_factory=list)
    flowful_median_density: float = 0.0

    @property
    def causal_total(self) -> int:
        return self.births + self.re_membered


def _non_test_pathset(paths: Any) -> frozenset[str]:
    return frozenset(p for p in (paths or []) if p and not is_test_file(p))


def _feature_kloc(
    feature: Any, repo_path: Path, loc_cache: dict[str, int],
) -> float:
    """Owned non-test KLOC via the Stage-6.97 per-file counter.

    Feature.loc is only re-truthed at Stage 6.97 (AFTER this stage), so
    the density denominator is counted live with the same primitive.
    """
    total = 0
    for rel in sorted(_non_test_pathset(getattr(feature, "paths", None))):
        cached = loc_cache.get(rel)
        if cached is None:
            try:
                cached = count_file_loc(repo_path / rel, rel)
            except Exception:  # noqa: BLE001 — a bad file is 0 loc
                cached = 0
            loc_cache[rel] = cached
        total += cached
    return total / 1000.0


def _chunk_cap(n_paths: int, n_exports: int) -> int:
    """Prompt-window NEED — the re-derive chunk-count cap.

    The ratio trigger fires because the 1-call window is BLIND (probe:
    2/3 of the twenty targets invisible behind the prompt sample caps);
    the cure is exactly as many chunks as visibility requires —
    ``ceil(paths / MAX_PATHS_IN_PROMPT)`` / ``ceil(exports /
    MAX_EXPORTS_IN_PROMPT)`` — and not one more. Scale-invariant (a
    pure ratio of the feature's own surface to the prompt windows).
    The Stage-3 ``len(paths) // repo-median`` law does NOT transfer to
    this cohort: the stage-3 unit median on a monorepo is dominated by
    1-2-path extractor anchors (armed-keyless twenty census: 2,524
    call units under that law vs 43 for the probe's biggest target
    under this one). ``_plan_chunks`` merges overflow into the
    residual chunk, so the cap holds even on pathological fan-outs.
    """
    return max(
        2,
        math.ceil(n_paths / MAX_PATHS_IN_PROMPT),
        math.ceil(n_exports / MAX_EXPORTS_IN_PROMPT),
    )


def select_rederive_cohort(
    features: list[Any],
    stage3_unit_snapshot: dict[str, list[str]],
    ctx: Any,
) -> CohortSelection:
    """Causal cohort selection over the CURRENT (post-grain) board.

    Pure and $0 — runs identically keyed and keyless, so the armed
    keyless census judges exactly this function's output.
    """
    repo_path = Path(ctx.repo_path)
    sel = CohortSelection()

    snapshot_sets: dict[str, frozenset[str]] = {}
    all_unit_sets: set[frozenset[str]] = set()
    for name, paths in stage3_unit_snapshot.items():
        ps = _non_test_pathset(paths)
        snapshot_sets[name] = ps
        all_unit_sets.add(ps)

    dev_features = [
        f for f in features
        if getattr(f, "layer", "developer") == "developer"
    ]

    # Secondary filter ruler — flows/KLOC median over the repo's own
    # FLOWFUL dev features. No flowful features ⇒ no ruler ⇒ the
    # secondary filter abstains (it may only EXCLUDE, never include).
    loc_cache: dict[str, int] = {}
    densities: list[float] = []
    for f in sorted(dev_features, key=lambda x: x.name):
        flows_n = len(getattr(f, "flows", None) or [])
        if flows_n <= 0:
            continue
        kloc = _feature_kloc(f, repo_path, loc_cache)
        if kloc > 0:
            densities.append(flows_n / kloc)
    median_density = statistics.median(densities) if densities else 0.0
    sel.flowful_median_density = round(median_density, 4)
    density_ceiling = median_density / _OVERSIZED_MEDIAN_MULT


    for f in sorted(dev_features, key=lambda x: x.name):
        current = _non_test_pathset(getattr(f, "paths", None))
        if not current:
            continue
        if current in all_unit_sets:
            continue  # unchanged Stage-3 unit — flows already derived on it
        stage3_set = snapshot_sets.get(f.name)
        if stage3_set is not None:
            if current < stage3_set:
                # Removal-only delta (strips / donor shed) — the unit
                # only shrank under its surviving flows; not causal.
                continue
            kind = "re_membered"
            sel.re_membered += 1
        else:
            kind = "birth"
            sel.births += 1

        flows_n = len(getattr(f, "flows", None) or [])

        # Secondary filter: healthy flow-density never re-runs (the
        # anti-case law — zero extra LLM calls on healthy features).
        if median_density > 0 and flows_n > 0:
            kloc = _feature_kloc(f, repo_path, loc_cache)
            if kloc > 0 and (flows_n / kloc) >= density_ceiling:
                sel.excluded_healthy_density += 1
                continue

        # Existing MIN_EXPORTS flow gate (locale-birth killer) — the
        # SAME ``_passes_flow_gate`` Stage 3 applies, not a duplicate.
        exports, routes, sym_loc, content_sig = _enumerate_candidates_paths(
            sorted(f.paths), str(repo_path),
        )
        if not _passes_flow_gate(f, exports, routes):
            sel.excluded_flow_gate += 1
            if len(sel.excluded_flow_gate_names) < _TELEMETRY_SAMPLE_CAP:
                sel.excluded_flow_gate_names.append(f.name)
            continue

        # Chunk eligibility — ratio trigger, INDEPENDENT of the global
        # oversized cut (probe-canon pt.2); existing _plan_chunks
        # mechanism with the Stage-3 scale-invariant chunk-count cap.
        chunk_plan: list[tuple[str, list[str]]] | None = None
        if (
            len(exports) > MAX_EXPORTS_IN_PROMPT
            or len(current) > MAX_PATHS_IN_PROMPT
        ):
            chunk_plan = _plan_chunks(
                sorted(f.paths),
                max_chunks=_chunk_cap(len(f.paths), len(exports)),
            )

        # Re-membered rows already at the per-call flow cap: their
        # starvation may be cap-induced — exclude unless chunkable.
        if flows_n >= MAX_FLOWS_PER_FEATURE and chunk_plan is None:
            sel.excluded_at_cap += 1
            continue

        sel.candidates.append(RederiveCandidate(
            feature=f,
            kind=kind,
            chunk_plan=chunk_plan,
            exports=exports,
            routes=routes,
            symbol_to_loc=sym_loc,
            content_sig=content_sig,
        ))

    return sel


# ── LLM re-derivation (Stage-3 writer identity) ────────────────────────


def _derive_for_candidate(
    cand: RederiveCandidate,
    *,
    repo_path: str,
    client: Any,
    model: str,
    cache_backend: Any,
    tracker: CostTracker,
    llm_health: LlmHealth | None,
    counters: dict[str, int],
    lock: threading.Lock,
) -> list[FlowSpec]:
    """Run the Stage-3 per-feature machinery over one cohort member.

    Chunked candidates run their chunks SERIALLY inside one worker
    (Stage-3 identity); the S7-B ``seen_entries`` set is seeded with the
    feature's EXISTING flow entry-points so a re-derived twin of a
    surviving flow is dropped, never duplicated.
    """
    feature = cand.feature
    seen_entries: set[tuple[str, int]] = {
        (fl.entry_point_file, fl.entry_point_line)
        for fl in (getattr(feature, "flows", None) or [])
        if fl.entry_point_file and fl.entry_point_line
    }
    units: list[tuple[str | None, list[str] | None]]
    if cand.chunk_plan:
        units = [(label, paths) for label, paths in cand.chunk_plan]
    else:
        units = [(None, None)]  # whole-feature single call

    new_specs: list[FlowSpec] = []
    for _label, unit_paths in units:
        if unit_paths is None:
            exports = cand.exports
            routes = cand.routes
            sym_loc = cand.symbol_to_loc
            content_sig = cand.content_sig
            prompt_paths = sorted(feature.paths)
        else:
            exports, routes, sym_loc, content_sig = (
                _enumerate_candidates_paths(unit_paths, repo_path)
            )
            prompt_paths = list(unit_paths)
        if not exports and not routes:
            continue  # empty unit — nothing to prompt (Stage-3 identity)

        user_prompt = _build_user_prompt(
            feature.name, prompt_paths, exports, routes,
        )
        cache_key: str | None = None
        cached_raw: list[dict[str, Any]] | None = None
        if cache_backend is not None:
            cache_key = _flow_cache_key(
                feature,
                model=model,
                system=_S3_SYSTEM_PROMPT,
                exports=exports,
                routes=routes,
                content_sig=content_sig,
                paths=unit_paths,
            )
            try:
                stored = cache_backend.get(
                    CacheKind.LLM_FLOWS.value, cache_key,
                )
            except Exception as exc:  # noqa: BLE001 — never break a scan
                logger.warning("flow_rederive: cache get failed: %s", exc)
                stored = None
            if isinstance(stored, dict) and isinstance(
                stored.get("flows"), list,
            ):
                cached_raw = [
                    fl for fl in stored["flows"] if isinstance(fl, dict)
                ]
        if cached_raw is not None:
            with lock:
                counters["cache_hits"] += 1
            raw_flows = cached_raw
        else:
            text, in_tok, out_tok = _call_haiku(
                client,
                model=model,
                system=_S3_SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=DEFAULT_MAX_TOKENS,
                llm_health=llm_health,
            )
            with lock:
                counters["llm_calls"] += 1
            if in_tok or out_tok:
                tracker.record(
                    provider="anthropic",
                    model=model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    label="flow-rederive",
                )
            if not text:
                continue  # do NOT cache a failure (Stage-3 rule)
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
                        "flow_rederive: cache set failed: %s", exc,
                    )
        valid, _notes = _validate_and_attach_lines(
            raw_flows, sym_loc, seen_entries=seen_entries,
        )
        new_specs.extend(valid)
    return new_specs


def _attach_flows(
    fwfs: list[FeatureWithFlows],
    features: list[Any],
    bipartite_flows: list[Any],
    bipartite_edges: list[Any],
    ctx: Any,
    routes_index: list[dict[str, Any]] | None,
) -> int:
    """Write the re-derived flows with Stage-3.5/5.5 writer identity.

    ``feature.flows[]`` + the top-level bipartite mirror + typed edges;
    ids via ``_flow_id`` (deterministic ordinal on collision), uuids
    content-derived (lineage ``_mint_uuid`` precedent — uuid4 churns
    byte-identity). Each new row also runs the per-flow Stage-3.5
    expansion (``_expand_one_flow`` + LOC-detail projection) so it
    carries real ``line_ranges`` / ``nodes`` coordinates — without them
    the default-ON B71 flow-grain T1 law (empty span-set) would kill
    the row at the very rollup it feeds. The whole-scan fan-in Pass 2
    is NOT re-run (it would reshape EXISTING flows' roles — this stage
    is additive by contract). Existing rows are untouched; the
    top-level list is re-sorted by id, the Stage-5.5 stable-order
    invariant (existing rows are already id-sorted, so their relative
    order is preserved).
    """
    from faultline.models.types import FeatureFlowEdge
    from faultline.pipeline_v2.flow_expansion.expander import (
        DEFAULT_MAX_NODES_PER_FLOW,
        _expand_one_flow,
        _project_loc_detail,
        build_reach_context,
    )
    from faultline.pipeline_v2.shared_source import shared_reach_context

    rctx: Any | None = None
    if any(fwf.flows for fwf in fwfs):
        try:
            rctx = shared_reach_context(ctx) or build_reach_context(ctx)
        except Exception as exc:  # noqa: BLE001 — expansion is best-effort
            logger.warning(
                "flow_rederive: reach context build failed: %s", exc,
            )
            rctx = None
    routes = routes_index or []

    dev_features = [
        f for f in features
        if getattr(f, "layer", "developer") == "developer"
    ]
    path_index: dict[str, set[str]] = {}
    for f in dev_features:
        for p in f.paths or []:
            path_index.setdefault(p, set()).add(f.name)

    existing_ids: set[str] = {
        fl.id for fl in bipartite_flows if getattr(fl, "id", None)
    }
    existing_pathsets: list[set[str]] = [
        set(fl.paths or []) for fl in bipartite_flows
    ]

    new_rows: list[Any] = []
    new_edges: list[Any] = []
    flows_added = 0
    for fwf in sorted(fwfs, key=lambda x: x.feature.name):
        # FeatureWithFlows types ``feature`` as the Stage-2
        # DeveloperFeature; here it carries the public Feature model
        # (flows[] present) — the same duck-typing Stage 3's helpers use.
        feature: Any = fwf.feature
        for spec in _sorted_flows(fwf.flows):
            flow = _flow_spec_to_flow(spec)
            # Stage-3.5 per-flow expansion (writer identity): the live
            # site expands every flow at max_depth=1 ("entry + direct
            # callees" — see the phase_finalize Stage 3.5 rationale);
            # this populates entry/nodes/edges/summary + line_ranges,
            # the coordinates the flow-grain T1 law requires.
            if rctx is not None:
                try:
                    flow, _fx_tel = _expand_one_flow(
                        flow, rctx, routes,
                        max_depth=1,
                        max_nodes=DEFAULT_MAX_NODES_PER_FLOW,
                    )
                    _project_loc_detail(flow, routes)
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.warning(
                        "flow_rederive: expansion failed for %s: %s",
                        flow.name, exc,
                    )
            flow.primary_feature = feature.name
            base_id = _flow_id(feature.name, flow.name)
            fid = base_id
            ordinal = 2
            while fid in existing_ids:
                fid = f"{base_id}-{ordinal}"
                ordinal += 1
            flow.id = fid
            existing_ids.add(fid)
            flow.uuid = hashlib.sha256(
                f"rederive-v1|{feature.name}|{fid}".encode("utf-8"),
            ).hexdigest()[:32]

            secondaries = sorted({
                owner
                for p in (flow.paths or [])
                for owner in path_index.get(p, ())
                if owner != feature.name
            })
            flow.secondary_features = secondaries
            flow.shared_with_features_count = len(secondaries)
            flow.cross_cutting = bool(secondaries)

            new_edges.append(FeatureFlowEdge(
                feature=feature.name,
                flow_id=fid,
                type="primary",
                reason=None,
            ))
            for sec in secondaries:
                new_edges.append(FeatureFlowEdge(
                    feature=sec,
                    flow_id=fid,
                    type="secondary",
                    reason="path-overlap",
                ))

            feature.flows.append(flow)
            new_rows.append(flow)
            flows_added += 1

    # Blast-radius counter for the NEW rows only (existing rows keep
    # their Stage-5.5 numbers — this stage is additive by contract).
    all_pathsets = existing_pathsets + [
        set(fl.paths or []) for fl in new_rows
    ]
    offset = len(existing_pathsets)
    for i, fl in enumerate(new_rows):
        ps = all_pathsets[offset + i]
        if not ps:
            continue
        fl.shared_with_flows_count = sum(
            1
            for j, other in enumerate(all_pathsets)
            if j != offset + i and ps & other
        )

    bipartite_flows.extend(new_rows)
    bipartite_flows.sort(key=lambda fl: fl.id or "")
    bipartite_edges.extend(new_edges)
    return flows_added


# ── Stage entry ─────────────────────────────────────────────────────────


def run_flow_rederive(
    features: list[Any],
    bipartite_flows: list[Any],
    bipartite_edges: list[Any],
    ctx: Any,
    *,
    stage3_unit_snapshot: dict[str, list[str]] | None,
    model: str,
    routes_index: list[dict[str, Any]] | None = None,
    tracker: CostTracker | None = None,
    llm_health: LlmHealth | None = None,
    client: Any | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
    max_workers: int = DEFAULT_MAX_WORKERS,
    log: Any = None,
) -> dict[str, Any] | None:
    """Run the F3′ post-grain flow re-derivation. Mutates in place.

    Returns the telemetry dict when the causal gate FIRED (≥1 causal
    candidate — births + re-membered), else ``None`` (the Seg C
    inertness law: no scan_meta key, no artifact, byte-identical armed
    no-fire boards).
    """
    if stage3_unit_snapshot is None:
        logger.info("flow_rederive: no stage-3 unit snapshot — skipped")
        return None

    sel = select_rederive_cohort(features, stage3_unit_snapshot, ctx)
    if sel.causal_total == 0:
        return None  # inertness law — armed no-fire is byte-identical

    tracker = tracker or CostTracker(max_cost=None)
    cost_before = tracker.total_cost_usd
    telemetry: dict[str, Any] = {
        "applied": False,
        "births": sel.births,
        "re_membered": sel.re_membered,
        "candidates_causal": sel.causal_total,
        "excluded_healthy_density": sel.excluded_healthy_density,
        "excluded_flow_gate": sel.excluded_flow_gate,
        "excluded_flow_gate_sample": list(sel.excluded_flow_gate_names),
        "excluded_at_cap": sel.excluded_at_cap,
        "flowful_median_density": sel.flowful_median_density,
        "cohort_selected": len(sel.candidates),
        "cohort": [c.feature.name for c in sel.candidates],
        "cohort_kinds": {
            c.feature.name: c.kind for c in sel.candidates
        },
        "chunked_features": sum(
            1 for c in sel.candidates if c.chunk_plan
        ),
        "call_units": sum(c.call_units for c in sel.candidates),
        "llm_calls": 0,
        "cache_hits": 0,
        "flows_added": 0,
        "timed_out": 0,
        "no_client": False,
    }

    if not sel.candidates:
        return telemetry  # causal class present, all excluded — honest

    if client is None:
        client = _client_factory()
    if client is None:
        # Stage-3 no-client identity: the deterministic part (selection
        # + enumeration + chunk planning) is reported; no flows derive
        # (the cache is deliberately not consulted either).
        telemetry["no_client"] = True
        if log is not None:
            log.info(
                "flow_rederive: no Anthropic client — cohort of %d "
                "selected (%d call units), zero flows derived"
                % (len(sel.candidates), telemetry["call_units"]),
                feature=None,
            )
        return telemetry

    cache_backend = getattr(ctx, "cache_backend", None)
    repo_path = str(ctx.repo_path)
    counters = {"llm_calls": 0, "cache_hits": 0}
    lock = threading.Lock()

    ordered = sorted(sel.candidates, key=lambda c: c.feature.name)
    results: dict[int, list[FlowSpec]] = {}
    max_units_one = max(c.call_units for c in ordered)
    wall_timeout = _compute_wall_timeout(
        telemetry["call_units"], max_workers, max_units_one,
    )
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(
                _derive_for_candidate,
                cand,
                repo_path=repo_path,
                client=client,
                model=model,
                cache_backend=cache_backend,
                tracker=tracker,
                llm_health=llm_health,
                counters=counters,
                lock=lock,
            ): idx
            for idx, cand in enumerate(ordered)
        }
        try:
            for fut in as_completed(future_to_idx, timeout=wall_timeout):
                idx = future_to_idx[fut]
                try:
                    results[idx] = fut.result()
                except Exception as exc:  # noqa: BLE001 — never break a scan
                    logger.warning(
                        "flow_rederive: candidate %r raised: %s",
                        ordered[idx].feature.name, exc,
                    )
                    results[idx] = []
        except TimeoutError:
            telemetry["timed_out"] = len(ordered) - len(results)

    fwfs = [
        FeatureWithFlows(
            feature=ordered[idx].feature,
            flows=results.get(idx) or [],
            rationale="flow-rederive",
        )
        for idx in range(len(ordered))
        if results.get(idx)
    ]

    # Stage-3 deterministic enrichment identity — call-graph reach +
    # per-flow line-range symbol attribution on the NEW specs only.
    if fwfs:
        _reach_tele, rctx = _enrich_flow_reach(fwfs, ctx)
        _enrich_flow_symbols(fwfs, rctx)

    flows_added = _attach_flows(
        fwfs, features, bipartite_flows, bipartite_edges,
        ctx, routes_index,
    )
    telemetry["llm_calls"] = counters["llm_calls"]
    telemetry["cache_hits"] = counters["cache_hits"]
    telemetry["flows_added"] = flows_added
    telemetry["applied"] = flows_added > 0
    telemetry["cost_usd"] = round(tracker.total_cost_usd - cost_before, 4)
    if log is not None:
        log.info(
            "flow_rederive: cohort=%d (births=%d re_membered=%d causal) "
            "units=%d llm_calls=%d cache_hits=%d flows_added=%d"
            % (
                len(sel.candidates), sel.births, sel.re_membered,
                telemetry["call_units"], counters["llm_calls"],
                counters["cache_hits"], flows_added,
            ),
            feature=None,
        )
    return telemetry


__all__ = [
    "FLOW_REDERIVE_ENV",
    "CohortSelection",
    "RederiveCandidate",
    "flow_rederive_enabled",
    "run_flow_rederive",
    "select_rederive_cohort",
]
