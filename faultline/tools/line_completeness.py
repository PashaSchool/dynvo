"""Line-completeness audit — WS2 of the deterministic-foundation program.

Given a repo tree + one scan JSON, measures how much of the codebase the
dev-grain layer actually accounts for, "to the millimeter":

* **attribution completeness** — executable (non-blank / non-comment)
  LOC in source files attributed to >=1 developer feature
  (``developer_features[].paths``) over total executable LOC; plus the
  top unattributed directories by LOC.
* **flow-span completeness** — executable LOC inside >=1 dev-flow
  symbol span (``flows[].line_ranges``) over total executable LOC;
  flow-LOC size distribution (p10/p50/p90); degenerate flows
  (<=2 raw span LOC — the wrapper-entry symptom class).
* **entry coverage** — ``routes_index[]`` entries whose file is not the
  ``entry_point_file`` of any flow (the Soc0/main.py class: route
  detected, no flow built from it).
* **graph reach** — % of graph-language source files reachable from any
  flow entry file via forward BFS over
  :func:`faultline.analyzer.symbol_graph.build_symbol_graph`; the
  unreached list (top by LOC) surfaces DI / dynamic-dispatch suspects.

Tracked-not-gated: this module REPORTS numbers, it enforces nothing.
Gates come only after the WS3 parser decision (see
``docs/specs`` deterministic-foundation program in the app repo).

Deterministic, $0, no LLM, no network. The repo tree is read-only.

CLI::

    python -m faultline.tools.line_completeness \
        --repo <path> --scan <scan.json> [--json out.json]

Executable-LOC counting is a per-language-family line scanner:

* hash family (``.py`` / ``.rb`` / ``.sh`` …) — a line is executable
  unless blank or its first non-space char starts a ``#`` comment.
  Python triple-quoted docstrings count as CODE (deterministic,
  documented approximation — they are string expressions).
* C family (TS/JS/Go/Rust/Java/…) — ``//`` line comments and
  non-nested ``/* … */`` block comments are stripped with a small
  state machine; whatever code remains on the line makes it
  executable. Comment markers inside string literals are NOT
  string-aware (documented approximation; verdicts stay correct for
  the common ``"https://…"`` shapes because code precedes the marker).
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable

from faultline.pipeline_v2.stage_6_9b_generated_strip import is_generated_path

# ── Language table ──────────────────────────────────────────────────

#: Extensions counted as source code, per comment-syntax family.
HASH_FAMILY_EXTS = {
    ".py", ".rb", ".sh", ".bash", ".zsh", ".pl", ".r", ".jl",
    ".ex", ".exs",
}
C_FAMILY_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
    ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".swift", ".scala",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm", ".dart",
    ".vue", ".svelte",
}
#: PHP allows both ``//`` and ``#`` line comments.
PHP_EXTS = {".php"}

SOURCE_EXTS = HASH_FAMILY_EXTS | C_FAMILY_EXTS | PHP_EXTS

#: Extensions the symbol graph can actually parse edges for — the
#: graph-reach denominator is restricted to these so the metric
#: measures graph blindness, not language coverage.
GRAPH_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
    ".py", ".go", ".rs",
}

_LIST_CAP = 50  # cap for embedded lists in the JSON report


# ── Executable-LOC scanner ──────────────────────────────────────────

def _c_family_executable(lines: list[str]) -> set[int]:
    """1-indexed executable line numbers for ``//`` + ``/* */`` syntax."""
    executable: set[int] = set()
    in_block = False
    for lineno, raw in enumerate(lines, start=1):
        code_parts: list[str] = []
        s = raw
        i = 0
        while i < len(s):
            if in_block:
                end = s.find("*/", i)
                if end == -1:
                    i = len(s)
                else:
                    in_block = False
                    i = end + 2
                continue
            line_idx = s.find("//", i)
            block_idx = s.find("/*", i)
            if line_idx != -1 and (block_idx == -1 or line_idx < block_idx):
                code_parts.append(s[i:line_idx])
                i = len(s)
            elif block_idx != -1:
                code_parts.append(s[i:block_idx])
                in_block = True
                i = block_idx + 2
            else:
                code_parts.append(s[i:])
                i = len(s)
        if "".join(code_parts).strip():
            executable.add(lineno)
    return executable


def _hash_family_executable(
    lines: list[str], markers: tuple[str, ...] = ("#",),
) -> set[int]:
    """1-indexed executable line numbers for full-line ``#`` comments."""
    executable: set[int] = set()
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if any(stripped.startswith(m) for m in markers):
            continue
        executable.add(lineno)
    return executable


def executable_lines(text: str, ext: str) -> set[int]:
    """Return the 1-indexed executable line numbers of *text*.

    ``ext`` is the lowercase file extension (with dot). Unknown
    extensions fall back to blank-line-only filtering.
    """
    lines = text.splitlines()
    if ext in C_FAMILY_EXTS:
        return _c_family_executable(lines)
    if ext in PHP_EXTS:
        # PHP: strip C-family comments first, then drop full-line ``#``.
        c_lines = _c_family_executable(lines)
        return {
            n for n in c_lines
            if not lines[n - 1].strip().startswith("#")
        }
    if ext in HASH_FAMILY_EXTS:
        return _hash_family_executable(lines)
    return {
        n for n, raw in enumerate(lines, start=1) if raw.strip()
    }


# ── Source-file enumeration (engine exclusion rules) ────────────────

def enumerate_source_files(repo_root: Path) -> list[str]:
    """Repo-relative source files per the engine's exclusion rules.

    Uses the pipeline's canonical tracked-file helper when the tree is
    a git repo (``faultline.analyzer.git.get_tracked_files``), else the
    intake walker fallback (fixture-friendly for tests). Then filters
    to :data:`SOURCE_EXTS` and drops generated files
    (:func:`is_generated_path`).
    """
    tracked: list[str]
    try:
        from git import Repo  # GitPython — engine dependency

        from faultline.analyzer.git import get_tracked_files

        tracked = get_tracked_files(Repo(str(repo_root)))
    except Exception:  # noqa: BLE001 — non-git fixture trees
        from faultline.pipeline_v2.stage_0_intake import _walk_tracked_files

        tracked = _walk_tracked_files(repo_root)

    out: list[str] = []
    for rel in tracked:
        norm = rel.replace(os.sep, "/")
        ext = os.path.splitext(norm)[1].lower()
        if ext not in SOURCE_EXTS:
            continue
        if is_generated_path(norm):
            continue
        if not (repo_root / norm).is_file():
            continue
        out.append(norm)
    return sorted(set(out))


def _load_executable_map(
    repo_root: Path, files: Iterable[str],
) -> dict[str, set[int]]:
    """path → set of executable line numbers. Unreadable files → empty."""
    out: dict[str, set[int]] = {}
    for rel in files:
        try:
            text = (repo_root / rel).read_text(
                encoding="utf-8", errors="replace",
            )
        except OSError:
            out[rel] = set()
            continue
        ext = os.path.splitext(rel)[1].lower()
        out[rel] = executable_lines(text, ext)
    return out


# ── Small helpers ───────────────────────────────────────────────────

def percentile(sorted_values: list[int], pct: float) -> int:
    """Deterministic nearest-rank percentile over a sorted list."""
    if not sorted_values:
        return 0
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[rank - 1]


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 2) if denominator else 0.0


def _top_dirs_by_loc(
    loc_by_file: dict[str, int], files: Iterable[str], top_n: int = 10,
) -> list[dict[str, Any]]:
    """Aggregate LOC of *files* by immediate parent dir, top-N desc."""
    acc: dict[str, dict[str, int]] = {}
    for rel in files:
        d = os.path.dirname(rel) or "."
        slot = acc.setdefault(d, {"loc": 0, "files": 0})
        slot["loc"] += loc_by_file.get(rel, 0)
        slot["files"] += 1
    ranked = sorted(acc.items(), key=lambda kv: (-kv[1]["loc"], kv[0]))
    return [
        {"dir": d, "loc": v["loc"], "files": v["files"]}
        for d, v in ranked[:top_n]
        if v["loc"] > 0
    ]


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent 1-indexed inclusive line ranges."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


# ── Audit sections ──────────────────────────────────────────────────

def _audit_attribution(
    scan: dict[str, Any],
    files: list[str],
    loc_by_file: dict[str, int],
    total_loc: int,
) -> dict[str, Any]:
    attributed_paths: set[str] = set()
    for feat in scan.get("developer_features") or []:
        for p in feat.get("paths") or []:
            attributed_paths.add(str(p).replace(os.sep, "/"))
    file_set = set(files)
    attributed_files = sorted(attributed_paths & file_set)
    unattributed_files = sorted(file_set - attributed_paths)
    attributed_loc = sum(loc_by_file[f] for f in attributed_files)
    return {
        "attributed_loc": attributed_loc,
        "total_executable_loc": total_loc,
        "pct": _pct(attributed_loc, total_loc),
        "attributed_files": len(attributed_files),
        "unattributed_files": len(unattributed_files),
        "unattributed_top_dirs": _top_dirs_by_loc(
            loc_by_file, unattributed_files,
        ),
    }


def _audit_flow_spans(
    scan: dict[str, Any],
    exec_map: dict[str, set[int]],
    loc_by_file: dict[str, int],
    total_loc: int,
) -> dict[str, Any]:
    flows = scan.get("flows") or []
    # Union of all flow spans per file → covered executable LOC.
    spans_by_file: dict[str, list[tuple[int, int]]] = {}
    flow_locs: list[tuple[str, int]] = []  # (flow id, raw span LOC)
    for flow in flows:
        per_file: dict[str, list[tuple[int, int]]] = {}
        for r in flow.get("line_ranges") or []:
            path = str(r.get("path", "")).replace(os.sep, "/")
            start = int(r.get("start_line") or 0)
            end = int(r.get("end_line") or 0)
            if not path or start <= 0 or end < start:
                continue
            per_file.setdefault(path, []).append((start, end))
            spans_by_file.setdefault(path, []).append((start, end))
        raw_loc = sum(
            end - start + 1
            for ranges in per_file.values()
            for start, end in _merge_ranges(ranges)
        )
        flow_locs.append((str(flow.get("id") or flow.get("name") or "?"),
                          raw_loc))

    covered_loc = 0
    for path, ranges in spans_by_file.items():
        exec_set = exec_map.get(path)
        if not exec_set:
            continue
        covered = set()
        for start, end in _merge_ranges(ranges):
            covered.update(n for n in range(start, end + 1) if n in exec_set)
        covered_loc += len(covered)

    sizes = sorted(loc for _, loc in flow_locs)
    degenerate = sorted(
        (fid for fid, loc in flow_locs if loc <= 2),
    )
    return {
        "covered_loc": covered_loc,
        "total_executable_loc": total_loc,
        "pct": _pct(covered_loc, total_loc),
        "flows_total": len(flows),
        "flow_loc_p10": percentile(sizes, 10),
        "flow_loc_p50": percentile(sizes, 50),
        "flow_loc_p90": percentile(sizes, 90),
        "degenerate_flows_count": len(degenerate),
        "degenerate_flows": degenerate[:_LIST_CAP],
    }


def _audit_entry_coverage(scan: dict[str, Any]) -> dict[str, Any]:
    routes = scan.get("routes_index") or []
    entry_files = {
        str(f.get("entry_point_file", "")).replace(os.sep, "/")
        for f in (scan.get("flows") or [])
        if f.get("entry_point_file")
    }
    flow_paths: set[str] = set()
    for f in scan.get("flows") or []:
        for p in f.get("paths") or []:
            flow_paths.add(str(p).replace(os.sep, "/"))

    orphans: list[dict[str, Any]] = []
    softer_orphans = 0
    for r in routes:
        rfile = str(r.get("file", "")).replace(os.sep, "/")
        if rfile and rfile not in entry_files:
            orphans.append({
                "pattern": r.get("pattern"),
                "method": r.get("method"),
                "file": rfile,
            })
            if rfile not in flow_paths:
                softer_orphans += 1
    orphans.sort(key=lambda o: (str(o["file"]), str(o["pattern"])))
    return {
        "routes_total": len(routes),
        "routes_no_flow_entry_count": len(orphans),
        "routes_no_flow_entry_pct": _pct(len(orphans), len(routes)),
        # Stricter symptom: route file absent from EVERY flow's paths.
        "routes_in_no_flow_paths_count": softer_orphans,
        "routes_no_flow_entry": orphans[:_LIST_CAP],
    }


def _audit_graph_reach(
    repo_root: Path,
    scan: dict[str, Any],
    files: list[str],
    loc_by_file: dict[str, int],
) -> dict[str, Any]:
    graph_files = [
        f for f in files
        if os.path.splitext(f)[1].lower() in GRAPH_EXTS
    ]
    if not graph_files:
        return {
            "graph_files_total": 0,
            "reached": 0,
            "pct": 0.0,
            "entry_seeds": 0,
            "unreached_top_loc": [],
        }
    from faultline.analyzer.symbol_graph import build_symbol_graph

    graph = build_symbol_graph(
        repo_root, graph_files, include_http_edges=True,
    )
    graph_file_set = set(graph_files)
    seeds = sorted({
        str(f.get("entry_point_file", "")).replace(os.sep, "/")
        for f in (scan.get("flows") or [])
        if f.get("entry_point_file")
    } & graph_file_set)

    reached: set[str] = set()
    frontier = list(seeds)
    reached.update(frontier)
    while frontier:
        nxt: list[str] = []
        for file in frontier:
            for edge in graph.forward.get(file, []):
                target = edge.target_file
                if target in graph_file_set and target not in reached:
                    reached.add(target)
                    nxt.append(target)
        frontier = nxt

    unreached = sorted(
        graph_file_set - reached,
        key=lambda f: (-loc_by_file.get(f, 0), f),
    )
    return {
        "graph_files_total": len(graph_files),
        "reached": len(reached),
        "pct": _pct(len(reached), len(graph_files)),
        "entry_seeds": len(seeds),
        "unreached_top_loc": [
            {"path": f, "loc": loc_by_file.get(f, 0)}
            for f in unreached[:10]
        ],
    }


# ── Public API ──────────────────────────────────────────────────────

def audit(repo_root: str | Path, scan: dict[str, Any]) -> dict[str, Any]:
    """Run the four-section line-completeness audit. Pure + read-only."""
    root = Path(repo_root).resolve()
    files = enumerate_source_files(root)
    exec_map = _load_executable_map(root, files)
    loc_by_file = {f: len(lines) for f, lines in exec_map.items()}
    total_loc = sum(loc_by_file.values())

    return {
        "schema": "line-completeness-audit/1",
        "repo_path": str(root),
        "engine_version": scan.get("engine_version"),
        "scan_run_id": (scan.get("scan_meta") or {}).get("run_id"),
        "analyzed_at": scan.get("analyzed_at"),
        "totals": {
            "source_files": len(files),
            "executable_loc": total_loc,
        },
        "attribution": _audit_attribution(
            scan, files, loc_by_file, total_loc,
        ),
        "flow_span": _audit_flow_spans(
            scan, exec_map, loc_by_file, total_loc,
        ),
        "entry_coverage": _audit_entry_coverage(scan),
        "graph_reach": _audit_graph_reach(root, scan, files, loc_by_file),
    }


def _summary(report: dict[str, Any]) -> str:
    a = report["attribution"]
    fs = report["flow_span"]
    ec = report["entry_coverage"]
    gr = report["graph_reach"]
    lines = [
        f"line-completeness: {report['repo_path']}",
        f"  source files          {report['totals']['source_files']}"
        f"  (executable LOC {report['totals']['executable_loc']})",
        f"  attribution           {a['pct']}%"
        f"  ({a['attributed_loc']}/{a['total_executable_loc']} LOC,"
        f" {a['unattributed_files']} files unattributed)",
        f"  flow-span coverage    {fs['pct']}%"
        f"  (flows={fs['flows_total']},"
        f" p10/p50/p90={fs['flow_loc_p10']}/{fs['flow_loc_p50']}/"
        f"{fs['flow_loc_p90']} LOC,"
        f" degenerate<=2LOC={fs['degenerate_flows_count']})",
        f"  entry coverage        "
        f"{ec['routes_total'] - ec['routes_no_flow_entry_count']}"
        f"/{ec['routes_total']} routes reached a flow"
        f" ({ec['routes_no_flow_entry_count']} orphaned)",
        f"  graph reach           {gr['pct']}%"
        f"  ({gr['reached']}/{gr['graph_files_total']} graph files"
        f" from {gr['entry_seeds']} entry seeds)",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m faultline.tools.line_completeness",
        description="WS2 dev-grain line-completeness audit "
                    "(tracked-not-gated).",
    )
    parser.add_argument("--repo", required=True, help="repo tree (read-only)")
    parser.add_argument("--scan", required=True, help="scan JSON path")
    parser.add_argument("--json", help="write full report JSON here")
    args = parser.parse_args(argv)

    scan = json.loads(Path(args.scan).read_text(encoding="utf-8"))
    report = audit(args.repo, scan)
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
