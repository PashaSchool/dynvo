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
    JOURNEY_LATTICE_V2_ENV,
    _action_family,
    _child_id,
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


def test_object_axis_split_takes_precedence() -> None:
    """A journey the OBJECT axis splits is never re-touched by the action
    axis (papermark teams-api scene from the W5 suite: distinct route
    families → object split fires; action pass must skip it)."""
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
