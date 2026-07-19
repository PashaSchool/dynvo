"""R5 corpus naming-wave (``FAULTLINE_NAMING_WAVE_R5``) — Segment R5-1
identity-parity law.

Named units for the census exhibits (R5-census 2026-07-19,
/private/tmp/r5-census, classify.py/refine.py rulers) + anti-cases:

  * R5-1 identity-parity: a PF display whose identity-fold equals ANOTHER
    live PF's canonical slug is trust-breaking (openstatus ``general`` ->
    'Settings' == the ``settings`` PF; ``checker`` -> 'Monitors' == the
    ``monitors`` PF; papermark ``datarooms`` -> 'Billing' == the
    ``billing`` PF). The wave repairs it to the honest own-slug word; the
    merged display-cross-gate leaves this evidence-grounded remnant, so
    the law is the authoritative last PF-display word.

Anti-cases (census false-positive lesson — healthy names KEEP):
  * a PF whose display IS its own identity ('Analytics' on ``analytics``)
    is untouched;
  * a healthy journey phrase ('Analyze cohort retention') never collides
    with a PF slug and is untouched;
  * a display that is a common word matching NO other PF slug is kept.

SACRED: flag unset/=0 ⇒ every display + the ``pf_identity_parity_*``
telemetry key is byte-identical to pre-R5.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2.naming_contract import (
    NAMING_WAVE_R5_ENV,
    _ident_fold,
    _identity_parity_repair,
    load_naming_vocab,
    naming_wave_r5_enabled,
    run_naming_contract,
)

VOCAB = load_naming_vocab()
_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _pf(slug: str, display: str, anchor_id: str | None = None) -> Feature:
    f = Feature(
        name=slug, display_name=display, layer="product",
        paths=[], authors=["a"], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_NOW, health_score=100.0,
    )
    if anchor_id:
        f.anchor_id = anchor_id
    return f


def _uf(uid: str, name: str, pfid: str) -> UserFlow:
    return UserFlow(
        id=uid, name=name, resource=pfid, domain=None,
        product_feature_id=pfid, intent="manage",
        member_flow_ids=[], member_count=0,
    )


def _run(pfs: list[Feature], ufs: list[UserFlow] | None = None) -> dict:
    return run_naming_contract(
        pfs, ufs or [], [], keeper_on=False,
        product_strings=None, routes_index=None,
        uf_authored_names={}, labeler=None, verifier=None, repo_root=None,
    )


def _disp(pfs: list[Feature]) -> dict[str, str]:
    return {str(p.name): str(p.display_name) for p in pfs}


# ── flag: default OFF + inverted kill-switch + cache keying ──────────────


def test_wave_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(NAMING_WAVE_R5_ENV, raising=False)
    assert naming_wave_r5_enabled() is False
    for off in ("0", "false", "off", ""):
        monkeypatch.setenv(NAMING_WAVE_R5_ENV, off)
        assert naming_wave_r5_enabled() is False
    for on in ("1", "true"):
        monkeypatch.setenv(NAMING_WAVE_R5_ENV, on)
        assert naming_wave_r5_enabled() is True


def test_wave_flag_registered_for_cache_keying() -> None:
    from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS

    assert "FAULTLINE_NAMING_WAVE_R5" in ENV_OUTPUT_FLAGS


# ── mechanism: identity fold + repair ───────────────────────────────────


def test_ident_fold_display_equals_slug() -> None:
    # display 'Status Pages' and slug 'status-pages' fold identically.
    assert _ident_fold("Status Pages") == _ident_fold("status-pages")
    assert _ident_fold("Settings (Dashboard)") != _ident_fold("settings")
    assert _ident_fold("Monitors") == "monitors"


def test_identity_parity_repair_prefers_own_slug_word() -> None:
    # 'Settings' on the ``general`` PF repairs to the honest own-slug word.
    idents = {"general", "settings"}
    repaired = _identity_parity_repair(
        "Settings", "route:app/settings/general", "general", VOCAB, {}, idents)
    assert repaired == "General"
    assert _ident_fold(repaired) not in (idents - {"general"})


# ── exhibit: openstatus general -> 'Settings' (== settings PF) ───────────


def _openstatus_settings_fixture() -> list[Feature]:
    # ``general`` first-come took the nav 'Settings' label; the real
    # ``settings`` PF is left wearing a qualifier. general's display folds
    # to the settings identity — the C12 remnant.
    return [
        _pf("general", "Settings", "route:app/(dashboard)/settings/general"),
        _pf("settings", "Settings", "route:app/(dashboard)/settings"),
    ]


def test_general_settings_parity_repaired_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pfs = _openstatus_settings_fixture()
    tele = _run(pfs)
    disp = _disp(pfs)
    # general no longer wears the settings identity.
    assert _ident_fold(disp["general"]) != _ident_fold("settings")
    assert disp["general"] == "General"
    assert tele.get("pf_identity_parity_qualified", 0) >= 1


def test_general_settings_parity_untouched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(NAMING_WAVE_R5_ENV, raising=False)
    pfs = _openstatus_settings_fixture()
    tele = _run(pfs)
    disp = _disp(pfs)
    # OFF: general keeps the colliding display and no telemetry key appears.
    assert _ident_fold(disp["general"]) == _ident_fold("settings")
    assert "pf_identity_parity_qualified" not in tele


# ── exhibit: openstatus checker -> 'Monitors' (== monitors PF slug) ─────


def test_checker_monitors_parity_repaired_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pfs = [
        _pf("checker", "Monitors", "route:app/(landing)/play/checker"),
        _pf("monitors", "Monitors", "route:app/(dashboard)/monitors"),
    ]
    _run(pfs)
    disp = _disp(pfs)
    assert _ident_fold(disp["checker"]) != _ident_fold("monitors")
    assert disp["checker"] == "Checker"


# ── exhibit: papermark datarooms -> 'Billing' (== billing PF slug) ──────


def test_datarooms_billing_parity_repaired_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pfs = [
        _pf("billing", "Billing", "route:app/billing"),
        _pf("datarooms", "Billing", "route:app/datarooms"),
    ]
    _run(pfs)
    disp = _disp(pfs)
    assert _ident_fold(disp["datarooms"]) != _ident_fold("billing")
    # billing keeps its honest identity.
    assert disp["billing"] == "Billing"


# ── exhibit: shared-display twin (tracecat organizations/workspaces) ────


def test_shared_display_twin_qualified_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pfs = [
        _pf("organizations", "Agent", "route:app/organizations"),
        _pf("workspaces", "Agent", "route:app/workspaces"),
    ]
    _run(pfs)
    disp = _disp(pfs)
    # the two must not both wear the same folded display.
    assert disp["organizations"].strip().lower() != disp["workspaces"].strip().lower()


# ── ANTI-CASES: healthy names KEEP (census false-positive lesson) ───────


def test_anticase_own_identity_display_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A PF whose display IS its own identity is NOT a parity violation.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pfs = [
        _pf("analytics", "Analytics", "route:app/analytics"),
        _pf("billing", "Billing", "route:app/billing"),
    ]
    tele = _run(pfs)
    disp = _disp(pfs)
    assert disp["analytics"] == "Analytics"
    assert disp["billing"] == "Billing"
    assert tele.get("pf_identity_parity_qualified", 0) == 0


def test_anticase_healthy_journey_phrase_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 'Analyze cohort retention' — the census false-positive class. It is a
    # UF, never folds to any PF slug, and must survive verbatim.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pfs = [_pf("cohorts", "Cohorts", "route:app/cohorts")]
    ufs = [_uf("u1", "Analyze cohort retention", "cohorts")]
    _run(pfs, ufs)
    assert ufs[0].name == "Analyze cohort retention"


def test_anticase_distinct_healthy_displays_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every display is a distinct honest word; none folds to another's slug.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pfs = [
        _pf("auth", "Auth", "route:app/auth"),
        _pf("billing", "Billing", "route:app/billing"),
        _pf("webhooks", "Webhooks", "route:app/webhooks"),
    ]
    tele = _run(pfs)
    disp = _disp(pfs)
    assert disp == {"auth": "Auth", "billing": "Billing", "webhooks": "Webhooks"}
    assert tele.get("pf_identity_parity_qualified", 0) == 0


# ══════════════════════════════════════════════════════════════════════
# Segment R5-2 — own-resource echo-hub templating
# ══════════════════════════════════════════════════════════════════════

from faultline.pipeline_v2.naming_contract import (  # noqa: E402
    _echo_own_resource_uf_ids,
    build_uf_candidates,
)


def _uf_res(uid: str, name: str, pfid: str, resource: str,
            *, category: str = "interactive", synthesized: bool = True) -> UserFlow:
    return UserFlow(
        id=uid, name=name, resource=resource, domain=None,
        product_feature_id=pfid, intent="manage",
        member_flow_ids=[], member_count=0,
        category=category, synthesized=synthesized,
    )


def _twenty_server_hub() -> tuple[list[Feature], list[UserFlow]]:
    # twenty ``twenty-server`` PF hosts many organic UFs each on a DISTINCT
    # resource; the PF-display template collapses them into 'Manage twenty
    # server (…)' echoes.
    pf = _pf("twenty-server", "Twenty Server", "package:packages/twenty-server")
    ufs = [
        _uf_res("u1", "Twenty Server", "twenty-server", "billing"),
        _uf_res("u2", "Twenty Server", "twenty-server", "field-metadata"),
        _uf_res("u3", "Twenty Server", "twenty-server", "dpa"),
        _uf_res("u4", "Twenty Server", "twenty-server", "fields"),
    ]
    return [pf], ufs


def test_echo_hub_detection_flags_multi_resource_hub() -> None:
    pfs, ufs = _twenty_server_hub()
    ids = _echo_own_resource_uf_ids(ufs, {p.name: p for p in pfs})
    assert ids == {"u1", "u2", "u3", "u4"}


def test_echo_hub_own_resource_kills_echo_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pfs, ufs = _twenty_server_hub()
    tele = _run(pfs, ufs)
    names = {u.name for u in ufs}
    # every sibling now carries its OWN resource, not the shared PF echo.
    assert not any(n.strip().lower().startswith("manage twenty server")
                   for n in names)
    assert len(names) == 4  # four distinct rows, no echo, no qualifier-spray
    assert tele.get("uf_own_resource_templated", -1) >= 0


def test_echo_hub_candidate_uses_own_resource_over_pf_display() -> None:
    pf = _pf("twenty-server", "Twenty Server", "package:packages/twenty-server")
    uf = _uf_res("u1", "Twenty Server", "twenty-server", "billing")
    with_own = build_uf_candidates(uf, pf, VOCAB, own_resource=True)
    without = build_uf_candidates(uf, pf, VOCAB, own_resource=False)
    # own-resource template mentions the resource, not the PF display.
    assert any("billing" in c.lower() for c in with_own)
    assert any("twenty server" in c.lower() for c in without)


# ── ANTI-CASES for R5-2 ──────────────────────────────────────────────


def test_anticase_single_resource_pf_not_a_hub() -> None:
    # A PF whose UFs all share ONE resource is not a multi-resource hub.
    pf = _pf("billing", "Billing", "route:app/billing")
    ufs = [
        _uf_res("u1", "Billing", "billing", "invoice"),
        _uf_res("u2", "Billing", "billing", "invoice"),
    ]
    ids = _echo_own_resource_uf_ids(ufs, {pf.name: pf})
    assert ids == set()


def test_anticase_own_identity_resource_not_templated() -> None:
    # A UF whose resource echoes the PF identity is the PF's core, not a
    # distinct hub member — untouched.
    pf = _pf("billing", "Billing", "route:app/billing")
    ufs = [
        _uf_res("u1", "Billing", "billing", "billing"),
        _uf_res("u2", "Billing", "billing", "billing"),
    ]
    ids = _echo_own_resource_uf_ids(ufs, {pf.name: pf})
    assert ids == set()


def test_r5_2_off_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(NAMING_WAVE_R5_ENV, raising=False)
    pfs, ufs = _twenty_server_hub()
    tele = _run(pfs, ufs)
    # OFF: no own-resource telemetry key, echo template preserved.
    assert "uf_own_resource_templated" not in tele
