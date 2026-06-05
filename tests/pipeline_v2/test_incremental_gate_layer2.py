"""Tests for Stage-B incremental Layer-2 + User-Flow reuse helpers.

These cover the second cost ceiling from finding-incremental-no-llm-savings:
Stage 8 (Sonnet analyst) and Stage 6.7b (Haiku UF refiner). The helpers
under test are pure / deterministic — no LLM — and only ever run on the
``--since`` path. They let an incremental scan reuse the base scan's
Layer-2 ``product_features`` and refined ``user_flows`` when the
deterministic structure is unchanged, instead of paying for both LLM
stages again.
"""

from __future__ import annotations

from faultline.models.types import UserFlow
from faultline.pipeline_v2.incremental_gate import (
    apply_base_uf_refinement,
    base_product_features,
    base_refinement_by_member_set,
    base_user_flows,
    is_noop_change,
    plan_uf_refinement_reuse,
    rehydrate_base_product_features,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _uf(
    uf_id: str,
    *,
    domain: str | None,
    members: list[str],
    name: str = "template-name",
    intent: str = "other",
    refined: bool = False,
) -> UserFlow:
    return UserFlow(
        id=uf_id,
        name=name,
        domain=domain,
        product_feature_id=domain,
        intent=intent,
        resource="thing",
        member_flow_ids=members,
        member_count=len(members),
        refined=refined,
    )


def _uf_dict(
    uf_id: str,
    *,
    domain: str | None,
    members: list[str],
    name: str,
    intent: str = "manage",
    ui_tier: str = "panel",
    acceptance: list[str] | None = None,
    refined: bool = True,
) -> dict:
    return {
        "id": uf_id,
        "name": name,
        "description": f"{name} description",
        "domain": domain,
        "product_feature_id": domain,
        "intent": intent,
        "resource": "thing",
        "member_flow_ids": members,
        "member_count": len(members),
        "ui_tier": ui_tier,
        "acceptance": acceptance or ["AC-1"],
        "refined": refined,
    }


def _pf_dict(name: str, *, paths: list[str]) -> dict:
    """A complete Layer-2 product Feature dict as the base scan serialises
    it (post Stage 6 metrics — all required Feature fields present)."""
    return {
        "name": name,
        "display_name": name.title(),
        "layer": "product",
        "paths": paths,
        "authors": ["a"],
        "total_commits": 3,
        "bug_fixes": 0,
        "bug_fix_ratio": 0.0,
        "last_modified": "2026-06-01T00:00:00+00:00",
        "health_score": 90.0,
        "flows": [],
    }


def _base_scan(
    *,
    product_features: list[dict] | None = None,
    user_flows: list[dict] | None = None,
    developer_features: list[dict] | None = None,
) -> dict:
    return {
        "developer_features": developer_features or [],
        "product_features": product_features or [],
        "user_flows": user_flows or [],
    }


# ── is_noop_change ───────────────────────────────────────────────────────


def test_is_noop_change_true_only_for_zero():
    assert is_noop_change(0) is True
    assert is_noop_change(1) is False
    assert is_noop_change(5) is False
    # the sentinel -1 (no gate meta) must NOT count as no-op
    assert is_noop_change(-1) is False


# ── base scan accessors ──────────────────────────────────────────────────


def test_base_accessors_tolerate_missing_or_wrong_type():
    assert base_product_features({}) == []
    assert base_user_flows({}) == []
    assert base_product_features({"product_features": "nope"}) == []
    assert base_user_flows({"user_flows": 42}) == []


# ── rehydrate_base_product_features ──────────────────────────────────────


def test_rehydrate_base_pfs_and_dev_map():
    base = _base_scan(
        product_features=[
            _pf_dict("billing", paths=["a.ts", "b.ts"]),
            _pf_dict("auth", paths=["c.ts"]),
        ],
        developer_features=[
            {"name": "stripe-webhook", "product_feature_id": "billing"},
            {"name": "checkout", "product_feature_id": "billing"},
            {"name": "login", "product_feature_id": "auth"},
            {"name": "orphan", "product_feature_id": None},
        ],
    )
    pfs, dev_map = rehydrate_base_product_features(base)
    assert {p.name for p in pfs} == {"billing", "auth"}
    assert dev_map["stripe-webhook"] == ("billing",)
    assert dev_map["login"] == ("auth",)
    # unmapped dev feature is absent from the map
    assert "orphan" not in dev_map


def test_rehydrate_skips_invalid_pf_without_crashing():
    base = _base_scan(
        product_features=[
            _pf_dict("ok", paths=["a.ts"]),
            {"not_a_feature": True, "paths": 12345},  # invalid → skipped
            "garbage-string",
        ],
    )
    pfs, _ = rehydrate_base_product_features(base)
    assert [p.name for p in pfs] == ["ok"]


# ── UF refinement reuse ──────────────────────────────────────────────────


def test_refinement_index_only_indexes_refined():
    base = _base_scan(
        user_flows=[
            _uf_dict("UF-001", domain="d", members=["f1"], name="Refined One"),
            _uf_dict("UF-002", domain="d", members=["f2"], name="Not Refined",
                     refined=False),
        ],
    )
    idx = base_refinement_by_member_set(base)
    assert frozenset(["f1"]) in idx
    assert frozenset(["f2"]) not in idx  # un-refined base UF not reusable


def test_apply_base_uf_refinement_copies_presentation_only():
    uf = _uf("UF-009", domain="d", members=["f1"], name="template", intent="other")
    base_uf = _uf_dict("UF-001", domain="d", members=["f1"], name="Create a thing",
                       intent="author", ui_tier="full-page", acceptance=["AC-1", "AC-2"])
    apply_base_uf_refinement(uf, base_uf)
    assert uf.name == "Create a thing"
    assert uf.intent == "author"
    assert uf.ui_tier == "full-page"
    assert uf.acceptance == ["AC-1", "AC-2"]
    assert uf.refined is True
    # structural identity preserved — id + members never copied
    assert uf.id == "UF-009"
    assert uf.member_flow_ids == ["f1"]


def test_plan_reuse_all_unchanged_no_rescan_domains():
    """Every fresh UF matches a refined base twin → zero domains to rescan."""
    base = _base_scan(
        user_flows=[
            _uf_dict("UF-001", domain="billing", members=["f1"], name="Pay"),
            _uf_dict("UF-002", domain="auth", members=["f2"], name="Sign in"),
        ],
    )
    fresh = [
        _uf("UF-100", domain="billing", members=["f1"]),
        _uf("UF-101", domain="auth", members=["f2"]),
    ]
    plan = plan_uf_refinement_reuse(fresh, base)
    assert plan.rescan_domains == set()
    assert plan.reused_domains == {"billing", "auth"}
    assert plan.reused_uf_count == 2
    # presentation adopted from base
    assert fresh[0].name == "Pay" and fresh[0].refined is True
    assert fresh[1].name == "Sign in" and fresh[1].refined is True


def test_plan_reuse_changed_member_set_forces_domain_rescan():
    """A UF whose members changed → its domain stays in rescan_domains."""
    base = _base_scan(
        user_flows=[
            _uf_dict("UF-001", domain="billing", members=["f1"], name="Pay"),
            _uf_dict("UF-002", domain="auth", members=["f2"], name="Sign in"),
        ],
    )
    fresh = [
        # billing UF gained a member → no base twin → rescan billing
        _uf("UF-100", domain="billing", members=["f1", "f3"]),
        # auth UF unchanged → reused
        _uf("UF-101", domain="auth", members=["f2"]),
    ]
    plan = plan_uf_refinement_reuse(fresh, base)
    assert plan.rescan_domains == {"billing"}
    assert plan.reused_domains == {"auth"}
    assert plan.reused_uf_count == 1
    # auth adopted base presentation; billing kept its template name
    assert fresh[1].name == "Sign in" and fresh[1].refined is True
    assert fresh[0].name == "template-name" and fresh[0].refined is False


def test_plan_reuse_partial_domain_rescans_whole_domain():
    """One changed UF in a domain forces a rescan of that whole domain
    (one Haiku call per domain), even if a sibling UF was unchanged."""
    base = _base_scan(
        user_flows=[
            _uf_dict("UF-001", domain="billing", members=["f1"], name="Pay"),
            _uf_dict("UF-002", domain="billing", members=["f2"], name="Refund"),
        ],
    )
    fresh = [
        _uf("UF-100", domain="billing", members=["f1"]),          # unchanged
        _uf("UF-101", domain="billing", members=["f2", "f9"]),    # changed
    ]
    plan = plan_uf_refinement_reuse(fresh, base)
    assert plan.rescan_domains == {"billing"}
    assert plan.reused_domains == set()
    # the unchanged sibling still adopted base presentation upstream
    assert fresh[0].name == "Pay" and fresh[0].refined is True


def test_plan_reuse_empty_user_flows():
    plan = plan_uf_refinement_reuse([], _base_scan())
    assert plan.rescan_domains == set()
    assert plan.reused_domains == set()
    assert plan.reused_uf_count == 0


# ── refine_user_flows domain_allowlist gating ────────────────────────────


class _CountingClient:
    """Anthropic-like fake that counts ``messages.create`` calls and
    returns a minimal valid refinement for whatever UF ids it was asked
    about (so refined domains succeed)."""

    def __init__(self) -> None:
        self.calls = 0
        outer = self

        class _Messages:
            def create(self, **kw):  # noqa: ANN003
                outer.calls += 1
                return outer._reply(kw)

        self.messages = _Messages()

    def _reply(self, _kw: dict):
        import json as _json
        from dataclasses import dataclass as _dc

        @_dc
        class _Usage:
            input_tokens: int = 100
            output_tokens: int = 40

        @_dc
        class _Block:
            text: str

        class _Msg:
            def __init__(self, text: str):
                self.content = [_Block(text=text)]
                self.usage = _Usage()

        # Empty-but-valid JSON object: domains "succeed" (no degrade) but
        # apply no field changes — the test only cares about CALL COUNT.
        return _Msg(_json.dumps({}))


def test_domain_allowlist_none_calls_every_domain():
    """Full-scan behaviour (allowlist None): one call per domain."""
    from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows

    ufs = [
        _uf("UF-1", domain="billing", members=["f1"]),
        _uf("UF-2", domain="auth", members=["f2"]),
    ]
    client = _CountingClient()
    _, tel = refine_user_flows(ufs, [], client=client, domain_allowlist=None)
    assert client.calls == 2  # billing + auth
    assert tel["domains_reused"] == 0


def test_domain_allowlist_restricts_calls_to_listed_domains():
    """Incremental: only allowlisted domains get a Haiku call; the rest
    are reused (zero calls)."""
    from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows

    ufs = [
        _uf("UF-1", domain="billing", members=["f1"]),
        _uf("UF-2", domain="auth", members=["f2"]),
        _uf("UF-3", domain="settings", members=["f3"]),
    ]
    client = _CountingClient()
    # Only "billing" changed; auth + settings reused.
    _, tel = refine_user_flows(
        ufs, [], client=client, domain_allowlist={"billing"},
    )
    assert client.calls == 1  # ONLY billing
    assert tel["domains_reused"] == 2  # auth + settings skipped


def test_domain_allowlist_empty_skips_all_calls():
    """No-op incremental (empty allowlist): zero Haiku calls."""
    from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows

    ufs = [
        _uf("UF-1", domain="billing", members=["f1"]),
        _uf("UF-2", domain="auth", members=["f2"]),
    ]
    client = _CountingClient()
    _, tel = refine_user_flows(
        ufs, [], client=client, domain_allowlist=set(),
    )
    assert client.calls == 0
    assert tel["domains_reused"] == 2
