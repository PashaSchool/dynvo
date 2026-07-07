"""Journey lattice (W5) — deterministic catch-all partition tests.

Fixtures are Soc0-shaped: PF `investigations` with one catch-all UF
whose members split by sub-object (core CRUD / lifecycle / notes /
fork) — the operator target "Investigations ⇒ 4-6 recognizable
journeys". Doctrine under test: partition is deterministic; LLM only
names/reviews; conservation (no member ever lost); conservative
verifier fallback.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Flow, UserFlow
from faultline.pipeline_v2.journey_lattice import (
    apply_journey_lattice,
    lattice_enabled,
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
_ENV = "FAULTLINE_JOURNEY_LATTICE"


def _flow(uuid: str, name: str, entry: str) -> Flow:
    return Flow(
        uuid=uuid, name=name, entry_point_file=entry, paths=[entry],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=100.0,
    )


_ENTRY = "backend/routers/investigations.py"


def _investigations_fixture():
    flows = [
        # core browse/CRUD (object=investigations, no sub-object)
        _flow("f1", "list-investigations-flow", _ENTRY),
        _flow("f2", "view-investigation-counts-flow", _ENTRY),
        _flow("f3", "create-investigation-flow", _ENTRY),
        _flow("f4", "delete-investigation-flow", _ENTRY),
        # lifecycle sub-object
        _flow("f5", "patch-api-investigations-investigation-id-lifecycle-flow", _ENTRY),
        _flow("f6", "patch-api-investigations-bulk-lifecycle-flow", _ENTRY),
        # notes sub-object
        _flow("f7", "post-api-investigations-investigation-id-notes-flow", _ENTRY),
        _flow("f8", "delete-api-investigations-investigation-id-notes-note-id-flow", _ENTRY),
        # fork sub-object
        _flow("f9", "post-api-investigations-investigation-id-fork-flow", _ENTRY),
        _flow("f10", "post-api-investigations-bulk-fork-flow", _ENTRY),
    ]
    uf = UserFlow(
        id="UF-001", name="Create and manage investigations",
        product_feature_id="investigations", intent="manage",
        resource="investigations",
        member_flow_ids=[f.uuid for f in flows],
        member_count=len(flows),
    )
    return [uf], flows


def test_catchall_splits_into_sub_object_journeys(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    ufs, flows = _investigations_fixture()
    original = ufs[0]
    tele = apply_journey_lattice(ufs, flows, [])
    assert tele["catchalls_split"] == 1
    assert tele["journeys_created"] == 3
    assert len(ufs) == 4
    # the original UF survives as the CORE (own resource) cluster
    assert original.id == "UF-001"
    assert original.name == "Create and manage investigations"
    assert set(original.member_flow_ids) == {"f1", "f2", "f3", "f4"}
    # conservation: every member lands in exactly one journey
    all_members = [m for u in ufs for m in u.member_flow_ids]
    assert len(all_members) == 10  # no duplicates across journeys
    assert set(all_members) == {f"f{i}" for i in range(1, 11)}
    children = {u.resource and u.id: u for u in ufs if u.id != "UF-001"}
    assert all(u.id.startswith("UF-lat-") for u in children.values())
    names = sorted(u.name for u in children.values())
    assert names == [
        "Manage investigation fork",
        "Manage investigation lifecycle",
        "Manage investigation notes",
    ]
    # binding note + flow backpointers
    for u in children.values():
        assert u.synthesis_reason == "lattice:route"
        assert u.synthesized is False
        for mid in u.member_flow_ids:
            fl = next(f for f in flows if f.uuid == mid)
            assert fl.user_flow_id == u.id


def test_below_three_clusters_untouched(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    ufs, flows = _investigations_fixture()
    # keep only core + lifecycle → 2 qualifying clusters
    keep = {"f1", "f2", "f3", "f4", "f5", "f6"}
    ufs[0].member_flow_ids = sorted(keep)
    flows = [f for f in flows if f.uuid in keep]
    tele = apply_journey_lattice(ufs, flows, [])
    assert tele["catchalls_split"] == 0
    assert len(ufs) == 1
    assert set(ufs[0].member_flow_ids) == keep


def test_thin_clusters_fold_into_core(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    ufs, flows = _investigations_fixture()
    # single 'feed' flow: 1 member, no line_ranges (0 LOC) — thin
    flows.append(_flow("f11", "get-api-investigations-feed-flow", _ENTRY))
    ufs[0].member_flow_ids = sorted(
        list(ufs[0].member_flow_ids) + ["f11"])
    tele = apply_journey_lattice(ufs, flows, [])
    assert tele["catchalls_split"] == 1
    core = next(u for u in ufs if u.id == "UF-001")
    assert "f11" in core.member_flow_ids  # folded, never lost


def test_subset_duplicate_merges(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    flows = [
        _flow("h1", "run-threat-hunt-flow", "backend/routers/hunts.py"),
        _flow("h2", "list-threat-hunts-flow", "backend/routers/hunts.py"),
        _flow("h3", "generate-detector-suggestions-flow",
              "backend/routers/hunts.py"),
    ]
    small = UserFlow(id="UF-010", name="Run threat hunts",
                     product_feature_id="threat-hunts", intent="execute",
                     resource="hunts", member_flow_ids=["h1", "h2"],
                     member_count=2)
    big = UserFlow(id="UF-011", name="Generate detector suggestions from hunts",
                   product_feature_id="threat-hunts", intent="execute",
                   resource="hunts", member_flow_ids=["h1", "h2", "h3"],
                   member_count=3)
    flows[0].user_flow_id = "UF-010"
    ufs = [small, big]
    tele = apply_journey_lattice(ufs, flows, [])
    assert tele["subset_merged"] == 1
    assert [u.id for u in ufs] == ["UF-011"]
    assert flows[0].user_flow_id == "UF-011"  # re-pointed, not dropped


def test_verifier_reject_keeps_catchall(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    ufs, flows = _investigations_fixture()
    before = sorted(ufs[0].member_flow_ids)

    def deny(items):
        assert items[0]["kind"] == "journey_lattice_split"
        return {items[0]["id"]: False}

    tele = apply_journey_lattice(ufs, flows, [], verifier=deny)
    assert tele["verifier_rejects"] == 1
    assert tele["catchalls_split"] == 0
    assert len(ufs) == 1
    assert sorted(ufs[0].member_flow_ids) == before  # untouched


def test_labeler_choice_overrides_template_name(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    ufs, flows = _investigations_fixture()

    def labeler(pending):
        assert all(p.kind == "user_flow" for p in pending)
        target = next(p for p in pending
                      if "notes" in p.current.lower())
        return {"choices": {target.key: "Annotate investigations"}}

    apply_journey_lattice(ufs, flows, [], labeler=labeler)
    assert any(u.name == "Annotate investigations" for u in ufs)


def test_kill_switch_and_skip_classes(monkeypatch):
    monkeypatch.setenv(_ENV, "0")
    ufs, flows = _investigations_fixture()
    tele = apply_journey_lattice(ufs, flows, [])
    assert tele["enabled"] is False and len(ufs) == 1

    monkeypatch.delenv(_ENV, raising=False)
    ufs, flows = _investigations_fixture()
    ufs[0].category = "system"
    tele = apply_journey_lattice(ufs, flows, [])
    assert tele["catchalls_split"] == 0 and len(ufs) == 1

    ufs, flows = _investigations_fixture()
    ufs[0].synthesized = True
    tele = apply_journey_lattice(ufs, flows, [])
    assert tele["catchalls_split"] == 0 and len(ufs) == 1


def test_deterministic_ids_and_repeat_stability(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    a_ufs, a_flows = _investigations_fixture()
    b_ufs, b_flows = _investigations_fixture()
    apply_journey_lattice(a_ufs, a_flows, [])
    apply_journey_lattice(b_ufs, b_flows, [])
    assert [(u.id, u.name, tuple(u.member_flow_ids)) for u in a_ufs] == \
           [(u.id, u.name, tuple(u.member_flow_ids)) for u in b_ufs]


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    assert lattice_enabled() is True
