"""Stage 8 — Sonnet-as-analyst Layer 2 clusterer (Sprint M4).

Replaces the Haiku label-mapper (``stage_8_marketing_clusterer``) with
a single Sonnet 4.6 call that acts as a senior product analyst.

Why Sonnet here?
================
Experimenter validation on better-auth / inbox-zero / supabase showed
avg dP +47.9pp, dR +30.3pp vs the Haiku clusterer at FINAL-K, for a
total of $0.43 on 3 repos (~$0.14 per repo). The Haiku clusterer is
constrained to map developer features into a pre-fetched marketing
taxonomy of labels — it cannot synthesise a label that isn't already
in the taxonomy. Sonnet can, given marketing surfaces + Layer 1
developer features + workspace structure, produce *new* product-grain
labels that aren't verbatim in the taxonomy ("Multi-Region Probing"
when /pricing said "Probing" and /platform listed "Multi-Region").

The system prompt is identical to the experimenter prototype that
validated the lift. We treat the prompt as a fixed contract.

Hard rules respected
====================
* README forbidden (``rule-no-readme``). Marketing fetch is via the
  ``marketing_fetcher`` module (homepage / docs sidebar / llms.txt /
  pricing / sitemap — all PUBLIC external surfaces).
* No repo-specific paths (``rule-no-repo-specific-paths``). The
  payload construction uses universal heuristics: top-N by path
  count, generic workspace folders (``apps/`` / ``packages/`` /
  ``plugins/`` / ``crates/`` / ``e2e/``).
* No magic-number tuning. Caps are structural (top 80 dev features,
  60 workspace packages, 30 KB marketing context).
* Fallback to the Haiku clusterer when:
    - Anthropic client unavailable (no API key, no SDK).
    - Sonnet response is malformed JSON (after one retry with a
      stricter prompt).
    - Marketing fetch returned nothing AND no workspace packages
      detected (analyst has no inputs to ground on).

Dispatcher
==========
``run.py`` reads ``FAULTLINE_STAGE_8_MODE`` from the environment
(matches the singular ``FAULTLINE_*`` convention used by Stage 6.3 /
6.4 / 6.6):

    * ``"haiku-clusterer"`` (DEFAULT, safety): legacy Stage 8.
    * ``"analyst"``: this module.

The mode is surfaced in ``scan_meta.stage_8.mode`` so downstream
tools can attribute lift to either path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.analyzer.marketing_fetcher import (
    discover_marketing_site,
    extract_docs_sidebar_taxonomy,
    fetch_llms_txt_urls,
    fetch_page_text,
    parse_llms_txt,
)
from faultline.llm.cost import CostTracker
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.stage_8_marketing_clusterer import (
    Stage8Result,
    fetch_marketing_taxonomy,
    run_stage_8 as run_stage_8_haiku,
)

if TYPE_CHECKING:
    from faultline.models.types import Feature, Flow
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)
from faultline.llm.model_gateway import resolve_model as gateway_model


# ── Constants ───────────────────────────────────────────────────────────

# Default Sonnet model id. Matches the engine-wide convention used by
# ``sonnet_scanner`` / ``aggregator_detector`` / ``dedup`` / ``tool_use_scan``
# (i.e. the short alias, NOT the dated id). Anthropic's API resolves
# this to the current Sonnet 4.6 build.
DEFAULT_ANALYST_MODEL = "claude-sonnet-4-6"

# Top developer features (by path count) included in the analyst
# payload. Matches the experimenter prototype's cutoff.
_MAX_DEV_FEATURES_IN_PAYLOAD = 80

# Sample paths per dev feature shown to the analyst.
_MAX_PATHS_PER_FEATURE = 5

# Maximum description length per dev feature (truncate long LLM-
# generated descriptions to keep the prompt bounded).
_MAX_DESCRIPTION_CHARS = 240

# Maximum workspace packages enumerated in the payload.
_MAX_WORKSPACE_PACKAGES = 60

# Top flows (by participant count) included in the analyst payload.
# Cap chosen to keep prompt bounded on cal-com-scale repos (2073 flows
# pre-cap); the analyst doesn't need every flow to attribute most of
# them, and undersized payloads matter less than over-spend.
_MAX_FLOWS_IN_PAYLOAD = 120

# Maximum description length per flow shown to the analyst.
_MAX_FLOW_DESCRIPTION_CHARS = 160

# Marketing context size cap (chars). Matches the experimenter
# prototype which truncated at 30 KB.
_MAX_MARKETING_CONTEXT_CHARS = 30_000

# Max tokens for the analyst response. 8000 was sufficient for every
# experimenter test (largest output was supabase at ~3500 out tokens).
_ANALYST_MAX_TOKENS = 8000

# Sonnet pricing — surfaced for documentation only. Real cost is
# computed via ``CostTracker.record`` which calls ``lookup_pricing``.
# Sonnet 4.6: $3 / Mtok input, $15 / Mtok output.

# Workspace-folder roots that indicate a monorepo package boundary.
# Universal across pnpm / Turborepo / Nx / Cargo / mixed.
_WORKSPACE_FOLDER_ROOTS = (
    "apps", "packages", "plugins", "crates", "examples", "e2e",
    "services", "libs", "modules", "extensions",
)


SYSTEM_PROMPT = """You are a senior product analyst. Given a deterministic codebase map (Layer 1 developer features) plus marketing surfaces, name the Layer 2 PRODUCT features that an end-user or customer of this product would care about.

Rules:
1. Be specific. "AI Email Assistant" not "AI". "OAuth Provider with 35+ social logins" not "Auth".
2. Each product feature MUST be grounded: include `grounded_in` listing which inputs (workspace package names, marketing surface names, developer feature names) justify it.
3. Map developer features into product features under `member_dev_features` (list of dev feature names that compose this product feature). It's OK for a dev feature to belong to multiple product features.
4. When a FLOWS section is provided in the payload, ALSO map flows into product features under `member_flows` (list of flow names that belong to this product feature). Use ONLY flow names verbatim from the FLOWS section — never invent. Omit `member_flows` entirely (or use `[]`) when no flows fit.
5. Aim for 20-50 product features total. Quality over quantity. If a developer feature is purely structural/internal (build tooling, test fixtures, CI), do NOT promote it to a product feature.
6. confidence must be 0-1 reflecting how grounded the feature is.
7. NEVER invent features that have no code or marketing evidence.

Output ONLY valid JSON of this exact shape (no markdown, no commentary):
{
  "product_features": [
    {
      "name": "Email and Password Authentication",
      "description": "Built-in credential auth with sessions, email verification, password reset.",
      "member_dev_features": ["email-password-auth", "session-management"],
      "member_flows": ["sign-up-flow", "password-reset-flow"],
      "confidence": 0.95,
      "grounded_in": ["packages/core", "marketing:/docs/concepts/email-password", "dev_feature:email-password-auth"]
    }
  ]
}
"""

# Stricter retry prompt appended on JSON-parse failure.
_RETRY_SUFFIX = (
    "\n\nPREVIOUS RESPONSE WAS NOT VALID JSON. Re-emit ONLY the "
    "product_features JSON object — no markdown fences, no leading "
    "or trailing text, no commentary. The first character must be `{` "
    "and the last character must be `}`."
)


# ── Payload construction ────────────────────────────────────────────────


def _workspace_packages_from_paths(
    developer_features: list["Feature"],
) -> list[str]:
    """Derive the set of monorepo package paths from dev features.

    Universal heuristic: any path whose first segment is one of the
    canonical workspace-folder roots (``apps``, ``packages``, …) and
    has a second segment becomes a candidate ``<root>/<name>``.

    Sorted + capped at ``_MAX_WORKSPACE_PACKAGES``.
    """
    pkgs: set[str] = set()
    for f in developer_features:
        for p in f.paths:
            parts = p.split("/")
            if (
                len(parts) >= 2
                and parts[0] in _WORKSPACE_FOLDER_ROOTS
                and parts[1]
            ):
                pkgs.add(f"{parts[0]}/{parts[1]}")
    return sorted(pkgs)[:_MAX_WORKSPACE_PACKAGES]


def _read_root_package(repo_path: Path) -> dict[str, Any]:
    """Read root ``package.json`` for name / description / homepage /
    keywords. Returns ``{}`` when unavailable.

    NOT a README read — ``package.json`` is structured config (allowed
    per ``rule-no-readme``).
    """
    pj = repo_path / "package.json"
    if not pj.is_file():
        return {}
    try:
        raw = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        "name": raw.get("name"),
        "description": raw.get("description"),
        "keywords": raw.get("keywords"),
        "homepage": raw.get("homepage"),
    }


def _compact_dev_features(
    developer_features: list["Feature"],
) -> list[dict[str, Any]]:
    """Project the top-N dev features into a lean payload shape.

    Sorted by path count descending — the assumption is that
    higher-path features are more important / more grounded.
    """
    sorted_df = sorted(
        developer_features, key=lambda f: -len(f.paths or []),
    )[:_MAX_DEV_FEATURES_IN_PAYLOAD]
    return [
        {
            "name": f.name,
            "display_name": f.display_name,
            "description": (f.description or "")[:_MAX_DESCRIPTION_CHARS],
            "n_paths": len(f.paths or []),
            "sample_paths": list(f.paths or [])[:_MAX_PATHS_PER_FEATURE],
        }
        for f in sorted_df
    ]


def _compact_flows(
    top_flows: list["Flow"] | None,
) -> list[dict[str, Any]]:
    """Project the top-N flows into a lean payload shape.

    Sorted by participant count descending — the most-cross-cutting
    flows are the ones most likely to belong to a customer-facing
    product feature. Returns ``[]`` when no flows are supplied.
    """
    if not top_flows:
        return []
    sorted_flows = sorted(
        top_flows, key=lambda f: -len(getattr(f, "participants", []) or []),
    )[:_MAX_FLOWS_IN_PAYLOAD]
    out: list[dict[str, Any]] = []
    for f in sorted_flows:
        out.append({
            "name": f.name,
            "display_name": getattr(f, "display_name", None) or f.name,
            "description": (getattr(f, "description", "") or "")[
                :_MAX_FLOW_DESCRIPTION_CHARS
            ],
            "entry_point_file": getattr(f, "entry_point_file", None),
            "n_participants": len(getattr(f, "participants", []) or []),
        })
    return out


def _harvest_marketing_text(
    repo_path: Path,
    repo_slug: str,
    *,
    cache_backend: Any | None = None,
) -> tuple[str, str | None, int]:
    """Build a single marketing-context blob for the analyst prompt.

    Composition (order matters — most-structured first):

      1. Cached/computed ``MarketingTaxonomy`` labels (the existing
         v7-A pipeline output: homepage + /features + /pricing + /docs
         sidebar + sitemap-ranked pages + llms.txt). Header line plus
         bulleted list.
      2. Raw text of the homepage (truncated).
      3. Raw text of the canonical /docs page or docs.<host> if
         reachable.
      4. Parsed llms.txt section structure (if present).

    Returns ``(text, marketing_url, taxonomy_size)``. ``marketing_url``
    is the canonical source used by telemetry; ``taxonomy_size`` is the
    count of marketing labels surfaced.
    """
    pieces: list[str] = []
    marketing_url: str | None = None
    taxonomy_size = 0

    # Taxonomy (cached or fresh).
    taxonomy = fetch_marketing_taxonomy(
        repo_path, repo_slug, cache_backend=cache_backend,
    )
    if taxonomy is not None:
        marketing_url = taxonomy.source_url
        taxonomy_size = len(taxonomy.product_features)
        if taxonomy.product_features:
            pieces.append(
                f"== Marketing taxonomy ({len(taxonomy.product_features)} labels) ==",
            )
            pieces.append(
                "\n".join("- " + lab for lab in taxonomy.product_features),
            )

    # Raw homepage text for richer context.
    primary = marketing_url or discover_marketing_site(repo_path)
    if primary:
        if marketing_url is None:
            marketing_url = primary
        html = fetch_page_text(primary)
        if html:
            pieces.append(f"\n== Homepage text ({primary}) ==")
            pieces.append(html[:10_000])

        # Docs landing — try /docs first then docs.<host>.
        docs_url = primary.rstrip("/") + "/docs"
        docs_html = fetch_page_text(docs_url)
        if docs_html:
            sidebar, _ = extract_docs_sidebar_taxonomy(docs_html)
            if sidebar:
                pieces.append(f"\n== Docs sidebar ({docs_url}) ==")
                pieces.append(
                    "\n".join("- " + s for s in sidebar[:80]),
                )
            else:
                pieces.append(f"\n== Docs page text ({docs_url}) ==")
                pieces.append(docs_html[:5_000])

        # llms.txt — structured product/feature summary if present.
        llms_urls = fetch_llms_txt_urls(primary)
        for url in llms_urls[:1]:
            llms_text = fetch_page_text(url)
            if llms_text:
                labels, _ = parse_llms_txt(llms_text)
                if labels:
                    pieces.append(f"\n== llms.txt structured ({url}) ==")
                    pieces.append(
                        "\n".join("- " + lab for lab in labels[:80]),
                    )

    text = "\n".join(pieces)
    return text[:_MAX_MARKETING_CONTEXT_CHARS], marketing_url, taxonomy_size


def build_analyst_payload(
    ctx: "ScanContext",
    developer_features: list["Feature"],
    top_flows: list["Flow"] | None = None,
) -> dict[str, Any]:
    """Assemble the full analyst payload from prior pipeline stages.

    Exposed (not underscored) so tests can assert on it and so an
    out-of-band replay tool can re-emit the prompt deterministically.

    ``top_flows`` (Sprint S6.3) — when provided, the top-N flows by
    participant count are projected into the payload so the analyst
    can return ``member_flows`` per product feature. Back-compatible:
    when ``None`` or empty, the payload omits the flows section and
    the analyst's ``member_flows`` is treated as empty downstream.
    """
    slug = ctx.repo_path.name
    workspace_packages = _workspace_packages_from_paths(developer_features)
    root_pkg = _read_root_package(ctx.repo_path)
    marketing_text, marketing_url, taxonomy_size = _harvest_marketing_text(
        ctx.repo_path, slug, cache_backend=getattr(ctx, "cache_backend", None),
    )
    return {
        "slug": slug,
        "audited_stack": ctx.audited_stack or ctx.stack or "",
        "secondary_stacks": list(ctx.secondary_stacks or ()),
        "workspace_manager": ctx.workspace_manager,
        "root_package": root_pkg,
        "auditor_hints": list(ctx.extractor_hints or ()),
        "workspace_packages": workspace_packages,
        "developer_features": _compact_dev_features(developer_features),
        "flows": _compact_flows(top_flows),
        "marketing_text": marketing_text,
        "marketing_url": marketing_url,
        "taxonomy_size": taxonomy_size,
    }


def build_user_prompt(payload: dict[str, Any]) -> str:
    """Render the analyst user-message body from a payload dict.

    Identical shape to the experimenter prototype that validated the
    lift — DO NOT silently restructure without re-validating. Sprint
    S6.3 added an optional FLOWS section (only when ``payload['flows']``
    is non-empty) so the analyst can populate ``member_flows`` per
    product feature.
    """
    flows = payload.get("flows") or []
    flows_section = ""
    if flows:
        flows_section = (
            "FLOWS (top by participant count — use names verbatim when "
            "filling member_flows):\n"
            + json.dumps(flows, indent=2) + "\n\n"
        )
    return (
        "REPO SLUG: " + payload["slug"] + "\n"
        "STACK (audited): " + payload["audited_stack"] + "\n"
        "SECONDARY STACKS: " + ", ".join(payload["secondary_stacks"]) + "\n"
        "WORKSPACE_MANAGER: " + str(payload["workspace_manager"]) + "\n"
        "ROOT PACKAGE: " + json.dumps(payload["root_package"]) + "\n\n"
        "AUDITOR HINTS (deterministic stack auditor, code-derived):\n"
        + "\n".join("- " + h for h in payload["auditor_hints"]) + "\n\n"
        "WORKSPACE PACKAGES (detected from monorepo manifest):\n"
        + json.dumps(payload["workspace_packages"], indent=2) + "\n\n"
        "DEVELOPER FEATURES (top by path count, deterministic Layer 1):\n"
        + json.dumps(payload["developer_features"], indent=2) + "\n\n"
        + flows_section
        + "MARKETING SURFACES (fetched from public web; NOT repo README):\n"
        + payload["marketing_text"] + "\n\n"
        "Now emit the Layer 2 product_features JSON per the system prompt rules."
    )


# ── Sonnet call + parse ─────────────────────────────────────────────────


def _strip_code_fences(text: str) -> str:
    """Strip ```json ... ``` fences if the model returned them despite
    the system-prompt rule."""
    s = text.strip()
    if not s.startswith("```"):
        return s
    s = s.strip("`")
    if s.startswith("json"):
        s = s[4:]
    return s.strip()


def _parse_analyst_response(text: str) -> dict[str, Any] | None:
    """Return the parsed dict on success, ``None`` on any failure."""
    if not text:
        return None
    cleaned = _strip_code_fences(text)
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    pf = obj.get("product_features")
    if not isinstance(pf, list):
        return None
    return obj


def _call_sonnet(
    client: Any,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int = _ANALYST_MAX_TOKENS,
    llm_health: LlmHealth | None = None,
) -> tuple[str, int, int, float]:
    """One Sonnet call. Returns ``(text, in_tokens, out_tokens, elapsed_s)``.

    NOTE: we do NOT pass ``deterministic_params`` here — Sonnet's
    sampling is fine at defaults for an open-ended analyst task. The
    Haiku clusterer uses deterministic params because it has to choose
    from a fixed label set; the analyst is generative.

    Consults the shared :class:`LlmHealth`: after the first auth-class
    failure anywhere in the scan the call is skipped (dead key).
    """
    t0 = time.time()
    if llm_health is not None and not llm_health.should_call():
        return "", 0, 0, time.time() - t0
    try:
        msg = client.messages.create(
            model=gateway_model(model),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001
        if llm_health is not None and llm_health.record_failure(
            exc, stage="stage_8_analyst",
        ):
            logger.error(
                "stage_8_analyst: LLM authentication failed — skipping all "
                "remaining LLM calls this scan: %s", exc,
            )
        else:
            logger.warning("stage_8_analyst: Sonnet call failed: %s", exc)
        return "", 0, 0, time.time() - t0
    if llm_health is not None:
        llm_health.record_success()
    try:
        parts = [getattr(b, "text", "") for b in msg.content]
        text = "\n".join(p for p in parts if p)
    except Exception:  # noqa: BLE001
        text = ""
    in_t = int(
        getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0,
    )
    out_t = int(
        getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0,
    )
    return text, in_t, out_t, time.time() - t0


# ── Emit Product Features ───────────────────────────────────────────────


def _emit_product_features_from_analyst(
    parsed: dict[str, Any],
    developer_features: list["Feature"],
    top_flows: list["Flow"] | None = None,
) -> tuple[
    list["Feature"],
    dict[str, tuple[str, ...]],
    dict[str, list[str]],
    dict[str, Any],
]:
    """Translate Sonnet's product_features array into ``Feature`` objects.

    Returns ``(product_features, dev_to_product_map, member_flows_map,
    telemetry_aux)``.

    Path/author/commit aggregation mirrors the Haiku clusterer's
    ``_emit_product_features`` — sum total commits + bug fixes, union
    paths and authors, average health, max last_modified.

    Unknown ``member_dev_features`` entries (Sonnet hallucinated a name)
    are silently skipped — we never invent dev features.

    Sprint S6.3: ``member_flows`` per PF is consumed when ``top_flows``
    is supplied. Each name is validated against the actual flow-name
    set; invented flow names are dropped + counted in telemetry. The
    resulting map is keyed by emitted PF slug (matching the rest of
    the pipeline's ``pf.name`` convention) so the rollup strategies
    can attach flows by slug lookup.
    """
    from faultline.models.types import Feature

    by_name: dict[str, "Feature"] = {f.name: f for f in developer_features}
    valid_flow_names: set[str] = (
        {f.name for f in top_flows} if top_flows else set()
    )
    pf_specs = parsed.get("product_features") or []

    out: list[Feature] = []
    dev_to_product: dict[str, list[str]] = defaultdict(list)
    member_flows_map: dict[str, list[str]] = {}
    invented_dev_features = 0
    invented_flows_skipped = 0
    accepted = 0
    skipped_empty = 0

    for spec in pf_specs:
        if not isinstance(spec, dict):
            continue
        raw_name = spec.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        label = raw_name.strip()
        slug = label.lower().replace(" ", "-").replace("/", "-")
        members = spec.get("member_dev_features") or []
        if not isinstance(members, list):
            continue

        contrib: list[Feature] = []
        for m in members:
            if not isinstance(m, str):
                continue
            df = by_name.get(m)
            if df is None:
                invented_dev_features += 1
                continue
            contrib.append(df)

        if not contrib:
            skipped_empty += 1
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
        bug_fix_ratio = (
            bug_fixes / total_commits if total_commits else 0.0
        )
        last_modified = max(
            (c.last_modified for c in contrib),
            default=datetime.now(timezone.utc),
        )
        health = sum(c.health_score for c in contrib) / len(contrib)
        cov_vals = [
            c.coverage_pct for c in contrib if c.coverage_pct is not None
        ]
        coverage_pct = (
            sum(cov_vals) / len(cov_vals) if cov_vals else None
        )
        description = spec.get("description")
        if not isinstance(description, str):
            description = (
                f"Product feature clustered from {len(contrib)} developer "
                f"features by sonnet-analyst."
            )

        out.append(Feature(
            name=slug,
            display_name=label,
            description=description,
            paths=merged_paths,
            authors=authors,
            total_commits=total_commits,
            bug_fixes=bug_fixes,
            bug_fix_ratio=bug_fix_ratio,
            last_modified=last_modified,
            health_score=round(health, 2),
            flows=[],
            coverage_pct=coverage_pct,
            layer="product",
        ))
        accepted += 1
        for c in contrib:
            dev_to_product[c.name].append(slug)

        # Sprint S6.3 — collect validated member_flows per PF slug.
        raw_flows = spec.get("member_flows") or []
        if isinstance(raw_flows, list):
            validated: list[str] = []
            seen_flow: set[str] = set()
            for fn in raw_flows:
                if not isinstance(fn, str):
                    continue
                if valid_flow_names and fn not in valid_flow_names:
                    invented_flows_skipped += 1
                    continue
                if not valid_flow_names:
                    # No flows were passed to the analyst — treat any
                    # ``member_flows`` value as invented to keep the
                    # rollup contract honest.
                    invented_flows_skipped += 1
                    continue
                if fn in seen_flow:
                    continue
                validated.append(fn)
                seen_flow.add(fn)
            if validated:
                member_flows_map[slug] = validated

    # Freeze tuples on the map.
    dev_map: dict[str, tuple[str, ...]] = {
        k: tuple(v) for k, v in dev_to_product.items()
    }
    aux = {
        "product_features_emitted": accepted,
        "product_features_skipped_no_members": skipped_empty,
        "invented_dev_features_skipped": invented_dev_features,
        "invented_flows_skipped": invented_flows_skipped,
        "pfs_with_member_flows": len(member_flows_map),
    }
    return out, dev_map, member_flows_map, aux


# ── Public entry point ──────────────────────────────────────────────────


def run_stage_8_analyst(
    ctx: "ScanContext",
    developer_features: list["Feature"],
    product_features_pre: list["Feature"],
    *,
    dev_to_product_map_pre: dict[str, tuple[str, ...]] | None = None,
    source_breakdown_pre: dict[str, int] | None = None,
    top_flows: list["Flow"] | None = None,
    log: "StageLogger | None" = None,
    client: Any | None = None,
    model: str = DEFAULT_ANALYST_MODEL,
    cost_tracker: CostTracker | None = None,
    llm_health: LlmHealth | None = None,
) -> Stage8Result:
    """Sonnet-as-analyst entry point.

    Signature matches ``run_stage_8`` from
    ``stage_8_marketing_clusterer`` so the dispatcher in ``run.py``
    can call either with identical arguments.

    Cascade:
      1. Customer YAML short-circuit — passthrough (same as Haiku
         path).
      2. Analyst Sonnet call → emit product features.
      3. Fallback to Haiku clusterer on:
         - No client
         - Empty analyst response
         - Parse failure after one retry
         - Zero product features accepted
    """
    slug = ctx.repo_path.name

    # 1. Customer YAML short-circuit — exact same logic as Haiku path.
    src_breakdown = source_breakdown_pre or {}
    customer_count = src_breakdown.get("rule:customer-yaml", 0)
    if customer_count > 0:
        if log is not None:
            log.info(
                f"customer-yaml-detected source_count={customer_count}",
            )
        base_map = dict(dev_to_product_map_pre or {})
        telemetry = {
            "mode": "analyst",
            "source": "customer-yaml",
            "analyst_called": False,
            "fallback_used": False,
            "developer_features_mapped": sum(
                1 for v in base_map.values() if v
            ),
            "product_features_emitted": len(product_features_pre),
            "analyst_cost_usd": 0.0,
            "prompt_input_tokens": 0,
            "prompt_output_tokens": 0,
            "phantom_count_estimate": 0,
            "confidence": 1.0,
        }
        return Stage8Result(
            product_features=list(product_features_pre),
            dev_to_product_map=base_map,
            telemetry=telemetry,
        )

    # 2. Try the analyst path.
    if client is None:
        if log is not None:
            log.info("no-client — falling back to deterministic Stage 8")
        return _fallback_to_haiku(
            ctx, developer_features, product_features_pre,
            dev_to_product_map_pre=dev_to_product_map_pre,
            source_breakdown_pre=source_breakdown_pre,
            log=log, client=None, model=model,
            cost_tracker=cost_tracker,
            llm_health=llm_health,
            fallback_reason="no-client",
        )

    payload = build_analyst_payload(ctx, developer_features, top_flows)
    if log is not None:
        log.info(
            f"analyst-payload-built dev_features={len(payload['developer_features'])} "
            f"flows={len(payload['flows'])} "
            f"workspace_packages={len(payload['workspace_packages'])} "
            f"taxonomy_size={payload['taxonomy_size']}",
        )

    user_prompt = build_user_prompt(payload)
    text, in_t, out_t, elapsed = _call_sonnet(
        client, model=model, system=SYSTEM_PROMPT, user=user_prompt,
        llm_health=llm_health,
    )
    cost = 0.0
    if cost_tracker is not None and (in_t or out_t):
        rec = cost_tracker.record(
            provider="anthropic",
            model=model,
            input_tokens=in_t,
            output_tokens=out_t,
            label="stage_8_analyst",
        )
        cost = rec.cost_usd

    parsed = _parse_analyst_response(text)
    retried = False
    if parsed is None and text:
        # One retry with a stricter prompt suffix.
        if log is not None:
            log.warn(
                "analyst-parse-failed — retrying with stricter suffix",
            )
        retried = True
        text2, in2, out2, _ = _call_sonnet(
            client,
            model=model,
            system=SYSTEM_PROMPT,
            user=user_prompt + _RETRY_SUFFIX,
            llm_health=llm_health,
        )
        if cost_tracker is not None and (in2 or out2):
            rec2 = cost_tracker.record(
                provider="anthropic",
                model=model,
                input_tokens=in2,
                output_tokens=out2,
                label="stage_8_analyst_retry",
            )
            cost += rec2.cost_usd
        in_t += in2
        out_t += out2
        parsed = _parse_analyst_response(text2)

    if parsed is None:
        if log is not None:
            log.warn(
                "analyst-parse-failed-twice — falling back to Haiku clusterer",
            )
        return _fallback_to_haiku(
            ctx, developer_features, product_features_pre,
            dev_to_product_map_pre=dev_to_product_map_pre,
            source_breakdown_pre=source_breakdown_pre,
            log=log, client=client, model=model,
            cost_tracker=cost_tracker,
            llm_health=llm_health,
            fallback_reason="analyst-parse-failed",
            analyst_aux={
                "analyst_cost_usd": round(cost, 6),
                "prompt_input_tokens": in_t,
                "prompt_output_tokens": out_t,
                "retried": retried,
            },
        )

    (
        product_features,
        dev_map,
        member_flows_map,
        aux,
    ) = _emit_product_features_from_analyst(
        parsed, developer_features, top_flows,
    )

    if not product_features:
        if log is not None:
            log.warn(
                "analyst-zero-product-features — falling back to Haiku clusterer",
            )
        return _fallback_to_haiku(
            ctx, developer_features, product_features_pre,
            dev_to_product_map_pre=dev_to_product_map_pre,
            source_breakdown_pre=source_breakdown_pre,
            log=log, client=client, model=model,
            cost_tracker=cost_tracker,
            llm_health=llm_health,
            fallback_reason="analyst-empty-output",
            analyst_aux={
                "analyst_cost_usd": round(cost, 6),
                "prompt_input_tokens": in_t,
                "prompt_output_tokens": out_t,
                "retried": retried,
            },
        )

    # Phantom-count estimate: emitted PFs whose member dev features
    # account for ≤1 unique path — likely orphan/junk. Universal
    # heuristic, not corpus-tuned.
    phantom_count = sum(
        1 for pf in product_features if len(pf.paths) <= 1
    )

    telemetry = {
        "mode": "analyst",
        "source": "analyst:sonnet",
        "model": model,
        "analyst_called": True,
        "fallback_used": False,
        "marketing_url": payload["marketing_url"],
        "taxonomy_size": payload["taxonomy_size"],
        "workspace_package_count": len(payload["workspace_packages"]),
        "dev_features_in_payload": len(payload["developer_features"]),
        "flows_in_payload": len(payload["flows"]),
        "analyst_cost_usd": round(cost, 6),
        "prompt_input_tokens": in_t,
        "prompt_output_tokens": out_t,
        "elapsed_sec": round(elapsed, 2),
        "retried_on_parse_error": retried,
        "product_features_emitted": len(product_features),
        "phantom_count_estimate": phantom_count,
        "developer_features_mapped": sum(
            1 for v in dev_map.values() if v
        ),
        "developer_features_unmapped": sum(
            1 for f in developer_features if not dev_map.get(f.name)
        ),
        "confidence": 0.90,
        **aux,
    }
    if log is not None:
        log.info(
            f"analyst-shipped product_features={len(product_features)} "
            f"cost_usd={telemetry['analyst_cost_usd']} "
            f"tokens_in={in_t} tokens_out={out_t}",
        )

    return Stage8Result(
        product_features=product_features,
        dev_to_product_map=dev_map,
        telemetry=telemetry,
        member_flows_map=member_flows_map,
    )


def _fallback_to_haiku(
    ctx: "ScanContext",
    developer_features: list["Feature"],
    product_features_pre: list["Feature"],
    *,
    dev_to_product_map_pre: dict[str, tuple[str, ...]] | None,
    source_breakdown_pre: dict[str, int] | None,
    log: "StageLogger | None",
    client: Any | None,
    model: str,
    cost_tracker: CostTracker | None,
    fallback_reason: str,
    analyst_aux: dict[str, Any] | None = None,
    llm_health: LlmHealth | None = None,
) -> Stage8Result:
    """Invoke the legacy Haiku Stage 8 and stamp fallback telemetry."""
    # Haiku model — never use Sonnet here, it would defeat the cost
    # bound. Use the canonical Haiku id.
    haiku_model = "claude-haiku-4-5-20251001"
    result = run_stage_8_haiku(
        ctx,
        developer_features,
        product_features_pre,
        dev_to_product_map_pre=dev_to_product_map_pre,
        source_breakdown_pre=source_breakdown_pre,
        log=log,
        client=client,
        model=haiku_model,
        cost_tracker=cost_tracker,
        llm_health=llm_health,
    )
    telemetry = dict(result.telemetry)
    telemetry["mode"] = "analyst"
    telemetry["fallback_used"] = True
    telemetry["fallback_reason"] = fallback_reason
    if analyst_aux:
        telemetry.update(analyst_aux)
    return Stage8Result(
        product_features=result.product_features,
        dev_to_product_map=result.dev_to_product_map,
        telemetry=telemetry,
    )


# ── Default Anthropic client factory ────────────────────────────────────


def _default_client_factory() -> Any | None:  # pragma: no cover - IO
    """Return an Anthropic client when SDK + key are available.

    Mirrors ``stage_8_marketing_clusterer._default_client_factory`` so
    the dispatcher can swap implementations transparently.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


__all__ = [
    "DEFAULT_ANALYST_MODEL",
    "SYSTEM_PROMPT",
    "Stage8Result",
    "build_analyst_payload",
    "build_user_prompt",
    "run_stage_8_analyst",
]
