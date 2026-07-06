"""Product-Spine Wave 1 — Layer-1 hygiene (spec §4.1).

Two deterministic, scale-invariant rules that kill the E-carrier class
(evidence report §E-2: in 10/10 board scans, concern-named dev features
carried BARE DIRECTORY paths — ``.``, ``frontend``, ``apps/web`` — and
dragged whole-app scope, up to 136,771 LOC in one dev, into whichever
product feature they landed in):

**Bare-dir member ban** (``FAULTLINE_SPINE_BAREDIR``, default ON)
    No developer feature may hold the repo root (``.``) or a bare
    directory as a member path. The SOURCE fix lives in
    ``extractors/package.py`` (dep-category anchors now carry the
    manifest FILE that declares the dependency — the explicit file the
    evidence actually supports — instead of the manifest's directory).
    This module provides the defensive reconcile-time guard: any
    candidate path that is a root marker or a PROVABLE directory (not a
    tracked file, but a ``path + "/"`` prefix of at least one tracked
    file) is rejected at claim time, so no extractor — present or
    future — can reintroduce the class. Paths that are neither tracked
    files nor provable directories are left alone (they can't vacuum
    LOC and synthetic test fixtures rely on them).

**Concern-facet rule** (``FAULTLINE_SPINE_FACETS``, default ON)
    A developer feature whose name matches the documented UNIVERSAL
    cross-cutting vocabulary (``data/concern-facets.yaml`` — auth,
    i18n, email, analytics, billing, ai, background-jobs, logging,
    telemetry, config…) AND whose claimed files span MORE THAN ONE
    route/workspace subtree is a cross-cutting FACET, not a vertical
    capability. It keeps existence (``role="facet"`` on the feature —
    a cross-cutting view the dashboard can render), but:

      * it is excluded from product-feature membership (Stage 6.5
        votes, the Stage 8 analyst payload, Stage 8.5 backfill and the
        6.7d digest / re-attribution all skip facets as owners);
      * it never carries LOC into any product feature (its
        ``product_feature_id`` stays ``None`` and Stage 6.97's
        primary-owner election deprioritizes facets, so shared files
        count at their structural owner);
      * it does not seed the Stage 2.6 import closure (a cross-cutting
        name must not BFS-vacuum the app).

    Subtree grain (structural, universal): a file's subtree is the
    longest matching WORKSPACE root when the repo declares workspaces,
    else its top-level directory — refined by a Next-style route-group
    segment (``app/(auth)`` and ``app/(dashboard)`` are distinct route
    subtrees even though both live under ``app/``). Repo-root files
    map to the ``.`` pseudo-subtree.

Both rules are deterministic, $0 LLM, and contain no repo-specific
paths or names (the vocabulary file documents why each entry is
universal — the concern slugs are the engine's own ``stage1_anchors``
dependency-category table plus classic infra concerns).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Iterable

from faultline.pipeline_v2.data import load_yaml

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.pipeline_v2.extractors.base import AnchorCandidate
    from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature

__all__ = [
    "SPINE_BAREDIR_ENV",
    "SPINE_FACETS_ENV",
    "baredir_ban_enabled",
    "facets_enabled",
    "is_root_marker",
    "strip_bare_dir_paths",
    "strip_bare_dir_feature_paths",
    "concern_vocabulary",
    "is_concern_name",
    "subtree_of",
    "classify_concern_facets",
    "is_facet",
]

SPINE_BAREDIR_ENV = "FAULTLINE_SPINE_BAREDIR"
SPINE_FACETS_ENV = "FAULTLINE_SPINE_FACETS"

_FACET_ROLE = "facet"

#: Concern vocabulary data file (packaged; authoring copy in ``eval/``).
_CONCERN_DATA_FILE = "concern-facets.yaml"


def _flag_enabled(env: str) -> bool:
    """Default ON; ``0``/``false`` disables."""
    return os.environ.get(env, "1").strip().lower() not in {"0", "false"}


def baredir_ban_enabled() -> bool:
    """Bare-dir member ban — default ON, ``FAULTLINE_SPINE_BAREDIR=0`` off."""
    return _flag_enabled(SPINE_BAREDIR_ENV)


def facets_enabled() -> bool:
    """Concern-facet rule — default ON, ``FAULTLINE_SPINE_FACETS=0`` off."""
    return _flag_enabled(SPINE_FACETS_ENV)


# ── Bare-dir member ban ─────────────────────────────────────────────────


def is_root_marker(path: str) -> bool:
    """``True`` for a whole-repo structural marker (``.`` / ``""`` / ``..``).

    Mirrors ``stage_6_97_feature_loc._is_root_marker`` (kept separate so
    the LOC stage stays import-light); disk-independent.
    """
    r = str(path).replace("\\", "/").strip().strip("/").strip()
    return r in ("", ".", "..")


def _normalize(path: str) -> str:
    return str(path).replace("\\", "/").strip().strip("/")


def _bad_paths(
    paths: Iterable[str],
    tracked: frozenset[str],
    dir_prefixes: frozenset[str],
) -> list[str]:
    """Paths violating the ban: root markers + provable directories.

    A path is a PROVABLE directory when it is not itself a tracked file
    but is a directory-prefix of at least one tracked file. Unknown
    strings that match neither test are NOT flagged — we only reject
    what the repo's own file universe proves is a directory claim.
    """
    out: list[str] = []
    for p in paths:
        norm = _normalize(p)
        if is_root_marker(p):
            out.append(p)
        elif norm not in tracked and norm in dir_prefixes:
            out.append(p)
    return out


def _dir_prefix_universe(tracked: frozenset[str]) -> frozenset[str]:
    """Every ancestor directory of every tracked file (normalized)."""
    dirs: set[str] = set()
    for f in tracked:
        parts = _normalize(f).split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return frozenset(dirs)


def strip_bare_dir_paths(
    candidates: list["AnchorCandidate"],
    tracked_files: Iterable[str],
) -> dict[str, Any]:
    """Reconcile-time guard — reject bare-dir / root-marker claims.

    ``AnchorCandidate`` is a frozen dataclass, so offending candidates are
    REPLACED in the caller's list (``dataclasses.replace`` with the
    filtered path tuple). Returns telemetry:
    ``{"paths_dropped": int, "candidates_touched": int, "sample": [...]}``.
    No-op (and zero telemetry) when the ban is disabled.
    """
    import dataclasses

    tele: dict[str, Any] = {
        "paths_dropped": 0, "candidates_touched": 0, "sample": [],
    }
    if not baredir_ban_enabled():
        return tele
    tracked = frozenset(_normalize(f) for f in tracked_files)
    dir_prefixes = _dir_prefix_universe(tracked)
    for idx, cand in enumerate(candidates):
        bad = _bad_paths(cand.paths, tracked, dir_prefixes)
        if not bad:
            continue
        bad_set = set(bad)
        candidates[idx] = dataclasses.replace(
            cand, paths=tuple(p for p in cand.paths if p not in bad_set),
        )
        tele["paths_dropped"] += len(bad)
        tele["candidates_touched"] += 1
        if len(tele["sample"]) < 10:
            tele["sample"].append(
                {"candidate": cand.name, "dropped": sorted(bad_set)[:5]},
            )
    return tele


def strip_bare_dir_feature_paths(features: list[Any]) -> int:
    """Emission-level sweep — drop ROOT-MARKER paths + member_files rows.

    Late stages union dev paths into product features, so a clean Layer 1
    keeps them clean; this defensive sweep (same kill-switch) guarantees
    no later stage can re-emit the whole-repo marker class (`.`/``/`..`),
    which is detectable without the tracked-file universe. Returns the
    number of entries dropped across ``paths`` + ``member_files``.
    """
    if not baredir_ban_enabled():
        return 0
    dropped = 0
    for f in features:
        paths = getattr(f, "paths", None)
        if paths:
            kept = [p for p in paths if not is_root_marker(str(p))]
            if len(kept) != len(paths):
                dropped += len(paths) - len(kept)
                f.paths = kept
        mfs = getattr(f, "member_files", None)
        if mfs:
            kept_mf = [
                mf for mf in mfs
                if not is_root_marker(
                    str(getattr(mf, "path", None) if not isinstance(mf, dict)
                        else mf.get("path") or ""))
            ]
            if len(kept_mf) != len(mfs):
                dropped += len(mfs) - len(kept_mf)
                f.member_files = kept_mf
    return dropped


# ── Concern-facet rule ──────────────────────────────────────────────────


_VOCAB_CACHE: dict[str, frozenset[str]] | None = None


def concern_vocabulary() -> dict[str, frozenset[str]]:
    """``{concern_slug: frozenset(aliases incl. the slug)}`` from the
    packaged ``concern-facets.yaml``. Cached for the process lifetime.
    """
    global _VOCAB_CACHE
    if _VOCAB_CACHE is not None:
        return _VOCAB_CACHE
    doc = load_yaml(_CONCERN_DATA_FILE)
    out: dict[str, frozenset[str]] = {}
    for slug, block in (doc.get("concerns") or {}).items():
        if not isinstance(slug, str) or not slug:
            continue
        aliases = {slug.strip().lower()}
        if isinstance(block, dict):
            for a in block.get("aliases") or []:
                if isinstance(a, str) and a.strip():
                    aliases.add(a.strip().lower())
        out[slug] = frozenset(aliases)
    _VOCAB_CACHE = out
    return out


def is_concern_name(name: str | None) -> str | None:
    """The matched concern slug when *name* IS a cross-cutting concern,
    else ``None``.

    Match rule (conservative — exact identity, never substring): the
    kebab slug equals a concern slug/alias, OR its token SET equals an
    alias's token set (``jobs-background`` ≡ ``background-jobs``). A
    compound name carrying extra tokens (``admin-email-domain``) never
    matches — those are vertical features that merely mention a concern.
    """
    if not name:
        return None
    slug = str(name).strip().lower()
    if not slug:
        return None
    tokens = frozenset(t for t in slug.split("-") if t)
    for concern, aliases in concern_vocabulary().items():
        if slug in aliases:
            return concern
        for a in aliases:
            if tokens == frozenset(t for t in a.split("-") if t):
                return concern
    return None


def subtree_of(path: str, workspace_roots: tuple[str, ...]) -> str:
    """The route/workspace subtree a file belongs to.

    Priority: longest matching workspace root → route-group-refined
    top-level dir (``app/(auth)``) → top-level dir → ``.`` (repo root).
    """
    norm = _normalize(path)
    best = ""
    for root in workspace_roots:
        r = _normalize(root)
        if r and (norm == r or norm.startswith(r + "/")) and len(r) > len(best):
            best = r
    if best:
        return best
    segs = norm.split("/")
    if len(segs) <= 1:
        return "."
    if len(segs) >= 3 and segs[1].startswith("(") and segs[1].endswith(")"):
        return segs[0] + "/" + segs[1]
    return segs[0]


def _feature_subtrees(
    paths: Iterable[str], workspace_roots: tuple[str, ...],
) -> set[str]:
    """Distinct subtrees of a feature's claimed files. Repo-root files
    (``.`` pseudo-subtree — manifests like ``package.json``) are NOT a
    subtree of their own: a root manifest plus one real subtree is still
    a single-subtree feature."""
    out: set[str] = set()
    for p in paths:
        if is_root_marker(p):
            continue
        st = subtree_of(p, workspace_roots)
        if st != ".":
            out.add(st)
    return out


def classify_concern_facets(
    features: list["DeveloperFeature"],
    workspace_roots: tuple[str, ...],
) -> dict[str, Any]:
    """Mark cross-cutting concern devs as facets (``role="facet"``).

    Runs at the END of Stage 2 reconciliation — on the authored CLAIM
    set, before the import closure / expansion stages derive additional
    membership (derived reach must never re-classify a vertical feature
    as a facet). Mutates ``role`` in place; returns telemetry.
    """
    tele: dict[str, Any] = {"facets": 0, "names": []}
    if not facets_enabled():
        return tele
    for f in features:
        concern = is_concern_name(getattr(f, "name", None))
        if concern is None:
            continue
        subtrees = _feature_subtrees(getattr(f, "paths", ()) or (),
                                     workspace_roots)
        if len(subtrees) > 1:
            f.role = _FACET_ROLE
            tele["facets"] += 1
            if len(tele["names"]) < 20:
                tele["names"].append(
                    {"name": f.name, "concern": concern,
                     "subtrees": sorted(subtrees)[:6]},
                )
    return tele


def is_facet(feature: Any) -> bool:
    """``True`` when *feature* is a concern facet (cross-cutting view)."""
    return getattr(feature, "role", None) == _FACET_ROLE
