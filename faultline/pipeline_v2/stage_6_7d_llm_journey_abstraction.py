"""Stage 6.7d — LLM product/journey abstraction (opt-in, default OFF).

Crosses the code-grain → product-grain gap that the deterministic stages
structurally cannot (see faultlines-app memory
``finding-llm-abstraction-p0-confirmed-2026-06-30`` + ``…codegrain-ceiling…``).

The deterministic pipeline emits CODE-grain artifacts: Stage 6.7 rolls flows
into one ``user_flow`` PER CRUD operation (1.5-4.4x over-produced vs a curated
journey golden), and Stage 6.5's workspace clusterer RE-LUMPS dev features into
coarse ``product_features``. When ENABLED, this stage REWRITES both arrays at
product/journey grain via two LLM calls over a CODE-grounded digest — Call 1
(the grain-lift) on SONNET by default (validated model; Haiku is too weak), Call
2 (re-attribution) on the passed model (Haiku on a default scan):

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
import hashlib
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
# The abstraction (Call 1) grain-lift was validated on SONNET — Haiku is too
# weak for it (it degrades + under-produces). The stage therefore resolves its
# abstraction model from a DEDICATED env, INDEPENDENT of the scan's main
# model_id (which stays Haiku for cost). Call 2 (re-attribution) is a cheap
# mechanical map and stays on the passed model_id (Haiku) — the validated
# "Sonnet + Haiku" combo. Override via FAULTLINE_STAGE_6_7D_ABSTRACTION_MODEL.
DEFAULT_ABSTRACTION_MODEL = "claude-sonnet-4-6"
ABSTRACTION_MODEL_ENV = "FAULTLINE_STAGE_6_7D_ABSTRACTION_MODEL"
# ~50-120 coarsened UFs + ~30-60 PFs must fit in ONE structured response on a
# large repo (dub). 8k truncated the JSON on big repos → parse-fail → degrade.
ABSTRACTION_MAX_TOKENS = 16000
REATTRIB_MAX_TOKENS = 16000
COST_CAP_USD = 0.60            # whole-stage guard against a runaway response
MAX_DEV_FEATURES_DIGEST = 200  # abstraction digest cap (re-attrib sees all)
MAX_ROUTES_DIGEST = 160
# The CURRENT user flows are SUPPORTING detail in the abstraction prompt (the
# model coarsens them). On dub (222 UFs) the raw list blew the prompt/output
# budget and forced a degrade. Cap to the top-N by member_count (the heaviest,
# most-supported journeys) — a prompt-size bound, NOT a tuned threshold.
MAX_USER_FLOWS_DIGEST = 120
# Anchor-alignment (Phase 2): cap the authoritative anchor vocabulary fed into
# the align prompt (a prompt-size bound). The GATE below decides align-vs-free-gen.
MAX_ANCHOR_TEXTS_DIGEST = 160
# improve-or-no-op gate floor: align only with a viable product taxonomy (a small
# absolute bound; the primary gate is the anchors>=features ratio — scale-invariant).
_MIN_ANCHORS_FLOOR = 8
# ALIGN is OPT-IN (default OFF). Empirically it DEGRADES stability on noisy anchor
# pools (Soc0 i18n leaf values: PF name-Jaccard 0.72 free-gen → 0.03 align) — the
# quantity gate can't tell a clean pool from UI-string noise. Until a QUALITY gate
# exists, free-gen is the default; align is opt-in for clean-anchor repos.
ALIGN_ENV = "FAULTLINE_STAGE_6_7D_ALIGN"


def align_enabled() -> bool:
    return os.environ.get(ALIGN_ENV, "0").strip() not in {"0", "false", "False", ""}

#: Bumped whenever the prompt / reconstruction changes in a way that would make
#: a previously-cached answer wrong. Part of the cache key, so a bump
#: transparently invalidates every stale entry.
ABSTRACTION_CACHE_VERSION = "align-1"

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


def resolve_abstraction_model() -> str:
    """Model for Call 1 (the grain-lift). Read from
    :data:`ABSTRACTION_MODEL_ENV`, defaulting to :data:`DEFAULT_ABSTRACTION_MODEL`
    (Sonnet) — DELIBERATELY decoupled from the scan's main model_id so the
    abstraction always runs on the model it was validated on, even on a
    Haiku-default scan. An empty env value falls back to the default."""
    return os.environ.get(ABSTRACTION_MODEL_ENV, "").strip() or DEFAULT_ABSTRACTION_MODEL


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


# ── Alignment system prompt (Phase 2 ALIGN mode — anchors present) ──────────
# When the gate says a repo has a rich, clean anchor pool, the LLM ALIGNS the
# code evidence to the AUTHORITATIVE anchor list instead of free-generating.
# Using anchor text VERBATIM as the name is what makes names STABLE across runs
# (the Phase-1 finding: free-gen naming drifts; anchors are deterministic).
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
     shows a real customer capability the anchors missed.
  5. Do NOT emit anchors that have NO supporting code evidence (an anchor with
     no related dev feature / route / flow is noise — drop it). Do NOT emit bare
     code-structure leaks ("lib","web","core","utils","components") as features.

Title Case names; product voice. Ground EVERY item in the supplied evidence.

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


def _canonical_anchor_texts(
    product_anchors: "list[ProductAnchor] | None",
) -> list[str]:
    """Deduped, bounded, stable list of anchor texts for the align prompt.

    Anchors arrive pre-sorted by source trust (analytics > nav > i18n > test).
    Dedup case-insensitively preserving the first/highest-trust occurrence, cap
    at MAX_ANCHOR_TEXTS_DIGEST. Pure + stable → identical anchors yield an
    identical list (a cache-key precondition, and the source of name stability).
    """
    out: list[str] = []
    seen: set[str] = set()
    for a in product_anchors or []:
        text = (getattr(a, "text", "") or "").strip()
        if not text:
            continue
        low = text.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(text)
        if len(out) >= MAX_ANCHOR_TEXTS_DIGEST:
            break
    return out


def _anchors_sufficient(
    product_anchors: "list[ProductAnchor] | None",
    product_features: list["Feature"],
) -> bool:
    """Improve-or-no-op gate: ALIGN only when the clean anchor pool is rich
    enough to be an authoritative product taxonomy — i.e. at least as many
    distinct capability anchors as deterministic product features (you cannot
    align to a bounded target smaller than the #capabilities without
    under-producing — the documenso 4-anchor failure), and above a small floor.
    Below that → free-generate (the validated default). Scale-invariant: the
    primary test is the anchors>=features ratio, not a per-repo tuned number."""
    distinct = len({
        (getattr(a, "text", "") or "").strip().lower()
        for a in (product_anchors or [])
        if (getattr(a, "text", "") or "").strip()
    })
    return distinct >= _MIN_ANCHORS_FLOOR and distinct >= len(product_features or [])


def _build_digest(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    user_flows: list["UserFlow"],
    routes_index: list[dict[str, Any]],
) -> dict[str, Any]:
    # Secondary key (name) breaks commit-count ties deterministically — else
    # equal-commit modules keep input order (nondeterministic) and the digest
    # JSON varies run-to-run, defeating the content-hash cache (byte-identical
    # re-scan). Same reason for the UF + re-attribution sorts below.
    devf = sorted(developer_features, key=lambda f: (-(f.total_commits or 0), f.name or ""))
    dev_lines = [
        {"name": f.display_name or f.name, "where": _top_dirs(_paths_of(f)),
         "what": _short(f.description, 90)}
        for f in devf[:MAX_DEV_FEATURES_DIGEST]
    ]
    pf_lines = [
        {"name": p.display_name or p.name, "what": _short(p.description, 110)}
        for p in product_features
    ]
    # CURRENT user flows are only SUPPORTING detail — cap to the top-N by
    # member_count so a large repo (dub: 222 UFs) can't blow the prompt/output
    # budget and force a degrade. The heaviest journeys carry the most signal;
    # the rest are redundant CRUD variants the model would coarsen away anyway.
    ufs_by_weight = sorted(user_flows, key=lambda u: (-(u.member_count or 0), u.id or ""))
    uf_lines = [
        {"id": u.id, "name": u.name, "resource": u.resource,
         "domain": u.domain, "intent": u.intent}
        for u in ufs_by_weight[:MAX_USER_FLOWS_DIGEST]
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


def _cache_key(digest: dict[str, Any], abstraction_model: str, reattrib_model: str,
               anchor_sig: str = "") -> str:
    """Stable content hash of (digest + both model ids + version).

    Same repo state + same models → same key → cache hit → byte-identical
    output on re-scan. ``sort_keys`` makes the JSON canonical. The abstraction
    model is part of the key so flipping FAULTLINE_STAGE_6_7D_ABSTRACTION_MODEL
    transparently invalidates stale entries. Content-keyed (same input → same
    answer), so this is a deterministic short-circuit, NOT per-repo memory —
    compliant with rule-cold-scan.
    """
    payload = json.dumps(
        {
            "v": ABSTRACTION_CACHE_VERSION,
            "abstraction_model": abstraction_model,
            "reattrib_model": reattrib_model,
            "anchor_sig": anchor_sig,
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

    Returns ``(user_flows, product_features, dev_to_product_map, telemetry)``.

    NEVER-WORSE INVARIANT (operator's core requirement): on ANY failure path —
    no client, LLM exception, empty / unparseable output, re-attribution
    failure, cost-cap, empty reconstruction — the ORIGINAL deterministic
    ``user_flows`` + ``product_features`` are returned UNCHANGED (object
    identity), ``dev_to_product_map`` is ``None`` (caller keeps its mapping),
    and ``telemetry["fallback"]`` is set to the reason string. On success
    ``telemetry["fallback"]`` is ``None`` and ``applied`` is ``True``.

    Call 1 (abstraction / grain-lift) runs on :func:`resolve_abstraction_model`
    (Sonnet by default) regardless of ``model``. Call 2 (re-attribution) runs
    on the passed ``model`` (Haiku on a default scan). ``cache`` (when supplied)
    content-keys the two structured LLM outputs so a re-scan of an unchanged
    repo replays byte-identical output at $0.
    """
    tele: dict[str, Any] = {
        "enabled": True, "applied": False, "degraded_reason": None,
        "fallback": None, "cache_hit": False,
        "uf_before": len(user_flows), "uf_after": len(user_flows),
        "pf_before": len(product_features), "pf_after": len(product_features),
        "files_before": 0, "files_after": 0, "cost_usd": 0.0, "llm_calls": 0,
    }
    files_before = sum(len(_paths_of(p)) for p in product_features)
    tele["files_before"] = files_before

    def _degrade(reason: str) -> tuple[
        list["UserFlow"], list["Feature"], None, dict[str, Any]
    ]:
        """Never-worse exit: return the ORIGINAL inputs UNCHANGED (identity) and
        record the fallback reason. Every early-return in this function routes
        through here, so no failure path can ever emit a partial/degenerate
        result — the deterministic Stage 6.5/6.7 output passes through."""
        tele["degraded_reason"] = reason
        tele["fallback"] = reason
        return user_flows, product_features, None, tele

    abstraction_model = resolve_abstraction_model()
    reattrib_model = model
    tele["abstraction_model"] = abstraction_model
    tele["reattrib_model"] = reattrib_model

    if not developer_features:
        return _degrade("no_dev_features")

    def _record(model_: str, in_tok: int, out_tok: int) -> float:
        cost = estimate_call_cost(model_, in_tok, out_tok) if (in_tok or out_tok) else 0.0
        if cost_tracker is not None and (in_tok or out_tok):
            try:
                cost_tracker.record(model=model_, input_tokens=in_tok,
                                    output_tokens=out_tok, label="stage_6_7d")
            except Exception:  # noqa: BLE001 — budget cap is enforced elsewhere
                pass
        return cost

    # ── Digest + input-size telemetry (large-repo robustness) ─────────
    digest = _build_digest(developer_features, product_features, user_flows, routes_index)
    tele.update({
        "input_dev_features": len(developer_features),
        "input_user_flows": len(user_flows),
        "input_routes": len(routes_index),
        "digest_dev_features": len(digest["developer_features"]),
        "digest_user_flows": len(digest["current_user_flows"]),
        "digest_routes": len(digest["routes"]),
    })
    # Phase 2: decide ALIGN (bounded, anchor-verbatim naming → stable) vs FREE-GEN
    # (validated default). The gate is improve-or-no-op: sparse-anchor repos fall
    # back so they never regress. anchor_sig makes align/free-gen cache separately.
    anchor_texts = _canonical_anchor_texts(product_anchors)
    aligned = align_enabled() and _anchors_sufficient(product_anchors, product_features)
    tele["aligned"] = aligned
    tele["anchor_count"] = len(anchor_texts)
    anchor_sig = json.dumps([t.lower() for t in anchor_texts], sort_keys=True) if aligned else ""
    key = _cache_key(digest, abstraction_model, reattrib_model, anchor_sig)

    def _finish(
        abstraction: dict[str, Any], dev_map: dict[str, str],
    ) -> tuple[list["UserFlow"], list["Feature"],
               dict[str, tuple[str, ...]], dict[str, Any]] | None:
        """Reconstruct the rewritten arrays from the two structured outputs and
        stamp success telemetry. Shared by the LIVE path and the cache-hit path
        — identical inputs → identical objects, so a cache hit is byte-identical
        to the original live run. Returns ``None`` on an empty reconstruction."""
        uf_specs_ = abstraction.get("user_flows") or []
        pf_specs_ = abstraction.get("product_features") or []
        new_pfs, dev_to_product, files_after = _build_product_features(
            pf_specs_, dev_map, developer_features)
        new_ufs = _build_user_flows(uf_specs_, user_flows)
        if not new_pfs or not new_ufs:
            return None
        # Deterministic output ordering (Phase 1 stability): the LLM emits
        # features/flows in an order that drifts run-to-run. Sort by a stable key
        # so the output array order never churns — applies identically to the
        # live and cache-hit paths, preserving the byte-identical-replay invariant.
        # Sort by CONTENT-derived keys only. NB: UserFlow.id is "UF-NNN" assigned
        # from the LLM's emission POSITION, so sorting by id would be a no-op (it
        # re-encodes the drifting response order) — use name+resource so identical
        # output content yields identical array order across independent runs.
        new_pfs.sort(key=lambda p: ((getattr(p, "name", "") or "").lower(), getattr(p, "name", "") or ""))
        new_ufs.sort(key=lambda u: ((getattr(u, "name", "") or "").lower(), str(getattr(u, "resource", "") or "")))
        tele.update({
            "applied": True, "fallback": None,
            "uf_after": len(new_ufs), "pf_after": len(new_pfs),
            "files_after": files_after, "dev_mapped": len(dev_map),
            "dev_total": len(developer_features),
            "residual_devs": sum(1 for d in developer_features
                                 if (d.display_name or d.name) not in dev_map),
        })
        if log is not None:
            # StageLogger.info(reason, feature=None, **extra) takes only 2-3
            # positional args — pre-format the message (%-style separate args
            # raise TypeError, which the caller's broad except turns into a
            # spurious "reconstruct_exception" degrade on the success path).
            log.info(
                "stage_6_7d: UF %d->%d, PF %d->%d, files %d->%d, dev_mapped %d/%d, "
                "abs_model=%s cache_hit=%s $%.4f" % (
                    tele["uf_before"], tele["uf_after"], tele["pf_before"], tele["pf_after"],
                    files_before, files_after, tele["dev_mapped"], tele["dev_total"],
                    abstraction_model, tele["cache_hit"], tele["cost_usd"],
                )
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
            if c_abs.get("user_flows") and c_abs.get("product_features") and c_map:
                tele["cache_hit"] = True
                try:
                    result = _finish(c_abs, c_map)
                except Exception:  # noqa: BLE001 — malformed cache must never crash
                    result = None
                if result is not None:
                    return result
                tele["cache_hit"] = False  # malformed → fall through to live call

    cli = client if client is not None else _client_factory()
    if cli is None:
        return _degrade("no_client")

    # ── Call 1 — abstraction / grain-lift (Sonnet); ALIGN when anchors suffice ─
    if aligned:
        sys1 = _ALIGN_SYSTEM
        anchor_block = ("\n\nAUTHORITATIVE product_capability_anchors "
                        "(align the evidence to these; use verbatim as names):\n"
                        + json.dumps(anchor_texts, ensure_ascii=False))
    else:
        sys1 = _ABSTRACTION_SYSTEM
        anchor_block = ""
    user1 = ("Repository evidence (code-grounded, no README):\n```json\n"
             + json.dumps(digest, ensure_ascii=False) + "\n```" + anchor_block
             + "\nEmit the JSON now.")
    text1, in1, out1 = _call_haiku(
        cli, model=abstraction_model, system=sys1, user=user1,
        max_tokens=ABSTRACTION_MAX_TOKENS, llm_health=llm_health)
    tele["llm_calls"] += 1
    tele["cost_usd"] = round(tele["cost_usd"] + _record(abstraction_model, in1, out1), 6)
    parsed1 = _parse_json(text1)
    if not parsed1:
        return _degrade("abstraction_parse_failed")

    # Sanitise at the boundary: keep only specs whose "name" is a non-empty
    # string. An LLM can emit a numeric/None name in otherwise-valid JSON, and
    # every downstream consumer calls ``.strip()``/``.get()`` on it — dropping
    # them here means no reconstruction path can raise (never-worse). If this
    # empties a list, degrade rather than proceed on garbage.
    def _valid_spec(s: Any) -> bool:
        return (isinstance(s, dict) and isinstance(s.get("name"), str)
                and bool(s.get("name").strip()))

    uf_specs = [s for s in (parsed1.get("user_flows") or []) if _valid_spec(s)]
    pf_specs = [s for s in (parsed1.get("product_features") or []) if _valid_spec(s)]
    if not uf_specs or not pf_specs:
        return _degrade("abstraction_empty")
    parsed1 = {"user_flows": uf_specs, "product_features": pf_specs}
    if tele["cost_usd"] > COST_CAP_USD:
        return _degrade(f"cost_cap ${tele['cost_usd']:.4f}")

    # ── Call 2 — dev → capability re-attribution (Haiku / passed model) ─
    caps = [s.get("name", "").strip() for s in pf_specs if s.get("name", "").strip()]
    caps_with_residual = caps + [_RESIDUAL_CAP]
    dev_items = [
        {"name": f.display_name or f.name, "where": _top_dirs(_paths_of(f)),
         "n_files": len(_paths_of(f))}
        for f in sorted(developer_features, key=lambda f: (-len(_paths_of(f)), f.name or ""))
    ]
    user2 = ("Product capabilities:\n" + json.dumps(caps_with_residual) +
             "\n\nDeveloper features (name, dir, file count):\n" +
             json.dumps(dev_items, ensure_ascii=False) +
             "\n\nReturn the full map now (every dev feature mapped).")
    text2, in2, out2 = _call_haiku(
        cli, model=reattrib_model, system=_REATTRIB_SYSTEM, user=user2,
        max_tokens=REATTRIB_MAX_TOKENS, llm_health=llm_health)
    tele["llm_calls"] += 1
    tele["cost_usd"] = round(tele["cost_usd"] + _record(reattrib_model, in2, out2), 6)
    parsed2 = _parse_json(text2)
    dev_map_raw = (parsed2 or {}).get("map") or {}
    dev_map = {k: v for k, v in dev_map_raw.items() if isinstance(k, str) and isinstance(v, str)}
    # Re-attribution failed entirely (LLM error / bad JSON / health-blocked):
    # without it every dev feature would collapse into the Shared Platform
    # residual, emitting a degenerate single-blob product layer. Degrade fully
    # — return the ORIGINAL arrays unchanged (never-worse invariant).
    if not dev_map:
        return _degrade("reattrib_failed")

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
    # Never-worse: any reconstruction error (e.g. LLM returns a non-string
    # "name" → .strip() raises) degrades to the original deterministic arrays
    # rather than crashing the scan.
    try:
        result = _finish(parsed1, dev_map)
    except Exception:  # noqa: BLE001
        return _degrade("reconstruct_exception")
    if result is None:
        return _degrade("reconstruct_empty")
    return result
