"""R5 phase-2 — member-evidence display derivation (``FAULTLINE_NAMING_WAVE_R5``).

Mechanism forms are CANONICAL from the 2026-07-19 experimenter probe
(``/private/tmp/r5probe-work/probe2.py`` + its ``out/{r53,r54,r55}.json``
verdicts) tuned by the delegated gate set:

  * R5-3 RENAME lane (split-core) — a glued display token is split into the
    repo's OWN word evidence, gated by: pure-alnum token, repo-wide
    symbol-singles == 0 (CONST-GUARD: ALL-CAPS constant identifiers never
    vote), run-vote >= 2, every split word >= 2 chars, camel-boundary
    alignment (a run matches only when its folded concatenation equals the
    token fold — no mid-camel cuts), and the SEPARATOR-RATIO rule against
    brand-camel splits: camel-only run evidence (zero separator-spelled
    forms such as ``blob-storage`` / ``page_layouts``) may split a token
    ONLY when the leading split word is a vocab verb (the ``signIn`` /
    ``signUp`` verb-particle glue class). ``Deepseek`` / ``Hitpay`` /
    ``Qrcode`` (camel-only, non-verb-led) stay intact — measured refutation
    2026-07-19: deepseek runs=6 vs glued=62, all camel.
  * ACRONYM-BY-SYMBOLS is REFUTED (probe: dpa={Dpa:46, dpa:21, DPA:2} —
    ALL-CAPS constants poison the vote). No acronym re-casing happens here;
    exact acronym/brand forms come only from the existing YAML corroboration
    (``polish_display_casing``) and the gated nav/manifest display channel.
  * R5-3 PLURAL rung — a non-write verb-led name whose LAST token is
    singular pluralizes only on >= 3x plural-vs-singular member evidence
    (ratio, not sing==0), with the ANCHOR-SOURCE segment excluded from the
    tally (the UF ``resource`` string is never seeded into local evidence —
    the anchor's own singular spelling must not veto the members' 15:1).
  * R5-3 CONF-DROP lane — an UNRESOLVED dir-token (glued, >= 6 chars, zero
    const-guarded symbol-singles, no plural-known singular, not a
    YAML-resolved brand/acronym) keeps its name but caps ``high`` ->
    ``medium`` and stamps ``shape:unresolved-dir-token``.
  * R5-5-ext BRAND-ECHO rung — a bare ``<verb> <pkg>`` journey whose
    post-verb remainder folds to a workspace package dir present in its OWN
    member paths AND whose package name carries a repo-slug token demotes
    ``high`` -> ``medium`` (ANY member count — the 1-2 member cap was
    refuted by probe r55).
  * R5-4 compose canonicalizer — ONE function over the three
    ``"<base> (<qual>)"`` compose sites: strip internal parens/brackets,
    collapse whitespace residue (``( List)``), fold double joints,
    optional-catch-all residue (``[[...space]]`` -> ``optional space``),
    and scaffold-verb inversion (``New (Webhooks)`` -> ``Webhooks — New``).

Everything here is a PURE mechanism over the row's / repo's own evidence
plus the packaged YAML vocabularies (corroboration only — no new word
lists). Flag gating lives at the call sites (``naming_contract`` /
``stage_6_86_anchored_mint``); with the wave OFF none of this module runs.
"""

from __future__ import annotations

import glob as _glob
import json
import os
import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping

from faultline.pipeline_v2.data import load_yaml

__all__ = [
    "MemberEvidence",
    "build_board_evidence",
    "r5_vocab_sets",
    "derive_split_display",
    "plural_rename",
    "unresolved_dir_token",
    "brand_echo_pkg",
    "canonicalize_compose",
]

#: camelCase / ALL-CAPS / lower word tokenizer (probe canon).
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
#: CONST-GUARD — an ALL-CAPS constant identifier never votes (probe
#: refutation: constants poison both case votes and split runs).
_ALLCAPS_CONST = re.compile(r"^[A-Z0-9_]+$")
_PURE_TOKEN = re.compile(r"[A-Za-z0-9_]+")
_ALNUM_ONLY = re.compile(r"[A-Za-z0-9]+")
#: Closed grammatical function-word class (probe canon) — connectors are
#: grammar, not domain vocabulary, and are never split or capped.
_CONNECTORS = frozenset(
    {"to", "and", "of", "for", "in", "on", "with", "via", "by",
     "the", "a", "an"})
#: Workspace package-dir prefix (probe r55 canon).
_PKG_DIR_RE = re.compile(r"(?:packages|apps|libs|crates)/([^/]+)/")
#: Optional catch-all route residue (probe r54 canon).
_OPT_CATCH = re.compile(r"\[\[\.\.\.([A-Za-z0-9_]+)\]\]")
#: Bounded manifest glob (probe canon) — workspace package manifests only.
_MANIFEST_PATTERNS = (
    "package.json", "*/package.json", "packages/*/package.json",
    "apps/*/package.json", "apps/*/*/package.json",
    "packages/*/*/package.json", "libs/*/package.json",
    "pyproject.toml", "*/pyproject.toml",
)


def _fold(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _ident_words(ident: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"[^A-Za-z0-9]+", ident or ""):
        out.extend(_WORD_RE.findall(part))
    return out


class MemberEvidence:
    """Word/run evidence pool over one row's members (or the whole board).

    Channels mirror the probe: SYMBOL words (const-guarded), PATH-segment
    words, and adjacent word RUNS (window 2..5) keyed by the folded
    concatenation. Separator-spelled runs (every word all-lowercase — a
    ``blob-storage`` path seg or ``blob_storage`` snake identifier) are
    distinguishable from camel runs by their word casing.
    """

    __slots__ = ("sym", "path", "runs", "run_src")

    def __init__(self) -> None:
        self.sym: dict[str, Counter[str]] = defaultdict(Counter)
        self.path: dict[str, Counter[str]] = defaultdict(Counter)
        self.runs: dict[str, Counter[tuple[str, ...]]] = defaultdict(Counter)
        self.run_src: dict[str, str] = {}

    def _add_runs(self, words: list[str], source: str) -> None:
        n = len(words)
        for i in range(n):
            for j in range(i + 2, min(i + 6, n + 1)):
                run = tuple(words[i:j])
                fr = _fold("".join(run))
                self.runs[fr][run] += 1
                self.run_src.setdefault(fr, source)

    def add_symbol(self, ident: str) -> None:
        if not ident or _ALLCAPS_CONST.match(ident):
            return  # CONST-GUARD: constants never vote
        words = _ident_words(ident)
        for w in words:
            f = _fold(w)
            if f:
                self.sym[f][w] += 1
        self._add_runs(words, "symbol")

    def add_path_seg(self, seg: str) -> None:
        words = _ident_words(seg)
        for w in words:
            f = _fold(w)
            if f:
                self.path[f][w] += 1
        if len(words) >= 2:
            self._add_runs(words, "path")

    def add_manifest(self, name: str) -> None:
        words = _ident_words(name)
        for w in words:
            f = _fold(w)
            if f:
                self.path[f][w] += 1
        self._add_runs(words, "manifest")

    def merge_from(self, other: "MemberEvidence") -> None:
        for f, wc in other.sym.items():
            self.sym[f].update(wc)
        for f, wc in other.path.items():
            self.path[f].update(wc)
        for f, rc in other.runs.items():
            self.runs[f].update(rc)
            self.run_src.setdefault(f, other.run_src.get(f, "evidence"))

    # ── read helpers ────────────────────────────────────────────────
    def sym_singles(self, f: str) -> int:
        return sum(self.sym.get(f, {}).values())

    def path_singles(self, f: str) -> int:
        return sum(self.path.get(f, {}).values())

    def local_total(self, f: str) -> int:
        return self.sym_singles(f) + self.path_singles(f)


def _sep_votes(runs: Mapping[tuple[str, ...], int]) -> int:
    """Separator-spelled votes: run forms whose EVERY word is lowercase —
    they can only come from an already-separated spelling (path seg with a
    ``-``/``_``, snake identifier, manifest name), never from glued camel."""
    return sum(n for run, n in runs.items() if all(w == w.lower() for w in run))


def _best_run(runs: Mapping[tuple[str, ...], int]) -> tuple[str, ...] | None:
    """Deterministic most-voted run form (count desc, then lexical)."""
    if not runs:
        return None
    return sorted(runs.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


# ── vocab sets (YAML corroboration — mechanisms over packaged data) ────

_VOCAB_SETS_CACHE: dict[str, Any] | None = None


def r5_vocab_sets(vocab: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Derived vocab sets for the R5 phase-2 lanes — a MECHANISM over the
    packaged YAML (never a new hardcoded list):

      * ``verbs`` — union of journey-template lead words, every
        ``flow_verb_classes`` member, every action-family verb, and the
        ``journey_verb_phrases`` lead words (this last brings ``sign`` in —
        the verb-led-glue rule's anchor).
      * ``scaffold`` — ``scaffold_lead_words`` (R5-4 inversion bases).
      * ``resolved`` — ``brand_casing`` keys + ``known_acronyms``: tokens
        the casing-polish law already resolves; never conf-dropped.
      * ``verb2fam`` — action-family reverse index (plural rung excludes
        create/delete leads).
    """
    global _VOCAB_SETS_CACHE
    if _VOCAB_SETS_CACHE is not None:
        return _VOCAB_SETS_CACHE
    v: Mapping[str, Any] = vocab if vocab is not None else (
        load_yaml("naming-contract-vocab.yaml"))
    try:
        fam = load_yaml("journey-action-families.yaml")
    except Exception:  # noqa: BLE001 — vocab optional; lanes degrade to no-op
        fam = {}
    verbs: set[str] = set()
    templates: Mapping[str, Any] = v.get("journey_templates") or {}
    for group in templates.values():
        for t in (group or {}).values():
            lead = str(t or "").split(None, 1)[0].strip().lower() if t else ""
            if lead:
                verbs.add(lead)
    for members in (v.get("flow_verb_classes") or {}).values():
        verbs.update(str(x).lower() for x in (members or []))
    verb2fam: dict[str, str] = {}
    for family in ("browse", "read", "create", "update", "delete", "act"):
        for x in (fam.get(family) or ()):
            verb2fam[str(x).lower()] = family
    verbs.update(verb2fam.keys())
    for phrase in (v.get("journey_verb_phrases") or ()):
        lead = str(phrase or "").split(None, 1)[0].strip().lower()
        if lead:
            verbs.add(lead)
    resolved = {str(k).lower() for k in (v.get("brand_casing") or {})}
    resolved.update(str(a).lower() for a in (v.get("known_acronyms") or ()))
    _VOCAB_SETS_CACHE = {
        "verbs": frozenset(verbs),
        "scaffold": frozenset(
            str(w).lower() for w in (v.get("scaffold_lead_words") or ())),
        "resolved": frozenset(resolved),
        "verb2fam": verb2fam,
    }
    return _VOCAB_SETS_CACHE


# ── evidence builder ───────────────────────────────────────────────────


def _flow_into(ev: MemberEvidence, fl: Any) -> None:
    for att in (getattr(fl, "flow_symbol_attributions", None) or ()):
        s = str(getattr(att, "symbol", "") or "")
        if s and s != "<file>":
            ev.add_symbol(s)
        p = str(getattr(att, "file", "") or "")
        if p:
            for seg in p.split("/"):
                ev.add_path_seg(os.path.splitext(seg)[0])
    for p in (getattr(fl, "paths", None) or ()):
        for seg in str(p).split("/"):
            ev.add_path_seg(os.path.splitext(seg)[0])


def _manifest_names(repo_root: Any) -> list[str]:
    root = str(repo_root or "")
    if not root or not os.path.isdir(root):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for pat in _MANIFEST_PATTERNS:
        for mp in sorted(_glob.glob(os.path.join(root, pat))):
            if "node_modules" in mp or mp in seen:
                continue
            seen.add(mp)
            try:
                if mp.endswith(".json"):
                    with open(mp, encoding="utf-8") as fh:
                        nm = str((json.load(fh) or {}).get("name") or "")
                else:
                    with open(mp, encoding="utf-8") as fh:
                        m = re.search(r'^name\s*=\s*"([^"]+)"', fh.read(), re.M)
                    nm = m.group(1) if m else ""
            except Exception:  # noqa: BLE001 — unreadable manifest: skip
                continue
            nm = nm.split("/")[-1]
            if nm and ("-" in nm or "_" in nm):
                out.append(nm)
    return out


def build_board_evidence(
    user_flows: Iterable[Any],
    product_features: Iterable[Any],
    flow_by_id: Mapping[str, Any],
    repo_root: Any,
) -> tuple[MemberEvidence, dict[str, MemberEvidence], dict[str, MemberEvidence]]:
    """``(repo_ev, uf_ev_by_id, pf_ev_by_slug)`` — probe-canon board pool.

    The UF ``resource`` string is deliberately NOT seeded (anchor-source
    exclusion — the plural rung's tally must not count the anchor's own
    singular spelling). PF path channels are capped at 400 entries per key
    (probe canon) so a mega-PF cannot dominate the pool."""
    repo_ev = MemberEvidence()
    uf_ev: dict[str, MemberEvidence] = {}
    for uf in user_flows:
        ev = MemberEvidence()
        for mid in (getattr(uf, "member_flow_ids", None) or ()):
            fl = flow_by_id.get(str(mid))
            if fl is not None:
                _flow_into(ev, fl)
        uf_ev[str(getattr(uf, "id", "") or "")] = ev
        repo_ev.merge_from(ev)
    pf_ev: dict[str, MemberEvidence] = {}
    for pf in product_features:
        ev = MemberEvidence()
        for att in (getattr(pf, "symbol_attributions", None) or ()):
            s = str(getattr(att, "symbol", "") or "")
            if s and s != "<file>":
                ev.add_symbol(s)
        for key in ("member_files", "paths"):
            entries = list(getattr(pf, key, None) or ())[:400]
            for entry in entries:
                p = str(getattr(entry, "path", None) or entry or "")
                for seg in p.split("/"):
                    ev.add_path_seg(os.path.splitext(seg)[0])
        pf_ev[str(getattr(pf, "name", "") or "")] = ev
        repo_ev.merge_from(ev)
    for nm in _manifest_names(repo_root):
        repo_ev.add_manifest(nm)
    return repo_ev, uf_ev, pf_ev


# ── R5-3 split core ────────────────────────────────────────────────────


def _split_token(
    part: str,
    ev_local: MemberEvidence,
    repo_ev: MemberEvidence,
    sets: Mapping[str, Any],
) -> tuple[list[str], str] | None:
    """Evidence-gated split of ONE glued word, or ``None``.

    Gates (delegation canon): len >= 6, repo sym-singles == 0
    (const-guarded), no plural-known singular, run-vote >= 2, every split
    word >= 2 chars, camel-boundary alignment (folded run == folded token),
    separator-ratio rule (separator-spelled votes >= 2 OR verb-led glue)."""
    f = _fold(part)
    if len(f) < 6:
        return None
    if repo_ev.sym_singles(f) > 0:
        return None
    if f.endswith("s") and repo_ev.sym_singles(f[:-1]) > 0:
        return None
    runs = repo_ev.runs.get(f)
    if not runs:
        return None
    total = sum(runs.values())
    if total < 2:
        return None
    # form preference: the row's own evidence wins when it has this fold
    local_runs = ev_local.runs.get(f)
    cand = _best_run(local_runs or runs)
    if (cand is None or len(cand) < 2 or any(len(x) < 2 for x in cand)
            or _fold("".join(cand)) != f):
        return None
    # separator-ratio rule: camel-only evidence splits nothing unless the
    # glue is verb-led (signIn / signUp class).
    if _sep_votes(runs) < 2 and _fold(cand[0]) not in sets["verbs"]:
        return None
    src = repo_ev.run_src.get(f, "evidence")
    return list(cand), src


def derive_split_display(
    name: str,
    ev_local: MemberEvidence,
    repo_ev: MemberEvidence,
    sets: Mapping[str, Any],
) -> tuple[str, list[str]]:
    """R5-3 rename lane over one display name (probe ``derive`` minus the
    refuted acronym rung). Returns ``(new_name, fired_sources)`` —
    ``new_name == name`` when nothing fired. Paren-bearing names are the
    R5-4 canonicalizer's jurisdiction, not this lane's."""
    if not name or "(" in name:
        return name, []
    out: list[str] = []
    sources: list[str] = []
    for i, tok in enumerate(name.split()):
        core = tok.rstrip(",.;:!?")
        suffix = tok[len(core):]
        if not core or not _PURE_TOKEN.fullmatch(core):
            out.append(tok)
            continue
        low = core.lower()
        if low in sets["verbs"] or low in _CONNECTORS:
            out.append(tok)
            continue
        if _ALLCAPS_CONST.match(core) and core != core.lower():
            # CONST-GUARD (display side): a displayed ALL-CAPS constant
            # identifier is never re-spelled ('CACHE_AUTO_PURGE' stays).
            out.append(tok)
            continue
        glued = core.islower() or (core[0].isupper() and core[1:].islower())
        parts = [p for p in core.split("_") if p] if "_" in core else [core]
        src_here: str | None = "snake" if "_" in core else None
        if src_here == "snake" and not all(
                p.islower() and len(p) >= 2 for p in parts):
            # The snake lane heals only the pure lower_snake dir-token
            # shape ('api_keys'); mixed-case or 1-char parts
            # ('DeleteComputed_B') would be mutilated, not healed.
            out.append(tok)
            continue
        words_acc: list[str] = []
        for p in parts:
            hit = _split_token(p, ev_local, repo_ev, sets) if glued else None
            if hit is not None:
                words_acc.extend(hit[0])
                src_here = f"split:{hit[1]}"
            else:
                words_acc.append(p)
        if src_here is None:
            out.append(tok)
            continue
        rendered_words = [
            w[0].upper() + w[1:].lower() if (i == 0 and k == 0) else w.lower()
            for k, w in enumerate(words_acc)
        ]
        rendered = " ".join(rendered_words)
        if rendered != core:
            out.append(rendered + suffix)
            sources.append(src_here)
        else:
            out.append(tok)
    return " ".join(out), sources


# ── R5-3 plural rung ───────────────────────────────────────────────────


def plural_rename(
    name: str,
    ev_local: MemberEvidence,
    sets: Mapping[str, Any],
) -> str | None:
    """Pluralized name, or ``None``. Fires only for a verb-led name whose
    lead is NOT a create/delete-family verb (a write verb legitimately takes
    a singular object) and whose member evidence backs the plural at >= 3x
    the singular with an absolute floor of 2 votes. The anchor-source
    segment never votes — ``build_board_evidence`` does not seed
    ``uf.resource`` (delegation: 'Manage account setting' -> settings 15:1
    once the anchor's own singular seed is excluded)."""
    toks = (name or "").split()
    if len(toks) < 2:
        return None
    lead = _fold(toks[0])
    if lead not in sets["verbs"]:
        return None
    if sets["verb2fam"].get(lead) in ("create", "delete"):
        return None
    # Common-noun phrase only (the census C1 shape is verb + lowercase
    # dir-token by construction): a Title-cased inner word marks a proper
    # noun phrase whose head is NOT the evidence's resource ('Configure
    # Deepseek Block' — the members' ``blocks/`` package segs must not
    # pluralize the product noun 'Block').
    if any(t != t.lower() for t in toks[1:]):
        return None
    # Grammar guards (closed function-word class): a singular indefinite
    # article anywhere is an explicit singular claim ('Register and log in
    # as a partner' never becomes 'a partners'), and a connector directly
    # before the head noun marks a prepositional object, not the resource
    # ('log in to account').
    folded_toks = [_fold(t) for t in toks]
    if "a" in folded_toks or "an" in folded_toks:
        return None
    if folded_toks[-2] in _CONNECTORS:
        return None
    m = re.match(r"^([A-Za-z0-9]+)([^A-Za-z0-9]*)$", toks[-1])
    if not m:
        return None
    w = m.group(1)
    if len(w) < 3 or w.lower().endswith("s"):
        return None
    fs = _fold(w)
    fp = fs + "s"
    sing = ev_local.local_total(fs)
    plur = ev_local.local_total(fp)
    if plur >= 2 and plur >= 3 * sing:
        return " ".join(toks[:-1] + [w + "s" + m.group(2)])
    return None


# ── R5-3 conf-drop lane ────────────────────────────────────────────────


def unresolved_dir_token(
    name: str,
    repo_ev: MemberEvidence,
    sets: Mapping[str, Any],
) -> str | None:
    """The display's UNRESOLVED dir-token, or ``None``.

    SHAPE-BOUNDED to the census dir-token classes (the R5 census is the
    lane's charter — free tokens inside authored prose names are never
    judged): either the ENTIRE display is one token (C7 / C8pf —
    ``Htmltopdf``, ``Bgtasks``) or the display is exactly ``<Verb>
    <lower-plural-token>`` (C1 — ``Manage evals`` shape).

    The bounded token is unresolved when it is pure-alnum, glued (lower or
    Titlecase), >= 4 chars folded, not a verb/connector, not resolved by
    the YAML casing vocab (brands/acronyms), actually LIVES in the repo's
    path segments (>= 2 — the DIR part of 'dir-token'; a prose word that
    names no directory/file is never capped), never appears as a
    standalone symbol word (const-guarded, repo-wide) and has no
    plural-known singular — and, because this runs AFTER the rename lane,
    could not be split from evidence either. The name is KEPT; only
    confidence drops (spec R5-3: an unresolved token keeps the name but
    loses confidence)."""
    toks = (name or "").split()
    cand: str | None = None
    if len(toks) == 1:
        cand = toks[0].rstrip(",.;:!?")
    elif (len(toks) == 2
          and _fold(toks[0]) in sets["verbs"]
          and toks[1] == toks[1].lower()
          and toks[1].rstrip(",.;:!?").endswith("s")):
        cand = toks[1].rstrip(",.;:!?")
    if not cand or not _ALNUM_ONLY.fullmatch(cand):
        return None
    low = cand.lower()
    if low in sets["verbs"] or low in _CONNECTORS or low in sets["resolved"]:
        return None
    if not (cand.islower() or (cand[0].isupper() and cand[1:].islower())):
        return None
    f = _fold(cand)
    if len(f) < 4:
        return None
    if repo_ev.path_singles(f) < 2:
        return None
    if repo_ev.sym_singles(f) > 0:
        return None
    if f.endswith("s") and repo_ev.sym_singles(f[:-1]) > 0:
        return None
    return cand


# ── R5-5-ext brand-echo rung ───────────────────────────────────────────


def brand_echo_pkg(
    name: str,
    member_paths: Iterable[str],
    repo_root: Any,
    sets: Mapping[str, Any],
) -> str | None:
    """The brand-echoing workspace package this bare journey name restates,
    or ``None``. Exact-template only: ``<verb> <pkg>`` where the ENTIRE
    post-verb remainder folds to a package dir present in the journey's OWN
    member paths AND the package name carries a repo-slug token (token
    equality, never substring — ``status-page`` on openstatus stays). ANY
    member count (the 1-2m cap was refuted by probe r55)."""
    if not name or "(" in name:
        return None
    toks = name.split()
    if len(toks) < 2:
        return None
    if _fold(toks[0]) not in sets["verbs"]:
        return None
    rest = _fold("".join(toks[1:]))
    if not rest:
        return None
    pkgs: dict[str, str] = {}
    for p in member_paths:
        m = _PKG_DIR_RE.match(str(p or ""))
        if m:
            pkgs.setdefault(_fold(m.group(1)), m.group(1))
    pkg = pkgs.get(rest)
    if not pkg:
        return None
    base = os.path.basename(os.path.normpath(str(repo_root or "")))
    slug_toks = {t for t in re.split(r"[^a-z0-9]+", base.lower()) if t}
    pkg_toks = {t for t in re.split(r"[^a-z0-9]+", pkg.lower()) if t}
    if slug_toks & pkg_toks:
        return pkg
    return None


# ── R5-4 compose canonicalizer (ONE function, three compose sites) ─────


def canonicalize_compose(
    name: str,
    sets: Mapping[str, Any],
) -> tuple[str, str | None]:
    """Canonical ``"<base> (<qual>)"`` form of a composed display, plus the
    rule that fired (``None`` when the input was already canonical).

    Rules (probe r54 canon — 7 heals / 109 identity / 0 anti-cases on the
    census-19 boards): ``residue-optional-catch-all`` (``[[...x]]`` ->
    ``optional x``), ``residue-drop`` (glyph-only qualifier dropped),
    ``orphan-qual-promote`` (base-less qualifier promoted), ``noun-leads-
    inversion`` (scaffold-verb base + non-verb qualifier -> the noun leads:
    ``New (Webhooks)`` -> ``Webhooks — New``), ``joint-restrip`` (internal
    parens/brackets stripped, whitespace residue collapsed, double joints
    folded)."""
    if not name or "(" not in name:
        return name, None
    i = name.find("(")
    base = name[:i].strip()
    qual = name[i:].strip()
    m = _OPT_CATCH.search(qual)
    if m:
        inner = "optional " + m.group(1)
        rest = re.sub(r"[()\[\]{}]", " ", _OPT_CATCH.sub(" ", qual))
        rest = re.sub(r"\s+", " ", rest).strip()
        inner = (rest + " " + inner).strip() if rest else inner
        rule: str | None = "residue-optional-catch-all"
    else:
        inner = re.sub(r"[()\[\]{}]", " ", qual)
        inner = re.sub(r"\s+", " ", inner).strip()
        rule = None
    if not re.search(r"[A-Za-z0-9]", inner):
        if base and base != name:
            return base, "residue-drop"
        return name, None
    if not base:
        return inner[0].upper() + inner[1:], "orphan-qual-promote"
    if (len(base.split()) == 1 and base.lower() in sets["scaffold"]
            and _fold(inner.split()[0]) not in sets["verbs"]):
        return f"{inner} — {base}", "noun-leads-inversion"
    canonical = f"{base} ({inner})"
    if canonical != name:
        return canonical, rule or "joint-restrip"
    return name, None
