"""Sprint 16 Day 2 — LLM-as-judge for feature / flow eval.

Standard pattern in modern AI evals (RAGAS, OpenAI / Anthropic
evals): use a model to score predictions against ground truth via
**semantic** matching. ``http-client`` matches ``request-handler``;
``user-auth`` matches ``authentication-and-account-access``; etc.

Without semantic matching, exact-string scoring would punish every
naming variation and the harness would be uselessly noisy. With it,
naming becomes orthogonal to coverage.

Public surface
==============

    JudgeResult                     — coverage, precision, F1, matches
    judge_run(expected, detected,
              client, repo) -> JudgeResult

The function takes a Haiku client + the two name lists, returns
metrics. Cache verdicts by ``hash(expected + detected)`` so re-runs
of the same state cost nothing.

The default model is ``claude-haiku-4-5`` (fast, cheap, deterministic
with temperature=0). Override via ``model=`` for testing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_MAX_TOKENS = 4_096
DEFAULT_CACHE_DIR = Path.home() / ".faultline" / "eval-cache"
_CACHE_VERSION = 3  # S17 — bumped when batching introduced (n8n 0% bug)

# S17 — split detected list into batches when too many. Above ~150 items
# Haiku silently truncates output → judge returns 0% (n8n hit this with
# 684 detected flows). Batching keeps each call within token budget;
# results merged per-expected (best-quality verdict wins across batches).
_BATCH_THRESHOLD = 150
_BATCH_SIZE = 120

_QUALITY_RANK = {"exact": 2, "partial": 1, "none": 0}

# Match-quality buckets returned by the judge. ``exact`` and ``partial``
# both count toward coverage; ``none`` doesn't.
_VALID_QUALITIES: frozenset[str] = frozenset({"exact", "partial", "none"})


@dataclass
class Match:
    """One pairing of expected → detected with a quality verdict."""

    expected: str
    detected: str | None
    quality: str  # exact / partial / none

    @property
    def is_hit(self) -> bool:
        return self.quality in {"exact", "partial"}


@dataclass
class JudgeResult:
    """Output of one judge_run call."""

    coverage: float          # hits / |expected|
    precision: float         # hits / |detected| (after dedup)
    f1: float
    matches: list[Match] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)  # detected but unexpected
    cache_hit: bool = False

    def hits(self) -> int:
        return sum(1 for m in self.matches if m.is_hit)


# ── Anthropic client protocol so tests can inject a fake ──────────────


class _AnthropicLike(Protocol):
    @property
    def messages(self) -> Any: ...  # pragma: no cover


# ── System prompt ─────────────────────────────────────────────────────


_SYSTEM_PROMPT = """\
You evaluate a feature-detection tool against a ground-truth list
curated by the open-source repository's maintainers.

For each EXPECTED feature, find the BEST matching DETECTED feature
or return "NONE" if no equivalent exists. Use SEMANTIC matching:

  - "http-client" matches "request-handling", "axios-core",
    "api-client". The names differ but the domain is the same.
  - "user-auth" matches "authentication-and-account-access" or
    "auth". Different framing, same surface.
  - "interceptors" matches "request-interceptors" exact;
    NOT "interceptor-chain-builder" (too specific) UNLESS that's
    literally what the repo calls the feature.

QUALITY BUCKETS:
  "exact"   — names + scope are equivalent.
  "partial" — there is ANY reasonable semantic connection — the
              detected feature carries some of the expected concept
              even if the name is broader / narrower / phrased
              differently. Includes:
                - sub-name or super-name relations
                  (``pull-requests`` ↔ ``pull``,
                   ``wiki`` ↔ ``repo-wiki``,
                   ``real-time-collaboration`` ↔ ``editor/realtime-and-data-sync``)
                - feature is bundled into a larger detected one
                  (``rest-api`` is part of ``model-inference`` for ollama)
                - feature is split across multiple detected pieces
                  but each carries part of it
  "none"    — no detected feature has any reasonable connection.

DEFAULT TO PARTIAL OVER NONE. Real-world feature detection produces
slightly different names than maintainers use; if you can articulate
any connection ("X is the dashboard implementation of Y", "X is a
sub-domain of Y"), it's a "partial" match. Return "none" only when
nothing in the detected list relates to the expected concept.

OUTPUT (JSON only, no prose, no markdown fences):
{
  "matches": [
    {"expected": "<name>",
     "detected": "<best_match_or_NONE>",
     "quality": "exact" | "partial" | "none"}
  ],
  "extras": ["<detected_features_not_matched_to_anything_expected>"]
}

Cover EVERY expected entry exactly once. ``extras`` lists detected
names that didn't get matched to any expected (so precision can be
computed correctly).
"""


# ── Helpers ───────────────────────────────────────────────────────────


def _cache_key(expected: list[str], detected: list[str]) -> str:
    """Stable hash over the two name sets. Reorder-tolerant."""
    payload = json.dumps(
        [sorted(expected), sorted(detected)], ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _cache_path(cache_dir: Path, repo: str) -> Path:
    safe_repo = re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-") or "unknown"
    return cache_dir / f"judge-{safe_repo}.json"


def _load_cache(
    cache_dir: Path | None, repo: str, key: str,
) -> dict[str, Any] | None:
    if cache_dir is None:
        return None
    path = _cache_path(cache_dir, repo)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("version") != _CACHE_VERSION:
        return None
    return data.get("entries", {}).get(key)


def _save_cache(
    cache_dir: Path | None, repo: str, key: str, payload: dict,
) -> None:
    if cache_dir is None:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = _cache_path(cache_dir, repo)
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                existing = {}
        if existing.get("version") != _CACHE_VERSION:
            existing = {"version": _CACHE_VERSION, "entries": {}}
        existing.setdefault("entries", {})[key] = payload
        path.write_text(json.dumps(existing, indent=2, sort_keys=True))
    except OSError as exc:
        logger.warning("judge: cache save failed (%s)", exc)


def _parse_response(text: str) -> dict[str, Any] | None:
    """Extract the JSON envelope. None on parse failure."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _coerce_match(entry: Any, expected_set: set[str]) -> Match | None:
    if not isinstance(entry, dict):
        return None
    expected = (entry.get("expected") or "").strip()
    detected_raw = entry.get("detected")
    detected = (
        detected_raw.strip() if isinstance(detected_raw, str) else None
    )
    if detected and detected.upper() == "NONE":
        detected = None
    quality = (entry.get("quality") or "").strip().lower()
    if expected not in expected_set or quality not in _VALID_QUALITIES:
        return None
    return Match(expected=expected, detected=detected, quality=quality)


def _build_metrics(
    matches: list[Match], detected: list[str],
) -> tuple[float, float, float]:
    """Coverage = hits / |expected|. Precision = hits / |detected dedup|.

    Both caps at 1.0. Returns (coverage, precision, f1).
    """
    if not matches:
        return (0.0, 0.0, 0.0)
    hits = sum(1 for m in matches if m.is_hit)
    coverage = min(1.0, hits / len(matches))
    n_detected = len(set(detected))
    # Precision capped at 1.0 — multiple expected mapping to one detected
    # is semantically valid (e.g. "rest-api" + "graphql-api" both match
    # detected "api"), but it makes raw hits/n_detected inflate above 1
    # which is mathematically meaningless.
    precision = min(1.0, hits / n_detected) if n_detected else 0.0
    if coverage + precision == 0:
        f1 = 0.0
    else:
        f1 = 2 * coverage * precision / (coverage + precision)
    return (coverage, precision, f1)


# ── Single Haiku call (one batch) ─────────────────────────────────────


def _judge_call(
    client: _AnthropicLike,
    model: str,
    expected_list: list[str],
    detected_batch: list[str],
    expected_set: set[str],
) -> tuple[list[Match], list[str]]:
    """One Haiku judge call. Returns (matches, extras) for this batch."""
    user_msg = json.dumps({
        "expected": expected_list,
        "detected": detected_batch,
    }, indent=2, ensure_ascii=False)

    try:
        response = client.messages.create(
            model=model,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("judge: API call failed (%s)", exc)
        return ([], [])

    text = ""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            text += getattr(block, "text", "")

    parsed = _parse_response(text) or {}
    raw_matches = parsed.get("matches") or []
    matches: list[Match] = []
    for entry in raw_matches:
        coerced = _coerce_match(entry, expected_set)
        if coerced is not None:
            matches.append(coerced)

    extras_raw = parsed.get("extras") or []
    extras = [s for s in extras_raw if isinstance(s, str) and s.strip()]
    return matches, extras


# ── Public entry point ────────────────────────────────────────────────


def judge_run(
    expected: Iterable[str],
    detected: Iterable[str],
    *,
    repo: str = "unknown",
    client: _AnthropicLike | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    cache_dir: Path | None = DEFAULT_CACHE_DIR,
) -> JudgeResult:
    """Score detected features against expected via Haiku semantic match.

    Returns JudgeResult. Returns a coverage=0/precision=0 fallback when
    Haiku is unavailable (no API key, no client) so callers can still
    produce reports — just zero-scored.
    """
    expected_list = sorted({s.strip() for s in expected if s and s.strip()})
    detected_list = sorted({s.strip() for s in detected if s and s.strip()})
    if not expected_list:
        return JudgeResult(0.0, 0.0, 0.0)

    key = _cache_key(expected_list, detected_list)
    cached = _load_cache(cache_dir, repo, key)
    if cached is not None:
        # Strip extra keys (e.g. override_reason from manual patches)
        # so Match.__init__ stays strict.
        _allowed = {"expected", "detected", "quality"}
        matches = [
            Match(**{k: v for k, v in m.items() if k in _allowed})
            for m in cached.get("matches", [])
            if isinstance(m, dict)
        ]
        extras = list(cached.get("extras", []))
        cov, prec, f1 = _build_metrics(matches, detected_list)
        return JudgeResult(cov, prec, f1, matches, extras, cache_hit=True)

    if client is None:
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.debug("judge: no API key — returning zero-score fallback")
            return JudgeResult(0.0, 0.0, 0.0)
        try:
            from anthropic import Anthropic
        except ImportError:
            logger.warning("judge: anthropic package missing")
            return JudgeResult(0.0, 0.0, 0.0)
        client = Anthropic(api_key=api_key)

    expected_set = set(expected_list)

    # S17 — batch detected list when too many to avoid token-budget
    # truncation. For each batch, get partial verdicts; merge by best
    # quality across batches (exact wins over partial wins over none).
    if len(detected_list) > _BATCH_THRESHOLD:
        batches = [
            detected_list[i : i + _BATCH_SIZE]
            for i in range(0, len(detected_list), _BATCH_SIZE)
        ]
        logger.info(
            "judge: detected list size %d > %d — splitting into %d batches",
            len(detected_list), _BATCH_THRESHOLD, len(batches),
        )
    else:
        batches = [detected_list]

    best_per_expected: dict[str, Match] = {}
    all_extras: list[str] = []
    for batch in batches:
        sub_matches, sub_extras = _judge_call(
            client, model, expected_list, batch, expected_set,
        )
        for m in sub_matches:
            prev = best_per_expected.get(m.expected)
            if prev is None or _QUALITY_RANK[m.quality] > _QUALITY_RANK[prev.quality]:
                best_per_expected[m.expected] = m
        all_extras.extend(sub_extras)

    matches: list[Match] = list(best_per_expected.values())

    # Fill in any expected features the judge silently dropped.
    seen_expected = {m.expected for m in matches}
    for exp in expected_list:
        if exp not in seen_expected:
            matches.append(Match(expected=exp, detected=None, quality="none"))

    # Extras: dedup, drop those already used in matches
    matched_detected = {m.detected for m in matches if m.detected}
    extras = sorted({e for e in all_extras if e and e not in matched_detected})

    cov, prec, f1 = _build_metrics(matches, detected_list)

    _save_cache(cache_dir, repo, key, {
        "matches": [
            {"expected": m.expected, "detected": m.detected, "quality": m.quality}
            for m in matches
        ],
        "extras": extras,
    })

    return JudgeResult(cov, prec, f1, matches, extras)
