"""B79 Seg A — robust truncated-response salvage for Stage 6.7d Call-1.

Diagnosis (VERIFIED, 2026-07-22): on cal-com the Call-1 abstraction response is
truncated at the ``ABSTRACTION_MAX_TOKENS`` (16000) output ceiling. The decision
log records ``output_tokens == 16000`` EXACTLY (in=32330), byte-cost-identical
across two fresh runs (033020Z & 041241Z, cost $0.33699) — a deterministic tail
truncation, not a mid-stream malformation. The unterminated JSON makes
``_parse_json`` return None, so the WHOLE journey layer degrades (applied=False;
uf/pf frozen at the raw rollup 365/194). Because ``cost_usd*2`` ($0.674) exceeds
``COST_CAP_USD`` ($0.60) the retry rung is skipped (llm_calls=1) — the very first
truncated draw must be salvaged in place.

The byte-exact real response was never persisted (the cache write is gated on a
successful parse), so the truncated fixtures below reproduce the exact
STRUCTURAL signature measured from the decision log: product_features FIRST +
user_flows SECOND (the prompt's required order), with a token-ceiling cut mid
user_flows object. Every product_feature and a complete prefix of user_flows
survive; the incomplete tail object is dropped.

Flag ``FAULTLINE_67D_ROBUST_PARSE`` default OFF ⇒ the flagless engine is
byte-identical (salvage branch + telemetry never run).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2 import degradations as deg
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _parse_json,
    _salvage_array,
    _salvage_truncated_json,
    _scan_object,
    robust_parse_enabled,
    run_journey_abstraction,
)


# ── fixtures: a real-shaped abstraction payload (PF first, UF second) ────────

def _full_abstraction(n_pf: int = 30, n_uf: int = 90) -> dict[str, Any]:
    pfs = [{"name": f"Capability {i:02d}", "description": f"capability {i}"}
           for i in range(1, n_pf + 1)]
    ufs = [{"name": f"Journey {i:02d}", "resource": f"res{i}",
            "product_feature": f"Capability {((i - 1) % n_pf) + 1:02d}",
            "from_flows": [f"UF-{i:03d}"], "from_dev_features": [f"dev{i}"]}
           for i in range(1, n_uf + 1)]
    return {"product_features": pfs, "user_flows": ufs}


def _truncate_mid_uf(obj: dict[str, Any], keep_uf: int,
                     marker: str | None = None) -> str:
    """Serialise ``obj`` (PF first, UF second) and cut INSIDE the
    ``keep_uf+1``-th user_flow's name value — so exactly ``keep_uf`` complete
    user_flows and ALL product_features survive, mirroring a token-ceiling cut.
    ``marker`` overrides the cut point (the name of the first dropped UF)."""
    text = json.dumps(obj)
    mark = marker if marker is not None else f"Journey {keep_uf + 1:02d}"
    cut = text.index(mark) + 4  # lands inside the (keep_uf+1)-th name string
    return text[:cut]


# ── pure salvage function: named exhibit + anti-cases ────────────────────────

def test_salvage_truncated_recovers_prefix_drops_tail() -> None:
    """EXHIBIT (cal-com signature): a truncated draw salvages N-of-M — every PF
    + the complete user_flows prefix — and drops the incomplete tail object."""
    full = _full_abstraction(n_pf=30, n_uf=90)
    trunc = _truncate_mid_uf(full, keep_uf=63)
    # The flagless parser loses the whole thing (this is the bug):
    assert _parse_json(trunc) is None

    salvaged, meta = _salvage_truncated_json(trunc)
    assert salvaged is not None
    assert meta["uf_salvaged"] == 63          # N of M=90 journeys
    assert meta["pf_salvaged"] == 30          # ALL capabilities survive (emitted first)
    assert meta["uf_dropped_tail"] is True    # the 64th (partial) journey dropped
    assert meta["pf_dropped_tail"] is False   # the PF array closed cleanly
    # salvaged objects are intact + parseable, in order, tail excluded
    names = [u["name"] for u in salvaged["user_flows"]]
    assert names[0] == "Journey 01" and names[-1] == "Journey 63"
    assert "Journey 64" not in names
    # every salvaged journey's product_feature cites a SURVIVING capability
    pf_names = {p["name"] for p in salvaged["product_features"]}
    assert all(u["product_feature"] in pf_names for u in salvaged["user_flows"])


def test_salvage_skips_complete_valid_identical_path() -> None:
    """ANTI-CASE (full valid → identical byte path): a complete response is NOT
    a truncation — salvage returns None so the flag-ON parse is the flagless
    parse. The engine's parse result is identical with the flag on or off."""
    full = _full_abstraction()
    text = json.dumps(full)
    salvaged, meta = _salvage_truncated_json(text)
    assert salvaged is None and meta == {}
    # the real parser handles it unchanged (salvage never engaged)
    parsed = _parse_json(text)
    assert parsed is not None
    assert len(parsed["user_flows"]) == 90 and len(parsed["product_features"]) == 30


def test_salvage_rejects_complete_malformed_honest_fail() -> None:
    """ANTI-CASE (malformed in the middle → honest fail as before): a
    COMPLETE-but-malformed response (balanced outer braces, invalid ``None``
    token inside) is NOT a truncation. Salvage must decline it so the stage
    fails honestly, exactly like the flagless engine."""
    # balanced outer braces, but a bare `None` (invalid JSON) mid-document
    malformed = ('{"product_features": [{"name": "A", "description": None}], '
                 '"user_flows": [{"name": "J1", "resource": "r"}]}')
    assert _parse_json(malformed) is None          # flagless: honest fail
    salvaged, meta = _salvage_truncated_json(malformed)
    assert salvaged is None and meta == {}          # salvage declines → still honest fail


def test_salvage_none_when_nothing_survives() -> None:
    """ANTI-CASE: truncated so early that neither array has a complete object →
    nothing usable → None (never fabricate a degenerate result)."""
    # cut inside the FIRST product_feature object, before any complete member
    text = json.dumps(_full_abstraction())
    cut = text.index("Capability 01") + 4
    salvaged, meta = _salvage_truncated_json(text[:cut])
    assert salvaged is None
    assert meta["uf_salvaged"] == 0 and meta["pf_salvaged"] == 0


def test_scan_object_honours_braces_and_escapes_in_strings() -> None:
    """A ``{`` / ``}`` / escaped-quote inside a string value must not fool the
    brace scanner (a naive depth counter would mis-close)."""
    s = '{"name": "a {nested} \\" brace", "x": 1} tail'
    obj_str, end = _scan_object(s, 0)
    assert obj_str is not None
    assert json.loads(obj_str) == {"name": 'a {nested} " brace', "x": 1}
    assert s[end:] == " tail"
    # truncated object → (None, len)
    obj_str2, end2 = _scan_object('{"name": "unclosed', 0)
    assert obj_str2 is None and end2 == len('{"name": "unclosed')


def test_salvage_array_clean_close_reports_no_tail() -> None:
    """A cleanly-closed array (found before the cut) reports dropped_tail=False;
    a never-closing array reports True."""
    complete = '{"product_features": [{"name": "A"}, {"name": "B"}], "user_flows": [{"name":'
    pfs, pf_tail = _salvage_array(complete, "product_features")
    assert [p["name"] for p in pfs] == ["A", "B"] and pf_tail is False
    ufs, uf_tail = _salvage_array(complete, "user_flows")
    assert ufs == [] and uf_tail is True
    # missing key → empty, no tail
    assert _salvage_array(complete, "nonexistent") == ([], False)


def test_flag_default_off_and_env_parsing(monkeypatch) -> None:
    monkeypatch.delenv("FAULTLINE_67D_ROBUST_PARSE", raising=False)
    assert robust_parse_enabled() is False
    for off in ("0", "false", "False", "off", "no", ""):
        monkeypatch.setenv("FAULTLINE_67D_ROBUST_PARSE", off)
        assert robust_parse_enabled() is False
    for on in ("1", "true", "yes", "on"):
        monkeypatch.setenv("FAULTLINE_67D_ROBUST_PARSE", on)
        assert robust_parse_enabled() is True


# ── end-to-end via the mocked-client harness (full _draw integration) ────────

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
                key = "assign each developer feature"
                return _Msg(reattrib if key in sysp else abstraction)
        messages = _M()
    return _C()


def _feat(name: str, paths: list[str]) -> Feature:
    from faultline.models.types import MemberFile
    mf = [MemberFile(path=p, role="anchor", confidence=1.0) for p in paths]
    return Feature(
        name=name, display_name=name, description=f"{name} module",
        paths=paths, authors=["a"], total_commits=3, bug_fixes=1,
        bug_fix_ratio=0.33, last_modified=datetime.now(timezone.utc),
        health_score=90.0, layer="developer", member_files=mf,
    )


def _uf(uf_id: str, name: str, members: list[str]) -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, intent="author", resource="thing",
        member_flow_ids=members, member_count=len(members), routes=[f"/{name}"],
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


# A truncated abstraction payload whose slugs match _devs() so re-attribution
# and reconstruction land (PF first, UF second, cut mid-3rd-UF).
_ABS_FULL = {
    "product_features": [
        {"name": "Account Management", "description": "manage accounts"},
        {"name": "Authentication", "description": "sign in/up"},
    ],
    "user_flows": [
        {"name": "Manage accounts", "resource": "account",
         "product_feature": "Account Management", "from_flows": ["UF-001", "UF-002"]},
        {"name": "Sign in", "resource": "session",
         "product_feature": "Authentication", "from_flows": ["UF-003"]},
        {"name": "Reset password", "resource": "session",
         "product_feature": "Authentication", "from_flows": ["UF-003"]},
    ],
}
# keep 2 UFs, drop the 3rd ("Reset password") tail
_ABS_TRUNC = _truncate_mid_uf(_ABS_FULL, keep_uf=2, marker="Reset password")
_MAP_FULL = json.dumps({"map": {
    "accounts": "Account Management", "auth": "Authentication",
    "shared-ui": "Shared Platform",
}})


def test_e2e_flag_off_truncated_degrades_honestly(monkeypatch) -> None:
    """Flag OFF + truncated Call-1 → the flagless degrade path: applied=False,
    degraded_reason='abstraction_parse_failed', NO salvage telemetry keys."""
    monkeypatch.delenv("FAULTLINE_67D_ROBUST_PARSE", raising=False)
    _u, _p, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [],
        client=_client(_ABS_TRUNC, _MAP_FULL))
    assert tel["applied"] is False
    assert tel["degraded_reason"] == "abstraction_parse_failed"
    assert "abstraction_salvaged" not in tel
    assert "salvaged_uf_n" not in tel


def test_e2e_flag_on_truncated_salvages_partial(monkeypatch) -> None:
    """Flag ON + truncated Call-1 → salvage the prefix and APPLY: applied=True,
    abstraction_salvaged=True, counts recorded, tail dropped."""
    monkeypatch.setenv("FAULTLINE_67D_ROBUST_PARSE", "1")
    ufs, pfs, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [],
        client=_client(_ABS_TRUNC, _MAP_FULL))
    assert tel["applied"] is True
    assert tel["abstraction_salvaged"] is True
    assert tel["salvaged_uf_n"] == 2          # 2 complete journeys salvaged
    assert tel["salvaged_pf_n"] == 2          # both capabilities survived
    assert tel["salvaged_dropped_tail"] is True
    # the abstraction actually landed at journey grain (the salvaged names)
    out_names = {u.name for u in ufs}
    assert "Manage accounts" in out_names and "Sign in" in out_names


def test_e2e_flag_on_valid_adds_no_salvage_keys(monkeypatch) -> None:
    """Flag ON + a COMPLETE valid Call-1 → identical to flag-off: applied=True
    and NO salvage telemetry keys (inertness — the KS byte-identity guarantee on
    non-truncating repos)."""
    monkeypatch.setenv("FAULTLINE_67D_ROBUST_PARSE", "1")
    _u, _p, _m, tel = run_journey_abstraction(
        _ufs(), _pfs(), _devs(), [],
        client=_client(json.dumps(_ABS_FULL), _MAP_FULL))
    assert tel["applied"] is True
    assert "abstraction_salvaged" not in tel
    assert "salvaged_uf_n" not in tel and "salvaged_dropped_tail" not in tel


# ── degradation honesty: salvage stamps severity=partial (not silent) ────────

def _s67d(**over: Any) -> dict[str, Any]:
    base = {"enabled": True, "applied": True, "abstraction_salvaged": True,
            "salvaged_uf_n": 63, "salvaged_pf_n": 30,
            "salvaged_dropped_tail": True, "cost_usd": 0.33699}
    base.update(over)
    return base


def test_classify_partial_fires_on_salvaged_draw() -> None:
    rec = deg.classify_journey_abstraction_partial(_s67d())
    assert rec is not None
    assert rec["type"] == deg.TYPE_JOURNEY_ABSTRACTION_PARTIAL
    assert rec["severity"] == deg.SEVERITY_PARTIAL       # NOT silent, NOT failed
    assert rec["metrics"]["uf_salvaged"] == 63
    assert rec["metrics"]["pf_salvaged"] == 30
    assert rec["metrics"]["dropped_tail"] is True
    assert set(rec) == {"type", "stage", "severity", "detail", "metrics"}


def test_classify_partial_none_when_flag_off_or_absent() -> None:
    # no salvage key (flag off / non-truncating) → byte-identical, no stamp
    assert deg.classify_journey_abstraction_partial(
        {"enabled": True, "applied": True}) is None
    assert deg.classify_journey_abstraction_partial({}) is None
    assert deg.classify_journey_abstraction_partial(
        {"enabled": False}) is None


def test_partial_and_fatal_are_mutually_exclusive() -> None:
    """A salvaged (applied=True) run stamps PARTIAL, never FAILED; a non-applied
    truncated run stamps FAILED, never PARTIAL. The two classifiers never both
    fire on the same block."""
    salvaged = _s67d()
    assert deg.classify_journey_abstraction_partial(salvaged) is not None
    assert deg.classify_journey_abstraction_degradation(salvaged) is None

    failed = {"enabled": True, "applied": False,
              "degraded_reason": "abstraction_parse_failed", "cost_usd": 0.337}
    assert deg.classify_journey_abstraction_partial(failed) is None
    fatal = deg.classify_journey_abstraction_degradation(failed)
    assert fatal is not None and fatal["severity"] == deg.SEVERITY_FAILED


def test_detect_finalize_emits_partial_for_salvaged() -> None:
    out = deg.detect_finalize_degradations(journey_abstraction=_s67d())
    kinds = {(d["type"], d["severity"]) for d in out}
    assert (deg.TYPE_JOURNEY_ABSTRACTION_PARTIAL, deg.SEVERITY_PARTIAL) in kinds
    # and nothing FATAL for the same (applied=True) block
    assert deg.TYPE_JOURNEY_ABSTRACTION_FAILED not in {d["type"] for d in out}


def test_flag_registered_in_env_output_flags() -> None:
    """Cache-key correctness: the flag is registered so a flip is cache-keyed
    (append-only, no KEY_SCHEMA bump)."""
    from faultline.pipeline_v2 import scan_result_cache as src
    assert "FAULTLINE_67D_ROBUST_PARSE" in src.ENV_OUTPUT_FLAGS
