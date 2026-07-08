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
    "naming_contract_enabled",
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


def naming_contract_enabled() -> bool:
    """Default ON; ``FAULTLINE_NAMING_CONTRACT=0`` restores pre-W3 output."""
    return os.environ.get(NAMING_CONTRACT_ENV, "1").strip().lower() not in {
        "0", "false",
    }


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
    code-location segments drop."""
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
            if base and qual and f"{base} ({qual})".strip().lower() not in taken:
                fallback = f"{base} ({qual})"
                tele["display_collisions_qualified"] += 1
            elif display_law_violations(fallback, vocab) and slug:
                from_slug = _display_word(slug, vocab)
                if (from_slug
                        and not display_law_violations(from_slug, vocab)
                        and from_slug.strip().lower() not in taken):
                    fallback = from_slug
            chosen = fallback

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
                            if not display_law_violations(c, vocab)],
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

    tele["labeler_pending"] = len(pending)
    return tele
