"""Stage 6.7b — User-Flow LLM refiner (additive Haiku pass).

Runs AFTER the deterministic Stage 6.7 rollup
(:mod:`faultline.pipeline_v2.stage_6_7_user_flows`). Per *domain*, makes
ONE Haiku call over that domain's deterministic UF clusters and refines
their *presentation* — WITHOUT re-clustering or changing membership /
grain. Stage 6.7 stays the single source of truth for which flows
compose which UF; this stage only rewrites human-facing fields.

What it fills onto each :class:`~faultline.models.types.UserFlow`:

  * ``name`` + ``description`` — journey-grain language ("Create a
    detector", "Promote a detector through its lifecycle") replacing the
    Stage-6.7 template name.
  * ``intent`` — a proper class for UFs whose deterministic intent is
    ``"other"`` (verbs the fixed table didn't cover: validate / track /
    inspect …).
  * ``ui_tier`` — one of ``full-page | panel | settings | admin |
    no-ui``, inferred from the FRONTEND surface (``ui``-layer
    participant paths) the member flows touch.
  * ``acceptance`` — first-draft ``AC-n`` strings, one per member flow
    that has a ``test_files`` entry (test-reach) — concise observable
    assertions.

Design — mirrors the Stack Auditor (Stage 0.5)
==============================================

This is a SELF-CONTAINED additive Haiku stage with its own system
prompt, its own per-call cost accounting recorded into the shared
:class:`CostTracker`, and a graceful per-domain degrade: if the LLM
call for a domain fails (no client / API error / cost cap / bad JSON),
the UFs in that domain KEEP their deterministic Stage-6.7 name + intent
and are simply left ``refined=False``. The scan never crashes.

Hard rules honoured
===================

  * **No README** — the only code-grounded inputs are flow names,
    router routes, and frontend component / nav labels (``ui``-layer
    participant file paths + symbols). No ``.md`` prose is ever read.
  * **No ``.ai/specs``** — that reverse-spec is VALIDATION-ONLY; its
    names / counts are NEVER fed to the LLM or used to tune the prompt.
  * **Additive only** — a brand-new prompt; no existing Stage 3/4/8 /
    auditor prompt is touched. Membership/grain from 6.7 are immutable.
  * **No magic numbers** — ``ui_tier`` is a categorical choice from the
    frontend surface, AC count = number of test-reached members; both
    are structural, not tuned thresholds.

Spec: faultlines-app/docs/specs/flow-to-user-flow-rollup.md (Stage D).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable

from faultline.llm.cost import CostTracker, deterministic_params

if TYPE_CHECKING:
    from faultline.models.types import Flow, UserFlow
    from faultline.pipeline_v2.run_logger import StageLogger

logger = logging.getLogger(__name__)
from faultline.llm.model_gateway import resolve_model as gateway_model


# ── Tunables ────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# One call per domain. Output is small structured JSON (one record per
# UF in the domain); 1500 tokens covers a large domain (~7 UFs) with
# names + descriptions + AC drafts.
DEFAULT_MAX_TOKENS = 1500
# Per-DOMAIN defensive cost cap. A domain with many UFs + members can
# inflate input tokens; $0.05/domain keeps even a 40-domain repo under
# ~$2 total while catching a runaway malformed response. Exceeding it
# degrades that domain to its deterministic verdict (refined=False).
COST_CAP_USD_PER_DOMAIN = 0.05
# Bound the per-UF context handed to the LLM so a huge cluster can't
# balloon the prompt. These cap LIST LENGTHS in the prompt only — they
# never change membership (Stage 6.7 owns that).
MAX_MEMBER_NAMES = 24
MAX_ROUTES = 16
MAX_UI_LABELS = 16

_VALID_UI_TIERS = ("full-page", "panel", "settings", "admin", "no-ui")
_VALID_INTENTS = (
    "author", "browse", "lifecycle", "execute", "manage", "bulk", "export",
)

# ``ui``-layer participant file → human nav/component label. Strip dir +
# extension, drop a leading ``use`` (React hook) so ``useDetectorTable``
# reads as ``DetectorTable``. Universal, not repo-specific.
_UI_EXT_RE = re.compile(r"\.(t|j)sx?$")
_ROUTE_HINT_RE = re.compile(r"routers?/")


# ── Anthropic client protocol (for tests / IoC) ─────────────────────────────


def _default_client_factory() -> Any | None:  # pragma: no cover - IO
    """Lazy Anthropic client builder. Returns ``None`` when the SDK or
    API key are absent — the refiner then degrades to the deterministic
    Stage-6.7 verdict without erroring.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


# ── Code-grounded input builders (README-FORBIDDEN) ─────────────────────────


def _ui_label(path: str) -> str | None:
    """Frontend component / nav label from a ``ui``-layer file path.

    ``frontend/src/components/DetectorTable.tsx`` → ``DetectorTable``.
    Returns ``None`` for non-frontend-looking paths so we never leak
    backend file names as "nav labels".
    """
    if not path:
        return None
    base = path.rsplit("/", 1)[-1]
    base = _UI_EXT_RE.sub("", base)
    if not base or base in ("index", "page", "layout", "route"):
        # index/page files carry the folder as their label.
        seg = [p for p in path.split("/") if p][:-1]
        base = seg[-1] if seg else base
    if not base:
        return None
    if base.startswith("use") and len(base) > 3 and base[3].isupper():
        base = base[3:]
    return base


def _ui_surface(flows: list["Flow"]) -> list[str]:
    """Distinct frontend surface labels touched by a UF's member flows.

    Reads ``ui``-layer participants (the frontend classifier already
    tagged them) + ``frontend``-pathed participants. This is explicit
    author-intent code, NOT prose — allowed per CLAUDE.md.
    """
    labels: list[str] = []
    seen: set[str] = set()
    for f in flows:
        for p in getattr(f, "participants", None) or []:
            layer = getattr(p, "layer", "") or ""
            path = getattr(p, "path", "") or ""
            is_ui = layer == "ui" or "frontend/" in path or "/components/" in path
            if not is_ui:
                continue
            label = _ui_label(path)
            if label and label not in seen:
                seen.add(label)
                labels.append(label)
                if len(labels) >= MAX_UI_LABELS:
                    return labels
    return labels


def _has_ui(flows: list["Flow"]) -> bool:
    for f in flows:
        for p in getattr(f, "participants", None) or []:
            if (getattr(p, "layer", "") or "") == "ui":
                return True
            path = getattr(p, "path", "") or ""
            if "frontend/" in path or "/components/" in path:
                return True
    return False


def _settings_pathed(flows: list["Flow"]) -> bool:
    for f in flows:
        for path in (getattr(f, "paths", None) or []):
            if "/settings" in path or "settings/" in path:
                return True
    return False


def _admin_pathed(flows: list["Flow"]) -> bool:
    for f in flows:
        for path in (getattr(f, "paths", None) or []):
            low = path.lower()
            if "/admin" in low or "admin/" in low:
                return True
    return False


def _default_ui_tier(flows: list["Flow"]) -> str:
    """Deterministic ui_tier fallback when the LLM doesn't answer.

    Structural only: no UI participants → no-ui; settings/admin paths →
    those tiers; otherwise full-page (a member route exists). Never a
    tuned threshold.
    """
    if _settings_pathed(flows):
        return "settings"
    if _admin_pathed(flows):
        return "admin"
    if not _has_ui(flows):
        return "no-ui"
    return "full-page"


def _member_flows_for(uf: "UserFlow", flows_by_key: dict[str, "Flow"]) -> list["Flow"]:
    out: list["Flow"] = []
    for mid in uf.member_flow_ids:
        f = flows_by_key.get(mid)
        if f is not None:
            out.append(f)
    return out


def _uf_payload(
    uf: "UserFlow", members: list["Flow"]
) -> dict[str, Any]:
    """Code-grounded context for one UF handed to the LLM.

    Only names / routes / frontend labels / tested-member count — the
    allowed code-grounded sources. No prose, no .ai/specs.
    """
    names: list[str] = []
    for m in members[:MAX_MEMBER_NAMES]:
        nm = getattr(m, "display_name", None) or m.name
        names.append(nm)
    routes = list(uf.routes)[:MAX_ROUTES]
    ui_labels = _ui_surface(members)
    tested = [m.name for m in members if getattr(m, "test_files", None)]
    return {
        "id": uf.id,
        "deterministic_name": uf.name,
        "intent": uf.intent,
        "resource": uf.resource,
        "member_flow_names": names,
        "routes": routes,
        "frontend_labels": ui_labels,
        "tested_member_count": len(tested),
        "default_ui_tier": _default_ui_tier(members),
    }


_SYSTEM_PROMPT = (
    "You name product user-journeys from code signals. You are given, for "
    "one product DOMAIN, a list of deterministically-clustered user flows "
    "(UFs). Each UF already has a fixed membership and a representative "
    "resource noun — DO NOT merge, split, or re-assign them. For each UF, "
    "produce journey-language metadata grounded ONLY in the provided code "
    "signals (member flow names, HTTP routes, frontend component/nav "
    "labels). Never invent capabilities not implied by those signals. "
    "Never reference documentation or marketing.\n\n"
    "For each UF return:\n"
    "  - name: a concise user-journey title in imperative voice "
    "('Create a detector', 'Promote a detector through its lifecycle', "
    "'Browse and filter alerts'). No trailing 'flow'. Title case-ish, "
    "<= 7 words.\n"
    "  - description: one sentence (<= 20 words) describing the journey "
    "from the user's perspective.\n"
    "  - intent: one of author|browse|lifecycle|execute|manage|bulk|export. "
    "Choose the class that best fits the member verbs (validate/inspect/"
    "track -> manage or browse as appropriate). Keep the given intent "
    "unless it is 'other'.\n"
    "  - ui_tier: one of full-page|panel|settings|admin|no-ui, inferred "
    "from the frontend labels/routes. If no frontend signal, use the "
    "provided default_ui_tier.\n"
    "  - acceptance: an array with EXACTLY tested_member_count short "
    "observable assertions (e.g. 'User can create a detector and it "
    "appears in the list'). Empty array if tested_member_count is 0.\n\n"
    "Output STRICT JSON only, no markdown fence: "
    '{"user_flows": [{"id": "...", "name": "...", "description": "...", '
    '"intent": "...", "ui_tier": "...", "acceptance": ["..."]}]}'
)


def _build_user_prompt(domain: str | None, uf_payloads: list[dict]) -> str:
    return (
        f"DOMAIN: {domain or 'uncategorized'}\n\n"
        f"USER_FLOWS:\n{json.dumps(uf_payloads, indent=1)}\n\n"
        "Return refined metadata for every UF id above, in the same order."
    )


# ── LLM call (mirrors stack_auditor._call_haiku) ────────────────────────────


def _call_haiku(
    client: Any,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
) -> tuple[str, int, int]:
    """One Haiku call. Returns ``(text, in_tokens, out_tokens)``."""
    try:
        msg = client.messages.create(
            model=gateway_model(model),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal at scan-time
        logger.warning("uf_refiner: Haiku call failed: %s", exc)
        return "", 0, 0
    try:
        text_parts = []
        for block in msg.content:
            t = getattr(block, "text", None)
            if t:
                text_parts.append(t)
        text = "\n".join(text_parts)
    except Exception:  # noqa: BLE001
        text = ""
    in_tokens = int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0)
    out_tokens = int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)
    return text, in_tokens, out_tokens


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_refinement(text: str) -> dict[str, dict] | None:
    """Parse the LLM JSON into ``{uf_id: refinement_dict}``.

    Tolerates a markdown fence by extracting the first ``{...}`` span.
    Returns ``None`` on any structural failure (caller degrades).
    """
    if not text:
        return None
    raw = text.strip()
    if not raw.startswith("{"):
        m = _JSON_OBJ_RE.search(raw)
        if not m:
            return None
        raw = m.group(0)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("user_flows")
    if not isinstance(rows, list):
        return None
    out: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            out[row["id"]] = row
    return out or None


def _apply_refinement(
    uf: "UserFlow",
    row: dict,
    members: list["Flow"],
) -> bool:
    """Stamp one refinement onto a UF in place. Returns True if applied.

    Validates every LLM field; on a missing/invalid field, keeps the
    deterministic value for that field (partial refine still counts).
    AC list is bounded to the count of test-reached members (the LLM is
    asked for exactly that many, but we enforce it structurally).
    """
    applied = False

    name = row.get("name")
    if isinstance(name, str) and name.strip():
        uf.name = name.strip()
        applied = True

    desc = row.get("description")
    if isinstance(desc, str) and desc.strip():
        uf.description = desc.strip()
        applied = True

    intent = row.get("intent")
    if isinstance(intent, str) and intent in _VALID_INTENTS:
        # Only overwrite when the deterministic intent was "other"
        # (the LLM is told to keep a valid given intent); if the model
        # reclassifies an "other", accept it.
        if uf.intent == "other":
            uf.intent = intent
            applied = True

    ui_tier = row.get("ui_tier")
    if isinstance(ui_tier, str) and ui_tier in _VALID_UI_TIERS:
        uf.ui_tier = ui_tier
        applied = True
    elif uf.ui_tier is None:
        uf.ui_tier = _default_ui_tier(members)

    tested = sum(1 for m in members if getattr(m, "test_files", None))
    acc = row.get("acceptance")
    if isinstance(acc, list) and tested > 0:
        clean = [str(a).strip() for a in acc if isinstance(a, str) and str(a).strip()]
        clean = clean[:tested]
        uf.acceptance = [f"AC-{i + 1}: {txt}" for i, txt in enumerate(clean)]
        if uf.acceptance:
            applied = True

    return applied


# ── Public entry point ──────────────────────────────────────────────────────


def refine_user_flows(
    user_flows: list["UserFlow"],
    flows: list["Flow"],
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    cost_tracker: CostTracker | None = None,
    log: "StageLogger | None" = None,
    domain_allowlist: set[str | None] | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> tuple[list["UserFlow"], dict[str, Any]]:
    """Refine ``user_flows`` in place via one Haiku call per domain.

    Args:
        user_flows: the Stage-6.7 deterministic UFs (mutated in place).
        flows: the final bipartite flow store (for member lookup +
            frontend/ui signal). Read-only.
        client: pre-built Anthropic-like client (tests pass a fake).
        model: Haiku model id.
        cost_tracker: shared :class:`CostTracker`; cost is recorded
            into it AND summed in the returned telemetry.
        log: optional :class:`StageLogger`.
        domain_allowlist: INCREMENTAL-PATH gating hook. When ``None``
            (the default, and ALWAYS on a full / cold scan) every domain
            is refined — behaviour is byte-identical to before this hook
            existed. When a set is supplied (only the ``--since`` path
            passes one), domains NOT in it skip their Haiku call and keep
            whatever fields they already carry (on the incremental path
            those are base-scan refinements applied upstream by
            ``incremental_gate.plan_uf_refinement_reuse``). This is how
            Stage 6.7b reuse is realised without re-clustering.
        _client_factory: injection hook for the default client builder.

    Returns:
        ``(user_flows, telemetry)``. Always returns; never raises for
        IO failures. Domains whose call fails keep their deterministic
        name/intent and ``refined=False``.
    """
    telemetry: dict[str, Any] = {
        "enabled": True,
        "domains_total": 0,
        "domains_refined": 0,
        "domains_degraded": 0,
        "uf_refined": 0,
        "intent_other_before": sum(1 for uf in user_flows if uf.intent == "other"),
        "intent_other_after": 0,
        "ui_tier_set": 0,
        "acceptance_total": 0,
        "cost_usd": 0.0,
        "fallback_reason": None,
    }
    if not user_flows:
        return user_flows, telemetry

    if client is None:
        client = _client_factory()
    if client is None:
        telemetry["enabled"] = False
        telemetry["fallback_reason"] = "no_anthropic_client"
        # Still apply the deterministic ui_tier so the field is never
        # left null even when the LLM is unavailable (additive, $0).
        _apply_deterministic_ui_tiers(user_flows, flows)
        telemetry["ui_tier_set"] = sum(1 for uf in user_flows if uf.ui_tier)
        telemetry["intent_other_after"] = telemetry["intent_other_before"]
        if log is not None:
            log.warn("uf_refiner: no Anthropic client; keeping deterministic UFs")
        return user_flows, telemetry

    flows_by_key: dict[str, "Flow"] = {}
    for f in flows:
        flows_by_key[f.uuid or f.name] = f
        flows_by_key.setdefault(f.name, f)

    # Group by the CODE-GRAIN domain (cluster key), not the Layer-2
    # product_feature_id marketing link. The refiner batches one LLM call
    # per code-grain domain; product_feature_id is a separate grouping link
    # and would over-merge unrelated code domains under one marketing label.
    by_domain: dict[str | None, list["UserFlow"]] = defaultdict(list)
    for uf in user_flows:
        by_domain[uf.domain].append(uf)
    telemetry["domains_total"] = len(by_domain)

    tracker = cost_tracker or CostTracker(max_cost=None)
    total_cost = 0.0
    domains_reused = 0

    for domain, ufs in sorted(by_domain.items(), key=lambda kv: str(kv[0])):
        # Incremental reuse: a domain NOT in the allowlist had every UF
        # matched to a refined base twin upstream — its members are
        # unchanged, so re-calling Haiku would just reproduce the base
        # presentation. Skip the call; keep the already-applied fields.
        if domain_allowlist is not None and domain not in domain_allowlist:
            domains_reused += 1
            continue
        members_by_uf = {
            uf.id: _member_flows_for(uf, flows_by_key) for uf in ufs
        }
        payloads = [_uf_payload(uf, members_by_uf[uf.id]) for uf in ufs]
        user_prompt = _build_user_prompt(domain, payloads)

        text, in_tok, out_tok = _call_haiku(
            client,
            model=model,
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=DEFAULT_MAX_TOKENS,
        )

        call_cost = 0.0
        if in_tok or out_tok:
            entry = tracker.record(
                provider="anthropic",
                model=model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                label="stage-6.7b-uf-refiner",
            )
            call_cost = float(getattr(entry, "cost_usd", 0.0) or 0.0)
            total_cost += call_cost

        degraded_reason: str | None = None
        if call_cost > COST_CAP_USD_PER_DOMAIN:
            degraded_reason = f"cost_cap ${call_cost:.4f}"
        else:
            parsed = _parse_refinement(text)
            if parsed is None:
                degraded_reason = "json_parse_failed"

        if degraded_reason is not None:
            telemetry["domains_degraded"] += 1
            # Graceful degrade: deterministic ui_tier so field isn't null.
            for uf in ufs:
                if uf.ui_tier is None:
                    uf.ui_tier = _default_ui_tier(members_by_uf[uf.id])
            if log is not None:
                log.warn(
                    f"uf_refiner: domain={domain!r} degraded "
                    f"({degraded_reason}); kept deterministic names",
                )
            continue

        refined_here = 0
        for uf in ufs:
            row = parsed.get(uf.id)
            members = members_by_uf[uf.id]
            if row and _apply_refinement(uf, row, members):
                uf.refined = True
                refined_here += 1
            elif uf.ui_tier is None:
                uf.ui_tier = _default_ui_tier(members)
        telemetry["domains_refined"] += 1
        telemetry["uf_refined"] += refined_here

    telemetry["cost_usd"] = round(total_cost, 6)
    telemetry["domains_reused"] = domains_reused
    telemetry["intent_other_after"] = sum(
        1 for uf in user_flows if uf.intent == "other"
    )
    telemetry["ui_tier_set"] = sum(1 for uf in user_flows if uf.ui_tier)
    telemetry["acceptance_total"] = sum(len(uf.acceptance) for uf in user_flows)

    if log is not None:
        log.info(
            f"uf_refiner: {telemetry['domains_refined']}/{telemetry['domains_total']} "
            f"domains refined ({telemetry['domains_degraded']} degraded), "
            f"{telemetry['uf_refined']} UF refined, "
            f"intent_other {telemetry['intent_other_before']}->{telemetry['intent_other_after']}, "
            f"ui_tier_set={telemetry['ui_tier_set']}, "
            f"AC={telemetry['acceptance_total']}, cost=${telemetry['cost_usd']:.4f}",
        )
    return user_flows, telemetry


def _apply_deterministic_ui_tiers(
    user_flows: list["UserFlow"], flows: list["Flow"]
) -> None:
    """Fill ui_tier deterministically (used on the no-client path)."""
    flows_by_key: dict[str, "Flow"] = {}
    for f in flows:
        flows_by_key[f.uuid or f.name] = f
        flows_by_key.setdefault(f.name, f)
    for uf in user_flows:
        if uf.ui_tier is None:
            members = _member_flows_for(uf, flows_by_key)
            uf.ui_tier = _default_ui_tier(members)


__all__ = [
    "COST_CAP_USD_PER_DOMAIN",
    "DEFAULT_MODEL",
    "refine_user_flows",
]
