"""Product-Spine W3.2 D9 — the flow-less system mint survives the keyed path.

wave31 (keyed): scan_meta.system_flow_routes stamped on 6/10 repos, yet
system-category UFs in output = 0 CORPUS-WIDE. The rollup's in-stage
synthesis (Stage 6.7d env-gated block) DID mint — Stage 6.7d's
``_finish`` then rebuilt user_flows[] from the LLM's journey specs and
dropped every member-less system seed. Soc0's 11 flow-less
``backend/inngest_functions/*.py`` jobs — the mint's canonical target —
matched, minted, vanished.

The fix mirrors the route-group-seed survival slot: phase_finalize
re-mints AFTER 6.7d via ``resynthesize_system_ufs`` (dedup-aware — a
keyless pipeline that kept the rollup output no-ops) and re-stamps
deterministic 6.8b trigger verdicts onto rebuilt journeys
(``restamp_system_triggers``, unanimous-evidence bar) — the w31x
"0/105 UFs carry a trigger" last inch.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.stage_6_7_user_flows import (
    SYSTEM_RECALL_REASON,
    restamp_system_triggers,
    resynthesize_system_ufs,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def flow(name: str, entry: str, paths: list[str] | None = None,
         uuid: str | None = None) -> Flow:
    return Flow(
        name=name, entry_point_file=entry, paths=paths or [entry],
        uuid=uuid or f"uuid-{name}",
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def dev(name: str, paths: list[str], *, pfid=None) -> Feature:
    return Feature(
        name=name, paths=list(paths), flows=[], product_feature_id=pfid,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def uf(uid: str, name: str, *, pfid=None, members=None, category="interactive",
       trigger=None, resource="item") -> UserFlow:
    return UserFlow(
        id=uid, name=name, intent="manage", resource=resource,
        product_feature_id=pfid, member_flow_ids=list(members or []),
        member_count=len(members or []), category=category, trigger=trigger,
    )


def test_soc0_shaped_jobs_survive_a_67d_rewrite() -> None:
    """THE D9 canonical target: flow-less inngest job files must yield
    thin system UFs even after the keyed rewrite dropped the rollup's
    seeds — the post-pass re-mints them, tagged and id-continuous."""
    # post-6.7d state: the LLM rewrite kept only interactive journeys
    journeys = [uf("UF-001", "Manage cases", pfid="cases"),
                uf("UF-002", "Chat with copilot", pfid="chat")]
    inngest = dev("inngest-functions", [
        "backend/inngest_functions/__init__.py",
        "backend/inngest_functions/articles.py",
        "backend/inngest_functions/cases.py",
        "backend/inngest_functions/chat.py",
        "backend/inngest_functions/crons.py",
    ])  # laned: pfid=None
    tele = resynthesize_system_ufs(journeys, [], [inngest], [])
    assert tele["minted"] == 4  # __init__ skipped
    minted = [u for u in journeys if u.category == "system"]
    assert {u.resource for u in minted} == {"articles", "cases", "chat", "crons"}
    assert all(u.trigger == "queue" for u in minted)
    assert all(u.synthesized and u.synthesis_reason == SYSTEM_RECALL_REASON
               for u in minted)
    assert all(u.member_count == 0 and u.ui_tier == "no-ui" for u in minted)
    # owning dev is laned → honest orphan (no invented PF home)
    assert all(u.product_feature_id is None for u in minted)
    # ids continue after the rewrite's numbering
    assert [u.id for u in minted] == ["UF-003", "UF-004", "UF-005", "UF-006"]


def test_keyless_path_is_a_dedup_noop() -> None:
    """When the rollup's seeds SURVIVED (keyless path), the post-pass
    must not double-mint."""
    journeys = [
        uf("UF-001", "Manage cases", pfid="cases"),
        uf("UF-002", "Execute crons", category="system", trigger="queue",
           resource="crons"),
    ]
    inngest = dev("jobs", ["backend/tasks/crons.py"])
    before = [u.id for u in journeys]
    tele = resynthesize_system_ufs(journeys, [], [inngest], [])
    assert tele["minted"] == 0 and tele["skipped_existing"] == 1
    assert [u.id for u in journeys] == before


def test_route_channel_attributes_pf_home_from_ownership() -> None:
    """A flow-less system ROUTE whose file is primary-owned by a
    PF-bound dev cites that PF (binding low); files owned by lane devs
    stay orphans."""
    routes = [
        {"file": "apps/web/app/api/google/webhook/route.ts",
         "pattern": "/api/google/webhook", "trigger": "webhook"},
        {"file": "apps/web/app/api/cron/digest/route.ts",
         "pattern": "/api/cron/digest", "trigger": "scheduled"},
    ]
    google = dev("google", ["apps/web/app/api/google/webhook/route.ts"],
                 pfid="google")
    digest = dev("digest", ["apps/web/app/api/cron/digest/route.ts"])  # laned
    journeys: list[UserFlow] = [uf("UF-001", "Browse settings", pfid="settings")]
    tele = resynthesize_system_ufs(journeys, [], [google, digest], routes)
    assert tele["minted"] == 2
    by_res = {u.resource: u for u in journeys if u.category == "system"}
    assert by_res["google-webhook"].product_feature_id == "google"
    assert by_res["google-webhook"].binding_confidence == "low"
    assert by_res["digest"].product_feature_id is None
    assert by_res["digest"].binding_confidence is None


def test_flow_covered_system_route_never_reminted() -> None:
    """The anchored skip carries over: a system route a real flow covers
    is that journey's business, not a seed."""
    routes = [{"file": "apps/web/app/api/stripe/webhook/route.ts",
               "pattern": "/api/stripe/webhook", "trigger": "webhook"}]
    fl = flow("process-webhook-flow", "apps/web/app/api/stripe/webhook/route.ts")
    journeys = [uf("UF-001", "Process stripe events", pfid="billing",
                   members=[fl.uuid])]
    tele = resynthesize_system_ufs(journeys, [fl], [], routes)
    assert tele["minted"] == 0


def test_restamp_unanimous_system_journey() -> None:
    """w31x last inch: a rebuilt journey whose member flows ALL enter
    system routes gets the deterministic trigger verdict; mixed journeys
    stay interactive."""
    routes = [
        {"file": "tracecat/schedules/run.py", "pattern": "/schedules",
         "trigger": "queue"},
        {"file": "frontend/src/app/cases/page.tsx", "pattern": "/cases",
         "trigger": "interactive"},
    ]
    sys_fl = flow("schedule-run-flow", "tracecat/schedules/run.py", uuid="u1")
    ui_fl = flow("browse-cases-flow", "frontend/src/app/cases/page.tsx",
                 uuid="u2")
    pure = uf("UF-001", "Run scheduled workflows", pfid="workflows",
              members=["u1"])
    mixed = uf("UF-002", "Manage cases", pfid="cases", members=["u1", "u2"])
    tele = restamp_system_triggers([pure, mixed], [sys_fl, ui_fl], routes)
    assert tele["stamped"] == 1
    assert pure.trigger == "queue" and pure.category == "system"
    assert mixed.trigger is None and mixed.category == "interactive"


def test_restamp_leaves_existing_verdicts_alone() -> None:
    routes = [{"file": "jobs/a.py", "pattern": "/a", "trigger": "queue"}]
    fl = flow("a-flow", "jobs/a.py", uuid="u1")
    already = uf("UF-001", "Execute a", members=["u1"], category="system",
                 trigger="scheduled")
    tele = restamp_system_triggers([already], [fl], routes)
    assert tele["stamped"] == 0
    assert already.trigger == "scheduled"


def test_kill_switch_disables_remint(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_SEED_SYSTEM_UFS", "0")
    journeys: list[UserFlow] = []
    inngest = dev("jobs", ["backend/tasks/crons.py"])
    tele = resynthesize_system_ufs(journeys, [], [inngest], [])
    assert tele == {"enabled": False, "minted": 0, "skipped_existing": 0,
                    "seeds": []}
    assert journeys == []
