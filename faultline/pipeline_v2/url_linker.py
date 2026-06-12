"""URL-literal frontend→backend linker (Stage 2.6 evidence channel).

Why this module exists
======================

Frontend code that CALLS a backend feature's routes belongs to that
feature, but no import edge connects them: a React component doing
``fetch("/api/org-knowledge/" + id)`` never imports the FastAPI router
that serves ``/api/org-knowledge/{id}``, so the Stage 2.6 import
closure cannot see the relationship — the per-2026-06 review's "main
differentiator" gap (item 1.2).

This module supplies the missing edge as PURE TEXT EVIDENCE:

  1. **Extract** URL string/template literals from frontend source
     (ts/tsx/js/jsx/mjs/cjs/vue/svelte) at known call idioms —
     ``fetch(...)``, ``axios.get(...)`` / ``axios(...)``, generic
     api-client receivers (``api.get`` / ``httpClient.post`` / …),
     ``useSWR`` / ``useFetch`` / ``$fetch``, and obvious URL constants
     (``const TEAMS_URL = "/api/teams"``). ``trpc.*`` chains are
     SKIPPED — the tRPC linker is a separate, typed channel.
  2. **Normalize** each literal to a route template: ``${id}``
     interpolations and ``+ id`` concat-tails become ``{param}``,
     query strings / fragments are stripped, slashes collapse.
     Relative URLs only — absolute ``http(s)://`` literals are skipped
     unless an in-file base-URL constant makes them host-relative.
  3. **Match** templates against the backend route table (explicit
     extractor routes + filesystem-derived routes), segment-wise:
     a frontend ``{param}`` aligns ONLY with a backend dynamic segment
     (``:id`` / ``{id}`` / ``<id>`` / ``[id]`` — all normalized), a
     frontend static segment aligns with backend static (exact) or
     dynamic (a concrete value); mount-prefix skew is tolerated in
     both directions (extra leading frontend segments for un-modelled
     server mounts; unknown frontend prefixes from base-URL
     interpolations).

The ATTACHMENT itself happens inside
:func:`faultline.pipeline_v2.stage_2_6_membership_closure.run_membership_closure`
— url-link claims join the SAME pool as import-closure claims, with
the same guards (fan-in cap → shared api-client; exact-ownership
shield; workspace-grained reclaim; primary election). This module is
deliberately attachment-free: extraction + normalization + matching
only, all deterministic and hermetic (regex over file text — NO LLM,
NO network, NO AST dependency).

Precision posture
=================

False URL matches are the real risk (a wrongly-attached frontend file
poisons feature membership), so every rule errs toward NOT matching:

  - backend patterns with zero static segments never enter the table
    (``/{param}`` matches everything);
  - mount-skewed matches require ≥ 2 segments with ≥ 1 static on the
    fully-specified side;
  - generic-client calls whose second argument looks like a route
    HANDLER (``api.get("/x", (req, res) => …)`` — an Express router
    that happens to be named ``api``) are skipped;
  - page routes (``method == "PAGE"``) are excluded — fetch targets
    are API surfaces, not page navigations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from faultline.pipeline_v2.indexes import _derive_route_from_path

# ── Extensions the extractor reads ───────────────────────────────────────

URL_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
})

# Confidence for a url-link claim: a string-literal match is weaker
# evidence than a static import (depth-1 closure = 0.5) and weaker than
# a majority co-commit signal (cap 0.45) — the call is real but the
# match is textual. Fixed functional position below both.
URL_LINK_CONFIDENCE = 0.4

_HTTP_VERBS = ("get", "post", "put", "patch", "delete", "head", "options")

# ── Literal fragments (single-line only — multi-line URL templates are
#    vanishingly rare and regex-hostile) ──────────────────────────────────

_QUOTED = r"""(?P<q>["'])(?P<s>[^"'\n]*?)(?P=q)"""
_TEMPLATE = r"`(?P<t>[^`\n]*?)`"
# Optional concat HEAD: ``API_BASE + "/teams"`` — a bare identifier
# (resolved against in-file base consts, else unknown-prefix).
_HEAD = r"(?:(?P<base>[A-Za-z_$][\w$]*)\s*\+\s*)?"
_LITERAL = rf"{_HEAD}(?:{_QUOTED}|{_TEMPLATE})"

# Known fetch-like callables whose FIRST argument is a URL.
_FETCH_CALL_RE = re.compile(
    rf"\b(?:fetch|\$fetch|useFetch|useSWR|useSWRImmutable)\s*\(\s*{_LITERAL}",
)

# axios-family libraries: bare call or .verb(...) call.
_AXIOS_CALL_RE = re.compile(
    rf"\b(?:axios|ky|got|superagent)\s*"
    rf"(?:\.\s*(?P<verb>{'|'.join(_HTTP_VERBS)}|request)\s*)?"
    rf"\(\s*{_LITERAL}",
)

# Generic api-client idiom: receiver IDENTIFIER ending in api/client/
# http (apiClient.get, api.post, httpClient.delete, …). The receiver
# suffix is validated in Python (regex keeps it broad), the trpc chain
# and route-definition shapes are filtered there too.
_CLIENT_CALL_RE = re.compile(
    rf"\b(?P<recv>[A-Za-z_$][\w$]*)\s*\.\s*(?P<verb>{'|'.join(_HTTP_VERBS)})"
    rf"\s*\(\s*{_LITERAL}",
)

# Obvious URL constant: const TEAMS_URL = "/api/teams". The NAME must
# smell like a URL holder and the VALUE must be root-relative.
_URL_CONST_RE = re.compile(
    rf"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*{_LITERAL}",
)
_URL_CONST_NAME_RE = re.compile(r"(url|endpoint|api|path|route)", re.I)

# fetch() method option, searched in a bounded window after the literal.
_METHOD_OPT_RE = re.compile(
    rf"method\s*:\s*[\"'](?P<m>{'|'.join(_HTTP_VERBS)})[\"']", re.I,
)

# Second argument that looks like a route HANDLER (Express-style
# ``api.get("/x", (req, res) => …)`` / ``api.get("/x", handler)``) —
# the literal is a route DEFINITION, not an outgoing call. Applied to
# generic-client matches only (fetch/axios are unambiguous callers).
_HANDLER_ARG_RE = re.compile(
    r"^\s*,\s*(?:async\b|function\b|\(|[A-Za-z_$][\w$]*\s*\)|[A-Za-z_$][\w$]*\s*=>)",
)

# ``${...}`` interpolation inside a template literal.
_INTERP_RE = re.compile(r"\$\{(?P<expr>[^}]*)\}")
# Interpolation that is a bare identifier (resolvable via base consts).
_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")

_ABSOLUTE_RE = re.compile(r"^https?://", re.I)


# ── Data shapes ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UrlRef:
    """One extracted + normalized URL template from a source file."""

    template: str                 # "/api/teams/{param}" (normalized)
    segments: tuple[str, ...]     # ("api", "teams", "{param}")
    method: str | None            # "GET"/"POST"/… when adjacent, else None
    raw: str                      # the literal as written (for evidence)
    unknown_prefix: bool = False  # template began with an unresolved
                                  # interpolation / concat head


@dataclass(frozen=True)
class RouteEntry:
    """One backend route owned by a feature."""

    segments: tuple[str, ...]     # normalized: dynamic → "{param}"/"{**}"
    method: str                   # extractor-reported verb (may be default)
    file: str                     # route source file
    feature: str                  # owning feature name
    pattern: str                  # original pattern (for evidence)


# ── Extraction ───────────────────────────────────────────────────────────


def _literal_of(m: "re.Match[str]") -> tuple[str, bool]:
    """Return (literal_text, is_template) from a ``_LITERAL`` match."""
    t = m.group("t")
    if t is not None:
        return t, True
    return m.group("s") or "", False


def _chain_contains_trpc(text: str, start: int) -> bool:
    """True when the dotted chain ENDING at ``start`` mentions trpc.

    ``trpc.documents.get(...)`` — the ``documents`` receiver match must
    be skipped. Walk backwards over ``ident . ident .`` links.
    """
    i = start - 1
    while i >= 0:
        while i >= 0 and text[i] in " \t":
            i -= 1
        if i < 0 or text[i] != ".":
            return False
        i -= 1
        while i >= 0 and text[i] in " \t":
            i -= 1
        j = i
        while j >= 0 and (text[j].isalnum() or text[j] in "_$"):
            j -= 1
        ident = text[j + 1:i + 1]
        if not ident:
            return False
        if "trpc" in ident.lower():
            return True
        i = j
    return False


def _resolve_base_consts(text: str) -> dict[str, str]:
    """Collect in-file constants usable as URL bases.

    ``const API_BASE = "/api"`` / ``const BASE = "https://x.dev/v1"`` —
    any const whose value is root-relative or absolute http(s). Used to
    resolve ``${API_BASE}/teams`` templates within the SAME file
    (hermetic; no cross-file resolution).
    """
    bases: dict[str, str] = {}
    for m in _URL_CONST_RE.finditer(text):
        if m.group("base") or "${" in (m.group("t") or ""):
            continue  # itself concatenated / interpolated — not a base
        lit, _ = _literal_of(m)
        if lit.startswith("/") or _ABSOLUTE_RE.match(lit):
            bases.setdefault(m.group("name"), lit)
    return bases


def _has_concat_tail(text: str, end: int) -> bool:
    """True when the literal at ``text[:end]`` is followed by ``+ expr``."""
    i = end
    while i < len(text) and text[i] in " \t":
        i += 1
    return i < len(text) and text[i] == "+"


def _method_near(text: str, end: int) -> str | None:
    """Look for ``method: "POST"`` in a bounded window after the literal."""
    window = text[end:end + 300]
    m = _METHOD_OPT_RE.search(window)
    return m.group("m").upper() if m else None


def normalize_url(
    raw: str,
    *,
    is_template: bool = False,
    base_consts: dict[str, str] | None = None,
    concat_tail: bool = False,
    head_const: str | None = None,
) -> UrlRef | None:
    """Normalize one extracted literal to a :class:`UrlRef` (or None).

    Rules (in order):
      1. a concat head (``API_BASE + "/teams"``) or a template
         ``${IDENT}`` at position 0 resolves via in-file base consts;
         an unresolvable head marks ``unknown_prefix`` and is dropped;
      2. every remaining ``${...}`` → ``{param}``;
      3. a ``+ expr`` concat tail appends ``/{param}`` (after a
         trailing slash) — ``"/api/items/" + id``;
      4. query string / fragment stripped; slashes collapsed; trailing
         slash dropped;
      5. absolute ``http(s)://`` URLs survive ONLY when produced by a
         base-const resolution (host stripped → host-relative);
         directly-written absolute literals are external calls — skip;
      6. the result must be root-relative (``/…``) or unknown-prefix,
         with ≥ 1 segment and ≥ 1 STATIC segment.
    """
    url = raw.strip()
    unknown_prefix = False
    from_base = False

    if head_const is not None:
        resolved_head = (base_consts or {}).get(head_const)
        if resolved_head is not None:
            url = resolved_head.rstrip("/") + url
            from_base = True
        else:
            unknown_prefix = True

    if is_template:
        lead = _INTERP_RE.match(url)
        if lead:
            expr = lead.group("expr").strip()
            resolved = (base_consts or {}).get(expr) if _IDENT_RE.match(expr) else None
            if resolved is not None:
                url = resolved.rstrip("/") + url[lead.end():]
                from_base = True
            else:
                url = url[lead.end():]
                unknown_prefix = True
        url = _INTERP_RE.sub("{param}", url)

    if _ABSOLUTE_RE.match(url):
        if not from_base:
            return None  # external absolute URL
        rest = url.split("://", 1)[1]
        slash = rest.find("/")
        url = rest[slash:] if slash >= 0 else ""

    if url.startswith("//"):
        return None  # protocol-relative — external
    url = url.split("?", 1)[0].split("#", 1)[0]
    if concat_tail:
        url = url.rstrip("=")  # "/api/items?id=" + x — query concat
        if url.endswith("/"):
            url += "{param}"
        # "…/items" + id  → ambiguous suffix; leave the static part.

    url = re.sub(r"/{2,}", "/", url).rstrip("/")
    if not url and not unknown_prefix:
        return None
    if not unknown_prefix and not url.startswith("/"):
        return None  # not root-relative ("./x", "x.png", plain words)
    if " " in url:
        return None

    segments = tuple(s for s in url.split("/") if s)
    if not segments:
        return None
    static = [s for s in segments if s != "{param}"]
    if not static:
        return None  # "/{param}" matches everything — refuse
    template = "/" + "/".join(segments)
    return UrlRef(
        template=template,
        segments=segments,
        method=None,
        raw=raw,
        unknown_prefix=unknown_prefix,
    )


def extract_url_refs(text: str) -> list[UrlRef]:
    """Extract every URL reference from one file's source text.

    Deterministic: refs are returned in (position) order, deduplicated
    on (template, method, unknown_prefix).
    """
    if not text:
        return []
    base_consts = _resolve_base_consts(text)
    found: list[tuple[int, UrlRef]] = []

    def _push(
        m: "re.Match[str]", method: str | None, *, lit_required_slash: bool,
    ) -> None:
        lit, is_template = _literal_of(m)
        if not lit and not is_template:
            return
        ref = normalize_url(
            lit,
            is_template=is_template,
            base_consts=base_consts,
            concat_tail=_has_concat_tail(text, m.end()),
            head_const=m.group("base"),
        )
        if ref is None:
            return
        if lit_required_slash and not (
            lit.startswith("/") or is_template or ref.unknown_prefix
        ):
            return
        if method:
            ref = UrlRef(
                template=ref.template, segments=ref.segments,
                method=method.upper(), raw=ref.raw,
                unknown_prefix=ref.unknown_prefix,
            )
        found.append((m.start(), ref))

    for m in _FETCH_CALL_RE.finditer(text):
        _push(m, _method_near(text, m.end()) or None, lit_required_slash=False)

    for m in _AXIOS_CALL_RE.finditer(text):
        verb = m.group("verb")
        method = verb.upper() if verb and verb != "request" else None
        _push(m, method or _method_near(text, m.end()), lit_required_slash=False)

    for m in _CLIENT_CALL_RE.finditer(text):
        recv = m.group("recv")
        low = recv.lower()
        if low in {"axios", "ky", "got", "superagent"}:
            continue  # already covered with better method handling
        if not low.endswith(("api", "client", "http")):
            continue
        if "trpc" in low or _chain_contains_trpc(text, m.start()):
            continue
        if _HANDLER_ARG_RE.match(text[m.end():m.end() + 120]):
            continue  # looks like a route definition, not a call
        _push(m, m.group("verb"), lit_required_slash=True)

    for m in _URL_CONST_RE.finditer(text):
        name = m.group("name")
        if not _URL_CONST_NAME_RE.search(name):
            continue
        if re.search(r"base|origin|host|prefix", name, re.I):
            continue  # base/origin consts are URL HEADS, not endpoints
        lit, is_template = _literal_of(m)
        if not lit.startswith("/") and not is_template:
            continue
        _push(m, None, lit_required_slash=True)

    found.sort(key=lambda t: t[0])
    out: list[UrlRef] = []
    seen: set[tuple[str, str | None, bool]] = set()
    for _, ref in found:
        key = (ref.template, ref.method, ref.unknown_prefix)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


# ── Backend route table ──────────────────────────────────────────────────

# Backend dynamic-segment spellings, normalized to "{param}":
#   :id (Express/Rails)   {id} (FastAPI)   <id>/<int:id> (Flask/Django)
#   [id]/[[id]] (Next)    $id (Remix flat-file)
# Catch-alls ([...slug], *, :rest*, {path:path}) → "{**}".
_CATCHALL_SEG_RE = re.compile(
    r"^(?:\[{1,2}\.\.\.[^\]]*\]{1,2}|\*[\w$]*|:[\w$]+\*|\{[^}]*:\s*path\s*\})$",
)
_DYNAMIC_SEG_RE = re.compile(
    r"^(?::[\w$]+|\{[^}]+\}|<[^>]+>|\[{1,2}[^\]]+\]{1,2}|\$[\w$]+)$",
)


def route_pattern_segments(pattern: str) -> tuple[str, ...] | None:
    """Normalize a backend route pattern to matchable segments.

    Returns None for patterns with no static segment (they would match
    everything) or with no segments at all.
    """
    raw = pattern.split("?", 1)[0].strip()
    segs: list[str] = []
    for seg in raw.split("/"):
        if not seg:
            continue
        if _CATCHALL_SEG_RE.match(seg):
            segs.append("{**}")
        elif _DYNAMIC_SEG_RE.match(seg):
            segs.append("{param}")
        else:
            segs.append(seg)
    if not segs:
        return None
    if not any(s not in ("{param}", "{**}") for s in segs):
        return None
    return tuple(segs)


def build_route_table(
    extractor_signals: dict[str, list[Any]] | None,
    features: list[Any],
) -> list[RouteEntry]:
    """Build the backend route table from Stage 1 extractor signals.

    Reuses the SAME two passes as ``build_routes_index`` (Stage 6.8):
    Pass A reads explicit ``(pattern, method, file)`` tuples off any
    extractor's candidates (FastAPI / Express / Django / Rails / …);
    Pass B derives ``(pattern, method)`` from the filesystem ``route``
    extractor's file paths. Routes are kept only when:

      - an owning feature exists (the route file appears in a feature's
        ``paths``, exact entry first, longest directory prefix second);
      - the method is a real verb surface (``PAGE`` routes are page
        navigations, not fetch targets);
      - the normalized pattern has ≥ 1 static segment.

    Output is sorted (pattern, method, file) for determinism.
    """
    if not extractor_signals:
        return []

    exact_owner: dict[str, str] = {}
    dir_prefixes: list[tuple[str, str]] = []  # (prefix, feature) longest-first
    for f in features:
        for raw in f.paths:
            p = str(raw).rstrip("/")
            if not p or p == ".":
                continue
            if "." in p.rsplit("/", 1)[-1]:
                exact_owner.setdefault(p, f.name)
            else:
                dir_prefixes.append((p + "/", f.name))
    dir_prefixes.sort(key=lambda t: (-len(t[0]), t[0], t[1]))

    def _owner(file_str: str) -> str | None:
        got = exact_owner.get(file_str)
        if got:
            return got
        for prefix, name in dir_prefixes:
            if file_str.startswith(prefix):
                return name
        return None

    entries: dict[tuple[tuple[str, ...], str, str], RouteEntry] = {}

    def _emit(pattern: str, method: str, file_str: str) -> None:
        if not pattern or not file_str:
            return
        meth = (method or "GET").upper()
        if meth == "PAGE":
            return
        segs = route_pattern_segments(pattern)
        if segs is None:
            return
        feature = _owner(file_str)
        if feature is None:
            return
        key = (segs, meth, file_str)
        if key not in entries:
            entries[key] = RouteEntry(
                segments=segs, method=meth, file=file_str,
                feature=feature, pattern=pattern,
            )

    # Pass A — explicit route tuples on ANY extractor's candidates.
    for src_name in sorted(extractor_signals):
        if src_name == "_errors":
            continue
        for cand in extractor_signals[src_name] or []:
            for entry in getattr(cand, "routes", None) or ():
                try:
                    pat, meth, file_str = entry
                except (ValueError, TypeError):
                    continue
                _emit(str(pat), str(meth or "GET"), str(file_str or ""))

    # Pass B — filesystem route extractor: derive pattern from path.
    for cand in extractor_signals.get("route") or []:
        for raw in getattr(cand, "paths", None) or ():
            file_str = str(raw)
            derived = _derive_route_from_path(file_str)
            if derived is None:
                continue
            pat, meth = derived
            _emit(pat, meth, file_str)

    return sorted(
        entries.values(), key=lambda e: (e.pattern, e.method, e.file),
    )


# ── Matching ─────────────────────────────────────────────────────────────


def _aligned(front: tuple[str, ...], back: tuple[str, ...]) -> bool:
    """Segment-wise alignment of frontend segments vs backend pattern.

    - backend static  : frontend must be the SAME static text;
    - backend {param} : frontend may be static (a concrete value) or
      {param} (an interpolation);
    - backend {**}    : terminal catch-all, consumes ≥ 1 remaining.
    A frontend {param} NEVER matches a backend static segment.
    """
    catchall = bool(back) and back[-1] == "{**}"
    fixed = back[:-1] if catchall else back
    if catchall:
        if len(front) < len(fixed) + 1:
            return False
    elif len(front) != len(fixed):
        return False
    for f, b in zip(front, fixed):
        if b == "{param}":
            continue
        if f != b:
            return False
    return True


def _static_count(segs: tuple[str, ...]) -> int:
    return sum(1 for s in segs if s not in ("{param}", "{**}"))


def match_url(ref: UrlRef, entry: RouteEntry) -> bool:
    """True when ``ref`` plausibly targets ``entry``.

    Three alignments, strictest first:
      1. exact — same segment count, segment-wise aligned;
      2. mount-prefix — DROP k ≥ 1 leading FRONTEND segments (the
         server mounts the router under a prefix the extractor didn't
         model: frontend ``/api/v1/teams/5`` vs backend ``/teams/:id``);
         requires the backend pattern to be specific enough
         (≥ 2 segments, ≥ 1 static);
      3. unknown-prefix — the frontend template lost its head to an
         unresolved interpolation (``${base}/teams/${id}``): align the
         frontend segments as a SUFFIX of the backend pattern; requires
         the frontend side to be specific enough (≥ 2 segments,
         ≥ 1 static).

    Method is deliberately NOT a hard filter: filesystem-derived routes
    default to GET without parsing the handler, and a same-pattern
    different-verb route almost always belongs to the same feature.
    """
    front, back = ref.segments, entry.segments
    if _aligned(front, back):
        return True
    # 2 — mount-prefix skew (extra leading frontend segments).
    if len(back) >= 2 and _static_count(back) >= 1:
        max_drop = len(front) - (len(back) - (1 if back[-1] == "{**}" else 0))
        for k in range(1, max_drop + 1):
            if _aligned(front[k:], back):
                return True
    # 3 — unknown frontend prefix: frontend is a suffix of the backend.
    if ref.unknown_prefix and len(front) >= 2 and _static_count(front) >= 1:
        if back and back[-1] == "{**}":
            return False  # suffix-vs-catchall is too ambiguous
        for k in range(1, len(back) - len(front) + 1):
            if _aligned(front, back[k:]):
                return True
    return False


__all__ = [
    "URL_LINK_CONFIDENCE",
    "URL_SOURCE_EXTENSIONS",
    "RouteEntry",
    "UrlRef",
    "build_route_table",
    "extract_url_refs",
    "match_url",
    "normalize_url",
    "route_pattern_segments",
]
