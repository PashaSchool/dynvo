"""B62 default-flip — per-flag inverted kill-switch units.

The B62 sprint flips 14 previously-default-OFF flags to default ON in one
commit (KEY_SCHEMA_VERSION 29; ``scan_meta.key_schema=29`` emitted). Each
helper must now satisfy the flip contract:

  * env UNSET               ⇒ ``enabled()`` is True   (unset ≡ explicit-1)
  * ``<FLAG>=1``            ⇒ ``enabled()`` is True
  * ``<FLAG>=0``            ⇒ ``enabled()`` is False   (the kill-switch)

One deterministic test per flag (no scans, no LLM). This is the inverted
kill-switch battery the flip-protocol requires: proving unset now behaves
like explicit-1 AND that the X=0 disable still holds for every flag.
"""

from __future__ import annotations

import pytest

from faultline.pipeline_v2.artifact_ink import (
    ARTIFACT_INK_ENV,
    artifact_ink_enabled,
)
from faultline.pipeline_v2.e2e_truth import (
    KEYLESS_JOURNEY_RECALL_ENV,
    keyless_journey_recall_enabled,
)
from faultline.pipeline_v2.fullname_expand import (
    FULLNAME_LAW_ENV,
    pf_fullname_law_enabled,
)
from faultline.pipeline_v2.naming_contract import (
    UF_NAME_DEGRIME_ENV,
    UF_RESOURCE_RUNG_ENV,
    UF_RUNG_SOURCES_V2_ENV,
    UF_VERB_SNAP_ENV,
    uf_name_degrime_enabled,
    uf_resource_rung_enabled,
    uf_rung_sources_v2_enabled,
    uf_verb_snap_enabled,
)
from faultline.pipeline_v2.profiles.next_pages_react import (
    ROUTER_ALIAS_RESOLVE_ENV,
    router_alias_resolve_enabled,
)
from faultline.pipeline_v2.profiles.react_router_fw import (
    REACT_ROUTER_FW_PROFILE_ENV,
    react_router_fw_profile_enabled,
)
from faultline.pipeline_v2.stage_6_7e_adjudicator import (
    ENV_FLAG as ADJUDICATOR_ENV,
)
from faultline.pipeline_v2.stage_6_7e_adjudicator import (
    adjudicator_6_7e_enabled,
)
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    ANNEXATION_GUARD_ENV,
    annexation_guard_enabled,
)
from faultline.pipeline_v2.technology_instruments import (
    WS_LIBRARY_LANE_ENV,
    ws_library_lane_enabled,
)
from faultline.pipeline_v2.transport_handoff import (
    FLOWFUL_TRANSPORT_LANE_ENV,
    flowful_transport_lane_enabled,
)
from faultline.pipeline_v2.ws_blob_domain_drain import (
    WS_BLOB_DRAIN_ENV,
    ws_blob_domain_drain_enabled,
)

# (env-var name, helper) for every flag flipped default OFF -> ON in B62.
# ADJUDICATOR_ENV is the adjudicator module's ``ENV_FLAG`` (aliased on import).
_FLIPPED = [
    (REACT_ROUTER_FW_PROFILE_ENV, react_router_fw_profile_enabled),
    (ROUTER_ALIAS_RESOLVE_ENV, router_alias_resolve_enabled),
    (KEYLESS_JOURNEY_RECALL_ENV, keyless_journey_recall_enabled),
    (WS_LIBRARY_LANE_ENV, ws_library_lane_enabled),
    (UF_NAME_DEGRIME_ENV, uf_name_degrime_enabled),
    (UF_RESOURCE_RUNG_ENV, uf_resource_rung_enabled),
    (UF_RUNG_SOURCES_V2_ENV, uf_rung_sources_v2_enabled),
    (UF_VERB_SNAP_ENV, uf_verb_snap_enabled),
    (FLOWFUL_TRANSPORT_LANE_ENV, flowful_transport_lane_enabled),
    (WS_BLOB_DRAIN_ENV, ws_blob_domain_drain_enabled),
    (FULLNAME_LAW_ENV, pf_fullname_law_enabled),
    (ADJUDICATOR_ENV, adjudicator_6_7e_enabled),
    (ANNEXATION_GUARD_ENV, annexation_guard_enabled),
    (ARTIFACT_INK_ENV, artifact_ink_enabled),
]

# Sanity: exactly the 14 flipped flags, no duplicate env names.
assert len(_FLIPPED) == 14
assert len({env for env, _ in _FLIPPED}) == 14


@pytest.mark.parametrize(
    "env,helper", _FLIPPED, ids=[env for env, _ in _FLIPPED],
)
def test_flip_contract_unset_is_on(env, helper, monkeypatch):
    """B62: UNSET ⇒ enabled (default flipped ON)."""
    monkeypatch.delenv(env, raising=False)
    assert helper() is True


@pytest.mark.parametrize(
    "env,helper", _FLIPPED, ids=[env for env, _ in _FLIPPED],
)
def test_flip_contract_explicit_one_is_on(env, helper, monkeypatch):
    """B62: <FLAG>=1 ⇒ enabled (unset ≡ explicit-1)."""
    monkeypatch.setenv(env, "1")
    assert helper() is True


@pytest.mark.parametrize(
    "env,helper", _FLIPPED, ids=[env for env, _ in _FLIPPED],
)
def test_flip_contract_zero_is_kill_switch(env, helper, monkeypatch):
    """B62: <FLAG>=0 ⇒ disabled (the kill-switch survives the flip)."""
    monkeypatch.setenv(env, "0")
    assert helper() is False
