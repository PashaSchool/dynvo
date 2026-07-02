"""Unit tests for ``faultline.pipeline_v2.flow_reach``.

Pure-deterministic module — no LLM, no network. Tests focus on:

  * BFS terminates at ``max_depth`` (handler → service → repo capture)
  * BFS terminates at ``max_paths`` (payload bound)
  * Cycle detection (file A imports B imports A doesn't loop)
  * Test / vendor / generated files excluded from frontier
  * Multi-language reach: TS/JS, Python, Go, Rust
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.flow_reach import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_PATHS,
    FlowReach,
    _is_test_or_vendor_or_generated,
    _resolve_python_module,
    build_reach_context,
    compute_flow_reach,
    compute_python_source_roots,
)


def _ctx(tmp_path: Path, tracked: list[str]) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=tracked,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


def _write(root: Path, rel: str, contents: str) -> None:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(contents)


# ── Test / vendor / generated filter ────────────────────────────────────


def test_filter_excludes_test_paths():
    assert _is_test_or_vendor_or_generated("src/foo.test.ts")
    assert _is_test_or_vendor_or_generated("packages/api/__tests__/foo.ts")
    assert _is_test_or_vendor_or_generated("internal/db_test.go")


def test_filter_excludes_vendor_paths():
    assert _is_test_or_vendor_or_generated("node_modules/react/index.js")
    assert _is_test_or_vendor_or_generated("vendor/github.com/foo/bar.go")
    assert _is_test_or_vendor_or_generated("dist/bundle.js")


def test_filter_excludes_generated_paths():
    assert _is_test_or_vendor_or_generated("api.generated.ts")
    assert _is_test_or_vendor_or_generated("api/foo.pb.go")
    assert _is_test_or_vendor_or_generated("types/index.d.ts")


def test_filter_keeps_normal_source():
    assert not _is_test_or_vendor_or_generated("src/auth/login.ts")
    assert not _is_test_or_vendor_or_generated("apps/web/page.tsx")
    assert not _is_test_or_vendor_or_generated("internal/api/handler.go")


# ── BFS depth + paths caps ──────────────────────────────────────────────


def test_bfs_caps_at_max_depth(tmp_path: Path):
    """5-deep linear chain with max_depth=3 should stop after 3 hops."""
    # a → b → c → d → e
    _write(tmp_path, "a.ts", 'import { b } from "./b";\nexport const a = 1;')
    _write(tmp_path, "b.ts", 'import { c } from "./c";\nexport const b = 1;')
    _write(tmp_path, "c.ts", 'import { d } from "./d";\nexport const c = 1;')
    _write(tmp_path, "d.ts", 'import { e } from "./e";\nexport const d = 1;')
    _write(tmp_path, "e.ts", "export const e = 1;")
    tracked = ["a.ts", "b.ts", "c.ts", "d.ts", "e.ts"]
    rctx = build_reach_context(_ctx(tmp_path, tracked))

    reach = compute_flow_reach(rctx, "a.ts", 1, max_depth=3, max_paths=100)

    # depth 3 means a + 3 hops = 4 files (a, b, c, d). e should be excluded.
    assert reach.entry_file == "a.ts"
    assert "a.ts" in reach.reached_paths
    assert "b.ts" in reach.reached_paths
    assert "c.ts" in reach.reached_paths
    assert "d.ts" in reach.reached_paths
    assert "e.ts" not in reach.reached_paths
    assert reach.depth_reached == 3


def test_bfs_caps_at_max_paths(tmp_path: Path):
    """One file importing 20 siblings with max_paths=5 should stop at 5."""
    siblings = [f"sibling_{i}.ts" for i in range(20)]
    import_lines = "\n".join(
        f'import {{ x{i} }} from "./{siblings[i][:-3]}";'
        for i in range(20)
    )
    _write(tmp_path, "hub.ts", f"{import_lines}\nexport const hub = 1;")
    for s in siblings:
        _write(tmp_path, s, f"export const x = 1;")
    tracked = ["hub.ts", *siblings]
    rctx = build_reach_context(_ctx(tmp_path, tracked))

    reach = compute_flow_reach(rctx, "hub.ts", 1, max_depth=3, max_paths=5)

    assert len(reach.reached_paths) == 5
    assert reach.reached_paths[0] == "hub.ts"  # entry first


def test_bfs_handles_cycles(tmp_path: Path):
    """a → b → a should not loop and should reach exactly {a, b}."""
    _write(tmp_path, "a.ts", 'import { b } from "./b";\nexport const a = 1;')
    _write(tmp_path, "b.ts", 'import { a } from "./a";\nexport const b = 1;')
    rctx = build_reach_context(_ctx(tmp_path, ["a.ts", "b.ts"]))

    reach = compute_flow_reach(rctx, "a.ts", 1)

    assert set(reach.reached_paths) == {"a.ts", "b.ts"}


def test_bfs_excludes_test_files(tmp_path: Path):
    """A flow reaching into a test file should NOT include it."""
    _write(
        tmp_path, "src/api.ts",
        'import { helper } from "./helper";\nexport const api = 1;',
    )
    _write(tmp_path, "src/helper.ts", "export const helper = 1;")
    _write(tmp_path, "src/api.test.ts", 'import { api } from "./api";')
    tracked = ["src/api.ts", "src/helper.ts", "src/api.test.ts"]
    rctx = build_reach_context(_ctx(tmp_path, tracked))

    reach = compute_flow_reach(rctx, "src/api.ts", 1)

    assert "src/helper.ts" in reach.reached_paths
    assert "src/api.test.ts" not in reach.reached_paths


def test_entry_only_when_no_imports(tmp_path: Path):
    """A leaf file with no imports yields reach = [entry_file] only."""
    _write(tmp_path, "leaf.ts", "export const leaf = 1;")
    rctx = build_reach_context(_ctx(tmp_path, ["leaf.ts"]))

    reach = compute_flow_reach(rctx, "leaf.ts", 1)

    assert reach.reached_paths == ("leaf.ts",)
    assert reach.depth_reached == 0


# ── Multi-language coverage ──────────────────────────────────────────────


def test_reach_python_from_import(tmp_path: Path):
    _write(tmp_path, "foo/__init__.py", "")
    _write(
        tmp_path, "foo/bar.py",
        "from foo.baz import qux\n\ndef bar(): return qux()\n",
    )
    _write(tmp_path, "foo/baz.py", "def qux(): return 1\n")
    tracked = ["foo/__init__.py", "foo/bar.py", "foo/baz.py"]
    rctx = build_reach_context(_ctx(tmp_path, tracked))

    reach = compute_flow_reach(rctx, "foo/bar.py", 1)

    assert "foo/bar.py" in reach.reached_paths
    assert "foo/baz.py" in reach.reached_paths


def test_reach_python_relative_import(tmp_path: Path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path, "pkg/main.py",
        "from .util import helper\n\ndef main(): return helper()\n",
    )
    _write(tmp_path, "pkg/util.py", "def helper(): return 1\n")
    tracked = ["pkg/__init__.py", "pkg/main.py", "pkg/util.py"]
    rctx = build_reach_context(_ctx(tmp_path, tracked))

    reach = compute_flow_reach(rctx, "pkg/main.py", 1)

    assert "pkg/util.py" in reach.reached_paths


def test_resolve_python_module_misses_stdlib(tmp_path: Path):
    file_set = frozenset({"foo/bar.py"})
    # stdlib / third-party — won't be in file_set
    assert _resolve_python_module("foo/bar.py", "os", file_set) is None
    assert _resolve_python_module("foo/bar.py", "requests", file_set) is None


# ── Source-root inference (FastAPI / Django / src-layout) ────────────────


def test_compute_source_roots_detects_service_dir():
    """A packageless dir hosting packages is inferred as a source root."""
    fs = frozenset({
        "backend/agent/__init__.py",
        "backend/agent/tools.py",
        "backend/routers/__init__.py",
        "backend/routers/detectors.py",
    })
    roots = compute_python_source_roots(fs)
    assert "" in roots          # repo root always present
    assert "backend" in roots   # inferred service source root


def test_compute_source_roots_detects_src_layout():
    fs = frozenset({
        "src/pkg/__init__.py",
        "src/pkg/mod.py",
    })
    roots = compute_python_source_roots(fs)
    assert "src" in roots


def test_compute_source_roots_plain_root_layout_no_widen():
    """A repo-root package layout yields only the root — no over-broaden."""
    fs = frozenset({
        "app/__init__.py",
        "app/main.py",
        "app/util.py",
    })
    roots = compute_python_source_roots(fs)
    assert roots == ("",)


def test_resolve_python_absolute_via_source_root():
    """``from agent.tools import x`` resolves when the file lives under a
    source root (``backend/agent/tools.py``).
    """
    fs = frozenset({
        "backend/agent/__init__.py",
        "backend/agent/tools.py",
        "backend/routers/__init__.py",
        "backend/routers/detectors.py",
    })
    roots = compute_python_source_roots(fs)
    resolved = _resolve_python_module(
        "backend/routers/detectors.py", "agent.tools", fs,
        source_roots=roots,
    )
    assert resolved == "backend/agent/tools.py"


def test_resolve_python_absolute_without_root_misses():
    """Backward-compat: with default (repo-root) roots, a source-root
    import does NOT resolve — the new behaviour is opt-in via the
    inferred roots threaded from ReachContext.
    """
    fs = frozenset({"backend/agent/tools.py"})
    assert _resolve_python_module(
        "backend/routers/detectors.py", "agent.tools", fs,
    ) is None


def test_reach_python_source_root_end_to_end(tmp_path: Path):
    """Full build_reach_context path resolves a source-root import."""
    _write(tmp_path, "backend/agent/__init__.py", "")
    _write(tmp_path, "backend/agent/tools.py", "def derive(): return 1\n")
    _write(tmp_path, "backend/routers/__init__.py", "")
    _write(
        tmp_path, "backend/routers/detectors.py",
        "from agent.tools import derive\n\ndef create(): return derive()\n",
    )
    tracked = [
        "backend/agent/__init__.py", "backend/agent/tools.py",
        "backend/routers/__init__.py", "backend/routers/detectors.py",
    ]
    rctx = build_reach_context(_ctx(tmp_path, tracked))
    reach = compute_flow_reach(rctx, "backend/routers/detectors.py", 1)
    assert "backend/agent/tools.py" in reach.reached_paths


def test_reach_go_internal_imports(tmp_path: Path):
    """Go internal-module imports resolve to package directories."""
    _write(
        tmp_path, "go.mod",
        "module github.com/example/app\n\ngo 1.21\n",
    )
    _write(
        tmp_path, "cmd/main.go",
        'package main\n\nimport "github.com/example/app/internal/service"\n\n'
        'func main() { service.Start() }\n',
    )
    _write(
        tmp_path, "internal/service/svc.go",
        "package service\n\nfunc Start() {}\n",
    )
    tracked = ["go.mod", "cmd/main.go", "internal/service/svc.go"]
    rctx = build_reach_context(_ctx(tmp_path, tracked))

    reach = compute_flow_reach(rctx, "cmd/main.go", 1)

    assert "cmd/main.go" in reach.reached_paths
    assert "internal/service/svc.go" in reach.reached_paths


def test_reach_go_skips_external_imports(tmp_path: Path):
    """Third-party imports must not pull anything into reach."""
    _write(tmp_path, "go.mod", "module github.com/example/app\n")
    _write(
        tmp_path, "main.go",
        'package main\n\nimport "github.com/other/lib"\n\nfunc main() {}\n',
    )
    rctx = build_reach_context(_ctx(tmp_path, ["go.mod", "main.go"]))

    reach = compute_flow_reach(rctx, "main.go", 1)

    assert reach.reached_paths == ("main.go",)


def test_reach_rust_use_crate(tmp_path: Path):
    _write(
        tmp_path, "src/lib.rs",
        "pub mod handler;\npub mod service;\n",
    )
    _write(
        tmp_path, "src/handler.rs",
        "use crate::service::Worker;\n\npub fn handle() {}\n",
    )
    _write(tmp_path, "src/service.rs", "pub struct Worker;\n")
    tracked = ["src/lib.rs", "src/handler.rs", "src/service.rs"]
    rctx = build_reach_context(_ctx(tmp_path, tracked))

    reach = compute_flow_reach(rctx, "src/handler.rs", 1)

    assert "src/handler.rs" in reach.reached_paths
    assert "src/service.rs" in reach.reached_paths


# ── Empty input + degenerate cases ──────────────────────────────────────


def test_reach_for_missing_entry_returns_entry_only(tmp_path: Path):
    """Entry file not in tracked list (defensive) — still returns it."""
    rctx = build_reach_context(_ctx(tmp_path, []))

    reach = compute_flow_reach(rctx, "ghost.ts", 1)

    assert reach.reached_paths == ("ghost.ts",)
    assert reach.depth_reached == 0


def test_max_depth_floor(tmp_path: Path):
    """max_depth=0 is treated as 1 (entry + one BFS layer)."""
    _write(tmp_path, "a.ts", 'import { b } from "./b";\nexport const a = 1;')
    _write(tmp_path, "b.ts", "export const b = 1;")
    rctx = build_reach_context(_ctx(tmp_path, ["a.ts", "b.ts"]))

    reach = compute_flow_reach(rctx, "a.ts", 1, max_depth=0)

    # max_depth coerced to 1 → entry + 1 hop
    assert "a.ts" in reach.reached_paths
    assert "b.ts" in reach.reached_paths


def test_default_caps_are_scale_invariant():
    """Documented caps live as module constants — test the values match
    the docstring claim (depth 3, paths 8). If you change these, the
    spec doc in flow_reach.py needs updating.
    """
    assert DEFAULT_MAX_DEPTH == 3
    assert DEFAULT_MAX_PATHS == 8


# ── FlowReach dataclass invariants ───────────────────────────────────────


def test_flowreach_is_frozen():
    reach = FlowReach(
        entry_file="a.ts", entry_line=1,
        reached_paths=("a.ts",), depth_reached=0,
    )
    with pytest.raises(Exception):
        reach.entry_file = "b.ts"  # type: ignore[misc]


# ── Determinism under hash randomisation (supabase drift, 2026-07-02) ───────

_SEED_DRIVER = """
import json, sys
from pathlib import Path
from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.flow_reach import build_reach_context, compute_flow_reach

root = Path(sys.argv[1])
tracked = json.loads(sys.argv[2])
ctx = ScanContext(repo_path=root, stack=None, monorepo=False, workspaces=None,
                  tracked_files=tracked, commits=[], stack_signals=[],
                  workspace_manager=None)
rctx = build_reach_context(ctx)
reach = compute_flow_reach(rctx, "entry.py", 1)
print(json.dumps(list(reach.reached_paths)))
"""


def test_reach_paths_stable_across_hash_seeds(tmp_path: Path) -> None:
    """The max_paths cap must keep the SAME neighbors regardless of the
    per-process PYTHONHASHSEED — a plain set made Flow.paths drift between
    two runs of an identical scan (supabase: bipartite shared_with/secondary
    + testmap counts). Requires subprocesses (the seed is fixed per process)."""
    import json as _json
    import subprocess
    import sys

    # Entry imports MORE modules than max_paths (8) → the cap binds.
    mods = [f"mod{i:02d}" for i in range(14)]
    _write(tmp_path, "entry.py",
           "\n".join(f"import {m}" for m in mods) + "\n")
    for m in mods:
        _write(tmp_path, f"{m}.py", "X = 1\n")
    tracked = ["entry.py"] + [f"{m}.py" for m in mods]

    driver = tmp_path / "driver.py"
    driver.write_text(_SEED_DRIVER)

    def _run(seed: str) -> list[str]:
        import os
        env = {"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin",
               "PYTHONPATH": os.getcwd()}
        out = subprocess.run(
            [sys.executable, str(driver), str(tmp_path), _json.dumps(tracked)],
            capture_output=True, text=True, env=env, check=True,
        )
        return _json.loads(out.stdout.strip())

    r1, r2 = _run("1"), _run("2")
    assert len(r1) > 0 and len(r1) <= 8 + 1
    assert r1 == r2, f"reach drifted across hash seeds:\n{r1}\nvs\n{r2}"


_MAPPER_SEED_DRIVER = """
import json, sys
from faultline.analyzer.test_mapper import _filename_match
# Many same-basename sources — the step-4 fallback must pick deterministically.
sources = {f"pkg{i:02d}/src/config.ts" for i in range(20)}
print(json.dumps(_filename_match("e2e/config.test.ts", sources)))
"""


def test_filename_match_step4_stable_across_hash_seeds(tmp_path: Path) -> None:
    """test_mapper._filename_match step-4 iterated a set and returned the
    first basename hit — the winning source was PYTHONHASHSEED-dependent
    (supabase testmap 382 vs 378). Must be stable across seeds."""
    import json as _json
    import os
    import subprocess
    import sys

    driver = tmp_path / "driver.py"
    driver.write_text(_MAPPER_SEED_DRIVER)

    def _run(seed: str) -> str:
        env = {"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin",
               "PYTHONPATH": os.getcwd()}
        out = subprocess.run([sys.executable, str(driver)],
                             capture_output=True, text=True, env=env,
                             check=True)
        return _json.loads(out.stdout.strip())

    r1, r2, r3 = _run("1"), _run("2"), _run("3")
    assert r1 == r2 == r3
    assert r1 == "pkg00/src/config.ts"  # lexicographic min


def test_lineage_uuids_content_derived_and_stable() -> None:
    """Fresh lineage mints must be identical across runs for identical
    content (uuid4 churned all identities per run), unique for dup names."""
    from faultline.pipeline_v2.lineage import assign_feature_lineage

    feats = [{"name": "auth", "paths": ["a.ts"]},
             {"name": "auth", "paths": ["b.ts"]},   # dup name → distinct uuid
             {"name": "billing", "paths": ["c.ts"]}]
    r1, _ = assign_feature_lineage([dict(f) for f in feats], None)
    r2, _ = assign_feature_lineage([dict(f) for f in feats], None)
    assert [x.uuid for x in r1] == [x.uuid for x in r2]
    assert len({x.uuid for x in r1}) == 3
    # flows namespace mints differently from features for the same name
    from faultline.pipeline_v2.lineage import assign_flow_lineage
    rf, _ = assign_flow_lineage([{"name": "auth", "paths": ["a.ts"]}], None)
    assert rf[0].uuid != r1[0].uuid
