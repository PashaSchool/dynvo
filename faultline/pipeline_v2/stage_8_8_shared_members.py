"""Stage 8.8 — shared-member enrichment of the de-sink residual.

Why this stage exists
=====================

After Stage 8.7 de-sink, a workspace anchor holds only its *residual* — the
files no specific feature's ``paths`` claim (the shared service / model / UI
long-tail). Measurement (keyless infisical) showed this residual is reached
ONLY by the anchor's own import-closure: the anchor seed is the whole
workspace, while specific features are stripped thin by Stage 2 attribution
(``package`` is the top source priority) and flowless features get no Stage 6.3
forward-expansion — so 100% of the residual sits on the anchor and 0% on the
specific features that actually *use* it.

These files are GENUINELY SHARED (a ``<Button>`` imported by 40 pages, a
``permission-service`` injected into many routers) — they have no single owner,
so attributing them to one feature would be wrong (de-sink + lever-#2 measured
that dead). But the features that import them should still SHOW them. This stage
records that: for each residual file, it finds the specific features whose own
files directly import it and attaches the file as an N:M ``role="shared"``
``member_file`` (the same surface Stage 2.6 uses for high-fan-in infra, and the
one the dashboard reads).

Safe by construction
====================

This stage NEVER touches ``feature.paths`` — it only appends ``member_files``
(the additive N:M claim ledger; a file legitimately appears on many features
there). So the ``paths``-based gates — ``eval/structural_audit``
(max_feature_share) and ``eval/membership`` — cannot regress. The quality bar is
import-edge precision (a deterministic resolve) and coverage (% of residual that
gets ≥1 importer). Genuinely-shared leaves with no importer (e.g. DI-injected db
models reached only transitively) stay honest residual.

1-hop direct imports only: "this feature's own code imports this shared file" is
the strongest, lowest-noise signal. Reuses Stage 6.3's import cache + resolvers
and the per-scan tsconfig alias map. Deterministic. No LLM. No network.

Default ON; disable via ``FAULTLINE_STAGE_8_8_SHARED_MEMBERS=0``.
"""

from __future__ import annotations

import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.analyzer.tsconfig_paths import build_path_alias_map, resolve_ts_import
from faultline.pipeline_v2.stage_6_3_import_tree import (
    _PY_EXTS,
    _SourceCache,
    _TS_EXTS,
    _fallback_relative_resolve,
    _is_vendor_or_test,
    _resolve_py_module_simple,
    _suffix,
)
from faultline.pipeline_v2.stage_8_7_anchor_desink import _is_workspace_anchor

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# Provenance confidence for a direct (1-hop) shared-import claim. Below a
# primary closure claim — this is N:M provenance, never primary ownership.
_SHARED_IMPORT_CONFIDENCE = 0.5

# ── Barrel / hub conduit guard (Phase-3 framework-awareness) ─────────────
#
# A barrel (``index.ts`` re-exporting many modules) or a high-fan-in hub
# (e.g. ``prisma.module.ts``, ``app.module.ts``, ``withMiddleware.ts``,
# a shared ``constants/index.ts``) sits on the import path of nearly every
# feature. When such a file is part of the de-sink residual, this stage
# would otherwise attach its WHOLE residual closure as a ``shared`` member
# of EVERY importing feature — producing identical phantom member-sets
# (cal-com: 10 enum features each carrying the same 140 files; 92 more
# carrying an identical 133-file set). That is the duplicate / phantom
# feature generator the framework-awareness mission targets.
#
# The guard: a residual file imported by an OUTLIER number of features is
# platform infra, not a per-feature member. It is left as honest residual
# on the workspace anchor (its existing role) and NOT propagated N:M.
#
# Threshold is scale-invariant by construction (per rule-no-magic-tuning):
#
#     T = max(_FANOUT_FLOOR, P90, ceil(_FANOUT_MIN_FRAC * n_features))
#
#   * ``_FANOUT_FLOOR`` (3) — three independent claimants before a share is
#     even a candidate for "infrastructure" (same convention as Stage 2.6 /
#     Stage 4 SAT_WINDOW). A pairwise share is a legitimate attachment.
#   * ``P90`` of the repo's OWN per-file importer-count distribution — a
#     file in the top decile of fan-in is statistically a hub for THIS repo,
#     whatever its absolute size.
#   * ``ceil(_FANOUT_MIN_FRAC * n_features)`` — a structural floor that
#     prevents P90 from stripping legitimate low-fan-in shares on repos with
#     NO conduit (e.g. documenso P90=4: without this floor a benign
#     4-importer util would be dropped). 10% of all features is the
#     "touched by a meaningful slice of the product" boundary.
#
# Measured (cached cold scans, member_files is post-LLM additive so this
# filter is faithfully simulable): cal-com phantom dup-features 102→0,
# trigger.dev 56→0, infisical 42→7; documenso / openstatus / supabase
# (no conduit symptom) byte-unchanged. uf_recall / naming / dup_flow read
# flows + names, never member_files, so they cannot regress; owned-blob
# (the workspace-anchor gate) is untouched because these files are already
# role="shared" / primary=False.
_FANOUT_FLOOR = 3
_FANOUT_MIN_FRAC = 0.10


@dataclass
class SharedMemberResult:
    enabled: bool = True
    residual_files: int = 0
    residual_attached: int = 0          # residual files with ≥1 importer
    edges: int = 0                      # total (feature, file) shared claims
    features_enriched: int = 0
    coverage_pct: float = 0.0
    fanout_cap: int = 0                 # barrel/hub conduit threshold
    conduit_files: int = 0             # residual files skipped as conduits
    sample: list[dict[str, Any]] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "residual_files": self.residual_files,
            "residual_attached": self.residual_attached,
            "edges": self.edges,
            "features_enriched": self.features_enriched,
            "coverage_pct": self.coverage_pct,
            "fanout_cap": self.fanout_cap,
            "conduit_files": self.conduit_files,
            "sample": list(self.sample[:20]),
        }


def _is_enabled() -> bool:
    return os.environ.get("FAULTLINE_STAGE_8_8_SHARED_MEMBERS", "1") != "0"


def _nearest_rank(sorted_values: list[int], pct: float) -> int:
    """Nearest-rank percentile of a pre-sorted nonempty int list."""
    if not sorted_values:
        return 0
    n = len(sorted_values)
    rank = max(1, math.ceil(pct * n))
    return sorted_values[min(rank, n) - 1]


def _fanout_cap(importer_counts: list[int], n_features: int) -> int:
    """Scale-invariant fan-out cap for the barrel/hub conduit guard.

    ``importer_counts`` is the per-residual-file importer-count distribution.
    See the module-level constants for the rationale of each prong.
    """
    sorted_counts = sorted(importer_counts)
    p90 = _nearest_rank(sorted_counts, 0.90)
    frac_floor = math.ceil(_FANOUT_MIN_FRAC * n_features)
    return max(_FANOUT_FLOOR, p90, frac_floor)


def _resolve_one(
    rel: str,
    spec: str,
    alias_map: Any,
    tracked: frozenset[str],
) -> str | None:
    """Resolve one import specifier from ``rel`` to a tracked file, or None."""
    suffix = _suffix(rel)
    if suffix in _TS_EXTS:
        return resolve_ts_import(
            rel, spec, alias_map=alias_map, tracked_files=tracked,
        ) or _fallback_relative_resolve(rel, spec, tracked)
    if suffix in _PY_EXTS:
        return _resolve_py_module_simple(rel, spec, tracked)
    return None


def enrich_shared_members(
    ctx: "ScanContext",
    features: list["Feature"],
) -> SharedMemberResult:
    """Attach de-sink residual files as ``role="shared"`` member_files on the
    specific features whose own files directly import them.

    Mutates importing features in place (``member_files`` only). Returns a
    :class:`SharedMemberResult` for telemetry.
    """
    from faultline.models.types import MemberFile

    result = SharedMemberResult(enabled=_is_enabled())
    if not result.enabled:
        return result

    anchors = [f for f in features if _is_workspace_anchor(f)]
    specifics = [f for f in features if not _is_workspace_anchor(f)]
    if not anchors or not specifics:
        return result

    # Residual = files claimed by a workspace anchor's `paths` but no specific
    # feature's `paths` (the de-sink residual).
    specific_paths: set[str] = set()
    for f in specifics:
        specific_paths.update(f.paths)
    residual: frozenset[str] = frozenset(
        p for a in anchors for p in a.paths if p not in specific_paths
    )
    result.residual_files = len(residual)
    if not residual:
        return result

    repo_path = Path(ctx.repo_path)
    tracked = frozenset(ctx.tracked_files)
    # R4 — adopt the ctx-shared source cache (identical content by
    # construction; ``None``/mismatch → local fallback, e.g. replay).
    from faultline.pipeline_v2.shared_source import shared_source_cache
    cache = shared_source_cache(ctx, repo_path) or _SourceCache(repo_path)
    alias_map = build_path_alias_map(repo_path)

    # feature name → set(residual files it directly imports)
    imports_by_feature: dict[str, set[str]] = defaultdict(set)
    # residual file → set(feature names importing it) — for fan-in evidence
    importers_by_file: dict[str, set[str]] = defaultdict(set)

    for f in specifics:
        for rel in f.paths:
            if _suffix(rel) not in (_TS_EXTS | _PY_EXTS):
                continue
            if _is_vendor_or_test(rel):
                continue
            for spec in cache.imports(rel).values():
                tgt = _resolve_one(rel, spec, alias_map, tracked)
                if tgt is not None and tgt in residual:
                    imports_by_feature[f.name].add(tgt)
                    importers_by_file[tgt].add(f.name)

    # ── Barrel / hub conduit guard ───────────────────────────────────
    # A residual file imported by an outlier number of features is a
    # barrel/hub conduit (platform infra), not a per-feature member.
    # Skip its N:M propagation; it stays honest residual on the anchor.
    fanout_cap = _fanout_cap(
        [len(v) for v in importers_by_file.values()],
        len(features),
    )
    conduit_files = {
        fp for fp, importers in importers_by_file.items()
        if len(importers) >= fanout_cap
    }
    result.fanout_cap = fanout_cap
    result.conduit_files = len(conduit_files)

    feat_by_name = {f.name: f for f in specifics}
    edges = 0
    enriched = 0
    for fname, files in imports_by_feature.items():
        feat = feat_by_name[fname]
        existing = {m.path for m in feat.member_files}
        added = 0
        for fp in sorted(files):
            if fp in existing:
                continue
            if fp in conduit_files:
                continue
            n_importers = len(importers_by_file[fp])
            feat.member_files.append(MemberFile(
                path=fp,
                role="shared",
                confidence=_SHARED_IMPORT_CONFIDENCE,
                evidence=(
                    f"direct import of de-sink residual; shared across "
                    f"{n_importers} feature(s)"
                ),
                primary=False,
            ))
            edges += 1
            added += 1
        if added:
            enriched += 1

    attached_files = [fp for fp in importers_by_file if fp not in conduit_files]
    result.residual_attached = len(attached_files)
    result.edges = edges
    result.features_enriched = enriched
    result.coverage_pct = round(len(attached_files) / len(residual), 4)
    result.sample = [
        {"file": fp, "importers": sorted(importers_by_file[fp])[:5],
         "n_importers": len(importers_by_file[fp])}
        for fp in sorted(attached_files, key=lambda x: -len(importers_by_file[x]))[:20]
    ]
    return result


__all__ = [
    "SharedMemberResult",
    "enrich_shared_members",
]
