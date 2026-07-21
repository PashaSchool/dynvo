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
    features the model omits are token-matched to an emitted capability, and
    only true platform containers fall into the ``Shared Platform`` residual,
    which carries the house "workspace anchor" platform marker so blob
    metrics never read it as the top product feature (trustworthy-core fix
    A1, 2026-07-02).

Trustworthy-core fix B (2026-07-02): every rewritten user_flow must be
GROUNDED — members inherited via ``from_flows``, grounded via the cited
``from_dev_features``'s flows, or deterministically rescued by resource/token
match against unclaimed flows / route patterns. A UF still holding 0 members
AND 0 routes is DROPPED (it was an LLM-invented 0-LOC journey name).

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


# ── S5b Seg H — digest stratification (default OFF) ─────────────────────────
# Class: digest-shadow starvation — the Call-1 digest carries MASS, not
# surface identity. On any repo above the caps the ranked cuts starve the
# whole page surface out of ALL channels at once (novu: 'sign-in'=0 tokens
# in the reconstructed prompt → 0/12 strict draws propose 'Sign in').
# Two CUT-ONLY mechanisms (probe canon 2026-07-20, s5bh-out/):
#   M1-ADDITIVE (UF channel): page-anchored UFs (entry flow = product-PAGE
#     file, P1 predicate) that did NOT fit the mass-sorted cap are APPENDED
#     BEYOND the cap — a reservation append, never displacement (the
#     fixed-cap stratified form starved 5/67 cached proposals: REFUTED).
#     The stratum qualifies INDEPENDENT of product_feature_id (UF-104
#     pf=None class).
#   M2-HYGIENE-QUOTA (route channel): under route pressure, half the route
#     budget is reserved for the HYGIENIC page stream (PAGE rows minus
#     storybook/dev-artifact paths and filename-echo patterns), the rest
#     fills in original order. Emission keeps the original relative order —
#     a pure cut change, so a repo whose routes all fit stays byte-identical
#     (inertness law). M2b slug-dedup was probe-REFUTED (flat page dirs).
# Registered in ``scan_result_cache.ENV_OUTPUT_FLAGS`` (append-only, no
# KEY_SCHEMA bump — reconciled at merge).
DIGEST_STRATIFICATION_ENV = "FAULTLINE_DIGEST_STRATIFICATION"


def digest_stratification_enabled() -> bool:
    """Default **OFF**. Unset / falsy keeps both digest cuts byte-identical
    to the flag-less engine (kill-switch law)."""
    return os.environ.get(DIGEST_STRATIFICATION_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }

#: Bumped whenever the prompt / reconstruction changes in a way that would make
#: a previously-cached answer wrong. Part of the cache key, so a bump
#: transparently invalidates every stale entry. "contract-3" invalidates the
#: frozen uncompressed draws that motivated the grain-contract gate
#: (2026-07-04: inbox-zero 140-in -> 140-emitted cached under "ground-2").
#: "contract-4" invalidates draws frozen before the jpf structural anchor
#: (MISSION-92 lever #3): entries cached under "contract-3" may be
#: two-axis-inflated draws the jpf prong would now retry.
ABSTRACTION_CACHE_VERSION = "contract-4"

# ── Grain-contract gate (2026-07-04) ────────────────────────────────────────
# Call 1 is the ABSTRACTION layer: its whole job is a grain LIFT (merge CRUD /
# variant flows of the same resource into one journey). A draw that emits
# roughly one user_flow per digest user_flow performed NO lift — it echoed the
# input grain. Ratio justified against corpus draws (rule-no-magic-tuning §6):
# broken draws sit at emitted/digest >= 1.0 (inbox-zero 140/120=1.17,
# documenso 89/61=1.46, cal-com 119/120=0.99 — all Jul-3/4 baselines), healthy
# draws at <= ~0.7 (inbox-zero Jul-2: 145->84 = 0.58; supabase 74 emitted on a
# larger digest). 0.9 splits the two populations with margin on both sides.
# SCALE-INVARIANT: both prongs are ratios of the digest the model actually saw.
UF_CONTRACT_RATIO = 0.9
#: telemetry values for scan_meta.stage_6_7d_journey_abstraction.abstraction_contract
CONTRACT_PASS = "pass"
CONTRACT_PASS_AFTER_RETRY = "pass_after_retry"
CONTRACT_UNCOMPRESSED = "uncompressed"
#: arming reasons for scan_meta...contract_armed_by (MISSION-92 lever #3)
CONTRACT_ARMED_RATIO = "ratio"
CONTRACT_ARMED_JPF = "jpf"

# ── JPF structural anchor — second arming prong (2026-07-04, lever #3) ──────
# The ratio prong alone misses a real inflation class: draws that stay under
# 0.9 x digest UFs yet still run 1.5-2.7x the golden journey grain (inbox-zero
# 79-96 built vs golden 35; supabase 85 vs 46 — Jul-4 recorded draws). The
# naive "draw journeys-per-PF > digest journeys-per-PF" test is structurally
# GAMEABLE and empirically dead on those draws: an inflated draw inflates its
# OWN product_features list too (inbox-zero r3: 50 PFs vs digest 31), so its
# j/pf reads a healthy 1.55-1.81 — below ANY digest-derived prior (2.58/3.87).
# The anchor therefore tests the same journeys-per-capability contract in
# DECOMPOSED two-axis form: a draw performed NO grain lift when it emitted
#   (a) MORE journeys than the digest has distinct flow resources, AND
#   (b) MORE capabilities than the deterministic product layer found.
# Both comparisons are ratios of THIS repo's own digest structure with the
# structural threshold 1.0 — no tuned constant (rule-no-magic-tuning).
# Recorded-draw check (Jul-4 baseline + replays): arms 4/4 known-inflated
# (inbox-zero base/r3/r4, supabase r1) with 0/4 false fires on known-good
# (dub 80.0, cal-com 80.4, formbricks 77.3/83.8); also arms supabase base
# 48.2 + documenso base 64.2 (the class the corrective retry lifted +8.5).

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
# The residual bucket is a PLATFORM container, not a customer capability. Its
# description carries the house-wide "workspace anchor" platform marker so
# every consumer that already special-cases platform buckets
# (eval/structural_audit._is_platform_feature, blob/concentration metrics,
# dashboards) recognises it and never reads it as the repo's top product
# feature. An empty description here is what made the residual score as the
# #1 blob in 6/7 re-score repos (2026-07-01 audit).
_RESIDUAL_DESCRIPTION = (
    "Shared platform bucket (workspace anchor): cross-cutting infrastructure "
    "— shared UI kit, generic lib/utils, build config, app shell — serving "
    "many capabilities; not a customer-facing product feature."
)
# Bare code-structure names: a developer feature whose SLUG is one of these is
# a container/scaffold, not a capability — when Call 2 omits it from the map
# it belongs in the platform residual, never as a token-match rescue target.
# Mirrors the code-structure-leak list in the abstraction prompt. Full-slug
# match ONLY (token-level matching would swallow real capabilities like
# "api-tokens").
_STRUCTURE_LEAK_SLUGS = frozenset({
    "lib", "libs", "web", "core", "editor", "utils", "util", "components",
    "shared", "common", "packages", "app", "apps", "src", "frontend",
    "backend", "server", "client", "api", "ui", "config", "scripts", "docs",
    "types", "hooks", "styles", "assets", "public", "internal", "tools",
    "vendor", "misc", "helpers", "tests", "test",
    # Entry-module / mock-scaffolding classes (tier-3 resettle, 2026-07-05):
    # `main` (backend/main.py app entry) OWNS diag/admin routes + flows yet is
    # an infra anchor, never a customer capability; mock fixtures likewise.
    "main", "mock", "mocks",
})
# Generic verbs/glue excluded from grounding-match content tokens (mirrors the
# uf-scorer convention: journeys are matched on NOUNS, not on CRUD verbs).
_GROUND_STOP = frozenset(
    "flow flows the and or to of for with your from into a an edit manage "
    "browse filter create run view configure set up new add make get list "
    "show require build connect process submit administer enforce update "
    "delete remove use using via".split()
)


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


# ── S2 Seg C — canonical LLM batch composition (flag OFF) ───────────────────
#
# The probe (2026-07-18) measured the resample class: 1 drifting flow of 980
# flipped the WHOLE-batch cache key (sha256 over model+prompt) and the full
# resample cost −26% UF. Part of the key's surface is VOLATILE — pure counts
# that change without any semantic content change: ``n_dev_features`` (a
# global count; a feature past the digest cap changes ONLY this number),
# ``n_files`` per dev row in the Call-2 re-attribution prompt, and the raw
# ``member_count`` sort rank in the digest's UF ordering (a ±1 member drift
# reorders the list and re-cuts the cap). Under FAULTLINE_LLM_BATCH_CANON
# those volatile fields leave the prompt canon and the weight ordering uses
# log2 BUCKETS (scale-invariant; a ±1 drift almost never crosses a power-of-2
# boundary), so a pure count drift no longer invalidates the key. A REAL
# content change (names/routes/resources) still flips it — content-keyed law.
# Default OFF → digest/prompts byte-identical.

BATCH_CANON_ENV = "FAULTLINE_LLM_BATCH_CANON"


def batch_canon_enabled() -> bool:
    """Default ON since the 2026-07-19 S*-pack flip (KEY_SCHEMA 32;
    cache-hit-rate on the 1/980 drift — S2 Seg C).
    ``FAULTLINE_LLM_BATCH_CANON=0`` (or false/no/off) keeps digest, prompts
    and keys byte-identical to pre-fix — explicit off stays a valid
    kill-switch forever."""
    return os.environ.get(BATCH_CANON_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def _weight_bucket(count: int) -> int:
    """log2 bucket of a member count — the canon's stable ordering rank.
    Scale-invariant (no tuned threshold): 0→0, 1→1, 2-3→2, 4-7→3, 8-15→4 …"""
    return int(count).bit_length() if count > 0 else 0


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
     primary noun), the `product_feature` it belongs to, `from_flows` = the
     list of CURRENT user-flow ids it subsumes (for member inheritance), and
     `from_dev_features` = the developer_features (their `name` VERBATIM from
     the evidence) whose code implements this journey.
     CRITICAL: `from_flows` is bookkeeping — it must NEVER cap your journey
     count. Enumerate the distinct capabilities FIRST (rule 1), THEN attach
     whichever current ids fit; `from_flows` may be empty for a capability the
     CRUD walk missed. Do NOT collapse your journeys down to roughly the number
     of current_user_flows — there are usually MORE distinct capabilities than
     the CRUD list shows.
  6. GROUNDING IS MANDATORY: every user_flow MUST cite at least one
     `from_flows` id OR one `from_dev_features` name. A journey citing neither
     is ungrounded and the engine will DROP it — never emit a capability you
     cannot point to concrete code evidence (a dev feature or a current flow)
     for.

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
 "user_flows":[{"name":"...","resource":"...","product_feature":"...",
  "from_flows":["UF-001", ...],"from_dev_features":["<dev feature name>", ...]}]}"""


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
     CURRENT user-flow ids that belong to each journey (member inheritance) and
     `from_dev_features` to the developer_features (their `name` VERBATIM)
     whose code implements it. `from_flows` may be empty for a capability whose
     CRUD flows were not detected — never inflate or cap your list to the
     current_user_flows count. GROUNDING IS MANDATORY: every user_flow MUST
     cite at least one `from_flows` id OR one `from_dev_features` name, or the
     engine will DROP it.
  4. Emit an item NOT covered by ANY anchor ONLY when the code evidence clearly
     shows a real customer capability the anchors missed.
  5. Do NOT emit anchors that have NO supporting code evidence (an anchor with
     no related dev feature / route / flow is noise — drop it). Do NOT emit bare
     code-structure leaks ("lib","web","core","utils","components") as features.

Title Case names; product voice. Ground EVERY item in the supplied evidence.

Return STRICT JSON only, no prose:
{"product_features":[{"name":"...","description":"..."}],
 "user_flows":[{"name":"...","resource":"...","product_feature":"...",
  "from_flows":["UF-001", ...],"from_dev_features":["<dev feature name>", ...]}]}"""

# Merge-corrective system addendum for the ONE contract retry. Structural
# anchor: the journey count is judged against the model's OWN product_features
# (journeys-per-capability), never against a fixed count or the input UF count
# (rule-no-magic-tuning).
_MERGE_CORRECTIVE = """

PREVIOUS ATTEMPT REJECTED — it emitted roughly one user_flow per input
current_user_flow (no grain lift). You are the ABSTRACTION layer: consolidate
the redundant CRUD/variant flows of the SAME resource into ONE journey per
capability. Judge your journey count against your OWN product_features list —
a healthy journey list has a small number of user_flows PER product feature —
never against the size of current_user_flows. Merge aggressively; keep only
genuinely DISTINCT capabilities separate. Re-emit the full JSON now."""

# jpf-anchor corrective for a draw armed by the JPF prong ALONE (a ratio-armed
# draw keeps the validated _MERGE_CORRECTIVE above — the prompt behind the
# documenso +8.5 retry — unchanged). Names the structural anchor explicitly
# (MISSION-92 lever #3).
_JPF_CORRECTIVE = """

PREVIOUS ATTEMPT REJECTED — it emitted MORE journeys than the repository has
distinct flow resources AND multiplied product capabilities beyond what the
deterministic scanner found (no grain lift on either axis). You are the
ABSTRACTION layer. Structural anchor: emit roughly ONE journey per distinct
capability of each product feature — merge the CRUD / settings / variant
journeys of the SAME capability into that single journey — and consolidate
near-duplicate capabilities instead of multiplying them. Judge your journey
count against your OWN product_features list: a healthy journey list has a
small number of user_flows PER product feature. Re-emit the full JSON now."""

_REATTRIB_SYSTEM = """You assign each developer feature (a code module) to exactly ONE product
capability from the given list, using the module name and its directory. Every
developer feature must map to exactly one capability. If a module is generic
shared infrastructure (ui kit, icons, generic lib/utils, build config, app
shell) that serves many capabilities, assign it to "Shared Platform".
Return STRICT JSON only: {"map": {"<dev feature name>": "<capability>", ...}}."""

# ── Anchored system prompt (Product-Spine §4.3, Wave 2b) ────────────────────
# With FAULTLINE_SPINE_ANCHORED_MINT on, the product-feature universe is FIXED
# by the deterministic anchored mint (Stage 6.86) — the LLM may describe those
# capabilities and write journeys that cite them, but it may NEVER invent a
# product feature or move membership (rootcause RC1: Call-2 retired; Call-1
# constrained). ``current_product_features`` in the digest IS the fixed list.
_ANCHORED_SYSTEM = """You are the journey-abstraction layer of a code-intelligence engine.
A deterministic scanner parsed a repository into CODE-GRAIN artifacts: developer
features (one per code module/dir), user flows (one per CRUD op), HTTP routes —
and a FIXED list of product capabilities (`current_product_features`), each
already bound to its code by a deterministic anchor spine.

THE CAPABILITY LIST IS FIXED. You must NOT invent, rename, split, or merge
product features — any product_features entry whose name is not VERBATIM from
`current_product_features` will be DROPPED by the engine. Your job:
  1. user_flows — re-express the CRUD-grain flows as PRODUCT JOURNEYS. Merge
     redundant CRUD variants of the SAME resource into one journey; keep
     distinct capabilities separate; surface cross-cutting journeys (auth,
     integrations, onboarding) the per-resource walk misses. Each user_flow:
     a short verb phrase `name`, one lowercase `resource`, `product_feature`
     = one name VERBATIM from `current_product_features`, `from_flows` = the
     CURRENT user-flow ids it subsumes, `from_dev_features` = the developer
     features (names VERBATIM) whose code implements it. GROUNDING IS
     MANDATORY: every user_flow MUST cite at least one `from_flows` id OR one
     `from_dev_features` name, or it will be DROPPED.
  2. product_features — OPTIONAL descriptions: for capabilities you understand
     from the evidence, emit {"name": "<verbatim from current_product_features>",
     "description": "<one customer-voice sentence>"}. Emit NOTHING else.

Return STRICT JSON only, no prose:
{"product_features":[{"name":"...","description":"..."}],
 "user_flows":[{"name":"...","resource":"...","product_feature":"...",
  "from_flows":["UF-001", ...],"from_dev_features":["<dev feature name>", ...]}]}"""

# W4 (§4.6) — appended to _ANCHORED_SYSTEM ONLY when the digest carries
# page-interior sections (Stage 6.55 active). Extends the citation
# vocabulary to sub-anchors; the engine verifies every cited section
# against the SAME capability's section list — cross-PF citations are
# dropped, so grounded journeys stay within ONE PF's subtree.
_INTERIOR_ADDENDUM = """

INTERIOR SECTIONS: some entries in `current_product_features` carry a
`sections` list — author-visible page sections/components a deterministic
parser found INSIDE that capability's own pages. When a journey happens on
such a section, ALSO cite it: add `"from_sections": ["<section label>", ...]`
to that user_flow, copying labels VERBATIM. Cited sections MUST belong to the
SAME capability you name in `product_feature` — a citation of another
capability's section is invalid and will be ignored. Prefer the section's own
wording for the journey name where it fits (it is the maintainer's
vocabulary)."""


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


def _content_tokens(*texts: str | None) -> set[str]:
    """Noun-ish content tokens for deterministic grounding matches: lowercase
    alnum words, crudely singularised, minus generic verbs/glue. Mirrors the
    uf-scorer tokenisation so engine-side grounding and eval-side matching
    agree on what counts as content."""
    out: set[str] = set()
    for t in texts:
        for w in re.findall(r"[a-z0-9]+", (t or "").lower()):
            if len(w) > 3 and w.endswith("s"):
                w = w[:-1]
            if len(w) > 2 and w not in _GROUND_STOP:
                out.add(w)
    return out


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


_SPLIT_MARKER = "sub-domain"
# _make_subfeature stamps: "sub-domain '<domain_key>' of feature '<source.name>'".
# The NAME channel is the reliable provenance: ``split_from`` (source uuid) is
# None in practice — dev features get their uuid backfilled AFTER Stage 8, so
# at mint time getattr(source, "uuid", None) yields nothing (measured on
# supabase wave-9: 67/67 subs split_from=None, description parseable 67/67).
_SPLIT_PARENT_RE = re.compile(r"of feature '([^']+)'\s*$")


def _split_parent_name(f: "Feature") -> str | None:
    """The parent feature NAME a Stage 8.9/8.9.5 subfeature was split from,
    or ``None`` when *f* is not a split subfeature."""
    desc = getattr(f, "description", None) or ""
    if _SPLIT_MARKER not in desc.lower():
        return None
    m = _SPLIT_PARENT_RE.search(desc)
    return m.group(1) if m else None


def _dev_key(f: Any) -> str:
    return getattr(f, "display_name", None) or getattr(f, "name", "") or ""


def _rollup_split_view(
    developer_features: list["Feature"],
) -> tuple[list[Any], dict[str, str]]:
    """Abstraction-input view with split-minted subfeatures FOLDED back into
    their parents. Returns ``(view, sub_to_parent_key)``.

    The DF layer decomposes physical containers (Stage 8.9 deterministic +
    8.9.5 LLM splits, 19-67 thin subfeatures on a monorepo) — but that grain
    choice must NOT leak into the product abstraction: measured on supabase,
    the post-split dev list (227 items, thin low-commit subs crowding the
    MAX_DEV_FEATURES_DIGEST cap) systematically degraded UF-F1 44.5→~34
    while the same engine with splits off held ~44 (2026-07-02 waves 3-7).
    Folding subs into their parent makes the 6.7d input INVARIANT to how
    deeply the container was decomposed; the parent's capability is
    propagated back to each sub at reconstruction, so PF aggregation places
    sub files exactly where a no-split scan would. A sub whose parent is
    absent (husk dropped) stays standalone. Pure view — real Feature objects
    are never mutated (proxies via SimpleNamespace).
    """
    from types import SimpleNamespace

    by_name = {f.name: f for f in developer_features}
    by_uuid = {
        u: f for f in developer_features
        if (u := getattr(f, "uuid", None))
    }

    def _root_ancestor(sub: "Feature") -> "Feature | None":
        """Walk the split chain to its NON-SUB root. Stage 8.9 recurses on
        its own minted subs (parent -> sub -> grandchild), so one hop lands
        on an intermediate sub whose own exclusion would silently drop the
        grandchild from the view (audit IMPORTANT, 2026-07-02). Cycle-guarded;
        an unresolvable chain returns None (the sub stays standalone)."""
        seen: set[int] = set()
        cur = sub
        while True:
            parent_name = _split_parent_name(cur)
            if parent_name is None:
                return cur  # reached a non-sub feature = the root
            if id(cur) in seen:
                return None  # cycle — treat as orphan
            seen.add(id(cur))
            # NAME channel first (reliable), uuid channel as a fallback.
            parent = by_name.get(parent_name) or by_uuid.get(
                getattr(cur, "split_from", None))
            if parent is None or parent is cur:
                return None  # orphan (husk dropped)
            cur = parent

    sub_to_parent: dict[str, str] = {}
    folded: dict[int, list["Feature"]] = defaultdict(list)
    for f in developer_features:
        if _split_parent_name(f) is None:
            continue
        root = _root_ancestor(f)
        if root is None or root is f:
            continue
        sub_to_parent[_dev_key(f)] = _dev_key(root)
        folded[id(root)].append(f)

    view: list[Any] = []
    for f in developer_features:
        if _dev_key(f) in sub_to_parent:
            continue  # folded into its parent
        subs = folded.get(id(f))
        if not subs:
            view.append(f)
            continue
        view.append(SimpleNamespace(
            name=f.name,
            display_name=getattr(f, "display_name", None),
            description=getattr(f, "description", None),
            member_files=None,  # force _paths_of onto the folded paths
            paths=list(getattr(f, "paths", None) or [])
            + [p for sub in subs for p in _paths_of(sub)],
            total_commits=(getattr(f, "total_commits", 0) or 0)
            + sum((getattr(sub, "total_commits", 0) or 0) for sub in subs),
        ))
    return view, sub_to_parent


def _propagate_dev_map(
    dev_map: dict[str, str], sub_to_parent: dict[str, str],
) -> dict[str, str]:
    """Extend the parent-level Call-2 map onto split subfeatures: each sub
    inherits its parent's capability, so ``_build_product_features``
    aggregates sub files into the SAME product feature a no-split scan
    would. Parents missing from the map stay unmapped (their subs fall to
    the normal fallback path)."""
    out = dict(dev_map)
    for sub_key, parent_key in sub_to_parent.items():
        cap = dev_map.get(parent_key)
        if cap and sub_key not in out:
            out[sub_key] = cap
    return out


def _page_anchored_uf_ids(
    user_flows: list["UserFlow"],
    developer_features: list["Feature"],
    routes_index: list[dict[str, Any]],
) -> frozenset[str]:
    """S5b Seg H M1 — ids of UFs any of whose member flows ENTERS on a
    product-PAGE file (the Seg C P1 predicate via
    :func:`leafroute_promotion._page_files`: layout/loading/error/template +
    ``_app``/``_document``/``_error`` + ``pages/api`` excluded; ``page.<ext>``,
    a component under a ``pages``/``routes`` segment, or a PAGE-method
    routes_index file qualify). The stratum is INDEPENDENT of
    ``product_feature_id`` — a pf=None journey (UF-104 class) anchors the
    same. Walks the ORIGINAL developer features (rollup SimpleNamespaces
    carry no flows)."""
    from faultline.pipeline_v2.leafroute_promotion import _page_files

    entry_by_mid: dict[str, str] = {}
    for d in developer_features:
        for fl in getattr(d, "flows", None) or []:
            mid = _flow_member_id(fl)
            if not mid:
                continue
            ep = getattr(fl, "entry_point_file", None)
            if ep and mid not in entry_by_mid:
                entry_by_mid[mid] = str(ep)
    ri_page_files = {
        str(r.get("file"))
        for r in routes_index
        if str(r.get("method") or "").upper() == "PAGE"
    }
    out: set[str] = set()
    for u in user_flows:
        for mid in u.member_flow_ids or []:
            ep = entry_by_mid.get(mid, "")
            if ep and _page_files([ep], ri_page_files):
                out.add(u.id or "")
                break
    out.discard("")
    return frozenset(out)


#: template placeholders (``/${ROUTES.SIGN_IN}/*``) are route INDIRECTION,
#: not filename identity — stripped before the echo test below.
_ROUTE_TPL = re.compile(r"\$\{[^}]*\}")


def _hygienic_page_route(r: dict[str, Any]) -> bool:
    """S5b Seg H M2 hygiene — is this PAGE row real page surface (quota-
    worthy) or artifact noise? Two rungs, both mechanism-first:

    1. artifact PATHS — the spa-router YAML skip vocabulary (storybook /
       stories / playground / examples / demo / generated) as corroboration:
       exact dir-segment match with dunder wrappers normalized
       (``__stories__`` ≡ ``stories``) + dotted filename markers
       (``SignInUp.stories.tsx``). Twenty exhibit: SignInUp.stories.
    2. filename-ECHO patterns — an authored web route is lowercase; a
       LITERAL pattern segment carrying an uppercase letter is the source
       filename echoed as a pseudo-route (component-path class; twenty
       exhibit: /object-record/RecordShowPageHeader). Dynamic params
       (``:connectedAccountId``, ``[slug]``) legitimately carry camelCase
       and are skipped; ``${...}`` templates are stripped first.

    Hygiene only DEMOTES a row out of the reserved page quota — the row
    stays eligible for the original-order fill, so a no-pressure repo is
    untouched (inertness law)."""
    from faultline.pipeline_v2.extractors.spa_router import (
        _skip_filename_markers,
        _skip_segments,
    )

    f = str(r.get("file") or "").lower()
    segs = f.split("/")
    if any(s.strip("_") in _skip_segments() for s in segs[:-1]):
        return False
    base = segs[-1] if segs else ""
    dotparts = base.split(".")
    if len(dotparts) >= 2 and any(
        comp in _skip_filename_markers() for comp in dotparts[1:-1]
    ):
        return False
    pat = _ROUTE_TPL.sub("", str(r.get("pattern") or ""))
    for seg in pat.split("/"):
        if seg.startswith(":") or seg.startswith("["):
            continue
        if any(c.isupper() for c in seg):
            return False
    return True


def _build_digest(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    user_flows: list["UserFlow"],
    routes_index: list[dict[str, Any]],
    *,
    stratified: bool = False,
    page_anchored: frozenset[str] | None = None,
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
    # S2 Seg C: under the batch canon the weight rank is the log2 BUCKET of
    # member_count, so a ±1 member drift no longer reorders the digest (and
    # re-cuts the cap) — a volatile-count key flip. OFF: raw count, unchanged.
    if batch_canon_enabled():
        ufs_by_weight = sorted(
            user_flows,
            key=lambda u: (-_weight_bucket(u.member_count or 0), u.id or ""),
        )
    else:
        ufs_by_weight = sorted(user_flows, key=lambda u: (-(u.member_count or 0), u.id or ""))
    uf_lines = [
        {"id": u.id, "name": u.name, "resource": u.resource,
         "domain": u.domain, "intent": u.intent}
        for u in ufs_by_weight[:MAX_USER_FLOWS_DIGEST]
    ]
    # S5b Seg H M1-ADDITIVE: page-anchored UFs that did not fit the
    # mass-sorted cap are APPENDED BEYOND it, in the same deterministic
    # weight order. The capped prefix stays byte-identical (displacement=0
    # — appending, not re-cutting, is what keeps every cached proposal's
    # raw material in the digest); a repo whose UFs all fit appends
    # nothing (inertness law).
    if stratified and page_anchored:
        uf_lines += [
            {"id": u.id, "name": u.name, "resource": u.resource,
             "domain": u.domain, "intent": u.intent}
            for u in ufs_by_weight[MAX_USER_FLOWS_DIGEST:]
            if (u.id or "") in page_anchored
        ]
    seen: set[tuple] = set()
    routes: list[dict[str, Any]] = []
    if not stratified:
        for r in routes_index:
            key = (r.get("pattern"), r.get("method"))
            if key in seen:
                continue
            seen.add(key)
            routes.append({"p": r.get("pattern"), "m": r.get("method"), "t": r.get("trigger")})
            if len(routes) >= MAX_ROUTES_DIGEST:
                break
    else:
        # S5b Seg H M2-HYGIENE-QUOTA — same dedup, but under route pressure
        # HALF the budget (a scale-invariant ratio of the cap, not a count)
        # is reserved for the hygienic page stream; the rest fills in
        # original order. Emission keeps the original relative order — a
        # pure CUT change, so when every unique route fits the digest is
        # byte-identical to the unstratified cut. spa-page rows
        # (method=PAGE, B74 route-table / B65-v3) ride the page stream by
        # construction.
        uniq: list[dict[str, Any]] = []
        for r in routes_index:
            key = (r.get("pattern"), r.get("method"))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        if len(uniq) <= MAX_ROUTES_DIGEST:
            chosen = uniq
        else:
            page_idx = [
                i for i, r in enumerate(uniq)
                if str(r.get("method") or "").upper() == "PAGE"
                and _hygienic_page_route(r)
            ]
            reserved = set(page_idx[:MAX_ROUTES_DIGEST // 2])
            budget = MAX_ROUTES_DIGEST - len(reserved)
            picked = set(reserved)
            for i in range(len(uniq)):
                if budget <= 0:
                    break
                if i in reserved:
                    continue
                picked.add(i)
                budget -= 1
            chosen = [uniq[i] for i in sorted(picked)]
        routes = [
            {"p": r.get("pattern"), "m": r.get("method"), "t": r.get("trigger")}
            for r in chosen
        ]
    digest = {
        "n_dev_features": len(developer_features),
        "developer_features": dev_lines,
        "current_product_features": pf_lines,
        "current_user_flows": uf_lines,
        "routes": routes,
    }
    if batch_canon_enabled():
        # S2 Seg C: ``n_dev_features`` is a VOLATILE global count — a feature
        # past the digest cap changes ONLY this number and flips the whole-
        # batch key with zero semantic content change. The canon drops it from
        # the digest (prompt + key). OFF keeps it byte-identically.
        digest.pop("n_dev_features")
    return digest


def _reattrib_dev_items(dev_view: list["Feature"]) -> tuple[list[dict[str, Any]], str]:
    """Call-2 re-attribution rows + their prompt header.

    OFF (default): rows carry ``n_files`` and sort by descending file count —
    byte-identical to the pre-canon prompt. S2 Seg C ON: ``n_files`` is a
    VOLATILE count (any file add/drop in a feature flips the whole Call-2
    prompt with no attribution-relevant change) — the canon drops it and
    sorts by the stable name key only.
    """
    rows: list[dict[str, Any]]
    if batch_canon_enabled():
        rows = [
            {"name": _dev_key(f), "where": _top_dirs(_paths_of(f))}
            for f in sorted(dev_view, key=lambda f: f.name or "")
        ]
        return rows, "Developer features (name, dir):\n"
    rows = [
        {"name": _dev_key(f), "where": _top_dirs(_paths_of(f)),
         "n_files": len(_paths_of(f))}
        for f in sorted(dev_view, key=lambda f: (-len(_paths_of(f)), f.name or ""))
    ]
    return rows, "Developer features (name, dir, file count):\n"


def _digest_resource_keys(digest: dict[str, Any]) -> set[str]:
    """Distinct-resource keys of the digest user_flows. UFs without a resource
    count as distinct (unknown mergeability -> conservative). Shared by the
    ratio prong (redundancy arming) and the jpf prong (resource-grain prior)."""
    keys: set[str] = set()
    for u in digest.get("current_user_flows") or []:
        res = str(u.get("resource") or "").strip().lower()
        keys.add(res if res else f"name:{u.get('name') or u.get('id') or id(u)}")
    return keys


def _contract_gate_armed(digest: dict[str, Any]) -> bool:
    """Arm the grain-contract gate ONLY when the digest shows mergeable
    same-resource redundancy — i.e. collapsing the CRUD variants of each
    resource into one journey would already pass the ratio. A repo whose
    deterministic UF list is ALREADY at resource grain (libraries: ~one flow
    per distinct resource) has nothing to compress, and Call 1 legitimately
    EXPANDS there (surfacing capabilities the CRUD walk missed — the validated
    fastapi lift), so the gate must never fire. UFs without a resource are
    counted as distinct (unknown mergeability -> conservative). Scale-invariant
    on both sides (rule-no-magic-tuning)."""
    ufs = digest.get("current_user_flows") or []
    if not ufs:
        return False
    return len(_digest_resource_keys(digest)) < UF_CONTRACT_RATIO * len(ufs)


def _contract_ok(n_emitted: int, n_digest_ufs: int) -> bool:
    """True when the draw performed a grain lift: emitted user_flows sit BELOW
    ``UF_CONTRACT_RATIO`` x the digest user_flows the model saw."""
    return n_emitted < UF_CONTRACT_RATIO * n_digest_ufs


def _distinct_pf_count(pf_specs: list[dict[str, Any]]) -> int:
    """Distinct emitted capabilities (case-folded name dedup — the LLM
    occasionally echoes a capability twice)."""
    return len({s["name"].strip().lower() for s in pf_specs})


def _jpf_armed(
    uf_specs: list[dict[str, Any]],
    pf_specs: list[dict[str, Any]],
    digest: dict[str, Any],
) -> bool:
    """JPF structural-anchor prong (see the module-level rationale at
    :data:`CONTRACT_ARMED_JPF`): True when the draw shows NO grain lift on
    EITHER axis of the journeys-per-capability contract —
      journeys axis:     emitted user_flows exceed the digest's distinct
                         flow resources (journeys above resource grain);
      capabilities axis: emitted product_features exceed the deterministic
                         product layer's count (capabilities multiplied).
    A lift on either axis (dub: 80 journeys but 44 <= 45 capabilities;
    formbricks: 66 <= 74 resources) means the draw abstracted SOMETHING and
    must not be rejected. Priors are THIS repo's own digest structure; the
    threshold on both axes is the structural 1.0 (rule-no-magic-tuning).

    Two preconditions keep the prong out of regimes where its priors are
    meaningless:
      * the digest must show same-resource redundancy
        (:func:`_contract_gate_armed`) — at resource grain (libraries) Call 1
        legitimately EXPANDS (the validated fastapi 41->73 lift) and
        "emitted > resources" would reject exactly that lift;
      * the deterministic product layer must be a viable capability taxonomy
        (>= :data:`_MIN_ANCHORS_FLOOR` features — the SAME viability bound
        the align gate uses, not a new constant): on a 1-3 PF repo every
        multi-capability draw would trivially exceed the prior."""
    ufs = digest.get("current_user_flows") or []
    pfs = digest.get("current_product_features") or []
    if not ufs or not pfs:
        return False  # nothing structural to anchor on -> never arm
    if len(pfs) < _MIN_ANCHORS_FLOOR or not _contract_gate_armed(digest):
        return False
    return (len(uf_specs) > len(_digest_resource_keys(digest))
            and _distinct_pf_count(pf_specs) > len(pfs))


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
    # Single capability/PF-name normalizer (emission-integrity fix): both the
    # UF's product_feature_id and the PF's name now slug through the SAME
    # function, so a special-char capability yields one consistent key. ASCII
    # single-spaced labels are byte-identical to the legacy regex → digest-safe.
    from faultline.pipeline_v2.emission_integrity import canonical_slug
    return canonical_slug(name)


def _intent_for(name: str) -> str:
    for tok in re.findall(r"[a-z]+", (name or "").lower()):
        if tok in _INTENT:
            return _INTENT[tok]
    return "other"


# Hard byte bound on the first-draw record (names + tuples only, no member
# lists) — a runaway draw must never bloat the artifact / cache entry.
_FIRST_DRAW_SPEC_MAX_BYTES = 100_000


def _first_draw_summary(
    uf_specs: list[dict[str, Any]], pf_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compact record of a parsed Call-1 draw: uf names + (resource,
    intent-class) per uf + pf names + emitted counts. NO member lists.

    Persisted when a contract retry REPLACES the first draw (mission-92
    band-guard post-mortem: the first draw's specs were unrecoverable from
    any artifact or cache, so offline both-candidate calibration had zero
    data). Pure observability — nothing in the pipeline consumes it. If the
    JSON record would exceed :data:`_FIRST_DRAW_SPEC_MAX_BYTES` the per-spec
    lists are dropped and ``truncated`` is flagged (counts always survive).
    """
    summary: dict[str, Any] = {
        "uf": [
            {"name": str(s.get("name") or ""),
             "resource": str(s.get("resource") or "").strip().lower(),
             "intent": _intent_for(str(s.get("name") or ""))}
            for s in uf_specs
        ],
        "pf": [str(s.get("name") or "") for s in pf_specs],
        "uf_emitted": len(uf_specs),
        "pf_emitted": len(pf_specs),
    }
    try:
        oversized = (len(json.dumps(summary, ensure_ascii=False).encode("utf-8"))
                     > _FIRST_DRAW_SPEC_MAX_BYTES)
    except Exception:  # noqa: BLE001 — recording must never break the stage
        oversized = True
    if oversized:
        summary = {"uf": [], "pf": [], "uf_emitted": len(uf_specs),
                   "pf_emitted": len(pf_specs), "truncated": True}
    return summary


def _flow_member_id(flow: Any) -> str:
    """Stable member identifier — mirrors Stage 6.7's ``_flow_key`` (uuid when
    present, else name) so grounded members join the same id space the
    deterministic UFs use."""
    return getattr(flow, "uuid", "") or getattr(flow, "name", "") or ""


_HOME_PURE_ENV = "FAULTLINE_UF_HOME_PURE_INHERIT"


def _home_pure_enabled() -> bool:
    return (os.environ.get(_HOME_PURE_ENV, "1") or "1").strip().lower() \
        not in {"0", "false", "no", "off"}


# ── B74 Seg C — home-pure: container ≠ foreignness (default OFF) ─────────
_CONTAINER_INHERIT_ENV = "FAULTLINE_HOME_PURE_CONTAINER_INHERIT"

_WS_ANCHOR_PREFIX = "ws:"


def _container_inherit_enabled() -> bool:
    return (os.environ.get(_CONTAINER_INHERIT_ENV, "0") or "0") \
        .strip().lower() in {"1", "true", "yes", "on"}


def _container_pf_keys(product_features: list["Feature"]) -> frozenset[str]:
    """PF keys of monorepo ws-pkg CONTAINERS (B74 Seg C, Form A ONLY).

    The anchored mint stamps ``Feature.anchor_id`` with the spine
    anchor's canonical id — ``ws:<workspace-path>`` marks a
    workspace-package anchor (the PF-side twin of Stage 8.7's
    ``_is_workspace_anchor`` dev-side marker). A journey member whose
    HOME is such a container carries packaging provenance, not
    foreignness: on twenty every twenty-front dev stamps home
    "twenty-front", so home-pure filtered ENTIRE product cores
    (24,226 filters/scan; 'Sign in and authenticate' dropped at 0
    members). Mass/ratio container forms were REFUTED by the
    2026-07-19 probe (they misclassify real capabilities at every
    rung) — the mint marker is the only detector."""
    out: set[str] = set()
    for pf in product_features:
        aid = str(getattr(pf, "anchor_id", None) or "")
        if aid.startswith(_WS_ANCHOR_PREFIX):
            key = getattr(pf, "name", "") or ""
            if key:
                out.add(key)
    return frozenset(out)


def _flow_home_map(
    developer_features: list["Feature"],
) -> dict[str, str | None]:
    """member-flow id → HOME product feature (entry-owner first, owning
    dev's stamp fallback — the same rule the span split uses). ``None``
    = lane/unowned home (no evidence of foreignness — inheritable by
    any journey)."""
    from faultline.pipeline_v2.flow_span_split import _owner_map

    owner = _owner_map(developer_features)
    home: dict[str, str | None] = {}
    for d in developer_features:
        dev_pf = getattr(d, "product_feature_id", None)
        for fl in getattr(d, "flows", None) or []:
            mid = _flow_member_id(fl)
            if not mid:
                continue
            entry = getattr(fl, "entry_point_file", None)
            home[mid] = (owner.get(entry) if entry else None) or dev_pf
    return home


def _build_user_flows(
    uf_specs: list[dict[str, Any]],
    old_ufs: list["UserFlow"],
    developer_features: list["Feature"],
    routes_index: list[dict[str, Any]],
    interior_evidence: dict[str, Any] | None = None,
    home_pure: bool = False,
    container_pf_keys: frozenset[str] | None = None,
) -> tuple[list["UserFlow"], dict[str, Any]]:
    """Reconstruct the abstracted user_flows — GROUNDED-ONLY.

    Every emitted UF must end up with real code behind it (member flows and/or
    routes). Grounding sources, in order:
      1. ``from_flows`` — member inheritance from the deterministic UFs
         (the validated original channel).
      2. ``from_dev_features`` — the cited dev features' UNCLAIMED flows
         (content-token overlap preferred; the channel that grounds
         capabilities the CRUD walk missed).
      3. Deterministic rescue — resource/token match of a still-empty UF
         against ALL unclaimed flows, then against ``routes_index`` patterns.
    A UF still holding 0 members AND 0 routes after all three is DROPPED —
    an LLM-invented capability name with no code is exactly the 0-LOC defect
    the trustworthy-core mission forbids (2026-07-01 audit: 5-53% of UFs).
    A UF holding routes but 0 members (validator I7, 2026-07-05) gets its
    members BACKFILLED from the route-owning devs' unclaimed flows — or is
    dropped when none remain (a memberless twin of an already-claimed flow).
    Returns ``(user_flows, grounding_telemetry)``.
    """
    from faultline.models.types import UserFlow

    old_by_id = {u.id: u for u in old_ufs}
    # Grounding pools: dev-feature lookup (name + display_name, case-folded)
    # and per-flow content tokens (flow name + owning dev name).
    dev_by_name: dict[str, "Feature"] = {}
    for d in developer_features:
        for key in {d.name, d.display_name}:
            if key:
                dev_by_name[key.strip().lower()] = d
    flow_tokens: dict[str, set[str]] = {}
    dev_flow_ids: dict[str, list[str]] = {}
    for d in developer_features:
        ids: list[str] = []
        dtok = _content_tokens(d.display_name or d.name)
        for fl in getattr(d, "flows", None) or []:
            mid = _flow_member_id(fl)
            if not mid:
                continue
            ids.append(mid)
            flow_tokens[mid] = _content_tokens(
                getattr(fl, "name", None), getattr(fl, "display_name", None),
            ) | dtok
        dev_flow_ids[d.name] = ids

    def _route_patterns_for(resource: str) -> list[str]:
        """routes_index patterns having *resource* (singular/plural) as a path
        segment — the deterministic route-grounding rescue."""
        res = resource[:-1] if len(resource) > 3 and resource.endswith("s") else resource
        if not res:
            return []
        hits: set[str] = set()
        for r in routes_index:
            pattern = str(r.get("pattern") or "")
            for seg in re.split(r"[/.]", pattern.lower()):
                seg = re.sub(r"[^a-z0-9]+", "", seg)
                if len(seg) > 3 and seg.endswith("s"):
                    seg = seg[:-1]
                if seg and seg == res:
                    hits.add(pattern)
                    break
        return sorted(hits)

    tele: dict[str, Any] = {
        "uf_dev_grounded": 0, "uf_rescued_flows": 0,
        "uf_rescued_routes": 0, "uf_dropped_ungrounded": 0,
        "uf_rescue_dropped_collisions": 0,
        "uf_route_member_backfill": 0, "uf_dropped_memberless": 0,
        "uf_dropped_names": [],
        "uf_sections_cited": 0, "uf_sections_invalid": 0,
        "uf_section_grounded": 0,
        "uf_home_filtered": 0,
    }

    # W4 §4.6 — HOME-PURE membership (anchored mode): a journey's cited
    # evidence must live within ONE PF's subtree, so a spec citing
    # capability X may only claim flows whose HOME is X (entry-owner
    # first, owning dev fallback) or lane/unowned (no foreignness
    # evidence). Call-1 lumping sibling capabilities' CRUD journeys
    # (openstatus keyed A/B: "Manage status pages" bound to
    # status-reports carrying 182-span status-pages blocs — the swap
    # class) is filtered AT CONSTRUCTION; the foreign flows stay
    # unclaimed for their own capability's journeys / backstop.
    home_by_mid: dict[str, str | None] = (
        _flow_home_map(developer_features)
        if home_pure and _home_pure_enabled() else {}
    )
    # B74 Seg C (FAULTLINE_HOME_PURE_CONTAINER_INHERIT, default OFF):
    # home = ws-pkg CONTAINER (anchored-mint "ws:" marker) is packaging,
    # not foreignness. Probe-canon scoped form (2026-07-19, replay-
    # refined member-by-member on the twenty capture):
    #   * A capability journey (own PF NOT a container) inherits
    #     container-homed members on the CITED channels — Pass 1
    #     from_flows + Pass 2a cited devs. Client+server journeys span
    #     containers ('Sign in and authenticate' = 8 twenty-front + 3
    #     twenty-server members), so the inherit is not per-container.
    #   * RESERVATION: a container-homed member riding a dev that some
    #     OTHER journey cites with a MATCHING product feature (spec pf
    #     slug == the member's home) belongs to that rightful claimant
    #     — it is never container-inherited away. Without this,
    #     'Create and configure applications' claim-greed-killed
    #     'Submit partner application' (its flow rides dev
    #     partner-application, cited by the SPA journey whose own PF is
    #     the twenty-website home).
    #   * A journey whose OWN PF IS a container never container-inherits
    #     a foreign container's member (its own members pass h==pf_key).
    #   * The whole-pool 2b token rescue and the route backfill stay
    #     home-STRICT (the 46-member/13.2% false-pool claim-greed law).
    #   * Armed, a BLOCKED container-homed member is a packaging
    #     reservation, not foreignness — uf_home_filtered keeps counting
    #     ONLY sibling-capability blocks (the metric the class-fix is
    #     judged by). Unset keeps every count byte-identical.
    # Telemetry key exists only when the channel is armed AND the repo
    # has ws-containers, so unset/inert worlds stay byte-identical.
    container_keys: frozenset[str] = (
        container_pf_keys
        if (home_by_mid and container_pf_keys
            and _container_inherit_enabled())
        else frozenset()
    )
    reserved_mids: set[str] = set()
    if container_keys:
        # Telemetry key appears only when the channel actually FIRES
        # (set in _home_ok): a repo whose ws-containers never home a
        # journey member (openstatus notification-slack) must stay
        # byte-identical armed vs unset — the inertness law.
        for spec in uf_specs:
            s_pf = _slug(spec.get("product_feature") or "") or None
            if not s_pf:
                continue
            for dref in spec.get("from_dev_features") or []:
                dev = (dev_by_name.get(dref.strip().lower())
                       if isinstance(dref, str) else None)
                if dev is None:
                    continue
                for mid in dev_flow_ids.get(dev.name, []):
                    if home_by_mid.get(mid) == s_pf:
                        reserved_mids.add(mid)

    def _home_ok(mid: str, pf_key: str | None, *,
                 container_inherit: bool = False) -> bool:
        if not home_by_mid or not pf_key:
            return True
        h = home_by_mid.get(mid)
        if h is None or h == pf_key:
            return True
        if h in container_keys:
            # Armed only (empty set when unset/inert). Packaging home:
            # inheritable by a capability journey on the cited channels
            # unless reserved for its rightful claimant; blocked and
            # UNCOUNTED everywhere else (reservation, not foreignness).
            if (container_inherit and pf_key not in container_keys
                    and mid not in reserved_mids):
                tele["uf_home_container_inherited"] = (
                    tele.get("uf_home_container_inherited", 0) + 1)
                return True
            return False
        tele["uf_home_filtered"] += 1
        return False

    # W4 §4.6 — interior-section citation index: (pf lower, section lower)
    # → hosting page files, plus page → flow-member ids (entry lookup).
    # Citations are VERIFIED against the SAME capability the journey
    # names — a cross-PF citation is invalid and ignored.
    section_pages: dict[tuple[str, str], list[str]] = {}
    page_flow_ids: dict[str, list[str]] = {}
    if interior_evidence:
        for page in sorted((interior_evidence.get("pages") or {})):
            info = (interior_evidence.get("pages") or {})[page] or {}
            pf_low = str(info.get("pf") or "").strip().lower()
            if not pf_low:
                continue
            for s in info.get("sections") or []:
                key_s = (pf_low, str(s).strip().lower())
                pages_list = section_pages.setdefault(key_s, [])
                if page not in pages_list:
                    pages_list.append(page)
        for d in developer_features:
            for fl in getattr(d, "flows", None) or []:
                ep = getattr(fl, "entry_point_file", None)
                mid = _flow_member_id(fl)
                if ep and mid:
                    page_flow_ids.setdefault(ep, []).append(mid)

    # Route-ownership lookup for the memberless-UF backfill (validator I7):
    # pattern → owning dev (routes_index feature_uuid first, route file in the
    # dev's owned paths second; first dev wins — input order is stable).
    dev_by_uuid: dict[str, "Feature"] = {}
    dev_by_path: dict[str, "Feature"] = {}
    for d in developer_features:
        u = getattr(d, "uuid", "") or ""
        if u and u not in dev_by_uuid:
            dev_by_uuid[u] = d
        for p in getattr(d, "paths", None) or []:
            dev_by_path.setdefault(p, d)
    route_owner: dict[str, "Feature"] = {}
    for r in routes_index:
        pat = str(r.get("pattern") or "")
        if not pat or pat in route_owner:
            continue
        owner = (dev_by_uuid.get(str(r.get("feature_uuid") or ""))
                 or dev_by_path.get(str(r.get("file") or "")))
        if owner is not None:
            route_owner[pat] = owner

    def _backfill_members(
        route_patterns: list[str], claimed: set[str],
        pf_key: str | None = None,
    ) -> list[str]:
        """UNCLAIMED flows of the devs owning *route_patterns* — the
        deterministic member backfill for a route-grounded, flow-less UF.
        Home-pure under anchored W4 (same rule as inheritance)."""
        out: list[str] = []
        seen: set[str] = set()
        for pat in route_patterns:
            owner = route_owner.get(pat)
            if owner is None:
                continue
            for mid in dev_flow_ids.get(owner.name, []):
                if (mid not in claimed and mid not in seen
                        and _home_ok(mid, pf_key)):
                    seen.add(mid)
                    out.append(mid)
        return out

    # ── Pass 1 — from_flows inheritance (home-pure under anchored W4) ──
    built: list[tuple[dict[str, Any], "UserFlow"]] = []
    for spec in uf_specs:
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        spec_pf_key = _slug(spec.get("product_feature") or "") or None
        members: list[str] = []
        routes: list[str] = []
        seen_m: set[str] = set()
        for ref in spec.get("from_flows") or []:
            src = old_by_id.get(ref)
            if not src:
                continue
            for mid in src.member_flow_ids:
                if mid not in seen_m and _home_ok(
                        mid, spec_pf_key, container_inherit=True):
                    seen_m.add(mid)
                    members.append(mid)
            routes.extend(src.routes or [])
        built.append((spec, UserFlow(
            id="UF-000",  # provisional — renumbered after the content sort
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
        )))

    # Flows already claimed by ANY inherited membership are off-limits to the
    # grounding passes — a flow keeps exactly one owning journey.
    claimed: set[str] = set()
    for _, uf in built:
        claimed.update(uf.member_flow_ids)

    # ── Pass 2 — ground still-empty UFs (cited devs → token rescue → routes) ─
    out: list["UserFlow"] = []
    # Collision rule (2026-07-04): at most ONE otherwise-empty journey may be
    # route-rescued onto the SAME route set. N journeys grounded on one route
    # are N-1 phantoms (inbox-zero: 15 zero-member journeys all citing
    # /api/user/group/:groupId/items) — the first (emission order, hence
    # deterministic) keeps the grounding, the rest fall through to the
    # ungrounded drop.
    rescued_route_sets: set[frozenset[str]] = set()
    for spec, uf in built:
        spec_pf_key = uf.product_feature_id
        if not uf.member_flow_ids:
            utok = _content_tokens(uf.name, uf.resource)
            attached: list[str] = []
            seen_a: set[str] = set()  # dedup — two cited devs may share a flow
            # 2a. cited dev features: their unclaimed, content-overlapping flows.
            for dref in spec.get("from_dev_features") or []:
                dev = dev_by_name.get(dref.strip().lower()) if isinstance(dref, str) else None
                if dev is None:
                    continue
                for mid in dev_flow_ids.get(dev.name, []):
                    if (mid not in claimed and mid not in seen_a
                            and (flow_tokens.get(mid) or set()) & utok
                            and _home_ok(mid, spec_pf_key,
                                         container_inherit=True)):
                        seen_a.add(mid)
                        attached.append(mid)
            if attached:
                tele["uf_dev_grounded"] += 1
            elif section_pages and (spec.get("from_sections") or []):
                # 2a-bis (W4 §4.6). VERIFIED interior-section citations:
                # attach the unclaimed flows whose ENTRY file is a page
                # hosting a cited section of the SAME capability the
                # journey names — entry+interior evidence within ONE
                # PF's subtree, deterministically checkable.
                pf_low = str(spec.get("product_feature") or "").strip().lower()
                for sref in spec.get("from_sections") or []:
                    if not isinstance(sref, str):
                        continue
                    hosted = section_pages.get((pf_low, sref.strip().lower()))
                    if hosted is None:
                        tele["uf_sections_invalid"] += 1
                        continue
                    tele["uf_sections_cited"] += 1
                    for page in hosted:
                        for mid in page_flow_ids.get(page, []):
                            if mid not in claimed and mid not in seen_a:
                                seen_a.add(mid)
                                attached.append(mid)
                if attached:
                    tele["uf_section_grounded"] += 1
            if not attached:
                # 2b. rescue: resource/token match over ALL unclaimed flows.
                res = uf.resource
                if len(res) > 3 and res.endswith("s"):
                    res = res[:-1]
                for mid, ftok in flow_tokens.items():
                    if mid in claimed or not _home_ok(mid, spec_pf_key):
                        continue
                    if (res and res in ftok) or len(utok & ftok) >= 2:
                        attached.append(mid)
                if attached:
                    tele["uf_rescued_flows"] += 1
            if attached:
                uf.member_flow_ids = attached
                uf.member_count = len(attached)
                claimed.update(attached)
        if not uf.member_flow_ids and not uf.routes:
            # 2c. route grounding — a journey may be real yet flow-less
            # (route detected, flow-walk missed it).
            route_hits = _route_patterns_for(uf.resource)
            if route_hits:
                route_set = frozenset(route_hits)
                if route_set in rescued_route_sets:
                    # Phantom: this route set already grounds another empty
                    # journey. Leave uf.routes empty → the drop branch below
                    # removes it as ungrounded.
                    tele["uf_rescue_dropped_collisions"] += 1
                else:
                    rescued_route_sets.add(route_set)
                    uf.routes = route_hits
                    tele["uf_rescued_routes"] += 1
        if not uf.member_flow_ids and uf.routes:
            # Route-grounded but flow-less. The old emission gate allowed this
            # ("a journey may be real yet flow-less") — validator I7 (operator,
            # 2026-07-05) forbids a memberless UF: backfill deterministically
            # from the route-owning devs' unclaimed flows, else DROP (Soc0
            # "Look up users by email": the owner's flows were all claimed by
            # another journey — emitting a memberless twin is dup inflation).
            fill = _backfill_members(uf.routes, claimed,
                                      pf_key=spec_pf_key)
            if fill:
                uf.member_flow_ids = fill
                uf.member_count = len(fill)
                claimed.update(fill)
                tele["uf_route_member_backfill"] += 1
            else:
                tele["uf_dropped_memberless"] += 1
                if len(tele["uf_dropped_names"]) < 30:  # scan_meta size bound
                    tele["uf_dropped_names"].append(uf.name)
                continue
        if not uf.member_flow_ids and not uf.routes:
            # Still 0-LOC → drop. Never emit a code-less journey name.
            tele["uf_dropped_ungrounded"] += 1
            if len(tele["uf_dropped_names"]) < 30:  # scan_meta size bound
                tele["uf_dropped_names"].append(uf.name)
            continue
        out.append(uf)
    return out, tele


def _fallback_capability(
    dev: "Feature", cap_tokens: dict[str, set[str]],
) -> tuple[str, bool]:
    """Capability for a dev feature Call 2 OMITTED from the map.

    A workspace anchor / bare code-structure container genuinely belongs in
    the platform residual. Any OTHER omitted dev feature is a mapping miss —
    dumping it into the residual is what inflated the shared-platform blob to
    48-78% of repo files (2026-07-01 audit) — so try a deterministic
    content-token match against the emitted capabilities first. No match →
    residual (conservation still holds). Returns ``(capability, rescued)``.
    """
    from faultline.pipeline_v2.stage_8_7_anchor_desink import _is_workspace_anchor

    nm = dev.display_name or dev.name or ""
    if _slug(nm) in _STRUCTURE_LEAK_SLUGS or _is_workspace_anchor(dev):
        return _RESIDUAL_CAP, False
    dtok = _content_tokens(nm)
    best: str | None = None
    best_score: tuple[int, float] = (0, 0.0)
    for cap in sorted(cap_tokens):  # deterministic final tie-break
        ctok = cap_tokens[cap]
        n = len(dtok & ctok)
        if not n:
            continue
        # Prefer more shared tokens, then the more SPECIFIC capability (higher
        # share of ITS tokens matched) — "account-billing" goes to "Billing"
        # (1/1) over "Account Management" (1/2).
        score = (n, n / len(ctok) if ctok else 0.0)
        if score > best_score:
            best, best_score = cap, score
    if best is not None:
        return best, True
    return _RESIDUAL_CAP, False


# ── Residual-confirmation guard (deterministic, $0) ────────────────────────
# Call 2 EXPLICITLY assigning a dev feature to "Shared Platform" was trusted
# blindly — on Soc0 (2026-07-05 audit) Haiku sent 60/155 devs there, including
# `edr` while its OWN Call-1 output contained an "EDR Integrations" capability,
# and whole feature-dir domains (frontend/src/{features,modules}/<domain>) like
# anomalies / network-security. The guard treats an explicit residual claim on
# a NON-container dev as unproven and tries two structural confirmations:
#   tier 1 — STRONG token match: the dev name's content tokens (minus public
#            vendor tokens, which name the connector INSTANCE while caps name
#            the FAMILY) are fully covered by one capability's tokens;
#   tier 2 — feature-dir promotion: the majority of the dev's owned paths sit
#            under `<features|modules>/<domain>/` where <domain> names the dev
#            itself — the author-declared React feature-folder convention —
#            so the dev IS a product domain: promote it to its own capability.
# No match → residual stands (honest platform bucket). Kill-switch:
# ``FAULTLINE_STAGE_6_7D_RESIDUAL_GUARD=0`` (default ON inside the opt-in
# 6.7d stage). Runs at reconstruction time → applies identically to live and
# cache-hit replays.
_FEATURE_DIR_CONTAINERS = frozenset(
    # React feature-folder conventions AND the backend service convention: a dev
    # majority-owning ``<container>/<its-own-name>/`` IS that product domain.
    # Mirrors the stage-8.9.6 carve container set so a carve-minted domain dev
    # promotes to its own capability instead of falling to the shared bucket.
    {"features", "feature", "modules", "module", "services", "service"})


def _residual_guard_enabled() -> bool:
    return os.environ.get("FAULTLINE_STAGE_6_7D_RESIDUAL_GUARD", "1") != "0"


def _kebab(name: str) -> str:
    """camelCase/space/slash → kebab slug (dir↔name comparison tier)."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name or "")
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _kebab_singular(slug: str) -> str:
    """Crude per-token singularisation of a kebab slug — same convention as
    stage 8.9.6 / the uf-scorer tokenisers."""
    return "-".join(
        t[:-1] if len(t) > 3 and t.endswith("s") else t
        for t in slug.split("-")
    )


# ── Iteration-5 grain surgery: token-family stemming + join-over-mint ────────
# The residual-guard tier-2/tier-3 promotions minted THIN DUPLICATE SHELLS on
# Soc0 (2026-07-05): a flowless `detection-studio` dev under
# ``modules/detection-studio`` minted its own "Detection Studio" product
# feature while the SAME product area already lived in "Custom Detector
# Builder" (detectors-page + detector-detail-page + api-detectors). Fix 1
# (JOIN-OVER-MINT) folds such a dev into the token-FAMILY capability that
# already exists instead of minting a duplicate; fix 2 (FLOWFUL REQUIREMENT)
# forbids a flowless dev from minting a standalone PF at all. Both are
# structural + scale-invariant (rule-no-magic-tuning / rule-no-repo-specific-
# paths): a light derivational stemmer folds ``detection`` / ``detector`` /
# ``detect`` to one stem, and the join target is chosen by the LARGEST
# established family home (gravitational-mass tie-break), not by any repo list.
_MIN_STEM_LEN = 4
# Longest-first: strip ONE derivational suffix past the singular form, keeping
# a stem of at least _MIN_STEM_LEN so short/ambiguous stems never collapse.
_STEM_SUFFIXES: tuple[str, ...] = (
    "ization", "isation", "ations", "ation", "ition", "ision", "ements",
    "ement", "ings", "ing", "ion", "ors", "ers", "ment", "ies", "or", "er",
)
# Generic container/surface tokens that must NEVER drive a family join (they
# name a page/app shell, not a product domain). Mirrors validator I11's
# container-page vocabulary + the 8.9.6 generic-domain class. A join on "page"
# would wrongly fold "home-page" into "detections-page".
_FAMILY_GENERIC_TOKENS = frozenset({
    "page", "home", "landing", "index", "main", "root", "app", "apps",
    "ui", "ux", "api", "web", "site", "portal", "dashboard", "console",
    "screen", "view", "panel", "tab", "layout", "shell",
})


def _stem(token: str) -> str:
    """Light dependency-free derivational stem for family folding.

    Singularises first (reusing the naming_validator convention, safe on
    ``status``/``analysis``), then strips ONE derivational suffix while the
    remaining stem stays >= _MIN_STEM_LEN: ``detection``→``detect``,
    ``detector``→``detect``, ``suggestions``→``suggest``. Idempotent on stems
    that carry no suffix (``studio``→``studio``)."""
    from faultline.pipeline_v2.naming_validator import _singular

    t = _singular(token.lower())
    for suf in _STEM_SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= _MIN_STEM_LEN:
            return t[: -len(suf)]
    return t


def _family_stems(name: str | None) -> set[str]:
    """Discriminative family stems of a produced name — content tokens
    (``_content_tokens`` already drops generic verbs/glue), minus the generic
    container/surface tokens, minus public vendor tokens (which name the
    connector INSTANCE, not the product family), stemmed."""
    from faultline.pipeline_v2.naming_validator import VENDOR_TOKENS

    out: set[str] = set()
    for t in _content_tokens(name):
        if t in _FAMILY_GENERIC_TOKENS or t in VENDOR_TOKENS:
            continue
        s = _stem(t)
        if len(s) >= _MIN_STEM_LEN:
            out.add(s)
    return out


def _family_capability_match(
    dev: "Feature",
    cap_context: dict[str, dict[str, Any]],
) -> str | None:
    """Existing capability whose token FAMILY the dev shares — the JOIN target
    for a would-be thin-shell mint (fix 1). ``cap_context`` maps each candidate
    capability display-name to ``{stems, members, flows, paths}`` (the family
    stems of its NAME + established gravitational mass from its already-assigned
    member devs). Returns the best join target, or ``None`` when the dev shares
    no discriminative family stem with any OTHER capability.

    Selection is structural and deterministic: prefer the capability sharing
    the MOST family stems, then the LARGEST established home by BEHAVIOURAL mass
    — member flows first (journeys are the product weight of a capability), then
    member devs, then owned paths — then the alphabetically-first slug. A
    residual dev joins the family's biggest existing home, never a repo-named
    target. A capability whose slug equals the dev's own slug (its self-mint)
    is never a join target."""
    dev_stems = _family_stems(dev.display_name or dev.name)
    if not dev_stems:
        return None
    dev_slug = _slug(dev.display_name or dev.name or "")
    best: str | None = None
    best_key: tuple[int, int, int, int, str] | None = None
    for cap, ctx in cap_context.items():
        if _slug(cap) == dev_slug:
            continue  # never join a dev to its own self-capability
        overlap = dev_stems & ctx["stems"]
        if not overlap:
            continue
        # Descending preference; alpha slug ASCENDING → keep slug for a stable
        # final tie-break. Flows rank above member count: a capability's
        # journeys are its behavioural mass (a 62-flow detector home outranks a
        # thinner 8-member one).
        key = (len(overlap), ctx["flows"], ctx["members"], ctx["paths"])
        cand_key = (*key, _slug(cap))
        if best_key is None or key > best_key[:4] or (
            key == best_key[:4] and _slug(cap) < best_key[4]
        ):
            best, best_key = cap, cand_key
    return best


# ── Container-page guard (fix 3) ─────────────────────────────────────────────
# A page container is a SURFACE that HOSTS features, never a feature itself
# (operator 2026-07-05: "хом пейдж фічою апріорі бути не може"). The tier-3
# route-surface promotion minted "Home Page" from a `home-page` dev that owns
# the landing route + 5 flows (inline-suggestions / ghost-text / knowledge-
# chips) — but those flows belong to their OWN capabilities, and the page
# skeleton belongs to the app shell. Structural identity check mirrors
# validator I11's regex on the kebab slug.
_CONTAINER_PAGE_SLUG_RE = re.compile(
    r"^(home|landing|index|main|root)(-page)?$"
)


def _is_container_page(dev: "Feature") -> bool:
    """True when the dev's IDENTITY is a page container (name is one of the
    home/landing/index/main/root page classes) — structural, mirrors the
    validator I11 container-page regex on the dev's kebab slug.

    Product-Spine §4.2 (Wave 2a, consequence c): the surface-taxonomy
    ``shell`` tag is consumed FIRST — Stage 6.85 stamps it from the
    YAML-documented shell vocabulary + (home)-group path evidence, so shell
    containers never name anything. The slug regex stays as the fallback so
    the guard keeps working when the taxonomy stage is disabled
    (FAULTLINE_SURFACE_TAXONOMY=0 → tag absent → regex decides, exactly the
    pre-W2a behavior)."""
    if getattr(dev, "surface_scope", None) == "shell":
        return True
    for raw in (dev.name, getattr(dev, "display_name", None)):
        if raw and _CONTAINER_PAGE_SLUG_RE.match(_kebab(raw)):
            return True
    return False


def _strong_capability_match(
    dev: "Feature", cap_tokens: dict[str, set[str]],
) -> str | None:
    """Capability whose content tokens FULLY COVER the dev feature's — a
    subset match, strictly stronger than the 1-shared-token rescue (which
    misroutes: 'network-security' → 'AI Security Chat Assistant' on the lone
    'security' token). Vendor tokens are excluded from the dev side when
    non-vendor tokens remain ('edr-crowdstrike' matches 'EDR Integrations')."""
    from faultline.pipeline_v2.naming_validator import VENDOR_TOKENS

    dtok = _content_tokens(dev.display_name or dev.name)
    if not dtok:
        return None
    core = {t for t in dtok if t not in VENDOR_TOKENS and f"{t}s" not in VENDOR_TOKENS}
    if not core:
        core = dtok  # pure-vendor name ('teams') — match on the vendor itself
    best: str | None = None
    best_score: tuple[int, float] = (0, 0.0)
    for cap in sorted(cap_tokens):  # deterministic tie-break
        ctok = cap_tokens[cap]
        if not ctok or not core <= ctok:
            continue
        # Prefer covering MORE tokens, then the more specific capability.
        score = (len(core), len(core) / len(ctok))
        if score > best_score:
            best, best_score = cap, score
    return best


def _feature_dir_capability(dev: "Feature") -> str | None:
    """Own-capability display name when the dev's owned footprint majority-
    sits under ``<features|modules>/<domain>/`` and <domain> names the dev
    itself (exact or crude-singular kebab match). Structural, scale-invariant:
    no repo paths, majority ratio, generic domain tokens skipped."""
    from faultline.pipeline_v2.stage_8_9_6_domain_member_attribution import (
        _GENERIC_DOMAIN_SKIP,
    )

    paths = list(getattr(dev, "paths", None) or [])
    if not paths:
        return None
    slugs = {_kebab(dev.name or ""), _kebab(dev.display_name or "")} - {""}
    slugs |= {_kebab_singular(s) for s in set(slugs)}
    hits = 0
    for p in paths:
        segs = p.split("/")
        for i in range(len(segs) - 2):  # container + domain must both be dirs
            if segs[i].lower() not in _FEATURE_DIR_CONTAINERS:
                continue
            dom = _kebab(segs[i + 1])
            if (
                dom
                and dom not in _GENERIC_DOMAIN_SKIP
                and dom not in _STRUCTURE_LEAK_SLUGS
                and (dom in slugs or _kebab_singular(dom) in slugs)
            ):
                hits += 1
            break  # first container segment decides
    if hits * 2 <= len(paths):  # strict majority — tolerate stray helpers
        return None
    return _titleize(dev.display_name or dev.name or "")


def _titleize(base: str) -> str | None:
    """Titleize kebab/snake dev names so a promoted capability reads like the
    LLM-emitted ones ("network-security" → "Network Security")."""
    words = re.split(r"[-_\s]+", base.strip())
    return " ".join(w if (w.isupper() and len(w) > 1) else w.capitalize()
                    for w in words if w) or None


def _route_surface_capability(
    dev: "Feature", route_files: frozenset[str], route_uuids: frozenset[str],
) -> str | None:
    """Tier 3 — own-capability display name for a flowful residual dev that
    owns a ROUTE SURFACE: any ``routes_index`` entry is attributed to it
    (``feature_uuid``) or one of its owned paths IS a route file. A dev with
    user-visible flows AND an HTTP/page routing surface is a product surface,
    not platform glue — the operator's resettle rule ("краще нова PF, ніж
    агрегатор", 2026-07-05). Structural only: infra anchors (workspace-anchor
    markers, structure-leak slugs incl. main/mock/scripts classes) never reach
    this helper — the caller excludes them first."""
    if not (getattr(dev, "flows", None) or []):
        return None
    owns = (getattr(dev, "uuid", "") or "") in route_uuids or any(
        p in route_files for p in (getattr(dev, "paths", None) or []))
    if not owns:
        return None
    return _titleize(dev.display_name or dev.name or "")


def _flows_surface_description(dev: "Feature") -> str:
    """Deterministic description for a tier-3 minted capability — grounded in
    the dev's own flow names (the promotion evidence), bounded for output."""
    names: list[str] = []
    for fl in getattr(dev, "flows", None) or []:
        nm = getattr(fl, "display_name", None) or getattr(fl, "name", None)
        if nm and nm not in names:
            names.append(nm)
    return _short(
        "Route-owning product surface (resettled from the shared platform "
        "bucket). Flows: " + ", ".join(names), 240,
    )


def _confirm_residual(
    dev: "Feature",
    cap_tokens: dict[str, set[str]],
    pf_tele: dict[str, Any],
    route_files: frozenset[str] = frozenset(),
    route_uuids: frozenset[str] = frozenset(),
    minted_descs: dict[str, str] | None = None,
    cap_context: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Confirmed capability for a dev feature Call 2 EXPLICITLY sent to the
    residual — see the guard rationale above. Returns the residual unchanged
    for genuine platform containers.

    Iteration-5 grain order (join over mint, flowful requirement):
      1. structure-leak / workspace-anchor  -> residual
      2. STRONG token-subset match          -> JOIN existing capability
      3. token-FAMILY match (fix 1)         -> JOIN existing capability
      4. tier-2 feature-dir / tier-3 route-surface MINT — ONLY when the dev
         has >= 1 flow (fix 2: a flowless dev never mints a standalone PF —
         it joins a family above or stays in the explainable residual)
      5. residual
    """
    from faultline.pipeline_v2.stage_8_7_anchor_desink import _is_workspace_anchor

    def _promote(promo: str) -> str:
        pf_tele["devs_residual_promoted"] += 1
        # Record the minted capability NAME too (bounded): the PF-UF
        # backstop tags journeys it synthesizes for these caps with the
        # "promoted_capability_backstop" reason — a promotion happens
        # AFTER the draw assigned journeys, so it is structurally UF-less.
        names = pf_tele.setdefault("promoted_cap_names", [])
        if promo not in names and len(names) < 50:
            names.append(promo)
        return promo

    nm = dev.display_name or dev.name or ""
    if _slug(nm) in _STRUCTURE_LEAK_SLUGS or _is_workspace_anchor(dev):
        return _RESIDUAL_CAP
    # Product-Spine §4.2 (Wave 2a, consequence a): a NON-PRODUCT surface dev
    # (marketing / docs / legal / dev_tooling per the Stage 6.85 tag) never
    # mints a capability (tier-2/tier-3) and never token-joins a PRODUCT
    # capability — a marketing-site blog dev folding into a product "Blog
    # Publishing" capability is exactly the C3 pollution. It stays in the
    # residual; the emission lane then re-binds it to its non-product
    # surface row. Tag absent (taxonomy off) → no-op.
    from faultline.pipeline_v2.surface_taxonomy import is_non_product_dev
    if is_non_product_dev(dev):
        pf_tele["devs_residual_non_product"] = (
            pf_tele.get("devs_residual_non_product", 0) + 1
        )
        return _RESIDUAL_CAP
    strong = _strong_capability_match(dev, cap_tokens)
    if strong is not None:
        pf_tele["devs_residual_rescued_strong"] += 1
        return strong
    # fix 1 — JOIN-OVER-MINT: fold a would-be thin-shell mint into the existing
    # token-family capability (its biggest established home) instead of minting
    # a duplicate. Runs BEFORE both tier-2 and tier-3 mints.
    if cap_context:
        fam = _family_capability_match(dev, cap_context)
        if fam is not None:
            pf_tele["devs_residual_family_joined"] = (
                pf_tele.get("devs_residual_family_joined", 0) + 1
            )
            return fam
    # fix 2 — FLOWFUL REQUIREMENT: a flowless dev never mints a standalone PF.
    if not (getattr(dev, "flows", None) or []):
        return _RESIDUAL_CAP
    promo = _feature_dir_capability(dev)
    if promo is not None:
        return _promote(promo)
    # tier 3 — route-surface promotion: a flowful dev owning routes / a route
    # file is a user-facing surface; resettle it as its own capability rather
    # than aggregating it (validator I9). Description grounded in its flows.
    promo3 = _route_surface_capability(dev, route_files, route_uuids)
    if promo3 is not None:
        if minted_descs is not None:
            minted_descs.setdefault(promo3, _flows_surface_description(dev))
        return _promote(promo3)
    return _RESIDUAL_CAP


def _container_page_guard_enabled() -> bool:
    """Default ON inside the opt-in 6.7d stage —
    ``FAULTLINE_STAGE_6_7D_CONTAINER_GUARD=0`` disables the container-page
    guard (fix 3)."""
    return os.environ.get("FAULTLINE_STAGE_6_7D_CONTAINER_GUARD", "1") != "0"


def _dev_dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _build_cap_context(
    cap_to_devs: dict[str, list["Feature"]],
) -> dict[str, dict[str, Any]]:
    """Family-join context per capability: its NAME family stems + the
    established gravitational mass (member devs / flows / owned paths) from the
    direct (phase-1) assignments. The residual bucket is never a join target."""
    ctx: dict[str, dict[str, Any]] = {}
    for cap, devs in cap_to_devs.items():
        if cap == _RESIDUAL_CAP:
            continue
        ctx[cap] = {
            "stems": _family_stems(cap),
            "members": len(devs),
            "flows": sum(len(getattr(d, "flows", None) or []) for d in devs),
            "paths": sum(len(_paths_of(d)) for d in devs),
        }
    return ctx


def _redistribute_container_flows(
    container_devs: list["Feature"],
    other_devs: list["Feature"],
) -> dict[str, str]:
    """Map each flow owned by a container-page dev to the NON-container
    developer feature that owns the flow's home directory (fix 3).

    A page container HOSTS features — its flows (``inline-suggestions`` /
    ``ghost-text`` / ``knowledge-chips``) belong to those features, whose devs
    own sibling files in the same ``features/<domain>/`` directory. Returns
    ``{flow_member_id: owning_dev_name}``. Structural + deterministic: pick the
    non-container dev owning the MOST files in the flow's directories, then the
    alphabetically-first name."""
    dir_owners: dict[str, Counter[str]] = defaultdict(Counter)
    for d in other_devs:
        for p in _paths_of(d):
            dir_owners[_dev_dir(p)][d.name] += 1
    override: dict[str, str] = {}
    for cd in container_devs:
        for fl in getattr(cd, "flows", None) or []:
            mid = _flow_member_id(fl)
            if not mid or mid in override:
                continue
            dirs: list[str] = []
            ep = getattr(fl, "entry_point_file", None)
            if ep:
                dirs.append(_dev_dir(ep))
            for p in getattr(fl, "paths", None) or []:
                dirs.append(_dev_dir(p))
            tally: Counter[str] = Counter()
            for dr in dirs:
                for dev_name, cnt in dir_owners.get(dr, {}).items():
                    tally[dev_name] += cnt
            if not tally:
                continue
            best = max(tally.values())
            override[mid] = min(n for n, c in tally.items() if c == best)
    return override


def _entry_owner_overrides(
    developer_features: list["Feature"],
    dev_to_product: dict[str, tuple[str, ...]],
) -> dict[str, str]:
    """``{flow member-id → PF slug of the flow's ENTRY-FILE owner}`` for
    flows held by one dev whose entry file is PRIMARY-owned by another
    (Wave 2b, hub amendment: per-vendor journeys follow the vendor dev
    even when the flow object rides the plumbing parent). Same ruler as
    validator I16 (entry-owner consistency). Deterministic: primary
    ownership is exclusive by construction; only cross-dev, cross-PF
    entries emit an override."""
    from faultline.pipeline_v2.spine_anchors import owned_paths_of

    owner_of_file: dict[str, str] = {}
    for d in sorted(developer_features, key=lambda x: getattr(x, "name", "") or ""):
        for p in owned_paths_of(d):
            owner_of_file.setdefault(p, d.name)
    out: dict[str, str] = {}
    for d in developer_features:
        holder_slugs = dev_to_product.get(d.name) or ()
        holder = holder_slugs[0] if holder_slugs else ""
        for fl in getattr(d, "flows", None) or []:
            ep = getattr(fl, "entry_point_file", None)
            if not ep:
                continue
            owner = owner_of_file.get(str(ep))
            if owner is None or owner == d.name:
                continue
            owner_slugs = dev_to_product.get(owner) or ()
            if not owner_slugs or owner_slugs[0] == holder:
                continue
            mid = _flow_member_id(fl)
            if mid and mid not in out:
                out[mid] = owner_slugs[0]
    return out


def _build_product_features(
    pf_specs: list[dict[str, Any]],
    dev_map: dict[str, str],
    developer_features: list["Feature"],
    routes_index: list[dict[str, Any]] | None = None,
    anchored: bool = False,
    anchored_pf_by_name: dict[str, "Feature"] | None = None,
) -> tuple[list["Feature"], dict[str, tuple[str, ...]], int, dict[str, Any],
           dict[str, str]]:
    """Aggregate dev features into the abstracted capabilities. Returns
    (product_features, dev_to_product_map, files_attributed, pf_telemetry,
    flow_owner_override). ``flow_owner_override`` maps a container-page dev's
    flow member-ids to the product-feature slug of the flow's real owner
    (fix 3) — the PF-UF backstop / shared-UF reassignment honour it so a
    hosted flow's journey follows its feature, not the page shell.

    ``anchored`` (Wave 2b): the map is the mint's lineage — TOTAL over
    lineage devs. Unmapped devs are LANE RESIDENTS (platform
    infrastructure): they are skipped outright — no token rescue, no
    residual guard, no Shared Platform row (the name-stem repair ladders
    are exactly the RC3 membership channel the spine retires). The
    container-page guard is also upstream now (the mint consumed the
    shell tags), so it is bypassed."""
    desc_by_cap = {
        (s.get("name") or "").strip(): _short(s.get("description"), 240)
        for s in pf_specs if (s.get("name") or "").strip()
    }
    cap_tokens = {cap: _content_tokens(cap) for cap in desc_by_cap}
    # Route-surface ownership signals for the tier-3 residual promotion —
    # structural, straight from the deterministic routes_index.
    route_files = frozenset(
        str(r.get("file") or "") for r in (routes_index or [])) - {""}
    route_uuids = frozenset(
        str(r.get("feature_uuid") or "") for r in (routes_index or [])) - {""}
    minted_descs: dict[str, str] = {}

    # Group dev features by their mapped capability. Omitted devs are token-
    # matched to an emitted capability when possible; only true platform
    # containers and unmatchable devs land in the (marked) residual.
    pf_tele: dict[str, Any] = {
        "devs_token_rescued": 0, "devs_residual": 0,
        "devs_residual_rescued_strong": 0, "devs_residual_promoted": 0,
    }
    cap_to_devs: dict[str, list["Feature"]] = defaultdict(list)
    dev_to_product: dict[str, tuple[str, ...]] = {}
    residual_guard = _residual_guard_enabled() and not anchored
    container_guard = _container_page_guard_enabled() and not anchored

    def _assign(dev: "Feature", cap: str) -> None:
        if cap == _RESIDUAL_CAP:
            pf_tele["devs_residual"] += 1
        cap_to_devs[cap].append(dev)
        dev_to_product[dev.name] = (_slug(cap),)

    # Phase 1 — direct (non-residual, non-container) assignments establish the
    # family-join context (the gravitational mass a would-be thin-shell mint
    # folds into). Container-page devs and explicit-residual / unmapped devs
    # are deferred so fix 1's family match and fix 3's redistribution see the
    # full set of real capabilities.
    deferred: list[tuple["Feature", str | None]] = []
    container_devs: list["Feature"] = []
    # Product-Spine §4.1 — concern facets never join a capability: not
    # assigned, not token-rescued, not residual (their product_feature_id
    # stays None and their paths never enter any PF union).
    from faultline.pipeline_v2.spine_hygiene import is_facet as _is_facet_dev

    for dev in developer_features:
        if _is_facet_dev(dev):
            pf_tele["devs_facet_excluded"] = (
                pf_tele.get("devs_facet_excluded", 0) + 1)
            continue
        nm = dev.display_name or dev.name
        cap = dev_map.get(nm)
        if container_guard and _is_container_page(dev):
            container_devs.append(dev)  # skeleton -> residual in phase 3
            continue
        if cap and cap != _RESIDUAL_CAP:
            _assign(dev, cap)
        elif anchored:
            # Lane resident (platform infrastructure) — the mint already
            # decided: pfid stays None, reason stamped upstream; never a
            # rescue target, never a Shared Platform row.
            pf_tele["devs_lane_residents"] = (
                pf_tele.get("devs_lane_residents", 0) + 1)
        else:
            deferred.append((dev, cap))

    cap_context = _build_cap_context(cap_to_devs)

    # Phase 2 — resolve deferred devs with the family context available.
    for dev, cap in deferred:
        if not cap:
            cap, rescued = _fallback_capability(dev, cap_tokens)
            if rescued:
                pf_tele["devs_token_rescued"] += 1
            elif cap == _RESIDUAL_CAP and residual_guard:
                # An UNMAPPED dev the LLM never saw (a carve-minted domain
                # subsystem, a late split) deserves the full residual-guard
                # treatment — family join / flowful feature-dir or route-surface
                # promotion — so it resettles instead of sinking into the shared
                # bucket (validator I9).
                cap = _confirm_residual(dev, cap_tokens, pf_tele,
                                        route_files, route_uuids, minted_descs,
                                        cap_context)
        elif cap == _RESIDUAL_CAP and residual_guard:
            # An EXPLICIT residual assignment is unproven — structural
            # confirmation or re-route (strong token match / token-family
            # join / flowful feature-dir or route-surface promotion). See the
            # guard rationale above _confirm_residual.
            cap = _confirm_residual(dev, cap_tokens, pf_tele,
                                    route_files, route_uuids, minted_descs,
                                    cap_context)
        _assign(dev, cap)

    # Phase 3 — container-page guard (fix 3): the page skeleton joins the
    # residual (app-shell), and each hosted flow's product ownership follows
    # the non-container dev owning its home directory.
    flow_owner_override: dict[str, str] = {}
    if container_devs:
        container_ids = {id(d) for d in container_devs}
        non_container = [d for d in developer_features
                         if id(d) not in container_ids]
        mid_to_owner = _redistribute_container_flows(container_devs,
                                                     non_container)
        for dev in container_devs:
            _assign(dev, _RESIDUAL_CAP)
        residual_slug = _slug(_RESIDUAL_CAP)
        for mid, owner_name in mid_to_owner.items():
            slugs = dev_to_product.get(owner_name)
            if slugs and slugs[0] != residual_slug:
                flow_owner_override[mid] = slugs[0]
        pf_tele["container_pages_guarded"] = len(container_devs)
        pf_tele["container_flows_redistributed"] = len(flow_owner_override)

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
        # The residual ALWAYS carries the platform marker description — even
        # if the LLM echoed "Shared Platform" as a capability with its own
        # blurb — so downstream blob metrics recognise it as a platform
        # bucket, never as the top product feature.
        desc = (_RESIDUAL_DESCRIPTION if cap == _RESIDUAL_CAP
                else desc_by_cap.get(cap) or minted_descs.get(cap, ""))
        feat = aggregate_product_feature(
            name=_slug(cap), display_name=cap,
            description=desc, contrib=contrib,
        )
        if anchored and anchored_pf_by_name:
            _src_pf = anchored_pf_by_name.get(cap)
            if _src_pf is not None:
                feat.anchor_id = getattr(_src_pf, "anchor_id", None)
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
        if cap == _RESIDUAL_CAP:
            pf_tele["residual_files"] = (len(merged_mf) if merged_mf
                                         else len(feat.paths))
        out.append(feat)
    return out, dev_to_product, files_attributed, pf_tele, flow_owner_override


# ── PF-UF backstop (deterministic, $0) ──────────────────────────────────────
# Operator invariant (2026-07-05, CRITICAL): a product feature whose member
# devs own >= 1 flow but which NO user flow references ("фіча без
# юзер-фловів", validator I8) must never ship. Two subclasses on Soc0:
#   (A) residual-guard tier-2 PROMOTIONS — the capability is minted at
#       reconstruction time, AFTER the draw assigned journeys → structurally
#       UF-less;
#   (B) draw coverage gaps — the model emitted the capability but attached
#       every journey elsewhere.
# Mechanism (runs inside ``_finish`` → applies identically to live and
# cache-hit replays):
#   1. REASSIGN journeys whose member flows are MAJORITY-owned by the
#      uncovered PF's devs (donor keeps >= 1 journey — never trade one I8
#      violation for another);
#   2. else SYNTHESIZE one THIN journey from the PF's highest-LOC flows —
#      the FAULTLINE_SEED_SYSTEM_UFS output-only precedent: appended to
#      user_flows[], flow graph untouched. Tagged ``synthesized: true`` +
#      ``synthesis_reason`` so eval / the surfaced tier can EXCLUDE them
#      (board completeness, never silent recall inflation).
# Kill-switch: FAULTLINE_STAGE_6_7D_PF_UF_BACKSTOP=0 (default ON inside the
# opt-in 6.7d stage; registered in scan_result_cache.ENV_OUTPUT_FLAGS).
_BACKSTOP_MEMBER_CAP = 8
_REASON_PROMOTED = "promoted_capability_backstop"
_REASON_UNCOVERED = "uncovered_product_feature_backstop"

#: W5.1 LOC-worthy arm — a FLOWLESS product feature with >= this much owned
#: LOC is "journey-worthy" and must be referenced by a UF (validator I8's
#: ``pf_loc >= 1000`` bar, mirrored EXACTLY — a contract constant, not a
#: tuned knob). Excavation mints such 0-flow surfaces (supabase Settings
#: 27.5K, Query Performance…); the flow-ful backstop arm below cannot seed
#: them (no member flows). They get a member-LESS system-seed instead — the
#: sole I7-exempt shape for a flowless surface (validator D9-carve).
_LOC_WORTHY_MIN = 1000


def _pf_uf_backstop_enabled() -> bool:
    return os.environ.get("FAULTLINE_STAGE_6_7D_PF_UF_BACKSTOP", "1") != "0"


def _loc_worthy_backstop_enabled() -> bool:
    """W5.1 LOC-worthy member-less arm (default ON; ``=0`` restores the
    flow-only backstop). Only fires once ``pf.loc`` is populated (Stage
    6.97), so pre-6.97 backstop calls stay byte-identical."""
    return os.environ.get("FAULTLINE_LOC_WORTHY_BACKSTOP", "1") != "0"


BACKSTOP_OWNED_COVER_ENV = "FAULTLINE_BACKSTOP_OWNED_COVER"


def backstop_owned_cover_enabled() -> bool:
    """B13 (default ON; ``FAULTLINE_BACKSTOP_OWNED_COVER=0`` restores the
    byte-identical pre-B13 backstop). When ON, the synthesize arm bundles
    ONLY flows whose ENTRY-OWNER is the covered PF (the validator's I16
    ruler, via :func:`_flow_home_map`) — a PF with no own-entry flow gets a
    member-less coverage seed instead of a majority-foreign bundle — and
    every member-less I8-cover seed is stamped an honest coverage marker
    (``synth_quality.honest_coverage_markers``)."""
    return os.environ.get(BACKSTOP_OWNED_COVER_ENV, "1").strip().lower() \
        not in {"0", "false"}


def _feature_loc_attr(f: Any) -> int:
    """Owned LOC of an in-memory Feature — mirrors the validator's
    ``feature_loc`` file-based arm (``loc``/``total_loc``/``loc_files``).
    0 until Stage 6.97 stamps ``loc``."""
    for k in ("loc", "total_loc", "loc_files"):
        v = getattr(f, k, None)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return 0


def _flow_loc_of(flow: Any) -> int:
    """Deterministic LOC of a typed Flow — merged line_ranges first (the
    flow's own span), loc_nodes spans as fallback. 0 when neither resolved."""
    total = 0
    for lr in getattr(flow, "line_ranges", None) or []:
        start = getattr(lr, "start_line", None)
        end = getattr(lr, "end_line", None)
        if isinstance(start, int) and isinstance(end, int):
            total += max(0, end - start + 1)
    if total:
        return total
    for nd in getattr(flow, "loc_nodes", None) or []:
        start = getattr(nd, "start_line", None)
        end = getattr(nd, "end_line", None)
        if isinstance(start, int) and isinstance(end, int):
            total += max(0, end - start + 1)
    return total


def _backstop_uncovered_pfs(
    new_ufs: list["UserFlow"],
    new_pfs: list["Feature"],
    dev_to_product: dict[str, tuple[str, ...]],
    developer_features: list["Feature"],
    promoted_caps: set[str],
    flow_owner_override: dict[str, str] | None = None,
    *,
    loc_only: bool = False,
) -> dict[str, Any]:
    """Guarantee every flowful product feature is referenced by >= 1 UF.

    Mutates ``new_ufs`` in place (reassigns ``product_feature_id`` /
    appends tagged thin journeys — the caller's content-sort + renumber
    runs AFTER this, so synthesized journeys get content-stable ids).
    Returns bounded telemetry. Fully deterministic: sorted iteration
    orders, no wall-clock, no randomness.

    ``loc_only`` (W5.1 final-close call site): run ONLY the FLOWLESS
    LOC-worthy arm (member-less system-seeds, I15/I16/I19-exempt) — the
    flow-ful reassign/synthesize arm has already run at the in-6.7d and
    lattice call sites, and re-running it here can mint a foreign-entry
    journey (I16). Trades zero violations: pure I8 close for the excavated
    0-flow-mint class the earlier arms structurally cannot cover.
    """
    from faultline.models.types import UserFlow
    from faultline.pipeline_v2.stage_6_7_user_flows import SYSTEM_RECALL_REASON

    tele: dict[str, Any] = {
        "pf_backstop_uncovered": 0,
        "pf_backstop_reassigned_ufs": 0,
        "pf_backstop_synthesized": 0,
        "pf_backstop_locworthy": 0,  # W5.1 member-less seeds for flowless PFs
        "pf_backstop_resolutions": [],  # bounded [{pf, action, ufs|members}]
    }

    def _resolve(pf_slug: str, action: str, detail: Any) -> None:
        if len(tele["pf_backstop_resolutions"]) < 50:
            tele["pf_backstop_resolutions"].append(
                {"pf": pf_slug, "action": action, "detail": detail})

    # Flow ownership registry: member id → owning PF slug (via the flow's
    # containment dev + dev_to_product), LOC and display label for the
    # synthesis pick. First containment owner wins (input order is stable).
    override = flow_owner_override or {}
    flow_pf: dict[str, str] = {}
    flow_loc: dict[str, int] = {}
    flow_obj: dict[str, Any] = {}
    pf_flows: dict[str, list[str]] = defaultdict(list)
    for dev in developer_features:
        slugs = dev_to_product.get(dev.name) or ()
        slug = slugs[0] if slugs else ""
        for fl in getattr(dev, "flows", None) or []:
            mid = _flow_member_id(fl)
            if not mid or mid in flow_pf:
                continue
            # Container-page redistribution (fix 3): a hosted flow's ownership
            # follows its real feature, not the page shell it was swept into.
            slug_for = override.get(mid, slug)
            flow_pf[mid] = slug_for
            flow_loc[mid] = _flow_loc_of(fl)
            flow_obj[mid] = fl
            if slug_for:
                pf_flows[slug_for].append(mid)

    # B13 — entry-owner HOME per flow (the validator's I16 ruler: entry-owner
    # first, dev-stamp fallback; None = lane/unowned). The synthesize arm
    # bundles ONLY own-entry flows so a coverage journey is never
    # majority-foreign. Computed once, flag-guarded (off => empty => the
    # pre-B13 containment path stays byte-identical).
    _owned_cover = backstop_owned_cover_enabled()
    home: dict[str, str | None] = (
        _flow_home_map(developer_features) if _owned_cover else {})

    # W1.1 — conservation-compatible reassignment (defect: the 2026-07-06
    # validation wave proved the reassign arm PING-PONGS with §4.5: it
    # judges by flow-containment member COUNT, conservation judges by
    # span-LOC file ownership, and the finalize conservation pass undid
    # exactly the backstop's reassigns (supabase: all 4 I8 PFs; midday:
    # 'support'), leaving the donors uncovered again. A reassignment is
    # coverage only if the conservation ladder would KEEP it — same
    # ruler, no ping-pong. Synthesized journeys were always exempt from
    # conservation, so the synthesize arm needs no check.
    from faultline.pipeline_v2.conservation import (
        _SHARED_PF_KEYS as _cons_shared_keys,
        build_file_pf_owner,
        conservation_enabled,
        conserved_pfid,
        dev_views_for,
    )

    _cons_on = conservation_enabled()
    if _cons_on:
        _pf_keys = frozenset(
            str(getattr(p, "id", None) or getattr(p, "name", "") or "")
            for p in new_pfs
        ) - {""}
        _file_pf_owner = build_file_pf_owner(
            dev_views_for(developer_features, dev_to_product),
            real_pf_keys=_pf_keys,
        )

    def _conservation_keeps(uf: "UserFlow", slug: str) -> bool:
        """Would the §4.5 ladder keep *uf* on *slug*? (True when the law
        is off — historical behavior.)"""
        if not _cons_on:
            return True
        members = [flow_obj[m] for m in (uf.member_flow_ids or [])
                   if m in flow_obj]
        if not members:
            return True
        chosen, _moved = conserved_pfid(members, _file_pf_owner, slug)
        return chosen == slug

    covered: Counter[str] = Counter(
        u.product_feature_id for u in new_ufs if u.product_feature_id)
    claimed: set[str] = {m for u in new_ufs for m in (u.member_flow_ids or [])}
    flowful = frozenset(pf_flows)

    # W5.1 — PF -> owning devs (for the flowless LOC-worthy loc rollup, the
    # validator's ``sum(feature_loc(m) for m in members)`` fallback).
    pf_members: dict[str, list[Any]] = defaultdict(list)
    for dev in developer_features:
        slugs = dev_to_product.get(dev.name) or ()
        if slugs:
            pf_members[slugs[0]].append(dev)
    _loc_arm = _loc_worthy_backstop_enabled()

    for pf in sorted(new_pfs, key=lambda p: p.name or ""):
        slug = pf.name or ""
        if not slug or covered.get(slug, 0) > 0:
            continue  # covered already (no duplicate backstop)
        if slug.strip().lower() in _cons_shared_keys:
            # Shared Platform owns code, never journeys (operator doctrine,
            # validator I10) — exempt from I8, must never pull a journey via
            # EITHER backstop arm (flow-ful reassign OR W5.1 flowless seed).
            continue
        if not pf_flows.get(slug):
            # ── W5.1 flowless arm: a journey-worthy-by-LOC surface with no
            # attachable flow (excavated 0-flow mints; validator I8 LOC bar).
            # A member-LESS system-seed is the only I7-exempt cover — the
            # flow-ful arm below cannot run (empty member pool). No-op until
            # Stage 6.97 stamps ``loc`` (pre-6.97 pf_loc == 0).
            if not _loc_arm:
                continue
            pf_loc = _feature_loc_attr(pf) or sum(
                _feature_loc_attr(m) for m in pf_members.get(slug, ()))
            if pf_loc < _LOC_WORTHY_MIN and not getattr(pf, "flows", None):
                continue  # not journey-worthy
            display = pf.display_name or slug
            new_ufs.append(UserFlow(
                id="UF-000",  # provisional — caller renumbers after sort
                name=display,
                resource=slug,
                domain=None,
                product_feature_id=slug,
                intent=_intent_for(display),
                member_flow_ids=[],
                member_count=0,
                routes=[],
                refined=True,
                name_confidence="low",
                ui_tier="no-ui",
                category="system",
                synthesized=True,
                synthesis_reason=SYSTEM_RECALL_REASON,
            ))
            covered[slug] += 1
            tele["pf_backstop_locworthy"] += 1
            _resolve(slug, "locworthy_seed", {"loc": pf_loc})
            continue
        if loc_only:
            continue  # flow-ful PF — earlier call sites own the member-ful arm
        tele["pf_backstop_uncovered"] += 1
        display = pf.display_name or slug
        reason = (_REASON_PROMOTED if display in promoted_caps
                  else _REASON_UNCOVERED)

        # ── 1. reassign majority-owned journeys ────────────────────────
        reassigned: list[str] = []
        for uf in sorted(new_ufs, key=lambda u: ((u.name or "").lower(),
                                                 str(u.resource or ""))):
            mids = uf.member_flow_ids or []
            if not mids or uf.product_feature_id == slug or uf.synthesized:
                continue
            owned = sum(1 for m in mids if flow_pf.get(m) == slug)
            if owned * 2 <= len(mids):
                continue  # not majority-owned by this PF's devs
            if not _conservation_keeps(uf, slug):
                continue  # §4.5 would resettle it away — synthesize instead
            donor = uf.product_feature_id
            if donor and donor in flowful and covered.get(donor, 0) <= 1:
                continue  # would uncover the donor — no violation trades
            if donor:
                covered[donor] -= 1
            uf.product_feature_id = slug
            covered[slug] += 1
            reassigned.append(uf.name)
        if reassigned:
            tele["pf_backstop_reassigned_ufs"] += len(reassigned)
            _resolve(slug, "reassigned", reassigned[:10])
            continue

        # ── 2. synthesize ONE thin journey (output-only, tagged) ───────
        if _owned_cover:
            # B13: bundle ONLY own-entry flows (entry-owner IS this PF) so the
            # synthesized journey can never be majority-foreign (validator
            # I16 — the containment-vs-entry gap that made 13 corpus backstop
            # UFs majority-foreign). A PF whose flows ALL enter foreign
            # surfaces has no own journey → emit a member-LESS coverage seed
            # (I8 covers on ANY UF ref — member_count is irrelevant; I16 never
            # fires at chk=0; the SYSTEM_RECALL_REASON reason is I7 D9-carve
            # exempt) instead of a foreign bundle.
            #
            # ``home`` is the validator's I16 owner: a member counts as FOREIGN
            # only when its entry file is owned by a DIFFERENT PF. ``None`` is
            # lane/unowned — "no evidence of foreignness, inheritable by any
            # journey" (``_flow_home_map`` doctrine) — and never trips I16
            # (the ruler skips owner=None), so own-and-inheritable both stay;
            # only genuinely-foreign entries (``home == other PF``) are dropped.
            pool = [m for m in pf_flows[slug]
                    if m not in claimed and home.get(m) in (None, slug)]
            if not pool:
                pool = [m for m in pf_flows[slug]
                        if home.get(m) in (None, slug)]
            if not pool:
                new_ufs.append(UserFlow(
                    id="UF-000",  # provisional — caller renumbers after sort
                    name=display,
                    resource=slug,
                    domain=None,
                    product_feature_id=slug,
                    intent=_intent_for(display),
                    member_flow_ids=[],
                    member_count=0,
                    routes=[],
                    refined=True,
                    name_confidence="low",
                    ui_tier="no-ui",
                    category="system",
                    synthesized=True,
                    synthesis_reason=SYSTEM_RECALL_REASON,
                ))
                covered[slug] += 1
                tele["pf_backstop_owned_seed"] = (
                    tele.get("pf_backstop_owned_seed", 0) + 1)
                _resolve(slug, "owned_cover_seed",
                         {"reason": "no_own_entry_flow"})
                continue
        else:
            pool = [m for m in pf_flows[slug] if m not in claimed]
            if not pool:
                # Every owned flow is claimed by other journeys (all minority
                # shares). Board completeness wins: reference the top flows
                # anyway — the journey is tagged, eval excludes it by tag.
                pool = list(pf_flows[slug])
        pool.sort(key=lambda m: (-flow_loc.get(m, 0), m))
        members = pool[:_BACKSTOP_MEMBER_CAP]
        if not members:
            continue  # defensive — pf_flows guaranteed non-empty above
        new_ufs.append(UserFlow(
            id="UF-000",  # provisional — caller renumbers after content sort
            name=display,
            resource=slug,
            domain=None,
            product_feature_id=slug,
            intent=_intent_for(display),
            member_flow_ids=members,
            member_count=len(members),
            routes=[],
            refined=True,
            name_confidence="low",
            synthesized=True,
            synthesis_reason=reason,
        ))
        claimed.update(members)
        covered[slug] += 1
        tele["pf_backstop_synthesized"] += 1
        _resolve(slug, "synthesized", {"reason": reason, "members": len(members)})
    return tele


# ── Shared-Platform UF reassignment (deterministic, $0) ─────────────────────
# Operator directive (2026-07-05, validator I10, CRITICAL): "Shared Platform
# may own CODE, never JOURNEYS." A user journey is by definition a product
# journey — the residual/platform bucket is an infrastructure container, so a
# user_flow must never carry its capability. On Soc0 (2026-07-05 audit) Call-2
# assigned journeys like "Browse and manage unified alert queue" / "Browse and
# triage detections feed" / "Manage investigation playbooks" to the residual
# even though their member flows belong to REAL product features
# (alert-ingestion / detections-page). Yesterday's residual guard protects the
# DEV -> PF mapping only; a UF's product_feature_id is set independently from
# Call-1's `product_feature` string, so a cross-feature journey can still land
# on the residual.
#
# Mechanism (runs inside ``_finish`` AFTER the PF-UF backstop -> applies
# identically to live and cache-hit replays): every UF still on the
# shared/platform capability is REASSIGNED to the non-shared product feature
# owning the PLURALITY of its member flows (flow -> owning dev -> dev's new
# capability via ``dev_to_product`` — the SAME "owning PF" notion the PF-UF
# backstop's ``flow_pf`` uses; shared-owned flows are IGNORED in the count).
# Ties break to the PF capturing the higher share of its OWN flows here, then
# the more specific (smaller) PF, then the alphabetically-first slug (stable).
# When EVERY member is shared-owned (no product PF at any share) the journey is
# a legitimate rarity — its code genuinely lives only in infra anchors
# (backend/main/frontend/mock) — so it is LEFT on the residual and counted in
# ``uf_shared_unresolved`` (validator I10 flags it; reported honestly). Leaving
# it there is also what keeps a flowful residual bucket's I8 satisfied.
# Kill-switch: FAULTLINE_STAGE_6_7D_UF_RESHARE=0 (default ON inside the opt-in
# 6.7d stage; registered in scan_result_cache.ENV_OUTPUT_FLAGS).
_SHARED_PF_SLUGS = frozenset({_slug(_RESIDUAL_CAP), "platform"})


def _uf_reshare_enabled() -> bool:
    return os.environ.get("FAULTLINE_STAGE_6_7D_UF_RESHARE", "1") != "0"


def _reshare_rank(
    pf: str, owner_counts: "Counter[str]", pf_size: "Counter[str]",
) -> tuple[int, float, int]:
    """Descending-preference key for a candidate non-shared owner PF:
    (plurality count, share of the PF's OWN flows captured here, -total flows
    owned). Higher wins; the caller applies an alphabetical slug tie-break for
    full determinism. ``share`` and ``-size`` agree (both favour the narrower
    PF on a count tie), so a tie is broken toward the more specific feature."""
    cnt = owner_counts[pf]
    size = pf_size.get(pf, 0)
    share = cnt / size if size else 0.0
    return (cnt, share, -size)


# ── All-shared-UF resolution ladder (RC2 fix-3, 2026-07-06) ─────────────────
# The plurality reshare above resolves a shared UF only when SOME member flow
# is owned by a NON-shared product feature. An "all-shared" UF — every member
# flow owned by an infra/anchor dev (studio / packages/ui / apps/worker) — has
# no non-shared candidate, so it stayed on the residual and tripped validator
# I10 corpus-wide (rallly command-palette, supabase 11, typebot/openstatus/…).
# These are REAL journeys whose implementation lives in SHARED packages. The
# ladder resolves them deterministically, in order:
#   (i)  TOKEN-FAMILY match of the UF NAME against an existing non-shared,
#        FLOWFUL capability (verb-folded stems so "…navigate" folds to the
#        "Navigation" capability, "authorize…" to "…authorization"); reassign.
#   (ii) ENTRY-FILE carve: the UF's member flows majority-sit under one
#        feature-dir domain owned by a domain-DEDICATED dev that was mis-sunk
#        to the residual — MINT that dev its own capability (tier-2/3 path,
#        flowful by construction). Anchors/app-shells never qualify.
#   (iii) neither fires — a legitimate rarity; LEFT on the residual and counted
#        in ``uf_shared_unresolved`` (I10 flags it; reported honestly).
# Verb folding is LADDER-LOCAL (does NOT touch the global ``_stem`` used by the
# dev-side join-over-mint) so existing family joins + snapshots stay byte-inert.
_VERB_FOLD_SUFFIXES: tuple[str, ...] = ("ate", "ize", "ise")


def _verb_fold(stem: str) -> str:
    """Fold a common verb ending onto its noun stem for NAME↔capability
    matching: ``navigate``→``navig`` (== ``navigation``), ``authorize``→
    ``author`` (== ``authorization``), ``integrate``→``integr``. Keeps a stem
    of >= _MIN_STEM_LEN so short/ambiguous stems (``create``→stays,
    ``state``→stays) never collapse. Idempotent on non-verb stems."""
    for suf in _VERB_FOLD_SUFFIXES:
        if stem.endswith(suf) and len(stem) - len(suf) >= _MIN_STEM_LEN:
            return stem[: -len(suf)]
    return stem


def _uf_family_capability(
    uf_name: str | None, cap_context: dict[str, dict[str, Any]],
) -> str | None:
    """Slug of the flowful non-shared capability whose token FAMILY the UF NAME
    shares — ladder step (i). ``cap_context`` maps each candidate display-name
    to ``{slug, stems, members, flows, paths}`` (only FLOWFUL non-shared PFs).
    Verb-folded on both sides. Deterministic: most shared stems, then the
    LARGEST behavioural home (flows → members → paths), then alpha slug."""
    uf_stems = {_verb_fold(s) for s in _family_stems(uf_name)}
    if not uf_stems:
        return None
    best: str | None = None
    best_key: tuple[int, int, int, int, str] | None = None
    for cap, ctx in cap_context.items():
        cap_stems = {_verb_fold(s) for s in ctx["stems"]}
        overlap = uf_stems & cap_stems
        if not overlap:
            continue
        key = (len(overlap), ctx["flows"], ctx["members"], ctx["paths"])
        slug = ctx["slug"]
        if best_key is None or key > best_key[:4] or (
            key == best_key[:4] and slug < best_key[4]
        ):
            best, best_key = slug, (*key, slug)
    return best


def _flowful_cap_context(
    new_pfs: list["Feature"],
    dev_to_product: dict[str, tuple[str, ...]],
    developer_features: list["Feature"],
) -> dict[str, dict[str, Any]]:
    """Family-match context keyed by display-name for every FLOWFUL non-shared
    product feature — its NAME stems + behavioural mass from its member devs.
    Only flowful caps are join targets (a UF/journey must land on a capability
    that actually owns journeys)."""
    dev_by_slug: dict[str, list["Feature"]] = defaultdict(list)
    for dev in developer_features:
        slugs = dev_to_product.get(dev.name) or ()
        if slugs:
            dev_by_slug[slugs[0]].append(dev)
    ctx: dict[str, dict[str, Any]] = {}
    for pf in new_pfs:
        slug = pf.name or ""
        if not slug or slug in _SHARED_PF_SLUGS:
            continue
        devs = dev_by_slug.get(slug, [])
        flows = sum(len(getattr(d, "flows", None) or []) for d in devs)
        if flows == 0:
            continue  # flowless shells are never a journey home
        disp = pf.display_name or slug
        ctx[disp] = {
            "slug": slug, "stems": _family_stems(disp), "flows": flows,
            "members": len(devs), "paths": sum(len(_paths_of(d)) for d in devs),
        }
    return ctx


def _carve_shared_uf(
    uf: "UserFlow",
    flow_owner_dev: dict[str, str],
    dev_by_name: dict[str, "Feature"],
    dev_to_product: dict[str, tuple[str, ...]],
    new_pfs: list["Feature"],
    route_files: frozenset[str],
    route_uuids: frozenset[str],
) -> str | None:
    """Ladder step (ii) — MINT a capability for an all-shared UF whose member
    flows are owned by a domain-DEDICATED dev that was mis-sunk to the residual.

    The plurality owning dev of the UF's member flows is promoted via the SAME
    tier-2 feature-dir / tier-3 route-surface test used at reconstruction
    (``_feature_dir_capability`` / ``_route_surface_capability``). A workspace
    anchor / app-shell mega-dev (``apps/studio`` / ``packages/ui``) never
    qualifies, so a broad shared package is never carved. On a hit the dev is
    re-homed onto the minted PF (flowful by construction — it owns these flows)
    and the new PF slug is returned; otherwise ``None`` (→ ladder iii)."""
    counts: Counter[str] = Counter()
    for m in uf.member_flow_ids or []:
        dn = flow_owner_dev.get(m)
        if dn:
            counts[dn] += 1
    if not counts:
        return None
    top = max(counts.values())
    dev_name = min(n for n, c in counts.items() if c == top)
    dev = dev_by_name.get(dev_name)
    if dev is None:
        return None
    promo = _feature_dir_capability(dev) or _route_surface_capability(
        dev, route_files, route_uuids)
    if not promo:
        return None
    slug = _slug(promo)
    if slug in _SHARED_PF_SLUGS:
        return None
    existing = {p.name or "" for p in new_pfs}
    if slug not in existing:
        new_pfs.append(aggregate_product_feature(
            name=slug, display_name=promo,
            description=_flows_surface_description(dev), contrib=[dev]))
    dev_to_product[dev.name] = (slug,)
    return slug


# ── DOCS/API-SURFACE family rung (RC2-4, 2026-07-06) ─────────────────────────
# Ladder step (iii), between the entry-file carve and the final unresolved
# fallback. A UF whose NAME reads like "Access documented REST API" is not,
# by itself, evidence that its flows are grounded in a real docs surface — a
# name is generated from cluster content and can describe a journey whose
# flows are actually anchored somewhere unrelated (midday's own instance: 12
# flows all in ``apps/api/evals/fixtures.ts``, an LLM tool-selection EVAL
# FIXTURES file, zero docs/openapi involvement — see the sibling
# ``_synthetic_fixture_uf`` rung below, which is what actually resolves that
# case). This rung is guarded on the FLOW PATHS, not the UF name: it fires
# only when a MAJORITY of member flows are genuinely route-grounded in a
# documented-API surface (openapi spec route, Scalar/Swagger UI mount, a
# ``/docs`` or ``api-reference`` page) — universal web-API vocabulary that
# needs no per-stack YAML (it is a naming CONVENTION, like "test"/"spec", not
# a stack-specific location rule). openstatus is the confirmed positive case
# (a real "browse API reference" journey belongs here, not in shared-platform
# or force-merged into an unrelated capability) — dev_tooling-natured, but a
# REAL customer-facing capability, so it gets its own dedicated home rather
# than being suppressed.
_DOCS_SURFACE_PATH_TOKENS: tuple[str, ...] = (
    "openapi", "swagger", "scalar", "api-reference", "/docs/",
)
_DOCS_SURFACE_PF_SLUG = "api-documentation"
_DOCS_SURFACE_PF_DISPLAY = "API Documentation"


def _is_docs_surface_path(path: str | None) -> bool:
    """True when ``path`` structurally names an openapi/swagger/scalar/
    api-reference/docs surface. Path-grounded (never the UF's free-text
    name) — see the rung docstring above for why that distinction matters."""
    p = (path or "").lower().replace("\\", "/")
    if not p:
        return False
    return any(tok in p for tok in _DOCS_SURFACE_PATH_TOKENS) or p.endswith("/docs")


def _docs_surface_uf(
    uf: "UserFlow",
    flow_by_id: dict[str, Any],
    flow_owner_dev: dict[str, str],
    dev_by_name: dict[str, "Feature"],
    dev_to_product: dict[str, tuple[str, ...]],
    new_pfs: list["Feature"],
) -> str | None:
    """Ladder step (iii) — mint/join the ``api-documentation`` capability for
    an all-shared UF whose flows are MAJORITY route-grounded in a docs/
    openapi surface. Returns the PF slug on a hit, else ``None`` (→ step iv).
    Join-over-mint: a second docs-surface UF reuses the same capability."""
    mids = uf.member_flow_ids or []
    if not mids:
        return None
    matched = [
        m for m in mids
        if _is_docs_surface_path(getattr(flow_by_id.get(m), "entry_point_file", None))
    ]
    if not matched or len(matched) * 2 < len(mids):
        return None  # not a MAJORITY — declines rather than force-fitting
    counts: Counter[str] = Counter(
        flow_owner_dev[m] for m in matched if flow_owner_dev.get(m)
    )
    if not counts:
        return None
    top = max(counts.values())
    dev_name = min(n for n, c in counts.items() if c == top)
    dev = dev_by_name.get(dev_name)
    if dev is None:
        return None
    slug = _DOCS_SURFACE_PF_SLUG
    existing = {p.name or "" for p in new_pfs}
    if slug not in existing:
        new_pfs.append(aggregate_product_feature(
            name=slug, display_name=_DOCS_SURFACE_PF_DISPLAY,
            description=_flows_surface_description(dev), contrib=[dev]))
    dev_to_product[dev.name] = (slug,)
    return slug


# ── Synthetic-fixture UF drop (RC2-4, 2026-07-06) ────────────────────────────
# Ladder step (iv), the last resort before the honest-unresolved fallback.
# midday's "Access documented REST API" diagnosis: Stage 3/4 flow detection
# produced 12 "flows" entirely from ``apps/api/evals/fixtures.ts`` — a
# ``ToolSelectionFixture[]`` test-data module for an LLM tool-selection eval
# harness, not application code. None of the 12 resolved to a real symbol
# (every node is the whole-file ``"<file>"`` fallback — the pipeline's
# existing "found no real symbol" marker, see ``flow_symbols.py``), and the
# file was never identified as a route. A UF built entirely from this
# material is not a deferred-home journey (rungs i-iii correctly decline it)
# — it never was a journey. Dropping it (not re-homing it) is the honest
# outcome; its dropped member flows stay in ``flows[]`` as ordinary
# code-grain artifacts (I4 unaffected) and their ``user_flow_id``
# backpointers are nulled by the existing I14 emission-integrity pass
# (orphan, not dangling — the same mechanism every other drop path relies
# on).
#
# Narrow by construction — ALL THREE conditions must hold for EVERY member
# flow, which is why the corpus collateral sweep (langfuse's ``evals/`` IS
# its product, openstatus's ``routes/mcp/evals/`` looks like a real routed
# capability, onyx's ``server/evals/api.py`` looks like a real backend
# route) never trips this rung: those files are either real detected routes
# or carry richer, non-whole-file-fallback flows.
_SYNTHETIC_DATA_FILE_MARKERS: tuple[str, ...] = (
    "fixture", "mock", "dummy", "sample",
)


def _is_synthetic_data_path(path: str | None) -> bool:
    """True when ``path``'s basename or directory names a test/eval
    fixture-data convention (fixtures / mocks / dummy / sample data, or the
    Vercel-AI-SDK / Mastra ``evals/`` + ``*.eval.ts`` AI-agent-eval
    convention). Deliberately NOT the bare token ``eval`` alone — that word
    is common in legitimate product code (an "evaluator" service, a formula
    "eval" feature) and would false-positive (see the Soc0 ``*_eval.py``
    collateral finding)."""
    p = (path or "").lower().replace("\\", "/")
    if not p:
        return False
    # Marker anywhere in the path — catches both filename conventions
    # (fixtures.ts, user.mock.ts) and directory conventions (__mocks__/,
    # test/fixtures/, evals/dummy-data/).
    if any(tok in p for tok in _SYNTHETIC_DATA_FILE_MARKERS):
        return True
    base = p.rsplit("/", 1)[-1]
    if base.endswith(".eval.ts") or base.endswith(".eval.tsx"):
        return True
    return f"/{p}".count("/evals/") > 0


def _is_whole_file_fallback_flow(flow: Any) -> bool:
    """True when a flow's ONLY node is the whole-file ``"<file>"`` fallback
    symbol — Stage 3/4 found no real function/handler inside it."""
    nodes = getattr(flow, "nodes", None) or []
    if len(nodes) != 1:
        return False
    node = nodes[0]
    sym = node.get("symbol") if isinstance(node, dict) else getattr(node, "symbol", None)
    return sym == "<file>"


def _synthetic_fixture_uf(
    uf: "UserFlow",
    flow_by_id: dict[str, Any],
    route_files: frozenset[str],
) -> bool:
    """Ladder step (iv) — ``True`` when EVERY member flow of an all-shared UF
    is (a) not a real detected route, (b) a whole-file fallback attribution,
    and (c) anchored in a fixture/mock/eval-convention file. All three must
    hold for ALL members — a single genuinely-grounded flow disqualifies the
    drop (falls through to the honest-unresolved fallback instead)."""
    mids = uf.member_flow_ids or []
    if not mids:
        return False
    for m in mids:
        flow = flow_by_id.get(m)
        if flow is None:
            return False
        entry = getattr(flow, "entry_point_file", None)
        if entry and entry in route_files:
            return False
        if not _is_whole_file_fallback_flow(flow):
            return False
        if not _is_synthetic_data_path(entry):
            return False
    return True


# ── Draw-native flowless-shell resolution (RC2 fix-3, Part B) ────────────────
# The PF-UF backstop guarantees a UF for every FLOWFUL capability, and the
# residual guard's flowful-requirement stops a FLOWLESS residual dev from
# minting a shell — but a Call-1 draw can still EMIT a capability whose devs own
# real code yet ZERO flows (linkwarden "Highlights"=AI worker, "Localization"=
# i18n): a draw-native flowless shell. With >= 1k owned LOC it trips validator
# I8 (journeys-worthy code, no UF) and the backstop can't cover it (no flow to
# reference). Extend the join-over-mint + flowful discipline to these shells,
# in order: (1) ABSORB a residual dev whose whole footprint sits inside the
# shell's claimed paths and which HAS flows (makes it flowful); (2) JOIN a
# token-family flowful PF (fold its devs in, drop the shell); (2) if the shell
# still owns >= 1k LOC with no flows, DEMOTE its devs to the shared bucket and
# DROP the shell (honest platform code, not a product feature); smaller flowless
# PFs are tolerated (I8 only fires >= 1k LOC).
#
# NOTE (2026-07-06): a naive "absorb a footprint-matched residual dev" rung was
# considered + rejected — a shell's claimed paths are often an anchor ROOT
# (``apps/web``), so prefix-matching swept an unrelated flowful dev into the
# shell and manufactured a has-flows I8. It is also unsound in principle: if a
# flowful dev genuinely belonged to the shell's domain, the Call-1 draw would
# have mapped it there (making the PF flowful). A truly flowless shell has
# nothing safe to absorb — so join-over-mint then demote-and-drop is the whole
# ladder.
#
# PLACEMENT: this runs as a POST-``feature_loc`` deterministic pass, NOT inside
# 6.7d ``_finish``. The >= 1k-LOC prong needs owned ``loc``, and Stage 6.97
# (feature_loc) is what populates ``Feature.loc`` — it runs AFTER 6.7d. Running
# here (right before emission-integrity) is the only point where the exact I8
# LOC signal exists; the pass is deterministic + always-run, so replay identity
# is preserved. Operates on the stamped ``feat.product_feature_id`` membership.
# Kill-switch: FAULTLINE_STAGE_6_7D_SHELL_ABSORB=0.
_SHELL_LOC_FLOOR = 1000  # mirrors validator I8's journeys-worthy LOC prong


def _shell_absorb_enabled() -> bool:
    return os.environ.get("FAULTLINE_STAGE_6_7D_SHELL_ABSORB", "1") != "0"


def _owned_loc_of(feat: "Feature") -> int:
    """A feature's OWNED LOC (never ``loc_shared`` — mirrors validator
    ``feature_loc``): the ``loc`` field, else 0."""
    v = getattr(feat, "loc", None)
    return v if isinstance(v, int) and v > 0 else 0


def resolve_flowless_shells(
    developer_features: list["Feature"],
    product_features: list["Feature"],
) -> tuple[list["Feature"], dict[str, Any]]:
    """Resettle draw-native flowless shells (see the block above), post-
    feature_loc. Reads/writes ``feat.product_feature_id`` membership + reads
    ``feat.loc``. Returns ``(filtered_product_features, telemetry)`` with the
    dropped shells removed. Fully deterministic — sorted iteration, no
    wall-clock, no randomness."""
    tele: dict[str, Any] = {
        "shell_absorbed": 0, "shell_joined": 0, "shell_demoted": 0,
        "shell_resolutions": [],  # bounded [{shell, action, target}]
    }

    def _record(shell: str, action: str, target: str | None) -> None:
        if len(tele["shell_resolutions"]) < 50:
            tele["shell_resolutions"].append(
                {"shell": shell, "action": action, "target": target})

    shared_slug = _slug(_RESIDUAL_CAP)

    def _pf_of(dev: "Feature") -> str:
        return getattr(dev, "product_feature_id", None) or ""

    slug_members: dict[str, list["Feature"]] = defaultdict(list)
    for dev in developer_features:
        pid = _pf_of(dev)
        if pid:
            slug_members[pid].append(dev)
    pf_by_slug = {p.name or "": p for p in product_features}

    def _flows(devs: list["Feature"]) -> int:
        return sum(len(getattr(d, "flows", None) or []) for d in devs)

    def _rehome(dev: "Feature", target_slug: str) -> None:
        dev.product_feature_id = target_slug
        tgt = pf_by_slug.get(target_slug)
        if tgt is not None:
            _extend_pf_membership(tgt, dev)

    def _ensure_shared_pf(contrib: list["Feature"]) -> "Feature":
        pf = pf_by_slug.get(shared_slug)
        if pf is None:
            # aggregate_product_feature averages over contrib → needs >= 1 dev;
            # the demote members are exactly that seed (idempotent re-home).
            pf = aggregate_product_feature(
                name=shared_slug, display_name=_RESIDUAL_CAP,
                description=_RESIDUAL_DESCRIPTION, contrib=list(contrib))
            product_features.append(pf)
            pf_by_slug[shared_slug] = pf
        return pf

    dropped: set[int] = set()
    # Family-join context = the FLOWFUL non-shared PFs (a shell may only fold
    # into a capability that actually owns journeys).
    cap_context = {
        (p.display_name or p.name or ""): {
            "stems": _family_stems(p.display_name or p.name),
            "members": len(slug_members.get(p.name or "", [])),
            "flows": _flows(slug_members.get(p.name or "", [])),
            "paths": sum(len(_paths_of(d))
                         for d in slug_members.get(p.name or "", [])),
        }
        for p in product_features
        if (p.name or "") not in _SHARED_PF_SLUGS
        and _flows(slug_members.get(p.name or "", [])) > 0
    }

    for pf in sorted(product_features, key=lambda p: p.name or ""):
        slug = pf.name or ""
        if not slug or slug in _SHARED_PF_SLUGS:
            continue
        members = slug_members.get(slug, [])
        if not members or _flows(members) > 0:
            continue  # flowful (or empty) — not a shell
        display = pf.display_name or slug

        # (1) JOIN a token-family flowful PF (drop the shell).
        fam = _family_capability_match(pf, cap_context)
        if fam is not None:
            fam_slug = _slug(fam)
            for dev in members:
                _rehome(dev, fam_slug)
            dropped.add(id(pf))
            tele["shell_joined"] += 1
            _record(display, "joined", fam_slug)
            continue

        # (2) DEMOTE + DROP — only when the shell is journeys-worthy by LOC
        # (>= 1k owned). Smaller flowless PFs are tolerated (mirror I8).
        owned = _owned_loc_of(pf) or sum(_owned_loc_of(m) for m in members)
        if owned < _SHELL_LOC_FLOOR:
            continue
        _ensure_shared_pf(members)
        for dev in members:
            _rehome(dev, shared_slug)
        dropped.add(id(pf))
        tele["shell_demoted"] += 1
        _record(display, "demoted", shared_slug)

    if dropped:
        product_features = [p for p in product_features if id(p) not in dropped]
    return product_features, tele


def _extend_pf_membership(pf: "Feature", dev: "Feature") -> None:
    """Fold a re-homed dev's owned files into a target PF's registries so the
    PF's ``paths`` / ``member_files`` stay coherent after a shell resettle
    (dedup by path, order-stable)."""
    seen = set(pf.paths or [])
    for p in _paths_of(dev):
        if p not in seen:
            pf.paths.append(p)
            seen.add(p)
    existing_mf = getattr(pf, "member_files", None)
    if existing_mf is None:
        return
    seen_mf = {
        (mf.get("path") if isinstance(mf, dict) else getattr(mf, "path", None))
        for mf in existing_mf
    }
    for mf in (getattr(dev, "member_files", None) or []):
        p = mf.get("path") if isinstance(mf, dict) else getattr(mf, "path", None)
        if p and p not in seen_mf:
            existing_mf.append(mf)
            seen_mf.add(p)


def _reassign_shared_ufs(
    new_ufs: list["UserFlow"],
    developer_features: list["Feature"],
    dev_to_product: dict[str, tuple[str, ...]],
    flow_owner_override: dict[str, str] | None = None,
    *,
    new_pfs: list["Feature"] | None = None,
    routes_index: list[dict[str, Any]] | None = None,
    anchored: bool = False,
) -> dict[str, Any]:
    """Reassign every UF left on the shared/platform capability to the
    non-shared PF owning the plurality of its member flows (see the block
    rationale above). An ALL-SHARED UF (no non-shared owner) runs the
    resolution ladder — token-family NAME match → entry-file carve → honest
    unresolved (see the ladder block above ``_verb_fold``). Mutates
    ``new_ufs[*].product_feature_id`` (and, on a carve, ``new_pfs`` /
    ``dev_to_product``) in place; returns bounded telemetry. Fully
    deterministic — sorted iteration, no wall-clock, no randomness."""
    tele: dict[str, Any] = {
        "uf_shared_reassigned": 0,
        "uf_shared_unresolved": 0,
        "uf_shared_family_resolved": 0,
        "uf_shared_carved": 0,
        "uf_shared_docs_resolved": 0,
        "uf_shared_synthetic_dropped": 0,
        "uf_shared_reassignments": [],  # bounded [{uf, from, to, basis}]
    }

    def _record(uf_name: str, donor: str | None, target: str | None,
                basis: str) -> None:
        if len(tele["uf_shared_reassignments"]) < 50:
            tele["uf_shared_reassignments"].append(
                {"uf": uf_name, "from": donor, "to": target, "basis": basis})

    pf_list = new_pfs if new_pfs is not None else []
    cap_context = _flowful_cap_context(pf_list, dev_to_product,
                                       developer_features)
    dev_by_name = {d.name: d for d in developer_features}
    route_files = frozenset(
        str(r.get("file") or "") for r in (routes_index or [])) - {""}
    route_uuids = frozenset(
        str(r.get("feature_uuid") or "") for r in (routes_index or [])) - {""}

    # member id -> owning PF slug via the flow's owning dev (containment; first
    # owner wins — input order stable). Mirrors _backstop_uncovered_pfs.flow_pf
    # so "owning PF" stays one consistent notion across the two passes.
    override = flow_owner_override or {}
    flow_pf: dict[str, str] = {}
    flow_owner_dev: dict[str, str] = {}
    flow_by_id: dict[str, Any] = {}
    for dev in developer_features:
        slugs = dev_to_product.get(dev.name) or ()
        slug = slugs[0] if slugs else ""
        for fl in getattr(dev, "flows", None) or []:
            mid = _flow_member_id(fl)
            if mid and mid not in flow_pf:
                # Container-page redistribution (fix 3): honour the hosted
                # flow's real owner over the page shell it was swept into.
                flow_pf[mid] = override.get(mid, slug)
                flow_owner_dev[mid] = dev.name
                flow_by_id[mid] = fl
    # Total flows each PF owns — the specificity tie-break (smaller = narrower).
    pf_size: Counter[str] = Counter(flow_pf.values())
    uf_drop_ids: set[int] = set()

    for uf in sorted(new_ufs, key=lambda u: ((u.name or "").lower(),
                                             str(u.resource or ""))):
        if (uf.product_feature_id or "") not in _SHARED_PF_SLUGS:
            continue
        mids = uf.member_flow_ids or []
        owner_counts: Counter[str] = Counter()
        for m in mids:
            owner = flow_pf.get(m, "")
            if owner and owner not in _SHARED_PF_SLUGS:
                owner_counts[owner] += 1
        if not owner_counts:
            # ALL-SHARED UF — no non-shared owner. Run the resolution ladder.
            donor0 = uf.product_feature_id
            # (i) token-family NAME match against a flowful non-shared cap.
            fam_slug = _uf_family_capability(uf.name, cap_context)
            if fam_slug:
                uf.product_feature_id = fam_slug
                tele["uf_shared_family_resolved"] += 1
                _record(uf.name, donor0, fam_slug, "name_family_match")
                continue
            # (ii)+(iii) are PF-APPEND rungs — they mint capabilities that
            # are NOT anchored nodes (no anchor_id). FORBIDDEN in anchored
            # mode (review F2: "the PF universe is FIXED by the mint");
            # the ladder falls through to the drop/unresolved rungs and
            # conservation / terminal home place the journey by ownership.
            if not anchored:
                # (ii) entry-file carve — mint a domain-dedicated owning
                # dev its own capability (flowful by construction).
                carved = _carve_shared_uf(
                    uf, flow_owner_dev, dev_by_name, dev_to_product, pf_list,
                    route_files, route_uuids)
                if carved:
                    uf.product_feature_id = carved
                    tele["uf_shared_carved"] += 1
                    _record(uf.name, donor0, carved, "entry_file_carve")
                    # keep cap_context fresh so a later UF can family-match
                    continue
                # (iii) DOCS/API-SURFACE family — flows genuinely
                # route-grounded in an openapi/scalar/docs surface get
                # their own home rather than defaulting to shared-platform.
                docs_slug = _docs_surface_uf(
                    uf, flow_by_id, flow_owner_dev, dev_by_name,
                    dev_to_product, pf_list)
                if docs_slug:
                    uf.product_feature_id = docs_slug
                    tele["uf_shared_docs_resolved"] += 1
                    _record(uf.name, donor0, docs_slug, "docs_api_surface")
                    continue
            # (iv) synthetic-fixture UF — every member is a whole-file
            # fallback attribution anchored in eval/fixture/mock data, not a
            # real journey. Drop it rather than force a home (see rung
            # docstring above ``_synthetic_fixture_uf``).
            if _synthetic_fixture_uf(uf, flow_by_id, route_files):
                tele["uf_shared_synthetic_dropped"] += 1
                _record(uf.name, donor0, None, "synthetic_fixture_drop")
                uf_drop_ids.add(id(uf))
                continue
            # (v) legitimate rarity — leave on the residual; flag for I10.
            tele["uf_shared_unresolved"] += 1
            _record(uf.name, donor0, None, "all_members_shared_owned")
            continue
        best_key = max(_reshare_rank(pf, owner_counts, pf_size) for pf in owner_counts)
        target = min(
            pf for pf in owner_counts
            if _reshare_rank(pf, owner_counts, pf_size) == best_key)
        donor = uf.product_feature_id
        uf.product_feature_id = target
        tele["uf_shared_reassigned"] += 1
        _record(uf.name, donor, target,
                f"plurality {owner_counts[target]}/{len(mids)}")
    if uf_drop_ids:
        # Prune in place — ``new_ufs`` is the caller's live list; the loop
        # above iterated a sorted COPY, so drops must be applied here.
        new_ufs[:] = [u for u in new_ufs if id(u) not in uf_drop_ids]
    return tele


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
    anchored: bool = False,
    interior_evidence: dict[str, Any] | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> tuple[list["UserFlow"], list["Feature"], dict[str, tuple[str, ...]] | None, dict[str, Any]]:
    """Rewrite user_flows[] + product_features[] at journey/capability grain.

    Returns ``(user_flows, product_features, dev_to_product_map, telemetry)``.

    ``anchored=True`` (Product-Spine §4.3, Wave 2b — the Stage 6.86 mint
    ran): the PF universe is FIXED. Call 1 runs CONSTRAINED (journeys must
    cite the given capabilities; invented PF names are dropped) and Call 2
    — the per-item membership oracle (rootcause RC1) — is RETIRED: dev→PF
    is the mint's lineage stamps, no LLM call. Membership ladders that
    joined by name stems are bypassed (the spine already decided); the
    UF-side ladders (conservation / backstop / reshare) run unchanged on
    spine relations. Residual devs stay OFF the product list (the
    platform_infrastructure lane) — no Shared Platform PF exists here.

    NEVER-WORSE INVARIANT (operator's core requirement): on ANY failure path —
    no client, LLM exception, empty / unparseable output, re-attribution
    failure, cost-cap, empty reconstruction — the ORIGINAL deterministic
    ``user_flows`` + ``product_features`` are returned UNCHANGED (object
    identity), ``dev_to_product_map`` is ``None`` (caller keeps its mapping),
    and ``telemetry["fallback"]`` is set to the reason string. On success
    ``telemetry["fallback"]`` is ``None`` and ``applied`` is ``True``.

    W4.1 (first-draw brittleness): an empty/unparseable FIRST Call-1 draw no
    longer degrades outright — it is retried once (same prompt,
    ``telemetry["retry_used"]=1``, omitted when unused); interior-evidence
    runs whose retry also fails fall back to the evidence-less v1 prompt +
    cache namespace before degrading (``telemetry["fallback"]="v1_prompt"``
    with ``applied=True`` when that path wins).

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

    def _record(model_: str, in_tok: int, out_tok: int,
                meta: dict[str, Any] | None = None) -> float:
        cost = estimate_call_cost(model_, in_tok, out_tok) if (in_tok or out_tok) else 0.0
        if cost_tracker is not None and (in_tok or out_tok):
            try:
                cost_tracker.record(model=model_, input_tokens=in_tok,
                                    output_tokens=out_tok, label="stage_6_7d",
                                    decision_meta=meta)
            except Exception:  # noqa: BLE001 — budget cap is enforced elsewhere
                pass
        return cost

    # ── Digest + input-size telemetry (large-repo robustness) ─────────
    # Split-invariance: fold Stage 8.9/8.9.5 subfeatures into their parents
    # for BOTH LLM calls, so the product abstraction never sees (or pays the
    # digest cap for) the DF layer's container decomposition. Their files
    # re-join the right capability via _propagate_dev_map at reconstruction.
    # Product-Spine §4.1: concern FACETS are excluded from the digest and
    # from Call-2 re-attribution — a cross-cutting view is never a
    # capability owner, so the LLM must not see (or map) it.
    from faultline.pipeline_v2.spine_hygiene import is_facet as _is_facet

    non_facet_devs = [f for f in developer_features if not _is_facet(f)]
    tele["facet_devs_excluded"] = len(developer_features) - len(non_facet_devs)
    dev_view, sub_to_parent = _rollup_split_view(non_facet_devs)
    # S5b Seg H — digest stratification (default OFF): the page-anchor walk
    # runs over the ORIGINAL feature list (rollup views carry no flows) and
    # only when armed, so the unset/``0`` digest is byte-identical.
    _strat = digest_stratification_enabled()
    _strat_page_ids: frozenset[str] | None = None
    if _strat:
        _strat_page_ids = _page_anchored_uf_ids(
            user_flows, developer_features, routes_index)
    digest = _build_digest(
        dev_view, product_features, user_flows, routes_index,
        stratified=_strat, page_anchored=_strat_page_ids,
    )
    if _strat:
        # telemetry key only on fire (openstatus inertness law).
        tele["digest_stratification"] = {
            "page_anchored_ufs": len(_strat_page_ids or ()),
            "uf_appended": max(
                0, len(digest["current_user_flows"]) - MAX_USER_FLOWS_DIGEST),
            "page_routes_in_digest": sum(
                1 for r in digest["routes"]
                if str(r.get("m") or "").upper() == "PAGE"),
        }

    # ── Anchored mode (Wave 2b): dev→PF from the mint's lineage stamps ──
    # The map is TOTAL over lineage devs; lane residents (pfid=None) are
    # deliberately ABSENT — they never join a capability and never form a
    # Shared Platform row (operator amendment 2026-07-06).
    anchored_dev_map: dict[str, str] = {}
    anchored_names: set[str] = set()
    anchored_pf_by_name: dict[str, "Feature"] = {}
    if anchored:
        tele["anchored"] = True
        pf_by_key = {getattr(p, "name", "") or "": p for p in product_features}
        for p in product_features:
            disp = getattr(p, "display_name", None) or getattr(p, "name", "")
            anchored_names.add(disp)
            anchored_pf_by_name[disp] = p
        for f in non_facet_devs:
            pid = getattr(f, "product_feature_id", None)
            pf = pf_by_key.get(pid or "")
            if pf is not None:
                anchored_dev_map[_dev_key(f)] = (
                    getattr(pf, "display_name", None) or getattr(pf, "name", "")
                )
        tele["anchored_dev_map"] = len(anchored_dev_map)
        tele["anchored_lane_devs"] = (
            len(non_facet_devs) - len(anchored_dev_map)
        )
    # ── W4 §4.6 — page-interior sections extend the citation vocabulary ──
    # ANCHORED mode only: each fixed capability's digest entry gains the
    # section labels the Stage-6.55 parser found inside ITS OWN pages.
    # When no evidence exists (tree-sitter absent / stage off) the digest,
    # the prompt and the cache namespace stay byte-identical to pre-W4.
    interior_attached = False
    if anchored and interior_evidence:
        by_pf = interior_evidence.get("by_pf") or {}
        if by_pf:
            for line in digest["current_product_features"]:
                secs = by_pf.get(str(line.get("name") or ""))
                if secs:
                    line["sections"] = [str(s) for s in secs][:8]
                    interior_attached = True
    if interior_attached:
        tele["interior_sections_pfs"] = sum(
            1 for line in digest["current_product_features"]
            if line.get("sections")
        )
    tele.update({
        "digest_rolled_subs": len(sub_to_parent),
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
    aligned = (align_enabled() and not anchored
               and _anchors_sufficient(product_anchors, product_features))
    tele["aligned"] = aligned
    tele["anchor_count"] = len(anchor_texts)
    if anchored:
        # Distinct cache namespace: the anchored prompt/reconstruction
        # semantics differ from free-gen, and the =0 path's keys must
        # stay byte-identical to pre-W2b (no new payload field there).
        # W4: interior-evidence runs append the _INTERIOR_ADDENDUM to
        # the system prompt (not part of the key), so they get their
        # OWN namespace; evidence-less anchored runs keep v1 and replay
        # their existing cache untouched (rule-cache-invalidation).
        anchor_sig = ("spine-anchored-mint-v2-interior"
                      if interior_attached else "spine-anchored-mint-v1")
    else:
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
        # Cache stores the PARENT-level map; propagation is deterministic
        # from (map, current features), so live and cache-hit replays agree.
        #
        # W3.1 D1 (fb3 dossier): NEVER propagate in ANCHORED mode. The
        # anchored map is TOTAL over the mint's lineage stamps — every
        # split subfeature was classified independently by Stage 6.86,
        # and a sub ABSENT from the map is a deliberate platform-lane
        # verdict. Propagation resurrected laned subs into whatever PF
        # their PARENT (mis)folded to, then the phase_finalize re-stamp
        # emptied the lane behind the mint's back: supabase `studio`'s
        # 32 sub-domain splits (110K LOC, shared_reason=
        # shell_lineage_only) rode `fold:import` into the 2.7K
        # `claim-project` PF — the biggest fresh-blood trust failure.
        # Call-2-era scans (anchored=False) keep the shim: their map is
        # parent-grain by construction and subs legitimately inherit.
        if not anchored:
            dev_map = _propagate_dev_map(dev_map, sub_to_parent)
        (new_pfs, dev_to_product, files_after, pf_tele,
         flow_owner_override) = _build_product_features(
            pf_specs_, dev_map, developer_features, routes_index,
            anchored=anchored, anchored_pf_by_name=anchored_pf_by_name)
        new_ufs, uf_tele = _build_user_flows(
            uf_specs_, user_flows, developer_features, routes_index,
            interior_evidence=interior_evidence if anchored else None,
            home_pure=anchored,
            # B74 Seg C: the INPUT product_features are the Stage 6.86
            # mint universe (the phase_finalize:1092 argument), whose
            # anchor_id carries the ws-pkg container marker. _build_
            # user_flows has no PF list of its own — thread the derived
            # container-key set (empty set when un-anchored / flag OFF).
            container_pf_keys=(
                _container_pf_keys(product_features) if anchored else None))
        if not new_pfs or not new_ufs:
            return None
        if anchored:
            # ENTRY-OWNER overrides (amendment: a vendor child's journeys
            # live in the vendor PF even when the flow object rides the
            # plumbing dev — Soc0 edr build-cortex-filters): a flow whose
            # entry file is PRIMARY-owned by a different dev follows that
            # owner's capability. Same ruler as validator I16. Explicit
            # container redistributions (none in anchored mode) would win.
            entry_over = _entry_owner_overrides(
                developer_features, dev_to_product)
            merged_over = dict(entry_over)
            merged_over.update(flow_owner_override)
            flow_owner_override = merged_over
            pf_tele["entry_owner_overrides"] = len(entry_over)
            tele.update({"entry_owner_overrides": len(entry_over)})
        # Product-Spine §4.5 — conservation law, applied to the freshly
        # reconstructed bindings BEFORE the backstop/reshare ladders: the
        # backstop then re-covers any PF a resettle emptied, and the
        # reshare ladder operates on conservation-clean bindings (its own
        # plurality/carve moves are conservation-shaped by construction).
        # Kill-switch: FAULTLINE_SPINE_CONSERVATION=0.
        from faultline.pipeline_v2.conservation import apply_uf_conservation

        cons_tele = apply_uf_conservation(
            new_ufs, developer_features, new_pfs,
            dev_to_product=dev_to_product,
        )
        tele["conservation"] = cons_tele
        # PF-UF backstop: every flowful capability must be referenced by
        # >= 1 journey (validator I8 — see the block above
        # _backstop_uncovered_pfs). BEFORE the content sort so synthesized
        # journeys join the stable renumbering like any other UF. Shared
        # by the live and cache-hit paths → byte-identical replays.
        if _pf_uf_backstop_enabled():
            bs_tele = _backstop_uncovered_pfs(
                new_ufs, new_pfs, dev_to_product, developer_features,
                set(pf_tele.get("promoted_cap_names") or []),
                flow_owner_override,
            )
            tele.update(bs_tele)
        # Shared Platform may own CODE, never JOURNEYS (operator 2026-07-05,
        # validator I10). Reassign every UF still on the shared/platform
        # capability to the non-shared PF owning the plurality of its member
        # flows. Runs AFTER the backstop (so a backstop-synthesized residual
        # journey is caught too) and INSIDE _finish -> identical on the live
        # and cache-hit replay paths.
        if _uf_reshare_enabled():
            rs_tele = _reassign_shared_ufs(
                new_ufs, developer_features, dev_to_product,
                flow_owner_override, new_pfs=new_pfs, routes_index=routes_index,
                anchored=anchored)
            tele.update(rs_tele)
        # Deterministic output ordering (Phase 1 stability): the LLM emits
        # features/flows in an order that drifts run-to-run. Sort by a stable key
        # so the output array order never churns — applies identically to the
        # live and cache-hit paths, preserving the byte-identical-replay invariant.
        # Sort by CONTENT-derived keys only, then renumber UF ids from the sorted
        # position — ids become content-stable across independent runs instead of
        # re-encoding the LLM's drifting emission order.
        new_pfs.sort(key=lambda p: ((getattr(p, "name", "") or "").lower(), getattr(p, "name", "") or ""))
        new_ufs.sort(key=lambda u: ((getattr(u, "name", "") or "").lower(), str(getattr(u, "resource", "") or "")))
        for i, u in enumerate(new_ufs, start=1):
            u.id = f"UF-{i:03d}"
        tele.update({
            "applied": True, "fallback": None,
            "uf_after": len(new_ufs), "pf_after": len(new_pfs),
            "files_after": files_after, "dev_mapped": len(dev_map),
            "dev_total": len(developer_features),
            # Post-A1 "omitted from the map" ≠ "landed in the residual" (token
            # rescue diverges them) — residual_devs mirrors the ACCURATE
            # devs_residual count so the blob signal stays honest.
            "residual_devs": pf_tele.get("devs_residual", 0),
            **pf_tele, **uf_tele,
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
                tele["abstraction_contract"] = cached.get("contract", CONTRACT_PASS)
                # Replay reports the SAME retry observability the live run
                # did (absent for pre-recording cache entries — no bump).
                if isinstance(cached.get("first_draw_spec"), dict):
                    tele["first_draw_spec"] = cached["first_draw_spec"]
                try:
                    result = _finish(c_abs, c_map)
                except Exception:  # noqa: BLE001 — malformed cache must never crash
                    result = None
                if result is not None:
                    return result
                tele["cache_hit"] = False  # malformed → fall through to live call
                tele.pop("first_draw_spec", None)  # stale restore must not leak

    cli = client if client is not None else _client_factory()
    if cli is None:
        return _degrade("no_client")

    # ── Call 1 — abstraction / grain-lift (Sonnet). Prompt selection:
    # ANCHORED (Wave 2b, PF list fixed) > ALIGN (opt-in) > free-gen.
    if anchored:
        sys1 = _ANCHORED_SYSTEM + (
            _INTERIOR_ADDENDUM if interior_attached else "")
        anchor_block = ""
    elif aligned:
        sys1 = _ALIGN_SYSTEM
        anchor_block = ("\n\nAUTHORITATIVE product_capability_anchors "
                        "(align the evidence to these; use verbatim as names):\n"
                        + json.dumps(anchor_texts, ensure_ascii=False))
    else:
        sys1 = _ABSTRACTION_SYSTEM
        anchor_block = ""
    def _user_prompt(digest_: dict[str, Any]) -> str:
        """Call-1 user message for ``digest_`` — ONE template shared by the
        primary draw and the W4.1 v1-fallback so the two can never drift."""
        return ("Repository evidence (code-grounded, no README):\n```json\n"
                + json.dumps(digest_, ensure_ascii=False) + "\n```" + anchor_block
                + "\nEmit the JSON now.")

    user1 = _user_prompt(digest)

    # Sanitise at the boundary: keep only specs whose "name" is a non-empty
    # string. An LLM can emit a numeric/None name in otherwise-valid JSON, and
    # every downstream consumer calls ``.strip()``/``.get()`` on it — dropping
    # them here means no reconstruction path can raise (never-worse). If this
    # empties a list, degrade rather than proceed on garbage.
    def _valid_spec(s: Any) -> bool:
        return (isinstance(s, dict) and isinstance(s.get("name"), str)
                and bool(s.get("name").strip()))

    # Phase-0 decision logging (Wave 2a): the prompt is logged as a HASH,
    # the parsed outcome as a decision record — 6.7d Call 1/2 are exactly
    # the membership/naming oracle the training spec targets.
    from faultline.llm.decision_log import digest_hash as _dhash
    from faultline.llm.decision_log import log_decision as _dlog

    def _draw(
        system: str, user: str, *, role: str = "journey_abstraction_draw",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
        """One Call-1 draw → sanitised ``(uf_specs, pf_specs, fail_reason)``.
        ``fail_reason`` is None on a usable draw. ``role`` distinguishes the
        W4.1 brittleness redraws in the cost/decision logs."""
        in_hash = _dhash(system, user)
        text, in_t, out_t = _call_haiku(
            cli, model=abstraction_model, system=system, user=user,
            max_tokens=ABSTRACTION_MAX_TOKENS, llm_health=llm_health)
        tele["llm_calls"] += 1
        tele["cost_usd"] = round(
            tele["cost_usd"]
            + _record(abstraction_model, in_t, out_t,
                      {"role": role,
                       "input_digest_hash": in_hash}),
            6,
        )
        parsed = _parse_json(text)
        if not parsed:
            return [], [], "abstraction_parse_failed"
        ufs = [s for s in (parsed.get("user_flows") or []) if _valid_spec(s)]
        pfs = [s for s in (parsed.get("product_features") or []) if _valid_spec(s)]
        if not ufs or not pfs:
            return [], [], "abstraction_empty"
        try:  # decision tap — names/counts only, never content
            _dlog(
                role=role,
                model=abstraction_model,
                input_digest_hash=in_hash,
                decision={
                    "uf_specs": len(ufs), "pf_specs": len(pfs),
                    "pf_names": [
                        (s.get("name") or "").strip() for s in pfs
                    ][:80],
                },
            )
        except Exception:  # noqa: BLE001 — tap must never break the stage
            pass
        return ufs, pfs, None

    # ── First draw + grain-contract gate (2026-07-04) ──────────────────
    # A draw that emits >= UF_CONTRACT_RATIO x the digest UFs performed no
    # grain lift (inbox-zero: 140-in -> 140 emitted -> F1 36.7 vs golden 35
    # journeys) — the RATIO prong. The JPF prong (lever #3) additionally arms
    # on draws under that ratio which still inflate BOTH the journey axis
    # (emitted > distinct digest resources) and the capability axis (emitted
    # PFs > deterministic PF count) — see :func:`_jpf_armed`. Reject ONCE with
    # a corrective addendum; if the retry still fails its armed prongs we KEEP
    # the retry (never-worse: more UFs beats degrading the whole stage) and
    # flag ``abstraction_contract`` so the uncompressed draw is visible in
    # scan_meta.
    uf_specs, pf_specs, fail1 = _draw(sys1, user1)
    # The prompt the KEPT draw engaged — the grain-contract corrective retry
    # below must re-engage the SAME prompt (the v1 one when the W4.1 fallback
    # won), never the prompt of a draw that already failed.
    sys_active, user_active = sys1, user1
    used_v1_fallback = False
    if fail1:
        # ── W4.1 — first-draw brittleness ladder (soc0f forensics) ──────
        # A single empty/unparseable Sonnet draw used to degrade the WHOLE
        # stage (wave4 Soc0: one `abstraction_empty` draw silently swapped
        # the scored journey layer for the raw 6.7 rollup — +32 phantom
        # validator rows). Cheapest rung first, degrade stays the last
        # resort with its reason vocabulary unchanged:
        #   1. retry ONCE, same prompt (transient-draw class);
        #   2. interior-evidence runs only: fall back to the evidence-less
        #      v1 prompt + cache namespace — the wave31-proven path, which
        #      REPLAYS its warm cache before paying for a live draw;
        #   3. degrade (existing never-worse exit).
        # Every redraw is cap-guarded like the contract retry: current
        # spend + one mean-cost draw must fit under COST_CAP_USD.
        if tele["cost_usd"] * 2 > COST_CAP_USD:
            return _degrade(fail1)
        tele["retry_used"] = 1
        uf_specs, pf_specs, fail2 = _draw(
            sys1, user1, role="journey_abstraction_retry")
        if fail2:
            if not interior_attached:
                return _degrade(fail2)
            # v1 digest: the sections riders were APPENDED to the PF lines
            # by the attach loop, so filtering the key out restores the
            # exact pre-W4 line dicts (insertion order included) → the
            # json.dumps bytes, the prompt and the v1 cache key all match
            # what an evidence-less run would compute.
            v1_digest = dict(digest)
            v1_digest["current_product_features"] = [
                {k: v for k, v in line.items() if k != "sections"}
                for line in digest["current_product_features"]
            ]
            v1_key = _cache_key(v1_digest, abstraction_model,
                                reattrib_model, "spine-anchored-mint-v1")
            v1_user = _user_prompt(v1_digest)
            sys_active, user_active = _ANCHORED_SYSTEM, v1_user
            # 2a. Replay the v1 namespace when warm ($0, byte-identical to
            # the evidence-less run that recorded it — same replay shape as
            # the primary lookup: cached specs are already post-scrub).
            v1_hit: Any = None
            if cache is not None:
                try:
                    v1_hit = cache.get(CacheKind.LLM_ABSTRACTION.value, v1_key)
                except Exception:  # noqa: BLE001 — cache fault ≠ stage abort
                    v1_hit = None
            if isinstance(v1_hit, dict) and v1_hit.get("v") == ABSTRACTION_CACHE_VERSION:
                c_abs = v1_hit.get("abstraction") or {}
                c_map = {k: v for k, v in (v1_hit.get("map") or {}).items()
                         if isinstance(k, str) and isinstance(v, str)}
                if c_abs.get("user_flows") and c_abs.get("product_features") and c_map:
                    tele["abstraction_contract"] = v1_hit.get(
                        "contract", CONTRACT_PASS)
                    if isinstance(v1_hit.get("first_draw_spec"), dict):
                        tele["first_draw_spec"] = v1_hit["first_draw_spec"]
                    try:
                        result = _finish(c_abs, c_map)
                    except Exception:  # noqa: BLE001 — malformed cache must never crash
                        result = None
                    if result is not None:
                        # Warm THIS config's own namespace so a re-scan
                        # replays at the primary lookup instead of
                        # re-buying the two failed draws.
                        if cache is not None:
                            try:
                                cache.set(CacheKind.LLM_ABSTRACTION.value,
                                          key, v1_hit)
                            except Exception:  # noqa: BLE001 — cache write never aborts
                                pass
                        tele["fallback"] = "v1_prompt"
                        return result
                    tele.pop("first_draw_spec", None)  # stale restore must not leak
            # 2b. Live draw on the v1 prompt (cap-guarded like any redraw).
            if tele["cost_usd"] * 3 > COST_CAP_USD * 2:
                return _degrade(fail2)
            uf_specs, pf_specs, fail3 = _draw(
                _ANCHORED_SYSTEM, v1_user,
                role="journey_abstraction_v1_fallback")
            if fail3:
                return _degrade(fail3)
            used_v1_fallback = True
    n_digest_ufs = len(digest["current_user_flows"])
    n_digest_pfs = len(digest.get("current_product_features") or [])
    tele["uf_specs_emitted"] = len(uf_specs)

    def _armed_prongs(ufs_: list[dict[str, Any]], pfs_: list[dict[str, Any]],
                      restrict: list[str] | None = None) -> list[str]:
        """Arming reasons for a draw; ``restrict`` (the retry re-check) only
        re-evaluates the prongs that armed the FIRST draw, so a ratio-only
        arming keeps the exact lever-1 pass_after_retry semantics."""
        out: list[str] = []
        if ((restrict is None or CONTRACT_ARMED_RATIO in restrict)
                and _contract_gate_armed(digest)
                and not _contract_ok(len(ufs_), n_digest_ufs)):
            out.append(CONTRACT_ARMED_RATIO)
        if ((restrict is None or CONTRACT_ARMED_JPF in restrict)
                and _jpf_armed(ufs_, pfs_, digest)):
            out.append(CONTRACT_ARMED_JPF)
        return out

    # jpf telemetry (always on a live draw, armed or not): the draw's
    # journeys-per-capability vs the repo's structural prior
    # (distinct flow resources per deterministic product feature).
    tele["jpf_draw"] = round(len(uf_specs) / max(_distinct_pf_count(pf_specs), 1), 3)
    tele["jpf_prior"] = round(
        len(_digest_resource_keys(digest)) / max(n_digest_pfs, 1), 3)

    contract = CONTRACT_PASS
    armed = _armed_prongs(uf_specs, pf_specs)
    if armed:
        tele["contract_armed_by"] = armed
        # A retry costs ≈ the first draw; skip it when a second same-shape
        # call could bust the whole-stage cost cap (structural x2, not tuned).
        if tele["cost_usd"] * 2 > COST_CAP_USD:
            contract = CONTRACT_UNCOMPRESSED
            tele["abstraction_retry_skipped_cost"] = True
        else:
            tele["abstraction_retried"] = True
            # Ratio-armed draws keep the VALIDATED merge corrective verbatim
            # (documenso +8.5); the jpf corrective serves the jpf-only class.
            corrective = (_MERGE_CORRECTIVE if CONTRACT_ARMED_RATIO in armed
                          else _JPF_CORRECTIVE)
            r_ufs, r_pfs, r_fail = _draw(sys_active + corrective, user_active)
            if r_fail is None:
                # Persist the FIRST draw's parsed-spec summary BEFORE the
                # retry replaces it — the only moment both candidates exist
                # (mission-92: closes the NO-DATA gap that blocked offline
                # both-candidate calibration). Recording only; the retry-keep
                # decision below is untouched. (When the retry is unusable
                # the first draw IS the kept result — nothing extra to save.)
                tele["first_draw_spec"] = _first_draw_summary(uf_specs, pf_specs)
                # Per spec: keep the RETRY result either way; flag when it
                # still failed the prongs that armed it.
                uf_specs, pf_specs = r_ufs, r_pfs
                tele["uf_specs_emitted_retry"] = len(r_ufs)
                contract = (CONTRACT_PASS_AFTER_RETRY
                            if not _armed_prongs(r_ufs, r_pfs, restrict=armed)
                            else CONTRACT_UNCOMPRESSED)
            else:
                # Retry unusable → the valid FIRST draw stands (never-worse).
                contract = CONTRACT_UNCOMPRESSED
                tele["abstraction_retry_failed"] = r_fail
    tele["abstraction_contract"] = contract
    if anchored:
        # THE CAPABILITY LIST IS FIXED: keep the draw's descriptions for
        # capabilities it cited VERBATIM (slug match), drop inventions,
        # and guarantee every anchored capability is present so the
        # reconstruction's ordered_caps covers the full anchored set
        # (deterministic descriptions from the mint fill the gaps).
        by_slug = {_slug(n): n for n in anchored_names}
        kept: list[dict[str, Any]] = []
        cited: set[str] = set()
        dropped = 0
        for s in pf_specs:
            slug = _slug(str(s.get("name") or ""))
            canon = by_slug.get(slug)
            if canon is None:
                dropped += 1
                continue
            if canon not in cited:
                cited.add(canon)
                kept.append({"name": canon,
                             "description": s.get("description") or ""})
        for name in sorted(anchored_names):
            if name not in cited:
                pf = anchored_pf_by_name.get(name)
                kept.append({
                    "name": name,
                    "description": getattr(pf, "description", None) or "",
                })
        tele["anchored_pf_invented_dropped"] = dropped
        pf_specs = kept
        # Re-slug UF citations onto the fixed list (review F2 — this was
        # a comment with no code): a journey citing a NON-anchored
        # capability (incl. the "Shared Platform" string the retired
        # Call-2 sink taught the models) gets its citation CLEARED — the
        # conservation law / emission integrity / terminal home then
        # place it by ownership evidence. Never let an invented name
        # reach the reshare ladder as a shared-platform attachment.
        scrubbed = 0
        for u_spec in uf_specs:
            cited = str(u_spec.get("product_feature") or "")
            if cited and by_slug.get(_slug(cited)) is None:
                u_spec["product_feature"] = ""
                scrubbed += 1
        tele["anchored_uf_citations_scrubbed"] = scrubbed
    parsed1 = {"user_flows": uf_specs, "product_features": pf_specs}
    if tele["cost_usd"] > COST_CAP_USD:
        return _degrade(f"cost_cap ${tele['cost_usd']:.4f}")

    if anchored:
        # ── Call 2 RETIRED (Product-Spine §4.3): dev→PF is the anchored
        # mint's lineage stamps — deterministic, total over lineage devs,
        # no LLM call, no Shared Platform sink. Lane residents are absent
        # from the map by construction.
        dev_map = dict(anchored_dev_map)
        if cache is not None:
            try:
                cache_payload = {
                    "v": ABSTRACTION_CACHE_VERSION,
                    "abstraction": {"product_features": pf_specs,
                                    "user_flows": uf_specs},
                    "map": dev_map,
                    "contract": tele.get("abstraction_contract", CONTRACT_PASS),
                }
                if "first_draw_spec" in tele:
                    cache_payload["first_draw_spec"] = tele["first_draw_spec"]
                cache.set(CacheKind.LLM_ABSTRACTION.value, key, cache_payload)
            except Exception:  # noqa: BLE001 — cache faults never abort
                pass
        try:
            result = _finish(parsed1, dev_map)
        except Exception:  # noqa: BLE001
            return _degrade("reconstruct_exception")
        if result is None:
            return _degrade("reconstruct_empty")
        if used_v1_fallback:
            # AFTER _finish (which stamps fallback=None on success): the
            # stage applied, but via the evidence-less v1 prompt — visible
            # in scan_meta without flipping `applied`.
            tele["fallback"] = "v1_prompt"
        return result

    # ── Call 2 — dev → capability re-attribution (Haiku / passed model) ─
    caps = [s.get("name", "").strip() for s in pf_specs if s.get("name", "").strip()]
    caps_with_residual = caps + [_RESIDUAL_CAP]
    dev_items, dev_items_header = _reattrib_dev_items(dev_view)
    user2 = ("Product capabilities:\n" + json.dumps(caps_with_residual) +
             "\n\n" + dev_items_header +
             json.dumps(dev_items, ensure_ascii=False) +
             "\n\nReturn the full map now (every dev feature mapped).")
    in2_hash = _dhash(_REATTRIB_SYSTEM, user2)
    text2, in2, out2 = _call_haiku(
        cli, model=reattrib_model, system=_REATTRIB_SYSTEM, user=user2,
        max_tokens=REATTRIB_MAX_TOKENS, llm_health=llm_health)
    tele["llm_calls"] += 1
    tele["cost_usd"] = round(
        tele["cost_usd"]
        + _record(reattrib_model, in2, out2,
                  {"role": "dev_reattribution",
                   "input_digest_hash": in2_hash}),
        6,
    )
    parsed2 = _parse_json(text2)
    dev_map_raw = (parsed2 or {}).get("map") or {}
    dev_map = {k: v for k, v in dev_map_raw.items() if isinstance(k, str) and isinstance(v, str)}
    try:  # Phase-0 decision tap — the RC1 oracle's full verdict (names only)
        _dlog(
            role="dev_reattribution",
            model=reattrib_model,
            input_digest_hash=in2_hash,
            candidates=caps_with_residual,
            decision=dev_map,
        )
    except Exception:  # noqa: BLE001 — tap must never break the stage
        pass
    # Re-attribution failed entirely (LLM error / bad JSON / health-blocked):
    # without it every dev feature would collapse into the Shared Platform
    # residual, emitting a degenerate single-blob product layer. Degrade fully
    # — return the ORIGINAL arrays unchanged (never-worse invariant).
    if not dev_map:
        return _degrade("reattrib_failed")

    # ── Persist the two structured outputs for byte-identical replay ──
    if cache is not None:
        try:
            cache_payload: dict[str, Any] = {
                "v": ABSTRACTION_CACHE_VERSION,
                "abstraction": {"product_features": pf_specs, "user_flows": uf_specs},
                "map": dev_map,
                # Persisted so a cache-hit replay reports the SAME contract
                # status the live run did (byte-identical telemetry).
                "contract": tele.get("abstraction_contract", CONTRACT_PASS),
            }
            if "first_draw_spec" in tele:
                # Alongside the kept result, so offline both-candidate
                # calibration can read the cache without the run dir.
                # Extra key is version-compatible (reader checks "v" only).
                cache_payload["first_draw_spec"] = tele["first_draw_spec"]
            cache.set(CacheKind.LLM_ABSTRACTION.value, key, cache_payload)
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
