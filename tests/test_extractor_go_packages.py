"""GoPackageExtractor unit tests.

Synthetic-repo fixtures: each test writes a handful of ``.go`` files
into ``tmp_path``, builds a ``ScanContext`` with the right activation
hints, and asserts the extractor emits the expected structural anchors.
One behaviour per test.
"""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.go_packages import GoPackageExtractor


def _ctx(
    *,
    repo_path: Path,
    tracked_files: list[str],
    audited_stack: str | None = "go-server",
    stack: str | None = None,
    secondary_stacks: tuple[str, ...] = (),
) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack=stack,
        monorepo=False,
        workspaces=None,
        tracked_files=tracked_files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
        audited_stack=audited_stack,
        secondary_stacks=secondary_stacks,
        extractor_hints=(),
        auditor_confidence=0.9 if audited_stack else None,
    )


def _write(p: Path, content: str = "package x\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _layout(tmp_path: Path) -> list[str]:
    """A representative Go repo: cmd/internal/pkg/top-level + noise."""
    files = [
        "go.mod",
        "cmd/server/main.go",
        "cmd/server/run.go",
        "internal/auth/auth.go",
        "internal/auth/oauth/oauth.go",      # nested → folds into auth
        "internal/auth/auth_test.go",
        "pkg/store/store.go",
        "httpapi/handler.go",                 # top-level package
        "vendor/github.com/x/y.go",           # excluded
        "testdata/fixture.go",                # excluded
    ]
    for f in files:
        _write(tmp_path / f)
    return [f for f in files]


# ── emits convention + top-level anchors ────────────────────────────────────


def test_emits_cmd_internal_pkg_and_toplevel_anchors(tmp_path: Path) -> None:
    tracked = _layout(tmp_path)
    cands = GoPackageExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=tracked))
    names = {c.name for c in cands}
    assert {"server", "auth", "store", "httpapi"}.issubset(names)


def test_all_candidates_carry_go_package_source(tmp_path: Path) -> None:
    tracked = _layout(tmp_path)
    cands = GoPackageExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=tracked))
    assert cands
    assert all(c.source == "go-package" for c in cands)
    assert all(c.confidence_self == 0.7 for c in cands)


def test_internal_anchor_claims_nested_files_recursively(tmp_path: Path) -> None:
    tracked = _layout(tmp_path)
    cands = GoPackageExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=tracked))
    auth = next(c for c in cands if c.name == "auth")
    # the nested oauth/ file folds into the single auth anchor
    assert "internal/auth/oauth/oauth.go" in auth.paths
    assert "internal/auth/auth.go" in auth.paths


# ── exclusions ───────────────────────────────────────────────────────────────


def test_excludes_vendor_and_testdata(tmp_path: Path) -> None:
    tracked = _layout(tmp_path)
    cands = GoPackageExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=tracked))
    all_paths = {p for c in cands for p in c.paths}
    assert not any(p.startswith("vendor/") for p in all_paths)
    assert not any(p.startswith("testdata/") for p in all_paths)


def test_test_only_directory_is_dropped(tmp_path: Path) -> None:
    """A top-level dir whose only .go file is a test must not anchor."""
    _write(tmp_path / "go.mod")
    _write(tmp_path / "metrics" / "metrics_test.go")
    tracked = ["go.mod", "metrics/metrics_test.go"]
    cands = GoPackageExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=tracked))
    assert "metrics" not in {c.name for c in cands}


def test_generated_only_directory_is_dropped(tmp_path: Path) -> None:
    """A dir whose only .go files are generated must not anchor."""
    _write(tmp_path / "go.mod")
    _write(tmp_path / "proto" / "service.pb.go")
    tracked = ["go.mod", "proto/service.pb.go"]
    cands = GoPackageExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=tracked))
    assert "proto" not in {c.name for c in cands}


# ── activation gate ──────────────────────────────────────────────────────────


def test_returns_empty_for_non_go_repo(tmp_path: Path) -> None:
    _write(tmp_path / "cmd" / "server" / "main.go")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["cmd/server/main.go"],
        audited_stack="rust-workspace",
        stack="rust",
    )
    assert GoPackageExtractor().extract(ctx) == []


def test_activates_on_secondary_go_stack(tmp_path: Path) -> None:
    _write(tmp_path / "cmd" / "worker" / "main.go")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["cmd/worker/main.go"],
        audited_stack="monorepo-polyglot",
        stack=None,
        secondary_stacks=("go", "typescript"),
    )
    names = {c.name for c in GoPackageExtractor().extract(ctx)}
    assert "worker" in names
