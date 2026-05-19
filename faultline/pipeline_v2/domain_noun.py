"""Structural domain-noun extractor for Stage 6.5 product clusterer.

Sprint B3.1 — refine workspace cluster labels by voting on the most
common domain token across a feature's paths.

The Stage 6.5 clusterer (Sprint B3) groups developer features by
workspace prefix (apps/web, packages/cli, …) but labels them with
the workspace name — "Webapp", "Packages", "Cli V3" — which does not
match marketing-grounded truth corpora ("Documents", "Data Room",
"E-Signature").

This module extracts a DOMAIN noun from the structural path tokens
under the workspace prefix. The token is voted across paths
(60% threshold). The choice is deterministic, scale-invariant
(a ratio, not a count), and emerges from the code itself — no
hardcoded domain list, no LLM, no README.

Token sources (highest signal first):
  1. Next.js route groups — ``app/(documents)/…`` → ``documents``.
     Explicit author grouping by Next convention; conf 0.85.
  2. First non-generic directory segment after the workspace prefix.
     Skips scaffolding dirs (api/, lib/, components/, …); conf 0.70.
  3. Filename stem fallback — only if no directory token won.
     ``dataroom-card.tsx`` → ``dataroom``; conf 0.50.

Generic tokens that NEVER win — they are universal scaffolding
vocabulary (same for every repo, not domain nouns).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass


# ── Universal scaffolding vocabulary ─────────────────────────────────────


# Tokens that are scaffolding, not domain nouns. Same set for every
# repo — per the no-magic-tuning rule we MUST NOT customise this per
# repo or per stack.
_GENERIC_TOKENS: frozenset[str] = frozenset({
    # Layout / routing scaffolding
    "api", "app", "src", "lib", "utils", "helpers",
    "components", "hooks", "types", "common", "shared", "core",
    "internal", "public", "dashboard", "server", "client",
    "pages", "layouts", "middleware", "config", "services",
    # Route-group scaffolding (no domain noun — wrap auth/marketing/dashboard)
    "auth", "marketing", "main", "site", "root", "default",
    # Misc framework conventions
    "fonts", "static", "assets", "styles", "css", "scripts",
    # Test scaffolding
    "test", "tests", "__tests__", "spec", "specs", "e2e",
})


# Strip Next.js route group parentheses + leading dynamic-segment marks.
# ``(documents)`` → ``documents``; ``[id]`` → empty (skipped).
_ROUTE_GROUP_RE = re.compile(r"^\(([^)]+)\)$")
_DYNAMIC_SEG_RE = re.compile(r"^\[.*\]$")


@dataclass(frozen=True)
class DomainNoun:
    """One extracted domain label with its provenance."""

    label: str
    """TitleCase, kebab-aware. ``data-room`` → ``"Data Room"``."""

    token: str
    """Raw path token. ``dataroom``, ``data-room``, ``e-signature``."""

    signal_paths: tuple[str, ...]
    """Paths whose tokens fired for this noun (audit trail)."""

    confidence: float
    """0..1 — 0.85 route-group, 0.70 first-non-generic, 0.50 filename."""


# ── Pure helpers (no I/O) ────────────────────────────────────────────────


def _normalize_path(path: str) -> str:
    """Backslash → slash; strip leading ``./`` or ``/``."""
    p = path.replace("\\", "/").lstrip("./").lstrip("/")
    return p


def _strip_workspace_prefix(path: str, workspace_prefix: str) -> str | None:
    """Return the path with workspace prefix removed, or None if it
    doesn't sit under that workspace at all.

    ``apps/web/(documents)/page.tsx`` with prefix ``apps/web`` →
    ``(documents)/page.tsx``.
    """
    p = _normalize_path(path)
    wp = workspace_prefix.strip("/")
    if not wp:
        return p
    if p == wp:
        return ""
    if p.startswith(wp + "/"):
        return p[len(wp) + 1:]
    return None


def _route_group_token(segment: str) -> str | None:
    """If ``segment`` is a Next.js route group ``(x)``, return ``x``."""
    m = _ROUTE_GROUP_RE.match(segment)
    if not m:
        return None
    inner = m.group(1).strip()
    if not inner or _DYNAMIC_SEG_RE.match(inner):
        return None
    return inner


def _is_dynamic_segment(segment: str) -> bool:
    """Skip Next.js dynamic segments ``[id]`` / ``[...slug]``."""
    return bool(_DYNAMIC_SEG_RE.match(segment))


def _filename_stem(segment: str) -> str | None:
    """``dataroom-card.tsx`` → ``dataroom-card``. Strips extension.

    Returns None for index files / config files (no domain signal).
    """
    if "." not in segment:
        # No extension — probably a dir, caller decides.
        return None
    stem = segment.rsplit(".", 1)[0]
    if not stem or stem in {"index", "page", "layout", "route", "default", "loading", "error", "not-found"}:
        return None
    return stem


def _extract_candidate(
    relpath: str,
) -> tuple[str, float] | None:
    """For one relative path, find its best candidate token.

    Returns ``(token, confidence)`` or ``None`` if the path is all
    generic / dynamic.

    Priority:
      1. First route-group segment (conf 0.85)
      2. First non-generic, non-dynamic directory segment (conf 0.70)
      3. Filename stem (conf 0.50)
    """
    parts = [seg for seg in relpath.split("/") if seg]
    if not parts:
        return None

    # Separate dir parts from the final filename (if any).
    if "." in parts[-1]:
        dir_parts = parts[:-1]
        file_part: str | None = parts[-1]
    else:
        dir_parts = parts
        file_part = None

    # Pass 1 — route groups anywhere in the dir chain.
    for seg in dir_parts:
        rg = _route_group_token(seg)
        if rg and rg.lower() not in _GENERIC_TOKENS:
            return rg.lower(), 0.85

    # Pass 2 — first non-generic, non-dynamic directory segment.
    for seg in dir_parts:
        if _is_dynamic_segment(seg):
            continue
        # If wrapped in route-group parens but token was generic
        # (e.g. "(auth)"), strip parens and re-check.
        candidate = seg
        m = _ROUTE_GROUP_RE.match(seg)
        if m:
            candidate = m.group(1)
        if candidate.lower() in _GENERIC_TOKENS:
            continue
        if not candidate:
            continue
        return candidate.lower(), 0.70

    # Pass 3 — filename stem fallback.
    if file_part is not None:
        stem = _filename_stem(file_part)
        if stem and stem.lower() not in _GENERIC_TOKENS:
            return stem.lower(), 0.50

    return None


def _titleize_token(token: str) -> str:
    """``data-room`` → ``"Data Room"``; ``dataroom`` → ``"Dataroom"``.

    Kebab + snake split into words; each word Capitalised.
    """
    cleaned = re.sub(r"[_\-]+", " ", token)
    cleaned = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cleaned)
    parts = [w for w in cleaned.split() if w]
    return " ".join(w.capitalize() for w in parts)


# ── Public API ───────────────────────────────────────────────────────────


# Vote threshold — winning token must appear in ≥60% of paths that
# produced ANY candidate. Scale-invariant ratio per no-magic-tuning rule.
_VOTE_THRESHOLD = 0.60


def extract_domain_noun(
    feature_paths: list[str],
    workspace_prefix: str,
) -> DomainNoun | None:
    """Extract the dominant domain noun from feature paths.

    Args:
        feature_paths: developer feature's paths, relative to repo root.
        workspace_prefix: workspace this feature was assigned to in
            Rule 1 (e.g. ``"apps/web"``). Paths NOT under this prefix
            are ignored — defensive against caller misuse.

    Returns:
        ``DomainNoun`` if a token wins the ≥60% vote, else ``None``.
    """
    if not feature_paths:
        return None

    # Per-path candidate extraction.
    # ``token_signal[token]`` collects paths that voted for each token,
    # plus the highest confidence seen for that token (route-group >
    # first-non-generic > filename).
    token_signal: dict[str, list[str]] = defaultdict(list)
    token_conf: dict[str, float] = {}
    paths_with_candidate = 0

    for p in feature_paths:
        rel = _strip_workspace_prefix(p, workspace_prefix)
        if rel is None:
            continue
        if rel == "":
            continue
        candidate = _extract_candidate(rel)
        if candidate is None:
            continue
        token, conf = candidate
        token_signal[token].append(p)
        token_conf[token] = max(token_conf.get(token, 0.0), conf)
        paths_with_candidate += 1

    if not token_signal or paths_with_candidate == 0:
        return None

    # Find the dominant token by vote count.
    counts = Counter({tok: len(paths) for tok, paths in token_signal.items()})
    winner, win_count = counts.most_common(1)[0]
    share = win_count / paths_with_candidate

    if share < _VOTE_THRESHOLD:
        return None

    return DomainNoun(
        label=_titleize_token(winner),
        token=winner,
        signal_paths=tuple(token_signal[winner]),
        confidence=token_conf[winner],
    )


__all__ = ["DomainNoun", "extract_domain_noun"]
