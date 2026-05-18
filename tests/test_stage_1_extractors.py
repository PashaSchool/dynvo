"""Tests for the Stage 1 extractor orchestrator
(``faultline.pipeline_v2.stage_1_extractors``).

Per-extractor fixture tests live in ``test_stage_1_extractor_wirings.py``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from faultline.pipeline_v2 import (
    AnchorCandidate,
    ScanContext,
    Workspace,
    stage_1_extractors,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _ctx(
    *,
    repo_path: Path,
    stack: str | None = None,
    tracked_files: list[str] | None = None,
    monorepo: bool = False,
    workspaces: list[Workspace] | None = None,
) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack=stack,
        monorepo=monorepo,
        workspaces=workspaces,
        tracked_files=tracked_files or [],
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


# ── A3 orchestrator behaviour ──────────────────────────────────────────────


class _SleepyExtractor:
    """Test double — sleeps for ``delay`` seconds, then returns a
    single fixed AnchorCandidate. Used to verify parallel execution.
    """

    def __init__(self, name: str, delay: float) -> None:
        self.name = name
        self._delay = delay

    def extract(self, ctx: ScanContext) -> list[AnchorCandidate]:
        time.sleep(self._delay)
        return [
            AnchorCandidate(
                name=self.name,
                paths=(),
                source=self.name,
                confidence_self=0.5,
            ),
        ]


class _BoomExtractor:
    name = "boom"

    def extract(self, ctx: ScanContext) -> list[AnchorCandidate]:
        raise RuntimeError("kaboom")


class _BadReturnExtractor:
    name = "bad"

    def extract(self, ctx: ScanContext) -> list[AnchorCandidate]:
        return ["not-an-anchor"]  # type: ignore[return-value]


def test_orchestrator_runs_in_parallel(tmp_path: Path) -> None:
    ctx = _ctx(repo_path=tmp_path)
    delay = 0.15
    extractors = [_SleepyExtractor(f"src{i}", delay) for i in range(4)]

    t0 = time.perf_counter()
    result = stage_1_extractors(ctx, extractors=extractors)
    elapsed = time.perf_counter() - t0

    # Sum of individual delays would be 4 * 0.15 = 0.6s. Parallel
    # execution should finish well under that (give a generous margin
    # for slow CI).
    assert elapsed < delay * len(extractors) * 0.75, (
        f"orchestrator did not run in parallel: elapsed={elapsed:.2f}s"
    )
    assert set(result.keys()) == {"src0", "src1", "src2", "src3"}
    for source, candidates in result.items():
        assert len(candidates) == 1
        assert candidates[0].source == source


def test_orchestrator_isolates_failing_extractor(tmp_path: Path) -> None:
    ctx = _ctx(repo_path=tmp_path)
    extractors = [_BoomExtractor(), _SleepyExtractor("ok", 0.01)]

    result = stage_1_extractors(ctx, extractors=extractors)

    # Healthy extractor still produced output
    assert result["ok"] and result["ok"][0].source == "ok"
    # Broken one's slot exists but is empty
    assert result["boom"] == []
    # _errors sentinel describes what went wrong
    assert "_errors" in result
    errors = result["_errors"]  # type: ignore[index]
    assert "boom" in errors
    assert "RuntimeError" in errors["boom"]


def test_orchestrator_rejects_bad_return_type(tmp_path: Path) -> None:
    ctx = _ctx(repo_path=tmp_path)
    result = stage_1_extractors(ctx, extractors=[_BadReturnExtractor()])
    # The wrapper catches the TypeError and records it.
    assert result["bad"] == []
    assert "bad" in result.get("_errors", {})  # type: ignore[arg-type]


def test_orchestrator_empty_registry(tmp_path: Path) -> None:
    ctx = _ctx(repo_path=tmp_path)
    assert stage_1_extractors(ctx, extractors=[]) == {}


def test_orchestrator_falls_back_to_default_loader_when_no_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When entry-point group returns empty, the discovery path calls
    ``_load_default_extractors``. We don't assert on the *contents* of
    that list here (the 5 built-in extractor modules land in A4) —
    we only verify the orchestrator routes correctly to the loader."""
    import importlib
    mod = importlib.import_module("faultline.pipeline_v2.stage_1_extractors")

    called = {"count": 0}

    def _stub_loader() -> list:
        called["count"] += 1
        return []

    monkeypatch.setattr(mod, "entry_points", lambda group=None: [])
    monkeypatch.setattr(mod, "_load_default_extractors", _stub_loader)

    ctx = _ctx(repo_path=tmp_path)
    stage_1_extractors(ctx)  # ``extractors=None`` triggers discovery

    assert called["count"] == 1


def test_orchestrator_prefers_entry_points_over_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    mod = importlib.import_module("faultline.pipeline_v2.stage_1_extractors")

    class _Stub:
        name = "stub"

        def extract(self, ctx: ScanContext) -> list[AnchorCandidate]:
            return [
                AnchorCandidate(
                    name="stub-only", paths=(), source="stub",
                    confidence_self=0.1,
                ),
            ]

    fake_ep = type("EP", (), {"name": "stub", "load": staticmethod(lambda: _Stub)})()
    monkeypatch.setattr(mod, "entry_points", lambda group=None: [fake_ep])

    ctx = _ctx(repo_path=tmp_path)
    result = stage_1_extractors(ctx)
    assert set(result.keys()) == {"stub"}
    assert result["stub"][0].name == "stub-only"
