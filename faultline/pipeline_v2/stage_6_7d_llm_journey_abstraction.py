"""Stage 6.7d — LLM product/journey abstraction (opt-in, default OFF).

Crosses the code-grain → product-grain gap that the deterministic stages
structurally cannot (see faultlines-app memory
``finding-llm-abstraction-p0-confirmed-2026-06-30`` + ``…codegrain-ceiling…``).

The deterministic pipeline emits CODE-grain artifacts: Stage 6.7 rolls flows
into one ``user_flow`` PER CRUD operation (1.5-4.4x over-produced vs a curated
journey golden), and Stage 6.5's workspace clusterer RE-LUMPS dev features into
coarse ``product_features``. When ENABLED, this stage REWRITES both arrays at
product/journey grain via two Haiku calls over a CODE-grounded digest:

  Call 1 (abstraction) — coarsen redundant CRUD variants of the SAME resource
    into one journey, PRESERVE distinct capabilities, and SURFACE cross-cutting
    journeys (auth, integrations, compound, read/view) the per-resource walk
    misses. Emits the new ``user_flows`` (name/resource/journey link, + which
    OLD UFs each subsumes, so members are inherited) and the new
    ``product_features`` (capability names + descriptions).

  Call 2 (re-attribution) — map every developer feature → one new product
    capability, so each rewritten ``product_feature`` AGGREGATES its members'
    files/metrics (paths union, commit sums, averaged health) via
    :func:`nav_taxonomy.aggregate_product_feature`. Nothing is lost: dev
    features the model omits fall into a ``Shared Platform`` residual.

P0 (Sonnet + Haiku, $0 subscription-proxy) lift, UF-F1 base → strong-Haiku:
fastapi 41→73, documenso 44→73, plane 56→76, ollama 49→71, axios 31→68 — every
repo up, no regressions; PF improved on the weak repos (plane 55/50→88/66).

Hard rules honoured
===================
  * **No README / no ``.ai/specs``** — inputs are dev-feature names + dirs,
    router routes, and the deterministic UF/PF names only. No ``.md`` prose.
  * **Output-layer only** — REWRITES ``user_flows[]`` / ``product_features[]``;
    NEVER mutates the central ``flows[]`` graph or any metric scalar (the
    proven "surface via the output layer" lesson, UF-coverage 2026-06-14).
  * **Additive / opt-in** — brand-new prompt; no existing stage prompt touched.
    Default OFF (``FAULTLINE_STAGE_6_7D_LLM_ABSTRACTION=1`` to enable). When
    disabled OR on any LLM failure the inputs pass through byte-identical.
  * **No magic numbers** — the model decides grain from evidence; the only
    constants are list-length caps that bound prompt size.

Wiring: ``phase_finalize`` calls :func:`run_journey_abstraction` after Stage
6.7b (user_flows final) and before Stage 6.95 (history scores the rewrite).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any, Callable

from faultline.llm.cost import CostTracker, deterministic_params, estimate_call_cost
from faultline.llm.model_gateway import resolve_model as gateway_model
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.nav_taxonomy import aggregate_product_feature

if TYPE_CHECKING:
    from faultline.models.types import Feature, Flow, UserFlow
    from faultline.pipeline_v2.run_logger import StageLogger

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
ABSTRACTION_MAX_TOKENS = 8000
REATTRIB_MAX_TOKENS = 16000
COST_CAP_USD = 0.50            # whole-stage guard against a runaway response
MAX_DEV_FEATURES_DIGEST = 200  # abstraction digest cap (re-attrib sees all)
MAX_ROUTES_DIGEST = 160

ENV_FLAG = "FAULTLINE_STAGE_6_7D_LLM_ABSTRACTION"

# verb → intent class (subset of Stage 6.7's fixed table; scale-invariant).
_INTENT = {
    "create": "author", "add": "author", "new": "author", "author": "author",
    "build": "author", "configure": "author", "set": "author", "define": "author",
    "update": "author", "edit": "author", "manage": "manage", "organize": "manage",
    "list": "browse", "view": "browse", "browse": "browse", "search": "browse",
    "filter": "browse", "inspect": "browse", "read": "browse", "preview": "browse",
    "sign": "execute", "authenticate": "execute", "connect": "execute",
    "send": "execute", "run": "execute", "generate": "execute", "import": "bulk",
    "export": "export", "sync": "execute", "receive": "execute", "verify": "execute",
    "approve": "lifecycle", "reject": "lifecycle", "publish": "lifecycle",
    "delete": "lifecycle", "archive": "lifecycle",
}

_RESIDUAL_CAP = "Shared Platform"


# ── Anthropic client (IoC for tests) ────────────────────────────────────────

def _default_client_factory() -> Any | None:  # pragma: no cover - IO
    """Lazy Anthropic client. ``None`` when SDK / key absent → the stage
    degrades to the deterministic inputs without erroring."""
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


def is_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "0") != "0"


# ── System prompts ──────────────────────────────────────────────────────────

_ABSTRACTION_SYSTEM = """You are the product-abstraction layer of a code-intelligence engine.
A deterministic scanner parsed a repository into CODE-GRAIN artifacts: developer
features (one per code module/dir), user flows (one per CRUD op), HTTP routes.
Re-express them at PRODUCT / JOURNEY grain. Ground EVERY item in the supplied
evidence (dev-feature names, routes, current flows). NEVER invent. No README is
given and you must not assume one.

CRITICAL — COMPLETENESS FIRST. The most common failure is producing TOO FEW
user_flows by over-merging. Avoid that. Rules in priority order:
  1. Produce ONE user_flow for EACH distinct capability visible in
     `developer_features`, and EACH distinct route-group in `routes`. If there
     are ~30 distinct capabilities, expect ~25-35 user_flows. Do NOT collapse
     the repo into a handful of generic "Manage X" items.
  2. MERGE only redundant CRUD variants of the SAME resource into one journey
     (create+update+sync+browse accounts -> ONE "Manage accounts"). Different
     resources / capabilities stay SEPARATE.
  3. SURFACE cross-cutting journeys the per-resource CRUD walk misses, when a
     route/dev-feature implies them: auth (sign up, sign in, reset password,
     verify email, 2FA), integrations (connect provider, receive webhook),
     compound goals, read/view journeys.
  4. For a LIBRARY/framework (no UI) a user_flow = an API capability a developer
     USING it accomplishes (validate data, inject dependencies, secure
     endpoints, add middleware/CORS, handle uploads, run background tasks, serve
     websockets/SSE, generate OpenAPI, serve static files, render templates,
     test with a client) — enumerate each as its OWN flow.
  5. Each user_flow: a short verb phrase `name`, one lowercase `resource` (the
     primary noun), the `product_feature` it belongs to, and `from_flows` =
     the list of CURRENT user-flow ids it subsumes (for member inheritance).
     CRITICAL: `from_flows` is bookkeeping — it must NEVER cap your journey
     count. Enumerate the distinct capabilities FIRST (rule 1), THEN attach
     whichever current ids fit; `from_flows` may be empty for a capability the
     CRUD walk missed. Do NOT collapse your journeys down to roughly the number
     of current_user_flows — there are usually MORE distinct capabilities than
     the CRUD list shows.

product_features — CUSTOMER-CAPABILITY grain. SAME completeness-first rule as
user_flows: enumerate EVERY distinct customer-facing capability the evidence
supports. A mature product has MANY features (often 30-60 for a full app) —
billing, authentication, teams/orgs, API tokens, webhooks, notifications, each
integration, admin, settings, audit logs, import/export, etc. are SEPARATE
product_features. MERGE only true duplicates (two dev modules that are the same
capability); when unsure, KEEP them separate — under-merging is far safer than
over-merging (collapsing distinct capabilities destroys recall). Do NOT collapse
the product to a handful of broad buckets. Only DROP bare code-structure leaks
that are not customer capabilities ("lib","web","core","editor","utils",
"components"). Title Case names; product voice.

Return STRICT JSON only, no prose:
{"product_features":[{"name":"...","description":"..."}],
 "user_flows":[{"name":"...","resource":"...","product_feature":"...","from_flows":["UF-001", ...]}]}"""

_REATTRIB_SYSTEM = """You assign each developer feature (a code module) to exactly ONE product
capability from the given list, using the module name and its directory. Every
developer feature must map to exactly one capability. If a module is generic
shared infrastructure (ui kit, icons, generic lib/utils, build config, app
shell) that serves many capabilities, assign it to "Shared Platform".
Return STRICT JSON only: {"map": {"<dev feature name>": "<capability>", ...}}."""


# ── Evidence digest (README-FORBIDDEN) ──────────────────────────────────────

def _paths_of(f: "Feature") -> list[str]:
    raw = getattr(f, "member_files", None) or getattr(f, "paths", None) or []
    out: list[str] = []
    for x in raw:
        if isinstance(x, str):
            out.append(x)
        else:
            p = getattr(x, "path", None)
            if p:
                out.append(p)
    return out


def _top_dirs(paths: list[str], k: int = 2) -> list[str]:
    c: Counter[str] = Counter()
    for p in paths:
        parts = p.split("/")
        c["/".join(parts[:2]) if len(parts) > 1 else parts[0]] += 1
    return [d for d, _ in c.most_common(k)]


def _short(s: str | None, n: int) -> str:
    return (s or "").strip().replace("\n", " ")[:n]


def _build_digest(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    user_flows: list["UserFlow"],
    routes_index: list[dict[str, Any]],
) -> dict[str, Any]:
    devf = sorted(developer_features, key=lambda f: -(f.total_commits or 0))
    dev_lines = [
        {"name": f.display_name or f.name, "where": _top_dirs(_paths_of(f)),
         "what": _short(f.description, 90)}
        for f in devf[:MAX_DEV_FEATURES_DIGEST]
    ]
    pf_lines = [
        {"name": p.display_name or p.name, "what": _short(p.description, 110)}
        for p in product_features
    ]
    uf_lines = [
        {"id": u.id, "name": u.name, "resource": u.resource,
         "domain": u.domain, "intent": u.intent}
        for u in user_flows
    ]
    seen: set[tuple] = set()
    routes: list[dict[str, Any]] = []
    for r in routes_index:
        key = (r.get("pattern"), r.get("method"))
        if key in seen:
            continue
        seen.add(key)
        routes.append({"p": r.get("pattern"), "m": r.get("method"), "t": r.get("trigger")})
        if len(routes) >= MAX_ROUTES_DIGEST:
            break
    return {
        "n_dev_features": len(developer_features),
        "developer_features": dev_lines,
        "current_product_features": pf_lines,
        "current_user_flows": uf_lines,
        "routes": routes,
    }


# ── LLM call (mirrors stage_6_7b._call_haiku) ───────────────────────────────

def _call_haiku(
    client: Any, *, model: str, system: str, user: str, max_tokens: int,
    llm_health: LlmHealth | None = None,
) -> tuple[str, int, int]:
    if llm_health is not None and not llm_health.should_call():
        return "", 0, 0
    try:
        msg = client.messages.create(
            model=gateway_model(model), max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}], **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal at scan-time
        if llm_health is not None and llm_health.record_failure(
            exc, stage="stage_6_7d_llm_journey_abstraction",
        ):
            logger.error("stage_6_7d: LLM auth failed — skipping remaining calls: %s", exc)
        else:
            logger.warning("stage_6_7d: Haiku call failed: %s", exc)
        return "", 0, 0
    if llm_health is not None:
        llm_health.record_success()
    text = ""
    try:
        text = "\n".join(t for block in msg.content if (t := getattr(block, "text", None)))
    except Exception:  # noqa: BLE001
        text = ""
    in_tok = int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0)
    out_tok = int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)
    return text, in_tok, out_tok


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_balanced(text: str) -> str | None:
    """Return the FIRST brace-balanced ``{...}`` object in ``text``, honouring
    string literals + escapes. Robust to trailing prose the model may append
    after the JSON (a greedy ``{.*}`` would swallow it and fail to parse)."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cand = _extract_balanced(text)
    if cand is None:
        m = _JSON_OBJ_RE.search(text)
        cand = m.group(0) if m else None
    if cand is None:
        return None
    try:
        obj = json.loads(cand)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


# ── Reconstruction ──────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    s = re.sub(r"[\s_/]+", "-", (name or "").strip().lower())
    return re.sub(r"-+", "-", s).strip("-")


def _intent_for(name: str) -> str:
    for tok in re.findall(r"[a-z]+", (name or "").lower()):
        if tok in _INTENT:
            return _INTENT[tok]
    return "other"


def _build_user_flows(
    uf_specs: list[dict[str, Any]], old_ufs: list["UserFlow"],
) -> list["UserFlow"]:
    from faultline.models.types import UserFlow

    old_by_id = {u.id: u for u in old_ufs}
    out: list[UserFlow] = []
    for i, spec in enumerate(uf_specs, start=1):
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        members: list[str] = []
        routes: list[str] = []
        seen_m: set[str] = set()
        for ref in spec.get("from_flows") or []:
            src = old_by_id.get(ref)
            if not src:
                continue
            for mid in src.member_flow_ids:
                if mid not in seen_m:
                    seen_m.add(mid)
                    members.append(mid)
            routes.extend(src.routes or [])
        out.append(UserFlow(
            id=f"UF-{i:03d}",
            name=name.strip(),
            resource=str(spec.get("resource") or "").lower(),
            domain=None,
            product_feature_id=_slug(spec.get("product_feature") or "") or None,
            intent=_intent_for(name),
            member_flow_ids=members,
            member_count=len(members),
            routes=sorted(set(routes)),
            refined=True,
            name_confidence="high",
        ))
    return out


def _build_product_features(
    pf_specs: list[dict[str, Any]],
    dev_map: dict[str, str],
    developer_features: list["Feature"],
) -> tuple[list["Feature"], dict[str, tuple[str, ...]], int]:
    """Aggregate dev features into the abstracted capabilities. Returns
    (product_features, dev_to_product_map, files_attributed)."""
    desc_by_cap = {
        (s.get("name") or "").strip(): _short(s.get("description"), 240)
        for s in pf_specs if (s.get("name") or "").strip()
    }
    dev_by_name = {(f.display_name or f.name): f for f in developer_features}

    # Group dev features by their mapped capability (residual for omitted).
    cap_to_devs: dict[str, list["Feature"]] = defaultdict(list)
    dev_to_product: dict[str, tuple[str, ...]] = {}
    for dev in developer_features:
        nm = dev.display_name or dev.name
        cap = dev_map.get(nm) or _RESIDUAL_CAP
        cap_to_devs[cap].append(dev)
        dev_to_product[dev.name] = (_slug(cap),)
    _ = dev_by_name  # reserved for future per-dev lookups

    out: list["Feature"] = []
    files_attributed = 0
    # Preserve the model's PF order first, then any extra caps (incl. residual).
    # Dedup by name — the LLM occasionally echoes a capability twice, which
    # would otherwise emit duplicate product_features rows.
    seen_caps: set[str] = set()
    ordered_caps: list[str] = []
    for s in pf_specs:
        cap = (s.get("name") or "").strip()
        if cap and cap not in seen_caps:
            seen_caps.add(cap)
            ordered_caps.append(cap)
    for cap in cap_to_devs:
        if cap not in seen_caps:
            seen_caps.add(cap)
            ordered_caps.append(cap)
    for cap in ordered_caps:
        contrib = cap_to_devs.get(cap)
        if not contrib:
            continue
        feat = aggregate_product_feature(
            name=_slug(cap), display_name=cap,
            description=desc_by_cap.get(cap, ""), contrib=contrib,
        )
        # aggregate_product_feature unions .paths only; carry the richer
        # member_files ledger too (the owned-files registry the dashboard
        # tree / coverage / blob metric read) — dedup by path, preserve order.
        seen_mf: set[str] = set()
        merged_mf: list[Any] = []
        for c in contrib:
            for mf in (getattr(c, "member_files", None) or []):
                p = getattr(mf, "path", None) if not isinstance(mf, dict) else mf.get("path")
                if p and p not in seen_mf:
                    seen_mf.add(p)
                    merged_mf.append(mf)
        if merged_mf:
            feat.member_files = merged_mf
        files_attributed += len(merged_mf) if merged_mf else len(feat.paths)
        out.append(feat)
    return out, dev_to_product, files_attributed


# ── Public entrypoint ───────────────────────────────────────────────────────

def run_journey_abstraction(
    user_flows: list["UserFlow"],
    product_features: list["Feature"],
    developer_features: list["Feature"],
    routes_index: list[dict[str, Any]],
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    cost_tracker: CostTracker | None = None,
    log: "StageLogger | None" = None,
    llm_health: LlmHealth | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> tuple[list["UserFlow"], list["Feature"], dict[str, tuple[str, ...]] | None, dict[str, Any]]:
    """Rewrite user_flows[] + product_features[] at journey/capability grain.

    Returns ``(user_flows, product_features, dev_to_product_map, telemetry)``.
    On ANY failure the ORIGINAL inputs are returned unchanged (degrade), and
    ``dev_to_product_map`` is ``None`` (caller keeps its existing mapping).
    """
    tele: dict[str, Any] = {
        "enabled": True, "applied": False, "degraded_reason": None,
        "uf_before": len(user_flows), "uf_after": len(user_flows),
        "pf_before": len(product_features), "pf_after": len(product_features),
        "files_before": 0, "files_after": 0, "cost_usd": 0.0, "llm_calls": 0,
    }
    files_before = sum(len(_paths_of(p)) for p in product_features)
    tele["files_before"] = files_before

    cli = client if client is not None else _client_factory()
    if cli is None:
        tele["degraded_reason"] = "no_client"
        return user_flows, product_features, None, tele
    if not developer_features:
        tele["degraded_reason"] = "no_dev_features"
        return user_flows, product_features, None, tele

    def _record(model_: str, in_tok: int, out_tok: int) -> float:
        cost = estimate_call_cost(model_, in_tok, out_tok) if (in_tok or out_tok) else 0.0
        if cost_tracker is not None and (in_tok or out_tok):
            try:
                cost_tracker.record(model=model_, input_tokens=in_tok,
                                    output_tokens=out_tok, label="stage_6_7d")
            except Exception:  # noqa: BLE001 — budget cap is enforced elsewhere
                pass
        return cost

    # ── Call 1 — abstraction ──────────────────────────────────────────
    digest = _build_digest(developer_features, product_features, user_flows, routes_index)
    user1 = ("Repository evidence (code-grounded, no README):\n```json\n"
             + json.dumps(digest, ensure_ascii=False) + "\n```\nEmit the JSON now.")
    text1, in1, out1 = _call_haiku(
        cli, model=model, system=_ABSTRACTION_SYSTEM, user=user1,
        max_tokens=ABSTRACTION_MAX_TOKENS, llm_health=llm_health)
    tele["llm_calls"] += 1
    cost1 = _record(model, in1, out1)
    tele["cost_usd"] = round(tele["cost_usd"] + cost1, 6)
    parsed1 = _parse_json(text1)
    if not parsed1:
        tele["degraded_reason"] = "abstraction_parse_failed"
        return user_flows, product_features, None, tele
    uf_specs = parsed1.get("user_flows") or []
    pf_specs = parsed1.get("product_features") or []
    if not uf_specs or not pf_specs:
        tele["degraded_reason"] = "abstraction_empty"
        return user_flows, product_features, None, tele
    if tele["cost_usd"] > COST_CAP_USD:
        tele["degraded_reason"] = f"cost_cap ${tele['cost_usd']:.4f}"
        return user_flows, product_features, None, tele

    # ── Call 2 — dev → capability re-attribution ──────────────────────
    caps = [s.get("name", "").strip() for s in pf_specs if s.get("name", "").strip()]
    caps_with_residual = caps + [_RESIDUAL_CAP]
    dev_items = [
        {"name": f.display_name or f.name, "where": _top_dirs(_paths_of(f)),
         "n_files": len(_paths_of(f))}
        for f in sorted(developer_features, key=lambda f: -len(_paths_of(f)))
    ]
    user2 = ("Product capabilities:\n" + json.dumps(caps_with_residual) +
             "\n\nDeveloper features (name, dir, file count):\n" +
             json.dumps(dev_items, ensure_ascii=False) +
             "\n\nReturn the full map now (every dev feature mapped).")
    text2, in2, out2 = _call_haiku(
        cli, model=model, system=_REATTRIB_SYSTEM, user=user2,
        max_tokens=REATTRIB_MAX_TOKENS, llm_health=llm_health)
    tele["llm_calls"] += 1
    cost2 = _record(model, in2, out2)
    tele["cost_usd"] = round(tele["cost_usd"] + cost2, 6)
    parsed2 = _parse_json(text2)
    dev_map_raw = (parsed2 or {}).get("map") or {}
    dev_map = {k: v for k, v in dev_map_raw.items() if isinstance(k, str) and isinstance(v, str)}
    # Re-attribution failed entirely (LLM error / bad JSON / health-blocked):
    # without it every dev feature would collapse into the Shared Platform
    # residual, emitting a degenerate single-blob product layer. Degrade fully
    # — return the ORIGINAL arrays unchanged (graceful-degrade invariant).
    if not dev_map:
        tele["degraded_reason"] = "reattrib_failed"
        return user_flows, product_features, None, tele

    # ── Reconstruct (conservation guaranteed — residual catches omits) ─
    new_pfs, dev_to_product, files_after = _build_product_features(
        pf_specs, dev_map, developer_features)
    new_ufs = _build_user_flows(uf_specs, user_flows)
    if not new_pfs or not new_ufs:
        tele["degraded_reason"] = "reconstruct_empty"
        return user_flows, product_features, None, tele

    tele.update({
        "applied": True, "uf_after": len(new_ufs), "pf_after": len(new_pfs),
        "files_after": files_after, "dev_mapped": len(dev_map),
        "dev_total": len(developer_features),
        "residual_devs": sum(1 for d in developer_features
                             if (d.display_name or d.name) not in dev_map),
    })
    if log is not None:
        log.info(
            "stage_6_7d: UF %d->%d, PF %d->%d, files %d->%d, dev_mapped %d/%d, $%.4f",
            tele["uf_before"], tele["uf_after"], tele["pf_before"], tele["pf_after"],
            files_before, files_after, tele["dev_mapped"], tele["dev_total"],
            tele["cost_usd"],
        )
    return new_ufs, new_pfs, dev_to_product, tele
