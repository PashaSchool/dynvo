"""RailsStimulusExtractor unit tests."""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.rails_stimulus import (
    RailsStimulusExtractor,
)


def _ctx(
    *,
    repo_path: Path,
    tracked_files: list[str],
    audited_stack: str | None = "rails-app",
) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack="ruby",
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


def test_extract_returns_empty_on_non_rails(tmp_path: Path) -> None:
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["app/javascript/controllers/users_controller.js"],
        audited_stack="next-app-router",
    )
    assert RailsStimulusExtractor().extract(ctx) == []


def test_extract_filename_suffix_match(tmp_path: Path) -> None:
    tracked = [
        "app/javascript/controllers/users_controller.js",
        "app/javascript/controllers/dropdown_controller.js",
    ]
    for f in tracked:
        _write(tmp_path / f, "export default class {}\n")
    ctx = _ctx(repo_path=tmp_path, tracked_files=tracked)
    out = RailsStimulusExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "users" in slugs
    assert "dropdown" in slugs


def test_extract_skips_non_controller_files(tmp_path: Path) -> None:
    tracked = [
        "app/javascript/controllers/index.js",
        "app/javascript/controllers/users_controller.js",
    ]
    for f in tracked:
        _write(tmp_path / f, "// js\n")
    ctx = _ctx(repo_path=tmp_path, tracked_files=tracked)
    out = RailsStimulusExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "users" in slugs
    # index.js doesn't match `_controller.js` suffix → not emitted.
    assert "index" not in slugs


def test_extract_alt_base_dir(tmp_path: Path) -> None:
    # Some apps put controllers under app/javascript/src/controllers/.
    _write(
        tmp_path / "app/javascript/src/controllers/menu_controller.js",
        "import { Controller } from '@hotwired/stimulus';\n"
        "export default class extends Controller {}\n",
    )
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["app/javascript/src/controllers/menu_controller.js"],
    )
    out = RailsStimulusExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "menu" in slugs


def test_anchor_source_tag(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/javascript/controllers/x_controller.js",
        "// stub\n",
    )
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["app/javascript/controllers/x_controller.js"],
    )
    out = RailsStimulusExtractor().extract(ctx)
    for a in out:
        assert a.source == "rails-stimulus"
