"""W4 — Stage 6.55 page-interior parser + flow-span consumers.

Focused gate tests (product-spine §4.6 / W4 brief):
  * parser determinism incl. the content-hash cache;
  * import-provenance classification (design-system vs product,
    workspace-aware);
  * span refinement (``role="interior"`` attributions, caps, dedup);
  * node injection + support-span tightening + LOC re-projection;
  * ≥2-page family construction;
  * degenerate-span ruler;
  * graceful degrade (env kill / tree-sitter absent semantics).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.models.types import (
    Feature,
    Flow,
    FlowNode,
    FlowSummary,
    FlowSymbolAttribution,
)
from faultline.pipeline_v2 import stage_6_55_page_interior as s655
from faultline.pipeline_v2.stage_6_55_page_interior import (
    InteriorFamily,
    InteriorNode,
    InteriorResult,
    PageInterior,
    _build_families,
    _cache_key,
    _parse_cached,
    _parse_page_source,
    degenerate_span_stats,
    get_page_interiors,
    inject_interior_nodes,
    refine_flow_spans,
)

requires_ts = pytest.mark.skipif(
    not s655.is_active(), reason="tree-sitter web grammars not installed",
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)

_PAGE_SRC = b"""
import { Button } from "@/components/ui/button";
import { DatabaseBackups } from "@/components/interfaces/Database/Backups";
import ConnectionPooling from "../../components/interfaces/Database/Pooling";
import { Tabs } from "@radix-ui/react-tabs";

export default function DatabasePage() {
  return (
    <div>
      <h1>Database Settings</h1>
      <DatabaseBackups title="Scheduled backups" />
      <ConnectionPooling />
      <Tabs />
      <Button>Save</Button>
    </div>
  );
}
"""

_PAGE_FILE = "apps/studio/src/pages/database/settings.tsx"
_TRACKED = frozenset({
    _PAGE_FILE,
    "apps/studio/src/components/interfaces/Database/Backups/index.tsx",
    "apps/studio/src/components/interfaces/Database/Pooling.tsx",
    "apps/studio/src/components/ui/button.tsx",
})


def _parse() -> list[dict]:
    return _parse_page_source(_PAGE_FILE, _PAGE_SRC, _TRACKED, {}, {})


# ── Parser + provenance ─────────────────────────────────────────────────


@requires_ts
def test_parse_extracts_components_headings_and_labels() -> None:
    nodes = _parse()
    by_name = {n["name"]: n for n in nodes}
    assert "Database Settings" in by_name          # heading text captured
    assert by_name["Database Settings"]["kind"] == "heading"
    backups = by_name["DatabaseBackups"]
    assert backups["label"] == "Scheduled backups"
    assert backups["source_file"] == (
        "apps/studio/src/components/interfaces/Database/Backups/index.tsx"
    )


@requires_ts
def test_import_provenance_classification() -> None:
    by_name = {n["name"]: n for n in _parse()}
    # Product components: local/workspace, non-primitive path.
    assert by_name["DatabaseBackups"]["provenance"] == "product"
    assert by_name["ConnectionPooling"]["provenance"] == "product"
    # Design system: external package + components/ui + primitive name.
    assert by_name["Tabs"]["provenance"] == "design_system"
    assert by_name["Tabs"]["source_kind"] == "package"
    assert by_name["Button"]["provenance"] == "design_system"


@requires_ts
def test_parse_deterministic() -> None:
    assert _parse() == _parse()


@requires_ts
def test_content_hash_cache_roundtrip() -> None:
    class _Backend:
        def __init__(self) -> None:
            self.store: dict[tuple[str, str], dict] = {}
            self.gets = 0

        def get(self, kind, key):  # noqa: ANN001
            self.gets += 1
            return self.store.get((str(kind), key))

        def set(self, kind, key, value, **kw):  # noqa: ANN001, ANN003
            self.store[(str(kind), key)] = value

    backend = _Backend()
    ctx = SimpleNamespace(cache_backend=backend)
    stats = {"parsed": 0, "cache_hits": 0}
    first = _parse_cached(ctx, _PAGE_FILE, _PAGE_SRC, _TRACKED, {}, {}, stats)
    second = _parse_cached(ctx, _PAGE_FILE, _PAGE_SRC, _TRACKED, {}, {}, stats)
    assert first == second
    assert stats == {"parsed": 1, "cache_hits": 1}
    # Content change → different key (never a stale hit).
    assert _cache_key(_PAGE_SRC) != _cache_key(_PAGE_SRC + b"\n// x")


def test_env_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_6_55", "0")
    ctx = SimpleNamespace(repo_path=Path("."), tracked_files=[],
                          workspaces=None, cache_backend=None)
    result = get_page_interiors(ctx, [])
    assert result.active is False
    assert "FAULTLINE_STAGE_6_55" in result.reason


# ── Families ────────────────────────────────────────────────────────────


def _inode(name: str, source_file: str, page_hint: str = "",
           provenance: str = "product") -> InteriorNode:
    return InteriorNode(
        kind="component", name=name, label=None,
        usage_line_start=1, usage_line_end=1,
        source_kind="local", provenance=provenance,
        source_file=source_file, def_line_start=1, def_line_end=40,
    )


def test_families_require_two_distinct_pages() -> None:
    comp = "apps/web/src/features/billing/InvoiceTable.tsx"
    pages = {
        "apps/web/src/app/billing/page.tsx": PageInterior(
            file="apps/web/src/app/billing/page.tsx", page_kind="page",
            nodes=(_inode("InvoiceTable", comp),),
        ),
        "apps/web/src/app/settings/page.tsx": PageInterior(
            file="apps/web/src/app/settings/page.tsx", page_kind="page",
            nodes=(_inode("InvoiceTable", comp),),
        ),
        "apps/web/src/app/one-off/page.tsx": PageInterior(
            file="apps/web/src/app/one-off/page.tsx", page_kind="page",
            nodes=(_inode("OneOff", "apps/web/src/features/oneoff/OneOff.tsx"),),
        ),
    }
    families = _build_families(pages)
    dirs = [f.family_dir for f in families]
    assert dirs == ["apps/web/src/features/billing"]
    fam = families[0]
    assert fam.component_names == ("InvoiceTable",)
    assert len(fam.page_files) == 2


def test_families_ignore_design_system() -> None:
    comp = "packages/ui/src/button.tsx"
    pages = {
        f"apps/web/src/app/{p}/page.tsx": PageInterior(
            file=f"apps/web/src/app/{p}/page.tsx", page_kind="page",
            nodes=(_inode("Button", comp, provenance="design_system"),),
        )
        for p in ("a", "b", "c")
    }
    assert _build_families(pages) == ()


# ── Flow-span consumers ─────────────────────────────────────────────────


def _flow(entry: str, **kw) -> Flow:
    base = dict(
        name="view-database-flow", paths=[entry], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0, entry_point_file=entry,
    )
    base.update(kw)
    return Flow(**base)


def _dev(flows: list[Flow]) -> Feature:
    return Feature(
        name="database", display_name="database",
        paths=[f.entry_point_file or "x" for f in flows], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="developer", flows=flows,
    )


def _result_with_page() -> InteriorResult:
    comp_file = "apps/studio/src/components/interfaces/Database/Backups/index.tsx"
    node = InteriorNode(
        kind="component", name="DatabaseBackups", label="Scheduled backups",
        usage_line_start=11, usage_line_end=11,
        source_kind="workspace", provenance="product",
        source_file=comp_file, def_line_start=5, def_line_end=64,
    )
    design = InteriorNode(
        kind="component", name="Button", label=None,
        usage_line_start=14, usage_line_end=14,
        source_kind="workspace", provenance="design_system",
        source_file="apps/studio/src/components/ui/button.tsx",
        def_line_start=1, def_line_end=30,
    )
    page = PageInterior(file=_PAGE_FILE, page_kind="page",
                        nodes=(node, design))
    return InteriorResult(active=True, pages={_PAGE_FILE: page})


def test_refine_flow_spans_adds_interior_attribution() -> None:
    flow = _flow(_PAGE_FILE, flow_symbol_attributions=[
        FlowSymbolAttribution(file=_PAGE_FILE, symbol="DatabasePage",
                              line_start=7, line_end=18, role="entry"),
    ])
    features = [_dev([flow])]
    tele = refine_flow_spans(features, _result_with_page())
    assert tele == {"flows_touched": 1, "interior_attributions": 1}
    interior = [a for a in flow.flow_symbol_attributions
                if a.role == "interior"]
    assert len(interior) == 1
    a = interior[0]
    assert a.symbol == "DatabaseBackups"
    assert (a.line_start, a.line_end) == (5, 64)
    # Design-system Button did NOT become evidence.
    assert all(x.symbol != "Button" for x in flow.flow_symbol_attributions)
    # Idempotent (dedup on (file, symbol)).
    tele2 = refine_flow_spans(features, _result_with_page())
    assert tele2 == {"flows_touched": 0, "interior_attributions": 0}


def test_refine_skips_non_page_flows() -> None:
    flow = _flow("apps/api/src/routes/backups.ts")
    tele = refine_flow_spans([_dev([flow])], _result_with_page())
    assert tele == {"flows_touched": 0, "interior_attributions": 0}
    assert flow.flow_symbol_attributions == []


def test_inject_interior_nodes_and_line_ranges() -> None:
    comp_file = "apps/studio/src/components/interfaces/Database/Backups/index.tsx"
    flow = _flow(_PAGE_FILE, flow_symbol_attributions=[
        FlowSymbolAttribution(file=_PAGE_FILE, symbol="DatabasePage",
                              line_start=7, line_end=18, role="entry"),
    ])
    features = [_dev([flow])]
    result = _result_with_page()
    refine_flow_spans(features, result)
    # Simulate the Stage 3.5 state: entry node exists, plus a whole-file
    # support node on the component source (the degenerate class).
    flow.nodes = [
        FlowNode(id=f"{_PAGE_FILE}#DatabasePage", kind="entry",
                 file=_PAGE_FILE, symbol="DatabasePage", lines=(7, 18),
                 role="entry", confidence="high"),
        FlowNode(id=comp_file, kind="file", file=comp_file, symbol=None,
                 lines=(1, 400), role="support", confidence="medium"),
    ]
    flow.summary = FlowSummary(
        total_nodes=2, total_files=2, total_lines_touched=412,
        cross_stack_hops=0, max_depth=1, unsupported_stack=False,
        truncated=False,
    )
    tele = inject_interior_nodes(features, result)
    assert tele["flows_touched"] == 1
    assert tele["nodes_added"] == 1
    assert tele["support_tightened"] == 1
    by_id = {n.id: n for n in flow.nodes}
    interior_node = by_id[f"{comp_file}#DatabaseBackups"]
    assert interior_node.role == "interior"
    assert interior_node.lines == (5, 64)
    # The whole-file support span tightened to the definition span.
    assert by_id[comp_file].lines == (5, 64)
    # Phase-5 projections rebuilt: line_ranges cover the component file
    # at definition grain, never (1, 400).
    spans = {(r.path, r.start_line, r.end_line) for r in flow.line_ranges}
    assert (comp_file, 5, 64) in spans
    assert (comp_file, 1, 400) not in spans
    # Summary recomputed from the tightened node set.
    assert flow.summary.total_lines_touched == sum(
        n.lines[1] - n.lines[0] + 1 for n in flow.nodes if n.lines
    )


def test_inject_noop_when_inactive() -> None:
    flow = _flow(_PAGE_FILE)
    tele = inject_interior_nodes(
        [_dev([flow])], InteriorResult(active=False, reason="off"),
    )
    assert tele == {"nodes_added": 0, "support_tightened": 0,
                    "flows_touched": 0}
    assert flow.nodes == []


# ── Degenerate-span ruler ───────────────────────────────────────────────


def test_degenerate_span_stats() -> None:
    wrapper = _flow("a.ts", nodes=[
        FlowNode(id="a.ts#f", kind="entry", file="a.ts", symbol="f",
                 lines=(3, 4), role="entry", confidence="high"),
    ])
    healthy = _flow("b.ts", nodes=[
        FlowNode(id="b.ts#g", kind="entry", file="b.ts", symbol="g",
                 lines=(1, 40), role="entry", confidence="high"),
    ])
    bare = _flow("c.ts")  # no nodes, no attributions
    stats = degenerate_span_stats([_dev([wrapper, healthy, bare])])
    assert stats["flows"] == 3
    assert stats["degenerate"] == 1
    assert stats["no_span"] == 1
    assert stats["degenerate_share"] == round(1 / 3, 4)


def test_degenerate_stats_fall_back_to_attributions() -> None:
    flow = _flow("a.ts", flow_symbol_attributions=[
        FlowSymbolAttribution(file="a.ts", symbol="f", line_start=1,
                              line_end=1, role="entry"),
    ])
    stats = degenerate_span_stats([_dev([flow])])
    assert stats["degenerate"] == 1


# ── Digest feed helper ──────────────────────────────────────────────────


def test_product_sections_by_source_prefix() -> None:
    result = _result_with_page()
    labels = result.product_sections_by_source_prefix(
        ("apps/studio/src/pages/database",))
    assert labels == ["Scheduled backups"]
    assert result.product_sections_by_source_prefix(("apps/other",)) == []


# ── 1-hop barrel following (debt-pack, w4-report residual 4) ─────────────
# typebot-class: workspace-package imports resolve to index barrels that
# hold no definitions — the def-span probe missed and nodes degraded to
# whole-file claims. One hop through the barrel's re-exports recovers
# the real source file + span.

from faultline.pipeline_v2.stage_6_55_page_interior import (  # noqa: E402
    _barrel_exports,
    _def_span_via_barrel,
)


def _clear_span_caches() -> None:
    s655._DEF_SPAN_CACHE.clear()
    s655._BARREL_EXPORT_CACHE.clear()


def _mk(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_barrel_exports_parses_named_alias_type_and_star(tmp_path: Path) -> None:
    _clear_span_caches()
    _mk(tmp_path, "pkg/index.ts", (
        'export { Block, type BlockDef } from "./block";\n'
        'export { InnerCard as Card } from "./card";\n'
        'export * from "./widgets";\n'
    ))
    ex = _barrel_exports(tmp_path, "pkg/index.ts")
    assert ex == [
        ("./block", {"Block": "Block", "BlockDef": "BlockDef"}),
        ("./card", {"Card": "InnerCard"}),
        ("./widgets", None),
    ]


def test_def_span_via_barrel_named_reexport(tmp_path: Path) -> None:
    _clear_span_caches()
    _mk(tmp_path, "pkg/index.ts", 'export { Block } from "./block";\n')
    _mk(tmp_path, "pkg/block.tsx",
        "export function Block() {\n  return null;\n}\n")
    tracked = frozenset({"pkg/index.ts", "pkg/block.tsx"})
    hop = _def_span_via_barrel(tmp_path, "pkg/index.ts", "Block", tracked)
    assert hop is not None
    target, span = hop
    assert target == "pkg/block.tsx"
    assert span[0] == 1 and span[1] >= 2


def test_def_span_via_barrel_star_and_alias(tmp_path: Path) -> None:
    _clear_span_caches()
    _mk(tmp_path, "pkg/index.ts", (
        'export { InnerCard as Card } from "./card";\n'
        'export * from "./widgets";\n'
    ))
    _mk(tmp_path, "pkg/card.tsx",
        "export const InnerCard = () => null;\n")
    _mk(tmp_path, "pkg/widgets.tsx",
        "export function Widget() {\n  return null;\n}\n")
    tracked = frozenset({"pkg/index.ts", "pkg/card.tsx", "pkg/widgets.tsx"})
    # alias: exported name Card → InnerCard span inside card.tsx
    hop = _def_span_via_barrel(tmp_path, "pkg/index.ts", "Card", tracked)
    assert hop is not None and hop[0] == "pkg/card.tsx"
    # star fan-out finds Widget
    hop = _def_span_via_barrel(tmp_path, "pkg/index.ts", "Widget", tracked)
    assert hop is not None and hop[0] == "pkg/widgets.tsx"


def test_def_span_via_barrel_is_one_hop_only(tmp_path: Path) -> None:
    _clear_span_caches()
    _mk(tmp_path, "pkg/index.ts", 'export * from "./inner";\n')
    _mk(tmp_path, "pkg/inner.ts", 'export * from "./deep";\n')
    _mk(tmp_path, "pkg/deep.tsx", "export function Deep() {\n  return null;\n}\n")
    tracked = frozenset({"pkg/index.ts", "pkg/inner.ts", "pkg/deep.tsx"})
    assert _def_span_via_barrel(
        tmp_path, "pkg/index.ts", "Deep", tracked) is None


def test_def_span_via_barrel_ignores_bare_package_specs(tmp_path: Path) -> None:
    _clear_span_caches()
    _mk(tmp_path, "pkg/index.ts", 'export { Block } from "@external/blocks";\n')
    tracked = frozenset({"pkg/index.ts"})
    assert _def_span_via_barrel(
        tmp_path, "pkg/index.ts", "Block", tracked) is None


@requires_ts
def test_barrel_hop_end_to_end_rehomes_source_file(tmp_path: Path) -> None:
    """typebot-shaped: page imports Block from a ws package whose index
    barrel re-exports it — the interior node must carry the REAL source
    file + definition span, not a span-less barrel claim."""
    _clear_span_caches()
    s655._MEMO.clear()
    s655._MEMO_ORDER.clear()
    page = "apps/builder/src/pages/editor.tsx"
    _mk(tmp_path, page, (
        'import { Block } from "@typebot.io/blocks-core";\n'
        "export default function EditorPage() {\n"
        "  return <Block />;\n"
        "}\n"
    ))
    _mk(tmp_path, "packages/blocks/core/src/index.ts",
        'export * from "./block";\n')
    _mk(tmp_path, "packages/blocks/core/src/block.tsx",
        "export function Block() {\n  return <div />;\n}\n")
    ctx = SimpleNamespace(
        repo_path=tmp_path,
        cache_backend=None,
        tracked_files=[
            page,
            "packages/blocks/core/src/index.ts",
            "packages/blocks/core/src/block.tsx",
        ],
        workspaces=[SimpleNamespace(
            name="@typebot.io/blocks-core", path="packages/blocks/core")],
    )
    res = get_page_interiors(ctx, [{"method": "PAGE", "file": page}])
    assert res.active
    node = next(n for n in res.pages[page].nodes if n.name == "Block")
    assert node.source_file == "packages/blocks/core/src/block.tsx"
    assert node.def_line_start == 1 and node.def_line_end >= 2
