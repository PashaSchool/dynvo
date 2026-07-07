"""Stage 6.87 — Lane excavation (Product-Spine W4.3).

Lifts PRODUCT out of the ``platform_infrastructure`` lane. The W3.1
lane law is correct — app shells must never force-bind to a single
capability — but the corpus measurement (w43-diagnosis, 2026-07-07)
showed 1.87M LOC / 1174 flows parked in ``shell_lineage_only`` lane
residents whose CONTENT is per-domain product code:

  * shells own ~zero route files (routes were carved into PFs long
    ago), so the shell's product mass lives in DOMAIN DIRS —
    ``apps/studio/data/<domain>``, ``components/interfaces/<Domain>``;
  * a shell's flows often enter through files of SIBLING lane devs
    (supabase ``studio``: 396/478 flow entries live in lane-dev
    ``data``'s files) — the honest excavation unit is the APP-SHELL
    GROUP of lane devs, never one dev;
  * the 8.9.x sub-splits that the mint laned (``auth-2``,
    ``storage-2`` …) are whole-dev movers once a finer anchor exists.

Mechanism (nothing re-invented):

  1. Group ``shell_lineage_only`` lane devs by app root; groups with at
     least one flowful member are input, flowless members are content
     donors.
  2. Build DOMAIN-DIR candidate anchors from the group's own files
     (``source="excav"``, ranked LAST) and merge them with the repo's
     EXISTING anchor set via the standard same-key cross-source merge —
     a ``data/organizations`` candidate unions with the route/interior
     anchors of the same capability and inherits their page evidence.
  3. Every candidate faces the EXISTING Stage-6.86 mint bar
     (``_mint_bar`` — shell / barred / instrument / page-surface /
     single-container guards all apply unchanged) plus two excavation
     floors: ≥150 code-LOC (the W3.1 D4 husk bound) and no 0-flow mint
     unless the merged anchor is multi-source-confirmed.
  4. Whole lane devs that now classify UNIQUE to a passing anchor move
     as-is; shells that still span many domains are CARVED — an
     8.9.x-style sub-feature takes the anchor's subtree files and the
     flows whose entry files live inside it; the shell keeps the honest
     residual and stays in the lane.
  5. Anchors that already minted a PF WIDEN it (members join the
     existing capability); the rest mint through the same
     ``aggregate_product_feature`` path Pass 4 uses. Runs BEFORE the
     flow-span split / 6.7d journey family, so every downstream
     consumer sees the richer PF set with no new logic.

Anti-sink by construction: a carve takes ONLY lane-group files (new
PFs' outside-share is 0); whole-dev moves need the same θ=0.5 majority
as every classification; files owned by non-lane devs in the same dirs
are untouchable.

Deterministic, $0 LLM, scale-invariant. Kill-switch:
``FAULTLINE_LANE_EXCAVATION=0`` → scans byte-identical to pre-W4.3.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.spine_anchors import (
    SpineAnchor,
    build_spine_anchors,
    load_spine_vocab,
    normalize_anchor_key,
    owned_paths_of,
)
from faultline.pipeline_v2.spine_anchors import (
    _merge_anchors as merge_anchors,
)
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    _classify_dev,
    _files_loc,
    _flow_evidence_index,
    _mint_bar,
    _slug,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature, FeatureFlowEdge, Flow

__all__ = [
    "LANE_EXCAVATION_ENV",
    "lane_excavation_enabled",
    "run_lane_excavation",
]

LANE_EXCAVATION_ENV = "FAULTLINE_LANE_EXCAVATION"

_SHELL_REASON = "shell_lineage_only"

#: provenance marker (idempotence + I22 explainability).
_EXCAV_MARKER = "lane-excavation"

#: W3.1 D4 bound reused as the excavation mint floor.
_CODE_LOC_FLOOR = 150

#: a domain-dir CANDIDATE needs only ≥1 code file — the floors act on
#: the carved CHUNK (which aggregates every same-key prefix of the
#: merged anchor: ``data/auth`` + ``interfaces/Auth`` clear the floor
#: TOGETHER even when each dir alone would not). Junk single-file dirs
#: die at the chunk floor / the existing mint bar, not here.

#: UI-SHAPE dirs: structural component containers whose NAME is never a
#: capability (midday forms/tables/modals class — H2 name trap). They
#: are transparent when walking for the domain segment.
_UI_SHAPE_SEGMENTS = frozenset({
    "forms", "form", "tables", "table", "modals", "modal", "sheets",
    "sheet", "dialogs", "dialog", "widgets", "widget", "buttons",
    "button", "icons", "icon", "layouts", "layout", "charts", "chart",
    "cards", "card", "lists", "list", "menus", "menu", "inputs",
    "panels", "panel", "common", "shared", "core", "base", "misc", "ui",
    "primitives", "elements",
})

#: dirs whose content is data/static/vendored — never candidates and
#: their files never count (monaco-editor 44.8K / cert bundles /
#: template JSON traps, measured in the diagnosis).
_STATIC_ROOTS = frozenset({
    "public", "static", "assets", "spec", "content", "vendor",
    "vendored", "third_party", "node_modules", "fixtures", "fonts",
    "locales", "i18n", "messages", "migrations", "__registry__",
    "registry", "templates", "generated", "dist", "build", "scripts",
    "__tests__", "test", "tests", "e2e", "cypress", "__mocks__",
})

#: structural segments skipped (transparent) while walking for the
#: first domain-capable segment — the measured lane grain.
_TRANSPARENT_SEGMENTS = frozenset({
    "src", "app", "apps", "pages", "components", "interfaces", "lib",
    "libs", "hooks", "data", "features", "modules", "services",
    "service", "utils", "helpers", "api", "routes", "routers", "store",
    "stores", "state", "types", "constants", "styles", "packages",
    "actions", "queries", "mutations", "views",
}) | _UI_SHAPE_SEGMENTS


def lane_excavation_enabled() -> bool:
    """Default ON; ``FAULTLINE_LANE_EXCAVATION=0`` restores pre-W4.3
    output byte-identically."""
    return os.environ.get(LANE_EXCAVATION_ENV, "1").strip().lower() not in {
        "0", "false",
    }


# ── Lane grouping ────────────────────────────────────────────────────────


def _app_root_of(owned: list[str]) -> str | None:
    """The dev's app-shell root: the majority 2-segment prefix
    (``apps/studio``) or, failing that, the majority 1-segment
    manifest-unit dir (Soc0 ``backend``)."""
    if not owned:
        return None
    two: Counter[str] = Counter()
    for p in owned:
        segs = p.split("/")
        two["/".join(segs[:2]) if len(segs) > 1 else segs[0]] += 1
    root, n = two.most_common(1)[0]
    if n * 2 > len(owned):
        return root
    one: Counter[str] = Counter(p.split("/", 1)[0] for p in owned)
    root1, n1 = one.most_common(1)[0]
    if n1 * 2 > len(owned):
        return root1
    return None


def _lane_groups(
    lane_devs: list["Feature"],
) -> dict[str, list["Feature"]]:
    """shell-laned devs grouped by app root; only groups with ≥1
    flowful member excavate (flowless members are content donors)."""
    by_root: dict[str, list["Feature"]] = defaultdict(list)
    for f in lane_devs:
        root = _app_root_of(owned_paths_of(f))
        if root:
            by_root[root].append(f)
    return {
        root: sorted(by_root[root], key=lambda f: f.name)
        for root in sorted(by_root)
        if any(getattr(f, "flows", None) for f in by_root[root])
    }


# ── Candidate derivation (the domain-dir channel) ────────────────────────


def _is_code(path: str, code_exts: tuple[str, ...]) -> bool:
    return path.lower().endswith(code_exts)


def _domain_prefix_of(path: str, root: str) -> tuple[str, str] | None:
    """``(normalized_key, dir_prefix)`` of the first non-structural,
    non-dynamic dir segment after the app root; ``None`` for files in
    static subtrees or with no domain-capable segment."""
    if not path.startswith(root + "/"):
        return None
    rel = path[len(root) + 1:]
    segs = rel.split("/")[:-1]
    prefix_parts = [root]
    for seg in segs:
        prefix_parts.append(seg)
        low = seg.lower()
        if low in _STATIC_ROOTS:
            return None  # static subtree — never a candidate
        if low in _TRANSPARENT_SEGMENTS:
            continue
        if seg.startswith(("(", "[", "_", ".", "$", "@")):
            continue
        key = normalize_anchor_key(seg)
        if not key:
            return None
        return key, "/".join(prefix_parts)
    return None


def _excav_anchors(
    group_files: list[str],
    root: str,
    code_exts: tuple[str, ...],
    stoplist: frozenset[str],
) -> list[SpineAnchor]:
    """Domain-dir candidate sub-anchors from the lane group's own
    content (stoplist-guarded; floors act later, on the carved chunk)."""
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    for p in sorted(group_files):
        if not _is_code(p, code_exts):
            continue
        hit = _domain_prefix_of(p, root)
        if hit is None:
            continue
        key, prefix = hit
        alnum = re.sub(r"[^a-z0-9]+", "", key)
        if len(alnum) < 3 or key in stoplist or alnum in stoplist:
            continue
        slot = acc.setdefault((key, prefix), {"files": []})
        slot["files"].append(p)
    out: list[SpineAnchor] = []
    for (key, prefix) in sorted(acc):
        out.append(SpineAnchor(
            canonical_id=f"excav:{prefix}",
            key=key,
            source="excav",
            display=_display_of(prefix.rsplit("/", 1)[-1]),
            prefixes=(prefix,),
            sources=frozenset({"excav"}),
        ))
    return out


def _display_of(raw: str) -> str:
    words = re.split(
        r"[-_\s]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", raw).strip())
    return " ".join(
        w if (w.isupper() and len(w) > 1) else w.capitalize()
        for w in words if w
    ) or raw


# ── Carve mechanics (8.9.x discipline) ───────────────────────────────────


def _make_excav_dev(
    shell: "Feature", prefix: str, files: list[str], name: str,
) -> "Feature":
    """A carved chunk-dev — mirrors 8.9.x ``_make_subfeature`` (owned
    primary members, content-derived uuid, ``split_from`` lineage) with
    the excavation marker. Flows move separately by entry file."""
    from faultline.models.types import MemberFile

    owned_members = [
        MemberFile(
            path=p, role="anchor", confidence=1.0, primary=True,
            evidence=f"{_EXCAV_MARKER} of '{shell.name}'",
        )
        for p in sorted(files)
    ]
    return shell.model_copy(deep=True, update={
        "name": name,
        "display_name": name,
        "paths": sorted(files),
        "member_files": owned_members,
        "description": (
            f"{_EXCAV_MARKER} '{prefix}' of lane shell '{shell.name}'"
        ),
        # content-derived — uuid4 would churn byte-identity (det arc).
        "uuid": hashlib.sha256(
            f"excav-v1|{getattr(shell, 'uuid', '') or shell.name}|"
            f"{prefix}|{name}".encode("utf-8")).hexdigest()[:32],
        "split_from": getattr(shell, "uuid", None),
        "previous_names": [],
        "merged_from": [],
        "total_commits": 0,
        "bug_fixes": 0,
        "bug_fix_ratio": 0.0,
        "flows": [],
        "shared_participants": [],
        "shared_attributions": [],
        "symbol_attributions": [],
        "hotspot_files": [],
        "participants": [],
        "history": None,
        "shared_reason": None,
    })


def _remove_files_from_shell(shell: "Feature", moved: set[str]) -> None:
    """Drop *moved* from the shell's ``paths`` and ``member_files`` —
    the chunk's primary claim is the truth now (Stage 8.8 re-adds
    shared claims where imports warrant them)."""
    shell.paths = [p for p in (shell.paths or []) if p not in moved]
    mfs = getattr(shell, "member_files", None) or []
    kept = []
    for m in mfs:
        p = m.get("path") if isinstance(m, dict) else getattr(m, "path", None)
        if p not in moved:
            kept.append(m)
    shell.member_files = kept


def _move_flows(
    shell: "Feature",
    chunk: "Feature",
    anchor: SpineAnchor,
    edges_by_flow_id: dict[str, list["FeatureFlowEdge"]],
) -> int:
    """Move the shell's flows whose ENTRY file sits inside *anchor*'s
    subtree onto *chunk*; re-stamp the bipartite identity fields
    (``primary_feature`` / ``id``) and fix the affected edges so the
    Stage-5.5 projection stays consistent."""
    moved = 0
    keep: list["Flow"] = []
    for fl in getattr(shell, "flows", None) or []:
        ep = str(getattr(fl, "entry_point_file", "") or "")
        if not ep or not anchor.matches(ep):
            keep.append(fl)
            continue
        old_id = getattr(fl, "id", None)
        chunk.flows.append(fl)
        fl.primary_feature = chunk.name
        new_id = f"{chunk.name}::{fl.name}"
        fl.id = new_id
        for e in edges_by_flow_id.get(old_id or "", []):
            if e.type == "primary":
                e.feature = chunk.name
            e.flow_id = new_id
        if old_id and old_id in edges_by_flow_id:
            edges_by_flow_id[new_id] = edges_by_flow_id.pop(old_id)
        moved += 1
    shell.flows = keep
    return moved


# ── Public entrypoint ────────────────────────────────────────────────────


def run_lane_excavation(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    routes_index: list[dict[str, Any]] | None,
    ctx: Any,
    extractor_signals: dict[str, list[Any]] | None = None,
    nav_keys: frozenset[str] = frozenset(),
    instrument_dirs: frozenset[str] = frozenset(),
    feature_flow_edges: list["FeatureFlowEdge"] | None = None,
) -> dict[str, Any]:
    """See module docstring. Mutates devs / ``product_features`` /
    edges in place; appends carved chunk-devs to ``developer_features``;
    returns telemetry for ``scan_meta.lane_excavation``."""
    from faultline.pipeline_v2.nav_taxonomy import aggregate_product_feature
    from faultline.pipeline_v2.spine_hygiene import is_facet

    tele: dict[str, Any] = {
        "enabled": True, "applied": False,
        "groups": 0, "shells_processed": 0, "candidates": 0,
        "devs_moved": 0, "devs_carved": 0, "chunks": 0,
        "pfs_minted": 0, "pfs_widened": 0,
        "loc_excavated": 0, "flows_excavated": 0,
        "residual_lane_loc": 0, "residual_lane_flows": 0,
        "guard_blocks": {}, "samples": [],
    }

    def _block(reason: str) -> None:
        gb = tele["guard_blocks"]
        gb[reason] = gb.get(reason, 0) + 1

    devs = [
        f for f in developer_features
        if getattr(f, "layer", "developer") == "developer"
        and getattr(f, "name", None)
    ]
    in_scope = [f for f in devs if not is_facet(f)]
    lane_shells = [
        f for f in in_scope
        if getattr(f, "product_feature_id", None) is None
        and getattr(f, "shared_reason", None) == _SHELL_REASON
    ]
    groups = _lane_groups(lane_shells)
    tele["groups"] = len(groups)
    if not groups:
        return tele

    vocab = load_spine_vocab()
    code_exts = tuple(vocab.get("code_extensions") or [])
    stoplist = frozenset(
        str(s) for s in (vocab.get("structural_stoplist") or []))
    repo_root = Path(getattr(ctx, "repo_path", "."))
    loc_cache: dict[str, int] = {}

    # (2) candidate anchors: the group's domain dirs, merged with the
    # repo's existing anchor set (route/interior/schema page evidence
    # rides the same-key merge).
    base = build_spine_anchors(
        in_scope, routes_index, ctx, extractor_signals, nav_keys)
    excav: list[SpineAnchor] = []
    group_union: dict[str, list[str]] = {}
    for root, members in groups.items():
        union = sorted({p for m in members for p in owned_paths_of(m)})
        group_union[root] = union
        excav.extend(_excav_anchors(union, root, code_exts, stoplist))
    tele["candidates"] = len(excav)
    if not excav:
        return tele

    merged = merge_anchors(base + excav)
    anchor_by_id = {a.canonical_id: a for a in merged}
    excav_backed = [
        a for a in merged
        if "excav" in a.sources and not a.shell and not a.barred
    ]
    repo_has_pages = any(a.page_route_files for a in merged)
    flow_entries, _flowful = _flow_evidence_index(in_scope)

    edges_by_flow_id: dict[str, list["FeatureFlowEdge"]] = defaultdict(list)
    for e in (feature_flow_edges or []):
        edges_by_flow_id[e.flow_id].append(e)

    pf_by_anchor = {
        getattr(pf, "anchor_id", None): pf
        for pf in product_features if getattr(pf, "anchor_id", None)
    }
    used_slugs = {getattr(pf, "name", "") for pf in product_features}
    used_slugs |= {"platform", "shared-platform"}
    used_dev_names = {f.name for f in devs}

    def _passes_floors(
        anchor: SpineAnchor, files: list[str], has_flows: bool,
    ) -> bool:
        code_files = [p for p in files if _is_code(p, code_exts)]
        if _files_loc(repo_root, code_files, loc_cache) < _CODE_LOC_FLOOR:
            _block("code_loc_floor")
            return False
        if not has_flows and len(anchor.sources) < 2:
            # a 0-flow chunk mints only with multi-source confirmation
            # (diagnosis R6 — the I8 journeys-worthy class).
            _block("zero_flow_single_source")
            return False
        return True

    # winners per anchor accumulate across groups; PF emission is one
    # deterministic pass at the end.
    contrib_by_anchor: dict[str, list["Feature"]] = defaultdict(list)

    for root in sorted(groups):
        members = groups[root]
        for dev in members:
            owned = owned_paths_of(dev)
            if not owned:
                continue
            flows = list(getattr(dev, "flows", None) or [])
            tele["shells_processed"] += 1

            # (4a) whole-dev move — the dev now classifies UNIQUE to a
            # passing capability anchor (the *-2 split class).
            winner, share, verdict, _plur = _classify_dev(owned, merged)
            if (winner is not None and verdict in {"unique", "near_tie"}
                    and not winner.shell and not winner.barred):
                bar = _mint_bar(
                    winner, [dev], flow_entries, repo_has_pages,
                    code_exts, repo_root, loc_cache,
                    instrument_dirs=instrument_dirs)
                if bar is None and _passes_floors(
                        winner, owned, bool(flows)):
                    contrib_by_anchor[winner.canonical_id].append(dev)
                    tele["devs_moved"] += 1
                    continue
                if bar is not None:
                    _block(f"bar:{bar}")

            # (4b) carve — partition the dev's files over the
            # excav-backed anchors; most specific prefix wins.
            by_anchor: dict[str, list[str]] = defaultdict(list)
            for p in owned:
                best: SpineAnchor | None = None
                best_len = -1
                for a in excav_backed:
                    for pre in a.prefixes:
                        if ((p == pre or p.startswith(pre + "/"))
                                and len(pre) > best_len):
                            best, best_len = a, len(pre)
                if best is not None:
                    by_anchor[best.canonical_id].append(p)
            if not by_anchor:
                continue
            total_carved: set[str] = set()
            chunks_here: list[tuple[SpineAnchor, list[str]]] = []
            for cid in sorted(by_anchor):
                a = anchor_by_id[cid]
                files = sorted(by_anchor[cid])
                flows_in = [
                    fl for fl in flows
                    if (ep := getattr(fl, "entry_point_file", None))
                    and a.matches(str(ep))
                ]
                if not _passes_floors(a, files, bool(flows_in)):
                    continue
                bar = _mint_bar(
                    a, [dev], flow_entries, repo_has_pages, code_exts,
                    repo_root, loc_cache, instrument_dirs=instrument_dirs)
                if bar is not None:
                    _block(f"bar:{bar}")
                    continue
                chunks_here.append((a, files))
                total_carved.update(files)
            if not chunks_here:
                continue
            # never empty the source (8.9.x zero-path protection): keep
            # the smallest chunk laned when the carve would take all.
            if len(total_carved) >= len(owned) and len(chunks_here) > 1:
                chunks_here.sort(key=lambda t: (len(t[1]), t[0].canonical_id))
                dropped = chunks_here.pop(0)
                total_carved.difference_update(dropped[1])
            elif len(total_carved) >= len(owned):
                _block("would_empty_shell")
                continue

            tele["devs_carved"] += 1
            for a, files in sorted(
                    chunks_here, key=lambda t: t[0].canonical_id):
                base_name = _slug(a.display) or a.key
                name = base_name
                if name in used_dev_names:
                    name = _slug(f"{base_name} {a.key}")
                    n = 2
                    while name in used_dev_names:
                        name = f"{base_name}-{n}"
                        n += 1
                used_dev_names.add(name)
                prefix = a.prefixes[0] if a.prefixes else a.canonical_id
                chunk = _make_excav_dev(dev, prefix, files, name)
                moved_flows = _move_flows(dev, chunk, a, edges_by_flow_id)
                _remove_files_from_shell(dev, set(files))
                developer_features.append(chunk)
                contrib_by_anchor[a.canonical_id].append(chunk)
                tele["chunks"] += 1
                tele["flows_excavated"] += moved_flows
                tele["loc_excavated"] += _files_loc(
                    repo_root,
                    [p for p in files if _is_code(p, code_exts)],
                    loc_cache)
                if len(tele["samples"]) < 20:
                    tele["samples"].append({
                        "shell": dev.name, "anchor": a.canonical_id,
                        "chunk": name, "files": len(files),
                        "flows": moved_flows,
                    })

    # (5) emission — widen existing PFs, mint the rest via the exact
    # Pass-4 path.
    for cid in sorted(contrib_by_anchor):
        a = anchor_by_id[cid]
        contrib = contrib_by_anchor[cid]
        existing = pf_by_anchor.get(cid)
        if existing is not None:
            slug = existing.name
            mf_out = list(getattr(existing, "member_files", None) or [])
            seen_mf = {
                (m.get("path") if isinstance(m, dict)
                 else getattr(m, "path", None))
                for m in mf_out
            }
            paths_out = list(existing.paths or [])
            seen_paths = set(paths_out)
            for c in contrib:
                for m in (getattr(c, "member_files", None) or []):
                    mp = (m.get("path") if isinstance(m, dict)
                          else getattr(m, "path", None))
                    if mp and mp not in seen_mf:
                        seen_mf.add(mp)
                        mf_out.append(m)
                for p in c.paths or []:
                    if p not in seen_paths:
                        seen_paths.add(p)
                        paths_out.append(p)
            existing.member_files = mf_out
            existing.paths = paths_out
            tele["pfs_widened"] += 1
        else:
            slug = _slug(a.display)
            if slug in used_slugs:
                slug = _slug(f"{a.display} ({a.key})")
                if slug in used_slugs:
                    slug = _slug(
                        f"{a.display} "
                        f"({re.sub(r'[^A-Za-z0-9]+', ' ', cid).strip()})")
            used_slugs.add(slug)
            pf = aggregate_product_feature(
                name=slug,
                display_name=a.display,
                description=(
                    f"Capability anchored at {cid} "
                    f"(sources: {', '.join(sorted(a.sources))}; "
                    f"{len(contrib)} developer feature(s); "
                    f"{_EXCAV_MARKER})."
                ),
                contrib=contrib,
            )
            pf.layer = "product"
            pf.anchor_id = cid
            seen_mf2: set[str] = set()
            merged_mf: list[Any] = []
            for c in contrib:
                for m in (getattr(c, "member_files", None) or []):
                    mp = (m.get("path") if isinstance(m, dict)
                          else getattr(m, "path", None))
                    if mp and mp not in seen_mf2:
                        seen_mf2.add(mp)
                        merged_mf.append(m)
            if merged_mf:
                pf.member_files = merged_mf
            product_features.append(pf)
            pf_by_anchor[cid] = pf
            tele["pfs_minted"] += 1
        for c in contrib:
            c.product_feature_id = slug
            c.anchor_id = f"fold:excavation->{cid}"
            if getattr(c, "shared_reason", None):
                c.shared_reason = None

    # residual accounting (post-carve lane truth for the report).
    for f in lane_shells:
        if getattr(f, "product_feature_id", None) is None:
            code_files = [
                p for p in owned_paths_of(f) if _is_code(p, code_exts)]
            tele["residual_lane_loc"] += _files_loc(
                repo_root, code_files, loc_cache)
            tele["residual_lane_flows"] += len(
                getattr(f, "flows", None) or [])

    tele["applied"] = bool(tele["pfs_minted"] or tele["pfs_widened"])
    return tele
