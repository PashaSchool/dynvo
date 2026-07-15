"""B69-v2 SPLIT ruling — the two-flag routing matrix.

After the keyed A/B, the coordinator split the family: the surgical Stage
6.99b rail + naming guards stay under ``FAULTLINE_HOMING_HYGIENE``; the
seed-birth pair (same-(pf,resource) coalescence + method-derived intent)
moved to ``FAULTLINE_SEED_HYGIENE`` (board-wide blast radius at seeding —
its own cycle). Mechanics untouched; this matrix proves the ROUTING:

  * helper independence (each flag arms only its own family);
  * registry parity (both flags in ENV_OUTPUT_FLAGS, KEY_SCHEMA
    unchanged — union append, the bump rides the flip commit only);
  * behaviour matrix: OFF/OFF, HOMING-only, SEED-only, BOTH — each
    family's observable fires only under its own flag.
"""

from __future__ import annotations

from faultline.pipeline_v2.naming_contract import (
    display_law_violations,
    homing_hygiene_enabled,
    load_naming_vocab,
)
from faultline.pipeline_v2.route_group_recall import seed_hygiene_enabled
from faultline.pipeline_v2.scan_result_cache import (
    ENV_OUTPUT_FLAGS,
    KEY_SCHEMA_VERSION,
)
from faultline.pipeline_v2.stage_6_7e_adjudicator import _rescore_tele_view

VOCAB = load_naming_vocab()
_RESCORE = {"confidence_before": {"high": 1}, "uf_uniqueness_qualified": 4}


# ── helper independence ──────────────────────────────────────────────────


def test_helpers_default_off(monkeypatch):
    monkeypatch.delenv("FAULTLINE_HOMING_HYGIENE", raising=False)
    monkeypatch.delenv("FAULTLINE_SEED_HYGIENE", raising=False)
    assert homing_hygiene_enabled() is False
    assert seed_hygiene_enabled() is False


def test_seed_flag_does_not_arm_homing(monkeypatch):
    monkeypatch.delenv("FAULTLINE_HOMING_HYGIENE", raising=False)
    monkeypatch.setenv("FAULTLINE_SEED_HYGIENE", "1")
    assert seed_hygiene_enabled() is True
    assert homing_hygiene_enabled() is False


def test_homing_flag_does_not_arm_seed(monkeypatch):
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    monkeypatch.delenv("FAULTLINE_SEED_HYGIENE", raising=False)
    assert homing_hygiene_enabled() is True
    assert seed_hygiene_enabled() is False


def test_both_flags_arm_both(monkeypatch):
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    monkeypatch.setenv("FAULTLINE_SEED_HYGIENE", "1")
    assert homing_hygiene_enabled() is True
    assert seed_hygiene_enabled() is True


# ── registry parity (union append, no schema bump) ──────────────────────


def test_registry_parity_both_flags_no_schema_bump():
    assert "FAULTLINE_HOMING_HYGIENE" in ENV_OUTPUT_FLAGS
    assert "FAULTLINE_SEED_HYGIENE" in ENV_OUTPUT_FLAGS
    # the bump rides the separate flip commit ONLY (flip-protocol);
    # KEY_SCHEMA 29 = the B62 flip — pinned so a silent bump fails loud.
    assert KEY_SCHEMA_VERSION == 29


# ── behaviour matrix: HOMING observables ─────────────────────────────────


def test_homing_law_fires_only_under_homing(monkeypatch):
    monkeypatch.delenv("FAULTLINE_HOMING_HYGIENE", raising=False)
    monkeypatch.setenv("FAULTLINE_SEED_HYGIENE", "1")
    # SEED-only world: the display law must NOT fire.
    assert "bare_verb" not in display_law_violations("Manage", VOCAB)
    assert "devgrain_token" not in display_law_violations("View API", VOCAB)
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    assert "bare_verb" in display_law_violations("Manage", VOCAB)
    assert "devgrain_token" in display_law_violations("View API", VOCAB)


def test_homing_tele_view_fires_only_under_homing(monkeypatch):
    monkeypatch.delenv("FAULTLINE_HOMING_HYGIENE", raising=False)
    monkeypatch.setenv("FAULTLINE_SEED_HYGIENE", "1")
    assert "uf_uniqueness_qualified" not in _rescore_tele_view(_RESCORE)
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    assert _rescore_tele_view(_RESCORE)["uf_uniqueness_qualified"] == 4


def test_b31_echo_guard_fires_only_under_homing(monkeypatch):
    from types import SimpleNamespace

    from faultline.pipeline_v2.synth_quality import _recall_name_candidates

    uf = SimpleNamespace(
        authored_label=None, resource="links", intent="browse", routes=[])
    monkeypatch.delenv("FAULTLINE_HOMING_HYGIENE", raising=False)
    monkeypatch.setenv("FAULTLINE_SEED_HYGIENE", "1")
    cands_seed_only = _recall_name_candidates(uf, "Links", "Manage links")
    assert "Browse & filter links (Links)" in cands_seed_only  # pre-split
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    cands_homing = _recall_name_candidates(uf, "Links", "Manage links")
    assert "Browse & filter links (Links)" not in cands_homing


# ── behaviour matrix: SEED observables (kwarg contract at the seam) ─────
# The seed pair is armed at the ONLY call site (finalize) via explicit
# kwargs fed by seed_hygiene_enabled(); the kwarg-level mechanics are
# unit-pinned in test_route_group_recall.py. Here we pin the SEAM: the
# finalize call site reads the SEED flag, not the HOMING flag.


def test_finalize_seam_reads_seed_flag_not_homing():
    import inspect

    from faultline.pipeline_v2 import phase_finalize

    src = inspect.getsource(phase_finalize)
    seam = src.split("route_group_seeds_enabled():", 1)[1]
    seam = seam.split("i16_rehome_enabled", 1)[0]
    assert "seed_hygiene_enabled" in seam
    assert "coalesce_same_pf_resource=_seed_hh()" in seam
    assert "derive_seed_intent=_seed_hh()" in seam
    # the HOMING helper must NOT feed the seed kwargs anywhere
    assert "coalesce_same_pf_resource=homing" not in seam
    assert "derive_seed_intent=homing" not in seam
