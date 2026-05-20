"""Stage 6.6 — Branch Slicer (Sprint D2, deterministic).

Why this stage exists
=====================

After Sprint C3/C4, every developer feature has rich per-symbol
attributions (function reach via import graph + framework linker
cross-file edges). What it does NOT have yet: **intra-symbol
conditional regions**.

Consider::

    function CreateUserPage() {
      if (isLoading) return <Spinner />        // L2-L2
      if (role === 'admin') return <AdminForm />   // L3-L3
      if (role === 'user')  return <UserForm />    // L4-L4
      return null
    }

C3/C4 attribute ``CreateUserPage L1-L6`` as a single block. A PR that
only touches the ``role === 'admin'`` branch is indistinguishable
from a PR that rewrites the whole component. Conditional-aware PR
overlap analysis ("did this PR touch the admin path specifically?")
and per-branch test-coverage require finer-grained slices.

Stage 6.6 walks every (file, parent_symbol, line_start, line_end)
attribution already on a feature, parses the file with tree-sitter,
and emits one extra :class:`FlowSymbolAttribution` per conditional
region found inside the parent's body. Role = ``branch``, line
range = the AST sub-node, ``symbol`` encodes ``branch:<kind>:<parent>__b<i>::<cond>``.

Determinism + cost
==================

Pure file IO + tree-sitter AST walk. **NO LLM**. **NO network**. No
mutation of any feature field except ``symbol_attributions``.

Tree-sitter is an **optional** dependency (``pip install
faultlines[ast]``). When the import fails, :func:`is_active` returns
``False`` and :func:`run_stage_6_6` writes a telemetry record saying
so — the rest of the pipeline is unaffected. This matches the
graceful-degradation pattern used by [[unknown-stack-handler]].

Algorithm
=========

For every feature × every ``symbol_attribution`` whose role ∈
``{entry, called}`` and whose line range covers > 1 LOC:

1. Resolve the absolute path; skip if it's not a file we recognise
   (extension not in :data:`_LANG_BY_EXT`).
2. Acquire / build a per-language :class:`tree_sitter.Parser`
   (lazy, cached process-wide).
3. Parse the file once per scan; cache the result keyed by mtime so
   the second attribution into the same file reuses the tree.
4. Descend the AST. For each node whose type is in the
   per-language **branch node set**:
     * skip if it lies outside the parent symbol's line range
     * skip if its body spans < 2 lines (too small to be meaningful)
     * extract a one-line ``condition_text`` from the test/condition
       sub-node (falls back to the empty string when none is
       semantically reasonable, e.g. ``finally`` / ``else``)
     * stamp a :class:`BranchSlice` with stable kind name + 1-indexed
       inclusive line range
5. Cap per parent symbol at :data:`_MAX_BRANCHES_PER_SYMBOL` to keep
   monster functions from blowing telemetry up.

Telemetry
=========

``scan_meta.stage_6_6`` is the canonical telemetry surface. The
stage artifact (``06-stage-branch_slicer.json``) carries the same
payload plus per-feature counts for replay debugging.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.models.types import FlowSymbolAttribution

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Optional tree-sitter import ────────────────────────────────────────────

try:  # pragma: no cover — import-time branch
    from tree_sitter import Language, Parser

    TREE_SITTER_AVAILABLE = True
except Exception:  # noqa: BLE001 — any failure → degrade gracefully
    TREE_SITTER_AVAILABLE = False
    Language = None  # type: ignore[assignment]
    Parser = None  # type: ignore[assignment]


# Per-language module probes. Each lazily reports the language()
# capsule that the Parser will bind to. Missing modules => language
# disabled for the scan (other languages still work).
_LANG_PROBES: dict[str, Any] = {}


def _probe_language(name: str) -> Any | None:
    """Return a tree-sitter Language capsule for ``name`` or None.

    Catches ImportError so a partial extras install (e.g. ts-only)
    still works for the languages that are present.
    """
    if not TREE_SITTER_AVAILABLE:
        return None
    if name in _LANG_PROBES:
        return _LANG_PROBES[name]
    try:
        if name == "typescript":
            import tree_sitter_typescript as mod

            lang = Language(mod.language_typescript())
        elif name == "tsx":
            import tree_sitter_typescript as mod

            lang = Language(mod.language_tsx())
        elif name == "javascript":
            import tree_sitter_javascript as mod

            lang = Language(mod.language())
        elif name == "python":
            import tree_sitter_python as mod

            lang = Language(mod.language())
        elif name == "go":
            import tree_sitter_go as mod

            lang = Language(mod.language())
        elif name == "rust":
            import tree_sitter_rust as mod

            lang = Language(mod.language())
        else:
            lang = None
    except Exception as exc:  # noqa: BLE001 — non-fatal
        logger.debug(
            "stage_6_6: tree-sitter language %s not loadable: %s", name, exc,
        )
        lang = None
    _LANG_PROBES[name] = lang
    return lang


# File-extension → tree-sitter language name. Every extension in this
# table is one the slicer knows how to parse; everything else is a
# silent no-op (slice_branches returns []).
_LANG_BY_EXT: dict[str, str] = {
    "ts": "typescript",
    "tsx": "tsx",
    "js": "javascript",
    "jsx": "javascript",   # JSX in .jsx uses the JavaScript grammar
    "mjs": "javascript",
    "cjs": "javascript",
    "py": "python",
    "go": "go",
    "rs": "rust",
}


# Per-language branch node-type sets. Each entry: AST node type →
# canonical kind name (matches the spec branch_kind enum).
#
# Rationale per language:
#   - ts/tsx/js: ``if_statement`` covers both the if-block and any
#     ``else_clause`` siblings; the slicer captures the else-arm by
#     walking the alternative child. ``ternary_expression`` is
#     surfaced when its true/false arms span any non-trivial work.
#   - py: ``elif_clause`` is a child of ``if_statement``;
#     ``conditional_expression`` is the ternary equivalent.
#   - go: ``expression_switch_statement`` and ``type_switch_statement``
#     both carry ``expression_case`` + ``default_case`` children.
#   - rust: ``if_expression`` chains via ``else_clause → if_expression``;
#     ``match_expression`` carries ``match_arm`` children.
_BRANCH_NODES: dict[str, dict[str, str]] = {
    "typescript": {
        "if_statement": "if",
        "ternary_expression": "ternary",
        "switch_statement": "switch",
        "switch_case": "switch_case",
        "switch_default": "switch_default",
        "try_statement": "try",
        "catch_clause": "catch",
        "finally_clause": "finally",
    },
    "tsx": {
        "if_statement": "if",
        "ternary_expression": "ternary",
        "switch_statement": "switch",
        "switch_case": "switch_case",
        "switch_default": "switch_default",
        "try_statement": "try",
        "catch_clause": "catch",
        "finally_clause": "finally",
    },
    "javascript": {
        "if_statement": "if",
        "ternary_expression": "ternary",
        "switch_statement": "switch",
        "switch_case": "switch_case",
        "switch_default": "switch_default",
        "try_statement": "try",
        "catch_clause": "catch",
        "finally_clause": "finally",
    },
    "python": {
        "if_statement": "if",
        "elif_clause": "elif",
        "else_clause": "else",
        "conditional_expression": "ternary",
        "try_statement": "try",
        "except_clause": "catch",
        "finally_clause": "finally",
        "match_statement": "match",
        "case_clause": "match_arm",
    },
    "go": {
        "if_statement": "if",
        "expression_switch_statement": "switch",
        "type_switch_statement": "switch",
        "expression_case": "switch_case",
        "type_case": "switch_case",
        "default_case": "switch_default",
        "select_statement": "select",
        "communication_case": "switch_case",
    },
    "rust": {
        "if_expression": "if",
        "match_expression": "match",
        "match_arm": "match_arm",
    },
}


# ── Tunables (universal, not corpus-tuned) ─────────────────────────────────

# Drop trivial-body branches. A 1-line "if (x) return;" is structurally
# present but provides no useful overlap signal — every PR that touches
# the function will also touch it. Universal threshold (not per-stack).
_MIN_BRANCH_LINES: int = 2

# Defensive cap against monstrous functions (think 1000-line reducers).
# 20 is generous: a function with > 20 branches is itself a refactor
# target and individual branches lose meaning anyway.
_MAX_BRANCHES_PER_SYMBOL: int = 20

# Roles that get sliced. Other roles (support, anchor-consumer, ...)
# either span whole files or refer to non-symbol surfaces — slicing
# them is meaningless.
_SLICEABLE_ROLES: frozenset[str] = frozenset({"entry", "called"})


# ── Parser + parse-tree caches (process-scoped) ────────────────────────────

_PARSERS: dict[str, Any] = {}
_TREE_CACHE: dict[tuple[str, int], Any] = {}
_SOURCE_CACHE: dict[tuple[str, int], bytes] = {}


def _get_parser(lang_name: str) -> Any | None:
    """Lazy parser cache."""
    if not TREE_SITTER_AVAILABLE:
        return None
    if lang_name in _PARSERS:
        return _PARSERS[lang_name]
    lang = _probe_language(lang_name)
    if lang is None:
        _PARSERS[lang_name] = None
        return None
    try:
        parser = Parser(lang)
    except Exception as exc:  # noqa: BLE001 — never fatal
        logger.warning(
            "stage_6_6: failed to construct Parser for %s: %s", lang_name, exc,
        )
        _PARSERS[lang_name] = None
        return None
    _PARSERS[lang_name] = parser
    return parser


def _parse_file(path: Path, lang_name: str) -> tuple[Any | None, bytes | None]:
    """Parse ``path`` with ``lang_name`` parser. Caches by (path, mtime).

    Returns (tree, source_bytes) or (None, None) on any failure.
    """
    parser = _get_parser(lang_name)
    if parser is None:
        return None, None
    try:
        st = path.stat()
    except OSError:
        return None, None
    cache_key = (str(path), int(st.st_mtime))
    if cache_key in _TREE_CACHE:
        return _TREE_CACHE[cache_key], _SOURCE_CACHE[cache_key]
    try:
        src = path.read_bytes()
    except OSError as exc:
        logger.debug("stage_6_6: cannot read %s: %s", path, exc)
        return None, None
    if not src:
        return None, None
    try:
        tree = parser.parse(src)
    except Exception as exc:  # noqa: BLE001 — non-fatal
        logger.debug("stage_6_6: parse failed for %s: %s", path, exc)
        return None, None
    _TREE_CACHE[cache_key] = tree
    _SOURCE_CACHE[cache_key] = src
    return tree, src


def reset_caches() -> None:
    """Clear parser + parse-tree caches (test helper)."""
    _PARSERS.clear()
    _TREE_CACHE.clear()
    _SOURCE_CACHE.clear()
    _LANG_PROBES.clear()


# ── Public types ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BranchSlice:
    """One intra-symbol conditional region extracted by Stage 6.6."""

    file: str               # repo-relative path (matches input attribution)
    parent_symbol: str      # the function this branch belongs to
    branch_kind: str        # if | else | elif | ternary | switch_case | ...
    condition_text: str     # gating expression, trimmed; empty for else/finally
    line_start: int         # 1-indexed inclusive
    line_end: int           # 1-indexed inclusive

    def as_attribution(self, *, index: int) -> FlowSymbolAttribution:
        """Project this slice into the shared symbol-attribution shape.

        Symbol field is ``branch:<kind>:<parent>__b<index>::<cond>``
        so downstream consumers can route on the kind without growing
        the schema. The condition is truncated to keep the symbol
        string under a reasonable display length.
        """
        cond = (self.condition_text or "").strip()
        if len(cond) > 80:
            cond = cond[:77] + "..."
        suffix = f"::{cond}" if cond else ""
        symbol = (
            f"branch:{self.branch_kind}:{self.parent_symbol}__b{index}{suffix}"
        )
        return FlowSymbolAttribution(
            file=self.file,
            symbol=symbol,
            line_start=self.line_start,
            line_end=self.line_end,
            role="branch",
        )


@dataclass
class StageResult:
    """Outcome of one Stage 6.6 run."""

    active: bool
    reason: str = ""                       # set when active=False
    tree_sitter_version: str = ""
    symbols_analyzed: int = 0
    branches_emitted: int = 0
    branch_kinds: dict[str, int] = field(default_factory=dict)
    languages_seen: dict[str, int] = field(default_factory=dict)
    capped_symbols: int = 0
    parse_failures: int = 0
    sample_slices: list[dict[str, Any]] = field(default_factory=list)
    elapsed_sec: float = 0.0

    def telemetry(self) -> dict[str, Any]:
        if not self.active:
            return {"active": False, "reason": self.reason}
        avg = (
            round(self.branches_emitted / self.symbols_analyzed, 3)
            if self.symbols_analyzed > 0
            else 0.0
        )
        return {
            "active": True,
            "tree_sitter_version": self.tree_sitter_version,
            "symbols_analyzed": self.symbols_analyzed,
            "branches_emitted": self.branches_emitted,
            "branch_kinds": dict(sorted(self.branch_kinds.items())),
            "languages_seen": dict(sorted(self.languages_seen.items())),
            "avg_branches_per_symbol": avg,
            "capped_symbols": self.capped_symbols,
            "parse_failures": self.parse_failures,
            "sample_slices": list(self.sample_slices),
            "elapsed_sec": round(self.elapsed_sec, 3),
        }


# ── Public API ─────────────────────────────────────────────────────────────


def is_active(ctx: "ScanContext | None" = None) -> bool:
    """Whether the slicer can run.

    True only when:
      1. tree-sitter is importable, AND
      2. at least ONE language binding is loadable.

    ``ctx`` is accepted for symmetry with other ``is_active`` hooks
    in the pipeline but is currently unused.
    """
    if not TREE_SITTER_AVAILABLE:
        return False
    for name in {"typescript", "tsx", "javascript", "python", "go", "rust"}:
        if _probe_language(name) is not None:
            return True
    return False


def _ext_of(path: str) -> str:
    """Return the lower-case extension of ``path`` without the dot."""
    p = path.rsplit(".", 1)
    return p[1].lower() if len(p) == 2 else ""


def _condition_text(
    node: Any, src: bytes, *, kind: str,
) -> str:
    """Extract a one-line condition snippet for ``node``.

    For ``if``/``elif``/``while``/``ternary`` we look for the
    canonical ``condition`` / ``test`` field. For ``switch_case`` we
    grab the value. For ``catch`` we grab the parameter. For
    ``else``/``finally``/``default`` there is no condition; we return
    "" so downstream consumers don't show a misleading label.
    """
    if kind in {"else", "finally", "switch_default"}:
        return ""
    # Prefer the explicit ``condition`` / ``value`` field where the
    # grammar exposes one; this is faster + more robust than walking
    # all children.
    for field_name in ("condition", "value", "test"):
        try:
            child = node.child_by_field_name(field_name)
        except Exception:  # noqa: BLE001 — grammars without field-name API
            child = None
        if child is not None:
            text = src[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace",
            )
            return _normalise_one_line(text)
    # Fallback: walk the immediate children for a likely-condition node.
    likely = {
        "parenthesized_expression",
        "binary_expression",
        "identifier",
        "member_expression",
        "call_expression",
        "comparison_operator",
        "boolean_operator",
        "match_pattern",
        "case_pattern",
        "expression_list",
    }
    for child in node.children:
        if child.type in likely:
            text = src[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace",
            )
            return _normalise_one_line(text)
    return ""


def _normalise_one_line(text: str) -> str:
    """Strip outer parens + collapse whitespace to a single line."""
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    text = " ".join(text.split())
    return text


def slice_branches(
    ctx: "ScanContext",
    file_path: str,
    parent_symbol: str,
    parent_line_start: int,
    parent_line_end: int,
) -> list[BranchSlice]:
    """Slice the body of ``parent_symbol`` into conditional regions.

    Args:
        ctx: scan context — used to resolve ``file_path`` against the
            repo root and to gate by stack if needed.
        file_path: repo-relative path of the source file.
        parent_symbol: display name of the enclosing symbol (purely
            informational — used to label slices).
        parent_line_start: 1-indexed inclusive start of the parent
            symbol body.
        parent_line_end: 1-indexed inclusive end of the parent symbol
            body.

    Returns:
        Zero or more :class:`BranchSlice` instances, in source order,
        capped at :data:`_MAX_BRANCHES_PER_SYMBOL`. Returns ``[]``
        gracefully when tree-sitter is unavailable, the file doesn't
        exist, the extension isn't supported, parsing fails, or no
        branches were found.
    """
    if not TREE_SITTER_AVAILABLE:
        return []
    ext = _ext_of(file_path)
    lang_name = _LANG_BY_EXT.get(ext)
    if lang_name is None:
        return []
    branch_nodes = _BRANCH_NODES.get(lang_name, {})
    if not branch_nodes:
        return []

    repo_root = Path(getattr(ctx, "repo_path", Path.cwd()))
    abs_path = (repo_root / file_path).resolve()
    if not abs_path.is_file():
        return []

    tree, src = _parse_file(abs_path, lang_name)
    if tree is None or src is None:
        return []

    # Inclusive range tightening. Some attributions span whole files
    # (line_end == LOC); we still honour the bounds and skip nodes
    # outside them.
    if parent_line_end < parent_line_start:
        return []

    slices: list[BranchSlice] = []

    def visit(node: Any) -> None:
        if len(slices) >= _MAX_BRANCHES_PER_SYMBOL:
            return
        # tree-sitter reports 0-indexed rows; convert to 1-indexed.
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        # Skip nodes entirely outside parent symbol body.
        if end < parent_line_start or start > parent_line_end:
            return
        kind = branch_nodes.get(node.type)
        if kind is not None:
            # Clip to parent bounds (defensive when AST node extends
            # beyond the recorded function range due to attribute
            # mis-attribution upstream).
            clipped_start = max(start, parent_line_start)
            clipped_end = min(end, parent_line_end)
            if clipped_end - clipped_start + 1 >= _MIN_BRANCH_LINES:
                cond = _condition_text(node, src, kind=kind)
                slices.append(BranchSlice(
                    file=file_path,
                    parent_symbol=parent_symbol,
                    branch_kind=kind,
                    condition_text=cond,
                    line_start=clipped_start,
                    line_end=clipped_end,
                ))
                if len(slices) >= _MAX_BRANCHES_PER_SYMBOL:
                    return
        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return slices


def _tree_sitter_version() -> str:
    """Best-effort version string. ``tree-sitter`` >=0.23 doesn't expose
    ``__version__`` on the module so we fall back to importlib metadata."""
    if not TREE_SITTER_AVAILABLE:
        return ""
    try:
        import tree_sitter as _ts

        v = getattr(_ts, "__version__", "")
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    try:
        import importlib.metadata as _md

        return _md.version("tree-sitter")
    except Exception:  # noqa: BLE001
        return ""


def _key_for(attr: FlowSymbolAttribution) -> tuple[str, str, int, int]:
    return (attr.file, attr.symbol, attr.line_start, attr.line_end)


def _slice_attribution_list(
    ctx: "ScanContext",
    attrs: list[FlowSymbolAttribution],
    *,
    owner_label: str,
    result: StageResult,
    log: "StageLogger",
    sample_target: int,
) -> list[FlowSymbolAttribution]:
    """Slice every eligible attribution in ``attrs``; return the **new**
    branch-role attributions to append.

    Args:
        ctx: scan context.
        attrs: snapshot of the owner's existing attribution list.
        owner_label: free-form label used for the sample-slices payload
            (``feature:<name>`` or ``flow:<feature>/<flow>``) so
            downstream replay debuggers can tell where the slice came
            from.
        result: shared accumulator (updated in place).
        log: stage logger.
        sample_target: max number of sample slices in telemetry.

    Returns:
        New :class:`FlowSymbolAttribution` records (deduped against the
        existing keys passed in via ``attrs``). Caller appends to its
        own list.
    """
    if not attrs:
        return []
    existing_keys: set[tuple[str, str, int, int]] = {
        _key_for(a) for a in attrs
    }
    new_attrs: list[FlowSymbolAttribution] = []

    for attr in attrs:
        if attr.role not in _SLICEABLE_ROLES:
            continue
        if (attr.line_end - attr.line_start + 1) < _MIN_BRANCH_LINES:
            continue
        ext = _ext_of(attr.file)
        if ext not in _LANG_BY_EXT:
            continue
        lang_name = _LANG_BY_EXT[ext]
        result.languages_seen[lang_name] = (
            result.languages_seen.get(lang_name, 0) + 1
        )
        result.symbols_analyzed += 1

        try:
            slices = slice_branches(
                ctx,
                file_path=attr.file,
                parent_symbol=attr.symbol,
                parent_line_start=attr.line_start,
                parent_line_end=attr.line_end,
            )
        except Exception as exc:  # noqa: BLE001
            # Tree-sitter parse failures must never break a scan.
            result.parse_failures += 1
            log.warn(
                f"slice_branches raised on {attr.file}:{attr.symbol}: "
                f"{type(exc).__name__}: {exc}",
                feature=owner_label,
            )
            logger.debug(
                "stage_6_6.slice_branches raised", exc_info=True,
            )
            continue

        if len(slices) >= _MAX_BRANCHES_PER_SYMBOL:
            result.capped_symbols += 1

        for idx, sl in enumerate(slices):
            new_attr = sl.as_attribution(index=idx)
            key = _key_for(new_attr)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            new_attrs.append(new_attr)
            result.branches_emitted += 1
            result.branch_kinds[sl.branch_kind] = (
                result.branch_kinds.get(sl.branch_kind, 0) + 1
            )
            if len(result.sample_slices) < sample_target:
                result.sample_slices.append({
                    "file": sl.file,
                    "parent_symbol": sl.parent_symbol,
                    "branch_kind": sl.branch_kind,
                    "condition_text": sl.condition_text,
                    "line_start": sl.line_start,
                    "line_end": sl.line_end,
                    "owner": owner_label,
                })

    return new_attrs


def run_stage_6_6(
    ctx: "ScanContext",
    features: list["Feature"],
    log: "StageLogger",
) -> StageResult:
    """Walk every (feature × eligible symbol_attribution) AND every
    (flow × eligible flow_symbol_attribution) and emit intra-symbol
    branch slices.

    Both surfaces are sliced because:
      * JS/TS stacks populate ``feature.symbol_attributions`` via the
        Stage 6.3 import tree.
      * Non-JS stacks (Go, Python lib, Rust) populate ``flow.flow_symbol_attributions``
        via Stage 3 entry detection but leave the feature-level surface
        empty. Skipping flow-level would leave non-JS scans without any
        branch enrichment.

    Mutates the relevant attribution lists in place. Skips silently +
    records ``active=False`` when tree-sitter is unavailable.
    """
    t0 = time.monotonic()

    if not is_active(ctx):
        reason = (
            "tree-sitter not installed (pip install faultlines[ast])"
            if not TREE_SITTER_AVAILABLE
            else "no tree-sitter language bindings available"
        )
        log.info(f"stage_6_6 inactive: {reason}")
        return StageResult(active=False, reason=reason,
                           elapsed_sec=round(time.monotonic() - t0, 3))

    result = StageResult(
        active=True,
        tree_sitter_version=_tree_sitter_version(),
    )
    sample_target = 5

    for feature in features:
        # ── Feature-level attributions (JS/TS-shaped) ─────────────
        feat_existing = list(feature.symbol_attributions or [])
        if feat_existing:
            feat_new = _slice_attribution_list(
                ctx, feat_existing,
                owner_label=f"feature:{feature.name}",
                result=result, log=log, sample_target=sample_target,
            )
            if feat_new:
                feature.symbol_attributions = feat_existing + feat_new

        # ── Flow-level attributions (Go / Python-lib / Rust shape) ─
        for flow in feature.flows or []:
            flow_existing = list(
                getattr(flow, "flow_symbol_attributions", []) or []
            )
            if not flow_existing:
                continue
            flow_new = _slice_attribution_list(
                ctx, flow_existing,
                owner_label=f"flow:{feature.name}/{flow.name}",
                result=result, log=log, sample_target=sample_target,
            )
            if flow_new:
                flow.flow_symbol_attributions = flow_existing + flow_new

    result.elapsed_sec = round(time.monotonic() - t0, 3)
    log.info(
        "branch-slicer summary: "
        f"symbols_analyzed={result.symbols_analyzed} "
        f"branches_emitted={result.branches_emitted} "
        f"kinds={dict(result.branch_kinds)} "
        f"capped_symbols={result.capped_symbols} "
        f"parse_failures={result.parse_failures} "
        f"elapsed={result.elapsed_sec}s",
    )
    return result


__all__ = [
    "BranchSlice",
    "StageResult",
    "is_active",
    "reset_caches",
    "run_stage_6_6",
    "slice_branches",
]
