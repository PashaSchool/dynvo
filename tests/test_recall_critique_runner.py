"""Tests for the Phase 5 wiring layer.

Covers the env-var gate, the Anthropic adapter (with a stub SDK),
extractor discovery on fixture repos, and the run_recall_critique
opportunistic-failure paths. No live API access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from faultline.llm import recall_critique_runner as rc


@dataclass
class _FakeResult:
    """Minimal DeepScanResult-shaped stub."""
    features: dict = field(default_factory=dict)
    descriptions: dict = field(default_factory=dict)


@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 30


@dataclass
class _FakeContentBlock:
    text: str


@dataclass
class _FakeResponse:
    content: list
    usage: _FakeUsage = field(default_factory=_FakeUsage)
    stop_reason: str = "end_turn"


@dataclass
class _FakeMessages:
    response_text: str
    captured: dict = field(default_factory=dict)

    def create(self, *, model, max_tokens, system, messages):
        self.captured.update(
            model=model, max_tokens=max_tokens,
            system=system, messages=messages,
        )
        return _FakeResponse(content=[_FakeContentBlock(text=self.response_text)])


@dataclass
class _FakeAnthropicClient:
    response_text: str = "{}"

    def __post_init__(self):
        self.messages = _FakeMessages(response_text=self.response_text)


@dataclass
class _FakeTracker:
    records: list = field(default_factory=list)

    def record(self, **kw):
        self.records.append(kw)


# ── is_enabled ────────────────────────────────────────────────────────


def test_is_enabled_off_by_default(monkeypatch):
    monkeypatch.delenv("FAULTLINE_CRITIQUE_RECALL", raising=False)
    assert rc.is_enabled() is False


@pytest.mark.parametrize("v", ["1", "true", "yes", "on", "TRUE"])
def test_is_enabled_truthy(monkeypatch, v):
    monkeypatch.setenv("FAULTLINE_CRITIQUE_RECALL", v)
    assert rc.is_enabled() is True


def test_is_enabled_other_values_false(monkeypatch):
    for v in ["0", "false", "no", "off", ""]:
        monkeypatch.setenv("FAULTLINE_CRITIQUE_RECALL", v)
        assert rc.is_enabled() is False, v


# ── _AnthropicLlmClient adapter ───────────────────────────────────────


def test_adapter_passes_through_text_and_records_cost():
    sdk = _FakeAnthropicClient(response_text="hello")
    tracker = _FakeTracker()
    adapter = rc._AnthropicLlmClient(
        client=sdk, model="claude-haiku-4-5", tracker=tracker,
    )

    resp = adapter.complete(system="sys", user="usr", max_tokens=512)
    assert resp.text == "hello"
    assert resp.input_tokens == 100
    assert resp.output_tokens == 30
    assert sdk.messages.captured["model"] == "claude-haiku-4-5"
    assert sdk.messages.captured["max_tokens"] == 512
    assert sdk.messages.captured["messages"][0]["content"] == "usr"
    assert len(tracker.records) == 1
    assert tracker.records[0]["label"] == "critique-recall"


def test_adapter_works_without_tracker():
    sdk = _FakeAnthropicClient(response_text="x")
    adapter = rc._AnthropicLlmClient(
        client=sdk, model="claude-haiku-4-5", tracker=None,
    )
    resp = adapter.complete(system="s", user="u", max_tokens=64)
    assert resp.text == "x"


# ── gather_signals ────────────────────────────────────────────────────


def test_gather_signals_on_empty_repo_returns_empty(tmp_path):
    # Empty dir: no manifests, no schemas, no rails, no next.
    sigs = rc.gather_signals(tmp_path)
    assert sigs == []


def test_gather_signals_on_pkgjson_with_anchor(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x", "version": "0.1.0",
        "dependencies": {"stripe": "^14.0.0"},
    }))
    sigs = rc.gather_signals(tmp_path)
    kinds = {s.kind for s in sigs}
    assert "expected-feature" in kinds


def test_gather_signals_uses_existing_rails_fixture():
    fixture = Path(__file__).parent / "fixtures" / "tiny_rails"
    if not fixture.exists():
        pytest.skip("tiny_rails fixture not present")
    sigs = rc.gather_signals(fixture)
    # The fixture is a rails app — should at least emit some controller
    # actions.
    kinds = {s.kind for s in sigs}
    assert "controller-action" in kinds


# ── run_recall_critique ───────────────────────────────────────────────


def test_run_skipped_when_repo_root_none():
    result = _FakeResult()
    out = rc.run_recall_critique(
        result=result, repo_root=None, api_key="k",
    )
    assert out is result


def test_run_skipped_when_no_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x", "dependencies": {"stripe": "*"},
    }))
    result = _FakeResult()
    out = rc.run_recall_critique(
        result=result, repo_root=tmp_path, api_key=None,
    )
    assert out is result
    assert result.features == {}


def test_run_skipped_when_no_signals(tmp_path):
    # Empty repo → no extractor emits → early exit before LLM call.
    result = _FakeResult()
    out = rc.run_recall_critique(
        result=result, repo_root=tmp_path, api_key="key",
    )
    assert out is result


def test_run_end_to_end_with_stub_anthropic(monkeypatch, tmp_path):
    """Wire fake anthropic SDK in; verify findings flow to result."""
    # Stripe dep → expected category "Billing", not in detected.
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x",
        "dependencies": {"stripe": "*"},
    }))

    raw = json.dumps({"missed": [
        {"feature_name": "Billing & Subscriptions",
         "matched_category": "billing",
         "files": ["package.json"],
         "rationale": "Stripe dependency with no detected billing feature."},
    ]})

    fake_client = _FakeAnthropicClient(response_text=raw)
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key: fake_client)

    result = _FakeResult(features={"Dashboard": ["src/page.tsx"]})
    out = rc.run_recall_critique(
        result=result,
        repo_root=tmp_path,
        api_key="dummy-key",
        tracker=None,
    )
    assert out is result
    assert "Billing & Subscriptions" in result.features
    assert result.features["Billing & Subscriptions"] == ["package.json"]
    assert result.descriptions["Billing & Subscriptions"].startswith("[critique]")
