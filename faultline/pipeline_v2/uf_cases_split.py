"""B75 — UF-giant cases-split (``FAULTLINE_UF_CASES_SPLIT``, default ON
since the 2026-07-21 pack-3 flip, KEY_SCHEMA 34; explicit =0 stays the
kill-switch).

A giant catch-all journey (member_count >= :data:`GIANT_MEMBER_FLOOR`)
whose member entry files cluster into >= K distinct SURFACE cases is
re-grained into per-case child journeys plus a residual that KEEPS the
parent row (id, name, lineage — the ``spray_absorption`` survivor-id
law, applied in the split direction: the survivor here is the parent
itself, wearing the residual).  Post-mint B7-family arithmetic: the
same "cases >= floor, max-child residual pooling" law the journey
lattice applies on the action axis, extended to the DIR-TREE axis the
2026-07-21 probe canonized (twenty 209m 'Browse object record' -> 10
children / 73 extracted / residual 136; 131m settings -> 11 children /
residual 72; anti-cases 6/6 intact — 34m activities ONE leaf, 44m
workflows qual=1, 8m connect-server below floor, onyx 63m cohesive,
novu 80m backend 0-qualified).

Mechanism (form (a) of the probe; entropy REFUTED as a gate — healthy
34m Hnorm 0.97 == sick 209m 0.94; the boundary is the monotone
count-of-children-above-floor):

1. recursive dir-tree descent over member entry files; a node is a
   CASE CANDIDATE iff it is a leaf of the >=N subtree (no sub-bucket
   of size >= N below it — deeper never qualifies);
2. a candidate EXTRACTS as a child iff size >= N AND (JSX-surface
   member share >= 1/2 OR route-witnessed member share >=
   ``_I15_ATTACH_FLOOR``);
3. the VENDORED GUARD runs BEFORE extraction (tracecat exhibit: 76m
   split into tiptap-icons / tiptap-ui-primitive / ai-elements —
   vendored-UI kits with technical names): a candidate with ZERO
   route-witnessed members (S3 no-product-surface) whose dir segment
   token-echoes an EXTERNAL dependency family from the repo manifests
   (S1 manifest law, the B19 transport-lane mechanism) is rejected;
4. the split fires iff >= K qualified children survive the guard;
   sub-floor / unqualified mass pools into the residual under the
   parent's own row (max-child analog — conservation by name:
   union(children) + residual == members(parent), checked and
   reverted defensively like the lattice apply).

Threshold derivations (rule-no-magic-tuning — every number below is
grounded in an existing floor mechanism or a census band edge, and the
derivation is re-asserted by ``tests/pipeline_v2/test_uf_cases_split.py``):

* ``K`` — NOT a new number: ``max(min_action_families,
  _MIN_MINTABLE)`` == 3, the exact expression the journey lattice
  action axis uses (``journey-action-families.yaml`` +
  ``_CATCHALL_MIN_CLUSTERS`` agree on 3 — the minimum set that makes
  a "cases" partition).
* ``N`` (:data:`CASE_MEMBER_FLOOR` == 5) — one above the upper
  quartile of the mintable (>= ``_MIN_MINTABLE``) dir-cluster leaf
  sizes across the census giant class (probe canon 2026-07-21:
  P75 == 4 over 124 leaves of the twenty giants -> N == 5).  A case
  must out-mass the giant's own typical cluster grain, not merely
  reach the lattice's 2-member mintable floor: single-dir co-location
  is weaker per-member evidence than a shared head verb.
* JSX-surface share floor — 1/2, the half band (non-strict side of
  the same majority ratio the lattice's owned-entry gate uses).
* route-witness share floor — ``_I15_ATTACH_FLOOR`` (0.34), the
  validator's own attach-evidence ruler, reused verbatim.
* :data:`GIANT_MEMBER_FLOOR` (30) — the census band edge of the
  flagged giant class over the 18-board cold census (census canon:
  un-flagged large journeys top out at 29 — langfuse / papermark;
  every operator-flagged giant sits at >= 30).  Arithmetic
  corroboration: 30 == 2*K*N — the smallest parent where K minimal
  cases can coexist with a residual majority (failure mode is
  UNDER-split, never over-split).

Residuals still >= GIANT floor (twenty 136/72) are the HONEST DEBT of
iteration 2 (deeper descent / graph-traversal witness) — out of scope
here by spec.

Laws held: conservation by name (union == parent, defensive revert);
I14 — extracted members' flow backpointers repoint to their child,
never dangle (emission integrity's global pass runs BEFORE this seam);
R5 no-new-dup — a child name already worn by a live same-PF row is
decorated or the child folds to residual; display law — a law-dirty
mint never ships (R5-5 census-shape caps ride ``display_law_violations``
at mint; children are never born ``name_confidence="high"``).

Flag unset/``0`` ⇒ :func:`run_uf_cases_split` returns ``None`` before
touching anything ⇒ user_flows[] + scan_meta byte-identical (the KS
4-way gate).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from faultline.pipeline_v2.journey_lattice import (
    _ACTION_FAMILY_ORDER,
    _ACTION_VERDICT,
    _ACTION_WORD,
    _I15_ATTACH_FLOOR,
    _MIN_MINTABLE,
    _action_family,
    _entry_file_of,
    _intent_for_verb,
    _norm_token,
    load_action_families,
)
from faultline.pipeline_v2.naming_contract import (
    _uf_flow_maps,
    display_law_violations,
    load_naming_vocab,
    polish_display_casing,
)

__all__ = [
    "CASES_SPLIT_ENV",
    "CASE_MEMBER_FLOOR",
    "GIANT_MEMBER_FLOOR",
    "SURFACE_SHARE_FLOOR",
    "WITNESS_SHARE_FLOOR",
    "apply_uf_cases_split",
    "cases_split_enabled",
    "collect_external_dep_tokens",
    "min_case_children",
    "run_uf_cases_split",
]

CASES_SPLIT_ENV = "FAULTLINE_UF_CASES_SPLIT"

#: Census band edge of the flagged giant class (see module docstring);
#: arithmetic corroboration 2*K*N == 30.
GIANT_MEMBER_FLOOR = 30

#: One above the P75 of mintable (>= _MIN_MINTABLE) dir-cluster leaf sizes
#: across the census giant class (probe canon: P75 == 4 -> 5). Derivation
#: re-asserted from the canon fixture by the derivation unit.
CASE_MEMBER_FLOOR = 5

#: The half band — non-strict side of the lattice's majority ratio.
SURFACE_SHARE_FLOOR = 0.5

#: The validator's I15 attach-evidence ruler, reused verbatim.
WITNESS_SHARE_FLOOR = _I15_ATTACH_FLOOR

#: Child-id prefix (content-derived ids, lattice ``_child_id`` precedent).
_CASES_ID_PREFIX = "UF-CS-"

#: Scaffold path segments that never NAME a case (superset of the
#: stage-6.7 domain-signal skip set — generic structure, not domains).
_STRUCTURAL_SEGMENTS = frozenset({
    "components", "component", "utils", "util", "hooks", "hook", "lib",
    "libs", "types", "helpers", "common", "shared", "core", "base",
    "layouts", "styles", "assets", "constants", "src", "modules",
    "packages", "apps", "app", "pages", "views", "screens", "routes",
    "states", "state", "contexts", "providers", "selectors", "queries",
    "mutations", "graphql", "stories", "__stories__", "tests",
    "__tests__", "internal", "tabs", "tab",
})

#: Routing-convention segments — a leaf whose identity dissolves into the
#: parent's own tokens but which lives under one of these dirs is the
#: parent's PAGE-SURFACE case ("record pages").
_ROUTING_SEGMENTS = frozenset({"pages", "views", "screens", "routes"})

_JSX_SUFFIXES = (".tsx", ".jsx")

_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")


def cases_split_enabled() -> bool:
    """Default **ON** since the 2026-07-21 pack-3 flip (KEY_SCHEMA 34;
    sim-canon 209m→10 children / 131m→11 on the composite twenty world,
    86m golden 'Create and edit records' = 4 recognizable cases +
    residual lineage; keyless inertness ×3 topologies byte-ident).
    Unset ≡ explicit ``1``; explicit ``FAULTLINE_UF_CASES_SPLIT=0``
    keeps the seam un-entered, byte-identical (kill-switch forever)."""
    return os.environ.get(CASES_SPLIT_ENV, "1").strip() == "1"


def min_case_children() -> int:
    """K — literal reuse of the lattice action-axis expression (== 3)."""
    vocab = load_action_families()
    return max(int(vocab.get("min_action_families", 3) or 3), _MIN_MINTABLE)


# ── S1 manifest law — external dependency families (B19 mechanism) ────


def _dep_family_tokens(dep: str) -> set[str]:
    """Family token(s) of one dependency name (``technology_instruments``
    ``_dep_tokens`` shape): ``@tiptap/react`` -> {tiptap}; ``ai`` -> {ai};
    ``trigger.dev`` -> {triggerdev, trigger}."""
    out: set[str] = set()
    norm = lambda s: _TOKEN_RE.sub("", str(s).lower())  # noqa: E731
    if dep.startswith("@"):
        scope, _, suffix = dep[1:].partition("/")
        if scope == "types":
            out.add(norm(suffix))
        else:
            out.add(norm(scope.split(".")[0]))
    else:
        out.add(norm(dep))
        if "." in dep:
            out.add(norm(dep.split(".")[0]))
    out.discard("")
    return out


def collect_external_dep_tokens(repo_root: Any) -> frozenset[str]:
    """EXTERNAL dependency family tokens over every tracked ``package.json``
    (internal workspace names + scopes excluded — the S1 manifest law only
    ever names OUTSIDE vendors)."""
    if not repo_root:
        return frozenset()
    root = Path(str(repo_root))
    if not root.is_dir():
        return frozenset()
    manifests: list[dict[str, Any]] = []
    skip_dirs = {"node_modules", ".git", "dist", "build", ".next", "vendor"}
    for p in sorted(root.rglob("package.json")):
        if any(seg in skip_dirs for seg in p.relative_to(root).parts[:-1]):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            manifests.append(doc)
    internal_names = {str(d.get("name") or "") for d in manifests} - {""}
    internal_scopes = {
        n.split("/")[0] for n in internal_names if n.startswith("@")}
    out: set[str] = set()
    for doc in manifests:
        for block in ("dependencies", "devDependencies"):
            deps = doc.get(block) or {}
            if not isinstance(deps, dict):
                continue
            for dep, spec in deps.items():
                if not isinstance(dep, str):
                    continue
                if dep in internal_names:
                    continue
                if dep.split("/")[0] in internal_scopes:
                    continue
                if str(spec).startswith("workspace:"):
                    continue
                out |= _dep_family_tokens(dep)
    return frozenset(out)


def _segment_echoes_dep(segment: str, dep_tokens: frozenset[str]) -> bool:
    """S1 echo test for ONE dir segment: the whole normalized segment or
    its family head (first hyphen token) matches an external dep family
    (``tiptap-icons`` -> ``tiptap``; ``ai-elements`` -> ``ai``)."""
    norm = _TOKEN_RE.sub("", segment.lower())
    if norm and norm in dep_tokens:
        return True
    head = _TOKEN_RE.sub("", segment.lower().split("-")[0])
    return bool(head) and head in dep_tokens


def _is_vendored_candidate(
    leaf_dir: str,
    witnessed: int,
    dep_tokens: frozenset[str],
) -> bool:
    """The vendored guard: S3 no-product-surface (ZERO route-witnessed
    members) AND an S1 dependency-family echo on a non-structural dir
    segment. Runs BEFORE extraction — a guarded candidate never counts
    toward K and its members pool into the residual."""
    if witnessed > 0 or not dep_tokens:
        return False
    for seg in leaf_dir.split("/"):
        if not seg or seg.lower() in _STRUCTURAL_SEGMENTS:
            continue
        if _segment_echoes_dep(seg, dep_tokens):
            return True
    return False


# ── dir-tree descent ──────────────────────────────────────────────────


def _descend_cases(
    entries: list[tuple[str, str]],
    floor: int = CASE_MEMBER_FLOOR,
) -> list[tuple[str, list[str]]]:
    """Leaf-of-the->=floor-subtree candidates over ``(member_id,
    entry_file)`` pairs. A node recurses into every sub-bucket of size >=
    floor; a node with NO such sub-bucket is emitted as ONE candidate
    carrying all its members. Members outside every emitted candidate
    (files at recursed nodes, sub-floor buckets) pool into the residual.
    Deterministic: sorted segment iteration, original member order kept."""
    out: list[tuple[str, list[str]]] = []

    def rec(prefix: str, items: list[tuple[str, str]]) -> None:
        buckets: dict[str, list[tuple[str, str]]] = {}
        for mid, entry in items:
            rest = entry[len(prefix):].lstrip("/") if prefix else entry
            parts = rest.split("/")
            seg = parts[0] if len(parts) > 1 and parts[0] else ""
            buckets.setdefault(seg, []).append((mid, entry))
        big = {
            seg: sub for seg, sub in buckets.items()
            if seg and len(sub) >= floor
        }
        if not big:
            if prefix:
                out.append((prefix, [mid for mid, _ in items]))
            return
        for seg in sorted(big):
            rec(f"{prefix}/{seg}" if prefix else seg, big[seg])

    rec("", [(str(m), str(e)) for m, e in entries if e])
    return out


# ── naming channels ───────────────────────────────────────────────────


def _fold_tokens(text: str) -> set[str]:
    return {
        _norm_token(t)
        for t in _TOKEN_RE.split(str(text or "").lower())
        if t
    } - {""}


def _child_key(
    leaf_dir: str,
    parent_resource: str,
    parent_domain: str,
) -> str:
    """Deterministic case key from the leaf dir — dir-segment channel:
    the leaf's own last meaningful segment minus ancestor-segment +
    parent-identity tokens (probe canon: ``object-filter-dropdown`` under
    ``object-record`` -> ``filter-dropdown``; ``settings/security`` ->
    ``security``; ``pages/object-record`` -> ``record-pages``)."""
    raw_segs = [s for s in leaf_dir.split("/") if s]
    meaningful = [
        s for s in raw_segs if s.lower() not in _STRUCTURAL_SEGMENTS]
    parent_folded = _fold_tokens(parent_resource)
    domain_key = parent_domain.rsplit(":", 1)[-1] if parent_domain else ""
    parent_folded |= _fold_tokens(domain_key)
    if not meaningful:
        return ""
    last = meaningful[-1]
    last_toks = [t for t in _TOKEN_RE.split(last.lower()) if t]
    ancestor_folded: set[str] = set()
    for seg in meaningful[:-1]:
        ancestor_folded |= _fold_tokens(seg)
    own = [
        t for t in last_toks
        if _norm_token(t) not in ancestor_folded
        and _norm_token(t) not in parent_folded
    ]
    if len(own) == 1 and len(last_toks) > 1:
        # Ancestor strip over-ate ("multiple-record-picker" under
        # "record-picker" -> "multiple"): retry against the parent
        # identity only.
        relaxed = [
            t for t in last_toks if _norm_token(t) not in parent_folded]
        if len(relaxed) > len(own):
            own = relaxed
    if not own:
        routing = next(
            (s.lower() for s in raw_segs if s.lower() in _ROUTING_SEGMENTS),
            None)
        if routing:
            # The leaf names the parent itself under a routing dir — the
            # parent's page-surface case ("record pages").
            base = [t for t in _TOKEN_RE.split(
                str(parent_resource or "").lower()) if t]
            return "-".join(base + [routing]) if base else routing
        own = last_toks
    return "-".join(own)


def _child_verb_word(
    member_ids: Iterable[str],
    flow_by_id: Mapping[str, Any],
    action_vocab: Mapping[str, Any],
    parent_name: str,
) -> tuple[str, str]:
    """``(display_word, family)`` — verb census over member flow names
    through the lattice's ``_action_family`` channel; majority family
    wins (ties by ``_ACTION_FAMILY_ORDER``); no majority -> the parent's
    own leading word (it already ships law-clean on the board)."""
    counts: dict[str, int] = {}
    voters = 0
    for mid in member_ids:
        fl = flow_by_id.get(str(mid))
        fam = _action_family(
            str(getattr(fl, "name", "") or "") if fl is not None else "",
            action_vocab,
        )
        voters += 1
        if fam is not None:
            counts[fam] = counts.get(fam, 0) + 1
    if counts:
        best = sorted(
            counts,
            key=lambda f: (-counts[f], _ACTION_FAMILY_ORDER.index(f)
                           if f in _ACTION_FAMILY_ORDER
                           else len(_ACTION_FAMILY_ORDER)),
        )[0]
        # Majority over ALL members (the lattice's strict-majority ratio):
        # a family a minority of members voices is a single datapoint,
        # not the child's intent.
        if counts[best] * 2 > voters:
            return _ACTION_WORD.get(best, "Manage"), best
    lead = next(
        (w for w in re.split(r"[^A-Za-z]+", str(parent_name or "")) if w),
        "",
    )
    if lead:
        return lead[:1].upper() + lead[1:], "act"
    return "Manage", "act"


def _child_id(parent_id: str, leaf_dir: str) -> str:
    digest = hashlib.sha1(
        f"{parent_id}|{leaf_dir}".encode("utf-8")).hexdigest()
    return f"{_CASES_ID_PREFIX}{digest[:10]}"


# ── stage entry ───────────────────────────────────────────────────────


def run_uf_cases_split(
    user_flows: list[Any],
    flows: Iterable[Any] = (),
    *,
    routes_index: Iterable[Mapping[str, Any]] | None = None,
    repo_root: Any = None,
    vocab: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The flag-gated stage entry ``phase_finalize`` wires after the
    spray-generalization seam. OFF/unset ⇒ ``None`` BEFORE any read or
    mutation — the caller then writes no scan_meta key (byte-identity
    law)."""
    if not cases_split_enabled():
        return None
    v = vocab or load_naming_vocab()
    _, _, flow_by_id = _uf_flow_maps(flows)
    patterns_by_file: dict[str, list[str]] = {}
    for e in (routes_index or ()):
        if not isinstance(e, Mapping):
            continue
        f, p = str(e.get("file") or ""), str(e.get("pattern") or "")
        if f and p and p not in patterns_by_file.setdefault(f, []):
            patterns_by_file[f].append(p)
    dep_tokens = collect_external_dep_tokens(repo_root)
    return apply_uf_cases_split(
        user_flows, flow_by_id, v,
        patterns_by_file=patterns_by_file,
        dep_tokens=dep_tokens,
    )


def apply_uf_cases_split(
    user_flows: list[Any],
    flow_by_id: Mapping[str, Any],
    vocab: Mapping[str, Any],
    *,
    patterns_by_file: Mapping[str, list[str]] | None = None,
    dep_tokens: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Split every giant catch-all with >= K qualified dir-tree cases.

    Mutates in place (only ever called behind ``cases_split_enabled`` ->
    the OFF path never runs -> byte-identical). The parent row SURVIVES
    as the residual (id, name, lineage kept — survivor-id law); children
    are new rows inserted right after it; every extracted member's flow
    backpointer repoints to its child (I14). Conservation is checked
    per giant and a violating plan reverts defensively (lattice law).
    Returns telemetry."""
    from faultline.models.types import UserFlow

    patterns_by_file = patterns_by_file or {}
    route_files = frozenset(patterns_by_file)
    action_vocab = load_action_families()
    k_floor = max(
        int(action_vocab.get("min_action_families", 3) or 3), _MIN_MINTABLE)
    tele: dict[str, Any] = {
        "giants_seen": 0,
        "giants_split": 0,
        "children_minted": 0,
        "members_extracted": 0,
        "below_k_kept": 0,
        "vendored_rejected": 0,
        "name_folded": 0,
        "conservation_reverts": 0,
        "samples": [],
    }

    # Live flow objects deduped for the I14 repoint (flow_by_id keys both
    # uuid and name forms onto the same object).
    seen_fl: set[int] = set()
    live_flows: list[Any] = []
    for fl in flow_by_id.values():
        if id(fl) not in seen_fl:
            seen_fl.add(id(fl))
            live_flows.append(fl)

    # Snapshot of live names per PF (R5 no-new-dup ruler).
    def _names_of_pf(pfid: str) -> set[str]:
        return {
            str(getattr(u, "name", "") or "").strip().lower()
            for u in user_flows
            if str(getattr(u, "product_feature_id", "") or "") == pfid
        }

    insertions: list[tuple[int, list[Any]]] = []
    # Cross-giant mint ledger: children minted for an earlier giant of the
    # SAME capability are not yet inserted into user_flows — the no-dup
    # ruler must still see their names.
    minted_by_pf: dict[str, set[str]] = {}
    for idx, parent in enumerate(list(user_flows)):
        members = [
            str(m) for m in (getattr(parent, "member_flow_ids", None) or [])]
        if len(members) < GIANT_MEMBER_FLOOR:
            continue
        tele["giants_seen"] += 1
        entries: list[tuple[str, str]] = []
        for mid in members:
            fl = flow_by_id.get(mid)
            entry = _entry_file_of(fl) if fl is not None else None
            entries.append((mid, str(entry or "")))
        candidates = _descend_cases(entries)
        by_mid = dict(entries)

        qualified: list[dict[str, Any]] = []
        for leaf_dir, mids in candidates:
            n = len(mids)
            if n < CASE_MEMBER_FLOOR:
                continue
            jsx = sum(
                1 for m in mids
                if by_mid.get(m, "").endswith(_JSX_SUFFIXES))
            wit = sum(1 for m in mids if by_mid.get(m, "") in route_files)
            if (jsx / n) < SURFACE_SHARE_FLOOR and \
                    (wit / n) < WITNESS_SHARE_FLOOR:
                continue
            if _is_vendored_candidate(leaf_dir, wit, dep_tokens):
                tele["vendored_rejected"] += 1
                continue
            qualified.append({
                "leaf_dir": leaf_dir,
                "mids": list(mids),
                "witnessed": wit,
            })
        if len(qualified) < k_floor:
            tele["below_k_kept"] += 1
            continue

        parent_id = str(getattr(parent, "id", "") or "")
        parent_name = str(getattr(parent, "name", "") or "")
        parent_resource = str(getattr(parent, "resource", "") or "")
        parent_domain = str(getattr(parent, "domain", "") or "")
        parent_pfid = str(getattr(parent, "product_feature_id", "") or "")
        live_names = _names_of_pf(parent_pfid)
        live_names |= minted_by_pf.setdefault(parent_pfid, set())

        # Same-key coalesce: sibling qualified leaves whose identity
        # dissolves to ONE case key (structural sub-dirs of a single
        # case dir — components/hooks/utils under settings/applications)
        # are the SAME case; minting them separately would fabricate
        # decorated near-duplicate names. Union their members (still
        # pairwise disjoint — descent leaves never overlap). Keys stay
        # distinct on the probe canon, so this is canon-neutral.
        grouped: dict[str, dict[str, Any]] = {}
        for cand in qualified:
            key = _child_key(
                cand["leaf_dir"], parent_resource, parent_domain)
            if not key:
                tele["name_folded"] += 1
                continue
            g = grouped.setdefault(
                key, {"key": key, "mids": [], "witnessed": 0})
            g["mids"].extend(cand["mids"])
            g["witnessed"] += cand["witnessed"]
        if len(grouped) < k_floor:
            tele["below_k_kept"] += 1
            continue

        children: list[Any] = []
        claimed: list[str] = []
        claimed_set: set[str] = set()
        sample_children: list[dict[str, Any]] = []
        for key in sorted(grouped):
            cand = grouped[key]
            word, family = _child_verb_word(
                cand["mids"], flow_by_id, action_vocab, parent_name)
            phrase = key.replace("-", " ")
            # Decoration noun = the HEAD noun of the parent resource (the
            # LAST compound token: 'multiple-record' -> 'record').
            noun = ([t for t in _TOKEN_RE.split(parent_resource.lower())
                     if t] or [""])[-1]
            raw_candidates = [f"{word} {phrase}"]
            if noun and _norm_token(noun) not in _fold_tokens(phrase):
                raw_candidates.append(f"{word} {noun} {phrase}")
                raw_candidates.append(f"{word} {phrase} {noun}")
            name = ""
            for raw in raw_candidates:
                cand_name = polish_display_casing(raw, vocab)
                folded = cand_name.strip().lower()
                if folded in live_names:
                    continue
                if display_law_violations(cand_name, vocab):
                    # A law-dirty mint never ships (R5-5 caps ride the
                    # display law at mint time).
                    continue
                name = cand_name
                break
            if not name:
                tele["name_folded"] += 1
                continue
            live_names.add(name.strip().lower())
            minted_by_pf[parent_pfid].add(name.strip().lower())
            routes: list[str] = []
            child = UserFlow(
                id=_child_id(parent_id, key),
                name=name,
                resource=phrase,
                domain=f"cases:{key}",
                product_feature_id=(
                    getattr(parent, "product_feature_id", None)),
                intent=_intent_for_verb(
                    _ACTION_VERDICT.get(family, "manage")),
                member_flow_ids=list(cand["mids"]),
                member_count=len(cand["mids"]),
                routes=routes,
                refined=False,
                name_confidence=(
                    "medium" if cand["witnessed"] > 0 else "low"),
            )
            children.append(child)
            claimed.extend(cand["mids"])
            claimed_set.update(cand["mids"])
            if len(sample_children) < 16:
                sample_children.append({
                    "id": child.id, "key": key, "name": name,
                    "members": len(cand["mids"]),
                    "witnessed": cand["witnessed"],
                })
        if len(children) < k_floor:
            tele["below_k_kept"] += 1
            continue

        residual = [m for m in members if m not in claimed_set]
        # Conservation by name: union(children) + residual == parent.
        if sorted(claimed + residual) != sorted(members) or \
                len(claimed) != len(claimed_set):
            tele["conservation_reverts"] += 1
            continue

        # Routes re-sync from the routes_index pattern map (the lattice
        # apply's ``patterns_by_file`` law): each row carries exactly the
        # patterns its OWN members' entry files witness.
        entry_by_mid = dict(entries)

        def _routes_of(mids: list[str]) -> list[str]:
            pats: list[str] = []
            for m in mids:
                for p in patterns_by_file.get(entry_by_mid.get(m, ""), ()):
                    if p not in pats:
                        pats.append(p)
            return sorted(pats)

        for child in children:
            child.routes = _routes_of(child.member_flow_ids)

        # The parent survives as the residual — id/name/lineage kept.
        parent.member_flow_ids = residual
        parent.member_count = len(residual)
        if getattr(parent, "routes", None):
            parent.routes = _routes_of(residual)

        # I14 — repoint extracted members' backpointers, never dangle.
        by_child: dict[str, str] = {}
        for child in children:
            for m in child.member_flow_ids:
                by_child[m] = str(child.id)
        for fl in live_flows:
            for key_form in (
                    getattr(fl, "uuid", None), getattr(fl, "name", None)):
                target = by_child.get(str(key_form or ""))
                if target:
                    fl.user_flow_id = target
                    break

        insertions.append((idx, children))
        tele["giants_split"] += 1
        tele["children_minted"] += len(children)
        tele["members_extracted"] += len(claimed)
        if len(tele["samples"]) < 8:
            tele["samples"].append({
                "parent_id": parent_id,
                "parent_name": parent_name,
                "parent_members": len(members),
                "residual": len(residual),
                "children": sample_children,
            })

    # Insert children right after their parent (stable board adjacency);
    # process bottom-up so earlier indices stay valid.
    for idx, children in sorted(insertions, key=lambda t: -t[0]):
        user_flows[idx + 1:idx + 1] = children
    return tele
