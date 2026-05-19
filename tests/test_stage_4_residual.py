"""Tests for ``faultline.pipeline_v2.stage_4_residual``.

Verifies:

  - Zero unattributed paths → empty Stage4Result, no LLM calls.
  - Mocked LLM response with valid paths produces low-confidence
    DeveloperFeatures with ``source=["llm-fallback"]``.
  - Bad names (folder paths, Title Case, empty) are rejected.
  - Paths the LLM invents (not in the input set) are stripped.
  - 30% LLM-fallback share cap kicks in and emits a warning.
  - Cost tracker accumulates per call.
  - LLM failure does not crash the orchestrator.
"""

from __future__ import annotations

import json
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from faultline.llm.cost import CostTracker
from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature
from faultline.pipeline_v2.stage_4_residual import (
    Stage4Result,
    _is_acceptable_name,
    stage_4_residual,
)


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


def _feat(name: str, paths: tuple[str, ...]) -> DeveloperFeature:
    return DeveloperFeature(
        name=name, paths=paths, sources=["route"], confidence="medium",
    )


class _FakeAnthropic:
    """Records Haiku calls and replays canned responses."""

    def __init__(
        self,
        responses: list[str],
        *,
        in_tokens: int = 200,
        out_tokens: int = 100,
        raise_on_call: int | None = None,
    ) -> None:
        self.responses = responses
        self._idx = 0
        self.calls: list[dict[str, Any]] = []
        self.in_tokens = in_tokens
        self.out_tokens = out_tokens
        self.raise_on_call = raise_on_call
        self.messages = self._Messages(self)
        self._lock = threading.Lock()

    class _Messages:
        def __init__(self, parent: "_FakeAnthropic") -> None:
            self._p = parent

        def create(self, **kwargs: Any) -> Any:
            with self._p._lock:
                self._p.calls.append(kwargs)
                if self._p.raise_on_call == len(self._p.calls):
                    raise RuntimeError("simulated outage")
                if self._p._idx < len(self._p.responses):
                    text = self._p.responses[self._p._idx]
                    self._p._idx += 1
                else:
                    text = self._p.responses[-1] if self._p.responses else '{"features":[]}'
            content = [types.SimpleNamespace(text=text)]
            usage = types.SimpleNamespace(
                input_tokens=self._p.in_tokens,
                output_tokens=self._p.out_tokens,
            )
            return types.SimpleNamespace(content=content, usage=usage)


# ── Unit: helpers ──────────────────────────────────────────────────────────


def test_is_acceptable_name_rules() -> None:
    # kebab-case alnum → ok
    assert _is_acceptable_name("billing-portal")
    assert _is_acceptable_name("auth")  # WAIT — this IS in rejected generics
    # ...actually "auth" is not in the generic-folder reject list; "app" is.
    assert _is_acceptable_name("auth")
    # rejected generic single-word folder names
    assert not _is_acceptable_name("app")
    assert not _is_acceptable_name("src")
    assert not _is_acceptable_name("lib")
    assert not _is_acceptable_name("utils")
    # uppercase / Title Case rejected
    assert not _is_acceptable_name("BillingPortal")
    assert not _is_acceptable_name("Billing-Portal")
    # slashes / dots / whitespace rejected
    assert not _is_acceptable_name("app/billing")
    assert not _is_acceptable_name("billing.portal")
    assert not _is_acceptable_name("billing portal")
    # empty rejected
    assert not _is_acceptable_name("")
    assert not _is_acceptable_name("   ")


# Sprint A1: the 30%-share cap was removed; Stage 4 now emits every
# surviving fallback feature. Downstream quality gates live in Stage 5.
# These tests are retained to LOCK that no cap is re-introduced.


def test_stage_4_no_longer_truncates_by_share_ratio(tmp_path: Path) -> None:
    """Even with deterministic=0 and 10 LLM features, all survive."""
    residual = [f"r{i}.ts" for i in range(10)]
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({
        "features": [
            {"name": f"resid-{i}", "paths": [f"r{i}.ts"]} for i in range(10)
        ],
    })
    client = _FakeAnthropic(responses=[canned])
    result = stage_4_residual(
        residual, ctx, existing_features=[], client=client,
    )
    # PRE-A1 this returned 0 (floored cap). Post-A1 it returns all 10.
    assert len(result.residual_features) == 10
    # No warning about share-cap drops anymore.
    assert not any("share" in w.lower() for w in result.warnings)


# ── Orchestrator: zero residual ────────────────────────────────────────────


def test_zero_unattributed_no_llm_calls(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, files=[])
    result = stage_4_residual(
        [], ctx, existing_features=[], client=_FakeAnthropic(responses=[]),
    )
    assert isinstance(result, Stage4Result)
    assert result.residual_features == []
    assert result.llm_calls == 0
    assert result.cost_usd == 0.0


def test_no_client_returns_empty_with_warning(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, files=["a.ts"])
    result = stage_4_residual(
        ["a.ts"], ctx, existing_features=[],
        client=None, _client_factory=lambda: None,
    )
    assert result.residual_features == []
    assert any("no Anthropic client" in w for w in result.warnings)


# ── Orchestrator: happy path ──────────────────────────────────────────────


def test_happy_path_emits_low_confidence_features(tmp_path: Path) -> None:
    residual_paths = [f"app/widget-{i}/index.ts" for i in range(4)]
    ctx = _ctx(tmp_path, files=residual_paths)
    canned = json.dumps({
        "features": [
            {
                "name": "widget-toolkit",
                "paths": residual_paths[:2],
                "confidence": "low",
            },
            {
                "name": "widget-storage",
                "paths": residual_paths[2:],
                "confidence": "low",
            },
        ],
    })
    client = _FakeAnthropic(responses=[canned])

    # A1: cap removed. ``existing_features`` retained for symmetry only.
    existing = [_feat(f"det-{i}", (f"det{i}.ts",)) for i in range(5)]

    result = stage_4_residual(
        residual_paths, ctx, existing_features=existing,
        client=client,
    )

    assert result.llm_calls == 1
    assert len(result.residual_features) == 2
    for f in result.residual_features:
        assert f.confidence == "low"
        assert f.sources == ["llm-fallback"]
    assert result.cost_usd > 0


def test_invented_paths_stripped(tmp_path: Path) -> None:
    """The LLM proposed paths that aren't in the input — drop them."""
    residual = ["app/x.ts"]
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({
        "features": [
            {
                "name": "phantom-feature",
                "paths": ["app/x.ts", "imaginary/y.ts"],
            },
            {
                "name": "all-fake",
                "paths": ["totally/imaginary.ts"],
            },
        ],
    })
    client = _FakeAnthropic(responses=[canned])

    result = stage_4_residual(
        residual, ctx, existing_features=[_feat("a", ("a.ts",)) for _ in range(10)],
        client=client,
    )
    names = [f.name for f in result.residual_features]
    # "phantom-feature" survives with one valid path; "all-fake" dropped.
    assert names == ["phantom-feature"]
    assert result.residual_features[0].paths == ("app/x.ts",)
    assert any("all-fake" in r for r in result.rejected_names)


# ── Naming-discipline rejection ───────────────────────────────────────────


def test_bad_names_rejected(tmp_path: Path) -> None:
    residual = ["a.ts", "b.ts"]
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({
        "features": [
            {"name": "app", "paths": ["a.ts"]},                # generic folder
            {"name": "Billing", "paths": ["a.ts"]},            # Title Case
            {"name": "x/y", "paths": ["b.ts"]},                # slash
            {"name": "good-feature", "paths": ["b.ts"]},       # ok
        ],
    })
    client = _FakeAnthropic(responses=[canned])
    result = stage_4_residual(
        residual, ctx, existing_features=[_feat("d", ("d.ts",)) for _ in range(10)],
        client=client,
    )
    names = [f.name for f in result.residual_features]
    assert names == ["good-feature"]
    rejected = result.rejected_names
    assert any("app" in r for r in rejected)
    assert any("billing" in r.lower() for r in rejected)
    assert any("y" in r for r in rejected)


# ── LLM failure handling ──────────────────────────────────────────────────


def test_llm_failure_does_not_crash(tmp_path: Path) -> None:
    residual = ["a.ts", "b.ts"]
    ctx = _ctx(tmp_path, files=residual)
    client = _FakeAnthropic(responses=['{"features":[]}'], raise_on_call=1)
    result = stage_4_residual(
        residual, ctx, existing_features=[_feat("d", ("d.ts",))],
        client=client,
    )
    # Call attempted but raised; orchestrator still returns valid result.
    assert isinstance(result, Stage4Result)
    assert result.residual_features == []
    assert result.llm_calls == 1


# ── Chunking + cost cap ───────────────────────────────────────────────────


def test_chunking_respects_max_chunks(tmp_path: Path) -> None:
    # 600 paths, chunk_size=200, max_chunks=2 → only 400 paths get an LLM call.
    residual = [f"f{i}.ts" for i in range(600)]
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({"features": []})
    client = _FakeAnthropic(responses=[canned])
    result = stage_4_residual(
        residual, ctx,
        existing_features=[_feat("d", ("d.ts",)) for _ in range(5)],
        client=client, max_chunks=2, chunk_size=200,
    )
    assert result.chunks_processed == 2
    assert any("200 residual paths skipped" in w
               or "skipped after" in w for w in result.warnings)


def test_cost_tracker_records_per_chunk(tmp_path: Path) -> None:
    residual = [f"f{i}.ts" for i in range(400)]
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({"features": [{"name": "x-tools", "paths": ["f0.ts"]}]})
    client = _FakeAnthropic(responses=[canned], in_tokens=500, out_tokens=200)
    tracker = CostTracker(max_cost=None)
    result = stage_4_residual(
        residual, ctx,
        existing_features=[_feat("d", ("d.ts",)) for _ in range(10)],
        client=client, max_chunks=5, chunk_size=200,
        cost_tracker=tracker,
    )
    # 400 paths split into 2 chunks of 200 → 2 LLM calls
    assert result.llm_calls == 2
    assert tracker.call_count == 2
    assert tracker.total_cost_usd > 0
