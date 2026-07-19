"""S*-pack default-flip (KEY_SCHEMA 32) — per-flag inverted kill-switch units.

The 2026-07-19 S* strategy pack (plan
``docs/anchor-arc/flip-pack-s-strategy-20260718.md``) flips 10
previously-default-OFF flags to default ON, each flag/group in its own
commit, ONE KEY_SCHEMA bump (31 -> 32) riding the pack's first commit.
Each flipped helper must satisfy the flip contract:

  * env UNSET               ⇒ ``enabled()`` is True   (unset ≡ explicit-1)
  * ``<FLAG>=1``            ⇒ ``enabled()`` is True
  * ``<FLAG>=0``            ⇒ ``enabled()`` is False   (the kill-switch)
  * ``<FLAG>=false/off/no`` ⇒ ``enabled()`` is False   (kill-switch aliases)

One deterministic test per flag (no scans, no LLM). This is the inverted
kill-switch battery the flip-protocol requires: proving unset now behaves
like explicit-1 AND that the X=0 disable still holds for every flag.

``_FLIPPED`` grows with each flip commit of the pack — the module-level
count assertion documents how many pack flags have landed so far.
"""

from __future__ import annotations

import pytest

from faultline.pipeline_v2.degradations import (
    DEGRADATION_STAMP_ENV,
    degradation_stamp_enabled,
)
from faultline.pipeline_v2.owner_oracle import (
    OWNER_ORACLE_ENV,
    owner_oracle_enabled,
)
from faultline.pipeline_v2.stage_6_7a_det_aggregation import (
    DET_AGGREGATION_ENV,
    det_aggregation_enabled,
)
from faultline.pipeline_v2.stage_6_7b_uf_refiner import (
    UF_REFINE_TOKEN_SCALE_ENV,
    _token_scale_enabled,
)
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    BATCH_CANON_ENV,
    batch_canon_enabled,
)
from faultline.pipeline_v2.overturn_ledger import (
    OVERTURN_ARBITER_ENV,
    overturn_arbiter_enabled,
)
from faultline.pipeline_v2.extractors.approuter_keyless import (
    APPROUTER_KEYLESS_ENV,
    approuter_keyless_enabled,
)
from faultline.pipeline_v2.extractors.go_router import (
    GO_EXTRACTION_ENV,
    go_extraction_enabled,
)

# (env-var name, helper) for every S*-pack flag flipped default OFF -> ON.
_FLIPPED = [
    (DEGRADATION_STAMP_ENV, degradation_stamp_enabled),
    (OWNER_ORACLE_ENV, owner_oracle_enabled),
    (DET_AGGREGATION_ENV, det_aggregation_enabled),
    (UF_REFINE_TOKEN_SCALE_ENV, _token_scale_enabled),
    (BATCH_CANON_ENV, batch_canon_enabled),
    (OVERTURN_ARBITER_ENV, overturn_arbiter_enabled),
    (APPROUTER_KEYLESS_ENV, approuter_keyless_enabled),
    (GO_EXTRACTION_ENV, go_extraction_enabled),
]

# Sanity: grows commit-by-commit with the pack; no duplicate env names.
assert len(_FLIPPED) == 8
assert len({env for env, _ in _FLIPPED}) == len(_FLIPPED)


@pytest.mark.parametrize(
    "env,helper", _FLIPPED, ids=[env for env, _ in _FLIPPED],
)
def test_flip_contract_unset_is_on(env, helper, monkeypatch):
    """flip32: UNSET ⇒ enabled (default flipped ON)."""
    monkeypatch.delenv(env, raising=False)
    assert helper() is True


@pytest.mark.parametrize(
    "env,helper", _FLIPPED, ids=[env for env, _ in _FLIPPED],
)
def test_flip_contract_explicit_one_is_on(env, helper, monkeypatch):
    """flip32: <FLAG>=1 ⇒ enabled (unset ≡ explicit-1)."""
    monkeypatch.setenv(env, "1")
    assert helper() is True


@pytest.mark.parametrize(
    "env,helper", _FLIPPED, ids=[env for env, _ in _FLIPPED],
)
def test_flip_contract_zero_is_kill_switch(env, helper, monkeypatch):
    """flip32: <FLAG>=0 ⇒ disabled (the kill-switch survives the flip)."""
    monkeypatch.setenv(env, "0")
    assert helper() is False


@pytest.mark.parametrize(
    "env,helper", _FLIPPED, ids=[env for env, _ in _FLIPPED],
)
@pytest.mark.parametrize("val", ["false", "off", "no"])
def test_flip_contract_falsy_aliases_kill(env, helper, val, monkeypatch):
    """flip32: false/off/no stay kill-switch aliases after the flip."""
    monkeypatch.setenv(env, val)
    assert helper() is False
