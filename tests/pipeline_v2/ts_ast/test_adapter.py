"""W6-AST M4 — adapter unit pack against spec-shape STUBS (Phase A).

The stubs implement the injected-pipeline contract exactly as
documented in ``adapter.py`` (M1 ``parse_fn``/``defs_fn``, M2
``imports_fn``, M3 ``resolve_fn``); Phase B swaps them for the real
modules without touching these assertions — they test the ADAPTER's
mapping/ordering/fallback laws, not the extraction itself.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from faultline.models.types import SymbolRange
from faultline.pipeline_v2.ts_ast import adapter
from faultline.pipeline_v2.ts_ast.adapter import (
    INTERIOR_KEY_TAG,
    ast_symbol_ranges,
    build_symbol_graph,
    current_provenance,
    defspans_to_flow_spans,
    entry_signals,
    provenance_view,
    repo_provenance,
    reset_ts_ast_state,
    ts_ast_enabled,
    ts_ast_entry_enabled,
)
from faultline.pipeline_v2.ts_ast.shapes import (
    DefSpan,
    ExportEntry,
    FileParse,
    ImportEdge,
    ResolvedEdge,
    SymbolGraph,
)

# ── stub corpus (in-memory; no tree-sitter) ──────────────────────────────

BTN = "apps/web/src/components/button.tsx"
PAGE = "apps/web/src/pages/index.tsx"
DB = "apps/web/src/lib/db.ts"
BARREL = "packages/ui/src/index.ts"
UIBTN = "packages/ui/src/button.tsx"
BROKEN = "apps/web/src/broken.ts"
DTS = "apps/web/src/legacy.d.ts"
PY = "server/main.py"

TRACKED = [BTN, PAGE, DB, BARREL, UIBTN, BROKEN, DTS, PY]

_DEFS: dict[str, list[DefSpan]] = {
    BTN: [
        DefSpan(file=BTN, name="Button", kind="component", start_line=5,
                end_line=9, exported=True, wrapper="forwardRef"),
        DefSpan(file=BTN, name="buttonVariants", kind="const", start_line=11,
                end_line=11, exported=True),
        DefSpan(file=BTN, name="useStyles", kind="function", start_line=13,
                end_line=20, exported=False),
    ],
    DB: [
        DefSpan(file=DB, name="Db", kind="class", start_line=3, end_line=30,
                exported=True),
        DefSpan(file=DB, name="constructor", kind="method", start_line=4,
                end_line=8, exported=False, parent="Db"),
        DefSpan(file=DB, name="query", kind="method", start_line=10,
                end_line=20, exported=False, parent="Db"),
    ],
    UIBTN: [
        DefSpan(file=UIBTN, name="UiButton", kind="component", start_line=2,
                end_line=14, exported=True, wrapper="memo"),
    ],
}

_EDGES: dict[str, list[ImportEdge]] = {
    PAGE: [
        ImportEdge(src_file=PAGE, kind="named", names=("Button",),
                   raw_target="@/components/button", line=1),
        ImportEdge(src_file=PAGE, kind="default", names=("Head",),
                   raw_target="next/head", line=2),
        ImportEdge(src_file=PAGE, kind="side_effect", names=(),
                   raw_target="./globals.css", line=3),
        ImportEdge(src_file=PAGE, kind="named", names=("type:FC",),
                   raw_target="react", line=4),
        ImportEdge(src_file=PAGE, kind="named", names=("UiKit",),
                   raw_target="@acme/ui", line=5),
    ],
    DB: [
        ImportEdge(src_file=DB, kind="named",
                   names=("Button", "buttonVariants"),
                   raw_target="../components/button", line=1),
        ImportEdge(src_file=DB, kind="named", names=("gone",),
                   raw_target="./missing", line=2),
    ],
    BARREL: [
        ImportEdge(src_file=BARREL, kind="reexport_named",
                   names=("UiButton",), raw_target="./button", line=1),
    ],
}

_EXPORTS: dict[str, list[ExportEntry]] = {
    BTN: [
        ExportEntry(file=BTN, name="Button", kind="named"),
        ExportEntry(file=BTN, name="buttonVariants", kind="named"),
    ],
    BARREL: [
        ExportEntry(file=BARREL, name="UiButton", kind="named",
                    origin_file=UIBTN),
    ],
}

#: (src, raw_target) → (target_file, resolution, via_barrels)
_RESOLUTION: dict[tuple[str, str], tuple[str | None, str, tuple[str, ...]]] = {
    (PAGE, "@/components/button"): (BTN, "tsconfig_alias", ()),
    (PAGE, "next/head"): (None, "package_external", ()),
    (PAGE, "./globals.css"): (None, "unresolved", ()),
    (PAGE, "react"): (None, "package_external", ()),
    (PAGE, "@acme/ui"): (UIBTN, "workspace", (BARREL,)),
    (DB, "../components/button"): (BTN, "relative", ()),
    (DB, "./missing"): (None, "relative", ()),  # defensive-mapping case
    (BARREL, "./button"): (UIBTN, "relative", ()),
}


def stub_parse(repo_root: str, rel: str, source: Any = None) -> FileParse | None:
    if rel == BROKEN:
        return None
    lang = "tsx" if rel.endswith(".tsx") else "ts"
    return FileParse(path=rel, content_hash="h:" + rel, lang=lang, tree=None)


def stub_defs(fp: FileParse) -> list[DefSpan]:
    return list(_DEFS.get(fp.path, []))


def stub_imports(
    fp: FileParse,
) -> tuple[list[ImportEdge], list[ExportEntry]]:
    return list(_EDGES.get(fp.path, [])), list(_EXPORTS.get(fp.path, []))


def stub_resolve(
    repo_root: str,
    edges: Any,
    exports_index: Any,
    tracked_files: Any,
) -> list[ResolvedEdge]:
    out: list[ResolvedEdge] = []
    for e in edges:
        target, resolution, via = _RESOLUTION[(e.src_file, e.raw_target)]
        out.append(ResolvedEdge(
            src_file=e.src_file, raw_target=e.raw_target, target_file=target,
            resolution=resolution, via_barrels=via, names=e.names,
            kind=e.kind,
        ))
    return out


def _build(files: list[str] | None = None) -> SymbolGraph:
    return build_symbol_graph(
        "/repo", files if files is not None else TRACKED,
        parse_fn=stub_parse, defs_fn=stub_defs,
        imports_fn=stub_imports, resolve_fn=stub_resolve,
    )


def _stub_fns() -> adapter._PipelineFns:
    return adapter._PipelineFns(
        parse_fn=stub_parse, defs_fn=stub_defs,
        imports_fn=stub_imports, resolve_fn=stub_resolve,
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FAULTLINE_TS_AST", raising=False)
    monkeypatch.delenv("FAULTLINE_TS_AST_ENTRY", raising=False)
    reset_ts_ast_state()
    yield
    reset_ts_ast_state()


# ── graph assembly ───────────────────────────────────────────────────────


def test_build_graph_payload_deterministic():
    a = _build(TRACKED)
    b = _build(list(reversed(TRACKED)))  # input order must not matter
    pa = json.dumps(a.to_payload(), sort_keys=True)
    pb = json.dumps(b.to_payload(), sort_keys=True)
    assert pa == pb
    # double-serialisation of the SAME graph is stable too
    assert pa == json.dumps(a.to_payload(), sort_keys=True)


def test_build_graph_telemetry_and_skips():
    g = _build()
    t = g.telemetry
    assert t["parse_failures"] == 1 and t["failed_files"] == [BROKEN]
    assert t["dts_skipped"] == 1
    assert t["non_ts_skipped"] == 1  # server/main.py
    assert t["files_parsed"] == 5 and BROKEN not in t["parsed_files"]
    assert t["resolution_histogram"] == {
        "package_external": 2, "relative": 3, "tsconfig_alias": 1,
        "unresolved": 1, "workspace": 1,
    }
    assert list(g.exports_index) == sorted(g.exports_index)


def test_build_graph_survives_raising_stage_fns():
    def bad_defs(fp: FileParse) -> list[DefSpan]:
        raise RuntimeError("boom")

    def bad_resolve(*a: Any, **k: Any) -> list[ResolvedEdge]:
        raise RuntimeError("boom")

    g = build_symbol_graph(
        "/repo", TRACKED, parse_fn=stub_parse, defs_fn=bad_defs,
        imports_fn=stub_imports, resolve_fn=bad_resolve,
    )
    assert g.defs == [] and g.resolved == []
    assert g.telemetry["files_parsed"] == 5  # per-file law: parses survive


# ── (a) def-spans → SymbolRange consumer shape ───────────────────────────


def test_flow_spans_kind_mapping_and_order():
    spans = defspans_to_flow_spans(_build())
    btn = spans[BTN]
    assert [r.name for r in btn] == ["Button", "buttonVariants", "useStyles"]
    # AMENDMENT-1: no legacy view here → wrapped component ('forwardRef')
    # maps to 'const' (a const-assignment form); still flow-eligible.
    assert btn[0].kind == "const"
    assert (btn[0].start_line, btn[0].end_line) == (5, 9)
    assert btn[2].kind == "local"           # non-exported → regex parity
    db = spans[DB]
    assert [r.name for r in db] == ["Db", "constructor", "query"]
    assert db[0].kind == "class" and db[0].parent is None
    assert db[1].kind == "constructor" and db[1].parent == "Db"
    assert db[2].kind == "method" and db[2].parent == "Db"


def test_merge_keeps_regex_only_names_ast_wins_on_clash():
    regex = [
        SymbolRange(name="ButtonProps", start_line=3, end_line=4,
                    kind="type"),
        SymbolRange(name="Button", start_line=5, end_line=21, kind="const"),
        SymbolRange(name="legacyHelper", start_line=23, end_line=25,
                    kind="method", parent="Db"),
    ]
    merged = adapter._merge_ranges(_DEFS[BTN], regex)
    by_name = {r.name: r for r in merged}
    # regex-only survives (types are NOT modelled by M1)
    assert by_name["ButtonProps"].kind == "type"
    # AST wins by name: the wrapper-span bleed (5..21) becomes exact 5..9
    assert (by_name["Button"].start_line, by_name["Button"].end_line) == (5, 9)
    # AMENDMENT-1 legacy-form law: regex saw Button as 'const' → 'const'
    assert by_name["Button"].kind == "const"
    # ordering discipline: top-level by start_line, methods appended last
    assert [r.name for r in merged] == [
        "ButtonProps", "Button", "buttonVariants", "useStyles",
        "legacyHelper",
    ]


def test_flow_spans_with_regex_ranges_by_file():
    regex_map = {
        BTN: [SymbolRange(name="ButtonProps", start_line=3, end_line=4,
                          kind="type")],
        "apps/web/src/only-regex.ts": [
            SymbolRange(name="Orphan", start_line=1, end_line=2,
                        kind="function"),
        ],
    }
    spans = defspans_to_flow_spans(_build(), regex_map)
    assert [r.name for r in spans[BTN]][0] == "ButtonProps"
    # a file the graph never parsed keeps its regex ranges untouched
    assert [r.name for r in spans["apps/web/src/only-regex.ts"]] == ["Orphan"]


# ── (b) provenance view ──────────────────────────────────────────────────


def test_provenance_type_only_excluded_and_files_population():
    view = provenance_view(_build())
    assert PAGE in view.files and BROKEN not in view.files
    assert "react" not in view.raw_specs(PAGE)  # purely type-level import
    assert view.raw_specs(PAGE) == [
        "./globals.css", "@/components/button", "@acme/ui", "next/head",
    ]


def test_provenance_s2_contract_resolve_and_occurrences():
    view = provenance_view(_build())
    # tracked-file answers
    assert view.resolve(PAGE, "@/components/button") == BTN
    assert view.resolve(PAGE, "@acme/ui") == UIBTN
    assert view.resolve(DB, "../components/button") == BTN
    # externals / unresolved answer None (S2 classifies externals itself)
    assert view.resolve(PAGE, "next/head") is None
    assert view.resolve(PAGE, "./globals.css") is None
    assert view.resolve(DB, "./missing") is None
    assert view.resolve("nope.ts", "./x") is None
    # per-imported-name weighting; side-effect counts once
    occ = view.spec_occurrences(DB)
    assert occ.count("../components/button") == 2
    occ_page = view.spec_occurrences(PAGE)
    assert occ_page.count("./globals.css") == 1
    assert occ_page.count("next/head") == 1


def test_provenance_655_contract_lookup():
    view = provenance_view(_build())
    assert view.lookup(PAGE, "@/components/button") == (BTN, "workspace")
    assert view.lookup(PAGE, "@acme/ui") == (UIBTN, "workspace")
    assert view.lookup(DB, "../components/button") == (BTN, "local")
    assert view.lookup(PAGE, "next/head") == (None, "package")
    assert view.lookup(PAGE, "./globals.css") == (None, "unresolved")
    # defensive: relative WITHOUT a target must not claim "local"
    assert view.lookup(DB, "./missing") == (None, "unresolved")
    # unknown spec / file → None → caller falls back to _resolve_spec
    assert view.lookup(PAGE, "./unknown") is None
    assert view.lookup("nope.ts", "./x") is None


# ── flags / kill-switch / fallback law ───────────────────────────────────


def test_kill_switch_all_entry_points_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAULTLINE_TS_AST", "0")
    assert not ts_ast_enabled()
    assert ast_symbol_ranges("/repo", BTN, "export const x = 1;", []) is None
    assert repo_provenance("/repo", TRACKED) is None
    assert current_provenance(frozenset(TRACKED)) is None


def test_entry_flag_skeleton():
    assert not ts_ast_entry_enabled()
    assert entry_signals(_build()) is None
    import os
    os.environ["FAULTLINE_TS_AST_ENTRY"] = "1"
    try:
        assert ts_ast_entry_enabled()
        assert entry_signals(_build()) is None  # skeleton: still no signal
    finally:
        del os.environ["FAULTLINE_TS_AST_ENTRY"]


def _m1_importable() -> bool:
    try:
        from faultline.pipeline_v2.ts_ast import parse  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(
    _m1_importable(), reason="M1-M3 merged (Phase B) — fallback moot",
)
def test_phase_a_flag_on_without_m1m3_falls_back():
    assert ts_ast_enabled()  # default ON
    assert ast_symbol_ranges("/repo", BTN, "export const x = 1;", []) is None
    assert repo_provenance("/repo", TRACKED) is None


def test_non_ts_and_dts_paths_answer_none():
    adapter._FNS_CACHE[0] = _stub_fns()  # autouse fixture resets
    assert ast_symbol_ranges("/repo", PY, "x=1", []) is None
    assert ast_symbol_ranges("/repo", DTS, "export {}", []) is None


# ── registry (hooks B/C plumbing) ────────────────────────────────────────


def test_repo_provenance_registry_and_identity():
    adapter._FNS_CACHE[0] = _stub_fns()  # autouse fixture resets
    view = repo_provenance("/repo", TRACKED)
    assert view is not None and view.resolve(PAGE, "@acme/ui") == UIBTN
    # memo: same (root, tracked) → same object
    assert repo_provenance("/repo", list(TRACKED)) is view
    # current: exact tracked set → view; different population → None
    assert current_provenance(frozenset(TRACKED)) is view
    assert current_provenance(frozenset(TRACKED[:3])) is None
    reset_ts_ast_state()
    assert current_provenance(frozenset(TRACKED)) is None


def test_parse_failed_file_not_in_view():
    adapter._FNS_CACHE[0] = _stub_fns()  # autouse fixture resets
    view = repo_provenance("/repo", TRACKED)
    assert view is not None
    assert BROKEN not in view.files  # hook consumers fall back per-file


# ── Hook A end-to-end through extract_signatures ─────────────────────────


_BTN_SOURCE = (
    'import * as React from "react";\n'
    "\n"
    "export type ButtonProps = { label: string };\n"
    "\n"
    "export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(\n"
    "  function ButtonImpl(props, ref) {\n"
    "    return null;\n"
    "  },\n"
    ");\n"
    "\n"
    "export const buttonVariants = { primary: 1 };\n"
)


def test_hook_a_upgrades_and_kill_switch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
):
    from faultline.analyzer.ast_extractor import (
        extract_signatures,
        extract_symbol_ranges,
    )

    rel = "apps/web/src/components/button.tsx"
    abs_file = tmp_path / rel
    abs_file.parent.mkdir(parents=True)
    abs_file.write_text(_BTN_SOURCE)

    regex_expected = extract_symbol_ranges(_BTN_SOURCE)
    regex_button = next(r for r in regex_expected if r.name == "Button")
    assert regex_button.end_line == 10  # the wrapper-span bleed we fix

    # flag OFF → byte-identical regex ranges (kill-switch)
    monkeypatch.setenv("FAULTLINE_TS_AST", "0")
    sig_off = extract_signatures([rel], str(tmp_path))[rel]
    assert [
        (r.name, r.start_line, r.end_line, r.kind)
        for r in sig_off.symbol_ranges
    ] == [
        (r.name, r.start_line, r.end_line, r.kind) for r in regex_expected
    ]

    # flag ON + stub pipeline → AST-precise Button, regex-only type kept
    monkeypatch.delenv("FAULTLINE_TS_AST", raising=False)
    adapter._FNS_CACHE[0] = _stub_fns()  # autouse fixture resets
    sig_on = extract_signatures([rel], str(tmp_path))[rel]
    by_name = {r.name: r for r in sig_on.symbol_ranges}
    assert (by_name["Button"].start_line, by_name["Button"].end_line) == (5, 9)
    # legacy saw `export const Button = …` → the legacy-form law → const
    assert by_name["Button"].kind == "const"
    assert by_name["ButtonProps"].kind == "type"       # regex-only survives
    assert by_name["useStyles"].kind == "local"        # AST non-exported
    names = [r.name for r in sig_on.symbol_ranges]
    assert names.index("ButtonProps") < names.index("Button")


def test_interior_key_tag_shape():
    assert INTERIOR_KEY_TAG.endswith(":") and len(INTERIOR_KEY_TAG) > 2


# ── AMENDMENT-1 mapping table ────────────────────────────────────────────


def test_amendment1_enum_never_flow_eligible():
    defs = [
        DefSpan(file="x.ts", name="Color", kind="enum", start_line=1,
                end_line=5, exported=True),
        DefSpan(file="x.ts", name="Mode", kind="enum", start_line=7,
                end_line=9, exported=False),
    ]
    merged = adapter._merge_ranges(defs, [])
    kinds = {r.name: r.kind for r in merged}
    assert kinds == {"Color": "enum", "Mode": "enum"}


def test_amendment1_component_legacy_form_law():
    comp = DefSpan(file="x.tsx", name="Card", kind="component",
                   start_line=1, end_line=9, exported=True)
    wrapped = DefSpan(file="x.tsx", name="Memo", kind="component",
                      start_line=11, end_line=15, exported=True,
                      wrapper="memo")
    # legacy saw an fn-declaration → 'function' wins over wrapper fallback
    legacy_fn = [SymbolRange(name="Card", start_line=1, end_line=20,
                             kind="function")]
    kinds = {r.name: r.kind
             for r in adapter._merge_ranges([comp, wrapped], legacy_fn)}
    assert kinds["Card"] == "function"
    # no legacy view: wrapped → 'const'; bare declaration → 'function'
    assert kinds["Memo"] == "const"
    kinds2 = {r.name: r.kind for r in adapter._merge_ranges([comp], [])}
    assert kinds2["Card"] == "function"


def test_amendment1_wrapper_separate_channel():
    ch = adapter.wrapper_channel(_build())
    assert ch[BTN] == {"Button": "forwardRef"}
    assert ch[UIBTN] == {"UiButton": "memo"}
    assert DB not in ch  # no wrapped defs there
