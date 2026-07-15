"""Stage 6.9c — schema-monolith member strip (B58-v3 Seg C, deterministic).

The leak (S4, documenso keyed exhibit ``team.verify.email.$token``): a
stage-2 anchor whose merged ``sources`` include both ``route`` and
``schema`` carries the repo's MONOLITHIC whole-DB schema file
(``packages/prisma/schema.prisma``, 895 LOC) as an anchor member of a
152-LOC leaf route, and the Stage 6.97 primary-owner tiebreak then dumps
the schema package's shared plumbing (``index.ts`` / ``helper.ts`` /
``utils/remember.ts``) onto the same PF — 1,202 LOC claimed where 152 LOC
of route code exist (84% prisma). A whole-DB schema dump is dev-infra
(the B33/B59 family): it must never inflate a foreign product feature.

Mechanism (structure first, YAML corroboration second — the
mechanisms-not-vocabularies doctrine):

1. A **monolith** is a file whose basename / path-suffix matches an
   ecosystem whole-DB-schema convention (``schema.prisma``,
   ``db/schema.rb``, ``db/structure.sql`` — data:
   ``schema-monolith-files.yaml``). Per-domain schema files (Drizzle
   ``<domain>/schema.ts``, Django per-app ``models.py``) are NOT
   monoliths and are never touched.
2. The monolith's **schema package** is its containing directory
   (``packages/prisma/schema.prisma`` → ``packages/prisma``). A monolith
   at the repo root degrades to a no-op (its "package" would be the whole
   repo — nothing is foreign).
3. A claimant feature is the package's **home** when EVERY file it
   claims lives inside the schema package — it IS the schema-package
   feature (documenso's ``prisma`` dev, ``dev_tooling`` scope, 100%
   ``packages/prisma/**``). Homes keep every claim. The bar is strict
   by design: any fractional threshold spares the minimal leak shape
   (a two-file dev ``route.tsx + schema.prisma`` is 50% inside), and a
   ``route,schema`` join always carries its route file outside the
   package — foreign by construction.
4. Every **foreign** claimant (majority outside) is stripped of ALL its
   claims under the schema package — ``paths``, ``member_files``,
   attributions, participants — exactly the Stage 6.9/6.9b output-tree
   strip contract (metric scalars are never recomputed here; Stage 6.97
   runs later and re-truths ``loc`` from the stripped ledgers).
   Features that become path-empty are dropped (test-strip precedent).

Runs as Stage 6.9c, immediately after the 6.9b generated strip: BEFORE
the 6.86 anchored mint (PF ``member_files`` are carried from dev
ledgers, and spine anchors read ``owned_paths_of`` — so PF rows form
clean) and BEFORE Stage 6.97 LOC (owned LOC re-truths itself).
Schema-monolith files carry no flows/journeys, so the journey layer is
structurally unaffected; the file's coordinates survive on its home
feature (lines-are-coordinates).

Flag: ``FAULTLINE_GRAIN_WAVE`` (B58-v3 wave switch, default OFF —
registered in ``scan_result_cache.ENV_OUTPUT_FLAGS``, appended WITHOUT a
KEY_SCHEMA bump; the bump rides the separate flip commit only).
``=0``/unset → the pass never runs → byte-identical output.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from faultline.pipeline_v2.data import load_yaml
from faultline.pipeline_v2.stage_6_9_test_strip import (
    _FEATURE_LIST_ATTRS,
    _feature_path_empty,
    _path_of,
)

__all__ = [
    "GRAIN_WAVE_ENV",
    "grain_wave_enabled",
    "monolith_package_of",
    "strip_schema_monolith_members",
]

#: B58-v3 — ONE flag gates the whole grain wave (Seg A internal-lib fdir
#: lane candidacy + Seg C schema-monolith member strip), the B53
#: one-flag-both-segments precedent. Default OFF.
GRAIN_WAVE_ENV = "FAULTLINE_GRAIN_WAVE"

#: Ecosystem whole-DB-schema conventions (data, not code).
_MONOLITH_FILE = "schema-monolith-files.yaml"


def grain_wave_enabled() -> bool:
    """B58-v3 grain wave. Default **OFF**; only an explicit ``1``/``true``
    arms it — ``FAULTLINE_GRAIN_WAVE=0``/unset keeps every output channel
    byte-identical (the kill-switch law)."""
    return os.environ.get(GRAIN_WAVE_ENV, "0").strip().lower() in {
        "1", "true",
    }


@lru_cache(maxsize=1)
def _monolith_conventions() -> tuple[frozenset[str], tuple[str, ...]]:
    """``(basenames, path_suffixes)`` from the packaged YAML — lowercased;
    suffixes are matched with ``endswith`` on the ``/``-normalized path."""
    data = load_yaml(_MONOLITH_FILE)
    basenames = frozenset(
        str(b).strip().lower()
        for b in (data.get("monolith_basenames") or [])
        if str(b).strip()
    )
    suffixes = tuple(
        str(s).strip().lower().lstrip("/")
        for s in (data.get("monolith_path_suffixes") or [])
        if str(s).strip()
    )
    return basenames, suffixes


def _is_monolith(path: str) -> bool:
    if not path or not isinstance(path, str):
        return False
    norm = path.replace("\\", "/").lower()
    basenames, suffixes = _monolith_conventions()
    base = norm.rsplit("/", 1)[-1]
    if base in basenames:
        return True
    return any(norm.endswith("/" + s) or norm == s for s in suffixes)


def monolith_package_of(path: str) -> str | None:
    """The monolith's schema-package dir (its containing directory), or
    ``None`` when the file is not a monolith or sits at the repo root
    (safe degradation — the whole repo is never "the schema package")."""
    if not _is_monolith(path):
        return None
    norm = path.replace("\\", "/").strip("/")
    if "/" not in norm:
        return None
    return norm.rsplit("/", 1)[0]


def _claimed_paths(feature: Any) -> set[str]:
    """Every path this feature claims — ``paths`` ∪ ``member_files``."""
    out: set[str] = set()
    for p in (getattr(feature, "paths", None) or []):
        if isinstance(p, str) and p:
            out.add(p)
    for m in (getattr(feature, "member_files", None) or []):
        p = _path_of(m)
        if p:
            out.add(p)
    return out


def _under(path: str, pkg: str) -> bool:
    return path == pkg or path.startswith(pkg + "/")


def _strip_pkg_attr(obj: Any, attr: str, pkg: str) -> int:
    """Drop entries under ``pkg`` from one list attribute. Returns the
    removed count (0 for missing/non-list attributes — tolerant)."""
    cur = getattr(obj, attr, None)
    if not isinstance(cur, list):
        return 0
    kept: list[Any] = []
    removed = 0
    for e in cur:
        p = _path_of(e)
        if p is not None and _under(p, pkg):
            removed += 1
            continue
        kept.append(e)
    if removed:
        setattr(obj, attr, kept)
    return removed


def strip_schema_monolith_members(
    features: list[Any],
    product_features: list[Any] | None = None,
) -> dict[str, Any]:
    """Strip schema-package claims from foreign claimants, in place.

    ``features`` — the dev-feature list (product-layer duplicates in it
    are swept by the same rule). ``product_features`` — swept defensively
    when already minted (at the 6.9c call site the mint has not run yet;
    unit scenes may pass pre-built PF rows).

    Returns telemetry: ``monoliths`` (paths found), ``packages``
    (schema-package dirs), ``homes`` (pkg → home feature name),
    ``no_home`` (packages every claimant was foreign to),
    ``paths_removed``, ``features_stripped``, ``features_dropped``.
    """
    tele: dict[str, Any] = {
        "monoliths": [], "packages": [], "homes": {}, "no_home": [],
        "paths_removed": 0, "features_stripped": 0, "features_dropped": 0,
    }
    features = features if isinstance(features, list) else []
    everyone: list[Any] = list(features) + list(product_features or [])

    # 1. Find monoliths among ALL claimed paths (any claimant, any role).
    packages: dict[str, str] = {}  # pkg dir -> monolith path
    for f in everyone:
        for p in _claimed_paths(f):
            pkg = monolith_package_of(p)
            if pkg:
                packages.setdefault(pkg, p)
    if not packages:
        return tele
    tele["monoliths"] = sorted(set(packages.values()))
    tele["packages"] = sorted(packages)

    # 2. Per package: homes keep, foreign claimants strip. The
    # home/foreign judgement reads a PRE-strip snapshot of every
    # feature's claims — a claimant partially stripped by one package's
    # pass must not become "all-inside" for the next (order
    # independence; the cross-package schema-phantom shape).
    claims_before: dict[int, set[str]] = {
        id(f): _claimed_paths(f) for f in everyone
    }
    stripped_ids: set[int] = set()
    for pkg in sorted(packages):
        home_names: list[str] = []
        for f in everyone:
            claimed = claims_before[id(f)]
            if not claimed:
                continue
            inside = sum(1 for c in claimed if _under(c, pkg))
            if not inside:
                continue
            if inside == len(claimed):
                # Every claim inside — the schema package's own feature
                # (the claim's home). Strict: see the module docstring.
                nm = str(getattr(f, "name", "") or "")
                if nm:
                    home_names.append(nm)
                continue
            removed = 0
            for attr in _FEATURE_LIST_ATTRS:
                removed += _strip_pkg_attr(f, attr, pkg)
            if removed:
                tele["paths_removed"] += removed
                if id(f) not in stripped_ids:
                    stripped_ids.add(id(f))
                    tele["features_stripped"] += 1
        if home_names:
            tele["homes"][pkg] = sorted(home_names)
        else:
            tele["no_home"].append(pkg)

    # 3. Drop ONLY features THIS pass stripped empty (test-strip
    # precedent) — a feature that was already path-less on entry (lane
    # rows, markers) is never compacted here. Only the dev list is
    # compacted — a pre-built PF row is never dropped here (PF survival
    # belongs to the mint/emission passes).
    drop_ids = {
        id(f) for f in features
        if id(f) in stripped_ids
        and getattr(f, "layer", "developer") != "product"
        and _feature_path_empty(f)
    }
    if drop_ids:
        before = len(features)
        features[:] = [f for f in features if id(f) not in drop_ids]
        tele["features_dropped"] = before - len(features)

    tele["no_home"] = sorted(tele["no_home"])
    return tele
