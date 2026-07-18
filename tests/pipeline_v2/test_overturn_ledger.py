"""S3 — overturn ledger + arbiter v1 units + named anti-cases.

The named anti-cases are the probe's real exhibits
(``/private/tmp/s3-probe/out/analysis.json``) encoded as synthetic
cascades so the MECHANISM is held without a live scan (gate-1 law):

* novu ``notification``   phase_enrich None→Database, 6.86 Database→dal,
                          transport dal→None  (post-freeze writer: transport
                          only → conflict-free; final None)
* cal.com ``app-store``   phase_enrich None→app_store, hub app_store→AI,
                          6.86 AI→app-store (double false loop; hub is
                          PRE-freeze → conflict-free; final app-store)
* Soc0 ``webhook-detail-page`` 8.5 None→Mssp, 6.86 Mssp→webhooks,
                          emission-I12 webhooks→None (post-freeze writer:
                          emission-I12 only → conflict-free; final None)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2.overturn_ledger import (
    OverturnEntry,
    OverturnLedger,
    finalize_arbiter,
    install_ledger,
    overturn_arbiter_enabled,
    rung_for_frames,
    uninstall_ledger,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _feat(name: str, pf: str | None = None) -> Feature:
    return Feature(
        name=name, paths=[f"{name}/a.ts"], authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=80.0,
        layer="developer", product_feature_id=pf,
    )


def _uf(name: str, pf: str | None = None) -> UserFlow:
    return UserFlow(id="UF-000", name=name, resource=name.lower(),
                    intent="manage", product_feature_id=pf)


def _entry(serial: int, old: str | None, new: str | None, rung: str,
           kind: str = "dev", ename: str = "e") -> OverturnEntry:
    return OverturnEntry(kind=kind, serial=serial, eid=ename, ename=ename,
                         layer="developer", old=old, new=new, rung=rung,
                         writer=f"{rung}.py:fn:1")


@pytest.fixture()
def ledger():
    led = OverturnLedger()
    install_ledger(led)
    try:
        yield led
    finally:
        uninstall_ledger()


# ── flag parsing ─────────────────────────────────────────────────────────


def test_overturn_arbiter_enabled_flag(monkeypatch):
    monkeypatch.delenv("FAULTLINE_OVERTURN_ARBITER", raising=False)
    assert overturn_arbiter_enabled() is False  # default OFF
    for off in ("0", "false", "off", "no", ""):
        monkeypatch.setenv("FAULTLINE_OVERTURN_ARBITER", off)
        assert overturn_arbiter_enabled() is False
    for on in ("1", "true", "yes", "on"):
        monkeypatch.setenv("FAULTLINE_OVERTURN_ARBITER", on)
        assert overturn_arbiter_enabled() is True


# ── observer: record fill / overturn / clear (write-through) ─────────────


def test_observer_records_fill_overturn_clear(ledger):
    f = _feat("auth", pf=None)
    f.product_feature_id = "A"          # fill None→A
    f.product_feature_id = "B"          # overturn A→B
    f.product_feature_id = "B"          # no-op (same value) — anti-case
    f.product_feature_id = None         # clear B→None
    # write-through: the live attribute reflects every write
    assert f.product_feature_id is None
    seq = [(e.old, e.new) for e in ledger.entries]
    assert seq == [(None, "A"), ("A", "B"), ("B", None)]  # no-op absent
    assert all(e.kind == "dev" for e in ledger.entries)


def test_uf_observer_records_kind_uf(ledger):
    u = _uf("book", pf=None)
    u.product_feature_id = "scheduling"
    assert u.product_feature_id == "scheduling"
    assert [e.kind for e in ledger.entries] == ["uf"]


def test_observer_off_is_pure_passthrough():
    # No ledger active → the setattr wrapper records NOTHING and applies
    # the write. This is the kill-switch guarantee (branch-OFF == main).
    uninstall_ledger()
    spy = OverturnLedger()          # NOT installed
    f = _feat("billing", pf=None)
    f.product_feature_id = "X"
    assert f.product_feature_id == "X"
    assert spy.entries == []        # untouched — no active ledger


# ── rung attribution ─────────────────────────────────────────────────────


def test_rung_for_frames():
    assert rung_for_frames(["transport_handoff.py:run:100"]) == "transport"
    assert rung_for_frames(["stage_6_86_anchored_mint.py:x:1"]) == "6.86-mint"
    assert rung_for_frames(["uf_terminal_home.py:a:9"]) == "terminal-home"
    assert rung_for_frames(["some_unmapped_pass.py:z:1"]) == "some_unmapped_pass"
    assert rung_for_frames([]) == "<unknown>"


# ── conflict detector — post-freeze multi-writer divergence ──────────────


def test_conflict_detector_synthetic_pair():
    led = OverturnLedger()
    # two DISTINCT post-freeze writers, DIVERGENT values, one entity → 1
    led.entries = [
        _entry(0, None, "A", "transport"),
        _entry(0, "A", "B", "devgrain"),
    ]
    conflicts = led.conflicts()
    assert len(conflicts) == 1
    assert set(conflicts[0]["writers"]) == {"transport", "devgrain"}


def test_conflict_detector_anticases():
    # (a) two post-freeze writers, SAME value → not a conflict
    led = OverturnLedger()
    led.entries = [_entry(0, None, "A", "transport"),
                   _entry(0, "A", "A", "devgrain")]
    assert led.conflicts() == []
    # (b) pre-freeze (phase_enrich) + post-freeze (transport), divergent →
    #     NOT a post-freeze conflict (the void write is overwritten by 6.86)
    led = OverturnLedger()
    led.entries = [_entry(0, None, "Database", "phase_enrich"),
                   _entry(0, "Database", "dal", "6.86-mint"),
                   _entry(0, "dal", None, "transport")]
    assert led.conflicts() == []
    # (c) single post-freeze writer → not a conflict
    led = OverturnLedger()
    led.entries = [_entry(0, "dal", None, "transport")]
    assert led.conflicts() == []


# ── arbiter replay reproduces the cascade byte-for-byte (STRONGEST) ──────


def test_arbiter_replay_last_writer_wins():
    led = OverturnLedger()
    led.entries = [
        _entry(0, None, "A", "phase_enrich"),
        _entry(0, "A", "B", "6.86-mint"),
        _entry(0, "B", "C", "transport"),
    ]
    assert led.replay("dev") == {0: "C"}  # rung-priority = record order


def test_replay_matches_live_state_write_through(ledger):
    # The replay-final for an observed, still-alive object EQUALS its live
    # product_feature_id — the arbiter reproduces the cascade result
    # byte-for-byte (verify_replay == 0 mismatches).
    f = _feat("api", pf=None)
    f.product_feature_id = "AI"
    f.product_feature_id = "network-security"
    assert ledger.verify_replay([f], []) == 0
    assert ledger.replay("dev")[ledger._serial_by_id[id(f)]] == \
        f.product_feature_id


# ── census reconciles with the probe analysis.json shape ─────────────────


def test_census_counts_named_exhibit_novu_notification():
    # novu 'notification': fill + 2 overturns; transport clears to None.
    led = OverturnLedger()
    led.entries = [
        _entry(0, None, "Database", "phase_enrich", ename="notification"),
        _entry(0, "Database", "dal", "6.86-mint", ename="notification"),
        _entry(0, "dal", None, "transport", ename="notification"),
    ]
    c = led.census("dev")
    assert c["entities_written"] == 1
    assert c["writes"] == 3
    assert c["fills(None->X)"] == 1
    assert c["overturns(X->Y)"] == 2
    assert c["clears(X->None)"] == 1
    assert c["per_writer_all"] == {
        "phase_enrich": 1, "6.86-mint": 1, "transport": 1,
    }
    assert led.replay("dev") == {0: None}        # final None
    assert led.conflicts() == []                 # transport is sole post-freeze


def test_census_named_exhibit_cal_app_store_double_false_loop():
    # cal.com 'app-store': None→app_store→AI→app-store; hub is pre-freeze.
    led = OverturnLedger()
    led.entries = [
        _entry(0, None, "app_store", "phase_enrich", ename="app-store"),
        _entry(0, "app_store", "AI", "hub", ename="app-store"),
        _entry(0, "AI", "app-store", "6.86-mint", ename="app-store"),
    ]
    assert led.replay("dev") == {0: "app-store"}
    assert led.conflicts() == []                 # hub pre-freeze, no post-freeze pair
    assert led.census("dev")["overturns(X->Y)"] == 2


def test_census_named_exhibit_soc0_webhook_detail():
    # Soc0 'webhook-detail-page': 8.5 None→Mssp, 6.86 Mssp→webhooks,
    # emission-I12 webhooks→None. Only emission-I12 is post-freeze.
    led = OverturnLedger()
    led.entries = [
        _entry(0, None, "Mssp", "8.5-backfill", ename="webhook-detail-page"),
        _entry(0, "Mssp", "webhooks", "6.86-mint", ename="webhook-detail-page"),
        _entry(0, "webhooks", None, "emission-I12", ename="webhook-detail-page"),
    ]
    assert led.replay("dev") == {0: None}
    assert led.conflicts() == []
    assert led.census("dev")["per_writer_overturns"] == {
        "6.86-mint": 1, "emission-I12": 1,
    }


# ── arbiter emits telemetry into scan_meta ───────────────────────────────


def test_finalize_arbiter_emits_scan_meta(ledger):
    f = _feat("api", pf=None)
    f.product_feature_id = "AI"
    f.product_feature_id = "cases"
    sm: dict = {}
    finalize_arbiter(ledger, [f], [], sm)
    assert "overturns" in sm and "overturn_conflicts" in sm
    assert sm["overturns"]["dev"]["writes"] == 2
    assert sm["overturns"]["journal_writes"] == 2
    assert sm["overturn_conflicts"] == []


def test_scan_meta_telemetry_keys_are_stripped_by_normalize():
    # ON adds overturns/overturn_conflicts to scan_meta; normalize_scan must
    # strip them so ON==OFF is byte-identical at the content layer.
    from faultline.tools.normalize_scan import normalize_scan
    off = {"features": [], "scan_meta": {"stack": "next"}}
    on = {"features": [], "scan_meta": {
        "stack": "next",
        "overturns": {"journal_writes": 999, "dev": {"writes": 999}},
        "overturn_conflicts": [{"kind": "dev", "ename": "x"}],
    }}
    assert normalize_scan(off) == normalize_scan(on)
