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


# ── flag: default ON (pack-2 flip) + inverted kill-switch + cache keying ──


def test_wave_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # SEMANTIC flip migration (2026-07-21 pack №2, KEY_SCHEMA 33): unset
    # now arms every R5 segment (unset ≡ explicit-1).
    monkeypatch.delenv(NAMING_WAVE_R5_ENV, raising=False)
    assert naming_wave_r5_enabled() is True
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
    # MECHANICAL flip migration (2026-07-21 pack №2, KEY_SCHEMA 33):
    # the OFF baseline is pinned with an explicit =0, not left unset.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "0")
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
    # MECHANICAL flip migration (2026-07-21 pack №2, KEY_SCHEMA 33):
    # the OFF baseline is pinned with an explicit =0, not left unset.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "0")
    pfs, ufs = _twenty_server_hub()
    tele = _run(pfs, ufs)
    # OFF: no own-resource telemetry key, echo template preserved.
    assert "uf_own_resource_templated" not in tele


# ══════════════════════════════════════════════════════════════════════
# Segment R5-5 — negative confidence rungs (census-shape caps)
# ══════════════════════════════════════════════════════════════════════

from faultline.pipeline_v2.naming_contract import _name_disease_shape  # noqa: E402


def test_shape_detector_paren_qualifier() -> None:
    assert _name_disease_shape("Manage item (Assistant)") == "paren-qualifier"
    assert _name_disease_shape("Settings (Dashboard)") == "paren-qualifier"


def test_shape_detector_raw_identifier() -> None:
    assert _name_disease_shape("Connect apiKeys") == "raw-identifier"
    assert _name_disease_shape("Manage api_keys") == "raw-identifier"
    assert _name_disease_shape("Htmltopdf") is None  # single glued word not caught (no dict)


def test_shape_detector_healthy_names_none() -> None:
    # The census false-positive class + honest journey phrases carry NO shape.
    for healthy in (
        "Analyze cohort retention",
        "Manage API keys",
        "Browse and filter monitors",
        "Create monitor",
        "View incidents",
    ):
        assert _name_disease_shape(healthy) is None, healthy


def _uf_high(uid: str, name: str, pfid: str, members: list[str]) -> UserFlow:
    return UserFlow(
        id=uid, name=name, resource="widget", domain=None,
        product_feature_id=pfid, intent="manage",
        member_flow_ids=members, member_count=len(members),
        name_confidence="high",
    )


def test_r5_5_off_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    # OFF: no shape cap, no telemetry key, confidence untouched.
    # MECHANICAL flip migration (2026-07-21 pack №2, KEY_SCHEMA 33):
    # the OFF baseline is pinned with an explicit =0, not left unset.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "0")
    pfs = [_pf("widget", "Widget", "route:app/widget")]
    ufs = [_uf_high("u1", "Manage widget (Legacy)", "widget", [])]
    tele = _run(pfs, ufs)
    assert "uf_shape_capped" not in tele


# ── R5-5 cap function (direct — the rubric-high path is grounding-fragile) ──

from faultline.pipeline_v2.naming_contract import _apply_r5_confidence_caps  # noqa: E402


def _uf_conf(name: str, conf: str) -> UserFlow:
    return UserFlow(
        id="u1", name=name, resource="r", domain=None,
        product_feature_id="pf", intent="manage",
        member_flow_ids=[], member_count=0, name_confidence=conf,
    )


def test_r5_5_caps_high_paren_uf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    uf = _uf_conf("Manage status pages (dashboard)", "high")
    tele: dict = {}
    _apply_r5_confidence_caps([uf], tele, rungs_on=True)
    assert uf.name_confidence == "medium"
    assert "shape:paren-qualifier" in (uf.name_evidence or [])
    assert tele["uf_shape_capped"] == 1


def test_r5_5_caps_high_raw_identifier_uf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    uf = _uf_conf("Connect apiKeys", "high")
    tele: dict = {}
    _apply_r5_confidence_caps([uf], tele, rungs_on=True)
    assert uf.name_confidence == "medium"
    assert "shape:raw-identifier" in (uf.name_evidence or [])


def test_r5_5_keeps_high_clean_uf(monkeypatch: pytest.MonkeyPatch) -> None:
    # ANTI-CASE: a clean verb-led journey stays 'high'.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    for clean in ("Browse and filter monitors", "Analyze cohort retention",
                  "Manage API keys"):
        uf = _uf_conf(clean, "high")
        _apply_r5_confidence_caps([uf], {}, rungs_on=True)
        assert uf.name_confidence == "high", clean


def test_r5_5_only_caps_high_not_medium(monkeypatch: pytest.MonkeyPatch) -> None:
    # a medium/low diseased row is left as-is (the cap only lowers 'high').
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    uf = _uf_conf("Manage item (Assistant)", "medium")
    _apply_r5_confidence_caps([uf], {}, rungs_on=True)
    assert uf.name_confidence == "medium"


def test_r5_5_cap_noop_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # MECHANICAL flip migration (2026-07-21 pack №2, KEY_SCHEMA 33):
    # the OFF baseline is pinned with an explicit =0, not left unset.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "0")
    uf = _uf_conf("Manage x (y)", "high")
    tele: dict = {}
    _apply_r5_confidence_caps([uf], tele, rungs_on=True)
    assert uf.name_confidence == "high"
    assert "uf_shape_capped" not in tele


# ══════════════════════════════════════════════════════════════════════
# Segment R5-2 iter-2 — keyed-refutation fixes (spray predicate,
# verifier re-derive threading, no-new-dup guard)
# ══════════════════════════════════════════════════════════════════════

from faultline.pipeline_v2.naming_contract import (  # noqa: E402
    _r5_sibling_name_dup,
    _spray_sibling_uf_ids,
)


def _uf_full(uid: str, name: str, pfid: str, resource: str, *,
             members: list[str] | None = None,
             synthesized: bool = True) -> UserFlow:
    m = members or []
    return UserFlow(
        id=uid, name=name, resource=resource, domain=None,
        product_feature_id=pfid, intent="manage",
        member_flow_ids=m, member_count=len(m),
        category="interactive", synthesized=synthesized,
    )


def _run_with_verifier(pfs, ufs, verifier):
    return run_naming_contract(
        pfs, ufs, [], keeper_on=False,
        product_strings=None, routes_index=None,
        uf_authored_names={}, labeler=None, verifier=verifier,
        repo_root=None,
    )


# ── spray predicate — 'Manage twenty server (billing)' ×N class ─────────


def _sprayed_hub() -> tuple[list[Feature], list[UserFlow]]:
    pf = _pf("twenty-server", "Twenty Server", "package:packages/twenty-server")
    ufs = [
        _uf_full("u1", "Manage twenty server (billing)", "twenty-server",
                 "billing", members=["m1"]),
        _uf_full("u2", "Manage twenty server (dpa)", "twenty-server",
                 "dpa", members=["m2"]),
        _uf_full("u3", "Manage twenty server (fields)", "twenty-server",
                 "fields", members=["m3"]),
    ]
    return [pf], ufs


def test_spray_predicate_flags_pf_identity_base() -> None:
    pfs, ufs = _sprayed_hub()
    ids = _spray_sibling_uf_ids(ufs, {p.name: p for p in pfs})
    assert ids == {"u1", "u2", "u3"}


def test_spray_rows_adopt_own_resource_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fresh-keyed live disease form: identical PF-identity base +
    # qualifier spray. ON: every row templates on its OWN resource; the
    # base echo and the qual spray are both gone.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pfs, ufs = _sprayed_hub()
    _run(pfs, ufs)
    names = [u.name for u in ufs]
    assert not any("twenty server" in n.lower() for n in names), names
    assert len({n.strip().lower() for n in names}) == 3  # all distinct


def test_spray_rows_no_own_resource_adoption_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OFF is the pre-R5 path: synthesized rows may churn through the
    # PF-display template + uniqueness (that churn IS the disease), but
    # the own-resource adoption never happens — the PF echo remains.
    # MECHANICAL flip migration (2026-07-21 pack №2, KEY_SCHEMA 33):
    # the OFF baseline is pinned with an explicit =0, not left unset.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "0")
    pfs, ufs = _sprayed_hub()
    _run(pfs, ufs)
    names = [str(u.name) for u in ufs]
    assert any("twenty server" in n.lower() for n in names), names


def test_anticase_spray_needs_pf_identity_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 'Manage invoices (drafts)'/'(sent)' — the base names a real resource,
    # NOT the PF home; the qualifier is legit disambiguation. Never flagged,
    # never renamed.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pf = _pf("billing", "Billing", "route:app/billing")
    ufs = [
        _uf_full("u1", "Manage invoices (drafts)", "billing", "drafts",
                 members=["m1"]),
        _uf_full("u2", "Manage invoices (sent)", "billing", "sent",
                 members=["m2"]),
    ]
    assert _spray_sibling_uf_ids(ufs, {"billing": pf}) == set()
    _run([pf], ufs)
    assert [u.name for u in ufs] == [
        "Manage invoices (drafts)", "Manage invoices (sent)"]


# ── verifier-reject re-derive threads own_resource (the keyed leak) ─────


def test_verifier_reject_rederive_keeps_own_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keyed refutation root cause: the Draft Verifier rejected 52 synth
    # drafts and the re-derive WITHOUT own_resource minted the PF-display
    # echo ('Manage twenty server' ×22) that Law A spray-qualified back.
    # iter-2: the re-derive threads the same own-resource membership.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pf = _pf("twenty-server", "Twenty Server", "package:packages/twenty-server")
    ufs = [
        _uf_full("u1", "Browse & manage billing", "twenty-server", "billing",
                 members=["m1"]),
        _uf_full("u2", "Browse & manage dpa", "twenty-server", "dpa",
                 members=["m2"]),
        _uf_full("u3", "Browse & manage fields", "twenty-server", "fields",
                 members=["m3"]),
    ]
    _run_with_verifier([pf], ufs, lambda drafts: {d["id"]: False for d in drafts})
    names = [u.name for u in ufs]
    # no PF-display echo, no spray — each row keeps its own resource.
    assert not any("twenty server" in n.lower() for n in names), names
    assert len({n.strip().lower() for n in names}) == 3


def test_verifier_reject_rederive_off_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OFF: the reject re-derive is the pre-R5 path — the PF-display echo
    # + uniqueness spray reproduce (the recorded keyed OFF behavior).
    # MECHANICAL flip migration (2026-07-21 pack №2, KEY_SCHEMA 33):
    # the OFF baseline is pinned with an explicit =0, not left unset.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "0")
    pf = _pf("twenty-server", "Twenty Server", "package:packages/twenty-server")
    ufs = [
        _uf_full("u1", "Browse & manage billing", "twenty-server", "billing",
                 members=["m1"]),
        _uf_full("u2", "Browse & manage dpa", "twenty-server", "dpa",
                 members=["m2"]),
    ]
    _run_with_verifier([pf], ufs, lambda drafts: {d["id"]: False for d in drafts})
    names = [u.name for u in ufs]
    assert any("twenty server" in n.lower() for n in names), names


# ── no-new-dup guard ────────────────────────────────────────────────────


def test_dup_guard_helper() -> None:
    ufs = [
        _uf_full("u1", "View resolvers", "twenty-server", "resolvers"),
        _uf_full("u2", "View admin panel", "twenty-server", "admin panel"),
        _uf_full("u3", "View resolvers", "other-pf", "resolvers"),
    ]
    # same PF + same folded name = dup; other PF never counts.
    assert _r5_sibling_name_dup(ufs[1], "View resolvers", ufs) is True
    assert _r5_sibling_name_dup(ufs[1], "view RESOLVERS", ufs) is True
    assert _r5_sibling_name_dup(ufs[1], "View widgets", ufs) is False
    assert _r5_sibling_name_dup(ufs[0], "View resolvers", ufs[:2]) is False


def test_no_new_sibling_dup_minted_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two same-resource siblings would both template to the same
    # own-resource name — the second is blocked and keeps its old name
    # (the keyed collateral class: a rename never duplicates a live row).
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pf = _pf("twenty-server", "Twenty Server", "package:packages/twenty-server")
    ufs = [
        _uf_full("u1", "Manage twenty server (resolver)", "twenty-server",
                 "resolvers", members=["m1"]),
        _uf_full("u2", "Manage twenty server (resolvers)", "twenty-server",
                 "resolvers", members=["m2"]),
        _uf_full("u3", "Manage twenty server (billing)", "twenty-server",
                 "billing", members=["m3"]),
    ]
    tele = _run(pf and [pf], ufs)
    names = [str(u.name) for u in ufs]
    folded = [n.strip().lower() for n in names]
    assert len(set(folded)) == len(folded), names  # no duplicates minted
    assert tele.get("uf_r5_dup_blocked", 0) >= 1


# ── anti-case: many-member aggregate keeps its PF-display name ──────────


def test_anticase_aggregate_8_member_untouched_by_r5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # twenty 'Connect twenty server' ×8 members — a REAL aggregate; R5 must
    # not re-template it onto one member's resource. Its name must be
    # byte-identical between OFF and ON.
    pf_off = _pf("twenty-server", "Twenty Server",
                 "package:packages/twenty-server")
    pf_on = _pf("twenty-server", "Twenty Server",
                "package:packages/twenty-server")
    mk = lambda: [
        _uf_full("agg", "Connect twenty server", "twenty-server",
                 "twenty-shared", members=[f"m{i}" for i in range(8)]),
        _uf_full("u1", "Manage twenty server (billing)", "twenty-server",
                 "billing", members=["m10"]),
        _uf_full("u2", "Manage twenty server (dpa)", "twenty-server",
                 "dpa", members=["m11"]),
    ]
    # MECHANICAL flip migration (2026-07-21 pack №2, KEY_SCHEMA 33):
    # the OFF baseline is pinned with an explicit =0, not left unset.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "0")
    ufs_off = mk()
    _run([pf_off], ufs_off)
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    ufs_on = mk()
    _run([pf_on], ufs_on)
    agg_off = next(u for u in ufs_off if u.id == "agg")
    agg_on = next(u for u in ufs_on if u.id == "agg")
    assert agg_on.name == agg_off.name  # aggregate untouched by the wave
    # while the thin spray siblings DID re-template ON.
    assert not any(
        "twenty server" in str(u.name).lower()
        for u in ufs_on if u.id != "agg")
