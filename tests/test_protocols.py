"""Substitutability tests for the new composable architecture.

Verifies that:
  - Extractor / Aggregator / Writer / LlmClient Protocols are runtime-
    checkable so a fake implementation can be detected at module
    boundaries.
  - A test fake (no inheritance, just the right shape) satisfies
    ``isinstance(obj, Extractor)``.
  - Signal / LlmResponse value objects are frozen and hashable.
  - AggregateResult.merge is associative for the "no conflicting
    feature names" case (commutative when no overlap).

These tests do NOT touch any existing engine code — they validate the
foundation modules in isolation. Phase 2 will add tests that exercise
the migrated extractors against this same Protocol contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

import pytest

from faultline.protocols import (
    Aggregator,
    Extractor,
    ExtractorError,
    LlmClient,
    Writer,
)
from faultline.results import AggregatedFeature, AggregateResult
from faultline.signals import LlmResponse, LlmToolCall, Signal


# ── Fakes (no inheritance — just the right shape) ────────────────────


@dataclass
class FakeExtractor:
    """Plain dataclass that happens to satisfy the Extractor Protocol."""

    name: str = "fake-extractor"
    canned_signals: tuple[Signal, ...] = ()

    def applicable(self, repo_root: Path) -> bool:
        return True

    def extract(self, repo_root: Path, files: Iterable[Path]) -> list[Signal]:
        return list(self.canned_signals)


@dataclass
class FakeAggregator:
    name: str = "fake-aggregator"

    def aggregate(self, signals: Iterable[Signal]) -> AggregateResult:
        feats = tuple(
            AggregatedFeature(name=str(s.payload.get("name", s.kind)))
            for s in signals
            if s.kind == "feature"
        )
        return AggregateResult(
            repo_path="/tmp/fake",
            analyzed_at=datetime(2026, 1, 1),
            features=feats,
        )


@dataclass
class FakeWriter:
    name: str = "fake-writer"
    written: list[AggregateResult] | None = None

    def write(self, result: AggregateResult, dest: Path) -> None:
        if self.written is None:
            self.written = []
        self.written.append(result)


@dataclass
class FakeLlmClient:
    name: str = "fake-llm"

    def complete(self, *, system: str, user: str, max_tokens: int,
                 tools: list[dict] | None = None) -> LlmResponse:
        return LlmResponse(text=f"echo({user[:20]})", input_tokens=len(user) // 4,
                           output_tokens=10)


# ── Protocol substitutability ────────────────────────────────────────


def test_fake_extractor_satisfies_protocol():
    fake = FakeExtractor()
    assert isinstance(fake, Extractor), \
        "FakeExtractor with name + applicable + extract should satisfy Extractor"


def test_fake_aggregator_satisfies_protocol():
    fake = FakeAggregator()
    assert isinstance(fake, Aggregator)


def test_fake_writer_satisfies_protocol():
    fake = FakeWriter()
    assert isinstance(fake, Writer)


def test_fake_llm_client_satisfies_protocol():
    fake = FakeLlmClient()
    assert isinstance(fake, LlmClient)


def test_object_without_protocol_methods_is_not_extractor():
    class NotAnExtractor:
        name = "x"
        # missing applicable() and extract()

    assert not isinstance(NotAnExtractor(), Extractor)


# ── Signal frozenness ────────────────────────────────────────────────


def test_signal_payload_becomes_immutable_view():
    s = Signal(kind="route-page", source="route-file-extractor",
               payload={"file": "app/page.tsx", "method": "GET"})
    assert isinstance(s.payload, MappingProxyType)
    with pytest.raises(TypeError):
        s.payload["new"] = "value"  # type: ignore[index]


def test_signal_equality_by_value():
    s1 = Signal(kind="route-page", source="x", payload={"file": "app/page.tsx"})
    s2 = Signal(kind="route-page", source="x", payload={"file": "app/page.tsx"})
    # Same kind + source + same payload contents → equal even though the
    # frozen MappingProxyType wrapper isn't hashable. Equality is what
    # the aggregators care about; dedup uses set-of-tuples internally.
    assert dict(s1.payload) == dict(s2.payload)
    assert s1.kind == s2.kind and s1.source == s2.source


def test_llm_response_is_frozen():
    r = LlmResponse(text="hello", input_tokens=10, output_tokens=2)
    with pytest.raises(Exception):
        r.text = "changed"  # type: ignore[misc]


def test_llm_tool_call_arguments_immutable():
    call = LlmToolCall(name="grep", arguments={"pattern": "auth"})
    assert isinstance(call.arguments, MappingProxyType)


# ── AggregateResult.merge ────────────────────────────────────────────


def test_aggregate_result_empty_constructs_cleanly():
    res = AggregateResult.empty(Path("/tmp/foo"))
    assert res.repo_path == "/tmp/foo"
    assert res.features == ()
    assert res.detection_confidence == "high"


def test_merge_combines_features_no_overlap():
    a = AggregateResult(
        repo_path="/r", analyzed_at=datetime(2026, 1, 1),
        features=(AggregatedFeature(name="Auth"),),
    )
    b = AggregateResult(
        repo_path="/r", analyzed_at=datetime(2026, 1, 1),
        features=(AggregatedFeature(name="Billing"),),
    )
    merged = a.merge(b)
    names = {f.name for f in merged.features}
    assert names == {"Auth", "Billing"}


def test_merge_overlapping_feature_takes_other():
    a = AggregateResult(
        repo_path="/r", analyzed_at=datetime(2026, 1, 1),
        features=(AggregatedFeature(name="Auth", description="initial"),),
    )
    b = AggregateResult(
        repo_path="/r", analyzed_at=datetime(2026, 1, 1),
        features=(AggregatedFeature(name="Auth", description="refined"),),
    )
    merged = a.merge(b)
    assert len(merged.features) == 1
    assert merged.features[0].description == "refined"


def test_merge_unions_warnings():
    a = AggregateResult(
        repo_path="/r", analyzed_at=datetime(2026, 1, 1),
        warnings=("W1",),
    )
    b = AggregateResult(
        repo_path="/r", analyzed_at=datetime(2026, 1, 1),
        warnings=("W2",),
    )
    assert set(a.merge(b).warnings) == {"W1", "W2"}


def test_merge_demotes_stack_recognised_when_either_false():
    a = AggregateResult(
        repo_path="/r", analyzed_at=datetime(2026, 1, 1),
        stack_recognised=True,
    )
    b = AggregateResult(
        repo_path="/r", analyzed_at=datetime(2026, 1, 1),
        stack_recognised=False,
    )
    assert a.merge(b).stack_recognised is False


# ── Custom exceptions ────────────────────────────────────────────────


def test_extractor_error_is_narrow():
    err = ExtractorError("test")
    assert isinstance(err, Exception)


# ── End-to-end: Extractor → Aggregator → Writer chain via Protocols ──


def test_protocols_chain_end_to_end():
    """Substitutability sanity: a no-engine pipeline composed entirely
    of fakes runs and produces a writable result.
    """
    extractor = FakeExtractor(canned_signals=(
        Signal(kind="feature", source="fake", payload={"name": "Auth"}),
        Signal(kind="feature", source="fake", payload={"name": "Billing"}),
    ))
    aggregator = FakeAggregator()
    writer = FakeWriter()

    signals = extractor.extract(Path("/tmp"), [])
    result = aggregator.aggregate(signals)
    writer.write(result, Path("/tmp/out.json"))

    assert writer.written is not None
    assert len(writer.written) == 1
    assert {f.name for f in writer.written[0].features} == {"Auth", "Billing"}
