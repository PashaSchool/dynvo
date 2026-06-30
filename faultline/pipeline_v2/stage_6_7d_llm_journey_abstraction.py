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

import hashlib
import json
import logging
import os
import re
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any, Callable

from faultline.cache.backend import CacheKind
from faultline.llm.cost import CostTracker, deterministic_params, estimate_call_cost
from faultline.llm.model_gateway import resolve_model as gateway_model
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.nav_taxonomy import aggregate_product_feature

if TYPE_CHECKING:
    from faultline.cache.backend import CacheBackend
    from faultline.models.types import Feature, Flow, UserFlow
    from faultline.pipeline_v2.anchor_extractors import ProductAnchor
    from faultline.pipeline_v2.run_logger import StageLogger

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
ABSTRACTION_MAX_TOKENS = 8000
REATTRIB_MAX_TOKENS = 16000
COST_CAP_USD = 0.50            # whole-stage guard against a runaway response
MAX_DEV_FEATURES_DIGEST = 200  # abstraction digest cap (re-attrib sees all)
MAX_ROUTES_DIGEST = 160
#: Authoritative-anchor list cap fed into the alignment prompt. A bound on
#: prompt size (NOT a tuned threshold) — anchors arrive pre-sorted by source
#: trust (analytics > nav > docs > i18n > test) so the slice keeps the
#: strongest product signal, not the alphabetical long tail.
MAX_ANCHOR_TEXTS_DIGEST = 160

#: Bumped whenever the prompt / tool schema / reconstruction changes in a way
#: that would make a previously-cached answer wrong. Part of the cache key, so
#: a bump transparently invalidates every stale entry.
ABSTRACTION_CACHE_VERSION = "p2-2"

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


# ── Alignment system prompt (anchors present — Phase 2 ALIGN mode) ───────────
# When deterministic product-capability ANCHORS are supplied, the model no
# longer free-generates. Its job is to ALIGN code evidence to the authoritative
# anchor list: use anchor text as the canonical name, group code under the
# anchor it serves, and emit a non-anchor item ONLY for a real capability the
# anchors clearly missed (flagged from_code_only). This bounds the output to
# the anchor set → kills over-production, blobs, and run-to-run drift.
_ALIGN_SYSTEM = """You are the product-abstraction layer of a code-intelligence engine.
A deterministic scanner parsed a repository into CODE-GRAIN artifacts: developer
features (one per code module/dir), user flows (one per CRUD op), HTTP routes.
SEPARATELY, a deterministic extractor mined an AUTHORITATIVE list of product
capabilities from code-grounded sources (i18n labels, navigation, analytics
events, test titles) — this is `product_capability_anchors`. No README exists.

Your job is ALIGNMENT, not invention. Map the code evidence onto the anchor
list. Rules in priority order:
  1. The anchor list is AUTHORITATIVE. Produce ONE product_feature per distinct
     product capability the anchors describe. Use the ANCHOR TEXT VERBATIM as
     the canonical `name`: copy the anchor's words EXACTLY — you may only adjust
     capitalisation to Title Case. Do NOT reword, paraphrase, translate, expand,
     abbreviate, or "improve" the wording. Drawing names verbatim from the fixed
     anchor list is REQUIRED — it keeps names stable across runs and faithful to
     the maintainer's own vocabulary. Consolidate near-duplicate anchors (the
     same capability worded twice) into one feature, keeping the clearest
     anchor's text verbatim.
  2. Produce user_flows that realise those capabilities. Each user_flow's
     `product_feature` MUST be one of the names you emitted in (1). When a
     user_flow realises a specific anchor capability, use THAT anchor's text
     VERBATIM (Title Case ok) as the flow `name` — do not paraphrase it. Only
     when no single anchor matches the flow, write a short verb phrase grounded
     in the code evidence.
  3. GROUP the code under the anchor it serves: set `from_flows` to the list of
     CURRENT user-flow ids that belong to each journey (member inheritance).
     `from_flows` may be empty for a capability whose CRUD flows were not
     detected — never inflate or cap your list to the current_user_flows count.
  4. Emit an item NOT covered by ANY anchor ONLY when the code evidence clearly
     shows a real customer capability the anchors missed. Flag every such item
     with `"from_code_only": true`. Anchor-aligned items omit the flag (false).
     Prefer aligning to an existing anchor over inventing a code-only item.
  5. Do NOT emit anchors that have NO supporting code evidence (an anchor with
     no related dev feature / route / flow is noise — drop it). Do NOT emit bare
     code-structure leaks ("lib","web","core","utils","components") as features.

Title Case names; product voice. Ground EVERY item in the supplied evidence.

Return STRICT JSON only, no prose:
{"product_features":[{"name":"...","description":"...","from_code_only":false}],
 "user_flows":[{"name":"...","resource":"...","product_feature":"...",
                "from_flows":["UF-001", ...],"from_code_only":false}]}"""


# ── Tool schemas for forced structured output ────────────────────────────────
# A forced tool-use call is more robust + deterministic than regex-extracting
# JSON from free text. The model MUST return through these schemas. When the
# SDK / endpoint does not support tools the caller falls back to text + regex.
_ABSTRACTION_TOOL: dict[str, Any] = {
    "name": "emit_product_abstraction",
    "description": "Return the aligned product_features and user_flows.",
    "input_schema": {
        "type": "object",
        "properties": {
            "product_features": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "from_code_only": {"type": "boolean"},
                    },
                    "required": ["name"],
                },
            },
            "user_flows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "resource": {"type": "string"},
                        "product_feature": {"type": "string"},
                        "from_flows": {"type": "array", "items": {"type": "string"}},
                        "from_code_only": {"type": "boolean"},
                    },
                    "required": ["name"],
                },
            },
        },
        "required": ["product_features", "user_flows"],
    },
}
_REATTRIB_TOOL: dict[str, Any] = {
    "name": "emit_dev_capability_map",
    "description": "Return the dev-feature → product-capability map.",
    "input_schema": {
        "type": "object",
        "properties": {
            "map": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "required": ["map"],
    },
}


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


def _canonical_anchor_texts(
    product_anchors: "list[ProductAnchor] | None",
) -> list[dict[str, str]]:
    """Deduped, bounded ``[{text, source}]`` for the alignment prompt.

    Anchors arrive pre-sorted by source trust (analytics > nav > docs > i18n >
    test). Dedup case-insensitively (preserving the first/highest-trust
    occurrence), then cap at :data:`MAX_ANCHOR_TEXTS_DIGEST`. Pure + stable →
    identical anchors yield an identical list (cache-key precondition).
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in product_anchors or []:
        text = (getattr(a, "text", "") or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "source": str(getattr(a, "source", ""))})
        if len(out) >= MAX_ANCHOR_TEXTS_DIGEST:
            break
    return out


def _cache_key(
    digest: dict[str, Any], anchor_texts: list[dict[str, str]], model: str,
) -> str:
    """Stable content hash of (digest + sorted anchor texts + model + version).

    Same repo state + same anchors + same model → same key → cache hit →
    byte-identical output on re-scan. ``sort_keys`` makes the JSON canonical."""
    payload = json.dumps(
        {
            "v": ABSTRACTION_CACHE_VERSION,
            "model": model,
            "mode": "aligned" if anchor_texts else "free",
            "anchors": sorted(a["text"].lower() for a in anchor_texts),
            "digest": digest,
        },
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


def _tool_use_input(msg: Any, tool_name: str) -> dict[str, Any] | None:
    """Return the ``.input`` of the first genuine ``tool_use`` block, else ``None``.

    Robust to SDK shapes where blocks expose ``type``/``name``/``input`` as
    attributes (real SDK) — and tolerant when those are absent (test fakes
    that only carry ``.text`` blocks return ``None`` → caller parses text).

    Returns ``None`` for the subscription-proxy SHIM that wraps the model's
    text in ``{"_raw": "```json…```"}`` instead of honouring the schema — that
    is NOT a real structured answer (the CLI refuses an unknown tool), so the
    caller must fall back to a plain text call which the proxy DOES serve.
    """
    try:
        for block in getattr(msg, "content", None) or []:
            if getattr(block, "type", None) == "tool_use":
                if tool_name and getattr(block, "name", tool_name) != tool_name:
                    continue
                data = getattr(block, "input", None)
                if isinstance(data, dict):
                    if set(data.keys()) == {"_raw"}:
                        return None  # proxy shim — treat as tools-not-honoured
                    return data
    except Exception:  # noqa: BLE001 — never let block inspection abort the stage
        return None
    return None


def _call_structured(
    client: Any, *, model: str, system: str, user: str, tool: dict[str, Any],
    max_tokens: int, llm_health: LlmHealth | None = None,
) -> tuple[dict[str, Any] | None, int, int, str]:
    """Forced tool-use call → parsed dict. Falls back to text+regex.

    Returns ``(parsed_or_None, in_tokens, out_tokens, path)`` where ``path`` is
    ``"tool"``, ``"text"`` (tool returned no tool_use block — fake/SDK ignored
    it), ``"text_fallback"`` (tools kwarg errored, retried plain), or ``""`` on
    a hard failure. The ``system`` prompt embeds the JSON schema so the text
    fallback parses identically. Determinism is via ``deterministic_params``
    (temperature=0) — re-asserted here so this stage can never drift to a
    non-zero temperature even if the shared helper changes.
    """
    if llm_health is not None and not llm_health.should_call():
        return None, 0, 0, ""

    params = dict(deterministic_params(model))
    params["temperature"] = 0  # hard guarantee for this stage (stability gate)

    def _usage(msg: Any) -> tuple[int, int]:
        u = getattr(msg, "usage", None)
        return (int(getattr(u, "input_tokens", 0) or 0),
                int(getattr(u, "output_tokens", 0) or 0))

    def _text_of(msg: Any) -> str:
        try:
            return "\n".join(
                t for block in (getattr(msg, "content", None) or [])
                if (t := getattr(block, "text", None))
            )
        except Exception:  # noqa: BLE001
            return ""

    def _create(with_tools: bool) -> Any:
        kw: dict[str, Any] = dict(
            model=gateway_model(model), max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}], **params,
        )
        if with_tools:
            kw["tools"] = [tool]
            kw["tool_choice"] = {"type": "tool", "name": tool["name"]}
        return client.messages.create(**kw)

    # 1) Forced tool-use. A genuine tool_use block is the deterministic path.
    #    A fake/SDK that ignores the kwarg may still return parseable text.
    try:
        msg = _create(with_tools=True)
    except Exception as exc:  # noqa: BLE001 — tools unsupported / transient
        logger.info("stage_6_7d: tool-use call errored (%s) — retrying plain text", exc)
        msg = None
    if msg is not None:
        in_tok, out_tok = _usage(msg)
        parsed = _tool_use_input(msg, tool["name"])
        if parsed is not None:
            if llm_health is not None:
                llm_health.record_success()
            return parsed, in_tok, out_tok, "tool"
        text = _text_of(msg)
        if text:
            ptext = _parse_json(text)
            if ptext is not None:
                if llm_health is not None:
                    llm_health.record_success()
                return ptext, in_tok, out_tok, "text"
        # Tool attempt produced nothing usable (proxy ``_raw`` shim / refusal /
        # no parseable text) → fall through to a plain (no-tools) call, which
        # the subscription proxy DOES serve.

    # 2) Plain text fallback.
    try:
        msg2 = _create(with_tools=False)
    except Exception as exc2:  # noqa: BLE001 — non-fatal at scan-time
        if llm_health is not None and llm_health.record_failure(
            exc2, stage="stage_6_7d_llm_journey_abstraction",
        ):
            logger.error("stage_6_7d: LLM auth failed — skipping: %s", exc2)
        else:
            logger.warning("stage_6_7d: structured call failed: %s", exc2)
        return None, 0, 0, ""
    if llm_health is not None:
        llm_health.record_success()
    in2, out2 = _usage(msg2)
    return _parse_json(_text_of(msg2)), in2, out2, "text_fallback"


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
            from_code_only=bool(spec.get("from_code_only")),
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
    code_only_by_cap = {
        (s.get("name") or "").strip(): bool(s.get("from_code_only"))
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
        feat.from_code_only = code_only_by_cap.get(cap, False)
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
    product_anchors: "list[ProductAnchor] | None" = None,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    cost_tracker: CostTracker | None = None,
    cache: "CacheBackend | None" = None,
    log: "StageLogger | None" = None,
    llm_health: LlmHealth | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> tuple[list["UserFlow"], list["Feature"], dict[str, tuple[str, ...]] | None, dict[str, Any]]:
    """Rewrite user_flows[] + product_features[] at journey/capability grain.

    When ``product_anchors`` is supplied (Phase-2 ALIGN mode) the LLM ALIGNS the
    code evidence to that deterministic, authoritative capability list instead
    of free-generating — bounding the output to the anchor set (kills over-
    production / blobs / dups). With no anchors it falls back to today's free-
    generation, so callers that never ran Phase-1 behave exactly as before.

    Stability: both calls are forced through a tool-use schema (with a text+regex
    fallback) at ``temperature=0``; when a ``cache`` backend is given, the two
    model outputs are content-hash cached on (digest + sorted anchor texts +
    model) so a re-scan of an unchanged repo replays the SAME answers → the
    rebuilt arrays are byte-identical.

    Returns ``(user_flows, product_features, dev_to_product_map, telemetry)``.
    On ANY failure the ORIGINAL inputs are returned unchanged (degrade), and
    ``dev_to_product_map`` is ``None`` (caller keeps its existing mapping).
    """
    tele: dict[str, Any] = {
        "enabled": True, "applied": False, "degraded_reason": None,
        "uf_before": len(user_flows), "uf_after": len(user_flows),
        "pf_before": len(product_features), "pf_after": len(product_features),
        "files_before": 0, "files_after": 0, "cost_usd": 0.0, "llm_calls": 0,
        "aligned": False, "anchor_count": 0, "cache_hit": False,
        "structured_path": None, "from_code_only_pf": 0, "from_code_only_uf": 0,
    }
    files_before = sum(len(_paths_of(p)) for p in product_features)
    tele["files_before"] = files_before

    anchor_texts = _canonical_anchor_texts(product_anchors)
    tele["aligned"] = bool(anchor_texts)
    tele["anchor_count"] = len(anchor_texts)

    if not developer_features:
        tele["degraded_reason"] = "no_dev_features"
        return user_flows, product_features, None, tele

    digest = _build_digest(developer_features, product_features, user_flows, routes_index)
    key = _cache_key(digest, anchor_texts, model)
    tele["cache_key"] = key

    def _record(model_: str, in_tok: int, out_tok: int) -> float:
        cost = estimate_call_cost(model_, in_tok, out_tok) if (in_tok or out_tok) else 0.0
        if cost_tracker is not None and (in_tok or out_tok):
            try:
                cost_tracker.record(model=model_, input_tokens=in_tok,
                                    output_tokens=out_tok, label="stage_6_7d")
            except Exception:  # noqa: BLE001 — budget cap is enforced elsewhere
                pass
        return cost

    def _finish(
        abstraction: dict[str, Any], dev_map: dict[str, str],
    ) -> tuple[list["UserFlow"], list["Feature"], dict[str, tuple[str, ...]], dict[str, Any]] | None:
        """Deterministic reconstruction shared by the cache-hit + live paths.

        Identical (abstraction, dev_map, developer_features) → identical output,
        which is what makes a cache hit byte-identical to the original run."""
        uf_specs = abstraction.get("user_flows") or []
        pf_specs = abstraction.get("product_features") or []
        new_pfs, dev_to_product, files_after = _build_product_features(
            pf_specs, dev_map, developer_features)
        new_ufs = _build_user_flows(uf_specs, user_flows)
        if not new_pfs or not new_ufs:
            return None
        tele.update({
            "applied": True, "uf_after": len(new_ufs), "pf_after": len(new_pfs),
            "files_after": files_after, "dev_mapped": len(dev_map),
            "dev_total": len(developer_features),
            "residual_devs": sum(1 for d in developer_features
                                 if (d.display_name or d.name) not in dev_map),
            "from_code_only_pf": sum(1 for p in new_pfs if p.from_code_only),
            "from_code_only_uf": sum(1 for u in new_ufs if u.from_code_only),
        })
        if log is not None:
            log.info(
                "stage_6_7d: UF %d->%d, PF %d->%d, files %d->%d, dev_mapped %d/%d, "
                "aligned=%s cache_hit=%s code_only=%d/%d $%.4f",
                tele["uf_before"], tele["uf_after"], tele["pf_before"], tele["pf_after"],
                files_before, files_after, tele["dev_mapped"], tele["dev_total"],
                tele["aligned"], tele["cache_hit"], tele["from_code_only_pf"],
                tele["from_code_only_uf"], tele["cost_usd"],
            )
        return new_ufs, new_pfs, dev_to_product, tele

    # ── Cache lookup — content-keyed replay (byte-identical re-scan) ──
    if cache is not None:
        try:
            cached = cache.get(CacheKind.LLM_ABSTRACTION.value, key)
        except Exception:  # noqa: BLE001 — a cache fault must never abort the stage
            cached = None
        if isinstance(cached, dict) and cached.get("v") == ABSTRACTION_CACHE_VERSION:
            c_abs = cached.get("abstraction") or {}
            c_map_raw = cached.get("map") or {}
            c_map = {k: v for k, v in c_map_raw.items()
                     if isinstance(k, str) and isinstance(v, str)}
            if (c_abs.get("user_flows") and c_abs.get("product_features") and c_map):
                tele["cache_hit"] = True
                result = _finish(c_abs, c_map)
                if result is not None:
                    return result
                tele["cache_hit"] = False  # malformed → fall through to live call

    cli = client if client is not None else _client_factory()
    if cli is None:
        tele["degraded_reason"] = "no_client"
        return user_flows, product_features, None, tele

    # ── Call 1 — abstraction / alignment (forced tool-use) ────────────
    if anchor_texts:
        sys1 = _ALIGN_SYSTEM
        anchor_block = ("\n\nAUTHORITATIVE product_capability_anchors "
                        "(align the evidence to these):\n"
                        + json.dumps(anchor_texts, ensure_ascii=False))
    else:
        sys1 = _ABSTRACTION_SYSTEM
        anchor_block = ""
    user1 = ("Repository evidence (code-grounded, no README):\n```json\n"
             + json.dumps(digest, ensure_ascii=False) + "\n```" + anchor_block
             + "\nEmit the structured output now.")
    parsed1, in1, out1, path1 = _call_structured(
        cli, model=model, system=sys1, user=user1, tool=_ABSTRACTION_TOOL,
        max_tokens=ABSTRACTION_MAX_TOKENS, llm_health=llm_health)
    tele["llm_calls"] += 1
    tele["structured_path"] = path1
    tele["cost_usd"] = round(tele["cost_usd"] + _record(model, in1, out1), 6)
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

    # ── Call 2 — dev → capability re-attribution (forced tool-use) ────
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
    parsed2, in2, out2, _path2 = _call_structured(
        cli, model=model, system=_REATTRIB_SYSTEM, user=user2, tool=_REATTRIB_TOOL,
        max_tokens=REATTRIB_MAX_TOKENS, llm_health=llm_health)
    tele["llm_calls"] += 1
    tele["cost_usd"] = round(tele["cost_usd"] + _record(model, in2, out2), 6)
    dev_map_raw = (parsed2 or {}).get("map") or {}
    dev_map = {k: v for k, v in dev_map_raw.items() if isinstance(k, str) and isinstance(v, str)}
    # Re-attribution failed entirely (LLM error / bad JSON / health-blocked):
    # without it every dev feature would collapse into the Shared Platform
    # residual, emitting a degenerate single-blob product layer. Degrade fully
    # — return the ORIGINAL arrays unchanged (graceful-degrade invariant).
    if not dev_map:
        tele["degraded_reason"] = "reattrib_failed"
        return user_flows, product_features, None, tele

    # ── Persist the two structured outputs for byte-identical replay ──
    if cache is not None:
        try:
            cache.set(CacheKind.LLM_ABSTRACTION.value, key, {
                "v": ABSTRACTION_CACHE_VERSION,
                "abstraction": {"product_features": pf_specs, "user_flows": uf_specs},
                "map": dev_map,
            })
        except Exception:  # noqa: BLE001 — a cache write fault must not abort
            pass

    # ── Reconstruct (conservation guaranteed — residual catches omits) ─
    result = _finish(parsed1, dev_map)
    if result is None:
        tele["degraded_reason"] = "reconstruct_empty"
        return user_flows, product_features, None, tele
    return result
