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
_TS_MAP_DYNAMIC_RE = re.compile(
    r"^\s*['\"]?([\w$-]+)['\"]?\s*:\s*.*?\bimport\(\s*['\"]([^'\"]+)['\"]",
)


def dispatch_registry_enabled() -> bool:
    """Default ON since the 2026-07-10 keyed Soc0 OFF/ON A/B (markers 3->1,
    +8 registry-bounded mints, validator 8->7, gauntlet CLEAR both sides).
    ``=0`` restores the pre-B34 board byte-identically."""
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
        if "switch" not in text and "import(" not in text:
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
    return sorted(set(out), key=lambda t: (
        t.registry_file, t.target_file, t.symbol, t.key,
    ))


# ── seed minting ────────────────────────────────────────────────────────


def _seed_name(target: RegistryTarget) -> str:
    """``run-<symbol>-flow`` via the B30 symbol machinery; file-stem
    fallback when the registry declared no symbol."""
    base = _symbol_name(target.symbol) if target.symbol else ""
    if not base:
        stem = target.target_file.rsplit("/", 1)[-1]
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", stem)
        slug = _slugify(stem)
        if not slug:
            return ""
        base = f"{slug}-flow"
    core = base[: -len("-flow")]
    if core.startswith("run-"):
        return base
    return f"run-{core}-flow"


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
        "seeds": [],
    }
    seen: set[tuple[str, str]] = set()
    for t in targets:
        if t.target_file in covered:
            tele["skipped_covered"] += 1
            continue
        owner = owner_of.get(t.target_file)
        if owner is None:
            tele["skipped_no_owner"] += 1
            continue
        name = _seed_name(t)
        if not name:
            tele["skipped_unnameable"] += 1
            continue
        key = (owner.feature.name, name)
        if key in seen:
            continue
        seen.add(key)

        entry_line: int | None = None
        if t.symbol:
            sigs = extract_signatures([t.target_file], str(repo_path))
            sig = sigs.get(t.target_file)
            if sig is not None:
                start = next(
                    (r.start_line for r in sig.symbol_ranges
                     if r.name == t.symbol),
                    None,
                )
                if start is not None:
                    entry_line = resolve_handler_line(sig, t.symbol, start)

        owner.flows.append(FlowSpec(
            name=name,
            description=(
                f"dispatch registry {t.registry_file}"
                + (f" ['{t.key}']" if t.key else "")
            ),
            entry_point_file=t.target_file,
            entry_point_line=entry_line,
            symbol_names=[t.symbol] if t.symbol else [],
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
