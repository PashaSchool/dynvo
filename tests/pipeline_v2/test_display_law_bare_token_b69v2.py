"""B69-v2 add-on 2 — the bare-verb / dev-grain-token display law
(BANKED under FAULTLINE_NAMING_LAW, third split).

Re-convoy forensics: the vocabulary-driven implementation false-positives
on verb-homonym resources ('Manage download', 'Browse webhook', 'Connect
auth') and MISSES the true exhibit ('View mupdf' — in no vocabulary), so
it left the HOMING family and is banked unchanged for the B70
member-evidence redesign. These units pin the banked mechanics AND the
new routing (armed ONLY by FAULTLINE_NAMING_LAW; HOMING/SEED never arm
it).

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
    monkeypatch.delenv("FAULTLINE_NAMING_LAW", raising=False)
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
    monkeypatch.delenv("FAULTLINE_NAMING_LAW", raising=False)
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
    monkeypatch.delenv("FAULTLINE_NAMING_LAW", raising=False)
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    monkeypatch.setenv("FAULTLINE_SEED_HYGIENE", "1")
    for name in ("Manage", "View API", "Manage download", "Browse webhook"):
        got = display_law_violations(name, VOCAB)
        assert "bare_verb" not in got, name
        assert "devgrain_token" not in got, name


def test_banked_forensics_pinned_false_positive_and_miss(monkeypatch):
    """The refutation exhibit, pinned for the B70 redesign: armed, the
    vocabulary mechanism bans healthy resources (verb-homonyms) and
    misses 'View mupdf' — the member-evidence redesign must invert BOTH."""
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    assert "bare_verb" in display_law_violations("Manage download", VOCAB)
    assert "bare_verb" in display_law_violations("Browse webhook", VOCAB)
    assert display_law_violations("View mupdf", VOCAB) == []  # the miss
