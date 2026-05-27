"""GoRouterExtractor — Go HTTP router registrations → anchors.

Parses ``.go`` files for HTTP route registration calls across the
common Go router ecosystems: chi, gin, echo, fiber, net/http
(``http.NewServeMux`` + ``HandleFunc``), and julienschmidt
``httprouter``.

We use REGEX deliberately — not Go AST parsing. The Go AST requires
a Go toolchain installed on the user machine; we can't assume that.
Regex is sufficient at the granularity we need: one anchor per
discovered route path. False positives cost ≤ one extra anchor (which
Stage 2 reconciliation handles) and the regex set is calibrated
against real chi/gin/echo source trees in the corpus.

Patterns live in ``eval/stacks/go-http-router.yaml`` — adding a new
router is a YAML edit, never a Python edit (per ``stack-pattern-library``
skill). This file just compiles those patterns at construction time.

Activation gate: the extractor only fires when the auditor or Stage 0
classified the repo as Go. On a non-Go repo (rust-workspace,
python-library, next-app-router) the extractor returns ``[]`` after a
cheap stack check — no .go files are read because there shouldn't be
any to begin with, but we short-circuit explicitly so a stray
``vendor/foo.go`` in a JS repo doesn't poison the result.

No LLM. No network. Pure file-system scan + regex.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── YAML config loader ─────────────────────────────────────────────────────


def _load_config() -> dict:
    """Load the go-http-router YAML from the packaged data tree (hermetic)."""
    return load_stack_yaml("go-http-router")


# Compiled-regex cache. Keyed by id(config) so YAML reloads in tests
# don't reuse stale compiled patterns.
_COMPILED_CACHE: dict[int, tuple[
    tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...],
    tuple[str, ...],
    tuple[str, ...],
    dict[str, float],
]] = {}


def _compile(config: dict) -> tuple[
    tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...],
    tuple[str, ...],
    tuple[str, ...],
    dict[str, float],
]:
    """Compile router regex pairs + extract path excludes + confidence map.

    Returns ``(routers, excludes, exclude_suffixes, confidence_map)``.
    """
    key = id(config)
    if key in _COMPILED_CACHE:
        return _COMPILED_CACHE[key]

    routers_raw = config.get("router_patterns") or {}
    routers: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = []
    for router_name, block in routers_raw.items():
        if not isinstance(block, dict):
            continue
        ctor = block.get("router_constructor")
        call = block.get("route_call")
        if not isinstance(ctor, str) or not isinstance(call, str):
            continue
        try:
            ctor_re = re.compile(ctor)
            call_re = re.compile(call)
        except re.error as exc:
            logger.warning(
                "go-http-router pattern compile failed for %s: %s",
                router_name, exc,
            )
            continue
        routers.append((router_name, ctor_re, call_re))

    excludes = tuple(
        str(p) for p in (config.get("excludes") or []) if isinstance(p, str)
    )
    exclude_suffixes = tuple(
        str(s) for s in (config.get("exclude_suffixes") or [])
        if isinstance(s, str)
    )
    conf_raw = config.get("confidence") or {}
    confidence = {
        "with_constructor_in_file": float(
            conf_raw.get("with_constructor_in_file", 0.9),
        ),
        "without_constructor_in_file": float(
            conf_raw.get("without_constructor_in_file", 0.7),
        ),
    }

    out = (tuple(routers), excludes, exclude_suffixes, confidence)
    _COMPILED_CACHE[key] = out
    return out


# ── Activation gate ────────────────────────────────────────────────────────


def _is_go_repo(ctx: "ScanContext") -> bool:
    """``True`` if any signal indicates this repo is Go-shaped."""
    audited = (ctx.audited_stack or "").lower()
    if audited.startswith("go-") or audited == "go":
        return True
    if (ctx.stack or "").lower() == "go":
        return True
    secondaries = tuple(s.lower() for s in (ctx.secondary_stacks or ()))
    if "go" in secondaries:
        return True
    # ``go-server``, ``go-library``, etc. as secondary
    if any(s.startswith("go-") for s in secondaries):
        return True
    return False


# ── Path → slug helper ─────────────────────────────────────────────────────


def _route_to_slug(route: str) -> str:
    """Convert a Go route path to a kebab-slug.

    Examples:
        ``"/"``                 → ``"root"``
        ``"/users"``            → ``"users"``
        ``"/users/{id}/posts"`` → ``"users-id-posts"``
        ``"/api/v1/orders/:id"``→ ``"api-v1-orders-id"``
        ``"/*"``                → ``"root"``  (wildcard-only)
    """
    if not route or route in ("/", "/*", "*"):
        return "root"
    # Strip braces / colons used for path params.
    stripped = (
        route.replace("{", " ")
        .replace("}", " ")
        .replace(":", " ")
        .replace("*", " ")
    )
    slug = slugify(stripped)
    return slug or "root"


def _is_excluded(path: str, prefixes: tuple[str, ...],
                 suffixes: tuple[str, ...]) -> bool:
    """``True`` if ``path`` matches any prefix or suffix exclude."""
    p = posix(path)
    for prefix in prefixes:
        if prefix and (p.startswith(prefix) or f"/{prefix}" in f"/{p}"):
            return True
    for suffix in suffixes:
        if suffix and p.endswith(suffix):
            return True
    return False


# ── Extractor ──────────────────────────────────────────────────────────────


class GoRouterExtractor:
    """Go HTTP router parser. Emits one anchor per discovered route.

    Implements the :class:`AnchorExtractor` Protocol.
    """

    name = "go-router"

    def __init__(self, config: dict | None = None) -> None:
        # ``config=None`` → load from YAML. Tests may pass a literal
        # dict to keep the unit hermetic.
        self._config = config if config is not None else _load_config()

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not _is_go_repo(ctx):
            return []

        routers, excludes, exclude_suffixes, confidence = _compile(self._config)
        if not routers:
            return []

        # slug → {paths_set, with_ctor_flag, rationale_set}
        anchors: dict[str, dict] = defaultdict(
            lambda: {"paths": set(), "with_ctor": False, "rationales": set()},
        )

        for rel_path in ctx.tracked_files:
            if not rel_path.endswith(".go"):
                continue
            if _is_excluded(rel_path, excludes, exclude_suffixes):
                continue

            abs_path = ctx.repo_path / rel_path
            text = read_text(abs_path)
            if not text:
                continue

            for router_name, ctor_re, call_re in routers:
                has_ctor = bool(ctor_re.search(text))
                for match in call_re.finditer(text):
                    # ``match.group(2)`` is the route path; ``group(1)``
                    # is the method/verb (informational).
                    route = match.group(2) if match.lastindex and match.lastindex >= 2 else ""
                    if not route:
                        continue
                    slug = _route_to_slug(route)
                    if not slug:
                        continue
                    bucket = anchors[slug]
                    bucket["paths"].add(posix(rel_path))
                    if has_ctor:
                        bucket["with_ctor"] = True
                    bucket["rationales"].add(
                        f"{router_name}:{route}",
                    )

        out: list[AnchorCandidate] = []
        for slug, data in anchors.items():
            paths = tuple(sorted(data["paths"]))
            conf = (
                confidence["with_constructor_in_file"]
                if data["with_ctor"]
                else confidence["without_constructor_in_file"]
            )
            rationale_sample = ", ".join(sorted(data["rationales"])[:5])
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=paths,
                    source=self.name,
                    confidence_self=conf,
                    rationale=f"go-router routes: {rationale_sample}",
                ),
            )
        return out


__all__ = ["GoRouterExtractor"]
