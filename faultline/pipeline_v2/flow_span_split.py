"""W4 — cross-PF flow-attribution split (Product-Spine §4.6, scope 3).

A flow is a line-range projection; sharing is legal and LABELED
(flow-feature-concept). Today a page-grain flow's ``paths`` routinely
span several product features' anchors, so journeys attach dirty:
validator I15 divides a UF's flow files by its PF scope (attach-overlap
median 0.25-0.65 vs the 0.7 target) and I16 counts foreign
entry-owners. This pass SPLITS the attribution instead of pretending
the whole span belongs to one PF:

  * primary   — the flow keeps the files owned by its HOME product
                feature (the entry file's owner PF, falling back to the
                owning dev's PF) plus every file with NO product owner
                (lane / unowned files are not evidence of foreignness);
  * secondary — files owned by OTHER PFs move to the additive
                ``Flow.shared_paths[]`` ledger ({path, owner PF,
                reason}) — conservation: no file is lost, it is
                RE-LABELED;
  * spans     — graph nodes on foreign files keep their symbol-grain
                spans but are retagged ``role="shared"`` (real evidence,
                labeled); whole-file ``kind="file"`` guesses on foreign
                files are dropped from the node surface (a whole-file
                span on another PF's file is not evidence — the honest
                span is unknown); the Phase-5 LOC views are re-projected.

Dual-LOC discipline: this pass NEVER touches ``member_files`` /
``paths`` on features, so Stage 6.97's owned/shared accounting is
untouched — only the FLOW-level projection changes.

Runs ONLY when the Stage-6.86 anchored mint applied (dev→PF stamps are
total by construction there). Deterministic, $0 LLM. Kill-switch:
``FAULTLINE_FLOW_SPAN_SPLIT=0``.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

logger = logging.getLogger(__name__)

__all__ = [
    "FLOW_SPAN_SPLIT_ENV",
    "flow_span_split_enabled",
    "split_cross_pf_flow_attribution",
]

FLOW_SPAN_SPLIT_ENV = "FAULTLINE_FLOW_SPAN_SPLIT"


def flow_span_split_enabled() -> bool:
    return (os.environ.get(FLOW_SPAN_SPLIT_ENV, "1") or "1").strip().lower() \
        not in {"0", "false", "no", "off"}


def _pf_key(pf: Any) -> str | None:
    return getattr(pf, "id", None) or getattr(pf, "name", None)


def _owner_map(features: list["Feature"]) -> dict[str, str | None]:
    """file → owning dev's product_feature_id (primary claims only).

    Mirrors the validator's ``file_owner_pf`` relation (path_index →
    feature_uuid → dev → product_feature_id) without needing the index:
    primary ``member_files`` first, ``paths`` fallback — the same
    population rule the spine calibration uses (``owned_paths_of``).
    First writer wins on the rare duplicate claim (features arrive in
    stable order).
    """
    from faultline.pipeline_v2.spine_anchors import owned_paths_of

    owner: dict[str, str | None] = {}
    for f in features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        pfid = getattr(f, "product_feature_id", None)
        for p in owned_paths_of(f):
            owner.setdefault(p, pfid)
    return owner


def split_cross_pf_flow_attribution(
    features: list["Feature"],
    product_features: list["Feature"],
    home_override: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Split every flow's file surface into primary vs cross-PF shared.

    Mutates flows in place (paths / shared_paths / nodes / attribution
    roles + the Phase-5 LOC re-projection). Returns telemetry incl. the
    conservation counters (``files_moved`` == Σ added shared rows).
    """
    from faultline.models.types import FlowSharedPath
    from faultline.pipeline_v2.flow_expansion.expander import (
        _project_loc_detail,
    )

    tele: dict[str, Any] = {
        "flows_split": 0, "files_moved": 0, "shared_rows": 0,
        "foreign_file_nodes_dropped": 0, "nodes_retagged_shared": 0,
        "conservation_ok": True,
    }
    owner = _owner_map(features)
    pf_display: dict[str, str] = {}
    for pf in product_features or []:
        k = _pf_key(pf)
        if k:
            pf_display[k] = (getattr(pf, "display_name", None)
                             or getattr(pf, "name", "") or k)

    for f in features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        dev_pf = getattr(f, "product_feature_id", None)
        for flow in getattr(f, "flows", None) or []:
            paths = list(getattr(flow, "paths", None) or [])
            if not paths:
                continue
            entry = getattr(flow, "entry_point_file", None)
            home = (owner.get(entry) if entry else None) or dev_pf
            if not home and home_override:
                # Post-UF second pass (W4): a LANE-homed flow (no owner
                # evidence) adopts its FINAL journey's capability — the
                # conservation-settled attachment is its best-evidence
                # home; other PFs' files become labeled sharing exactly
                # like the first pass. No information destroyed.
                mid = getattr(flow, "uuid", "") or getattr(flow, "name", "")
                home = home_override.get(mid)
            if not home:
                continue  # lane-resident dev — no split basis
            foreign: dict[str, str] = {}
            for p in paths:
                own = owner.get(p)
                if own is not None and own != home and p != entry:
                    foreign[p] = own
            if not foreign:
                continue

            before_set = set(paths)
            flow.paths = [p for p in paths if p not in foreign]
            existing_shared = {
                s.path for s in (getattr(flow, "shared_paths", None) or [])
            }
            for p in sorted(foreign):
                if p in existing_shared:
                    continue
                flow.shared_paths = list(
                    getattr(flow, "shared_paths", None) or []
                ) + [FlowSharedPath(
                    path=p,
                    owner_product_feature=foreign[p],
                    owner_display=pf_display.get(foreign[p]),
                    reason="cross_pf_span",
                )]
                tele["shared_rows"] += 1
            # Conservation over UNIQUE files (``paths`` may legally carry
            # duplicate entries from reach/seed merging — set semantics is
            # the honest ruler): every original file is either kept in
            # ``paths`` or present in the shared ledger.
            moved = len(before_set - set(flow.paths))
            tele["files_moved"] += moved
            shared_set = {s.path for s in (flow.shared_paths or [])}
            if not before_set <= (set(flow.paths) | shared_set):
                tele["conservation_ok"] = False
                logger.warning(
                    "flow_span_split: conservation mismatch on flow %s "
                    "(moved=%d foreign=%d)",
                    getattr(flow, "name", "?"), moved, len(foreign),
                )

            # ── node surface: labeled sharing, no whole-file guesses ──
            # EVIDENCE-CONSERVATION EXCEPTION (supabase smoke I4 blast,
            # 2026-07-07: 87 flows lost their LAST spans): dropping the
            # foreign whole-file guesses is only legal while the flow
            # keeps ≥1 lined node — a flow whose ONLY spans are foreign
            # support files keeps them as labeled sharing instead
            # (retag, never zero a flow's LOC surface).
            nodes = list(getattr(flow, "nodes", None) or [])
            if nodes:
                planned_drop = {
                    id(n) for n in nodes
                    if (n.file in foreign and n.role != "entry"
                        and n.kind == "file")
                }
                lined_left = sum(
                    1 for n in nodes
                    if n.lines is not None and id(n) not in planned_drop
                )
                allow_drop = lined_left > 0
                new_nodes = []
                changed = False
                for n in nodes:
                    if n.file in foreign and n.role != "entry":
                        if n.kind == "file" and allow_drop:
                            tele["foreign_file_nodes_dropped"] += 1
                            changed = True
                            continue
                        if n.role != "shared":
                            n = n.model_copy(update={"role": "shared"})
                            tele["nodes_retagged_shared"] += 1
                            changed = True
                    new_nodes.append(n)
                if changed:
                    flow.nodes = new_nodes
                    if flow.summary is not None:
                        total_lines = sum(
                            max(0, n.lines[1] - n.lines[0] + 1)
                            for n in new_nodes if n.lines is not None
                        )
                        flow.summary = flow.summary.model_copy(update={
                            "total_nodes": len(new_nodes),
                            "total_files": len({n.file for n in new_nodes}),
                            "total_lines_touched": total_lines,
                        })
                    try:
                        _project_loc_detail(flow)
                    except Exception:  # noqa: BLE001 — additive projection
                        logger.debug(
                            "flow_span_split: loc re-projection failed",
                            exc_info=True,
                        )

            # ── attribution roles: support guesses out, evidence labeled ──
            attrs = list(getattr(flow, "flow_symbol_attributions", None) or [])
            if attrs:
                kept = []
                changed = False
                for a in attrs:
                    if a.file in foreign and a.role != "entry":
                        if a.role == "support":
                            changed = True
                            continue
                        if a.role not in ("shared",):
                            a = a.model_copy(update={"role": "shared"})
                            changed = True
                    kept.append(a)
                if changed:
                    flow.flow_symbol_attributions = kept

            tele["flows_split"] += 1
    return tele
