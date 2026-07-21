"""Stage 7 (post-emission) — FILE-level shared-infrastructure lane.

Forensic root cause (wave7, faithful ``validate_scan.py`` replica): 69% of the
118 I15/I16 spine violations are ``SPILL->unowned`` — a journey is CORRECTLY
homed with CORRECT entries, but its member flows TRAVERSE shared-infra files that
NO product feature and NO lane claims (papermark ``lib/prisma.ts``,
``lib/types.ts``, ``components/ui/*``; onyx ``configs/constants.py``,
``error_handling/exceptions.py``, ``background/indexing/models.py``). These
unowned-infra files inflate the I15 attach-overlap denominator and drag overlap
below the 0.34 floor.

The validator's OWN I15 logic already excludes lane-classified infra from that
denominator — its comment names "packages/ui, lib" as infrastructure a journey
TRAVERSES, "NEITHER ours NOR foreign — neutral ground". The unowned-infra files
are the SAME category, merely never lane-classified. This stage classifies them
(also the correct *product* mirror: infra attributed as infra), moving the RESULT,
not the gate (the I15 floor is untouched).

Mechanism (scale-invariant, mechanisms-not-vocabularies — a file F joins the
``platform_infrastructure`` lane IFF ALL hold):

  * **S2 import-asymmetry (PRIMARY)** — F's *fan-in*, the count of DISTINCT
    product features whose OWNED code imports F (via the ``ts_ast`` + ``py_ast``
    resolved import graph), is high by a SCALE-INVARIANT test: F is imported by
    ``>= ceil(pct * num_product_features)`` distinct PFs, with a definitional
    shared-floor of ``>= 2`` (a file imported by fewer than two product features
    is that feature's private util, not shared infrastructure). ``pct`` is a
    single corpus-calibrated fraction (``FAULTLINE_FILE_LANE_PCT``), never a
    per-repo absolute integer (rule-no-magic-tuning).
  * **S3 no-product-surface (HARD)** — F is not a route/page (``routes_index``)
    and not any product feature's anchor file. A product surface is NEVER laned,
    however widely imported (anti-case: a shared dashboard page).
  * **GUARD** — F is currently unowned by any product dev-feature AND not present
    in any ``pf.paths`` (and not already a lane resident). Never steal a file a
    PF claims — this kills the pf.paths over-fires the counterfactual measured.

  S1 (structural directory / dependency-manifest) is CORROBORATION only — reported
  in telemetry for ranking/explainability, never a sole trigger.

Placement & discipline: runs post-rollup, AFTER ``emission_integrity`` settles the
final ``features`` / ``product_features`` and BEFORE the ``path_index`` rebuild +
``build_platform_infrastructure_lane`` — so the emitted ``path_index`` (the
validator's ``file_owner_pf`` source) sees the files as lane-owned and the lane
surface emits them. Strictly additive (rule-stage-8): the Sonnet prompt is
untouched; existing PFs/devs are never mutated (only NEW lane devs appended).
Deterministic, $0 LLM: sorted iteration, content-derived uuids, no set iteration
into output. Kill-switch ``FAULTLINE_FILE_LANE=0`` -> byte-identical to main.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.spine_anchors import owned_paths_of

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

logger = logging.getLogger(__name__)

__all__ = [
    "FILE_LANE_ENV",
    "FILE_LANE_PCT_ENV",
    "SHARED_LEAF_CONSISTENCY_ENV",
    "DATA_LEAF_ENV",
    "DEFAULT_FILE_LANE_PCT",
    "SHARED_FLOOR",
    "file_lane_enabled",
    "file_lane_pct",
    "shared_leaf_consistency_enabled",
    "data_leaf_enabled",
    "run_file_lane_infra",
    "enforce_shared_leaf_consistency",
    "enforce_data_leaf_shared",
]

FILE_LANE_ENV = "FAULTLINE_FILE_LANE"
FILE_LANE_PCT_ENV = "FAULTLINE_FILE_LANE_PCT"
#: B15 — role-CONSISTENCY law for shared-leaf member files. Same scale-invariant
#: fan-in threshold as the lane; a member file that is high cross-PF-fan-in, NOT a
#: product surface, and ALREADY ``role="shared"`` in >= 1 feature is shared infra
#: (i18n locale blobs, fixtures, fonts) — force ``role="shared"`` in EVERY feature
#: that claims it, so the I23 anchor-body check (which excludes ``shared``) stops
#: counting it as a PF's own body. Kill-switch ``=0`` -> byte-identical.
SHARED_LEAF_CONSISTENCY_ENV = "FAULTLINE_SHARED_LEAF_CONSISTENCY"

#: Corpus-calibrated fan-in fraction (see filelane-report.md calibration curve):
#: a file joins the lane when imported by ``>= ceil(pct * num_product_features)``
#: distinct product features. Scale-invariant — the threshold tracks each repo's
#: product breadth, not an absolute count. Calibrated at the corpus knee: over
#: the 8-repo wave7 corpus, clearing is flat-maximal at pct 0.06-0.08 (37/118
#: I15/I16 cleared) then falls (0.10→34, 0.12→30); 0.08 sits at the knee with
#: the higher (more conservative) per-repo threshold for equal yield. The
#: resulting thresholds track product breadth: documenso(19 PFs)→2, onyx(34)→3,
#: papermark(36)→3, midday(44)→4, supabase(49)→4, Soc0(53)→5, typebot(78)→7 —
#: never a per-repo absolute (rule-no-magic-tuning). Overridable for the sweep.
DEFAULT_FILE_LANE_PCT = 0.08

#: Definitional lower bound of "shared": a file imported by fewer than two
#: distinct product features is a single feature's private code, not shared
#: infrastructure. NOT a tuned constant — the semantic boundary of sharing.
SHARED_FLOOR = 2

_FALSY = frozenset({"0", "false", "no", "off"})

#: Structural directory tokens — S1 CORROBORATION only (telemetry / ranking),
#: never a trigger. Data-as-list per feedback-mechanisms-over-vocabularies.
_INFRA_DIR_TOKENS = frozenset({
    "lib", "libs", "utils", "util", "ui", "components", "config", "configs",
    "constants", "types", "helpers", "shared", "common", "core", "clients",
    "client", "layouts", "hooks", "styles", "theme", "context", "providers",
    "models", "schemas", "errors", "error", "exceptions", "middleware",
})


def file_lane_enabled() -> bool:
    """Default ON; ``FAULTLINE_FILE_LANE=0`` restores byte-identical output
    (this stage becomes a no-op — no lane devs appended, no view built)."""
    return (os.environ.get(FILE_LANE_ENV, "1") or "1").strip().lower() \
        not in _FALSY


def shared_leaf_consistency_enabled() -> bool:
    """Default ON; ``FAULTLINE_SHARED_LEAF_CONSISTENCY=0`` restores byte-identical
    output (the B15 consistency post-pass becomes a no-op — no roles changed)."""
    return (os.environ.get(SHARED_LEAF_CONSISTENCY_ENV, "1") or "1").strip().lower() \
        not in _FALSY


#: B15b (§4b DATA-FILE rail, re-ratified). A SEPARATE sub-flag (not the B15
#: consistency flag): the two rails ship / roll back independently — B15 is a
#: consistency law (shared-tagged files made consistent), B15b deliberately DROPS
#: that guard to reach the closure-EVERYWHERE locale blobs (de/en.json), so it
#: carries the heavier over-fire risk and deserves its own kill-switch + A/B.
DATA_LEAF_ENV = "FAULTLINE_DATA_LEAF"

#: A data blob is "large" when its LOC is >= this fraction of repo source LOC.
#: Scale-invariant (no absolute constant, rule-no-magic-tuning) — calibrated at
#: the MAX-MARGIN split of the measured corpus gap: locale packs + template-asset
#: blobs sit at 1.3-2.4% of repo_loc (Soc0 de/en/fr/pt.json 2.06%; kan board/
#: template/member/settings assets 1.3-2.4%); the nearest SPARED data file
#: (documenso prisma schema) is at 0.54% — 0.01 sits in the middle of that gap.
DATA_LEAF_LOC_FRAC = 0.01


def data_leaf_enabled() -> bool:
    """Default ON; ``FAULTLINE_DATA_LEAF=0`` restores byte-identical output
    (the B15b data-file rail becomes a no-op — no roles changed)."""
    return (os.environ.get(DATA_LEAF_ENV, "1") or "1").strip().lower() not in _FALSY


def file_lane_pct() -> float:
    """The scale-invariant fan-in fraction (``FAULTLINE_FILE_LANE_PCT``).

    Clamped to ``(0, 1]``; falls back to :data:`DEFAULT_FILE_LANE_PCT` on any
    malformed value so a bad env can never crash a scan.
    """
    raw = os.environ.get(FILE_LANE_PCT_ENV)
    if not raw:
        return DEFAULT_FILE_LANE_PCT
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_FILE_LANE_PCT
    if not (0.0 < val <= 1.0):
        return DEFAULT_FILE_LANE_PCT
    return val


def _pf_key(pf: Any) -> str | None:
    return getattr(pf, "id", None) or getattr(pf, "name", None)


def _parent_dir(rel: str) -> str:
    rel = rel.replace("\\", "/")
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _dir_infra_corroborated(rel: str) -> bool:
    """S1 corroboration: does any path component look structurally like infra?
    Reported only — NEVER gates a decision (mechanisms-not-vocabularies)."""
    parts = [p.lower() for p in rel.replace("\\", "/").split("/")[:-1]]
    return any(p in _INFRA_DIR_TOKENS for p in parts)


def _importer_views(ctx: Any) -> list[Any]:
    """The resolved import graphs (TS/JS via ts_ast, Python via py_ast). Each
    is a ``ProvenanceView`` exposing ``in_repo_targets(src)``. ``None`` views
    (flag off / tree-sitter absent / build failed) are dropped — the stage then
    simply sees fewer edges and lanes fewer files (never crashes)."""
    tracked = sorted(
        str(p).replace("\\", "/")
        for p in (getattr(ctx, "tracked_files", None) or [])
    )
    if not tracked:
        return []
    repo_root = str(getattr(ctx, "repo_path", "."))
    views: list[Any] = []
    try:
        from faultline.pipeline_v2.ts_ast.adapter import repo_provenance as _ts
        v = _ts(repo_root, tracked)
        if v is not None:
            views.append(v)
    except Exception:  # noqa: BLE001 — fallback law: missing lang graph degrades
        logger.debug("file_lane: ts_ast provenance unavailable", exc_info=True)
    try:
        from faultline.pipeline_v2.py_ast.adapter import repo_provenance as _py
        v = _py(repo_root, tracked)
        if v is not None:
            views.append(v)
    except Exception:  # noqa: BLE001
        logger.debug("file_lane: py_ast provenance unavailable", exc_info=True)
    return views


def run_file_lane_infra(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    routes_index: list[dict[str, Any]] | None,
    ctx: Any,
    *,
    views: list[Any] | None = None,
) -> dict[str, Any]:
    """Classify unowned high-fan-in shared-infra files into the lane.

    Appends NEW lane dev features (``product_feature_id=None`` +
    ``shared_reason='shared_infra_fanin'``) to ``developer_features`` in place;
    NEVER mutates an existing feature. Returns telemetry for
    ``scan_meta.file_lane``.

    ``views`` — the resolved import graphs (``ProvenanceView`` list). Left
    ``None`` in production (built from ``ctx`` via ts_ast + py_ast); tests
    inject fakes so the classification is exercised without tree-sitter or a
    real repo on disk. Each view only needs ``.files`` + ``.in_repo_targets``.
    """
    tele: dict[str, Any] = {
        "enabled": True, "applied": False,
        "num_product_features": 0, "threshold": 0, "pct": file_lane_pct(),
        "candidates_scanned": 0, "laned_files": 0, "laned_devs": 0,
        "laned_loc": 0,
        "blocked_owned": 0, "blocked_pf_paths": 0, "blocked_surface": 0,
        "blocked_low_fanin": 0,
        "dir_corroborated": 0, "samples": [],
    }
    if not file_lane_enabled():
        tele["enabled"] = False
        return tele

    devs = [
        f for f in developer_features
        if getattr(f, "layer", "developer") == "developer"
        and getattr(f, "name", None)
    ]
    pf_by_key: dict[str, "Feature"] = {}
    for pf in product_features or []:
        k = _pf_key(pf)
        if k:
            pf_by_key[k] = pf
    num_pfs = len(pf_by_key)
    tele["num_product_features"] = num_pfs
    if num_pfs == 0:
        return tele
    threshold = max(SHARED_FLOOR, math.ceil(file_lane_pct() * num_pfs))
    tele["threshold"] = threshold

    # ── ownership maps (mirror validator.file_owner_pf + provenance_rehome) ──
    # file -> owning product PF key (product-homed dev primary claims); the set
    # of ALL files any dev already owns (product OR lane — never re-lane those);
    # the set of files any PF lists in pf.paths (guard); PF anchor files (S3).
    file_owner_pf: dict[str, str] = {}
    owned_any: set[str] = set()
    for f in devs:
        pfid = getattr(f, "product_feature_id", None)
        owned = owned_paths_of(f)
        owned_any.update(owned)
        if pfid and pfid in pf_by_key:
            for p in owned:
                file_owner_pf.setdefault(p, pfid)
    all_pf_paths: set[str] = set()
    surface_files: set[str] = set()
    for pf in product_features or []:
        all_pf_paths.update(str(p) for p in (getattr(pf, "paths", None) or []))
        aid = str(getattr(pf, "anchor_id", None) or "")
        if ":" in aid:
            tail = aid.split(":", 1)[1]
            if tail:
                surface_files.add(tail)
    for r in (routes_index or []):
        fp = r.get("file") if isinstance(r, dict) else None
        if fp:
            surface_files.add(str(fp))

    # ── import graph -> importers[target] = {source files importing it} ──────
    if views is None:
        views = _importer_views(ctx)
    if not views:
        tele["reason"] = "no import graph"
        return tele
    importers: dict[str, set[str]] = defaultdict(set)
    for view in views:
        for src in sorted(getattr(view, "files", None) or ()):
            for tgt in view.in_repo_targets(src):
                importers[tgt].add(src)

    # ── classify (deterministic: sorted candidate order) ─────────────────────
    laned: dict[str, int] = {}
    for tgt in sorted(importers):
        tele["candidates_scanned"] += 1
        # GUARD — never touch an owned file or one a PF claims.
        if tgt in owned_any:
            tele["blocked_owned"] += 1
            continue
        if tgt in all_pf_paths:
            tele["blocked_pf_paths"] += 1
            continue
        # S3 (HARD) — a product surface is never laned.
        if tgt in surface_files:
            tele["blocked_surface"] += 1
            continue
        # S2 (PRIMARY) — fan-in across DISTINCT product features.
        pf_importers = {
            file_owner_pf[s] for s in importers[tgt] if s in file_owner_pf
        }
        fanin = len(pf_importers)
        if fanin < threshold:
            tele["blocked_low_fanin"] += 1
            continue
        laned[tgt] = fanin

    if not laned:
        return tele

    # ── build lane devs, grouped by parent directory (readable, structural) ──
    from faultline.pipeline_v2.stage_6_86_anchored_mint import _files_loc

    root = Path(getattr(ctx, "repo_path", "."))
    loc_cache: dict[str, int] = {}
    template = _template_dev(developer_features)
    used_names = {getattr(f, "name", "") for f in developer_features}

    groups: dict[str, list[str]] = defaultdict(list)
    for tgt in sorted(laned):
        groups[_parent_dir(tgt)].append(tgt)

    new_devs: list["Feature"] = []
    total_loc = 0
    for gdir in sorted(groups):
        files = sorted(groups[gdir])
        loc = _files_loc(root, files, loc_cache)
        dev = _make_lane_dev(template, gdir, files, loc, used_names)
        used_names.add(dev.name)
        new_devs.append(dev)
        total_loc += loc
        corr = sum(1 for p in files if _dir_infra_corroborated(p))
        tele["dir_corroborated"] += corr
        if len(tele["samples"]) < 25:
            tele["samples"].append({
                "dir": gdir or "<root>", "files": len(files), "loc": loc,
                "min_fanin": min(laned[p] for p in files),
                "max_fanin": max(laned[p] for p in files),
                "dir_infra": bool(corr),
                "sample_file": files[0],
            })

    developer_features.extend(new_devs)
    tele["applied"] = True
    tele["laned_files"] = len(laned)
    tele["laned_devs"] = len(new_devs)
    tele["laned_loc"] = total_loc
    return tele


# ── lane-dev construction (mirror provenance_rehome / lane_excavation) ────────


def _template_dev(developer_features: list["Feature"]) -> "Feature":
    """A deterministic template to clone field DEFAULTS from — every content
    field is overridden in :func:`_make_lane_dev`, so the choice only supplies
    pydantic defaults for fields we do not set. Prefer an existing lane resident
    (closest shape), else the uuid-lowest dev."""
    devs = [
        f for f in developer_features
        if getattr(f, "layer", "developer") == "developer"
    ]
    lane_devs = [
        f for f in devs if getattr(f, "product_feature_id", None) is None
    ]
    pool = lane_devs or devs
    return sorted(pool, key=lambda f: str(getattr(f, "uuid", "") or ""))[0]


def _sanitize(seg: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", seg.lower()).strip("-")


def _make_lane_dev(
    template: "Feature",
    gdir: str,
    files: list[str],
    loc: int,
    used_names: set[str],
) -> "Feature":
    """A file-lane resident dev claiming *files* under directory *gdir* — mirrors
    ``provenance_rehome._make_rehome_dev`` field discipline (content-derived uuid,
    owned primary members, zeroed git/flow state), with the infra-lane reason."""
    from faultline.models.types import MemberFile
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        _SHARED_REASON_INFRA_FANIN,
    )

    base = "shared-infra"
    tail = _sanitize(gdir.rsplit("/", 1)[-1]) if gdir else "root"
    name = f"{base}-{tail}" if tail else base
    n = 2
    while name in used_names:
        name = f"{base}-{tail}-{n}" if tail else f"{base}-{n}"
        n += 1
    members = [
        MemberFile(
            path=p, role="anchor", confidence=1.0, primary=True,
            evidence="file-lane: shared-infra fan-in >= product-breadth floor",
        )
        for p in files
    ]
    uuid = hashlib.sha256(
        f"file-lane-v1|{gdir}|{'|'.join(files)}".encode("utf-8")
    ).hexdigest()[:32]
    from faultline.pipeline_v2.metrics_recompute import (  # local: avoid import cycle
        metrics_recompute_enabled,
        mint_null_state,
    )

    # Every content field is set explicitly (git / health / flow state zeroed to
    # neutral defaults) so the synthetic lane dev never inherits a template
    # feature's stats — regardless of which template supplied the pydantic
    # defaults for fields we do not name.
    _update: dict = {
        "name": name,
        "display_name": name,
        "paths": list(files),
        "member_files": members,
        "product_feature_id": None,
        "shared_reason": _SHARED_REASON_INFRA_FANIN,
        "loc": loc,
        "loc_shared": 0,
        "description": (
            f"Shared infrastructure ({gdir or 'repo root'}): files imported "
            f"across multiple product features with no product surface of "
            f"their own — neutral ground (file-lane)."
        ),
        "uuid": uuid,
        "anchor_id": f"file-lane:{gdir}" if gdir else "file-lane:<root>",
        "split_from": None,
        "previous_names": [],
        "merged_from": [],
        "authors": [],
        "total_commits": 0,
        "bug_fixes": 0,
        "bug_fix_ratio": 0.0,
        "health_score": 100.0,
        "health_confidence": "insufficient",
        "coverage_pct": None,
        "symbol_health_score": None,
        "name_confidence": "high",
        "dual_evidence": None,
        "flows": [],
        "bug_fix_prs": [],
        "hotspot_files": [],
        "shared_participants": [],
        "shared_attributions": [],
        "symbol_attributions": [],
        "participants": [],
        "history": None,
    }
    if metrics_recompute_enabled():
        # B76 — the one identity field this factory still deep-copied
        # from the template: last_modified (mint-time metric zeroing
        # class). mint_null_state() is idempotent over the null stamps
        # already listed above.
        _update.update(mint_null_state())
    return template.model_copy(deep=True, update=_update)


# ── B15: shared-leaf role consistency (post-emission) ────────────────────────


def _surface_paths(product_features: list["Feature"],
                   routes_index: list[dict[str, Any]] | None) -> set[str]:
    """S3 no-product-surface set — mirrors ``run_file_lane_infra``: a PF anchor
    file (the tail of ``anchor_id``) and every routes_index file. A product
    surface is NEVER reclassified shared, however widely claimed."""
    surface: set[str] = set()
    for pf in product_features or []:
        aid = str(getattr(pf, "anchor_id", None) or "")
        if ":" in aid:
            tail = aid.split(":", 1)[1]
            if tail:
                surface.add(tail)
    for r in (routes_index or []):
        fp = r.get("file") if isinstance(r, dict) else None
        if fp:
            surface.add(str(fp))
    return surface


def enforce_shared_leaf_consistency(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    routes_index: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """B15 — role-CONSISTENCY law for shared-leaf member files.

    A file claimed as a member file by MANY features (cross-PF claim fan-in
    ``>= max(SHARED_FLOOR, ceil(pct * num_pfs))`` — the SAME scale-invariant
    threshold the lane uses), that is NOT a product surface, and that is
    ALREADY ``role="shared"`` on >= 1 feature, is shared infrastructure the
    closure BFS re-attributed inconsistently (the i18n-locale class: the same
    ``locales/*.json`` tagged ``shared`` on one PF but ``closure`` on another).
    Force ``role="shared"`` on EVERY member-file row for that path so the I23
    anchor-body coherence check (which excludes ``role="shared"``) stops
    counting it as any single PF's own body.

    In-place, deterministic (sorted), idempotent. NEVER touches a low-fan-in
    owned asset (fan-in floor) nor a product surface (S3). No-op — and
    byte-identical — when the flag is off. Returns telemetry incl. the caught
    files so non-i18n high-fan-in leaves (fonts/fixtures) are logged, not hidden.
    """
    tele: dict[str, Any] = {
        "enabled": True, "applied": False, "threshold": 0,
        "forced_shared_files": 0, "forced_member_rows": 0, "samples": [],
    }
    if not shared_leaf_consistency_enabled():
        tele["enabled"] = False
        return tele
    pf_list = [pf for pf in (product_features or []) if _pf_key(pf)]
    num_pfs = len(pf_list)
    if num_pfs == 0:
        return tele
    threshold = max(SHARED_FLOOR, math.ceil(file_lane_pct() * num_pfs))
    tele["threshold"] = threshold
    surface = _surface_paths(product_features, routes_index)

    # cross-PF claim fan-in + already-shared-somewhere over PF member files.
    claim_pfs: dict[str, set[str]] = defaultdict(set)
    shared_somewhere: set[str] = set()
    for pf in pf_list:
        key = _pf_key(pf) or ""
        for mf in (getattr(pf, "member_files", None) or []):
            path = getattr(mf, "path", None)
            if not path:
                continue
            claim_pfs[path].add(key)
            if getattr(mf, "role", None) == "shared":
                shared_somewhere.add(path)

    forced = {
        p for p, pfs_ in claim_pfs.items()
        if len(pfs_) >= threshold and p not in surface and p in shared_somewhere
    }
    if not forced:
        return tele

    # Force role='shared' on every claimant (PF + dev) — closure/co-commit/
    # url-link -> shared. Deterministic feature order; only non-shared rows move.
    rows = 0
    for feat in list(product_features or []) + list(developer_features or []):
        for mf in (getattr(feat, "member_files", None) or []):
            if getattr(mf, "path", None) in forced \
                    and getattr(mf, "role", None) != "shared":
                mf.role = "shared"
                rows += 1
    tele["applied"] = True
    tele["forced_shared_files"] = len(forced)
    tele["forced_member_rows"] = rows
    tele["samples"] = sorted(forced)[:20]
    return tele


# ── B15b: data-file shared-leaf rail (§4b, re-ratified) ──────────────────────


def _file_suffix(rel: str) -> str:
    base = rel.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def enforce_data_leaf_shared(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    routes_index: list[dict[str, Any]] | None,
    repo_loc: Any,
) -> dict[str, Any]:
    """B15b §4b — force ``role="shared"`` on large shared-DATA leaf files (i18n
    locale packs, template/seed JSON) that the closure BFS attributed as owned
    body. A file qualifies when ALL hold: (1) pure DATA — a non-code extension
    (the engine's OWN code/data boundary via ``code_extensions``; a ``.json`` /
    ``.yaml`` / ``.toml`` has ZERO code symbols by construction); (2) large —
    ``loc >= DATA_LEAF_LOC_FRAC * repo_loc`` (scale-invariant); (3) shared —
    claimed by ``>= 2`` distinct PFs; (4) not a product surface (S3).

    UNLIKE the B15 consistency law this does NOT require the file be
    ``role="shared"`` anywhere — that is why it reaches the closure-EVERYWHERE
    locale blobs B15 could not; the dropped guard is why (1)+(2)+(4) are strict
    and the FULL over-fire list is emitted (``samples``) as the central gate.
    In-place, deterministic. No-op (byte-identical) when off / no repo_loc.
    """
    tele: dict[str, Any] = {
        "enabled": True, "applied": False, "floor_loc": 0,
        "forced_shared_files": 0, "forced_member_rows": 0, "samples": [],
    }
    if not data_leaf_enabled():
        tele["enabled"] = False
        return tele
    if not isinstance(repo_loc, (int, float)) or repo_loc <= 0:
        tele["reason"] = "no repo_loc"
        return tele
    pf_list = [pf for pf in (product_features or []) if _pf_key(pf)]
    if len(pf_list) < 2:
        return tele
    floor = DATA_LEAF_LOC_FRAC * repo_loc
    tele["floor_loc"] = int(floor)
    from faultline.pipeline_v2.spine_anchors import load_spine_vocab
    code_exts = frozenset(load_spine_vocab().get("code_extensions") or ())
    surface = _surface_paths(product_features, routes_index)

    claim_pfs: dict[str, set[str]] = {}
    loc_of: dict[str, float] = {}
    for pf in pf_list:
        key = _pf_key(pf) or ""
        for mf in (getattr(pf, "member_files", None) or []):
            path = getattr(mf, "path", None)
            if not path:
                continue
            claim_pfs.setdefault(path, set()).add(key)
            loc = getattr(mf, "loc", None)
            if isinstance(loc, (int, float)):
                loc_of[path] = max(loc_of.get(path, 0.0), float(loc))

    forced = {
        p for p, pfs_ in claim_pfs.items()
        if len(pfs_) >= 2
        and p not in surface
        and _file_suffix(p) not in code_exts
        and loc_of.get(p, 0.0) >= floor
    }
    if not forced:
        return tele

    rows = 0
    for feat in list(product_features or []) + list(developer_features or []):
        for mf in (getattr(feat, "member_files", None) or []):
            if getattr(mf, "path", None) in forced \
                    and getattr(mf, "role", None) != "shared":
                mf.role = "shared"
                rows += 1
    tele["applied"] = True
    tele["forced_shared_files"] = len(forced)
    tele["forced_member_rows"] = rows
    tele["samples"] = sorted(forced)   # FULL list — the over-fire audit gate
    return tele
