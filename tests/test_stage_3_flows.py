"""Tests for ``faultline.pipeline_v2.stage_3_flows``.

Verifies:

  - Features with <3 exports skip the LLM (no calls made).
  - Mocked LLM responses round-trip through the pipeline correctly.
  - Naming-discipline filter drops ``use-X`` and non-``-flow`` slugs.
  - LLM failures don't crash the orchestrator.
  - Cost tracking accumulates across parallel calls.
"""

from __future__ import annotations

import json
import textwrap
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from faultline.llm.cost import CostTracker
from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature
from faultline.pipeline_v2.stage_3_flows import (
    FlowSpec,
    Stage3Result,
    _enumerate_candidates,
    _parse_response_text,
    _validate_and_attach_lines,
    stage_3_flows,
)


# ── Fixtures ───────────────────────────────────────────────────────────────


def _ctx(tmp_path: Path, files: list[str]) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


def _feature(name: str, paths: tuple[str, ...]) -> DeveloperFeature:
    return DeveloperFeature(
        name=name,
        paths=paths,
        sources=["route"],
        confidence="medium",
    )


def _make_ts_file(tmp_path: Path, rel: str, body: str) -> None:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")


class _FakeAnthropic:
    """Records calls and returns canned responses.

    ``responses`` is a list of strings, returned in FIFO order. Once
    exhausted the client returns the last response repeatedly so tests
    can call N features with one canned reply.
    """

    def __init__(
        self,
        responses: list[str],
        *,
        in_tokens: int = 100,
        out_tokens: int = 50,
        raise_on_call: int | None = None,
    ) -> None:
        self.responses = responses
        self._idx = 0
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []
        self.in_tokens = in_tokens
        self.out_tokens = out_tokens
        self.raise_on_call = raise_on_call
        self._lock = threading.Lock()
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, parent: "_FakeAnthropic") -> None:
            self._p = parent

        def create(self, **kwargs: Any) -> Any:
            with self._p._lock:
                self._p.call_count += 1
                this_call = self._p.call_count
                self._p.calls.append(kwargs)
                if (
                    self._p.raise_on_call is not None
                    and this_call == self._p.raise_on_call
                ):
                    raise RuntimeError("simulated Anthropic outage")
                if self._p._idx < len(self._p.responses):
                    text = self._p.responses[self._p._idx]
                    self._p._idx += 1
                else:
                    text = (
                        self._p.responses[-1]
                        if self._p.responses
                        else '{"flows": []}'
                    )
            content = [types.SimpleNamespace(text=text)]
            usage = types.SimpleNamespace(
                input_tokens=self._p.in_tokens,
                output_tokens=self._p.out_tokens,
            )
            return types.SimpleNamespace(content=content, usage=usage)


# ── Unit-level: enumerate / parse / validate ───────────────────────────────


def test_enumerate_candidates_pulls_exports_and_lines(tmp_path: Path) -> None:
    """``_enumerate_candidates`` walks the feature's paths via
    ``extract_signatures`` and records each export's start line."""
    _make_ts_file(
        tmp_path, "app/billing/page.tsx",
        textwrap.dedent(
            """\
            export function BillingPage() { return null; }
            export const billingHelper = () => 1;
            export async function GET(req: Request) { return Response.json({}); }
            """
        ),
    )
    feature = _feature("billing", ("app/billing/page.tsx",))
    exports, routes, sym_loc = _enumerate_candidates(feature, str(tmp_path))
    assert "BillingPage" in exports
    assert "billingHelper" in exports
    # Next.js route handler counts as both an export and route.
    assert any("GET" in r or "billing" in r for r in routes)
    # Each export records a (file, start_line) tuple.
    assert sym_loc["BillingPage"][0] == "app/billing/page.tsx"
    assert sym_loc["BillingPage"][1] >= 1


def test_parse_response_handles_fenced_json() -> None:
    raw = "```json\n{\"flows\": [{\"name\": \"create-invoice-flow\"}]}\n```"
    flows = _parse_response_text(raw)
    assert flows == [{"name": "create-invoice-flow"}]


def test_parse_response_handles_prose_prefix() -> None:
    raw = 'Sure! Here is the JSON: {"flows": [{"name": "manage-team-flow"}]}'
    flows = _parse_response_text(raw)
    assert flows == [{"name": "manage-team-flow"}]


def test_parse_response_empty_on_garbage() -> None:
    assert _parse_response_text("definitely not json") == []
    assert _parse_response_text("") == []


def test_validate_drops_naming_violations() -> None:
    sym_loc = {"X": ("a.ts", 1), "Y": ("b.ts", 2)}
    raw = [
        {"name": "create-invoice-flow", "symbols": ["X"]},      # ok
        {"name": "use-billing-flow", "symbols": ["X"]},         # use- prefix
        {"name": "BillingFlow", "symbols": ["Y"]},              # not kebab
        {"name": "list-customers", "symbols": ["Y"]},           # missing -flow suffix
        {"name": "", "symbols": []},                            # empty
    ]
    valid, notes = _validate_and_attach_lines(raw, sym_loc)
    assert [f.name for f in valid] == ["create-invoice-flow"]
    assert valid[0].entry_point_file == "a.ts"
    assert valid[0].entry_point_line == 1
    assert len(notes) == 4


def test_validate_drops_unknown_symbols() -> None:
    sym_loc = {"X": ("a.ts", 1)}
    raw = [{"name": "do-x-flow", "symbols": ["X", "ZZZ_NOT_REAL"]}]
    valid, _notes = _validate_and_attach_lines(raw, sym_loc)
    assert valid[0].symbol_names == ["X"]  # ZZZ filtered out


# ── Orchestrator: features-with-flows happy path ──────────────────────────


def test_features_under_min_exports_skip_llm(tmp_path: Path) -> None:
    """A feature with only one exported symbol should NOT call the LLM
    and should default to ``flows=[]``."""
    _make_ts_file(
        tmp_path, "lib/utils.ts", "export const ONE = 1;\n",
    )
    feature = _feature("utils", ("lib/utils.ts",))
    ctx = _ctx(tmp_path, files=["lib/utils.ts"])
    client = _FakeAnthropic(responses=['{"flows": [{"name": "x-flow"}]}'])

    result = stage_3_flows([feature], ctx, client=client, max_workers=2)

    assert result.llm_calls == 0
    assert result.features_with_flows[0].flows == []
    assert client.call_count == 0


def test_happy_path_attaches_flows(tmp_path: Path) -> None:
    _make_ts_file(
        tmp_path, "app/billing/page.tsx",
        textwrap.dedent(
            """\
            export function BillingPage() {}
            export function CheckoutForm() {}
            export function SubscriptionTable() {}
            """
        ),
    )
    feature = _feature("billing", ("app/billing/page.tsx",))
    ctx = _ctx(tmp_path, files=["app/billing/page.tsx"])
    canned = json.dumps({
        "flows": [
            {"name": "view-billing-flow", "description": "View invoices",
             "symbols": ["BillingPage"]},
            {"name": "checkout-flow", "description": "Pay",
             "symbols": ["CheckoutForm"]},
        ],
    })
    client = _FakeAnthropic(responses=[canned])

    result = stage_3_flows([feature], ctx, client=client, max_workers=2)

    assert result.llm_calls == 1
    fwf = result.features_with_flows[0]
    assert [f.name for f in fwf.flows] == ["view-billing-flow", "checkout-flow"]
    assert fwf.flows[0].entry_point_file == "app/billing/page.tsx"
    assert result.cost_usd > 0


def test_parallel_execution_across_features(tmp_path: Path) -> None:
    """Two features → two Haiku calls; results bound to the right feature."""
    for slug, syms in [
        ("billing", ["A", "B", "C"]),
        ("auth", ["D", "E", "F"]),
    ]:
        body = "\n".join(f"export function {s}() {{}}" for s in syms)
        _make_ts_file(tmp_path, f"app/{slug}/page.tsx", body + "\n")

    features = [
        _feature("billing", ("app/billing/page.tsx",)),
        _feature("auth", ("app/auth/page.tsx",)),
    ]
    ctx = _ctx(
        tmp_path,
        files=["app/billing/page.tsx", "app/auth/page.tsx"],
    )
    responses = [
        json.dumps({"flows": [{"name": "pay-now-flow", "symbols": ["A"]}]}),
        json.dumps({"flows": [{"name": "sign-in-flow", "symbols": ["D"]}]}),
    ]
    client = _FakeAnthropic(responses=responses)

    result = stage_3_flows(features, ctx, client=client, max_workers=2)

    assert result.llm_calls == 2
    assert client.call_count == 2
    # Bind results back to the feature names — order matches input order.
    names = {fwf.feature.name: [f.name for f in fwf.flows]
             for fwf in result.features_with_flows}
    # Each feature received exactly one flow (matched to its own symbols).
    for slug in ("billing", "auth"):
        assert len(names[slug]) == 1
        assert names[slug][0].endswith("-flow")


def test_llm_failure_does_not_crash(tmp_path: Path) -> None:
    """A raised exception from the Anthropic client must be swallowed
    and the feature defaults to ``flows=[]``."""
    _make_ts_file(
        tmp_path, "app/x/page.tsx",
        "export function A() {}\nexport function B() {}\nexport function C() {}\n",
    )
    feature = _feature("x", ("app/x/page.tsx",))
    ctx = _ctx(tmp_path, files=["app/x/page.tsx"])
    client = _FakeAnthropic(
        responses=['{"flows": []}'],
        raise_on_call=1,
    )

    result = stage_3_flows([feature], ctx, client=client, max_workers=2)

    assert isinstance(result, Stage3Result)
    assert result.features_with_flows[0].flows == []
    assert result.llm_calls == 1  # we still record the attempt


def test_no_client_yields_empty_flows_with_warning(tmp_path: Path) -> None:
    _make_ts_file(
        tmp_path, "app/x/page.tsx",
        "export function A() {}\nexport function B() {}\nexport function C() {}\n",
    )
    feature = _feature("x", ("app/x/page.tsx",))
    ctx = _ctx(tmp_path, files=["app/x/page.tsx"])
    result = stage_3_flows(
        [feature], ctx, client=None,
        _client_factory=lambda: None,
        max_workers=2,
    )
    assert result.llm_calls == 0
    assert result.features_with_flows[0].flows == []
    assert any("no Anthropic client" in w for w in result.warnings)


def test_cost_tracker_accumulates(tmp_path: Path) -> None:
    for slug in ("a", "b", "c"):
        _make_ts_file(
            tmp_path, f"app/{slug}/page.tsx",
            "export function X() {}\nexport function Y() {}\nexport function Z() {}\n",
        )
    features = [
        _feature(slug, (f"app/{slug}/page.tsx",))
        for slug in ("a", "b", "c")
    ]
    ctx = _ctx(
        tmp_path,
        files=[f"app/{slug}/page.tsx" for slug in ("a", "b", "c")],
    )
    canned = json.dumps({"flows": [{"name": "view-flow", "symbols": ["X"]}]})
    client = _FakeAnthropic(
        responses=[canned], in_tokens=1000, out_tokens=500,
    )
    tracker = CostTracker(max_cost=None)
    result = stage_3_flows(
        features, ctx, client=client, cost_tracker=tracker, max_workers=2,
    )
    assert result.llm_calls == 3
    # Each call: 1000 in + 500 out at Haiku 4.5 pricing → > 0.
    assert tracker.total_cost_usd > 0
    assert tracker.call_count == 3


def test_empty_feature_list_returns_empty_result(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, files=[])
    result = stage_3_flows(
        [], ctx, client=_FakeAnthropic(responses=[]),
    )
    assert result.features_with_flows == []
    assert result.llm_calls == 0
