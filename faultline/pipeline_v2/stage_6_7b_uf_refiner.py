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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from faultline.llm.cost import CostTracker, deterministic_params, estimate_call_cost
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.naming_validator import (
    EvidenceBundle,
    retry_prohibition,
    validate_name,
)

if TYPE_CHECKING:
    from faultline.models.types import Flow, UserFlow
    from faultline.pipeline_v2.product_strings import ProductStringIndex
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

# ThreadPool size for the per-DOMAIN refinement LLM calls. One Haiku call (plus
# at most one name-validation retry) per code-grain domain; on big repos with
# many domains the SEQUENTIAL per-domain latency dominated this stage's
# wall-time (Phase 5, ~70min on the largest units). The domains are
# independent, so we fan their LLM IO out over a bounded pool — execution-only,
# the assembled result is byte-identical to the sequential run (see
# ``refine_user_flows`` for the ordering proof). Tunable via env so a deploy
# can change concurrency WITHOUT an engine release; bounded [1, 32] to respect
# the provider rate limit. Shares the Stage 6 env knob with stage_6_7c so one
# var sizes both Stage 6 LLM fan-outs (default 8, matching stage_3_flows).
DEFAULT_MAX_WORKERS = max(
    1, min(32, int(os.environ.get("FAULTLINES_STAGE6_MAX_WORKERS", "8") or "8"))
)


@dataclass
class _DomainResult:
    """Everything the SEQUENTIAL apply pass needs for one domain.

    The parallel worker (:func:`_compute_domain`) does ALL the LLM IO + pure
    validation for a domain and returns one of these; the apply pass then
    records cost into the shared :class:`CostTracker`, accumulates telemetry,
    and stamps the UFs — all in input (sorted-domain) order so a concurrent
    run is byte-identical to the old sequential loop. The worker NEVER mutates
    shared state (tracker / telemetry / UFs).
    """

    domain: str | None
    ufs: list["UserFlow"]
    members_by_uf: dict[str, list["Flow"]]
    # Final post-retry-merge parsed refinements, or None when degraded.
    parsed: dict[str, dict] | None
    name_ok: dict[str, bool]
    # Token usage for the FIRST call (always) and the retry (when it fired).
    in_tok: int = 0
    out_tok: int = 0
    retry_fired: bool = False
    retry_in_tok: int = 0
    retry_out_tok: int = 0
    # Pre-computed (pure) cost of call #1, used for the per-domain cost-cap
    # degrade decision WITHOUT touching the shared tracker mid-flight.
    call1_cost: float = 0.0
    degraded_reason: str | None = None
    # Telemetry deltas the apply pass folds into the scan-level counters.
    names_invalid: int = 0
    names_recovered: int = 0
    names_fallback: int = 0


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


def _uf_product_strings(
    members: list["Flow"],
    product_strings: "ProductStringIndex | None",
) -> list[str]:
    """Anchor-first product strings for a UF's member flows. Anchors are
    the members' entry-point files; remaining member paths follow."""
    if product_strings is None:
        return []
    paths: list[str] = []
    anchors: list[str] = []
    for m in members:
        ep = getattr(m, "entry_point_file", None)
        if ep:
            anchors.append(ep)
            paths.append(ep)
        paths.extend(getattr(m, "paths", None) or [])
    return product_strings.bundle_for(
        paths, anchor_paths=anchors, cap=MAX_UI_LABELS,
    )


def _uf_payload(
    uf: "UserFlow", members: list["Flow"],
    product_strings: "ProductStringIndex | None" = None,
) -> dict[str, Any]:
    """Code-grounded context for one UF handed to the LLM.

    Only names / routes / frontend labels / product strings /
    tested-member count — the allowed code-grounded sources. No prose,
    no .ai/specs. ``product_strings`` (naming-evidence core, 2026-06)
    carries nav labels / page titles / i18n copy from the member flows'
    OWN files — the in-repo product vocabulary.
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
        "product_strings": _uf_product_strings(members, product_strings),
        "tested_member_count": len(tested),
        "default_ui_tier": _default_ui_tier(members),
    }


def _uf_evidence_bundle(
    uf: "UserFlow",
    members: list["Flow"],
    product_strings: "ProductStringIndex | None",
) -> EvidenceBundle:
    """Evidence a refined UF name may draw vocabulary from: member flow
    names + routes + frontend labels (global), member files (+ their
    product strings) per file."""
    b = EvidenceBundle()
    for m in members:
        b.add_global(getattr(m, "display_name", None) or m.name)
        b.add_global(getattr(m, "description", "") or "")
        for p in (getattr(m, "paths", None) or []):
            b.add_file(p)
            if product_strings is not None:
                for row in product_strings.strings_for_file(p):
                    b.add_file(p, row.text)
    b.add_global(*uf.routes)
    b.add_global(*_ui_surface(members))
    b.add_global(uf.resource or "", str(uf.domain or ""))
    return b


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
    llm_health: LlmHealth | None = None,
) -> tuple[str, int, int]:
    """One Haiku call. Returns ``(text, in_tokens, out_tokens)``.

    Consults the shared :class:`LlmHealth`: after the first auth-class
    failure anywhere in the scan the call is skipped (dead key).
    """
    if llm_health is not None and not llm_health.should_call():
        return "", 0, 0
    try:
        msg = client.messages.create(
            model=gateway_model(model),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal at scan-time
        if llm_health is not None and llm_health.record_failure(
            exc, stage="stage_6_7b_uf_refiner",
        ):
            logger.error(
                "uf_refiner: LLM authentication failed — skipping all "
                "remaining LLM calls this scan: %s", exc,
            )
        else:
            logger.warning("uf_refiner: Haiku call failed: %s", exc)
        return "", 0, 0
    if llm_health is not None:
        llm_health.record_success()
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
    name_ok: bool = True,
) -> bool:
    """Stamp one refinement onto a UF in place. Returns True if applied.

    Validates every LLM field; on a missing/invalid field, keeps the
    deterministic value for that field (partial refine still counts).
    AC list is bounded to the count of test-reached members (the LLM is
    asked for exactly that many, but we enforce it structurally).

    ``name_ok=False`` (anti-hallucination validator verdict after the
    one allowed retry) keeps the deterministic Stage-6.7 name and stamps
    ``name_confidence="low"``; the other fields still apply.
    """
    applied = False

    name = row.get("name")
    if isinstance(name, str) and name.strip():
        if name_ok:
            uf.name = name.strip()
            uf.name_confidence = "high"
            applied = True
        else:
            uf.name_confidence = "low"

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


# ── Per-domain compute (parallel-safe; NO shared mutation) ──────────────────


def _compute_domain(
    domain: str | None,
    ufs: list["UserFlow"],
    *,
    client: Any,
    model: str,
    flows_by_key: dict[str, "Flow"],
    product_strings: "ProductStringIndex | None",
    llm_health: LlmHealth | None,
) -> _DomainResult:
    """Do ALL the LLM IO + pure validation for one domain.

    Returns a :class:`_DomainResult`. Touches NO shared state: cost is
    computed (not recorded), telemetry is returned as deltas, UFs are NOT
    mutated. Mirrors exactly what the old sequential per-domain loop body
    did up to (but not including) the apply step — so the apply pass, run
    sequentially in input order, reproduces the byte-identical result.
    """
    members_by_uf = {
        uf.id: _member_flows_for(uf, flows_by_key) for uf in ufs
    }
    payloads = [
        _uf_payload(uf, members_by_uf[uf.id], product_strings)
        for uf in ufs
    ]
    user_prompt = _build_user_prompt(domain, payloads)

    text, in_tok, out_tok = _call_haiku(
        client,
        model=model,
        system=_SYSTEM_PROMPT,
        user=user_prompt,
        max_tokens=DEFAULT_MAX_TOKENS,
        llm_health=llm_health,
    )

    # Cost of call #1 — derived from the pricing table (pure), identical to
    # what ``tracker.record(...).cost_usd`` will compute in the apply pass.
    call1_cost = (
        estimate_call_cost(model, in_tok, out_tok) if (in_tok or out_tok) else 0.0
    )

    result = _DomainResult(
        domain=domain,
        ufs=ufs,
        members_by_uf=members_by_uf,
        parsed=None,
        name_ok={},
        in_tok=in_tok,
        out_tok=out_tok,
        call1_cost=call1_cost,
    )

    if call1_cost > COST_CAP_USD_PER_DOMAIN:
        result.degraded_reason = f"cost_cap ${call1_cost:.4f}"
        return result

    parsed = _parse_refinement(text)
    if parsed is None:
        result.degraded_reason = "json_parse_failed"
        return result

    # ── Anti-hallucination name validation (naming review №2) ──
    # Every content token of a refined name must be evidenced in the UF's
    # bundle. ONE retry per domain with an explicit prohibition; second
    # failure keeps the deterministic Stage-6.7 name + name_confidence="low".
    bundles = {
        uf.id: _uf_evidence_bundle(
            uf, members_by_uf[uf.id], product_strings,
        )
        for uf in ufs
    }
    name_ok: dict[str, bool] = {}
    violations: dict[str, list[str]] = {}
    failing_ids: list[str] = []
    for uf in ufs:
        row = parsed.get(uf.id)
        nm = row.get("name") if row else None
        if not (isinstance(nm, str) and nm.strip()):
            continue
        verdict = validate_name(nm, bundles[uf.id])
        name_ok[uf.id] = verdict.ok
        if not verdict.ok:
            violations[nm] = verdict.all_violations
            failing_ids.append(uf.id)
    if violations:
        result.names_invalid = len(failing_ids)
        if llm_health is None or llm_health.should_call():
            result.retry_fired = True
            retry_text, r_in, r_out = _call_haiku(
                client,
                model=model,
                system=_SYSTEM_PROMPT,
                user=user_prompt + "\n" + retry_prohibition(violations),
                max_tokens=DEFAULT_MAX_TOKENS,
                llm_health=llm_health,
            )
            result.retry_in_tok = r_in
            result.retry_out_tok = r_out
            retry_parsed = _parse_refinement(retry_text) or {}
            for uf_id in failing_ids:
                row2 = retry_parsed.get(uf_id)
                nm2 = row2.get("name") if row2 else None
                if isinstance(nm2, str) and nm2.strip() and validate_name(
                    nm2, bundles[uf_id],
                ).ok:
                    # Adopt the grounded rename into the first response's row;
                    # other fields keep the first (already-parsed) values.
                    parsed[uf_id] = {**parsed.get(uf_id, {}), "name": nm2}
                    name_ok[uf_id] = True
                    result.names_recovered += 1
        result.names_fallback = sum(
            1 for uf_id in failing_ids if not name_ok.get(uf_id, True)
        )

    result.parsed = parsed
    result.name_ok = name_ok
    return result


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
    llm_health: LlmHealth | None = None,
    product_strings: "ProductStringIndex | None" = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
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
        "uf_names_invalid": 0,
        "uf_names_recovered_on_retry": 0,
        "uf_names_fallback": 0,
        "validator_retries": 0,
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

    # Domains to actually refine, in the canonical sorted order. Allowlist
    # skips (incremental --since path) are accounted here and never computed.
    # ``sorted(... key=str(domain))`` is the SAME deterministic order the old
    # sequential loop walked — preserving it is what makes the parallel run
    # byte-identical (cost-record order, telemetry accumulation, UF mutation
    # order all follow this list, NOT thread-completion order).
    domains_sorted = sorted(by_domain.items(), key=lambda kv: str(kv[0]))
    to_compute: list[tuple[str | None, list["UserFlow"]]] = []
    for domain, ufs in domains_sorted:
        # Incremental reuse: a domain NOT in the allowlist had every UF
        # matched to a refined base twin upstream — its members are
        # unchanged, so re-calling Haiku would just reproduce the base
        # presentation. Skip the call; keep the already-applied fields.
        if domain_allowlist is not None and domain not in domain_allowlist:
            domains_reused += 1
            continue
        to_compute.append((domain, ufs))

    # ── Parallel compute (LLM IO) — collected by INPUT index ───────────
    # Each domain is independent; its LLM call(s) + pure validation run in a
    # bounded thread pool. The worker mutates NOTHING shared. Results land in
    # ``computed[idx]`` keyed by the domain's position in ``to_compute``.
    computed: dict[int, _DomainResult] = {}
    if to_compute:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            future_to_idx = {
                pool.submit(
                    _compute_domain, domain, ufs,
                    client=client, model=model, flows_by_key=flows_by_key,
                    product_strings=product_strings, llm_health=llm_health,
                ): idx
                for idx, (domain, ufs) in enumerate(to_compute)
            }
            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                domain, ufs = to_compute[idx]
                try:
                    computed[idx] = fut.result()
                except Exception as exc:  # noqa: BLE001 — degrade as json_parse_failed
                    logger.warning(
                        "uf_refiner: domain=%r raised: %s", domain, exc,
                    )
                    computed[idx] = _DomainResult(
                        domain=domain, ufs=ufs,
                        members_by_uf={
                            uf.id: _member_flows_for(uf, flows_by_key)
                            for uf in ufs
                        },
                        parsed=None, name_ok={},
                        degraded_reason="worker_error",
                    )

    # ── Sequential apply (deterministic, sorted-domain order) ──────────
    # Record cost, accumulate telemetry, and mutate the UFs strictly in
    # ``to_compute`` order so the result matches the old sequential loop.
    for idx, (domain, ufs) in enumerate(to_compute):
        res = computed[idx]
        members_by_uf = res.members_by_uf

        # Cost #1 (recorded in input order → tracker.records order preserved).
        if res.in_tok or res.out_tok:
            tracker.record(
                provider="anthropic",
                model=model,
                input_tokens=res.in_tok,
                output_tokens=res.out_tok,
                label="stage-6.7b-uf-refiner",
            )
            total_cost += res.call1_cost

        if res.degraded_reason is not None or res.parsed is None:
            telemetry["domains_degraded"] += 1
            # Graceful degrade: deterministic ui_tier so field isn't null.
            for uf in ufs:
                if uf.ui_tier is None:
                    uf.ui_tier = _default_ui_tier(members_by_uf[uf.id])
            if log is not None:
                log.warn(
                    f"uf_refiner: domain={domain!r} degraded "
                    f"({res.degraded_reason}); kept deterministic names",
                )
            continue

        # Name-validation telemetry + retry cost (retry already happened in
        # the worker; we only record its cost + counters here, in order).
        if res.names_invalid:
            telemetry["uf_names_invalid"] += res.names_invalid
            if res.retry_fired:
                telemetry["validator_retries"] += 1
                if res.retry_in_tok or res.retry_out_tok:
                    tracker.record(
                        provider="anthropic",
                        model=model,
                        input_tokens=res.retry_in_tok,
                        output_tokens=res.retry_out_tok,
                        label="stage-6.7b-uf-refiner-name-retry",
                    )
                    total_cost += estimate_call_cost(
                        model, res.retry_in_tok, res.retry_out_tok,
                    )
                telemetry["uf_names_recovered_on_retry"] += res.names_recovered
            telemetry["uf_names_fallback"] += res.names_fallback

        parsed = res.parsed
        name_ok = res.name_ok
        refined_here = 0
        for uf in ufs:
            row = parsed.get(uf.id)
            members = members_by_uf[uf.id]
            if row and _apply_refinement(
                uf, row, members, name_ok=name_ok.get(uf.id, True),
            ):
                uf.refined = True
                refined_here += 1
            elif uf.ui_tier is None:
                uf.ui_tier = _default_ui_tier(members)
        telemetry["domains_refined"] += 1
        telemetry["uf_refined"] += refined_here

    telemetry["cost_usd"] = round(total_cost, 6)
    telemetry["domains_reused"] = domains_reused
    # Degraded-scan stamp (naming review №6): a dead key mid-scan means
    # names were not (or only partially) LLM-validated this run.
    if llm_health is not None and llm_health.auth_failed:
        for uf in user_flows:
            uf.name_confidence = "low"
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
