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

import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any, Callable, Literal

from faultline.cache.backend import CacheKind
from faultline.llm.cost import CostTracker, deterministic_params, estimate_call_cost
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.llm.model_gateway import resolve_model as gateway_model

if TYPE_CHECKING:
    from faultline.cache.backend import CacheBackend
    from faultline.models.types import Flow, UserFlow
    from faultline.pipeline_v2.run_logger import StageLogger

logger = logging.getLogger(__name__)

# ThreadPool size for the per-mega-UF partition LLM calls. The split fires on
# only a handful of mega-UFs per repo, but on the largest over-merged repos
# (cal.com) that handful is still enough that the SEQUENTIAL per-call latency
# (Sonnet, ~several s each) dominated this stage's wall-time. The calls are
# independent, so we fan them out over a bounded pool — execution-only, the
# assembled result is identical to the sequential run (see
# ``split_mega_user_flows`` for the ordering proof). Tunable via env so a
# deploy can change concurrency WITHOUT an engine release; bounded [1, 32] to
# respect the provider rate limit. Mirrors stage_3_flows' knob (shared default
# of 8); same env var name so one knob sizes both Stage 6 LLM fan-outs.
DEFAULT_MAX_WORKERS = max(
    1, min(32, int(os.environ.get("FAULTLINES_STAGE6_MAX_WORKERS", "8") or "8"))
)

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

# ── Content-hash LLM cache (deterministic short-circuit) ────────────────────
#
# Each per-mega-UF partition call is a pure function of its input: the system
# prompt + the user prompt (domain + the distinct member flow names) + the
# canonical model id. We cache the PARSED ``journeys[]`` array keyed on a
# sha256 of exactly those inputs, so a re-scan of an unchanged repo REPLAYS
# the identical partition ($0) through the SAME ``_split_one`` builder —
# byte-identical sub-UFs. Content-keyed (same input → same answer): a
# deterministic short-circuit, NOT per-repo memory — compliant with
# rule-cold-scan. Mirrors Stage 3's CacheKind.LLM_FLOWS cache. Default ON;
# opt out via ``FAULTLINE_STAGE_6_7C_CACHE=0``.
#
# STAGE_6_7C_CACHE_VERSION is the manual invalidation lever required by
# rule-cache-invalidation: bump it whenever the prompt template, the parse
# logic, or the cached-value shape changes in a way that must NOT serve a
# stale answer. (The system prompt is ALSO hashed into the key, but the
# version constant is the documented, explicit control surface.)
STAGE_6_7C_CACHE_VERSION = "v1"

_CACHE_ENV = "FAULTLINE_STAGE_6_7C_CACHE"


def _cache_enabled() -> bool:
    """Default ON — set ``FAULTLINE_STAGE_6_7C_CACHE=0`` to opt out."""
    return os.environ.get(_CACHE_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


# ── B77 — residual citability (default OFF) ────────────────────────────────
# Class: RESIDUAL-CITABILITY MASS-TRANSFER (forensics 2026-07-21, ledger
# §ФОРЕНЗИКА 502M). The 6.7c mega-split's recall-safe RESIDUAL sub-UF keeps
# the parent's (often journey-like) name, so downstream Call-1 cites it via
# ``from_flows`` and Pass-1 inherits the WHOLE bucket without any content
# gate — 'Create and run logic functions' inherited 778 ids from two cited
# buckets (UF-128-9 510/557 = 91.6% residual mass + UF-131-9 268/332), the
# same class ×3 in one run (502m/278m/216m). Under the flag:
#   Seg 1 — 6.7c stamps ``UserFlow.residual=True`` on the leftover bucket
#           (it structurally KNOWS the row is a remainder — :func:`_split_one`);
#           6.7d Pass-1 refuses wholesale inheritance from a residual row
#           (members stay in their bucket / remain available to the
#           token-gated grounding channels + backstop — no-orphan).
#   Seg 2 — 6.7d Pass-1 container-inherit passes a member ONLY on the same
#           ``& utok`` content-token overlap Pass-2a already requires.
#   Seg 3 — mint-side domain carve: a built UF whose members majority-vote
#           for >1 real PF home with no common majority is carved per home
#           (existing :func:`conservation.member_votes` mechanism).
#   Seg 4 — a ws-container PF is not a valid conservation resettle target.
# Default OFF; unset/=0 keeps every path byte-identical (kill-switch law).
RESIDUAL_CITABILITY_ENV = "FAULTLINE_RESIDUAL_CITABILITY"


def residual_citability_enabled() -> bool:
    """Default **OFF**. Unset / falsy keeps 6.7c/6.7d/conservation
    byte-identical to the flag-less engine (kill-switch law)."""
    return os.environ.get(RESIDUAL_CITABILITY_ENV, "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _split_cache_key(model: str, user: str) -> str:
    """Content-hash key for one mega-UF partition call.

    Components: cache version + canonical model id (pre-gateway) + the
    system prompt + the full user prompt (domain + member flow names —
    the exact structured input). Deliberately EXCLUDED: run_id,
    timestamps, UF ids, thread identity, or any other run-varying value.
    """
    payload = json.dumps(
        {
            "version": STAGE_6_7C_CACHE_VERSION,
            "model": model,
            "system": _SYSTEM_PROMPT,
            "user": user,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _cache_get_journeys(cache: "CacheBackend", key: str) -> list[dict] | None:
    """Read stored parsed journeys. ``None`` on miss, version mismatch,
    malformed entry, or ANY backend fault — a cache problem must never
    abort the stage (never-worse)."""
    try:
        stored = cache.get(CacheKind.LLM_UF_SPLIT.value, key)
    except Exception as exc:  # noqa: BLE001 — cache must never break a scan
        logger.warning("uf_splitter: cache get failed: %s", exc)
        return None
    if not isinstance(stored, dict) or stored.get("v") != STAGE_6_7C_CACHE_VERSION:
        return None
    journeys = stored.get("journeys")
    if not isinstance(journeys, list) or not journeys:
        # ``_call_llm`` never returns an empty list (it degrades to ``None``),
        # so an empty stored value is malformed → miss.
        return None
    # Returned VERBATIM (no per-entry filtering): ``_split_one`` slices then
    # type-guards each entry itself, so replay must hand it the exact list a
    # live parse produced.
    return journeys


def _cache_put_journeys(
    cache: "CacheBackend", key: str, journeys: list[dict],
) -> None:
    """Persist parsed journeys. Failures are logged + swallowed. Only
    SUCCESSFUL parses are ever stored (callers guarantee it) so a transient
    outage never poisons future reproducible replays."""
    try:
        cache.set(
            CacheKind.LLM_UF_SPLIT.value,
            key,
            {"v": STAGE_6_7C_CACHE_VERSION, "journeys": journeys},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("uf_splitter: cache set failed: %s", exc)


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
    client: Any, model: str, user: str,
    llm_health: LlmHealth | None = None,
) -> tuple[list[dict] | None, int, int]:
    """One partition call. Returns ``(journeys | None, in_tokens, out_tokens)``.

    Returns ``(None, 0, 0)`` on any failure (no client surface / API error /
    bad JSON). Does NOT record cost — the caller records into the shared
    :class:`CostTracker` from the deterministic apply phase so that, when the
    calls run in parallel, ``tracker.records`` is still appended in input
    order (byte-identical telemetry).

    Consults the shared :class:`LlmHealth`: after the first auth-class
    failure anywhere in the scan the call is skipped (dead key).
    """
    if llm_health is not None and not llm_health.should_call():
        return None, 0, 0
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
        return None, 0, 0
    if llm_health is not None:
        llm_health.record_success()
    in_tok = int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0)
    out_tok = int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)
    text = "\n".join(
        t for block in getattr(msg, "content", []) if (t := getattr(block, "text", None))
    ).strip()
    if not text:
        return None, in_tok, out_tok
    if not text.startswith("{"):
        m = _JSON_OBJ_RE.search(text)
        if not m:
            return None, in_tok, out_tok
        text = m.group(0)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, in_tok, out_tok
    journeys = data.get("journeys") if isinstance(data, dict) else None
    if not isinstance(journeys, list) or not journeys:
        return None, in_tok, out_tok
    return journeys, in_tok, out_tok


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
        sub = _make_sub(
            uf, f"{uf.id}-{len(subs) + 1}", uf.name, residual,
            name_confidence=uf.name_confidence,
        )
        if residual_citability_enabled():
            # B77 Seg 1 — the leftover bucket is a REMAINDER, not a journey:
            # mark it structurally so 6.7d Pass-1 refuses wholesale
            # ``from_flows`` inheritance (the residual-citability
            # mass-transfer class). Flag-gated: unset keeps the row (and its
            # dump) byte-identical.
            sub.residual = True
        subs.append(sub)
    return subs


def _make_sub(
    parent: "UserFlow", sub_id: str, name: str, member_ids: list[str],
    name_confidence: Literal["high", "medium", "low"] = "high",
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
    max_workers: int = DEFAULT_MAX_WORKERS,
    cache: "CacheBackend | None" = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> tuple[list["UserFlow"], dict[str, Any]]:
    """Split mega-mixed UFs into per-journey sub-UFs via one LLM call each.

    ``cache`` (content-hash short-circuit, CacheKind.LLM_UF_SPLIT): when
    supplied AND ``FAULTLINE_STAGE_6_7C_CACHE`` != 0 (default ON), a
    mega-UF whose partition input is unchanged replays its stored PARSED
    ``journeys[]`` at $0 through the SAME ``_split_one`` builder —
    byte-identical sub-UFs on an unchanged repo. ``None`` behaves exactly
    as pre-cache; any cache fault falls through to the live call
    (never-worse). Failures are never cached.

    Mutates ``Flow.user_flow_id`` in place for moved members and returns the
    NEW user-flow list (non-mega UFs unchanged, mega UFs replaced by their
    sub-UFs). Always returns; never raises for IO/budget failures.

    Concurrency (execution-only — output is order-independent)
    ---------------------------------------------------------
    The mega-UFs are independent, so each one's LLM partition call runs in a
    bounded :class:`ThreadPoolExecutor` (``max_workers``). ONLY the LLM IO +
    pure ``_split_one`` build run in parallel; every effect that touches
    shared/ordered state — recording cost into the ``CostTracker``,
    accumulating telemetry, stamping ``Flow.user_flow_id``, and building the
    output list — happens in a SEQUENTIAL second pass over ``mega`` (and then
    ``user_flows``) in input order. Results are collected into a dict keyed by
    the mega-UF's input index, so a concurrent run produces byte-identical
    ``user_flows[]`` / telemetry to the old sequential loop for the same
    per-UF LLM responses. A per-call exception degrades exactly as before
    (``subs=[]`` → mega-UF kept).
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
        "cache_hits": 0,
        "llm_calls": 0,
    }
    if not mega:
        return user_flows, telemetry

    if client is None:
        client = _client_factory()
    if client is None:
        telemetry["enabled"] = False
        telemetry["fallback_reason"] = "no_anthropic_client"
        return user_flows, telemetry

    # Env opt-out honoured regardless of what the caller threaded in.
    if cache is not None and not _cache_enabled():
        cache = None

    cost_before = tracker.total_cost_usd
    flow_by_key_for_stamp = flow_by_key
    out: list["UserFlow"] = []
    mega_ids = {uf.id for uf in mega}
    split_results: dict[str, list["UserFlow"]] = {}

    def _process(
        idx: int, uf: "UserFlow",
    ) -> tuple[list["UserFlow"], int, int, int, int]:
        """Parallel-safe worker: LLM call + pure sub-UF build for one mega-UF.

        Returns ``(subs, in_tokens, out_tokens, cache_hits, llm_calls)``. No
        shared mutation here — cost recording, telemetry and
        ``Flow.user_flow_id`` stamping are done by the sequential apply pass
        below in input order.
        """
        names = _member_names(uf, flow_by_key)
        prompt = (
            f"domain: {uf.domain}\nflow names:\n"
            + "\n".join(f"- {n}" for n in names[:MAX_NAMES_IN_PROMPT])
        )
        # ── Cache lookup (content-hash short-circuit) ──
        key: str | None = None
        journeys: list[dict] | None = None
        if cache is not None:
            key = _split_cache_key(model, prompt)
            journeys = _cache_get_journeys(cache, key)
        if journeys is not None:
            # HIT: no LLM call, no tokens, $0 — the parsed journeys feed the
            # SAME ``_split_one`` builder, so replay is byte-identical.
            in_tok = out_tok = 0
            hits, calls = 1, 0
        else:
            journeys, in_tok, out_tok = _call_llm(client, model, prompt, llm_health)
            hits, calls = 0, 1
            # MISS → persist the parsed journeys. ``None`` (call failed /
            # bad JSON) is NEVER cached.
            if journeys is not None and key is not None and cache is not None:
                _cache_put_journeys(cache, key, journeys)
        subs = _split_one(uf, journeys, flow_by_key) if journeys else []
        return subs, in_tok, out_tok, hits, calls

    # ── Parallel compute (LLM IO) — collected by INPUT index ───────────
    computed: dict[int, tuple[list["UserFlow"], int, int, int, int]] = {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        future_to_idx = {
            pool.submit(_process, idx, uf): idx for idx, uf in enumerate(mega)
        }
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                computed[idx] = fut.result()
            except Exception as exc:  # noqa: BLE001 — degrade exactly as sequential
                logger.warning(
                    "uf_splitter: mega-UF %r raised: %s", mega[idx].name, exc,
                )
                computed[idx] = ([], 0, 0, 0, 0)

    # ── Sequential apply (deterministic, input order) ──────────────────
    for idx, uf in enumerate(mega):
        subs, in_tok, out_tok, hits, calls = computed.get(idx, ([], 0, 0, 0, 0))
        telemetry["cache_hits"] += hits
        telemetry["llm_calls"] += calls
        if in_tok or out_tok:
            try:
                tracker.record(
                    model=model, input_tokens=in_tok, output_tokens=out_tok,
                    label="stage-6.7c-uf-splitter",
                )
            except Exception:  # noqa: BLE001 — budget cap; the call already happened
                pass
        if subs:
            split_results[uf.id] = subs
            telemetry["mega_split"] += 1
            telemetry["sub_ufs_created"] += len(subs)
            # B77 Seg 1 — residual-bucket telemetry. The key exists only in
            # an ARMED world (the marker itself is flag-gated), so unset
            # scan_meta stays byte-identical.
            n_res = sum(1 for s in subs if getattr(s, "residual", False))
            if n_res:
                telemetry["residual_marked"] = (
                    telemetry.get("residual_marked", 0) + n_res)
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


__all__ = [
    "RESIDUAL_CITABILITY_ENV",
    "STAGE_6_7C_CACHE_VERSION",
    "residual_citability_enabled",
    "split_mega_user_flows",
]
