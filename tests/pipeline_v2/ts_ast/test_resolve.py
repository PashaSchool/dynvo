"""W6-AST M3 — tests for faultline.pipeline_v2.ts_ast.resolve.

Fixture repos are built under tmp_path with ONLY config files on disk
(tsconfig.json / package.json / pnpm-workspace.yaml) — source files are
represented purely by the ``file_set`` membership contract, and barrel
structure purely by a hand-built ``exports_index``, mirroring how M2
feeds M3 in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.ts_ast.resolve import (
    MAX_REEXPORT_DEPTH,
    TELEMETRY_KEYS,
    ExportEntry,
    ImportEdge,
    ResolvedEdge,
    clear_resolver_caches,
    resolve_edges,
)

UI = "packages/ui/src"
CORE = "packages/core/src"
HOME = "apps/web/src/pages/home.tsx"


@pytest.fixture(autouse=True)
def _fresh_caches():
    clear_resolver_caches()
    yield
    clear_resolver_caches()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _edge(
    src: str,
    raw: str,
    *,
    kind: str = "named",
    names: tuple[str, ...] = (),
    line: int = 1,
) -> ImportEdge:
    return ImportEdge(
        src_file=src, kind=kind, names=names, raw_target=raw, line=line,  # type: ignore[arg-type]
    )


def _entry(file: str, name: str, kind: str, origin: str | None = None) -> ExportEntry:
    return ExportEntry(file=file, name=name, kind=kind, origin_file=origin)  # type: ignore[arg-type]


@pytest.fixture()
def mono(tmp_path: Path) -> dict:
    """Monorepo fixture: pnpm workspaces + tsconfig paths/baseUrl +
    3-level barrel + rename + star + cycle + depth-cap chain +
    package.json exports map (exact / wildcard / condition / null)."""
    root = tmp_path / "mono"
    _write(root, "pnpm-workspace.yaml", 'packages:\n  - "packages/*"\n  - "apps/*"\n')
    _write(root, "package.json", '{"name": "fixture-root", "private": true}\n')
    # Root tsconfig — JSONC on purpose (comments + trailing commas).
    _write(root, "tsconfig.json", """{
  // repo-wide base config
  "compilerOptions": {
    "baseUrl": ".",
    "paths": {
      "~shared/*": ["./shared/*"], /* alias into shared/ */
    },
  },
}
""")
    _write(root, "apps/web/tsconfig.json", """{
  "compilerOptions": {
    "paths": {"@/*": ["./src/*"]}
  }
}
""")
    _write(root, "packages/ui/package.json", (
        '{"name": "@acme/ui", "exports": {'
        '".": {"import": "./src/index.ts"},'
        '"./widgets/*": "./src/widgets/*.ts",'
        '"./blocked": null'
        "}}\n"
    ))
    _write(root, "packages/core/package.json",
           '{"name": "@acme/core", "main": "./src/index.ts"}\n')

    files = frozenset(
        [
            "shared/util.ts",
            "shared/fmt.ts",
            "apps/web/src/pages/home.tsx",
            "apps/web/src/components/Button.tsx",
            "apps/web/src/components/index.ts",
            "apps/web/src/lib/helpers.ts",
            "apps/web/src/lib/data.ts",
            f"{UI}/index.ts",
            f"{UI}/buttons/index.ts",
            f"{UI}/buttons/button.tsx",
            f"{UI}/cards.ts",
            f"{UI}/rename-src.ts",
            f"{UI}/widgets/chip.ts",
            f"{UI}/loop-a.ts",
            f"{UI}/loop-b.ts",
            f"{CORE}/index.ts",
            f"{CORE}/helpers/math.ts",
        ]
        + [f"{CORE}/chain/t{i}.ts" for i in range(10)]
    )

    index: dict[str, list[ExportEntry]] = {
        f"{UI}/index.ts": [
            _entry(f"{UI}/index.ts", "Button", "named", "./buttons"),
            _entry(f"{UI}/index.ts", "*", "star_from", "./cards"),
            _entry(f"{UI}/index.ts", "renamed", "named", "./rename-src"),
            _entry(f"{UI}/index.ts", "zed", "named", "zod"),
            _entry(f"{UI}/index.ts", "default", "named", "./buttons/button"),
        ],
        f"{UI}/buttons/index.ts": [
            _entry(f"{UI}/buttons/index.ts", "Button", "named", "./button"),
        ],
        f"{UI}/buttons/button.tsx": [
            _entry(f"{UI}/buttons/button.tsx", "Button", "named", None),
        ],
        f"{UI}/cards.ts": [
            _entry(f"{UI}/cards.ts", "Card", "named", None),
            _entry(f"{UI}/cards.ts", "CardGrid", "named", None),
        ],
        f"{UI}/rename-src.ts": [
            # `export {inner as renamed} from './rename-src'` upstream —
            # the source name is NOT recoverable from the frozen shape.
            _entry(f"{UI}/rename-src.ts", "inner", "named", None),
        ],
        f"{UI}/loop-a.ts": [
            _entry(f"{UI}/loop-a.ts", "*", "star_from", "./loop-b"),
        ],
        f"{UI}/loop-b.ts": [
            _entry(f"{UI}/loop-b.ts", "*", "star_from", "./loop-a"),
        ],
        f"{CORE}/index.ts": [
            _entry(f"{CORE}/index.ts", "add", "named", "./helpers/math"),
        ],
        f"{CORE}/helpers/math.ts": [
            _entry(f"{CORE}/helpers/math.ts", "add", "named", None),
        ],
    }
    for i in range(9):
        index[f"{CORE}/chain/t{i}.ts"] = [
            _entry(f"{CORE}/chain/t{i}.ts", "Deep", "named", f"./t{i + 1}"),
        ]
    index[f"{CORE}/chain/t9.ts"] = [
        _entry(f"{CORE}/chain/t9.ts", "Deep", "named", None),
    ]

    return {"root": str(root), "files": files, "index": index}


def _resolve(mono: dict, edges: list[ImportEdge]):
    return resolve_edges(edges, mono["index"], mono["root"], mono["files"])


def _only(edges: list[ResolvedEdge]) -> ResolvedEdge:
    assert len(edges) == 1, edges
    return edges[0]


# ── Relative resolution ──────────────────────────────────────────────────


def test_relative_parent_dir(mono):
    out, _ = _resolve(mono, [
        _edge(HOME, "../components/Button", names=("Button",)),
    ])
    edge = _only(out)
    assert edge.target_file == "apps/web/src/components/Button.tsx"
    assert edge.resolution == "relative"
    assert edge.via_barrels == ()


def test_relative_index_descent(mono):
    out, _ = _resolve(mono, [_edge(HOME, "../components", names=("Nav",))])
    assert _only(out).target_file == "apps/web/src/components/index.ts"


def test_relative_exact_extension(mono):
    out, _ = _resolve(mono, [_edge(HOME, "../lib/helpers.ts", names=("h",))])
    assert _only(out).target_file == "apps/web/src/lib/helpers.ts"


def test_relative_js_to_ts_swap(mono):
    out, _ = _resolve(mono, [_edge(HOME, "../lib/data.js", names=("d",))])
    edge = _only(out)
    assert edge.target_file == "apps/web/src/lib/data.ts"
    assert edge.resolution == "relative"


def test_relative_escape_and_miss_unresolved(mono):
    out, tele = _resolve(mono, [
        _edge(HOME, "../../../../../outside", names=("x",)),
        _edge(HOME, "./missing", names=("y",)),
    ])
    assert [e.resolution for e in out] == ["unresolved", "unresolved"]
    assert all(e.target_file is None for e in out)
    assert tele["resolution_unresolved"] == 2


def test_dot_specifier_resolves_dir_index(mono):
    out, _ = _resolve(mono, [
        _edge(f"{UI}/buttons/button.tsx", ".", kind="namespace", names=("self",)),
    ])
    assert _only(out).target_file == f"{UI}/buttons/index.ts"


def test_root_absolute_vite_style(mono):
    out, _ = _resolve(mono, [_edge(HOME, "/shared/util", names=("u",))])
    edge = _only(out)
    assert edge.target_file == "shared/util.ts"
    assert edge.resolution == "relative"


# ── tsconfig paths + baseUrl ─────────────────────────────────────────────


def test_tsconfig_paths_nearest_workspace(mono):
    out, _ = _resolve(mono, [_edge(HOME, "@/components/Button", names=("Button",))])
    edge = _only(out)
    assert edge.target_file == "apps/web/src/components/Button.tsx"
    assert edge.resolution == "tsconfig_alias"


def test_tsconfig_paths_root_alias_from_workspace_file(mono):
    out, _ = _resolve(mono, [_edge(HOME, "~shared/util", names=("u",))])
    edge = _only(out)
    assert edge.target_file == "shared/util.ts"
    assert edge.resolution == "tsconfig_alias"


def test_tsconfig_baseurl_bare_specifier(mono):
    out, _ = _resolve(mono, [_edge(HOME, "shared/fmt", names=("fmt",))])
    edge = _only(out)
    assert edge.target_file == "shared/fmt.ts"
    assert edge.resolution == "tsconfig_alias"


def test_tsconfig_alias_file_miss_counted(mono):
    out, tele = _resolve(mono, [_edge(HOME, "@/nope/thing", names=("x",))])
    edge = _only(out)
    assert edge.resolution == "unresolved"
    assert edge.target_file is None
    assert tele["tsconfig_candidate_misses"] == 1


# ── Workspace packages + exports maps ────────────────────────────────────


def test_workspace_bare_exports_dot_condition(mono):
    out, _ = _resolve(mono, [
        _edge(HOME, "@acme/ui", kind="namespace", names=("UI",)),
    ])
    edge = _only(out)
    assert edge.target_file == f"{UI}/index.ts"
    assert edge.resolution == "workspace"
    assert edge.via_barrels == ()  # namespace imports never chase


def test_workspace_exports_wildcard_subpath(mono):
    out, _ = _resolve(mono, [
        _edge(HOME, "@acme/ui/widgets/chip", names=("Chip",)),
    ])
    edge = _only(out)
    assert edge.target_file == f"{UI}/widgets/chip.ts"
    assert edge.resolution == "workspace"


def test_workspace_exports_null_block_falls_to_miss(mono):
    out, tele = _resolve(mono, [_edge(HOME, "@acme/ui/blocked", names=("B",))])
    edge = _only(out)
    assert edge.resolution == "unresolved"
    assert tele["workspace_file_misses"] == 1


def test_workspace_main_field_entry(mono):
    out, _ = _resolve(mono, [
        _edge(HOME, "@acme/core", kind="namespace", names=("core",)),
    ])
    edge = _only(out)
    assert edge.target_file == f"{CORE}/index.ts"
    assert edge.resolution == "workspace"


def test_workspace_subpath_src_fallback(mono):
    out, _ = _resolve(mono, [
        _edge(HOME, "@acme/core/helpers/math", kind="namespace", names=("m",)),
    ])
    edge = _only(out)
    assert edge.target_file == f"{CORE}/helpers/math.ts"
    assert edge.resolution == "workspace"


def test_workspace_unknown_subpath_miss(mono):
    out, tele = _resolve(mono, [
        _edge(HOME, "@acme/core/nope", kind="namespace", names=("n",)),
    ])
    assert _only(out).resolution == "unresolved"
    assert tele["workspace_file_misses"] == 1


def test_npm_style_workspaces_field(tmp_path):
    root = tmp_path / "npmrepo"
    _write(root, "package.json", '{"name": "r", "workspaces": ["libs/*"]}\n')
    _write(root, "libs/box/package.json", '{"name": "box"}\n')
    files = frozenset(["libs/box/index.ts", "app.ts"])
    out, _ = resolve_edges(
        [_edge("app.ts", "box", kind="namespace", names=("box",))],
        {}, str(root), files,
    )
    edge = _only(out)
    assert edge.target_file == "libs/box/index.ts"
    assert edge.resolution == "workspace"


# ── External packages ────────────────────────────────────────────────────


def test_external_package_and_builtin(mono):
    out, tele = _resolve(mono, [
        _edge(HOME, "react", names=("useState",)),
        _edge(HOME, "node:fs", kind="namespace", names=("fs",)),
        _edge(HOME, "@tanstack/react-query", names=("useQuery",)),
    ])
    assert all(e.resolution == "package_external" for e in out)
    assert all(e.target_file is None for e in out)
    assert tele["resolution_package_external"] == 3


# ── type:-prefixed names ─────────────────────────────────────────────────


def test_type_names_skipped_mixed(mono):
    out, tele = _resolve(mono, [
        _edge(HOME, "../lib/helpers", names=("type:Props", "helper")),
    ])
    edge = _only(out)
    assert edge.names == ("helper",)
    assert tele["names_type_skipped"] == 1
    assert tele["edges_type_only_skipped"] == 0


def test_type_only_edge_dropped(mono):
    out, tele = _resolve(mono, [
        _edge(HOME, "../lib/helpers", names=("type:Props",)),
    ])
    assert out == []
    assert tele["edges_type_only_skipped"] == 1
    assert tele["names_type_skipped"] == 1
    assert tele["edges_out"] == 0


# ── Barrel chase ─────────────────────────────────────────────────────────


def test_barrel_three_levels(mono):
    out, tele = _resolve(mono, [_edge(HOME, "@acme/ui", names=("Button",))])
    edge = _only(out)
    assert edge.target_file == f"{UI}/buttons/button.tsx"
    assert edge.via_barrels == (f"{UI}/index.ts", f"{UI}/buttons/index.ts")
    assert edge.resolution == "workspace"  # first-hop mechanism preserved
    assert tele["reexport_hops"] == 2


def test_star_reexport_chain(mono):
    out, _ = _resolve(mono, [_edge(HOME, "@acme/ui", names=("Card",))])
    edge = _only(out)
    assert edge.target_file == f"{UI}/cards.ts"
    assert edge.via_barrels == (f"{UI}/index.ts",)


def test_rename_anchors_on_declared_origin(mono):
    out, _ = _resolve(mono, [_edge(HOME, "@acme/ui", names=("renamed",))])
    edge = _only(out)
    assert edge.target_file == f"{UI}/rename-src.ts"
    assert edge.via_barrels == (f"{UI}/index.ts",)


def test_external_reexport_stops_in_repo(mono):
    out, tele = _resolve(mono, [_edge(HOME, "@acme/ui", names=("zed",))])
    edge = _only(out)
    assert edge.target_file == f"{UI}/index.ts"
    assert edge.via_barrels == ()
    assert tele["reexport_external_stops"] == 1


def test_default_import_follows_default_reexport(mono):
    out, _ = _resolve(mono, [
        _edge(HOME, "@acme/ui", kind="default", names=("UiKit",)),
    ])
    edge = _only(out)
    assert edge.target_file == f"{UI}/buttons/button.tsx"
    assert edge.via_barrels == (f"{UI}/index.ts",)
    assert edge.names == ("UiKit",)  # local binding name preserved


def test_per_name_split_into_origin_groups(mono):
    out, _ = _resolve(mono, [_edge(HOME, "@acme/ui", names=("Button", "Card"))])
    assert len(out) == 2
    by_target = {e.target_file: e for e in out}
    assert by_target[f"{UI}/buttons/button.tsx"].names == ("Button",)
    assert by_target[f"{UI}/cards.ts"].names == ("Card",)
    # Both keep the original raw target + first-hop resolution.
    assert {e.raw_target for e in out} == {"@acme/ui"}
    assert {e.resolution for e in out} == {"workspace"}


def test_cycle_safe_star_loop(mono):
    out, tele = _resolve(mono, [
        _edge(f"{UI}/index.ts", "./loop-a", names=("Ghost",)),
    ])
    edge = _only(out)
    assert edge.target_file == f"{UI}/loop-a.ts"  # anchored, not lost
    assert edge.via_barrels == ()
    assert tele["reexport_cycle_hits"] >= 1
    assert tele["reexport_name_misses"] == 1


def test_depth_cap_anchors_mid_chain(mono):
    out, tele = _resolve(mono, [
        _edge(f"{CORE}/index.ts", "./chain/t0", names=("Deep",)),
    ])
    edge = _only(out)
    assert tele["reexport_depth_cap_hits"] == 1
    assert edge.target_file == f"{CORE}/chain/t{MAX_REEXPORT_DEPTH}.ts"
    assert len(edge.via_barrels) == MAX_REEXPORT_DEPTH
    assert edge.via_barrels[0] == f"{CORE}/chain/t0.ts"


def test_reexport_named_edge_kind_chases(mono):
    # The barrel's own `export {Button} from './buttons'` edge.
    out, _ = _resolve(mono, [
        _edge(f"{UI}/index.ts", "./buttons", kind="reexport_named",
              names=("Button",)),
    ])
    edge = _only(out)
    assert edge.kind == "reexport_named"
    assert edge.target_file == f"{UI}/buttons/button.tsx"
    assert edge.via_barrels == (f"{UI}/buttons/index.ts",)


def test_reexport_star_edge_no_chase(mono):
    out, _ = _resolve(mono, [
        _edge(f"{UI}/index.ts", "./cards", kind="reexport_star"),
    ])
    edge = _only(out)
    assert edge.target_file == f"{UI}/cards.ts"
    assert edge.via_barrels == ()


# ── side-effect / asset imports ──────────────────────────────────────────


def test_side_effect_import_respects_file_set(mono):
    css_edge = _edge(HOME, "./styles.css", kind="side_effect")
    out, _ = resolve_edges([css_edge], mono["index"], mono["root"], mono["files"])
    assert _only(out).resolution == "unresolved"

    with_css = frozenset(mono["files"] | {"apps/web/src/pages/styles.css"})
    out2, _ = resolve_edges([css_edge], mono["index"], mono["root"], with_css)
    edge = _only(out2)
    assert edge.resolution == "relative"
    assert edge.target_file == "apps/web/src/pages/styles.css"


def test_empty_specifier_unresolved(mono):
    out, _ = _resolve(mono, [_edge(HOME, "", kind="dynamic")])
    assert _only(out).resolution == "unresolved"


# ── Determinism / canonical output ───────────────────────────────────────


def _mixed_edge_bag(mono) -> list[ImportEdge]:
    return [
        _edge(HOME, "@acme/ui", names=("Card", "Button")),
        _edge(HOME, "react", names=("useState",)),
        _edge(HOME, "../components/Button", names=("Button",)),
        _edge(HOME, "@/lib/helpers", names=("helper",)),
        _edge(HOME, "~shared/util", names=("u",)),
        _edge(f"{CORE}/index.ts", "./chain/t0", names=("Deep",)),
        _edge(HOME, "./missing", names=("nope",)),
        _edge(HOME, "@acme/core/helpers/math", kind="namespace", names=("m",)),
    ]


def test_duplicate_import_statements_merge(mono):
    out, _ = _resolve(mono, [
        _edge(HOME, "../lib/helpers", names=("helper",), line=1),
        _edge(HOME, "../lib/helpers", names=("helper",), line=42),
    ])
    edge = _only(out)
    assert edge.names == ("helper",)


def test_deterministic_and_input_order_independent(mono):
    edges = _mixed_edge_bag(mono)
    out_a, tele_a = _resolve(mono, edges)
    out_b, tele_b = _resolve(mono, list(reversed(edges)))
    out_c, tele_c = _resolve(mono, edges)
    assert out_a == out_b == out_c
    assert tele_a == tele_b == tele_c
    assert out_a == sorted(out_a, key=lambda e: (
        e.src_file, e.raw_target, e.kind, e.resolution,
        e.target_file or "", e.via_barrels, e.names,
    ))


def test_telemetry_shape_and_accounting(mono):
    out, tele = _resolve(mono, _mixed_edge_bag(mono))
    assert list(tele.keys()) == list(TELEMETRY_KEYS)
    assert list(TELEMETRY_KEYS) == sorted(TELEMETRY_KEYS)
    assert all(isinstance(v, int) for v in tele.values())
    assert tele["edges_in"] == len(_mixed_edge_bag(mono))
    assert tele["edges_out"] == len(out)
    assert tele["edges_out"] == (
        tele["resolution_relative"]
        + tele["resolution_tsconfig_alias"]
        + tele["resolution_workspace"]
        + tele["resolution_package_external"]
        + tele["resolution_unresolved"]
    )
    assert tele["reexport_hops"] == sum(len(e.via_barrels) for e in out)


def test_two_repo_roots_do_not_cross_contaminate(mono, tmp_path):
    other = tmp_path / "other"
    _write(other, "package.json", '{"name": "solo"}\n')
    files = frozenset(["a.ts", "b.ts"])
    out, _ = resolve_edges(
        [_edge("a.ts", "./b", names=("b",))], {}, str(other), files,
    )
    assert _only(out).target_file == "b.ts"
    # The mono context still resolves its workspace packages afterwards.
    out2, _ = _resolve(mono, [
        _edge(HOME, "@acme/ui", kind="namespace", names=("UI",)),
    ])
    assert _only(out2).target_file == f"{UI}/index.ts"
