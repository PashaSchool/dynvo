"""Stage 6.885 — provenance re-home (Track-A A1, W6-AST).

Lane excavation (6.87) lifts product out of the ``platform_infrastructure``
lane by DIRECTORY anchors: a lane file joins a PF only when it lives under a
domain-named subtree. That misses the dominant monorepo shape the
w6ast/Track-A diagnosis measured — a THIN route-anchored product feature
(openstatus ``Email`` = 9 owned LOC on ``api/internal/email``) whose real
work-code lives in a SIBLING app the mint laned (the
``apps/workflows/src/cron/emails.ts`` sender). Directory anchors never
connect the two; the ts_ast import graph does: the sender and the PF's own
code BOTH import the first-party domain package ``@openstatus/emails``.

The ruler this fixes is I16 (entry-owner consistency): a journey is
misattached when its member flows' entry files are owned by a PF other than
the journey's own. The failure is a LANE entry — the work-code the journey
correctly recruited sits in the lane, so its entry reads foreign to EVERY
product PF, the journey's included.

This stage runs AFTER the user-flow layer settles, so it can be
CONFIRMATION-GATED and therefore never manufacture a new foreign entry — the
class of regression a pre-UF re-home caused (moving a file to a PF whose
journey did not claim it). A lane entry file re-homes to product PF ``P``
ONLY when ALL of:

  * PROVENANCE agrees — the entry reaches, through a first-party WORKSPACE
    package (``resolution="workspace"``: the cross-package DOMAIN
    dependency, never a same-app relative/alias util), a domain package that
    EXACTLY ONE product PF's owned code also imports, and ``P`` is the
    strict-unique such PF (ties abstain — no membership oracle);
  * the JOURNEY layer agrees — every user flow that carries a member flow
    whose entry is this file is homed on ``P`` (unanimous; a file shared by
    journeys on different PFs is left laned);
  * the file is currently LANE/unowned — we never contest a sibling PF's
    existing primary claim.

The confirmed entry (deterministically) leaves its lane dev for a member dev
under ``P`` (mirroring 6.87's ``_make_excav_dev`` field discipline), so
Stage 6.97 recomputes ``P``'s owned LOC + ``file_owner_pf`` sees the entry
as ``P``-owned. By construction the move only turns FOREIGN entries NATIVE
(I16 ↓); it cannot create a foreign one, and it never removes a PF's code.
Conservation: the entry MOVES (never duplicates); the lane dev keeps its
residual.

DUAL-LANGUAGE PROVENANCE (Track-B integration). The re-home reads BOTH
import graphs, dispatched by entry-file language: TS/JS entries resolve
through ``ts_ast`` (cross-package pnpm ``@scope/pkg`` domain imports);
``.py`` entries resolve through ``py_ast`` (absolute first-party module
imports — Soc0 backend, onyx FastAPI, horilla Django). Both providers
project the IDENTICAL ``ProvenanceView`` shape (py_ast re-exports the
ts_ast view class by design), so the confirmation gate — single-PF
domain package + journey unanimity + lane-owned — is ONE code path over
both languages. The domain grain differs only in how a target file rolls
up to its "package": TS uses the pnpm workspace map; Python uses the
target's immediate package directory (siblings = one domain module,
mirroring the ts_ast package-root aggregation). A file's provenance is
read from its OWN language's graph, so a mixed repo (Next front-end +
FastAPI back-end) re-homes each side through the right graph.

Requires ``FAULTLINE_TS_AST`` and/or ``FAULTLINE_PY_AST`` (at least one
graph) and runs BEFORE ``build_path_index`` / emission integrity, so the
emitted ``path_index`` — the validator's ``file_owner_pf`` source —
reflects the correction. Deterministic, $0 LLM. Kill-switch
``FAULTLINE_PROV_ATTACH=0`` → scans byte-identical to pre-Track-A (the
stage is skipped at the call site: no view built, no move made). Setting
``FAULTLINE_PY_AST=0`` restores the TS-only Track-A behaviour byte-
identically (the Python provider yields no view → no Python re-home).
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.spine_anchors import owned_paths_of

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

logger = logging.getLogger(__name__)

__all__ = [
    "PROV_ATTACH_ENV",
    "prov_attach_enabled",
    "run_provenance_rehome",
]

PROV_ATTACH_ENV = "FAULTLINE_PROV_ATTACH"

#: provenance marker (idempotence + I22 explainability).
_REHOME_MARKER = "provenance-rehome"

_TS_JS_SUFFIXES = (
    ".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs",
)
_DECL_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")
_PY_SUFFIXES = (".py",)
_PY_STUB_SUFFIXES = (".pyi",)


def prov_attach_enabled() -> bool:
    """Default ON; ``FAULTLINE_PROV_ATTACH=0`` restores pre-Track-A output
    byte-identically (this stage becomes a no-op)."""
    return os.environ.get(PROV_ATTACH_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _is_ts_js(rel: str) -> bool:
    low = rel.lower()
    return low.endswith(_TS_JS_SUFFIXES) and not low.endswith(_DECL_SUFFIXES)


def _is_py(rel: str) -> bool:
    """A Python source file py_ast provenance covers (``.pyi`` stubs are
    excluded — they carry no runtime imports, mirroring py_ast's own filter)."""
    low = rel.lower()
    return low.endswith(_PY_SUFFIXES) and not low.endswith(_PY_STUB_SUFFIXES)


def _is_prov_src(rel: str) -> bool:
    """A source file EITHER provenance graph can speak for (TS/JS or Python)."""
    return _is_ts_js(rel) or _is_py(rel)


class _UnifiedProvenance:
    """Language-dispatched provenance over the two ``ProvenanceView`` graphs.

    ``workspace_targets(src)`` routes to the ts_ast view for TS/JS files and
    the py_ast view for ``.py`` files. Each underlying view holds ONLY its own
    language's edges, so a mis-routed query would return empty anyway — the
    dispatch just makes intent explicit and skips a needless lookup. Either
    view may be ``None`` (that language's graph off / unavailable) → empty.
    The two views share ONE class (py_ast re-exports ts_ast's), so the caller
    reads a single interface regardless of language. Determinism: each
    delegate returns a canonical ``frozenset`` and the outcome logic in
    :func:`run_provenance_rehome` is order-invariant (ties abstain)."""

    __slots__ = ("_ts", "_py")

    def __init__(self, ts_view: Any, py_view: Any) -> None:
        self._ts = ts_view
        self._py = py_view

    def workspace_targets(self, src_file: str) -> frozenset[str]:
        if _is_ts_js(src_file):
            return (self._ts.workspace_targets(src_file)
                    if self._ts is not None else frozenset())
        if _is_py(src_file):
            return (self._py.workspace_targets(src_file)
                    if self._py is not None else frozenset())
        return frozenset()


def _pf_key(pf: Any) -> str | None:
    return getattr(pf, "id", None) or getattr(pf, "name", None)


def run_provenance_rehome(
    user_flows: list[Any],
    developer_features: list["Feature"],
    product_features: list["Feature"],
    ctx: Any,
) -> dict[str, Any]:
    """See module docstring. Mutates lane devs / ``product_features`` in
    place and appends member devs to ``developer_features``. Returns
    telemetry for ``scan_meta.provenance_rehome``."""
    # Both providers are imported at CALL time (inside the function) so a
    # per-language monkeypatch on the adapter module takes effect (the test
    # discipline the ts-only stage already relied on).
    from faultline.pipeline_v2.ts_ast.adapter import (
        repo_provenance as _ts_repo_provenance,
        ts_ast_enabled,
    )
    from faultline.pipeline_v2.py_ast.adapter import (
        py_ast_enabled,
        repo_provenance as _py_repo_provenance,
    )

    tele: dict[str, Any] = {
        "enabled": True, "applied": False,
        "entries_confirmed": 0, "entries_rehomed": 0, "pfs_widened": 0,
        "skipped_journey_conflict": 0, "skipped_owned": 0,
        "abstained_ties": 0, "samples": [],
    }
    ts_on = ts_ast_enabled()
    py_on = py_ast_enabled()
    if not ts_on and not py_on:
        tele["enabled"] = False
        tele["reason"] = "ts_ast and py_ast disabled"
        return tele
    tracked = frozenset(
        str(p).replace("\\", "/")
        for p in (getattr(ctx, "tracked_files", None) or [])
    )
    if not tracked:
        return tele
    repo_path = Path(getattr(ctx, "repo_path", "."))
    # One provenance graph per language present; each provider returns None
    # when its flag is off / its toolchain is absent / the build failed
    # (fallback law) — the other language still re-homes.
    ts_view = _ts_repo_provenance(str(repo_path), tracked) if ts_on else None
    py_view = _py_repo_provenance(str(repo_path), tracked) if py_on else None
    if ts_view is None and py_view is None:  # nothing to read → no move
        tele["enabled"] = False
        tele["reason"] = "no provenance view"
        return tele
    tele["providers"] = {"ts": ts_view is not None, "py": py_view is not None}
    view = _UnifiedProvenance(ts_view, py_view)

    devs = [
        f for f in developer_features
        if getattr(f, "layer", "developer") == "developer"
        and getattr(f, "name", None)
    ]
    pf_by_key = {}
    for pf in product_features or []:
        k = _pf_key(pf)
        if k:
            pf_by_key[k] = pf

    # file -> owning product-PF key (primary claims of PF-homed devs);
    # file -> owning LANE dev (unhomed dev primary claim).
    file_owner_pf: dict[str, str] = {}
    lane_owner_of: dict[str, "Feature"] = {}
    for f in devs:
        pfid = getattr(f, "product_feature_id", None)
        owned = owned_paths_of(f)
        if pfid and pfid in pf_by_key:
            for p in owned:
                file_owner_pf.setdefault(p, pfid)
        elif pfid is None:
            for p in owned:
                lane_owner_of.setdefault(p, f)

    # PACKAGE-root aggregation: a workspace target file
    # (``packages/db/src/schema/integration.ts``) is domain evidence for its
    # PACKAGE (``packages/db``), not for itself — so a SHARED infra package
    # (``@scope/db``, imported by many PFs) is ambiguous as a whole even when
    # an individual FILE inside it is coincidentally imported by one PF
    # (the openstatus tie: a lone db-schema file tied ``notification-slack``
    # with the real ``@scope/emails`` domain signal and abstained the fix).
    from faultline.analyzer.import_graph import detect_workspace_package_map
    from faultline.pipeline_v2.ts_ast.resolve import _ws_deep_glob_enabled
    ws_dirs = sorted(
        set(detect_workspace_package_map(
            str(repo_path), deep=_ws_deep_glob_enabled()).values()),
        key=lambda d: (-len(d), d),
    )

    def _pkg_root(tgt: str) -> str:
        if _is_ts_js(tgt):
            for d in ws_dirs:  # longest dir prefix wins (nested pkgs)
                if tgt == d or tgt.startswith(d + "/"):
                    return d
            return tgt  # not under a known workspace pkg — its own root
        # Python (and any non-TS target): there is no pnpm workspace map, so
        # the domain unit is the target's IMMEDIATE package DIRECTORY. Sibling
        # modules of one domain (``recruitment/models.py`` +
        # ``recruitment/views.py``) roll up together, so a lone module inside
        # a SHARED package cannot coincidentally out-vote a real single-PF
        # domain package — the ts_ast 949114c package-root fix, ported to
        # Python's directory-is-package layout. Pure path op (deterministic).
        slash = tgt.rfind("/")
        return tgt[:slash] if slash > 0 else tgt

    # DOMAIN co-import index — the ONLY provenance channel (see docstring):
    # each first-party WORKSPACE PACKAGE (resolution="workspace", aggregated
    # to its package root) and the product PFs whose OWNED code imports it
    # cross-package. A package imported by EXACTLY ONE PF is that PF's domain
    # package; a ubiquitous one has many PF importers → ambiguous → no
    # evidence. NOT direct-ownership of imported files (mint-placement noise)
    # and NOT relative/alias imports (same-app locals).
    pkg_pf_importers: dict[str, set[str]] = defaultdict(set)
    for src, pfid in file_owner_pf.items():
        if not _is_prov_src(src):
            continue
        for tgt in view.workspace_targets(src):
            pkg_pf_importers[_pkg_root(tgt)].add(pfid)
    # DIAGNOSTIC (deterministic): the provenance-signal richness — how many
    # domain package-roots are imported by EXACTLY ONE PF (the raw material a
    # re-home needs). Zero here means the corpus offers no single-PF domain
    # evidence at all (keyless journeys often lack the PF-owned import surface).
    tele["single_pf_packages"] = sum(
        1 for imp in pkg_pf_importers.values() if len(imp) == 1)
    tele["lane_prov_files"] = sum(1 for p in lane_owner_of if _is_prov_src(p))

    def _attraction(entry: str) -> str | None:
        """The product PF an entry file provenance-attracts to — strict
        unique argmax over the DISTINCT workspace PACKAGES it imports that
        are owned by a SINGLE PF, else None (no evidence / tie / ambiguous
        shared package)."""
        evidence: Counter[str] = Counter()
        seen_roots: set[str] = set()
        for tgt in view.workspace_targets(entry):
            root = _pkg_root(tgt)
            if root in seen_roots:
                continue
            seen_roots.add(root)
            importers = pkg_pf_importers.get(root)
            if importers and len(importers) == 1:
                evidence[next(iter(importers))] += 1
        if not evidence:
            return None
        ranked = evidence.most_common(2)
        winner, wc = ranked[0]
        if wc < 1:
            return None
        if len(ranked) > 1 and ranked[1][1] == wc:
            tele["abstained_ties"] += 1
            return None
        return winner

    # flow uuid -> entry file (flows live on dev features pre-emission).
    entry_of_flow: dict[str, str] = {}
    for f in devs:
        for fl in getattr(f, "flows", None) or []:
            mid = getattr(fl, "uuid", None) or getattr(fl, "id", None)
            ep = getattr(fl, "entry_point_file", None)
            if mid and ep:
                entry_of_flow[str(mid)] = str(ep)

    # For every member-flow entry, the SET of journey PFs it appears under.
    # A re-home requires this set to be a single PF (journey unanimity), so
    # the move can never make the entry foreign to a co-owning journey.
    entry_journey_pfs: dict[str, set[str]] = defaultdict(set)
    for uf in user_flows or []:
        pfid = getattr(uf, "product_feature_id", None)
        if not pfid or pfid not in pf_by_key:
            continue
        for mid in getattr(uf, "member_flow_ids", None) or []:
            ep = entry_of_flow.get(str(mid))
            if ep:
                entry_journey_pfs[ep].add(pfid)

    # Confirmed re-homes: entry E -> PF P.  DIAGNOSTIC counters (deterministic,
    # order-invariant) explain a 0-fire: how many journey-carried entries were
    # examined, how many reached the provenance test (unanimous + lane-owned +
    # prov-language), and how many of THOSE found no single-PF domain evidence.
    confirmed: dict[str, str] = {}
    tele["entries_examined"] = len(entry_journey_pfs)
    lane_candidates = 0
    abstained_no_attraction = 0
    for ep, journey_pfs in entry_journey_pfs.items():
        if len(journey_pfs) != 1:
            if len(journey_pfs) > 1:
                tele["skipped_journey_conflict"] += 1
            continue
        p = next(iter(journey_pfs))
        if file_owner_pf.get(ep) is not None:  # a PF already owns it
            if file_owner_pf.get(ep) != p:
                tele["skipped_owned"] += 1
            continue
        if not _is_prov_src(ep) or ep not in lane_owner_of:
            continue
        lane_candidates += 1
        if _attraction(ep) == p:
            tele["entries_confirmed"] += 1
            confirmed[ep] = p
        else:
            abstained_no_attraction += 1
    tele["lane_candidates"] = lane_candidates
    tele["abstained_no_attraction"] = abstained_no_attraction

    if not confirmed:
        return tele
    # language split of the confirmed re-homes (Track-B integration telemetry).
    tele["confirmed_ts"] = sum(1 for ep in confirmed if _is_ts_js(ep))
    tele["confirmed_py"] = sum(1 for ep in confirmed if _is_py(ep))

    used_dev_names = {f.name for f in devs}
    by_pf: dict[str, list[str]] = defaultdict(list)
    for ep, p in confirmed.items():
        by_pf[p].append(ep)

    for p in sorted(by_pf):
        files = sorted(by_pf[p])
        pf = pf_by_key[p]
        # a representative lane shell to clone field discipline from.
        shell = lane_owner_of[files[0]]
        rehome = _make_rehome_dev(shell, pf, files, used_dev_names)
        used_dev_names.add(rehome.name)
        for ep in files:
            _shrink_lane_dev(lane_owner_of[ep], {ep})
        _widen_pf(pf, rehome, files)
        developer_features.append(rehome)
        tele["entries_rehomed"] += len(files)
        tele["pfs_widened"] += 1
        if len(tele["samples"]) < 20:
            tele["samples"].append({
                "pf": p, "entries": len(files), "sample_entry": files[0],
            })

    tele["applied"] = bool(tele["entries_rehomed"])
    return tele


# ── move mechanics (mirror lane_excavation 8.9.x discipline) ──────────────


def _make_rehome_dev(
    shell: "Feature", pf: Any, files: list[str], used_names: set[str],
) -> "Feature":
    """A re-home member dev under *pf* claiming *files* as primary — mirrors
    ``lane_excavation._make_excav_dev`` (content-derived uuid, owned primary
    members, ``split_from`` lineage) with the provenance marker."""
    from faultline.models.types import MemberFile

    pfid = _pf_key(pf)
    base = f"{pfid}-prov"
    name = base
    n = 2
    while name in used_names:
        name = f"{base}{n}"
        n += 1
    owned_members = [
        MemberFile(
            path=p, role="anchor", confidence=1.0, primary=True,
            evidence=f"{_REHOME_MARKER}: entry imports {pfid} domain code",
        )
        for p in sorted(files)
    ]
    return shell.model_copy(deep=True, update={
        "name": name,
        "display_name": name,
        "paths": sorted(files),
        "member_files": owned_members,
        "product_feature_id": pfid,
        "description": (
            f"{_REHOME_MARKER} into '{pfid}' (import-graph provenance; "
            f"journey-confirmed entry)."
        ),
        "uuid": hashlib.sha256(
            f"prov-rehome-v1|{pfid}|{'|'.join(sorted(files))}".encode("utf-8")
        ).hexdigest()[:32],
        "split_from": getattr(shell, "uuid", None),
        "anchor_id": f"fold:prov-rehome->{getattr(pf, 'anchor_id', '') or pfid}",
        "shared_reason": None,
        "previous_names": [],
        "merged_from": [],
        "total_commits": 0,
        "bug_fixes": 0,
        "bug_fix_ratio": 0.0,
        "flows": [],
        "shared_participants": [],
        "shared_attributions": [],
        "symbol_attributions": [],
        "hotspot_files": [],
        "participants": [],
        "history": None,
    })


def _shrink_lane_dev(shell: "Feature", moved: set[str]) -> None:
    """Drop *moved* from the lane dev's ``paths`` + ``member_files``
    (mirror ``lane_excavation._remove_files_from_shell``)."""
    shell.paths = [p for p in (shell.paths or []) if p not in moved]
    mfs = getattr(shell, "member_files", None) or []
    kept = []
    for m in mfs:
        p = m.get("path") if isinstance(m, dict) else getattr(m, "path", None)
        if p not in moved:
            kept.append(m)
    shell.member_files = kept


def _widen_pf(pf: Any, rehome: "Feature", files: list[str]) -> None:
    """Add the re-homed files to the PF's own ``paths`` + ``member_files``
    (mirror ``lane_excavation`` widen), so PF scope + Stage 6.97 owned LOC
    see them regardless of member-dev traversal."""
    seen_paths = set(pf.paths or [])
    pf.paths = list(pf.paths or []) + [p for p in files if p not in seen_paths]
    mf_out = list(getattr(pf, "member_files", None) or [])
    seen_mf = {
        (m.get("path") if isinstance(m, dict) else getattr(m, "path", None))
        for m in mf_out
    }
    for m in rehome.member_files or []:
        mp = (m.get("path") if isinstance(m, dict)
              else getattr(m, "path", None))
        if mp and mp not in seen_mf:
            seen_mf.add(mp)
            mf_out.append(m)
    pf.member_files = mf_out
