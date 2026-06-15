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
    # Bare HTTP-verb route handlers (Next.js App Router ``route.ts``
    # exports ``GET`` / ``POST`` / … and Nest/Express handlers named
    # ``getHandler`` / ``postHandler``). Humanizing these yields labels
    # ("Get Handler", "Post") strictly worse than the kebab ``name``,
    # so we demote them to fall through to the route / fallback tiers.
    "get", "post", "put", "patch", "delete", "head", "options",
    "gethandler", "posthandler", "puthandler", "patchhandler",
    "deletehandler", "headhandler", "optionshandler",
}

# Trailing dangling conjunctions/prepositions — when the 6-word cap (or
# a singularizer) leaves one of these as the LAST word, drop it so we
# never emit "... Credentials Or" / "... Settings With".
_DANGLING_TRAILERS = {"and", "or", "with", "for", "to", "of", "the", "a", "an"}

# A version-suffix on a DTO/symbol token sequence: a run of trailing
# all-numeric tokens (``CancelBookingOutput_2024_08_13`` →
# "Cancel Booking Output 2024 08 13"). Such date/version tails make the
# label worse than the kebab name, so a symbol whose humanized form ends
# in ≥2 numeric tokens is demoted to fall through.
_NUMERIC_TOKEN_RE = re.compile(r"^\d+$")

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
    # Already-singular Latinate ``-us`` / ``-is`` endings — never strip the
    # tail (status→statu, focus→focu, analysis→analysi all regressed here).
    # ``-ss`` is guarded by the final rule below.
    if low.endswith(("us", "is", "ous", "ius")):
        return word
    if low.endswith("ies") and len(low) > 3:
        return word[:-3] + "y"
    # Sibilant ``-es`` collapses to its stem (addresses→address,
    # classes→class, boxes→box); plain ``-es`` keeps its ``e`` below
    # (cases→case, not cas — the #59 over-strip).
    if low.endswith(("sses", "shes", "ches", "xes", "zzes")):
        return word[:-2]
    if low.endswith("s") and not low.endswith("ss") and len(low) > 1:
        return word[:-1]
    return word


def _strip_dangling_trailer(words: list[str]) -> list[str]:
    """Drop a trailing dangling conjunction/preposition.

    Guards against the ``_MAX_WORDS`` cap (or a singularizer) leaving a
    label ending in "Or" / "And" / "With" — strictly worse than ending
    on the noun before it. Strips repeatedly (a label can end in two).
    """
    while words and words[-1].lower() in _DANGLING_TRAILERS:
        words = words[:-1]
    return words


def _humanize_symbol(symbol: str) -> str:
    """Split + strip handler prefix + Title-case a code symbol.

    Returns ``""`` for symbols that would humanize to a label WORSE than
    the kebab ``name`` (caller falls through): ``<...>`` sentinels and
    version/date-suffixed DTOs (trailing run of ≥2 numeric tokens).
    """
    # Sentinel symbols (``<file>``, ``<deep:...>``, ``<anonymous>``) are
    # not real identifiers — never humanize them; fall through.
    if symbol.startswith("<"):
        return ""
    tokens = [t for t in _TOKEN_SPLIT_RE.split(symbol) if t]
    if not tokens:
        return ""
    # Strip a leading handler-ish prefix (case-insensitive).
    if len(tokens) > 1 and tokens[0].lower() in _SYMBOL_PREFIXES:
        tokens = tokens[1:]
    # Demote version/date-suffixed DTOs: a trailing run of ≥2 numeric
    # tokens (``CancelBookingOutput_2024_08_13``) reads worse than name.
    trailing_numeric = 0
    for tok in reversed(tokens):
        if _NUMERIC_TOKEN_RE.match(tok):
            trailing_numeric += 1
        else:
            break
    if trailing_numeric >= 2:
        return ""
    tokens = tokens[:_MAX_WORDS]
    words = _strip_dangling_trailer(
        [_titleize_token(t) for t in tokens],
    )
    return " ".join(words)


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

    words = _strip_dangling_trailer(f"{action} {resource}".split()[:_MAX_WORDS])
    return " ".join(words)


def _humanize_file_basename(entry_file: str | None) -> str:
    """Humanize the entry file's path tail into a label.

    Used when the symbol tier is a ``<file>`` sentinel — the file path
    is the only real signal. ``apple-calendar/webhook.ts`` → "Apple
    Calendar Webhook". We take the leaf directory + filename stem so a
    bare ``route.ts`` / ``page.tsx`` still picks up its parent dir
    ("teams/route.ts" → "Teams"). Dynamic + noise segments are dropped.
    """
    if not entry_file:
        return ""
    segs = [s for s in entry_file.split("/") if s]
    if not segs:
        return ""
    # Strip the extension off the filename stem.
    fname = segs[-1]
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", fname)
    # Convention markers (route/page/index/etc.) carry no meaning — drop
    # them and lean on the parent directory.
    parts: list[str] = segs[:-1]
    if stem and stem not in {
        "route", "page", "layout", "index", "handler", "default",
        "+page", "+server", "_app", "_document", "middleware",
    }:
        parts.append(stem)
    # Keep the last two meaningful (non-dynamic, non-noise) segments so
    # we get "Apple Calendar Webhook" not the whole repo path.
    meaningful = [
        s for s in parts
        if not _DYNAMIC_SEG_RE.match(s) and s.lower() not in _NOISE_SEGMENTS
    ]
    if not meaningful:
        return ""
    meaningful = meaningful[-2:]
    tokens: list[str] = []
    for seg in meaningful:
        tokens.extend(t for t in _TOKEN_SPLIT_RE.split(seg) if t)
    tokens = tokens[:_MAX_WORDS]
    words = _strip_dangling_trailer([_titleize_token(t) for t in tokens])
    return " ".join(words)


def _name_label(flow: "Flow") -> str:
    """Humanize summary.title / description / kebab name."""
    summary = getattr(flow, "summary", None)
    title = getattr(summary, "title", None) if summary is not None else None
    candidate = title or getattr(flow, "description", None) or flow.name or ""
    candidate = candidate.strip()
    if not candidate:
        return ""
    # Drop a trailing "-flow" / " flow" so the label isn't "... Flow".
    candidate = re.sub(r"[-_\s]flow$", "", candidate, flags=re.IGNORECASE)
    tokens = [t for t in _TOKEN_SPLIT_RE.split(candidate) if t][:_MAX_WORDS]
    words = _strip_dangling_trailer([_titleize_token(t) for t in tokens])
    return " ".join(words)


def _meaningful_word_count(label: str) -> int:
    """Count label words that aren't generic structural nouns.

    Used to choose between the file-basename label and the kebab-name
    label in the fallback tier: a descriptive name ("Setup Alby
    Integration") should win over a thin file label ("Pages Alby"),
    while a generic name should lose to a specific file basename
    ("Apple Calendar Webhook"). Structural-route directory nouns carry
    no product meaning so they don't count.
    """
    generic = {
        "api", "app", "pages", "page", "src", "route", "routes",
        "handler", "index", "view", "get", "post", "the", "a", "an",
    }
    return sum(1 for w in label.split() if w.lower() not in generic)


def _fallback_label(flow: "Flow", entry_file: str | None = None) -> str:
    """Last-resort label: the richer of {file-basename, kebab name}.

    Per Fix 1 (2026-05-26): a ``<file>``-entry flow has no usable symbol.
    The humanized entry-file basename ("Apple Calendar Webhook") often
    reads better than a generic kebab name — but a descriptive kebab
    name ("Setup Alby Integration") must NOT be regressed to a thin file
    label ("Pages Alby"). We compute both and keep whichever has more
    *meaningful* (non-structural) words; the kebab name wins ties so the
    stable-id-derived label remains the default when neither is richer.
    """
    name_label = _name_label(flow)
    file_label = _humanize_file_basename(entry_file)
    if file_label and (
        _meaningful_word_count(file_label) > _meaningful_word_count(name_label)
    ):
        return file_label
    return name_label or file_label


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
    entry_dict = getattr(flow, "entry", None)
    if entry_file is None and entry_dict:
        entry_file = entry_dict.get("file") or None
        entry_symbol = entry_symbol or entry_dict.get("symbol")
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

    # 2. entry_point symbol. Skip framework-boilerplate + bare-verb
    #    handlers (``_NOISE_SYMBOLS``) and ``<...>`` sentinels (incl.
    #    ``<file>``) so they fall THROUGH to the file-basename fallback
    #    rather than emitting "<file>" / "Get Handler" verbatim.
    if (
        entry_symbol
        and not entry_symbol.startswith("<")
        and entry_symbol.lower() not in _NOISE_SYMBOLS
    ):
        label = _humanize_symbol(entry_symbol)
        if label:
            return label

    # 3. Fallback — humanize the entry-file basename, then summary /
    #    description / kebab name.
    return _fallback_label(flow, entry_file)


__all__ = ["derive_display_name"]
