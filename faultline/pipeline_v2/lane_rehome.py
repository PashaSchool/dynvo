"""Product-Spine W3.2 — UF-evidence lane re-homing (``fold:uf-evidence``).

W3.1 killed the tiny-anchor giant-body sinks by refusing dishonest
folds — and the corpus lane grew from 85K to 1.94M LOC as the freed
mass landed in ``platform_infrastructure[]`` with no owner (chain6 §D.3:
supabase 1.42M with 63 product-named devs in lane; comp 197.7K / 70
devs).  Part of that pile is *journey-evidenced*: the final user flows
already CITE those devs' files (supabase "Manage cron jobs" cites the
``apps/studio/data/database-cron-jobs/*`` hooks; comp's ``*-2`` split
subs are cited by exactly the capability PF they were split from).  A
lane row that a product journey demonstrably rides is not flowless
plumbing — it is that capability's body, stranded.

This rung re-homes a lane dev to a product feature when the JOURNEY
EVIDENCE is strong enough to stand on its own:

  * CITATION   — ≥ min(3, |dev files|) distinct dev files are cited by
                 the PF's user flows (entries + spans);
  * ONE PF     — a STRICT MAJORITY of the dev's cited files belong to
                 one PF's journeys (ties never move);
  * SELF-EVIDENCE — the citations cover ≥ ``_UNION_FLOOR`` (0.34, the
                 house random-tail bound) of the dev's OWN files: a
                 647-file multi-domain dev with one cited subdir stays
                 (supabase ``data`` — re-homing it wholesale would
                 rebuild the sink W3.1 killed);
  * TARGET SANITY — the PF is anchored and its anchor tail is not a
                 structural token (comp's ``src`` PF attracted 52K in
                 simulation — a structural name is not a capability);
  * CAPACITY CAP — cumulative re-homed LOC per PF must not build a
                 gate-cell-shaped pile (validator I23 gate cell,
                 share ≥ 0.75 & out ≥ 25K: blocked when the moved mass
                 reaches 25K AND 3× the PF's own body).

Deliberately out of scope: flowful lane devs (the mint's flowful-never-
lane law owns them — W3.2 fix 1) and workspace-anchor devs ("anchors
never move — their sample spans the whole workspace").

Deterministic, $0 LLM. Runs on BOTH the keyed and keyless anchored
paths, AFTER 6.7d + route-group seeds (citations must reflect the FINAL
journey layer) and BEFORE Stage 6.97 (the moved membership is stamped
into PF/lane loc accounting by the normal pass — loc-truth I13 holds).
Kill-switch: ``FAULTLINE_SPINE_LANE_REHOME=0``.
"""

from __future__ import annotations
from faultline.pipeline_v2.overturn_ledger import propose_pf

import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from faultline.models.types import Feature, Flow, UserFlow

#: Provenance note stamped on re-homed devs (``Feature.anchor_id``).
UF_EVIDENCE_NOTE = "fold:uf-evidence"

#: Ruler mirrors — see module docstring. ``_UNION_FLOOR`` is the same
#: constant the mint's fold guards and validator I15 gate on; the cap
#: constants mirror the validator I23 GATE cell (share>=0.75 & out>=25K:
#: out >= 25K and out >= 3x the rest of the body).
_UNION_FLOOR = 0.34
_CAP_LOC = 25_000
_CAP_BODY_MULT = 3.0

_LANE_REASONS = frozenset({
    "no_anchor_lineage", "sub_mint_bar_surface", "shell_lineage_only",
})


def lane_rehome_enabled() -> bool:
    """Default ON; disable via ``FAULTLINE_SPINE_LANE_REHOME=0``."""
    return os.environ.get("FAULTLINE_SPINE_LANE_REHOME", "1") != "0"


def _norm_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _structural_tokens() -> frozenset[str]:
    """The spine vocabulary's structural stoplist (packaged YAML)."""
    try:
        from faultline.pipeline_v2.spine_anchors import load_spine_vocab
        stop = load_spine_vocab().get("structural_stoplist") or []
        return frozenset(_norm_token(str(t)) for t in stop)
    except Exception:  # noqa: BLE001 — vocabulary is packaged; be safe
        return frozenset()


def _anchor_tail_token(anchor_id: str) -> str:
    """``route:apps/studio/pages/api/platform/pg-meta`` → ``pgmeta``;
    ``ws:packages/ui`` → ``ui``; ``fold:...`` notes → their tail."""
    tail = str(anchor_id or "").rsplit("/", 1)[-1]
    tail = tail.split(":", 1)[-1] if "/" not in str(anchor_id or "") else tail
    return _norm_token(tail)


def rehome_uf_cited_lane_devs(
    features: list["Feature"],
    product_features: list["Feature"],
    user_flows: list["UserFlow"],
    flows: list["Flow"],
    repo_path: Path | str | None = None,
) -> dict[str, Any]:
    """Re-home journey-evidenced lane devs; returns telemetry.

    Mutates ``Feature.product_feature_id`` / ``anchor_id`` /
    ``shared_reason`` in place (the same canonical move the mint and
    ``conservation.rehome_shared_flowful_devs`` perform). The lane
    surface (``build_platform_infrastructure_lane``) and Stage 6.97 loc
    accounting are derived from dev state downstream, so no other
    surface needs patching.
    """
    tele: dict[str, Any] = {
        "enabled": lane_rehome_enabled(), "checked": 0, "rehomed": 0,
        "rehomed_loc": 0,
        "blocked_concentration": 0, "blocked_self_evidence": 0,
        "blocked_target": 0, "blocked_cap": 0,
        "sample": [],
    }
    if not lane_rehome_enabled():
        return tele
    if not features or not product_features or not user_flows:
        return tele

    from faultline.pipeline_v2.stage_6_86_anchored_mint import _files_loc
    from faultline.pipeline_v2.stage_8_7_anchor_desink import (
        _is_workspace_anchor,
    )

    pf_by_slug: dict[str, "Feature"] = {}
    for pf in product_features:
        slug = str(getattr(pf, "name", "") or "")
        if slug:
            pf_by_slug[slug] = pf

    # ── journey citations: PF slug → set of files its UFs ride ─────────
    flow_by_key: dict[str, "Flow"] = {}
    for fl in flows or []:
        for key in (getattr(fl, "uuid", None), getattr(fl, "name", None)):
            if key:
                flow_by_key.setdefault(str(key), fl)
    cited_by_pf: dict[str, set[str]] = defaultdict(set)
    for uf in user_flows:
        pfid = str(getattr(uf, "product_feature_id", None) or "")
        if not pfid or pfid not in pf_by_slug:
            continue
        for mid in (getattr(uf, "member_flow_ids", None) or []):
            mfl = flow_by_key.get(str(mid))
            if mfl is None:
                continue
            ep = getattr(mfl, "entry_point_file", None)
            if ep:
                cited_by_pf[pfid].add(str(ep))
            for p in (getattr(mfl, "paths", None) or []):
                cited_by_pf[pfid].add(str(p))
    if not cited_by_pf:
        return tele

    stop = _structural_tokens()
    loc_cache: dict[str, int] = {}
    root = Path(repo_path) if repo_path else None

    def _dev_loc(d: "Feature") -> int:
        loc = getattr(d, "loc", None)
        if isinstance(loc, int) and loc > 0:
            return loc
        if root is None:
            return 0
        return _files_loc(root, [str(p) for p in (d.paths or [])], loc_cache)

    def _pf_body_loc(pf: "Feature") -> int:
        loc = getattr(pf, "loc", None)
        if isinstance(loc, int) and loc > 0:
            return loc
        if root is None:
            return 0
        return _files_loc(root, [str(p) for p in (getattr(pf, "paths", None) or [])], loc_cache)

    # ── candidates: flowless, non-facet, non-ws-anchor lane residents ──
    candidates: list[tuple[int, str, "Feature", str, int, int]] = []
    for d in features:
        if getattr(d, "layer", "developer") != "developer":
            continue
        if getattr(d, "product_feature_id", None) is not None:
            continue
        if getattr(d, "shared_reason", None) not in _LANE_REASONS:
            continue
        if getattr(d, "role", None) == "facet":
            continue
        if getattr(d, "flows", None):
            continue  # flowful lane devs are the mint law's concern
        if _is_workspace_anchor(d):
            continue  # anchors never move
        dpaths = {str(p) for p in (getattr(d, "paths", None) or [])}
        if not dpaths:
            continue
        votes: Counter[str] = Counter()
        for pfid, fset in cited_by_pf.items():
            n = len(dpaths & fset)
            if n:
                votes[pfid] = n
        if not votes:
            continue
        tele["checked"] += 1
        total = sum(votes.values())
        best = sorted(votes, key=lambda k: (-votes[k], k))[0]
        bn = votes[best]
        if bn < min(3, len(dpaths)) or bn * 2 <= total:
            tele["blocked_concentration"] += 1
            continue
        if bn / len(dpaths) < _UNION_FLOOR:
            tele["blocked_self_evidence"] += 1
            continue
        pf = pf_by_slug[best]
        anchor_id = str(getattr(pf, "anchor_id", None) or "")
        if (not anchor_id or anchor_id.startswith("fold:")
                or _anchor_tail_token(anchor_id) in stop
                or best in ("platform", "shared-platform")):
            tele["blocked_target"] += 1
            continue
        candidates.append((bn, d.name, d, best, len(dpaths), total))

    # ── deterministic apply order + per-PF capacity cap ────────────────
    moved_loc_by_pf: dict[str, int] = defaultdict(int)
    pf_body_cache: dict[str, int] = {}
    for bn, name, d, best, npaths, total in sorted(
            candidates, key=lambda c: (-c[0], c[1])):
        dloc = _dev_loc(d)
        if best not in pf_body_cache:
            pf_body_cache[best] = _pf_body_loc(pf_by_slug[best])
        would = moved_loc_by_pf[best] + dloc
        if would >= _CAP_LOC and would >= _CAP_BODY_MULT * pf_body_cache[best]:
            tele["blocked_cap"] += 1
            continue
        propose_pf(d, best, rung="lane_rehome")
        d.anchor_id = UF_EVIDENCE_NOTE
        d.shared_reason = None
        moved_loc_by_pf[best] = would
        tele["rehomed"] += 1
        tele["rehomed_loc"] += dloc
        if len(tele["sample"]) < 20:
            tele["sample"].append({
                "dev": name, "pf": best, "cited": bn,
                "dev_files": npaths, "loc": dloc,
            })
    tele["moved_loc_by_pf"] = dict(sorted(moved_loc_by_pf.items()))
    return tele


__all__ = [
    "UF_EVIDENCE_NOTE",
    "lane_rehome_enabled",
    "rehome_uf_cited_lane_devs",
]
