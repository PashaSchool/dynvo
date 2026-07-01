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
             "product_feature": "Account Management", "from_flows": []},
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
