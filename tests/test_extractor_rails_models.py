"""RailsModelsExtractor unit tests."""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.rails_models import RailsModelsExtractor


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
        tracked_files=["app/models/user.rb"],
        audited_stack="django",
    )
    assert RailsModelsExtractor().extract(ctx) == []


def test_extract_single_active_record_model(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/models/user.rb",
        """
class User < ApplicationRecord
  has_many :posts
  belongs_to :account
end
""".strip(),
    )
    ctx = _ctx(repo_path=tmp_path, tracked_files=["app/models/user.rb"])
    out = RailsModelsExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "user" in slugs


def test_extract_multiple_models(tmp_path: Path) -> None:
    files = {
        "app/models/user.rb": "class User < ApplicationRecord\nend\n",
        "app/models/post.rb": "class Post < ApplicationRecord\nend\n",
        "app/models/address.rb": "class Address < ApplicationRecord\nend\n",
    }
    for path, content in files.items():
        _write(tmp_path / path, content)
    ctx = _ctx(repo_path=tmp_path, tracked_files=list(files.keys()))
    out = RailsModelsExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert {"user", "post", "address"}.issubset(slugs)


def test_extract_handles_active_record_base(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/models/legacy.rb",
        "class Legacy < ActiveRecord::Base\nend\n",
    )
    ctx = _ctx(repo_path=tmp_path, tracked_files=["app/models/legacy.rb"])
    out = RailsModelsExtractor().extract(ctx)
    assert any(a.name == "legacy" for a in out)


def test_extract_skips_application_record_base_file(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/models/application_record.rb",
        "class ApplicationRecord < ActiveRecord::Base\nend\n",
    )
    _write(
        tmp_path / "app/models/user.rb",
        "class User < ApplicationRecord\nend\n",
    )
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=[
            "app/models/application_record.rb",
            "app/models/user.rb",
        ],
    )
    out = RailsModelsExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "user" in slugs
    # ApplicationRecord is the framework base, never a feature.
    assert "application-record" not in slugs


def test_extract_skips_non_active_record_classes(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/models/concerns/searchable.rb",
        """
module Searchable
  extend ActiveSupport::Concern
end
""".strip(),
    )
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["app/models/concerns/searchable.rb"],
    )
    out = RailsModelsExtractor().extract(ctx)
    assert out == []


def test_extract_associations_appear_in_rationale(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/models/user.rb",
        """
class User < ApplicationRecord
  has_many :posts
  belongs_to :account
end
""".strip(),
    )
    ctx = _ctx(repo_path=tmp_path, tracked_files=["app/models/user.rb"])
    out = RailsModelsExtractor().extract(ctx)
    user_anchor = next(a for a in out if a.name == "user")
    assert "associations" in user_anchor.rationale


def test_anchor_source_tag(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/models/user.rb",
        "class User < ApplicationRecord\nend\n",
    )
    ctx = _ctx(repo_path=tmp_path, tracked_files=["app/models/user.rb"])
    out = RailsModelsExtractor().extract(ctx)
    for a in out:
        assert a.source == "rails-models"
