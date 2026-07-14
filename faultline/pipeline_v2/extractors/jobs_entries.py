"""JobsEntryExtractor — background-job / cron handlers as Stage 1 entries (B67).

Background workers live outside ``routes_index``, so their flows/journeys
never mint and whole system capabilities (sync / digest / cleanup /
billing-cron) stay invisible (B63 meter: 192 unseen — twenty 85, plane 30,
onyx 24, ...). This extractor walks the repo for background-job SIGNATURES and
emits, per handler, one :class:`AnchorCandidate` carrying an explicit
``routes`` tuple ``(identity, method, file)`` with a synthetic ``JOB`` / ``CRON``
method (the same synthetic-method idiom as the existing ``PAGE`` route method).
``build_routes_index`` Pass A folds any extractor's ``.routes`` into
``routes_index``; ``system_flows.classify_routes`` then stamps each entry's
``trigger`` from the handler file's own library markers — so the two compose
without this module touching the system-flows layer.

Segments (each a separate commit, ONE flag):
  * Seg A — TS/JS: ``@Processor`` (NestJS/BullMQ), ``new Worker(...)`` (BullMQ),
    ``cron.schedule(...)`` (node-cron), ``agenda.define(...)``.
  * Seg B — Python: celery ``@shared_task`` / ``@app.task``, APScheduler,
    rq ``@job`` (added in its own commit).
  * Seg C — manifest-cron: ``vercel.json`` crons[], GitHub Actions schedules,
    k8s ``CronJob`` (added in its own commit).

Flag ``FAULTLINE_JOBS_ENTRIES`` — default OFF. Unset/``0`` -> ``extract``
returns ``[]`` and the scan is byte-identical to pre-B67 (kill-switch unit).

B64 literal law: identity is taken only from a STATIC token — a class/function
name (always literal) or a string-literal queue/task name. A queue/task name
that is a variable or member expression is an honest skip for the *name meta*
only; the entry is still emitted keyed on the class/function name. Truly
dynamic registrations (no static class/function) emit nothing.

No LLM. No network. Read-only.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    has_any_suffix,
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.stage_6_9_test_strip import is_test_path

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


JOBS_ENTRIES_ENV = "FAULTLINE_JOBS_ENTRIES"

#: Bounded per-file read — handler modules are small; the cap only guards
#: pathological blobs (mirrors ``lazy_imports._MAX_BYTES``).
_MAX_BYTES = 1_500_000


def jobs_entries_enabled() -> bool:
    """``True`` when ``FAULTLINE_JOBS_ENTRIES`` is set truthy (default OFF).

    Unset/``0`` keeps the extractor inert (``extract`` -> ``[]``), so every
    scan is byte-identical to pre-B67 (no candidates -> no routes_index rows ->
    nothing downstream moves)."""
    return os.environ.get(JOBS_ENTRIES_ENV, "0").strip().lower() not in {
        "", "0", "false", "no", "off",
    }


# ── config ──────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _cfg() -> dict:
    """Grammar vocabulary from ``stacks/background-jobs.yaml`` (cached)."""
    return load_stack_yaml("background-jobs")


def _confidence() -> float:
    return float(_cfg().get("confidence") or 0.85)


@lru_cache(maxsize=1)
def _skip_segments() -> frozenset[str]:
    return frozenset(
        str(s).lower() for s in (_cfg().get("skip_path_segments") or ())
    )


@lru_cache(maxsize=1)
def _skip_filename_markers() -> frozenset[str]:
    return frozenset(
        str(s).lower() for s in (_cfg().get("skip_filename_markers") or ())
    )


def _should_skip_path(path: str) -> bool:
    """``True`` for a test/mock/fixture file (shared predicate) OR an artifact
    class the predicate does not cover (storybook / examples / playground /
    demo / sample / generated). Segment match is EXACT — never a substring."""
    p = posix(path).lower()
    if is_test_path(p):
        return True
    segs = p.split("/")
    if any(seg in _skip_segments() for seg in segs[:-1]):
        return True
    base = segs[-1] if segs else ""
    dotparts = base.split(".")
    # dot-component markers (foo.stories.ts -> component "stories")
    if len(dotparts) >= 2 and any(
        comp in _skip_filename_markers() for comp in dotparts[1:-1]
    ):
        return True
    return False


# ── job record ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Job:
    """One detected background-job handler (pre-emission)."""

    slug: str          # capability slug (identity, kebab-case)
    method: str        # synthetic route method: "JOB" | "CRON"
    file: str          # repo-relative POSIX handler file
    grammar: str       # which grammar fired (rationale/provenance)
    queue_meta: str = ""  # literal queue/task name when one was resolvable


def _file_stem(path: str) -> str:
    base = posix(path).rsplit("/", 1)[-1]
    return base.split(".")[0]


def _strip_suffixes(name: str, suffixes: tuple[str, ...]) -> str:
    """Peel trailing capability-noise suffixes (Job/Worker/Cron/...).

    Repeated so ``MarketplaceCatalogSyncCronJob`` -> ``CronJob`` peeled as one
    token first, then any residual ``Cron`` — order in the YAML puts compound
    suffixes (``CronJob``) before their parts."""
    changed = True
    while changed:
        changed = False
        for suf in suffixes:
            if name.endswith(suf) and len(name) > len(suf):
                name = name[: -len(suf)]
                changed = True
                break
    return name


# ── Seg A — TS / JS ──────────────────────────────────────────────────────────


@dataclass
class _TsGrammars:
    processor_decorator: re.Pattern[str]
    processor_class: re.Pattern[str]
    processor_queue_literal: re.Pattern[str]
    processor_cron_hint: re.Pattern[str]
    processor_method: str
    worker_require: re.Pattern[str]
    worker_call: re.Pattern[str]
    worker_method: str
    node_cron_require: re.Pattern[str]
    node_cron_call: re.Pattern[str]
    node_cron_method: str
    agenda_call: re.Pattern[str]
    agenda_method: str
    extensions: tuple[str, ...]
    suffixes: tuple[str, ...]


@lru_cache(maxsize=1)
def _ts_grammars() -> _TsGrammars | None:
    block = _cfg().get("ts_js")
    if not isinstance(block, dict):
        return None
    g = block.get("grammars") or {}
    proc = g.get("processor") or {}
    worker = g.get("bullmq_worker") or {}
    ncron = g.get("node_cron") or {}
    agenda = g.get("agenda") or {}

    def _c(pat: str | None) -> re.Pattern[str]:
        return re.compile(pat or r"(?!x)x")  # never-match placeholder when absent

    return _TsGrammars(
        processor_decorator=_c(proc.get("decorator_re")),
        processor_class=_c(proc.get("class_re")),
        processor_queue_literal=_c(proc.get("queue_literal_re")),
        processor_cron_hint=_c(proc.get("cron_hint_re")),
        processor_method=str(proc.get("method") or "JOB"),
        worker_require=_c(worker.get("require_import_re")),
        worker_call=_c(worker.get("call_re")),
        worker_method=str(worker.get("method") or "JOB"),
        node_cron_require=_c(ncron.get("require_import_re")),
        node_cron_call=_c(ncron.get("call_re")),
        node_cron_method=str(ncron.get("method") or "CRON"),
        agenda_call=_c(agenda.get("call_re")),
        agenda_method=str(agenda.get("method") or "JOB"),
        extensions=tuple(str(e) for e in (block.get("extensions") or ())),
        suffixes=tuple(str(s) for s in (block.get("suffix_strip") or ())),
    )


def _collect_ts_js(text: str, path: str) -> list[_Job]:
    gr = _ts_grammars()
    if gr is None:
        return []
    jobs: list[_Job] = []
    filename = posix(path).rsplit("/", 1)[-1].lower()
    is_cron_file = ".cron." in filename

    # 1) @Processor(...) on a class (NestJS / BullMQ).
    for dm in gr.processor_decorator.finditer(text):
        cm = gr.processor_class.search(text, dm.end())
        if cm is None:
            continue  # decorator with no following class -> not a static handler
        classname = cm.group(1)
        identity = _strip_suffixes(classname, gr.suffixes)
        slug = slugify(identity)
        if not slug:
            continue
        cron = is_cron_file or bool(gr.processor_cron_hint.search(text))
        method = "CRON" if cron else gr.processor_method
        qm = gr.processor_queue_literal.search(text, dm.start(), cm.start())
        queue_meta = qm.group(1) if qm else ""
        jobs.append(_Job(slug, method, path, "processor", queue_meta))

    # 2) new Worker("queue", handler) — BullMQ only (import-corroborated).
    if gr.worker_require.search(text):
        for wm in gr.worker_call.finditer(text):
            queue = wm.group(1)
            slug = slugify(_strip_suffixes(queue, gr.suffixes))
            if not slug:
                continue
            jobs.append(_Job(slug, gr.worker_method, path, "bullmq-worker", queue))

    # 3) cron.schedule("* * * * *", ...) — node-cron (import-corroborated).
    if gr.node_cron_require.search(text):
        for nm in gr.node_cron_call.finditer(text):
            var = nm.group(1)
            identity = var or _file_stem(path)
            slug = slugify(_strip_suffixes(identity, gr.suffixes))
            if not slug:
                continue
            jobs.append(_Job(slug, gr.node_cron_method, path, "node-cron", ""))

    # 4) agenda.define("job name", ...) / bree.
    for am in gr.agenda_call.finditer(text):
        slug = slugify(am.group(1))
        if not slug:
            continue
        jobs.append(_Job(slug, gr.agenda_method, path, "agenda", am.group(1)))

    return jobs


# ── Seg B — Python ───────────────────────────────────────────────────────────


@dataclass
class _PyGrammars:
    celery_require: re.Pattern[str]
    celery_decorator: re.Pattern[str]
    celery_def: re.Pattern[str]
    celery_name_literal: re.Pattern[str]
    celery_method: str
    aps_require: re.Pattern[str]
    aps_decorator: re.Pattern[str]
    aps_def: re.Pattern[str]
    aps_add_job: re.Pattern[str]
    aps_method: str
    rq_require: re.Pattern[str]
    rq_decorator: re.Pattern[str]
    rq_def: re.Pattern[str]
    rq_method: str
    dq_require: re.Pattern[str]
    dq_async_task: re.Pattern[str]
    dq_schedule: re.Pattern[str]
    extensions: tuple[str, ...]
    suffixes: tuple[str, ...]


@lru_cache(maxsize=1)
def _py_grammars() -> _PyGrammars | None:
    block = _cfg().get("python")
    if not isinstance(block, dict):
        return None
    g = block.get("grammars") or {}
    cel = g.get("celery") or {}
    aps = g.get("apscheduler") or {}
    rq = g.get("rq") or {}
    dq = g.get("django_q") or {}

    def _c(pat: str | None) -> re.Pattern[str]:
        return re.compile(pat or r"(?!x)x")

    return _PyGrammars(
        celery_require=_c(cel.get("require_import_re")),
        celery_decorator=_c(cel.get("decorator_re")),
        celery_def=_c(cel.get("def_re")),
        celery_name_literal=_c(cel.get("name_literal_re")),
        celery_method=str(cel.get("method") or "JOB"),
        aps_require=_c(aps.get("require_import_re")),
        aps_decorator=_c(aps.get("decorator_re")),
        aps_def=_c(aps.get("def_re")),
        aps_add_job=_c(aps.get("add_job_re")),
        aps_method=str(aps.get("method") or "CRON"),
        rq_require=_c(rq.get("require_import_re")),
        rq_decorator=_c(rq.get("decorator_re")),
        rq_def=_c(rq.get("def_re")),
        rq_method=str(rq.get("method") or "JOB"),
        dq_require=_c(dq.get("require_import_re")),
        dq_async_task=_c(dq.get("async_task_re")),
        dq_schedule=_c(dq.get("schedule_re")),
        extensions=tuple(str(e) for e in (block.get("extensions") or ())),
        suffixes=tuple(str(s) for s in (block.get("suffix_strip") or ())),
    )


def _last_segment(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]


def _decorated_def_jobs(
    text: str,
    path: str,
    decorator: re.Pattern[str],
    def_re: re.Pattern[str],
    method: str,
    grammar: str,
    suffixes: tuple[str, ...],
    name_literal: re.Pattern[str] | None = None,
) -> list[_Job]:
    """One job per ``def`` that FOLLOWS a matching decorator.

    The decorator may span lines (``@shared_task(\\n name=..., \\n)``); the
    first ``def NAME(`` after the decorator names the handler."""
    jobs: list[_Job] = []
    for dm in decorator.finditer(text):
        fn = def_re.search(text, dm.end())
        if fn is None:
            continue
        identity = _strip_suffixes(fn.group(1), suffixes)
        slug = slugify(identity)
        if not slug:
            continue
        queue_meta = ""
        if name_literal is not None:
            nm = name_literal.search(text, dm.start(), fn.start())
            if nm:
                queue_meta = nm.group(1)
        jobs.append(_Job(slug, method, path, grammar, queue_meta))
    return jobs


def _collect_python(text: str, path: str) -> list[_Job]:
    gr = _py_grammars()
    if gr is None:
        return []
    jobs: list[_Job] = []

    # Celery @shared_task / @app.task (decorator -> def).
    if gr.celery_require.search(text):
        jobs.extend(_decorated_def_jobs(
            text, path, gr.celery_decorator, gr.celery_def,
            gr.celery_method, "celery", gr.suffixes, gr.celery_name_literal,
        ))

    # APScheduler @scheduled_job (decorator -> def) + scheduler.add_job(func,…).
    if gr.aps_require.search(text):
        jobs.extend(_decorated_def_jobs(
            text, path, gr.aps_decorator, gr.aps_def,
            gr.aps_method, "apscheduler", gr.suffixes,
        ))
        for am in gr.aps_add_job.finditer(text):
            slug = slugify(_strip_suffixes(_last_segment(am.group(1)), gr.suffixes))
            if slug:
                jobs.append(_Job(slug, gr.aps_method, path, "apscheduler", ""))

    # RQ @job (decorator -> def) — strict rq import.
    if gr.rq_require.search(text):
        jobs.extend(_decorated_def_jobs(
            text, path, gr.rq_decorator, gr.rq_def,
            gr.rq_method, "rq", gr.suffixes,
        ))

    # django-q async_task("mod.func") / schedule("mod.func") — strict import.
    if gr.dq_require.search(text):
        for qm in gr.dq_async_task.finditer(text):
            slug = slugify(_strip_suffixes(_last_segment(qm.group(1)), gr.suffixes))
            if slug:
                jobs.append(_Job(slug, "JOB", path, "django-q", qm.group(1)))
        for qm in gr.dq_schedule.finditer(text):
            slug = slugify(_strip_suffixes(_last_segment(qm.group(1)), gr.suffixes))
            if slug:
                jobs.append(_Job(slug, "CRON", path, "django-q", qm.group(1)))

    return jobs


# ── extractor ────────────────────────────────────────────────────────────────


# Segment collectors: (predicate on posix-path, collector(text, path)).
# Grows one entry per segment commit. Order is informational only.
def _segment_collectors() -> list:
    out: list = []
    ts = _ts_grammars()
    if ts is not None and ts.extensions:
        out.append((
            lambda p, exts=ts.extensions: has_any_suffix(p, exts),
            _collect_ts_js,
        ))
    py = _py_grammars()
    if py is not None and py.extensions:
        out.append((
            lambda p, exts=py.extensions: has_any_suffix(p, exts),
            _collect_python,
        ))
    return out


class JobsEntryExtractor:
    """Background-job / cron handlers -> routes_index entries (B67)."""

    name = "jobs-entry"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not jobs_entries_enabled():
            return []

        collectors = _segment_collectors()
        if not collectors:
            return []

        jobs: list[_Job] = []
        for raw in ctx.tracked_files:
            path = posix(raw)
            if _should_skip_path(path):
                continue
            active = [c for pred, c in collectors if pred(path)]
            if not active:
                continue
            text = read_text(ctx.repo_path / path)
            if not text or len(text) > _MAX_BYTES:
                continue
            for collect in active:
                jobs.extend(collect(text, path))

        return _emit(jobs)


def _emit(jobs: list[_Job]) -> list[AnchorCandidate]:
    """Dedup by (file, method, slug) and emit one AnchorCandidate per job.

    Deterministic: iterate a sorted key so the emitted order (and everything
    downstream derives from it) is stable across runs."""
    conf = _confidence()
    seen: dict[tuple[str, str, str], _Job] = {}
    for j in jobs:
        seen.setdefault((j.file, j.method, j.slug), j)

    out: list[AnchorCandidate] = []
    for key in sorted(seen):
        j = seen[key]
        meta = f" (queue {j.queue_meta!r})" if j.queue_meta else ""
        out.append(
            AnchorCandidate(
                name=j.slug,
                paths=(j.file,),
                source=JobsEntryExtractor.name,
                confidence_self=conf,
                routes=((f"/{j.slug}", j.method, j.file),),
                rationale=(
                    f"{j.grammar} background job {j.slug!r} "
                    f"[{j.method}]{meta} in {j.file}"
                ),
            ),
        )
    return out


__all__ = ["JobsEntryExtractor", "jobs_entries_enabled", "JOBS_ENTRIES_ENV"]
