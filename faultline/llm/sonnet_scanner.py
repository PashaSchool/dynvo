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
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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


# ── DeepScanResult dataclass (D10) ─────────────────────────────────────────
#
# The legacy interface returned a bare ``dict[str, list[str]]`` and stashed
# flows + descriptions in the module-global ``_last_scan_result``. This
# breaks reentrancy (you can't analyze two repos in one process) and forces
# the caller to know about three different read paths.
#
# ``DeepScanResult`` collects all of that into a single value. To avoid
# breaking the existing dict-iterating callers in cli.py and the test
# suite, the dataclass also implements the dict read interface
# (``__getitem__``, ``__iter__``, ``__contains__``, ``__len__``,
# ``items``/``keys``/``values``, ``get``). New code should prefer the
# explicit attributes (``result.features``, ``result.flows``, etc.) and
# old code keeps working unchanged.


@dataclass
class DeepScanResult:
    """Structured return value from ``deep_scan`` and ``deep_scan_workspace``.

    Attributes:
        features: feature_name → list[file_path] (the primary mapping)
        flows: feature_name → list[flow_name] (empty for libraries)
        descriptions: feature_name → one-line description
        flow_descriptions: feature_name → flow_name → description
        cost_summary: snapshot of the cost tracker at scan completion
            (``None`` when no tracker was passed in)

    The dataclass is intentionally dict-compatible at the read level so
    legacy callers that iterate the result as a feature map keep working.
    """

    features: dict[str, list[str]] = field(default_factory=dict)
    flows: dict[str, list[str]] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    flow_descriptions: dict[str, dict[str, str]] = field(default_factory=dict)
    cost_summary: dict[str, Any] | None = None

    # ── dict read shims (legacy compat) ─────────────────────────────────
    def __getitem__(self, key: str) -> list[str]:
        return self.features[key]

    def __setitem__(self, key: str, value: list[str]) -> None:
        self.features[key] = value

    def __contains__(self, key: object) -> bool:
        return key in self.features

    def __iter__(self):
        return iter(self.features)

    def __len__(self) -> int:
        return len(self.features)

    def __bool__(self) -> bool:
        return bool(self.features)

    def items(self):
        return self.features.items()

    def keys(self):
        return self.features.keys()

    def values(self):
        return self.features.values()

    def get(self, key: str, default: Any = None) -> Any:
        return self.features.get(key, default)


# ── Commit context builder (D5) ────────────────────────────────────────────


def build_commit_context(
    commits: list | None,
    *,
    top_n: int = 30,
    days: int = 90,
) -> str | None:
    """Render a compact "recent activity" snippet for the LLM prompt.

    Counts how often each file (and its parent directory) was touched
    by commits in the last ``days`` days, then returns the top ``top_n``
    entries as a single newline-separated string. The result is meant
    to be appended to the user prompt under a ``## Recent activity``
    heading so Sonnet can distinguish actively developed features
    from dormant ones.

    The output is intentionally bounded:
      - Only the ``top_n`` most-touched paths are included
      - Each entry is a single line (``path  N commits``)
      - Files and directories are interleaved by commit count, so
        a hot feature directory rises above its individual files

    Returns ``None`` when there is nothing useful to inject (no
    commits, no files in the last ``days`` window). The caller can
    pass ``None`` straight through to ``deep_scan(commit_context=...)``
    without a guard.

    The shape was chosen to spend at most ~600 tokens on the
    ``## Recent activity`` section, leaving the rest of Sonnet's
    context budget for the actual candidates and file lists.
    """
    if not commits:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    file_counts: Counter[str] = Counter()
    dir_counts: Counter[str] = Counter()

    for commit in commits:
        commit_date = getattr(commit, "date", None)
        if commit_date is None:
            continue
        # Normalize naive datetimes to UTC so the comparison is safe.
        if commit_date.tzinfo is None:
            commit_date = commit_date.replace(tzinfo=timezone.utc)
        if commit_date < cutoff:
            continue

        files = getattr(commit, "files_changed", None) or []
        for fp in files:
            file_counts[fp] += 1
            parent = str(Path(fp).parent)
            if parent and parent != ".":
                dir_counts[parent] += 1

    if not file_counts and not dir_counts:
        return None

    # Interleave files and directories by count, then take top_n.
    combined: Counter[str] = Counter()
    for path, count in file_counts.items():
        combined[path] = count
    for path, count in dir_counts.items():
        # Mark dirs with trailing slash so the LLM can tell them apart.
        combined[path.rstrip("/") + "/"] = count

    entries = combined.most_common(top_n)
    if not entries:
        return None

    lines = [f"{path}  {count} commits" for path, count in entries]
    return "\n".join(lines)


# ── Prompts ────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT_TEMPLATE = """\
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

**HARD CAP: never produce more than 8 sub-features from a single split.** If you \
cannot identify 8 or fewer cohesive business sub-domains, do not split — leave the \
candidate as one feature.

{target_block}

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


# Target-block variants. The repo-wide variant asks for 12-25 features. The
# package variant tells the LLM it's analyzing a single monorepo package and
# should return at most 8 features (often just 1). Both are injected into
# ``_SYSTEM_PROMPT_TEMPLATE`` via ``_build_system_prompt``.
_TARGET_REPO = (
    "## Target\n\n"
    "After all operations: **12-25 business features**. Not 5, not 50."
)

_TARGET_PACKAGE_TEMPLATE = (
    "## Target\n\n"
    "This is a single package within a monorepo (name: `{package_name}`). "
    "Return **1-8 features** for this package only — NOT 12-25. If the package "
    "has a single cohesive purpose (e.g. `auth`, `db`, `cli`), return ONE feature "
    "named after the package. Only split into multiple features if you can identify "
    "2 or more distinct business sub-domains from the file paths. **HARD CAP: never "
    "more than 8 features for a single package, ever.**"
)


def _build_system_prompt(
    *,
    package_mode: bool = False,
    package_name: str | None = None,
) -> str:
    """Render the system prompt with the right ``## Target`` block.

    When ``package_mode`` is True the LLM is told it's analyzing a single
    monorepo package and asked for 1-8 features instead of 12-25. Used by
    ``deep_scan_workspace`` to drive per-package invocations without the
    cli.py:304 ``_SPLIT_THRESHOLD=200`` hack (delta D7).
    """
    if package_mode:
        # Use .replace, not .format — the template body contains literal
        # '{' from JSON examples that would otherwise need double-escaping.
        target = _TARGET_PACKAGE_TEMPLATE.replace(
            "{package_name}", package_name or "unknown"
        )
    else:
        target = _TARGET_REPO
    return _SYSTEM_PROMPT_TEMPLATE.replace("{target_block}", target)


# Backwards-compat alias for code that imported the old constant by name.
# Resolves the repo-wide variant lazily so existing tests still see the
# 12-25 target string they expect.
_SYSTEM_PROMPT = _build_system_prompt()


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
    package_mode: bool = False,
    package_name: str | None = None,
    commit_context: str | None = None,
) -> DeepScanResult | None:
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
        package_mode: When True, swap the system prompt to a per-package
            variant that asks for 1-8 features instead of 12-25. Used by
            ``deep_scan_workspace`` to analyze a single monorepo package
            in isolation. Default False (whole-repo mode).
        package_name: Name of the package being analyzed in
            ``package_mode``. Inserted into the per-package prompt so the
            LLM knows what to call the resulting feature when collapsing
            to a single bucket. Ignored when ``package_mode=False``.
        commit_context: Optional pre-rendered string describing recent
            activity (top modified files/dirs over the last N days).
            Built via ``build_commit_context(commits)``. When provided,
            appended to the user prompt under a ``## Recent activity``
            heading so Sonnet can weigh actively developed areas more
            heavily when naming features.

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
    system_prompt = _build_system_prompt(
        package_mode=package_mode,
        package_name=package_name,
    )

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

    # D5: append recent-activity context when available. Bounded by
    # ``build_commit_context`` to ~30 lines so it doesn't blow the
    # token budget on huge repos.
    if commit_context:
        prompt = (
            f"{prompt}\n\n"
            f"## Recent activity (last 90 days, top files/dirs)\n"
            f"{commit_context}"
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
                system=system_prompt,
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
    features_dict = _finalize_result(result, docs_files, is_library)

    # D10: wrap into a DeepScanResult that carries flows + descriptions +
    # cost summary alongside the feature map. Reads from the global
    # side channel ``_last_scan_result`` populated above; legacy
    # ``get_deep_scan_*`` accessors continue to work for callers that
    # haven't migrated yet.
    return _build_deep_scan_result(features_dict, tracker)


# ── DeepScanResult builder (D10) ───────────────────────────────────────────


def _build_deep_scan_result(
    features: dict[str, list[str]],
    tracker: CostTracker | None,
) -> DeepScanResult:
    """Snapshot the global ``_last_scan_result`` into a DeepScanResult.

    Reads flows, descriptions, and flow descriptions from the module-level
    ``_last_scan_result`` that ``deep_scan`` populated just before calling
    this helper. Captured here so the return value is self-contained and
    doesn't leave callers at the mercy of a subsequent ``deep_scan`` call
    overwriting the global (the old reentrancy hazard).

    The global itself is intentionally NOT cleared — the legacy
    ``get_deep_scan_flows`` / ``get_deep_scan_descriptions`` readers are
    still exported so existing callers in ``cli.py`` keep working until
    the Week 2 cutover.
    """
    flows: dict[str, list[str]] = {}
    descriptions: dict[str, str] = {}
    flow_descriptions: dict[str, dict[str, str]] = {}

    if _last_scan_result is not None:
        for feat in _last_scan_result.features:
            if feat.flows:
                flows[feat.name] = [fl.name for fl in feat.flows]
                flow_descriptions[feat.name] = {
                    fl.name: fl.description for fl in feat.flows
                }
            if feat.description:
                descriptions[feat.name] = feat.description

    cost_summary = tracker.summary() if tracker is not None else None

    return DeepScanResult(
        features=features,
        flows=flows,
        descriptions=descriptions,
        flow_descriptions=flow_descriptions,
        cost_summary=cost_summary,
    )


# ── Workspace-aware orchestration (D6) ─────────────────────────────────────
#
# Monorepos like documenso (2.5K files) and cal.com (10K) timed out the
# legacy single-call ``deep_scan`` at 600s. The fix is to call it once per
# workspace package, with each call seeing only its own files. This keeps
# every individual prompt small enough that Sonnet can answer in <60s, and
# total cost grows linearly with the number of real packages instead of
# combinatorially with file count.
#
# This helper does NOT touch ``cli.py``. The Week 2 cutover will replace
# the legacy strategy at ``cli.py:264-380`` with a single call to this
# function. Until then it lives in parallel and can be unit-tested with
# mocked ``deep_scan`` calls.

# Default size threshold below which a package is treated as a single
# feature without an LLM call. Tuned to avoid spending tokens on tiny
# helper packages while still letting medium packages get a name from
# Sonnet. The legacy ``_SPLIT_THRESHOLD=200`` is intentionally NOT used —
# the per-package prompt's HARD CAP of 8 features replaces it.
_DEFAULT_PKG_LLM_FLOOR = 30

# Package-name prefixes that mark example/demo/starter content. These get
# grouped into a single ``examples`` feature instead of one feature per
# package. Mirrors the legacy filter at cli.py:262.
_EXAMPLE_PKG_PREFIXES = (
    "example", "sample", "demo", "template", "starter", "tutorial",
)


def deep_scan_workspace(
    workspace_info,  # WorkspaceInfo from faultline.analyzer.workspace
    *,
    api_key: str | None = None,
    model: str | None = None,
    signatures: dict | None = None,
    is_library: bool = False,
    tracker: CostTracker | None = None,
    min_files_for_llm: int = _DEFAULT_PKG_LLM_FLOOR,
    commit_context: str | None = None,
) -> DeepScanResult | None:
    """Run ``deep_scan`` once per workspace package and merge the results.

    For each package in ``workspace_info.packages``:

      - Test packages (``tests``, ``e2e``, ``__tests__``…) are skipped
        entirely. Tests are never a feature.
      - Example/demo packages (``examples/foo``, ``demo-app``…) are pooled
        into a single synthetic ``examples`` feature. None of them get
        their own LLM call.
      - Packages smaller than ``min_files_for_llm`` become one feature
        named after the package, with no LLM call. The package name is
        already a cohesive label (it came from package.json).
      - Larger packages get their own ``deep_scan(package_mode=True)``
        invocation. The returned features are re-prefixed with the
        package name (``{pkg.name}/{sub-feature}``) when more than one
        sub-feature comes back; a single sub-feature collapses to the
        bare package name to avoid noise like ``auth/auth``.

    The shared ``CostTracker`` is threaded through every per-package
    call so the total cost reported at the end of the run is the sum
    across all packages. ``BudgetExceeded`` from any one call propagates
    out immediately so the caller can stop the scan before firing more
    requests.

    ``workspace_info.root_files`` (anything that wasn't claimed by a
    package — typically CI config, root package.json, README) becomes
    the ``shared-infra`` feature.

    Returns the merged feature → file mapping, or ``None`` if the
    workspace had no usable packages at all.
    """
    from faultline.analyzer.features import detect_candidates
    from faultline.analyzer.validation import (
        canonical_bucket_name,
        is_test_feature_name,
    )

    if workspace_info is None or not workspace_info.packages:
        return None

    raw_mapping: dict[str, list[str]] = {}
    merged_flows: dict[str, list[str]] = {}
    merged_descriptions: dict[str, str] = {}
    merged_flow_descriptions: dict[str, dict[str, str]] = {}
    examples_files: list[str] = []

    # Largest packages first so a budget abort kills the expensive ones
    # before we waste calls on small packages.
    packages = sorted(workspace_info.packages, key=lambda p: -len(p.files))

    for pkg in packages:
        if not pkg.files:
            continue

        name_lower = pkg.name.lower()

        # Test packages: skipped entirely (tests are never a feature).
        if is_test_feature_name(pkg.name):
            logger.info("workspace: skip test package %s (%d files)", pkg.name, len(pkg.files))
            continue

        # Example / demo / tutorial packages: pooled into one bucket.
        if any(name_lower.startswith(prefix) for prefix in _EXAMPLE_PKG_PREFIXES):
            examples_files.extend(pkg.files)
            logger.info("workspace: pool %s (%d files) → examples", pkg.name, len(pkg.files))
            continue

        # Small packages: 1 feature, no LLM call. The package name is
        # already a good label so spending tokens here is pure waste.
        if len(pkg.files) < min_files_for_llm:
            raw_mapping[pkg.name] = list(pkg.files)
            logger.info(
                "workspace: %s (%d files) → 1 feature (under LLM floor)",
                pkg.name,
                len(pkg.files),
            )
            continue

        # Large package: per-package LLM call.
        # Strip the package path prefix so files passed to deep_scan are
        # relative to the package root. This keeps the candidate detector
        # focused and the LLM prompt readable.
        pkg_prefix = pkg.path.rstrip("/") + "/" if pkg.path else ""
        if pkg_prefix:
            pkg_files_rel = [
                f[len(pkg_prefix):] for f in pkg.files if f.startswith(pkg_prefix)
            ]
        else:
            pkg_files_rel = list(pkg.files)

        if not pkg_files_rel:
            raw_mapping[pkg.name] = list(pkg.files)
            continue

        pkg_sigs: dict | None = None
        if signatures:
            pkg_sigs = {
                f[len(pkg_prefix):]: sig
                for f, sig in signatures.items()
                if pkg_prefix and f.startswith(pkg_prefix)
            }
            if not pkg_sigs:
                pkg_sigs = None

        try:
            pkg_candidates = detect_candidates(pkg_files_rel)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "workspace: detect_candidates failed for %s (%s) — fallback 1 feature",
                pkg.name, exc,
            )
            raw_mapping[pkg.name] = list(pkg.files)
            continue

        try:
            sub_result = deep_scan(
                pkg_files_rel,
                pkg_candidates,
                api_key=api_key,
                signatures=pkg_sigs,
                is_library=is_library,
                model=model,
                tracker=tracker,
                package_mode=True,
                package_name=pkg.name,
                commit_context=commit_context,
            )
        except Exception:
            # BudgetExceeded and any other terminal error propagate out
            # so the caller can abort the whole scan with a clean stack.
            raise

        if not sub_result:
            logger.warning(
                "workspace: deep_scan returned no features for %s — fallback 1 feature",
                pkg.name,
            )
            raw_mapping[pkg.name] = list(pkg.files)
            continue

        # ``sub_result`` is a DeepScanResult after D10. Use ``.features``
        # for re-prefixing and pull flows/descriptions for the merged
        # workspace-level result.
        sub_mapping = sub_result.features

        # Re-prefix file paths back to repo-relative form, building a
        # mapping from sub-feature name (in sub_result) → final feature
        # key (in raw_mapping) so we can re-key flows/descriptions to
        # match what cli.py will read.
        sub_to_final: dict[str, str] = {}

        if len(sub_mapping) == 1:
            # Single sub-feature → bare package name (avoids "auth/auth").
            only_name, only_files = next(iter(sub_mapping.items()))
            raw_mapping[pkg.name] = [pkg_prefix + f for f in only_files]
            sub_to_final[only_name] = pkg.name
        else:
            for sub_name, sub_files in sub_mapping.items():
                # Canonical infra names from the per-package call go into
                # the global shared-infra bucket, not pkg.name/shared-infra.
                if canonical_bucket_name(sub_name) == "shared-infra":
                    raw_mapping.setdefault("shared-infra", []).extend(
                        pkg_prefix + f for f in sub_files
                    )
                    sub_to_final[sub_name] = "shared-infra"
                    continue
                final_key = f"{pkg.name}/{sub_name}"
                raw_mapping[final_key] = [pkg_prefix + f for f in sub_files]
                sub_to_final[sub_name] = final_key

        # Merge flows and descriptions under the final feature keys.
        for sub_name, final_key in sub_to_final.items():
            if sub_name in sub_result.flows and final_key not in merged_flows:
                merged_flows[final_key] = sub_result.flows[sub_name]
            if sub_name in sub_result.descriptions and final_key not in merged_descriptions:
                merged_descriptions[final_key] = sub_result.descriptions[sub_name]
            if sub_name in sub_result.flow_descriptions and final_key not in merged_flow_descriptions:
                merged_flow_descriptions[final_key] = sub_result.flow_descriptions[sub_name]
        logger.info("workspace: %s → %d feature(s)", pkg.name, len(sub_mapping))

    if examples_files:
        raw_mapping["examples"] = examples_files

    if workspace_info.root_files:
        raw_mapping.setdefault("shared-infra", []).extend(workspace_info.root_files)

    if not raw_mapping:
        return None

    # Deterministic ordering, matches single-call deep_scan (D11).
    sorted_features = dict(
        sorted(raw_mapping.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    )

    return DeepScanResult(
        features=sorted_features,
        flows=merged_flows,
        descriptions=merged_descriptions,
        flow_descriptions=merged_flow_descriptions,
        cost_summary=tracker.summary() if tracker is not None else None,
    )


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
