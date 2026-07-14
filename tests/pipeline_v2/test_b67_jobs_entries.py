"""B67 — background-job / cron entry extractor. Seg A (TS/JS) unit pack.

Covers the mechanism + the SACRED anti-cases (spec §"SACRED анти-кейси"):
  * flag default OFF (kill-switch) + byte-identical inert when unset;
  * @Processor (NestJS/BullMQ) class -> CRON / JOB entry, class-name identity
    even when the queue arg is a member expr (B64 literal law);
  * BullMQ ``new Worker("q", …)`` (import-corroborated) vs a DOM/web Worker
    (no bullmq import) -> honest skip;
  * node-cron ``cron.schedule`` (import-corroborated) vs no import -> skip;
  * agenda ``define`` literal-name entry;
  * test / storybook / example files -> NOT entries (test-strip law);
  * ``.routes`` tuples flow into ``build_routes_index`` with the JOB/CRON method.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.extractors.jobs_entries import (
    JOBS_ENTRIES_ENV,
    JobsEntryExtractor,
    jobs_entries_enabled,
)
from faultline.pipeline_v2.indexes import build_routes_index
from faultline.pipeline_v2.stage_0_intake import ScanContext


def _ctx(repo: Path, files: list[str], **kw) -> ScanContext:
    return ScanContext(
        repo_path=repo,
        stack=kw.get("stack", "nestjs"),
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
        secondary_stacks=kw.get("secondary_stacks", ()),
        audited_stack=kw.get("audited_stack"),
    )


@pytest.fixture
def jobs_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(JOBS_ENTRIES_ENV, "1")


def _write(tmp_path: Path, rel: str, body: str) -> str:
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    return rel


# ── flag / kill-switch ───────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(JOBS_ENTRIES_ENV, raising=False)
    assert jobs_entries_enabled() is False
    for falsy in ("0", "false", "off", "no", ""):
        monkeypatch.setenv(JOBS_ENTRIES_ENV, falsy)
        assert jobs_entries_enabled() is False, falsy
    for truthy in ("1", "true", "True", "yes", "on"):
        monkeypatch.setenv(JOBS_ENTRIES_ENV, truthy)
        assert jobs_entries_enabled() is True, truthy


def test_off_is_inert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset flag -> zero candidates even with a real job file present."""
    monkeypatch.delenv(JOBS_ENTRIES_ENV, raising=False)
    rel = _write(
        tmp_path,
        "src/jobs/cleanup.job.ts",
        'import { Processor } from "x";\n'
        "@Processor(MessageQueue.cronQueue)\n"
        "export class CleanupJob {}\n",
    )
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel])) == []


# ── @Processor (NestJS / BullMQ) ─────────────────────────────────────────────


def test_processor_cron_job_member_expr_queue(tmp_path: Path, jobs_on) -> None:
    """twenty's marketplace-catalog-sync: @Processor(MessageQueue.cronQueue) on
    a ``.cron.job.ts`` -> CRON entry keyed on the class name; the member-expr
    queue is an honest skip for the meta (B64 literal law)."""
    rel = _write(
        tmp_path,
        "packages/twenty-server/src/engine/core-modules/application/"
        "application-marketplace/crons/marketplace-catalog-sync.cron.job.ts",
        'import { Injectable } from "@nestjs/common";\n'
        'import { Processor } from "src/.../processor.decorator";\n'
        "@Injectable()\n"
        "@Processor(MessageQueue.cronQueue)\n"
        "export class MarketplaceCatalogSyncCronJob {\n"
        "  @Process(MarketplaceCatalogSyncCronJob.name)\n"
        "  @SentryCronMonitor(MarketplaceCatalogSyncCronJob.name, CRON_PATTERN)\n"
        "  async handle() {}\n"
        "}\n",
    )
    anchors = JobsEntryExtractor().extract(_ctx(tmp_path, [rel]))
    assert len(anchors) == 1
    a = anchors[0]
    assert a.name == "marketplace-catalog-sync"
    assert a.source == "jobs-entry"
    assert a.paths == (rel,)
    assert a.routes == (("/marketplace-catalog-sync", "CRON", rel),)
    # member-expr queue -> no literal meta leaked into the rationale
    assert "queue" not in a.rationale


def test_processor_plain_job_object_queue(tmp_path: Path, jobs_on) -> None:
    """@Processor({queueName: MessageQueue.billingQueue}) on a ``.job.ts`` (no
    cron hint) -> JOB entry."""
    rel = _write(
        tmp_path,
        "packages/twenty-server/src/engine/core-modules/billing/jobs/"
        "update-subscription-quantity.job.ts",
        'import { Processor } from "x";\n'
        "@Processor({ queueName: MessageQueue.billingQueue, scope: Scope.REQUEST })\n"
        "export class UpdateSubscriptionQuantityJob {}\n",
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel]))
    assert a.name == "update-subscription-quantity"
    assert a.routes == (("/update-subscription-quantity", "JOB", rel),)


def test_processor_string_literal_queue_meta(tmp_path: Path, jobs_on) -> None:
    """A STRING-LITERAL queue is captured as meta (still class-name identity)."""
    rel = _write(
        tmp_path,
        "src/mail/mail-send.job.ts",
        'import { Processor } from "x";\n'
        '@Processor("mail")\n'
        "export class MailSendJob {}\n",
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel]))
    assert a.name == "mail-send"
    assert "'mail'" in a.rationale  # literal queue meta present


# ── BullMQ Worker ────────────────────────────────────────────────────────────


def test_bullmq_worker_literal_queue(tmp_path: Path, jobs_on) -> None:
    """rybbit: ``new Worker("monitor-checks", …)`` with a bullmq import."""
    rel = _write(
        tmp_path,
        "server/src/services/uptime/monitorExecutor.ts",
        'import { Worker, Job } from "bullmq";\n'
        'this.worker = new Worker("monitor-checks", async (job) => {});\n',
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel]))
    assert a.name == "monitor-checks"
    assert a.routes == (("/monitor-checks", "JOB", rel),)


def test_web_worker_without_bullmq_import_is_skipped(tmp_path: Path, jobs_on) -> None:
    """ANTI-CASE: a DOM/web Worker (no bullmq import) is NOT a background job."""
    rel = _write(
        tmp_path,
        "src/app/sandbox/runner.ts",
        'const w = new Worker(new URL("./worker.js", import.meta.url));\n',
    )
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel])) == []


# ── node-cron ────────────────────────────────────────────────────────────────


def test_node_cron_bound_var(tmp_path: Path, jobs_on) -> None:
    rel = _write(
        tmp_path,
        "server/src/services/reports.ts",
        'import cron from "node-cron";\n'
        'const dailyReport = cron.schedule("0 0 * * *", () => {});\n',
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel]))
    assert a.name == "daily-report"
    assert a.routes == (("/daily-report", "CRON", rel),)


def test_node_cron_without_import_is_skipped(tmp_path: Path, jobs_on) -> None:
    """ANTI-CASE: ``cron.schedule`` with no node-cron import -> honest skip."""
    rel = _write(
        tmp_path,
        "src/misc/x.ts",
        'cron.schedule("* * * * *", fn);\n',
    )
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel])) == []


# ── agenda ───────────────────────────────────────────────────────────────────


def test_agenda_define(tmp_path: Path, jobs_on) -> None:
    rel = _write(
        tmp_path,
        "src/jobs/index.ts",
        'agenda.define("send welcome email", async (job) => {});\n',
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel]))
    assert a.name == "send-welcome-email"
    assert a.routes[0][1] == "JOB"


# ── test-strip / storybook / examples (SACRED) ───────────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        "src/jobs/__tests__/cleanup.job.ts",
        "src/jobs/cleanup.job.spec.ts",
        "src/jobs/cleanup.job.test.ts",
        # twenty's runFrontComponentSandboxIsolationProbe lives under __stories__
        "packages/twenty-front/src/__stories__/utils/probe.job.ts",
        "packages/x/examples/demo.job.ts",
        "packages/x/playground/scratch.job.ts",
        "src/jobs/cleanup.stories.ts",
    ],
)
def test_test_and_artifact_files_are_not_entries(
    tmp_path: Path, jobs_on, rel: str
) -> None:
    _write(
        tmp_path,
        rel,
        'import { Processor } from "x";\n'
        "@Processor(MessageQueue.cronQueue)\n"
        "export class CleanupJob {}\n",
    )
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel])) == []


# ── routes_index integration + determinism ───────────────────────────────────


def test_routes_flow_into_routes_index(tmp_path: Path, jobs_on) -> None:
    rel = _write(
        tmp_path,
        "src/jobs/cleanup.cron.job.ts",
        'import { Processor } from "x";\n'
        "@Processor(MessageQueue.cronQueue)\n"
        "export class CleanupCronJob {}\n",
    )
    ctx = _ctx(tmp_path, [rel])
    signals = {"jobs-entry": JobsEntryExtractor().extract(ctx)}
    routes_index = build_routes_index([], signals)
    rows = [
        r for r in routes_index
        if r.get("file") == rel and r.get("method") == "CRON"
    ]
    assert len(rows) == 1
    assert rows[0]["pattern"] == "/cleanup"


# ── Seg B — Python: celery / APScheduler / rq / django-q ─────────────────────


def test_celery_bare_shared_task(tmp_path: Path, jobs_on) -> None:
    """plane: bare ``@shared_task`` -> JOB entry on the function name."""
    rel = _write(
        tmp_path,
        "apps/api/plane/bgtasks/deletion_task.py",
        "from celery import shared_task\n\n"
        "@shared_task\n"
        "def soft_delete_related_objects(app_label, model_name): ...\n",
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="django"))
    assert a.name == "soft-delete-related-objects"
    assert a.routes == (("/soft-delete-related-objects", "JOB", rel),)


def test_celery_multiline_member_expr_name(tmp_path: Path, jobs_on) -> None:
    """onyx: ``@shared_task(name=OnyxCeleryTask.X, …)`` spanning lines -> entry
    on the function name (``_task`` suffix stripped); member-expr name is an
    honest skip for the meta (B64 law)."""
    rel = _write(
        tmp_path,
        "backend/ee/onyx/background/celery/tasks/cleanup/tasks.py",
        "from celery import shared_task\n\n"
        "@shared_task(\n"
        "    name=OnyxCeleryTask.EXPORT_QUERY_HISTORY_CLEANUP_TASK,\n"
        "    ignore_result=True,\n"
        "    soft_time_limit=JOB_TIMEOUT,\n"
        ")\n"
        "def export_query_history_cleanup_task(*, tenant_id): ...\n",
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="django"))
    assert a.name == "export-query-history-cleanup"
    assert "queue" not in a.rationale


def test_celery_literal_name_meta(tmp_path: Path, jobs_on) -> None:
    rel = _write(
        tmp_path,
        "app/tasks.py",
        "import celery\n\n"
        '@app.task(name="emails.send")\n'
        "def send_email(): ...\n",
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="django"))
    assert a.name == "send-email"
    assert "'emails.send'" in a.rationale


def test_apscheduler_decorator_and_add_job(tmp_path: Path, jobs_on) -> None:
    rel = _write(
        tmp_path,
        "app/scheduler.py",
        "from apscheduler.schedulers.background import BackgroundScheduler\n\n"
        '@scheduled_job("cron", hour=3)\n'
        "def nightly_rollup(): ...\n\n"
        'scheduler.add_job(cleanup_sessions, "interval", minutes=5)\n',
    )
    anchors = JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="fastapi"))
    got = sorted((a.name, a.routes[0][1]) for a in anchors)
    assert got == [("cleanup-sessions", "CRON"), ("nightly-rollup", "CRON")]


def test_rq_job_requires_rq_import(tmp_path: Path, jobs_on) -> None:
    rel = _write(
        tmp_path,
        "app/tasks.py",
        "from rq import job\n\n"
        '@job("default")\n'
        "def resize_image(): ...\n",
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="fastapi"))
    assert a.name == "resize-image"
    assert a.routes[0][1] == "JOB"


def test_rq_job_without_import_is_skipped(tmp_path: Path, jobs_on) -> None:
    """ANTI-CASE: a generic ``@job`` decorator with no rq import -> skip."""
    rel = _write(tmp_path, "app/x.py", '@job("x")\ndef f(): ...\n')
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="fastapi")) == []


def test_django_q_tasks(tmp_path: Path, jobs_on) -> None:
    rel = _write(
        tmp_path,
        "app/services.py",
        "from django_q.tasks import async_task, schedule\n\n"
        'async_task("app.mail.send_welcome")\n'
        'schedule("app.reports.weekly")\n',
    )
    anchors = JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="django"))
    got = sorted((a.name, a.routes[0][1]) for a in anchors)
    assert got == [("send-welcome", "JOB"), ("weekly", "CRON")]


def test_django_q_words_without_import_skipped(tmp_path: Path, jobs_on) -> None:
    """ANTI-CASE: the common word ``schedule(`` with no django_q import -> skip."""
    rel = _write(tmp_path, "app/y.py", 'schedule("a.b.c")\n')
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="django")) == []


def test_python_test_file_is_not_entry(tmp_path: Path, jobs_on) -> None:
    """ANTI-CASE: a celery task inside a test file -> not an entry."""
    rel = _write(
        tmp_path,
        "apps/api/plane/bgtasks/tests/test_deletion.py",
        "from celery import shared_task\n@shared_task\ndef fake_task(): ...\n",
    )
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="django")) == []


def test_deterministic_sorted_emission(tmp_path: Path, jobs_on) -> None:
    files = []
    for stem in ("zeta", "alpha", "mid"):
        files.append(
            _write(
                tmp_path,
                f"src/jobs/{stem}.job.ts",
                'import { Processor } from "x";\n'
                f"@Processor(MessageQueue.q)\nexport class {stem.title()}Job {{}}\n",
            )
        )
    ctx = _ctx(tmp_path, files)
    out1 = [a.name for a in JobsEntryExtractor().extract(ctx)]
    out2 = [a.name for a in JobsEntryExtractor().extract(ctx)]
    assert out1 == out2 == sorted(out1)
