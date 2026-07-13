"""Stage 6.885b — ws-app blob domain-dir member drain (B53 Seg A).

THE CLASS (twenty panel review, 2026-07-12): a ``ws:``-anchored monorepo
package PF (``twenty-front`` 674K, ``twenty-server`` 490K) is a BLOB that
holds product-ink belonging to EXISTING domain PFs of the same board — the
named ``object-record`` / ``page-layout`` / ``messaging`` features carry only
a slice of their real modules, the rest sits in the package blob, and the
journeys 'Manage object record' / 'Manage page layout' home on the blob
DESPITE those PFs existing.

This deterministic, keyless ($0) pass runs AFTER the anchored PF set + stamps
exist (post-6.86) and BEFORE the LOC census (6.97) + the journey re-home
stages. It re-attributes the MEMBER files of a donor blob's internal domain
dirs (``<pkg>/<container>/<domain>/**``) to the EXISTING PF whose identity the
domain name echoes — moving them at the DEVELOPER-FEATURE level so the
existing machinery carries the rest for free:

  * Stage 6.97 owned-LOC (member-dev rollup) shifts to the real PF;
  * the emission ``path_index`` rebuild re-owns the files (dev → pfid);
  * Stage 6.99 I16 re-home (path_index-driven strict-majority) then moves
    the journeys onto their real PF — NO new journey mover is written here
    (B52 conservation law); the I8 orphan guard protects the blob's last UF.

DISCIPLINE (unchanged from B49/B51 + the SACRED anti-cases):

  * NO MINTS — only re-attribution between an EXISTING donor blob and an
    EXISTING domain PF; a domain dir with no product PF stays in the blob
    (honest); the package tile is NOT killed (that is B42, operator-gated).
  * MATCH LAW — the domain name FULL-normalized-matches a PF's anchor-slug /
    name via the SAME matcher (:class:`NamespaceEcho` identity index +
    ``normalize_anchor_key``, banked 2fecf29, reused by B51/B52); plural is
    handled by the singularizer; >1 PF ⇒ AMBIGUOUS skip; a generic token
    (``components``/``utils``/…) ⇒ skip (it IS app-shell content).
  * CROSS-UNIT LAW (B22a) — a file in package A never moves to a PF that owns
    no code in package A (monorepo boundary).
  * IDEMPOTENCE with keyed 8.9.6 — only files whose PRIMARY owner is the donor
    blob are touched; a file already product-homed (non-blob) is left as-is.

Kill-switch ``FAULTLINE_WS_BLOB_DOMAIN_DRAIN=0`` (default OFF) → the pass is a
no-op and scans are byte-identical to main.
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.data import load_yaml
from faultline.pipeline_v2.spine_anchors import (
    normalize_anchor_key,
    owned_paths_of,
)
from faultline.pipeline_v2.transport_handoff import NamespaceEcho

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

WS_BLOB_DRAIN_ENV = "FAULTLINE_WS_BLOB_DOMAIN_DRAIN"

#: I22 provenance tag stamped on every drained member (explainability).
_DRAIN_MARKER = "b53_domain_drain"

#: Ecosystem container conventions (data, not code — YAML source of truth).
_CONTAINERS_FILE = "ws-blob-drain-containers.yaml"

__all__ = [
    "WS_BLOB_DRAIN_ENV",
    "ws_blob_domain_drain_enabled",
    "run_ws_blob_domain_drain",
]


def ws_blob_domain_drain_enabled() -> bool:
    """Default OFF (both Seg A drain and Seg B lane gate on this ONE flag);
    ``FAULTLINE_WS_BLOB_DOMAIN_DRAIN=1`` turns the pass on. OFF is byte-
    identical to main (the kill-switch law)."""
    return os.environ.get(WS_BLOB_DRAIN_ENV, "0").strip().lower() in {
        "1", "true",
    }


@lru_cache(maxsize=1)
def _containers() -> tuple[frozenset[str], frozenset[str]]:
    """``(container segments, generic-token skip set)`` from the packaged
    YAML. Container segments are matched exactly (lowercased); generic
    tokens are ``normalize_anchor_key``-normalized so ``components`` and
    ``component`` collapse to the same skip."""
    data = load_yaml(_CONTAINERS_FILE)
    containers = frozenset(
        str(c).strip().lower()
        for c in (data.get("domain_dir_containers") or [])
        if str(c).strip()
    )
    generic = frozenset(
        normalize_anchor_key(str(g))
        for g in (data.get("generic_domain_tokens") or [])
        if str(g).strip()
    )
    return containers, generic


def _attr(o: Any, name: str, default: Any = None) -> Any:
    return o.get(name, default) if isinstance(o, dict) else getattr(o, name, default)


def _pf_key(pf: Any) -> str | None:
    k = _attr(pf, "id") or _attr(pf, "name")
    return str(k) if k else None


def _domain_dir_of(
    path: str, pkg: str, containers: frozenset[str],
) -> tuple[str, str] | None:
    """``(domain-dir path, domain name)`` for a file living under a
    ``<pkg>/…/<container>/<domain>/…`` subtree, else ``None``.

    The container segment may appear at ANY depth inside the package
    (``<pkg>/src/modules/`` or ``<pkg>/src/engine/core-modules/``); the
    segment right after the FIRST container is the domain dir, and the file
    must live BENEATH that dir (a file literally named after the domain is
    not a domain dir)."""
    if not (path == pkg or path.startswith(pkg + "/")):
        return None
    rel = path[len(pkg) + 1:]
    segs = [s for s in rel.split("/") if s]
    for i, seg in enumerate(segs):
        # need: container at i, domain at i+1, ≥1 further segment (i+2) so the
        # domain is a DIRECTORY that contains this file.
        if seg.lower() in containers and i + 2 < len(segs):
            domain = segs[i + 1]
            domain_dir = pkg + "/" + "/".join(segs[: i + 2])
            return domain_dir, domain
    return None


def _target_owns_in_pkg(
    target_key: str,
    target_pf: Any,
    pkg: str,
    devs_by_pf: dict[str, list[Any]],
) -> bool:
    """B22a cross-unit guard: the matched PF must own ≥1 file inside the
    donor's package ``pkg`` (else the drain would cross a workspace pkg)."""
    pref = pkg + "/"
    for d in devs_by_pf.get(target_key, ()):
        for f in owned_paths_of(d):
            if f == pkg or f.startswith(pref):
                return True
    for p in (_attr(target_pf, "paths") or ()):
        if str(p) == pkg or str(p).startswith(pref):
            return True
    return False


def run_ws_blob_domain_drain(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    user_flows: list[Any],
    flows: list[Any],
    ctx: Any,
) -> dict[str, Any]:
    """Drain ws-blob domain-dir members onto their real PFs. Mutates
    ``developer_features`` / ``product_features`` in place (appends carved
    devs; re-homes / shrinks member ownership). Returns telemetry for
    ``scan_meta.ws_blob_drain``. Never raises for a scan-shaped input — the
    caller still wraps it in try/except (never break a scan)."""
    tele: dict[str, Any] = {
        "enabled": True, "donors": [], "matched_dirs": {},
        "files_moved": 0, "loc_moved": 0, "ufs_rehomed": 0,
        "skipped_generic": 0, "skipped_ambig": 0, "samples": [],
    }
    if not product_features:
        return tele
    containers, generic = _containers()

    pf_by_key: dict[str, Any] = {}
    ws_donor_keys: set[str] = set()
    pkg_of_donor: dict[str, str] = {}
    for pf in product_features:
        k = _pf_key(pf)
        if not k:
            continue
        pf_by_key[k] = pf
        aid = str(_attr(pf, "anchor_id") or "")
        if aid.startswith("ws:"):
            pkg = aid[3:].strip("/")
            if pkg:
                ws_donor_keys.add(k)
                pkg_of_donor[k] = pkg

    # THE matcher (identity index over EXISTING non-blob PFs) — the shared
    # NamespaceEcho pf_by_key + normalize_anchor_key, NOT a name vocabulary.
    echo = NamespaceEcho.build(product_features, frozenset(ws_donor_keys))

    devs = [
        f for f in developer_features
        if _attr(f, "layer", "developer") == "developer" and _attr(f, "name")
    ]
    devs_by_pf: dict[str, list[Any]] = defaultdict(list)
    for d in devs:
        pid = _attr(d, "product_feature_id")
        if pid:
            devs_by_pf[str(pid)].append(d)

    # ── Match domain dirs → existing PFs ────────────────────────────────
    drain_map: dict[str, str] = {}          # file → target PF key
    matched_dirs: dict[str, str] = {}       # domain-dir path → target PF key
    donors_hit: set[str] = set()
    for donor_key, pkg in sorted(pkg_of_donor.items()):
        member_devs = devs_by_pf.get(donor_key, [])
        if not member_devs:
            continue
        files_by_dir: dict[str, list[str]] = defaultdict(list)
        domain_of_dir: dict[str, str] = {}
        for d in member_devs:
            for f in owned_paths_of(d):
                dd = _domain_dir_of(str(f), pkg, containers)
                if dd is None:
                    continue
                files_by_dir[dd[0]].append(str(f))
                domain_of_dir[dd[0]] = dd[1]
        for dir_path in sorted(files_by_dir):
            key = normalize_anchor_key(domain_of_dir[dir_path])
            if not key:
                continue
            if key in generic:
                tele["skipped_generic"] += 1
                continue
            hits = echo.pf_by_key.get(key)
            if not hits:
                continue  # no product surface — the domain stays in the blob
            candidates = {
                h for h in hits if h != donor_key and h not in ws_donor_keys
            }
            if not candidates:
                continue
            if len(candidates) > 1:
                tele["skipped_ambig"] += 1
                continue
            target_key = next(iter(candidates))
            target_pf = pf_by_key.get(target_key)
            if target_pf is None:
                continue
            if not _target_owns_in_pkg(target_key, target_pf, pkg, devs_by_pf):
                continue  # B22a cross-unit boundary
            for f in files_by_dir[dir_path]:
                drain_map.setdefault(f, target_key)
            matched_dirs[dir_path] = target_key
            donors_hit.add(donor_key)

    if not drain_map:
        return tele

    # ── Execute the move at the DEV level ───────────────────────────────
    used_names = {str(_attr(d, "name")) for d in devs}
    new_devs: list[Any] = []
    # donor key → target key → moved files (for PF-level bookkeeping).
    pf_move: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list))
    for d in devs:
        dpf = _attr(d, "product_feature_id")
        if not dpf or str(dpf) not in donors_hit:
            continue
        owned = [str(p) for p in owned_paths_of(d)]
        drained = [f for f in owned if f in drain_map]
        if not drained:
            continue
        donor_key = str(dpf)
        remaining = [f for f in owned if f not in drain_map]
        by_target: dict[str, list[str]] = defaultdict(list)
        for f in drained:
            by_target[drain_map[f]].append(f)
        if not remaining:
            # Whole dev is drained: reassign it wholesale to the target with
            # the most files (flows follow naturally, no empty husk); carve
            # any remaining split targets. Deterministic tie → alpha.
            primary = max(sorted(by_target),
                          key=lambda t: (len(by_target[t]), t))
            _reassign_dev(d, primary, set(by_target[primary]))
            pf_move[donor_key][primary].extend(by_target[primary])
            for tkey in sorted(t for t in by_target if t != primary):
                carved = _make_drain_dev(
                    d, pf_by_key[tkey], tkey, by_target[tkey], used_names)
                used_names.add(str(_attr(carved, "name")))
                new_devs.append(carved)
                pf_move[donor_key][tkey].extend(by_target[tkey])
                # those files leave the (now-reassigned) dev.
                _shrink_dev(d, set(by_target[tkey]))
        else:
            # Straddling dev: carve each target's files; keep the remainder
            # (and the dev's flows) with the donor.
            for tkey in sorted(by_target):
                carved = _make_drain_dev(
                    d, pf_by_key[tkey], tkey, by_target[tkey], used_names)
                used_names.add(str(_attr(carved, "name")))
                new_devs.append(carved)
                pf_move[donor_key][tkey].extend(by_target[tkey])
            _shrink_dev(d, set(drained))

    developer_features.extend(new_devs)

    # ── PF-level scope bookkeeping (mirror provenance_rehome widen) ──────
    for donor_key, tmap in pf_move.items():
        donor_pf = pf_by_key.get(donor_key)
        all_moved: set[str] = set()
        for tkey, files_t in tmap.items():
            all_moved |= set(files_t)
            target_pf = pf_by_key.get(tkey)
            if target_pf is not None:
                _widen_pf(target_pf, files_t, tkey)
        if donor_pf is not None:
            _shrink_pf(donor_pf, all_moved)

    tele["donors"] = sorted(donors_hit)
    tele["matched_dirs"] = dict(sorted(matched_dirs.items()))
    tele["files_moved"] = len(drain_map)
    tele["loc_moved"] = _count_loc(drain_map, ctx)
    # Drain-time PROJECTION of the Stage-6.99 I16 effect (the authoritative
    # count is scan_meta.i16_rehome.rehomed): journeys homed on a donor whose
    # member-flow entries are now strict-majority drained.
    tele["ufs_rehomed"] = _project_rehomes(
        user_flows, developer_features, drain_map, donors_hit)
    tele["samples"] = [
        {"dir": k, "pf": v} for k, v in list(tele["matched_dirs"].items())[:20]
    ]
    return tele


# ── move mechanics (mirror provenance_rehome / lane_excavation) ───────────


def _reassign_dev(dev: Any, target_key: str, moved: set[str]) -> None:
    """Wholesale re-home a fully-drained dev onto ``target_key`` and stamp
    the drain provenance on the moved members (I22)."""
    dev.product_feature_id = target_key
    for m in (_attr(dev, "member_files") or []):
        p = m.get("path") if isinstance(m, dict) else getattr(m, "path", None)
        if p in moved:
            ev = _attr(m, "evidence") or ""
            marked = f"{ev} [{_DRAIN_MARKER}->{target_key}]".strip()
            if isinstance(m, dict):
                m["evidence"] = marked
            else:
                setattr(m, "evidence", marked)


def _make_drain_dev(
    shell: Any, target_pf: Any, target_key: str,
    files: list[str], used_names: set[str],
) -> Any:
    """A carved member dev under ``target_pf`` claiming ``files`` as primary
    (content-derived uuid, ``split_from`` lineage, ``fold:`` anchor, no
    flows) — mirrors ``provenance_rehome._make_rehome_dev`` with the B53
    marker. Supports pydantic Features and lightweight stubs."""
    base = f"{target_key}-{_DRAIN_MARKER}"
    name = base
    n = 2
    while name in used_names:
        name = f"{base}{n}"
        n += 1
    uuid = hashlib.sha256(
        f"ws-blob-drain-v1|{target_key}|{'|'.join(sorted(files))}".encode(
            "utf-8")).hexdigest()[:32]
    anchor = f"fold:{_DRAIN_MARKER}->{_attr(target_pf, 'anchor_id') or target_key}"
    evidence = f"{_DRAIN_MARKER}: drained into '{target_key}'"

    if hasattr(shell, "model_copy"):
        from faultline.models.types import MemberFile
        members = [
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True,
                       evidence=evidence)
            for p in sorted(files)
        ]
        return shell.model_copy(deep=True, update={
            "name": name, "display_name": name,
            "paths": sorted(files), "member_files": members,
            "product_feature_id": target_key,
            "description": f"{_DRAIN_MARKER} carve into '{target_key}'.",
            "uuid": uuid, "split_from": getattr(shell, "uuid", None),
            "anchor_id": anchor, "shared_reason": None,
            "previous_names": [], "merged_from": [],
            "total_commits": 0, "bug_fixes": 0, "bug_fix_ratio": 0.0,
            "flows": [], "shared_participants": [], "shared_attributions": [],
            "symbol_attributions": [], "hotspot_files": [], "participants": [],
            "history": None,
        })

    from types import SimpleNamespace
    ch: Any = SimpleNamespace()
    ch.layer = "developer"
    ch.name = name
    ch.display_name = name
    ch.uuid = uuid
    ch.paths = sorted(files)
    ch.member_files = [
        {"path": p, "role": "anchor", "confidence": 1.0, "primary": True,
         "evidence": evidence}
        for p in sorted(files)
    ]
    ch.flows = []
    ch.product_feature_id = target_key
    ch.anchor_id = anchor
    ch.shared_reason = None
    ch.split_from = _attr(shell, "uuid")
    return ch


def _shrink_dev(dev: Any, moved: set[str]) -> None:
    """Drop ``moved`` from a dev's ``paths`` + ``member_files``."""
    dev.paths = [p for p in (_attr(dev, "paths") or []) if p not in moved]
    kept = []
    for m in (_attr(dev, "member_files") or []):
        p = m.get("path") if isinstance(m, dict) else getattr(m, "path", None)
        if p not in moved:
            kept.append(m)
    dev.member_files = kept


def _widen_pf(pf: Any, files: list[str], target_key: str) -> None:
    """Add ``files`` to the PF's ``paths`` + ``member_files`` (so PF scope +
    validators see them regardless of member-dev traversal)."""
    seen_paths = set(_attr(pf, "paths") or [])
    pf.paths = list(_attr(pf, "paths") or []) + [
        p for p in files if p not in seen_paths]
    mf_out = list(_attr(pf, "member_files") or [])
    seen_mf = {
        (m.get("path") if isinstance(m, dict) else getattr(m, "path", None))
        for m in mf_out
    }
    evidence = f"{_DRAIN_MARKER}: drained into '{target_key}'"
    if mf_out and isinstance(mf_out[0], dict):
        for p in sorted(files):
            if p not in seen_mf:
                seen_mf.add(p)
                mf_out.append({"path": p, "role": "anchor", "confidence": 1.0,
                               "primary": True, "evidence": evidence})
    else:
        from faultline.models.types import MemberFile
        for p in sorted(files):
            if p not in seen_mf:
                seen_mf.add(p)
                mf_out.append(MemberFile(
                    path=p, role="anchor", confidence=1.0, primary=True,
                    evidence=evidence))
    pf.member_files = mf_out


def _shrink_pf(pf: Any, moved: set[str]) -> None:
    """Drop ``moved`` from the donor PF's ``paths`` + ``member_files``."""
    pf.paths = [p for p in (_attr(pf, "paths") or []) if p not in moved]
    kept = []
    for m in (_attr(pf, "member_files") or []):
        p = m.get("path") if isinstance(m, dict) else getattr(m, "path", None)
        if p not in moved:
            kept.append(m)
    pf.member_files = kept


def _count_loc(drain_map: dict[str, str], ctx: Any) -> int:
    """Non-blank-line count of the drained files (mirrors 6.97's counting).
    Best-effort — an unreadable file contributes 0, never raises."""
    root = Path(str(_attr(ctx, "repo_path", ".") or "."))
    total = 0
    for f in drain_map:
        try:
            text = (root / f).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        total += sum(1 for ln in text.splitlines() if ln.strip())
    return total


def _project_rehomes(
    user_flows: list[Any], developer_features: list[Any],
    drain_map: dict[str, str], donors_hit: set[str],
) -> int:
    """Projected count of donor-homed journeys whose member-flow entries are
    now strict-majority drained (the Stage-6.99 I16 move, projected)."""
    flow_by_uuid: dict[str, Any] = {}
    for f in developer_features:
        for fl in (_attr(f, "flows") or []):
            u = _attr(fl, "uuid")
            if u:
                flow_by_uuid[str(u)] = fl
    count = 0
    for uf in user_flows:
        home = _attr(uf, "product_feature_id")
        if not home or str(home) not in donors_hit:
            continue
        chk = drained = 0
        for fid in (_attr(uf, "member_flow_ids") or []):
            fl = flow_by_uuid.get(str(fid))
            ep = _attr(fl, "entry_point_file") if fl is not None else None
            if not ep:
                continue
            chk += 1
            if str(ep) in drain_map:
                drained += 1
        if chk and drained * 2 > chk:
            count += 1
    return count
