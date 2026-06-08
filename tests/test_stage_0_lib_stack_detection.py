"""Tests for Commit B — pure-library stack misdetection fix.

`_detect_js_stack` must classify on RUNTIME deps only (dependencies +
peerDependencies), and match deps by exact-or-scoped-prefix rather than
substring. So:

  - a library that DEV-depends on express (test server) → js-generic
  - a library whose only "vite" signal is `vitest` in devDeps → js-generic
  - a real app that RUNTIME-depends on express → stays express
  - a got-shaped exports-map lib → js-generic AND _is_js_library activates

Synthetic neutral fixtures only (memory/rule-no-repo-specific-paths).
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2 import stage_0_intake
from faultline.pipeline_v2.extractors.js_library import _is_js_library
from faultline.pipeline_v2.stage_0_intake import detect_stack


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    _write(path, json.dumps(data, indent=2))


# ── (a) lib dev-depending on express → js-generic, not express ──────────────


def test_lib_dev_depends_express_is_js_generic(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {
        "name": "tinylib",
        "version": "1.0.0",
        "main": "dist/index.js",
        "exports": {".": "./source/index.ts"},
        "devDependencies": {"express": "^4", "typescript": "^5"},
    })
    _write(tmp_path / "source" / "index.ts", "export const x = 1\n")
    stack, _signals = detect_stack(tmp_path, [])
    assert stack == "js-generic"


# ── (b) lib dev-depending on vitest → js-generic, not vite ──────────────────


def test_lib_dev_depends_vitest_is_js_generic(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {
        "name": "tinylib2",
        "version": "1.0.0",
        "module": "dist/index.mjs",
        "exports": {".": "./src/index.ts"},
        "devDependencies": {"vitest": "^2", "typescript": "^5"},
    })
    _write(tmp_path / "src" / "index.ts", "export const y = 2\n")
    stack, _signals = detect_stack(tmp_path, [])
    assert stack == "js-generic"


# ── (c) real app runtime-depending on express → stays express ───────────────


def test_app_runtime_depends_express_stays_express(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {
        "name": "myapp",
        "version": "1.0.0",
        "dependencies": {"express": "^4"},
    })
    _write(tmp_path / "server.js", "const app = require('express')()\n")
    stack, _signals = detect_stack(tmp_path, [])
    assert stack == "express"


def test_app_runtime_depends_vite_stays_vite(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {
        "name": "spa",
        "version": "1.0.0",
        "dependencies": {"vite": "^5", "react": "^18"},
    })
    _write(tmp_path / "src" / "main.tsx", "export default function App() {}\n")
    stack, _signals = detect_stack(tmp_path, [])
    assert stack == "vite"


def test_vite_config_present_stays_vite(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {
        "name": "spa2",
        "version": "1.0.0",
        "devDependencies": {"vite": "^5"},
    })
    _write(tmp_path / "vite.config.ts", "export default {}\n")
    _write(tmp_path / "src" / "main.ts", "console.log('x')\n")
    stack, _signals = detect_stack(tmp_path, [])
    assert stack == "vite"


def test_express_substring_dep_does_not_match(tmp_path: Path) -> None:
    # A runtime dep on express-rate-limit must NOT be read as "express".
    _write_json(tmp_path / "package.json", {
        "name": "ratelib",
        "version": "1.0.0",
        "exports": {".": "./src/index.ts"},
        "dependencies": {"express-rate-limit": "^7"},
    })
    _write(tmp_path / "src" / "index.ts", "export const z = 3\n")
    stack, _signals = detect_stack(tmp_path, [])
    assert stack == "js-generic"


# ── (d) got-shaped exports-map fixture → _is_js_library activates ───────────


def test_got_shaped_lib_activates_js_library(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", {
        "name": "fetchlib",
        "version": "1.0.0",
        "type": "module",
        "exports": {".": "./dist/source/index.js"},
        "main": "dist/source/index.js",
        # Express only in devDeps (its own test server) — must not flip
        # the stack to express nor disqualify the library extractor.
        "devDependencies": {"express": "^4", "vitest": "^2"},
    })
    _write(tmp_path / "source" / "index.ts", "export * from './core/options'\n")
    _write(tmp_path / "source" / "core" / "options.ts", "export const o = {}\n")

    ctx = stage_0_intake(tmp_path, skip_git=True)
    assert ctx.stack == "js-generic"
    assert _is_js_library(ctx) is True
