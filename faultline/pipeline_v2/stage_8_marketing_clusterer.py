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

import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.analyzer.marketing_fetcher import (
    MarketingTaxonomy,
    discover_marketing_site,
    extract_product_taxonomy,
    fetch_page_text,
)
from faultline.llm.cost import CostTracker, deterministic_params

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────

# Where we keep marketing-page extractions to avoid hammering public
# sites on consecutive scans. Same shape as ``~/.faultline/llm-cache/``
# — content-keyed by slug + 7-day TTL.
_MARKETING_CACHE_DIR = Path.home() / ".faultline" / "marketing-cache"

# Conservative TTL — marketing pages change rarely; 7 days lets daily
# CI scans of the same repo stay fast.
_CACHE_TTL_SECONDS = 7 * 24 * 3600

# Below this taxonomy size the marketing fetch is treated as
# insufficient signal and we fall back to the deterministic result.
_MIN_TAXONOMY_SIZE = 3

# Haiku call constraints.
_MAX_DEV_FEATURES_IN_PROMPT = 80
_MAX_PATHS_PER_FEATURE = 5
_MAX_TAXONOMY_ENTRIES_IN_PROMPT = 25
_HAIKU_MAX_TOKENS = 2000

# Confidence floor stamped on Stage 8 results when marketing+haiku
# succeeds. Higher than deterministic (0.6) but lower than customer
# override (1.0) to preserve the cascade.
_CONF_MARKETING_HAIKU = 0.85


# ── Public types ────────────────────────────────────────────────────────


@dataclass
class Stage8Result:
    """Outcome of Stage 8 — telemetry + new product feature list."""

    product_features: list[Any]  # list[Feature] but TYPE_CHECKING import
    dev_to_product_map: dict[str, tuple[str, ...]]
    telemetry: dict[str, Any]


# ── Cache helpers ───────────────────────────────────────────────────────


def _cache_path(slug: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._\-]", "_", slug or "unknown")
    return _MARKETING_CACHE_DIR / f"{safe}.json"


def _load_cached_taxonomy(slug: str) -> MarketingTaxonomy | None:
    """Return the cached taxonomy if present + fresh, else None."""
    path = _cache_path(slug)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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


def _write_cache(taxonomy: MarketingTaxonomy) -> None:
    """Persist taxonomy to cache. Failures are logged + swallowed."""
    try:
        _MARKETING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _cache_path(taxonomy.repo_slug)
        path.write_text(json.dumps({
            "repo_slug": taxonomy.repo_slug,
            "source_url": taxonomy.source_url,
            "fetched_at": taxonomy.fetched_at,
            "fetched_at_epoch": time.time(),
            "product_features": list(taxonomy.product_features),
            "confidence": taxonomy.confidence,
            "notes": taxonomy.notes,
        }, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.debug("stage_8: cache write failed: %s", exc)


# ── Marketing fetch (cached) ────────────────────────────────────────────


def fetch_marketing_taxonomy(
    repo_path: Path,
    repo_slug: str,
    *,
    cache_ttl_seconds: int = _CACHE_TTL_SECONDS,
    use_cache: bool = True,
) -> MarketingTaxonomy | None:
    """Top-level entry — discover + fetch + parse, with caching.

    Returns ``None`` when no marketing surface is reachable (private
    repo / no homepage / network failure / 404). Callers must handle
    that gracefully (fall back to Stage 6.5 deterministic result).
    """
    if use_cache:
        cached = _load_cached_taxonomy(repo_slug)
        if cached is not None:
            return cached

    url = discover_marketing_site(repo_path)
    if not url:
        return None

    html = fetch_page_text(url)
    if not html:
        return None

    candidates, conf = extract_product_taxonomy(html)
    if not candidates:
        return None

    taxonomy = MarketingTaxonomy(
        repo_slug=repo_slug,
        source_url=url,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        product_features=tuple(candidates),
        confidence=conf,
        notes=f"extracted {len(candidates)} candidates from {url}",
    )
    if use_cache:
        _write_cache(taxonomy)
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
) -> str:
    """Build the per-call user prompt — feature list + taxonomy."""
    feature_lines: list[str] = []
    for f in developer_features[:_MAX_DEV_FEATURES_IN_PROMPT]:
        paths = list(f.paths or [])[:_MAX_PATHS_PER_FEATURE]
        path_summary = ", ".join(paths) if paths else "(no paths)"
        feature_lines.append(
            f"- {f.name}: {path_summary}"
        )
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
) -> tuple[str, int, int]:
    """One Haiku call. Mirrors :func:`stage_4_residual._call_haiku`."""
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage_8: Haiku call failed: %s", exc)
        return "", 0, 0
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
) -> tuple[dict[str, str], dict[str, Any]]:
    """Single Haiku call: map each dev feature to a taxonomy label.

    Returns ``(mapping, telemetry)`` where ``mapping`` is
    ``{dev_name: product_label}`` — entries the model said were
    ``null`` are dropped from the mapping (they remain orphan and the
    deterministic Stage 6.5 result wins for those features).
    """
    if not developer_features or not taxonomy.product_features:
        return {}, {"called": False, "reason": "empty-input"}

    user = _build_user_prompt(developer_features, taxonomy)
    text, in_tokens, out_tokens = _call_haiku(
        client,
        model=model,
        system=_SYSTEM_PROMPT,
        user=user,
        max_tokens=_HAIKU_MAX_TOKENS,
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
) -> Stage8Result:
    """Cascade entry point for Sprint E1.

    Cascade:
      1. If Stage 6.5 already saw a ``faultlines.yaml`` and emitted
         customer-yaml-sourced features → passthrough (highest conf).
      2. Else try marketing-grounded Haiku cluster:
         fetch homepage → extract taxonomy → ask Haiku → apply.
      3. Else fall back to Stage 6.5 deterministic result.

    Returns :class:`Stage8Result` carrying the (possibly refined)
    product feature list + dev→product mapping + telemetry to be
    folded into ``scan_meta.stage_8``.
    """
    slug = ctx.repo_path.name
    base_map: dict[str, tuple[str, ...]] = dict(dev_to_product_map_pre or {})

    # ── 1. Customer YAML short-circuit ──
    src_breakdown = source_breakdown_pre or {}
    customer_count = src_breakdown.get("rule:customer-yaml", 0)
    if customer_count > 0:
        if log is not None:
            log.info(f"customer-yaml-detected source_count={customer_count}")
        telemetry = {
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
    cached_before = _load_cached_taxonomy(slug)
    taxonomy: MarketingTaxonomy | None = None
    if client is not None:
        taxonomy = fetch_marketing_taxonomy(ctx.repo_path, slug)

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
        mapping, haiku_telemetry = cluster_via_haiku(
            developer_features, taxonomy,
            client=client, model=model, cost_tracker=cost_tracker,
        )
        if mapping:
            product_features = _emit_product_features(
                developer_features, mapping, source="marketing+haiku",
            )
            new_dev_map: dict[str, tuple[str, ...]] = {
                dev: (label,) for dev, label in mapping.items()
            }
            # Preserve any deterministic mappings the Haiku pass left
            # unmapped — never regress below the Stage 6.5 baseline.
            for dev, labels in base_map.items():
                new_dev_map.setdefault(dev, labels)

            # Anything Stage 6.5 mapped but Haiku didn't — keep the
            # deterministic product feature so we don't lose recall.
            keep_pre = [
                pf for pf in product_features_pre
                if pf.name not in {x.name for x in product_features}
                and any(
                    pf.name in labels
                    for labels in (base_map.values())
                )
            ]
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
            }
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
    "Stage8Result",
    "fetch_marketing_taxonomy",
    "cluster_via_haiku",
    "run_stage_8",
]
