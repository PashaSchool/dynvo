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
stages. It re-attributes the member files of a donor blob's internal domain
dirs (``<pkg>/<container>/<domain>/**``) to the EXISTING PF whose identity the
domain name echoes — moving them at the DEVELOPER-FEATURE level so the
existing machinery carries the rest for free:

  * Stage 6.97 owned-LOC (member-dev rollup) shifts to the real PF;
  * the emission ``path_index`` rebuild re-owns the files (dev → pfid);
  * Stage 6.99 I16 re-home (path_index-driven strict-majority) then moves
    the journeys onto their real PF — NO new journey mover is written here
    (B52 conservation law); the I8 orphan guard protects the blob's last UF.

V2 (census forensics, 2026-07-13): blob LOC is IMPLICIT SUBTREE mass —
dev ``paths`` carry DIRECTORY entries and Stage 6.97
``_expand_feature_files`` expands them recursively, so every
path_index-UNATTRIBUTED file under the ws-package rolls to the blob
(twenty-front: 8,574 tracked files, 4,165 in path_index, 4,409
unattributed ⇒ Σpaths=1055 yet Σloc=674K). Draining only explicit
member-dev files (v1) moved ~8.5K LOC of >100K available. V2 therefore:

  * MOVE-SET per matched domain dir = donor-dev-owned explicit files
    (v1) ∪ tracked files ABSENT from ``path_index`` (unattributed ⇒
    donor's by the same subtree rule that gives the blob its LOC), minus
    any file with a non-donor ``path_index`` entry or a live non-donor
    dev claim (explicit or dir-prefix) — the drain NEVER touches another
    feature's property (SACRED + keyed-8.9.6 idempotency: on a keyed
    re-scan those files carry entries/claims and are skipped).
  * CARVE devs claim the EXPLICIT FILE LIST only (never a directory) —
    a dir path would expand over other devs' files and could steal them
    through the 6.97 ``_primary`` dircount tiebreak (dircount favours
    whole-dir holders); an explicit list creates no contest.
  * DONOR ANCESTOR CARVE-OUT (load-bearing): a donor dev path that is an
    ANCESTOR directory of a drained domain dir would keep covering the
    moved files via expansion (and keep winning ``_primary``); each such
    path is rewritten into its child paths minus the drained subtrees
    (recursive split along the ancestor chain only, deterministic sorted
    output). Without this the LOC does not shift — the v1 shortfall.

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

#: Structured dev-level carve marker (v4): every carve dev's ``anchor_id``
#: carries this prefix — set by :func:`_make_drain_dev` on BOTH the
#: pydantic and stub branches, content-addressed, serialization-schema
#: neutral (no new model field). The anchored-husk pass keys its ORGANIC
#: mass test on this predicate — never on a name suffix.
_CARVE_ANCHOR_PREFIX = f"fold:{_DRAIN_MARKER}->"

#: Ecosystem container conventions (data, not code — YAML source of truth).
_CONTAINERS_FILE = "ws-blob-drain-containers.yaml"

#: Lane sentinel for the non-donor claim index (a pfid=None dev's claim
#: blocks a drain conservatively — lane property is not the donor's).
_LANE_TAG = "__lane__"

__all__ = [
    "WS_BLOB_DRAIN_ENV",
    "is_drain_carve_dev",
    "ws_blob_domain_drain_enabled",
    "run_ws_blob_domain_drain",
]


def is_drain_carve_dev(dev: Any) -> bool:
    """Is *dev* a B53 drain-carve member dev? Structured marker: the
    ``fold:b53_domain_drain-><target-anchor>`` ``anchor_id`` prefix (the
    carve constructor stamps it deterministically). Used by the
    emission-integrity anchored-husk pass to judge husk candidacy on
    ORGANIC mass only — drain-contributed mass must never spare a PF row
    the OFF board would drop (PF-survival invariance; typebot Popup).
    Boards with no drain marks return False everywhere → that pass stays
    byte-identical."""
    aid = (dev.get("anchor_id") if isinstance(dev, dict)
           else getattr(dev, "anchor_id", None))
    return str(aid or "").startswith(_CARVE_ANCHOR_PREFIX)


def ws_blob_domain_drain_enabled() -> bool:
    """Default ON (flipped B62, KEY_SCHEMA 29; both Seg A drain and Seg B
    lane gate on this ONE flag). ``FAULTLINE_WS_BLOB_DOMAIN_DRAIN=0`` turns
    the pass off — byte-identical to pre-B53 main (the kill-switch law)."""
    return os.environ.get(WS_BLOB_DRAIN_ENV, "1").strip().lower() in {
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


def _ancestors_of(path: str) -> list[str]:
    """Every proper ancestor dir prefix of *path* (shallow→deep)."""
    segs = path.split("/")
    return ["/".join(segs[:i]) for i in range(1, len(segs))]


def _under_any(path: str, dirs: list[str]) -> bool:
    return any(path == d or path.startswith(d + "/") for d in dirs)


def run_ws_blob_domain_drain(
    developer_features: list["Feature"],
    product_features: list["Feature"],
    user_flows: list[Any],
    flows: list[Any],
    ctx: Any,
    path_index: Any = None,
) -> dict[str, Any]:
    """Drain ws-blob domain-dir members onto their real PFs. Mutates
    ``developer_features`` / ``product_features`` in place (appends carved
    devs; re-homes / shrinks member ownership). Returns telemetry for
    ``scan_meta.ws_blob_drain``. Never raises for a scan-shaped input — the
    caller still wraps it in try/except (never break a scan).

    ``path_index`` = the Stage 6.8 lineage index (literal path →
    ``{feature_uuid, …}``); a file carrying a NON-donor entry is never
    touched. ``ctx.tracked_files`` powers the v2 subtree channel + the
    ancestor carve-out; when absent (unit stubs), the pass degrades to the
    v1 explicit-member behaviour."""
    tele: dict[str, Any] = {
        "enabled": True, "donors": [], "matched_dirs": {},
        "files_moved": 0, "files_moved_unattributed": 0, "loc_moved": 0,
        "ufs_rehomed": 0, "skipped_generic": 0, "skipped_ambig": 0,
        "skipped_pi_foreign": 0, "skipped_nondonor_covered": 0,
        "samples": [],
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

    # ── v2 inputs: tracked population + literal path_index + claim index ─
    tracked = sorted({
        str(p).replace("\\", "/")
        for p in (_attr(ctx, "tracked_files", None) or []) if p
    })
    prefix_set: set[str] = set()
    for f in tracked:
        prefix_set.update(_ancestors_of(f))
    pi_owner: dict[str, str] = {}
    if isinstance(path_index, dict):
        for p, e in path_index.items():
            fu = (e.get("feature_uuid") if isinstance(e, dict)
                  else _attr(e, "feature_uuid", ""))
            if fu:
                pi_owner[str(p)] = str(fu)

    # Live non-donor claim index (the dev ledger is the post-6.8 truth):
    # every developer-layer, non-facet dev's path claims, tagged by its
    # pfid (None → lane sentinel). Facets never win 6.97 primary, so their
    # claims don't block a drain (mirror of the 6.97 non-facet-first rule).
    from faultline.pipeline_v2.spine_hygiene import is_facet
    exact_claims: dict[str, set[str]] = defaultdict(set)
    dir_claims: dict[str, set[str]] = defaultdict(set)
    for d in devs:
        if is_facet(d):
            continue
        tag = str(_attr(d, "product_feature_id") or _LANE_TAG)
        for p in (_attr(d, "paths") or []):
            sp = str(p)
            exact_claims[sp].add(tag)
            dir_claims[sp].add(tag)

    def _foreign_claim(f: str, donor_key: str) -> bool:
        tags = set(exact_claims.get(f, ()))
        for anc in _ancestors_of(f):
            tags |= dir_claims.get(anc, set())
        return any(t != donor_key for t in tags)

    # ── Discover domain dirs (explicit + subtree channels) ──────────────
    explicit_by_dir: dict[str, set[str]] = defaultdict(set)
    tracked_by_dir: dict[str, set[str]] = defaultdict(set)
    domain_of_dir: dict[str, str] = {}
    donor_of_dir: dict[str, str] = {}
    donor_uuid_sets: dict[str, frozenset[str]] = {}
    for donor_key, pkg in sorted(pkg_of_donor.items()):
        member_devs = devs_by_pf.get(donor_key, [])
        if not member_devs:
            continue
        donor_uuid_sets[donor_key] = frozenset(
            {str(_attr(d, "uuid") or "") for d in member_devs}
            | {str(_attr(pf_by_key[donor_key], "uuid") or "")}
        ) - {""}
        # explicit channel (v1): donor member devs' owned entries.
        for d in member_devs:
            for fp in owned_paths_of(d):
                dd = _domain_dir_of(str(fp), pkg, containers)
                if dd is None:
                    continue
                explicit_by_dir[dd[0]].add(str(fp))
                domain_of_dir[dd[0]] = dd[1]
                donor_of_dir.setdefault(dd[0], donor_key)
        # subtree channel (v2): the tracked population under the package —
        # the same implicit territory whose LOC rolls to the blob at 6.97.
        pref = pkg + "/"
        for f in tracked:
            if not f.startswith(pref):
                continue
            dd = _domain_dir_of(f, pkg, containers)
            if dd is None:
                continue
            tracked_by_dir[dd[0]].add(f)
            domain_of_dir.setdefault(dd[0], dd[1])
            donor_of_dir.setdefault(dd[0], donor_key)

    # ── Match laws (unchanged from v1/B49/B51) ──────────────────────────
    matched: dict[str, str] = {}  # domain-dir → target PF key
    for dir_path in sorted(domain_of_dir):
        donor_key = donor_of_dir[dir_path]
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
        pkg = pkg_of_donor[donor_key]
        if not _target_owns_in_pkg(target_key, target_pf, pkg, devs_by_pf):
            continue  # B22a cross-unit boundary
        matched[dir_path] = target_key

    if not matched:
        return tele

    # ── Move-set per matched dir ─────────────────────────────────────────
    drain_map: dict[str, str] = {}           # file → target PF key
    unattributed_moved: set[str] = set()
    dir_files: dict[str, list[str]] = {}
    for dir_path in sorted(matched):
        donor_key = donor_of_dir[dir_path]
        target_key = matched[dir_path]
        donor_uuids = donor_uuid_sets.get(donor_key, frozenset())
        moved_here: set[str] = set()
        # (a) explicit donor-dev-owned FILES (a dir entry's files arrive
        # via the tracked channel; the entry itself is dropped in the
        # rewrite — carve lists must stay explicit files).
        for f in explicit_by_dir.get(dir_path, ()):
            if tracked and f in prefix_set:
                continue  # directory entry, not a file
            moved_here.add(f)
        # (b) unattributed tracked files (v2 subtree rule).
        for f in sorted(tracked_by_dir.get(dir_path, ())):
            if f in moved_here:
                continue
            fu = pi_owner.get(f)
            if fu is not None:
                if fu in donor_uuids:
                    moved_here.add(f)  # donor-explicit via path_index
                else:
                    tele["skipped_pi_foreign"] += 1
                continue
            if _foreign_claim(f, donor_key):
                tele["skipped_nondonor_covered"] += 1
                continue
            moved_here.add(f)
            unattributed_moved.add(f)
        if not moved_here:
            continue
        for f in moved_here:
            drain_map.setdefault(f, target_key)
        dir_files[dir_path] = sorted(moved_here)

    if not drain_map:
        return tele

    donors_hit = {donor_of_dir[dp] for dp in dir_files}
    drained_dirs_by_donor: dict[str, list[str]] = defaultdict(list)
    for dp in dir_files:
        drained_dirs_by_donor[donor_of_dir[dp]].append(dp)

    # ── Execute: dev-ledger surgery (the 6.97 LOC authority) ─────────────
    used_names = {str(_attr(d, "name")) for d in devs}
    new_devs: list[Any] = []
    for donor_key in sorted(donors_hit):
        drained_dirs = sorted(drained_dirs_by_donor[donor_key])
        for d in devs_by_pf.get(donor_key, []):
            _rewrite_paths(d, drained_dirs, drain_map, tracked, prefix_set)
        # per-target carve devs (explicit file lists — the no-contest law).
        by_target: dict[str, list[str]] = defaultdict(list)
        for dir_path in drained_dirs:
            by_target[matched[dir_path]].extend(dir_files[dir_path])
        member_devs = sorted(devs_by_pf.get(donor_key, []),
                             key=lambda x: str(_attr(x, "name") or ""))
        shell = member_devs[0] if member_devs else pf_by_key[donor_key]
        donor_pf = pf_by_key.get(donor_key)
        for target_key in sorted(by_target):
            files_t = sorted(set(by_target[target_key]))
            carved = _make_drain_dev(
                shell, pf_by_key[target_key], target_key, files_t,
                used_names)
            used_names.add(str(_attr(carved, "name")))
            new_devs.append(carved)
            _widen_pf(pf_by_key[target_key], files_t, target_key)
        if donor_pf is not None:
            all_moved = {f for fs in by_target.values() for f in fs}
            _shrink_pf(donor_pf, all_moved)

    developer_features.extend(new_devs)

    tele["donors"] = sorted(donors_hit)
    tele["matched_dirs"] = {
        d: {
            "pf": matched[d],
            "files": len(dir_files.get(d, ())),
            "loc": _count_loc({f: "" for f in dir_files.get(d, ())}, ctx),
        }
        for d in sorted(dir_files)
    }
    tele["files_moved"] = len(drain_map)
    tele["files_moved_unattributed"] = len(unattributed_moved)
    tele["loc_moved"] = _count_loc(drain_map, ctx)
    # Drain-time PROJECTION of the Stage-6.99 I16 effect (the authoritative
    # count is scan_meta.i16_rehome.rehomed): journeys homed on a donor whose
    # member-flow entries are now strict-majority drained.
    tele["ufs_rehomed"] = _project_rehomes(
        user_flows, developer_features, drain_map, donors_hit)
    tele["samples"] = [
        {"dir": d, "pf": matched[d], "files": len(dir_files[d])}
        for d in list(sorted(dir_files))[:20]
    ]
    return tele


# ── move mechanics (mirror provenance_rehome / lane_excavation) ───────────


def _split_dir(
    path: str, drained_under: list[str], tracked: list[str],
    prefix_set: set[str],
) -> list[str]:
    """Recursive ancestor split: the child paths of directory *path* minus
    the drained subtrees (deterministic sorted). A child that itself
    contains a drained dir is split further; a child that IS one is
    dropped."""
    pref = path + "/"
    children = sorted({
        pref + f[len(pref):].split("/", 1)[0]
        for f in tracked if f.startswith(pref)
    })
    out: list[str] = []
    for c in children:
        if any(c == d for d in drained_under):
            continue  # the drained subtree leaves the donor entirely
        deeper = [d for d in drained_under if d.startswith(c + "/")]
        if deeper and c in prefix_set:
            out.extend(_split_dir(c, deeper, tracked, prefix_set))
        else:
            out.append(c)
    return out


def _rewrite_paths(
    dev: Any, drained_dirs: list[str], drain_map: dict[str, str],
    tracked: list[str], prefix_set: set[str],
) -> None:
    """Apply the v2 surgery to one donor dev's ``paths`` + ``member_files``:
    drop moved files and any entry at/under a drained dir; split ancestor
    DIRECTORY entries into children minus the drained subtrees (the donor
    ancestor carve-out — without it 6.97 expansion keeps covering the moved
    files and the LOC never shifts)."""
    def _rewrite_one(p: str) -> list[str] | None:
        """``None`` → keep as-is; list → replacement entries (may be [])."""
        if p in drain_map:
            return []  # moved explicit file
        if _under_any(p, drained_dirs):
            return []  # entry inside a drained subtree
        under = [d for d in drained_dirs if d.startswith(p + "/")]
        if under and tracked:
            return _split_dir(p, under, tracked, prefix_set)
        return None

    new_paths: list[str] = []
    for p in (_attr(dev, "paths") or []):
        rep = _rewrite_one(str(p))
        if rep is None:
            new_paths.append(str(p))
        else:
            new_paths.extend(rep)
    dev.paths = list(dict.fromkeys(new_paths))  # dedup, order-stable

    mfs = _attr(dev, "member_files") or []
    kept: list[Any] = []
    for m in mfs:
        mp = m.get("path") if isinstance(m, dict) else getattr(m, "path", None)
        if mp is None:
            kept.append(m)
            continue
        rep = _rewrite_one(str(mp))
        if rep is None:
            kept.append(m)
            continue
        for child in rep:
            if isinstance(m, dict):
                mm: Any = dict(m)
                mm["path"] = child
            elif hasattr(m, "model_copy"):
                mm = m.model_copy(update={"path": child})
            else:  # pragma: no cover — exotic stub member
                continue
            kept.append(mm)
    dev.member_files = kept


def _make_drain_dev(
    shell: Any, target_pf: Any, target_key: str,
    files: list[str], used_names: set[str],
) -> Any:
    """A carved member dev under ``target_pf`` claiming ``files`` as primary
    (content-derived uuid, ``split_from`` lineage, ``fold:`` anchor, no
    flows) — mirrors ``provenance_rehome._make_rehome_dev`` with the B53
    marker. The paths are an EXPLICIT FILE LIST by construction (the
    no-contest law). Supports pydantic Features and lightweight stubs."""
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
    """Drop ``moved`` from the donor PF's explicit ``paths`` +
    ``member_files``. Directory entries stay — PF paths never drive the
    6.97 LOC when member devs exist; the dev ledger is the authority."""
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
