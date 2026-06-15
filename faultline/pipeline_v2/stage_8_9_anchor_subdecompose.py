"""Stage 8.9 — workspace-anchor sub-decomposition (deterministic).

Why this stage exists
=====================

Stage 8.7 de-sinks a workspace anchor's *double-claimed* files, but what
remains — the genuine residual that no specific feature reached — can
still be a structural blob: on a large monorepo the ``backend`` /
``<app>-frontend`` anchor keeps hundreds of files (``eval/structural_audit``
``max_feature_share`` 20-25 %, ``largest_sink_share`` high). De-sink made
the residual *honest* and Stage 8.7-naming made it *honestly named*, but
neither DECOMPOSES it: one feature still owns a quarter of the repo.

This stage splits that residual along the repository's OWN module
structure. Modern apps organise code by architectural-LAYER directories
(``modules/`` ``features/`` ``services/`` ``controllers`` …) whose
children are product DOMAINS (``modules/network-security``,
``services/secret-sync``). When an anchor's residual partitions cleanly
into such domains, each domain becomes its own developer sub-feature —
surfacing the real product capabilities the blob was hiding, and lifting
feature recall, WITHOUT any LLM and WITHOUT the precision risk of
attribution (each file lands in exactly ONE domain bucket — there is no
"which feature owns this shared file?" contention; cf. the measured-dead
single-importer / co-commit attribution levers).

The rule (locked by an offline corpus sim — soc0/infisical/inbox-zero/
documenso, ``/tmp/desink/subdecompose_sim.py``)
=============================================================

* **Layer vocab** (``_LAYER_DIRS``) — a small UNIVERSAL set of
  architectural-layer directory names (house pattern, like
  ``eval/stacks/*.yaml`` and ``_PRIMITIVE_DIR_SEGMENTS``). It locates the
  split LEVEL only; the DOMAIN names (children) are discovered from the
  tree, never hardcoded (``rule-no-repo-specific-paths``).
* **Grain floor** — a domain bucket is promoted only if it holds at least
  the repo's MEDIAN existing-feature size (``rule-no-magic-tuning``:
  scale-invariant, relative to the repo's own grain — a fine-grained repo
  splits more, a coarse one only its biggest domains). Sub-floor buckets
  fold back to the residual.
* **Container floor** — a layer must yield ≥ 2 promotable domains to count
  as a container (a lone domain is not a decomposition).

Safety / conservation
=====================

* **Path conservation.** ``residual ∪ Σ domains == anchor.paths`` exactly
  (every path lands in one place, none dropped, none duplicated). The
  anchor keeps the residual + its identity / ``"workspace anchor"`` marker.
* **Product paths byte-stable.** Sub-features inherit the anchor's
  ``product_feature_id``, so the owning product feature's path UNION is
  unchanged — the product-layer + membership-by-product gates cannot
  regress by construction. Only the DEVELOPER feature set gains specificity.
* **Not re-entrant.** Sub-features carry a ``"workspace sub-domain"``
  description (NOT ``"workspace anchor"``) so de-sink / this stage never
  treat them as anchors.

Sub-features are intentionally THIN on aggregate git metrics (commits /
bug-fix ratio reset; health inherited from the anchor as an approximation)
— their ``paths`` and distributed path-keyed surfaces are exact; richer
per-domain metrics are a follow-up (same "surface structurally, enrich
later" pattern as Stage 8.8 shared members). No LLM. No network.

This stage runs BEFORE Stage 6.9 test-strip, so the grain floor sees
each domain's pre-strip file count (tests included). A domain that clears
the floor with its tests can therefore end up below it in the final output
once its test files are stripped — the result is a genuine but thin domain,
not a floor violation.

Default ON; disable via ``FAULTLINE_STAGE_8_9_SUBDECOMPOSE=0``.
"""

from __future__ import annotations

import os
import statistics
import uuid as _uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.stage_8_7_anchor_desink import (
    _FILE_KEYED_SURFACES,
    _is_workspace_anchor,
)

if TYPE_CHECKING:
    from faultline.models.types import Feature


# Universal architectural-LAYER directory names. NOT domain names — the
# child of one of these is the discovered domain. House pattern, scale- and
# repo-invariant (rule-no-repo-specific-paths, rule-no-magic-tuning).
_LAYER_DIRS: frozenset[str] = frozenset({
    "modules", "module", "features", "feature", "services", "service",
    "controllers", "controller", "domains", "domain", "packages",
    "apps", "plugins", "integrations", "views", "screens", "routers",
    "handlers", "resources", "models", "schemas", "agents",
})

_MIN_DOMAINS_PER_LAYER = 2  # a layer with <2 promotable domains is not a container
_SUBDOMAIN_MARKER = "workspace sub-domain"


@dataclass
class SubdecomposeResult:
    enabled: bool = True
    anchors_total: int = 0
    anchors_split: int = 0          # anchors that produced ≥1 sub-feature
    subfeatures_created: int = 0
    paths_moved: int = 0            # paths relocated from anchors to sub-features
    grain_floor_by_anchor: dict[str, int] = field(default_factory=dict)
    split_sample: list[dict[str, Any]] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "anchors_total": self.anchors_total,
            "anchors_split": self.anchors_split,
            "subfeatures_created": self.subfeatures_created,
            "paths_moved": self.paths_moved,
            "split_sample": list(self.split_sample[:20]),
        }


def _is_enabled() -> bool:
    """Default ON; disable via ``FAULTLINE_STAGE_8_9_SUBDECOMPOSE=0``."""
    return os.environ.get("FAULTLINE_STAGE_8_9_SUBDECOMPOSE", "1") != "0"


def _domain_key(path: str) -> str | None:
    """``layer/child`` when *path* passes through a known layer dir whose
    child is itself a directory (i.e. more segments follow it); else None.

    ``frontend/src/modules/network-security/pages/X.tsx`` → ``modules/
    network-security``. The trailing ``len(parts) - 1`` guard requires the
    child to be a directory, not the file itself (``services/util.py`` is
    NOT a domain — ``util.py`` is a file in the layer root, residual).
    """
    parts = path.split("/")
    for i, seg in enumerate(parts[:-1]):
        if seg in _LAYER_DIRS and i + 1 < len(parts) - 1:
            return f"{seg}/{parts[i + 1]}"
    return None


def _plan_split(
    paths: list[str], floor: int,
) -> tuple[dict[str, list[str]], list[str]]:
    """Partition *paths* into ``{domain_key: [files]}`` + residual list.

    Applies the grain floor (a domain needs ≥ ``floor`` files) and the
    container floor (a layer needs ≥ ``_MIN_DOMAINS_PER_LAYER`` promotable
    domains). Sub-floor / non-container files fall to residual. Path
    conservation holds: every input path is in exactly one output bucket.
    """
    raw: dict[str, list[str]] = defaultdict(list)
    residual: list[str] = []
    for p in paths:
        k = _domain_key(p)
        if k is None:
            residual.append(p)
        else:
            raw[k].append(p)

    by_layer: dict[str, dict[str, list[str]]] = defaultdict(dict)
    for k, files in raw.items():
        by_layer[k.split("/", 1)[0]][k] = files

    domains: dict[str, list[str]] = {}
    for _layer, doms in by_layer.items():
        promotable = {k: f for k, f in doms.items() if len(f) >= floor}
        if len(promotable) >= _MIN_DOMAINS_PER_LAYER:
            domains.update(promotable)
            for k, files in doms.items():
                if k not in promotable:
                    residual.extend(files)
        else:
            for files in doms.values():
                residual.extend(files)
    return domains, residual


def _slug(domain_key: str, used: set[str]) -> str:
    """``modules/network-security`` → ``network-security`` (the domain
    child), de-duplicated against already-used feature names."""
    base = domain_key.split("/", 1)[1].replace("_", "-")
    name = base
    i = 2
    while name in used:
        name = f"{base}-{i}"
        i += 1
    used.add(name)
    return name


def _split_surfaces(anchor: "Feature", domain_files: set[str]) -> dict[str, list]:
    """Subset of each path-keyed surface whose file is in *domain_files*."""
    out: dict[str, list] = {}
    for attr, file_field in _FILE_KEYED_SURFACES:
        items = getattr(anchor, attr, None)
        if not items:
            continue
        kept = [it for it in items if getattr(it, file_field, None) in domain_files]
        if kept:
            out[attr] = kept
    return out


def _make_subfeature(
    anchor: "Feature", domain_key: str, files: list[str], name: str,
) -> "Feature":
    """Mint a developer sub-feature for one domain of *anchor*.

    Inherits the anchor's identity-ish fields (so the model is valid +
    keeps ``product_feature_id``), takes the domain's exact ``paths`` and
    its slice of the path-keyed surfaces, and resets aggregate git metrics
    (thin by design — paths are exact, metrics enrich later)."""
    fileset = set(files)
    surfaces = _split_surfaces(anchor, fileset)
    sub = anchor.model_copy(deep=True, update={
        "name": name,
        "display_name": name,
        "paths": sorted(files),
        "description": (
            f"{_SUBDOMAIN_MARKER} '{domain_key}' of anchor '{anchor.name}'"
        ),
        "uuid": _uuid.uuid4().hex,
        "split_from": getattr(anchor, "uuid", None),
        "previous_names": [],
        "merged_from": [],
        # thin aggregate metrics — exact paths, approximate stats
        "total_commits": 0,
        "bug_fixes": 0,
        "bug_fix_ratio": 0.0,
        # N:M overlays + flows are not split here (kept on the residual anchor)
        "flows": [],
        "member_files": [],
        "shared_participants": [],
        "shared_attributions": [],
        "symbol_attributions": [],
        "hotspot_files": [],
        "participants": [],
        "history": None,
    })
    for attr, kept in surfaces.items():
        setattr(sub, attr, kept)
    return sub


def subdecompose_workspace_anchors(
    features: list["Feature"],
) -> SubdecomposeResult:
    """Split workspace-anchor residuals into per-domain developer
    sub-features along the repo's own module structure.

    Mutates ``features`` in place: a split anchor keeps its residual paths,
    and the new sub-features are APPENDED. Returns telemetry.
    """
    result = SubdecomposeResult(enabled=_is_enabled())
    if not result.enabled:
        return result

    anchors = [f for f in features if _is_workspace_anchor(f)]
    result.anchors_total = len(anchors)
    if not anchors:
        return result

    # Scale-invariant grain floor: the repo's median existing (non-anchor)
    # feature size. Sub-features must be peers of the repo's own grain.
    anchor_ids = {id(a) for a in anchors}
    existing = [len(f.paths) for f in features if id(f) not in anchor_ids]
    floor = max(2, int(statistics.median(existing))) if existing else 2

    used_names = {f.name for f in features}
    new_features: list["Feature"] = []

    for anchor in anchors:
        if not anchor.paths:
            continue
        domains, residual = _plan_split(list(anchor.paths), floor)
        if not domains:
            continue
        result.grain_floor_by_anchor[anchor.name] = floor
        # Zero-path protection — never empty an anchor (mirrors 8.7 rule 1).
        # If every path moved into a domain, keep the smallest domain on the
        # anchor as its residual so it stays a valid, non-ghost feature.
        if not residual:
            smallest = min(domains, key=lambda k: len(domains[k]))
            residual = domains.pop(smallest)
            if not domains:
                continue

        anchor_residual_set = set(residual)
        moved = 0
        for domain_key, files in domains.items():
            name = _slug(domain_key, used_names)
            new_features.append(_make_subfeature(anchor, domain_key, files, name))
            moved += len(files)

        # Anchor keeps the residual: shrink paths + prune its surfaces to match.
        anchor.paths = sorted(anchor_residual_set)
        for attr, file_field in _FILE_KEYED_SURFACES:
            items = getattr(anchor, attr, None)
            if not items:
                continue
            kept = [
                it for it in items
                if getattr(it, file_field, None) in anchor_residual_set
            ]
            if len(kept) != len(items):
                setattr(anchor, attr, kept)

        result.anchors_split += 1
        result.subfeatures_created += len(domains)
        result.paths_moved += moved
        if len(result.split_sample) < 20:
            result.split_sample.append({
                "anchor": anchor.name,
                "domains": len(domains),
                "moved": moved,
                "residual": len(residual),
            })

    features.extend(new_features)
    return result


__all__ = [
    "SubdecomposeResult",
    "subdecompose_workspace_anchors",
]
