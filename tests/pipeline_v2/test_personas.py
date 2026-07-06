"""Wave-3 personas (§4.7): PM Labeler / Surface Adjudicator / Draft
Verifier — happy path, validation guards, kill-switches, cost/decision
taps, cache replay, keyless degrade."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from faultline.llm.cost import CostTracker
from faultline.pipeline_v2.personas import (
    ADJUDICATOR_ENV,
    ESCALATION_MODEL_ENV,
    LABELER_ENV,
    LABELER_MODEL_ENV,
    VERIFIER_ENV,
    adjudicator_enabled,
    build_draft_verifier,
    build_pm_labeler,
    build_surface_adjudicator,
    escalation_model,
    labeler_enabled,
    verifier_enabled,
)


class _FakeClient:
    """Scripted client: pops the next payload per call; records models."""

    def __init__(self, payloads: list[str]) -> None:
        self.messages = self
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        payload = self.payloads.pop(0) if self.payloads else "{}"
        return SimpleNamespace(
            content=[SimpleNamespace(text=payload)],
            usage=SimpleNamespace(input_tokens=500, output_tokens=100),
        )


class _DictCache:
    def __init__(self) -> None:
        self.store: dict = {}

    def get(self, kind, key):
        return self.store.get((kind, key))

    def set(self, kind, key, value):
        self.store[(kind, key)] = value


def _item(key: str, kind: str, current: str, candidates: list[str],
          pf_display: str | None = None, context: dict | None = None):
    return SimpleNamespace(
        key=key, kind=kind, current=current, candidates=candidates,
        context=context or {}, obj=SimpleNamespace(), pf_display=pf_display,
    )


# ── Env gates ───────────────────────────────────────────────────────────


def test_kill_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    for env, fn in ((LABELER_ENV, labeler_enabled),
                    (ADJUDICATOR_ENV, adjudicator_enabled),
                    (VERIFIER_ENV, verifier_enabled)):
        monkeypatch.delenv(env, raising=False)
        assert fn() is True
        monkeypatch.setenv(env, "0")
        assert fn() is False
        monkeypatch.delenv(env, raising=False)

    monkeypatch.delenv(ESCALATION_MODEL_ENV, raising=False)
    assert escalation_model() is None
    monkeypatch.setenv(ESCALATION_MODEL_ENV, "claude-opus-4-7")
    assert escalation_model() == "claude-opus-4-7"


def test_builders_return_none_when_disabled_or_keyless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LABELER_ENV, "0")
    assert build_pm_labeler(model_id="m") is None
    monkeypatch.delenv(LABELER_ENV, raising=False)
    # Keyless: the client factory yields None → deterministic path.
    assert build_pm_labeler(
        model_id="m", _client_factory=lambda: None) is None
    monkeypatch.setenv(VERIFIER_ENV, "0")
    assert build_draft_verifier(model_id="m") is None
    monkeypatch.delenv(VERIFIER_ENV, raising=False)
    monkeypatch.setenv(ADJUDICATOR_ENV, "0")
    assert build_surface_adjudicator(model_id="m") is None
    monkeypatch.delenv(ADJUDICATOR_ENV, raising=False)


# ── PM Labeler ──────────────────────────────────────────────────────────


def test_labeler_candidate_pick_and_taps(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    from faultline.llm import decision_log

    monkeypatch.setenv("FAULTLINE_DECISION_LOG_DIR", str(tmp_path))
    client = _FakeClient([json.dumps({"choices": {
        "gocardless": "banking — gocardless",   # candidate, case-folded
        "uf-1": "Totally Unrelated Words Here",  # composed, unevidenced
    }})])
    tracker = CostTracker()
    labeler = build_pm_labeler(
        model_id="claude-haiku-4-5", cost_tracker=tracker,
        _client_factory=lambda: client)
    assert labeler is not None

    decision_log.begin_scan("w3-personas-test")
    try:
        out = labeler([
            _item("gocardless", "pf", "GoCardless",
                  ["Banking — GoCardless", "GoCardless"]),
            _item("uf-1", "uf", "Manage settings",
                  ["Manage settings"], pf_display="Settings"),
        ])
    finally:
        decision_log.end_scan()

    # Case-insensitive candidate match returns the CANONICAL form.
    assert out["choices"] == {"gocardless": "Banking — GoCardless"}
    assert out["accepted_candidate"] == 1
    assert out["rejected_validation"] == 1  # unevidenced compose rejected
    # Cost tap: one tracked call, persona label.
    assert tracker.call_count == 1
    assert tracker.records[0].label == "persona_pm_labeler"
    # Decision tap: llm_call + decision rows with the candidate sets.
    rows = [json.loads(line) for line in
            (tmp_path / "decisions-w3-personas-test.jsonl")
            .read_text().splitlines()]
    roles = {(r["kind"], r["role"]) for r in rows}
    assert ("llm_call", "pm_labeler") in roles
    assert ("decision", "pm_labeler") in roles
    dec = [r for r in rows if r["kind"] == "decision"][0]
    assert dec["candidates"][0]["id"] == "gocardless"


def test_labeler_composed_grammar_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient([json.dumps({"choices": {
        "uf-ok": "Connect Slack",          # verb-led + evidenced
        "uf-noverb": "Slack connection",   # no verb phrase → reject
        "uf-twin": "Slack",                # equals PF display → reject
    }})])
    labeler = build_pm_labeler(
        model_id="claude-haiku-4-5", _client_factory=lambda: client)
    assert labeler is not None
    items = [
        _item("uf-ok", "uf", "Manage Slack integration",
              ["Manage Slack integration"], pf_display="Slack",
              context={"member_flows": ["slack-oauth-connect-flow"]}),
        _item("uf-noverb", "uf", "Manage Slack integration",
              ["Manage Slack integration"], pf_display="Slack",
              context={"member_flows": ["slack-oauth-connect-flow"]}),
        _item("uf-twin", "uf", "Manage Slack integration",
              ["Manage Slack integration"], pf_display="Slack",
              context={"member_flows": ["slack-oauth-connect-flow"]}),
    ]
    out = labeler(items)
    assert out["choices"] == {"uf-ok": "Connect Slack"}
    assert out["accepted_composed"] == 1
    assert out["rejected_validation"] == 2


def test_labeler_composed_gated_by_verifier() -> None:
    client = _FakeClient([json.dumps({"choices": {
        "uf-a": "Connect Slack",
        "uf-b": "Connect Teams",
    }})])
    rejected_ids: list[str] = []

    def _verifier(drafts):
        # Reject uf-b, accept uf-a; record what we saw.
        rejected_ids.extend(d["id"] for d in drafts)
        return {d["id"]: d["id"] != "uf-b" for d in drafts}

    labeler = build_pm_labeler(
        model_id="claude-haiku-4-5", _client_factory=lambda: client,
        verifier=_verifier)
    assert labeler is not None
    items = [
        _item("uf-a", "uf", "Manage Slack integration", ["x1"],
              pf_display="Slack",
              context={"member_flows": ["slack-connect-flow"]}),
        _item("uf-b", "uf", "Manage Teams integration", ["x2"],
              pf_display="Teams",
              context={"member_flows": ["teams-connect-flow"]}),
    ]
    out = labeler(items)
    assert set(rejected_ids) == {"uf-a", "uf-b"}
    assert out["choices"] == {"uf-a": "Connect Slack"}
    assert out.get("verifier_rejected") == 1


def test_labeler_cache_replay_no_second_call() -> None:
    payload = json.dumps({"choices": {"pf-1": "Billing"}})
    cache = _DictCache()
    client = _FakeClient([payload, payload])
    labeler = build_pm_labeler(
        model_id="claude-haiku-4-5", cache=cache,
        _client_factory=lambda: client)
    items = [_item("pf-1", "pf", "Billing Page", ["Billing", "Billing Page"])]
    out1 = labeler(items)
    assert out1["choices"] == {"pf-1": "Billing"}
    assert len(client.calls) == 1
    out2 = labeler(items)
    assert out2["choices"] == {"pf-1": "Billing"}
    assert out2["cache_hit"] is True
    assert len(client.calls) == 1  # replayed from cache — $0


def test_labeler_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LABELER_MODEL_ENV, "claude-sonnet-4-6")
    client = _FakeClient(["{}"])
    labeler = build_pm_labeler(
        model_id="claude-haiku-4-5", _client_factory=lambda: client)
    labeler([_item("x", "pf", "X Y", ["X Y", "Y X"])])
    assert client.calls[0]["model"].endswith("claude-sonnet-4-6")


# ── Surface Adjudicator ─────────────────────────────────────────────────


def test_adjudicator_verdicts_constrained_to_allowed() -> None:
    client = _FakeClient([json.dumps({"scopes": {
        "pricing": "marketing",     # allowed → applied
        "docs-page": "product",     # allowed → applied
        "sneaky": "legal",          # NOT in allowed → dropped
    }})])
    adj = build_surface_adjudicator(
        model_id="claude-haiku-4-5", _client_factory=lambda: client)
    assert adj is not None
    out = adj([
        {"id": "pricing", "name": "Pricing",
         "allowed": ["product", "marketing"], "signals": {}},
        {"id": "docs-page", "name": "Docs",
         "allowed": ["product", "docs"], "signals": {}},
        {"id": "sneaky", "name": "Sneaky",
         "allowed": ["product", "marketing"], "signals": {}},
    ])
    assert out == {"pricing": "marketing", "docs-page": "product"}


def test_adjudicator_empty_batch_no_call() -> None:
    client = _FakeClient([])
    adj = build_surface_adjudicator(
        model_id="claude-haiku-4-5", _client_factory=lambda: client)
    assert adj([]) == {}
    assert client.calls == []


# ── Draft Verifier ──────────────────────────────────────────────────────


def test_verifier_reject_then_retry_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ESCALATION_MODEL_ENV, "claude-opus-4-7")
    client = _FakeClient([
        json.dumps({"verdicts": {"d1": True, "d2": False}}),
        json.dumps({"verdicts": {"d2": False}}),  # retry (escalated) — final
    ])
    verify = build_draft_verifier(
        model_id="claude-haiku-4-5", _client_factory=lambda: client)
    assert verify is not None
    out = verify([
        {"id": "d1", "kind": "uf", "draft": "Manage billing"},
        {"id": "d2", "kind": "uf", "draft": "schema.json"},
    ])
    assert out == {"d1": True, "d2": False}
    assert len(client.calls) == 2
    # Escalation model fired ONLY on the post-reject retry.
    assert client.calls[0]["model"].endswith("claude-haiku-4-5")
    assert client.calls[1]["model"].endswith("claude-opus-4-7")


def test_verifier_retry_can_flip_to_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ESCALATION_MODEL_ENV, raising=False)
    client = _FakeClient([
        json.dumps({"verdicts": {"d1": False}}),
        json.dumps({"verdicts": {"d1": True}}),
    ])
    verify = build_draft_verifier(
        model_id="claude-haiku-4-5", _client_factory=lambda: client)
    out = verify([{"id": "d1", "kind": "uf", "draft": "Manage billing"}])
    assert out == {"d1": True}
    # No escalation model set → the retry reuses the role model.
    assert client.calls[1]["model"].endswith("claude-haiku-4-5")


def test_verifier_parse_failure_defaults_accept() -> None:
    client = _FakeClient(["not json at all"])
    verify = build_draft_verifier(
        model_id="claude-haiku-4-5", _client_factory=lambda: client)
    out = verify([{"id": "d1", "kind": "uf", "draft": "Manage billing"}])
    # No explicit verdict → caller-side default is ACCEPT (missing key).
    assert out.get("d1") is not False


# ── Naming-stage integration (labeler applies through the stage) ────────


def test_naming_stage_applies_labeler_choices_with_law_recheck() -> None:
    from datetime import datetime, timezone

    from faultline.models.types import Feature, UserFlow
    from faultline.pipeline_v2.naming_contract import run_naming_contract

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pf = Feature(
        name="gocardless", display_name="Gocardless",
        anchor_id="hub:packages/banking/src/providers/gocardless",
        layer="product", paths=[], authors=["a"], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=now,
        health_score=100.0,
    )
    uf = UserFlow(
        id="UF-001", name="Gocardless", resource="gocardless", domain=None,
        product_feature_id="gocardless", intent="manage",
        member_flow_ids=["sync-transactions-flow"], member_count=1,
        synthesized=True,
    )

    def _labeler(pending):
        # Pick the bare vendor for the PF (a listed candidate) and a
        # law-VIOLATING twin for the UF (must be re-blocked by the stage).
        choices = {}
        for it in pending:
            choices[it.key] = ("GoCardless" if it.kind == "pf"
                               else "GoCardless")
        return {"choices": choices}

    tele = run_naming_contract([pf], [uf], [], labeler=_labeler)
    assert pf.display_name == "GoCardless"          # labeler pick applied
    assert uf.name == "Ingest data from GoCardless"  # law re-check blocked twin
    assert tele["labeler"]["applied"] == 1


# ── Surface-taxonomy adjudication wiring (W3 commit N4) ─────────────────


def test_classify_feature_with_signals_verdict_matches_legacy() -> None:
    from faultline.pipeline_v2.surface_taxonomy import SurfaceScopeClassifier

    clf = SurfaceScopeClassifier()
    shapes = [
        SimpleNamespace(name="pricing", display_name="Pricing",
                        paths=["app/pricing/page.tsx",
                               "app/workspace/pricing-table.tsx"]),
        SimpleNamespace(name="billing", display_name="Billing",
                        paths=["app/workspace/billing/page.tsx"]),
        SimpleNamespace(name="home-page", display_name="Home Page",
                        paths=["app/page.tsx"]),
        SimpleNamespace(name="mkt", display_name="Mkt",
                        paths=["app/(marketing)/pricing/page.tsx",
                               "app/(marketing)/about/page.tsx"]),
        SimpleNamespace(name="empty", display_name="Empty", paths=[]),
    ]
    for f in shapes:
        scope, _amb, _sig = clf.classify_feature_with_signals(f, {})
        assert scope == clf.classify_feature(f, {}), f.name


def test_ambiguity_narrow_notion() -> None:
    from faultline.pipeline_v2.surface_taxonomy import SurfaceScopeClassifier

    clf = SurfaceScopeClassifier()
    # Non-product signal denied the majority by abstains → ambiguous.
    _s, amb, _w = clf.classify_feature_with_signals(SimpleNamespace(
        name="pricing", display_name="Pricing",
        paths=["app/pricing/page.tsx", "src/x.ts", "src/y.ts"]), {})
    assert amb is True
    # Unanimous non-product → NOT ambiguous (nothing to adjudicate).
    _s, amb, _w = clf.classify_feature_with_signals(SimpleNamespace(
        name="mkt", display_name="Mkt",
        paths=["app/(marketing)/pricing/page.tsx",
               "app/(marketing)/about/page.tsx"]), {})
    assert amb is False
    # No signal at all → NOT ambiguous.
    _s, amb, _w = clf.classify_feature_with_signals(SimpleNamespace(
        name="billing", display_name="Billing",
        paths=["app/workspace/billing/page.tsx"]), {})
    assert amb is False
    # Shell container → NOT ambiguous (its interior names things, §4.6).
    _s, amb, _w = clf.classify_feature_with_signals(SimpleNamespace(
        name="home-page", display_name="Home Page",
        paths=["app/page.tsx"]), {})
    assert amb is False


def _emission_fixture():
    from datetime import datetime, timezone

    from faultline.models.types import Feature

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _feat(name, paths, layer):
        return Feature(
            name=name, display_name=name.title(), layer=layer, paths=paths,
            authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
            last_modified=now, health_score=100.0,
        )

    # Ambiguous: one marketing path + two abstains (observed shape).
    pricing = _feat("pricing", ["app/pricing/page.tsx", "src/x.ts",
                                "src/y.ts"], "product")
    billing = _feat("billing", ["app/workspace/billing/page.tsx"], "product")
    return [pricing, billing]


def test_emission_taxonomy_adjudicator_flip_moves_pf_to_lane() -> None:
    from faultline.pipeline_v2.surface_taxonomy import apply_emission_taxonomy

    pfs = _emission_fixture()
    seen_items: list[dict] = []

    def _adjudicator(items):
        seen_items.extend(items)
        return {i["id"]: "marketing" for i in items}

    tele, lane, kept = apply_emission_taxonomy(
        [], pfs, [], [], None, adjudicator=_adjudicator)
    # Only the ambiguous PF was sent; billing (no signal) was not.
    assert [i["id"] for i in seen_items] == ["pricing"]
    assert "marketing" in seen_items[0]["allowed"]
    assert tele["adjudicator"]["flips"] == 1
    assert [p.name for p in kept] == ["billing"]
    assert [e["name"] for e in lane] == ["pricing"]


def test_emission_taxonomy_invalid_adjudicator_scope_ignored() -> None:
    from faultline.pipeline_v2.surface_taxonomy import apply_emission_taxonomy

    pfs = _emission_fixture()

    def _adjudicator(items):
        return {i["id"]: "legal" for i in items}  # NOT in allowed set

    tele, lane, kept = apply_emission_taxonomy(
        [], pfs, [], [], None, adjudicator=_adjudicator)
    assert tele["adjudicator"]["flips"] == 0
    assert [p.name for p in kept] == ["pricing", "billing"]
    assert lane == []


def test_emission_taxonomy_none_adjudicator_deterministic() -> None:
    from faultline.pipeline_v2.surface_taxonomy import apply_emission_taxonomy

    pfs_a = _emission_fixture()
    pfs_b = _emission_fixture()
    tele_a, lane_a, kept_a = apply_emission_taxonomy(
        [], pfs_a, [], [], None, adjudicator=None)
    tele_b, lane_b, kept_b = apply_emission_taxonomy(
        [], pfs_b, [], [], None)
    assert [p.surface_scope for p in pfs_a] == [
        p.surface_scope for p in pfs_b]
    assert "adjudicator" not in tele_a and "adjudicator" not in tele_b


# ── Synth-UF verification through the naming stage (W3 commit N5) ───────


def _synth_fixture():
    from datetime import datetime, timezone

    from faultline.models.types import Feature, UserFlow

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pf = Feature(
        name="settings", display_name="Settings",
        anchor_id="route:apps/dashboard/settings", layer="product",
        paths=[], authors=["a"], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=now, health_score=100.0,
    )
    uf = UserFlow(
        id="UF-001", name="Settings", resource="settings", domain=None,
        product_feature_id="settings", intent="manage",
        member_flow_ids=["edit-settings-flow"], member_count=1,
        synthesized=True, synthesis_reason="uncovered_product_feature_backstop",
    )
    return pf, uf


def test_naming_stage_verifier_reviews_synth_ufs() -> None:
    from faultline.pipeline_v2.naming_contract import run_naming_contract

    pf, uf = _synth_fixture()
    seen: list[dict] = []

    def _verifier(drafts):
        seen.extend(drafts)
        return {d["id"]: True for d in drafts}  # accept all

    tele = run_naming_contract([pf], [uf], [], verifier=_verifier)
    assert len(seen) == 1
    d = seen[0]
    assert d["kind"] == "synth_uf"
    assert d["draft"] == "Manage settings"  # post-law template draft
    assert d["pf_display"] == "Settings"
    assert d["synthesis_reason"] == "uncovered_product_feature_backstop"
    assert tele["verifier_synth_reviewed"] == 1
    assert tele["verifier_synth_rejected"] == 0
    assert uf.name == "Manage settings"


def test_naming_stage_verifier_reject_falls_back_to_template() -> None:
    """A rejected synth draft reverts to the deterministic journey
    template (never dropped — dropping would re-arm I8)."""
    from faultline.pipeline_v2.naming_contract import run_naming_contract

    pf, uf = _synth_fixture()

    def _labeler(pending):
        # The labeler composes something the verifier will then reject.
        return {"choices": {
            it.key: "View settings" for it in pending if it.kind == "uf"
        }}

    def _verifier(drafts):
        return {d["id"]: False for d in drafts}  # reject everything

    tele = run_naming_contract(
        [pf], [uf], [], labeler=_labeler, verifier=_verifier)
    assert tele["verifier_synth_rejected"] == 1
    # Deterministic template restored; UF survived (I8 safe).
    assert uf.name == "Manage settings"
    assert uf.synthesized is True


def test_naming_stage_verifier_exception_never_blocks() -> None:
    from faultline.pipeline_v2.naming_contract import run_naming_contract

    pf, uf = _synth_fixture()

    def _verifier(drafts):
        raise RuntimeError("persona exploded")

    tele = run_naming_contract([pf], [uf], [], verifier=_verifier)
    assert uf.name == "Manage settings"  # law fix still applied
    assert "verifier_error" in tele


def test_emission_taxonomy_journey_guard_blocks_adjudication() -> None:
    """W3 mini-A/B finding (openstatus `notifications`, 8 journeys):
    a PF referenced by >=2 user flows is product-evidenced — never sent
    to the adjudicator (the I9 flowful-never-in-lane law at PF grain)."""
    from faultline.models.types import UserFlow
    from faultline.pipeline_v2.surface_taxonomy import apply_emission_taxonomy

    pfs = _emission_fixture()  # 'pricing' is the ambiguous one

    def _uf(uid: str) -> UserFlow:
        return UserFlow(
            id=uid, name=f"Journey {uid}", resource="pricing", domain=None,
            product_feature_id="pricing", intent="manage",
            member_flow_ids=[f"flow-{uid}"], member_count=1,
        )

    seen: list[dict] = []

    def _adjudicator(items):
        seen.extend(items)
        return {i["id"]: "marketing" for i in items}

    ufs = [_uf("UF-001"), _uf("UF-002")]
    tele, lane, kept = apply_emission_taxonomy(
        [], pfs, ufs, [], None, adjudicator=_adjudicator)
    assert seen == []  # journey-rich PF never reached the persona
    assert tele["adjudicator"]["journey_guarded"] == 1
    assert tele["adjudicator"]["flips"] == 0
    assert [p.name for p in kept] == ["pricing", "billing"]
