"""Stage 6.7d draw-2 pick-best (MISSION-92 lever #4) — unit tests.

The selector was CALIBRATED on 16 recorded Sonnet draws with known uf_score F1
(2026-07-03/04 corpus sessions; architect-state-layer1-67d-draw2-20260704):
10/10 same-repo pairs with |dF1| >= 1 ranked correctly. The calibration
feature vectors are embedded here as regression fixtures so any change to
:func:`_draw_rank` re-proves itself against the recorded evidence.

Default OFF (FAULTLINE_STAGE_6_7D_DRAW2 unset): single-draw behaviour, cache
keys and telemetry byte-identical to the pre-draw-2 engine.
"""

from __future__ import annotations

import json
from typing import Any

from faultline.pipeline_v2 import stage_6_7d_llm_journey_abstraction as _mod
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    CONTRACT_PASS,
    CONTRACT_PASS_AFTER_RETRY,
    DEFAULT_ABSTRACTION_MODEL,
    DRAW2_ENV,
    _cache_key,
    _draw_rank,
    draw2_enabled,
    run_journey_abstraction,
)

# Reuse the canonical fixtures + fakes of the main 6.7d test module.
from tests.test_stage_6_7d_llm_journey_abstraction import (
    _ABS,
    _FakeCache,
    _MAP_FULL,
    _UNCOMPRESSED,
    _client,
    _devs,
    _pfs,
    _seq_client,
    _ufs,
)


# ── Selector unit tests on the recorded calibration fixtures ────────────────

def _vec(built_n: int, single_tok: float, zero: float, tie_rank: int = 1) -> dict[str, Any]:
    return {"built_n": built_n, "single_tok_frac": single_tok,
            "zero_member_frac": zero, "tie_rank": tie_rank}


def test_selector_ranks_recorded_supabase_echo_pair() -> None:
    """supabase align draws Jul-4: F1 51.7 (105 UFs, healthy) vs F1 21.1
    (26 UFs, 57.7% single-token widget-label names) — the echo prong."""
    healthy = _vec(105, 0.0, 0.0)
    echo = _vec(26, 0.577, 0.0)
    assert _draw_rank(healthy) < _draw_rank(echo)


def test_selector_ranks_recorded_inbox_zero_pair() -> None:
    """inbox-zero contract draws: F1 46.0 (zero_frac .038) vs 42.7 (.052) —
    the route-rescue incompleteness prong."""
    better = _vec(79, 0.0, 0.038)
    worse = _vec(96, 0.0, 0.052)
    assert _draw_rank(better) < _draw_rank(worse)


def test_selector_ranks_recorded_formbricks_upside_pair() -> None:
    """formbricks: baseline draw F1 77.3 (zero_frac .030) vs fresh draw 83.8
    (.000) — the +6.5 upside the lottery leaves on the table."""
    baseline = _vec(66, 0.0, 0.030)
    fresh = _vec(64, 0.0, 0.0)
    assert _draw_rank(fresh) < _draw_rank(baseline)


def test_selector_ranks_recorded_documenso_pair() -> None:
    """documenso: F1 64.2 (zero_frac .127) vs 72.7 (.065)."""
    worse = _vec(79, 0.0, 0.127)
    better = _vec(62, 0.0, 0.065)
    assert _draw_rank(better) < _draw_rank(worse)


def test_selector_grounds_to_nothing_loses() -> None:
    """cal-com align pair analogue: a draw that reconstructs to nothing (the
    parse-fail/passthrough class, F1 63.1) loses to any grounded draw."""
    grounded = _vec(156, 0.019, 0.0)
    nothing = _vec(0, 0.0, 0.0)
    assert _draw_rank(grounded) < _draw_rank(nothing)


def test_selector_tie_keeps_incumbent() -> None:
    """Selector-indifference must be a no-op: on a full tie the incumbent
    (tie_rank 0 — what the single-draw engine would have kept) wins."""
    incumbent = _vec(80, 0.0, 0.0, tie_rank=0)
    challenger = _vec(75, 0.0, 0.0, tie_rank=2)
    assert _draw_rank(incumbent) < _draw_rank(challenger)


# ── Env flag + cache-key isolation ──────────────────────────────────────────

def test_flag_default_off(monkeypatch: Any) -> None:
    monkeypatch.delenv(DRAW2_ENV, raising=False)
    assert draw2_enabled() is False
    monkeypatch.setenv(DRAW2_ENV, "0")
    assert draw2_enabled() is False
    monkeypatch.setenv(DRAW2_ENV, "false")
    assert draw2_enabled() is False
    monkeypatch.setenv(DRAW2_ENV, "1")
    assert draw2_enabled() is True


def test_cache_key_variant_isolated_and_default_unchanged() -> None:
    """variant='' must hash the EXACT legacy payload (no version bump, no
    invalidation of existing caches); any non-empty variant namespaces away."""
    digest = {"n_dev_features": 1, "developer_features": [],
              "current_product_features": [], "current_user_flows": [], "routes": []}
    legacy = _cache_key(digest, "m1", "m2", "")
    assert legacy == _cache_key(digest, "m1", "m2", "", "")
    assert legacy != _cache_key(digest, "m1", "m2", "", "draw2")
    assert (_cache_key(digest, "m1", "m2", "", "draw2")
            != _cache_key(digest, "m1", "m2", "", "draw2-cand-0"))


# ── Default-OFF byte-identity ───────────────────────────────────────────────

def test_default_off_single_draw_no_draw2_telemetry(monkeypatch: Any) -> None:
    monkeypatch.delenv(DRAW2_ENV, raising=False)
    cli = _seq_client([_ABS], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=cli)
    assert tel["applied"] is True
    assert len(cli.state["systems"]) == 1          # exactly one Call-1 draw
    for k in ("draws_sampled", "selector_winner", "draw2_candidates",
              "draw2_enabled", "draw2_skipped_cost"):
        assert k not in tel


# ── Draw-2 flow with a fake client ──────────────────────────────────────────

# First draw: 2 journeys, ONE of them cites nothing and only route-rescues
# onto /api/webhooks → a zero-member journey (zero_member_frac 0.5).
_ABS_RESCUE = json.dumps({
    "product_features": [
        {"name": "Account Management", "description": "manage accounts"},
        {"name": "Webhooks", "description": "receive webhooks"},
    ],
    "user_flows": [
        {"name": "Manage accounts", "resource": "account",
         "product_feature": "Account Management", "from_flows": ["UF-001", "UF-002"]},
        {"name": "Receive webhooks", "resource": "webhook",
         "product_feature": "Webhooks", "from_flows": []},
    ],
})
_ROUTES = [{"pattern": "/api/webhooks", "method": "POST", "trigger": "webhook"}]

# Echo-shaped draw: grounded (each cites a from_flow) but majority
# single-token label names — the supabase 21.1 catastrophe shape.
_ABS_ECHO = json.dumps({
    "product_features": [{"name": "Widgets", "description": "ui"}],
    "user_flows": [
        {"name": "Tooltip", "resource": "tooltip",
         "product_feature": "Widgets", "from_flows": ["UF-001"]},
        {"name": "Tabs", "resource": "tabs",
         "product_feature": "Widgets", "from_flows": ["UF-002"]},
        {"name": "Popover", "resource": "popover",
         "product_feature": "Widgets", "from_flows": ["UF-003"]},
    ],
})


def test_draw2_samples_extra_and_selector_picks_cleaner_draw(monkeypatch: Any) -> None:
    """Flag ON + clean first draw → ONE extra draw is sampled and the winner
    is the draw with the lower zero-member share (calibration prong 3)."""
    monkeypatch.setenv(DRAW2_ENV, "1")
    cli = _seq_client([_ABS_RESCUE, _ABS], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), _ROUTES, client=cli)
    assert tel["applied"] is True
    assert len(cli.state["systems"]) == 2
    assert tel["draws_sampled"] == 2
    assert tel["selector_winner"] == "extra"
    assert tel["draw2_candidates"][0]["zero_member_frac"] == 0.5
    assert tel["draw2_candidates"][1]["zero_member_frac"] == 0.0
    assert {u.name for u in ufs} == {"Manage accounts", "Sign in"}


def test_draw2_echo_shaped_retry_candidate_loses(monkeypatch: Any) -> None:
    """Contract retry counts as the second candidate (no third draw), and an
    echo-shaped draw loses to a grounded multi-word one even at equal
    zero-member share. Here the ECHO draw trips the grain gate (3 emitted vs
    3 digest UFs) → retry; the selector then prefers the retry."""
    monkeypatch.setenv(DRAW2_ENV, "1")
    cli = _seq_client([_ABS_ECHO, _ABS], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=cli)
    assert tel["applied"] is True
    assert len(cli.state["systems"]) == 2          # first + retry, NO extra
    assert tel["draws_sampled"] == 2
    assert tel["selector_winner"] == "retry"
    assert tel["draw2_candidates"][0]["single_tok_frac"] == 1.0
    assert {u.name for u in ufs} == {"Manage accounts", "Sign in"}
    assert tel["abstraction_contract"] == CONTRACT_PASS_AFTER_RETRY


def test_draw2_composes_with_contract_retry_tie_keeps_retry(monkeypatch: Any) -> None:
    """Uncompressed first draw → contract retry → both fully grounded (all
    prongs tie) → the INCUMBENT (retry, per the grain-contract keep-the-retry
    rule) wins, preserving single-draw semantics on selector-indifference."""
    monkeypatch.setenv(DRAW2_ENV, "1")
    cli = _seq_client([_UNCOMPRESSED, _ABS], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=cli)
    assert tel["applied"] is True
    assert len(cli.state["systems"]) == 2
    assert tel["selector_winner"] == "retry"
    assert tel["abstraction_contract"] == CONTRACT_PASS_AFTER_RETRY
    assert {u.name for u in ufs} == {"Manage accounts", "Sign in"}


def test_draw2_parse_failed_first_draw_rescued_by_extra(monkeypatch: Any) -> None:
    """The cal-com 63.1 degrade class: with the flag ON a parse-failed first
    draw is no longer terminal — the extra draw carries the stage."""
    monkeypatch.setenv(DRAW2_ENV, "1")
    cli = _seq_client(["not json at all", _ABS], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=cli)
    assert tel["applied"] is True
    assert tel["abstraction_first_draw_failed"] == "abstraction_parse_failed"
    assert tel["draws_sampled"] == 1
    assert tel["selector_winner"] == "extra"
    assert tel["abstraction_contract"] == CONTRACT_PASS
    assert {u.name for u in ufs} == {"Manage accounts", "Sign in"}


def test_draw2_both_draws_unusable_degrades_never_worse(monkeypatch: Any) -> None:
    monkeypatch.setenv(DRAW2_ENV, "1")
    ufs_in = _ufs()
    cli = _seq_client(["garbage", "more garbage"], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        ufs_in, _pfs(), _devs(), [], client=cli)
    assert tel["applied"] is False
    assert tel["degraded_reason"] == "abstraction_parse_failed"
    assert ufs is ufs_in                            # identity — never-worse


def test_draw2_cost_guard_skips_extra_draw(monkeypatch: Any) -> None:
    """Same structural x2 guard as the contract retry: near the stage cost
    cap the extra draw is skipped and the first draw ships."""
    from faultline.llm.cost import estimate_call_cost
    one_call = estimate_call_cost(DEFAULT_ABSTRACTION_MODEL, 400, 200)
    monkeypatch.setattr(_mod, "COST_CAP_USD", one_call * 1.5)
    monkeypatch.setenv(DRAW2_ENV, "1")
    cli = _seq_client([_ABS], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=cli)
    assert tel["applied"] is True
    assert len(cli.state["systems"]) == 1           # extra draw NOT issued
    assert tel["draw2_skipped_cost"] is True
    assert tel["draws_sampled"] == 1
    assert tel["selector_winner"] == "first"
    assert tel["abstraction_contract"] == CONTRACT_PASS


def test_draw2_cache_namespace_isolated_from_default_path(monkeypatch: Any) -> None:
    """A draw-2 session caches its winner under the 'draw2' variant key plus
    one raw entry per candidate — and NONE of it is visible to a default-OFF
    scan (no pollution), while a second draw-2 run replays at $0."""
    monkeypatch.setenv(DRAW2_ENV, "1")
    cache = _FakeCache()
    cli = _seq_client([_ABS_RESCUE, _ABS], _MAP_FULL)
    _u1, _p1, _m1, tel1 = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), _ROUTES, client=cli, cache=cache)
    assert tel1["applied"] is True
    # main winner entry + 2 raw candidate entries
    assert len(cache.store) == 3
    kinds = {v.get("kind") for v in cache.store.values() if "draw" in v}
    assert kinds == {"first", "extra"}
    # draw-2 replay: cache hit, zero LLM calls, same names.
    _u2, _p2, _m2, tel2 = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), _ROUTES,
        client=_client(_UNCOMPRESSED, _MAP_FULL), cache=cache)
    assert tel2["cache_hit"] is True
    assert tel2["llm_calls"] == 0
    assert [u.name for u in _u2] == [u.name for u in _u1]
    # default-OFF scan on the SAME cache: no hit — it must draw live.
    monkeypatch.delenv(DRAW2_ENV)
    _u3, _p3, _m3, tel3 = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), _ROUTES,
        client=_client(_UNCOMPRESSED, _MAP_FULL), cache=cache)
    assert tel3["cache_hit"] is False
    assert tel3["llm_calls"] > 0
