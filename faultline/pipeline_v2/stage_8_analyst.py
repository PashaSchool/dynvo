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
from faultline.pipeline_v2.nav_taxonomy import pin_nav_labels
from faultline.pipeline_v2.naming_validator import (
    EvidenceBundle,
    retry_prohibition,
    validate_name,
)
from faultline.pipeline_v2.product_strings import (
    ProductStringIndex,
    collect_product_strings,
)
from faultline.pipeline_v2.stage_8_7_anchor_desink import _is_workspace_anchor
from faultline.pipeline_v2.stage_8_marketing_clusterer import (
    Stage8Result,
    fetch_marketing_taxonomy,
    llm_cache_enabled,
    llm_cache_get,
    llm_cache_key,
    llm_cache_put,
    run_stage_8 as run_stage_8_haiku,
)

if TYPE_CHECKING:
    from faultline.cache.backend import CacheBackend
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

# Product strings shown per dev feature in the analyst payload.
# Matches the _MAX_PATHS_PER_FEATURE per-feature evidence budget —
# same structural scale, not corpus-tuned.
_MAX_PRODUCT_STRINGS_PER_FEATURE = 5

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


def _anchor_paths_of(feature: "Feature") -> list[str]:
    """Anchor files of a feature — Stage 2.6 ``member_files`` with role
    ``anchor`` when present, else the head of ``paths`` (pre-closure
    scans). Anchor-first evidence ordering (naming review №3)."""
    anchors = [
        mf.path for mf in (feature.member_files or []) if mf.role == "anchor"
    ]
    return anchors or list(feature.paths or [])[:1]


def _compact_dev_features(
    developer_features: list["Feature"],
    product_strings: ProductStringIndex | None = None,
) -> list[dict[str, Any]]:
    """Project the top-N dev features into a lean payload shape.

    Sorted by path count descending — the assumption is that
    higher-path features are more important / more grounded.

    Naming-evidence core (2026-06): each feature additionally carries
    ``product_strings`` — the human-facing strings (nav labels, page
    titles, default-locale i18n copy) collected from ITS OWN member
    files, anchor files first. Empty list when the repo carries none.
    """
    sorted_df = sorted(
        developer_features, key=lambda f: -len(f.paths or []),
    )[:_MAX_DEV_FEATURES_IN_PAYLOAD]
    out: list[dict[str, Any]] = []
    for f in sorted_df:
        strings: list[str] = []
        if product_strings is not None:
            strings = product_strings.bundle_for(
                f.paths or [],
                anchor_paths=_anchor_paths_of(f),
                cap=_MAX_PRODUCT_STRINGS_PER_FEATURE,
            )
        out.append({
            "name": f.name,
            "display_name": f.display_name,
            "description": (f.description or "")[:_MAX_DESCRIPTION_CHARS],
            "n_paths": len(f.paths or []),
            "sample_paths": list(f.paths or [])[:_MAX_PATHS_PER_FEATURE],
            "product_strings": strings,
        })
    return out


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


def collect_index_for_features(
    repo_path: Path,
    developer_features: list["Feature"],
) -> ProductStringIndex:
    """Collect the product-string index over every member file of every
    dev feature (paths + Stage 2.6 member_files closure). Deterministic;
    README/prose docs structurally excluded inside the collector."""
    candidates: set[str] = set()
    for f in developer_features:
        candidates.update(f.paths or [])
        candidates.update(mf.path for mf in (f.member_files or []))
    return collect_product_strings(repo_path, candidates)


def build_analyst_payload(
    ctx: "ScanContext",
    developer_features: list["Feature"],
    top_flows: list["Flow"] | None = None,
    product_strings: ProductStringIndex | None = None,
    nav_taxonomy_map: dict[str, str] | None = None,
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
    if product_strings is None:
        product_strings = collect_index_for_features(
            ctx.repo_path, developer_features,
        )
    return {
        "slug": slug,
        "audited_stack": ctx.audited_stack or ctx.stack or "",
        "secondary_stacks": list(ctx.secondary_stacks or ()),
        "workspace_manager": ctx.workspace_manager,
        "root_package": root_pkg,
        "auditor_hints": list(ctx.extractor_hints or ()),
        "workspace_packages": workspace_packages,
        "developer_features": _compact_dev_features(
            developer_features, product_strings,
        ),
        "flows": _compact_flows(top_flows),
        "marketing_text": marketing_text,
        "marketing_url": marketing_url,
        "taxonomy_size": taxonomy_size,
        "product_strings_files": len(product_strings.by_file),
        "product_strings_total": product_strings.total_strings,
        # In-repo nav taxonomy (vendor-declared) — labels the matcher
        # already pinned to dev features. Shown to the analyst so its
        # synthesis aligns with (and never contradicts) the vendor's
        # own framing; the deterministic pin happens post-emission.
        "nav_taxonomy_labels": sorted(set((nav_taxonomy_map or {}).values())),
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
    nav_labels = payload.get("nav_taxonomy_labels") or []
    nav_section = ""
    if nav_labels:
        nav_section = (
            "IN-REPO NAV TAXONOMY (the vendor's OWN nav/sidebar + route "
            "hierarchy — the author's product framing, versioned with the "
            "code; HIGHER trust than marketing surfaces. Use these labels "
            "verbatim when a product feature corresponds to one):\n"
            + "\n".join("- " + lab for lab in nav_labels) + "\n\n"
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
        "DEVELOPER FEATURES (top by path count, deterministic Layer 1; "
        "each feature's product_strings are human-facing nav labels / "
        "page titles / i18n copy extracted from ITS OWN files — prefer "
        "this vocabulary when naming, and never use product words that "
        "appear in NO feature's evidence):\n"
        + json.dumps(payload["developer_features"], indent=2) + "\n\n"
        + flows_section
        + nav_section
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


# ── Post-LLM name validation (anti-hallucination, deterministic) ────────


_RENAME_SYSTEM_PROMPT = (
    "You fix product-feature names that contain unsupported words. "
    "For each entry you get the offending name, the prohibited words "
    "(no evidence in the feature's files/strings/history), and the "
    "feature's actual evidence (member dev features, sample paths, "
    "product strings). Re-name each entry using ONLY vocabulary present "
    "in its evidence. Keep names specific and product-grain. "
    'Output STRICT JSON only: {"renames": [{"old": "...", "new": "..."}]}'
)

# Evidence rows shown per failing name in the rename-retry prompt.
_MAX_RETRY_EVIDENCE_ROWS = 10


def _file_commit_tokens(commits: list[Any]) -> dict[str, set[str]]:
    """One-pass map of file → tokens of every commit message touching it.

    Commit messages are legitimate naming evidence (the namers already
    see them); per-FILE attachment keeps the vendor-domination rule
    honest (a vendor mentioned in one commit grounds only the files of
    that commit). Token sets are shared per commit — cheap."""
    from faultline.pipeline_v2.naming_validator import _tokenize_evidence_text

    out: dict[str, set[str]] = {}
    for c in commits or []:
        msg = getattr(c, "message", "") or ""
        files = getattr(c, "files_changed", None) or []
        if not msg or not files:
            continue
        toks = _tokenize_evidence_text(msg)
        for fp in files:
            out.setdefault(fp, set()).update(toks)
    return out


def _bundle_for_pf(
    pf: Any,
    contrib: list["Feature"],
    *,
    product_strings: ProductStringIndex | None,
    marketing_text: str,
    workspace_packages: list[str],
    commit_tokens: dict[str, set[str]] | None,
) -> EvidenceBundle:
    """Evidence bundle for one emitted product feature: member-file
    paths (+ their product strings + commit-message tokens) per file;
    dev-feature names/descriptions, workspace packages and the external
    marketing surface (allowed grounding) as global evidence."""
    b = EvidenceBundle()
    for p in pf.paths or []:
        b.add_file(p)
        if product_strings is not None:
            for row in product_strings.strings_for_file(p):
                b.add_file(p, row.text)
        if commit_tokens is not None and p in commit_tokens:
            b.add_file_tokens(p, commit_tokens[p])
    for df in contrib:
        b.add_global(df.name, df.display_name or "", df.description or "")
        for sa in (df.symbol_attributions or [])[:200]:
            b.add_global(getattr(sa, "symbol", "") or "")
    b.add_global(*workspace_packages)
    if marketing_text:
        b.add_global(marketing_text)
    return b


def _call_rename_retry(
    client: Any,
    *,
    model: str,
    failing: list[dict[str, Any]],
    cost_tracker: CostTracker | None,
    llm_health: LlmHealth | None,
    cache: "CacheBackend | None" = None,
) -> tuple[dict[str, str], bool]:
    """ONE retry call for the failing names. Returns ``({old: new},
    cache_hit)`` (empty mapping on any failure — caller falls back
    deterministically).

    ``cache`` (content-hash short-circuit, CacheKind.LLM_PRODUCT_CLUSTER):
    the rename prompt is deterministic given the failing entries + their
    prohibitions (both content-derived), so an unchanged repo replays the
    stored renames at $0. Failures / empty rename sets are never cached.
    """
    prohibitions = {
        e["name"]: list(e["prohibited"]) for e in failing
    }
    user = (
        "ENTRIES TO RENAME:\n"
        + json.dumps(failing, indent=2)
        + "\n"
        + retry_prohibition(prohibitions)
    )

    # ── Cache lookup ──
    key: str | None = None
    if cache is not None:
        key = llm_cache_key(model, _RENAME_SYSTEM_PROMPT, user)
        raw = llm_cache_get(cache, key, "renames")
        if isinstance(raw, dict) and raw:
            cached = {
                k: v for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            }
            if cached:
                return cached, True

    text, in_t, out_t, _ = _call_sonnet(
        client, model=model, system=_RENAME_SYSTEM_PROMPT, user=user,
        max_tokens=2000, llm_health=llm_health,
    )
    if cost_tracker is not None and (in_t or out_t):
        cost_tracker.record(
            provider="anthropic", model=model,
            input_tokens=in_t, output_tokens=out_t,
            label="stage_8_name_validator_retry",
        )
    if not text:
        return {}, False
    try:
        obj = json.loads(_strip_code_fences(text))
    except json.JSONDecodeError:
        return {}, False
    if not isinstance(obj, dict):
        return {}, False
    out: dict[str, str] = {}
    for row in obj.get("renames") or []:
        if (
            isinstance(row, dict)
            and isinstance(row.get("old"), str)
            and isinstance(row.get("new"), str)
            and row["new"].strip()
        ):
            out[row["old"]] = row["new"].strip()
    # MISS → persist a NON-EMPTY rename set (an empty mapping is
    # indistinguishable from a failed call, so it is never cached).
    if out and key is not None and cache is not None:
        llm_cache_put(cache, key, "renames", out)
    return out, False


def _slugify(label: str) -> str:
    return label.lower().replace(" ", "-").replace("/", "-")


def _anchor_blob_owner(contrib: list["Feature"]) -> "Feature | None":
    """Return the workspace-anchor developer feature that *dominates* this
    product feature's membership, or ``None``.

    A monorepo **workspace anchor** (``soc0-frontend`` / ``backend`` — the
    package-ROOT catch-all, marked ``"workspace anchor"`` in its
    ``description``) is never a single product capability: it is the whole
    app. When the analyst groups such an anchor under a product feature and
    that anchor owns MORE files than every other contributor combined, the
    product feature simply *is* the catch-all bucket — and any specific
    marketing label the LLM coined for it (``custom-date-range-and-preset-
    filters`` over 546 frontend files) is unrepresentative of the 80-95 %
    of files it never describes. The name-token validator misses this: a
    minority feature's tokens DO appear in the bundle, so the name grounds
    even though it is not representative.

    Dominance is a strict majority of contributing-feature paths — a
    scale-invariant structural test, no tuned cutoff (rule-no-magic-tuning).
    Dependency-category **package anchors** (``auth`` / ``i18n`` /
    ``stripe``) are deliberately excluded: those legitimately own their
    consumers and map to a coherent product feature, so ``_is_workspace_
    anchor`` (which matches only the ``"workspace anchor"`` marker, never
    ``"package anchor"``) is the right discriminator.
    """
    anchors = [d for d in contrib if _is_workspace_anchor(d)]
    if not anchors:
        return None
    anchor = max(anchors, key=lambda d: len(d.paths or []))
    anchor_paths = len(anchor.paths or [])
    other_paths = sum(len(d.paths or []) for d in contrib if d is not anchor)
    return anchor if anchor_paths > other_paths else None


def _validate_pf_names(
    ctx: "ScanContext",
    product_features: list["Feature"],
    dev_map: dict[str, tuple[str, ...]],
    member_flows_map: dict[str, list[str]],
    developer_features: list["Feature"],
    payload: dict[str, Any],
    product_strings: ProductStringIndex | None,
    *,
    client: Any | None,
    model: str,
    cost_tracker: CostTracker | None,
    llm_health: LlmHealth | None,
    log: "StageLogger | None",
    pinned_slugs: set[str] | None = None,
    cache: "CacheBackend | None" = None,
) -> dict[str, Any]:
    """Anti-hallucination pass over the analyst's PF names (review №2).

    ``pinned_slugs`` — PFs carrying a vendor label pinned from the
    in-repo nav taxonomy. They are exempt: the label is structural
    evidence by construction (it is literally declared in the repo's
    nav registry / route tree), so token-checking it against the PF's
    member-file bundle would punish exactly the highest-trust names.

    Contract: every content token of a PF name must appear in that PF's
    evidence bundle. Failures get ONE batched retry with an explicit
    prohibition; names still failing fall back to the deterministic slug
    of the PF's largest member dev feature with ``name_confidence="low"``.
    Mutates ``product_features`` / ``dev_map`` / ``member_flows_map``
    in place (slug renames must stay consistent across all three).
    """
    by_name = {f.name: f for f in developer_features}
    contrib_by_slug: dict[str, list["Feature"]] = defaultdict(list)
    for dev, slugs in dev_map.items():
        df = by_name.get(dev)
        if df is None:
            continue
        for s in slugs:
            contrib_by_slug[s].append(df)

    commit_tokens = _file_commit_tokens(getattr(ctx, "commits", None) or [])
    workspace_packages = list(payload.get("workspace_packages") or [])
    marketing_text = payload.get("marketing_text") or ""
    # Default ON; disable via ``FAULTLINE_PF_ANCHOR_NAME_GUARD=0``.
    anchor_guard_on = (
        os.environ.get("FAULTLINE_PF_ANCHOR_NAME_GUARD", "1") != "0"
    )

    used_slugs = {pf.name for pf in product_features}

    def _reslug(pf: "Feature", new_label: str) -> None:
        """Apply a new label + slug, keeping the maps consistent."""
        old_slug = pf.name
        new_slug = _slugify(new_label)
        if new_slug != old_slug and new_slug in used_slugs:
            i = 2
            while f"{new_slug}-{i}" in used_slugs:
                i += 1
            new_slug = f"{new_slug}-{i}"
        used_slugs.discard(old_slug)
        used_slugs.add(new_slug)
        pf.name = new_slug
        pf.display_name = new_label
        if old_slug != new_slug:
            for dev, slugs in dev_map.items():
                if old_slug in slugs:
                    dev_map[dev] = tuple(
                        new_slug if s == old_slug else s for s in slugs
                    )
            if old_slug in member_flows_map:
                member_flows_map[new_slug] = member_flows_map.pop(old_slug)

    bundles: dict[str, EvidenceBundle] = {}
    failing: list[dict[str, Any]] = []
    anchor_guarded = 0
    for pf in product_features:
        if pinned_slugs and pf.name in pinned_slugs:
            continue
        # Workspace-anchor blob guard: a product feature whose membership is
        # dominated by a package-root catch-all is renamed to that anchor (an
        # honest structural name) instead of keeping the LLM's specific — and
        # unrepresentative — marketing label. Token validation is skipped: the
        # anchor name is structural evidence by construction.
        if anchor_guard_on:
            anchor = _anchor_blob_owner(contrib_by_slug.get(pf.name, []))
            if anchor is not None:
                _reslug(pf, anchor.display_name or anchor.name)
                pf.name_confidence = "low"
                anchor_guarded += 1
                continue
        bundle = _bundle_for_pf(
            pf,
            contrib_by_slug.get(pf.name, []),
            product_strings=product_strings,
            marketing_text=marketing_text,
            workspace_packages=workspace_packages,
            commit_tokens=commit_tokens,
        )
        bundles[pf.name] = bundle
        if bundle.is_poor:
            pf.name_confidence = "low"
            continue
        label = pf.display_name or pf.name
        verdict = validate_name(label, bundle)
        if verdict.ok:
            continue
        contrib = contrib_by_slug.get(pf.name, [])
        strings: list[str] = []
        if product_strings is not None:
            strings = product_strings.bundle_for(
                pf.paths or [], cap=_MAX_RETRY_EVIDENCE_ROWS,
            )
        failing.append({
            "name": label,
            "slug": pf.name,
            "prohibited": verdict.all_violations,
            "member_dev_features": [
                d.name for d in contrib
            ][:_MAX_RETRY_EVIDENCE_ROWS],
            "sample_paths": list(pf.paths or [])[:_MAX_RETRY_EVIDENCE_ROWS],
            "product_strings": strings,
        })

    telemetry: dict[str, Any] = {
        "pf_names_checked": len(product_features),
        "pf_names_invalid": len(failing),
        "pf_names_renamed": 0,
        "pf_names_fallback": 0,
        "pf_names_anchor_guarded": anchor_guarded,
        "validator_retry_called": False,
        "rename_cache_hits": 0,
        "rename_llm_calls": 0,
    }
    if not failing:
        return telemetry

    renames: dict[str, str] = {}
    if client is not None and (llm_health is None or llm_health.should_call()):
        telemetry["validator_retry_called"] = True
        renames, rename_hit = _call_rename_retry(
            client, model=model, failing=failing,
            cost_tracker=cost_tracker, llm_health=llm_health,
            cache=cache,
        )
        telemetry["rename_cache_hits"] = 1 if rename_hit else 0
        telemetry["rename_llm_calls"] = 0 if rename_hit else 1

    pf_by_slug: dict[str, "Feature"] = {
        pf.name: pf for pf in product_features
    }
    for entry in failing:
        pf_obj = pf_by_slug.get(entry["slug"])
        if pf_obj is None:
            continue
        new_label = renames.get(entry["name"])
        if new_label:
            verdict = validate_name(new_label, bundles[entry["slug"]])
            if verdict.ok:
                _reslug(pf_obj, new_label)
                telemetry["pf_names_renamed"] += 1
                continue
        # Second failure (or no/invalid retry) → deterministic slug of
        # the largest member dev feature; never synthesize.
        contrib = contrib_by_slug.get(entry["slug"], [])
        fallback_df = max(
            contrib, key=lambda d: (len(d.paths or []), d.name), default=None,
        )
        if fallback_df is not None:
            _reslug(pf_obj, fallback_df.display_name or fallback_df.name)
        pf_obj.name_confidence = "low"
        telemetry["pf_names_fallback"] += 1
        if log is not None:
            log.warn(
                f"name-validator-fallback pf={entry['name']!r} "
                f"prohibited={entry['prohibited']}",
            )

    return telemetry


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
    nav_taxonomy_map: dict[str, str] | None = None,
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
            nav_taxonomy_map=nav_taxonomy_map,
        )

    product_strings = collect_index_for_features(
        ctx.repo_path, developer_features,
    )
    payload = build_analyst_payload(
        ctx, developer_features, top_flows, product_strings,
        nav_taxonomy_map,
    )
    if log is not None:
        log.info(
            f"analyst-payload-built dev_features={len(payload['developer_features'])} "
            f"flows={len(payload['flows'])} "
            f"workspace_packages={len(payload['workspace_packages'])} "
            f"taxonomy_size={payload['taxonomy_size']} "
            f"product_strings={payload['product_strings_total']}"
            f"/{payload['product_strings_files']}f",
        )

    user_prompt = build_user_prompt(payload)

    # ── Content-hash LLM cache (CacheKind.LLM_PRODUCT_CLUSTER) ─────────
    # The analyst's Sonnet call does NOT use deterministic sampling params,
    # so re-running it on an unchanged repo is THE noise source for
    # product_feature_id stamps. A warm entry replays the PARSED analyst
    # dict through the SAME emission/validation code below — byte-identical
    # Layer 2 at $0. Backend comes from the scan context (same as Stage 3 /
    # the marketing cache); env opt-out FAULTLINE_STAGE_8_CACHE=0. Parse
    # failures are never cached; cache faults fall through to a live call.
    llm_cache: "CacheBackend | None" = getattr(ctx, "cache_backend", None)
    if llm_cache is not None and not llm_cache_enabled():
        llm_cache = None
    llm_cache_hits = 0
    llm_calls = 0

    def _cached_analysis(key: str | None) -> dict[str, Any] | None:
        """Stored parsed analyst dict, validated with the SAME structural
        check as ``_parse_analyst_response`` (product_features is a list)."""
        if key is None or llm_cache is None:
            return None
        raw = llm_cache_get(llm_cache, key, "analysis")
        if isinstance(raw, dict) and isinstance(raw.get("product_features"), list):
            return raw
        return None

    key_main: str | None = (
        llm_cache_key(model, SYSTEM_PROMPT, user_prompt)
        if llm_cache is not None else None
    )
    parsed = _cached_analysis(key_main)
    retried = False
    if parsed is not None:
        # HIT: no Sonnet call, no tokens, $0.
        llm_cache_hits += 1
        in_t = out_t = 0
        elapsed = 0.0
        cost = 0.0
    else:
        text, in_t, out_t, elapsed = _call_sonnet(
            client, model=model, system=SYSTEM_PROMPT, user=user_prompt,
            llm_health=llm_health,
        )
        llm_calls += 1
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
        if parsed is not None and key_main is not None and llm_cache is not None:
            llm_cache_put(llm_cache, key_main, "analysis", parsed)
        if parsed is None and text:
            # One retry with a stricter prompt suffix (its OWN content key —
            # the suffix changes the user prompt).
            if log is not None:
                log.warn(
                    "analyst-parse-failed — retrying with stricter suffix",
                )
            retried = True
            retry_user = user_prompt + _RETRY_SUFFIX
            key_retry: str | None = (
                llm_cache_key(model, SYSTEM_PROMPT, retry_user)
                if llm_cache is not None else None
            )
            parsed = _cached_analysis(key_retry)
            if parsed is not None:
                llm_cache_hits += 1
            else:
                text2, in2, out2, _ = _call_sonnet(
                    client,
                    model=model,
                    system=SYSTEM_PROMPT,
                    user=retry_user,
                    llm_health=llm_health,
                )
                llm_calls += 1
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
                if parsed is not None and key_retry is not None and llm_cache is not None:
                    llm_cache_put(llm_cache, key_retry, "analysis", parsed)

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
            nav_taxonomy_map=nav_taxonomy_map,
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
            nav_taxonomy_map=nav_taxonomy_map,
            analyst_aux={
                "analyst_cost_usd": round(cost, 6),
                "prompt_input_tokens": in_t,
                "prompt_output_tokens": out_t,
                "retried": retried,
            },
        )

    # In-repo nav taxonomy pin (vendor-declared Layer 2) — matched
    # clusters get the VENDOR'S label (renamed / created PFs,
    # name_confidence="high"); the analyst's synthesis survives for
    # unmatched clusters and its descriptions are kept as refinement.
    pinned_slugs, pin_telemetry = pin_nav_labels(
        product_features, dev_map, member_flows_map,
        nav_taxonomy_map or {}, developer_features,
    )

    # Anti-hallucination name validation (naming review №2) — every PF
    # name token must be evidenced; one retry, then deterministic slug
    # fallback + name_confidence="low". Mutates the three structures in
    # place so slugs stay consistent. Nav-pinned PFs are exempt (their
    # labels are structural evidence by construction).
    validator_telemetry = _validate_pf_names(
        ctx, product_features, dev_map, member_flows_map,
        developer_features, payload, product_strings,
        client=client, model=model, cost_tracker=cost_tracker,
        llm_health=llm_health, log=log, pinned_slugs=pinned_slugs,
        cache=llm_cache,
    )

    # Degraded-scan stamp (naming review №6): when the key died mid-scan
    # the analyst output may be partial / unvalidated — mark every PF
    # name low-confidence. Deterministic fallbacks already are.
    # Nav-pinned PFs stay "high": their labels are deterministic
    # structural evidence, unaffected by LLM health.
    if llm_health is not None and llm_health.auth_failed:
        for pf in product_features:
            if pf.name not in pinned_slugs:
                pf.name_confidence = "low"

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
        # Content-hash LLM cache counters (main + parse-retry calls; the
        # name-validator's rename call reports its own via
        # rename_cache_hits / rename_llm_calls below).
        "llm_cache_hits": llm_cache_hits,
        "llm_calls": llm_calls,
        "product_features_emitted": len(product_features),
        "phantom_count_estimate": phantom_count,
        "developer_features_mapped": sum(
            1 for v in dev_map.values() if v
        ),
        "developer_features_unmapped": sum(
            1 for f in developer_features if not dev_map.get(f.name)
        ),
        "confidence": 0.90,
        "product_strings_files": payload["product_strings_files"],
        "product_strings_total": payload["product_strings_total"],
        "nav_taxonomy_pinned_pfs": len(pinned_slugs),
        **pin_telemetry,
        **validator_telemetry,
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
    nav_taxonomy_map: dict[str, str] | None = None,
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
        nav_taxonomy_map=nav_taxonomy_map,
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
    "collect_index_for_features",
    "run_stage_8_analyst",
]
