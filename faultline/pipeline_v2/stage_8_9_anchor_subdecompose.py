"""Stage 8.9 — oversized-feature sub-decomposition (deterministic).

Why this stage exists
=====================

Upstream stages (8.6 lever-A scaffold de-own, 8.7 de-sink) deflate a
monorepo *workspace anchor*'s borrowed/shared files, but they do NOT
DECOMPOSE the genuine residual: one feature can still OWN a structural
blob — caddy's ``caddyhttp`` go-package group owns 118 files
(``modules/caddyhttp/{reverseproxy,encode,caddyauth,...}``); inbox-zero's
``inbox-zero-ai`` route+package group owns 1762 (``apps/web/**``);
formbricks' ``web`` workspace anchor owns 544 (``apps/web/**``). The
``eval/cold_eval`` ``owned_max_feature_share`` blob ceiling is exactly
these single-feature mega-owners.

This stage splits an OVERSIZED feature along the repository's OWN
directory tree into per-DOMAIN developer sub-features — surfacing the
real product capabilities the blob was hiding, lifting feature recall,
WITHOUT any LLM and WITHOUT the precision risk of attribution (each file
lands in exactly ONE domain bucket — there is no "which feature owns this
shared file?" contention).

What changed vs the shipped depth-1 version (2026-06-20 blob-audit fix)
======================================================================

The previous implementation was DEPTH-1: it only looked one level below a
known architectural-LAYER directory and gated on ``_is_workspace_anchor``.
An independent audit proved it fired ZERO domains on caddy / inbox-zero /
formbricks:

* **Depth.** caddy's domains live at depth-2
  (``modules/caddyhttp/{reverseproxy,...}``) — depth-1 collapsed them all
  to the single key ``modules/caddyhttp`` (< ``_MIN_DOMAINS``). The Next
  apps' domains live deeper still (``apps/web/app/(app)/<domain>`` and
  ``apps/web/modules/<domain>``) under a single-child ``apps/web`` chain.
* **Gate.** caddy ``caddyhttp`` is a ``[go-package]`` group and
  inbox-zero ``inbox-zero-ai`` is a ``[route]+[package]`` group — NEITHER
  is a workspace anchor, so the old gate skipped the very features that
  needed splitting.
* **Metric.** ``cold_eval.owned_max`` reads ``member_files`` first; the
  old stage pruned ``paths`` but NEVER ``member_files``, so even when it
  fired the metric did not move.

The rule (universal, scale-invariant, stack-agnostic)
=====================================================

1. **Oversized gate (not workspace-anchor).** A feature is decomposable
   iff it OWNS more files than
   ``max(2 * repo_median_owned_feature_size, ceil(0.15 * total_owned))``.
   Both prongs are RELATIVE to the repo's own grain / size — a fine-grained
   repo and a 600-feature monorepo both gate correctly
   (``rule-no-magic-tuning``). Works for Go (caddy), Rust, Python, Next.

2. **Layer-transparent domain detection (depth-recurse).** The DOMAIN of
   a file = the FIRST path segment, below the feature's longest-common
   directory prefix, that is NOT a pure architectural-LAYER / infra / test
   / dynamic-route segment. Layer dirs (``app`` ``modules`` ``services``
   ``src`` …) are TRANSPARENT — we recurse THROUGH them to the domain
   beneath (``app/(app)/mail/page.tsx`` → ``mail``;
   ``modules/ee/contacts/x.ts`` → ``contacts``;
   ``modules/caddyhttp/reverseproxy/x.go`` → ``reverseproxy``). This is the
   deep recursion the depth-1 version lacked; capped at
   :data:`_DEPTH_CAP` segments for safety.

3. **Naming guard (no junk buckets).** A sub-feature is named after its
   leaf DOMAIN directory segment (route-group parens ``(x)`` stripped,
   kebab-cased). A pure layer / infra / test / dynamic-route directory
   NEVER becomes a feature (it is transparent in step 2), so the stage
   cannot mint ``utils`` / ``components`` / ``__tests__`` / ``[id]`` /
   ``prisma`` junk features. Files that resolve to NO domain segment
   (top-level scaffold, ``layout.tsx``, ``components/ui/*``, tests) fall
   to the SHARED residual (step 5).

4. **Grain + container floors.** A domain is promoted only if it holds at
   least the repo's median owned-feature size (``floor``); a feature is
   only split if ≥ :data:`_MIN_DOMAINS` domains clear that floor (a lone
   domain is not a decomposition). Sub-floor domains fold to the residual.

5. **Thin SHARED residual (member_files-aware).** The source feature
   keeps every loose / sub-floor / non-domain file in ``member_files`` but
   reclassified to ``role="shared"`` (``primary=False``) — exactly the
   Stage 8.6 lever-A de-own contract. Those files STOP counting toward
   ``owned_max_feature_share`` (which credits a file only when ``primary``
   or ``role in {anchor, owner}``) but are NOT lost. The source's exclusive
   ``paths`` shrink to the (de-owned) residual; its path-keyed surfaces are
   pruned of moved files. Sub-features receive their domain's files as
   OWNED ``member_files`` (``role="anchor"``, ``primary=True``) AND
   ``paths`` — so the metric actually moves.

Safety / conservation
=====================

* **File conservation.** Every owned file lands in exactly one place — a
  sub-feature (owned) or the source residual (shared). Nothing dropped or
  duplicated.
* **Product paths byte-stable.** Sub-features inherit the source's
  ``product_feature_id``; the owning product feature's path UNION is
  unchanged — the product-layer + membership-by-product gates cannot
  regress by construction. Only the DEVELOPER feature set gains specificity.
* **Flow / UF immune.** This stage runs AFTER flow + user-flow synthesis
  (Stage 6.7) and AFTER the bipartite store (Stage 5.5). It never mutates
  ``flows`` / ``user_flows`` / ``feature_flow_edges`` / ``product_feature_id``.
  UF rollup is from ``flows[]``, not the feature partition (audit-confirmed:
  blob↔UF has no wall — late decomposition is UF-free). So an aggressive
  split is safe; the ONLY risk is dev-feature NAMING precision, which the
  naming guard (step 3) addresses by naming from real domain dirs.
* **Not re-entrant.** Sub-features carry a ``"sub-domain"`` description
  marker so de-sink / this stage never treat them as decomposable anchors,
  and their flows / N:M overlays stay on the source residual.

Sub-features are intentionally THIN on aggregate git metrics (commits /
bug-fix ratio reset; health inherited as an approximation) — their
``paths`` / owned ``member_files`` / distributed path-keyed surfaces are
exact; richer per-domain metrics are a follow-up. No LLM. No network.

Default ON; disable via ``FAULTLINE_STAGE_8_9_SUBDECOMPOSE=0``.
"""

from __future__ import annotations

import math
import os
import re
import statistics
import uuid as _uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.stage_8_7_anchor_desink import _FILE_KEYED_SURFACES

if TYPE_CHECKING:
    from faultline.models.types import Feature, MemberFile


# ── Structural vocabulary (corpus-free, scale-invariant) ─────────────────────
#
# Pure architectural-LAYER / infra / test directory segments. A segment in
# this set is NEVER a domain: step-2 recursion is TRANSPARENT through it (it
# descends to the domain beneath), and step-3 naming can therefore never mint
# a feature named after it. House-pattern vocabulary, the SAME spirit as
# ``eval/stacks/*.yaml`` and Stage 8.6 ``_DEOWN_SCAFFOLD_SEGMENTS`` (this is
# its superset: scaffold + layers + framework + test/build/infra). It contains
# NO path/folder name harvested from a corpus repo, NO counts, NO ratios
# (memory/rule-no-magic-tuning + memory/rule-no-repo-specific-paths).
_NON_DOMAIN_SEGMENTS: frozenset[str] = frozenset({
    # shared scaffold (Stage 8.6 lever-A subset)
    "lib", "libs", "util", "utils", "helper", "helpers", "hook", "hooks",
    "type", "types", "constant", "constants", "config", "configs",
    "style", "styles", "shared", "common",
    # architectural layers (containers, not domains)
    "src", "app", "apps", "pages", "page", "components", "component", "ui",
    "api", "server", "client", "core", "internal", "pkg", "cmd", "public",
    "static", "assets", "models", "model", "schemas", "schema", "views",
    "view", "controllers", "controller", "services", "service", "handlers",
    "handler", "routes", "route", "router", "routers", "store", "stores",
    "providers", "provider", "middleware", "middlewares",
    "modules", "module", "features", "feature", "domains", "domain",
    "packages", "package", "plugins", "plugin", "integrations",
    "integration", "screens", "screen", "resources", "resource", "agents",
    "agent",
    # i18n / asset leaf dirs
    "i18n", "intl", "locale", "locales", "css", "scss", "images", "img",
    "fonts", "icons",
    # generic monorepo workspace-container names (transparent so their
    # domain children surface, e.g. apps/web/<domain>)
    "web", "frontend", "backend", "studio", "dashboard", "admin",
    # test / build / generated / infra
    "test", "tests", "__tests__", "spec", "specs", "e2e", "fixtures",
    "mocks", "__mocks__", "node_modules", "dist", "build", "out",
    "coverage", "vendor", "prisma", "migrations", "generated", "gen",
    "docs", "doc",
})

# Dynamic-route / private segments are NEVER domains:
#   ``[id]`` ``[[...slug]]`` ``[...all]`` (Next/Remix/Nuxt route params)
#   ``_private`` (Nuxt/SvelteKit private dir; leading-underscore convention)
_DYNAMIC_SEGMENT_RE = re.compile(r"^\[.*\]$|^_")
_ROUTE_GROUP_RE = re.compile(r"^\((.*)\)$")  # Next route group ``(marketing)``

_MIN_DOMAINS = 2          # a feature splitting into <2 domains is not a split
_DEPTH_CAP = 6            # max segments to descend looking for a domain
_SUBDOMAIN_MARKER = "sub-domain"
_OVERSIZED_SHARE = 0.15   # a feature owning >15% of the repo is oversized…
_OVERSIZED_MEDIAN_MULT = 2  # …OR more than 2x the repo's median feature size


# ── Result / telemetry ───────────────────────────────────────────────────────


@dataclass
class SubdecomposeResult:
    enabled: bool = True
    features_total: int = 0          # developer features considered
    oversized_total: int = 0         # features that passed the oversized gate
    features_split: int = 0          # features that produced >=1 sub-feature
    subfeatures_created: int = 0
    paths_moved: int = 0             # owned files relocated to sub-features
    members_deowned: int = 0         # residual member files flipped to shared
    floor_by_feature: dict[str, int] = field(default_factory=dict)
    split_sample: list[dict[str, Any]] = field(default_factory=list)

    # Back-compat aliases (older telemetry consumers / tests expect these).
    @property
    def anchors_total(self) -> int:
        return self.oversized_total

    @property
    def anchors_split(self) -> int:
        return self.features_split

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "features_total": self.features_total,
            "oversized_total": self.oversized_total,
            "features_split": self.features_split,
            "subfeatures_created": self.subfeatures_created,
            "paths_moved": self.paths_moved,
            "members_deowned": self.members_deowned,
            "split_sample": list(self.split_sample[:20]),
        }


def _is_enabled() -> bool:
    """Default ON; disable via ``FAULTLINE_STAGE_8_9_SUBDECOMPOSE=0``."""
    return os.environ.get("FAULTLINE_STAGE_8_9_SUBDECOMPOSE", "1") != "0"


# ── Owned-file model (mirrors eval/cold_eval._owned_file_set) ────────────────


def _owned_paths(feature: "Feature") -> list[str]:
    """Files this feature OWNS — ``member_files`` with ``primary`` or
    ``role in {anchor, owner}``; degrades to ``paths`` when member_files
    carry no ownership metadata. This is byte-identical to the blob
    metric's ``_owned_file_set`` so the gate and the metric agree.
    """
    mf = list(getattr(feature, "member_files", None) or [])
    if mf:
        owned = [
            m.path for m in mf
            if isinstance(getattr(m, "path", None), str)
            and (getattr(m, "primary", False)
                 or getattr(m, "role", None) in ("anchor", "owner"))
        ]
        if owned:
            return owned
    return list(getattr(feature, "paths", None) or [])


# ── Directory-tree domain detection ──────────────────────────────────────────


def _strip_route_group(seg: str) -> str:
    m = _ROUTE_GROUP_RE.fullmatch(seg)
    return m.group(1) if m else seg


def _is_non_domain(seg: str) -> bool:
    """``True`` when *seg* is a pure layer / infra / test / route-param dir
    that must never become a feature (transparent in recursion)."""
    if _DYNAMIC_SEGMENT_RE.match(seg):
        return True
    s = _strip_route_group(seg).lower()
    return s in _NON_DOMAIN_SEGMENTS or s.startswith(".")


def _common_segments(paths: list[str]) -> int:
    """Count of leading path SEGMENTS shared by every path AND followed by
    a deeper segment somewhere (so the common prefix is a directory, not a
    whole file). ``['a/b/x','a/b/y']`` → 2; ``['a/x']`` → 0."""
    if not paths:
        return 0
    split = [p.split("/") for p in paths]
    n = 0
    for segs in zip(*split):
        if all(s == segs[0] for s in segs) and any(len(sp) > n + 1 for sp in split):
            n += 1
        else:
            break
    return n


def _domain_key(path: str, start: int) -> str | None:
    """The domain-key prefix of *path*: the path up to and INCLUDING the
    first segment at-or-after index *start* that is a DOMAIN (not a layer /
    infra / test / route-param dir) and is itself a directory (a deeper
    segment follows). Returns ``None`` when no such segment exists within
    :data:`_DEPTH_CAP` (the file is pure scaffold/infra → residual).

    ``modules/caddyhttp/reverseproxy/x.go`` (start=1) → ``modules/caddyhttp``
    ``apps/web/app/(app)/mail/page.tsx`` (start=0) → ``apps/web/app/(app)/mail``
    Returning the full prefix (not just the leaf) keeps two domains with the
    same leaf name under different layers distinct.
    """
    segs = path.split("/")
    i, depth = start, 0
    while i < len(segs) - 1 and depth < _DEPTH_CAP:
        if not _is_non_domain(segs[i]):
            return "/".join(segs[: i + 1])
        i += 1
        depth += 1
    return None


def _plan_split(
    owned: list[str], floor: int,
) -> tuple[dict[str, list[str]], list[str]]:
    """Partition *owned* files into ``{domain_key: [files]}`` + residual.

    Layer-transparent: each file is assigned to its first DOMAIN ancestor
    (:func:`_domain_key`). A domain is promoted only with ≥ ``floor`` files;
    a split happens only with ≥ :data:`_MIN_DOMAINS` promotable domains.
    Sub-floor / non-domain files fall to residual. File conservation holds:
    every input is in exactly one output bucket.
    """
    start = _common_segments(owned)
    raw: dict[str, list[str]] = defaultdict(list)
    residual: list[str] = []
    for p in owned:
        k = _domain_key(p, start)
        if k is None:
            residual.append(p)
        else:
            raw[k].append(p)

    promotable = {k: f for k, f in raw.items() if len(f) >= floor}
    if len(promotable) < _MIN_DOMAINS:
        return {}, list(owned)
    for k, files in raw.items():
        if k not in promotable:
            residual.extend(files)
    return promotable, residual


def _slug(domain_key: str, used: set[str]) -> str:
    """``modules/caddyhttp`` → ``caddyhttp``; ``apps/web/app/(app)/mail`` →
    ``mail`` — the leaf domain segment, route-group parens stripped,
    kebab-cased, de-duplicated against already-used feature names."""
    leaf = _strip_route_group(domain_key.rsplit("/", 1)[-1])
    base = re.sub(r"[_\s]+", "-", leaf).strip("-").lower() or "domain"
    name, i = base, 2
    while name in used:
        name = f"{base}-{i}"
        i += 1
    used.add(name)
    return name


# ── Sub-feature factory + residual de-own ────────────────────────────────────


def _split_surfaces(feature: "Feature", domain_files: set[str]) -> dict[str, list]:
    """Subset of each path-keyed surface whose file is in *domain_files*."""
    out: dict[str, list] = {}
    for attr, file_field in _FILE_KEYED_SURFACES:
        items = getattr(feature, attr, None)
        if not items:
            continue
        kept = [it for it in items if getattr(it, file_field, None) in domain_files]
        if kept:
            out[attr] = kept
    return out


def _make_subfeature(
    source: "Feature", domain_key: str, files: list[str], name: str,
) -> "Feature":
    """Mint a developer sub-feature for one domain of *source*.

    Takes the domain's exact ``paths`` AND owned ``member_files``
    (``role="anchor"``, ``primary=True`` — so ``cold_eval`` counts them as
    OWNED and the blob metric moves), inherits the source's
    ``product_feature_id`` (product path union conserved), and resets
    aggregate git metrics (thin by design)."""
    from faultline.models.types import MemberFile  # local: avoid import cycle

    fileset = set(files)
    surfaces = _split_surfaces(source, fileset)
    owned_members = [
        MemberFile(
            path=p, role="anchor", confidence=1.0, primary=True,
            evidence=f"{_SUBDOMAIN_MARKER} of '{source.name}'",
        )
        for p in sorted(files)
    ]
    sub = source.model_copy(deep=True, update={
        "name": name,
        "display_name": name,
        "paths": sorted(files),
        "member_files": owned_members,
        "description": (
            f"{_SUBDOMAIN_MARKER} '{domain_key}' of feature '{source.name}'"
        ),
        "uuid": _uuid.uuid4().hex,
        "split_from": getattr(source, "uuid", None),
        "previous_names": [],
        "merged_from": [],
        # thin aggregate metrics — exact paths, approximate stats
        "total_commits": 0,
        "bug_fixes": 0,
        "bug_fix_ratio": 0.0,
        # N:M overlays + flows stay on the source residual (not split here)
        "flows": [],
        "shared_participants": [],
        "shared_attributions": [],
        "symbol_attributions": [],
        "hotspot_files": [],
        "participants": [],
        "history": None,
    })
    for attr, kept in surfaces.items():
        setattr(sub, attr, kept)
    return sub


def _deown_residual(
    source: "Feature", moved: set[str], residual: set[str],
) -> int:
    """Reclassify the source feature so it OWNS none of the split set.

    * Moved files (now owned by sub-features) are removed from the source's
      ``member_files`` + path-keyed surfaces entirely.
    * Residual files are kept in ``member_files`` but flipped to
      ``role="shared"`` (``primary=False``) — the Stage 8.6 lever-A de-own
      contract: still a (shared) claim, no longer OWNED, so they stop
      counting toward ``owned_max_feature_share``.
    * The source's exclusive ``paths`` shrink to the de-owned residual.

    Returns the number of residual members flipped to shared.
    """
    members = list(getattr(source, "member_files", None) or [])
    deowned = 0
    if members:
        kept_members: list["MemberFile"] = []
        for m in members:
            p = getattr(m, "path", None)
            if p in moved:
                continue  # owned elsewhere now → drop from source ledger
            if p in residual and (
                getattr(m, "primary", False)
                or getattr(m, "role", None) in ("anchor", "owner")
            ):
                m.role = "shared"
                m.primary = False
                deowned += 1
            kept_members.append(m)
        source.member_files = kept_members

    # Exclusive paths = de-owned residual only (sub-features own the rest).
    source.paths = sorted(residual)
    # Prune path-keyed surfaces of MOVED files (residual line-level
    # provenance legitimately stays — it is still a shared member).
    for attr, file_field in _FILE_KEYED_SURFACES:
        items = getattr(source, attr, None)
        if not items:
            continue
        kept = [it for it in items if getattr(it, file_field, None) not in moved]
        if len(kept) != len(items):
            setattr(source, attr, kept)
    return deowned


# ── Orchestration ────────────────────────────────────────────────────────────


def subdecompose_oversized_features(
    features: list["Feature"],
) -> SubdecomposeResult:
    """Split OVERSIZED developer features into per-domain sub-features along
    the repo's own directory tree.

    Mutates ``features`` in place: a split source keeps its (de-owned,
    ``role="shared"``) residual, and the new sub-features are APPENDED.
    Returns telemetry.
    """
    result = SubdecomposeResult(enabled=_is_enabled())
    if not result.enabled:
        return result

    devs = [
        f for f in features
        if getattr(f, "layer", "developer") == "developer"
    ]
    result.features_total = len(devs)
    if not devs:
        return result

    # Scale-invariant grain floor + oversized cut, both RELATIVE to the
    # repo's own owned-file grain (rule-no-magic-tuning). ``floor`` = the
    # repo's median owned-feature size (sub-features must be peers of the
    # repo's grain). ``cut`` = oversized iff owning > max(2x median,
    # 15% of all owned files).
    owned_by_feature = {id(f): _owned_paths(f) for f in devs}
    sizes = [len(v) for v in owned_by_feature.values() if v]
    if not sizes:
        return result
    median = max(2, int(statistics.median(sizes)))
    total_owned = len({p for v in owned_by_feature.values() for p in v})
    floor = median
    cut = max(
        _OVERSIZED_MEDIAN_MULT * median,
        math.ceil(_OVERSIZED_SHARE * total_owned),
    )

    used_names = {f.name for f in features}
    new_features: list["Feature"] = []

    for source in devs:
        owned = owned_by_feature[id(source)]
        if len(owned) <= cut:
            continue
        result.oversized_total += 1
        domains, residual = _plan_split(owned, floor)
        if not domains:
            continue

        result.floor_by_feature[source.name] = floor
        # Zero-path protection — never empty the source. If every owned file
        # moved into a domain, keep the smallest domain on the source as its
        # residual so it stays a valid, non-ghost feature.
        if not residual:
            smallest = min(domains, key=lambda k: len(domains[k]))
            residual = domains.pop(smallest)
            if len(domains) < 1:
                continue

        moved_files: set[str] = set()
        for domain_key, files in domains.items():
            name = _slug(domain_key, used_names)
            new_features.append(_make_subfeature(source, domain_key, files, name))
            moved_files.update(files)

        residual_set = set(residual)
        result.members_deowned += _deown_residual(
            source, moved_files, residual_set,
        )
        result.features_split += 1
        result.subfeatures_created += len(domains)
        result.paths_moved += len(moved_files)
        if len(result.split_sample) < 20:
            result.split_sample.append({
                "feature": source.name,
                "domains": len(domains),
                "moved": len(moved_files),
                "residual": len(residual_set),
                "names": sorted({_strip_route_group(k.rsplit("/", 1)[-1])
                                 for k in domains})[:25],
            })

    features.extend(new_features)
    return result


# Back-compat alias — phase_layer2 + older callers import this name.
def subdecompose_workspace_anchors(
    features: list["Feature"],
) -> SubdecomposeResult:
    """Deprecated name retained for the pipeline wiring + existing imports.

    The stage no longer restricts itself to workspace anchors (the blob
    audit proved the real blobs are often go-package / route groups, not
    workspace anchors); it now decomposes ANY oversized feature. See
    :func:`subdecompose_oversized_features`.
    """
    return subdecompose_oversized_features(features)


__all__ = [
    "SubdecomposeResult",
    "subdecompose_oversized_features",
    "subdecompose_workspace_anchors",
]
