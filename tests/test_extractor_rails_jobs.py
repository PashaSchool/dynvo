"""RailsJobsExtractor unit tests."""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.rails_jobs import RailsJobsExtractor


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
        tracked_files=["app/jobs/welcome_email_job.rb"],
        audited_stack="next-app-router",
    )
    assert RailsJobsExtractor().extract(ctx) == []


def test_extract_application_job_subclass(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/jobs/welcome_email_job.rb",
        "class WelcomeEmailJob < ApplicationJob\nend\n",
    )
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["app/jobs/welcome_email_job.rb"],
    )
    out = RailsJobsExtractor().extract(ctx)
    slugs = {a.name for a in out}
    # "Job" suffix stripped before slugify.
    assert "welcome-email" in slugs


def test_extract_sidekiq_worker(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/jobs/data_sync_worker.rb",
        "class DataSyncWorker\n  include Sidekiq::Worker\nend\n",
    )
    # The Sidekiq pattern in YAML matches `< Sidekiq::Worker` style.
    _write(
        tmp_path / "app/jobs/report_job.rb",
        "class ReportJob < Sidekiq::Job\nend\n",
    )
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=[
            "app/jobs/data_sync_worker.rb",
            "app/jobs/report_job.rb",
        ],
    )
    out = RailsJobsExtractor().extract(ctx)
    slugs = {a.name for a in out}
    # `< Sidekiq::Job` style matches the regex; `include` form does not
    # (intentional — those are common false-positives in helper files).
    assert "report" in slugs


def test_extract_active_job_base(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/jobs/legacy_job.rb",
        "class LegacyJob < ActiveJob::Base\nend\n",
    )
    ctx = _ctx(repo_path=tmp_path, tracked_files=["app/jobs/legacy_job.rb"])
    out = RailsJobsExtractor().extract(ctx)
    assert any(a.name == "legacy" for a in out)


def test_extract_skips_application_job_base(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/jobs/application_job.rb",
        "class ApplicationJob < ActiveJob::Base\nend\n",
    )
    _write(
        tmp_path / "app/jobs/welcome_job.rb",
        "class WelcomeJob < ApplicationJob\nend\n",
    )
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=[
            "app/jobs/application_job.rb",
            "app/jobs/welcome_job.rb",
        ],
    )
    out = RailsJobsExtractor().extract(ctx)
    slugs = {a.name for a in out}
    assert "welcome" in slugs
    assert "application" not in slugs


def test_anchor_source_tag(tmp_path: Path) -> None:
    _write(
        tmp_path / "app/jobs/x_job.rb",
        "class XJob < ApplicationJob\nend\n",
    )
    ctx = _ctx(repo_path=tmp_path, tracked_files=["app/jobs/x_job.rb"])
    out = RailsJobsExtractor().extract(ctx)
    for a in out:
        assert a.source == "rails-jobs"
