"""Stage 6.7c — Mega-User-Flow semantic split (additive LLM).

Stage 6.7's deterministic ``(domain, resource, intent)`` clusterer, plus its
``_merge_singleton_noise`` / ``_merge_same_name_clusters`` passes, can fold
many genuinely-DISTINCT journeys into a single mega-UF when a domain is
greedy and the ``intent='other'`` catch-all pools unrelated verbs. Measured
on cal.com: one ``availability`` UF carried 181 member flows spanning 33
distinct journey names (reschedule-booking, onboard-users, verify-phone …).
No deterministic merge tweak separates these without trading wins for losses
on other repos (the merge↔split tension is repo-specific) — so the split is
done SEMANTICALLY here, by the LLM, on ONLY the mega-mixed UFs.

What it does:
  * **Gate (deterministic, cheap)** — select UFs whose membership both
    exceeds ``MEGA_MIN_MEMBERS`` AND spans more than ``MEGA_MIN_DISTINCT_NAMES``
    distinct journey names. Everything else is left byte-identical.
  * **One LLM call per mega-UF** — partition its distinct member flow names
    into 2-8 coherent user journeys (Sonnet default — only a handful of calls
    per repo, so the latency/cost is bounded and the burst-timeout that bites
    Stage 3 does not apply here).
  * **Apply (recall-safe)** — replace the mega-UF with one sub-UF per journey,
    re-assigning ``member_flow_ids`` and re-stamping ``Flow.user_flow_id``.
    Members the model does not place fall into a residual sub-UF — **no flow
    is ever dropped**.

Runs BETWEEN Stage 6.7 (rollup) and Stage 6.7b (presentation refiner): 6.7b
then names/enriches the freshly-split UFs exactly as it would any other.

Graceful degrade: no client / API error / bad JSON / budget → keep the mega-UF
untouched (``refined`` semantics unchanged). Validated against the curated
``eval/uf-golden`` answer keys via ``eval/uf_score.py``: cal.com F1 64→74.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable, Literal

from faultline.llm.cost import CostTracker, deterministic_params
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.llm.model_gateway import resolve_model as gateway_model

if TYPE_CHECKING:
    from faultline.models.types import Flow, UserFlow
    from faultline.pipeline_v2.run_logger import StageLogger

logger = logging.getLogger(__name__)

# Sonnet by default: the split fires on only a HANDFUL of UFs per repo, so the
# higher per-call cost/latency is bounded, and Sonnet partitions a touch
# cleaner than Haiku (+1.5pp precision measured on cal.com). Override via the
# ``model`` kwarg for a cheaper Haiku run.
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 1100

# Mega-UF gate (structural, scale-invariant — see rule-no-magic-tuning). A UF
# is a single journey when its members share a journey identity. >20 members
# AND >5 DISTINCT journey names means the cluster pooled many unrelated
# journeys — exactly the over-merge signature. A large-but-single-journey UF
# (e.g. 42× reschedule-booking across features) has 1 distinct name and is
# never split.
MEGA_MIN_MEMBERS = 20
MEGA_MIN_DISTINCT_NAMES = 5
# Prompt bound + journey bound (presentation only; never changes which members
# exist, only how they are grouped).
MAX_NAMES_IN_PROMPT = 40
MAX_JOURNEYS = 8

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM_PROMPT = (
    "You group software user-flow names into coherent end-user journeys. "
    "You are given a product domain and a list of flow names that were "
    "clustered together but look like SEVERAL distinct user journeys. "
    "Partition them into 2-8 journeys. A journey is one thing a user sets "
    "out to do (e.g. 'Reschedule a booking', 'Connect a calendar'). "
    'Return ONLY JSON: {"journeys":[{"name":"<Verb Noun, <=5 words>",'
    '"members":["<exact flow name>", ...]}]}. Every input name must appear '
    "in exactly one journey. Use the flow names verbatim."
)


def _flow_key(flow: "Flow") -> str:
    """Stable member identifier — mirrors stage_6_7 (uuid when present)."""
    return getattr(flow, "uuid", None) or flow.name


def _default_client_factory() -> Any | None:  # pragma: no cover - IO
    try:
        from anthropic import Anthropic
    except Exception:  # noqa: BLE001
        return None
    try:
        return Anthropic()
    except Exception:  # noqa: BLE001
        return None


def _member_names(uf: "UserFlow", flow_by_key: dict[str, "Flow"]) -> list[str]:
    """Distinct member journey names, order-preserving."""
    seen: dict[str, None] = {}
    for mid in uf.member_flow_ids:
        f = flow_by_key.get(mid)
        if f is not None:
            seen.setdefault(f.name, None)
    return list(seen.keys())


def _is_mega(uf: "UserFlow", flow_by_key: dict[str, "Flow"]) -> bool:
    if len(uf.member_flow_ids) <= MEGA_MIN_MEMBERS:
        return False
    return len(_member_names(uf, flow_by_key)) > MEGA_MIN_DISTINCT_NAMES


def _call_llm(
    client: Any, model: str, user: str, cost_tracker: CostTracker,
    llm_health: LlmHealth | None = None,
) -> list[dict] | None:
    """One partition call. Returns the journeys list or None on any failure.

    Consults the shared :class:`LlmHealth`: after the first auth-class
    failure anywhere in the scan the call is skipped (dead key).
    """
    if llm_health is not None and not llm_health.should_call():
        return None
    try:
        msg = client.messages.create(
            model=gateway_model(model),
            max_tokens=DEFAULT_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
            **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal at scan-time (incl. budget)
        if llm_health is not None and llm_health.record_failure(
            exc, stage="stage_6_7c_uf_splitter",
        ):
            logger.error(
                "uf_splitter: LLM authentication failed — skipping all "
                "remaining LLM calls this scan: %s", exc,
            )
        else:
            logger.warning("uf_splitter: LLM call failed: %s", exc)
        return None
    if llm_health is not None:
        llm_health.record_success()
    in_tok = int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0)
    out_tok = int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)
    try:
        cost_tracker.record(model=model, input_tokens=in_tok, output_tokens=out_tok)
    except Exception:  # noqa: BLE001 — budget cap; the call already happened
        pass
    text = "\n".join(
        t for block in getattr(msg, "content", []) if (t := getattr(block, "text", None))
    ).strip()
    if not text:
        return None
    if not text.startswith("{"):
        m = _JSON_OBJ_RE.search(text)
        if not m:
            return None
        text = m.group(0)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    journeys = data.get("journeys") if isinstance(data, dict) else None
    if not isinstance(journeys, list) or not journeys:
        return None
    return journeys


def _split_one(
    uf: "UserFlow",
    journeys: list[dict],
    flow_by_key: dict[str, "Flow"],
) -> list["UserFlow"]:
    """Build sub-UFs from an LLM partition. Recall-safe: unplaced members go to
    a residual sub-UF so every member keeps a UF id. Returns [] if the model
    produced nothing usable (caller keeps the original mega-UF)."""
    from faultline.models.types import UserFlow

    # name -> member ids within THIS uf (a name can map to >1 member id when
    # cross-feature flows share a journey name — all of them move together).
    ids_by_name: dict[str, list[str]] = defaultdict(list)
    for mid in uf.member_flow_ids:
        f = flow_by_key.get(mid)
        if f is not None:
            ids_by_name[f.name].append(mid)

    subs: list["UserFlow"] = []
    placed: set[str] = set()
    for i, j in enumerate(journeys[:MAX_JOURNEYS]):
        name = (j.get("name") or "").strip() if isinstance(j, dict) else ""
        member_names = j.get("members") if isinstance(j, dict) else None
        if not name or not isinstance(member_names, list):
            continue
        ids: list[str] = []
        for nm in member_names:
            for mid in ids_by_name.get(nm, []):
                if mid not in placed:
                    ids.append(mid)
                    placed.add(mid)
        if not ids:
            continue
        subs.append(
            # Naming-evidence core (2026-06): an LLM-synthesized journey
            # name is not yet evidence-validated — marked low-confidence
            # until Stage 6.7b's validator confirms (it lifts to "high"
            # only when the validated name is applied).
            _make_sub(uf, f"{uf.id}-{len(subs) + 1}", name, ids,
                      name_confidence="low")
        )

    if not subs:
        return []

    residual = [m for m in uf.member_flow_ids if m not in placed]
    if residual:
        subs.append(_make_sub(
            uf, f"{uf.id}-{len(subs) + 1}", uf.name, residual,
            name_confidence=uf.name_confidence,
        ))
    return subs


def _make_sub(
    parent: "UserFlow", sub_id: str, name: str, member_ids: list[str],
    name_confidence: Literal["high", "low"] = "high",
) -> "UserFlow":
    """A sub-UF inherits the parent's domain/intent/resource/pf-link; 6.7b
    refines name/description/ui_tier/acceptance downstream."""
    from faultline.models.types import UserFlow

    return UserFlow(
        id=sub_id,
        name=name,
        description=None,
        domain=parent.domain,
        product_feature_id=parent.product_feature_id,
        intent=parent.intent,
        resource=parent.resource,
        member_flow_ids=list(member_ids),
        member_count=len(member_ids),
        routes=list(parent.routes),
        cross_links=list(parent.cross_links),
        ac_draft_count=0,
        acceptance=[],
        coverage_pct=None,
        ui_tier=None,
        refined=False,
        name_confidence=name_confidence,
    )


def split_mega_user_flows(
    user_flows: list["UserFlow"],
    flows: list["Flow"],
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    cost_tracker: CostTracker | None = None,
    log: "StageLogger | None" = None,
    llm_health: LlmHealth | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> tuple[list["UserFlow"], dict[str, Any]]:
    """Split mega-mixed UFs into per-journey sub-UFs via one LLM call each.

    Mutates ``Flow.user_flow_id`` in place for moved members and returns the
    NEW user-flow list (non-mega UFs unchanged, mega UFs replaced by their
    sub-UFs). Always returns; never raises for IO/budget failures.
    """
    tracker = cost_tracker or CostTracker(max_cost=None)
    flow_by_key = {_flow_key(f): f for f in flows}
    mega = [uf for uf in user_flows if _is_mega(uf, flow_by_key)]
    telemetry: dict[str, Any] = {
        "enabled": True,
        "model": model,
        "mega_detected": len(mega),
        "mega_split": 0,
        "sub_ufs_created": 0,
        "members_moved": 0,
        "cost_usd": 0.0,
        "fallback_reason": None,
    }
    if not mega:
        return user_flows, telemetry

    if client is None:
        client = _client_factory()
    if client is None:
        telemetry["enabled"] = False
        telemetry["fallback_reason"] = "no_anthropic_client"
        return user_flows, telemetry

    cost_before = tracker.total_cost_usd
    flow_by_key_for_stamp = flow_by_key
    out: list["UserFlow"] = []
    mega_ids = {uf.id for uf in mega}
    split_results: dict[str, list["UserFlow"]] = {}

    for uf in mega:
        names = _member_names(uf, flow_by_key)
        prompt = (
            f"domain: {uf.domain}\nflow names:\n"
            + "\n".join(f"- {n}" for n in names[:MAX_NAMES_IN_PROMPT])
        )
        journeys = _call_llm(client, model, prompt, tracker, llm_health)
        subs = _split_one(uf, journeys, flow_by_key) if journeys else []
        if subs:
            split_results[uf.id] = subs
            telemetry["mega_split"] += 1
            telemetry["sub_ufs_created"] += len(subs)
            for sub in subs:
                telemetry["members_moved"] += sub.member_count
                for mid in sub.member_flow_ids:
                    f = flow_by_key_for_stamp.get(mid)
                    if f is not None:
                        f.user_flow_id = sub.id
            if log is not None:
                log.emit(uf.name, f"split {uf.member_count} members → {len(subs)} journeys")

    for uf in user_flows:
        if uf.id in mega_ids and uf.id in split_results:
            out.extend(split_results[uf.id])
        else:
            out.append(uf)

    telemetry["cost_usd"] = round(tracker.total_cost_usd - cost_before, 6)
    if log is not None:
        log.info(
            f"uf_splitter: {telemetry['mega_split']}/{telemetry['mega_detected']} "
            f"mega-UFs split → {telemetry['sub_ufs_created']} sub-UFs "
            f"(cost ${telemetry['cost_usd']:.4f})"
        )
    return out, telemetry


__all__ = ["split_mega_user_flows"]
