"""Flip-packs №2/№3 (KEY_SCHEMA 33/34) — per-flag inverted kill-switch units.

The 2026-07-21 ratified flip-packs (evidence:
``docs/anchor-arc/flip-pack-2-20260719.md`` + the BUG_LEDGER pack-3
assembly section) flip 13 previously-default-OFF flags to default ON,
each in its own commit, ONE KEY_SCHEMA bump per pack riding the pack's
first flip commit (32 -> 33 for pack №2, 33 -> 34 for pack №3). Each
flipped helper must satisfy the flip contract:

  * env UNSET               ⇒ ``enabled()`` is True   (unset ≡ explicit-1)
  * ``<FLAG>=1``            ⇒ ``enabled()`` is True
  * ``<FLAG>=0``            ⇒ ``enabled()`` is False   (the kill-switch)
  * ``<FLAG>=false/off/no`` ⇒ ``enabled()`` is False   (kill-switch aliases)

One deterministic battery per flag (no scans, no LLM). This is the
inverted kill-switch battery the flip-protocol requires: proving unset
now behaves like explicit-1 AND that the X=0 disable still holds for
every flag. ``FAULTLINE_UF_DET_AGGREGATION`` re-enters here (pack №3):
the 04cf47f un-flip is REVERSED — the naming collapse it refuted is
cured by R5 + spray-generalization (ledger §S2-A-V3), and the flag flips
together with its pair ``FAULTLINE_SPRAY_GENERALIZED``.

``_FLIPPED`` grows with each flip commit of the packs — the module-level
count assertion documents how many pack flags have landed so far.
"""

from __future__ import annotations

import pytest

from faultline.pipeline_v2.naming_contract import (
    NAMING_WAVE_R5_ENV,
    PF_DISPLAY_EVIDENCE_GATE_ENV,
    naming_wave_r5_enabled,
    pf_display_evidence_gate_enabled,
)
from faultline.pipeline_v2.stage_1_per_workspace import (
    WORKSPACE_UNION_ENV,
    workspace_union_enabled,
)
from faultline.pipeline_v2.stage_6_99b_post_uf_rehome import (
    ORGANIC_MOVE_ENV,
    organic_move_enabled,
)
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _CONTAINER_INHERIT_ENV,
    _container_inherit_enabled,
)
from faultline.pipeline_v2.extractors.spa_router import (
    SPA_ROUTE_TABLE_ENV,
    spa_route_table_enabled,
)

# (env-var name, helper) for every pack-№2/№3 flag flipped default OFF -> ON.
_FLIPPED = [
    (PF_DISPLAY_EVIDENCE_GATE_ENV, pf_display_evidence_gate_enabled),
    (WORKSPACE_UNION_ENV, workspace_union_enabled),
    (NAMING_WAVE_R5_ENV, naming_wave_r5_enabled),
    (ORGANIC_MOVE_ENV, organic_move_enabled),
    # ── pack №3 (KEY_SCHEMA 34) ──
    (_CONTAINER_INHERIT_ENV, _container_inherit_enabled),
    (SPA_ROUTE_TABLE_ENV, spa_route_table_enabled),
]

# Sanity: grows to 13 with the pack commits; no duplicate envs.
assert len(_FLIPPED) == 6
assert len({env for env, _ in _FLIPPED}) == len(_FLIPPED)


@pytest.mark.parametrize(
    "env,helper", _FLIPPED, ids=[env for env, _ in _FLIPPED],
)
def test_flip_contract_unset_is_on(env, helper, monkeypatch):
    """flip33/34: UNSET ⇒ enabled (default flipped ON)."""
    monkeypatch.delenv(env, raising=False)
    assert helper() is True


@pytest.mark.parametrize(
    "env,helper", _FLIPPED, ids=[env for env, _ in _FLIPPED],
)
def test_flip_contract_explicit_one_is_on(env, helper, monkeypatch):
    """flip33/34: <FLAG>=1 ⇒ enabled (unset ≡ explicit-1)."""
    monkeypatch.setenv(env, "1")
    assert helper() is True


@pytest.mark.parametrize(
    "env,helper", _FLIPPED, ids=[env for env, _ in _FLIPPED],
)
def test_flip_contract_zero_is_kill_switch(env, helper, monkeypatch):
    """flip33/34: <FLAG>=0 ⇒ disabled (the kill-switch survives the flip)."""
    monkeypatch.setenv(env, "0")
    assert helper() is False


@pytest.mark.parametrize(
    "env,helper", _FLIPPED, ids=[env for env, _ in _FLIPPED],
)
@pytest.mark.parametrize("val", ["false", "off", "no"])
def test_flip_contract_falsy_aliases_kill(env, helper, val, monkeypatch):
    """flip33/34: false/off/no stay kill-switch aliases after the flip."""
    monkeypatch.setenv(env, val)
    assert helper() is False
