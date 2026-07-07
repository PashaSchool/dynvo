"""Stage 6.86 — Anchored PF minting (Product-Spine §4.3, Wave 2b).

THE CORE INVARIANT: a product feature is a named node over an existing
L1 subtree; dev→PF membership derives DETERMINISTICALLY from anchor
lineage. No LLM may move membership (6.7d Call-2, the per-item
classification oracle with the Shared-Platform sink — rootcause RC1 —
is retired behind this stage's flag).

Lineage classification (calibration verdict 2026-07-06, GO):
  * population — every developer feature (``role != facet``); owned
    files = primary ``member_files`` (fallback ``paths``);
  * share(dev, anchor) = |owned ∩ subtree| / |owned|; majority set at
    **θ = 0.5**;
  * SPECIFICITY REDUCTION — a majority anchor whose per-dev matched set
    strictly contains another majority anchor's matched set is dropped
    (route beats enclosing workspace); identical matched sets merge
    (keep the higher-ranked source);
  * UNIQUE when one candidate remains or the top beats the runner-up by
    > 10 pp; near-ties resolve by the fixed ``SOURCE_RANK`` order with
    nav-confirmation as the first tie-break (nav is a ranking confirmer,
    never a subtree source);
  * NONE → the residual ladder (below).

Mint bar (which winning anchors become product features):
  * shells (``ws-app``) never mint — rider R1;
  * ``single_letter`` / ``version_dir`` keys never mint (midday i/p/r/s,
    linkwarden v1/v2) — their devs FOLD;
  * service-dir–only anchors never standalone-mint (operator case:
    Soc0 ``widget-query``);
  * PAGE-SURFACE RULE — in a repo that has ≥ 1 PAGE route, an anchor
    whose only surface evidence is API routes / router files / schema
    matches is an implementation surface, not a product capability
    (operator case: Soc0 ``api-context-items`` family, supabase
    ``get-utc-time``/``parse-query``); it folds. Repos with no page
    surface at all (pure-API products) keep API anchors mintable;
  * SINGLE-CONTAINER RULE — an unmerged non-authored anchor whose whole
    evidence is ONE file never mints (stray leaf-file class);
  * hub-vendor children mint iff ≥ 1 flow lands in the child (owned or
    entry-file) OR the child owns ≥ 1 source-code file (the amendment's
    stub rule: a single STATIC file — the supabase FDW-wrapper class —
    never mints); hub cores mint iff ≥ 1 sibling vendor minted.

Fold ladder for devs whose winner cannot mint (and the NONE class):
  1. UNION-PLURALITY (rider R2, single-app class) — accept the top
     capability-grain plurality anchor when its share ≥ 0.34 (the
     E-report random-tail bound, the same constant validator I15 gates
     on) AND the anchor is multi-source-confirmed;
  2. PARENT FOLD — version-dir / collection-descend children fold into
     the nearest minting ancestor anchor (``v1`` → its API surface);
  3. IMPORT FOLD — the dev's owned files' OUTGOING imports (workspace-
     aware TS resolver + the python module resolver, the same machinery
     Stage 8.8 uses) majority-target ONE minting capability's dev-owned
     files (midday ``i`` page → invoice components → Invoices);
  4. PLATFORM-INFRASTRUCTURE LANE — the second operator amendment
     (2026-07-06, final): **"Shared Platform" as a product feature no
     longer exists.** Unresolved devs keep ``product_feature_id=None``,
     carry a machine-readable ``shared_reason`` (``no_anchor_lineage`` |
     ``sub_mint_bar_surface`` | ``shell_lineage_only``) and are emitted
     in the top-level ``platform_infrastructure[]`` lane (sibling of
     ``non_product_surfaces[]``); their genuinely-shared files surface
     as ``role="shared"`` members on every consuming feature (the Stage
     8.8 mechanism, extended over the lane residents' files).

Deterministic, $0 LLM, scale-invariant, zero repo-tuned rules.
Kill-switch: ``FAULTLINE_SPINE_ANCHORED_MINT=0`` restores the old
Stage 6.5/8 → 6.7d Call-2 path byte-identically (the A/B baseline).
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.spine_anchors import (
    SOURCE_RANK,
    SpineAnchor,
    build_spine_anchors,
    load_spine_vocab,
    owned_paths_of,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

__all__ = [
    "ANCHORED_MINT_ENV",
    "anchored_mint_enabled",
    "run_anchored_mint",
    "build_platform_infrastructure_lane",
    "enforce_hub_family_parity",
]

ANCHORED_MINT_ENV = "FAULTLINE_SPINE_ANCHORED_MINT"

#: θ — the majority threshold (calibration §F: U-cap is monotonically
#: decreasing in θ; 0.5 is the conservation-law dual of §4.5).
_THETA = 0.5
#: near-tie band (calibration: ties within 10 pp resolve by source rank).
_NEAR_TIE_PP = 0.10
#: union-plurality floor (rider R2) — the E-report random-tail bound,
#: the SAME constant validator I15 gates on (a plurality at or below the
#: random-collection tail is noise, above it is signal).
_UNION_FLOOR = 0.34

_SHARED_REASON_NONE = "no_anchor_lineage"
_SHARED_REASON_BAR = "sub_mint_bar_surface"
_SHARED_REASON_SHELL = "shell_lineage_only"


def anchored_mint_enabled() -> bool:
    """Default ON; ``FAULTLINE_SPINE_ANCHORED_MINT=0`` restores the old
    PF path (Stage 6.5/8 survivorship + 6.7d Call-2) for A/B."""
    return os.environ.get(ANCHORED_MINT_ENV, "1").strip().lower() not in {
        "0", "false",
    }


# ── Per-dev classification ───────────────────────────────────────────────


def _classify_dev(
    owned: list[str],
    anchors: list[SpineAnchor],
) -> tuple[SpineAnchor | None, float, str, list[tuple[SpineAnchor, float]]]:
    """One dev's lineage: ``(winner, share, verdict, plurality_top)``.

    verdict ∈ {"unique", "near_tie", "none"}. ``plurality_top`` is the
    ranked sub-θ candidate list (capability-grain fold evidence).
    """
    n = len(owned)
    if n == 0:
        return None, 0.0, "none", []
    scored: list[tuple[SpineAnchor, float, frozenset[str]]] = []
    for a in anchors:
        matched = a.matched_set(owned)
        if matched:
            scored.append((a, len(matched) / n, matched))
    if not scored:
        return None, 0.0, "none", []
    majority = [(a, s, m) for a, s, m in scored if s >= _THETA]
    if not majority:
        top = sorted(scored, key=lambda t: (-t[1], t[0].rank, t[0].canonical_id))
        return None, 0.0, "none", [(a, s) for a, s, _ in top[:5]]

    # SPECIFICITY REDUCTION — drop anchors whose matched set strictly
    # contains another majority anchor's matched set; identical matched
    # sets merge. Identical-set preference (fastapi-template + supabase
    # smokes, 2026-07-06): source rank, then the MOST SPECIFIC subtree
    # (longest matching dir prefix — the mega-segment carve:
    # ``project/[ref]/integrations`` beats ``project``), then the key
    # that names the matched FILE itself (a central-router file carries
    # several URL keys — login.py = /login + /password-recovery — and
    # the stem names the surface), then the stable id.
    def _ident_key(a: SpineAnchor, m: frozenset[str]) -> tuple:
        from faultline.pipeline_v2.spine_anchors import (
            normalize_anchor_key as _nk,
        )
        longest_prefix = 0
        for p in m:
            for pre in a.prefixes:
                if p.startswith(pre + "/") or p == pre:
                    longest_prefix = max(longest_prefix, len(pre))
        stem_match = 0
        if len(m) == 1:
            f = next(iter(m))
            stem = f.rsplit("/", 1)[-1]
            stem = stem[: stem.rfind(".")] if "." in stem else stem
            stem_match = 0 if _nk(stem) == a.key else 1
        return (a.rank, -longest_prefix, stem_match, a.canonical_id)

    reduced: list[tuple[SpineAnchor, float, frozenset[str]]] = []
    for a, s, m in majority:
        dominated = False
        for b, _sb, mb in majority:
            if a is b:
                continue
            # Strict matched-subset AND structural nesting (W2b.1 fix a):
            # b is "more specific" only when its own subtree nests inside
            # a's. Bare set-containment inverted on cross-app MERGED
            # anchors — openstatus `login`: ws:apps/dashboard matched 6 of
            # the 10 login files (a strict subset of route:login's 10) and
            # the shell dropped the route anchor; the shell's subtree is
            # NOT inside the route subtree, so it may not dominate it.
            if mb < m and b.subtree_inside(a):
                dominated = True
                break
            if mb == m and _ident_key(b, mb) < _ident_key(a, m):
                dominated = True
                break
        if not dominated:
            reduced.append((a, s, m))
    reduced.sort(key=lambda t: (-t[1], t[0].rank, t[0].canonical_id))
    winner, share, _ = reduced[0]
    if len(reduced) == 1 or share - reduced[1][1] > _NEAR_TIE_PP:
        return winner, share, "unique", []
    # Near-tie: nav confirmation first (ranking confirmer), then the
    # fixed source rank, then the stable id.
    tied = [(a, s) for a, s, _ in reduced if share - s <= _NEAR_TIE_PP]
    tied.sort(key=lambda t: (not t[0].nav_confirmed, t[0].rank,
                             t[0].canonical_id))
    return tied[0][0], tied[0][1], "near_tie", []


# ── Mint bar ─────────────────────────────────────────────────────────────


def _flow_evidence_index(
    developer_features: list["Feature"],
) -> tuple[dict[str, list[str]], set[str]]:
    """``entry-file → [flow names]`` over EVERY dev's flows (a hub
    child's flow evidence often lives on the parent dev — Soc0 edr) +
    the set of devs' names having ≥1 flow."""
    entries: dict[str, list[str]] = defaultdict(list)
    flowful_devs: set[str] = set()
    for f in developer_features:
        flows = getattr(f, "flows", None) or []
        if flows:
            flowful_devs.add(getattr(f, "name", "") or "")
        for fl in flows:
            ep = getattr(fl, "entry_point_file", None)
            if ep:
                entries[str(ep)].append(getattr(fl, "name", "") or "")
    return entries, flowful_devs


def _anchor_flow_evidence(
    anchor: SpineAnchor,
    winners: list["Feature"],
    flow_entries: dict[str, list[str]],
) -> bool:
    """≥1 flow lands in the anchor's subtree: a winner dev has flows, or
    ANY flow's entry file matches the subtree (parent-held flows)."""
    for dev in winners:
        if getattr(dev, "flows", None):
            return True
    for ep in flow_entries:
        if anchor.matches(ep):
            return True
    return False


def _is_code(path: str, code_exts: tuple[str, ...]) -> bool:
    return path.lower().endswith(code_exts)


def _mint_bar(
    anchor: SpineAnchor,
    winners: list["Feature"],
    flow_entries: dict[str, list[str]],
    repo_has_pages: bool,
    code_exts: tuple[str, ...],
) -> str | None:
    """``None`` when the anchor may mint, else the bar reason."""
    if not winners:
        return "no_winning_devs"
    if anchor.shell:
        return "shell"
    if anchor.barred:
        return anchor.barred  # single_letter | version_dir
    if anchor.sources == frozenset({"svc"}):
        return "service_dir_only"
    if anchor.source == "hub-vendor":
        if _anchor_flow_evidence(anchor, winners, flow_entries):
            return None
        if anchor.hub_parent_generic:
            # Generic-container family (backend/routers, backend/models):
            # a vendor-named file with NO flow is not an integration PF.
            return "hub_child_no_flow"
        child_files: set[str] = set(anchor.files)
        for w in winners:
            child_files.update(anchor.matched_set(owned_paths_of(w)))
        if any(_is_code(p, code_exts) for p in child_files):
            return None
        return "hub_stub_child"  # single static file class (FDW wrappers)
    if anchor.source == "hub-core":
        return None  # gated on sibling mints by the caller
    # PAGE-SURFACE RULE (only in repos that have a page surface at all).
    # Authored capability declarations (feature-dirs, workspace packages,
    # python domain packages) are product evidence of their own.
    if repo_has_pages:
        has_page_evidence = (
            bool(anchor.page_route_files)
            or bool(anchor.sources & {"fdir", "ws-pkg", "pypkg"})
            or anchor.nav_confirmed
        )
        if not has_page_evidence:
            return "api_only_surface"
    # PYPKG THINNESS (W2b.1 fix b): a package-domain anchor with no flow
    # evidence and fewer than two code files is structure, not a
    # capability (onyx `redis`/`httpx` wrapper-package class).
    if anchor.sources == frozenset({"pypkg"}):
        pk_evidence: set[str] = set(anchor.files)
        for w in winners:
            pk_evidence.update(anchor.matched_set(owned_paths_of(w)))
        code_files = [p for p in pk_evidence if _is_code(p, code_exts)]
        if (len(code_files) < 2
                and not _anchor_flow_evidence(anchor, winners, flow_entries)):
            return "pypkg_thin_surface"
    # SINGLE-CONTAINER RULE — an unmerged, non-authored anchor whose
    # entire evidence is one file AND no flow lands in it (a flowful
    # single-page capability — a real login/pricing page — is legal;
    # a flow-less stray leaf file is not).
    if len(anchor.sources) == 1 and anchor.source == "route":
        evidence: set[str] = set(anchor.files)
        for w in winners:
            evidence.update(anchor.matched_set(owned_paths_of(w)))
        if (len(evidence) <= 1 and not anchor.prefixes
                and not _anchor_flow_evidence(anchor, winners, flow_entries)):
            return "single_file_surface"
    return None


# ── Import fold (ladder rung 3) ──────────────────────────────────────────


def _import_fold_targets(
    dev_owned: list[str],
    repo_path: Path,
    tracked: frozenset[str],
    cache: Any,
    alias_map: Any,
) -> list[str]:
    """Resolved OUTGOING import targets of the dev's owned files (repo
    files only), excluding self-owned targets. Reuses the Stage 6.3/8.8
    source cache + workspace-aware resolvers."""
    from faultline.pipeline_v2.stage_6_3_import_tree import (
        _PY_EXTS,
        _TS_EXTS,
        _is_vendor_or_test,
        _suffix,
    )
    from faultline.pipeline_v2.stage_8_8_shared_members import _resolve_one

    own = set(dev_owned)
    out: list[str] = []
    for rel in sorted(own):
        if _suffix(rel) not in (_TS_EXTS | _PY_EXTS) or _is_vendor_or_test(rel):
            continue
        try:
            specs = cache.imports(rel).values()
        except Exception:  # noqa: BLE001 — unreadable file → no imports
            continue
        for spec in specs:
            tgt = _resolve_one(rel, spec, alias_map, tracked)
            if tgt is not None and tgt not in own:
                out.append(tgt)
    return out


# ── Public entrypoint ────────────────────────────────────────────────────


def run_anchored_mint(
    developer_features: list["Feature"],
    routes_index: list[dict[str, Any]] | None,
    ctx: Any,
    extractor_signals: dict[str, list[Any]] | None = None,
    nav_keys: frozenset[str] = frozenset(),
) -> tuple[list["Feature"], dict[str, Any]]:
    """Derive dev→PF from anchor lineage; REPLACE the product layer.

    Mutates dev features in place (``product_feature_id`` /
    ``anchor_id`` / ``shared_reason``); returns the anchored
    ``product_features`` list + telemetry. The caller swaps its PF
    array for the returned one.
    """
    from faultline.pipeline_v2.nav_taxonomy import aggregate_product_feature
    from faultline.pipeline_v2.spine_hygiene import is_facet

    vocab = load_spine_vocab()
    code_exts = tuple(vocab.get("code_extensions") or [])

    tele: dict[str, Any] = {
        "enabled": True, "applied": False,
        "anchors_by_source": {}, "anchors_total": 0,
        "devs_total": 0, "devs_in_scope": 0,
        "unique": 0, "unique_capability": 0, "unique_shell": 0,
        "near_tie": 0, "none": 0,
        "fold_union_plurality": 0, "fold_parent": 0, "fold_import": 0,
        "infra_lane": 0, "infra_reasons": {},
        "pf_minted": 0, "bar_decisions": [],
        "churn_devs": 0, "hub_families": [],
    }

    devs = [
        f for f in developer_features
        if getattr(f, "layer", "developer") == "developer"
        and getattr(f, "name", None)
    ]
    tele["devs_total"] = len(devs)
    in_scope = [f for f in devs if not is_facet(f)]
    tele["devs_in_scope"] = len(in_scope)
    if not in_scope:
        return [], tele

    anchors = build_spine_anchors(
        in_scope, routes_index, ctx, extractor_signals, nav_keys)
    tele["anchors_total"] = len(anchors)
    by_src: Counter[str] = Counter(a.source for a in anchors)
    tele["anchors_by_source"] = dict(sorted(by_src.items()))

    repo_has_pages = any(a.page_route_files for a in anchors)
    tele["repo_has_pages"] = repo_has_pages
    flow_entries, _flowful = _flow_evidence_index(in_scope)

    # Pass 1 — classify every in-scope dev.
    owned_by_dev: dict[str, list[str]] = {}
    winner_by_dev: dict[str, SpineAnchor | None] = {}
    verdicts: dict[str, str] = {}
    plurality_by_dev: dict[str, list[tuple[SpineAnchor, float]]] = {}
    prev_stamp = {f.name: getattr(f, "product_feature_id", None) for f in in_scope}
    for f in sorted(in_scope, key=lambda x: x.name):
        owned = owned_paths_of(f)
        owned_by_dev[f.name] = owned
        winner, share, verdict, plur = _classify_dev(owned, anchors)
        winner_by_dev[f.name] = winner
        verdicts[f.name] = verdict
        plurality_by_dev[f.name] = plur
        if verdict == "unique":
            tele["unique"] += 1
            if winner is not None and winner.shell:
                tele["unique_shell"] += 1
            else:
                tele["unique_capability"] += 1
        elif verdict == "near_tie":
            tele["near_tie"] += 1
        else:
            tele["none"] += 1

    # Pass 2 — mint bar over anchors that won ≥ 1 dev.
    winners_by_anchor: dict[str, list["Feature"]] = defaultdict(list)
    for f in sorted(in_scope, key=lambda x: x.name):
        w = winner_by_dev[f.name]
        if w is not None:
            winners_by_anchor[w.canonical_id].append(f)
    anchor_by_id = {a.canonical_id: a for a in anchors}
    bar_by_anchor: dict[str, str | None] = {}
    for cid in sorted(winners_by_anchor):
        a = anchor_by_id[cid]
        bar_by_anchor[cid] = _mint_bar(
            a, winners_by_anchor[cid], flow_entries, repo_has_pages, code_exts)
    # Hub cores mint only when ≥ 1 sibling vendor minted (amendment §2:
    # a core exists relative to its children).
    minted_vendor_hubs = {
        anchor_by_id[cid].hub_dir
        for cid, bar in bar_by_anchor.items()
        if bar is None and anchor_by_id[cid].source == "hub-vendor"
    }
    for cid in sorted(bar_by_anchor):
        a = anchor_by_id[cid]
        if a.source == "hub-core" and bar_by_anchor[cid] is None:
            if a.hub_dir not in minted_vendor_hubs:
                bar_by_anchor[cid] = "hub_core_without_children"
    for cid in sorted(bar_by_anchor):
        if bar_by_anchor[cid] and len(tele["bar_decisions"]) < 50:
            tele["bar_decisions"].append(
                {"anchor": cid, "bar": bar_by_anchor[cid],
                 "devs": [f.name for f in winners_by_anchor[cid][:5]]})

    mintable = {cid for cid, bar in bar_by_anchor.items() if bar is None}

    # Pass 3 — assignment + fold ladder.
    #   assignment: dev name → (anchor canonical_id, provenance)
    assignment: dict[str, tuple[str, str]] = {}
    infra: dict[str, str] = {}  # dev name → shared_reason

    def _parent_fold(a: SpineAnchor) -> str | None:
        """Nearest MINTING ancestor by prefix containment (version-dir /
        collection-descend children fold into their API surface). File
        anchors (api leaves) locate through their files' dirs — the
        W2b.1 api-leaf-fold rung (fix d2) rides this."""
        units = list(a.prefixes or ())
        units.extend(f.rsplit("/", 1)[0] for f in sorted(a.files) if "/" in f)
        best: tuple[int, str] | None = None
        for cid in mintable:
            m = anchor_by_id[cid]
            for mp in m.prefixes:
                for ap in units:
                    if ap.startswith(mp + "/") or ap == mp:
                        cand = (len(mp), cid)
                        if best is None or cand > best:
                            best = cand
        return best[1] if best else None

    def _entry_fold(f: "Feature") -> str | None:
        """The minting anchor holding the MAJORITY of the dev's flow
        entry files — the strongest behavioral fold signal (validator
        I16's own ruler): a dev whose journeys enter through one
        capability's surface belongs to it (supabase FDW wrappers class:
        the dev's flow enters via the integrations page)."""
        entries = [str(ep) for fl in (getattr(f, "flows", None) or [])
                   if (ep := getattr(fl, "entry_point_file", None))]
        if not entries:
            return None
        votes: Counter[str] = Counter()
        for ep in entries:
            best_cid: str | None = None
            best_spec: tuple[int, int, str] | None = None
            for cid in sorted(mintable):
                a = anchor_by_id[cid]
                if not a.matches(ep):
                    continue
                # Most specific match wins: exact file > longest prefix.
                spec = (
                    1 if ep in a.files else 0,
                    max((len(p) for p in a.prefixes
                         if ep.startswith(p + "/") or ep == p), default=0),
                    cid,
                )
                if best_spec is None or spec > best_spec:
                    best_cid, best_spec = cid, spec
            if best_cid is not None:
                votes[best_cid] += 1
        if not votes:
            return None
        (best, n), = votes.most_common(1)
        tied = sorted(c for c, v in votes.items() if v == n)
        best = tied[0]
        return best if votes[best] * 2 > len(entries) else None

    def _anchor_of_target(t: str) -> str | None:
        """The most specific MINTING anchor covering one file (exact
        file > longest prefix). Shared by the import-fold and the
        span-vote law rungs."""
        best_cid: str | None = None
        best_spec: tuple[int, int] | None = None
        for cid in sorted(mintable):
            a = anchor_by_id[cid]
            if not a.matches(t):
                continue
            spec = (
                1 if t in a.files else 0,
                max((len(p) for p in a.prefixes
                     if t.startswith(p + "/") or t == p), default=0),
            )
            if best_spec is None or spec > best_spec:
                best_cid, best_spec = cid, spec
        return best_cid

    def _file_owner_map() -> dict[str, str]:
        """file → first ASSIGNED dev primary-owning it (ownership
        fallback channel of the vote rungs)."""
        out: dict[str, str] = {}
        for name in sorted(assignment):
            for p in owned_by_dev.get(name, ()):
                out.setdefault(p, name)
        return out

    def _entry_route_mint(f: "Feature") -> str | None:
        """W2b.1 law rung L1 — entry-file ROUTE lineage: a flowful dev
        whose flow entry files majority-sit inside ONE route anchor gets
        that anchor MINTED ON DEMAND (openstatus login class: the dev's
        journeys enter through a real page surface, so the surface is a
        product feature even when lineage dilution starved the anchor of
        winners). The on-demand anchor passes the FULL mint bar — an
        api-only or param-barred surface never mints this way (those
        fold via the api-leaf/span rungs instead)."""
        entries = [str(ep) for fl in (getattr(f, "flows", None) or [])
                   if (ep := getattr(fl, "entry_point_file", None))]
        if not entries:
            return None
        votes: Counter[str] = Counter()
        for ep in entries:
            best_cid: str | None = None
            best_spec: tuple[int, int, str] | None = None
            for a in anchors:
                if a.source != "route" or not a.matches(ep):
                    continue
                spec = (
                    1 if ep in a.files else 0,
                    max((len(p) for p in a.prefixes
                         if ep.startswith(p + "/") or ep == p), default=0),
                    a.canonical_id,
                )
                if best_spec is None or spec > best_spec:
                    best_cid, best_spec = a.canonical_id, spec
            if best_cid is not None:
                votes[best_cid] += 1
        if not votes:
            return None
        (_best, n), = votes.most_common(1)
        tied = sorted(c for c, v in votes.items() if v == n)
        best = tied[0]
        if votes[best] * 2 <= len(entries):
            return None
        if best in mintable:
            return best
        a = anchor_by_id[best]
        if a.shell:
            return None
        bar = _mint_bar(a, [f], flow_entries, repo_has_pages, code_exts)
        if bar is not None:
            return None
        mintable.add(best)
        bar_by_anchor[best] = None
        return best

    def _span_vote(f: "Feature") -> str | None:
        """W2b.1 law rung L2 — the dev's flows' file SPANS vote for the
        minting anchor (or assigned owner) holding them; PLURALITY wins
        (terminal-rung semantics — a flowful dev must land in a real
        capability, the binding is recorded in the provenance note).

        W3.1 D1 COHERENCE GUARD: the winning target must account for at
        least ``_UNION_FLOOR`` of the dev's DISTINCT span files — the
        same random-tail bound every other plurality rung uses, with the
        honest denominator (unresolvable span mass counts AGAINST the
        bind). Without it a giant dev whose span resolves only through a
        tiny sliver was force-bound to that sliver's PF (comp
        `mcp-server` 34.5K LOC → the 3-file `security` route PF — the
        fb3 D1 sink class)."""
        file_owner = _file_owner_map()
        votes: Counter[str] = Counter()
        span_files: set[str] = set()
        matched_files: dict[str, set[str]] = defaultdict(set)
        for fl in (getattr(f, "flows", None) or []):
            span = [str(p) for p in (getattr(fl, "paths", None) or [])]
            ep = getattr(fl, "entry_point_file", None)
            if not span and ep:
                span = [str(ep)]
            for p in span:
                span_files.add(p)
                cid = _anchor_of_target(p)
                if cid is None:
                    owner = file_owner.get(p)
                    if owner is not None:
                        cid = assignment[owner][0]
                if cid is not None:
                    votes[cid] += 1
                    matched_files[cid].add(p)
        if not votes:
            return None
        (_best, n), = votes.most_common(1)
        tied = sorted(c for c, v in votes.items() if v == n)
        best = tied[0]
        if span_files and (
                len(matched_files[best]) / len(span_files) < _UNION_FLOOR):
            return None
        return best

    def _ancestor_walk(f: "Feature") -> str | None:
        """W2b.1 law rung L3 — nearest-ancestor plurality: walk UP from
        the dev's owned files' common dir; the first level where ANY
        assigned dev owns files decides by plurality of their anchors.
        Total whenever ≥1 dev is assigned (the repo-root level sees
        every assigned file)."""
        owned = owned_by_dev.get(f.name) or []
        dirs = [p.rsplit("/", 1)[0] if "/" in p else "" for p in owned]
        if dirs:
            first = dirs[0].split("/") if dirs[0] else []
            k = len(first)
            for other_str in dirs[1:]:
                other = other_str.split("/") if other_str else []
                j = 0
                while j < min(k, len(other)) and other[j] == first[j]:
                    j += 1
                k = j
            common = "/".join(first[:k])
        else:
            common = ""
        assigned_file_cid = {
            p: assignment[name][0]
            for name in sorted(assignment)
            for p in owned_by_dev.get(name, ())
        }
        level = common
        while True:
            votes: Counter[str] = Counter()
            for p, cid in assigned_file_cid.items():
                if not level or p.startswith(level + "/"):
                    votes[cid] += 1
            if votes:
                (_best, n), = votes.most_common(1)
                tied = sorted(c for c, v in votes.items() if v == n)
                return tied[0]
            if not level:
                return None
            level = level.rsplit("/", 1)[0] if "/" in level else ""

    fold_pending: list[tuple["Feature", SpineAnchor | None, str]] = []
    for f in sorted(in_scope, key=lambda x: x.name):
        w = winner_by_dev[f.name]
        if w is not None and w.canonical_id in mintable:
            assignment[f.name] = (w.canonical_id, "lineage")
            continue
        if w is None:
            # NONE → rider R2 union-plurality first.
            accepted = False
            for cand, share in plurality_by_dev[f.name]:
                if (cand.canonical_id in mintable and share >= _UNION_FLOOR
                        and len(cand.sources) >= 2):
                    assignment[f.name] = (cand.canonical_id, "union_plurality")
                    tele["fold_union_plurality"] += 1
                    accepted = True
                    break
            if not accepted:
                ef = _entry_fold(f)
                if ef is not None:
                    assignment[f.name] = (ef, "fold:entry->none")
                    tele["fold_entry"] = tele.get("fold_entry", 0) + 1
                else:
                    fold_pending.append((f, None, _SHARED_REASON_NONE))
            continue
        # Winner exists but cannot mint → parent fold, entry fold,
        # api-leaf fold, then import fold.
        parent = _parent_fold(w)
        if parent is not None and w.barred in {"version_dir", "single_letter",
                                               "param_leaf"}:
            assignment[f.name] = (parent, f"fold:parent->{w.canonical_id}")
            tele["fold_parent"] += 1
            continue
        ef = _entry_fold(f)
        if ef is not None:
            assignment[f.name] = (ef, f"fold:entry->{w.canonical_id}")
            tele["fold_entry"] = tele.get("fold_entry", 0) + 1
            continue
        # API-LEAF FOLD (W2b.1 fix d2): a dev whose winner is an
        # api-only-barred surface folds into the nearest MINTING
        # ancestor capability (supabase pages/api singles were meant to
        # die by this class; the parent rung above only served the
        # barred-KEY classes).
        if (bar_by_anchor.get(w.canonical_id) == "api_only_surface"
                and parent is not None):
            assignment[f.name] = (parent, f"fold:api-parent->{w.canonical_id}")
            tele["fold_api_parent"] = tele.get("fold_api_parent", 0) + 1
            continue
        reason = (_SHARED_REASON_SHELL if w.shell else _SHARED_REASON_BAR)
        fold_pending.append((f, w, reason))

    # LAW RUNG L1 (W2b.1 fix a) — entry-route lineage BEFORE the import
    # fold (task order: entry-file route lineage → import-fold →
    # span-vote). Mint-on-demand may EXTEND ``mintable`` — iteration is
    # name-sorted, so the extension is deterministic.
    still_pending: list[tuple["Feature", SpineAnchor | None, str]] = []
    for f, w, reason in fold_pending:
        if getattr(f, "flows", None):
            target = _entry_route_mint(f)
            if os.environ.get("FAULTLINE_MINT_DEBUG") == "1":
                tele.setdefault("fold_debug", []).append({
                    "dev": f.name, "rung": "L1-entry-route",
                    "target": target,
                    "entries": [str(ep) for fl in (getattr(f, "flows", None) or [])
                                if (ep := getattr(fl, "entry_point_file", None))][:8],
                })
            if target is not None:
                src = w.canonical_id if w is not None else "none"
                assignment[f.name] = (target, f"mint:entry-route->{src}")
                tele["mint_entry_route"] = tele.get("mint_entry_route", 0) + 1
                continue
        still_pending.append((f, w, reason))
    fold_pending = still_pending

    # Import fold — one deterministic round over the pass-3 residue.
    if fold_pending:
        from faultline.analyzer.tsconfig_paths import build_path_alias_map
        from faultline.pipeline_v2.stage_6_3_import_tree import _SourceCache

        repo_path = Path(getattr(ctx, "repo_path", "."))
        tracked = frozenset(str(p) for p in (getattr(ctx, "tracked_files", None) or []))
        src_cache = _SourceCache(repo_path)
        try:
            alias_map = build_path_alias_map(repo_path)
        except Exception:  # noqa: BLE001 — resolver is best-effort
            alias_map = None
        # Vote by MINTING-ANCHOR SUBTREE membership of the import targets
        # (spine-true: anchors are the membership units; dev-ownership is
        # derived). The midday acceptance run showed the dev-ownership
        # vote misses workspace-package imports whose files nobody
        # primary-owns (`@midday/invoice/templates` → packages/invoice
        # inside the MERGED `invoices` anchor). Per target: the most
        # specific matching minting anchor (exact file > longest prefix).
        file_owner = _file_owner_map()
        still_pending = []
        for f, w, reason in fold_pending:
            owned = owned_by_dev[f.name]
            targets = _import_fold_targets(
                owned, repo_path, tracked, src_cache, alias_map)
            votes: Counter[str] = Counter()
            for t in targets:
                target_cid = _anchor_of_target(t)
                if target_cid is None:
                    owner = file_owner.get(t)
                    if owner is not None:
                        target_cid = assignment[owner][0]
                if target_cid is not None:
                    votes[target_cid] += 1
            resolved = False
            if os.environ.get("FAULTLINE_MINT_DEBUG") == "1":
                tele.setdefault("fold_debug", []).append({
                    "dev": f.name, "rung": "import",
                    "targets_total": len(targets),
                    "votes": dict(votes.most_common(8)),
                })
            if votes:
                total = sum(votes.values())
                (best_cid, best_n), = votes.most_common(1)
                # strict majority of anchor-covered targets, tie → alpha
                tied = sorted(c for c, n in votes.items() if n == best_n)
                best_cid = tied[0]
                # W3.1 D1 SELF-EVIDENCE GUARD: imports point at what the
                # dev DEPENDS ON, not what it IS — on every app-shaped
                # repo the import majority lands on the shared component
                # library (documenso: 11 route devs → ws:packages/ui at
                # 60-90% majorities; tracecat: 21 frontend clusters →
                # the `status` PF; supabase: `studio` 99.5K → the 2.7K
                # `claim-project` page). A fold may follow imports only
                # when the target also holds >= _UNION_FLOOR of the
                # dev's OWN files (near-lineage confirmation), or the
                # dev is a <= 3-file stub whose only content IS the
                # import surface (the midday `i` page class W2b built
                # this rung for).
                if votes[best_cid] * 2 > total:
                    target_anchor = anchor_by_id[best_cid]
                    own_inside = sum(
                        1 for p in owned if target_anchor.matches(p))
                    self_evident = (
                        len(owned) <= 3
                        or (len(owned) > 0
                            and own_inside / len(owned) >= _UNION_FLOOR)
                    )
                    if self_evident:
                        src = w.canonical_id if w is not None else "none"
                        assignment[f.name] = (best_cid, f"fold:import->{src}")
                        tele["fold_import"] += 1
                        resolved = True
                    else:
                        tele["fold_import_guard_blocked"] = (
                            tele.get("fold_import_guard_blocked", 0) + 1)
            if not resolved:
                still_pending.append((f, w, reason))
        fold_pending = still_pending

    # LAW (W2b.1 fix a — operator): a dev with ≥1 flow must NEVER land
    # in the platform_infrastructure lane. Terminal rungs: span-vote
    # (flow-path plurality, rung L2) then ancestor-walk plurality
    # (rung L3, total whenever ≥1 dev is assigned). The lane is for
    # genuinely FLOWLESS plumbing only; the binding is low-confidence by
    # construction and carries a provenance note (``fold:span->…`` /
    # ``fold:walk->…``). Degenerate scans (zero mintable anchors) keep
    # the honest lane — there is no capability to bind to.
    #
    # W3.1 D1 CARVE-OUT — the law's own ruler (validator I9) EXEMPTS
    # workspace-anchor devs from the flowful-in-lane class, and
    # conservation.py's dev-rehome states why: "anchors never move —
    # their flow sample spans the whole workspace, not one capability".
    # Force-binding them was the single biggest sink source (supabase
    # `studio` 99.5K LOC / 478 flows → the `claim-project` page PF).
    # They lane honestly (flows stay visible on the lane row; their
    # files ride role="shared" members on every importing feature).
    from faultline.pipeline_v2.stage_8_7_anchor_desink import (
        _is_workspace_anchor,
    )

    for f, w, reason in fold_pending:
        flowful = bool(getattr(f, "flows", None))
        if flowful and _is_workspace_anchor(f):
            tele["law_ws_anchor_laned"] = (
                tele.get("law_ws_anchor_laned", 0) + 1)
            infra[f.name] = (_SHARED_REASON_SHELL if w is not None and w.shell
                             else reason)
            continue
        if flowful and mintable:
            src = w.canonical_id if w is not None else "none"
            target = _span_vote(f)
            if os.environ.get("FAULTLINE_MINT_DEBUG") == "1":
                tele.setdefault("fold_debug", []).append({
                    "dev": f.name, "rung": "span", "target": target,
                })
            if target is not None:
                assignment[f.name] = (target, f"fold:span->{src}")
                tele["fold_span_vote"] = tele.get("fold_span_vote", 0) + 1
                continue
            target = _ancestor_walk(f)
            if target is not None:
                assignment[f.name] = (target, f"fold:walk->{src}")
                tele["fold_ancestor_walk"] = (
                    tele.get("fold_ancestor_walk", 0) + 1)
                continue
        if flowful:
            # Reachable ONLY on degenerate scans (zero mintable anchors,
            # or an assignment-less walk) — tracked so the smoke gates
            # can assert the law held (0 on every real product scan).
            tele["law_flowful_in_lane"] = (
                tele.get("law_flowful_in_lane", 0) + 1)
        infra[f.name] = reason

    # Pass 4 — build the anchored product features.
    devs_by_anchor: dict[str, list["Feature"]] = defaultdict(list)
    dev_by_name = {f.name: f for f in in_scope}
    for name in sorted(assignment):
        cid, _prov = assignment[name]
        devs_by_anchor[cid].append(dev_by_name[name])

    product_features: list["Feature"] = []
    slug_by_anchor: dict[str, str] = {}
    # RESERVED legacy keys (W2b.1): every _SHARED_PF_KEYS consumer
    # (validator I9, surface-taxonomy shared-bucket exemptions, dashboard
    # lanes) treats a PF keyed ``platform``/``shared-platform`` as the
    # ABOLISHED shared bucket. A real anchor honestly named "platform"
    # (supabase docs) must therefore mint under a QUALIFIED slug — the
    # display keeps the author's word, the key stays out of the legacy
    # namespace.
    used_slugs: set[str] = {"platform", "shared-platform"}
    for cid in sorted(devs_by_anchor, key=lambda c: (anchor_by_id[c].display.lower(), c)):
        a = anchor_by_id[cid]
        contrib = devs_by_anchor[cid]
        slug = _slug(a.display)
        display = a.display
        if slug in used_slugs:
            # Same display from two anchors (cross-FAMILY vendor clash —
            # Soc0 claroty under both edr and iot_ot): qualify the
            # DISPLAY (dev_map / the 6.7d rebuild key capabilities by
            # display, so two live PFs sharing one display would
            # silently merge downstream) and derive the slug FROM the
            # qualified display via the SAME canonical_slug the 6.7d
            # rebuild uses (review F3: a hand-rolled `claroty-iot-ot`
            # diverged from canonical_slug("Claroty (Iot Ot)") =
            # `claroty-(iot-ot)`, voiding hub parity for exactly this
            # class and forking keyless-vs-keyed slugs).
            fam = getattr(a, "family_key", "") or ""
            qual = (fam.replace("-", " ").title() if fam
                    else re.sub(r"[^A-Za-z0-9]+", " ", cid).strip().title())
            display = f"{a.display} ({qual})"
            slug = _slug(display)
            if slug in used_slugs:  # same qualified display twice — cid tail
                display = f"{a.display} ({re.sub(r'[^A-Za-z0-9]+', ' ', cid).strip().title()})"
                slug = _slug(display)
        used_slugs.add(slug)
        slug_by_anchor[cid] = slug
        desc = (
            f"Capability anchored at {cid} "
            f"(sources: {', '.join(sorted(a.sources))}; "
            f"{len(contrib)} developer feature(s))."
        )
        pf = aggregate_product_feature(
            name=slug, display_name=display, description=desc,
            contrib=contrib,
        )
        pf.layer = "product"
        pf.anchor_id = cid
        # Carry the richer member_files ledger (dedup by path).
        seen_mf: set[str] = set()
        merged_mf: list[Any] = []
        for c in contrib:
            for mf in (getattr(c, "member_files", None) or []):
                mfp = (mf.get("path") if isinstance(mf, dict)
                       else getattr(mf, "path", None))
                if mfp and mfp not in seen_mf:
                    seen_mf.add(mfp)
                    merged_mf.append(mf)
        if merged_mf:
            pf.member_files = merged_mf
        product_features.append(pf)
    tele["pf_minted"] = len(product_features)

    # Pass 5 — stamp devs.
    for f in sorted(in_scope, key=lambda x: x.name):
        if f.name in assignment:
            cid, prov = assignment[f.name]
            f.product_feature_id = slug_by_anchor[cid]
            f.anchor_id = cid if prov == "lineage" else f"{prov}"
            if getattr(f, "shared_reason", None):
                f.shared_reason = None
        else:
            reason = infra.get(f.name, _SHARED_REASON_NONE)
            f.product_feature_id = None
            f.anchor_id = None
            f.shared_reason = reason
            tele["infra_lane"] += 1
            tele["infra_reasons"][reason] = tele["infra_reasons"].get(reason, 0) + 1
        if prev_stamp.get(f.name) != f.product_feature_id:
            tele["churn_devs"] += 1
    tele["churn_pct"] = round(tele["churn_devs"] / max(len(in_scope), 1), 4)

    # Hub-family stamps (sibling parity is re-asserted post-6.7d).
    fam_stamp: dict[str, str] = {}
    for cid in sorted(devs_by_anchor):
        a = anchor_by_id[cid]
        if a.source in {"hub-vendor", "hub-core"}:
            for f in devs_by_anchor[cid]:
                fam_stamp[f.name] = slug_by_anchor[cid]
    tele["hub_family_stamps"] = dict(sorted(fam_stamp.items()))
    fams: dict[str, dict[str, Any]] = {}
    for cid in sorted(devs_by_anchor):
        a = anchor_by_id[cid]
        if a.source == "hub-vendor" and a.hub_dir:
            fams.setdefault(a.hub_dir, {"vendors": [], "core": None})
            fams[a.hub_dir]["vendors"].append(a.vendor or a.key)
        elif a.source == "hub-core" and a.hub_dir:
            fams.setdefault(a.hub_dir, {"vendors": [], "core": None})
            fams[a.hub_dir]["core"] = slug_by_anchor[cid]
    tele["hub_families"] = [
        {"hub_dir": d, **v} for d, v in sorted(fams.items())
    ][:20]

    # Shared-consumer pass (amendment §2): the infra residents' files
    # surface as role="shared" members on every feature whose own code
    # imports them — the Stage 8.8 mechanism over the lane's file set.
    infra_devs = [dev_by_name[n] for n in sorted(infra)]
    if infra_devs:
        tele["shared_consumers"] = _attach_shared_consumers(
            ctx, in_scope, infra_devs, owned_by_dev)

    tele["applied"] = True
    return product_features, tele


def _slug(name: str) -> str:
    from faultline.pipeline_v2.emission_integrity import canonical_slug
    return canonical_slug(name)


# ── Shared-consumer attachment (8.8 mechanism over the infra lane) ───────


def _attach_shared_consumers(
    ctx: Any,
    in_scope: list["Feature"],
    infra_devs: list["Feature"],
    owned_by_dev: dict[str, list[str]],
) -> dict[str, int]:
    """Attach infra residents' files as ``role="shared"`` member_files on
    the features whose OWN code directly imports them. Reuses the Stage
    8.8 resolvers + its fan-out conduit guard; additive (member_files
    only), flow-immune."""
    from faultline.analyzer.tsconfig_paths import build_path_alias_map
    from faultline.models.types import MemberFile
    from faultline.pipeline_v2.stage_6_3_import_tree import (
        _PY_EXTS,
        _SourceCache,
        _TS_EXTS,
        _is_vendor_or_test,
        _suffix,
    )
    from faultline.pipeline_v2.stage_8_8_shared_members import (
        _fanout_cap,
        _resolve_one,
    )

    stats = {"files": 0, "edges": 0, "conduits": 0}
    residual: set[str] = set()
    for d in infra_devs:
        residual.update(owned_by_dev.get(d.name, ()))
    if not residual:
        return stats
    stats["files"] = len(residual)

    repo_path = Path(getattr(ctx, "repo_path", "."))
    tracked = frozenset(str(p) for p in (getattr(ctx, "tracked_files", None) or []))
    cache = _SourceCache(repo_path)
    try:
        alias_map = build_path_alias_map(repo_path)
    except Exception:  # noqa: BLE001
        alias_map = None

    infra_names = {d.name for d in infra_devs}
    consumers = [f for f in in_scope if f.name not in infra_names]
    imports_by_feature: dict[str, set[str]] = defaultdict(set)
    importers_by_file: dict[str, set[str]] = defaultdict(set)
    for f in consumers:
        for rel in owned_by_dev.get(f.name, ()):
            if _suffix(rel) not in (_TS_EXTS | _PY_EXTS) or _is_vendor_or_test(rel):
                continue
            try:
                specs = cache.imports(rel).values()
            except Exception:  # noqa: BLE001
                continue
            for spec in specs:
                tgt = _resolve_one(rel, spec, alias_map, tracked)
                if tgt is not None and tgt in residual:
                    imports_by_feature[f.name].add(tgt)
                    importers_by_file[tgt].add(f.name)

    cap = _fanout_cap([len(v) for v in importers_by_file.values()],
                      len(in_scope))
    conduits = {fp for fp, imp in importers_by_file.items() if len(imp) >= cap}
    stats["conduits"] = len(conduits)
    feat_by_name = {f.name: f for f in consumers}
    for fname in sorted(imports_by_feature):
        feat = feat_by_name[fname]
        existing = {
            (m.get("path") if isinstance(m, dict) else getattr(m, "path", None))
            for m in (feat.member_files or [])
        }
        for fp in sorted(imports_by_feature[fname]):
            if fp in conduits or fp in existing:
                continue
            feat.member_files.append(MemberFile(
                path=fp, role="shared", primary=False,
                confidence=0.5,
                evidence="spine-w2b: platform-infrastructure file "
                         "directly imported by this feature",
            ))
            stats["edges"] += 1
    return stats


# ── platform_infrastructure[] lane (amendment §3) ────────────────────────


def build_platform_infrastructure_lane(
    developer_features: list["Feature"],
) -> list[dict[str, Any]]:
    """Emission-time lane rows for the anchored residual: one entry PER
    resident dev (name, files, loc, reason). Zero-loss: residents stay
    in ``features[]`` (Layer-1 truth) with ``product_feature_id=None``;
    the lane is the explainability surface (I22 reads it post-W2b)."""
    rows: list[dict[str, Any]] = []
    lane_reasons = {_SHARED_REASON_NONE, _SHARED_REASON_BAR,
                    _SHARED_REASON_SHELL}
    for f in developer_features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        if getattr(f, "product_feature_id", None) is not None:
            continue
        reason = getattr(f, "shared_reason", None)
        # ONLY the three amendment reasons the MINT stamps (review F4):
        # a pfid=None dev some other stage tagged with a different
        # reason (non_product_surface / genuinely_shared_infra /
        # facet_view) is that stage's concern, never a lane resident.
        if reason not in lane_reasons:
            continue
        rows.append({
            "name": f.name,
            "display_name": getattr(f, "display_name", None) or f.name,
            "shared_reason": reason,
            "uuid": getattr(f, "uuid", "") or "",
            "paths": list(getattr(f, "paths", None) or []),
            "loc": getattr(f, "loc", None),
            "loc_shared": getattr(f, "loc_shared", None),
            "flows": len(getattr(f, "flows", None) or []),
        })
    rows.sort(key=lambda r: r["name"])
    return rows


# ── Hub sibling parity (post-6.7d re-assert; replaces W1 inherit rule) ───


def enforce_hub_family_parity(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    family_stamps: dict[str, str],
) -> dict[str, Any]:
    """Amendment §4: children of one hub are sibling PFs under a common
    parent, NEVER shared/scattered. The mint's family stamps are
    construction law — any later ladder that moved a family dev is
    re-stamped, and affected PF path unions are re-derived."""
    tele = {"checked": len(family_stamps), "restamped": 0}
    if not family_stamps:
        return tele
    pf_by_key: dict[str, "Feature"] = {}
    for pf in product_features:
        key = getattr(pf, "name", None) or ""
        if key:
            pf_by_key.setdefault(key, pf)
    affected: set[str] = set()
    for f in developer_features:
        want = family_stamps.get(getattr(f, "name", "") or "")
        if want is None or want not in pf_by_key:
            continue
        have = getattr(f, "product_feature_id", None)
        if have != want:
            if have and have in pf_by_key:
                affected.add(have)
            f.product_feature_id = want
            f.shared_reason = None
            affected.add(want)
            tele["restamped"] += 1
    if affected:
        members_by_pf: dict[str, list["Feature"]] = defaultdict(list)
        for f in developer_features:
            pid = getattr(f, "product_feature_id", None)
            if (pid and pid in affected
                    and getattr(f, "layer", "developer") == "developer"):
                members_by_pf[str(pid)].append(f)
        for key in sorted(affected):
            target_pf = pf_by_key.get(key)
            if target_pf is None:
                continue
            merged: list[str] = []
            seen: set[str] = set()
            for m in members_by_pf.get(key, []):
                for p in (getattr(m, "paths", None) or []):
                    if p not in seen:
                        seen.add(p)
                        merged.append(p)
            target_pf.paths = merged
    return tele
