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
    is_enabled,
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


# ── Phase 2 — anchor alignment, structured output, content-hash cache ───────

from dataclasses import dataclass as _dc

from faultline.cache import MemoryCacheBackend


@_dc
class _Anchor:
    text: str
    source: str
    locator: str = "x"


def _anchors() -> list[_Anchor]:
    return [
        _Anchor("Account Management", "nav"),
        _Anchor("Authentication", "i18n"),
        _Anchor("account management", "test"),  # case-dup of #1 → deduped
    ]


# Alignment abstraction payload: names drawn from anchors + one from_code_only.
_ALIGN_ABS = json.dumps({
    "product_features": [
        {"name": "Account Management", "description": "manage accounts",
         "from_code_only": False},
        {"name": "Authentication", "description": "sign in/up",
         "from_code_only": False},
        {"name": "Realtime Sync", "description": "code-only capability anchors missed",
         "from_code_only": True},
    ],
    "user_flows": [
        {"name": "Manage accounts", "resource": "account",
         "product_feature": "Account Management", "from_flows": ["UF-001", "UF-002"],
         "from_code_only": False},
        {"name": "Sign in", "resource": "session",
         "product_feature": "Authentication", "from_flows": ["UF-003"],
         "from_code_only": False},
        {"name": "Sync in realtime", "resource": "socket",
         "product_feature": "Realtime Sync", "from_flows": [],
         "from_code_only": True},
    ],
})
# shared-ui maps to the code-only capability so that PF has contributing dev
# evidence (a product feature with NO attributed dev features is correctly
# dropped by reconstruction — aggregations need members).
_ALIGN_MAP = json.dumps({"map": {
    "accounts": "Account Management", "auth": "Authentication",
    "shared-ui": "Realtime Sync",
}})


def _tool_client(abstraction_obj: dict, reattrib_obj: dict) -> Any:
    """Fake client that returns FORCED tool-use blocks (the structured path),
    routed by tool name (abstraction vs re-attribution)."""
    @dataclass
    class _ToolBlock:
        type: str
        name: str
        input: dict

    class _ToolMsg:
        def __init__(self, name: str, payload: dict) -> None:
            self.content = [_ToolBlock(type="tool_use", name=name, input=payload)]
            self.usage = _Usage(400, 200)

    class _C:
        class _M:
            def create(self, **kw: Any) -> Any:
                tools = kw.get("tools") or []
                name = tools[0]["name"] if tools else ""
                if name == "emit_dev_capability_map":
                    return _ToolMsg(name, reattrib_obj)
                return _ToolMsg(name, abstraction_obj)
        messages = _M()
    return _C()


def test_anchors_passed_output_names_drawn_from_anchors() -> None:
    ufs, pfs, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [],
        product_anchors=_anchors(), client=_client(_ALIGN_ABS, _ALIGN_MAP))
    assert tel["applied"] is True
    assert tel["aligned"] is True
    assert tel["anchor_count"] == 2  # the case-dup collapsed
    pf_names = {p.display_name for p in pfs}
    # the two anchor capabilities survive as canonical product-feature names
    assert {"Account Management", "Authentication"} <= pf_names


def test_code_only_capability_flagged() -> None:
    ufs, pfs, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [],
        product_anchors=_anchors(), client=_client(_ALIGN_ABS, _ALIGN_MAP))
    rt = next(p for p in pfs if p.display_name == "Realtime Sync")
    assert rt.from_code_only is True
    acct = next(p for p in pfs if p.display_name == "Account Management")
    assert acct.from_code_only is False
    sync_uf = next(u for u in ufs if u.name == "Sync in realtime")
    assert sync_uf.from_code_only is True
    assert tel["from_code_only_pf"] == 1
    assert tel["from_code_only_uf"] == 1


def test_structured_tool_use_path_parses() -> None:
    """A client returning tool_use blocks (forced structured output) is parsed
    via block.input — telemetry records the 'tool' path."""
    abs_obj = json.loads(_ABS)
    map_obj = json.loads(_MAP_FULL)
    ufs, pfs, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_tool_client(abs_obj, map_obj))
    assert tel["applied"] is True
    assert tel["structured_path"] == "tool"
    assert {u.name for u in ufs} == {"Manage accounts", "Sign in"}


def test_text_fallback_when_tools_unsupported() -> None:
    """A client whose create() raises when given the tools kwarg must fall back
    to a plain text call + regex parse (graceful degrade of the SDK path)."""
    class _NoToolsClient:
        class _M:
            def create(self, **kw: Any) -> Any:
                if "tools" in kw:
                    raise TypeError("tools not supported by this endpoint")
                sysp = kw.get("system", "")
                return _Msg(_MAP_FULL if "assign each developer feature" in sysp else _ABS)
        messages = _M()
    ufs, pfs, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], client=_NoToolsClient())
    assert tel["applied"] is True
    assert tel["structured_path"] == "text_fallback"
    assert {u.name for u in ufs} == {"Manage accounts", "Sign in"}


def test_cache_hit_returns_identical_output_and_skips_llm() -> None:
    """Second run with the same (digest+anchors+model) must hit the cache,
    fire ZERO LLM calls, and return byte-identical output."""
    cache = MemoryCacheBackend()

    # Count create() calls so we can prove the 2nd run never touches the LLM.
    class _CountingClient:
        calls = 0
        class _M:
            def create(self_inner, **kw: Any) -> Any:
                _CountingClient.calls += 1
                sysp = kw.get("system", "")
                return _Msg(_ALIGN_MAP if "assign each developer feature" in sysp else _ALIGN_ABS)
        messages = _M()

    c = _CountingClient()
    # Reuse the SAME deterministic inputs across both runs — in a real re-scan
    # the developer features are byte-identical; here _devs() would otherwise
    # differ only by its now()-stamped last_modified, which is not what the
    # cache guards (it guards the LLM answers; reconstruction is deterministic).
    devs, ufs_in, pfs_in = _devs(), _ufs(), _pfs()
    ufs1, pfs1, map1, tel1 = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], product_anchors=_anchors(), client=c, cache=cache)
    assert tel1["applied"] is True and tel1["cache_hit"] is False
    calls_after_first = _CountingClient.calls
    assert calls_after_first == 2  # abstraction + re-attribution

    ufs2, pfs2, map2, tel2 = run_journey_abstraction(
        ufs_in, pfs_in, devs, [], product_anchors=_anchors(), client=c, cache=cache)
    assert tel2["cache_hit"] is True
    assert tel2["llm_calls"] == 0
    assert _CountingClient.calls == calls_after_first  # no new LLM calls

    # Byte-identical output (compare the serialised model dumps).
    assert [u.model_dump() for u in ufs1] == [u.model_dump() for u in ufs2]
    assert [p.model_dump() for p in pfs1] == [p.model_dump() for p in pfs2]
    assert map1 == map2


def test_align_prompt_instructs_verbatim_anchor_naming() -> None:
    """The ALIGN prompt must instruct the model to reuse anchor text VERBATIM
    (stability + product-grain fidelity), not paraphrase it."""
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import _ALIGN_SYSTEM

    low = _ALIGN_SYSTEM.lower()
    assert "verbatim" in low
    assert "do not reword" in low  # explicit no-paraphrase instruction
    # Applies to BOTH product_features (rule 1) and user_flows (rule 2).
    assert low.count("verbatim") >= 2


def test_free_generation_fallback_when_no_anchors() -> None:
    """No anchors → free-generation path (aligned=False), behaves as before."""
    _u, _p, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [], product_anchors=None,
        client=_client(_ABS, _MAP_FULL))
    assert tel["applied"] is True
    assert tel["aligned"] is False
    assert tel["anchor_count"] == 0
