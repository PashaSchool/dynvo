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
    hub_child_is_plumbing,
    hub_plumbing_child_enabled,
    load_spine_vocab,
    normalize_anchor_key,
    owned_paths_of,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

__all__ = [
    "ANCHORED_MINT_ENV",
    "MINT_DOMAIN_FOLD_ENV",
    "FOLD_CROSSAPP_GUARD_ENV",
    "ANNEXATION_GUARD_ENV",
    "mint_domain_fold_enabled",
    "fold_crossapp_guard_enabled",
    "annexation_guard_enabled",
    "anchored_mint_enabled",
    "run_anchored_mint",
    "build_platform_infrastructure_lane",
    "enforce_hub_family_parity",
    "fold_unreferenced_vendor_husks",
    "husk_post_uf_fold_enabled",
]

ANCHORED_MINT_ENV = "FAULTLINE_SPINE_ANCHORED_MINT"

#: B8c (2026-07-09) — mint-time domain-fold rail. A flowful router dev that
#: the fold ladder folds into a DISTINCT-domain plurality host PF re-homes to
#: its OWN ``<domain>-page`` surface PF when that surface already exists (the
#: webhook exhibit: ``api-webhooks`` + ``webhook-detail-page`` reunite on
#: ``webhooks-page`` so the "Create and manage webhooks" journey re-forms
#: own-PF, I16-clean). Binds ONLY to surfaces that already receive ≥1 dev, so
#: PF count cannot rise (over-decomposition = 0 by construction); same-domain
#: and surface-less routers stay folded exactly as before. Kill-switch:
#: ``FAULTLINE_MINT_DOMAIN_FOLD_V2=0`` restores the byte-identical pre-B8c fold.
MINT_DOMAIN_FOLD_ENV = "FAULTLINE_MINT_DOMAIN_FOLD_V2"

#: B22a (2026-07-10) — cross-app fold guard. The terminal ancestor-walk rung
#: (``fold:walk``) votes by plurality of ALL assigned files at the first
#: populated ancestor level; a dev whose own workspace unit holds no minted
#: anchor escalates to repo root, where the repo's BIGGEST anchor annexes it
#: across the workspace boundary (documenso forensic: the ``ws:packages/trpc``
#: PF annexed ~90 product files / 11,783 journey span-lines outside
#: ``packages/trpc`` — embed/o/sign-token/d-token from ``apps/remix``, plus
#: ``apps/docs`` and ``apps/openpage-api`` devs — poisoning every downstream
#: owner-map ruler). The guard voids walk votes for anchors whose evidence
#: sits wholly in a FOREIGN workspace unit (a unit holding none of the dev's
#: own files). Unit roots come from the repo's OWN manifests (Stage-0
#: ``detect_workspace``: pnpm-workspace.yaml globs, package.json
#: "workspaces", turbo/nx/lerna/cargo/go; fallback: top-level dirs with their
#: own unit manifest) — mechanisms, not vocabularies; no thresholds.
#: Kill-switch: ``FAULTLINE_FOLD_CROSSAPP_GUARD=0`` restores the unguarded
#: walk byte-identically.
FOLD_CROSSAPP_GUARD_ENV = "FAULTLINE_FOLD_CROSSAPP_GUARD"

#: B58 (2026-07-13) — container-anchor annexation guard. Two closed holes
#: over B22a (plane ``Issue`` 174K/53% board, novu ``Notifications``
#: 120K/52%, cal.com ``Bookings``+apps/api/v2 — keyed forensics):
#:
#: * Seg A — the B22a guard judged a host FOREIGN by unit-UNANIMITY over
#:   its full evidence union (``prefixes + files``); a same-key MERGE
#:   (plane: svc:issue + issue-named dirs in packages/types/constants)
#:   makes a central host multi-unit ⇒ ``None`` ⇒ never foreign ⇒ it may
#:   annex every cross-unit flowful dev (the i18n 125K exhibit). The
#:   guard now derives the host's unit from its CANONICAL identity (the
#:   anchor-id-embedded path, then route-file unanimity, then evidence
#:   unanimity) and covers the ENTRY and SPAN rescue rungs, which B22a
#:   never wrapped (the cal.com fold:entry->ws:apps/api/v2 exhibit). A
#:   unit-coherent flowful dev is never force-bound across a workspace
#:   boundary — it lanes with its own unit (B22a's honest refusal).
#: * Seg B — an anchor wholly inside a workspace unit whose unit-ROOT
#:   path carries a dev-artifact segment (``playground/*`` — the novu
#:   example apps the repo's own pnpm-workspace.yaml declares) never
#:   mints (bar ``dev_artifact_unit``). Unit-root grain only: cal.com's
#:   product admin PAGE named ``playground`` (a route leaf inside
#:   apps/web) can never fire — the Lane-C "measured out" verdict on
#:   page-grain tokens is preserved. Tokens live in
#:   ``spine-anchor-vocab.yaml:unit_root_artifact_tokens`` (data).
#:
#: Default OFF; ``FAULTLINE_ANNEXATION_GUARD=1`` arms both segments.
ANNEXATION_GUARD_ENV = "FAULTLINE_ANNEXATION_GUARD"

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
#: W4.2 Fix 1 — devs of a technology-instrument anchor (packages/ui,
#: packages/prisma …) lane directly under this reason; the fold ladder
#: never routes instrument code into a product capability.
_SHARED_REASON_INSTRUMENT = "technology_instrument"
#: FILELANE (2026-07-08) — a FILE-level shared-infra resident: an unowned
#: file with high import fan-in across DISTINCT product features and no
#: product surface (``faultline.pipeline_v2.file_lane``). The lane's file
#: mirror of the instrument reason (package-level); both are neutral
#: ground the validator I15 denominator excludes.
_SHARED_REASON_INFRA_FANIN = "shared_infra_fanin"
#: B22a — a flowful dev ISOLATED in a workspace unit that holds no minted
#: anchor: every target the ancestor-walk could reach sits across a
#: workspace-unit boundary, so annexing it would poison the owner map (the
#: documenso apps/docs / apps/openpage-api class). It lanes WITH its unit
#: (the unit's shell is already a lane resident) instead of riding a foreign
#: PF; counted as ``fold_walk_crossunit_laned``, never as a law breach.
_SHARED_REASON_CROSS_UNIT = "cross_unit_isolation"

#: W3.1 D4 — vendor-husk floor: a hub child with NO flow evidence must
#: own at least this many LOC of code to mint (else it folds under the
#: hub core / enclosing package as a dev-child). 150 is the valsem4 H9
#: calibration bound (2026-07-07): 13-scan sweep showed zero false
#: positives at 150 — midday's 27 app-store husks (logo.tsx + config.ts,
#: 27-34 LOC) and the comp `-(integration)` 0-LOC twins die, while
#: real-code 0-flow connectors (tracecat google 286 / microsoft 557,
#: Soc0 sentinelone 1,258) stay minted.
_HUB_HUSK_LOC_FLOOR = 150

#: B26 — bar reason for a hub child whose segment normalizes into the
#: plumbing/stop vocabulary (cal.com ``app-store/_utils`` shape): the
#: family's shared plumbing may not mint even with flow evidence; its
#: winners take the ordinary fold_hub_parent path.
_BAR_HUB_PLUMBING_CHILD = "hub_plumbing_child"


def _hub_child_segment(anchor: SpineAnchor) -> str:
    """The child-naming terminal segment of a hub-vendor anchor's
    canonical id (``hub:packages/app-store/_utils`` → ``_utils``)."""
    cid = anchor.canonical_id or ""
    if ":" not in cid:
        return ""
    tail = cid.split(":", 1)[1].rstrip("/")
    return tail.rsplit("/", 1)[-1] if tail else ""

#: W3.2 I9 fix — the lane carve-out fires ONLY for the app-shell /
#: shared-package class the law's own ruler (validator I9) exempts.
#: Universal monorepo conventions, mirrored from the ruler verbatim
#: (rule-no-repo-specific-paths: these are ecosystem tiers, not repos):
#: a structural shell NAME, or a path-majority under shared-package
#: roots / the dev's own ``apps/<name>`` shell. The W3.1 blanket
#: ws-anchor carve-out over-fired on typebot `user` (a small, coherent,
#: flowful account dev whose ws package held only 7/20 of its files) —
#: the one core I9 regression of wave31. A flowful ws-anchor dev that
#: FAILS this test is product code and must ride the rescue ladder
#: (span-vote → walk) like every other flowful dev.
_LANE_SHELL_NAME_HINTS = (
    "backend", "frontend", "main", "mock", "scripts", "infra", "platform",
)
_LANE_SHARED_PKG_ROOTS = (
    "packages/", "libs/", "internal/", "tooling/", "config/",
)


def _lane_shell_exempt(f: "Feature") -> bool:
    """``True`` when laning flowful *f* is exempt under I9's own rules."""
    name = (getattr(f, "name", "") or "").lower()
    if any(h in name for h in _LANE_SHELL_NAME_HINTS):
        return True
    paths = [str(p) for p in (getattr(f, "paths", None) or [])]
    if not paths:
        return False
    hits = sum(
        1 for p in paths
        if any(p.startswith(r) for r in _LANE_SHARED_PKG_ROOTS)
        or p.startswith(f"apps/{name}/")
    )
    return hits >= max(1, len(paths) // 2)


def anchored_mint_enabled() -> bool:
    """Default ON; ``FAULTLINE_SPINE_ANCHORED_MINT=0`` restores the old
    PF path (Stage 6.5/8 survivorship + 6.7d Call-2) for A/B."""
    return os.environ.get(ANCHORED_MINT_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def mint_domain_fold_enabled() -> bool:
    """Default ON; ``FAULTLINE_MINT_DOMAIN_FOLD_V2=0`` restores the
    byte-identical pre-B8c fold (a distinct-domain router stays folded into
    its plurality host instead of re-homing to its own surface PF)."""
    return os.environ.get(MINT_DOMAIN_FOLD_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def fold_crossapp_guard_enabled() -> bool:
    """Default ON; ``FAULTLINE_FOLD_CROSSAPP_GUARD=0`` restores the
    unguarded ancestor-walk (pre-B22a cross-app annexation) byte-identically
    for A/B."""
    return os.environ.get(FOLD_CROSSAPP_GUARD_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def annexation_guard_enabled() -> bool:
    """B58 — default OFF; ``FAULTLINE_ANNEXATION_GUARD=1`` arms the
    container-anchor annexation guard (Seg A canonical-unit fencing on
    the entry/span/walk rescue rungs + Seg B dev-artifact-unit mint
    bar). OFF is byte-identical to the pre-B58 pipeline."""
    return os.environ.get(ANNEXATION_GUARD_ENV, "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _workspace_unit_roots(ctx: Any) -> tuple[str, ...]:
    """Workspace-unit roots (an app or a workspace package), derived from
    the repo's OWN manifests — never from directory-name vocabulary.

    Primary: ``ctx.workspaces`` (Stage-0 ``detect_workspace`` output —
    pnpm-workspace.yaml globs, package.json "workspaces", turbo / nx /
    lerna / cargo / go). Fallback for non-workspace repos: a TOP-LEVEL dir
    carrying its own unit manifest (the spine-anchor
    ``unit_manifest_files`` convention — the Soc0 backend/ + frontend/
    class). Sorted longest-first so nested units resolve to the most
    specific root. Empty on single-unit repos → the guard is inert.
    """
    roots: set[str] = set()
    for w in (getattr(ctx, "workspaces", None) or []):
        p = str(getattr(w, "path", "") or "").replace("\\", "/").strip("/")
        if p:
            roots.add(p)
    if not roots:
        manifests = set(load_spine_vocab().get("unit_manifest_files") or [])
        for t in (getattr(ctx, "tracked_files", None) or []):
            segs = str(t).split("/")
            if len(segs) == 2 and segs[1] in manifests:
                roots.add(segs[0])
    return tuple(sorted(roots, key=lambda r: (-len(r), r)))


def _unit_of(path: str, unit_roots: tuple[str, ...]) -> str | None:
    """The most specific workspace unit holding *path* (longest-prefix
    match over ``unit_roots``), or ``None`` outside every unit."""
    for r in unit_roots:
        if path == r or path.startswith(r + "/"):
            return r
    return None


#: The surface token stripped to recover a route anchor's DOMAIN family. Only
#: ``-page`` appears as a surface-PF suffix in practice (``webhooks-page``,
#: ``settings-page``); the api-side domain anchors (``webhook``, ``setting``)
#: carry no surface token, so a single suffix is sufficient and scale-invariant.
_SURFACE_PAGE_SUFFIX = "-page"


def _domain_family(key: str) -> str:
    """Route-family DOMAIN of an anchor key: strip a trailing ``-page``
    surface token, then singularise the remaining stem with the SAME guarded
    house singularizer every anchor key already uses (``normalize_anchor_key``
    only singularises the LAST token, so ``webhooks-page`` keeps ``webhooks``
    plural — this collapses it).

    ``webhooks-page`` → ``webhook``; ``webhook`` → ``webhook``;
    ``detection`` → ``detection``; ``settings-page`` → ``setting``.
    A degenerate/empty stem falls back to the input key unchanged.
    """
    stem = key
    if stem.endswith(_SURFACE_PAGE_SUFFIX) and len(stem) > len(_SURFACE_PAGE_SUFFIX):
        stem = stem[: -len(_SURFACE_PAGE_SUFFIX)]
    return normalize_anchor_key(stem) or key


def _mint_domain_fold_rebinds(
    assignment: dict[str, tuple[str, str]],
    winner_by_dev: dict[str, "SpineAnchor | None"],
    anchor_by_id: dict[str, SpineAnchor],
    dev_by_name: dict[str, "Feature"],
) -> list[tuple[str, str, str]]:
    """B8c rail — re-home flowful devs FOLDED into a distinct-domain host
    onto their own EXISTING ``<domain>-page`` surface PF.

    Returns a deterministically-ordered list of
    ``(dev_name, surface_cid, provenance)`` rebinds; the caller applies them.
    Pure (no mutation, no I/O). The two structural rails, no magic counts:

    * **distinct-from-host** — the dev's route-family domain differs from the
      host PF's own route domain (a same-domain router — a ``detection`` router
      folding into ``detections`` — is NOT distinct → stays folded).
    * **existing surface PF** — a genuine ``<domain>-page`` page-route anchor
      that ALREADY receives ≥1 dev in *assignment*. Because the target already
      mints, PF count cannot rise and no product-judgment "should this domain
      be a PF?" call is made — the engine already decided by minting the page.
      A surface-less domain (admin/compliance/entra: no page PF) never
      qualifies → stays folded.
    """
    # Index existing page-surface PFs by domain family. A candidate is a route
    # anchor whose cid ends ``-page``, that carries ≥1 page_route_file (a real
    # frontend surface, not an api-only route), AND that already receives a dev
    # (so it is an EXISTING PF — the rail never mints). Deterministic: iterate
    # sorted cids and keep the alpha-min on the (rare) same-family collision.
    existing_targets = {cid for cid, _prov in assignment.values()}
    surface_by_family: dict[str, str] = {}
    for cid in sorted(existing_targets):
        a = anchor_by_id.get(cid)
        if a is None or a.source != "route":
            continue
        if not a.canonical_id.endswith(_SURFACE_PAGE_SUFFIX):
            continue
        if not a.page_route_files:
            continue
        surface_by_family.setdefault(_domain_family(a.key), cid)
    if not surface_by_family:
        return []

    rebinds: list[tuple[str, str, str]] = []
    for name in sorted(assignment):
        host_cid, prov = assignment[name]
        if not prov.startswith("fold:"):
            continue  # only re-home FOLDED devs — lineage / mint stay put
        f = dev_by_name.get(name)
        if f is None or not getattr(f, "flows", None):
            continue  # flowful devs only (the fold LAW's own population)
        w = winner_by_dev.get(name)
        if w is None or not w.canonical_id.startswith("route:"):
            continue  # route-family domain only
        dev_fam = _domain_family(w.key)
        host_a = anchor_by_id.get(host_cid)
        host_fam = (
            _domain_family(host_a.key)
            if host_a is not None and host_a.canonical_id.startswith("route:")
            else None
        )
        if host_fam == dev_fam:
            continue  # same domain → still folds (anti-case preserved)
        surf_cid = surface_by_family.get(dev_fam)
        if surf_cid is None or surf_cid == host_cid:
            continue  # surface-less, or already home
        rebinds.append((name, surf_cid, f"fold:surface->route:{dev_fam}"))
    return rebinds


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


def _files_loc(
    repo_root: Path,
    rel_paths: list[str],
    cache: dict[str, int],
) -> int:
    """Summed LOC of *rel_paths* per the engine's Stage-6.97 counting
    convention (tests / generated / binary / missing count 0). Cached
    per path; called only for flowless hub children (a handful of small
    files per repo), so the IO is bounded."""
    from faultline.pipeline_v2.stage_6_97_feature_loc import count_file_loc

    total = 0
    for rel in rel_paths:
        if rel not in cache:
            cache[rel] = count_file_loc(repo_root / rel, rel)
        total += cache[rel]
    return total


def _anchor_in_instrument_dirs(
    anchor: SpineAnchor,
    instrument_dirs: frozenset[str],
) -> bool:
    """Every membership unit of *anchor* (prefixes; files when it has no
    prefixes) sits inside a technology-instrument dir. Page evidence or a
    nav mention keeps the anchor product (S3 belt at anchor grain)."""
    if not instrument_dirs:
        return False
    if anchor.page_route_files or anchor.nav_confirmed:
        return False

    def _inside(p: str) -> bool:
        return any(p == d or p.startswith(d + "/") for d in instrument_dirs)

    units: list[str] = list(anchor.prefixes or ())
    if not units:
        units = sorted(anchor.files)
    return bool(units) and all(_inside(u) for u in units)


def _mint_bar(
    anchor: SpineAnchor,
    winners: list["Feature"],
    flow_entries: dict[str, list[str]],
    repo_has_pages: bool,
    code_exts: tuple[str, ...],
    repo_root: Path,
    loc_cache: dict[str, int],
    instrument_dirs: frozenset[str] = frozenset(),
    plumbing_keys: frozenset[str] = frozenset(),
) -> str | None:
    """``None`` when the anchor may mint, else the bar reason."""
    if not winners:
        return "no_winning_devs"
    if anchor.shell:
        return "shell"
    if anchor.barred:
        return anchor.barred  # single_letter | version_dir
    # W4.2 Fix 1 — technology instruments (dev tooling by mechanism, not
    # dictionary: manifest grounding / import asymmetry / no product
    # surfaces, >=2 signals in technology_instruments.py) never mint.
    if _anchor_in_instrument_dirs(anchor, instrument_dirs):
        return "technology_instrument"
    if anchor.sources == frozenset({"svc"}):
        return "service_dir_only"
    if anchor.source == "hub-vendor":
        # B26 backstop — a hub child whose segment NORMALIZES into the
        # plumbing/stop vocabulary (and names no vendor) is the family's
        # shared plumbing: it may not mint EVEN WITH flow evidence (a
        # helper dir is always somebody's call-chain entry, so the flow
        # bar can never stop this class). Its winners take the existing
        # fold_hub_parent path to the enclosing package / hub core. The
        # spine filter (Fix A) already blocks the dir-per-vendor shape;
        # this rung covers every other path an anchor can reach the bar
        # (file-per-vendor token sets, cross-family merges).
        if (plumbing_keys and hub_plumbing_child_enabled()
                and hub_child_is_plumbing(
                    _hub_child_segment(anchor), plumbing_keys)):
            return _BAR_HUB_PLUMBING_CHILD
        if _anchor_flow_evidence(anchor, winners, flow_entries):
            return None
        if anchor.hub_parent_generic:
            # Generic-container family (backend/routers, backend/models):
            # a vendor-named file with NO flow is not an integration PF.
            return "hub_child_no_flow"
        child_files: set[str] = set(anchor.files)
        for w in winners:
            child_files.update(anchor.matched_set(owned_paths_of(w)))
        code_files = [p for p in sorted(child_files)
                      if _is_code(p, code_exts)]
        if not code_files:
            return "hub_stub_child"  # single static file class (FDW wrappers)
        # W3.1 D4 (fb3 dossier, valsem4 H9): a flowless child needs a
        # BODY, not just a code file — the old any-code prong let
        # logo.tsx + config.ts marketplace husks mint (midday app-store
        # ×27; comp `aws` + `aws-(integration)` 0-LOC dup twins). Under
        # the floor the husk folds under its hub core / enclosing
        # package as a dev-child; the same-vendor dup-pair class dies
        # with it (the husk twin never mints, so no pair exists).
        if _files_loc(repo_root, code_files, loc_cache) < _HUB_HUSK_LOC_FLOOR:
            return "hub_husk_child"
        return None
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
    # B26 — the authored plumbing/stop vocabulary union for the
    # hub_plumbing_child mint-bar backstop (data, not code).
    plumbing_keys = frozenset(
        vocab.get("hub_plumbing_segments") or []
    ) | frozenset(vocab.get("structural_stoplist") or [])

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

    # W4.2 Fix 1 — technology-instrument detection (operator principle:
    # mechanisms, not dictionaries). Runs ONCE per scan, here — the mint
    # is the earliest consumer, and suppression must happen AT the mint
    # (the 6.7d PF-backstop would otherwise seed journeys onto the fake
    # capability — the typebot "Run prisma" exhibit).
    from faultline.pipeline_v2.technology_instruments import (
        detect_technology_instruments,
        tech_instruments_enabled,
    )

    instrument_dirs: frozenset[str] = frozenset()
    if tech_instruments_enabled():
        fdir_units = sorted({
            p for a in anchors if "fdir" in a.sources for p in a.prefixes
        })
        hub_dirs = sorted({a.hub_dir for a in anchors if a.hub_dir})
        # B48 S3 (nav) — the author's nav-declared anchor subtrees; a
        # candidate covered by one is a product area, never a lane.
        nav_prefixes = sorted({
            p for a in anchors if getattr(a, "nav_confirmed", False)
            for p in a.prefixes
        })
        try:
            # R4 — hand the detector the ctx-shared source cache (it has
            # no ctx of its own; ``None`` → it constructs locally).
            from faultline.pipeline_v2.shared_source import (
                shared_source_cache as _shared_src,
            )
            ti_tele = detect_technology_instruments(
                Path(getattr(ctx, "repo_path", ".")),
                [str(p) for p in (getattr(ctx, "tracked_files", None) or [])],
                routes_index,
                fdir_units=fdir_units,
                hub_dirs=hub_dirs,
                source_cache=_shared_src(
                    ctx, Path(getattr(ctx, "repo_path", "."))),
                nav_prefixes=nav_prefixes,
            )
            instrument_dirs = frozenset(ti_tele.get("dirs") or [])
            tele["technology_instruments"] = ti_tele
        except Exception as exc:  # noqa: BLE001 — detector never breaks a scan
            tele["technology_instruments"] = {"enabled": True,
                                              "error": str(exc)}

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
    mint_repo_root = Path(getattr(ctx, "repo_path", "."))
    loc_cache: dict[str, int] = {}
    # Workspace-unit roots, computed ONCE — consumed by the B22a walk
    # guard below and by the B58 annexation guard (Seg A fencing + Seg B
    # dev-artifact-unit bar). Empty tuple on single-unit repos.
    _ws_unit_roots: tuple[str, ...] = _workspace_unit_roots(ctx)
    _annex_on: bool = annexation_guard_enabled()
    bar_by_anchor: dict[str, str | None] = {}
    for cid in sorted(winners_by_anchor):
        a = anchor_by_id[cid]
        bar_by_anchor[cid] = _mint_bar(
            a, winners_by_anchor[cid], flow_entries, repo_has_pages,
            code_exts, mint_repo_root, loc_cache,
            instrument_dirs=instrument_dirs, plumbing_keys=plumbing_keys)
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
        the dev's flow enters via the integrations page).

        W3.1 D1/D6 DEFER RULE: when the entries ALSO majority-sit inside
        a MORE SPECIFIC unminted route anchor carrying >= 2 page routes
        (a real capability surface, the I24 grain), the fold DEFERS to
        the pending ladder — rung L1 mints that anchor on demand.
        Without it a workspace-scoped app router (tracecat: everything
        under /workspaces/[id]/...) entry-folds tables/integrations/
        settings into the coarse minted `Workspaces` PF (46K LOC — the
        fb3 D1 sink shape at the entry rung). A single-page sub-surface
        (supabase FDW wrappers, 1 page) still folds under its hosting
        capability — the operator amendment case 5d grain."""
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
        if votes[best] * 2 <= len(entries):
            return None
        # Defer to L1 when a finer >=2-page route anchor holds the
        # entry majority (deterministic: sorted candidates, first hit).
        route_votes: Counter[str] = Counter()
        for ep in entries:
            fine_cid: str | None = None
            fine_spec: tuple[int, int, str] | None = None
            for a in anchors:
                if (a.source != "route" or a.canonical_id in mintable
                        or a.shell or a.barred or not a.matches(ep)):
                    continue
                spec = (
                    1 if ep in a.files else 0,
                    max((len(p) for p in a.prefixes
                         if ep.startswith(p + "/") or ep == p), default=0),
                    a.canonical_id,
                )
                if fine_spec is None or spec > fine_spec:
                    fine_cid, fine_spec = a.canonical_id, spec
            if fine_cid is not None:
                route_votes[fine_cid] += 1
        if route_votes:
            (fine, fn), = route_votes.most_common(1)
            fine_tied = sorted(c for c, v in route_votes.items() if v == fn)
            fine = fine_tied[0]
            fa = anchor_by_id.get(fine)
            if (fa is not None
                    and route_votes[fine] * 2 > len(entries)
                    and len(fa.page_route_files) >= 2
                    and fa.subtree_inside(anchor_by_id[best])):
                tele["entry_fold_deferred_to_l1"] = (
                    tele.get("entry_fold_deferred_to_l1", 0) + 1)
                return None
        return best

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
        # B58 Seg A — L1 is an entry-shaped rung the B22a guard never
        # wrapped: a unit-coherent dev never entry-mints across a
        # workspace unit (the ladder falls through to span/walk, which
        # lane it honestly when every target is foreign).
        if _annex_foreign(f, best):
            tele["annex_guard_entry_blocked"] = (
                tele.get("annex_guard_entry_blocked", 0) + 1)
            return None
        if best in mintable:
            return best
        a = anchor_by_id[best]
        if a.shell:
            return None
        bar = _mint_bar(a, [f], flow_entries, repo_has_pages, code_exts,
                        mint_repo_root, loc_cache,
                        instrument_dirs=instrument_dirs,
                        plumbing_keys=plumbing_keys)
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

    # ── B22a — cross-app fold guard state (walk rung only) ───────────
    # Unit roots from the repo's own manifests; empty tuple ⇒ guard inert
    # (single-unit repos, or kill-switch FAULTLINE_FOLD_CROSSAPP_GUARD=0).
    _fold_guard_units: tuple[str, ...] = (
        _ws_unit_roots if fold_crossapp_guard_enabled() else ())
    _anchor_unit_cache: dict[str, str | None] = {}

    def _anchor_unit(cid: str) -> str | None:
        """The single workspace unit holding the anchor's evidence, or
        ``None`` when the evidence spans ≥2 units or sits wholly outside
        every unit (a genuinely cross-unit / non-unit anchor is never
        foreign — the guard cannot call it an annexation). Evidence
        OUTSIDE any unit (root-level files) does not veto a single-unit
        anchor — a ``packages/email`` anchor with a stray root manifest
        is still a packages/email anchor (documenso keyless
        calibration: ``route:email`` must stay foreign to
        ``apps/openpage-api`` devs)."""
        if cid not in _anchor_unit_cache:
            a = anchor_by_id.get(cid)
            unit: str | None = None
            if a is not None:
                units = {
                    u for ev in list(a.prefixes) + sorted(a.files)
                    if (u := _unit_of(str(ev), _fold_guard_units)) is not None
                }
                if len(units) == 1:
                    unit = next(iter(units))
            _anchor_unit_cache[cid] = unit
        return _anchor_unit_cache[cid]

    # ── B58 Seg A — canonical anchor unit + all-rung fencing ─────────
    _canon_unit_cache: dict[str, str | None] = {}

    def _canonical_anchor_unit(cid: str) -> str | None:
        """The workspace unit of the anchor's CANONICAL identity — not
        its post-merge evidence union. Rungs, most-canonical first:

        1. the anchor-id-embedded path (``svc:apps/web/core/services/
           issue`` → apps/web; ws:/fdir:/pypkg:/hub:/route:<prefix> all
           embed their own subtree) — a same-key merge widens evidence
           but never rewrites the head's id;
        2. unit UNANIMITY over the anchor's OWN route surface files
           (page + api) — a key-only route anchor (``route:space``) is
           identified by where its routes live;
        3. the B22a evidence-unanimity fallback (``_anchor_unit``).

        ``None`` ⇒ genuinely cross-unit — never called foreign."""
        if cid not in _canon_unit_cache:
            unit: str | None = None
            a = anchor_by_id.get(cid)
            if a is not None:
                tail = cid.split(":", 1)[1] if ":" in cid else cid
                if "/" in tail:
                    unit = _unit_of(tail, _ws_unit_roots)
                if unit is None:
                    surface = sorted(a.page_route_files | a.api_route_files)
                    units = {
                        u for p in surface
                        if (u := _unit_of(str(p), _ws_unit_roots)) is not None
                    }
                    if len(units) == 1:
                        unit = next(iter(units))
                if unit is None:
                    unit = _anchor_unit(cid)
            _canon_unit_cache[cid] = unit
        return _canon_unit_cache[cid]

    def _dev_home_unit(f: "Feature") -> str | None:
        """The single workspace unit holding the dev's own files, or
        ``None`` when they span units / sit outside every unit (a
        non-coherent dev is never fenced — B22a semantics)."""
        owned = owned_by_dev.get(f.name) or []
        units = {
            u for p in owned
            if (u := _unit_of(p, _ws_unit_roots)) is not None
        }
        return next(iter(units)) if len(units) == 1 else None

    def _annex_foreign(f: "Feature", target_cid: str) -> bool:
        """B58 Seg A predicate — ``True`` when force-binding *f* onto
        *target_cid* would cross a workspace-unit boundary: the dev is
        unit-coherent AND the target's canonical unit is a DIFFERENT
        unit. Either side unresolved ⇒ never foreign (conservative)."""
        if not (_annex_on and _ws_unit_roots):
            return False
        home = _dev_home_unit(f)
        if home is None:
            return False
        tgt = _canonical_anchor_unit(target_cid)
        return tgt is not None and tgt != home

    def _ancestor_walk(f: "Feature") -> tuple[str | None, bool]:
        """W2b.1 law rung L3 — nearest-ancestor plurality: walk UP from
        the dev's owned files' common dir; the first level where ANY
        assigned dev owns files decides by plurality of their anchors.
        Total whenever ≥1 dev is assigned (the repo-root level sees
        every assigned file).

        B22a cross-app fold guard (default ON): a vote for an anchor
        whose evidence sits wholly inside a FOREIGN workspace unit — a
        unit holding none of the dev's own files — is void; the walk may
        never annex a dev across the workspace boundary (the documenso
        ``ws:packages/trpc`` exhibit). Same-unit folds and a package
        folding within its own subtree are untouched. Returns
        ``(target, guard_isolated)``: ``guard_isolated`` is True only
        when the guard voided EVERY target the unguarded walk would have
        reached (the caller lanes the dev with its own unit instead of
        recording a law breach)."""
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

        def _walk(skip: frozenset[str]) -> str | None:
            level = common
            while True:
                votes: Counter[str] = Counter()
                for p, cid in assigned_file_cid.items():
                    if cid in skip:
                        continue
                    if not level or p.startswith(level + "/"):
                        votes[cid] += 1
                if votes:
                    (_best, n), = votes.most_common(1)
                    tied = sorted(c for c, v in votes.items() if v == n)
                    return tied[0]
                if not level:
                    return None
                level = level.rsplit("/", 1)[0] if "/" in level else ""

        # B58 Seg A: with the annexation guard armed the walk fences by
        # the target's CANONICAL unit (closing the multi-unit-evidence
        # host hole) and stays armed even when the B22a flag alone is
        # kill-switched. OFF ⇒ byte-identical B22a behaviour.
        guard_units = _fold_guard_units or (
            _ws_unit_roots if _annex_on else ())
        if not guard_units:
            return _walk(frozenset()), False
        dev_units = frozenset(
            u for p in owned
            if (u := _unit_of(p, guard_units)) is not None)
        if not dev_units:
            return _walk(frozenset()), False
        unit_fn = _canonical_anchor_unit if _annex_on else _anchor_unit
        foreign = frozenset(
            cid for cid in set(assigned_file_cid.values())
            if (au := unit_fn(cid)) is not None and au not in dev_units)
        if not foreign:
            return _walk(frozenset()), False
        guarded = _walk(foreign)
        if os.environ.get("FAULTLINE_MINT_DEBUG") == "1":
            tele.setdefault("fold_debug", []).append({
                "dev": f.name, "rung": "walk-guard",
                "dev_units": sorted(dev_units),
                "foreign": sorted(foreign)[:8],
                "guarded": guarded,
                "guarded_unit": (_anchor_unit(guarded)
                                 if guarded is not None else None),
            })
        if guarded is not None:
            if guarded != _walk(frozenset()):
                # the annexation fix: the dev re-homes inside a unit it
                # actually lives in instead of the foreign plurality.
                tele["fold_walk_crossunit_rehomed"] = (
                    tele.get("fold_walk_crossunit_rehomed", 0) + 1)
            return guarded, False
        # every reachable target is cross-unit ⇒ the dev is ISOLATED in
        # a unit with no minted anchor; annexing it is the B22a disease.
        return None, _walk(frozenset()) is not None

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
                # B58 Seg A — the entry rung was never B22a-guarded (the
                # cal.com fold:entry->ws:apps/api/v2 annexation): a
                # unit-coherent dev never entry-folds across a unit.
                if ef is not None and _annex_foreign(f, ef):
                    tele["annex_guard_entry_blocked"] = (
                        tele.get("annex_guard_entry_blocked", 0) + 1)
                    ef = None
                if ef is not None:
                    assignment[f.name] = (ef, "fold:entry->none")
                    tele["fold_entry"] = tele.get("fold_entry", 0) + 1
                else:
                    fold_pending.append((f, None, _SHARED_REASON_NONE))
            continue
        # W4.2 Fix 1 — an instrument anchor's devs NEVER fold into a
        # product capability (attributing the UI kit / ORM package to a
        # random importing feature is exactly the mis-attribution the
        # lane exists for). Flowless devs lane under
        # ``technology_instrument``; a flowful dev still rides the LAW
        # rescue (span-vote → walk) below — the lane law holds.
        if bar_by_anchor.get(w.canonical_id) == "technology_instrument":
            tele["instrument_devs"] = tele.get("instrument_devs", 0) + 1
            fold_pending.append((f, w, _SHARED_REASON_INSTRUMENT))
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
        # B58 Seg A — entry-rung fence (see the none-winner site above).
        if ef is not None and _annex_foreign(f, ef):
            tele["annex_guard_entry_blocked"] = (
                tele.get("annex_guard_entry_blocked", 0) + 1)
            ef = None
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
        # HUSK FOLD (W3.1 D4): a vendor-husk child folds under its hub
        # core / enclosing package as a dev-child — the fb3 amendment
        # rule ("flowful OR >= 150 owned LOC, else fold into the parent
        # integrations PF"). B26: a plumbing-named child
        # (hub_plumbing_child) takes the same hub-parent path — its
        # flowful devs usually resolve one rung earlier via the entry
        # fold (entries sit in the enclosing package's subtree); this
        # rung catches the flowless / entry-miss residue.
        if (bar_by_anchor.get(w.canonical_id)
                in {"hub_husk_child", _BAR_HUB_PLUMBING_CHILD}
                and parent is not None):
            assignment[f.name] = (parent, f"fold:hub-parent->{w.canonical_id}")
            tele["fold_hub_parent"] = tele.get("fold_hub_parent", 0) + 1
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
        # R4 — adopt the ctx-shared source cache (fallback: local).
        from faultline.pipeline_v2.shared_source import shared_source_cache
        src_cache = (shared_source_cache(ctx, repo_path)
                     or _SourceCache(repo_path))
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
            if reason == _SHARED_REASON_INSTRUMENT:
                # Fix 1: instrument code is DEPENDED ON by everything —
                # the import vote is exactly backwards for it.
                still_pending.append((f, w, reason))
                continue
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
    #
    # W3.2 NARROWING — the ws-anchor MARKER alone over-fires: it also
    # tags small coherent capability devs (typebot `user`: 8 account
    # flows, 20 files split packages/user + builder features/user) whose
    # laning the ruler does NOT exempt → wave31's one core I9 breach.
    # The carve-out now additionally requires the SHELL SHAPE the ruler
    # itself exempts (``_lane_shell_exempt``); ws-marker devs that fail
    # it ride the same guarded rescue ladder (span-vote → walk) as every
    # other flowful dev — the law holds for every flowful dev regardless
    # of anchor family, lane only if truly flowless.
    from faultline.pipeline_v2.stage_8_7_anchor_desink import (
        _is_workspace_anchor,
    )

    for f, w, reason in fold_pending:
        flowful = bool(getattr(f, "flows", None))
        if flowful and _is_workspace_anchor(f):
            if _lane_shell_exempt(f):
                tele["law_ws_anchor_laned"] = (
                    tele.get("law_ws_anchor_laned", 0) + 1)
                infra[f.name] = (
                    _SHARED_REASON_SHELL if w is not None and w.shell
                    else reason)
                continue
            # marker present but shape says product code — released to
            # the ladder (tracked for the smoke gates).
            tele["law_ws_anchor_released"] = (
                tele.get("law_ws_anchor_released", 0) + 1)
        if flowful and mintable:
            src = w.canonical_id if w is not None else "none"
            target = _span_vote(f)
            # B58 Seg A — the span rung was never B22a-guarded (novu
            # fold:span->ws:packages/js): a unit-coherent dev never
            # span-binds across a workspace unit; it falls through to
            # the guarded walk (and lanes when every target is foreign).
            if target is not None and _annex_foreign(f, target):
                tele["annex_guard_span_blocked"] = (
                    tele.get("annex_guard_span_blocked", 0) + 1)
                target = None
            if os.environ.get("FAULTLINE_MINT_DEBUG") == "1":
                tele.setdefault("fold_debug", []).append({
                    "dev": f.name, "rung": "span", "target": target,
                })
            if target is not None:
                assignment[f.name] = (target, f"fold:span->{src}")
                tele["fold_span_vote"] = tele.get("fold_span_vote", 0) + 1
                continue
            target, guard_isolated = _ancestor_walk(f)
            if target is not None:
                assignment[f.name] = (target, f"fold:walk->{src}")
                tele["fold_ancestor_walk"] = (
                    tele.get("fold_ancestor_walk", 0) + 1)
                continue
            if guard_isolated:
                # B22a — every walk target sits across a workspace-unit
                # boundary: the dev lanes WITH its own unit (whose shell
                # is already a lane resident) instead of poisoning a
                # foreign PF's owner map. Deliberately NOT counted as
                # ``law_flowful_in_lane`` — this is the guard's honest
                # refusal, not an assignment-less degenerate scan.
                tele["fold_walk_crossunit_laned"] = (
                    tele.get("fold_walk_crossunit_laned", 0) + 1)
                infra[f.name] = _SHARED_REASON_CROSS_UNIT
                continue
        if flowful:
            # Reachable ONLY on degenerate scans (zero mintable anchors,
            # or an assignment-less walk) — tracked so the smoke gates
            # can assert the law held (0 on every real product scan).
            tele["law_flowful_in_lane"] = (
                tele.get("law_flowful_in_lane", 0) + 1)
        infra[f.name] = reason

    # B8c domain-fold rail (mint-time) — re-home flowful devs that folded
    # into a DISTINCT-domain host onto their own EXISTING <domain>-page
    # surface PF (the webhook 1→18 unification). Binds only to surfaces that
    # already receive a dev, so PF count cannot rise; same-domain and
    # surface-less routers stay folded. Runs AFTER the whole fold ladder so
    # every fold provenance (walk / import / span / …) is visible, and BEFORE
    # Pass 4 so the re-homed devs mint under the surface PF. Kill-switch
    # ``FAULTLINE_MINT_DOMAIN_FOLD_V2=0`` skips it entirely (byte-identical).
    if mint_domain_fold_enabled():
        _rebind_dev_by_name = {f.name: f for f in in_scope}
        for _name, _surf_cid, _prov in _mint_domain_fold_rebinds(
                assignment, winner_by_dev, anchor_by_id, _rebind_dev_by_name):
            assignment[_name] = (_surf_cid, _prov)
            tele["mint_domain_fold_rebind"] = (
                tele.get("mint_domain_fold_rebind", 0) + 1)

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
    # R4 — adopt the ctx-shared source cache (fallback: local).
    from faultline.pipeline_v2.shared_source import shared_source_cache
    cache = shared_source_cache(ctx, repo_path) or _SourceCache(repo_path)
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
    user_flows: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Emission-time lane rows for the anchored residual: one entry PER
    resident dev (name, files, loc, reason). Zero-loss: residents stay
    in ``features[]`` (Layer-1 truth) with ``product_feature_id=None``;
    the lane is the explainability surface (I22 reads it post-W2b).

    B52 (``FAULTLINE_FLOWFUL_TRANSPORT_LANE``): a FLOW-BEARING lane row
    (laned transport residue) additionally carries ``flow_ids[]`` (the
    resident's flow uuids, list order) and ``journeys[]`` (``{id, name}``
    of the ``user_flows[]`` rows whose ``lane_ref`` points at this row's
    uuid) — ADDITIVE, emitted ONLY when the flag is ON and the list is
    non-empty, so every existing board (including flowless documenso
    trpc) stays byte-identical."""
    from faultline.pipeline_v2.emission_integrity import ANCHORED_HUSK_REASON
    from faultline.pipeline_v2.transport_handoff import (
        FLOWFUL_LANE_ANCHOR,
        flowful_transport_lane_enabled,
    )

    flowful_lane = flowful_transport_lane_enabled()
    journeys_by_ref: dict[str, list[dict[str, Any]]] = {}
    if flowful_lane:
        for uf in (user_flows or []):
            ref = getattr(uf, "lane_ref", None)
            if ref:
                journeys_by_ref.setdefault(str(ref), []).append({
                    "id": getattr(uf, "id", None),
                    "name": getattr(uf, "name", None),
                })

    rows: list[dict[str, Any]] = []
    # The three amendment reasons the MINT stamps + the W4.2 additions:
    # the anchored-husk shell reason (emission_integrity unbinds a
    # dropped husk's 0-owned / 0-flow devs into this lane — zero-loss,
    # I22-visible) and the technology-instrument reason (Fix 1: dev
    # tooling lanes, never mints).
    lane_reasons = {_SHARED_REASON_NONE, _SHARED_REASON_BAR,
                    _SHARED_REASON_SHELL, ANCHORED_HUSK_REASON,
                    _SHARED_REASON_INSTRUMENT, _SHARED_REASON_INFRA_FANIN,
                    _SHARED_REASON_CROSS_UNIT}
    for f in developer_features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        if getattr(f, "product_feature_id", None) is not None:
            continue
        reason = getattr(f, "shared_reason", None)
        # ONLY the reasons in the accepted set above (review F4): a
        # pfid=None dev some other stage tagged with a different reason
        # (non_product_surface / genuinely_shared_infra / facet_view) is
        # that stage's concern, never a lane resident.
        if reason not in lane_reasons:
            continue
        row: dict[str, Any] = {
            "name": f.name,
            "display_name": getattr(f, "display_name", None) or f.name,
            "shared_reason": reason,
            "uuid": getattr(f, "uuid", "") or "",
            "paths": list(getattr(f, "paths", None) or []),
            "loc": getattr(f, "loc", None),
            "loc_shared": getattr(f, "loc_shared", None),
            "flows": len(getattr(f, "flows", None) or []),
        }
        if flowful_lane:
            # B52 flow-bearing lane representation (additive; flow_ids
            # ONLY on rows whose dev the flowful-lane branch itself
            # laned — the provenance anchor — so a pre-existing flowful
            # resident (documenso openpage-api) stays byte-identical
            # under the flag, and flowless rows never change).
            if getattr(f, "anchor_id", None) == FLOWFUL_LANE_ANCHOR:
                fids = [str(getattr(fl, "uuid", "") or "")
                        for fl in (getattr(f, "flows", None) or [])]
                fids = [x for x in fids if x]
                if fids:
                    row["flow_ids"] = fids
            js = journeys_by_ref.get(row["uuid"])
            if js:
                row["journeys"] = js
        rows.append(row)
    rows.sort(key=lambda r: r["name"])
    return rows


# ── W4.2 — post-UF vendor-husk fold (D4's missing journey ruler) ─────────

_HUSK_POST_UF_ENV = "FAULTLINE_HUSK_POST_UF_FOLD"


def husk_post_uf_fold_enabled() -> bool:
    """Default ON; ``FAULTLINE_HUSK_POST_UF_FOLD=0`` disables."""
    return os.environ.get(_HUSK_POST_UF_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def fold_unreferenced_vendor_husks(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    user_flows: list[Any],
) -> dict[str, Any]:
    """W4.2 (operator exhibit: midday ``Enable Banking`` I8) — the D4
    vendor-husk rule gains the ruler it could not see at mint time: the
    JOURNEY layer.

    At Stage 6.86 the flowless hub-vendor floor is LOC-only (``≥ 150``
    mints — the valsem4 H9 bound; enablebanking's 1,162 real LOC passed
    it legitimately, no floor slip). But a flowless vendor child that the
    settled journey layer ALSO never cites is the operator's "фіча без
    юзер-фловів" anomaly (validator I8's journeys-worthy prong): real
    code, no behavioral evidence, no journey — a standalone PF row tells
    a PM nothing its hub cannot. Post-UF (after 6.7d + every seed
    channel), such a child folds under its hub core sibling — or, when
    the family minted no core (midday's providers hub), the nearest
    enclosing minted capability (``Banking``) — exactly where D4 sends
    sub-floor husks at mint time. Runs BEFORE Stage 6.97, so dual-LOC
    accounting re-truths itself.

    Journey-cited children (midday ``gocardless``) and flow-evidenced
    children are untouched. Deterministic, $0. Kill-switch:
    ``FAULTLINE_HUSK_POST_UF_FOLD=0``.
    """
    from faultline.pipeline_v2.hub_relation import HUB_PARENT_SEGMENTS

    tele: dict[str, Any] = {"enabled": True, "checked": 0, "folded": [],
                            "no_target": 0}
    if not husk_post_uf_fold_enabled():
        tele["enabled"] = False
        return tele

    uf_refs: Counter[str] = Counter()
    for uf in user_flows:
        ref = getattr(uf, "product_feature_id", None)
        if ref:
            uf_refs[str(ref)] += 1

    devs_by_pf: dict[str, list["Feature"]] = defaultdict(list)
    for f in developer_features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        pfid = getattr(f, "product_feature_id", None)
        if pfid:
            devs_by_pf[str(pfid)].append(f)

    def _anchor_path(pf: "Feature") -> str | None:
        aid = str(getattr(pf, "anchor_id", None) or "")
        if ":" not in aid:
            return None
        return aid.split(":", 1)[1] or None

    def _seg_stem(seg: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", seg.lower())

    # Vendor-child candidates: hub: anchors whose PARENT dir basename is a
    # hub container segment (providers / integrations / connectors …) —
    # hub COREs (anchored at the container itself) never qualify.
    candidates: list["Feature"] = []
    for pf in sorted(product_features,
                     key=lambda p: str(getattr(p, "name", "") or "")):
        aid = str(getattr(pf, "anchor_id", None) or "")
        if not aid.startswith("hub:"):
            continue
        path = aid[4:]
        if "/" not in path:
            continue
        parent = path.rsplit("/", 1)[0]
        if _seg_stem(parent.rsplit("/", 1)[-1]) not in {
            _seg_stem(s) for s in HUB_PARENT_SEGMENTS
        }:
            continue
        tele["checked"] += 1
        key = str(getattr(pf, "name", "") or "")
        if uf_refs.get(key):
            continue  # journey-cited — a real board row
        members = devs_by_pf.get(key, [])
        if (getattr(pf, "flows", None) or []) or any(
            getattr(m, "flows", None) for m in members
        ):
            continue  # flow-evidenced — D4's mint-time verdict stands
        candidates.append(pf)
    if not candidates:
        return tele

    husk_keys = {str(getattr(pf, "name", "") or "") for pf in candidates}
    pf_by_key = {
        str(getattr(pf, "name", "") or ""): pf for pf in product_features
    }

    def _fold_target(pf: "Feature") -> "Feature | None":
        path = _anchor_path(pf) or ""
        parent = path.rsplit("/", 1)[0]
        # 1. the hub core sibling (anchored at the container itself);
        # 2. the LONGEST enclosing path-anchored capability (ws-pkg /
        #    fdir / pypkg subtree strictly containing the child).
        best: tuple[int, str] | None = None
        for other in product_features:
            okey = str(getattr(other, "name", "") or "")
            if okey in husk_keys or okey.strip().lower() in (
                "platform", "shared-platform",
            ):
                continue
            opath = _anchor_path(other)
            if not opath:
                continue
            if opath == parent or path.startswith(opath + "/"):
                cand = (len(opath), okey)
                if best is None or cand > best:
                    best = cand
        return pf_by_key.get(best[1]) if best else None

    folded_keys: set[str] = set()
    for pf in candidates:
        key = str(getattr(pf, "name", "") or "")
        target = _fold_target(pf)
        if target is None:
            tele["no_target"] += 1
            continue
        tkey = str(getattr(target, "name", "") or "")
        aid = str(getattr(pf, "anchor_id", None) or "")
        for m in devs_by_pf.get(key, []):
            m.product_feature_id = tkey
            m.anchor_id = f"fold:husk-post-uf->{aid}"
            if getattr(m, "shared_reason", None):
                m.shared_reason = None
        # paths + member_files union onto the target (dedup, stable order).
        seen_p = set(getattr(target, "paths", None) or [])
        merged_p = list(getattr(target, "paths", None) or [])
        for p in (getattr(pf, "paths", None) or []):
            if p not in seen_p:
                seen_p.add(p)
                merged_p.append(p)
        target.paths = merged_p
        seen_mf = {
            (mf.get("path") if isinstance(mf, dict)
             else getattr(mf, "path", None))
            for mf in (getattr(target, "member_files", None) or [])
        }
        for mf in (getattr(pf, "member_files", None) or []):
            mfp = (mf.get("path") if isinstance(mf, dict)
                   else getattr(mf, "path", None))
            if mfp and mfp not in seen_mf:
                seen_mf.add(mfp)
                target.member_files.append(mf)
        folded_keys.add(key)
        if len(tele["folded"]) < 25:
            tele["folded"].append({"pf": key, "into": tkey, "anchor": aid})

    if folded_keys:
        product_features[:] = [
            pf for pf in product_features
            if str(getattr(pf, "name", "") or "") not in folded_keys
        ]
    return tele


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
