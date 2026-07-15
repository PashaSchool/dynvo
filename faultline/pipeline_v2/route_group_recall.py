"""W3.1 D6 — route-group journey recall seeds (fb3 dossier, validator I24).

THE HOLE CLASS: a product route-group with >= 2 pages that NO user flow
references — tracecat ``tables`` + ``chat`` (full page + router surfaces,
zero journeys), the comp Auditor group, supabase's studio holes (29 on
the keyed wave). The journeys exist in the flow graph (Stage 3 walked
the pages) but no UF cites them, so the board simply has no row for the
surface — a PM-visible recall failure no attach-quality metric catches.

THE RULE (deterministic, $0, mirrors validator I24's exact grouping):
group product-scope ``routes_index`` rows by route-file dir with
trailing param/(group) segments stripped; a group with >= 2 routes whose
files are touched by NO existing UF (member flows' entry + paths) gets
ONE thin seed journey built from the flows that enter the group's own
files. Output-only, tagged, verifier-reviewable:

  * ``synthesized: true`` + ``synthesis_reason: "route_group_recall"``
    (eval / the surfaced tier can exclude them — the
    FAULTLINE_SEED_SYSTEM_UFS precedent: board completeness, never
    silent recall inflation);
  * ``binding_confidence: "low"`` + ``name_confidence: "low"``;
  * ``product_feature_id`` = the plurality PF of the group files'
    owning devs (a REAL key — validator I12); a group with no flow
    evidence or no PF home is left as an HONEST hole (never invented).

Kill-switch: ``FAULTLINE_ROUTE_GROUP_SEED_UFS=0``.
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from typing import Any

__all__ = [
    "route_group_seeds_enabled",
    "seed_route_group_journeys",
]

#: Mirrors validator I24's ``_I23_PARAM_SEG`` — URL/file params and
#: route groups: ``[id]`` ``[[...slug]]`` ``{param}`` ``:param`` ``(grp)``.
_PARAM_SEG = re.compile(r"^(\[.*\]|\{.*\}|:.+|\(.*\))$")

#: Same member cap as the 6.7d PF-UF backstop (thin by construction).
_SEED_MEMBER_CAP = 8

_REASON = "route_group_recall"


def route_group_seeds_enabled() -> bool:
    """Default ON; ``FAULTLINE_ROUTE_GROUP_SEED_UFS=0`` disables."""
    return os.environ.get("FAULTLINE_ROUTE_GROUP_SEED_UFS", "1") != "0"


def _flow_member_id(flow: Any) -> str:
    """uuid-else-name — the Stage 6.7 ``_flow_key`` id space."""
    return getattr(flow, "uuid", "") or getattr(flow, "name", "") or ""


def _group_dir(route_file: str) -> str:
    """The route file's dir with trailing param/group segments stripped
    (validator I24's grouping, formula-identical)."""
    segs = route_file.split("/")[:-1]
    while segs and _PARAM_SEG.match(segs[-1]):
        segs.pop()
    return "/".join(segs)


def _clean_seg(seg: str) -> str:
    """A dir/url segment humanized for the seed name: Remix ``+`` layout
    suffixes and convention-private ``_`` prefixes dropped."""
    return seg.rstrip("+").lstrip("_")


def _group_noun(group_dir: str, patterns: list[str]) -> str:
    """The group's display noun: the deepest concrete segment shared by
    the group's URL patterns, else the cleaned dir basename."""
    seg_lists = []
    for pat in patterns:
        segs = [s for s in (pat or "").split("/")
                if s and not _PARAM_SEG.match(s) and not s.startswith(
                    ("[", "{", ":", "$", "<", "*", "(", "@"))]
        if segs:
            seg_lists.append(segs)
    if seg_lists:
        first = seg_lists[0]
        k = len(first)
        for other in seg_lists[1:]:
            j = 0
            while j < min(k, len(other)) and other[j] == first[j]:
                j += 1
            k = j
        if k:
            return first[k - 1]
    base = _clean_seg(group_dir.rsplit("/", 1)[-1] if "/" in group_dir
                      else group_dir)
    return base or "pages"


def _humanize(noun: str) -> str:
    return re.sub(r"[-_]+", " ", noun).strip()


def seed_route_group_journeys(
    user_flows: list[Any],
    features: list[Any],
    product_features: list[Any],
    flows: list[Any],
    routes_index: list[dict[str, Any]] | None,
    scope_classifier: Any = None,
    route_by_file: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one thin tagged seed UF per uncovered >=2-route product
    group (mutates ``user_flows`` in place; ids continue the stable
    UF-xxx numbering). Returns telemetry.

    W4.2 Fix 2 (seed surface-guard): when *scope_classifier* is given
    (a ``SurfaceScopeClassifier``), product features whose own evidence
    classifies non-product (marketing / docs / legal / dev_tooling /
    shell) are EXCLUDED from the home vote — a seed journey never homes
    onto a non-product surface. A group whose only homes are
    non-product is an honest hole (``skipped_non_product_home``).
    """
    tele: dict[str, Any] = {
        "groups": 0, "groups_ge2": 0, "holes": 0,
        "seeded": 0, "skipped_no_flows": 0, "skipped_no_pf": 0,
        "skipped_non_product_home": 0,
        "seeds": [],
    }
    if not routes_index or not isinstance(routes_index, list):
        return tele

    nonprod_pf_keys: set[str] = set()
    if scope_classifier is not None:
        from faultline.pipeline_v2.surface_taxonomy import (
            NON_PRODUCT_PF_SCOPES,
        )
        for pf in product_features:
            key = str(getattr(pf, "name", "") or "")
            if not key:
                continue
            try:
                scope = scope_classifier.classify_feature(
                    pf, route_by_file or {})
            except Exception:  # noqa: BLE001 — guard is best-effort
                continue
            if scope in NON_PRODUCT_PF_SCOPES:
                nonprod_pf_keys.add(key)

    # 1. product route groups (I24 grouping).
    # B58 v3 — dev-artifact top-level dirs never count as PRODUCT route
    # groups (novu ``playground/nextjs/src/pages/api`` appeared as an I24
    # hole once the annexation guard barred playground PFs — the routes
    # of a sample app are not product recall debt). TOP-LEVEL grain only:
    # a product page under apps/web/**/playground/ keeps counting.
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        annexation_guard_enabled as _b58_on,
    )
    _art_tokens: frozenset[str] = frozenset()
    if _b58_on():
        from faultline.pipeline_v2.spine_anchors import load_spine_vocab
        _art_tokens = frozenset(
            str(t).strip().lower()
            for t in (load_spine_vocab().get("unit_root_artifact_tokens")
                      or ())
            if str(t).strip())
    groups: dict[str, dict[str, Any]] = {}
    for r in routes_index:
        if not isinstance(r, dict):
            continue
        if r.get("surface_scope") not in (None, "product"):
            continue
        f = str(r.get("file") or "")
        if not f:
            continue
        if _art_tokens and f.split("/", 1)[0].lower() in _art_tokens:
            tele["skipped_dev_artifact"] = (
                tele.get("skipped_dev_artifact", 0) + 1)
            continue
        g = _group_dir(f)
        e = groups.setdefault(g, {"n": 0, "files": set(), "patterns": []})
        e["n"] += 1
        e["files"].add(f)
        if r.get("pattern"):
            e["patterns"].append(str(r["pattern"]))
    tele["groups"] = len(groups)
    tele["groups_ge2"] = sum(1 for e in groups.values() if e["n"] >= 2)

    # 2. files already touched by ANY user flow's member flows.
    flow_by_id = { _flow_member_id(fl): fl for fl in (flows or []) }
    touched: set[str] = set()
    for uf in user_flows:
        for mid in (getattr(uf, "member_flow_ids", None) or []):
            fl = flow_by_id.get(str(mid))
            if fl is None:
                continue
            ep = getattr(fl, "entry_point_file", None)
            if ep:
                touched.add(str(ep))
            touched.update(str(p) for p in (getattr(fl, "paths", None) or []))

    # 3. file -> PF plurality channels (dev ownership first, PF paths as
    # the fallback) — the seed must cite a REAL PF key (validator I12).
    pf_keys = {str(getattr(pf, "name", "") or "") for pf in product_features}
    pf_keys.discard("")
    pf_keys -= nonprod_pf_keys  # Fix 2: non-product homes never receive seeds
    file_dev_pf: dict[str, list[str]] = defaultdict(list)
    nonprod_touch: dict[str, int] = defaultdict(int)
    for feat in features:
        if getattr(feat, "layer", "developer") != "developer":
            continue
        pfid = getattr(feat, "product_feature_id", None)
        if not pfid:
            continue
        if str(pfid) in nonprod_pf_keys:
            for p in (getattr(feat, "paths", None) or []):
                nonprod_touch[str(p)] += 1
            continue
        if str(pfid) not in pf_keys:
            continue
        for p in (getattr(feat, "paths", None) or []):
            file_dev_pf[str(p)].append(str(pfid))
    file_pf_paths: dict[str, list[str]] = defaultdict(list)
    for pf in product_features:
        key = str(getattr(pf, "name", "") or "")
        if not key:
            continue
        if key in nonprod_pf_keys:
            for p in (getattr(pf, "paths", None) or []):
                nonprod_touch[str(p)] += 1
            continue
        for p in (getattr(pf, "paths", None) or []):
            file_pf_paths[str(p)].append(key)

    # 4. seed each hole.
    max_id = 0
    for uf in user_flows:
        m = re.match(r"^UF-(\d+)$", str(getattr(uf, "id", "") or ""))
        if m:
            max_id = max(max_id, int(m.group(1)))
    existing_names = {
        (getattr(uf, "name", "") or "").strip().lower() for uf in user_flows
    }

    from faultline.models.types import UserFlow

    seeds: list[Any] = []
    for g in sorted(groups):
        e = groups[g]
        if e["n"] < 2:
            continue
        files: set[str] = e["files"]
        if any(f in touched for f in files):
            continue
        tele["holes"] += 1
        # member flows: entry inside the group's files first, then any
        # flow whose span crosses them.
        members: list[tuple[int, str]] = []
        seen_mids: set[str] = set()
        for fl in flows or []:
            mid = _flow_member_id(fl)
            if not mid or mid in seen_mids:
                continue
            ep = str(getattr(fl, "entry_point_file", "") or "")
            span = {str(p) for p in (getattr(fl, "paths", None) or [])}
            if ep in files or (span & files):
                seen_mids.add(mid)
                rank = 0 if ep in files else 1
                members.append((rank, mid))
        if not members:
            tele["skipped_no_flows"] += 1
            continue  # honest hole — nothing to cite
        members.sort(key=lambda t: (t[0], t[1]))
        member_ids = [mid for _, mid in members[:_SEED_MEMBER_CAP]]
        # PF home: plurality over the group files' owning devs.
        votes: Counter[str] = Counter()
        for f in sorted(files):
            for pfid in file_dev_pf.get(f, ()):
                votes[pfid] += 1
        if not votes:
            for f in sorted(files):
                for pfid in file_pf_paths.get(f, ()):
                    votes[pfid] += 1
        if not votes:
            # Fix 2: distinguish "no home at all" from "only non-product
            # homes" — both are honest holes, the reason is telemetry.
            if any(nonprod_touch.get(f) for f in files):
                tele["skipped_non_product_home"] += 1
            else:
                tele["skipped_no_pf"] += 1
            continue  # honest hole — no PRODUCT home to cite
        (_top, n), = votes.most_common(1)
        pf_home = sorted(k for k, v in votes.items() if v == n)[0]
        noun = _humanize(_group_noun(g, e["patterns"]))
        name = f"Browse & manage {noun}"
        if name.strip().lower() in existing_names:
            name = f"Browse & manage {noun} ({_clean_seg(g.rsplit('/', 1)[-1])})"
        existing_names.add(name.strip().lower())
        seeds.append(UserFlow(
            id="UF-000",  # provisional — renumbered below
            name=name,
            resource=noun,
            domain=None,
            product_feature_id=pf_home,
            intent="browse",
            member_flow_ids=member_ids,
            member_count=len(member_ids),
            routes=[],
            refined=True,
            name_confidence="low",
            binding_confidence="low",
            synthesized=True,
            synthesis_reason=_REASON,
        ))
        if len(tele["seeds"]) < 25:
            tele["seeds"].append({"group": g, "pf": pf_home,
                                  "members": len(member_ids), "name": name})

    # B69-v2 — same-(pf,resource) seed coalescence: an API-side group and a
    # page-side group of the SAME noun homed to the SAME PF are one product
    # journey split by transport, not two ('conversations' under
    # pages/api/teams/.../datarooms/[id]/ AND pages/datarooms/[id]/ — the
    # keyed A/B showed the twin pair colliding at naming and cascading into
    # labeler twin-picks + B31 parenthetical qualifiers downstream).
    # Coalesce at BIRTH: the first-seen group (sorted order — deterministic)
    # keeps its identity and base name; later twins fold their member flows
    # in (deduped, re-capped). Cross-PF same-noun seeds are NOT coalesced
    # (different homes = honest separate journeys). Flag OFF ⇒
    # byte-identical.
    from faultline.pipeline_v2.naming_contract import homing_hygiene_enabled
    if homing_hygiene_enabled() and seeds:
        by_key: dict[tuple[str, str], Any] = {}
        kept: list[Any] = []
        tele["coalesced"] = 0
        for uf in seeds:
            key = (str(uf.product_feature_id or ""),
                   str(uf.resource or "").strip().lower())
            first = by_key.get(key)
            if first is None or not key[1]:
                by_key[key] = uf
                kept.append(uf)
                continue
            merged = list(first.member_flow_ids or [])
            for mid in (uf.member_flow_ids or []):
                if mid not in merged:
                    merged.append(mid)
            first.member_flow_ids = merged[:_SEED_MEMBER_CAP]
            first.member_count = len(first.member_flow_ids)
            tele["coalesced"] += 1
            tele.setdefault("coalesced_seeds", []).append({
                "pf": key[0], "resource": key[1],
                "dropped_name": str(uf.name or ""),
                "into_name": str(first.name or ""),
            })
        seeds = kept

    seeds.sort(key=lambda u: ((u.name or "").lower(), str(u.resource or "")))
    for i, uf in enumerate(seeds, start=1):
        uf.id = f"UF-{max_id + i:03d}"
    user_flows.extend(seeds)
    tele["seeded"] = len(seeds)
    return tele
