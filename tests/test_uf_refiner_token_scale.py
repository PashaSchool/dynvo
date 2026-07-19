"""S2 Seg B' — uf_refiner per-UF output-token budget
(``FAULTLINE_UF_REFINE_TOKEN_SCALE``).

Root cause (forensics on Soc0 healthy run ``20260717T132518Z-9b994a79``, live
key): the fixed ``DEFAULT_MAX_TOKENS = 1500`` ceiling truncates a large
domain's structured JSON response mid-object → ``json_parse_failed`` → the
WHOLE domain degrades to deterministic names — deterministically, every keyed
run. The 3 degraded domains were EXACTLY the 3 largest (network 26 / service
18 / detector 17 UFs); the next-largest (admin, 11) refined cleanly. Measured
per-row output over 6,189 multi-row cached uf-refine responses: median ~74,
p90 ~106, max ~235 tokens/row — at p90 only ~14 rows fit 1500 tokens.

The fix: scale ``max_tokens`` by a structural per-UF allowance
(``TOKENS_PER_UF = 300``, covering the measured max), floored at the legacy
DEFAULT, ceilinged at 8192 (far below the model's 64k output cap and under
the non-streaming timeout line). OFF/unset → DEFAULT for every domain AND the
legacy cache key → byte-identical.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from faultline.models.types import Flow, UserFlow
from faultline.pipeline_v2.stage_6_7b_uf_refiner import (
    DEFAULT_MAX_TOKENS,
    MAX_OUTPUT_TOKENS_CEILING,
    TOKENS_PER_UF,
    UF_REFINE_TOKEN_SCALE_ENV,
    _effective_max_tokens,
    _parse_refinement,
    _refine_cache_key,
    refine_user_flows,
)


# ── budget arithmetic ───────────────────────────────────────────────────────


def test_unset_budget_scales_like_explicit_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SEMANTIC flip migration (2026-07-19 S*-pack, KEY_SCHEMA 32): unset now
    # arms the per-UF budget (unset ≡ explicit-1); the fixed-1500 world is
    # the explicit kill-switch below.
    monkeypatch.delenv(UF_REFINE_TOKEN_SCALE_ENV, raising=False)
    for n in (1, 5, 11, 17, 18, 26, 100):
        assert _effective_max_tokens(n) == min(
            max(n * TOKENS_PER_UF, DEFAULT_MAX_TOKENS),
            MAX_OUTPUT_TOKENS_CEILING,
        )


def test_explicit_zero_budget_is_default_1500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pre-fix fixed ceiling stays reachable via the kill-switch forever.
    monkeypatch.setenv(UF_REFINE_TOKEN_SCALE_ENV, "0")
    for n in (1, 5, 11, 17, 18, 26, 100):
        assert _effective_max_tokens(n) == DEFAULT_MAX_TOKENS


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_off_values_are_kill_switch(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    monkeypatch.setenv(UF_REFINE_TOKEN_SCALE_ENV, val)
    assert _effective_max_tokens(26) == DEFAULT_MAX_TOKENS


def test_on_scales_the_soc0_degraded_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three Soc0 exhibits (network 26 / service 18 / detector 17 UFs)
    get budgets that fit their measured worst-case output."""
    monkeypatch.setenv(UF_REFINE_TOKEN_SCALE_ENV, "1")
    assert _effective_max_tokens(26) == 26 * TOKENS_PER_UF  # network → 7800
    assert _effective_max_tokens(18) == 18 * TOKENS_PER_UF  # service → 5400
    assert _effective_max_tokens(17) == 17 * TOKENS_PER_UF  # detector → 5100
    # All above the old cliff and under the ceiling:
    for n in (17, 18, 26):
        assert DEFAULT_MAX_TOKENS < _effective_max_tokens(n) <= MAX_OUTPUT_TOKENS_CEILING


def test_on_small_domain_keeps_the_default_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small domain (scaled value under the floor) keeps DEFAULT — its
    request AND cache key stay byte-identical even when the flag is ON."""
    monkeypatch.setenv(UF_REFINE_TOKEN_SCALE_ENV, "1")
    assert _effective_max_tokens(1) == DEFAULT_MAX_TOKENS
    assert _effective_max_tokens(5) == DEFAULT_MAX_TOKENS
    # boundary: 5 * 300 = 1500 == DEFAULT; 6 * 300 = 1800 > DEFAULT
    assert _effective_max_tokens(6) == 6 * TOKENS_PER_UF


def test_on_ceiling_caps_below_nonstreaming_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UF_REFINE_TOKEN_SCALE_ENV, "1")
    assert _effective_max_tokens(100) == MAX_OUTPUT_TOKENS_CEILING
    assert MAX_OUTPUT_TOKENS_CEILING <= 8192


def test_token_scale_flag_registered_in_env_output_flags() -> None:
    """Cache-key correctness: the flag reshapes user_flows[] → must be keyed
    (append-only, no KEY_SCHEMA bump — reconciled at merge)."""
    from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS
    assert "FAULTLINE_UF_REFINE_TOKEN_SCALE" in ENV_OUTPUT_FLAGS


# ── cache-key identity (OFF byte-ident; ON keys diverge only when scaled) ───


def test_cache_key_identical_at_default_budget() -> None:
    """The 3-arg key at DEFAULT == the legacy 2-arg key: every OFF-flag and
    small-domain key is byte-identical to pre-fix, so existing llm-cache
    entries still hit."""
    legacy = _refine_cache_key("m", "prompt")
    assert _refine_cache_key("m", "prompt", DEFAULT_MAX_TOKENS) == legacy


def test_cache_key_differs_for_scaled_budget() -> None:
    """A scaled budget can turn a truncated (uncached) response into a
    parseable one — a different answer that must key separately."""
    legacy = _refine_cache_key("m", "prompt")
    scaled = _refine_cache_key("m", "prompt", 26 * TOKENS_PER_UF)
    assert scaled != legacy
    assert _refine_cache_key("m", "prompt", 5100) != _refine_cache_key(
        "m", "prompt", 5400,
    )


# ── live-call threading (the request actually carries the budget) ───────────


def _flow(name: str) -> Flow:
    return Flow(
        name=name,
        uuid=name,
        paths=["backend/routers/network.py"],
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=100.0,
        test_files=[],
        participants=[],
    )


def _uf(uf_id: str, member: str) -> UserFlow:
    return UserFlow(
        id=uf_id,
        name=f"Manage {uf_id}",
        domain="network",
        product_feature_id="network",
        intent="manage",
        resource="network",
        member_flow_ids=[member],
        member_count=1,
        routes=[],
    )


def _big_domain(n: int) -> tuple[list[UserFlow], list[Flow]]:
    """One domain with ``n`` UFs (the Soc0 network-domain shape)."""
    flows = [_flow(f"net-flow-{i}") for i in range(n)]
    ufs = [_uf(f"UF-{i:03d}", f"net-flow-{i}") for i in range(n)]
    return ufs, flows


def _capturing_client(captured: list[dict[str, Any]], response: str) -> Any:
    class _Usage:
        input_tokens = 400
        output_tokens = 150

    class _Block:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Msg:
        def __init__(self) -> None:
            self.content = [_Block(response)]
            self.usage = _Usage()

    class _Client:
        class _Messages:
            def create(self, **kw: Any) -> Any:
                captured.append(kw)
                return _Msg()

        messages = _Messages()

    return _Client()


def _response_for(n: int, *, verbose: bool = False) -> str:
    """A structured response with ``n`` rows. ``verbose=True`` mirrors the
    measured p90 row verbosity (~422 chars/row: acceptance drafts + fuller
    descriptions — the shape the degraded Soc0 domains actually produced)."""
    rows = []
    for i in range(n):
        row: dict[str, Any] = {
            "id": f"UF-{i:03d}",
            "name": f"Manage network zone {i}",
            "description": "User manages a network zone.",
            "intent": "manage",
            "ui_tier": "full-page",
            "acceptance": [],
        }
        if verbose:
            row["description"] = (
                "User reviews the network zone configuration, adjusts the "
                "monitored ranges, and saves the updated detection policy."
            )
            row["acceptance"] = [
                f"User can open network zone {i} and see its current "
                "monitored ranges listed",
                f"User can edit the detection policy of zone {i} and the "
                "change is persisted and visible on reload",
            ]
        rows.append(row)
    return json.dumps({"user_flows": rows})


def test_on_large_domain_call_carries_scaled_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UF_REFINE_TOKEN_SCALE_ENV, "1")
    captured: list[dict[str, Any]] = []
    ufs, flows = _big_domain(17)
    out, tel = refine_user_flows(
        ufs, flows, client=_capturing_client(captured, _response_for(17)),
    )
    assert tel["domains_degraded"] == 0
    assert len(captured) >= 1
    assert captured[0]["max_tokens"] == 17 * TOKENS_PER_UF


def test_off_large_domain_call_carries_default_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MECHANICAL flip migration (flip32): OFF world = explicit kill-switch.
    monkeypatch.setenv(UF_REFINE_TOKEN_SCALE_ENV, "0")
    captured: list[dict[str, Any]] = []
    ufs, flows = _big_domain(17)
    refine_user_flows(
        ufs, flows, client=_capturing_client(captured, _response_for(17)),
    )
    assert len(captured) >= 1
    assert captured[0]["max_tokens"] == DEFAULT_MAX_TOKENS


# ── the truncation mechanism itself ($0 replay of the failure shape) ────────


def test_truncated_large_domain_response_fails_parse_but_full_parses() -> None:
    """Mechanism replay at $0: a 26-row response (the Soc0 network domain)
    cut at the 1500-token boundary (~4 chars/token) is unparseable —
    reproducing ``json_parse_failed`` — while the SAME response, untruncated
    (what the scaled budget allows), parses all 26 rows.

    The failed live responses were never cached (failures are never stored),
    so the mechanism is reproduced synthetically; the size arithmetic mirrors
    the measured cache distribution (see module docstring).
    """
    full = _response_for(26, verbose=True)
    assert len(full) > DEFAULT_MAX_TOKENS * 4  # the domain CANNOT fit 1500 tok
    truncated = full[: DEFAULT_MAX_TOKENS * 4]
    assert _parse_refinement(truncated) is None  # json_parse_failed shape
    parsed = _parse_refinement(full)
    assert parsed is not None and len(parsed) == 26
    # And the scaled budget actually covers it: 26 * 300 tok * ~4 ch/tok.
    assert len(full) <= 26 * TOKENS_PER_UF * 4


def test_degrade_path_unchanged_when_scaled_response_still_truncates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A domain that STILL truncates past the scaled budget degrades exactly
    as before (honest json_parse_failed; never a crash, never cached)."""
    monkeypatch.setenv(UF_REFINE_TOKEN_SCALE_ENV, "1")
    captured: list[dict[str, Any]] = []
    ufs, flows = _big_domain(3)
    bad = _response_for(3)[:80]  # mid-object cut
    out, tel = refine_user_flows(
        ufs, flows, client=_capturing_client(captured, bad),
    )
    assert tel["domains_degraded"] == 1
    assert all(uf.refined is False for uf in out)
