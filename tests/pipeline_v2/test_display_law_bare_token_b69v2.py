"""B69-v2 add-on 2 → B70 member-evidence redesign — the bare-verb /
dev-grain-token display law (armed by FAULTLINE_NAMING_LAW).

Re-convoy forensics: the ORIGINAL vocabulary-driven implementation
false-positived on verb-homonym resources ('Manage download', 'Browse
webhook', 'Connect auth' — download/webhook/auth all live in verb classes
so the leading-verb strip ate them → 'bare') and MISSED the true exhibit
('View mupdf' — mupdf is in no vocabulary). B70 redesign (landed): when a
row's own member evidence (``member_tokens``) is supplied, the strip STOPS
at a token a member grounds — so a grounded verb-homonym is kept as the
resource — and a nominal remainder no member grounds is flagged
``evidence_absent``. No member context ⇒ the banked vocabulary behavior,
byte-identical (so every no-``member_tokens`` unit below still pins the
banked mechanics exactly). Armed ONLY by FAULTLINE_NAMING_LAW;
HOMING/SEED never arm it.

B56-family codification: 'Manage' (bare verb) and 'View API' / 'Manage
tRPC' (verb + dev-grain transport token) are not journey names. The law
lives in ``display_law_violations`` — the single chokepoint EVERY display
writer checks through (Pass-2 candidates, labeler picks, Pass-4 verifier
REVERTS, C′ renames) — so once armed the class is impossible on any
channel; the keyed-A/B collateral ('Manage', 'View API', 'View mupdf' via
the verifier-revert exposing a raw template) cannot recur.

NAMED ANTI-CASES: 'Manage users' / 'Browse AI' / 'View dashboard
overview' / 'Create and manage webhooks' / 'Run waiting (tRPC)' are clean
(real things after the verb); flag OFF ⇒ law list byte-identical.
"""

from __future__ import annotations

from faultline.pipeline_v2.naming_contract import (
    display_law_violations,
    load_naming_vocab,
)

VOCAB = load_naming_vocab()


def test_bare_verb_flagged_when_armed(monkeypatch):
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    assert "bare_verb" in display_law_violations("Manage", VOCAB)
    assert "bare_verb" in display_law_violations("Browse & manage", VOCAB)
    assert "bare_verb" in display_law_violations("View", VOCAB)


def test_devgrain_token_flagged_when_armed(monkeypatch):
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    assert "devgrain_token" in display_law_violations("View API", VOCAB)
    assert "devgrain_token" in display_law_violations("Manage tRPC", VOCAB)
    assert "devgrain_token" in display_law_violations("API", VOCAB)


def test_anticase_real_things_stay_clean(monkeypatch):
    """Verb + real resource is the canonical journey shape — untouched."""
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    for name in (
        "Manage users",
        "Browse AI",
        "View dashboard overview",
        "Create and manage webhooks",
        "Run waiting (tRPC)",
        "View datarooms",
        "Manage dataroom FAQs",
    ):
        got = display_law_violations(name, VOCAB)
        assert "bare_verb" not in got, name
        assert "devgrain_token" not in got, name


def test_anticase_non_verb_single_word_not_bare(monkeypatch):
    """A single NOUN display is not this law's business (other laws own
    single-word quality); only a verb-class lead with no remainder is."""
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    got = display_law_violations("Datarooms", VOCAB)
    assert "bare_verb" not in got
    assert "devgrain_token" not in got


def test_off_gate_byte_identical(monkeypatch):
    # MECHANICAL (horizon-1 flip): explicit "0" (unset now defaults ON).
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "0")
    for name in ("Manage", "View API", "Manage tRPC", "API"):
        got = display_law_violations(name, VOCAB)
        assert "bare_verb" not in got
        assert "devgrain_token" not in got


def test_verifier_revert_shape_falls_to_law_clean_non_bare(monkeypatch):
    """The ratified anti-case: the Pass-4 revert loop picks the FIRST
    law-clean candidate — with the law armed a raw bare/token template can
    never be that candidate (the keyed-A/B 'Manage'/'View API' channel)."""
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    candidates = ["Manage", "View API", "Browse & manage folders"]
    chosen = None
    for cand in candidates:  # the exact revert-loop shape
        if not display_law_violations(cand, VOCAB, pf_display="Datarooms"):
            chosen = cand
            break
    assert chosen == "Browse & manage folders"
    # MECHANICAL (horizon-1 flip): explicit "0" for the pre-B69-v2 half.
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "0")
    chosen_off = None
    for cand in candidates:
        if not display_law_violations(cand, VOCAB, pf_display="Datarooms"):
            chosen_off = cand
            break
    assert chosen_off == "Manage"  # pre-B69-v2 behaviour, byte-compat


def test_third_split_homing_or_seed_never_arm_the_law(monkeypatch):
    """Re-convoy ruling: the law is BANKED — HOMING and SEED flags must
    never arm it (the churn cascade: ban → retry → new names → collisions
    → B31 parentheticals)."""
    # MECHANICAL (horizon-1 flip): explicit "0" kills the (now default-ON)
    # law so we prove HOMING/SEED do not re-arm it.
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "0")
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    monkeypatch.setenv("FAULTLINE_SEED_HYGIENE", "1")
    for name in ("Manage", "View API", "Manage download", "Browse webhook"):
        got = display_law_violations(name, VOCAB)
        assert "bare_verb" not in got, name
        assert "devgrain_token" not in got, name


def test_banked_forensics_no_member_context_is_byte_identical(monkeypatch):
    """No ``member_tokens`` ⇒ the banked vocabulary behavior, unchanged:
    the refuted false-positive/miss are PRESERVED verbatim on the
    context-free path (the ~28 non-UF call sites), so arming the flag
    there is byte-identical to the banked implementation."""
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    assert "bare_verb" in display_law_violations("Manage download", VOCAB)
    assert "bare_verb" in display_law_violations("Browse webhook", VOCAB)
    assert display_law_violations("View mupdf", VOCAB) == []  # the miss


def test_member_evidence_inverts_the_false_positive(monkeypatch):
    """B70 redesign: a verb-homonym resource a member GROUNDS is the thing,
    not the action — the strip stops at it and the name is clean. Inverts
    the refuted 'Manage download'/'Browse webhook' false-positive."""
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    assert display_law_violations(
        "Manage download", VOCAB,
        member_tokens=frozenset({"download"})) == []
    assert display_law_violations(
        "Browse webhook", VOCAB,
        member_tokens=frozenset({"webhook"})) == []
    # singular/plural-robust: a plural display token folds to the singular
    # the member evidence carries ('_uf_member_evidence' folds member tokens
    # to singular, so 'download' grounds a 'downloads' display).
    assert display_law_violations(
        "Manage downloads", VOCAB,
        member_tokens=frozenset({"download"})) == []


def test_member_evidence_catches_the_miss(monkeypatch):
    """B70 redesign: a nominal remainder NO member backs names nothing real —
    'View mupdf' (mupdf in no member) is now flagged ``evidence_absent``,
    the exhibit the vocabulary mechanism missed."""
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    got = display_law_violations(
        "View mupdf", VOCAB, member_tokens=frozenset({"pdf", "render"}))
    assert "evidence_absent" in got
    # ...but with mupdf actually grounded by a member it is clean.
    assert display_law_violations(
        "View mupdf", VOCAB, member_tokens=frozenset({"mupdf"})) == []


def test_member_evidence_bare_and_transport_still_fire(monkeypatch):
    """The structural laws survive the redesign: a pure verb is still
    ``bare_verb`` and a transport-only remainder still ``devgrain_token``,
    regardless of member evidence (no member can ground 'nothing' or a
    dev-grain transport token into a journey name)."""
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    assert "bare_verb" in display_law_violations(
        "Manage", VOCAB, member_tokens=frozenset({"download"}))
    assert "devgrain_token" in display_law_violations(
        "View API", VOCAB, member_tokens=frozenset({"api", "user"}))


def test_member_evidence_real_resource_stays_clean(monkeypatch):
    """A verb + real (grounded) resource — the canonical journey shape —
    is untouched whether or not the resource is a verb-homonym."""
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    for name, mem in (
        ("Manage users", {"users"}),
        ("Browse AI", {"ai"}),
        ("View datarooms", {"datarooms"}),
        ("Create and manage webhooks", {"webhooks"}),
    ):
        got = display_law_violations(name, VOCAB, member_tokens=frozenset(mem))
        assert got == [], (name, got)
