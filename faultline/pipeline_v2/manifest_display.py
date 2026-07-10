"""B27 — package-manifest display names (S1 dependency-manifest grounding).

An integration / app-store PF anchored at a PACKAGE DIRECTORY
(``hub:<family-dir>/<vendor>`` or ``ws:<dir>``) too often ships a display
that is the raw directory-name concatenation ("Exchange2013calendar",
"Office365video", "Stripepayment"). The package itself usually DECLARES
its human name in its own metadata — the repo is the authority
(mechanisms, not vocabularies: no vendor lists, no name dictionaries;
we read what the maintainer wrote inside the anchor subtree).

Manifest sources, in priority order (first authored name wins):

1. ``config.json``               ``"name"`` (JSON string — the cal.com
   app-store convention, and a generic per-package metadata shape).
2. ``_metadata.ts|js`` / ``metadata.ts|js`` — first top-level
   ``name: "…"`` string literal (the metadata-module convention that
   predates ``config.json`` in the same ecosystem; bounded regex read —
   the engine's TS layer is regex-grade by design).
3. ``package.json``              ``"displayName"``.
4. ``package.json``              ``"name"`` when HUMAN-authored: the npm
   scope is stripped (``@calcom/stripepayment`` → ``stripepayment``) and
   the remainder compared to the directory slug modulo case / hyphens /
   underscores — equal means it is just the dir name again, NOT an
   authored display name.
5. ``composer.json``             ``"name"`` (``vendor/pkg``), vendor part
   stripped, same authored test.
6. ``pyproject.toml``            ``[project] name``, same authored test.

Fallback rung strictly BELOW the manifests (still no dictionaries): a
mechanical word-split of the directory slug on letter/digit boundaries
(``exchange2013calendar`` → ``exchange-2013-calendar`` → "Exchange 2013
Calendar" after the caller's titleize) — ``word_split_slug`` returns
``None`` when the slug has no such boundary (nothing mechanical to add).

DISPLAY CHANNEL ONLY (the B16 pattern): the naming contract feeds these
as ranked display-name candidates; ``Feature.name`` slugs, ``anchor_id``
and every other identity field are never touched. Missing / unparseable
/ oversized manifests fall through to the next rung — never a crash.
Kill-switch: ``FAULTLINE_PF_MANIFEST_NAME=0`` restores pre-B27 displays
byte-identically.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

__all__ = [
    "PF_MANIFEST_NAME_ENV",
    "pf_manifest_name_enabled",
    "package_dir_of_anchor",
    "manifest_display_name",
    "word_split_slug",
]

PF_MANIFEST_NAME_ENV = "FAULTLINE_PF_MANIFEST_NAME"

#: Bounded manifest read — package metadata files are small; anything
#: bigger is not a manifest (a work bound, not a tuning knob).
_MAX_MANIFEST_BYTES = 256 * 1024

#: An authored display name is a short human phrase, not a prose blob.
#: Purely a sanity bound (scale-invariant); typical names are < 60 chars.
_MAX_NAME_CHARS = 120

#: First top-level ``name: "…"`` / ``name: '…'`` literal in a metadata
#: module (``export const metadata = { name: "Stripe", … }``).
_TS_NAME_RE = re.compile(r"""^\s{0,8}name:\s*(["'])(.+?)\1\s*,?\s*$""",
                         re.MULTILINE)

#: Metadata-module basenames probed inside the package dir (rung 2).
_METADATA_MODULES = ("_metadata.ts", "metadata.ts", "_metadata.js",
                     "metadata.js")

#: Letter↔digit boundary (both directions) for the mechanical word split.
_LETTER_DIGIT_BOUNDARY = re.compile(
    r"(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")


def pf_manifest_name_enabled() -> bool:
    """B27 package-manifest PF display names. Default ON;
    ``FAULTLINE_PF_MANIFEST_NAME=0`` restores the pre-B27 display names
    byte-identically."""
    return os.environ.get(PF_MANIFEST_NAME_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def package_dir_of_anchor(anchor_id: str) -> str | None:
    """Repo-relative package directory of a PACKAGE-DIR anchor, else
    ``None``.

    ``ws:<dir>`` → the workspace-package dir; ``hub:<dir>/<vendor>`` →
    the vendor package dir. A single-segment ``hub:<dir>`` is a hub CORE
    (its "<Family> Core" display is designed); multi-segment hub paths
    are treated as potential vendor dirs — the id shape cannot separate
    a multi-segment core dir from ``<dir>/<vendor>`` (the §4.8
    composition shares this ambiguity), so callers additionally guard on
    the core display convention (``" Core"``). Every other source
    (``route:``, ``fdir:``, …) is not a package dir: a route-anchored
    product PF keeps its route-derived name."""
    aid = anchor_id or ""
    if ":" not in aid:
        return None
    src, _, path = aid.partition(":")
    segs = [s for s in path.split("/") if s and s not in (".", "..")]
    if not segs or len(segs) != len([s for s in path.split("/") if s]):
        return None  # a traversal segment disqualifies the anchor
    if src == "ws":
        return "/".join(segs)
    if src == "hub" and len(segs) >= 2:
        return "/".join(segs)
    return None


def word_split_slug(slug: str) -> str | None:
    """Mechanical letter/digit word split of a directory slug —
    ``exchange2013calendar`` → ``exchange-2013-calendar`` — or ``None``
    when the slug carries no letter/digit boundary. Strictly the rung
    BELOW manifest names; the caller titleizes the hyphenated result."""
    s = slug or ""
    out = _LETTER_DIGIT_BOUNDARY.sub("-", s)
    return out if out != s else None


# ── manifest readers (each rung: best-effort, never a crash) ────────────


def _read_small(path: Path) -> str | None:
    """Bounded text read; ``None`` on anything unusual (missing, huge,
    unreadable, undecodable)."""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_MANIFEST_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="strict")
    except OSError:
        return None
    except UnicodeDecodeError:
        return None


def _clean_name(raw: object) -> str | None:
    """A usable display name: a str, whitespace-normalized, bounded,
    carrying at least one letter."""
    if not isinstance(raw, str):
        return None
    name = " ".join(raw.split())
    if not name or len(name) > _MAX_NAME_CHARS:
        return None
    if not any(c.isalpha() for c in name):
        return None
    return name


def _slug_norm(text: str) -> str:
    """Case / hyphen / underscore / space-insensitive slug form."""
    return re.sub(r"[-_\s]+", "", (text or "").lower())


def _authored_slug_name(raw: object, rel_dir: str) -> str | None:
    """A manifest ``name`` that is more than the package's path again.

    Strips the ``@scope/`` (npm) or ``vendor/`` (composer) prefix, then
    compares to the terminal dir slug AND to the joined trailing path
    segments, modulo case / hyphens / underscores — equal means NOT an
    authored display name (``@calcom/stripepayment`` does not qualify
    for dir ``stripepayment``; ``@calcom/platform-enums`` does not
    qualify for dir ``packages/platform/enums`` — it is the PATH slug)."""
    name = _clean_name(raw)
    if not name:
        return None
    bare = name.rpartition("/")[2].strip()
    if not bare:
        return None
    segs = [s for s in (rel_dir or "").split("/") if s]
    bare_norm = _slug_norm(bare)
    for k in range(1, min(len(segs), 3) + 1):
        if bare_norm == _slug_norm("".join(segs[-k:])):
            return None
    return bare


def _json_field(path: Path, field: str) -> object:
    text = _read_small(path)
    if text is None:
        return None
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return doc.get(field) if isinstance(doc, dict) else None


def _metadata_module_name(pkg_dir: Path) -> str | None:
    """Rung 2 — first top-level ``name: "…"`` literal in a metadata
    module (``_metadata.ts`` class)."""
    for base in _METADATA_MODULES:
        text = _read_small(pkg_dir / base)
        if text is None:
            continue
        m = _TS_NAME_RE.search(text)
        if m:
            name = _clean_name(m.group(2))
            if name:
                return name
    return None


def _pyproject_name(pkg_dir: Path, rel_dir: str) -> str | None:
    """Rung 6 — ``[project] name`` (authored-test gated)."""
    text = _read_small(pkg_dir / "pyproject.toml")
    if text is None:
        return None
    try:
        import tomllib
        doc = tomllib.loads(text)
    except Exception:  # noqa: BLE001 — malformed toml is a skip, not a crash
        return None
    project = doc.get("project")
    if not isinstance(project, dict):
        return None
    return _authored_slug_name(project.get("name"), rel_dir)


def manifest_display_name(repo_root: str | os.PathLike[str],
                          rel_dir: str) -> str | None:
    """The package's OWN declared display name, or ``None``.

    ``rel_dir`` is the repo-relative package directory (a ``ws:`` /
    ``hub:``-vendor anchor path). Deterministic: pure bounded file reads
    in a fixed rung order; any missing / unparseable rung falls through."""
    rel = (rel_dir or "").strip("/")
    if not rel:
        return None
    try:
        root = Path(repo_root).resolve()
    except OSError:
        return None
    return _manifest_display_name_cached(str(root), rel)


@lru_cache(maxsize=4096)
def _manifest_display_name_cached(root: str, rel_dir: str) -> str | None:
    root_path = Path(root)
    pkg_dir = root_path / rel_dir
    try:
        resolved = pkg_dir.resolve()
        if root_path != resolved and root_path not in resolved.parents:
            return None  # containment: an anchor may never escape the repo
        if not pkg_dir.is_dir():
            return None
    except OSError:
        return None

    # 1. config.json "name" — a declared display string, used verbatim.
    name = _clean_name(_json_field(pkg_dir / "config.json", "name"))
    if name:
        return name
    # 2. metadata module name literal.
    name = _metadata_module_name(pkg_dir)
    if name:
        return name
    # 3. package.json "displayName" — display by definition.
    name = _clean_name(_json_field(pkg_dir / "package.json", "displayName"))
    if name:
        return name
    # 4. package.json "name" — authored-test gated (npm slugs equal to
    #    the dir name do NOT qualify).
    name = _authored_slug_name(
        _json_field(pkg_dir / "package.json", "name"), rel_dir)
    if name:
        return name
    # 5. composer.json "name" (vendor/pkg) — authored-test gated.
    name = _authored_slug_name(
        _json_field(pkg_dir / "composer.json", "name"), rel_dir)
    if name:
        return name
    # 6. pyproject.toml [project] name — authored-test gated.
    return _pyproject_name(pkg_dir, rel_dir)
