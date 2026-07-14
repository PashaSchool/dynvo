"""Stage 1 — parallel deterministic anchor extractors.

Runs all registered :class:`AnchorExtractor` instances against the
:class:`ScanContext` from Stage 0, in parallel. Extractors are I/O
bound (file reads, manifest parses), so a ``ThreadPoolExecutor`` is
the right primitive — see ``python-architect-developer`` skill.

Discovery model (MERGE, not either/or):

  1. The built-in first-party extractors are ALWAYS present. They
     are loaded directly from this package via
     ``_load_default_extractors`` and can never be dropped by a
     stale, partial, or empty entry-point group.
  2. Python entry-points under ``faultlines.extractors`` ADD any
     third-party / customer extractors on top of the built-ins
     (``~/.faultline/extractors/<custom>.py`` packages). An
     entry-point whose ``name`` collides with a built-in is ignored
     (the in-tree class wins — it is the source of truth).

Why merge instead of "entry-points override defaults": the installed
``*.dist-info/entry_points.txt`` is a SNAPSHOT taken at install time.
On an editable install that hasn't been re-installed since a new
built-in extractor was added (e.g. ``fastapi-route``, the Rails
suite), that snapshot is stale and lists only a subset of the
built-ins. The previous "use entry-points OR fall back to defaults"
logic then silently ran the stale subset and dropped the newer
first-party extractors — producing ``routes_index == 0`` on FastAPI
repos despite the extractor existing in-tree. Merging makes the
built-in set authoritative and immune to that snapshot drift.

Failure handling: each extractor runs inside a try/except. A failing
extractor does NOT kill the orchestrator; the failure is recorded in
the returned ``_errors`` key and the other extractors continue. The
orchestrator does NOT mutate ``scan_meta`` directly — Stage 7 is
responsible for surfacing telemetry to the final FeatureMap.

No LLM calls. No network calls.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import entry_points  # module-level binding so tests can monkeypatch
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.extractors.base import (
    AnchorCandidate,
    AnchorExtractor,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# The single hardcoded fallback list — only used when entry-points
# return nothing. Lazy-imported inside the function so importing this
# module is cheap when callers pass their own extractor list.
_DEFAULT_ENTRY_POINT_GROUP = "faultlines.extractors"


def _load_default_extractors() -> list[AnchorExtractor]:
    """Load the built-in extractors directly. Used as the fallback
    when entry-point discovery yields zero entries.

    Imports are local so import-time of this module stays light when
    a caller provides their own ``extractors=`` list. Each import is
    individually try/except'd so a missing or syntactically-broken
    extractor doesn't kill the whole orchestrator — the user gets a
    smaller registry but the scan still runs.
    """
    out: list[AnchorExtractor] = []

    def _try(import_path: str, class_name: str) -> None:
        try:
            module = __import__(import_path, fromlist=[class_name])
            cls = getattr(module, class_name)
            instance = cls()
            if isinstance(instance, AnchorExtractor):
                out.append(instance)
            else:
                logger.warning(
                    "%s.%s does not satisfy AnchorExtractor",
                    import_path, class_name,
                )
        except (ImportError, AttributeError) as exc:
            logger.debug(
                "default extractor %s.%s not available: %s",
                import_path, class_name, exc,
            )

    # Order is informational only — extractors run in parallel.
    _try("faultline.pipeline_v2.extractors.route",   "RouteFileExtractor")
    _try("faultline.pipeline_v2.extractors.mvc",     "MVCControllerExtractor")
    _try("faultline.pipeline_v2.extractors.schema",  "SchemaDomainExtractor")
    _try("faultline.pipeline_v2.extractors.package", "PackageAnchorExtractor")
    _try("faultline.pipeline_v2.extractors.config",  "ConfigAsProductExtractor")
    # Sprint A4 — stack-gated extractors. Each self-skips when its
    # activation gate (Go / Rust workspace / Python library) fails,
    # so registering them unconditionally is safe + cheap.
    _try("faultline.pipeline_v2.extractors.go_router",       "GoRouterExtractor")
    # Go package-structure extractor. Emits one deterministic anchor per
    # cmd/ internal/ pkg/ modules/ apps/ first-level dir + top-level
    # repo-root package. Closes the ~50% Go llm_fallback gap left by the
    # route-only go_router. Self-skips via the same Go activation gate.
    _try("faultline.pipeline_v2.extractors.go_packages",     "GoPackageExtractor")
    _try("faultline.pipeline_v2.extractors.rust_workspace",  "RustWorkspaceExtractor")
    # Rust intra-crate module extractor. Emits one deterministic anchor per
    # first-level src/ module (src/<m>.rs, src/<m>/, src/bin/<n>.rs) for each
    # crate root. rust_workspace only emits ONE anchor per workspace member;
    # this closes the within-crate gap that pushes meilisearch (~36%) and
    # single-crate Rust repos to the LLM. Self-skips via the same Rust gate.
    _try("faultline.pipeline_v2.extractors.rust_packages",   "RustModuleExtractor")
    _try("faultline.pipeline_v2.extractors.python_library",  "PythonLibraryExtractor")
    # FastAPI HTTP-route extractor. Parses @app/@router decorators +
    # APIRouter(prefix=...) + include_router(...) into explicit routes.
    # Self-skips unless Stage 0 / the auditor signals fastapi (primary
    # or secondary) or a Python repo exposes FastAPI source markers.
    _try("faultline.pipeline_v2.extractors.fastapi",         "FastApiRouteExtractor")
    # Sprint S3.1 — Fastify code-based router. Self-skips unless the
    # auditor / Stage 0 / a workspace package.json signals fastify.
    _try("faultline.pipeline_v2.extractors.fastify",         "FastifyRouteExtractor")
    # Express code-based router. Self-skips unless the auditor / Stage 0
    # / a package.json RUNTIME dep signals express. Never activates on
    # NestJS repos (Nest wraps Express but owns its own route
    # conventions — extracting both would double-count).
    _try("faultline.pipeline_v2.extractors.express",         "ExpressRouteExtractor")
    # Django / DRF URLConf + view extractor. Parses urls.py urlpatterns
    # (path/re_path/url + include), DRF ViewSets/APIViews, and Django CBVs
    # into explicit routes with view-symbol attribution. Self-skips unless
    # the auditor / Stage 0 / a workspace stack signals django-app (or a
    # Python repo exposes Django source markers). Critical for the Django
    # backend workspace of a polyglot monorepo (Stage 1 per-workspace).
    _try("faultline.pipeline_v2.extractors.django",          "DjangoExtractor")
    # Sprint H — JS/TS library extractor. Mirrors python-library: reads
    # package.json#exports + lib/ submodule layout. Self-skips for app-
    # shaped repos (next/express/fastify/etc. in direct deps).
    _try("faultline.pipeline_v2.extractors.js_library",      "JsLibraryExtractor")
    # Sprint Rails — five Rails-app extractors covering routes, models,
    # views, jobs, Stimulus controllers. Each self-skips unless the
    # Stage 0.5 auditor labelled the repo ``rails-app`` (or a secondary
    # stack equals ``rails-app``), so registering them unconditionally
    # costs only the activation gate on non-Rails repos.
    _try("faultline.pipeline_v2.extractors.rails_routes",    "RailsRoutesExtractor")
    _try("faultline.pipeline_v2.extractors.rails_models",    "RailsModelsExtractor")
    _try("faultline.pipeline_v2.extractors.rails_views",     "RailsViewsExtractor")
    _try("faultline.pipeline_v2.extractors.rails_jobs",      "RailsJobsExtractor")
    _try("faultline.pipeline_v2.extractors.rails_stimulus",  "RailsStimulusExtractor")
    # B67 — background-job / cron entry extractor. Emits a routes_index entry
    # (synthetic JOB/CRON method) per @Processor/Worker/cron.schedule (TS/JS),
    # celery/APScheduler/rq (Python), and manifest-cron (vercel/actions/k8s)
    # handler, so background capabilities mint flows/journeys. Self-skips
    # unless FAULTLINE_JOBS_ENTRIES is set (default OFF) — byte-identical when
    # unset. Registered unconditionally; the flag gate lives in extract().
    _try("faultline.pipeline_v2.extractors.jobs_entries",    "JobsEntryExtractor")

    return out


def _discover_entry_point_extractors() -> list[AnchorExtractor]:
    """Load extractors registered via ``importlib.metadata`` entry-points.

    Returns whatever the (possibly empty / partial / stale) group
    yields. Callers MERGE this with the built-in set rather than
    treating it as authoritative.
    """
    eps: Iterable[Any]
    try:
        eps = entry_points(group=_DEFAULT_ENTRY_POINT_GROUP)
    except TypeError:
        # Python 3.10 returns a dict; the ``group=`` form is 3.12+ in
        # some packaging interpreters. Fall back defensively — we
        # still target 3.11+ overall.
        all_eps = entry_points()
        eps = all_eps.get(_DEFAULT_ENTRY_POINT_GROUP, [])  # 3.10 dict-style API
    except Exception:  # noqa: BLE001 — defensive
        eps = []

    loaded: list[AnchorExtractor] = []
    for ep in eps:
        try:
            cls = ep.load()
            instance = cls()
            if not isinstance(instance, AnchorExtractor):
                logger.warning(
                    "entry-point %s did not satisfy AnchorExtractor Protocol",
                    ep.name,
                )
                continue
            loaded.append(instance)
        except Exception as exc:  # noqa: BLE001 — extractor load failure is non-fatal
            logger.warning("failed to load extractor %s: %s", ep.name, exc)

    return loaded


def _discover_extractors() -> list[AnchorExtractor]:
    """Build the active extractor registry.

    The built-in first-party extractors are ALWAYS included. Any
    entry-point-registered third-party extractor whose ``name`` does
    NOT collide with a built-in is appended. A stale, partial, or
    empty entry-point group can therefore never drop a built-in — it
    can only ADD genuinely-external extractors.
    """
    extractors: list[AnchorExtractor] = _load_default_extractors()
    seen: set[str] = {ex.name for ex in extractors}

    for ext in _discover_entry_point_extractors():
        name = getattr(ext, "name", None)
        if not name or name in seen:
            # ``name in seen`` → an entry-point pointing at a built-in
            # (the common case for an in-tree install); the in-tree
            # instance already loaded wins. Skip the duplicate.
            continue
        extractors.append(ext)
        seen.add(name)

    return extractors


def merge_profile_extractors(
    extractors: list[AnchorExtractor],
    profile: Any,
    ctx: "ScanContext",
) -> list[AnchorExtractor]:
    """Apply the active profile's optional Stage-1 extractor overrides.

    StackProfile Phase B activation fold: a framework profile MAY
    implement ``stage_1_extractor_overrides(ctx) -> list[AnchorExtractor]``
    to supply the extractor instances its stack needs — folding the
    pre-profile stack-tag activation gates into the profile module.

    Duck-typed (``getattr``) so this trunk seam never names a concrete
    profile (G3) and is a strict no-op for the DefaultProfile / ``None``
    / any profile without the method — byte-for-byte preservation for
    every other stack. Merge rule: an override whose ``name`` matches a
    discovered extractor REPLACES it in place (never runs twice); new
    names are APPENDED sorted by name (deterministic registry order).
    """
    if profile is None:
        return extractors
    method = getattr(profile, "stage_1_extractor_overrides", None)
    if method is None:
        return extractors
    try:
        overrides = [
            o for o in (method(ctx) or [])
            if isinstance(o, AnchorExtractor)
        ]
    except Exception as exc:  # noqa: BLE001 — override failure is non-fatal
        logger.warning(
            "profile %s stage_1_extractor_overrides failed: %s",
            getattr(profile, "name", "?"), exc,
        )
        return extractors
    if not overrides:
        return extractors

    by_name: dict[str, AnchorExtractor] = {o.name: o for o in overrides}
    merged: list[AnchorExtractor] = [
        by_name.pop(ex.name, ex) for ex in extractors
    ]
    merged.extend(by_name[name] for name in sorted(by_name))
    return merged


def _safe_extract(
    extractor: AnchorExtractor,
    ctx: ScanContext,
) -> tuple[str, list[AnchorCandidate] | None, str | None]:
    """Run a single extractor, swallowing exceptions into an error tuple.

    Returns ``(source, candidates, error)``. ``candidates`` is ``None``
    when an error occurred — the orchestrator stores the error message
    in ``_errors`` and skips the extractor's contribution.
    """
    try:
        candidates = extractor.extract(ctx)
        # Type-check the result so a buggy extractor that returns
        # ``None`` or a generator doesn't poison downstream code.
        if candidates is None:
            return extractor.name, [], None
        candidates = list(candidates)
        for c in candidates:
            if not isinstance(c, AnchorCandidate):
                raise TypeError(
                    f"extractor {extractor.name!r} returned non-AnchorCandidate "
                    f"item: {c!r}",
                )
        return extractor.name, candidates, None
    except Exception as exc:  # noqa: BLE001 — extractor exception is non-fatal
        return extractor.name, None, f"{type(exc).__name__}: {exc}"


def stage_1_extractors(
    ctx: ScanContext,
    extractors: list[AnchorExtractor] | None = None,
    *,
    max_workers: int | None = None,
    profile: Any | None = None,
) -> dict[str, list[AnchorCandidate]]:
    """Run all registered extractors in parallel.

    Args:
        ctx: Stage 0 output.
        extractors: optional explicit registry. When ``None`` (default)
            we discover via Python entry-points and fall back to the
            built-in 5. Tests pass an explicit list to keep the unit
            under control.
        max_workers: thread pool size. ``None`` lets ``ThreadPoolExecutor``
            pick a sensible default based on the number of extractors.
        profile: the ACTIVE framework profile (highest ``detects()`` win),
            consulted duck-typed for Stage-1 extractor overrides — see
            :func:`merge_profile_extractors`. ``None`` / DefaultProfile /
            profiles without the optional method are a strict no-op.

    Returns:
        A ``dict`` keyed by extractor ``name`` (i.e. the ``source``
        string emitted on every candidate). The ``_errors`` key is
        added when one or more extractors raised — its value is a
        ``dict[str, str]`` mapping extractor name to error message.
        The ``_errors`` key is absent when no failures occurred.

    Empty extractor registry yields an empty dict (no ``_errors`` key).
    """
    if extractors is None:
        extractors = _discover_extractors()
    extractors = merge_profile_extractors(extractors, profile, ctx)

    if not extractors:
        return {}

    # ThreadPoolExecutor — each extractor is independent I/O. Cap at
    # the number of extractors (no point spawning idle workers).
    pool_size = max_workers if max_workers is not None else len(extractors)

    results: dict[str, list[AnchorCandidate]] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        # Collect in REGISTRY order, not completion order. Iterating
        # ``as_completed`` here used to build ``results`` in thread-
        # completion order, which made every downstream consumer of
        # ``results.items()`` — and ultimately the emitted
        # ``developer_features[]`` array — nondeterministic across
        # identical runs. ``.result()`` in submission order keeps the
        # extraction fully parallel while making the dict order (and
        # everything derived from it) stable.
        futures = [(ex.name, pool.submit(_safe_extract, ex, ctx)) for ex in extractors]
        for _name, fut in futures:
            source, candidates, error = fut.result()
            if error is not None:
                errors[source] = error
                # Still mark the source key present (empty list) so
                # consumers can iterate ``results.items()`` deterministically.
                results[source] = []
            else:
                assert candidates is not None  # narrowed by ``error is None``
                results[source] = candidates

    if errors:
        # ``_errors`` is a sentinel key — never collides with a real
        # extractor name (extractor names are kebab-case, never start
        # with underscore).
        results["_errors"] = errors  # type: ignore[assignment]

    return results


__all__ = ["merge_profile_extractors", "stage_1_extractors"]
