"""Route-segment extractor (Sprint 9b).

Where ``route_file`` extractors emit ONE signal per route file (folder
or path), this extractor walks each route file and emits a finer-grained
signal per **leaf segment**. The motivation is the documenso bug:
``apps/remix/app/routes/_authenticated+/`` collapses 67 flows under a
single feature because the primary scan saw the route group as ONE
unit. Per-leaf signals let critique split it into Inbox, Documents,
Settings, Templates Page, Org Settings, etc.

Patterns supported:

  - **Remix flat-routes** — ``apps/<app>/app/routes/(<group>)/<leaf>.tsx``
    or ``apps/<app>/app/routes/<group>+/<leaf>.tsx``. The leaf is the
    last dot-separated segment of the file basename, with parameter
    placeholders (``$id``, ``$teamUrl``) stripped.

  - **Next App Router** — ``apps/<app>/app/(<group>)/<leaf>/page.tsx``
    or ``app/(<group>)/<leaf>/page.tsx``. The folder is the leaf.

  - **Next Pages Router** — ``apps/<app>/pages/<leaf>.tsx`` or
    ``pages/<leaf>.tsx``.

This extractor is generic per ``rule-no-repo-specific-paths`` — only
folder-shape rules.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from faultline.signals import Signal

logger = logging.getLogger(__name__)


# Match Remix flat-route file under apps/<app>/app/routes (with or
# without ``app/`` middle segment). Capture the FULL filename so we
# can derive the leaf in code.
_REMIX_FILE_RE = re.compile(
    r"^apps/[^/]+/(?:app/)?routes/(.+)\.(?:tsx|ts|jsx|js)$"
)

# Match Next App Router page file under apps/<app>/app/.../page.tsx OR
# top-level app/.../page.tsx. Capture path between app/ and /page.
_NEXT_APP_PAGE_RE = re.compile(
    r"^(?:apps/[^/]+/)?app/(.+)/page\.(?:tsx|ts|jsx|js)$"
)

# Match Next Pages router file under apps/<app>/pages/.../<leaf>.tsx OR
# pages/.../<leaf>.tsx (excluding _app, _document, _error).
_NEXT_PAGES_RE = re.compile(
    r"^(?:apps/[^/]+/)?pages/(.+)\.(?:tsx|ts|jsx|js)$"
)


_SKIP_DIRS = frozenset({
    "node_modules", "dist", "build", ".next", ".turbo", ".git",
    "__pycache__", ".venv", "venv", "env", "target", "vendor",
    "coverage",
})


# Skip files whose basename matches these — they are framework
# scaffolding, not features.
_NEXT_FILE_NOISE = frozenset({
    "_app", "_document", "_error", "404", "500", "loading",
    "error", "not-found", "layout", "template", "default",
    "head", "icon", "favicon", "robots", "sitemap", "manifest",
    "page",  # only when literal — handled separately
})


# Slugs to skip — generic noise / framework scaffolding.
_NOISE_SEGMENTS = frozenset({
    "_layout", "_index", "index", "layout", "template", "loading",
    "error", "not-found", "default", "head", "icon", "favicon",
    "robots", "sitemap", "manifest", "_app", "_document",
    "_error",
})


def _strip_route_param_markers(seg: str) -> str:
    """Strip Remix/Next dynamic-route markers from a segment.
    ``$teamUrl`` → ``team-url``; ``[id]`` → ``id``;
    ``$$slug`` → ``slug``.
    """
    s = seg
    s = re.sub(r"^\$\$?", "", s)
    s = re.sub(r"^\[(.*)\]$", r"\1", s)
    s = re.sub(r"^\(.*\)$", "", s)  # Next route group like (dashboard)
    s = s.replace("+", "")            # Remix flat-routes folder marker
    # Convert camelCase to kebab-case for human readability
    s = re.sub(r"(.)([A-Z])", r"\1-\2", s).lower()
    return s


def _remix_leaf_from_file(captured: str) -> str | None:
    """Given a Remix routes-relative path like
    ``_authenticated+/t.$teamUrl+/documents._index``, return a
    human-readable leaf slug.
    """
    # Take basename (after last "/")
    base = captured.rsplit("/", 1)[-1]
    # Drop trailing ``._index`` / ``.index`` — those are layout indices
    base = re.sub(r"\.(?:_)?index$", "", base)
    # Take last dot-segment as the leaf
    parts = base.split(".")
    # Walk from the right, take the first non-noise non-empty segment
    for piece in reversed(parts):
        cleaned = _strip_route_param_markers(piece)
        if cleaned and cleaned not in _NOISE_SEGMENTS:
            return cleaned
    return None


def _next_leaf_from_path(captured: str) -> str | None:
    """For Next App Router ``(<group>)/<leaf>`` returns ``<leaf>``."""
    parts = captured.split("/")
    for piece in reversed(parts):
        cleaned = _strip_route_param_markers(piece)
        if cleaned and cleaned not in _NOISE_SEGMENTS:
            return cleaned
    return None


def _walk(repo_root: Path):
    import os
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        yield root, dirs, files


def _walk_route_files(repo_root: Path):
    for root, _dirs, files in _walk(repo_root):
        for fn in files:
            if not fn.endswith((".tsx", ".ts", ".jsx", ".js")):
                continue
            try:
                rel = str((Path(root) / fn).relative_to(repo_root))
            except ValueError:
                continue
            yield rel


# ── Extractor ───────────────────────────────────────────────────────


class RouteSegmentExtractor:
    """Emits one ``route-segment`` signal per route-file leaf."""

    name = "route-segment-extractor"

    def applicable(self, repo_root: Path) -> bool:
        # Cheap check — does the repo have any apps/*/routes/ or
        # app/ or pages/ folder?
        for marker in ("apps", "app", "pages"):
            p = repo_root / marker
            if p.is_dir():
                return True
        return False

    def extract(self, repo_root: Path, files=()) -> list[Signal]:
        _ = files
        out: list[Signal] = []
        seen: set[str] = set()
        for rel in _walk_route_files(repo_root):
            slug: str | None = None
            label = "remix-leaf"

            m = _REMIX_FILE_RE.match(rel)
            if m:
                slug = _remix_leaf_from_file(m.group(1))
            else:
                m = _NEXT_APP_PAGE_RE.match(rel)
                if m:
                    slug = _next_leaf_from_path(m.group(1))
                    label = "next-app-page"
                else:
                    m = _NEXT_PAGES_RE.match(rel)
                    if m:
                        captured = m.group(1)
                        # skip framework scaffolding files
                        base = captured.rsplit("/", 1)[-1]
                        if base in _NEXT_FILE_NOISE:
                            continue
                        slug = _next_leaf_from_path(captured)
                        label = "next-pages-leaf"
            if not slug:
                continue
            key = slug.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Signal(
                    kind="route-segment",
                    source=self.name,
                    payload={"file": rel, "slug": slug, "match_kind": label},
                ),
            )
        return out


__all__ = ["RouteSegmentExtractor"]
