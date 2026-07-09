"""Stage 7 naming contract — B9 UF name laws (uniqueness / name-claim / conf).

Three deterministic laws over FINAL members at emission:
  A. UF-vs-UF display uniqueness — no two journeys render identically; the
     labeler's lossy collapse (two action families → one name) is undone, else
     an evidence qualifier is appended (NEVER a numeric suffix).
  B. name-claim — an ORGANIC name that leads with a write verb (create/update/
     delete) while every member is a read is narrowed to "<Browse|View> X";
     a justified wide name (real write member) is untouched.
  C. name_confidence — one evidence rubric for all sources: grounded+unique
     → high, grounded/abstracted → medium, narrowed/thin → low.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, UserFlow

_EPOCH = datetime.fromtimestamp(0, timezone.utc)
from faultline.pipeline_v2.naming_contract import (
    UF_NAME_LAWS_ENV,
    _apply_uf_name_laws,
    load_naming_vocab,
    uf_name_laws_enabled,
)


def _pf(slug: str, display: str) -> Feature:
    f = Feature(name=slug, paths=[], authors=[], total_commits=0, bug_fixes=0,
                bug_fix_ratio=0.0, last_modified=_EPOCH, health_score=80.0,
                layer="product")
    f.display_name = display
    return f


def _uf(uid: str, name: str, pfid: str, members: list[str], *,
        resource: str = "thing", domain: str | None = None,
        conf: str = "high", synthesized: bool = False) -> UserFlow:
    return UserFlow(
        id=uid, name=name, intent="manage", resource=resource,
        product_feature_id=pfid, member_flow_ids=members,
        member_count=len(members), domain=domain, synthesized=synthesized,
        name_confidence=conf)


def _apply(ufs, pfs, flow_names, *, authored=frozenset()):
    vocab = load_naming_vocab()
    pf_by_slug = {str(p.name): p for p in pfs}
    tele: dict = {}
    _apply_uf_name_laws(ufs, pf_by_slug, vocab, flow_names, tele,
                        authored_ids=set(authored), keeper_on=True)
    return tele


# ── flag plumbing ────────────────────────────────────────────────────────

def test_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(UF_NAME_LAWS_ENV, raising=False)
    assert uf_name_laws_enabled() is True
    monkeypatch.setenv(UF_NAME_LAWS_ENV, "0")
    assert uf_name_laws_enabled() is False


# ── Law A — uniqueness ───────────────────────────────────────────────────

def test_labeler_collapsed_action_children_deduped() -> None:
    """Two action families the labeler renamed to the SAME 'Configure Slack'
    revert to their canonical family names — unique, no numeric suffix."""
    flow_names = {"c1": "post-api-slack-flow", "c2": "post-api-slack-bulk-flow",
                  "u1": "patch-api-slack-flow", "u2": "update-slack-flow"}
    ufs = [
        _uf("UF-L-1", "Configure Slack", "slack", ["c1", "c2"],
            resource="slack", domain="lattice:action:slack-create"),
        _uf("UF-L-2", "Configure Slack", "slack", ["u1", "u2"],
            resource="slack", domain="lattice:action:slack-update"),
    ]
    _apply(ufs, [_pf("slack", "Slack")], flow_names)
    names = sorted(u.name for u in ufs)
    assert names == ["Create Slack", "Update Slack"]
    assert len({u.name for u in ufs}) == 2  # unique


def test_three_way_collision_all_unique_no_numeric_suffix() -> None:
    flow_names = {"a": "list-team-flow", "b": "post-api-team-flow",
                  "c": "patch-api-team-flow", "d": "get-team-flow"}
    ufs = [
        _uf("UF-L-1", "Configure teams", "integrations", ["d"],
            resource="team", domain="lattice:dir:team"),
        _uf("UF-L-2", "Configure teams", "teams", ["b"],
            resource="team", domain="lattice:action:team-create"),
        _uf("UF-L-3", "Configure teams", "teams", ["c"],
            resource="team", domain="lattice:action:team-update"),
    ]
    _apply(ufs, [_pf("integrations", "Integrations"), _pf("teams", "Teams")],
           flow_names)
    names = [u.name for u in ufs]
    assert len(set(names)) == 3, f"not unique: {names}"
    assert not any(n[-1].isdigit() for n in names), f"numeric suffix: {names}"


# ── Law B — name-claim ───────────────────────────────────────────────────

def test_read_only_over_claim_narrowed() -> None:
    """'Create and manage webhooks' with one browse member → 'Browse webhook'
    (the write claim is unsupported); confidence drops to low."""
    flow_names = {"v": "view-webhooks-flow"}
    ufs = [_uf("UF-1", "Create and manage webhooks", "webhooks-page", ["v"],
               resource="webhook", conf="high")]
    tele = _apply(ufs, [_pf("webhooks-page", "Webhooks Page")], flow_names)
    assert ufs[0].name == "Browse webhook"
    assert ufs[0].name_confidence == "low"
    assert tele.get("uf_claim_narrowed") == 1


def test_justified_wide_name_not_narrowed() -> None:
    """A real create member supports the 'Create' claim → NOT narrowed."""
    flow_names = {"c": "create-webhook-flow", "l": "list-webhooks-flow",
                  "u": "update-webhook-flow"}
    ufs = [_uf("UF-1", "Create and manage webhooks", "webhooks", ["c", "l", "u"],
               resource="webhook")]
    tele = _apply(ufs, [_pf("webhooks", "Webhooks")], flow_names)
    assert ufs[0].name == "Create and manage webhooks"  # untouched
    assert not tele.get("uf_claim_narrowed")


def test_mixed_member_over_claim_not_renamed_but_flagged() -> None:
    """Write-lead with an act member (not all reads) stays named but is not
    high-confidence (Law C flags it)."""
    flow_names = {"i": "investigate-case-flow", "l": "list-cases-flow"}
    ufs = [_uf("UF-1", "Create case report", "cases", ["i", "l"],
               resource="case")]
    _apply(ufs, [_pf("cases", "Cases")], flow_names)
    assert ufs[0].name == "Create case report"  # not renamed (mixed members)
    assert ufs[0].name_confidence != "high"      # but flagged


def test_lattice_action_child_exempt_from_claim() -> None:
    """A lattice action child is never claim-narrowed (its family name is
    authored by Law A, not a false claim)."""
    flow_names = {"c1": "post-api-slack-flow", "c2": "post-api-slack-bulk-flow"}
    ufs = [_uf("UF-L-1", "Configure Slack", "slack", ["c1", "c2"],
               resource="slack", domain="lattice:action:slack-create")]
    tele = _apply(ufs, [_pf("slack", "Slack")], flow_names)
    assert not tele.get("uf_claim_narrowed")
    assert ufs[0].name == "Configure Slack"  # unique already → unchanged


# ── Law C — confidence rubric ────────────────────────────────────────────

def test_grounded_unique_name_is_high() -> None:
    flow_names = {"c": "create-invoice-flow", "l": "list-invoices-flow"}
    ufs = [_uf("UF-1", "Manage invoices", "invoices", ["c", "l"],
               resource="invoice", conf="low")]
    _apply(ufs, [_pf("invoices", "Invoices")], flow_names)
    assert ufs[0].name_confidence == "high"  # grounded + unique → lifted


def test_memberless_uf_is_low() -> None:
    ufs = [_uf("UF-1", "Manage widgets", "widgets", [], resource="widget",
               conf="high")]
    _apply(ufs, [_pf("widgets", "Widgets")], {})
    assert ufs[0].name_confidence == "low"


# ── protection ───────────────────────────────────────────────────────────

def test_authored_uf_untouched() -> None:
    flow_names = {"v": "view-webhooks-flow"}
    ufs = [_uf("UF-1", "Create and manage webhooks", "webhooks-page", ["v"],
               resource="webhook")]
    tele = _apply(ufs, [_pf("webhooks-page", "Webhooks Page")], flow_names,
                  authored={"UF-1"})
    assert ufs[0].name == "Create and manage webhooks"  # authored → untouched
    assert not tele.get("uf_claim_narrowed")
