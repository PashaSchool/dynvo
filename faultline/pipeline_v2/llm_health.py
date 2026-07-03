"""Scan-level LLM health state — fail LOUD on authentication errors.

Why this module exists (two production incidents of the same class):

  1. 2026-06-06 — Vercel AI Gateway free-tier 429-rate-limited every
     LLM call. The scan reported *succeeded* but was hollow: no flows,
     deterministic-only Layer 2.
  2. 2026-06-10 — a worker shipped an invalid ``ANTHROPIC_API_KEY``.
     Every call in stage_3_flows / stage_4_residual / stage_8_analyst
     401'd with ``invalid x-api-key``, each stage swallowed the error
     per-call, and the scan again ended "succeeded" but empty. The
     user burned a debugging session suspecting an unrelated UI
     feature.

Design decision — degrade VISIBLY, never abort:

  The scan is intentionally **not** aborted when authentication fails.
  Deterministic output (extractors, Stage 2 reconciliation, metrics,
  Layer 2 deterministic clustering) still has real value, and the
  hosted worker pipeline expects a result artifact to exist. Instead
  the failure is made loud and machine-readable:

    - the FIRST auth-class failure flips a scan-level flag; every
      LLM-bearing stage consults it before each call and stops issuing
      further calls (no point retrying hundreds of features against a
      dead key);
    - ``scan_meta["llm_degraded"]`` is stamped (additive key — absent
      on healthy scans) with ``{"reason", "first_stage", "detail"}``;
    - a prominent warning is appended to the scan's ``warnings`` list;
    - the CLI prints a red warning block after the scan.

  Rate-limit (429) storms do NOT flip the short-circuit flag — they
  are transient and the SDK's per-call retry already handles them.
  But if a scan finishes with at least one rate-limit failure and
  ZERO successful LLM calls (the 2026-06-06 silent-empty shape),
  ``llm_degraded`` is stamped with ``reason="rate_limited"`` so the
  hollow result is still machine-readable.

One ``LlmHealth`` instance is created per ``run_pipeline_v2`` run and
threaded to every LLM-bearing stage the same way the shared
``CostTracker`` is. Thread-safe: Stage 3 fires calls from a
``ThreadPoolExecutor``.
"""

from __future__ import annotations

import re
import threading
from typing import Any

__all__ = [
    "LLM_AUTH_WARNING",
    "LLM_RATE_LIMIT_WARNING",
    "LlmHealth",
    "is_auth_error",
    "is_rate_limit_error",
    "sanitize_detail",
    "stamp_llm_degraded",
]


LLM_AUTH_WARNING = (
    "LLM calls failed with an authentication error — scan completed "
    "WITHOUT LLM stages (no flows, deterministic Layer 2 only). "
    "Check ANTHROPIC_API_KEY."
)

LLM_RATE_LIMIT_WARNING = (
    "Every LLM call failed with rate-limit errors — scan completed "
    "WITHOUT LLM stages (no flows, deterministic Layer 2 only). "
    "Check your Anthropic rate limits / gateway credits."
)

# Never leak the key itself into scan_meta / warnings / logs. Anthropic
# keys are ``sk-ant-…``; redact anything matching that shape from
# exception text before storing it.
_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")

_MAX_DETAIL_LEN = 300


def sanitize_detail(text: str) -> str:
    """Redact API-key material and cap length for scan_meta storage."""
    return _KEY_RE.sub("sk-ant-[REDACTED]", text)[:_MAX_DETAIL_LEN]


def _status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status of an exception (SDK or raw response)."""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    # httpx-style: exc.response.status_code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def is_auth_error(exc: BaseException) -> bool:
    """True for authentication-class failures (401/403).

    Matches the Anthropic SDK's typed exceptions when the SDK is
    importable, and falls back to the HTTP status carried on the
    exception (covers raw-response wrappers and gateway shims).
    """
    try:
        import anthropic

        if isinstance(
            exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)
        ):
            return True
    except ImportError:  # pragma: no cover — anthropic always present in prod
        pass
    return _status_code(exc) in (401, 403)


def is_rate_limit_error(exc: BaseException) -> bool:
    """True for HTTP 429 rate-limit failures (transient — never short-circuit)."""
    try:
        import anthropic

        if isinstance(exc, anthropic.RateLimitError):
            return True
    except ImportError:  # pragma: no cover
        pass
    return _status_code(exc) == 429


class LlmHealth:
    """Shared, thread-safe LLM health state for one scan.

    Stages call :meth:`should_call` before every LLM request and
    :meth:`record_failure` / :meth:`record_success` after each attempt.
    The orchestrator calls :meth:`degraded` at finalize time to stamp
    ``scan_meta["llm_degraded"]`` and :meth:`warning` to append the
    human-readable warning. See the module docstring for the
    degrade-visibly-never-abort decision.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._auth_failed = False
        self._first_stage: str | None = None
        self._detail: str | None = None
        self._successes = 0
        self._rate_limit_failures = 0
        self._first_rate_limit_stage: str | None = None
        self._rate_limit_detail: str | None = None

    # ── stage-side API ──────────────────────────────────────────────

    def should_call(self) -> bool:
        """False once an auth-class failure has been recorded."""
        with self._lock:
            return not self._auth_failed

    @property
    def auth_failed(self) -> bool:
        with self._lock:
            return self._auth_failed

    def record_success(self) -> None:
        with self._lock:
            self._successes += 1

    def seed_auth_failure(self, *, stage: str, detail: str = "") -> None:
        """Restore a RECORDED auth-dead state (replay fidelity).

        Replay reconstructs each stage's input from artifacts, but
        LlmHealth is a fresh service object; when the source run's LLM
        auth died at an EARLIER stage, the replayed stage entered with
        ``should_call() == False`` live — reproduce that, otherwise a
        warm llm-cache makes the replay healthier than the recorded run
        (WS1 identity gate). Idempotent; never overwrites an existing
        failure record.
        """
        with self._lock:
            if not self._auth_failed:
                self._auth_failed = True
                self._first_stage = stage
                self._detail = sanitize_detail(detail)

    def record_failure(self, exc: BaseException, *, stage: str) -> bool:
        """Record one failed LLM call.

        Returns True when the failure is authentication-class — the
        caller should stop issuing further LLM calls in its stage
        (``should_call`` flips False scan-wide). Rate-limit failures
        are counted (for the zero-success ``rate_limited`` stamp) but
        never short-circuit.
        """
        if is_auth_error(exc):
            with self._lock:
                if not self._auth_failed:
                    self._auth_failed = True
                    self._first_stage = stage
                    self._detail = sanitize_detail(str(exc))
            return True
        if is_rate_limit_error(exc):
            with self._lock:
                self._rate_limit_failures += 1
                if self._first_rate_limit_stage is None:
                    self._first_rate_limit_stage = stage
                    self._rate_limit_detail = sanitize_detail(str(exc))
        return False

    # ── orchestrator-side API ───────────────────────────────────────

    def degraded(self) -> dict[str, Any] | None:
        """Payload for ``scan_meta["llm_degraded"]`` — None when healthy.

        ``auth_error`` wins over ``rate_limited``; ``rate_limited`` is
        only stamped when NOT A SINGLE LLM call succeeded (transient
        partial 429s on an otherwise-working key stay silent).
        """
        with self._lock:
            if self._auth_failed:
                return {
                    "reason": "auth_error",
                    "first_stage": self._first_stage or "unknown",
                    "detail": self._detail or "",
                }
            if self._rate_limit_failures > 0 and self._successes == 0:
                return {
                    "reason": "rate_limited",
                    "first_stage": self._first_rate_limit_stage or "unknown",
                    "detail": self._rate_limit_detail or "",
                }
            return None

    def warning(self) -> str | None:
        """Human-readable warning matching :meth:`degraded` — None when healthy."""
        payload = self.degraded()
        if payload is None:
            return None
        if payload["reason"] == "auth_error":
            return LLM_AUTH_WARNING
        return LLM_RATE_LIMIT_WARNING


def stamp_llm_degraded(
    scan_meta: dict[str, Any], llm_health: LlmHealth,
) -> None:
    """Stamp ``scan_meta["llm_degraded"]`` + the warnings entry.

    No-op on a healthy scan — the key stays ABSENT (additive contract,
    consumers treat absence as "LLM stages ran normally"). Called by
    the orchestrator's finalize phase after the last LLM-bearing stage
    and before Stage 7 writes the artifact.
    """
    payload = llm_health.degraded()
    if payload is None:
        return
    scan_meta["llm_degraded"] = payload
    warning = llm_health.warning()
    warnings = scan_meta.setdefault("warnings", [])
    if warning and warning not in warnings:
        warnings.append(warning)
