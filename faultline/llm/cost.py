"""Centralized LLM cost tracking for the detection pipeline.

The legacy pipeline only tracks cost for the iterative strategy
(``iterative_detector.estimate_cost``). Every other strategy flies
blind — which is why documenso timed out at $unknown and cal.com
was left unscanned in the Day 1 baseline.

This module provides a single ``CostTracker`` that:
  - records every LLM call (provider, model, tokens, cost)
  - looks up per-million-token pricing from a versioned table
  - supports the Anthropic batch-API 50% discount
  - aborts the run when a user-specified ``--max-cost`` is exceeded
  - serializes a summary for logs and the ``metadata.json`` baseline

Nothing here calls an LLM or performs I/O. Adding a new model is a
single line in ``_PRICING``.

Wiring into ``sonnet_scanner`` / ``iterative_detector`` happens in
Day 5–7 of the rewrite. This file lands first so those modules have
a stable target to import against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ── Pricing table ─────────────────────────────────────────────────────────
#
# Prices are USD per 1 million tokens. Source: Anthropic public pricing
# page as of 2026-04. Update this table when Anthropic changes prices;
# do NOT hard-code prices at call sites.
#
# Shape: model_id → (input_per_mtok, output_per_mtok)

_PRICING: dict[str, tuple[float, float]] = {
    # Claude 4/4.6 family
    "claude-opus-4-20250514":    (15.00, 75.00),
    "claude-opus-4-6":           (15.00, 75.00),
    "claude-sonnet-4-20250514":  (3.00, 15.00),
    "claude-sonnet-4-6":         (3.00, 15.00),
    "claude-haiku-4-5":          (1.00,  5.00),
    "claude-haiku-4-5-20251001": (1.00,  5.00),
    # Claude 3.x legacy — kept for compatibility with older configs
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022":  (0.80,  4.00),
    "claude-3-opus-20240229":     (15.00, 75.00),
}

# Fallback used when an unknown model id is passed. Choosing Sonnet as
# the default keeps estimates conservative (not too cheap) so a bad
# config doesn't hide spend.
_DEFAULT_PRICING = (3.00, 15.00)

# Anthropic batch-API discount (50%) applies to both input and output.
_BATCH_DISCOUNT = 0.50


def lookup_pricing(model: str) -> tuple[float, float]:
    """Return (input_per_mtok, output_per_mtok) for a model id.

    Unknown models fall back to Sonnet pricing as a safe default so
    unrecognized configs still produce non-zero estimates.

    >>> lookup_pricing("claude-sonnet-4-6")
    (3.0, 15.0)
    >>> lookup_pricing("claude-haiku-4-5")
    (1.0, 5.0)
    >>> lookup_pricing("some-future-model")
    (3.0, 15.0)
    """
    return _PRICING.get(model, _DEFAULT_PRICING)


def estimate_call_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    batch: bool = False,
) -> float:
    """Compute the USD cost of a single LLM call.

    >>> round(estimate_call_cost("claude-sonnet-4-6", 1_000_000, 500_000), 2)
    10.5
    >>> round(estimate_call_cost("claude-sonnet-4-6", 1_000_000, 500_000, batch=True), 2)
    5.25
    """
    in_rate, out_rate = lookup_pricing(model)
    cost = (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
    if batch:
        cost *= _BATCH_DISCOUNT
    return cost


# ── Call records ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CostRecord:
    """Single LLM call accounted for by the tracker."""

    provider: Literal["anthropic", "ollama", "deepseek"]
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    label: str = ""  # free-form tag, e.g. "deep-scan", "flow-enrichment"
    batch: bool = False


# ── Tracker ───────────────────────────────────────────────────────────────


class BudgetExceeded(RuntimeError):
    """Raised when a tracker's accumulated cost exceeds its ``max_cost``."""

    def __init__(self, spent: float, limit: float) -> None:
        super().__init__(
            f"LLM budget exceeded: ${spent:.4f} spent, limit ${limit:.4f}"
        )
        self.spent = spent
        self.limit = limit


@dataclass
class CostTracker:
    """Accumulates LLM cost across all calls in a single analyze run.

    Usage::

        tracker = CostTracker(max_cost=0.50)
        tracker.record(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            label="deep-scan",
        )
        tracker.check_budget()  # raises BudgetExceeded if over max_cost
        print(tracker.summary())

    ``max_cost=None`` disables budget enforcement (the tracker still
    records calls for reporting).
    """

    max_cost: float | None = None
    records: list[CostRecord] = field(default_factory=list)

    def record(
        self,
        *,
        provider: Literal["anthropic", "ollama", "deepseek"],
        model: str,
        input_tokens: int,
        output_tokens: int,
        label: str = "",
        batch: bool = False,
    ) -> CostRecord:
        """Record a single LLM call and return the resulting CostRecord.

        For non-Anthropic providers (ollama runs locally, deepseek has
        its own pricing elsewhere), cost is set to 0.0 — the tracker
        still records tokens for reporting.
        """
        if provider == "anthropic":
            cost = estimate_call_cost(
                model, input_tokens, output_tokens, batch=batch
            )
        else:
            cost = 0.0

        rec = CostRecord(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            label=label,
            batch=batch,
        )
        self.records.append(rec)
        return rec

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.records)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.records)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.records)

    @property
    def call_count(self) -> int:
        return len(self.records)

    def check_budget(self) -> None:
        """Raise BudgetExceeded if accumulated cost exceeds max_cost.

        Call this right after ``record()`` on each LLM call so the
        pipeline aborts before firing another expensive request.
        """
        if self.max_cost is None:
            return
        if self.total_cost_usd > self.max_cost:
            raise BudgetExceeded(self.total_cost_usd, self.max_cost)

    def summary(self) -> dict:
        """Return a JSON-serializable summary for logs / metadata.json."""
        by_label: dict[str, dict[str, float | int]] = {}
        for r in self.records:
            b = by_label.setdefault(
                r.label or "unlabeled",
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0},
            )
            b["calls"] = int(b["calls"]) + 1
            b["input_tokens"] = int(b["input_tokens"]) + r.input_tokens
            b["output_tokens"] = int(b["output_tokens"]) + r.output_tokens
            b["cost_usd"] = float(b["cost_usd"]) + r.cost_usd

        return {
            "total_calls": self.call_count,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "max_cost_usd": self.max_cost,
            "by_label": {
                k: {
                    "calls": int(v["calls"]),
                    "input_tokens": int(v["input_tokens"]),
                    "output_tokens": int(v["output_tokens"]),
                    "cost_usd": round(float(v["cost_usd"]), 6),
                }
                for k, v in by_label.items()
            },
        }

    def format_report(self) -> str:
        """Return a human-readable one-block report for terminal output."""
        if not self.records:
            return "LLM cost: (no calls recorded)"
        lines = [
            f"LLM cost: ${self.total_cost_usd:.4f}  "
            f"({self.call_count} call{'s' if self.call_count != 1 else ''}, "
            f"{self.total_input_tokens:,} in / {self.total_output_tokens:,} out tokens)"
        ]
        if self.max_cost is not None:
            pct = 100 * self.total_cost_usd / self.max_cost if self.max_cost else 0
            lines.append(f"  budget: ${self.max_cost:.2f}  ({pct:.0f}% used)")
        return "\n".join(lines)
