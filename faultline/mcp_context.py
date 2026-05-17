"""Public surface for MCP tool modules.

Subpackages (``faultline.impact``, ``faultline.symbols``) import the
shared MCP instance, the loader, the resolver, and the freshness
helpers from here — never from ``faultline.mcp_server`` directly.
Anything starting with an underscore is private and may move.

Decisions:
- ``load_map()`` caches parsed JSON keyed by ``(path, mtime)`` so
  repeat calls during a single client session don't re-parse the
  whole file (cal.com: 282 features / ~1MB).
- ``resolve_feature()`` is the single feature-lookup contract. Two
  modes: ``"exact"`` for tools that take a name the caller already
  picked, ``"fuzzy"`` for free-text search.
- Tool responses no longer carry ``_savings_metadata``. Telemetry is
  recorded out-of-band via ``record_call()``.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("faultlines")

# Bump on any breaking shape change to a tool response. Consumers
# (dashboard, AI agents that store output) read this to decide
# whether they can deserialise. Add migration notes here.
SCHEMA_VERSION = "1.0"

_STALE_DAYS = 30


# ---------------------------------------------------------------------------
# Loader with mtime cache
# ---------------------------------------------------------------------------

_map_cache: dict[Path, tuple[float, dict[str, Any]]] = {}


def load_map() -> dict[str, Any]:
    """Load the most recent feature-map JSON.

    Cached by ``(path, mtime)`` — re-parses only when the file changes.
    Pure read: never triggers background refresh. For that, agents call
    the explicit ``refresh_feature_map`` tool (or set
    ``FAULTLINE_AUTO_REFRESH=1`` and call ``trigger_auto_refresh``
    yourself).

    Precedence:
        1. ``FAULTLINE_MAP_PATH`` (explicit path)
        2. Most recent ``~/.faultline/feature-map-*.json``
        3. Raises ``RuntimeError`` with install instructions.
    """
    path = _resolve_map_path()
    mtime = path.stat().st_mtime
    cached = _map_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    data = json.loads(path.read_text())
    _map_cache[path] = (mtime, data)
    return data


def trigger_auto_refresh() -> dict[str, Any]:
    """Kick off a background refresh of the current feature map.

    Non-blocking. Returns immediately. The current map is still served
    until the refresh completes.
    """
    path = _resolve_map_path()
    mtime = path.stat().st_mtime
    cached = _map_cache.get(path)
    if cached and cached[0] == mtime:
        data = cached[1]
    else:
        data = json.loads(path.read_text())
        _map_cache[path] = (mtime, data)

    try:
        from faultline.cache.auto_refresh import maybe_trigger_refresh
        triggered = maybe_trigger_refresh(path, data)
    except Exception as exc:  # noqa: BLE001 — best-effort, never raise
        return {
            "triggered": False,
            "reason": f"refresh helper failed: {exc.__class__.__name__}",
            "map_path": str(path),
        }
    return {
        "triggered": bool(triggered),
        "map_path": str(path),
        "scanned_sha": data.get("last_scanned_sha", "")[:8] or None,
    }


def _resolve_map_path() -> Path:
    explicit = os.environ.get("FAULTLINE_MAP_PATH")
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise RuntimeError(
                f"FAULTLINE_MAP_PATH={explicit} does not exist. "
                f"Run `faultlines analyze .` first."
            )
        return p

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
    return scans[-1]


# ---------------------------------------------------------------------------
# Unified feature lookup
# ---------------------------------------------------------------------------

def _haystacks(f: dict[str, Any], *, include_description: bool) -> list[str]:
    """Lowercase strings against which a query is compared."""
    out: list[str] = []
    for key in ("display_name", "name"):
        v = f.get(key)
        if isinstance(v, str):
            out.append(v.lower())
    for v in f.get("aliases") or []:
        if isinstance(v, str):
            out.append(v.lower())
    for lab in f.get("labels") or []:
        name = lab.get("name") if isinstance(lab, dict) else None
        if isinstance(name, str):
            out.append(name.lower())
    if include_description:
        desc = f.get("description")
        if isinstance(desc, str):
            out.append(desc.lower())
    return out


def resolve_feature(
    fm: dict[str, Any],
    query: str,
    *,
    mode: Literal["exact", "fuzzy"] = "exact",
) -> dict[str, Any] | None:
    """Single feature-lookup contract used by every name-based tool.

    Args:
        fm: Feature-map dict from ``load_map()``.
        query: Caller-supplied name / search term.
        mode:
          - ``"exact"``: case-insensitive equality on name / display_name /
            aliases / labels. Use when the caller already picked from
            ``list_features``.
          - ``"fuzzy"``: substring match across the same fields plus
            description. Use for free-text search (``find_feature``).
    """
    q = query.strip().lower()
    if not q:
        return None
    for f in fm.get("features", []):
        if mode == "exact":
            if q in _haystacks(f, include_description=False):
                return f
        else:
            if any(q in hay for hay in _haystacks(f, include_description=True)):
                return f
    return None


def error_payload(message: str, **extra: Any) -> dict[str, Any]:
    """Uniform error envelope with schema version.

    Returns ``{"_schema_version": ..., "error": message, **extra}`` so
    every error response from any tool has the same shape.
    """
    return {"_schema_version": SCHEMA_VERSION, "error": message, **extra}


def fuzzy_feature_suggestions(
    fm: dict[str, Any], query: str, *, limit: int = 5
) -> list[str]:
    """Top-N display names closest to a failed query.

    Replaces the previous ``error.available = <every feature name>``
    pattern which dumped 282 names on cal.com.
    """
    names = [
        f.get("display_name") or f.get("name") or ""
        for f in fm.get("features", [])
    ]
    names = [n for n in names if n]
    return get_close_matches(query, names, n=limit, cutoff=0.0)


def feature_display_name(f: dict[str, Any]) -> str:
    return f.get("display_name") or f.get("name") or ""


# ---------------------------------------------------------------------------
# Freshness signals
# ---------------------------------------------------------------------------

def inject_warning(result: dict[str, Any], fm: dict[str, Any]) -> dict[str, Any]:
    """Append stale_warning + freshness + schema_version to a tool result."""
    result.setdefault("_schema_version", SCHEMA_VERSION)
    warning = _stale_warning(fm)
    if warning:
        result["stale_warning"] = warning

    freshness = _git_freshness(fm)
    if freshness is not None:
        result["freshness"] = freshness
        if freshness.get("is_stale"):
            behind = freshness.get("commits_behind", 0)
            result["stale_warning"] = (
                f"Feature map is {behind} commit(s) behind HEAD. "
                f"Call the `refresh_feature_map` tool for an LLM-free "
                f"incremental update, or run `faultlines refresh` in the shell."
            )
    return result


def _stale_warning(fm: dict[str, Any]) -> str | None:
    analyzed_at = fm.get("analyzed_at")
    if not analyzed_at:
        return None
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


def _git_freshness(fm: dict[str, Any]) -> dict[str, Any] | None:
    scanned_sha = fm.get("last_scanned_sha", "")
    repo_path = fm.get("repo_path", "")
    if not scanned_sha or not repo_path:
        return None

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


# ---------------------------------------------------------------------------
# Telemetry — fire-and-forget; never returns anything that goes in payload
# ---------------------------------------------------------------------------

def record_call(
    tool_name: str,
    *,
    query_arg: str | None = None,
    files_returned: int = 0,
) -> None:
    """Record a tool invocation for cloud telemetry.

    Replaces the per-response ``_savings_metadata`` payload pattern.
    No-op unless ``FAULTLINE_API_KEY`` is set. Never raises.
    """
    if not os.environ.get("FAULTLINE_API_KEY"):
        return
    try:
        from faultline.cloud.event_buffer import record_mcp_event
        record_mcp_event(
            tool_name=tool_name,
            query_arg=query_arg,
            files_returned=files_returned,
            tokens_saved=0,
        )
    except Exception:
        pass


__all__ = [
    "SCHEMA_VERSION",
    "mcp",
    "load_map",
    "trigger_auto_refresh",
    "resolve_feature",
    "fuzzy_feature_suggestions",
    "feature_display_name",
    "inject_warning",
    "record_call",
    "error_payload",
]
