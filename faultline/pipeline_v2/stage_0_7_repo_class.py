"""Stage 0.7 — Repo-Class exit gate (StackProfile spec, Phase C).

Deterministic (no LLM) classification of the SCAN UNIT into a
``repo_class`` verdict::

    product-app | library | cli-tool | infra-daemon | framework

Distinct from its two siblings:

  - Stage 0.6  (``stage_0_6_shape``)   — HOW the repo is laid out
    (architecture shape for the flow-rollup dispatcher).
  - Stage 0.6b (``stage_0_6_project_classifier``) — WHICH workspaces
    of a monorepo are scan-worthy projects (partition plan).
  - Stage 0.7  (this module)           — WHAT KIND OF THING the scan
    unit is as a product funnel decision: does it have USERS with
    JOURNEYS (a product app), or consumers of an API surface /
    command grammar (library, CLI, infra daemon, framework)?

Why (operator decision 2026-07-03, stack-profile-architecture spec
Phase C): libraries, CLI tools and infra daemons must NOT get
hallucinated "user flows" — they are a parked funnel with a future
LLM approach of their own. What ships NOW is a deterministic verdict
plus UF-synthesis suppression for CONFIDENT non-product classes, so
they exit the product funnel cleanly. The developer-feature/flow
skeleton is still produced — only the product-grain ``user_flows[]``
projection is suppressed.

Design tenets (mirrors the 0.6 family)
======================================

  - Pure function. ``classify_repo_class(ctx)`` is idempotent: same
    inputs -> same output. Safe without ``ctx.run_dir``.
  - Composition over inheritance. Every class is a tiny standalone
    Strategy implementing :class:`RepoClassifier`; the registry is the
    single source of truth; tests inject fakes.
  - FAIL-OPEN. Ambiguity resolves to ``product-app`` (the residual),
    and suppression additionally requires confidence >=
    :data:`SUPPRESS_MIN_CONFIDENCE`. A product app must never lose its
    user flows to a mis-fire; a library occasionally keeping fake
    journeys is the lesser bug (status quo today).
  - Universal, scale-invariant signals only. Dependency/name
    vocabularies are INDUSTRY-STANDARD ecosystem conventions (the same
    class of signal Stage 0.6b already trusts), never per-repo lists.
    No magic thresholds tuned on one repo.
  - No README parsing (``CLAUDE.md`` hard rule) — manifests + folder
    presence + entry-point headers only.
  - Reuse, don't rebuild: signal probes are imported from
    ``stage_0_6_shape`` / ``stage_0_6_project_classifier``; the
    monorepo product test reuses the 0.6b partition classifiers.

Suppression gate
================

``should_suppress_user_flows(verdict)`` is the ONE seam the finalize
phase consults. It fires only when

  - the verdict is a NON-product class, AND
  - ``verdict.confidence >= SUPPRESS_MIN_CONFIDENCE``, AND
  - the kill-switch env ``FAULTLINE_REPO_CLASS_GATE`` is not ``0``.

The kill-switch disables SUPPRESSION only — the ``repo_class`` verdict
itself is always computed and emitted (observability is free and
additive).
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Sequence, runtime_checkable

# Reuse the deterministic manifest/folder probes of the 0.6 family —
# DO NOT re-implement (single source of truth for each signal).
from faultline.pipeline_v2.stage_0_6_shape import (
    ShapeSignals,
    _read_json_safe,
    _read_text_safe,
)
from faultline.pipeline_v2.stage_0_6_project_classifier import (
    _RUST_SERVER_FRAMEWORK_DEPS,
    _UNIT_TYPES,
    classify_project,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# ── Repo classes ────────────────────────────────────────────────────────

REPO_CLASS_PRODUCT_APP = "product-app"
REPO_CLASS_LIBRARY = "library"
REPO_CLASS_CLI_TOOL = "cli-tool"
REPO_CLASS_INFRA_DAEMON = "infra-daemon"
REPO_CLASS_FRAMEWORK = "framework"

#: Every class whose confident detection exits the product funnel.
NON_PRODUCT_CLASSES: frozenset[str] = frozenset({
    REPO_CLASS_LIBRARY,
    REPO_CLASS_CLI_TOOL,
    REPO_CLASS_INFRA_DAEMON,
    REPO_CLASS_FRAMEWORK,
})

# ── Universal confidence grades (mirrors the 0.6b constants) ───────────
CONF_STRONG: float = 0.95
CONF_HIGH: float = 0.90
CONF_MEDIUM: float = 0.85
CONF_WEAK: float = 0.70
CONF_RESIDUAL: float = 0.40

#: UF suppression requires at least this much evidence. ``0.80``
#: reflects "strong structural evidence" — every rule below that emits
#: a non-product verdict at >= 0.85 fired on an unambiguous manifest /
#: entry-point signal; anything graded below (name-only conventions,
#: packages-only heuristics) stays visible as a verdict but does NOT
#: suppress. Scale-invariant cutoff, not per-repo tuning.
SUPPRESS_MIN_CONFIDENCE: float = 0.80

#: Kill-switch for the SUPPRESSION seam (verdict is always emitted).
#: Default ON — suppression only ever fires on confident non-product
#: verdicts, and the product-app corpus is byte-unchanged (snapshot
#: gate) except for the new fields.
GATE_ENV = "FAULTLINE_REPO_CLASS_GATE"


def gate_enabled() -> bool:
    """True unless ``FAULTLINE_REPO_CLASS_GATE=0`` (default ON)."""
    return os.environ.get(GATE_ENV, "1").strip() != "0"


# ── Framework-name vocabularies (self-repo detection) ───────────────────
#
# The "framework self-repo" signal: the scan unit's OWN published name
# equals a well-known web/app framework's package name — the repo IS
# the framework, not an app built on it (litestar, fastapi, chi, gin,
# express clones...). These are the canonical published names of the
# major frameworks per ecosystem — universal ecosystem knowledge, the
# exact same class of vocabulary as ``_SERVER_FRAMEWORK_DEPS`` /
# ``_RUST_SERVER_FRAMEWORK_DEPS`` in Stage 0.6b. Matched EXACTLY
# against the de-scoped/de-versioned self name, never as a substring —
# a product app named ``dispatch`` or a plugin named ``flask-admin``
# can not fire.

_PY_FRAMEWORK_NAMES: frozenset[str] = frozenset({
    "django", "fastapi", "litestar", "flask", "starlette", "sanic",
    "tornado", "aiohttp", "pyramid", "bottle", "falcon", "quart",
})

_JS_FRAMEWORK_NAMES: frozenset[str] = frozenset({
    "next", "react", "vue", "svelte", "nuxt", "astro", "remix",
    "express", "fastify", "koa", "hono", "nestjs", "solid-js",
    "preact", "angular", "ember.js", "gatsby",
})

_GO_FRAMEWORK_NAMES: frozenset[str] = frozenset({
    "chi", "gin", "echo", "fiber", "iris", "beego", "gorilla",
})

_RUST_FRAMEWORK_NAMES: frozenset[str] = _RUST_SERVER_FRAMEWORK_DEPS

# ── Python server-framework dependency vocabulary ────────────────────────
#
# A pyproject ``[project]`` package that DEPENDS on a server framework
# at runtime is a deployable service/app (dispatch, weblate, saleor),
# not a published library — libraries do not pull a whole web framework
# into their runtime deps. Same names as the framework vocabulary: the
# roles differ (self-name = framework repo; dependency = app built on
# it), the vocabulary is one.
_PY_SERVER_FRAMEWORK_DEPS: frozenset[str] = _PY_FRAMEWORK_NAMES

# Root directory names that mark a LONG-RUNNING server process in a
# binary repo (daemon vs one-shot CLI discrimination). Standard Go/Rust
# project conventions (ollama ``server/``, traefik ``pkg/server`` +
# ``pkg/proxy``) — the same segment-convention signal class as
# Stage 0.6's ``has_go_server_dir``.
_DAEMON_DIR_SEGMENTS: tuple[str, ...] = (
    "server",
    "proxy",
    "daemon",
    "internal/server",
    "pkg/server",
    "pkg/proxy",
)

_RE_PACKAGE_MAIN = re.compile(r"^\s*package\s+main\b", re.MULTILINE)

# Distribution-name prefix of a PEP 508 requirement string
# (``Django>=5.0`` -> ``Django``; ``uvicorn[standard]`` -> ``uvicorn``).
_RE_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


# ── Signals ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RepoClassSignals:
    """Pure structural snapshot for the repo-class decision.

    Wraps the Stage 0.6 :class:`ShapeSignals` snapshot (reused, not
    re-probed) plus the handful of repo-class-specific facts the shape
    classifier does not collect. No judgement, no I/O beyond cheap
    manifest/folder reads. Never reads prose files.
    """

    shape: ShapeSignals

    # The scan unit's OWN published name per ecosystem (empty when the
    # manifest is absent/unparseable). De-scoped (``@org/x`` -> ``x``)
    # and de-versioned (go module ``.../chi/v5`` -> ``chi``).
    self_name_js: str
    self_name_py: str
    self_name_go: str
    self_name_rust: str

    # Python: a server framework in ``[project].dependencies``.
    py_server_framework_deps: tuple[str, ...]

    # Go: any ``cmd/<x>/*.go`` declaring ``package main`` (broader than
    # Stage 0.6's ``cmd/<x>/main.go`` — traefik's entry is
    # ``cmd/traefik/traefik.go``), or a root ``main.go``.
    has_go_binary_entry: bool

    # Rust: root crate binary entry + server-framework crate dep.
    has_rust_binary_entry: bool
    rust_server_deps: tuple[str, ...]

    # Long-running-process marker for binary repos (daemon dirs).
    has_daemon_dir: bool
    daemon_dir: str | None

    # Monorepo: any 0.6b workspace classified app/service.
    has_product_workspace: bool
    product_workspace_sample: tuple[str, ...]

    @classmethod
    def collect(cls, ctx: "ScanContext") -> "RepoClassSignals":
        """Build the snapshot. Defensive: every probe degrades to
        "signal absent"; a broken manifest never crashes the scan."""
        root = Path(ctx.repo_path)
        shape = ShapeSignals.collect(ctx)

        pkg_json = _read_json_safe(root / "package.json")
        self_js = ""
        if isinstance(pkg_json, dict) and isinstance(pkg_json.get("name"), str):
            self_js = pkg_json["name"].split("/")[-1].lower()

        pyproject_text = _read_text_safe(root / "pyproject.toml")
        self_py = ""
        py_deps: tuple[str, ...] = ()
        if pyproject_text is not None:
            self_py, py_deps = _pyproject_name_and_server_deps(pyproject_text)

        self_go = _go_module_self_name(_read_text_safe(root / "go.mod"))

        cargo_text = _read_text_safe(root / "Cargo.toml")
        self_rust = ""
        rust_server: tuple[str, ...] = ()
        has_rust_bin = False
        if cargo_text is not None:
            self_rust, rust_server, has_rust_bin = _cargo_self_signals(
                cargo_text, root,
            )

        has_go_bin = shape.has_go_mod and (
            (root / "main.go").exists() or _cmd_has_package_main(root)
        )

        daemon_dir = _first_daemon_dir(root)

        has_product_ws, ws_sample = _monorepo_has_product_workspace(ctx)

        return cls(
            shape=shape,
            self_name_js=self_js,
            self_name_py=self_py,
            self_name_go=self_go,
            self_name_rust=self_rust,
            py_server_framework_deps=py_deps,
            has_go_binary_entry=has_go_bin,
            has_rust_binary_entry=has_rust_bin,
            rust_server_deps=rust_server,
            has_daemon_dir=daemon_dir is not None,
            daemon_dir=daemon_dir,
            has_product_workspace=has_product_ws,
            product_workspace_sample=ws_sample,
        )


@dataclass(frozen=True, slots=True)
class RepoClassVerdict:
    """The Stage 0.7 verdict for one scan unit."""

    repo_class: str
    confidence: float
    rationale: str
    matched_signals: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["matched_signals"] = list(self.matched_signals)
        return d


@runtime_checkable
class RepoClassifier(Protocol):
    """Strategy interface for one repo class.

    Contract (same as the 0.6 family): MUST NOT raise (failures degrade
    to ``None``; the dispatcher logs and proceeds); MUST be a pure
    function of :class:`RepoClassSignals`; lower ``priority`` runs
    first.
    """

    repo_class: str
    priority: int

    def classify(self, signals: RepoClassSignals) -> RepoClassVerdict | None:
        """Return a verdict if this class applies; otherwise ``None``."""


# ── Concrete classifiers ─────────────────────────────────────────────────


class FrameworkSelfClassifier:
    """The repo IS a framework: its own published name equals a
    canonical framework package name (litestar, fastapi, chi, express
    clones...). Runs FIRST — a framework repo commonly also looks like
    a library ([project] exports) and its docs/test apps can even
    look route-shaped, so the self-name signal must win.
    """

    repo_class: str = REPO_CLASS_FRAMEWORK
    priority: int = 10

    def classify(self, signals: RepoClassSignals) -> RepoClassVerdict | None:
        hits: list[tuple[str, str]] = []
        if signals.self_name_py and signals.self_name_py in _PY_FRAMEWORK_NAMES:
            hits.append(("py", signals.self_name_py))
        if signals.self_name_js and signals.self_name_js in _JS_FRAMEWORK_NAMES:
            hits.append(("js", signals.self_name_js))
        if signals.self_name_go and signals.self_name_go in _GO_FRAMEWORK_NAMES:
            hits.append(("go", signals.self_name_go))
        if signals.self_name_rust and signals.self_name_rust in _RUST_FRAMEWORK_NAMES:
            hits.append(("rust", signals.self_name_rust))
        if not hits:
            return None
        eco, name = hits[0]
        return RepoClassVerdict(
            repo_class=self.repo_class,
            confidence=CONF_HIGH,
            rationale=(
                f"Self-published name ``{name}`` ({eco}) is a canonical "
                "framework package name — the repo IS the framework, not "
                "an app built on it."
            ),
            matched_signals=tuple(f"self_name:{e}:{n}" for e, n in hits),
        )


class ProductAppClassifier:
    """Positive product-app evidence — runs BEFORE every non-product
    classifier so a routed/serviced repo can never be demoted by an
    incidental library/binary shape (fail-open by ordering).

    Fires on: file-system routes (Next/Remix/Rails/Django/Laravel),
    a FastAPI factory, a split-fullstack layout, a monorepo with an
    app/service workspace (0.6b verdicts reused), or a Python package
    whose RUNTIME deps include a server framework (dispatch, weblate —
    a published library does not pull Django/FastAPI into its runtime).
    """

    repo_class: str = REPO_CLASS_PRODUCT_APP
    priority: int = 20

    def classify(self, signals: RepoClassSignals) -> RepoClassVerdict | None:
        s = signals.shape
        reasons: list[str] = []
        sig: list[str] = []
        if s.has_app_router_dir or s.has_pages_router_dir or s.has_remix_routes_dir:
            reasons.append("file-system page routes")
            sig.append("has_page_routes")
        if s.has_rails_app_dir or s.has_django_manage_py or s.has_laravel_controllers_dir:
            reasons.append("MVC backend app layout")
            sig.append("has_mvc_app_layout")
        if s.has_fastapi_app_factory:
            reasons.append("FastAPI() factory")
            sig.append("has_fastapi_app_factory")
        if signals.py_server_framework_deps:
            reasons.append(
                "server-framework runtime dep "
                f"({', '.join(signals.py_server_framework_deps)})"
            )
            sig.append("py_server_framework_deps")
        if signals.has_product_workspace:
            reasons.append(
                "monorepo app/service workspace "
                f"({', '.join(signals.product_workspace_sample)})"
            )
            sig.append("has_product_workspace")
        if s.has_split_fullstack_frontend_backend:
            reasons.append("split-fullstack frontend/+backend/ layout")
            sig.append("has_split_fullstack_frontend_backend")
        if not reasons:
            return None
        return RepoClassVerdict(
            repo_class=self.repo_class,
            confidence=CONF_STRONG,
            rationale=f"Product app: {'; '.join(reasons)}.",
            matched_signals=tuple(sig),
        )


class InfraDaemonClassifier:
    """Long-running infra server distributed as a binary: a Go/Rust
    binary entry point PLUS a long-running-process marker (a
    ``server``/``proxy``/``daemon`` source dir, or — for Rust — a
    server-framework crate dep, which only exists where a server
    boots). traefik, ollama, qdrant are the canonical shapes.
    """

    repo_class: str = REPO_CLASS_INFRA_DAEMON
    priority: int = 30

    def classify(self, signals: RepoClassSignals) -> RepoClassVerdict | None:
        go_daemon = signals.has_go_binary_entry and signals.has_daemon_dir
        rust_daemon = signals.has_rust_binary_entry and bool(
            signals.rust_server_deps
        )
        if not (go_daemon or rust_daemon):
            return None
        reasons: list[str] = []
        sig: list[str] = []
        if go_daemon:
            reasons.append(
                f"Go binary entry + ``{signals.daemon_dir}`` dir"
            )
            sig.extend(("has_go_binary_entry", f"daemon_dir:{signals.daemon_dir}"))
        if rust_daemon:
            reasons.append(
                "Rust binary entry + server crate "
                f"({', '.join(signals.rust_server_deps)})"
            )
            sig.extend(("has_rust_binary_entry", "rust_server_deps"))
        return RepoClassVerdict(
            repo_class=self.repo_class,
            confidence=CONF_HIGH,
            rationale=f"Infra daemon: {'; '.join(reasons)}.",
            matched_signals=tuple(sig),
        )


class CliToolClassifier:
    """One-shot command binary: a binary entry point WITHOUT the
    long-running markers the daemon classifier requires. Also the
    JS-bin-only and Python-scripts-only shapes Stage 0.6 recognises.
    """

    repo_class: str = REPO_CLASS_CLI_TOOL
    priority: int = 40

    def classify(self, signals: RepoClassSignals) -> RepoClassVerdict | None:
        s = signals.shape
        go_cli = signals.has_go_binary_entry
        rust_cli = signals.has_rust_binary_entry and not signals.rust_server_deps
        js_cli = (
            s.has_package_json
            and s.has_bin_dir
            and s.package_json_no_app_entry
            and not s.package_json_main_or_exports
        )
        py_cli = (
            s.has_cli_py_entry
            and s.has_pyproject
            and not s.pyproject_has_project_section
        )
        if not (go_cli or rust_cli or js_cli or py_cli):
            return None
        sig: list[str] = []
        if go_cli:
            sig.append("has_go_binary_entry")
        if rust_cli:
            sig.append("has_rust_binary_entry")
        if js_cli:
            sig.append("js_bin_only")
        if py_cli:
            sig.append("py_scripts_only")
        return RepoClassVerdict(
            repo_class=self.repo_class,
            confidence=CONF_MEDIUM,
            rationale=(
                "CLI tool: binary/script entry point with no long-running "
                "server marker and no app routes."
            ),
            matched_signals=tuple(sig),
        )


class LibraryClassifier:
    """Published library: a public-API manifest shape (JS
    main/exports, Python ``[project]``, Rust lib crate, Go top-level
    package files) with no app entry, no server-framework runtime dep,
    and no binary entry point.
    """

    repo_class: str = REPO_CLASS_LIBRARY
    priority: int = 50

    def classify(self, signals: RepoClassSignals) -> RepoClassVerdict | None:
        s = signals.shape
        js_lib = (
            s.has_package_json
            and s.package_json_main_or_exports
            and s.package_json_no_app_entry
        )
        py_lib = (
            s.has_pyproject
            and s.pyproject_has_project_section
            and not signals.py_server_framework_deps
            and not s.has_fastapi_app_factory
            and not s.has_django_manage_py
        )
        go_lib = (
            s.has_go_mod
            and s.has_go_top_level_files
            and not signals.has_go_binary_entry
        )
        rust_lib = (
            s.has_cargo_toml
            and s.cargo_is_single_crate
            and not signals.has_rust_binary_entry
        )
        if not (js_lib or py_lib or go_lib or rust_lib):
            return None
        sig: list[str] = []
        if js_lib:
            sig.append("js_main_or_exports")
        if py_lib:
            sig.append("pyproject_project_no_server_dep")
        if go_lib:
            sig.append("go_top_level_package")
        if rust_lib:
            sig.append("rust_lib_crate")
        return RepoClassVerdict(
            repo_class=self.repo_class,
            confidence=CONF_MEDIUM,
            rationale=(
                "Published library: public-API manifest shape with no app "
                "entry, no server-framework runtime dep, no binary entry."
            ),
            matched_signals=tuple(sig),
        )


class ResidualProductAppClassifier:
    """Always-last FAIL-OPEN fallback: anything without a confident
    signal is treated as a product app (suppression never fires — a
    residual verdict is far below :data:`SUPPRESS_MIN_CONFIDENCE`).
    """

    repo_class: str = REPO_CLASS_PRODUCT_APP
    priority: int = 999

    def classify(self, signals: RepoClassSignals) -> RepoClassVerdict:
        return RepoClassVerdict(
            repo_class=self.repo_class,
            confidence=CONF_RESIDUAL,
            rationale=(
                "Residual: no confident product/library/binary signal; "
                "fail-open to product-app (UF suppression never fires)."
            ),
            matched_signals=("residual",),
        )


_DEFAULT_CLASSIFIERS: tuple[RepoClassifier, ...] = tuple(
    sorted(
        (
            FrameworkSelfClassifier(),
            ProductAppClassifier(),
            InfraDaemonClassifier(),
            CliToolClassifier(),
            LibraryClassifier(),
            ResidualProductAppClassifier(),
        ),
        key=lambda c: (c.priority, c.repo_class),
    )
)


# ── Dispatcher ───────────────────────────────────────────────────────────


def classify_repo_class(
    ctx: "ScanContext",
    *,
    classifiers: Sequence[RepoClassifier] | None = None,
    signals: RepoClassSignals | None = None,
) -> RepoClassVerdict:
    """Classify the scan unit into a ``repo_class`` verdict.

    Pure function: collects :class:`RepoClassSignals` (unless supplied)
    and runs the strategies in priority order; the first non-``None``
    verdict wins. The residual guarantees a result (fail-open
    product-app). No LLM, no network; never raises.
    """
    pipeline = classifiers if classifiers is not None else _DEFAULT_CLASSIFIERS
    try:
        snap = signals if signals is not None else RepoClassSignals.collect(ctx)
    except Exception as exc:  # noqa: BLE001 — classification must never fail a scan
        logger.warning("stage_0_7: signal collection failed (%s); fail-open", exc)
        return ResidualProductAppClassifier().classify(None)  # type: ignore[arg-type]
    for clf in pipeline:
        try:
            verdict = clf.classify(snap)
        except Exception as exc:  # noqa: BLE001 — graceful degradation
            logger.warning(
                "repo-classifier %s raised (%s); skipping",
                getattr(clf, "repo_class", "?"), exc,
            )
            continue
        if verdict is not None:
            return verdict
    return ResidualProductAppClassifier().classify(snap)


def classify_repo_class_per_unit(
    ctx: "ScanContext",
    *,
    classifiers: Sequence[RepoClassifier] | None = None,
) -> RepoClassVerdict:
    """Whole-repo verdict refined by the Stage 0.6b partition (Phase B+).

    Where the partition provides scan units, ``repo_class`` evaluates
    PER UNIT: a hybrid repo whose whole-repo signals read non-product
    (a backend that looks daemon/library-shaped next to a nested
    frontend) is still a product app when ANY unit is CONFIDENTLY a
    product app. Rules:

      * whole-repo verdict already ``product-app`` → returned as-is
        (nothing to refine; the common case, zero behaviour change);
      * no partition units (single-package repo, library monorepo) →
        whole-repo verdict as-is (the G4 path);
      * a unit verdict overrides ONLY when it is ``product-app`` at
        ``confidence >= SUPPRESS_MIN_CONFIDENCE`` — the same bar UF
        suppression itself requires. The fail-open residual verdict
        (``CONF_RESIDUAL``) can therefore never flip a confident
        non-product whole-repo verdict.

    Pure, deterministic (units sorted by subpath; first confident
    product-app unit wins), never raises.
    """
    verdict = classify_repo_class(ctx, classifiers=classifiers)
    if verdict.repo_class == REPO_CLASS_PRODUCT_APP:
        return verdict

    try:
        from faultline.pipeline_v2.unit_scope import (
            scan_unit_subpaths,
            unit_scoped_ctx,
        )

        subpaths = scan_unit_subpaths(ctx)
    except Exception as exc:  # noqa: BLE001 — refinement must never fail a scan
        logger.warning("stage_0_7 per-unit: unit derivation failed (%s)", exc)
        return verdict
    if not subpaths:
        return verdict

    for subpath in subpaths:
        try:
            scoped = unit_scoped_ctx(ctx, subpath)
            if not scoped.tracked_files:
                continue
            unit_verdict = classify_repo_class(scoped, classifiers=classifiers)
        except Exception as exc:  # noqa: BLE001 — graceful degradation
            logger.warning(
                "stage_0_7 per-unit: unit %s classification failed (%s)",
                subpath, exc,
            )
            continue
        if (
            unit_verdict.repo_class == REPO_CLASS_PRODUCT_APP
            and unit_verdict.confidence >= SUPPRESS_MIN_CONFIDENCE
        ):
            return RepoClassVerdict(
                repo_class=REPO_CLASS_PRODUCT_APP,
                confidence=unit_verdict.confidence,
                rationale=(
                    f"Per-unit refinement: scan unit ``{subpath}`` is a "
                    f"confident product app ({unit_verdict.rationale}) — "
                    "the repo ships a product even though its whole-repo "
                    f"signals read ``{verdict.repo_class}``."
                ),
                matched_signals=(
                    "per-unit",
                    f"unit:{subpath}",
                    *unit_verdict.matched_signals,
                ),
            )
    return verdict


def should_suppress_user_flows(verdict: RepoClassVerdict | None) -> bool:
    """The ONE suppression seam the finalize phase consults.

    True only for a CONFIDENT non-product verdict with the gate env ON.
    ``None`` (verdict unavailable) never suppresses — fail-open.
    """
    if verdict is None:
        return False
    if not gate_enabled():
        return False
    return (
        verdict.repo_class in NON_PRODUCT_CLASSES
        and verdict.confidence >= SUPPRESS_MIN_CONFIDENCE
    )


def suppression_reason(verdict: RepoClassVerdict) -> str:
    """The ``scan_meta.uf_suppressed_reason`` marker string."""
    return f"repo_class:{verdict.repo_class}"


# ── scan_meta / artifact projection ─────────────────────────────────────


def scan_meta_block(verdict: RepoClassVerdict) -> dict[str, Any]:
    """The ``scan_meta['repo_class']`` value (stable key order)."""
    return {
        "class": verdict.repo_class,
        "confidence": verdict.confidence,
        "rationale": verdict.rationale,
        "matched_signals": list(verdict.matched_signals),
        "gate_enabled": gate_enabled(),
        "uf_suppression_eligible": should_suppress_user_flows(verdict),
    }


def write_repo_class_artifact(
    ctx: "ScanContext",
    verdict: RepoClassVerdict,
) -> None:
    """Write ``06-stage-repo_class.json`` when ``ctx.run_dir`` is set.

    Mirrors the 0.6 family. No-op in CLI mode (run_dir None).
    """
    run_dir = getattr(ctx, "run_dir", None)
    if run_dir is None:
        return
    try:
        from faultline.pipeline_v2.stage_7_output import write_stage_artifact
    except ImportError:
        return
    payload = {
        "stage": "0.7-repo-class",
        "run_id": getattr(ctx, "run_id", None),
        "verdict": verdict.as_dict(),
        "gate_enabled": gate_enabled(),
        "suppress_user_flows": should_suppress_user_flows(verdict),
        "suppress_min_confidence": SUPPRESS_MIN_CONFIDENCE,
    }
    try:
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="repo_class",
            payload=payload,
            run_dir=run_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage_0_7_repo_class: failed to write artifact: %s", exc)


# ── Signal helpers ───────────────────────────────────────────────────────


def _pyproject_name_and_server_deps(text: str) -> tuple[str, tuple[str, ...]]:
    """``([project].name, sorted server-framework runtime deps)``.

    Parsed with :mod:`tomllib` (not regex). Only ``[project].dependencies``
    counts — a framework in dev/test extras does not make an app.
    Safe on malformed manifests: ``("", ())``.
    """
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return "", ()
    project = data.get("project")
    if not isinstance(project, dict):
        return "", ()
    raw_name = project.get("name")
    name = raw_name.strip().lower().replace("_", "-") if isinstance(raw_name, str) else ""
    deps = project.get("dependencies")
    found: set[str] = set()
    if isinstance(deps, list):
        for req in deps:
            if not isinstance(req, str):
                continue
            m = _RE_REQUIREMENT_NAME.match(req)
            if m is None:
                continue
            dist = m.group(1).lower().replace("_", "-")
            if dist in _PY_SERVER_FRAMEWORK_DEPS:
                found.add(dist)
    return name, tuple(sorted(found))


def _go_module_self_name(go_mod_text: str | None) -> str:
    """Last path segment of the ``module`` directive, de-versioned
    (``github.com/go-chi/chi/v5`` -> ``chi``). ``""`` when absent."""
    if not go_mod_text:
        return ""
    for line in go_mod_text.splitlines():
        line = line.strip()
        if line.startswith("module "):
            module = line.split(None, 1)[1].strip()
            segments = [s for s in module.split("/") if s]
            if segments and re.fullmatch(r"v\d+", segments[-1]):
                segments = segments[:-1]
            return segments[-1].lower() if segments else ""
    return ""


def _cargo_self_signals(
    cargo_text: str, root: Path,
) -> tuple[str, tuple[str, ...], bool]:
    """``(package name, server crate deps, has binary entry)`` of the
    ROOT Cargo manifest. Unlike Stage 0.6b's per-workspace probe this
    accepts a manifest that carries BOTH ``[package]`` and
    ``[workspace]`` (qdrant's root crate is the workspace root)."""
    try:
        data = tomllib.loads(cargo_text)
    except (tomllib.TOMLDecodeError, ValueError):
        return "", (), False
    package = data.get("package")
    if not isinstance(package, dict):
        return "", (), False
    raw_name = package.get("name")
    name = raw_name.strip().lower() if isinstance(raw_name, str) else ""
    keys: set[str] = set()
    for section_name in ("dependencies", "dev-dependencies", "build-dependencies"):
        section = data.get(section_name)
        if isinstance(section, dict):
            keys.update(k for k in section.keys() if isinstance(k, str))
    server = tuple(sorted(keys & _RUST_SERVER_FRAMEWORK_DEPS))
    bins = data.get("bin")
    has_bin = (root / "src" / "main.rs").exists() or (
        isinstance(bins, list) and len(bins) > 0
    )
    return name, server, has_bin


def _cmd_has_package_main(root: Path) -> bool:
    """True when any ``cmd/<name>/*.go`` declares ``package main``.

    Broader than Stage 0.6's ``cmd/<x>/main.go`` filename convention:
    the Go binary entry is the PACKAGE declaration, not the file name
    (traefik's entry is ``cmd/traefik/traefik.go``). Reads at most the
    non-test ``*.go`` files directly under each ``cmd/`` subdir.
    """
    cmd = root / "cmd"
    if not cmd.is_dir():
        return False
    try:
        for entry in sorted(cmd.iterdir()):
            if not entry.is_dir():
                continue
            for go_file in sorted(entry.glob("*.go")):
                if go_file.name.endswith("_test.go"):
                    continue
                text = _read_text_safe(go_file)
                if text is not None and _RE_PACKAGE_MAIN.search(text):
                    return True
    except OSError:
        return False
    return False


def _first_daemon_dir(root: Path) -> str | None:
    """First existing long-running-process dir marker (or ``None``)."""
    for candidate in _DAEMON_DIR_SEGMENTS:
        try:
            if (root / candidate).is_dir():
                return candidate
        except OSError:
            continue
    return None


def _monorepo_has_product_workspace(
    ctx: "ScanContext",
) -> tuple[bool, tuple[str, ...]]:
    """Reuse the 0.6b per-workspace classifiers: does ANY enumerated
    workspace classify as an app/service scan unit? Sample carries up
    to 3 workspace paths for the rationale."""
    workspaces = list(getattr(ctx, "workspaces", None) or [])
    if len(workspaces) < 2:
        return False, ()
    repo_root = Path(ctx.repo_path)
    hits: list[str] = []
    for ws in workspaces:
        try:
            verdict = classify_project(repo_root, ws)
        except Exception:  # noqa: BLE001 — defensive; 0.6b is already defensive
            continue
        if verdict.project_type in _UNIT_TYPES:
            hits.append(ws.path)
            if len(hits) >= 3:
                break
    return bool(hits), tuple(hits)


__all__ = [
    "REPO_CLASS_PRODUCT_APP",
    "REPO_CLASS_LIBRARY",
    "REPO_CLASS_CLI_TOOL",
    "REPO_CLASS_INFRA_DAEMON",
    "REPO_CLASS_FRAMEWORK",
    "NON_PRODUCT_CLASSES",
    "SUPPRESS_MIN_CONFIDENCE",
    "GATE_ENV",
    "RepoClassSignals",
    "RepoClassVerdict",
    "RepoClassifier",
    "FrameworkSelfClassifier",
    "ProductAppClassifier",
    "InfraDaemonClassifier",
    "CliToolClassifier",
    "LibraryClassifier",
    "ResidualProductAppClassifier",
    "classify_repo_class",
    "classify_repo_class_per_unit",
    "gate_enabled",
    "scan_meta_block",
    "should_suppress_user_flows",
    "suppression_reason",
    "write_repo_class_artifact",
]
