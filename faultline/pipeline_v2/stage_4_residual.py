"""Stage 4 — residual LLM fallback (Haiku 4.5).

For files NOT attributed to any Stage 2 deterministic feature, group
them into 1–10 additional ``low``-confidence developer features via a
trimmed Haiku call. Hard caps protect cost and recall integrity:

  - ≤200 paths per LLM chunk (200, 200, ... over the residual list).
  - Max 5 chunks per scan.
  - Max $0.30 total LLM cost (CostTracker.max_cost gate).
  - After concatenation, residual features must not exceed 30% of the
    total feature count; over-cap, we keep the highest-confidence
    subset and emit a warning.
  - Each emitted name must obey the same naming-discipline filter as
    Stage 5 (kebab, no folder paths, no Title Case, non-empty).

Stage 4 is intentionally NOT a call to ``sonnet_scanner.deep_scan``:
that orchestration shell carries every legacy concern (workspace
splitting, chunking, retries, validation) that we have replaced with
the Stage 1–3 deterministic pipeline. Stage 4 is just "the LLM looks
at the leftovers and proposes names".
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from faultline.llm.cost import CostTracker, deterministic_params
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 4_096
MAX_PATHS_PER_CHUNK = 200
MAX_CHUNKS = 5
DEFAULT_COST_CAP_USD = 0.30
MAX_FEATURES_PER_CHUNK = 10
LLM_FALLBACK_SHARE_CAP = 0.30  # ≤30% of total features may come from Stage 4

# Naming-discipline pattern matches the Stage 5 ``_slugify_names`` rule:
# starts with lowercase alnum, then lowercase alnum + hyphens. No
# slashes, dots, whitespace, or uppercase.
_KEBAB_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Reject single-segment kebab names that look like folder paths sliced
# out of repo layout (``app``, ``src``, ``lib``). These are the same
# vocabulary words the deterministic Stage 1/2 layer already de-duped;
# the LLM should not be allowed to reintroduce them as Layer-1 noise.
_REJECTED_GENERIC_NAMES = frozenset({
    "app", "apps", "src", "lib", "libs", "util", "utils", "common",
    "shared", "core", "base", "main", "index", "root",
    "components", "pages", "routes", "api", "server", "client",
    "frontend", "backend", "config", "configs", "test", "tests",
    "docs", "doc", "scripts", "build", "dist", "node-modules",
    "uncategorized", "misc",
})


# ── Output dataclass ───────────────────────────────────────────────────────


@dataclass
class Stage4Result:
    """Public output of :func:`stage_4_residual`.

    Attributes:
        residual_features: new ``low``-confidence DeveloperFeatures.
        cost_usd: total Haiku spend on this stage.
        llm_calls: number of completed Haiku calls (including no-ops).
        warnings: free-form telemetry for ``scan_meta.warnings``.
        chunks_processed: how many residual chunks reached the LLM.
        rejected_names: names the LLM proposed that we filtered out.
    """

    residual_features: list[DeveloperFeature]
    cost_usd: float
    llm_calls: int
    warnings: list[str] = field(default_factory=list)
    chunks_processed: int = 0
    rejected_names: list[str] = field(default_factory=list)


# ── Prompt builders ────────────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You are a residual feature scanner. The deterministic extractors "
    "already mapped most files in this repo to features. The paths "
    "below were NOT claimed by any extractor — they are the residual. "
    "Group them into 1–10 ADDITIONAL developer features with "
    "kebab-case slugs.\n\n"
    "Output STRICT JSON only — no prose, no fences, no markdown. "
    "Schema: {\"features\": [{\"name\": \"<kebab-slug>\", "
    "\"paths\": [\"<rel/path/to/file>\", ...], "
    "\"confidence\": \"low\"}]}\n\n"
    "Rules:\n"
    "- Names MUST be kebab-case (^[a-z0-9][a-z0-9-]*$). NO slashes, "
    "dots, uppercase, or whitespace. NO single-word folder names like "
    "\"app\", \"src\", \"lib\", \"utils\", \"shared\" — these are "
    "structural, not features.\n"
    "- Each feature's ``paths`` MUST be a strict subset of the input "
    "paths. Do NOT invent or hallucinate file paths.\n"
    "- Group by code-grounded semantics: shared route prefix, shared "
    "import target, shared filename pattern. Avoid grouping by depth.\n"
    f"- Return at most {MAX_FEATURES_PER_CHUNK} features per response.\n"
    "- If no useful grouping exists, return {\"features\": []}."
)


def _build_user_prompt(paths: list[str], chunk_idx: int, total_chunks: int) -> str:
    header = (
        f"Residual paths (chunk {chunk_idx + 1} of {total_chunks}, "
        f"{len(paths)} paths):"
    )
    body = "\n".join(f"  - {p}" for p in paths)
    return f"{header}\n{body}\n\nReturn JSON only."


# ── LLM client wiring ──────────────────────────────────────────────────────


def _default_client_factory() -> Any | None:  # pragma: no cover - IO
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


def _call_haiku(
    client: Any,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
) -> tuple[str, int, int]:
    """One Haiku call. Returns ``(text, in_tokens, out_tokens)``.

    Empty string on failure; caller decides whether to skip the chunk.
    """
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage_4_residual: Haiku call failed: %s", exc)
        return "", 0, 0
    try:
        parts = [getattr(b, "text", "") for b in msg.content]
        text = "\n".join(p for p in parts if p)
    except Exception:  # noqa: BLE001
        text = ""
    in_t = int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0)
    out_t = int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)
    return text, in_t, out_t


# ── Response parsing ───────────────────────────────────────────────────────


def _parse_response(text: str) -> list[dict[str, Any]]:
    """Extract the ``features`` array from a Haiku response.

    Returns an empty list when parsing fails.
    """
    if not text:
        return []
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        return []
    return [f for f in feats if isinstance(f, dict)]


# ── Naming-discipline filter (mirrors Stage 5) ─────────────────────────────


def _is_acceptable_name(name: str) -> bool:
    if not name or not name.strip():
        return False
    if not _KEBAB_NAME_PATTERN.match(name):
        return False
    if name in _REJECTED_GENERIC_NAMES:
        return False
    return True


def _build_developer_features(
    raw: list[dict[str, Any]],
    allowed_paths: set[str],
) -> tuple[list[DeveloperFeature], list[str]]:
    """Build :class:`DeveloperFeature` records from raw LLM output.

    Returns ``(accepted, rejected_names)``.

    Filters:
      - bad name (naming-discipline)
      - paths must be a strict subset of ``allowed_paths``
      - features with no surviving paths after filtering
    """
    accepted: list[DeveloperFeature] = []
    rejected: list[str] = []
    seen_names: set[str] = set()

    for entry in raw:
        name_raw = (entry.get("name") or "").strip()
        # Reject BEFORE lowercasing so Title Case ("Billing") and
        # camelCase ("billingPortal") fail the kebab rule deterministically.
        if not _is_acceptable_name(name_raw):
            rejected.append(name_raw or "<empty>")
            continue
        name = name_raw
        if name in seen_names:
            # Two chunks proposed the same name — let the dedup at the
            # caller level handle it; here we just skip the dup.
            continue
        raw_paths = entry.get("paths") or []
        if not isinstance(raw_paths, list):
            rejected.append(f"{name} (paths not a list)")
            continue
        paths = tuple(
            sorted({p for p in raw_paths if isinstance(p, str) and p in allowed_paths}),
        )
        if not paths:
            rejected.append(f"{name} (no valid paths)")
            continue
        seen_names.add(name)
        accepted.append(
            DeveloperFeature(
                name=name,
                paths=paths,
                sources=["llm-fallback"],
                confidence="low",
                rationale="stage-4-residual",
            ),
        )
    return accepted, rejected


# ── 30% LLM-fallback share cap ─────────────────────────────────────────────


def _enforce_share_cap(
    residual: list[DeveloperFeature],
    deterministic_count: int,
    *,
    cap_ratio: float = LLM_FALLBACK_SHARE_CAP,
) -> tuple[list[DeveloperFeature], str | None]:
    """If residual features would exceed ``cap_ratio`` of the total set,
    keep only the largest-by-path-count subset that fits.

    Returns ``(kept, warning_or_None)``.
    """
    if not residual:
        return residual, None
    total_with = deterministic_count + len(residual)
    if total_with == 0:
        return residual, None
    current_share = len(residual) / total_with
    if current_share <= cap_ratio:
        return residual, None

    # Solve for max_k: k / (deterministic + k) ≤ cap_ratio
    # ⟹ k ≤ cap_ratio * deterministic / (1 - cap_ratio)
    if cap_ratio >= 1.0:
        return residual, None
    max_k = int(cap_ratio * deterministic_count / (1.0 - cap_ratio))
    max_k = max(max_k, 0)
    if max_k >= len(residual):
        return residual, None

    # Rank by paths-count descending so the densest fallback features
    # survive; tie-break alphabetically for determinism.
    ranked = sorted(
        residual,
        key=lambda f: (-len(f.paths), f.name),
    )
    kept = ranked[:max_k]
    dropped = len(residual) - len(kept)
    warning = (
        f"stage_4_residual: dropped {dropped} fallback features to keep "
        f"share ≤ {int(cap_ratio * 100)}% "
        f"({deterministic_count} deterministic + {len(kept)} fallback)"
    )
    return kept, warning


# ── Public entry point ────────────────────────────────────────────────────


def stage_4_residual(
    unattributed_files: list[str],
    ctx: "ScanContext",
    existing_features: list[DeveloperFeature],
    *,
    model: str = DEFAULT_MODEL,
    max_chunks: int = MAX_CHUNKS,
    chunk_size: int = MAX_PATHS_PER_CHUNK,
    cost_cap_usd: float = DEFAULT_COST_CAP_USD,
    cost_tracker: CostTracker | None = None,
    client: Any | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> Stage4Result:
    """Run the residual LLM scanner over unattributed files.

    Args:
        unattributed_files: paths NOT claimed by any Stage 2 feature.
        ctx: Stage 0 context (kept for symmetry with sibling stages).
        existing_features: deterministic features from Stage 2 / Stage 3.
            Used to enforce the 30% LLM-fallback share cap.
        model: Haiku model id.
        max_chunks: hard cap on Haiku calls per scan.
        chunk_size: paths per Haiku call.
        cost_cap_usd: hard USD budget; aborts further chunks when hit.
        cost_tracker: optional shared tracker. A new one is created
            with ``max_cost=cost_cap_usd`` if None.
        client: pre-built Anthropic client (testing hook).
        _client_factory: injection point for the default builder.

    Returns:
        :class:`Stage4Result`.
    """
    # ctx is currently unused beyond symmetry; consume it for clarity
    # and to satisfy callers that pass it positionally.
    _ = ctx

    if not unattributed_files:
        return Stage4Result(
            residual_features=[],
            cost_usd=0.0,
            llm_calls=0,
            warnings=[],
            chunks_processed=0,
            rejected_names=[],
        )

    tracker = cost_tracker or CostTracker(max_cost=cost_cap_usd)

    if client is None:
        client = _client_factory()
    if client is None:
        return Stage4Result(
            residual_features=[],
            cost_usd=0.0,
            llm_calls=0,
            warnings=["no Anthropic client; residual scan skipped"],
            chunks_processed=0,
            rejected_names=[],
        )

    # Chunk the residual list.
    chunks: list[list[str]] = []
    for i in range(0, len(unattributed_files), chunk_size):
        chunks.append(unattributed_files[i : i + chunk_size])
        if len(chunks) >= max_chunks:
            break

    truncated = len(unattributed_files) > max_chunks * chunk_size
    warnings: list[str] = []
    if truncated:
        leftover = len(unattributed_files) - max_chunks * chunk_size
        warnings.append(
            f"stage_4_residual: {leftover} residual paths skipped after "
            f"the {max_chunks}-chunk cap"
        )

    allowed_paths_set = set(unattributed_files)

    raw_features_all: list[dict[str, Any]] = []
    llm_calls = 0
    chunks_processed = 0
    cost_aborted = False
    for chunk_idx, chunk_paths in enumerate(chunks):
        # Budget guard BEFORE calling — if the next call would exceed
        # we still attempt it once and rely on CostTracker to surface
        # the overage, but if we've already exceeded we stop hard.
        if tracker.max_cost is not None and tracker.total_cost_usd >= tracker.max_cost:
            cost_aborted = True
            warnings.append(
                f"stage_4_residual: cost cap ${tracker.max_cost:.2f} hit "
                f"after {chunks_processed} chunks; remaining skipped"
            )
            break
        prompt = _build_user_prompt(chunk_paths, chunk_idx, len(chunks))
        text, in_t, out_t = _call_haiku(
            client,
            model=model,
            system=_SYSTEM_PROMPT,
            user=prompt,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
        llm_calls += 1
        if in_t or out_t:
            tracker.record(
                provider="anthropic",
                model=model,
                input_tokens=in_t,
                output_tokens=out_t,
                label="stage-4-residual",
            )
        chunks_processed += 1
        if text:
            raw_features_all.extend(_parse_response(text))

    accepted, rejected = _build_developer_features(
        raw_features_all, allowed_paths_set,
    )

    # Enforce 30% LLM-fallback share cap against deterministic features.
    deterministic_count = len(existing_features)
    capped, cap_warning = _enforce_share_cap(accepted, deterministic_count)
    if cap_warning:
        warnings.append(cap_warning)

    if cost_aborted:
        # already logged above; nothing more to do
        pass

    return Stage4Result(
        residual_features=capped,
        cost_usd=tracker.total_cost_usd,
        llm_calls=llm_calls,
        warnings=warnings,
        chunks_processed=chunks_processed,
        rejected_names=rejected,
    )


__all__ = [
    "Stage4Result",
    "stage_4_residual",
]
