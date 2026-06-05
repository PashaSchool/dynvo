"""Fan-in gating — separate flow CORE from SHARED infrastructure.

Deterministic, no LLM. Operates on the WHOLE scan (every flow's depth-1
call graph) so it must run as a second pass after every flow has been
expanded — that is the only point at which a symbol's global fan-in is
visible.

The problem
===========

After the depth-1 cross-file cap, a flow is "entry + its direct
callees". But some of those direct callees are SHARED INFRASTRUCTURE —
a database-session opener, a registry/dispatcher, a generic
validator/logger. They are correctly REACHED from the flow, but they
are not what distinguishes THIS behaviour from every other one. Per
``flow-feature-concept``: sharing is normal and should be surfaced as a
shared-dependency badge, not deleted, and such symbols should not count
toward the flow's CORE line-of-code.

Fan-in
======

A symbol's *fan-in* is the number of DISTINCT flow entry-points that
call it across the entire scan. ``async_session`` called by 60 flows
has fan-in 60; ``_persist_single_detector`` called by one flow has
fan-in 1.

Scale-invariant threshold (per [[rule-no-magic-tuning]])
========================================================

We never compare fan-in to an absolute constant tuned on one repo. A
symbol is SHARED iff BOTH hold:

  1. ``fan_in >= 2`` — a structural floor: a symbol called by exactly
     one flow is by definition not shared, so it can never be demoted.
     This is a property of the word "shared", not a tuned number.

  2. ``fan_in > ratio * median`` where ``median`` is the median fan-in
     of EVERY reached callee symbol in the scan (the *typical* callee,
     which is reached by one flow in any non-trivial repo) and ``ratio``
     defaults to 5. Shared infrastructure (a DB-session opener, a
     registry, a generic validator) sits in the long right tail, a
     multiple of the typical callee's reach above the body of the
     distribution. The median is computed from THIS scan, so the same
     code adapts to a 5-flow library and a 5000-flow monorepo — it
     demotes symbols whose reach is a multiple of the median callee's
     reach, never a fixed count.

Why the median of ALL callees (not just the >= floor sub-distribution)?
On a real, heavily right-skewed fan-in histogram the typical callee is
reached by exactly one flow. Taking the median over the whole reached
set anchors the denominator at that typical value (≈ 1) and lets the
ratio isolate the right-tail outliers. Restricting the median to the
>= floor sub-distribution makes it degenerate when there is a single
outlier (median == the outlier's own value, so it can never exceed
``ratio * median``).

Why ``ratio = 5``? It is a RATIO against the median, not an absolute
fan-in, so it is scale-invariant. The value 5 was chosen as the
smallest multiplier that, on the validation contexts below, isolates
the genuine infrastructure outlier from the body of domain-shared
helpers (which cluster at 2-5× the median). Sweep tried: 3, 4, 5.
Validated on: a FastAPI service corpus (Soc0 — median callee fan-in 1;
ratio 5 demotes only the DB-session opener, fan-in 11, leaving the
domain serializers/getters at fan-in 4-5 as core) and a synthetic
multi-handler micro-repo (one shared session opener at 100% fan-in vs
per-handler helpers at fan-in 1). Lower ratios (3-4) additionally
demote domain-shared helpers that belong in core; this is the
documented trade-off, not a silently-tuned constant.

When the distribution has no symbol more than ``ratio * median`` (a flat
distribution with no shared infra) nothing is demoted — which is
correct: there is no outlier tail to separate.

Output
======

:func:`compute_fan_in` returns a :class:`FanInResult` carrying the
per-symbol fan-in map, the derived threshold, and the set of symbol
keys classified as shared. The expander consumes it to flip
``role="called"`` → ``role="shared"`` on the matching attributions /
nodes.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field

# Default multiplier for the ratio-vs-median outlier rule. A symbol is
# shared infrastructure when its fan-in is more than this many times the
# MEDIAN fan-in of ALL reached callee symbols (the typical callee). It
# is a RATIO, not an absolute fan-in count, so it is scale-invariant by
# construction. See the module docstring for the sweep + validation
# contexts that fix the value at 5.
DEFAULT_FAN_IN_RATIO = 5.0

# Structural floor: a symbol called by fewer than this many distinct
# flows cannot be "shared". 2 is definitional (sharing requires >= 2
# distinct callers), not a tuned magic number.
SHARED_FLOOR = 2


@dataclass(frozen=True)
class FanInResult:
    """Outcome of the global fan-in pass.

    Attributes:
        fan_in: ``{symbol_key: distinct_flow_count}`` for every callee
            symbol seen across the scan.
        threshold: the derived fan-in value a symbol must STRICTLY
            exceed (in addition to the floor) to be shared
            (``ratio * median``). ``None`` when there were no candidate
            symbols.
        shared_keys: the set of ``symbol_key`` classified as shared.
        ratio: the ratio multiplier used (echoed for telemetry).
        median: the candidate-distribution median (echoed for telemetry).
        candidate_count: number of symbols at/above the floor.
    """

    fan_in: dict[str, int]
    threshold: float | None
    shared_keys: frozenset[str]
    ratio: float = DEFAULT_FAN_IN_RATIO
    median: float | None = None
    candidate_count: int = 0


def symbol_key(file: str, symbol: str | None) -> str:
    """Stable key identifying a callee symbol for fan-in counting.

    A shared utility is "the same symbol" regardless of which flow
    reaches it, so the key is ``<file>#<symbol>``. Symbols without a
    name (file-level support nodes) are keyed by file alone — those are
    never demoted to core/shared (they are already ``support``).
    """
    return f"{file}#{symbol}" if symbol else file


def compute_fan_in(
    callers_by_symbol: dict[str, set[str]],
    *,
    ratio: float = DEFAULT_FAN_IN_RATIO,
    floor: int = SHARED_FLOOR,
) -> FanInResult:
    """Classify shared-infrastructure symbols from per-symbol caller sets.

    Args:
        callers_by_symbol: ``{symbol_key: {flow_id, ...}}`` — the set of
            DISTINCT flow identities that call each symbol. The expander
            builds this in pass 1.
        ratio: multiplier against the candidate-distribution median
            (default 3.0).
        floor: structural minimum distinct callers to be shareable.

    Returns:
        :class:`FanInResult`.
    """
    fan_in = {k: len(v) for k, v in callers_by_symbol.items()}
    if not fan_in:
        return FanInResult(
            fan_in=fan_in, threshold=None, shared_keys=frozenset(),
            ratio=ratio, median=None, candidate_count=0,
        )

    # Median over EVERY reached callee — the typical callee, reached by
    # one flow in any non-trivial repo. Anchoring the denominator here
    # (rather than the >= floor sub-distribution) keeps the rule robust
    # when there is a single outlier.
    median = statistics.median(fan_in.values())
    threshold = ratio * median

    # A symbol is shared iff it clears BOTH the structural floor and the
    # scale-invariant ratio threshold.
    shared_keys = frozenset(
        k for k, v in fan_in.items()
        if v >= floor and v > threshold
    )
    candidate_count = sum(1 for v in fan_in.values() if v >= floor)
    return FanInResult(
        fan_in=fan_in,
        threshold=threshold,
        shared_keys=shared_keys,
        ratio=ratio,
        median=median,
        candidate_count=candidate_count,
    )


@dataclass
class FanInAccumulator:
    """Mutable pass-1 accumulator: which flows call which symbols."""

    callers_by_symbol: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set),
    )

    def record(self, file: str, symbol: str | None, flow_id: str) -> None:
        """Record that ``flow_id`` calls ``<file>#<symbol>``."""
        if symbol is None:
            return
        self.callers_by_symbol[symbol_key(file, symbol)].add(flow_id)

    def finalize(
        self,
        *,
        ratio: float = DEFAULT_FAN_IN_RATIO,
        floor: int = SHARED_FLOOR,
    ) -> FanInResult:
        return compute_fan_in(
            dict(self.callers_by_symbol),
            ratio=ratio, floor=floor,
        )


__all__ = [
    "FanInResult",
    "FanInAccumulator",
    "compute_fan_in",
    "symbol_key",
    "DEFAULT_FAN_IN_RATIO",
    "SHARED_FLOOR",
]
