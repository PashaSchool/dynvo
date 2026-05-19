"""Stage 1 — parallel deterministic anchor extractors.

Runs all registered :class:`AnchorExtractor` instances against the
:class:`ScanContext` from Stage 0, in parallel. Extractors are I/O
bound (file reads, manifest parses), so a ``ThreadPoolExecutor`` is
the right primitive — see ``python-architect-developer`` skill.

Discovery preference order:

  1. Python entry-points under ``faultlines.extractors`` group
     (installed packages register themselves; customers can plug in
     ``~/.faultline/extractors/<custom>.py`` packages).
  2. Hardcoded default registry of the 5 built-in extractors. Used
     when entry-points return an empty group — typical for editable
     installs that haven't been re-installed since the entry-points
     were added.

Failure handling: each extractor runs inside a try/except. A failing
extractor does NOT kill the orchestrator; the failure is recorded in
the returned ``_errors`` key and the other extractors continue. The
orchestrator does NOT mutate ``scan_meta`` directly — Stage 7 is
responsible for surfacing telemetry to the final FeatureMap.

No LLM calls. No network calls.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib.metadata import entry_points  # module-level binding so tests can monkeypatch
from typing import TYPE_CHECKING

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
    _try("faultline.pipeline_v2.extractors.rust_workspace",  "RustWorkspaceExtractor")
    _try("faultline.pipeline_v2.extractors.python_library",  "PythonLibraryExtractor")

    return out


def _discover_extractors() -> list[AnchorExtractor]:
    """Discover registered extractors via ``importlib.metadata``.

    Falls back to the built-in 5 when the group is empty.
    """
    try:
        eps = entry_points(group=_DEFAULT_ENTRY_POINT_GROUP)
    except TypeError:
        # Python 3.10 returns a dict; the ``group=`` form is 3.12+ in
        # some packaging interpreters. Fall back defensively — we
        # still target 3.11+ overall.
        all_eps = entry_points()
        eps = all_eps.get(_DEFAULT_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
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

    if not loaded:
        return _load_default_extractors()
    return loaded


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

    if not extractors:
        return {}

    # ThreadPoolExecutor — each extractor is independent I/O. Cap at
    # the number of extractors (no point spawning idle workers).
    pool_size = max_workers if max_workers is not None else len(extractors)

    results: dict[str, list[AnchorCandidate]] = {}
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        futures = {
            pool.submit(_safe_extract, ex, ctx): ex.name for ex in extractors
        }
        for fut in as_completed(futures):
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


__all__ = ["stage_1_extractors"]
