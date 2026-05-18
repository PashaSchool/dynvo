"""Shared helpers for Stage 1 extractors.

Kept private (``_util``) — these helpers are not part of the public
extractor contract. Anything an external extractor needs should be
imported from :mod:`faultline.pipeline_v2.extractors.base` instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


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
    "read_json",
    "read_text",
    "posix",
    "has_any_suffix",
]
