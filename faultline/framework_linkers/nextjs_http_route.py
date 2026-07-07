"""Next.js HTTP route linker — Stage 6.4 v1 (Sprint C4).

What it links
=============

For Next.js App Router (``app/api/**/route.{ts,tsx,js,jsx,mts}``) and
Next.js Pages Router (``pages/api/**/*.{ts,js}``) repos, this linker
resolves runtime fetch / axios / SWR call sites in client code to the
corresponding server-side route handler file + HTTP method symbol.

Pipeline gap closed
===================

The C3 whole-import-tree (Sprint C3) walks JS/TS ``import`` statements
— it cannot resolve URL strings. A page that calls
``fetch("/api/rules", {method: "POST", body: ...})`` shares no symbol
with ``app/api/rules/route.ts`` so the import graph stops at the page.
This linker bridges that gap with deterministic URL → file resolution.

Algorithm
=========

1. **Build route map (per-scan cached):**
   * Glob ``**/app/api/**/route.{ts,tsx,js,jsx,mts}`` and
     ``**/pages/api/**/*.{ts,js}`` under repo root.
   * Convert each file path to a URL pattern:
     - Strip the ``app/`` (or ``apps/<ws>/app/`` etc.) prefix and the
       trailing ``/route.<ext>`` (or for ``pages/api/``, strip the
       ``pages/`` prefix and the file extension).
     - Translate Next dynamic segments:
       * ``[foo]`` → ``[^/]+``
       * ``[...foo]`` (catchall) → ``.*``
       * ``[[...foo]]`` (optional catchall) → ``(?:.*)?``
   * Compile each pattern (anchored ``^/api/...$``) into a regex.
   * Store ``{regex: route_file_path}``.

2. **Per feature**: read every file reachable from
   ``feature.symbol_attributions`` (the C3-enriched surface) — these
   are the client-side files that may contain fetch calls. Scan each
   file's text with regexes for the fetch-style call patterns and
   collect ``(url, line)`` pairs.

3. **Normalise each captured URL**:
   * Strip ``https?://[^/]+`` prefix (external host → skip).
   * Replace ``${...}`` template-literal slots with ``[^/]+``.
   * Drop trailing ``?query`` and ``#fragment``.
   * Only keep URLs starting with ``/api/`` after normalisation.

4. **Match normalised URL against the route map**. For each match,
   emit a :class:`FrameworkLink` with the right confidence:
   * ``1.0`` — pure literal URL match (no ``${var}`` in original).
   * ``0.7`` — at least one ``${var}`` was replaced.

5. **Resolve target symbol**: read the route file once (cached) and
   pick the exported HTTP method handler whose verb matches the
   caller's ``method:`` option (when detectable) or — when ambiguous —
   the first verb in priority order ``GET, POST, PUT, PATCH, DELETE``.
   Fall back to ``"<route>"`` with the whole-file line range when no
   exported verb is found.

Determinism
===========

Pure file IO + regex. NO LLM. NO network. Same code → same links.
Scale-invariant: thresholds are activation gates ("does this repo
have any ``app/api/`` files?"), never tuned magic numbers.

Failure modes
=============

* No ``app/api/`` or ``pages/api/`` files → ``is_active`` returns
  False (no work done, telemetry records "skipped: no route files").
* Route file is unreadable → that file is dropped from the route map
  with a debug log; other routes still resolve.
* Caller file is binary / unreadable → skipped silently (telemetry
  ``files_unreadable``).
* No fetch-style calls in a caller file → 0 links from that file.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.framework_linkers.base import FrameworkLink, canonical_sample

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Activation gates ────────────────────────────────────────────────────────

# Audited-stack prefixes we react to. ``next-`` covers ``next-app-router``,
# ``next-pages``, ``next-monorepo``.
_NEXT_AUDITED_PREFIXES: tuple[str, ...] = ("next-", "next")
_NEXT_DEPENDENCY_PATTERNS: tuple[str, ...] = ("next",)


# ── Regex library ───────────────────────────────────────────────────────────

# Fetch-style call patterns. Each pattern MUST capture the URL string
# literal in the FIRST group. We support both single, double, and
# template-literal quoting (the latter without the leading backtick so
# we can keep one regex per pattern).
#
# Important: these are deliberately permissive — false negatives on a
# wild URL idiom are preferable to false positives that pollute the
# link stream. The post-match normalisation step rejects anything that
# doesn't start with /api/.
_FETCH_CALL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"""\bfetch\s*\(\s*['"`]([^'"`]+)['"`]"""),
    re.compile(r"""\baxios\.(?:get|post|put|patch|delete|head|options|request)\s*\(\s*['"`]([^'"`]+)['"`]"""),
    re.compile(r"""\bdelete\(\s*['"`]([^'"`]+)['"`]\s*,\s*\{\s*['"]?method['"]?\s*:"""),  # rare: delete("/api/x", {method: ...})
    re.compile(r"""\buseSWR(?:Mutation|Immutable)?\s*\(\s*['"`]([^'"`]+)['"`]"""),
    re.compile(r"""\buseQuery\s*\(\s*['"`]([^'"`]+)['"`]"""),
    re.compile(r"""\buseMutation\s*\(\s*['"`]([^'"`]+)['"`]"""),
    # axios({url: '/api/...'})
    re.compile(r"""\baxios\s*\(\s*\{\s*['"]?url['"]?\s*:\s*['"`]([^'"`]+)['"`]"""),
)

# Optional method extraction — we look for `method: "POST"` within
# the SAME call expression (the next ~200 chars after the URL). This
# is best-effort; missing method = we use the first available verb.
_METHOD_NEAR_URL = re.compile(
    r"""['"]?method['"]?\s*:\s*['"`]([A-Z]+)['"`]""",
)

# Next dynamic-segment patterns (in route file paths). Order matters —
# optional catchall must be tried BEFORE catchall (longer specificity).
_DYN_OPTIONAL_CATCHALL = re.compile(r"\[\[\.\.\.[^\]]+\]\]")
_DYN_CATCHALL = re.compile(r"\[\.\.\.[^\]]+\]")
_DYN_SINGLE = re.compile(r"\[[^\]\[]+\]")

# Template-literal slot inside a URL: replace ${...} (possibly nested
# braces of one level deep) with a placeholder for normalisation.
_TPL_SLOT = re.compile(r"\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")

# Drop URL trailing query / fragment.
_URL_QUERY = re.compile(r"[?#].*$")

# External URL prefix.
_EXTERNAL_URL = re.compile(r"^https?://[^/]+")

# HTTP method exports inside a route file. Matches:
#   export async function GET(...)
#   export function POST(...)
#   export const PATCH = ...
#   export const DELETE = withError(...)
#   export const dynamic = ... (NOT a method — filtered by verb list)
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_METHOD_EXPORT = re.compile(
    r"""^\s*export\s+(?:async\s+)?(?:function|const|let|var)\s+([A-Z]+)\b""",
    re.MULTILINE,
)


# ── Internal types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _CompiledRoute:
    """One route handler with its compiled URL regex."""

    regex: re.Pattern[str]
    file: str           # repo-relative POSIX
    raw_pattern: str    # the human-readable URL pattern (for logging)


@dataclass
class _LinkerTelemetry:
    """Per-scan counters surfaced into the Stage 6.4 artifact."""

    route_map_size: int = 0
    features_processed: int = 0
    files_scanned: int = 0
    files_unreadable: int = 0
    fetch_urls_found: int = 0
    urls_matched: int = 0
    urls_unmatched: int = 0
    urls_external_skipped: int = 0
    links_emitted: int = 0
    # Appended (uncapped) from Stage 6.4 worker threads; the emission cap
    # + canonical ordering live in ``as_dict`` (see base.canonical_sample).
    unmatched_sample: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "route_map_size": self.route_map_size,
            "features_processed": self.features_processed,
            "files_scanned": self.files_scanned,
            "files_unreadable": self.files_unreadable,
            "fetch_urls_found": self.fetch_urls_found,
            "urls_matched": self.urls_matched,
            "urls_unmatched": self.urls_unmatched,
            "urls_external_skipped": self.urls_external_skipped,
            "links_emitted": self.links_emitted,
            "unmatched_sample": canonical_sample(self.unmatched_sample, 10),
        }


# ── Helper functions (module-level for testability + caching) ───────────────


def _file_to_url_pattern(rel_path: str) -> str | None:
    """Convert a repo-relative route file path to a URL pattern string.

    Returns ``None`` when the path doesn't look like a Next API route.

    Examples (POSIX-style input):
      * ``apps/web/app/api/rules/route.ts``
            → ``/api/rules``
      * ``apps/web/app/api/rules/[id]/route.ts``
            → ``/api/rules/[id]``
      * ``apps/web/app/api/auth/[...all]/route.ts``
            → ``/api/auth/[...all]``
      * ``pages/api/health.ts``
            → ``/api/health``
      * ``apps/web/pages/api/users/[id].ts``
            → ``/api/users/[id]``
    """
    if not rel_path:
        return None
    posix = rel_path.replace("\\", "/")

    # App-router: locate the substring "app/api/" anywhere in the path,
    # then keep the segment that begins at "api/" and strip "/route.<ext>".
    app_router_idx = posix.find("/app/api/")
    if posix.startswith("app/api/"):
        app_router_idx = 0
        suffix = posix[len("app/"):]  # strip the leading "app/" only
    elif app_router_idx >= 0:
        suffix = posix[app_router_idx + len("/app/"):]
    else:
        suffix = None

    if suffix is not None:
        # suffix looks like "api/.../route.ts"
        if not suffix.startswith("api/"):
            return None
        # Drop "/route.<ext>" at the end.
        if "/route." in suffix:
            suffix = suffix[: suffix.rfind("/route.")]
        else:
            return None
        return "/" + suffix

    # Pages-router: locate "/pages/api/" and treat each file as a route.
    pages_idx = posix.find("/pages/api/")
    if posix.startswith("pages/api/"):
        rel = posix[len("pages/"):]
    elif pages_idx >= 0:
        rel = posix[pages_idx + len("/pages/"):]
    else:
        return None
    # rel: "api/.../file.ts"
    if not rel.startswith("api/"):
        return None
    # Strip file extension.
    if "." in rel.rsplit("/", 1)[-1]:
        rel = rel[: rel.rfind(".")]
    # Pages-api skip if it's the index endpoint marker — but Next treats
    # `index.ts` as the bare folder path. Normalise that.
    if rel.endswith("/index"):
        rel = rel[: -len("/index")]
    return "/" + rel


def _url_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a Next URL pattern (with [foo] segments) into a regex."""
    # Replace dynamic segments BEFORE escaping the literal parts. We
    # use a placeholder unlikely to collide with anything in URLs.
    OPT_CATCHALL_PH = "\x00OCATCH\x00"
    CATCHALL_PH = "\x00CATCH\x00"
    SINGLE_PH = "\x00SINGLE\x00"

    pat = _DYN_OPTIONAL_CATCHALL.sub(OPT_CATCHALL_PH, pattern)
    pat = _DYN_CATCHALL.sub(CATCHALL_PH, pat)
    pat = _DYN_SINGLE.sub(SINGLE_PH, pat)

    escaped = re.escape(pat)
    escaped = escaped.replace(re.escape(OPT_CATCHALL_PH), r"(?:.*)?")
    escaped = escaped.replace(re.escape(CATCHALL_PH), r".*")
    escaped = escaped.replace(re.escape(SINGLE_PH), r"[^/]+")
    return re.compile("^" + escaped + "$")


def _normalise_url(raw: str) -> tuple[str | None, bool, bool]:
    """Normalise a captured URL string to a matchable form.

    Returns ``(normalised, had_template_slot, external_skip)``.
      * ``normalised`` is the URL ready for regex matching, or ``None``
        when it should be skipped entirely (external host, non-api).
      * ``had_template_slot`` indicates at least one ``${var}`` was
        replaced — caller maps this to a confidence of 0.7.
      * ``external_skip`` is True when the URL was rejected because it
        targets an external host (used for telemetry).
    """
    if not raw:
        return None, False, False

    s = raw.strip()
    # Drop query/fragment first so they don't influence host detection
    # for relative URLs. For absolute URLs we strip the host first.
    external = False
    if _EXTERNAL_URL.match(s):
        # External URL — only keep if there's a relative /api/ part
        # AFTER the host (we still skip cross-origin calls since they
        # can't hit OUR route handlers).
        external = True
        return None, False, True

    s = _URL_QUERY.sub("", s)

    had_slot = bool(_TPL_SLOT.search(s))
    if had_slot:
        # Replace each ${var} with a slash-free placeholder so the
        # downstream regex-fullmatch against compiled route patterns
        # (``^/api/.../[^/]+$``) succeeds for dynamic segments. The
        # placeholder MUST not contain a forward slash; we use a
        # static ASCII token rather than the bracketed regex literal
        # ``[^/]+`` (which would itself contain a slash, breaking
        # the per-segment match).
        s = _TPL_SLOT.sub("__DYN__", s)

    # Only consider /api/ URLs — internal calls to Next API.
    if not s.startswith("/api/") and s != "/api":
        return None, had_slot, external

    return s, had_slot, False


@lru_cache(maxsize=2048)
def _read_text_cached(abs_path: str) -> str | None:
    """LRU-cached UTF-8 read of ``abs_path``. Returns ``None`` on error."""
    try:
        with open(abs_path, "r", encoding="utf-8") as fp:
            return fp.read()
    except (OSError, UnicodeDecodeError):
        return None


def _find_method_handlers(text: str) -> dict[str, tuple[int, int]]:
    """Return ``{verb: (start_line, end_line)}`` for HTTP methods.

    ``end_line`` is a best-effort: we walk forward from the export line
    looking for the next top-level export OR end of file. This gives a
    line range we can surface; we never need an exact AST extent.
    """
    handlers: dict[str, tuple[int, int]] = {}
    lines = text.splitlines()

    # First pass: collect export start lines for HTTP verbs.
    starts: list[tuple[str, int]] = []
    for idx, line in enumerate(lines, start=1):
        m = _METHOD_EXPORT.match(line)
        if m and m.group(1) in _HTTP_METHODS:
            starts.append((m.group(1), idx))

    # Second pass: compute end lines as start-of-next-export - 1.
    for i, (verb, start_line) in enumerate(starts):
        if i + 1 < len(starts):
            end_line = starts[i + 1][1] - 1
        else:
            end_line = len(lines)
        handlers[verb] = (start_line, max(start_line, end_line))

    return handlers


def _pick_target_symbol(
    verb_handlers: dict[str, tuple[int, int]],
    method_hint: str | None,
) -> tuple[str, int, int]:
    """Pick a (symbol, line_start, line_end) given the caller's method hint."""
    if method_hint and method_hint in verb_handlers:
        lo, hi = verb_handlers[method_hint]
        return method_hint, lo, hi
    # Priority order — most common HTTP verbs first.
    for verb in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        if verb in verb_handlers:
            lo, hi = verb_handlers[verb]
            return verb, lo, hi
    # Any other verb (HEAD/OPTIONS) — first one alphabetically.
    for verb in sorted(verb_handlers):
        lo, hi = verb_handlers[verb]
        return verb, lo, hi
    # No exports detected → whole-file fallback.
    return "<route>", 1, 1


# ── Linker class ────────────────────────────────────────────────────────────


class NextjsHttpRouteLinker:
    """Resolves Next.js fetch URLs to ``route.ts`` handler files."""

    name: str = "nextjs-http-route"
    activation_keys: tuple[str, ...] = ("next-app-router", "next-pages", "next-monorepo", "next")

    def __init__(self) -> None:
        # Per-scan precomputed state — populated by ``is_active`` /
        # ``_ensure_route_map``. Re-instantiated on every scan because
        # the orchestrator builds a fresh linker per run.
        self._route_map: list[_CompiledRoute] | None = None
        self._repo_root: Path | None = None
        self.telemetry: _LinkerTelemetry = _LinkerTelemetry()

    # ── Activation ──────────────────────────────────────────────────────

    def is_active(self, ctx: "ScanContext") -> bool:
        if self._is_next_stack(ctx):
            self._repo_root = ctx.repo_path
            return True
        return False

    @staticmethod
    def _is_next_stack(ctx: "ScanContext") -> bool:
        # Auditor verdict first.
        audited = (ctx.audited_stack or "").lower()
        if any(audited.startswith(p) for p in _NEXT_AUDITED_PREFIXES):
            return True
        # Stage 0 heuristic stack.
        stage0 = (ctx.stack or "").lower()
        if any(stage0.startswith(p) for p in _NEXT_AUDITED_PREFIXES):
            return True
        # Secondary stacks from auditor.
        for sec in (ctx.secondary_stacks or ()):
            sec_l = sec.lower()
            if any(sec_l.startswith(p) for p in _NEXT_AUDITED_PREFIXES):
                return True
        # Workspace package.json next dep — last resort for monorepos
        # where the root auditor verdict missed the per-workspace stack.
        for ws in (ctx.workspaces or []):
            if not ws.package_json:
                continue
            deps = {}
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                d = ws.package_json.get(key)
                if isinstance(d, dict):
                    deps.update(d)
            for dep in deps:
                if any(str(dep).startswith(p) for p in _NEXT_DEPENDENCY_PATTERNS):
                    return True
        return False

    # ── Route map (per-scan, lazy) ──────────────────────────────────────

    def _ensure_route_map(self, ctx: "ScanContext") -> list[_CompiledRoute]:
        if self._route_map is not None:
            return self._route_map

        self._repo_root = ctx.repo_path
        routes: list[_CompiledRoute] = []
        # Stage 0's tracked_files is our source of truth — never re-glob.
        for rel in ctx.tracked_files:
            posix = rel.replace("\\", "/")
            # Fast pre-filter: must look like a Next route file.
            is_app_route = (
                "/app/api/" in "/" + posix
                and (posix.endswith("/route.ts") or posix.endswith("/route.tsx")
                     or posix.endswith("/route.js") or posix.endswith("/route.jsx")
                     or posix.endswith("/route.mts") or posix.endswith("/route.mjs"))
            )
            is_pages_route = (
                "/pages/api/" in "/" + posix
                and (posix.endswith(".ts") or posix.endswith(".tsx")
                     or posix.endswith(".js") or posix.endswith(".jsx"))
            )
            if not (is_app_route or is_pages_route):
                continue
            pattern = _file_to_url_pattern(posix)
            if pattern is None:
                continue
            try:
                regex = _url_pattern_to_regex(pattern)
            except re.error:
                continue
            routes.append(_CompiledRoute(regex=regex, file=posix, raw_pattern=pattern))

        # Sort by raw_pattern length (longest first) so a literal route
        # wins against a catchall when both match — Next's own routing
        # uses specificity ranking and we mirror it.
        routes.sort(key=lambda r: (-len(r.raw_pattern), r.raw_pattern))

        self._route_map = routes
        self.telemetry.route_map_size = len(routes)
        return routes

    # ── Public surface ─────────────────────────────────────────────────

    def link_for_feature(
        self,
        feature: "Feature",
        ctx: "ScanContext",
        log: "StageLogger",
    ) -> list[FrameworkLink]:
        routes = self._ensure_route_map(ctx)
        if not routes:
            return []
        self.telemetry.features_processed += 1

        # Caller files = every file mentioned by the feature's C3
        # symbol_attributions PLUS the feature's primary paths (some
        # features have no flows + no symbol_attributions — we still
        # want their primary files scanned).
        caller_files = self._caller_files(feature)
        if not caller_files:
            return []

        links: list[FrameworkLink] = []
        for rel in caller_files:
            abs_path = str(ctx.repo_path / rel)
            text = _read_text_cached(abs_path)
            if text is None:
                self.telemetry.files_unreadable += 1
                continue
            self.telemetry.files_scanned += 1
            file_links = self._scan_file_for_links(
                rel, text, routes, ctx, log,
                feature_name=feature.name,
            )
            links.extend(file_links)

        self.telemetry.links_emitted += len(links)
        return links

    # ── Internals ──────────────────────────────────────────────────────

    @staticmethod
    def _caller_files(feature: "Feature") -> list[str]:
        """Return every repo-relative file the feature is attributed to."""
        seen: set[str] = set()
        out: list[str] = []
        for attr in (feature.symbol_attributions or []):
            f = (attr.file or "").replace("\\", "/")
            if f and f not in seen:
                seen.add(f)
                out.append(f)
        for f in (feature.paths or []):
            f = (f or "").replace("\\", "/")
            if not f:
                continue
            # Only include FILE paths (skip directories); a path is a
            # file when it has a real extension. Directories show up as
            # bare slugs like ``apps/web``.
            if "." not in f.rsplit("/", 1)[-1]:
                continue
            if f in seen:
                continue
            seen.add(f)
            out.append(f)
        return out

    def _scan_file_for_links(
        self,
        rel: str,
        text: str,
        routes: list[_CompiledRoute],
        ctx: "ScanContext",
        log: "StageLogger",
        *,
        feature_name: str,
    ) -> list[FrameworkLink]:
        results: list[FrameworkLink] = []
        # Pre-split for line resolution; cheap on text < ~1MB.
        lines = text.splitlines()

        # Each (url, line, after_url_excerpt) triple from any pattern.
        captures = self._extract_url_captures(text, lines)
        if not captures:
            return results

        for raw_url, line_no, after_excerpt in captures:
            self.telemetry.fetch_urls_found += 1
            normalised, had_slot, external = _normalise_url(raw_url)
            if external:
                self.telemetry.urls_external_skipped += 1
                continue
            if normalised is None:
                # Not an /api/ URL — silently skip; we only link API calls.
                continue

            # Method hint: look for `method: "X"` in the small window
            # immediately following the URL literal.
            method_hint = None
            mh = _METHOD_NEAR_URL.search(after_excerpt)
            if mh:
                method_hint = mh.group(1).upper()

            # Match against route map.
            matched_route = None
            for r in routes:
                if r.regex.fullmatch(normalised):
                    matched_route = r
                    break
            if matched_route is None:
                self.telemetry.urls_unmatched += 1
                self.telemetry.unmatched_sample.append(
                    {"url": raw_url, "file": rel},
                )
                continue

            self.telemetry.urls_matched += 1

            # Resolve target symbol inside the route file.
            target_abs = str(ctx.repo_path / matched_route.file)
            target_text = _read_text_cached(target_abs) or ""
            handlers = _find_method_handlers(target_text)
            target_symbol, ts_lo, ts_hi = _pick_target_symbol(handlers, method_hint)

            # Source symbol: best-effort by walking backwards from the
            # match line looking for an enclosing function/component
            # declaration. Falls back to "<module>".
            source_symbol = _enclosing_symbol(lines, line_no)

            confidence = 0.7 if had_slot else 1.0
            link = FrameworkLink(
                source_file=rel,
                source_symbol=source_symbol,
                source_line=line_no,
                target_file=matched_route.file,
                target_symbol=target_symbol,
                target_line_start=ts_lo,
                target_line_end=ts_hi,
                linker=self.name,
                link_kind="http-route",
                confidence=confidence,
                reason=(
                    f"fetch URL {raw_url!r} resolves to Next route "
                    f"{matched_route.raw_pattern}"
                ),
            )
            results.append(link)
            log.emit(
                feature_name,
                f"link emitted: {rel}:{line_no} → "
                f"{matched_route.file}:{target_symbol}",
                linker=self.name,
                url=raw_url,
                normalised_url=normalised,
                method_hint=method_hint,
                confidence=confidence,
            )

        return results

    @staticmethod
    def _extract_url_captures(
        text: str, lines: list[str],
    ) -> list[tuple[str, int, str]]:
        """Return ``(url, line_no, after_url_excerpt)`` for every match."""
        out: list[tuple[str, int, str]] = []
        for pattern in _FETCH_CALL_PATTERNS:
            for m in pattern.finditer(text):
                url = m.group(1)
                # Resolve 1-indexed line number from byte offset.
                start = m.start()
                line_no = text.count("\n", 0, start) + 1
                # Capture a small excerpt AFTER the URL so the method-hint
                # regex can run on local context only.
                after = text[m.end(): m.end() + 200]
                out.append((url, line_no, after))
        return out


# ── Enclosing-symbol heuristic ──────────────────────────────────────────────


_FN_DECL = re.compile(
    r"""^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?
        (?:function\s+([A-Za-z_$][\w$]*)
         |const\s+([A-Za-z_$][\w$]*)\s*=
         |let\s+([A-Za-z_$][\w$]*)\s*=
        )""",
    re.VERBOSE,
)


def _enclosing_symbol(lines: list[str], call_line: int) -> str:
    """Walk backwards from ``call_line`` to find an enclosing decl name.

    Returns ``"<module>"`` when nothing is found within a 200-line look-back.
    """
    start = max(1, call_line - 200)
    for ln in range(call_line - 1, start - 1, -1):
        if ln <= 0 or ln > len(lines):
            continue
        line = lines[ln - 1]
        m = _FN_DECL.match(line)
        if m:
            for grp in m.groups():
                if grp:
                    return grp
    return "<module>"


__all__ = ["NextjsHttpRouteLinker"]
