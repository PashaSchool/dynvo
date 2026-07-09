"""B15b — data-file shared-leaf rail (§4b).

A LARGE pure-DATA leaf file (i18n locale pack, template JSON) consumed by >=2
PFs and NOT a product surface is forced role="shared" everywhere — WITHOUT the
B15 shared-somewhere guard, so it reaches the closure-EVERYWHERE locale blobs.

Anti-cases: small data file (< LOC floor) untouched; CODE file untouched even
when large + high-fan-in; single-PF data file untouched; product surface never
forced; flag-off / no-repo_loc no-op; determinism.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.file_lane import (
    DATA_LEAF_ENV,
    DATA_LEAF_LOC_FRAC,
    data_leaf_enabled,
    enforce_data_leaf_shared,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_REPO_LOC = 100_000
_FLOOR = int(DATA_LEAF_LOC_FRAC * _REPO_LOC)   # 1000 at repo_loc=100k


def _feat(name, mfs, *, anchor=None, layer="product"):
    return Feature(
        name=name, display_name=name, paths=[m[0] for m in mfs], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_NOW,
        health_score=90.0, layer=layer, anchor_id=anchor,
        member_files=[MemberFile(path=p, role=r, confidence=1.0, primary=False,
                                 loc=loc) for p, r, loc in mfs])


def _roles(feat, path):
    return sorted(m.role for m in feat.member_files if m.path == path)


def _scene():
    """`i18n/de.json`: LARGE (2000 LOC) pure-data, closure-EVERYWHERE (never
    shared), claimed by A & B -> the exact class B15 could not reach.
    `a/own.ts`: large CODE, single-PF -> stays.
    `small.json`: tiny data (<floor) -> stays."""
    A = _feat("a", [("i18n/de.json", "closure", 2000), ("a/own.ts", "anchor", 5000),
                    ("data/small.json", "closure", 40)])
    B = _feat("b", [("i18n/de.json", "closure", 2000),
                    ("data/small.json", "closure", 40)])
    return [A, B]


def test_forces_large_closure_everywhere_data_blob(monkeypatch):
    monkeypatch.setenv(DATA_LEAF_ENV, "1")
    pfs = _scene()
    tele = enforce_data_leaf_shared([], pfs, [], _REPO_LOC)
    assert tele["applied"]
    for f in pfs:                                   # de.json shared on A and B
        assert _roles(f, "i18n/de.json") == ["shared"]
    assert "i18n/de.json" in tele["samples"]
    assert tele["forced_member_rows"] == 2


def test_code_file_never_forced(monkeypatch):
    monkeypatch.setenv(DATA_LEAF_ENV, "1")
    pfs = _scene()
    enforce_data_leaf_shared([], pfs, [], _REPO_LOC)
    assert _roles(pfs[0], "a/own.ts") == ["anchor"]   # .ts is code — untouched


def test_small_data_file_below_floor_untouched(monkeypatch):
    monkeypatch.setenv(DATA_LEAF_ENV, "1")
    pfs = _scene()
    enforce_data_leaf_shared([], pfs, [], _REPO_LOC)
    assert _roles(pfs[0], "data/small.json") == ["closure"]  # 40 < floor 1000


def test_single_pf_data_file_untouched(monkeypatch):
    monkeypatch.setenv(DATA_LEAF_ENV, "1")
    A = _feat("a", [("solo/big.json", "closure", 5000)])
    B = _feat("b", [("b/x.ts", "anchor", 10)])
    enforce_data_leaf_shared([], [A, B], [], _REPO_LOC)
    assert _roles(A, "solo/big.json") == ["closure"]   # fan-in 1 -> untouched


def test_product_surface_data_file_never_forced(monkeypatch):
    monkeypatch.setenv(DATA_LEAF_ENV, "1")
    # a large data file that IS a PF's anchor (surface) -> never forced
    A = _feat("a", [("content/page.json", "anchor", 5000)],
              anchor="route:content/page.json")
    B = _feat("b", [("content/page.json", "closure", 5000)])
    enforce_data_leaf_shared([], [A, B], [{"file": "x"}], _REPO_LOC)
    assert _roles(B, "content/page.json") == ["closure"]   # surface — untouched


def test_reaches_what_consistency_law_cannot(monkeypatch):
    # The whole point: de.json is closure on BOTH PFs (never shared) — the B15
    # consistency guard would skip it; the data rail catches it.
    monkeypatch.setenv(DATA_LEAF_ENV, "1")
    pfs = _scene()
    assert all(_roles(f, "i18n/de.json") == ["closure"] for f in pfs)  # pre
    enforce_data_leaf_shared([], pfs, [], _REPO_LOC)
    assert all(_roles(f, "i18n/de.json") == ["shared"] for f in pfs)   # post


def test_flag_off_noop(monkeypatch):
    monkeypatch.setenv(DATA_LEAF_ENV, "0")
    assert not data_leaf_enabled()
    pfs = _scene()
    tele = enforce_data_leaf_shared([], pfs, [], _REPO_LOC)
    assert tele["enabled"] is False
    assert _roles(pfs[0], "i18n/de.json") == ["closure"]


def test_no_repo_loc_noop(monkeypatch):
    monkeypatch.setenv(DATA_LEAF_ENV, "1")
    pfs = _scene()
    tele = enforce_data_leaf_shared([], pfs, [], None)
    assert tele["applied"] is False
    assert _roles(pfs[0], "i18n/de.json") == ["closure"]


def test_idempotent(monkeypatch):
    monkeypatch.setenv(DATA_LEAF_ENV, "1")
    pfs = _scene()
    t1 = enforce_data_leaf_shared([], pfs, [], _REPO_LOC)
    t2 = enforce_data_leaf_shared([], pfs, [], _REPO_LOC)
    assert t1["forced_member_rows"] == 2
    assert t2["forced_member_rows"] == 0    # already shared
