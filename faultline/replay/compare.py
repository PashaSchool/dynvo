"""Stage-artifact normalization + comparison for replay identity gates.

The identity ship-gate (WS1 spec) is: replaying stage N with unchanged
code must produce an output artifact byte-identical — AFTER
normalization — to the original run's ``NN-stage-<name>.json``.

Normalization REUSES the Phase-A scan normalizer
(:mod:`faultline.tools.normalize_scan`) rather than inventing a second
key catalogue: :func:`normalize_stage_artifact` first applies
``normalize_scan`` (top-level ``analyzed_at``, the ``scan_meta``
subtree scrub, wall-clock-decayed health fields) and then applies the
SAME volatile-key catalogue document-wide, because stage artifacts
carry the run-telemetry keys (``elapsed_sec`` / ``cost_usd`` /
``*_sample`` / ``calls``) at arbitrary depths, not only under
``scan_meta``. Two stage-artifact-only additions: top-level ``run_id``
(00-intake payload) and ``cache_hits`` counters (a replay is SUPPOSED
to hit the warm llm-cache where the original run paid for live calls —
identical content, different counter).

No LLM, no network, no engine imports beyond the normalizer.
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from typing import Any

from faultline.tools.normalize_scan import (
    _VOLATILE_IN_SCAN_META,
    _VOLATILE_SCAN_META,
    _VOLATILE_SCAN_META_SUFFIXES,
    canonical_json,
    normalize_scan,
)

__all__ = [
    "load_artifact",
    "normalize_stage_artifact",
    "stage_artifact_digest_text",
    "diff_summary",
]

#: Volatile keys stripped EVERYWHERE in a stage artifact. Union of the
#: scan normalizer's two scan_meta catalogues (reused, not re-declared)
#: plus the stage-artifact-only additions documented above.
_STAGE_VOLATILE: frozenset[str] = (
    _VOLATILE_IN_SCAN_META
    | _VOLATILE_SCAN_META
    | frozenset({
        "run_id", "run_dir", "cache_hits", "llm_calls",
        # chunked-flow twins of cache_hits / llm_calls (Stage 3
        # chunk_telemetry): a replay serves chunk units from the warm
        # llm-cache where the original run paid for live calls —
        # identical flows, shifted counters. The OTHER chunk_* keys
        # (features_chunked / chunks_total / flows_from_chunks) are
        # deterministic content counts and stay compared.
        "chunk_cache_hits", "chunk_llm_calls",
        # per-feature wall-clock timing in the import-tree artifact
        # (the scan normalizer covers duration_ms but not this spelling).
        "elapsed_ms",
        # additive replay marker (scan_meta.replayed_from) — a replayed
        # stage-7 artifact differs from the original ONLY by this stamp.
        "replayed_from",
    })
)

# Anthropic request ids leak into auth-degradation detail strings
# (scan_meta.llm_degraded.detail / degradations[].detail) — the only
# run-scoped token inside otherwise-meaningful text. Mask the id, keep
# the message.
_REQUEST_ID_RE = re.compile(r"req_[A-Za-z0-9]+")


def _scrub_everywhere(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _scrub_everywhere(value)
            for key, value in node.items()
            if key not in _STAGE_VOLATILE
            and not (
                isinstance(key, str)
                and key.endswith(_VOLATILE_SCAN_META_SUFFIXES)
            )
        }
    if isinstance(node, list):
        return [_scrub_everywhere(item) for item in node]
    if isinstance(node, str):
        return _REQUEST_ID_RE.sub("req_x", node)
    return node


def normalize_stage_artifact(doc: dict[str, Any]) -> dict[str, Any]:
    """Normalize one stage artifact for byte-stable comparison."""
    return _scrub_everywhere(normalize_scan(doc))


def load_artifact(path: Path | str) -> dict[str, Any]:
    """Read a (possibly gzipped) JSON artifact."""
    p = Path(path)
    raw = gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_bytes()
    return json.loads(raw)


def stage_artifact_digest_text(doc: dict[str, Any]) -> str:
    """Canonical JSON of the normalized artifact (identity-compare unit)."""
    return canonical_json(normalize_stage_artifact(doc))


def diff_summary(a: dict[str, Any], b: dict[str, Any], *, max_paths: int = 12) -> list[str]:
    """First ``max_paths`` JSON-pointer-ish paths where two NORMALIZED
    artifacts differ. Empty list == identical."""
    out: list[str] = []

    def _walk(x: Any, y: Any, path: str) -> None:
        if len(out) >= max_paths:
            return
        if type(x) is not type(y):
            out.append(f"{path}: type {type(x).__name__} != {type(y).__name__}")
            return
        if isinstance(x, dict):
            for k in sorted(set(x) | set(y)):
                if k not in x:
                    out.append(f"{path}/{k}: missing on left")
                elif k not in y:
                    out.append(f"{path}/{k}: missing on right")
                else:
                    _walk(x[k], y[k], f"{path}/{k}")
                if len(out) >= max_paths:
                    return
            return
        if isinstance(x, list):
            if len(x) != len(y):
                out.append(f"{path}: list len {len(x)} != {len(y)}")
            for i, (xi, yi) in enumerate(zip(x, y)):
                _walk(xi, yi, f"{path}[{i}]")
                if len(out) >= max_paths:
                    return
            return
        if x != y:
            out.append(f"{path}: {x!r} != {y!r}")

    _walk(normalize_stage_artifact(a), normalize_stage_artifact(b), "")
    return out
