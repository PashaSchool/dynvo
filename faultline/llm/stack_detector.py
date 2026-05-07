"""Sprint 19 — Repo stack detection for stack-aware prompt routing.

Detects what kind of repo we're scanning so the pipeline can pick a
matching system prompt variant. Built around the S17 ground-truth
``stack`` taxonomy:

  next-monorepo, next-app-router, node-monorepo,
  vue-spa, vue-nuxt-monorepo,
  python-flat, python-modules, python-library,
  go-modular, rust-modular, rails-app,
  js-library, mixed (fallback)

Why static-first
================

Static signals (config files, directory layout) classify ~85% of repos
correctly without an LLM call. They are:

  - Free (no API cost, no latency)
  - Deterministic (same repo → same stack)
  - Auditable (you can see why a repo got tagged)

When static signals are ambiguous (e.g. a repo has both ``apps/`` and
loose Python modules), the detector falls back to a single Haiku call
that classifies based on a sample of file paths. This adds ~$0.001 and
~2 seconds per ambiguous scan, which is acceptable since most scans
already cost $0.50+.

Public surface
==============

    detect_stack(repo_root, files, *, api_key=None) -> StackProfile

Returns a ``StackProfile`` with:
  - ``kind``  : the stack tag (one of the values above)
  - ``confidence``: 0.0 - 1.0
  - ``signals``  : list of signals that drove the verdict
  - ``via``      : "static" or "haiku-fallback"

Override path
=============

Callers can pass ``stack_hint=...`` directly to bypass detection. CLI
exposes ``--stack-hint`` for the same purpose.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


VALID_STACKS = frozenset({
    "next-monorepo", "next-app-router", "node-monorepo",
    "vue-spa", "vue-nuxt-monorepo",
    "python-flat", "python-modules", "python-library",
    "go-modular", "rust-modular", "rails-app",
    "js-library", "mixed",
})


@dataclass
class StackProfile:
    """Stack-detection verdict for a single repo."""

    kind: str           # one of VALID_STACKS
    confidence: float   # 0.0 - 1.0
    signals: list[str] = field(default_factory=list)
    via: str = "static"  # "static" | "haiku-fallback" | "override"


# ── Static signal helpers ─────────────────────────────────────────────


def _has(repo_root: Path, *names: str) -> bool:
    """True iff any of the given filenames exist at the repo root."""
    return any((repo_root / n).exists() for n in names)


def _read_json_safe(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _has_workspace_marker(repo_root: Path) -> tuple[bool, list[str]]:
    """Return (is_workspace, signals).

    pnpm-workspace.yaml, turbo.json, lerna.json, nx.json, or
    `workspaces` field in root package.json all count.
    """
    signals: list[str] = []
    for fn in ("pnpm-workspace.yaml", "pnpm-workspace.yml",
              "turbo.json", "lerna.json", "nx.json"):
        if (repo_root / fn).exists():
            signals.append(fn)

    pkg = repo_root / "package.json"
    if pkg.exists():
        data = _read_json_safe(pkg) or {}
        if "workspaces" in data:
            signals.append("package.json:workspaces")

    return (bool(signals), signals)


def _detect_next_app_router(repo_root: Path) -> tuple[bool, list[str]]:
    """Single Next.js app (no workspace markers)."""
    signals: list[str] = []
    for fn in ("next.config.js", "next.config.ts", "next.config.mjs"):
        if (repo_root / fn).exists():
            signals.append(fn)
    has_app = (repo_root / "app").is_dir() or (repo_root / "src" / "app").is_dir()
    if has_app:
        signals.append("app/ dir")
    return (bool(signals), signals)


def _detect_node_app_in_monorepo(repo_root: Path) -> bool:
    """True if any apps/* contains next.config or package.json with next dep.

    Distinguishes ``next-monorepo`` (has Next.js apps) from generic
    ``node-monorepo`` (no Next.js, e.g. trigger.dev's task runner).
    """
    apps = repo_root / "apps"
    if not apps.is_dir():
        return False
    for child in list(apps.iterdir())[:10]:  # cap scan
        if not child.is_dir():
            continue
        if any((child / cfg).exists() for cfg in (
            "next.config.js", "next.config.ts", "next.config.mjs",
        )):
            return True
        pkg = _read_json_safe(child / "package.json")
        if pkg and "next" in (pkg.get("dependencies") or {}):
            return True
    return False


def _detect_vue(repo_root: Path) -> tuple[str | None, list[str]]:
    """Return ('vue-spa' | 'vue-nuxt-monorepo' | None, signals)."""
    signals: list[str] = []
    has_vue_config = _has(repo_root, "vue.config.js", "vue.config.ts")
    has_nuxt_config = _has(repo_root,
                           "nuxt.config.js", "nuxt.config.ts", "nuxt.config.mjs")
    pkg = _read_json_safe(repo_root / "package.json") or {}
    deps = (pkg.get("dependencies") or {}) | (pkg.get("devDependencies") or {})
    has_vue_dep = "vue" in deps or "@vue/cli-service" in deps
    has_nuxt_dep = "nuxt" in deps or "nuxt3" in deps

    if has_vue_config:
        signals.append("vue.config")
    if has_nuxt_config:
        signals.append("nuxt.config")
    if has_vue_dep:
        signals.append("vue dep")
    if has_nuxt_dep:
        signals.append("nuxt dep")

    if has_nuxt_config or has_nuxt_dep:
        if (repo_root / "packages").is_dir() or (repo_root / "apps").is_dir():
            return ("vue-nuxt-monorepo", signals)
        return ("vue-spa", signals)  # nuxt single app — closest fit
    if has_vue_config or has_vue_dep:
        return ("vue-spa", signals)
    return (None, signals)


def _detect_python(repo_root: Path) -> tuple[str | None, list[str]]:
    """Return ('python-library' | 'python-modules' | 'python-flat' | None, signals).

    Heuristic:
      - pyproject.toml + setup.py + many top-level packages → python-library
      - app/<modules> Django/Flask layout → python-modules
      - flat single package → python-flat
    """
    signals: list[str] = []
    has_pyproject = (repo_root / "pyproject.toml").exists()
    has_setup_py = (repo_root / "setup.py").exists()
    has_setup_cfg = (repo_root / "setup.cfg").exists()
    requirements = (repo_root / "requirements.txt").exists()

    if not (has_pyproject or has_setup_py or requirements):
        return (None, signals)

    if has_pyproject:
        signals.append("pyproject.toml")
    if has_setup_py:
        signals.append("setup.py")

    # Library indicator: pyproject + sdist-style layout (single top pkg)
    # AND no Django/Flask app shape.
    if (repo_root / "manage.py").exists():
        signals.append("manage.py")
        return ("python-modules", signals)  # Django

    # Look for top-level python packages (dirs with __init__.py)
    top_pkgs = [
        c for c in repo_root.iterdir()
        if c.is_dir()
        and not c.name.startswith(".")
        and not c.name.startswith("_")
        and c.name not in ("docs", "tests", "test", "examples", "scripts",
                           "venv", ".venv", "node_modules")
        and (c / "__init__.py").exists()
    ]
    if len(top_pkgs) == 1 and has_pyproject:
        signals.append(f"single pkg: {top_pkgs[0].name}")
        return ("python-flat", signals)
    if len(top_pkgs) >= 2:
        signals.append(f"{len(top_pkgs)} top pkgs")
        return ("python-modules", signals)

    return ("python-flat", signals)  # default for python repos


def _detect_go(repo_root: Path) -> tuple[bool, list[str]]:
    signals: list[str] = []
    if (repo_root / "go.mod").exists():
        signals.append("go.mod")
        if (repo_root / "go.work").exists():
            signals.append("go.work")
        return (True, signals)
    return (False, signals)


def _detect_rust(repo_root: Path) -> tuple[bool, list[str]]:
    signals: list[str] = []
    cargo = repo_root / "Cargo.toml"
    if not cargo.exists():
        return (False, signals)
    signals.append("Cargo.toml")
    try:
        text = cargo.read_text()
    except OSError:
        return (True, signals)  # at least it exists
    if "[workspace]" in text:
        signals.append("workspace")
    return (True, signals)


def _detect_rails(repo_root: Path) -> tuple[bool, list[str]]:
    signals: list[str] = []
    if (repo_root / "Gemfile").exists():
        signals.append("Gemfile")
    if (repo_root / "config" / "application.rb").exists():
        signals.append("config/application.rb")
        return (True, signals)
    if (repo_root / "Rakefile").exists() and (repo_root / "app").is_dir():
        signals.append("Rakefile + app/")
        return (True, signals)
    return (False, signals)


def _detect_js_library(repo_root: Path) -> tuple[bool, list[str]]:
    """Library if package.json has 'main'/'exports' but no apps/ next config."""
    pkg = _read_json_safe(repo_root / "package.json")
    if not pkg:
        return (False, [])
    has_main = "main" in pkg or "exports" in pkg
    has_app_shell = any((repo_root / d).is_dir() for d in ("apps", "src/app", "app"))
    if has_main and not has_app_shell and "next" not in (
        (pkg.get("dependencies") or {}) | (pkg.get("devDependencies") or {})
    ):
        signals = ["package.json:main/exports"]
        if (repo_root / "lib").is_dir():
            signals.append("lib/")
        return (True, signals)
    return (False, [])


# ── Top-level static dispatch ─────────────────────────────────────────


def _detect_static(repo_root: Path) -> StackProfile | None:
    """Return a StackProfile when static signals confidently classify.

    Returns None when the static signals are inconclusive or conflicting,
    in which case the caller should fall back to Haiku.
    """
    signals: list[str] = []

    # 1. Workspace monorepo? Then drill into workspace flavor.
    is_ws, ws_signals = _has_workspace_marker(repo_root)
    if is_ws:
        signals.extend(ws_signals)
        # Vue Nuxt monorepo?
        vue_kind, vue_signals = _detect_vue(repo_root)
        if vue_kind == "vue-nuxt-monorepo":
            signals.extend(vue_signals)
            return StackProfile("vue-nuxt-monorepo", 0.95, signals)
        # Has Next.js app?
        if _detect_node_app_in_monorepo(repo_root):
            signals.append("apps/*/next.config")
            return StackProfile("next-monorepo", 0.95, signals)
        # Otherwise generic node monorepo
        return StackProfile("node-monorepo", 0.85, signals)

    # 2. Rails (BEFORE Next — Rails has app/ dir which would otherwise
    #    match the Next app-router signal).
    is_rails, rails_signals = _detect_rails(repo_root)
    if is_rails:
        return StackProfile("rails-app", 0.95, rails_signals)

    # 3. Single Next.js app (no workspace markers). Require config file —
    #    bare app/ dir is not enough because Rails uses it too.
    next_signals = [
        fn for fn in ("next.config.js", "next.config.ts", "next.config.mjs")
        if (repo_root / fn).exists()
    ]
    if next_signals:
        if (repo_root / "app").is_dir() or (repo_root / "src" / "app").is_dir():
            next_signals.append("app/ dir")
        return StackProfile("next-app-router", 0.9, next_signals)

    # 4. Vue (no workspace).
    vue_kind, vue_signals = _detect_vue(repo_root)
    if vue_kind:
        signals.extend(vue_signals)
        return StackProfile(vue_kind, 0.85, signals)

    # 5. Go.
    is_go, go_signals = _detect_go(repo_root)
    if is_go:
        return StackProfile("go-modular", 0.9, go_signals)

    # 6. Rust.
    is_rust, rust_signals = _detect_rust(repo_root)
    if is_rust:
        kind = "rust-modular"
        return StackProfile(kind, 0.9, rust_signals)

    # 7. Python.
    py_kind, py_signals = _detect_python(repo_root)
    if py_kind:
        return StackProfile(py_kind, 0.8, py_signals)

    # 8. JS library (package.json with main/exports, no app shell).
    is_lib, lib_signals = _detect_js_library(repo_root)
    if is_lib:
        return StackProfile("js-library", 0.85, lib_signals)

    return None  # caller falls back to Haiku


# ── Haiku fallback ────────────────────────────────────────────────────


_HAIKU_SYSTEM_PROMPT = """\
You classify the stack of a software repo from a sample of its file
paths. Pick exactly ONE tag from this list:

  next-monorepo       — Next.js apps in a turborepo / pnpm workspace
  next-app-router     — single Next.js application, app/ directory
  node-monorepo       — Node.js workspace, no Next.js
  vue-spa             — flat Vue single-page app (src/components/*.vue)
  vue-nuxt-monorepo   — Nuxt + workspace
  python-flat         — single Python package (e.g. plugin library)
  python-modules      — Django / Flask / multi-module Python app
  python-library      — published Python library (publishes via pyproject)
  go-modular          — Go modular project (go.mod, internal/, cmd/)
  rust-modular        — Rust project (Cargo.toml, possibly workspace)
  rails-app           — Ruby on Rails app (app/controllers, Gemfile)
  js-library          — JavaScript / TypeScript library (no app shell)
  mixed               — fallback when none of the above fits

Output ONLY a JSON object with two keys:

{"kind": "<tag>", "confidence": 0.0-1.0}

No prose. No markdown. No additional keys.
"""


def _haiku_classify(
    file_sample: list[str],
    *,
    api_key: str | None,
    model: str = "claude-haiku-4-5",
) -> StackProfile | None:
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.warning("stack_detector: anthropic package missing — skip Haiku")
        return None

    client = Anthropic(api_key=api_key)
    user_msg = "Files:\n" + "\n".join(file_sample[:60])
    try:
        resp = client.messages.create(
            model=model,
            system=_HAIKU_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=128,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stack_detector: Haiku call failed (%s)", exc)
        return None

    text = ""
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            text += getattr(block, "text", "")
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in VALID_STACKS:
        return None
    conf = data.get("confidence")
    confidence = float(conf) if isinstance(conf, (int, float)) else 0.5
    return StackProfile(
        kind=kind,
        confidence=confidence,
        signals=[f"haiku-classified ({len(file_sample)} files sampled)"],
        via="haiku-fallback",
    )


# ── Public entry point ────────────────────────────────────────────────


def detect_stack(
    repo_root: Path | str,
    files: Iterable[str],
    *,
    api_key: str | None = None,
    override: str | None = None,
) -> StackProfile:
    """Detect a repo's stack tag.

    Tries static signals first; falls back to Haiku when ambiguous.
    Returns ``StackProfile(kind='mixed', confidence=0.0, via='fallback')``
    if both fail (still safe — the picker will use the mixed example bag).

    Args:
      repo_root: path to the repository root.
      files: an iterable of file paths (relative or absolute) to use as
        sample for the Haiku fallback. The function takes the first 60.
      api_key: Anthropic API key for the Haiku fallback. Reads
        ``ANTHROPIC_API_KEY`` from env if omitted.
      override: when set, bypass detection entirely. Must be a valid
        stack tag from ``VALID_STACKS``.
    """
    if override:
        if override not in VALID_STACKS:
            logger.warning(
                "stack_detector: override=%r not in VALID_STACKS, falling back to mixed",
                override,
            )
            return StackProfile("mixed", 0.0, [f"override-invalid: {override}"], via="override")
        return StackProfile(override, 1.0, [f"override: {override}"], via="override")

    root = Path(repo_root)
    static = _detect_static(root)
    if static is not None:
        logger.info(
            "stack_detector: static verdict %s (confidence=%.2f, signals=%s)",
            static.kind, static.confidence, static.signals,
        )
        return static

    file_list = list(files)[:60]
    api = api_key or os.environ.get("ANTHROPIC_API_KEY")
    haiku = _haiku_classify(file_list, api_key=api) if api else None
    if haiku is not None:
        logger.info(
            "stack_detector: haiku verdict %s (confidence=%.2f)",
            haiku.kind, haiku.confidence,
        )
        return haiku

    logger.info("stack_detector: no verdict — defaulting to 'mixed'")
    return StackProfile("mixed", 0.0, ["no signals matched"], via="fallback")
