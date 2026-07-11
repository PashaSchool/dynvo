"""B46 — UF-name hygiene: kill three garbage-name sources (flag default OFF).

FAULTLINE_UF_NAME_HYGIENE arms:
  1. concat root fix — a component file-stem that camel-restates its directory
     (``settings/accounts/SettingsAccounts``) doubles the token run; collapsed
     in flow_name_v2._resource_tokens. Reproduced from the twenty keyless scan
     ('account settingsaccounts' UF, member 'view-settings-accounts-settings-
     accounts-flow').
  2. bare pluralized leaf — an ungrounded 'other'-intent slot rendered the bare
     "{r}" template as a naked plural dir stem ('onboardings'); now
     'Manage onboarding' (manage-X rule); confidence stays low.
  3. inherited ordinal — a UF label inheriting a Stage-5.5 '-N' ordinal
     ('Browse available model action 3', typebot); stripped at the UF
     derivation; flow-level ordinal (a legal slug) untouched.

OFF ⇒ flow + UF names byte-identical to pre-B46.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from faultline.models.types import Flow, FlowEntryPoint
from faultline.pipeline_v2.flow_name_v2 import (
    UF_NAME_HYGIENE_ENV,
    _compose_name,
    _resource_tokens,
    _verb_for,
    apply_flow_name_v2,
    uf_name_hygiene_enabled,
)
from faultline.pipeline_v2.stage_6_7_user_flows import (
    _slot_consistent_label,
    cluster_user_flows,
)
from faultline.pipeline_v2.synth_quality import (
    BACKSTOP_REASON,
    reground_backstop_uf_names,
)

# The real twenty route shape that produced the concat.
_TWENTY_ENTRY = (
    "packages/twenty-front/src/pages/settings/accounts/SettingsAccounts.tsx"
)
_TWENTY_PATTERN = "/settings/accounts/SettingsAccounts"


# ── flag plumbing ────────────────────────────────────────────────────────

def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UF_NAME_HYGIENE_ENV, raising=False)
    assert uf_name_hygiene_enabled() is False
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    assert uf_name_hygiene_enabled() is True


# ── source 1 — concat root fix ────────────────────────────────────────────

def test_concat_root_off_is_byte_identical(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "0")
    toks = _resource_tokens(_TWENTY_PATTERN, _TWENTY_ENTRY)
    # pre-B46: the file-stem 'SettingsAccounts' -> 'settings-accounts' doubles
    # the dir tokens.
    assert toks == ["settings", "accounts", "settings-accounts"]
    assert _compose_name(_verb_for("PAGE", toks), toks) == (
        "view-settings-accounts-settings-accounts-flow"
    )


def test_concat_root_on_collapses_doubled_run(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    toks = _resource_tokens(_TWENTY_PATTERN, _TWENTY_ENTRY)
    assert toks == ["settings", "accounts"]
    assert _compose_name(_verb_for("PAGE", toks), toks) == (
        "view-settings-accounts-flow"
    )


def test_concat_anticase_partial_or_distinct_repeat_preserved(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    # distinct tokens (teams != team) — NOT a duplicate run, left intact.
    toks = _resource_tokens("/teams/TeamMembers", "app/teams/TeamMembers.tsx")
    assert toks == ["teams", "team-members"]
    # partial overlap with an intervening token (admin-panel) — conservatively
    # NOT collapsed (only an exact adjacent run is).
    toks2 = _resource_tokens(
        "/settings/admin-panel/SettingsAdmin",
        "app/settings/admin-panel/SettingsAdmin.tsx")
    assert toks2 == ["settings", "admin-panel", "settings-admin"]


def _flow(name: str, *, entry_file: str, description: str = "") -> Flow:
    now = datetime.now(timezone.utc)
    return Flow(
        name=name, description=description or None,
        entry_point_file=entry_file,
        entry_point=FlowEntryPoint(path=entry_file, symbol=None, line=1),
        paths=[entry_file], authors=["a"], total_commits=5, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=now, health_score=90.0,
        uuid=f"uuid-{name}", id=f"feat::{name}", primary_feature="feat",
        display_name=name,
        short_label=name[:-5] if name.endswith("-flow") else name)


def test_concat_end_to_end_flow_rename(monkeypatch) -> None:
    # Stage 3 mints the bare route echo; flow_name_v2's route arm adds the verb
    # + re-derives the resource. OFF => the file-stem restating the dir doubles
    # the run ('view-settings-accounts-settings-accounts-flow'); ON => clean.
    seed = "settings-accounts-settings-accounts-flow"

    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "0")
    fl_off = _flow(seed, entry_file=_TWENTY_ENTRY, description=_TWENTY_PATTERN)
    apply_flow_name_v2([fl_off], [], None)
    assert fl_off.name == "view-settings-accounts-settings-accounts-flow"

    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    fl_on = _flow(seed, entry_file=_TWENTY_ENTRY, description=_TWENTY_PATTERN)
    apply_flow_name_v2([fl_on], [], None)
    assert fl_on.name == "view-settings-accounts-flow"


# ── source 2 — bare pluralized leaf ('onboardings' -> 'Manage onboarding') ─

def _uf_flow(name: str, uuid: str, entry: str, paths: list[str]) -> dict:
    return {
        "name": name, "uuid": uuid, "entry_point_file": entry,
        "paths": paths, "primary_feature": None, "secondary_features": [],
        "test_files": [], "coverage_pct": None,
    }


def _bare_plural_scene() -> dict:
    # verb 'onboarding' is not a journey verb -> intent 'other'; the flow name
    # has no noun span (resource -> 'item') and there is no product-string
    # vocabulary, so _slot_consistent_label falls to the ungrounded path-3
    # basename stem 'onboarding' -> pluralized 'onboardings'.
    return {
        "flows": [_uf_flow("onboarding-flow", "a",
                           "src/pages/onboarding.tsx",
                           ["src/pages/onboarding.tsx"])],
        "developer_features": [],
    }


def test_bare_plural_off_ships_naked_plural(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "0")
    r = cluster_user_flows(_bare_plural_scene())
    uf = r["user_flows"][0]
    assert uf["intent"] == "other"
    assert uf["name"] == "onboardings"
    assert uf["name_confidence"] == "low"


def test_bare_plural_on_becomes_manage_singular(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    r = cluster_user_flows(_bare_plural_scene())
    uf = r["user_flows"][0]
    assert uf["name"] == "Manage onboarding"
    # confidence stays honest — the evidence is only a dir name.
    assert uf["name_confidence"] == "low"


def test_bare_plural_grounded_slot_untouched(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    # verb 'flag' -> intent 'other', but the flow HAS a noun span ('detector')
    # so the slot is GROUNDED (path-2) — the fix must not touch it.
    scene = {
        "flows": [_uf_flow("flag-detector-flow", "a",
                          "src/detectors.py", ["src/detectors.py"])],
        "developer_features": [],
    }
    r = cluster_user_flows(scene)
    uf = r["user_flows"][0]
    assert uf["intent"] == "other"
    assert uf["name"] == "detectors"  # grounded -> unchanged


# ── source 3 — inherited Stage-5.5 ordinal ────────────────────────────────

def test_ordinal_stripped_at_uf_label_off(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "0")
    members = [{"name": "fetch-available-model-action-3-flow",
                "entry_point_file": "m.py", "paths": ["m.py"]}]
    label, grounded = _slot_consistent_label(members)
    assert grounded is True
    assert "3" in label  # pre-B46: the ordinal leaks into the label


def test_ordinal_stripped_at_uf_label_on(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    members = [{"name": "fetch-available-model-action-3-flow",
                "entry_point_file": "m.py", "paths": ["m.py"]}]
    label, grounded = _slot_consistent_label(members)
    assert grounded is True
    assert "3" not in label
    assert label == "available model actions"


def test_ordinal_flow_name_itself_untouched(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    # the member flow name still carries its (legal) ordinal slug — only the
    # UF label derived FROM it is cleaned.
    members = [{"name": "fetch-available-model-action-3-flow",
                "entry_point_file": "m.py", "paths": ["m.py"]}]
    _slot_consistent_label(members)
    assert members[0]["name"] == "fetch-available-model-action-3-flow"


# ── source 3 — inherited ordinal at the synth_quality single-member path ──

def _sq_uf(uid, name, *, members, pf):
    return SimpleNamespace(
        id=uid, name=name, synthesis_reason=BACKSTOP_REASON,
        member_flow_ids=list(members), member_count=len(members),
        synthesized=True, product_feature_id=pf, resource=None,
        routes=[], category="interactive", trigger=None,
        name_confidence="low")


def _sq_flow(uuid, name):
    return SimpleNamespace(uuid=uuid, name=name, short_label=name,
                           user_flow_id=None)


def test_synth_single_member_ordinal_off_leaks(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "0")
    ufs = [_sq_uf("UF-1", "Manage widgets", members=["m1"], pf="widgets")]
    flows = [_sq_flow("m1", "configure-webhook-endpoints-3-flow")]
    reground_backstop_uf_names(ufs, flows,
                               [SimpleNamespace(id="widgets", name="widgets",
                                                display_name="Widgets")], {})
    assert ufs[0].name == "Configure webhook endpoints 3"


def test_synth_single_member_ordinal_on_stripped(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    ufs = [_sq_uf("UF-1", "Manage widgets", members=["m1"], pf="widgets")]
    flows = [_sq_flow("m1", "configure-webhook-endpoints-3-flow")]
    reground_backstop_uf_names(ufs, flows,
                               [SimpleNamespace(id="widgets", name="widgets",
                                                display_name="Widgets")], {})
    assert ufs[0].name == "Configure webhook endpoints"
