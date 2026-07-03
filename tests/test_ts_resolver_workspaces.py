"""TS/JS import-resolver: per-package tsconfig walking + workspace map.

WS3 SCIP-oracle finding (2026-07-03): ~90% of missed cross-file
reference edges were RESOLUTION failures —

  1. only the repo-root tsconfig was loaded, so per-app aliases
     (``~/* → ./src/*`` in ``apps/marketing``) resolved against the
     wrong base and missed;
  2. ``build_symbol_graph`` never received the workspace package map,
     so scoped workspace imports (``@acme/ui/...``) never resolved.

These tests pin the fix: nearest-enclosing-workspace alias resolution
(:func:`faultline.analyzer.tsconfig_paths.resolve_alias_import`),
workspace-package composition inside
:func:`faultline.analyzer.import_graph._resolve_import`, and the wiring
through :func:`faultline.analyzer.symbol_graph.build_symbol_graph`.

All fixtures are synthetic tiny monorepos in ``tmp_path`` (neutral
names — no real-repo layouts baked in). $0, no LLM, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.analyzer.import_graph import (
    _resolve_import,
    detect_workspace_package_map,
)
from faultline.analyzer.symbol_graph import build_symbol_graph
from faultline.analyzer.tsconfig_paths import (
    build_path_alias_map,
    resolve_alias_import,
)


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── Per-package alias resolution ─────────────────────────────────────────


def test_nested_tsconfigs_nearest_workspace_wins(tmp_path: Path) -> None:
    """Same prefix declared at root AND in an app: the app's own
    mapping wins for files inside the app; the root mapping applies
    to files outside it."""
    _write(tmp_path, "tsconfig.json", json.dumps({
        "compilerOptions": {"paths": {"@/*": ["./src/*"]}},
    }))
    _write(tmp_path, "apps/web/tsconfig.json", json.dumps({
        "compilerOptions": {"paths": {"@/*": ["./*"]}},
    }))
    _write(tmp_path, "src/lib/util.ts", "export const u = 1;\n")
    _write(tmp_path, "apps/web/lib/util.ts", "export const u = 2;\n")

    alias_map = build_path_alias_map(tmp_path)
    tracked = frozenset({"src/lib/util.ts", "apps/web/lib/util.ts"})

    assert resolve_alias_import(
        "apps/web/pages/index.ts", "@/lib/util", alias_map, tracked,
    ) == "apps/web/lib/util.ts"
    assert resolve_alias_import(
        "main.ts", "@/lib/util", alias_map, tracked,
    ) == "src/lib/util.ts"


def test_sibling_workspace_alias_never_leaks(tmp_path: Path) -> None:
    """An alias declared in a SIBLING workspace must not resolve —
    even when the target file exists there. TS scopes each tsconfig
    to its own directory tree; cross-app resolution would fabricate
    edges the compiler never makes."""
    for app in ("alpha", "beta"):
        _write(tmp_path, f"apps/{app}/tsconfig.json", json.dumps({
            "compilerOptions": {"paths": {"~/*": ["./src/*"]}},
        }))
    # The component exists ONLY in beta.
    _write(tmp_path, "apps/beta/src/components/Nav.tsx", "export const Nav = 1;\n")

    alias_map = build_path_alias_map(tmp_path)
    tracked = frozenset({"apps/beta/src/components/Nav.tsx"})

    # Importer inside alpha: its own mapping misses; beta's mapping
    # must NOT be borrowed.
    assert resolve_alias_import(
        "apps/alpha/src/app/page.tsx", "~/components/Nav", alias_map, tracked,
    ) is None
    # Importer inside beta resolves normally.
    assert resolve_alias_import(
        "apps/beta/src/app/page.tsx", "~/components/Nav", alias_map, tracked,
    ) == "apps/beta/src/components/Nav.tsx"


def test_extends_chain_alias_resolves_against_leaf(tmp_path: Path) -> None:
    """Alias inherited via ``extends`` from a shared base package
    resolves against the EXTENDING app's directory (the unsend /
    create-t3 shape: ``~/* → ./src/*`` declared once, used per app)."""
    _write(tmp_path, "packages/tsconfig/nextjs.json", json.dumps({
        "compilerOptions": {"paths": {"~/*": ["./src/*"]}},
    }))
    _write(tmp_path, "apps/site/tsconfig.json", json.dumps({
        "extends": "@repo/tsconfig/nextjs.json",
    }))
    _write(tmp_path, "apps/site/src/components/Nav.tsx", "export const Nav = 1;\n")

    alias_map = build_path_alias_map(tmp_path)
    tracked = frozenset({"apps/site/src/components/Nav.tsx"})

    assert resolve_alias_import(
        "apps/site/src/app/page.tsx", "~/components/Nav", alias_map, tracked,
    ) == "apps/site/src/components/Nav.tsx"


def test_baseurl_offsets_alias_target(tmp_path: Path) -> None:
    """``baseUrl`` participates in target resolution: paths are
    relative to (tsconfig dir / baseUrl)."""
    _write(tmp_path, "svc/tsconfig.json", json.dumps({
        "compilerOptions": {
            "baseUrl": "./src",
            "paths": {"#lib/*": ["lib/*"]},
        },
    }))
    _write(tmp_path, "svc/src/lib/db.ts", "export const db = 1;\n")

    alias_map = build_path_alias_map(tmp_path)
    tracked = frozenset({"svc/src/lib/db.ts"})

    assert resolve_alias_import(
        "svc/src/main.ts", "#lib/db", alias_map, tracked,
    ) == "svc/src/lib/db.ts"


def test_multiple_targets_tried_in_declared_order(tmp_path: Path) -> None:
    """Every ``paths`` target is tried, in declared order (the old
    code silently kept only the first)."""
    _write(tmp_path, "tsconfig.json", json.dumps({
        "compilerOptions": {
            "paths": {"@x/*": ["./gen/*", "./src/*"]},
        },
    }))
    _write(tmp_path, "src/thing.ts", "export const t = 1;\n")

    alias_map = build_path_alias_map(tmp_path)
    tracked = frozenset({"src/thing.ts"})

    # gen/thing.ts does not exist → second target must be tried.
    assert resolve_alias_import(
        "main.ts", "@x/thing", alias_map, tracked,
    ) == "src/thing.ts"


def test_alias_map_discovery_is_deterministic(tmp_path: Path) -> None:
    for app in ("a", "b", "c"):
        _write(tmp_path, f"apps/{app}/tsconfig.json", json.dumps({
            "compilerOptions": {"paths": {"~/*": ["./src/*"]}},
        }))
    first = build_path_alias_map(tmp_path)
    second = build_path_alias_map(tmp_path)
    assert first == second
    # With identical prefixes the tie-break is the (sorted) discovery
    # order of the declaring tsconfigs — fully defined, no fs-order leak.
    assert [e.workspace_root for e in first] == ["apps/a", "apps/b", "apps/c"]


# ── Workspace package map composition in _resolve_import ────────────────


def _workspace_repo(tmp_path: Path) -> Path:
    """Tiny pnpm-workspace monorepo with one app + one shared package."""
    _write(tmp_path, "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    _write(tmp_path, "package.json", json.dumps({"name": "root", "private": True}))
    _write(tmp_path, "packages/ui/package.json", json.dumps({
        "name": "@acme/ui", "main": "./src/index.tsx",
    }))
    _write(tmp_path, "packages/ui/src/index.tsx", "export const Ui = 1;\n")
    _write(tmp_path, "packages/ui/src/button.tsx", "export const Button = 1;\n")
    _write(tmp_path, "apps/web/package.json", json.dumps({"name": "web"}))
    return tmp_path


def test_workspace_bare_import_resolves_entry(tmp_path: Path) -> None:
    repo = _workspace_repo(tmp_path)
    ws_map = detect_workspace_package_map(str(repo))
    assert ws_map.get("@acme/ui") == "packages/ui"

    tracked = frozenset({
        "packages/ui/src/index.tsx", "packages/ui/src/button.tsx",
    })
    assert _resolve_import(
        "apps/web/page.tsx", "@acme/ui", tracked,
        workspace_package_map=ws_map, repo_root=str(repo),
    ) == "packages/ui/src/index.tsx"


def test_workspace_deep_import_with_src_fallback(tmp_path: Path) -> None:
    repo = _workspace_repo(tmp_path)
    ws_map = detect_workspace_package_map(str(repo))
    tracked = frozenset({
        "packages/ui/src/index.tsx", "packages/ui/src/button.tsx",
    })
    # 'packages/ui/button' misses → 'packages/ui/src/button' fallback hits.
    assert _resolve_import(
        "apps/web/page.tsx", "@acme/ui/button", tracked,
        workspace_package_map=ws_map, repo_root=str(repo),
    ) == "packages/ui/src/button.tsx"


def test_alias_entries_take_priority_over_workspace_map(tmp_path: Path) -> None:
    """Composition order inside ``_resolve_import``: relative →
    per-workspace aliases → (legacy flat map / builtins / monorepo
    dirs) → workspace package map."""
    repo = _workspace_repo(tmp_path)
    _write(repo, "apps/web/tsconfig.json", json.dumps({
        "compilerOptions": {"paths": {"@acme/ui/*": ["./stubs/*"]}},
    }))
    _write(repo, "apps/web/stubs/button.tsx", "export const Stub = 1;\n")

    alias_entries = build_path_alias_map(repo)
    ws_map = detect_workspace_package_map(str(repo))
    tracked = frozenset({
        "packages/ui/src/index.tsx", "packages/ui/src/button.tsx",
        "apps/web/stubs/button.tsx",
    })
    assert _resolve_import(
        "apps/web/page.tsx", "@acme/ui/button", tracked,
        alias_entries=alias_entries,
        workspace_package_map=ws_map, repo_root=str(repo),
    ) == "apps/web/stubs/button.tsx"
    # No matching alias → falls through to the workspace map.
    assert _resolve_import(
        "apps/web/page.tsx", "@acme/ui", tracked,
        alias_entries=alias_entries,
        workspace_package_map=ws_map, repo_root=str(repo),
    ) == "packages/ui/src/index.tsx"


def test_plain_relative_imports_unchanged(tmp_path: Path) -> None:
    """No-regression guard: relative resolution ignores every new
    lever and behaves exactly as before."""
    tracked = frozenset({"src/a.ts", "src/lib/b.ts"})
    assert _resolve_import(
        "src/a.ts", "./lib/b", tracked,
        alias_entries=build_path_alias_map(tmp_path),  # empty
        workspace_package_map={}, repo_root=str(tmp_path),
    ) == "src/lib/b.ts"
    assert _resolve_import(
        "src/lib/b.ts", "../a", tracked,
    ) == "src/a.ts"


def test_unresolvable_import_stays_silent(tmp_path: Path) -> None:
    repo = _workspace_repo(tmp_path)
    ws_map = detect_workspace_package_map(str(repo))
    tracked = frozenset({"packages/ui/src/index.tsx"})
    for spec in ("react", "@other-scope/pkg", "@acme/unknown", "node:fs"):
        assert _resolve_import(
            "apps/web/page.tsx", spec, tracked,
            alias_entries=build_path_alias_map(repo),
            workspace_package_map=ws_map, repo_root=str(repo),
        ) is None


# ── build_symbol_graph wiring ────────────────────────────────────────────


def _symbol_graph_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """Monorepo where EVERY cross-file import needs the new levers."""
    _write(tmp_path, "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'packages/*'\n")
    _write(tmp_path, "packages/ui/package.json", json.dumps({
        "name": "@acme/ui", "main": "./src/index.tsx",
    }))
    _write(tmp_path, "packages/ui/src/index.tsx", "export const Ui = () => null;\n")
    _write(tmp_path, "apps/web/tsconfig.json", json.dumps({
        "compilerOptions": {"paths": {"~/*": ["./src/*"]}},
    }))
    _write(tmp_path, "apps/web/src/lib/cn.ts", "export function cn() { return ''; }\n")
    _write(
        tmp_path, "apps/web/src/app/page.tsx",
        # Default import from a scoped workspace package (previously
        # dropped by the @// ~/ prefix pre-filter) + a per-app alias
        # named import (previously resolved against the repo root).
        'import Ui from "@acme/ui";\n'
        'import { cn } from "~/lib/cn";\n'
        'import React from "react";\n'
        "export default function Page() { return cn() && Ui && React; }\n",
    )
    files = [
        "packages/ui/src/index.tsx",
        "apps/web/src/lib/cn.ts",
        "apps/web/src/app/page.tsx",
    ]
    return tmp_path, files


def test_symbol_graph_resolves_workspace_and_per_app_alias(tmp_path: Path) -> None:
    repo, files = _symbol_graph_repo(tmp_path)
    graph = build_symbol_graph(str(repo), files, include_http_edges=False)

    targets = {e.target_file for e in graph.forward.get("apps/web/src/app/page.tsx", [])}
    assert "packages/ui/src/index.tsx" in targets      # workspace map
    assert "apps/web/src/lib/cn.ts" in targets         # per-app alias
    # External import produced NO edge (silent failure preserved).
    assert len(targets) == 2


def test_symbol_graph_build_is_deterministic(tmp_path: Path) -> None:
    repo, files = _symbol_graph_repo(tmp_path)

    def _dump() -> str:
        graph = build_symbol_graph(str(repo), files, include_http_edges=False)
        return json.dumps({
            f: [(e.target_file, e.target_symbol) for e in edges]
            for f, edges in graph.forward.items()
        }, sort_keys=False)

    assert _dump() == _dump()
