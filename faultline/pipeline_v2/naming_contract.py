"""Stage 6.87 — display-name contract (Product-Spine §4.8, Wave 3).

SELECTION-not-generation: every product-feature / user-flow display name
is one of

  (a) a PIN from the previous scan (keeper channel — content-derived
      join by ``anchor_id`` / canonical slug; production rescans only,
      ``FAULTLINE_KEEPER=0`` restores cold-scan purity for eval),
  (b) a ranked DETERMINISTIC candidate (authored nav labels, hub
      composition "<Family> — <Vendor>", humanized anchor segments,
      journey templates from flow-verb evidence, the current display),
  (c) a PM-Labeler pick from that candidate set (keyed scans only,
      Wave-3 persona) — validated against the documented grammar +
      token evidence and falling back to (b) on reject.

Identity ≠ display (hard law): this stage writes ONLY the display
channel — ``Feature.display_name`` on product features and
``UserFlow.name`` (the UF display channel) — plus telemetry. Canonical
identity — ``Feature.name`` slugs, ``anchor_id``, ``product_feature_id``,
UF ``id``/``uuid``/``resource``/``intent``, flow linkage — is NEVER
touched (fixture-equality + static grep-guard tests). Names are never
join keys downstream of this stage: it runs at the very end of finalize,
after 6.7d/hub-parity/emission-taxonomy, immediately before the
platform-infrastructure lane assembly and the Stage-7 output write.

Display-name laws (each deterministic, each with a fixture test):

  * ``single_letter`` — no 1-character display / 1-letter content word;
  * ``param`` — no route-param glyphs (``$ : { } [ ] < > *``);
  * ``file_stem`` — no ``schema.json``-class names (extension tokens);
  * ``pf_uf_twin`` — a UF display never exactly equals its own PF's
    display (journey phrasing: verb-led, from the journey grammar);
  * ``acronym_case`` — known acronyms render UPPERCASE (EDR, CLI, MCP)
    and known brands in canonical casing (GoCardless, tRPC) — vendor /
    dictionary aware, applied as a universal casing polish;
  * ``display_collision`` — two product PFs never share a case-folded
    display (the qualified form "<Base> (<Qualifier>)" disambiguates).

Kill-switch: ``FAULTLINE_NAMING_CONTRACT=0`` (default ON) restores the
pre-W3 displays byte-identically. Vocabulary is data
(``data/naming-contract-vocab.yaml``; authoring copy ``eval/…``,
drift-guarded). Keyless scans take the deterministic top-choice path —
no LLM, byte-stable across runs (snapshot-gate property).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from faultline.pipeline_v2.data import load_yaml

logger = logging.getLogger(__name__)

__all__ = [
    "NAMING_CONTRACT_ENV",
    "HUMANIZE_ROUTE_NAMES_ENV",
    "PF_NAME_LAW_ENV",
    "naming_contract_enabled",
    "humanize_route_names_enabled",
    "pf_name_law_enabled",
    "load_naming_vocab",
    "polish_display_casing",
    "display_law_violations",
    "build_pf_candidates",
    "build_uf_candidates",
    "hub_composition_display",
    "humanize_anchor_display",
    "nav_labels_for_pfs",
    "run_naming_contract",
]

NAMING_CONTRACT_ENV = "FAULTLINE_NAMING_CONTRACT"

_VOCAB_FILE = "naming-contract-vocab.yaml"
_vocab_cache: dict[str, Any] | None = None

#: Route-param / template glyphs forbidden in any display name.
_PARAM_GLYPHS = re.compile(r"[$:{}\[\]<>*]")

#: Word split for casing polish — keeps separators so the display's
#: punctuation (spaces, " — ", parens) survives token replacement.
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*")

#: Dynamic route-segment forms (mirrors spine_anchors._DYNAMIC_RE — all
#: dialects: ``[x]`` ``$x`` ``:x`` ``{x}`` ``<x>`` ``*x``).
_DYNAMIC_SEG = re.compile(r"^(\[+|[$:{<*])")

#: Route-group segment ``(marketing)`` — author-authored grouping word.
_GROUP_SEG = re.compile(r"^\((.+)\)$")

#: API-class transparent segments for humanization (same spirit as the
#: spine vocab's api transparency; bounded, universal).
_API_SEGS = frozenset({"api", "trpc", "rest", "graphql", "rpc"})

#: Version-dir segments (v1/v2 class) — never display words.
_VERSION_SEG = re.compile(r"^v\d+$", re.IGNORECASE)

#: Kill-switch for the route-template humanizer (B2). Default ON; ``=0``
#: restores the pre-B2 anchor humanization byte-identically (a route
#: display keeps whatever the legacy ``_meaningful_segments`` produced).
HUMANIZE_ROUTE_NAMES_ENV = "FAULTLINE_HUMANIZE_ROUTE_NAMES"

#: B9 UF name laws kill-switch (default ON). ``=0`` restores base UF display
#: names + name_confidence byte-identically.
UF_NAME_LAWS_ENV = "FAULTLINE_UF_NAME_LAWS"

#: B16 PF dev-grain suffix law kill-switch (default ON). ``=0`` restores the
#: pre-B16 PF display names byte-identically (a route-dir leak like
#: 'policy-page' keeps its "Policy Page" display).
PF_NAME_LAW_ENV = "FAULTLINE_PF_NAME_LAW"

#: Dev-grain surface nouns that must never TRAIL a product-feature display
#: when the route anchor's terminal dir segment leaked them (operator
#: doctrine: 'there is no such thing as a page in product features'). Anchor-
#: form-gated in :func:`_strip_pf_devgrain_suffix` — a display word here is
#: stripped ONLY when the anchor dir ends '-<word>' ('policy-page' ->
#: 'Policy'; 'investigation-flow' -> 'Investigation'). A capability that
#: merely CONTAINS one ('Landing Page Builder', anchor '*-builder') is never
#: touched. Fixed, scale-invariant vocabulary — corroboration, not tuning.
_PF_DEVGRAIN_SUFFIX_TOKENS = frozenset({"page", "screen", "view", "flow"})

#: Deterministic display word per action family (the labeler's lossy collapse
#: — two distinct action-family children both "Configure X" — is undone here).
_ACTION_FAMILY_WORD = {
    "browse": "Browse", "view": "View", "create": "Create",
    "update": "Update", "delete": "Delete", "act": "Manage",
}
#: Write families a name may not claim without a member performing that action.
_WRITE_FAMILIES = frozenset({"create", "update", "delete"})
#: Read families (collection vs member reads).
_READ_FAMILIES = frozenset({"browse", "view"})
#: Packaged action-family vocab file (same authoring copy the lattice uses;
#: loaded directly to avoid a circular import with journey_lattice).
_ACTION_FAMILIES_VOCAB_FILE = "journey-action-families.yaml"

#: A word-adjacent ``+`` (Remix flat-route folder nesting marker) or a
#: leading ``_`` word (pathless/layout prefix) that leaked into a display.
_TRAILING_PLUS_RE = re.compile(r"\w\+")


def naming_contract_enabled() -> bool:
    """Default ON; ``FAULTLINE_NAMING_CONTRACT=0`` restores pre-W3 output."""
    return os.environ.get(NAMING_CONTRACT_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def uf_name_laws_enabled() -> bool:
    """UF name laws (B9): name-claim narrowing + UF-vs-UF display uniqueness +
    evidence-derived name_confidence, applied over FINAL members at emission.
    Default ON; ``FAULTLINE_UF_NAME_LAWS=0`` restores the base display names +
    name_confidence byte-identically."""
    return os.environ.get(UF_NAME_LAWS_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def humanize_route_names_enabled() -> bool:
    """Default ON; ``FAULTLINE_HUMANIZE_ROUTE_NAMES=0`` restores the pre-B2
    anchor humanization (byte-identical route display names)."""
    return os.environ.get(HUMANIZE_ROUTE_NAMES_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def pf_name_law_enabled() -> bool:
    """PF dev-grain suffix law (B16): a route-dir-naming leak
    ('policy-page' -> 'Policy Page') is stripped to the capability
    ('Policy') at the display channel. Default ON;
    ``FAULTLINE_PF_NAME_LAW=0`` restores the pre-B16 PF displays
    byte-identically."""
    return os.environ.get(PF_NAME_LAW_ENV, "1").strip().lower() not in {
        "0", "false",
    }


# ── Route-template humanization (B2 — router-family aware) ──────────────
#
# File-system routers encode structure IN the path/filename with dialect
# glyphs that are dev machinery, never product words: trailing ``+``
# (folder nesting), ``$param`` / ``:param`` / ``{param}`` (dynamic),
# ``_layout`` (pathless), ``[escaped]`` (literal escape), ``.`` (path
# separator inside one folder name), ``[param]`` (dynamic — DROP),
# ``(group)`` (route group — unwrap). ``[..]`` means the OPPOSITE thing
# across dialects (escape vs dynamic), so a dialect is read from the
# path's own STRUCTURAL markers (never a stack NAME — trunk-purity G3)
# into capability flags that drive the per-segment rule. Every rule is
# generic to the marker — never a repo-specific literal.


@dataclass(frozen=True)
class _RouteDialect:
    """Structural capabilities of a filesystem router, inferred from a
    path's own glyphs (not a stack name):

    * ``flat_nesting`` — a trailing ``+`` on a folder groups nested
      routes (flat-routes convention) and is not a display word.
    * ``bracket_escape`` — ``[x]`` is a LITERAL escape (keep the inner
      text) rather than a dynamic-param placeholder (drop it). The two
      readings are mutually exclusive per router; a router that addresses
      params with ``$`` never uses brackets for params.
    """

    flat_nesting: bool
    bracket_escape: bool


def _route_dialect(path: str) -> _RouteDialect:
    """Read a path's router capabilities from its structural markers."""
    segs = [s for s in (path or "").split("/") if s]
    low = {s.lower() for s in segs}
    dollar_params = any("$" in s for s in segs)
    plus_folders = any(len(s) > 1 and s.endswith("+") for s in segs)
    routes_root = "routes" in low and ("app" in low or "src" in low)
    dotted_layout = any(s.startswith("_") and "." in s for s in segs)
    flat = plus_folders or dollar_params or routes_root or dotted_layout
    return _RouteDialect(flat_nesting=flat, bracket_escape=dollar_params or flat)


def _param_noun(seg: str, vocab: Mapping[str, Any]) -> str | None:
    """The human NOUN of a dynamic route param, or ``None`` for pure
    addressing. ``$teamUrl`` → ``"team"`` (the ``Url`` addressing suffix
    drops), ``$documentId`` → ``"document"``, ``$id``/``$slug``/``$token``
    → ``None`` (an opaque identifier names nothing)."""
    raw = (seg or "").strip()
    raw = raw.lstrip("$:{<*[").rstrip("}>]").strip("[]")
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)
    words = [w.lower() for w in re.split(r"[-_\s]+", raw) if w]
    addr = {str(a).lower() for a in (vocab.get("route_addressing_suffixes") or [])}
    core = [w for w in words if w not in addr]
    return " ".join(core) if core else None


def _normalize_route_segment(
    seg: str, dialect: _RouteDialect, vocab: Mapping[str, Any],
) -> str | None:
    """One path segment → a human display token (space-joined for
    dot-notation), or ``None`` to drop (layout prefix, dynamic param,
    empty). Router-template glyphs are removed per the path's dialect."""
    s = (seg or "").strip()
    if not s:
        return None
    if dialect.flat_nesting:
        stripped = s.rstrip("+")           # flat-route folder nesting marker
        if stripped:
            s = stripped
    if s.startswith("_"):
        return None                        # pathless / layout — organises files
    g = _GROUP_SEG.match(s)
    if g:                                  # route group "(marketing)" → word
        s = g.group(1).strip()
        if not s:
            return None
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if dialect.bracket_escape:         # escaped LITERAL → unwrap
            inner = inner.strip("._")
            if not inner:
                return None
            s = inner
        else:
            return None                    # bracket-dynamic param → drop
    if "." in s:                           # Remix dot = URL separator
        parts: list[str] = []
        for p in s.split("."):
            p = p.strip()
            if not p:
                continue
            if _DYNAMIC_SEG.match(p) or p[0] in "$:{":
                noun = _param_noun(p, vocab)
                if noun:
                    parts.append(noun)
                continue
            if p.startswith("_"):
                continue
            if len(p) == 1 and p.isalpha():
                continue                   # single-char URL scaffold ("/p/")
            parts.append(p)
        return " ".join(parts) if parts else None
    if _DYNAMIC_SEG.match(s):
        return _param_noun(s, vocab)
    return s


def _peel_edge_single_letters(text: str | None) -> str | None:
    """Repair a display carrying edge single-letter words (the ``/p/$url``
    → "P URL" class): drop leading/trailing 1-char alpha words so the
    single_letter law passes ("P URL" → "URL"). Interior single letters
    and all-single-letter strings are left intact."""
    if not text:
        return text
    words = text.split(" ")
    if len(words) <= 1 or not any(
        len(w) == 1 and w.isalpha() for w in words
    ):
        return text
    while len(words) > 1 and len(words[0]) == 1 and words[0].isalpha():
        words = words[1:]
    while len(words) > 1 and len(words[-1]) == 1 and words[-1].isalpha():
        words = words[:-1]
    return " ".join(words)


def _has_route_template_residue(text: str) -> bool:
    """True when a display still carries router-template machinery: a
    param glyph (``$ : { } [ ] < > *``), a word-adjacent ``+`` (Remix
    nesting), or a leading-underscore word (layout prefix)."""
    t = text or ""
    if _PARAM_GLYPHS.search(t) or _TRAILING_PLUS_RE.search(t):
        return True
    return any(w.startswith("_") for w in t.split(" ") if w)


def _strip_display_residue(text: str, vocab: Mapping[str, Any]) -> str | None:
    """Last-resort string scrub of route-template residue from a display
    (used only when no clean anchor word is available). Removes param
    glyphs, trailing ``+``, leading-underscore words, then peels edge
    single letters + re-polishes. May return ``None`` when nothing human
    remains."""
    t = _PARAM_GLYPHS.sub("", text or "")
    t = re.sub(r"\+", "", t)
    words = [w for w in t.split(" ") if w and not w.startswith("_")]
    out = _peel_edge_single_letters(" ".join(words))
    out = polish_display_casing((out or "").strip(), vocab)
    return out or None


def load_naming_vocab() -> dict[str, Any]:
    """Packaged vocabulary (cached — pure data, read once per process)."""
    global _vocab_cache
    if _vocab_cache is None:
        _vocab_cache = load_yaml(_VOCAB_FILE)
    return _vocab_cache


# ── Casing polish (acronym / brand aware — law ``acronym_case``) ────────


def polish_display_casing(text: str, vocab: Mapping[str, Any] | None = None) -> str:
    """Render known acronyms UPPERCASE and known brands in canonical
    casing, word by word; every other word (and all separators) is kept
    byte-identical. Vendor identity wins over acronym reading (a token
    present in ``brand_casing`` never uppercases via ``known_acronyms``).
    Idempotent — polishing a polished display is a no-op."""
    v = vocab or load_naming_vocab()
    acronyms = {str(a).lower() for a in (v.get("known_acronyms") or [])}
    brands = {str(k).lower(): str(val)
              for k, val in (v.get("brand_casing") or {}).items()}

    def _one(m: re.Match[str]) -> str:
        word = m.group(0)
        low = word.lower()
        if low in brands:
            return brands[low]
        if low in acronyms:
            return word.upper()
        return word

    return _WORD_RE.sub(_one, text or "")


# ── Display-name laws ───────────────────────────────────────────────────


def display_law_violations(
    text: str,
    vocab: Mapping[str, Any] | None = None,
    *,
    pf_display: str | None = None,
) -> list[str]:
    """Deterministic law check for one display name. Returns the list of
    violated law ids (empty == clean). ``pf_display`` (UF checks only)
    arms the ``pf_uf_twin`` law against the journey's own capability."""
    v = vocab or load_naming_vocab()
    out: list[str] = []
    t = (text or "").strip()
    if not t or len(t.replace(" ", "")) <= 1:
        out.append("single_letter")
    else:
        words = _WORD_RE.findall(t)
        if any(len(w) == 1 and w.isalpha() for w in words):
            out.append("single_letter")
    # W3.1 rider (fb3 '2025' class): a display with no letter at all —
    # bare years, error codes, counters — names nothing; candidates/pins
    # carrying it are law-dirty and fall through to the anchor-derived
    # fallback.
    if t and not any(c.isalpha() for c in t):
        out.append("digit_only")
    if _PARAM_GLYPHS.search(t):
        out.append("param")
    exts = {str(e).lower() for e in (v.get("file_extensions") or [])}
    for w in _WORD_RE.findall(t):
        m = re.search(r"\.([A-Za-z0-9]{1,5})$", w)
        if m and m.group(1).lower() in exts:
            out.append("file_stem")
            break
    if pf_display is not None and t and (
        t.strip().lower() == (pf_display or "").strip().lower()
    ):
        out.append("pf_uf_twin")
    return out


# ── Anchor-id humanization (candidate source) ───────────────────────────


def _display_word(seg: str, vocab: Mapping[str, Any]) -> str:
    """Titleized display form of one path segment (camel/kebab/snake
    split; acronym/brand cased)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", seg)
    words = [w for w in re.split(r"[-_\s]+", spaced) if w]
    raw = " ".join(
        w if (w.isupper() and len(w) > 1) else w.capitalize() for w in words
    )
    return polish_display_casing(raw, vocab)


def _meaningful_segments(path: str, vocab: Mapping[str, Any]) -> list[str]:
    """Author-meaningful display words of an anchor-id path: route groups
    unwrap to their word; params, api/version segments, and structural
    code-location segments drop.

    With ``FAULTLINE_HUMANIZE_ROUTE_NAMES`` on (B2, default) the walk is
    router-template aware — Remix ``+``/``$``/``_layout``/``[escape]``/dot
    notation is normalized (see ``_route_meaningful_segments``). ``=0``
    restores this legacy body byte-identically."""
    if humanize_route_names_enabled():
        return _route_meaningful_segments(path, vocab)
    structural = {str(s).lower() for s in (vocab.get("structural_segments") or [])}
    hub_containers = {str(s).lower()
                      for s in (vocab.get("hub_container_segments") or [])}
    exts = {str(e).lower() for e in (vocab.get("file_extensions") or [])}
    out: list[str] = []
    for seg in (path or "").split("/"):
        s = seg.strip()
        if not s:
            continue
        g = _GROUP_SEG.match(s)
        if g:
            s = g.group(1).strip()
            if not s:
                continue
        if _DYNAMIC_SEG.match(s) or _VERSION_SEG.match(s):
            continue
        # File-anchor leaf segments carry an extension ("schema.json") —
        # the display word is the stem (the file_stem law forbids the
        # extension in any display).
        m = re.match(r"^(.+)\.([A-Za-z0-9]{1,5})$", s)
        if m and m.group(2).lower() in exts:
            s = m.group(1)
        low = s.lower()
        if low in structural or low in hub_containers or low in _API_SEGS:
            continue
        out.append(s)
    return out


def _route_meaningful_segments(
    path: str, vocab: Mapping[str, Any],
) -> list[str]:
    """Router-template-aware meaningful segments (B2). Each ``/``-segment
    is normalized per the detected router family; structural / hub /
    extension leaves drop; api / version / tenant-scope tokens are
    TRANSPARENT only when a deeper meaningful token exists (mirrors
    ``spine_anchors._pattern_key_chain`` — ``/workspaces/{id}/tables`` →
    "Tables", but a terminal ``/api`` or ``/t/$teamUrl`` still keys its
    own surface). Never returns empty when any token survives."""
    structural = {str(s).lower() for s in (vocab.get("structural_segments") or [])}
    hub_containers = {str(s).lower()
                      for s in (vocab.get("hub_container_segments") or [])}
    exts = {str(e).lower() for e in (vocab.get("file_extensions") or [])}
    tenant_scope = {
        str(s).lower() for s in (vocab.get("tenant_scope_segments") or [])
    }
    dialect = _route_dialect(path)

    tokens: list[str] = []
    for seg in (path or "").split("/"):
        tok = _normalize_route_segment(seg, dialect, vocab)
        if tok is None:
            continue
        # File-anchor leaf extension ("schema.json") → stem (file_stem law).
        m = re.match(r"^(.+)\.([A-Za-z0-9]{1,5})$", tok)
        if m and m.group(2).lower() in exts:
            tok = m.group(1)
        low = tok.lower()
        if low in structural or low in hub_containers:
            continue
        tokens.append(tok)

    def _transparent(i: int) -> bool:
        low = tokens[i].lower()
        if not (low in _API_SEGS or _VERSION_SEG.match(low)
                or low in tenant_scope):
            return False
        # transparent only if a deeper NON-transparent token follows.
        for j in range(i + 1, len(tokens)):
            lj = tokens[j].lower()
            if not (lj in _API_SEGS or _VERSION_SEG.match(lj)
                    or lj in tenant_scope):
                return True
        return False

    out = [t for i, t in enumerate(tokens) if not _transparent(i)]
    if not out and tokens:
        out = [tokens[-1]]
    return out


def _anchor_path(anchor_id: str) -> tuple[str, str]:
    """``(source, path)`` of a canonical anchor id (``route:apps/x`` →
    ``("route", "apps/x")``). Unknown shapes → ``("", anchor_id)``."""
    aid = anchor_id or ""
    if ":" in aid:
        src, _, rest = aid.partition(":")
        return src, rest
    return "", aid


def humanize_anchor_display(
    anchor_id: str, vocab: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """``(base, qualifier)`` display words derived from an anchor id.

    ``base`` is the last author-meaningful segment ("Discord" for
    ``route:apps/web/src/app/(landing)/(redirect)/discord``); ``qualifier``
    is the nearest meaningful ancestor ("Redirect") — used only when the
    bare base would collide with another display. ``(None, None)`` when
    the id yields no meaningful words (fallback = current display)."""
    src, path = _anchor_path(anchor_id)
    if not src:
        return None, None
    segs = _meaningful_segments(path, vocab)
    if not segs:
        return None, None
    base = _display_word(segs[-1], vocab)
    qual = _display_word(segs[-2], vocab) if len(segs) >= 2 else None
    if humanize_route_names_enabled():
        base = _peel_edge_single_letters(base) or ""
        qual = _peel_edge_single_letters(qual)
    return (base or None), (qual or None)


def hub_composition_display(
    anchor_id: str, current_display: str, vocab: Mapping[str, Any],
) -> str | None:
    """"<Family> — <Vendor>" for a ``hub:<dir>/<vendor>`` anchor
    (Product-Spine §4.8 hub composition). ``None`` for non-hub anchors
    and hub CORE anchors (whose family display already carries "Core")."""
    src, path = _anchor_path(anchor_id)
    if src != "hub":
        return None
    segs = [s for s in (path or "").split("/") if s]
    if len(segs) < 2:
        return None
    family_segs = _meaningful_segments("/".join(segs[:-1]), vocab)
    if not family_segs:
        return None
    family = _display_word(family_segs[-1], vocab)
    vendor = polish_display_casing(current_display, vocab)
    if not family or not vendor:
        return None
    if vendor.strip().lower() == family.strip().lower():
        return None  # degenerate (vendor dir == family dir)
    return f"{family} — {vendor}"


# ── PF dev-grain suffix law (B16 — display channel only) ────────────────


def _anchor_terminal_segment(anchor_id: str) -> str | None:
    """Last author-meaningful dir segment of a ``route:`` anchor
    ('policy-page' for 'route:policy-page'); ``None`` for non-route anchors
    or when only params remain. Route groups unwrap ('(dashboard)' ->
    'dashboard'); param/dynamic segments ('[id]', '$teamUrl') are skipped."""
    src, path = _anchor_path(anchor_id)
    if src != "route":
        return None
    for seg in reversed((path or "").split("/")):
        s = seg.strip()
        if not s:
            continue
        if _DYNAMIC_SEG.match(s):
            continue
        g = _GROUP_SEG.match(s)
        if g:
            s = g.group(1).strip()
            if not s:
                continue
        return s
    return None


def _strip_pf_devgrain_suffix(
    display: str, anchor_id: str, vocab: Mapping[str, Any],
) -> str | None:
    """The dev-grain-suffix-stripped display, or ``None`` if nothing to
    strip.

    Anchor-form-driven (never token-blind): the display's TRAILING word W
    (one of :data:`_PF_DEVGRAIN_SUFFIX_TOKENS`) is stripped ONLY when the
    route anchor's terminal dir segment ENDS with ``-W`` — i.e. the token is
    a repo-dir-naming leak ('policy-page' -> 'Policy Page' -> 'Policy';
    'investigation-flow' -> 'Investigation Flow' -> 'Investigation'). 'flow'
    is therefore stripped ONLY behind a ``route:*-flow`` anchor (the
    capability "Investigation Flow" vs a flow-suffix leak is decided by the
    anchor form, per operator brief). Guards: 'Landing Page Builder' (anchor
    '*-builder', trailing word 'Builder') is untouched; a bare "Page" (single
    word, or a strip that would leave nothing / a single letter) is kept."""
    terminal = _anchor_terminal_segment(anchor_id)
    if not terminal:
        return None
    tl = terminal.lower()
    tok = next(
        (w for w in sorted(_PF_DEVGRAIN_SUFFIX_TOKENS) if tl.endswith("-" + w)),
        None,
    )
    if tok is None:
        return None
    words = (display or "").split()
    if len(words) < 2 or words[-1].lower() != tok:
        return None
    stripped = " ".join(words[:-1]).strip()
    if not stripped or len(stripped.replace(" ", "")) <= 1:
        return None
    if display_law_violations(stripped, vocab):
        return None
    polished = polish_display_casing(stripped, vocab)
    return polished if polished.strip().lower() != (display or "").strip().lower() else None


def _apply_pf_devgrain_law(
    chosen: str,
    anchor_id: str,
    slug: str,
    vocab: Mapping[str, Any],
    taken: Mapping[str, str],
    tele: dict[str, Any],
) -> tuple[str, bool]:
    """Return ``(display, stripped?)`` after the PF dev-grain suffix law.

    A clean strip fires when the stripped capability name is still UNIQUE.
    On a post-strip COLLISION — the stripped name is already claimed by a
    sibling PF (a fragmented capability: 'Detections Page' -> 'Detections'
    == existing route:detection) — the current display is kept UNCHANGED
    (never a duplicate, never a qualifier) and recorded as the Part-2
    unification signal. Doctrine: LAW > PIN; display channel only; the
    existing PF-vs-PF uniqueness law (``taken``) decides the collision."""
    stripped = _strip_pf_devgrain_suffix(chosen, anchor_id, vocab)
    if not stripped:
        return chosen, False
    folded = stripped.strip().lower()
    if folded in taken:
        tele["pf_devgrain_collision"] = tele.get("pf_devgrain_collision", 0) + 1
        tele.setdefault("pf_devgrain_collision_samples", []).append({
            "slug": slug,
            "anchor_id": anchor_id,
            "kept": chosen,
            "would_be": stripped,
            "collides_with": taken.get(folded),
        })
        return chosen, False
    tele["pf_devgrain_stripped"] = tele.get("pf_devgrain_stripped", 0) + 1
    return stripped, True


# ── Nav-label channel (authored labels; product_strings nav pairs) ──────


def nav_labels_for_pfs(
    product_features: Iterable[Any],
    product_strings: Any,
    routes_index: Iterable[Mapping[str, Any]] | None,
) -> dict[str, str]:
    """``{pf_slug: authored nav label}`` — a nav pair (label, href) votes
    for the PF that owns the route FILE its normalized href resolves to.
    One deterministic label per PF: most votes → shortest → alpha.
    Empty on scans without a product-string index (keyless suppressed
    paths) — the channel is optional by construction."""
    pairs_by_file: Mapping[str, list[tuple[str, str | None]]] = (
        getattr(product_strings, "nav_pairs_by_file", None) or {}
    )
    if not pairs_by_file or not routes_index:
        return {}
    try:
        from faultline.pipeline_v2.product_strings import normalize_href
    except ImportError:  # pragma: no cover — same package
        return {}

    file_by_pattern: dict[str, str] = {}
    for r in routes_index:
        pat = str(r.get("pattern") or "").strip()
        f = str(r.get("file") or "").strip()
        if pat and f and pat not in file_by_pattern:
            file_by_pattern[pat] = f

    pf_by_path: dict[str, str] = {}
    for pf in product_features:
        slug = str(getattr(pf, "name", "") or "")
        for p in (getattr(pf, "paths", None) or []):
            pf_by_path.setdefault(str(p), slug)

    # Shell words never name anything (§4.2 — HomePage doctrine): a nav
    # label that is itself a shell slug ("Home", "Dashboard") is not an
    # authored capability label. Best-effort — classifier faults skip
    # the guard, never the channel.
    _shell_check = None
    try:
        from faultline.pipeline_v2.surface_taxonomy import (
            SurfaceScopeClassifier,
        )
        _shell_check = SurfaceScopeClassifier().is_shell_name
    except Exception:  # noqa: BLE001 — optional guard
        _shell_check = None

    votes: dict[str, dict[str, int]] = {}
    for pairs in pairs_by_file.values():
        for label, href in pairs:
            if not label or not href:
                continue
            lab = " ".join(str(label).split())
            if not (1 <= len(lab.split()) <= 4) or len(lab) > 40:
                continue
            if _shell_check is not None and _shell_check(lab):
                continue
            norm = normalize_href(str(href)) or ""
            if not norm:
                continue
            route_file = file_by_pattern.get(norm)
            if not route_file:
                continue
            owner_slug = pf_by_path.get(route_file)
            if not owner_slug:
                continue
            votes.setdefault(owner_slug, {})
            votes[owner_slug][lab] = votes[owner_slug].get(lab, 0) + 1

    out: dict[str, str] = {}
    for slug, labs in votes.items():
        best = sorted(
            labs.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]),
        )[0][0]
        out[slug] = best
    return out


# ── Candidate builders ──────────────────────────────────────────────────


def build_pf_candidates(
    pf: Any,
    vocab: Mapping[str, Any],
    *,
    nav_label: str | None = None,
) -> list[str]:
    """Ranked display candidates for one product feature (dedup, order-
    preserving). The CURRENT display (casing-polished) is always present
    — the contract can never invent from nothing (never-worse)."""
    slug = str(getattr(pf, "name", "") or "")
    current = str(
        getattr(pf, "display_name", None)
        or (_display_word(slug, vocab) if slug else "")
    )
    anchor_id = str(getattr(pf, "anchor_id", None) or "")
    out: list[str] = []

    def _add(c: str | None) -> None:
        if c and c.strip() and c not in out:
            out.append(c.strip())

    if nav_label:
        _add(polish_display_casing(nav_label, vocab))
    _add(hub_composition_display(anchor_id, current, vocab))
    src, _p = _anchor_path(anchor_id)
    polished_current = polish_display_casing(current, vocab)
    # The collision-qualified verbose class ("Discord (Route Apps Web
    # Src App Landing Redirect Discord)") or a law-dirty current display
    # yields to the humanized anchor words; a law-clean, non-verbose
    # display outranks them (no churn of good names). Never for hub
    # anchors: vendor children carry the composition above, and a hub
    # CORE's designed "<Family> Core" must not degrade to the bare
    # family word.
    qual_match = re.search(r"\(([^)]{2,})\)\s*$", polished_current)
    verbose_qualified = bool(
        qual_match and len(qual_match.group(1).split()) >= 3
    )
    current_dirty = bool(display_law_violations(polished_current, vocab))
    # B2: a route display carrying template residue ('Admin+', 'API+',
    # 'Internal+', 'P.$URL') is dirty even when the param LAW misses the
    # '+'/'_'-prefix class — yield to the humanized anchor word.
    if (humanize_route_names_enabled() and src == "route"
            and _has_route_template_residue(polished_current)):
        current_dirty = True
    base, qual = (None, None) if src == "hub" else humanize_anchor_display(
        anchor_id, vocab)
    if current_dirty or verbose_qualified:
        if base:
            _add(base)
            if qual:
                _add(f"{base} ({qual})")
        _add(polished_current)
    else:
        _add(polished_current)
        if base:
            _add(base)
            if qual:
                _add(f"{base} ({qual})")
    return out


def _flow_verb_verdict(
    member_flow_names: Iterable[str], vocab: Mapping[str, Any],
) -> str:
    """Journey intent verdict from member-flow verb evidence — first
    matching class in the vocab's documented order wins; ``manage`` is
    the fallback."""
    classes: Mapping[str, Any] = vocab.get("flow_verb_classes") or {}
    tokens: list[str] = []
    for nm in member_flow_names:
        tokens.extend(re.split(r"[^a-z0-9]+", str(nm or "").lower()))
    tokset = {t for t in tokens if t}
    for verdict in ("connect", "ingest", "send", "receive", "run", "view"):
        verbs = {str(x).lower() for x in (classes.get(verdict) or [])}
        if tokset & verbs:
            return verdict
    return "manage"


def _resource_phrase(pf_display: str, vocab: Mapping[str, Any]) -> str:
    """Lower-cased resource phrase of a PF display for journey templates
    ("Status Reports" → "status reports"); acronyms/brands keep their
    canonical casing ("EDR Core" → "EDR core")."""
    v = vocab
    acronyms = {str(a).lower() for a in (v.get("known_acronyms") or [])}
    brands = {str(k).lower(): str(val)
              for k, val in (v.get("brand_casing") or {}).items()}
    words = []
    for w in (pf_display or "").split(" "):
        low = w.lower()
        if low in brands:
            words.append(brands[low])
        elif low in acronyms:
            words.append(w.upper())
        else:
            words.append(w.lower())
    return " ".join(x for x in words if x)


def build_uf_candidates(
    uf: Any,
    pf: Any | None,
    vocab: Mapping[str, Any],
    member_flow_names: Iterable[str] = (),
    authored: Iterable[str] = (),
) -> list[str]:
    """Ranked display candidates for one user flow. Journey templates
    (verb-led, actor+intent+outcome shape) lead when the current name is
    a twin of its PF or the UF is backstop-synthesized; a law-clean
    existing name leads otherwise (no churn of good journey names).

    ``authored`` (Track C) — MAINTAINER-authored journey labels from e2e
    specs. When supplied they lead ALL other candidates: the maintainer
    literally named this journey, which outranks any derived template. Each
    is still polished + law-gated by the caller like any candidate, so an
    unlawful authored string simply falls through to the derived set.
    Empty (the default) ⇒ byte-identical to the pre-Track-C ranking."""
    current = str(getattr(uf, "name", "") or "")
    pf_display = (
        str(getattr(pf, "display_name", None) or getattr(pf, "name", "") or "")
        if pf is not None else ""
    )
    anchor_id = str(getattr(pf, "anchor_id", None) or "") if pf is not None else ""
    synthesized = bool(getattr(uf, "synthesized", False))
    twin = bool(current) and pf_display and (
        current.strip().lower() == pf_display.strip().lower()
    )

    templates: Mapping[str, Any] = vocab.get("journey_templates") or {}
    verdict = _flow_verb_verdict(member_flow_names, vocab)
    src, _path = _anchor_path(anchor_id)
    is_vendor_pf = src == "hub" and " Core" not in pf_display
    # W3.2 D9 — a synthesized SYSTEM journey (flow-less job/route seed)
    # is named by its OWN job resource, never the PF display: the seed
    # exists to surface the JOB's identity; the PF is only its terminal
    # home. The PF-display template collapsed every job under one hub
    # into identical rows (Soc0: 11 inngest seeds → "Manage
    # integrations" ×11 after terminal-home binding — a verifier cannot
    # review that). Background executions template on the "run" verb.
    is_system_seed = (
        synthesized
        and str(getattr(uf, "category", "") or "") == "system"
        and bool(getattr(uf, "resource", None))
    )
    if is_system_seed:
        tmpl = (templates.get("generic") or {}).get("run") or "Run {r}"
        own = re.sub(r"[-_]+", " ", str(uf.resource)).strip()
        template_name = tmpl.replace("{r}", _resource_phrase(own, vocab))
    elif is_vendor_pf:
        tmpl = (templates.get("vendor") or {}).get(verdict) or "Manage {v}"
        vendor_disp = polish_display_casing(pf_display, vocab)
        # A composed "<Family> — <Vendor>" display templates on the
        # VENDOR half (the journey subject), never the family prefix.
        if " — " in vendor_disp:
            vendor_disp = vendor_disp.split(" — ", 1)[1]
        template_name = tmpl.replace("{v}", vendor_disp)
    else:
        tmpl = (templates.get("generic") or {}).get(verdict) or "Manage {r}"
        template_name = tmpl.replace(
            "{r}", _resource_phrase(pf_display or current, vocab))

    out: list[str] = []

    def _add(c: str | None) -> None:
        if c and c.strip() and c not in out:
            out.append(c.strip())

    # Track C — maintainer-authored labels lead (highest authority below LAW).
    for a in authored:
        _add(polish_display_casing(str(a), vocab))

    polished_current = polish_display_casing(current, vocab)
    current_clean = not display_law_violations(
        polished_current, vocab, pf_display=pf_display)
    if (synthesized or twin) or not current_clean:
        _add(template_name)
        _add(polished_current)
    else:
        _add(polished_current)
        _add(template_name)
    return out


# ── Pin channel (keeper — content-derived prev-scan join) ───────────────


def _prev_pf_displays(prev_scan: Mapping[str, Any] | None) -> tuple[
    dict[str, str], dict[str, str],
]:
    """(by_anchor_id, by_slug) display maps from an explicit prev scan."""
    by_anchor: dict[str, str] = {}
    by_slug: dict[str, str] = {}
    for row in ((prev_scan or {}).get("product_features") or []):
        if not isinstance(row, dict):
            continue
        disp = str(row.get("display_name") or row.get("name") or "").strip()
        if not disp:
            continue
        aid = str(row.get("anchor_id") or "").strip()
        slug = str(row.get("name") or row.get("id") or "").strip()
        if aid and aid not in by_anchor:
            by_anchor[aid] = disp
        if slug and slug not in by_slug:
            by_slug[slug] = disp
    return by_anchor, by_slug


# ── Stage runner ────────────────────────────────────────────────────────


@dataclass
class _PendingItem:
    """One display decision forwarded to the PM Labeler (keyed scans)."""

    kind: str                 # "pf" | "uf"
    key: str                  # pf slug / uf id (stable within the scan)
    current: str
    candidates: list[str]
    context: dict[str, Any] = field(default_factory=dict)
    obj: Any = None
    pf_display: str | None = None  # UF items: the owning PF display


_action_families_cache: dict[str, Any] | None = None


def _action_family_index(vocab: Mapping[str, Any]) -> dict[str, Any]:
    """Reverse verb→family index + id-markers from the packaged action-family
    vocab (browse/read/create/update/delete/act). Cached; loaded directly to
    avoid a circular import with journey_lattice."""
    global _action_families_cache
    if _action_families_cache is None:
        try:
            av = load_yaml(_ACTION_FAMILIES_VOCAB_FILE)
        except Exception:  # noqa: BLE001 — vocab optional; laws degrade to no-op
            av = {}
        verb2fam: dict[str, str] = {}
        for fam in ("browse", "read", "create", "update", "delete", "act"):
            for v in (av.get(fam) or ()):
                verb2fam[str(v).lower()] = fam
        _action_families_cache = {
            "verb2fam": verb2fam,
            "id_markers": {str(m).lower() for m in (av.get("id_markers") or ("id",))},
        }
    return _action_families_cache


def _flow_action_family(name: str, idx: Mapping[str, Any]) -> str | None:
    """Coarse CRUD family of a flow name's LEADING verb (mirrors the lattice's
    ``_action_family``: GET-class reads split by an id marker)."""
    toks = [t for t in re.split(r"[^a-z0-9]+", str(name or "").lower()) if t]
    if not toks:
        return None
    fam = idx["verb2fam"].get(toks[0])
    if fam == "read":
        return "view" if (idx["id_markers"] & set(toks)) else "browse"
    return fam


def _name_lead_family(display: str, idx: Mapping[str, Any]) -> str | None:
    """The CRUD family a UF DISPLAY name's leading word claims ('Create and
    manage webhooks' → 'create'). Reads never over-claim, so map read→its
    write-less family only when unambiguous."""
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", str(display or "").lower()) if t]
    if not toks:
        return None
    fam = idx["verb2fam"].get(toks[0])
    if fam == "read":
        return "view" if "id" in toks else "browse"
    return fam


def _action_family_from_domain(domain: str) -> str | None:
    """``lattice:action:<res>-<fam>`` → ``<fam>`` (the labeler-collapsed
    action child's true family)."""
    d = str(domain or "")
    if not d.startswith("lattice:action:"):
        return None
    key = d.split("lattice:action:", 1)[1]
    fam = key.rsplit("-", 1)[-1] if "-" in key else ""
    return fam if fam in _ACTION_FAMILY_WORD else None


def _uf_protected(uf: Any, authored_ids: set[str], keeper_on: bool) -> bool:
    """Maintainer-authored or pinned journeys are never re-worded by the laws."""
    if str(getattr(uf, "id", "") or "") in authored_ids:
        return True
    return bool(
        keeper_on and isinstance(getattr(uf, "identity", None), dict)
        and (uf.identity or {}).get("pinned_from")
    )


def _conf_hist(user_flows: Iterable[Any]) -> dict[str, int]:
    h = {"high": 0, "medium": 0, "low": 0}
    for u in user_flows:
        c = str(getattr(u, "name_confidence", "") or "low")
        h[c] = h.get(c, 0) + 1
    return h


def _apply_uf_name_laws(
    user_flows: list[Any],
    pf_by_slug: Mapping[str, Any],
    vocab: Mapping[str, Any],
    flow_name_by_id: Mapping[str, str],
    tele: dict[str, Any],
    *,
    authored_ids: set[str],
    keeper_on: bool,
) -> None:
    """B9 — three deterministic UF display laws over FINAL members/names, run
    AFTER the labeler so labeler-introduced collisions/over-claims are caught.
    Mutates ONLY ``uf.name`` / ``uf.name_confidence`` — identity, membership,
    product_feature_id, paths untouched (I12/I14/I15/I16-neutral)."""
    from collections import defaultdict

    idx = _action_family_index(vocab)

    def _members(uf: Any) -> list[str]:
        return [flow_name_by_id.get(str(m), str(m))
                for m in (getattr(uf, "member_flow_ids", None) or [])]

    def _mfams(names: list[str]) -> set[str]:
        return {f for f in (_flow_action_family(n, idx) for n in names) if f}

    def _pfd(uf: Any) -> str:
        pf = pf_by_slug.get(str(getattr(uf, "product_feature_id", None) or ""))
        return (str(getattr(pf, "display_name", None) or getattr(pf, "name", "") or "")
                if pf is not None else "")

    def _res(uf: Any) -> str:
        base = str(getattr(uf, "resource", "") or "") or _pfd(uf)
        base = re.sub(r"[-_]+", " ", base).strip()
        return _resource_phrase(base, vocab) or base

    def _folded(uf: Any) -> str:
        return str(getattr(uf, "name", "") or "").strip().lower()

    def _is_lattice(uf: Any) -> bool:
        return (str(getattr(uf, "domain", "") or "").startswith("lattice")
                or str(getattr(uf, "id", "") or "").startswith("UF-L-"))

    ordered = sorted(user_flows, key=lambda u: str(getattr(u, "id", "") or ""))
    narrowed: set[str] = set()
    qualified: set[str] = set()

    # ── Law B — name-claim narrowing (ORGANIC journeys only) ─────────
    # A name that LEADS with a write family (create/update/delete) while EVERY
    # member is a read (browse/view) claims an action absent from the evidence
    # → narrow to the strictly-narrower read name "<Browse|View> <resource>"
    # (never widen to the generic "Manage" fallback). Mixed-member over-claims
    # (some act/write member) are left named but flagged low by Law C, and
    # justified wide names (a real write member) are untouched entirely.
    # Lattice action children are EXEMPT — their canonical "<Family>
    # <resource>" identity is authored by Law A, not a false claim.
    for uf in ordered:
        if _uf_protected(uf, authored_ids, keeper_on) or _is_lattice(uf):
            continue
        names = _members(uf)
        if not names:
            continue
        mfams = _mfams(names)
        lead = _name_lead_family(str(getattr(uf, "name", "") or ""), idx)
        if (lead in _WRITE_FAMILIES and mfams and mfams <= _READ_FAMILIES
                and lead not in mfams):
            word = "View" if "view" in mfams else "Browse"
            cand = polish_display_casing(f"{word} {_res(uf)}".strip(), vocab)
            if (cand and cand.strip().lower() != _folded(uf)
                    and not display_law_violations(cand, vocab, pf_display=_pfd(uf) or None)):
                uf.name = cand
                narrowed.add(str(getattr(uf, "id", "") or ""))
                tele["uf_claim_narrowed"] = tele.get("uf_claim_narrowed", 0) + 1

    # ── Law A — UF-vs-UF display uniqueness (never a numeric suffix) ──
    # step 1: undo the labeler's lossy collapse — a colliding lattice action
    # child reverts to its deterministic "<Family> <resource>" name.
    groups: dict[str, list[Any]] = defaultdict(list)
    for uf in ordered:
        groups[_folded(uf)].append(uf)
    for _f, grp in sorted(groups.items()):
        if len(grp) < 2:
            continue
        for uf in grp:
            if _uf_protected(uf, authored_ids, keeper_on):
                continue
            fam = _action_family_from_domain(str(getattr(uf, "domain", "") or ""))
            if not fam:
                continue
            cand = polish_display_casing(
                f"{_ACTION_FAMILY_WORD[fam]} {_res(uf)}".strip(), vocab)
            if (cand and cand.strip().lower() != _folded(uf)
                    and not display_law_violations(cand, vocab, pf_display=_pfd(uf) or None)):
                uf.name = cand
    # step 2: qualify any residual collision from distinguishing evidence.
    taken: dict[str, str] = {}
    for uf in ordered:
        taken.setdefault(_folded(uf), str(getattr(uf, "id", "") or ""))
    groups = defaultdict(list)
    for uf in ordered:
        groups[_folded(uf)].append(uf)
    for _f, grp in sorted(groups.items()):
        if len(grp) < 2:
            continue
        keep = grp[0]  # smallest id keeps the base display
        for uf in grp[1:]:
            if _uf_protected(uf, authored_ids, keeper_on):
                continue
            base = str(getattr(uf, "name", "") or "")
            quals: list[str] = []
            # distinct resource, then owning PF, then route/domain tail
            r = str(getattr(uf, "resource", "") or "")
            if r and r.strip().lower() != str(getattr(keep, "resource", "") or "").strip().lower():
                quals.append(_resource_phrase(r, vocab) or r)
            pfd = _pfd(uf)
            if pfd and pfd.strip().lower() != _pfd(keep).strip().lower():
                quals.append(pfd)
            fam = _action_family_from_domain(str(getattr(uf, "domain", "") or ""))
            if fam:
                quals.append(_ACTION_FAMILY_WORD[fam].lower())
            for q in quals:
                cand = polish_display_casing(f"{base} ({q})", vocab)
                fld = cand.strip().lower()
                if (fld not in taken
                        and not display_law_violations(cand, vocab, pf_display=pfd or None)):
                    uf.name = cand
                    taken[fld] = str(getattr(uf, "id", "") or "")
                    qualified.add(str(getattr(uf, "id", "") or ""))
                    tele["uf_uniqueness_qualified"] = tele.get("uf_uniqueness_qualified", 0) + 1
                    break

    # ── Law C — evidence-derived name_confidence (one rubric, all sources) ──
    tele["confidence_before"] = _conf_hist(user_flows)
    for uf in ordered:
        uid = str(getattr(uf, "id", "") or "")
        names = _members(uf)
        if not names:
            uf.name_confidence = "low"
            continue
        if uid in narrowed:
            uf.name_confidence = "low"
            continue
        mfams = _mfams(names)
        lead = _name_lead_family(str(getattr(uf, "name", "") or ""), idx)

        # resource-grounded: the name's resource phrase overlaps the PF display
        # (member-grounded) or a member flow name — singular/plural-robust so
        # "webhook" grounds against "webhooks".
        def _sing(t: str) -> str:
            return t[:-1] if (t.endswith("s") and len(t) > 3) else t

        def _toks(text: str) -> set[str]:
            return {_sing(t) for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}

        res_toks = _toks(_res(uf))
        member_toks = set().union(*(_toks(n) for n in names)) if names else set()
        res_grounded = bool(res_toks & member_toks) or bool(res_toks & _toks(_pfd(uf)))
        # A specific verb must be performed by a member; a generic lead
        # (Manage/Overview — not in any CRUD family) is grounded by any member
        # action (it abstracts, it does not over-claim a specific verb).
        verb_grounded = (lead in mfams) or (lead is None and len(mfams) >= 1)
        if res_grounded and verb_grounded and uid not in qualified:
            uf.name_confidence = "high"
        elif res_grounded:
            uf.name_confidence = "medium"
        else:
            uf.name_confidence = "low"
    tele["confidence_after"] = _conf_hist(user_flows)


def run_naming_contract(
    product_features: list[Any],
    user_flows: list[Any],
    flows: Iterable[Any] = (),
    *,
    prev_scan: Mapping[str, Any] | None = None,
    keeper_on: bool = True,
    product_strings: Any = None,
    routes_index: Iterable[Mapping[str, Any]] | None = None,
    thesis_tokens: Iterable[str] = (),
    uf_authored_names: Mapping[str, Iterable[str]] | None = None,
    labeler: Callable[[list[_PendingItem]], dict[str, Any]] | None = None,
    verifier: Callable[[list[dict[str, Any]]], dict[str, bool]] | None = None,
) -> dict[str, Any]:
    """Apply the display-name contract in place; return telemetry.

    Order of authority per display: LAW > PIN > candidate rank. The
    ``labeler`` seam (Wave-3 persona, keyed scans) receives the pending
    items and returns ``{item_key: telemetry-dict}`` decisions — it is
    injected by the caller so this module stays LLM-free and fully
    unit-testable (keyless scans pass ``labeler=None`` and take the
    deterministic top choice everywhere).
    """
    vocab = load_naming_vocab()
    tele: dict[str, Any] = {
        "enabled": True,
        "pf_total": len(product_features),
        "uf_total": len(user_flows),
        "pf_renamed": 0,
        "pf_pinned": 0,
        "pf_pin_rejected_law": 0,
        "uf_renamed": 0,
        "uf_twins_resolved": 0,
        "uf_synth_named": 0,
        "uf_pin_overridden_by_law": 0,
        "laws_fixed": {},
        "display_collisions_qualified": 0,
        "casing_polished": 0,
    }
    # B16 PF dev-grain law telemetry — added ONLY when the law is ON so a
    # ``FAULTLINE_PF_NAME_LAW=0`` scan_meta.naming_contract is byte-identical
    # to the pre-B16 emission.
    if pf_name_law_enabled():
        tele["pf_devgrain_stripped"] = 0
        tele["pf_devgrain_collision"] = 0

    def _law_fix(law: str) -> None:
        tele["laws_fixed"][law] = tele["laws_fixed"].get(law, 0) + 1

    _authored_map: Mapping[str, Iterable[str]] = uf_authored_names or {}

    def _authored_for(uf: Any) -> list[str]:
        return list(_authored_map.get(str(getattr(uf, "id", "") or ""), ()) or ())

    # Flow display names by member id (verb evidence for UF templates).
    flow_name_by_id: dict[str, str] = {}
    for fl in flows or ():
        for key in (getattr(fl, "uuid", None), getattr(fl, "name", None)):
            if key:
                flow_name_by_id.setdefault(str(key), str(getattr(fl, "name", "") or ""))

    nav_labels = nav_labels_for_pfs(
        product_features, product_strings, routes_index)
    prev_by_anchor, prev_by_slug = (
        _prev_pf_displays(prev_scan) if (keeper_on and prev_scan) else ({}, {})
    )

    pending: list[_PendingItem] = []

    # ── Pass 1: product features (pin > candidates; laws gate both) ──
    taken: dict[str, str] = {}  # case-folded display -> pf slug
    pf_by_slug: dict[str, Any] = {}
    for pf in sorted(product_features,
                     key=lambda p: str(getattr(p, "name", "") or "")):
        slug = str(getattr(pf, "name", "") or "")
        pf_by_slug[slug] = pf
        current = str(
            getattr(pf, "display_name", None)
            or (_display_word(slug, vocab) if slug else "")
        )
        anchor_id = str(getattr(pf, "anchor_id", None) or "")
        candidates = build_pf_candidates(
            pf, vocab, nav_label=nav_labels.get(slug))

        chosen: str | None = None
        pinned = False
        pin = (prev_by_anchor.get(anchor_id) if anchor_id else None) or (
            prev_by_slug.get(slug) if slug else None)
        if pin:
            pin_polished = polish_display_casing(pin, vocab)
            folded = pin_polished.strip().lower()
            if display_law_violations(pin_polished, vocab):
                tele["pf_pin_rejected_law"] += 1
            elif folded in taken:
                tele["pf_pin_rejected_law"] += 1
            else:
                chosen, pinned = pin_polished, True

        if chosen is None:
            for cand in candidates:
                folded = cand.strip().lower()
                if display_law_violations(cand, vocab):
                    continue
                if folded in taken:
                    continue
                chosen = cand
                break
        if chosen is None:
            # Every candidate law-dirty or colliding: qualify with the
            # anchor qualifier (uniqueness law), else derive a lawful
            # display from the SLUG's words (param/extension glyphs
            # cannot survive canonical_slug), else keep the polished
            # current verbatim (never-worse; mint guaranteed uniqueness).
            base, qual = humanize_anchor_display(anchor_id, vocab)
            fallback = polish_display_casing(current, vocab)
            bq = f"{base} ({qual})" if base and qual else None
            if humanize_route_names_enabled():
                # B2: the qualified fallback must be LAW-clean AND free of
                # route-template residue (the pre-B2 path emitted
                # "T.$team URL+ (Authenticated+)" here without checking).
                if (bq and not display_law_violations(bq, vocab)
                        and not _has_route_template_residue(bq)
                        and bq.strip().lower() not in taken):
                    fallback = bq
                    tele["display_collisions_qualified"] += 1
                elif (base and not display_law_violations(base, vocab)
                        and not _has_route_template_residue(base)
                        and base.strip().lower() not in taken):
                    fallback = base
                elif (display_law_violations(fallback, vocab)
                        or _has_route_template_residue(fallback)):
                    scrubbed = _strip_display_residue(fallback, vocab)
                    from_slug = _display_word(slug, vocab) if slug else None
                    if (scrubbed and not display_law_violations(scrubbed, vocab)
                            and scrubbed.strip().lower() not in taken):
                        fallback = scrubbed
                    elif (from_slug
                            and not display_law_violations(from_slug, vocab)
                            and from_slug.strip().lower() not in taken):
                        fallback = from_slug
            elif bq and bq.strip().lower() not in taken:
                fallback = bq
                tele["display_collisions_qualified"] += 1
            elif display_law_violations(fallback, vocab) and slug:
                from_slug = _display_word(slug, vocab)
                if (from_slug
                        and not display_law_violations(from_slug, vocab)
                        and from_slug.strip().lower() not in taken):
                    fallback = from_slug
            chosen = fallback

        # ── B16: PF dev-grain suffix law (LAW > PIN, display channel) ──
        # A route-dir leak ('Policy Page' from route:policy-page) strips to
        # the capability ('Policy'); a strip that collides with a sibling PF
        # is kept as-is (the Part-2 unification signal). Runs after the
        # existing selection so it also overrides a stale pin.
        if pf_name_law_enabled():
            new_chosen, stripped_law = _apply_pf_devgrain_law(
                chosen, anchor_id, slug, vocab, taken, tele)
            if stripped_law:
                chosen, pinned = new_chosen, False

        taken[chosen.strip().lower()] = slug
        if pinned:
            tele["pf_pinned"] += 1
        if chosen != current:
            for law in display_law_violations(current, vocab):
                _law_fix(law)
            if chosen == polish_display_casing(current, vocab):
                tele["casing_polished"] += 1
            else:
                tele["pf_renamed"] += 1
            pf.display_name = chosen
        elif getattr(pf, "display_name", None) != chosen:
            pf.display_name = chosen
        if not pinned and len(candidates) > 1 and labeler is not None:
            pending.append(_PendingItem(
                kind="pf", key=slug, current=chosen,
                candidates=[c for c in candidates
                            if not display_law_violations(c, vocab)
                            # B16: never offer the persona a dev-grain-leak
                            # candidate (would re-introduce "Page").
                            and not (pf_name_law_enabled()
                                     and _strip_pf_devgrain_suffix(
                                         c, anchor_id, vocab))],
                context={
                    "anchor_id": anchor_id,
                    "nav_label": nav_labels.get(slug),
                },
                obj=pf,
            ))

    # ── Pass 2: user flows (pins respected; twins/synths templated) ──
    for uf in user_flows:
        pf = pf_by_slug.get(str(getattr(uf, "product_feature_id", None) or ""))
        pf_display = (
            str(getattr(pf, "display_name", None) or getattr(pf, "name", ""))
            if pf is not None else ""
        )
        current = str(getattr(uf, "name", "") or "")
        member_names = [
            flow_name_by_id.get(str(m), str(m))
            for m in (getattr(uf, "member_flow_ids", None) or [])
        ]
        candidates = build_uf_candidates(
            uf, pf, vocab, member_names, authored=_authored_for(uf))
        violations = display_law_violations(
            polish_display_casing(current, vocab), vocab,
            pf_display=pf_display or None)
        pinned = bool(
            keeper_on and isinstance(getattr(uf, "identity", None), dict)
            and (uf.identity or {}).get("pinned_from")
        )
        if pinned and not violations:
            continue  # stability wins — pinned, law-clean
        if pinned and violations:
            tele["uf_pin_overridden_by_law"] += 1

        chosen = None
        for cand in candidates:
            if not display_law_violations(cand, vocab, pf_display=pf_display or None):
                chosen = cand
                break
        if chosen is None:
            chosen = polish_display_casing(current, vocab)

        if chosen != current:
            for law in violations:
                _law_fix(law)
            if "pf_uf_twin" in violations:
                tele["uf_twins_resolved"] += 1
            if getattr(uf, "synthesized", False):
                tele["uf_synth_named"] += 1
            if chosen == polish_display_casing(current, vocab) and not violations:
                tele["casing_polished"] += 1
            else:
                tele["uf_renamed"] += 1
            uf.name = chosen
        if (labeler is not None and not pinned
                and (violations or getattr(uf, "synthesized", False))):
            pending.append(_PendingItem(
                kind="uf", key=str(getattr(uf, "id", "") or ""),
                current=chosen or current,
                candidates=[
                    c for c in candidates
                    if not display_law_violations(
                        c, vocab, pf_display=pf_display or None)
                ],
                context={
                    "pf_display": pf_display,
                    "synthesized": bool(getattr(uf, "synthesized", False)),
                    "member_flows": member_names[:8],
                },
                obj=uf,
                pf_display=pf_display or None,
            ))

    # ── Pass 3: PM Labeler (keyed persona seam — Wave 3 §4.7) ────────
    # The persona returns VALIDATED picks; this stage stays the single
    # display writer and re-checks the laws before applying (defense in
    # depth — a persona bug can never ship a law-violating display).
    if labeler is not None and pending:
        try:
            lab_result = dict(labeler(pending) or {})
        except Exception as exc:  # noqa: BLE001 — persona must never break a scan
            lab_result = {"error": str(exc)}
        choices = lab_result.pop("choices", None) or {}
        applied = 0
        # PF picks first — UF twin checks below must see the LIVE
        # (post-pick) capability displays, or a PF pick could re-create
        # the very twin the UF pick was validated against.
        for item in pending:
            pick = choices.get(item.key)
            if (item.kind != "pf" or not isinstance(pick, str)
                    or not pick.strip()):
                continue
            pick = " ".join(pick.split())
            if display_law_violations(pick, vocab):
                continue
            if pick != str(getattr(item.obj, "display_name", "") or ""):
                item.obj.display_name = pick
                applied += 1
        for item in pending:
            pick = choices.get(item.key)
            if (item.kind != "uf" or not isinstance(pick, str)
                    or not pick.strip()):
                continue
            pick = " ".join(pick.split())
            uf_obj = item.obj  # UF display channel is ``name``
            live_pf = pf_by_slug.get(
                str(getattr(uf_obj, "product_feature_id", None) or ""))
            live_pf_display = (
                str(getattr(live_pf, "display_name", None)
                    or getattr(live_pf, "name", "") or "")
                if live_pf is not None else (item.pf_display or "")
            )
            if display_law_violations(
                pick, vocab, pf_display=live_pf_display or None,
            ):
                continue
            if pick != str(getattr(uf_obj, "name", "") or ""):
                uf_obj.name = pick
                applied += 1
        lab_result["applied"] = applied
        tele["labeler"] = lab_result

        # Final twin sweep against the LIVE displays: a PF pick can twin
        # an untouched UF name ("GoCardless" PF pick vs a "GoCardless"
        # journey the labeler never saw). Law > pick: re-template.
        for uf in user_flows:
            live_pf = pf_by_slug.get(
                str(getattr(uf, "product_feature_id", None) or ""))
            if live_pf is None:
                continue
            live_disp = str(
                getattr(live_pf, "display_name", None)
                or getattr(live_pf, "name", "") or "")
            if not live_disp or (
                str(getattr(uf, "name", "") or "").strip().lower()
                != live_disp.strip().lower()
            ):
                continue
            member_names = [
                flow_name_by_id.get(str(m), str(m))
                for m in (getattr(uf, "member_flow_ids", None) or [])
            ]
            for cand in build_uf_candidates(
                    uf, live_pf, vocab, member_names,
                    authored=_authored_for(uf)):
                if not display_law_violations(
                    cand, vocab, pf_display=live_disp,
                ):
                    uf.name = cand
                    tele["uf_twins_resolved"] += 1
                    _law_fix("pf_uf_twin")
                    break

    # ── Pass 4: Draft Verifier over backstop-synthesized UFs (§4.7) ──
    # The chain4 'schema.json'-class guard: a synthesized journey whose
    # draft (post-laws, post-labeler) still fails the persona's honesty
    # review reverts to the deterministic journey TEMPLATE (fold is
    # structurally impossible for a backstop synth — it exists only
    # because no sibling journey covers its PF; dropping would re-arm
    # I8). Rejects never block; keyless (verifier=None) skips.
    if verifier is not None:
        synth_ufs = [u for u in user_flows if getattr(u, "synthesized", False)]
        if synth_ufs:
            drafts = []
            for uf in synth_ufs:
                pf = pf_by_slug.get(
                    str(getattr(uf, "product_feature_id", None) or ""))
                drafts.append({
                    "id": str(getattr(uf, "id", "") or ""),
                    "kind": "synth_uf",
                    "draft": str(getattr(uf, "name", "") or ""),
                    "pf_display": (
                        str(getattr(pf, "display_name", None)
                            or getattr(pf, "name", "") or "")
                        if pf is not None else ""
                    ),
                    "member_flows": [
                        flow_name_by_id.get(str(m), str(m))
                        for m in (getattr(uf, "member_flow_ids", None) or [])
                    ][:8],
                    "synthesis_reason": str(
                        getattr(uf, "synthesis_reason", None) or ""),
                })
            try:
                verdicts = verifier(drafts) or {}
            except Exception as exc:  # noqa: BLE001 — persona never breaks a scan
                verdicts = {}
                tele["verifier_error"] = str(exc)
            rejected = 0
            for uf in synth_ufs:
                if verdicts.get(str(getattr(uf, "id", "") or "")) is not False:
                    continue  # accept (explicit or default) — keep draft
                rejected += 1
                pf = pf_by_slug.get(
                    str(getattr(uf, "product_feature_id", None) or ""))
                pf_display = (
                    str(getattr(pf, "display_name", None)
                        or getattr(pf, "name", "") or "")
                    if pf is not None else ""
                )
                member_names = [
                    flow_name_by_id.get(str(m), str(m))
                    for m in (getattr(uf, "member_flow_ids", None) or [])
                ]
                for cand in build_uf_candidates(uf, pf, vocab, member_names):
                    if not display_law_violations(
                        cand, vocab, pf_display=pf_display or None,
                    ):
                        uf.name = cand
                        break
            tele["verifier_synth_reviewed"] = len(drafts)
            tele["verifier_synth_rejected"] = rejected

    # Post-labeler uniqueness re-check (a labeler pick could collide):
    # first-come (slug-sorted) keeps its display, later duplicates revert
    # to their pre-labeler display recorded in ``taken``.
    seen: dict[str, str] = {}
    for pf in sorted(product_features,
                     key=lambda p: str(getattr(p, "name", "") or "")):
        disp = str(getattr(pf, "display_name", None) or "")
        folded = disp.strip().lower()
        if not folded:
            continue
        if folded in seen:
            base, qual = humanize_anchor_display(
                str(getattr(pf, "anchor_id", None) or ""), vocab)
            slug = str(getattr(pf, "name", "") or "")
            requalified = (
                f"{base} ({qual})" if base and qual
                else polish_display_casing(slug.replace("-", " ").title(), vocab)
            )
            pf.display_name = requalified
            tele["display_collisions_qualified"] += 1
        else:
            seen[folded] = str(getattr(pf, "name", "") or "")

    # ── B9 UF name laws (post-labeler, evaluated over FINAL members) ──
    # Runs AFTER the labeler so labeler-introduced collisions/over-claims are
    # caught; composes with membership fixes (evaluates ACTUAL members).
    if uf_name_laws_enabled():
        _apply_uf_name_laws(
            user_flows, pf_by_slug, vocab, flow_name_by_id, tele,
            authored_ids={str(k) for k in _authored_map.keys()},
            keeper_on=keeper_on,
        )

    tele["labeler_pending"] = len(pending)
    return tele
