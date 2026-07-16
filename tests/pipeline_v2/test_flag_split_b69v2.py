"""B69-v2 SPLIT rulings — the THREE-flag routing matrix.

Two keyed re-convoys split the original umbrella into three families:
``FAULTLINE_HOMING_HYGIENE`` = the surgical 6.99b rail + B31 echo-guard +
6.7e telemetry (FINAL composition); ``FAULTLINE_SEED_HYGIENE`` = the
seed-birth pair (board-wide blast radius, own cycle);
``FAULTLINE_NAMING_LAW`` = the banked display law (vocabulary mechanism
refuted — B70 member-evidence redesign). Mechanics untouched; this
matrix proves the ROUTING:

  * helper independence, pairwise (each flag arms only its own family);
  * registry parity (all three flags in ENV_OUTPUT_FLAGS, KEY_SCHEMA
    pinned — union append, the bump rides the flip commit only);
  * behaviour matrix: each family's observable fires only under its own
    flag (single-flag worlds + all-three world).
"""

from __future__ import annotations

from faultline.pipeline_v2.naming_contract import (
    display_law_violations,
    homing_hygiene_enabled,
    load_naming_vocab,
    naming_law_enabled,
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


def test_helpers_defaults(monkeypatch):
    # SEMANTIC (horizon-1 flip): HOMING flips ON; SEED stays OFF (unflipped);
    # NAMING_LAW flips ON (its own commit).
    monkeypatch.delenv("FAULTLINE_HOMING_HYGIENE", raising=False)
    monkeypatch.delenv("FAULTLINE_SEED_HYGIENE", raising=False)
    monkeypatch.delenv("FAULTLINE_NAMING_LAW", raising=False)
    assert homing_hygiene_enabled() is True
    assert seed_hygiene_enabled() is False
    assert naming_law_enabled() is True  # SEMANTIC (horizon-1 flip)


def test_inverted_killswitch_naming_law(monkeypatch):
    """Inverted kill-switch: unset ≡ explicit ``1``; ``0``/``false`` ==
    the pre-flip (banked-law-OFF) behaviour."""
    monkeypatch.delenv("FAULTLINE_NAMING_LAW", raising=False)
    unset = naming_law_enabled()
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    assert naming_law_enabled() is unset is True
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "0")
    assert naming_law_enabled() is False
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "false")
    assert naming_law_enabled() is False


def test_inverted_killswitch_homing_hygiene(monkeypatch):
    """Inverted kill-switch + mechanical-dependency pair: HOMING_HYGIENE is
    read in TWO modules (naming_contract canonical + stage_6_86 duplicate).
    unset ≡ explicit ``1`` in BOTH (unset+unset == ON+ON); explicit ``0`` ==
    old OFF in BOTH."""
    from faultline.pipeline_v2.naming_contract import (
        homing_hygiene_enabled as nc_homing,
    )
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        homing_hygiene_enabled as mint_homing,
    )
    monkeypatch.delenv("FAULTLINE_HOMING_HYGIENE", raising=False)
    assert nc_homing() is mint_homing() is True
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    assert nc_homing() is mint_homing() is True
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "0")
    assert nc_homing() is mint_homing() is False
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "false")
    assert nc_homing() is mint_homing() is False


def test_seed_flag_does_not_arm_homing_or_law(monkeypatch):
    # MECHANICAL (horizon-1 flip): HOMING + NAMING_LAW default ON, so their
    # kill-switches must be set explicitly to prove SEED=1 does not re-arm them.
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "0")
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "0")
    monkeypatch.setenv("FAULTLINE_SEED_HYGIENE", "1")
    assert seed_hygiene_enabled() is True
    assert homing_hygiene_enabled() is False
    assert naming_law_enabled() is False


def test_homing_flag_does_not_arm_seed_or_law(monkeypatch):
    # MECHANICAL (horizon-1 flip): NAMING_LAW defaults ON — kill it explicitly
    # to prove HOMING=1 does not arm the law.
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    monkeypatch.delenv("FAULTLINE_SEED_HYGIENE", raising=False)
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "0")
    assert homing_hygiene_enabled() is True
    assert seed_hygiene_enabled() is False
    assert naming_law_enabled() is False


def test_law_flag_does_not_arm_homing_or_seed(monkeypatch):
    # MECHANICAL (horizon-1 flip): HOMING now defaults ON — set its
    # kill-switch to prove NAMING_LAW=1 does not re-arm it.
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "0")
    monkeypatch.delenv("FAULTLINE_SEED_HYGIENE", raising=False)
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    assert naming_law_enabled() is True
    assert homing_hygiene_enabled() is False
    assert seed_hygiene_enabled() is False


def test_all_three_flags_arm_all(monkeypatch):
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    monkeypatch.setenv("FAULTLINE_SEED_HYGIENE", "1")
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    assert homing_hygiene_enabled() is True
    assert seed_hygiene_enabled() is True
    assert naming_law_enabled() is True


# ── registry parity (union append, no schema bump) ──────────────────────


def test_registry_parity_all_flags_schema_bumped():
    assert "FAULTLINE_HOMING_HYGIENE" in ENV_OUTPUT_FLAGS
    assert "FAULTLINE_SEED_HYGIENE" in ENV_OUTPUT_FLAGS
    assert "FAULTLINE_NAMING_LAW" in ENV_OUTPUT_FLAGS
    # SEMANTIC (horizon-1 flip): the bump rides the flip commit (flip-protocol);
    # KEY_SCHEMA 30 = the horizon-1 flip — pinned so a silent bump fails loud.
    assert KEY_SCHEMA_VERSION == 30


# ── behaviour matrix: HOMING observables ─────────────────────────────────


def test_display_law_fires_only_under_naming_law(monkeypatch):
    # HOMING+SEED world (the re-convoy config): the law must NOT fire.
    # MECHANICAL (horizon-1 flip): explicit "0" kills NAMING_LAW for the
    # unarmed half (unset now defaults ON).
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "0")
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    monkeypatch.setenv("FAULTLINE_SEED_HYGIENE", "1")
    assert "bare_verb" not in display_law_violations("Manage", VOCAB)
    assert "devgrain_token" not in display_law_violations("View API", VOCAB)
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    assert "bare_verb" in display_law_violations("Manage", VOCAB)
    assert "devgrain_token" in display_law_violations("View API", VOCAB)


def test_homing_tele_view_fires_only_under_homing(monkeypatch):
    # MECHANICAL (horizon-1 flip): explicit "0" for the unarmed half.
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "0")
    monkeypatch.setenv("FAULTLINE_SEED_HYGIENE", "1")
    assert "uf_uniqueness_qualified" not in _rescore_tele_view(_RESCORE)
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    assert _rescore_tele_view(_RESCORE)["uf_uniqueness_qualified"] == 4


def test_b31_echo_guard_fires_only_under_homing(monkeypatch):
    from types import SimpleNamespace

    from faultline.pipeline_v2.synth_quality import _recall_name_candidates

    uf = SimpleNamespace(
        authored_label=None, resource="links", intent="browse", routes=[])
    # MECHANICAL (horizon-1 flip): explicit "0" for the unarmed half.
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "0")
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


def test_law_only_world_does_not_arm_homing_observables(monkeypatch):
    """Pairwise completeness: a NAMING_LAW-only world leaves the HOMING
    observables (6.7e tele view, B31 echo-guard) unarmed."""
    from types import SimpleNamespace

    from faultline.pipeline_v2.synth_quality import _recall_name_candidates

    # MECHANICAL (horizon-1 flip): HOMING defaults ON — explicit "0" keeps
    # the HOMING observables unarmed in this NAMING_LAW-only world.
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "0")
    monkeypatch.delenv("FAULTLINE_SEED_HYGIENE", raising=False)
    monkeypatch.setenv("FAULTLINE_NAMING_LAW", "1")
    assert "uf_uniqueness_qualified" not in _rescore_tele_view(_RESCORE)
    uf = SimpleNamespace(
        authored_label=None, resource="links", intent="browse", routes=[])
    cands = _recall_name_candidates(uf, "Links", "Manage links")
    assert "Browse & filter links (Links)" in cands  # pre-split ladder
