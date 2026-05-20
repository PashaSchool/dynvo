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
    _resolve_exports_to_paths,
)


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
