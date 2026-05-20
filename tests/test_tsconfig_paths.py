"""Tests for :mod:`faultline.analyzer.tsconfig_paths` (Sprint C3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.analyzer.tsconfig_paths import (
    AliasEntry,
    build_path_alias_map,
    resolve_ts_import,
)


def _write(p: Path, content: str | dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict):
        p.write_text(json.dumps(content, indent=2))
    else:
        p.write_text(content)


def test_single_tsconfig_with_paths(tmp_path: Path) -> None:
    """A single root tsconfig with ``paths`` produces alias entries."""
    _write(tmp_path / "tsconfig.json", {
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["./src/*"]},
        },
    })
    entries = build_path_alias_map(tmp_path)
    assert len(entries) == 1
    assert entries[0].prefix == "@/"
    assert entries[0].target_prefix == "src/"
    assert entries[0].workspace_root == ""


def test_tsconfig_extends_relative_file(tmp_path: Path) -> None:
    """``extends`` to a sibling file merges paths from the parent."""
    _write(tmp_path / "tsconfig.base.json", {
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@base/*": ["./shared/*"]},
        },
    })
    _write(tmp_path / "tsconfig.json", {
        "extends": "./tsconfig.base.json",
        "compilerOptions": {
            "paths": {"@/*": ["./src/*"]},
        },
    })
    entries = build_path_alias_map(tmp_path)
    # Child paths block fully replaces parent's (per TS spec) — we
    # expect @/* only on this config.
    # BUT the parent config also gets walked directly, contributing
    # its own entry.
    prefixes = {e.prefix for e in entries}
    assert "@/" in prefixes
    assert "@base/" in prefixes  # contributed by tsconfig.base.json directly


def test_tsconfig_extends_sibling_package(tmp_path: Path) -> None:
    """``extends`` to a sibling workspace package resolves via
    ``packages/<name>``.
    """
    _write(tmp_path / "packages" / "tsconfig" / "nextjs.json", {
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@org/shared/*": ["./shared/*"]},
        },
    })
    _write(tmp_path / "apps" / "web" / "tsconfig.json", {
        "extends": "tsconfig/nextjs.json",
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["./*"]},
        },
    })
    entries = build_path_alias_map(tmp_path)
    # apps/web should have @/ resolving to apps/web/
    web_aliases = [e for e in entries if e.workspace_root == "apps/web"]
    assert any(e.prefix == "@/" and e.target_prefix == "apps/web/"
               for e in web_aliases)


def test_extends_missing_target_is_graceful(tmp_path: Path) -> None:
    """A bogus extends reference yields the OWN config's paths only."""
    _write(tmp_path / "tsconfig.json", {
        "extends": "nonexistent/config.json",
        "compilerOptions": {
            "paths": {"@/*": ["./src/*"]},
        },
    })
    entries = build_path_alias_map(tmp_path)
    assert len(entries) == 1
    assert entries[0].prefix == "@/"


def test_alias_with_wildcard_root_target(tmp_path: Path) -> None:
    """``@/*`` → ``./*`` at repo root yields empty target_prefix
    (concatenation contract — prefix ``@/`` + remainder gives the
    correct repo-relative path with no leading slash)."""
    _write(tmp_path / "tsconfig.json", {
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["./*"]},
        },
    })
    entries = build_path_alias_map(tmp_path)
    assert len(entries) == 1
    assert entries[0].prefix == "@/"
    # Empty string is the sentinel for "repo root" so the resolver
    # produces ``"" + "components/x.tsx" == "components/x.tsx"``.
    assert entries[0].target_prefix == ""


def test_alias_with_wildcard_subdir_target(tmp_path: Path) -> None:
    """``@/*`` → ``./src/*`` yields ``src/`` with trailing slash."""
    _write(tmp_path / "tsconfig.json", {
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["./src/*"]},
        },
    })
    entries = build_path_alias_map(tmp_path)
    assert len(entries) == 1
    assert entries[0].prefix == "@/"
    assert entries[0].target_prefix == "src/"


def test_multi_workspace_alias_map(tmp_path: Path) -> None:
    """Two workspaces with their own tsconfigs both contribute entries."""
    _write(tmp_path / "apps" / "web" / "tsconfig.json", {
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["./*"]},
        },
    })
    _write(tmp_path / "apps" / "admin" / "tsconfig.json", {
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"~/*": ["./*"]},
        },
    })
    entries = build_path_alias_map(tmp_path)
    workspaces = {e.workspace_root for e in entries}
    assert "apps/web" in workspaces
    assert "apps/admin" in workspaces


def test_resolve_ts_import_uses_longest_prefix(tmp_path: Path) -> None:
    """When two aliases overlap, the LONGER one wins."""
    alias_map = [
        AliasEntry("@/utils/", "apps/web", "apps/web/utils/"),
        AliasEntry("@/", "apps/web", "apps/web/"),
    ]
    # Sort longest first (this is what build_path_alias_map does).
    alias_map.sort(key=lambda e: -len(e.prefix))
    tracked = frozenset({
        "apps/web/utils/foo.ts",
        "apps/web/foo.ts",
    })
    resolved = resolve_ts_import(
        "apps/web/page.tsx", "@/utils/foo",
        alias_map=alias_map, tracked_files=tracked,
    )
    assert resolved == "apps/web/utils/foo.ts"


def test_resolve_relative_import(tmp_path: Path) -> None:
    """Relative imports bypass the alias map entirely."""
    tracked = frozenset({"apps/web/lib/foo.ts"})
    resolved = resolve_ts_import(
        "apps/web/lib/bar.ts", "./foo",
        alias_map=[],
        tracked_files=tracked,
    )
    assert resolved == "apps/web/lib/foo.ts"


def test_resolve_bare_import_returns_none(tmp_path: Path) -> None:
    """``react`` resolves to None (external package)."""
    assert resolve_ts_import(
        "apps/web/page.tsx", "react",
        alias_map=[], tracked_files=frozenset({"apps/web/page.tsx"}),
    ) is None


def test_jsonc_comments_in_tsconfig(tmp_path: Path) -> None:
    """tsconfig with // comments + trailing commas parses cleanly."""
    (tmp_path / "tsconfig.json").write_text("""
    {
      // The root config.
      "compilerOptions": {
        "baseUrl": ".",
        "paths": {
          "@/*": ["./src/*"],  /* trailing comma below */
        },
      },
    }
    """.strip())
    entries = build_path_alias_map(tmp_path)
    assert any(e.prefix == "@/" for e in entries)
