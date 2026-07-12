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
    _collapse_glued_echo,
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

def test_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default ON since the 2026-07-12 flip (KEY_SCHEMA v27); =0 is the
    # explicit kill-switch.
    monkeypatch.delenv(UF_NAME_HYGIENE_ENV, raising=False)
    assert uf_name_hygiene_enabled() is True
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    assert uf_name_hygiene_enabled() is True
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "0")
    assert uf_name_hygiene_enabled() is False
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "false")
    assert uf_name_hygiene_enabled() is False


def test_unset_equals_explicit_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inverted kill-switch (post-flip): UNSET behaves identically to an
    explicit ``1`` on the synthetic scenes (flow root + UF label sites)."""
    def _snapshot() -> tuple:
        toks = _resource_tokens(_TWENTY_PATTERN, _TWENTY_ENTRY)
        label = _slot_consistent_label(_twenty_uf_side_members())
        scene = {
            "flows": [_uf_flow(_TWENTY_SEED, "a", _TWENTY_ENTRY,
                               [_TWENTY_ENTRY])],
            "developer_features": [],
        }
        uf = cluster_user_flows(scene)["user_flows"][0]
        return toks, label, uf["name"], uf["name_confidence"]

    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    explicit = _snapshot()
    monkeypatch.delenv(UF_NAME_HYGIENE_ENV, raising=False)
    assert _snapshot() == explicit


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


# ── source 1b (iteration 2, live twenty wave) — GLUED seed on the UF side ──
#
# The live ON-scan proved a SECOND independent producer: Stage 6.7 derives UF
# labels BEFORE flow_name_v2 renames flows, so the label reads the Stage-3
# PLAIN-slug seed name 'settings-accounts-settingsaccounts-flow' (flow id
# settings::settings-accounts-settingsaccounts-flow) — the stem glues with NO
# camel split, _split_name eats 'settings' as the verb, and path-2 renders
# 'account settingsaccounts' (twenty UF-003, survives the flow-level fix).

_TWENTY_SEED = "settings-accounts-settingsaccounts-flow"


def test_glued_echo_collapse_exact_live_seed() -> None:
    assert _collapse_glued_echo(_TWENTY_SEED) == "settings-accounts-flow"
    # glued stem + real leaf: prefix strips, the leaf survives.
    assert _collapse_glued_echo(
        "settings-accounts-settingsaccountsemails-flow"
    ) == "settings-accounts-emails-flow"


def test_glued_echo_anticases_untouched() -> None:
    # partial restatement is NOT an exact concat — never collapses.
    assert _collapse_glued_echo("teams-teammembers-flow") == (
        "teams-teammembers-flow")
    # live T3 anti-case: a ONE-token character prefix is linguistic, not
    # structural — 'auth-authorize' must NOT strip to 'orize' (a proper-prefix
    # match requires the duplicated run to span >=2 preceding tokens; a single
    # preceding token counts only on EXACT equality).
    assert _collapse_glued_echo("auth-authorize-flow") == "auth-authorize-flow"
    assert _collapse_glued_echo("view-auth-authorize-flow") == (
        "view-auth-authorize-flow")
    # exact single-token dup (pure restatement) still collapses.
    assert _collapse_glued_echo("settings-settings-flow") == "settings-flow"
    # genuine 2-token glue with empty remainder (live 'found notfounds' row).
    assert _collapse_glued_echo("not-found-notfound-flow") == "not-found-flow"
    # non-adjacent repeat (intervening token) — never collapses.
    assert _collapse_glued_echo(
        "settings-accounts-stories-settingsaccounts-stories-flow"
    ) == "settings-accounts-stories-settingsaccounts-stories-flow"
    # clean names pass through byte-identically.
    assert _collapse_glued_echo("view-settings-accounts-flow") == (
        "view-settings-accounts-flow")
    assert _collapse_glued_echo("create-detector-flow") == (
        "create-detector-flow")


def _twenty_uf_side_members() -> list[dict]:
    # the exact live shape: the primary member still wears its Stage-3 seed
    # name at UF-derivation time (multiple paths make it primary).
    return [{
        "name": _TWENTY_SEED,
        "entry_point_file": _TWENTY_ENTRY,
        "paths": [_TWENTY_ENTRY, "packages/twenty-front/src/x.ts",
                  "packages/twenty-front/src/y.ts"],
    }]


def test_uf_side_glued_label_off_reproduces_live_garbage(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "0")
    label, grounded = _slot_consistent_label(_twenty_uf_side_members())
    assert (label, grounded) == ("account settingsaccounts", True)


def test_uf_side_glued_label_on_clean(monkeypatch) -> None:
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    label, grounded = _slot_consistent_label(_twenty_uf_side_members())
    assert (label, grounded) == ("accounts", True)
    # the member flow's own (seed) name is untouched — flow_name_v2 owns the
    # flow-level rename later in the pipeline.
    members = _twenty_uf_side_members()
    _slot_consistent_label(members)
    assert members[0]["name"] == _TWENTY_SEED


def test_uf_side_glued_end_to_end_cluster(monkeypatch) -> None:
    # full Stage-6.7 cluster pass over the live seed shape: OFF mints the
    # garbage UF name, ON mints the clean grounded label.
    scene = {
        "flows": [_uf_flow(_TWENTY_SEED, "a", _TWENTY_ENTRY,
                           [_TWENTY_ENTRY])],
        "developer_features": [],
    }
    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "0")
    off = cluster_user_flows(scene)["user_flows"][0]
    assert off["name"] == "account settingsaccounts"

    monkeypatch.setenv(UF_NAME_HYGIENE_ENV, "1")
    on = cluster_user_flows(scene)["user_flows"][0]
    assert on["name"] == "accounts"
    assert on["name_confidence"] == off["name_confidence"]  # confidence-neutral
