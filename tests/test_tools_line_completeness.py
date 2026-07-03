"""Tests for faultline.tools.line_completeness (WS2 audit tool).

Deterministic fixtures in tmp_path — nothing cloned, no git needed
(the enumerator falls back to the intake walker on non-git trees).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.tools.line_completeness import (
    audit,
    enumerate_source_files,
    executable_lines,
    main,
    percentile,
)

# ── executable_lines: hash family ───────────────────────────────────


def test_py_blank_and_comment_lines_excluded():
    text = (
        "# top comment\n"
        "\n"
        "x = 1\n"
        "   \n"
        "    # indented comment\n"
        "y = 2  # trailing comment still code\n"
    )
    assert executable_lines(text, ".py") == {3, 6}


def test_py_docstrings_count_as_code():
    # Documented approximation: triple-quoted strings are code.
    text = '"""module docstring"""\ndef f():\n    pass\n'
    assert executable_lines(text, ".py") == {1, 2, 3}


def test_ruby_hash_comments():
    text = "# frozen_string_literal: true\nputs 'hi'\n"
    assert executable_lines(text, ".rb") == {2}


# ── executable_lines: C family ──────────────────────────────────────


def test_ts_line_comments_and_blanks():
    text = (
        "// header\n"
        "const a = 1;\n"
        "\n"
        "  // indented comment\n"
        "const b = 2; // trailing\n"
    )
    assert executable_lines(text, ".ts") == {2, 5}


def test_ts_block_comment_multiline():
    text = (
        "/*\n"
        " * license\n"
        " */\n"
        "export const x = 1;\n"
    )
    assert executable_lines(text, ".ts") == {4}


def test_ts_code_before_and_after_block_comment():
    text = (
        "const a = 1; /* note */\n"          # code before → executable
        "/* lead */ const b = 2;\n"          # code after → executable
        "/* only comment */\n"               # nothing left → comment
    )
    assert executable_lines(text, ".ts") == {1, 2}


def test_ts_block_spanning_code_lines():
    text = (
        "const a = 1; /* open\n"
        "still comment\n"
        "end */ const b = 2;\n"
    )
    assert executable_lines(text, ".ts") == {1, 3}


def test_ts_url_in_string_is_executable():
    # '//' inside a string: verdict stays executable because code
    # precedes the marker (documented approximation).
    text = "const u = 'https://example.com';\n"
    assert executable_lines(text, ".ts") == {1}


def test_go_and_rust_use_c_family():
    assert executable_lines("// c\nfunc main() {}\n", ".go") == {2}
    assert executable_lines("/* c */\nfn main() {}\n", ".rs") == {2}


def test_php_hash_and_slash_comments():
    text = "<?php\n# hash comment\n// slash comment\n$x = 1;\n"
    assert executable_lines(text, ".php") == {1, 4}


def test_unknown_ext_blank_filter_only():
    assert executable_lines("a\n\nb\n", ".txt") == {1, 3}


# ── percentile ──────────────────────────────────────────────────────


def test_percentile_nearest_rank():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(values, 10) == 1
    assert percentile(values, 50) == 5
    assert percentile(values, 90) == 9
    assert percentile([], 50) == 0
    assert percentile([7], 90) == 7


# ── fixture repo + scan ─────────────────────────────────────────────


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src/billing").mkdir(parents=True)
    (repo / "src/orphan").mkdir(parents=True)
    (repo / "node_modules/dep").mkdir(parents=True)

    # 4 executable lines (1 comment, 1 blank excluded).
    (repo / "src/billing/charge.ts").write_text(
        "// billing entry\n"
        "import { helper } from './helper';\n"
        "\n"
        "export function charge() {\n"
        "  return helper();\n"
        "}\n",
        encoding="utf-8",
    )
    # 3 executable lines.
    (repo / "src/billing/helper.ts").write_text(
        "export function helper() {\n"
        "  return 1;\n"
        "}\n",
        encoding="utf-8",
    )
    # Unattributed + unreached: 5 executable lines.
    (repo / "src/orphan/di_service.py").write_text(
        "# DI-injected, nobody imports statically\n"
        "class Service:\n"
        "    def run(self):\n"
        "        a = 1\n"
        "        b = 2\n"
        "        return a + b\n",
        encoding="utf-8",
    )
    # Excluded: generated + vendored.
    (repo / "src/orphan/api_pb2.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "node_modules/dep/index.js").write_text(
        "module.exports = 1;\n", encoding="utf-8",
    )
    return repo


@pytest.fixture()
def fixture_scan() -> dict:
    return {
        "engine_version": "test",
        "analyzed_at": "1970-01-01T00:00:00Z",
        "scan_meta": {"run_id": "fixture"},
        "developer_features": [
            {"name": "billing", "paths": [
                "src/billing/charge.ts",
                "src/billing/helper.ts",
            ]},
        ],
        "flows": [
            {
                "id": "billing::charge-flow",
                "entry_point_file": "src/billing/charge.ts",
                "paths": ["src/billing/charge.ts"],
                "line_ranges": [
                    {"path": "src/billing/charge.ts",
                     "start_line": 4, "end_line": 6},
                ],
            },
            {
                # Degenerate wrapper-entry flow: 1 raw span LOC.
                "id": "billing::wrapper-flow",
                "entry_point_file": "src/billing/charge.ts",
                "paths": ["src/billing/charge.ts"],
                "line_ranges": [
                    {"path": "src/billing/charge.ts",
                     "start_line": 2, "end_line": 2},
                ],
            },
        ],
        "routes_index": [
            {"pattern": "/charge", "method": "POST",
             "file": "src/billing/charge.ts"},
            {"pattern": "/orphan", "method": "GET",
             "file": "src/orphan/di_service.py"},
        ],
    }


# ── enumeration ─────────────────────────────────────────────────────


def test_enumerate_excludes_generated_and_vendored(fixture_repo: Path):
    files = enumerate_source_files(fixture_repo)
    assert files == [
        "src/billing/charge.ts",
        "src/billing/helper.ts",
        "src/orphan/di_service.py",
    ]


# ── audit sections ──────────────────────────────────────────────────


def test_audit_attribution(fixture_repo: Path, fixture_scan: dict):
    report = audit(fixture_repo, fixture_scan)
    # charge.ts 4 + helper.ts 3 attributed; di_service.py 5 not.
    assert report["totals"]["executable_loc"] == 12
    a = report["attribution"]
    assert a["attributed_loc"] == 7
    assert a["pct"] == pytest.approx(58.33)
    assert a["unattributed_files"] == 1
    assert a["unattributed_top_dirs"][0] == {
        "dir": "src/orphan", "loc": 5, "files": 1,
    }


def test_audit_flow_spans_and_degenerate(
    fixture_repo: Path, fixture_scan: dict,
):
    fs = audit(fixture_repo, fixture_scan)["flow_span"]
    # Span 4-6 covers exec lines {4,5,6}; span 2-2 covers {2} → 4 LOC.
    assert fs["covered_loc"] == 4
    assert fs["flows_total"] == 2
    assert fs["degenerate_flows_count"] == 1
    assert fs["degenerate_flows"] == ["billing::wrapper-flow"]
    assert fs["flow_loc_p50"] == 1  # sorted sizes [1, 3]
    assert fs["flow_loc_p90"] == 3


def test_audit_entry_coverage(fixture_repo: Path, fixture_scan: dict):
    ec = audit(fixture_repo, fixture_scan)["entry_coverage"]
    assert ec["routes_total"] == 2
    assert ec["routes_no_flow_entry_count"] == 1
    assert ec["routes_no_flow_entry"][0]["file"] == "src/orphan/di_service.py"
    assert ec["routes_in_no_flow_paths_count"] == 1


def test_audit_graph_reach(fixture_repo: Path, fixture_scan: dict):
    gr = audit(fixture_repo, fixture_scan)["graph_reach"]
    # Entry charge.ts imports helper.ts; di_service.py unreached.
    assert gr["graph_files_total"] == 3
    assert gr["entry_seeds"] == 1
    assert gr["reached"] == 2
    assert gr["pct"] == pytest.approx(66.67)
    assert gr["unreached_top_loc"] == [
        {"path": "src/orphan/di_service.py", "loc": 5},
    ]


def test_audit_no_flows_no_routes(fixture_repo: Path):
    report = audit(fixture_repo, {"developer_features": [], "flows": []})
    assert report["flow_span"]["pct"] == 0.0
    assert report["entry_coverage"]["routes_total"] == 0
    assert report["graph_reach"]["entry_seeds"] == 0


# ── overlapping spans merge (no double counting) ────────────────────


def test_overlapping_flow_spans_merge(fixture_repo: Path):
    scan = {
        "developer_features": [],
        "flows": [
            {"id": "f1", "entry_point_file": "src/billing/charge.ts",
             "line_ranges": [
                 {"path": "src/billing/charge.ts",
                  "start_line": 1, "end_line": 4},
                 {"path": "src/billing/charge.ts",
                  "start_line": 3, "end_line": 6},
             ]},
        ],
        "routes_index": [],
    }
    fs = audit(fixture_repo, scan)["flow_span"]
    # Merged raw span 1-6 = 6 raw LOC (not 4+4=8);
    # executable within = lines {2,4,5,6} = 4.
    assert fs["flow_loc_p50"] == 6
    assert fs["covered_loc"] == 4


# ── CLI ─────────────────────────────────────────────────────────────


def test_cli_writes_json(
    fixture_repo: Path, fixture_scan: dict, tmp_path: Path, capsys,
):
    scan_path = tmp_path / "scan.json"
    scan_path.write_text(json.dumps(fixture_scan), encoding="utf-8")
    out_path = tmp_path / "report.json"
    rc = main([
        "--repo", str(fixture_repo),
        "--scan", str(scan_path),
        "--json", str(out_path),
    ])
    assert rc == 0
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["schema"] == "line-completeness-audit/1"
    assert report["attribution"]["attributed_loc"] == 7
    assert "line-completeness" in capsys.readouterr().out
