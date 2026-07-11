"""Stage 6.88 journey lattice — ACTION axis (W5.2 / fix B6).

The object axis (route/section/dir) cannot separate a single-router REST
surface: ``_route_family`` keys off the flow's entry FILE and returns the
first meaningful segment across all of that file's patterns, so a FastAPI
``routers/cases.py`` with 29 endpoints collapses to ONE route family and the
journey ships as an unrecognizable catch-all ("Manage cases end-to-end",
mc=35 — the Soc0 operator exhibit). The action axis partitions such journeys
by USER INTENT (the leading verb of each member flow name).

Anti-cases (operator mandate): single-intent journeys must NOT split —
wizard/editor chains that are one intent (build-resume), CRUD-of-one-resource
with one flow per action (manage-webhooks mc=3), and journeys the object axis
already split.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature, Flow, FlowNode, UserFlow
from faultline.pipeline_v2.journey_lattice import (
    JOURNEY_LATTICE_B7_ENV,
    JOURNEY_LATTICE_B25_ENV,
    JOURNEY_LATTICE_V2_ENV,
    _action_family,
    _child_id,
    journey_lattice_b7_enabled,
    journey_lattice_b25_enabled,
    journey_lattice_v2_enabled,
    load_action_families,
    run_journey_lattice,
)

_EPOCH = datetime.fromtimestamp(0, timezone.utc)

# tests/pipeline_v2/ is two levels below the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _flow(name: str, entry: str, loc: int = 40) -> Flow:
    return Flow(
        name=name, uuid=name, entry_point_file=entry, paths=[entry],
        authors=[], total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_EPOCH, health_score=80.0,
        nodes=[FlowNode(
            id=f"{entry}#{name}", kind="entry", file=entry,
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


def _pf(slug: str, display: str | None = None) -> Feature:
    f = Feature(
        name=slug, paths=[], authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=_EPOCH,
        health_score=80.0, layer="product",
    )
    f.display_name = display or slug.replace("-", " ").title()
    return f


def _uf(uf_id: str, name: str, pfid: str | None, members: list[str],
        category: str = "interactive", synthesized: bool = False,
        resource: str = "thing", domain: str | None = None) -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, intent="manage", resource=resource,
        product_feature_id=pfid, member_flow_ids=members,
        member_count=len(members), category=category,
        synthesized=synthesized, domain=domain,
    )


# ── The Soc0 UF-037 shape: single-router REST surface ───────────────────
#
# One FastAPI router file carries every endpoint; the sorted-first meaningful
# pattern segment ("summary") wins for EVERY member, so the object axis sees
# ONE family and never splits — this is the forensic root cause, reproduced.

_CASES_ROUTER = "backend/routers/cases.py"

_CASES_FLOW_NAMES = [
    # browse (collection reads)
    "list-cases-flow",
    "get-api-cases-summary-flow",
    # view (member reads — id marker)
    "get-api-cases-case-id-flow",
    "get-api-cases-case-id-timeline-flow",
    # create
    "post-api-cases-flow",
    "post-api-cases-bulk-flow",
    # update
    "patch-api-cases-case-id-flow",
    "update-case-flow",
    # act (domain actions)
    "investigate-case-with-agent-flow",
    "review-case-findings-flow",
]


def _cases_scene() -> tuple[list[UserFlow], list[Feature], list[Feature],
                            list[dict]]:
    flows = [_flow(n, _CASES_ROUTER) for n in _CASES_FLOW_NAMES]
    devs = [_dev("api-cases", [_CASES_ROUTER], "cases", flows=flows)]
    pfs = [_pf("cases", "Cases")]
    routes = [
        {"file": _CASES_ROUTER, "pattern": "/api/cases"},
        {"file": _CASES_ROUTER, "pattern": "/api/cases/summary"},
        {"file": _CASES_ROUTER, "pattern": "/api/cases/bulk"},
        {"file": _CASES_ROUTER, "pattern": "/api/cases/{case_id}"},
        {"file": _CASES_ROUTER, "pattern": "/api/cases/{case_id}/timeline"},
    ]
    ufs = [_uf("UF-037", "Manage cases end-to-end", "cases",
               [f.name for f in flows], resource="case")]
    return ufs, devs, pfs, routes


# ── Vocab + flag plumbing ────────────────────────────────────────────────


@pytest.mark.skipif(
    not (_REPO_ROOT / "eval").exists(),
    reason="eval/ is local/private-only (scrubbed 2026-07-11)",
)
def test_vocab_drift_guard_packaged_equals_eval_copy() -> None:
    packaged = (
        _REPO_ROOT / "faultline" / "pipeline_v2" / "data"
        / "journey-action-families.yaml"
    ).read_bytes()
    authoring = (
        _REPO_ROOT / "eval" / "journey-action-families.yaml").read_bytes()
    assert packaged == authoring, (
        "journey-action-families vocab drift: faultline/pipeline_v2/data/ "
        "and eval/ copies must stay byte-identical"
    )


def test_vocab_loads_with_expected_families() -> None:
    vocab = load_action_families()
    for fam in ("browse", "read", "create", "update", "delete", "act"):
        assert vocab.get(fam), f"family {fam!r} missing/empty"
    assert int(vocab.get("min_action_families", 0)) >= 2


def test_v2_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(JOURNEY_LATTICE_V2_ENV, raising=False)
    assert journey_lattice_v2_enabled() is True
    monkeypatch.setenv(JOURNEY_LATTICE_V2_ENV, "0")
    assert journey_lattice_v2_enabled() is False


# ── Classifier units ─────────────────────────────────────────────────────


def test_action_family_classifier() -> None:
    vocab = load_action_families()
    # collection vs member reads (the id marker)
    assert _action_family("list-cases-flow", vocab) == "browse"
    assert _action_family("get-api-cases-summary-flow", vocab) == "browse"
    assert _action_family("get-api-cases-case-id-flow", vocab) == "view"
    assert _action_family("retrieve-category-flow", vocab) == "browse"
    # writes
    assert _action_family("post-api-cases-flow", vocab) == "create"
    assert _action_family("patch-api-cases-case-id-flow", vocab) == "update"
    assert _action_family("delete-workflow-flow", vocab) == "delete"
    # domain actions
    assert _action_family("investigate-case-with-agent-flow", vocab) == "act"
    assert _action_family("review-case-findings-flow", vocab) == "act"
    # HEAD verb only — no secondary-token fallback: unclassified heads fold
    # to residual (precision over recall). "build"/"manage" are deliberately
    # NOT action verbs (UI-shell / container words).
    assert _action_family("build-resume-on-desktop-flow", vocab) is None
    assert _action_family("manage-resume-sections-flow", vocab) is None
    assert _action_family("format-date-display-flow", vocab) is None
    assert _action_family("", vocab) is None


# ── The fix: single-router catch-all splits by intent ───────────────────


def test_single_router_catchall_splits_by_action() -> None:
    ufs, devs, pfs, routes = _cases_scene()
    tele = run_journey_lattice(ufs, devs, pfs, routes)

    assert tele["applied"] is True
    assert tele.get("action_catchalls_detected") == 1
    children = [u for u in ufs if str(u.domain or "").startswith(
        "lattice:action:")]
    assert len(children) == 5

    by_domain = {u.domain: u for u in children}
    assert sorted(by_domain) == [
        "lattice:action:case-act",
        "lattice:action:case-browse",
        "lattice:action:case-create",
        "lattice:action:case-update",
        "lattice:action:case-view",
    ]
    assert sorted(by_domain["lattice:action:case-browse"].member_flow_ids) == [
        "get-api-cases-summary-flow", "list-cases-flow"]
    assert sorted(by_domain["lattice:action:case-view"].member_flow_ids) == [
        "get-api-cases-case-id-flow", "get-api-cases-case-id-timeline-flow"]
    assert sorted(by_domain["lattice:action:case-create"].member_flow_ids) == [
        "post-api-cases-bulk-flow", "post-api-cases-flow"]
    assert sorted(by_domain["lattice:action:case-update"].member_flow_ids) == [
        "patch-api-cases-case-id-flow", "update-case-flow"]
    assert sorted(by_domain["lattice:action:case-act"].member_flow_ids) == [
        "investigate-case-with-agent-flow", "review-case-findings-flow"]

    # W3-lawful deterministic names: "<ActionWord> <resource>".
    assert by_domain["lattice:action:case-browse"].name == "Browse cases"
    assert by_domain["lattice:action:case-view"].name == "View cases"
    assert by_domain["lattice:action:case-create"].name == "Create cases"
    assert by_domain["lattice:action:case-update"].name == "Update cases"
    assert by_domain["lattice:action:case-act"].name == "Manage cases"

    # PF attach stays; resource inherited from the parent journey.
    for ch in children:
        assert ch.product_feature_id == "cases"
        assert ch.resource == "case"
        assert str(ch.id).startswith("UF-L-")

    # Existing lattice convention: every member claimed → parent dissolves.
    assert "UF-037" not in {str(u.id) for u in ufs}

    # Conservation (I13/I14 substrate): children partition the parent's
    # member union exactly — nothing lost, nothing duplicated.
    all_members = sorted(m for u in ufs for m in u.member_flow_ids)
    assert all_members == sorted(_CASES_FLOW_NAMES)
    assert tele["conservation_reverts"] == 0


def test_child_ids_content_derived_and_stable() -> None:
    ufs, devs, pfs, routes = _cases_scene()
    run_journey_lattice(ufs, devs, pfs, routes)
    browse = next(u for u in ufs
                  if u.domain == "lattice:action:case-browse")
    assert str(browse.id) == _child_id("cases", "action:case-browse")

    # Rescan stability: a fresh identical scene mints identical ids.
    ufs2, devs2, pfs2, routes2 = _cases_scene()
    run_journey_lattice(ufs2, devs2, pfs2, routes2)
    assert {str(u.id) for u in ufs} == {str(u.id) for u in ufs2}


def test_kill_switch_restores_object_only_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(JOURNEY_LATTICE_V2_ENV, "0")
    ufs, devs, pfs, routes = _cases_scene()
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    assert tele.get("action_catchalls_detected", 0) == 0
    assert [str(u.id) for u in ufs] == ["UF-037"]
    assert sorted(ufs[0].member_flow_ids) == sorted(_CASES_FLOW_NAMES)


# ── Anti-cases: single-intent journeys must NOT split ────────────────────


def test_editor_wizard_chain_not_split() -> None:
    """The reactive-resume 'Build resume in the editor' class: one editor
    surface whose members are section facets (edit-basics/education/skills)
    + preview + shells. Only 2 action families (browse, update) — below the
    >=3 full-CRUD signature — so it stays ONE journey."""
    entry = "apps/web/src/features/builder/editor.tsx"
    names = [
        "preview-resume-layout-flow",       # browse
        "access-version-history-flow",      # browse
        "edit-resume-basics-flow",          # update
        "edit-resume-education-flow",       # update
        "edit-resume-skills-flow",          # update
        "build-resume-on-desktop-flow",     # residual (shell verb)
        "build-resume-on-mobile-flow",      # residual (shell verb)
        "manage-resume-sections-flow",      # residual (container verb)
    ]
    flows = [_flow(n, entry) for n in names]
    devs = [_dev("builder", [entry], "builder", flows=flows)]
    pfs = [_pf("builder", "Builder")]
    ufs = [_uf("UF-003", "Build resume in the editor", "builder",
               [f.name for f in flows], resource="builder")]
    tele = run_journey_lattice(ufs, devs, pfs, [])
    assert tele.get("action_catchalls_detected", 0) == 0
    assert [str(u.id) for u in ufs] == ["UF-003"]
    assert sorted(ufs[0].member_flow_ids) == sorted(names)


def test_small_crud_of_one_resource_not_split() -> None:
    """'Manage webhooks' mc=3, one flow per action: no family reaches the
    corroboration bar (>= 2 members, strict) → zero qualifying families →
    stays one journey."""
    entry = "app/api/webhooks/route.ts"
    names = ["create-webhook-flow", "delete-webhook-flow",
             "list-webhooks-flow"]
    flows = [_flow(n, entry) for n in names]
    devs = [_dev("webhooks", [entry], "webhooks", flows=flows)]
    pfs = [_pf("webhooks", "Webhooks")]
    routes = [{"file": entry, "pattern": "/api/webhooks"}]
    ufs = [_uf("UF-020", "Manage webhooks", "webhooks",
               [f.name for f in flows], resource="webhook")]
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    assert tele.get("action_catchalls_detected", 0) == 0
    assert [str(u.id) for u in ufs] == ["UF-020"]


def _dual_axis_scene() -> tuple[list[UserFlow], list[Feature], list[Feature],
                                list[dict]]:
    """A catch-all that qualifies on BOTH axes (papermark teams-api scene
    from the W5 suite): distinct route families (domain/token/slack) give
    the object axis its buckets, while the member verbs (create ×2,
    delete ×2, act ×3) form the >=3-family full-CRUD signature the action
    axis needs. Pass-1 precedence goes to the object axis; B25 releases
    the slot when the Draft Verifier fully reverts that plan."""
    flows = [
        _flow("create-workflow-flow", "pages/api/workflows/create.ts"),
        _flow("run-workflow-flow", "pages/api/workflows/run.ts"),
        _flow("list-workflows-flow", "pages/api/workflows/index.ts"),
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
    pfs = [_pf("workflows", "Workflows")]
    routes = [
        {"file": "pages/api/workflows/create.ts",
         "pattern": "/api/workflows/create"},
        {"file": "pages/api/workflows/run.ts",
         "pattern": "/api/workflows/run"},
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


def test_object_axis_split_takes_precedence() -> None:
    """A journey the OBJECT axis splits is never re-touched by the action
    axis (papermark teams-api scene from the W5 suite: distinct route
    families → object split fires; action pass must skip it)."""
    ufs, devs, pfs, routes = _dual_axis_scene()
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    assert tele["catchalls_split"] == 1
    assert tele.get("action_catchalls_detected", 0) == 0
    assert not any(
        str(u.domain or "").startswith("lattice:action:") for u in ufs)


def test_only_largest_sibling_splits_per_pf() -> None:
    """Two organic CRUD-shaped journeys on the SAME capability: only the
    largest action-splits (content-derived child ids would collide if both
    did); the sibling stays byte-intact and no duplicate ids appear."""
    ufs, devs, pfs, routes = _cases_scene()
    sibling_names = [
        "list-case-columns-flow",           # browse
        "get-case-columns-defaults-flow",   # browse
        "post-case-columns-flow",           # create
        "post-case-columns-copy-flow",      # create
        "patch-case-columns-order-flow",    # update
        "set-case-columns-width-flow",      # update
    ]
    sib_flows = [_flow(n, _CASES_ROUTER) for n in sibling_names]
    devs[0].flows.extend(sib_flows)
    ufs.append(_uf("UF-036", "Manage case columns", "cases",
                   sibling_names, resource="case"))
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    assert tele.get("action_catchalls_detected") == 1
    sib = next(u for u in ufs if str(u.id) == "UF-036")
    assert sorted(sib.member_flow_ids) == sorted(sibling_names)
    ids = [str(u.id) for u in ufs]
    assert len(ids) == len(set(ids)), "duplicate journey ids after split"


def test_lattice_born_journeys_are_exempt() -> None:
    """The action axis operates on ORGANIC journeys only — a lattice-born
    child (UF-L id / lattice: domain) is never re-partitioned."""
    ufs, devs, pfs, routes = _cases_scene()
    ufs[0].id = "UF-L-0123456789"
    ufs[0].domain = "lattice:route:case"
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    assert tele.get("action_catchalls_detected", 0) == 0
    assert [str(u.id) for u in ufs] == ["UF-L-0123456789"]


def test_synthesized_and_non_interactive_ineligible() -> None:
    ufs, devs, pfs, routes = _cases_scene()
    ufs[0].synthesized = True
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    assert tele.get("action_catchalls_detected", 0) == 0

    ufs2, devs2, pfs2, routes2 = _cases_scene()
    ufs2[0].category = "system"
    tele2 = run_journey_lattice(ufs2, devs2, pfs2, routes2)
    assert tele2.get("action_catchalls_detected", 0) == 0


def test_action_domain_resource_not_split() -> None:
    """The onyx 'Sign in and verify identity' class: the journey's resource
    ('auth') is itself a flow-verb-class word — an ACTION DOMAIN, not a
    countable entity. CRUD partition there is grain-noise ("Create auth"
    from POST /logout) → the journey is exempt."""
    entry = "backend/auth/api.py"
    names = [
        "get-authorize-flow",             # browse
        "get-callback-flow",              # browse
        "post-auth-captcha-verify-flow",  # create (verb-noise)
        "post-logout-flow",               # create (verb-noise)
        "post-refresh-flow",              # create (verb-noise)
        "post-sso-exchange-flow",         # create (verb-noise)
        "validate-user-email-flow",       # act
        "verify-user-identity-flow",      # act
    ]
    flows = [_flow(n, entry) for n in names]
    devs = [_dev("auth", [entry], "auth", flows=flows)]
    pfs = [_pf("auth", "Auth")]
    ufs = [_uf("UF-033", "Sign in and verify identity", "auth",
               [f.name for f in flows], resource="auth")]
    tele = run_journey_lattice(ufs, devs, pfs, [])
    assert tele.get("action_catchalls_detected", 0) == 0
    assert [str(u.id) for u in ufs] == ["UF-033"]
    assert sorted(ufs[0].member_flow_ids) == sorted(names)


def test_single_member_families_never_qualify() -> None:
    """Strict corroboration: one flow's head verb is a single datapoint —
    a family with 1 member never qualifies, regardless of its span LOC
    (unlike the object axis' 150-LOC rescue arm)."""
    entry = "backend/routers/monitors.py"
    names = [
        "list-monitors-flow",            # browse
        "search-monitors-flow",          # browse
        "post-api-monitors-flow",        # create — 1 member, HUGE loc
        "patch-api-monitors-id-flow",    # update — 1 member, HUGE loc
    ]
    flows = [_flow(n, entry, loc=400) for n in names]
    devs = [_dev("monitors", [entry], "monitors", flows=flows)]
    pfs = [_pf("monitors", "Monitors")]
    ufs = [_uf("UF-022", "Monitor uptime and status", "monitors",
               [f.name for f in flows], resource="monitor")]
    tele = run_journey_lattice(ufs, devs, pfs, [])
    assert tele.get("action_catchalls_detected", 0) == 0
    assert [str(u.id) for u in ufs] == ["UF-022"]


def test_majority_foreign_family_folds_back() -> None:
    """The I16 ruler applied at mint time: an action family whose known-owner
    entries are majority-owned by OTHER PFs would be born a misattached
    journey (Soc0 'Update labels' 2/3-foreign class) — it folds to residual;
    when that drops the plan below the bar, the catch-all survives intact."""
    own_entry = "backend/routers/labels.py"
    foreign_entry = "frontend/src/pages/SettingsPage.tsx"
    names_own = [
        "list-labels-flow", "get-api-labels-summary-flow",   # browse (owned)
        "post-api-labels-flow", "post-api-labels-bulk-flow",  # create (owned)
    ]
    names_foreign = [
        "patch-api-labels-label-id-flow", "update-label-flow",  # update
    ]
    flows = ([_flow(n, own_entry) for n in names_own]
             + [_flow(n, foreign_entry) for n in names_foreign])
    devs = [
        _dev("api-labels", [own_entry], "labels",
             flows=flows),  # flows looked up via this dev
        _dev("settings-page", [foreign_entry], "settings"),
    ]
    pfs = [_pf("labels", "Labels"), _pf("settings", "Settings")]
    ufs = [_uf("UF-038", "Manage labels and track changes", "labels",
               [f.name for f in flows], resource="label")]
    tele = run_journey_lattice(ufs, devs, pfs, [])
    # update family (2 members, both foreign-owned) folds; only browse +
    # create remain -> below the >=3 bar -> no split at all.
    assert tele.get("action_catchalls_detected", 0) == 0
    assert [str(u.id) for u in ufs] == ["UF-038"]
    assert tele.get("clusters_unowned_folded", 0) >= 1


def test_attach_floor_folds_weakly_attached_families() -> None:
    """The validator's I15 ruler as an ELIGIBILITY floor (papermark
    'workflows' class): a capability misattached at EVERY grain — the parent
    AND every child sprawl far outside the PF scope (share 0.1 each) — is
    skipped and the catch-all survives intact. This is the anti-case the B7
    max-child gate must preserve: no child clears the floor, so nothing
    rescues the split (contrast ``test_b7_diluted_parent_...``, where one
    in-scope child does). Passes with B7 default-ON."""
    entry = "app/api/workflows/route.ts"
    sprawl = [f"lib/shared/util-{i}.ts" for i in range(9)]  # foreign mass

    def wide_flow(name: str) -> Flow:
        f = _flow(name, entry)
        f.paths = [entry] + sprawl  # 1/10 inside PF scope < 0.34
        return f

    names = [
        "list-workflows-flow", "get-api-workflows-summary-flow",   # browse
        "post-api-workflows-flow", "create-workflow-flow",          # create
        "patch-api-workflows-id-flow", "update-workflow-flow",      # update
    ]
    flows = [wide_flow(n) for n in names]
    devs = [_dev("workflows-api", [entry], "workflows", flows=flows)]
    pfs = [_pf("workflows", "Workflows")]
    ufs = [_uf("UF-007", "Browse and filter workflows", "workflows",
               [f.name for f in flows], resource="workflow")]
    tele = run_journey_lattice(ufs, devs, pfs, [])
    assert tele.get("action_catchalls_detected", 0) == 0
    assert [str(u.id) for u in ufs] == ["UF-007"]
    assert tele.get("action_parent_attach_skipped", 0) == 1


def test_attach_floor_lane_files_are_neutral() -> None:
    """Lane-owned files (pfid-None dev features at 6.88 time) are excluded
    from the attach denominator — a journey traversing shared infrastructure
    still splits when its non-lane files sit inside the PF scope."""
    entry = "backend/routers/cases.py"
    lane = [f"packages/ui/comp-{i}.tsx" for i in range(9)]

    def lane_flow(name: str) -> Flow:
        f = _flow(name, entry)
        f.paths = [entry] + lane  # non-lane share = 1/1 inside scope
        return f

    names = [
        "list-cases-flow", "get-api-cases-summary-flow",     # browse
        "post-api-cases-flow", "post-api-cases-bulk-flow",   # create
        "patch-api-cases-case-id-flow", "update-case-flow",  # update
    ]
    flows = [lane_flow(n) for n in names]
    devs = [
        _dev("api-cases", [entry], "cases", flows=flows),
        _dev("ui-lane", lane, None),  # pfid=None -> lane resident
    ]
    pfs = [_pf("cases", "Cases")]
    ufs = [_uf("UF-037", "Manage cases end-to-end", "cases",
               [f.name for f in flows], resource="case")]
    tele = run_journey_lattice(ufs, devs, pfs, [])
    assert tele.get("action_catchalls_detected", 0) == 1
    children = [u for u in ufs if str(u.domain or "").startswith(
        "lattice:action:")]
    assert {u.name for u in children} == {
        "Browse cases", "Create cases", "Update cases"}


def test_unowned_families_fold_back() -> None:
    """A family with no owned entry (developer-layer PF-stamped path) has no
    attachment evidence — it folds to residual exactly like the object axis;
    when that drops the plan below the bar, the catch-all survives."""
    ufs, devs, pfs, routes = _cases_scene()
    devs[0].product_feature_id = None  # no entry ownership anywhere
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    assert tele.get("action_catchalls_detected", 0) == 0
    assert [str(u.id) for u in ufs] == ["UF-037"]
    assert sorted(ufs[0].member_flow_ids) == sorted(_CASES_FLOW_NAMES)


# ── B7: diluted-parent recovery (the live Soc0 UF-037 divergence) ────────
#
# On the live keyed board UF-037 'Manage cases end-to-end' did NOT split even
# though B6's wave9 offline sim split it: the pre-split parent's 6.88-time
# lane-attach share fell below the floor (a few lane-diluted / stray members
# drag the mean down), so B6's parent-ONLY floor skipped it — while its
# 'View cases' child is a pure in-scope sub-journey (share 1.0). B7 gates
# eligibility on the MAX child instead of the parent mean: a capability with
# even one floor-clearing child splits; only a UNIFORMLY misattached
# capability (every child below the floor — papermark) is skipped. The change
# is a MONOTONE relaxation: NEW-skip ⊆ OLD-skip, so no B6 split un-splits.


def _diluted_cases_scene() -> tuple[list[UserFlow], list[Feature],
                                    list[Feature], list[dict]]:
    """A cases catch-all whose PARENT attach is below the floor (browse +
    create members each sprawl into 4 distinct foreign files → parent share
    1/17 ≈ 0.06) but whose VIEW child is fully in-scope (share 1.0)."""
    entry = _CASES_ROUTER

    def wide(name: str, tag: str) -> Flow:
        f = _flow(name, entry)
        f.paths = [entry] + [f"lib/shared/{tag}-{i}.ts" for i in range(4)]
        return f

    def tight(name: str) -> Flow:
        f = _flow(name, entry)
        f.paths = [entry]  # pure in-scope → child share 1.0
        return f

    flows = [
        wide("list-cases-flow", "b1"),             # browse (weak)
        wide("get-api-cases-summary-flow", "b2"),  # browse (weak)
        wide("post-api-cases-flow", "c1"),         # create (weak)
        wide("post-api-cases-bulk-flow", "c2"),    # create (weak)
        tight("get-api-cases-case-id-flow"),           # view (in-scope 1.0)
        tight("get-api-cases-case-id-timeline-flow"),  # view (in-scope 1.0)
    ]
    devs = [_dev("api-cases", [entry], "cases", flows=flows)]
    pfs = [_pf("cases", "Cases")]
    ufs = [_uf("UF-037", "Manage cases end-to-end", "cases",
               [f.name for f in flows], resource="case")]
    return ufs, devs, pfs, []


def test_b7_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(JOURNEY_LATTICE_B7_ENV, raising=False)
    assert journey_lattice_b7_enabled() is True
    monkeypatch.setenv(JOURNEY_LATTICE_B7_ENV, "0")
    assert journey_lattice_b7_enabled() is False


def test_b7_diluted_parent_with_attached_child_splits() -> None:
    """The fix: a sub-floor PARENT that still owns a floor-clearing child
    (View cases, share 1.0) splits — the low parent mean is an averaging
    artifact, not misattachment. The weak browse/create children (share
    ≈0.1) still MINT: the max child is the ELIGIBILITY signal, never a
    per-child fold (that per-child fold is exactly what B6 rejected)."""
    ufs, devs, pfs, routes = _diluted_cases_scene()
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    assert tele.get("action_catchalls_detected") == 1
    assert tele.get("action_parent_attach_skipped", 0) == 0
    children = [u for u in ufs if str(u.domain or "").startswith(
        "lattice:action:")]
    assert {u.name for u in children} == {
        "Browse cases", "Create cases", "View cases"}
    assert "UF-037" not in {str(u.id) for u in ufs}


def test_b7_off_restores_b6_parent_floor_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B7=0 restores B6 byte-behaviour: the parent-ONLY floor skips the same
    diluted catch-all (the child attach is never consulted)."""
    monkeypatch.setenv(JOURNEY_LATTICE_B7_ENV, "0")
    ufs, devs, pfs, routes = _diluted_cases_scene()
    tele = run_journey_lattice(ufs, devs, pfs, routes)
    assert tele.get("action_catchalls_detected", 0) == 0
    assert tele.get("action_parent_attach_skipped", 0) == 1
    # parent survives intact — no lattice children, all 6 members retained.
    assert [str(u.id) for u in ufs] == ["UF-037"]
    assert not any(
        str(u.domain or "").startswith("lattice:action:") for u in ufs)
    assert sorted(ufs[0].member_flow_ids) == sorted([
        "list-cases-flow", "get-api-cases-summary-flow",
        "post-api-cases-flow", "post-api-cases-bulk-flow",
        "get-api-cases-case-id-flow", "get-api-cases-case-id-timeline-flow",
    ])


# ── Fix B25 — verifier-revert slot release ───────────────────────────────
#
# The Soc0 'Manage cases end-to-end' wave12-14 signature: the object axis
# claims the catch-all with a doomed plan (a single-router route shard),
# the Draft Verifier honestly kills it, and pre-B25 the pf's ONE
# action-split slot stayed consumed forever — the healthy action plan was
# never built. B25 releases the slot exactly once per pf per scan, screens
# the recovered action plan in ONE additional verifier batch, and a
# reverted ACTION plan is always final (no re-entry loop).


def test_b25_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    assert journey_lattice_b25_enabled()
    monkeypatch.setenv(JOURNEY_LATTICE_B25_ENV, "0")
    assert not journey_lattice_b25_enabled()
    monkeypatch.setenv(JOURNEY_LATTICE_B25_ENV, "false")
    assert not journey_lattice_b25_enabled()


def test_b25_full_revert_releases_slot_to_action_axis() -> None:
    """Full object-plan revert → the slot releases once and the recovered
    ACTION plan (screened by the second batch) applies."""
    ufs, devs, pfs, routes = _dual_axis_scene()
    original_members = sorted(ufs[0].member_flow_ids)
    batches: list[list[str]] = []

    def _verifier(items: list[dict]) -> dict[str, bool]:
        batches.append([i["context"]["evidence"] for i in items])
        if len(batches) == 1:
            # Pass 1 is the OBJECT plan — reject every child (the Soc0
            # 'View export' class: dishonest shards of the parent).
            assert all(not e.startswith("action:") for e in batches[0])
            return {i["id"]: False for i in items}
        return {}  # recovery batch: missing verdict defaults to ACCEPT

    tele = run_journey_lattice(ufs, devs, pfs, routes, verifier=_verifier)
    assert len(batches) == 2
    assert all(e.startswith("action:") for e in batches[1])
    assert tele.get("slots_released") == 1
    assert tele.get("slot_release_pfs") == ["workflows"]
    assert tele.get("slot_release_recovered") == 1
    assert tele.get("plans_reverted_verifier") == 1  # the object plan only
    assert tele["catchalls_split"] == 1
    assert tele["journeys_created"] == 3

    children = [u for u in ufs
                if str(u.domain or "").startswith("lattice:action:")]
    assert sorted(str(u.domain) for u in children) == [
        "lattice:action:workflow-act",
        "lattice:action:workflow-create",
        "lattice:action:workflow-delete",
    ]
    # No object child was minted anywhere.
    assert not any(str(u.domain or "").startswith(("lattice:route:",
                                                   "lattice:dir:"))
                   for u in ufs)
    # Conservation (I13/I14 substrate): children + residual parent
    # partition the original member union exactly.
    assert sorted(m for u in ufs for m in (u.member_flow_ids or [])) == \
        original_members
    assert tele["conservation_reverts"] == 0


def test_b25_partial_revert_keeps_slot_consumed() -> None:
    """A PARTIALLY surviving plan applies — the slot is genuinely spent, so
    there is no release: one batch, no action children."""
    ufs, devs, pfs, routes = _dual_axis_scene()
    batches: list[int] = []

    def _verifier(items: list[dict]) -> dict[str, bool]:
        batches.append(len(items))
        # Reject ONE object child (slack); domain + token survive >= the
        # mint bar, so the plan applies partially.
        return {i["id"]: False for i in items
                if i["context"]["evidence"] == "route:slack"}

    tele = run_journey_lattice(ufs, devs, pfs, routes, verifier=_verifier)
    assert batches == [3]
    assert "slots_released" not in tele
    assert tele.get("plans_reverted_verifier") is None
    assert tele["journeys_created"] == 2  # domain + token applied
    assert not any(str(u.domain or "").startswith("lattice:action:")
                   for u in ufs)


def test_b25_reject_everything_is_bounded_and_byte_identical() -> None:
    """The explicit no-infinite-loop property: a verifier that rejects
    EVERYTHING forever sees exactly TWO batches — pass 1 plus the single
    slot-release recovery (a pf releases at most ONCE; a reverted action
    plan is final) — and the board survives byte-identically."""
    ufs, devs, pfs, routes = _dual_axis_scene()
    before = [u.model_dump() for u in ufs]
    calls = 0

    def _verifier(items: list[dict]) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {i["id"]: False for i in items}

    tele = run_journey_lattice(ufs, devs, pfs, routes, verifier=_verifier)
    assert calls == 2
    assert tele.get("slots_released") == 1
    assert tele.get("slot_release_recovered") == 0
    assert tele.get("plans_reverted_verifier") == 2
    assert tele["journeys_created"] == 0
    assert [u.model_dump() for u in ufs] == before


def test_b25_action_plan_revert_is_final_no_release() -> None:
    """No re-entry: when the pf's pass-1 plan IS the action plan (the
    object axis never claimed the UF), a full revert is final — one batch,
    no release."""
    ufs, devs, pfs, routes = _cases_scene()
    before = [u.model_dump() for u in ufs]
    calls = 0

    def _verifier(items: list[dict]) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        assert all(i["context"]["evidence"].startswith("action:")
                   for i in items)
        return {i["id"]: False for i in items}

    tele = run_journey_lattice(ufs, devs, pfs, routes, verifier=_verifier)
    assert calls == 1
    assert tele.get("plans_reverted_verifier") == 1
    assert "slots_released" not in tele
    assert [u.model_dump() for u in ufs] == before


def test_b25_off_restores_pre_b25_single_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kill-switch: B25=0 restores the pre-B25 slot behavior — a full
    revert keeps the slot consumed (one batch, no recovery, no release
    telemetry), the catch-all byte-identical."""
    monkeypatch.setenv(JOURNEY_LATTICE_B25_ENV, "0")
    ufs, devs, pfs, routes = _dual_axis_scene()
    before = [u.model_dump() for u in ufs]
    calls = 0

    def _verifier(items: list[dict]) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {i["id"]: False for i in items}

    tele = run_journey_lattice(ufs, devs, pfs, routes, verifier=_verifier)
    assert calls == 1
    assert tele["verifier_rejects"] == 3
    assert tele.get("plans_reverted_verifier") == 1
    assert "slots_released" not in tele
    assert tele["journeys_created"] == 0
    assert [u.model_dump() for u in ufs] == before
