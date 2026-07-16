"""ServerApiEntryExtractor — code-first server API surfaces as Stage 1 entries (B66).

Decorator-/DSL-routed backends encode their routes in code, not the filesystem
route tree, so they never land in ``routes_index`` — their flows/journeys never
mint and whole backends stay invisible (B63 meter: 554 unseen — NestJS 169,
GraphQL code-first 205, tRPC 120, koa/hono ~60). This extractor walks the repo
for server-entry SIGNATURES and emits, per entry-unit (controller / resolver /
router), one :class:`AnchorCandidate` carrying an explicit ``routes`` tuple
``(pattern, method, file)``. ``build_routes_index`` Pass A folds any extractor's
``.routes`` into ``routes_index`` deduped by ``(pattern, method, file)`` — so a
route already emitted by another source is not duplicated, and existing route
sources are untouched (we only ADD kinds).

Segments (each a separate commit, ONE flag):
  * Seg A — NestJS REST: ``@Controller(prefix)`` class + ``@Get/@Post/@Put/
    @Patch/@Delete/@Options/@Head/@All`` methods (real HTTP methods).
  * Seg B — GraphQL code-first: decorator (``@Resolver`` + ``@Query/@Mutation/
    @Subscription``, type-graphql / @nestjs/graphql), pothos
    (``builder.queryField/mutationField``), nexus (``queryField`` /
    ``extendType``). method = QUERY/MUTATION/SUBSCRIPTION; pattern = operation.
  * Seg C — tRPC: ``router({ key: publicProcedure...query()/mutation() })``.
    method by the terminating verb; pattern = ``<namespace>.<key>``.
  * Seg D — koa-router (``new Router()`` + ``router.<verb>(path|rpc-name)``) and
    hono (``new Hono()`` + ``app.<verb>(path)``) — real HTTP methods.

Flag ``FAULTLINE_SERVER_API_ENTRIES`` — default OFF. Unset/``0`` -> ``extract``
returns ``[]`` and the scan is byte-identical to pre-B66 (kill-switch unit). The
REGISTRY gates on the flag too (``scan_meta.extractor_hits`` serializes every
registered source key — an inert-but-registered extractor still grows the OFF
board by one key, the B67 kill-switch lesson).

B64 literal law: identity/route are taken only from a STATIC token — a
class/const name, a decorator string-literal path, a procedure key, a builder
field-name literal. A path/name that is a variable or member expression is an
honest skip for that route; the entry-unit is still emitted when its own name
is a static token.

No LLM. No network. Read-only.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    is_noise,
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.ownership_v2 import ownership_v2_enabled
from faultline.pipeline_v2.stage_6_9_test_strip import is_test_path

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


SERVER_API_ENTRIES_ENV = "FAULTLINE_SERVER_API_ENTRIES"

#: The single source slug on every emitted candidate (all 4 segments share it,
#: so ``extractor_hits`` grows by exactly one key when the flag is ON).
SERVER_API_ENTRY_SOURCE = "server-api-entry"

#: Bounded per-file read — entry modules are small; the cap only guards
#: pathological blobs (mirrors ``jobs_entries._MAX_BYTES``).
_MAX_BYTES = 1_500_000

#: Bounded lookahead (chars) inside a nexus ``extendType({ type: "Query" })``
#: block for its ``t.field("name")`` declarations.
_EXTEND_WINDOW = 4000

#: Cheap whole-file pre-filter: only files mentioning at least one of these
#: framework markers are parsed (skips the bulk of a TS/JS repo cheaply).
_MARKERS = (
    "@Controller", "@Resolver", "@Query", "@Mutation", "@Subscription",
    "Procedure", "queryField", "mutationField", "subscriptionField",
    "extendType", "new Router", "new Hono",
)


def server_api_entries_enabled() -> bool:
    """``True`` when ``FAULTLINE_SERVER_API_ENTRIES`` is set truthy (default OFF).

    Unset/``0`` keeps the extractor inert (``extract`` -> ``[]``) AND unregistered
    (see :mod:`faultline.pipeline_v2.stage_1_extractors`), so every scan is
    byte-identical to pre-B66."""
    return os.environ.get(SERVER_API_ENTRIES_ENV, "0").strip().lower() not in {
        "", "0", "false", "no", "off",
    }


# ── config ──────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _cfg() -> dict:
    """Grammar vocabulary from ``stacks/server-api-entries.yaml`` (cached)."""
    return load_stack_yaml("server-api-entries")


def _confidence() -> float:
    return float(_cfg().get("confidence") or 0.85)


@lru_cache(maxsize=1)
def _extensions() -> tuple[str, ...]:
    return tuple(str(e) for e in (_cfg().get("extensions") or ()))


@lru_cache(maxsize=1)
def _suffixes() -> tuple[str, ...]:
    return tuple(str(s) for s in (_cfg().get("suffix_strip") or ()))


@lru_cache(maxsize=1)
def _skip_segments() -> frozenset[str]:
    return frozenset(
        str(s).lower() for s in (_cfg().get("skip_path_segments") or ())
    )


@lru_cache(maxsize=1)
def _skip_filename_markers() -> frozenset[str]:
    return frozenset(
        str(s).lower() for s in (_cfg().get("skip_filename_markers") or ())
    )


def _should_skip_path(path: str) -> bool:
    """``True`` for a test/mock/fixture file (shared predicate) OR an artifact
    class the predicate does not cover (storybook / examples / playground /
    demo / sample / generated). Segment match is EXACT — never a substring."""
    p = posix(path).lower()
    if is_test_path(p):
        return True
    segs = p.split("/")
    if any(seg in _skip_segments() for seg in segs[:-1]):
        return True
    base = segs[-1] if segs else ""
    dotparts = base.split(".")
    if len(dotparts) >= 2 and any(
        comp in _skip_filename_markers() for comp in dotparts[1:-1]
    ):
        return True
    return False


def _c(pat: str | None) -> re.Pattern[str]:
    """Compile ``pat`` or a never-match placeholder when it is absent."""
    return re.compile(pat or r"(?!x)x")


# ── shared helpers ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Route:
    pattern: str
    method: str


@dataclass
class _Entry:
    """One detected entry-unit (pre-emission)."""

    slug: str
    file: str
    grammar: str
    routes: list[_Route] = field(default_factory=list)


def _strip_suffixes(name: str, suffixes: tuple[str, ...]) -> str:
    """Peel trailing framework suffixes (Controller/Resolver/Router/...)."""
    changed = True
    while changed:
        changed = False
        for suf in suffixes:
            if name.endswith(suf) and len(name) > len(suf):
                name = name[: -len(suf)]
                changed = True
                break
    return name


def _file_stem(path: str) -> str:
    base = posix(path).rsplit("/", 1)[-1]
    return base.split(".")[0]


def _parent_dir(path: str) -> str:
    parts = posix(path).rsplit("/", 1)
    if len(parts) < 2:
        return ""
    return parts[0].rsplit("/", 1)[-1]


def _stem_slug(path: str, suffixes: tuple[str, ...]) -> str:
    """Capability slug from the file stem, else the parent dir; ``""`` when both
    are pure noise (index/schema/types/...)."""
    s = slugify(_strip_suffixes(_file_stem(path), suffixes))
    if s and not is_noise(s):
        return s
    d = slugify(_parent_dir(path))
    if d and not is_noise(d):
        return d
    return ""


def _normalize_path(*parts: str) -> str:
    """Join URL fragments into a normalized ``/a/b/c`` pattern (drops empty
    segments, keeps ``:id`` params). Empty input -> ``"/"``."""
    segs: list[str] = []
    for part in parts:
        for seg in part.split("/"):
            seg = seg.strip()
            if seg:
                segs.append(seg)
    return "/" + "/".join(segs)


def _first_segment_slug(url: str) -> str:
    """Slug of the first non-noise, non-param URL segment; ``""`` when none."""
    for raw in url.split("/"):
        seg = raw.strip()
        if not seg or seg.startswith(":") or seg == "*":
            continue
        if (seg.startswith("{") and seg.endswith("}")) or (
            seg.startswith("<") and seg.endswith(">")
        ):
            continue
        if is_noise(seg):
            continue
        return slugify(seg)
    return ""


# ── Seg A — NestJS REST controllers ───────────────────────────────────────────


@dataclass
class _NestGrammar:
    require: re.Pattern[str]
    controller_string_arg: re.Pattern[str]
    controller_obj_path: re.Pattern[str]
    controller_bare: re.Pattern[str]
    class_re: re.Pattern[str]
    verb_re: re.Pattern[str]


@lru_cache(maxsize=1)
def _nest_grammar() -> _NestGrammar | None:
    block = _cfg().get("nestjs")
    if not isinstance(block, dict):
        return None
    return _NestGrammar(
        require=_c(block.get("require_import_re")),
        controller_string_arg=_c(block.get("controller_string_arg_re")),
        controller_obj_path=_c(block.get("controller_obj_path_re")),
        controller_bare=_c(block.get("controller_bare_re")),
        class_re=_c(block.get("class_re")),
        verb_re=_c(block.get("verb_re")),
    )


def _nest_slug(text: str, prefix: str, gr: _NestGrammar) -> str:
    """Prefix first-segment, else the ``*Controller`` class name."""
    s = _first_segment_slug(prefix)
    if s:
        return s
    names = [m.group(1) for m in gr.class_re.finditer(text)]
    for n in names:
        if n.endswith("Controller"):
            cand = slugify(_strip_suffixes(n, _suffixes()))
            if cand and not is_noise(cand):
                return cand
    for n in names:
        cand = slugify(_strip_suffixes(n, _suffixes()))
        if cand and not is_noise(cand):
            return cand
    return ""


def _collect_nestjs(text: str, path: str) -> list[_Entry]:
    gr = _nest_grammar()
    if gr is None or not gr.controller_bare.search(text):
        return []
    # @Controller is the Nest-specific entry marker; a @nestjs import is a
    # further corroboration but the decorator alone is distinctive enough.
    prefix = ""
    m = gr.controller_string_arg.search(text)
    if m:
        prefix = m.group(1)
    else:
        m2 = gr.controller_obj_path.search(text)
        if m2:
            prefix = m2.group(1)

    slug = _nest_slug(text, prefix, gr)
    if not slug:
        return []

    # A prefix-less ``@Controller()`` routes at "/" in Nest; using the
    # class-derived slug (a static token, B64-legal) as the pattern prefix gives
    # a descriptive, unique route instead of a pile of bare "/" rows.
    route_prefix = prefix if prefix.strip("/") else slug

    routes: list[_Route] = []
    for vm in gr.verb_re.finditer(text):
        verb = vm.group(1).upper()
        sub = vm.group(2) or ""
        routes.append(_Route(_normalize_path(route_prefix, sub), verb))
    if not routes:
        return []
    return [_Entry(slug, path, "nestjs-controller", routes)]


# ── Seg B — GraphQL code-first ─────────────────────────────────────────────────


@dataclass
class _GraphqlGrammar:
    query_method: str
    mutation_method: str
    subscription_method: str
    # decorator flavor
    dec_require: re.Pattern[str]
    resolver_type: re.Pattern[str]
    resolver_class: re.Pattern[str]
    op_decorator: re.Pattern[str]
    op_name_option: re.Pattern[str]
    # pothos
    pothos_require: re.Pattern[str]
    pothos_field: re.Pattern[str]
    # nexus
    nexus_require: re.Pattern[str]
    nexus_field: re.Pattern[str]
    nexus_extend_type: re.Pattern[str]
    nexus_extend_field: re.Pattern[str]


@lru_cache(maxsize=1)
def _graphql_grammar() -> _GraphqlGrammar | None:
    block = _cfg().get("graphql")
    if not isinstance(block, dict):
        return None
    dec = block.get("decorator") or {}
    pot = block.get("pothos") or {}
    nex = block.get("nexus") or {}
    return _GraphqlGrammar(
        query_method=str(block.get("query_method") or "QUERY"),
        mutation_method=str(block.get("mutation_method") or "MUTATION"),
        subscription_method=str(block.get("subscription_method") or "SUBSCRIPTION"),
        dec_require=_c(dec.get("require_import_re")),
        resolver_type=_c(dec.get("resolver_type_re")),
        resolver_class=_c(dec.get("resolver_class_re")),
        op_decorator=_c(dec.get("op_decorator_re")),
        op_name_option=_c(dec.get("op_name_option_re")),
        pothos_require=_c(pot.get("require_import_re")),
        pothos_field=_c(pot.get("field_re")),
        nexus_require=_c(nex.get("require_import_re")),
        nexus_field=_c(nex.get("field_re")),
        nexus_extend_type=_c(nex.get("extend_type_re")),
        nexus_extend_field=_c(nex.get("extend_field_re")),
    )


#: Optional method modifiers before the operation identifier.
_METHOD_IDENT_RE = re.compile(
    r"(?:(?:public|private|protected|static|readonly|async|get|set)\s+)*"
    r"([A-Za-z_$][\w$]*)\s*\(",
)
#: A stacked decorator's ``@name`` head (dotted names allowed: ``@a.b()``).
_DECORATOR_HEAD_RE = re.compile(r"@[A-Za-z_$][\w$.]*")


def _skip_balanced_parens(text: str, i: int) -> int:
    """``text[i-1]`` was ``(`` (depth 1); return the index just past its match.

    String contents are not parsed — a stray unbalanced paren inside a string
    literal only mis-skips a single operation, never crashes (deterministic
    best-effort, same tolerance as the rest of the regex grammar)."""
    depth = 1
    n = len(text)
    while i < n and depth > 0:
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return i


def _operation_method_name(text: str, i: int) -> str | None:
    """From index ``i`` (just past a ``@Query`` decorator + its args), skip any
    stacked decorators and modifiers, then return the operation method name.

    Handles same-line (``@Query(() => X) users()``), multi-line, and stacked
    (``@Query() @UseGuards(G) users()``) decorator forms."""
    n = len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i < n and text[i] == "@":
            dm = _DECORATOR_HEAD_RE.match(text, i)
            if dm is None:
                return None
            i = dm.end()
            while i < n and text[i].isspace():
                i += 1
            if i < n and text[i] == "(":
                i = _skip_balanced_parens(text, i + 1)
            continue
        break
    mm = _METHOD_IDENT_RE.match(text, i)
    return mm.group(1) if mm else None


def _graphql_kind_method(kind: str, gr: _GraphqlGrammar) -> str:
    return {
        "query": gr.query_method,
        "mutation": gr.mutation_method,
        "subscription": gr.subscription_method,
    }[kind.lower()]


def _graphql_resolver_slug(text: str, path: str, gr: _GraphqlGrammar) -> str:
    m = gr.resolver_type.search(text)
    if m:
        cand = slugify(_strip_suffixes(m.group(1), _suffixes()))
        if cand and not is_noise(cand):
            return cand
    for cm in gr.resolver_class.finditer(text):
        n = cm.group(1)
        if n.endswith("Resolver"):
            cand = slugify(_strip_suffixes(n, _suffixes()))
            if cand and not is_noise(cand):
                return cand
    return _stem_slug(path, _suffixes())


def _collect_graphql_decorator(text: str, path: str, gr: _GraphqlGrammar) -> list[_Entry]:
    if not gr.dec_require.search(text):
        return []
    routes: list[_Route] = []
    for om in gr.op_decorator.finditer(text):
        kind = om.group(1)
        method = _graphql_kind_method(kind, gr)
        # Walk past the decorator's own (optional) balanced args; look for an
        # explicit ``name:`` option ONLY inside them, else the method name.
        j = om.end()
        while j < len(text) and text[j] in " \t":
            j += 1
        op: str | None = None
        if j < len(text) and text[j] == "(":
            args_end = _skip_balanced_parens(text, j + 1)
            nm = gr.op_name_option.search(text, j, args_end)
            if nm:
                op = nm.group(1)
            j = args_end
        if op is None:
            op = _operation_method_name(text, j)
        if not op or is_noise(op):
            continue  # no static operation token -> honest skip
        routes.append(_Route(op, method))
    if not routes:
        return []
    slug = _graphql_resolver_slug(text, path, gr)
    if not slug:
        slug = slugify(routes[0].pattern)
    if not slug:
        return []
    return [_Entry(slug, path, "graphql-resolver", routes)]


def _graphql_schema_slug(path: str, routes: list[_Route]) -> str:
    slug = _stem_slug(path, _suffixes())
    if slug:
        return slug
    return slugify(routes[0].pattern) if routes else ""


def _collect_graphql_pothos(text: str, path: str, gr: _GraphqlGrammar) -> list[_Entry]:
    if not gr.pothos_require.search(text):
        return []
    routes: list[_Route] = []
    for fm in gr.pothos_field.finditer(text):
        method = _graphql_kind_method(fm.group(1), gr)
        op = fm.group(2)
        if op and not is_noise(op):
            routes.append(_Route(op, method))
    if not routes:
        return []
    slug = _graphql_schema_slug(path, routes)
    if not slug:
        return []
    return [_Entry(slug, path, "graphql-pothos", routes)]


def _collect_graphql_nexus(text: str, path: str, gr: _GraphqlGrammar) -> list[_Entry]:
    if not gr.nexus_require.search(text):
        return []
    routes: list[_Route] = []
    for fm in gr.nexus_field.finditer(text):
        method = _graphql_kind_method(fm.group(1), gr)
        op = fm.group(2)
        if op and not is_noise(op):
            routes.append(_Route(op, method))
    for em in gr.nexus_extend_type.finditer(text):
        method = _graphql_kind_method(em.group(1), gr)
        window = text[em.end(): em.end() + _EXTEND_WINDOW]
        for ffm in gr.nexus_extend_field.finditer(window):
            op = ffm.group(1)
            if op and not is_noise(op):
                routes.append(_Route(op, method))
    if not routes:
        return []
    slug = _graphql_schema_slug(path, routes)
    if not slug:
        return []
    return [_Entry(slug, path, "graphql-nexus", routes)]


def _collect_graphql(text: str, path: str) -> list[_Entry]:
    gr = _graphql_grammar()
    if gr is None:
        return []
    out: list[_Entry] = []
    out.extend(_collect_graphql_decorator(text, path, gr))
    out.extend(_collect_graphql_pothos(text, path, gr))
    out.extend(_collect_graphql_nexus(text, path, gr))
    return out


# ── Seg C — tRPC procedures ────────────────────────────────────────────────────


@dataclass
class _TrpcGrammar:
    require: re.Pattern[str]
    router_const: re.Pattern[str]
    procedure_key: re.Pattern[str]
    verb: re.Pattern[str]
    verb_window: int
    fallback_method: str
    query_method: str
    mutation_method: str
    subscription_method: str
    lazy_handler: re.Pattern[str]


@lru_cache(maxsize=1)
def _trpc_grammar() -> _TrpcGrammar | None:
    block = _cfg().get("trpc")
    if not isinstance(block, dict):
        return None
    return _TrpcGrammar(
        require=_c(block.get("require_import_re")),
        router_const=_c(block.get("router_const_re")),
        procedure_key=_c(block.get("procedure_key_re")),
        verb=_c(block.get("verb_re")),
        verb_window=int(block.get("verb_window") or 800),
        fallback_method=str(block.get("fallback_method") or "PROCEDURE"),
        query_method=str(block.get("query_method") or "QUERY"),
        mutation_method=str(block.get("mutation_method") or "MUTATION"),
        subscription_method=str(block.get("subscription_method") or "SUBSCRIPTION"),
        lazy_handler=_c(block.get("lazy_handler_re")),
    )


def _trpc_admits(gr: _TrpcGrammar, text: str) -> bool:
    """Whether *text* is a tRPC router file worth parsing.

    Standard gate: a canonical tRPC import (``@trpc/server`` / ``initTRPC`` /
    ``publicProcedure`` …). B66-v2 Seg D (flag-gated) adds a lazy handler-cache
    gate: a file that BOTH constructs a router (``= router(`` / createTRPCRouter)
    AND dispatches through a lazily-imported ``*.handler`` module — the cal.com
    ``UNSTABLE_HANDLER_CACHE`` shape whose relative ``router`` import fails the
    canonical gate. Both signals are required, so no stray file false-positives.
    """
    if gr.require.search(text):
        return True
    if not ownership_v2_enabled():
        return False
    return bool(gr.router_const.search(text) and gr.lazy_handler.search(text))


def _collect_trpc(text: str, path: str) -> list[_Entry]:
    gr = _trpc_grammar()
    if gr is None or not _trpc_admits(gr, text):
        return []
    base = ""
    rm = gr.router_const.search(text)
    if rm:
        base = slugify(_strip_suffixes(rm.group(1), _suffixes()))
    if not base or is_noise(base):
        base = _stem_slug(path, _suffixes())

    method_map = {
        "query": gr.query_method,
        "mutation": gr.mutation_method,
        "subscription": gr.subscription_method,
    }
    routes: list[_Route] = []
    for pm in gr.procedure_key.finditer(text):
        key = pm.group(1)
        if not key or is_noise(key):
            continue
        window = text[pm.end(): pm.end() + gr.verb_window]
        vm = gr.verb.search(window)
        method = method_map[vm.group(1).lower()] if vm else gr.fallback_method
        pattern = f"{base}.{key}" if base else key
        routes.append(_Route(pattern, method))
    if not routes:
        return []
    slug = base or slugify(routes[0].pattern.split(".")[0])
    if not slug or is_noise(slug):
        return []
    return [_Entry(slug, path, "trpc-router", routes)]


# ── Seg D — koa-router + hono ──────────────────────────────────────────────────


@dataclass
class _KoaGrammar:
    require: re.Pattern[str]
    router_ctor: re.Pattern[str]
    prefix: re.Pattern[str]
    method_call: re.Pattern[str]
    default_receivers: frozenset[str]


@dataclass
class _HonoGrammar:
    require: re.Pattern[str]
    app_ctor: re.Pattern[str]
    method_call: re.Pattern[str]
    default_receivers: frozenset[str]


@lru_cache(maxsize=1)
def _koa_grammar() -> _KoaGrammar | None:
    block = _cfg().get("koa")
    if not isinstance(block, dict):
        return None
    return _KoaGrammar(
        require=_c(block.get("require_import_re")),
        router_ctor=_c(block.get("router_ctor_re")),
        prefix=_c(block.get("prefix_re")),
        method_call=_c(block.get("method_call_re")),
        default_receivers=frozenset(
            str(r) for r in (block.get("default_receivers") or ())
        ),
    )


@lru_cache(maxsize=1)
def _hono_grammar() -> _HonoGrammar | None:
    block = _cfg().get("hono")
    if not isinstance(block, dict):
        return None
    return _HonoGrammar(
        require=_c(block.get("require_import_re")),
        app_ctor=_c(block.get("app_ctor_re")),
        method_call=_c(block.get("method_call_re")),
        default_receivers=frozenset(
            str(r) for r in (block.get("default_receivers") or ())
        ),
    )


def _collect_koa(text: str, path: str) -> list[_Entry]:
    gr = _koa_grammar()
    if gr is None or not gr.require.search(text):
        return []
    receivers = set(gr.default_receivers)
    for m in gr.router_ctor.finditer(text):
        receivers.add(m.group(1))
    prefix = ""
    pm = gr.prefix.search(text)
    if pm:
        prefix = pm.group(1)

    routes: list[_Route] = []
    for m in gr.method_call.finditer(text):
        recv, verb, p = m.group(1), m.group(2), m.group(3)
        if recv not in receivers:
            continue
        method = "DELETE" if verb == "del" else verb.upper()
        if p.startswith("/"):
            pattern = _normalize_path(prefix, p)
        else:
            pattern = p  # koa RPC-name route (outline: "documents.list")
        routes.append(_Route(pattern, method))
    if not routes:
        return []
    slug = _stem_slug(path, _suffixes())
    if not slug:
        for r in routes:
            cand = (
                _first_segment_slug(r.pattern)
                if r.pattern.startswith("/")
                else slugify(r.pattern.split(".")[0])
            )
            if cand and not is_noise(cand):
                slug = cand
                break
    if not slug:
        return []
    return [_Entry(slug, path, "koa-router", routes)]


def _collect_hono(text: str, path: str) -> list[_Entry]:
    gr = _hono_grammar()
    if gr is None or not gr.require.search(text):
        return []
    receivers = set(gr.default_receivers)
    for m in gr.app_ctor.finditer(text):
        receivers.add(m.group(1))

    routes: list[_Route] = []
    for m in gr.method_call.finditer(text):
        recv, verb, p = m.group(1), m.group(2), m.group(3)
        if recv not in receivers:
            continue
        pattern = _normalize_path(p) if p.startswith("/") else p
        routes.append(_Route(pattern, verb.upper()))
    if not routes:
        return []
    slug = _stem_slug(path, _suffixes())
    if not slug:
        for r in routes:
            cand = _first_segment_slug(r.pattern) if r.pattern.startswith("/") else ""
            if cand and not is_noise(cand):
                slug = cand
                break
    if not slug:
        return []
    return [_Entry(slug, path, "hono-app", routes)]


# ── extractor ──────────────────────────────────────────────────────────────────


def _has_marker(text: str) -> bool:
    return any(mk in text for mk in _MARKERS)


def _collect_file(text: str, path: str) -> list[_Entry]:
    entries: list[_Entry] = []
    entries.extend(_collect_nestjs(text, path))
    entries.extend(_collect_graphql(text, path))
    entries.extend(_collect_trpc(text, path))
    entries.extend(_collect_koa(text, path))
    entries.extend(_collect_hono(text, path))
    return entries


class ServerApiEntryExtractor:
    """Code-first server API surfaces -> routes_index entries (B66)."""

    name = SERVER_API_ENTRY_SOURCE

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not server_api_entries_enabled():
            return []
        exts = _extensions()
        if not exts:
            return []
        entries: list[_Entry] = []
        for raw in ctx.tracked_files:
            path = posix(raw)
            if not path.endswith(exts):
                continue
            if _should_skip_path(path):
                continue
            text = read_text(ctx.repo_path / path)
            if not text or len(text) > _MAX_BYTES:
                continue
            if not _has_marker(text):
                continue
            entries.extend(_collect_file(text, path))
        return _emit(entries)


def _emit(entries: list[_Entry]) -> list[AnchorCandidate]:
    """Group by (file, slug), union+dedup routes, emit one candidate per group.

    Deterministic: emitted in sorted (file, slug) order; each candidate's routes
    are sorted by (pattern, method), so everything downstream derives from a
    stable order across identical runs."""
    conf = _confidence()
    grouped: dict[tuple[str, str], _Entry] = {}
    for e in entries:
        if not e.slug or not e.routes:
            continue
        key = (e.file, e.slug)
        if key in grouped:
            grouped[key].routes.extend(e.routes)
        else:
            grouped[key] = _Entry(e.slug, e.file, e.grammar, list(e.routes))

    out: list[AnchorCandidate] = []
    for key in sorted(grouped):
        e = grouped[key]
        seen: dict[tuple[str, str], _Route] = {}
        for r in e.routes:
            seen.setdefault((r.pattern, r.method), r)
        routes = tuple(
            (r.pattern, r.method, e.file) for _, r in sorted(seen.items())
        )
        out.append(
            AnchorCandidate(
                name=e.slug,
                paths=(e.file,),
                source=SERVER_API_ENTRY_SOURCE,
                confidence_self=conf,
                routes=routes,
                rationale=(
                    f"{e.grammar} server API entry {e.slug!r} "
                    f"({len(routes)} route(s)) in {e.file}"
                ),
            ),
        )
    return out


__all__ = [
    "ServerApiEntryExtractor",
    "server_api_entries_enabled",
    "SERVER_API_ENTRIES_ENV",
    "SERVER_API_ENTRY_SOURCE",
]
