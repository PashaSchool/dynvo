"""Stage 8.9.7 — deterministic per-vendor connector split.

Integration-hub developer features aggregate MANY vendor connectors into one
row (canonical case, Soc0 2026-07-05: dev ``edr`` = 21 files where
``backend/services/edr/{crowdstrike,sentinelone,defender,cortex,
claroty_xdome}.py`` + ``schema/<vendor>_*.py`` are five distinct product
integrations). Users think per-connector ("Connect Stripe" / "Connect
Shopify" in the dub golden), so the aggregate hides real product grain and
— being name-matchable to nothing — tends to fall into the Layer-2 platform
residual whole.

The split is pure vocabulary corroboration against the PUBLIC vendor
vocabulary (``naming_validator.VENDOR_TOKENS`` — the house precedent for
"this token names a third-party product, not repo code"): inside ONE dev
feature, every owned file whose *stem tokens* name exactly one vendor joins
that vendor's group; when the groups show a genuine connector hub the
feature splits into ``<parent>-<vendor>`` sub-features (8.9's
``_make_subfeature`` contract: owned member rows, ``split_from`` lineage,
``product_feature_id`` inherited → product unions conserved). Shared
plumbing (``base.py``, ``factory.py``, normalisers) stays with the parent.

Structural rails (rule-no-magic-tuning — ratios/structure only):
  * never split a workspace anchor (vendor mentions scattered through an
    app shell are not a connector hub);
  * never split a GENERIC-CONTAINER feature (``dialogs``/``modals``/
    ``components``… — the 8.9.6 generic-domain vocabulary): per-vendor
    presentational widgets (``AwsDialog.tsx``) are UI plumbing for the
    vendor's integration, not the integration itself — splitting minted
    thin ``dialogs-<vendor>`` husks on Soc0 (2026-07-05 replay);
  * ≥ 2 DISTINCT vendors required (one vendor file ≠ a hub);
  * vendor-named files must be the MAJORITY of the feature's owned files
    (a hub feature IS its connectors; a product feature that merely touches
    two vendor SDKs keeps its grain);
  * a file naming ≥ 2 vendors is shared plumbing — stays with the parent;
  * name collisions with existing features skip that vendor group
    (deterministic, conservative).

Deterministic, $0 LLM. Default ON since Product-Spine Wave 1 (spec §4.4,
operator decision A, 2026-07-06 — the per-vendor grain is the
construction rule for connector hubs); opt-out:
``FAULTLINE_STAGE_8_9_7_VENDOR_SPLIT=0``.

Product-Spine §4.4 extension: when the caller passes the detected hub
relation (``hub_dirs``), files under a hub directory group by their
IMMEDIATE CHILD segment (vendor DIRECTORY children like
``app-store/zoom/**`` — not just vendor file stems), so dir-per-vendor
hubs split the same way stem-per-vendor hubs always did.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.naming_validator import VENDOR_TOKENS, _split_tokens

if TYPE_CHECKING:
    from faultline.models.types import Feature


def _is_enabled() -> bool:
    """Default ON since Product-Spine Wave 1 (§4.4, 2026-07-06) —
    opt-out ``FAULTLINE_STAGE_8_9_7_VENDOR_SPLIT=0``. (Historically
    default OFF / opt-in ``=1``.)"""
    return os.environ.get("FAULTLINE_STAGE_8_9_7_VENDOR_SPLIT", "1") != "0"


@dataclass
class VendorSplitResult:
    """Per-scan telemetry."""

    enabled: bool = False
    features_examined: int = 0
    hubs_split: int = 0
    connectors_created: int = 0
    files_moved: int = 0
    collisions_skipped: int = 0
    sample: list[dict[str, Any]] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "features_examined": self.features_examined,
            "hubs_split": self.hubs_split,
            "connectors_created": self.connectors_created,
            "files_moved": self.files_moved,
            "collisions_skipped": self.collisions_skipped,
            "sample": list(self.sample[:20]),
        }


def _file_vendor(path: str) -> str | None:
    """The single vendor a file's stem names, else ``None`` (0 or ≥2 vendors
    → shared plumbing). Stem only — vendor DIRECTORY names are the parent
    hub's identity, the per-file stem is the connector instance."""
    stem = path.rsplit("/", 1)[-1]
    dot = stem.find(".")
    if dot > 0:
        stem = stem[:dot]
    vendors = {t for t in _split_tokens(stem) if t in VENDOR_TOKENS}
    if len(vendors) == 1:
        return next(iter(vendors))
    return None


def _slug(name: str) -> str:
    import re

    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name or "")
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _hub_child_vendor(path: str, hub_dirs: tuple[str, ...]) -> str | None:
    """Vendor named by the file's IMMEDIATE CHILD segment under a hub dir
    (Product-Spine §4.4 dir-per-vendor layouts), else ``None``."""
    from faultline.pipeline_v2.hub_relation import vendor_of_segment

    norm = path.replace("\\", "/").strip("/")
    for hub in hub_dirs:
        prefix = hub + "/"
        if norm.startswith(prefix):
            child = norm[len(prefix):].split("/", 1)[0]
            if child and not child.startswith((".", "_")):
                return vendor_of_segment(child)
    return None


def split_vendor_connectors(
    features: list["Feature"],
    hub_dirs: tuple[str, ...] = (),
) -> VendorSplitResult:
    """Split connector-hub dev features per vendor. Appends the minted
    connector features to *features* in place; returns telemetry. No-op when
    disabled — safe to wire unconditionally.

    ``hub_dirs`` (Product-Spine §4.4) — detected hub directories; files
    under one group by their immediate child segment (vendor dirs), with
    the historical stem rule as fallback for everything else."""
    from faultline.pipeline_v2.stage_8_7_anchor_desink import (
        _FILE_KEYED_SURFACES,
        _is_workspace_anchor,
    )
    from faultline.pipeline_v2.stage_8_9_6_domain_member_attribution import (
        _GENERIC_DOMAIN_SKIP,
    )
    from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
        _make_subfeature,
    )

    result = VendorSplitResult(enabled=_is_enabled())
    if not result.enabled:
        return result

    devs = [
        f for f in features
        if getattr(f, "layer", "developer") == "developer"
    ]
    taken_names = {f.name for f in features if getattr(f, "name", None)}
    minted_all: list["Feature"] = []

    from faultline.pipeline_v2.spine_hygiene import is_facet

    for source in devs:
        paths = list(getattr(source, "paths", None) or [])
        # Facets (Product-Spine §4.1) are cross-cutting views — never hubs.
        if len(paths) < 2 or _is_workspace_anchor(source) or is_facet(source):
            continue
        # Generic-container features (dialogs/modals/components/…) hold
        # per-vendor PRESENTATIONAL widgets, not connectors — never split.
        if _slug(source.name or "") in _GENERIC_DOMAIN_SKIP:
            continue
        result.features_examined += 1
        groups: dict[str, list[str]] = {}
        for p in paths:
            v = _hub_child_vendor(p, hub_dirs) if hub_dirs else None
            if v is None:
                v = _file_vendor(p)
            if v is not None:
                groups.setdefault(v, []).append(p)
        vendor_files = sum(len(v) for v in groups.values())
        # Rails: ≥2 distinct vendors AND vendor files are the majority of the
        # footprint — the feature IS a connector hub, not a mere SDK user.
        if len(groups) < 2 or vendor_files * 2 < len(paths):
            continue
        minted: list["Feature"] = []
        moved: set[str] = set()
        for vendor in sorted(groups):  # deterministic order
            name = f"{_slug(source.name)}-{vendor}"
            if name in taken_names:
                result.collisions_skipped += 1
                continue
            files = sorted(groups[vendor])
            sub = _make_subfeature(source, vendor, files, name)
            taken_names.add(name)
            minted.append(sub)
            moved.update(files)
        if not minted:
            continue
        # Parent keeps the shared plumbing as OWNED (unlike the 8.9.5 de-own
        # contract — here the residual base/factory/normaliser files remain
        # genuinely the hub's own code).
        remaining = [p for p in paths if p not in moved]
        source.paths = remaining
        source.member_files = [
            mf for mf in (getattr(source, "member_files", None) or [])
            if (mf.get("path") if isinstance(mf, dict) else getattr(mf, "path", None))
            not in moved
        ]
        # Filter EVERY path-keyed surface (unlike _split_surfaces, keep
        # empty results too — stale rows on moved files must not linger).
        for attr, file_field in _FILE_KEYED_SURFACES:
            items = getattr(source, attr, None)
            if not items:
                continue
            setattr(source, attr, [
                it for it in items
                if getattr(it, file_field, None) not in moved
            ])
        minted_all.extend(minted)
        result.hubs_split += 1
        result.connectors_created += len(minted)
        result.files_moved += len(moved)
        if len(result.sample) < 20:
            result.sample.append({
                "source": source.name,
                "connectors": [m.name for m in minted],
                "files_moved": len(moved),
            })

    features.extend(minted_all)
    return result


__all__ = ["VendorSplitResult", "split_vendor_connectors"]
