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
from faultline.pipeline_v2.stage_6_9b_generated_strip import (
    GENERATED_CONTENT_ENV_FLAG,
    generated_content_marker_enabled,
)
from faultline.pipeline_v2.transport_handoff import (
    MEGA_DECOMP_ARM_ENV,
    mega_decomp_armed,
)

# (env-var name, helper) for every S*-pack flag flipped default OFF -> ON.
# it2 amendment: FAULTLINE_UF_DET_AGGREGATION was UN-flipped back to OFF
# after the corpus regression audit (4x WORSE — bare 'Manage <plural>'
# naming corpus-wide); it left this list and carries its own un-flip
# contract test below. 9 flags remain default ON.
_FLIPPED = [
    (DEGRADATION_STAMP_ENV, degradation_stamp_enabled),
    (OWNER_ORACLE_ENV, owner_oracle_enabled),
    (UF_REFINE_TOKEN_SCALE_ENV, _token_scale_enabled),
    (BATCH_CANON_ENV, batch_canon_enabled),
    (OVERTURN_ARBITER_ENV, overturn_arbiter_enabled),
    (APPROUTER_KEYLESS_ENV, approuter_keyless_enabled),
    (GO_EXTRACTION_ENV, go_extraction_enabled),
    (MEGA_DECOMP_ARM_ENV, mega_decomp_armed),
    (GENERATED_CONTENT_ENV_FLAG, generated_content_marker_enabled),
]

# Sanity: the S*-pack after the it2 un-flip (9 flags); no duplicate envs.
assert len(_FLIPPED) == 9
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


def test_det_aggregation_reflip_contract(monkeypatch):
    """SEMANTIC un-flip №2 (2026-07-22, KEY_SCHEMA 35): the pack-3
    re-flip is REVERSED — det-agg ON skips the 6.7d rewrite on the
    keyed channel (phase_finalize S2-Seg-A probe), neutralizing the
    6.7d-family pack-3 defaults; default OFF until the det×6.7d
    composition cycle. =1 still arms; =0 still kills (forever)."""
    monkeypatch.delenv(DET_AGGREGATION_ENV, raising=False)
    assert det_aggregation_enabled() is False
    monkeypatch.setenv(DET_AGGREGATION_ENV, "1")
    assert det_aggregation_enabled() is True
    monkeypatch.setenv(DET_AGGREGATION_ENV, "0")
    assert det_aggregation_enabled() is False
