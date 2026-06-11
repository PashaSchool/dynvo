"""Shared helpers for Stage 1 extractors.

Kept private (``_util``) — these helpers are not part of the public
extractor contract. Anything an external extractor needs should be
imported from :mod:`faultline.pipeline_v2.extractors.base` instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── slug helpers ────────────────────────────────────────────────────────────

_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SLUG_TRIM_DASH = re.compile(r"^-+|-+$")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def slugify(value: str) -> str:
    """Turn an arbitrary string into a kebab-case slug.

    Splits camelCase (``UserAuth`` → ``user-auth``) and underscores,
    lowercases, collapses runs of non-alphanumerics into single dashes.
    Empty input yields ``""`` (Stage 5 Fix A drops empty names).
    """
    if not value:
        return ""
    # split CamelCase into pieces first so ``UserAPI`` → ``User-API`` →
    # ``user-api`` (rather than ``userapi``)
    spaced = _CAMEL_BOUNDARY.sub("-", value)
    lowered = spaced.lower()
    dashed = _SLUG_NON_ALNUM.sub("-", lowered)
    return _SLUG_TRIM_DASH.sub("", dashed)


# Universal noise tokens — common framework/scaffolding words that, on
# their own, are NEVER a useful feature name. Filtered by extractors
# that derive names from path segments. Kept small and stack-agnostic;
# stack-specific cleaning happens in Stage 5 post-process.
_NOISE_TOKENS = frozenset({
    "src", "app", "pages", "routes", "api", "lib", "libs",
    "components", "component", "shared", "common", "utils", "util",
    "helpers", "helper", "hooks", "config", "configs",
    "controllers", "controller", "models", "model", "views", "view",
    "services", "service", "handlers", "handler", "middleware",
    "internal", "core", "main", "index", "page", "layout", "route",
    "schemas", "schema", "types", "type",
    "public", "private",
})


def is_noise(token: str) -> bool:
    """``True`` if ``token`` is a scaffolding/framework word on its own."""
    return token.lower() in _NOISE_TOKENS


# ── activation-gate helpers ─────────────────────────────────────────────────
#
# Most Stage 1 extractors gate on the same three context fields — the
# Stage 0.5 auditor's primary tag (``ctx.audited_stack``), the Stage 0
# heuristic tag (``ctx.stack``), and the auditor's secondary tags
# (``ctx.secondary_stacks``). These helpers centralise the exact-match
# checks; prefix-style checks (``startswith("go-")``) stay local to the
# extractors that need them.


def is_audited_stack(ctx: "ScanContext", stack: str) -> bool:
    """``True`` iff the auditor declared ``stack`` (primary OR secondary).

    Exact match only — does not consult the Stage 0 heuristic
    ``ctx.stack`` tag. Use :func:`is_any_stack` when the Stage 0 tag
    should also count.
    """
    wanted = stack.lower()
    if (ctx.audited_stack or "").lower() == wanted:
        return True
    return any(
        s.lower() == wanted for s in (ctx.secondary_stacks or ())
    )


def is_any_stack(ctx: "ScanContext", *stacks: str) -> bool:
    """``True`` iff ANY of ``stacks`` matches the audited primary tag,
    the Stage 0 heuristic tag, or an audited secondary tag (exact,
    case-insensitive)."""
    wanted = {s.lower() for s in stacks}
    if (ctx.audited_stack or "").lower() in wanted:
        return True
    if (ctx.stack or "").lower() in wanted:
        return True
    return any(
        s.lower() in wanted for s in (ctx.secondary_stacks or ())
    )


# ── manifest readers ────────────────────────────────────────────────────────

def read_json(path: Path) -> dict | list | None:
    """Read a JSON file or return ``None`` on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_text(path: Path) -> str | None:
    """Read a text file or return ``None`` on any error."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


# ── path helpers ────────────────────────────────────────────────────────────

def posix(p: str) -> str:
    """Normalise a path string to POSIX separators (``\\`` → ``/``)."""
    return p.replace("\\", "/")


def has_any_suffix(file_path: str, suffixes: tuple[str, ...]) -> bool:
    """``True`` if ``file_path`` ends with any of ``suffixes``."""
    lower = file_path.lower()
    return any(lower.endswith(s) for s in suffixes)


__all__ = [
    "slugify",
    "is_noise",
    "is_audited_stack",
    "is_any_stack",
    "read_json",
    "read_text",
    "posix",
    "has_any_suffix",
]
