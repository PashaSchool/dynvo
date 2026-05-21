"""RailsRoutesExtractor unit tests."""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.rails_routes import RailsRoutesExtractor


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
        tracked_files=["config/routes.rb"],
        audited_stack="next-app-router",
    )
    assert RailsRoutesExtractor().extract(ctx) == []


def test_extract_returns_empty_when_routes_file_missing(tmp_path: Path) -> None:
    ctx = _ctx(repo_path=tmp_path, tracked_files=[])
    assert RailsRoutesExtractor().extract(ctx) == []


def test_extract_resources_block(tmp_path: Path) -> None:
    _write(
        tmp_path / "config/routes.rb",
        """
Rails.application.routes.draw do
  resources :users
  resources :posts, only: [:index, :show]
  resource :profile
end
""".strip(),
    )
    ctx = _ctx(repo_path=tmp_path, tracked_files=["config/routes.rb"])
    out = RailsRoutesExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "users" in slugs
    assert "posts" in slugs
    assert "profile" in slugs


def test_extract_explicit_verb_routes(tmp_path: Path) -> None:
    _write(
        tmp_path / "config/routes.rb",
        """
Rails.application.routes.draw do
  get '/about' => 'pages#about'
  post '/contact' => 'pages#contact'
end
""".strip(),
    )
    ctx = _ctx(repo_path=tmp_path, tracked_files=["config/routes.rb"])
    out = RailsRoutesExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "about" in slugs
    assert "contact" in slugs


def test_extract_namespace_block(tmp_path: Path) -> None:
    _write(
        tmp_path / "config/routes.rb",
        """
Rails.application.routes.draw do
  namespace :api do
    resources :widgets
  end
end
""".strip(),
    )
    ctx = _ctx(repo_path=tmp_path, tracked_files=["config/routes.rb"])
    out = RailsRoutesExtractor().extract(ctx)
    slugs = {a.name for a in out}
    # 'api' itself is in the noise token set; widgets must still appear.
    assert "widgets" in slugs


def test_extract_skips_comment_lines(tmp_path: Path) -> None:
    _write(
        tmp_path / "config/routes.rb",
        """
Rails.application.routes.draw do
  # resources :ignored_in_comment
  resources :real
end
""".strip(),
    )
    ctx = _ctx(repo_path=tmp_path, tracked_files=["config/routes.rb"])
    out = RailsRoutesExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "real" in slugs
    assert "ignored-in-comment" not in slugs


def test_anchors_carry_source_tag(tmp_path: Path) -> None:
    _write(
        tmp_path / "config/routes.rb",
        "resources :users\n",
    )
    ctx = _ctx(repo_path=tmp_path, tracked_files=["config/routes.rb"])
    for a in RailsRoutesExtractor().extract(ctx):
        assert a.source == "rails-routes"
        assert a.paths == ("config/routes.rb",)
