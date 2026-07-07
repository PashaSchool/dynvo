"""Product-Spine W3.2 — UF-evidence lane re-homing (``fold:uf-evidence``).

Fixtures distilled from wave31 keyed scans:

  * comp: the ``*-2`` split subs (integration-platform-2 → integrations
    at 0.87 concentration, isms-2/billing-2/people-2 at 0.9-1.0) sit in
    the 197.7K lane while the capability PF's own journeys cite them;
  * supabase: dev ``data`` (647 files, 35K LOC, every studio domain's
    query hooks) is cited by MANY PFs' journeys, one subdir each — it
    must NOT move (re-homing it wholesale rebuilds the sink class);
  * comp: a PF named ``src`` (structural token) attracted 52K LOC in
    simulation — structural anchors are not re-home targets;
  * capacity: a pile reaching the validator-I23 gate-cell shape
    (>= 25K moved AND >= 3x the PF's own body) is blocked.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.lane_rehome import (
    UF_EVIDENCE_NOTE,
    rehome_uf_cited_lane_devs,
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


def dev(name: str, paths: list[str], *, pfid=None, reason="shell_lineage_only",
        flows=None, loc=None, **kw) -> Feature:
    return Feature(
        name=name, paths=list(paths), flows=flows or [],
        product_feature_id=pfid, shared_reason=reason, loc=loc,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, **kw,
    )


def pf(slug: str, paths: list[str], anchor_id: str, loc: int = 1000) -> Feature:
    f = Feature(
        name=slug, paths=list(paths), flows=[],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, loc=loc,
    )
    f.layer = "product"
    f.anchor_id = anchor_id
    return f


def uf(uid: str, pfid: str, member_ids: list[str]) -> UserFlow:
    return UserFlow(
        id=uid, name=f"Journey {uid}", intent="manage", resource="item",
        product_feature_id=pfid, member_flow_ids=list(member_ids),
        member_count=len(member_ids),
    )


def test_split_sub_rehomes_on_journey_evidence() -> None:
    """comp integration-platform-2 class: a flowless lane sub whose
    files are majority-cited by ONE capability's journeys re-homes there
    with the fold:uf-evidence note; the lane row disappears."""
    sub_files = [f"apps/app/src/integration-platform/{n}.tsx"
                 for n in ("list", "detail", "connect", "form", "hooks")]
    lane_dev = dev("integration-platform-2", sub_files, loc=1200)
    target = pf("integrations", ["packages/integrations/core.ts"],
                "hub:packages/integrations", loc=9000)
    fl = flow("browse-integrations-flow",
              "packages/integrations/core.ts",
              paths=["packages/integrations/core.ts", *sub_files[:4]])
    journeys = [uf("UF-001", "integrations", [fl.uuid])]
    tele = rehome_uf_cited_lane_devs(
        [lane_dev], [target], journeys, [fl])
    assert lane_dev.product_feature_id == "integrations"
    assert lane_dev.anchor_id == UF_EVIDENCE_NOTE
    assert lane_dev.shared_reason is None
    assert tele["rehomed"] == 1 and tele["rehomed_loc"] == 1200
    assert tele["sample"][0]["dev"] == "integration-platform-2"


def test_multi_domain_dev_stays_laned() -> None:
    """supabase `data` class: a big dev cited one-subdir-per-PF fails
    both the concentration and the self-evidence bars — it stays."""
    files = ([f"apps/studio/data/cron/{n}.ts" for n in "abc"]
             + [f"apps/studio/data/storage/{n}.ts" for n in "abcdefgh"]
             + [f"apps/studio/data/auth/{n}.ts" for n in "abcdefgh"])
    data_dev = dev("data", files, loc=35000)
    pg = pf("pg-meta", ["apps/studio/pages/api/platform/pg-meta/x.ts"],
            "route:apps/studio/pages/api/platform/pg-meta")
    fl = flow("manage-cron-flow", "apps/studio/data/cron/a.ts",
              paths=[f"apps/studio/data/cron/{n}.ts" for n in "abc"])
    journeys = [uf("UF-001", "pg-meta", [fl.uuid])]
    tele = rehome_uf_cited_lane_devs([data_dev], [pg], journeys, [fl])
    assert data_dev.product_feature_id is None, (
        "sink resurrection: the multi-domain data layer was re-homed "
        "wholesale into one capability")
    assert tele["rehomed"] == 0
    assert tele["blocked_self_evidence"] == 1


def test_split_citations_between_two_pfs_do_not_move() -> None:
    files = [f"src/thing/{n}.ts" for n in "abcdef"]
    d = dev("thing", files, loc=100)
    p1 = pf("alpha", ["src/alpha/x.ts"], "route:src/alpha")
    p2 = pf("beta", ["src/beta/x.ts"], "route:src/beta")
    f1 = flow("a-flow", "src/alpha/x.ts", paths=files[:3], uuid="u1")
    f2 = flow("b-flow", "src/beta/x.ts", paths=files[3:], uuid="u2")
    journeys = [uf("UF-001", "alpha", ["u1"]), uf("UF-002", "beta", ["u2"])]
    tele = rehome_uf_cited_lane_devs([d], [p1, p2], journeys, [f1, f2])
    assert d.product_feature_id is None
    assert tele["blocked_concentration"] == 1


def test_structural_anchor_target_blocked() -> None:
    """comp `src` class: a PF whose anchor tail is a structural token
    never attracts re-homes."""
    files = [f"apps/app/src/misc/{n}.ts" for n in "abcd"]
    d = dev("misc-2", files, loc=500)
    target = pf("src", ["apps/app/src/index.ts"], "ws:apps/app/src")
    fl = flow("misc-flow", "apps/app/src/index.ts", paths=files)
    journeys = [uf("UF-001", "src", [fl.uuid])]
    tele = rehome_uf_cited_lane_devs([d], [target], journeys, [fl])
    assert d.product_feature_id is None
    assert tele["blocked_target"] == 1


def test_capacity_cap_blocks_gate_cell_pile() -> None:
    """A move that would build a validator-I23 gate-cell-shaped pile
    (>= 25K moved AND >= 3x the PF body) is blocked."""
    files = [f"apps/big/{n}.ts" for n in "abcdefgh"]
    d = dev("big-sub", files, loc=30000)
    target = pf("tiny", ["apps/tiny/page.tsx"], "route:apps/tiny", loc=2000)
    fl = flow("tiny-flow", "apps/tiny/page.tsx", paths=files)
    journeys = [uf("UF-001", "tiny", [fl.uuid])]
    tele = rehome_uf_cited_lane_devs([d], [target], journeys, [fl])
    assert d.product_feature_id is None
    assert tele["blocked_cap"] == 1
    # same mass into a big-enough body is NOT a gate-cell shape
    d2 = dev("big-sub-2", files, loc=30000)
    target2 = pf("editor", ["apps/editor/page.tsx"], "route:apps/editor",
                 loc=20000)
    journeys2 = [uf("UF-001", "editor", [fl.uuid])]
    tele2 = rehome_uf_cited_lane_devs([d2], [target2], journeys2, [fl])
    assert d2.product_feature_id == "editor"
    assert tele2["rehomed"] == 1


def test_flowful_ws_anchor_and_facet_devs_are_not_candidates() -> None:
    files = [f"pkg/x/{n}.ts" for n in "abc"]
    target = pf("cap", ["pkg/cap/x.ts"], "route:pkg/cap")
    fl = flow("cap-flow", "pkg/cap/x.ts", paths=files)
    journeys = [uf("UF-001", "cap", [fl.uuid])]
    flowful = dev("flowful", files, flows=[fl], loc=10)
    ws = dev("shell", files, loc=10,
             description="[package] workspace anchor 'shell' from "
                         "monorepo package 'pkg/x'")
    facet = dev("view", files, loc=10, role="facet")
    homed = dev("homed", files, pfid="cap", reason=None, loc=10)
    tele = rehome_uf_cited_lane_devs(
        [flowful, ws, facet, homed], [target], journeys, [fl])
    assert tele["rehomed"] == 0 and tele["checked"] == 0
    assert flowful.product_feature_id is None
    assert ws.product_feature_id is None


def test_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_SPINE_LANE_REHOME", "0")
    files = [f"a/{n}.ts" for n in "abc"]
    d = dev("d", files, loc=10)
    target = pf("cap", ["cap/x.ts"], "route:cap")
    fl = flow("f", "cap/x.ts", paths=files)
    tele = rehome_uf_cited_lane_devs(
        [d], [target], [uf("UF-001", "cap", [fl.uuid])], [fl])
    assert tele == {"enabled": False, "checked": 0, "rehomed": 0,
                    "rehomed_loc": 0, "blocked_concentration": 0,
                    "blocked_self_evidence": 0, "blocked_target": 0,
                    "blocked_cap": 0, "sample": []}
    assert d.product_feature_id is None
