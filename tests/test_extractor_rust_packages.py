"""RustModuleExtractor unit tests.

Synthetic-repo fixtures: each test writes a handful of ``.rs`` files
into ``tmp_path``, builds a ``ScanContext`` with the right activation
hints, and asserts the extractor emits the expected first-level module
anchors. One behaviour per test.
"""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.rust_packages import RustModuleExtractor


def _ctx(
    *,
    repo_path: Path,
    tracked_files: list[str],
    audited_stack: str | None = "rust-binary",
    stack: str | None = "rust",
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


def _write(p: Path, content: str = "// rust\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _layout(tmp_path: Path) -> list[str]:
    """A representative single-crate Rust repo: flat + folder modules,
    a bin target, plus noise that must be excluded."""
    files = [
        "Cargo.toml",
        "src/main.rs",                 # crate root — never a feature
        "src/lib.rs",                  # crate root — never a feature
        "src/auth.rs",                 # flat module
        "src/store/mod.rs",            # module folder
        "src/store/backend/lru.rs",    # nested → folds into store
        "src/bin/cli.rs",              # binary target
        "tests/it.rs",                 # excluded
        "target/debug/x.rs",           # excluded
    ]
    for f in files:
        _write(tmp_path / f)
    return list(files)


# ── emits module anchors ─────────────────────────────────────────────────────


def test_emits_flat_folder_and_bin_module_anchors(tmp_path: Path) -> None:
    tracked = _layout(tmp_path)
    cands = RustModuleExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=tracked))
    names = {c.name for c in cands}
    assert {"auth", "store", "cli"}.issubset(names)


def test_all_candidates_carry_rust_module_source(tmp_path: Path) -> None:
    tracked = _layout(tmp_path)
    cands = RustModuleExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=tracked))
    assert cands
    assert all(c.source == "rust-module" for c in cands)
    assert all(c.confidence_self == 0.7 for c in cands)


def test_module_folder_claims_nested_files_recursively(tmp_path: Path) -> None:
    tracked = _layout(tmp_path)
    cands = RustModuleExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=tracked))
    store = next(c for c in cands if c.name == "store")
    assert "src/store/mod.rs" in store.paths
    assert "src/store/backend/lru.rs" in store.paths


# ── crate-root files are not features ────────────────────────────────────────


def test_lib_and_main_do_not_become_features(tmp_path: Path) -> None:
    tracked = _layout(tmp_path)
    cands = RustModuleExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=tracked))
    names = {c.name for c in cands}
    assert "lib" not in names
    assert "main" not in names


# ── exclusions ───────────────────────────────────────────────────────────────


def test_excludes_target_and_tests(tmp_path: Path) -> None:
    tracked = _layout(tmp_path)
    cands = RustModuleExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=tracked))
    all_paths = {p for c in cands for p in c.paths}
    assert not any(p.startswith("target/") for p in all_paths)
    assert not any(p.startswith("tests/") for p in all_paths)


# ── workspace per-crate + collision prefixing ────────────────────────────────


def test_per_crate_modules_with_collision_prefix(tmp_path: Path) -> None:
    """Two crates each exposing an ``auth`` module get crate-prefixed
    slugs; a non-colliding module keeps its bare slug."""
    files = [
        "Cargo.toml",
        "crates/api/src/lib.rs",
        "crates/api/src/auth.rs",      # collides
        "crates/api/src/billing.rs",   # unique
        "crates/worker/src/lib.rs",
        "crates/worker/src/auth.rs",   # collides
    ]
    for f in files:
        _write(tmp_path / f)
    cands = RustModuleExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=files))
    names = {c.name for c in cands}
    assert "api-auth" in names
    assert "worker-auth" in names
    assert "billing" in names
    assert "auth" not in names


# ── activation gate ──────────────────────────────────────────────────────────


def test_returns_empty_for_non_rust_repo(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "auth.rs")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["src/auth.rs"],
        audited_stack="go-server",
        stack="go",
    )
    assert RustModuleExtractor().extract(ctx) == []


def test_activates_on_secondary_rust_stack(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "ingest.rs")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["src/ingest.rs"],
        audited_stack="monorepo-polyglot",
        stack=None,
        secondary_stacks=("rust", "typescript"),
    )
    names = {c.name for c in RustModuleExtractor().extract(ctx)}
    assert "ingest" in names
