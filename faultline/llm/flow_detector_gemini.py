"""Sprint 24 — Gemini-only flow detector.

A plain ``messages.create`` flow detector that's compatible with
Gemini Flash / Pro via the GeminiClient wrapper. Mirrors the output
shape of ``flow_detector.detect_flows_llm`` so the rest of the
pipeline doesn't care which path produced the flows.

Why a separate module
=====================

  - Legacy ``flow_detector.detect_flows_llm`` uses ``messages.parse()``
    (Anthropic structured-output) — Gemini wrapper doesn't expose this.
  - New ``flow_detector_v2`` uses Anthropic tool-use API — no Gemini
    translation layer in our wrapper.

This detector keeps Gemini support cheap: prompt → JSON output →
parse manually. Same flow-mapping shape so callers don't change.

Public surface
==============

    detect_flows_gemini(
        feature_name, feature_files, signatures,
        e2e_anchors=None, commits=None, model=None,
    ) -> list[_FlowFileMapping]

Re-uses ``_FlowFileMapping`` from ``flow_detector`` so the upstream
pipeline (``faultline.cli._run_flow_detection``) accepts the result
unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from faultline.llm.flow_detector import (
    _FlowFileMapping,
    _build_flow_extra_context,
    _build_signatures_text,
    _enrich_crud_gaps,
    _filter_valid_files,
    _format_e2e_anchors,
)

if TYPE_CHECKING:
    from faultline.analyzer.ast_extractor import FileSignature

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-flash-latest"
DEFAULT_MAX_TOKENS = 32_768


_SYSTEM_PROMPT = """\
You map files in a software feature to user-facing FLOWS — sequences \
of actions the end user can take. A flow is a single intent, e.g. \
"create-account-flow", "send-message-flow", "configure-billing-flow".

Rules:
  - Flow names: lowercase, kebab-case, end with "-flow".
  - Each flow ties to AT LEAST ONE file from the input.
  - Multiple flows may share the same file (UI shells often do).
  - Skip pure infrastructure files (tsconfig, vite.config, lockfiles).
  - 3-12 flows per feature is the typical range; emit fewer if the \
    feature genuinely has fewer user actions.

Return ONLY this JSON shape (no prose, no markdown fence):

{
  "flows": [
    {"flow_name": "<name>-flow", "files": ["<path>", ...]},
    ...
  ]
}
"""


def _parse_flows_json(text: str) -> list[_FlowFileMapping]:
    """Extract the JSON envelope and coerce to ``_FlowFileMapping``s.

    Tolerates markdown fences, trailing prose, and the
    ``flows: [string, ...]`` short-form (rare but seen with Flash).
    """
    if not text:
        return []
    # Strip markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*\n", "", text.strip())
    cleaned = re.sub(r"\n```\s*$", "", cleaned.strip())
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return []
    flows_raw = data.get("flows")
    if not isinstance(flows_raw, list):
        return []
    out: list[_FlowFileMapping] = []
    for entry in flows_raw:
        if isinstance(entry, str):
            # Bare-string short-form — coerce with empty file list
            out.append(_FlowFileMapping(flow_name=entry, files=[]))
            continue
        if not isinstance(entry, dict):
            continue
        name = entry.get("flow_name") or entry.get("name") or ""
        files = entry.get("files") or []
        if not isinstance(name, str) or not isinstance(files, list):
            continue
        if not name.strip():
            continue
        # Coerce file list to strings
        valid_files = [f for f in files if isinstance(f, str) and f.strip()]
        out.append(_FlowFileMapping(
            flow_name=name.strip(), files=valid_files,
        ))
    return out


def detect_flows_gemini(
    feature_name: str,
    feature_files: list[str],
    signatures: dict[str, "FileSignature"],
    e2e_anchors: dict[str, list[str]] | None = None,
    commits: list | None = None,
    model: str | None = None,
) -> list[_FlowFileMapping]:
    """Detect flows for one feature via Gemini. Returns [] on any failure.

    Lazily imports ``GeminiClient`` and ``client_factory`` so this
    module is cheap to load when Gemini-mode isn't active.
    """
    if not feature_files:
        return []

    from faultline.llm.client_factory import (
        _gemini_only_enabled, gemini_model_for, make_llm_client,
    )
    if not _gemini_only_enabled():
        # Defensive — caller should have routed elsewhere.
        return []

    try:
        client = make_llm_client(api_key=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("flow_detector_gemini: client init failed (%s)", exc)
        return []

    resolved_model = gemini_model_for(model or DEFAULT_MODEL) or DEFAULT_MODEL

    signatures_text = _build_signatures_text(feature_files, signatures)
    e2e_context = _format_e2e_anchors(e2e_anchors or {})
    extra_context = _build_flow_extra_context(feature_files, signatures, commits)

    user_prompt = (
        f"Feature: {feature_name}\n\n"
        f"{e2e_context}\n\n"
        f"FILES (with signatures):\n{signatures_text}\n\n"
        f"{extra_context}"
    )

    try:
        resp = client.messages.create(
            model=resolved_model,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "flow_detector_gemini(%s): API call failed (%s)",
            feature_name, exc,
        )
        return []

    text = ""
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            text += getattr(block, "text", "")

    raw = _parse_flows_json(text)
    if not raw:
        logger.warning(
            "flow_detector_gemini(%s): empty / unparseable response",
            feature_name,
        )
        return []

    filtered = _filter_valid_files(raw, set(feature_files))
    return _enrich_crud_gaps(filtered, feature_name, feature_files, signatures)
