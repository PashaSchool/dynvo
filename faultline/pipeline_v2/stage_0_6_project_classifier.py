"""Stage 0.6b — Per-Workspace Project Classifier + Monorepo Partition Plan.

Deterministic (no LLM) classification of EACH enumerated monorepo
workspace/package into a ``project_type``, followed by a partition plan
that decides which workspaces become independent SCAN UNITS (subpaths
fed to :func:`faultline.pipeline_v2.multi.run_pipeline_multi`).

This is the BRAIN-PARTITIONER core. It is the per-PROJECT sibling of
Stage 0.6 (:mod:`faultline.pipeline_v2.stage_0_6_shape`), which
classifies the WHOLE-REPO architecture shape. Where Stage 0.6 answers
"how is this repo laid out?", this stage answers "which of its
workspaces are real scan-worthy projects, and which ride along or are
excluded?".

Why this matters
================

The decided engine contract is ``engine(repo_root, subpaths[]) ->
result-per-path`` (memory: project-monorepo-subprojects-2026-06-09).
The multi-subpath orchestrator (``run_pipeline_multi``) already runs
the pipeline once per subpath sharing ONE git pass. This module's job
is purely to DECIDE the subpath list — it feeds, never re-implements,
that orchestrator.

A naive "scan every workspace" would be catastrophic: cal.com
enumerates 219 workspaces (``packages/app-store/*`` and
``packages/features/*`` globs explode into ~200 leaf libraries). Most
are shared libraries, not independent products. The partition rule
"only ``app`` + ``service`` workspaces become units" collapses those
219 enumerated packages to a handful of real scan-units, with the
libraries riding along inside the whole-repo tree.

Design tenets (mirrors stage_0_6_shape.py)
==========================================

  - Pure functions. ``classify_project`` and ``partition_monorepo`` are
    idempotent: same inputs -> same output. Safe to call without
    ``ctx.run_dir`` (CLI mode).
  - Composition over inheritance. Every ``project_type`` is a tiny
    standalone class implementing the :class:`ProjectClassifier`
    Protocol. No base class, no shared implementation via inheritance —
    shared logic is imported as functions from ``stage_0_6_shape``.
  - Strategy registry. ``_DEFAULT_CLASSIFIERS`` is the single source of
    truth; tests inject fakes via the optional ``classifiers`` argument.
  - Universal, scale-invariant rules. NO magic numbers tuned to one
    repo (see memory: rule-no-magic-tuning). NO repo-specific paths or
    folder names baked in (see memory: rule-no-repo-specific-paths);
    only INDUSTRY-STANDARD path-segment conventions (``examples/``,
    ``e2e/``) and tooling NAME conventions (``*-config``, ``*-cli``),
    which are the same class of signal the workspace markers themselves
    are.
  - No README parsing. Only manifests (already-parsed
    ``Workspace.package_json``) + folder presence. See ``CLAUDE.md``.
  - Graceful degradation. A buggy classifier returns ``None`` (logged);
    the next classifier runs. The :class:`ResidualClassifier` fallback
    ALWAYS returns a result.
  - Additive + safe. ``partition_monorepo`` is a deterministic helper
    callable from the CLI BEFORE ``run_pipeline_multi``; it does NOT
    change the default scan behaviour. The orchestration-switch (auto-
    feeding units) is a later phase.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Sequence, runtime_checkable

# Reuse Stage 0.6's deterministic manifest/folder helpers — DO NOT
# re-implement. These take an arbitrary directory root so they work
# per-workspace just as well as per-repo.
from faultline.pipeline_v2.stage_0_6_shape import (
    _detect_fastapi_factory,
    _has_cmd_with_main_go,
    _has_framework_manifest,
    _has_top_level_go_files,
    _is_split_fullstack,
    _pyproject_has_scripts,
    _read_json_safe,
    _read_text_safe,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace

logger = logging.getLogger(__name__)


# ── Universal thresholds (scale-invariant, NOT per-repo tuning) ─────────

# A monorepo needs at least this many enumerated workspaces to be worth
# partitioning. ``2`` is the structural minimum for "more than one
# project" — it is the definition of plurality, not a tuned knob. A
# single declared workspace is just a repo with a manifest in a
# subfolder; partitioning it adds nothing over a whole-repo scan.
MIN_WORKSPACES_FOR_PARTITION: int = 2

# Confidence a classifier reports when a hard structural signal fires
# (e.g. a server-framework dependency present). "High" = unambiguous
# manifest evidence.
CONF_STRONG: float = 0.95
# Confidence when a softer but still structural signal fires (e.g. a
# published-library export shape with no contradicting signal).
CONF_MEDIUM: float = 0.80
# Confidence for a name-convention-only match (tooling suffix) — real
# but the weakest of the structural signals, so it ranks last.
CONF_WEAK: float = 0.60
# The residual fallback's confidence — "below half of evidence".
CONF_RESIDUAL: float = 0.40


# ── Scale-invariant signal vocabularies ────────────────────────────────
#
# These are INDUSTRY-STANDARD conventions, not paths observed in one
# corpus repo. They are the same class of structural signal as the
# workspace markers (pnpm-workspace.yaml, turbo.json) the engine
# already trusts. Each is matched as a whole path SEGMENT or a whole
# package-NAME token / suffix — never a substring of an arbitrary path —
# so a directory merely *containing* the word doesn't false-fire.

# Path SEGMENTS that mark sample / demo / non-product code. Matched
# case-insensitively against any single segment of the workspace path
# (so ``examples/auth`` and ``packages/foo/__fixtures__`` both fire,
# but ``my-examples-app`` as a single dir name does NOT — it is not the
# bare segment ``examples``). These are the canonical names the JS/Go/
# Rust/Python ecosystems use for non-shipping sample code + acceptance
# test harnesses.
_EXAMPLE_PATH_SEGMENTS: frozenset[str] = frozenset({
    "example",
    "examples",
    "sample",
    "samples",
    "template",
    "templates",
    "demo",
    "demos",
    "fixture",
    "fixtures",
    "__fixtures__",
    "e2e",
    "playground",
    "playgrounds",
    "sandbox",
    "sandboxes",
    "test-apps",
    "example-apps",
    "example-app",
    # Test-harness packages (``packages/tests``, ``e2e``, ``integration-tests``)
    # are NOT product scan units. ``e2e`` is already above; ``test``/``tests``
    # cover the common monorepo convention of a dedicated test-harness package
    # (e.g. a package whose only job is to exercise the siblings). This is
    # defense-in-depth alongside the server-ENTRY-POINT requirement in
    # :class:`ServiceClassifier` — a test harness that pulls in a server
    # framework as a devDependency (with no real server entry) must never
    # become a service unit.
    "test",
    "tests",
    "integration-tests",
})

# Client-side UI framework dependency keys — presence (together with a
# routes/pages dir) marks a user-facing ``app``. Superset reuse of the
# Stage 0.6 react/vue/vite vocabularies plus the other major UI stacks.
_CLIENT_FRAMEWORK_DEPS: frozenset[str] = frozenset({
    "next",
    "react",
    "react-dom",
    "react-router",
    "react-router-dom",
    "@remix-run/react",
    "@tanstack/react-router",
    "vue",
    "nuxt",
    "svelte",
    "@sveltejs/kit",
    "@angular/core",
    "astro",
    "solid-js",
    "@builder.io/qwik",
    "preact",
})

# Server-side HTTP-server framework dependency keys — presence (without
# client routes) marks a backend ``service``. Deliberately limited to
# frameworks that RUN an HTTP/RPC server process. NOT included:
# ``@trpc/server``, ``ws``, ``graphql`` — those are LIBRARY building
# blocks imported by shared packages (cal.com's ``packages/trpc`` is the
# tRPC router library, not a running service); including them
# misclassifies libraries as services.
_SERVER_FRAMEWORK_DEPS: frozenset[str] = frozenset({
    "express",
    "fastify",
    "@nestjs/core",
    "koa",
    "hono",
    "@hapi/hapi",
    "apollo-server",
    "@apollo/server",
    "graphql-yoga",
})

# Rust HTTP/RPC-server framework crate names. A Cargo crate that depends
# on one of these AND has a binary entry (``src/main.rs`` or a Cargo
# ``[[bin]]`` table) BOOTS a server process and is a ``service`` — the
# direct Rust analogue of ``_SERVER_FRAMEWORK_DEPS`` for JS. Deliberately
# limited to crates that RUN a server: a crate with a ``src/main.rs`` but
# NO server-framework dep is a CLI/dev-tool/bench (meilisearch's
# ``meilitool``, ``xtask``, ``openapi-generator`` each have a ``main.rs``
# but no server crate — they ride along, they do NOT become units). This
# is what keeps the Rust path from re-creating the lib-explosion: only the
# crate that depends on a server framework (meilisearch's ``crates/
# meilisearch`` depends on ``actix-web``) is promoted.
_RUST_SERVER_FRAMEWORK_DEPS: frozenset[str] = frozenset({
    "actix-web",
    "actix",
    "axum",
    "warp",
    "rocket",
    "tonic",
    "poem",
    "salvo",
    "hyper",
    "tide",
    "gotham",
})

# A SUBSET of server deps that is definitive enough to mark a service
# EVEN when the package also exposes library exports — the dependency
# only exists in a package that boots an HTTP server (``NestFactory``).
# ``@nestjs/common`` is deliberately EXCLUDED from ``_SERVER_FRAMEWORK_DEPS``
# entirely: it is the decorator/DTO library imported by types-only
# packages (cal.com ``packages/platform/types`` declares Nest DTOs but is
# a published types library, not a service). The broader set above marks
# a service only when the package is NOT a published library OR has a
# server entry point — see :class:`ServiceClassifier`.
_SERVER_RUNTIME_DEPS: frozenset[str] = frozenset({
    "@nestjs/core",
})

# Route/page directories (relative to a workspace root) that signal a
# user-facing app. The standard file-system-routing conventions across
# Next App Router / Pages, Remix, SvelteKit, Nuxt, generic SPAs.
_ROUTE_DIR_CANDIDATES: tuple[str, ...] = (
    "app",
    "pages",
    "src/app",
    "src/pages",
    "src/routes",
    "routes",
    "app/routes",
)

# Tooling-only package NAME conventions. A package whose name (or its
# last scope segment) matches one of these is tooling/config — NOT a
# product library — even when it exports a config object. These are
# universal ecosystem naming conventions (``@org/eslint-config``,
# ``tsconfig``, ``*-cli``), not repo-specific names. Matched against the
# de-scoped package name token.
#
# DELIBERATELY EXCLUDED (rule-no-repo-specific-paths): ``build-icons`` and
# ``dev-tools`` were here but are literal supabase package names, not
# industry conventions — a package called ``build-icons`` in another repo
# is just as likely a product image library. They were removed; such
# packages now classify via their STRUCTURAL signals (a ``bin`` field /
# ``bin``·``cli``·``scripts`` dir with no library exports -> tool;
# otherwise lib ride-along). Either way they are excluded from scan units,
# so the partition is unaffected. Only genuine ecosystem conventions
# remain below.
_TOOL_NAME_EXACT: frozenset[str] = frozenset({
    "tsconfig",
    "config",
    "cli",
    "eslint-config",
    "prettier-config",
    "tailwind-config",
    "codegen",
    "scripts",
})
# Tooling name SUFFIXES / tokens (matched on the de-scoped name). A name
# ENDING in one of these, or split-token-containing ``eslint``/``lint``
# config markers, is tooling.
_TOOL_NAME_SUFFIXES: tuple[str, ...] = (
    "-config",
    "-cli",
    "-eslint-config",
    "-prettier-config",
    "-tsconfig",
    "-codegen",
    "-rules",
    "-lint-rules",
    "-oxlint-rules",
    "-eslint-plugin",
)
# Tooling name PREFIXES. The shareable-config ecosystem convention is
# ``eslint-config-<name>`` / ``prettier-config-<name>`` / ``cli-<name>``
# (e.g. ``eslint-config-airbnb``, ``eslint-config-next``). A name STARTING
# with one of these is tooling. Universal ecosystem naming, not a
# repo-specific list.
_TOOL_NAME_PREFIXES: tuple[str, ...] = (
    "eslint-config-",
    "prettier-config-",
    "eslint-plugin-",
    "stylelint-config-",
    "cli-",
)
# Directory names (at workspace root) that mark a tooling/CLI package.
_TOOL_DIR_CANDIDATES: tuple[str, ...] = (
    "bin",
    "cli",
    "cmd",
    "scripts",
    "src/bin",
)

# Conventional top-level directory names for a "split-fullstack" repo that
# declares NO workspace manifest (no pnpm-workspace.yaml / package.json
# ``workspaces`` / turbo.json / Cargo ``[workspace]``) yet physically
# separates a frontend app from a backend service. ``frontend``+``backend``
# is the canonical pattern that :func:`_is_split_fullstack` gates on;
# ``client``/``server``/``web``/``api``/``app`` are the other common
# spellings of the same split. Only directories that ACTUALLY have a
# framework manifest are synthesized as units — a repo that incidentally
# has an ``api/`` folder with no manifest contributes nothing. Universal
# convention, not a repo-specific list.
_SPLIT_FULLSTACK_DIRS: tuple[str, ...] = (
    "frontend",
    "backend",
    "client",
    "server",
    "web",
    "api",
    "app",
    "ui",
    "www",
)


# ── Data types ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProjectSignals:
    """Pure structural snapshot of ONE workspace.

    Collected once per workspace and fed to every classifier. Pure
    structural facts — no judgement. Mirrors
    :class:`stage_0_6_shape.ShapeSignals` but scoped to a single
    package directory.

    All values derive from the already-parsed ``Workspace.package_json``
    + cheap filesystem checks against the workspace root. Never reads
    ``*.md`` or any prose (no-README rule).
    """

    name: str
    path: str  # repo-root-relative, e.g. "packages/twenty-front"
    stack: str | None

    # ── Path-segment signal ──────────────────────────────────────────
    is_example_path: bool
    example_segment: str | None

    # ── Dependency signals (from package.json) ───────────────────────
    has_client_framework_dep: bool
    has_server_framework_dep: bool
    has_server_runtime_dep: bool  # definitive server-boot dep (e.g. @nestjs/core)
    client_deps: tuple[str, ...]
    server_deps: tuple[str, ...]

    # ── Routing / app markers ────────────────────────────────────────
    has_route_dir: bool
    route_dir: str | None

    # ── Backend (non-JS) markers ─────────────────────────────────────
    has_fastapi_factory: bool
    has_go_cmd_main: bool
    has_go_top_level: bool
    has_server_main_ts: bool
    # Rust: a binary entry (``src/main.rs`` or a ``[[bin]]`` table) AND a
    # server-framework crate dep => a running Rust service.
    has_rust_binary_entry: bool
    has_cargo_server_dep: bool
    cargo_server_deps: tuple[str, ...]

    # ── Library (published) markers ──────────────────────────────────
    has_js_exports: bool
    has_pyproject_project: bool
    has_cargo_package: bool

    # ── Tooling markers ──────────────────────────────────────────────
    has_bin_field: bool
    has_tool_dir: bool
    has_tool_name: bool
    has_pyproject_scripts: bool

    @classmethod
    def collect(cls, repo_root: Path, ws: "Workspace") -> "ProjectSignals":
        """Build a snapshot for one workspace.

        ``repo_root`` is the absolute repo root; ``ws.path`` is joined
        onto it to locate the workspace directory. Defensive: every
        probe swallows errors and falls back to "signal absent" — a
        broken manifest must never crash classification.
        """
        ws_root = (repo_root / ws.path).resolve()

        # ── path-segment example detection ──
        seg = _matching_example_segment(ws.path)

        # ── dependency signals ──
        pkg_json = ws.package_json if ws.package_json is not None else _read_json_safe(
            ws_root / "package.json"
        )
        client_deps = _deps_matching(pkg_json, _CLIENT_FRAMEWORK_DEPS)
        server_deps = _deps_matching(pkg_json, _SERVER_FRAMEWORK_DEPS)
        runtime_deps = _deps_matching(pkg_json, _SERVER_RUNTIME_DEPS)

        # ── package.json published shape + bin ──
        has_js_exports = False
        has_bin_field = False
        if isinstance(pkg_json, dict):
            has_js_exports = bool(
                pkg_json.get("main")
                or pkg_json.get("exports")
                or pkg_json.get("module")
            )
            has_bin_field = bool(pkg_json.get("bin"))

        # ── routing dirs ──
        route_dir = _first_existing_dir(ws_root, _ROUTE_DIR_CANDIDATES)

        # ── tooling dirs ──
        tool_dir = _first_existing_dir(ws_root, _TOOL_DIR_CANDIDATES)

        # ── non-JS backend markers (reuse Stage 0.6 helpers) ──
        has_fastapi = _detect_fastapi_factory(ws_root)
        has_go_cmd_main = _has_cmd_with_main_go(ws_root)
        has_go_top_level = (ws_root / "go.mod").exists() and _has_top_level_go_files(
            ws_root
        )
        # A server entry like ``src/main.ts`` (NestJS bootstrap) or
        # ``server.ts`` at the workspace root — a backend entry point.
        has_server_main_ts = any(
            (ws_root / c).exists()
            for c in ("src/main.ts", "server.ts", "src/server.ts")
        )

        # ── python / rust library shape ──
        pyproject_text = _read_text_safe(ws_root / "pyproject.toml")
        has_pyproject_project = (
            pyproject_text is not None and "[project]" in pyproject_text
        )
        has_pyproject_scripts = _pyproject_has_scripts(pyproject_text)
        cargo_text = _read_text_safe(ws_root / "Cargo.toml")
        has_cargo_package = (
            cargo_text is not None
            and "[package]" in cargo_text
            and "[workspace]" not in cargo_text
        )
        # Rust binary-service detection: a crate is a running service when
        # it has a binary entry (``src/main.rs`` OR a Cargo ``[[bin]]``
        # table) AND depends on an HTTP/RPC server framework crate. The
        # binary entry alone is NOT promoted (a CLI/bench/dev-tool also has
        # a ``main.rs``) — the server-framework dep is the discriminator.
        has_rust_binary_entry = (ws_root / "src" / "main.rs").exists() or (
            has_cargo_package and _cargo_has_bin_table(cargo_text)
        )
        cargo_server_deps = (
            _cargo_deps_matching(cargo_text, _RUST_SERVER_FRAMEWORK_DEPS)
            if has_cargo_package
            else ()
        )

        # ── tooling name convention ──
        has_tool_name = _is_tool_name(ws.name)

        return cls(
            name=ws.name,
            path=ws.path,
            stack=ws.stack,
            is_example_path=seg is not None,
            example_segment=seg,
            has_client_framework_dep=bool(client_deps),
            has_server_framework_dep=bool(server_deps),
            has_server_runtime_dep=bool(runtime_deps),
            client_deps=client_deps,
            server_deps=server_deps,
            has_route_dir=route_dir is not None,
            route_dir=route_dir,
            has_fastapi_factory=has_fastapi,
            has_go_cmd_main=has_go_cmd_main,
            has_go_top_level=has_go_top_level,
            has_server_main_ts=has_server_main_ts,
            has_rust_binary_entry=has_rust_binary_entry,
            has_cargo_server_dep=bool(cargo_server_deps),
            cargo_server_deps=cargo_server_deps,
            has_js_exports=has_js_exports,
            has_pyproject_project=has_pyproject_project,
            has_cargo_package=has_cargo_package,
            has_bin_field=has_bin_field,
            has_tool_dir=tool_dir is not None,
            has_tool_name=has_tool_name,
            has_pyproject_scripts=has_pyproject_scripts,
        )


@dataclass(frozen=True, slots=True)
class ProjectClassification:
    """Verdict for one workspace."""

    name: str
    path: str
    project_type: str  # app | service | lib | tool | example
    confidence: float
    rationale: str
    matched_signals: tuple[str, ...] = ()


@runtime_checkable
class ProjectClassifier(Protocol):
    """Strategy interface for ONE project_type.

    Implementations are tiny: a ``project_type`` tag, a ``priority``,
    and a ``classify`` method returning a :class:`ProjectClassification`
    or ``None``.

    Contract:
      - MUST NOT raise. Failures degrade to ``None``; the dispatcher
        logs and proceeds.
      - MUST be a pure function of ``ProjectSignals``. No I/O beyond
        what ``signals`` already contains.
      - Lower ``priority`` wins (runs first). The ordering encodes the
        decision precedence example > app > service > lib > tool.
    """

    project_type: str
    priority: int

    def classify(self, signals: ProjectSignals) -> ProjectClassification | None:
        """Return a verdict if this type applies; otherwise ``None``."""


# ── Concrete classifiers ───────────────────────────────────────────────


class ExampleClassifier:
    """Sample / demo / acceptance-test code — EXCLUDED from the product
    partition. Highest priority: a package living under ``examples/`` or
    ``e2e/`` is non-product regardless of what it imports.

    Scale-invariant: fires on an industry-standard path SEGMENT
    (``examples``, ``e2e``, ``fixtures`` …), never on a repo-specific
    folder list. supabase puts 18 sample apps under ``examples/**`` (not
    even enumerated as workspaces) and an ``e2e/studio`` workspace;
    twenty has ``twenty-e2e-testing``; cal.com has ``example-apps/*`` —
    all caught by the segment rule, none by a hardcoded name.
    """

    project_type: str = "example"
    priority: int = 10

    def classify(self, signals: ProjectSignals) -> ProjectClassification | None:
        if not signals.is_example_path:
            return None
        return ProjectClassification(
            name=signals.name,
            path=signals.path,
            project_type=self.project_type,
            confidence=CONF_STRONG,
            rationale=(
                f"Path segment ``{signals.example_segment}`` marks sample/"
                "demo/acceptance-test code (industry-standard convention)."
            ),
            matched_signals=("is_example_path", f"segment:{signals.example_segment}"),
        )


class AppClassifier:
    """User-facing application: a client UI-framework dependency AND a
    file-system routes/pages directory.

    BOTH are required. A package that merely DEPENDS on react can be a
    backend that renders email templates (twenty-server has react +
    react-dom but is a NestJS service with no routes dir). Requiring a
    routes dir is what separates the SPA/Next app from the server.
    """

    project_type: str = "app"
    priority: int = 20

    def classify(self, signals: ProjectSignals) -> ProjectClassification | None:
        if not (signals.has_client_framework_dep and signals.has_route_dir):
            return None
        return ProjectClassification(
            name=signals.name,
            path=signals.path,
            project_type=self.project_type,
            confidence=CONF_STRONG,
            rationale=(
                f"Client framework dep ({', '.join(signals.client_deps)}) + "
                f"routes dir ``{signals.route_dir}`` => user-facing app."
            ),
            matched_signals=(
                "has_client_framework_dep",
                f"route_dir:{signals.route_dir}",
            ),
        )


class ServiceClassifier:
    """Backend service: a server-framework dependency, a FastAPI factory,
    a Go ``cmd/<x>/main.go``, or a server entry (``src/main.ts``) — and
    NO client routes dir (else it would have classified as ``app``).

    Because :class:`AppClassifier` runs first (lower priority number), a
    package reaching here with a server signal and no app verdict is a
    service.
    """

    project_type: str = "service"
    priority: int = 30

    def classify(self, signals: ProjectSignals) -> ProjectClassification | None:
        # DEFINITIVE server signals always trigger a service, even if the
        # package also exposes library exports: a server-RUNTIME dep
        # (``@nestjs/core`` = ``NestFactory.create``), a FastAPI factory,
        # a Go ``cmd/<x>/main.go`` binary, or a Rust crate that has a
        # binary entry AND depends on a server framework (``actix-web`` +
        # ``src/main.rs``). Each only exists in a package that boots a
        # server process.
        rust_service = signals.has_rust_binary_entry and signals.has_cargo_server_dep
        definitive = (
            signals.has_server_runtime_dep
            or signals.has_fastapi_factory
            or signals.has_go_cmd_main
            or rust_service
        )
        # A broader server-FRAMEWORK dep (express/fastify/koa/…) marks a
        # service ONLY when the package ALSO has a real server ENTRY POINT
        # — a top-level ``src/main.ts`` / ``server.ts`` / ``src/server.ts``
        # bootstrap. The framework dep on its OWN is not enough:
        #
        #   - an ADAPTER LIBRARY depends on the framework to provide an
        #     integration (trpc ``@trpc/server`` / ``@trpc/next`` depend on
        #     express/fastify and even ship a tiny ``bin`` install helper)
        #     but has NO server bootstrap — it never boots a process.
        #   - a TEST HARNESS spins the framework up only inside its test
        #     suite (trpc ``packages/tests`` pulls in ``fastify`` as a
        #     devDependency) but ships no server entry.
        #
        # The ``src/main.ts`` bootstrap is the universal "does this package
        # BOOT a server?" discriminator. It is deliberately preferred over a
        # ``not published`` / ``bin``-field test: a real backend service
        # legitimately sets ``main`` to its compiled output (infisical
        # ``backend`` declares ``"main": "./dist/main.mjs"`` AND has a
        # ``src/main.ts`` — it IS a fastify service), so a ``main``/exports
        # field does NOT imply "library", and a ``bin`` field does NOT imply
        # "server" (published libs ship ``bin`` helpers — the same trap
        # :class:`ToolClassifier` guards against). The definitive
        # Nest/FastAPI/Go/Rust signals above are unaffected — they fire even
        # for packages with exports, because those deps only exist where a
        # server boots.
        framework_service = (
            signals.has_server_framework_dep and signals.has_server_main_ts
        )
        if not (definitive or framework_service):
            return None
        # Defensive: a client routes dir would have made this an app via
        # the higher-priority AppClassifier; if we somehow reach here
        # with one, still prefer service only when there's a real server
        # signal AND no route dir (keeps the two types mutually clean).
        if signals.has_route_dir and signals.has_client_framework_dep:
            return None
        reasons: list[str] = []
        sig: list[str] = []
        if signals.has_server_framework_dep:
            reasons.append(f"server dep ({', '.join(signals.server_deps)})")
            sig.append("has_server_framework_dep")
            if framework_service and signals.has_server_main_ts:
                reasons.append("server entry (src/main.ts)")
                sig.append("has_server_main_ts")
        if signals.has_fastapi_factory:
            reasons.append("FastAPI() factory")
            sig.append("has_fastapi_factory")
        if signals.has_go_cmd_main:
            reasons.append("cmd/<x>/main.go")
            sig.append("has_go_cmd_main")
        if rust_service:
            reasons.append(
                f"Rust binary + server crate ({', '.join(signals.cargo_server_deps)})"
            )
            sig.append("has_rust_binary_entry")
            sig.append("has_cargo_server_dep")
        return ProjectClassification(
            name=signals.name,
            path=signals.path,
            project_type=self.project_type,
            confidence=CONF_STRONG,
            rationale=f"Backend: {', '.join(reasons)}; no client routes.",
            matched_signals=tuple(sig),
        )


class ToolClassifier:
    """Tooling / config / CLI package — EXCLUDED from the partition.

    Runs BEFORE :class:`LibClassifier` because a config package
    (``tailwind-config``, ``eslint-config``) commonly exports a config
    object and would otherwise look like a library. A tooling NAME
    convention or a ``bin`` field / ``bin``/``cli``/``scripts`` dir
    wins over the published-export shape.

    Scale-invariant: the name conventions (``*-config``, ``*-cli``,
    ``tsconfig``, ``*-rules``) are universal ecosystem naming, not a
    repo-specific package list.
    """

    project_type: str = "tool"
    priority: int = 40

    def classify(self, signals: ProjectSignals) -> ProjectClassification | None:
        # A PUBLISHED LIBRARY commonly ships a build ``scripts/`` dir, a
        # ``bin/`` helper, or even a codegen ``bin`` field while still
        # being a product library (twenty-ui/twenty-sdk export a public
        # API). Such bin/dir signals must therefore NOT override a
        # published-export shape — only a tooling NAME convention is
        # decisive enough to call an exporting package "tooling"
        # (``@org/eslint-config`` exports a config but is tooling).
        #
        # Discriminator vs lib:
        #   - tooling NAME convention  -> tool (even with exports)
        #   - bin field / bin·cli·scripts dir / py console-scripts, but
        #     ONLY when the package has NO product exports (a pure script
        #     bag / CLI with no importable library surface).
        published = (
            signals.has_js_exports
            or signals.has_pyproject_project
            or signals.has_cargo_package
        )
        reasons: list[str] = []
        sig: list[str] = []
        if signals.has_tool_name:
            reasons.append("tooling name convention")
            sig.append("has_tool_name")
            conf = CONF_MEDIUM
        elif not published and (
            signals.has_bin_field
            or signals.has_tool_dir
            or signals.has_pyproject_scripts
        ):
            if signals.has_bin_field:
                reasons.append("package.json bin field (no library exports)")
                sig.append("has_bin_field")
            if signals.has_tool_dir:
                reasons.append("bin/cli/scripts dir (no library exports)")
                sig.append("has_tool_dir")
            if signals.has_pyproject_scripts:
                reasons.append("[project.scripts] console entry")
                sig.append("has_pyproject_scripts")
            conf = CONF_MEDIUM if signals.has_bin_field else CONF_WEAK
        else:
            return None
        return ProjectClassification(
            name=signals.name,
            path=signals.path,
            project_type=self.project_type,
            confidence=conf,
            rationale=f"Tooling/config: {', '.join(reasons)}.",
            matched_signals=tuple(sig),
        )


class LibClassifier:
    """Shared library: a published-package shape (JS main/exports/module,
    Python ``[project]``, or Rust ``[package]``) with no app/service/tool
    signal. Product-consumable code that rides along inside the repo.
    """

    project_type: str = "lib"
    priority: int = 50

    def classify(self, signals: ProjectSignals) -> ProjectClassification | None:
        lib_signal = (
            signals.has_js_exports
            or signals.has_pyproject_project
            or signals.has_cargo_package
        )
        if not lib_signal:
            return None
        sig: list[str] = []
        if signals.has_js_exports:
            sig.append("has_js_exports")
        if signals.has_pyproject_project:
            sig.append("has_pyproject_project")
        if signals.has_cargo_package:
            sig.append("has_cargo_package")
        return ProjectClassification(
            name=signals.name,
            path=signals.path,
            project_type=self.project_type,
            confidence=CONF_MEDIUM,
            rationale="Published-library shape (main/exports/module) with no app/service.",
            matched_signals=tuple(sig),
        )


class ResidualClassifier:
    """Always-last fallback. Classifies anything no other classifier
    claimed. A package with a manifest but no published exports and no
    routes is most likely an internal helper library; default to ``lib``
    at low confidence so it RIDES ALONG rather than being dropped (we
    never want to silently exclude code that might matter). If it has a
    tooling NAME even weakly, prefer ``tool``.
    """

    project_type: str = "lib"
    priority: int = 999

    def classify(self, signals: ProjectSignals) -> ProjectClassification | None:
        if signals.has_tool_name:
            return ProjectClassification(
                name=signals.name,
                path=signals.path,
                project_type="tool",
                confidence=CONF_RESIDUAL,
                rationale="Residual: tooling name convention, no other signal.",
                matched_signals=("residual", "has_tool_name"),
            )
        return ProjectClassification(
            name=signals.name,
            path=signals.path,
            project_type="lib",
            confidence=CONF_RESIDUAL,
            rationale="Residual: no app/service/lib/tool signal; default lib (ride-along).",
            matched_signals=("residual",),
        )


# The registry — single source of truth, deterministically ordered by
# (priority, project_type). Tests inject fakes via the ``classifiers``
# argument to ``classify_project``.
_DEFAULT_CLASSIFIERS: tuple[ProjectClassifier, ...] = tuple(
    sorted(
        (
            ExampleClassifier(),
            AppClassifier(),
            ServiceClassifier(),
            ToolClassifier(),
            LibClassifier(),
            ResidualClassifier(),
        ),
        key=lambda c: (c.priority, c.project_type),
    )
)


# ── Partition plan types ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ScanUnit:
    """One independent scan unit (a subpath fed to run_pipeline_multi)."""

    subpath: str | None  # repo-root-relative dir, or None for whole-repo
    project_type: str
    name: str


@dataclass(frozen=True, slots=True)
class ExcludedProject:
    """A workspace deliberately kept out of the scan units."""

    path: str
    type: str  # project_type
    reason: str


@dataclass(frozen=True, slots=True)
class PartitionPlan:
    """The deterministic partition decision for a repo.

    ``units`` is the subpath list to feed ``run_pipeline_multi`` (or a
    single ``subpath=None`` whole-repo unit when ``is_monorepo`` is
    False). ``excluded`` records tool/example/config workspaces with the
    reason they were dropped. ``classifications`` is the full per-
    workspace verdict list for telemetry/debug.
    """

    is_monorepo: bool
    units: tuple[ScanUnit, ...]
    excluded: tuple[ExcludedProject, ...]
    classifications: tuple[ProjectClassification, ...] = ()
    rationale: str = ""
    # True when the workspaces were SYNTHESIZED by the manifest-less
    # split-fullstack rescue (no declared/discovered workspace list).
    # Consumers that must stay byte-identical for split-fullstack repos
    # (Phase B+ per-unit profile selection) skip refinement on it.
    synthesized_split: bool = False

    def subpaths(self) -> list[str]:
        """The non-None subpaths, ready for ``run_pipeline_multi``.

        A whole-repo plan (``is_monorepo`` False) returns ``[]`` — the
        caller should fall back to a single ``run_pipeline_v2`` with
        ``subpath=None`` (back-compat). Multiple subpaths route through
        ``run_pipeline_multi``.
        """
        return [u.subpath for u in self.units if u.subpath is not None]


# ── Project types that become independent scan units ───────────────────
_UNIT_TYPES: frozenset[str] = frozenset({"app", "service"})
# Project types excluded from scan units (recorded with reason).
_EXCLUDED_TYPES: frozenset[str] = frozenset({"tool", "example"})


# ── Dispatcher ─────────────────────────────────────────────────────────


def classify_project(
    repo_root: Path,
    ws: "Workspace",
    *,
    classifiers: Sequence[ProjectClassifier] | None = None,
    signals: ProjectSignals | None = None,
) -> ProjectClassification:
    """Classify ONE workspace into a ``project_type``.

    Pure function. Collects :class:`ProjectSignals` (unless supplied),
    then runs each classifier in priority order; the first non-``None``
    verdict wins. The :class:`ResidualClassifier` guarantees a result.

    Args:
        repo_root: absolute repo root.
        ws: the workspace to classify.
        classifiers: optional override of the registry (tests).
        signals: optional pre-collected signals (tests / reuse).
    """
    pipeline = classifiers if classifiers is not None else _DEFAULT_CLASSIFIERS
    snap = signals if signals is not None else ProjectSignals.collect(repo_root, ws)
    for clf in pipeline:
        try:
            verdict = clf.classify(snap)
        except Exception as exc:  # noqa: BLE001 — graceful degradation
            logger.warning(
                "project-classifier %s raised on %s (%s); skipping",
                getattr(clf, "project_type", "?"),
                ws.path,
                exc,
            )
            continue
        if verdict is not None:
            return verdict
    # Unreachable in practice (residual always returns) — defensive.
    return ProjectClassification(
        name=ws.name,
        path=ws.path,
        project_type="lib",
        confidence=CONF_RESIDUAL,
        rationale="No classifier matched (no residual registered).",
        matched_signals=("no-match",),
    )


# ── Partition planner ──────────────────────────────────────────────────


def partition_monorepo(
    ctx: "ScanContext",
    *,
    classifiers: Sequence[ProjectClassifier] | None = None,
) -> PartitionPlan:
    """Decide which workspaces become independent scan units.

    Pure deterministic function. The partition rules (all scale-
    invariant, no magic numbers, no repo-specific paths):

      - ``app`` + ``service`` workspaces become independent scan UNITS.
      - ``lib`` workspaces RIDE ALONG (scanned in place as part of the
        whole-repo tree) when ANY app/service exists in the repo; a lib
        becomes its OWN unit only when NO app/service workspace exists
        (a standalone library monorepo, e.g. a packages-only repo).
      - ``tool`` + ``example`` workspaces are EXCLUDED, recorded in
        ``excluded`` with the reason.
      - Nesting de-dup: among chosen units, any unit whose path is a
        strict ancestor-segment of another chosen unit's path is kept
        and its descendants dropped (the shallowest enumerated app/
        service is the real project; deeper enumerated workspaces under
        it ride inside its tree). This guarantees no file is scanned
        twice (e.g. cal.com ``apps/api`` vs ``apps/api/v1``).
      - NOT a monorepo (no enumerated workspaces, or fewer than
        :data:`MIN_WORKSPACES_FOR_PARTITION`) -> a single whole-repo
        unit with ``subpath=None`` (back-compat; the caller runs the
        ordinary single ``run_pipeline_v2``).

    Returns a :class:`PartitionPlan`. Never raises.
    """
    repo_root = Path(ctx.repo_path).resolve()
    workspaces = list(ctx.workspaces or [])

    # ── Manifest-less split-fullstack rescue ──
    #    A repo that declares NO workspace manager but physically splits a
    #    frontend app from a backend service (``frontend/`` + ``backend/``
    #    each with a manifest) enumerates ZERO workspaces above — it would
    #    be scanned as ONE blob. Synthesize the split dirs as workspace
    #    units so it partitions like any monorepo. Only fires when the
    #    canonical frontend/backend split is present AND ≥2 split dirs have
    #    a framework manifest; otherwise we leave ``workspaces`` untouched
    #    and fall through to the whole-repo back-compat path.
    synthesized_split = False
    if len(workspaces) < MIN_WORKSPACES_FOR_PARTITION:
        rescued = _synthesize_split_fullstack_workspaces(repo_root)
        if len(rescued) >= MIN_WORKSPACES_FOR_PARTITION:
            workspaces = rescued
            synthesized_split = True

    # ── Not-a-monorepo short-circuit (back-compat whole-repo) ──
    if len(workspaces) < MIN_WORKSPACES_FOR_PARTITION:
        reason = (
            "no enumerated workspaces"
            if not workspaces
            else f"only {len(workspaces)} workspace (< {MIN_WORKSPACES_FOR_PARTITION})"
        )
        return PartitionPlan(
            is_monorepo=False,
            units=(ScanUnit(subpath=None, project_type="repo", name=_repo_name(repo_root)),),
            excluded=(),
            classifications=(),
            rationale=f"Whole-repo scan ({reason}); no partition.",
        )

    # ── Classify every workspace ──
    classifications: list[ProjectClassification] = [
        classify_project(repo_root, ws, classifiers=classifiers) for ws in workspaces
    ]

    has_app_or_service = any(
        c.project_type in _UNIT_TYPES for c in classifications
    )

    # ── Pick units + record exclusions ──
    candidate_units: list[ProjectClassification] = []
    excluded: list[ExcludedProject] = []
    for c in classifications:
        if c.project_type in _UNIT_TYPES:
            candidate_units.append(c)
        elif c.project_type in _EXCLUDED_TYPES:
            excluded.append(
                ExcludedProject(
                    path=c.path,
                    type=c.project_type,
                    reason=(
                        "sample/demo/acceptance-test code"
                        if c.project_type == "example"
                        else "tooling/config package (no product surface)"
                    ),
                )
            )
        else:  # lib
            # Libraries NEVER become their own scan unit. When an app/
            # service exists they ride along inside its tree; when NONE
            # exists (a pile of libs with no product entry — a publishable
            # library-monorepo such as meilisearch's ``crates/*`` or
            # excalidraw's ``packages/*``) they still ride along, and the
            # degenerate guard below collapses the whole repo into a SINGLE
            # whole-repo unit. Emitting one-unit-per-lib was the inverse of
            # the cal.com 219->3 win (it produced 24 units on meilisearch,
            # 83 on lobe-chat); a library-monorepo is best scanned whole.
            excluded.append(
                ExcludedProject(
                    path=c.path,
                    type="lib",
                    reason=(
                        "shared library — rides along inside an app/service unit"
                        if has_app_or_service
                        else "library-monorepo with no app/service — rides along in whole-repo scan"
                    ),
                )
            )

    # ── Nesting de-dup: drop any unit nested under another chosen unit ──
    kept = _drop_nested(candidate_units)
    dropped_nested = [c for c in candidate_units if c not in kept]
    for c in dropped_nested:
        excluded.append(
            ExcludedProject(
                path=c.path,
                type=c.project_type,
                reason="nested under another scan unit — rides inside the ancestor's tree",
            )
        )

    units = tuple(
        ScanUnit(subpath=c.path, project_type=c.project_type, name=c.name)
        for c in sorted(kept, key=lambda x: x.path)
    )

    # ── No-app/service fallback (library-monorepo guard) ──
    #    A declared monorepo where NOTHING classified as app/service leaves
    #    ZERO candidate units (libs/tools/examples all ride along or are
    #    excluded). This is the library-monorepo case (meilisearch's
    #    ``crates/*``, excalidraw's ``packages/*``): collapse to a SINGLE
    #    whole-repo unit rather than exploding into one-unit-per-lib. Also
    #    covers the truly-degenerate "every workspace excluded" case. Never
    #    emit an empty plan.
    if not units:
        return PartitionPlan(
            is_monorepo=True,
            units=(ScanUnit(subpath=None, project_type="repo", name=_repo_name(repo_root)),),
            excluded=tuple(excluded),
            classifications=tuple(classifications),
            rationale=(
                "Monorepo with no app/service unit (library-monorepo / only "
                "ride-along libs); single whole-repo scan, no independent units."
            ),
        )

    rationale = (
        f"{'Split-fullstack ' if synthesized_split else ''}Monorepo: "
        f"{len(units)} scan unit(s) "
        f"({', '.join(sorted({u.project_type for u in units}))}); "
        f"{len(excluded)} excluded/ride-along."
        + (
            " Workspaces synthesized from frontend/backend split (no workspace manifest)."
            if synthesized_split
            else ""
        )
    )
    return PartitionPlan(
        is_monorepo=True,
        units=units,
        excluded=tuple(excluded),
        classifications=tuple(classifications),
        rationale=rationale,
        synthesized_split=synthesized_split,
    )


# ── Helpers ────────────────────────────────────────────────────────────


def _matching_example_segment(path: str) -> str | None:
    """Return the first path SEGMENT that is an example/demo marker.

    Splits on ``/`` and compares each segment (lower-cased) against
    :data:`_EXAMPLE_PATH_SEGMENTS`. Whole-segment match only — a dir
    named ``my-examples`` does NOT match the segment ``examples``.
    """
    for seg in path.split("/"):
        if seg.lower() in _EXAMPLE_PATH_SEGMENTS:
            return seg
    return None


def _deps_matching(
    pkg_json: object,
    vocab: frozenset[str],
) -> tuple[str, ...]:
    """Return the sorted subset of ``vocab`` present in package.json deps.

    Reads ``dependencies`` + ``devDependencies`` + ``peerDependencies``.
    Safe on missing / malformed manifests.
    """
    if not isinstance(pkg_json, dict):
        return ()
    keys: set[str] = set()
    for field_name in ("dependencies", "devDependencies", "peerDependencies"):
        section = pkg_json.get(field_name)
        if isinstance(section, dict):
            keys.update(k for k in section.keys() if isinstance(k, str))
    return tuple(sorted(keys & vocab))


def _cargo_deps_matching(cargo_text: str | None, vocab: frozenset[str]) -> tuple[str, ...]:
    """Return the sorted subset of ``vocab`` present in a Cargo manifest's deps.

    Parses ``[dependencies]`` + ``[dev-dependencies]`` + ``[build-dependencies]``
    with :mod:`tomllib` (NOT regex). Safe on missing / malformed manifests
    (returns ``()``). Crate keys are matched as whole names — the same
    class of structural signal as the JS ``package.json`` dependency keys.
    """
    if cargo_text is None or "[package]" not in cargo_text:
        return ()
    try:
        data = tomllib.loads(cargo_text)
    except (tomllib.TOMLDecodeError, ValueError):
        return ()
    keys: set[str] = set()
    for section_name in ("dependencies", "dev-dependencies", "build-dependencies"):
        section = data.get(section_name)
        if isinstance(section, dict):
            keys.update(k for k in section.keys() if isinstance(k, str))
    return tuple(sorted(keys & vocab))


def _cargo_has_bin_table(cargo_text: str | None) -> bool:
    """True when the Cargo manifest declares an explicit ``[[bin]]`` target.

    A ``[[bin]]`` array-of-tables names a binary the crate produces — an
    explicit, author-declared binary entry independent of the conventional
    ``src/main.rs``. Parsed with :mod:`tomllib`; ``()`` on parse failure.
    """
    if cargo_text is None or "[[bin]]" not in cargo_text:
        return False
    try:
        data = tomllib.loads(cargo_text)
    except (tomllib.TOMLDecodeError, ValueError):
        return False
    bins = data.get("bin")
    return isinstance(bins, list) and len(bins) > 0


def _first_existing_dir(root: Path, candidates: tuple[str, ...]) -> str | None:
    """Return the first candidate sub-path that is a directory under root."""
    for c in candidates:
        try:
            if (root / c).is_dir():
                return c
        except OSError:
            continue
    return None


def _is_tool_name(name: str | None) -> bool:
    """True when the de-scoped package NAME matches a tooling convention.

    De-scopes ``@org/eslint-config`` -> ``eslint-config`` then checks
    exact-match and suffix vocabularies. Universal ecosystem naming, not
    a repo-specific list.
    """
    if not name:
        return False
    token = name.split("/")[-1].lower()
    if token in _TOOL_NAME_EXACT:
        return True
    if any(token.endswith(suf) for suf in _TOOL_NAME_SUFFIXES):
        return True
    if any(token.startswith(pre) for pre in _TOOL_NAME_PREFIXES):
        return True
    # Split-token check for lint/config markers embedded in a name like
    # ``twenty-oxlint-rules`` (already covered by ``-rules``) or a name
    # whose tokens include ``eslint``/``prettier`` config markers.
    parts = set(token.replace("_", "-").split("-"))
    if "tsconfig" in parts:
        return True
    return False


def _drop_nested(
    units: list[ProjectClassification],
) -> list[ProjectClassification]:
    """Drop any unit whose path is a strict ancestor-segment of another.

    Keeps the SHALLOWEST enumerated app/service when workspaces nest
    (e.g. cal.com ``apps/api`` is kept; ``apps/api/v1`` + ``apps/api/v2``
    are dropped — they ride inside ``apps/api``'s tree). Segment-aware:
    ``apps/api`` is an ancestor of ``apps/api/v1`` but NOT of
    ``apps/api-v2`` (different sibling).
    """
    kept: list[ProjectClassification] = []
    # Sort by path depth (shallow first) so ancestors are seen first.
    for cand in sorted(units, key=lambda c: (c.path.count("/"), c.path)):
        cand_segs = cand.path.split("/")
        nested = False
        for k in kept:
            k_segs = k.path.split("/")
            if (
                len(k_segs) < len(cand_segs)
                and cand_segs[: len(k_segs)] == k_segs
            ):
                nested = True
                break
        if not nested:
            kept.append(cand)
    return kept


def _synthesize_split_fullstack_workspaces(
    repo_root: Path,
) -> list["Workspace"]:
    """Synthesize workspace units for a manifest-less split-fullstack repo.

    Some repos physically separate a frontend app from a backend service
    (``frontend/`` + ``backend/`` each with a ``package.json``) but declare
    NO workspace manager, so :func:`detect_workspace` enumerates nothing
    and the repo would otherwise be scanned as ONE blob — the exact disease
    this module cures.

    Gated on :func:`stage_0_6_shape._is_split_fullstack` (both ``frontend``
    AND ``backend`` present with a framework manifest). When it fires, we
    enumerate every conventional split dir (:data:`_SPLIT_FULLSTACK_DIRS`)
    that actually has a framework manifest and synthesize a
    :class:`Workspace` for it (parsing its ``package.json`` so the
    classifier sees its deps). Returns ``[]`` when the repo is not a
    split-fullstack layout, so the caller cleanly falls back to whole-repo.

    Pure + defensive: a missing/broken manifest yields ``package_json=None``
    (the classifier then leans on folder signals); never raises.
    """
    if not _is_split_fullstack(repo_root):
        return []
    from faultline.pipeline_v2.stage_0_intake import Workspace

    synthesized: list[Workspace] = []
    for dir_name in _SPLIT_FULLSTACK_DIRS:
        d = repo_root / dir_name
        try:
            if not d.is_dir() or not _has_framework_manifest(d):
                continue
        except OSError:
            continue
        pkg = _read_json_safe(d / "package.json")
        synthesized.append(
            Workspace(
                name=dir_name,
                path=dir_name,
                package_json=pkg if isinstance(pkg, dict) else None,
                stack=None,
                files=[],
            )
        )
    return synthesized


def _repo_name(repo_root: Path) -> str:
    """Best-effort repo name = root dir name (no manifest parsing needed)."""
    return repo_root.name


def _repo_slug_root(ctx: "ScanContext") -> Path:
    return Path(ctx.repo_path).resolve()


def write_partition_artifact(
    ctx: "ScanContext",
    plan: PartitionPlan,
) -> None:
    """Write ``06b-stage-partition.json`` when ``ctx.run_dir`` is set.

    Mirrors :func:`stage_0_6_shape._maybe_write_artifact`. No-op in CLI
    mode (run_dir None) so ad-hoc classification never touches
    ``~/.faultline``.
    """
    run_dir = getattr(ctx, "run_dir", None)
    if run_dir is None:
        return
    try:
        from faultline.pipeline_v2.stage_7_output import write_stage_artifact
    except ImportError:
        return
    payload = {
        "stage": "0.6b-project-partition",
        "run_id": getattr(ctx, "run_id", None),
        "is_monorepo": plan.is_monorepo,
        "rationale": plan.rationale,
        "units": [asdict(u) for u in plan.units],
        "excluded": [asdict(e) for e in plan.excluded],
        "classifications": [asdict(c) for c in plan.classifications],
        "min_workspaces_for_partition": MIN_WORKSPACES_FOR_PARTITION,
    }
    try:
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="partition",
            payload=payload,
            run_dir=run_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage_0_6_project_classifier: failed to write artifact: %s", exc)


__all__ = [
    "MIN_WORKSPACES_FOR_PARTITION",
    "CONF_STRONG",
    "CONF_MEDIUM",
    "CONF_WEAK",
    "CONF_RESIDUAL",
    "ProjectSignals",
    "ProjectClassification",
    "ProjectClassifier",
    "ExampleClassifier",
    "AppClassifier",
    "ServiceClassifier",
    "ToolClassifier",
    "LibClassifier",
    "ResidualClassifier",
    "ScanUnit",
    "ExcludedProject",
    "PartitionPlan",
    "classify_project",
    "partition_monorepo",
    "write_partition_artifact",
]
