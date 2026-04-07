"""
Iterative LLM feature detection — 3-phase pipeline.

Phase 1: Cluster files into broad business domains (1 LLM call)
Phase 2: Per-domain sub-feature extraction (batched LLM calls)
Phase 3: Cross-domain merge for features that span domains (1 LLM call)

Designed for large repos (500+ files) where single-shot detection
loses accuracy due to token truncation. For small repos, this gives
the same quality as single-shot but with better domain separation.

Cost overhead vs single-shot: ~2-3x (Haiku), mitigated by:
- Skipping Phase 2 for tiny domains (≤5 files)
- Batching ~5 domains per LLM call in Phase 2
- Caching results by file-structure hash
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path

import anthropic
from pydantic import BaseModel, ValidationError

from faultline.analyzer.ast_extractor import FileSignature
from faultline.models.types import Commit

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0

# Domains with ≤ this many files skip Phase 2 (auto-accept as single feature)
_SMALL_DOMAIN_THRESHOLD = 5
# Max domains per batched Phase 2 call
_BATCH_SIZE = 5
# Max files to show per domain in Phase 2 prompt
_MAX_FILES_PER_DOMAIN_PROMPT = 80
# Max tokens for LLM responses
_MAX_TOKENS_PHASE1 = 16_384
_MAX_TOKENS_PHASE2 = 8_192
_MAX_TOKENS_PHASE3 = 8_192

# Cache
_CACHE_DIR = Path.home() / ".faultline" / "llm-cache"
_CACHE_TTL_DAYS = 90


# ─── Pydantic models ─────────────────────────────────────────────────────────

class _DomainMapping(BaseModel):
    domain_name: str
    files: list[str]


class _Phase1Response(BaseModel):
    domains: list[_DomainMapping]


class _SubFeature(BaseModel):
    feature_name: str
    files: list[str]


class _DomainFeatures(BaseModel):
    domain_name: str
    features: list[_SubFeature]


class _Phase2Response(BaseModel):
    domains: list[_DomainFeatures]


class _MergeAction(BaseModel):
    merged_name: str
    original_names: list[str]


class _Phase3Response(BaseModel):
    merges: list[_MergeAction]
    keep_names: list[str]


# ─── Prompts ─────────────────────────────────────────────────────────────────

_PHASE1_SYSTEM = """\
You are a senior software architect. Your task is to cluster a codebase's files \
into broad business DOMAINS. A domain is a top-level product area, not a technical layer.

## Rules

1. Group files by the business area they serve: "authentication", "billing", "dashboard", etc.
2. Domain names: lowercase, hyphen-separated, 1-3 words.
3. Every file must appear in exactly one domain. No omissions.
4. Target 10-30 domains. Too few (< 8) means over-merging. Too many (> 40) means over-splitting.
5. Shared/utility files go into the domain they primarily serve. Only truly generic files \
   (formatDate, useDebounce, Button) go into "shared-utilities".
6. Skip infrastructure files: package.json, tsconfig.json, .gitignore, Dockerfile, CI configs.
7. Test files belong to the same domain as the code they test.
8. In Next.js apps: app/<page>/ and app/api/<page>/ belong to the SAME domain.
9. DO NOT create technical-layer domains like "components", "hooks", "api-routes", "models". \
   These are layers, not domains.

## Example

Files: app/auth/login.tsx, app/auth/signup.tsx, hooks/useAuth.ts, \
app/billing/plans.tsx, app/api/billing/route.ts, utils/formatDate.ts

Domains:
- "authentication": [app/auth/login.tsx, app/auth/signup.tsx, hooks/useAuth.ts]
- "billing": [app/billing/plans.tsx, app/api/billing/route.ts]
- "shared-utilities": [utils/formatDate.ts]\
"""

_PHASE1_USER = """\
Cluster these {n_files} files into broad business domains (target: {target_min}-{target_max} domains).
{extra_context}
<files>
{file_list}
</files>

Return every file in exactly one domain. Use business domain names, not technical layers.\
"""

_PHASE1_DIR_SYSTEM = """\
You are a senior software architect. Your task is to cluster a codebase's DIRECTORIES \
into broad business DOMAINS. A domain is a top-level product area, not a technical layer.

## Rules

1. Group directories by the business area they serve.
2. Domain names: lowercase, hyphen-separated, 1-3 words.
3. Every directory must appear in exactly one domain. No omissions.
4. Target 10-30 domains.
5. The `files` field must contain DIRECTORY PATHS exactly as listed — not sample filenames.
6. Shared/utility directories go into the domain they primarily serve.
7. Skip pure infrastructure directories: .github, ci/, scripts/, .storybook.
8. In Next.js: app/<page>/ and app/api/<page>/ belong to the SAME domain.
9. DO NOT create technical-layer domains. "hooks", "components", "utils" are NOT domains — \
   assign them to the business domain they serve.

Each line shows a directory, optionally followed by → and sample filenames for context. \
Indented lines are subdirectories.\
"""

_PHASE1_DIR_USER = """\
Cluster these {n_dirs} directories into broad business domains (target: {target_min}-{target_max} domains).
{extra_context}
<directories>
{dir_tree}
</directories>

Return directory paths exactly as listed. Every directory in exactly one domain.\
"""

_PHASE2_SYSTEM = """\
You are analyzing files within a business domain to identify distinct sub-features.

## Rules

1. Each sub-feature is a specific user-facing capability within the domain.
2. Feature names: lowercase, hyphen-separated, 1-3 words. Prefix with domain name if ambiguous.
3. Every file must appear in exactly one feature. No omissions.
4. A domain may contain 1-5 features. If all files serve the same purpose, return 1 feature.
5. Test files belong to the same feature as the code they test.
6. Do NOT split pages from their API routes — they serve the same feature.
7. No feature should exceed 40 files. Split if larger.

## Example

Domain "billing" files: plans-page.tsx, checkout-form.tsx, api/billing/route.ts, \
api/billing/invoices/route.ts, hooks/usePlans.ts, hooks/useInvoices.ts

Features:
- "billing-plans": [plans-page.tsx, checkout-form.tsx, api/billing/route.ts, hooks/usePlans.ts]
- "billing-invoices": [api/billing/invoices/route.ts, hooks/useInvoices.ts]\
"""

_PHASE2_USER = """\
Analyze {n_domains} domain(s) and split each into distinct sub-features.

{domain_blocks}

For each domain, return 1-5 features. Every file in exactly one feature.\
"""

_PHASE3_SYSTEM = """\
You are reviewing a feature list from automated analysis. Your job is to merge \
features that clearly belong to the same business capability across different domains.

## Rules

1. Merge features ONLY when they serve the exact same business purpose.
   Example: "auth-login" and "auth-session" → merge into "authentication"
2. Do NOT merge features that are distinct capabilities.
   "billing" and "user-management" stay separate even if related.
3. Return ALL feature names — either in a merge group or in keep_names.
4. Every original feature name must appear exactly once.\
"""

_PHASE3_USER = """\
Review these {n_features} features and merge any that belong to the same business capability.

Features:
{feature_list}

Return merge groups and keep_names. Every feature name must appear exactly once.\
"""


# ─── Main entry point ────────────────────────────────────────────────────────

def detect_features_iterative(
    files: list[str],
    api_key: str | None = None,
    commits: list[Commit] | None = None,
    path_prefix: str = "",
    signatures: dict[str, FileSignature] | None = None,
    layer_context: str = "",
    on_progress: callable = None,
) -> dict[str, list[str]]:
    """Iterative 3-phase feature detection.

    Args:
        files: File paths (relative, path_prefix already stripped).
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        commits: Optional commit history for context enrichment.
        path_prefix: Stripped prefix (e.g. "src/") for commit normalization.
        signatures: AST signatures for route/entity anchors.
        layer_context: Repo structure context string.
        on_progress: Optional callback(phase: int, message: str).

    Returns:
        dict mapping feature names to file lists. Empty dict on failure.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key or not files:
        return {}

    # Check cache
    cache_key = _cache_key(files)
    cached = _read_cache(cache_key)
    if cached is not None:
        return cached

    client = anthropic.Anthropic(api_key=key)

    def _progress(phase: int, msg: str):
        if on_progress:
            on_progress(phase, msg)

    # Normalize commits for co-change context
    from faultline.llm.detector import _normalize_commit_files, _compute_cochange
    norm_commits = _normalize_commit_files(commits, path_prefix) if commits and path_prefix else commits
    cochange_context = _format_cochange(norm_commits) if norm_commits else ""

    # Route anchor context
    route_context = ""
    if signatures:
        from faultline.llm.detector import _format_route_anchors
        route_context = _format_route_anchors(signatures)

    extra_context = cochange_context + route_context

    # ── Phase 1: Domain Clustering ──
    _progress(1, "Clustering files into business domains...")

    use_dir_collapse = len(files) > 500
    if use_dir_collapse:
        from faultline.llm.detector import _unique_dirs, _dir_to_sample_files, _format_dir_tree
        dirs = _unique_dirs(files)
        samples = _dir_to_sample_files(dirs, files)
        dir_tree = _format_dir_tree(dirs, samples)
        domains = _phase1_dir_clustering(client, dir_tree, len(dirs), extra_context, layer_context)
        if not domains:
            return {}
        # Expand directory domains back to file-level domains
        domains = _expand_dir_domains(domains, files)
    else:
        domains = _phase1_file_clustering(client, files, extra_context, layer_context)
        if not domains:
            return {}

    _progress(1, f"Found {len(domains)} domains")

    # ── Phase 2: Per-Domain Sub-Features ──
    _progress(2, "Extracting features per domain...")

    features: dict[str, list[str]] = {}

    # Small domains → auto-accept as single feature
    small_domains = {name: fs for name, fs in domains.items() if len(fs) <= _SMALL_DOMAIN_THRESHOLD}
    large_domains = {name: fs for name, fs in domains.items() if len(fs) > _SMALL_DOMAIN_THRESHOLD}

    for name, fs in small_domains.items():
        features[name] = fs

    # Batch large domains into groups of _BATCH_SIZE
    large_domain_items = list(large_domains.items())
    for batch_start in range(0, len(large_domain_items), _BATCH_SIZE):
        batch = dict(large_domain_items[batch_start:batch_start + _BATCH_SIZE])
        batch_features = _phase2_extract_features(client, batch)
        features.update(batch_features)

    _progress(2, f"Extracted {len(features)} features")

    # ── Phase 3: Cross-Domain Merge ──
    if len(features) > 15:
        _progress(3, "Merging related features across domains...")
        features = _phase3_merge(client, features)
        _progress(3, f"Final: {len(features)} features")

    # Post-processing (reuse existing cleanup pipeline)
    from faultline.llm.detector import (
        _collapse_plugin_features,
        _extract_shared_ui,
        _redistribute_infra_features,
        _redistribute_oversized_features,
        _final_cleanup,
    )
    features = _collapse_plugin_features(features)
    features = _extract_shared_ui(features)
    features = _redistribute_infra_features(features)
    features = _redistribute_oversized_features(features)
    features = _final_cleanup(features)

    # Cache result
    _write_cache(cache_key, features)

    return features


def detect_features_iterative_ollama(
    files: list[str],
    model: str = "llama3.1:8b",
    host: str = "http://localhost:11434",
    commits: list[Commit] | None = None,
    path_prefix: str = "",
    signatures: dict[str, FileSignature] | None = None,
    layer_context: str = "",
    on_progress: callable = None,
) -> dict[str, list[str]]:
    """Iterative 3-phase feature detection using Ollama.

    Same pipeline as Anthropic version but uses local Ollama models.
    """
    try:
        import ollama as _ollama
    except ImportError:
        return {}

    if not files:
        return {}

    cache_key = _cache_key(files, model)
    cached = _read_cache(cache_key)
    if cached is not None:
        return cached

    ollama_client = _ollama.Client(host=host)

    def _progress(phase: int, msg: str):
        if on_progress:
            on_progress(phase, msg)

    from faultline.llm.detector import _normalize_commit_files, _compute_cochange
    norm_commits = _normalize_commit_files(commits, path_prefix) if commits and path_prefix else commits
    cochange_context = _format_cochange(norm_commits) if norm_commits else ""

    route_context = ""
    if signatures:
        from faultline.llm.detector import _format_route_anchors
        route_context = _format_route_anchors(signatures)

    extra_context = cochange_context + route_context

    # ── Phase 1 ──
    _progress(1, "Clustering files into business domains...")

    use_dir_collapse = len(files) > 500
    if use_dir_collapse:
        from faultline.llm.detector import _unique_dirs, _dir_to_sample_files, _format_dir_tree
        dirs = _unique_dirs(files)
        samples = _dir_to_sample_files(dirs, files)
        dir_tree = _format_dir_tree(dirs, samples)
        domains = _phase1_dir_clustering_ollama(ollama_client, model, dir_tree, len(dirs), extra_context, layer_context)
        if not domains:
            return {}
        domains = _expand_dir_domains(domains, files)
    else:
        domains = _phase1_file_clustering_ollama(ollama_client, model, files, extra_context, layer_context)
        if not domains:
            return {}

    _progress(1, f"Found {len(domains)} domains")

    # ── Phase 2 ──
    _progress(2, "Extracting features per domain...")

    features: dict[str, list[str]] = {}
    small_domains = {name: fs for name, fs in domains.items() if len(fs) <= _SMALL_DOMAIN_THRESHOLD}
    large_domains = {name: fs for name, fs in domains.items() if len(fs) > _SMALL_DOMAIN_THRESHOLD}

    for name, fs in small_domains.items():
        features[name] = fs

    large_domain_items = list(large_domains.items())
    for batch_start in range(0, len(large_domain_items), _BATCH_SIZE):
        batch = dict(large_domain_items[batch_start:batch_start + _BATCH_SIZE])
        batch_features = _phase2_extract_features_ollama(ollama_client, model, batch)
        features.update(batch_features)

    _progress(2, f"Extracted {len(features)} features")

    # ── Phase 3 ──
    if len(features) > 15:
        _progress(3, "Merging related features across domains...")
        features = _phase3_merge_ollama(ollama_client, model, features)
        _progress(3, f"Final: {len(features)} features")

    from faultline.llm.detector import (
        _collapse_plugin_features,
        _extract_shared_ui,
        _redistribute_infra_features,
        _redistribute_oversized_features,
        _final_cleanup,
    )
    features = _collapse_plugin_features(features)
    features = _extract_shared_ui(features)
    features = _redistribute_infra_features(features)
    features = _redistribute_oversized_features(features)
    features = _final_cleanup(features)

    _write_cache(cache_key, features)
    return features


# ─── Phase 1: Domain Clustering ──────────────────────────────────────────────

def _phase1_file_clustering(
    client: anthropic.Anthropic,
    files: list[str],
    extra_context: str,
    layer_context: str,
) -> dict[str, list[str]] | None:
    """Phase 1 for small repos (≤500 files): send full file list."""
    target_min = max(8, len(files) // 50)
    target_max = min(30, max(12, len(files) // 20))

    prompt = _PHASE1_USER.format(
        n_files=len(files),
        target_min=target_min,
        target_max=target_max,
        extra_context=f"\n{extra_context}" if extra_context else "",
        file_list="\n".join(files),
    )
    system = _PHASE1_SYSTEM + layer_context

    return _call_llm_parsed(client, system, prompt, _Phase1Response, _MAX_TOKENS_PHASE1, _to_domain_dict)


def _phase1_dir_clustering(
    client: anthropic.Anthropic,
    dir_tree: str,
    n_dirs: int,
    extra_context: str,
    layer_context: str,
) -> dict[str, list[str]] | None:
    """Phase 1 for large repos (>500 files): send directory tree."""
    target_min = max(8, n_dirs // 25)
    target_max = min(30, max(12, n_dirs // 10))

    prompt = _PHASE1_DIR_USER.format(
        n_dirs=n_dirs,
        target_min=target_min,
        target_max=target_max,
        extra_context=f"\n{extra_context}" if extra_context else "",
        dir_tree=dir_tree,
    )
    system = _PHASE1_DIR_SYSTEM + layer_context

    return _call_llm_parsed(client, system, prompt, _Phase1Response, _MAX_TOKENS_PHASE1, _to_domain_dict)


def _phase1_file_clustering_ollama(
    ollama_client,
    model: str,
    files: list[str],
    extra_context: str,
    layer_context: str,
) -> dict[str, list[str]] | None:
    target_min = max(8, len(files) // 50)
    target_max = min(30, max(12, len(files) // 20))
    prompt = _PHASE1_USER.format(
        n_files=len(files),
        target_min=target_min,
        target_max=target_max,
        extra_context=f"\n{extra_context}" if extra_context else "",
        file_list="\n".join(files),
    )
    system = _PHASE1_SYSTEM + layer_context
    return _call_ollama_parsed(ollama_client, model, system, prompt, _Phase1Response, _to_domain_dict)


def _phase1_dir_clustering_ollama(
    ollama_client,
    model: str,
    dir_tree: str,
    n_dirs: int,
    extra_context: str,
    layer_context: str,
) -> dict[str, list[str]] | None:
    target_min = max(8, n_dirs // 25)
    target_max = min(30, max(12, n_dirs // 10))
    prompt = _PHASE1_DIR_USER.format(
        n_dirs=n_dirs,
        target_min=target_min,
        target_max=target_max,
        extra_context=f"\n{extra_context}" if extra_context else "",
        dir_tree=dir_tree,
    )
    system = _PHASE1_DIR_SYSTEM + layer_context
    return _call_ollama_parsed(ollama_client, model, system, prompt, _Phase1Response, _to_domain_dict)


# ─── Phase 2: Per-Domain Sub-Features ────────────────────────────────────────

def _phase2_extract_features(
    client: anthropic.Anthropic,
    domains: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Extract sub-features from a batch of domains (≤ _BATCH_SIZE domains per call)."""
    domain_blocks = _format_domain_blocks(domains)
    prompt = _PHASE2_USER.format(
        n_domains=len(domains),
        domain_blocks=domain_blocks,
    )

    result = _call_llm_parsed(
        client, _PHASE2_SYSTEM, prompt, _Phase2Response,
        _MAX_TOKENS_PHASE2, _to_features_dict,
    )
    if result:
        return result

    # Fallback: each domain is one feature
    return dict(domains)


def _phase2_extract_features_ollama(
    ollama_client,
    model: str,
    domains: dict[str, list[str]],
) -> dict[str, list[str]]:
    domain_blocks = _format_domain_blocks(domains)
    prompt = _PHASE2_USER.format(
        n_domains=len(domains),
        domain_blocks=domain_blocks,
    )
    result = _call_ollama_parsed(
        ollama_client, model, _PHASE2_SYSTEM, prompt,
        _Phase2Response, _to_features_dict,
    )
    if result:
        return result
    return dict(domains)


# ─── Phase 3: Cross-Domain Merge ─────────────────────────────────────────────

def _phase3_merge(
    client: anthropic.Anthropic,
    features: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Merge features that span the same business capability across domains."""
    feature_lines = []
    for name, fs in sorted(features.items(), key=lambda x: -len(x[1])):
        sample = ", ".join(Path(f).name for f in fs[:5])
        suffix = f" +{len(fs) - 5} more" if len(fs) > 5 else ""
        feature_lines.append(f"  {name} ({len(fs)} files): {sample}{suffix}")

    prompt = _PHASE3_USER.format(
        n_features=len(features),
        feature_list="\n".join(feature_lines),
    )

    result = _call_llm_parsed(
        client, _PHASE3_SYSTEM, prompt, _Phase3Response,
        _MAX_TOKENS_PHASE3, lambda r: r,
    )
    if not result:
        return features

    return _apply_phase3_merge(features, result)


def _phase3_merge_ollama(
    ollama_client,
    model: str,
    features: dict[str, list[str]],
) -> dict[str, list[str]]:
    feature_lines = []
    for name, fs in sorted(features.items(), key=lambda x: -len(x[1])):
        sample = ", ".join(Path(f).name for f in fs[:5])
        suffix = f" +{len(fs) - 5} more" if len(fs) > 5 else ""
        feature_lines.append(f"  {name} ({len(fs)} files): {sample}{suffix}")

    prompt = _PHASE3_USER.format(
        n_features=len(features),
        feature_list="\n".join(feature_lines),
    )
    result = _call_ollama_parsed(
        ollama_client, model, _PHASE3_SYSTEM, prompt,
        _Phase3Response, lambda r: r,
    )
    if not result:
        return features
    return _apply_phase3_merge(features, result)


def _apply_phase3_merge(
    features: dict[str, list[str]],
    merge_response: _Phase3Response,
) -> dict[str, list[str]]:
    """Applies merge instructions from Phase 3 response."""
    result: dict[str, list[str]] = {}
    merged_names: set[str] = set()

    for merge in merge_response.merges:
        merged_files: list[str] = []
        for orig_name in merge.original_names:
            if orig_name in features:
                merged_files.extend(features[orig_name])
                merged_names.add(orig_name)
        if merged_files:
            result.setdefault(merge.merged_name, []).extend(merged_files)

    # Keep features that weren't merged
    for name, fs in features.items():
        if name not in merged_names:
            result.setdefault(name, []).extend(fs)

    return result


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _to_domain_dict(response: _Phase1Response) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for domain in response.domains:
        name = domain.domain_name.lower().strip()
        result[name] = domain.files
    return result


def _to_features_dict(response: _Phase2Response) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for domain in response.domains:
        for feat in domain.features:
            name = feat.feature_name.lower().strip()
            # Deduplicate names
            if name in result:
                result[name].extend(feat.files)
            else:
                result[name] = list(feat.files)
    return result


def _format_domain_blocks(domains: dict[str, list[str]]) -> str:
    """Formats domain file lists for Phase 2 prompt."""
    blocks = []
    for name, files in domains.items():
        display_files = files[:_MAX_FILES_PER_DOMAIN_PROMPT]
        file_list = "\n".join(f"  {f}" for f in display_files)
        suffix = f"\n  ... ({len(files) - len(display_files)} more files)" if len(files) > len(display_files) else ""
        blocks.append(f'Domain "{name}" ({len(files)} files):\n{file_list}{suffix}')
    return "\n\n".join(blocks)


def _format_cochange(commits: list[Commit] | None) -> str:
    """Formats co-change pairs as LLM context."""
    if not commits:
        return ""
    from faultline.llm.detector import _compute_cochange
    pairs = _compute_cochange(commits)
    if not pairs:
        return ""
    lines = [f"  {f1} ↔ {f2} (score: {score})" for f1, f2, score in pairs[:20]]
    return f"\n<co-change-pairs>\n" + "\n".join(lines) + "\n</co-change-pairs>"


def _expand_dir_domains(
    dir_domains: dict[str, list[str]],
    all_files: list[str],
) -> dict[str, list[str]]:
    """Expands directory-level domain mapping back to individual files."""
    # Build dir → domain index
    dir_to_domain: dict[str, str] = {}
    for domain_name, dirs in dir_domains.items():
        for d in dirs:
            dir_to_domain[d.rstrip("/")] = domain_name

    result: dict[str, list[str]] = {}
    unassigned: list[str] = []

    for f in all_files:
        # Find the longest matching directory
        parts = Path(f).parts
        matched_domain = None
        for i in range(len(parts) - 1, 0, -1):
            candidate = "/".join(parts[:i])
            if candidate in dir_to_domain:
                matched_domain = dir_to_domain[candidate]
                break

        if matched_domain:
            result.setdefault(matched_domain, []).append(f)
        else:
            unassigned.append(f)

    # Put unassigned files into "shared-utilities" or the largest domain
    if unassigned:
        target = "shared-utilities" if "shared-utilities" in result else max(result, key=lambda k: len(result[k]))
        result.setdefault(target, []).extend(unassigned)

    return result


def _call_llm_parsed(
    client: anthropic.Anthropic,
    system: str,
    prompt: str,
    response_model: type[BaseModel],
    max_tokens: int,
    transform,
):
    """Generic retry wrapper for Claude API with structured output."""
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.messages.parse(
                model=_MODEL,
                max_tokens=max_tokens,
                temperature=0,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=response_model,
            )
            return transform(response.parsed_output)
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("LLM call failed (attempt %d/%d): %s. Retrying in %.1fs...", attempt + 1, _MAX_RETRIES, e, delay)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(delay)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                anthropic.NotFoundError, ValidationError, anthropic.APIStatusError):
            logger.warning("LLM call failed permanently: %s", type(e).__name__ if 'e' in dir() else "unknown")
            return None
    return None


def _call_ollama_parsed(
    ollama_client,
    model: str,
    system: str,
    prompt: str,
    response_model: type[BaseModel],
    transform,
):
    """Generic wrapper for Ollama with structured output."""
    try:
        response = ollama_client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            format=response_model.model_json_schema(),
        )
        parsed = response_model.model_validate_json(response.message.content)
        return transform(parsed)
    except (ValidationError, Exception) as e:
        logger.warning("Ollama call failed: %s", e)
        return None


# ─── Cache ────────────────────────────────────────────────────────────────────

def _cache_key(files: list[str], model: str = _MODEL) -> str:
    content = json.dumps(sorted(files)) + model
    return "iterative-" + hashlib.sha256(content.encode()).hexdigest()[:16]


def _read_cache(key: str) -> dict[str, list[str]] | None:
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    from datetime import datetime
    age_days = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).days
    if age_days > _CACHE_TTL_DAYS:
        path.unlink()
        return None
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and all(isinstance(v, list) for v in data.values()):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _write_cache(key: str, features: dict[str, list[str]]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.json").write_text(json.dumps(features))


# ─── Cost estimation ─────────────────────────────────────────────────────────

def estimate_cost(
    n_files: int,
    n_domains: int = 0,
) -> dict[str, float]:
    """Estimates API cost for iterative detection.

    Returns dict with per-phase and total costs in USD.
    Uses Haiku 4.5 pricing: $1.00/MTok input, $5.00/MTok output.
    """
    input_price = 1.00 / 1_000_000  # per token
    output_price = 5.00 / 1_000_000

    # Phase 1: system (~1500 tokens) + file list (~n_files * 8 tokens avg)
    p1_input = 1500 + n_files * 8
    p1_output = 3000  # domain mapping
    p1_cost = p1_input * input_price + p1_output * output_price

    # Estimate domains if not provided
    if not n_domains:
        n_domains = max(10, min(30, n_files // 30))

    # Phase 2: skip small domains, batch the rest
    large_domains = max(1, int(n_domains * 0.6))  # ~60% need Phase 2
    n_calls = max(1, (large_domains + _BATCH_SIZE - 1) // _BATCH_SIZE)
    avg_files_per_call = (n_files * 0.6) / n_calls  # 60% of files in large domains
    p2_input_per_call = 800 + avg_files_per_call * 8
    p2_output_per_call = 2000
    p2_cost = n_calls * (p2_input_per_call * input_price + p2_output_per_call * output_price)

    # Phase 3: feature names + sample files
    p3_input = 1000 + n_domains * 2 * 40  # ~40 tokens per feature line
    p3_output = 1500
    p3_cost = p3_input * input_price + p3_output * output_price

    total = p1_cost + p2_cost + p3_cost

    return {
        "phase1": round(p1_cost, 4),
        "phase2": round(p2_cost, 4),
        "phase3": round(p3_cost, 4),
        "total": round(total, 4),
        "total_batch": round(total * 0.5, 4),  # Batch API = 50% off
        "n_llm_calls": 1 + n_calls + 1,
    }
