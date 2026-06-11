"""Pure, engine-free tool logic — the SINGLE source of truth for all 13
Faultlines MCP tools.

Every tool is a pure function with the uniform signature::

    fn(scan: dict, args: dict, runtime: dict | None = None) -> {"summary": str, "details": dict}

No file or network I/O happens inside these functions: the ``scan`` dict
(a loaded feature-map / Scan object) is passed in by the caller. This lets
the SAME logic back BOTH deployment modes:

* **Local stdio** (:mod:`faultlines_mcp.server`) — the FastMCP wrappers load
  the scan from disk via ``_load_map()`` then delegate here with
  ``runtime=None``.
* **HTTP service** (:mod:`faultlines_mcp.http_service`) — the hosted dashboard
  POSTs ``{tool, args, scan, runtime}``; the service looks up
  ``TOOLS[tool]["fn"]`` and calls it.

The two runtime tools (``get_feature_errors`` / ``get_feature_pageviews``)
read ``runtime["errors"]`` / ``runtime["pageviews"]`` when the hosted proxy
supplies them; otherwise they return a graceful ``{available: False, ...}``
result so the toolkit stays identical across modes.

The ``TOOLS`` registry at the bottom maps each of the 13 tool names to
``{fn, description, inputSchema}``. Both deployment modes iterate it for
``tools/list`` and dispatch ``/call`` through it — there is no second copy
of any tool's logic.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Shared pure helpers
# ---------------------------------------------------------------------------

def _attach_response_metadata(details: dict[str, Any], files_returned: int) -> dict[str, Any]:
    """Attach honest per-response token accounting under ``_savings_metadata``.

    DECISION (replaces the old static formula): the previous implementation
    claimed a fixed ``15 files × 2500 tokens − 500 = 37000 tokens saved`` on
    every call regardless of the result — fake precision. The scan JSON that
    core.py receives carries file *paths* but no file sizes, so no grounded
    counterfactual ("what would grep-and-read have cost?") is computable from
    available data. Per the honesty rule, the savings claim is therefore
    REMOVED entirely; we report only what we can actually measure:

    * ``response_tokens_est`` — estimated token size of the actual returned
      payload, ``len(json.dumps(details)) // 4`` (~4 chars/token), computed
      BEFORE this metadata block is inserted.
    * ``files_returned`` — how many files/items the response carries.

    The ``_savings_metadata`` key name is kept so consumers that look for the
    block keep finding one; the fabricated fields (``estimated_tokens_saved``,
    ``baseline_tokens``) are gone.
    """
    payload = json.dumps(details, default=str, separators=(",", ":"))
    details["_savings_metadata"] = {
        "response_tokens_est": max(1, len(payload) // 4),
        "files_returned": files_returned,
    }
    return details


# --- find_feature tokenization / scoring -----------------------------------

# Fixed suffix folds for trivial plural/gerund variants ("payments" ~
# "payment", "billing" ~ "bill"). Deliberately tiny — prefix matching already
# covers most variants; this is NOT a stemmer. Longest-first, applied once.
_SUFFIX_FOLDS = ("ing", "es", "s")
_MIN_PREFIX_LEN = 3  # shortest prefix that counts as a partial token match

_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _fold_suffix(token: str) -> str:
    """Fold a trivial trailing suffix (fixed list, once, never below 3 chars)."""
    for suffix in _SUFFIX_FOLDS:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def _tokenize(text: str) -> list[str]:
    """Split text into normalized tokens.

    Handles kebab-case, snake_case, camelCase, spaces, and punctuation
    (parens, dots, slashes — anything non-alphanumeric is a separator).
    Tokens are lowercased and suffix-folded.
    """
    if not text:
        return []
    text = _CAMEL_SPLIT_RE.sub(" ", text)
    return [_fold_suffix(t) for t in _NON_ALNUM_RE.split(text.lower()) if t]


def _token_coverage(query_tokens: list[str], hay_tokens: list[str]) -> float:
    """Fraction of query tokens found in the haystack tokens, in [0, 1].

    Per query token, the best credit across hay tokens:
      * 1.0 — exact token match (after fold)
      * 0.5 — prefix match either direction, shorter side ≥ 3 chars
        ("org" matches "organization"; "organizations" matches "organ")
      * 0.0 — otherwise
    """
    if not query_tokens or not hay_tokens:
        return 0.0
    hay_set = set(hay_tokens)
    total = 0.0
    for q in query_tokens:
        if q in hay_set:
            total += 1.0
            continue
        for h in hay_tokens:
            if len(q) >= _MIN_PREFIX_LEN and h.startswith(q):
                total += 0.5
                break
            if len(h) >= _MIN_PREFIX_LEN and q.startswith(h):
                total += 0.5
                break
    return total / len(query_tokens)


def _score_feature(query_tokens: list[str], f: dict[str, Any]) -> float:
    """Deterministic relevance score for one feature.

    Formula (documented; weights are field trust, coverage is per-field
    ``_token_coverage`` in [0, 1])::

        score = 3 * name_coverage      # display_name + name tokens
              + 2 * alias_coverage     # aliases + label names
              + 1 * desc_coverage      # description

    Max score 6.0. Zero means no token overlap anywhere.
    """
    name_tokens: list[str] = []
    for key in ("display_name", "name"):
        v = f.get(key)
        if isinstance(v, str):
            name_tokens.extend(_tokenize(v))

    alias_tokens: list[str] = []
    for v in f.get("aliases") or []:
        if isinstance(v, str):
            alias_tokens.extend(_tokenize(v))
    for lab in f.get("labels") or []:
        name = lab.get("name") if isinstance(lab, dict) else None
        if isinstance(name, str):
            alias_tokens.extend(_tokenize(name))

    desc = f.get("description")
    desc_tokens = _tokenize(desc) if isinstance(desc, str) else []

    return round(
        3 * _token_coverage(query_tokens, name_tokens)
        + 2 * _token_coverage(query_tokens, alias_tokens)
        + 1 * _token_coverage(query_tokens, desc_tokens),
        3,
    )


def _features(scan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the developer-feature list under either key (``features`` is
    the canonical alias; ``developer_features`` is the raw pipeline name)."""
    return scan.get("features") or scan.get("developer_features") or []


def _match_feature(scan: dict[str, Any], query: str) -> dict[str, Any] | None:
    """Resolve a feature by exact display_name / name / alias / label match.

    Case-insensitive exact equality, not substring — callers pass the name
    the user already picked, not a fuzzy query. Falls back to case-insensitive
    match so "Tags" resolves "tags".
    """
    q = (query or "").strip().lower()
    for f in _features(scan):
        display = (f.get("display_name") or "").lower()
        name = (f.get("name") or "").lower()
        if q == display or q == name:
            return f
        aliases = f.get("aliases") or []
        if any(q == (a or "").lower() for a in aliases):
            return f
        labels = f.get("labels") or []
        if any(q == (lab.get("name") or "").lower() for lab in labels if isinstance(lab, dict)):
            return f
    return None


def _build_deeplinks(
    remote_url: str,
    file_path: str,
    line_ranges: list[Any],
) -> list[str]:
    """Build GitHub blob URLs for each line range.

    Returns ``["{remote}/blob/HEAD/{file}#L{start}-L{end}", ...]``. Falls back
    to a bare file path when remote_url is empty (e.g. local-only scans). Range
    items are accepted as ``[start, end]`` or ``(start, end)`` — JSON sends
    lists, Python sends tuples.
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


# ---------------------------------------------------------------------------
# The 13 pure tool functions
# ---------------------------------------------------------------------------

def list_features(scan: dict[str, Any], args: dict[str, Any],
                  runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """List all features detected in the codebase with health scores.

    Returns a compact overview sorted by risk (lowest health first) so the
    riskiest code is visible first. For details on a specific feature, follow
    up with ``find_feature``.
    """
    features = sorted(_features(scan), key=lambda f: f.get("health_score", 100))
    details = {
        "repo_path": scan.get("repo_path", ""),
        "total_features": len(features),
        "total_commits": scan.get("total_commits", 0),
        "features": [
            {
                "name": f["name"],
                "description": f.get("description"),
                "health": round(f.get("health_score", 0)),
                "bug_fix_ratio": round(f.get("bug_fix_ratio", 0) * 100, 1),
                "commits": f.get("total_commits", 0),
                "file_count": len(f.get("paths", [])),
                "flow_count": len(f.get("flows", [])),
                "coverage_pct": f.get("coverage_pct"),
            }
            for f in features
        ],
    }
    return {
        "summary": f"{len(features)} feature(s) detected, sorted by risk (worst first).",
        "details": _attach_response_metadata(details, 0),
    }


def find_feature(scan: dict[str, Any], args: dict[str, Any],
                 runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Find a feature by semantic name, alias, label, or description.

    Use this BEFORE reading random files. Much faster than grep and returns
    the full context: file list, health, ownership, flows. Matching is
    token-based (kebab/snake/camel split, punctuation stripped), so
    "knowledge RAG" finds "organization-knowledge-base-(rag)". See
    :func:`_score_feature` for the exact formula. The best match is returned
    with the historical flat fields; up to 3 ranked ``candidates`` ride along
    (additive — existing consumers keep working). ``matched: false`` only
    when NO feature has any token overlap with the query.

    Args:
        query: Feature name, alias, or keyword (e.g. "payments", "auth").
    """
    query = args.get("query") or ""
    query_tokens = _tokenize(query)

    scored = [
        (score, f)
        for f in _features(scan)
        if (score := _score_feature(query_tokens, f)) > 0
    ]
    # Deterministic: score desc, then name asc as a tie-break.
    scored.sort(key=lambda sf: (-sf[0], (sf[1].get("display_name") or sf[1].get("name") or "")))

    if not scored:
        return {
            "summary": f"No feature matched query '{query}'.",
            "details": {"matched": False, "query": args.get("query"), "candidates": []},
        }

    candidates = [
        {
            "name": f.get("display_name") or f.get("name"),
            "score": score,
            "description": f.get("description"),
            "file_count": len(f.get("paths", [])),
        }
        for score, f in scored[:3]
    ]
    best_score, f = scored[0]
    display_name = f.get("display_name") or f["name"]
    details = {
        "matched": True,
        "name": display_name,
        "match_score": best_score,
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
        "candidates": candidates,
    }
    return {
        "summary": (
            f"Matched feature '{display_name}' "
            f"({len(f.get('paths', []))} file(s), health "
            f"{round(f.get('health_score', 0))}; "
            f"{len(candidates)} candidate(s))."
        ),
        "details": _attach_response_metadata(details, len(f.get("paths", []))),
    }


def get_feature_files(scan: dict[str, Any], args: dict[str, Any],
                      runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get the exact list of files that belong to a feature.

    Use this to scope a refactor or code review to the files that actually
    matter, instead of grepping the whole repo.

    Args:
        feature_name: Exact feature name from ``list_features``.
    """
    feature_name = args.get("feature_name", "")
    f = _match_feature(scan, feature_name)
    if f:
        resolved = f.get("display_name") or f.get("name") or feature_name
        details = {
            "feature": resolved,
            "files": f.get("paths", []),
            "file_count": len(f.get("paths", [])),
            "hotspot_files": [
                h for fl in f.get("flows", [])
                for h in fl.get("hotspot_files", [])
            ][:5],
        }
        return {
            "summary": f"{len(f.get('paths', []))} file(s) in feature '{resolved}'.",
            "details": _attach_response_metadata(details, len(f.get("paths", []))),
        }
    return {
        "summary": f"Feature '{feature_name}' not found.",
        "details": {
            "error": f"Feature '{feature_name}' not found",
            "available": [f.get("display_name") or f["name"] for f in _features(scan)],
        },
    }


def get_flow_files(scan: dict[str, Any], args: dict[str, Any],
                   runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get files belonging to a specific user-facing flow.

    A flow is a named user journey (e.g. "checkout-flow") that spans multiple
    files within a feature.

    Args:
        feature_name: Parent feature name.
        flow_name: Flow name from ``find_feature`` results.
    """
    feature_name = args.get("feature_name", "")
    flow_name = args.get("flow_name", "")
    for f in _features(scan):
        if f.get("name") != feature_name:
            continue
        for fl in f.get("flows", []):
            if fl.get("name") == flow_name:
                details = {
                    "feature": feature_name,
                    "flow": flow_name,
                    "description": fl.get("description"),
                    "files": fl.get("paths", []),
                    "file_count": len(fl.get("paths", [])),
                    "health": round(fl.get("health_score", 0)),
                    "bug_fix_ratio": round(fl.get("bug_fix_ratio", 0) * 100, 1),
                    "hotspot_files": fl.get("hotspot_files", []),
                }
                return {
                    "summary": (
                        f"{len(fl.get('paths', []))} file(s) in flow "
                        f"'{flow_name}' of feature '{feature_name}'."
                    ),
                    "details": _attach_response_metadata(details, len(fl.get("paths", []))),
                }
    return {
        "summary": f"Flow '{flow_name}' in feature '{feature_name}' not found.",
        "details": {"error": f"Flow '{flow_name}' in feature '{feature_name}' not found"},
    }


def get_repo_summary(scan: dict[str, Any], args: dict[str, Any],
                     runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """High-level stats about the repo: features, commits, health, risk.

    Returns aggregated metrics without file-level detail.
    """
    features = _features(scan)
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
    details = {
        "repo_path": scan.get("repo_path", ""),
        "remote_url": scan.get("remote_url", ""),
        "analyzed_at": scan.get("analyzed_at", ""),
        "date_range_days": scan.get("date_range_days", 0),
        "total_commits": scan.get("total_commits", 0),
        "total_features": len(features),
        "total_flows": sum(len(f.get("flows", [])) for f in features),
        "total_bug_fixes": total_bug_fixes,
        "avg_health_score": round(avg_health, 1),
        "avg_coverage_pct": round(avg_coverage, 1) if avg_coverage is not None else None,
        "features_at_risk": at_risk,
    }
    return {
        "summary": (
            f"{len(features)} feature(s), {details['total_flows']} flow(s), "
            f"avg health {round(avg_health, 1)}, {at_risk} at risk."
        ),
        "details": _attach_response_metadata(details, 0),
    }


def get_hotspots(scan: dict[str, Any], args: dict[str, Any],
                 runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get the riskiest features in the codebase.

    Returns features sorted by health score (worst first) with their hotspot
    files — the specific files accumulating the most bug fixes.

    Args:
        limit: Max features to return (default 5).
    """
    limit = int(args.get("limit", 5) or 5)
    risky = sorted(_features(scan), key=lambda f: f.get("health_score", 100))[:limit]
    result = []
    for f in risky:
        hotspot_files: list[str] = []
        for fl in f.get("flows", []):
            hotspot_files.extend(fl.get("hotspot_files", []))
        result.append({
            "name": f["name"],
            "description": f.get("description"),
            "health": round(f.get("health_score", 0)),
            "bug_fix_ratio": round(f.get("bug_fix_ratio", 0) * 100, 1),
            "bug_fixes": f.get("bug_fixes", 0),
            "commits": f.get("total_commits", 0),
            "coverage_pct": f.get("coverage_pct"),
            "hotspot_files": hotspot_files[:3],
            "owners": f.get("authors", [])[:3],
        })
    return {
        "summary": f"Top {len(result)} riskiest feature(s) by health score.",
        "details": _attach_response_metadata({"hotspots": result}, len(result)),
    }


def get_feature_owners(scan: dict[str, Any], args: dict[str, Any],
                       runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get the people who maintain a feature.

    Returns top contributors sorted by commit count. Also reports bus-factor
    risk if there's only one active owner.

    Args:
        feature_name: Exact feature name from ``list_features``.
    """
    feature_name = args.get("feature_name", "")
    f = _match_feature(scan, feature_name)
    if f:
        resolved = f.get("display_name") or f.get("name") or feature_name
        authors = f.get("authors", [])
        flow_bus_factors = [fl.get("bus_factor", 1) for fl in f.get("flows", [])]
        min_bus_factor = min(flow_bus_factors) if flow_bus_factors else len(authors) or 1
        details = {
            "feature": resolved,
            "owners": authors,
            "total_contributors": len(authors),
            "bus_factor": min_bus_factor,
            "at_risk": min_bus_factor == 1,
        }
        details = _attach_response_metadata(details, 0)
        return {
            "summary": (
                f"Feature '{resolved}' has {len(authors)} contributor(s), "
                f"bus factor {min_bus_factor}"
                f"{' (at risk)' if min_bus_factor == 1 else ''}."
            ),
            "details": details,
        }
    return {
        "summary": f"Feature '{feature_name}' not found.",
        "details": {"error": f"Feature '{feature_name}' not found"},
    }


def analyze_change_impact(scan: dict[str, Any], args: dict[str, Any],
                          runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Blast radius for a set of files you are about to change.

    Returns which features the changed files touch (by path overlap), total
    impact (structural blast × bug-fix history − coverage), co-changed-but-
    missing files from ``shared_attributions``, a risk level, and
    recommendations. Engine-free: reads precomputed scan fields only — no live
    git, no engine.

    Args:
        changed_files: Files being changed (repo-relative).
        repo_path: Accepted for compatibility; unused (data comes from the scan).
    """
    changed = set(args.get("changed_files") or [])
    features = _features(scan)

    affected: list[dict[str, Any]] = []
    co_changed_missing: set[str] = set()
    total_impact = 0.0

    for f in features:
        paths = set(f.get("paths", []))
        overlap = changed & paths
        if not overlap:
            continue
        bug_ratio = float(f.get("bug_fix_ratio", 0.0) or 0.0)
        coverage = float(f.get("coverage_pct", 0.0) or 0.0) / 100.0
        # structural blast (how many of the feature's files you touch),
        # amplified by bug history, dampened by test coverage
        impact = len(overlap) * (1.0 + bug_ratio) * (1.0 - 0.5 * coverage)
        total_impact += impact
        # co-changed-but-missing: paths the scan ties to this feature that you
        # did not include in the change set
        for sa in f.get("shared_attributions", []):
            p = sa.get("file_path") or sa.get("path")
            if p and p not in changed:
                co_changed_missing.add(p)
        affected.append({
            "feature": f.get("name"),
            "display_name": f.get("display_name", f.get("name")),
            "files_touched": sorted(overlap),
            "files_in_feature": len(paths),
            "bug_fix_ratio": round(bug_ratio, 3),
            "coverage_pct": round(float(f.get("coverage_pct", 0.0) or 0.0), 1),
            "health_score": round(float(f.get("health_score", 0.0) or 0.0)),
            "impact": round(impact, 2),
        })

    affected.sort(key=lambda a: a["impact"], reverse=True)

    if not affected:
        risk = "low"
    else:
        max_bug = max(a["bug_fix_ratio"] for a in affected)
        if total_impact >= 8 or max_bug >= 0.5:
            risk = "critical"
        elif total_impact >= 4 or max_bug >= 0.3:
            risk = "high"
        elif total_impact >= 1.5 or max_bug >= 0.15:
            risk = "medium"
        else:
            risk = "low"

    recommendations: list[str] = []
    if co_changed_missing:
        recommendations.append(
            f"{len(co_changed_missing)} file(s) historically co-change with the "
            "features you're editing but aren't in your change set — review them."
        )
    low_cov = [a["feature"] for a in affected if a["coverage_pct"] < 30]
    if low_cov:
        recommendations.append(
            "Low test coverage on: " + ", ".join(str(x) for x in low_cov[:5])
            + " — add tests before merging."
        )

    details = {
        "changed_files": sorted(changed),
        "risk_level": risk,
        "total_impact": round(total_impact, 2),
        "affected_features": affected,
        "co_changed_but_missing": sorted(co_changed_missing),
        "recommendations": recommendations,
        "method": "path-overlap blast radius from the feature map (engine-free)",
    }
    details = _attach_response_metadata(details, len(changed))
    return {
        "summary": (
            f"{risk.upper()} risk: {len(affected)} feature(s) affected, "
            f"total impact {round(total_impact, 2)}."
        ),
        "details": details,
    }


def get_regression_risk(scan: dict[str, Any], args: dict[str, Any],
                        runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Quick check: how likely is this change to cause a regression?

    Returns a probability (0.0-1.0) based on how buggy the affected features
    have been historically — a fast go/no-go signal before merging.

    Args:
        changed_files: Files being changed (relative to repo root).
    """
    features = _features(scan)
    changed_set = set(args.get("changed_files") or [])

    affected = [f for f in features if set(f.get("paths", [])) & changed_set]

    if not affected:
        return {
            "summary": "Low regression risk: changed files don't belong to any tracked feature.",
            "details": _attach_response_metadata({
                "regression_probability": 0.0,
                "risk_level": "low",
                "reason": "Changed files don't belong to any tracked feature.",
            }, 0),
        }

    total_weight = 0.0
    weighted_ratio = 0.0
    for f in affected:
        overlap = len(changed_set & set(f.get("paths", [])))
        weighted_ratio += f.get("bug_fix_ratio", 0.0) * overlap
        total_weight += overlap

    prob = round(weighted_ratio / total_weight, 2) if total_weight else 0.0

    if prob >= 0.6:
        risk = "critical"
    elif prob >= 0.4:
        risk = "high"
    elif prob >= 0.2:
        risk = "medium"
    else:
        risk = "low"

    details = {
        "regression_probability": prob,
        "risk_level": risk,
        "affected_features": [
            {"name": f["name"], "health": round(f.get("health_score", 0))}
            for f in affected
        ],
        "reason": (
            f"{round(prob * 100)}% of historical changes to these features "
            f"resulted in bug fixes."
        ),
    }
    details = _attach_response_metadata(details, 0)
    return {
        "summary": f"{risk.upper()} regression risk ({round(prob * 100)}% historical bug-fix rate).",
        "details": details,
    }


def find_symbols_in_flow(scan: dict[str, Any], args: dict[str, Any],
                         runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get precise symbols (functions, classes) that belong to a flow.

    Returns symbols grouped by file so the AI agent can read only the relevant
    functions instead of the full file. Falls back to full file paths when
    symbol-level attribution is unavailable.

    Args:
        feature_name: Parent feature name (from list_features).
        flow_name: Flow name (from find_feature or get_flow_files).
    """
    feature_name = args.get("feature_name", "")
    flow_name = args.get("flow_name", "")
    for f in _features(scan):
        if f.get("name") != feature_name:
            continue
        for fl in f.get("flows", []):
            if fl.get("name") != flow_name:
                continue

            attributions = fl.get("symbol_attributions", [])
            if attributions:
                remote = scan.get("remote_url", "")
                details = {
                    "feature": feature_name,
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
                    "hint": "Read only the symbols listed. Use deeplinks for direct GitHub navigation. Fall back to fallback_files when full context is needed.",
                }
                details = _attach_response_metadata(details, len(details["attributions"]))
                return {
                    "summary": (
                        f"Symbol-level attribution for flow '{flow_name}' "
                        f"({len(details['attributions'])} file(s))."
                    ),
                    "details": details,
                }

            # Fallback when --symbols wasn't enabled at scan time
            details = {
                "feature": feature_name,
                "flow": flow_name,
                "precision": "file-level",
                "attributions": [],
                "fallback_files": fl.get("paths", []),
                "hint": (
                    "Symbol-level attribution not available for this scan. "
                    "Re-run with `faultlines analyze . --llm --flows --symbols` "
                    "for precise function-level context."
                ),
            }
            details = _attach_response_metadata(details, len(fl.get("paths", [])))
            return {
                "summary": (
                    f"File-level fallback for flow '{flow_name}' "
                    "(no symbol attribution in this scan)."
                ),
                "details": details,
            }

    return {
        "summary": f"Flow '{flow_name}' in feature '{feature_name}' not found.",
        "details": {"error": f"Flow '{flow_name}' in feature '{feature_name}' not found"},
    }


def find_symbols_for_feature(scan: dict[str, Any], args: dict[str, Any],
                             runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Get the feature's shared symbols (types, interfaces, enums).

    Returns types and interfaces shared across all flows in the feature —
    the contracts/models an AI agent needs to understand the feature's data
    shape.

    Args:
        feature_name: Feature name from list_features.
    """
    feature_name = args.get("feature_name", "")
    for f in _features(scan):
        if f.get("name") != feature_name:
            continue

        shared = f.get("shared_attributions", [])
        remote = scan.get("remote_url", "")
        details = {
            "feature": feature_name,
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
        }
        details = _attach_response_metadata(details, len(details["shared_symbols"]))
        return {
            "summary": (
                f"{len(details['shared_symbols'])} shared-symbol file(s) "
                f"for feature '{feature_name}'."
            ),
            "details": details,
        }

    return {
        "summary": f"Feature '{feature_name}' not found.",
        "details": {"error": f"Feature '{feature_name}' not found"},
    }


def get_feature_errors(scan: dict[str, Any], args: dict[str, Any],
                       runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Production errors (Sentry) mapped to a feature.

    When the hosted dashboard supplies ``runtime["errors"]`` (already fetched
    from the org's Sentry integration), this maps those errors to the feature
    by path overlap. The standalone local package has no hosted connection, so
    with no runtime it returns a graceful ``{available: False, ...}`` result —
    the tool stays REGISTERED so the toolkit is identical across modes.

    Args:
        feature_name: Feature name from ``list_features``.
        window: Lookback window (e.g. "24h", "14d"). Hosted-only.
    """
    feature_name = args.get("feature_name", "")
    window = args.get("window", "24h")
    f = _match_feature(scan, feature_name)
    paths = f.get("paths", []) if f else []

    errors = (runtime or {}).get("errors") if runtime else None
    if errors is None:
        return {
            "summary": f"Runtime errors unavailable offline for '{feature_name}'.",
            "details": {
                "available": False,
                "reason": (
                    "Runtime errors need a hosted connection with a Sentry "
                    "integration. The standalone local MCP serves only the static "
                    "feature map. Connect this repo at https://faultlines.dev to "
                    "see production errors mapped to this feature."
                ),
                "feature": feature_name,
                "window": window,
                "paths": paths,
            },
        }

    path_set = set(paths)
    matched = [
        e for e in errors
        if (e.get("path") or e.get("file_path") or e.get("path_index")) in path_set
        or any(p in path_set for p in (e.get("paths") or []))
    ]
    total = sum(int(e.get("count", 1) or 1) for e in matched)
    return {
        "summary": (
            f"{len(matched)} error group(s) ({total} event(s)) mapped to "
            f"feature '{feature_name}' over {window}."
        ),
        "details": {
            "available": True,
            "feature": feature_name,
            "window": window,
            "error_groups": matched,
            "total_events": total,
            "paths": paths,
        },
    }


def get_feature_pageviews(scan: dict[str, Any], args: dict[str, Any],
                          runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    """Product usage / pageviews (PostHog) for a feature.

    When the hosted dashboard supplies ``runtime["pageviews"]`` (already
    fetched from the org's PostHog integration), this maps those events to the
    feature by path overlap. With no runtime it returns a graceful
    ``{available: False, ...}`` result — the tool stays REGISTERED so the
    toolkit is identical across modes.

    Args:
        feature_name: Feature name from ``list_features``.
        window: Lookback window (e.g. "24h", "14d"). Hosted-only.
    """
    feature_name = args.get("feature_name", "")
    window = args.get("window", "24h")
    f = _match_feature(scan, feature_name)
    paths = f.get("paths", []) if f else []

    pageviews = (runtime or {}).get("pageviews") if runtime else None
    if pageviews is None:
        return {
            "summary": f"Usage metrics unavailable offline for '{feature_name}'.",
            "details": {
                "available": False,
                "reason": (
                    "Usage metrics need a hosted connection with a PostHog "
                    "integration. The standalone local MCP serves only the static "
                    "feature map. Connect this repo at https://faultlines.dev to "
                    "see pageviews/traffic mapped to this feature."
                ),
                "feature": feature_name,
                "window": window,
                "paths": paths,
            },
        }

    path_set = set(paths)
    matched = [
        v for v in pageviews
        if (v.get("path") or v.get("file_path") or v.get("path_index")) in path_set
        or any(p in path_set for p in (v.get("paths") or []))
    ]
    total = sum(int(v.get("count", 0) or 0) for v in matched)
    return {
        "summary": (
            f"{total} pageview(s) across {len(matched)} surface(s) mapped to "
            f"feature '{feature_name}' over {window}."
        ),
        "details": {
            "available": True,
            "feature": feature_name,
            "window": window,
            "pageview_groups": matched,
            "total_pageviews": total,
            "paths": paths,
        },
    }


# ---------------------------------------------------------------------------
# Registry — the 13 tools, in spec order
# ---------------------------------------------------------------------------

# Reusable JSON-Schema fragments for the common args.
_FEATURE_NAME_PROP = {
    "feature_name": {"type": "string", "description": "Exact feature name from list_features."},
}
_FLOW_PROP = {
    "feature_name": {"type": "string", "description": "Parent feature name."},
    "flow_name": {"type": "string", "description": "Flow name from find_feature / get_flow_files."},
}
_CHANGED_FILES_PROP = {
    "changed_files": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Files being changed, relative to the repo root.",
    },
}
_WINDOW_PROP = {
    "window": {
        "type": "string",
        "default": "24h",
        "description": "Lookback window (e.g. \"24h\", \"14d\"). Hosted-only.",
    },
}
# Every tool accepts an OPTIONAL repo_slug so multi-repo callers (and the
# hosted layer) can disambiguate which scan to serve. Locally it selects the
# matching ~/.faultline/feature-map-<slug>*.json; the hosted layer resolves
# it before the scan ever reaches these pure functions.
_REPO_SLUG_PROP = {
    "repo_slug": {
        "type": "string",
        "description": "Repository slug; omit to use the session default.",
    },
}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """Build a minimal JSON Schema object for a tool's args.

    ``repo_slug`` is injected into EVERY tool's properties (always optional —
    never appended to ``required``) so agents can target a specific repo
    instead of hitting the hosted layer's repo-resolution error (-32097).
    """
    schema: dict[str, Any] = {"type": "object", "properties": {**properties, **_REPO_SLUG_PROP}}
    if required:
        schema["required"] = required
    return schema


# Each entry: name -> {fn, description, inputSchema}. ``description`` is the
# first paragraph of the pure fn's docstring; the schema mirrors the args the
# fn reads. Both deployment modes iterate this for tools/list and dispatch.
TOOLS: dict[str, dict[str, Any]] = {
    "list_features": {
        "fn": list_features,
        "description": "List all features detected in the codebase with health scores, sorted by risk (worst first).",
        "inputSchema": _schema({}),
    },
    "find_feature": {
        "fn": find_feature,
        "description": "Find a feature by semantic name, alias, label, or description. Returns files, health, ownership, flows.",
        "inputSchema": _schema(
            {"query": {"type": "string", "description": "Feature name, alias, or keyword."}},
            required=["query"],
        ),
    },
    "get_feature_files": {
        "fn": get_feature_files,
        "description": "Get the exact list of files that belong to a feature.",
        "inputSchema": _schema(dict(_FEATURE_NAME_PROP), required=["feature_name"]),
    },
    "get_flow_files": {
        "fn": get_flow_files,
        "description": "Get files belonging to a specific user-facing flow within a feature.",
        "inputSchema": _schema(dict(_FLOW_PROP), required=["feature_name", "flow_name"]),
    },
    "get_repo_summary": {
        "fn": get_repo_summary,
        "description": "High-level repo stats: features, flows, commits, average health, risk counts, scan timestamp.",
        "inputSchema": _schema({}),
    },
    "get_hotspots": {
        "fn": get_hotspots,
        "description": "Get the riskiest features (lowest health) with their hotspot files.",
        "inputSchema": _schema(
            {"limit": {"type": "integer", "default": 5, "description": "Max features to return."}}
        ),
    },
    "get_feature_owners": {
        "fn": get_feature_owners,
        "description": "Get the people who maintain a feature, with bus-factor risk.",
        "inputSchema": _schema(dict(_FEATURE_NAME_PROP), required=["feature_name"]),
    },
    "analyze_change_impact": {
        "fn": analyze_change_impact,
        "description": "Blast radius for a set of changed files: affected features/flows, total impact, co-changed-but-missing files, risk level, recommendations.",
        "inputSchema": _schema(
            {
                **_CHANGED_FILES_PROP,
                "repo_path": {
                    "type": "string",
                    "default": ".",
                    "description": "Accepted for compatibility; unused (data comes from the scan).",
                },
            },
            required=["changed_files"],
        ),
    },
    "get_regression_risk": {
        "fn": get_regression_risk,
        "description": "Regression probability (0.0-1.0) and risk level from weighted bug-fix history of the touched features.",
        "inputSchema": _schema(dict(_CHANGED_FILES_PROP), required=["changed_files"]),
    },
    "find_symbols_in_flow": {
        "fn": find_symbols_in_flow,
        "description": "Precise symbols (functions/classes) per file for a flow, with line ranges + deeplinks; falls back to file paths.",
        "inputSchema": _schema(dict(_FLOW_PROP), required=["feature_name", "flow_name"]),
    },
    "find_symbols_for_feature": {
        "fn": find_symbols_for_feature,
        "description": "Shared symbols (types, interfaces, enums) aggregated across a feature's flows.",
        "inputSchema": _schema(dict(_FEATURE_NAME_PROP), required=["feature_name"]),
    },
    "get_feature_errors": {
        "fn": get_feature_errors,
        "description": "Production errors (Sentry) mapped to a feature. Uses runtime.errors when supplied by the hosted proxy; else graceful unavailable.",
        "inputSchema": _schema(
            {**_FEATURE_NAME_PROP, **_WINDOW_PROP}, required=["feature_name"]
        ),
    },
    "get_feature_pageviews": {
        "fn": get_feature_pageviews,
        "description": "Product usage / pageviews (PostHog) for a feature. Uses runtime.pageviews when supplied by the hosted proxy; else graceful unavailable.",
        "inputSchema": _schema(
            {**_FEATURE_NAME_PROP, **_WINDOW_PROP}, required=["feature_name"]
        ),
    },
}


def list_tool_specs() -> list[dict[str, Any]]:
    """Return ``[{name, description, inputSchema}, ...]`` for all 13 tools.

    Used by both deployment modes to answer ``tools/list`` / ``GET /tools``
    without leaking the callable.
    """
    return [
        {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
        for name, spec in TOOLS.items()
    ]


def call_tool(
    name: str,
    scan: dict[str, Any],
    args: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch a tool by name through the registry.

    Raises ``KeyError`` if the tool name is unknown — callers map that to a
    4xx / structured error.
    """
    fn: Callable[..., dict[str, Any]] = TOOLS[name]["fn"]
    return fn(scan, args or {}, runtime)
