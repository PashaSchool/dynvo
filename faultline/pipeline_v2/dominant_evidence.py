"""B78 Seg H — dominant-evidence naming (``FAULTLINE_DOMINANT_EVIDENCE_NAMING``).

THE CLASS (composite-name smear, B78 census: 49.7% of eligible UFs carry a
name token supported by ≤25% of their members). The B70 member-evidence law
(``naming_contract.display_law_violations`` + ``_uf_member_evidence``) and
the refiner's anti-hallucination validator (``naming_validator``) are both
PRESENCE gates: ONE member grounding a token admits it into the journey's
display. A single audit-write rider inside an 11-member labels journey
gifts 'audit' to the title ('Create, manage, and audit labels' — Soc0
UF-040, 'audit' backed by 1/11 members) and a multi-resource PF display
gifts every one of its resources to a journey about one of them.

THE LAW (presence → RATIO): a CONTENT token may enter a composed UF display
only with member support ≥ :data:`MEMBER_SUPPORT_FLOOR` — the share of the
row's members whose own evidence (flow name + entry-file path) grounds the
token. Grammar is exempt (the naming machinery's own verb vocabulary +
stop-words are not evidence claims); the row's OWN resource is exempt (a
journey about auditing keeps 'audit' — the token is its identity, not a
gift). Members of a SIDE-EFFECT flow family (audit-write / telemetry /
cache / log — vocab corroboration, closed leading-token set per the
``journey_step_leaf_tokens`` discipline) never gift words: they are
excluded from every numerator while still diluting the denominator.

Compose sites armed by the flag (each drop-only, reject → the site's
existing deterministic re-derive channel):

  1. refiner accept-gate (``stage_6_7b_uf_refiner._compute_domain``) — an
     accepted LLM composite with an under-supported token is stripped
     token-wise (grammar-preserving); an unstrippable name is rejected to
     the deterministic Stage-6.7 name (the existing ``name_ok=False``
     channel).
  2. lattice ``journey_lattice._deterministic_name`` — the child's object
     phrase drops under-supported tokens before templating.
  3. own-resource / generic template (``naming_contract.build_uf_candidates``)
     — the ``{r}`` join phrase (PF display or own resource) drops
     under-supported tokens before templating.

Flag default OFF; unset ≡ explicit ``0`` (kill-switch forever). Registered
in ``scan_result_cache.ENV_OUTPUT_FLAGS`` (append-only, no KEY_SCHEMA
bump). OFF ⇒ every consumer site is byte-identical.

No LLM calls, no IO beyond the packaged vocab — pure functions, fully
unit-coverable on keyless scans.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Sequence

__all__ = [
    "DOMINANT_EVIDENCE_ENV",
    "MEMBER_SUPPORT_FLOOR",
    "dominant_evidence_naming_enabled",
    "member_evidence_pairs",
    "side_effect_verb_families",
    "is_side_effect_flow",
    "unsupported_display_tokens",
    "strip_display_tokens",
]

#: B78 Seg H flag — default OFF.
DOMINANT_EVIDENCE_ENV = "FAULTLINE_DOMINANT_EVIDENCE_NAMING"

#: Family support floor for a content token entering a composed display.
#: DERIVATION (reused ruler, not new tuning): this is the validator's I15
#: attach-overlap floor (eval/validate_scan.py) — the board's own "family
#: membership" measure, already reused verbatim at lattice ACTION-child
#: mint time (``journey_lattice._I15_ATTACH_FLOOR = 0.34``): a journey
#: whose files sit < 34% inside its PF scope is misattached by the board's
#: own ruler. Seg H applies the SAME scale-invariant ratio one grain down:
#: a token whose backing sits < 34% of the row's member mass is not a
#: family-level property of the journey and may not title it. Support is a
#: share of members (scale-invariant in member count); no per-repo number.
MEMBER_SUPPORT_FLOOR = 0.34

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?")

#: File extensions stripped from an entry path before tokenization
#: (mirrors the census ruler's entry-token line).
_ENTRY_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")

#: Coordination connectors (list grammar) recognized by the surgeon.
_CONN_WORDS = frozenset({"and", "or"})


def dominant_evidence_naming_enabled() -> bool:
    """Default **OFF** — ``FAULTLINE_DOMINANT_EVIDENCE_NAMING=1`` arms the
    ratio gate at the three compose sites. Unset ≡ explicit ``0`` ≡ any
    falsy spelling ⇒ every consumer is byte-identical (kill-switch
    forever)."""
    return os.environ.get(DOMINANT_EVIDENCE_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ── vocab corroboration (side-effect families) ──────────────────────────


def side_effect_verb_families(
    vocab: Mapping[str, Any],
) -> dict[str, frozenset[str]]:
    """The ``side_effect_flow_families`` vocab block (data —
    ``naming-contract-vocab.yaml``, NOT hardcoded Python): family name →
    closed set of leading verb tokens. Frozensets so every match is a
    membership test (the recorded set-iteration nondeterminism class stays
    out of this rail)."""
    block = vocab.get("side_effect_flow_families") or {}
    return {
        str(fam): frozenset(str(v).lower() for v in (verbs or ()))
        for fam, verbs in block.items()
    }


def is_side_effect_flow(
    flow_name: str, families: Mapping[str, frozenset[str]],
) -> bool:
    """``True`` when a member flow's LEADING name token names a
    cross-cutting side-effect action (journey_step_leaf_tokens discipline:
    closed set, exact leading-token match, no fuzzy/substring)."""
    toks = _WORD_RE.findall(str(flow_name or "").lower())
    if not toks:
        return False
    head = toks[0]
    for fam in sorted(families):
        if head in families[fam]:
            return True
    return False


# ── member evidence ─────────────────────────────────────────────────────


def member_evidence_pairs(
    members: Sequence[Any],
) -> list[tuple[str, str]]:
    """``(display-or-name, entry-file)`` per member flow object — the two
    evidence sources of the census smear ruler (flow name + entry path)."""
    out: list[tuple[str, str]] = []
    for m in members:
        nm = str(getattr(m, "display_name", None) or getattr(m, "name", "") or "")
        entry = getattr(m, "entry_point_file", None)
        if not entry:
            ep = getattr(m, "entry_point", None)
            if ep is not None:
                entry = (ep.get("path") if isinstance(ep, dict)
                         else getattr(ep, "path", None))
        out.append((nm, str(entry or "")))
    return out


def _member_tokens(name: str, entry: str) -> frozenset[str]:
    """Singular-folded evidence tokens of one member: flow name (the
    ``-flow`` suffix is machinery, kept — it never collides with content)
    + entry path segments (extension stripped)."""
    from faultline.pipeline_v2.naming_validator import (
        _singular,
        _split_tokens,
    )

    toks: set[str] = set()
    for t in _split_tokens(name or ""):
        toks.add(_singular(t))
    if entry:
        for t in _split_tokens(_ENTRY_EXT_RE.sub("", entry)):
            toks.add(_singular(t))
    return frozenset(toks)


def _resource_token_set(resource: str) -> frozenset[str]:
    from faultline.pipeline_v2.naming_validator import (
        _singular,
        _split_tokens,
    )

    return frozenset(_singular(t) for t in _split_tokens(resource or ""))


def unsupported_display_tokens(
    display: str,
    member_pairs: Sequence[tuple[str, str]],
    *,
    resource: str = "",
    vocab: Mapping[str, Any],
) -> list[str]:
    """Content tokens of ``display`` (singular-folded) whose member support
    falls below :data:`MEMBER_SUPPORT_FLOOR`. Sorted (determinism).

    * GRAMMAR EXEMPT — stop-words (``naming_validator.tokenize_name``
      drops them) and the vocab's own verb-class tokens
      (``naming_contract._verb_class_tokens`` — template leads +
      flow_verb_classes + action-family verbs) are machinery, not
      evidence claims.
    * RESOURCE EXEMPT — tokens grounded by the row's own ``resource``
      are its identity ('View audit logs' with resource='audit-log'
      keeps its words even when every member is side-effect-class).
    * SIDE-EFFECT MEMBERS never gift: excluded from every numerator,
      kept in the denominator (their mass dilutes weak tokens).
    * NO EVIDENCE ⇒ NO VERDICT — an empty ``member_pairs`` abstains
      (returns ``[]``): the ratio gate strips only on measured
      under-support, never on missing instrumentation.
    """
    from faultline.pipeline_v2.naming_contract import _verb_class_tokens
    from faultline.pipeline_v2.naming_validator import (
        _token_matches,
        tokenize_name,
    )

    if not member_pairs:
        return []
    content = tokenize_name(display)
    if not content:
        return []
    verb_toks = _verb_class_tokens(vocab)
    res_toks = _resource_token_set(resource)
    families = side_effect_verb_families(vocab)
    supporting: list[frozenset[str]] = [
        _member_tokens(nm, entry)
        for nm, entry in member_pairs
        if not is_side_effect_flow(nm, families)
    ]
    n = len(member_pairs)
    out: list[str] = []
    for tok in content:
        if tok in verb_toks:
            continue
        if res_toks and _token_matches(tok, set(res_toks)):
            continue
        support = sum(1 for mt in supporting if _token_matches(tok, set(mt)))
        if support / n < MEMBER_SUPPORT_FLOOR:
            out.append(tok)
    return sorted(out)


# ── grammar-preserving strip (coordination-chain surgeon) ───────────────


def _gap_connector(gap: str) -> str | None:
    """Classify the text BETWEEN two words: a list connector (``','``,
    ``'and'``, ``'or'``, ``'&'`` — possibly combined ``', and'``) returns
    its strongest joining word (``'&'`` > ``'and'``/``'or'`` > ``','``);
    a plain space returns ``None``."""
    g = gap.strip()
    if not g:
        return None
    low = g.lower()
    has_comma = "," in low
    word = low.replace(",", "").strip()
    if word in ("&",):
        return "&"
    if word in _CONN_WORDS:
        return word
    if word == "" and has_comma:
        return ","
    return None  # anything else (dash, paren...) — not a list gap


def strip_display_tokens(
    display: str,
    drop: Sequence[str],
    vocab: Mapping[str, Any] | None = None,
) -> str | None:
    """Drop-only, grammar-preserving removal of the ``drop`` tokens
    (singular-folded lower forms) from a composed display.

    The display is modeled as words joined by gaps; maximal runs of words
    joined by LIST connectors (``,`` / ``and`` / ``or`` / ``&``) form a
    coordination chain. Dropped words leave their chain; survivors re-join
    under English list grammar with the chain's own connector word
    (n=2 → ``A and B`` / ``A & B``; n≥3 → ``A, B and C``, Oxford comma
    preserved when the original carried one). Non-list gaps re-emit as-is.

    Returns the stripped display, or ``None`` when the surgery would leave
    no content word standing (caller must fall back to its deterministic
    re-derive channel — a bare-verb stump never ships). ``vocab`` sharpens
    the stump check with the naming machinery's own verb-class tokens
    ('Archive' alone is a stump even though 'archive' is no stop-word).
    """
    from faultline.pipeline_v2.naming_validator import (
        _singular,
        tokenize_name,
    )

    dropset = {str(d).lower() for d in drop}
    if not dropset:
        return display
    all_matches = list(_WORD_RE.finditer(display or ""))
    # Connector WORDS ('and' / 'or') are list grammar, not content — fold
    # them into the gaps between the remaining words.
    wm = [m for m in all_matches if m.group(0).lower() not in _CONN_WORDS]
    if not wm:
        return None

    words = [m.group(0) for m in wm]
    # gap text between consecutive content words (carries ',', 'and', '&')
    gaps: list[str] = []
    for i in range(len(wm) - 1):
        gaps.append(display[wm[i].end(): wm[i + 1].start()])
    conns = [_gap_connector(g) for g in gaps]

    keep = [
        _singular(w.lower()) not in dropset for w in words
    ]
    if all(keep):
        return display
    # ── chains: maximal runs joined by list gaps ──
    chain_of = [0] * len(words)
    cid = 0
    for i, conn in enumerate(conns):
        if conn is None:
            cid += 1
        chain_of[i + 1] = cid

    # per chain: word indices + strongest connector word + Oxford style
    chains: dict[int, list[int]] = {}
    for i, c in enumerate(chain_of):
        chains.setdefault(c, []).append(i)
    chain_conn: dict[int, str] = {}
    chain_oxford: dict[int, bool] = {}
    for c in sorted(chains):
        idxs = chains[c]
        conn_word = "and"
        oxford = False
        for i in idxs[:-1]:
            g = conns[i]
            if g == "&":
                conn_word = "&"
            elif g in _CONN_WORDS:
                if conn_word != "&":
                    conn_word = g
                if "," in gaps[i]:
                    oxford = True
        chain_conn[c] = conn_word
        chain_oxford[c] = oxford

    # ── re-emit ──
    out: list[str] = []
    emitted_any = False
    for c in sorted(chains):
        idxs = chains[c]
        survivors = [i for i in idxs if keep[i]]
        if not survivors:
            continue
        if emitted_any:
            # original inter-chain gap (a NON-list gap by construction);
            # anything beyond plain whitespace (a paren/dash that may now
            # dangle) normalizes to one space.
            joining = gaps[idxs[0] - 1] if idxs[0] > 0 else " "
            out.append(joining if joining.strip() == "" else " ")
        piece: list[str] = []
        for k, i in enumerate(survivors):
            if k > 0:
                if len(survivors) > 2 and k < len(survivors) - 1:
                    piece.append(", ")
                else:
                    joiner = chain_conn[c]
                    lead = ", " if (chain_oxford[c] and len(survivors) > 2) \
                        else " "
                    piece.append(f"{lead}{joiner} ")
            piece.append(words[i])
        out.append("".join(piece))
        emitted_any = True
    result = "".join(out).strip()
    if not result:
        return None
    # a stump with NO content word left (bare verbs/glue) never ships
    remaining = tokenize_name(result)
    if vocab is not None and remaining:
        from faultline.pipeline_v2.naming_contract import _verb_class_tokens

        verb_toks = _verb_class_tokens(vocab)
        remaining = [t for t in remaining if t not in verb_toks]
    if not remaining:
        return None
    # casing: a decapitated display keeps its sentence lead
    if display[:1].isupper() and result[:1].islower():
        result = result[:1].upper() + result[1:]
    return result
