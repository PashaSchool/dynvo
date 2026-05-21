"""RailsViewsExtractor unit tests."""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.rails_views import RailsViewsExtractor


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


def test_extract_returns_empty_on_non_rails(tmp_path: Path) -> None:
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["app/views/users/index.html.erb"],
        audited_stack="next-app-router",
    )
    assert RailsViewsExtractor().extract(ctx) == []


def test_extract_one_anchor_per_resource_dir(tmp_path: Path) -> None:
    tracked = [
        "app/views/users/index.html.erb",
        "app/views/users/show.html.erb",
        "app/views/users/_form.html.erb",
        "app/views/posts/index.html.erb",
        "app/views/posts/show.html.erb",
    ]
    ctx = _ctx(repo_path=tmp_path, tracked_files=tracked)
    out = RailsViewsExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "users" in slugs
    assert "posts" in slugs
    assert len(out) == 2

    users_anchor = next(a for a in out if a.name == "users")
    # All 3 user templates should be in its path tuple.
    assert len(users_anchor.paths) == 3


def test_extract_skips_layouts_and_shared(tmp_path: Path) -> None:
    tracked = [
        "app/views/layouts/application.html.erb",
        "app/views/shared/_navbar.html.erb",
        "app/views/application/_flash.html.erb",
        "app/views/users/index.html.erb",
    ]
    ctx = _ctx(repo_path=tmp_path, tracked_files=tracked)
    out = RailsViewsExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "users" in slugs
    assert "layouts" not in slugs
    assert "shared" not in slugs
    assert "application" not in slugs


def test_extract_handles_haml_and_slim(tmp_path: Path) -> None:
    tracked = [
        "app/views/users/index.html.haml",
        "app/views/posts/index.html.slim",
    ]
    ctx = _ctx(repo_path=tmp_path, tracked_files=tracked)
    out = RailsViewsExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "users" in slugs
    assert "posts" in slugs


def test_extract_skips_files_directly_under_views(tmp_path: Path) -> None:
    # files directly in app/views/ are not resource folders
    tracked = ["app/views/orphan.html.erb"]
    ctx = _ctx(repo_path=tmp_path, tracked_files=tracked)
    out = RailsViewsExtractor().extract(ctx)
    assert out == []


def test_anchor_source_tag(tmp_path: Path) -> None:
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["app/views/users/index.html.erb"],
    )
    out = RailsViewsExtractor().extract(ctx)
    for a in out:
        assert a.source == "rails-views"
