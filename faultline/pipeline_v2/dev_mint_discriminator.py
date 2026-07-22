"""B78-it2 Goal 2a — dev-standalone mint-vs-fold discriminator (D).

Probe canon (experimenter 2026-07-22, verdict=supports SHIP/medium): the
Seg A walk-evidence gate honestly rescues vacuum-folded api devs, but its
R2/R3 mint rungs also mint DEAD-ROUTER shapes — a backend router whose
frontend client is wired (bootstrap ``set*BaseUrl`` imports) yet consumed
by nobody, or consumed only by a FOREIGN domain's page. On the Soc0
exhibit board the +mints split 6 ways and every SINGLE deterministic
signal (plain import-grep / sibling-prefix / page-route existence /
UF-mass) mis-classifies at least one of them; only the disjunction

    D = scope-guard + (C1 stem-consumption OR C2 stem-UI) + fold-else

separates all six: {context-items, trial, suggestions} stay minted (each
via a DIFFERENT clause), {audit, network-mock} fold into the PF that owns
their real consumer, {audit-events} demotes (zero consumers anywhere).

The module is PURE repo analysis — no anchors, no Feature objects, no env
reads. The caller (stage 6.86, under ``FAULTLINE_FOLD_EVIDENCE_WEIGHT``)
supplies the dev's identity + owned files + route patterns + the tracked
file listing + a text reader, and maps the verdict onto anchors itself
(fold-target selection is anchor-business: probe risk 2 forbids board
membership as the owner ruler — dir-attribution via the anchor subtree
map is the caller's job).

Clause mechanics (all measured on the Soc0 repo, 2026-07-22):

SCOPE GUARD — D may only judge the dead-router shape, nothing else:
  ≤3 members, zero own frontend-UI members, every member a single file
  inside a SHARED directory (the dir holds tracked code that is NOT the
  dev's — ``backend/routers/`` hosting 20 sibling routers), and NO
  dedicated domain directory anywhere in a member path (``modules/
  <domain>/`` / ``features/<domain>/`` is structural author intent — the
  twenty/langfuse anti-case: every legit zero-UI PF there carries its
  own dir, so the guard keeps D at exactly 0 candidates on both boards).

C1 (stem-consumption) — resolve the router's frontend client by URL
  evidence, then ask whether any consumed DATA symbol carries the
  domain's compound stem. The URL literal in the client is usually NOT
  the full backend pattern (Soc0 splits ``'/api'`` base + ``'/audit/…'``
  path), so matching is by route segment-TAILS that keep at least half
  of the static prefix's segments (``/api/audit/logs`` → also
  ``/audit/logs``, never the generic ``/logs``). Symbols are the export
  enclosing the hit line plus a same-line ``name: (…) =>`` property (the
  shared ``client.ts`` wrapper hop: ``getSuggestions: () =>
  request('/suggestions')``). A symbol is CONSUMED only by a prod,
  non-test file that imports the client file AND references the symbol —
  minus BOOTSTRAP WIRING consumers, generalized as fan-out (a consumer
  importing more than half of the client's sibling family — Soc0
  ``App.tsx``/``AuthContext.tsx`` import 27 of the 34 ``api/`` clients
  to call ``set*BaseUrl``; a real domain consumer imports 1-2). Without
  the exclusion ``setAuditBaseUrl`` ("…audit…") would falsely C1-mint
  the dead audit router.

C2 (stem-UI) — ≥1 prod frontend UI file OUTSIDE api/ dirs whose BASENAME
  compound stem contains the domain stem (Soc0 trial: ``TrialBanner`` /
  ``TrialExpiredPage`` / ``TrialStatusContext`` — the domain's real
  surface reads Firestore, not the router, so C1 cannot see it). C1 runs
  FIRST (probe risk 4: polysemy — foreign ``*suggestion*`` UI files must
  never be the reason suggestions mints when its own consumption edge
  already proves it).

ELSE — consumers exist → FOLD into the consumer's owner (caller resolves
  via dir-attribution); zero consumers → DEMOTE (the dev is not a PF;
  the caller keeps the pre-gate disposition).

COMPOUND STEM LAW (probe risk 3): all name matching is by the FULL
  normalized compound stem (``network-mock`` → ``networkmock``) — single
  tokens are forbidden (token "mock" would false-mint network-mock via
  ``mockMode.ts``; token "context" hits 17 files).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Sequence

from faultline.analyzer.reverse_imports import _is_vendor_or_test

__all__ = [
    "DVerdict",
    "discriminate_dev_mint",
    "d_scope_guard",
]

#: Frontend UI component extensions (member-shape + C2 rulers).
_UI_EXTS: tuple[str, ...] = (".tsx", ".jsx", ".vue", ".svelte")

#: Frontend surface extensions C1 scans for URL literals + consumers.
_JS_EXTS: tuple[str, ...] = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
)

#: Path segments that mark an api-client dir (C2's "outside api/" rule).
_API_DIR_SEGMENTS: frozenset[str] = frozenset({"api", "apis"})

#: A URL tail hit must sit in string-literal context: the char right
#: before the match is a quote, a template backtick, or a closing brace
#: (the ```${base}/audit/logs`` template shape).
_LITERAL_PRECEDERS: frozenset[str] = frozenset({"'", '"', "`", "}"})

#: Bootstrap fan-out exclusion needs a FAMILY to measure against — with
#: fewer than 3 sibling clients "imports most of the family" is
#: meaningless (a single-client repo's only true consumer would always
#: trip the ratio). Structural floor, not a tuned threshold.
_WIRING_FAMILY_FLOOR = 3

_DECL_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_PROP_RE = re.compile(
    r"([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s+)?(?:function\b|\()",
)
_IMPORT_RE = re.compile(
    r"""(?:from\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|import\(\s*['"]([^'"]+)['"]\s*\))""",
)


@dataclass(frozen=True)
class DVerdict:
    """Outcome of one D evaluation.

    ``kind``: ``"out-of-scope"`` (guard failed — the caller's decision
    stands untouched), ``"mint"`` (C1/C2 confirmed — keep the mint),
    ``"fold"`` (consumers exist; ``consumer_files`` is the voting
    basis), or ``"demote"`` (zero consumers — not a PF).
    ``via``: honest clause stamp — ``"c1:<symbol>"`` / ``"c2:<file>"``
    for mints, ``None`` otherwise.
    """

    kind: str
    via: str | None = None
    consumer_files: tuple[str, ...] = field(default_factory=tuple)


def _norm(s: str) -> str:
    """Compound stem: lowercase alnum only (``network-mock`` →
    ``networkmock``). The ONLY name-matching grain D uses (risk 3)."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[-_\s]+", name.lower()) if t]


def _is_test_file(rel: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    if ".test." in base or ".spec." in base or ".stories." in base:
        return True
    return _is_vendor_or_test(rel)


def _dirname(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _file_stem(rel: str) -> str:
    base = rel.rsplit("/", 1)[-1]
    return base[: base.rfind(".")] if "." in base else base


# ── scope guard ──────────────────────────────────────────────────────────────


def d_scope_guard(
    name: str,
    owned: Sequence[str],
    tracked_files: Sequence[str],
    code_exts: Sequence[str],
) -> bool:
    """True when the dev is the dead-router shape D may judge (see the
    module docstring). Corroboration is mandatory: with no tracked
    listing the shared-dir condition cannot be verified and the guard
    honestly declines (existing walk-evidence fixtures stay untouched)."""
    owned_list = sorted(set(str(p) for p in owned))
    if not owned_list or len(owned_list) > 3:
        return False
    # Zero own frontend-UI members.
    if any(p.endswith(_UI_EXTS) and not _is_test_file(p) for p in owned_list):
        return False
    # No dedicated domain directory (structural author intent — the
    # twenty/langfuse legit zero-UI shape). Both rulers from the probe:
    # compound-stem containment in the normalized dir name, and the
    # name's first token equalling a raw dir segment.
    stem = _norm(name)
    toks = _tokens(name)
    for p in owned_list:
        segs = p.split("/")[:-1]
        for d in segs:
            if stem and stem in _norm(d):
                return False
            if toks and toks[0] == d.lower():
                return False
    # Every member is a single file in a SHARED dir: the dir must hold
    # ≥1 tracked code file that is NOT the dev's own.
    exts = tuple(code_exts) or _JS_EXTS
    dir_counts: Counter[str] = Counter()
    for rel in tracked_files:
        rel = str(rel).replace("\\", "/")
        if rel.endswith(exts):
            dir_counts[_dirname(rel)] += 1
    if not dir_counts:
        return False
    own_in_dir: Counter[str] = Counter(_dirname(p) for p in owned_list)
    for d, own_n in own_in_dir.items():
        if dir_counts.get(d, 0) - own_n < 1:
            return False
    return True


# ── C1 machinery ─────────────────────────────────────────────────────────────


def _static_prefix(pattern: str) -> str:
    """Longest static URL prefix of one route pattern (cut at the first
    param marker, keep whole segments only)."""
    cut = len(pattern)
    for marker in ("{", ":", "*", "("):
        idx = pattern.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    prefix = pattern[:cut]
    if cut < len(pattern) and "/" in prefix:
        # A partial segment survived the cut — drop it.
        prefix = prefix[: prefix.rfind("/")]
    return prefix.rstrip("/")

def _url_tails(patterns: Sequence[str]) -> list[str]:
    """Segment tails of every static prefix that keep ≥ half of its
    segments (scale-invariant: ``/api/audit/logs`` yields itself +
    ``/audit/logs`` but never the generic ``/logs``)."""
    tails: set[str] = set()
    for pat in patterns:
        prefix = _static_prefix(str(pat))
        segs = [s for s in prefix.split("/") if s]
        n = len(segs)
        if not n:
            continue
        min_keep = (n + 1) // 2
        for start in range(0, n - min_keep + 1):
            tails.add("/" + "/".join(segs[start:]))
    return sorted(tails, key=lambda t: (-len(t), t))


def _tail_hits(text: str, tails: Sequence[str]) -> list[int]:
    """Char offsets of URL-tail matches in string-literal context with a
    route boundary after the tail (``/api/audit`` never matches inside
    ``/api/audit-events``)."""
    hits: list[int] = []
    for tail in tails:
        for m in re.finditer(re.escape(tail) + r"(?![A-Za-z0-9-])", text):
            start = m.start()
            if start > 0 and text[start - 1] in _LITERAL_PRECEDERS:
                hits.append(start)
    return sorted(set(hits))


def _candidate_symbols(text: str, hit_offsets: Sequence[int]) -> list[str]:
    """Symbols that may carry one URL hit outward: the top-level
    declaration enclosing the hit (last decl at or above the hit line)
    plus a same-line ``name: (…) =>`` property (the wrapper-method hop)."""
    decls = [(m.start(), m.group(1)) for m in _DECL_RE.finditer(text)]
    out: set[str] = set()
    for off in hit_offsets:
        enclosing = None
        for pos, name in decls:
            if pos <= off:
                enclosing = name
            else:
                break
        if enclosing:
            out.add(enclosing)
        line_start = text.rfind("\n", 0, off) + 1
        line = text[line_start:off]
        props = _PROP_RE.findall(line)
        if props:
            out.add(props[-1])
    return sorted(out)


def _import_stems(text: str) -> frozenset[str]:
    """Last-segment stems of every static import/require specifier."""
    stems: set[str] = set()
    for m in _IMPORT_RE.finditer(text):
        spec = next((g for g in m.groups() if g), "")
        seg = spec.rstrip("/").split("/")[-1]
        if "." in seg:
            seg = seg[: seg.rfind(".")]
        if seg:
            stems.add(seg)
    return frozenset(stems)


def discriminate_dev_mint(
    name: str,
    owned: Sequence[str],
    route_patterns: Sequence[str],
    tracked_files: Sequence[str],
    read: Callable[[str], str | None],
    code_exts: Sequence[str] = (),
) -> DVerdict:
    """Run D for one would-be dev-standalone mint. See module docstring."""
    tracked = [str(p).replace("\\", "/") for p in tracked_files]
    if not d_scope_guard(name, owned, tracked, code_exts):
        return DVerdict(kind="out-of-scope")

    stem = _norm(name)
    owned_set = frozenset(str(p) for p in owned)
    js_prod = [
        rel for rel in sorted(set(tracked))
        if rel.endswith(_JS_EXTS)
        and rel not in owned_set
        and not _is_test_file(rel)
    ]

    # ── C1: resolve client files by URL-tail evidence ────────────────────
    tails = _url_tails(route_patterns)
    client_syms: dict[str, list[str]] = {}
    if tails:
        for rel in js_prod:
            text = read(rel)
            if not text:
                continue
            hits = _tail_hits(text, tails)
            if hits:
                syms = _candidate_symbols(text, hits)
                if syms:
                    client_syms[rel] = syms

    consumer_files: set[str] = set()
    c1_symbol: str | None = None
    import_cache: dict[str, frozenset[str]] = {}

    def _imports_of(rel: str) -> frozenset[str]:
        if rel not in import_cache:
            text = read(rel)
            import_cache[rel] = _import_stems(text) if text else frozenset()
        return import_cache[rel]

    for client, syms in sorted(client_syms.items()):
        client_stem = _file_stem(client)
        client_dir = _dirname(client)
        family = frozenset(
            _file_stem(rel) for rel in tracked
            if _dirname(rel) == client_dir
            and rel.endswith(_JS_EXTS)
            and not _is_test_file(rel)
        )
        sym_res = [re.compile(r"\b" + re.escape(s) + r"\b") for s in syms]
        for rel in js_prod:
            if rel == client:
                continue
            if client_stem not in _imports_of(rel):
                continue
            text = read(rel)
            if not text:
                continue
            hit_syms = [s for s, sre in zip(syms, sym_res) if sre.search(text)]
            if not hit_syms:
                continue
            # Bootstrap-wiring fan-out exclusion (generalized App.tsx /
            # AuthContext): the consumer imports MOST of the client's
            # sibling family — wiring, not domain consumption.
            if len(family) >= _WIRING_FAMILY_FLOOR:
                imported = len(family & _imports_of(rel))
                if imported * 2 > len(family):
                    continue
            consumer_files.add(rel)
            if c1_symbol is None:
                for s in hit_syms:
                    if stem and stem in _norm(s):
                        c1_symbol = s
                        break

    if c1_symbol is not None:
        return DVerdict(
            kind="mint", via=f"c1:{c1_symbol}",
            consumer_files=tuple(sorted(consumer_files)),
        )

    # ── C2: own-stem prod UI file outside api/ ───────────────────────────
    if stem:
        for rel in js_prod:
            if not rel.endswith(_UI_EXTS):
                continue
            if any(seg.lower() in _API_DIR_SEGMENTS
                   for seg in rel.split("/")[:-1]):
                continue
            if stem in _norm(_file_stem(rel)):
                return DVerdict(
                    kind="mint", via=f"c2:{rel}",
                    consumer_files=tuple(sorted(consumer_files)),
                )

    # ── else: fold to the consumer's owner / demote ──────────────────────
    if consumer_files:
        return DVerdict(
            kind="fold", consumer_files=tuple(sorted(consumer_files)),
        )
    return DVerdict(kind="demote")
