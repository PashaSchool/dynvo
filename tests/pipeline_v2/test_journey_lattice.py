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


def test_verifier_reject_all_keeps_catchall(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    ufs, flows = _investigations_fixture()
    before = sorted(ufs[0].member_flow_ids)

    def deny_all(items):
        # per-child NAME drafts — the persona's native contract
        assert all(i["kind"] == "user_flow" for i in items)
        assert all(i["parent_journey"] for i in items)
        return {i["id"]: False for i in items}

    tele = apply_journey_lattice(ufs, flows, [], verifier=deny_all)
    assert tele["verifier_rejects"] == 3
    assert tele["catchalls_split"] == 0
    assert len(ufs) == 1
    assert sorted(ufs[0].member_flow_ids) == before  # untouched


def test_verifier_partial_reject_folds_child_into_core(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    ufs, flows = _investigations_fixture()

    def deny_notes(items):
        return {i["id"]: ("note" not in i["draft"].lower())
                for i in items}

    tele = apply_journey_lattice(ufs, flows, [], verifier=deny_notes)
    assert tele["verifier_rejects"] == 1
    assert tele["catchalls_split"] == 1
    assert tele["journeys_created"] == 2  # notes folded back into core
    core = next(u for u in ufs if u.id == "UF-001")
    assert {"f7", "f8"} <= set(core.member_flow_ids)  # conservation
    all_members = [m for u in ufs for m in u.member_flow_ids]
    assert len(all_members) == 10 and len(set(all_members)) == 10


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


def _dev(name, pfid, paths):
    return NS_DEV(name=name, layer="developer", product_feature_id=pfid,
                  paths=paths)


class NS_DEV:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_child_rehomes_to_entry_owner_pf(monkeypatch):
    """papermark UF-003 class: catch-all under pf=workflows carries
    team-api flows — the split child must live under the TEAMS pf
    (entry-owner majority), not inherit the parent's home (that would
    mint majority-foreign I15/I16 rows by construction)."""
    monkeypatch.delenv(_ENV, raising=False)
    ufs, flows = _investigations_fixture()
    devs = [
        _dev("investigations-core", "investigations",
             [_ENTRY]),
        _dev("notes-domain", "annotations",
             []),
    ]
    # notes flows enter through a file owned by the annotations PF
    notes_entry = "backend/routers/investigations_notes.py"
    for fl in flows:
        if fl.uuid in ("f7", "f8"):
            fl.entry_point_file = notes_entry
    devs[1].paths = [notes_entry]
    apply_journey_lattice(ufs, flows, [], features=devs)
    notes = next(u for u in ufs if "note" in u.name.lower())
    assert notes.product_feature_id == "annotations"
    lifecycle = next(u for u in ufs if "lifecycle" in u.name.lower())
    assert lifecycle.product_feature_id == "investigations"  # parent home


def test_dissolve_past_dominant_core_and_honest_resource(monkeypatch):
    """Soc0 fresh-draw class: 'network security' dump with a dominant
    widget_library trio — strays with homes leave, the core stays, the
    UF resource turns honest (naming stack stops templating the lie)."""
    monkeypatch.delenv(_ENV, raising=False)
    flows = [
        _flow("w1", "preview-widget-refresh-flow",
              "backend/routers/widget_library.py"),
        _flow("w2", "post-api-widget-library-widget-id-refresh-flow",
              "backend/routers/widget_library.py"),
        _flow("w3", "post-api-widget-library-preview-refresh-flow",
              "backend/routers/widget_library.py"),
        _flow("s1", "view-cases-list-flow", "frontend/src/pages/CasesPage.tsx"),
        _flow("s2", "view-dashboard-pages-flow",
              "frontend/src/pages/DashboardPage.tsx"),
        _flow("s3", "browse-knowledge-entries-flow",
              "frontend/src/pages/KnowledgePage.tsx"),
    ]
    dump = UserFlow(
        id="UF-042", name="Send network security",
        product_feature_id="network-security", intent="other",
        resource="network-security",
        member_flow_ids=[f.uuid for f in flows], member_count=6,
        synthesized=True,
        synthesis_reason="uncovered_product_feature_backstop",
    )
    cases = UserFlow(id="UF-010", name="Manage cases end-to-end",
                     product_feature_id="cases", intent="manage",
                     resource="cases", member_flow_ids=["x1"],
                     member_count=1)
    dash = UserFlow(id="UF-011", name="Build and browse dashboards",
                    product_feature_id="dashboard", intent="browse",
                    resource="dashboards", member_flow_ids=["x2"],
                    member_count=1)
    ufs = [dump, cases, dash]
    tele = apply_journey_lattice(ufs, flows, [])
    assert tele["garbage_dissolved"] == 1
    assert tele["members_rehomed"] == 2       # cases + dashboard strays
    assert "s1" in cases.member_flow_ids
    assert "s2" in dash.member_flow_ids
    # knowledge stray has no home → stays; widget core stays
    assert set(dump.member_flow_ids) == {"w1", "w2", "w3", "s3"}
    assert dump.resource == "widget_library"  # honest survivor resource
