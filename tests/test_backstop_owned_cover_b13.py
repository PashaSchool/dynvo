"""B13 — backstop own-entry cover (Part 1) + honest coverage markers (Part 2a).

Part 1: the synthesize arm bundles ONLY flows whose ENTRY-OWNER is the covered
PF (the validator's I16 ruler), so a coverage journey can never be
majority-foreign. A PF with no own-entry flow gets a member-LESS coverage seed
instead of a foreign bundle (I8 covers on any UF ref; I16 never fires at chk=0).

Part 2a: every surviving member-less I8-cover seed gets a machine-readable
``is_coverage_marker`` flag + an honest ``'Uncovered: <PF> routes'`` name
(replacing the ``'Run X'`` verb template).

Both behind ``FAULTLINE_BACKSTOP_OWNED_COVER`` (=0 byte-identical to pre-B13).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from faultline.models.types import Feature, Flow, FlowLineRange, UserFlow
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _backstop_uncovered_pfs,
    backstop_owned_cover_enabled,
)
from faultline.pipeline_v2.synth_quality import (
    SYSTEM_RECALL_REASON,
    _coverage_marker_name,
    honest_coverage_markers,
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
_ENV = "FAULTLINE_BACKSTOP_OWNED_COVER"


def _flow(uuid: str, entry: str, loc: int = 10) -> Flow:
    return Flow(
        name=f"{uuid}-flow", uuid=uuid, paths=[entry],
        entry_point_file=entry, authors=["a"], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_TS, health_score=90.0,
        line_ranges=[FlowLineRange(path=entry, start_line=1, end_line=loc)],
    )


def _dev(name: str, owns: str, flows: list[Flow], pf: str | None = None) -> Feature:
    return Feature(
        name=name, display_name=name, paths=[owns], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="developer", flows=flows,
        product_feature_id=pf if pf is not None else name,  # entry-owner map reads this
    )


def _pf(slug: str, display: str) -> Feature:
    return Feature(
        name=slug, display_name=display, paths=[f"src/{slug}/a.py"],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0, layer="product",
    )


def _uf(name, pfid, members, **kw):
    return UserFlow(id="UF-000", name=name, resource=name.lower(),
                    intent="manage", product_feature_id=pfid,
                    member_flow_ids=list(members), member_count=len(members),
                    **kw)


# ── Part 1 — own-entry filter ────────────────────────────────────────────

def _scene():
    """`foo` dev's only flow ENTERS a file owned by `bar` (foreign entry);
    `baz` dev's flow enters its OWN file. `bar` is already covered."""
    devs = [
        _dev("foo", "src/foo/a.py", [_flow("ff", "src/bar/a.py")]),  # foreign entry
        _dev("bar", "src/bar/a.py", [_flow("fb", "src/bar/a.py")]),
        _dev("baz", "src/baz/a.py", [_flow("fz", "src/baz/a.py")]),  # own entry
    ]
    d2p = {"foo": ("foo",), "bar": ("bar",), "baz": ("baz",)}
    pfs = [_pf("foo", "Foo"), _pf("bar", "Bar"), _pf("baz", "Baz")]
    ufs = [_uf("Manage bar", "bar", ["fb"])]  # bar covered; foo+baz uncovered
    return ufs, pfs, d2p, devs


def test_own_entry_pf_gets_bundled(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    ufs, pfs, d2p, devs = _scene()
    _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
    baz = [u for u in ufs if u.product_feature_id == "baz"]
    assert len(baz) == 1
    assert baz[0].member_flow_ids == ["fz"]      # own-entry flow bundled
    assert baz[0].member_count == 1


def test_foreign_entry_pf_gets_memberless_seed_not_foreign_bundle(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    ufs, pfs, d2p, devs = _scene()
    tele = _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
    foo = [u for u in ufs if u.product_feature_id == "foo"]
    assert len(foo) == 1, "foo must still be COVERED (I8)"
    assert foo[0].member_count == 0            # member-LESS seed, not a bundle
    assert foo[0].member_flow_ids == []
    assert foo[0].synthesized
    assert foo[0].synthesis_reason == SYSTEM_RECALL_REASON
    assert "ff" not in (foo[0].member_flow_ids or [])  # foreign flow NOT bundled
    assert tele.get("pf_backstop_owned_seed", 0) == 1


def test_kill_switch_bundles_foreign_like_base(monkeypatch):
    # Flag OFF = pre-B13 behavior: foo's foreign flow IS bundled (the defect).
    monkeypatch.setenv(_ENV, "0")
    assert not backstop_owned_cover_enabled()
    ufs, pfs, d2p, devs = _scene()
    _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
    foo = [u for u in ufs if u.product_feature_id == "foo"]
    assert len(foo) == 1
    assert foo[0].member_flow_ids == ["ff"]     # foreign flow bundled (old bug)
    assert foo[0].member_count == 1


def test_determinism_double_run(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    outs = []
    for _ in range(2):
        ufs, pfs, d2p, devs = _scene()
        _backstop_uncovered_pfs(ufs, pfs, d2p, devs, set())
        outs.append([(u.product_feature_id, tuple(u.member_flow_ids or []),
                      u.member_count) for u in ufs])
    assert outs[0] == outs[1]


# ── Part 2a — honest coverage markers ────────────────────────────────────

def _seed(uid, name, pf, resource, **kw):
    return SimpleNamespace(
        id=uid, name=name, synthesis_reason=SYSTEM_RECALL_REASON,
        synthesized=True, member_flow_ids=[], member_count=0,
        product_feature_id=pf, resource=resource, name_confidence="low", **kw)


def test_coverage_marker_name_no_verb_template():
    assert _coverage_marker_name("SentinelOne") == "Uncovered: SentinelOne routes"
    assert _coverage_marker_name("  a  b  ") == "Uncovered: a b routes"
    assert _coverage_marker_name("") == "Uncovered: capability routes"


def test_marks_and_renames_member_less_seed(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    ufs = [_seed("UF-051", "Run SentinelOne", "sentinelone", "sentinelone")]
    pfs = [SimpleNamespace(id="sentinelone", name="sentinelone",
                           display_name="SentinelOne")]
    meta: dict = {}
    tele = honest_coverage_markers(ufs, pfs, meta)
    assert tele["marked"] == 1
    assert ufs[0].name == "Uncovered: SentinelOne routes"
    assert ufs[0].is_coverage_marker is True
    assert meta["synth_quality"]["coverage_markers"] == 1


def test_member_ful_uf_untouched(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    real = SimpleNamespace(id="UF-1", name="Manage webhooks", synthesized=True,
                           member_flow_ids=["a", "b"], member_count=2,
                           product_feature_id="webhooks", resource="webhook",
                           name_confidence="high")
    honest_coverage_markers([real], [], {})
    assert real.name == "Manage webhooks"
    assert getattr(real, "is_coverage_marker", False) is False


def test_non_synthesized_untouched(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    u = SimpleNamespace(id="UF-1", name="Browse things", synthesized=False,
                        member_flow_ids=[], member_count=0,
                        product_feature_id="p", resource="thing")
    honest_coverage_markers([u], [], {})
    assert u.name == "Browse things"


def test_flag_off_noop(monkeypatch):
    monkeypatch.setenv(_ENV, "0")
    ufs = [_seed("UF-051", "Run SentinelOne", "sentinelone", "sentinelone")]
    tele = honest_coverage_markers(ufs, [], {})
    assert tele["marked"] == 0
    assert ufs[0].name == "Run SentinelOne"          # unchanged
    assert getattr(ufs[0], "is_coverage_marker", False) is False


def test_idempotent(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    ufs = [_seed("UF-051", "Run SentinelOne", "sentinelone", "sentinelone")]
    pfs = [SimpleNamespace(id="sentinelone", name="sentinelone",
                           display_name="SentinelOne")]
    honest_coverage_markers(ufs, pfs, {})
    first = ufs[0].name
    honest_coverage_markers(ufs, pfs, {})
    assert ufs[0].name == first == "Uncovered: SentinelOne routes"
