import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path

import anthropic
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds, doubles each attempt

from faultline.analyzer.ast_extractor import FileSignature
from faultline.models.types import Commit, Feature

_MODEL = "claude-haiku-4-5-20251001"
_MAX_SAMPLE_PATHS = 5
_MAX_FEATURES_PER_CALL = 50
_MAX_FILES_FOR_DETECTION = 500

# Token budgets for LLM responses.
# The Anthropic SDK requires streaming for max_tokens > ~21,333 when using
# messages.parse() (non-streaming). Stay well below that limit.
# Dir-collapse responses list directory paths (~4–6K tokens in practice),
# so 16,384 is more than sufficient even for repos with 500+ unique dirs.
_MAX_TOKENS_FILE = 16_384
_MAX_TOKENS_DIR  = 16_384

_DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
_DEFAULT_OLLAMA_HOST = "http://localhost:11434"

# When file count exceeds this, collapse to unique directories to save tokens
_DIR_COLLAPSE_THRESHOLD = 500
# Max route-anchor entries injected into the LLM prompt
_MAX_ROUTE_ANCHOR_FILES = 25
_MAX_ROUTES_PER_ENTRY   = 3
# Max sample filenames shown per directory in the enriched dir tree
_DIR_SAMPLE_FILES = 4
# Top co-change pairs to include in the prompt
_MAX_COCHANGE_IN_PROMPT = 20
# Max clusters per single merge LLM call (avoids prompt/response truncation)
_MERGE_CHUNK_SIZE = 40

_COMMIT_STOP_WORDS = {
    # conventional commit types
    "feat", "fix", "chore", "docs", "test", "refactor", "style", "perf", "ci",
    # generic coding verbs
    "add", "update", "remove", "change", "move", "delete", "create", "handle",
    "support", "use", "implement", "improve", "cleanup", "clean", "minor", "wip",
    # English stop words
    "the", "a", "an", "and", "or", "in", "to", "for", "of", "with", "is",
    "it", "this", "that", "not", "be", "from", "by", "on", "at", "as", "are",
}

_DETECTION_SYSTEM_PROMPT = """\
You are a senior software architect analyzing a codebase's file tree to identify semantic business features.

## Task

Given a list of file paths from a git repository, group them into business features. A feature is a user-facing capability or business domain area, not a technical layer.

## Rules

1. Group files by the business domain they serve, not by technical role. Files from different directories (components, stores, API routes, tests) that serve the same business purpose belong to the same feature.
2. Use business domain terminology for feature names. Prefer "user-auth" over "authentication-module", "order-checkout" over "stripe-integration", "content-search" over "elasticsearch-wrapper".
3. Feature names must be lowercase, hyphen-separated, 1-3 words. Examples: "user-auth", "payment-processing", "dashboard", "notifications", "team-management".
4. Each file must appear in exactly one feature. No duplicates, no omissions.
5. Group by business domain, not by directory. Each distinct business domain should be its own feature. Merge only when two groups serve the exact same domain.
6. Every feature must contain at least 3 files. Single or pair files must be merged into the closest related feature.
7. Test files belong to the same feature as the code they test. Match by naming convention (test_auth.py belongs with auth.py, UserService.test.ts belongs with UserService.ts).
8. Skip infrastructure and tooling files entirely: package.json, pyproject.toml, setup.py, .gitignore, Makefile, *.lock, *.toml, Dockerfile, docker-compose.yml, CI configs.
9. Shared utility files go into the most closely related business feature, or into "shared-utilities" only if they truly cross all feature boundaries.
10. For monorepo structures, group by business feature across packages when the same domain spans multiple packages.
11. If a <route-anchors> section is provided, treat those files as strong feature anchors.
    Files that define API routes (GET/POST/PUT/DELETE) are entry points to a feature — group
    other files in the same directory tree with the file that shares their route prefix.

## Anti-patterns

BAD — grouping by technical layer:
  "components": [LoginForm.tsx, CheckoutForm.tsx, Dashboard.tsx]
  "api": [auth.ts, payments.ts, analytics.ts]

GOOD — grouping by business domain:
  "user-auth": [LoginForm.tsx, auth.ts]
  "checkout": [CheckoutForm.tsx, payments.ts]
  "analytics": [Dashboard.tsx, analytics.ts]

## Example

Files:
  components/LoginForm.tsx
  components/CheckoutForm.tsx
  api/auth/login.ts
  api/payments/charge.ts
  hooks/useSession.ts
  utils/currency.ts

Reasoning: LoginForm.tsx, api/auth/login.ts, and hooks/useSession.ts all serve user authentication across different technical layers. CheckoutForm.tsx and api/payments/charge.ts handle payment processing. utils/currency.ts is a shared utility — assign to the feature that uses it most.

Result:
  "user-auth": [components/LoginForm.tsx, api/auth/login.ts, hooks/useSession.ts]
  "checkout": [components/CheckoutForm.tsx, api/payments/charge.ts, utils/currency.ts]\
"""

_DETECTION_USER_PROMPT = """\
Analyze these repository files and group them into semantic business features.
{feature_hint}
<file_list>
{file_tree}
</file_list>{extra_context}
Return the JSON mapping of features to files. Skip infrastructure/config files. Each file in exactly one feature. Use business domain names.\
"""

# ── Dir-collapse variants (used when file count > _DIR_COLLAPSE_THRESHOLD) ──
# The input is DIRECTORIES (with sample filenames for context), not individual
# files. The LLM must return directory paths in the `files` field, not filenames.

_DIR_DETECTION_SYSTEM_PROMPT = """\
You are a senior software architect grouping a large codebase's directories into semantic business features.

## Task

You will receive a list of DIRECTORIES. Each line shows a directory path, optionally followed \
by → and a few sample filenames to illustrate what that directory contains. \
Indented lines are subdirectories of the line above them — use this nesting to understand \
how the codebase is structured.

Group these directories into business features. A feature is a user-facing capability or \
business domain area, not a technical layer.

## Rules

1. Group directories by the business domain they serve, not by technical role.
2. Feature names: lowercase, hyphen-separated, 1-3 words. \
   Examples: "user-auth", "app-router", "build-pipeline", "image-optimization".
3. Every directory must appear in exactly one feature. No omissions.
4. The `files` field in your response must contain DIRECTORY PATHS exactly as shown in the \
   input — not the sample filenames after →, not expanded sub-paths, not invented paths.
5. Balance granularity: each distinct business capability gets its own feature. \
   Do NOT lump unrelated capabilities into a single feature just because they share a parent directory. \
   For example, "billing", "webhooks", "templates", "auth" are SEPARATE features, not one "core-platform".
6. Deeply nested subdirectories almost always belong to the same feature as their parent. \
   Only split siblings when they serve clearly different business domains (e.g. "payments" vs "auth").
10. IMPORTANT: Look at the sample filenames after → to detect MULTIPLE business domains within \
   a single directory. In Django/Rails/Flask apps, one directory often contains many business \
   modules: e.g. if sample files show barcodes.py, classifier.py, bulk_edit.py, mail.py, \
   signals.py → this directory spans barcode-detection, classification, bulk-operations, etc. \
   When a directory has many sample files suggesting different business domains, assign that \
   directory to the MOST DOMINANT domain. The other domains will be in sibling directories or \
   identified elsewhere.
7. Technical directories (utils, helpers, hooks, providers, components, types, models, schemas, \
   middleware, config, constants) are NOT features — absorb them into the business feature they support.
8. Skip pure infrastructure directories: .storybook, __mocks__, .github, ci/, scripts/, etc.
9. If a <route-anchors> section is provided, directories with routes are strong anchors.
   Assign nearby sibling directories to the same feature as the directory that shares
   the same route prefix (e.g. /api/payments/* dirs belong to the payments feature).

## Anti-patterns

BAD — too many tiny features (one dir = one feature):
  "login-form":  ["src/auth/login"]
  "signup-form": ["src/auth/signup"]
  "auth-utils":  ["src/auth/utils"]   ← these are all one feature

GOOD — grouped by business domain:
  "user-auth":   ["src/auth/login", "src/auth/signup", "src/auth/utils"]

BAD — too few features (everything lumped into one):
  "core-platform": ["src/auth", "src/billing", "src/webhooks", "src/templates", "src/api"]
  ← these are 5 distinct business capabilities, not one feature

GOOD — each capability is its own feature:
  "user-auth":  ["src/auth"]
  "billing":    ["src/billing"]
  "webhooks":   ["src/webhooks"]
  "templates":  ["src/templates"]
  "api":        ["src/api"]

BAD — putting individual filenames in `files`:
  "auth": ["LoginForm.tsx", "useAuth.ts"]  ← WRONG, these are filenames not directories

GOOD — putting directory paths exactly as listed:
  "auth": ["src/auth", "src/hooks/auth", "src/api/auth"]  ← correct

## Example

Input:
  src/auth → LoginForm.tsx, useSession.ts
    src/auth/utils → token.ts
  src/api/auth → login.ts, logout.ts
  src/payments → CheckoutForm.tsx, stripe.ts
    src/payments/hooks → useCheckout.ts
  src/api/payments → charge.ts, refund.ts

Result:
  "user-auth": ["src/auth", "src/auth/utils", "src/api/auth"]
  "checkout":  ["src/payments", "src/payments/hooks", "src/api/payments"]\
"""

_DIR_DETECTION_USER_PROMPT = """\
Group these directories into semantic business features.
{feature_hint}
<directories>
{file_tree}
</directories>{extra_context}
Return directory paths exactly as listed above in the `files` field (not individual filenames). \
Every directory in exactly one feature. Use business domain names.\
"""


class _FeatureFileMapping(BaseModel):
    feature_name: str
    files: list[str]


class _FeatureDetectionResponse(BaseModel):
    features: list[_FeatureFileMapping]


class _FeatureEnrichment(BaseModel):
    original_name: str
    description: str


class _EnrichmentResponse(BaseModel):
    features: list[_FeatureEnrichment]


def detect_features_llm(
    files: list[str],
    api_key: str | None = None,
    commits: list[Commit] | None = None,
    path_prefix: str = "",
    signatures: dict[str, FileSignature] | None = None,
) -> dict[str, list[str]]:
    """
    Sends the repository file tree to Claude and returns a semantic feature mapping.
    Returns {} on any error (caller falls back to heuristic detection).

    When commits are provided, enriches the prompt with:
    - Co-change pairs (files that frequently change together)
    - Commit message keywords per directory

    For large repos (>_DIR_COLLAPSE_THRESHOLD files), collapses to unique directories
    before sending to the LLM — saves tokens and improves accuracy. The returned
    feature→files mapping is then expanded back to full file paths.

    Args:
        files: List of file paths (relative, with path_prefix already stripped).
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        commits: Optional commit history for co-change and keyword enrichment.
        path_prefix: Prefix stripped from files (e.g. "src/"). Used to normalize
            commit paths so they match the stripped file paths.

    Returns:
        dict mapping feature names to lists of file paths.
        Empty dict if LLM call fails.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key or not files:
        return {}

    client = anthropic.Anthropic(api_key=key)

    # Normalize commit file paths to match analysis_files (which have path_prefix stripped)
    norm_commits = _normalize_commit_files(commits, path_prefix) if commits and path_prefix else commits
    cochange_pairs = _compute_cochange(norm_commits) if norm_commits else []

    if len(files) > _DIR_COLLAPSE_THRESHOLD:
        dirs = _unique_dirs(files)
        samples = _dir_to_sample_files(dirs, files)
        dir_keywords = _extract_dir_keywords(dirs, files, norm_commits) if norm_commits else {}
        file_tree = _format_dir_tree(dirs, samples)
        route_anchors = _format_route_anchors(signatures, dirs=dirs) if signatures else ""
        extra_context = _format_extra_context(cochange_pairs, dir_keywords) + route_anchors
        response = _call_dir_detection(client, file_tree, n_dirs=len(dirs), extra_context=extra_context)
        if not response:
            return {}
        result = _expand_dir_mapping(response, files)
    else:
        file_tree = "\n".join(files[:_MAX_FILES_FOR_DETECTION])
        route_anchors = _format_route_anchors(signatures) if signatures else ""
        extra_context = _format_extra_context(cochange_pairs, {}) + route_anchors
        response = _call_feature_detection(client, file_tree, extra_context, n_files=len(files))
        if not response:
            return {}
        result = _build_feature_dict(response, set(files))

    # Post-process: re-split oversized features, then redistribute infrastructure noise
    result = _resplit_oversized_features(client, result)
    return _redistribute_infra_features(result)


_RESPLIT_FILE_THRESHOLD = 80
_RESPLIT_CONCENTRATION_PCT = 0.70  # re-split if >70% of files in one dir

# Patterns that indicate infrastructure-only features (not business domains)
_INFRA_FEATURE_PATTERNS = {
    "database-migrations", "migrations", "test-fixtures", "fixtures",
    "management-commands", "commands", "config-files", "infrastructure",
}


def _redistribute_infra_features(
    result: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Redistribute files from infrastructure-only features to their nearest business feature."""
    infra_names = [
        name for name in result
        if name in _INFRA_FEATURE_PATTERNS
        or any(name.endswith(f"-{p}") or name.startswith(f"{p}-") for p in ("migrations", "fixtures", "commands"))
    ]
    if not infra_names:
        return result

    business = {n: fs for n, fs in result.items() if n not in infra_names}
    if not business:
        return result

    for infra_name in infra_names:
        infra_files = result[infra_name]
        for f in infra_files:
            target = _find_best_merge_target([f], business)
            if target:
                business[target].append(f)
            else:
                business.setdefault("shared-utilities", []).append(f)
        logger.info("Redistributed %d files from '%s' to business features", len(infra_files), infra_name)

    return business


def _resplit_oversized_features(
    client: anthropic.Anthropic,
    result: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Re-splits oversized or directory-concentrated features.

    Triggers when:
    - A feature has >_RESPLIT_FILE_THRESHOLD files (too many files), OR
    - >70% of a feature's files are in a single directory AND the feature
      has >= 20 files (indicates dir-collapse couldn't split properly).
    """
    resplit_needed = {}
    for name, feat_files in result.items():
        if len(feat_files) > _RESPLIT_FILE_THRESHOLD:
            resplit_needed[name] = feat_files
        elif len(feat_files) >= 20:
            dir_counts: dict[str, int] = {}
            for f in feat_files:
                d = str(Path(f).parent)
                dir_counts[d] = dir_counts.get(d, 0) + 1
            max_dir_pct = max(dir_counts.values()) / len(feat_files) if dir_counts else 0
            if max_dir_pct >= _RESPLIT_CONCENTRATION_PCT:
                resplit_needed[name] = feat_files
    if not resplit_needed:
        return result

    for feat_name, feat_files in resplit_needed.items():
        logger.info("Re-splitting oversized feature '%s' (%d files) with Sonnet", feat_name, len(feat_files))
        sub_tree = "\n".join(feat_files)
        min_sub = max(3, len(feat_files) // 30)
        max_sub = max(5, len(feat_files) // 15)
        resplit_prompt = (
            f"These {len(feat_files)} files were all grouped into one feature '{feat_name}'. "
            f"This is too coarse. Split them into {min_sub}–{max_sub} distinct sub-features "
            "based on what each file DOES, not which directory it's in.\n\n"
            "KEY PATTERNS to look for:\n"
            "- In Django/Rails/Flask apps, filenames reveal business domains: "
            "tags.py, permissions.py, bulk_edit.py, workflows.py, custom_fields.py each "
            "represent a SEPARATE business capability, even if they're in the same directory.\n"
            "- Files named models.py, views.py, serializers.py, admin.py are shared across "
            "ALL features in that app — assign them to the LARGEST or most core sub-feature.\n"
            "- Match test files (test_tags.py) with their source (tags.py).\n"
            "- Look for business entity names in filenames: 'correspondent', 'document_type', "
            "'saved_view', 'storage_path', 'custom_field', 'note', 'share' etc.\n"
            "- Do NOT split a feature that is already cohesive (e.g. 'email-ingestion' with "
            "mail fetching, parsing, rules all serving the same business domain). Only split "
            "when filenames clearly indicate DIFFERENT user-facing capabilities.\n\n"
            f"<file_list>\n{sub_tree}\n</file_list>\n"
            "Return a JSON mapping of sub-feature names to file lists. "
            "Each file in exactly one feature. Use business domain names."
        )
        try:
            # Use Haiku with messages.parse (Sonnet 4 doesn't support output_format)
            resp = client.messages.parse(
                model=_MODEL,
                max_tokens=_MAX_TOKENS_FILE,
                temperature=0,
                system=_DETECTION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": resplit_prompt}],
                output_format=_FeatureDetectionResponse,
            )
            if resp.parsed_output:
                sub_features = _build_feature_dict(resp.parsed_output, set(feat_files))
                if len(sub_features) > 1:
                    del result[feat_name]
                    result.update(sub_features)
                    logger.info("Re-split '%s' → %d sub-features", feat_name, len(sub_features))
        except Exception as e:
            logger.warning("Re-split failed for '%s': %s", feat_name, e)

    return result



def _call_feature_detection(
    client: anthropic.Anthropic,
    file_tree: str,
    extra_context: str = "",
    n_files: int = 0,
) -> _FeatureDetectionResponse | None:
    """Calls Claude API for feature detection (file-path mode). Returns None on any failure."""
    hint = _file_feature_count_hint(n_files) if n_files else ""
    prompt = _DETECTION_USER_PROMPT.format(
        file_tree=file_tree, extra_context=extra_context, feature_hint=hint,
    )

    # Inject minimum feature count into system prompt for large repos
    system = _DETECTION_SYSTEM_PROMPT
    if n_files >= 100:
        min_f = min(max(8, n_files // 30), 15)
        system += (
            f"\n\n## CRITICAL REQUIREMENT\n"
            f"This codebase has {n_files} files. You MUST return at least {min_f} features. "
            f"Producing fewer than {min_f} means you are over-merging distinct business capabilities. "
            f"Each of these should be separate: auth, billing, webhooks, templates, integrations, "
            f"api, settings, notifications, admin, teams, etc."
        )

    for attempt in range(_MAX_RETRIES):
        try:
            response = client.messages.parse(
                model=_MODEL,
                max_tokens=_MAX_TOKENS_FILE,
                temperature=0,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=_FeatureDetectionResponse,
            )
            return response.parsed_output
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("LLM call failed (attempt %d/%d): %s. Retrying in %.1fs...", attempt + 1, _MAX_RETRIES, e, delay)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(delay)
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
            ValidationError,
        ):
            return None
        except anthropic.APIStatusError:
            return None
    return None


def _call_dir_detection(
    client: anthropic.Anthropic,
    file_tree: str,
    n_dirs: int,
    extra_context: str = "",
) -> _FeatureDetectionResponse | None:
    """
    Calls Claude API for dir-collapse feature detection.
    Uses dir-specific prompts and a larger token budget to accommodate
    responses that list hundreds of directory paths.
    Returns None on any failure.
    """
    prompt = _DIR_DETECTION_USER_PROMPT.format(
        file_tree=file_tree,
        feature_hint=_feature_count_hint(n_dirs),
        extra_context=extra_context,
    )

    # Inject minimum feature count into system prompt
    system = _DIR_DETECTION_SYSTEM_PROMPT
    min_f = min(max(8, n_dirs // 15), 15)
    system += (
        f"\n\n## CRITICAL REQUIREMENT\n"
        f"This codebase has {n_dirs} directories. You MUST return at least {min_f} features. "
        f"Producing fewer than {min_f} means you are over-merging distinct business capabilities. "
        f"Each of these should be separate: auth, billing, webhooks, templates, integrations, "
        f"api, settings, notifications, admin, teams, etc."
    )

    for attempt in range(_MAX_RETRIES):
        try:
            response = client.messages.parse(
                model=_MODEL,
                max_tokens=_MAX_TOKENS_DIR,
                temperature=0,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=_FeatureDetectionResponse,
            )
            return response.parsed_output
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("LLM dir-detection failed (attempt %d/%d): %s. Retrying in %.1fs...", attempt + 1, _MAX_RETRIES, e, delay)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(delay)
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
            ValidationError,
        ):
            return None
        except anthropic.APIStatusError:
            return None
    return None


def _build_feature_dict(
    response: _FeatureDetectionResponse,
    allowed_files: set[str],
) -> dict[str, list[str]]:
    """Converts the LLM response into a dict, filtering out unknown file paths."""
    result: dict[str, list[str]] = {}
    for mapping in response.features:
        valid_files = [f for f in mapping.files if f in allowed_files]
        if valid_files:
            result[mapping.feature_name] = valid_files
    return result


def _unique_dirs(files: list[str]) -> list[str]:
    """
    Extracts unique directory paths from a file list, sorted.
    Skips the root (files with no parent directory).
    """
    dirs: set[str] = set()
    for f in files:
        parent = str(Path(f).parent)
        if parent != ".":
            dirs.add(parent)
    return sorted(dirs)


def _dir_to_sample_files(dirs: list[str], all_files: list[str]) -> dict[str, list[str]]:
    """For each directory, returns sample file names.

    Large directories (>20 files) get up to 20 samples to give the LLM enough
    context to detect multiple business domains within a single directory
    (common in Django/Rails monoliths).
    """
    # Count files per directory first
    dir_file_count: dict[str, int] = {d: 0 for d in dirs}
    for f in all_files:
        parent = str(Path(f).parent)
        if parent in dir_file_count:
            dir_file_count[parent] += 1

    samples: dict[str, list[str]] = {d: [] for d in dirs}
    for f in all_files:
        parent = str(Path(f).parent)
        if parent not in samples:
            continue
        limit = 20 if dir_file_count[parent] > 20 else _DIR_SAMPLE_FILES
        if len(samples[parent]) < limit:
            samples[parent].append(Path(f).name)
    return samples


def _format_dir_tree(dirs: list[str], samples: dict[str, list[str]]) -> str:
    """
    Formats directories with sample file names and hierarchical indentation.
    Child directories (whose parent also appears in the list) are indented to
    visually communicate nesting depth to the LLM.
    """
    dir_set = set(dirs)
    lines = []
    for d in dirs:
        depth = _dir_nesting_depth(d, dir_set)
        indent = "  " * (depth + 1)
        s = samples.get(d, [])
        suffix = f" → {', '.join(s)}" if s else ""
        lines.append(f"{indent}{d}{suffix}")
    return "\n".join(lines)


def _dir_nesting_depth(d: str, dir_set: set[str]) -> int:
    """
    Returns how many ancestor directories of `d` also appear in `dir_set`.
    Used to compute indentation depth for the dir tree.
    """
    depth = 0
    current = d
    while True:
        parent = str(Path(current).parent)
        if parent == "." or parent == current:
            break
        if parent in dir_set:
            depth += 1
        current = parent
    return depth


def _feature_count_hint(n_dirs: int) -> str:
    """Generates a feature-count guidance line for the dir-collapse prompt."""
    min_f = min(max(8, n_dirs // 15), 15)
    max_f = min(max(15, n_dirs // 7), 30)
    return (
        f"\nYou have {n_dirs} directories. "
        f"You MUST produce at least {min_f} features (aim for {min_f}–{max_f}). "
        "Each distinct business capability (auth, billing, webhooks, templates, "
        "integrations, etc.) MUST be its own feature. "
        "Do NOT merge unrelated capabilities into 'core-platform' or 'app-features'.\n"
    )


def _file_feature_count_hint(n_files: int) -> str:
    """Generates a feature-count guidance line for the file-path mode prompt."""
    if n_files < 30:
        return ""
    min_f = min(max(5, n_files // 30), 15)
    max_f = min(max(10, n_files // 12), 30)
    return (
        f"\nYou have {n_files} files. "
        f"You MUST produce at least {min_f} features (aim for {min_f}–{max_f}).\n"
        "IMPORTANT: Do NOT create one giant feature for an entire directory. "
        "Each distinct business capability (auth, billing, webhooks, templates, "
        "integrations, search, etc.) MUST be its own feature. "
        "Do NOT merge unrelated capabilities into 'core-platform' or 'app-features'.\n"
    )


def _extract_dir_keywords(
    dirs: list[str],
    all_files: list[str],
    commits: list[Commit],
) -> dict[str, list[str]]:
    """Extracts top commit message keywords per directory from git history."""
    from collections import Counter

    dir_set = set(dirs)
    dir_counters: dict[str, Counter] = {d: Counter() for d in dirs}

    file_to_dir: dict[str, str] = {}
    for f in all_files:
        parent = str(Path(f).parent)
        if parent in dir_set:
            file_to_dir[f] = parent

    word_pattern = re.compile(r"[a-z]{3,}")
    for commit in commits:
        words = {
            w for w in word_pattern.findall(commit.message.lower())
            if w not in _COMMIT_STOP_WORDS
        }
        dirs_touched: set[str] = set()
        for f in commit.files_changed:
            if f in file_to_dir:
                dirs_touched.add(file_to_dir[f])
        for d in dirs_touched:
            dir_counters[d].update(words)

    return {
        d: [w for w, _ in counter.most_common(4)]
        for d, counter in dir_counters.items()
        if counter
    }


def _normalize_commit_files(commits: list[Commit], path_prefix: str) -> list[Commit]:
    """
    Returns commits with path_prefix stripped from each file path.
    Needed when --src is used: commits retain full paths (src/auth/...)
    but analysis_files have the prefix stripped (auth/...).
    """
    result = []
    for c in commits:
        stripped = [
            f[len(path_prefix):] if f.startswith(path_prefix) else f
            for f in c.files_changed
        ]
        result.append(c.model_copy(update={"files_changed": stripped}))
    return result


def _compute_cochange(commits: list[Commit]) -> list[tuple[str, str, float]]:
    """Delegates co-change computation to the features module."""
    from faultline.analyzer.features import compute_cochange
    return compute_cochange(commits)


def _format_extra_context(
    cochange_pairs: list[tuple[str, str, float]],
    dir_keywords: dict[str, list[str]],
) -> str:
    """Builds an extra context block to append to the LLM feature detection prompt."""
    parts: list[str] = []

    if cochange_pairs:
        lines = [
            f"  {f1} ↔ {f2} ({int(s * 100)}%)"
            for f1, f2, s in cochange_pairs[:_MAX_COCHANGE_IN_PROMPT]
        ]
        parts.append(
            "<co-changes>\n"
            "Files changed together frequently — strong signal they belong to the same feature:\n"
            + "\n".join(lines)
            + "\n</co-changes>"
        )

    kw_lines = [
        f"  {d} → {', '.join(sorted(kws))}"
        for d, kws in sorted(dir_keywords.items())
        if kws
    ]
    if kw_lines:
        parts.append(
            "<commit-topics>\n"
            "Top commit message topics per directory:\n"
            + "\n".join(kw_lines)
            + "\n</commit-topics>"
        )

    return ("\n\n" + "\n\n".join(parts) + "\n") if parts else ""


def _format_route_anchors(
    signatures: dict[str, FileSignature],
    dirs: list[str] | None = None,
) -> str:
    """
    Builds a <route-anchors> section for the LLM prompt.

    File mode (dirs=None): one line per file that has routes.
    Dir mode (dirs provided): one line per directory, routes aggregated from direct children.

    Returns empty string if no routes found in signatures.
    """
    if not signatures:
        return ""

    if dirs is None:
        lines = []
        for path, sig in sorted(signatures.items()):
            if not sig.routes:
                continue
            routes_str = ", ".join(sorted(sig.routes)[:_MAX_ROUTES_PER_ENTRY])
            lines.append(f"  {path} → {routes_str}")
            if len(lines) >= _MAX_ROUTE_ANCHOR_FILES:
                break

        if not lines:
            return ""

        return (
            "\n\n<route-anchors>\n"
            "Files with API routes — use as starting anchors for feature grouping:\n"
            + "\n".join(lines)
            + "\n</route-anchors>"
        )
    else:
        dirs_set = set(dirs)
        dir_routes: dict[str, list[str]] = {}
        for path, sig in sorted(signatures.items()):
            if not sig.routes:
                continue
            parent = str(Path(path).parent)
            if parent in dirs_set:
                dir_routes.setdefault(parent, []).extend(sig.routes)

        if not dir_routes:
            return ""

        lines = []
        for d in dirs:
            if d not in dir_routes:
                continue
            routes_str = ", ".join(dir_routes[d][:_MAX_ROUTES_PER_ENTRY])
            lines.append(f"  {d} → {routes_str}")
            if len(lines) >= _MAX_ROUTE_ANCHOR_FILES:
                break

        if not lines:
            return ""

        return (
            "\n\n<route-anchors>\n"
            "Directories with API routes — strong feature boundary anchors:\n"
            + "\n".join(lines)
            + "\n</route-anchors>"
        )


# ── Entity anchors ──────────────────────────────────────────────────────────

_MAX_ENTITY_ANCHOR_FILES = 30
_MAX_ENTITIES_PER_FILE = 12

# File patterns that typically define business entities/models
_ENTITY_FILE_PATTERNS = {
    "models.py", "model.py", "schemas.py", "schema.py", "types.py",
    "entities.py", "entity.py", "forms.py", "serializers.py",
    "admin.py", "views.py", "urls.py", "routes.py", "handlers.py",
}
# Directory name patterns that suggest entity-defining files
_ENTITY_DIR_PATTERNS = {"models", "schemas", "entities", "types"}


def _format_entity_anchors(
    signatures: dict[str, FileSignature],
    dirs: list[str] | None = None,
) -> str:
    """Build an <entity-anchors> section listing class/export names from key files.

    This helps the LLM detect features that exist as classes inside shared files
    (e.g. Django models.py with Tag, Correspondent, SavedView classes).

    File mode (dirs=None): shows exports from model/schema/entity files.
    Dir mode (dirs provided): aggregates exports by directory.
    """
    if not signatures:
        return ""

    # Filter to Python entity-defining files only.
    # TS/JS models/types/schemas are technical layers, not business entities —
    # including them causes over-merging in non-Python repos.
    entity_sigs: list[FileSignature] = []
    for path, sig in signatures.items():
        if not sig.exports:
            continue
        if not path.endswith(".py"):
            continue
        filename = Path(path).name.lower()
        parent_name = Path(path).parent.name.lower()
        if filename in _ENTITY_FILE_PATTERNS or parent_name in _ENTITY_DIR_PATTERNS:
            entity_sigs.append(sig)

    if not entity_sigs:
        return ""

    if dirs is None:
        # File mode: one line per entity-defining file
        lines: list[str] = []
        for sig in sorted(entity_sigs, key=lambda s: s.path)[
            :_MAX_ENTITY_ANCHOR_FILES
        ]:
            exports_str = ", ".join(sig.exports[:_MAX_ENTITIES_PER_FILE])
            more = (
                f" (+{len(sig.exports) - _MAX_ENTITIES_PER_FILE} more)"
                if len(sig.exports) > _MAX_ENTITIES_PER_FILE
                else ""
            )
            lines.append(f"  {sig.path} → {exports_str}{more}")

        if not lines:
            return ""

        return (
            "\n\n<entity-anchors>\n"
            "Business entity definitions found in model/schema files — "
            "each entity name often maps to a distinct feature or sub-feature:\n"
            + "\n".join(lines)
            + "\n</entity-anchors>"
        )

    # Dir mode: aggregate exports by directory
    dir_set = set(dirs)
    dir_entities: dict[str, list[str]] = {}
    for sig in entity_sigs:
        parent = str(Path(sig.path).parent)
        if parent in dir_set:
            dir_entities.setdefault(parent, []).extend(
                sig.exports[:_MAX_ENTITIES_PER_FILE]
            )

    if not dir_entities:
        return ""

    lines = []
    for d in dirs:
        if d not in dir_entities:
            continue
        # Deduplicate and limit
        entities = list(dict.fromkeys(dir_entities[d]))[
            :_MAX_ENTITIES_PER_FILE
        ]
        entities_str = ", ".join(entities)
        lines.append(f"  {d} → {entities_str}")
        if len(lines) >= _MAX_ENTITY_ANCHOR_FILES:
            break

    if not lines:
        return ""

    return (
        "\n\n<entity-anchors>\n"
        "Business entity definitions (models/schemas) found in these directories — "
        "entity names reveal distinct business domains within a single directory:\n"
        + "\n".join(lines)
        + "\n</entity-anchors>"
    )


def _expand_dir_mapping(
    response: _FeatureDetectionResponse,
    all_files: list[str],
) -> dict[str, list[str]]:
    """
    Expands a directory-level feature mapping to file-level.
    LLM returned directories → we assign all files under those dirs to the feature.
    """
    dir_to_files: dict[str, list[str]] = {}
    for f in all_files:
        parts = Path(f).parts
        for i in range(1, len(parts)):
            d = str(Path(*parts[:i]))
            dir_to_files.setdefault(d, []).append(f)

    result: dict[str, list[str]] = {}
    assigned: set[str] = set()

    for mapping in response.features:
        feature_files: list[str] = []
        for d in mapping.files:
            d_clean = d.rstrip("/").strip()  # normalize trailing slashes from LLM
            for f in dir_to_files.get(d_clean, []):
                if f not in assigned:
                    feature_files.append(f)
                    assigned.add(f)
        if feature_files:
            result[mapping.feature_name] = feature_files

    return result


def validate_ollama(
    model: str = _DEFAULT_OLLAMA_MODEL,
    host: str = _DEFAULT_OLLAMA_HOST,
) -> tuple[bool, str]:
    """
    Checks if Ollama is reachable and the requested model is available.
    Returns (is_valid, error_message).
    """
    try:
        import ollama as _ollama
    except ImportError:
        return False, (
            "ollama package not installed. Run: pip install 'faultline[ollama]' "
            "or: pip install ollama"
        )

    try:
        client = _ollama.Client(host=host)
        available = [m.model for m in client.list().models]
        model_base = model.split(":")[0]
        if not any(m.startswith(model_base) for m in available):
            available_str = ", ".join(available) if available else "none pulled yet"
            return False, (
                f"Model '{model}' not found in Ollama. "
                f"Available: {available_str}. "
                f"Run: ollama pull {model}"
            )
        return True, ""
    except Exception:
        return False, (
            f"Cannot connect to Ollama at {host}. "
            "Make sure Ollama is running: ollama serve"
        )


def detect_features_ollama(
    files: list[str],
    model: str = _DEFAULT_OLLAMA_MODEL,
    host: str = _DEFAULT_OLLAMA_HOST,
    commits: list[Commit] | None = None,
    path_prefix: str = "",
    signatures: dict[str, FileSignature] | None = None,
) -> dict[str, list[str]]:
    """
    Sends the repository file tree to a local Ollama model and returns a semantic feature mapping.
    Returns {} on any error (caller falls back to heuristic detection).

    Args:
        files: List of file paths (relative, with path_prefix already stripped).
        model: Ollama model name (e.g. 'qwen2.5-coder:7b', 'llama3.2').
        host: Ollama server URL.
        commits: Optional commit history for co-change enrichment.
        path_prefix: Prefix stripped from files (e.g. "src/"). Used to normalize
            commit paths so they match the stripped file paths.

    Returns:
        dict mapping feature names to lists of file paths.
        Empty dict if the call fails.
    """
    if not files:
        return {}

    norm_commits = _normalize_commit_files(commits, path_prefix) if commits and path_prefix else commits
    cochange_pairs = _compute_cochange(norm_commits) if norm_commits else []

    if len(files) > _DIR_COLLAPSE_THRESHOLD:
        dirs = _unique_dirs(files)
        samples = _dir_to_sample_files(dirs, files)
        dir_keywords = _extract_dir_keywords(dirs, files, norm_commits) if norm_commits else {}
        file_tree = _format_dir_tree(dirs, samples)
        route_anchors = _format_route_anchors(signatures, dirs=dirs) if signatures else ""
        extra_context = _format_extra_context(cochange_pairs, dir_keywords) + route_anchors
        response = _call_dir_detection_ollama(file_tree, model, host, n_dirs=len(dirs), extra_context=extra_context)
        if not response:
            return {}
        return _expand_dir_mapping(response, files)
    else:
        file_tree = "\n".join(files[:_MAX_FILES_FOR_DETECTION])
        route_anchors = _format_route_anchors(signatures) if signatures else ""
        extra_context = _format_extra_context(cochange_pairs, {}) + route_anchors
        response = _call_feature_detection_ollama(file_tree, model, host, extra_context, n_files=len(files))
        if not response:
            return {}
        return _build_feature_dict(response, set(files))


def _call_feature_detection_ollama(
    file_tree: str,
    model: str,
    host: str,
    extra_context: str = "",
    n_files: int = 0,
) -> _FeatureDetectionResponse | None:
    """Calls Ollama API for feature detection (file-path mode). Returns None on any failure."""
    try:
        import ollama as _ollama
    except ImportError:
        return None

    hint = _file_feature_count_hint(n_files) if n_files else ""
    prompt = _DETECTION_USER_PROMPT.format(
        file_tree=file_tree, extra_context=extra_context, feature_hint=hint,
    )

    try:
        client = _ollama.Client(host=host)
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": _DETECTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            format=_FeatureDetectionResponse.model_json_schema(),
        )
        return _FeatureDetectionResponse.model_validate_json(response.message.content)
    except (ValidationError, Exception):
        return None


def _call_dir_detection_ollama(
    file_tree: str,
    model: str,
    host: str,
    n_dirs: int,
    extra_context: str = "",
) -> _FeatureDetectionResponse | None:
    """
    Calls Ollama API for dir-collapse feature detection.
    Uses dir-specific prompts so the model returns directory paths, not filenames.
    Returns None on any failure.
    """
    try:
        import ollama as _ollama
    except ImportError:
        return None

    prompt = _DIR_DETECTION_USER_PROMPT.format(
        file_tree=file_tree,
        feature_hint=_feature_count_hint(n_dirs),
        extra_context=extra_context,
    )

    try:
        client = _ollama.Client(host=host)
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": _DIR_DETECTION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            format=_FeatureDetectionResponse.model_json_schema(),
        )
        return _FeatureDetectionResponse.model_validate_json(response.message.content)
    except (ValidationError, Exception):
        return None


# ── Cluster merge + name (import graph → semantic features) ──────────────────
# Used when import-graph clusters need to be MERGED across business boundaries
# and given semantic names in a single LLM call.
# Unlike naming-only, LLM can merge N clusters → M features (M ≤ N).
# Results are cached: same cluster structure → same output on every run.

_MERGE_SYSTEM_PROMPT = """\
You are a software architect merging import-dependency clusters into business features.

## Context
Files have been pre-grouped into clusters by analyzing import statements.
Files inside a cluster directly import each other.
Your job is to identify which clusters serve the same business feature and merge them,
even when they have no direct import relationship (e.g. a Redux slice and the component
that uses it via a hook; an API service and the page that renders its data).

## Task
1. Merge clusters that serve the same user-facing capability.
2. Give each merged group a semantic business domain name.

## Rules
- Feature names: lowercase, hyphen-separated, 1–3 words.
- Use business domain terminology, not technical layers.
  Examples: "user-auth", "checkout", "analytics-dashboard", "notifications", "team-management".
- Merge clusters that serve the same business domain into one feature.
  BAD: merge all hooks clusters into "hooks-feature".
  GOOD: merge auth-hooks + auth-components + auth-api into "user-auth".
- Keep features separate when they represent distinct business capabilities \
  (e.g. "payments" vs "auth" vs "labels" vs "notifications").
- IMPORTANT: small clusters (2-5 files) that represent a clear business domain \
  must stay as their own feature. Do NOT absorb them into larger features. \
  Example: a "system-status" page with 3 files is a real feature, not part of "dashboard".
- When in doubt, keep features separate rather than merging them.
- NEVER create features for technical layers or utilities. Clusters containing only \
  shared utilities, hooks, helpers, icons, assets, UI components, stories (Storybook), \
  theme/locale files, or general-purpose code must be merged into the business feature \
  that uses them. If no clear business owner exists, merge them into the largest \
  feature that imports them.
  BAD: "hooks-utils", "shared-components", "icons-assets", "stories", "general-utils".
  GOOD: merge these into the business features they support.
- Every cluster must appear in exactly one feature — no omissions.
- cluster_indices contains 1-based indices from the list provided.\
"""

_MERGE_USER_PROMPT = """\
Below are code clusters formed from import dependency analysis.
Group related clusters into business features and name each feature.
{feature_hint}
{clusters}

For each feature: provide a feature_name and the list of cluster_indices (1-based) it contains.
Every cluster index must appear in exactly one feature.\
"""


def _merge_feature_count_hint(n_clusters: int) -> str:
    """Generates a scoped feature-count guidance line for the merge prompt.

    Scale: ~1 feature per 8 clusters, clamped to 8–60.
    Small repo (20 clusters) → 8–12 features.
    Medium repo (100 clusters) → 12–20 features.
    Large repo (300 clusters) → 25–50 features.
    """
    min_f = max(8, n_clusters // 12)
    max_f = max(12, n_clusters // 6)
    max_f = min(max_f, 60)
    return (
        f"\nYou have {n_clusters} clusters. "
        f"Aim for {min_f}–{max_f} business features. "
        f"Merge clusters that clearly serve the same domain, "
        f"but keep distinct business capabilities as separate features.\n"
    )


class _ClusterMergeItem(BaseModel):
    feature_name: str
    cluster_indices: list[int]


class _ClusterMergeResponse(BaseModel):
    features: list[_ClusterMergeItem]


def _merge_cache_key(cluster_mapping: dict[str, list[str]], model: str) -> str:
    """Stable cache key that includes both file membership and cluster structure."""
    # Sort clusters by their sorted file list for a stable canonical form
    clusters_repr = sorted([sorted(files) for files in cluster_mapping.values()])
    content = json.dumps(clusters_repr) + model
    return "merge_" + hashlib.sha256(content.encode()).hexdigest()[:24]


def _extract_cluster_keywords(
    cluster_mapping: dict[str, list[str]],
    commits: list[Commit],
) -> dict[str, list[str]]:
    """Extracts top commit message keywords per cluster from git history.

    For each cluster, collects commit messages of commits that touched any file
    in that cluster, then returns the top 4 non-stop words. These keywords give
    the LLM semantic hints about the business domain (e.g. "payment", "checkout",
    "billing") even when file paths alone are ambiguous.

    Bulk commits (>30 files) are excluded — they're refactors, not feature signals.
    """
    from collections import Counter

    _MAX_FILES_BULK = 30

    file_to_cluster: dict[str, str] = {
        f: cluster_id
        for cluster_id, files in cluster_mapping.items()
        for f in files
    }

    cluster_counters: dict[str, Counter] = {c: Counter() for c in cluster_mapping}
    word_pattern = re.compile(r"[a-z]{3,}")

    for commit in commits:
        if len(commit.files_changed) > _MAX_FILES_BULK:
            continue
        words = {
            w for w in word_pattern.findall(commit.message.lower())
            if w not in _COMMIT_STOP_WORDS
        }
        clusters_touched: set[str] = set()
        for f in commit.files_changed:
            if f in file_to_cluster:
                clusters_touched.add(file_to_cluster[f])
        for cluster_id in clusters_touched:
            cluster_counters[cluster_id].update(words)

    return {
        cluster_id: [w for w, _ in counter.most_common(4)]
        for cluster_id, counter in cluster_counters.items()
        if counter
    }


def _format_clusters_for_merge_prompt(
    cluster_mapping: dict[str, list[str]],
    keywords_per_cluster: dict[str, list[str]] | None = None,
) -> str:
    """Formats clusters as a numbered list for the LLM merge prompt.

    When keywords_per_cluster is provided, each cluster entry includes its
    top commit message topics as a hint for semantic business domain naming.
    """
    lines = []
    for i, (cluster_id, files) in enumerate(cluster_mapping.items(), start=1):
        sample = files[:8]
        file_lines = "\n".join(f"  {f}" for f in sample)
        suffix = f"\n  … ({len(files) - 8} more)" if len(files) > 8 else ""
        keywords = (keywords_per_cluster or {}).get(cluster_id, [])
        kw_line = f"\n  Commit topics: {', '.join(keywords)}" if keywords else ""
        lines.append(f"Cluster {i} ({cluster_id}):{kw_line}\n{file_lines}{suffix}")
    return "\n\n".join(lines)


def _apply_cluster_merge(
    cluster_mapping: dict[str, list[str]],
    merge_response: _ClusterMergeResponse,
) -> dict[str, list[str]]:
    """Builds the merged feature mapping from the LLM response.

    Handles duplicate names, out-of-range indices, and unassigned clusters
    (any cluster not referenced falls back to its original directory-derived name).
    """
    cluster_ids = list(cluster_mapping.keys())
    assigned: set[int] = set()
    result: dict[str, list[str]] = {}
    used_names: set[str] = set()

    for item in merge_response.features:
        merged_files: list[str] = []
        for idx in item.cluster_indices:
            if 1 <= idx <= len(cluster_ids) and idx not in assigned:
                cluster_id = cluster_ids[idx - 1]
                merged_files.extend(cluster_mapping[cluster_id])
                assigned.add(idx)

        if not merged_files:
            continue

        name = item.feature_name
        if name in used_names:
            suffix = 2
            while f"{name}-{suffix}" in used_names:
                suffix += 1
            name = f"{name}-{suffix}"
        used_names.add(name)
        result[name] = sorted(merged_files)

    # Unassigned clusters: merge into nearest feature by directory overlap.
    # Clusters with a distinct business directory stay standalone.
    for i, cluster_id in enumerate(cluster_ids, start=1):
        if i not in assigned:
            orphan_files = cluster_mapping[cluster_id]
            # Try to merge into an existing feature with directory overlap
            target = _find_best_merge_target(orphan_files, result)
            if target:
                result[target].extend(orphan_files)
            elif _cluster_has_distinct_dir(orphan_files):
                # Distinct business dir — keep as standalone
                name = cluster_id
                if name in used_names:
                    suffix = 2
                    while f"{name}-{suffix}" in used_names:
                        suffix += 1
                    name = f"{name}-{suffix}"
                used_names.add(name)
                result[name] = orphan_files
            else:
                # No overlap, no distinct dir — merge into largest feature
                largest = max(result, key=lambda k: len(result[k])) if result else None
                if largest:
                    result[largest].extend(orphan_files)
                else:
                    result[cluster_id] = orphan_files

    return result


def _find_best_merge_target(
    orphan_files: list[str],
    existing_features: dict[str, list[str]],
) -> str | None:
    """Finds the existing feature with the most directory overlap to absorb orphan files."""
    if not existing_features:
        return None

    orphan_dirs = {str(Path(f).parent) for f in orphan_files}

    best_name, best_overlap = None, 0
    for feat_name, feat_files in existing_features.items():
        feat_dirs = {str(Path(f).parent) for f in feat_files}
        overlap = len(orphan_dirs & feat_dirs)
        if overlap > best_overlap:
            best_overlap = overlap
            best_name = feat_name

    # Fallback: match by top-level directory
    if not best_name:
        orphan_tops = {Path(f).parts[0] if len(Path(f).parts) > 1 else "" for f in orphan_files}
        for feat_name, feat_files in existing_features.items():
            feat_tops = {Path(f).parts[0] if len(Path(f).parts) > 1 else "" for f in feat_files}
            if orphan_tops & feat_tops:
                if len(feat_files) > best_overlap:
                    best_overlap = len(feat_files)
                    best_name = feat_name

    return best_name


def _call_cluster_merge(
    client: anthropic.Anthropic,
    cluster_mapping: dict[str, list[str]],
    keywords_per_cluster: dict[str, list[str]] | None = None,
) -> _ClusterMergeResponse | None:
    """Sends all clusters to Claude for merge+name. Returns None on any failure."""
    prompt = _MERGE_USER_PROMPT.format(
        clusters=_format_clusters_for_merge_prompt(cluster_mapping, keywords_per_cluster),
        feature_hint=_merge_feature_count_hint(len(cluster_mapping)),
    )
    n_clusters = len(cluster_mapping)
    logger.info("Cluster merge: %d clusters, prompt length ~%d chars", n_clusters, len(prompt))
    for attempt in range(_MAX_RETRIES):
        try:
            max_tokens = 2048 if n_clusters < 50 else 4096 if n_clusters < 150 else 8192
            response = client.messages.parse(
                model=_MODEL,
                max_tokens=max_tokens,
                temperature=0,
                system=_MERGE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_format=_ClusterMergeResponse,
            )
            logger.info("Cluster merge success: %d features", len(response.parsed_output.features))
            return response.parsed_output
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("LLM cluster merge failed (attempt %d/%d): %s. Retrying in %.1fs...", attempt + 1, _MAX_RETRIES, e, delay)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(delay)
        except ValidationError as e:
            logger.warning("LLM cluster merge ValidationError (clusters=%d): %s", n_clusters, e)
            return None
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
        ) as e:
            logger.warning("LLM cluster merge auth error: %s", e)
            return None
        except anthropic.APIStatusError as e:
            logger.warning("LLM cluster merge APIStatusError: %s", e)
            return None
    return None


# ── Domain-keyword consolidation ─────────────────────────────────────────────
# Clusters whose files share a distinctive business keyword (e.g. "labels",
# "payments") are merged before LLM sees them.  This fixes the common case
# where one feature's files are split across many import-graph clusters because
# they don't import each other directly (hooks, views, services, schemas).

_DOMAIN_TECH_WORDS = {
    "components", "component", "hooks", "hook", "utils", "util", "helpers",
    "helper", "views", "view", "pages", "page", "features", "feature",
    "shared", "common", "lib", "libs", "src", "app", "modules", "module",
    "services", "service", "schemas", "schema", "types", "models", "model",
    "queries", "query", "mutations", "actions", "reducers", "slices",
    "middleware", "guards", "interceptors", "pipes", "decorators",
    "providers", "context", "contexts", "stores", "store", "state",
    "api", "rest", "graphql", "grpc",
    "ui", "assets", "icons", "images", "fonts", "styles", "theme", "themes",
    "stories", "storybook", "tests", "test", "spec", "specs", "mocks",
    "fixtures", "config", "configs", "constants", "enums",
    "index", "main", "root", "base", "core", "internal",
    "ndr", "hunterx", "easm", "edr",  # product-specific prefixes (too broad)
}

# Minimum fraction of a cluster's files that must contain a keyword
# for it to count as a "signature keyword" for that cluster.
_DOMAIN_KEYWORD_MIN_RATIO = 0.3
# Minimum number of clusters sharing a keyword to trigger consolidation.
_DOMAIN_KEYWORD_MIN_CLUSTERS = 2


def _extract_domain_keywords(files: list[str]) -> set[str]:
    """Extracts distinctive business-domain keywords from file paths.

    Looks at directory names and file stems, filtering out technical terms.
    Returns lowercase keywords that appear in at least 30% of the files.
    """
    from collections import Counter

    keyword_counts: Counter[str] = Counter()
    for f in files:
        seen: set[str] = set()
        parts = list(Path(f).parts)
        # Include directory names + file stem (without extension)
        stem = Path(f).stem.lower()
        for part in parts[:-1]:
            token = part.lower()
            if token not in seen and len(token) > 2:
                seen.add(token)
        # Also check camelCase/PascalCase splitting of stem
        import re
        tokens = re.findall(r"[a-z]+", re.sub(r"([A-Z])", r" \1", stem).lower())
        for t in tokens:
            if len(t) > 2 and t not in seen:
                seen.add(t)
        for s in seen:
            keyword_counts[s] += 1

    threshold = max(1, int(len(files) * _DOMAIN_KEYWORD_MIN_RATIO))
    return {
        kw for kw, count in keyword_counts.items()
        if count >= threshold and kw not in _DOMAIN_TECH_WORDS
    }


def _consolidate_domain_clusters(
    cluster_mapping: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Merges clusters that share distinctive business-domain keywords.

    Example: clusters containing files in `*/labels/*`, `*/Labels/*`,
    `*LabelManager*` all share the keyword "label" and get merged into one
    cluster before LLM merge sees them.
    """
    from collections import Counter, defaultdict as _defaultdict

    if len(cluster_mapping) <= 20:
        return cluster_mapping

    # Step 1: Extract signature keywords per cluster
    cluster_keywords: dict[str, set[str]] = {}
    keyword_to_clusters: dict[str, list[str]] = _defaultdict(list)

    for cname, files in cluster_mapping.items():
        kws = _extract_domain_keywords(files)
        cluster_keywords[cname] = kws
        for kw in kws:
            keyword_to_clusters[kw].append(cname)

    # Step 2: Find keywords shared by multiple clusters (domain signals)
    # Only use keywords that aren't too common (shared by <15% of clusters)
    # to avoid creating mega-clusters through transitive chains.
    max_cluster_share = max(3, len(cluster_mapping) // 7)
    merge_groups: dict[str, list[str]] = {}  # keyword → list of cluster names
    for kw, cnames in keyword_to_clusters.items():
        if _DOMAIN_KEYWORD_MIN_CLUSTERS <= len(cnames) <= max_cluster_share:
            merge_groups[kw] = cnames

    if not merge_groups:
        return cluster_mapping

    # Step 3: Direct merge — for each keyword, merge its clusters into one.
    # Unlike Union-Find, this doesn't create transitive chains across keywords.
    # Each keyword group is merged independently.
    already_merged: set[str] = set()
    merged_clusters: dict[str, list[str]] = {}

    # Sort keywords by specificity (fewer clusters = more specific = merge first)
    for kw in sorted(merge_groups, key=lambda k: len(merge_groups[k])):
        cnames = [c for c in merge_groups[kw] if c not in already_merged]
        if len(cnames) < 2:
            continue

        # Calculate total files — don't create mega-clusters
        total_files = sum(len(cluster_mapping[c]) for c in cnames)
        total_all_files = sum(len(v) for v in cluster_mapping.values())
        max_merged_size = max(80, total_all_files // 5)
        if total_files > max_merged_size:
            continue

        # Merge all into one cluster
        combined: list[str] = []
        for c in cnames:
            combined.extend(cluster_mapping[c])
            already_merged.add(c)
        merged_clusters[kw] = sorted(set(combined))

    # Step 4: Build result — merged clusters + untouched originals
    result: dict[str, list[str]] = {}
    used_names: set[str] = set()
    # Add merged clusters first (named by keyword)
    for kw_name, files in merged_clusters.items():
        name = kw_name
        if name in used_names:
            suffix = 2
            while f"{name}-{suffix}" in used_names:
                suffix += 1
            name = f"{name}-{suffix}"
        used_names.add(name)
        result[name] = files
    # Add untouched originals
    for cname, cfiles in cluster_mapping.items():
        if cname not in already_merged:
            name = cname
            if name in used_names:
                suffix = 2
                while f"{name}-{suffix}" in used_names:
                    suffix += 1
                name = f"{name}-{suffix}"
            used_names.add(name)
            result[name] = cfiles

    consolidated = len(cluster_mapping) - len(result)
    if consolidated > 0:
        logger.info(
            "Domain consolidation: %d → %d clusters (merged %d by shared keywords)",
            len(cluster_mapping), len(result), consolidated,
        )
    return result


_PRE_MERGE_MAX_FILES = 3  # clusters with this many files or fewer get pre-merged
_PRE_MERGE_THRESHOLD = 150  # only pre-merge when total clusters exceed this

# Directories that are technical layers, not business domains.
# Clusters rooted in these are safe to absorb.
_TECHNICAL_DIR_NAMES = {
    "utils", "util", "helpers", "helper", "lib", "libs", "common", "shared",
    "core", "base", "config", "configs", "constants", "types", "interfaces",
    "hooks", "hoc", "providers", "context", "contexts", "middleware",
    "middlewares", "decorators", "guards", "interceptors", "pipes",
    "styles", "assets", "icons", "images", "fonts", "theme", "themes",
    "__tests__", "__mocks__", "test", "tests", "spec", "specs",
    "fixtures", "storybook", "stories",
}


def _cluster_has_distinct_dir(files: list[str]) -> bool:
    """Returns True if the cluster's files live in a unique, non-technical directory.

    Such clusters likely represent a distinct business domain and should not
    be absorbed into larger clusters during pre-merge.
    """
    dirs = set()
    for f in files:
        parts = Path(f).parts
        if len(parts) >= 2:
            dirs.add(parts[0])
    if len(dirs) != 1:
        return False
    dir_name = next(iter(dirs)).lower().rstrip("s")
    return dir_name not in {d.lower().rstrip("s") for d in _TECHNICAL_DIR_NAMES}


def _pre_merge_tiny_clusters(
    cluster_mapping: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Merges tiny clusters (≤_PRE_MERGE_MAX_FILES files) into the nearest large cluster.

    This reduces the number of clusters sent to the LLM merge step, making the
    prompt manageable for repos with 300+ import graph clusters.

    Clusters in a unique business-domain directory are protected from absorption.
    """
    if len(cluster_mapping) <= _PRE_MERGE_THRESHOLD:
        return cluster_mapping

    large: dict[str, list[str]] = {}
    tiny: dict[str, list[str]] = {}
    protected: dict[str, list[str]] = {}
    for name, files in cluster_mapping.items():
        if len(files) <= _PRE_MERGE_MAX_FILES:
            if _cluster_has_distinct_dir(files):
                protected[name] = files
            else:
                tiny[name] = files
        else:
            large[name] = files

    if not large:
        return cluster_mapping

    result = {name: list(files) for name, files in large.items()}

    for _name, files in tiny.items():
        target = _find_best_merge_target(files, result)
        if target:
            result[target].extend(files)
        else:
            result[_name] = files

    # Add protected clusters back — they stay as-is for LLM to see
    for name, files in protected.items():
        result[name] = files

    absorbed = len(cluster_mapping) - len(result)
    logger.info("Pre-merge: %d → %d clusters (absorbed %d tiny, protected %d)",
                len(cluster_mapping), len(result), absorbed, len(protected))
    return result


def _dedup_chunk_names(merged: dict[str, list[str]]) -> dict[str, list[str]]:
    """Merge features that only differ by a chunk-collision suffix (e.g. auth-2 → auth).

    When chunked merging names the same feature in two chunks, the second gets a
    '-2' suffix.  This merges them back together.
    """
    import re as _re

    suffix_re = _re.compile(r"^(.+)-(\d+)$")
    result: dict[str, list[str]] = {}
    for name, files in merged.items():
        m = suffix_re.match(name)
        base = m.group(1) if m and m.group(1) in merged else name
        if base in result:
            result[base].extend(files)
        else:
            result[base] = list(files)
    return result


def _chunked_cluster_merge(
    client: anthropic.Anthropic,
    cluster_mapping: dict[str, list[str]],
    keywords_per_cluster: dict[str, list[str]] | None = None,
) -> dict[str, list[str]] | None:
    """Merges clusters in chunks to avoid prompt/response truncation.

    For small cluster sets (<=_MERGE_CHUNK_SIZE), does a single merge call.
    For larger sets, splits into chunks, merges each separately, then
    does a final merge pass to consolidate cross-chunk duplicates.

    Returns the final feature->files mapping, or None if merge failed.
    """
    if len(cluster_mapping) <= _MERGE_CHUNK_SIZE:
        response = _call_cluster_merge(
            client, cluster_mapping, keywords_per_cluster,
        )
        if response:
            return _apply_cluster_merge(cluster_mapping, response)
        return None

    cluster_items = list(cluster_mapping.items())
    chunks = [
        dict(cluster_items[i : i + _MERGE_CHUNK_SIZE])
        for i in range(0, len(cluster_items), _MERGE_CHUNK_SIZE)
    ]
    logger.info(
        "Chunked merge: %d clusters -> %d chunks of <=%d",
        len(cluster_mapping),
        len(chunks),
        _MERGE_CHUNK_SIZE,
    )

    all_merged: dict[str, list[str]] = {}
    for idx, chunk in enumerate(chunks):
        chunk_kw = (
            {k: v for k, v in keywords_per_cluster.items() if k in chunk}
            if keywords_per_cluster
            else None
        )
        response = _call_cluster_merge(client, chunk, chunk_kw or None)
        if response:
            chunk_result = _apply_cluster_merge(chunk, response)
            for name, files in chunk_result.items():
                unique_name = name
                suffix = 2
                while unique_name in all_merged:
                    unique_name = f"{name}-{suffix}"
                    suffix += 1
                all_merged[unique_name] = files
            logger.info(
                "Chunk %d/%d: %d clusters -> %d features",
                idx + 1,
                len(chunks),
                len(chunk),
                len(chunk_result),
            )
        else:
            for cid, files in chunk.items():
                all_merged[cid] = files
            logger.warning(
                "Chunk %d/%d failed, keeping %d raw clusters",
                idx + 1,
                len(chunks),
                len(chunk),
            )

    if not all_merged:
        return None

    # Final merge pass if intermediate result is still too fragmented
    if len(all_merged) > _MERGE_CHUNK_SIZE:
        logger.info(
            "Final merge pass: %d intermediate features",
            len(all_merged),
        )
        final_response = _call_cluster_merge(client, all_merged, None)
        if final_response:
            return _apply_cluster_merge(all_merged, final_response)
        logger.warning(
            "Final merge pass failed, returning %d intermediate features",
            len(all_merged),
        )

    # Deduplicate features with collision suffixes (e.g. "auth-2" → merge into "auth")
    return _dedup_chunk_names(all_merged)


# Single words: feature is technical if ALL its words are in this set.
_TECHNICAL_FEATURE_WORDS = {
    "utils", "util", "utilities", "helpers", "helper", "hooks", "hoc",
    "shared", "common", "general", "misc", "core", "base", "lib",
    "icons", "assets", "images", "fonts", "theme", "themes", "locale",
    "stories", "storybook", "mocks", "fixtures", "test", "tests",
    "styles", "css", "scss", "components", "ui",
    "workers", "worker",
}

# Multi-word patterns: feature names matching these exactly are technical.
_TECHNICAL_FEATURE_NAMES = {
    "ui-library", "ui-components", "shared-components", "custom-hooks",
    "hooks-utils", "general-utils", "data-schemas", "state-management",
    "api-services", "export-utilities", "app-shell", "routing",
    "icons-assets", "locale-theme", "custom-components", "custom-utils",
    "dashboard-utils", "chart-components", "input-component", "table-cells",
    "common-store", "template-components", "data-prefetch",
    "filter-processor", "entity-processors",
}


def _filter_technical_features(
    merged: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Absorbs technical/utility features into the business feature with the most overlap.

    Features named after technical layers (hooks-utils, shared-components, stories)
    are not useful to engineering managers. This redistributes their files.
    """
    technical: dict[str, list[str]] = {}
    business: dict[str, list[str]] = {}

    for name, files in merged.items():
        normalized = name.lower().replace("_", "-")
        parts = set(normalized.split("-"))
        is_technical = (
            normalized in _TECHNICAL_FEATURE_NAMES
            or (parts & _TECHNICAL_FEATURE_WORDS and not (parts - _TECHNICAL_FEATURE_WORDS - {""}))
        )
        if is_technical:
            technical[name] = files
        else:
            business[name] = files

    if not technical or not business:
        return merged

    for tech_name, tech_files in technical.items():
        target = _find_best_merge_target(tech_files, business)
        if target:
            business[target].extend(tech_files)
        else:
            # No overlap — pick the largest feature
            largest = max(business, key=lambda k: len(business[k]))
            business[largest].extend(tech_files)

    logger.info("Filtered %d technical features, redistributed files into business features",
                len(technical))
    return business


def merge_and_name_clusters_llm(
    cluster_mapping: dict[str, list[str]],
    api_key: str | None = None,
    commits: list[Commit] | None = None,
) -> dict[str, list[str]]:
    """Uses Claude to merge import-graph clusters into business features and name them.

    Unlike name_clusters_llm(), this can merge multiple clusters into one feature —
    essential when related files don't import each other directly (e.g. Redux slices,
    separate services, cross-cutting utilities).

    When commits are provided, extracts top commit message keywords per cluster
    and includes them in the prompt as semantic naming hints.

    Results are cached by cluster structure hash — same codebase → same result.
    Falls back to the original cluster_mapping on any error.

    Args:
        cluster_mapping: Output of build_import_clusters().
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        commits: Optional commit history for keyword extraction.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key or not cluster_mapping:
        return cluster_mapping

    # Consolidate clusters sharing business-domain keywords (e.g. "labels")
    working_clusters = _consolidate_domain_clusters(cluster_mapping)
    # Pre-merge tiny clusters to keep prompt size manageable for large repos
    working_clusters = _pre_merge_tiny_clusters(working_clusters)

    cache_key = _merge_cache_key(working_clusters, _MODEL)
    cached = _read_name_cache(cache_key)
    if cached is not None:
        if isinstance(next(iter(cached.values()), None), list):
            return cached  # type: ignore[return-value]

    keywords_per_cluster = _extract_cluster_keywords(working_clusters, commits) if commits else None

    client = anthropic.Anthropic(api_key=key)

    # Try single-shot merge first (preserves previous behavior).
    # Fall back to chunked merge only when single-shot fails (large repos).
    merge_response = _call_cluster_merge(
        client, working_clusters, keywords_per_cluster,
    )
    if merge_response:
        merged = _filter_technical_features(
            _apply_cluster_merge(working_clusters, merge_response),
        )
        _write_name_cache(cache_key, merged)  # type: ignore[arg-type]
        return merged

    # Single-shot failed (likely truncation) — try chunked merge
    if len(working_clusters) > _MERGE_CHUNK_SIZE:
        logger.info("Single-shot merge failed, trying chunked merge")
        merged = _chunked_cluster_merge(
            client, working_clusters, keywords_per_cluster,
        )
        if merged:
            merged = _filter_technical_features(merged)
            _write_name_cache(cache_key, merged)  # type: ignore[arg-type]
            return merged

    return working_clusters


def _call_cluster_merge_ollama(
    cluster_mapping: dict[str, list[str]],
    model: str,
    host: str,
    keywords_per_cluster: dict[str, list[str]] | None = None,
) -> _ClusterMergeResponse | None:
    """Calls Ollama for cluster merge+name. Returns None on any failure."""
    try:
        import ollama as _ollama
    except ImportError:
        return None

    prompt = _MERGE_USER_PROMPT.format(
        clusters=_format_clusters_for_merge_prompt(cluster_mapping, keywords_per_cluster),
        feature_hint=_merge_feature_count_hint(len(cluster_mapping)),
    )
    try:
        client = _ollama.Client(host=host)
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": _MERGE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            format=_ClusterMergeResponse.model_json_schema(),
        )
        return _ClusterMergeResponse.model_validate_json(response.message.content)
    except (ValidationError, Exception):
        return None


def merge_and_name_clusters_ollama(
    cluster_mapping: dict[str, list[str]],
    model: str = _DEFAULT_OLLAMA_MODEL,
    host: str = _DEFAULT_OLLAMA_HOST,
    commits: list[Commit] | None = None,
) -> dict[str, list[str]]:
    """Ollama version of merge_and_name_clusters_llm. See that function for full docs."""
    if not cluster_mapping:
        return cluster_mapping

    working_clusters = _consolidate_domain_clusters(cluster_mapping)
    working_clusters = _pre_merge_tiny_clusters(working_clusters)

    cache_key = _merge_cache_key(working_clusters, model)
    cached = _read_name_cache(cache_key)
    if cached is not None:
        if isinstance(next(iter(cached.values()), None), list):
            return cached  # type: ignore[return-value]

    keywords_per_cluster = _extract_cluster_keywords(working_clusters, commits) if commits else None

    merge_response = _call_cluster_merge_ollama(working_clusters, model, host, keywords_per_cluster)
    if merge_response:
        merged = _filter_technical_features(
            _apply_cluster_merge(working_clusters, merge_response),
        )
        _write_name_cache(cache_key, merged)  # type: ignore[arg-type]
        return merged

    return working_clusters


# ── Cluster naming (co-change grouping → semantic names) ─────────────────────
# Used when co-change detection produced the clusters and LLM only needs to name them.
# Results are cached: same file set → same names on every run.

_NAME_CACHE_DIR = Path.home() / ".faultline" / "llm-cache"
_NAME_CACHE_TTL_DAYS = 90

_NAMING_SYSTEM_PROMPT = """\
You are a software architect assigning business domain names to feature clusters.
Each cluster is a group of files that frequently change together in git history —
they belong to the same business feature even if spread across multiple directories.

Rules:
- Feature names must be lowercase, hyphen-separated, 1–3 words.
- Use business domain terminology, not technical layer names.
- Examples: "user-auth", "payment-processing", "dashboard", "notifications", "team-management".
- Every cluster must receive a unique name.
- Return exactly one name per cluster index — no skipping.\
"""

_NAMING_USER_PROMPT = """\
Name each feature cluster below. Each cluster contains files that change together in git.

{clusters}

Return a feature_name for each cluster by its index.\
"""


class _ClusterNamingItem(BaseModel):
    index: int
    feature_name: str


class _ClusterNamingResponse(BaseModel):
    features: list[_ClusterNamingItem]


def _cluster_cache_key(cluster_mapping: dict[str, list[str]], model: str) -> str:
    """Stable SHA256 cache key based on all files across all clusters."""
    all_files = sorted(f for files in cluster_mapping.values() for f in files)
    content = json.dumps(all_files) + model
    return hashlib.sha256(content.encode()).hexdigest()[:24]


def _read_name_cache(key: str) -> dict[str, str] | None:
    """Returns cached {cluster_id: feature_name} mapping or None if missing/expired."""
    path = _NAME_CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    age_days = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).days
    if age_days > _NAME_CACHE_TTL_DAYS:
        path.unlink()
        return None
    return json.loads(path.read_text())


def _write_name_cache(key: str, names: dict[str, str]) -> None:
    _NAME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_NAME_CACHE_DIR / f"{key}.json").write_text(json.dumps(names))


def _format_clusters_for_prompt(cluster_mapping: dict[str, list[str]]) -> str:
    """Formats cluster mapping as a numbered list for the naming prompt."""
    lines = []
    for i, (cluster_id, files) in enumerate(cluster_mapping.items(), start=1):
        sample = files[:8]
        file_lines = "\n".join(f"  {f}" for f in sample)
        suffix = f" … ({len(files) - 8} more)" if len(files) > 8 else ""
        lines.append(f"Cluster {i}:\n{file_lines}{suffix}")
    return "\n\n".join(lines)


def _apply_cluster_names(
    cluster_mapping: dict[str, list[str]],
    names: dict[str, str],
) -> dict[str, list[str]]:
    """Replaces cluster IDs with LLM-generated names, deduplicating collisions."""
    result: dict[str, list[str]] = {}
    used: set[str] = set()
    for cluster_id, files in cluster_mapping.items():
        name = names.get(cluster_id, cluster_id)
        if name in used:
            suffix = 2
            while f"{name}-{suffix}" in used:
                suffix += 1
            name = f"{name}-{suffix}"
        used.add(name)
        result[name] = files
    return result


def _call_cluster_naming(
    client: anthropic.Anthropic,
    cluster_mapping: dict[str, list[str]],
) -> dict[str, str] | None:
    """Sends all clusters to Claude in one call. Returns {cluster_id: name} or None."""
    cluster_ids = list(cluster_mapping.keys())
    prompt = _NAMING_USER_PROMPT.format(
        clusters=_format_clusters_for_prompt(cluster_mapping),
    )

    try:
        response = client.messages.parse(
            model=_MODEL,
            max_tokens=512,
            temperature=0,
            system=_NAMING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=_ClusterNamingResponse,
        )
        items = response.parsed_output.features
        # Map 1-based index back to cluster_id
        return {
            cluster_ids[item.index - 1]: item.feature_name
            for item in items
            if 1 <= item.index <= len(cluster_ids)
        }
    except (
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
        anthropic.RateLimitError,
        anthropic.APIStatusError,
        anthropic.APIConnectionError,
        ValidationError,
        IndexError,
    ):
        return None


def name_clusters_llm(
    cluster_mapping: dict[str, list[str]],
    api_key: str | None = None,
) -> dict[str, list[str]]:
    """Uses Claude to assign semantic names to co-change clusters.

    Results are cached by a hash of the file set — same repo state always
    returns the same names without making another API call.

    Falls back to the original cluster_mapping (directory-derived names)
    on any error or missing API key.

    Args:
        cluster_mapping: Output of detect_features_from_cochange().
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key or not cluster_mapping:
        return cluster_mapping

    cache_key = _cluster_cache_key(cluster_mapping, _MODEL)
    cached = _read_name_cache(cache_key)
    if cached is not None:
        return _apply_cluster_names(cluster_mapping, cached)

    client = anthropic.Anthropic(api_key=key)
    names = _call_cluster_naming(client, cluster_mapping)
    if names:
        _write_name_cache(cache_key, names)
        return _apply_cluster_names(cluster_mapping, names)

    return cluster_mapping


def _call_cluster_naming_ollama(
    cluster_mapping: dict[str, list[str]],
    model: str,
    host: str,
) -> dict[str, str] | None:
    """Calls Ollama to name clusters. Returns {cluster_id: name} or None."""
    try:
        import ollama as _ollama
    except ImportError:
        return None

    cluster_ids = list(cluster_mapping.keys())
    prompt = _NAMING_USER_PROMPT.format(
        clusters=_format_clusters_for_prompt(cluster_mapping),
    )

    try:
        client = _ollama.Client(host=host)
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": _NAMING_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            format=_ClusterNamingResponse.model_json_schema(),
        )
        parsed = _ClusterNamingResponse.model_validate_json(response.message.content)
        return {
            cluster_ids[item.index - 1]: item.feature_name
            for item in parsed.features
            if 1 <= item.index <= len(cluster_ids)
        }
    except (ValidationError, IndexError, Exception):
        return None


def name_clusters_ollama(
    cluster_mapping: dict[str, list[str]],
    model: str = _DEFAULT_OLLAMA_MODEL,
    host: str = _DEFAULT_OLLAMA_HOST,
) -> dict[str, list[str]]:
    """Ollama version of name_clusters_llm. See name_clusters_llm() for full docs."""
    if not cluster_mapping:
        return cluster_mapping

    cache_key = _cluster_cache_key(cluster_mapping, model)
    cached = _read_name_cache(cache_key)
    if cached is not None:
        return _apply_cluster_names(cluster_mapping, cached)

    names = _call_cluster_naming_ollama(cluster_mapping, model, host)
    if names:
        _write_name_cache(cache_key, names)
        return _apply_cluster_names(cluster_mapping, names)

    return cluster_mapping


def validate_api_key(api_key: str | None = None) -> tuple[bool, str]:
    """
    Validates the Anthropic API key before running the full analysis.
    Returns (is_valid, error_message).
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return False, "No API key provided. Use --api-key or set ANTHROPIC_API_KEY env var."

    if not key.startswith("sk-ant-"):
        return False, (
            f"Key format looks wrong (got: {key[:10]}...). "
            "Anthropic API keys start with 'sk-ant-'. "
            "Get yours at console.anthropic.com → API Keys."
        )

    client = anthropic.Anthropic(api_key=key)
    try:
        client.messages.create(
            model=_MODEL,
            max_tokens=10,
            temperature=0,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True, ""
    except anthropic.AuthenticationError as e:
        return False, (
            f"API key rejected by Anthropic ({e.status_code}). "
            "The key may be revoked or incorrect. "
            "Check console.anthropic.com → API Keys."
        )
    except anthropic.PermissionDeniedError:
        return False, (
            f"API key has no access to model '{_MODEL}'. "
            "Check your plan at console.anthropic.com."
        )
    except anthropic.APIConnectionError:
        return False, "Cannot reach Anthropic API. Check your internet connection."
    except anthropic.APIStatusError as e:
        if e.status_code == 400 and "credit balance" in str(e.message).lower():
            return False, (
                "Insufficient credits. Add funds at console.anthropic.com → Settings → Billing."
            )
        return False, f"Unexpected API error (HTTP {e.status_code}): {e.message}"


def enrich_features(
    features: list[Feature],
    api_key: str | None = None,
) -> list[Feature]:
    """
    Enriches features with LLM-generated descriptions.
    Returns original features unchanged if the API call fails or no key is provided.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key or not features:
        return features

    client = anthropic.Anthropic(api_key=key)
    enrichments = _fetch_enrichments(client, features)
    return _apply_enrichments(features, enrichments)


def _fetch_enrichments(
    client: anthropic.Anthropic,
    features: list[Feature],
) -> list[_FeatureEnrichment]:
    """Calls Claude API and returns enrichments. Returns empty list on any failure."""
    feature_data = [
        {
            "name": f.name,
            "sample_paths": f.paths[:_MAX_SAMPLE_PATHS],
        }
        for f in features[:_MAX_FEATURES_PER_CALL]
    ]

    try:
        response = client.messages.parse(
            model=_MODEL,
            max_tokens=1024,
            temperature=0,
            system=(
                "You are a software architecture analyst. "
                "Analyze code modules by their directory names and file paths, "
                "and return structured metadata about each one."
            ),
            messages=[{
                "role": "user",
                "content": (
                    "For each code module below, provide:\n"
                    "- original_name: exactly the same name as given (do not change it)\n"
                    "- description: one sentence describing what this module does\n\n"
                    f"Modules:\n{json.dumps(feature_data, indent=2)}"
                ),
            }],
            output_format=_EnrichmentResponse,
        )
        return response.parsed_output.features
    except (
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
        anthropic.RateLimitError,
        anthropic.APIStatusError,
        anthropic.APIConnectionError,
        ValidationError,
    ):
        return []


def _apply_enrichments(
    features: list[Feature],
    enrichments: list[_FeatureEnrichment],
) -> list[Feature]:
    """Merges LLM enrichment data into existing Feature objects."""
    by_name = {e.original_name: e for e in enrichments}
    return [
        feature.model_copy(update={"description": by_name[feature.name].description})
        if feature.name in by_name
        else feature
        for feature in features
    ]
