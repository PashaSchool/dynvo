"""Stage 6.4 framework-linker discovery.

Mirrors the Stage 1 extractor discovery pattern in
:mod:`faultline.pipeline_v2.stage_1_extractors`. Discovery preference:

  1. Python entry-points under the ``faultlines.framework_linkers``
     group. Customers can register third-party linkers without
     forking the engine.
  2. Hardcoded default registry — used when entry-points return
     nothing (typical for editable installs that haven't been
     re-installed since the entry-point was added).

The default registry currently contains exactly one linker —
:class:`NextjsHttpRouteLinker`. Future C5+ sprints will append to
the registry by adding one ``_try(...)`` call AND one entry-point
line — they MUST NOT modify the existing protocols or the Stage 6.4
orchestrator core.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points  # module-level binding so tests can monkeypatch

from faultline.framework_linkers.base import FrameworkLinker

logger = logging.getLogger(__name__)


_ENTRY_POINT_GROUP = "faultlines.framework_linkers"


def _load_default_linkers() -> list[FrameworkLinker]:
    """Direct-import the built-in linkers (fallback path)."""
    out: list[FrameworkLinker] = []

    def _try(import_path: str, class_name: str) -> None:
        try:
            module = __import__(import_path, fromlist=[class_name])
            cls = getattr(module, class_name)
            instance = cls()
            if isinstance(instance, FrameworkLinker):
                out.append(instance)
            else:
                logger.warning(
                    "%s.%s does not satisfy FrameworkLinker",
                    import_path, class_name,
                )
        except (ImportError, AttributeError) as exc:
            logger.debug(
                "default linker %s.%s not available: %s",
                import_path, class_name, exc,
            )

    # Sprint C4 (v1): Next.js HTTP route linker.
    # Sprint D1 (C5+C7): Next.js Server Actions + tRPC procedure linkers.
    # To add a new linker later: append one _try(...) call below AND one
    # line under [project.entry-points."faultlines.framework_linkers"]
    # in pyproject.toml.
    _try(
        "faultline.framework_linkers.nextjs_http_route",
        "NextjsHttpRouteLinker",
    )
    _try(
        "faultline.framework_linkers.nextjs_server_actions",
        "NextjsServerActionsLinker",
    )
    _try(
        "faultline.framework_linkers.trpc_procedure",
        "TrpcProcedureLinker",
    )
    # Sprint C6: store-mutation linker (Zustand / Redux / Jotai / Valtio / Nanostores).
    _try(
        "faultline.framework_linkers.store_mutation",
        "StoreMutationLinker",
    )

    return out


def discover_linkers() -> list[FrameworkLinker]:
    """Discover registered linkers; fall back to the built-in registry."""
    try:
        eps = entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:
        all_eps = entry_points()
        eps = all_eps.get(_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — defensive
        eps = []

    loaded: list[FrameworkLinker] = []
    for ep in eps:
        try:
            cls = ep.load()
            instance = cls()
            if not isinstance(instance, FrameworkLinker):
                logger.warning(
                    "entry-point %s did not satisfy FrameworkLinker", ep.name,
                )
                continue
            loaded.append(instance)
        except Exception as exc:  # noqa: BLE001 — non-fatal
            logger.warning("failed to load framework linker %s: %s", ep.name, exc)

    if not loaded:
        return _load_default_linkers()
    return loaded


__all__ = ["discover_linkers"]
