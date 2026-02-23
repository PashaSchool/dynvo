import json
import os
from pathlib import Path

import anthropic
from pydantic import BaseModel, ValidationError

from faultline.models.types import Feature

_MODEL = "claude-haiku-4-5-20251001"
_MAX_SAMPLE_PATHS = 5
_MAX_FEATURES_PER_CALL = 50
# Large repos: cap the file list sent to the LLM to control token usage and cost.
_MAX_FILES_FOR_DETECTION = 500

_DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b"
_DEFAULT_OLLAMA_HOST = "http://localhost:11434"
# When file count exceeds this, collapse to unique directories to save tokens
_DIR_COLLAPSE_THRESHOLD = 300

_DETECTION_SYSTEM_PROMPT = """\
You are a senior software architect analyzing a codebase's file tree to identify semantic business features.

## Task

Given a list of file paths from a git repository, group them into business features. A feature is a user-facing capability or business domain area, not a technical layer.

## Rules

1. Group files by the business domain they serve, not by technical role. Files from different directories (components, stores, API routes, tests) that serve the same business purpose belong to the same feature.
2. Use business domain terminology for feature names. Prefer "user-auth" over "authentication-module", "order-checkout" over "stripe-integration", "content-search" over "elasticsearch-wrapper".
3. Feature names must be lowercase, hyphen-separated, 1-3 words. Examples: "user-auth", "payment-processing", "dashboard", "notifications", "team-management".
4. Each file must appear in exactly one feature. No duplicates, no omissions.
5. Every feature must contain at least 2 files. If a file would be the sole member of a feature, merge it into the most closely related feature.
6. Test files belong to the same feature as the code they test. Match by naming convention (test_auth.py belongs with auth.py, UserService.test.ts belongs with UserService.ts).
7. Skip infrastructure and tooling files entirely. Do not include them in any feature. Skip: package.json, pyproject.toml, setup.py, setup.cfg, .gitignore, Makefile, *.lock, *.cfg, *.ini, *.env, *.toml, Dockerfile, docker-compose.yml, CI configs (.github/workflows/*, .circleci/*, Jenkinsfile), and similar build/config files.
8. Shared utility files (helpers, utils, constants, types, common middleware) that serve multiple features go into a feature named "shared-utilities".
9. For monorepo structures with apps/ or packages/ prefixes, consider each app or package as a potential scope, but still group by business feature within each scope. If the same business feature spans multiple packages, group them together.
10. For flat repositories where all files are in the root directory, group by filename patterns and domain keywords.\
"""

_DETECTION_USER_PROMPT = """\
Analyze these repository files and group them into semantic business features.

<file_list>
{file_tree}
</file_list>

Return the JSON mapping of features to files. Remember: skip infrastructure/config files, minimum 2 files per feature, each file in exactly one feature, use business domain names.\
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
) -> dict[str, list[str]]:
    """
    Sends the repository file tree to Claude and returns a semantic feature mapping.
    Returns {} on any error (caller falls back to heuristic detection).

    For large repos (>_DIR_COLLAPSE_THRESHOLD files), collapses to unique directories
    before sending to the LLM — saves tokens and improves accuracy. The returned
    feature→files mapping is then expanded back to full file paths.

    Args:
        files: List of file paths tracked in the repo (relative paths).
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.

    Returns:
        dict mapping feature names to lists of file paths.
        Empty dict if LLM call fails.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key or not files:
        return {}

    client = anthropic.Anthropic(api_key=key)

    if len(files) > _DIR_COLLAPSE_THRESHOLD:
        # Send unique directories instead of individual files — 10–50x fewer tokens
        dirs = _unique_dirs(files)
        response = _call_feature_detection(client, dirs)
        if not response:
            return {}
        return _expand_dir_mapping(response, files)
    else:
        response = _call_feature_detection(client, files[:_MAX_FILES_FOR_DETECTION])
        if not response:
            return {}
        return _build_feature_dict(response, set(files))


def _call_feature_detection(
    client: anthropic.Anthropic,
    files: list[str],
) -> _FeatureDetectionResponse | None:
    """Calls Claude API for feature detection. Returns None on any failure."""
    file_tree = "\n".join(files)
    prompt = _DETECTION_USER_PROMPT.format(file_tree=file_tree)

    try:
        response = client.messages.parse(
            model=_MODEL,
            max_tokens=8192,
            system=_DETECTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_format=_FeatureDetectionResponse,
        )
        return response.parsed_output
    except (
        anthropic.AuthenticationError,
        anthropic.PermissionDeniedError,
        anthropic.NotFoundError,
        anthropic.RateLimitError,
        anthropic.APIStatusError,
        anthropic.APIConnectionError,
        ValidationError,
    ):
        return None


def _build_feature_dict(
    response: _FeatureDetectionResponse,
    allowed_files: set[str],
) -> dict[str, list[str]]:
    """Converts the LLM response into a dict, filtering out unknown file paths."""
    result: dict[str, list[str]] = {}
    for mapping in response.features:
        # Safety check: only keep files that actually exist in the repo
        valid_files = [f for f in mapping.files if f in allowed_files]
        if valid_files:
            result[mapping.feature_name] = valid_files
    return result


def _unique_dirs(files: list[str]) -> list[str]:
    """
    Extracts unique directory paths from a file list, sorted.
    Skips the root (files with no parent directory).

    Example:
        ["EDR/Page.tsx", "EDR/hooks/useData.ts", "Firewall/Form.tsx"]
        → ["EDR", "EDR/hooks", "Firewall"]
    """
    dirs: set[str] = set()
    for f in files:
        parent = str(Path(f).parent)
        if parent != ".":
            dirs.add(parent)
    return sorted(dirs)


def _expand_dir_mapping(
    response: _FeatureDetectionResponse,
    all_files: list[str],
) -> dict[str, list[str]]:
    """
    Expands a directory-level feature mapping to file-level.
    LLM returned directories → we assign all files under those dirs to the feature.
    """
    # Build dir → files index
    dir_to_files: dict[str, list[str]] = {}
    for f in all_files:
        parts = Path(f).parts
        # Index every ancestor directory of this file
        for i in range(1, len(parts)):
            d = str(Path(*parts[:i]))
            dir_to_files.setdefault(d, []).append(f)

    result: dict[str, list[str]] = {}
    assigned: set[str] = set()

    for mapping in response.features:
        feature_files: list[str] = []
        for d in mapping.files:
            for f in dir_to_files.get(d, []):
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
) -> dict[str, list[str]]:
    """
    Sends the repository file tree to a local Ollama model and returns a semantic feature mapping.
    Returns {} on any error (caller falls back to heuristic detection).

    Args:
        files: List of file paths tracked in the repo (relative paths).
        model: Ollama model name (e.g. 'qwen2.5-coder:7b', 'llama3.2').
        host: Ollama server URL.

    Returns:
        dict mapping feature names to lists of file paths.
        Empty dict if the call fails.
    """
    if not files:
        return {}

    capped_files = files[:_MAX_FILES_FOR_DETECTION]
    response = _call_feature_detection_ollama(capped_files, model, host)
    if not response:
        return {}

    allowed_files = set(files)
    return _build_feature_dict(response, allowed_files)


def _call_feature_detection_ollama(
    files: list[str],
    model: str,
    host: str,
) -> _FeatureDetectionResponse | None:
    """Calls Ollama API for feature detection. Returns None on any failure."""
    try:
        import ollama as _ollama
    except ImportError:
        return None

    file_tree = "\n".join(files)
    prompt = _DETECTION_USER_PROMPT.format(file_tree=file_tree)

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


def validate_api_key(api_key: str | None = None) -> tuple[bool, str]:
    """
    Validates the Anthropic API key before running the full analysis.
    Returns (is_valid, error_message).
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return False, "No API key provided. Use --api-key or set ANTHROPIC_API_KEY env var."

    # Basic format check — Anthropic keys start with "sk-ant-"
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
