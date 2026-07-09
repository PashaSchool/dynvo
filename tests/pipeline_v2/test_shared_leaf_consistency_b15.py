"""B15 — shared-leaf role consistency law.

A member file that is high cross-PF-claim-fan-in, NOT a product surface, and
already role="shared" in >=1 feature is shared infra (the i18n-locale class:
the same locales/*.json tagged shared on one PF, closure on another). Force
role="shared" everywhere so the I23 anchor-body check stops counting it as a
PF's own body. Behind FAULTLINE_SHARED_LEAF_CONSISTENCY (=0 byte-identical).

Anti-cases: a low-fan-in OWNED asset is never forced; a product SURFACE is never
forced; a file that is never shared-anywhere is never forced (the consistency
guard).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.file_lane import (
    SHARED_LEAF_CONSISTENCY_ENV,
    enforce_shared_leaf_consistency,
    shared_leaf_consistency_enabled,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _feat(name, mfs, *, anchor=None, layer="product"):
    return Feature(
        name=name, display_name=name, paths=[m[0] for m in mfs], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_NOW,
        health_score=90.0, layer=layer, anchor_id=anchor,
        member_files=[MemberFile(path=p, role=r, confidence=1.0, primary=False)
                      for p, r in mfs],
    )


def _roles(feat, path):
    return sorted(m.role for m in feat.member_files if m.path == path)


def _scene():
    """`loc.json`: shared on A, closure on B & C (fan-in 3, K=2) — the i18n
    class. `own.ts`: single-PF anchor — must stay. `page.tsx`: a SURFACE
    (A's anchor tail) shared+claimed widely — must stay."""
    A = _feat("a", [("i18n/loc.json", "shared"), ("a/own.ts", "anchor"),
                    ("frontend/PageA.tsx", "shared")],
              anchor="route:frontend/PageA.tsx")
    B = _feat("b", [("i18n/loc.json", "closure"), ("frontend/PageA.tsx", "closure")])
    C = _feat("c", [("i18n/loc.json", "closure"), ("frontend/PageA.tsx", "closure")])
    return [A, B, C]


def test_forces_shared_everywhere(monkeypatch):
    monkeypatch.setenv(SHARED_LEAF_CONSISTENCY_ENV, "1")
    pfs = _scene()
    tele = enforce_shared_leaf_consistency([], pfs, [])
    assert tele["applied"]
    assert tele["threshold"] == 2
    # loc.json forced shared on B and C (was closure)
    for f in pfs:
        assert _roles(f, "i18n/loc.json") == ["shared"]
    assert tele["forced_member_rows"] == 2   # B + C rows flipped
    assert "i18n/loc.json" in tele["samples"]


def test_owned_low_fanin_asset_untouched(monkeypatch):
    monkeypatch.setenv(SHARED_LEAF_CONSISTENCY_ENV, "1")
    pfs = _scene()
    enforce_shared_leaf_consistency([], pfs, [])
    assert _roles(pfs[0], "a/own.ts") == ["anchor"]   # single-PF owned — kept


def test_product_surface_never_forced(monkeypatch):
    monkeypatch.setenv(SHARED_LEAF_CONSISTENCY_ENV, "1")
    pfs = _scene()
    enforce_shared_leaf_consistency([], pfs, [])
    # PageA.tsx is A's anchor surface — must NOT be forced (still closure on B/C)
    assert _roles(pfs[1], "frontend/PageA.tsx") == ["closure"]
    assert _roles(pfs[2], "frontend/PageA.tsx") == ["closure"]


def test_routes_index_file_is_a_surface(monkeypatch):
    monkeypatch.setenv(SHARED_LEAF_CONSISTENCY_ENV, "1")
    # widget.ts: high fan-in, shared-somewhere, but IS a routes_index file.
    A = _feat("a", [("w/widget.ts", "shared")])
    B = _feat("b", [("w/widget.ts", "closure")])
    C = _feat("c", [("w/widget.ts", "closure")])
    enforce_shared_leaf_consistency([], [A, B, C],
                                    [{"file": "w/widget.ts"}])
    assert _roles(B, "w/widget.ts") == ["closure"]    # surface — untouched


def test_never_shared_anywhere_not_forced(monkeypatch):
    monkeypatch.setenv(SHARED_LEAF_CONSISTENCY_ENV, "1")
    # closure-only in all 3 (fan-in 3 >= K) but NEVER shared -> consistency
    # guard blocks it (we only make a file consistent, never invent sharing).
    A = _feat("a", [("i18n/de.json", "closure")])
    B = _feat("b", [("i18n/de.json", "closure")])
    C = _feat("c", [("i18n/de.json", "closure")])
    tele = enforce_shared_leaf_consistency([], [A, B, C], [])
    assert tele["forced_shared_files"] == 0
    for f in [A, B, C]:
        assert _roles(f, "i18n/de.json") == ["closure"]


def test_low_fanin_shared_file_not_forced(monkeypatch):
    monkeypatch.setenv(SHARED_LEAF_CONSISTENCY_ENV, "1")
    # shared on A, closure on B — fan-in 2 == SHARED_FLOOR K=2, so it DOES fire.
    # Drop to fan-in 1 (only A) -> below floor -> not forced.
    A = _feat("a", [("lib/x.ts", "shared")])
    tele = enforce_shared_leaf_consistency([], [A], [])
    # single PF: num_pfs=1, K=max(2, ceil(0.08*1))=2, fan-in 1 < 2 -> no-op
    assert tele["forced_shared_files"] == 0


def test_dev_member_files_also_forced(monkeypatch):
    monkeypatch.setenv(SHARED_LEAF_CONSISTENCY_ENV, "1")
    pfs = _scene()
    dev = _feat("dev-x", [("i18n/loc.json", "closure")], layer="developer")
    enforce_shared_leaf_consistency([dev], pfs, [])
    assert _roles(dev, "i18n/loc.json") == ["shared"]   # dev row flipped too


def test_flag_off_noop(monkeypatch):
    monkeypatch.setenv(SHARED_LEAF_CONSISTENCY_ENV, "0")
    assert not shared_leaf_consistency_enabled()
    pfs = _scene()
    tele = enforce_shared_leaf_consistency([], pfs, [])
    assert tele["enabled"] is False
    assert _roles(pfs[1], "i18n/loc.json") == ["closure"]   # unchanged


def test_idempotent_determinism(monkeypatch):
    monkeypatch.setenv(SHARED_LEAF_CONSISTENCY_ENV, "1")
    pfs = _scene()
    t1 = enforce_shared_leaf_consistency([], pfs, [])
    t2 = enforce_shared_leaf_consistency([], pfs, [])   # second run: nothing left
    assert t1["forced_member_rows"] == 2
    assert t2["forced_member_rows"] == 0   # idempotent — already shared
