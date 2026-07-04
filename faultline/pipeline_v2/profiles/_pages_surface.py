"""Shared Next.js Pages-Router surface machinery (helper tier).

Extracted from ``next_pages_react.py`` (MISSION-92 recall-at-depth fix 2)
so that BOTH Next profiles can consume the same deterministic
Pages-Router index without cross-profile imports (the G2 lint):

* :class:`NextPagesReactProfile` — its whole pages surface;
* ``NextAppRouterProfile`` — HYBRID trees. Next routes ``pages/``
  alongside ``app/`` whenever a package carries both (app-over-pages
  precedence applies per-conflicting-route, not per-repo), so a repo
  the App Router profile wins can still ship its dominant surface under
  ``pages/`` (the supabase-studio class: a vestigial ``app/`` dir +
  178 ``pages/**`` screens → 0 routes extracted before this fix).

Everything here is framework convention (Next docs), never a corpus
repo's paths — see ``rule-no-repo-specific-paths`` /
``rule-no-magic-tuning``. Deterministic — NO LLM, NO network.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._util import posix, read_text
from faultline.pipeline_v2.extractors.route import (
    _emit_for_fs_routing,
    _load_routing_tables,
)
from faultline.pipeline_v2.profiles.base import FlowEntry

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── framework constants (Next docs, not tuned numbers) ──────────────────────

#: App-shell filename stems (first dot-token). Next mounts these around
#: every page — framework wiring, never a capability. ``404``/``500``
#: are the convention error pages.
_SHELL_STEMS = frozenset({"_app", "_document", "_error", "404", "500"})

#: Filename stems that qualify a ``pages/`` tree as a REAL Pages Router
#: (detection only): the shell files and the root index page. A bare
#: folder of page-like components without any of these is not enough
#: evidence to claim the repo (structural confirmation, the litestar
#: lesson).
_PAGES_MARKER_STEMS = frozenset({"_app", "_document", "index"})

#: JS/TS source extensions for router files / page entries.
_JS_EXTS = (".tsx", ".jsx", ".ts", ".js", ".mjs", ".cjs")

#: Path segments that never host routing evidence: vendored trees,
#: docs/example scaffolding, build output, tests. Ecosystem names, not
#: corpus paths. ``example`` matches as a PREFIX (``examples/``,
#: ``example-apps/`` — the ecosystem uses both spellings).
_EXCLUDED_SEGMENTS = frozenset({
    "node_modules", "dist", "build", "out", ".next",
    "docs", "doc", "samples", "sample", "fixtures",
    "__tests__", "test", "tests", "e2e", "cypress", "playwright",
    "storybook", ".storybook",
})
_EXCLUDED_SEGMENT_PREFIXES = ("example",)


# ── small helpers ────────────────────────────────────────────────────────────


def _segments(path: str) -> tuple[list[str], str]:
    p = posix(path)
    if "/" in p:
        head, fname = p.rsplit("/", 1)
        return head.split("/"), fname
    return [], p


def _is_excluded_path(path: str) -> bool:
    segs, _fname = _segments(posix(path).lower())
    for seg in segs:
        if seg in _EXCLUDED_SEGMENTS:
            return True
        if seg.startswith(_EXCLUDED_SEGMENT_PREFIXES):
            return True
    return False


def _first_dot_token(fname: str) -> str:
    return fname.split(".", 1)[0]


def _is_js_source(path: str) -> bool:
    return posix(path).lower().endswith(_JS_EXTS)


def _pages_suffixes() -> tuple[str, ...]:
    """The Pages-Router page suffixes from the packaged stack YAML —
    single source of truth shared with the stock route extractor."""
    return _load_routing_tables()[0]["next-pages"][1]


# ── pages-root discovery + bucket index ──────────────────────────────────────


def _pages_roots(tracked: list[str]) -> list[str]:
    """Every accepted Pages-Router routing root in the tree (sorted).

    A root is a ``pages`` directory segment whose parent is a package
    root — structurally: the prefix before ``pages/`` is empty, ends in
    ``src``, or hosts a tracked ``package.json`` (Next only routes
    ``<root>/pages`` / ``<root>/src/pages``; a directory merely NAMED
    ``pages`` deeper in the source tree — ``lib/pages/`` — is not a
    router). Excluded/vendored segments never host roots.
    """
    tracked_set = frozenset(tracked)
    roots: set[str] = set()
    for f in tracked:
        p = posix(f)
        if not _is_js_source(p) or _is_excluded_path(p):
            continue
        segs, _fname = _segments(p)
        for i, seg in enumerate(segs):
            if seg != "pages":
                continue
            prefix = "/".join(segs[:i])
            ok = (
                not prefix
                or segs[i - 1] == "src"
                or f"{prefix}/package.json" in tracked_set
            )
            if ok:
                roots.add((prefix + "/" if prefix else "") + "pages/")
            break  # only the first ``pages`` segment can be the router
    return sorted(roots)


class _PagesIndex:
    """Deterministic index of the Pages-Router surface.

    ``roots`` — accepted routing roots; ``buckets`` — slug → sorted
    routing files (the EXACT computation the profile's route extractor
    emits, so ``feature_of`` aligns byte-for-byte); ``owned`` — routing
    file → slug; shell files are indexed separately (never owned, never
    a capability).
    """

    def __init__(self, ctx: "ScanContext") -> None:
        tracked = [posix(f) for f in ctx.tracked_files]
        self.roots: tuple[str, ...] = tuple(_pages_roots(tracked))
        suffixes = _pages_suffixes()

        self.shell_files: set[str] = set()
        routable: list[str] = []
        for f in tracked:
            root = self._root_of(f)
            if root is None:
                continue
            if not f.endswith(suffixes):
                continue
            _segs, fname = _segments(f)
            if _first_dot_token(fname) in _SHELL_STEMS:
                self.shell_files.add(f)
                continue
            routable.append(f)

        raw = _emit_for_fs_routing(routable, self.roots, suffixes)
        self.buckets: dict[str, tuple[str, ...]] = {
            slug: tuple(sorted(set(paths)))
            for slug, paths in sorted(raw.items())
        }
        self.owned: dict[str, str] = {
            p: slug for slug, paths in self.buckets.items() for p in paths
        }
        self.routable: tuple[str, ...] = tuple(sorted(routable))

    def _root_of(self, path: str) -> str | None:
        for root in self.roots:
            if path.startswith(root):
                return root
        return None

    def rest_of(self, path: str) -> str | None:
        """Path relative to its routing root, or ``None``."""
        root = self._root_of(path)
        return path[len(root):] if root else None

    def marker_roots(self) -> list[str]:
        """Roots showing a Pages-Router MARKER file (detection grade).

        Excluded/vendored roots never qualify; a root whose package
        also hosts a real App Router tree is disqualified (Next's own
        app-over-pages precedence — those repos belong to the
        ``next-app-router`` profile)."""
        qualified: list[str] = []
        for root in self.roots:
            if _is_excluded_path(root + "x"):
                continue
            for f in list(self.shell_files) + list(self.routable):
                if not f.startswith(root):
                    continue
                _segs, fname = _segments(f)
                if _first_dot_token(fname) in _PAGES_MARKER_STEMS:
                    qualified.append(root)
                    break
        return qualified


# ── URL derivation + flow entries ────────────────────────────────────────────


def url_from_rest(rest: str) -> tuple[str, bool]:
    """URL pattern for a path under a pages root (+ api flag).

    ``index`` maps to the parent segment; dynamic ``[x]`` /
    ``[...x]`` / ``[[...x]]`` become ``:x``; a trailing ``page``
    dot-token (the Next ``pageExtensions`` colocation convention)
    is not part of the URL.
    """
    parts = rest.split("/")
    fname = parts.pop()
    stem = fname
    for ext in _JS_EXTS:
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    stem_tokens = [t for t in stem.split(".") if t]
    if stem_tokens and stem_tokens[-1] == "page":
        stem_tokens.pop()
    stem = ".".join(stem_tokens)
    segs = [s for s in parts if s]
    if stem and stem != "index":
        segs.append(stem)
    out: list[str] = []
    for seg in segs:
        if seg.startswith("[") and seg.endswith("]"):
            out.append(":" + seg.strip("[]").lstrip("."))
        else:
            out.append(seg)
    is_api = bool(segs) and segs[0] == "api"
    return "/" + "/".join(out), is_api


def default_export_symbol(repo_root, rel_path: str) -> str:  # noqa: ANN001
    """Best-effort default-export component name (else ``""``)."""
    text = read_text(repo_root / rel_path)
    if not text:
        return ""
    m = re.search(
        r"export\s+default\s+(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
        text,
    )
    if m:
        return m.group(1)
    m = re.search(r"export\s+default\s+([A-Za-z_$][\w$]*)\s*;?", text)
    if m and m.group(1) not in ("function", "async", "class"):
        return m.group(1)
    return ""


def pages_flow_entries(ctx: "ScanContext", pages: _PagesIndex) -> list[FlowEntry]:
    """One :class:`FlowEntry` per routed Pages-Router file.

    Files under a root's ``api/`` segment are kind ``http``; the rest
    are kind ``page``. Shell files never reach ``pages.routable`` (the
    app-shell rule), so they seed nothing.
    """
    entries: list[FlowEntry] = []
    for f in pages.routable:
        rest = pages.rest_of(f)
        if rest is None:
            continue
        url, is_api = url_from_rest(rest)
        entries.append(FlowEntry(
            path=f,
            symbol=default_export_symbol(ctx.repo_path, f),
            kind="http" if is_api else "page",
            route=url,
        ))
    return entries


__all__ = [
    "_PagesIndex",
    "_pages_roots",
    "default_export_symbol",
    "pages_flow_entries",
    "url_from_rest",
]
