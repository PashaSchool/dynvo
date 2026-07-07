#!/usr/bin/env python3
"""W6-AST A/B metrics harness (M5) — regex engine vs tree-sitter engine.

Computes, on a REAL clone (any local repo path; fixtures dirs work too),
the four program-gate metrics of w6ast-spec §5:

  * def-span coverage      — % of exported symbols carrying a usable
                             line-range (gate: AST >= 90%)
  * degenerate-span %      — % of exported symbols whose span is <= 2
                             lines; mirrors the W4 wrapper-span ruler
                             (stage_6_55.degenerate_span_stats uses the
                             same <= 2 LOC class on flows; here it is
                             measured at the def level, pre-pipeline)
  * resolution-%           — resolved / total NON-EXTERNAL import edges;
                             non-external is decided by a shared,
                             engine-independent, config-derived ruler
                             (relative || tsconfig-alias || workspace
                             package) so both engines are graded against
                             the same denominator definition
                             (gate: AST >= 95%)
  * parse-fail %           — % of TS/JS files the engine failed to read/
                             parse (gate: AST < 0.5%; the regex path
                             counts only read/decode failures — regexes
                             cannot fail to "parse")

plus wrapper-share (% of exported defs with wrapper != 'none'; AST only —
the regex path has no wrapper notion, which is exactly the W4 gap).

Engine modes:
  --engine-regex   the shipped fallback path (faultline.analyzer.
                   ast_extractor + tsconfig_paths/import_graph resolvers)
  --engine-ast     the W6 tree-sitter path (faultline.pipeline_v2.ts_ast.*,
                   FAULTLINE_TS_AST=1); reported as available=false until
                   M1-M3 land — the harness must run TODAY.

Default (no engine flag): run both and emit a side-by-side table.

Output: JSON (machine) + a markdown table (human), stdout by default,
--out-json / --out-md to write files. Deterministic: files walked sorted,
no set iteration reaches the output.

Usage:
  .venv/bin/python tools/ast_metrics_ab.py <repo_path> [--engine-regex]
      [--engine-ast] [--out-json F] [--out-md F] [--max-files N]

Target clones for the program's before/after run (wave_parallel repo_for):
  typebot     /Users/pkuzina/workspace/fl-unseen7/typebot
  openstatus  /Users/pkuzina/workspace/_faultlines-testrepos/openstatus
  papermark   /Users/pkuzina/workspace/_faultlines-testrepos/papermark
  supabase    /Users/pkuzina/workspace/_faultlines-testrepos/supabase
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Run from a repo checkout without installation.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from faultline.analyzer.ast_extractor import (  # noqa: E402
    extract_named_imports,
    extract_signatures,
)
from faultline.analyzer.import_graph import (  # noqa: E402
    _resolve_workspace_package_import,
    detect_workspace_package_map,
)
from faultline.analyzer.tsconfig_paths import (  # noqa: E402
    build_path_alias_map,
    resolve_ts_import,
)

try:  # keep the walk exclusions aligned with the engine's own resolver walk
    from faultline.analyzer.tsconfig_paths import _SKIP_DIR_NAMES
except ImportError:  # pragma: no cover - constant rename safety net
    _SKIP_DIR_NAMES = frozenset(
        {"node_modules", ".git", "dist", "build", "out", "vendor"})

_TS_JS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_EXTRA_SKIP_DIRS = frozenset(
    {".git", ".next", ".turbo", ".venv", "__pycache__", "coverage",
     "storybook-static", ".output", ".nuxt", ".svelte-kit"})
_DEGENERATE_MAX_LINES = 2  # mirrors stage_6_55.degenerate_span_stats

_AST_DEFS_MOD = "faultline.pipeline_v2.ts_ast.defs"
_AST_IMPORTS_MOD = "faultline.pipeline_v2.ts_ast.imports"
_AST_RESOLVE_MOD = "faultline.pipeline_v2.ts_ast.resolve"
_DEFS_ENTRYPOINTS = ("extract_defs", "collect_defs", "defs_for_root")
_IMPORTS_ENTRYPOINTS = ("extract_imports", "collect_imports")
_RESOLVE_ENTRYPOINTS = ("resolve_imports", "resolve_edges", "resolve")


# ── file walk ────────────────────────────────────────────────────────────

def collect_ts_files(repo: Path, max_files: int | None) -> tuple[list[str], int]:
    """Sorted repo-relative TS/JS files (skipping vendor dirs and .d.ts);
    returns (files, dts_skipped_count)."""
    skip = frozenset(_SKIP_DIR_NAMES) | _EXTRA_SKIP_DIRS
    files: list[str] = []
    dts = 0
    for base, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(
            d for d in dirnames if d not in skip and not d.startswith("."))
        rel_base = os.path.relpath(base, repo)
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _TS_JS_EXTENSIONS:
                continue
            rel = fn if rel_base == "." else f"{rel_base}/{fn}"
            rel = rel.replace(os.sep, "/")
            if fn.endswith(".d.ts"):
                dts += 1
                continue
            files.append(rel)
    files.sort()
    if max_files is not None:
        files = files[:max_files]
    return files, dts


# ── shared non-external ruler (config-derived, engine-independent) ──────

class NonExternalRuler:
    """Classify an import specifier as non-external via repo CONFIG only:
    relative target, tsconfig paths/baseUrl alias, or workspace package.
    Both engines are graded against this same denominator."""

    def __init__(self, repo: Path) -> None:
        self.alias_entries = build_path_alias_map(repo)
        self.alias_prefixes = tuple(
            sorted({e.prefix for e in self.alias_entries}, key=len,
                   reverse=True))
        self.workspace_packages = detect_workspace_package_map(str(repo))
        self._ws_names = tuple(
            sorted(self.workspace_packages, key=len, reverse=True))

    def classify(self, spec: str) -> str:
        if spec.startswith("./") or spec.startswith("../"):
            return "relative"
        for prefix in self.alias_prefixes:
            if spec.startswith(prefix):
                return "tsconfig_alias"
        for name in self._ws_names:
            if spec == name or spec.startswith(name + "/"):
                return "workspace"
        return "external"


# ── regex engine ─────────────────────────────────────────────────────────

def run_regex_engine(repo: Path, files: list[str],
                     ruler: NonExternalRuler) -> dict[str, Any]:
    os.environ["FAULTLINE_TS_AST"] = "0"
    t0 = time.monotonic()
    tracked = frozenset(files)

    read_fail = 0
    sources: dict[str, str] = {}
    for rel in files:
        try:
            sources[rel] = (repo / rel).read_text(
                encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            read_fail += 1

    sigs = extract_signatures(sorted(sources), str(repo))

    exported = with_span = degenerate = 0
    for rel in sorted(sources):
        sig = sigs.get(rel)
        if sig is None:
            continue
        ranges = {}
        for sr in sig.symbol_ranges:
            ranges.setdefault(sr.name, (sr.start_line, sr.end_line))
        for name in sorted(set(sig.exports)):
            exported += 1
            span = ranges.get(name)
            if span is None or span[0] < 1 or span[1] < span[0]:
                continue
            with_span += 1
            if span[1] - span[0] + 1 <= _DEGENERATE_MAX_LINES:
                degenerate += 1

    imports_total = non_external = resolved = 0
    for rel in sorted(sources):
        for spec in sorted(extract_named_imports(sources[rel])):
            imports_total += 1
            klass = ruler.classify(spec)
            if klass == "external":
                continue
            non_external += 1
            target = resolve_ts_import(
                rel, spec, alias_map=ruler.alias_entries,
                tracked_files=tracked)
            if target is None and klass == "workspace":
                target = _resolve_workspace_package_import(
                    spec, tracked, ruler.workspace_packages, str(repo))
            if target is not None:
                resolved += 1

    return _metrics_payload(
        available=True,
        engine="regex",
        files_scanned=len(sources),
        parse_fail=read_fail,
        files_total=len(files),
        exported=exported,
        with_span=with_span,
        degenerate=degenerate,
        wrapper_share_pct=None,  # the regex path has no wrapper notion (W4 gap)
        imports_total=imports_total,
        non_external=non_external,
        resolved=resolved,
        elapsed_sec=round(time.monotonic() - t0, 2),
    )


# ── AST engine (adaptive to the M1-M3 entrypoints; absent today) ────────

def _first_entrypoint(mod_name: str, candidates: tuple[str, ...]):
    mod = importlib.import_module(mod_name)
    for name in candidates:
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    raise AttributeError(f"{mod_name} lacks any of {candidates}")


def _call_root(fn, repo: Path, *extra: Any) -> Any:
    try:
        return fn(repo, *extra)
    except TypeError:
        return fn(str(repo), *extra)


def run_ast_engine(repo: Path, files: list[str],
                   ruler: NonExternalRuler) -> dict[str, Any]:
    os.environ["FAULTLINE_TS_AST"] = "1"
    t0 = time.monotonic()
    try:
        from faultline.pipeline_v2.ts_ast import adapter
        adapter.reset_ts_ast_state()
        fns = adapter._load_real_fns()
        if fns is None:
            return {"available": False, "engine": "ast",
                    "reason": "ts_ast real pipeline unavailable (M1-M3 not wired)"}
        # Repo-level facade: the shipped M1/M2/M3 entrypoints are PER-FILE;
        # adapter.build_symbol_graph is the repo-level bridge M4 built
        # (parse→defs→imports per file, then resolve over the full file set),
        # emitting the canonical §1 shapes the metrics below read.
        graph = adapter.build_symbol_graph(
            str(repo), files,
            parse_fn=fns.parse_fn, defs_fn=fns.defs_fn,
            imports_fn=fns.imports_fn, resolve_fn=fns.resolve_fn,
        )
    except (ImportError, AttributeError) as exc:
        return {"available": False, "engine": "ast",
                "reason": f"ts_ast modules not usable yet: {exc}"}

    defs = graph.defs
    edges = graph.edges
    resolved_edges = graph.resolved

    def _get(rec: Any, key: str, default: Any = None) -> Any:
        if isinstance(rec, dict):
            return rec.get(key, default)
        return getattr(rec, key, default)

    exported = with_span = degenerate = wrappers = 0
    for rec in defs:
        if not _get(rec, "exported", False):
            continue
        exported += 1
        if str(_get(rec, "wrapper", "none")) != "none":
            wrappers += 1
        start, end = _get(rec, "start_line", 0), _get(rec, "end_line", 0)
        if not start or not end or start < 1 or end < start:
            continue
        with_span += 1
        if end - start + 1 <= _DEGENERATE_MAX_LINES:
            degenerate += 1

    resolved_by_edge: dict[tuple[str, str], bool] = {}
    for rec in resolved_edges:
        key = (str(_get(rec, "src_file")), str(_get(rec, "raw_target")))
        hit = _get(rec, "target_file") is not None
        resolved_by_edge[key] = resolved_by_edge.get(key, False) or hit

    imports_total = non_external = resolved = 0
    parse_failures = int(graph.telemetry.get("parse_failures", 0))
    for rec in edges:
        imports_total += 1
        spec = str(_get(rec, "raw_target", ""))
        if ruler.classify(spec) == "external":
            continue
        non_external += 1
        if resolved_by_edge.get((str(_get(rec, "src_file")), spec), False):
            resolved += 1

    payload = _metrics_payload(
        available=True,
        engine="ast",
        files_scanned=len(files),
        parse_fail=parse_failures if parse_failures is not None else 0,
        files_total=len(files),
        exported=exported,
        with_span=with_span,
        degenerate=degenerate,
        wrapper_share_pct=_pct(wrappers, exported),
        imports_total=imports_total,
        non_external=non_external,
        resolved=resolved,
        elapsed_sec=round(time.monotonic() - t0, 2),
    )
    if parse_failures is None:
        payload["parse_fail_pct"] = None
        payload["notes"] = ("parse-fail telemetry not exposed by ts_ast.parse "
                            "yet (parse_stats)")
    return payload


# ── shared payload/rendering ─────────────────────────────────────────────

def _pct(num: int, den: int) -> float | None:
    return round(100.0 * num / den, 2) if den else None


def _metrics_payload(*, available: bool, engine: str, files_scanned: int,
                     parse_fail: int, files_total: int, exported: int,
                     with_span: int, degenerate: int,
                     wrapper_share_pct: float | None, imports_total: int,
                     non_external: int, resolved: int,
                     elapsed_sec: float) -> dict[str, Any]:
    return {
        "available": available,
        "engine": engine,
        "files_scanned": files_scanned,
        "parse_fail": parse_fail,
        "parse_fail_pct": _pct(parse_fail, files_total),
        "exported_symbols": exported,
        "with_line_ranges": with_span,
        "def_span_coverage_pct": _pct(with_span, exported),
        "no_span": exported - with_span,
        "degenerate_spans": degenerate,
        "degenerate_span_pct": _pct(degenerate, exported),
        "wrapper_share_pct": wrapper_share_pct,
        "imports_total": imports_total,
        "imports_non_external": non_external,
        "imports_resolved": resolved,
        "resolution_pct": _pct(resolved, non_external),
        "elapsed_sec": elapsed_sec,
    }


_TABLE_ROWS: list[tuple[str, str]] = [
    ("files_scanned", "files scanned"),
    ("parse_fail_pct", "parse-fail % (gate <0.5)"),
    ("exported_symbols", "exported symbols"),
    ("def_span_coverage_pct", "def-span coverage % (gate >=90)"),
    ("degenerate_span_pct", "degenerate-span % <=2 LOC (target <3)"),
    ("wrapper_share_pct", "wrapper share % (AST only)"),
    ("imports_total", "import edges captured"),
    ("imports_non_external", "non-external edges (shared ruler)"),
    ("resolution_pct", "resolution % (gate >=95)"),
    ("elapsed_sec", "elapsed sec"),
]


def render_markdown(result: dict[str, Any]) -> str:
    engines = result["engines"]
    cols = [name for name in ("regex", "ast") if name in engines]
    lines = [
        f"# AST A/B metrics — {result['repo']}",
        "",
        f"- ts/js files: {result['file_counts']['ts_js_files']} "
        f"(+{result['file_counts']['dts_skipped']} .d.ts skipped)",
        f"- shared non-external ruler: relative | tsconfig_alias"
        f" ({result['shared_ruler']['alias_prefixes']} prefixes)"
        f" | workspace ({result['shared_ruler']['workspace_packages']} pkgs)",
        "",
        "| metric | " + " | ".join(cols) + " |",
        "|---|" + "---|" * len(cols),
    ]
    for key, label in _TABLE_ROWS:
        row = [label]
        for col in cols:
            eng = engines[col]
            if not eng.get("available"):
                row.append("n/a (engine unavailable)")
                continue
            val = eng.get(key)
            row.append("-" if val is None else str(val))
        lines.append("| " + " | ".join(row) + " |")
    for col in cols:
        eng = engines[col]
        if not eng.get("available"):
            lines += ["", f"> {col}: {eng.get('reason', 'unavailable')}"]
        elif eng.get("notes"):
            lines += ["", f"> {col}: {eng['notes']}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="tools/ast_metrics_ab.py",
        description="W6-AST regex-vs-tree-sitter A/B metrics on a real clone")
    ap.add_argument("repo", help="path to a local clone (or a fixture dir)")
    ap.add_argument("--engine-regex", action="store_true",
                    help="run only the regex engine")
    ap.add_argument("--engine-ast", action="store_true",
                    help="run only the tree-sitter engine (FAULTLINE_TS_AST=1)")
    ap.add_argument("--out-json", type=Path, default=None)
    ap.add_argument("--out-md", type=Path, default=None)
    ap.add_argument("--max-files", type=int, default=None,
                    help="cap the walked file list (smoke runs)")
    args = ap.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 2

    run_regex = args.engine_regex or not args.engine_ast
    run_ast = args.engine_ast or not args.engine_regex

    files, dts_skipped = collect_ts_files(repo, args.max_files)
    ruler = NonExternalRuler(repo)

    engines: dict[str, Any] = {}
    if run_regex:
        engines["regex"] = run_regex_engine(repo, files, ruler)
    if run_ast:
        engines["ast"] = run_ast_engine(repo, files, ruler)

    result = {
        "repo": str(repo),
        "generated_by": "tools/ast_metrics_ab.py (W6-AST M5)",
        "file_counts": {"ts_js_files": len(files),
                        "dts_skipped": dts_skipped},
        "shared_ruler": {
            "alias_prefixes": len(ruler.alias_prefixes),
            "workspace_packages": len(ruler.workspace_packages),
        },
        "engines": engines,
    }

    json_txt = json.dumps(result, indent=2, sort_keys=True,
                          ensure_ascii=True) + "\n"
    md_txt = render_markdown(result)
    if args.out_json:
        args.out_json.write_text(json_txt, encoding="utf-8")
    if args.out_md:
        args.out_md.write_text(md_txt, encoding="utf-8")
    print(md_txt)
    if not args.out_json:
        print(json_txt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
