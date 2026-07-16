"""B69-v2 add-on 4 — the 6.7e ``law_c_rescore`` telemetry view.

The forensics exhibit: ``rescore_uf_confidence`` runs the FULL law body
with a fresh tele dict, and the historical 3-key whitelist threw away the
Law-A action counters — a qualifier stamped during the re-score was
invisible to the operator's census (`uf_uniqueness_qualified` absent while
the board wore parentheticals). Armed, every SCALAR key survives; nested
blobs stay out; OFF is the exact historical view (byte-identical).
"""

from __future__ import annotations

from faultline.pipeline_v2.stage_6_7e_adjudicator import _rescore_tele_view

_RESCORE = {
    "confidence_before": {"high": 3, "low": 5},
    "confidence_after": {"high": 6, "low": 2},
    "uf_uniqueness_qualified": 4,
    "uf_twins_resolved": 2,
    "uf_name_degrimed": 1,
    "uf_verb_snap": {"snapped": 7},   # nested — never in scan_meta view
    "labeler_pending": 12,
}


def test_armed_preserves_scalar_law_counters(monkeypatch):
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    view = _rescore_tele_view(_RESCORE)
    assert view["uf_uniqueness_qualified"] == 4
    assert view["uf_twins_resolved"] == 2
    assert view["uf_name_degrimed"] == 1
    assert view["labeler_pending"] == 12
    assert view["confidence_before"] == {"high": 3, "low": 5}
    assert "uf_verb_snap" not in view  # nested blobs stay out


def test_off_is_the_historical_three_key_view(monkeypatch):
    # MECHANICAL (horizon-1 flip): explicit "0" for the OFF (historical) view.
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "0")
    view = _rescore_tele_view(_RESCORE)
    assert set(view) == {"confidence_before", "confidence_after"}


def test_skipped_key_kept_both_ways(monkeypatch):
    # MECHANICAL (horizon-1 flip): explicit "0" for the OFF half.
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "0")
    assert _rescore_tele_view({"skipped": "naming-laws-off"}) == {
        "skipped": "naming-laws-off"}
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    assert _rescore_tele_view({"skipped": "naming-laws-off"}) == {
        "skipped": "naming-laws-off"}


def test_armed_deterministic_key_order(monkeypatch):
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    assert list(_rescore_tele_view(_RESCORE)) == \
        list(_rescore_tele_view(dict(reversed(list(_RESCORE.items())))))
