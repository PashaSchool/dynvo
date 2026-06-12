"""Product-string evidence collector — deterministic, $0 LLM.

The repo itself carries the product vocabulary the LLM namers need:
i18n catalogs, nav/sidebar registries, and route/page titles are all
EXPLICIT author intent expressed in code/config. This module harvests
those human-facing strings and keys them by the FILE they live in, so
a feature's evidence bundle can be assembled from the product strings
belonging to ITS member files (``feature.paths`` / ``member_files``).

Sources collected (all structural conventions, never repo-specific):

  * **i18n catalogs** — locale JSON/TS files under conventional dirs
    (``locales/``, ``i18n/``, ``messages/``, ``translations/``,
    ``lang/``) or named like a locale file (``en.json``,
    ``en-US.json``). Only the DEFAULT/English locale is read (one
    vocabulary, not 40 translations of it). Nested string values are
    flattened; keys are discarded.
  * **nav/sidebar registries** — JSX/TSX nav components and config
    arrays: ``{label/title/name: "...", href/path/to: "..."}`` object
    literals, plus ``<Link href=...>Label</Link>`` / ``<a href=...>``
    pairs inside files that look like navigation (basename or content
    signature). CLAUDE.md explicitly allows JSX nav as grounding.
  * **route/page titles** — ``<title>`` elements, ``metadata.title``
    (Next App Router convention), and top-heading literals (``<h1>``)
    in page/route files.

HARD RULE — README and prose docs are NEVER read. ``.md`` / ``.mdx`` /
``.rst`` / ``.txt`` / ``.adoc`` files (and anything named README*) are
structurally excluded before any IO happens (rule-no-readme). A test
asserts a README.md string can never enter a bundle.

Caps are STRUCTURAL, not corpus-tuned:

  * ``_MAX_STRINGS_PER_FILE`` = 40 — a nav registry / page rarely
    declares more distinct labels; beyond that the file is a data
    dump, not a navigation surface.
  * ``_BUNDLE_CAP`` = 30 — matches the existing Stage 8 evidence caps
    (``_TAXONOMY_UNION_CAP`` = 30 marketing labels) so the prompt
    budget for product strings equals the budget already granted to
    the external marketing taxonomy. Same scale, same justification.
  * ``_MAX_FILE_BYTES`` = 256 KiB — i18n catalogs above this are
    machine-generated dumps; reading them buys noise.

Ordering is anchor-first (review №3): when assembling a bundle the
caller passes the entity's anchor files; their strings are emitted
first, closure/residual files last, source-priority (nav > title >
i18n) within each group. All iteration is sorted → byte-stable output.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

__all__ = [
    "ProductString",
    "ProductStringIndex",
    "collect_product_strings",
]


# ── Structural caps (documented in module docstring) ───────────────────

_MAX_STRINGS_PER_FILE = 40
_BUNDLE_CAP = 30
_MAX_FILE_BYTES = 256 * 1024

# Source priority — lower sorts first. Nav labels are the maintainer's
# curated product taxonomy (strongest), page titles next, i18n catalog
# values last (broadest / noisiest).
_SOURCE_PRIORITY = {"nav": 0, "title": 1, "i18n": 2}

# ── HARD EXCLUSION — prose docs are never a signal source ──────────────

_PROSE_EXTENSIONS = frozenset({
    ".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc", ".asciidoc",
})
_PROSE_BASENAMES_PREFIX = ("readme", "changelog", "contributing", "license")


def _is_prose_doc(path: str) -> bool:
    """True for README / prose docs — structurally off-limits."""
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    suffix = "." + base.rsplit(".", 1)[-1] if "." in base else ""
    if suffix in _PROSE_EXTENSIONS:
        return True
    return base.startswith(_PROSE_BASENAMES_PREFIX)


# ── i18n catalog detection ──────────────────────────────────────────────

_I18N_DIR_SEGMENTS = frozenset({
    "locales", "locale", "i18n", "messages", "translations", "lang",
    "langs", "intl",
})
# Default-locale file stems. We read ONE vocabulary — the default /
# English catalog — never every translation.
_DEFAULT_LOCALE_STEM_RE = re.compile(
    r"^(en([-_][a-z]{2})?|default|messages|common|translation|index)$",
    re.IGNORECASE,
)
# A path segment that IS a locale code (``locales/en/common.json``).
_LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")


def _is_i18n_catalog(path: str) -> bool:
    """True when ``path`` is the DEFAULT-locale catalog of an i18n dir."""
    p = path.replace("\\", "/")
    segs = p.split("/")
    base = segs[-1]
    if "." not in base:
        return False
    stem, _, ext = base.rpartition(".")
    if ext.lower() not in ("json", "ts", "js"):
        return False
    dir_segs = [s.lower() for s in segs[:-1]]
    in_i18n_dir = any(s in _I18N_DIR_SEGMENTS for s in dir_segs)
    if not in_i18n_dir:
        return False
    # Skip non-default locale files/dirs (``locales/fr.json``,
    # ``locales/de/common.json``) — one vocabulary only. Positional
    # check: the segment directly under the i18n dir, or the file stem
    # itself, must be the default locale (or a non-locale name).
    for i, s in enumerate(dir_segs):
        if s in _I18N_DIR_SEGMENTS and i + 1 < len(dir_segs):
            child = dir_segs[i + 1]
            if _LOCALE_SEGMENT_RE.match(child) and not child.startswith("en"):
                return False
    if _LOCALE_SEGMENT_RE.match(stem.lower()):
        return stem.lower().startswith("en")
    return bool(_DEFAULT_LOCALE_STEM_RE.match(stem))


def _flatten_json_strings(obj: object, out: list[str]) -> None:
    """Collect nested string VALUES from a parsed locale JSON, sorted by key."""
    if len(out) >= _MAX_STRINGS_PER_FILE:
        return
    if isinstance(obj, str):
        s = obj.strip()
        if _is_human_string(s):
            out.append(s)
        return
    if isinstance(obj, dict):
        for k in sorted(obj):
            _flatten_json_strings(obj[k], out)
            if len(out) >= _MAX_STRINGS_PER_FILE:
                return
    elif isinstance(obj, list):
        for item in obj:
            _flatten_json_strings(item, out)
            if len(out) >= _MAX_STRINGS_PER_FILE:
                return


# TS/JS locale modules: ``key: "Human label"`` / ``key: 'Human label'``.
_TS_LOCALE_PAIR_RE = re.compile(
    r"""['"]?[\w.\-]+['"]?\s*:\s*(['"])((?:(?!\1).){2,120})\1""",
)


def _is_human_string(s: str) -> bool:
    """Filter for human-facing copy: short-ish, has a letter, not a
    path/URL/format-token/css blob."""
    if not (2 <= len(s) <= 120):
        return False
    if not re.search(r"[A-Za-z]", s):
        return False
    if s.startswith(("http://", "https://", "/", "./", "#", "{", "<")):
        return False
    if re.fullmatch(r"[\w./:#-]+", s) and ("/" in s or "." in s):
        return False  # path-like / dotted identifier
    return True


# ── Nav / sidebar registry detection ────────────────────────────────────

_NAV_BASENAME_RE = re.compile(
    r"(nav|sidebar|side-bar|menu|topbar|header|footer|tabs|breadcrumb|links|navigation)",
    re.IGNORECASE,
)
_NAV_CONTENT_RE = re.compile(r"<nav[\s>]|<Sidebar[\s>/]|<TabsList[\s>/]", re.IGNORECASE)

# {label: "Billing", href: "/settings/billing"} — label-ish key then
# string value; href-ish key anywhere in the same object literal.
_NAV_LABEL_KEY_RE = re.compile(
    r"""\b(?:label|title|name|text)\s*:\s*(['"])((?:(?!\1).){2,80})\1""",
)
_NAV_HREF_KEY_RE = re.compile(
    r"""\b(?:href|path|to|url|route)\s*:\s*(['"])((?:(?!\1).){1,160})\1""",
)
# <Link href="/x">Label</Link> and <a href="/x">Label</a>
_NAV_JSX_LINK_RE = re.compile(
    r"""<(?:Link|a|NavLink)\b[^>]*\bhref=\{?["']([^"'}]+)["']\}?[^>]*>\s*([^<{][^<]{1,80}?)\s*<""",
    re.DOTALL,
)

_CODE_EXTENSIONS = frozenset({
    ".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte", ".astro",
})


def _looks_like_nav_file(path: str, text: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    if _NAV_BASENAME_RE.search(base):
        return True
    return bool(_NAV_CONTENT_RE.search(text))


def _extract_nav_strings(text: str) -> list[str]:
    """Label (+href context) pairs from a nav-looking file, in source order."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(label: str, href: str | None) -> None:
        label = label.strip()
        if not _is_human_string(label):
            return
        entry = f"{label} ({href})" if href else label
        if entry not in seen:
            seen.add(entry)
            out.append(entry)

    # Object-literal items arrays: pair each label with the nearest href
    # in a window after it (same object literal, structurally).
    for m in _NAV_LABEL_KEY_RE.finditer(text):
        if len(out) >= _MAX_STRINGS_PER_FILE:
            return out
        window = text[m.end():m.end() + 200]
        hm = _NAV_HREF_KEY_RE.search(window)
        # Also look just BEFORE the label (href may precede it).
        if hm is None:
            back = text[max(0, m.start() - 200):m.start()]
            for bm in _NAV_HREF_KEY_RE.finditer(back):
                hm = bm  # last one before the label
        _add(m.group(2), hm.group(2) if hm else None)
    # JSX link elements.
    for m in _NAV_JSX_LINK_RE.finditer(text):
        if len(out) >= _MAX_STRINGS_PER_FILE:
            return out
        _add(m.group(2), m.group(1))
    return out


# ── Route / page title detection ────────────────────────────────────────

_PAGE_BASENAMES = frozenset({
    "page", "layout", "route", "index", "_app", "_document",
})
_PAGE_DIR_HINT_RE = re.compile(r"(?:^|/)(?:app|pages|routes|views|screens)/")

_TITLE_TAG_RE = re.compile(r"<title>\s*([^<{]{2,120}?)\s*</title>", re.IGNORECASE)
_METADATA_TITLE_RE = re.compile(
    r"""\btitle\s*:\s*(['"`])((?:(?!\1).){2,120})\1""",
)
_H1_RE = re.compile(r"<h1[^>]*>\s*([^<{][^<]{1,120}?)\s*</h1>", re.DOTALL)
# export const metadata = {...} / generateMetadata — gate _METADATA_TITLE_RE
# to files that actually declare page metadata, otherwise any object with a
# ``title`` key (charts config, modals) would leak in.
_METADATA_DECL_RE = re.compile(
    r"\bmetadata\b|\bgenerateMetadata\b|<Head[\s>]|<title>",
)


def _is_page_file(path: str) -> bool:
    p = path.replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    ext = "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if ext not in _CODE_EXTENSIONS:
        return False
    return stem in _PAGE_BASENAMES or bool(_PAGE_DIR_HINT_RE.search(p))


def _extract_title_strings(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = s.strip()
        if _is_human_string(s) and s not in seen:
            seen.add(s)
            out.append(s)

    for m in _TITLE_TAG_RE.finditer(text):
        _add(m.group(1))
    if _METADATA_DECL_RE.search(text):
        for m in _METADATA_TITLE_RE.finditer(text):
            _add(m.group(2))
            if len(out) >= _MAX_STRINGS_PER_FILE:
                return out
    for m in _H1_RE.finditer(text):
        _add(m.group(1))
        if len(out) >= _MAX_STRINGS_PER_FILE:
            return out
    return out[:_MAX_STRINGS_PER_FILE]


# ── Public types ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProductString:
    """One human-facing string attached to the file it lives in."""

    text: str
    source: str  # "nav" | "title" | "i18n"
    file: str    # repo-relative path


@dataclass
class ProductStringIndex:
    """File-keyed product-string store + bundle assembler."""

    by_file: dict[str, list[ProductString]] = field(default_factory=dict)

    @property
    def total_strings(self) -> int:
        return sum(len(v) for v in self.by_file.values())

    def strings_for_file(self, path: str) -> list[ProductString]:
        return self.by_file.get(path, [])

    def bundle_for(
        self,
        paths: Iterable[str],
        *,
        anchor_paths: Iterable[str] | None = None,
        cap: int = _BUNDLE_CAP,
    ) -> list[str]:
        """Assemble an entity's evidence bundle from its member files.

        Anchor-first ordering: strings from ``anchor_paths`` come first,
        the remaining member files after; within each group strings sort
        by source priority (nav > title > i18n) then file then text.
        Deduplicated, capped at ``cap`` (structural — see module
        docstring). Deterministic for any input order.
        """
        anchors = set(anchor_paths or ())
        member = [p for p in paths if p in self.by_file]

        def _group(files: list[str]) -> list[ProductString]:
            rows: list[ProductString] = []
            for f in sorted(files):
                rows.extend(self.by_file[f])
            rows.sort(key=lambda r: (
                _SOURCE_PRIORITY.get(r.source, 9), r.file, r.text,
            ))
            return rows

        anchor_rows = _group([p for p in member if p in anchors])
        rest_rows = _group([p for p in member if p not in anchors])

        out: list[str] = []
        seen: set[str] = set()
        for row in anchor_rows + rest_rows:
            key = row.text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(row.text)
            if len(out) >= cap:
                break
        return out

    def evidence_tokens_for(self, paths: Iterable[str]) -> set[str]:
        """Lowercase word tokens of every product string on ``paths`` —
        consumed by the naming validator as evidence vocabulary."""
        tokens: set[str] = set()
        for p in paths:
            for row in self.by_file.get(p, ()):  # pragma: no branch
                for w in re.split(r"[^a-z0-9]+", row.text.lower()):
                    if w:
                        tokens.add(w)
        return tokens


# ── Collector ───────────────────────────────────────────────────────────


def _read_text(repo_path: Path, rel: str) -> str | None:
    fp = repo_path / rel
    try:
        if not fp.is_file() or fp.stat().st_size > _MAX_FILE_BYTES:
            return None
        return fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def collect_product_strings(
    repo_path: Path,
    candidate_paths: Iterable[str],
) -> ProductStringIndex:
    """Collect product strings from ``candidate_paths`` (repo-relative).

    ``candidate_paths`` is typically the union of all features' member
    files — strings only ever attach to files that BELONG to some
    entity, so scanning anything else is wasted IO. Files that match no
    structural convention are skipped without being read; prose docs
    (README & friends) are excluded BEFORE any IO. Deterministic:
    candidates are de-duplicated and processed in sorted order.
    """
    index = ProductStringIndex()
    for rel in sorted(set(candidate_paths)):
        if _is_prose_doc(rel):
            continue
        rows: list[ProductString] = []

        if _is_i18n_catalog(rel):
            text = _read_text(repo_path, rel)
            if text:
                values: list[str] = []
                if rel.lower().endswith(".json"):
                    try:
                        values_obj = json.loads(text)
                    except (json.JSONDecodeError, ValueError):
                        values_obj = None
                    if values_obj is not None:
                        _flatten_json_strings(values_obj, values)
                else:  # .ts / .js locale module
                    for m in _TS_LOCALE_PAIR_RE.finditer(text):
                        s = m.group(2).strip()
                        if _is_human_string(s):
                            values.append(s)
                        if len(values) >= _MAX_STRINGS_PER_FILE:
                            break
                rows.extend(
                    ProductString(text=v, source="i18n", file=rel)
                    for v in values[:_MAX_STRINGS_PER_FILE]
                )
        else:
            ext = "." + rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
            if ext in _CODE_EXTENSIONS:
                text = _read_text(repo_path, rel)
                if text:
                    if _looks_like_nav_file(rel, text):
                        rows.extend(
                            ProductString(text=v, source="nav", file=rel)
                            for v in _extract_nav_strings(text)
                        )
                    if _is_page_file(rel):
                        existing = {r.text for r in rows}
                        rows.extend(
                            ProductString(text=v, source="title", file=rel)
                            for v in _extract_title_strings(text)
                            if v not in existing
                        )

        if rows:
            index.by_file[rel] = rows[:_MAX_STRINGS_PER_FILE]
    return index
