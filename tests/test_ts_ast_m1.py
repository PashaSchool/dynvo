"""W6 M1 unit tests — ts_ast/parse.py + ts_ast/defs.py (spec §3.M1).

Minimal local fixtures in M5 shape (inline sources, generic patterns —
no repo-specific vocabulary) until the M5 fixture corpus lands.
"""

from __future__ import annotations

import pytest

from faultline.pipeline_v2.ts_ast import parse as ts_parse
from faultline.pipeline_v2.ts_ast.defs import (
    DefSpan,
    defs_from_payload,
    defs_to_payload,
    extract_defs,
    extract_defs_cached,
)
from faultline.pipeline_v2.ts_ast.parse import (
    FileParse,
    cache_key,
    cached_payload,
    content_hash_of,
    is_active,
    lang_for_path,
    parse_file,
    telemetry_snapshot,
)

pytestmark = pytest.mark.skipif(
    not ts_parse.TREE_SITTER_AVAILABLE,
    reason="tree-sitter extras not installed — ts_ast degrades to regex",
)


@pytest.fixture(autouse=True)
def _clean_state():
    ts_parse.reset_state()
    yield
    ts_parse.reset_state()


def _defs(path: str, source: str) -> list[DefSpan]:
    fp = parse_file(path, source.encode())
    assert fp is not None, f"parse_file unexpectedly degraded for {path}"
    return extract_defs(fp, source.encode())


def _by_name(spans: list[DefSpan], name: str, parent: str | None = None) -> DefSpan:
    hits = [d for d in spans if d.name == name and d.parent == parent]
    assert hits, f"no DefSpan named {name!r} (parent={parent!r}) in {spans}"
    return hits[0]


# ── parse.py ─────────────────────────────────────────────────────────────


def test_lang_for_path_dialects_and_dts_skip():
    assert lang_for_path("src/a.ts") == "ts"
    assert lang_for_path("src/a.mts") == "ts"
    assert lang_for_path("src/a.cts") == "ts"
    assert lang_for_path("src/a.tsx") == "tsx"
    assert lang_for_path("src/a.js") == "js"
    assert lang_for_path("src/a.mjs") == "js"
    assert lang_for_path("src/a.cjs") == "js"
    assert lang_for_path("src/a.jsx") == "jsx"
    # Ambient declaration files are skipped (spec §3.M5).
    assert lang_for_path("src/a.d.ts") is None
    assert lang_for_path("src/a.d.mts") is None
    assert lang_for_path("src/a.d.cts") is None
    # Non-TS/JS.
    assert lang_for_path("src/a.py") is None
    assert lang_for_path("Makefile") is None


def test_kill_switch_deactivates_layer(monkeypatch):
    monkeypatch.setenv(ts_parse.ENV_FLAG, "0")
    assert not is_active()
    assert parse_file("a.ts", b"export const x = 1;\n") is None
    assert extract_defs_cached("a.ts", b"export const x = 1;\n") is None


def test_entry_flag_default_off():
    assert not ts_parse.entry_enabled()


def test_parse_file_memo_reuses_tree():
    src = b"export const x = 1;\n"
    fp1 = parse_file("a.ts", src)
    fp2 = parse_file("b.ts", src)  # same content, different path
    assert fp1 is not None and fp2 is not None
    assert fp1.tree is fp2.tree
    tele = telemetry_snapshot()
    assert tele["parses"] == 1
    assert tele["memo_hits"] == 1


def test_parse_file_recovers_from_partial_errors():
    # Broken tail — tree-sitter recovers locally; file must NOT degrade.
    src = b"export function ok() { return 1; }\nconst broken = (;\n"
    fp = parse_file("a.ts", src)
    assert fp is not None
    assert telemetry_snapshot()["files_with_errors"] == 1
    spans = extract_defs(fp, src)
    assert any(d.name == "ok" for d in spans)


def test_cache_key_varies_by_namespace_lang_hash():
    h = content_hash_of(b"x")
    assert cache_key("defs", "ts", h) != cache_key("imports", "ts", h)
    assert cache_key("defs", "ts", h) != cache_key("defs", "tsx", h)
    assert cache_key("defs", "ts", h) != cache_key("defs", "ts", content_hash_of(b"y"))


class _FakeBackend:
    def __init__(self):
        self.store: dict[tuple[str, str], dict] = {}
        self.gets = 0
        self.sets = 0

    def get(self, kind, key):
        self.gets += 1
        return self.store.get((str(kind), key))

    def set(self, kind, key, value, ttl_seconds=None):
        self.sets += 1
        self.store[(str(kind), key)] = value


def test_cached_payload_roundtrip_and_none_not_cached():
    backend = _FakeBackend()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return {"defs": [{"name": "x"}]}

    h = content_hash_of(b"src")
    p1 = cached_payload(backend, "defs", "ts", h, compute)
    p2 = cached_payload(backend, "defs", "ts", h, compute)
    assert p1 == p2 == {"defs": [{"name": "x"}]}
    assert calls["n"] == 1  # second call replayed from cache
    assert backend.sets == 1

    def compute_none():
        calls["n"] += 1
        return None

    h2 = content_hash_of(b"other")
    assert cached_payload(backend, "defs", "ts", h2, compute_none) is None
    assert cached_payload(backend, "defs", "ts", h2, compute_none) is None
    assert calls["n"] == 3  # failures recompute — never cached


# ── defs.py: functions + arrow consts ────────────────────────────────────


def test_export_function_and_async_arrow_spans_exact():
    src = (
        "export async function loadThing(id: string) {\n"   # 1
        "  const raw = await fetch(id);\n"                   # 2
        "  return raw.json();\n"                             # 3
        "}\n"                                                # 4
        "\n"                                                 # 5
        "export const saveThing = async (input: Input) => {\n"  # 6
        "  await persist(input);\n"                          # 7
        "  return true;\n"                                   # 8
        "};\n"                                               # 9
        "\n"                                                 # 10
        "const helper = () => {\n"                           # 11
        "  return 1;\n"                                      # 12
        "};\n"                                               # 13
    )
    spans = _defs("api/things.ts", src)
    load = _by_name(spans, "loadThing")
    assert (load.kind, load.exported) == ("function", True)
    assert (load.start_line, load.end_line) == (1, 4)
    save = _by_name(spans, "saveThing")
    assert (save.kind, save.exported) == ("function", True)
    # THE regression the regex had: end = next-export-minus-one / EOF.
    assert (save.start_line, save.end_line) == (6, 9)
    helper = _by_name(spans, "helper")
    assert (helper.exported, helper.start_line, helper.end_line) == (False, 11, 13)


def test_wrapper_const_span_covers_inline_handler_body():
    # `const POST = wrap(async () => …)` — the wrapper-span class: the
    # span must cover the whole statement (inline body included).
    src = (
        "const POST = wrap(async (req: Request) => {\n"  # 1
        "  const body = await req.json();\n"             # 2
        "  return respond(body);\n"                      # 3
        "});\n"                                          # 4
        "export { POST };\n"                             # 5
    )
    spans = _defs("app/api/route.ts", src)
    post = _by_name(spans, "POST")
    assert (post.start_line, post.end_line) == (1, 4)
    assert post.exported is True  # export-clause post-pass
    assert post.kind == "const"
    assert post.wrapper == "none"


def test_multi_declarator_gets_per_declarator_spans():
    src = (
        "export const first = () => {\n"  # 1
        "  return 1;\n"                   # 2
        "}, second = 2;\n"                # 3
    )
    spans = _defs("a.ts", src)
    first = _by_name(spans, "first")
    second = _by_name(spans, "second")
    assert (first.start_line, first.end_line) == (1, 3)
    assert first.kind == "function"
    assert second.kind == "const"
    assert second.exported is True


def test_overload_signatures_skipped_only_impl_emitted():
    src = (
        "export function pick(a: string): void;\n"
        "export function pick(a: number): void;\n"
        "export function pick(a: unknown): void {\n"
        "  return;\n"
        "}\n"
    )
    spans = _defs("a.ts", src)
    picks = [d for d in spans if d.name == "pick"]
    assert len(picks) == 1
    assert (picks[0].start_line, picks[0].end_line) == (3, 5)


def test_satisfies_and_bare_let_and_destructuring():
    src = (
        "export const config = { a: 1 } satisfies Config;\n"
        "let cache;\n"
        "const { part } = load();\n"
    )
    spans = _defs("a.ts", src)
    assert _by_name(spans, "config").kind == "const"
    assert all(d.name not in ("cache", "part") for d in spans)


# ── defs.py: React components + wrappers ─────────────────────────────────


def test_plain_jsx_component_detected_without_wrapper():
    src = (
        "export function StatusCard({ label }: Props) {\n"
        "  return <section>{label}</section>;\n"
        "}\n"
        "export const smallHelper = () => 1;\n"
    )
    spans = _defs("ui/card.tsx", src)
    card = _by_name(spans, "StatusCard")
    assert (card.kind, card.wrapper) == ("component", "none")
    assert _by_name(spans, "smallHelper").kind == "function"


def test_forwardref_and_memo_wrappers():
    src = (
        "const FancyInput = React.forwardRef<HTMLInputElement, P>((props, ref) => {\n"
        "  return <input ref={ref} {...props} />;\n"
        "});\n"
        "export const Row = memo(({ id }: P) => <li>{id}</li>);\n"
    )
    spans = _defs("ui/input.tsx", src)
    fancy = _by_name(spans, "FancyInput")
    assert (fancy.kind, fancy.wrapper, fancy.exported) == (
        "component", "forwardRef", False)
    assert (fancy.start_line, fancy.end_line) == (1, 3)
    row = _by_name(spans, "Row")
    assert (row.kind, row.wrapper, row.exported) == ("component", "memo", True)


def test_memo_of_forwardref_takes_outermost_known():
    src = (
        "export const Combo = memo(forwardRef((props, ref) => {\n"
        "  return <div ref={ref} />;\n"
        "}));\n"
    )
    combo = _by_name(_defs("ui/combo.tsx", src), "Combo")
    assert (combo.kind, combo.wrapper) == ("component", "memo")


def test_styled_tagged_template_and_call_form():
    src = (
        "const Wrap = styled.div`\n"
        "  color: red;\n"
        "`;\n"
        "const FancyButton = styled(BaseButton)`\n"
        "  color: blue;\n"
        "`;\n"
    )
    spans = _defs("ui/styles.ts", src)
    wrap = _by_name(spans, "Wrap")
    assert (wrap.kind, wrap.wrapper) == ("component", "styled")
    assert (wrap.start_line, wrap.end_line) == (1, 3)
    fancy = _by_name(spans, "FancyButton")
    assert (fancy.kind, fancy.wrapper) == ("component", "styled")
    assert (fancy.start_line, fancy.end_line) == (4, 6)


def test_hoc_convention_capitalized_vs_lowercase():
    src = (
        "const GuardedPage = withAuth(DashboardView);\n"
        "const translate = withTranslation('ns');\n"
        "export default withRouter(GuardedPage);\n"
    )
    spans = _defs("pages/guarded.tsx", src)
    guarded = _by_name(spans, "GuardedPage")
    assert (guarded.kind, guarded.wrapper) == ("component", "hoc")
    translate = _by_name(spans, "translate")
    assert (translate.kind, translate.wrapper) == ("const", "hoc")
    # export default withRouter(GuardedPage) → anonymous wrapped default.
    default = _by_name(spans, "default")
    assert (default.kind, default.wrapper, default.exported) == (
        "component", "hoc", True)


# ── defs.py: default exports ─────────────────────────────────────────────


def test_default_anonymous_arrow_and_function():
    src_arrow = "export default async () => {\n  return 1;\n};\n"
    d = _by_name(_defs("a.ts", src_arrow), "default")
    assert (d.kind, d.exported, d.start_line, d.end_line) == (
        "function", True, 1, 3)

    src_fn = "export default function () {\n  return 2;\n}\n"
    d2 = _by_name(_defs("b.ts", src_fn), "default")
    assert (d2.kind, d2.exported) == ("function", True)


def test_default_named_function_keeps_name():
    src = (
        "export default function DashboardPage() {\n"
        "  return <main />;\n"
        "}\n"
    )
    spans = _defs("app/page.tsx", src)
    d = _by_name(spans, "DashboardPage")
    assert (d.kind, d.exported) == ("component", True)
    assert all(s.name != "default" for s in spans)


def test_default_memo_named_inner_function():
    src = (
        "export default memo(function BoardGrid({ id }: P) {\n"
        "  return <div />;\n"
        "});\n"
    )
    d = _by_name(_defs("ui/board.tsx", src), "BoardGrid")
    assert (d.kind, d.wrapper, d.exported) == ("component", "memo", True)
    assert (d.start_line, d.end_line) == (1, 3)


def test_default_identifier_marks_existing_def_exported():
    src = (
        "function makeThing() {\n"
        "  return 1;\n"
        "}\n"
        "export default makeThing;\n"
    )
    d = _by_name(_defs("a.ts", src), "makeThing")
    assert d.exported is True
    assert (d.start_line, d.end_line) == (1, 3)


def test_default_anonymous_class_with_methods():
    src = (
        "export default class extends Base {\n"
        "  run() {\n"
        "    return 1;\n"
        "  }\n"
        "}\n"
    )
    spans = _defs("a.ts", src)
    cls = _by_name(spans, "default")
    assert (cls.kind, cls.exported) == ("class", True)
    run = _by_name(spans, "run", parent="default")
    assert run.kind == "method"


def test_default_object_literal_is_const_default():
    d = _by_name(_defs("a.ts", "export default { a: 1 };\n"), "default")
    assert (d.kind, d.exported) == ("const", True)


# ── defs.py: classes + methods ───────────────────────────────────────────


def test_class_methods_constructor_fields_and_abstract_skip():
    src = (
        "@Injectable()\n"                                   # 1
        "export class ThingService {\n"                     # 2
        "  constructor(private db: Db) {}\n"                # 3
        "  async findAll(): Promise<Thing[]> {\n"           # 4
        "    return this.db.all();\n"                       # 5
        "  }\n"                                             # 6
        "  static of(x: number) { return new ThingService(x); }\n"  # 7
        "  private trim = (a: string) => {\n"               # 8
        "    return a.trim();\n"                            # 9
        "  };\n"                                            # 10
        "  get size() { return 1; }\n"                      # 11
        "  limit = 10;\n"                                   # 12
        "}\n"                                               # 13
    )
    spans = _defs("svc/thing.ts", src)
    cls = _by_name(spans, "ThingService")
    assert (cls.kind, cls.exported) == ("class", True)
    # Decorator included in the class span.
    assert (cls.start_line, cls.end_line) == (1, 13)
    ctor = _by_name(spans, "constructor", parent="ThingService")
    assert (ctor.kind, ctor.start_line, ctor.end_line) == ("method", 3, 3)
    find = _by_name(spans, "findAll", parent="ThingService")
    assert (find.start_line, find.end_line) == (4, 6)
    assert _by_name(spans, "of", parent="ThingService").kind == "method"
    trim = _by_name(spans, "trim", parent="ThingService")
    assert (trim.kind, trim.start_line, trim.end_line) == ("method", 8, 10)
    assert _by_name(spans, "size", parent="ThingService").kind == "method"
    # Data field is NOT a method symbol.
    assert all(not (d.name == "limit") for d in spans)


def test_abstract_class_signatures_skipped():
    src = (
        "export abstract class BaseJob {\n"
        "  abstract run(): void;\n"
        "  concrete() { return 1; }\n"
        "}\n"
    )
    spans = _defs("jobs/base.ts", src)
    assert _by_name(spans, "BaseJob").kind == "class"
    assert all(d.name != "run" for d in spans)
    assert _by_name(spans, "concrete", parent="BaseJob").kind == "method"


def test_js_dialect_class_fields_and_var():
    src = (
        "class Widget {\n"              # 1
        "  onClick = (e) => {\n"        # 2
        "    handle(e);\n"              # 3
        "  };\n"                        # 4
        "  render() {\n"                # 5
        "    return null;\n"            # 6
        "  }\n"                         # 7
        "  count = 0;\n"                # 8
        "}\n"                           # 9
        "var legacy = function () {\n"  # 10
        "  return 4;\n"                 # 11
        "};\n"                          # 12
    )
    spans = _defs("ui/widget.js", src)
    onclick = _by_name(spans, "onClick", parent="Widget")
    assert (onclick.kind, onclick.start_line, onclick.end_line) == (
        "method", 2, 4)
    assert _by_name(spans, "render", parent="Widget").kind == "method"
    assert all(d.name != "count" for d in spans)
    legacy = _by_name(spans, "legacy")
    assert (legacy.kind, legacy.start_line, legacy.end_line) == (
        "function", 10, 12)


def test_class_expression_const_walks_methods():
    src = (
        "const Registry = class {\n"
        "  register(x) { return x; }\n"
        "};\n"
    )
    spans = _defs("a.js", src)
    assert _by_name(spans, "Registry").kind == "class"
    assert _by_name(spans, "register", parent="Registry").kind == "method"


def test_method_decorators_extend_span_start():
    # Method decorators are SIBLING nodes in class_body (M5 fixture 13
    # truth): the span starts at the FIRST decorator line.
    src = (
        "export class ItemsController {\n"   # 1
        "  @Get(':id')\n"                    # 2
        "  @Cached()\n"                      # 3
        "  findOne(id: string) {\n"          # 4
        "    return { id };\n"               # 5
        "  }\n"                              # 6
        "  plain() {\n"                      # 7
        "    return [];\n"                   # 8
        "  }\n"                              # 9
        "}\n"                                # 10
    )
    spans = _defs("src/items.controller.ts", src)
    find = _by_name(spans, "findOne", parent="ItemsController")
    assert (find.start_line, find.end_line) == (2, 6)
    plain = _by_name(spans, "plain", parent="ItemsController")
    assert (plain.start_line, plain.end_line) == (7, 9)


def test_require_bindings_are_imports_not_defs():
    # M5 pin 3: require bindings (identifier AND destructured) carry no
    # definition span — the edge belongs to M2.
    src = (
        "const { readData } = require('./io.js');\n"
        "const util = require('./util');\n"
        "const fs = require('fs');\n"
        "const wrapped = notRequire('./x');\n"
        "function run() {\n"
        "  return readData(util.tag) || fs;\n"
        "}\n"
    )
    spans = _defs("src/consume.js", src)
    names = {d.name for d in spans}
    assert "util" not in names and "fs" not in names
    assert "readData" not in names  # destructuring — no single def
    assert "wrapped" in names  # non-require call keeps its const span
    assert "run" in names


def test_cjs_module_exports_marks_defs_exported():
    src = (
        "const tag = 'x';\n"
        "function readData(p) {\n"
        "  return p;\n"
        "}\n"
        "function writeData(p, d) {\n"
        "  return { p, d };\n"
        "}\n"
        "function hidden() { return 0; }\n"
        "module.exports = { readData, io: writeData };\n"
        "exports.tagName = tag;\n"
    )
    spans = _defs("src/io.js", src)
    assert _by_name(spans, "readData").exported is True
    assert _by_name(spans, "writeData").exported is True  # pair value
    assert _by_name(spans, "tag").exported is True        # exports.x = ident
    assert _by_name(spans, "hidden").exported is False


def test_cjs_module_exports_identifier_form():
    src = (
        "function main() {\n"
        "  return 1;\n"
        "}\n"
        "module.exports = main;\n"
    )
    assert _by_name(_defs("a.js", src), "main").exported is True


# ── defs.py: exports bookkeeping + type-space ────────────────────────────


def test_export_clause_alias_and_default_alias():
    src = (
        "function alpha() { return 1; }\n"
        "const beta = 2;\n"
        "export { alpha as default, beta as renamed };\n"
    )
    spans = _defs("a.ts", src)
    assert _by_name(spans, "alpha").exported is True
    assert _by_name(spans, "beta").exported is True


def test_type_only_exports_do_not_mark_values():
    src = (
        "const Shape = 1;\n"
        "const value = 2;\n"
        "export type { Shape };\n"
        "export { type Shape as ShapeT, value };\n"
    )
    spans = _defs("a.ts", src)
    assert _by_name(spans, "Shape").exported is False
    assert _by_name(spans, "value").exported is True


def test_reexports_and_type_space_emit_no_defs():
    src = (
        "export * from './other';\n"
        "export { thing } from './there';\n"
        "export interface Shape { a: string }\n"
        "export type Alias = string;\n"
        "declare function ambient(): void;\n"
        "import { x } from './x';\n"
    )
    assert _defs("a.ts", src) == []


def test_enum_kind_is_enum():
    # AMENDMENT-1: honest kind='enum' (runtime value, NOT flow-eligible;
    # the legacy SymbolRange mapping is M4's documented table).
    src = "export enum Direction {\n  Up,\n  Down,\n}\n"
    d = _by_name(_defs("a.ts", src), "Direction")
    assert (d.kind, d.exported, d.start_line, d.end_line) == (
        "enum", True, 1, 4)
    # `const enum` (TS erasable enum) — same honest kind.
    src2 = "export const enum Mode {\n  A,\n}\n"
    d2 = _by_name(_defs("b.ts", src2), "Mode")
    assert (d2.kind, d2.exported) == ("enum", True)
    # Round-trips through the cache payload with the extended kind set.
    fp = parse_file("a.ts", src.encode())
    assert fp is not None
    spans = extract_defs(fp, src.encode())
    assert defs_from_payload(defs_to_payload(spans), "a.ts") == spans


# ── determinism + serialisation + cache ──────────────────────────────────


_MIXED_FIXTURE = (
    "import { api } from './api';\n"
    "export const ListView = () => {\n"
    "  return <ul />;\n"
    "};\n"
    "const Detail = memo(() => <li />);\n"
    "export default withGuard(Detail);\n"
    "export class Store {\n"
    "  read() { return api.get(); }\n"
    "}\n"
    "function localOnly() { return 1; }\n"
    "export { localOnly };\n"
)


def test_extraction_is_deterministic_and_sorted():
    a = _defs("mix.tsx", _MIXED_FIXTURE)
    b = _defs("mix.tsx", _MIXED_FIXTURE)
    assert a == b
    keys = [(d.start_line, d.parent or "", d.name, d.end_line) for d in a]
    assert keys == sorted(keys)


def test_payload_roundtrip_identity():
    spans = _defs("mix.tsx", _MIXED_FIXTURE)
    payload = defs_to_payload(spans)
    back = defs_from_payload(payload, "mix.tsx")
    assert back == spans
    # Malformed rows dropped, never raise.
    assert defs_from_payload(
        [{"name": "x"}, "junk", {"name": "y", "kind": "nope",
                                 "start_line": 1, "end_line": 1,
                                 "exported": True}],
        "f.ts",
    ) == []


def test_extract_defs_cached_replays_from_backend():
    backend = _FakeBackend()
    src = _MIXED_FIXTURE.encode()
    first = extract_defs_cached("mix.tsx", src, backend)
    assert first is not None and len(first) > 0
    ts_parse.reset_state()  # drop the memo — force the persistent path
    second = extract_defs_cached("mix.tsx", src, backend)
    assert second == first
    assert backend.sets == 1
    assert telemetry_snapshot()["cache_hits"] == 1
    assert telemetry_snapshot()["parses"] == 0  # replayed without a parse


def test_extract_defs_cached_dts_and_non_ts_none():
    assert extract_defs_cached("a.d.ts", b"export declare const x: 1;\n") is None
    assert extract_defs_cached("a.py", b"def f():\n    pass\n") is None
