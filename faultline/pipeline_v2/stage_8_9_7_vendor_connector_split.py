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

W1.1 aggregate-carve arm (``carve_hub_dirs``): a MEMBER-LESS hub — one
whose files all ride inside covering aggregates (midday
``apps/api/src/rest/routers/apps/{fortnox,gmail,…}`` inside the
``apps/api`` workspace anchor) — never reaches the binding: no dev is
majority-inside, so detection alone yields no members, and the two
historical rails (workspace anchors never split; vendor files must be
the majority of the source's footprint) both block the split precisely
BECAUSE the covering dev is a big aggregate. For those hubs the caller
passes ``carve_hub_dirs`` and the carve arm moves the hub's vendor
DIRECTORY children (files at depth >= 2 — direct files under the hub
stay put as plumbing) out of ANY non-facet covering dev, workspace
anchors included, with no footprint-majority rail: the ≥ 3-distinct-
vendor evidence lives on the DIRECTORY (checked at detection), not on
the accidental covering feature. Mirrors the 8.9.6b service-carve
precedent (structure evidence carves aggregates). The minted children
are majority-inside by construction, so re-detection at Stage 8.9.8
binds the hub and sibling parity then survives every later
re-attribution (the post-6.7d re-enforcement re-binds members).
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
    # W1.1 aggregate-carve arm (member-less hubs; see module docstring).
    aggregate_carves: int = 0
    carve_connectors_created: int = 0
    carve_files_moved: int = 0
    carve_sample: list[dict[str, Any]] = field(default_factory=list)
    # Debt-pack D4 keyed follow-up — husk groups folded into the parent
    # instead of minting (0-flow, sub-floor LOC; the comp
    # `aws-(integration)` twin class).
    husk_folds: int = 0
    husk_fold_sample: list[dict[str, Any]] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        tele = {
            "enabled": self.enabled,
            "features_examined": self.features_examined,
            "hubs_split": self.hubs_split,
            "connectors_created": self.connectors_created,
            "files_moved": self.files_moved,
            "collisions_skipped": self.collisions_skipped,
            "sample": list(self.sample[:20]),
        }
        # Stamped only when the arm acted — keeps pre-W1.1 telemetry
        # byte-identical on scans with no member-less hubs.
        if self.aggregate_carves:
            tele["aggregate_carves"] = self.aggregate_carves
            tele["carve_connectors_created"] = self.carve_connectors_created
            tele["carve_files_moved"] = self.carve_files_moved
            tele["carve_sample"] = list(self.carve_sample[:20])
        # Same only-when-acted contract for the husk floor.
        if self.husk_folds:
            tele["husk_folds"] = self.husk_folds
            tele["husk_fold_sample"] = list(self.husk_fold_sample[:20])
        return tele


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


def _hub_child_dir_vendor(path: str, hub_dirs: tuple[str, ...]) -> str | None:
    """Vendor named by the file's immediate child DIRECTORY under a hub
    dir, else ``None`` (W1.1 carve arm). Unlike :func:`_hub_child_vendor`
    a file sitting DIRECTLY under the hub never matches — the carve's
    evidence is the dir-per-vendor layout, and direct files
    (``index.ts``, a shared router) are hub plumbing that stays with the
    covering feature."""
    from faultline.pipeline_v2.hub_relation import vendor_of_segment

    norm = path.replace("\\", "/").strip("/")
    for hub in hub_dirs:
        prefix = hub + "/"
        if norm.startswith(prefix):
            child, sep, _rest = norm[len(prefix):].partition("/")
            if sep and child and not child.startswith((".", "_")):
                return vendor_of_segment(child)
            return None
    return None


def split_vendor_connectors(
    features: list["Feature"],
    hub_dirs: tuple[str, ...] = (),
    carve_hub_dirs: tuple[str, ...] = (),
    repo_root: Any = None,
) -> VendorSplitResult:
    """Split connector-hub dev features per vendor. Appends the minted
    connector features to *features* in place; returns telemetry. No-op when
    disabled — safe to wire unconditionally.

    ``hub_dirs`` (Product-Spine §4.4) — detected hub directories; files
    under one group by their immediate child segment (vendor dirs), with
    the historical stem rule as fallback for everything else.

    ``carve_hub_dirs`` (W1.1) — MEMBER-LESS hub directories: their vendor
    DIRECTORY children are carved out of any non-facet covering dev
    (workspace anchors included, no footprint-majority rail — see module
    docstring).

    ``repo_root`` (debt-pack, D4 keyed follow-up) — when given, arms the
    HUSK FLOOR on both arms: a vendor group with NO flow evidence (no
    source flow enters through its files) whose code files sum under
    ``stage_6_86_anchored_mint._HUB_HUSK_LOC_FLOOR`` LOC does NOT mint —
    the files stay with the parent (merge, not a standalone dev/PF).
    This is the keyed-channel twin of the W3.1 anchored-mint floor: on
    keyed scans the 8.9.x split minted the comp `aws-(integration)`
    logo.tsx+config.ts shells straight past the 6.86 bar (w31x-report
    honest-anomaly 3). Same calibrated bound (valsem4 H9, 150), same
    flow-evidence rescue. ``None`` (replay / old callers) keeps the
    historical behavior byte-identical."""
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

    # Husk-floor machinery (armed only when the caller passes repo_root).
    husk_root = None
    husk_code_exts: tuple[str, ...] = ()
    husk_loc_cache: dict[str, int] = {}
    if repo_root is not None:
        from pathlib import Path

        from faultline.pipeline_v2.spine_anchors import load_spine_vocab

        husk_root = Path(repo_root)
        husk_code_exts = tuple(
            load_spine_vocab().get("code_extensions") or ())

    def _fold_as_husk(source: "Feature", vendor: str,
                      files: list[str]) -> bool:
        """True when the vendor group is a husk (fold into parent)."""
        if husk_root is None:
            return False
        from faultline.pipeline_v2.stage_6_86_anchored_mint import (
            _HUB_HUSK_LOC_FLOOR,
            _files_loc,
            _is_code,
        )

        fileset = set(files)
        for fl in getattr(source, "flows", None) or []:
            ep = (fl.get("entry_point_file") if isinstance(fl, dict)
                  else getattr(fl, "entry_point_file", None))
            if ep in fileset:
                return False  # flow evidence — a real connector surface
        code_files = [p for p in files if _is_code(p, husk_code_exts)]
        if code_files and _files_loc(
                husk_root, code_files, husk_loc_cache) >= _HUB_HUSK_LOC_FLOOR:
            return False
        result.husk_folds += 1
        if len(result.husk_fold_sample) < 20:
            result.husk_fold_sample.append({
                "source": source.name, "vendor": vendor,
                "files": len(files),
            })
        return True

    devs = [
        f for f in features
        if getattr(f, "layer", "developer") == "developer"
    ]
    taken_names = {f.name for f in features if getattr(f, "name", None)}
    minted_all: list["Feature"] = []

    from faultline.pipeline_v2.spine_hygiene import is_facet

    for source in devs:
        paths = list(getattr(source, "paths", None) or [])
        # Facets (Product-Spine §4.1) are cross-cutting views — never hubs
        # and never carve donors.
        if len(paths) < 2 or is_facet(source):
            continue
        is_anchor = _is_workspace_anchor(source)
        is_generic = _slug(source.name or "") in _GENERIC_DOMAIN_SKIP

        # ── W1.1 carve arm: vendor DIRECTORY children of member-less hubs
        # leave the covering aggregate — workspace anchors included, no
        # footprint rail, and NO generic-name guard: the evidence is the
        # ≥3-vendor DIRECTORY (checked at detection), not the covering
        # feature, and workspace aggregates carry exactly the generic
        # names the stem-arm guard exists for (midday's cover is the
        # apps/api anchor literally named 'api').
        carve_groups: dict[str, list[str]] = {}
        if carve_hub_dirs:
            for p in paths:
                v = _hub_child_dir_vendor(p, carve_hub_dirs)
                if v is not None:
                    carve_groups.setdefault(v, []).append(p)
        carved_files = {p for fs in carve_groups.values() for p in fs}

        # ── Historical stem / member-ful-hub arm (rails unchanged: never
        # splits workspace anchors; generic-container features hold
        # per-vendor PRESENTATIONAL widgets, not connectors — never split).
        groups: dict[str, list[str]] = {}
        if not is_anchor and not is_generic:
            result.features_examined += 1
            for p in paths:
                if p in carved_files:
                    continue  # a file leaves the aggregate exactly once
                v = _hub_child_vendor(p, hub_dirs) if hub_dirs else None
                if v is None:
                    v = _file_vendor(p)
                if v is not None:
                    groups.setdefault(v, []).append(p)
            vendor_files = sum(len(v) for v in groups.values())
            # Rails: ≥2 distinct vendors AND vendor files are the majority of
            # the footprint — the feature IS a connector hub, not a mere SDK
            # user.
            if len(groups) < 2 or vendor_files * 2 < len(paths):
                groups = {}
        if not groups and not carve_groups:
            continue

        minted: list["Feature"] = []
        carve_minted: list["Feature"] = []
        moved: set[str] = set()
        carve_moved: set[str] = set()
        for arm_groups, minted_sink, moved_sink in (
            (carve_groups, carve_minted, carve_moved),
            (groups, minted, moved),
        ):
            for vendor in sorted(arm_groups):  # deterministic order
                name = f"{_slug(source.name)}-{vendor}"
                if name in taken_names:
                    result.collisions_skipped += 1
                    continue
                files = sorted(arm_groups[vendor])
                # D4 keyed husk floor: a flowless sub-floor group merges
                # into the parent instead of minting a shell twin.
                if _fold_as_husk(source, vendor, files):
                    continue
                sub = _make_subfeature(source, vendor, files, name)
                taken_names.add(name)
                minted_sink.append(sub)
                moved_sink.update(files)
        if not minted and not carve_minted:
            continue
        moved |= carve_moved
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
        minted_all.extend(carve_minted)
        minted_all.extend(minted)
        if minted:
            result.hubs_split += 1
            result.connectors_created += len(minted)
            result.files_moved += len(moved) - len(carve_moved)
            if len(result.sample) < 20:
                result.sample.append({
                    "source": source.name,
                    "connectors": [m.name for m in minted],
                    "files_moved": len(moved) - len(carve_moved),
                })
        if carve_minted:
            result.aggregate_carves += 1
            result.carve_connectors_created += len(carve_minted)
            result.carve_files_moved += len(carve_moved)
            if len(result.carve_sample) < 20:
                result.carve_sample.append({
                    "source": source.name,
                    "connectors": [m.name for m in carve_minted],
                    "files_moved": len(carve_moved),
                })

    features.extend(minted_all)
    return result


__all__ = ["VendorSplitResult", "split_vendor_connectors"]
