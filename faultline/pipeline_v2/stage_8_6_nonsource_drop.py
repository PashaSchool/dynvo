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

Increment-4 LEVER A — workspace-anchor deflation
================================================

The all-or-nothing feature drop above only fires when 100% of a
feature's paths are non-source. A monorepo *workspace anchor* (the
``[package] workspace anchor`` catch-all) is the opposite case: it owns a
huge MIXED path-set — real route/page source AND a long tail of static
assets, locale JSON, videos, plus shared scaffold (``lib`` / ``utils`` /
``types`` / ``hooks``). Those non-features are what make the anchor the
``owned_max_feature_share`` blob ceiling, yet the all-or-nothing drop
can't touch them (the anchor has plenty of real source so it survives
whole).

Two deterministic, member-level deflators run here, AFTER flows / user
flows are built (Stage 6.7) so they are provably flow-immune:

* :func:`strip_nonsource_members` — removes the non-source MEMBER FILES
  from any feature with a source/non-source mix. A workspace anchor (or
  any feature) shouldn't OWN ``.png`` / ``.mp4`` / locale-json / static
  assets. Reuses the SAME :func:`_path_is_source` predicate — no new
  vocabulary, same conservatism (extensionless = source, schema/.css =
  source).

* :func:`deown_anchor_scaffold` — a file under a universal shared-scaffold
  directory that is a PRIMARY/anchor member of a WORKSPACE-ANCHOR feature
  is reclassified to ``role="shared"`` (``primary=False``) and dropped
  from the anchor's exclusive ``paths``. It stops counting toward
  ``owned_max_feature_share`` (which credits a file to a feature only
  when ``primary`` or ``role in {anchor, owner}``) but is NOT lost — it
  stays in ``member_files`` as a shared claim. Applies ONLY to workspace
  anchors (never a real leaf ``lib`` feature, which is not a workspace
  anchor and so is never gutted).

Both are precision-safe: ``flows`` / ``user_flows`` / feature ``name`` /
the path-keyed ``_file_set`` used by phantom-dup + name dedup are
untouched (de-own keeps the path in ``member_files``; only its ``role`` /
``primary`` flip). The ONLY metric that moves is
``owned_max_feature_share`` (down). No real feature is dropped.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.stage_8_7_anchor_desink import (
    _is_workspace_anchor,
    _prune_surfaces,
)

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


# ── Increment-4 LEVER A — shared-scaffold de-own vocabulary ─────────────────
#
# The canonical shared-scaffold directory vocabulary lives in Stage 8.6.5
# (``_SCAFFOLD_SEGMENTS`` in ``stage_8_6_5_scaffold_filter``). We REUSE a
# documented SUBSET of it here. Stage 8.6.5 can afford the wider vocabulary
# (which also includes ``components`` / ``ui`` / ``i18n`` / ``locale``)
# because it only demotes a scaffold file from a SPECIFIC feature when a
# fan-in guard also fires (``>= max(3, P90)`` claimants) — the guard is what
# keeps a genuine product ``components/`` surface safe.
#
# LEVER A de-owns from a WORKSPACE ANCHOR with NO fan-in guard, so it must
# restrict itself to the segments that are UNAMBIGUOUSLY non-feature shared
# scaffold — pure cross-cutting infrastructure that a package container never
# legitimately "owns" as a product surface:
#
#   lib / utils / helpers / hooks / types / constants / config / styles /
#   shared / common
#
# We DELIBERATELY EXCLUDE ``components`` / ``ui`` / ``i18n`` / ``intl`` /
# ``locale`` from the de-own set: those can hold real product UI / route
# surfaces, and without a fan-in guard de-owning them risks gutting genuine
# anchor members. (Their non-source assets — locale JSON, images — are
# already handled by :func:`strip_nonsource_members`.)
#
# Structural-token vocabulary only: no path/folder names from any corpus
# repo, no counts, no ratios, no tuned thresholds
# (memory/rule-no-magic-tuning + memory/rule-no-repo-specific-paths).
_DEOWN_SCAFFOLD_SEGMENTS: frozenset[str] = frozenset(
    {
        "lib", "libs",
        "util", "utils",
        "helper", "helpers",
        "hook", "hooks",
        "type", "types",
        "constant", "constants",
        "config", "configs",
        "style", "styles",
        "shared",
        "common",
    }
)

_DEOWN_SCAFFOLD_RE = re.compile(
    r"(?:^|/)(" + "|".join(sorted(_DEOWN_SCAFFOLD_SEGMENTS)) + r")(?:/|$)",
    re.IGNORECASE,
)


def _is_deown_scaffold_path(path: str) -> bool:
    """``True`` when *path* sits under an unambiguous shared-scaffold dir.

    Matches when any ``/``-bounded path SEGMENT is one of
    :data:`_DEOWN_SCAFFOLD_SEGMENTS`. Structural, scale-invariant,
    corpus-free.
    """
    return bool(_DEOWN_SCAFFOLD_RE.search(path))


def _member_files(feature: "Feature") -> list[Any]:
    """The feature's ``member_files`` list (empty when absent)."""
    return list(getattr(feature, "member_files", None) or [])


# ── Part 1 — non-source MEMBER strip ────────────────────────────────────────


@dataclass
class NonsourceStripResult:
    """Per-scan non-source member-strip outcome, for the stage artifact."""

    enabled: bool = True
    features_trimmed: int = 0          # features that lost >=1 member
    members_removed: int = 0           # total (feature, path) removals
    distinct_paths_removed: int = 0    # distinct files removed across all
    sample: list[str] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "features_trimmed": self.features_trimmed,
            "members_removed": self.members_removed,
            "distinct_paths_removed": self.distinct_paths_removed,
            "sample": list(self.sample[:20]),
        }


def _nonsource_strip_enabled() -> bool:
    """Default ON; disable via ``FAULTLINE_STAGE_8_6_NONSOURCE_STRIP=0``."""
    return os.environ.get("FAULTLINE_STAGE_8_6_NONSOURCE_STRIP", "0") != "0"


def strip_nonsource_members(features: list["Feature"]) -> NonsourceStripResult:
    """Remove non-source MEMBER FILES from any source/non-source-mix feature.

    A feature whose path-set is wholly non-source is dropped entirely by
    :func:`drop_all_nonsource_features`. This complements that: a feature
    that has BOTH source and non-source members (a workspace anchor that
    owns real route source AND a tail of ``.png`` / ``.mp4`` / locale-JSON
    static assets) keeps its source members but sheds the non-source ones
    — a feature should not OWN static assets.

    Mutates trimmed features in place: prunes ``paths``, ``member_files``,
    and the path-keyed attribution surfaces (reusing the Stage 8.7
    ``_prune_surfaces`` machinery). Reuses :func:`_path_is_source` verbatim
    — no new vocabulary, same conservatism (extensionless = source,
    schema/.css = source). Never empties a feature: the all-or-nothing
    drop already handled the wholly-non-source case, so by construction a
    mixed feature retains >=1 source member here.

    Deterministic, no LLM, scale-invariant. Default ON; disable via
    ``FAULTLINE_STAGE_8_6_NONSOURCE_STRIP=0``.
    """
    result = NonsourceStripResult(enabled=_nonsource_strip_enabled())
    if not result.enabled:
        return result

    distinct: set[str] = set()
    for f in features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        paths = list(getattr(f, "paths", None) or [])
        members = _member_files(f)
        # Candidate non-source files drawn from BOTH the exclusive ``paths``
        # projection and the full ``member_files`` ledger (a member may be a
        # non-source claim that never made it to ``paths``).
        member_paths: set[str] = {
            p for m in members
            if isinstance((p := getattr(m, "path", None)), str)
        }
        all_paths: set[str] = set(paths) | member_paths
        if not all_paths:
            continue
        nonsource = {p for p in all_paths if not _path_is_source(p)}
        if not nonsource:
            continue
        # Never strip a feature down to nothing — only act when a source
        # member survives (the all-or-nothing drop owns the empty case).
        if not any(_path_is_source(p) for p in all_paths):
            continue

        kept_paths = [p for p in paths if p not in nonsource]
        if members:
            kept_members = [
                m for m in members if getattr(m, "path", None) not in nonsource
            ]
            if len(kept_members) != len(members):
                f.member_files = kept_members
        if len(kept_paths) != len(paths):
            f.paths = kept_paths
        _prune_surfaces(f, nonsource)

        result.features_trimmed += 1
        result.members_removed += len(nonsource)
        distinct |= nonsource
        for p in sorted(nonsource):
            if len(result.sample) < 20:
                result.sample.append(p)

    result.distinct_paths_removed = len(distinct)
    return result


# ── Part 2 — workspace-anchor shared-scaffold de-own ────────────────────────


@dataclass
class AnchorScaffoldDeownResult:
    """Per-scan anchor-scaffold de-own outcome, for the stage artifact."""

    enabled: bool = True
    anchors_total: int = 0             # workspace-anchor features seen
    anchors_deowned: int = 0           # anchors that released >=1 member
    members_reclassified: int = 0      # total member files flipped to shared
    distinct_paths_deowned: int = 0
    sample: list[str] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "anchors_total": self.anchors_total,
            "anchors_deowned": self.anchors_deowned,
            "members_reclassified": self.members_reclassified,
            "distinct_paths_deowned": self.distinct_paths_deowned,
            "sample": list(self.sample[:20]),
        }


def _scaffold_deown_enabled() -> bool:
    """Default ON; disable via ``FAULTLINE_STAGE_8_6_ANCHOR_SCAFFOLD_DEOWN=0``."""
    return (
        os.environ.get("FAULTLINE_STAGE_8_6_ANCHOR_SCAFFOLD_DEOWN", "1") != "0"
    )


def deown_anchor_scaffold(features: list["Feature"]) -> AnchorScaffoldDeownResult:
    """Reclassify shared-scaffold members of WORKSPACE-ANCHOR features.

    A file under an unambiguous shared-scaffold directory
    (:data:`_DEOWN_SCAFFOLD_SEGMENTS`) that is currently a PRIMARY / anchor
    member of a workspace-ANCHOR feature is reclassified to ``role="shared"``
    (``primary=False``) and dropped from the anchor's exclusive ``paths``.
    The file stays in ``member_files`` (now as a shared claim) so it is NOT
    lost and the path-keyed ``_file_set`` used by phantom-dup / name dedup is
    unchanged — only the OWNED set shrinks, deflating
    ``owned_max_feature_share``.

    Scope guard (load-bearing): ONLY workspace-anchor features are touched
    (detected via the ``"workspace anchor"`` description marker — the same
    discriminator Stage 8.7 de-sink and Stage 8.8 use). A genuine leaf
    ``lib`` feature (e.g. a published util package) is NOT a workspace anchor
    and is therefore never gutted. A member already ``role="shared"`` is left
    as-is (idempotent).

    Deterministic, no LLM, scale-invariant (structural dir vocabulary, no
    counts / ratios / repo paths). Default ON; disable via
    ``FAULTLINE_STAGE_8_6_ANCHOR_SCAFFOLD_DEOWN=0``.
    """
    result = AnchorScaffoldDeownResult(enabled=_scaffold_deown_enabled())
    if not result.enabled:
        return result

    anchors = [
        f for f in features
        if getattr(f, "layer", "developer") == "developer"
        and _is_workspace_anchor(f)
    ]
    result.anchors_total = len(anchors)
    if not anchors:
        return result

    distinct: set[str] = set()
    for anchor in anchors:
        members = _member_files(anchor)
        if not members:
            continue
        deowned_here: set[str] = set()
        for m in members:
            path = getattr(m, "path", None)
            if not path or not _is_deown_scaffold_path(path):
                continue
            # Only flip OWNED members (primary or anchor/owner role). A member
            # already shared contributes nothing to owned_max — skip it.
            is_owned = bool(getattr(m, "primary", False)) or (
                getattr(m, "role", None) in ("anchor", "owner")
            )
            if not is_owned:
                continue
            m.role = "shared"
            m.primary = False
            deowned_here.add(path)

        if not deowned_here:
            continue

        # Drop the de-owned paths from the anchor's exclusive ``paths`` list
        # so the primary projection stays consistent with the ledger. We do
        # NOT prune the path-keyed attribution surfaces here: the file is
        # still a (shared) member of the anchor, so its line-level provenance
        # legitimately remains.
        kept_paths = [p for p in (anchor.paths or []) if p not in deowned_here]
        if len(kept_paths) != len(anchor.paths or []):
            anchor.paths = kept_paths

        result.anchors_deowned += 1
        result.members_reclassified += len(deowned_here)
        distinct |= deowned_here
        for p in sorted(deowned_here):
            if len(result.sample) < 20:
                result.sample.append(p)

    result.distinct_paths_deowned = len(distinct)
    return result


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


def drop_phantom_product_features(
    developer_features: list["Feature"],
    product_features: list["Feature"],
) -> tuple[list["Feature"], int]:
    """Drop product features that have ZERO developer-feature members.

    A product feature is *derived from* the developer features that vote for
    it (Stage 6.5 / Stage 8), so one with no surviving member is an artifact:
    the analyst named a cluster whose members were later merged or renamed
    away, leaving a content-less row whose ``paths`` are already owned by the
    product features its (vanished) members actually belong to. Such a row has
    no LOC, no flows and no symbol attributions — it is pure duplication.

    :func:`reconcile_product_features` already removes these, but only runs
    when some non-source developer features were dropped; on a clean repo
    (nothing non-source to drop) the phantom product features survived to
    output. This function applies the SAME emptiness rule — membership via
    ``developer_feature.product_feature_id == product_feature.name`` — but is
    deterministic, path-preserving and safe to run on EVERY scan.

    Returns ``(kept_product_features, dropped_count)``.
    """
    member_pf_ids: set[str] = set()
    for f in developer_features:
        pid = getattr(f, "product_feature_id", None)
        if pid:
            member_pf_ids.add(pid)

    kept: list["Feature"] = []
    dropped = 0
    for pf in product_features:
        if pf.name in member_pf_ids:
            kept.append(pf)
        else:
            dropped += 1
    return kept, dropped


__all__ = [
    "drop_all_nonsource_features",
    "reconcile_product_features",
    "drop_phantom_product_features",
    "NonsourceStripResult",
    "strip_nonsource_members",
    "AnchorScaffoldDeownResult",
    "deown_anchor_scaffold",
]
