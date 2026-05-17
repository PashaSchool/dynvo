"""Faultlines MCP server.

Exposes the latest feature-map JSON as tools that AI coding agents
(Cursor, Claude Code, Cline, Aider) can call to get precise codebase
context instead of grepping and reading random files.

Tools:
    list_features          -- overview of all features (paginated)
    find_feature           -- semantic search by name or description
    get_feature_files      -- file list for a feature
    get_hotspots           -- riskiest features (lowest health)
    get_feature_owners     -- top contributors for a feature
    get_flow_files         -- files belonging to a user-facing flow
    get_repo_summary       -- high-level repo stats
    refresh_feature_map    -- explicitly request a background refresh

Resources (read-only, no side effects — clients browse in UI):
    repo://summary         -- the same payload as get_repo_summary()
    feature://{name}       -- one feature; same shape as find_feature()

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

Implementation lives behind a small public surface in
``faultline.mcp_context`` (``mcp`` instance, ``load_map``,
``resolve_feature``, ``inject_warning``, ``record_call``). Subpackages
import from there — never from this module's private names.
"""

from __future__ import annotations

from typing import Any

from faultline.mcp_context import (
    SCHEMA_VERSION,
    error_payload,
    feature_display_name,
    fuzzy_feature_suggestions,
    inject_warning,
    load_map,
    mcp,
    record_call,
    resolve_feature,
    trigger_auto_refresh,
)


def _not_found(query: str, fm: dict[str, Any], *, kind: str = "Feature") -> dict[str, Any]:
    """Uniform error payload with top-5 fuzzy suggestions (not the full list)."""
    return error_payload(
        f"{kind} '{query}' not found",
        suggestions=fuzzy_feature_suggestions(fm, query),
    )


@mcp.tool()
def list_features(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """List features in the codebase, sorted by risk (worst health first).

    Use this when the user asks "what's in this codebase" or "show me
    all the features". For repos with hundreds of features (monorepos,
    SaaS dashboards) use ``limit`` + ``offset`` to page through.

    Args:
        limit:  Max features to return (default 50, max 200).
        offset: Skip this many before returning (default 0).
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    fm = load_map()
    features = sorted(
        fm.get("features", []),
        key=lambda f: f.get("health_score", 100),
    )
    total = len(features)
    page = features[offset : offset + limit]

    record_call("list_features", files_returned=0)
    return inject_warning({
        "repo_path": fm.get("repo_path", ""),
        "total_features": total,
        "total_commits": fm.get("total_commits", 0),
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
        "features": [
            {
                "name": feature_display_name(f),
                "description": f.get("description"),
                "health": round(f.get("health_score", 0)),
                "bug_fix_ratio": round(f.get("bug_fix_ratio", 0) * 100, 1),
                "commits": f.get("total_commits", 0),
                "file_count": len(f.get("paths", [])),
                "flow_count": len(f.get("flows", [])),
                "coverage_pct": f.get("coverage_pct"),
            }
            for f in page
        ],
    }, fm)


@mcp.tool()
def find_feature(query: str) -> dict[str, Any] | None:
    """Find a feature by semantic name, alias, label, or description.

    Use this BEFORE reading random files. Substring match across
    display_name / name / aliases / labels / description. For an exact
    name (already picked from ``list_features``) the other tools
    accept it directly.

    Args:
        query: Feature name, alias, or keyword (e.g. "payments",
            "auth", "checkout", "rich text editor").
    """
    fm = load_map()
    f = resolve_feature(fm, query, mode="fuzzy")
    if f is None:
        return None

    record_call("find_feature", query_arg=query, files_returned=len(f.get("paths", [])))
    return inject_warning({
        "name": feature_display_name(f),
        "original_name": f.get("original_name") or f.get("name"),
        "aliases": f.get("aliases") or [],
        "labels": f.get("labels") or [],
        "description": f.get("description"),
        "health": round(f.get("health_score", 0)),
        "bug_fix_ratio": round(f.get("bug_fix_ratio", 0) * 100, 1),
        "coverage_pct": f.get("coverage_pct"),
        "files": f.get("paths", []),
        "file_count": len(f.get("paths", [])),
        "owners": f.get("authors", [])[:5],
        "flows": [
            {"name": fl["name"], "health": round(fl.get("health_score", 0))}
            for fl in f.get("flows", [])
        ],
    }, fm)


@mcp.tool()
def get_feature_files(feature_name: str) -> dict[str, Any]:
    """Get the exact list of files that belong to a feature.

    Use this to scope a refactor or code review. Accepts the exact
    name returned by ``find_feature`` / ``list_features`` —
    display_name, name, alias, or label all resolve.

    Args:
        feature_name: Exact feature name from ``list_features``.
    """
    fm = load_map()
    f = resolve_feature(fm, feature_name, mode="exact")
    if f is None:
        return _not_found(feature_name, fm)

    resolved = feature_display_name(f)
    record_call("get_feature_files", query_arg=feature_name, files_returned=len(f.get("paths", [])))
    return inject_warning({
        "feature": resolved,
        "files": f.get("paths", []),
        "file_count": len(f.get("paths", [])),
        "hotspot_files": [
            h for fl in f.get("flows", [])
            for h in fl.get("hotspot_files", [])
        ][:5],
    }, fm)


@mcp.tool()
def get_hotspots(limit: int = 5) -> dict[str, Any]:
    """Get the riskiest features in the codebase.

    Use this when the user asks "where are the bugs", "what should I
    refactor next", or "what parts of the code are broken". Returns
    features sorted by health score (worst first) with hotspot files.

    Args:
        limit: Max features to return (default 5).
    """
    fm = load_map()
    risky = sorted(
        fm.get("features", []),
        key=lambda f: f.get("health_score", 100),
    )[:limit]

    result = []
    for f in risky:
        hotspot_files: list[str] = []
        for fl in f.get("flows", []):
            hotspot_files.extend(fl.get("hotspot_files", []))
        result.append({
            "name": feature_display_name(f),
            "description": f.get("description"),
            "health": round(f.get("health_score", 0)),
            "bug_fix_ratio": round(f.get("bug_fix_ratio", 0) * 100, 1),
            "bug_fixes": f.get("bug_fixes", 0),
            "commits": f.get("total_commits", 0),
            "coverage_pct": f.get("coverage_pct"),
            "hotspot_files": hotspot_files[:3],
            "owners": f.get("authors", [])[:3],
        })

    record_call("get_hotspots", files_returned=limit)
    return inject_warning({"hotspots": result}, fm)


@mcp.tool()
def get_feature_owners(feature_name: str) -> dict[str, Any]:
    """Get the people who maintain a feature.

    Use this when the user asks "who owns X", "who should review this
    PR", or "who knows about Y". Reports bus factor risk if there's
    only one active owner.

    Args:
        feature_name: Exact feature name from ``list_features``.
    """
    fm = load_map()
    f = resolve_feature(fm, feature_name, mode="exact")
    if f is None:
        return _not_found(feature_name, fm)

    authors = f.get("authors", [])
    flow_bus_factors = [fl.get("bus_factor", 1) for fl in f.get("flows", [])]
    min_bus_factor = min(flow_bus_factors) if flow_bus_factors else len(authors) or 1

    record_call("get_feature_owners", query_arg=feature_name, files_returned=1)
    return inject_warning({
        "feature": feature_display_name(f),
        "owners": authors,
        "total_contributors": len(authors),
        "bus_factor": min_bus_factor,
        "at_risk": min_bus_factor == 1,
    }, fm)


@mcp.tool()
def get_flow_files(feature_name: str, flow_name: str) -> dict[str, Any]:
    """Get files belonging to a specific user-facing flow.

    Use this for PR reviews or targeted refactoring. A flow is a
    named user journey (e.g. "checkout-flow", "manage-team-flow")
    that spans multiple files within a feature.

    Args:
        feature_name: Parent feature name.
        flow_name: Flow name from ``find_feature`` results.
    """
    fm = load_map()
    f = resolve_feature(fm, feature_name, mode="exact")
    if f is None:
        return _not_found(feature_name, fm)

    for fl in f.get("flows", []):
        if fl.get("name") == flow_name:
            record_call(
                "get_flow_files",
                query_arg=f"{feature_name}/{flow_name}",
                files_returned=len(fl.get("paths", [])),
            )
            return inject_warning({
                "feature": feature_display_name(f),
                "flow": flow_name,
                "description": fl.get("description"),
                "files": fl.get("paths", []),
                "file_count": len(fl.get("paths", [])),
                "health": round(fl.get("health_score", 0)),
                "bug_fix_ratio": round(fl.get("bug_fix_ratio", 0) * 100, 1),
                "hotspot_files": fl.get("hotspot_files", []),
            }, fm)

    available_flows = [fl.get("name") for fl in f.get("flows", []) if fl.get("name")]
    return error_payload(
        f"Flow '{flow_name}' in feature '{feature_display_name(f)}' not found",
        available_flows=available_flows,
    )


@mcp.tool()
def get_repo_summary() -> dict[str, Any]:
    """High-level stats about the repo: features, commits, health, risk.

    Use this for "give me an overview of this codebase" or when
    starting work on an unfamiliar repo. No file-level detail.
    """
    fm = load_map()
    features = fm.get("features", [])
    total_bug_fixes = sum(f.get("bug_fixes", 0) for f in features)
    avg_health = (
        sum(f.get("health_score", 0) for f in features) / len(features)
        if features else 0
    )
    at_risk = sum(1 for f in features if f.get("health_score", 100) < 50)
    with_coverage = [
        f.get("coverage_pct") for f in features if f.get("coverage_pct") is not None
    ]
    avg_coverage = sum(with_coverage) / len(with_coverage) if with_coverage else None

    record_call("get_repo_summary")
    return inject_warning({
        "repo_path": fm.get("repo_path", ""),
        "remote_url": fm.get("remote_url", ""),
        "analyzed_at": fm.get("analyzed_at", ""),
        "date_range_days": fm.get("date_range_days", 0),
        "total_commits": fm.get("total_commits", 0),
        "total_features": len(features),
        "total_flows": sum(len(f.get("flows", [])) for f in features),
        "total_bug_fixes": total_bug_fixes,
        "avg_health_score": round(avg_health, 1),
        "avg_coverage_pct": round(avg_coverage, 1) if avg_coverage is not None else None,
        "features_at_risk": at_risk,
    }, fm)


@mcp.tool()
def refresh_feature_map() -> dict[str, Any]:
    """Trigger a non-blocking refresh of the feature map.

    Use this when ``freshness.is_stale`` shows commits behind HEAD.
    Returns immediately; the next tool call sees the refreshed data.
    No-op if no refresh helper is installed.
    """
    result = trigger_auto_refresh()
    result["_schema_version"] = SCHEMA_VERSION
    record_call("refresh_feature_map")
    return result


# ---------------------------------------------------------------------------
# MCP Resources — read-only, client-discoverable, no agent invocation needed.
# Cursor / Claude show these in their UI so a developer can browse features
# without prompting the model first.
# ---------------------------------------------------------------------------

@mcp.resource("repo://summary")
def resource_repo_summary() -> dict[str, Any]:
    """High-level repo stats. Mirrors `get_repo_summary()` exactly."""
    return get_repo_summary()


@mcp.resource("feature://{name}")
def resource_feature(name: str) -> dict[str, Any]:
    """One feature by name / alias / label. Same shape as `find_feature()`."""
    fm = load_map()
    f = resolve_feature(fm, name, mode="exact")
    if f is None:
        return _not_found(name, fm)
    return inject_warning({
        "name": feature_display_name(f),
        "original_name": f.get("original_name") or f.get("name"),
        "aliases": f.get("aliases") or [],
        "labels": f.get("labels") or [],
        "description": f.get("description"),
        "health": round(f.get("health_score", 0)),
        "bug_fix_ratio": round(f.get("bug_fix_ratio", 0) * 100, 1),
        "coverage_pct": f.get("coverage_pct"),
        "files": f.get("paths", []),
        "file_count": len(f.get("paths", [])),
        "owners": f.get("authors", [])[:5],
        "flows": [
            {"name": fl["name"], "health": round(fl.get("health_score", 0))}
            for fl in f.get("flows", [])
        ],
    }, fm)


def main() -> None:
    """Entry point for the ``faultlines-mcp`` console script."""
    # Register impact analysis tools (adds 2 more MCP tools)
    import faultline.impact.mcp_tools  # noqa: F401
    # Register symbol-level attribution tools (adds 2 more MCP tools)
    import faultline.symbols.mcp_tools  # noqa: F401

    mcp.run()


if __name__ == "__main__":
    main()
