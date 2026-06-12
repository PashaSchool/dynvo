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
    "NavTaxonomyEntry",
    "ProductString",
    "ProductStringIndex",
    "build_nav_taxonomy",
    "collect_product_strings",
    "normalize_href",
    "route_path_for_file",
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
# Also accepts i18n tagged-template macro labels (lingui / i18next
# convention): ``label: msg`Documents``` / ``label: t`Templates``` —
# the label is still the author's literal nav copy, one macro deeper.
# Macro labels must be static (no ``${}`` interpolation → no ``$``).
_NAV_LABEL_KEY_RE = re.compile(
    r"""\b(?:label|title|name|text)\s*:\s*"""
    r"""(?:(['"])((?:(?!\1).){2,80})\1"""
    r"""|(?:msg|t|_)\s*`([^`$]{2,80})`)""",
)
# Quoted hrefs + template-literal hrefs (``href: `/t/${teamUrl}/docs```)
# — dynamic ``${}`` segments are dropped by ``normalize_href``,
# mirroring how ``route_path_for_file`` drops ``$param`` segments.
_NAV_HREF_KEY_RE = re.compile(
    r"""\b(?:href|path|to|url|route)\s*:\s*"""
    r"""(?:(['"])((?:(?!\1).){1,160})\1"""
    r"""|`([^`]{1,160})`)""",
)
# <Link href="/x">Label</Link> and <a href="/x">Label</a>; react-router
# links use ``to=`` instead of ``href=``.
_NAV_JSX_LINK_RE = re.compile(
    r"""<(?:Link|a|NavLink)\b[^>]*\b(?:href|to)=\{?["']([^"'}]+)["']\}?[^>]*>\s*([^<{][^<]{1,80}?)\s*<""",
    re.DOTALL,
)
# i18n-wrapped link labels: <Link href="/x"><Trans>Label</Trans></Link>
# (lingui / react-i18next convention — the label is still the author's
# literal nav copy, one JSX wrapper deeper). Decorative wrappers
# (icons, <Button>) may sit between the link and its <Trans> — allow a
# bounded gap that never crosses into the NEXT link or past the
# closing tag.
_NAV_JSX_LINK_TRANS_RE = re.compile(
    r"""<(?:Link|a|NavLink)\b[^>]*\b(?:href|to)=\{?["']([^"'}]+)["']\}?[^>]*>"""
    r"""(?:(?!</?(?:Link|a|NavLink)\b|<Trans).){0,400}?"""
    r"""<(?:Trans|T)\b[^>]*>\s*([^<{][^<]{1,80}?)\s*</""",
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


def _extract_nav_pairs(text: str) -> list[tuple[str, str | None]]:
    """``(label, href)`` pairs from a nav-looking file, in source order.

    The structured form feeds :func:`build_nav_taxonomy`; the legacy
    string form (``"Label (href)"``) is derived from it for the
    product-string bundles.
    """
    out: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()

    def _add(label: str, href: str | None) -> None:
        label = label.strip()
        if not _is_human_string(label):
            return
        pair = (label, href)
        if pair not in seen:
            seen.add(pair)
            out.append(pair)

    # Mask template interpolations so `}` stops being ambiguous between
    # "end of object literal" and "end of ${…}". The placeholder keeps
    # a `$` so ``normalize_href`` drops the segment as dynamic.
    masked = re.sub(r"\$\{[^}]*\}", "$_", text)

    # Object-literal items arrays: pair each label with the nearest
    # href INSIDE the same object literal — windows are bounded by the
    # enclosing braces so neighbouring items' hrefs can't be claimed.
    for m in _NAV_LABEL_KEY_RE.finditer(masked):
        if len(out) >= _MAX_STRINGS_PER_FILE:
            return out
        brace_end = masked.find("}", m.end())
        fwd_end = min(
            m.end() + 200, brace_end if brace_end >= 0 else len(masked),
        )
        hm = _NAV_HREF_KEY_RE.search(masked[m.end():fwd_end])
        # Also look just BEFORE the label (href may precede it).
        if hm is None:
            brace_start = masked.rfind("{", 0, m.start())
            back_start = max(
                m.start() - 200, brace_start + 1 if brace_start >= 0 else 0,
            )
            for bm in _NAV_HREF_KEY_RE.finditer(masked[back_start:m.start()]):
                hm = bm  # last one before the label
        label = m.group(2) if m.group(2) is not None else m.group(3)
        href = None
        if hm is not None:
            href = hm.group(2) if hm.group(2) is not None else hm.group(3)
        _add(label, href)
    # JSX link elements (plain + i18n-wrapped labels).
    for rx in (_NAV_JSX_LINK_RE, _NAV_JSX_LINK_TRANS_RE):
        for m in rx.finditer(text):
            if len(out) >= _MAX_STRINGS_PER_FILE:
                return out
            _add(m.group(2), m.group(1))
    return out


def _extract_nav_strings(text: str) -> list[str]:
    """Label (+href context) strings from a nav-looking file — the legacy
    bundle form, derived from :func:`_extract_nav_pairs`."""
    out: list[str] = []
    seen: set[str] = set()
    for label, href in _extract_nav_pairs(text):
        entry = f"{label} ({href})" if href else label
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
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
    # ``(label, href)`` pairs per nav-looking file — the structured raw
    # material for :func:`build_nav_taxonomy`. Kept separate from
    # ``by_file`` so the bundle format is unchanged.
    nav_pairs_by_file: dict[str, list[tuple[str, str | None]]] = field(
        default_factory=dict,
    )

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
                        pairs = _extract_nav_pairs(text)
                        if pairs:
                            index.nav_pairs_by_file[rel] = pairs
                        seen_nav: set[str] = set()
                        for label, href in pairs:
                            entry = (
                                f"{label} ({href})" if href else label
                            )
                            if entry not in seen_nav:
                                seen_nav.add(entry)
                                rows.append(ProductString(
                                    text=entry, source="nav", file=rel,
                                ))
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


# ── Nav taxonomy — the vendor's own product framing, in-repo ────────────
#
# The nav/sidebar registry + the route hierarchy ARE the maintainer's
# declared list of user-facing surfaces. ``build_nav_taxonomy`` projects
# the collected nav pairs (+ top-level route segments) into an ordered,
# hierarchical taxonomy that Layer 2 matches dev-feature clusters
# against (see ``nav_taxonomy.py``). Deterministic, $0, NO README.

# Flattened entry cap — structural: matches the Stage 8 analyst payload
# budget for workspace packages (``_MAX_WORKSPACE_PACKAGES`` = 60). A
# product's primary nav rarely declares more distinct destinations;
# beyond that we are reading a data dump, not a navigation surface.
_MAX_TAXONOMY_ENTRIES = 60

# Universal app-chrome labels that never name a product capability.
# SHORT, documented, universal-English — NOT corpus-tuned. Grouped by
# why each is generic:
_GENERIC_NAV_LABELS = frozenset({
    # Landing chrome — every app shell has a start surface; the label
    # carries zero product information.
    "home", "dashboard", "overview", "getting started", "get started",
    # Account/settings chrome — universal containers (their CHILDREN,
    # e.g. "Billing", are kept; only the container label is generic).
    "settings", "preferences", "account", "profile", "my account",
    # Support chrome.
    "help", "support", "faq", "feedback", "contact", "contact us",
    # Auth ACTIONS (the auth capability is detected from code, not from
    # the sign-in button).
    "login", "log in", "logout", "log out", "sign in", "sign up",
    "sign out", "register",
    # Marketing-site chrome (legal/company pages, not capabilities).
    "about", "blog", "careers", "pricing", "terms", "privacy", "legal",
    "terms of service", "privacy policy",
    # Docs chrome.
    "docs", "documentation", "changelog",
    # Pure UI affordances.
    "back", "menu", "more", "close", "open",
    # CTA verbs.
    "learn more", "upgrade", "download",
})

# File stems that denote the routing-file itself, not a path segment.
_ROUTE_FILE_STEMS = frozenset({
    "page", "route", "layout", "index", "_app", "_document", "template",
    "loading", "error", "not-found",
})

# Directory names that root a file-system router. Mirrors
# ``_PAGE_DIR_HINT_RE`` (same conventions: Next App/Pages, Remix,
# generic views/screens).
_ROUTING_ROOT_SEGMENTS = frozenset({"app", "pages", "routes"})


def normalize_href(href: str) -> str | None:
    """Normalize an internal nav href for route matching.

    Returns ``None`` for external / anchor / fully-dynamic hrefs.
    Strips query + hash + trailing slash, lowercases. Dynamic
    template-literal segments (``/t/${teamUrl}/documents`` →
    ``/t/documents``) are dropped — the same normalization
    :func:`route_path_for_file` applies to ``$param`` route segments,
    so nav hrefs and file-system routes stay comparable.
    """
    h = href.strip()
    if not h.startswith("/"):
        return None
    h = h.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if "$" in h or "{" in h:
        # Drop dynamic segments; a href with NO concrete segment left
        # carries no routing information.
        concrete = [s for s in h.split("/") if s and "$" not in s and "{" not in s]
        if not concrete:
            return None
        h = "/" + "/".join(concrete)
    if not h:
        h = "/"
    return h.lower()


def route_path_for_file(path: str) -> str | None:
    """Derive the URL route a file-system-routed page serves.

    ``apps/web/src/app/(dashboard)/documents/[id]/page.tsx`` →
    ``/documents/[id]``;  ``pages/settings/billing.tsx`` →
    ``/settings/billing``. Returns ``None`` for files outside a
    routing root. Route groups ``(x)`` and parallel slots ``@x`` are
    stripped (organizational, not part of the URL). Deterministic,
    path-only — no IO.
    """
    p = path.replace("\\", "/").lstrip("./")
    segs = p.split("/")
    if len(segs) < 2:
        return None
    base = segs[-1]
    if "." not in base:
        return None
    stem, _, ext = base.rpartition(".")
    if ext.lower() not in ("tsx", "jsx", "ts", "js", "vue", "svelte", "astro"):
        return None
    if any(
        part in ("test", "spec", "stories", "d", "config")
        for part in stem.lower().split(".")
    ):
        return None  # page.test.tsx / foo.spec.ts — not pages
    # LAST routing-root directory wins (``apps/web/app/...``).
    root_idx = max(
        (i for i, s in enumerate(segs[:-1]) if s in _ROUTING_ROOT_SEGMENTS),
        default=-1,
    )
    if root_idx < 0:
        return None

    def _expand(seg: str) -> list[str]:
        """Normalize one path segment to URL parts.

        Handles Remix flat-routes conventions on top of Next's:
        ``admin+`` → ``admin`` (folder ``+`` suffix), dotted segments
        are path separators (``sign.$token`` → ``sign`` / ``$token``),
        ``$param`` parts are dynamic and dropped, ``(group)`` /
        ``@slot`` / ``_layout`` parts are organizational.
        """
        out: list[str] = []
        for part in seg.split("."):
            part = part.removesuffix("+")
            if (
                not part
                or (part.startswith("(") and part.endswith(")"))
                or part.startswith(("@", "_", "$"))
            ):
                continue
            out.append(part)
        return out

    dir_segs: list[str] = []
    for s in segs[root_idx + 1:-1]:
        dir_segs.extend(_expand(s))
    if stem not in _ROUTE_FILE_STEMS:
        dir_segs.extend(_expand(stem))
    return "/" + "/".join(dir_segs).lower()


@dataclass(frozen=True)
class NavTaxonomyEntry:
    """One vendor-declared product surface: nav label + destination."""

    label: str
    href: str | None          # normalized internal href, or None
    source_file: str          # repo-relative file declaring the entry
    source: str               # "nav" | "route"
    children: tuple["NavTaxonomyEntry", ...] = ()

    def flatten(self) -> list["NavTaxonomyEntry"]:
        out: list[NavTaxonomyEntry] = [self]
        for c in self.children:
            out.extend(c.flatten())
        return out


def _is_generic_label(label: str) -> bool:
    return label.strip().lower() in _GENERIC_NAV_LABELS


def _nest_entries(
    flat: list[NavTaxonomyEntry],
) -> list[NavTaxonomyEntry]:
    """Nest entries whose href sits under another entry's href.

    Stable: input order is preserved within each level. Entries without
    an href stay top-level.
    """
    with_href = [e for e in flat if e.href]
    children_of: dict[int, list[NavTaxonomyEntry]] = {}
    child_ids: set[int] = set()
    for e in flat:
        if not e.href:
            continue
        # Longest strict-prefix parent (segment-wise).
        best: NavTaxonomyEntry | None = None
        for cand in with_href:
            if cand is e or not cand.href:
                continue
            if e.href.startswith(cand.href + "/"):
                if best is None or len(cand.href) > len(best.href or ""):
                    best = cand
        if best is not None:
            children_of.setdefault(id(best), []).append(e)
            child_ids.add(id(e))

    def _materialize(e: NavTaxonomyEntry) -> NavTaxonomyEntry:
        kids = children_of.get(id(e), [])
        if not kids:
            return e
        return NavTaxonomyEntry(
            label=e.label,
            href=e.href,
            source_file=e.source_file,
            source=e.source,
            children=tuple(_materialize(k) for k in kids),
        )

    return [_materialize(e) for e in flat if id(e) not in child_ids]


def build_nav_taxonomy(
    index: ProductStringIndex,
    candidate_paths: Iterable[str],
) -> list[NavTaxonomyEntry]:
    """The repo's vendor-declared product taxonomy, ordered + nested.

    Sources, in trust order:

      1. nav/sidebar registries (``index.nav_pairs_by_file``) — explicit
         label + href pairs, the author's own product list.
      2. route hierarchy — top-level route segments from file-system
         routed page files in ``candidate_paths``; fills surfaces the
         nav doesn't link (API namespaces, embeds).

    Quality guards: generic app-chrome labels dropped
    (``_GENERIC_NAV_LABELS``, documented), external/anchor hrefs
    ignored, dedupe by label (case-insensitive; nav beats route),
    flattened size capped structurally at ``_MAX_TAXONOMY_ENTRIES``.
    Deterministic for any input order. Hierarchy: entries whose href
    nests under another entry's href become its children.
    """
    flat: list[NavTaxonomyEntry] = []
    seen_labels: set[str] = set()
    seen_hrefs: set[str] = set()

    def _add(label: str, href: str | None, source_file: str, source: str) -> None:
        if len(flat) >= _MAX_TAXONOMY_ENTRIES:
            return
        label = label.strip()
        if not _is_human_string(label) or _is_generic_label(label):
            return
        key = label.lower()
        if key in seen_labels:
            return
        if href is not None and href in seen_hrefs:
            return
        seen_labels.add(key)
        if href is not None:
            seen_hrefs.add(href)
        flat.append(NavTaxonomyEntry(
            label=label, href=href, source_file=source_file, source=source,
        ))

    # 1. Nav registries — sorted by file for determinism, source order
    #    within each file.
    for rel in sorted(index.nav_pairs_by_file):
        for label, raw_href in index.nav_pairs_by_file[rel]:
            href = normalize_href(raw_href) if raw_href else None
            if raw_href is not None and href is None:
                continue  # external / anchor link — not a product surface
            _add(label, href, rel, "nav")

    # 2. Route hierarchy — top-level route segments not already covered
    #    by a nav href. One entry per first segment; representative
    #    source file is the lexicographically-first page producing it.
    nav_first_segments = {
        (e.href or "/").split("/")[1]
        for e in flat
        if e.href and e.href != "/"
    }
    route_reps: dict[str, str] = {}
    route_counts: dict[str, int] = {}
    for rel in sorted(set(candidate_paths)):
        route = route_path_for_file(rel)
        if not route or route == "/":
            continue
        seg = route.split("/")[1]
        if not seg or seg.startswith("[") or seg in nav_first_segments:
            continue
        route_counts[seg] = route_counts.get(seg, 0) + 1
        route_reps.setdefault(seg, rel)
    # Structural guard: a product SURFACE in the route tree spans
    # multiple pages; a single-page top-level segment is a leaf action
    # (``/upload``) or a one-off page (``/terms``), not a vendor-
    # declared feature. Minimum plural (≥2) — not corpus-tuned.
    route_reps = {
        seg: rel for seg, rel in route_reps.items()
        if route_counts.get(seg, 0) >= 2
    }
    for seg in sorted(route_reps):
        label = " ".join(
            w.capitalize() for w in re.split(r"[-_]+", seg) if w
        )
        _add(label, "/" + seg, route_reps[seg], "route")

    return _nest_entries(flat)
