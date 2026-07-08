#!/usr/bin/env python3
"""Track-B py_ast metrics harness — the §5 gate ruler for Python.

The Python mirror of ``tools/ast_metrics_ab.py``. Computes, on a REAL
clone (any local repo path), the three program-gate metrics of the
Track-B spec §5:

  * def-span coverage — % of exported symbols carrying a usable line
    range (gate: >= 90%).
  * resolution-%      — resolved / total NON-EXTERNAL import edges,
    where non-external is decided by py_ast's own resolver classes
    (relative || workspace). (gate: >= 95%).
  * parse-fail %      — % of .py files py_ast failed to parse
    (gate: < 0.5%).

The regex/legacy Python path (``analyzer.ast_extractor`` — itself
stdlib-``ast`` based) is shown for def-span coverage parity; it has NO
import resolution (the gap py_ast fills), so its resolution column is
``n/a``.

Deterministic: files walked sorted, no set iteration reaches output.

Usage:
  .venv/bin/python tools/py_ast_metrics_ab.py <repo> [--subpath backend]
      [--out-json F] [--out-md F] [--max-files N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    "migrations", "static", "media", ".next", "dist", "build", ".mypy_cache",
    ".pytest_cache", "site-packages", ".eggs",
})
_NON_EXTERNAL = frozenset({"relative", "workspace", "tsconfig_alias"})


def collect_py_files(repo: Path, subpath: str, max_files: int | None
                     ) -> list[str]:
    base = repo / subpath if subpath else repo
    files: list[str] = []
    for root, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS
                         and not d.startswith("."))
        for fn in sorted(names):
            if fn.endswith(".py") and not fn.endswith(".pyi"):
                files.append(os.path.relpath(os.path.join(root, fn), repo)
                             .replace(os.sep, "/"))
    files.sort()
    return files[:max_files] if max_files else files


def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 2) if den else None


def run_ast(repo: Path, files: list[str]) -> dict[str, Any]:
    os.environ["FAULTLINE_PY_AST"] = "1"
    from faultline.pipeline_v2.py_ast import adapter, parse
    parse.reset_state()
    adapter.reset_py_ast_state()
    fns = adapter._load_real_fns()
    if fns is None:
        return {"available": False, "reason": "py_ast pipeline unavailable"}
    t0 = time.monotonic()
    graph = adapter.build_symbol_graph(
        str(repo), files,
        parse_fn=fns.parse_fn, defs_fn=fns.defs_fn,
        imports_fn=fns.imports_fn, resolve_fn=fns.resolve_fn,
    )
    tel = graph.telemetry
    exported = with_span = 0
    for d in graph.defs:
        if not d.exported:
            continue
        exported += 1
        if d.start_line >= 1 and d.end_line >= d.start_line:
            with_span += 1
    non_ext = resolved = 0
    for r in graph.resolved:
        if r.resolution not in _NON_EXTERNAL:
            continue
        non_ext += 1
        if r.target_file is not None:
            resolved += 1
    pf = int(tel.get("parse_failures", 0))
    seen = int(tel.get("files_parsed", 0)) + pf
    return {
        "available": True,
        "engine": "py_ast",
        "files_parsed": tel.get("files_parsed"),
        "parse_fail": pf,
        "parse_fail_pct": _pct(pf, seen),
        "exported_symbols": exported,
        "def_span_coverage_pct": _pct(with_span, exported),
        "imports_total": tel.get("edges"),
        "resolved_total": tel.get("resolved_total"),
        "non_external": non_ext,
        "resolved_non_external": resolved,
        "resolution_pct": _pct(resolved, non_ext),
        "resolution_histogram": tel.get("resolution_histogram"),
        "source_roots": tel.get("resolve", {}).get("source_roots"),
        "elapsed_sec": round(time.monotonic() - t0, 2),
    }


def run_legacy(repo: Path, files: list[str]) -> dict[str, Any]:
    os.environ["FAULTLINE_PY_AST"] = "0"
    from faultline.analyzer.ast_extractor import extract_signatures
    t0 = time.monotonic()
    sigs = extract_signatures(files, str(repo))
    exported = with_span = read_fail = 0
    for rel in files:
        sig = sigs.get(rel)
        if sig is None:
            continue
        ranges = {}
        for sr in sig.symbol_ranges:
            ranges.setdefault(sr.name, (sr.start_line, sr.end_line))
        for name in sorted(set(sig.exports)):
            exported += 1
            span = ranges.get(name)
            if span and span[0] >= 1 and span[1] >= span[0]:
                with_span += 1
    return {
        "available": True,
        "engine": "legacy",
        "exported_symbols": exported,
        "def_span_coverage_pct": _pct(with_span, exported),
        "resolution_pct": None,  # legacy Python has NO import graph
        "elapsed_sec": round(time.monotonic() - t0, 2),
    }


def render_md(result: dict[str, Any]) -> str:
    a = result["engines"]["py_ast"]
    lg = result["engines"]["legacy"]
    lines = [
        f"# py_ast metrics — {result['repo']}",
        "",
        f"- .py files: {result['file_count']}  source_roots: {a.get('source_roots')}",
        "",
        "| metric | py_ast | legacy |",
        "|---|---|---|",
        f"| def-span coverage % (gate >=90) | {a.get('def_span_coverage_pct')} | {lg.get('def_span_coverage_pct')} |",
        f"| resolution % (gate >=95) | {a.get('resolution_pct')} | n/a |",
        f"| parse-fail % (gate <0.5) | {a.get('parse_fail_pct')} | - |",
        f"| import edges | {a.get('imports_total')} | n/a |",
        f"| non-external edges | {a.get('non_external')} | n/a |",
        f"| exported symbols | {a.get('exported_symbols')} | {lg.get('exported_symbols')} |",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/py_ast_metrics_ab.py")
    ap.add_argument("repo")
    ap.add_argument("--subpath", default="")
    ap.add_argument("--out-json", type=Path, default=None)
    ap.add_argument("--out-md", type=Path, default=None)
    ap.add_argument("--max-files", type=int, default=None)
    args = ap.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 2
    files = collect_py_files(repo, args.subpath, args.max_files)
    result = {
        "repo": str(repo),
        "subpath": args.subpath,
        "file_count": len(files),
        "generated_by": "tools/py_ast_metrics_ab.py (Track B M5)",
        "engines": {
            "py_ast": run_ast(repo, files),
            "legacy": run_legacy(repo, files),
        },
    }
    md = render_md(result)
    js = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    if args.out_json:
        args.out_json.write_text(js, encoding="utf-8")
    if args.out_md:
        args.out_md.write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
