"""Align-v2 — grain-gated anchor alignment (Phase 3.1).

Covers the five test areas from the align-v2 brief:
  1. tier classification per SOURCE; i18n leaf VALUES are never tier-1
  2. gate truth table on the recorded Phase-3.0 pool shapes
     (formbricks / supabase / cal-com / Soc0)
  3. gate-refused path is byte-identical to align-OFF — outputs AND cache keys
  4. telemetry (align_decision) + degradation emit on requested-but-refused
  5. i18n NAMESPACE-key extraction yields key paths (humanised), not values

The LLM is always mocked. No network, no real cache.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from faultline.pipeline_v2.anchor_extractors import (
    TIER1_ACTION,
    TIER2_ADVISORY,
    ProductAnchor,
    anchor_tier,
    build_alignment_pool,
    distinct_tier_counts,
    extract_i18n_anchors,
    extract_raw_anchors,
)
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    ALIGN_ENV,
    _canonical_anchor_texts,
    _grain_gate,
    run_journey_abstraction,
)

# ── shared fixtures (mirror test_stage_6_7d_llm_journey_abstraction) ────────


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _Block:
    text: str


class _Msg:
    def __init__(self, text: str) -> None:
        self.content = [_Block(text=text)]
        self.usage = _Usage(400, 200)


def _client(abstraction: str, reattrib: str) -> Any:
    class _C:
        class _M:
            def create(self, **kw: Any) -> Any:
                sysp = kw.get("system", "")
                return _Msg(reattrib if "assign each developer feature" in sysp else abstraction)
        messages = _M()
    return _C()


def _feat(name: str, paths: list[str]):
    from faultline.models.types import Feature, MemberFile
    return Feature(
        name=name, display_name=name, description=f"{name} module",
        paths=paths, authors=["a"], total_commits=3, bug_fixes=1,
        bug_fix_ratio=0.33, last_modified=datetime.now(timezone.utc),
        health_score=90.0, layer="developer",
        member_files=[MemberFile(path=p, role="anchor", confidence=1.0) for p in paths],
    )


def _uf(uf_id: str, name: str, members: list[str], resource: str = "thing"):
    from faultline.models.types import UserFlow
    return UserFlow(
        id=uf_id, name=name, intent="author", resource=resource,
        member_flow_ids=members, member_count=len(members),
        routes=[f"/{name}"],
    )


_ABS = json.dumps({
    "product_features": [
        {"name": "Account Management", "description": "manage accounts"},
        {"name": "Authentication", "description": "sign in/up"},
    ],
    "user_flows": [
        {"name": "Manage accounts", "resource": "account",
         "product_feature": "Account Management", "from_flows": ["UF-001", "UF-002"]},
        {"name": "Sign in", "resource": "session",
         "product_feature": "Authentication", "from_flows": ["UF-003"]},
    ],
})
_MAP = json.dumps({"map": {
    "accounts": "Account Management", "auth": "Authentication",
    "shared-ui": "Shared Platform",
}})


def _anchors(texts: list[str], source: str = "analytics", tier: str = ""):
    return [
        ProductAnchor(text=t, source=source, locator=f"src/{i}.ts", tier=tier)  # type: ignore[arg-type]
        for i, t in enumerate(texts)
    ]


def _n_anchors(n: int, source: str = "analytics", tier: str = "", prefix: str = "Cap"):
    return _anchors([f"{prefix} {i}" for i in range(n)], source=source, tier=tier)


class _RecCache:
    """Records every (op, kind, key) — asserts cache-key invariance."""

    def __init__(self) -> None:
        self.ops: list[tuple[str, str, str]] = []

    def get(self, kind: str, key: str) -> None:
        self.ops.append(("get", kind, key))
        return None

    def set(self, kind: str, key: str, value: Any) -> None:
        self.ops.append(("set", kind, key))


# ── 1. tier classification per source ───────────────────────────────────────


def test_default_tier_by_source() -> None:
    """SOURCE decides the default tier: analytics/nav/docs are action-grain;
    i18n (leaf default) / test / docs_nav are advisory."""
    assert ProductAnchor("Track Event", "analytics", "a.ts").tier == TIER1_ACTION
    assert ProductAnchor("Settings", "nav", "nav.tsx").tier == TIER1_ACTION
    assert ProductAnchor("Pricing", "docs", "x").tier == TIER1_ACTION
    assert ProductAnchor("Sign in to continue", "i18n", "en.json").tier == TIER2_ADVISORY
    assert ProductAnchor("creates a booking", "test", "a.spec.ts").tier == TIER2_ADVISORY
    assert ProductAnchor("Getting Started", "docs_nav", "sidebars.js").tier == TIER2_ADVISORY


def test_i18n_namespace_key_can_be_tier1_explicitly() -> None:
    a = ProductAnchor("Billing", "i18n", "en.json#billing", tier=TIER1_ACTION)
    assert a.tier == TIER1_ACTION
    assert anchor_tier(a) == TIER1_ACTION


def test_anchor_tier_robust_to_foreign_objects() -> None:
    """Duck-typed anchors without a tier attr derive from source; unknown
    source falls to tier-2 (never counts toward the gate)."""
    class _Foreign:
        text = "Something"
        source = "analytics"
    assert anchor_tier(_Foreign()) == TIER1_ACTION

    class _Unknown:
        text = "Mystery"
        source = "weird"
    assert anchor_tier(_Unknown()) == TIER2_ADVISORY


def test_distinct_tier_counts_case_insensitive() -> None:
    anchors = (
        _anchors(["Billing", "billing", "Auth"])          # 2 distinct tier1
        + _anchors(["Some UI copy", "More copy"], source="test")  # 2 tier2
    )
    assert distinct_tier_counts(anchors) == (2, 2)


# ── 5. i18n namespace-key extraction: key paths, not values ─────────────────


def _locale_repo(tmp_path, payload: dict) -> Any:
    loc = tmp_path / "web" / "locales" / "en"
    loc.mkdir(parents=True)
    (loc / "common.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def test_i18n_namespace_keys_tier1_leaf_values_tier2(tmp_path) -> None:
    repo = _locale_repo(tmp_path, {
        "billing": {"title": "Billing & Payments",
                    "invoice": {"paid": "Your invoice was paid"}},
        "auth": {"login": "Sign In To Your Account"},
    })
    anchors = extract_i18n_anchors(repo)
    tier1 = {a.text for a in anchors if a.tier == TIER1_ACTION}
    tier2 = {a.text for a in anchors if a.tier == TIER2_ADVISORY}
    # namespace KEYS (humanised) are tier-1 and carry a #key locator
    assert tier1 == {"Billing", "Auth"}
    assert all("#" in a.locator for a in anchors if a.tier == TIER1_ACTION)
    # leaf VALUES are tier-2 — never tier-1
    assert {"Billing & Payments", "Your invoice was paid",
            "Sign In To Your Account"} <= tier2
    assert not (tier1 & tier2)


def test_namespace_wins_dedup_collision_against_same_text_leaf(tmp_path) -> None:
    """When a namespace key humanises to the same text as a leaf value, the
    raw dedup must keep the TIER-1 (namespace) anchor — else the vocabulary
    silently shrinks at the gate."""
    repo = _locale_repo(tmp_path, {"billing": {"x": "Billing"}})
    raw = extract_raw_anchors(repo)
    billing = [a for a in raw if a.text.lower() == "billing"]
    assert len(billing) == 1
    assert billing[0].tier == TIER1_ACTION


def test_alignment_pool_prefers_tier1_within_i18n(tmp_path) -> None:
    repo = _locale_repo(tmp_path, {
        f"namespace_{i}": {"leaf": f"Leaf copy number {i}"} for i in range(20)
    })
    pool = build_alignment_pool(extract_raw_anchors(repo))
    i18n = [a for a in pool if a.source == "i18n"]
    first_20 = i18n[:20]
    assert all(a.tier == TIER1_ACTION for a in first_20)


# ── 2. gate truth table on MEASURED Phase-3.0 pool shapes ────────────────────
# (tier-1 / candidate-journey counts measured 2026-07-02 on the actual clones
#  with the current extractor + the actual 6.7d input artifacts)


def test_candidate_journeys_is_distinct_resources() -> None:
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        _candidate_journeys,
    )
    ufs = [
        _uf("UF-001", "Create account", ["f1"], resource="account"),
        _uf("UF-002", "Update account", ["f2"], resource="Account"),  # case-folds
        _uf("UF-003", "Sign in", ["f3"], resource="session"),
    ]
    assert _candidate_journeys(ufs) == 2
    # no resources at all → conservative fallback to the flow count
    bare = [_uf("UF-001", "A", ["f1"], resource=""),
            _uf("UF-002", "B", ["f2"], resource="")]
    assert _candidate_journeys(bare) == 2
    assert _candidate_journeys([]) == 0


def test_gate_formbricks_shape_refuses() -> None:
    """formbricks: 11 coarse i18n DOMAIN namespaces vs 103 candidate journeys
    → free-gen (the measured −9..−14.5 F1 failure align-v2 exists to prevent)."""
    pool = (
        _n_anchors(11, source="i18n", tier=TIER1_ACTION, prefix="Domain")
        + _n_anchors(300, source="i18n", prefix="Ui copy")  # leaves, tier2
    )
    granted, t1, t2 = _grain_gate(pool, 103)
    assert (granted, t1, t2) == (False, 11, 300)


def test_gate_supabase_shape_grants() -> None:
    """supabase: 63 analytics events + 26 nav labels (89 tier-1) vs 61 distinct
    resources among 126 input flows → align (the measured +7..+10 win — gating
    on the raw 126 flow count would wrongly refuse this repo)."""
    pool = (
        _n_anchors(63, source="analytics", prefix="Event")
        + _n_anchors(26, source="nav", prefix="Nav")
    )
    granted, t1, _t2 = _grain_gate(pool, 61)
    assert granted is True
    assert t1 == 89
    # the raw flow count would refuse — exactly why the gate is journey-grain
    assert _grain_gate(pool, 126)[0] is False


def test_gate_calcom_shape_grants() -> None:
    """cal-com: thousands of fine i18n NAMESPACE keys vs ~240 candidate
    journeys → align (measured +9..+12)."""
    pool = _n_anchors(1000, source="i18n", tier=TIER1_ACTION, prefix="Key")
    granted, t1, _t2 = _grain_gate(pool, 248)
    assert granted is True
    assert t1 == 1000


def test_gate_documenso_shape_refuses() -> None:
    """documenso: 5 tier-1 anchors (sparse-signal boundary) vs ~55-86 candidate
    journeys → free-gen; also below the floor."""
    pool = (_n_anchors(5, source="analytics", prefix="Event")
            + _n_anchors(900, source="test", prefix="Spec"))
    granted, t1, t2 = _grain_gate(pool, 55)
    assert (granted, t1, t2) == (False, 5, 900)


def test_gate_soc0_shape_refuses() -> None:
    """Leaf-value-heavy pool (the 0.03-Jaccard lesson): tiny tier-1 → free-gen,
    no matter how large the tier-2 noise pool is."""
    pool = (
        _n_anchors(5, source="nav", prefix="Nav")
        + _n_anchors(500, source="i18n", prefix="Noise")
    )
    granted, t1, t2 = _grain_gate(pool, 30)
    assert (granted, t1, t2) == (False, 5, 500)


def test_gate_floor_applies_even_on_tiny_repos() -> None:
    assert _grain_gate(_n_anchors(7), 3)[0] is False   # 7 < floor 8
    assert _grain_gate(_n_anchors(8), 3)[0] is True


def test_gate_uses_raw_anchors_when_provided(monkeypatch) -> None:
    """The gate measures the RAW vocabulary (pool caps would understate it):
    a capped pool of 8 tier-1 anchors with a rich raw extraction still aligns
    when raw tier-1 >= candidate journeys."""
    monkeypatch.setenv(ALIGN_ENV, "1")
    ufs = [_uf(f"UF-{i:03d}", f"Flow {i}", [f"f{i}"], resource=f"res{i}")
           for i in range(1, 13)]              # 12 flows, 12 distinct resources
    pool = _n_anchors(8)                       # 8 < 12 → pool alone would refuse
    raw = _n_anchors(40)                       # 40 >= 12 → raw grants
    devs = [_feat("accounts", ["app/accounts/a.ts"]), _feat("auth", ["app/auth/l.ts"])]
    _u, _p, _m, tel = run_journey_abstraction(
        ufs, [_feat("web", ["app/a.ts"])], devs, [],
        product_anchors=pool, raw_anchors=raw, client=_client(_ABS, _MAP))
    assert tel["aligned"] is True
    assert tel["align_decision"]["tier1_count"] == 40
    assert tel["align_decision"]["candidate_ufs"] == 12
    assert tel["align_decision"]["candidate_journeys"] == 12


# ── tier-2 never reaches the prompt ──────────────────────────────────────────


def test_canonical_anchor_texts_tier1_only() -> None:
    pool = (
        _anchors(["Track Signup", "Invite Member"])                 # tier1
        + _anchors(["Some long UI sentence"], source="i18n")        # tier2 leaf
        + _anchors(["creates a booking"], source="test")            # tier2
    )
    assert _canonical_anchor_texts(pool) == ["Track Signup", "Invite Member"]


def test_align_prompt_carries_only_tier1_texts(monkeypatch) -> None:
    monkeypatch.setenv(ALIGN_ENV, "1")
    seen_prompts: list[str] = []

    class _SpyClient:
        class _M:
            def create(self, **kw: Any) -> Any:
                seen_prompts.append(kw.get("user", "") or kw["messages"][0]["content"])
                sysp = kw.get("system", "")
                return _Msg(_MAP if "assign each developer feature" in sysp else _ABS)
        messages = _M()

    pool = (_n_anchors(10, prefix="Action")
            + _anchors(["Leafy UI copy sentence"], source="i18n"))
    devs = [_feat("accounts", ["app/accounts/a.ts"])]
    ufs = [_uf("UF-001", "Create account", ["f1"])]
    _u, _p, _m, tel = run_journey_abstraction(
        ufs, [_feat("web", ["app/a.ts"])], devs, [],
        product_anchors=pool, client=_SpyClient())
    assert tel["aligned"] is True
    joined = "\n".join(seen_prompts)
    assert "Action 0" in joined
    assert "Leafy UI copy sentence" not in joined


# ── 3. gate-refused == align-OFF: byte-identical output + cache keys ────────


def _run_pair(monkeypatch, devs, pfs, ufs):
    """Run once align-OFF and once align-ON-but-gate-refused; same fixtures."""
    sparse = _n_anchors(3)  # 3 tier1 < floor 8 → gate refuses

    monkeypatch.delenv(ALIGN_ENV, raising=False)
    cache_off = _RecCache()
    off = run_journey_abstraction(
        ufs, pfs, devs, [], product_anchors=sparse, raw_anchors=sparse,
        client=_client(_ABS, _MAP), cache=cache_off)

    monkeypatch.setenv(ALIGN_ENV, "1")
    cache_on = _RecCache()
    on = run_journey_abstraction(
        ufs, pfs, devs, [], product_anchors=sparse, raw_anchors=sparse,
        client=_client(_ABS, _MAP), cache=cache_on)
    return off, cache_off, on, cache_on


def test_gate_refused_output_and_cache_keys_identical_to_align_off(monkeypatch) -> None:
    devs = [_feat("accounts", ["app/accounts/a.ts", "app/accounts/b.ts"]),
            _feat("auth", ["app/auth/login.ts"]),
            _feat("shared-ui", ["packages/ui/button.tsx"])]
    pfs = [_feat("web", ["app/accounts/a.ts", "app/auth/login.ts"])]
    ufs = [_uf("UF-001", "Create account", ["f1"]),
           _uf("UF-002", "Update account", ["f2"]),
           _uf("UF-003", "Sign in", ["f3"])]
    (u_off, p_off, m_off, t_off), c_off, (u_on, p_on, m_on, t_on), c_on = _run_pair(
        monkeypatch, devs, pfs, ufs)

    # identical cache traffic — SAME keys (anchor_sig empty on both paths)
    assert c_off.ops == c_on.ops
    assert len(c_off.ops) >= 1
    # identical rewritten arrays
    assert [u.model_dump() for u in u_off] == [u.model_dump() for u in u_on]
    assert [(p.name, p.display_name, sorted(p.paths)) for p in p_off] == \
           [(p.name, p.display_name, sorted(p.paths)) for p in p_on]
    assert m_off == m_on
    assert t_off["aligned"] is False and t_on["aligned"] is False
    # the ONLY telemetry difference is the requested-path extras
    extras = {"align_decision", "degradations"}
    assert {k: v for k, v in t_on.items() if k not in extras} == \
           {k: v for k, v in t_off.items() if k not in extras}


# ── 4. telemetry + degradation emit ──────────────────────────────────────────


def test_align_decision_absent_when_env_off(monkeypatch) -> None:
    monkeypatch.delenv(ALIGN_ENV, raising=False)
    devs = [_feat("accounts", ["app/accounts/a.ts"])]
    _u, _p, _m, tel = run_journey_abstraction(
        [_uf("UF-001", "Create account", ["f1"])], [_feat("web", ["app/a.ts"])],
        devs, [], product_anchors=_n_anchors(50), client=_client(_ABS, _MAP))
    assert "align_decision" not in tel
    assert "degradations" not in tel


def test_align_decision_and_degradation_on_requested_but_refused(monkeypatch) -> None:
    monkeypatch.setenv(ALIGN_ENV, "1")
    devs = [_feat("accounts", ["app/accounts/a.ts"])]
    ufs = [_uf("UF-001", "Create account", ["f1"]),
           _uf("UF-002", "Update account", ["f2"])]
    pool = (_n_anchors(4) + _n_anchors(37, source="i18n", prefix="Leaf"))
    _u, _p, _m, tel = run_journey_abstraction(
        ufs, [_feat("web", ["app/a.ts"])], devs, [],
        product_anchors=pool, client=_client(_ABS, _MAP))
    assert tel["aligned"] is False
    assert tel["align_decision"] == {
        "requested": True, "granted": False,
        "tier1_count": 4, "tier2_count": 37,
        "candidate_ufs": 2, "candidate_journeys": 1,
    }
    (rec,) = tel["degradations"]
    assert rec["type"] == "align_gate_refused"
    assert rec["stage"] == "stage_6_7d_journey_abstraction"
    assert rec["severity"] == "degraded"
    assert rec["metrics"] == {
        "tier1_count": 4, "tier2_count": 37, "candidate_ufs": 2,
        "candidate_journeys": 1, "floor": 8,
    }
    # free-gen still applied (never-worse) — refusal is not a failure
    assert tel["applied"] is True


def test_align_decision_on_granted(monkeypatch) -> None:
    monkeypatch.setenv(ALIGN_ENV, "1")
    devs = [_feat("accounts", ["app/accounts/a.ts"])]
    ufs = [_uf("UF-001", "Create account", ["f1"])]
    _u, _p, _m, tel = run_journey_abstraction(
        ufs, [_feat("web", ["app/a.ts"])], devs, [],
        product_anchors=_n_anchors(12), client=_client(_ABS, _MAP))
    assert tel["aligned"] is True
    assert tel["align_decision"] == {
        "requested": True, "granted": True,
        "tier1_count": 12, "tier2_count": 0,
        "candidate_ufs": 1, "candidate_journeys": 1,
    }
    assert "degradations" not in tel


def test_granted_gate_but_empty_tier1_pool_refuses(monkeypatch) -> None:
    """Guard: raw grants but the curated pool holds no tier-1 text → refuse
    (nothing to align to) and record the decision."""
    monkeypatch.setenv(ALIGN_ENV, "1")
    devs = [_feat("accounts", ["app/accounts/a.ts"])]
    ufs = [_uf("UF-001", "Create account", ["f1"])]
    _u, _p, _m, tel = run_journey_abstraction(
        ufs, [_feat("web", ["app/a.ts"])], devs, [],
        product_anchors=_anchors(["Leafy copy"], source="i18n"),
        raw_anchors=_n_anchors(40), client=_client(_ABS, _MAP))
    assert tel["aligned"] is False
    assert tel["align_decision"]["granted"] is False
    assert tel["degradations"][0]["type"] == "align_gate_refused"


def test_degradations_builder_shape() -> None:
    from faultline.pipeline_v2.degradations import align_gate_refused
    rec = align_gate_refused(tier1_count=11, tier2_count=300,
                             candidate_ufs=153, candidate_journeys=103, floor=8)
    assert rec["type"] == "align_gate_refused"
    assert rec["severity"] == "degraded"
    assert "11" in rec["detail"] and "103" in rec["detail"]
    assert rec["metrics"]["floor"] == 8
    assert rec["metrics"]["candidate_journeys"] == 103


# ── 6. contract × align interaction (anchor-echo contract, 2026-07-04) ──────
#
# The count-contract (grain-contract gate) cannot see ALIGN's catastrophic
# draw class: the supabase 2026-07-03 echo draw emitted 10 coarse nav labels
# (10 << 0.9 x 120 digest UFs) yet "Dashboard" swallowed 322/470 member flows
# → F1 18.2. The echo contract rejects such a draw, retries with the
# align-specific corrective, and — if the retry STILL echoes — bounds the
# result at ONE free-gen draw (align must never end below free-gen).

from faultline.pipeline_v2 import stage_6_7d_llm_journey_abstraction as _mod67d
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    CONTRACT_ANCHOR_ECHO,
    CONTRACT_ECHO_FREEGEN,
    CONTRACT_PASS,
    CONTRACT_PASS_AFTER_RETRY,
    DEFAULT_ABSTRACTION_MODEL,
    _align_echo_stats,
)


def _seq_client(abstraction_payloads: list[str], reattrib: str) -> Any:
    """Fake client returning abstraction_payloads[i] on the i-th Call-1 draw
    (last repeats); records system + user prompts of every Call-1."""
    state: dict[str, Any] = {"i": 0, "systems": [], "users": []}

    class _C:
        class _M:
            def create(self, **kw: Any) -> Any:
                sysp = kw.get("system", "")
                if "assign each developer feature" in sysp:
                    return _Msg(reattrib)
                state["systems"].append(sysp)
                state["users"].append(kw["messages"][0]["content"])
                i = min(state["i"], len(abstraction_payloads) - 1)
                state["i"] += 1
                return _Msg(abstraction_payloads[i])
        messages = _M()

    c = _C()
    c.state = state  # type: ignore[attr-defined]
    return c


def _four_ufs():
    """4 UFs, 4 DISTINCT resources (count-contract disarmed → echo isolated),
    3 member flows each (balanced, so only a blob draw trips the prong)."""
    return [
        _uf("UF-001", "Create thing", ["f1", "f2", "f3"], resource="alpha"),
        _uf("UF-002", "Update widget", ["f4", "f5", "f6"], resource="beta"),
        _uf("UF-003", "Delete gadget", ["f7", "f8", "f9"], resource="gamma"),
        _uf("UF-004", "View report", ["f10", "f11", "f12"], resource="delta"),
    ]


# Echo shape: one journey inherits 9/12 members (share 0.75 > 0.5).
_ECHO = json.dumps({
    "product_features": [{"name": "Dashboard", "description": "nav echo"}],
    "user_flows": [
        {"name": "Dashboard", "resource": "dashboard",
         "product_feature": "Dashboard",
         "from_flows": ["UF-001", "UF-002", "UF-003"]},
        {"name": "Subpage", "resource": "subpage",
         "product_feature": "Dashboard", "from_flows": ["UF-004"]},
    ],
})

# Healthy shape: 6/12 + 6/12 (top share 0.5, NOT > 0.5), full coverage.
_BALANCED = json.dumps({
    "product_features": [
        {"name": "Alpha Suite", "description": "a"},
        {"name": "Delta Suite", "description": "d"},
    ],
    "user_flows": [
        {"name": "Manage alpha things", "resource": "alpha",
         "product_feature": "Alpha Suite", "from_flows": ["UF-001", "UF-002"]},
        {"name": "Review delta reports", "resource": "delta",
         "product_feature": "Delta Suite", "from_flows": ["UF-003", "UF-004"]},
    ],
})

_DEVS_MAP = json.dumps({"map": {
    "accounts": "Alpha Suite", "auth": "Delta Suite",
}})


def _run_aligned(payloads: list[str], monkeypatch, **kw: Any):
    monkeypatch.setenv(ALIGN_ENV, "1")
    cli = _seq_client(payloads, _DEVS_MAP)
    devs = [_feat("accounts", ["app/accounts/a.ts"]),
            _feat("auth", ["app/auth/l.ts"])]
    ufs, pfs, dm, tel = run_journey_abstraction(
        _four_ufs(), [_feat("web", ["app/a.ts"])], devs, [],
        product_anchors=_n_anchors(8), client=cli, **kw)
    return ufs, pfs, tel, cli


def test_align_echo_stats_blob_prong() -> None:
    """The recorded catastrophe shape (one journey swallowing the majority of
    inherited members) violates; a balanced draw does not."""
    specs = json.loads(_ECHO)["user_flows"]
    violated, top, cov = _align_echo_stats(specs, _four_ufs(), 4)
    assert violated is True
    assert top == 0.75
    assert cov == 1.0
    specs_ok = json.loads(_BALANCED)["user_flows"]
    violated, top, cov = _align_echo_stats(specs_ok, _four_ufs(), 4)
    assert violated is False
    assert top == 0.5


def test_align_echo_stats_citation_prong() -> None:
    """Anchor labels emitted with near-zero grouping (the task-described echo
    shape): distinct citations under 20% of the digest → violated."""
    specs = [{"name": "Dashboard", "from_flows": []},
             {"name": "Projects", "from_flows": []}]
    violated, top, cov = _align_echo_stats(specs, _four_ufs(), 4)
    assert violated is True
    assert cov == 0.0
    # ...but with no digest UFs at all there is nothing to group → clean.
    violated, _t, cov = _align_echo_stats(specs, [], 0)
    assert violated is False
    assert cov == 1.0


def test_align_echo_stats_single_journey_skips_blob_prong() -> None:
    """A single-journey draw must not self-reject on the blob prong (its top
    share is trivially 1.0); only the citation prong may fire."""
    specs = [{"name": "Everything", "from_flows": ["UF-001", "UF-002",
                                                   "UF-003", "UF-004"]}]
    violated, top, cov = _align_echo_stats(specs, _four_ufs(), 4)
    assert violated is False
    assert top == 1.0 and cov == 1.0


def test_align_echo_draw_retried_with_echo_corrective(monkeypatch) -> None:
    """Echo first draw → ONE retry carrying the ALIGN echo corrective (not the
    merge corrective); healthy retry ships as pass_after_retry."""
    ufs, pfs, tel, cli = _run_aligned([_ECHO, _BALANCED], monkeypatch)
    assert tel["aligned"] is True
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_PASS_AFTER_RETRY
    assert tel["abstraction_retried"] is True
    assert {u.name for u in ufs} == {"Manage alpha things",
                                     "Review delta reports"}
    systems = cli.state["systems"]
    assert len(systems) == 2
    assert "echoed a few coarse anchor" in systems[1]
    assert "product_features list" not in systems[1]  # merge corrective absent
    # telemetry carries the prong values of the last evaluated draw
    assert tel["align_top_member_share"] == 0.5
    assert tel["align_citation_coverage"] == 1.0


def test_align_double_echo_falls_back_to_freegen(monkeypatch) -> None:
    """Retry STILL echoes → ONE free-gen draw (plain abstraction system, no
    anchor block) bounds the result at the validated free-gen mode."""
    ufs, pfs, tel, cli = _run_aligned([_ECHO, _ECHO, _BALANCED], monkeypatch)
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_ECHO_FREEGEN
    assert tel["abstraction_retried_freegen"] is True
    assert {u.name for u in ufs} == {"Manage alpha things",
                                     "Review delta reports"}
    systems = cli.state["systems"]
    assert len(systems) == 3
    assert "Your job is ALIGNMENT" in systems[0]
    assert "Your job is ALIGNMENT" in systems[1]
    assert "Your job is ALIGNMENT" not in systems[2]   # free-gen system
    assert "product_capability_anchors" not in cli.state["users"][2]
    assert tel["llm_calls"] == 4  # draw + retry + free-gen + reattrib


def test_align_double_echo_fallback_unparseable_keeps_retry(monkeypatch) -> None:
    """Free-gen fallback unusable → the align retry stands, flagged
    anchor_echo (never-worse: a bad fallback must not degrade the stage)."""
    ufs, pfs, tel, cli = _run_aligned(
        [_ECHO, _ECHO, "garbage not json"], monkeypatch)
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_ANCHOR_ECHO
    assert tel["align_freegen_fallback_failed"] == "abstraction_parse_failed"
    assert {u.name for u in ufs} == {"Dashboard", "Subpage"}  # retry kept


def test_align_freegen_fallback_skipped_when_cost_capped(monkeypatch) -> None:
    """Structural cost guard: cap admits the retry (2 draws) but not a third
    same-shape call → fallback skipped, flagged anchor_echo."""
    from faultline.llm.cost import estimate_call_cost
    one_call = estimate_call_cost(DEFAULT_ABSTRACTION_MODEL, 400, 200)
    monkeypatch.setattr(_mod67d, "COST_CAP_USD", one_call * 2.5)
    ufs, pfs, tel, cli = _run_aligned([_ECHO, _ECHO, _BALANCED], monkeypatch)
    assert tel["abstraction_contract"] == CONTRACT_ANCHOR_ECHO
    assert tel["align_freegen_fallback_skipped_cost"] is True
    assert len(cli.state["systems"]) == 2               # no third draw issued
    assert {u.name for u in ufs} == {"Dashboard", "Subpage"}


def test_echo_contract_never_arms_in_freegen(monkeypatch) -> None:
    """Align OFF: an echo-shaped draw sails through (the echo contract is
    ALIGN-only) and no align prong telemetry is emitted."""
    monkeypatch.delenv(ALIGN_ENV, raising=False)
    cli = _seq_client([_ECHO], _DEVS_MAP)
    devs = [_feat("accounts", ["app/accounts/a.ts"]),
            _feat("auth", ["app/auth/l.ts"])]
    ufs, pfs, dm, tel = run_journey_abstraction(
        _four_ufs(), [_feat("web", ["app/a.ts"])], devs, [], client=cli)
    assert tel["aligned"] is False
    assert tel["abstraction_contract"] == CONTRACT_PASS
    assert "align_top_member_share" not in tel
    assert len(cli.state["systems"]) == 1               # no retry
