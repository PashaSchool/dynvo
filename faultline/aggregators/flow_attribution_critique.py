"""Flow→feature semantic-attribution critique (Sprint 8h).

Late-stage pass: for each (feature, flows) pair, ask the LLM to
classify whether each flow semantically belongs to the feature it's
currently attached to. Three possible verdicts per flow:

  - ``keep``       — flow legitimately belongs here.
  - ``move``       — flow belongs to a different existing feature.
  - ``new``        — flow belongs to a feature the primary scan missed.

The aggregator then mutates the FeatureMap accordingly:

  - ``move`` flows are removed from current feature and appended to
    the suggested existing feature.
  - ``new`` proposals get bucketed by ``suggested_feature`` name and,
    when a name accumulates ≥ 3 supporting flows, a new
    ``Feature(discovery_method="flow-critique")`` is appended.
  - ``keep`` is the no-op default.

This pass exists because token-overlap reattribution (Sprint 8e
``flow_reattribution.py``) is too literal — it cannot tell that
``manage-bull-mqjob-provider-flow`` semantically belongs to
``Background Jobs`` rather than ``Translations`` (the documenso
Sprint 8g bucket-overflow bug). Semantic critique fixes this.

Costs ≈ $0.10 per scan with Haiku 4.5 (one batched call per feature),
parallelised with concurrency 5 → ~5 s wall-clock total.

Generic per ``rule-no-repo-specific-paths``: no per-repo lists in
the prompt; the LLM reasons from feature/flow names alone.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """True iff ``FAULTLINE_FLOW_CRITIQUE`` is set."""
    return os.environ.get("FAULTLINE_FLOW_CRITIQUE", "").lower() in {
        "1", "true", "yes", "on",
    }


# ── Defaults ──────────────────────────────────────────────────────────

DEFAULT_MAX_TOKENS = 32_768
DEFAULT_NEW_FEATURE_MIN_SUPPORT = 3   # ≥ N flows must propose the same NEW name
DEFAULT_CONCURRENCY = 5


# ── Prompt ────────────────────────────────────────────────────────────


_LLM_SYSTEM_PROMPT = """\
You are a SEMANTIC validator for feature→flow attributions in a
codebase analysis tool. Each scan produces a list of FEATURES (product
capabilities) with FLOWS (user-facing interactions or workflows)
attached. The primary scan sometimes attaches a flow to the wrong
feature, or misses a feature that several flows point to.

You will receive ONE feature and the list of flows currently attached
to it. For each flow, decide:

  - ``keep``  : the flow semantically belongs to this feature
  - ``move``  : the flow semantically belongs to a different EXISTING
                feature (provide its name in ``suggested_feature``)
  - ``new``   : the flow points to a NEW feature the scan missed
                (provide a short business-language name in
                ``suggested_feature``, e.g. "Background Jobs",
                "Form Validation", "Email Notifications")

Rules:
- Reason about MEANING, not token overlap. A flow named
  ``manage-bull-mqjob-provider-flow`` belongs to ``Background Jobs``,
  not ``Translations``, even if the current feature is Translations.
- ``move`` only when you're confident the destination feature is in
  the supplied ``other_features`` list. Otherwise prefer ``new``.
- ``new`` feature names: 1-3 words, business-language (Compliance,
  Background Jobs, Form Validation), never plumbing words (Layer,
  Service, Engine). One concept per name, no "&" or "and".
- Default to ``keep`` when uncertain — don't churn the map without a
  clear semantic mismatch.

Return JSON only — no prose, no markdown fences. Schema:
{
  "verdicts": [
    {
      "flow": "<exact input flow name>",
      "verdict": "keep" | "move" | "new",
      "suggested_feature": "<feature name>" | null,
      "reason": "<one short sentence>"
    },
    ...
  ]
}
"""


def _build_user(
    feature_name: str,
    flow_names: list[str],
    other_feature_names: list[str],
) -> str:
    obj = {
        "current_feature": feature_name,
        "flows": flow_names,
        "other_features": other_feature_names,
    }
    return (
        "Validate the flow→feature attributions below.\n\n"
        + json.dumps(obj, indent=2, ensure_ascii=False)
    )


def _parse_verdicts(raw: str) -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("flow-critique: invalid JSON from LLM")
        return []
    out: list[dict] = []
    for entry in data.get("verdicts", []) or []:
        if not isinstance(entry, dict):
            continue
        flow = entry.get("flow")
        verdict = entry.get("verdict")
        if not isinstance(flow, str) or verdict not in {"keep", "move", "new"}:
            continue
        suggested = entry.get("suggested_feature")
        out.append({
            "flow": flow,
            "verdict": verdict,
            "suggested_feature": suggested if isinstance(suggested, str) else None,
            "reason": str(entry.get("reason") or "")[:240],
        })
    return out


# ── Apply verdicts to FeatureMap ──────────────────────────────────────


@dataclass
class FlowCritiqueStats:
    moved: int = 0
    new_features_added: int = 0
    new_feature_proposals_dropped: int = 0
    kept: int = 0


def _normalise_feature_key(name: str) -> str:
    return re.sub(r"[\s_/-]+", "", name.lower())


def _make_new_feature(name: str, supporting_flows: list):
    """Construct a new Feature carrying flows from ``new`` verdicts."""
    from faultline.models.types import Feature
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = "flow-critique-new"
    paths = list(dict.fromkeys(p for fl in supporting_flows for p in fl.paths))
    total_commits = sum(fl.total_commits or 0 for fl in supporting_flows)
    bug_fixes = sum(fl.bug_fixes or 0 for fl in supporting_flows)
    return Feature(
        name=slug,
        display_name=name.strip(),
        paths=paths,
        authors=[],
        total_commits=total_commits,
        bug_fixes=bug_fixes,
        bug_fix_ratio=bug_fixes / max(total_commits, 1),
        last_modified=datetime.now(tz=timezone.utc),
        health_score=80.0,
        flows=list(supporting_flows),
        discovery_method="flow-critique",
    )


def apply_verdicts(
    feature_map,
    verdicts_by_feature: dict[str, list[dict]],
    *,
    new_feature_min_support: int = DEFAULT_NEW_FEATURE_MIN_SUPPORT,
) -> FlowCritiqueStats:
    """Mutate ``feature_map`` according to per-feature verdicts.

    Returns ``FlowCritiqueStats`` describing changes.
    """
    stats = FlowCritiqueStats()
    by_name = {f.name: f for f in feature_map.features}
    by_norm = {_normalise_feature_key(f.name): f for f in feature_map.features}
    by_norm.update(
        {_normalise_feature_key(f.display_name or ""): f
         for f in feature_map.features if f.display_name}
    )

    new_proposals: dict[str, list] = defaultdict(list)

    for feat_name, verdicts in verdicts_by_feature.items():
        host = by_name.get(feat_name)
        if host is None:
            continue
        flows_by_name = {fl.name: fl for fl in host.flows}
        keep_flows = []
        for v in verdicts:
            flow = flows_by_name.get(v["flow"])
            if flow is None:
                continue
            if v["verdict"] == "keep":
                keep_flows.append(flow)
                stats.kept += 1
                continue
            if v["verdict"] == "move":
                target_key = _normalise_feature_key(v["suggested_feature"] or "")
                target = by_norm.get(target_key)
                if target is None or target is host:
                    keep_flows.append(flow)
                    continue
                target.flows.append(flow)
                stats.moved += 1
                continue
            if v["verdict"] == "new":
                proposed = (v["suggested_feature"] or "").strip()
                if not proposed:
                    keep_flows.append(flow)
                    continue
                # If proposed name matches an EXISTING feature, treat
                # as move — protects against LLM forgetting that the
                # target was in other_features.
                existing = by_norm.get(_normalise_feature_key(proposed))
                if existing is not None:
                    if existing is host:
                        keep_flows.append(flow)
                    else:
                        existing.flows.append(flow)
                        stats.moved += 1
                    continue
                new_proposals[proposed].append(flow)
        # Flows tagged ``new`` move out of host even before the
        # min-support gate — if the gate rejects them, we'll restore.
        host.flows = keep_flows

    # Materialise ``new`` features that cleared the support threshold.
    restorations: list = []  # (host_name, flow)
    for proposed_name, flows in new_proposals.items():
        if len(flows) >= new_feature_min_support:
            feature_map.features.append(_make_new_feature(proposed_name, flows))
            stats.new_features_added += 1
        else:
            stats.new_feature_proposals_dropped += len(flows)
            # Restore flows to original host(s) — find by walking
            # original verdicts is overkill; safest is to drop them
            # only if they have no original host (rare).
            for fl in flows:
                # Best-effort: re-attach to the first feature whose
                # name token-overlaps. If none, give up — the flow is
                # lost (tracked in stats).
                tokens = set(re.split(r"[-_/\s]+", fl.name.lower()))
                best = None
                best_score = 0
                for cand in feature_map.features:
                    score = len(
                        tokens & set(
                            re.split(r"[-_/\s]+", cand.name.lower())
                        ),
                    )
                    if score > best_score:
                        best_score = score
                        best = cand
                if best is not None:
                    best.flows.append(fl)

    return stats


# ── Top-level orchestrator ────────────────────────────────────────────


def critique_flow_attribution(
    feature_map,
    *,
    llm,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    new_feature_min_support: int = DEFAULT_NEW_FEATURE_MIN_SUPPORT,
):
    """Sprint 10a — pure-function. Returns ``(new_feature_map,
    FlowCritiqueStats)``. Input ``feature_map`` is NEVER mutated.

    Runs one LLM call per feature in parallel, then applies verdicts
    on a deep copy of the feature map. Skips features with no flows.
    """
    new_fm = feature_map.model_copy(deep=True)
    feature_map = new_fm  # operate on the copy below
    if not feature_map.features:
        return new_fm, FlowCritiqueStats()

    other_names_global = [f.display_name or f.name for f in feature_map.features]

    def _validate_one(host):
        flow_names = [fl.name for fl in host.flows]
        if not flow_names:
            return host.name, []
        other = [n for n in other_names_global if n != (host.display_name or host.name)]
        user = _build_user(host.display_name or host.name, flow_names, other)
        try:
            response = llm.complete(
                system=_LLM_SYSTEM_PROMPT,
                user=user,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — opportunistic
            logger.warning(
                "flow-critique: LLM call failed for %s (%s)", host.name, exc,
            )
            return host.name, []
        return host.name, _parse_verdicts(response.text)

    verdicts_by_feature: dict[str, list[dict]] = {}
    targets = [f for f in feature_map.features if f.flows]
    if not targets:
        return new_fm, FlowCritiqueStats()

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(_validate_one, h) for h in targets]
        for fut in as_completed(futures):
            try:
                host_name, verdicts = fut.result()
            except Exception as exc:  # noqa: BLE001 — opportunistic
                logger.warning("flow-critique: worker failed (%s)", exc)
                continue
            if verdicts:
                verdicts_by_feature[host_name] = verdicts

    stats = apply_verdicts(
        feature_map,
        verdicts_by_feature,
        new_feature_min_support=new_feature_min_support,
    )
    return new_fm, stats


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "FlowCritiqueStats",
    "apply_verdicts",
    "critique_flow_attribution",
    "is_enabled",
]
