"""Deterministic human-readable ``Flow.display_name`` derivation.

Phase 5 (2026-05-26). ADDITIVE — never mutates any pre-existing Flow
field. Derives a short Title-Case UI label for a Flow from signals the
flow already carries, with NO LLM (cold-scan reproducibility per
``rule-cold-scan`` + cost). Priority order:

  1. **HTTP route** — when the flow's entry file matches an entry in the
     Sprint-1 ``routes_index`` (``{pattern, method, file}``), build a
     label from the HTTP verb + the humanized resource in the path.
     ``POST /api/teams/:id/invite`` → "Create Team Invite".
  2. **entry_point symbol** — split camelCase / snake_case, strip
     handler prefixes (``handle`` / ``handler`` / ``use`` / ``on``),
     Title-case. ``createCheckoutSession`` → "Create Checkout Session".
  3. **Fallback** — humanize the existing ``summary.title`` /
     ``description`` or finally the kebab ``name`` (the stable id).

The result is a display label (Title Case, no kebab, ≤ ~6 words), NOT an
id. ``Flow.name`` is the stable identifier and is never touched here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultline.models.types import Flow

# Max words in a display label (keep it short for UI chips).
_MAX_WORDS = 6

# HTTP verb → human action prefix. POST is ambiguous (create vs submit);
# we default to "Create" — the dominant REST semantic — which the
# resource noun disambiguates ("Create Team Invite" reads fine).
_VERB_ACTION = {
    "POST": "Create",
    "GET": "View",
    "PUT": "Update",
    "PATCH": "Update",
    "DELETE": "Delete",
    "HEAD": "View",
    "OPTIONS": "View",
    # Filesystem (page) routes carry method "PAGE" in routes_index.
    "PAGE": "View",
}

# Handler-ish prefixes stripped from an entry symbol before humanizing.
_SYMBOL_PREFIXES = ("handle", "handler", "use", "on")

# Framework-boilerplate entry symbols that carry no feature meaning — a
# humanized flow ``name`` reads far better than "Get Server Side Props".
# We skip the symbol tier for these and fall through to the fallback.
_NOISE_SYMBOLS = {
    "getserversideprops", "getstaticprops", "getstaticpaths",
    "getinitialprops", "default", "middleware", "loader", "action",
    "generatemetadata", "page", "layout", "handler", "main",
}

# Path segments that carry no resource meaning.
_NOISE_SEGMENTS = {"api", "v1", "v2", "v3", "trpc", "_", ""}

# A dynamic-segment token: ``:id``, ``[id]``, ``[...slug]``, ``{id}``,
# ``<id>``, or ``$id`` (TanStack). These are DROPPED from the resource.
_DYNAMIC_SEG_RE = re.compile(r"^(?::.+|\[.*\]|\{.*\}|<.+>|\$.+)$")

# camelCase / PascalCase / snake / kebab tokenizer.
_TOKEN_SPLIT_RE = re.compile(
    r"[_\-\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])",
)

# Acronyms / initialisms that should stay upper-case in a label.
_ACRONYMS = {"api", "id", "url", "ui", "ai", "sso", "oauth", "http", "sms"}

# Naive singularizer for the trailing resource noun only.
_PLURAL_OVERRIDES = {
    "settings": "settings",   # mass noun — keep as-is
    "preferences": "preferences",
    "analytics": "analytics",
}


def _titleize_token(tok: str) -> str:
    # Strip leading/trailing punctuation (periods, commas, parens) so a
    # sentence-derived fallback doesn't keep "Record." with a period.
    tok = tok.strip(".,;:!?()[]{}\"'`")
    if not tok:
        return tok
    low = tok.lower()
    if low in _ACRONYMS:
        return tok.upper()
    return tok[:1].upper() + tok[1:].lower()


def _singularize(word: str) -> str:
    low = word.lower()
    if low in _PLURAL_OVERRIDES:
        return _PLURAL_OVERRIDES[low]
    if low.endswith("ies") and len(low) > 3:
        return word[:-3] + "y"
    if low.endswith("ses") and len(low) > 3:
        return word[:-2]  # "addresses" → "address"
    if low.endswith("s") and not low.endswith("ss") and len(low) > 1:
        return word[:-1]
    return word


def _humanize_symbol(symbol: str) -> str:
    """Split + strip handler prefix + Title-case a code symbol."""
    tokens = [t for t in _TOKEN_SPLIT_RE.split(symbol) if t]
    if not tokens:
        return ""
    # Strip a leading handler-ish prefix (case-insensitive).
    if len(tokens) > 1 and tokens[0].lower() in _SYMBOL_PREFIXES:
        tokens = tokens[1:]
    tokens = tokens[:_MAX_WORDS]
    return " ".join(_titleize_token(t) for t in tokens)


def _path_segments(pattern: str) -> list[str]:
    return [s for s in pattern.split("/") if s]


def _humanize_route(method: str, pattern: str) -> str:
    """Build "<Action> <Resource>" from an HTTP method + path pattern."""
    verb = (method or "").upper()
    action = _VERB_ACTION.get(verb, "View")

    segs = _path_segments(pattern)
    # Walk from the end, skipping dynamic + noise segments, to find the
    # last meaningful resource segment. If that segment is dynamic (e.g.
    # ``/users/:id``), fall back to the preceding static one.
    meaningful: list[str] = [
        s for s in segs
        if not _DYNAMIC_SEG_RE.match(s) and s.lower() not in _NOISE_SEGMENTS
    ]
    if not meaningful:
        # Pure dynamic / noise path — degrade to the action only.
        return action

    resource_seg = meaningful[-1]
    parent_seg = meaningful[-2] if len(meaningful) > 1 else None

    res_tokens = [t for t in _TOKEN_SPLIT_RE.split(resource_seg) if t]
    if not res_tokens:
        return action

    last_is_dynamic = bool(segs) and bool(_DYNAMIC_SEG_RE.match(segs[-1]))
    # A true sub-resource is a static resource that sits AFTER a dynamic
    # id (``/teams/:id/invite`` → invite under a team). Detect by whether
    # the segment immediately preceding the resource in the RAW path is
    # dynamic.
    res_raw_idx = segs.index(resource_seg) if resource_seg in segs else -1
    has_dynamic_before = res_raw_idx > 0 and bool(
        _DYNAMIC_SEG_RE.match(segs[res_raw_idx - 1]),
    )

    is_page = verb in ("PAGE",)
    is_read = verb in ("GET", "HEAD", "PAGE")
    # Collection read = GET on a non-dynamic-terminal path (lists many).
    # Pages are always a "View", never a "List".
    is_collection_read = is_read and not last_is_dynamic and not is_page

    if is_collection_read:
        action = "List"
    elif is_read:
        action = "View"
        res_tokens[-1] = _singularize(res_tokens[-1])
    else:
        res_tokens[-1] = _singularize(res_tokens[-1])

    resource = " ".join(_titleize_token(t) for t in res_tokens)

    # Disambiguate sub-resources: "Create Team Invite" rather than the
    # bare "Create Invite" when a static parent precedes a dynamic id
    # which precedes the resource (``/teams/:id/invite``).
    if has_dynamic_before and parent_seg and not _DYNAMIC_SEG_RE.match(parent_seg):
        parent_tokens = [t for t in _TOKEN_SPLIT_RE.split(parent_seg) if t]
        if parent_tokens:
            parent_tokens[-1] = _singularize(parent_tokens[-1])
            parent = " ".join(_titleize_token(t) for t in parent_tokens)
            if parent.lower() not in resource.lower():
                resource = f"{parent} {resource}"

    words = f"{action} {resource}".split()
    return " ".join(words[:_MAX_WORDS])


def _fallback_label(flow: "Flow") -> str:
    """Humanize summary / description / kebab name as a last resort."""
    summary = getattr(flow, "summary", None)
    title = getattr(summary, "title", None) if summary is not None else None
    candidate = title or getattr(flow, "description", None) or flow.name or ""
    candidate = candidate.strip()
    if not candidate:
        return ""
    # Drop a trailing "-flow" / " flow" so the label isn't "... Flow".
    candidate = re.sub(r"[-_\s]flow$", "", candidate, flags=re.IGNORECASE)
    tokens = [t for t in _TOKEN_SPLIT_RE.split(candidate) if t][:_MAX_WORDS]
    return " ".join(_titleize_token(t) for t in tokens)


def _match_route(
    entry_file: str | None,
    routes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not entry_file or not routes:
        return None
    for r in routes:
        if str(r.get("file") or "") == entry_file and r.get("pattern"):
            return r
    return None


def derive_display_name(
    flow: "Flow",
    routes: list[dict[str, Any]] | None = None,
) -> str:
    """Deterministically derive a Title-Case display label for ``flow``.

    Reads ``flow.entry_point`` (or the legacy ``entry`` / scalar entry
    fields), the optional ``routes`` (Sprint-1 ``routes_index``), and the
    ``summary`` / ``name`` fallbacks. Pure — does not mutate ``flow``.
    """
    routes = routes or []

    # Resolve the entry file + symbol from the richest available source.
    entry_file: str | None = None
    entry_symbol: str | None = None
    ep = getattr(flow, "entry_point", None)
    if ep is not None:
        entry_file = ep.path or None
        entry_symbol = ep.symbol or None
    if entry_file is None and getattr(flow, "entry", None):
        entry_file = flow.entry.get("file") or None
        entry_symbol = entry_symbol or flow.entry.get("symbol")
    if entry_file is None:
        entry_file = flow.entry_point_file or None
    # Last resort for the symbol: the entry-role flow_symbol_attribution
    # (older/thin flows carry the symbol here, not on entry_point).
    if not entry_symbol:
        for fsa in getattr(flow, "flow_symbol_attributions", None) or []:
            sym = getattr(fsa, "symbol", None)
            if sym and sym != "<file>" and (
                getattr(fsa, "role", None) == "entry" or entry_symbol is None
            ):
                entry_symbol = sym
                if getattr(fsa, "role", None) == "entry":
                    break

    # 1. HTTP route.
    route = _match_route(entry_file, routes)
    if route is not None:
        label = _humanize_route(
            str(route.get("method") or ""), str(route.get("pattern") or ""),
        )
        if label:
            return label

    # 2. entry_point symbol (skip framework-boilerplate symbols).
    if entry_symbol and entry_symbol.lower() not in _NOISE_SYMBOLS:
        label = _humanize_symbol(entry_symbol)
        if label:
            return label

    # 3. Fallback (summary / description / kebab name).
    return _fallback_label(flow)


__all__ = ["derive_display_name"]
