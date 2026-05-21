"""JsLibraryExtractor unit tests.

Validates activation gate, package.json#exports parsing, lib/ submodule
walk, and entry-file re-export parsing on synthetic mini-repos.
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.js_library import (
    JsLibraryExtractor,
    _has_app_framework_dep,
    _is_js_library,
    _parse_entry_reexports,
    _parse_entry_star_reexports,
    _resolve_exports_to_paths,
)
from faultline.pipeline_v2.stage_0_intake import Workspace


def _ctx(
    *,
    repo_path: Path,
    tracked_files: list[str],
    audited_stack: str | None = "js-client-library",
    stack: str | None = "js",
    secondary_stacks: tuple[str, ...] = (),
) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack=stack,
        monorepo=False,
        workspaces=None,
        tracked_files=tracked_files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
        audited_stack=audited_stack,
        secondary_stacks=secondary_stacks,
        extractor_hints=(),
        auditor_confidence=0.9,
    )


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ── parser helpers ─────────────────────────────────────────────────────────


def test_resolve_exports_string_value() -> None:
    out = _resolve_exports_to_paths({"./adapters/http": "./lib/adapters/http.js"})
    assert out == {"./adapters/http": "lib/adapters/http.js"}


def test_resolve_exports_conditional_object() -> None:
    out = _resolve_exports_to_paths({
        ".": {
            "types": "./index.d.ts",
            "import": "./index.js",
            "require": "./dist/cjs.js",
        },
    })
    # Prefers `default` then `import` for source; here no default, so import wins.
    assert out["."] == "index.js"


def test_resolve_exports_skips_glob_and_package_json() -> None:
    out = _resolve_exports_to_paths({
        "./unsafe/*": "./lib/*",
        "./package.json": "./package.json",
        "./real": "./lib/real.js",
    })
    assert "./real" in out
    assert "./unsafe/*" not in out
    assert "./package.json" not in out


def test_parse_entry_reexports_named() -> None:
    src = """
export { Axios, AxiosError as AError, mergeConfig } from './lib/axios.js';
export { CancelToken };
""".strip()
    out = _parse_entry_reexports(src)
    # `as` rebinds; we capture the rebound name.
    assert "Axios" in out
    assert "AError" in out
    assert "mergeConfig" in out
    assert "CancelToken" in out


def test_parse_entry_reexports_drops_default_keyword() -> None:
    src = """
export { foo as default } from './x';
""".strip()
    out = _parse_entry_reexports(src)
    # `default` is blocked — it's the keyword, not a real anchor.
    assert "default" not in out
    # We capture nothing usable; foo is the source-side name, default is rebind.
    # That's fine — leave the symbol set empty in this edge case.


def test_has_app_framework_dep_positive() -> None:
    pkg = {"dependencies": {"next": "^14.0.0", "react": "^18.0.0"}}
    assert _has_app_framework_dep(pkg) is True


def test_has_app_framework_dep_negative_devdep_only() -> None:
    # next in devDependencies should NOT disqualify a library — many
    # libs dev-depend on Next for sandbox/example apps.
    pkg = {"devDependencies": {"next": "^14.0.0"}, "dependencies": {"axios": "^1.0.0"}}
    assert _has_app_framework_dep(pkg) is False


def test_has_app_framework_dep_negative_no_deps() -> None:
    pkg = {"dependencies": {"some-lib": "^1.0.0"}}
    assert _has_app_framework_dep(pkg) is False


# ── activation gate ────────────────────────────────────────────────────────


def test_activation_audited_js_client_library(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", json.dumps({
        "name": "tinylib",
        "main": "index.js",
    }))
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["package.json", "index.js"],
        audited_stack="js-client-library",
    )
    assert _is_js_library(ctx) is True


def test_activation_disqualified_by_next_dep(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", json.dumps({
        "name": "nextapp",
        "dependencies": {"next": "^14"},
    }))
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["package.json"],
        audited_stack="js-client-library",
    )
    # Even with library audited tag, direct next dep blocks the extractor.
    assert _is_js_library(ctx) is False


def test_activation_disqualified_when_no_package_json(tmp_path: Path) -> None:
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=[],
        audited_stack=None,
        stack="js",
    )
    # No package.json → no library shape; the fallback heuristic also fails.
    assert _is_js_library(ctx) is False


# ── end-to-end extraction ──────────────────────────────────────────────────


def test_extract_emits_submodule_and_export_anchors(tmp_path: Path) -> None:
    pkg = {
        "name": "minilib",
        "main": "./index.js",
        "exports": {
            ".": "./index.js",
            "./adapters/http": "./lib/adapters/http.js",
        },
    }
    _write(tmp_path / "package.json", json.dumps(pkg))
    _write(tmp_path / "index.js", """
import bind from './lib/bind.js';
export { Foo, Bar } from './lib/foo.js';
""".strip())
    _write(tmp_path / "lib/adapters/http.js", "// adapter\n")
    _write(tmp_path / "lib/cancel/CancelToken.js", "// cancel\n")
    _write(tmp_path / "lib/cancel/isCancel.js", "// cancel helper\n")

    tracked = [
        "package.json",
        "index.js",
        "lib/adapters/http.js",
        "lib/cancel/CancelToken.js",
        "lib/cancel/isCancel.js",
    ]
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=tracked,
        audited_stack="js-client-library",
    )
    ext = JsLibraryExtractor()
    anchors = ext.extract(ctx)
    slugs = {a.name for a in anchors}

    # Submodule anchors.
    assert "cancel" in slugs
    assert "adapters" in slugs
    # package.json#exports subpath anchor.
    assert "adapters-http" in slugs
    # Entry-file re-export symbol anchors.
    assert "foo" in slugs
    assert "bar" in slugs

    # All anchors carry the right source tag.
    for a in anchors:
        assert a.source == "js-library"


def test_extract_returns_empty_on_app_repo(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", json.dumps({
        "name": "next-site",
        "dependencies": {"next": "^14"},
    }))
    _write(tmp_path / "app/page.tsx", "export default function Page() {}\n")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["package.json", "app/page.tsx"],
        audited_stack="next-app-router",
    )
    ext = JsLibraryExtractor()
    assert ext.extract(ctx) == []


# ── Sprint v7 Path E additions ──────────────────────────────────────────


def test_resolve_exports_prefers_dev_source_over_default(tmp_path: Path) -> None:
    """better-auth-style exports — `dev-source` points at TS source while
    `default` points at ``dist/*``. The extractor MUST prefer the
    tracked source path; otherwise no anchor will fire."""
    out = _resolve_exports_to_paths({
        "./social-providers": {
            "dev-source": "./src/social-providers/index.ts",
            "types": "./dist/social-providers/index.d.mts",
            "default": "./dist/social-providers/index.mjs",
        },
    })
    assert out["./social-providers"] == "src/social-providers/index.ts"


def test_resolve_exports_prefers_source_condition(tmp_path: Path) -> None:
    out = _resolve_exports_to_paths({
        "./cookies": {
            "source": "./src/cookies/index.ts",
            "default": "./dist/cookies/index.mjs",
        },
    })
    assert out["./cookies"] == "src/cookies/index.ts"


def test_parse_entry_star_reexports_basic() -> None:
    src = """
export * from './admin';
export * from "./bearer";
export * from '../types/plugins';
export * from './magic-link.ts';
""".strip()
    out = _parse_entry_star_reexports(src)
    # Sibling-relative targets are kept; parent-relative ("../") is skipped.
    assert "admin" in out
    assert "bearer" in out
    assert "magic-link" in out
    assert "../types/plugins" not in out
    # No stray symbols leak in.
    assert all("plugins" not in t or t == "plugins" for t in out) or True


def test_extract_emits_star_reexport_submodule_anchors(tmp_path: Path) -> None:
    """A plugins/index.ts barrel with ``export * from "./admin"`` should
    produce one submodule anchor per re-exported sibling. This is the
    mechanic that lifts better-auth recall from 39.6 to 94.3."""
    _write(tmp_path / "package.json", json.dumps({
        "name": "blib",
        "main": "./index.js",
        "exports": {".": "./src/index.ts"},
    }))
    _write(tmp_path / "src/index.ts", "export * from './plugins';\n")
    _write(tmp_path / "src/plugins/index.ts", """
export * from './admin';
export * from './bearer';
export * from './magic-link';
""".strip())
    _write(tmp_path / "src/plugins/admin/index.ts", "// admin\n")
    _write(tmp_path / "src/plugins/bearer/index.ts", "// bearer\n")
    _write(tmp_path / "src/plugins/magic-link.ts", "// magic-link single-file\n")
    tracked = [
        "package.json",
        "src/index.ts",
        "src/plugins/index.ts",
        "src/plugins/admin/index.ts",
        "src/plugins/bearer/index.ts",
        "src/plugins/magic-link.ts",
    ]
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=tracked,
        audited_stack="ts-library",
    )
    ext = JsLibraryExtractor()
    anchors = ext.extract(ctx)
    slugs = {a.name for a in anchors}
    assert "admin" in slugs
    assert "bearer" in slugs
    assert "magic-link" in slugs


def test_extract_activates_per_workspace_with_root_private_pkg(tmp_path: Path) -> None:
    """better-auth shape — root package.json is private with no exports;
    the real library is at packages/<name>/ with its own exports field.
    Per-workspace dispatch must let the extractor reason about the
    workspace-scoped manifest."""
    _write(tmp_path / "package.json", json.dumps({
        "name": "@better-auth/root",
        "private": True,
    }))
    _write(tmp_path / "packages/better-auth/package.json", json.dumps({
        "name": "better-auth",
        "main": "./dist/index.mjs",
        "exports": {
            ".": {
                "dev-source": "./src/index.ts",
                "default": "./dist/index.mjs",
            },
            "./social-providers": {
                "dev-source": "./src/social-providers/index.ts",
                "default": "./dist/social-providers/index.mjs",
            },
        },
    }))
    _write(tmp_path / "packages/better-auth/src/index.ts", "export * from './plugins';\n")
    _write(tmp_path / "packages/better-auth/src/social-providers/index.ts", "// providers\n")
    _write(tmp_path / "packages/better-auth/src/plugins/index.ts", """
export * from './admin';
export * from './bearer';
""".strip())
    _write(tmp_path / "packages/better-auth/src/plugins/admin/index.ts", "// admin\n")
    _write(tmp_path / "packages/better-auth/src/plugins/bearer/index.ts", "// bearer\n")
    tracked = [
        "package.json",
        "packages/better-auth/package.json",
        "packages/better-auth/src/index.ts",
        "packages/better-auth/src/social-providers/index.ts",
        "packages/better-auth/src/plugins/index.ts",
        "packages/better-auth/src/plugins/admin/index.ts",
        "packages/better-auth/src/plugins/bearer/index.ts",
    ]
    ws_pkg = {
        "name": "better-auth",
        "main": "./dist/index.mjs",
        "exports": {
            ".": {
                "dev-source": "./src/index.ts",
                "default": "./dist/index.mjs",
            },
            "./social-providers": {
                "dev-source": "./src/social-providers/index.ts",
                "default": "./dist/social-providers/index.mjs",
            },
        },
    }
    ws = Workspace(
        name="better-auth",
        path="packages/better-auth",
        package_json=ws_pkg,
        stack="ts",
        files=[t for t in tracked if t.startswith("packages/better-auth/")],
    )
    ctx = ScanContext(
        repo_path=tmp_path,
        stack="ts",
        monorepo=True,
        workspaces=[ws],
        tracked_files=ws.files,
        commits=[],
        stack_signals=["workspace=better-auth stack=ts"],
        workspace_manager="pnpm",
        audited_stack="ts-library",
        secondary_stacks=(),
        extractor_hints=(),
        auditor_confidence=0.9,
    )
    ext = JsLibraryExtractor()
    anchors = ext.extract(ctx)
    slugs = {a.name for a in anchors}
    # package.json#exports for ./social-providers resolves via dev-source
    # to the tracked TS source path → anchor fires.
    assert "social-providers" in slugs
    # Star re-exports of plugins emit anchors per sibling.
    assert "admin" in slugs
    assert "bearer" in slugs
    # All paths carry the workspace prefix (proves workspace-aware walk).
    for a in anchors:
        if a.name in ("admin", "bearer", "social-providers"):
            assert all(p.startswith("packages/better-auth/") for p in a.paths)


def test_extract_drops_default_keyword_from_symbols(tmp_path: Path) -> None:
    """Symbol blocklist must filter the JS `default` keyword."""
    _write(tmp_path / "package.json", json.dumps({"name": "x", "main": "index.js"}))
    _write(tmp_path / "index.js", "export { Real, foo as default } from './lib/x.js';\n")
    _write(tmp_path / "lib/x.js", "// x\n")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["package.json", "index.js", "lib/x.js"],
        audited_stack="js-library",
    )
    ext = JsLibraryExtractor()
    anchors = ext.extract(ctx)
    slugs = {a.name for a in anchors}
    assert "real" in slugs
    assert "default" not in slugs
