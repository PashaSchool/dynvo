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


def test_orchestrator_always_loads_built_in_extractors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The built-in first-party extractors are ALWAYS in the registry,
    even when the entry-point group is empty (stale / editable install)
    — they are loaded directly from this package.

    This guards the ``routes_index == 0 on FastAPI`` regression: a
    partial/empty entry-point snapshot must never drop a built-in
    extractor such as ``fastapi-route``."""
    import importlib
    mod = importlib.import_module("faultline.pipeline_v2.stage_1_extractors")

    monkeypatch.setattr(mod, "entry_points", lambda group=None: [])

    registry = mod._discover_extractors()
    names = {ex.name for ex in registry}
    # All built-ins present, regardless of entry-point state.
    assert {
        "route", "mvc", "schema", "package", "config",
        "fastapi-route", "route-fastify",
        "rails-routes", "rails-models", "rails-views",
        "rails-jobs", "rails-stimulus",
    }.issubset(names)


def test_orchestrator_does_not_drop_built_ins_for_stale_partial_entrypoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION GUARD for commit ab1811c follow-up: a STALE entry-point
    group listing only a SUBSET of the built-ins (the exact shape of the
    installed dist-info that omitted ``fastapi-route``) must NOT cause the
    newer built-ins to be dropped. Merge semantics keep them."""
    import importlib
    mod = importlib.import_module("faultline.pipeline_v2.stage_1_extractors")

    # Simulate a stale snapshot: only the 9 pre-fastapi extractors.
    stale_names = [
        "config", "go-router", "js-library", "mvc", "package",
        "python-library", "route", "rust-workspace", "schema",
    ]

    def _fake_ep(name: str):
        # Point each stale ep at the real built-in class so .load() works.
        from faultline.pipeline_v2.extractors.route import RouteFileExtractor
        return type(
            "EP", (),
            {"name": name, "load": staticmethod(lambda: RouteFileExtractor)},
        )()

    monkeypatch.setattr(
        mod, "entry_points",
        lambda group=None: [_fake_ep(n) for n in stale_names],
    )

    names = {ex.name for ex in mod._discover_extractors()}
    # fastapi-route lives in-tree but NOT in the stale ep list — it must
    # still be present via the always-on built-in merge.
    assert "fastapi-route" in names
    assert "route-fastify" in names


def test_orchestrator_adds_third_party_entry_point_extractor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely-external entry-point extractor (name not colliding
    with any built-in) is ADDED on top of the built-ins."""
    import importlib
    mod = importlib.import_module("faultline.pipeline_v2.stage_1_extractors")

    class _Stub:
        name = "custom-stub"

        def extract(self, ctx: ScanContext) -> list[AnchorCandidate]:
            return [
                AnchorCandidate(
                    name="stub-only", paths=(), source="custom-stub",
                    confidence_self=0.1,
                ),
            ]

    fake_ep = type(
        "EP", (),
        {"name": "custom-stub", "load": staticmethod(lambda: _Stub)},
    )()
    monkeypatch.setattr(mod, "entry_points", lambda group=None: [fake_ep])

    names = {ex.name for ex in mod._discover_extractors()}
    assert "custom-stub" in names          # third-party added
    assert "route" in names                # built-ins still present
