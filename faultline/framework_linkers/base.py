"""Stage 6.4 framework linker Protocol + shared dataclass.

A framework linker emits :class:`FrameworkLink` records — typed pointers
from a CALL SITE in one file (a fetch invocation, a useSWR hook, an
axios call, a Server Action dispatch, a Zustand store mutation, ...)
to a TARGET handler in another file (a Next route.ts ``POST`` export,
a ``"use server"`` action symbol, a store reducer, ...).

These links are the deterministic equivalent of the C3 import graph
for surfaces the import graph cannot resolve:

  * fetch URLs are strings, never imported.
  * Server Actions cross the network boundary via Next runtime magic,
    not via explicit imports from server to client.
  * Zustand / Redux store mutations are dispatched by string action
    type, not by direct symbol import.

The Protocol is FROZEN once Sprint C4 ships. Adding a sixth linker
must not modify the contract; new linkers plug in via Python
entry-points under the ``faultlines.framework_linkers`` group.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


@dataclass(frozen=True)
class FrameworkLink:
    """One framework-specific deterministic link from caller to target.

    The link is always anchored at a (source_file, source_line) inside
    a feature's already-attributed code; the (target_file, target_symbol)
    is the destination handler the framework dispatches to.

    Attributes:
        source_file: caller path (repo-relative POSIX).
        source_symbol: enclosing symbol at the call site. Falls back to
            ``"<module>"`` when the linker cannot recover the wrapping
            function/component name.
        source_line: 1-indexed line of the call (the URL literal or the
            dispatch expression).
        target_file: linked target path (e.g. the ``route.ts``).
        target_symbol: exported symbol inside the target file (e.g.
            ``"POST"``, ``"GET"``, ``"createOrder"``). Empty string
            when the target file is treated as a single entry point.
        target_line_start: 1-indexed inclusive.
        target_line_end: 1-indexed inclusive.
        linker: short slug — ``"nextjs-http-route"`` for v1.
        link_kind: free-form category — ``"http-route"`` for v1; future
            kinds include ``"server-action"``, ``"store-mutation"``,
            ``"trpc-procedure"``.
        confidence: 0..1. 1.0 = literal URL match, 0.7 = partial-dynamic
            (``${var}`` interpolation), 0.3 = mostly-dynamic / catchall.
        reason: short human-readable explanation surfaced in artifacts.
    """

    source_file: str
    source_symbol: str
    source_line: int
    target_file: str
    target_symbol: str
    target_line_start: int
    target_line_end: int
    linker: str
    link_kind: str
    confidence: float
    reason: str = ""


def canonical_sample(items: Iterable[object], cap: int) -> list:
    """Deterministic emission of a telemetry debug-sample list.

    Stage 6.4 runs ``link_for_feature`` on a ThreadPool; linkers append
    debug samples (``unmatched_sample`` / ``sample_links`` / …) to their
    shared per-scan telemetry FROM WORKER THREADS. The historical idiom
    capped these lists at APPEND time (``if len(list) < N``), which
    leaked thread completion order into scan output twice over: the
    ORDER of the sample followed scheduling, and — worse — with more
    candidates than the cap, the MEMBERSHIP did too (first N arrivals
    won). The 2026-07-07 perf audit caught exactly this as a set-equal
    reordering of ``per_linker[...].unmatched_sample`` between two
    otherwise byte-identical papermark scans.

    The fix: linkers append EVERY candidate (append is GIL-atomic) and
    the cap moves here, into ``as_dict()`` emission — the sample becomes
    the lexicographically-first ``cap`` items under a canonical-JSON
    sort, which is a pure function of the collected multiset. Duplicates
    are preserved (parity with the historical list semantics).
    """
    return sorted(
        items,
        key=lambda item: json.dumps(
            item, sort_keys=True, separators=(",", ":"), default=str,
        ),
    )[:cap]


@runtime_checkable
class FrameworkLinker(Protocol):
    """Stage 6.4 contract.

    Each linker is a self-contained adapter for one framework-specific
    coupling pattern. The orchestrator calls :meth:`is_active` once per
    scan (cheap activation gate) and, when active, calls
    :meth:`link_for_feature` for every Layer 1 feature.

    Implementations should:
      * Return an empty list whenever the linker's framework does not
        apply (never raise on ``is_active=False`` paths).
      * Cache any per-scan precomputation (the route map for the
        Next.js HTTP linker, the list of ``"use server"`` files for
        the Server Actions linker, etc.) on the instance itself —
        the orchestrator instantiates one linker per scan.
      * Be fully deterministic (no LLM, no network).
    """

    name: str
    activation_keys: tuple[str, ...]

    def is_active(self, ctx: "ScanContext") -> bool:
        """Return True when this linker can produce any links for ``ctx``.

        The orchestrator skips :meth:`link_for_feature` entirely when
        this returns False and records the linker in
        ``scan_meta.stage_6_4.skipped_linkers``.
        """
        ...

    def link_for_feature(
        self,
        feature: "Feature",
        ctx: "ScanContext",
        log: "StageLogger",
    ) -> list[FrameworkLink]:
        """Return zero-or-more links rooted in ``feature``'s files.

        Returning ``[]`` is the canonical "no links found for this
        feature" response — it is NOT an error. Raising is reserved
        for genuine programming errors; the orchestrator catches them
        and continues with the remaining linkers.
        """
        ...


__all__ = ["FrameworkLink", "FrameworkLinker", "canonical_sample"]
