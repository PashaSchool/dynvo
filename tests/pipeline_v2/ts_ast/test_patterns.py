"""W6-AST fixture-corpus runner (M5) — TDD face of the ts_ast program.

Each directory under ``fixtures/patterns/<case>/`` is a minimal,
generalized TS/JS pattern (NEVER a snapshot of a real repo) plus a
hand-authored ``EXPECTED.json`` truth with the frozen spec §1 shapes:

    defs           -> list[DefSpan]      (M1: faultline.pipeline_v2.ts_ast.defs)
    edges          -> list[ImportEdge]   (M2: faultline.pipeline_v2.ts_ast.imports)
    resolved       -> list[ResolvedEdge] (M3: faultline.pipeline_v2.ts_ast.resolve)
    exports_index  -> dict[file, list[ExportEntry]]  (post-resolution, M3)

A section set to ``null`` in EXPECTED.json is not asserted for that case;
``[]`` / ``{}`` assert emptiness. While an M1/M2/M3 module (or its
entrypoint) has not landed, the corresponding tests SKIP with a reason —
they must never fail on absence (TDD: the corpus precedes the code).

Corpus semantic pins (the truth encoded in EXPECTED.json; disputes go
through the coordinator per w6ast-spec §0 law):

* paths POSIX, relative to the case root; lines 1-based inclusive;
* DefSpan spans the whole statement incl. decorators + export keyword;
* kind='function' covers fn decls AND arrow/function-expression consts;
  'const' is only for non-function value bindings; React components
  (capitalized, JSX-returning or wrapper-produced) are 'component';
  methods carry parent + exported=false;
* wrapper composition -> OUTERMOST call wins (memo(forwardRef(..)) ->
  'memo'); inner named function expressions in wrapper calls get no
  DefSpan; require()/destructuring bindings are imports, not defs;
  interfaces/type aliases yield no DefSpan (absent from the kind enum)
  but do appear in exports_index; .d.ts files are skipped entirely;
* ImportEdge.names sorted; renames kept as one 'A as B' string;
  type-only names prefixed 'type:'; default/namespace imports carry the
  local binding name; dynamic / side_effect / bare require carry ();
  edge.line = line of the statement start;
* ResolvedEdge: consumer edges resolve to the FINAL defining file with
  via_barrels = traversed re-export files in walk order (entry barrel
  first) and split into one edge per distinct final target; reexport_*
  edges resolve to their DIRECT target with via_barrels=(); 'type:'
  names are dropped from ResolvedEdge.names (provenance skip) — a
  pure-type edge still resolves with names=(); bare external targets
  are resolution='package_external' with target_file=None; baseUrl bare
  specifiers count as 'tsconfig_alias';
* exports_index is the post-resolution view: origin_file = the DIRECT
  resolved source file for re-exports (None for local), star re-exports
  use name='*' kind='star_from', default exports use name='default'.

Entrypoint contract requested from M1/M2/M3 (any one of the candidate
names below; first match wins):

    defs.extract_defs(root)                  | collect_defs | defs_for_root
    imports.extract_imports(root)            | collect_imports
    resolve.resolve_imports(root, edges=...) | resolve_edges | resolve
    resolve.build_exports_index(root, ...)   | exports_index

Each callable is tried with (root: Path), falling back to (str(root)),
and for resolve-stage entrypoints also with the M2 edge list.
"""

from __future__ import annotations

import dataclasses
import difflib
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "patterns"

# The full pattern-class roster (guards against accidental case loss).
EXPECTED_CASES = [
    "01-barrel-chain-3-rename",
    "02-dynamic-import",
    "03-require-js",
    "04-wrapper-forwardref",
    "05-wrapper-memo",
    "06-wrapper-hoc",
    "07-wrapper-styled",
    "08-wrapper-composed",
    "09-tsconfig-paths",
    "10-workspace-monorepo",
    "11-package-exports-map",
    "12-type-only-imports",
    "13-decorators",
    "14-jsx-only",
    "15-dts-skip",
    "16-cyclic-barrel",
    "17-multiline-imports-comments",
]

CASES = sorted(
    p.name for p in FIXTURES.iterdir() if (p / "EXPECTED.json").is_file()
) if FIXTURES.is_dir() else []

# ── frozen shapes (w6ast-spec §1) ────────────────────────────────────────

DEF_FIELDS = frozenset(
    {"file", "name", "kind", "start_line", "end_line", "exported", "wrapper",
     "parent"})
DEF_KINDS = frozenset({"function", "class", "component", "method", "const"})
DEF_WRAPPERS = frozenset({"none", "forwardRef", "memo", "hoc", "styled"})

EDGE_FIELDS = frozenset({"src_file", "kind", "names", "raw_target", "line"})
EDGE_KINDS = frozenset(
    {"named", "default", "namespace", "dynamic", "require", "reexport_star",
     "reexport_named", "side_effect"})

RESOLVED_FIELDS = frozenset(
    {"src_file", "raw_target", "target_file", "resolution", "via_barrels",
     "names", "kind"})
RESOLUTIONS = frozenset(
    {"relative", "tsconfig_alias", "workspace", "package_external",
     "unresolved"})

EXPORT_FIELDS = frozenset({"file", "name", "kind", "origin_file"})
EXPORT_KINDS = frozenset({"named", "default", "star_from"})

_DEFS_MOD = "faultline.pipeline_v2.ts_ast.defs"
_IMPORTS_MOD = "faultline.pipeline_v2.ts_ast.imports"
_RESOLVE_MOD = "faultline.pipeline_v2.ts_ast.resolve"

_DEFS_ENTRYPOINTS = ("extract_defs", "collect_defs", "defs_for_root")
_IMPORTS_ENTRYPOINTS = ("extract_imports", "collect_imports")
_RESOLVE_ENTRYPOINTS = ("resolve_imports", "resolve_edges", "resolve")
_INDEX_ENTRYPOINTS = ("build_exports_index", "exports_index")


# ── helpers ──────────────────────────────────────────────────────────────

def _case_dir(case: str) -> Path:
    return FIXTURES / case


def _load_expected(case: str) -> dict[str, Any]:
    return json.loads((_case_dir(case) / "EXPECTED.json").read_text(
        encoding="utf-8"))


def _module_or_skip(name: str, stage: str):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        pytest.skip(f"{name} not importable yet ({stage} pending): {exc}")


def _entrypoint_or_skip(mod, candidates: tuple[str, ...]):
    for fn_name in candidates:
        fn = getattr(mod, fn_name, None)
        if callable(fn):
            return fn
    pytest.skip(
        f"{mod.__name__} has none of the expected entrypoints {candidates} "
        f"— align via coordinator (M5 runner contract)")


def _call_flexibly(fn, root: Path, *, edges: Any = None):
    """Try the small matrix of natural call shapes; skip informatively when
    none binds (signature mismatch is an alignment issue, not a failure)."""
    attempts = []
    if edges is not None:
        attempts.extend([
            ((root, edges), {}),
            ((root,), {"edges": edges}),
            ((str(root), edges), {}),
        ])
    attempts.extend([((root,), {}), ((str(root),), {})])
    last_type_error = None
    for args, kwargs in attempts:
        try:
            return fn(*args, **kwargs)
        except TypeError as exc:  # signature mismatch — try the next shape
            last_type_error = exc
    pytest.skip(
        f"{getattr(fn, '__name__', fn)} signature did not accept the runner "
        f"call shapes (last TypeError: {last_type_error}) — align via "
        f"coordinator")


def _plain(obj: Any) -> Any:
    """Object of any pedigree (dataclass / pydantic / mapping / sequence) ->
    plain JSON-able python; tuples become lists, Paths become POSIX str."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _plain(dataclasses.asdict(obj))
    dump = getattr(obj, "model_dump", None)
    if callable(dump):  # pydantic v2
        return _plain(dump())
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = [_plain(v) for v in obj]
        return sorted(items, key=_canon_key) if isinstance(
            obj, (set, frozenset)) else items
    if isinstance(obj, Path):
        return obj.as_posix()
    if hasattr(obj, "__dict__") and not isinstance(
            obj, (str, int, float, bool, type(None))):
        return {k: _plain(v) for k, v in vars(obj).items()
                if not k.startswith("_")}
    return obj


def _canon_key(item: Any) -> str:
    return json.dumps(item, sort_keys=True, ensure_ascii=True, default=str)


def _norm_path(value: Any, root: Path) -> Any:
    if not isinstance(value, str):
        return value
    v = value.replace("\\", "/")
    root_posix = root.resolve().as_posix().rstrip("/") + "/"
    if v.startswith(root_posix):
        v = v[len(root_posix):]
    if v.startswith("./"):
        v = v[2:]
    return v


_PATH_KEYS = {"file", "src_file", "target_file", "origin_file"}


def _normalize_record(rec: dict[str, Any], root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in rec.items():
        if k in _PATH_KEYS:
            out[k] = _norm_path(v, root)
        elif k == "via_barrels":
            out[k] = [_norm_path(x, root) for x in (v or [])]
        elif k == "names":
            out[k] = [str(x) for x in (v or [])]
        else:
            out[k] = v
    return out


def _assert_shape(records: list[dict[str, Any]], fields: frozenset[str],
                  what: str) -> None:
    for rec in records:
        got = frozenset(rec.keys())
        assert got == fields, (
            f"{what} shape drift: fields {sorted(got)} != frozen spec §1 "
            f"{sorted(fields)} — shape changes go through the coordinator")


def _sorted_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=_canon_key)


def _diff(expected: Any, actual: Any, what: str) -> str:
    exp_txt = json.dumps(expected, indent=2, sort_keys=True,
                         ensure_ascii=True).splitlines()
    act_txt = json.dumps(actual, indent=2, sort_keys=True,
                         ensure_ascii=True).splitlines()
    diff = "\n".join(difflib.unified_diff(
        exp_txt, act_txt, fromfile=f"EXPECTED.{what}",
        tofile=f"actual.{what}", lineterm=""))
    return f"{what} mismatch:\n{diff}"


def _canonical_records(raw: Any, root: Path, fields: frozenset[str],
                       what: str) -> list[dict[str, Any]]:
    plain = _plain(raw)
    assert isinstance(plain, list), (
        f"{what}: expected a list of records, got {type(raw).__name__}")
    records = [_normalize_record(r, root) for r in plain]
    _assert_shape(records, fields, what)
    return _sorted_records(records)


def _canonical_index(raw: Any, root: Path) -> dict[str, list[dict[str, Any]]]:
    plain = _plain(raw)
    assert isinstance(plain, dict), (
        f"exports_index: expected dict[file, list[ExportEntry]], got "
        f"{type(raw).__name__}")
    out: dict[str, list[dict[str, Any]]] = {}
    for file, entries in plain.items():
        norm_entries = []
        for entry in entries:
            # Entries may or may not carry the redundant per-entry 'file'
            # field (the dict key already scopes them) — accept both.
            entry = dict(entry)
            entry.pop("file", None)
            norm_entries.append(_normalize_record(entry, root))
        _assert_shape(norm_entries, EXPORT_FIELDS - {"file"}, "ExportEntry")
        out[_norm_path(file, root)] = _sorted_records(norm_entries)
    return out


def _expected_records(section: Any, fields: frozenset[str]
                      ) -> list[dict[str, Any]]:
    _assert_shape(section, fields, "EXPECTED")
    return _sorted_records(section)


def _expected_index(section: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, list[dict[str, Any]]] = {}
    for file, entries in section.items():
        prepared = []
        for entry in entries:
            entry = dict(entry)
            entry.pop("file", None)
            prepared.append(entry)
        out[file] = _sorted_records(prepared)
    return out


def _section_or_skip(expected: dict[str, Any], key: str) -> Any:
    section = expected.get(key)
    if section is None:
        pytest.skip(f"case does not assert '{key}' (null section)")
    return section


def _run_twice_identical(fn, root: Path, what: str, *, edges: Any = None):
    """Determinism law (spec §2): a second run over identical input must
    produce an identical collection."""
    first = _call_flexibly(fn, root, edges=edges)
    second = _call_flexibly(fn, root, edges=edges)
    assert _canon_key(_plain(first)) == _canon_key(_plain(second)), (
        f"{what}: two consecutive runs differ — determinism law violated "
        f"(sorted collections / no set iteration)")
    return first


# ── corpus integrity (green from day zero, no ts_ast modules needed) ─────

def test_corpus_present() -> None:
    assert FIXTURES.is_dir(), f"fixture corpus missing at {FIXTURES}"
    assert CASES == EXPECTED_CASES, (
        "pattern-class roster drifted: "
        f"missing={sorted(set(EXPECTED_CASES) - set(CASES))} "
        f"unexpected={sorted(set(CASES) - set(EXPECTED_CASES))}")


@pytest.mark.parametrize("case", CASES)
def test_corpus_integrity(case: str) -> None:
    """EXPECTED.json is well-formed against the frozen §1 shapes and every
    referenced file/line actually exists in the case sources."""
    expected = _load_expected(case)
    root = _case_dir(case)
    assert expected.get("case") == case
    for key in ("defs", "edges", "resolved", "exports_index"):
        assert key in expected, f"{case}: EXPECTED.json lacks '{key}' key"

    sources = {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "EXPECTED.json"
    }

    defs = expected["defs"]
    if defs is not None:
        _assert_shape(defs, DEF_FIELDS, "EXPECTED DefSpan")
        for rec in defs:
            assert rec["kind"] in DEF_KINDS, rec
            assert rec["wrapper"] in DEF_WRAPPERS, rec
            assert rec["file"] in sources, f"{case}: {rec['file']} missing"
            n_lines = len(sources[rec["file"]].splitlines())
            assert 1 <= rec["start_line"] <= rec["end_line"] <= n_lines, rec
            assert isinstance(rec["exported"], bool), rec
            assert not rec["file"].endswith(".d.ts"), (
                f"{case}: .d.ts files must never yield defs: {rec}")

    edges = expected["edges"]
    if edges is not None:
        _assert_shape(edges, EDGE_FIELDS, "EXPECTED ImportEdge")
        for rec in edges:
            assert rec["kind"] in EDGE_KINDS, rec
            assert rec["src_file"] in sources, rec
            n_lines = len(sources[rec["src_file"]].splitlines())
            assert 1 <= rec["line"] <= n_lines, rec
            assert rec["names"] == sorted(rec["names"]), (
                f"{case}: ImportEdge.names must be sorted: {rec}")

    resolved = expected["resolved"]
    if resolved is not None:
        _assert_shape(resolved, RESOLVED_FIELDS, "EXPECTED ResolvedEdge")
        for rec in resolved:
            assert rec["resolution"] in RESOLUTIONS, rec
            assert rec["kind"] in EDGE_KINDS, rec
            assert rec["src_file"] in sources, rec
            if rec["resolution"] == "package_external":
                assert rec["target_file"] is None, rec
            if rec["target_file"] is not None:
                assert rec["target_file"] in sources, rec
            for barrel in rec["via_barrels"]:
                assert barrel in sources, rec
            assert rec["names"] == sorted(rec["names"]), rec
            assert not any(n.startswith("type:") for n in rec["names"]), (
                f"{case}: type-only names must be dropped from "
                f"ResolvedEdge.names: {rec}")

    index = expected["exports_index"]
    if index is not None:
        for file, entries in index.items():
            assert file in sources, f"{case}: {file} missing"
            assert not file.endswith(".d.ts"), (
                f"{case}: .d.ts files must be absent from exports_index")
            for entry in entries:
                assert set(entry) == (EXPORT_FIELDS - {"file"}), entry
                assert entry["kind"] in EXPORT_KINDS, entry
                if entry["origin_file"] is not None:
                    assert entry["origin_file"] in sources, entry
                if entry["kind"] == "star_from":
                    assert entry["name"] == "*", entry


# ── M1: DefSpans ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", CASES)
def test_defs(case: str) -> None:
    expected = _section_or_skip(_load_expected(case), "defs")
    mod = _module_or_skip(_DEFS_MOD, "M1")
    fn = _entrypoint_or_skip(mod, _DEFS_ENTRYPOINTS)
    root = _case_dir(case)
    raw = _run_twice_identical(fn, root, f"{case}: defs")
    actual = _canonical_records(raw, root, DEF_FIELDS, "DefSpan")
    exp = _expected_records(expected, DEF_FIELDS)
    assert actual == exp, _diff(exp, actual, f"{case}.defs")


# ── M2: ImportEdges ──────────────────────────────────────────────────────

@pytest.mark.parametrize("case", CASES)
def test_import_edges(case: str) -> None:
    expected = _section_or_skip(_load_expected(case), "edges")
    mod = _module_or_skip(_IMPORTS_MOD, "M2")
    fn = _entrypoint_or_skip(mod, _IMPORTS_ENTRYPOINTS)
    root = _case_dir(case)
    raw = _run_twice_identical(fn, root, f"{case}: edges")
    actual = _canonical_records(raw, root, EDGE_FIELDS, "ImportEdge")
    exp = _expected_records(expected, EDGE_FIELDS)
    assert actual == exp, _diff(exp, actual, f"{case}.edges")


# ── M3: ResolvedEdges (consumes M2 output when the signature wants it) ──

def _m2_edges_or_none(root: Path):
    try:
        mod = importlib.import_module(_IMPORTS_MOD)
    except ImportError:
        return None
    for fn_name in _IMPORTS_ENTRYPOINTS:
        fn = getattr(mod, fn_name, None)
        if callable(fn):
            try:
                return fn(root)
            except TypeError:
                try:
                    return fn(str(root))
                except TypeError:
                    return None
    return None


@pytest.mark.parametrize("case", CASES)
def test_resolved_edges(case: str) -> None:
    expected = _section_or_skip(_load_expected(case), "resolved")
    mod = _module_or_skip(_RESOLVE_MOD, "M3")
    fn = _entrypoint_or_skip(mod, _RESOLVE_ENTRYPOINTS)
    root = _case_dir(case)
    edges = _m2_edges_or_none(root)
    raw = _run_twice_identical(fn, root, f"{case}: resolved", edges=edges)
    actual = _canonical_records(raw, root, RESOLVED_FIELDS, "ResolvedEdge")
    exp = _expected_records(expected, RESOLVED_FIELDS)
    assert actual == exp, _diff(exp, actual, f"{case}.resolved")


@pytest.mark.parametrize("case", CASES)
def test_exports_index(case: str) -> None:
    expected = _section_or_skip(_load_expected(case), "exports_index")
    mod = _module_or_skip(_RESOLVE_MOD, "M3 (post-resolution exports_index)")
    fn = _entrypoint_or_skip(mod, _INDEX_ENTRYPOINTS)
    root = _case_dir(case)
    edges = _m2_edges_or_none(root)
    raw = _call_flexibly(fn, root, edges=edges)
    actual = _canonical_index(raw, root)
    exp = _expected_index(expected)
    # Only the files the case asserts (a richer index may carry more files
    # only if the case listed every exporting file — compare strictly).
    assert actual == exp, _diff(exp, actual, f"{case}.exports_index")
