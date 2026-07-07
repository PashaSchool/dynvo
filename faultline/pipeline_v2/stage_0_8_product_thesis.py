"""Stage 0.8 — Product Thesis (deterministic, NO LLM).

After the Stage 0.7 repo-class gate has decided the scan unit IS a
product app, this stage derives a one-object answer to "what is this
product?"::

    scan_meta.product_thesis = {
        "vertical":     "security-operations",
        "core_objects": ["alert", "detection", "case", ...],
        "audience":     "security & SOC teams",
        "sentence":     "Security operations platform around alerts, "
                        "detections and cases for security & SOC teams.",
        "evidence":     {...ranked verticals + signal counts...},
    }

Position & call site
====================

Numbered 0.8 because it is an intake-family judgement about the WHOLE
scan unit (like 0.6 shape / 0.7 repo-class), but its inputs are the
Stage-1 anchor outputs — so the runner invokes it right after the
extract phase, gated on the 0.7 verdict (``run.py``; the wiring is a
separate one-file commit). Non-product repo classes skip the stage
entirely and their outputs stay byte-identical (omit-when-absent).

Signals (ALL deterministic; consumed, never re-parsed)
======================================================

  1. **Schema domain nouns** — Stage-1 ``schema`` anchors
     (SchemaDomainExtractor already parsed prisma/drizzle/rails/django
     models into kebab slugs; we read ``AnchorCandidate.name``).
  2. **Dependency categories** — Stage-1 ``package`` anchors whose slug
     is a dep-anchor category (``billing``, ``email``, ... — the
     ``stage1_anchors`` vocabulary of ``dependency-anchors.yaml``).
  3. **Route vocabulary** — URL segments from the anchors' explicit
     ``routes`` tuples (decorator-routed stacks) plus
     ``route_path_for_file`` over anchor paths (filesystem-routed
     stacks; path-only, no IO).
  4. **Nav labels** — vendor-declared sidebar/nav labels via the
     existing ``product_strings`` collectors (``source == "nav"``
     entries only; route-derived taxonomy entries are excluded because
     channel 3 already owns route vocabulary).

Vertical decision
=================

``data/product-verticals.yaml`` (authoring copy ``eval/…``,
drift-guarded) maps noun FAMILIES + dep categories to verticals. Rules
are structural and scale-invariant: industry-standard domain nouns and
dependency families, never repo names, never per-repo thresholds.

  * a vertical is ELIGIBLE only with >= :data:`MIN_NOUN_FAMILIES`
    distinct noun families matched in code signals — one noun is an
    ingredient, two independent domain nouns are a thesis;
  * dep categories only CORROBORATE an eligible vertical (+1 each);
    they can never establish one (IS-vs-USES: importing posthog makes
    a repo an analytics *user*, not an analytics *product*);
  * ranking key = (score, noun-family count, channel confirmations);
    the top vertical must be STRICTLY greater than the runner-up —
    an exact tie, like no eligible vertical at all, falls back to
    ``generic-saas`` with the top core objects still listed.

HARD LAW — write-only stage
===========================

The thesis must NOT influence membership, attribution, flows, or any
other pipeline decision. This module only derives a value that the
runner writes under ``scan_meta.product_thesis``; no pipeline module
reads it back (grep-guard + import-allowlist tests in
``tests/pipeline_v2/test_stage_0_8_product_thesis.py``). A future wave
(W3 personas) may consume it — through an explicit reviewed seam, not
by quietly importing this module.

Kill-switch: ``FAULTLINE_PRODUCT_THESIS=0`` disables the stage (the
key is simply absent). Default ON — the stage is additive-only.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from faultline.pipeline_v2.data import load_yaml
from faultline.pipeline_v2.product_strings import (
    build_nav_taxonomy,
    collect_product_strings,
    route_path_for_file,
)
from faultline.pipeline_v2.stage_0_7_repo_class import REPO_CLASS_PRODUCT_APP

if TYPE_CHECKING:
    from faultline.pipeline_v2.extractors.base import AnchorCandidate
    from faultline.pipeline_v2.stage_0_intake import ScanContext
    from faultline.pipeline_v2.stage_0_7_repo_class import RepoClassVerdict

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────

#: The fallback vertical id (also declared in the YAML ``fallback``
#: block, which carries its display/audience strings).
GENERIC_VERTICAL = "generic-saas"

#: A vertical needs at least this many DISTINCT noun families matched
#: in code signals before it may win. Structural, not corpus-tuned:
#: one domain noun appears incidentally everywhere ("payment" in any
#: SaaS settings page); two independent families of the same domain
#: are the minimal signature of the product actually being ABOUT that
#: domain. Dep-anchor categories never count toward this minimum.
MIN_NOUN_FAMILIES: int = 2

#: How many core objects the thesis carries (the sentence uses the
#: top 3). Small on purpose — this is a thesis, not an inventory.
MAX_CORE_OBJECTS: int = 5

#: Kill-switch (verdict-style, mirrors ``FAULTLINE_REPO_CLASS_GATE``).
#: Default ON; ``FAULTLINE_PRODUCT_THESIS=0`` disables the stage.
THESIS_ENV = "FAULTLINE_PRODUCT_THESIS"

_LEXICON_FILENAME = "product-verticals.yaml"

#: URL version-prefix segments (``/v1/…``) — structural REST
#: convention, dropped from route vocabulary before matching.
_RE_VERSION_SEGMENT = re.compile(r"^v\d+$")

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")

#: Leading characters that mark a DYNAMIC route segment across stacks:
#: ``[id]`` (Next), ``{id}`` (FastAPI/OpenAPI), ``:id`` (Express/Rails),
#: ``$id`` (Remix), ``<int:id>`` (Django/Flask), ``*splat``, ``(group)``,
#: ``@slot`` / ``_private`` (Next organizational).
_DYNAMIC_SEGMENT_PREFIXES = ("[", "{", ":", "$", "<", "*", "(", "@", "_")


def thesis_enabled() -> bool:
    """True unless ``FAULTLINE_PRODUCT_THESIS=0`` (default ON)."""
    return os.environ.get(THESIS_ENV, "1").strip() != "0"


# ── Token normalization ─────────────────────────────────────────────────


def _singular(word: str) -> str:
    """Light singularisation — kept in sync with
    ``nav_taxonomy._singular`` / ``naming_validator._singular``.

    Never strips ``-us`` / ``-is`` / ``-ss`` (status, focus, analysis,
    address are already singular); only collapses ``-es`` to its stem
    when the stem is a sibilant (classes→class), so plain words keep
    their ``e`` (cases→case, not cas).
    """
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ss", "us", "is", "ous", "ius")):
        return word
    if word.endswith(("sses", "shes", "ches", "xes", "zzes")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def _token_parts(token: str) -> tuple[str, ...]:
    """Ordered content parts of one signal token.

    Camel/kebab/snake/space split, lowercase, singularized, pure-number
    and 1-char parts dropped. ORDER IS KEPT (unlike the naming
    validator's set-tokens) because multi-part noun families match
    contiguously (``event-type`` must not fire on ``…/type/…/event``).
    """
    spaced = _CAMEL_RE.sub(" ", token)
    parts: list[str] = []
    for raw in _SPLIT_RE.split(spaced):
        if not raw or raw.isdigit():
            continue
        t = _singular(raw.lower())
        if len(t) >= 2:
            parts.append(t)
    return tuple(parts)


def _canonical(parts: Sequence[str]) -> str:
    return "-".join(parts)


def _contains_contiguous(
    haystack: Sequence[str], needle: Sequence[str],
) -> bool:
    """True when ``needle`` appears as a contiguous run inside ``haystack``."""
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    needle_t = tuple(needle)
    return any(
        tuple(haystack[i:i + n]) == needle_t
        for i in range(len(haystack) - n + 1)
    )


def _pluralize(word: str) -> str:
    """Naive English plural for the thesis SENTENCE only (the
    ``core_objects`` list stays canonical-singular). Deterministic:
    compounds pluralize their last part (``api-key`` → ``api-keys``);
    words already ending in ``s`` are left alone (analysis, sms)."""
    last = word.rsplit("-", 1)[-1]
    if not last:
        return word
    if last.endswith("s"):
        plural = last
    elif last.endswith(("x", "z", "ch", "sh")):
        plural = last + "es"
    elif last.endswith("y") and len(last) > 1 and last[-2] not in "aeiou":
        plural = last[:-1] + "ies"
    else:
        plural = last + "s"
    return word[: len(word) - len(last)] + plural


# ── Lexicon (YAML-backed, house pattern) ────────────────────────────────


@dataclass(frozen=True, slots=True)
class VerticalRule:
    """One vertical's evidence vocabulary (parsed from the YAML)."""

    vertical_id: str
    display: str
    audience: str
    #: Each family is its normalized part tuple (``event-type`` →
    #: ``("event", "type")``).
    noun_families: tuple[tuple[str, ...], ...]
    dep_categories: tuple[str, ...]
    #: W3.1 D3 — optional integration-vendor category this vertical may
    #: draw catalog evidence from (``security`` for security-operations).
    vendor_category: str = ""


@dataclass(frozen=True, slots=True)
class ThesisLexicon:
    """The parsed ``product-verticals.yaml``."""

    rules: tuple[VerticalRule, ...]
    fallback_id: str
    fallback_display: str
    fallback_audience: str
    core_object_stopwords: frozenset[str]
    #: W3.1 D3 — plumbing noun families (canonical form): may neither
    #: establish a vertical nor score unless SCHEMA-matched.
    plumbing_families: frozenset[str] = frozenset()
    #: W3.1 D3 — category → vendor-token part tuples.
    vendor_categories: dict[str, tuple[tuple[str, ...], ...]] = field(
        default_factory=dict)
    #: Dir segments marking an integration-context path.
    integration_context_segments: frozenset[str] = frozenset()


@lru_cache(maxsize=1)
def load_thesis_lexicon() -> ThesisLexicon:
    """Parse the packaged lexicon once. Deterministic: rule order is
    the YAML authoring order (insertion order of the mapping)."""
    data = load_yaml(_LEXICON_FILENAME)
    verticals = data.get("verticals")
    rules: list[VerticalRule] = []
    if isinstance(verticals, dict):
        for vid, spec in verticals.items():
            if not isinstance(spec, dict):
                continue
            nouns = spec.get("nouns") or []
            families = tuple(
                parts
                for n in nouns
                if isinstance(n, str) and (parts := _token_parts(n))
            )
            deps = tuple(
                d for d in (spec.get("deps") or []) if isinstance(d, str)
            )
            rules.append(VerticalRule(
                vertical_id=str(vid),
                display=str(spec.get("display") or vid),
                audience=str(spec.get("audience") or "end users & teams"),
                noun_families=families,
                dep_categories=deps,
                vendor_category=str(spec.get("vendor_category") or ""),
            ))
    fallback = data.get("fallback") or {}
    stop_raw = data.get("core_object_stopwords") or []
    stopwords = frozenset(
        _canonical(_token_parts(s))
        for s in stop_raw
        if isinstance(s, str) and _token_parts(s)
    )
    plumbing = frozenset(
        _canonical(parts)
        for s in (data.get("plumbing_families") or [])
        if isinstance(s, str) and (parts := _token_parts(s))
    )
    vendor_cats: dict[str, tuple[tuple[str, ...], ...]] = {}
    for cat, tokens in (data.get("integration_vendor_categories") or {}).items():
        fams = tuple(
            parts for t in (tokens or [])
            if isinstance(t, str) and (parts := _token_parts(t))
        )
        if fams:
            vendor_cats[str(cat)] = fams
    ctx_segs = frozenset(
        str(s).lower()
        for s in (data.get("integration_context_segments") or [])
        if isinstance(s, str) and s
    )
    return ThesisLexicon(
        rules=tuple(rules),
        fallback_id=str(fallback.get("id") or GENERIC_VERTICAL),
        fallback_display=str(fallback.get("display") or "General SaaS"),
        fallback_audience=str(fallback.get("audience") or "end users & teams"),
        core_object_stopwords=stopwords,
        plumbing_families=plumbing,
        vendor_categories=vendor_cats,
        integration_context_segments=ctx_segs,
    )


@lru_cache(maxsize=1)
def _dep_category_slugs() -> frozenset[str]:
    """Every category slug of the ``stage1_anchors`` dep vocabulary
    (both ecosystems) — the values PackageAnchorExtractor emits as
    anchor names. Reused, not redeclared."""
    section = load_yaml("dependency-anchors.yaml").get("stage1_anchors") or {}
    slugs: set[str] = set()
    if isinstance(section, dict):
        for entries in section.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("slug"), str):
                    slugs.add(entry["slug"])
    return frozenset(slugs)


# ── Signals ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ThesisSignals:
    """Pure signal snapshot the derivation runs on.

    Duplicates inside ``route_segments`` / ``nav_labels`` are
    meaningful (occurrence counts rank core objects); ``schema_nouns``
    and ``dep_categories`` are naturally unique.
    """

    schema_nouns: tuple[str, ...] = ()
    route_segments: tuple[str, ...] = ()
    nav_labels: tuple[str, ...] = ()
    dep_categories: tuple[str, ...] = ()
    #: W3.1 D3 — sorted (category, vendor-canonical) pairs found in
    #: integration-context paths (dir segments + file stems).
    vendor_hits: tuple[tuple[str, str], ...] = ()

    @classmethod
    def collect(
        cls,
        ctx: "ScanContext",
        stage1_out: Mapping[str, Any],
    ) -> "ThesisSignals":
        """Build the snapshot from Stage-1 anchors (consume, don't
        re-parse). Defensive per channel: a failing channel degrades to
        "signal absent" and never kills the scan."""
        candidates: list["AnchorCandidate"] = []
        for source in sorted(stage1_out):
            if source.startswith("_"):  # the ``_errors`` sentinel
                continue
            value = stage1_out[source]
            if isinstance(value, list):
                candidates.extend(value)

        schema_nouns = tuple(sorted({
            c.name for c in candidates
            if getattr(c, "source", "") == "schema" and getattr(c, "name", "")
        }))

        all_paths = sorted({
            p for c in candidates for p in (getattr(c, "paths", None) or ())
        })

        segments: list[str] = []
        try:
            for c in candidates:
                for pattern, _method, _file in (getattr(c, "routes", None) or ()):
                    segments.extend(_url_segments(pattern))
            for p in all_paths:
                route = route_path_for_file(p)
                if route:
                    segments.extend(_url_segments(route))
        except Exception as exc:  # noqa: BLE001 — degrade, never fail the scan
            logger.warning("stage_0_8: route-vocabulary channel failed (%s)", exc)
        route_segments = tuple(sorted(segments))

        nav_labels: tuple[str, ...] = ()
        try:
            index = collect_product_strings(ctx.repo_path, all_paths)
            taxonomy = build_nav_taxonomy(index, all_paths)
            nav_labels = tuple(sorted(
                entry.label
                for top in taxonomy
                for entry in top.flatten()
                if entry.source == "nav"
            ))
        except Exception as exc:  # noqa: BLE001 — degrade, never fail the scan
            logger.warning("stage_0_8: nav-label channel failed (%s)", exc)

        dep_slugs = _dep_category_slugs()
        dep_categories = tuple(sorted({
            c.name for c in candidates
            if getattr(c, "source", "") == "package"
            and getattr(c, "name", "") in dep_slugs
        }))

        vendor_hits = _collect_vendor_hits(all_paths)

        return cls(
            schema_nouns=schema_nouns,
            route_segments=route_segments,
            nav_labels=nav_labels,
            dep_categories=dep_categories,
            vendor_hits=vendor_hits,
        )


def _url_segments(pattern: str) -> list[str]:
    """Concrete lowercase segments of one URL pattern; dynamic
    (``[id]`` / ``{id}`` / ``:id`` / ``$x`` / ``<int:x>`` / ``*``),
    organizational (``(group)`` / ``@slot`` / ``_private``) and
    version-prefix (``v1``) segments dropped."""
    out: list[str] = []
    for seg in pattern.split("/"):
        seg = seg.strip()
        if not seg or seg.startswith(_DYNAMIC_SEGMENT_PREFIXES):
            continue
        low = seg.lower()
        if _RE_VERSION_SEGMENT.match(low):
            continue
        if not any(ch.isalnum() for ch in low):
            continue
        out.append(low)
    return out


def _collect_vendor_hits(
    all_paths: list[str] | tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """W3.1 D3 — distinct (category, vendor) pairs in INTEGRATION-context
    paths. A path qualifies when any dir segment is an
    ``integration_context_segments`` word; vendor families then match
    contiguously against each remaining dir segment's / the file stem's
    token parts (same matcher as noun families)."""
    lex = load_thesis_lexicon()
    if not lex.vendor_categories or not lex.integration_context_segments:
        return ()
    hits: set[tuple[str, str]] = set()
    for p in all_paths:
        segs = str(p).split("/")
        dirs = segs[:-1]
        if not any(s.lower() in lex.integration_context_segments
                   for s in dirs):
            continue
        stem = segs[-1]
        stem = stem[: stem.rfind(".")] if "." in stem else stem
        units = [*dirs, stem]
        parts_list = [parts for u in units if (parts := _token_parts(u))]
        for cat, families in lex.vendor_categories.items():
            for family in families:
                canon = _canonical(family)
                if (cat, canon) in hits:
                    continue
                if any(_contains_contiguous(parts, family)
                       for parts in parts_list):
                    hits.add((cat, canon))
    return tuple(sorted(hits))


# ── Derivation ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VerticalEvidence:
    """One vertical's matched evidence (for the ranked list)."""

    vertical_id: str
    score: int
    noun_families: tuple[str, ...]
    dep_categories: tuple[str, ...]
    #: family canonical -> sorted channels it matched in
    channels: dict[str, tuple[str, ...]] = field(default_factory=dict)
    eligible: bool = False
    #: W3.1 D3 — plumbing families matched but SUPPRESSED (no schema
    #: confirmation) + the vendor-cluster contribution, for evidence
    #: transparency.
    plumbing_suppressed: tuple[str, ...] = ()
    vendor_cluster: dict[str, Any] = field(default_factory=dict)

    @property
    def rank_key(self) -> tuple[int, int, int]:
        confirmations = sum(len(chs) for chs in self.channels.values())
        return (self.score, len(self.noun_families), confirmations)

    def as_dict(self) -> dict[str, Any]:
        out = {
            "vertical": self.vertical_id,
            "score": self.score,
            "noun_families": list(self.noun_families),
            "dep_categories": list(self.dep_categories),
            "channels": {k: list(v) for k, v in sorted(self.channels.items())},
        }
        if self.plumbing_suppressed:
            out["plumbing_suppressed"] = list(self.plumbing_suppressed)
        if self.vendor_cluster:
            out["vendor_cluster"] = dict(self.vendor_cluster)
        return out


@dataclass(frozen=True, slots=True)
class ProductThesis:
    """The Stage 0.8 result for one product-app scan unit."""

    vertical: str
    display: str
    audience: str
    core_objects: tuple[str, ...]
    sentence: str
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return scan_meta_block(self)


def _channel_token_parts(
    signals: ThesisSignals,
) -> tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]:
    """``(channel, unique token-part tuples)`` for the noun channels.

    Uniqued per channel so 30 ``/alerts/…`` routes count as ONE route
    confirmation — matching stays scale-invariant in repo size.
    """
    per_channel: list[tuple[str, tuple[tuple[str, ...], ...]]] = []
    for channel, tokens in (
        ("schema", signals.schema_nouns),
        ("route", signals.route_segments),
        ("nav", signals.nav_labels),
    ):
        uniq = tuple(sorted({
            parts for t in tokens if (parts := _token_parts(t))
        }))
        per_channel.append((channel, uniq))
    return tuple(per_channel)


def _score_verticals(
    signals: ThesisSignals, lexicon: ThesisLexicon,
) -> list[VerticalEvidence]:
    """Evidence for every vertical with >=1 noun-family hit, best first.

    Deterministic: rank key desc, then vertical id asc (the id
    tie-break orders the REPORTED list only — an exact rank-key tie at
    the top is resolved to the generic fallback, never alphabetically).
    """
    channel_tokens = _channel_token_parts(signals)
    dep_present = set(signals.dep_categories)
    vendors_by_cat: dict[str, set[str]] = {}
    for cat, vendor in signals.vendor_hits:
        vendors_by_cat.setdefault(cat, set()).add(vendor)
    out: list[VerticalEvidence] = []
    for rule in lexicon.rules:
        family_channels: dict[str, set[str]] = {}
        for channel, tokens in channel_tokens:
            for parts in tokens:
                for family in rule.noun_families:
                    if _contains_contiguous(parts, family):
                        family_channels.setdefault(
                            _canonical(family), set(),
                        ).add(channel)
        # W3.1 D3 — plumbing families (api-key/secret/token/...) count
        # ONLY when SCHEMA-matched: a product whose own data model
        # declares the entity IS about it (infisical Secret models,
        # openstatus ApiKey); a /api-keys route is chrome every SaaS
        # has (the tracecat dev-tools=7 mislabel).
        suppressed = tuple(sorted(
            fam for fam, chs in family_channels.items()
            if fam in lexicon.plumbing_families and "schema" not in chs
        ))
        counted_channels = {
            fam: chs for fam, chs in family_channels.items()
            if fam not in suppressed
        }
        # W3.1 D3 — vendor-cluster family equivalents: every 3 DISTINCT
        # same-category vendors in integration-context paths = 1 noun
        # family of evidence (the spine hub-family >=3 constant). Never
        # establishes alone: >=1 real counted noun family required.
        cat = rule.vendor_category
        vendors = sorted(vendors_by_cat.get(cat, ())) if cat else []
        vendor_fe = len(vendors) // 3 if counted_channels else 0
        if not counted_channels and not family_channels:
            continue
        families = tuple(sorted(counted_channels))
        eligible = (
            len(families) >= 1
            and len(families) + vendor_fe >= MIN_NOUN_FAMILIES
        )
        dep_hits = (
            tuple(sorted(set(rule.dep_categories) & dep_present))
            if eligible else ()
        )
        if not families and not suppressed:
            continue
        vendor_cluster: dict[str, Any] = {}
        if vendor_fe:
            vendor_cluster = {
                "category": cat,
                "distinct_vendors": len(vendors),
                "family_equivalents": vendor_fe,
                "vendors": vendors[:12],
            }
        out.append(VerticalEvidence(
            vertical_id=rule.vertical_id,
            score=len(families) + vendor_fe + len(dep_hits),
            noun_families=families,
            dep_categories=dep_hits,
            channels={
                fam: tuple(sorted(chs))
                for fam, chs in counted_channels.items()
            },
            eligible=eligible,
            plumbing_suppressed=suppressed,
            vendor_cluster=vendor_cluster,
        ))
    out.sort(key=lambda ev: (
        -ev.rank_key[0], -ev.rank_key[1], -ev.rank_key[2], ev.vertical_id,
    ))
    return out


def _core_objects(
    signals: ThesisSignals, lexicon: ThesisLexicon,
) -> tuple[str, ...]:
    """Top domain nouns of the repo, vertical-independent.

    Rank: channels the noun appears in (desc), declared in the schema
    (desc — the schema is the strongest core-object declaration),
    occurrence count (desc), then alphabetical. Chrome/plumbing tokens
    (YAML stopword list) never qualify.
    """
    from faultline.pipeline_v2.naming_validator import VENDOR_TOKENS

    stats: dict[str, dict[str, Any]] = {}

    def _feed(channel: str, token: str) -> None:
        parts = _token_parts(token)
        if not parts:
            return
        canon = _canonical(parts)
        if canon in lexicon.core_object_stopwords:
            return
        if len(parts) == 1 and len(parts[0]) < 3:
            return  # 2-char fragments (``me``, ``ui``) are never a thesis noun
        if any(p in VENDOR_TOKENS for p in parts):
            # W2b.1 (e): a vendor/brand token names an INTEGRATION, never
            # the product's own core object (rallly "stripes" = Stripe
            # webhook routes; the dep-anchor channel already carries the
            # billing/email/... FAMILY corroboration for the vertical).
            return
        entry = stats.setdefault(
            canon, {"channels": set(), "count": 0},
        )
        entry["channels"].add(channel)
        entry["count"] += 1

    for noun in signals.schema_nouns:
        _feed("schema", noun)
    for seg in signals.route_segments:
        _feed("route", seg)
    for label in signals.nav_labels:
        _feed("nav", label)

    ranked = sorted(
        stats.items(),
        key=lambda kv: (
            -len(kv[1]["channels"]),
            -int("schema" in kv[1]["channels"]),
            -kv[1]["count"],
            kv[0],
        ),
    )
    return tuple(canon for canon, _ in ranked[:MAX_CORE_OBJECTS])


def _join_for_sentence(objects: Sequence[str]) -> str:
    plurals = [_pluralize(o) for o in objects]
    if len(plurals) == 1:
        return plurals[0]
    return ", ".join(plurals[:-1]) + " and " + plurals[-1]


def _sentence(display: str, audience: str, core_objects: Sequence[str]) -> str:
    """The deterministic thesis TEMPLATE — no LLM in this wave (a
    flag-gated polisher may come with W3 personas)."""
    if core_objects:
        joined = _join_for_sentence(core_objects[:3])
        return f"{display} platform around {joined} for {audience}."
    return f"{display} platform for {audience}."


def derive_product_thesis(
    signals: ThesisSignals,
    lexicon: ThesisLexicon | None = None,
) -> ProductThesis:
    """Derive the thesis from a signal snapshot. Pure + total: always
    returns (``generic-saas`` fallback), same inputs -> same output."""
    lex = lexicon if lexicon is not None else load_thesis_lexicon()
    ranked = _score_verticals(signals, lex)
    core = _core_objects(signals, lex)

    winner: VerticalEvidence | None = None
    tie = False
    if ranked and ranked[0].eligible:
        if len(ranked) >= 2 and ranked[0].rank_key == ranked[1].rank_key:
            tie = True  # ambiguous — fall back, report both in evidence
        else:
            winner = ranked[0]

    if winner is not None:
        rule = next(r for r in lex.rules if r.vertical_id == winner.vertical_id)
        vertical, display, audience = rule.vertical_id, rule.display, rule.audience
    else:
        vertical, display, audience = (
            lex.fallback_id, lex.fallback_display, lex.fallback_audience,
        )

    evidence: dict[str, Any] = {
        "signals": {
            "schema_nouns": len(signals.schema_nouns),
            "route_segments": len(set(signals.route_segments)),
            "nav_labels": len(set(signals.nav_labels)),
            "dep_categories": list(signals.dep_categories),
        },
        "ranked": [ev.as_dict() for ev in ranked[:3]],
        "tie": tie,
    }
    return ProductThesis(
        vertical=vertical,
        display=display,
        audience=audience,
        core_objects=core,
        sentence=_sentence(display, audience, core),
        evidence=evidence,
    )


# ── Gate + scan_meta / artifact projection (mirrors Stage 0.7) ──────────


def should_derive_thesis(verdict: "RepoClassVerdict | None") -> bool:
    """The ONE gate the runner consults: product-app verdicts only
    (any confidence — the fail-open residual is a product app too),
    with the kill-switch env ON. ``None`` (legacy callers that don't
    classify) never derives — omit-when-absent."""
    if verdict is None or not thesis_enabled():
        return False
    return verdict.repo_class == REPO_CLASS_PRODUCT_APP


def scan_meta_block(thesis: ProductThesis) -> dict[str, Any]:
    """The ``scan_meta['product_thesis']`` value (stable key order)."""
    return {
        "vertical": thesis.vertical,
        "core_objects": list(thesis.core_objects),
        "audience": thesis.audience,
        "sentence": thesis.sentence,
        "evidence": thesis.evidence,
    }


def write_product_thesis_artifact(
    ctx: "ScanContext",
    thesis: ProductThesis | None,
    *,
    skipped_reason: str | None = None,
) -> None:
    """Write ``06-stage-product_thesis.json`` when ``ctx.run_dir`` is
    set. Mirrors the 0.6/0.7 family. No-op in CLI mode."""
    run_dir = getattr(ctx, "run_dir", None)
    if run_dir is None:
        return
    try:
        from faultline.pipeline_v2.stage_7_output import write_stage_artifact
    except ImportError:
        return
    payload: dict[str, Any] = {
        "stage": "0.8-product-thesis",
        "run_id": getattr(ctx, "run_id", None),
        "enabled": thesis_enabled(),
        "skipped_reason": skipped_reason,
        "thesis": scan_meta_block(thesis) if thesis is not None else None,
    }
    try:
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="product_thesis",
            payload=payload,
            run_dir=run_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage_0_8_product_thesis: failed to write artifact: %s", exc)


def run_stage_0_8(
    ctx: "ScanContext",
    stage1_out: Mapping[str, Any],
    repo_class_verdict: "RepoClassVerdict | None",
) -> ProductThesis | None:
    """The one seam the runner calls (right after the extract phase).

    Gate -> collect -> derive -> artifact + stage log. Returns ``None``
    when the gate says skip (non-product repo class / kill-switch /
    legacy caller) or on any internal failure — the runner then simply
    omits ``scan_meta.product_thesis``. Never raises.
    """
    run_dir = getattr(ctx, "run_dir", None)
    try:
        from faultline.replay.capture import write_stage_input

        if run_dir is not None:
            write_stage_input(run_dir, 6, "product_thesis", {
                "ctx": ctx,
                "stage1_out": stage1_out,
                "repo_class_verdict": repo_class_verdict,
            })
    except Exception as exc:  # noqa: BLE001 — replay capture is best-effort
        logger.debug("stage_0_8: replay input capture failed (%s)", exc)

    try:
        if not should_derive_thesis(repo_class_verdict):
            reason = (
                "kill_switch" if not thesis_enabled()
                else "no_repo_class_verdict" if repo_class_verdict is None
                else f"repo_class:{repo_class_verdict.repo_class}"
            )
            write_product_thesis_artifact(ctx, None, skipped_reason=reason)
            _log_stage(run_dir, f"skipped ({reason})")
            return None

        signals = ThesisSignals.collect(ctx, stage1_out)
        thesis = derive_product_thesis(signals)
        write_product_thesis_artifact(ctx, thesis)
        _log_stage(
            run_dir,
            f"vertical={thesis.vertical} "
            f"core_objects={list(thesis.core_objects)} "
            f"tie={thesis.evidence.get('tie')} "
            f"signals={thesis.evidence.get('signals')}",
        )
        return thesis
    except Exception as exc:  # noqa: BLE001 — thesis must never fail a scan
        logger.warning("stage_0_8: product-thesis derivation failed (%s)", exc)
        return None


def _log_stage(run_dir: Any, message: str) -> None:
    """One StageLogger line (best-effort; plain logger without run_dir)."""
    if run_dir is None:
        logger.info("stage_0_8: %s", message)
        return
    try:
        from faultline.pipeline_v2.run_logger import StageLogger

        with StageLogger(run_dir, 6, "product_thesis") as log:
            log.info(message)
    except Exception:  # noqa: BLE001 — logging must never fail a scan
        logger.info("stage_0_8: %s", message)


__all__ = [
    "GENERIC_VERTICAL",
    "MAX_CORE_OBJECTS",
    "MIN_NOUN_FAMILIES",
    "THESIS_ENV",
    "ProductThesis",
    "ThesisLexicon",
    "ThesisSignals",
    "VerticalEvidence",
    "VerticalRule",
    "derive_product_thesis",
    "load_thesis_lexicon",
    "run_stage_0_8",
    "scan_meta_block",
    "should_derive_thesis",
    "thesis_enabled",
    "write_product_thesis_artifact",
]
