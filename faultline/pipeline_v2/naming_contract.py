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
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from faultline.pipeline_v2.data import load_yaml
from faultline.pipeline_v2.fullname_expand import (
    apply_fullname_expansion,
    pf_fullname_law_enabled,
)
from faultline.pipeline_v2.manifest_display import (
    manifest_display_name,
    package_dir_of_anchor,
    pf_manifest_name_enabled,
    word_split_slug,
)

logger = logging.getLogger(__name__)

__all__ = [
    "NAMING_CONTRACT_ENV",
    "NAMING_PACK_ENV",
    "naming_pack_enabled",
    "HUMANIZE_ROUTE_NAMES_ENV",
    "PF_NAME_LAW_ENV",
    "UF_DEVGRAIN_NAME_ENV",
    "UF_NAME_DEGRIME_ENV",
    "UF_RESOURCE_RUNG_ENV",
    "UF_RUNG_SOURCES_V2_ENV",
    "UF_VERB_SNAP_ENV",
    "naming_contract_enabled",
    "humanize_route_names_enabled",
    "pf_name_law_enabled",
    "uf_devgrain_name_enabled",
    "uf_name_degrime_enabled",
    "uf_resource_rung_enabled",
    "uf_rung_sources_v2_enabled",
    "uf_verb_snap_enabled",
    "homing_hygiene_enabled",
    "naming_law_enabled",
    "load_naming_vocab",
    "polish_display_casing",
    "display_law_violations",
    "degrime_rename_plan",
    "build_pf_candidates",
    "build_uf_candidates",
    "hub_composition_display",
    "humanize_anchor_display",
    "nav_labels_for_pfs",
    "nav_label_sets_for_pfs",
    "gated_nav_labels_for_pfs",
    "route_verb_indexes",
    "member_verb_composition",
    "run_naming_contract",
    "rescore_uf_confidence",
]

NAMING_CONTRACT_ENV = "FAULTLINE_NAMING_CONTRACT"

#: B71 Seg A-C naming pack (default OFF). One flag gates the display-channel
#: laws (PF route-grammar + provenance, leaf-collision qualification, UF echo/
#: verb/family/uniqueness synth) and the degraded-scan confidence scoping.
NAMING_PACK_ENV = "FAULTLINE_NAMING_PACK"

#: Display-cross evidence gate (B71 provenance-ladder consumer; default OFF).
#: A foreign authored nav label — one whose tokens are absent from BOTH the
#: PF's own name/anchor identity AND its member-dominant path tokens (cal
#: ``insights`` display 'Bookings', ``organization`` -> 'directory_sync') is
#: reverted so the ladder falls through to the honest basename; the surviving
#: label wins an anchor-page tie-break before alpha and is title-cased. DISPLAY
#: channel only — feeds ``ProvenanceSources.nav``; the B40 nav-pinning rung and
#: the B57 nav-cluster rung read the ungated votes and are untouched.
PF_DISPLAY_EVIDENCE_GATE_ENV = "FAULTLINE_PF_DISPLAY_EVIDENCE_GATE"

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

#: B16 Part-1b — UF-level dev-grain suffix law kill-switch (default ON). ``=0``
#: restores UserFlow.name to the pre-Part-1b output byte-identically (independent
#: of the merged PF law so each has its own clean kill-switch).
UF_DEVGRAIN_NAME_ENV = "FAULTLINE_UF_DEVGRAIN_NAME"

#: B50 Seg1-2 — UF/PF display de-grime kill-switch (default **OFF**). At the
#: display JOINER it kills adjacent-token echoes ('Ingest ingest', 'View
#: views', 'case case ids', 'chat chatids' — Seg1) and glyph-less route-param
#: leaks ('teamurl documents', PF 'URL' — Seg2) in ``UserFlow.name`` /
#: ``Feature.display_name``. Display channel ONLY — identity / membership /
#: ``product_feature_id`` / paths / cluster keys / the ``resource`` field /
#: lineage are untouched. ``=1``/``true`` opts in; unset ⇒ display
#: byte-identical to pre-B50.
UF_NAME_DEGRIME_ENV = "FAULTLINE_UF_NAME_DEGRIME"

#: B50 Seg3 — earned resource-grounding rung kill-switch (default **OFF**).
#: A low UF carrying ``missing:resource`` (verb IS grounded) earns
#: resource-grounding ONLY from a REAL evidence rung (member-file domain
#: noun / param-free route segment / mapped test-file noun), each stamping a
#: distinct ``name_evidence`` entry ('resource:member-noun' …). The Law-C
#: rubric bar is UNCHANGED — this ADDS OR-sources to ``res_grounded``, never
#: lowers a threshold, never invents ``missing:verb`` grounding.
#: ``CONFIDENCE`` channel only. Unset ⇒ confidence + serialized output
#: byte-identical.
UF_RESOURCE_RUNG_ENV = "FAULTLINE_UF_RESOURCE_RUNG"

#: B57 Seg1 — deterministic rung-source expansion kill-switch (default
#: **OFF**). Four ADDITIONAL evidence sources for Law C's existing
#: resource/verb rungs, each a telemetered provenance-tagged OR-source
#: (the B50 Seg3 precedent — bar UNCHANGED, never a lowered threshold):
#:   (a) nav-cluster — ALL authored nav labels voted onto the UF's owning
#:       PF (not just the one top-voted label) may ground the resource;
#:   (b) i18n-key — i18n KEYS referenced in member SOURCE files
#:       (``t('billing_overview')`` / ``i18nKey=`` / ``getTranslation(``)
#:       ground the resource. KEYS ONLY: a key is identifier-shaped (no
#:       spaces); translated VALUES are a FORBIDDEN source (operator rule
#:       2026-07-13 — translations may live outside the repo; not
#:       structural truth), so anything space-broken is skipped and the
#:       ``product_strings`` VALUE channel is never consulted;
#:   (c) route-method — a member route's declared HTTP method grounds a
#:       matching verb-family lead (GET→browse/view, POST→create,
#:       PUT/PATCH→update, DELETE→delete — structural HTTP semantics,
#:       module-level frozen, not a repo vocabulary);
#:   (d) test-assert — assertion labels inside MAPPED member test files
#:       (``flow.test_files``, B36 member-overlap already holds) ground
#:       resource (non-verb token overlap) and/or verb (lead-verb family
#:       agreement).
#: CONFIDENCE/EVIDENCE channel only — UF NAMES are byte-stable either way
#: (the B40 law). Unset ⇒ confidence + serialized output byte-identical.
UF_RUNG_SOURCES_V2_ENV = "FAULTLINE_UF_RUNG_SOURCES_V2"

#: B61 Seg1 — evidence-born verb-snap kill-switch (default **OFF**). A
#: deterministic post-pass that REPLACES a UF display's LEADING verb when
#: its action-family is ABSENT from the member VERB-COMPOSITION (B57
#: :func:`member_verb_composition` — the HTTP-methods / page-kinds the
#: member flows structurally imply). The lead verb is snapped to the
#: canonical verb of the composition's DOMINANT family (mutation families
#: outrank read — a mutation is the stronger claim the code makes); the
#: resource remainder, membership, identity, and lineage are untouched.
#:
#: This is the DISPLAY channel — and, unlike every rung/adjudicator flag,
#: it is the FIRST feature permitted to CHANGE a UF NAME. It therefore
#: carries its OWN kill-switch so the B40 "UF NAMES byte-stable" law under
#: ``FAULTLINE_UF_RUNG_SOURCES_V2`` / ``FAULTLINE_STAGE_6_7E_ADJUDICATOR``
#: is preserved intact: those flags STILL never change a name; only THIS
#: flag legally extends the law to a name-changing, still-deterministic,
#: still-$0 post-pass. NAME_CONFIDENCE is never written here — Law C scores
#: the snapped name via its existing ``structural:verb-composition`` rung
#: (so an earned high requires ``FAULTLINE_UF_RUNG_SOURCES_V2`` co-armed,
#: as the keyed battery runs it). Iter2 rider (same flag): a GENERIC /
#: editorial lead with NO action family ('Manage …', 'Confirm …') is
#: verb-grounded by a NON-EMPTY member verb-composition — the exact mirror
#: of Law C's own generic clause (``lead is None and mfams >= 1``) with
#: route-structural facts as one more OR-source at the SAME bar (evidence
#: tag ``structural:verb-composition-generic``); such names are NEVER
#: snapped (no family conflict — nothing lies). SACRED: an EMPTY composition leaves the
#: name UNCHANGED (no facts → no claim → honest ``missing:verb``); a
#: mutation verb is assigned ONLY over a mutation composition (a GET-only
#: journey never earns a create/delete name); authored/pinned rows are
#: exempt; the snap is COLLISION-SAFE (B31 twin-protection — two rows
#: never snap to one name). Unset ⇒ names + confidence + serialized output
#: byte-identical.
UF_VERB_SNAP_ENV = "FAULTLINE_UF_VERB_SNAP"

#: B69-v2 — PF-homing hygiene family, FINAL composition (re-convoy
#: forensics ruling): the Stage 6.99b post-UF rehome rail (A′ + C′ rename +
#: home-tie guards) + the B31 pf-display echo-guard + the 6.7e Law-A
#: telemetry preservation. Default OFF; OFF ⇒ every consumer is skipped
#: and the serialized output is byte-identical to pre-B69-v2. SPLIT
#: rulings: the seed-birth pair lives under ``FAULTLINE_SEED_HYGIENE``
#: (route_group_recall); the bare-verb/dev-grain-token display law lives
#: under ``FAULTLINE_NAMING_LAW`` (banked — see its note below).
HOMING_HYGIENE_ENV = "FAULTLINE_HOMING_HYGIENE"

#: B69-v2 third split → B70 member-evidence redesign — the bare-verb /
#: dev-grain-token display law. The keyed re-convoy proved the ORIGINAL
#: vocabulary-driven implementation violated the mechanisms-over-
#: vocabularies law: verb-class token lists banned healthy product resources
#: ('Manage download', 'Browse webhook', 'Connect auth' — download /
#: webhook / auth / verify / register all live in verb classes) while
#: MISSING the true exhibit ('View mupdf' — mupdf is in no vocabulary);
#: each ban cascaded retry → new names → collisions → B31 parentheticals
#: (= the entire off-rail churn of the keyed pair). B70 redesign (landed in
#: :func:`display_law_violations`): the strip STOPS at a token a row member
#: grounds (``member_tokens``) — a grounded verb-homonym is the resource,
#: not the action — and a nominal remainder no member backs is flagged
#: ``evidence_absent`` ('View mupdf' caught by evidence ABSENCE; 'Manage
#: download' lives by its route evidence). No member context at a call site
#: ⇒ the banked vocabulary behavior, unchanged. Default OFF ⇒ the law list
#: is byte-identical.
NAMING_LAW_ENV = "FAULTLINE_NAMING_LAW"

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


def naming_pack_enabled() -> bool:
    """B71 Seg A-C naming pack. Default **ON** since the 2026-07-16 horizon-1
    flip (KEY_SCHEMA 30; keyed proof documenso + novu green — echo-fold in the
    rich boards, anti-cases live). Arms the display + confidence laws.
    ``FAULTLINE_NAMING_PACK=0`` keeps every naming channel byte-identical (the
    kill-switch law forever; unset ≡ explicit ``1``)."""
    return os.environ.get(NAMING_PACK_ENV, "1").strip().lower() in {"1", "true"}


def pf_display_evidence_gate_enabled() -> bool:
    """Display-cross evidence gate over the PF provenance ladder's nav
    channel (B71 consumer). Default **OFF** (new behavior): armed, an
    authored nav label only becomes a PF display when its tokens intersect
    the PF's name/anchor-terminal identity OR its member-dominant path
    tokens; equal-vote ties prefer the label whose href lands on the PF's
    own anchor page before alpha; the surviving label is title-cased.
    ``FAULTLINE_PF_DISPLAY_EVIDENCE_GATE=1``/``true`` arms; unset/``0`` keeps
    ``ProvenanceSources.nav`` — and therefore the emitted display — byte
    identical (the kill-switch law)."""
    return os.environ.get(
        PF_DISPLAY_EVIDENCE_GATE_ENV, "0"
    ).strip().lower() in {"1", "true"}


def uf_name_laws_enabled() -> bool:
    """UF name laws (B9): name-claim narrowing + UF-vs-UF display uniqueness +
    evidence-derived name_confidence, applied over FINAL members at emission.
    Default ON; ``FAULTLINE_UF_NAME_LAWS=0`` restores the base display names +
    name_confidence byte-identically."""
    return os.environ.get(UF_NAME_LAWS_ENV, "1").strip().lower() not in {
        "0", "false",
    }


#: B40 name-evidence rungs kill-switch (default ON — flipped 2026-07-12 after
#: the keyed proof on papermark + cal.com; KEY_SCHEMA v27). Arms the
#: provenance ladder (nav / registry / structural-route corroboration +
#: singular-folded multi-member agreement) in Law C + synth_quality and stamps
#: ``UserFlow.name_evidence``. ``=0`` restores the pre-B40 confidence rubric
#: AND serialized output byte-identically (only name_confidence/name_evidence
#: may ever differ under ON — UF NAMES are byte-stable either way).
NAME_EVIDENCE_RUNGS_ENV = "FAULTLINE_NAME_EVIDENCE_RUNGS"


def humanize_route_names_enabled() -> bool:
    """Default ON; ``FAULTLINE_HUMANIZE_ROUTE_NAMES=0`` restores the pre-B2
    anchor humanization (byte-identical route display names)."""
    return os.environ.get(HUMANIZE_ROUTE_NAMES_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def name_evidence_rungs_enabled() -> bool:
    """B40 provenance-graded confidence + ``name_evidence`` audit trail.
    Default ON (flipped 2026-07-12 after the keyed proof on papermark +
    cal.com; KEY_SCHEMA v27): unset arms the nav / registry / structural-route
    rungs (Law C) and the singular-folded member-agreement widening
    (synth_quality). ``FAULTLINE_NAME_EVIDENCE_RUNGS=0`` restores the pre-B40
    rubric + serialized output byte-identically (``name_evidence`` stays
    ``None`` everywhere)."""
    return os.environ.get(NAME_EVIDENCE_RUNGS_ENV, "1").strip().lower() not in {
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


def uf_devgrain_name_enabled() -> bool:
    """B16 Part-1b — strip a dev-grain suffix from ``UserFlow.name`` ("View
    detections page" -> "View detections") when the journey's home PF anchor
    is a route:*-page leak. Default ON; ``FAULTLINE_UF_DEVGRAIN_NAME=0``
    restores the pre-Part-1b UF names byte-identically."""
    return os.environ.get(UF_DEVGRAIN_NAME_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def uf_name_degrime_enabled() -> bool:
    """B50 Seg1-2 display de-grime. Default **ON** (flipped B62, KEY_SCHEMA
    29): the adjacent-echo discriminator + glyph-less deparam run at the
    display JOINER. ``FAULTLINE_UF_NAME_DEGRIME=0`` disables (byte-identical)."""
    return os.environ.get(UF_NAME_DEGRIME_ENV, "1").strip().lower() in {
        "1", "true",
    }


def uf_resource_rung_enabled() -> bool:
    """B50 Seg3 earned resource rung. Default **ON** (flipped B62,
    KEY_SCHEMA 29): the member-noun / route / test grounding rungs run in
    Law C (adds OR-sources to ``res_grounded``, bar unchanged).
    ``FAULTLINE_UF_RESOURCE_RUNG=0`` disables (confidence + serialized
    output byte-identical)."""
    return os.environ.get(UF_RESOURCE_RUNG_ENV, "1").strip().lower() in {
        "1", "true",
    }


def uf_rung_sources_v2_enabled() -> bool:
    """B57 Seg1 rung-source expansion. Default **ON** (flipped B62,
    KEY_SCHEMA 29): the nav-cluster / i18n-key / route-method / test-assert
    grounding rungs run in Law C (adds OR-sources to ``res_grounded`` /
    ``verb_grounded``, bar unchanged). ``FAULTLINE_UF_RUNG_SOURCES_V2=0``
    disables (confidence + serialized output byte-identical)."""
    return os.environ.get(UF_RUNG_SOURCES_V2_ENV, "1").strip().lower() in {
        "1", "true",
    }


def uf_verb_snap_enabled() -> bool:
    """B61 Seg1 evidence-born verb-snap. Default **ON** (flipped B62,
    KEY_SCHEMA 29): the deterministic lead-verb snap runs (replace a lying
    lead verb with the canonical verb of the member verb-composition's
    dominant family). DISPLAY channel; the ONLY UF-name-changing flag.
    ``FAULTLINE_UF_VERB_SNAP=0`` disables (names + confidence + serialized
    output byte-identical)."""
    return os.environ.get(UF_VERB_SNAP_ENV, "1").strip().lower() in {
        "1", "true",
    }


def homing_hygiene_enabled() -> bool:
    """B69-v2 — default **ON** since the 2026-07-16 horizon-1 flip
    (KEY_SCHEMA 30; keyed proof papermark + cal green — pm churn=1 fold,
    cal no-op). Arms the PF-homing hygiene family, FINAL composition
    (Stage 6.99b post-UF rehome rail + rename-on-rehome + the B31
    pf-display echo-guard + the 6.7e Law-A telemetry preservation).
    ``FAULTLINE_HOMING_HYGIENE=0`` ⇒ byte-identical pre-B69-v2 output
    (kill-switch forever; unset ≡ explicit ``1``). Split rulings: the
    seed-birth pair = ``FAULTLINE_SEED_HYGIENE`` (unflipped); the display
    law = ``FAULTLINE_NAMING_LAW``."""
    return os.environ.get(HOMING_HYGIENE_ENV, "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def naming_law_enabled() -> bool:
    """B70 member-evidence redesign — default **ON** since the 2026-07-16
    horizon-1 flip (KEY_SCHEMA 30; keyed proof papermark + cal green —
    law-attributed churn, zero bare names). Arms the bare-verb/dev-grain-
    token display law (member-evidence redesign; see :data:`NAMING_LAW_ENV`).
    ``FAULTLINE_NAMING_LAW=0`` ⇒ the law list is byte-identical to pre-B70
    (kill-switch forever; unset ≡ explicit ``1``). Independent of the HOMING
    and SEED flags."""
    return os.environ.get(NAMING_LAW_ENV, "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


# ── B50 display de-grime (Seg1 echo discriminator; Seg2 raw-param) ──────
#
# Structural, vocabulary-free token surgery applied at the display JOINER
# (never a post-filter over arbitrary finished strings): the same
# singular-fold + explicit-glued-suffix rule the B46 discriminator uses,
# lifted to work on already-split display tokens. Seg1 removes adjacent
# echoes; Seg2 (extended below) additionally reduces glyph-less route-param
# slugs to their noun core and drops standalone addressing identifiers.


def _degrime_sing(t: str) -> str:
    """Singular-fold one token (mirrors the B46 ``sing``): drop a trailing
    's' only when >3 chars remain, so 'views'->'view' but 'ids'->'ids'."""
    return t[:-1] if (t.endswith("s") and len(t) > 3) else t


#: Explicit glued tail-echo suffixes (B46 law: EXACT singular-folded
#: equality OR one of these glued suffixes — NEVER a partial char prefix,
#: so 'auth-authorize' is safe).
_DEGRIME_ECHO_SUFFIXES = ("id", "ids", "uuid", "s")


def _deglue_echo_tokens(tokens: list[str]) -> list[str]:
    """Drop an adjacent echo token: (a) a singular-folded duplicate of the
    previous token (core >=3 chars), or (b) the previous token glued with an
    explicit id/ids/uuid/s addressing suffix ('chat'+'ids'='chatids').

    NEVER strips a partial character prefix — 'auth authorize' /
    'auth authorizes' both survive (B46 SACRED anti-case)."""
    out: list[str] = []
    for t in tokens:
        if out:
            prev = out[-1]
            pl, tl = prev.lower(), t.lower()
            ps, ts = _degrime_sing(pl), _degrime_sing(tl)
            # (a) adjacent noun dup, singular-folded, core >= 3.
            if ts == ps and len(ps) >= 3:
                continue
            # (b) glued tail-echo: t == prev(+sing) + explicit suffix.
            if len(pl) >= 3 and any(
                tl == base + suf
                for base in (pl, ps)
                for suf in _DEGRIME_ECHO_SUFFIXES
            ):
                continue
        out.append(t)
    return out


#: Seg2 — FROZEN unambiguous identifier tokens (executor ruling, B46 lesson:
#: linguistic ≠ structural). Used for BOTH the glyph-less glued deparam
#: ('teamurl'→'team', 'boardid'→'board') and the standalone addressing drop
#: ('case id'→'case'). Deliberately NARROWER than the vocab's
#: ``route_addressing_suffixes`` (which the GLYPHED ``_param_noun`` path
#: keeps using unchanged): vocab suffixes like 'name'/'code'/'key'/'ref'/
#: 'hash'/'handle' are real word-endings — gluing on them is meaningful-word
#: loss ('username'→'user', 'barcode'→'bar').
_STANDALONE_ADDR = frozenset({
    "id", "ids", "url", "uuid", "guid", "slug", "pk",
})


def _drop_standalone_addr(words: list[str]) -> list[str]:
    """Seg2 — drop a standalone pure-addressing token ('id', 'ids', 'url', …)
    that FOLLOWS a non-addressing noun ('case id' -> 'case'); a leading or
    solitary addressing token is kept (it is handled by the pure-param
    display replacement, not silently deleted)."""
    out: list[str] = []
    for w in words:
        wl = w.lower()
        if out and wl in _STANDALONE_ADDR and out[-1].lower() not in _STANDALONE_ADDR:
            continue
        out.append(w)
    return out


def _degrime_words(words: list[str]) -> list[str]:
    """Casing-preserving DROP-only de-grime of a display-word list: Seg1
    adjacent-echo removal + Seg2 standalone addressing-token drop. No
    mutation (glyph-less deparam lives in :func:`_deparam_word`), so the
    result is always a subsequence of the input — span-mapping in
    :func:`_degrime_display` stays valid."""
    return _drop_standalone_addr(_deglue_echo_tokens(words))


_DEGRIME_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _degrime_display(text: str) -> str:
    """DROP-only de-grime over a finished display string, preserving every
    non-word separator (spaces, '&', '—', parens) of the surviving words.

    Used at the two producer seams that emit a joined string — the journey
    TEMPLATE ('Ingest ingest'->'Ingest') and the current-name echo
    discriminator. Only removes word tokens the de-grime rule marks as
    echoes; it never mutates a surviving token (mutation lives in the
    token-level renderer :func:`_resource_phrase`)."""
    matches = list(_DEGRIME_WORD_RE.finditer(text or ""))
    if len(matches) < 2:
        return text
    low = [m.group(0).lower() for m in matches]
    kept = _degrime_words(low)
    if kept == low:
        return text
    keptq = list(kept)
    drop_spans: list[tuple[int, int]] = []
    for m in matches:
        wl = m.group(0).lower()
        if keptq and wl == keptq[0]:
            keptq.pop(0)
        else:
            drop_spans.append((m.start(), m.end()))
    if not drop_spans:
        return text
    res: list[str] = []
    last = 0
    for s, e in drop_spans:
        res.append(text[last:s])
        last = e
    res.append(text[last:])
    out = "".join(res)
    out = re.sub(r"\(\s*\)", "", out)          # empty parens after a drop
    out = re.sub(r"\s+([)\]])", r"\1", out)     # space before a close bracket
    out = re.sub(r"\s{2,}", " ", out).strip(" -–—&")
    return out or text


def _qualifier_echoes_base(base: str, qual: str) -> bool:
    """True when EVERY singular-folded token of a candidate qualifier is
    already present (singular-folded) in the base display — the tautological
    '(Team)' on 'Teams', '(link)' on 'Manage links', '(Settings)' on 'Manage
    settings' class. A distinguishing qualifier ('(file)', '(API keys)')
    shares no folded token and is kept."""
    b = {_degrime_sing(t) for t in re.split(r"[^a-z0-9]+", (base or "").lower()) if t}
    q = [_degrime_sing(t) for t in re.split(r"[^a-z0-9]+", (qual or "").lower()) if t]
    return bool(q) and all(t in b for t in q)


def _deparam_word(word: str) -> str:
    """Seg2 — a glyph-less route-param slug ``<noun><addr-suffix>`` (the
    ``$teamUrl`` → 'teamurl', ``boardId`` → 'boardid' class that bypassed
    ``_param_noun`` because it carried no ``$``/``:`` glyph) reduces to its
    noun core: 'teamurl'->'team', 'boardid'->'board', 'chatids'->'chat'.

    Suffix source is the FROZEN :data:`_STANDALONE_ADDR` subset — NEVER the
    full vocab ``route_addressing_suffixes`` (executor ruling: 'username'/
    'filename'/'barcode' must survive; the glyphed ``_param_noun`` path keeps
    the full vocab set as today). A pure-addressing token ('url', 'id',
    'ids') is left as-is (the standalone-drop / pure-param replacement
    handle it). Requires a noun core of >=3 chars that is not itself
    pure-addressing, so nothing meaningful is truncated."""
    wl = word.lower()
    if wl in _STANDALONE_ADDR:
        return word
    best: str | None = None
    for suf in _STANDALONE_ADDR:
        for tail in (suf, suf + "s"):
            if wl.endswith(tail) and len(wl) > len(tail):
                core = wl[: -len(tail)]
                if len(core) >= 3 and core not in _STANDALONE_ADDR:
                    # Longest matching suffix wins (prefer 'ids' over 'id').
                    if best is None or (len(wl) - len(core)) > (len(wl) - len(best)):
                        best = core
    return best if best is not None else word


def _degrime_resource_words(
    words: list[str], vocab: Mapping[str, Any],
) -> list[str]:
    """Full de-grime of a rendered resource phrase token stream: Seg2
    glyph-less deparam (mutate ``<noun><addr>`` → core) composed with Seg1
    adjacent-echo removal + standalone addressing drop. 'teamurl document'
    -> 'team document'; 'case caseid' -> 'case case' -> 'case'.

    ``vocab`` is accepted for signature stability with the Seg1-committed
    mint-side caller (``_slot_consistent_label``); the deparam subset is
    FROZEN (executor ruling), so vocab is not consulted here."""
    del vocab  # frozen subset; see _deparam_word
    deparamed = [_deparam_word(w) for w in words]
    return _degrime_words(deparamed)


def _deparam_display(text: str) -> str:
    """Seg2 raw-param DISPLAY LAW over a finished display string: mutate each
    glyph-less param slug to its noun core, drop standalone addressing
    identifiers, collapse the resulting adjacent echoes — preserving every
    non-word separator ('&', '—', parens). 'teamurl documents' -> 'team
    documents'; 'boardid card cardids' -> 'board card'."""
    matches = list(_DEGRIME_WORD_RE.finditer(text or ""))
    if not matches:
        return text
    mutated = [_deparam_word(m.group(0)) for m in matches]
    keep = [True] * len(mutated)
    prev_low: str | None = None
    for i, w in enumerate(mutated):
        wl = w.lower()
        if prev_low is not None:
            # standalone addressing token after a noun → drop.
            if wl in _STANDALONE_ADDR and prev_low not in _STANDALONE_ADDR:
                keep[i] = False
                continue
            ps, ts = _degrime_sing(prev_low), _degrime_sing(wl)
            dup = ts == ps and len(ps) >= 3
            glued = len(prev_low) >= 3 and any(
                wl == base + suf
                for base in (prev_low, ps)
                for suf in _DEGRIME_ECHO_SUFFIXES
            )
            if dup or glued:
                keep[i] = False
                continue
        prev_low = wl
    if all(keep) and mutated == [m.group(0) for m in matches]:
        return text
    out: list[str] = []
    last = 0
    for i, m in enumerate(matches):
        sep = text[last:m.start()]
        if keep[i]:
            out.append(sep)
            out.append(mutated[i])
        elif sep.strip():
            out.append(sep)     # keep a punctuation separator ('&'), drop bare spaces
        last = m.end()
    out.append(text[last:])
    res = "".join(out)
    res = re.sub(r"\(\s*\)", "", res)
    res = re.sub(r"\s+([)\]])", r"\1", res)
    res = re.sub(r"\s{2,}", " ", res).strip(" -–—&")
    return res or text


def _is_pure_param_display(display: str) -> bool:
    """True when a display is nothing but addressing identifiers ('URL',
    'ID', 'Slug') — it names no domain object and must be replaced."""
    toks = [t for t in re.split(r"[^a-z0-9]+", (display or "").lower()) if t]
    return bool(toks) and all(t in _STANDALONE_ADDR for t in toks)


def _resolve_param_display(
    anchor_id: str,
    nav_label: str | None,
    paths: Iterable[str],
    vocab: Mapping[str, Any],
) -> str | None:
    """Seg2 — resolve a domain noun for a display that would be a pure param
    ('/p/$url' PF → 'URL'). Ladder: (a) the anchor segment WITHOUT the param,
    (b) the nav-cluster label, (c) the member-file dominant domain noun via
    :func:`domain_noun.extract_domain_noun`. NEVER the route letter 'p' /
    another pure param — each rung is rejected unless it names something."""
    # (a) anchor segment without the param.
    base, _qual = humanize_anchor_display(anchor_id, vocab)
    if (base and not _is_pure_param_display(base)
            and len(base.replace(" ", "")) > 1
            and not display_law_violations(base, vocab)):
        return base
    # (b) nav-cluster label.
    if nav_label:
        nl = polish_display_casing(nav_label, vocab)
        if (nl and not _is_pure_param_display(nl)
                and not display_law_violations(nl, vocab)):
            return nl
    # (c) member-file / member-component dominant domain noun.
    try:
        from faultline.pipeline_v2.domain_noun import extract_domain_noun
        dn = extract_domain_noun(list(paths), "")
    except Exception:  # noqa: BLE001 — optional structural resolver
        dn = None
    if dn and dn.label and not _is_pure_param_display(dn.label):
        cand = polish_display_casing(dn.label, vocab)
        if cand and not display_law_violations(cand, vocab):
            return cand
    return None


def degrime_rename_plan(
    current_names: Mapping[str, str],
    proposals: Mapping[str, str],
) -> set[str]:
    """B50 collision-safe rename plan (kan forensics; B16 precedent:
    "strip … collision-safe"). Compute targets FIRST over all rows, then
    apply only non-colliding renames.

    A proposed rename is REJECTED (row keeps its original name) when its
    case-folded target (a) already exists as ANOTHER row's current name —
    conservative: even when that row is itself being renamed away — or
    (b) is the target of TWO OR MORE proposals (skip BOTH; the kan
    'boardids'/'boardslugs' → 'Manage boards' ×2 class stays as honest
    distinct grime rather than a flag-created display collision).

    Pure function of the two maps ⇒ deterministic and independent of
    iteration / application / input order. Returns the uids whose rename
    may be applied."""
    cnt = Counter((n or "").strip().lower() for n in current_names.values())
    tcnt = Counter((v or "").strip().lower() for v in proposals.values())
    applied: set[str] = set()
    for uid in sorted(proposals):
        tgt = (proposals[uid] or "").strip().lower()
        cur = (current_names.get(uid) or "").strip().lower()
        exists_other = cnt.get(tgt, 0) - (1 if tgt == cur else 0) > 0
        if exists_other or tcnt[tgt] >= 2:
            continue
        applied.add(uid)
    return applied


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


def _has_route_template_residue(text: str, *, route_anchor: bool = True) -> bool:
    """True when a display still carries router-template machinery: a
    param glyph (``$ : { } [ ] < > *``), a word-adjacent ``+`` (Remix
    nesting), or a leading-underscore word (layout prefix).

    Horizon-1 ruling (2026-07-16, escalation #3): the trailing-``+`` rung
    fires ONLY for ``route:``-anchored displays — in route slugs ``+`` is
    Remix flat-route syntax, but on ``ws:``/``fdir:``/``hub:`` anchors it
    is a legitimate name character ('Enterprise+' lives; the older
    humanize law's named anti-case). Callers grading a NON-route anchor
    pass ``route_anchor=False``; the glyph and underscore rungs stay
    text-based for every anchor kind (those glyphs are never prose).
    Default True == the pre-ruling behaviour for text-only call sites."""
    t = text or ""
    if _PARAM_GLYPHS.search(t):
        return True
    if route_anchor and _TRAILING_PLUS_RE.search(t):
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


def _verb_class_tokens(vocab: Mapping[str, Any]) -> frozenset[str]:
    """Verb-class tokens derived from the vocab's OWN data — the journey
    templates' lead words, every ``flow_verb_classes`` member, and the
    action-family verbs (a mechanism over the curated YAML, never a new
    hardcoded list). Consumers: the B69-v2 bare-verb/dev-grain-token
    display law and the Stage 6.99b rename-on-rehome resource guard."""
    out: set[str] = set()
    templates: Mapping[str, Any] = vocab.get("journey_templates") or {}
    for group in templates.values():
        for t in (group or {}).values():
            lead = str(t or "").split(None, 1)[0].strip().lower() if t else ""
            if lead:
                out.add(lead)
    for verbs in (vocab.get("flow_verb_classes") or {}).values():
        out.update(str(v).lower() for v in (verbs or []))
    out.update(_action_family_index(vocab)["verb2fam"].keys())
    return frozenset(out)


def display_law_violations(
    text: str,
    vocab: Mapping[str, Any] | None = None,
    *,
    pf_display: str | None = None,
    member_tokens: frozenset[str] | None = None,
) -> list[str]:
    """Deterministic law check for one display name. Returns the list of
    violated law ids (empty == clean). ``pf_display`` (UF checks only)
    arms the ``pf_uf_twin`` law against the journey's own capability.

    ``member_tokens`` (B70) is the set of resource tokens a row's own
    members actually anchor (singular-folded). It arms the member-evidence
    branch of the banked ``FAULTLINE_NAMING_LAW`` display law: a
    verb-homonym resource that a member grounds ('download', 'webhook') is
    the THING, not the action, and a nominal remainder no member backs
    ('View mupdf') names nothing real. ``None`` (no member context at the
    call site) ⇒ the banked vocabulary behavior, byte-identical."""
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
    # B56-family codification, armed by FAULTLINE_NAMING_LAW: a display that
    # names an ACTION without a thing ('Manage', 'Browse & manage') or whose
    # only "thing" is a dev-grain transport token ('View API', 'Manage tRPC',
    # bare 'API') is not a journey name. Strip the LEADING verb-class tokens
    # (vocab-derived — template leads + flow_verb_classes + action-family
    # verbs) and connectors; an empty remainder is ``bare_verb``, a remainder
    # made ONLY of dev-grain segments is ``devgrain_token``.
    #
    # B70 member-evidence redesign (mechanisms-over-vocabularies): the pure
    # vocabulary mechanism false-positived on verb-homonym resources
    # ('Manage download', 'Browse webhook' — download/webhook/auth all live
    # in verb classes so the strip ate them → bare) and MISSED the true
    # exhibit ('View mupdf' — mupdf is in no vocabulary). When ``member_tokens``
    # is supplied (a row's own member evidence), the strip STOPS at a token a
    # member grounds — so a grounded verb-homonym is kept as the resource — and
    # a nominal remainder no member grounds is flagged ``evidence_absent``.
    # ``member_tokens is None`` (no member context) ⇒ the banked behavior,
    # byte-identical. Armed ONLY by the banked flag; OFF ⇒ the law list is
    # byte-identical.
    if naming_law_enabled() and t:
        lows = [w.lower() for w in _WORD_RE.findall(t)]
        verb_toks = _verb_class_tokens(v)

        def _grounded(w: str) -> bool:
            if member_tokens is None:
                return False
            return w in member_tokens or _degrime_sing(w) in member_tokens

        i = 0
        while i < len(lows) and (
                (lows[i] in verb_toks and not _grounded(lows[i]))
                or lows[i] == "and"):
            i += 1
        rest = lows[i:]
        if lows and i and not rest:
            out.append("bare_verb")
        elif rest and all(w in _API_SEGS for w in rest):
            out.append("devgrain_token")
        elif (member_tokens is not None and rest
                and not any(_grounded(w) for w in rest)):
            out.append("evidence_absent")
    if pf_display is not None and t and (
        t.strip().lower() == (pf_display or "").strip().lower()
    ):
        out.append("pf_uf_twin")
    return out


def _inherit_fullname(name: str, fullname_map: Mapping[str, str]) -> str:
    """B56 §5 — replace each PF abbreviation token in a UF name with its
    ``Full Name (ABBR)`` expansion (whole word, case-insensitive, once per
    abbreviation). Never rewrites an ``(ABBR)`` already inside parentheses, and
    skips an abbreviation whose expansion the name already carries — so a
    second application is a no-op. Deterministic (abbreviations sorted)."""
    text = name
    for abbr in sorted(fullname_map):
        value = fullname_map[abbr]
        if value in text:
            continue
        pat = re.compile(
            rf"(?<![\w(]){re.escape(abbr)}(?![\w)])", re.IGNORECASE)
        m = pat.search(text)
        if m:
            text = text[:m.start()] + value + text[m.end():]
    return text


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
    *,
    vendor_display: str | None = None,
) -> str | None:
    """"<Family> — <Vendor>" for a ``hub:<dir>/<vendor>`` anchor
    (Product-Spine §4.8 hub composition). ``None`` for non-hub anchors
    and hub CORE anchors (whose family display already carries "Core").

    ``vendor_display`` (B27) — the vendor word from the package's OWN
    declared metadata; when supplied it replaces the current-display
    vendor word so the family decoration keeps composing
    ("App Store — Stripe"). ``None`` (the default) is byte-identical to
    the pre-B27 composition."""
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
    vendor = vendor_display or polish_display_casing(current_display, vocab)
    if not family or not vendor:
        return None
    if vendor.strip().lower() == family.strip().lower():
        return None  # degenerate (vendor dir == family dir)
    return f"{family} — {vendor}"


def _package_manifest_display(
    anchor_id: str, vocab: Mapping[str, Any], repo_root: Any,
    current_display: str = "",
) -> tuple[str | None, str]:
    """B27 — ``(display, source)`` for a package-dir-anchored PF from the
    package's OWN declared metadata; ``(None, "")`` when the flag is off,
    no repo root is available, the anchor is not a package dir, or no
    rung yields a name.

    ``source`` is ``"manifest"`` (an authored metadata name — used
    verbatim, casing-polished) or ``"wordsplit"`` (the mechanical
    letter/digit split of the dir slug — strictly the rung below).

    A hub CORE (designed "<Family> Core" display — same convention
    :func:`build_uf_candidates` keys on) never takes a manifest name:
    the family dir's own metadata names the FAMILY package, not the
    core capability."""
    if repo_root is None or not pf_manifest_name_enabled():
        return None, ""
    src, _path = _anchor_path(anchor_id)
    if src == "hub" and " Core" in (current_display or ""):
        return None, ""
    pkg_dir = package_dir_of_anchor(anchor_id)
    if not pkg_dir:
        return None, ""
    authored = manifest_display_name(repo_root, pkg_dir)
    if authored:
        if authored == authored.lower() and " " not in authored:
            # A slug-cased authored name ("report-studio") is authored
            # NAMING, not authored CASING — titleize it like any path
            # word; an intentionally-cased name (WipeMyCal, Close.com)
            # ships verbatim (casing-polished).
            return _display_word(authored, vocab), "manifest"
        return polish_display_casing(authored, vocab), "manifest"
    split = word_split_slug(pkg_dir.rsplit("/", 1)[-1])
    if split:
        return _display_word(split, vocab), "wordsplit"
    return None, ""


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


def _strip_uf_devgrain_suffix(
    name: str, home_anchor: str, vocab: Mapping[str, Any],
) -> str | None:
    """B16 Part-1b — the dev-grain-suffix-stripped UF name, or ``None``.

    Mirrors the PF law for ``UserFlow.name``: strip the TRAILING dev-grain
    word W (page/screen/view/flow) from a journey name ("View detections
    page" -> "View detections") ONLY when the journey's HOME PF anchor's
    terminal route dir ends ``-W`` — i.e. the token is the same route-dir
    leak the PF carried. Guard (the "Publish page" anti-case): the strip
    must leave a verb+object phrase (>= 2 words), so a 2-word "Publish page"
    (whose home is a page-builder PF, not ``route:*-page``, and which would
    strip to a bare verb) is never mutilated — anchor-form AND arity gated."""
    terminal = _anchor_terminal_segment(home_anchor)
    if not terminal:
        return None
    tl = terminal.lower()
    tok = next(
        (w for w in sorted(_PF_DEVGRAIN_SUFFIX_TOKENS) if tl.endswith("-" + w)),
        None,
    )
    if tok is None:
        return None
    words = (name or "").split()
    if len(words) < 3 or words[-1].lower() != tok:
        return None   # need verb + object + devgrain (>=3) -> leaves >=2
    stripped = " ".join(words[:-1]).strip()
    if display_law_violations(stripped, vocab):
        return None
    polished = polish_display_casing(stripped, vocab)
    return polished if polished.strip().lower() != (name or "").strip().lower() else None


# ── Nav-label channel (authored labels; product_strings nav pairs) ──────


def _nav_label_votes(
    product_features: Iterable[Any],
    product_strings: Any,
    routes_index: Iterable[Mapping[str, Any]] | None,
) -> dict[str, dict[str, int]]:
    """``{pf_slug: {authored nav label: votes}}`` — a nav pair (label,
    href) votes for the PF that owns the route FILE its normalized href
    resolves to. Shared vote machinery for :func:`nav_labels_for_pfs`
    (one top label per PF — the B40 nav rung) and
    :func:`nav_label_sets_for_pfs` (ALL voted labels per PF — the B57
    nav-cluster rung). Empty on scans without a product-string index
    (keyless suppressed paths) — the channel is optional by
    construction."""
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
    return votes


def nav_labels_for_pfs(
    product_features: Iterable[Any],
    product_strings: Any,
    routes_index: Iterable[Mapping[str, Any]] | None,
) -> dict[str, str]:
    """``{pf_slug: authored nav label}`` — one deterministic label per
    PF: most votes → shortest → alpha (see :func:`_nav_label_votes`)."""
    votes = _nav_label_votes(product_features, product_strings, routes_index)
    out: dict[str, str] = {}
    for slug, labs in votes.items():
        best = sorted(
            labs.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]),
        )[0][0]
        out[slug] = best
    return out


def nav_label_sets_for_pfs(
    product_features: Iterable[Any],
    product_strings: Any,
    routes_index: Iterable[Mapping[str, Any]] | None,
) -> dict[str, list[str]]:
    """B57 Seg1 (a) — ``{pf_slug: ALL voted authored nav labels}``,
    sorted for determinism. The existing B40 nav rung consumes only the
    one top-voted label per PF (:func:`nav_labels_for_pfs`); the
    nav-cluster rung lets EVERY label the authors voted onto the PF
    ground a UF resource — same votes, same guards (shell filter, length
    bounds), wider read. New function beside the old one — the B40
    channel is untouched."""
    votes = _nav_label_votes(product_features, product_strings, routes_index)
    return {slug: sorted(labs) for slug, labs in sorted(votes.items())}


# ── Display-cross evidence gate (B71 provenance-ladder consumer) ────────
#
# A NEW consumer of the SAME ungated ``_nav_label_votes`` machinery — the
# B40 nav-pinning rung (:func:`nav_labels_for_pfs`) and the B57 nav-cluster
# rung (:func:`nav_label_sets_for_pfs`) are untouched. Only armed behind
# ``FAULTLINE_PF_DISPLAY_EVIDENCE_GATE`` (default OFF): the raw top-voted
# label can be a FOREIGN capability (a nav link whose href first-come-owns a
# borrowed route file — cal ``insights`` display 'Bookings'), which the
# provenance ladder then installs as the PF display. The gate keeps a label
# only on identity evidence, tie-breaks equal votes toward the PF's own
# anchor page, and title-cases the survivor.

_GATE_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_GATE_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def _gate_stem(token: str) -> str:
    """Light plural stem ('features'->'featur', 'apps'->'app') — the census
    predicate's stem, so the gate and the forensic agree token-for-token."""
    t = token.lower()
    if len(t) > 3 and t.endswith("es"):
        return t[:-2]
    if len(t) > 2 and t.endswith("s"):
        return t[:-1]
    return t


def _gate_tokens(text: str) -> set[str]:
    """Stemmed identity tokens of a string: camelCase + non-alnum split,
    len>=2, no pure-digit tokens. The evidence unit of the display-cross
    gate (matches the forensic census tokenizer)."""
    out: set[str] = set()
    for seg in _GATE_SPLIT.split(text or ""):
        for word in _GATE_CAMEL.sub(" ", seg).split():
            if len(word) >= 2 and not word.isdigit():
                out.add(_gate_stem(word))
    return out


def _member_dominant_tokens(
    paths: Iterable[str], ratio: float = 0.5,
) -> set[str]:
    """Path tokens present in >= ``ratio`` of the PF's member file paths —
    the member-majoritarian evidence that legitimizes an authored label
    even when it is absent from the slug (cal ``flags`` PF, label
    'features', member files under ``.../flags/`` and the feature-flag
    lib)."""
    files = [str(p) for p in (paths or [])]
    n = len(files)
    if n == 0:
        return set()
    counts: dict[str, int] = {}
    for f in files:
        for tok in _gate_tokens(f):
            counts[tok] = counts.get(tok, 0) + 1
    need = ratio * n
    return {tok for tok, k in counts.items() if k >= need}


def _nav_anchor_page_labels(
    product_features: Iterable[Any],
    product_strings: Any,
    routes_index: Iterable[Mapping[str, Any]] | None,
) -> dict[str, set[str]]:
    """``{pf_slug: {labels whose href lands on the PF's OWN anchor page}}``.

    The tie-break signal: for a ``route:<dir>`` anchored PF, the anchor
    INDEX page is ``<dir>/page.<ext>``; a nav pair whose normalized href
    resolves to that exact file is the PF's canonical self-link (cal
    ``insights``: 'Insights' -> ``.../insights/page.tsx``), which must beat
    a child-tab collision ('Bookings') that merely first-come-owns a
    sub-route file. Empty on keyless-suppressed / route-less scans."""
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

    # anchor index page file -> owning PF slug (first-come, list order)
    slug_by_anchor_page: dict[str, str] = {}
    for pf in product_features:
        slug = str(getattr(pf, "name", "") or "")
        src, path = _anchor_path(str(getattr(pf, "anchor_id", None) or ""))
        if src != "route" or not path:
            continue
        prefix = path.rstrip("/") + "/page."
        for f in file_by_pattern.values():
            if f.startswith(prefix) and "/" not in f[len(prefix):]:
                slug_by_anchor_page.setdefault(f, slug)
    if not slug_by_anchor_page:
        return {}

    out: dict[str, set[str]] = {}
    for pairs in pairs_by_file.values():
        for label, href in pairs:
            if not label or not href:
                continue
            lab = " ".join(str(label).split())
            norm = normalize_href(str(href)) or ""
            if not norm:
                continue
            route_file = file_by_pattern.get(norm)
            owner = slug_by_anchor_page.get(route_file) if route_file else None
            if owner:
                out.setdefault(owner, set()).add(lab)
    return out


def gated_nav_labels_for_pfs(
    product_features: Iterable[Any],
    product_strings: Any,
    routes_index: Iterable[Mapping[str, Any]] | None,
    vocab: Mapping[str, Any],
) -> dict[str, str]:
    """``{pf_slug: display-ready nav label}`` — the display-cross gate's
    replacement for :func:`nav_labels_for_pfs` on the gated provenance path
    ONLY (``FAULTLINE_PF_DISPLAY_EVIDENCE_GATE``). Three additive laws over
    the SAME ungated votes:

    * **tie-break** — equal-vote labels are ordered anchor-page-first
      before ``len``/alpha, so a PF's own self-link beats a child-tab href
      collision (cal ``insights``: 'Insights' over 'Bookings');
    * **gate** — the top label survives only when its tokens intersect the
      PF's name/anchor-terminal identity OR its member-dominant path tokens;
      a foreign label is dropped (omitted from the map) so the ladder falls
      through to the honest basename;
    * **casing** — the surviving raw authored label / i18n key is
      title-cased (``directory_sync`` -> 'Directory Sync'; ``features`` ->
      'Features').

    Omitting a slug (no surviving label) is the revert: the caller reads
    ``get(slug, "")`` and the ladder uses manifest/basename instead.
    """
    pfs = list(product_features)
    votes = _nav_label_votes(pfs, product_strings, routes_index)
    if not votes:
        return {}
    anchor_labels = _nav_anchor_page_labels(pfs, product_strings, routes_index)
    pf_by_slug = {str(getattr(p, "name", "") or ""): p for p in pfs}
    out: dict[str, str] = {}
    for slug, labs in votes.items():
        pf = pf_by_slug.get(slug)
        if pf is None:
            continue
        anchor_hits = anchor_labels.get(slug, set())
        # Anchor-page preference is inserted BEFORE alpha but AFTER the vote
        # plurality — it only ever changes the winner at a vote TIE, so a
        # label the authors clearly favored is never overridden.
        best = sorted(
            labs.items(),
            key=lambda kv: (-kv[1], 0 if kv[0] in anchor_hits else 1,
                            len(kv[0]), kv[0]),
        )[0][0]
        aid = str(getattr(pf, "anchor_id", None) or "")
        identity = _gate_tokens(str(getattr(pf, "name", "") or "")) | _gate_tokens(
            _anchor_terminal_segment(aid) or "")
        toks = _gate_tokens(best)
        if toks & identity or toks & _member_dominant_tokens(
            getattr(pf, "paths", None) or []
        ):
            out[slug] = _display_word(best, vocab)
    return out


# ── B57 Seg1 rung-source extractors (flag-gated; Law C consumers) ───────
#
# Pure text→evidence extractors for the FAULTLINE_UF_RUNG_SOURCES_V2
# rungs. Deterministic, $0, NO README, no LLM. File reads are bounded
# (size cap mirrors product_strings._MAX_FILE_BYTES; per-UF file-count
# cap) — the caps only bound work, they are not tuned to any repo.

#: i18n-KEY reference patterns in member SOURCE files: ``t('key')`` /
#: ``t("key")`` (Vue ``$t('key')`` included — the hoppscotch-class
#: canonical reference form), ``i18nKey="key"`` (JSX ``{'key'}`` form
#: included), and ``getTranslation('key')``. The KEY (group 1) is the
#: evidence — the identifier the author wrote in CODE. The lookbehind on
#: ``$?t(`` keeps word-tails out (``format(`` / ``at(`` / ``foo$t(``
#: never match; ``i18n.t(`` / ``this.$t(`` / ``{{ $t( }}`` do).
_I18N_KEY_REFS = (
    re.compile(r"(?<![\w$])\$?t\(\s*['\"]([^'\"\n]+)['\"]"),
    re.compile(r"i18nKey\s*=\s*\{?\s*['\"]([^'\"\n]+)['\"]"),
    re.compile(r"getTranslation\(\s*['\"]([^'\"\n]+)['\"]"),
)

#: Structural i18n-KEY discriminator: identifier-shaped — word chars plus
#: the namespace glyphs ``. : $ -`` and NO whitespace. Anything
#: space-broken is human copy (a translated VALUE — the FORBIDDEN
#: source), never a key.
_I18N_IDENT_RE = re.compile(r"[\w.:$\-]+\Z")

#: camelCase boundary for key tokenization (``profileTitle`` → profile
#: title); composes with the ``[._:$-]`` namespace split.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

#: Test-assertion label patterns: ``it('…')`` / ``test('…')`` /
#: ``describe('…')`` (modifier chains ``it.only(`` / ``describe.skip(``
#: included; ' " ` quotes) + python ``def test_*`` names.
_TEST_LABEL_RE = re.compile(
    r"\b(?:it|test|describe)(?:\.\w+)*\(\s*['\"`]([^'\"`\n]+)['\"`]")
_PY_TEST_DEF_RE = re.compile(r"^\s*def\s+test_(\w+)", re.MULTILINE)

#: HTTP method → verb family (B57 Seg1 (c)). STRUCTURAL semantics of the
#: HTTP verb itself (RFC 9110 method → CRUD family), not a repo
#: vocabulary — module-level frozen. Filesystem "PAGE" pseudo-methods
#: deliberately absent from the METHOD map — a page route declares no
#: author action verb; its READ nature feeds the b57-iter2 composition
#: rung via :data:`_PAGE_VERB_FAMILIES` instead.
_HTTP_METHOD_VERB_FAMILIES: Mapping[str, frozenset[str]] = {
    "GET": frozenset({"browse", "view"}),
    "POST": frozenset({"create"}),
    "PUT": frozenset({"update"}),
    "PATCH": frozenset({"update"}),
    "DELETE": frozenset({"delete"}),
}

#: b57-iter2 — a PAGE surface (filesystem-routed page / page-kind flow
#: entry) is a READ surface: navigating to it views/browses, it mutates
#: nothing. Structural navigation semantics, not a repo vocabulary.
_PAGE_VERB_FAMILIES = frozenset({"browse", "view"})

#: Flow-entry kinds that mark a page/navigation-class flow. ``Flow`` has
#: no dedicated kind field — Stage 3 stamps ``description = entry.route
#: or entry.kind``, so a NON-routed page entry carries its kind here
#: (routed pages are covered by the routes_index ``PAGE`` rows instead).
_PAGE_FLOW_KINDS = frozenset({"page"})


def route_verb_indexes(
    routes_index: Iterable[Mapping[str, Any]] | None,
) -> tuple[dict[str, set[str]], set[str]]:
    """``(method_families_by_file, page_files)`` — the per-file verb-fact
    indexes of ``routes_index``: declared HTTP methods fold to verb
    families via :data:`_HTTP_METHOD_VERB_FAMILIES`; filesystem ``PAGE``
    rows collect into a page-surface file set. Shared by Law C's B57
    Seg1 rungs and the Stage 6.7e adjudicator (one builder, no dup)."""
    method_fams: dict[str, set[str]] = {}
    page_files: set[str] = set()
    for r in (routes_index or ()):
        rf = str(r.get("file") or "")
        if not rf:
            continue
        meth = str(r.get("method") or "").strip().upper()
        fams = _HTTP_METHOD_VERB_FAMILIES.get(meth)
        if fams:
            method_fams.setdefault(rf, set()).update(fams)
        elif meth == "PAGE":
            page_files.add(rf)
    return method_fams, page_files


def member_verb_composition(
    uf: Any,
    flow_by_id: Mapping[str, Any],
    method_fams_by_file: Mapping[str, set[str]],
    page_files: set[str] | frozenset[str],
) -> set[str]:
    """b57-iter2 — the verb families a journey's MEMBER COMPOSITION
    implies (scan facts, $0): (a) member routes' declared HTTP methods
    (via :func:`route_verb_indexes`), (b) page-surface members — a PAGE
    route file or a page-kind flow entry — imply the read families. An
    EMPTY result means the composition asserts NOTHING about verbs (no
    facts — no claim; the honest ``missing:verb`` stays)."""
    fams: set[str] = set()
    for m in (getattr(uf, "member_flow_ids", None) or []):
        fl = flow_by_id.get(str(m))
        if fl is None:
            continue
        paths = {str(p) for p in (getattr(fl, "paths", None) or []) if p}
        ep = getattr(fl, "entry_point_file", None)
        if ep:
            paths.add(str(ep))
        for p in sorted(paths):
            fams |= method_fams_by_file.get(p, set())
            if p in page_files:
                fams |= _PAGE_VERB_FAMILIES
        if str(getattr(fl, "description", "") or "") in _PAGE_FLOW_KINDS:
            fams |= _PAGE_VERB_FAMILIES
    return fams


#: Work bounds for the file-reading rungs (i18n-key / test-assert). Size
#: cap mirrors ``product_strings._MAX_FILE_BYTES``; the per-UF file cap
#: bounds pathological member fan-outs. Bounds-of-work only.
_RUNG_SOURCE_MAX_FILE_BYTES = 256 * 1024
_RUNG_SOURCE_MAX_FILES = 32


def _i18n_keys_from_text(text: str) -> list[str]:
    """Identifier-shaped i18n KEYS referenced in one source file, in
    document order. VALUES (anything with whitespace) are structurally
    rejected — the operator rule 2026-07-13: translations may live
    outside the repo; only the key names are code-ground truth."""
    out: list[str] = []
    for rx in _I18N_KEY_REFS:
        for m in rx.finditer(text):
            key = m.group(1).strip()
            if key and _I18N_IDENT_RE.fullmatch(key):
                out.append(key)
    return out


def _test_assertion_labels(text: str) -> list[str]:
    """Assertion labels authored inside one MAPPED test file, in document
    order: JS/TS ``it/test/describe('…')`` strings + python ``def
    test_*`` names (underscores → spaces so both tokenize alike)."""
    labels = [m.group(1) for m in _TEST_LABEL_RE.finditer(text)]
    labels.extend(
        m.group(1).replace("_", " ") for m in _PY_TEST_DEF_RE.finditer(text))
    return labels


# ── Candidate builders ──────────────────────────────────────────────────


def build_pf_candidates(
    pf: Any,
    vocab: Mapping[str, Any],
    *,
    nav_label: str | None = None,
    repo_root: Any = None,
) -> list[str]:
    """Ranked display candidates for one product feature (dedup, order-
    preserving). The CURRENT display (casing-polished) is always present
    — the contract can never invent from nothing (never-worse).

    ``repo_root`` (B27) arms the package-manifest channel: a package-dir-
    anchored PF (``hub:``-vendor / ``ws:``) leads with the display name
    the package DECLARES in its own metadata (config.json name /
    metadata-module name / package.json displayName / authored name),
    composing with the existing hub decoration ("App Store — Stripe"); a
    mechanical letter/digit word-split of the dir slug is the rung below
    ("Exchange 2013 Calendar"). ``None`` (the default) or
    ``FAULTLINE_PF_MANIFEST_NAME=0`` is byte-identical to pre-B27."""
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
    # B50 Seg2 — a PF display that would BE a pure route param ('/p/$url' ->
    # 'URL') names no domain object: resolve a domain noun from the anchor
    # (minus the param) / nav-cluster / member-file dominant domain noun and
    # lead with it. Never the route letter 'p'. Flag OFF ⇒ byte-identical.
    if uf_name_degrime_enabled() and _is_pure_param_display(
            polish_display_casing(current, vocab)):
        _add(_resolve_param_display(
            anchor_id, nav_label, getattr(pf, "paths", None) or [], vocab))
    pkg_display, _pkg_src = _package_manifest_display(
        anchor_id, vocab, repo_root, current)
    if pkg_display:
        _add(hub_composition_display(
            anchor_id, current, vocab, vendor_display=pkg_display))
    _add(hub_composition_display(anchor_id, current, vocab))
    src, _p = _anchor_path(anchor_id)
    if pkg_display and src != "hub":
        _add(pkg_display)  # bare manifest name for a ws package anchor
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
    if pkg_display and base:
        # B27 — the manifest word replaces the dir-slug base so the
        # existing "(Qualifier)" decoration keeps composing
        # ("Stripe (App Store)" instead of "Stripepayment (App Store)").
        base = pkg_display
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
    words = [x for x in words if x]
    # B50 — the canonical resource renderer is the JOINER for every UF-name
    # template + Law-A/C resource phrase; de-grime its token stream so an
    # echoed ('case case ids') or param-glued resource never composes into a
    # display. Flag OFF ⇒ byte-identical.
    if uf_name_degrime_enabled():
        words = _degrime_resource_words(words, v)
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

    # B50 Seg1 — a template whose verb duplicates the leading resource token
    # ('Ingest ingest', 'View views', 'Send send test email') collapses at
    # the joiner. Flag OFF ⇒ byte-identical.
    if uf_name_degrime_enabled():
        template_name = _degrime_display(template_name)

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
    # B50 Seg1 — an adjacent-echo current name ('Manage API case case ids')
    # passes the display laws yet must yield to the now-clean template: mark
    # it UNCLEAN so the template leads and the echo is re-derived away.
    if uf_name_degrime_enabled() and current_clean and (
        _degrime_display(polished_current) != polished_current
    ):
        current_clean = False
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


#: B61 — deterministic dominant-family priority for a member verb-composition.
#: Mutation families outrank read (a WRITE is the stronger claim the code
#: makes — SACRED: mutation name only over a mutation composition); ties break
#: by this frozen CRUD order. The composition families a journey can ever imply
#: are exactly ``member_verb_composition``'s range — the HTTP-method families
#: (:data:`_HTTP_METHOD_VERB_FAMILIES`) ∪ the page-read families
#: (:data:`_PAGE_VERB_FAMILIES`) = {browse, view, create, update, delete}; no
#: "act"/"read" bucket ever appears, so every family here has a canonical
#: display verb in :data:`_ACTION_FAMILY_WORD`.
_SNAP_MUTATION_ORDER: tuple[str, ...] = ("create", "update", "delete")
_SNAP_READ_ORDER: tuple[str, ...] = ("browse", "view")


def _dominant_comp_family(comp: Iterable[str]) -> str | None:
    """The family whose canonical verb a verb-snap adopts: the first present
    mutation family (create → update → delete) when the composition mutates,
    else the first present read family (browse → view). ``None`` when the
    composition names no snap-eligible family (defensive; e.g. an empty set —
    the caller must already have skipped it)."""
    fams = set(comp)
    for fam in _SNAP_MUTATION_ORDER:
        if fam in fams:
            return fam
    for fam in _SNAP_READ_ORDER:
        if fam in fams:
            return fam
    return None


def _snap_lead_verb(
    name: str, target_fam: str, vocab: Mapping[str, Any],
) -> str:
    """Replace a UF display's LEADING word (the verb that lies) with the
    canonical verb of ``target_fam`` (:data:`_ACTION_FAMILY_WORD` — the
    engine's frozen per-family display verb; for the composition families it
    IS the family's canonical head verb, e.g. create → "Create"), preserving
    the resource remainder VERBATIM, then run the FULL B50 chain (degrime echo
    collapse + casing polish). 'Manage webhooks' + create → 'Create webhooks';
    'Handle incoming events' + delete → 'Delete incoming events'. ONLY the
    single leading token is replaced (the resource part is SACRED — spec B61)."""
    verb = _ACTION_FAMILY_WORD.get(target_fam)
    if not verb:
        return name
    parts = (name or "").strip().split(None, 1)
    remainder = parts[1] if len(parts) == 2 else ""
    snapped = f"{verb} {remainder}".strip() if remainder else verb
    if uf_name_degrime_enabled():
        snapped = _degrime_display(snapped)
    return polish_display_casing(snapped, vocab)


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
    nav_labels: Mapping[str, str] | None = None,
    flow_origin_by_id: Mapping[str, str] | None = None,
    flow_by_id: Mapping[str, Any] | None = None,
    nav_label_sets: Mapping[str, list[str]] | None = None,
    routes_index: Iterable[Mapping[str, Any]] | None = None,
    repo_root: Any = None,
    adjudicated_sources: Mapping[str, Mapping[str, Iterable[str]]] | None = None,
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

    def _uf_member_evidence(uf: Any) -> frozenset[str]:
        """B70 — resource tokens the UF's OWN evidence grounds (member flow
        names + resource + resource-phrase), singular-folded. Arms the
        FAULTLINE_NAMING_LAW member-evidence display law so a grounded
        verb-homonym resource survives and an evidence-less remainder is
        flagged. Only consulted when that flag is armed (OFF ⇒ ignored, so
        threading it here is byte-identical when the flag is off)."""
        toks: set[str] = set()
        srcs = list(_members(uf))
        srcs.append(str(getattr(uf, "resource", "") or ""))
        srcs.append(_res(uf))
        for src in srcs:
            for tok in re.split(r"[^a-z0-9]+", (src or "").lower()):
                if tok:
                    toks.add(_degrime_sing(tok))
        return frozenset(toks)

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
                    and not display_law_violations(
                        cand, vocab, pf_display=_pfd(uf) or None,
                        member_tokens=_uf_member_evidence(uf))):
                uf.name = cand
                narrowed.add(str(getattr(uf, "id", "") or ""))
                tele["uf_claim_narrowed"] = tele.get("uf_claim_narrowed", 0) + 1

    # ── Law A — UF-vs-UF display uniqueness (never a numeric suffix) ──
    # step 1: undo the labeler's lossy collapse — a colliding lattice action
    # child reverts to its deterministic "<Family> <resource>" name.
    # B50 (degrime ON only): the re-derived cand carries a DEGRIMED resource
    # phrase, so it can newly collide with a third row — apply step-1 renames
    # collision-safely (targets computed first, then only non-colliding
    # renames land; skip-both on duplicate targets). Flag OFF ⇒ the original
    # immediate-apply path, byte-identical.
    _lawa_two_phase = uf_name_degrime_enabled()
    _lawa_proposals: dict[str, str] = {}
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
                    and not display_law_violations(
                        cand, vocab, pf_display=_pfd(uf) or None,
                        member_tokens=_uf_member_evidence(uf))):
                if _lawa_two_phase:
                    _lawa_proposals[str(getattr(uf, "id", "") or "")] = cand
                else:
                    uf.name = cand
    if _lawa_proposals:
        _cur = {str(getattr(u, "id", "") or ""): str(getattr(u, "name", "") or "")
                for u in ordered}
        _by_id = {str(getattr(u, "id", "") or ""): u for u in ordered}
        for _uid in sorted(degrime_rename_plan(_cur, _lawa_proposals)):
            _by_id[_uid].name = _lawa_proposals[_uid]
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
                # B50 Seg1 — never append a qualifier that merely restates a
                # word already in the base ('Teams (Team)', 'Manage links
                # (link)', 'Manage settings (Settings)', 'Manage tRPC (tRPC)').
                if uf_name_degrime_enabled() and _qualifier_echoes_base(base, q):
                    continue
                cand = polish_display_casing(f"{base} ({q})", vocab)
                fld = cand.strip().lower()
                if (fld not in taken
                        and not display_law_violations(
                            cand, vocab, pf_display=pfd or None,
                            member_tokens=_uf_member_evidence(uf))):
                    uf.name = cand
                    taken[fld] = str(getattr(uf, "id", "") or "")
                    qualified.add(str(getattr(uf, "id", "") or ""))
                    tele["uf_uniqueness_qualified"] = tele.get("uf_uniqueness_qualified", 0) + 1
                    break

    # ── Law C — evidence-derived name_confidence (one rubric, all sources) ──
    # B40 (provenance rungs, FAULTLINE_NAME_EVIDENCE_RUNGS): the base rubric is
    # unchanged; when the flag is ON two extra grounding rungs may fire — a
    # nav-label token match (resource-grounding) and an all-dispatch-member
    # registry provenance (verb-grounding for Run/act leads) — and EVERY arm
    # stamps ``name_evidence`` (fired rungs, or ``missing:*`` for a low). The
    # flag NEVER changes a UF NAME; with it OFF the confidence values and the
    # serialized output are byte-identical to pre-B40 (name_evidence stays None).
    # ── B50 Seg2: raw-param display law over the FINAL UF name (mutate a
    # glyph-less route-param slug to its noun core, drop standalone
    # addressing tokens) — applied before Law C so confidence is scored on
    # the clean name. DISPLAY-ONLY; protected (authored/pinned) journeys are
    # never re-worded. COLLISION-SAFE (kan forensics): targets are computed
    # first over sorted UFs, then only non-colliding renames apply — two
    # rows deparaming to the same name ('boardids'/'boardslugs' → 'board')
    # BOTH keep their original names. Flag OFF ⇒ byte-identical.
    if uf_name_degrime_enabled():
        dep_proposals: dict[str, str] = {}
        for uf in ordered:
            if _uf_protected(uf, authored_ids, keeper_on):
                continue
            cur = str(getattr(uf, "name", "") or "")
            deparamed = _deparam_display(cur)
            if (deparamed and deparamed != cur
                    and not display_law_violations(
                        deparamed, vocab, pf_display=_pfd(uf) or None,
                        member_tokens=_uf_member_evidence(uf))):
                dep_proposals[str(getattr(uf, "id", "") or "")] = deparamed
        if dep_proposals:
            cur_names = {
                str(getattr(u, "id", "") or ""): str(getattr(u, "name", "") or "")
                for u in ordered
            }
            allowed = degrime_rename_plan(cur_names, dep_proposals)
            by_id = {str(getattr(u, "id", "") or ""): u for u in ordered}
            for uid in sorted(allowed):
                by_id[uid].name = dep_proposals[uid]
                tele["uf_name_degrimed"] = tele.get("uf_name_degrimed", 0) + 1
            skipped = len(dep_proposals) - len(allowed)
            if skipped:
                tele["uf_degrime_collision_skipped"] = (
                    tele.get("uf_degrime_collision_skipped", 0) + skipped)

    # ── B61 Seg1 — evidence-born verb-snap (FAULTLINE_UF_VERB_SNAP) ──
    # A UF display whose LEAD-verb family is ABSENT from the member
    # verb-composition (the name's verb lies about the code) has that lead
    # verb REPLACED by the canonical verb of the composition's DOMINANT
    # family (mutation outranks read). The resource remainder, membership,
    # identity, and lineage are untouched; authored/pinned rows are exempt;
    # an EMPTY composition leaves the name UNCHANGED (no facts → no claim →
    # honest missing:verb below). Runs BEFORE Law C so the snapped name is
    # scored by the existing structural:verb-composition rung (earned high
    # at $0, requires FAULTLINE_UF_RUNG_SOURCES_V2 co-armed). COLLISION-SAFE
    # (B31): targets computed first, two rows never snap to one name. This
    # body — inside _apply_uf_name_laws — runs identically in the initial
    # contract pass and the B57 Seg2 rescore seam; it is IDEMPOTENT (once
    # lead ∈ comp the snap never refires). Flag OFF ⇒ names/confidence/
    # serialized output byte-identical (the ONLY UF-name-changing flag).
    _snap_on = uf_verb_snap_enabled()
    _snap_mf: dict[str, set[str]] = {}
    _snap_pf: set[str] = set()
    _snap_fbi: Mapping[str, Any] = flow_by_id or {}
    _snap_tele: dict[str, Any] = {}
    if _snap_on:
        _snap_tele = tele.setdefault("uf_verb_snap", {
            "snapped": 0, "families": {}, "skipped_empty": 0,
            "skipped_authored": 0, "skipped_collision": 0,
            "generic_grounded": 0,
        })
        _snap_mf, _snap_pf = route_verb_indexes(routes_index)
        _snap_proposals: dict[str, str] = {}
        _snap_fam: dict[str, str] = {}
        for uf in ordered:
            if _uf_protected(uf, authored_ids, keeper_on):
                _snap_tele["skipped_authored"] += 1
                continue
            cur = str(getattr(uf, "name", "") or "")
            lead = _name_lead_family(cur, idx)
            if lead is None:
                continue  # no recognized lead verb — nothing to fold (honest)
            comp = member_verb_composition(uf, _snap_fbi, _snap_mf, _snap_pf)
            if not comp:
                _snap_tele["skipped_empty"] += 1
                continue  # SACRED — empty composition, name unchanged
            if lead in comp:
                continue  # verb already grounded by composition — no change
            # b61-iter3 NEVER-WORSE guard (real keyed samples caught it):
            # a lead the member FLOW-NAME families already witness (Law
            # C's base ``lead in mfams`` source) is TRUE — page-only
            # compositions under-represent mutations (server actions are
            # invisible to routes_index), so overwriting a member-named
            # Create/Verify/Run lead with a read verb would make the name
            # LIE and could demote an already-high row ('Create and
            # manage data rooms' -> 'Browse ...' harm class). Snap fires
            # ONLY for rows whose lead no verb source grounds — the
            # would-be missing:verb residue.
            if lead in _mfams(_members(uf)):
                _snap_tele["skipped_member_named"] = (
                    _snap_tele.get("skipped_member_named", 0) + 1)
                continue
            target = _dominant_comp_family(comp)
            if target is None:
                continue
            snapped = _snap_lead_verb(cur, target, vocab)
            # Defense in depth: only commit when the snapped lead verb truly
            # folds back INTO the composition (earned-high by construction)
            # and the result is law-clean (no pf_uf_twin / param / etc.).
            if (snapped and snapped.strip().lower() != cur.strip().lower()
                    and _name_lead_family(snapped, idx) in comp
                    and not display_law_violations(
                        snapped, vocab, pf_display=_pfd(uf) or None,
                        member_tokens=_uf_member_evidence(uf))):
                _uid = str(getattr(uf, "id", "") or "")
                _snap_proposals[_uid] = snapped
                _snap_fam[_uid] = target
        if _snap_proposals:
            _snap_cur = {
                str(getattr(u, "id", "") or ""): str(getattr(u, "name", "") or "")
                for u in ordered
            }
            _snap_by_id = {str(getattr(u, "id", "") or ""): u for u in ordered}
            _snap_allowed = degrime_rename_plan(_snap_cur, _snap_proposals)
            for _uid in sorted(_snap_allowed):
                _snap_by_id[_uid].name = _snap_proposals[_uid]
                _snap_tele["snapped"] += 1
                _fam = _snap_fam[_uid]
                _snap_tele["families"][_fam] = (
                    _snap_tele["families"].get(_fam, 0) + 1)
            _snap_tele["skipped_collision"] += (
                len(_snap_proposals) - len(_snap_allowed))

    rungs_on = name_evidence_rungs_enabled()
    _nav = nav_labels or {}
    _origin = flow_origin_by_id or {}
    tele["confidence_before"] = _conf_hist(user_flows)

    def _sing(t: str) -> str:
        return t[:-1] if (t.endswith("s") and len(t) > 3) else t

    def _toks(text: str) -> set[str]:
        return {_sing(t) for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}

    # ── B50 Seg3 — earned resource rung (FAULTLINE_UF_RESOURCE_RUNG) ──
    # A low ``missing:resource`` UF (verb grounded, resource not) earns
    # resource-grounding ONLY from a REAL evidence rung. Bar UNCHANGED — these
    # are extra OR-sources to ``res_grounded``, never a lowered threshold. Flag
    # OFF ⇒ every hit below is False ⇒ res_grounded / confidence / name_evidence
    # byte-identical.
    rung_on = uf_resource_rung_enabled()
    _flow_by_id = flow_by_id or {}
    _extract_domain_noun = None
    if rung_on:
        try:
            from faultline.pipeline_v2.domain_noun import extract_domain_noun
            _extract_domain_noun = extract_domain_noun
        except Exception:  # noqa: BLE001 — optional structural resolver
            _extract_domain_noun = None
        tele["resource_rung_fired"] = {"member-noun": 0, "route": 0, "test": 0}

    def _member_paths(u: Any) -> list[str]:
        """Union of the member flows' entry_point_file + paths."""
        out: set[str] = set()
        for m in (getattr(u, "member_flow_ids", None) or []):
            fl = _flow_by_id.get(str(m))
            if fl is None:
                continue
            for p in (getattr(fl, "paths", None) or []):
                if p:
                    out.add(str(p))
            ep = getattr(fl, "entry_point_file", None)
            if ep:
                out.add(str(ep))
        return sorted(out)

    def _member_test_nouns(u: Any) -> set[str]:
        """Singular-folded nouns from a member flow's MAPPED test files (B36 —
        flow_test_mapper only maps a test whose member-overlap already holds)."""
        nouns: set[str] = set()
        for m in (getattr(u, "member_flow_ids", None) or []):
            fl = _flow_by_id.get(str(m))
            if fl is None:
                continue
            for tf in (getattr(fl, "test_files", None) or []):
                base = str(tf).rsplit("/", 1)[-1]
                stem = re.split(
                    r"\.(?:test|spec|e2e|stories|cy)\b", base, maxsplit=1,
                    flags=re.IGNORECASE)[0]
                if "." in stem:
                    stem = stem.rsplit(".", 1)[0]
                nouns |= _toks(re.sub(r"[-_.]+", " ", stem))
        return nouns

    # ── B57 Seg1 — rung-source expansion (FAULTLINE_UF_RUNG_SOURCES_V2) ──
    # Four extra OR-sources for the SAME Law C bar (B50 Seg3 precedent):
    # nav-cluster / i18n-key ground the resource, route-method grounds the
    # verb, test-assert grounds either. Flag OFF ⇒ every hit below is
    # False and no telemetry key is added ⇒ confidence / name_evidence /
    # serialized output byte-identical.
    v2_on = uf_rung_sources_v2_enabled()
    _nav_sets: Mapping[str, list[str]] = nav_label_sets or {}
    # B57 Seg2 — adjudicated rung evidence (Stage 6.7e): per-UF VERIFIED
    # citation rungs act as extra OR-sources at the SAME bar, per channel
    # (``{uf_id: {"resource": [rungs], "verb": [rungs]}}`` — the verb
    # channel is the b57-seg2-iter ruling). Confidence is only ever
    # written HERE — the adjudicator hands Law C its verified evidence
    # and Law C judges. Absent map ⇒ byte-identical.
    _adjudicated: Mapping[str, Mapping[str, Iterable[str]]] = (
        adjudicated_sources or {})
    _v2_repo: Any = None
    _v2_read_cache: dict[str, str] = {}
    _method_fams_by_file: dict[str, set[str]] = {}
    _page_files: set[str] = set()
    if v2_on:
        tele["rung_sources_v2_fired"] = {
            "nav-cluster": 0, "i18n-key": 0, "route-verb": 0,
            "test-assert": 0, "verb-composition": 0,
        }
        _method_fams_by_file, _page_files = route_verb_indexes(routes_index)
        if repo_root is not None:
            try:
                from pathlib import Path
                _v2_repo = Path(str(repo_root))
            except (TypeError, ValueError):  # pragma: no cover — defensive
                _v2_repo = None

    def _v2_read(rel: str) -> str:
        """Capped, cached member-file read (bounds-of-work only)."""
        if rel in _v2_read_cache:
            return _v2_read_cache[rel]
        text = ""
        if _v2_repo is not None:
            try:
                fp = _v2_repo / rel
                if (fp.is_file()
                        and fp.stat().st_size <= _RUNG_SOURCE_MAX_FILE_BYTES):
                    text = fp.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
        _v2_read_cache[rel] = text
        return text

    def _member_test_files(u: Any) -> list[str]:
        """MAPPED test files of the member flows (``flow.test_files`` —
        flow_test_mapper already guarantees member-overlap; the rung never
        reads an unmapped test file)."""
        files: set[str] = set()
        for m in (getattr(u, "member_flow_ids", None) or []):
            fl = _flow_by_id.get(str(m))
            if fl is None:
                continue
            files.update(str(tf) for tf in (getattr(fl, "test_files", None) or []) if tf)
        return sorted(files)

    def _is_verb_tok(t: str) -> bool:
        return t in idx["verb2fam"] or _sing(t) in idx["verb2fam"]

    def _grounds_resource(src_toks: set[str], res_toks: set[str],
                          name_toks: set[str]) -> bool:
        """Resource-grounding overlap for prose/identifier sources: a
        resource-phrase token always grounds; a NAME token grounds only
        when it is not an action verb (a verb echo — 'manages'/'delete_'
        — must never stand in for resource evidence; the verb channel
        judges verbs)."""
        if src_toks & res_toks:
            return True
        return bool({t for t in src_toks if not _is_verb_tok(t)} & name_toks)

    def _fold_read(fam: str | None) -> str | None:
        """browse/view collapse to 'read' for family agreement — the
        split is an id-marker display heuristic, not an evidence line."""
        return "read" if fam in ("browse", "view") else fam

    def _label_lead_family(label: str) -> str | None:
        """Leading-verb family of an assertion label, singular-folded so
        prose thirds ('creates …', 'renders …') resolve like base verbs."""
        toks_l = [t for t in re.split(r"[^a-z0-9]+", label.lower()) if t]
        if not toks_l:
            return None
        fam = (idx["verb2fam"].get(toks_l[0])
               or idx["verb2fam"].get(_sing(toks_l[0])))
        if fam == "read":
            return "view" if (idx["id_markers"] & set(toks_l)) else "browse"
        return fam

    for uf in ordered:
        uid = str(getattr(uf, "id", "") or "")
        names = _members(uf)
        if not names:
            uf.name_confidence = "low"
            # A member-less row can never uplift (the rubric floor) — its low
            # is honest: there is no member evidence to ground a name.
            if rungs_on:
                uf.name_evidence = ["missing:members"]
            continue
        if uid in narrowed:
            uf.name_confidence = "low"
            # Law B narrowed an unsupported write claim → the claimed verb had
            # no member evidence; low is honest.
            if rungs_on:
                uf.name_evidence = ["missing:verb"]
            continue
        mfams = _mfams(names)
        lead = _name_lead_family(str(getattr(uf, "name", "") or ""), idx)

        # resource-grounded: the name's resource phrase overlaps the PF display
        # (member-grounded) or a member flow name — singular/plural-robust so
        # "webhook" grounds against "webhooks".
        res_toks = _toks(_res(uf))
        member_toks = set().union(*(_toks(n) for n in names)) if names else set()
        base_res = bool(res_toks & member_toks) or bool(res_toks & _toks(_pfd(uf)))
        # A specific verb must be performed by a member; a generic lead
        # (Manage/Overview — not in any CRUD family) is grounded by any member
        # action (it abstracts, it does not over-claim a specific verb).
        base_verb = (lead in mfams) or (lead is None and len(mfams) >= 1)

        # ── B40 rungs (flag-gated corroboration — NEVER a name change) ──
        # nav rung: the author's own nav label for this UF's PF shares a token
        # with the UF name (INGEST doctrine — author's vocabulary) ⇒ the
        # resource is grounded even when the code-derived resource missed it.
        name_toks = _toks(str(getattr(uf, "name", "") or ""))
        nav_label = _nav.get(str(getattr(uf, "product_feature_id", None) or ""))
        nav_hit = bool(rungs_on and nav_label and (_toks(nav_label) & name_toks))
        # registry rung: every member flow is a maintainer-declared dispatch
        # mint (B34 — the author's own key + exported symbol). That provenance
        # is verb-grounding for a Run/act-family lead (the 'Run X' template).
        member_ids = [str(m) for m in (getattr(uf, "member_flow_ids", None) or [])]
        registry_all = bool(
            rungs_on and member_ids
            and all(_origin.get(m) == "dispatch" for m in member_ids)
        )
        registry_hit = bool(registry_all and lead == "act")

        # ── B50 Seg3 rungs (flag-gated; each grounds RESOURCE only) ──
        # (a) member-file domain noun, (b) param-free route segment, (c) mapped
        # test-file noun — each overlapping the UF name or its resource phrase.
        member_noun_hit = route_hit = test_hit = False
        if rung_on:
            mpaths = _member_paths(uf)
            if mpaths:
                if _extract_domain_noun is not None:
                    dn = _extract_domain_noun(mpaths, "")
                    if dn is not None:
                        dn_toks = _toks(dn.label) | {_sing(dn.token)}
                        member_noun_hit = bool(
                            dn_toks & name_toks) or bool(dn_toks & res_toks)
                route_toks: set[str] = set()
                for p in mpaths:
                    for seg in _route_meaningful_segments(p, vocab):
                        route_toks |= _toks(seg)
                route_hit = bool(
                    route_toks & name_toks) or bool(route_toks & res_toks)
            tnouns = _member_test_nouns(uf)
            test_hit = bool(tnouns & name_toks) or bool(tnouns & res_toks)
            # Telemetry — count each NEW grounding (the low the rung uplifts).
            if not base_res and not nav_hit:
                if member_noun_hit:
                    tele["resource_rung_fired"]["member-noun"] += 1
                if route_hit:
                    tele["resource_rung_fired"]["route"] += 1
                if test_hit:
                    tele["resource_rung_fired"]["test"] += 1

        # ── B57 Seg1 rungs (flag-gated; SAME bar, extra OR-sources) ──
        nav_cluster_hit = i18n_hit = route_verb_hit = False
        ta_res_hit = ta_verb_hit = verb_composition_hit = False
        if v2_on:
            _res_pre = (base_res or nav_hit or member_noun_hit or route_hit
                        or test_hit)
            _verb_pre = base_verb or registry_hit
            # (a) nav-cluster — ANY authored label the nav voted onto the
            # owning PF (not just the one top-voted B40 label) may ground
            # the resource. Labels arrive pre-sorted (determinism).
            for lab in _nav_sets.get(
                    str(getattr(uf, "product_feature_id", None) or ""), ()):
                if _grounds_resource(_toks(lab), res_toks, name_toks):
                    nav_cluster_hit = True
                    break
            # (c) route-method — a member route's declared HTTP method
            # grounds a matching verb-family lead (GET never grounds a
            # 'Delete X' lead — families must agree). Index-only, no IO.
            if lead is not None and _method_fams_by_file:
                for p in _member_paths(uf):
                    if lead in _method_fams_by_file.get(p, ()):
                        route_verb_hit = True
                        break
            # (e) structural:verb-composition (b57-iter2) — the verb
            # families the member COMPOSITION implies (member routes'
            # HTTP methods + page-surface members → read families; the
            # cal.com ceiling: UI-composite journeys whose verb exists
            # nowhere in code as a citable string). A lead whose family
            # the composition implies is verb-grounded; a mutation lead
            # over a read-only composition is NOT (family must be
            # PRESENT — mixed GET+POST grounds only browse/view/create).
            # Index-only, no IO. Empty composition asserts nothing.
            if lead is not None and (_method_fams_by_file or _page_files
                                     or _flow_by_id):
                verb_composition_hit = lead in member_verb_composition(
                    uf, _flow_by_id, _method_fams_by_file, _page_files)
            # (b) i18n-key — KEYS referenced in member SOURCE files.
            # Bounded IO, non-high candidates only: read ONLY when the
            # resource is still ungrounded by every cheaper source.
            if not _res_pre and not nav_cluster_hit:
                ktoks: set[str] = set()
                for p in _member_paths(uf)[:_RUNG_SOURCE_MAX_FILES]:
                    for key in _i18n_keys_from_text(_v2_read(p)):
                        ktoks |= _toks(
                            _CAMEL_BOUNDARY_RE.sub(" ", key))
                i18n_hit = _grounds_resource(ktoks, res_toks, name_toks)
            # (d) test-assert — assertion labels inside MAPPED member
            # test files (flow.test_files only — the B36 member-overlap
            # mapping; an unmapped test file is never read). Bounded IO,
            # read only when a channel is still ungrounded.
            _need_res = not (_res_pre or nav_cluster_hit or i18n_hit)
            _need_verb = not (_verb_pre or route_verb_hit
                              or verb_composition_hit)
            if _need_res or _need_verb:
                for tf in _member_test_files(uf)[:_RUNG_SOURCE_MAX_FILES]:
                    for label in _test_assertion_labels(_v2_read(tf)):
                        if _need_res and not ta_res_hit and _grounds_resource(
                                _toks(label), res_toks, name_toks):
                            ta_res_hit = True
                        if (_need_verb and not ta_verb_hit and lead is not None
                                and _fold_read(_label_lead_family(label))
                                == _fold_read(lead)):
                            ta_verb_hit = True
                    if ((ta_res_hit or not _need_res)
                            and (ta_verb_hit or not _need_verb)):
                        break
            # Telemetry — count each NEW grounding (the row the rung
            # uplifts); test-assert counts each channel it newly grounds.
            _v2_tele = tele["rung_sources_v2_fired"]
            if not _res_pre:
                if nav_cluster_hit:
                    _v2_tele["nav-cluster"] += 1
                if i18n_hit:
                    _v2_tele["i18n-key"] += 1
                if ta_res_hit:
                    _v2_tele["test-assert"] += 1
            if not _verb_pre:
                if route_verb_hit:
                    _v2_tele["route-verb"] += 1
                if ta_verb_hit:
                    _v2_tele["test-assert"] += 1
                if verb_composition_hit:
                    _v2_tele["verb-composition"] += 1

        # ── B61 iter2 — generic-lead composition grounding (same
        # FAULTLINE_UF_VERB_SNAP flag). The exact mirror of the base
        # rubric's generic clause (``lead is None and len(mfams) >= 1`` —
        # "it abstracts, it does not over-claim"): a generic/editorial
        # lead with NO action family ('Manage …', 'Complete …',
        # 'Confirm …') is verb-grounded by the member VERB-COMPOSITION —
        # route-declared HTTP methods / page surfaces are a STRONGER
        # evidence class than the flow-name-derived ``mfams`` the base
        # clause accepts. Same bar, one more OR-source (B50 Seg3 / B57
        # Seg1 precedent); names are NEVER touched here (a generic lead
        # has no family conflict — nothing lies, nothing snaps). EMPTY
        # composition stays honest ``missing:verb``. Flag OFF ⇒ False ⇒
        # byte-identical.
        generic_comp_hit = False
        if _snap_on and lead is None:
            generic_comp_hit = bool(member_verb_composition(
                uf, _snap_fbi, _snap_mf, _snap_pf))
            if generic_comp_hit and not (base_verb or registry_hit
                                         or route_verb_hit or ta_verb_hit
                                         or verb_composition_hit):
                _snap_tele["generic_grounded"] += 1

        # ── B57 Seg2 — adjudicated rungs (verified citations; TWO
        # channels since b57-seg2-iter: resource + verb — each verb
        # citation was family-matched against this UF's lead verb by the
        # adjudicator's deterministic verifier before reaching the map) ──
        _adj_entry: Mapping[str, Iterable[str]] = (
            (_adjudicated.get(uid) or {}) if _adjudicated else {})
        adj_res_rungs = sorted(
            {str(a) for a in (_adj_entry.get("resource") or ())})
        adj_verb_rungs = sorted(
            {str(a) for a in (_adj_entry.get("verb") or ())})
        adj_hit = bool(adj_res_rungs)
        adj_verb_hit = bool(adj_verb_rungs)

        res_grounded = (base_res or nav_hit or member_noun_hit or route_hit
                        or test_hit or nav_cluster_hit or i18n_hit
                        or ta_res_hit or adj_hit)
        verb_grounded = (base_verb or registry_hit or route_verb_hit
                         or ta_verb_hit or verb_composition_hit
                         or generic_comp_hit or adj_verb_hit)

        def _rung_fired() -> list[str]:
            r: list[str] = []
            if member_noun_hit:
                r.append("resource:member-noun")
            if route_hit:
                r.append("resource:route")
            if test_hit:
                r.append("resource:test")
            if nav_cluster_hit:
                r.append("resource:nav-cluster")
            if i18n_hit:
                r.append("resource:i18n-key")
            if ta_res_hit:
                r.append("resource:test-assert")
            r.extend(f"adjudicated:{a}" for a in adj_res_rungs)
            return r

        if res_grounded and verb_grounded and uid not in qualified:
            uf.name_confidence = "high"
            if rungs_on:
                fired: list[str] = []
                if base_res and base_verb:
                    fired.append("structural-route")
                if nav_hit:
                    fired.append("nav")
                if registry_hit:
                    fired.append("registry")
                fired.extend(_rung_fired())
                if route_verb_hit:
                    fired.append("verb:route-method")
                if ta_verb_hit:
                    fired.append("verb:test-assert")
                if verb_composition_hit:
                    fired.append("structural:verb-composition")
                if generic_comp_hit:
                    fired.append("structural:verb-composition-generic")
                fired.extend(f"adjudicated:{a}" for a in adj_verb_rungs)
                uf.name_evidence = fired or ["structural-route"]
        elif res_grounded:
            uf.name_confidence = "medium"
            if rungs_on:
                fired = []
                if base_res:
                    fired.append("resource")
                if nav_hit:
                    fired.append("nav")
                fired.extend(_rung_fired())
                fired.append("missing:verb")
                uf.name_evidence = fired
        else:
            uf.name_confidence = "low"
            if rungs_on:
                miss = ["missing:resource"]
                if not verb_grounded:
                    miss.append("missing:verb")
                uf.name_evidence = miss
    tele["confidence_after"] = _conf_hist(user_flows)


def _uf_flow_maps(
    flows: Iterable[Any],
) -> tuple[dict[str, str], dict[str, str], dict[str, Any]]:
    """``(flow_name_by_id, flow_origin_by_id, flow_by_id)`` — the member-id
    lookup maps Law C consumes, keyed by BOTH uuid and name id-forms so
    ``member_flow_ids`` resolve identically everywhere (run_naming_contract
    + the B57 Seg2 ``rescore_uf_confidence`` seam share this builder)."""
    flow_name_by_id: dict[str, str] = {}
    flow_origin_by_id: dict[str, str] = {}
    flow_by_id: dict[str, Any] = {}
    for fl in flows or ():
        _origin_tag = (
            "dispatch"
            if str(getattr(fl, "description", "") or "").startswith(
                "dispatch registry ")
            else ""
        )
        for key in (getattr(fl, "uuid", None), getattr(fl, "name", None)):
            if key:
                flow_name_by_id.setdefault(str(key), str(getattr(fl, "name", "") or ""))
                flow_by_id.setdefault(str(key), fl)
                if _origin_tag:
                    flow_origin_by_id.setdefault(str(key), _origin_tag)
    return flow_name_by_id, flow_origin_by_id, flow_by_id


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
    repo_root: Any = None,
) -> dict[str, Any]:
    """Apply the display-name contract in place; return telemetry.

    Order of authority per display: LAW > PIN > candidate rank. The
    ``labeler`` seam (Wave-3 persona, keyed scans) receives the pending
    items and returns ``{item_key: telemetry-dict}`` decisions — it is
    injected by the caller so this module stays LLM-free and fully
    unit-testable (keyless scans pass ``labeler=None`` and take the
    deterministic top choice everywhere).

    ``repo_root`` (B27) arms the package-manifest display channel for
    package-dir-anchored PFs (see :func:`build_pf_candidates`). ``None``
    (the default) or ``FAULTLINE_PF_MANIFEST_NAME=0`` keeps the emission
    byte-identical to pre-B27.
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
    if uf_devgrain_name_enabled():
        tele["uf_devgrain_stripped"] = 0   # B16 Part-1b
    # B27 package-manifest telemetry — added ONLY when the channel is
    # armed (flag ON + repo root available) so a
    # ``FAULTLINE_PF_MANIFEST_NAME=0`` scan_meta.naming_contract is
    # byte-identical to the pre-B27 emission.
    manifest_channel_on = repo_root is not None and pf_manifest_name_enabled()
    if manifest_channel_on:
        tele["pf_manifest_named"] = 0
        tele["pf_wordsplit_named"] = 0
    # B56 full-name display telemetry — added ONLY when the law is ON so a
    # ``FAULTLINE_PF_FULLNAME_LAW=0`` scan_meta.naming_contract is
    # byte-identical to the pre-B56 emission.
    fullname_law_on = pf_fullname_law_enabled()
    if fullname_law_on:
        tele["pf_fullname_expanded"] = 0
        tele["pf_fullname_missing"] = 0
        tele["uf_fullname_inherited"] = 0
    #: abbr(lower) -> composed "Full Name (ABBR)" for the LEAD token, recorded
    #: during PF Pass 1 and replayed onto UF names (§5). Empty when the law is
    #: OFF ⇒ the final UF pass is a no-op ⇒ byte-identical.
    fullname_map: dict[str, str] = {}

    def _law_fix(law: str) -> None:
        tele["laws_fixed"][law] = tele["laws_fixed"].get(law, 0) + 1

    _authored_map: Mapping[str, Iterable[str]] = uf_authored_names or {}

    def _authored_for(uf: Any) -> list[str]:
        return list(_authored_map.get(str(getattr(uf, "id", "") or ""), ()) or ())

    # Flow display names by member id (verb evidence for UF templates).
    # B40 — ``flow_origin_by_id`` tags each flow's provenance for Law C's
    # registry rung: a dispatch-registry mint (dispatch_registry.py) carries a
    # ``description`` that begins ``"dispatch registry "`` (the maintainer's own
    # declared key + exported symbol). Keyed by the same id forms as the name
    # map so ``member_flow_ids`` resolve identically.
    flow_name_by_id, flow_origin_by_id, flow_by_id = _uf_flow_maps(flows)

    # B57 Seg1 — materialize once (three consumers may iterate) and build
    # the ALL-voted-labels view only when the rung flag is armed (OFF ⇒
    # zero extra work, byte-identical emission).
    routes_index = list(routes_index) if routes_index is not None else None
    nav_labels = nav_labels_for_pfs(
        product_features, product_strings, routes_index)
    nav_label_sets = (
        nav_label_sets_for_pfs(product_features, product_strings, routes_index)
        if uf_rung_sources_v2_enabled() else {}
    )
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
            pf, vocab, nav_label=nav_labels.get(slug), repo_root=repo_root)
        # B27 — the package's own declared display word (None when the
        # channel is off / not a package-dir anchor). Cached read.
        pkg_display, pkg_src = (
            _package_manifest_display(anchor_id, vocab, repo_root, current)
            if manifest_channel_on else (None, "")
        )

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
            if pkg_display:
                # B27 — the manifest word replaces the dir-slug base so
                # the "(Qualifier)" decoration keeps composing.
                base = pkg_display
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

        # ── B50 Seg2: raw-param display law (a glyph-less route-param token
        # never appears in a PF display: 'teamurl documents' -> 'team
        # documents'). Runs last so it also cleans a stale pin. Flag OFF ⇒
        # byte-identical.
        if uf_name_degrime_enabled():
            deparamed = _deparam_display(chosen)
            if (deparamed and deparamed != chosen
                    and not display_law_violations(deparamed, vocab)
                    and deparamed.strip().lower() not in taken):
                chosen, pinned = deparamed, False
                tele["pf_display_degrimed"] = (
                    tele.get("pf_display_degrimed", 0) + 1)

        # ── B56: full-name display law (abbreviation → 'Full Name (ABBR)').
        # A shape-flagged abbreviation display ('Sso', 'Pbac', 'Wp') takes its
        # repo-grounded full form; shape-flagged-but-no-evidence is honest debt
        # (display unchanged + missing telemetry, never invented). Runs last so
        # it also expands a de-grimed/manifest choice. Flag OFF ⇒ no map, no
        # keys, no change ⇒ byte-identical.
        if fullname_law_on:
            fn = apply_fullname_expansion(chosen, pf, repo_root, vocab)
            if (fn.display and fn.display != chosen
                    and not display_law_violations(fn.display, vocab)
                    and fn.display.strip().lower() not in taken):
                if fn.abbr and fn.composed_lead:
                    fullname_map[fn.abbr] = fn.composed_lead
                chosen, pinned = fn.display, False
                tele["pf_fullname_expanded"] += 1
            elif fn.source == "missing:expansion":
                tele["pf_fullname_missing"] += 1

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
        # B27 telemetry — the committed display carries the package's
        # own declared word (bare, hub-composed, or "(Qualifier)"-form).
        if (pkg_display and not pinned and chosen != current and (
                chosen == pkg_display
                or chosen.endswith(f"— {pkg_display}")
                or chosen.startswith(f"{pkg_display} ("))):
            tele[f"pf_{pkg_src}_named"] += 1
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
    # B50 (degrime ON only) — a template adoption caused SOLELY by the
    # Seg1 echo discriminator (law-clean current name, marked unclean by
    # _degrime_display) is DEFERRED and applied collision-safely after the
    # pass: targets first, then only non-colliding renames land (a template
    # already worn by a sibling never duplicates). Flag OFF ⇒ byte-identical.
    degrime_p2_proposals: dict[str, str] = {}
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

        # B50 — pure degrime-induced adoption (no law violation, organic
        # journey, no authored label, current only unclean via the echo
        # mark): defer the template; keep the pre-B50 choice (the law-clean
        # polished current) for this pass.
        polished_cur = polish_display_casing(current, vocab)
        if (uf_name_degrime_enabled() and not violations
                and not getattr(uf, "synthesized", False)
                and not _authored_for(uf)
                and chosen != polished_cur
                and _degrime_display(polished_cur) != polished_cur):
            degrime_p2_proposals[str(getattr(uf, "id", "") or "")] = chosen
            chosen = polished_cur

        # ── B16 Part-1b: UF dev-grain suffix law (mirror the PF law) ──
        # "View detections page" -> "View detections" when the home PF anchor
        # is a route:*-page leak. Display channel (uf.name) only.
        if uf_devgrain_name_enabled() and pf is not None:
            uf_stripped = _strip_uf_devgrain_suffix(
                chosen, str(getattr(pf, "anchor_id", None) or ""), vocab)
            if uf_stripped:
                chosen = uf_stripped
                tele["uf_devgrain_stripped"] = (
                    tele.get("uf_devgrain_stripped", 0) + 1)

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

    # B50 — apply the deferred degrime template adoptions collision-safely
    # (targets computed over the final Pass-2 names; two rows proposing the
    # same template BOTH keep their names; a template already worn by any
    # row never duplicates). Runs before the labeler so Pass 3 sees final
    # names. Flag OFF ⇒ degrime_p2_proposals is empty ⇒ byte-identical.
    if degrime_p2_proposals:
        _p2_cur = {
            str(getattr(u, "id", "") or ""): str(getattr(u, "name", "") or "")
            for u in user_flows
        }
        _p2_by_id = {str(getattr(u, "id", "") or ""): u for u in user_flows}
        _p2_allowed = degrime_rename_plan(_p2_cur, degrime_p2_proposals)
        for _uid in sorted(_p2_allowed):
            _p2_by_id[_uid].name = degrime_p2_proposals[_uid]
            tele["uf_renamed"] += 1
        _p2_skipped = len(degrime_p2_proposals) - len(_p2_allowed)
        if _p2_skipped:
            tele["uf_degrime_collision_skipped"] = (
                tele.get("uf_degrime_collision_skipped", 0) + _p2_skipped)

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
            nav_labels=nav_labels,
            flow_origin_by_id=flow_origin_by_id,
            flow_by_id=flow_by_id,
            # B57 Seg1 — rung-source collectors (inert dict/None when the
            # FAULTLINE_UF_RUNG_SOURCES_V2 flag is OFF).
            nav_label_sets=nav_label_sets,
            routes_index=routes_index,
            repo_root=repo_root,
        )

    # ── B56 final pass: UF names inherit the PF abbreviation expansions ──
    # A journey whose name carries an expanded PF's abbreviation renders with
    # the SAME full form (same proof). Runs after every UF name pass. Flag OFF
    # ⇒ ``fullname_map`` is empty ⇒ no-op ⇒ byte-identical.
    if fullname_map:
        for uf in user_flows:
            name = str(getattr(uf, "name", "") or "")
            if not name:
                continue
            new_name = _inherit_fullname(name, fullname_map)
            if new_name != name:
                uf.name = new_name
                tele["uf_fullname_inherited"] += 1

    # ── B71 Seg A — PF display route-grammar (L-A1) + provenance ladder
    # (L-A2) (FAULTLINE_NAMING_PACK, default OFF). Runs over the FINAL PF
    # displays: cleans router-template residue and upgrades a defective
    # (route-residue / bare-basename) display to the highest available
    # higher-provenance source (nav > manifest > basename). OFF/unset never
    # enters this block -> serialized output byte-identical.
    if naming_pack_enabled():
        from faultline.pipeline_v2.pf_display_provenance import (
            ProvenanceSources,
            apply_pf_display_provenance,
        )

        # Display-cross evidence gate (default OFF): when armed, the nav
        # channel feeding the provenance ladder is the GATED map (foreign
        # labels reverted, anchor-page tie-break, title-cased) instead of
        # the raw B40 top-vote label. OFF/unset ⇒ ``_gated_nav`` is empty
        # and the ``else`` branch reproduces the pre-gate ``nav`` byte for
        # byte.
        _gate_on = pf_display_evidence_gate_enabled()
        _gated_nav = (
            gated_nav_labels_for_pfs(
                product_features, product_strings, routes_index, vocab)
            if _gate_on else {}
        )

        def _pf_sources(pf: Any) -> ProvenanceSources:
            pslug = str(getattr(pf, "name", "") or "")
            aid = str(getattr(pf, "anchor_id", None) or "")
            cur = str(getattr(pf, "display_name", None)
                      or getattr(pf, "name", "") or "")
            manifest = ""
            if repo_root is not None:
                pkg, pkg_src = _package_manifest_display(aid, vocab, repo_root, cur)
                if pkg and pkg_src == "manifest":
                    manifest = pkg
            nav = (
                _gated_nav.get(pslug, "") if _gate_on
                else nav_labels.get(pslug, "") or ""
            )
            return ProvenanceSources(
                nav=nav,
                manifest=manifest,
                basename=_anchor_terminal_segment(aid) or "",
                # Horizon-1 ruling (2026-07-16): anchor-kind scopes the
                # trailing-'+' residue rung (route: only).
                anchor_source=_anchor_path(aid)[0],
            )

        tele["pf_display_provenance"] = apply_pf_display_provenance(
            product_features, _pf_sources, vocab)

    tele["labeler_pending"] = len(pending)
    return tele


def rescore_uf_confidence(
    product_features: list[Any],
    user_flows: list[Any],
    flows: Iterable[Any] = (),
    *,
    product_strings: Any = None,
    routes_index: Iterable[Mapping[str, Any]] | None = None,
    uf_authored_names: Mapping[str, Iterable[str]] | None = None,
    keeper_on: bool = True,
    repo_root: Any = None,
    adjudicated_sources: Mapping[str, Mapping[str, Iterable[str]]] | None = None,
) -> dict[str, Any]:
    """B57 Seg2 — the Law C RE-SCORE seam for the Stage 6.7e adjudicator.

    Re-applies the UF name laws (the same ``_apply_uf_name_laws`` body the
    contract runs, over the CURRENT rows) with the adjudicator's VERIFIED
    citation rungs threaded as extra OR-sources per channel
    (``adjudicated_sources``: ``{uf_id: {"resource": [rung, ...],
    "verb": [rung, ...]}}`` — each stamps an ``adjudicated:<rung>``
    evidence tag; the verb channel is the b57-seg2-iter ruling, its
    citations family-matched by the adjudicator's verifier). The bar is
    UNCHANGED; confidence and evidence are only ever written by Law C —
    the adjudicator itself NEVER touches ``name_confidence``. Idempotent
    over an unchanged board (laws A/B are stable fixed points); no-ops
    (telemetry-only) when the naming contract or the UF name laws are
    switched off."""
    tele: dict[str, Any] = {}
    if not (naming_contract_enabled() and uf_name_laws_enabled()):
        tele["skipped"] = "naming-laws-off"
        return tele
    vocab = load_naming_vocab()
    flow_name_by_id, flow_origin_by_id, flow_by_id = _uf_flow_maps(flows)
    routes_index = list(routes_index) if routes_index is not None else None
    nav_labels = nav_labels_for_pfs(
        product_features, product_strings, routes_index)
    nav_label_sets = (
        nav_label_sets_for_pfs(product_features, product_strings, routes_index)
        if uf_rung_sources_v2_enabled() else {}
    )
    pf_by_slug = {
        str(getattr(pf, "name", "") or ""): pf for pf in product_features
    }
    _authored: Mapping[str, Iterable[str]] = uf_authored_names or {}
    _apply_uf_name_laws(
        user_flows, pf_by_slug, vocab, flow_name_by_id, tele,
        authored_ids={str(k) for k in _authored.keys()},
        keeper_on=keeper_on,
        nav_labels=nav_labels,
        flow_origin_by_id=flow_origin_by_id,
        flow_by_id=flow_by_id,
        nav_label_sets=nav_label_sets,
        routes_index=routes_index,
        repo_root=repo_root,
        adjudicated_sources=adjudicated_sources,
    )
    return tele
