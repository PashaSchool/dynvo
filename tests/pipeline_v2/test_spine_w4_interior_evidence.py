"""W4 — 6.7d interior-section evidence: digest, prompt, verified citations.

The constrained (anchored) Call-1 gains a ``sections`` field per fixed
capability and may cite ``from_sections``; the engine verifies each
citation against the SAME capability's section list and grounds
still-empty journeys via the hosting pages' flows. Evidence-less runs
stay byte-identical (prompt, digest, cache namespace).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _ANCHORED_SYSTEM,
    _INTERIOR_ADDENDUM,
    _build_user_flows,
    run_journey_abstraction,
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _flow(name: str, entry: str) -> Flow:
    return Flow(
        name=name, uuid=f"uuid-{name}", paths=[entry], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, entry_point_file=entry,
    )


def _dev(name: str, pfid: str | None, paths: list[str],
         flows: list[Flow] | None = None) -> Feature:
    return Feature(
        name=name, display_name=name, paths=paths, authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="developer", product_feature_id=pfid,
        flows=flows or [],
    )


def _pf(name: str, display: str) -> Feature:
    return Feature(
        name=name, display_name=display, paths=[], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="product",
    )


def _uf(uid: str, name: str, pfid: str, member_ids: list[str]) -> UserFlow:
    return UserFlow(
        id=uid, name=name, resource="database", domain="database",
        product_feature_id=pfid, intent="manage",
        member_flow_ids=member_ids, member_count=len(member_ids),
    )


_EVIDENCE = {
    "by_pf": {"Database": ["Scheduled Backups", "Connection Pooling"]},
    "pages": {
        "pages/database/backups.tsx": {
            "pf": "Database", "sections": ["Scheduled Backups"],
        },
        "pages/database/pooling.tsx": {
            "pf": "Database", "sections": ["Connection Pooling"],
        },
    },
}


# ── _build_user_flows unit: verified citations ──────────────────────────


def _world():
    f1 = _flow("view-backups-flow", "pages/database/backups.tsx")
    f2 = _flow("tune-pooling-flow", "pages/database/pooling.tsx")
    d = _dev("database", "database",
             ["pages/database/backups.tsx", "pages/database/pooling.tsx"],
             [f1, f2])
    return d, f1, f2


def test_valid_section_citation_grounds_via_page_flows() -> None:
    d, f1, _f2 = _world()
    specs = [{
        "name": "Restore a scheduled backup", "resource": "backup",
        "product_feature": "Database",
        "from_flows": [], "from_dev_features": [],
        "from_sections": ["Scheduled Backups"],
    }]
    ufs, tele = _build_user_flows(specs, [], [d], [],
                                  interior_evidence=_EVIDENCE)
    assert len(ufs) == 1
    assert ufs[0].member_flow_ids == ["uuid-view-backups-flow"]
    assert tele["uf_sections_cited"] == 1
    assert tele["uf_section_grounded"] == 1
    assert tele["uf_sections_invalid"] == 0


def test_cross_pf_section_citation_is_invalid() -> None:
    d, _f1, _f2 = _world()
    specs = [{
        "name": "Restore a scheduled backup", "resource": "backup",
        # Journey names ANOTHER capability than the section's host.
        "product_feature": "Storage",
        "from_flows": [], "from_dev_features": [],
        "from_sections": ["Scheduled Backups"],
    }]
    ufs, tele = _build_user_flows(specs, [], [d], [],
                                  interior_evidence=_EVIDENCE)
    assert tele["uf_sections_invalid"] == 1
    assert tele["uf_section_grounded"] == 0
    # The 2b token rescue may still ground it ("backup" tokens) — the
    # citation itself contributed nothing.
    for u in ufs:
        assert tele["uf_dev_grounded"] == 0


def test_no_evidence_no_new_telemetry_effects() -> None:
    d, _f1, _f2 = _world()
    specs = [{
        "name": "Restore a scheduled backup", "resource": "backup",
        "product_feature": "Database",
        "from_flows": [], "from_dev_features": [],
        "from_sections": ["Scheduled Backups"],
    }]
    _ufs, tele = _build_user_flows(specs, [], [d], [],
                                   interior_evidence=None)
    assert tele["uf_sections_cited"] == 0
    assert tele["uf_sections_invalid"] == 0


# ── run_journey_abstraction end-to-end (anchored + evidence) ────────────


class _CapturingClient:
    def __init__(self, payload: str) -> None:
        self.calls: list[dict] = []
        self._payload = payload
        self.messages = self

    def create(self, **kw):  # noqa: ANN003
        self.calls.append(kw)
        return SimpleNamespace(
            content=[SimpleNamespace(text=self._payload)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )


def _anchored_world():
    d, f1, f2 = _world()
    pfs = [_pf("database", "Database")]
    old_uf = _uf("UF-001", "Manage database", "database",
                 [f1.uuid, f2.uuid])
    return d, pfs, old_uf


def test_sections_enter_digest_and_prompt_and_cache_namespace() -> None:
    d, pfs, old_uf = _anchored_world()
    payload = (
        '{"product_features":[{"name":"Database","description":"db"}],'
        '"user_flows":[{"name":"Manage database","resource":"database",'
        '"product_feature":"Database","from_flows":["UF-001"],'
        '"from_dev_features":["database"]}]}'
    )
    cli = _CapturingClient(payload)
    _ufs, _pfs2, _map, tele = run_journey_abstraction(
        [old_uf], pfs, [d], [], client=cli, model="m",
        anchored=True, interior_evidence=_EVIDENCE,
    )
    assert tele["applied"], tele.get("fallback")
    assert tele["interior_sections_pfs"] == 1
    call = cli.calls[0]
    assert call["system"].endswith(_INTERIOR_ADDENDUM)
    user_text = call["messages"][0]["content"]
    digest = json.loads(
        user_text.split("```json\n", 1)[1].split("\n```", 1)[0])
    (pf_line,) = digest["current_product_features"]
    assert pf_line["sections"] == ["Scheduled Backups",
                                   "Connection Pooling"]


def test_without_evidence_prompt_and_digest_unchanged() -> None:
    d, pfs, old_uf = _anchored_world()
    payload = (
        '{"product_features":[{"name":"Database","description":"db"}],'
        '"user_flows":[{"name":"Manage database","resource":"database",'
        '"product_feature":"Database","from_flows":["UF-001"],'
        '"from_dev_features":["database"]}]}'
    )
    cli = _CapturingClient(payload)
    _ufs, _pfs2, _map, tele = run_journey_abstraction(
        [old_uf], pfs, [d], [], client=cli, model="m",
        anchored=True, interior_evidence=None,
    )
    assert tele["applied"], tele.get("fallback")
    assert "interior_sections_pfs" not in tele
    call = cli.calls[0]
    assert call["system"] == _ANCHORED_SYSTEM  # no addendum
    user_text = call["messages"][0]["content"]
    digest = json.loads(
        user_text.split("```json\n", 1)[1].split("\n```", 1)[0])
    (pf_line,) = digest["current_product_features"]
    assert "sections" not in pf_line


# ── W4 home-pure membership (anchored) ──────────────────────────────────


def _mixed_world():
    """Two PFs; one deterministic UF holding BOTH PFs' flows (the
    openstatus status-pages/status-reports swap class)."""
    fp1 = _eflow("pages/status-pages/edit.tsx")
    fp2 = _eflow("pages/status-pages/list.tsx")
    fr1 = _eflow("pages/status-reports/new.tsx")
    d_pages = _dev("status-pages", "status-pages",
                   ["pages/status-pages/edit.tsx",
                    "pages/status-pages/list.tsx"], [fp1, fp2])
    d_reports = _dev("status-reports", "status-reports",
                     ["pages/status-reports/new.tsx"], [fr1])
    mixed_uf = _uf("UF-001", "Manage status things", "status-pages",
                   [fp1.uuid, fp2.uuid, fr1.uuid])
    return d_pages, d_reports, mixed_uf, (fp1, fp2, fr1)


def _eflow(entry: str) -> "Flow":
    from faultline.models.types import Flow
    return Flow(
        name=entry.replace("/", "-"), uuid=f"uuid-{entry}", paths=[entry],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0, entry_point_file=entry,
    )


def test_home_pure_inheritance_filters_foreign_bloc() -> None:
    d_pages, d_reports, mixed_uf, (fp1, fp2, fr1) = _mixed_world()
    specs = [{
        "name": "Manage status pages", "resource": "status page",
        "product_feature": "Status Pages",
        "from_flows": ["UF-001"], "from_dev_features": [],
    }]
    ufs, tele = _build_user_flows(
        specs, [mixed_uf], [d_pages, d_reports], [], home_pure=True)
    (uf,) = ufs
    # Only the status-pages HOME flows inherited; the reports flow
    # stays unclaimed for its own capability's journey.
    assert set(uf.member_flow_ids) == {fp1.uuid, fp2.uuid}
    assert tele["uf_home_filtered"] == 1


def test_home_pure_off_keeps_legacy_inheritance() -> None:
    d_pages, d_reports, mixed_uf, (fp1, fp2, fr1) = _mixed_world()
    specs = [{
        "name": "Manage status pages", "resource": "status page",
        "product_feature": "Status Pages",
        "from_flows": ["UF-001"], "from_dev_features": [],
    }]
    ufs, tele = _build_user_flows(
        specs, [mixed_uf], [d_pages, d_reports], [], home_pure=False)
    (uf,) = ufs
    assert set(uf.member_flow_ids) == {fp1.uuid, fp2.uuid, fr1.uuid}
    assert tele["uf_home_filtered"] == 0


def test_home_pure_lane_flows_stay_inheritable() -> None:
    from faultline.models.types import Flow
    lane_flow = Flow(
        name="lane-flow", uuid="uuid-lane", paths=["lib/x.ts"],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0, entry_point_file="lib/x.ts",
    )
    lane_dev = _dev("lib", None, ["lib/x.ts"], [lane_flow])
    old = _uf("UF-001", "Old", "status-pages", ["uuid-lane"])
    specs = [{
        "name": "Do things", "resource": "thing",
        "product_feature": "Status Pages",
        "from_flows": ["UF-001"], "from_dev_features": [],
    }]
    ufs, tele = _build_user_flows(specs, [old], [lane_dev], [],
                                  home_pure=True)
    (uf,) = ufs
    assert uf.member_flow_ids == ["uuid-lane"]  # None-home = inheritable
    assert tele["uf_home_filtered"] == 0
