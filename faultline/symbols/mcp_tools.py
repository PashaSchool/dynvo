"""MCP tools that expose symbol-level attribution.

Registers tools on the shared MCP server. Agents get precise
function-level context when available, falling back to full file
paths when symbols haven't been attributed.
"""

from __future__ import annotations

from typing import Any

from faultline.mcp_context import (
    error_payload,
    feature_display_name,
    fuzzy_feature_suggestions,
    inject_warning,
    load_map,
    mcp,
    record_call,
    resolve_feature,
)


def _build_deeplinks(
    remote_url: str,
    file_path: str,
    line_ranges: list[Any],
) -> list[str]:
    """Build GitHub blob URLs for each line range.

    Returns ``["{remote}/blob/HEAD/{file}#L{start}-L{end}", ...]``.
    Falls back to a bare file path when remote_url is empty. Ranges
    accept ``[start, end]`` or ``(start, end)`` — JSON sends lists,
    Python sends tuples.
    """
    if not file_path:
        return []
    base = remote_url.rstrip("/") if remote_url else ""
    out: list[str] = []
    for rng in line_ranges or []:
        if not isinstance(rng, (list, tuple)) or len(rng) != 2:
            continue
        start, end = rng[0], rng[1]
        if base:
            out.append(f"{base}/blob/HEAD/{file_path}#L{start}-L{end}")
        else:
            out.append(f"{file_path}#L{start}-L{end}")
    return out


def _feature_not_found(query: str, fm: dict[str, Any]) -> dict[str, Any]:
    return error_payload(
        f"Feature '{query}' not found",
        suggestions=fuzzy_feature_suggestions(fm, query),
    )


@mcp.tool()
def find_symbols_in_flow(feature_name: str, flow_name: str) -> dict[str, Any]:
    """Get precise symbols (functions, classes) that belong to a flow.

    Returns symbols grouped by file so the agent reads only the relevant
    functions instead of full files. Falls back to file paths when
    symbol-level attribution is unavailable.

    Args:
        feature_name: Parent feature name (from list_features).
        flow_name: Flow name (from find_feature or get_flow_files).
    """
    fm = load_map()
    f = resolve_feature(fm, feature_name, mode="exact")
    if f is None:
        return _feature_not_found(feature_name, fm)

    for fl in f.get("flows", []):
        if fl.get("name") != flow_name:
            continue

        attributions = fl.get("symbol_attributions", [])
        remote = fm.get("remote_url", "")
        resolved_feature = feature_display_name(f)

        if attributions:
            record_call(
                "find_symbols_in_flow",
                query_arg=f"{feature_name}/{flow_name}",
                files_returned=len(attributions),
            )
            return inject_warning({
                "feature": resolved_feature,
                "flow": flow_name,
                "precision": "symbol-level",
                "attributions": [
                    {
                        "file": a.get("file_path"),
                        "symbols": a.get("symbols", []),
                        "roles": a.get("roles", {}),
                        "line_ranges": a.get("line_ranges", []),
                        "attributed_lines": a.get("attributed_lines", 0),
                        "total_file_lines": a.get("total_file_lines", 0),
                        "deeplinks": _build_deeplinks(
                            remote, a.get("file_path", ""), a.get("line_ranges", []),
                        ),
                    }
                    for a in attributions if a.get("symbols")
                ],
                "fallback_files": fl.get("paths", []),
                "hint": (
                    "Read only the symbols listed. Use deeplinks for direct "
                    "GitHub navigation. Fall back to fallback_files when full "
                    "context is needed."
                ),
            }, fm)

        # No symbol-level data — redirect to get_flow_files instead of
        # silently mimicking it. Lets the agent pick one tool clearly.
        record_call(
            "find_symbols_in_flow",
            query_arg=f"{feature_name}/{flow_name}",
            files_returned=0,
        )
        return error_payload(
            "Symbol-level attribution not available for this scan.",
            code="no_symbol_attribution",
            feature=resolved_feature,
            flow=flow_name,
            hint=(
                "Call `get_flow_files` for the file-level view of this flow. "
                "For function-level precision, re-run "
                "`faultlines analyze . --llm --flows --symbols`."
            ),
        )

    available_flows = [fl.get("name") for fl in f.get("flows", []) if fl.get("name")]
    return error_payload(
        f"Flow '{flow_name}' in feature '{feature_display_name(f)}' not found",
        available_flows=available_flows,
    )


@mcp.tool()
def find_symbols_for_feature(feature_name: str) -> dict[str, Any]:
    """Get the feature's shared symbols (types, interfaces, enums).

    Returns types and interfaces shared across all flows in the
    feature — the contracts your AI agent needs to understand the
    data shape. For function-level attribution per flow use
    ``find_symbols_in_flow``.

    Args:
        feature_name: Feature name from list_features.
    """
    fm = load_map()
    f = resolve_feature(fm, feature_name, mode="exact")
    if f is None:
        return _feature_not_found(feature_name, fm)

    shared = f.get("shared_attributions", [])
    remote = fm.get("remote_url", "")
    record_call(
        "find_symbols_for_feature",
        query_arg=feature_name,
        files_returned=len(shared),
    )
    return inject_warning({
        "feature": feature_display_name(f),
        "description": f.get("description"),
        "shared_symbols": [
            {
                "file": a.get("file_path"),
                "symbols": a.get("symbols", []),
                "roles": a.get("roles", {}),
                "line_ranges": a.get("line_ranges", []),
                "deeplinks": _build_deeplinks(
                    remote, a.get("file_path", ""), a.get("line_ranges", []),
                ),
            }
            for a in shared if a.get("symbols")
        ],
        "all_files": f.get("paths", []),
        "flow_count": len(f.get("flows", [])),
        "hint": (
            "Shared symbols are types, interfaces, and enums used across "
            "all flows. For function-level attribution per flow, use "
            "find_symbols_in_flow."
        ),
    }, fm)
