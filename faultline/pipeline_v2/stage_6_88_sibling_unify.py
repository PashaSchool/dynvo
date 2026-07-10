"""Stage 6.88 — sibling-anchor capability unification (B16 Part 2).

One product capability minted from sibling route dirs (Soc0
``route:investigation`` + ``route:investigations-page`` +
``route:investigation-flow`` = ONE "Investigations") collapses to a single
PF. The mint bar evaluates each anchor independently and b8c's ``_domain_family``
only RE-HOMES folded devs onto a ``-page`` surface — it never MERGES two
already-minted sibling PFs. This pass does.

Structural rails (no magic counts):
  1. **route family only** — both anchors ``route:*`` (fdir/hub/schema never).
  2. **same parent namespace** — identical anchor path prefix above the
     terminal segment (the "sibling" constraint; capability identity is
     compared only within one route parent, never globally — the b8a
     over-fire lesson).
  3. **capability identity match** — strip a trailing dev-grain suffix
     (page/flow/view/screen) then singularise the terminal with the house
     ``normalize_anchor_key`` (``investigations-page`` / ``investigation-flow``
     / ``investigation`` all -> ``investigation``).
  4. **dev-suffix-driven ONLY** — the cluster MUST contain >= 1 member whose
     terminal carries a dev-grain suffix. A bare singular/plural pair
     (``user`` + ``users``, no suffix) is NOT merged — the over-unification
     guard.

Winner (canonical anchor): the non-dev-suffix, largest-body member (Soc0
``route:investigation``). Losers' devs + user-flows re-point to the winner;
losers' member_files + paths fold in; loser PFs drop. Runs AFTER the journey
layer, BEFORE Stage 6.97 loc — so the merged body is loc-stamped, role-lane'd,
path_index'd and I23-read as ONE PF. Kill-switch ``FAULTLINE_PF_SIBLING_UNIFY=0``
-> byte-identical.
"""

from __future__ import annotations

import os
import re
from typing import Any

from faultline.pipeline_v2.spine_anchors import normalize_anchor_key

SIBLING_UNIFY_ENV = "FAULTLINE_PF_SIBLING_UNIFY"

#: Dev-grain surface suffixes a route dir may leak (mirror the naming law).
_DEVGRAIN_SUFFIX = ("page", "flow", "view", "screen")
#: Param / route-group segment forms to skip when reading the terminal.
_SKIP_SEG = re.compile(r"^(\[.*\]|\(.*\)|[$:].*)$")

__all__ = ["SIBLING_UNIFY_ENV", "sibling_unify_enabled", "unify_sibling_anchors"]


def sibling_unify_enabled() -> bool:
    """Default ON; ``FAULTLINE_PF_SIBLING_UNIFY=0`` restores the pre-B16-Part2
    output byte-identically (sibling PFs stay separate)."""
    return os.environ.get(SIBLING_UNIFY_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def _anchor_parts(anchor_id: Any) -> tuple[str, str] | None:
    """``(parent_ns, terminal)`` of a ``route:`` anchor, else ``None``."""
    aid = str(anchor_id or "")
    if not aid.startswith("route:"):
        return None
    segs = [s for s in aid[len("route:"):].split("/")
            if s and not _SKIP_SEG.match(s)]
    if not segs:
        return None
    return "/".join(segs[:-1]), segs[-1]


def _capability_identity(terminal: str) -> str:
    """Strip a trailing dev-grain suffix then singularise the terminal."""
    stem = terminal
    for sf in _DEVGRAIN_SUFFIX:
        if stem.endswith("-" + sf) and len(stem) > len(sf) + 1:
            stem = stem[: -(len(sf) + 1)]
            break
    return normalize_anchor_key(stem) or terminal


def _has_devsuffix(terminal: str) -> bool:
    return any(terminal.endswith("-" + sf) for sf in _DEVGRAIN_SUFFIX)


def _size(pf: Any) -> int:
    return len(getattr(pf, "member_files", None) or []) or len(
        getattr(pf, "paths", None) or [])


def unify_sibling_anchors(
    user_flows: list[Any],
    features: list[Any],
    product_features: list[Any],
) -> dict[str, Any]:
    """Merge co-identity sibling route PFs in place. Returns telemetry."""
    tele: dict[str, Any] = {
        "enabled": True, "clusters": 0, "merged_away": 0, "merges": [],
    }
    groups: dict[tuple[str, str], list[tuple[Any, str]]] = {}
    for pf in product_features:
        parts = _anchor_parts(getattr(pf, "anchor_id", None))
        if not parts:
            continue
        parent, terminal = parts
        groups.setdefault((parent, _capability_identity(terminal)), []).append(
            (pf, terminal))

    remap: dict[str, str] = {}   # loser slug -> winner slug
    for _key, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        if not any(_has_devsuffix(t) for _, t in members):
            continue  # rail 4 — over-unification guard
        # winner: non-dev-suffix first, then largest body, then anchor alpha.
        members.sort(key=lambda x: (
            _has_devsuffix(x[1]), -_size(x[0]),
            str(getattr(x[0], "anchor_id", "") or "")))
        winner = members[0][0]
        w_slug = str(getattr(winner, "name", "") or "")
        losers = [m[0] for m in members[1:]]
        tele["clusters"] += 1
        for loser in losers:
            l_slug = str(getattr(loser, "name", "") or "")
            if not l_slug or l_slug == w_slug:
                continue
            remap[l_slug] = w_slug
            seen = {m.path for m in (winner.member_files or [])}
            for m in (loser.member_files or []):
                if m.path not in seen:
                    winner.member_files.append(m)
                    seen.add(m.path)
            wp = set(winner.paths or [])
            for p in (loser.paths or []):
                if p not in wp:
                    winner.paths.append(p)
                    wp.add(p)
            tele["merges"].append({
                "winner": w_slug, "loser": l_slug,
                "winner_anchor": str(getattr(winner, "anchor_id", "") or ""),
                "loser_anchor": str(getattr(loser, "anchor_id", "") or ""),
            })
            tele["merged_away"] += 1

    if not remap:
        return tele

    for f in features:
        if str(getattr(f, "product_feature_id", "") or "") in remap:
            f.product_feature_id = remap[f.product_feature_id]
    for uf in user_flows:
        if str(getattr(uf, "product_feature_id", "") or "") in remap:
            uf.product_feature_id = remap[uf.product_feature_id]
    product_features[:] = [
        pf for pf in product_features
        if str(getattr(pf, "name", "") or "") not in remap
    ]
    return tele
