"""W4.1 — 6.7d first-draw brittleness ladder (soc0f forensics).

A single empty/parse-failed Sonnet Call-1 draw used to degrade the WHOLE
journey layer (wave4 Soc0: ``abstraction_empty`` silently swapped the
scored layer for the raw 6.7 rollup). The fix ladder, cheapest first:

  1. retry ONCE, same prompt (``retry_used=1``, distinct decision-log role);
  2. interior-evidence runs only: fall back to the evidence-less v1
     prompt + cache namespace — the wave31-proven path (replays a warm v1
     cache before paying for a live draw) — ``fallback="v1_prompt"``;
  3. degrade (existing never-worse exit, reason vocabulary unchanged).

Telemetry keys are omit-when-unset so old scans stay byte-identical.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _ANCHORED_SYSTEM,
    _INTERIOR_ADDENDUM,
    run_journey_abstraction,
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── World (mirrors test_spine_w4_interior_evidence's anchored setup) ────


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
    },
}

_GOOD = (
    '{"product_features":[{"name":"Database","description":"db"}],'
    '"user_flows":[{"name":"Manage database","resource":"database",'
    '"product_feature":"Database","from_flows":["UF-001"],'
    '"from_dev_features":["database"]}]}'
)
#: parseable JSON, zero valid specs — the exact wave4 Soc0 failure class
_EMPTY = '{"user_flows": [], "product_features": []}'


def _world() -> tuple[list[UserFlow], list[Feature], list[Feature]]:
    f1 = _flow("view-backups-flow", "pages/database/backups.tsx")
    f2 = _flow("tune-pooling-flow", "pages/database/pooling.tsx")
    d = _dev("database", "database",
             ["pages/database/backups.tsx", "pages/database/pooling.tsx"],
             [f1, f2])
    pfs = [_pf("database", "Database")]
    old_uf = _uf("UF-001", "Manage database", "database", [f1.uuid, f2.uuid])
    return [old_uf], pfs, [d]


class _SeqClient:
    """create() returns ``payloads[i]`` for the i-th call (last one repeats),
    capturing every call for prompt-shape assertions."""

    def __init__(self, payloads: list[str]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._payloads = list(payloads)
        self.messages = self

    def create(self, **kw: Any) -> Any:  # noqa: ANN401
        self.calls.append(kw)
        i = min(len(self.calls) - 1, len(self._payloads) - 1)
        return SimpleNamespace(
            content=[SimpleNamespace(text=self._payloads[i])],
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )


class _FakeCache:
    """In-memory CacheBackend (mirrors test_stage_6_7b's _MemCache)."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Any] = {}

    def get(self, kind: str, key: str) -> Any:
        return self.store.get((kind, key))

    def set(self, kind: str, key: str, value: Any, *,
            ttl_seconds: Any = None) -> None:
        self.store[(kind, key)] = value

    def delete(self, kind: str, key: str) -> None:
        self.store.pop((kind, key), None)

    def load_namespace(self, kind: str) -> dict[str, Any]:
        return {}

    def flush(self) -> None:
        pass


def _digest_of(call: dict[str, Any]) -> dict[str, Any]:
    text = call["messages"][0]["content"]
    return json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])


# ── (a) first-empty → retry succeeds ─────────────────────────────────────


def test_first_empty_draw_retries_once_and_applies() -> None:
    ufs_in, pfs_in, devs = _world()
    cli = _SeqClient([_EMPTY, _GOOD])
    _u, _p, _m, tel = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], client=cli, model="m",
        anchored=True, interior_evidence=_EVIDENCE,
    )
    assert tel["applied"] is True
    assert tel["retry_used"] == 1
    assert tel["fallback"] is None
    assert tel["llm_calls"] == 2
    # Same prompt on the retry — system (incl. interior addendum) AND user.
    assert cli.calls[1]["system"] == cli.calls[0]["system"]
    assert cli.calls[0]["system"].endswith(_INTERIOR_ADDENDUM)
    assert cli.calls[1]["messages"] == cli.calls[0]["messages"]


def test_parse_failed_first_draw_also_retries() -> None:
    ufs_in, pfs_in, devs = _world()
    cli = _SeqClient(["not json at all", _GOOD])
    _u, _p, _m, tel = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], client=cli, model="m",
        anchored=True, interior_evidence=_EVIDENCE,
    )
    assert tel["applied"] is True
    assert tel["retry_used"] == 1
    assert tel["llm_calls"] == 2


# ── (b) retry also empty → v1 evidence-less fallback ────────────────────


def test_both_empty_falls_back_to_v1_prompt() -> None:
    ufs_in, pfs_in, devs = _world()
    cli = _SeqClient([_EMPTY, _EMPTY, _GOOD])
    _u, _p, _m, tel = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], client=cli, model="m",
        anchored=True, interior_evidence=_EVIDENCE,
    )
    assert tel["applied"] is True
    assert tel["retry_used"] == 1
    assert tel["fallback"] == "v1_prompt"
    assert tel["llm_calls"] == 3
    # Draw 3 is the evidence-less v1 engagement: no interior addendum,
    # no sections riders in the digest (byte-identical pre-W4 shape).
    assert cli.calls[2]["system"] == _ANCHORED_SYSTEM
    (pf_line,) = _digest_of(cli.calls[2])["current_product_features"]
    assert "sections" not in pf_line
    # ...while draws 1-2 carried the interior evidence.
    (pf_line0,) = _digest_of(cli.calls[0])["current_product_features"]
    assert pf_line0["sections"] == ["Scheduled Backups", "Connection Pooling"]


# ── (c) all three fail → degrade (existing behavior preserved) ──────────


def test_all_draws_fail_degrades_never_worse() -> None:
    ufs_in, pfs_in, devs = _world()
    cli = _SeqClient([_EMPTY])  # repeats forever
    u, p, m, tel = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], client=cli, model="m",
        anchored=True, interior_evidence=_EVIDENCE,
    )
    assert tel["applied"] is False
    assert tel["degraded_reason"] == "abstraction_empty"
    assert tel["fallback"] == "abstraction_empty"
    assert tel["retry_used"] == 1  # the ladder ran and is visible
    assert tel["llm_calls"] == 3   # draw + retry + v1 fallback
    # Never-worse identity: the ORIGINAL arrays pass through untouched.
    assert u is ufs_in and p is pfs_in and m is None


# ── (d) kill-switch / old-path untouched ─────────────────────────────────


def test_evidence_less_run_retries_but_never_minted_a_fallback() -> None:
    """interior kill-switched/absent = the run IS already the v1 path: the
    ladder retries once, then degrades — no third draw exists."""
    ufs_in, pfs_in, devs = _world()
    cli = _SeqClient([_EMPTY])
    u, p, _m, tel = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], client=cli, model="m",
        anchored=True, interior_evidence=None,
    )
    assert tel["applied"] is False
    assert tel["degraded_reason"] == "abstraction_empty"
    assert tel["retry_used"] == 1
    assert tel["llm_calls"] == 2  # NO v1 fallback rung on the v1 path
    assert u is ufs_in and p is pfs_in


def test_successful_first_draw_path_unchanged() -> None:
    ufs_in, pfs_in, devs = _world()
    cli = _SeqClient([_GOOD])
    _u, _p, _m, tel = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], client=cli, model="m",
        anchored=True, interior_evidence=_EVIDENCE,
    )
    assert tel["applied"] is True
    assert tel["llm_calls"] == 1
    assert "retry_used" not in tel
    assert tel["fallback"] is None


# ── (e) telemetry omit-when-unset ────────────────────────────────────────


def test_telemetry_omitted_when_ladder_never_ran() -> None:
    ufs_in, pfs_in, devs = _world()
    # Success path (interior on) — no ladder keys.
    cli = _SeqClient([_GOOD])
    _u, _p, _m, tel = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], client=cli, model="m",
        anchored=True, interior_evidence=_EVIDENCE,
    )
    assert "retry_used" not in tel and tel["fallback"] is None
    # Degrade BEFORE any draw (no client) — no ladder keys either.
    _u2, _p2, _m2, tel2 = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], client=None,
        _client_factory=lambda: None,
        anchored=True, interior_evidence=_EVIDENCE,
    )
    assert tel2["degraded_reason"] == "no_client"
    assert "retry_used" not in tel2 and "v1_prompt" not in str(tel2.get("fallback"))


# ── v1 cache replay: the wave31-proven path is REPLAYED, not re-bought ──


def test_fallback_replays_warm_v1_cache_and_warms_own_namespace() -> None:
    cache = _FakeCache()
    # Phase 1 — an evidence-less anchored run (the v1 path) records its
    # abstraction in the v1 namespace.
    ufs_in, pfs_in, devs = _world()
    cli_ok = _SeqClient([_GOOD])
    _u, _p, _m, tel = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], client=cli_ok, model="m",
        anchored=True, interior_evidence=None, cache=cache,
    )
    assert tel["applied"] is True
    assert len(cache.store) == 1  # the v1-namespace entry
    # Phase 2 — the interior run's draws all fail; the ladder must REPLAY
    # the v1 entry ($0) instead of degrading or re-drawing v1 live.
    ufs_in2, pfs_in2, devs2 = _world()
    cli_bad = _SeqClient([_EMPTY])
    _u2, _p2, _m2, tel2 = run_journey_abstraction(
        ufs_in2, pfs_in2, devs2, [], client=cli_bad, model="m",
        anchored=True, interior_evidence=_EVIDENCE, cache=cache,
    )
    assert tel2["applied"] is True
    assert tel2["fallback"] == "v1_prompt"
    assert tel2["retry_used"] == 1
    assert tel2["llm_calls"] == 2       # both failed draws; NO live v1 draw
    assert len(cli_bad.calls) == 2
    assert tel2["abstraction_contract"] == tel["abstraction_contract"]
    # The run warmed its OWN (v2-interior) key with the replayed payload...
    assert len(cache.store) == 2
    # Phase 3 — a re-scan replays at the PRIMARY lookup: zero LLM calls.
    ufs_in3, pfs_in3, devs3 = _world()
    cli_never = _SeqClient([_EMPTY])
    _u3, _p3, _m3, tel3 = run_journey_abstraction(
        ufs_in3, pfs_in3, devs3, [], client=cli_never, model="m",
        anchored=True, interior_evidence=_EVIDENCE, cache=cache,
    )
    assert tel3["applied"] is True
    assert tel3["cache_hit"] is True
    assert tel3["llm_calls"] == 0
    assert not cli_never.calls


def test_live_v1_fallback_result_cached_under_own_namespace() -> None:
    """A LIVE v1-fallback success is cached under the interior key, so the
    next scan replays it at the primary lookup ($0)."""
    cache = _FakeCache()
    ufs_in, pfs_in, devs = _world()
    cli = _SeqClient([_EMPTY, _EMPTY, _GOOD])
    _u, _p, _m, tel = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], client=cli, model="m",
        anchored=True, interior_evidence=_EVIDENCE, cache=cache,
    )
    assert tel["applied"] is True and tel["fallback"] == "v1_prompt"
    assert len(cache.store) == 1  # own-namespace write only (no v1 write)
    ufs_in2, pfs_in2, devs2 = _world()
    cli2 = _SeqClient([_EMPTY])
    _u2, _p2, _m2, tel2 = run_journey_abstraction(
        ufs_in2, pfs_in2, devs2, [], client=cli2, model="m",
        anchored=True, interior_evidence=_EVIDENCE, cache=cache,
    )
    assert tel2["applied"] is True and tel2["cache_hit"] is True
    assert not cli2.calls
