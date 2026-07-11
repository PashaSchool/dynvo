"""B30 — deterministic verb+resource flow naming ($0, output layer).

Operator audit class 2 (wave-14, 2026-07-10): route-derived flows carry
HTTP path slugs verbatim as their name — ``api-account-passkeys-flow``,
``api-file-s3-get-presigned-get-url-proxy-flow`` (papermark: 208/415 =
50% of the board; cal.com 203, supabase 92, kan 16) — plus single-noun
page echoes (``branding-flow``, ``boards-flow``) and the absurd
``flow-flow`` (from ``flow.tsx``). A PM reads a path slug and learns
nothing the URL wouldn't say; the intent verb is absent. Per
``rule-flow-naming`` a flow name must be a verb-led capability label
(``manage-X-flow``), never a dir echo.

This stage renames exactly that class, mechanically (no LLM, no
vocabularies — the only token maps are the CLOSED HTTP-method set):

  * **route-derived** flows (the flow's provenance carries a URL
    pattern: its seed ``description`` route, a ``routes_index`` entry
    for its entry file, or the file-system routing convention) whose
    CURRENT name is a mechanical echo of that route/path get
    ``<verb>-<resource>-flow``:
      - verb from the HTTP method — GET → ``view`` (``browse`` when the
        path is a plural-shaped collection), POST → ``create``,
        PUT/PATCH → ``update``, DELETE → ``delete``, page routes →
        ``view``. When the method is structurally unknowable (a Next
        Pages API handler that multiplexes methods in one function) the
        handler source is sniffed for ``req.method`` comparisons /
        exported method handlers; a single method maps as above,
        several (or none) map to the honest umbrella ``manage``.
      - resource = the meaningful path segments, EXCLUDING boilerplate
        (``api`` / ``v1``..``vN`` / ``trpc`` / ``index``). Dynamic
        params: a terminal ``[id]`` becomes ``by-id``; interior and
        catch-all params drop (the static segment before a param is
        singularized — it names the instance).
      ``api-account-passkeys-flow`` → ``manage-account-passkeys-flow``;
      ``post-api-auth-verify-code-flow`` → ``create-auth-verify-code-flow``.
  * **non-route path echoes** (a flow named after its FILE — the
    ``flow-flow`` / dir-stem class) are renamed from the exported
    symbol the extractor recorded (``entry_point.symbol``), kebab-cased
    — the symbol IS the author's name for the capability. When only a
    dir/file is known (no usable symbol), the current name is KEPT —
    honest fallback, never invent.
  * Flows already carrying a semantic name (LLM-authored,
    symbol-derived, or anything that is NOT an echo of the flow's own
    route/path) are untouched by construction: eligibility requires the
    current name to EQUAL a mechanical slug of the flow's provenance.

Collision ladder (BEFORE ordinals): when a proposed name would collide
(within the renamed set or against an untouched flow's name), every
colliding renamed flow is qualified by its owning developer-feature
token (``notification-slack`` vs ``notification-discord`` — the wave-14
audit showed the feature token separates 137/151 ordinal twins); a
minimal stable ordinal (``-2``, ``-3``…) is the LAST resort and a flow
that still cannot win a unique name keeps its old one. Uniqueness of
``flows[].name`` (the stage-5.5 invariant) is preserved.

Placement + additivity
----------------------
Runs at the very END of the finalize phase (after 6.97c flow-loc,
immediately before Stage 7 output), so every downstream consumer of the
OLD names — UF rollup, journey lattice, lineage, dedup, ids — has
already run: the ONLY output-JSON change is ``flows[].name`` /
``display_name`` / ``short_label``. ``flow.id`` / ``flow.uuid`` (the
join keys for ``feature_flow_edges``, ``path_index``,
``user_flows[].member_flow_ids``) are NEVER touched. Flow objects are
shared between ``flows[]`` and ``developer_features[].flows[]``, so one
in-place mutation updates both views.

Kill-switch: ``FAULTLINE_FLOW_NAME_V2=0`` skips the stage — byte-
identical output. Deterministic: list-ordered iteration, sorted
membership structures, regex-only source sniffing, no network, no LLM.
Telemetry goes to the stage artifact/log only (never ``scan_meta``), so
the flag-ON vs flag-OFF diff is exactly the three name fields.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.flow_expansion.flow_display_name import (
    _NOISE_SYMBOLS,
    _singularize,
)
from faultline.pipeline_v2.indexes import _derive_route_from_path

if TYPE_CHECKING:
    from faultline.models.types import Flow

__all__ = [
    "FLOW_NAME_V2_ENV",
    "flow_name_v2_enabled",
    "apply_flow_name_v2",
]

FLOW_NAME_V2_ENV = "FAULTLINE_FLOW_NAME_V2"


def flow_name_v2_enabled() -> bool:
    """Default ON; ``FAULTLINE_FLOW_NAME_V2=0`` (or false/False) disables."""
    return os.environ.get(FLOW_NAME_V2_ENV, "1").strip() not in {
        "0", "false", "False",
    }


# ── mechanical constants (closed sets, not vocabularies) ────────────────

#: The closed HTTP method set. This is protocol grammar, not a word list.
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_HTTP_METHOD_SET = frozenset(_HTTP_METHODS)

#: HTTP method → kebab intent verb. ``GET`` resolves to ``view`` or
#: ``browse`` in :func:`_verb_for` (instance vs collection); ``None``
#: method (structurally unknowable multiplexing handler) resolves to
#: the honest umbrella ``manage`` per ``rule-flow-naming``.
_METHOD_VERB = {
    "GET": "view",
    "HEAD": "view",
    "OPTIONS": "view",
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
    "PAGE": "view",
}

#: URL segments that carry no resource meaning (mirror of the display-name
#: deriver's ``_NOISE_SEGMENTS`` + ``index`` marker). ``v1``..``vN`` are
#: matched by regex below.
_NOISE_SEGMENTS = frozenset({"api", "trpc", "index", "_"})
_VERSION_SEG_RE = re.compile(r"^v\d+$")

#: A dynamic route segment in any convention this pipeline emits:
#: ``:id`` (routes_index / derived patterns), ``[id]`` / ``[...slug]`` /
#: ``[[...slug]]`` (raw fs segments), ``{id}``, ``<id>``, ``$id``.
_DYNAMIC_SEG_RE = re.compile(r"^(?::.+|\[.*\]|\{.*\}|<.+>|\$.+)$")

#: Catch-all param declared in the ENTRY FILE path (``[...slug]`` /
#: ``[[...slug]]``) — pattern normalisation erases the ``...``, so the
#: file path is the only place the catch-all shape survives.
_CATCHALL_FILE_RE = re.compile(r"\[{1,2}\.{3}([^\]]+?)\]{1,2}")

#: A per-verb leaf file (``_get.ts`` / ``get.ts``) — names the method,
#: not a URL segment (same rule as ``indexes._VERB_LEAF_RE``).
_VERB_LEAF_RE = re.compile(
    r"^_?(get|post|put|patch|delete|head|options)$", re.IGNORECASE,
)

#: A provenance ``description`` that IS a route: ``/path`` or
#: ``METHOD /path`` — exactly the two shapes the Stage-3 profile seeder
#: writes (``FlowSpec.description = entry.route``). Prose never matches.
_DESC_ROUTE_RE = re.compile(
    r"^(?:(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)?(/\S*)$",
)

#: ``req.method`` / ``request.method`` compared against a method literal
#: (both operand orders) — the Pages-Router multiplexing convention.
_REQ_METHOD_LIT_RE = re.compile(
    r"(?:req|request)\s*\.\s*method\s*[!=]==?\s*[\"'](%s)[\"']"
    % "|".join(_HTTP_METHODS),
)
_LIT_REQ_METHOD_RE = re.compile(
    r"[\"'](%s)[\"']\s*[!=]==?\s*(?:req|request)\s*\.\s*method"
    % "|".join(_HTTP_METHODS),
)
#: ``switch (req.method) { case "POST": … }`` — collect case labels only
#: when the switch head is present.
_SWITCH_HEAD_RE = re.compile(r"switch\s*\(\s*(?:req|request)\s*\.\s*method")
_CASE_LABEL_RE = re.compile(r"case\s+[\"'](%s)[\"']" % "|".join(_HTTP_METHODS))
#: App-Router ``route.ts`` exported per-method handlers.
_EXPORTED_METHOD_RE = re.compile(
    r"export\s+(?:async\s+)?(?:function|const|let|var)\s+(%s)\b"
    % "|".join(_HTTP_METHODS),
)

#: Generic fillers dropped from a feature-token qualifier (mirror of the
#: stage-5.5 ``_CTX_STOP_TOKENS`` mechanism).
_QUALIFIER_STOP_TOKENS = frozenset({
    "flow", "flows", "api", "page", "pages", "route", "routes", "router",
    "handler", "handlers", "view", "views", "endpoint", "endpoints",
})

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CAMEL_SPLIT_RE = re.compile(
    r"[_\-\s./]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])",
)
_ORDINAL_TAIL_RE = re.compile(r"-\d+$")

#: Bounded handler-source read for the method sniff (guard, not a tuning
#: knob — route handlers are small; this only caps pathological files).
_MAX_SNIFF_BYTES = 1_000_000

#: B46 UF-name hygiene kill-switch (default OFF; the canonical reader — Stage
#: 6.7 + synth_quality import it). ``=1`` collapses the doubled route/file-stem
#: token run at the flow-name root (a component file that camel-restates its
#: directory — ``settings/accounts/SettingsAccounts`` -> 'settings accounts
#: settings accounts') and arms the UF-level bare-plural + inherited-ordinal
#: fixes. OFF ⇒ byte-identical to pre-B46 flow + UF names.
UF_NAME_HYGIENE_ENV = "FAULTLINE_UF_NAME_HYGIENE"


def uf_name_hygiene_enabled() -> bool:
    """B46 — kill garbage UF names (doubled concat / bare pluralized leaf /
    inherited Stage-5.5 ordinal). Default OFF; ``FAULTLINE_UF_NAME_HYGIENE=1``
    arms the root + UF-level fixes. OFF ⇒ flow/UF names byte-identical."""
    return os.environ.get(UF_NAME_HYGIENE_ENV, "0").strip().lower() in {
        "1", "true",
    }


# ── small pure helpers ──────────────────────────────────────────────────


def _slugify(text: str) -> str:
    """Kebab slug over camelCase / snake / path separators."""
    if not text:
        return ""
    parts = [p for p in _CAMEL_SPLIT_RE.split(text) if p]
    return _SLUG_RE.sub("-", "-".join(parts).lower()).strip("-")


def _plain_slug(text: str) -> str:
    """The Stage-3 seeder's slug rule — lowercase, NO camel split
    (``:caseId`` → ``caseid``). Echo candidates must cover this form."""
    if not text:
        return ""
    return _SLUG_RE.sub("-", text.lower()).strip("-")


def _name_key(name: str) -> str:
    return (name or "").strip().lower()


def _strip_flow_suffix(name: str) -> str:
    return re.sub(r"-flows?$", "", name or "")


def _entry_file_of(flow: "Flow") -> str:
    ep = getattr(flow, "entry_point", None)
    if ep is not None and getattr(ep, "path", None):
        return str(ep.path).replace("\\", "/")
    return str(flow.entry_point_file or "").replace("\\", "/")


def _entry_symbol_of(flow: "Flow") -> str:
    ep = getattr(flow, "entry_point", None)
    if ep is not None and getattr(ep, "symbol", None):
        return str(ep.symbol)
    entry = getattr(flow, "entry", None)
    if isinstance(entry, dict) and entry.get("symbol"):
        return str(entry["symbol"])
    for fsa in getattr(flow, "flow_symbol_attributions", None) or []:
        sym = getattr(fsa, "symbol", None)
        if sym and getattr(fsa, "role", None) == "entry":
            return str(sym)
    return ""


# ── provenance: route candidates per flow ───────────────────────────────


def _route_candidates(
    flow: "Flow",
    entry_file: str,
    routes_by_file: dict[str, list[dict[str, Any]]],
) -> list[tuple[str, str]]:
    """``(pattern, method_hint)`` candidates, provenance-ordered.

    Order: the flow's OWN seed description route first (it names exactly
    the capability this flow was minted for — a FastAPI module hosts many
    routes in one file, so per-file lookups alone are ambiguous), then
    the ``routes_index`` entries for the entry file, then the pure
    file-system derivation. ``method_hint`` may be ``""`` (unknown).
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(pattern: str, method: str) -> None:
        pattern = (pattern or "").strip()
        if not pattern:
            return
        key = (pattern, (method or "").upper())
        if key in seen:
            return
        seen.add(key)
        out.append((pattern, (method or "").upper()))

    desc = (flow.description or "").strip()
    m = _DESC_ROUTE_RE.match(desc)
    if m:
        _add(m.group(2), m.group(1) or "")

    for entry in routes_by_file.get(entry_file, ()):  # routes_index
        _add(str(entry.get("pattern") or ""), str(entry.get("method") or ""))

    if entry_file:
        derived = _derive_route_from_path(entry_file)
        if derived is not None:
            _add(derived[0], derived[1])

    return out


def _echo_slugs_for_route(pattern: str, method: str) -> list[str]:
    """The mechanical slugs Stage 3 / 5.5 could have minted for a route.

    Both slug conventions are covered: the seeder's plain lowercase form
    (``:caseId`` → ``caseid``) and the camel-split form (``case-id``).
    """
    slugs: list[str] = []
    for slug_fn in (_plain_slug, _slugify):
        base = slug_fn(pattern)
        if not base:
            continue
        for cand in (base, slug_fn(f"{method} {pattern}") if method else ""):
            if cand and cand not in slugs:
                slugs.append(cand)
    return slugs


def _file_echo_slugs(entry_file: str) -> list[str]:
    """Mechanical slugs a FILE-named flow could carry (stem / dir forms)."""
    if not entry_file:
        return []
    segs = [s for s in entry_file.split("/") if s]
    if not segs:
        return []
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", segs[-1])
    out: list[str] = []
    for cand in (
        stem,
        segs[-2] if len(segs) >= 2 else "",
        f"{segs[-2]}-{stem}" if len(segs) >= 2 else "",
        "/".join(segs),
    ):
        for slug_fn in (_plain_slug, _slugify):
            s = slug_fn(cand)
            if s and s not in out:
                out.append(s)
    return out


def _is_echo(name_core: str, candidates: list[str]) -> bool:
    """Is ``name_core`` a mechanical echo of any provenance slug?

    Exact match, exact match after stripping a stage-5.5 ordinal tail
    (``…-2``), or — for multi-token candidates only (an unambiguous
    path spine; single tokens would over-match semantic names like
    ``login-with-google``) — a prefix match tolerating the context
    tokens the stage-5.5 disambiguator appends.
    """
    if not name_core:
        return False
    stripped = _ORDINAL_TAIL_RE.sub("", name_core)
    for cand in candidates:
        if not cand:
            continue
        if name_core == cand or stripped == cand:
            return True
        if "-" in cand and (
            name_core.startswith(cand + "-") or stripped.startswith(cand + "-")
        ):
            return True
    return False


# ── method resolution ───────────────────────────────────────────────────


def _sniff_methods(repo_path: Path | None, entry_file: str) -> list[str]:
    """HTTP methods a handler SOURCE declares, in first-seen order.

    Reads the entry file once (bounded) and collects method literals
    from ``req.method`` comparisons, ``switch (req.method)`` case
    labels, and exported per-method handlers. Empty on any read failure
    — the caller degrades to the honest ``manage`` umbrella.
    """
    if repo_path is None or not entry_file:
        return []
    try:
        text = (Path(repo_path) / entry_file).read_text(
            encoding="utf-8", errors="ignore",
        )
    except OSError:
        return []
    if len(text) > _MAX_SNIFF_BYTES:
        text = text[:_MAX_SNIFF_BYTES]
    found: list[str] = []

    def _collect(values: list[str]) -> None:
        for v in values:
            u = v.upper()
            if u not in found:
                found.append(u)

    _collect(_REQ_METHOD_LIT_RE.findall(text))
    _collect(_LIT_REQ_METHOD_RE.findall(text))
    if _SWITCH_HEAD_RE.search(text):
        _collect(_CASE_LABEL_RE.findall(text))
    _collect(_EXPORTED_METHOD_RE.findall(text))
    return found


def _resolve_method(
    flow: "Flow",
    entry_file: str,
    pattern: str,
    method_hint: str,
    repo_path: Path | None,
) -> str | None:
    """The flow's HTTP method, or ``None`` when structurally unknowable.

    Trust order: an explicit method in the seed provenance (description
    ``METHOD /path`` → ``method_hint``; App-Router per-method entry
    symbol; per-verb leaf file), then ``PAGE`` for non-API page routes,
    then a bounded source sniff for multiplexing handlers. ``None``
    (→ ``manage``) when several methods share one handler or nothing is
    declared — never guess.
    """
    hint = (method_hint or "").upper()
    entry_symbol = _entry_symbol_of(flow)
    stem = ""
    if entry_file:
        fname = entry_file.rsplit("/", 1)[-1]
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", fname)

    if hint in _HTTP_METHOD_SET and hint != "GET":
        # Non-GET hints are always explicit (seed route / verb leaf /
        # Pass-A extractor tuple) — GET alone may be the routes_index
        # "read default" for App-Router route.ts, handled below.
        return hint
    if entry_symbol in _HTTP_METHOD_SET:
        return entry_symbol
    verb_leaf = _VERB_LEAF_RE.match(stem)
    if verb_leaf:
        return verb_leaf.group(1).upper()

    first_seg = ""
    for seg in pattern.split("/"):
        if seg:
            first_seg = seg.lower()
            break
    if hint == "PAGE":
        if first_seg != "api":
            return "PAGE"
        # Pages-Router API handler — one function multiplexes methods.
        sniffed = _sniff_methods(repo_path, entry_file)
        return sniffed[0] if len(sniffed) == 1 else None
    if hint == "GET":
        if stem in {"route", "+server"}:
            # App-Router read-default: verify against the exported set.
            sniffed = _sniff_methods(repo_path, entry_file)
            if len(sniffed) == 1:
                return sniffed[0]
            if sniffed:
                return None
        return "GET"
    if not hint:
        # A seed description route carries no method — backfill the hint
        # from the file-system routing convention (PAGE / verb leaf)
        # before degrading to the source sniff.
        if entry_file:
            derived = _derive_route_from_path(entry_file)
            if derived is not None and derived[1] and derived[1] != hint:
                return _resolve_method(
                    flow, entry_file, pattern, derived[1], repo_path,
                )
        sniffed = _sniff_methods(repo_path, entry_file)
        return sniffed[0] if len(sniffed) == 1 else None
    return None


# ── verb + resource synthesis ───────────────────────────────────────────


def _verb_for(method: str | None, resource_tokens: list[str]) -> str:
    """Intent verb for ``method``; collection GETs read as ``browse``."""
    if method is None:
        return "manage"
    verb = _METHOD_VERB.get(method, "manage")
    if method == "GET" and resource_tokens:
        last = resource_tokens[-1]
        if not last.startswith("by-") and _singularize(last) != last:
            return "browse"
    return verb


def _resource_tokens(pattern: str, entry_file: str) -> list[str]:
    """Meaningful kebab resource tokens from a URL pattern.

    Boilerplate segments drop; a static segment immediately followed by
    a dynamic param is singularized (it names the instance); a terminal
    plain param becomes ``by-<param>``; interior and catch-all params
    drop. Empty when the pattern carries no static resource at all
    (caller keeps the old name — honest fallback).
    """
    catchall = {
        _slugify(m) for m in _CATCHALL_FILE_RE.findall(entry_file or "")
    }
    # A per-verb leaf segment (``_patch`` / ``get``) names the METHOD,
    # not a resource (same rule as ``indexes._VERB_LEAF_RE``) — drop it
    # before terminal-ness is computed so ``/api/keys/:id/_patch`` reads
    # as an instance of ``keys``.
    raw = [
        s for s in pattern.split("/")
        if s and not _VERB_LEAF_RE.match(s)
    ]
    out: list[str] = []
    static_flags: list[bool] = []
    n = len(raw)
    for i, seg in enumerate(raw):
        if _DYNAMIC_SEG_RE.match(seg):
            pname = _slugify(seg.strip(":[]{}<>$."))
            # The static segment before a param names the instance.
            if out and static_flags[-1]:
                out[-1] = _singularize(out[-1])
            is_terminal = i == n - 1
            if (
                is_terminal
                and pname
                and pname not in catchall
                and not (out and pname == out[-1])
                and not (out and _singularize(pname) == out[-1])
            ):
                out.append(f"by-{pname}")
                static_flags.append(False)
            continue
        low = seg.lower()
        if low in _NOISE_SEGMENTS or _VERSION_SEG_RE.match(low):
            continue
        s = _slugify(seg)
        if not s or (out and s == out[-1]):
            continue
        # B46 — a component file-stem that camel-restates its directory path
        # (``settings/accounts/SettingsAccounts`` -> the last segment slugs to
        # 'settings-accounts', an ADJACENT DUPLICATE of the dir tokens already
        # accumulated) produces the doubled 'settings accounts settings
        # accounts' garbage. Drop only the leading sub-tokens of a camel-split
        # segment that duplicate the accumulated tail — a partial or
        # non-adjacent repeat (legit 'teams/TeamMembers' -> teams != team) is
        # left intact. Root of the twenty 'account settingsaccounts' UF concat.
        if uf_name_hygiene_enabled() and "-" in s:
            sub = s.split("-")
            flat_tail = [p for tok in out for p in tok.split("-")]
            k = 0
            for cand in range(min(len(sub), len(flat_tail)), 0, -1):
                if flat_tail[-cand:] == sub[:cand]:
                    k = cand
                    break
            sub = sub[k:]
            if not sub:
                continue
            s = "-".join(sub)
        out.append(s)
        static_flags.append(True)
    if not any(static_flags):
        return []  # params-only path — nothing honest to name
    return out


def _compose_name(verb: str, resource_tokens: list[str]) -> str:
    tokens = list(resource_tokens)
    if tokens and tokens[0] == verb:
        tokens = tokens[1:]  # never emit view-view-…
    core = "-".join([verb, *tokens]) if tokens else verb
    return f"{core}-flow"


def _symbol_name(symbol: str) -> str:
    """Kebab flow name from an exported handler symbol, or ``""``."""
    if not symbol or symbol.startswith("<"):
        return ""
    low = symbol.lower()
    if low in _NOISE_SYMBOLS or _VERB_LEAF_RE.match(low):
        return ""
    slug = _slugify(symbol)
    if not slug:
        return ""
    return f"{slug}-flow"


# ── collision ladder (feature token BEFORE ordinal) ─────────────────────


def _feature_qualified(core: str, primary_feature: str | None) -> str | None:
    """``<core>-<feature-tokens>-flow`` or ``None`` (no usable tokens)."""
    tokens = [
        t for t in _slugify(primary_feature or "").split("-")
        if t and t not in _QUALIFIER_STOP_TOKENS
    ]
    base_tokens = set(core.split("-"))
    kept = [t for t in tokens if t not in base_tokens]
    if not kept:
        return None
    return f"{core}-{'-'.join(kept)}-flow"


# ── public entry point ──────────────────────────────────────────────────


def apply_flow_name_v2(
    flows: list["Flow"],
    routes_index: list[dict[str, Any]] | None,
    repo_path: Path | str | None,
) -> dict[str, Any]:
    """Rename path-echo flows to verb+resource IN PLACE; return telemetry.

    Mutates only ``flow.name`` (+ the ``display_name`` / ``short_label``
    kebab mirrors). Never touches ``flow.id`` / ``flow.uuid`` or any
    non-name field. See the module docstring for the full mechanism.
    """
    repo = Path(repo_path) if repo_path is not None else None
    routes_by_file: dict[str, list[dict[str, Any]]] = {}
    for entry in routes_index or []:
        f = str(entry.get("file") or "")
        if f:
            routes_by_file.setdefault(f, []).append(entry)

    tele: dict[str, Any] = {
        "flows_total": len(flows),
        "route_echo_flows": 0,
        "file_echo_flows": 0,
        "renamed_route": 0,
        "renamed_symbol": 0,
        "kept_honest_fallback": 0,
        "collision_feature_qualified": 0,
        "collision_ordinal": 0,
        "samples": [],
    }

    # Pass 1 — propose. ``proposals[i]`` is ``(flow, proposed_core, arm)``
    # where ``arm`` is ``"route"`` or ``"symbol"``.
    proposals: list[tuple[Any, str, str]] = []
    proposed_idx: set[int] = set()
    for idx, flow in enumerate(flows):
        name = flow.name or ""
        if not name or not (getattr(flow, "uuid", "") or ""):
            continue  # uuid is the UF-member join key fallback — never risk it
        entry_file = _entry_file_of(flow)
        if not entry_file:
            continue  # no structural provenance — LLM-legacy flow
        name_core = _strip_flow_suffix(name)

        route_cands = _route_candidates(flow, entry_file, routes_by_file)
        matched_route: tuple[str, str] | None = None
        for pattern, method_hint in route_cands:
            if _is_echo(name_core, _echo_slugs_for_route(pattern, method_hint)):
                matched_route = (pattern, method_hint)
                break
        if matched_route is None and route_cands:
            # The Stage-3 seeder's EMPTY-basis literal: a root/noise-only
            # route slugs to "" and the seed falls back to the bare name
            # ``flow-flow`` (the wave-14 absurd exhibit on 4 repos). The
            # name IS a route echo — of an unsluggable route.
            if _ORDINAL_TAIL_RE.sub("", name_core) == "flow":
                matched_route = route_cands[0]

        if matched_route is not None:
            tele["route_echo_flows"] += 1
            pattern, method_hint = matched_route
            resource = _resource_tokens(pattern, entry_file)
            if not resource:
                # Route carries no static resource (root page ``/``,
                # params-only, all-boilerplate). The entry symbol is the
                # author's own name for the surface — use it when it is
                # usable; otherwise keep the old name (honest fallback).
                sym_name = _symbol_name(_entry_symbol_of(flow))
                if sym_name and _name_key(sym_name) != _name_key(name):
                    proposals.append(
                        (flow, _strip_flow_suffix(sym_name), "symbol"),
                    )
                    proposed_idx.add(idx)
                else:
                    tele["kept_honest_fallback"] += 1
                continue
            method = _resolve_method(
                flow, entry_file, pattern, method_hint, repo,
            )
            verb = _verb_for(method, resource)
            new_name = _compose_name(verb, resource)
            if _name_key(new_name) != _name_key(name):
                proposals.append((flow, _strip_flow_suffix(new_name), "route"))
                proposed_idx.add(idx)
            continue

        # Non-route path echo (file-stem / dir-token class).
        if _is_echo(name_core, _file_echo_slugs(entry_file)):
            tele["file_echo_flows"] += 1
            symbol = _entry_symbol_of(flow)
            # Anti-case guard 1: a name that already embeds the entry
            # symbol IS symbol-named (possibly with a stage-5.5 context
            # suffix) — renaming would strip legitimate context
            # (``decrypt-v1-credentials`` must not become ``decrypt-v1``).
            if symbol and _is_echo(
                name_core,
                [s for s in (_plain_slug(symbol), _slugify(symbol)) if s],
            ):
                continue
            sym_name = _symbol_name(symbol)
            if not sym_name:
                tele["kept_honest_fallback"] += 1
                continue  # only a dir/file is known — keep, do not invent
            # Anti-case guard 2: the symbol arm targets the audit's hard
            # core only — SINGLE-token file echoes (``flow-flow``,
            # ``signin-flow``) — and only when the symbol strictly adds
            # information. Multi-token file-stem coincidences
            # (``parse-groups`` from ``parseGroups.ts``,
            # ``select-plan`` from ``select-plan.tsx``) are already
            # capability-shaped names; renaming them from a symbol
            # would churn, not improve — keep the original (honest).
            stripped_core = _ORDINAL_TAIL_RE.sub("", name_core)
            if len(stripped_core.split("-")) > 1 or len(
                _strip_flow_suffix(sym_name).split("-"),
            ) <= 1:
                tele["kept_honest_fallback"] += 1
                continue
            if _name_key(sym_name) != _name_key(name):
                proposals.append((flow, _strip_flow_suffix(sym_name), "symbol"))
                proposed_idx.add(idx)

    # Pass 2 — collision ladder + apply. Names of untouched flows are
    # reserved; a proposal that collides is feature-qualified, then
    # ordinal-suffixed, then abandoned (old name kept).
    taken: set[str] = {
        _name_key(fl.name)
        for i, fl in enumerate(flows)
        if i not in proposed_idx and fl.name
    }
    core_counts: dict[str, int] = {}
    for _fl, core, _arm in proposals:
        k = _name_key(core)
        core_counts[k] = core_counts.get(k, 0) + 1

    for flow, core, arm in proposals:
        candidate = f"{core}-flow"
        used_qualifier = False
        if core_counts[_name_key(core)] > 1 or _name_key(candidate) in taken:
            qualified = _feature_qualified(core, flow.primary_feature)
            if qualified is not None:
                candidate = qualified
                used_qualifier = True
        if _name_key(candidate) in taken:
            base = _strip_flow_suffix(candidate)
            ordinal = 2
            candidate = f"{base}-{ordinal}-flow"
            while _name_key(candidate) in taken:
                ordinal += 1
                candidate = f"{base}-{ordinal}-flow"
            tele["collision_ordinal"] += 1
        if used_qualifier:
            tele["collision_feature_qualified"] += 1

        old = flow.name
        taken.add(_name_key(candidate))
        flow.name = candidate
        if not flow.display_name or flow.display_name == old:
            flow.display_name = candidate
        old_short = _strip_flow_suffix(old)
        if flow.short_label in (old, old_short, ""):
            flow.short_label = _strip_flow_suffix(candidate)
        tele["renamed_route" if arm == "route" else "renamed_symbol"] += 1
        if len(tele["samples"]) < 20:
            tele["samples"].append({"from": old, "to": candidate})

    tele["renamed_total"] = tele["renamed_route"] + tele["renamed_symbol"]
    return tele
