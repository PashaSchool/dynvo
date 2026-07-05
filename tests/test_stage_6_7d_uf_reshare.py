"""Unit + integration tests for the Stage 6.7d Shared-Platform UF reassignment.

Operator directive (2026-07-05, validator I10): "Shared Platform may own CODE,
never JOURNEYS." Every user_flow left on the shared/platform capability is
reassigned to the non-shared product feature owning the PLURALITY of its member
flows; shared-owned member flows are ignored in the count; when EVERY member is
shared-owned the journey is a legitimate rarity left on the residual and flagged
in ``uf_shared_unresolved``.

The reassignment runs inside ``_finish`` so it applies identically on the live
and the cache-hit replay path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from faultline.models.types import Feature, Flow, MemberFile, UserFlow
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _SHARED_PF_SLUGS,
    _reassign_shared_ufs,
    _uf_reshare_enabled,
    run_journey_abstraction,
)

SHARED = "shared-platform"


# ── Fixtures ────────────────────────────────────────────────────────────────

def _flow(uuid: str) -> Flow:
    return Flow(
        name=uuid, uuid=uuid, paths=[f"backend/{uuid}.py"], authors=["a"],
        total_commits=2, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=95.0,
    )


def _dev(name: str, flow_uuids: list[str]) -> Feature:
    return Feature(
        name=name, display_name=name, description=f"{name} module",
        paths=[f"backend/{name}/x.py"], authors=["a"], total_commits=3,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=datetime.now(timezone.utc),
        health_score=90.0, layer="developer",
        member_files=[MemberFile(path=f"backend/{name}/x.py", role="anchor",
                                 confidence=1.0)],
        flows=[_flow(u) for u in flow_uuids],
    )


def _uf(name: str, pf_slug: str, members: list[str]) -> UserFlow:
    return UserFlow(
        id="UF-000", name=name, intent="browse", resource=name.lower(),
        product_feature_id=pf_slug, member_flow_ids=members,
        member_count=len(members), routes=[],
    )


# ── Direct unit tests of _reassign_shared_ufs ───────────────────────────────

def test_plurality_reassign() -> None:
    """A shared UF is reassigned to the non-shared PF owning the plurality of
    its member flows."""
    devs = [_dev("billing-svc", ["b1", "b2", "b3"]), _dev("auth-svc", ["a1"])]
    d2p = {"billing-svc": ("billing",), "auth-svc": ("auth",)}
    uf = _uf("Manage alerts", SHARED, ["b1", "b2", "b3", "a1"])
    tele = _reassign_shared_ufs([uf], devs, d2p)
    assert uf.product_feature_id == "billing"
    assert tele["uf_shared_reassigned"] == 1
    assert tele["uf_shared_unresolved"] == 0
    (row,) = tele["uf_shared_reassignments"]
    assert row == {"uf": "Manage alerts", "from": SHARED, "to": "billing",
                   "basis": "plurality 3/4"}


def test_shared_owned_flows_excluded_from_count() -> None:
    """Member flows owned by shared devs are IGNORED — a single non-shared
    owner still wins even when shared flows dominate."""
    devs = [_dev("billing-svc", ["b1"]), _dev("shared-lib", ["s1", "s2"])]
    d2p = {"billing-svc": ("billing",), "shared-lib": (SHARED,)}
    uf = _uf("Browse queue", SHARED, ["s1", "b1", "s2"])
    tele = _reassign_shared_ufs([uf], devs, d2p)
    assert uf.product_feature_id == "billing"
    assert tele["uf_shared_reassigned"] == 1
    assert tele["uf_shared_reassignments"][0]["basis"] == "plurality 1/3"


def test_all_members_shared_owned_stays_unresolved() -> None:
    """When every member is owned by shared/infra devs the UF is left on the
    residual and flagged (the legitimate all-shared rarity — the Soc0
    'Manage investigation playbooks' case)."""
    devs = [_dev("backend", ["s1", "s2"])]
    d2p = {"backend": (SHARED,)}
    uf = _uf("Manage playbooks", SHARED, ["s1", "s2"])
    tele = _reassign_shared_ufs([uf], devs, d2p)
    assert uf.product_feature_id == SHARED  # unchanged
    assert tele["uf_shared_reassigned"] == 0
    assert tele["uf_shared_unresolved"] == 1
    assert tele["uf_shared_reassignments"] == [
        {"uf": "Manage playbooks", "from": SHARED, "to": None,
         "basis": "all_members_shared_owned"}]


def test_platform_slug_also_treated_as_shared() -> None:
    """Both 'shared-platform' and the bare 'platform' slug count as residual."""
    assert "platform" in _SHARED_PF_SLUGS and SHARED in _SHARED_PF_SLUGS
    devs = [_dev("billing-svc", ["b1", "b2"])]
    d2p = {"billing-svc": ("billing",)}
    uf = _uf("Handle billing", "platform", ["b1", "b2"])
    tele = _reassign_shared_ufs([uf], devs, d2p)
    assert uf.product_feature_id == "billing"
    assert tele["uf_shared_reassigned"] == 1


def test_tie_break_prefers_more_specific_smaller_pf() -> None:
    """On a plurality tie the narrower PF (fewer total owned flows) wins."""
    big = _dev("big-svc", ["g1", "g2", "g3", "g4", "g5", "g6"])   # owns 6
    small = _dev("small-svc", ["m1", "m2"])                       # owns 2
    d2p = {"big-svc": ("big",), "small-svc": ("small",)}
    uf = _uf("Cross feature", SHARED, ["g1", "g2", "m1", "m2"])   # 2 vs 2
    tele = _reassign_shared_ufs([uf], [big, small], d2p)
    assert uf.product_feature_id == "small"
    assert tele["uf_shared_reassigned"] == 1


def test_tie_break_alphabetical_when_fully_equal() -> None:
    """A total tie (same count, same PF size) breaks to the alphabetically
    first slug — fully deterministic."""
    alpha = _dev("alpha-svc", ["x1", "x2"])
    bravo = _dev("bravo-svc", ["y1", "y2"])
    d2p = {"alpha-svc": ("alpha",), "bravo-svc": ("bravo",)}
    uf = _uf("Split", SHARED, ["x1", "x2", "y1", "y2"])
    tele = _reassign_shared_ufs([uf], [alpha, bravo], d2p)
    assert uf.product_feature_id == "alpha"
    assert tele["uf_shared_reassigned"] == 1


def test_non_shared_uf_untouched() -> None:
    """A UF already on a real product feature is never modified."""
    devs = [_dev("billing-svc", ["b1"])]
    d2p = {"billing-svc": ("billing",)}
    uf = _uf("Pay invoice", "billing", ["b1"])
    tele = _reassign_shared_ufs([uf], devs, d2p)
    assert uf.product_feature_id == "billing"
    assert tele["uf_shared_reassigned"] == 0
    assert tele["uf_shared_unresolved"] == 0
    assert tele["uf_shared_reassignments"] == []


def test_deterministic_across_repeated_runs() -> None:
    """Same inputs → identical assignment + telemetry every time."""
    def _once() -> tuple[str | None, dict[str, Any]]:
        devs = [_dev("billing-svc", ["b1", "b2"]), _dev("auth-svc", ["a1"]),
                _dev("shared-lib", ["s1"])]
        d2p = {"billing-svc": ("billing",), "auth-svc": ("auth",),
               "shared-lib": (SHARED,)}
        uf = _uf("Mixed", SHARED, ["b1", "b2", "a1", "s1"])
        tele = _reassign_shared_ufs([uf], devs, d2p)
        return uf.product_feature_id, tele
    r1 = _once()
    r2 = _once()
    assert r1[0] == r2[0] == "billing"
    assert r1[1] == r2[1]


def test_kill_switch_reads_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("FAULTLINE_STAGE_6_7D_UF_RESHARE", raising=False)
    assert _uf_reshare_enabled() is True
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_UF_RESHARE", "0")
    assert _uf_reshare_enabled() is False
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_UF_RESHARE", "1")
    assert _uf_reshare_enabled() is True


# ── Integration through run_journey_abstraction (live + cache-hit replay) ────

class _MemCache:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Any] = {}

    def get(self, kind: str, key: str) -> Any:
        return self.store.get((kind, key))

    def set(self, kind: str, key: str, value: Any) -> None:
        self.store[(kind, key)] = value


class _Msg:
    def __init__(self, text: str) -> None:
        class _B:
            def __init__(self, t: str) -> None:
                self.text = t

        class _U:
            input_tokens = 300
            output_tokens = 150
        self.content = [_B(text)]
        self.usage = _U()


def _client(abstraction: str, reattrib: str) -> Any:
    class _C:
        class _M:
            def create(self, **kw: Any) -> Any:
                sysp = kw.get("system", "")
                return _Msg(reattrib if "assign each developer feature" in sysp
                            else abstraction)
        messages = _M()
    return _C()


def _raising_client() -> Any:
    class _C:
        class _M:
            def create(self, **_kw: Any) -> Any:
                raise RuntimeError("no live call on a cache hit")
        messages = _M()
    return _C()


def _integration_inputs() -> tuple[list[UserFlow], list[Feature], list[Feature]]:
    """A deterministic UF ('UF-001') whose members belong to the billing dev,
    plus a shared-ui dev with no flows. Call-1 (below) misfiles the journey
    onto Shared Platform; the reshare pass must move it to Billing."""
    billing = _dev("billing", ["fx-b1", "fx-b2"])
    shared_ui = Feature(
        name="shared-ui", display_name="shared-ui", description="ui kit",
        paths=["packages/ui/button.tsx"], authors=["a"], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=datetime.now(timezone.utc),
        health_score=90.0, layer="developer",
        member_files=[MemberFile(path="packages/ui/button.tsx", role="anchor",
                                 confidence=1.0)],
    )
    ufs = [UserFlow(id="UF-001", name="Charge card", intent="execute",
                    resource="invoice", member_flow_ids=["fx-b1", "fx-b2"],
                    member_count=2, routes=["/billing"])]
    pfs = [Feature(
        name="web", display_name="web", description="app",
        paths=["billing/x.py"], authors=["a"], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=datetime.now(timezone.utc),
        health_score=90.0, layer="product",
        member_files=[MemberFile(path="billing/x.py", role="anchor",
                                 confidence=1.0)])]
    return ufs, [billing, shared_ui], pfs


_ABS = json.dumps({
    "product_features": [
        {"name": "Billing", "description": "billing"},
        {"name": "Shared Platform", "description": "platform"},
    ],
    "user_flows": [
        # Misfiled onto Shared Platform though its members are billing flows.
        {"name": "Manage billing", "resource": "invoice",
         "product_feature": "Shared Platform", "from_flows": ["UF-001"]},
    ],
})
_MAP = json.dumps({"map": {"billing": "Billing", "shared-ui": "Shared Platform"}})


def test_integration_reshare_moves_shared_uf_off_residual(monkeypatch: Any) -> None:
    """End-to-end: a journey Call-1 assigned to Shared Platform, but whose
    member flows are billing-owned, ends up on Billing after the reshare pass.
    Backstop disabled to isolate the reshare pass."""
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_PF_UF_BACKSTOP", "0")
    ufs_in, devs, pfs = _integration_inputs()
    ufs, _pfs, _dm, tel = run_journey_abstraction(
        ufs_in, pfs, devs, [], client=_client(_ABS, _MAP))
    assert tel["applied"] is True
    (uf,) = ufs
    assert uf.name == "Manage billing"
    assert uf.product_feature_id == "billing"      # moved off the residual
    assert tel["uf_shared_reassigned"] == 1
    assert tel["uf_shared_unresolved"] == 0
    # No UF may remain on the shared/platform bucket (validator I10).
    assert not any((u.product_feature_id or "") in _SHARED_PF_SLUGS for u in ufs)


def test_integration_cache_hit_reshares_identically(monkeypatch: Any) -> None:
    """The reshare runs inside _finish, so a cache-hit replay reassigns byte-
    identically to the live run (proved by a client that would raise if called)."""
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_PF_UF_BACKSTOP", "0")
    cache = _MemCache()
    ufs_in, devs, pfs = _integration_inputs()
    ufs1, _p1, _dm1, tel1 = run_journey_abstraction(
        ufs_in, pfs, devs, [], client=_client(_ABS, _MAP), cache=cache)
    assert tel1["cache_hit"] is False and tel1["uf_shared_reassigned"] == 1

    ufs_in2, devs2, pfs2 = _integration_inputs()
    ufs2, _p2, _dm2, tel2 = run_journey_abstraction(
        ufs_in2, pfs2, devs2, [], client=_raising_client(), cache=cache)
    assert tel2["cache_hit"] is True
    assert tel2["uf_shared_reassigned"] == 1
    assert [(u.id, u.name, u.product_feature_id, u.member_flow_ids) for u in ufs2] \
        == [(u.id, u.name, u.product_feature_id, u.member_flow_ids) for u in ufs1]


def test_integration_kill_switch_leaves_uf_on_residual(monkeypatch: Any) -> None:
    """With the reshare disabled the journey stays on the residual (proves the
    pass is what moves it)."""
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_PF_UF_BACKSTOP", "0")
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_UF_RESHARE", "0")
    ufs_in, devs, pfs = _integration_inputs()
    ufs, _pfs, _dm, tel = run_journey_abstraction(
        ufs_in, pfs, devs, [], client=_client(_ABS, _MAP))
    (uf,) = ufs
    assert uf.product_feature_id == SHARED
    assert "uf_shared_reassigned" not in tel
