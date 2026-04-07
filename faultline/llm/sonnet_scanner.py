"""
Deep scan module using Claude Sonnet for high-quality initial feature detection.

This is an independent module that provides a one-shot analysis:
  candidates (from heuristics) + file tree → Sonnet → features with files AND flows

Designed for SaaS initial scan — runs once per repo, then incremental updates
use cheaper methods (heuristics + Haiku).

Output is a standard dict[str, SonnetFeature] that cli.py converts to FeatureMap.
"""

import json
import logging
import os
import time
from pathlib import Path

import anthropic
from pydantic import BaseModel, ValidationError

from faultline.analyzer.validation import (
    canonical_bucket_name,
    drop_phantom_features,
    filter_test_files,
    is_documentation_file,
    is_test_feature_name,
    is_test_file,
    partition_docs_vs_code,
)
from faultline.llm.cost import CostTracker

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-20250514"
_MAX_RETRIES = 2
_RETRY_BASE_DELAY = 2.0


# ── Response models ────────────────────────────────────────────────────────

class SonnetFlow(BaseModel):
    name: str
    description: str = ""
    files: list[str] = []


class SonnetFeature(BaseModel):
    name: str
    description: str = ""
    files: list[str] = []
    flows: list[SonnetFlow] = []


class RenameOp(BaseModel):
    from_name: str = ""
    to: str = ""


class SplitOp(BaseModel):
    from_name: str = ""
    into: list[str] = []


class SonnetOpsResponse(BaseModel):
    merge: list[list[str]] = []
    rename: list[RenameOp] = []
    remove: list[str] = []
    split: list[SplitOp] = []
    features: list[SonnetFeature] = []


# ── Prompts ────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a senior software architect. You will receive pre-grouped feature \
candidates from structural heuristics. Return operations to clean them up, \
plus user flows for each surviving feature.

## What to return

JSON with five keys:
- **merge**: candidate groups to combine (first = target name)
- **rename**: candidates that need a business-domain name
- **remove**: candidates that are pure UI scaffolding or infra (→ shared-infra)
- **split**: large candidates that should be broken into sub-features
- **features**: final feature list with description and 3-5 flows each

Do NOT return file lists. Files stay with their candidates automatically.

## Merge rules

You MUST aggressively merge. Apply these rules:

1. **Singular/plural duplicates** → merge. "issue" + "issues" → "issues".
2. **Sub-feature + parent** → merge into parent. "work-item-filters" → "issues". \
   "workspace-draft" → "workspace". "pdf-export" → merge into the feature it exports.
3. **UI scaffolding** → remove. These are NOT features: "dropdowns", "empty-state", \
   "base-layouts", "breadcrumbs", "toolbar", "navigation", "sidebar", "icons", \
   "shared-ui" (keep this name but as infrastructure). "helpers", "src" → remove.
4. **Technical layers** → remove. "editor" (if it's a rich-text editor used by many features), \
   "live" (if it's real-time infrastructure), "deploy", "proxy" → remove.

## Split rules — CRITICAL

Look at candidates with >100 files. These are often god-features that contain \
multiple distinct business domains. Based on the sample file paths shown, identify \
if the candidate should be split.

Common splits:
- "document" (200+ files) might contain: signing, templates, recipients, audit-logs, fields → split
- "api" (400+ files) might contain: auth, billing, webhooks, admin → split
- "app" (500+ files) might contain: dashboard, settings, onboarding, admin → split

Format: {"from": "document", "into": ["document-signing", "document-templates", "document-recipients"]}

Only split if you can clearly identify 2+ distinct business sub-domains from the file paths. \
Do NOT split just because a candidate is large.

## Target

After all operations: **12-25 business features**. Not 5, not 50.

## Feature rules

A feature = something a user interacts with. Ask: "Can a PM write a user story about this?"

## Flow rules — CRITICAL

EVERY feature MUST have flows. No exceptions. A flow = a user action sequence.
- Flow names: lowercase, end with "-flow"
- Each flow has a 1-sentence description
- Think about what a user DOES with this feature

**Flow count scales with feature size:**
- Small feature (<20 files): 3-5 flows
- Medium feature (20-100 files): 5-8 flows
- Large feature (100-300 files): 8-12 flows
- Very large feature (300+ files): 10-15 flows — use the subdirectory breakdown to identify flows

**Derive flows from exported functions and routes.** Some files show their exports and API routes:
- "exports: create_dashboard, delete_dashboard, duplicate_dashboard" → flows: create, delete, duplicate
- "routes: GET /counts, POST /{id}/duplicate" → flows: view-counts, duplicate
- "exports: CreateInvestigationRequest, list_investigations" → flows: create, browse

Each exported function that represents a user action = a flow. CRUD exports = 3-4 flows minimum.

For large features, each major subdirectory often represents a distinct user workflow. \
If you see subdirectories like "SecurityGroups/", "AutoSegmentation/", "Dashboard/", "Issues/" \
inside a feature — each of those is a flow (or multiple flows).

Example:
- "network-detection" (800 files with subdirs: SecurityGroups, AutoSegmentation, Dashboard, Issues, NetworkLog, Inventory, PortflowPage):
  → "view-detections-flow", "investigate-anomaly-flow", "manage-security-groups-flow", \
    "configure-auto-segmentation-flow", "view-network-dashboard-flow", "manage-issues-flow", \
    "analyze-network-log-flow", "monitor-port-flows-flow", "create-exclusion-rules-flow", \
    "benchmark-detection-flow"

If you return a feature without flows, the output is INVALID.

## JSON format

Return ONLY JSON, no text before or after:

{"merge":[],"rename":[],"remove":[],"split":[],"features":[{"name":"x","description":"...","flows":[{"name":"y-flow","description":"..."}]}]}\
"""

_USER_PROMPT = """\
<candidates>
{candidates_text}
</candidates>

<unmatched_directories>
{unmatched_text}
</unmatched_directories>

Return JSON. Rules: merge aggressively, split god-features (>100 files), 12-25 final features, EVERY feature MUST have 3-5 flows.\
"""


# ── Pre/post-processing helpers ────────────────────────────────────────────
#
# These are extracted from deep_scan() so they can be unit-tested without
# hitting the LLM. The real function below just orchestrates:
#     _clean_inputs → LLM call → apply ops → _finalize_result


def _clean_inputs(
    files: list[str],
    candidates: dict[str, list[str]],
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """Apply validation primitives upstream of the LLM call.

    Filters test files and documentation files out of both ``files`` and
    every candidate bucket, and drops any candidate whose name is a
    test-infrastructure alias (``vitest-mocks``, ``__tests__``, etc.).

    Returns a 3-tuple:
      - ``cleaned_files``: input minus docs and test files
      - ``cleaned_candidates``: candidates with docs/test paths removed;
        empty buckets and test-named buckets are dropped entirely
      - ``docs_files``: every file that matched ``is_documentation_file``,
        to be re-attached as a single ``documentation`` feature in
        ``_finalize_result``

    Rationale: the Day 1 baseline showed heuristic candidates leaking
    ``vitest-mocks`` (cal.com) and splitting ``docs_src/tutorial001_py310``
    into 21 features (fastapi). Running the LLM on that noise wastes
    tokens and produces phantom features. Doing the filtering here keeps
    the LLM's context focused on actual business code.
    """
    code_files, docs_files = partition_docs_vs_code(files)
    cleaned_files = filter_test_files(code_files)

    cleaned_candidates: dict[str, list[str]] = {}
    for name, paths in candidates.items():
        if is_test_feature_name(name):
            continue
        kept = [
            p for p in paths
            if not is_documentation_file(p) and not is_test_file(p)
        ]
        if kept:
            cleaned_candidates[name] = kept

    return cleaned_files, cleaned_candidates, docs_files


def _finalize_result(
    result: dict[str, list[str]],
    docs_files: list[str],
    is_library: bool,
) -> dict[str, list[str]]:
    """Apply post-processing cleanups to the LLM operation result.

    Steps (in order):
      1. Attach a synthetic ``documentation`` feature containing every
         file previously partitioned out by ``_clean_inputs``.
      2. Canonicalize bucket names (``root``/``init``/``main`` → ``shared-infra``),
         merging any duplicates that resolve to the same canonical name.
         Also rewrites feature names in the module-global
         ``_last_scan_result`` so ``get_deep_scan_flows`` can still match
         by name.
      3. Drop phantom features — empty buckets and test-infrastructure
         names that slipped through (belt-and-braces; ``_clean_inputs``
         should have caught these already).
      4. If ``is_library=True``, strip flows from every feature in
         ``_last_scan_result``. Libraries per acceptance criterion C
         produce feature maps but no user-journey flows; the operations
         prompt still runs because it handles naming/merging, just not
         flow output.

    Returns the cleaned feature map.
    """
    # 1. Attach documentation bucket
    if docs_files:
        result.setdefault("documentation", []).extend(docs_files)

    # 2. Canonicalize bucket names, merging duplicates
    canonicalized: dict[str, list[str]] = {}
    for name, paths in result.items():
        canonical = canonical_bucket_name(name)
        canonicalized.setdefault(canonical, []).extend(paths)

    # 2b. Canonicalize feature names in the side-channel so fuzzy flow
    # matching in get_deep_scan_flows() still resolves correctly.
    global _last_scan_result
    if _last_scan_result is not None:
        for feat in _last_scan_result.features:
            feat.name = canonical_bucket_name(feat.name)

    # 3. Drop phantom features (empty, test-named)
    cleaned = drop_phantom_features(canonicalized)

    # 4. Strip flows for libraries
    if is_library and _last_scan_result is not None:
        for feat in _last_scan_result.features:
            feat.flows = []

    # 5. Deterministic ordering (D11): sort by descending size then name.
    # Combined with temperature=0 on the LLM call, this makes two
    # consecutive runs on the same repo produce byte-identical JSON.
    cleaned = dict(
        sorted(cleaned.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    )
    # Also sort _last_scan_result features to match, so flow matching
    # and downstream rendering use the same order.
    if _last_scan_result is not None:
        order = {name: idx for idx, name in enumerate(cleaned.keys())}
        _last_scan_result.features.sort(
            key=lambda f: order.get(f.name, len(order))
        )

    return cleaned


# ── Main function ──────────────────────────────────────────────────────────

def deep_scan(
    files: list[str],
    candidates: dict[str, list[str]],
    api_key: str | None = None,
    signatures: dict | None = None,
    *,
    is_library: bool = False,
    model: str | None = None,
    tracker: CostTracker | None = None,
) -> dict[str, list[str]] | None:
    """
    Performs a deep scan using Sonnet to detect features and flows.

    Args:
        files: All file paths in the repo.
        candidates: Pre-computed candidates from detect_candidates().
        api_key: Anthropic API key.
        signatures: Optional AST-extracted signatures keyed by file path.
        is_library: When True, the result has flows stripped from every
            feature. Set this when ``repo_classifier.detect_library``
            reports the repo is a consumable library. Default False.
        model: Override the default Sonnet model id. When omitted,
            uses the module-level ``_MODEL`` constant. Passed through
            to the CostTracker so pricing is looked up accurately.
        tracker: Optional CostTracker. When provided, every successful
            LLM call is recorded with its token usage and cost, and
            ``tracker.check_budget()`` is invoked immediately after —
            which may raise ``BudgetExceeded`` to abort the scan before
            the caller fires further requests.

    Returns:
        Tuple of (feature_paths, flow_data) where:
        - feature_paths: dict[feature_name → list[file_path]]
        - flow_data: stored in _last_scan_result for cli.py to retrieve
        Returns None on failure.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        logger.error("No API key for deep scan")
        return None

    client = anthropic.Anthropic(api_key=key)
    resolved_model = model or _MODEL

    # Pre-process: strip test and docs files before they reach the LLM.
    # This replaces the earlier scattered filtering and makes the LLM's
    # token budget go toward real business code, not tutorial samples.
    files, candidates, docs_files = _clean_inputs(files, candidates)
    if docs_files:
        logger.info(
            "Deep scan: partitioned %d documentation files into synthetic bucket",
            len(docs_files),
        )

    # Separate real candidates from catch-all buckets
    _CATCHALL = {"backend", "frontend", "root", "init", "packages", "web", "api", "lib"}
    real_candidates: dict[str, list[str]] = {}
    unmatched: list[str] = []

    for name, paths in candidates.items():
        if name in _CATCHALL or len(paths) < 2:
            unmatched.extend(paths)
        else:
            real_candidates[name] = paths

    # Format candidates — show subdirectory breakdown for large ones, ALL files for small
    _MAX_FILES = 30  # Show more files so Sonnet can derive flows from filenames
    _LARGE_THRESHOLD = 50  # Show subdir breakdown instead of file list
    _SKIP_SUBDIR_NAMES = {
        "src", "app", "core", "lib", "views", "components", "hooks", "utils",
        "helpers", "types", "models", "schemas", "services", "store", "stores",
        "features", "shared", "ui", "common", "ndr", "web", "api",
    }
    cand_lines = []
    for name, paths in sorted(real_candidates.items(), key=lambda x: -len(x[1])):
        if len(paths) >= _LARGE_THRESHOLD:
            # Show subdirectory breakdown — reveals internal structure
            from collections import Counter as _Ctr
            subdirs = _Ctr()
            for fp in paths:
                parts = Path(fp).parts
                for part in parts[:-1]:
                    if part.lower() not in _SKIP_SUBDIR_NAMES and not part.startswith((".", "(")):
                        subdirs[part] += 1
                        break
            cand_lines.append(f"## {name} ({len(paths)} files) — subdirectories:")
            for subdir, cnt in subdirs.most_common(20):
                cand_lines.append(f"  {subdir}/: {cnt} files")
        else:
            cand_lines.append(f"## {name} ({len(paths)} files)")
            for p in paths[:_MAX_FILES]:
                # Show exports/routes for key files (routers, pages)
                sig_info = ""
                if signatures and p in signatures:
                    sig = signatures[p]
                    if sig.exports:
                        sig_info = f"  → exports: {', '.join(sig.exports[:8])}"
                    if sig.routes:
                        sig_info += f"  → routes: {', '.join(sig.routes[:5])}"
                cand_lines.append(f"  {p}")
                if sig_info:
                    cand_lines.append(f"    {sig_info}")
            if len(paths) > _MAX_FILES:
                cand_lines.append(f"  ... and {len(paths) - _MAX_FILES} more")
    candidates_text = "\n".join(cand_lines)

    # Format unmatched — collapse to dirs if too many
    _MAX_UNMATCHED = 200
    if len(unmatched) > _MAX_UNMATCHED:
        from collections import defaultdict
        dir_files: dict[str, list[str]] = defaultdict(list)
        for f in unmatched:
            d = str(Path(f).parent) if "/" in f else "."
            dir_files[d].append(Path(f).name)
        lines = []
        for d in sorted(dir_files.keys()):
            samples = dir_files[d][:4]
            lines.append(f"{d}/ ({len(dir_files[d])} files): {', '.join(samples)}")
        unmatched_text = "\n".join(lines)
    else:
        unmatched_text = "\n".join(sorted(unmatched)) if unmatched else "(none)"

    prompt = _USER_PROMPT.format(
        candidates_text=candidates_text,
        unmatched_text=unmatched_text,
    )

    logger.info("Deep scan: %d candidates, %d unmatched files → Sonnet", len(real_candidates), len(unmatched))

    # Call Sonnet (operations-based — no file lists in response)
    ops = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.messages.create(
                model=resolved_model,
                max_tokens=8_192,
                temperature=0,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )

            # D4: record token usage and check budget immediately, so
            # BudgetExceeded aborts the run before we attempt to parse
            # or retry. The tracker is opt-in; callers without cost
            # tracking pass tracker=None and nothing happens here.
            if tracker is not None:
                usage = getattr(response, "usage", None)
                input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
                tracker.record(
                    provider="anthropic",
                    model=resolved_model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    label="deep-scan",
                )
                tracker.check_budget()  # may raise BudgetExceeded

            text = response.content[0].text if response.content else ""
            parsed = _parse_json_response(text)
            if not parsed:
                logger.warning("Deep scan: could not parse JSON from response")
                continue

            parsed = _normalize_response(parsed)

            try:
                ops = SonnetOpsResponse.model_validate(parsed)
                break
            except ValidationError as e:
                logger.warning("Deep scan validation error: %s", str(e)[:300])
                continue

        except (anthropic.RateLimitError, anthropic.APIConnectionError,
                anthropic.InternalServerError) as e:
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("Deep scan failed (attempt %d): %s. Retry in %.1fs", attempt + 1, e, delay)
            if attempt < _MAX_RETRIES - 1:
                time.sleep(delay)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            logger.error("Deep scan auth error: %s", e)
            break

    if not ops:
        return None

    logger.info("Deep scan: Sonnet returned %d features, %d merges, %d removes, %d splits",
                len(ops.features), len(ops.merge), len(ops.remove), len(ops.split))

    # Apply operations to candidates
    result = dict(real_candidates)

    # Apply merges — largest candidate becomes target
    for group in ops.merge:
        if len(group) < 2:
            continue
        existing = [(n, len(result.get(n, []))) for n in group if n in result]
        if len(existing) < 2:
            continue
        target = max(existing, key=lambda x: x[1])[0]
        for name, _ in existing:
            if name != target:
                result[target].extend(result.pop(name))
                logger.info("Merged '%s' into '%s'", name, target)

    # Apply splits — distribute files by keyword matching on subdirectory/filename
    for split_op in ops.split:
        source = split_op.from_name
        if source not in result or len(split_op.into) < 2:
            continue
        source_files = result.pop(source)
        # Distribute files: match each file to the best sub-feature by keyword
        sub_features: dict[str, list[str]] = {name: [] for name in split_op.into}
        remainder: list[str] = []
        for fp in source_files:
            fp_lower = fp.lower()
            placed = False
            for sub_name in split_op.into:
                # Extract keywords from sub-feature name (e.g. "document-signing" → ["signing"])
                keywords = [k for k in sub_name.replace(source, "").split("-") if k and len(k) >= 3]
                if any(kw in fp_lower for kw in keywords):
                    sub_features[sub_name].append(fp)
                    placed = True
                    break
            if not placed:
                remainder.append(fp)
        # Put remainder in the first (primary) sub-feature
        if remainder:
            sub_features[split_op.into[0]].extend(remainder)
        for sub_name, sub_files in sub_features.items():
            if sub_files:
                result[sub_name] = sub_files
                logger.info("Split '%s' → '%s' (%d files)", source, sub_name, len(sub_files))

    # Apply renames
    for rename in ops.rename:
        old = rename.from_name
        new = rename.to
        if old in result and new != old:
            result[new] = result.pop(old)
            logger.info("Renamed '%s' → '%s'", old, new)

    # Apply removes → shared-infra
    infra_files: list[str] = []
    for name in ops.remove:
        if name in result:
            infra_files.extend(result.pop(name))
            logger.info("Removed '%s' → shared-infra", name)
    if infra_files:
        result.setdefault("shared-infra", []).extend(infra_files)

    # Add unmatched catch-all files to shared-infra (not as separate features)
    for name, paths in candidates.items():
        if name in _CATCHALL and paths:
            result.setdefault("shared-infra", []).extend(paths)

    # Store features with flow data for later extraction
    global _last_scan_result
    _last_scan_result = ops

    # Post-process: re-attach docs bucket, canonicalize names, drop
    # phantoms, strip flows for libraries. All pure transformations —
    # unit-tested in tests/test_sonnet_scanner_pipeline.py.
    return _finalize_result(result, docs_files, is_library)


def _normalize_response(data: dict) -> dict:
    """Normalizes LLM response to expected SonnetOpsResponse format.

    Handles variations in how Sonnet returns features (dict vs list)
    while preserving merge/rename/remove operations.
    """
    result = dict(data)

    features = result.get("features", [])

    # If features is a dict keyed by name → convert to list
    if isinstance(features, dict):
        normalized = []
        for name, value in features.items():
            if isinstance(value, dict):
                feat = {"name": name, **value}
                # Normalize flows if dict
                if "flows" in feat and isinstance(feat["flows"], dict):
                    feat["flows"] = [
                        {"name": fn, **(fv if isinstance(fv, dict) else {})}
                        for fn, fv in feat["flows"].items()
                    ]
                normalized.append(feat)
        result["features"] = normalized

    # Normalize rename items — Sonnet may return:
    # [{"from": "x", "to": "y"}] or [["old", "new"]] or [{"from_name": "x", "to": "y"}]
    renames = result.get("rename", [])
    normalized_renames = []
    for r in renames:
        if isinstance(r, dict):
            if "from" in r and "from_name" not in r:
                r["from_name"] = r.pop("from")
            normalized_renames.append(r)
        elif isinstance(r, list) and len(r) == 2:
            normalized_renames.append({"from_name": r[0], "to": r[1]})
    result["rename"] = normalized_renames

    # Normalize split items (same "from" → "from_name" issue)
    for s in result.get("split", []):
        if isinstance(s, dict) and "from" in s and "from_name" not in s:
            s["from_name"] = s.pop("from")

    return result


def _parse_json_response(text: str) -> dict | None:
    """Extracts JSON from LLM response text."""
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from ```json ... ``` block
    import re
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding first { ... } block
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break

    return None


# ── Flow extraction ────────────────────────────────────────────────────────

_last_scan_result: SonnetOpsResponse | None = None


def get_deep_scan_flows() -> dict[str, list[str]]:
    """
    Returns flow names per feature from the last deep scan.

    Uses fuzzy matching to map Sonnet feature names to candidate names,
    since they may differ (Sonnet says "documents", candidate = "document").

    Returns:
        dict[feature_name → list[flow_name]]
    """
    if not _last_scan_result:
        return {}

    result: dict[str, list[str]] = {}
    for feat in _last_scan_result.features:
        if feat.flows:
            result[feat.name] = [fl.name for fl in feat.flows]

    return result


def match_flows_to_features(
    flow_data: dict[str, list[str]],
    feature_names: list[str],
) -> dict[str, list[str]]:
    """Maps Sonnet flow data to actual feature names via fuzzy matching.

    Sonnet may use "documents" but the feature is named "document".
    """
    result: dict[str, list[str]] = {}
    used_sonnet_names: set[str] = set()

    for feat_name in feature_names:
        # Exact match
        if feat_name in flow_data:
            result[feat_name] = flow_data[feat_name]
            used_sonnet_names.add(feat_name)
            continue

        # Fuzzy: singular/plural, substring
        for sonnet_name, flows in flow_data.items():
            if sonnet_name in used_sonnet_names:
                continue
            if (feat_name in sonnet_name or sonnet_name in feat_name
                    or feat_name.rstrip("s") == sonnet_name.rstrip("s")):
                result[feat_name] = flows
                used_sonnet_names.add(sonnet_name)
                break

    return result


def get_deep_scan_descriptions() -> dict[str, str]:
    """Returns feature descriptions from the last deep scan."""
    if not _last_scan_result:
        return {}
    return {f.name: f.description for f in _last_scan_result.features if f.description}


def get_deep_scan_flow_descriptions() -> dict[str, dict[str, str]]:
    """Returns flow descriptions from the last deep scan."""
    if not _last_scan_result:
        return {}
    result: dict[str, dict[str, str]] = {}
    for feat in _last_scan_result.features:
        if feat.flows:
            result[feat.name] = {fl.name: fl.description for fl in feat.flows}
    return result
