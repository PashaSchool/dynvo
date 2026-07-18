"""GoRouterExtractor — Go HTTP router registrations → anchors.

Parses ``.go`` files for HTTP route registration calls across the
common Go router ecosystems: chi, gin, echo, fiber, net/http
(``http.NewServeMux`` + ``HandleFunc``), and julienschmidt
``httprouter``.

We use REGEX deliberately — not Go AST parsing. The Go AST requires
a Go toolchain installed on the user machine; we can't assume that.
Regex is sufficient at the granularity we need: one anchor per
discovered route path. False positives cost ≤ one extra anchor (which
Stage 2 reconciliation handles) and the regex set is calibrated
against real chi/gin/echo source trees in the corpus.

Patterns live in ``eval/stacks/go-http-router.yaml`` — adding a new
router is a YAML edit, never a Python edit (per ``stack-pattern-library``
skill). This file just compiles those patterns at construction time.

Activation gate: the extractor only fires when the auditor or Stage 0
classified the repo as Go. On a non-Go repo (rust-workspace,
python-library, next-app-router) the extractor returns ``[]`` after a
cheap stack check — no .go files are read because there shouldn't be
any to begin with, but we short-circuit explicitly so a stray
``vendor/foo.go`` in a JS repo doesn't poison the result.

No LLM. No network. Pure file-system scan + regex.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._pattern_base import PatternExtractor
from faultline.pipeline_v2.extractors._util import (
    is_any_stack,
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Armed-extraction flag ──────────────────────────────────────────────────

GO_EXTRACTION_ENV = "FAULTLINE_GO_EXTRACTION"


def go_extraction_enabled() -> bool:
    """Default **OFF**. ``FAULTLINE_GO_EXTRACTION`` in ``{1,true,on,yes}``
    arms the ``armed:`` YAML block: the gorilla/mux registration signature
    plus the ``route_must_be_path`` filter that drops the header-name / JSON-
    key false positives the bare ``.Get("...")`` patterns pick up on real Go
    code. Unset / ``0`` / any falsy token keeps the shipped extractor
    byte-identical (the flag is read at COLLECT time, so the compiled bundle
    is shared across OFF/ON — no cache staleness)."""
    return os.environ.get(GO_EXTRACTION_ENV, "0").strip().lower() in {
        "1", "true", "on", "yes",
    }


# HTTP method tokens that may prefix a Go 1.22 net/http ServeMux pattern
# (``"GET /items/{id}"``). Used to recognise a method-prefixed route PATH and
# to strip the token before slugifying.
_METHOD_TOKENS = frozenset({
    "GET", "POST", "PUT", "PATCH", "DELETE",
    "HEAD", "OPTIONS", "CONNECT", "TRACE",
})


# ── YAML config loader ─────────────────────────────────────────────────────


def _load_config() -> dict:
    """Load the go-http-router YAML from the packaged data tree (hermetic)."""
    return load_stack_yaml("go-http-router")


_RouterTable = tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...]

_CompiledTables = tuple[
    _RouterTable,          # [0] base routers (used when flag OFF)
    tuple[str, ...],       # [1] path-prefix excludes
    tuple[str, ...],       # [2] suffix excludes
    dict[str, float],      # [3] confidence map
    _RouterTable,          # [4] armed routers = base + armed.router_patterns
    bool,                  # [5] route_must_be_path (armed policy)
    frozenset[str],        # [6] armed extra exclude segments (testdata/…)
]


def _compile_routers(
    routers_raw: object,
) -> _RouterTable:
    """Compile a ``{name: {router_constructor, route_call}}`` mapping."""
    routers: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = []
    if not isinstance(routers_raw, dict):
        return ()
    for router_name, block in routers_raw.items():
        if not isinstance(block, dict):
            continue
        ctor = block.get("router_constructor")
        call = block.get("route_call")
        if not isinstance(ctor, str) or not isinstance(call, str):
            continue
        try:
            ctor_re = re.compile(ctor)
            call_re = re.compile(call)
        except re.error as exc:
            logger.warning(
                "go-http-router pattern compile failed for %s: %s",
                router_name, exc,
            )
            continue
        routers.append((str(router_name), ctor_re, call_re))
    return tuple(routers)


def _compile(config: dict) -> _CompiledTables:
    """Compile router regex pairs + path excludes + confidence map + the
    armed (flag-gated) additions.

    Returns
    ``(base_routers, excludes, exclude_suffixes, confidence_map,
       armed_routers, route_must_be_path, armed_exclude_segments)``.
    The armed fields are consulted at collect time ONLY when
    ``FAULTLINE_GO_EXTRACTION`` is on, so the same cached bundle serves both
    OFF and ON with no staleness. Caching is handled by
    :class:`PatternExtractor`.
    """
    base_routers = _compile_routers(config.get("router_patterns"))

    excludes = tuple(
        str(p) for p in (config.get("excludes") or []) if isinstance(p, str)
    )
    exclude_suffixes = tuple(
        str(s) for s in (config.get("exclude_suffixes") or [])
        if isinstance(s, str)
    )
    conf_raw = config.get("confidence") or {}
    confidence = {
        "with_constructor_in_file": float(
            conf_raw.get("with_constructor_in_file", 0.9),
        ),
        "without_constructor_in_file": float(
            conf_raw.get("without_constructor_in_file", 0.7),
        ),
    }

    # Armed additions — the base routers stay active (now filtered by the
    # path policy) and the armed router signatures are appended.
    armed_raw = config.get("armed") or {}
    if not isinstance(armed_raw, dict):
        armed_raw = {}
    armed_extra = _compile_routers(armed_raw.get("router_patterns"))
    armed_routers = base_routers + armed_extra
    route_must_be_path = bool(armed_raw.get("route_must_be_path", False))
    armed_exclude_segments = frozenset(
        str(s) for s in (armed_raw.get("exclude_segments") or [])
        if isinstance(s, str)
    )

    return (
        base_routers,
        excludes,
        exclude_suffixes,
        confidence,
        armed_routers,
        route_must_be_path,
        armed_exclude_segments,
    )


# ── Activation gate ────────────────────────────────────────────────────────


def _is_go_repo(ctx: "ScanContext") -> bool:
    """``True`` if any signal indicates this repo is Go-shaped."""
    if is_any_stack(ctx, "go"):
        return True
    if (ctx.audited_stack or "").lower().startswith("go-"):
        return True
    # ``go-server``, ``go-library``, etc. as secondary
    secondaries = tuple(s.lower() for s in (ctx.secondary_stacks or ()))
    return any(s.startswith("go-") for s in secondaries)


# ── Path → slug helper ─────────────────────────────────────────────────────


def _route_to_slug(route: str) -> str:
    """Convert a Go route path to a kebab-slug.

    Examples:
        ``"/"``                 → ``"root"``
        ``"/users"``            → ``"users"``
        ``"/users/{id}/posts"`` → ``"users-id-posts"``
        ``"/api/v1/orders/:id"``→ ``"api-v1-orders-id"``
        ``"/*"``                → ``"root"``  (wildcard-only)
    """
    if not route or route in ("/", "/*", "*"):
        return "root"
    # Strip braces / colons used for path params.
    stripped = (
        route.replace("{", " ")
        .replace("}", " ")
        .replace(":", " ")
        .replace("*", " ")
    )
    slug = slugify(stripped)
    return slug or "root"


def _is_excluded(path: str, prefixes: tuple[str, ...],
                 suffixes: tuple[str, ...]) -> bool:
    """``True`` if ``path`` matches any prefix or suffix exclude."""
    p = posix(path)
    for prefix in prefixes:
        if prefix and (p.startswith(prefix) or f"/{prefix}" in f"/{p}"):
            return True
    for suffix in suffixes:
        if suffix and p.endswith(suffix):
            return True
    return False


def _has_excluded_segment(path: str, segments: frozenset[str]) -> bool:
    """``True`` if any path segment is in ``segments`` (armed exclude —
    ``testdata/`` fixtures, ``examples/`` demos never mint routes)."""
    if not segments:
        return False
    return any(seg in segments for seg in posix(path).split("/"))


def _method_prefix(route: str) -> str | None:
    """Return the leading HTTP-method token of a ``"METHOD /path"`` mux
    pattern (Go 1.22 net/http), else ``None``. ``"GET /items"`` → ``"GET"``;
    ``"/items"`` → ``None``; ``"Content-Type"`` → ``None``."""
    head, sep, rest = route.partition(" ")
    if sep and head in _METHOD_TOKENS and rest.startswith("/"):
        return head
    return None


def _is_route_path(route: str) -> bool:
    """``True`` iff ``route`` is a URL PATH — starts with ``/`` OR is a
    method-prefixed ServeMux pattern (``"GET /items/{id}"``).

    A header name (``Content-Type``), a JSON/struct key (``status``), or a
    query-param name (``search``) is NOT a path — this is the scale-invariant
    discriminator that kills the bare-``.Get("s")`` false positives without
    any per-repo vocabulary.
    """
    return route.startswith("/") or _method_prefix(route) is not None


def _strip_method_prefix(route: str) -> str:
    """Drop the leading ``"METHOD "`` from a ServeMux pattern so the slug is
    keyed on the path alone (``"GET /items/{id}"`` → ``"/items/{id}"``)."""
    if _method_prefix(route) is not None:
        return route.split(" ", 1)[1]
    return route


def _http_method_of(verb_group: str, route: str) -> str:
    """Best-effort HTTP method for a matched registration.

    Priority: the matched verb group when it IS an HTTP method name
    (chi ``.Get`` / gin ``.GET``), else the Go 1.22 ServeMux method
    prefix (``"GET /x"``), else ``"GET"`` — the ``build_routes_index``
    default. gorilla/mux verbs (``Path``/``PathPrefix``/``Handle*``)
    carry no method in the matched call (it lives in a separate
    ``.Methods(...)`` link of the fluent chain), so they take the
    honest default rather than a guessed parse."""
    verb = (verb_group or "").upper()
    if verb in _METHOD_TOKENS:
        return verb
    prefix = _method_prefix(route)
    if prefix is not None:
        return prefix
    return "GET"


# ── Extractor ──────────────────────────────────────────────────────────────


class GoRouterExtractor(PatternExtractor):
    """Go HTTP router parser. Emits one anchor per discovered route.

    Implements the :class:`AnchorExtractor` Protocol.
    """

    name = "go-router"

    def load_config(self) -> dict:
        return _load_config()

    def is_active(self, ctx: "ScanContext") -> bool:
        return _is_go_repo(ctx)

    def compile_patterns(self, config: dict) -> _CompiledTables:
        return _compile(config)

    def collect(
        self, ctx: "ScanContext", compiled: _CompiledTables,
    ) -> dict[str, dict]:
        (base_routers, excludes, exclude_suffixes, _confidence,
         armed_routers, route_must_be_path, armed_segments) = compiled

        # Flag is read HERE (collect time), not at compile time, so the
        # cached bundle is shared across OFF/ON with no staleness. OFF keeps
        # the shipped base routers + no path filter → byte-identical board.
        armed = go_extraction_enabled()
        routers = armed_routers if armed else base_routers
        if not routers:
            return {}

        # slug → {paths_set, with_ctor_flag, rationale_set, routes_set}
        # ``routes`` is filled ONLY when armed: explicit (pattern, method,
        # file) triples for ``build_routes_index`` Pass A — Go routers are
        # DSL-routed (the URL lives in the source, not the filesystem), the
        # exact class ``AnchorCandidate.routes`` was designed for (FastAPI
        # precedent). OFF leaves ``routes=()`` → Pass A skips → byte-ident.
        anchors: dict[str, dict] = defaultdict(
            lambda: {
                "paths": set(), "with_ctor": False, "rationales": set(),
                "routes": set(),
            },
        )

        for rel_path in ctx.tracked_files:
            if not rel_path.endswith(".go"):
                continue
            if _is_excluded(rel_path, excludes, exclude_suffixes):
                continue
            if armed and _has_excluded_segment(rel_path, armed_segments):
                continue

            abs_path = ctx.repo_path / rel_path
            text = read_text(abs_path)
            if not text:
                continue

            for router_name, ctor_re, call_re in routers:
                has_ctor = bool(ctor_re.search(text))
                for match in call_re.finditer(text):
                    # ``match.group(2)`` is the route path; ``group(1)``
                    # is the method/verb (informational).
                    route = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
                    if not route:
                        continue
                    # Armed: a route must be a URL PATH — drops header
                    # names / JSON keys that bare ``.Get("s")`` picks up.
                    if armed and route_must_be_path and not _is_route_path(route):
                        continue
                    slug_route = _strip_method_prefix(route) if armed else route
                    slug = _route_to_slug(slug_route)
                    if not slug:
                        continue
                    bucket = anchors[slug]
                    bucket["paths"].add(posix(rel_path))
                    if has_ctor:
                        bucket["with_ctor"] = True
                    bucket["rationales"].add(
                        f"{router_name}:{route}",
                    )
                    if armed:
                        bucket["routes"].add((
                            slug_route,
                            _http_method_of(match.group(1) or "", route),
                            posix(rel_path),
                        ))

        return anchors

    def emit(
        self,
        ctx: "ScanContext",
        key: str,
        bucket: dict,
        compiled: _CompiledTables,
    ) -> AnchorCandidate:
        confidence = compiled[3]
        paths = tuple(sorted(bucket["paths"]))
        conf = (
            confidence["with_constructor_in_file"]
            if bucket["with_ctor"]
            else confidence["without_constructor_in_file"]
        )
        rationale_sample = ", ".join(sorted(bucket["rationales"])[:5])
        return AnchorCandidate(
            name=key,
            paths=paths,
            source=self.name,
            confidence_self=conf,
            rationale=f"go-router routes: {rationale_sample}",
            # Armed-only (empty set OFF): sorted for determinism.
            routes=tuple(sorted(bucket["routes"])),
        )


__all__ = ["GO_EXTRACTION_ENV", "GoRouterExtractor", "go_extraction_enabled"]
