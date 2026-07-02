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

# ── Flat-route (Remix-style) virtual-path vocabulary ─────────────────────────
#
# Some file-system routers encode the WHOLE route hierarchy in a single
# DOT-SEPARATED NAME inside ONE ``routes/`` directory, instead of in nested
# sub-directories. This is the Remix / React-Router "flat-routes" convention
# (also produced by ``remix-flat-routes`` and the ``app/routes.ts`` flat config):
#
#   ``app/routes/_app.orgs.$organizationSlug.projects.$projectParam.alerts.new.ts``
#   ``app/routes/account.tokens/route.tsx``      (dot-name as a DIRECTORY)
#   ``app/routes/@.runs.$runParam.ts``
#
# The route hierarchy ``/orgs/:slug/projects/:param/alerts/new`` lives in the
# DOT-NAME, NOT in folders. The directory-tree decomposer (:func:`_domain_key`)
# therefore sees ONE undecomposable ``routes/`` dir and resolves every flat-route
# file to the SHARED residual — the workspace anchor stays a blob (trigger.dev's
# ``webapp`` owned 39% of the repo this way). The fix: when a segment sits
# DIRECTLY under a ``routes``/``route`` directory and its name is a genuine
# flat-route (≥2 route-like dot segments), parse the VIRTUAL path hierarchy out
# of the dot-name and key the file by its first real route DOMAIN.
#
# File-system routing is an EXPLICITLY ALLOWED grounding source (CLAUDE.md /
# memory/rule-no-readme — folder + filename routing convention, not prose). This
# vocabulary is the universal flat-routes convention, NO repo-specific path and
# NO magic number (memory/rule-no-repo-specific-paths + rule-no-magic-tuning).
#
# A directory whose lowercased name is one of these IS a flat-routes root; a
# segment directly beneath it is a flat-route candidate. ``routes``/``route`` are
# already TRANSPARENT layers above, so non-flat children (e.g. a plain
# ``routes/index.tsx``) keep their existing residual behaviour.
_FLAT_ROUTE_DIRS: frozenset[str] = frozenset({"routes", "route"})

# Flat-route SEGMENT markers — segments that form the virtual path but never
# NAME the domain. Universal Remix flat-routes convention:
#   * pathless layout    — a segment that is exactly ``_`` + name (``_app``,
#     ``_layout``, ``_auth``): groups children without adding a URL segment.
#   * dynamic param      — ``$`` prefix (``$organizationSlug``, ``$``): a URL
#     parameter, not a domain.
#   * index / escape     — ``@`` (escape / pathless index marker) and the
#     ``_index`` / ``index`` route-index leaf.
# These are SKIPPED for domain NAMING but still prove the name is a route. The
# DOMAIN is the first segment that is none of these — the first literal URL
# segment (``_app.orgs.$org…`` → ``orgs``; ``@.runs.$runParam`` → ``runs``;
# ``account.tokens`` → ``account``).
_FLAT_ROUTE_INDEX_MARKERS: frozenset[str] = frozenset({"@", "_index", "index"})

# ── POSITIVE Remix flat-routes signal (replaces the old denylist gate) ───────
#
# UNIVERSAL-SAFETY FIX (2026-06-23). The previous gate decided "is this a flat
# route?" by a DENYLIST (``_NON_ROUTE_DOT_SUFFIXES``): a dotted name FIRED unless
# one of its segments was a known type/role token. Wrong polarity — the denylist's
# completeness is bounded by imagination, so anything NOT enumerated misfired:
# ``src/routes/auth.routes.ts`` (Express) → ``[auth, routes]``, ``routes`` not in
# the denylist → parsed as a flat route → minted an ``auth`` domain on a NON-Remix
# repo; the entire NestJS/Angular file-suffix vocabulary
# (``.controller`` ``.service`` ``.dto`` ``.guard`` ``.resolver`` ``.entity``
# ``.gateway`` ``.repository`` ``.interceptor`` ``.pipe`` ``.filter`` …) misfired
# the same way. That CHANGED non-Remix decomposition (novu/NestJS at risk).
#
# The correct test is a POSITIVE structural signal: a directory is a Remix
# flat-routes dir IFF it CONTAINS at least one file carrying a genuine Remix
# flat-route MARKER. The markers below are Remix-SPECIFIC conventions that
# Express / NestJS / Angular never use in filenames, so they cannot be faked by a
# conventional ``*.routes.ts`` / ``*.controller.ts`` file. Presence of a marker
# CONFIRMS the dir; absence leaves the dir non-Remix and every file under it
# byte-identical to the pre-fix directory descent.
#
# File-system routing is an EXPLICITLY ALLOWED grounding source (CLAUDE.md /
# memory/rule-no-readme — folder + filename convention, not prose). These markers
# are the universal Remix flat-routes convention, NO repo-specific path and NO
# magic number (memory/rule-no-repo-specific-paths + rule-no-magic-tuning).
#
# A flat-route name's dot-segment is a MARKER segment when it is:
#   * pathless layout — ``_``-prefixed (``_app`` ``_layout`` ``_auth`` ``_index``)
#   * dynamic param   — ``$``-prefixed (``$slug`` ``$organizationSlug`` ``$``)
#   * escape / index  — exactly ``@`` (Remix v2 escape / pathless-index marker)
# Express ``auth.routes`` / NestJS ``users.controller`` have NONE of these → the
# dir is never confirmed Remix from such a file.
def _is_remix_marker_segment(part: str) -> bool:
    """``True`` when a single flat-route dot-segment is a Remix-SPECIFIC marker
    (pathless-layout ``_x`` / dynamic-param ``$x`` / escape ``@``). These are the
    decisive positive signals: Express/NestJS/Angular filenames never contain
    them, so a conventional ``auth.routes`` / ``users.controller`` segment list
    returns ``False`` for every part and the dir is NOT confirmed Remix."""
    return part.startswith("_") or part.startswith("$") or part == "@"


# The Remix v2 co-located route MODULE basename: a directory route like
# ``routes/account.tokens/route.tsx`` declares the route via a child file named
# exactly ``route`` (+ a JS/TS extension). Its PRESENCE strictly below a
# ``routes``/``route`` dir is itself a positive Remix signal (no Express/NestJS
# convention co-locates a bare ``route.tsx`` route module this way). Universal
# Remix v2 convention, no corpus name.
_ROUTE_MODULE_RE = re.compile(r"^route\.(?:t|j)sx?$", re.IGNORECASE)

# Known co-located NON-ROUTE dotted-file suffixes — retained as a SECONDARY
# guard. The POSITIVE marker above is the decisive gate (a dir is Remix only with
# a marker); this set additionally rejects ordinary type/role files
# (``Button.test.tsx`` / ``foo.server.ts`` / ``index.css`` / ``schema.d.ts``)
# from being parsed as routes EVEN inside a confirmed-Remix dir — those are
# co-located test/style/server modules, not route leaves. Universal JS/TS
# co-location vocabulary, no corpus name (rule-no-repo-specific-paths).
#
# DELIBERATELY this set is ONLY genuine FILE-ROLE / TYPE / build tokens — it does
# NOT include the layer/domain vocabulary (``api`` ``admin`` ``account`` …),
# because those words are legitimate URL segments in a confirmed flat route
# (``_app.api.runs`` is the route ``/api/runs``).
_NON_ROUTE_DOT_SUFFIXES: frozenset[str] = frozenset({
    # test / story
    "test", "spec", "stories", "story", "bench", "e2e",
    # framework file-roles (Remix/Next/SvelteKit/Nuxt co-located, NOT URL parts)
    "server", "client", "worker", "node", "browser", "edge",
    # module / build flavours
    "module", "min", "bundle", "esm", "cjs", "umd",
    # type / declaration
    "d", "types",
    # style files
    "css", "scss", "sass", "less", "styl",
    # data / config / doc dotted files
    "config", "json", "yaml", "yml", "toml", "md", "mdx",
    "mock", "mocks", "fixture", "snap",
})

# File-extension matcher (final ``.<ext>`` of a basename). Used to strip the
# extension before flat-route parsing so ``foo.bar.ts`` is exploded as
# ``foo``/``bar`` (route segments) not ``foo``/``bar``/``ts``.
_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]+$")

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
    # ── Husk cleanup (step 6) telemetry ───────────────────────────────────
    husks_total: int = 0             # split sources that ended 0-owned (husks)
    husks_dropped: int = 0           # empty-shell husks removed (all conserved)
    husks_kept_residual: int = 0     # husks kept as de-duped 0-owned shared bucket
    husk_members_deduped: int = 0    # double-count shared members removed from husks
    husk_files_conserved: int = 0    # distinct sole-held files kept (never dropped)

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
            "husks_total": self.husks_total,
            "husks_dropped": self.husks_dropped,
            "husks_kept_residual": self.husks_kept_residual,
            "husk_members_deduped": self.husk_members_deduped,
            "husk_files_conserved": self.husk_files_conserved,
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


def _is_clean_domain_part(part: str) -> bool:
    """``True`` when a single flat-route segment is a usable literal route
    DOMAIN — i.e. an ordinary URL word. ``False`` for the non-domain markers
    (layout ``_x`` / param ``$x`` / index ``@`` ``index``) AND for any segment
    carrying bracket escape/param syntax (``[id]`` dynamic, ``[.]`` escaped
    literal dot, ``[[...slug]]`` optional catch-all). Brackets mean the segment
    is a router special form, never a clean domain name (this also stops the
    dot-explode from minting junk like ``sitemap[`` out of an escaped
    ``sitemap[.]xml`` route)."""
    if not part:
        return False
    if part.startswith("$"):
        return False  # dynamic URL param
    if part.startswith("_"):
        return False  # pathless layout (``_app``) / ``_index``
    if part in _FLAT_ROUTE_INDEX_MARKERS:
        return False  # ``@`` escape / ``index`` leaf
    if "[" in part or "]" in part:
        return False  # bracket escape / param syntax — not a clean domain
    return True


def _flat_route_domain(name_no_ext: str) -> str | None:
    """The first real route DOMAIN of a flat-route *name* (extension already
    stripped), or ``None`` when the name is all layout/param/index/escape
    markers.

    Explodes the dot-name into its virtual route segments and returns the first
    that is a clean literal URL segment (:func:`_is_clean_domain_part`) —
    skipping pathless layouts (``_app``), dynamic params (``$slug``), index /
    escape markers (``@`` / ``_index`` / ``index``) and bracket forms (``[id]``,
    ``[.]``). ``_app.orgs.$organizationSlug.projects`` → ``orgs``;
    ``@.runs.$runParam`` → ``runs``; ``account.tokens`` → ``account``;
    ``sitemap[.]xml`` → ``None`` (escaped-dot single route file, no domain)."""
    for part in name_no_ext.split("."):
        if _is_clean_domain_part(part):
            return part
    return None


def _is_flat_route_name(name_no_ext: str) -> bool:
    """``True`` when *name_no_ext* (a basename with its extension already
    stripped, OR a directory name) is a parseable flat-route name — i.e. it
    encodes a route HIERARCHY in dot-separated segments, not a co-located
    type/role suffix.

    Two conditions, both required:

    1. **≥2 dot segments.** A single-dot name (``foo.ts`` → just ``foo`` after
       the ext strip → no dots left) is NOT a flat route; a hierarchy needs at
       least two segments (``account.tokens``, ``_app.orgs``).
    2. **No co-located non-route suffix.** If any segment AFTER the first is a
       known type/role token (``test`` ``server`` ``client`` ``css`` ``d`` …)
       the dots are a FILE-ROLE suffix, not URL segments — ``Button.test`` /
       ``foo.server`` / ``index.css`` / ``schema.d`` are NOT flat routes.

    NOTE — this predicate decides whether a name is PARSEABLE as a flat route
    once its ``routes`` dir is already CONFIRMED Remix (see
    :func:`_dir_is_confirmed_remix`). It is NOT the gate that decides whether a
    NON-Remix dir misfires: a conventional ``auth.routes`` / ``users.controller``
    would pass condition 1+2 here too (``routes`` / ``controller`` are not in the
    type/role denylist — that denylist completeness is exactly the anti-pattern
    we removed). Misfire safety comes from the POSITIVE confirmation gate, which
    only ever calls this for a file whose dir contains a real Remix marker. So a
    marker-less ``auth.routes.ts`` in an UNCONFIRMED Express ``routes/`` dir is
    never even reached.
    """
    if "." not in name_no_ext:
        return False
    parts = [p for p in name_no_ext.split(".") if p]
    if len(parts) < 2:
        return False
    # Reject co-located non-route dotted files: a known type/role token anywhere
    # after the first segment means the dots are a suffix, not a route path.
    return not any(p.lower() in _NON_ROUTE_DOT_SUFFIXES for p in parts[1:])


def _name_carries_remix_marker(name_no_ext: str) -> bool:
    """``True`` when a basename/dirname (extension already stripped) is a
    flat-route name that contains a Remix-SPECIFIC marker segment
    (:func:`_is_remix_marker_segment`).

    This is the POSITIVE per-file signal: ``_app.orgs.$slug`` (``_app`` + ``$slug``
    markers) → ``True``; ``@.runs.$runParam`` (``@`` + ``$runParam``) → ``True``;
    ``account.tokens`` (no marker) → ``False``; ``auth.routes`` (no marker) →
    ``False``; ``users.controller`` (no marker) → ``False``. Express/NestJS/Angular
    filenames NEVER contain these markers, so they cannot confirm a dir as Remix.
    """
    # Require a DOTTED, MULTI-SEGMENT name whose marker is a ``$``-param or
    # ``@``-escape — the UNAMBIGUOUS file-routing markers. ``$``- and
    # ``@``-prefixed filename segments appear in NO other framework
    # (Express/NestJS/Angular/Next/TS), so ``_app.orgs.$organizationSlug`` →
    # confirms, but the ``_``-prefix is DELIBERATELY EXCLUDED here: it is shared
    # with TS barrels (``_index``), private modules (``_private.service``), and
    # NestJS base classes (``_base.controller``), so a ``_``-segment alone — even
    # inside a dotted name — re-opened the Express/NestJS misfire the re-audit
    # caught (``_private.service`` is dotted + ``_``-marker, yet pure NestJS).
    # A Remix dir confirms via ANY one of its real dynamic routes (``$``-param),
    # which every non-trivial Remix app has; its marker-less static siblings
    # (``account.tokens``) then parse because the DIR is proven.
    parts = [p for p in name_no_ext.split(".") if p]
    if len(parts) < 2:
        return False
    return any(p.startswith("$") or p.startswith("@") for p in parts)


def _path_confirms_remix_routes_dir(segs: list[str]) -> set[str]:
    """The set of ancestor ``routes``/``route`` directory prefixes that *segs*
    (a single split path) PROVES are genuine Remix flat-routes dirs.

    A path proves a ``routes`` dir at index ``d`` is Remix when, strictly below
    it, there is positive evidence:

      * the IMMEDIATE child ``segs[d+1]`` (a flat-route FILE basename
        ext-stripped, or a dot-name DIRECTORY) carries a Remix marker segment
        (``_app`` / ``$slug`` / ``@``), OR
      * a Remix v2 co-located route MODULE basename ``route.{ts,tsx,js,jsx}``
        appears anywhere strictly below the ``routes`` dir (the
        ``routes/account.tokens/route.tsx`` directory-route convention).

    Express ``src/routes/auth.routes.ts`` (child ``auth.routes`` has no marker,
    basename is not ``route.*``) → ``{}``: the dir is NOT confirmed, so the
    flat-route branch never fires and the file is byte-identical to the legacy
    directory descent. NestJS ``modules/auth/routes/auth.routes.ts`` → ``{}``
    for the same reason (its ``routes`` dir holds only marker-less ``*.routes`` /
    ``*.controller`` / ``*.service`` files).

    Returns a set (a path may sit under several nested ``routes`` dirs, e.g. a
    sub-package's own ``routes``) so a self-confirm check can ask about a
    SPECIFIC dir, not just the shallowest. Universal Remix convention, no
    repo-specific path, no magic number.
    """
    last = len(segs) - 1
    confirmed: set[str] = set()
    for d, seg in enumerate(segs):
        if seg.lower() not in _FLAT_ROUTE_DIRS or d >= last:
            continue
        prefix = "/".join(segs[: d + 1])
        # (a) immediate child carries a marker (file basename or dot-name dir).
        child = segs[d + 1]
        child_no_ext = _FILE_EXT_RE.sub("", child) if (d + 1) == last else child
        if _name_carries_remix_marker(child_no_ext):
            confirmed.add(prefix)
        # NOTE: the Remix-v2 co-located ``route.{ts,tsx,js,jsx}`` confirmation was
        # REMOVED. ``route.ts`` is *also* Next.js App Router's handler filename and
        # a common Express/Hono/Fastify per-resource router name, so it
        # false-confirmed non-Remix dirs; and the old any-ancestor match
        # (``last > d``) retroactively confirmed shallow ``routes`` dirs from a
        # deep ``route.ts``. Remix-v2 FOLDER routes (``routes/account/route.tsx``)
        # are directory-based and already decomposed by the legacy directory
        # descent — only FLAT (dot-name) routes need this fast-path, and those are
        # caught by the dotted-marker child check above.
    return confirmed


def _confirmed_remix_routes_dirs(owned: list[str]) -> frozenset[str]:
    """Pre-pass over a feature's owned files → the set of confirmed Remix
    flat-routes directory prefixes (each a ``…/routes`` path that contains ≥1
    file carrying a genuine Remix marker, per
    :func:`_path_confirms_remix_routes_dir`).

    This is the heart of the universal-safety fix. Once a dir is in this set,
    EVERY flat-route name under it parses (including marker-LESS siblings like
    ``account.tokens.tsx`` — the dir is proven Remix by some other ``_app`` /
    ``$`` / ``@`` / ``route.tsx`` file). A NON-Remix Express/NestJS ``routes/``
    dir contributes nothing to this set (no marker file), so the flat-route
    branch is never consulted for any file under it and the decomposition is
    byte-identical to the pre-fix behaviour.
    """
    dirs: set[str] = set()
    for p in owned:
        dirs |= _path_confirms_remix_routes_dir(p.split("/"))
    return frozenset(dirs)


def _dir_is_confirmed_remix(
    routes_dir_prefix: str, segs: list[str], confirmed: frozenset[str] | None,
) -> bool:
    """Decide whether the ``routes`` dir at *routes_dir_prefix* is a confirmed
    Remix flat-routes dir for the purpose of keying *segs*.

    * When *confirmed* is supplied (the real pipeline path — a pre-pass over the
      whole feature's owned files built the set), membership is authoritative:
      a marker-less sibling parses iff its dir was confirmed by some OTHER file.
    * When *confirmed* is ``None`` (a standalone / unit-test ``_domain_key``
      call with no feature context), the path must SELF-confirm: this single
      path alone must carry the Remix marker for this dir. This makes a lone
      ``_app.orgs.$slug.ts`` resolve while a lone marker-less ``account.tokens.ts``
      does not (it is genuinely ambiguous with Express ``auth.routes.ts`` in
      isolation — only a sibling marker or an explicit confirmed-set can
      disambiguate it). Either way a NON-Remix path never confirms.
    """
    if confirmed is not None:
        return routes_dir_prefix in confirmed
    return routes_dir_prefix in _path_confirms_remix_routes_dir(segs)


def _flat_route_key(segs: list[str], i: int, *, is_last: bool) -> str | None | bool:
    """Flat-route resolution for segment ``segs[i]`` when it sits directly under
    a ``routes``/``route`` directory.

    Returns:
      * a domain-key string — the virtual prefix ``…/routes`` + the parsed
        route domain (``…/routes/orgs``) — when the segment is a flat-route with
        a real domain;
      * ``None`` — when the segment IS a flat-route but resolves to no domain
        (pure layout/param/index, e.g. ``_app.tsx`` or ``@._index.ts``): the
        file is a layout/index shell → shared residual;
      * ``False`` — sentinel meaning "NOT a flat-route segment"; the caller
        falls through to the ordinary directory-tree behaviour (so a plain
        ``routes/index.tsx`` or ``routes/loaders/`` keeps its existing handling).

    The virtual key reuses the leaf-naming path: ``_slug`` of ``…/routes/orgs``
    yields ``orgs``. Two flat-routes with the same leaf domain under different
    ``routes`` roots stay distinct because the full prefix is retained.
    """
    seg = segs[i]
    name_no_ext = _FILE_EXT_RE.sub("", seg) if is_last else seg
    if not _is_flat_route_name(name_no_ext):
        return False  # ordinary file/dir under routes/ → existing behaviour
    domain = _flat_route_domain(name_no_ext)
    if domain is None:
        return None  # layout/index-only route shell → residual
    if _is_component_name(domain):
        return None  # PascalCase view leaf → residual (same guard as dirs)
    return "/".join(segs[:i]) + "/" + domain


def _flat_route_scan(
    segs: list[str], start: int, confirmed: frozenset[str] | None = None,
) -> str | None | bool:
    """Detect a flat-route hierarchy ANYWHERE at-or-after *start* and key the
    file by its virtual route domain, INDEPENDENT of where the directory-tree
    common-prefix landed — but ONLY inside a CONFIRMED Remix flat-routes dir.

    Why independent of ``start``: a CONFIRMED Remix ``routes``/``route`` directory
    is an UNAMBIGUOUS file-system-routing signal. The ordinary descent
    (:func:`_domain_key`) keys off the all-owned-files common prefix (``start``),
    which a single stray attributed file (e.g. an import-closure file outside the
    workspace package) can drag shallower — so the descent may resolve the
    WORKSPACE-PACKAGE dir (``apps/webapp``) as a spurious single "domain" and
    return BEFORE ever reaching ``routes/``. A flat-routes directory does not
    depend on that prefix to be meaningful, so we locate it directly.

    UNIVERSAL-SAFETY GATE (2026-06-23): a ``routes``/``route`` dir is only treated
    as flat-routes when it is CONFIRMED Remix by a POSITIVE marker
    (:func:`_dir_is_confirmed_remix`) — a ``_app`` / ``$slug`` / ``@`` /
    ``route.tsx`` file under it. An Express ``src/routes/`` or NestJS
    ``modules/auth/routes/`` dir holds only marker-less ``*.routes`` /
    ``*.controller`` / ``*.service`` files, so it is NEVER confirmed, the scan
    skips it, and the file falls through to the byte-identical legacy descent.

    Returns a domain-key string (flat-route domain found in a confirmed dir),
    ``None`` (a confirmed flat-routes dir was found but THIS file is a
    layout/index shell with no domain → residual), or ``False`` (no CONFIRMED
    flat-routes structure on this path → the caller runs the ordinary directory
    descent unchanged). Non-Remix paths therefore behave EXACTLY as before
    (byte-identical): ``False`` short-circuits to the legacy walk.
    """
    last = len(segs) - 1
    for j in range(max(start, 1), len(segs)):
        if segs[j - 1].lower() not in _FLAT_ROUTE_DIRS:
            continue
        # POSITIVE gate: only key off this routes dir if it is CONFIRMED Remix.
        routes_dir_prefix = "/".join(segs[:j])
        if not _dir_is_confirmed_remix(routes_dir_prefix, segs, confirmed):
            continue  # non-Remix routes dir → legacy descent (byte-identical)
        # ``segs[j]`` sits directly under a CONFIRMED routes dir — parse it.
        fr = _flat_route_key(segs, j, is_last=(j == last))
        if fr is not False:
            return fr  # str (domain) or None (layout/index shell → residual)
        # Confirmed dir but this particular name is not parseable as a route
        # (e.g. a co-located ``styles.css`` sibling) → keep looking deeper, else
        # fall through to the legacy descent.
    return False


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


def _domain_key(
    path: str, start: int = 0, confirmed: frozenset[str] | None = None,
) -> str | None:
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

    **Flat-route (Remix) virtual hierarchy.** When a segment sits directly under
    a ``routes``/``route`` directory and its name encodes a flat-route hierarchy
    in dot-separated segments (:func:`_is_flat_route_name`), the route DOMAIN is
    parsed from the DOT-NAME — not from sub-directories, which a flat-routes
    repo does not have. This applies to BOTH a dot-name FILE (the basename, which
    the directory loop below otherwise never inspects) and a dot-name DIRECTORY:
    ``app/routes/_app.orgs.$organizationSlug.projects.ts`` → ``app/routes/orgs``;
    ``app/routes/account.tokens/route.tsx`` → ``app/routes/account``;
    ``app/routes/@.runs.$runParam.ts`` → ``app/routes/runs``. Files that are
    layout/index-only route shells (``_app.tsx``, ``@._index.ts``) resolve to no
    domain → shared residual. NON-flat dotted files under such a dir
    (``foo.server.ts``, ``Button.test.tsx``) are NOT flat routes and keep their
    ordinary behaviour. Outside a ``routes`` dir the flat-route branch is never
    consulted, so every non-flat-route path is byte-identical to before.
    """
    segs = path.split("/")
    # Flat-route (Remix) fast path — start-INDEPENDENT, CONFIRMED-Remix-only. A
    # ``routes``/``route`` dir that a POSITIVE Remix marker has CONFIRMED is an
    # unambiguous file-system-routing signal that does not depend on the
    # (outlier-fragile) common-prefix ``start``; resolve it directly so the
    # workspace-package dir is never mistaken for the domain. ``confirmed`` is the
    # pre-pass set of Remix-routes dirs (None ⇒ self-confirm from this path alone,
    # for standalone calls). ``False`` = no CONFIRMED flat-route structure → the
    # ordinary directory walk below runs byte-identically to the legacy behaviour
    # for every non-Remix path (Express/NestJS ``routes/`` never confirm).
    fr = _flat_route_scan(segs, start, confirmed)
    if fr is not False:
        return fr  # str (route domain) or None (layout/index shell → residual)
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
    # Universal-safety PRE-PASS: identify which ``routes``/``route`` dirs in THIS
    # feature's owned files are CONFIRMED Remix flat-routes dirs (contain ≥1 file
    # with a real ``_app`` / ``$slug`` / ``@`` / ``route.tsx`` marker). The
    # flat-route branch fires ONLY inside these dirs, so a marker-less sibling
    # (``account.tokens.tsx``) parses in a proven-Remix dir while an Express/NestJS
    # ``routes/`` dir (no marker file) is left byte-identical to the legacy
    # directory descent.
    confirmed = _confirmed_remix_routes_dirs(owned)
    raw: dict[str, list[str]] = defaultdict(list)
    residual: list[str] = []
    for p in owned:
        k = _domain_key(p, start, confirmed)
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
        # Content-derived (parent identity + domain) — uuid4 churned every
        # run, breaking byte-identical re-scans (determinism arc 2026-07-02).
        "uuid": __import__("hashlib").sha256(
            f"subfeat-v1|{getattr(source, 'uuid', '') or source.name}|"
            f"{domain_key}|{name}".encode("utf-8")).hexdigest()[:32],
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


# ── Husk cleanup (step 6) ────────────────────────────────────────────────────


def _all_member_paths(feature: "Feature") -> set[str]:
    """Every file the feature CLAIMS (any role) — its full ``member_files``
    path set. This is what ``cold_eval._file_set`` reads for the all-files
    blob denominator + ``max_feature_share`` candidacy."""
    return {
        m.path for m in (getattr(feature, "member_files", None) or [])
        if isinstance(getattr(m, "path", None), str)
    }


def _owns_zero_files(feature: "Feature") -> bool:
    """``True`` when the feature OWNS no files, by the SAME definition the blob
    gate (``cold_eval._owned_file_set``) uses — i.e. its ``member_files`` carry
    role/primary metadata and NONE is ``primary`` or ``role in {anchor,
    owner}``.

    This deliberately does NOT use :func:`_owned_paths`, which falls back to the
    feature's flat ``paths`` list when no owned member row exists. A de-owned
    Stage-8.9 residual keeps its residual files in BOTH ``member_files``
    (``role="shared"``) AND ``paths`` (``_deown_residual`` sets
    ``source.paths = residual``), so ``_owned_paths`` would report it as owning
    its residual and the husk would be invisible. The gate ignores that
    fallback whenever role metadata is present (a fully-shared member set owns
    0), so the husk test must match the gate exactly."""
    mf = list(getattr(feature, "member_files", None) or [])
    if not mf:
        return False  # no members at all → not a husk (handled separately)
    has_role_meta = any(
        getattr(m, "primary", None) is not None or getattr(m, "role", None) is not None
        for m in mf
    )
    if not has_role_meta:
        return False  # legacy rows with no metadata → treated as owned by the gate
    return not any(
        getattr(m, "primary", False) or getattr(m, "role", None) in ("anchor", "owner")
        for m in mf
    )


def _cleanup_husks(
    features: list["Feature"],
    split_sources: list["Feature"],
    *,
    cut: int,
    result: SubdecomposeResult,
) -> None:
    """De-degenerate the residual ``Feature`` objects this stage produced.

    A *husk* is a split source that ended up OWNING 0 files: every domain it
    contained moved to a sub-feature, leaving only a residual of loose /
    sub-floor / scaffold files which :func:`_deown_residual` reclassified to
    ``role="shared"``. Such a feature OWNS nothing yet LISTS (as shared)
    hundreds of files — a degenerate "husk" that should not surface as a
    developer feature (``flow-feature-concept``: a 0-owned developer feature is
    not a feature) and that double-counts files already owned by its
    sub-features when a naive all-files share is computed (inbox-zero
    ``inbox-zero-ai``: 1701 members, ALL ``role="shared"``, owns 0).

    This pass runs ONCE after the fixed-point loop, ONLY over the features this
    stage de-owned (``split_sources``) — pre-existing 0-owned features from
    other stages are never touched (``rule-stage-8-fixes-must-be-additive``).
    It is **strictly subtractive on each husk**: it never promotes a residual
    file to OWNED (preserving the operator-audited "a fully de-owned source
    contributes 0 owned files" invariant — memory/project-blob-decomposer-
    shipped-2026-06-21) and never moves a file onto another feature. For each
    husk it applies, in order:

    1. **De-dup (lossless).** Remove every husk member whose path is CLAIMED
       (any role) by ≥1 OTHER feature in the scan. Those files are conserved on
       that other feature; on the husk they are pure double-counts and the
       direct cause of its inflated member list. This alone roughly halves the
       fake feature (inbox-zero ``inbox-zero-ai`` 1697→866 members; dub ``web``
       2037→813) and CANNOT change the blob metric: the union of attributed
       files is unchanged (the removed files stay on the other feature) and the
       husk stays 0-owned (still excluded from the owned/all-files
       ``max_feature_share`` candidacy by ``cold_eval``). De-duped files are
       pruned from ``paths`` and the path-keyed surfaces too.

    2. **Drop empty shell.** If EVERY member was conserved elsewhere (the
       sole-held set is empty), the husk holds nothing unique → REMOVE it from
       ``features``. Conservation holds (every file is on another feature).
       ``n_features`` drops by one. Flows / user_flows / feature_flow_edges are
       untouched (the dropped container's flows already live on the wider flow
       store, keyed independently of the feature partition — this stage runs
       after flow synthesis and never reads ``flows``).

    3. **Keep as de-duped 0-owned shared residual** (irreducible case). A husk
       whose sole-held remainder is non-empty (inbox-zero ``inbox-zero-ai``
       after de-dup = a single app's ``utils`` / ``components`` / ``prisma`` /
       config / test spread that NO sub-domain and NO other feature owns) is the
       SOLE attribution holder for those files. It cannot be dropped (the files
       would be lost — conservation is sacred), cannot OWN them (that would
       re-blob ``owned_max`` and break the audited invariant), and must not
       redistribute them onto countable features (that would REGRESS the
       all-files gate — measured: 0.063 → 0.11-0.24). So it is kept verbatim as
       the de-duped 0-owned shared bucket. The blob metric stays honest via the
       (operator-approved 2026-06-21) ``cold_eval`` husk-skip with which this
       engine pass PAIRS; here we have removed the double-count inflation so the
       product surface shows the minimal honest residual instead of the inflated
       1701-file phantom.

    Conservation invariant (asserted by tests): the set of files attributed to
    SOME feature across the whole ``features`` list is IDENTICAL before and
    after this pass. ``owned_max`` is unchanged (no residual file is ever
    promoted to owned). Flows / user_flows / feature_flow_edges are never read
    or mutated.

    (``cut`` is accepted for symmetry with the rest of the stage and possible
    future shape-gated behaviour; this subtractive cleanup does not branch on
    it.)
    """
    if not split_sources:
        return

    split_ids = {id(f) for f in split_sources}
    # Husks = split sources that ended 0-owned (by the gate's owned definition,
    # NOT _owned_paths — see _owns_zero_files). Re-read ownership NOW (after the
    # whole fixed-point loop), since a source split in an early iteration may
    # have been further mutated. A pre-existing 0-owned feature from another
    # stage is NOT in ``split_ids`` and is left untouched.
    husks = [
        f for f in features
        if id(f) in split_ids and _owns_zero_files(f)
    ]
    if not husks:
        return
    result.husks_total = len(husks)

    to_drop: list[int] = []
    for husk in husks:
        # Files claimed by ANY other feature (survivor OR another husk) — those
        # are conserved elsewhere and may be removed from this husk losslessly.
        claimed_elsewhere: set[str] = set()
        for other in features:
            if other is husk:
                continue
            claimed_elsewhere |= _all_member_paths(other)

        members = list(getattr(husk, "member_files", None) or [])
        sole_members = [
            m for m in members
            if isinstance(getattr(m, "path", None), str)
            and m.path not in claimed_elsewhere
        ]
        deduped = len(members) - len(sole_members)
        result.husk_members_deduped += deduped
        sole_paths = set(m.path for m in sole_members)

        # (2) Empty shell — every member conserved elsewhere → drop the husk.
        if not sole_members:
            to_drop.append(id(husk))
            result.husks_dropped += 1
            continue

        # (3) Keep as the de-duped 0-owned shared residual. Strictly subtractive:
        # remove only the conserved-elsewhere double-counts; the surviving
        # sole-held members keep their (shared) role untouched. Nothing promoted
        # to owned → audited owned-share invariant preserved.
        if deduped:
            husk.member_files = sole_members
            # Prune ``paths`` + path-keyed surfaces of the de-duped files so the
            # husk's flat projections agree with its (shrunk) member ledger.
            husk.paths = sorted(p for p in (getattr(husk, "paths", None) or []) if p in sole_paths)
            for attr, file_field in _FILE_KEYED_SURFACES:
                items = getattr(husk, attr, None)
                if not items:
                    continue
                kept = [it for it in items if getattr(it, file_field, None) in sole_paths]
                if len(kept) != len(items):
                    setattr(husk, attr, kept)
        result.husk_files_conserved += len(sole_paths)
        result.husks_kept_residual += 1

    if to_drop:
        drop_set = set(to_drop)
        features[:] = [f for f in features if id(f) not in drop_set]


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
    # Every feature that produced ≥1 sub-feature (was de-owned in place) — the
    # ONLY features the husk-cleanup (step 6) is allowed to touch. Tracked by
    # object identity; pre-existing 0-owned features from other stages are never
    # in this set, keeping the cleanup strictly additive
    # (rule-stage-8-fixes-must-be-additive).
    split_sources: list["Feature"] = []
    split_source_ids: set[int] = set()
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
                if id(source) not in split_source_ids:
                    split_source_ids.add(id(source))
                    split_sources.append(source)
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

    # ── Step 6 — husk cleanup ──────────────────────────────────────────────
    # A split source that ended 0-owned is a degenerate "husk" (owns nothing,
    # lists its de-owned residual as shared). Run the cleanup over the FINAL
    # feature set (incl. minted subs, so "claimed elsewhere" sees them) but only
    # ACTING on the split sources. Conserves every file. Must run after
    # ``features.extend(all_new)`` so the conservation/claim test sees the full
    # post-stage feature set.
    _cleanup_husks(features, split_sources, cut=cut, result=result)
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
