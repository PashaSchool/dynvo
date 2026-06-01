"""Faultlines MCP server — local stdio mode.

Exposes the latest feature-map JSON as tools that AI coding agents
(Cursor, Claude Code, Cline, Aider) can call to get precise codebase
context instead of grepping and reading random files.

The 13 tools' LOGIC lives ONCE, as pure functions in
:mod:`faultlines_mcp.core` (the single source of truth shared with the
HTTP service). The ``@mcp.tool()`` wrappers here are THIN: they load the
feature map from disk via :func:`_load_map`, delegate to the matching
``core`` pure function (with ``runtime=None``), then inject the local-only
freshness / stale warnings before returning. Behavior is unchanged for
local users — each wrapper still returns the same flat dict shape it
always did.

Tools (all 13 from the unified spec):
    list_features       -- overview of all features with health scores
    find_feature        -- semantic search by name or description
    get_feature_files   -- exact file list for a feature
    get_flow_files      -- files belonging to a user-facing flow
    get_repo_summary    -- high-level repo stats
    get_hotspots        -- riskiest features (lowest health)
    get_feature_owners  -- top contributors for a feature
    analyze_change_impact / get_regression_risk      -- blast radius / risk
    find_symbols_in_flow / find_symbols_for_feature  -- symbol attribution
    get_feature_errors / get_feature_pageviews       -- runtime (graceful)

Run:
    faultlines-mcp                  # uses default map location
    FAULTLINE_MAP_PATH=... faultlines-mcp

Install for Cursor (``~/.cursor/mcp.json``)::

    {
      "mcpServers": {
        "faultlines": {
          "command": "faultlines-mcp"
        }
      }
    }
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from faultlines_mcp import core

mcp = FastMCP("faultlines")

_STALE_DAYS = 30                        # warn after this many days


def _stale_warning(fm: dict[str, Any]) -> str | None:
    """Return a warning string if the feature map is older than _STALE_DAYS."""
    analyzed_at = fm.get("analyzed_at")
    if not analyzed_at:
        return None
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(analyzed_at.replace("Z", "+00:00"))
        age = (datetime.now(tz=timezone.utc) - ts).days
        if age > _STALE_DAYS:
            return (
                f"Feature map is {age} days old. Results may be outdated. "
                f"Run `faultlines analyze .` to refresh."
            )
    except (ValueError, TypeError):
        pass
    return None


def _inject_warning(result: dict[str, Any], fm: dict[str, Any]) -> dict[str, Any]:
    """Add stale_warning and freshness fields to the result.

    freshness compares last_scanned_sha to the current git HEAD and
    tells the AI agent how many commits behind the feature map is.
    """
    warning = _stale_warning(fm)
    if warning:
        result["stale_warning"] = warning

    freshness = _git_freshness(fm)
    if freshness is not None:
        result["freshness"] = freshness
        if freshness.get("is_stale"):
            behind = freshness.get("commits_behind", 0)
            auto_on = os.environ.get("FAULTLINE_AUTO_REFRESH") in ("1", "true", "yes")
            if auto_on:
                result["stale_warning"] = (
                    f"Feature map is {behind} commit(s) behind HEAD. "
                    f"A background refresh has been triggered — next query "
                    f"will see fresh data."
                )
            else:
                result["stale_warning"] = (
                    f"Feature map is {behind} commit(s) behind HEAD. "
                    f"Run `faultlines refresh` for an LLM-free incremental update, "
                    f"or set FAULTLINE_AUTO_REFRESH=1 to enable automatic updates."
                )
    return result


def _git_freshness(fm: dict[str, Any]) -> dict[str, Any] | None:
    """Compare stored SHA to current HEAD. Returns None if git unavailable."""
    scanned_sha = fm.get("last_scanned_sha", "")
    repo_path = fm.get("repo_path", "")
    if not scanned_sha or not repo_path:
        return None

    import subprocess
    try:
        current = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, text=True, timeout=3,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if current == scanned_sha:
        return {"is_stale": False, "current_sha": current[:8], "scanned_sha": scanned_sha[:8]}

    try:
        behind = int(subprocess.check_output(
            ["git", "rev-list", "--count", f"{scanned_sha}..{current}"],
            cwd=repo_path, text=True, timeout=5,
        ).strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        behind = 0

    return {
        "is_stale": True,
        "current_sha": current[:8],
        "scanned_sha": scanned_sha[:8],
        "commits_behind": behind,
    }


def _load_map() -> dict[str, Any]:
    """Loads the most recent feature-map JSON.

    Precedence:
        1. ``FAULTLINE_MAP_PATH`` environment variable (explicit path)
        2. Most recent ``~/.faultline/feature-map-*.json``
        3. Raises RuntimeError with install instructions
    """
    explicit = os.environ.get("FAULTLINE_MAP_PATH")
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise RuntimeError(
                f"FAULTLINE_MAP_PATH={explicit} does not exist. "
                f"Run `faultlines analyze .` first."
            )
        data = json.loads(p.read_text())
        _maybe_auto_refresh(p, data)
        return data

    home_dir = Path.home() / ".faultline"
    if not home_dir.exists():
        raise RuntimeError(
            "No feature map found at ~/.faultline/. "
            "Run `faultlines analyze /path/to/your/repo --llm --flows` first."
        )

    scans = sorted(home_dir.glob("feature-map-*.json"))
    if not scans:
        raise RuntimeError(
            "No feature-map-*.json found. "
            "Run `faultlines analyze /path/to/your/repo --llm --flows` first."
        )
    latest = scans[-1]
    data = json.loads(latest.read_text())
    _maybe_auto_refresh(latest, data)
    return data


def _maybe_auto_refresh(path: Path, data: dict[str, Any]) -> None:
    """If FAULTLINE_AUTO_REFRESH is enabled, kick off a background refresh.

    Engine-independent: this shells out to the ``faultlines`` CLI as a
    detached subprocess (never an import). Non-blocking — returns
    immediately. The current query sees the data as it was when loaded;
    the next query will see the refreshed version.

    Only triggers when:
      - ``FAULTLINE_AUTO_REFRESH`` is set to 1/true/yes, AND
      - the map records a ``repo_path`` we can re-scan.
    """
    if os.environ.get("FAULTLINE_AUTO_REFRESH") not in ("1", "true", "yes"):
        return
    repo_path = data.get("repo_path", "")
    if not repo_path or not Path(repo_path).exists():
        return
    import subprocess
    try:
        # Detached, fire-and-forget. Output discarded; the CLI writes a
        # fresh feature-map-*.json that the next _load_map() picks up.
        subprocess.Popen(
            ["faultlines", "refresh", repo_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError):
        # Auto-refresh is best-effort — never break a tool call because
        # the CLI is absent or the spawn failed.
        pass


def _serve(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Load the feature map from disk, delegate to the ``core`` pure fn, and
    inject local-only freshness / stale warnings.

    This is the single bridge between the FastMCP wrappers (which own disk
    I/O) and the pure tool logic (which owns everything else). Each wrapper
    returns the pure fn's ``details`` dict — preserving the historical flat
    response shape local users already depend on — with the warning fields
    merged in. ``runtime=None`` always: the local package has no Sentry/
    PostHog connection.
    """
    fm = _load_map()
    result = core.call_tool(tool_name, fm, args, runtime=None)
    return _inject_warning(dict(result["details"]), fm)


@mcp.tool()
def list_features() -> dict[str, Any]:
    """List all features detected in the codebase with health scores.

    Use this when the user asks "what's in this codebase" or "show me
    all the features". Returns a compact overview sorted by risk so
    the riskiest code is visible first. For details on a specific
    feature, follow up with ``find_feature``.
    """
    return _serve("list_features", {})


@mcp.tool()
def find_feature(query: str) -> dict[str, Any] | None:
    """Find a feature by semantic name, alias, label, or description.

    Use this BEFORE reading random files. Much faster than grep and
    returns the full context: file list, health, ownership, flows.

    Matching order (case-insensitive substring):
      1. custom `display_name` set by the team (dashboard overrides)
      2. original LLM `name`
      3. user-authored `aliases` (e.g. team uses "labels" for what
         the LLM named "tags")
      4. `labels` (e.g. "core", "billing", "beta")
      5. `description`

    Args:
        query: Feature name, alias, or keyword (e.g. "payments",
            "auth", "checkout", "rich text editor", "labels")
    """
    result = _serve("find_feature", {"query": query})
    # Preserve the historical contract: a miss returns ``None``, not a dict.
    if result.get("matched") is False:
        return None
    return result


@mcp.tool()
def get_feature_files(feature_name: str) -> dict[str, Any]:
    """Get the exact list of files that belong to a feature.

    Use this to scope a refactor or code review to the files that
    actually matter, instead of grepping the whole repo. Returns
    both source files and test files where available.

    Args:
        feature_name: Exact feature name from ``list_features``
    """
    return _serve("get_feature_files", {"feature_name": feature_name})


@mcp.tool()
def get_hotspots(limit: int = 5) -> dict[str, Any]:
    """Get the riskiest features in the codebase.

    Use this when the user asks "where are the bugs", "what should I
    refactor next", or "what parts of the code are broken". Returns
    features sorted by health score (worst first) with their hotspot
    files — the specific files accumulating the most bug fixes.

    Args:
        limit: Max features to return (default 5)
    """
    return _serve("get_hotspots", {"limit": limit})


@mcp.tool()
def get_feature_owners(feature_name: str) -> dict[str, Any]:
    """Get the people who maintain a feature.

    Use this when the user asks "who owns X", "who should review this
    PR", or "who knows about Y". Returns top contributors sorted by
    commit count. Also reports bus factor risk if there's only one
    active owner.

    Args:
        feature_name: Exact feature name from ``list_features``
    """
    return _serve("get_feature_owners", {"feature_name": feature_name})


@mcp.tool()
def get_flow_files(feature_name: str, flow_name: str) -> dict[str, Any]:
    """Get files belonging to a specific user-facing flow.

    Use this for PR reviews or targeted refactoring. A flow is a
    named user journey (e.g. "checkout-flow", "manage-team-flow")
    that spans multiple files within a feature.

    Args:
        feature_name: Parent feature name
        flow_name: Flow name from ``find_feature`` results
    """
    return _serve("get_flow_files", {"feature_name": feature_name, "flow_name": flow_name})


@mcp.tool()
def get_repo_summary() -> dict[str, Any]:
    """High-level stats about the repo: features, commits, health, risk.

    Use this for "give me an overview of this codebase" or when
    starting work on an unfamiliar repo. Returns aggregated metrics
    without file-level detail.
    """
    return _serve("get_repo_summary", {})


@mcp.tool()
def analyze_change_impact(changed_files: list[str], repo_path: str = ".") -> dict[str, Any]:
    """Blast radius for a set of files you are about to change.

    Use this BEFORE submitting a PR or making a refactor. Returns which
    features the changed files touch (by path overlap), total impact,
    co-changed-but-missing files, a risk level, and recommendations.
    Engine-free: reads precomputed scan fields — no live git, no engine.

    Args:
        changed_files: Files being changed (repo-relative).
        repo_path: Accepted for compatibility; unused (data comes from the map).
    """
    return _serve("analyze_change_impact",
                  {"changed_files": changed_files, "repo_path": repo_path})


@mcp.tool()
def get_regression_risk(changed_files: list[str]) -> dict[str, Any]:
    """Quick check: how likely is this change to cause a regression?

    Returns a probability (0.0-1.0) based on how buggy the affected
    features have been historically. Use this for a fast go/no-go
    signal before merging.

    Args:
        changed_files: Files being changed (relative to repo root).
    """
    return _serve("get_regression_risk", {"changed_files": changed_files})


@mcp.tool()
def find_symbols_in_flow(feature_name: str, flow_name: str) -> dict[str, Any]:
    """Get precise symbols (functions, classes) that belong to a flow.

    Returns a list of symbols grouped by file, so the AI agent can read
    only the relevant functions instead of the full file. Falls back
    to full file paths when symbol-level attribution is unavailable.

    Args:
        feature_name: Parent feature name (from list_features).
        flow_name: Flow name (from find_feature or get_flow_files).
    """
    return _serve("find_symbols_in_flow",
                  {"feature_name": feature_name, "flow_name": flow_name})


@mcp.tool()
def find_symbols_for_feature(feature_name: str) -> dict[str, Any]:
    """Get the feature's shared symbols (types, interfaces, enums).

    Returns types and interfaces that are shared across all flows in
    the feature. These are the contracts/models your AI agent needs
    to understand the feature's data shape.

    Args:
        feature_name: Feature name from list_features.
    """
    return _serve("find_symbols_for_feature", {"feature_name": feature_name})


@mcp.tool()
def get_feature_errors(feature_name: str, window: str = "24h") -> dict[str, Any]:
    """Production errors (Sentry) mapped to a feature.

    Hosted MCP queries the org's Sentry integration and maps issues to
    the feature by path. The standalone local package has no hosted
    connection, so this returns a graceful, structured "unavailable"
    result — the tool stays REGISTERED so the toolkit is identical
    across deployment modes.

    Args:
        feature_name: Feature name from ``list_features``.
        window: Lookback window (e.g. "24h", "14d"). Hosted-only.
    """
    return _serve("get_feature_errors", {"feature_name": feature_name, "window": window})


@mcp.tool()
def get_feature_pageviews(feature_name: str, window: str = "24h") -> dict[str, Any]:
    """Product usage / pageviews (PostHog) for a feature.

    Hosted MCP queries the org's PostHog integration and maps events to
    the feature by path. The standalone local package has no hosted
    connection, so this returns a graceful, structured "unavailable"
    result — the tool stays REGISTERED so the toolkit is identical
    across deployment modes.

    Args:
        feature_name: Feature name from ``list_features``.
        window: Lookback window (e.g. "24h", "14d"). Hosted-only.
    """
    return _serve("get_feature_pageviews", {"feature_name": feature_name, "window": window})


def main() -> None:
    """Entry point for the ``faultlines-mcp`` console script.

    All 13 tools are registered at import time via the ``@mcp.tool()``
    decorators above; this just starts the stdio server loop.
    """
    mcp.run()


if __name__ == "__main__":
    main()
