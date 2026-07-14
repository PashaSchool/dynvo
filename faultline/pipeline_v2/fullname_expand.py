"""B56 — full-name display law for abbreviations (DISPLAY CHANNEL ONLY).

An operator class (2026-07-13): "names that cannot be pronounced as a word;
an abbreviation must ALWAYS carry its full name." A board tile displayed as a
bare abbreviation — ``Pbac``, ``Sso``, ``Ooo``, ``I18n``, ``Wp`` — is opaque.
When the repo ITSELF spells the full form (a code identifier, an i18n KEY, a
JSX label, the anchor package's manifest, a route segment) the display becomes
``Full Name (ABBR)`` — "Single Sign-On (SSO)", "Out of Office (OOO)". No
dictionaries, no vendor lists (mechanisms over vocabularies): the expansion is
GROUNDED in what the maintainer wrote inside the repo, never invented.

Sibling of :mod:`manifest_display` (the B27 template): an ENV gate, bounded
best-effort file reads, ``lru_cache``, never a crash, always a fall-through.
Kill-switch: ``FAULTLINE_PF_FULLNAME_LAW`` (default **OFF**) — unset restores
the pre-B56 display byte-identically.

DISPLAY CHANNEL ONLY (the B16/B27/B50 pattern): the naming contract feeds this
as a ranked display-name override; ``ProductFeature.name`` slugs, ``anchor_id``,
``product_feature_id``, ``member_files``, ``paths``, lineage — every identity
field — are never touched. Only ``PF.display_name`` and (via inheritance)
``UserFlow.name`` change.

Detector — a cheap SHAPE filter (``is_abbreviation_shape``). A leading display
token is abbreviation-shaped if ANY prong fires:

* **P1** zero vowels AND ``len <= 4``           — ``Wp``, ``TLS``, ``Trpc``.
* **P2** a digit embedded in a short token      — ``i18n``, ``a11y``, ``k8s``.
* **P3** ``len <= 3`` and not a product word     — ``Di``, ``Sso``, ``Ooo``.
* **P4** UNPRONOUNCEABLE shape (len 4-9, has a
  vowel, invalid leading consonant cluster)      — ``Pbac`` ("pb"),
  ``Htmltopdf`` ("ht"), ``Rbac``, ``Ldap``.

P4 is the ONLY prong that catches ``Pbac`` (it has a vowel and len 4, so the
literal P1-P3 prongs from the spec miss it). It fires on STRUCTURE, not on
evidence: a token whose leading consonant run is not a real English onset
cannot be pronounced as a word — exactly the operator's class. That is what
distinguishes ``Pbac`` (unpronounceable ⇒ shape-flagged ⇒ honest debt when the
repo carries no allowed expansion) from a legitimate word like ``Bulk`` /
``Link`` / ``Post`` / ``Sign`` / ``Form`` (a valid onset ⇒ never flagged).

Honest debt: a shape-flagged token with NO allowed evidence keeps its display
unchanged and reports ``missing:expansion`` — measured, never invented. In
particular ``Pbac``'s full form lives ONLY in a JSDoc comment in cal.com, and
COMMENTS ARE NOT A SOURCE here (comment text is stripped before any scan), so
``Pbac`` is ``missing:expansion``, not auto-expanded.

False-expansion filters (round-3 census class: a coincidental initials
collision is WORSE than missing — it invents meaning). Four MECHANICAL
filters, no dictionaries:

* **F1 SELF-ECHO** — a candidate expansion in which ANY word equals the token
  is rejected (``edrDateRanges`` → "Edr Date Ranges" for EDR).
* **F2 LITERAL-INITIALS OFF** — plain string literals / JSX text participate
  ONLY via the explicit author gloss ``"Full Phrase (ABBR)"``; their word
  initials are never matched ("Missing code parameter" ≠ MCP's meaning).
* **F3 PLAIN-WORD GUARD** — every word of a candidate expansion must read as
  a plain word (or minor glue); ``extract_dv_rows`` → "Extract Dv Rows" dies
  on the non-word "Dv".
* **F4 TOKEN-HOME GUARD** — identifier-initials evidence counts only when the
  citing file carries the token as a whole path segment (``packages/sso/``),
  the entire basename stem, or an UPPERCASE-BOUNDED acronym in the basename
  (``PrismaOOORepository``); a coincidental interface in a foreign module
  (``PlatformActorMetadata`` in ``audit-log-types.ts`` for PAM) or a
  lowercase kebab fragment (``pg-meta-column-privileges.ts`` for PG) is not
  the token's meaning.

INTERPLAY (do not "simplify" these into fewer filters): the census false
cases ``extract_dv_rows`` (services/edr/) and ``"Missing code parameter"``
(features/mcp/) DO sit inside their token's home — F4 alone passes them; F3
and F2 respectively are what kill them. Conversely F4 is the only filter that
kills a plain-worded coincidence outside the home (PAM). Gloss / manifest /
numeric / route+brand sources are exempt from F4 (self-evident author intent)
but still pass F1 + F3. And do NOT add a ">= 2 distinct identifiers" rule:
cal.com's Ooo has exactly ONE identifier root in its citing file and is a
TRUE expansion.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple

import yaml

from faultline.pipeline_v2.data import load_yaml
from faultline.pipeline_v2.manifest_display import (
    _authored_slug_name,
    package_dir_of_anchor,
)

__all__ = [
    "FULLNAME_LAW_ENV",
    "pf_fullname_law_enabled",
    "load_fullname_whitelist",
    "is_abbreviation_shape",
    "expand_abbreviation",
    "compose_display",
    "apply_fullname_expansion",
    "ExpansionResult",
]

FULLNAME_LAW_ENV = "FAULTLINE_PF_FULLNAME_LAW"

_WHITELIST_FILE = "fullname-whitelist.yaml"
_VOCAB_FILE = "naming-contract-vocab.yaml"

#: Bounded reads — a manifest / member file is small; anything bigger is not a
#: source we grovel through (a work bound, not a tuning knob).
_MAX_FILE_BYTES = 256 * 1024
#: Bounded evidence scan — the expansion is best-effort; we never walk an
#: unbounded member set (scale-invariant work cap).
_MAX_MEMBER_FILES = 60
#: An authored full form is a short human phrase, not a prose blob.
_MAX_FULL_CHARS = 120

_VOWELS = frozenset("aeiou")

#: Whole-word "minor" tokens dropped from an acronym's initials AND lower-cased
#: in a rendered full phrase (never the first word). Structural English glue,
#: not a domain vocabulary.
_MINOR_WORDS = frozenset({
    "of", "and", "the", "a", "an", "to", "for", "with", "or", "by",
    "at", "from", "as", "&",
})

#: Valid English leading consonant clusters (len >= 2). GENEROUS by design: a
#: token whose leading consonant run is NOT here (and len >= 2) reads as
#: unpronounceable ⇒ abbreviation-shaped (P4). Erring generous means we never
#: false-flag a real word (Blob "bl", Chart "ch", Draft "dr", Query "qu"); we
#: merely decline to flag some genuine abbreviations (conservative — a decline
#: costs nothing on the display channel). Single-consonant / vowel-initial
#: onsets are always pronounceable and handled separately.
_VALID_ONSETS = frozenset({
    "bl", "br", "by",
    "ch", "cl", "cr",
    "dr", "dw",
    "fj", "fl", "fr",
    "gh", "gl", "gn", "gr", "gu", "gw",
    "kh", "kl", "kn", "kr", "kw",
    "ph", "pl", "pr", "ps", "pt",
    "qu",
    "rh",
    "sc", "sh", "sk", "sl", "sm", "sn", "sp", "sq", "st", "sv", "sw", "sy",
    "th", "tr", "ts", "tw", "ty",
    "vl", "vr",
    "wh", "wr",
    "chr", "phr", "sch", "scr", "shr", "spl", "spr", "squ", "str", "thr",
})

#: Source-kind priority for CITATION selection when an expansion is confirmed
#: by more than one source (spec §2 order: identifiers → i18n keys / JSX labels
#: → manifest → route). Lower wins. Never affects whether we expand — only
#: which citation we report.
_PRIO: dict[str, int] = {
    "identifier": 1,
    "i18n-key": 2,
    "manifest": 3,
    "route": 4,
}

_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_JSON_KEY_RE = re.compile(r"""["']([A-Za-z_][A-Za-z0-9_.\-]*)["']\s*:""")
#: Path markers that make a file a LOCALE file: its string VALUES are a
#: FORBIDDEN source (operator rule 2026-07-13 — translations may be external /
#: out-of-repo). Only its KEY names are allowed evidence.
_LOCALE_MARKERS = ("/locales/", "/locale/", "/i18n/", "/lang/", "/langs/",
                   "/translations/", "/messages/", "/intl/")

#: Prose-document formats — README / docs / description files. A FORBIDDEN
#: grounding source (project hard rule: no README grounding — maintainer prose
#: is marketing, not structure). A mechanical FORMAT gate, not a name list:
#: any member file in a prose format is skipped wholesale.
_PROSE_DOC_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".adoc", ".txt"})

#: Workspace-package discovery work bound (fix-4 mechanism): one-level glob
#: expansion of the root workspaces config never returns more than this many
#: dirs (a work bound, not a tuning knob).
_MAX_WS_PACKAGE_DIRS = 512


class ExpansionResult(NamedTuple):
    """The full-name outcome for one display.

    ``display``       — the rewritten ``Full Name (ABBR)[ tail]`` or ``None``.
    ``source``        — a citation ``"identifier:path:line"`` / ``"manifest:..."``
                        / ``"i18n-key:..."`` / ``"route"`` on success, else one
                        of ``"missing:expansion"`` (shape-flagged, no evidence —
                        honest debt), ``"not-flagged"``, ``"vendor"``,
                        ``"ambiguous"``.
    ``abbr``          — the lower-cased abbreviation token that was expanded
                        (for UF inheritance), else ``None``.
    ``composed_lead`` — the ``Full Name (ABBR)`` for the LEAD token alone
                        (tail excluded), else ``None``.
    """

    display: str | None
    source: str
    abbr: str | None = None
    composed_lead: str | None = None


def pf_fullname_law_enabled() -> bool:
    """B56 full-name display law. Default **ON** (flipped B62, KEY_SCHEMA
    29); ``FAULTLINE_PF_FULLNAME_LAW`` in ``{0, false, off}`` disables it,
    restoring the pre-B56 display byte-identically."""
    return os.environ.get(FULLNAME_LAW_ENV, "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


@lru_cache(maxsize=1)
def load_fullname_whitelist() -> frozenset[str]:
    """Closed product-word spare list (lower-cased). Best-effort: a missing /
    malformed file yields an empty set (the law simply flags more, never
    crashes)."""
    try:
        data = load_yaml(_WHITELIST_FILE)
    except Exception:  # noqa: BLE001 — data problems degrade, never crash a scan
        return frozenset()
    words = data.get("product_words") if isinstance(data, dict) else None
    return frozenset(
        str(w).strip().lower() for w in (words or []) if str(w).strip()
    )


@lru_cache(maxsize=1)
def _brand_casing() -> dict[str, str]:
    try:
        data = load_yaml(_VOCAB_FILE)
    except Exception:  # noqa: BLE001
        return {}
    raw = data.get("brand_casing") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(k).lower(): str(v) for k, v in raw.items()}


# ── shape detector ──────────────────────────────────────────────────────


def _looks_unpronounceable(word: str) -> bool:
    """True when ``word``'s leading consonant run is a real cluster no English
    word begins with (``pb`` / ``ht`` / ``rb`` / ``ld``). A single-consonant or
    vowel-initial onset is always pronounceable."""
    i = 0
    while i < len(word) and word[i] not in _VOWELS:
        i += 1
    onset = word[:i]
    if len(onset) <= 1:
        return False
    return onset not in _VALID_ONSETS


def is_abbreviation_shape(
    token: str, whitelist: Iterable[str] | None = None,
) -> str | None:
    """Return the SHAPE prong id (``"P1"``..``"P4"``) a display token trips, or
    ``None`` when it reads as an ordinary word. Cheap, evidence-free, pure."""
    t = (token or "").strip()
    if not t:
        return None
    wl = whitelist if whitelist is not None else load_fullname_whitelist()
    low = t.lower()
    if low in wl:
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", t):
        return None  # not a single alnum word (leading letter) — not our shape
    letters = re.sub(r"[^A-Za-z]", "", t)
    has_digit = any(c.isdigit() for c in t)
    has_vowel = any(c in _VOWELS for c in low if c.isalpha())
    # P2 — numeric contraction (a digit inside a short token).
    if has_digit and 2 <= len(letters) <= 6:
        return "P2"
    # P1 — zero vowels AND short.
    if not has_vowel and len(t) <= 4:
        return "P1"
    # P3 — very short and not a product word.
    if len(t) <= 3:
        return "P3"
    # P4 — unpronounceable shape (closes the Pbac gap).
    if has_vowel and 4 <= len(t) <= 9 and _looks_unpronounceable(low):
        return "P4"
    return None


def _is_plain_word(token: str, whitelist: Iterable[str]) -> bool:
    """A phrase-guard predicate: does ``token`` read as an ordinary word (so a
    display made ONLY of such tokens is never flagged — "No Show", "Feature Opt
    In")? Pronounceable (valid onset), has a vowel, no embedded digit, or a
    product-word."""
    t = (token or "").strip()
    if not t:
        return False
    low = t.lower()
    if low in whitelist:
        return True
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", t):
        return False
    if any(c.isdigit() for c in t):
        return False
    if t.isupper() and len(t) >= 2:
        return False  # an ACRONYM (EDR, SSH, SSO, TLS) — never a plain word
    if not any(c in _VOWELS for c in low):
        return False
    return not _looks_unpronounceable(low)


def _expansion_words_ok(disp: str, token: str) -> bool:
    """F1 + F3 over one candidate expansion phrase (see module docstring).

    F1 SELF-ECHO: any word equal to the token (case-insensitive) means the
    "expansion" restates the abbreviation instead of explaining it
    ("Edr Date Ranges" / "Successfully Ssh Host" / "Sse Subscription Error").
    F3 PLAIN-WORD GUARD: every word must read as a plain word (or minor glue)
    — a non-word fragment ("Dv") marks a coincidental identifier, not a
    meaning. Applied to EVERY source's candidates."""
    tok = (token or "").strip().lower()
    wl = load_fullname_whitelist()
    for raw in re.split(r"[^A-Za-z0-9]+", disp or ""):
        if not raw:
            continue
        low = raw.lower()
        if low == tok:
            return False  # F1 — self-echo
        if low in _MINOR_WORDS:
            continue
        if not _is_plain_word(raw, wl):
            return False  # F3 — non-word fragment
    return True


def _token_home(rel: str, token: str) -> bool:
    """F4 — is this file the token's HOME? True when the repo-relative path
    carries the token as a WHOLE directory segment (``packages/sso/…``,
    ``…/features/ooo/…``), as the ENTIRE basename stem (``mcp.py``), or as an
    UPPERCASE-BOUNDED acronym inside the basename (``PrismaOOORepository.ts``
    — the author wrote the token AS an acronym). A lowercase kebab fragment
    is NOT a home: ``pg-meta-column-privileges.ts`` does not home "pg"
    (the census residual — a coincidental ``privilegeGrant`` zod object must
    not become "Privilege Grant (PG)"). Identifier-initials evidence found
    outside the home is a coincidence, not the token's meaning."""
    tok = (token or "").strip().lower()
    if not tok:
        return False
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p]
    if not parts:
        return False
    if any(seg.lower() == tok for seg in parts[:-1]):
        return True
    stem = parts[-1].rsplit(".", 1)[0]
    if stem.lower() == tok:
        return True
    return re.search(
        rf"(?<![A-Z0-9]){re.escape(tok.upper())}(?![a-z0-9])", stem,
    ) is not None


# ── word splitting + acronym maths ──────────────────────────────────────


def _word_tokens(s: str) -> list[str]:
    """Split a string into words on camelCase / PascalCase / snake / kebab /
    dot / digit boundaries. ``outOfOfficeCreateOrUpdate`` →
    ``[out, Of, Office, Create, Or, Update]``; ``out_of_office`` →
    ``[out, of, office]``; ``WordPress`` → ``[Word, Press]``."""
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s or "")
    s2 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s2)
    return [p for p in re.split(r"[^A-Za-z0-9]+", s2) if p]


def _numeric_contract(word: str) -> str:
    """Letter+innercount+letter contraction of a single word:
    ``internationalization`` → ``i18n``, ``accessibility`` → ``a11y``."""
    w = re.sub(r"[^A-Za-z]", "", word or "")
    if len(w) < 3:
        return ""
    return f"{w[0].lower()}{len(w) - 2}{w[-1].lower()}"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _titlecase_phrase(phrase: str) -> str:
    """Title-case a rendered full phrase: capitalise each alphabetic run
    (preserving hyphens — "sign-on" → "Sign-On"), then lower-case whole-word
    minor tokens except the first. Idempotent-ish for display."""
    capped = re.sub(
        r"[A-Za-z]+",
        lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(),
        phrase or "",
    )
    words = capped.split(" ")
    out: list[str] = []
    for idx, w in enumerate(words):
        if idx > 0 and w.lower() in _MINOR_WORDS:
            out.append(w.lower())
        else:
            out.append(w)
    return " ".join(out)


def _verbatim_brand(cand: str) -> str:
    """A manifest brand rendered as authored — canonical brand casing when the
    vocab knows it (``wordpress`` → ``WordPress``), else first-letter-capped
    verbatim (preserves internal capitals: ``WordPress`` stays)."""
    c = (cand or "").strip()
    if not c:
        return c
    brand = _brand_casing().get(c.lower())
    if brand:
        return brand
    return c[:1].upper() + c[1:]


def compose_display(full_form: str, token: str) -> str:
    """``Full Name (ABBR)`` — the canonical B56 display form. The suffix is the
    abbreviation UPPER-cased; the full phrase is passed through verbatim (the
    caller has already cased it)."""
    return f"{full_form} ({(token or '').upper()})"


# ── evidence readers (bounded, best-effort, never crash) ────────────────


def _safe_root(repo_root: Any) -> Path | None:
    if repo_root is None:
        return None
    try:
        return Path(repo_root).resolve()
    except OSError:
        return None


def _resolve(root: Path, rel: str) -> Path | None:
    """A repo-relative path resolved WITHIN the repo (containment), else
    ``None``."""
    try:
        p = (root / rel).resolve()
    except OSError:
        return None
    if root != p and root not in p.parents:
        return None
    return p


def _read_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return None


def _strip_comments(text: str) -> str:
    """Aggressively remove comment text — the maintainer's PROSE is NOT an
    allowed source (the Pbac law). Over-stripping is SAFE (it can only cost us
    a would-be expansion → more honest debt); under-stripping is the danger
    (it would let a comment gloss expand). Line numbering is preserved so
    identifier citations stay accurate."""
    # Block comments /* ... */ (JSDoc included), replaced by matching newlines.
    def _blank(m: re.Match[str]) -> str:
        return "\n" * m.group(0).count("\n")

    text = re.sub(r"/\*.*?\*/", _blank, text or "", flags=re.DOTALL)
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("//", "#", "*", "/*", "<!--", "-->")):
            out.append("")
            continue
        idx = line.find("//")
        if idx != -1:
            line = line[:idx]
        out.append(line)
    return "\n".join(out)


def _is_locale_path(rel: str) -> bool:
    low = ("/" + (rel or "").replace("\\", "/").lstrip("/")).lower()
    return any(mark in low for mark in _LOCALE_MARKERS)


class _Match(NamedTuple):
    norm: str
    display: str
    cite: str
    prio: int


def _match_from_words(
    cand: str, token: str, source: str, cite_body: str, *, brand: bool,
) -> _Match | None:
    """Initials / prefix / numeric match of one candidate string against the
    token. ``brand`` renders the whole candidate verbatim (manifest names);
    otherwise the matched word-prefix is title-cased and joined (identifiers,
    keys, routes)."""
    tok = (token or "").lower()
    raw_words = _word_tokens(cand)
    if not raw_words:
        return None
    words = raw_words
    brand_form: str | None = None
    # A single GLUED word may BE a known brand whose canonical casing reveals
    # its word structure (naming-vocab ``brand_casing`` — the corroboration
    # YAML, not a new dictionary): ``wordpress`` → "WordPress" →
    # ``[Word, Press]`` → initials "wp". The display is the brand verbatim.
    if len(raw_words) == 1:
        b = _brand_casing().get(raw_words[0].lower())
        if b:
            bwords = _word_tokens(b)
            if len(bwords) >= 2:
                brand_form, words = b, bwords
    initials = ""
    for k, w in enumerate(words, 1):
        initials += w[0].lower()
        if initials == tok:
            matched = words[:k]
            if brand_form is not None and k == len(words):
                disp = brand_form
            elif brand and brand_form is None and k == len(words):
                disp = _verbatim_brand(cand.strip())
            else:
                disp = _titlecase_phrase(" ".join(matched))
            if not disp or len(disp) > _MAX_FULL_CHARS:
                return None
            if not _expansion_words_ok(disp, tok):
                return None  # F1 self-echo / F3 non-word fragment
            return _Match(_norm(disp), disp, f"{source}:{cite_body}",
                          _PRIO[source])
        if len(initials) > len(tok):
            break
    if len(raw_words) == 1 and _numeric_contract(cand) == tok:
        disp = _verbatim_brand(cand.strip()) if brand else _titlecase_phrase(cand)
        if (disp and len(disp) <= _MAX_FULL_CHARS
                and _expansion_words_ok(disp, tok)):
            return _Match(_norm(disp), disp, f"{source}:{cite_body}",
                          _PRIO[source])
    return None


def _plausible_gloss(phrase: str, token: str) -> bool:
    """Is ``phrase`` a believable expansion of ``token`` (so a coincidental
    ``(...)`` is not mistaken for a gloss)? Initials of all words, initials of
    non-minor words, or the numeric law must reproduce the token."""
    tok = (token or "").lower()
    words = _word_tokens(phrase)
    if not words:
        return False
    if "".join(w[0].lower() for w in words) == tok:
        return True
    non_minor = [w for w in words if w.lower() not in _MINOR_WORDS]
    if non_minor and "".join(w[0].lower() for w in non_minor) == tok:
        return True
    if len(words) == 1 and _numeric_contract(words[0]) == tok:
        return True
    return False


def _gloss_matches(
    text: str, token: str, source: str, rel: str,
) -> list[_Match]:
    """Explicit-gloss matches: ``Full Phrase (TOKEN)`` written by the author in
    an ALLOWED source (never a comment — callers pass comment-stripped text)."""
    out: list[_Match] = []
    pat = re.compile(
        r"([A-Za-z][A-Za-z0-9 &/'\-]*?[A-Za-z0-9])\s*\(\s*"
        + re.escape(token) + r"\s*\)",
        re.IGNORECASE,
    )
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in pat.finditer(line):
            phrase = " ".join(m.group(1).split())
            if not _plausible_gloss(phrase, token):
                continue
            disp = _titlecase_phrase(phrase)
            if (disp and len(disp) <= _MAX_FULL_CHARS
                    and _expansion_words_ok(disp, token)):
                out.append(_Match(_norm(disp), disp,
                                  f"{source}:{rel}:{lineno}", _PRIO[source]))
    return out


def _evidence_from_file(root: Path, rel: str, token: str) -> list[_Match]:
    # Prose-doc FORMAT gate: README / docs files are a forbidden grounding
    # source (their free text regex-reads as "identifiers"/"labels" — the
    # typebot wordpress README leak). Skipped wholesale.
    if Path(rel).suffix.lower() in _PROSE_DOC_SUFFIXES:
        return []
    path = _resolve(root, rel)
    if path is None:
        return []
    text = _read_text(path)
    if text is None:
        return []
    out: list[_Match] = []
    if _is_locale_path(rel):
        # Locale file — KEY names only; string VALUES are forbidden.
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in _JSON_KEY_RE.finditer(line):
                mm = _match_from_words(
                    m.group(1), token, "i18n-key", f"{rel}:{lineno}",
                    brand=False)
                if mm is not None:
                    out.append(mm)
        return out
    code = _strip_comments(text)
    # (a) code identifiers (multi-word) — F4 TOKEN-HOME gated: identifier
    #     initials only carry meaning inside the token's own module (whole
    #     path segment or basename word); elsewhere they are coincidences
    #     (PlatformActorMetadata in audit-log-types.ts is not PAM).
    if _token_home(rel, token):
        for lineno, line in enumerate(code.splitlines(), 1):
            for m in _IDENT_RE.finditer(line):
                ident = m.group(0)
                if len(_word_tokens(ident)) >= 2:
                    mm = _match_from_words(
                        ident, token, "identifier", f"{rel}:{lineno}",
                        brand=False)
                    if mm is not None:
                        out.append(mm)
    # (b) F2 — plain string literals / JSX text participate ONLY via the
    #     explicit author gloss ``"Full Phrase (ABBR)"``. Their word initials
    #     are NEVER matched (an error message "Missing code parameter" is not
    #     MCP's meaning; a toast "Successfully ssh host…" is not SSH's).
    out.extend(_gloss_matches(code, token, "i18n-key", rel))
    return out


def _manifest_strings(root: Path, pkg_dir: str) -> list[tuple[str, str, str]]:
    """``(value, field, cite)`` AUTHORED manifest strings of a package dir
    (allowed manifest source — NOT locale values).

    ``config.json`` ``name`` is the app-store display convention — authored by
    definition (the B27 rung-1 precedent: it is read verbatim). A
    ``package.json`` ``name`` passes the B27 AUTHORED test
    (:func:`manifest_display._authored_slug_name`): a scope-stripped name equal
    to the package's own dir slug is the PATH again, not an authored word —
    ``@documenso/ee`` in ``packages/ee`` / ``@typebot.io/js`` establish
    NOTHING (neither vendor identity nor expansion evidence).
    ``displayName`` is display by definition; ``description`` strings are
    carried for gloss matching only."""
    dir_path = _resolve(root, pkg_dir)
    if dir_path is None or not dir_path.is_dir():
        return []
    out: list[tuple[str, str, str]] = []
    for base, authored_test in (("config.json", False),
                                ("package.json", True)):
        p = dir_path / base
        text = _read_text(p)
        if text is None:
            continue
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError):
            continue
        if not isinstance(doc, dict):
            continue
        cite = f"{pkg_dir}/{base}"
        raw_name = doc.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            if authored_test:
                bare = _authored_slug_name(raw_name, pkg_dir)
            else:
                bare = (raw_name.rpartition("/")[2].strip()
                        or raw_name.strip())
            if bare:
                out.append((bare, "name", cite))
        for field in ("displayName", "description"):
            val = doc.get(field)
            if isinstance(val, str) and val.strip():
                out.append((val.strip(), field, cite))
    return out


def _evidence_from_manifest(root: Path, pkg_dir: str, token: str) -> list[_Match]:
    out: list[_Match] = []
    for value, field, cite in _manifest_strings(root, pkg_dir):
        if field == "description":
            out.extend(_gloss_matches(value, token, "manifest", cite))
        else:
            mm = _match_from_words(value, token, "manifest", cite, brand=True)
            if mm is not None:
                out.append(mm)
    return out


@lru_cache(maxsize=64)
def _workspace_package_dirs(root_str: str) -> tuple[str, ...]:
    """Repo-relative workspace package dirs, resolved from ROOT CONFIG only
    (root ``package.json`` ``workspaces`` + ``pnpm-workspace.yaml``
    ``packages`` — allowed config sources; no repo-specific paths, no
    dictionaries). Terminal-star patterns (``packages/*``) get a BOUNDED
    one-level expansion; literal patterns are taken as-is; negations and
    complex globs are skipped. Best-effort: missing / malformed configs ⇒
    ``()``. Deterministic (sorted children)."""
    root = Path(root_str)
    patterns: list[str] = []
    text = _read_text(root / "package.json")
    if text is not None:
        try:
            doc: Any = json.loads(text)
        except (ValueError, RecursionError):
            doc = None
        if isinstance(doc, dict):
            ws = doc.get("workspaces")
            if isinstance(ws, dict):
                ws = ws.get("packages")
            if isinstance(ws, list):
                patterns.extend(str(p) for p in ws if isinstance(p, str))
    text = _read_text(root / "pnpm-workspace.yaml")
    if text is not None:
        try:
            pnpm = yaml.safe_load(text)
        except yaml.YAMLError:
            pnpm = None
        if isinstance(pnpm, dict) and isinstance(pnpm.get("packages"), list):
            patterns.extend(
                str(p) for p in pnpm["packages"] if isinstance(p, str))
    out: list[str] = []
    seen: set[str] = set()
    for pat in patterns:
        pat = pat.strip().strip("/")
        if not pat or pat.startswith("!"):
            continue  # negations exclude; they never add
        head, _, star = pat.rpartition("/")
        if star in ("*", "**"):
            base = _resolve(root, head) if head else root
            if base is None or not base.is_dir():
                continue
            try:
                children = sorted(
                    p.name for p in base.iterdir() if p.is_dir())
            except OSError:
                continue
            rels = [f"{head}/{c}" if head else c for c in children]
        elif "*" in pat:
            continue  # non-terminal / complex glob — out of the bounded law
        else:
            rels = [pat]
        for rel in rels:
            if rel not in seen:
                seen.add(rel)
                out.append(rel)
            if len(out) >= _MAX_WS_PACKAGE_DIRS:
                return tuple(out)
    return tuple(out)


def _same_name_workspace_dirs(root: Path, token: str) -> list[str]:
    """Workspace package dirs whose TERMINAL dir name IS the token
    (case-insensitive) — the package named after the abbreviation is the
    natural home of its declared meaning (cal.com ``packages/i18n`` for the
    route-anchored ``I18n`` tile)."""
    tok = (token or "").strip().lower()
    if not tok:
        return []
    return [d for d in _workspace_package_dirs(str(root))
            if d.rpartition("/")[2].lower() == tok]


def _evidence_from_routes(pf: Any, token: str) -> list[_Match]:
    """Full-form route/dir segments (spec §2d). Anchor-id path segments.
    Single glued segments still qualify via the numeric law
    (``internationalization`` → i18n) or brand-cased word structure
    (``wordpress`` → WordPress → wp); the token can never expand ITSELF
    (its own initials/contraction never reproduce it)."""
    out: list[_Match] = []
    anchor = str(getattr(pf, "anchor_id", None) or "")
    _, _, path = anchor.partition(":")
    for seg in path.split("/"):
        seg = seg.strip()
        if not seg or seg.lower() == (token or "").lower():
            continue
        mm = _match_from_words(seg, token, "route", seg, brand=False)
        if mm is not None:
            out.append(_Match(mm.norm, mm.display, "route", mm.prio))
    return out


def _member_paths(pf: Any) -> list[str]:
    """Repo-relative member file paths of a PF (member_files[].path first, the
    ``paths`` list as the legacy fallback — mirrors ``owned_paths_of``)."""
    seen: set[str] = set()
    out: list[str] = []
    for mf in (getattr(pf, "member_files", None) or []):
        p = mf.get("path") if isinstance(mf, dict) else getattr(mf, "path", None)
        if p and str(p) not in seen:
            seen.add(str(p))
            out.append(str(p))
    if not out:
        for p in (getattr(pf, "paths", None) or []):
            if p and str(p) not in seen:
                seen.add(str(p))
                out.append(str(p))
    return out


def _anchor_pkg_dir(pf: Any) -> str | None:
    return package_dir_of_anchor(str(getattr(pf, "anchor_id", None) or ""))


def _is_vendor_token(token: str, pf: Any, repo_root: Any) -> bool:
    """Vendor gate (§5): the lead token IS the anchor package's OWN declared
    name (a vendor product — Dub, Groq, Dify). Vendor identity wins; never an
    abbreviation candidate. Mechanism (the package's own manifest), no list.
    ``_manifest_strings`` already applied the B27 authored test, so a bare
    package slug (``@documenso/ee``) can never establish vendor identity;
    only NAME fields count (a description is prose, not an identity)."""
    root = _safe_root(repo_root)
    pkg_dir = _anchor_pkg_dir(pf)
    if root is None or not pkg_dir:
        return False
    tok = _norm(token)
    if not tok:
        return False
    for value, field, _cite in _manifest_strings(root, pkg_dir):
        if field in ("name", "displayName") and _norm(value) == tok:
            return True
    return False


def expand_abbreviation(
    token: str, pf: Any, repo_root: Any, vocab: Mapping[str, Any] | None = None,
) -> tuple[str | None, str]:
    """``(full_form, source)`` for an abbreviation token grounded in the repo.

    Searches the PF's member files (identifiers, i18n keys, JSX labels), the
    anchor package manifest, and route segments — first UNAMBIGUOUS match
    wins. ``> 1`` DISTINCT expansion ⇒ ``ambiguous`` (do not expand). No
    match ⇒ ``missing:expansion`` (honest debt). Bounded, never crashes."""
    del vocab  # brand casing is read directly; kept for signature symmetry
    root = _safe_root(repo_root)
    matches: list[_Match] = []
    pkg_dir: str | None = None
    if root is not None:
        for rel in sorted(_member_paths(pf))[:_MAX_MEMBER_FILES]:
            matches.extend(_evidence_from_file(root, rel, token))
        pkg_dir = _anchor_pkg_dir(pf)
        if pkg_dir:
            matches.extend(_evidence_from_manifest(root, pkg_dir, token))
        # Same-name workspace package (config-grounded): a route-anchored PF
        # named "I18n" finds packages/i18n via the root workspaces globs and
        # reads THAT package's manifest as an additional manifest source.
        for ws_dir in _same_name_workspace_dirs(root, token):
            if ws_dir != pkg_dir:
                matches.extend(_evidence_from_manifest(root, ws_dir, token))
    matches.extend(_evidence_from_routes(pf, token))

    best: dict[str, _Match] = {}
    for m in matches:
        cur = best.get(m.norm)
        if cur is None or m.prio < cur.prio:
            best[m.norm] = m
    if not best:
        return (None, "missing:expansion")
    if len(best) >= 2:
        return (None, "ambiguous")
    only = next(iter(sorted(best)))
    winner = best[only]
    # Brand render-polish: when the winning phrase IS a known brand modulo
    # spacing/casing ("Word Press" reconstructed from a WordPress identifier),
    # render the canonical brand form — corroboration YAML, display only.
    brand = _brand_casing().get(winner.norm)
    return (brand or winner.display, winner.cite)


def apply_fullname_expansion(
    display: str, pf: Any, repo_root: Any,
    vocab: Mapping[str, Any] | None = None,
) -> ExpansionResult:
    """The orchestrator the naming contract calls per PF display.

    Phrase-guard (skip all-word displays) → LEADING-token shape detector →
    vendor gate → evidence expansion. On success the lead is expanded and the
    qualifier tail is kept VERBATIM ("EDR Core" → "Endpoint Detection Response
    (EDR) Core"). Flag OFF ⇒ ``not-flagged`` (no work)."""
    if not pf_fullname_law_enabled():
        return ExpansionResult(None, "not-flagged")
    text = (display or "").strip()
    if not text:
        return ExpansionResult(None, "not-flagged")
    whitelist = load_fullname_whitelist()
    raw_tokens = text.split()
    letter_tokens = [t for t in raw_tokens if any(c.isalpha() for c in t)]
    # Phrase-guard: never flag a display whose every word is a plain word
    # ("No Show", "Feature Opt In", "Gen AI Visibility").
    if (len(letter_tokens) >= 2
            and all(_is_plain_word(t, whitelist) for t in letter_tokens)):
        return ExpansionResult(None, "not-flagged")
    lead = raw_tokens[0]
    tail = raw_tokens[1:]
    reason = is_abbreviation_shape(lead, whitelist)
    if reason is None:
        return ExpansionResult(None, "not-flagged")
    if _is_vendor_token(lead, pf, repo_root):
        return ExpansionResult(None, "vendor")
    full, src = expand_abbreviation(lead, pf, repo_root, vocab)
    if full:
        composed_lead = compose_display(full, lead)
        new_display = (
            " ".join([composed_lead, *tail]) if tail else composed_lead
        )
        return ExpansionResult(new_display, src, lead.lower(), composed_lead)
    if src == "missing:expansion":
        return ExpansionResult(None, "missing:expansion")
    return ExpansionResult(None, src)
