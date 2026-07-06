"""Product-Spine §4.4 — explicit hub/child connector relation (decision A).

A connector HUB is a directory whose children are per-vendor
integrations: ``backend/services/edr/{claroty,cortex,crowdstrike,…}.py``,
``packages/banking/src/providers/{gocardless,plaid,…}``,
``apps/api/src/rest/routers/apps/{fortnox,gmail,slack,…}``,
``apps/studio/…/integrations/{airtable,auth0,…}_wrapper``. The board
evidence (2026-07-06, exhibits 2/12/18/19) shows what happens without an
explicit relation: children scatter across unrelated product features or
sink wholesale into Shared Platform (Soc0 edr-* 4.3k LOC; midday apps
routers 6/6 shared; supabase 28 FDW wrappers; typebot bot-engine blocks).

This module makes the relation FIRST-CLASS and deterministic:

**Detection** (:func:`detect_hub_relations`) — two structural arms over
the dev features' owned files, both requiring ``>= 3`` DISTINCT
vendor-named children (``naming_validator.VENDOR_TOKENS`` — the house
public-vendor vocabulary):

  * *lexicon arm*: the hub dir's own segment is a connector-container
    word (``connectors|integrations|providers|adapters|channels|
    sources|destinations|plugins|app-store|apps``). ``apps`` counts only
    at depth >= 1 (``apps/`` at the repo root is a workspace root, not a
    hub — midday's REAL hub is ``…/rest/routers/apps``).
  * *vendor-majority arm* (the 8.9.7 seed, generalized): ANY directory
    where vendor-named children are the MAJORITY of its children
    (``backend/services/edr`` — the dir segment itself names the
    capability, not a container word).

**Binding** (:func:`apply_hub_pf_binding`) — construction rule, applied
after dev→PF assignment on BOTH product paths (Stage 8 family and the
6.7d rewrite):

  * members = dev features whose OWN files are majority-inside the hub
    dir (the hub dev + every per-vendor child dev, incl. 8.9.7-minted
    sub-features);
  * target PF = the majority non-shared PF among members (weighted by
    files under the hub), else a deterministically MINTED hub PF (the
    parent capability);
  * every member is stamped to the target PF — children of one hub NEVER
    split between Shared Platform and a PF and never scatter across
    unrelated PFs (sibling parity, by construction);
  * affected PFs' path/member_files unions are re-derived.

The UF layer consumes the SAME relation: Stage 6.7's Filter B clusters a
hub's flows as ``(hub_domain, vendor, intent)`` so per-vendor journeys
become possible where a child has its own flows (singleton vendors still
fold into one hub journey via the existing singleton-noise merge), and
the hub's journeys attach to the hub PF instead of Shared Platform.

Deterministic, $0 LLM, scale-invariant (majority ratios + a documented
universal lexicon; no repo-specific paths). Kill-switch:
``FAULTLINE_SPINE_HUBS=0`` (default ON).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from faultline.pipeline_v2.naming_validator import VENDOR_TOKENS, _split_tokens

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

__all__ = [
    "SPINE_HUBS_ENV",
    "HUB_PARENT_SEGMENTS",
    "HubRelation",
    "hubs_enabled",
    "vendor_of_segment",
    "detect_hub_relations",
    "apply_hub_pf_binding",
]

SPINE_HUBS_ENV = "FAULTLINE_SPINE_HUBS"

#: Connector-container directory vocabulary (universal across the
#: integration-hub layouts in the wild: cal.com ``app-store``, Backstage
#: ``plugins``, Airbyte ``sources``/``destinations``, midday ``apps``
#: router dir, supabase ``integrations``). Directory NAMES, never paths.
HUB_PARENT_SEGMENTS: frozenset[str] = frozenset({
    "connectors", "connector",
    "integrations", "integration",
    "providers", "provider",
    "adapters", "adapter",
    "channels", "channel",
    "sources", "destinations",
    "plugins", "plugin",
    "app-store", "app_store",
    "apps",
})

#: Minimum DISTINCT vendor-named children for a dir to be a hub (spec
#: §4.4: ">= 3 vendor-named children" — two vendors is an SDK pairing,
#: three is a hub).
_MIN_VENDOR_CHILDREN = 3

#: Shared/platform PF keys (mirrors emission_integrity._SHARED_PF_KEYS —
#: kept literal here to avoid an import cycle).
_SHARED_PF_KEYS = frozenset(("shared-platform", "platform"))


def hubs_enabled() -> bool:
    """Hub/child relation — default ON, ``FAULTLINE_SPINE_HUBS=0`` off."""
    return os.environ.get(SPINE_HUBS_ENV, "1").strip().lower() not in {
        "0", "false",
    }


# ── Detection ───────────────────────────────────────────────────────────


@dataclass
class HubRelation:
    """One detected connector hub."""

    hub_dir: str                       # "backend/services/edr"
    hub_key: str                       # deterministic capability slug
    arm: str                           # "lexicon" | "vendor-majority"
    vendor_children: dict[str, list[str]] = field(default_factory=dict)
    # dev features whose own files are majority-inside the hub dir
    member_dev_names: list[str] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "hub_dir": self.hub_dir,
            "hub_key": self.hub_key,
            "arm": self.arm,
            "vendors": sorted(self.vendor_children),
            "members": list(self.member_dev_names),
        }


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").strip().strip("/")


def _segment_stem(segment: str) -> str:
    """``airtable_wrapper.ts`` → ``airtable_wrapper`` (strip extension)."""
    dot = segment.find(".")
    return segment[:dot] if dot > 0 else segment


def vendor_of_segment(segment: str) -> str | None:
    """The single vendor a child segment names, else ``None``.

    Mirrors 8.9.7's stem rule: the segment's tokens must name EXACTLY ONE
    vendor (0 → not vendor-named; >= 2 → shared plumbing).
    """
    tokens = {t for t in _split_tokens(_segment_stem(segment))
              if t in VENDOR_TOKENS}
    if len(tokens) == 1:
        return next(iter(tokens))
    return None


def _paths_of(f: Any) -> list[str]:
    return [str(p) for p in (getattr(f, "paths", None) or []) if p]


def _is_dev(f: Any) -> bool:
    return getattr(f, "layer", "developer") == "developer"


def _last_meaningful_segment(hub_dir: str) -> str:
    """The capability-naming segment of a hub dir: the LAST segment that
    is neither a connector-container word nor a generic src marker —
    ``packages/banking/src/providers`` → ``banking``;
    ``backend/services/edr`` → ``edr``. Returns ``""`` when every segment
    is generic plumbing (``apps/api/src/rest/routers`` — W1.1: the old
    ``segs[-1]`` fallback produced ``routers-apps``-style hub keys; an
    empty parent lets :func:`_hub_key` fall back to the container segment
    alone, which IS the capability for those layouts)."""
    generic = {"src", "lib", "app", "code", "internal", "rest", "api",
               "server", "packages", "package", "services", "service",
               "modules", "module", "core", "static-data", "data",
               "routers", "router", "routes", "route"}
    segs = _norm(hub_dir).split("/")
    for seg in reversed(segs):
        if seg.lower() not in HUB_PARENT_SEGMENTS and seg.lower() not in generic:
            return seg
    return ""


def _hub_key(hub_dir: str, arm: str) -> str:
    """Deterministic capability slug for a hub (composed from the dir's
    own structural segments — no free naming):

      * vendor-majority arm — the dir segment IS the capability
        (``…/edr`` → ``edr``);
      * lexicon arm — ``<parent-capability>-<container>`` when a
        meaningful parent exists (``packages/banking/src/providers`` →
        ``banking-providers``), else the container segment itself.
    """
    from faultline.pipeline_v2.emission_integrity import canonical_slug

    segs = _norm(hub_dir).split("/")
    last = segs[-1].lower()
    if arm == "vendor-majority" or last not in HUB_PARENT_SEGMENTS:
        return canonical_slug(segs[-1])
    parent = _last_meaningful_segment("/".join(segs[:-1])) if len(segs) > 1 else ""
    if parent and parent.lower() != last:
        return canonical_slug(f"{parent}-{last}")
    return canonical_slug(last)


def detect_hub_relations(
    features: Iterable[Any],
    *,
    include_memberless: bool = False,
) -> list[HubRelation]:
    """Detect connector hubs from the dev features' owned files.

    Deterministic: dirs and children iterate sorted; nested hubs collapse
    to the SHALLOWEST qualifying dir (one hub per subtree — children of a
    hub are the relation's unit, not hubs themselves).

    ``include_memberless`` (W1.1, midday ``rest/routers/apps``): by
    default a hub with no member devs binds nothing and is dropped — the
    historical contract every binding/UF consumer relies on. The 8.9.7
    carve pass alone asks for member-less hubs too: a vendor hub whose
    files all sit inside ONE covering aggregate (a workspace anchor, a
    per-workspace route merge) has no per-child grain yet — the carve
    CREATES it, after which re-detection sees members and the binding
    holds sibling parity through every later re-attribution.
    """
    if not hubs_enabled():
        return []
    devs = [f for f in features if _is_dev(f)]
    # dir → child segment → files (immediate children only).
    children_by_dir: dict[str, dict[str, list[str]]] = {}
    for f in devs:
        for p in _paths_of(f):
            norm = _norm(p)
            segs = norm.split("/")
            for i in range(len(segs) - 1):
                d = "/".join(segs[: i + 1])
                child = segs[i + 1]
                children_by_dir.setdefault(d, {}).setdefault(
                    child, [],
                ).append(norm)

    hubs: list[HubRelation] = []
    for d in sorted(children_by_dir):
        kids = children_by_dir[d]
        segs = d.split("/")
        seg = segs[-1].lower()
        vendors: dict[str, list[str]] = {}
        vendor_kids = 0
        for child in sorted(kids):
            v = vendor_of_segment(child)
            if v is not None:
                vendors.setdefault(v, []).extend(sorted(set(kids[child])))
                vendor_kids += 1
        if len(vendors) < _MIN_VENDOR_CHILDREN:
            continue
        # Depth guard (both arms): a REPO-TOP-LEVEL dir is a workspace root
        # (``apps/``, ``packages/``, a connector-monorepo's package tree),
        # never a hub — the relation targets connector dirs INSIDE a
        # package ("apps at depth>=1", spec §4.4, generalized to every
        # candidate segment).
        if len(segs) == 1:
            continue
        in_lexicon = seg in HUB_PARENT_SEGMENTS
        vendor_majority = vendor_kids * 2 >= len(kids)
        if not in_lexicon and not vendor_majority:
            continue
        arm = "lexicon" if in_lexicon else "vendor-majority"
        hubs.append(HubRelation(
            hub_dir=d,
            hub_key=_hub_key(d, arm),
            arm=arm,
            vendor_children={v: sorted(set(fs)) for v, fs in vendors.items()},
        ))

    # Collapse nested hubs: keep the shallowest qualifying dir per subtree.
    kept: list[HubRelation] = []
    for h in hubs:  # already sorted by dir (shallow dirs sort before deep)
        if any(h.hub_dir.startswith(k.hub_dir + "/") for k in kept):
            continue
        kept.append(h)

    # Member devs: OWN files majority-inside the hub dir. Facets never join.
    from faultline.pipeline_v2.spine_hygiene import is_facet

    for h in kept:
        prefix = h.hub_dir + "/"
        for f in devs:
            if is_facet(f):
                continue
            paths = _paths_of(f)
            if not paths:
                continue
            inside = sum(1 for p in paths if _norm(p).startswith(prefix))
            if inside * 2 > len(paths):
                h.member_dev_names.append(f.name)
        h.member_dev_names.sort()
    if include_memberless:
        return kept
    # A hub with no member devs binds nothing — drop it.
    return [h for h in kept if h.member_dev_names]


# ── PF binding ──────────────────────────────────────────────────────────


def _pf_key(pf: Any) -> str:
    return str(getattr(pf, "id", None) or getattr(pf, "name", "") or "")


def _display_name(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-") if w)


def apply_hub_pf_binding(
    features: list["Feature"],
    product_features: list["Feature"],
    dev_to_product_map: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enforce the hub relation on dev→PF membership (both product paths).

    For every detected hub: all member devs land on ONE product feature —
    the majority non-shared PF among them (weighted by files under the
    hub dir), else a freshly minted hub PF (appended to
    ``product_features``). Pulls hubs out of Shared Platform; re-derives
    the path/member_files unions of every affected PF. Mutates in place;
    returns telemetry.
    """
    tele: dict[str, Any] = {
        "enabled": hubs_enabled(), "hubs": 0, "devs_rebound": 0,
        "pfs_minted": 0, "hubs_detail": [],
    }
    if not hubs_enabled():
        return tele
    hubs = detect_hub_relations(features)
    if not hubs:
        return tele
    tele["hubs"] = len(hubs)

    devs_by_name: dict[str, "Feature"] = {}
    for f in features:
        if _is_dev(f) and getattr(f, "name", None):
            devs_by_name.setdefault(f.name, f)
    pf_by_key: dict[str, "Feature"] = {}
    for pf in product_features:
        key = _pf_key(pf)
        if key:
            pf_by_key.setdefault(key, pf)

    affected_pf_keys: set[str] = set()
    for h in hubs:
        members = [devs_by_name[n] for n in h.member_dev_names
                   if n in devs_by_name]
        if not members:
            continue
        prefix = h.hub_dir + "/"
        # Majority non-shared PF among members, weighted by hub-dir files.
        votes: dict[str, int] = {}
        for m in members:
            pid = getattr(m, "product_feature_id", None)
            if not pid or pid in _SHARED_PF_KEYS or pid not in pf_by_key:
                continue
            weight = sum(1 for p in _paths_of(m)
                         if _norm(p).startswith(prefix))
            votes[pid] = votes.get(pid, 0) + max(weight, 1)
        if votes:
            target_key = sorted(
                votes.items(), key=lambda kv: (-kv[1], kv[0]),
            )[0][0]
        else:
            # Mint the parent-capability PF (deterministic, composed from
            # the hub dir's own segments — never free-generated).
            from faultline.pipeline_v2.nav_taxonomy import (
                aggregate_product_feature,
            )

            target_key = h.hub_key
            if target_key not in pf_by_key:
                minted = aggregate_product_feature(
                    name=target_key,
                    display_name=_display_name(target_key),
                    description=(
                        "Connector-hub capability (Product-Spine §4.4): "
                        f"per-vendor integrations under {h.hub_dir!r} "
                        f"({', '.join(sorted(h.vendor_children))})."
                    ),
                    contrib=members,
                )
                minted.layer = "product"
                product_features.append(minted)
                pf_by_key[target_key] = minted
                tele["pfs_minted"] += 1
        rebound = 0
        for m in members:
            old = getattr(m, "product_feature_id", None)
            if old != target_key:
                if old and old in pf_by_key:
                    affected_pf_keys.add(old)
                m.product_feature_id = target_key
                if dev_to_product_map is not None:
                    dev_to_product_map[m.name] = [target_key]
                rebound += 1
        if rebound:
            affected_pf_keys.add(target_key)
        tele["devs_rebound"] += rebound
        if len(tele["hubs_detail"]) < 20:
            tele["hubs_detail"].append(
                {**h.as_telemetry(), "target_pf": target_key,
                 "devs_rebound": rebound},
            )

    # Re-derive the path/member_files unions of every PF that gained or
    # lost a member (narrow reconcile — untouched PFs stay byte-stable).
    if affected_pf_keys:
        members_by_pf: dict[str, list["Feature"]] = {}
        for f in features:
            if not _is_dev(f):
                continue
            pid = getattr(f, "product_feature_id", None)
            if pid in affected_pf_keys:
                members_by_pf.setdefault(pid, []).append(f)
        for key in sorted(affected_pf_keys):
            pf = pf_by_key.get(key)
            if pf is None:
                continue
            members = members_by_pf.get(key, [])
            merged_paths: list[str] = []
            seen: set[str] = set()
            merged_mf: list[Any] = []
            seen_mf: set[str] = set()
            for m in members:
                for p in _paths_of(m):
                    if p not in seen:
                        seen.add(p)
                        merged_paths.append(p)
                for mf in (getattr(m, "member_files", None) or []):
                    mp = (getattr(mf, "path", None)
                          if not isinstance(mf, dict) else mf.get("path"))
                    if mp and mp not in seen_mf:
                        seen_mf.add(mp)
                        merged_mf.append(mf)
            # Recompute even when the donor lost its LAST member: stale
            # paths must not keep pointing at files that moved to the hub
            # PF. A member-less, path-less donor is then a phantom and
            # emission integrity drops it (or keeps it when it still has
            # flows / is the protected platform bucket).
            pf.paths = merged_paths
            if merged_mf or (getattr(pf, "member_files", None) or []):
                pf.member_files = merged_mf
    return tele
