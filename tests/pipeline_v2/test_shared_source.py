"""Perf wave 2 (R4) — shared per-scan repo snapshot state.

Contract under test:
  * shared ``_SourceCache`` serves the SAME content a locally
    constructed one computes (text / functions / imports);
  * guards: a foreign repo_path or a scoped (different tracked_files)
    ctx gets ``None`` → callers fall back to local construction;
  * ``__deepcopy__`` transparency across the orchestrator's ``_isolate``
    boundary;
  * the replay serializer EXCLUDES ``shared_source`` from captured
    ``ScanContext`` payloads and a replayed ctx carries ``None`` (so
    per-stage replay always exercises the local-construction fallback).
"""

from __future__ import annotations

import copy
from pathlib import Path

from faultline.pipeline_v2.shared_source import (
    SharedSourceState,
    shared_reach_context,
    shared_source_cache,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext


def _ctx(tmp_path: Path, files: list[str]) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack="nextjs",
        monorepo=False,
        workspaces=None,
        tracked_files=list(files),
        commits=[],
    )


def _repo(tmp_path: Path) -> list[str]:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.ts").write_text(
        'import {b} from "./b";\nexport function fa() { return b(); }\n',
    )
    (tmp_path / "app" / "b.ts").write_text(
        "export function b() { return 1; }\n",
    )
    return ["app/a.ts", "app/b.ts"]


def test_shared_source_cache_content_matches_local(tmp_path: Path) -> None:
    from faultline.pipeline_v2.stage_6_3_import_tree import _SourceCache

    files = _repo(tmp_path)
    ctx = _ctx(tmp_path, files)
    ctx.shared_source = SharedSourceState(ctx)

    shared = shared_source_cache(ctx, tmp_path)
    assert shared is not None
    local = _SourceCache(tmp_path)
    for rel in files:
        assert shared.text(rel) == local.text(rel)
        assert shared.imports(rel) == local.imports(rel)
        assert [
            (e.name, e.line_start, e.line_end)
            for e in shared.functions(rel)
        ] == [
            (e.name, e.line_start, e.line_end)
            for e in local.functions(rel)
        ]

    # Same instance on every ask — that's the dedupe.
    assert shared_source_cache(ctx, tmp_path) is shared


def test_source_cache_guard_foreign_repo_path(tmp_path: Path) -> None:
    files = _repo(tmp_path)
    ctx = _ctx(tmp_path, files)
    ctx.shared_source = SharedSourceState(ctx)
    assert shared_source_cache(ctx, tmp_path / "somewhere-else") is None
    assert shared_source_cache(ctx, tmp_path) is not None


def test_reach_context_shared_and_guarded(tmp_path: Path) -> None:
    from faultline.pipeline_v2.flow_reach import build_reach_context

    files = _repo(tmp_path)
    ctx = _ctx(tmp_path, files)
    ctx.shared_source = SharedSourceState(ctx)

    rctx = shared_reach_context(ctx)
    assert rctx is not None
    assert shared_reach_context(ctx) is rctx  # one build, served twice

    # Content parity vs a fresh local build.
    local = build_reach_context(ctx)
    assert rctx.file_set == local.file_set
    assert set(rctx.signatures) == set(local.signatures)

    # An _isolate'd (deepcopied) ctx has EQUAL content -> still served.
    iso = copy.deepcopy(ctx)
    assert iso.shared_source is ctx.shared_source
    assert shared_reach_context(iso) is rctx

    # A SCOPED ctx (different tracked_files) must NOT be served.
    scoped = _ctx(tmp_path, files[:1])
    scoped.shared_source = ctx.shared_source
    assert shared_reach_context(scoped) is None


def test_no_shared_state_is_clean_none(tmp_path: Path) -> None:
    files = _repo(tmp_path)
    ctx = _ctx(tmp_path, files)  # shared_source left at default None
    assert shared_source_cache(ctx, tmp_path) is None
    assert shared_reach_context(ctx) is None


def test_deepcopy_transparency(tmp_path: Path) -> None:
    files = _repo(tmp_path)
    ctx = _ctx(tmp_path, files)
    state = SharedSourceState(ctx)
    assert copy.deepcopy(state) is state
    assert copy.copy(state) is state


def test_replay_capture_excludes_shared_source(tmp_path: Path) -> None:
    from faultline.replay.serialize import from_jsonable, to_jsonable

    files = _repo(tmp_path)
    ctx = _ctx(tmp_path, files)
    ctx.shared_source = SharedSourceState(ctx)

    tree = to_jsonable(ctx)
    assert "shared_source" not in tree["value"]
    assert "cache_backend" not in tree["value"]  # pre-existing exclusion

    rebuilt = from_jsonable(tree)
    assert rebuilt.shared_source is None  # replay -> local fallback
    assert rebuilt.tracked_files == ctx.tracked_files
