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


# ── median-outlier flat-leaf collapse ────────────────────────────────────────
#
# Scale-invariant relative-outlier rule (rule-no-magic-tuning): a crate's
# flat ``src/<m>.rs`` leaves are dropped only when its flat-leaf count is
# a large multiple of the workspace MEDIAN flat-leaf count, AND the
# median itself clears a minimum-input floor. Synthetic, neutral crate /
# module names only (rule-no-repo-specific-paths). The RustWorkspace
# crate-level anchor is the coverage floor, so collapse never loses a
# crate.


def _flat_crate(crate: str, n: int) -> list[str]:
    """``n`` flat ``src/<m>.rs`` leaf modules plus a ``lib.rs`` root."""
    files = [f"{crate}/src/lib.rs"]
    files += [f"{crate}/src/mod_{i}.rs" for i in range(n)]
    return files


def test_collapse_inert_on_tiny_uniform_workspace(tmp_path: Path) -> None:
    """Median below the floor → pass is inert; ALL flat modules kept."""
    # Two crates with 2 flat leaves each → median 2 < floor 3 → inert.
    files = ["Cargo.toml"]
    files += _flat_crate("crates/alpha", 2)
    files += _flat_crate("crates/beta", 2)
    for f in files:
        _write(tmp_path / f)
    cands = RustModuleExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=files))
    rationales = {c.rationale for c in cands}
    # every flat leaf survives → 4 flat-module anchors present.
    flat = [c for c in cands if c.rationale == "rust src module file"]
    assert len(flat) == 4
    assert "rust src module file" in rationales


def test_collapse_drops_mega_crate_flat_leaves_keeps_folders_and_bins(
    tmp_path: Path,
) -> None:
    """One mega-crate's flat outliers collapse; its folder-modules and
    bins stay; the small peer crates are untouched."""
    files = ["Cargo.toml"]
    # Three small peers: 3 flat leaves each → median anchors at 3.
    for name in ("alpha", "beta", "gamma"):
        files += _flat_crate(f"crates/{name}", 3)
    # Mega crate: 20 flat leaves (>= 2.0 * median 3 = 6 → outlier),
    # plus a folder-module and a bin that must be preserved.
    files += _flat_crate("crates/mega", 20)
    files += [
        "crates/mega/src/engine/mod.rs",      # folder-module — always kept
        "crates/mega/src/engine/inner.rs",    # nested → folds into engine
        "crates/mega/src/bin/tool.rs",        # binary — always kept
    ]
    for f in files:
        _write(tmp_path / f)
    cands = RustModuleExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=files))

    # mega's flat leaves (mod_0..mod_19) are gone.
    flat_paths = {
        p
        for c in cands
        if c.rationale == "rust src module file"
        for p in c.paths
    }
    assert not any("crates/mega/src/mod_" in p for p in flat_paths)
    # mega's folder-module + bin survive.
    folder = next(c for c in cands if c.name == "engine")
    assert "crates/mega/src/engine/inner.rs" in folder.paths
    assert any(
        c.name == "tool" and c.rationale == "rust src/bin binary" for c in cands
    )
    # small peers untouched — their flat leaves all still present.
    for name in ("alpha", "beta", "gamma"):
        kept = [p for p in flat_paths if p.startswith(f"crates/{name}/src/mod_")]
        assert len(kept) == 3, f"{name} peer flat leaves should be untouched"


def test_collapse_inert_when_all_crates_uniformly_large(tmp_path: Path) -> None:
    """A workspace of uniformly-large flat crates has no RELATIVE outlier
    (every count == median) → nothing collapses, ALL flat leaves kept."""
    files = ["Cargo.toml"]
    for name in ("alpha", "beta", "gamma", "delta"):
        files += _flat_crate(f"crates/{name}", 15)  # all identical, well above floor
    for f in files:
        _write(tmp_path / f)
    cands = RustModuleExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=files))
    flat = [c for c in cands if c.rationale == "rust src module file"]
    # 4 crates x 15 = 60 flat anchors, none collapsed (15 < 2.0*15=30).
    assert len(flat) == 60


def test_collapse_two_crate_2x_gap_never_loses_a_crate(tmp_path: Path) -> None:
    """Edge: a 2-crate workspace where one crate is ~2x the other.

    With two crates the median is the mean of the two counts, so a pure
    2x gap does NOT make the larger crate reach 2.0 * median. The larger
    crate keeps its flat leaves — and even if it had collapsed, the
    RustWorkspace crate-level anchor would remain the floor, so a crate
    is never fully lost. Here we assert the smaller crate is always fully
    intact and the larger keeps at least its folder-module."""
    files = ["Cargo.toml"]
    # small: 4 flat leaves; large: 8 flat leaves (2x). median = 6.
    # cutoff = 2.0 * 6 = 12. Neither 4 nor 8 reaches 12 → both kept.
    files += _flat_crate("crates/small", 4)
    files += _flat_crate("crates/large", 8)
    files += [
        "crates/large/src/core/mod.rs",   # folder-module — always kept
    ]
    for f in files:
        _write(tmp_path / f)
    cands = RustModuleExtractor().extract(_ctx(repo_path=tmp_path, tracked_files=files))
    flat_paths = {
        p
        for c in cands
        if c.rationale == "rust src module file"
        for p in c.paths
    }
    # smaller crate fully intact.
    small_flat = [p for p in flat_paths if p.startswith("crates/small/src/mod_")]
    assert len(small_flat) == 4
    # larger crate keeps its flat leaves (no real outlier) ...
    large_flat = [p for p in flat_paths if p.startswith("crates/large/src/mod_")]
    assert len(large_flat) == 8
    # ... and its folder-module regardless.
    assert any(c.name == "core" for c in cands)
    # neither crate is fully lost.
    assert any(p.startswith("crates/small/") for c in cands for p in c.paths)
    assert any(p.startswith("crates/large/") for c in cands for p in c.paths)
