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
   directory prefix, that is NOT a TRANSPARENT non-domain segment. Two
   kinds of non-domain segment, with opposite recursion behaviour:

   * **Transparent** — layers (``app`` ``modules`` ``services`` ``src`` ``lib``
     …), version dirs (``v1`` ``v2``), dynamic routes (``[id]``), hidden
     (``.x``). We recurse THROUGH them to the domain beneath
     (``app/(app)/mail/page.tsx`` → ``mail``; ``modules/ee/contacts/x.ts``
     → ``contacts``; ``modules/caddyhttp/reverseproxy/x.go`` →
     ``reverseproxy``; ``api/v1/integrations/airtable/x.ts`` → ``airtable``).
   * **Terminal** — asset/build/tooling/test/generated containers (``public``
     ``scripts`` ``playwright`` ``dist`` ``__tests__`` …) AND UI
     COMPONENT/VIEW COLLECTION dirs (``components`` ``ui`` ``widgets``
     ``primitives`` ``layouts`` …). Recursion STOPS here and the whole
     subtree is residual: their children are organisational buckets
     (``public/favicon`` is an asset, ``scripts/docker`` is tooling,
     ``components/v2/Accordion`` is a UI primitive), NEVER product domains.

   This is the deep recursion the depth-1 version lacked; capped at
   :data:`_DEPTH_CAP` segments for safety.

3. **Precision guards against per-component over-shatter.** Three layered
   rules stop the recursion minting per-component / per-view junk
   (``Accordion`` / ``Button`` / ``Card`` / ``MfaSessionPage`` …) once it
   descends past a frontend layer:

   * **3a — component-collection dirs are TERMINAL** (in
     :data:`_TERMINAL_SEGMENTS`): ``components`` / ``ui`` / ``widgets`` /
     ``primitives`` / ``elements`` / ``fragments`` / ``partials`` /
     ``layouts``. These are NEVER file-system routing roots, so stopping
     here cannot suppress a real route domain — it only stops
     ``components/v2/Accordion`` from minting ``Accordion``. (``pages`` /
     ``views`` STAY transparent — they ARE routing roots in Next Pages
     Router / Nuxt / Astro / Vite — so route domains beneath them survive.)
   * **3b — PascalCase component leaves are not domains**
     (:func:`_is_component_name`): a first-domain segment that is a single
     mixed-case identifier (``MfaSessionPage`` under a transparent ``pages``
     root; any presentational dir 3a's vocabulary missed) falls to the
     residual. Kebab/lowercase domain dirs (``app-connection``,
     ``content-manager``, ``reverseproxy``) promote. This is the universal
     React/Vue component-naming convention, not a corpus name.
   * **3c — naming.** A sub-feature is named after its leaf DOMAIN segment
     (route-group parens ``(x)`` stripped, kebab-cased). Transparent
     layer/version/route-param dirs are invisible to naming; terminal +
     PascalCase dirs never reach naming. So the stage cannot mint
     ``utils`` / ``components`` / ``Accordion`` / ``__tests__`` / ``[id]`` /
     ``prisma`` / ``v1`` junk. Files that resolve to NO domain (top-level
     scaffold, ``layout.tsx``, ``public/**``, component primitives, tests)
     fall to the SHARED residual (step 5).

4. **Grain + container floors + anti-shatter cap.** A domain is promoted
   only if it holds at least the repo's median owned-feature size
   (``floor``); a feature is only split if ≥ :data:`_MIN_DOMAINS` domains
   clear that floor (a lone domain is not a decomposition). Sub-floor
   domains fold to the residual. **Guard 3d (anti-shatter cap):** a single
   feature may promote at most ``max(_MIN_DOMAINS, n_other_dev_features)``
   domains — one feature shattering into more pieces than the rest of the
   repo has features is pathological, so the largest domains survive and the
   thin tail coarsens to the residual. The cap is the repo's OWN grain (its
   other-feature count), scale-invariant, no magic constant. It DOES fire on
   the validated corpus (e.g. infisical's backend exposes ~140+ service
   domains, more than its developer-feature count, so the cap coarsens the
   thin tail there) and it never suppresses a legitimately large
   decomposition because the SURVIVING domains are always the largest ones;
   it also bounds the per-feature fan-out at every recursion level.

5. **Recursion to a fixed point.** A minted sub-feature is itself re-tested
   by the SAME oversized gate; if it still owns more than the repo's ``cut``
   AND still has ≥ :data:`_MIN_DOMAINS` decomposable child domains, it is
   decomposed again, repeating until no oversized-decomposable feature
   remains. The repo-grain thresholds (``floor`` / ``cut`` / ``max_domains``)
   are computed ONCE and held fixed for every level — they describe the repo,
   not the level — so the fixed point is scale-invariant. Termination is
   guaranteed by strict monotonic descent (each child owns a proper subset of
   its parent's owned files); :data:`_FIXED_POINT_ITER_CAP` is a defensive CPU
   bound only. Every precision guard above applies at every level (they live
   inside the per-feature split), so junk stays 0 at depth-2+ as well.

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

* **File conservation.** Every file the source had — its owned
  ``member_files`` AND any path-only ``paths`` entries that carried no owning
  member row — lands in exactly one place: a sub-feature (owned) or the source
  residual (shared). Path-only residual files are MATERIALISED as
  ``role="shared"`` member rows so overwriting ``source.paths`` cannot drop
  them (the jsonhero-web 9-file-drop bug). Nothing is dropped or duplicated;
  a stage-wide conservation assertion is covered by tests.
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
* **Re-entrancy / idempotence.** This stage recurses INTERNALLY by design
  (step 5), but it is IDEMPOTENT across independent invocations: a feature
  carrying this stage's sub-domain provenance at ENTRY (``split_from`` set +
  the ``"sub-domain"`` description marker) is recognised as prior output and is
  NOT re-decomposed as a fresh source, so ``stage(stage(features)) ==
  stage(features)``. The marker also keeps de-sink from treating sub-features
  as decomposable anchors, and their flows / N:M overlays stay on the source
  residual. (Within a SINGLE run, freshly-minted sub-features ARE recursed —
  they are tracked locally, not via the entry marker — which is the fixed-point
  behaviour, not re-entry.)

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
# A non-domain segment is NEVER a feature. There are TWO kinds, and the
# distinction is load-bearing for the depth-recursion (step 2):
#
#   * TRANSPARENT LAYERS (:data:`_LAYER_SEGMENTS`) — architectural containers
#     whose CHILDREN are real domains. Recursion descends THROUGH them to the
#     domain beneath: ``src/lib/ai`` → ``ai``; ``apps/web/modules/contacts``
#     → ``contacts``. The layer itself never names a feature.
#
#   * TERMINAL CONTAINERS (:data:`_TERMINAL_SEGMENTS`) — asset / build /
#     tooling / test / generated dirs whose ENTIRE SUBTREE is non-domain.
#     Recursion STOPS at them and the whole path falls to the SHARED residual
#     — their children are NOT product domains, they are organisational
#     buckets (``public/favicon`` is an asset, not a feature; ``scripts/docker``
#     is tooling, not a feature; ``playwright/api`` is a test tree, not a
#     feature). This is what stops the depth-recursion from minting
#     asset/tooling/test sub-features once it descends past a layer.
#
# House-pattern vocabulary, the SAME spirit as ``eval/stacks/*.yaml`` and
# Stage 8.6 ``_DEOWN_SCAFFOLD_SEGMENTS``. It contains NO path/folder name
# harvested from a corpus repo, NO counts, NO ratios — only universal
# asset/build/tooling/test/layer tokens (memory/rule-no-magic-tuning +
# memory/rule-no-repo-specific-paths).
_LAYER_SEGMENTS: frozenset[str] = frozenset({
    # shared scaffold (Stage 8.6 lever-A subset)
    "lib", "libs", "util", "utils", "helper", "helpers", "hook", "hooks",
    "type", "types", "constant", "constants", "config", "configs",
    "style", "styles", "shared", "common",
    # architectural layers (containers, not domains). NOTE: ``pages`` / ``page``
    # / ``views`` / ``view`` / ``screens`` / ``screen`` stay TRANSPARENT here on
    # purpose — they are legitimate FILE-SYSTEM ROUTING roots (Next Pages
    # Router, Nuxt, Astro, Vite/TanStack ``src/pages``, see
    # eval/stacks/filesystem-routing.yaml), so their children are real route
    # DOMAINS (``pages/dashboard`` → ``dashboard``). View-component junk that
    # also lives under them (``pages/MfaSessionPage``) is caught by the
    # PascalCase-component-leaf guard (:func:`_is_component_name`), not by
    # making the routing root terminal. ``components`` / ``ui`` / ``widgets`` …
    # are NEVER routing roots → they live in _TERMINAL_SEGMENTS below.
    "src", "app", "apps", "pages", "page",
    "api", "server", "client", "core", "internal", "pkg", "cmd",
    "models", "model", "schemas", "schema", "views",
    "view", "controllers", "controller", "services", "service", "handlers",
    "handler", "routes", "route", "router", "routers", "store", "stores",
    "providers", "provider", "middleware", "middlewares",
    "modules", "module", "features", "feature", "domains", "domain",
    "packages", "package", "plugins", "plugin", "integrations",
    "integration", "screens", "screen", "resources", "resource", "agents",
    "agent",
    # i18n layer dirs (descend to the locale's domain children if any)
    "i18n", "intl", "locale", "locales",
    # generic monorepo workspace-container names (transparent so their
    # domain children surface, e.g. apps/web/<domain>)
    "web", "frontend", "backend", "studio", "dashboard", "admin",
})

# Terminal containers — recursion STOPS here; the entire subtree is residual.
# These are asset / build / tooling / test / generated / infra dirs whose
# children are organisational buckets, never product domains. Universal
# vocabulary only — no repo-specific names (rule-no-repo-specific-paths).
_TERMINAL_SEGMENTS: frozenset[str] = frozenset({
    # UI component / view COLLECTION dirs — terminal, NOT transparent.
    # A dir named ``components`` / ``ui`` / ``widgets`` … is a presentational
    # collection: its children are individual components (``components/v2/
    # Accordion``, ``components/Button``), NEVER product domains. Recursion
    # MUST stop here, otherwise the depth-recurse descends through
    # ``components`` (and a ``v2`` version dir) and mints each UI primitive
    # (``Accordion`` / ``Button`` / ``Card`` / ``Checkbox`` / ``Alert``) as a
    # bogus "domain" — the per-COMPONENT over-shatter this guard fixes. These
    # tokens are NEVER file-system routing roots in any framework (unlike
    # ``pages`` / ``views``, which stay transparent above), so stopping here
    # cannot suppress a real route domain. Universal React/Vue/Svelte/Angular
    # vocabulary only — no repo-specific names (rule-no-repo-specific-paths).
    "components", "component", "ui", "elements", "element",
    "widgets", "widget", "primitives", "primitive",
    "fragments", "fragment", "partials", "partial", "layouts", "layout",
    # static / asset roots (children are images, fonts, media — not domains)
    "public", "static", "assets", "asset", "media",
    "images", "image", "img", "fonts", "font", "icons", "icon",
    "css", "scss", "sass", "styles-static",
    # build / tooling / generated output
    "scripts", "script", "bin", "tools", "tooling", "dist", "build",
    "out", "output", "target", "coverage", "vendor", "node_modules",
    "generated", "gen", "codegen", "__generated__",
    # test trees (children are spec/fixture sub-trees, not product domains)
    "test", "tests", "__tests__", "spec", "specs", "e2e", "fixtures",
    "fixture", "mocks", "__mocks__", "__snapshots__", "playwright",
    "cypress", "storybook", ".storybook", "stories",
    # docs / db-infra (children are guides / migration files, not domains)
    "docs", "doc", "documentation", "migrations", "migration", "prisma",
    "seed", "seeds", "drizzle",
})

# Union retained for back-compat (``_is_non_domain`` membership test).
_NON_DOMAIN_SEGMENTS: frozenset[str] = _LAYER_SEGMENTS | _TERMINAL_SEGMENTS

# Dynamic-route / private segments are NEVER domains (transparent):
#   ``[id]`` ``[[...slug]]`` ``[...all]`` (Next/Remix/Nuxt route params)
#   ``_private`` (Nuxt/SvelteKit private dir; leading-underscore convention)
_DYNAMIC_SEGMENT_RE = re.compile(r"^\[.*\]$|^_")
# Version segments are NEVER domains (transparent): the real domain lives
# BELOW the version (``api/v1/integrations/airtable`` → ``airtable``;
# ``api/v2/management/contacts`` → ``management``). Matches ``v1`` ``v2`` …
# ``v1beta`` ``v2alpha`` etc. — a leading ``v`` + digit. Scale-invariant,
# stack-agnostic (REST/gRPC/SDK versioning is universal).
_VERSION_SEGMENT_RE = re.compile(r"^v\d+[a-z0-9]*$", re.IGNORECASE)
_ROUTE_GROUP_RE = re.compile(r"^\((.*)\)$")  # Next route group ``(marketing)``
# A single PascalCase / camelCase COMPONENT identifier — ``Accordion``,
# ``Button``, ``MfaSessionPage``, ``AccessManagementPage``, ``DataTable``,
# ``aiEditor``. By ubiquitous JS/TS/React/Vue convention a component- or
# view-DIRECTORY is named in mixed case with NO separator; a product-domain or
# route directory is kebab-case or lowercase (``reverse-proxy``, ``app-connection``,
# ``content-manager``, ``mail``, ``reverseproxy``). Matches an identifier that
# contains at least one lower AND one upper letter and no ``-`` / ``_`` / ``.``.
# Scale-invariant + stack-agnostic: it is the *casing convention*, not a
# corpus-observed name (rule-no-magic-tuning / rule-no-repo-specific-paths).
# Non-JS domains (Go ``reverseproxy``, Rust ``net``, Python ``billing``) are
# conventionally lowercase, so this never suppresses a real non-frontend domain.
_COMPONENT_NAME_RE = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])[A-Za-z][A-Za-z0-9]*$")

_MIN_DOMAINS = 2          # a feature splitting into <2 domains is not a split
_DEPTH_CAP = 6            # max segments to descend looking for a domain
_SUBDOMAIN_MARKER = "sub-domain"
_OVERSIZED_SHARE = 0.15   # a feature owning >15% of the repo is oversized…
_OVERSIZED_MEDIAN_MULT = 2  # …OR more than 2x the repo's median feature size

# Defensive recursion-depth cap for the fixed-point loop (step 1). This is a
# SAFETY BOUND, not a tuned magic number: termination is already guaranteed by
# strict monotonic descent — every split gives each child a PROPER SUBSET of its
# parent's owned files (the residual stays on the parent and a split needs
# ≥_MIN_DOMAINS domains, so every child is strictly smaller than the parent), so
# the chain owned-size → strictly-smaller-owned-size is a decreasing sequence of
# non-negative integers and MUST reach a fixed point. The cap exists ONLY to
# bound CPU against a hypothetical future bug that broke the monotonicity
# invariant (e.g. a child that re-claimed its parent's files); it is generous
# (a real decomposition tree is at most a handful of levels: workspace → app →
# route-group → domain) and is logged in telemetry if it ever fires. Per
# memory/rule-no-magic-tuning: a number that bounds termination is a safety
# constant, not a corpus-fit threshold — it is independent of repo size/grain.
_FIXED_POINT_ITER_CAP = 8


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
    iterations: int = 0              # fixed-point recursion levels actually run
    depth_cap_hit: bool = False      # safety cap reached (should never happen)
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
            "iterations": self.iterations,
            "depth_cap_hit": self.depth_cap_hit,
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


def _is_transparent(seg: str) -> bool:
    """``True`` when *seg* is a TRANSPARENT non-domain segment — a layer,
    version, dynamic-route, or hidden dir we recurse THROUGH to find the
    domain beneath (it never names a feature, but its children might)."""
    if _DYNAMIC_SEGMENT_RE.match(seg):
        return True
    s = _strip_route_group(seg).lower()
    if s in _TERMINAL_SEGMENTS:
        return False  # terminal: handled by _is_terminal, NOT transparent
    return (
        s in _LAYER_SEGMENTS
        or bool(_VERSION_SEGMENT_RE.match(s))
        or s.startswith(".")
    )


def _is_terminal(seg: str) -> bool:
    """``True`` when *seg* is a TERMINAL container — an asset / build /
    tooling / test / generated / UI-component-collection dir whose ENTIRE
    subtree is non-domain. Recursion stops here; the path falls to the
    shared residual."""
    return _strip_route_group(seg).lower() in _TERMINAL_SEGMENTS


def _is_component_name(seg: str) -> bool:
    """``True`` when *seg* is a single COMPONENT / VIEW identifier rather than a
    product domain — a mixed-case identifier with no separator (``Accordion``,
    ``Button``, ``MfaSessionPage``, ``DataTable``, ``aiEditor``). Such a
    directory is one presentational component / page-view, never a product
    domain, so it must NOT mint a sub-feature even when it sits beneath a
    transparent routing root (``pages/MfaSessionPage``) that we cannot make
    terminal. Kebab-case / lowercase domain dirs (``reverse-proxy``,
    ``content-manager``, ``reverseproxy``, ``mail``) return ``False``. Route
    groups ``(x)`` are unwrapped first."""
    return bool(_COMPONENT_NAME_RE.fullmatch(_strip_route_group(seg)))


def _is_non_domain(seg: str) -> bool:
    """``True`` when *seg* must never name a feature — i.e. it is either a
    transparent layer/version/route-param OR a terminal asset/tooling/test
    container. (Back-compat helper; recursion uses the finer-grained
    :func:`_is_transparent` / :func:`_is_terminal` split.)"""
    return _is_terminal(seg) or _is_transparent(seg)


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


def _domain_key(path: str, start: int = 0) -> str | None:
    """The domain-key prefix of *path*: the path up to and INCLUDING the
    first segment at-or-after index *start* that is a DOMAIN (not a layer /
    version / route-param dir) and is itself a directory (a deeper segment
    follows). Returns ``None`` when no such segment exists (the file is pure
    scaffold/asset/tooling/test → SHARED residual).

    Recursion has two non-domain behaviours:

    * **Transparent** segments (layers ``src``/``app``/``modules``…, version
      dirs ``v1``/``v2``, dynamic-route ``[id]``, hidden ``.x``) are skipped
      — we descend THROUGH them to the domain beneath.
    * **Terminal** containers (asset/build/tooling/test/generated dirs —
      ``public``, ``scripts``, ``playwright``, ``dist``…) STOP the recursion
      and yield ``None``: their whole subtree is organisational, never a
      product domain. This is what prevents the depth-recursion from minting
      ``favicon`` / ``docker`` / ``playwright`` style sub-features.

    ``modules/caddyhttp/reverseproxy/x.go`` (start=1) → ``modules/caddyhttp``
    ``apps/web/app/(app)/mail/page.tsx`` (start=0) → ``apps/web/app/(app)/mail``
    ``apps/web/public/favicon/site.webmanifest``  → ``None`` (terminal)
    ``apps/web/app/api/v1/integrations/airtable/route.ts`` → ``…/airtable``
    Returning the full prefix (not just the leaf) keeps two domains with the
    same leaf name under different layers distinct.
    """
    segs = path.split("/")
    i, depth = start, 0
    while i < len(segs) - 1 and depth < _DEPTH_CAP:
        seg = segs[i]
        if _is_terminal(seg):
            return None  # asset/build/tooling/test/component subtree → residual
        if not _is_transparent(seg):
            # First non-layer segment. A mixed-case COMPONENT/VIEW identifier
            # (``MfaSessionPage`` under a transparent ``pages`` routing root,
            # any presentational dir Guard-1's vocabulary missed) is NOT a
            # product domain → residual. A kebab/lowercase domain promotes.
            if _is_component_name(seg):
                return None
            return "/".join(segs[: i + 1])  # first real domain segment
        i += 1
        depth += 1
    return None


def _plan_split(
    owned: list[str], floor: int, *, max_domains: int | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Partition *owned* files into ``{domain_key: [files]}`` + residual.

    Layer-transparent: each file is assigned to its first DOMAIN ancestor
    (:func:`_domain_key`). A domain is promoted only with ≥ ``floor`` files;
    a split happens only with ≥ :data:`_MIN_DOMAINS` promotable domains.
    Sub-floor / non-domain files fall to residual. File conservation holds:
    every input is in exactly one output bucket.

    *max_domains* is the defensive ANTI-SHATTER cap (Guard 3): when set and a
    feature would promote MORE than ``max_domains`` domains, only the
    ``max_domains`` LARGEST domains are kept; the smaller ones COARSEN back
    into the residual (de-owned/shared) rather than minting a long tail of
    thin sub-features. The cap is supplied by the caller and is RELATIVE to
    the repo's own grain (the count of its other developer features — see
    :func:`subdecompose_oversized_features`), never a magic constant
    (rule-no-magic-tuning). ``None`` disables the cap.
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

    # Guard 3 — anti-shatter coarsening. A single feature decomposing into
    # more domains than the rest of the repo has features is a shatter signal;
    # keep the largest ``max_domains`` (the substantive domains) and fold the
    # thin tail back. Deterministic tie-break by key so the output is stable.
    if max_domains is not None and len(promotable) > max(_MIN_DOMAINS, max_domains):
        ordered = sorted(
            promotable.items(), key=lambda kv: (-len(kv[1]), kv[0]),
        )
        promotable = dict(ordered[: max(_MIN_DOMAINS, max_domains)])

    # Everything in ``raw`` not finally promoted (sub-floor OR coarsened tail)
    # folds to the residual — exactly once, so file conservation holds.
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

    **File conservation (path-only residual).** The split universe is the
    source's OWNED file set (:func:`_owned_paths`). When the source's
    ``paths`` is a SUPERSET of its owned ``member_files`` — i.e. it carries
    *path-only* entries that have no owning ``member_file`` row (a real shape:
    jsonhero-web's anchor lists 9 files in ``paths`` with no role metadata) —
    those path-only files are part of neither ``moved`` nor any owned
    ``member_file``. The caller folds them into *residual* (they were never
    OWNED, so they coarsen to the shared residual, not a sub-feature). This
    function then MATERIALISES every residual file as a ``role="shared"``
    member row even when it had no prior ``member_file`` — so a path-only file
    is recorded as a (de-owned) shared claim rather than silently dropped when
    ``paths`` is overwritten below. Nothing the source previously had in
    ``paths`` ∪ owned ``member_files`` is lost.

    Returns the number of residual members flipped/created as shared.
    """
    from faultline.models.types import MemberFile  # local: avoid import cycle

    members = list(getattr(source, "member_files", None) or [])
    deowned = 0
    seen_paths: set[str] = set()
    kept_members: list["MemberFile"] = []
    for m in members:
        p = getattr(m, "path", None)
        if p in moved:
            continue  # owned elsewhere now → drop from source ledger
        if isinstance(p, str):
            seen_paths.add(p)
        if p in residual and (
            getattr(m, "primary", False)
            or getattr(m, "role", None) in ("anchor", "owner")
        ):
            m.role = "shared"
            m.primary = False
            deowned += 1
        kept_members.append(m)

    # Conservation: any residual file with NO surviving member row (a
    # path-only entry, or a member row that was somehow absent) is
    # materialised as a shared claim so it is NOT dropped when ``paths`` is
    # overwritten. Deterministic order for stable output.
    for p in sorted(residual):
        if p not in seen_paths:
            kept_members.append(
                MemberFile(
                    path=p, role="shared", confidence=1.0, primary=False,
                    evidence=f"{_SUBDOMAIN_MARKER} residual of '{source.name}'",
                )
            )
            deowned += 1
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


def _split_one_feature(
    source: "Feature",
    *,
    floor: int,
    cut: int,
    max_domains: int,
    used_names: set[str],
    result: SubdecomposeResult,
) -> list["Feature"] | None:
    """Decompose ONE oversized feature into per-domain sub-features.

    Returns the list of minted sub-features (≥ :data:`_MIN_DOMAINS`), or
    ``None`` when *source* does not split (not oversized, or no decomposable
    domain structure — i.e. a fixed point for this feature). Mutates *source*
    in place (de-owns its residual) and updates *result* telemetry.

    *floor* / *cut* / *max_domains* are the REPO-LEVEL grain thresholds,
    computed once from the whole repo and held FIXED for every recursion level
    (step 1). They describe the repo's grain, which does not change as we
    descend — so a minted sub-feature that still owns more than the repo's
    ``cut`` is judged oversized by the SAME standard as a top-level feature.
    This is what makes the fixed point scale-invariant: every level is measured
    against one stable repo-grain ruler, not a per-level drifting one.
    """
    owned = _owned_paths(source)
    if len(owned) <= cut:
        return None  # not oversized → fixed point for this feature
    result.oversized_total += 1

    # File conservation: the split universe is the OWNED set. When ``paths`` is
    # a SUPERSET of the owned member set (path-only entries with no owning
    # member_file row), those extra files are part of neither a domain nor the
    # owned-derived residual. Fold them into the residual so _deown_residual
    # materialises them as shared claims — nothing is dropped when ``paths`` is
    # overwritten (audit: jsonhero-web dropped 9 such files).
    path_only = [p for p in (getattr(source, "paths", None) or []) if p not in owned]

    domains, residual = _plan_split(owned, floor, max_domains=max_domains)
    if not domains:
        return None  # no decomposable domain structure → fixed point

    result.floor_by_feature[source.name] = floor
    # Zero-path protection — never empty the source. If every owned file moved
    # into a domain, keep the smallest domain on the source as its residual so
    # it stays a valid, non-ghost feature.
    if not residual:
        smallest = min(domains, key=lambda k: len(domains[k]))
        residual = domains.pop(smallest)
        if len(domains) < 1:
            return None

    minted: list["Feature"] = []
    moved_files: set[str] = set()
    for domain_key, files in domains.items():
        name = _slug(domain_key, used_names)
        sub = _make_subfeature(source, domain_key, files, name)
        minted.append(sub)
        moved_files.update(files)

    # path-only files (never owned) join the de-owned shared residual.
    residual_set = set(residual) | set(path_only)
    result.members_deowned += _deown_residual(source, moved_files, residual_set)
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
    return minted


def subdecompose_oversized_features(
    features: list["Feature"],
) -> SubdecomposeResult:
    """Split OVERSIZED developer features into per-domain sub-features along
    the repo's own directory tree — RECURSIVELY, to a fixed point.

    Mutates ``features`` in place: a split source keeps its (de-owned,
    ``role="shared"``) residual, and the new sub-features are APPENDED. A
    minted sub-feature that is ITSELF still oversized AND still has a
    decomposable domain structure is decomposed AGAIN, and so on until no
    oversized-decomposable feature remains (the fixed point). Returns
    telemetry.

    Recursion design + termination (step 1)
    ---------------------------------------
    The per-feature split (:func:`_split_one_feature`) is driven by a WORKLIST.
    The worklist is seeded with the repo's current developer features; whenever
    a feature splits, its freshly-minted sub-features are pushed back onto the
    worklist and re-evaluated by the SAME oversized gate. The repo-grain
    thresholds (``floor`` / ``cut`` / ``max_domains``) are computed ONCE and
    held fixed for the whole loop — they are a property of the repo, not of the
    recursion level — so the fixed point is defined against one stable ruler.

    Termination is GUARANTEED by strict monotonic descent: a split requires
    ≥ :data:`_MIN_DOMAINS` promotable domains and leaves a non-empty residual
    on the parent, so every child owns a PROPER SUBSET of its parent's owned
    files (strictly fewer). Owned-file count is a non-negative integer that
    strictly decreases along every parent→child chain, so the chain is finite
    and the worklist drains. :data:`_FIXED_POINT_ITER_CAP` is a defensive CPU
    bound only (logged via ``depth_cap_hit`` if ever hit), NOT the termination
    mechanism — see its definition. The precision guards (terminal containers,
    PascalCase-component leaves, grain floor, anti-shatter cap) apply at EVERY
    level because they live inside :func:`_split_one_feature` /
    :func:`_plan_split`, so junk cannot be minted at depth-2+ any more than at
    depth-1.

    Idempotence / re-entrancy
    -------------------------
    The stage recurses INTERNALLY by design, but a second INDEPENDENT
    invocation on already-decomposed output is a no-op: features carrying the
    sub-domain provenance marker (``split_from`` set + the ``sub-domain``
    description) at ENTRY are this stage's OWN prior output and are NOT
    re-decomposed as fresh sources. Within a single run, sub-features minted
    THIS run are still recursed (they are tracked locally, not by the entry
    marker). So ``stage(stage(features)) == stage(features)``.
    """
    result = SubdecomposeResult(enabled=_is_enabled())
    if not result.enabled:
        return result

    # Re-entrancy guard: a feature that ALREADY carries this stage's sub-domain
    # provenance at entry is prior output — count it toward the grain but never
    # re-decompose it as a fresh source (idempotence on re-run). Freshly minted
    # sub-features (this run) are recursed via the worklist, not this set.
    def _is_prior_subdomain(f: "Feature") -> bool:
        if getattr(f, "split_from", None):
            desc = (getattr(f, "description", None) or "").lower()
            return _SUBDOMAIN_MARKER in desc
        return False

    devs = [
        f for f in features
        if getattr(f, "layer", "developer") == "developer"
    ]
    result.features_total = len(devs)
    if not devs:
        return result

    # Scale-invariant grain floor + oversized cut, both RELATIVE to the repo's
    # own owned-file grain (rule-no-magic-tuning), computed ONCE over the full
    # current developer-feature set and held FIXED for every recursion level.
    # ``floor`` = the repo's median owned-feature size (sub-features must be
    # peers of the repo's grain). ``cut`` = oversized iff owning > max(2x
    # median, 15% of all owned files).
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
    # Guard 3 — defensive anti-shatter cap, scale-invariant. A single feature
    # decomposing into MORE domains than the repo has OTHER developer features
    # is pathological: it alone would more than double the feature set with a
    # long thin tail. Cap promoted domains at that count (the repo's own grain)
    # so a true shatter coarsens gracefully (largest domains survive, tail folds
    # to the shared residual). The cap IS exercised by the validated corpus —
    # e.g. infisical's backend holds ~140 real service domains under a 134-ish
    # feature repo, so the cap fires there and coarsens the thin tail — and it
    # never suppresses a legitimately large decomposition because the SURVIVING
    # domains are the largest ones. No corpus-tuned constant — the cap is the
    # repo's existing developer-feature count (rule-no-magic-tuning).
    max_domains = max(_MIN_DOMAINS, len(devs) - 1)

    used_names = {f.name for f in features}
    all_new: list["Feature"] = []
    # Seed the worklist with eligible (non-prior-output) developer features.
    worklist: list["Feature"] = [f for f in devs if not _is_prior_subdomain(f)]

    iteration = 0
    while worklist and iteration < _FIXED_POINT_ITER_CAP:
        iteration += 1
        next_round: list["Feature"] = []
        for source in worklist:
            minted = _split_one_feature(
                source,
                floor=floor, cut=cut, max_domains=max_domains,
                used_names=used_names, result=result,
            )
            if minted:
                all_new.extend(minted)
                # Re-evaluate the freshly minted sub-features next round: any
                # still-oversized + decomposable one splits again (fixed point).
                next_round.extend(minted)
        worklist = next_round

    result.iterations = iteration
    if worklist:
        # Cap reached with work still pending — should be impossible given the
        # strict-descent termination proof; surface it loudly for diagnosis
        # rather than silently leaving an oversized blob.
        result.depth_cap_hit = True

    features.extend(all_new)
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
