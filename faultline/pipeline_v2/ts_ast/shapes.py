"""W6-AST frozen data shapes (spec §1) — the SINGLE canonical module.

LAW (spec §2): determinism everywhere — every collection SORTED on
assembly, dicts built in key order, no set iteration reaches output.
``SymbolGraph.to_payload()`` is the serialisation contract used for the
CacheKind.AST cache and for byte-level diffing; two graphs built from
the same inputs MUST serialise to identical bytes.

Shape changes go through the coordinator ONLY (spec header law). M1-M3
import these shapes; they do not redefine them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "DefKind",
    "DefSpan",
    "ExportEntry",
    "ExportKind",
    "FileParse",
    "ImportEdge",
    "ImportKind",
    "Lang",
    "Resolution",
    "ResolvedEdge",
    "SymbolGraph",
    "WrapperKind",
]

Lang = Literal["ts", "tsx", "js", "jsx"]
# AMENDMENT-1 §1: +'enum' (M1 emits enum → 'enum', NEVER 'const';
# type/interface get NO DefSpan — they live in exports_index only).
DefKind = Literal["function", "class", "component", "method", "const", "enum"]
WrapperKind = Literal["none", "forwardRef", "memo", "hoc", "styled"]
ImportKind = Literal[
    "named", "default", "namespace", "dynamic", "require",
    "reexport_star", "reexport_named", "side_effect",
]
Resolution = Literal[
    "relative", "tsconfig_alias", "workspace", "package_external",
    "unresolved",
]
ExportKind = Literal["named", "default", "star_from"]


@dataclass
class FileParse:
    """One parsed file. NOT serialised (``tree`` is a live TSTree)."""

    path: str
    content_hash: str
    lang: Lang
    tree: Any  # tree_sitter.Tree — kept opaque here (M1 owns parsing)


@dataclass(frozen=True, order=True)
class DefSpan:
    """A symbol definition with its exact body line range (1-based incl.)."""

    file: str
    name: str
    kind: DefKind
    start_line: int
    end_line: int
    exported: bool
    wrapper: WrapperKind = "none"
    parent: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "end_line": self.end_line,
            "exported": self.exported,
            "file": self.file,
            "kind": self.kind,
            "name": self.name,
            "parent": self.parent,
            "start_line": self.start_line,
            "wrapper": self.wrapper,
        }


@dataclass(frozen=True, order=True)
class ImportEdge:
    """One raw import/export edge as written in source (M2 output).

    ``names`` carries the LOCAL binding names; type-only bindings are
    prefixed ``type:`` (spec §3 M2) — resolution keeps them, provenance
    consumers skip them.
    """

    src_file: str
    kind: ImportKind
    names: tuple[str, ...]
    raw_target: str
    line: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "line": self.line,
            "names": list(self.names),
            "raw_target": self.raw_target,
            "src_file": self.src_file,
        }


@dataclass(frozen=True, order=True)
class ResolvedEdge:
    """An import edge resolved to a repo file (or classified external)."""

    src_file: str
    raw_target: str
    target_file: str | None
    resolution: Resolution
    via_barrels: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    kind: str = "named"

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "names": list(self.names),
            "raw_target": self.raw_target,
            "resolution": self.resolution,
            "src_file": self.src_file,
            "target_file": self.target_file,
            "via_barrels": list(self.via_barrels),
        }


@dataclass(frozen=True, order=True)
class ExportEntry:
    """One exported name of a file (M2 output; barrels feed M3 descent)."""

    file: str
    name: str
    kind: ExportKind
    origin_file: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "kind": self.kind,
            "name": self.name,
            "origin_file": self.origin_file,
        }


def _sorted_telemetry(value: Any) -> Any:
    """Recursively key-sort dicts (lists keep caller order — callers sort)."""
    if isinstance(value, dict):
        return {k: _sorted_telemetry(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_sorted_telemetry(v) for v in value]
    return value


@dataclass
class SymbolGraph:
    """The assembled per-repo symbol graph (adapter output, spec §1)."""

    defs: list[DefSpan] = field(default_factory=list)
    edges: list[ImportEdge] = field(default_factory=list)
    resolved: list[ResolvedEdge] = field(default_factory=list)
    exports_index: dict[str, list[ExportEntry]] = field(default_factory=dict)
    telemetry: dict[str, Any] = field(default_factory=dict)

    def sort_canonical(self) -> None:
        """Impose the canonical ordering in place (idempotent)."""
        self.defs.sort(key=_def_sort_key)
        self.edges.sort(key=_edge_sort_key)
        self.resolved.sort(key=_resolved_sort_key)
        self.exports_index = {
            f: sorted(entries, key=_export_sort_key)
            for f, entries in sorted(self.exports_index.items())
        }

    def to_payload(self) -> dict[str, Any]:
        """JSON-able dict, keys sorted at every level (cache + diffs)."""
        self.sort_canonical()
        return {
            "defs": [d.to_payload() for d in self.defs],
            "edges": [e.to_payload() for e in self.edges],
            "exports_index": {
                f: [x.to_payload() for x in entries]
                for f, entries in self.exports_index.items()
            },
            "resolved": [r.to_payload() for r in self.resolved],
            "telemetry": _sorted_telemetry(self.telemetry),
        }


def _def_sort_key(d: DefSpan) -> tuple[str, int, int, str, str]:
    return (d.file, d.start_line, d.end_line, d.parent or "", d.name)


def _edge_sort_key(e: ImportEdge) -> tuple[str, int, str, str, tuple[str, ...]]:
    return (e.src_file, e.line, e.kind, e.raw_target, e.names)


def _resolved_sort_key(
    r: ResolvedEdge,
) -> tuple[str, str, str, str, tuple[str, ...]]:
    return (r.src_file, r.raw_target, r.kind, r.target_file or "", r.names)


def _export_sort_key(x: ExportEntry) -> tuple[str, str, str]:
    return (x.name, x.kind, x.origin_file or "")
