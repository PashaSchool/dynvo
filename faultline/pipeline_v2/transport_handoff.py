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
resolved a target (and every NEW target has a contributing dev so the
mint is never a phantom). ANY unresolved UF → the candidate does NOT
lane — the scan output for that PF is exactly the flag-OFF output plus
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

__all__ = [
    "TRANSPORT_HANDOFF_ENV",
    "TRANSPORT_HANDOFF_PLURALITY_ENV",
    "transport_handoff_enabled",
    "transport_plurality_enabled",
    "hub_cutoff",
    "GrainTarget",
    "TargetGrainIndex",
    "resolve_user_flow",
    "run_transport_handoff",
]

TRANSPORT_HANDOFF_ENV = "FAULTLINE_TRANSPORT_LANE_HANDOFF"
TRANSPORT_HANDOFF_PLURALITY_ENV = "FAULTLINE_TRANSPORT_HANDOFF_PLURALITY"

#: Provenance marker stamped on re-homed / minted rows (I22
#: explainability + idempotence).
_HANDOFF_MARKER = "transport-handoff"

#: Coverage telemetry floor (design §4 thin-coverage class): a strict
#: re-home whose VOTING mass covers < 34% of the journey's span mass is
#: accepted (dissolution is worse than a thin honest home) but marked.
_THIN_COVERAGE = 0.34

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

    ``grain_of_file``: most specific non-shell, non-barred anchor whose
    subtree contains the file (exact-file beats prefix; longer prefix
    beats shorter; ties break on canonical_id) — an anchor with a
    minted PF answers that PF; an unminted ROUTE-sourced anchor answers
    a ``new`` excavation target; anything else answers ``None`` (the
    consumer rung may still resolve the file). Anchors inside the
    candidate unit / instrument dirs never answer (lane is never a
    target — B20 law).
    """

    def __init__(
        self,
        anchors: Iterable[Any],
        product_features: Iterable[Any],
        excluded_units: Iterable[str] = (),
        candidate_pf_keys: Iterable[str] = (),
    ) -> None:
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
        self._anchors: list[Any] = []
        for a in sorted(anchors, key=lambda x: x.canonical_id):
            if getattr(a, "shell", False) or getattr(a, "barred", None):
                continue
            units = list(getattr(a, "prefixes", ()) or ()) + sorted(
                getattr(a, "files", ()) or ())
            if units and all(self._in_excluded(u) for u in units):
                continue  # anchor lives wholly inside a lane unit
            self._anchors.append(a)
        self._memo: dict[str, GrainTarget | None] = {}

    def _in_excluded(self, path: str) -> bool:
        return any(path == u or path.startswith(u + "/")
                   for u in self._excluded)

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
        out: GrainTarget | None = None
        if best is not None:
            pf_key = self._pf_by_anchor.get(best.canonical_id)
            if pf_key is not None and pf_key not in self._cand_keys:
                out = GrainTarget("pf", pf_key,
                                  display=getattr(best, "display", "") or "")
            elif pf_key is None and "route" in (
                    getattr(best, "sources", None) or {best.source}):
                # Unminted route-group anchor → excavation target.
                out = GrainTarget("new", best.canonical_id,
                                  display=getattr(best, "display", "") or "")
        self._memo[path] = out
        return out

    def anchor_of(self, canonical_id: str) -> Any:
        for a in self._anchors:
            if a.canonical_id == canonical_id:
                return a
        return None


def _norm_route_segs(pattern: str) -> tuple[str, ...]:
    """URL pattern → comparable segments (every dialect's params → *)."""
    from faultline.pipeline_v2.spine_anchors import _DYNAMIC_RE

    out: list[str] = []
    for seg in str(pattern or "").split("/"):
        if not seg:
            continue
        out.append("*" if _DYNAMIC_RE.match(seg) else seg.lower())
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
        want = _norm_route_segs(url)
        if not want:
            return None
        best_file: str | None = None
        best_score = 0
        for segs, f in self._entries:
            if segs == want:
                run = len(want) + 1_000  # exact match dominates
            else:
                run = 0
                for a, b in zip(segs, want):
                    if a == b or a == "*" or b == "*":
                        run += 1
                    else:
                        break
            if run > best_score:
                best_score, best_file = run, f
        if best_file is None or best_score == 0:
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
    top2: list[tuple[str, int]] = field(default_factory=list)
    reason: str | None = None  # unresolved reason


def _tie_sorted(votes: Counter) -> list[tuple[Any, int]]:
    """B20's deterministic ``(-count, str(key))`` convention."""
    return sorted(votes.items(), key=lambda kv: (-kv[1], str(kv[0])))


def _grain_key(t: GrainTarget) -> str:
    return f"{t.kind}:{t.key}"


class _FileResolver:
    """Ladder over ONE candidate: lane → owned → route grain → consumer
    seed. Memoised; every rung deterministic."""

    def __init__(
        self,
        unit: str,
        cand_pf_key: str,
        owner_map: Mapping[str, str | None],
        grain: TargetGrainIndex,
        consumers: ConsumerIndex | None,
        lane_pf_keys: frozenset[str] = frozenset(),
    ) -> None:
        self.unit = unit.strip("/")
        self.cand = cand_pf_key
        self.owner = owner_map
        self.grain = grain
        self.consumers = consumers
        self.lane_keys = lane_pf_keys
        self._direct: dict[str, GrainTarget | None] = {}
        self._seed: dict[str, tuple[GrainTarget | None, str]] = {}

    def in_lane(self, path: str) -> bool:
        return path == self.unit or path.startswith(self.unit + "/")

    def direct(self, path: str) -> GrainTarget | None:
        """Rungs 1-3 of the ladder (no consumer walk)."""
        if path in self._direct:
            return self._direct[path]
        out: GrainTarget | None = None
        if not self.in_lane(path):
            own = self.owner.get(path)
            if (own is not None and own != self.cand
                    and own not in self.lane_keys
                    and own in self.grain.pf_keys):
                out = GrainTarget("pf", str(own))
            else:
                out = self.grain.grain_of_file(path)
        self._direct[path] = out
        return out

    def seed(self, path: str) -> tuple[GrainTarget | None, str]:
        """r2 consumer completion for one non-voting seed file —
        winner-take-all strict majority over its consumer votes."""
        if path in self._seed:
            return self._seed[path]
        out: tuple[GrainTarget | None, str]
        if self.consumers is None:
            out = (None, "no_consumer_index")
            self._seed[path] = out
            return out
        votes: Counter = Counter()
        if self.in_lane(path):
            pool: list[str] = []
            for c in sorted(self.consumers.unit_file_consumers(path)):
                t = self.direct(c)
                if t is not None:
                    votes[_grain_key(t)] += 1
                elif (not self.in_lane(c)
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
                out = (None, "hub")
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
                              and len(self.consumers.importers_of(imp))
                              <= self.consumers.cutoff):
                            nxt.append(imp)
                frontier = nxt
        total = sum(votes.values())
        if not total:
            out = (None, "no_consumers")
        else:
            ranked = _tie_sorted(votes)
            top_key, ct = ranked[0]
            if ct * 2 > total:
                kind, _, key = str(top_key).partition(":")
                out = (GrainTarget(kind, key), f"{ct}/{total}")
            else:
                out = (None, "split")
        self._seed[path] = out
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

    # r1 — direct owned+route votes over non-lane span mass.
    direct_votes: Counter = Counter()
    seeds: list[tuple[str, int]] = []
    for p in sorted(mass):
        m = mass[p]
        t = resolver.direct(p)
        if t is not None:
            direct_votes[_grain_key(t)] += m
        else:
            seeds.append((p, m))
    voting = sum(direct_votes.values())
    if voting:
        ranked = _tie_sorted(direct_votes)
        top_key, ct = ranked[0]
        if ct * 2 > voting:
            kind, _, key = str(top_key).partition(":")
            res.rung, res.target = "r1-strict", GrainTarget(kind, key)
            res.voting_mass = voting
            res.coverage = voting / res.total_mass
            res.thin_coverage = res.coverage < _THIN_COVERAGE
            res.top2 = [(str(k), c) for k, c in ranked[:2]]
            return res

    # r2 — consumer completion: each seed re-homes winner-take-all.
    pooled = Counter(direct_votes)
    for p, m in seeds:
        t, _why = resolver.seed(p)
        if t is not None:
            pooled[_grain_key(t)] += m
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
        # r3 — plurality last resort: top1 must STRICTLY beat top2 (a
        # 50/50 tie never re-homes); the caller adds the I16 rail.
        if plurality_ok and (len(ranked) < 2 or ct > ranked[1][1]):
            kind, _, key = str(top_key).partition(":")
            res.rung, res.target = "r3-plurality", GrainTarget(kind, key)
            return res
        res.reason = "split"
        return res
    res.reason = "zero_product_votes"
    return res


# ── The stage ────────────────────────────────────────────────────────────


def _owned_of(f: Any) -> list[str]:
    from faultline.pipeline_v2.spine_anchors import owned_paths_of
    return owned_paths_of(f)


def _build_owner_map(devs: list[Any]) -> dict[str, str | None]:
    """file → owning dev's ``product_feature_id`` (LIVE state — this
    stage runs before the emission path_index refresh, so the 6.8 index
    is stale for post-6.8 dev moves; the dev ledger is the truth).
    First claimant in name-sorted dev order wins (deterministic)."""
    owner: dict[str, str | None] = {}
    for f in sorted(devs, key=lambda x: str(_attr(x, "name") or "")):
        pfid = _attr(f, "product_feature_id")
        for p in _owned_of(f):
            owner.setdefault(p, pfid)
    return owner


def _i16_new_row(
    uf: Any,
    target_key: str,
    flow_by_uuid: Mapping[str, Any],
    planned_owner: Mapping[str, str | None],
    lane_files: "_FileResolver",
) -> bool:
    """The plurality rail: would this re-home be a NEW I16 row under the
    entry-owner ruler projected over the POST-handoff owner map?
    Lane-neutral (B21): lane-resident / unowned entries never count."""
    dist: Counter = Counter()
    chk = 0
    for fid in (_attr(uf, "member_flow_ids") or []):
        fl = flow_by_uuid.get(fid)
        ep = _attr(fl, "entry_point_file") if fl is not None else None
        if not ep:
            continue
        if lane_files.in_lane(str(ep)):
            continue
        own = planned_owner.get(str(ep))
        if own is None:
            continue
        chk += 1
        dist[str(own)] += 1
    if not chk:
        return False
    mis = sum(c for o, c in dist.items() if o != target_key)
    return mis * 2 > chk  # majority-foreign to the NEW home = new I16 row


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
    grain_index: TargetGrainIndex | None = None,
    consumer_index_factory: Any = None,
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

    owner_map = _build_owner_map(devs)

    # Candidate unit → its minted PF (anchor identity: ``ws:<unit>``).
    pf_by_key = { (str(_attr(pf, "id") or _attr(pf, "name"))): pf
                  for pf in product_features
                  if (_attr(pf, "id") or _attr(pf, "name")) }
    cand_pf: dict[str, str] = {}
    for unit in sorted(transport_candidates):
        want = f"ws:{unit.strip('/')}"
        for pf in product_features:
            if str(_attr(pf, "anchor_id") or "") == want:
                key = str(_attr(pf, "id") or _attr(pf, "name") or "")
                if key:
                    cand_pf[unit] = key
                break
    tele["candidate_pfs"] = dict(sorted(cand_pf.items()))
    if not cand_pf:
        return tele

    # THE grain oracle (condition 4) — built once, shared by vote+mint.
    if grain_index is None:
        from faultline.pipeline_v2.spine_anchors import build_spine_anchors
        anchors = build_spine_anchors(
            devs, routes_index, ctx, extractor_signals, frozenset())
        grain_index = TargetGrainIndex(
            anchors, product_features,
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
            lane_pf_keys=frozenset(cand_pf.values()))

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
                    t, _why = resolver.seed(p)
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

        # ── plan: NEW-target demand + contributing-dev check ──────────
        uf_new_demand = {
            r.target.key for r in resolutions
            if r.target is not None and r.target.kind == "new"}
        dev_targets_new = {
            t.key for t in dev_plan.values()
            if t is not None and t.kind == "new"}
        mintable_new = sorted(uf_new_demand)
        undevved = sorted(uf_new_demand - dev_targets_new)

        # ── plan: plurality I16 rail over the PLANNED owner map ───────
        planned_owner = dict(owner_map)
        for f in sorted(cand_devs, key=lambda x: str(_attr(x, "name"))):
            t = dev_plan[str(_attr(f, "name"))]
            planned = t.key if t is not None else None
            for p in _owned_of(f):
                if owner_map.get(p) == cand_key or planned_owner.get(p) \
                        == cand_key:
                    planned_owner[p] = planned
        for r in resolutions:
            if r.rung != "r3-plurality" or r.target is None:
                continue
            uf = next(u for u in homed
                      if str(_attr(u, "id") or "") == r.uf_id)
            if _i16_new_row(uf, r.target.key, flow_by_uuid, planned_owner,
                            resolver):
                r.rung, r.target = None, None
                r.reason = "plurality_i16_rail"

        # ── conservation gate (all-or-nothing) ────────────────────────
        unresolved = [r for r in resolutions if r.target is None]
        blocked_reasons: list[dict[str, Any]] = [
            {"uf": r.uf_id, "name": r.name,
             "reason": r.reason or "unresolved",
             "top2": [[k, c] for k, c in r.top2]}
            for r in unresolved
        ]
        if undevved:
            blocked_reasons.append(
                {"uf": None, "name": None,
                 "reason": "new_target_without_devs",
                 "top2": [[k, 0] for k in undevved]})
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
        for cid in mintable_new:
            a = grain_index.anchor_of(cid)
            display = (getattr(a, "display", "") or
                       cid.rsplit("/", 1)[-1]) if a is not None else \
                cid.rsplit("/", 1)[-1]
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

        def _final_key(t: GrainTarget) -> str:
            return t.key if t.kind == "pf" else minted_key[t.key]

        # devs: re-home or lane (never left on the dissolving PF).
        for f in sorted(cand_devs, key=lambda x: str(_attr(x, "name"))):
            t = dev_plan[str(_attr(f, "name"))]
            if (t is not None and (t.kind == "pf"
                                   or t.key in minted_key)):
                f.product_feature_id = _final_key(t)
                f.anchor_id = f"fold:{_HANDOFF_MARKER}->" + (
                    t.key if t.kind == "new" else f"pf:{t.key}")
                if _attr(f, "shared_reason"):
                    f.shared_reason = None
                tele["devs_rehomed"] += 1
            else:
                f.product_feature_id = None
                f.shared_reason = _SHARED_REASON_INSTRUMENT
                tele["devs_laned"] += 1

        # journeys: every homed UF re-homes (gate guaranteed a target).
        rung_counter: Counter = Counter()
        for u in sorted(homed, key=lambda x: str(_attr(x, "id") or "")):
            r = res_by_id[str(_attr(u, "id") or "")]
            assert r.target is not None  # gate invariant
            u.product_feature_id = _final_key(r.target)
            rung_counter[r.rung or "?"] += 1
            tele["ufs_rehomed"] += 1
            if len(tele["moves"]) < 60:
                tele["moves"].append({
                    "uf": r.uf_id, "name": r.name, "rung": r.rung,
                    "to": u.product_feature_id,
                    "coverage": round(r.coverage, 3),
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
