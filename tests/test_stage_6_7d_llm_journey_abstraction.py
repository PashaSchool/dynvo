"""Unit tests for Stage 6.7d — LLM product/journey abstraction.

The LLM is always mocked (a fake Anthropic client routed by system prompt).
Asserts: user_flows + product_features REWRITTEN at journey grain; dev→capability
re-attribution conserves files (residual catches omitted devs); member_flow_ids
inherited via from_flows; graceful degrade (originals returned byte-identical) on
no-client / bad-JSON / empty; the central flows[] graph is never read or mutated;
gate default OFF.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    DEFAULT_ABSTRACTION_MODEL,
    MAX_USER_FLOWS_DIGEST,
    is_enabled,
    resolve_abstraction_model,
    run_journey_abstraction,
)


# ── Fake Anthropic client (routed by system prompt) ─────────────────────────

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
    """create() returns the abstraction payload when the system prompt is the
    abstraction one, else the re-attribution map."""
    class _C:
        class _M:
            def create(self, **kw: Any) -> Any:
                sysp = kw.get("system", "")
                return _Msg(reattrib if "assign each developer feature" in sysp else abstraction)
        messages = _M()
    return _C()


def _raising_client() -> Any:
    class _C:
        class _M:
            def create(self, **_kw: Any) -> Any:
                raise RuntimeError("boom")
        messages = _M()
    return _C()


# ── Fixtures ────────────────────────────────────────────────────────────────

def _feat(name: str, paths: list[str], member_files: list[str] | None = None) -> Feature:
    from faultline.models.types import MemberFile
    mf = [MemberFile(path=p, role="anchor", confidence=1.0)
          for p in (member_files if member_files is not None else paths)]
    return Feature(
        name=name, display_name=name, description=f"{name} module",
        paths=paths, authors=["a"], total_commits=3, bug_fixes=1,
        bug_fix_ratio=0.33, last_modified=datetime.now(timezone.utc),
        health_score=90.0, layer="developer", member_files=mf,
    )


def _uf(uf_id: str, name: str, members: list[str]) -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, intent="author", resource="thing",
        member_flow_ids=members, member_count=len(members),
        routes=[f"/{name}"],
    )


def _devs() -> list[Feature]:
    return [
        _feat("accounts", ["app/accounts/a.ts", "app/accounts/b.ts"]),
        _feat("auth", ["app/auth/login.ts"]),
        _feat("shared-ui", ["packages/ui/button.tsx", "packages/ui/card.tsx"]),
    ]


def _pfs() -> list[Feature]:
    return [_feat("web", ["app/accounts/a.ts", "app/accounts/b.ts", "app/auth/login.ts"])]


def _ufs() -> list[UserFlow]:
    return [
        _uf("UF-001", "Create account", ["f1"]),
        _uf("UF-002", "Update account", ["f2"]),
        _uf("UF-003", "Sign in", ["f3"]),
    ]


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
_MAP_FULL = json.dumps({"map": {
    "accounts": "Account Management", "auth": "Authentication",
    "shared-ui": "Shared Platform",
}})
_MAP_OMITS_SHARED = json.dumps({"map": {
    "accounts": "Account Management", "auth": "Authentication",
}})


# ── Tests ───────────────────────────────────────────────────────────────────

def test_success_path_stagelogger_info_no_spurious_degrade() -> None:
    """Regression (audit 2026-07-01): the success-path ``log.info`` must pass a
    single pre-formatted positional arg. StageLogger.info(reason, feature=None,
    **extra) raises TypeError on the old %-style multi-positional call, and the
    broad ``except`` around _finish() swallowed it into a spurious
    ``reconstruct_exception`` degrade — discarding the fully-abstracted arrays on
    the SUCCESS path. A strict StageLogger-signature fake catches a relapse."""
    calls: list[tuple[str, Any, dict]] = []

    class _StrictLog:  # mirrors StageLogger.info's exact signature
        def info(self, reason: str, feature: Any = None, **extra: Any) -> None:
            calls.append((reason, feature, extra))

    ufs, pfs, dev_map, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [],
        client=_client(_ABS, _MAP_FULL), log=_StrictLog(),
    )
    assert tel["applied"] is True
    assert tel.get("fallback") is None
    assert tel.get("degraded_reason") != "reconstruct_exception"
    # exactly one call, single pre-formatted positional string (no *args overflow)
    assert len(calls) == 1
    reason, feature, extra = calls[0]
    assert feature is None and extra == {}
    assert reason.startswith("stage_6_7d:")


def _anchors(n: int, texts: list[str] | None = None, source: str = "analytics"):
    """Anchor fixtures — default source ``analytics`` (tier-1/action-grain) so
    a rich pool exercises the align path; pass source="i18n" (leaf → tier-2 by
    default) to build an advisory-only pool."""
    from faultline.pipeline_v2.anchor_extractors import ProductAnchor
    items = texts if texts is not None else [f"Capability {i}" for i in range(n)]
    return [ProductAnchor(text=t, source=source, locator=f"a/{i}#k") for i, t in enumerate(items)]


def test_grain_gate_tier1_vs_candidate_ufs() -> None:
    """Align-v2 grain gate: align only when distinct TIER-1 anchors >= candidate
    user_flows AND >= floor(8). Tier-2 (i18n leaf values) NEVER counts."""
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import _grain_gate
    assert _grain_gate([], 1) == (False, 0, 0)
    assert _grain_gate(_anchors(3), 1) == (False, 3, 0)          # below floor
    assert _grain_gate(_anchors(8), 3) == (True, 8, 0)           # >= floor and >= UFs
    assert _grain_gate(_anchors(8), 10) == (False, 8, 0)         # 8 tier1 < 10 UFs
    assert _grain_gate(_anchors(10), 10) == (True, 10, 0)
    # a huge tier-2 pool can never open the gate (Soc0 leaf-value lesson)
    assert _grain_gate(_anchors(500, source="i18n"), 3) == (False, 0, 500)


def test_sparse_anchors_fall_back_to_free_gen() -> None:
    _u, _p, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], product_anchors=_anchors(3),
        client=_client(_ABS, _MAP_FULL))
    assert tel["aligned"] is False      # gate → free-gen
    assert tel["applied"] is True       # free-gen still applies (never-worse)


def test_align_is_opt_in_default_off() -> None:
    """Align defaults OFF (it degrades stability on noisy pools) — even with rich
    anchors, free-gen runs unless FAULTLINE_STAGE_6_7D_ALIGN is set."""
    anchors = _anchors(12)
    _u, _p, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], product_anchors=anchors,
        client=_client(_ABS, _MAP_FULL))
    assert tel["aligned"] is False
    assert tel["applied"] is True


def test_rich_anchors_align_with_verbatim_names(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_ALIGN", "1")  # opt in
    anchors = _anchors(0, texts=["Account Management", "Authentication", "Billing",
                                 "Settings", "Reports", "Search", "Notifications", "Team"])
    _u, pfs, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], product_anchors=anchors,
        client=_client(_ABS, _MAP_FULL))
    assert tel["aligned"] is True
    assert tel["anchor_count"] == 8
    assert tel["applied"] is True
    # verbatim anchor text is preserved as the display_name (name is slugified);
    # display_name is the customer-facing, run-to-run-stable string.
    assert {"Account Management", "Authentication"} <= {p.display_name for p in pfs}


def test_output_is_deterministically_ordered() -> None:
    """Phase 1 stability: abstracted product_features + user_flows come back in a
    stable sort (by name / id) regardless of LLM emission order, so re-scans and
    independent scans never churn the output array order."""
    ufs, pfs, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL))
    assert tel["applied"] is True
    pf_names = [p.name for p in pfs]
    uf_names = [(u.name or "").lower() for u in ufs]
    assert pf_names == sorted(pf_names, key=lambda n: (n.lower(), n))
    assert uf_names == sorted(uf_names)


def test_output_order_stable_across_llm_emission_order() -> None:
    """Regression (audit 2026-07-01): the UF/PF sort must be CONTENT-derived so two
    runs whose LLM emits the SAME items in a DIFFERENT order produce the SAME final
    array order. A position-derived (UF-id) sort is a no-op and fails this."""
    payload = json.loads(_ABS)
    rev = json.dumps({"product_features": list(reversed(payload["product_features"])),
                      "user_flows": list(reversed(payload["user_flows"]))})
    u1, p1, _m, _t = run_journey_abstraction(_ufs(), _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL))
    u2, p2, _m, _t = run_journey_abstraction(_ufs(), _pfs(), _devs(), [], client=_client(rev, _MAP_FULL))
    assert [u.name for u in u1] == [u.name for u in u2]
    assert [p.name for p in p1] == [p.name for p in p2]


def test_rewrites_user_flows_and_product_features() -> None:
    ufs, pfs, dev_map, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL))
    assert tel["applied"] is True
    assert {u.name for u in ufs} == {"Manage accounts", "Sign in"}
    assert {p.display_name for p in pfs} == {"Account Management", "Authentication", "Shared Platform"}
    # UF count coarsened 3 -> 2
    assert tel["uf_before"] == 3 and tel["uf_after"] == 2


def test_member_flow_ids_inherited_via_from_flows() -> None:
    ufs, _pfs_out, _m, _t = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL))
    manage = next(u for u in ufs if u.name == "Manage accounts")
    assert sorted(manage.member_flow_ids) == ["f1", "f2"]  # union of UF-001+UF-002
    assert manage.member_count == 2


def test_files_conserved_all_devs_attributed() -> None:
    devs = _devs()
    total_dev_files = sum(len(d.paths) for d in devs)  # 5
    _ufs_out, pfs, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), devs, [], client=_client(_ABS, _MAP_FULL))
    attributed = sum(len(p.paths) for p in pfs)
    assert attributed == total_dev_files == 5
    assert tel["files_after"] == 5


def test_member_files_ledger_carried_through() -> None:
    """New product features must carry member_files (the owned-files ledger the
    dashboard/coverage/blob metric read), not just .paths — member_files can be
    richer than .paths and aggregate_product_feature only unions .paths."""
    devs = [
        _feat("accounts", ["app/accounts/a.ts"],
              member_files=["app/accounts/a.ts", "app/accounts/extra.ts"]),
        _feat("auth", ["app/auth/login.ts"], member_files=["app/auth/login.ts"]),
        _feat("shared-ui", ["packages/ui/b.tsx"], member_files=["packages/ui/b.tsx"]),
    ]
    _u, pfs, _m, _t = run_journey_abstraction(
        _ufs(), _pfs(), devs, [], client=_client(_ABS, _MAP_FULL))
    all_mf = {mf.path for p in pfs for mf in (p.member_files or [])}
    # every dev member_file is attributed to some product feature (none lost)
    assert all_mf == {"app/accounts/a.ts", "app/accounts/extra.ts",
                      "app/auth/login.ts", "packages/ui/b.tsx"}


def test_residual_catches_unmapped_devs() -> None:
    # Map omits shared-ui -> it must land in the Shared Platform residual,
    # so no files are lost.
    devs = _devs()
    _ufs_out, pfs, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), devs, [], client=_client(_ABS, _MAP_OMITS_SHARED))
    names = {p.display_name for p in pfs}
    assert "Shared Platform" in names
    shared = next(p for p in pfs if p.display_name == "Shared Platform")
    assert sorted(shared.paths) == ["packages/ui/button.tsx", "packages/ui/card.tsx"]
    assert sum(len(p.paths) for p in pfs) == 5  # still conserved
    assert tel["residual_devs"] == 1


def test_dev_to_product_map_returned() -> None:
    _u, _p, dev_map, _t = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL))
    assert dev_map is not None
    assert dev_map["accounts"] == ("account-management",)
    assert dev_map["shared-ui"] == ("shared-platform",)


def test_degrade_no_client() -> None:
    ufs_in, pfs_in = _ufs(), _pfs()
    ufs, pfs, dev_map, tel = run_journey_abstraction(
        ufs_in, pfs_in, _devs(), [], client=None, _client_factory=lambda: None)
    assert ufs is ufs_in and pfs is pfs_in  # byte-identical passthrough
    assert dev_map is None
    assert tel["applied"] is False and tel["degraded_reason"] == "no_client"


def test_degrade_bad_json() -> None:
    ufs_in, pfs_in = _ufs(), _pfs()
    bad = _client("not json at all", _MAP_FULL)
    ufs, pfs, dev_map, tel = run_journey_abstraction(ufs_in, pfs_in, _devs(), [], client=bad)
    assert ufs is ufs_in and pfs is pfs_in
    assert tel["applied"] is False and tel["degraded_reason"] == "abstraction_parse_failed"


def test_degrade_empty_abstraction() -> None:
    ufs_in, pfs_in = _ufs(), _pfs()
    empty = _client(json.dumps({"product_features": [], "user_flows": []}), _MAP_FULL)
    ufs, pfs, _m, tel = run_journey_abstraction(ufs_in, pfs_in, _devs(), [], client=empty)
    assert ufs is ufs_in and pfs is pfs_in
    assert tel["degraded_reason"] == "abstraction_empty"


def test_degrade_llm_raises() -> None:
    ufs_in, pfs_in = _ufs(), _pfs()
    ufs, pfs, _m, tel = run_journey_abstraction(
        ufs_in, pfs_in, _devs(), [], client=_raising_client())
    assert ufs is ufs_in and pfs is pfs_in
    assert tel["applied"] is False


def test_degrade_no_dev_features() -> None:
    ufs_in, pfs_in = _ufs(), _pfs()
    ufs, pfs, _m, tel = run_journey_abstraction(
        ufs_in, pfs_in, [], [], client=_client(_ABS, _MAP_FULL))
    assert ufs is ufs_in and pfs is pfs_in
    assert tel["degraded_reason"] == "no_dev_features"


def test_intent_derived_from_journey_name() -> None:
    ufs, _p, _m, _t = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL))
    assert next(u for u in ufs if u.name == "Manage accounts").intent == "manage"
    assert next(u for u in ufs if u.name == "Sign in").intent == "execute"


def test_degrade_call2_fails_returns_originals() -> None:
    """Call 1 (abstraction) OK but Call 2 (re-attribution) raises → must degrade
    fully to the ORIGINAL arrays, NOT emit a degenerate single-blob PF layer."""
    class _PartialFail:
        _n = 0
        class _M:
            def create(self_inner, **kw: Any) -> Any:
                _PartialFail._n += 1
                if _PartialFail._n == 1:
                    return _Msg(_ABS)
                raise RuntimeError("call2 boom")
        messages = _M()
    ufs_in, pfs_in = _ufs(), _pfs()
    ufs, pfs, dev_map, tel = run_journey_abstraction(
        ufs_in, pfs_in, _devs(), [], client=_PartialFail())
    assert ufs is ufs_in and pfs is pfs_in     # byte-identical passthrough
    assert dev_map is None
    assert tel["applied"] is False and tel["degraded_reason"] == "reattrib_failed"


def test_degrade_call2_empty_map() -> None:
    """Call 2 returns a syntactically-valid but EMPTY map → degrade."""
    empty_map = _client(_ABS, json.dumps({"map": {}}))
    ufs_in, pfs_in = _ufs(), _pfs()
    ufs, pfs, dev_map, tel = run_journey_abstraction(ufs_in, pfs_in, _devs(), [], client=empty_map)
    assert ufs is ufs_in and pfs is pfs_in
    assert tel["degraded_reason"] == "reattrib_failed"


def test_duplicate_pf_names_deduped() -> None:
    """LLM echoing a capability name twice must NOT emit duplicate PFs."""
    dup_abs = json.dumps({
        "product_features": [
            {"name": "Account Management", "description": "a"},
            {"name": "Account Management", "description": "echoed dup"},
            {"name": "Authentication", "description": "b"},
        ],
        "user_flows": [
            {"name": "Manage accounts", "resource": "account",
             "product_feature": "Account Management", "from_flows": ["UF-001"]},
        ],
    })
    _u, pfs, _m, _t = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(dup_abs, _MAP_FULL))
    names = [p.display_name for p in pfs]
    assert names.count("Account Management") == 1   # deduped, not 2


def test_parse_json_tolerates_trailing_prose() -> None:
    """Balanced-brace extraction must recover JSON even with trailing prose
    containing braces (a greedy match would swallow it and fail)."""
    abs_with_prose = (_ABS + "\n\nNote: I merged some flows {like CRUD variants} "
                      "as instructed.")
    ufs, pfs, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(abs_with_prose, _MAP_FULL))
    assert tel["applied"] is True
    assert {u.name for u in ufs} == {"Manage accounts", "Sign in"}


def test_gate_default_off(monkeypatch: Any) -> None:
    monkeypatch.delenv("FAULTLINE_STAGE_6_7D_LLM_ABSTRACTION", raising=False)
    assert is_enabled() is False
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_LLM_ABSTRACTION", "1")
    assert is_enabled() is True


def test_central_flow_graph_untouched() -> None:
    # The stage takes NO flows[] argument — proves it cannot mutate the graph.
    import inspect
    from faultline.pipeline_v2 import stage_6_7d_llm_journey_abstraction as m
    sig = inspect.signature(m.run_journey_abstraction)
    assert "flows" not in sig.parameters


# ── Ship changes: Sonnet model / never-worse fallback / large-repo / cache ──

class _CapturingClient:
    """Records the (system, model) of every create() call and routes the
    response by system prompt (abstraction vs re-attribution)."""

    def __init__(self, abstraction: str, reattrib: str) -> None:
        self.abstraction = abstraction
        self.reattrib = reattrib
        self.calls: list[dict[str, Any]] = []
        outer = self

        class _M:
            def create(self, **kw: Any) -> Any:
                outer.calls.append({"system": kw.get("system", ""),
                                    "model": kw.get("model", "")})
                sysp = kw.get("system", "")
                is_reattrib = "assign each developer feature" in sysp
                return _Msg(outer.reattrib if is_reattrib else outer.abstraction)

        self.messages = _M()


class _FakeCache:
    """In-memory CacheBackend for replay tests."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], Any] = {}

    def get(self, kind: str, key: str) -> Any:
        return self.store.get((kind, key))

    def set(self, kind: str, key: str, value: Any, *, ttl_seconds: Any = None) -> None:
        self.store[(kind, key)] = value

    def delete(self, kind: str, key: str) -> None:
        self.store.pop((kind, key), None)

    def load_namespace(self, kind: str) -> dict[str, Any]:
        return {}

    def flush(self) -> None:
        pass


def test_abstraction_model_defaults_to_sonnet_env(monkeypatch: Any) -> None:
    """Change 1: Call 1 (abstraction) resolves to the Sonnet env default,
    INDEPENDENT of the passed model_id (which stays on the Haiku default for
    Call 2)."""
    monkeypatch.delenv("FAULTLINE_STAGE_6_7D_ABSTRACTION_MODEL", raising=False)
    assert resolve_abstraction_model() == DEFAULT_ABSTRACTION_MODEL  # claude-sonnet-4-6
    cli = _CapturingClient(_ABS, _MAP_FULL)
    _u, _p, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=cli, model="claude-haiku-4-5-20251001")
    assert tel["applied"] is True
    # Call 1 (abstraction system prompt) went out on a SONNET gateway id.
    abs_call = next(c for c in cli.calls if "assign each developer feature" not in c["system"])
    reattrib_call = next(c for c in cli.calls if "assign each developer feature" in c["system"])
    assert "sonnet" in abs_call["model"].lower()
    assert "haiku" in reattrib_call["model"].lower()   # Call 2 stays on passed model
    assert tel["abstraction_model"] == DEFAULT_ABSTRACTION_MODEL


def test_abstraction_model_env_override(monkeypatch: Any) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_ABSTRACTION_MODEL", "claude-haiku-4-5")
    assert resolve_abstraction_model() == "claude-haiku-4-5"
    cli = _CapturingClient(_ABS, _MAP_FULL)
    _u, _p, _m, tel = run_journey_abstraction(_ufs(), _pfs(), _devs(), [], client=cli)
    assert tel["abstraction_model"] == "claude-haiku-4-5"
    abs_call = next(c for c in cli.calls if "assign each developer feature" not in c["system"])
    assert "haiku" in abs_call["model"].lower()


def test_abstraction_model_empty_env_falls_back(monkeypatch: Any) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_ABSTRACTION_MODEL", "   ")
    assert resolve_abstraction_model() == DEFAULT_ABSTRACTION_MODEL


def test_fallback_field_set_on_every_failure_path() -> None:
    """Change 2 (never-worse): every failure path returns ORIGINAL inputs
    (identity) AND sets tele['fallback'] to the reason string."""
    ufs_in, pfs_in = _ufs(), _pfs()

    # no dev features
    u, p, dm, tel = run_journey_abstraction(ufs_in, pfs_in, [], [], client=_client(_ABS, _MAP_FULL))
    assert u is ufs_in and p is pfs_in and dm is None
    assert tel["fallback"] == "no_dev_features" and tel["applied"] is False

    # no client
    u, p, dm, tel = run_journey_abstraction(
        ufs_in, pfs_in, _devs(), [], client=None, _client_factory=lambda: None)
    assert u is ufs_in and p is pfs_in and tel["fallback"] == "no_client"

    # bad abstraction JSON
    u, p, dm, tel = run_journey_abstraction(
        ufs_in, pfs_in, _devs(), [], client=_client("not json", _MAP_FULL))
    assert u is ufs_in and p is pfs_in and tel["fallback"] == "abstraction_parse_failed"

    # empty abstraction
    empty = _client(json.dumps({"product_features": [], "user_flows": []}), _MAP_FULL)
    u, p, dm, tel = run_journey_abstraction(ufs_in, pfs_in, _devs(), [], client=empty)
    assert u is ufs_in and p is pfs_in and tel["fallback"] == "abstraction_empty"

    # re-attribution failed (Call 2 raises)
    u, p, dm, tel = run_journey_abstraction(
        ufs_in, pfs_in, _devs(), [], client=_client(_ABS, "not json"))
    assert u is ufs_in and p is pfs_in and tel["fallback"] == "reattrib_failed"

    # LLM raises entirely
    u, p, dm, tel = run_journey_abstraction(
        ufs_in, pfs_in, _devs(), [], client=_raising_client())
    assert u is ufs_in and p is pfs_in and tel["fallback"] is not None


def test_fallback_is_none_on_success() -> None:
    _u, _p, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL))
    assert tel["applied"] is True and tel["fallback"] is None


def _many_ufs(n: int) -> list[UserFlow]:
    return [_uf(f"UF-{i:03d}", f"Do thing {i}", [f"f{i}"]) for i in range(1, n + 1)]


def test_large_uf_input_capped_in_digest_no_crash() -> None:
    """Change 3: a dub-scale UF count (222) must NOT crash and must be CAPPED
    in the abstraction digest (supporting detail only)."""
    big_ufs = _many_ufs(222)
    # from_flows in _ABS references UF-001..UF-003 which still exist here.
    _u, _p, _m, tel = run_journey_abstraction(
        big_ufs, _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL))
    assert tel["applied"] is True                       # abstracted, did not degrade
    assert tel["input_user_flows"] == 222
    assert tel["digest_user_flows"] == MAX_USER_FLOWS_DIGEST  # capped to top-N
    assert tel["digest_user_flows"] < tel["input_user_flows"]


def test_cache_hit_returns_identical_output() -> None:
    """Change 4: a warm content-hash cache replays byte-identical output with
    NO LLM call (a subsequent raising client proves the LLM is not touched)."""
    cache = _FakeCache()
    # Same repo state across both scans = identical input objects (the fixtures
    # stamp a fresh now() per call, so reuse them to model an UNCHANGED repo).
    ufs_in, pfs_in, devs_in = _ufs(), _pfs(), _devs()
    ufs1, pfs1, map1, tel1 = run_journey_abstraction(
        ufs_in, pfs_in, devs_in, [], client=_client(_ABS, _MAP_FULL), cache=cache)
    assert tel1["applied"] is True and tel1["cache_hit"] is False
    assert len(cache.store) == 1

    # Second run: same inputs, cache warm, LLM would RAISE if called.
    ufs2, pfs2, map2, tel2 = run_journey_abstraction(
        ufs_in, pfs_in, devs_in, [], client=_raising_client(), cache=cache)
    assert tel2["cache_hit"] is True and tel2["applied"] is True
    assert tel2["llm_calls"] == 0 and tel2["cost_usd"] == 0.0
    assert tel2["fallback"] is None
    # Byte-identical reconstruction.
    assert [u.model_dump() for u in ufs2] == [u.model_dump() for u in ufs1]
    assert [p.model_dump() for p in pfs2] == [p.model_dump() for p in pfs1]
    assert map2 == map1


def test_cache_write_then_key_changes_on_model(monkeypatch: Any) -> None:
    """A different abstraction model → different cache key → cold miss (no
    stale replay across models)."""
    cache = _FakeCache()
    monkeypatch.delenv("FAULTLINE_STAGE_6_7D_ABSTRACTION_MODEL", raising=False)
    run_journey_abstraction(_ufs(), _pfs(), _devs(), [],
                            client=_client(_ABS, _MAP_FULL), cache=cache)
    assert len(cache.store) == 1
    # Flip the abstraction model → new key → the warm entry must NOT be reused
    # (a raising client would surface a wrongful hit).
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_ABSTRACTION_MODEL", "claude-haiku-4-5")
    _u, _p, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL), cache=cache)
    assert tel["cache_hit"] is False and tel["applied"] is True
    assert len(cache.store) == 2   # two distinct keys now cached


def test_cache_fault_never_aborts_stage() -> None:
    """A cache backend that raises on get/set must not break the stage."""
    class _BadCache(_FakeCache):
        def get(self, kind: str, key: str) -> Any:
            raise RuntimeError("cache down")

        def set(self, kind: str, key: str, value: Any, *, ttl_seconds: Any = None) -> None:
            raise RuntimeError("cache down")

    _u, _p, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL), cache=_BadCache())
    assert tel["applied"] is True and tel["cache_hit"] is False


# ── Audit fixes (ship): C1 reconstruct-exception, I1 digest determinism ──────

def test_nonstring_name_sanitised_not_crash() -> None:
    """A non-string 'name' in otherwise-valid JSON must be DROPPED at the
    boundary — never crash the scan (never-worse). (Audit C1.)"""
    mixed_abs = json.dumps({
        "product_features": [
            {"name": "Account Management", "description": "ok"},
            {"name": 42, "description": "numeric — must be dropped"},
        ],
        "user_flows": [
            {"name": "Manage accounts", "resource": "account",
             "product_feature": "Account Management",
             "from_flows": ["UF-001"]},  # grounded — ungrounded UFs drop (fix B)
            {"name": None, "resource": "x", "product_feature": "Y"},
        ],
    })
    reattrib = json.dumps({"map": {"accounts": "Account Management",
                                   "auth": "Account Management",
                                   "shared-ui": "Shared Platform"}})
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(mixed_abs, reattrib))
    assert tel["applied"] is True                       # sanitised, did not crash
    assert "Manage accounts" in {u.name for u in ufs}   # valid kept
    assert all(isinstance(p.display_name, str) for p in pfs)


def test_digest_deterministic_under_input_order() -> None:
    """Equal-commit dev features must not make the digest order (and thus the
    cache key) depend on input order — else the byte-identical re-scan cache
    never hits. (Audit I1.)"""
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import _build_digest
    a = _feat("alpha", ["a/x.ts"])   # _feat sets total_commits=3 for all → tie
    b = _feat("beta", ["b/y.ts"])
    d1 = _build_digest([a, b], [], [], [])
    d2 = _build_digest([b, a], [], [], [])
    assert json.dumps(d1, ensure_ascii=False) == json.dumps(d2, ensure_ascii=False)


# ── Trustworthy-core fixes (2026-07-02): A1 platform-marked residual, B UF grounding ──

def _flow(name: str, uuid: str) -> Any:
    from faultline.models.types import Flow
    return Flow(
        name=name, uuid=uuid, paths=[f"app/{name}.ts"], authors=["a"],
        total_commits=2, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=95.0,
    )


def _devs_with_flows() -> list[Feature]:
    devs = _devs()
    # "auth" carries two real flows the grounding passes can attach.
    devs[1].flows = [_flow("password-reset-flow", "fx-1"),
                     _flow("session-login-flow", "fx-2")]
    return devs


def test_from_dev_features_grounds_empty_uf() -> None:
    """Fix B channel 2a: a UF with no from_flows but a cited dev feature gets
    that dev's content-overlapping flows as members (no 0-LOC journey)."""
    abs_payload = json.dumps({
        "product_features": [{"name": "Authentication", "description": "auth"}],
        "user_flows": [
            {"name": "Reset password", "resource": "password",
             "product_feature": "Authentication", "from_flows": [],
             "from_dev_features": ["auth"]},
        ],
    })
    reattrib = json.dumps({"map": {"accounts": "Authentication",
                                   "auth": "Authentication",
                                   "shared-ui": "Shared Platform"}})
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs_with_flows(), [], client=_client(abs_payload, reattrib))
    assert tel["applied"] is True
    uf = next(u for u in ufs if u.name == "Reset password")
    assert uf.member_flow_ids == ["fx-1"]  # token overlap: "password"
    assert uf.member_count == 1
    assert tel["uf_dev_grounded"] == 1
    assert tel["uf_dropped_ungrounded"] == 0


def test_ungrounded_uf_dropped_with_telemetry() -> None:
    """Fix B: a UF with no from_flows, no from_dev_features, no token match and
    no route match is DROPPED — never a 0-LOC journey in the output."""
    abs_payload = json.dumps({
        "product_features": [{"name": "Account Management", "description": "x"}],
        "user_flows": [
            {"name": "Manage accounts", "resource": "account",
             "product_feature": "Account Management", "from_flows": ["UF-001"]},
            {"name": "Quantum teleportation", "resource": "qubit",
             "product_feature": "Account Management", "from_flows": []},
        ],
    })
    reattrib = json.dumps({"map": {"accounts": "Account Management",
                                   "auth": "Account Management",
                                   "shared-ui": "Shared Platform"}})
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(abs_payload, reattrib))
    assert tel["applied"] is True
    names = {u.name for u in ufs}
    assert "Quantum teleportation" not in names
    assert "Manage accounts" in names
    assert tel["uf_dropped_ungrounded"] == 1
    assert tel["uf_dropped_names"] == ["Quantum teleportation"]
    assert all(u.member_flow_ids or u.routes for u in ufs)


def test_rescue_by_resource_token_match() -> None:
    """Fix B channel 2b: an empty UF whose resource matches an UNCLAIMED
    flow's tokens gets that flow attached deterministically."""
    abs_payload = json.dumps({
        "product_features": [{"name": "Authentication", "description": "auth"}],
        "user_flows": [
            {"name": "Sign in to your workspace", "resource": "session",
             "product_feature": "Authentication", "from_flows": []},
        ],
    })
    reattrib = json.dumps({"map": {"accounts": "Authentication",
                                   "auth": "Authentication",
                                   "shared-ui": "Shared Platform"}})
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs_with_flows(), [], client=_client(abs_payload, reattrib))
    assert tel["applied"] is True
    uf = next(u for u in ufs if u.name.startswith("Sign in"))
    assert "fx-2" in uf.member_flow_ids  # "session-login-flow" ∋ "session"
    assert tel["uf_rescued_flows"] == 1


def test_rescue_by_route_pattern() -> None:
    """Fix B channel 2c: a flow-less UF whose resource appears as a segment in
    routes_index keeps the route patterns as grounding (real but flow-less)."""
    routes = [{"pattern": "/api/webhooks/{id}", "method": "POST", "trigger": "webhook"},
              {"pattern": "/api/accounts", "method": "GET", "trigger": None}]
    abs_payload = json.dumps({
        "product_features": [{"name": "Webhooks", "description": "wh"}],
        "user_flows": [
            {"name": "Receive webhook", "resource": "webhook",
             "product_feature": "Webhooks", "from_flows": []},
        ],
    })
    reattrib = json.dumps({"map": {"accounts": "Webhooks", "auth": "Webhooks",
                                   "shared-ui": "Shared Platform"}})
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), routes, client=_client(abs_payload, reattrib))
    assert tel["applied"] is True
    uf = next(u for u in ufs if u.name == "Receive webhook")
    assert uf.routes == ["/api/webhooks/{id}"]
    assert tel["uf_rescued_routes"] == 1


def test_residual_carries_platform_marker() -> None:
    """Fix A1: the Shared Platform residual's description carries the house
    "workspace anchor" marker so blob metrics recognise it as a platform
    bucket instead of the top product feature."""
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, json.dumps(
            {"map": {"accounts": "Account Management", "auth": "Authentication",
                     "shared-ui": "Shared Platform"}})))
    assert tel["applied"] is True
    residual = next(p for p in pfs if p.display_name == "Shared Platform")
    assert "workspace anchor" in (residual.description or "").lower()


def test_omitted_dev_token_rescued_not_residual() -> None:
    """Fix A1: a dev feature Call 2 OMITS is token-matched to an emitted
    capability instead of being dumped into the shared-platform blob."""
    devs = _devs() + [_feat("account-billing", ["app/billing/b.ts"])]
    abs_payload = json.dumps({
        "product_features": [
            {"name": "Account Management", "description": "acc"},
            {"name": "Billing", "description": "money"},
        ],
        "user_flows": [
            {"name": "Manage accounts", "resource": "account",
             "product_feature": "Account Management", "from_flows": ["UF-001"]},
        ],
    })
    # NB: "account-billing" is deliberately MISSING from the map.
    reattrib = json.dumps({"map": {"accounts": "Account Management",
                                   "auth": "Account Management",
                                   "shared-ui": "Shared Platform"}})
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), devs, [], client=_client(abs_payload, reattrib))
    assert tel["applied"] is True
    assert dm["account-billing"] == ("billing",)  # token-matched, NOT shared-platform
    assert tel["devs_token_rescued"] == 1
    billing = next(p for p in pfs if p.display_name == "Billing")
    assert "app/billing/b.ts" in billing.paths


def test_omitted_structure_leak_dev_goes_residual() -> None:
    """Fix A1 guard: an omitted dev feature named like a bare code container
    ("web") must go to the platform residual, never token-rescue."""
    devs = _devs() + [_feat("web", ["apps/web/page.tsx"])]
    abs_payload = json.dumps({
        "product_features": [
            {"name": "Web Analytics", "description": "traffic"},
        ],
        "user_flows": [
            {"name": "Manage accounts", "resource": "account",
             "product_feature": "Web Analytics", "from_flows": ["UF-001"]},
        ],
    })
    reattrib = json.dumps({"map": {"accounts": "Web Analytics",
                                   "auth": "Web Analytics",
                                   "shared-ui": "Shared Platform"}})
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), devs, [], client=_client(abs_payload, reattrib))
    assert tel["applied"] is True
    assert dm["web"] == ("shared-platform",)  # leak slug → residual, no rescue
    assert tel["devs_residual"] >= 1


def test_uf_ids_renumbered_content_stable() -> None:
    """Phase-1 stability: UF ids are assigned AFTER the content sort, so the
    same output content yields the same ids regardless of emission order."""
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, json.dumps(
            {"map": {"accounts": "Account Management", "auth": "Authentication",
                     "shared-ui": "Shared Platform"}})))
    assert tel["applied"] is True
    assert [u.id for u in ufs] == [f"UF-{i:03d}" for i in range(1, len(ufs) + 1)]
    assert [u.name for u in ufs] == sorted((u.name for u in ufs), key=str.lower)


def test_cache_hit_replays_dev_grounded_uf_identically() -> None:
    """Audit #3: the cache payload keeps from_dev_features, so a cache-hit
    reconstruction grounds a dev-grounded UF EXACTLY like the live run —
    even when the replay client would raise (proof nothing is re-called)."""
    from faultline.cache.backend import CacheKind

    class _MemCache:
        def __init__(self) -> None:
            self.store: dict[tuple[str, str], Any] = {}
        def get(self, kind: str, key: str) -> Any:
            return self.store.get((kind, key))
        def set(self, kind: str, key: str, value: Any) -> None:
            self.store[(kind, key)] = value

    abs_payload = json.dumps({
        "product_features": [{"name": "Authentication", "description": "auth"}],
        "user_flows": [
            {"name": "Reset password", "resource": "password",
             "product_feature": "Authentication", "from_flows": [],
             "from_dev_features": ["auth"]},
        ],
    })
    reattrib = json.dumps({"map": {"accounts": "Authentication",
                                   "auth": "Authentication",
                                   "shared-ui": "Shared Platform"}})
    cache = _MemCache()
    ufs1, pfs1, dm1, tel1 = run_journey_abstraction(
        _ufs(), _pfs(), _devs_with_flows(), [],
        client=_client(abs_payload, reattrib), cache=cache)
    assert tel1["applied"] is True and tel1["cache_hit"] is False
    assert tel1["uf_dev_grounded"] == 1
    # Persisted spec must retain the grounding channel.
    (payload,) = [v for (k, _), v in cache.store.items()
                  if k == CacheKind.LLM_ABSTRACTION.value]
    assert payload["abstraction"]["user_flows"][0]["from_dev_features"] == ["auth"]

    ufs2, pfs2, dm2, tel2 = run_journey_abstraction(
        _ufs(), _pfs(), _devs_with_flows(), [],
        client=_raising_client(), cache=cache)  # any live call would raise
    assert tel2["applied"] is True and tel2["cache_hit"] is True
    assert tel2["uf_dev_grounded"] == 1
    assert [(u.id, u.name, u.member_flow_ids) for u in ufs2] == \
           [(u.id, u.name, u.member_flow_ids) for u in ufs1]
    assert dm2 == dm1


def test_dev_grounding_dedups_shared_flow_across_cited_devs() -> None:
    """Audit #1: two cited dev features sharing a physical flow must not
    produce duplicate member ids / inflated member_count."""
    devs = _devs()
    shared = _flow("password-reset-flow", "fx-shared")
    devs[0].flows = [shared]   # accounts
    devs[1].flows = [shared]   # auth — same flow object, same uuid
    abs_payload = json.dumps({
        "product_features": [{"name": "Authentication", "description": "auth"}],
        "user_flows": [
            {"name": "Reset password", "resource": "password",
             "product_feature": "Authentication", "from_flows": [],
             "from_dev_features": ["auth", "accounts", "Auth"]},
        ],
    })
    reattrib = json.dumps({"map": {"accounts": "Authentication",
                                   "auth": "Authentication",
                                   "shared-ui": "Shared Platform"}})
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), devs, [], client=_client(abs_payload, reattrib))
    assert tel["applied"] is True
    uf = next(u for u in ufs if u.name == "Reset password")
    assert uf.member_flow_ids == ["fx-shared"]
    assert uf.member_count == 1


def test_residual_devs_telemetry_counts_actual_residual() -> None:
    """Audit #2: residual_devs must equal devs_residual (devs that actually
    LANDED in the residual), not 'devs omitted from the map'."""
    devs = _devs() + [_feat("account-billing", ["app/billing/b.ts"])]
    abs_payload = json.dumps({
        "product_features": [
            {"name": "Account Management", "description": "acc"},
            {"name": "Billing", "description": "money"},
        ],
        "user_flows": [
            {"name": "Manage accounts", "resource": "account",
             "product_feature": "Account Management", "from_flows": ["UF-001"]},
        ],
    })
    # Both "account-billing" (token-rescuable) and "shared-ui" (residual)
    # are omitted from the map.
    reattrib = json.dumps({"map": {"accounts": "Account Management",
                                   "auth": "Account Management"}})
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), devs, [], client=_client(abs_payload, reattrib))
    assert tel["applied"] is True
    assert tel["devs_token_rescued"] == 1          # account-billing → Billing
    assert tel["residual_devs"] == tel["devs_residual"] == 1  # shared-ui only


# ── Split-invariance (2026-07-02): 8.9/8.9.5 subfeatures folded for 6.7d ─────

def _sub(name: str, parent: Feature, paths: list[str]) -> Feature:
    f = _feat(name, paths)
    f.uuid = f"sub-{name}"
    f.split_from = parent.uuid
    f.description = f"sub-domain '{paths[0].rsplit('/', 1)[0]}' of feature '{parent.name}'"
    return f


def test_rollup_view_folds_subs_into_parent() -> None:
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        _rollup_split_view,
    )
    parent = _feat("web", ["apps/web/app.tsx"])
    parent.uuid = "web-uuid"
    s1 = _sub("issues", parent, ["apps/web/components/issues/a.tsx"])
    s2 = _sub("cycles", parent, ["apps/web/components/cycles/b.tsx"])
    orphan = _sub("ghost", parent, ["apps/web/components/ghost/c.tsx"])
    orphan.split_from = "missing-uuid"   # parent husk dropped
    orphan.description = "sub-domain 'x' of feature 'gone'"  # name unresolvable
    plain = _feat("auth", ["app/auth/login.ts"])
    view, sub_to_parent = _rollup_split_view([parent, s1, s2, orphan, plain])
    names = [getattr(v, "name") for v in view]
    assert "issues" not in names and "cycles" not in names
    assert "ghost" in names and "auth" in names   # orphan + plain stay
    folded = next(v for v in view if v.name == "web")
    assert "apps/web/components/issues/a.tsx" in folded.paths
    assert "apps/web/components/cycles/b.tsx" in folded.paths
    # 3 commits each (fixture) — parent + 2 folded subs
    assert folded.total_commits == 9
    assert sub_to_parent == {"issues": "web", "cycles": "web"}
    # real objects untouched
    assert parent.paths == ["apps/web/app.tsx"]


def test_rollup_matches_by_name_when_split_from_missing() -> None:
    """Production shape (supabase wave-9): uuid backfill happens AFTER
    Stage 8, so subs carry split_from=None — the description NAME channel
    must still fold them."""
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        _rollup_split_view,
    )
    parent = _feat("studio", ["apps/studio/app.tsx"])
    parent.uuid = "later-backfilled"
    sub = _sub("auth", parent, ["apps/studio/components/interfaces/Auth/a.tsx"])
    sub.split_from = None
    view, sub_to_parent = _rollup_split_view([parent, sub])
    assert sub_to_parent == {"auth": "studio"}
    assert len(view) == 1 and view[0].name == "studio"


def test_split_subfeatures_inherit_parent_capability() -> None:
    """End-to-end: Call 2 maps the PARENT; sub files aggregate into the
    parent's capability PF (same placement as a no-split scan)."""
    parent = _feat("web", ["apps/web/app.tsx"])
    parent.uuid = "web-uuid"
    s1 = _sub("issues", parent, ["apps/web/components/issues/a.tsx"])
    devs = [parent, s1] + [_feat("auth", ["app/auth/login.ts"])]
    abs_payload = json.dumps({
        "product_features": [
            {"name": "Issue Tracking", "description": "issues"},
            {"name": "Authentication", "description": "auth"},
        ],
        "user_flows": [
            {"name": "Manage issues", "resource": "issue",
             "product_feature": "Issue Tracking", "from_flows": ["UF-001"]},
        ],
    })
    # Call 2 sees the ROLLED view → maps parent "web"; "issues" NOT in map.
    reattrib = json.dumps({"map": {"web": "Issue Tracking",
                                   "auth": "Authentication"}})
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), devs, [], client=_client(abs_payload, reattrib))
    assert tel["applied"] is True
    assert tel["digest_rolled_subs"] == 1
    assert dm["issues"] == ("issue-tracking",)   # inherited from parent
    it = next(p for p in pfs if p.display_name == "Issue Tracking")
    assert "apps/web/components/issues/a.tsx" in it.paths


def test_digest_invariant_to_split_depth() -> None:
    """The 6.7d digest must be IDENTICAL whether the container was split or
    not — the split-invariance contract."""
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        _build_digest, _rollup_split_view,
    )
    whole = _feat("web", ["apps/web/a.tsx", "apps/web/components/issues/b.tsx"])
    whole.uuid = "web-uuid"
    whole.total_commits = 6

    husk = _feat("web", ["apps/web/a.tsx"])
    husk.uuid = "web-uuid"
    husk.total_commits = 3
    sub = _sub("issues", husk, ["apps/web/components/issues/b.tsx"])
    sub.total_commits = 3

    view_split, _ = _rollup_split_view([husk, sub])
    d_whole = _build_digest([whole], [], [], [])
    d_split = _build_digest(view_split, [], [], [])
    # Same names, same dirs, same n_dev_features — description text may differ.
    assert d_whole["n_dev_features"] == 1 == d_split["n_dev_features"]
    w = d_whole["developer_features"][0]; v = d_split["developer_features"][0]
    assert w["name"] == v["name"] and w["where"] == v["where"]


def test_rollup_folds_multilevel_chain_to_root() -> None:
    """Audit IMPORTANT: 8.9 recurses on minted subs — a grandchild must fold
    into the ROOT parent, never vanish from both view and folded buckets."""
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        _rollup_split_view,
    )
    parent = _feat("web", ["apps/web/app.tsx"])
    parent.uuid = "web-uuid"
    sub = _sub("issues", parent, ["apps/web/components/issues/a.tsx"])
    grand = _feat("issues-detail", ["apps/web/components/issues/detail/d.tsx"])
    grand.uuid = "sub-grand"
    grand.split_from = sub.uuid
    grand.description = "sub-domain 'detail' of feature 'issues'"
    view, sub_to_parent = _rollup_split_view([parent, sub, grand])
    assert sub_to_parent == {"issues": "web", "issues-detail": "web"}
    assert len(view) == 1
    folded = view[0]
    assert "apps/web/components/issues/a.tsx" in folded.paths
    assert "apps/web/components/issues/detail/d.tsx" in folded.paths  # not lost


def test_cache_hit_propagates_capability_to_subs() -> None:
    """Audit gap: propagation must hold on the CACHE-HIT path — the cache
    stores the PARENT-level map; subs re-inherit at every reconstruction."""
    from faultline.cache.backend import CacheKind

    class _MemCache:
        def __init__(self) -> None:
            self.store: dict[tuple[str, str], Any] = {}
        def get(self, kind: str, key: str) -> Any:
            return self.store.get((kind, key))
        def set(self, kind: str, key: str, value: Any) -> None:
            self.store[(kind, key)] = value

    parent = _feat("web", ["apps/web/app.tsx"])
    parent.uuid = "web-uuid"
    s1 = _sub("issues", parent, ["apps/web/components/issues/a.tsx"])
    devs = [parent, s1, _feat("auth", ["app/auth/login.ts"])]
    abs_payload = json.dumps({
        "product_features": [{"name": "Issue Tracking", "description": "i"}],
        "user_flows": [
            {"name": "Manage issues", "resource": "issue",
             "product_feature": "Issue Tracking", "from_flows": ["UF-001"]},
        ],
    })
    reattrib = json.dumps({"map": {"web": "Issue Tracking",
                                   "auth": "Issue Tracking"}})
    cache = _MemCache()
    ufs1, pfs1, dm1, tel1 = run_journey_abstraction(
        _ufs(), _pfs(), devs, [], client=_client(abs_payload, reattrib),
        cache=cache)
    assert tel1["applied"] and not tel1["cache_hit"]
    assert dm1["issues"] == ("issue-tracking",)
    # Cached map must stay PARENT-level ("issues" absent).
    (payload,) = [v for (k, _), v in cache.store.items()
                  if k == CacheKind.LLM_ABSTRACTION.value]
    assert "issues" not in payload["map"]

    ufs2, pfs2, dm2, tel2 = run_journey_abstraction(
        _ufs(), _pfs(), devs, [], client=_raising_client(), cache=cache)
    assert tel2["applied"] and tel2["cache_hit"]
    assert dm2["issues"] == ("issue-tracking",)   # re-propagated on replay
    assert dm2 == dm1


# ── Grain-contract gate + route-rescue collision rule (2026-07-04) ──────────

from faultline.pipeline_v2 import stage_6_7d_llm_journey_abstraction as _mod
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    ABSTRACTION_CACHE_VERSION,
    CONTRACT_PASS,
    CONTRACT_PASS_AFTER_RETRY,
    CONTRACT_UNCOMPRESSED,
)


def _seq_client(abstraction_payloads: list[str], reattrib: str) -> Any:
    """Fake client returning abstraction_payloads[i] on the i-th Call-1 draw
    (last payload repeats) and the reattrib map for Call 2. Records the system
    prompt of every abstraction call."""
    state: dict[str, Any] = {"i": 0, "systems": []}

    class _C:
        class _M:
            def create(self, **kw: Any) -> Any:
                sysp = kw.get("system", "")
                if "assign each developer feature" in sysp:
                    return _Msg(reattrib)
                state["systems"].append(sysp)
                i = min(state["i"], len(abstraction_payloads) - 1)
                state["i"] += 1
                return _Msg(abstraction_payloads[i])
        messages = _M()

    c = _C()
    c.state = state  # type: ignore[attr-defined]
    return c


# 3 uf specs vs a 3-UF digest whose UFs share ONE resource ("thing" via _uf):
# gate armed (1 distinct resource < 0.9*3) and 3 emitted >= 0.9*3 → violation.
_UNCOMPRESSED = json.dumps({
    "product_features": [{"name": "Account Management", "description": "acc"}],
    "user_flows": [
        {"name": "Create account", "resource": "account",
         "product_feature": "Account Management", "from_flows": ["UF-001"]},
        {"name": "Update account", "resource": "account",
         "product_feature": "Account Management", "from_flows": ["UF-002"]},
        {"name": "Sign in", "resource": "session",
         "product_feature": "Account Management", "from_flows": ["UF-003"]},
    ],
})


def test_contract_pass_no_retry() -> None:
    """A compressed first draw (2 journeys vs 3 digest UFs) passes the gate:
    no retry, contract=pass, exactly 2 LLM calls (abstraction + reattrib)."""
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL))
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_PASS
    assert tel["llm_calls"] == 2
    assert "abstraction_retried" not in tel


def test_contract_violation_retries_with_merge_corrective() -> None:
    """No-grain-lift draw (3 emitted vs 3 digest UFs, mergeable redundancy) is
    REJECTED; the ONE retry carries the merge-corrective system addendum and
    its compressed result ships with contract=pass_after_retry."""
    cli = _seq_client([_UNCOMPRESSED, _ABS], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=cli)
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_PASS_AFTER_RETRY
    assert tel["abstraction_retried"] is True
    assert tel["llm_calls"] == 3  # draw + retry + reattrib
    assert tel["uf_specs_emitted"] == 3
    assert tel["uf_specs_emitted_retry"] == 2
    assert {u.name for u in ufs} == {"Manage accounts", "Sign in"}
    systems = cli.state["systems"]
    assert len(systems) == 2
    assert "PREVIOUS ATTEMPT REJECTED" not in systems[0]
    assert "PREVIOUS ATTEMPT REJECTED" in systems[1]
    assert "product_features list" in systems[1]  # journeys-per-PF anchor


def test_contract_retry_still_uncompressed_kept_and_flagged() -> None:
    """Retry also fails the ratio → keep the RETRY result (never-worse: more
    UFs beats degrading) but flag contract=uncompressed for scan_meta."""
    cli = _seq_client([_UNCOMPRESSED, _UNCOMPRESSED], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=cli)
    assert tel["applied"] is True                       # NOT a degrade
    assert tel["abstraction_contract"] == CONTRACT_UNCOMPRESSED
    assert tel["llm_calls"] == 3
    assert len(ufs) == 3                                # retry result kept
    assert tel["fallback"] is None


def test_contract_retry_unparseable_keeps_first_draw() -> None:
    """Retry draw unusable (garbage JSON) → the valid FIRST draw stands,
    flagged uncompressed. Never-worse: a bad retry must not degrade a stage
    that already holds a valid draw."""
    cli = _seq_client([_UNCOMPRESSED, "not json at all"], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=cli)
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_UNCOMPRESSED
    assert tel["abstraction_retry_failed"] == "abstraction_parse_failed"
    assert len(ufs) == 3                                # first draw kept
    assert {u.name for u in ufs} == {"Create account", "Update account", "Sign in"}


def test_contract_gate_disarmed_at_resource_grain() -> None:
    """A digest already at resource grain (every UF a distinct resource) has
    nothing to compress — a 1:1 draw is legitimate expansion territory
    (libraries), so the gate must NOT fire."""
    ufs_in = _ufs()
    for i, u in enumerate(ufs_in):
        u.resource = f"res{i}"                          # 3 distinct resources
    cli = _seq_client([_UNCOMPRESSED], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        ufs_in, _pfs(), _devs(), [], client=cli)
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_PASS
    assert tel["llm_calls"] == 2                        # no retry
    assert "abstraction_retried" not in tel


def test_contract_retry_skipped_when_cost_capped(monkeypatch: Any) -> None:
    """Structural cost guard: when a second same-shape Sonnet call could bust
    the whole-stage cap, the retry is skipped and the first draw ships
    flagged uncompressed (never a cap-degrade caused BY the gate)."""
    from faultline.llm.cost import estimate_call_cost
    one_call = estimate_call_cost(DEFAULT_ABSTRACTION_MODEL, 400, 200)
    monkeypatch.setattr(_mod, "COST_CAP_USD", one_call * 1.5)
    cli = _seq_client([_UNCOMPRESSED, _ABS], _MAP_FULL)
    ufs, pfs, dm, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=cli)
    assert tel["applied"] is True
    assert tel["abstraction_contract"] == CONTRACT_UNCOMPRESSED
    assert tel["abstraction_retry_skipped_cost"] is True
    assert len(cli.state["systems"]) == 1               # no retry call issued
    assert len(ufs) == 3


def test_route_rescue_collision_single_survivor() -> None:
    """Pass-2c collision rule: N otherwise-empty journeys resolving to the
    SAME route set → only the first is route-rescued; the rest are phantoms
    dropped as ungrounded, counted in uf_rescue_dropped_collisions
    (inbox-zero: 15 zero-member journeys on ONE /items route)."""
    routes = [{"pattern": "/api/webhooks/{id}", "method": "POST", "trigger": "webhook"}]
    abs_payload = json.dumps({
        "product_features": [{"name": "Webhooks", "description": "wh"}],
        "user_flows": [
            {"name": "Receive webhook", "resource": "webhook",
             "product_feature": "Webhooks", "from_flows": []},
            {"name": "Replay webhook delivery", "resource": "webhook",
             "product_feature": "Webhooks", "from_flows": []},
            {"name": "Inspect webhook payload", "resource": "webhook",
             "product_feature": "Webhooks", "from_flows": []},
        ],
    })
    reattrib = json.dumps({"map": {"accounts": "Webhooks", "auth": "Webhooks",
                                   "shared-ui": "Shared Platform"}})
    ufs_in = _ufs()
    for i, u in enumerate(ufs_in):
        u.resource = f"res{i}"                          # disarm the grain gate
    ufs, pfs, dm, tel = run_journey_abstraction(
        ufs_in, _pfs(), _devs(), routes, client=_client(abs_payload, reattrib))
    assert tel["applied"] is True
    survivors = [u for u in ufs if u.routes == ["/api/webhooks/{id}"]]
    assert len(survivors) == 1
    assert survivors[0].name == "Receive webhook"       # first in emission order
    assert tel["uf_rescued_routes"] == 1
    assert tel["uf_rescue_dropped_collisions"] == 2
    assert tel["uf_dropped_ungrounded"] == 2


def test_route_rescue_distinct_route_sets_no_collision() -> None:
    """Different route sets are NOT collisions — each empty journey keeps its
    own grounding."""
    routes = [{"pattern": "/api/webhooks/{id}", "method": "POST", "trigger": None},
              {"pattern": "/api/invoices", "method": "GET", "trigger": None}]
    abs_payload = json.dumps({
        "product_features": [{"name": "Ops", "description": "o"}],
        "user_flows": [
            {"name": "Receive webhook", "resource": "webhook",
             "product_feature": "Ops", "from_flows": []},
            {"name": "Browse invoices", "resource": "invoice",
             "product_feature": "Ops", "from_flows": []},
        ],
    })
    reattrib = json.dumps({"map": {"accounts": "Ops", "auth": "Ops",
                                   "shared-ui": "Shared Platform"}})
    ufs_in = _ufs()
    for i, u in enumerate(ufs_in):
        u.resource = f"res{i}"
    ufs, pfs, dm, tel = run_journey_abstraction(
        ufs_in, _pfs(), _devs(), routes, client=_client(abs_payload, reattrib))
    assert tel["applied"] is True
    assert tel["uf_rescued_routes"] == 2
    assert tel["uf_rescue_dropped_collisions"] == 0


def test_cache_hit_restores_contract_flag() -> None:
    """The contract status is persisted in the cache entry so a cache-hit
    replay reports the SAME abstraction_contract as the live run."""
    cache = _FakeCache()
    cli = _seq_client([_UNCOMPRESSED, _UNCOMPRESSED], _MAP_FULL)
    _u1, _p1, _m1, tel1 = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=cli, cache=cache)
    assert tel1["abstraction_contract"] == CONTRACT_UNCOMPRESSED
    # Second run: a client that WOULD return a compressed draw — but the
    # cache hit replays the recorded draw + its contract flag.
    _u2, _p2, _m2, tel2 = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_client(_ABS, _MAP_FULL), cache=cache)
    assert tel2["cache_hit"] is True
    assert tel2["llm_calls"] == 0
    assert tel2["abstraction_contract"] == CONTRACT_UNCOMPRESSED
    assert [u.name for u in _u2] == [u.name for u in _u1]


def test_cache_version_bumped_for_contract_fix() -> None:
    """Frozen pre-fix draws (cached under 'contract-3' and earlier) must be
    invalidated — the version participates in the cache key AND the
    entry-validity check. 'contract-4' = the jpf structural anchor (lever #3):
    contract-3 entries may hold two-axis-inflated draws the jpf prong retries."""
    assert ABSTRACTION_CACHE_VERSION == "contract-4"
