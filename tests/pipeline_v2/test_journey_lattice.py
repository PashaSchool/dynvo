"""Stage 6.88 — journey lattice (Product-Spine W5).

Deterministic catch-all partition on evidence clusters, exact
subset-duplicate merge, canonical child identity, the persona seams
(labeler names / verifier reviews — never membership), and the
conservation guarantees (nothing is ever lost).
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, Flow, FlowNode, UserFlow
from faultline.pipeline_v2.journey_lattice import (
    _child_id,
    dedup_lattice_journeys,
    journey_lattice_enabled,
    run_journey_lattice,
)

_EPOCH = datetime.fromtimestamp(0, timezone.utc)


def _flow(name: str, entry: str, loc: int = 40) -> Flow:
    return Flow(
        name=name, uuid=name, entry_point_file=entry, paths=[entry],
        authors=[], total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_EPOCH, health_score=80.0,
        nodes=[FlowNode(
            id=f"{entry}#h", kind="entry", file=entry,
            lines=(1, max(1, loc)), role="entry", confidence="high",
        )],
    )


def _dev(name: str, paths: list[str], pfid: str | None,
         flows: list[Flow] | None = None) -> Feature:
    return Feature(
        name=name, paths=paths, authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_EPOCH, health_score=80.0,
        layer="developer", product_feature_id=pfid, flows=flows or [],
    )


def _pf(slug: str, display: str | None = None,
        anchor_id: str | None = None,
        paths: list[str] | None = None) -> Feature:
    f = Feature(
        name=slug, paths=paths or [], authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=_EPOCH,
        health_score=80.0, layer="product",
    )
    f.display_name = display or slug.replace("-", " ").title()
    if anchor_id is not None:
        f.anchor_id = anchor_id
    return f


def _uf(uf_id: str, name: str, pfid: str | None, members: list[str],
        category: str = "interactive", synthesized: bool = False) -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, intent="manage", resource="thing",
        product_feature_id=pfid, member_flow_ids=members,
        member_count=len(members), category=category,
        synthesized=synthesized,
    )


# ── The papermark UF-003 shape: teams-api catch-all on one capability ──


def _catchall_scene() -> tuple[list[UserFlow], list[Feature], list[Feature],
                               list[dict]]:
    flows = [
        # Capability core (route family == PF root word) — stays parent.
        _flow("create-workflow-flow", "pages/api/workflows/create.ts"),
        _flow("run-workflow-flow", "pages/api/workflows/run.ts"),
        _flow("list-workflows-flow", "pages/api/workflows/index.ts"),
        # Tenant-scoped foreign families (the /api/teams/{id}/* mass).
        _flow("verify-domain-flow",
              "pages/api/teams/[teamId]/domains/verify.ts"),
        _flow("remove-domain-flow",
              "pages/api/teams/[teamId]/domains/remove.ts"),
        _flow("create-token-flow", "pages/api/teams/[teamId]/tokens/new.ts"),
        _flow("revoke-token-flow",
              "pages/api/teams/[teamId]/tokens/revoke.ts"),
        _flow("connect-slack-flow",
              "pages/api/teams/[teamId]/slack/oauth.ts"),
        _flow("send-slack-alert-flow",
              "pages/api/teams/[teamId]/slack/notify.ts"),
    ]
    devs = [_dev("workflows-dev", [f.entry_point_file for f in flows],
                 "workflows", flows=flows)]
    pfs = [_pf("workflows", "Workflows", "route:pages/api/workflows")]
    routes = [
        {"file": "pages/api/workflows/create.ts",
         "pattern": "/api/workflows/create"},
        {"file": "pages/api/workflows/run.ts", "pattern": "/api/workflows/run"},
        {"file": "pages/api/workflows/index.ts", "pattern": "/api/workflows"},
        {"file": "pages/api/teams/[teamId]/domains/verify.ts",
         "pattern": "/api/teams/[teamId]/domains/verify"},
        {"file": "pages/api/teams/[teamId]/domains/remove.ts",
         "pattern": "/api/teams/[teamId]/domains/remove"},
        {"file": "pages/api/teams/[teamId]/tokens/new.ts",
         "pattern": "/api/teams/[teamId]/tokens/new"},
        {"file": "pages/api/teams/[teamId]/tokens/revoke.ts",
         "pattern": "/api/teams/[teamId]/tokens/revoke"},
        {"file": "pages/api/teams/[teamId]/slack/oauth.ts",
         "pattern": "/api/teams/[teamId]/slack/oauth"},
        {"file": "pages/api/teams/[teamId]/slack/notify.ts",
         "pattern": "/api/teams/[teamId]/slack/notify"},
    ]
    ufs = [_uf("UF-010", "Build automated workflows", "workflows",
               [f.name for f in flows])]
    return ufs, devs, pfs, routes


def test_catchall_splits_on_route_families_tenancy_transparent() -> None:
    ufs, devs, pfs, routes = _catchall_scene()
    tele = run_journey_lattice(ufs, devs, pfs, routes)

    assert tele["applied"] is True
    assert tele["catchalls_detected"] == 1
    assert tele["catchalls_split"] == 1
    assert tele["journeys_created"] == 3
    by_id = {u.id: u for u in ufs}

    # Parent survives with its OWN capability core, id + name kept.
    parent = by_id["UF-010"]
    assert parent.name == "Build automated workflows"
    assert sorted(parent.member_flow_ids) == [
        "create-workflow-flow", "list-workflows-flow", "run-workflow-flow",
    ]
    assert parent.member_count == 3

    children = [u for u in ufs if u.id.startswith("UF-L-")]
    assert len(children) == 3
    by_key = {u.domain: u for u in children}
    dom = by_key["lattice:route:domain"]
    tok = by_key["lattice:route:token"]
    slk = by_key["lattice:route:slack"]
    assert sorted(dom.member_flow_ids) == [
        "remove-domain-flow", "verify-domain-flow"]
    assert sorted(tok.member_flow_ids) == [
        "create-token-flow", "revoke-token-flow"]
    assert sorted(slk.member_flow_ids) == [
        "connect-slack-flow", "send-slack-alert-flow"]

    # Conservation: nothing lost, nothing duplicated.
    all_members = sorted(
        m for u in ufs for m in u.member_flow_ids
    )
    assert all_members == sorted(f.name for d in devs for f in d.flows)

    # Names: verb-led "<Verb> <object>"; brand casing rides the W3 vocab.
    assert slk.name == "Connect Slack"
    assert dom.name == "Manage domains"

    # Every child binds to the parent capability at construction.
    assert {u.product_feature_id for u in children} == {"workflows"}
    # Route evidence → high name confidence.
    assert {u.name_confidence for u in children} == {"high"}


def test_child_ids_are_canonical_and_rescan_stable() -> None:
    ufs1, devs1, pfs1, routes1 = _catchall_scene()
    run_journey_lattice(ufs1, devs1, pfs1, routes1)
    ufs2, devs2, pfs2, routes2 = _catchall_scene()
    # Same repo state re-scanned with a different member emission order.
    ufs2[0].member_flow_ids = list(reversed(ufs2[0].member_flow_ids))
    run_journey_lattice(ufs2, devs2, pfs2, routes2)

    ids1 = sorted(u.id for u in ufs1 if u.id.startswith("UF-L-"))
    ids2 = sorted(u.id for u in ufs2 if u.id.startswith("UF-L-"))
    assert ids1 == ids2
    assert _child_id("workflows", "domain") in ids1


def test_two_bucket_journey_is_not_a_catchall() -> None:
    flows = [
        _flow("verify-domain-flow",
              "pages/api/teams/[teamId]/domains/verify.ts"),
        _flow("remove-domain-flow",
              "pages/api/teams/[teamId]/domains/remove.ts"),
        _flow("create-token-flow", "pages/api/teams/[teamId]/tokens/new.ts"),
        _flow("revoke-token-flow",
              "pages/api/teams/[teamId]/tokens/revoke.ts"),
    ]
    devs = [_dev("teams-dev", [f.entry_point_file for f in flows],
                 "teams", flows=flows)]
    pfs = [_pf("teams", "Teams", "route:pages/api/teams")]
    routes = [
        {"file": f.entry_point_file,
         "pattern": "/" + f.entry_point_file.replace("pages/", "").replace(
             ".ts", "")}
        for f in flows
    ]
    ufs = [_uf("UF-001", "Manage team settings", "teams",
               [f.name for f in flows])]
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    # domains + tokens = 2 buckets < 3 — recognizable journey, untouched.
    assert tele["catchalls_detected"] == 0
    assert len(ufs) == 1
    assert ufs[0].member_count == 4


def test_single_family_journey_untouched() -> None:
    flows = [
        _flow(f"saml-step-{i}-flow", f"pages/api/auth/saml/step{i}.ts")
        for i in range(1, 6)
    ]
    devs = [_dev("auth-dev", [f.entry_point_file for f in flows], "auth",
                 flows=flows)]
    pfs = [_pf("auth", "Auth", "route:pages/api/auth")]
    routes = [
        {"file": f.entry_point_file, "pattern": f"/api/auth/saml/step{i}"}
        for i, f in enumerate(flows, start=1)
    ]
    ufs = [_uf("UF-002", "Sign in with SAML", "auth",
               [f.name for f in flows])]
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    assert tele["applied"] is False
    assert len(ufs) == 1
    assert ufs[0].member_count == 5


def test_loc_bar_rescues_single_flow_page_clusters() -> None:
    """The Soc0 garbage-bucket shape: one flow per foreign surface, each
    a full page — the >= 150 LOC arm mints them; a tiny stray folds."""
    flows = [
        _flow("view-chat-conversation-flow",
              "frontend/src/pages/chat/ChatPage.tsx", loc=400),
        _flow("view-reports-flow",
              "frontend/src/pages/reports/ReportsPage.tsx", loc=300),
        _flow("browse-knowledge-entries-flow",
              "frontend/src/pages/knowledge/KnowledgePage.tsx", loc=250),
        _flow("preview-widget-refresh-flow",
              "backend/widget_library.py", loc=10),  # sub-bar stray
    ]
    devs = [_dev("netsec-dev", [f.entry_point_file for f in flows],
                 "network-security", flows=flows)]
    pfs = [_pf("network-security", "Network Security",
               "fdir:frontend/src/modules/network-security")]
    ufs = [_uf("UF-003", "View network security", "network-security",
               [f.name for f in flows])]
    tele = run_journey_lattice(ufs, devs, pfs, routes_index=[])

    assert tele["catchalls_split"] == 1
    children = [u for u in ufs if u.id.startswith("UF-L-")]
    keys = sorted(u.domain for u in children)
    assert keys == [
        "lattice:dir:chat", "lattice:dir:knowledge", "lattice:dir:report",
    ]
    # Dir evidence only → low name confidence (labeler's to polish).
    assert {u.name_confidence for u in children} == {"low"}
    # The sub-bar stray stays with the surviving parent (never lost).
    parent = next(u for u in ufs if u.id == "UF-003")
    assert parent.member_flow_ids == ["preview-widget-refresh-flow"]


def test_parent_dissolves_when_every_member_leaves() -> None:
    flows = [
        _flow("view-chat-conversation-flow",
              "frontend/src/pages/chat/ChatPage.tsx", loc=400),
        _flow("view-reports-flow",
              "frontend/src/pages/reports/ReportsPage.tsx", loc=300),
        _flow("browse-knowledge-entries-flow",
              "frontend/src/pages/knowledge/KnowledgePage.tsx", loc=250),
    ]
    devs = [_dev("netsec-dev", [f.entry_point_file for f in flows],
                 "network-security", flows=flows)]
    pfs = [_pf("network-security", "Network Security",
               "fdir:frontend/src/modules/network-security")]
    ufs = [_uf("UF-003", "View network security", "network-security",
               [f.name for f in flows])]
    tele = run_journey_lattice(ufs, devs, pfs, routes_index=[])

    assert tele["parents_dissolved"] == 1
    assert all(u.id != "UF-003" for u in ufs)
    # Members conserved across the children.
    assert sorted(m for u in ufs for m in u.member_flow_ids) == sorted(
        f.name for f in flows)
    # No memberless journey was emitted (I7 safety).
    assert all(u.member_flow_ids for u in ufs)


def test_subset_duplicate_merges_the_hunts_case() -> None:
    flows = [_flow(f"hunt-{i}-flow", f"api/hunts/h{i}.py") for i in range(6)]
    devs = [_dev("hunts-dev", [f.entry_point_file for f in flows],
                 "threat-hunts", flows=flows)]
    pfs = [_pf("threat-hunts", "Threat Hunts", "route:api/hunts")]
    subset = [f.name for f in flows[:4]]
    superset = [f.name for f in flows]
    ufs = [
        _uf("UF-005", "Run threat hunts", "threat-hunts", subset),
        _uf("UF-006", "Generate detector suggestions from hunts",
            "threat-hunts", superset),
    ]
    tele = run_journey_lattice(ufs, devs, pfs, routes_index=[])
    assert tele["subset_merged"] == 1
    assert [u.id for u in ufs] == ["UF-006"]
    assert ufs[0].member_count == 6
    assert tele["subset_merged_pairs"][0]["dropped"] == "Run threat hunts"


def test_equal_member_sets_keep_the_smaller_id() -> None:
    flows = [_flow(f"x-{i}-flow", f"api/x/{i}.py") for i in range(3)]
    devs = [_dev("x-dev", [f.entry_point_file for f in flows], "x",
                 flows=flows)]
    pfs = [_pf("x", "X Things", "route:api/x")]
    members = [f.name for f in flows]
    ufs = [
        _uf("UF-001", "Manage x", "x", list(members)),
        _uf("UF-002", "Handle x", "x", list(members)),
    ]
    run_journey_lattice(ufs, devs, pfs, routes_index=[])
    assert [u.id for u in ufs] == ["UF-001"]


def test_subset_across_different_pfs_is_untouched() -> None:
    flows = [_flow(f"y-{i}-flow", f"api/y/{i}.py") for i in range(4)]
    devs = [_dev("y-dev", [f.entry_point_file for f in flows], "y",
                 flows=flows)]
    pfs = [_pf("y", "Y"), _pf("z", "Z")]
    ufs = [
        _uf("UF-001", "A", "y", [f.name for f in flows[:2]]),
        _uf("UF-002", "B", "z", [f.name for f in flows]),
    ]
    run_journey_lattice(ufs, devs, pfs, routes_index=[])
    assert [u.id for u in ufs] == ["UF-001", "UF-002"]


def test_verifier_reject_keeps_the_catchall_byte_identical() -> None:
    ufs, devs, pfs, routes = _catchall_scene()
    before = [u.model_dump() for u in ufs]

    def _verifier(items: list[dict]) -> dict[str, bool]:
        assert items and items[0]["kind"] == "lattice_split"
        assert items[0]["id"] == "workflows"
        return {items[0]["id"]: False}

    tele = run_journey_lattice(ufs, devs, pfs, routes, verifier=_verifier)
    assert tele["verifier_rejects"] == 1
    assert tele["journeys_created"] == 0
    assert [u.model_dump() for u in ufs] == before


def test_verifier_accept_and_missing_verdict_apply() -> None:
    ufs, devs, pfs, routes = _catchall_scene()

    def _verifier(items: list[dict]) -> dict[str, bool]:
        return {}  # missing verdict defaults to ACCEPT

    tele = run_journey_lattice(ufs, devs, pfs, routes, verifier=_verifier)
    assert tele["journeys_created"] == 3


def test_labeler_picks_are_law_checked_and_applied() -> None:
    ufs, devs, pfs, routes = _catchall_scene()
    dom_id = _child_id("workflows", "domain")
    tok_id = _child_id("workflows", "token")

    def _labeler(pending: list) -> dict:
        assert all(item.kind == "uf" for item in pending)
        return {"choices": {
            dom_id: "Configure custom domains",
            tok_id: "Workflows",  # PF==UF twin — must be discarded
        }}

    run_journey_lattice(ufs, devs, pfs, routes, labeler=_labeler)
    by_id = {u.id: u for u in ufs}
    assert by_id[dom_id].name == "Configure custom domains"
    assert by_id[tok_id].name == "Manage tokens"  # deterministic name kept


def test_interior_section_axis_and_key_token_subset_merge() -> None:
    """A route family and its authored section label are ONE object:
    'ai' (route) absorbs 'ai-copilot' (section) — no twin journeys."""
    flows = [
        _flow("get-ai-chat-flow", "pages/api/ai/chat.ts", loc=30),
        _flow("retrieve-chat-history-flow", "pages/api/ai/history.ts",
              loc=30),
        _flow("view-chat-panel-flow", "pages/datarooms/[id]/ai.tsx",
              loc=200),
        _flow("upload-document-flow", "pages/api/documents/upload.ts",
              loc=30),
        _flow("index-document-flow", "pages/api/documents/index-doc.ts",
              loc=30),
        _flow("create-dataroom-flow", "pages/api/datarooms/create.ts",
              loc=30),
    ]
    devs = [_dev("datarooms-dev", [f.entry_point_file for f in flows],
                 "datarooms", flows=flows)]
    pfs = [_pf("datarooms", "Datarooms", "route:pages/api/datarooms")]
    routes = [
        {"file": "pages/api/ai/chat.ts", "pattern": "/api/ai/chat"},
        {"file": "pages/api/ai/history.ts", "pattern": "/api/ai/history"},
        {"file": "pages/api/documents/upload.ts",
         "pattern": "/api/documents/upload"},
        {"file": "pages/api/documents/index-doc.ts",
         "pattern": "/api/documents/index-doc"},
        {"file": "pages/api/datarooms/create.ts",
         "pattern": "/api/datarooms/create"},
    ]
    interior = {
        "by_pf": {"Datarooms": ["AI Copilot"]},
        "pages": {
            "pages/datarooms/[id]/ai.tsx": {
                "pf": "Datarooms", "sections": ["AI Copilot"],
            },
        },
    }
    ufs = [_uf("UF-008", "Create and manage datarooms", "datarooms",
               [f.name for f in flows])]
    tele = run_journey_lattice(
        ufs, devs, pfs, routes, interior_evidence=interior)

    assert tele["catchalls_split"] == 1
    children = {u.domain: u for u in ufs if u.id.startswith("UF-L-")}
    # 'ai-copilot' (section) merged INTO 'ai' (route root key); the
    # authored section phrase wins the display.
    assert "lattice:section:ai" in children
    ai = children["lattice:section:ai"]
    assert sorted(ai.member_flow_ids) == [
        "get-ai-chat-flow", "retrieve-chat-history-flow",
        "view-chat-panel-flow",
    ]
    assert "copilot" in ai.name.lower()
    assert "lattice:route:document" in children
    # Parent keeps the capability-core dataroom flow.
    parent = next(u for u in ufs if u.id == "UF-008")
    assert parent.member_flow_ids == ["create-dataroom-flow"]


def test_cross_pf_interior_page_is_ignored() -> None:
    flows = [
        _flow("view-panel-flow", "pages/other/panel.tsx", loc=10),
        _flow("edit-thing-flow", "src/things/edit.ts", loc=10),
    ]
    devs = [_dev("things-dev", [f.entry_point_file for f in flows],
                 "things", flows=flows)]
    pfs = [_pf("things", "Things")]
    interior = {
        "by_pf": {"Other": ["Panel"]},
        "pages": {
            "pages/other/panel.tsx": {"pf": "Other", "sections": ["Panel"]},
        },
    }
    ufs = [_uf("UF-001", "Manage things", "things",
               [f.name for f in flows])]
    tele = run_journey_lattice(
        ufs, devs, pfs, [], interior_evidence=interior)
    # The foreign page contributes NO section evidence to this PF.
    assert tele["catchalls_detected"] == 0


def test_system_and_synthesized_journeys_are_untouched() -> None:
    flows = [
        _flow("job-a-flow", "jobs/a/run.py", loc=400),
        _flow("job-b-flow", "jobs/b/run.py", loc=400),
        _flow("job-c-flow", "jobs/c/run.py", loc=400),
    ]
    devs = [_dev("jobs-dev", [f.entry_point_file for f in flows], "jobs",
                 flows=flows)]
    pfs = [_pf("jobs", "Jobs")]
    ufs = [
        _uf("UF-001", "Run jobs", "jobs", [f.name for f in flows],
            category="system"),
        _uf("UF-002", "Seeded journey", "jobs", [flows[0].name],
            synthesized=True),
    ]
    tele = run_journey_lattice(ufs, devs, pfs, routes_index=[])
    assert tele["applied"] is False
    assert tele["eligible_ufs"] == 0
    assert [u.id for u in ufs] == ["UF-001", "UF-002"]


def test_dangling_member_ids_stay_with_the_parent() -> None:
    ufs, devs, pfs, routes = _catchall_scene()
    ufs[0].member_flow_ids = ufs[0].member_flow_ids + ["ghost-flow"]
    ufs[0].member_count += 1
    run_journey_lattice(ufs, devs, pfs, routes)
    parent = next(u for u in ufs if u.id == "UF-010")
    assert "ghost-flow" in parent.member_flow_ids


def test_kill_switch_disables(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_JOURNEY_LATTICE", "0")
    assert journey_lattice_enabled() is False
    monkeypatch.setenv("FAULTLINE_JOURNEY_LATTICE", "1")
    assert journey_lattice_enabled() is True


def test_dedup_merges_same_key_children_after_resettle() -> None:
    """Two capabilities' catch-alls each shed a 'domains' cluster;
    conservation re-homed both children onto the domains capability —
    they are ONE journey."""
    a = UserFlow(
        id=_child_id("workflows", "domain"), name="Manage domains",
        intent="manage", resource="domains", product_feature_id="domains",
        member_flow_ids=["f1", "f2"], member_count=2,
        domain="lattice:route:domain",
    )
    b = UserFlow(
        id=_child_id("teams", "domain"), name="Manage domains",
        intent="manage", resource="domains", product_feature_id="domains",
        member_flow_ids=["f2", "f3"], member_count=2,
        domain="lattice:route:domain",
    )
    keep_first = min(a.id, b.id)
    ufs = [a, b]
    tele = dedup_lattice_journeys(ufs)
    assert tele["merged"] == 1
    assert len(ufs) == 1
    assert ufs[0].id == keep_first
    assert sorted(ufs[0].member_flow_ids) == ["f1", "f2", "f3"]


def test_dedup_folds_child_into_existing_resource_match() -> None:
    existing = _uf("UF-004", "Configure custom domains", "domains",
                   ["e1", "e2"])
    existing.resource = "domains"
    child = UserFlow(
        id=_child_id("workflows", "domain"), name="Manage domains",
        intent="manage", resource="domains", product_feature_id="domains",
        member_flow_ids=["f1", "f2"], member_count=2,
        domain="lattice:route:domain",
    )
    ufs = [existing, child]
    tele = dedup_lattice_journeys(ufs)
    assert tele["into_existing"] == 1
    assert [u.id for u in ufs] == ["UF-004"]
    assert sorted(ufs[0].member_flow_ids) == ["e1", "e2", "f1", "f2"]


def test_crud_leaf_segments_never_become_families() -> None:
    """/investigations/{id}/edit is the SAME journey as the
    investigations core — the actor+intent+outcome guard."""
    flows = [
        _flow("edit-investigation-flow",
              "api/investigations/edit.py", loc=40),
        _flow("create-investigation-flow",
              "api/investigations/create.py", loc=40),
        _flow("add-note-flow", "api/investigations/notes/add.py", loc=40),
        _flow("list-notes-flow", "api/investigations/notes/list.py", loc=40),
        _flow("attach-evidence-flow",
              "api/investigations/evidence/attach.py", loc=40),
        _flow("list-evidence-flow",
              "api/investigations/evidence/list.py", loc=40),
        _flow("view-timeline-flow",
              "api/investigations/timeline/view.py", loc=40),
        _flow("replay-timeline-flow",
              "api/investigations/timeline/replay.py", loc=40),
    ]
    devs = [_dev("inv-dev", [f.entry_point_file for f in flows],
                 "investigations", flows=flows)]
    pfs = [_pf("investigations", "Investigations",
               "route:api/investigations")]
    routes = [
        {"file": "api/investigations/edit.py",
         "pattern": "/api/investigations/{id}/edit"},
        {"file": "api/investigations/create.py",
         "pattern": "/api/investigations/create"},
        {"file": "api/investigations/notes/add.py",
         "pattern": "/api/investigations/{id}/notes/add"},
        {"file": "api/investigations/notes/list.py",
         "pattern": "/api/investigations/{id}/notes"},
        {"file": "api/investigations/evidence/attach.py",
         "pattern": "/api/investigations/{id}/evidence/attach"},
        {"file": "api/investigations/evidence/list.py",
         "pattern": "/api/investigations/{id}/evidence"},
        {"file": "api/investigations/timeline/view.py",
         "pattern": "/api/investigations/{id}/timeline"},
        {"file": "api/investigations/timeline/replay.py",
         "pattern": "/api/investigations/{id}/timeline/replay"},
    ]
    ufs = [_uf("UF-020", "Create and manage investigations",
               "investigations", [f.name for f in flows])]
    tele = run_journey_lattice(ufs, devs, pfs, routes)

    # notes/evidence/timeline mint; edit/create stay on the parent core.
    assert tele["catchalls_split"] == 1
    children = {u.domain: u for u in ufs if u.id.startswith("UF-L-")}
    assert set(children) == {
        "lattice:route:note", "lattice:route:evidence",
        "lattice:route:timeline",
    }
    parent = next(u for u in ufs if u.id == "UF-020")
    assert sorted(parent.member_flow_ids) == [
        "create-investigation-flow", "edit-investigation-flow"]


def test_api_tier_segments_are_transparent_with_deeper_object() -> None:
    """/api/v1/management/surveys keys 'surveys' (tier transparent);
    /api/clients/{id} keeps 'client' (CRM class — no deeper object)."""
    from faultline.pipeline_v2.journey_lattice import _route_family
    from faultline.pipeline_v2.spine_anchors import load_spine_vocab
    import re as _re

    vocab = load_spine_vocab()
    vre = _re.compile(vocab.get("version_segment_pattern") or r"^v\d+$")
    pbf = {
        "a.ts": ["/api/v1/management/surveys"],
        "b.ts": ["/api/v1/client/[envId]/responses"],
        "c.ts": ["/api/clients/[id]"],
    }
    assert _route_family("a.ts", pbf, frozenset(), vocab, vre) == (
        "survey", "surveys")
    assert _route_family("b.ts", pbf, frozenset(), vocab, vre) == (
        "response", "responses")
    assert _route_family("c.ts", pbf, frozenset(), vocab, vre) == (
        "client", "clients")


def test_tier_only_route_collapses_to_core() -> None:
    """A management-API route of the capability itself has no foreign
    object — flows stay on the parent (no tier-named journey)."""
    flows = [
        _flow("list-surveys-flow", "api/mgmt/surveys/list.ts"),
        _flow("client-surveys-flow", "api/client/surveys/get.ts"),
        _flow("send-templates-flow", "api/templates/send.ts", loc=200),
        _flow("view-health-flow", "api/health/view.ts", loc=200),
        _flow("edit-workspace-flow", "api/workspaces/edit.ts", loc=200),
    ]
    devs = [_dev("survey-dev", [f.entry_point_file for f in flows],
                 "survey", flows=flows)]
    pfs = [_pf("survey", "Survey", "route:api/surveys")]
    routes = [
        {"file": "api/mgmt/surveys/list.ts",
         "pattern": "/api/v1/management/surveys"},
        {"file": "api/client/surveys/get.ts",
         "pattern": "/api/v1/client/[envId]/surveys"},
        {"file": "api/templates/send.ts", "pattern": "/api/v1/templates"},
        {"file": "api/health/view.ts", "pattern": "/api/health"},
        {"file": "api/workspaces/edit.ts", "pattern": "/api/workspaces"},
    ]
    ufs = [_uf("UF-001", "Manage surveys", "survey",
               [f.name for f in flows])]
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    parent = next(u for u in ufs if u.id == "UF-001")
    # The two tier-routed survey flows stay home; no 'management'/'client'
    # journey exists.
    assert sorted(parent.member_flow_ids) == [
        "client-surveys-flow", "list-surveys-flow"]
    assert all("management" not in (u.domain or "")
               and ":client" not in (u.domain or "") for u in ufs)
    assert tele["catchalls_split"] == 1
