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


def test_off_not_registered_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF byte-identity at the REGISTRY surface: scan_meta.extractor_hits
    serializes every registered source key, so with the flag unset the
    extractor must not even REGISTER (kill-switch lesson: the sole A/B diff
    was ``extractor_hits.jobs-entry`` ONLY-IN-B). With the flag set it must
    appear."""
    from faultline.pipeline_v2.stage_1_extractors import (
        _load_default_extractors,
    )

    monkeypatch.delenv(JOBS_ENTRIES_ENV, raising=False)
    names_off = {e.name for e in _load_default_extractors()}
    assert "jobs-entry" not in names_off

    monkeypatch.setenv(JOBS_ENTRIES_ENV, "1")
    names_on = {e.name for e in _load_default_extractors()}
    assert "jobs-entry" in names_on
    # the rest of the registry is unchanged by the flag
    assert names_on - {"jobs-entry"} == names_off


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


# ── Seg D — Trigger.dev v3 ───────────────────────────────────────────────────


def test_trigger_dev_task_literal_id(tmp_path: Path, jobs_on) -> None:
    """papermark: ``task({id: "...", run})`` with @trigger.dev/sdk import ->
    JOB entry named by the author's declared id."""
    rel = _write(
        tmp_path,
        "ee/features/billing/cancellation/lib/trigger/pause-resume-notification.ts",
        'import { logger, task } from "@trigger.dev/sdk";\n\n'
        "export const sendPauseResumeNotificationTask = task({\n"
        '  id: "send-pause-resume-notification",\n'
        "  retry: { maxAttempts: 3 },\n"
        "  run: async (payload) => {},\n"
        "});\n",
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="next"))
    assert a.name == "send-pause-resume-notification"
    assert a.routes == (("/send-pause-resume-notification", "JOB", rel),)


def test_trigger_dev_schedules_task_is_cron(tmp_path: Path, jobs_on) -> None:
    """midday: ``schedules.task({id})`` -> CRON entry; the bare-task grammar
    must NOT double-fire on the same call."""
    rel = _write(
        tmp_path,
        "packages/jobs/src/tasks/bank/scheduler/bank-scheduler.ts",
        'import { logger, schedules } from "@trigger.dev/sdk";\n\n'
        "export const bankSyncScheduler = schedules.task({\n"
        '  id: "bank-sync-scheduler",\n'
        "  maxDuration: 120,\n"
        "  run: async (payload) => {},\n"
        "});\n",
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="next"))
    assert a.name == "bank-sync-scheduler"
    assert a.routes == (("/bank-sync-scheduler", "CRON", rel),)


def test_trigger_dev_member_expr_id_falls_back_to_binding(
    tmp_path: Path, jobs_on
) -> None:
    """ANTI-CASE (lead-ruled): a member-expr id is an honest meta-skip — the
    entry falls back to the bound const name (Task suffix stripped), and no
    id meta leaks into the rationale (B64 literal law)."""
    rel = _write(
        tmp_path,
        "lib/trigger/bulk-download.ts",
        'import { task } from "@trigger.dev/sdk";\n\n'
        "export const bulkDownloadTask = task({\n"
        "  id: TASK_IDS.bulkDownload,\n"
        "  run: async () => {},\n"
        "});\n",
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="next"))
    assert a.name == "bulk-download"
    assert "queue" not in a.rationale


def test_trigger_dev_dynamic_id_anonymous_is_skipped(
    tmp_path: Path, jobs_on
) -> None:
    """ANTI-CASE: member-expr id AND no const binding -> no static token ->
    honest full skip."""
    rel = _write(
        tmp_path,
        "lib/trigger/x.ts",
        'import { task } from "@trigger.dev/sdk";\n'
        "register(task({ id: TASK_IDS.x, run: async () => {} }));\n",
    )
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="next")) == []


def test_trigger_dev_without_import_is_skipped(tmp_path: Path, jobs_on) -> None:
    """ANTI-CASE: ``task({id})`` with no @trigger.dev import -> skip (generic
    ``task(`` calls are everywhere)."""
    rel = _write(
        tmp_path,
        "src/x.ts",
        'export const t = task({ id: "not-trigger", run: async () => {} });\n',
    )
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="next")) == []


def test_trigger_dev_wrapped_calls_never_match(tmp_path: Path, jobs_on) -> None:
    """ANTI-CASE: ``myTask({id})`` / ``obj.task({id})`` never match the bare
    task grammar (negative lookbehind)."""
    rel = _write(
        tmp_path,
        "src/y.ts",
        'import { x } from "@trigger.dev/sdk";\n'
        'const a = myTask({ id: "nope" });\n'
        'obj.task({ id: "nope2" });\n',
    )
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="next")) == []


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


# ── Seg C — manifest cron: vercel / GitHub Actions / k8s ─────────────────────


def test_vercel_cron_on_existing_route_is_marker_not_entry(
    tmp_path: Path, jobs_on
) -> None:
    """SACRED dup anti-case: a vercel crons[] path that resolves to a tracked
    route file emits NO entry (system_flows marks the existing route)."""
    import json

    _write(
        tmp_path,
        "vercel.json",
        json.dumps({"crons": [{"path": "/api/cron/digest", "schedule": "0 0 * * *"}]}),
    )
    route = _write(tmp_path, "app/api/cron/digest/route.ts", "export function GET(){}\n")
    anchors = JobsEntryExtractor().extract(
        _ctx(tmp_path, ["vercel.json", route], stack="next")
    )
    # No jobs-entry row for the already-routed cron path.
    assert all(a.source == "jobs-entry" for a in anchors)
    assert not any(
        "/api/cron/digest" in r[0] or "digest" == a.name
        for a in anchors for r in a.routes
    )


def test_vercel_cron_orphan_target_gets_entry(tmp_path: Path, jobs_on) -> None:
    """A vercel cron path with NO tracked route file (orphan) DOES get an
    entry so the scheduled capability is not lost."""
    import json

    _write(
        tmp_path,
        "vercel.json",
        json.dumps({"crons": [{"path": "/api/cron/orphan-sweep", "schedule": "*/5 * * * *"}]}),
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, ["vercel.json"], stack="next"))
    assert a.routes[0][1] == "CRON"
    assert "orphan-sweep" in a.name


def test_github_actions_scheduled_workflow(tmp_path: Path, jobs_on) -> None:
    rel = _write(
        tmp_path,
        ".github/workflows/nightly.yml",
        "name: Nightly DB Backup\n"
        "on:\n"
        "  schedule:\n"
        '    - cron: "0 3 * * *"\n'
        "jobs:\n  run:\n    runs-on: ubuntu-latest\n",
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="next"))
    assert a.name == "nightly-db-backup"
    assert a.routes == (("/nightly-db-backup", "CRON", rel),)


def test_github_actions_non_scheduled_is_skipped(tmp_path: Path, jobs_on) -> None:
    """ANTI-CASE: a push-triggered workflow is not a scheduled entry."""
    rel = _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "name: CI\non:\n  push:\n    branches: [main]\njobs: {}\n",
    )
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="next")) == []


def test_k8s_cronjob(tmp_path: Path, jobs_on) -> None:
    rel = _write(
        tmp_path,
        "deploy/cronjob.yaml",
        "apiVersion: batch/v1\nkind: CronJob\n"
        "metadata:\n  name: cleanup-orphans\n"
        'spec:\n  schedule: "0 * * * *"\n',
    )
    (a,) = JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="go"))
    assert a.name == "cleanup-orphans"
    assert a.routes[0][1] == "CRON"


def test_k8s_non_cronjob_is_skipped(tmp_path: Path, jobs_on) -> None:
    """ANTI-CASE: a Deployment manifest is not a scheduled entry."""
    rel = _write(
        tmp_path,
        "deploy/web.yaml",
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web\n",
    )
    assert JobsEntryExtractor().extract(_ctx(tmp_path, [rel], stack="go")) == []


# ── per-workspace merge: twin-slug routes preservation ───────────────────────


def _twin_candidates():
    from faultline.pipeline_v2.extractors.base import AnchorCandidate

    a = AnchorCandidate(
        name="calendar-event-list-fetch",
        paths=("pkg/srv/crons/jobs/calendar-event-list-fetch.cron.job.ts",),
        source="jobs-entry",
        confidence_self=0.85,
        routes=(("/calendar-event-list-fetch", "CRON",
                 "pkg/srv/crons/jobs/calendar-event-list-fetch.cron.job.ts"),),
    )
    b = AnchorCandidate(
        name="calendar-event-list-fetch",
        paths=("pkg/srv/jobs/calendar-event-list-fetch.job.ts",),
        source="jobs-entry",
        confidence_self=0.85,
        routes=(("/calendar-event-list-fetch", "JOB",
                 "pkg/srv/jobs/calendar-event-list-fetch.job.ts"),),
    )
    return a, b


def test_ws_merge_preserves_twin_routes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """twenty forensics class: same-slug cron/job twins coalesce in the
    per-workspace merge and historically LOST their explicit routes (22 of 27
    dropped rows). Flag ON -> the coalesced candidate carries the routes
    union."""
    from faultline.pipeline_v2.stage_1_per_workspace import (
        _merge_anchors_across_workspaces,
    )

    monkeypatch.setenv(JOBS_ENTRIES_ENV, "1")
    a, b = _twin_candidates()
    merged = _merge_anchors_across_workspaces([("srv", {"jobs-entry": [a, b]})])
    (cand,) = merged["jobs-entry"]
    assert set(cand.routes) == set(a.routes) | set(b.routes)
    assert set(cand.paths) == set(a.paths) | set(b.paths)


def test_ws_merge_legacy_drop_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag OFF -> byte-identity: the coalesce keeps the LEGACY behavior
    (routes dropped), so OFF-world boards are unchanged."""
    from faultline.pipeline_v2.stage_1_per_workspace import (
        _merge_anchors_across_workspaces,
    )

    monkeypatch.delenv(JOBS_ENTRIES_ENV, raising=False)
    a, b = _twin_candidates()
    merged = _merge_anchors_across_workspaces([("srv", {"jobs-entry": [a, b]})])
    (cand,) = merged["jobs-entry"]
    assert cand.routes == ()


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
