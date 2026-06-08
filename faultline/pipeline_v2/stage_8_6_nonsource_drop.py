"""Stage 8.6 — universal non-source scaffold/docs feature drop.

A developer feature whose ENTIRE path-set is non-source (docs, config,
static assets, certs, lockfiles) is junk: it carries no behaviour, no
flows, no health signal, yet it inflates the feature count and the
``llm_fallback_pct`` denominator. This stage drops such features with an
all-or-nothing predicate — a feature is removed ONLY when 100% of its
paths are non-source. A single source file keeps the whole feature.

Deterministic. No LLM. Scale-invariant: the predicate is a universal
extension-category test plus a tiny set of well-known build leaves. It
contains NO path/folder names harvested from any corpus repo, NO counts,
NO ratios, NO tuned thresholds (per memory/rule-no-magic-tuning +
memory/rule-no-repo-specific-paths).

Two load-bearing conservatism rules:

1. **Extensionless paths / bare directories are SOURCE.** A path is
   non-source ONLY when it has a real extension that is not a recognised
   source extension. No-extension → source. This protects libraries
   whose real modules live in bare directories (e.g. a package dir) and
   avoids mis-dropping a feature whose paths are all directories.

2. **Schema / product-source extensions are SOURCE.** ``.prisma``,
   ``.sql``, ``.graphql``, ``.proto``, ``.css`` describe product
   behaviour and must never be classed as scaffold.

Insertion point is AFTER Stage 8.5 member backfill — path-sets are not
final until 8.5 attaches ``.ts``/`.js`` members to schema-only anchors.
A Stage-5 insertion would wrongly drop real features that, at that
point, carry only ``prisma/schema.prisma``.

This complements (does not duplicate) the Stage-4 residual guards: those
filter noise PATHS before clustering; this drops a finished feature
whose surviving path-set is wholly non-source.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faultline.models.types import Feature


# ── Source-extension vocabulary (universal, extension-only) ─────────────────
#
# Real source extensions across the stacks Faultlines targets. Schema /
# query / style files are SOURCE (product behaviour). The list is keyed
# on extension category, never on a path or folder name, so it scales
# from a 5-file lib to a 600-file monorepo identically.
_SOURCE_EXTS: frozenset[str] = frozenset(
    {
        # JS / TS family
        ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
        ".vue", ".svelte", ".astro",
        # Python
        ".py", ".pyi", ".pyx",
        # Go / Rust / C-family / JVM / .NET
        ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".hxx",
        ".m", ".mm", ".java", ".kt", ".kts", ".scala", ".groovy",
        ".cs", ".fs", ".vb",
        # Dynamic / scripting
        ".rb", ".php", ".ex", ".exs", ".erl", ".clj", ".cljs",
        ".swift", ".dart", ".lua", ".pl", ".r", ".jl",
        ".sh", ".bash", ".zsh", ".fish", ".ps1",
        # Schema / query / IDL / style — product source, NOT scaffold
        ".prisma", ".sql", ".graphql", ".gql", ".proto", ".thrift",
        ".css", ".scss", ".sass", ".less", ".styl",
        # Templating that holds behaviour
        ".erb", ".haml", ".slim", ".ejs", ".hbs", ".pug", ".njk",
        ".liquid", ".twig", ".blade",
        ".html", ".htm", ".xml", ".xsl",
    }
)


# Source files that conventionally carry NO extension. These are real
# build / runtime entrypoints, not scaffold. Matched on the lowercase
# leaf name. Universal across stacks; not corpus-derived.
_SOURCE_LEAVES: frozenset[str] = frozenset(
    {
        "dockerfile",
        "containerfile",
        "makefile",
        "gnumakefile",
        "rakefile",
        "gemfile",
        "guardfile",
        "procfile",
        "vagrantfile",
        "brewfile",
        "justfile",
        "caddyfile",
        "jenkinsfile",
        "berksfile",
        "fastfile",
        "appfile",
        "podfile",
        "cmakelists.txt",
        "bsconfig",
    }
)


def _path_is_source(path: str) -> bool:
    """``True`` when *path* should count as a source path.

    Conservative by construction:

    * A bare directory or extensionless file → SOURCE (rule 1). This
      includes a path whose last segment has no ``.`` at all.
    * A recognised source leaf name (``Dockerfile``, ``Makefile`` …) →
      SOURCE.
    * A real extension in :data:`_SOURCE_EXTS` → SOURCE.
    * Anything else (a real extension NOT in the source set — ``.md``,
      ``.json``, ``.txt``, ``.pem`` …) → non-source.
    """
    leaf = path.rstrip("/").rsplit("/", 1)[-1].lower()
    if not leaf:
        return True  # trailing-slash directory
    if leaf in _SOURCE_LEAVES:
        return True
    # Find the real extension. A leading-dot dotfile with no further dot
    # (``.gitignore`` → leaf ".gitignore") has no "real" extension — but a
    # dotfile is config, not source; treat ``.<name>`` with a single dot
    # at index 0 as extensionless-but-config only when it has a known
    # non-source meaning. To stay conservative we classify a path as
    # non-source ONLY when it has a real extension token that is not a
    # source extension. Compute the extension after the LAST dot.
    dot = leaf.rfind(".")
    if dot <= 0:
        # No dot, or dot only at index 0 (pure dotfile like ".gitignore").
        # No real extension → treat as SOURCE per rule 1 (extensionless).
        return True
    ext = leaf[dot:]
    # Handle compound declaration files (``foo.d.ts``) — the inner ``.ts``
    # is the real extension and IS source; but a ``.d.ts`` is a type stub.
    # We keep it SOURCE (it is TS) to stay conservative.
    return ext in _SOURCE_EXTS


def _feature_is_all_nonsource(feature: "Feature") -> bool:
    """``True`` when EVERY path on *feature* is non-source.

    All-or-nothing: a single source path keeps the feature. A feature
    with no paths at all is NOT dropped (no evidence to act on).
    """
    paths = list(getattr(feature, "paths", None) or [])
    if not paths:
        return False
    return all(not _path_is_source(p) for p in paths)


def _is_enabled() -> bool:
    """Default ON; disable via ``FAULTLINE_STAGE_8_6_NONSOURCE_DROP=0``."""
    return os.environ.get("FAULTLINE_STAGE_8_6_NONSOURCE_DROP", "1") != "0"


def drop_all_nonsource_features(
    features: list["Feature"],
) -> tuple[list["Feature"], list[str]]:
    """Return ``(kept_features, dropped_names)``.

    Drops every developer feature whose entire path-set is non-source.
    Product features (``layer == "product"``) are never evaluated here —
    Layer-2 consistency is repaired separately by the caller.
    """
    if not _is_enabled():
        return list(features), []

    kept: list["Feature"] = []
    dropped_names: list[str] = []
    for f in features:
        if getattr(f, "layer", "developer") != "developer":
            kept.append(f)
            continue
        if _feature_is_all_nonsource(f):
            dropped_names.append(f.name)
        else:
            kept.append(f)
    return kept, dropped_names


def reconcile_product_features(
    kept_developer_features: list["Feature"],
    product_features: list["Feature"],
) -> tuple[list["Feature"], dict[str, int]]:
    """Repair Layer-2 consistency after dropping developer features.

    Each developer feature links to a product feature via
    ``product_feature_id`` (the product :class:`Feature` ``name`` slug).
    After dropping junk developer features we:

    * recompute each surviving product feature's ``paths`` as the union
      of its remaining members' paths (this is the product feature's
      "member_count / routes" surface — paths ARE the route/file union);
    * drop a product feature that lost ALL of its members.

    Returns ``(kept_product_features, telemetry)`` where telemetry holds
    ``{"recomputed": N, "dropped_empty": M}``.
    """
    members_by_pf: dict[str, list["Feature"]] = {}
    for f in kept_developer_features:
        pid = getattr(f, "product_feature_id", None)
        if pid:
            members_by_pf.setdefault(pid, []).append(f)

    kept_pfs: list["Feature"] = []
    recomputed = 0
    dropped_empty = 0
    for pf in product_features:
        members = members_by_pf.get(pf.name)
        if not members:
            # No surviving developer member → product feature is now empty.
            dropped_empty += 1
            continue
        merged: list[str] = []
        seen: set[str] = set()
        for m in members:
            for p in m.paths:
                if p not in seen:
                    merged.append(p)
                    seen.add(p)
        if list(pf.paths) != merged:
            pf.paths = merged
            recomputed += 1
        kept_pfs.append(pf)

    return kept_pfs, {"recomputed": recomputed, "dropped_empty": dropped_empty}


__all__ = [
    "drop_all_nonsource_features",
    "reconcile_product_features",
]
