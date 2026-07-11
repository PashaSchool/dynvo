"""B38 — marker coordinate integrity: zero-span markers must not ship.

Anti-cases: markers WITH spans stay; organic UFs never touched; discovery
is by structure (synthesized + mc=0), never by the 'Uncovered:' name
prefix (the cal.com authored-label lesson); default ON since the keyed cal.com proof; =0 byte-inert.
"""

from __future__ import annotations

import pytest

from faultline.pipeline_v2.synth_quality import (
    MARKER_COORDS_REQUIRED_ENV,
    marker_coords_required,
    suppress_no_coords_markers,
)


def _marker(uid: str, name: str, surface=None, mc: int = 0, synth=True):
    return {
        "id": uid, "name": name, "synthesized": synth,
        "member_count": mc, "member_flow_ids": [],
        "is_coverage_marker": True,
        "surface_files": surface,
        "synthesis_reason": "e2e_journey_recall",
    }


def _organic(uid: str, name: str):
    return {
        "id": uid, "name": name, "synthesized": False,
        "member_count": 3, "member_flow_ids": ["a", "b", "c"],
        "surface_files": None,
    }


def test_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default flipped ON at merge (2026-07-11 keyed cal.com proof);
    # =0 remains the kill-switch restoring the rows.
    monkeypatch.delenv(MARKER_COORDS_REQUIRED_ENV, raising=False)
    assert marker_coords_required() is True
    monkeypatch.setenv(MARKER_COORDS_REQUIRED_ENV, "0")
    assert marker_coords_required() is False
    ufs = [_marker("UF-1", "Uncovered: X routes")]
    tele = suppress_no_coords_markers(ufs, {})
    assert tele == {"enabled": False, "suppressed": 0}
    assert len(ufs) == 1  # byte-inert under =0


def test_zero_span_marker_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MARKER_COORDS_REQUIRED_ENV, "1")
    meta: dict = {}
    ufs = [
        _marker("UF-1", "Uncovered: GoCardless routes"),
        _organic("UF-2", "Manage invoices"),
    ]
    tele = suppress_no_coords_markers(ufs, meta)
    assert tele["suppressed"] == 1
    assert [u["id"] for u in ufs] == ["UF-2"]
    sq = meta["synth_quality"]
    assert sq["markers_suppressed_no_coords"] == 1
    rec = sq["suppressed_markers"][0]
    assert rec == {"id": "UF-1", "name": "Uncovered: GoCardless routes",
                   "pf": "", "reason": "no_resolvable_coords"}


def test_full_machine_record_never_truncated(monkeypatch) -> None:
    # No-silent-gap law: EVERY suppressed marker lands in scan_meta with
    # its pf — the board hides the row, the record keeps the gap.
    monkeypatch.setenv(MARKER_COORDS_REQUIRED_ENV, "1")
    meta: dict = {}
    rows = []
    for i in range(25):
        r = _marker(f"UF-{i}", f"Authored label {i}")
        r["product_feature_id"] = f"pf-{i}"
        rows.append(r)
    tele = suppress_no_coords_markers(rows, meta)
    assert tele["suppressed"] == 25
    recs = meta["synth_quality"]["suppressed_markers"]
    assert len(recs) == 25  # complete, never truncated
    assert recs[7] == {"id": "UF-7", "name": "Authored label 7",
                       "pf": "pf-7", "reason": "no_resolvable_coords"}


def test_authored_label_marker_dropped_by_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The warden lesson: 20/22 wave15 breaches wore authored e2e labels,
    # not 'Uncovered:' — discovery must be structural.
    monkeypatch.setenv(MARKER_COORDS_REQUIRED_ENV, "1")
    ufs = [_marker("UF-143", "Can delete user account")]
    tele = suppress_no_coords_markers(ufs, {})
    assert tele["suppressed"] == 1
    assert ufs == []


def test_marker_with_spans_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MARKER_COORDS_REQUIRED_ENV, "1")
    ufs = [_marker("UF-1", "Uncovered: EDR routes",
                   surface=[{"path": "a.py", "start_line": 1,
                             "end_line": 100}])]
    tele = suppress_no_coords_markers(ufs, {})
    assert tele["suppressed"] == 0
    assert len(ufs) == 1


def test_memberful_synthetic_never_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MARKER_COORDS_REQUIRED_ENV, "1")
    row = _marker("UF-1", "Manage team", mc=4)
    row["member_flow_ids"] = ["x"] * 4
    ufs = [row]
    tele = suppress_no_coords_markers(ufs, {})
    assert tele["suppressed"] == 0 and len(ufs) == 1


def test_no_drop_no_meta_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MARKER_COORDS_REQUIRED_ENV, "1")
    meta: dict = {}
    ufs = [_organic("UF-2", "Manage invoices")]
    suppress_no_coords_markers(ufs, meta)
    assert meta == {}  # no telemetry keys grown on clean boards
