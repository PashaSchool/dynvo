"""Stage 6.987 — S5b leaf-route dissolution + platform promotion (ONE wave).

Two sides of ONE disease (spec ``docs/anchor-arc/fixs5b-residual-blackholes-
spec.md`` §ПРОБИ-ФОРМИ; experimenter canon ``/private/tmp/s5b-probe``):

**Seg B — leaf-route black hole.** A ``route:``-anchored PF whose route
surface is a FLAT leaf (no product-route DIR carries the slug ``dir_grain==0``
AND a flat route file's stem == the slug) but which annexed a whole sibling
subtree (``own<=3`` slug-token paths ∧ ``annexed/own>=12``) is an anchor-mass
disproportion (novu ``duplicate-workflow`` 315/2, typebot ``past-due`` 91/1,
Soc0 ``policy-page`` 56/1). The anti-protection is the LEAF gate, never the
ratio: a real deep domain (cal ``bookings`` ``dir_grain=66``, twenty
``workflows``) is unreachable. Its foreign member devs dissolve to their real
capability siblings via the S5a dev-identity bridge + a file-grain
majority-segment fallback (kebab containment, deeper-first). A dev with no
clean home is **NOT forced** — it lanes (``product_feature_id=None`` + shell
reason), which is precisely the "freed page" Seg C then consumes.

**Seg C — platform burial (the a-lite MIRROR).** ``stage_6_86._mint_bar`` bars
a birth with NO page evidence as ``api_only_surface``; the mirror is that a
``platform_infrastructure`` lane resident whose OWN paths carry PAGE evidence
is a *buried* product surface. The pure mirror was refuted (the exhibit pages
are annexed inside the Seg B holes), so the working form is:

* **P1** — page evidence in a resident's members (a-lite exclusions:
  ``_app`` / ``_document`` / ``_error`` + a ``pages/api`` segment + layout/
  loading/error/template). The promotion unit is the **PAGE-COHORT** (the page
  files + their own directory), never the whole row — supabase ``docs`` (979
  files, 2 pages) must not drag 979 files.
* **P2** — a lane token bridges to a freed / mis-homed product-PAGE
  (``len>3`` ∧ token-``intersection>=1``): the pages Seg B just freed. This is
  how ``workflow-editor`` (189 laned files, 0 pages of its own) promotes once
  Seg B dissolves the workflow leaf-holes.

Promotion **births** a PF under the S5a birth-law (real ``member_files`` +
``split_from`` + page evidence, so I2/I8 keep it) or **merges** into an
existing sibling (the ``notifications`` class, ~2 % of moves).

ARBITER WAVE: Seg B runs FIRST and frees pages into the lane; Seg C consumes
them in the SAME pass. Every dev / UF re-home is an overturn-ledger PROPOSAL
(``propose_pf_now``, rung ``leafroute``) — there are no direct
``product_feature_id`` writers. Deterministic, ``$0`` LLM. Kill-switch:
``FAULTLINE_LEAFROUTE_PROMOTION`` unset/``0`` → the stage is not entered and
the scan is byte-identical to pre-S5b.
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from typing import Any

from faultline.pipeline_v2.overturn_ledger import propose_pf_now
from faultline.pipeline_v2.spine_anchors import normalize_anchor_key
from faultline.pipeline_v2.transport_handoff import _attr

__all__ = [
    "LEAFROUTE_PROMOTION_ENV",
    "leafroute_promotion_enabled",
    "run_leafroute_promotion",
]

LEAFROUTE_PROMOTION_ENV = "FAULTLINE_LEAFROUTE_PROMOTION"

#: Provenance marker on dissolved / promoted rows (I22 explainability).
_MARKER = "leafroute-promotion"

# ── Seg B constants — every one a probe-measured natural band edge ──────────
#: own-mass ceiling: a real leaf route owns <= this many slug-token paths
#: (novu/typebot/Soc0 exhibits all own 1-3; the conservative probe anchor 40).
_LEAF_OWN_MAX = 3
#: annexed/own ratio floor: the natural break at 12 (census @12: 38/9 boards,
#: cal bookings dir_grain=66 sits UNDER the leaf gate, never near the ratio).
_LEAF_RATIO_MIN = 12
#: kebab sub-containment guard (probe): a sibling token shorter than this is
#: too generic to home a foreign file/dev onto.
_MIN_SUB_TOKEN = 3

_COMPONENT_EXT = re.compile(r"\.(tsx|jsx|vue|svelte|astro|erb|html)$")
_PAGE_BASENAME = re.compile(r"(^|/)page\.(tsx|jsx|js|ts|vue|svelte)$")
#: a-lite layout/shell files are legal platform residents — never page evidence.
_LAYOUT_BASENAME = re.compile(r"(^|/)(layout|loading|error|template)\.")
#: Next.js pages-dir specials — framework scaffolding, never a product page
#: (langfuse ``pages/_app.tsx`` false-fire; spec P1 exclusion).
_SPECIAL_PAGE = re.compile(r"(^|/)(_app|_document|_error)\.")

#: the lane-resident reasons ``build_platform_infrastructure_lane`` accepts —
#: Seg C reads the SAME set so it never promotes a non-lane pfid=None dev.
def _lane_reasons() -> frozenset[str]:
    from faultline.pipeline_v2.emission_integrity import ANCHORED_HUSK_REASON
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        _SHARED_REASON_BAR,
        _SHARED_REASON_CROSS_UNIT,
        _SHARED_REASON_DEV_ARTIFACT,
        _SHARED_REASON_INFRA_FANIN,
        _SHARED_REASON_INSTRUMENT,
        _SHARED_REASON_NONE,
        _SHARED_REASON_SHELL,
    )
    return frozenset({
        _SHARED_REASON_NONE, _SHARED_REASON_BAR, _SHARED_REASON_SHELL,
        ANCHORED_HUSK_REASON, _SHARED_REASON_INSTRUMENT,
        _SHARED_REASON_INFRA_FANIN, _SHARED_REASON_CROSS_UNIT,
        _SHARED_REASON_DEV_ARTIFACT,
    })


def leafroute_promotion_enabled() -> bool:
    """Default OFF. Unset/``0``/false/off → the stage is not entered and the
    scan is byte-identical to pre-S5b. Flipped only by its own keyed A/B."""
    return os.environ.get(LEAFROUTE_PROMOTION_ENV, "").strip().lower() in {
        "1", "true",
    }


# ── shared token helpers (probe canon) ─────────────────────────────────────


def _norm(s: str) -> str:
    return normalize_anchor_key(str(s or ""))


def _stem(path: str) -> str:
    base = str(path).rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[:dot] if dot > 0 else base


def _kebab_contains(hay: str, needle: str) -> bool:
    """Token-boundary containment in kebab space (``-needle-`` in ``-hay-``)."""
    return f"-{needle}-" in f"-{hay}-"


def _toks(kebab: str) -> set[str]:
    return {t for t in str(kebab).split("-") if t}


def _pf_key(pf: Any) -> str:
    return str(_attr(pf, "id") or _attr(pf, "name") or "")


def _anchor_term(pf: Any) -> str:
    aid = str(_attr(pf, "anchor_id") or "")
    if ":" not in aid:
        return ""
    return aid.split(":", 1)[1].rstrip("/")


def _route_slug(pf: Any) -> str:
    aid = str(_attr(pf, "anchor_id") or "")
    if not aid.startswith("route:"):
        return ""
    return _norm(_anchor_term(pf).rsplit("/", 1)[-1])


def _identity_tokens(pf: Any) -> set[str]:
    """A sibling PF's identity tokens: normalized name + anchor terminal +
    numeric/version-twin strip (``topics-v2`` ↔ ``topics``)."""
    toks = {_norm(_attr(pf, "name"))}
    aterm = _anchor_term(pf)
    if aterm:
        toks.add(_norm(aterm.rsplit("/", 1)[-1]))
    for t in list(toks):
        parts = t.split("-")
        if len(parts) > 1 and parts[-1].isdigit():
            toks.add("-".join(parts[:-1]))
    toks.discard("")
    return toks


def _dev_tokens(d: Any) -> set[str]:
    """Dev capability tokens — the S5a dev-identity bridge shape (name +
    api-strip + fold-target + numeric-twin), reused verbatim from
    ``mega_pf_nav_rehome`` with the spine structural stoplist as layer-vocab."""
    from faultline.pipeline_v2.mega_pf_nav_rehome import (
        _dev_identity_tokens,
        _s5a_stoplist,
    )
    try:
        return _dev_identity_tokens(d, _s5a_stoplist())
    except Exception:  # noqa: BLE001 — synthetic scenes without spine vocab
        return _dev_identity_tokens(d, frozenset())


def _page_files(paths: list[str], page_ri_files: set[str]) -> list[str]:
    """PAGE-evidence members with the a-lite exclusions (spec P1): drop
    layout/loading/error/template + Next specials ``_app``/``_document``/
    ``_error`` + any ``pages/api`` segment; keep ``page.<ext>``, a component
    under a ``pages``/``routes`` segment, or a PAGE-method routes_index file."""
    out: list[str] = []
    for p in paths:
        sp = str(p)
        segs = sp.split("/")
        if _LAYOUT_BASENAME.search(sp) or _SPECIAL_PAGE.search(sp):
            continue
        # a ``pages/api/**`` (or ``routes/api/**``) file is an API route, not a
        # product page (langfuse pages/api class; spec exclusion).
        if any(segs[i] in ("pages", "routes") and i + 1 < len(segs)
               and segs[i + 1] == "api" for i in range(len(segs) - 1)):
            continue
        if _PAGE_BASENAME.search(sp):
            out.append(sp)
            continue
        if (("pages" in segs[:-1] or "routes" in segs[:-1])
                and _COMPONENT_EXT.search(sp)):
            out.append(sp)
            continue
        if sp in page_ri_files:
            out.append(sp)
    return out


# ── Seg B — leaf-route firing predicate + dissolution ──────────────────────


def _product_route_seg_index(
    routes_index: list[dict[str, Any]] | None,
) -> tuple[list[set[str]], list[str]]:
    """Per product-route file: (normalized DIR segment set, normalized stem)."""
    dir_sets: list[set[str]] = []
    stems: list[str] = []
    for e in routes_index or []:
        if not isinstance(e, dict):
            continue
        if e.get("surface_scope") not in (None, "", "product"):
            continue
        f = str(e.get("file") or "")
        if not f:
            continue
        segs = [s for s in f.split("/") if s]
        dir_sets.append({_norm(s) for s in segs[:-1]})
        stems.append(_norm(_stem(f)))
    return dir_sets, stems


def _leaf_firing(
    pf: Any, dir_sets: list[set[str]], stems: list[str],
) -> dict[str, Any] | None:
    """The Seg B predicate over one PF. ``None`` → does not fire."""
    slug = _route_slug(pf)
    if not slug:
        return None
    dir_grain = sum(1 for ds in dir_sets if slug in ds)
    file_grain = sum(1 for st in stems if st == slug or _kebab_contains(st, slug))
    leaf = dir_grain == 0 and file_grain >= 1
    if not leaf:
        return None
    slug_toks = _toks(slug)
    own = 0
    annexed = 0
    for p in (_attr(pf, "paths") or []):
        segs = [s for s in str(p).split("/") if s]
        cands = [_norm(s) for s in segs[:-1]] + [_norm(_stem(p))]
        hit = any(_kebab_contains(c, slug) or _toks(c) >= slug_toks
                  for c in cands)
        if hit:
            own += 1
        else:
            annexed += 1
    ratio = annexed / max(1, own)
    if own <= _LEAF_OWN_MAX and ratio >= _LEAF_RATIO_MIN and annexed > 0:
        return {"slug": slug, "own": own, "annexed": annexed,
                "ratio": round(ratio, 2), "dir_grain": dir_grain,
                "file_grain": file_grain}
    return None


def _sibling_token_map(
    product_features: list[Any], exclude_key: str,
) -> dict[str, str]:
    """token → sibling PF key (deterministic first-wins by PF name)."""
    tok2pf: dict[str, str] = {}
    for pf in sorted(product_features, key=lambda x: str(_attr(x, "name"))):
        k = _pf_key(pf)
        if not k or k == exclude_key:
            continue
        for t in sorted(_identity_tokens(pf)):
            tok2pf.setdefault(t, k)
    return tok2pf


def _dev_bridge_target(
    dev: Any, slug: str, tok2pf: dict[str, str], exclude_key: str,
) -> str | None:
    """The S5a dev-identity bridge for a whole dev: a dev token that is NOT
    the slug (a core dev never bridges away) matching a sibling identity
    token. ``None`` → this dev has no whole-dev identity home (its files fall
    to the per-path file-grain resolver)."""
    dtoks = _dev_tokens(dev)
    if slug in dtoks:
        return None
    for t in sorted(dtoks):
        if t in tok2pf and tok2pf[t] != exclude_key:
            return tok2pf[t]
    return None


def _path_home_target(
    path: str, slug: str, tok2pf: dict[str, str], exclude_key: str,
) -> str | None:
    """File-grain segment homing for ONE annexed path (the probe's ``seg:``/
    ``sub:`` rungs): deeper segment first (a file under
    ``components/subscribers/`` votes ``subscriber``), exact sibling token
    then kebab sub-containment (``len>3``). ``None`` → the path stays
    (unhomed is NOT forced)."""
    segs = [s for s in str(path).split("/") if s]
    cands = [_norm(_stem(path))] + [_norm(s) for s in reversed(segs[:-1])]
    for c in cands:
        if c and c != slug and c in tok2pf and tok2pf[c] != exclude_key:
            return tok2pf[c]
    for c in cands:
        if len(c) > _MIN_SUB_TOKEN:
            for st, spf in sorted(tok2pf.items()):
                if (spf != exclude_key and len(st) > _MIN_SUB_TOKEN
                        and _kebab_contains(c, st)):
                    return spf
    return None


def _shed_paths(pf: Any, gone: set[str]) -> None:
    """A dissolved / promoted source row sheds the departed paths from its
    ``paths`` + ``member_files`` (conservation: released == claimed)."""
    if not gone:
        return
    pf.paths = [p for p in (_attr(pf, "paths") or []) if str(p) not in gone]
    kept = []
    for m in (_attr(pf, "member_files") or []):
        mp = m.get("path") if isinstance(m, dict) else getattr(m, "path", None)
        if mp not in gone:
            kept.append(m)
    pf.member_files = kept


def _is_own_path(path: str, slug: str, slug_toks: set[str]) -> bool:
    """A path carries the leaf's own slug identity (kebab OR unordered
    token-set — ProjectClaim↔claim-project; the probe's own-mass rule)."""
    segs = [s for s in str(path).split("/") if s]
    cands = [_norm(s) for s in segs[:-1]] + [_norm(_stem(path))]
    return any(_kebab_contains(c, slug) or _toks(c) >= slug_toks for c in cands)


def _attach_paths(pf: Any, paths: set[str]) -> None:
    """Attach carved paths to a sibling PF's ``paths`` + ``member_files``."""
    from faultline.models.types import MemberFile
    have = {str(p) for p in (_attr(pf, "paths") or [])}
    add = sorted(p for p in paths if p not in have)
    if not add:
        return
    pf.paths = sorted(have | set(add))
    mf = list(_attr(pf, "member_files") or [])
    mf.extend(MemberFile(path=p, role="closure", confidence=0.9,
                         evidence=f"{_MARKER} dissolution", primary=False)
              for p in add)
    pf.member_files = mf


def _dissolve_leaf(
    pf: Any, slug: str, devs_by_pf: dict[str, list[Any]],
    product_features: list[Any], developer_features: list[Any],
    page_ri_files: set[str], tele: dict[str, Any],
) -> list[tuple[str, str, Any]]:
    """Path-grain dissolution: each ANNEXED path (not the slug's own) carves to
    its real sibling by the S5a dev-identity bridge (whole-dev when a dev's
    whole annexed mass shares one home) or the file-grain segment (a page under
    ``components/subscribers/`` -> ``subscribers``). Unhomed annexed paths STAY
    (never forced); unhomed annexed PAGES are returned as
    ``(path, leaf_key, owning_dev)`` freed-page fodder for Seg C P2. The leaf PF
    + the source devs shed every carved path (conservation: released ==
    claimed)."""
    key = _pf_key(pf)
    tok2pf = _sibling_token_map(product_features, key)
    pf_by_key = {_pf_key(p): p for p in product_features if _pf_key(p)}
    slug_toks = _toks(slug)

    carved: dict[str, set[str]] = defaultdict(set)     # target -> paths
    whole_dev_moves: list[tuple[Any, str]] = []        # (dev, target)
    strip_from: dict[int, tuple[Any, set[str]]] = {}   # dev id -> (dev, paths)
    freed_pages: list[tuple[str, str, Any]] = []
    homed = 0

    def _record_freed(p: str, owner: Any) -> None:
        if p in page_ri_files or _page_files([p], page_ri_files):
            freed_pages.append((p, key, owner))

    for dev in sorted(devs_by_pf.get(key, []),
                      key=lambda d: str(_attr(d, "name") or "")):
        dpaths = [str(p) for p in (_attr(dev, "paths") or [])]
        annexed = [p for p in dpaths if not _is_own_path(p, slug, slug_toks)]
        if not annexed:
            continue
        bridge = _dev_bridge_target(dev, slug, tok2pf, key)
        own_left = [p for p in dpaths if _is_own_path(p, slug, slug_toks)]
        # per-path target (dev-bridge wins; else file-grain segment).
        per: dict[str, str] = {}
        for p in annexed:
            t = bridge or _path_home_target(p, slug, tok2pf, key)
            if t and t != key:
                per[p] = t
        if not per:
            for p in annexed:
                _record_freed(p, dev)
            continue
        moved = set(per)
        if not own_left and len(set(per.values())) == 1 and moved == set(annexed):
            # the whole dev is one foreign capability — re-home it intact.
            tgt = next(iter(per.values()))
            whole_dev_moves.append((dev, tgt))
            carved[tgt] |= moved
        else:
            strip_from[id(dev)] = (dev, moved)
            for p, t in per.items():
                carved[t].add(p)
        homed += len(moved)
        for p in annexed:
            if p not in per:
                _record_freed(p, dev)

    if homed == 0:
        tele["leaf_dissolved"].append({
            "pf": key, "slug": slug, "paths_moved": 0, "whole_devs": 0,
            "chunks": 0, "freed_pages": len(freed_pages)})
        return freed_pages

    # apply whole-dev re-homes (proposal — the ledger journals it).
    for dev, tgt in whole_dev_moves:
        propose_pf_now(dev, tgt, rung="leafroute")
        dev.anchor_id = f"fold:{_MARKER}->{tgt}"
        if _attr(dev, "shared_reason"):
            dev.shared_reason = None

    # carve per-path chunks off the mixed source devs (one chunk per target),
    # then strip the carved paths from the source dev.
    chunks = 0
    for _did, (dev, moved) in sorted(strip_from.items(),
                                     key=lambda kv: str(_attr(kv[1][0], "name"))):
        tgt_of: dict[str, set[str]] = defaultdict(set)
        for p in sorted(moved):
            for t, ps in carved.items():
                if p in ps:
                    tgt_of[t].add(p)
                    break
        for tgt in sorted(tgt_of):
            chunk = _mk_chunk(dev, sorted(tgt_of[tgt]),
                              f"{_attr(dev, 'name')}-{tgt}-leafroute")
            developer_features.append(chunk)
            propose_pf_now(chunk, tgt, rung="leafroute")
            chunk.anchor_id = f"fold:{_MARKER}->{tgt}"
            chunks += 1
        _shed_paths_dev(dev, moved)

    # attach carved paths to their sibling PF rows; shed from the leaf PF.
    all_moved: set[str] = set()
    for tgt, ps in carved.items():
        tpf = pf_by_key.get(tgt)
        if tpf is not None:
            _attach_paths(tpf, ps)
        all_moved |= ps
    _shed_paths(pf, all_moved)

    tele["leaf_dissolved"].append({
        "pf": key, "slug": slug, "paths_moved": len(all_moved),
        "whole_devs": len(whole_dev_moves), "chunks": chunks,
        "freed_pages": len(freed_pages)})
    tele["paths_dissolved"] += len(all_moved)
    tele["devs_rehomed"] += len(whole_dev_moves)
    if len(tele["dissolve_moves"]) < 60:
        for tgt, ps in sorted(carved.items()):
            tele["dissolve_moves"].append(
                {"from": key, "to": tgt, "paths": len(ps)})
    return freed_pages


def _shed_paths_dev(dev: Any, gone: set[str]) -> None:
    """Strip carved paths off a source dev (member_files + paths)."""
    dev.paths = [p for p in (_attr(dev, "paths") or []) if str(p) not in gone]
    kept = []
    for m in (_attr(dev, "member_files") or []):
        mp = m.get("path") if isinstance(m, dict) else getattr(m, "path", None)
        if mp not in gone:
            kept.append(m)
    dev.member_files = kept


# ── Seg C — platform promotion (P1 page-cohort ∪ P2 bridge) ────────────────


def _mk_chunk(src: Any, paths: list[str], name: str) -> Any:
    """A carved dev holding only the cohort ``paths`` (deep-copy of the light
    metrics from ``src``) — keeps the born PF from dragging the whole row."""
    from datetime import datetime, timezone

    from faultline.models.types import Feature
    commits = int(_attr(src, "total_commits") or 0)
    fixes = int(_attr(src, "bug_fixes") or 0)
    return Feature(
        name=name,
        display_name=name,
        description=f"{_MARKER} cohort of '{_attr(src, 'name')}'",
        paths=sorted(paths),
        authors=list(_attr(src, "authors") or []),
        total_commits=commits,
        bug_fixes=fixes,
        bug_fix_ratio=(fixes / commits) if commits else 0.0,
        layer="developer",
        last_modified=_attr(src, "last_modified")
        or datetime.fromtimestamp(0, timezone.utc),
        health_score=float(_attr(src, "health_score") or 0.0),
    )


def _birth_pf(
    display: str, contrib: list[Any], source_key: str, anchor_id: str,
    surface_scope: str, used_slugs: set[str],
) -> Any:
    """Birth a product PF under the S5a birth-law: real ``member_files`` +
    ``split_from`` + page evidence (by construction the caller only births a
    cohort that carries pages, so the a-lite api_only bar is satisfied)."""
    from faultline.models.types import MemberFile
    from faultline.pipeline_v2.emission_integrity import canonical_slug
    from faultline.pipeline_v2.nav_taxonomy import aggregate_product_feature

    slug = canonical_slug(display) or canonical_slug(source_key)
    if slug in used_slugs:
        base, n = slug, 2
        while slug in used_slugs:
            slug = canonical_slug(f"{base} {n}")
            n += 1
    used_slugs.add(slug)
    pf = aggregate_product_feature(
        name=slug, display_name=display,
        description=(f"Product surface promoted from the platform lane "
                     f"({_MARKER}; buried under '{source_key}')."),
        contrib=contrib)
    pf.layer = "product"
    pf.anchor_id = anchor_id
    pf.surface_scope = surface_scope
    pf.split_from = source_key
    pf.member_files = [
        MemberFile(path=str(p), role="anchor", confidence=1.0,
                   evidence=f"{_MARKER} promotion", primary=True)
        for p in sorted({str(x) for x in (pf.paths or [])})
    ]
    return pf, slug


def _cohort_of(dev: Any, page_paths: list[str]) -> set[str]:
    """PAGE-COHORT: the page files + files in the SAME immediate directory as
    a page (tight bound — supabase/docs 979-file shell yields only the 2 pages'
    own dirs, never the whole row)."""
    page_dirs = {str(p).rsplit("/", 1)[0] for p in page_paths if "/" in str(p)}
    cohort = {str(p) for p in page_paths}
    for p in (_attr(dev, "paths") or []):
        sp = str(p)
        d = sp.rsplit("/", 1)[0] if "/" in sp else ""
        if d in page_dirs:
            cohort.add(sp)
    return cohort


class _LocProbe:
    """On-demand owned-LOC over the repo tree (mega ``_MassOracle`` shape):
    a birth may only mint a surface that carries REAL owned source (the S5a
    birth-law no-husk rule — a member-ful 0-LOC PF is a trust bug). The
    channel is live iff the tree exists AND at least one probed path is on
    disk; a synthetic unit scene (fixture paths that exist nowhere) has NO
    channel → the gate is vacuously open (tests birth freely)."""

    def __init__(self, ctx: Any, sample_paths: list[str]) -> None:
        from pathlib import Path
        self._root: Any = None
        rp = _attr(ctx, "repo_path", None)
        if rp and Path(str(rp)).is_dir():
            self._root = Path(str(rp))
        self._cache: dict[str, int] = {}
        self.channel = False
        if self._root is not None:
            for p in sample_paths[:400]:
                if (self._root / str(p)).exists():
                    self.channel = True
                    break

    def loc_of(self, paths: set[str]) -> int:
        if self._root is None:
            return 0
        from faultline.pipeline_v2.stage_6_97_feature_loc import (
            _expand_feature_files,
        )
        files = _expand_feature_files(self._root, sorted(paths), self._cache)
        return sum(files.values())

    def ok(self, cohort: set[str]) -> bool:
        # no channel (synthetic) → open; live channel → require owned LOC > 0.
        return (not self.channel) or self.loc_of(cohort) > 0


def _promote_resident(
    dev: Any, cohort: set[str], page_paths: list[str],
    developer_features: list[Any], product_features: list[Any],
    pf_by_key: dict[str, Any], sib_tok: dict[str, str],
    used_slugs: set[str], surface_scope: str, tele: dict[str, Any],
    kind: str, loc_probe: "_LocProbe | None" = None,
    absorbed: set[str] | None = None,
) -> bool:
    """Merge the cohort into a token-matching sibling (the notifications class,
    ~2 %) else birth a PF. ``cohort`` is the EXACT resident set of the promoted
    surface; ``absorbed`` are cohort paths NOT already on ``dev`` (P2 bridged
    pages, already stripped from their owners by the caller). When the cohort
    covers the whole dev, the dev re-homes intact (absorbing the bridged
    pages); else a chunk holding exactly the cohort carves off a big shell."""
    dtoks = _dev_tokens(dev)
    absorbed = absorbed or set()
    merge_key = None
    for t in sorted(dtoks):
        if t in sib_tok:
            merge_key = sib_tok[t]
            break
    dev_paths = {str(p) for p in (_attr(dev, "paths") or [])}
    cohort_in_dev = cohort & dev_paths
    # the promotion takes the WHOLE dev iff its cohort covers every dev path.
    whole = bool(dev_paths) and cohort_in_dev >= dev_paths
    display = str(_attr(dev, "display_name") or _attr(dev, "name") or "surface")

    if whole and absorbed:
        # the dev absorbs the bridged pages, then re-homes/merges intact.
        dev.paths = sorted(dev_paths | absorbed)

    if merge_key and merge_key in pf_by_key:
        tpf = pf_by_key[merge_key]
        if whole:
            propose_pf_now(dev, merge_key, rung="leafroute")
            dev.shared_reason = None
            move = dev_paths | absorbed
        else:
            chunk = _mk_chunk(dev, sorted(cohort), _attr(dev, "name"))
            developer_features.append(chunk)
            propose_pf_now(chunk, merge_key, rung="leafroute")
            _shed_paths_dev(dev, cohort_in_dev)
            move = cohort
        _attach_paths(tpf, move)
        tele["merged"].append({"dev": _attr(dev, "name"), "into": merge_key,
                               "kind": kind, "paths": len(move)})
        tele["devs_merged"] += 1
        return True

    # BIRTH — the S5a birth-law no-husk gate: the cohort must carry REAL
    # owned source (else the born PF is a member-ful 0-LOC trust bug — Soc0
    # i18n/notifications/home husks). Vacuously open on synthetic scenes.
    if loc_probe is not None and not loc_probe.ok(cohort):
        if whole and absorbed:
            dev.paths = sorted(dev_paths)  # undo the pre-emptive absorb
        tele.setdefault("birth_husk_held", []).append(
            {"dev": _attr(dev, "name"), "kind": kind})
        return False

    anchor = f"promote:{_MARKER}/{_attr(dev, 'name')}"
    if whole:
        pf, slug = _birth_pf(display, [dev], _pf_key_of_lane(dev), anchor,
                             surface_scope, used_slugs)
        propose_pf_now(dev, slug, rung="leafroute")
        dev.shared_reason = None
    else:
        chunk = _mk_chunk(dev, sorted(cohort), _attr(dev, "name"))
        developer_features.append(chunk)
        pf, slug = _birth_pf(display, [chunk], _pf_key_of_lane(dev), anchor,
                             surface_scope, used_slugs)
        propose_pf_now(chunk, slug, rung="leafroute")
        _shed_paths_dev(dev, cohort_in_dev)
    product_features.append(pf)
    pf_by_key[slug] = pf
    tele["births"].append({"dev": _attr(dev, "name"), "pf": slug, "kind": kind,
                           "pages": len(page_paths), "paths": len(pf.paths)})
    tele["pfs_born"] += 1
    return True


def _pf_key_of_lane(dev: Any) -> str:
    return f"lane:{_attr(dev, 'name')}"


def _seg_c_promote(
    developer_features: list[Any], product_features: list[Any],
    page_ri_files: set[str], freed_pages: list[tuple[str, str, Any]],
    surface_scope: str, tele: dict[str, Any], loc_probe: "_LocProbe | None",
) -> None:
    """P1 (page evidence in a lane resident) ∪ P2 (lane token ↔ a freed /
    mis-homed product-PAGE). ``freed_pages`` = ``(page_path, leaf_key,
    owning_dev)`` the Seg B dissolution could not home — the buried surface's
    pages, stranded on the leaf hole."""
    lane_reasons = _lane_reasons()
    residents = [
        f for f in developer_features
        if _attr(f, "layer", "developer") == "developer"
        and _attr(f, "product_feature_id") is None
        and _attr(f, "shared_reason") in lane_reasons
        and _attr(f, "name")
    ]
    pf_by_key = {_pf_key(pf): pf for pf in product_features if _pf_key(pf)}
    sib_tok = _sibling_token_map(product_features, exclude_key="")
    used_slugs = set(pf_by_key) | {"platform", "shared-platform"}
    promoted: set[int] = set()

    # ── P1 — page-cohort promotion (deterministic order by name) ───────────
    for dev in sorted(residents, key=lambda d: str(_attr(d, "name"))):
        pages = _page_files([str(p) for p in (_attr(dev, "paths") or [])],
                            page_ri_files)
        if not pages:
            continue
        cohort = _cohort_of(dev, pages)
        if _promote_resident(dev, cohort, pages, developer_features,
                             product_features, pf_by_key, sib_tok, used_slugs,
                             surface_scope, tele, kind="P1",
                             loc_probe=loc_probe):
            promoted.add(id(dev))

    # ── P2 — lane token ↔ freed / mis-homed product-PAGE ───────────────────
    # each freed page's component tokens (len>3); a lane resident whose token
    # intersects claims the page as its promotion evidence and carves it off
    # BOTH the leaf hole and the owning dev where it was stranded.
    page_tok: list[tuple[str, str, Any, set[str]]] = []
    for pp, leaf_key, owner in freed_pages:
        segs = [s for s in str(pp).split("/") if s]
        ptoks = _toks(_norm(_stem(pp)))
        for s in segs[:-1]:
            ptoks |= _toks(_norm(s))
        page_tok.append((str(pp), leaf_key, owner,
                         {t for t in ptoks if len(t) > _MIN_SUB_TOKEN}))
    if not page_tok:
        return
    claimed: set[str] = set()
    for dev in sorted(residents, key=lambda d: str(_attr(d, "name"))):
        if id(dev) in promoted:
            continue
        # component-grain tokens (workflow-editor -> {workflow, editor}) so a
        # lane token bridges a freed page that shares a WORD (len>3).
        dtoks = {c for t in _dev_tokens(dev) for c in _toks(t)
                 if len(c) > _MIN_SUB_TOKEN}
        if not dtoks:
            continue
        bridged = [(pp, lk, owner) for pp, lk, owner, pt in page_tok
                   if pp not in claimed and (dtoks & pt)]
        if not bridged:
            continue
        bridged_paths = [pp for pp, _lk, _o in bridged]
        cohort = ({str(p) for p in (_attr(dev, "paths") or [])}
                  | set(bridged_paths))
        # no-husk gate BEFORE any shed, so a held husk never strands a page.
        if loc_probe is not None and not loc_probe.ok(cohort):
            tele.setdefault("birth_husk_held", []).append(
                {"dev": _attr(dev, "name"), "kind": "P2"})
            continue
        claimed.update(bridged_paths)
        # carve each bridged page off the leaf hole AND its owning dev.
        for pp, lk, owner in bridged:
            lpf = pf_by_key.get(lk)
            if lpf is not None:
                _shed_paths(lpf, {pp})
            if owner is not None and owner is not dev:
                _shed_paths_dev(owner, {pp})
        # the born PF's residents = the lane resident's own paths + the
        # bridged freed pages (the buried surface reunited with its pages).
        if _promote_resident(dev, cohort, bridged_paths, developer_features,
                             product_features, pf_by_key, sib_tok, used_slugs,
                             surface_scope, tele, kind="P2",
                             loc_probe=loc_probe, absorbed=set(bridged_paths)):
            promoted.add(id(dev))
            tele["p2_pages_bridged"] += len(bridged_paths)


# ── entrypoint ──────────────────────────────────────────────────────────────


def run_leafroute_promotion(
    developer_features: list[Any],
    product_features: list[Any],
    user_flows: list[Any],
    flows: list[Any],
    routes_index: list[dict[str, Any]] | None,
    ctx: Any,
    feature_flow_edges: list[Any] | None = None,
) -> dict[str, Any]:
    """Stage 6.987 — see module docstring. Mutates ``developer_features`` /
    ``product_features`` in place per a verified plan; returns telemetry for
    ``scan_meta.leafroute_promotion`` (the caller writes the key only when the
    wave did something — inertness convention)."""
    tele: dict[str, Any] = {
        "enabled": True, "leaf_dissolved": [], "dissolve_moves": [],
        "births": [], "merged": [], "devs_rehomed": 0, "paths_dissolved": 0,
        "devs_merged": 0, "pfs_born": 0, "p2_pages_bridged": 0,
    }
    if not product_features:
        return tele

    devs = [f for f in developer_features
            if _attr(f, "layer", "developer") == "developer" and _attr(f, "name")]
    devs_by_pf: dict[str, list[Any]] = defaultdict(list)
    for d in devs:
        pid = _attr(d, "product_feature_id")
        if pid:
            devs_by_pf[str(pid)].append(d)

    dir_sets, stems = _product_route_seg_index(routes_index)
    page_ri_files = {
        str(e.get("file") or "") for e in (routes_index or [])
        if isinstance(e, dict) and str(e.get("method") or "").upper() == "PAGE"
        and e.get("file")
    }

    # ── Seg B — dissolve leaf-route holes (deterministic order) ────────────
    fired: list[tuple[Any, str]] = []
    for pf in product_features:
        fire = _leaf_firing(pf, dir_sets, stems)
        if fire is not None:
            fired.append((pf, fire["slug"]))
            tele.setdefault("leaf_fired", []).append(
                {"pf": _pf_key(pf), **fire})
    fired.sort(key=lambda t: _pf_key(t[0]))
    freed_pages: list[tuple[str, str, Any]] = []
    for pf, slug in fired:
        freed_pages.extend(_dissolve_leaf(
            pf, slug, devs_by_pf, product_features, developer_features,
            page_ri_files, tele))

    # ── Seg C — promote buried surfaces (consumes Seg B's freed pages) ─────
    # The no-husk LOC probe samples lane-resident paths to decide its channel.
    surface_scope = "product"
    lane_sample: list[str] = []
    for d in devs:
        if _attr(d, "product_feature_id") is None:
            lane_sample.extend(str(p) for p in (_attr(d, "paths") or []))
    loc_probe = _LocProbe(ctx, lane_sample)
    _seg_c_promote(developer_features, product_features, page_ri_files,
                   freed_pages, surface_scope, tele, loc_probe)

    # ── LOC re-truth (the LOC-doctrine: member-ful 0-LOC = trust bug) ──────
    # The wave ran AFTER Stage 6.97 froze loc, so path moves + births carry
    # stale/zero loc. Re-invoke THE 6.97 owned-loc counter over the updated
    # dev→PF attribution: births get real loc, the leaf shrinks, targets grow,
    # untouched PFs stay identical (idempotent, ``sum_pf_owned <= repo_loc`` by
    # construction). Only when the wave changed something and a tree exists.
    moved_anything = (tele["paths_dissolved"] or tele["pfs_born"]
                      or tele["devs_merged"])
    if moved_anything:
        try:
            from pathlib import Path

            from faultline.pipeline_v2.stage_6_97_feature_loc import (
                apply_feature_loc,
            )
            rp = _attr(ctx, "repo_path", None)
            if rp and Path(str(rp)).is_dir():
                apply_feature_loc(
                    developer_features, product_features, str(rp),
                    user_flows=user_flows, flows=flows)
                tele["loc_retruthed"] = True
        except Exception as exc:  # noqa: BLE001 — never break a scan
            tele["loc_retruth_error"] = str(exc)
    return tele
