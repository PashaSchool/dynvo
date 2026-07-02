"""Stage 8 — Layer 2 marketing-grounded clusterer (Sprint E1).

The deterministic Stage 6.5 clusterer ships generic anchor labels
(``Billing`` / ``Auth`` / ``Email`` / …). Marketing truth uses
product-grain labels (``HTTP Uptime Monitoring`` / ``Multi-Region
Probing`` / ``Status Pages``). The mismatch is the precision drag.

Stage 8 closes that gap by:

  1. Discovering the maintainer's PUBLIC marketing site from
     ``package.json#homepage`` (NOT README per ``CLAUDE.md`` rule).
  2. Fetching the page and extracting candidate product labels via
     :mod:`faultline.analyzer.marketing_fetcher`.
  3. Asking Haiku for a single batched mapping of
     ``developer_feature_name → product_label_or_null`` grounded in
     the fetched taxonomy.
  4. Rebuilding the product-feature list from that mapping.

Cascade (highest confidence first):

    customer-yaml (faultlines.yaml)  →  conf 1.00
    marketing+haiku                  →  conf 0.85
    deterministic Stage 6.5 result   →  conf 0.60 (passthrough)

The customer YAML check is delegated to Stage 6.5 (which already
runs first and handles ``faultlines.yaml``). When Stage 6.5
returned product features that came from ``rule:customer-yaml``,
Stage 8 short-circuits — the customer has already declared the
mapping.

Marketing data is cached at ``~/.faultline/marketing-cache/<slug>.json``
with a 7-day TTL. This is OK under the cold-scan rule because the
cache is keyed on EXTERNAL content (not per-repo scan state), just
like ``llm-cache/``. A fresh scan of a repo the engine has never
seen still produces correct results — the cache only short-circuits
the network call.

NO README reads. NO new dependencies (urllib + anthropic SDK already
in use).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.analyzer.marketing_fetcher import (
    MarketingTaxonomy,
    discover_marketing_site,
    extract_docs_sidebar_taxonomy,
    extract_product_taxonomy,
    fetch_llms_txt_urls,
    fetch_page_text,
    fetch_sitemap_urls,
    parse_llms_txt,
    rank_sitemap_urls_by_product_likelihood,
)
from faultline.cache.backend import CacheKind
from faultline.llm.cost import CostTracker, deterministic_params
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.product_strings import (
    ProductStringIndex,
    collect_product_strings,
)

if TYPE_CHECKING:
    from faultline.cache.backend import CacheBackend
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)
from faultline.llm.model_gateway import resolve_model as gateway_model


# ── Constants ───────────────────────────────────────────────────────────

# Marketing-page extractions are cached (content-keyed by slug, 7-day
# TTL) through the pluggable cache backend under the ``marketing`` kind
# so consecutive scans don't hammer public sites. The on-disk layout
# (``<base>/marketing-cache/<slug>.json``) is preserved by
# ``FilesystemCacheBackend``.

# Conservative TTL — marketing pages change rarely; 7 days lets daily
# CI scans of the same repo stay fast.
_CACHE_TTL_SECONDS = 7 * 24 * 3600

# Below this taxonomy size the marketing fetch is treated as
# insufficient signal and we fall back to the deterministic result.
_MIN_TAXONOMY_SIZE = 3

# Haiku call constraints.
_MAX_DEV_FEATURES_IN_PROMPT = 80
_MAX_PATHS_PER_FEATURE = 5
# Product strings per feature line — same per-feature evidence budget
# as _MAX_PATHS_PER_FEATURE (structural, not corpus-tuned).
_MAX_PRODUCT_STRINGS_PER_FEATURE = 5
_MAX_TAXONOMY_ENTRIES_IN_PROMPT = 25
_HAIKU_MAX_TOKENS = 2000

# Confidence floor stamped on Stage 8 results when marketing+haiku
# succeeds. Higher than deterministic (0.6) but lower than customer
# override (1.0) to preserve the cascade.
_CONF_MARKETING_HAIKU = 0.85

# ── Content-hash LLM cache (deterministic short-circuit) ────────────────
#
# Every Stage-8 LLM call on the product-cluster path (this module's Haiku
# label-mapper AND the Sonnet analyst / rename-retry calls in
# ``stage_8_analyst`` — which imports these helpers) is a pure function of
# its input: the system prompt + the user prompt + the canonical model id.
# We cache the PARSED structured output keyed on a sha256 of exactly those
# inputs (CacheKind.LLM_PRODUCT_CLUSTER), so a re-scan of an unchanged repo
# REPLAYS the identical mapping/analysis ($0) through the SAME
# validation/emission code — byte-identical ``product_features[]`` +
# ``product_feature_id`` stamps. This matters doubly for the analyst, whose
# Sonnet call does NOT use deterministic sampling params. Content-keyed
# (same input → same answer): a deterministic short-circuit, NOT per-repo
# memory — compliant with rule-cold-scan. NOT the marketing-page cache
# (kind ``marketing``), which is slug-keyed with a 7-day TTL and stays
# as-is. Default ON; opt out via ``FAULTLINE_STAGE_8_CACHE=0``.
#
# STAGE_8_CACHE_VERSION is the manual invalidation lever required by
# rule-cache-invalidation: bump it whenever a prompt template, the parse
# logic, or the cached-value shape changes in a way that must NOT serve a
# stale answer. (The system prompt is ALSO hashed into the key, but the
# version constant is the documented, explicit control surface.)
STAGE_8_CACHE_VERSION = "v1"

_LLM_CACHE_ENV = "FAULTLINE_STAGE_8_CACHE"


def llm_cache_enabled() -> bool:
    """Default ON — set ``FAULTLINE_STAGE_8_CACHE=0`` to opt out."""
    return os.environ.get(_LLM_CACHE_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def llm_cache_key(model: str, system: str, user: str) -> str:
    """Content-hash key for one Stage-8 LLM call.

    Components: cache version + canonical model id (pre-gateway) + the
    system prompt + the full user prompt (dev-feature digest + taxonomy /
    analyst payload / rename entries — the exact structured input).
    Deliberately EXCLUDED: run_id, timestamps, clone dir, thread identity,
    or any other run-varying value.
    """
    payload = json.dumps(
        {
            "version": STAGE_8_CACHE_VERSION,
            "model": model,
            "system": system,
            "user": user,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def llm_cache_get(cache: "CacheBackend", key: str, field: str) -> Any | None:
    """Read the stored parsed output under ``field``. ``None`` on miss,
    version mismatch, malformed entry, or ANY backend fault — a cache
    problem must never abort the stage (never-worse)."""
    try:
        stored = cache.get(CacheKind.LLM_PRODUCT_CLUSTER.value, key)
    except Exception as exc:  # noqa: BLE001 — cache must never break a scan
        logger.warning("stage_8: llm cache get failed: %s", exc)
        return None
    if not isinstance(stored, dict) or stored.get("v") != STAGE_8_CACHE_VERSION:
        return None
    return stored.get(field)


def llm_cache_put(cache: "CacheBackend", key: str, field: str, value: Any) -> None:
    """Persist a parsed output under ``field``. Failures are logged +
    swallowed. Only SUCCESSFUL parses are ever stored (callers guarantee
    it) so a transient outage never poisons future reproducible replays."""
    try:
        cache.set(
            CacheKind.LLM_PRODUCT_CLUSTER.value,
            key,
            {"v": STAGE_8_CACHE_VERSION, field: value},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage_8: llm cache set failed: %s", exc)


# ── Public types ────────────────────────────────────────────────────────


@dataclass
class Stage8Result:
    """Outcome of Stage 8 — telemetry + new product feature list.

    ``member_flows_map`` (added Sprint S6.3) maps PF slug → list of
    flow names the Sonnet analyst attributed to that PF. Consumed by
    ``stage_8_rollup_flows`` for the OssLibrary / FrameworkRepo
    strategies (which refuse to use path overlap). Empty dict means
    no map was produced — strategies degrade gracefully (oss-library
    attaches nothing; framework-repo falls back to entry-point-in-paths).
    """

    product_features: list[Any]  # list[Feature] but TYPE_CHECKING import
    dev_to_product_map: dict[str, tuple[str, ...]]
    telemetry: dict[str, Any]
    member_flows_map: dict[str, list[str]] = field(default_factory=dict)


# ── Cache helpers ───────────────────────────────────────────────────────


def _cache_key(slug: str) -> str:
    """The ``marketing`` cache key — sanitised slug, unchanged from the
    legacy ``marketing-cache/<slug>.json`` filename so dev caches hit."""
    return re.sub(r"[^A-Za-z0-9._\-]", "_", slug or "unknown")


def _resolve_marketing_backend(
    cache_backend: "CacheBackend | None",
) -> "CacheBackend":
    if cache_backend is not None:
        return cache_backend
    from faultline.cache import get_cache_backend

    return get_cache_backend()


def _load_cached_taxonomy(
    slug: str, *, cache_backend: "CacheBackend | None" = None,
) -> MarketingTaxonomy | None:
    """Return the cached taxonomy if present + fresh, else None."""
    backend = _resolve_marketing_backend(cache_backend)
    raw = backend.get("marketing", _cache_key(slug))
    if not isinstance(raw, dict):
        return None
    fetched_at = raw.get("fetched_at_epoch")
    if not isinstance(fetched_at, (int, float)):
        return None
    if time.time() - float(fetched_at) > _CACHE_TTL_SECONDS:
        return None
    pf = raw.get("product_features") or []
    if not isinstance(pf, list):
        return None
    return MarketingTaxonomy(
        repo_slug=str(raw.get("repo_slug") or slug),
        source_url=str(raw.get("source_url") or ""),
        fetched_at=str(raw.get("fetched_at") or ""),
        product_features=tuple(str(x) for x in pf if isinstance(x, str)),
        confidence=float(raw.get("confidence") or 0.0),
        notes=str(raw.get("notes") or ""),
    )


def _write_cache(
    taxonomy: MarketingTaxonomy, *, cache_backend: "CacheBackend | None" = None,
) -> None:
    """Persist taxonomy to cache. Failures are logged + swallowed."""
    backend = _resolve_marketing_backend(cache_backend)
    backend.set("marketing", _cache_key(taxonomy.repo_slug), {
        "repo_slug": taxonomy.repo_slug,
        "source_url": taxonomy.source_url,
        "fetched_at": taxonomy.fetched_at,
        "fetched_at_epoch": time.time(),
        "product_features": list(taxonomy.product_features),
        "confidence": taxonomy.confidence,
        "notes": taxonomy.notes,
    })


# ── Marketing fetch (cached) ────────────────────────────────────────────


def _candidate_urls(primary: str) -> list[str]:
    """Given a primary marketing URL, derive sensible fallbacks.

    M1 expansion — the fallback chain now covers more conventional
    product-page slugs and the docs subdomain. We harvest the union of
    labels across ALL these pages rather than first-to-clear (some
    SPAs serve identical HTML on every route, but real product
    sub-pages like ``/platform``, ``/use-cases`` carry distinct
    headings).

    Order:
        1. ``<primary>``               # homepage
        2. ``<primary>/features``      # explicit features page
        3. ``<primary>/pricing``       # tiers usually list capabilities
        4. ``<primary>/docs``          # in-site docs landing
        5. ``<primary>/product``       # alt product overview slug
        6. ``<primary>/platform``      # alt product overview slug
        7. ``<primary>/solutions``     # vertical-specific landing
        8. ``<primary>/use-cases``     # capability-by-scenario page
        9. ``https://docs.<host>``     # docs subdomain (sidebar nav)
    """
    out: list[str] = [primary]
    base = primary.rstrip("/")
    out.append(f"{base}/features")
    out.append(f"{base}/pricing")
    out.append(f"{base}/docs")
    out.append(f"{base}/product")
    out.append(f"{base}/platform")
    out.append(f"{base}/solutions")
    out.append(f"{base}/use-cases")
    # docs.<host> fallback
    import urllib.parse as _urlparse
    parsed = _urlparse.urlparse(primary)
    host = parsed.netloc
    if host and not host.startswith("docs."):
        # strip "www." if present so we don't try docs.www.x.com
        host_clean = host.removeprefix("www.")
        out.append(f"{parsed.scheme}://docs.{host_clean}")
    # Dedupe preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for u in out:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    return deduped


# Maximum pages we harvest in one call. Direct candidates burn most of
# this budget; sitemap-driven enrichment fills the rest.
_MAX_HARVEST_PAGES = 18

# Cap union taxonomy at this size. Mirrors ``extract_product_taxonomy``
# per-page cap so the Haiku prompt stays bounded.
_TAXONOMY_UNION_CAP = 30


def fetch_marketing_taxonomy(
    repo_path: Path,
    repo_slug: str,
    *,
    cache_ttl_seconds: int = _CACHE_TTL_SECONDS,
    use_cache: bool = True,
    cache_backend: "CacheBackend | None" = None,
) -> MarketingTaxonomy | None:
    """Top-level entry — discover + fetch + parse, with caching.

    M1 algorithm — multi-page UNION harvest with sitemap.xml fallback:

      1. Discover canonical URL (``discover_marketing_site``).
      2. Visit each direct candidate URL (homepage, /features,
         /pricing, /docs, /product, /platform, /solutions,
         /use-cases, docs.<host>). UNION all acceptable labels.
      3. If union is already strong (≥6 labels), return.
      4. Otherwise fetch ``<primary>/sitemap.xml`` and rank its URLs
         by product-likelihood. Harvest the top 8-10.
      5. Return the union (capped at 30).

    Returns ``None`` when no marketing surface is reachable. Callers
    must handle that gracefully (fall back to Stage 6.5 deterministic
    result).

    Pitch-sentence labels are filtered upstream by
    ``_is_acceptable_label`` (which now drops verb-prefix imperatives
    like "Turn X into Y").
    """
    if use_cache:
        cached = _load_cached_taxonomy(repo_slug, cache_backend=cache_backend)
        if cached is not None:
            return cached

    primary = discover_marketing_site(repo_path)
    if not primary:
        return None

    union: list[str] = []
    seen: set[str] = set()
    urls_visited: list[str] = []
    best_url = primary  # remembered for telemetry / source_url

    def _add(c: str) -> bool:
        k = c.lower()
        if k in seen:
            return False
        seen.add(k)
        union.append(c)
        return True

    def _harvest(url: str) -> int:
        if url in urls_visited or len(urls_visited) >= _MAX_HARVEST_PAGES:
            return 0
        urls_visited.append(url)
        html = fetch_page_text(url)
        if not html:
            return 0

        added = 0
        # Sprint v7-A: on /docs and docs.<host> pages prefer the
        # sidebar parser (with section-header rollup) before falling
        # back to the generic H1/H3 + <li><a> harvester. The sidebar
        # carries the maintainer's curated taxonomy and naturally
        # groups sub-features under their section.
        if "/docs" in url or "://docs." in url:
            sidebar, _scf = extract_docs_sidebar_taxonomy(html)
            for c in sidebar:
                if _add(c):
                    added += 1

        # Always run the generic harvester too — it catches feature
        # callouts on marketing pages that aren't strictly sidebars.
        candidates, _conf = extract_product_taxonomy(html)
        for c in candidates:
            if _add(c):
                added += 1
        return added

    # Sprint v7-A: Step 0 — try llms.txt / llms-full.txt first.
    # llms.txt is the maintainer's curated product taxonomy explicitly
    # designed for LLM consumption. When present it carries far higher
    # signal than scraped HTML and naturally rolls 35 OAuth providers
    # under "OAuth Providers" via the ## section-header convention.
    llms_added_any = False
    for llms_url in fetch_llms_txt_urls(primary):
        if len(urls_visited) >= _MAX_HARVEST_PAGES:
            break
        urls_visited.append(llms_url)
        text = fetch_page_text(llms_url, timeout_s=10)
        if not text:
            continue
        labels, _lconf = parse_llms_txt(text)
        if not labels:
            continue
        for label in labels:
            if _add(label):
                llms_added_any = True
        if llms_added_any:
            best_url = llms_url
        # llms.txt is authoritative; one good file is enough.
        if len(union) >= 6:
            break

    # Step 1: direct candidates
    for url in _candidate_urls(primary):
        added = _harvest(url)
        if added > 0:
            # Track the FIRST URL that contributed labels — used as
            # ``source_url`` since it's the most-likely real surface.
            if best_url == primary and url != primary:
                best_url = url

    # Step 2: sitemap-driven enrichment if union still weak
    if len(union) < 6:
        sitemap_urls = fetch_sitemap_urls(primary)
        if sitemap_urls:
            ranked = rank_sitemap_urls_by_product_likelihood(sitemap_urls)
            for url in ranked[:10]:
                _harvest(url)
                if len(union) >= 8:
                    break

    if not union:
        return None

    # Cap union size; assign confidence per the existing ladder.
    capped = union[:_TAXONOMY_UNION_CAP]
    if len(capped) >= 6:
        conf = 0.9
    elif len(capped) >= 3:
        conf = 0.75
    elif len(capped) >= 1:
        conf = 0.5
    else:
        conf = 0.0

    taxonomy = MarketingTaxonomy(
        repo_slug=repo_slug,
        source_url=best_url,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        product_features=tuple(capped),
        confidence=conf,
        notes=(
            f"harvested {len(capped)} labels across {len(urls_visited)} "
            f"pages (M1 union)"
        ),
    )
    if use_cache:
        _write_cache(taxonomy, cache_backend=cache_backend)
    return taxonomy


# ── Haiku mapping call ──────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You map code-grounded developer features to marketing-grounded "
    "product taxonomy entries. "
    "Each developer feature has a kebab-case slug and a small sample "
    "of file paths. Each product taxonomy entry is a title-case label "
    "from the maintainer's public marketing site. "
    "For every developer feature, choose the ONE taxonomy entry that "
    "best describes the user-visible product capability it implements, "
    "or null when no entry fits. "
    "Do NOT invent taxonomy labels. "
    "Output STRICT JSON of the shape "
    '{"mappings": [{"developer": "<slug>", "product": "<Label>" | null}, ...]}. '
    "No prose, no markdown, no commentary."
)


def _build_user_prompt(
    developer_features: list["Feature"],
    taxonomy: MarketingTaxonomy,
    product_strings: "ProductStringIndex | None" = None,
) -> str:
    """Build the per-call user prompt — feature list + taxonomy.

    Naming-evidence core (2026-06): when a product-string index is
    supplied, each feature line additionally carries the human-facing
    strings (nav labels / page titles / i18n copy) found in ITS OWN
    files — the in-repo product vocabulary that makes the mapping
    choice evidence-grounded rather than path-guessed.
    """
    feature_lines: list[str] = []
    for f in developer_features[:_MAX_DEV_FEATURES_IN_PROMPT]:
        paths = list(f.paths or [])[:_MAX_PATHS_PER_FEATURE]
        path_summary = ", ".join(paths) if paths else "(no paths)"
        line = f"- {f.name}: {path_summary}"
        if product_strings is not None:
            strings = product_strings.bundle_for(
                f.paths or [], cap=_MAX_PRODUCT_STRINGS_PER_FEATURE,
            )
            if strings:
                line += " | product strings: " + "; ".join(strings)
        feature_lines.append(line)
    taxonomy_lines = [
        f"- {label}"
        for label in taxonomy.product_features[:_MAX_TAXONOMY_ENTRIES_IN_PROMPT]
    ]
    body = (
        f"Repository slug: {taxonomy.repo_slug}\n"
        f"Marketing source: {taxonomy.source_url}\n\n"
        "Product taxonomy (from marketing site):\n"
        + "\n".join(taxonomy_lines)
        + "\n\nDeveloper features:\n"
        + "\n".join(feature_lines)
        + "\n\nReturn JSON only."
    )
    return body


def _parse_haiku_mapping(text: str) -> dict[str, str | None]:
    """Parse Haiku's structured response into a dict.

    Returns ``{}`` on any parse failure.
    """
    if not text:
        return {}
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    if not isinstance(data, dict):
        return {}
    mappings = data.get("mappings") or []
    if not isinstance(mappings, list):
        return {}
    out: dict[str, str | None] = {}
    for entry in mappings:
        if not isinstance(entry, dict):
            continue
        dev = entry.get("developer")
        prod = entry.get("product")
        if not isinstance(dev, str) or not dev.strip():
            continue
        if prod is None:
            out[dev.strip()] = None
        elif isinstance(prod, str) and prod.strip():
            out[dev.strip()] = prod.strip()
    return out


def _call_haiku(
    client: Any,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    llm_health: LlmHealth | None = None,
) -> tuple[str, int, int]:
    """One Haiku call. Mirrors :func:`stage_4_residual._call_haiku`.

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
    except Exception as exc:  # noqa: BLE001
        if llm_health is not None and llm_health.record_failure(
            exc, stage="stage_8_marketing_clusterer",
        ):
            logger.error(
                "stage_8: LLM authentication failed — skipping all "
                "remaining LLM calls this scan: %s", exc,
            )
        else:
            logger.warning("stage_8: Haiku call failed: %s", exc)
        return "", 0, 0
    if llm_health is not None:
        llm_health.record_success()
    try:
        parts = [getattr(b, "text", "") for b in msg.content]
        text = "\n".join(p for p in parts if p)
    except Exception:  # noqa: BLE001
        text = ""
    in_t = int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0)
    out_t = int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)
    return text, in_t, out_t


def cluster_via_haiku(
    developer_features: list["Feature"],
    taxonomy: MarketingTaxonomy,
    *,
    client: Any,
    model: str,
    cost_tracker: CostTracker | None = None,
    llm_health: LlmHealth | None = None,
    product_strings: "ProductStringIndex | None" = None,
    cache: "CacheBackend | None" = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Single Haiku call: map each dev feature to a taxonomy label.

    Returns ``(mapping, telemetry)`` where ``mapping`` is
    ``{dev_name: product_label}`` — entries the model said were
    ``null`` are dropped from the mapping (they remain orphan and the
    deterministic Stage 6.5 result wins for those features).

    ``cache`` (content-hash short-circuit, CacheKind.LLM_PRODUCT_CLUSTER):
    when supplied AND ``FAULTLINE_STAGE_8_CACHE`` != 0 (default ON), an
    unchanged prompt replays its stored PARSED mapping at $0 through the
    SAME taxonomy-validation code below. Failures ARE never cached; any
    cache fault falls through to the live call (never-worse).

    Naming-evidence note: emitted PF names are constrained to the
    fetched marketing taxonomy (invented labels are rejected below), and
    the external marketing surface is an ALLOWED grounding source — so
    the anti-hallucination validator's contract is satisfied
    structurally on this path; no separate token check is needed.
    """
    if not developer_features or not taxonomy.product_features:
        return {}, {"called": False, "reason": "empty-input"}

    if cache is not None and not llm_cache_enabled():
        cache = None

    user = _build_user_prompt(developer_features, taxonomy, product_strings)

    # ── Cache lookup (content-hash short-circuit) ──
    key: str | None = None
    cached_mapping: dict[str, str | None] | None = None
    if cache is not None:
        key = llm_cache_key(model, _SYSTEM_PROMPT, user)
        raw = llm_cache_get(cache, key, "mappings")
        if isinstance(raw, dict) and raw:
            candidate = {
                k: v for k, v in raw.items()
                if isinstance(k, str) and (v is None or isinstance(v, str))
            }
            # ``_parse_haiku_mapping`` failures return ``{}`` and are never
            # stored, so an empty candidate is malformed → miss.
            cached_mapping = candidate or None

    if cached_mapping is not None:
        # HIT: no Haiku call, no tokens, $0 — the parsed mapping feeds the
        # SAME validation below, so replay is byte-identical.
        in_tokens = out_tokens = 0
        cost = 0.0
        cache_hit = True
        parsed = cached_mapping
    else:
        cache_hit = False
        text, in_tokens, out_tokens = _call_haiku(
            client,
            model=model,
            system=_SYSTEM_PROMPT,
            user=user,
            max_tokens=_HAIKU_MAX_TOKENS,
            llm_health=llm_health,
        )
        cost = 0.0
        if cost_tracker is not None and (in_tokens or out_tokens):
            rec = cost_tracker.record(
                provider="anthropic",
                model=model,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                label="stage_8_marketing_cluster",
            )
            cost = rec.cost_usd

        parsed = _parse_haiku_mapping(text)
        # MISS → persist the parsed mapping. Parse failures (``{}``) are
        # NEVER cached so a transient outage can't poison future replays.
        if parsed and key is not None and cache is not None:
            llm_cache_put(cache, key, "mappings", parsed)

    # Validate: only accept labels that appear in taxonomy (case-
    # insensitive). This stops Haiku from inventing categories.
    allowed = {label.lower(): label for label in taxonomy.product_features}
    cleaned: dict[str, str] = {}
    invented = 0
    for dev, prod in parsed.items():
        if prod is None:
            continue
        key = prod.lower()
        if key in allowed:
            cleaned[dev] = allowed[key]
        else:
            invented += 1

    telemetry = {
        "called": True,
        "tokens_in": in_tokens,
        "tokens_out": out_tokens,
        "cost_usd": round(cost, 6),
        "mappings_returned": len(parsed),
        "mappings_accepted": len(cleaned),
        "invented_labels_rejected": invented,
        "model": model,
        "cache_hits": 1 if cache_hit else 0,
        "llm_calls": 0 if cache_hit else 1,
    }
    return cleaned, telemetry


# ── Apply mapping to features ───────────────────────────────────────────


def _emit_product_features(
    developer_features: list["Feature"],
    mapping: dict[str, str],
    source: str,
) -> list["Feature"]:
    """Rebuild ``product_features`` from a dev→product label mapping.

    Aggregates paths / authors / commits / health across the mapped
    contributors, mirroring the Stage 6.5 emission logic.
    """
    from faultline.models.types import Feature

    by_label: dict[str, list["Feature"]] = defaultdict(list)
    for f in developer_features:
        label = mapping.get(f.name)
        if not label:
            continue
        by_label[label].append(f)

    out: list[Feature] = []
    for label, contrib in by_label.items():
        if not contrib:
            continue
        merged_paths: list[str] = []
        seen_paths: set[str] = set()
        for c in contrib:
            for p in c.paths:
                if p not in seen_paths:
                    merged_paths.append(p)
                    seen_paths.add(p)
        authors: list[str] = []
        seen_authors: set[str] = set()
        for c in contrib:
            for a in (c.authors or []):
                if a not in seen_authors:
                    authors.append(a)
                    seen_authors.add(a)
        total_commits = sum(c.total_commits for c in contrib)
        bug_fixes = sum(c.bug_fixes for c in contrib)
        bug_fix_ratio = (bug_fixes / total_commits) if total_commits else 0.0
        last_modified = max(
            (c.last_modified for c in contrib),
            default=datetime.now(timezone.utc),
        )
        health_score = (
            sum(c.health_score for c in contrib) / len(contrib)
        )
        cov_vals = [c.coverage_pct for c in contrib if c.coverage_pct is not None]
        coverage_pct = (sum(cov_vals) / len(cov_vals)) if cov_vals else None
        out.append(Feature(
            name=label,
            display_name=label,
            description=(
                f"Product feature clustered from {len(contrib)} developer "
                f"features by {source}."
            ),
            paths=merged_paths,
            authors=authors,
            total_commits=total_commits,
            bug_fixes=bug_fixes,
            bug_fix_ratio=bug_fix_ratio,
            last_modified=last_modified,
            health_score=round(health_score, 2),
            flows=[],
            coverage_pct=coverage_pct,
            layer="product",
        ))
    return out


# ── Public entry point ──────────────────────────────────────────────────


def run_stage_8(
    ctx: "ScanContext",
    developer_features: list["Feature"],
    product_features_pre: list["Feature"],
    *,
    dev_to_product_map_pre: dict[str, tuple[str, ...]] | None = None,
    source_breakdown_pre: dict[str, int] | None = None,
    log: "StageLogger | None" = None,
    client: Any | None = None,
    model: str = "claude-haiku-4-5-20251001",
    cost_tracker: CostTracker | None = None,
    llm_health: LlmHealth | None = None,
    nav_taxonomy_map: dict[str, str] | None = None,
) -> Stage8Result:
    """Cascade entry point for Sprint E1.

    Cascade:
      1. If Stage 6.5 already saw a ``faultlines.yaml`` and emitted
         customer-yaml-sourced features → passthrough (highest conf).
      2. Else try marketing-grounded Haiku cluster:
         fetch homepage → extract taxonomy → ask Haiku → apply.
      3. Else fall back to Stage 6.5 deterministic result.

    ``nav_taxonomy_map`` (in-repo nav taxonomy, Stage 6.5 rule 2.5) —
    ``{dev_feature_name: vendor_label}``. The vendor's own nav labels
    rank ABOVE the external marketing taxonomy, so they override the
    Haiku mapping for matched dev features; unmatched features keep
    the marketing/haiku synthesis path unchanged.

    Returns :class:`Stage8Result` carrying the (possibly refined)
    product feature list + dev→product mapping + telemetry to be
    folded into ``scan_meta.stage_8``.
    """
    slug = ctx.repo_path.name
    # Cache routes through the pluggable backend threaded on the context
    # (None → the call helpers fall back to the env-selected default).
    cache_backend = getattr(ctx, "cache_backend", None)
    base_map: dict[str, tuple[str, ...]] = dict(dev_to_product_map_pre or {})

    # ── 1. Customer YAML short-circuit ──
    src_breakdown = source_breakdown_pre or {}
    customer_count = src_breakdown.get("rule:customer-yaml", 0)
    if customer_count > 0:
        if log is not None:
            log.info(f"customer-yaml-detected source_count={customer_count}")
        telemetry: dict[str, Any] = {
            "source": "customer-yaml",
            "marketing_url": None,
            "taxonomy_size": 0,
            "taxonomy_sample": [],
            "developer_features_mapped": sum(
                1 for v in base_map.values() if v
            ),
            "developer_features_unmapped": sum(
                1 for f in developer_features if not base_map.get(f.name)
            ),
            "product_features_emitted": len(product_features_pre),
            "haiku_call_cost_usd": 0.0,
            "haiku_tokens_in": 0,
            "haiku_tokens_out": 0,
            "confidence": 1.0,
            "cache_hit": False,
            "haiku_called": False,
        }
        return Stage8Result(
            product_features=list(product_features_pre),
            dev_to_product_map=base_map,
            telemetry=telemetry,
        )

    # ── 2. Marketing + Haiku ──
    # Try cache first to surface telemetry; if hit, no fetch happens.
    cached_before = _load_cached_taxonomy(slug, cache_backend=cache_backend)
    taxonomy: MarketingTaxonomy | None = None
    if client is not None:
        taxonomy = fetch_marketing_taxonomy(
            ctx.repo_path, slug, cache_backend=cache_backend,
        )

    if (
        client is not None
        and taxonomy is not None
        and len(taxonomy.product_features) >= _MIN_TAXONOMY_SIZE
    ):
        if log is not None:
            log.info(
                f"marketing-taxonomy-fetched url={taxonomy.source_url} "
                f"size={len(taxonomy.product_features)}",
            )
        # Naming-evidence core (2026-06) — in-repo product vocabulary
        # for the mapping prompt. Deterministic; README excluded.
        ps_candidates: set[str] = set()
        for f in developer_features:
            ps_candidates.update(f.paths or [])
            ps_candidates.update(
                mf.path for mf in (f.member_files or [])
            )
        product_strings = collect_product_strings(
            ctx.repo_path, ps_candidates,
        )
        mapping, haiku_telemetry = cluster_via_haiku(
            developer_features, taxonomy,
            client=client, model=model, cost_tracker=cost_tracker,
            llm_health=llm_health,
            product_strings=product_strings,
            cache=cache_backend,
        )
        if mapping:
            # In-repo nav taxonomy overrides the marketing mapping for
            # matched dev features — the vendor's own nav label is the
            # higher-trust source (matched, not synthesized).
            nav_overrides = 0
            valid_devs = {f.name for f in developer_features}
            for dev, label in (nav_taxonomy_map or {}).items():
                if dev in valid_devs and mapping.get(dev) != label:
                    mapping[dev] = label
                    nav_overrides += 1
            product_features = _emit_product_features(
                developer_features, mapping, source="marketing+haiku",
            )
            nav_labels = set((nav_taxonomy_map or {}).values())
            for pf in product_features:
                if pf.name in nav_labels:
                    pf.name_confidence = "high"
            new_dev_map: dict[str, tuple[str, ...]] = {
                dev: (label,) for dev, label in mapping.items()
            }
            # Preserve any deterministic mappings the Haiku pass left
            # unmapped — never regress below the Stage 6.5 baseline.
            for dev, labels in base_map.items():
                new_dev_map.setdefault(dev, labels)

            # M2 — when Haiku has placed a developer feature into a
            # marketing+haiku PF, the older Stage 6.5 PF that contained
            # the SAME developer feature is a duplicate and must be
            # dropped. Previously we kept those PFs (creating singleton
            # workspace+domain duplicates of every Haiku-mapped feature
            # — 84% of Layer 2 over-emission across the corpus).
            #
            # Universal rule: only retain a Stage-6.5 PF when at least
            # one of its developer-feature members is NOT now mapped
            # to a marketing+haiku PF.
            haiku_emitted_names = {x.name for x in product_features}
            haiku_mapped_devs = set(mapping.keys())

            keep_pre = []
            for pf in product_features_pre:
                if pf.name in haiku_emitted_names:
                    # Same name already exists in the Haiku output —
                    # always drop the duplicate.
                    continue
                # Identify which developer features this Stage-6.5 PF
                # claims by looking up its label in base_map.
                pf_devs = {
                    dev for dev, labels in base_map.items()
                    if pf.name in labels
                }
                if not pf_devs:
                    # No devs map back — anomalous; preserve to be safe.
                    keep_pre.append(pf)
                    continue
                # Drop ONLY if every dev member is now in a Haiku PF.
                # If at least one dev is unmapped by Haiku, the
                # deterministic PF still earns its keep.
                if pf_devs.issubset(haiku_mapped_devs):
                    continue
                keep_pre.append(pf)
            product_features.extend(keep_pre)

            telemetry = {
                "source": "marketing+haiku",
                "marketing_url": taxonomy.source_url,
                "taxonomy_size": len(taxonomy.product_features),
                "taxonomy_sample": list(taxonomy.product_features[:10]),
                "developer_features_mapped": sum(
                    1 for v in new_dev_map.values() if v
                ),
                "developer_features_unmapped": sum(
                    1 for f in developer_features
                    if not new_dev_map.get(f.name)
                ),
                "product_features_emitted": len(product_features),
                "haiku_call_cost_usd": haiku_telemetry.get("cost_usd", 0.0),
                "haiku_tokens_in": haiku_telemetry.get("tokens_in", 0),
                "haiku_tokens_out": haiku_telemetry.get("tokens_out", 0),
                "haiku_mappings_accepted": haiku_telemetry.get(
                    "mappings_accepted", 0,
                ),
                "haiku_invented_rejected": haiku_telemetry.get(
                    "invented_labels_rejected", 0,
                ),
                "confidence": _CONF_MARKETING_HAIKU,
                "cache_hit": cached_before is not None,
                "haiku_called": True,
                # Content-hash LLM cache counters (CacheKind.LLM_PRODUCT_
                # CLUSTER) — distinct from ``cache_hit`` above, which is
                # the slug-keyed marketing-PAGE cache.
                "llm_cache_hits": haiku_telemetry.get("cache_hits", 0),
                "llm_calls": haiku_telemetry.get("llm_calls", 0),
                "nav_taxonomy_overrides": nav_overrides,
            }
            # Degraded-scan stamp (naming review №6) — a dead key mid-
            # scan means names may be partial; mark them low-confidence.
            if llm_health is not None and llm_health.auth_failed:
                for pf in product_features:
                    pf.name_confidence = "low"
            if log is not None:
                log.info(
                    f"marketing+haiku: mapped={telemetry['developer_features_mapped']} "
                    f"product_features={len(product_features)} "
                    f"cost_usd={telemetry['haiku_call_cost_usd']}",
                )
            return Stage8Result(
                product_features=product_features,
                dev_to_product_map=new_dev_map,
                telemetry=telemetry,
            )
        # Haiku failed to produce anything usable.
        if log is not None:
            log.warn("haiku-mapping-empty — falling back to deterministic")

    # ── 3. Deterministic fallback ──
    reason = (
        "no-client" if client is None
        else "fetch-failed-or-empty" if taxonomy is None
        else "taxonomy-too-small"
    )
    if log is not None:
        log.info(f"fallback-to-deterministic reason={reason}")
    telemetry = {
        "source": "deterministic-only",
        "marketing_url": (taxonomy.source_url if taxonomy is not None else None),
        "taxonomy_size": (
            len(taxonomy.product_features) if taxonomy is not None else 0
        ),
        "taxonomy_sample": (
            list(taxonomy.product_features[:10]) if taxonomy is not None else []
        ),
        "developer_features_mapped": sum(
            1 for v in base_map.values() if v
        ),
        "developer_features_unmapped": sum(
            1 for f in developer_features if not base_map.get(f.name)
        ),
        "product_features_emitted": len(product_features_pre),
        "haiku_call_cost_usd": 0.0,
        "haiku_tokens_in": 0,
        "haiku_tokens_out": 0,
        "confidence": 0.6,
        "cache_hit": cached_before is not None,
        "haiku_called": False,
        "fallback_reason": reason,
    }
    # Degraded-scan stamp (naming review №6): when the key died mid-scan
    # we KEEP the deterministic Stage 6.5 slugs (never synthesize) and
    # mark them low-confidence. A deliberately keyless scan (client is
    # None, no auth failure) keeps the default — deterministic slugs are
    # accurate names, just not LLM-refined.
    if llm_health is not None and llm_health.auth_failed:
        for pf in product_features_pre:
            pf.name_confidence = "low"
    return Stage8Result(
        product_features=list(product_features_pre),
        dev_to_product_map=base_map,
        telemetry=telemetry,
    )


# ── Default Anthropic client factory (mirrors stage_4_residual) ─────────


def _default_client_factory() -> Any | None:  # pragma: no cover - IO
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


__all__ = [
    "STAGE_8_CACHE_VERSION",
    "Stage8Result",
    "cluster_via_haiku",
    "fetch_marketing_taxonomy",
    "llm_cache_enabled",
    "llm_cache_get",
    "llm_cache_key",
    "llm_cache_put",
    "run_stage_8",
]
