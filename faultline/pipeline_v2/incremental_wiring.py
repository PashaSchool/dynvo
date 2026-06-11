"""--since incremental wiring for the pipeline-v2 orchestrator.

Extracted from ``run.py`` (refactor/run-decomposition). Each function
here owns one slice of the incremental (``--since``) orchestration that
used to live inline in ``run_pipeline_v2``:

  - :func:`run_incremental_gate` — the Stage 2.5 LLM gate (partition +
    rehydrate + residual filtering) that restricts Stage 3/4 to the
    changed set.
  - :func:`splice_untouched_features` — re-attach the re-hydrated
    untouched features after Stage 5.
  - :func:`is_layer2_noop` / :func:`reuse_base_layer2` — the no-op-diff
    heuristic that lets Stage 8 reuse the base scan's Layer 2 verbatim.
  - :func:`apply_incremental_bookkeeping` — head SHA + Stage 6 metric
    carry-forward for untouched features.
  - :func:`plan_uf_domain_allowlist` — Stage 6.7b per-domain reuse plan.

On a FULL / cold scan none of these run (except the head-SHA lookup) —
the whole-repo path stays byte-for-byte unchanged (cold-scan rule).
Inputs and outputs are explicit; the only side effects are the stage
log + artifact writes that the inline code already performed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2.stage_7_output import write_stage_artifact


@dataclass
class IncrementalGateOutcome:
    """Everything the Stage 2.5 gate hands back to the orchestrator.

    ``deterministic_features`` / ``unattributed`` REPLACE the Stage 2
    outputs from here on — Stage 3 + Stage 4 see only changed work
    (+ any untouched feature we could not re-hydrate).
    """

    deterministic_features: list[Any]
    unattributed: list[Any]
    untouched_features: list[Any]
    gate_meta: dict[str, Any]
    base_scan: dict[str, Any]


def run_incremental_gate(
    *,
    repo_path: Path,
    since: str | None,
    base_scan_path: Path | str | None,
    deterministic_features: list[Any],
    unattributed: list[Any],
    ctx: Any,
    run_dir: Path,
) -> IncrementalGateOutcome:
    """Stage 2.5 — incremental LLM gating (--since path ONLY).

    Restrict the expensive LLM stages (Stage 3 per-feature flows +
    Stage 4 per-cluster residual) to the files this diff touched.
    On a FULL / cold scan this is never called and the whole-repo path
    is byte-for-byte unchanged (cold-scan rule).

    See ``incremental_gate`` for the rationale: untouched features are
    re-hydrated from the base scan AFTER Stage 5 (their flows/metrics
    are already final there), so they never pay for Stage 3/4. This is
    Option A from ``finding-incremental-no-llm-savings`` — it turns a
    ~$0.24 PR scan into a ~$0.01-0.03 one without re-LLM-ing unchanged
    code.
    """
    from faultline.pipeline_v2.incremental import (
        load_base_scan as _load_base_scan_early,
    )
    from faultline.pipeline_v2.incremental_gate import (
        compute_changed_set,
        filter_unattributed,
        partition_features,
        rehydrate_untouched_features,
    )

    if base_scan_path is None:
        raise ValueError(
            "--since requires --base-scan-path (engine cannot gate "
            "LLM stages without a previous scan to reuse).",
        )
    incremental_base_scan = _load_base_scan_early(base_scan_path)
    changed_set = compute_changed_set(
        repo_path, since or "", incremental_base_scan,
    )
    with StageLogger(run_dir, 2, "incremental_gate") as log2_5:
        partition = partition_features(deterministic_features, changed_set)
        # Re-hydrate untouched features from the base scan NOW so we
        # know which ones have a base twin. Features with no base
        # match (``missing_names``) are routed BACK through Stage 3
        # rather than dropped — the silent-drop guard.
        rehydrate = rehydrate_untouched_features(
            partition.untouched, incremental_base_scan,
        )
        missing = set(rehydrate.missing_names)
        rescan_untouched = [
            f for f in partition.untouched if f.name in missing
        ]
        unattributed_pre = len(unattributed)
        unattributed = filter_unattributed(unattributed, changed_set)
        # Stage 3 + Stage 4 see ONLY changed work (+ any untouched
        # feature we could not re-hydrate) from here on.
        deterministic_features = partition.touched + rescan_untouched
        incremental_untouched = rehydrate.features
        incremental_gate_meta = {
            "incremental_gate_active": True,
            "incremental_gate_changed_files": len(changed_set),
            "incremental_gate_features_touched": len(partition.touched),
            "incremental_gate_features_untouched": len(partition.untouched),
            "incremental_gate_features_rehydrated": len(rehydrate.features),
            "incremental_gate_features_rescanned_missing": len(
                rescan_untouched,
            ),
            "incremental_gate_unattributed_pre": unattributed_pre,
            "incremental_gate_unattributed_post": len(unattributed),
        }
        log2_5.info(
            "incremental gate: "
            f"changed_files={len(changed_set)} "
            f"features_touched={len(partition.touched)} "
            f"features_untouched={len(partition.untouched)} "
            f"rehydrated={len(rehydrate.features)} "
            f"rescanned_missing={len(rescan_untouched)} "
            f"residual_paths={unattributed_pre}->{len(unattributed)}",
        )
        for nm in rehydrate.missing_names:
            log2_5.warn(
                f"untouched feature {nm!r} not in base scan — "
                f"re-scanning via Stage 3",
            )
        write_stage_artifact(
            ctx.repo_path,
            stage_index=2,
            stage_name="incremental_gate",
            payload={
                **incremental_gate_meta,
                "touched_feature_names": [
                    f.name for f in partition.touched
                ],
                "rehydrated_feature_names_sample":
                    rehydrate.rehydrated_names[:50],
                "rescanned_missing_feature_names":
                    rehydrate.missing_names[:50],
            },
            run_dir=run_dir,
        )
    return IncrementalGateOutcome(
        deterministic_features=deterministic_features,
        unattributed=unattributed,
        untouched_features=incremental_untouched,
        gate_meta=incremental_gate_meta,
        base_scan=incremental_base_scan,
    )


def splice_untouched_features(
    features: list[Any],
    untouched: list[Any],
) -> int:
    """Incremental splice (--since path ONLY) — returns spliced count.

    Re-attach the untouched features re-hydrated from the base
    scan (Stage 2.5). They are already final ``Feature`` objects
    (flows + metrics intact) and skipped Stage 3/4 entirely — the
    cost saving. They join the freshly-scanned touched features
    here and flow through the deterministic downstream stages
    (5.3 collapse, 5.5 bipartite, 6 metrics, 8 Layer-2) over the
    COMPLETE feature set so cross-cutting + Layer 2 stay correct.

    Mutates ``features`` in place (append-only).
    """
    existing_names = {f.name for f in features}
    spliced = 0
    for uf in untouched:
        if uf.name in existing_names:
            # A freshly-scanned touched feature already owns this
            # name (rename / split collision) — prefer the fresh
            # one; never double-emit.
            continue
        features.append(uf)
        existing_names.add(uf.name)
        spliced += 1
    return spliced


def is_layer2_noop(
    *,
    is_full_scan: bool,
    base_scan: dict[str, Any] | None,
    gate_meta: dict[str, Any],
) -> bool:
    """Incremental Layer-2 reuse decision (--since path ONLY).

    Stage 8 (single Sonnet analyst call) + Stage 6.7b (per-domain Haiku
    UF refiner) are the second cost ceiling from
    finding-incremental-no-llm-savings: they still run over the WHOLE
    merged feature set on every incremental. Both are pure functions of
    the DETERMINISTIC feature set (Stage 0/1/2), so a NO-OP diff (zero
    touched dev features → the Layer-1 set is identical to base) lets us
    reuse the base scan's FINAL product_features verbatim and SKIP the
    analyst + its deterministic post-passes (rollup, 8.5, hotspots).
    On a full / cold scan this is ALWAYS False — Stage 8 runs
    whole-repo, byte-identical (cold-scan rule).
    """
    return (
        not is_full_scan
        and base_scan is not None
        and gate_meta.get(
            "incremental_gate_features_touched", -1,
        ) == 0
    )


def reuse_base_layer2(
    base_scan: dict[str, Any],
    log: StageLogger,
) -> Any:
    """Build a ``Stage8Result`` from the base scan's final Layer 2.

    No developer feature changed → reuse the base scan's FINAL
    Layer-2 (already through analyst + rollup + 8.5 + hotspots)
    verbatim. Build a Stage8Result from base so the orchestrator's
    override block is unchanged; the deterministic post-passes
    (rollup / 8.5 / hotspots) are skipped via the
    ``incremental_layer2_noop`` guard since base PFs already
    carry attached flows + backfilled members + hotspots.
    """
    from faultline.pipeline_v2.incremental_gate import (
        rehydrate_base_product_features as _rehydrate_base_pfs,
    )
    from faultline.pipeline_v2.stage_8_marketing_clusterer import (
        Stage8Result as _Stage8Result,
    )
    _reused_pfs, _reused_map = _rehydrate_base_pfs(base_scan)
    stage_8_result = _Stage8Result(
        product_features=_reused_pfs,
        dev_to_product_map=_reused_map,
        telemetry={
            "source": "incremental-reuse-base",
            "haiku_called": False,
            "sonnet_called": False,
            "reused_product_features": len(_reused_pfs),
            "incremental_layer2_noop": True,
        },
        member_flows_map={},
    )
    log.info(
        "mode=incremental-reuse-base — reused "
        f"{len(_reused_pfs)} base product features (analyst "
        "skipped, no-op diff)",
    )
    return stage_8_result


def apply_incremental_bookkeeping(
    *,
    repo_path: Path,
    since: str | None,
    is_full_scan: bool,
    base_scan: dict[str, Any] | None,
    features: list[Any],
) -> tuple[str, dict[str, Any]]:
    """Incremental scan bookkeeping — returns ``(head_sha, meta)``.

    Carries forward Stage 6 metrics for untouched features by matching
    feature UUIDs against the base scan, mutating ``features`` in place.
    On a full scan only the head SHA is computed and the meta dict
    carries the empty defaults.
    """
    from faultline.pipeline_v2.incremental import (
        carry_forward_metrics as _carry_forward_metrics,
        changed_files_since as _changed_files_since,
        head_sha as _head_sha,
        touched_feature_uuids as _touched_feature_uuids,
    )

    head = _head_sha(repo_path)
    carried_count = 0
    incremental_meta: dict[str, Any] = {
        "incremental_changed_files": [],
        "incremental_touched_uuids": [],
        "incremental_carried_forward_count": 0,
    }
    if not is_full_scan:
        if base_scan is None:
            raise ValueError(
                "--since requires --base-scan-path (engine cannot match "
                "lineage without a previous scan)."
            )
        changed = _changed_files_since(repo_path, since or "")
        touched = _touched_feature_uuids(changed, base_scan)
        # Carry forward Stage 6 metrics for untouched features.
        # We mutate the Feature pydantic models via model_dump round-trip
        # so the carry-forward helper can operate on plain dicts.
        base_feats = (
            base_scan.get("developer_features")
            or base_scan.get("features")
            or []
        )
        # Mutate features in-place — easier than rebuilding pydantic models.
        feat_payload = [f.model_dump() for f in features]
        carried_count = _carry_forward_metrics(
            feat_payload, list(base_feats), touched,
        )
        # Push the touched metric values back onto the Feature objects.
        by_uuid = {p.get("uuid"): p for p in feat_payload if p.get("uuid")}
        for feat in features:
            p = by_uuid.get(feat.uuid)
            if not p:
                continue
            for k in (
                "health_score", "bug_fix_ratio", "bug_fixes",
                "coverage_pct", "total_commits",
                "symbol_health_score",
            ):
                if k in p and p[k] is not None:
                    setattr(feat, k, p[k])
        incremental_meta = {
            "incremental_changed_files": list(changed),
            "incremental_touched_uuids": sorted(touched),
            "incremental_carried_forward_count": carried_count,
        }
    return head, incremental_meta


def plan_uf_domain_allowlist(
    user_flows: list[Any],
    base_scan: dict[str, Any],
    log: StageLogger,
) -> set[str | None]:
    """Incremental UF-refiner reuse (--since path ONLY).

    A UF's refined presentation depends ONLY on its member flows
    (deterministic Stage 6.7) + their frontend signal. UFs whose
    member-flow-set is unchanged from the base scan adopt the base
    refinement verbatim (keyed on frozenset(member_flow_ids) — a
    stable structural key, no magic number). Only domains with a
    changed UF still get a Haiku call. On a full / cold scan the
    orchestrator keeps ``domain_allowlist`` as None → every domain is
    refined, byte-identical to before (cold-scan rule).
    """
    from faultline.pipeline_v2.incremental_gate import (
        plan_uf_refinement_reuse as _plan_uf_reuse,
    )
    uf_plan = _plan_uf_reuse(user_flows, base_scan)
    log.info(
        "uf_refiner incremental reuse: "
        f"reused_uf={uf_plan.reused_uf_count} "
        f"reused_domains={len(uf_plan.reused_domains)} "
        f"rescan_domains={len(uf_plan.rescan_domains)}",
    )
    return uf_plan.rescan_domains


__all__ = [
    "IncrementalGateOutcome",
    "apply_incremental_bookkeeping",
    "is_layer2_noop",
    "plan_uf_domain_allowlist",
    "reuse_base_layer2",
    "run_incremental_gate",
    "splice_untouched_features",
]
