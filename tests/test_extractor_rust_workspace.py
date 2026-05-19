"""RustWorkspaceExtractor unit tests."""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.rust_workspace import RustWorkspaceExtractor


def _ctx(
    *,
    repo_path: Path,
    tracked_files: list[str],
    audited_stack: str | None = "rust-workspace",
    stack: str | None = "rust",
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
        secondary_stacks=(),
        extractor_hints=(),
        auditor_confidence=0.9,
    )


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ── basic workspace ───────────────────────────────────────────────────────


def test_workspace_explicit_members(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        """
[workspace]
resolver = "2"
members = [
    "crates/meilisearch",
    "crates/meilisearch-types",
]
""".strip(),
    )
    _write(
        tmp_path / "crates" / "meilisearch" / "Cargo.toml",
        """
[package]
name = "meilisearch"
version = "0.1.0"
""".strip(),
    )
    _write(tmp_path / "crates" / "meilisearch" / "src" / "main.rs", "fn main() {}")
    _write(
        tmp_path / "crates" / "meilisearch-types" / "Cargo.toml",
        """
[package]
name = "meilisearch-types"
version = "0.1.0"
""".strip(),
    )
    _write(
        tmp_path / "crates" / "meilisearch-types" / "src" / "lib.rs",
        "pub fn foo() {}",
    )

    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=[
            "Cargo.toml",
            "crates/meilisearch/Cargo.toml",
            "crates/meilisearch/src/main.rs",
            "crates/meilisearch-types/Cargo.toml",
            "crates/meilisearch-types/src/lib.rs",
        ],
    )

    cands = RustWorkspaceExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert {"meilisearch", "meilisearch-types"}.issubset(names)
    for c in cands:
        assert c.source == "rust-workspace"
        assert c.confidence_self == 0.95


def test_workspace_glob_members(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        """
[workspace]
members = ["crates/*"]
""".strip(),
    )
    for crate in ("alpha", "beta", "gamma"):
        _write(
            tmp_path / "crates" / crate / "Cargo.toml",
            f'[package]\nname = "{crate}"\n',
        )
        _write(tmp_path / "crates" / crate / "src" / "lib.rs", "")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=[
            "Cargo.toml",
            "crates/alpha/Cargo.toml", "crates/alpha/src/lib.rs",
            "crates/beta/Cargo.toml",  "crates/beta/src/lib.rs",
            "crates/gamma/Cargo.toml", "crates/gamma/src/lib.rs",
        ],
    )
    cands = RustWorkspaceExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert {"alpha", "beta", "gamma"} == names


def test_skip_when_no_workspace_section(tmp_path: Path) -> None:
    """A single-crate Rust project must NOT fire — single-crate
    repos are handled elsewhere."""
    _write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "myapp"\nversion = "0.1.0"\n',
    )
    _write(tmp_path / "src" / "main.rs", "fn main() {}")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["Cargo.toml", "src/main.rs"],
        audited_stack="rust-binary",  # NOT rust-workspace
        stack="rust",
    )
    cands = RustWorkspaceExtractor().extract(ctx)
    assert cands == []


def test_fallback_to_basename_when_member_manifest_missing(tmp_path: Path) -> None:
    _write(
        tmp_path / "Cargo.toml",
        '[workspace]\nmembers = ["crates/orphan"]\n',
    )
    _write(tmp_path / "crates" / "orphan" / "src" / "lib.rs", "")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["Cargo.toml", "crates/orphan/src/lib.rs"],
    )
    cands = RustWorkspaceExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "orphan" in names


def test_skip_on_non_rust_stack(tmp_path: Path) -> None:
    """Even when a Cargo.toml is present, the extractor must stay
    silent when the audited stack is js-monorepo."""
    _write(
        tmp_path / "Cargo.toml",
        '[workspace]\nmembers = ["crates/alpha"]\n',
    )
    _write(
        tmp_path / "crates" / "alpha" / "Cargo.toml",
        '[package]\nname = "alpha"\n',
    )
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["Cargo.toml", "crates/alpha/Cargo.toml"],
        audited_stack="js-monorepo",
        stack="javascript",
    )
    cands = RustWorkspaceExtractor().extract(ctx)
    assert cands == []
