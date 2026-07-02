"""Nav-taxonomy matching — vendor-declared Layer 2, matched not synthesized.

The repo's nav/sidebar registry + route hierarchy DECLARE the product
taxonomy (see ``product_strings.build_nav_taxonomy``). This module
matches dev-feature clusters against that taxonomy so Layer 2 product
features carry the VENDOR'S OWN labels wherever possible; synthesis
(Stage 6.5 workspace/dep-anchor labels, Stage 8 analyst/haiku) remains
the fallback for unmatched clusters only (API-only, background, infra).

Matching channels, in trust order (a > b > c):

  (a) **route** — a feature's anchor files serve the entry's href
      (file-system route equality or nesting; reuses
      ``product_strings.route_path_for_file`` / ``normalize_href``).
  (b) **strings** — the entry's label appears verbatim among the
      feature's page-local product strings (``title`` / ``i18n``
      sources). Nav-registry strings are deliberately EXCLUDED here:
      the registry file lists every entry, so owning it would match a
      single feature to the whole taxonomy (circular evidence).
  (c) **tokens** — token containment between the entry label and the
      feature slug (either direction), with light singularization
      (``t-team-url`` ⊆/⊇ ``Teams``).

Cascade position: in-repo nav taxonomy ranks ABOVE the external
marketing taxonomy (the nav is the author's product framing, versioned
with the code) and BELOW the customer's explicit ``faultlines.yaml``
override. Wiring:

  * Stage 6.5 — new ``nav-taxonomy`` rule (confidence 0.85 >
    dep-anchor 0.75) names matched clusters with the vendor label →
    keyless / deterministic-only scans get vendor-named product
    features.
  * Stage 8 haiku — nav matches override the haiku marketing mapping.
  * Stage 8 analyst — nav matches are pinned post-emission
    (:func:`pin_nav_labels`); the analyst still refines descriptions
    and synthesizes for unmatched clusters.

Matched product features carry ``name_confidence="high"`` — the label
is structural evidence by construction. Deterministic, $0, NO README.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.product_strings import (
    NavTaxonomyEntry,
    ProductStringIndex,
    route_path_for_file,
)

if TYPE_CHECKING:
    from faultline.models.types import Feature

__all__ = [
    "NavMatch",
    "aggregate_product_feature",
    "match_features_to_taxonomy",
    "pin_nav_labels",
]


# ── Tokenization (mirrors the naming validator's spirit) ────────────────

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


def _singular(word: str) -> str:
    """Light singularisation — kept in sync with ``naming_validator._singular``.

    Never strips ``-us`` / ``-is`` / ``-ss`` (status, focus, analysis,
    address are already singular); only collapses ``-es`` to its stem when the
    stem is a sibilant (classes→class), so plain words keep their ``e``
    (cases→case, not cas).
    """
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ss", "us", "is", "ous", "ius")):
        return word
    if word.endswith(("sses", "shes", "ches", "xes", "zzes")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def _tokens(name: str) -> frozenset[str]:
    """Content tokens: camel/kebab/snake split, lowercase, singularized,
    1-char + pure-number tokens dropped."""
    spaced = _CAMEL_RE.sub(" ", name)
    out: set[str] = set()
    for raw in _SPLIT_RE.split(spaced):
        if not raw or raw.isdigit():
            continue
        t = _singular(raw.lower())
        if len(t) >= 2:
            out.add(t)
    return frozenset(out)


# ── Matching ────────────────────────────────────────────────────────────


# Channel priority — lower wins when one feature matches several
# entries through different channels. "route-prefix" = a coarse
# single-segment prefix hit, demoted below the content channels.
_VIA_PRIORITY = {"route": 0, "strings": 1, "tokens": 2, "route-prefix": 3}


@dataclass(frozen=True)
class NavMatch:
    """One dev feature's match against a taxonomy entry."""

    label: str          # vendor's label, verbatim
    href: str | None
    source_file: str    # where the vendor declared the entry
    via: str            # "route" | "strings" | "tokens"


def _anchor_paths(feature: "Feature") -> list[str]:
    """Stage 2.6 anchors when present, else every path (pre-closure)."""
    anchors = [
        mf.path for mf in (feature.member_files or []) if mf.role == "anchor"
    ]
    return anchors or list(feature.paths or [])


def _route_match(
    feature: "Feature", entries: list[NavTaxonomyEntry],
) -> tuple[NavTaxonomyEntry | None, bool]:
    """Channel (a): an anchor file serves the entry's href. The entry
    with the LONGEST (most specific) href wins.

    Returns ``(entry, specific)``. A match is SPECIFIC when an anchor
    route equals the href exactly, or the href has ≥2 path segments;
    a bare prefix hit on a single-segment href (``/api`` claiming
    every ``/api/**`` feature) is the WEAKEST evidence class — the
    caller demotes it below the strings/tokens channels."""
    routes = {
        r for r in (route_path_for_file(p) for p in _anchor_paths(feature))
        if r
    }
    if not routes:
        return None, False
    best: NavTaxonomyEntry | None = None
    for e in entries:
        if not e.href or e.href == "/":
            continue
        if any(r == e.href or r.startswith(e.href + "/") for r in routes):
            if best is None or len(e.href) > len(best.href or ""):
                best = e
    if best is None:
        return None, False
    href = best.href or ""
    specific = href in routes or href.count("/") >= 2
    return best, specific


def _strings_match(
    feature: "Feature",
    entries: list[NavTaxonomyEntry],
    index: ProductStringIndex | None,
) -> NavTaxonomyEntry | None:
    """Channel (b): the entry label appears verbatim among the feature's
    page-local product strings (title / i18n sources only)."""
    if index is None:
        return None
    texts: set[str] = set()
    for p in feature.paths or []:
        for row in index.strings_for_file(p):
            if row.source in ("title", "i18n"):
                texts.add(row.text.strip().lower())
    if not texts:
        return None
    for e in entries:  # taxonomy order = vendor order
        if e.label.strip().lower() in texts:
            return e
    return None


def _tokens_match(
    feature: "Feature", entries: list[NavTaxonomyEntry],
) -> NavTaxonomyEntry | None:
    """Channel (c): token containment between label and feature slug.
    Largest label-token overlap wins; ties resolve in taxonomy order."""
    ftoks = _tokens(feature.name)
    if not ftoks:
        return None
    best: NavTaxonomyEntry | None = None
    best_overlap = 0
    for e in entries:
        ltoks = _tokens(e.label)
        if not ltoks:
            continue
        if ltoks <= ftoks or ftoks <= ltoks:
            overlap = len(ltoks & ftoks)
            if overlap > best_overlap:
                best, best_overlap = e, overlap
    return best


def match_features_to_taxonomy(
    developer_features: list["Feature"],
    taxonomy: list[NavTaxonomyEntry],
    product_strings: ProductStringIndex | None = None,
) -> dict[str, NavMatch]:
    """Match each dev feature to at most ONE taxonomy entry.

    Returns ``{dev_feature_name: NavMatch}`` for matched features only.
    Channel precedence route > strings > tokens. Deterministic.
    """
    entries: list[NavTaxonomyEntry] = []
    for top in taxonomy:
        entries.extend(top.flatten())
    if not entries:
        return {}

    out: dict[str, NavMatch] = {}
    for f in developer_features:
        route_entry, route_specific = _route_match(f, entries)
        entry = route_entry if route_specific else None
        via = "route"
        if entry is None:
            entry = _strings_match(f, entries, product_strings)
            via = "strings"
        if entry is None:
            entry = _tokens_match(f, entries)
            via = "tokens"
        if entry is None and route_entry is not None:
            # Coarse single-segment prefix hit — weakest evidence,
            # used only when nothing more specific matched.
            entry, via = route_entry, "route-prefix"
        if entry is None:
            continue
        out[f.name] = NavMatch(
            label=entry.label,
            href=entry.href,
            source_file=entry.source_file,
            via=via,
        )
    return out


# ── Product-feature aggregation (shared emit shape) ─────────────────────


def aggregate_product_feature(
    name: str,
    display_name: str,
    description: str,
    contrib: list["Feature"],
) -> "Feature":
    """One Layer 2 ``Feature`` row aggregated from its contributing dev
    features — same merge semantics as the Stage 6.5 / Stage 8 emitters
    (path/author union, commit sums, averaged health/coverage)."""
    from faultline.models.types import Feature

    merged_paths: list[str] = []
    seen_paths: set[str] = set()
    for c in contrib:
        for p in c.paths:
            if p not in seen_paths:
                merged_paths.append(p)
                seen_paths.add(p)
    authors: list[str] = []
    seen_authors: set[str] = set()
    for c in contrib:
        for a in (c.authors or []):
            if a not in seen_authors:
                authors.append(a)
                seen_authors.add(a)
    total_commits = sum(c.total_commits for c in contrib)
    bug_fixes = sum(c.bug_fixes for c in contrib)
    cov_vals = [c.coverage_pct for c in contrib if c.coverage_pct is not None]
    return Feature(
        name=name,
        display_name=display_name,
        description=description,
        paths=merged_paths,
        authors=authors,
        total_commits=total_commits,
        bug_fixes=bug_fixes,
        bug_fix_ratio=(bug_fixes / total_commits) if total_commits else 0.0,
        last_modified=max(
            (c.last_modified for c in contrib),
            default=datetime.fromtimestamp(0, timezone.utc),  # deterministic: zero-evidence aggregate must not stamp scan wall-clock (2026-07-02)
        ),
        health_score=round(
            sum(c.health_score for c in contrib) / len(contrib), 2,
        ),
        flows=[],
        coverage_pct=(sum(cov_vals) / len(cov_vals)) if cov_vals else None,
        layer="product",
        name_confidence="high",
    )


# ── Stage 8 pinning ─────────────────────────────────────────────────────


def _slugify(label: str) -> str:
    return label.lower().replace(" ", "-").replace("/", "-")


def pin_nav_labels(
    product_features: list["Feature"],
    dev_map: dict[str, tuple[str, ...]],
    member_flows_map: dict[str, list[str]],
    nav_map: dict[str, str],
    developer_features: list["Feature"],
) -> tuple[set[str], dict[str, Any]]:
    """Pin vendor labels onto an LLM-emitted Layer 2 (Stage 8 analyst).

    For every nav label ``L`` with matched dev-feature set ``D``:

      * an emitted PF whose member set is a SUBSET of ``D`` is RENAMED
        to ``L`` (vendor label wins; the analyst's description is kept
        as the refinement) — largest such PF when several qualify;
      * else if a PF already carries ``L``'s slug it is pinned as-is;
      * else a new PF named ``L`` is created from ``D``'s dev features;
      * every dev in ``D`` gains membership in ``L``'s PF (bipartite —
        analyst memberships are preserved alongside).

    Mutates ``product_features`` / ``dev_map`` / ``member_flows_map``
    in place. Returns ``(pinned_slugs, telemetry)``; pinned PFs carry
    ``name_confidence="high"`` and are exempt from the anti-
    hallucination validator (the label IS structural evidence).
    """
    telemetry = {
        "nav_pfs_renamed": 0,
        "nav_pfs_created": 0,
        "nav_pfs_existing": 0,
    }
    pinned: set[str] = set()
    if not nav_map:
        return pinned, telemetry

    dev_by_name = {f.name: f for f in developer_features}

    # label → sorted dev names matched to it.
    devs_by_label: dict[str, list[str]] = {}
    for dev, label in sorted(nav_map.items()):
        if dev in dev_by_name:
            devs_by_label.setdefault(label, []).append(dev)

    # PF slug → member dev set (reverse of dev_map).
    members_of: dict[str, set[str]] = {}
    for dev, slugs in dev_map.items():
        for s in slugs:
            members_of.setdefault(s, set()).add(dev)

    pf_by_slug = {pf.name: pf for pf in product_features}
    used_slugs = set(pf_by_slug)

    def _rename(pf: "Feature", label: str) -> str:
        old_slug = pf.name
        new_slug = _slugify(label)
        if new_slug != old_slug and new_slug in used_slugs:
            i = 2
            while f"{new_slug}-{i}" in used_slugs:
                i += 1
            new_slug = f"{new_slug}-{i}"
        used_slugs.discard(old_slug)
        used_slugs.add(new_slug)
        pf.name = new_slug
        pf.display_name = label
        if old_slug != new_slug:
            for dev, slugs in dev_map.items():
                if old_slug in slugs:
                    dev_map[dev] = tuple(
                        new_slug if s == old_slug else s for s in slugs
                    )
            if old_slug in member_flows_map:
                member_flows_map[new_slug] = member_flows_map.pop(old_slug)
            members_of[new_slug] = members_of.pop(old_slug, set())
            pf_by_slug[new_slug] = pf_by_slug.pop(old_slug)
        return new_slug

    for label in sorted(devs_by_label):
        devs = devs_by_label[label]
        dev_set = set(devs)
        slug = _slugify(label)

        target_slug: str | None = None
        if slug in pf_by_slug:
            # Analyst already used the vendor's label — pin as-is.
            target_slug = slug
            telemetry["nav_pfs_existing"] += 1
        else:
            # Rename: largest emitted PF whose members ⊆ matched devs
            # (a finer- or equal-grain cluster of this vendor surface).
            candidates = sorted(
                (
                    (len(members), s)
                    for s, members in members_of.items()
                    if members and members <= dev_set and s not in pinned
                ),
                key=lambda t: (-t[0], t[1]),
            )
            if candidates:
                target_slug = _rename(pf_by_slug[candidates[0][1]], label)
                telemetry["nav_pfs_renamed"] += 1
            else:
                contrib = [dev_by_name[d] for d in devs]
                pf = aggregate_product_feature(
                    name=slug if slug not in used_slugs else f"{slug}-nav",
                    display_name=label,
                    description=(
                        f"Vendor-declared product surface ({label}) matched "
                        f"from the in-repo nav taxonomy; {len(contrib)} "
                        f"developer features."
                    ),
                    contrib=contrib,
                )
                used_slugs.add(pf.name)
                pf_by_slug[pf.name] = pf
                members_of[pf.name] = set()
                product_features.append(pf)
                target_slug = pf.name
                telemetry["nav_pfs_created"] += 1

        pf_obj = pf_by_slug[target_slug]
        pf_obj.name_confidence = "high"
        pinned.add(target_slug)
        # Ensure every matched dev belongs to the vendor-labeled PF.
        for dev in devs:
            slugs = dev_map.get(dev, ())
            if target_slug not in slugs:
                dev_map[dev] = (*slugs, target_slug)
                members_of.setdefault(target_slug, set()).add(dev)
        # Recompute the pinned PF's aggregate over its (possibly grown)
        # member set so paths/health reflect every matched dev.
        contrib_now = sorted(members_of.get(target_slug, set()) | dev_set)
        contrib_feats = [
            dev_by_name[d] for d in contrib_now if d in dev_by_name
        ]
        if contrib_feats:
            fresh = aggregate_product_feature(
                pf_obj.name, pf_obj.display_name or label,
                pf_obj.description or "", contrib_feats,
            )
            pf_obj.paths = fresh.paths
            pf_obj.authors = fresh.authors
            pf_obj.total_commits = fresh.total_commits
            pf_obj.bug_fixes = fresh.bug_fixes
            pf_obj.bug_fix_ratio = fresh.bug_fix_ratio
            pf_obj.last_modified = fresh.last_modified
            pf_obj.health_score = fresh.health_score
            pf_obj.coverage_pct = fresh.coverage_pct

    return pinned, telemetry
