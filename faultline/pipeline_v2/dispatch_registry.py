"""B34 Tier 2 — dispatch-registry detection + system-flow seeds ($0).

The SentinelOne class (wave-14 operator exhibit): a vendor connector is
implemented and product-real, but its ONLY call path runs through a
string-keyed dispatch site — a Python factory with branch-local lazy
imports (`services/edr/factory.py`) or a TS switch/object registry
(`graph/processors/serializeEntitiesToNodes.ts`). Static call-graph
expansion cannot cross runtime string dispatch, so the connector files
carry ``flows: 0``, the owning PF is flowless, and the board shows an
honest ``Uncovered:`` marker.

The registry itself is the maintainer's authoritative declaration that
these N targets exist (INGEST principle — same doctrine as i18n/nav
lists): each branch/key names exactly one target module. This stage
reads that declaration and mints ONE deterministic flow seed per
UNCOVERED registry target:

  * detection signatures (mechanical, no vocabularies):
      (a) *branch-local lazy-import factory* (Python ``ast``): a
          function with >=2 ``if``/``elif`` branches whose body does a
          branch-local import and returns the imported symbol (bare or
          instantiated);
      (b) *switch/object registry* (TS/JS, literal grammar): >=2
          ``case '<key>': … return <ImportedIdent>`` arms whose idents
          resolve to static import specifiers, or >=2
          ``<key>: … import('<literal>')`` map entries.
  * minting guard: the target file must currently be COVERED BY NO
    stage-3 flow (entry or reach path) — the detector never duplicates
    an existing flow's territory, and a module that is lazily
    importable but NOT declared by any registry is NEVER revived
    (dead-code anti-case: minting is declaration-driven).
  * the seed: ``run-<symbol>-flow`` (verb ``run`` — the closed trigger
    semantics of a dispatched connector; symbol kebab via the B30
    ``flow_name_v2`` machinery), entry = the target file's declared
    symbol (line resolved like every stage-3 seed), owner = the
    developer feature that owns the target file. No owner → no seed
    (never invent features). Downstream stages (5.5 ids, lineage, UF
    rollup, recall) treat these seeds like any other flow — the
    ``Uncovered:`` marker for the PF dissolves NATURALLY once the PF
    has member flows; no marker suppression anywhere.

Kill-switch: ``FAULTLINE_DISPATCH_REGISTRY_FLOWS=0`` (default) — the
stage never runs, output byte-identical. Deterministic: sorted file
walks, sorted targets, dedup by (feature, name). Telemetry via stage
artifact/log only.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.flow_name_v2 import _symbol_name, _slugify
from faultline.pipeline_v2.lazy_imports import (
    LazyImportEdge,
    build_ts_suffix_index,
    _ts_norm,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows

__all__ = [
    "DISPATCH_REGISTRY_ENV",
    "RegistryTarget",
    "dispatch_registry_enabled",
    "detect_py_registries",
    "detect_ts_registries",
    "mint_dispatch_seeds",
    "run_dispatch_registry_stage",
]

DISPATCH_REGISTRY_ENV = "FAULTLINE_DISPATCH_REGISTRY_FLOWS"

_MAX_BYTES = 1_500_000
_TS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

#: TS static import: ``import { A, B as C } from 'spec'`` (single line —
#: the registry grammar this stage reads is line-shaped by convention).
_TS_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:type\s+)?{?\s*([^}]*?)\s*}?\s+from\s+['\"]([^'\"]+)['\"]",
)
_TS_CASE_RE = re.compile(r"^\s*case\s+['\"]([^'\"]+)['\"]\s*:")
_TS_RETURN_IDENT_RE = re.compile(
    r"^\s*return\s+\[?\s*(?:new\s+)?([A-Za-z_$][\w$]*)",
)
#: Object-literal registry entry with a lazy component:
#: ``key: … import('<literal>')`` (quoted or bare key).
#: B37-s1 — then-member navigator entry (supabase Integrations landing):
#: ``dynamic(() => import('spec').then((mod) => mod.Symbol))``. Whole-text
#: scan (the real sites wrap across lines); bounded gap between the
#: import literal and the member access.
_TS_THEN_MEMBER_RE = re.compile(
    r"import\(\s*['\"]([^'\"]+)['\"]\s*\)[\s\S]{0,40}?"
    r"\.then\(\s*\(?(\w+)\)?\s*=>\s*\2\.(\w+)",
)
#: B37-s3 — block-execute string dispatch guard (typebot):
#: ``if ("chatwoot" in clientSideAction)``.
_TS_IF_IN_RE = re.compile(r"^\s*if\s*\(\s*['\"]([\w-]+)['\"]\s+in\s+\w")
#: A call to an imported executor within an arm body:
#: ``return executeChatwoot(...)`` / ``await executeX(...)``.
_TS_ARM_CALL_RE = re.compile(
    r"^\s*(?:return\s+|await\s+)?([A-Za-z_$][\w$]*)\(",
)
#: B37-s2 — default-import config registry (midday app-store):
#: ``import xApp from "./x/config-client"`` collected into a literal.
_TS_DEFAULT_IMPORT_RE = re.compile(
    r"^\s*import\s+([A-Za-z_$][\w$]*)\s+from\s+['\"](\.[^'\"]+)['\"]",
)
_TS_BARE_ELEMENT_RE = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*,?\s*$")

_TS_MAP_DYNAMIC_RE = re.compile(
    r"^\s*['\"]?([\w$-]+)['\"]?\s*:\s*.*?\bimport\(\s*['\"]([^'\"]+)['\"]",
)


def dispatch_registry_enabled() -> bool:
    """Default ON since the 2026-07-10 B34-b re-flip proof (keyed supabase +
    Soc0 with the rails merged: hollow=0 on both, markers 14->4 / 3->1,
    Soc0 gauntlet CLEAR). History: the first ON-flip was reverted the same
    day after 328 hollow UI-demo mints on supabase; the B34-b anchor guard
    now makes hollow mints structurally impossible. ``=0`` restores the
    pre-B34 board byte-identically."""
    return os.environ.get(DISPATCH_REGISTRY_ENV, "1").strip() in {
        "1", "true", "True",
    }


@dataclass(frozen=True)
class RegistryTarget:
    """One declared dispatch target: registry file → target module."""

    registry_file: str
    key: str             # the dispatch key ("sentinelOne", …); "" if unknown
    symbol: str          # exported symbol dispatched to ("" if unknown)
    target_file: str


# ── Python detector (ast) ───────────────────────────────────────────────


def detect_py_registries(
    repo_path: Path | str,
    py_lazy_edges: list[LazyImportEdge],
) -> list[RegistryTarget]:
    """Branch-local lazy-import factories (signature (a)).

    Only files that already carry lazy edges are parsed (cheap
    prefilter — a registry without lazy imports is the TS shape).
    """
    root = Path(repo_path)
    edge_by_src: dict[str, dict[str, str]] = {}
    for e in py_lazy_edges:
        if e.lang == "py":
            edge_by_src.setdefault(e.src, {})[e.target] = e.target_file

    out: list[RegistryTarget] = []
    for src in sorted(edge_by_src):
        try:
            p = root / src
            if p.stat().st_size > _MAX_BYTES:
                continue
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, SyntaxError):
            continue
        module_index = edge_by_src[src]
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            branches: list[tuple[str, str, str]] = []  # (key, symbol, module)
            for br in ast.walk(fn):
                if not isinstance(br, ast.If):
                    continue
                key = _py_branch_key(br.test)
                imported: dict[str, str] = {}  # symbol -> module
                returned: str | None = None
                for stmt in br.body:
                    if isinstance(stmt, ast.ImportFrom) and stmt.module:
                        for alias in stmt.names:
                            imported[alias.asname or alias.name] = stmt.module
                    if isinstance(stmt, ast.Return) and stmt.value is not None:
                        returned = _returned_name(stmt.value)
                if returned and returned in imported:
                    branches.append((key, returned, imported[returned]))
            if len(branches) < 2:
                continue
            for key, symbol, module in branches:
                target = module_index.get(module)
                if target:
                    out.append(RegistryTarget(
                        registry_file=src, key=key, symbol=symbol,
                        target_file=target,
                    ))
    return sorted(set(out), key=lambda t: (
        t.registry_file, t.target_file, t.symbol, t.key,
    ))


def _py_branch_key(test: ast.expr) -> str:
    """The compared string constant in an ``if x == "key"`` guard."""
    if isinstance(test, ast.Compare):
        for cmp_node in [*test.comparators, test.left]:
            if isinstance(cmp_node, ast.Constant) and isinstance(
                cmp_node.value, str,
            ):
                return cmp_node.value
            if isinstance(cmp_node, (ast.Tuple, ast.List, ast.Set)):
                for el in cmp_node.elts:
                    if isinstance(el, ast.Constant) and isinstance(
                        el.value, str,
                    ):
                        return el.value
    return ""


def _returned_name(value: ast.expr) -> str | None:
    """The identifier a ``return`` hands back — bare or instantiated."""
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return value.func.id
    return None


# ── TS detector (literal grammar) ───────────────────────────────────────


def detect_ts_registries(
    repo_path: Path | str,
    tracked_files: list[str],
) -> list[RegistryTarget]:
    """Switch / object-literal registries (signature (b))."""
    root = Path(repo_path)
    files = [str(f).replace("\\", "/") for f in tracked_files]
    ts_index = build_ts_suffix_index(files)

    out: list[RegistryTarget] = []
    for rel in sorted(files):
        if not rel.endswith(_TS_EXT):
            continue
        try:
            p = root / rel
            if p.stat().st_size > _MAX_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Pass-scoped prefilter: b1/b2/b3 need switch/dynamic-import
        # grammar; b4 needs the '"key" in x' guard; b5 needs relative
        # default imports. Skip only files that can host NONE of them.
        if ("switch" not in text and "import(" not in text
                and '" in ' not in text and "' in " not in text
                and "import " not in text):
            continue
        lines = text.splitlines()

        # Static import map: ident -> specifier (line grammar).
        ident_spec: dict[str, str] = {}
        for line in lines:
            m = _TS_IMPORT_RE.match(line)
            if not m:
                continue
            for raw in m.group(1).split(","):
                name = raw.strip().split(" as ")[-1].strip().lstrip("* ")
                if name and re.match(r"^[A-Za-z_$][\w$]*$", name):
                    ident_spec[name] = m.group(2)

        # (b1) switch arms: case '<key>': … return <ImportedIdent>
        arms: list[tuple[str, str, str]] = []  # (key, ident, spec)
        current_key: str | None = None
        for line in lines:
            cm = _TS_CASE_RE.match(line)
            if cm:
                current_key = cm.group(1)
                continue
            if current_key is not None:
                rm = _TS_RETURN_IDENT_RE.match(line)
                if rm and rm.group(1) in ident_spec:
                    arms.append(
                        (current_key, rm.group(1), ident_spec[rm.group(1)]),
                    )
                    current_key = None
                elif line.strip().startswith(("case ", "default", "}")):
                    current_key = None
        if len(arms) >= 2:
            for key, ident, spec in arms:
                nk = _ts_norm(spec)
                target = ts_index.get(nk) if nk else None
                if target and target != rel:
                    out.append(RegistryTarget(
                        registry_file=rel, key=key, symbol=ident,
                        target_file=target,
                    ))

        # (b2) object-literal map with lazy components.
        map_entries: list[tuple[str, str]] = []
        for line in lines:
            m = _TS_MAP_DYNAMIC_RE.match(line)
            if m:
                map_entries.append((m.group(1), m.group(2)))
        if len(map_entries) >= 2:
            for key, spec in map_entries:
                nk = _ts_norm(spec)
                target = ts_index.get(nk) if nk else None
                if target and target != rel:
                    out.append(RegistryTarget(
                        registry_file=rel, key=key, symbol="",
                        target_file=target,
                    ))

        # (b3, B37-s1) then-member navigator entries — whole-text scan
        # (the supabase Integrations landing wraps these across lines):
        # import('spec').then((mod) => mod.Symbol). The member IS the
        # exported symbol — anchors + names like any symbol-ful target.
        then_hits = _TS_THEN_MEMBER_RE.findall(text)
        if len(then_hits) >= 2:
            for spec, _param, member in then_hits:
                nk = _ts_norm(spec)
                target = ts_index.get(nk) if nk else None
                if target and target != rel:
                    out.append(RegistryTarget(
                        registry_file=rel, key="", symbol=member,
                        target_file=target,
                    ))

        # (b4, B37-s3) block-execute string dispatch — typebot shape:
        # if ("chatwoot" in action) { return executeChatwoot(...) }.
        # Arms must call a STATICALLY IMPORTED executor (flavor D — the
        # registry declares the block set even though imports are static).
        arm_key: str | None = None
        arm_gap = 0
        if_in_arms: list[tuple[str, str, str]] = []
        for line in lines:
            km = _TS_IF_IN_RE.match(line)
            if km:
                arm_key = km.group(1)
                arm_gap = 0
                continue
            if arm_key is not None:
                arm_gap += 1
                cm = _TS_ARM_CALL_RE.match(line)
                if cm and cm.group(1) in ident_spec:
                    if_in_arms.append(
                        (arm_key, cm.group(1), ident_spec[cm.group(1)]),
                    )
                    arm_key = None
                elif arm_gap >= 4:
                    arm_key = None
        if len(if_in_arms) >= 2:
            for key, ident, spec in if_in_arms:
                nk = _ts_norm(spec)
                target = ts_index.get(nk) if nk else None
                if target and target != rel:
                    out.append(RegistryTarget(
                        registry_file=rel, key=key, symbol=ident,
                        target_file=target,
                    ))

        # (b5, B37-s2) default-import config registry — midday app-store
        # shape: >=2 sibling default imports whose idents are later
        # enumerated as bare literal elements (the exported apps array).
        def_imports: dict[str, str] = {}
        for line in lines:
            m = _TS_DEFAULT_IMPORT_RE.match(line)
            if m:
                def_imports[m.group(1)] = m.group(2)
        if len(def_imports) >= 2:
            enumerated: list[str] = []
            for line in lines:
                m = _TS_BARE_ELEMENT_RE.match(line)
                if m and m.group(1) in def_imports \
                        and m.group(1) not in enumerated:
                    enumerated.append(m.group(1))
            if len(enumerated) >= 2:
                for ident in enumerated:
                    spec = def_imports[ident]
                    nk = _ts_norm(spec)
                    target = ts_index.get(nk) if nk else None
                    if target and target != rel:
                        # KEY = the entry's own dir segment
                        # ('./quick-books/config-client' -> quick-books).
                        segs = [x for x in spec.split("/")
                                if x not in (".", "..", "")]
                        key = segs[0] if segs else ""
                        out.append(RegistryTarget(
                            registry_file=rel, key=key, symbol="",
                            target_file=target,
                        ))
    return sorted(set(out), key=lambda t: (
        t.registry_file, t.target_file, t.symbol, t.key,
    ))


# ── seed minting + B34-b registry rails ─────────────────────────────────

#: UI component extensions — rail 1 applies only to JSX component files.
_UI_EXT = (".tsx", ".jsx")

#: Convention-marker file stems that carry no capability meaning (mirror
#: of ``flow_display_name._humanize_file_basename``'s marker set) — the
#: base name walks up to the nearest meaningful path segments instead
#: (``<app>/api/index.ts`` → ``<app>-api``, never ``index``).
_BASE_STEM_MARKERS = frozenset({
    "index", "route", "page", "layout", "handler", "default", "main",
    "+page", "+server", "_app", "_document", "middleware",
})


def _target_kind(target: RegistryTarget) -> str:
    """Rail-1 grouping key: the declared symbol, else the file stem —
    "which KIND of module is this registry entry" (``Setup``,
    ``EventTypeAppCardInterface``, ``CalendarService``…)."""
    if target.symbol:
        return target.symbol
    stem = target.target_file.rsplit("/", 1)[-1]
    return re.sub(r"\.[A-Za-z0-9]+$", "", stem)


def _seed_core(target: RegistryTarget) -> str:
    """Kebab core (no ``run-`` prefix, no ``-flow`` suffix) for a target.

    Symbol first (B30 machinery); for symbol-less targets the file stem —
    and when the stem is a convention marker, up to two meaningful
    trailing path segments (``packages/app-store/alby/api/index.ts`` →
    ``alby-api``)."""
    if target.symbol:
        base = _symbol_name(target.symbol)
        if base:
            return base[: -len("-flow")]
    parts = target.target_file.replace("\\", "/").split("/")
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", parts[-1])
    if stem and stem not in _BASE_STEM_MARKERS:
        # Meaningful stem — pre-rail behavior, byte-stable names.
        return _slugify(stem)
    # Convention-marker stem (<app>/api/index.ts) — up to two meaningful
    # trailing directory segments instead ("alby-api", never "index").
    meaningful = [
        s for s in parts[:-1] if s and s not in _BASE_STEM_MARKERS
    ]
    if not meaningful:
        return ""
    return _slugify("-".join(meaningful[-2:]))


def _seed_name(target: RegistryTarget) -> str:
    """``run-<core>-flow``; empty when nothing honest to name."""
    core = _seed_core(target)
    if not core:
        return ""
    if core.startswith("run-"):
        return f"{core}-flow"
    return f"run-{core}-flow"


def _registry_group_token(
    target: RegistryTarget,
    group_targets: list[RegistryTarget],
) -> str:
    """Rail-2 qualifier token for ``target`` within its registry group.

    The registry's own declared KEY is the maintainer's name for the
    entry (the app slug in cal.com's generated maps, the vendor string
    in a branch factory) — use it when present. Fallback: the first
    path segment where this target diverges from its registry siblings
    (``packages/app-store/<app>/…`` → the app dir)."""
    if target.key:
        tok = _slugify(target.key)
        if tok:
            return tok
    paths = sorted({t.target_file for t in group_targets})
    if len(paths) < 2:
        return ""
    own = target.target_file.split("/")
    common = 0
    while all(
        len(p.split("/")) > common and p.split("/")[common] == own[common]
        for p in paths
    ) and common < len(own) - 1:
        common += 1
    return _slugify(own[common]) if common < len(own) else ""


def _apply_registry_rails(
    targets: list[RegistryTarget],
) -> tuple[list[RegistryTarget], dict[str, str], int]:
    """B34-b rails over the detected target set.

    Rail 1 — *UI micro-component skip*: within ONE registry, a kind
    (symbol / file stem) declared for >=2 DISTINCT component files
    (``.tsx``/``.jsx``) is a per-entry UI wrapper (cal.com's
    ``EventTypeAppCardInterface`` x26, per-app ``Setup.tsx``) — interior
    of the entry's card, not a capability. Those targets are NOT minted
    (not laned — simply skipped). Unique-kind components and ALL
    non-component (server) targets pass.

    Rail 2 — *registry-key qualifier BEFORE ordinals*: when >=2 distinct
    surviving target FILES share one base name (cal.com's
    ``CalendarService`` x11, ``<app>/api/index.ts`` x85), every such
    target's name is qualified with its registry-declared key / app-dir
    token (``run-zoom-calendar-service-flow``), so downstream
    disambiguation never falls to ordinals. Unique base names are left
    untouched — Soc0's factory mints stay byte-identical.

    Returns (kept_targets, {target_file: final_core}, ui_skipped).
    """
    kind_files: dict[tuple[str, str], set[str]] = {}
    for t in targets:
        kind_files.setdefault(
            (t.registry_file, _target_kind(t)), set(),
        ).add(t.target_file)

    kept: list[RegistryTarget] = []
    ui_skipped = 0
    for t in targets:
        # Rail 1: repeated component KIND across one registry's entries
        # (cal.com EventTypeAppCardInterface x26, per-app Setup.tsx).
        if t.target_file.endswith(_UI_EXT) and len(
            kind_files[(t.registry_file, _target_kind(t))],
        ) >= 2:
            ui_skipped += 1
            continue
        # Rail 3 (B34-b keyed-supabase evidence, 328 hollow demo widgets):
        # a SYMBOL-LESS map entry pointing at a JSX component file is a
        # RENDER-catalog entry (design-system `__registry__` demos, icon
        # catalogs, lazy page chunks) — components render, capabilities
        # are invoked. Never mint.
        if not t.symbol and t.target_file.endswith(_UI_EXT):
            ui_skipped += 1
            continue
        kept.append(t)

    # One name decision per DISTINCT target file (only one seed can ever
    # mint per file) — first target in deterministic order wins.
    by_file: dict[str, RegistryTarget] = {}
    for t in kept:
        by_file.setdefault(t.target_file, t)
    by_registry: dict[str, list[RegistryTarget]] = {}
    for t in by_file.values():
        by_registry.setdefault(t.registry_file, []).append(t)

    base_files: dict[str, set[str]] = {}
    for t in by_file.values():
        core = _seed_core(t)
        if core:
            base_files.setdefault(core, set()).add(t.target_file)

    cores: dict[str, str] = {}
    for t in by_file.values():
        core = _seed_core(t)
        if not core:
            continue
        if len(base_files[core]) >= 2:
            tok = _registry_group_token(t, by_registry[t.registry_file])
            if tok:
                core_tokens = core.split("-")
                kept_tok = [
                    x for x in tok.split("-") if x not in core_tokens
                ]
                if kept_tok:
                    core = "-".join([*kept_tok, core])
        cores[t.target_file] = core
    return kept, cores, ui_skipped


def mint_dispatch_seeds(
    features_with_flows: list["FeatureWithFlows"],
    targets: list[RegistryTarget],
    repo_path: Path | str,
) -> dict[str, Any]:
    """Append one FlowSpec per uncovered registry target, IN PLACE."""
    from faultline.pipeline_v2.stage_3_flows import FlowSpec
    from faultline.analyzer.ast_extractor import extract_signatures
    from faultline.pipeline_v2.profiles._flow_lines import resolve_handler_line

    covered: set[str] = set()
    owner_of: dict[str, Any] = {}
    for fwf in features_with_flows:
        for fl in fwf.flows:
            if fl.entry_point_file:
                covered.add(fl.entry_point_file)
            covered.update(fl.reach_paths or ())
        for p in getattr(fwf.feature, "paths", None) or []:
            owner_of.setdefault(str(p), fwf)

    tele: dict[str, Any] = {
        "registries": sorted({t.registry_file for t in targets}),
        "targets_total": len(targets),
        "minted": 0,
        "skipped_covered": 0,
        "skipped_no_owner": 0,
        "skipped_unnameable": 0,
        "skipped_no_anchor": 0,
        "skipped_ui_component_kind": 0,
        "qualified_by_registry_key": 0,
        "ordinal_fallback": 0,
        "seeds": [],
    }
    # B34-b rails: UI micro-component kinds skipped; duplicated base
    # names pre-qualified with the registry's own key/app token.
    targets, rail_cores, ui_skipped = _apply_registry_rails(targets)
    tele["skipped_ui_component_kind"] = ui_skipped

    seen: set[tuple[str, str]] = set()
    for t in targets:
        if t.target_file in covered:
            tele["skipped_covered"] += 1
            continue
        owner = owner_of.get(t.target_file)
        if owner is None:
            tele["skipped_no_owner"] += 1
            continue
        core = rail_cores.get(t.target_file, "")
        if not core:
            tele["skipped_unnameable"] += 1
            continue
        # Anchor resolution (hollow guard, B34-b): a seed without a
        # symbol anchors NO spans downstream — flow-loc stamps 0/0 and
        # the board shows a hollow row (the keyed-supabase
        # obstacle-course FAIL class: 328 rows). Registry-declared
        # symbols anchor directly; symbol-less map targets resolve
        # their DOMINANT exported symbol (first export, source order);
        # a target with no resolvable export is NOT minted — no
        # anchor, no flow.
        sigs = extract_signatures([t.target_file], str(repo_path))
        sig = sigs.get(t.target_file)
        symbol = t.symbol
        if not symbol and sig is not None and sig.exports:
            symbol = sig.exports[0]
        entry_line: int | None = None
        if symbol and sig is not None:
            start = next(
                (r.start_line for r in sig.symbol_ranges
                 if r.name == symbol),
                None,
            )
            if start is not None:
                entry_line = resolve_handler_line(sig, symbol, start)
        if not symbol or entry_line is None:
            # No (symbol, line) anchor -> downstream span stamping has
            # nothing to ground on and the mint WOULD be a hollow row
            # (loc 0/0 -- the keyed-supabase gauntlet FAIL class). The
            # residual exhibits are junk anyway (re-exported `dayjs`,
            # `cn` utils, private py wrappers whose range the extractor
            # cannot see). No anchor, no flow.
            tele["skipped_no_anchor"] += 1
            continue

        if _seed_core(t) != core:
            tele["qualified_by_registry_key"] += 1
        name = core if core.startswith("run-") else f"run-{core}"
        name = f"{name}-flow"
        key = (owner.feature.name, name)
        if key in seen:
            # Same feature already minted this name for ANOTHER file —
            # last-resort ordinal (never expected on the rails corpus).
            ordinal = 2
            base = name[: -len("-flow")]
            while (owner.feature.name, f"{base}-{ordinal}-flow") in seen:
                ordinal += 1
            name = f"{base}-{ordinal}-flow"
            key = (owner.feature.name, name)
            tele["ordinal_fallback"] += 1
        seen.add(key)

        owner.flows.append(FlowSpec(
            name=name,
            description=(
                f"dispatch registry {t.registry_file}"
                + (f" ['{t.key}']" if t.key else "")
            ),
            entry_point_file=t.target_file,
            entry_point_line=entry_line,
            symbol_names=[symbol],
        ))
        covered.add(t.target_file)  # one seed per target file
        tele["minted"] += 1
        if len(tele["seeds"]) < 30:
            tele["seeds"].append({
                "feature": owner.feature.name, "name": name,
                "target": t.target_file, "registry": t.registry_file,
            })
    return tele


def run_dispatch_registry_stage(
    features_with_flows: list["FeatureWithFlows"],
    ctx: Any,
    lazy_edges: list[LazyImportEdge],
) -> dict[str, Any]:
    """Detect registries and mint seeds; returns artifact telemetry."""
    tracked = [str(f) for f in getattr(ctx, "tracked_files", []) or []]
    py_targets = detect_py_registries(ctx.repo_path, lazy_edges)
    ts_targets = detect_ts_registries(ctx.repo_path, tracked)
    tele = mint_dispatch_seeds(
        features_with_flows, [*py_targets, *ts_targets], ctx.repo_path,
    )
    tele["py_targets"] = len(py_targets)
    tele["ts_targets"] = len(ts_targets)
    return tele
