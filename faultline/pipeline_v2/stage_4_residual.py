"""Stage 4 — residual LLM fallback (Haiku 4.5).

For files NOT attributed to any Stage 2 deterministic feature, group
them by STRUCTURAL signature (via
:func:`residual_clusterer.cluster_residual_paths`) and ask Haiku to
name the cluster — one call per cluster, NOT one call per fixed-size
chunk.

Sprint A2 — saturation-stop clustering
=====================================

Pre-A2 Stage 4 chunked the residual into 200-path slices and stopped
after 5 chunks. That hard cap silently lost the bulk of the residual
on large repos (infisical 7979 paths, supabase 8584). The fix is two
parts:

1. **Structural clustering** instead of chunking. Group residual paths
   by ``(top_level_dir, filename_suffix, extension, depth_bucket)`` —
   see :mod:`faultline.pipeline_v2.residual_clusterer`. Cost scales
   with structural diversity, not raw path count.

2. **Saturation stop** instead of fixed cap. After ``SAT_WINDOW``
   consecutive clusters that emit no NEW feature names, stop. The
   window length 3 is convention — "three sources of confirmation" —
   not a tuned threshold; do not change it without a corresponding
   architectural decision.

There is NO per-cluster size cap. Large clusters are exactly where
the LLM extracts value — let Haiku read the structural signature +
the evenly-spaced sample paths and produce a meaningful name.

Stage 4 is intentionally NOT a call to ``sonnet_scanner.deep_scan``:
that orchestration shell carries every legacy concern (workspace
splitting, chunking, retries, validation) that we have replaced with
the Stage 1–3 deterministic pipeline. Stage 4 is just "the LLM looks
at the leftovers and proposes names".
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from faultline.llm.cost import CostTracker, deterministic_params
from faultline.pipeline_v2.residual_clusterer import (
    ResidualCluster,
    cluster_residual_paths,
    synthesize_singleton_feature,
)
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature
from faultline.pipeline_v2.stage_4_guards import (
    DropEvent,
    apply_stage_4_guards,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 4_096
# Convention, not a tuning threshold: three consecutive saturated
# clusters is the minimum signal needed to declare "we've seen
# everything the LLM will tell us about this residual". Treat as a
# fixed shape of the algorithm — changing it would shift the
# stop-vs-explore trade-off in a way that needs its own design call.
SAT_WINDOW = 3
DEFAULT_COST_CAP_USD = 0.80

# Naming-discipline pattern matches the Stage 5 ``_slugify_names`` rule:
# starts with lowercase alnum, then lowercase alnum + hyphens. No
# slashes, dots, whitespace, or uppercase.
_KEBAB_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Reject single-segment kebab names that look like folder paths sliced
# out of repo layout (``app``, ``src``, ``lib``). These are the same
# vocabulary words the deterministic Stage 1/2 layer already de-duped;
# the LLM should not be allowed to reintroduce them as Layer-1 noise.
_REJECTED_GENERIC_NAMES = frozenset({
    "app", "apps", "src", "lib", "libs", "util", "utils", "common",
    "shared", "core", "base", "main", "index", "root",
    "components", "pages", "routes", "api", "server", "client",
    "frontend", "backend", "config", "configs", "test", "tests",
    "docs", "doc", "scripts", "build", "dist", "node-modules",
    "uncategorized", "misc",
})


# ── Output dataclass ───────────────────────────────────────────────────────


@dataclass
class Stage4Result:
    """Public output of :func:`stage_4_residual`.

    Attributes:
        residual_features: new ``low``-confidence DeveloperFeatures.
        cost_usd: total Haiku spend on this stage.
        llm_calls: number of completed Haiku calls (including no-ops).
        warnings: free-form telemetry for ``scan_meta.warnings``.
        clusters_total: clusters produced by the path clusterer.
        clusters_processed: NON-SINGLETON clusters actually sent to
            Haiku (singletons are handled deterministically and don't
            count here). May be less than the count of non-singleton
            clusters if saturation stopped early or the cost cap fired.
        saturation_stopped: True iff the loop exited because
            ``SAT_WINDOW`` consecutive empty clusters occurred.
        rejected_names: names the LLM proposed that we filtered out.
        singletons_synthesized: size-1 clusters that synthesized a
            deterministic feature (NOT an LLM call).
        singletons_skipped: size-1 clusters whose path was scaffolding
            (root dot-file, known manifest) — emitted no feature.
        cost_cap_hit: True iff Stage 4's local cost-cap fallback fired
            (only relevant when the shared CostTracker had
            ``max_cost=None``; otherwise the tracker's own cap fires
            first).
        guard_singletons_dropped: features dropped by Guard A
            (single-path phantoms with no product signal) — Sprint S2b.
        guard_incoherent_clusters_split: multi-path features that
            failed cohesion (mixed parent dirs AND top-2 segments)
            and were split into per-path singletons — Sprint S2b.
        guard_drops_sample: up to 5 ``{name, reason, path}`` records
            for telemetry / diagnostics.
    """

    residual_features: list[DeveloperFeature]
    cost_usd: float
    llm_calls: int
    warnings: list[str] = field(default_factory=list)
    clusters_total: int = 0
    clusters_processed: int = 0
    saturation_stopped: bool = False
    rejected_names: list[str] = field(default_factory=list)
    singletons_synthesized: int = 0
    singletons_skipped: int = 0
    cost_cap_hit: bool = False
    # Sprint S2b — structural guard telemetry.
    guard_singletons_dropped: int = 0
    guard_incoherent_clusters_split: int = 0
    guard_drops_sample: list[dict[str, str]] = field(default_factory=list)


# ── Prompt builders ────────────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You are a residual feature scanner. The deterministic extractors "
    "already mapped most files in this repo to features. The paths "
    "below were NOT claimed by any extractor — they are the residual.\n\n"
    "You will see ONE structurally-coherent cluster at a time: all "
    "members share the same top-level directory, file extension, and "
    "depth-band. Propose 1–5 developer features that describe what "
    "this cluster is.\n\n"
    "Output STRICT JSON only — no prose, no fences, no markdown. "
    "Schema: {\"features\": [{\"name\": \"<kebab-slug>\", "
    "\"paths\": [\"<rel/path/to/file>\", ...], "
    "\"confidence\": \"low\"}]}\n\n"
    "Rules:\n"
    "- Names MUST be kebab-case (^[a-z0-9][a-z0-9-]*$). NO slashes, "
    "dots, uppercase, or whitespace. NO single-word folder names like "
    "\"app\", \"src\", \"lib\", \"utils\", \"shared\" — these are "
    "structural, not features.\n"
    "- Each feature's ``paths`` MUST be a strict subset of the cluster "
    "members shown to you. Do NOT invent file paths.\n"
    "- If the cluster is too generic to name (e.g. shared test "
    "fixtures), return {\"features\": []}."
)


def _build_user_prompt(cluster: ResidualCluster, idx: int, total: int) -> str:
    # A2b: cluster key is a 3-tuple ``(top_level_dir, extension,
    # depth_bucket)``. ``filename_suffix`` was removed — it caused
    # cardinality explosion on JS monorepos. See clusterer docstring.
    top, ext, depth = cluster.key
    key_repr = (
        f"top_level_dir={top!r}, extension={ext!r}, "
        f"depth_bucket={depth!r}"
    )
    header = (
        f"Cluster {idx + 1} of {total}\n"
        f"Signature: {key_repr}\n"
        f"Total members in cluster: {cluster.size}\n"
        f"Representative sample ({len(cluster.sample_paths)} of {cluster.size}):"
    )
    body = "\n".join(f"  - {p}" for p in cluster.sample_paths)
    return f"{header}\n{body}\n\nReturn JSON only."


# ── LLM client wiring ──────────────────────────────────────────────────────


def _default_client_factory() -> Any | None:  # pragma: no cover - IO
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
    """One Haiku call. Returns ``(text, in_tokens, out_tokens)``.

    Empty string on failure; caller decides whether to skip the cluster.
    """
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage_4_residual: Haiku call failed: %s", exc)
        return "", 0, 0
    try:
        parts = [getattr(b, "text", "") for b in msg.content]
        text = "\n".join(p for p in parts if p)
    except Exception:  # noqa: BLE001
        text = ""
    in_t = int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0)
    out_t = int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)
    return text, in_t, out_t


# ── Response parsing ───────────────────────────────────────────────────────


def _parse_response(text: str) -> list[dict[str, Any]]:
    """Extract the ``features`` array from a Haiku response.

    Returns an empty list when parsing fails.
    """
    if not text:
        return []
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        return []
    return [f for f in feats if isinstance(f, dict)]


# ── Naming-discipline filter (mirrors Stage 5) ─────────────────────────────


def _is_acceptable_name(name: str) -> bool:
    if not name or not name.strip():
        return False
    if not _KEBAB_NAME_PATTERN.match(name):
        return False
    if name in _REJECTED_GENERIC_NAMES:
        return False
    return True


def _build_developer_features_for_cluster(
    raw: list[dict[str, Any]],
    allowed_paths: set[str],
    already_emitted_names: set[str],
) -> tuple[list[DeveloperFeature], list[str]]:
    """Build :class:`DeveloperFeature` records from one Haiku response.

    Returns ``(accepted, rejected_names)``.

    Filters:
      - bad name (naming-discipline)
      - paths must be a strict subset of ``allowed_paths`` (this
        cluster's members)
      - name already emitted by an earlier cluster — silently skipped
        (not surfaced as a rejection because it's not a quality issue)
      - features with no surviving paths after filtering
    """
    accepted: list[DeveloperFeature] = []
    rejected: list[str] = []
    seen_in_response: set[str] = set()

    for entry in raw:
        name_raw = (entry.get("name") or "").strip()
        if not _is_acceptable_name(name_raw):
            rejected.append(name_raw or "<empty>")
            continue
        name = name_raw
        if name in already_emitted_names or name in seen_in_response:
            continue
        raw_paths = entry.get("paths") or []
        if not isinstance(raw_paths, list):
            rejected.append(f"{name} (paths not a list)")
            continue
        paths = tuple(
            sorted({p for p in raw_paths if isinstance(p, str) and p in allowed_paths}),
        )
        if not paths:
            rejected.append(f"{name} (no valid paths)")
            continue
        seen_in_response.add(name)
        accepted.append(
            DeveloperFeature(
                name=name,
                paths=paths,
                sources=["llm-fallback"],
                confidence="low",
                rationale="stage-4-residual",
            ),
        )
    return accepted, rejected


# ── Public entry point ────────────────────────────────────────────────────


def stage_4_residual(
    unattributed_files: list[str],
    ctx: "ScanContext",
    existing_features: list[DeveloperFeature],
    *,
    model: str = DEFAULT_MODEL,
    cost_cap_usd: float = DEFAULT_COST_CAP_USD,
    cost_tracker: CostTracker | None = None,
    client: Any | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
    log: "StageLogger | None" = None,
) -> Stage4Result:
    """Run the cluster-then-saturate residual scanner.

    Args:
        unattributed_files: paths NOT claimed by any Stage 2 feature.
        ctx: Stage 0 context (kept for symmetry with sibling stages).
        existing_features: deterministic features from Stage 2 / Stage 3.
            Retained for signature symmetry — no longer drives a
            truncation step.
        model: Haiku model id.
        cost_cap_usd: hard USD budget; aborts further clusters when hit.
        cost_tracker: optional shared tracker. A new one is created
            with ``max_cost=cost_cap_usd`` if None.
        client: pre-built Anthropic client (testing hook).
        _client_factory: injection point for the default builder.
        log: optional :class:`StageLogger` for per-cluster ``cluster``
            events. The orchestrator passes one; tests don't have to.

    Returns:
        :class:`Stage4Result`.
    """
    # Sprint S2b — ``existing_features`` is now consumed by the structural
    # guards to compute the anchor-token overlap pool used by Guard A's
    # prong 2 (singleton-anchored admission).

    if not unattributed_files:
        return Stage4Result(
            residual_features=[],
            cost_usd=0.0,
            llm_calls=0,
            warnings=[],
            clusters_total=0,
            clusters_processed=0,
            saturation_stopped=False,
            rejected_names=[],
            singletons_synthesized=0,
            singletons_skipped=0,
            cost_cap_hit=False,
        )

    tracker = cost_tracker or CostTracker(max_cost=cost_cap_usd)

    clusters = cluster_residual_paths(unattributed_files)
    if log is not None:
        log.info(
            f"clustered {len(unattributed_files)} residual paths into "
            f"{len(clusters)} clusters",
        )

    # ── Pass 1: singletons → deterministic, no LLM ──────────────────
    # A2b: size-1 clusters are handled by ``synthesize_singleton_feature``.
    # This drops 30+ root configs on a typical monorepo and avoids
    # wasteful per-file Haiku calls. Non-singletons go to Pass 2.
    emitted_features: list[DeveloperFeature] = []
    emitted_names: set[str] = set()
    singletons_synth = 0
    singletons_skipped = 0
    llm_clusters: list[ResidualCluster] = []

    for cluster in clusters:
        if cluster.size == 1:
            path = cluster.paths[0]
            feat = synthesize_singleton_feature(path, ctx.repo_path)
            if feat is None:
                singletons_skipped += 1
                if log is not None:
                    log.drop(
                        feature=None,
                        reason=f"singleton_skipped:{path}",
                    )
                continue
            if feat.name in emitted_names:
                # Two synthesizable singletons collided on the same
                # synthesized name; the first wins, second drops.
                singletons_skipped += 1
                if log is not None:
                    log.drop(
                        feature=feat.name,
                        reason=f"singleton_dup:{path}",
                    )
                continue
            singletons_synth += 1
            emitted_features.append(feat)
            emitted_names.add(feat.name)
            if log is not None:
                log.emit(
                    feature=feat.name,
                    reason=f"singleton_synthesized:{path}",
                )
        else:
            llm_clusters.append(cluster)

    if log is not None:
        log.info(
            f"singletons: {singletons_synth} synthesized, "
            f"{singletons_skipped} skipped → {len(llm_clusters)} "
            f"clusters going to LLM",
        )

    # If no LLM-bound clusters remain (e.g. all-singleton residual),
    # skip the client setup entirely.
    warnings: list[str] = []
    rejected_names: list[str] = []
    sat_counter = 0
    llm_calls = 0
    clusters_processed = 0
    saturation_stopped = False
    cost_cap_hit = False

    if not llm_clusters:
        guarded = apply_stage_4_guards(emitted_features, existing_features)
        if log is not None and (
            guarded.singletons_dropped or guarded.incoherent_clusters_split
        ):
            log.info(
                f"guards: dropped={guarded.singletons_dropped} "
                f"split={guarded.incoherent_clusters_split} "
                f"(of {len(emitted_features)} residual features)",
            )
        return Stage4Result(
            residual_features=guarded.kept,
            cost_usd=tracker.total_cost_usd,
            llm_calls=0,
            warnings=warnings,
            clusters_total=len(clusters),
            clusters_processed=0,
            saturation_stopped=False,
            rejected_names=[],
            singletons_synthesized=singletons_synth,
            singletons_skipped=singletons_skipped,
            cost_cap_hit=False,
            guard_singletons_dropped=guarded.singletons_dropped,
            guard_incoherent_clusters_split=guarded.incoherent_clusters_split,
            guard_drops_sample=[
                {"name": d.name, "reason": d.reason, "path": d.path}
                for d in guarded.drops
            ],
        )

    if client is None:
        client = _client_factory()
    if client is None:
        guarded = apply_stage_4_guards(emitted_features, existing_features)
        return Stage4Result(
            residual_features=guarded.kept,
            cost_usd=tracker.total_cost_usd,
            llm_calls=0,
            warnings=["no Anthropic client; residual scan skipped"],
            clusters_total=len(clusters),
            clusters_processed=0,
            saturation_stopped=False,
            rejected_names=[],
            singletons_synthesized=singletons_synth,
            singletons_skipped=singletons_skipped,
            cost_cap_hit=False,
            guard_singletons_dropped=guarded.singletons_dropped,
            guard_incoherent_clusters_split=guarded.incoherent_clusters_split,
            guard_drops_sample=[
                {"name": d.name, "reason": d.reason, "path": d.path}
                for d in guarded.drops
            ],
        )

    # ── Pass 2: non-singleton clusters → LLM with saturation stop ──
    # ``stage4_cost_at_loop_start`` captures the tracker's cumulative
    # cost BEFORE Stage 4's LLM loop, so the fallback cap below
    # measures THIS stage's spend rather than the whole-scan total
    # (Stage 3's cost is already on the shared tracker).
    stage4_cost_at_loop_start = tracker.total_cost_usd

    for i, cluster in enumerate(llm_clusters):
        # Budget guard 1: shared tracker has its own cap → respect it.
        if (
            tracker.max_cost is not None
            and tracker.total_cost_usd >= tracker.max_cost
        ):
            cost_cap_hit = True
            warnings.append(
                f"stage_4_residual: shared cost cap "
                f"${tracker.max_cost:.2f} hit after {clusters_processed} "
                f"LLM clusters; {len(llm_clusters) - clusters_processed} "
                f"skipped",
            )
            break

        # Budget guard 2: when the shared tracker has no cap (the
        # orchestrator's default), fall back to Stage 4's local cap
        # so adversarial monorepos can't blow the budget.
        if tracker.max_cost is None:
            stage4_spend = tracker.total_cost_usd - stage4_cost_at_loop_start
            if stage4_spend >= cost_cap_usd:
                cost_cap_hit = True
                warnings.append(
                    f"stage_4_cost_cap_hit: ${stage4_spend:.2f} > "
                    f"${cost_cap_usd:.2f}; stopping after cluster "
                    f"{clusters_processed}/{len(llm_clusters)}",
                )
                if log is not None:
                    log.warn(warnings[-1])
                break

        prompt = _build_user_prompt(cluster, i, len(llm_clusters))
        text, in_t, out_t = _call_haiku(
            client,
            model=model,
            system=_SYSTEM_PROMPT,
            user=prompt,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
        llm_calls += 1
        if in_t or out_t:
            tracker.record(
                provider="anthropic",
                model=model,
                input_tokens=in_t,
                output_tokens=out_t,
                label="stage-4-residual",
            )
        clusters_processed += 1

        raw = _parse_response(text) if text else []
        cluster_member_set = set(cluster.paths)
        accepted, rejected = _build_developer_features_for_cluster(
            raw, cluster_member_set, emitted_names,
        )
        rejected_names.extend(rejected)
        new_names = [f.name for f in accepted]
        for f in accepted:
            emitted_names.add(f.name)
        emitted_features.extend(accepted)

        if log is not None:
            log.cluster(
                reason=(
                    f"cluster {i + 1}/{len(llm_clusters)} key={cluster.key} "
                    f"size={cluster.size} emit={len(accepted)} "
                    f"new={len(new_names)}"
                ),
            )

        if not new_names:
            sat_counter += 1
            if sat_counter >= SAT_WINDOW:
                saturation_stopped = True
                if log is not None:
                    log.info(
                        f"saturation: {SAT_WINDOW} consecutive clusters "
                        f"without new features → stopping after "
                        f"{clusters_processed}/{len(llm_clusters)} clusters",
                    )
                break
        else:
            sat_counter = 0

    # Sprint S2b — structural admission guards on the assembled
    # residual list (singleton-synth + LLM clusters together).
    guarded = apply_stage_4_guards(emitted_features, existing_features)
    if log is not None and (
        guarded.singletons_dropped or guarded.incoherent_clusters_split
    ):
        log.info(
            f"guards: dropped={guarded.singletons_dropped} "
            f"split={guarded.incoherent_clusters_split} "
            f"(of {len(emitted_features)} residual features)",
        )
    for d in guarded.drops:
        if log is not None:
            log.drop(d.name, f"{d.reason}:{d.path}")

    return Stage4Result(
        residual_features=guarded.kept,
        cost_usd=tracker.total_cost_usd,
        llm_calls=llm_calls,
        warnings=warnings,
        clusters_total=len(clusters),
        clusters_processed=clusters_processed,
        saturation_stopped=saturation_stopped,
        rejected_names=rejected_names,
        singletons_synthesized=singletons_synth,
        singletons_skipped=singletons_skipped,
        cost_cap_hit=cost_cap_hit,
        guard_singletons_dropped=guarded.singletons_dropped,
        guard_incoherent_clusters_split=guarded.incoherent_clusters_split,
        guard_drops_sample=[
            {"name": d.name, "reason": d.reason, "path": d.path}
            for d in guarded.drops
        ],
    )


__all__ = [
    "SAT_WINDOW",
    "Stage4Result",
    "stage_4_residual",
]
