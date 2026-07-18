"""Stage 6.985 — transport-lane journey-conservation handoff (B22 Phase-2).

THE PROBLEM (B19 keyed A/B, 2026-07-10): laning a transport package at
mint time (Stage 6.86) kills the journeys homed to it before they exist
— documenso ``packages/trpc`` 79→42 UFs, ~20 real product journeys
dissolved into ``Uncovered: … routes`` markers. The classifier verdict
("a transport is plumbing") is ratified; the COVERAGE HANDOFF was
missing. Operator doctrine (binding): **no journey may EVER be
dissolved**.

THE FIX — defer the act of laning until after the LAST journey
producer. Under ``FAULTLINE_TRANSPORT_LANE_HANDOFF`` (default ON, inert
unless the transport prong fires — ``FAULTLINE_TECH_TRANSPORT_LANE`` is
default OFF) the S2 transport verdict becomes a candidate MARK
(``technology_instruments`` emits ``transport_candidates`` instead of
instrument dirs), the unit mints normally, journeys mint normally, and
THIS stage — hooked in ``phase_finalize`` AFTER Stage 6.98b
e2e_orphan_uf and BEFORE the 6.97 LOC prefetch (the same "final journey
layer, before 6.97" slot family as lane_rehome, so loc-truth I13 and
lane accounting hold with zero extra plumbing) — re-homes every homed
journey to the product PF it actually serves, re-homes the annexed
product devs, and only THEN converts the candidate PF into a
platform-infrastructure lane resident.

RUNG LADDER (per homed UF, deterministic, $0 LLM):

  r1 — strict-majority owner over the journey's non-lane span mass:
       per span file, the completed owner ladder = existing owner map
       (dev ``product_feature_id``; the candidate itself never votes —
       its ownership is the annexation under audit) → route-anchor
       target-grain completion. Strict = top target > 50% of voting
       mass.
  r2 — deterministic consumer completion for the still-unresolved
       seeds: the candidate package's own EXPORT SURFACE (exported
       nested-object key paths, matched against member-access chains
       rooted at identifiers imported FROM the candidate — no framework
       vocabulary; the dotted-path map is derived from the package's
       exports) + reverse imports INTO the candidate (type-file
       channel) + a reverse-import walk (depth ≤ 2) with a HUB CUTOFF
       (share-scaled, see :func:`hub_cutoff`) for unowned seeds.
       Per-seed winner-take-all, mass-weighted; a seed with no strict
       consumer majority abstains (never pollutes).
  r3 — plurality LAST-RESORT rung (sub-flag
       ``FAULTLINE_TRANSPORT_HANDOFF_PLURALITY``, default ON): when the
       alternative is dissolution a plurality home is the least-churn
       conserving move — accepted ONLY when (a) top1 strictly beats
       top2 (a 50/50 split NEVER re-homes) and (b) the move creates NO
       new I16 row under the entry-owner ruler projected over the
       POST-handoff owner map (the B22 plurality rail; lane-neutral per
       B21). Every r3 accept carries a per-UF telemetry marker.
  route-URL — synthesized ``member_count=0`` UFs (route-recall seeds)
       re-home by deterministic route-URL → route-file → target-grain
       mapping; they never touch the span machinery.

CONSERVATION GATE (operator law, mechanized): the handoff is
all-or-nothing per candidate PF. It lanes ONLY if EVERY homed UF
resolved a target AND every resolution clears the ATTACH FLOOR (Phase-2
rework: the journey's projected lane-aware attach at its target must
clear the validator's own I15 0.34 ruler — thin majorities never ship a
home the journey's files barely touch) AND no re-home creates a NEW I16
row (the rail, every rung) AND every NEW target has a contributor (the
mint is never a phantom) AND no FLOWFUL dev would land in the lane
(validator I9: the platform lane is flowless plumbing only). ANY failed
leg → the candidate does NOT lane — the scan output for that PF is
exactly the flag-OFF output plus
``scan_meta.transport_handoff.conservation_blocked`` telemetry with
per-UF reasons. The stage plans first and applies only a verified plan;
a hard UF-count invariant (before == after, no other PF loses a
journey) backstops the construction — violations raise under
pytest/``FAULTLINE_STRICT_CONSERVATION=1`` and warn-telemeter in prod.

ONE ATOMIC TARGET GRAIN (design risk #1, the 17/25-NEW-PF hazard): the
UF vote, the dev re-home vote AND the late-mint excavator all consult
ONE :class:`TargetGrainIndex` built once per run — the excavator mints
exactly the grain the vote selected (``GrainTarget`` identity), and the
plan re-verifies every re-home key against the minted set inside the
same atomic stage.

ORPHAN GUARD (B20 inversion): Stage 6.99's guard protects a source PF's
LAST journey; here the source PF is dissolving, so the guard flips —
the handoff is atomic and every re-home target ends flowful BY
CONSTRUCTION (it receives the journey being moved; a NEW-minted target
additionally requires a contributing dev). The plan verifier asserts no
OTHER PF's journey count ever decreases (B20's ``uf_count`` bookkeeping
shape, inverted).

Deterministic, $0 LLM. Kill-switch ``FAULTLINE_TRANSPORT_LANE_HANDOFF=0``
restores mint-time laning (B19 behavior) when the transport prong is ON,
and is byte-identical to HEAD when it is OFF (today's default).
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from faultline.pipeline_v2.overturn_ledger import propose_pf_now

__all__ = [
    "TRANSPORT_HANDOFF_ENV",
    "TRANSPORT_HANDOFF_PLURALITY_ENV",
    "TRANSPORT_NAMESPACE_ECHO_ENV",
    "TRANSPORT_ROUTER_DECOMP_ENV",
    "FLOWFUL_TRANSPORT_LANE_ENV",
    "transport_handoff_enabled",
    "transport_plurality_enabled",
    "transport_namespace_echo_enabled",
    "transport_router_decomp_enabled",
    "flowful_transport_lane_enabled",
    "hub_cutoff",
    "GrainTarget",
    "TargetGrainIndex",
    "NamespaceEcho",
    "resolve_user_flow",
    "run_transport_handoff",
]

TRANSPORT_HANDOFF_ENV = "FAULTLINE_TRANSPORT_LANE_HANDOFF"
TRANSPORT_HANDOFF_PLURALITY_ENV = "FAULTLINE_TRANSPORT_HANDOFF_PLURALITY"
#: B49 r2.6 namespace-echo rung — default OFF.
TRANSPORT_NAMESPACE_ECHO_ENV = "FAULTLINE_TRANSPORT_NAMESPACE_ECHO"
#: B51 router-mega decomposition pass — default OFF.
TRANSPORT_ROUTER_DECOMP_ENV = "FAULTLINE_TRANSPORT_ROUTER_DECOMP"
#: B52 flow-bearing transport lane (Option A) — default OFF. The ONE
#: cycle switch: it also drives the B51 decomposition pass (in
#: drain-then-lane mode) without needing the B51 flag.
FLOWFUL_TRANSPORT_LANE_ENV = "FAULTLINE_FLOWFUL_TRANSPORT_LANE"

#: Provenance marker stamped on re-homed / minted rows (I22
#: explainability + idempotence).
_HANDOFF_MARKER = "transport-handoff"
#: B51 provenance marker for router-decomposition carve chunks (I22).
_ROUTER_DECOMP_MARKER = "transport-router-decomp"
#: B52 provenance anchor stamped ONLY on a FLOWFUL dev the flowful-lane
#: branch lanes (legacy never lanes a flowful dev → the field changes
#: only in the flag-ON world). The lane builder keys ``flow_ids[]`` on
#: THIS marker, so a pre-existing flowful lane resident (documenso
#: ``openpage-api``: 32 flows, shell_lineage_only) stays byte-identical
#: under the flag — the E==C SACRED proof.
FLOWFUL_LANE_ANCHOR = f"lane:{_HANDOFF_MARKER}-flowful"

#: Coverage telemetry floor (design §4 thin-coverage class): a strict
#: re-home whose VOTING mass covers < 34% of the journey's span mass is
#: marked in telemetry.
_THIN_COVERAGE = 0.34

#: Attach-floor gate (Phase-2 rework, 2026-07-10): a rung's target only
#: counts as RESOLVED when the journey's PROJECTED lane-aware attach at
#: the target clears the SAME 0.34 floor the validator's I15 gate uses
#: (the E-report random-tail bound — one provenance, one value, no new
#: constant). The keyed A/B showed thin re-homes shipping fresh
#: I15/I16 rows; thin target ⇒ UNRESOLVED ⇒ the all-or-nothing gate
#: refuses the candidate (journeys stay put — refusal is success).
_ATTACH_FLOOR = _THIN_COVERAGE

#: Sub-second per-file read guard for the export-surface parser.
_MAX_PARSE_BYTES = 512 * 1024


def transport_handoff_enabled() -> bool:
    """Default ON; ``FAULTLINE_TRANSPORT_LANE_HANDOFF=0`` restores the
    B19 mint-time laning (and is byte-identical to HEAD while the
    transport prong itself is OFF)."""
    return os.environ.get(TRANSPORT_HANDOFF_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def transport_plurality_enabled() -> bool:
    """r3 sub-flag — default ON under the handoff, separately killable."""
    return os.environ.get(
        TRANSPORT_HANDOFF_PLURALITY_ENV, "1",
    ).strip().lower() not in {"0", "false"}


def transport_namespace_echo_enabled() -> bool:
    """B49 r2.6 rung — default OFF. When ON, an in-lane tRPC router seed
    that abstains at r2 (typed-proxy consumption leaves it with no
    product consumers) votes its span mass for the EXISTING product PF
    whose anchor-identity its namespace token echoes — the SAME
    normalized keys the S3-nav echo uses (``normalize_anchor_key``).
    Re-homes ONLY onto existing PFs (never mints); a token matching >1
    PF (ambiguous) or 0 PFs (generic/no-surface) abstains — the
    all-or-nothing conservation judge is untouched, r2.6 only adds
    votes for otherwise-abstaining seeds."""
    return os.environ.get(
        TRANSPORT_NAMESPACE_ECHO_ENV, "0",
    ).strip().lower() in {"1", "true"}


def transport_router_decomp_enabled() -> bool:
    """B51 router-mega decomposition — default OFF. When ON, a FLOW-BEARING
    transport candidate (a dev carrying flows that I9 forbids laning) is
    decomposed per tRPC sub-router: each sub-router group whose namespace
    token echoes an EXISTING product PF (the SAME ``NamespaceEcho`` matcher
    as r2.6) has its flows + routers-tree files re-homed to that PF, BEFORE
    the conservation gate. Matched groups leave (the tile shrinks); residue
    (unmatched sub-routers + non-routers ``[trpc].ts``/middleware flows)
    stays flowful and holds the tile — an honest abstain, never forced.
    Re-homed routers files are LIFTED out of the lane so the existing
    resolve_user_flow ladder (r1) drains their journeys with NO new rung.
    Conservation-safe: flows/files are MOVED (lineage re-stamped), never
    dropped or minted."""
    return os.environ.get(
        TRANSPORT_ROUTER_DECOMP_ENV, "0",
    ).strip().lower() in {"1", "true"}


def flowful_transport_lane_enabled() -> bool:
    """B52 flow-bearing transport lane (Option A) — default ON (flipped
    B62, KEY_SCHEMA 29); ``FAULTLINE_FLOWFUL_TRANSPORT_LANE=0`` disables.
    When ON, a ws-anchored transport candidate that cannot fully drain leaves the
    product layer ANYWAY: the B51 decomposition runs in drain-then-lane
    mode (matched sub-router/handler groups re-home WITH their journeys;
    the (c) handler grain ``api/trpc/<domain>/…`` is legal), the flowful
    RESIDUE lanes (Seg1 — the engine aligns with validator I9's existing
    ``ws:``-anchor exemption), and every transport-INTRINSIC journey
    stays in ``user_flows[]`` as a lane row reference
    (``product_feature_id=None`` + ``lane_ref=<lane-row uuid>`` +
    ``surface_scope='platform_infrastructure'``). Nothing is dropped:
    Σflows == product-homed + lane flow_ids, ΣUF == product-homed +
    lane_ref rows. Mint is FORBIDDEN on this path (receivers are only
    EXISTING PFs); a receiver that would end journey-less pulls its
    carve back (I8 backstop). The ONE cycle switch — it drives the B51
    pass without needing ``FAULTLINE_TRANSPORT_ROUTER_DECOMP``."""
    return os.environ.get(
        FLOWFUL_TRANSPORT_LANE_ENV, "1",
    ).strip().lower() in {"1", "true"}


def _strict_conservation() -> bool:
    """Raise (instead of warn) on a conservation-invariant violation —
    always under pytest, or with ``FAULTLINE_STRICT_CONSERVATION=1``."""
    if os.environ.get("FAULTLINE_STRICT_CONSERVATION", "").strip().lower() \
            in {"1", "true"}:
        return True
    return "PYTEST_CURRENT_TEST" in os.environ


def hub_cutoff(n_ts_files: int) -> int:
    """Scale-invariant hub cutoff for the reverse-import walk — a file
    with more distinct importers than this abstains (shared substrate:
    the documenso ``use-toast`` 134-importer trap). Ships as
    ``max(floor, share·|repo ts files|)`` per rule-no-magic-tuning:
    ceil(1%) with a floor of 10 (documenso ≈ 2.6K ts files → 26; the
    Phase-1 prototype's calibrated 25). Monotone in repo size by
    construction (unit-tested)."""
    return max(10, -(-int(n_ts_files) // 100))


def _attr(o: Any, name: str, default: Any = None) -> Any:
    return o.get(name, default) if isinstance(o, dict) else \
        getattr(o, name, default)


# ── Target grain (THE single oracle — vote == mint, condition 4) ────────


@dataclass(frozen=True)
class GrainTarget:
    """One re-home target at the atomic grain.

    ``kind == "pf"``: an EXISTING product feature (``key`` = the
    ``product_feature_id`` value journeys/devs point at).
    ``kind == "new"``: a route-group PF the excavator will mint
    (``key`` = the anchor ``canonical_id``; the apply step assigns the
    slug and re-verifies every vote key against the minted set)."""

    kind: str            # "pf" | "new"
    key: str
    display: str = ""


class TargetGrainIndex:
    """The one target-grain function (design risk #1).

    Built ONCE per run from the repo's merged spine-anchor set + the
    live PF list. Both the vote (:func:`resolve_user_flow`, dev
    re-home) and the late-mint excavator consult THIS object, so the
    vote target and the minted target cannot diverge by construction.

    ``grain_of_file`` answers, in order of SPECIFICITY (longest matched
    prefix wins; exact-file anchors beat any prefix):

      * a PF-BACKED anchor whose subtree contains the file → that PF
        (``t.$team-url+`` / ``settings+`` / ``admin+`` keep their own
        journeys — the existing product grain outranks a NEW group at
        equal-or-deeper specificity);
      * else, a file under a ROUTES ROOT (roots derived from the
        routes_index file population via the spine's
        ``_route_root_end`` — dialect-blind) → the NEW route-GROUP
        target at the top-level group dir (``embed+``,
        ``_authenticated+``, ``_recipient+``, ``(marketing)`` …) — the
        ratified excavation grain: one author route group = one
        candidate PF, so sibling page votes POOL instead of
        fragmenting into per-page anchors (the design's 17/25
        NEW-target risk, observed live on the documenso offline sim);
      * else ``None`` (the consumer rung may still resolve the file).

    Anchors/groups inside the candidate unit / instrument dirs never
    answer (lane is never a target — B20 law).
    """

    def __init__(
        self,
        anchors: Iterable[Any],
        product_features: Iterable[Any],
        routes_index: Iterable[Mapping[str, Any]] | None = None,
        excluded_units: Iterable[str] = (),
        candidate_pf_keys: Iterable[str] = (),
        tenant_descent: bool = False,
    ) -> None:
        #: B24 (Stage 6.986) opt-in rung: the route-GROUP grain descends
        #: through tenant-address pairs (``project/[ref]/database`` keys
        #: ``database``, not ``project``) — see :func:`_tenant_descend`.
        #: Default OFF: 6.985 callers keep the ratified B22 grain
        #: byte-identically.
        self._tenant_descent = bool(tenant_descent)
        self._excluded = tuple(sorted(str(u).strip("/")
                                      for u in excluded_units if u))
        self._cand_keys = frozenset(candidate_pf_keys)
        pf_by_anchor: dict[str, Any] = {}
        pf_keys: set[str] = set()
        for pf in product_features:
            key = _attr(pf, "id") or _attr(pf, "name")
            if key:
                pf_keys.add(str(key))
            aid = _attr(pf, "anchor_id")
            if aid and key:
                pf_by_anchor.setdefault(str(aid), str(key))
        self._pf_by_anchor = pf_by_anchor
        self.pf_keys = frozenset(pf_keys)
        # Only PF-BACKED anchors participate in matching — the NEW grain
        # is the route-GROUP channel below, never a per-page anchor.
        self._anchors: list[Any] = []
        for a in sorted(anchors, key=lambda x: x.canonical_id):
            if getattr(a, "shell", False) or getattr(a, "barred", None):
                continue
            if a.canonical_id not in pf_by_anchor:
                continue
            units = list(getattr(a, "prefixes", ()) or ()) + sorted(
                getattr(a, "files", ()) or ())
            if units and all(self._in_excluded(u) for u in units):
                continue  # anchor lives wholly inside a lane unit
            self._anchors.append(a)
        # Routes roots + ALLOWED group prefixes from the routes_index
        # file population — PRODUCT-scoped entries only (W2a surface
        # taxonomy rides on the entries): a docs/marketing/legal route
        # group is never a journey re-home target (design Q3 —
        # cross-lane journeys are the gate's reason to exist, not a
        # grain source).
        from faultline.pipeline_v2.spine_anchors import _route_root_end
        roots: set[str] = set()
        route_files: list[str] = []
        for e in (routes_index or []):
            if not isinstance(e, Mapping):
                continue
            scope = e.get("surface_scope")
            if scope not in (None, "", "product"):
                continue
            f = str(e.get("file") or "")
            segs = [s for s in f.replace("\\", "/").split("/") if s]
            end = _route_root_end(segs)
            if end is not None and end < len(segs):
                roots.add("/".join(segs[:end]))
                route_files.append(f)
        self._roots = sorted(roots, key=len, reverse=True)
        self._display: dict[str, str] = {}
        self._memo: dict[str, GrainTarget | None] = {}
        # Group prefixes are ALLOWED only where a product-scoped route
        # file actually lives (a foreign app sharing the same root
        # shape never becomes a target by prefix accident).
        self._allowed_groups: set[str] = set()
        for f in route_files:
            g = self._route_group_of(f, check_allowed=False)
            if g is not None:
                self._allowed_groups.add(g[0])

    def _in_excluded(self, path: str) -> bool:
        return any(path == u or path.startswith(u + "/")
                   for u in self._excluded)

    def _route_group_of(
        self, path: str, check_allowed: bool = True,
    ) -> tuple[str, str, int] | None:
        """``(canonical_id, display, specificity)`` of the route-GROUP
        target containing *path*, or ``None`` (not under a routes root
        / not an allowed product-scoped group)."""
        for root in self._roots:
            if not path.startswith(root + "/"):
                continue
            rest = path[len(root) + 1:].split("/")
            seg1 = rest[0]
            if len(rest) == 1:
                # Leaf file directly at the root (central routers /
                # flat-route dialects): the stem's head atom keys the
                # group.
                seg1 = seg1.rsplit(".", 1)[0]  # strip extension
                seg1 = seg1.split(".", 1)[0] or seg1
            prefix = f"{root}/{seg1}"
            if self._tenant_descent and len(rest) > 1:
                ext = _tenant_descend(rest)
                if ext is not None:
                    seg1, rel_prefix = ext
                    prefix = f"{root}/{rel_prefix}"
            cid = f"route:{prefix}"
            if check_allowed and cid not in self._allowed_groups:
                return None
            return (cid, _group_display(seg1), len(prefix))
        return None

    @property
    def routes_roots(self) -> tuple[str, ...]:
        """The product-scoped routes roots (longest-first) — read-only
        (B24 same-app rail)."""
        return tuple(self._roots)

    def grain_of_file(self, path: str) -> GrainTarget | None:
        if path in self._memo:
            return self._memo[path]
        if self._in_excluded(path):
            self._memo[path] = None
            return None
        best: Any = None
        best_spec = -1
        for a in self._anchors:
            if path in (getattr(a, "files", None) or frozenset()):
                spec = len(path) + 1_000_000  # exact-file beats any prefix
            else:
                spec = -1
                for pre in getattr(a, "prefixes", ()) or ():
                    if (path == pre or path.startswith(pre + "/")) \
                            and len(pre) > spec:
                        spec = len(pre)
                if spec < 0:
                    continue
                if not a.matches(path):  # exclude_* carve-outs
                    continue
            if spec > best_spec:
                best, best_spec = a, spec
        group = self._route_group_of(path)
        out: GrainTarget | None = None
        if best is not None and (group is None or best_spec >= group[2]):
            pf_key = self._pf_by_anchor.get(best.canonical_id)
            if pf_key is not None and pf_key not in self._cand_keys:
                out = GrainTarget("pf", pf_key,
                                  display=getattr(best, "display", "") or "")
        elif group is not None:
            cid, display, _spec = group
            self._display.setdefault(cid, display)
            pf_key = self._pf_by_anchor.get(cid)
            if pf_key is not None:
                # A PF already sits at EXACTLY this grain — never mint a
                # twin; the group answers that PF.
                if pf_key not in self._cand_keys:
                    out = GrainTarget("pf", pf_key, display=display)
            else:
                out = GrainTarget("new", cid, display=display)
        self._memo[path] = out
        return out

    def display_of(self, canonical_id: str) -> str:
        return self._display.get(
            canonical_id,
            _group_display(canonical_id.rsplit("/", 1)[-1]))

    def group_cid_of(self, path: str) -> str | None:
        """The route-GROUP cid containing *path* (allowed groups only)
        — the carve predicate: virtual flat-route groups can't be
        matched by a plain prefix test."""
        g = self._route_group_of(path)
        return g[0] if g is not None else None


#: Framework leaf-file stems that never key a nav area (Next App Router
#: page/route conventions + Remix index markers) — structural addressing,
#: mirrors the 6.7d structure-leak class.
_AREA_LEAF_STEMS = frozenset({
    "page", "route", "index", "layout", "template", "loading", "error",
    "not-found", "_index",
})


def _tenant_descend(segs: list[str]) -> tuple[str, str] | None:
    """B24 nav-area rung: ``(area_token, literal_prefix)`` after
    consuming ``(static, dynamic+)`` tenant-address pairs while a deeper
    non-CRUD static segment exists (the W4 tenancy-transparency rule of
    ``spine_anchors._pattern_key_chain``, applied to the group grain):
    ``project/[ref]/database/backups.tsx`` keys ``database``,
    ``org/[slug]/sso.tsx`` keys ``sso`` (leaf stem), while
    ``documents/[id]/edit`` keeps the B22 group (CRUD leaves never key)
    and ``support/new.tsx`` keeps ``support`` (no tenant pair).
    ``None`` → no descent (the caller keeps the B22 top-level group)."""
    from faultline.pipeline_v2.journey_lattice import _CRUD_LEAF_SEGS
    from faultline.pipeline_v2.spine_anchors import _DYNAMIC_RE

    def _stem(seg: str) -> str:
        s = seg.rsplit(".", 1)[0]
        return s.split(".", 1)[0] or s

    def _transparent(seg: str) -> bool:
        # protocol/version addressing — never a nav area itself when a
        # deeper segment exists (the _pattern_key_chain transparency
        # class; live exhibit: apps/studio/app/api/... minting a PF
        # named "api")
        return seg in ("api", "trpc", "rest", "graphql", "rpc") or bool(
            re.match(r"^v\d+$", seg))

    def _valid_area(tok: str) -> bool:
        return bool(tok) and not _DYNAMIC_RE.match(tok) \
            and tok not in _CRUD_LEAF_SEGS and tok not in _AREA_LEAF_STEMS

    i, n = 0, len(segs)
    descended = False
    while True:
        # transparent hop: api / trpc / vN with deeper segments
        if i + 1 < n and _transparent(segs[i]):
            j = i + 1
            tok = _stem(segs[j]) if j == n - 1 else segs[j]
            if (_transparent(segs[j]) or _DYNAMIC_RE.match(segs[j])) \
                    and j < n - 1:
                i = j  # api/v1/… — keep hopping
                continue
            if _valid_area(tok) and not _transparent(tok):
                i, descended = j, True
                continue
            break
        # tenant-address pair: static + dynamic+ with a deeper
        # non-CRUD static beyond
        if i + 1 < n and not _DYNAMIC_RE.match(segs[i]) \
                and _DYNAMIC_RE.match(segs[i + 1]):
            j = i + 2
            while j < n and _DYNAMIC_RE.match(segs[j]):
                j += 1
            if j >= n:
                break
            tok = _stem(segs[j]) if j == n - 1 else segs[j]
            if not _valid_area(tok):
                break
            i, descended = j, True
            continue
        break
    if not descended:
        return None
    if i == n - 1:  # leaf file keys by extensionless stem
        return (_stem(segs[i]), "/".join(segs[:i] + [_stem(segs[i])]))
    return (segs[i], "/".join(segs[: i + 1]))


def _group_display(seg: str) -> str:
    """Route-group dir segment → display label (``_recipient+`` →
    ``Recipient``, ``(marketing)`` → ``Marketing``, ``embed+`` →
    ``Embed``, ``o.$orgUrl.settings`` → ``O``-head atom class)."""
    raw = seg.strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    raw = raw.lstrip("_").rstrip("+")
    raw = raw.split(".", 1)[0]  # flat-route dot-chains key the head atom
    words = re.split(
        r"[-_\s]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", raw).strip())
    return " ".join(
        w if (w.isupper() and len(w) > 1) else w.capitalize()
        for w in words if w
    ) or (raw or seg)


def _norm_route_segs(pattern: str) -> tuple[str, ...]:
    """Route pattern OR live URL → comparable atoms.

    Dialect-blind: ``/``-segments are further split on ``.``
    (flat-route dot-chains); URL-invisible atoms drop (leading-``_``
    pathless layouts / ``_index`` markers, ``(group)`` dirs); the
    flat-route ``+`` dir suffix strips; params of every dialect → ``*``
    — so the file-flavored ``/_recipient+/sign.$token`` and the live
    URL ``/sign/:param`` normalize to the same ``("sign", "*")``."""
    from faultline.pipeline_v2.spine_anchors import _DYNAMIC_RE

    out: list[str] = []
    for seg in str(pattern or "").split("/"):
        for atom in seg.split("."):
            atom = atom.strip()
            if not atom:
                continue
            if atom.startswith("(") and atom.endswith(")"):
                continue
            if atom.endswith("+"):
                atom = atom[:-1]
            if not atom or atom.startswith("_"):
                continue
            out.append("*" if _DYNAMIC_RE.match(atom) else atom.lower())
    return tuple(out)


class RouteUrlResolver:
    """Deterministic route-URL → route-file → target grain (the
    synthesized ``member_count=0`` UFs' only signal). Exact normalized
    pattern match first; else the entry sharing the longest common
    leading segment run (params wildcard-equal); ties → smallest file."""

    def __init__(self, routes_index: Iterable[Mapping[str, Any]] | None,
                 grain: TargetGrainIndex) -> None:
        self._grain = grain
        self._entries: list[tuple[tuple[str, ...], str]] = []
        seen: set[tuple[tuple[str, ...], str]] = set()
        for e in (routes_index or []):
            if not isinstance(e, Mapping):
                continue
            f = str(e.get("file") or "")
            pat = e.get("pattern") or e.get("path") or ""
            if not f or not pat:
                continue
            key = (_norm_route_segs(str(pat)), f)
            if key not in seen:
                seen.add(key)
                self._entries.append(key)
        self._entries.sort()

    def grain_of_route(self, url: str) -> GrainTarget | None:
        """Best entry by (exact-atom matches, run length, closest
        length) — EXACT atom equality outranks wildcard alignment, so a
        literal ``/sign/:param`` pattern always beats a fully-dynamic
        catch-all whose leading param happens to align (the documenso
        ``[__htmltopdf]`` trap, offline sim 2026-07-10)."""
        want = _norm_route_segs(url)
        if not want:
            return None
        best_file: str | None = None
        best_score: tuple[int, int, int] = (0, 0, 0)
        for segs, f in self._entries:
            exacts = run = 0
            for a, b in zip(segs, want):
                if a == b and a != "*":
                    exacts += 1
                    run += 1
                elif a == b or a == "*" or b == "*":
                    run += 1
                else:
                    break
            score = (exacts, run, -abs(len(segs) - len(want)))
            if run and score > best_score:
                best_score, best_file = score, f
        if best_file is None:
            return None
        return self._grain.grain_of_file(best_file)


# ── Consumer completion (r2) ─────────────────────────────────────────────


_IMPORT_BINDING_RE = re.compile(
    r"import\s+(?:type\s+)?"
    r"(?:([\w$]+)\s*,\s*)?"                      # default binding
    r"(?:\*\s+as\s+([\w$]+)|\{([^}]*)\})?"       # ns / named bindings
    r"\s*from\s*['\"]([^'\"]+)['\"]",
)
_EXPORT_OBJ_RE = re.compile(
    r"(?:export\s+)?const\s+([\w$]+)\s*=\s*((?:[\w$.]+\s*\(\s*)*)\{",
)
_REL_IMPORT_RE = re.compile(
    r"import\s+\{([^}]*)\}\s*from\s*['\"](\.[^'\"]*)['\"]",
)
_KEY_RE = re.compile(r"([A-Za-z0-9_$]+)\s*:\s*")
_IDENT_RE = re.compile(r"([A-Za-z0-9_$]+)")


def _read_text(repo_root: Path, rel: str) -> str | None:
    try:
        p = repo_root / rel
        if p.stat().st_size > _MAX_PARSE_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _parse_export_object(
    body: str, prefix: list[str], imports: Mapping[str, str],
    out: dict[tuple[str, ...], str],
) -> None:
    """Recursive nested-object-literal walk (the Phase-1 finding: plain
    object literals nest routers — a flat parse mapped 66 leaf paths, a
    recursive one 192). ``key: {…}`` / ``key: fn({…})`` recurse;
    ``key: Identifier`` with the identifier imported from a RELATIVE
    unit file records ``prefix+key → that file``."""
    i = 0
    n = len(body)
    while i < n:
        m = _KEY_RE.match(body, i)
        if not m:
            i += 1
            continue
        key = m.group(1)
        j = m.end()
        # Skip through call wrappers: `key: router({`, `key: t.router({`.
        k = j
        wrap = re.compile(r"[\w$.]+\s*\(\s*").match(body, k)
        while wrap:
            k = wrap.end()
            wrap = re.compile(r"[\w$.]+\s*\(\s*").match(body, k)
        if k < n and body[k] == "{":
            lvl, e = 1, k + 1
            while e < n and lvl:
                if body[e] == "{":
                    lvl += 1
                elif body[e] == "}":
                    lvl -= 1
                e += 1
            _parse_export_object(
                body[k + 1:e - 1], prefix + [key], imports, out)
            i = e
        else:
            vm = _IDENT_RE.match(body, j)
            if vm and vm.group(1) in imports:
                out[tuple(prefix + [key])] = imports[vm.group(1)]
            i = vm.end() if vm else j + 1


def _resolve_relative(base_file: str, spec: str,
                      unit_files: frozenset[str]) -> str | None:
    parts: list[str] = []
    joined = base_file.rsplit("/", 1)[0] + "/" + spec
    for seg in joined.split("/"):
        if seg == "..":
            if parts:
                parts.pop()
        elif seg not in (".", ""):
            parts.append(seg)
    base = "/".join(parts)
    for cand in (base, base + ".ts", base + ".tsx", base + "/index.ts",
                 base + "/index.tsx"):
        if cand in unit_files:
            return cand
    return None


def _export_surface_index(
    repo_root: Path, unit: str, unit_files: frozenset[str],
) -> dict[str, str]:
    """Dotted export-surface path → defining unit file.

    Per unit file: exported object literals parsed recursively; leaf
    identifiers resolved through the file's own relative imports. The
    per-file trees are then composed transitively (a root file whose
    leaf is another unit file splices that file's tree under the leaf's
    key path), yielding fully-qualified dotted paths — the export
    surface the package's consumers address. No framework vocabulary:
    everything derives from the package's own source."""
    per_file: dict[str, dict[tuple[str, ...], str]] = {}
    for rel in sorted(unit_files):
        if not rel.endswith((".ts", ".tsx")) or rel.endswith(".d.ts"):
            continue
        text = _read_text(repo_root, rel)
        if not text:
            continue
        imports = {}
        for m in _REL_IMPORT_RE.finditer(text):
            tgt = _resolve_relative(rel, m.group(2), unit_files)
            if tgt is None:
                continue
            for name in m.group(1).split(","):
                name = name.strip().split(" as ")[-1].strip()
                if name:
                    imports[name] = tgt
        tree: dict[tuple[str, ...], str] = {}
        for m in _EXPORT_OBJ_RE.finditer(text):
            k = m.end() - 1  # the "{"
            lvl, e = 1, k + 1
            n = len(text)
            while e < n and lvl:
                if text[e] == "{":
                    lvl += 1
                elif text[e] == "}":
                    lvl -= 1
                e += 1
            _parse_export_object(text[k + 1:e - 1], [], imports, tree)
        if tree:
            per_file[rel] = tree

    dotted: dict[str, str] = {}

    def _expand(file: str, prefix: tuple[str, ...], depth: int,
                seen: frozenset[str]) -> None:
        if len(dotted) > 20_000:  # defensive cap
            return
        for keys in sorted(per_file.get(file, {})):
            tgt = per_file[file][keys]
            full = prefix + keys
            path = ".".join(full)
            dotted.setdefault(path, tgt)
            if tgt in per_file and tgt not in seen and depth < 6:
                _expand(tgt, full, depth + 1, seen | {tgt})

    for rel in sorted(per_file):
        _expand(rel, (), 0, frozenset({rel}))
    return dotted


class ConsumerIndex:
    """r2 machinery: reverse-import graph + the candidate unit's export
    surface, built once per run over the tracked ts/js population."""

    def __init__(
        self,
        repo_root: Path,
        tracked: list[str],
        unit: str,
        ctx: Any = None,
    ) -> None:
        from faultline.pipeline_v2.stage_6_3_import_tree import (
            _PY_EXTS,
            _TS_EXTS,
            _is_vendor_or_test,
            _suffix,
        )

        self.unit = unit.strip("/")
        self.repo_root = repo_root
        ts_files = [
            t for t in sorted(tracked)
            if _suffix(t) in _TS_EXTS and not _is_vendor_or_test(t)
        ]
        self.cutoff = hub_cutoff(len(ts_files))
        self.unit_files = frozenset(
            t for t in tracked
            if t == self.unit or t.startswith(self.unit + "/"))

        # Forward resolved-import edges → reverse importer index.
        from faultline.pipeline_v2.ts_ast.adapter import repo_provenance
        prov = None
        try:
            prov = repo_provenance(str(repo_root), tracked)
        except Exception:  # noqa: BLE001 — AST is best-effort here
            prov = None
        alias_map = None
        cache = None
        tracked_set = frozenset(tracked)
        if prov is None:
            from faultline.analyzer.tsconfig_paths import build_path_alias_map
            from faultline.pipeline_v2.stage_6_3_import_tree import (
                _SourceCache,
            )
            from faultline.pipeline_v2.shared_source import (
                shared_source_cache,
            )
            try:
                alias_map = build_path_alias_map(repo_root)
            except Exception:  # noqa: BLE001 — resolver is best-effort
                alias_map = None
            cache = ((ctx is not None
                      and shared_source_cache(ctx, repo_root))
                     or _SourceCache(repo_root))
        importers: dict[str, set[str]] = defaultdict(set)
        imports_into_unit: dict[str, set[str]] = defaultdict(set)
        for rel in ts_files:
            targets: set[str] = set()
            if prov is not None and rel in prov.files:
                targets = set(prov.in_repo_targets(rel))
            elif cache is not None:
                from faultline.pipeline_v2.stage_8_8_shared_members import (
                    _resolve_one,
                )
                try:
                    specs = set(cache.imports(rel).values())
                except Exception:  # noqa: BLE001 — unreadable → no imports
                    specs = set()
                for spec in specs:
                    tgt = _resolve_one(rel, spec, alias_map, tracked_set)
                    if tgt is not None:
                        targets.add(tgt)
            for t in targets:
                if t == rel:
                    continue
                importers[t].add(rel)
                if self._in_unit(t) and not self._in_unit(rel):
                    imports_into_unit[rel].add(t)
        self._importers = {k: frozenset(v) for k, v in importers.items()}
        #: consumer file → unit files it imports directly (type-file /
        #: deep-subpath channel — a pure reverse-import edge).
        self._imports_into_unit = {
            k: frozenset(v) for k, v in imports_into_unit.items()}

        # Export-surface call-path channel.
        self._dotted = _export_surface_index(
            repo_root, self.unit, self.unit_files)
        self._surface_consumers: dict[str, set[str]] = defaultdict(set)
        if self._dotted:
            chain_cache: dict[str, str | None] = {}
            for consumer in sorted(imports_into_unit):
                text = _read_text(repo_root, consumer)
                if not text:
                    continue
                bindings: set[str] = set()
                for m in _IMPORT_BINDING_RE.finditer(text):
                    spec = m.group(4)
                    if not self._spec_into_unit(
                            consumer, spec, prov, tracked_set):
                        continue
                    if m.group(1):
                        bindings.add(m.group(1))
                    if m.group(2):
                        bindings.add(m.group(2))
                    for name in (m.group(3) or "").split(","):
                        name = name.strip().split(" as ")[-1].strip()
                        if name:
                            bindings.add(name)
                for b in sorted(bindings):
                    for cm in re.finditer(
                            rf"\b{re.escape(b)}\.((?:[\w$]+\.)*[\w$]+)",
                            text):
                        chain = cm.group(1)
                        leaf = chain_cache.get(chain)
                        if chain not in chain_cache:
                            leaf = self._longest_dotted(chain)
                            chain_cache[chain] = leaf
                        if leaf:
                            self._surface_consumers[leaf].add(consumer)

    def _in_unit(self, path: str) -> bool:
        return path == self.unit or path.startswith(self.unit + "/")

    def _spec_into_unit(self, src: str, spec: str, prov: Any,
                        tracked: frozenset[str]) -> bool:
        if prov is not None and src in prov.files:
            tgt = prov.resolve(src, spec)
            if tgt is not None:
                return self._in_unit(tgt)
        # Fallback: the workspace-name channel (`@scope/<unitname>/…`).
        base = self.unit.rsplit("/", 1)[-1]
        segs = spec.split("/")
        if spec.startswith("@") and len(segs) >= 2:
            return segs[1] == base
        return segs[0] == base

    def _longest_dotted(self, chain: str) -> str | None:
        segs = chain.split(".")
        for ln in range(len(segs), 0, -1):
            hit = self._dotted.get(".".join(segs[:ln]))
            if hit is not None:
                return hit
        return None

    def importers_of(self, path: str) -> frozenset[str]:
        return self._importers.get(path, frozenset())

    def unit_file_consumers(self, path: str) -> frozenset[str]:
        """Consumers of one candidate-unit file: export-surface call
        sites + direct reverse imports (type channel)."""
        out = set(self._surface_consumers.get(path, ()))
        base = re.sub(r"\.types\.(ts|tsx)$", r".\1", path)
        if base != path:
            out |= self._surface_consumers.get(base, set())
        for consumer, targets in self._imports_into_unit.items():
            if path in targets:
                out.add(consumer)
        return frozenset(out)


# ── Per-UF resolution (the rung ladder) ──────────────────────────────────


@dataclass
class UfResolution:
    uf_id: str
    name: str
    rung: str | None          # "r1-strict"|"r2-consumer"|"r3-plurality"|"route-url"|None
    target: GrainTarget | None
    total_mass: int = 0
    voting_mass: int = 0
    coverage: float = 0.0
    thin_coverage: bool = False
    #: projected lane-aware attach at the target (the I15 mirror) —
    #: filled by the attach-floor pass; None when exempt (mf<2 /
    #: lane-only / synthesized).
    attach: float | None = None
    top2: list[tuple[str, int]] = field(default_factory=list)
    reason: str | None = None  # unresolved reason


def _tie_sorted(votes: Counter) -> list[tuple[Any, int]]:
    """B20's deterministic ``(-count, str(key))`` convention."""
    return sorted(votes.items(), key=lambda kv: (-kv[1], str(kv[0])))


def _grain_key(t: GrainTarget) -> str:
    return f"{t.kind}:{t.key}"


#: tRPC router-directory suffix (documenso shape: ``team-router/``).
_ROUTER_DIR_SUFFIX = "-router"


def _ns_tokens(path: str) -> list[str]:
    """Namespace tokens of a tRPC router seed (B49 r2.6). Two structural
    tRPC file-organisation conventions — both code-grounded, NO
    vocabulary:

      * ``.../routers/<ns...>/file`` — the DIRECTORY chain after the
        last ``routers`` segment (cal.com; create-t3
        ``server/api/routers/``). The filename is a procedure, not a
        namespace, so it is dropped — UNLESS the router is a bare
        ``routers/<file>`` with no namespace directory, in which case
        the filename stem is the namespace.
      * ``.../<name>-router/file`` — the ``<name>`` stem of any
        ``-router``-suffixed directory segment (documenso).

    Transparent grouping tokens (``viewer``/``publicViewer``/``apps``)
    and structural stems (``index``/``_app``) are NOT filtered here —
    they simply fail the exact PF-identity match downstream (there is no
    product PF named ``viewer``), so the mechanism needs no stop-list."""
    segs = [s for s in path.strip("/").split("/") if s]
    if len(segs) < 2:
        return []
    dir_segs = segs[:-1]
    fname = segs[-1]
    stem = fname[: fname.rfind(".")] if "." in fname else fname
    toks: list[str] = []
    if "routers" in dir_segs:
        i = len(dir_segs) - 1 - dir_segs[::-1].index("routers")
        after = dir_segs[i + 1:]
        toks.extend(after or [stem])
    for d in dir_segs:
        if d.endswith(_ROUTER_DIR_SUFFIX) and len(d) > len(_ROUTER_DIR_SUFFIX):
            toks.append(d[: -len(_ROUTER_DIR_SUFFIX)])
    return toks


@dataclass
class NamespaceEcho:
    """B49 r2.6 — maps a tRPC router-namespace token to an EXISTING
    product PF via the shared ``normalize_anchor_key`` (the SAME
    normalization the S3-nav echo uses: ``apiKeys`` and the nav segment
    ``api-keys`` both normalise to ``api-key``, and the ``API Keys`` PF's
    anchor-identity is that same ``api-key``). Discipline:

      * ONLY existing, non-candidate PFs are match targets — never mints.
      * FULL normalized match (dict lookup, never substring); a generic
        token (``utils``/``viewer``) has no product PF so it abstains.
      * A seed whose tokens collectively hit >1 distinct PF is AMBIGUOUS
        → abstain (never guess).
      * ``nav_keys`` corroboration is recorded in telemetry but does NOT
        gate the target: the deterministic nav collector keys on the
        FIRST href segment only (``/settings/developer/api-keys`` →
        ``setting``), so a deep surface like ``api-keys`` need not appear
        in ``nav_keys`` — the authoritative target is the PF
        anchor-identity (side a)."""

    pf_by_key: dict[str, frozenset[str]]
    nav_keys: frozenset[str] = frozenset()
    #: telemetry — seed path → matched pf key (the r2.6 move map).
    matched: dict[str, str] = field(default_factory=dict)
    nav_corroborated: int = 0

    @classmethod
    def build(
        cls,
        product_features: Iterable[Any],
        excluded_pf_keys: frozenset[str],
        nav_keys: frozenset[str] = frozenset(),
    ) -> "NamespaceEcho":
        from faultline.pipeline_v2.mega_pf_nav_rehome import _core_identity
        acc: dict[str, set[str]] = defaultdict(set)
        for pf in product_features:
            key = str(_attr(pf, "id") or _attr(pf, "name") or "")
            if not key or key in excluded_pf_keys:
                continue
            for k in _core_identity(pf):
                if k:
                    acc[k].add(key)
        return cls(
            pf_by_key={k: frozenset(v) for k, v in acc.items()},
            nav_keys=nav_keys,
        )

    def target_for(self, path: str) -> GrainTarget | None:
        from faultline.pipeline_v2.spine_anchors import normalize_anchor_key
        toks = _ns_tokens(path)
        if not toks:
            return None
        hits: set[str] = set()
        nav_hit = False
        for tok in toks:
            k = normalize_anchor_key(tok)
            if not k:
                continue
            pfs = self.pf_by_key.get(k)
            if pfs:
                hits |= set(pfs)
            if k in self.nav_keys:
                nav_hit = True
        if len(hits) != 1:
            return None  # 0 → generic/no-surface; >1 → ambiguous
        pf_key = next(iter(hits))
        self.matched[path] = pf_key
        if nav_hit:
            self.nav_corroborated += 1
        return GrainTarget("pf", pf_key)


class _FileResolver:
    """Ladder over ONE candidate: lane → lane-neutral → owned → route
    grain → consumer seed. Memoised; every rung deterministic."""

    def __init__(
        self,
        unit: str,
        cand_pf_key: str,
        owner_map: Mapping[str, str | None],
        grain: TargetGrainIndex,
        consumers: ConsumerIndex | None,
        lane_pf_keys: frozenset[str] = frozenset(),
        neutral_files: frozenset[str] = frozenset(),
        ns_echo: NamespaceEcho | None = None,
        lifted: frozenset[str] = frozenset(),
    ) -> None:
        self.unit = unit.strip("/")
        self.cand = cand_pf_key
        self.owner = owner_map
        self.grain = grain
        self.consumers = consumers
        self.lane_keys = lane_pf_keys
        #: B49 r2.6 namespace-echo (None when the flag is OFF → the rung
        #: is inert and the ladder is byte-identical to r1→r2→r3).
        self.ns_echo = ns_echo
        #: B51 router-decomp — files physically inside the transport unit
        #: that the decomposition pass already re-homed to a product PF
        #: (carved into a product-owned chunk). They have LEFT the lane:
        #: ``in_lane`` returns False so the r1 owned rung counts them as
        #: product votes (their owner in ``owner_map`` is the product PF).
        #: Empty when the flag is OFF → ``in_lane`` byte-identical.
        self.lifted = lifted
        #: Files owned by a pfid=None LANE-RESIDENT dev — already
        #: adjudicated non-product mass (B21 lane-neutral doctrine):
        #: neutral ground, never a vote, never a seed, excluded from
        #: the r1 denominator exactly like candidate-lane mass.
        self.neutral = neutral_files
        self._direct: dict[str, GrainTarget | None] = {}
        self._seed: dict[str, tuple[GrainTarget | None, str, Counter]] = {}

    def in_lane(self, path: str) -> bool:
        if path in self.lifted:
            return False  # B51 — re-homed by decomp; no longer plumbing
        return path == self.unit or path.startswith(self.unit + "/")

    def is_neutral(self, path: str) -> bool:
        return path in self.neutral

    def direct(self, path: str) -> GrainTarget | None:
        """Owned + route-grain rungs of the ladder (no consumer walk)."""
        if path in self._direct:
            return self._direct[path]
        out: GrainTarget | None = None
        if not self.in_lane(path) and not self.is_neutral(path):
            own = self.owner.get(path)
            if (own is not None and own != self.cand
                    and own not in self.lane_keys
                    and own in self.grain.pf_keys):
                out = GrainTarget("pf", str(own))
            else:
                out = self.grain.grain_of_file(path)
        self._direct[path] = out
        return out

    def seed(self, path: str) -> tuple[GrainTarget | None, str, Counter]:
        """r2 consumer completion for one non-voting seed file —
        winner-take-all strict majority over its consumer votes. The
        raw vote distribution rides along for the r3 pooled-plurality
        rung (a strict-less seed abstains at r2 but its distribution
        still informs the last-resort pool)."""
        if path in self._seed:
            return self._seed[path]
        out: tuple[GrainTarget | None, str, Counter]
        if self.is_neutral(path):
            out = (None, "lane_neutral", Counter())
            self._seed[path] = out
            return out
        if self.consumers is None:
            out = (None, "no_consumer_index", Counter())
            self._seed[path] = out
            return out
        votes: Counter = Counter()
        if self.in_lane(path):
            pool: list[str] = []
            for c in sorted(self.consumers.unit_file_consumers(path)):
                t = self.direct(c)
                if t is not None:
                    votes[_grain_key(t)] += 1
                elif (not self.in_lane(c) and not self.is_neutral(c)
                      and len(self.consumers.importers_of(c))
                      <= self.consumers.cutoff):
                    pool.append(c)
            for c in pool:  # one extra hop for consumer components
                for imp in sorted(self.consumers.importers_of(c)):
                    t = self.direct(imp)
                    if t is not None:
                        votes[_grain_key(t)] += 1
        else:
            if len(self.consumers.importers_of(path)) \
                    > self.consumers.cutoff:
                out = (None, "hub", Counter())
                self._seed[path] = out
                return out
            frontier = [path]
            seen = {path}
            for _ in range(2):
                nxt: list[str] = []
                for p in frontier:
                    for imp in sorted(self.consumers.importers_of(p)):
                        if imp in seen:
                            continue
                        seen.add(imp)
                        t = self.direct(imp)
                        if t is not None:
                            votes[_grain_key(t)] += 1
                        elif (not self.in_lane(imp)
                              and not self.is_neutral(imp)
                              and len(self.consumers.importers_of(imp))
                              <= self.consumers.cutoff):
                            nxt.append(imp)
                frontier = nxt
        total = sum(votes.values())
        if not total:
            out = (None, "no_consumers", votes)
        else:
            ranked = _tie_sorted(votes)
            top_key, ct = ranked[0]
            if ct * 2 > total:
                kind, _, key = str(top_key).partition(":")
                out = (GrainTarget(kind, key), f"{ct}/{total}", votes)
            else:
                out = (None, "split", votes)
        self._seed[path] = out
        return out


def _uf_flow_files(uf: Any, flow_by_uuid: Mapping[str, Any]) -> set[str]:
    """The journey's flow-file surface — the validator's exact I15 view
    (``_spine_flow_files``: union of member flows' ``paths``, falling
    back to the entry file when a flow carries no path list)."""
    out: set[str] = set()
    for fid in (_attr(uf, "member_flow_ids") or []):
        fl = flow_by_uuid.get(fid)
        if fl is None:
            continue
        ps = [str(p) for p in (_attr(fl, "paths") or []) if p]
        if not ps:
            ep = _attr(fl, "entry_point_file")
            ps = [str(ep)] if ep else []
        out.update(ps)
    return out


def _uf_span_mass(uf: Any, flow_by_uuid: Mapping[str, Any]) -> Counter:
    mass: Counter = Counter()
    for fid in (_attr(uf, "member_flow_ids") or []):
        fl = flow_by_uuid.get(fid)
        if fl is None:
            continue
        for lr in (_attr(fl, "line_ranges") or []):
            p = _attr(lr, "path")
            s = _attr(lr, "start_line")
            e = _attr(lr, "end_line")
            if p and isinstance(s, int) and isinstance(e, int) and e >= s:
                mass[str(p)] += e - s + 1
    return mass


def resolve_user_flow(
    uf: Any,
    flow_by_uuid: Mapping[str, Any],
    resolver: _FileResolver,
    routes: "RouteUrlResolver | None",
    plurality_ok: bool,
) -> UfResolution:
    """The rung ladder for ONE homed journey (see module docstring).

    r3 acceptance here is provisional — the caller still runs the
    plurality I16 rail over the planned post-handoff owner map."""
    res = UfResolution(uf_id=str(_attr(uf, "id") or ""),
                       name=str(_attr(uf, "name") or ""),
                       rung=None, target=None)
    mass = _uf_span_mass(uf, flow_by_uuid)
    res.total_mass = sum(mass.values())

    # Synthesized route-recall seeds: route-URL rung ONLY (never span).
    if not res.total_mass and (_attr(uf, "routes") or []):
        votes: Counter = Counter()
        for r in sorted(str(x) for x in (_attr(uf, "routes") or []) if x):
            t = routes.grain_of_route(r) if routes is not None else None
            if t is not None:
                votes[_grain_key(t)] += 1
        total = sum(votes.values())
        if total:
            ranked = _tie_sorted(votes)
            top_key, ct = ranked[0]
            res.top2 = [(str(k), c) for k, c in ranked[:2]]
            if ct * 2 > total:
                kind, _, key = str(top_key).partition(":")
                res.rung, res.target = "route-url", GrainTarget(kind, key)
                res.voting_mass, res.coverage = total, 1.0
                return res
            res.reason = "split"
            return res
        res.reason = "zero_product_votes"
        return res
    if not res.total_mass:
        res.reason = "zero_product_votes"  # no spans, no routes
        return res

    # r1 — strict majority over the journey's NON-LANE span mass: the
    # denominator keeps the still-unresolved seed mass, so a sliver of
    # direct votes can never outvote a large unknown (the Phase-1
    # UF-051 "15-line route sliver" trap / the sim's 1%-coverage docs
    # exhibit). Candidate-lane files and lane-NEUTRAL files (owned by
    # platform-lane residents — already-adjudicated non-product mass)
    # leave the denominator, exactly the validator's B21 convention.
    direct_votes: Counter = Counter()
    seeds: list[tuple[str, int]] = []
    nonlane_mass = 0
    for p in sorted(mass):
        m = mass[p]
        if resolver.in_lane(p) or resolver.is_neutral(p):
            continue
        nonlane_mass += m
        t = resolver.direct(p)
        if t is not None:
            direct_votes[_grain_key(t)] += m
        else:
            seeds.append((p, m))
    # lane files still consumer-complete at r2 (their consumers are the
    # product surface the transport serves) — they are seeds, just
    # excluded from the r1 denominator.
    for p in sorted(mass):
        if resolver.in_lane(p):
            seeds.append((p, mass[p]))
    seeds.sort()
    if direct_votes and nonlane_mass:
        ranked = _tie_sorted(direct_votes)
        top_key, ct = ranked[0]
        if ct * 2 > nonlane_mass:
            kind, _, key = str(top_key).partition(":")
            res.rung, res.target = "r1-strict", GrainTarget(kind, key)
            res.voting_mass = sum(direct_votes.values())
            res.coverage = res.voting_mass / res.total_mass
            res.thin_coverage = res.coverage < _THIN_COVERAGE
            res.top2 = [(str(k), c) for k, c in ranked[:2]]
            return res

    # r2 — consumer completion: each seed re-homes winner-take-all onto
    # its strict consumer majority (abstains never pollute); verdict =
    # strict majority of the completed VOTING mass (the Phase-1 q2c
    # ruler; coverage telemetry keeps thin verdicts visible).
    pooled = Counter(direct_votes)
    seed_dists: list[tuple[int, Counter]] = []
    abstain_seeds: list[tuple[str, int]] = []
    for p, m in seeds:
        t, _why, dist = resolver.seed(p)
        if t is not None:
            pooled[_grain_key(t)] += m
        else:
            if dist:
                seed_dists.append((m, dist))
            if resolver.in_lane(p):
                abstain_seeds.append((p, m))
    voting = sum(pooled.values())
    res.voting_mass = voting
    res.coverage = (voting / res.total_mass) if res.total_mass else 0.0
    res.thin_coverage = res.coverage < _THIN_COVERAGE
    if voting:
        ranked = _tie_sorted(pooled)
        res.top2 = [(str(k), c) for k, c in ranked[:2]]
        top_key, ct = ranked[0]
        if ct * 2 > voting:
            kind, _, key = str(top_key).partition(":")
            res.rung, res.target = "r2-consumer", GrainTarget(kind, key)
            return res

    # r2.6 — namespace echo (B49, flag-gated via ``resolver.ns_echo``):
    # the IN-LANE tRPC router seeds that abstained at r2 (typed-proxy
    # consumption leaves them with no product consumers) vote their span
    # mass for the EXISTING product PF their namespace token echoes.
    # Runs on a COPY of the pool: it either RESOLVES via a strict
    # majority (a clean new rung) or leaves the ladder untouched for r3
    # — it never guesses and never perturbs the plurality pool. Order is
    # preserved: r1 and r2 already had their say above; when the flag is
    # OFF (``ns_echo is None``) the whole block is inert.
    if resolver.ns_echo is not None and abstain_seeds:
        aug = Counter(pooled)
        ns_mass = 0
        for p, m in abstain_seeds:
            t = resolver.ns_echo.target_for(p)
            if t is not None:
                aug[_grain_key(t)] += m
                ns_mass += m
        if ns_mass:
            voting_aug = sum(aug.values())
            ranked = _tie_sorted(aug)
            top_key, ct = ranked[0]
            if ct * 2 > voting_aug:
                kind, _, key = str(top_key).partition(":")
                res.rung, res.target = (
                    "r2.6-namespace", GrainTarget(kind, key))
                res.voting_mass = voting_aug
                res.coverage = (voting_aug / res.total_mass) \
                    if res.total_mass else 0.0
                res.thin_coverage = res.coverage < _THIN_COVERAGE
                res.top2 = [(str(k), c) for k, c in ranked[:2]]
                return res

    if not voting and not seed_dists:
        res.reason = "zero_product_votes"
        return res

    # r3 — pooled-plurality last resort: abstaining seeds contribute
    # their FULL consumer-vote distributions (mass-weighted, exact
    # rational arithmetic — no float ties), and the top target must
    # STRICTLY beat the runner-up (an exactly-50/50 journey never
    # re-homes — design §8.2). The caller still runs the I16 rail.
    if plurality_ok:
        from fractions import Fraction
        pooled_r3: dict[str, Fraction] = defaultdict(lambda: Fraction(0))
        for k, c in pooled.items():
            pooled_r3[str(k)] += Fraction(c)
        for m, dist in seed_dists:
            tot = sum(dist.values())
            for k, c in dist.items():
                pooled_r3[str(k)] += Fraction(m) * Fraction(c, tot)
        ranked3 = sorted(pooled_r3.items(), key=lambda kv: (-kv[1], kv[0]))
        res.top2 = [(k, int(v)) for k, v in ranked3[:2]]
        if ranked3 and (len(ranked3) < 2 or ranked3[0][1] > ranked3[1][1]):
            kind, _, key = ranked3[0][0].partition(":")
            res.rung, res.target = "r3-plurality", GrainTarget(kind, key)
            return res
    res.reason = "split"
    return res


# ── The stage ────────────────────────────────────────────────────────────


def _owned_of(f: Any) -> list[str]:
    from faultline.pipeline_v2.spine_anchors import owned_paths_of
    return owned_paths_of(f)


def _build_owner_map(
    devs: list[Any],
) -> tuple[dict[str, str | None], frozenset[str]]:
    """``(file → owning dev's product_feature_id, lane-neutral files)``.

    LIVE state — this stage runs before the emission path_index
    refresh, so the 6.8 index is stale for post-6.8 dev moves; the dev
    ledger is the truth. First claimant in name-sorted dev order wins
    (deterministic). A file whose owner is a ``pfid=None`` dev (a
    platform-lane resident: shells, instruments, no-anchor residue) is
    LANE-NEUTRAL — already-adjudicated non-product mass that must
    neither vote nor become a NEW-target grain source (B21)."""
    owner: dict[str, str | None] = {}
    for f in sorted(devs, key=lambda x: str(_attr(x, "name") or "")):
        pfid = _attr(f, "product_feature_id")
        for p in _owned_of(f):
            owner.setdefault(p, pfid)
    neutral = frozenset(p for p, o in owner.items() if o is None)
    return owner, neutral


def _carve_chunk(src: Any, cid: str, files: list[str],
                 marker: str = _HANDOFF_MARKER) -> Any:
    """8.9.x-style chunk dev for a carved group grain (mirrors
    ``lane_excavation._make_excav_dev`` with the handoff marker;
    content-derived uuid — uuid4 would churn byte-identity). ``marker``
    stamps the provenance channel (default: this stage's; Stage 6.986
    passes its own — byte-identical for existing callers)."""
    import hashlib

    name = f"{str(_attr(src, 'name'))}-{marker}"
    uuid = hashlib.sha256(
        f"transport-carve-v1|{_attr(src, 'uuid') or _attr(src, 'name')}|"
        f"{cid}|{name}".encode("utf-8")).hexdigest()[:32]
    if hasattr(src, "model_copy"):
        from faultline.models.types import MemberFile
        members = [
            MemberFile(
                path=p, role="anchor", confidence=1.0, primary=True,
                evidence=f"{marker} carve of '{src.name}'",
            )
            for p in sorted(files)
        ]
        return src.model_copy(deep=True, update={
            "name": name,
            "display_name": name,
            "paths": sorted(files),
            "member_files": members,
            "description": (
                f"{marker} carve '{cid}' of '{src.name}'"),
            "uuid": uuid,
            "split_from": getattr(src, "uuid", None),
            "previous_names": [], "merged_from": [],
            "total_commits": 0, "bug_fixes": 0, "bug_fix_ratio": 0.0,
            "flows": [], "shared_participants": [],
            "shared_attributions": [], "symbol_attributions": [],
            "hotspot_files": [], "participants": [],
            "history": None, "shared_reason": None,
        })

    from types import SimpleNamespace  # test/sim stubs (no pydantic)

    ch: Any = SimpleNamespace()
    ch.layer = "developer"
    ch.name = name
    ch.display_name = name
    ch.uuid = uuid
    ch.paths = sorted(files)
    ch.member_files = [
        {"path": p, "role": "anchor", "confidence": 1.0, "primary": True,
         "evidence": f"{marker} carve of '{_attr(src, 'name')}'"}
        for p in sorted(files)
    ]
    ch.flows = []
    propose_pf_now(ch, None, rung="transport")
    ch.shared_reason = None
    ch.anchor_id = None
    for k in ("authors", "total_commits", "bug_fixes", "coverage_pct",
              "last_modified", "health_score"):
        setattr(ch, k, _attr(src, k))
    ch.authors = list(_attr(src, "authors") or [])
    ch.total_commits = 0
    ch.bug_fixes = 0
    return ch


def _move_carved_flows(
    src: Any, chunk: Any, files: set[str],
    edges_by_flow_id: Mapping[str, list[Any]],
) -> int:
    """Move the source dev's flows whose ENTRY file was carved; restamp
    the bipartite identity fields + edges (lane_excavation's
    ``_move_flows`` contract, prefix-free: the carve set is explicit)."""
    moved = 0
    keep: list[Any] = []
    for fl in (_attr(src, "flows") or []):
        ep = str(_attr(fl, "entry_point_file") or "")
        if not ep or ep not in files:
            keep.append(fl)
            continue
        old_id = _attr(fl, "id")
        chunk.flows.append(fl)
        fl.primary_feature = _attr(chunk, "name")
        new_id = f"{_attr(chunk, 'name')}::{_attr(fl, 'name')}"
        fl.id = new_id
        for e in edges_by_flow_id.get(str(old_id or ""), []):
            if _attr(e, "type") == "primary":
                e.feature = _attr(chunk, "name")
            e.flow_id = new_id
        moved += 1
    src.flows = keep
    return moved


def _strip_carved_files(src: Any, files: set[str]) -> None:
    """Drop carved files from the source's ``paths``/``member_files``
    (mirrors ``lane_excavation._remove_files_from_shell``)."""
    src.paths = [p for p in (_attr(src, "paths") or []) if p not in files]
    kept = []
    for m in (_attr(src, "member_files") or []):
        p = m.get("path") if isinstance(m, dict) else getattr(m, "path", None)
        if p not in files:
            kept.append(m)
    src.member_files = kept


def _i16_flagged(
    uf: Any,
    home_key: str,
    flow_by_uuid: Mapping[str, Any],
    owner: Mapping[str, str | None],
    lane_files: "_FileResolver",
) -> bool:
    """The validator's I16 entry-owner ruler over one owner-map view:
    majority-foreign journey vs *home_key* (> 0.5 — an exactly-half
    split does not fire). Lane-neutral (B21): candidate-lane /
    lane-resident / unowned entries never count. Used twice by the
    plurality rail — pre-state (current map, dissolving home) vs
    post-state (planned map, plurality target): only a clean→flagged
    transition is a NEW row."""
    chk = mis = 0
    for fid in (_attr(uf, "member_flow_ids") or []):
        fl = flow_by_uuid.get(fid)
        ep = _attr(fl, "entry_point_file") if fl is not None else None
        if not ep:
            continue
        if lane_files.in_lane(str(ep)):
            continue
        own = owner.get(str(ep))
        if own is None:
            continue
        chk += 1
        if str(own) != home_key:
            mis += 1
    return bool(chk) and mis * 2 > chk


#: tRPC router-tree transparent grouping segments (author-structural, not
#: product surfaces): ``routers/viewer/<domain>`` nests every namespace
#: under a grouping router. They carry no product identity, so the
#: sub-router discriminator is the FIRST non-transparent token of the
#: common hole. NOT a stop-list gate (a transparent-only common hole simply
#: yields no discriminator → residue).
_ROUTER_TRANSPARENT = frozenset({
    "viewer", "publicviewer", "loggedinviewer", "apps",
})


def _flow_router_files(fl: Any) -> list[str]:
    """The flow's distinct line-range file surface (falling back to the
    entry file when a flow carries no line_ranges)."""
    out: set[str] = set()
    for lr in (_attr(fl, "line_ranges") or []):
        p = _attr(lr, "path")
        if p:
            out.add(str(p))
    if not out:
        ep = _attr(fl, "entry_point_file")
        if ep:
            out.add(str(ep))
    return sorted(out)


def _handler_ns_tokens(path: str) -> list[str]:
    """B52 (c)-grain — the transport-handler DOMAIN of an
    ``…/api/trpc/<domain>/…`` file (cal.com:
    ``apps/web/pages/api/trpc/apiKeys/[trpc].ts`` → ``apiKeys``).
    Code-grounded, no vocabulary: the directory segment immediately
    after a ``trpc`` directory that itself follows an ``api`` directory.
    The filename (``[trpc].ts`` catch-all) is a procedure surface, never
    a namespace. A path without the ``api/trpc/<domain>/`` shape yields
    ``[]`` — exactly like ``_ns_tokens`` outside ``routers/`` trees."""
    segs = [s for s in path.strip("/").split("/") if s]
    if len(segs) < 2:
        return []
    dir_segs = segs[:-1]
    toks: list[str] = []
    for i, d in enumerate(dir_segs):
        if (d == "trpc" and i > 0 and dir_segs[i - 1] == "api"
                and i + 1 < len(dir_segs)):
            toks.append(dir_segs[i + 1])
    return toks


def _flow_subrouter_disc(fl: Any, handler_grain: bool = False) -> str | None:
    """B51 grain — the sub-router discriminator of a flow whose span sits
    under a tRPC ``routers/`` sub-tree: the deepest hole COMMON to ALL its
    routers-tree line-range files (``_ns_tokens`` chains), reduced to the
    first non-transparent segment. Returns ``None`` (RESIDUE) when the flow
    has NO routers-tree file (middleware / client / ``api/trpc/*/[trpc].ts``
    handler → residue immediately) or its routers files span more than one
    sub-router (no single common discriminating hole).

    B52 (c)-grain (``handler_grain=True``, the drain-then-lane mode only):
    a flow with NO routers-tree file MAY still discriminate by its
    ``api/trpc/<domain>/`` handler domain (``_handler_ns_tokens``) — the
    B51 spec-residue class the ledger measured at 47/66 flows. Routers
    files, when present, keep priority (unchanged law). Default OFF →
    byte-identical to the B51 grain."""
    chains: list[list[str]] = []
    for p in _flow_router_files(fl):
        toks = _ns_tokens(p)
        if toks:
            chains.append(toks)
    if not chains and handler_grain:
        for p in _flow_router_files(fl):
            toks = _handler_ns_tokens(p)
            if toks:
                chains.append(toks)
    if not chains:
        return None  # no routers-tree file → residue
    common = list(chains[0])
    for ch in chains[1:]:
        j = 0
        while j < len(common) and j < len(ch) and common[j] == ch[j]:
            j += 1
        common = common[:j]
    for tok in common:
        if tok.lower() not in _ROUTER_TRANSPARENT:
            return tok  # deepest common discriminating hole
    return None  # transparent-only / empty common hole → residue


def _move_group_flows(
    src: Any, chunk: Any, group_flows: list[Any],
    edges_by_flow_id: Mapping[str, list[Any]],
) -> int:
    """Move an EXPLICIT flow set (by object identity, not entry file — the
    B51 decomp groups by routers-hole, whose flows enter via the residual
    ``[trpc].ts`` handler) from ``src`` to ``chunk``, re-stamping the
    bipartite identity fields + edges (the ``_move_carved_flows`` contract,
    lineage preserved: same uuid, no mint → flow-dup LAW untouched)."""
    ids = {id(f) for f in group_flows}
    keep: list[Any] = []
    moved = 0
    for fl in (_attr(src, "flows") or []):
        if id(fl) not in ids:
            keep.append(fl)
            continue
        old_id = _attr(fl, "id")
        chunk.flows.append(fl)
        fl.primary_feature = _attr(chunk, "name")
        new_id = f"{_attr(chunk, 'name')}::{_attr(fl, 'name')}"
        fl.id = new_id
        for e in edges_by_flow_id.get(str(old_id or ""), []):
            if _attr(e, "type") == "primary":
                e.feature = _attr(chunk, "name")
            e.flow_id = new_id
        moved += 1
    src.flows = keep
    return moved


def _router_decomp(
    unit: str,
    cand_key: str,
    cand_devs: list[Any],
    echo: NamespaceEcho,
    edges_by_flow_id: Mapping[str, list[Any]],
    developer_features: list[Any],
    drain_then_lane: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """B51 — decompose a FLOW-BEARING transport candidate per sub-router.

    Each sub-router group whose namespace token echoes an EXISTING product
    PF (``NamespaceEcho`` — the r2.6 matcher, FULL normalized match, ambig
    →residue, generic→residue, NEVER mints) has its flows + routers-tree
    files carved into a product-owned chunk dev re-homed to that PF (I22
    marker), and the carved files are LIFTED out of the lane so r1 drains
    their journeys.

    ALL-OR-NOTHING (B51 rework, 2026-07-13): the carve is applied ONLY when
    the candidate FULLY drains — every flow re-homes, ``residue_flows == 0``,
    the residual dev goes flowless and lanes cleanly downstream. A PARTIAL
    re-home (matched flows leaving while a flowful residue holds the tile)
    was refuted by the keyed gate: it orphans a flowful product PF that no
    journey references (validator I8 — cal.com ``credits``) and breaks the
    documenso keyed byte-identity SACRED anti-case (keyed documenso's trpc
    is flow-bearing). So when ``residue_flows > 0`` the pass ABSTAINS — no
    mutation, no telemetry, output byte-identical (the honest Option-B
    limit: a real tRPC monolith carries ``api/trpc/*/[trpc].ts`` handler
    flows that no sub-router grain can re-home, so it never fully drains).

    DRAIN-THEN-LANE (B52, ``drain_then_lane=True``): the all-or-nothing
    gate opens — matched groups carve even when ``residue_flows > 0``
    (the residue lanes downstream via the Seg1 ws-anchor carve-out), and
    the (c) handler grain is legal. The caller owns the I8 receiver
    backstop (``_pullback_chunk``) — hence the second return value: one
    commit record per carve with everything needed for an exact undo.

    Returns ``(telemetry, commits)`` — the caller mutates nothing and
    emits nothing when ``applied is False`` (``commits == []``)."""
    tele: dict[str, Any] = {
        "unit": unit, "pf": cand_key, "groups": 0,
        "matched": {}, "unmatched": [], "residue_flows": 0,
        "flows_moved": 0, "files_moved": 0, "applied": False, "lifted": [],
    }
    # ── PLAN (no mutation) ──────────────────────────────────────────────
    #: (src, pf_key, [disc], [flow], [carve file]) per matched group.
    plan: list[tuple[Any, str, list[str], list[Any], list[str]]] = []
    total_residue = 0
    ngroups = 0
    for src in sorted(cand_devs, key=lambda x: str(_attr(x, "name"))):
        if not (_attr(src, "flows") or []):
            continue
        owned = set(_owned_of(src))
        groups: dict[str, list[Any]] = defaultdict(list)
        residue = 0
        for fl in list(_attr(src, "flows") or []):
            disc = _flow_subrouter_disc(fl, handler_grain=drain_then_lane)
            if disc is None:
                residue += 1
            else:
                groups[disc].append(fl)
        ngroups += len(groups)
        matched: dict[str, str] = {}  # disc → target pf key
        for disc in sorted(groups):
            t = echo.target_for(f"routers/{disc}/_decomp.ts")
            if t is not None and t.kind == "pf" and t.key != cand_key:
                matched[disc] = t.key
            else:
                tele["unmatched"].append(
                    {"disc": disc, "flows": len(groups[disc])})
                residue += len(groups[disc])  # unmatched → residue
        by_pf: dict[str, list[str]] = defaultdict(list)
        flows_by_pf: dict[str, list[Any]] = defaultdict(list)
        for disc in sorted(matched):
            by_pf[matched[disc]].append(disc)
            flows_by_pf[matched[disc]].extend(groups[disc])
        for pf_key in sorted(by_pf):
            gflows = flows_by_pf[pf_key]
            cfiles = sorted(
                p for fl in gflows for p in _flow_router_files(fl)
                if p in owned and (
                    _ns_tokens(p)
                    or (drain_then_lane and _handler_ns_tokens(p))))
            if not cfiles:
                residue += len(gflows)  # matched but nothing to carve
                continue
            plan.append((src, pf_key, by_pf[pf_key], gflows, cfiles))
            tele["matched"].setdefault(pf_key, []).extend(by_pf[pf_key])
        total_residue += residue
    tele["groups"] = ngroups
    tele["residue_flows"] = total_residue
    tele["matched"] = {k: sorted(set(v)) for k, v in tele["matched"].items()}

    # ── COMMIT GATE — full drain only (residue == 0), UNLESS the B52
    # drain-then-lane mode holds (the residue lanes downstream) ─────────
    if not plan or (total_residue != 0 and not drain_then_lane):
        # Abstain: the candidate keeps its (whole) tile; caller emits
        # nothing → byte-identical. Honest Option-B limit.
        tele["matched"] = {}  # nothing actually moved
        return tele, []
    lifted: set[str] = set()
    commits: list[dict[str, Any]] = []
    for src, pf_key, discs, gflows, cfiles in plan:
        # Exact-undo snapshot (B52 receiver backstop): the flows' current
        # bipartite identity + the member_files rows the strip removes.
        old_ids = {
            id(fl): (_attr(fl, "id"), _attr(fl, "primary_feature"))
            for fl in gflows
        }
        cset = set(cfiles)
        removed_members = []
        for m in (_attr(src, "member_files") or []):
            p = m.get("path") if isinstance(m, dict) \
                else getattr(m, "path", None)
            if p in cset:
                removed_members.append(m)
        chunk = _carve_chunk(src, f"pf:{pf_key}", cfiles,
                             marker=_ROUTER_DECOMP_MARKER)
        name = f"{_attr(src, 'name')}-{_ROUTER_DECOMP_MARKER}-{pf_key}"
        chunk.name = name
        chunk.display_name = name
        _move_group_flows(src, chunk, gflows, edges_by_flow_id)
        _strip_carved_files(src, cset)
        propose_pf_now(chunk, pf_key, rung="transport")
        chunk.anchor_id = f"fold:{_ROUTER_DECOMP_MARKER}->pf:{pf_key}"
        chunk.shared_reason = None
        developer_features.append(chunk)
        lifted.update(cfiles)
        tele["flows_moved"] += len(gflows)
        tele["files_moved"] += len(cfiles)
        commits.append({
            "pf_key": pf_key, "chunk": chunk, "src": src,
            "discs": list(discs), "gflows": list(gflows),
            "cfiles": list(cfiles), "old_ids": old_ids,
            "removed_members": removed_members,
        })
    tele["applied"] = True
    tele["lifted"] = sorted(lifted)
    return tele, commits


def _pullback_chunk(
    commit: Mapping[str, Any],
    developer_features: list[Any],
    edges_by_flow_id: Mapping[str, list[Any]],
) -> None:
    """B52 receiver I8 backstop — exact undo of ONE decomp carve (the
    receiver PF would end flowful-but-journey-less; the B51 keyed gate
    proved that class ships validator I8 rows): flows return to the
    source dev with their ORIGINAL bipartite identity (recorded ids —
    never re-derived), edges re-point, carved files rejoin the source's
    ``paths``/``member_files``, and the chunk row leaves
    ``developer_features``. Deterministic; the group becomes residue and
    rides the lane."""
    chunk, src = commit["chunk"], commit["src"]
    old_ids: Mapping[int, tuple[Any, Any]] = commit["old_ids"]
    for fl in list(_attr(chunk, "flows") or []):
        old_id, old_primary = old_ids[id(fl)]
        cur_id = _attr(fl, "id")
        for e in edges_by_flow_id.get(str(cur_id or ""), []):
            if _attr(e, "type") == "primary":
                e.feature = old_primary
            e.flow_id = old_id
        fl.id = old_id
        fl.primary_feature = old_primary
        src.flows.append(fl)
    chunk.flows = []
    cset = set(commit["cfiles"])
    src.paths = sorted(set(_attr(src, "paths") or []) | cset)
    src.member_files = list(_attr(src, "member_files") or []) + list(
        commit["removed_members"])
    developer_features[:] = [
        f for f in developer_features if f is not chunk
    ]


def run_transport_handoff(
    developer_features: list[Any],
    product_features: list[Any],
    user_flows: list[Any],
    flows: list[Any],
    routes_index: list[dict[str, Any]] | None,
    ctx: Any,
    transport_candidates: Mapping[str, str],
    extractor_signals: dict[str, list[Any]] | None = None,
    instrument_dirs: Iterable[str] = (),
    feature_flow_edges: list[Any] | None = None,
    grain_index: TargetGrainIndex | None = None,
    consumer_index_factory: Any = None,
    nav_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Stage 6.985 entrypoint — see module docstring.

    Mutates ``user_flows`` / ``developer_features`` /
    ``product_features`` in place ONLY per a verified plan; returns
    telemetry for ``scan_meta.transport_handoff``. ``grain_index`` /
    ``consumer_index_factory`` are injection points for tests and the
    offline simulator (the default builds both from the live scan)."""
    tele: dict[str, Any] = {
        "enabled": True,
        "candidates": sorted(transport_candidates or {}),
        "plurality_enabled": transport_plurality_enabled(),
        "laned": [], "conservation_blocked": {},
        "ufs_rehomed": 0, "devs_rehomed": 0, "devs_laned": 0,
        "pfs_minted": 0, "rungs": {}, "moves": [],
    }
    if not transport_candidates:
        return tele

    uf_count_before = len(user_flows)
    uf_home_before: Counter = Counter(
        str(_attr(u, "product_feature_id"))
        for u in user_flows if _attr(u, "product_feature_id"))

    devs = [f for f in developer_features
            if _attr(f, "layer", "developer") == "developer"
            and _attr(f, "name")]
    flow_by_uuid: dict[str, Any] = {}
    for fl in flows or []:
        u = _attr(fl, "uuid")
        if u:
            flow_by_uuid[str(u)] = fl
    # Flows may also ride on the devs (test scenes / degraded inputs).
    for f in devs:
        for fl in (_attr(f, "flows") or []):
            u = _attr(fl, "uuid")
            if u and str(u) not in flow_by_uuid:
                flow_by_uuid[str(u)] = fl

    owner_map, neutral_files = _build_owner_map(devs)

    # Candidate unit → its minted PF (anchor identity: ``ws:<unit>``).
    # B58-v3 Seg A (FAULTLINE_GRAIN_WAVE, default OFF): a grain-wave
    # fdir candidate's PF is ``fdir:``-anchored (cal.com data-table =
    # ``fdir:apps/web/modules/data-table``) — resolve that shape too,
    # gated so the OFF world never widens the ws: contract (and OFF
    # produces no fdir candidates in the first place — belt and braces).
    from faultline.pipeline_v2.schema_member_strip import (
        grain_wave_enabled as _grain_wave_enabled,
    )
    _accept_fdir = _grain_wave_enabled()
    pf_by_key = { (str(_attr(pf, "id") or _attr(pf, "name"))): pf
                  for pf in product_features
                  if (_attr(pf, "id") or _attr(pf, "name")) }
    cand_pf: dict[str, str] = {}
    for unit in sorted(transport_candidates):
        wanted = [f"ws:{unit.strip('/')}"]
        if _accept_fdir:
            wanted.append(f"fdir:{unit.strip('/')}")
        for pf in product_features:
            if str(_attr(pf, "anchor_id") or "") in wanted:
                key = str(_attr(pf, "id") or _attr(pf, "name") or "")
                if key:
                    cand_pf[unit] = key
                break

    # B58-v3 iter-2 (FAULTLINE_GRAIN_WAVE) — twin-unit resolution
    # (census MODE 2: candidate present, row standing). A ws-package
    # capability often has an APP-SIDE twin feature-dir carrying the
    # PF-of-record (typebot: candidate ``packages/variables`` laned
    # while the gap row sat on the PF anchored
    # ``fdir:apps/builder/src/features/variables``) — the exact-dir
    # resolution above can never see it, so the candidate silently
    # skips and the row survives. Fallback, guarded: only when the
    # candidate has NO own-anchored PF; twin anchor must be ``fdir:``
    # (a ``route:`` twin is a REAL product surface — never a lane
    # target; a ``ws:`` twin is a different package that merely shares
    # a name — ambiguous); the twin dir must carry no route file (the
    # website-with-routes analog); the basename identity uses the SAME
    # ``normalize_anchor_key`` as every echo matcher; exactly ONE twin,
    # never a PF another candidate already resolved. Downstream is
    # PF-scoped by construction (cand_devs/journeys key on the PF), so
    # the twin PF's devs and journeys ride the existing all-or-nothing
    # conservation machinery unchanged.
    if _accept_fdir:
        from faultline.pipeline_v2.spine_anchors import (
            normalize_anchor_key as _nak,
        )
        _route_files = {
            str(e.get("file") or "") for e in (routes_index or [])
            if isinstance(e, Mapping) and e.get("file")
        }
        _taken = set(cand_pf.values())
        _twin_tele: dict[str, str] = {}
        for unit in sorted(transport_candidates):
            if unit in cand_pf:
                continue
            base = _nak(unit.strip("/").rsplit("/", 1)[-1])
            if not base:
                continue
            twins: list[tuple[str, str]] = []
            for pf in product_features:
                aid = str(_attr(pf, "anchor_id") or "")
                if not aid.startswith("fdir:"):
                    continue
                d = aid[len("fdir:"):].strip("/")
                if not d or _nak(d.rsplit("/", 1)[-1]) != base:
                    continue
                key = str(_attr(pf, "id") or _attr(pf, "name") or "")
                if key and key not in _taken:
                    twins.append((d, key))
            if len(twins) != 1:
                continue  # ambiguous / none — honest abstain
            twin_dir, twin_key = twins[0]
            if any(rf == twin_dir or rf.startswith(twin_dir + "/")
                   for rf in _route_files):
                continue  # route-bearing twin = real surface
            cand_pf[unit] = twin_key
            _taken.add(twin_key)
            _twin_tele[unit] = f"fdir-twin:{twin_dir}"
        if _twin_tele:
            tele["twin_resolutions"] = dict(sorted(_twin_tele.items()))

    tele["candidate_pfs"] = dict(sorted(cand_pf.items()))
    if not cand_pf:
        return tele

    # B49 r2.6 — the namespace-echo oracle (flag-gated, default OFF):
    # built once from the EXISTING non-candidate PFs' anchor-identities,
    # shared across candidates. None → the rung is inert (byte-identical
    # to the r1→r2→r3 ladder).
    echo: NamespaceEcho | None = None
    if transport_namespace_echo_enabled():
        echo = NamespaceEcho.build(
            product_features,
            excluded_pf_keys=frozenset(cand_pf.values()),
            nav_keys=nav_keys,
        )

    # B51 — the router-decomposition matcher (flag-gated, default OFF):
    # built once from the EXISTING non-candidate PFs' anchor-identities
    # (SAME oracle as the r2.6 echo). None → decomp is skipped and the
    # stage is byte-identical. B52 — the flowful-transport-lane switch
    # ALSO drives the pass (drain-then-lane mode; the one cycle switch).
    flowful_lane = flowful_transport_lane_enabled()
    decomp_echo: NamespaceEcho | None = None
    if transport_router_decomp_enabled() or flowful_lane:
        decomp_echo = NamespaceEcho.build(
            product_features,
            excluded_pf_keys=frozenset(cand_pf.values()),
            nav_keys=nav_keys,
        )

    # THE grain oracle (condition 4) — built once, shared by vote+mint.
    if grain_index is None:
        from faultline.pipeline_v2.spine_anchors import build_spine_anchors
        anchors = build_spine_anchors(
            devs, routes_index, ctx, extractor_signals, frozenset())
        grain_index = TargetGrainIndex(
            anchors, product_features,
            routes_index=routes_index,
            excluded_units=set(transport_candidates)
            | {str(d) for d in instrument_dirs},
            candidate_pf_keys=set(cand_pf.values()),
        )
    routes_resolver = RouteUrlResolver(routes_index, grain_index)

    plurality_ok = transport_plurality_enabled()
    strict = _strict_conservation()

    for unit in sorted(cand_pf):
        cand_key = cand_pf[unit]
        homed = [u for u in user_flows
                 if str(_attr(u, "product_feature_id") or "") == cand_key]
        cand_devs = [f for f in devs
                     if str(_attr(f, "product_feature_id") or "") == cand_key]

        # ── B51 router-mega decomposition (flag-gated; FLOW-BEARING only) ──
        # Carves each matched sub-router group into a product-owned chunk
        # BEFORE the conservation gate. Runs on the candidate's flow-bearing
        # devs; refreshes the owner map so the resolver sees the re-homes,
        # and LIFTS the carved files out of the lane (r1 drains their
        # journeys). Flowless candidates (documenso trpc) are a no-op →
        # byte-identical. Matched flows leave regardless of whether the
        # candidate ultimately lanes: residue (unmatched / handler flows)
        # keeps a reduced tile downstream — the honest Option-B abstain.
        decomp_lifted: frozenset[str] = frozenset()
        d_commits: list[dict[str, Any]] = []
        d_tele: dict[str, Any] = {}
        if decomp_echo is not None:
            flow_bearing = [f for f in cand_devs if (_attr(f, "flows") or [])]
            if flow_bearing:
                edges_d: dict[str, list[Any]] = defaultdict(list)
                for e in (feature_flow_edges or []):
                    edges_d[_attr(e, "flow_id")].append(e)
                d_tele, d_commits = _router_decomp(
                    unit, cand_key, flow_bearing, decomp_echo,
                    edges_d, developer_features,
                    drain_then_lane=flowful_lane)
                # Emitted ONLY when the pass actually applied (full drain).
                # A flowless candidate (documenso keyless trpc) OR an
                # abstain (flowful residue — cal.com, documenso keyed) adds
                # NO telemetry and mutates nothing → output byte-identical
                # to OFF (the documenso SACRED anti-case holds in BOTH
                # channels; the honest Option-B limit stays inert).
                if d_tele.get("applied"):
                    tele.setdefault("router_decomp", {})[unit] = d_tele
                decomp_lifted = frozenset(d_tele.get("lifted") or [])
                if decomp_lifted:
                    # Refresh the dev set + owner map to see the carved
                    # product-owned chunks; re-read the residual cand devs.
                    devs = [f for f in developer_features
                            if _attr(f, "layer", "developer") == "developer"
                            and _attr(f, "name")]
                    owner_map, neutral_files = _build_owner_map(devs)
                    cand_devs = [
                        f for f in devs
                        if str(_attr(f, "product_feature_id") or "")
                        == cand_key]
                    for f in devs:
                        for fl in (_attr(f, "flows") or []):
                            u = _attr(fl, "uuid")
                            if u and str(u) not in flow_by_uuid:
                                flow_by_uuid[str(u)] = fl

        consumers: ConsumerIndex | None = None
        if consumer_index_factory is not None:
            consumers = consumer_index_factory(unit)
        else:
            try:
                consumers = ConsumerIndex(
                    Path(_attr(ctx, "repo_path", ".")),
                    [str(p) for p in (_attr(ctx, "tracked_files") or [])],
                    unit, ctx=ctx)
            except Exception:  # noqa: BLE001 — r2 degrades to abstain
                consumers = None
        # Every candidate PF key is lane-classed for THIS vote too — a
        # file owned by a SIBLING dissolving transport never becomes a
        # target (B20 law: lane/None are never re-home targets).
        resolver = _FileResolver(
            unit, cand_key, owner_map, grain_index, consumers,
            lane_pf_keys=frozenset(cand_pf.values()),
            neutral_files=neutral_files, ns_echo=echo,
            lifted=decomp_lifted)

        # ── plan: UF votes ────────────────────────────────────────────
        resolutions = [
            resolve_user_flow(u, flow_by_uuid, resolver, routes_resolver,
                              plurality_ok)
            for u in sorted(homed, key=lambda x: str(_attr(x, "id") or ""))
        ]
        res_by_id = {r.uf_id: r for r in resolutions}

        # ── plan: dev re-homes (same ladder, same grain oracle) ───────
        dev_plan: dict[str, GrainTarget | None] = {}  # dev name → target
        for f in sorted(cand_devs, key=lambda x: str(_attr(x, "name"))):
            owned = _owned_of(f)
            in_unit = [p for p in owned if resolver.in_lane(p)]
            if not owned or len(in_unit) * 2 > len(owned):
                dev_plan[str(_attr(f, "name"))] = None  # true router dev
                continue
            votes: Counter = Counter()
            for p in sorted(owned):
                if resolver.in_lane(p):
                    continue
                t = resolver.direct(p)
                if t is None:
                    t, _why, _dist = resolver.seed(p)
                if t is not None:
                    votes[_grain_key(t)] += 1
            total = sum(votes.values())
            target: GrainTarget | None = None
            if total:
                ranked = _tie_sorted(votes)
                top_key, ct = ranked[0]
                if ct * 2 > total:
                    kind, _, key = str(top_key).partition(":")
                    target = GrainTarget(kind, key)
            dev_plan[str(_attr(f, "name"))] = target  # None → lane residual

        # ── plan: INITIAL NEW-target demand + carve preview ────────────
        uf_new_demand = {
            r.target.key for r in resolutions
            if r.target is not None and r.target.kind == "new"}
        dev_targets_new = {
            t.key for t in dev_plan.values()
            if t is not None and t.kind == "new"}
        # A demanded NEW target no WHOLE dev re-homes to can still mint
        # from a CARVE (lane_excavation's 8.9.x discipline): candidate
        # devs' own files inside the group grain become a chunk dev
        # (the documenso ``share`` dev straddling t.$teamUrl+ and
        # _share+). Preview here (no mutation before the gate).
        carve_preview: dict[str, list[tuple[str, list[str]]]] = {}
        for cid in sorted(uf_new_demand - dev_targets_new):
            plan: list[tuple[str, list[str]]] = []
            for f in sorted(cand_devs, key=lambda x: str(_attr(x, "name"))):
                files = sorted(
                    p for p in _owned_of(f)
                    if not resolver.in_lane(p)
                    and grain_index.group_cid_of(p) == cid)
                if files:
                    plan.append((str(_attr(f, "name")), files))
            if plan:
                carve_preview[cid] = plan

        # ── plan: PROJECTED target scopes (the validator's I15 view) ──
        # scope(PF) = pf.paths ∪ member devs' paths ∪ planned dev moves;
        # scope(NEW cid) = whole-dev contributors ∪ carve files. The
        # validator reads FULL ``paths`` (owned + shared claims), so the
        # mirror does too — a primary-owned-only scope under-estimates
        # attach and over-refuses (old-pair resim exhibit: 'Manage
        # account utilities' cov 1.00 blocked at a phantom 0.2 attach).
        def _full_paths(f: Any) -> list[str]:
            return [str(p) for p in (_attr(f, "paths") or [])] \
                or _owned_of(f)

        planned_scope: dict[str, set[str]] = defaultdict(set)
        for pf in product_features:
            key = str(_attr(pf, "id") or _attr(pf, "name") or "")
            if key:
                planned_scope["pf:" + key].update(
                    str(p) for p in (_attr(pf, "paths") or []))
        for f in devs:
            pfid = _attr(f, "product_feature_id")
            if pfid:
                planned_scope["pf:" + str(pfid)].update(_full_paths(f))
        for f in sorted(cand_devs, key=lambda x: str(_attr(x, "name"))):
            t = dev_plan[str(_attr(f, "name"))]
            if t is not None:
                planned_scope[_grain_key(t)].update(_full_paths(f))
        for cid, plan in carve_preview.items():
            for _dev_name, files in plan:
                planned_scope["new:" + cid].update(files)

        # ── plan: ATTACH FLOOR (rework a, 2026-07-10) — a rung's target
        # counts as RESOLVED only if the journey's projected lane-aware
        # attach at the target clears the SAME 0.34 floor the
        # validator's I15 gate uses (its exact ruler mirrored: flow
        # PATHS union, lane/neutral files out of the denominator, only
        # journeys with ≥2 member flows gated). A strict/consumer/
        # plurality majority over a SLIVER of a journey must not ship a
        # home the journey's own files barely touch (keyed A/B exhibit:
        # 'Copy document recipient link' cov 0.005 → attach 0.04 →
        # fresh I15+I16 rows). Thin target → UNRESOLVED → the
        # all-or-nothing gate refuses the candidate (status quo).
        for r in resolutions:
            if r.target is None:
                continue
            uf = next(u for u in homed
                      if str(_attr(u, "id") or "") == r.uf_id)
            if len(_attr(uf, "member_flow_ids") or []) < 2:
                continue  # validator's single-flow carve — not gated
            ffiles = _uf_flow_files(uf, flow_by_uuid)
            eff = {p for p in ffiles
                   if not resolver.in_lane(p) and not resolver.is_neutral(p)}
            if not eff:
                continue  # lane-only journey — validator skips it too
            scope = planned_scope.get(_grain_key(r.target), set())
            attach = len(eff & scope) / len(eff)
            r.attach = attach
            if attach < _ATTACH_FLOOR:
                r.rung, r.target = None, None
                r.reason = "attach_floor"

        # ── plan: I16 rail over the PLANNED owner map — EVERY rung ────
        # (rework, 2026-07-10: the zero-NEW-I16-rows principle was
        # ratified for r3 only — a ratification gap; the keyed A/B
        # showed an r2 re-home minting a fresh I16 row, so the rail now
        # guards every rung.)
        demand_after_floor = {
            r.target.key for r in resolutions
            if r.target is not None and r.target.kind == "new"}
        planned_owner = dict(owner_map)
        for f in sorted(cand_devs, key=lambda x: str(_attr(x, "name"))):
            t = dev_plan[str(_attr(f, "name"))]
            planned = None
            if t is not None and (
                    t.kind == "pf" or t.key in demand_after_floor):
                planned = t.key
            for p in _owned_of(f):
                if owner_map.get(p) == cand_key or planned_owner.get(p) \
                        == cand_key:
                    planned_owner[p] = planned
        for r in resolutions:
            if r.target is None:
                continue
            uf = next(u for u in homed
                      if str(_attr(u, "id") or "") == r.uf_id)
            # ZERO **NEW** I16 rows (the measured rail): a journey that
            # is ALREADY majority-foreign today (pre-flagged under the
            # CURRENT owner map vs its dissolving home) does not gain a
            # row by moving — only a clean→flagged transition refuses.
            pre_flagged = _i16_flagged(
                uf, cand_key, flow_by_uuid, owner_map, resolver)
            post_flagged = _i16_flagged(
                uf, r.target.key, flow_by_uuid, planned_owner, resolver)
            if post_flagged and not pre_flagged:
                reason = ("plurality_i16_rail" if r.rung == "r3-plurality"
                          else "i16_rail")
                r.rung, r.target = None, None
                r.reason = reason

        # ── B52 — flow-bearing transport lane (Option A) ──────────────
        # The operator 'трпц A' mandate: a ws-anchored transport
        # candidate ALWAYS leaves the product layer. Resolved journeys
        # re-home onto EXISTING PFs (attach floor + I16 rail already
        # adjudicated above); transport-INTRINSIC journeys stay in
        # user_flows[] as lane-row references (pfid=None + lane_ref +
        # surface_scope); the flowful residue lanes — Seg1: the SAME
        # ws:-anchor carve-out the validator's I9 already grants
        # (is_workspace_anchor), the engine gate aligns instead of
        # over-refusing. Scoped STRICTLY to transport candidates
        # (cand_pf construction requires the ws:<unit> anchor) —
        # non-transport flowful lane attempts keep the old law.
        if flowful_lane:
            from faultline.pipeline_v2.stage_6_86_anchored_mint import (
                _SHARED_REASON_INSTRUMENT as _INSTRUMENT,
            )
            # Mint is FORBIDDEN on this path (B51 receiver law: existing
            # PFs only): a "new"-grain journey resolution is a residue
            # journey (rides the lane); a "new"-grain dev target lanes.
            for r in resolutions:
                if r.target is not None and r.target.kind == "new":
                    r.rung, r.target = None, None
                    r.reason = "flowful_lane_no_mint"
            for dev_name in list(dev_plan):
                t = dev_plan[dev_name]
                if t is not None and t.kind == "new":
                    dev_plan[dev_name] = None

            # Receiver I8 backstop: a decomp receiver that would end
            # flowful-but-journey-less (no existing journey reference,
            # no journey re-homing to it in THIS plan) pulls its carve
            # back — the group becomes residue, byte-equal to never
            # having carved it (the B51 keyed-gate I8 exhibit:
            # cal.com `credits` 26→27).
            planned_to: Counter = Counter(
                r.target.key for r in resolutions if r.target is not None)
            existing_refs: Counter = Counter(
                str(_attr(u, "product_feature_id"))
                for u in user_flows
                if _attr(u, "product_feature_id")
                and str(_attr(u, "product_feature_id")) != cand_key)
            pulled: list[str] = []
            pulled_flows = pulled_files = 0
            if d_commits:
                edges_cur: dict[str, list[Any]] = defaultdict(list)
                for e in (feature_flow_edges or []):
                    edges_cur[str(_attr(e, "flow_id") or "")].append(e)
                for rec in sorted({c["pf_key"] for c in d_commits}):
                    if existing_refs.get(rec) or planned_to.get(rec):
                        continue
                    for c in d_commits:
                        if c["pf_key"] == rec:
                            _pullback_chunk(
                                c, developer_features, edges_cur)
                            pulled_flows += len(c["gflows"])
                            pulled_files += len(c["cfiles"])
                    pulled.append(rec)
            if pulled:
                d_commits = [c for c in d_commits
                             if c["pf_key"] not in pulled]
                # Bookkeeping refresh (sibling candidates + telemetry).
                devs = [f for f in developer_features
                        if _attr(f, "layer", "developer") == "developer"
                        and _attr(f, "name")]
                owner_map, neutral_files = _build_owner_map(devs)
                cand_devs = [
                    f for f in devs
                    if str(_attr(f, "product_feature_id") or "")
                    == cand_key]
                for f in cand_devs:
                    dev_plan.setdefault(str(_attr(f, "name")), None)
                if unit in (tele.get("router_decomp") or {}):
                    rd = tele["router_decomp"][unit]
                    rd["pulled_back"] = pulled
                    rd["residue_flows"] += pulled_flows
                    rd["flows_moved"] -= pulled_flows
                    rd["files_moved"] -= pulled_files
                    for rec in pulled:
                        rd["matched"].pop(rec, None)

            def _lanes(f: Any) -> bool:
                t = dev_plan.get(str(_attr(f, "name")))
                return t is None or t.kind != "pf"

            laned_planned = [
                f for f in sorted(cand_devs,
                                  key=lambda x: str(_attr(x, "name")))
                if _lanes(f)
            ]
            unresolved = [r for r in resolutions if r.target is None]
            if unresolved and not laned_planned:
                # No lane resident to anchor the residue journeys —
                # refuse the whole candidate (legacy block) and UNDO
                # every remaining carve: a receiver whose journey plan
                # just evaporated must not ship flows journey-less.
                if d_commits:
                    edges_u: dict[str, list[Any]] = defaultdict(list)
                    for e in (feature_flow_edges or []):
                        edges_u[str(_attr(e, "flow_id") or "")].append(e)
                    for c in d_commits:
                        _pullback_chunk(c, developer_features, edges_u)
                    d_commits = []
                    if unit in (tele.get("router_decomp") or {}):
                        tele["router_decomp"].pop(unit, None)
                        if not tele["router_decomp"]:
                            tele.pop("router_decomp", None)
                    devs = [f for f in developer_features
                            if _attr(f, "layer", "developer")
                            == "developer" and _attr(f, "name")]
                    owner_map, neutral_files = _build_owner_map(devs)
                    cand_devs = [
                        f for f in devs
                        if str(_attr(f, "product_feature_id") or "")
                        == cand_key]
                tele["conservation_blocked"][unit] = {
                    "pf": cand_key, "ufs_homed": len(homed),
                    "blocked": [
                        {"uf": r.uf_id, "name": r.name,
                         "reason": "no_lane_anchor", "top2": []}
                        for r in unresolved],
                }
                continue  # NO product-layer mutation for this PF

            # ── apply (B52 partial: re-home the resolved, lane the
            # intrinsic — nothing is ever dropped) ─────────────────────
            rehomed_before = tele["ufs_rehomed"]
            lane_rungs: Counter = Counter()
            lane_ufs: list[Any] = []
            for u in sorted(homed, key=lambda x: str(_attr(x, "id") or "")):
                r = res_by_id[str(_attr(u, "id") or "")]
                if r.target is not None:  # kind == "pf" by construction
                    propose_pf_now(u, r.target.key, rung="transport")
                    lane_rungs[r.rung or "?"] += 1
                    tele["ufs_rehomed"] += 1
                    if len(tele["moves"]) < 60:
                        tele["moves"].append({
                            "uf": r.uf_id, "name": r.name, "rung": r.rung,
                            "to": u.product_feature_id,
                            "coverage": round(r.coverage, 3),
                            **({"attach": round(r.attach, 3)}
                               if r.attach is not None else {}),
                            **({"thin_coverage": True}
                               if r.thin_coverage else {}),
                        })
                else:
                    lane_ufs.append(u)

            # devs: re-home or lane — the FLOWFUL residue lanes too
            # (Seg1; validator I9's ws:-anchor exemption, engine-side).
            laned_devs: list[Any] = []
            for f in sorted(cand_devs, key=lambda x: str(_attr(x, "name"))):
                t = dev_plan.get(str(_attr(f, "name")))
                if t is not None and t.kind == "pf":
                    propose_pf_now(f, t.key, rung="transport")
                    f.anchor_id = f"fold:{_HANDOFF_MARKER}->pf:{t.key}"
                    if _attr(f, "shared_reason"):
                        f.shared_reason = None
                    tele["devs_rehomed"] += 1
                else:
                    propose_pf_now(f, None, rung="transport")
                    f.shared_reason = _INSTRUMENT
                    if (_attr(f, "flows") or []):
                        # B52 provenance: only THIS branch ever lanes a
                        # flowful dev — the lane builder keys flow_ids[]
                        # on the marker (E==C SACRED: pre-existing
                        # flowful lane residents stay untouched).
                        f.anchor_id = FLOWFUL_LANE_ANCHOR
                    tele["devs_laned"] += 1
                    laned_devs.append(f)

            # transport-intrinsic journeys → lane-row references. The
            # anchor is the laned dev owning the strict majority of the
            # journey's member flows; fallback: the flow-richest laned
            # dev (deterministic by (-flows, name)).
            flow_owner: dict[str, str] = {}
            lane_flow_ids: list[str] = []
            for f in laned_devs:
                fu = str(_attr(f, "uuid") or "")
                for fl in (_attr(f, "flows") or []):
                    fuid = _attr(fl, "uuid")
                    if fuid:
                        flow_owner[str(fuid)] = fu
                        lane_flow_ids.append(str(fuid))
            anchor_default = ""
            if laned_devs:
                anchor_default = str(_attr(sorted(
                    laned_devs,
                    key=lambda f: (-len(_attr(f, "flows") or []),
                                   str(_attr(f, "name"))))[0], "uuid") or "")
            for u in lane_ufs:
                lane_votes: Counter = Counter()
                for mid in (_attr(u, "member_flow_ids") or []):
                    owner_uuid = flow_owner.get(str(mid))
                    if owner_uuid:
                        lane_votes[owner_uuid] += 1
                anchor = anchor_default
                if lane_votes:
                    ranked = _tie_sorted(lane_votes)
                    top_uuid, ct = ranked[0]
                    if ct * 2 > sum(lane_votes.values()):
                        anchor = str(top_uuid)
                propose_pf_now(u, None, rung="transport")
                u.lane_ref = anchor
                u.surface_scope = "platform_infrastructure"
                lane_rungs["lane"] += 1

            # the candidate PF row leaves the product layer (→ lane).
            product_features[:] = [
                pf for pf in product_features
                if str(_attr(pf, "id") or _attr(pf, "name") or "")
                != cand_key
            ]
            pf_by_key.pop(cand_key, None)
            tele["laned"].append({
                "unit": unit, "pf": cand_key, "ufs": len(homed),
                "rungs": dict(sorted(lane_rungs.items())),
                "minted": {},
                # non-empty ONLY (documenso-keyless SACRED: a flowless
                # candidate's laned row is byte-identical to the legacy
                # path's).
                **({"lane_journeys": len(lane_ufs)} if lane_ufs else {}),
                **({"lane_flows": len(lane_flow_ids)}
                   if lane_flow_ids else {}),
            })
            tele["rungs"][unit] = dict(sorted(lane_rungs.items()))
            if unit in (tele.get("router_decomp") or {}):
                rd = tele["router_decomp"][unit]
                rd["journeys_moved"] = tele["ufs_rehomed"] - rehomed_before
                rd["lane_journeys"] = len(lane_ufs)
                rd["lane_flow_ids"] = len(lane_flow_ids)
            continue

        # ── plan: FINAL demand + contributors (post floor/rail) ───────
        uf_new_demand = {
            r.target.key for r in resolutions
            if r.target is not None and r.target.kind == "new"}
        mintable_new = sorted(uf_new_demand)
        carve_preview = {cid: plan for cid, plan in carve_preview.items()
                         if cid in uf_new_demand}
        undevved = sorted(
            uf_new_demand - dev_targets_new - set(carve_preview))

        # ── plan: flowful-dev guard (rework b — validator I9) ─────────
        # A dev with attached flows must NEVER lane (I9: the platform
        # lane is flowless plumbing only): it either re-homes to a
        # target that will exist post-apply, or the candidate is
        # UNRESOLVED (keyed A/B exhibit: flowful `d-token` laned).
        flowful_stranded: list[str] = []
        for f in sorted(cand_devs, key=lambda x: str(_attr(x, "name"))):
            if not (_attr(f, "flows") or []):
                continue
            t = dev_plan[str(_attr(f, "name"))]
            if t is None or (t.kind == "new" and t.key not in uf_new_demand):
                flowful_stranded.append(str(_attr(f, "name")))

        # ── conservation gate (all-or-nothing) ────────────────────────
        unresolved = [r for r in resolutions if r.target is None]
        blocked_reasons: list[dict[str, Any]] = [
            {"uf": r.uf_id, "name": r.name,
             "reason": r.reason or "unresolved",
             "top2": [[k, c] for k, c in r.top2],
             **({"attach": round(r.attach, 3)}
                if r.attach is not None else {})}
            for r in unresolved
        ]
        if undevved:
            blocked_reasons.append(
                {"uf": None, "name": None,
                 "reason": "new_target_without_devs",
                 "top2": [[k, 0] for k in undevved]})
        if flowful_stranded:
            blocked_reasons.append(
                {"uf": None, "name": None,
                 "reason": "flowful_dev_would_lane",
                 "top2": [[n, 0] for n in flowful_stranded]})
        if blocked_reasons:
            tele["conservation_blocked"][unit] = {
                "pf": cand_key, "ufs_homed": len(homed),
                "blocked": blocked_reasons,
            }
            continue  # NO mutation — exact flag-OFF output for this PF

        # ── apply (verified plan only) ────────────────────────────────
        used_slugs = set(pf_by_key) | {"platform", "shared-platform"}
        minted_key: dict[str, str] = {}  # anchor cid → pf slug
        from faultline.pipeline_v2.nav_taxonomy import (
            aggregate_product_feature,
        )
        from faultline.pipeline_v2.stage_6_86_anchored_mint import (
            _SHARED_REASON_INSTRUMENT,
            _slug,
        )

        contrib_by_new: dict[str, list[Any]] = defaultdict(list)
        for f in sorted(cand_devs, key=lambda x: str(_attr(x, "name"))):
            t = dev_plan[str(_attr(f, "name"))]
            if t is not None and t.kind == "new" and t.key in uf_new_demand:
                contrib_by_new[t.key].append(f)
        # Carve chunks for demanded targets without a whole-dev
        # contributor (previewed above; the gate passed, so every plan
        # is non-empty). 8.9.x discipline: the chunk takes ONLY the
        # source dev's own group files; a carve that would EMPTY the
        # source moves the whole dev instead.
        dev_by_name = {str(_attr(f, "name")): f for f in cand_devs}
        edges_by_flow_id: dict[str, list[Any]] = defaultdict(list)
        for e in (feature_flow_edges or []):
            edges_by_flow_id[_attr(e, "flow_id")].append(e)
        for cid in sorted(carve_preview):
            for dev_name, files in carve_preview[cid]:
                src = dev_by_name[dev_name]
                owned_all = _owned_of(src)
                if len(files) >= len(owned_all):
                    dev_plan[dev_name] = GrainTarget("new", cid)
                    contrib_by_new[cid].append(src)
                    continue
                chunk = _carve_chunk(src, cid, files)
                _move_carved_flows(src, chunk, set(files), edges_by_flow_id)
                _strip_carved_files(src, set(files))
                developer_features.append(chunk)
                contrib_by_new[cid].append(chunk)
                tele["devs_carved"] = tele.get("devs_carved", 0) + 1
        for cid in mintable_new:
            display = grain_index.display_of(cid)
            slug = _slug(display) or _slug(cid.rsplit(":", 1)[-1])
            if slug in used_slugs:
                slug = _slug(f"{display} ({cid.rsplit('/', 1)[-1]})")
                n = 2
                while slug in used_slugs:
                    slug = _slug(f"{display} {n}")
                    n += 1
            used_slugs.add(slug)
            contrib = contrib_by_new[cid]
            pf = aggregate_product_feature(
                name=slug,
                display_name=display,
                description=(
                    f"Capability anchored at {cid} "
                    f"({len(contrib)} developer feature(s); "
                    f"{_HANDOFF_MARKER} of '{cand_key}')."
                ),
                contrib=contrib,
            )
            pf.layer = "product"
            pf.anchor_id = cid
            product_features.append(pf)
            pf_by_key[slug] = pf
            minted_key[cid] = slug
            tele["pfs_minted"] += 1
            # Carved chunks are not in the dev-move loop below — stamp
            # every contributor here (whole devs get the identical
            # stamp again there; idempotent).
            for c in contrib:
                propose_pf_now(c, slug, rung="transport")
                c.anchor_id = f"fold:{_HANDOFF_MARKER}->{cid}"
                if _attr(c, "shared_reason"):
                    c.shared_reason = None

        def _final_key(t: GrainTarget) -> str:
            return t.key if t.kind == "pf" else minted_key[t.key]

        # devs: re-home or lane (never left on the dissolving PF).
        for f in sorted(cand_devs, key=lambda x: str(_attr(x, "name"))):
            t = dev_plan[str(_attr(f, "name"))]
            if (t is not None and (t.kind == "pf"
                                   or t.key in minted_key)):
                propose_pf_now(f, _final_key(t), rung="transport")
                f.anchor_id = f"fold:{_HANDOFF_MARKER}->" + (
                    t.key if t.kind == "new" else f"pf:{t.key}")
                if _attr(f, "shared_reason"):
                    f.shared_reason = None
                tele["devs_rehomed"] += 1
            else:
                propose_pf_now(f, None, rung="transport")
                f.shared_reason = _SHARED_REASON_INSTRUMENT
                tele["devs_laned"] += 1

        # journeys: every homed UF re-homes (gate guaranteed a target).
        rung_counter: Counter = Counter()
        for u in sorted(homed, key=lambda x: str(_attr(x, "id") or "")):
            r = res_by_id[str(_attr(u, "id") or "")]
            assert r.target is not None  # gate invariant
            propose_pf_now(u, _final_key(r.target), rung="transport")
            rung_counter[r.rung or "?"] += 1
            tele["ufs_rehomed"] += 1
            if len(tele["moves"]) < 60:
                tele["moves"].append({
                    "uf": r.uf_id, "name": r.name, "rung": r.rung,
                    "to": u.product_feature_id,
                    "coverage": round(r.coverage, 3),
                    **({"attach": round(r.attach, 3)}
                       if r.attach is not None else {}),
                    **({"thin_coverage": True} if r.thin_coverage else {}),
                })

        # the candidate PF row leaves the product layer (→ lane).
        product_features[:] = [
            pf for pf in product_features
            if str(_attr(pf, "id") or _attr(pf, "name") or "") != cand_key
        ]
        pf_by_key.pop(cand_key, None)
        tele["laned"].append({
            "unit": unit, "pf": cand_key, "ufs": len(homed),
            "rungs": dict(sorted(rung_counter.items())),
            "minted": dict(sorted(minted_key.items())),
        })
        tele["rungs"][unit] = dict(sorted(rung_counter.items()))

    if echo is not None:
        tele["namespace_echo"] = {
            "enabled": True,
            "seeds_matched": len(echo.matched),
            "nav_corroborated": echo.nav_corroborated,
            "moves": dict(sorted(echo.matched.items())),
        }

    # ── hard conservation invariant (the doctrine, structurally) ────────
    violations = _conservation_violations(
        uf_count_before, uf_home_before, user_flows,
        {row["pf"] for row in tele["laned"]})
    if violations:
        tele["conservation_violations"] = violations
        if strict:
            raise AssertionError(
                "transport_handoff conservation violated: "
                + "; ".join(violations))
    return tele


def _conservation_violations(
    uf_count_before: int,
    uf_home_before: Mapping[str, int],
    user_flows: list[Any],
    laned_keys: set[str],
) -> list[str]:
    """Structural journey-conservation check (B20's ``uf_count``
    bookkeeping shape, inverted for a dissolving source): the UF COUNT
    is exactly conserved, no NON-laned PF ever loses a journey, and no
    journey still points at a dissolved home. Raised on (test/strict
    mode) or telemetered (prod) by the caller."""
    violations: list[str] = []
    if len(user_flows) != uf_count_before:
        violations.append(
            f"uf_count {uf_count_before} -> {len(user_flows)}")
    uf_home_after: Counter = Counter(
        str(_attr(u, "product_feature_id"))
        for u in user_flows if _attr(u, "product_feature_id"))
    for key in sorted(uf_home_before):
        before = uf_home_before[key]
        if key in laned_keys:
            continue  # dissolving home — journeys moved, by design
        if uf_home_after.get(key, 0) < before:
            violations.append(
                f"pf '{key}' journeys {before} -> "
                f"{uf_home_after.get(key, 0)}")
    for u in user_flows:
        ref = _attr(u, "product_feature_id")
        if ref is not None and str(ref) in laned_keys:
            violations.append(
                f"uf {_attr(u, 'id')} still on laned '{ref}'")
    return violations
