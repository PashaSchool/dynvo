"""Sibling-router / sibling-service collapse aggregator.

When a backend monolith ships dozens of sibling service / router /
controller folders that share a token prefix (e.g. infisical's
``backend/src/services/identity-*-auth``, ``secret-*``,
``certificate-*``, ``pki-*``), the bucketizer correctly produces
one feature per folder. That's accurate at the engineering grain
but wrong at the product grain — a product owner thinks of
"Machine Identities" not 12 separate features named after each
auth method.

This aggregator runs PRE-dedup and collapses such sibling families
into one feature per ``<prefix>`` family. Output naming is
deterministic and derived from the prefix token itself (no
hardcoded domains), so the rule is universal across stacks /
products.

## Rule (scale-invariant, no magic numbers per ``rule-no-magic-tuning``)

For each common parent directory ``D`` (computed as the longest
common ancestor of each feature's primary file paths):

  Group the features whose paths concentrate in ``D`` by their
  ``first-token`` (the segment before the first ``-`` in the
  feature name).

  A group qualifies for collapse when:
    1. ``len(group) >= MIN_SIBLINGS`` (default 4) — structural,
       not corpus-tuned. Repos with <4 sibling folders (chi,
       axios, small libs) never fire.
    2. ``distinct_tails(group) >= MIN_DISTINCT_TAILS`` (default 3) —
       guards against `<prefix>` repeated for unrelated reasons.
    3. Parent dir ``D`` matches a universal "service container"
       pattern: ends in ``services``, ``routes``, ``routers``,
       ``controllers``, ``handlers``, ``views``, ``modules``,
       ``packages``, or ``components``. This keeps the collapse
       targeted at architectural container dirs (universal across
       stacks) rather than ad-hoc folders.

  When qualified, collapse all members into a single feature named
  ``<Prefix> Management`` (e.g. ``identity`` → ``Identity Management``,
  ``pki`` → ``PKI Management``). The collapsed feature inherits the
  union of paths, flows, and descriptions.

## Why this is universal

- Small repos (``chi``, ``axios``, ``fastapi`` library) ship <4
  sibling folders per container → rule never fires.
- The prefix is derived from the feature's own name token. No
  ``"identity"`` / ``"secret"`` strings appear anywhere in this
  module — see ``rule-no-repo-specific-paths``.
- Container suffix set (``services``, ``routes``, ...) is a
  universal naming convention shared by NestJS, Express, Rails,
  Django, Spring, Phoenix, FastAPI, NextAuth, etc.
- The threshold pair (``MIN_SIBLINGS=4``, ``MIN_DISTINCT_TAILS=3``)
  is structural — a folder with 3 sibling files is engineering-grain
  granularity per ``rule-engineering-granularity-is-correct``; a
  folder with 12+ siblings sharing a prefix is over-split.

Complies with ``rule-cold-scan``: deterministic, no per-repo
state, no priors.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faultline.llm.sonnet_scanner import DeepScanResult

logger = logging.getLogger(__name__)


# ── Universal thresholds (no magic numbers per rule-no-magic-tuning)

# Minimum number of sibling features that share both parent dir and
# first-token prefix before we consider collapsing. 4 is the floor
# below which engineering-grain granularity is preferred (small libs
# with 1-3 plugins, middlewares, or routes are not over-split).
MIN_SIBLINGS = 4

# Minimum number of distinct tail tokens after the prefix. Guards
# against accidental same-prefix groupings (e.g. 5 files all named
# ``user-list-X.ts`` would have tail = ``list-X`` — not a family).
MIN_DISTINCT_TAILS = 3

# Universal container directories — folders whose conventional role
# is to hold a list of feature modules. Recognised across stacks:
#   - NestJS / Express / Fastify: routes, routers, controllers, services, modules
#   - Rails / Phoenix: controllers, services, views
#   - Django: views
#   - FastAPI: routers, services
#   - Generic monorepos: packages, components
#
# Per rule-no-repo-specific-paths these are CONVENTIONS, not
# repo-specific names. Any repo following one of these conventions
# benefits; repos using other folder names are unaffected.
_CONTAINER_DIR_NAMES = frozenset({
    "services",
    "routes",
    "routers",
    "controllers",
    "handlers",
    "views",
    "modules",
    "packages",
    "components",
})


@dataclass
class CollapseStats:
    families_collapsed: int = 0
    features_before: int = 0
    features_after: int = 0
    siblings_removed: int = 0
    collapsed_names: list[str] = field(default_factory=list)


# ── Tokenisation helpers ─────────────────────────────────────────────


_TOKEN_SPLIT = re.compile(r"[-_/\s]+")


def _first_token(name: str) -> str:
    """Return the segment before the first ``-`` / ``_`` / ``/``.

    Used as the family prefix. ``identity-kubernetes-auth`` →
    ``identity``. ``pki-acme`` → ``pki``. A single-token feature
    returns the whole name (excluded from collapse anyway because
    it has no tail).
    """
    parts = _TOKEN_SPLIT.split(name.strip().lower(), maxsplit=1)
    return parts[0] if parts else name.strip().lower()


def _tail(name: str) -> str:
    """Return everything after the first token.

    ``identity-kubernetes-auth`` → ``kubernetes-auth``.
    ``identity`` → ``""`` (empty — flags ``no tail``).
    """
    parts = _TOKEN_SPLIT.split(name.strip().lower(), maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _container_dir_and_subfolder(
    paths: Iterable[str], name: str,
) -> tuple[str, str] | None:
    """Identify (container_key, sibling_folder) for a feature.

    A feature qualifies as a "sibling folder" candidate only when at
    least one of its paths matches the pattern
    ``.../<container>/<sibling-folder>/...`` where ``<sibling-folder>``
    starts with the feature's first-token prefix. This filters out
    features whose name happens to share a prefix but whose paths
    live NESTED (e.g. ``services/app-connection/external-infisical/...``
    — that's a child of app-connection, not a sibling of other
    external-* services).

    Returns ``(container_key, sibling_folder)`` where:
      - ``container_key`` is ``<top-segment>/<container-name>`` so
        ``backend/src/services`` and ``backend/src/ee/services``
        normalise to the same key ``backend/services``.
      - ``sibling_folder`` is the directory segment directly under
        the container (the one that should start with the feature's
        prefix token).

    Returns ``None`` when the feature isn't a top-level sibling
    folder anywhere — it's then ineligible for collapse.
    """
    first_token = _first_token(name)
    for path in paths:
        segments = path.split("/")
        for i, seg in enumerate(segments[1:-1], start=1):
            if seg in _CONTAINER_DIR_NAMES:
                # Segment immediately after the container is the
                # sibling-folder candidate. Must exist and start
                # with the feature's prefix token.
                sibling = segments[i + 1]
                sibling_first_token = _TOKEN_SPLIT.split(
                    sibling.lower(), maxsplit=1,
                )[0]
                if sibling_first_token == first_token:
                    return f"{segments[0]}/{seg}", sibling
    return None


# Back-compat alias used by tests / introspection.
def _primary_container_dir(paths: Iterable[str]) -> str | None:
    """Return only the container key, or ``None``.

    Thin shim around :func:`_container_dir_and_subfolder` that
    drops the sibling-folder component. Caller must pass a name
    via the newer API to get full sibling verification.
    """
    for path in paths:
        segments = path.split("/")
        for seg in segments[1:]:
            if seg in _CONTAINER_DIR_NAMES:
                return f"{segments[0]}/{seg}"
    return None


def _display_name(prefix: str) -> str:
    """Deterministic display name for a collapsed family.

    Single-letter / acronym-shaped prefixes (all caps in the
    original token, e.g. ``pki``, ``sso``, ``api``) are uppercased.
    Otherwise title-cased.
    """
    # Heuristic: short prefixes ≤ 3 chars are usually acronyms.
    if len(prefix) <= 3:
        return f"{prefix.upper()} Management"
    return f"{prefix.title()} Management"


# ── Public entrypoint ────────────────────────────────────────────────


def collapse_sibling_router_families(
    result: DeepScanResult,
    *,
    min_siblings: int = MIN_SIBLINGS,
    min_distinct_tails: int = MIN_DISTINCT_TAILS,
) -> CollapseStats:
    """Collapse over-split sibling families in-place on ``result``.

    Returns a :class:`CollapseStats` describing what happened. Does
    not raise; on empty input or no qualifying group, returns stats
    with ``families_collapsed=0`` and leaves ``result`` untouched.
    """
    stats = CollapseStats(features_before=len(result.features))

    # Group features by (container_dir, prefix). Skip:
    #   - features with no tail (single-token names can't be a family).
    #   - features that aren't a top-level sibling folder under any
    #     universal container (filters out nested children whose name
    #     just happens to share a prefix).
    by_key: dict[tuple[str, str], list[str]] = {}
    sibling_folders: dict[tuple[str, str], set[str]] = {}
    for name, paths in result.features.items():
        if not paths:
            continue
        tail = _tail(name)
        if not tail:
            continue
        prefix = _first_token(name)
        # Skip prefixes that are themselves container names — we
        # don't want to collapse e.g. ``services-X`` features.
        if prefix in _CONTAINER_DIR_NAMES:
            continue
        loc = _container_dir_and_subfolder(paths, name)
        if loc is None:
            continue
        container, sibling = loc
        key = (container, prefix)
        by_key.setdefault(key, []).append(name)
        sibling_folders.setdefault(key, set()).add(sibling)

    for (container, prefix), names in sorted(by_key.items()):
        if len(names) < min_siblings:
            continue
        distinct_tails = {_tail(n) for n in names}
        if len(distinct_tails) < min_distinct_tails:
            continue
        # Require distinct sibling folders too — guards against N
        # features that all live under the SAME subfolder being
        # collapsed (would be a sub-decompose artifact, not a true
        # multi-folder family).
        if len(sibling_folders.get((container, prefix), set())) < min_distinct_tails:
            continue

        collapsed_name = _display_name(prefix)

        # Defensive: if collapsed_name already exists as a feature
        # (e.g. the LLM also named one ``Identity Management``),
        # merge into it rather than overwrite.
        union_paths: set[str] = set()
        union_flows: set[str] = set()
        merged_flow_descs: dict[str, str] = {}
        merged_flow_participants: dict[str, list] = {}
        descs: list[str] = []

        if collapsed_name in result.features:
            union_paths.update(result.features[collapsed_name])
            union_flows.update(result.flows.get(collapsed_name, []))
            merged_flow_descs.update(
                result.flow_descriptions.get(collapsed_name, {})
            )
            merged_flow_participants.update(
                getattr(result, "flow_participants", {}).get(collapsed_name, {})
            )
            if collapsed_name in result.descriptions:
                descs.append(result.descriptions[collapsed_name])

        for n in names:
            union_paths.update(result.features.get(n, []))
            union_flows.update(result.flows.get(n, []))
            merged_flow_descs.update(result.flow_descriptions.get(n, {}))
            merged_flow_participants.update(
                getattr(result, "flow_participants", {}).get(n, {})
            )
            if n in result.descriptions:
                descs.append(result.descriptions[n])

        # Drop siblings from every side channel.
        for n in names:
            result.features.pop(n, None)
            result.flows.pop(n, None)
            result.descriptions.pop(n, None)
            result.flow_descriptions.pop(n, None)
            if hasattr(result, "flow_participants"):
                result.flow_participants.pop(n, None)

        # Materialise the collapsed feature.
        result.features[collapsed_name] = sorted(union_paths)
        if union_flows:
            result.flows[collapsed_name] = sorted(union_flows)
        if merged_flow_descs:
            result.flow_descriptions[collapsed_name] = merged_flow_descs
        if merged_flow_participants and hasattr(result, "flow_participants"):
            result.flow_participants[collapsed_name] = merged_flow_participants
        if descs:
            # Keep the longest description (most informative).
            result.descriptions[collapsed_name] = max(descs, key=len)

        stats.families_collapsed += 1
        stats.siblings_removed += len(names) - 1  # net delta
        stats.collapsed_names.append(
            f"{collapsed_name} ({len(names)} siblings under {container})"
        )
        logger.info(
            "sibling_router_collapse: %s ← %d siblings under %s/%s-*",
            collapsed_name,
            len(names),
            container,
            prefix,
        )

    stats.features_after = len(result.features)
    return stats


__all__ = [
    "MIN_SIBLINGS",
    "MIN_DISTINCT_TAILS",
    "CollapseStats",
    "collapse_sibling_router_families",
]
