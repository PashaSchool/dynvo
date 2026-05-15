"""JSX nav-component extractor (Sprint 2 / Phase 5 Layer C continuation).

Walks ``.tsx`` / ``.jsx`` files and extracts navigation links — pairs
of (URL path, human label) — from author-written nav components like
sidebars, top-bars, tab strips, menus, and navigation footers.

Why this matters: every customer-facing React/Next app encodes its
"feature map" twice. Once in the file system (route files) and once
in the navigation markup. The nav markup carries the **author-
intended LABEL** for each surface — exactly the customer-facing
phrasing the engine should produce.

  <Link href="/billing">Billing</Link>
  ──────────────────────────────────
  href = "/billing"   (route)
  label = "Billing"   (display name — what the customer sees)

That label is gold for naming / display_name canonicalisation
(Sprint 5 will consume it). It is also a high-confidence "this is a
real product surface" signal for the recall critique.

Generic per ``memory/rule-no-repo-specific-paths`` — no per-repo
filenames hardcoded. The detector identifies nav components
structurally:

  - File path or component name contains a nav-shaped token
    (sidebar, topnav, navbar, header, mainnav, tabs, menu, footer-nav, ...)
  - OR file contains >=N (default 3) ``<Link href=`` / ``<NavLink to=``
    matches per file (dense navigation markup)

Both signal kinds emit ``nav-link`` Signal objects. Aggregators
(critique) consume them as expected-category hints.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.signals import Signal


# Filename / component stems that strongly indicate a nav component.
# Lowercase, matched as substring on the file stem.
_NAV_NAME_TOKENS = frozenset({
    "sidebar", "topnav", "navbar", "mainnav", "menu",
    "tabs", "tabbar", "footer-nav", "header", "navigation",
    "tabnav", "side-nav", "top-nav", "main-nav", "primary-nav",
    "secondary-nav", "appbar", "appshell",
})

# Source extensions we walk.
_REACT_EXTS = frozenset({".tsx", ".jsx"})

_SKIP_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", ".git", "dist", "build",
    ".next", ".turbo", ".venv", "venv", "env",
    "tests", "test", "spec", "specs", "fixtures", "stories",
    "storybook-static", ".storybook",
})

# Threshold for "dense nav markup" — files with this many or more
# Link/NavLink occurrences are treated as nav components even when
# the filename doesn't include a nav token.
DENSE_LINK_THRESHOLD = 3

# Capture: <Link href="/path" ...>Label</Link>
# and    : <NavLink to="/path" ...>Label</NavLink>
# Label group captures the inner text up to the next `<` or `{`.
_LINK_RE = re.compile(
    r"""
    < (?: Link | NavLink )                      # tag
    \b
    [^>]*?                                       # other props
    \s+ (?: href | to ) \s* = \s*
    (?: "([^"]+)" | '([^']+)' )                  # 1=double, 2=single
    [^>]*?
    >
    \s*
    (?: ([^<{]+?) )?                             # 3 = inner text label
    \s*
    (?: < | \{ )
    """,
    re.VERBOSE | re.DOTALL,
)

# Companion: <a href="/path">Label</a> when inside a nav component.
_ANCHOR_RE = re.compile(
    r"""
    < a
    \b
    [^>]*?
    \s+ href \s* = \s*
    (?: "([^"]+)" | '([^']+)' )
    [^>]*?
    >
    \s*
    (?: ([^<{]+?) )?
    \s*
    (?: < | \{ )
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class NavLink:
    file: str          # repo-relative
    href: str          # the route string from the prop
    label: str         # author-provided text (may be empty when label was a child component)
    component_kind: str  # "Link" / "NavLink" / "a"


def _walkable_files(repo_root: Path) -> Iterable[Path]:
    for p in repo_root.rglob("*"):
        if not p.is_file() or p.suffix not in _REACT_EXTS:
            continue
        if any(part in _SKIP_DIR_NAMES for part in p.parts):
            continue
        yield p


def _stem_lower(p: Path) -> str:
    """Return the filename stem in lowercase (no directory)."""
    return p.stem.lower()


def _file_looks_like_nav(p: Path) -> bool:
    """Filename heuristic: stem contains a nav-shaped token."""
    stem = _stem_lower(p)
    # Replace dot/dash to make substring match work for kebab and
    # PascalCase-converted forms.
    norm = re.sub(r"[._]", "-", stem)
    return any(tok in norm for tok in _NAV_NAME_TOKENS)


def _extract_links_from_text(text: str) -> list[tuple[str, str, str]]:
    """Return list of (href, label, kind) tuples found in source text.

    Empty labels are kept — caller decides what to do with them
    (some nav components use icons or child components for the label).
    """
    out: list[tuple[str, str, str]] = []
    for m in _LINK_RE.finditer(text):
        href = m.group(1) or m.group(2) or ""
        label = _clean_label(m.group(3) or "")
        # Determine which tag matched by re-checking the char before
        # the href position. A bit ugly but robust enough.
        kind = "Link" if "Link" in text[max(0, m.start()): m.start() + 8] else "NavLink"
        out.append((href, label, kind))
    for m in _ANCHOR_RE.finditer(text):
        href = m.group(1) or m.group(2) or ""
        label = _clean_label(m.group(3) or "")
        out.append((href, label, "a"))
    return out


# Marketing / legal nav routes — these are universal site-shell
# items, not customer-facing product features. Filter them out so
# they never reach the recall critique.
_MARKETING_HREFS = frozenset({
    "privacy", "privacy-policy", "terms", "terms-of-service",
    "tos", "legal", "imprint", "cookies", "cookie-policy",
    "about", "about-us", "contact", "contact-us", "support",
    "careers", "jobs", "team", "blog", "press", "news",
    "changelog", "release-notes", "roadmap", "docs",
    "documentation", "pricing", "plans", "demo", "request-demo",
    "login", "signin", "signup", "register", "logout",
    "community", "discord", "github", "twitter", "linkedin",
    "facebook", "instagram", "youtube",
})

# Human labels that signal a marketing/legal nav item rather than
# a product surface. Compared case-insensitively.
_MARKETING_LABELS = frozenset({
    "privacy policy", "terms of service", "careers", "changelog",
    "blog", "about", "contact", "contact us", "contact sales",
    "pricing", "docs", "documentation", "read docs", "support",
    "community", "get started", "log in", "sign in", "sign up",
    "log out", "sign out", "legal", "imprint", "press",
    "feedback form", "request demo", "book a demo", "learn more",
    "company", "team", "twitter", "github", "discord",
})


def _looks_like_marketing(href: str, label: str) -> bool:
    """True iff this nav-link is a marketing / site-shell item, not
    a customer-facing product surface. Generic across SaaS sites.
    """
    # Last segment of the route, lowercased.
    seg = href.rstrip("/").rsplit("/", 1)[-1].lower()
    if seg in _MARKETING_HREFS:
        return True
    if label and label.lower() in _MARKETING_LABELS:
        return True
    return False


def _is_external_or_anchor(href: str) -> bool:
    """Skip mailto:, tel:, http(s)://, # fragments — these aren't
    in-app navigation."""
    if not href:
        return True
    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return True
    if href.startswith(("http://", "https://", "//")):
        return True
    return False


# Characters that indicate a label captured from inside a JSX
# expression / prop continuation, not real human text.
_NON_LABEL_CHARS = re.compile(r"[(){}=;\n\r]")


def _clean_label(raw: str) -> str:
    """Return label only if it looks like real human text. Returns
    empty string when the captured text is JSX expression syntax,
    multi-line continuation, or other non-human content.

    The link regex isn't a JSX parser — it sometimes captures
    fragments like ``closeMobileSidebar("left-sidebar")}\n  >`` when
    the JSX expression spans multiple lines. Filter those out so the
    aggregator only ever sees clean labels.
    """
    if not raw:
        return ""
    label = raw.strip()
    if not label:
        return ""
    # Reject anything containing JSX/code syntax characters.
    if _NON_LABEL_CHARS.search(label):
        return ""
    # Reject labels that are obviously a single var/prop name
    # (camelCase/snake_case identifiers) rather than a phrase.
    if " " not in label and label.isidentifier():
        # Single identifier could still be a real label
        # (Dashboard, Billing, Docs). Keep when first char is upper.
        if not label[0].isupper():
            return ""
    # Reasonable length cap — labels longer than ~60 chars are
    # almost certainly captured cruft.
    if len(label) > 60:
        return ""
    return label


def collect_nav_links(repo_root: Path) -> list[NavLink]:
    """Walk the repo, return nav-link records.

    A file qualifies as a nav source when:
      - Its filename matches a nav-shaped token, OR
      - It contains >= DENSE_LINK_THRESHOLD links

    Internal links only (external URLs and anchors filtered).
    """
    out: list[NavLink] = []
    for p in _walkable_files(repo_root):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "<Link" not in text and "<NavLink" not in text and "<a " not in text:
            continue

        links = _extract_links_from_text(text)
        if not links:
            continue

        is_nav_file = _file_looks_like_nav(p)
        is_dense = len(links) >= DENSE_LINK_THRESHOLD
        if not (is_nav_file or is_dense):
            continue

        rel = str(p.relative_to(repo_root))
        for href, label, kind in links:
            if _is_external_or_anchor(href):
                continue
            # Strip query/hash from href so the route is canonical.
            clean_href = href.split("?", 1)[0].split("#", 1)[0]
            if not clean_href or clean_href == "/":
                # Skip root + empty
                continue
            if _looks_like_marketing(clean_href, label):
                # Marketing / legal site shell — not a product feature.
                continue
            out.append(NavLink(
                file=rel, href=clean_href,
                label=label, component_kind=kind,
            ))
    return out


@dataclass(frozen=True, slots=True, kw_only=True)
class JsxNavExtractor:
    """Universal nav-link extractor for React/Next-shaped repos."""

    name: str = "jsx-nav-extractor"

    def applicable(self, repo_root: Path) -> bool:
        # Cheap probe — at least one .tsx/.jsx file outside skip dirs.
        for p in _walkable_files(repo_root):
            return True
        return False

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files
        links = collect_nav_links(repo_root)
        return [
            Signal(
                kind="nav-link",
                source=self.name,
                payload={
                    "file": ln.file,
                    "href": ln.href,
                    "label": ln.label,
                    "component_kind": ln.component_kind,
                },
            )
            for ln in links
        ]


__all__ = [
    "DENSE_LINK_THRESHOLD",
    "JsxNavExtractor",
    "NavLink",
    "collect_nav_links",
]
