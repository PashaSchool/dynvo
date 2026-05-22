"""Stage 0.6 — Repo Shape Classifier.

Deterministic (no LLM) classification of how a repository is ORGANIZED.
Distinct from Stage 0.5 (``audited_stack``):

  - ``audited_stack`` is a stack-tag (e.g. ``next-app-router``, ``go-server``).
  - ``repo_shape``  is an architecture-tag describing how the codebase is
    laid out (e.g. ``turborepo-monorepo``, ``oss-library``, ``backend-monolith``).

Stage 8's flow-rollup dispatcher uses ``repo_shape`` to pick the right
flow-attribution strategy (workspace-match for monorepos, entry-point-in-
paths for single-SaaS routed apps, controller-match for MVC backends, etc.).

Design tenets
=============

  - Pure function. ``classify_repo_shape(ctx)`` is idempotent: same
    inputs → same output. Safe to call without ``ctx.run_dir`` (CLI mode).
  - Composition over inheritance. Every shape is a tiny standalone class
    implementing the :class:`ShapeClassifier` Protocol.
  - Strategy registry. ``_DEFAULT_CLASSIFIERS`` is the single source of
    truth; tests inject fakes via the optional ``classifiers`` argument.
  - Universal thresholds. ``MIN_CONFIDENCE`` and ``FALLBACK_CONFIDENCE``
    are scale-invariant constants — not magic numbers tuned per repo.
  - No README parsing. ``ShapeSignals.collect()`` only reads structured
    files (manifests + folder presence). See ``CLAUDE.md``.
  - Graceful degradation. A buggy classifier returns ``None`` (logged);
    the next classifier runs. The :class:`UniversalResidualClassifier`
    fallback ALWAYS returns a result.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# ── Universal thresholds ───────────────────────────────────────────────

# Below this, a classifier's verdict is ignored and the next is tried.
# ``0.60`` reflects "supermajority of signals" — a universal cutoff, not
# a repo-specific tuning knob.
MIN_CONFIDENCE: float = 0.60

# The :class:`UniversalResidualClassifier` always returns this score.
# ``0.40`` reflects "below half of evidence — use safe defaults".
FALLBACK_CONFIDENCE: float = 0.40


# ── Data types ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ShapeSignals:
    """Pre-computed structural snapshot of the repo.

    Collected ONCE at the top of :func:`classify_repo_shape` and fed
    to every classifier. Pure structural facts — no judgment.

    All values are derived from ``ctx`` + cheap filesystem checks
    (folder existence, manifest field presence). Never reads ``*.md``
    or any prose file — that would violate the no-README rule.
    """

    # ── From Stage 0 / 0.5 ───────────────────────────────────────────
    audited_stack: str | None
    stage_0_stack: str | None
    secondary_stacks: tuple[str, ...]
    monorepo: bool
    workspace_count: int
    workspace_names: tuple[str, ...]
    extractor_hints: tuple[str, ...]

    # ── Root manifests (bool flags) ──────────────────────────────────
    has_package_json: bool
    has_pnpm_workspace: bool
    has_turbo_json: bool
    has_pyproject: bool
    has_cargo_toml: bool
    has_cargo_workspace: bool
    has_go_mod: bool
    has_gemfile: bool
    has_composer_json: bool

    # ── Routing / app markers ────────────────────────────────────────
    has_app_router_dir: bool
    has_pages_router_dir: bool
    has_rails_app_dir: bool
    has_django_manage_py: bool
    has_fastapi_app_factory: bool
    has_remix_routes_dir: bool
    has_laravel_controllers_dir: bool

    # ── Binary / CLI markers ─────────────────────────────────────────
    has_bin_dir: bool
    has_cmd_dir: bool
    has_cli_py_entry: bool
    has_main_rs_bin: bool

    # ── Library markers ──────────────────────────────────────────────
    package_json_main_or_exports: bool
    package_json_no_app_entry: bool
    pyproject_has_project_section: bool
    cargo_is_single_crate: bool

    # ── Workspace shape detail ───────────────────────────────────────
    workspace_has_apps_dir: bool
    workspace_has_packages_dir: bool

    # ── Framework-self vs app ────────────────────────────────────────
    is_framework_self_repo: bool

    @classmethod
    def collect(cls, ctx: "ScanContext") -> "ShapeSignals":
        """Build a :class:`ShapeSignals` snapshot from ``ctx``.

        Performs the cheap filesystem checks needed for classification.
        NEVER reads ``*.md`` or any prose — only manifests + folder
        existence. See module docstring for the no-README rule.

        Defensive: every read swallows ``OSError`` / ``JSONDecodeError``
        and falls back to "signal absent". A broken manifest must not
        crash classification.
        """
        root = Path(ctx.repo_path)

        pkg_json_path = root / "package.json"
        pkg_json = _read_json_safe(pkg_json_path)
        pyproject_text = _read_text_safe(root / "pyproject.toml")
        cargo_text = _read_text_safe(root / "Cargo.toml")

        # ── workspace info from ctx ──
        workspaces = ctx.workspaces or []
        ws_count = len(workspaces)
        ws_names = tuple(w.path for w in workspaces)

        has_apps_dir = any(w.path.startswith("apps/") for w in workspaces) or (
            root / "apps"
        ).is_dir()
        has_packages_dir = any(w.path.startswith("packages/") for w in workspaces) or (
            root / "packages"
        ).is_dir()

        # ── package.json shape ──
        pkg_main_or_exports = False
        pkg_has_bin_field = False
        if pkg_json is not None:
            pkg_main_or_exports = bool(
                pkg_json.get("main")
                or pkg_json.get("exports")
                or pkg_json.get("module"),
            )
            pkg_has_bin_field = "bin" in pkg_json

        # ── pyproject shape ──
        pyproject_has_project = (
            pyproject_text is not None and "[project]" in pyproject_text
        )

        # ── Cargo shape ──
        has_cargo_workspace = (
            cargo_text is not None and "[workspace]" in cargo_text
        )
        cargo_single = (
            cargo_text is not None
            and "[package]" in cargo_text
            and "[workspace]" not in cargo_text
        )

        # ── Routing markers ──
        has_app_router = (root / "app" / "page.tsx").exists() or (
            root / "app" / "page.jsx"
        ).exists() or (root / "src" / "app" / "page.tsx").exists() or (
            root / "src" / "app" / "page.jsx"
        ).exists()
        has_pages_router = (root / "pages" / "_app.tsx").exists() or (
            root / "pages" / "_app.jsx"
        ).exists() or (root / "src" / "pages" / "_app.tsx").exists() or (
            root / "src" / "pages" / "_app.jsx"
        ).exists()
        # Looser fallbacks — many real Next repos don't have those exact
        # paths but DO have the directory.
        if not has_app_router:
            has_app_router = (root / "app").is_dir() and any(
                (root / "app").glob("**/page.*")
            )
        if not has_pages_router:
            has_pages_router = (root / "pages").is_dir() and any(
                p for p in (root / "pages").iterdir() if p.is_file()
                and p.suffix in {".tsx", ".jsx", ".ts", ".js"}
            )

        has_rails_app = (root / "app" / "controllers").is_dir() and (
            root / "app" / "models"
        ).is_dir()
        has_django = (root / "manage.py").exists()
        has_fastapi = _detect_fastapi_factory(root)
        has_remix = (root / "app" / "routes").is_dir()
        has_laravel = (root / "app" / "Http" / "Controllers").is_dir()

        # ── CLI / binary markers ──
        has_bin = (root / "bin").is_dir() and any(
            (root / "bin").iterdir()
        )
        has_cmd = (root / "cmd").is_dir() and any(
            p for p in (root / "cmd").iterdir() if p.is_dir()
        )
        has_cli_py = (
            (root / "cli.py").exists()
            or (root / "__main__.py").exists()
            or _pyproject_has_scripts(pyproject_text)
        )
        has_main_rs_bin = (root / "src" / "main.rs").exists()

        # ── package_json_no_app_entry ──
        # "No app entry" = no apps/ dir AND no router dir AND no server entrypoint
        pkg_no_app_entry = not (
            (root / "apps").is_dir()
            or has_app_router
            or has_pages_router
            or has_remix
            or (root / "server.js").exists()
            or (root / "server.ts").exists()
        )

        # ── Framework-self hint ──
        # Stage 0.5 emits "framework-self" via extractor_hints; absent
        # by default. We're conservative: only fires when the auditor
        # explicitly flagged it.
        framework_self = "framework-self" in (ctx.extractor_hints or ())

        return cls(
            audited_stack=ctx.audited_stack,
            stage_0_stack=ctx.stack,
            secondary_stacks=tuple(ctx.secondary_stacks or ()),
            monorepo=bool(ctx.monorepo),
            workspace_count=ws_count,
            workspace_names=ws_names,
            extractor_hints=tuple(ctx.extractor_hints or ()),
            has_package_json=pkg_json is not None,
            has_pnpm_workspace=(root / "pnpm-workspace.yaml").exists(),
            has_turbo_json=(root / "turbo.json").exists()
            or (root / "turbo.jsonc").exists(),
            has_pyproject=(root / "pyproject.toml").exists(),
            has_cargo_toml=(root / "Cargo.toml").exists(),
            has_cargo_workspace=has_cargo_workspace,
            has_go_mod=(root / "go.mod").exists(),
            has_gemfile=(root / "Gemfile").exists(),
            has_composer_json=(root / "composer.json").exists(),
            has_app_router_dir=has_app_router,
            has_pages_router_dir=has_pages_router,
            has_rails_app_dir=has_rails_app,
            has_django_manage_py=has_django,
            has_fastapi_app_factory=has_fastapi,
            has_remix_routes_dir=has_remix,
            has_laravel_controllers_dir=has_laravel,
            has_bin_dir=has_bin or pkg_has_bin_field,
            has_cmd_dir=has_cmd,
            has_cli_py_entry=has_cli_py,
            has_main_rs_bin=has_main_rs_bin,
            package_json_main_or_exports=pkg_main_or_exports,
            package_json_no_app_entry=pkg_no_app_entry,
            pyproject_has_project_section=pyproject_has_project,
            cargo_is_single_crate=cargo_single,
            workspace_has_apps_dir=has_apps_dir,
            workspace_has_packages_dir=has_packages_dir,
            is_framework_self_repo=framework_self,
        )


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Verdict returned by a classifier or the dispatcher."""

    shape: str
    confidence: float
    rationale: str
    matched_signals: tuple[str, ...] = ()


@runtime_checkable
class ShapeClassifier(Protocol):
    """Strategy interface for one architectural shape.

    Implementations are tiny: a name, a priority, and a ``classify``
    method that returns a :class:`ClassificationResult` or ``None``.

    Contract:
      - MUST NOT raise. Failures degrade to ``None``; the dispatcher
        logs a warning and proceeds.
      - MUST be a pure function of ``(ctx, signals)``. No I/O beyond
        what ``signals`` already contains.
    """

    name: str
    priority: int

    def classify(
        self,
        ctx: "ScanContext",
        signals: ShapeSignals,
    ) -> ClassificationResult | None:
        """Return a verdict if this shape applies; otherwise ``None``."""


# ── Concrete classifiers ───────────────────────────────────────────────


class TurborepoMonorepoClassifier:
    """Canonical apps/+packages/ pnpm/turbo monorepo."""

    name: str = "turborepo-monorepo"
    priority: int = 10

    def classify(
        self,
        ctx: "ScanContext",
        signals: ShapeSignals,
    ) -> ClassificationResult | None:
        if not signals.monorepo:
            return None
        if not (signals.has_turbo_json or signals.has_pnpm_workspace):
            return None
        if signals.workspace_count < 2:
            return None

        matched: list[str] = []
        matched.append("monorepo")
        if signals.has_turbo_json:
            matched.append("has_turbo_json")
        if signals.has_pnpm_workspace:
            matched.append("has_pnpm_workspace")

        if signals.workspace_has_apps_dir and signals.workspace_has_packages_dir:
            conf = 0.95
            matched.extend(["workspace_has_apps_dir", "workspace_has_packages_dir"])
            layout = "apps/+packages/ canonical Turborepo"
        elif signals.workspace_has_apps_dir or signals.workspace_has_packages_dir:
            conf = 0.85
            if signals.workspace_has_apps_dir:
                matched.append("workspace_has_apps_dir")
            if signals.workspace_has_packages_dir:
                matched.append("workspace_has_packages_dir")
            layout = "apps/ XOR packages/ layout"
        else:
            conf = 0.70
            layout = "flat workspaces (no apps/ or packages/)"

        manager = "turbo.json" if signals.has_turbo_json else "pnpm-workspace.yaml"
        rationale = (
            f"Detected {signals.workspace_count} workspaces with {manager}; "
            f"{layout}."
        )
        return ClassificationResult(
            shape=self.name,
            confidence=conf,
            rationale=rationale,
            matched_signals=tuple(matched),
        )


class OssLibraryClassifier:
    """Single-package library with no app entry-point."""

    name: str = "oss-library"
    priority: int = 20

    def classify(
        self,
        ctx: "ScanContext",
        signals: ShapeSignals,
    ) -> ClassificationResult | None:
        # Library classifier never fires for monorepos (Turborepo wins).
        if signals.monorepo:
            return None

        # Branch 1 — JS library.
        js_match = (
            signals.has_package_json
            and signals.package_json_main_or_exports
            and signals.package_json_no_app_entry
        )
        # Branch 2 — Python library.
        py_match = (
            signals.has_pyproject
            and signals.pyproject_has_project_section
            and not signals.has_fastapi_app_factory
            and not signals.has_django_manage_py
        )
        # Branch 3 — Rust library (single-crate, no bin).
        rust_match = (
            signals.has_cargo_toml
            and signals.cargo_is_single_crate
            and not signals.has_main_rs_bin
        )

        if not (js_match or py_match or rust_match):
            return None

        matched: list[str] = []
        if js_match:
            kind = "JS"
            matched.extend([
                "has_package_json",
                "package_json_main_or_exports",
                "package_json_no_app_entry",
            ])
            api_source = "main/exports"
        elif py_match:
            kind = "Python"
            matched.extend(["has_pyproject", "pyproject_has_project_section"])
            api_source = "[project]"
        else:
            kind = "Rust"
            matched.extend(["has_cargo_toml", "cargo_is_single_crate"])
            api_source = "[package]"

        # Borderline: has bin/ alongside library exports.
        borderline = js_match and signals.has_bin_dir
        conf = 0.75 if borderline else 0.90

        bracketed = "{" + api_source + "}"
        rationale = (
            f"Single-package {kind} library: declares public API via "
            f"{bracketed} with no app entry-point."
        )
        if borderline:
            rationale += " (Note: also exposes a CLI binary.)"
        return ClassificationResult(
            shape=self.name,
            confidence=conf,
            rationale=rationale,
            matched_signals=tuple(matched),
        )


class BackendMonolithClassifier:
    """Rails / Django / Laravel MVC backend monolith."""

    name: str = "backend-monolith"
    priority: int = 30

    def classify(
        self,
        ctx: "ScanContext",
        signals: ShapeSignals,
    ) -> ClassificationResult | None:
        if signals.monorepo:
            return None

        if signals.has_gemfile and signals.has_rails_app_dir:
            return ClassificationResult(
                shape=self.name,
                confidence=0.90,
                rationale=(
                    "MVC backend monolith (Rails); Gemfile + app/controllers + "
                    "app/models present at conventional paths."
                ),
                matched_signals=("has_gemfile", "has_rails_app_dir"),
            )

        if signals.has_django_manage_py:
            return ClassificationResult(
                shape=self.name,
                confidence=0.90,
                rationale=(
                    "MVC backend monolith (Django); manage.py present at root."
                ),
                matched_signals=("has_django_manage_py",),
            )

        if signals.has_composer_json and signals.has_laravel_controllers_dir:
            return ClassificationResult(
                shape=self.name,
                confidence=0.90,
                rationale=(
                    "MVC backend monolith (Laravel); composer.json + "
                    "app/Http/Controllers present."
                ),
                matched_signals=(
                    "has_composer_json",
                    "has_laravel_controllers_dir",
                ),
            )

        return None


class CliToolClassifier:
    """Primary entry-point is a CLI binary."""

    name: str = "cli-tool"
    priority: int = 40

    def classify(
        self,
        ctx: "ScanContext",
        signals: ShapeSignals,
    ) -> ClassificationResult | None:
        if signals.monorepo:
            return None

        # Branch 1 — Go cmd/ binary.
        go_match = (
            signals.has_cmd_dir
            and signals.has_go_mod
            and (signals.audited_stack or "") in {"go-cli", "go-binary"}
        )

        # Branch 2 — Python script-only (CLI but no library [project]).
        py_match = (
            signals.has_cli_py_entry
            and signals.has_pyproject
            and not signals.pyproject_has_project_section
        )

        # Branch 3 — Rust single-crate with main.rs.
        rust_match = (
            signals.has_main_rs_bin
            and signals.cargo_is_single_crate
        )

        # Branch 4 — JS with bin/ but no app entry AND no library export.
        # (Pure CLI — if library exports also present, OssLibrary takes it.)
        js_match = (
            signals.has_package_json
            and signals.has_bin_dir
            and signals.package_json_no_app_entry
            and not signals.package_json_main_or_exports
        )

        if not (go_match or py_match or rust_match or js_match):
            return None

        if go_match:
            entry = "cmd/"
            matched = ("has_cmd_dir", "has_go_mod", "audited_stack")
        elif py_match:
            entry = "cli.py/__main__.py"
            matched = ("has_cli_py_entry", "has_pyproject")
        elif rust_match:
            entry = "src/main.rs"
            matched = ("has_main_rs_bin", "cargo_is_single_crate")
        else:
            entry = "bin/"
            matched = (
                "has_package_json",
                "has_bin_dir",
                "package_json_no_app_entry",
            )

        return ClassificationResult(
            shape=self.name,
            confidence=0.85,
            rationale=(
                f"Primary entry-point is a CLI binary ({entry}); "
                f"1-command-1-flow attribution applies."
            ),
            matched_signals=matched,
        )


class FrameworkRepoClassifier:
    """Repo IS the framework, not an app built on it (e.g. next.js source repo)."""

    name: str = "framework-repo"
    priority: int = 50

    def classify(
        self,
        ctx: "ScanContext",
        signals: ShapeSignals,
    ) -> ClassificationResult | None:
        if not signals.is_framework_self_repo:
            return None
        return ClassificationResult(
            shape=self.name,
            confidence=0.85,
            rationale=(
                "Repo IS the framework, not an app built on it; flow names "
                "are usage patterns, not user journeys."
            ),
            matched_signals=("is_framework_self_repo",),
        )


class SingleSaasRoutedClassifier:
    """Single-package routed SaaS app (Next / Remix / FastAPI app)."""

    name: str = "single-saas-routed"
    priority: int = 60

    def classify(
        self,
        ctx: "ScanContext",
        signals: ShapeSignals,
    ) -> ClassificationResult | None:
        if signals.monorepo:
            return None

        if signals.has_app_router_dir:
            return ClassificationResult(
                shape=self.name,
                confidence=0.85,
                rationale=(
                    "Single-package routed app (next-app-router); flow → PF "
                    "via entry-point-in-paths attribution."
                ),
                matched_signals=("has_app_router_dir",),
            )
        if signals.has_pages_router_dir:
            return ClassificationResult(
                shape=self.name,
                confidence=0.85,
                rationale=(
                    "Single-package routed app (next-pages); flow → PF via "
                    "entry-point-in-paths attribution."
                ),
                matched_signals=("has_pages_router_dir",),
            )
        if signals.has_remix_routes_dir:
            return ClassificationResult(
                shape=self.name,
                confidence=0.85,
                rationale=(
                    "Single-package routed app (remix); flow → PF via "
                    "entry-point-in-paths attribution."
                ),
                matched_signals=("has_remix_routes_dir",),
            )
        if (
            signals.has_fastapi_app_factory
            and not signals.pyproject_has_project_section
        ):
            return ClassificationResult(
                shape=self.name,
                confidence=0.85,
                rationale=(
                    "Single-package routed app (fastapi-app); flow → PF via "
                    "entry-point-in-paths attribution."
                ),
                matched_signals=("has_fastapi_app_factory",),
            )
        return None


class UniversalResidualClassifier:
    """Always-wins safety-net; emits ``shape="universal-residual"``."""

    name: str = "universal-residual"
    priority: int = 999

    def classify(
        self,
        ctx: "ScanContext",
        signals: ShapeSignals,
    ) -> ClassificationResult:
        return ClassificationResult(
            shape=self.name,
            confidence=FALLBACK_CONFIDENCE,
            rationale=(
                f"No specific shape matched above threshold "
                f"{MIN_CONFIDENCE:.2f}; falling back to universal-residual "
                "rollup (entry-point + 50%-overlap)."
            ),
            matched_signals=(),
        )


_DEFAULT_CLASSIFIERS: tuple[ShapeClassifier, ...] = (
    TurborepoMonorepoClassifier(),
    OssLibraryClassifier(),
    BackendMonolithClassifier(),
    CliToolClassifier(),
    FrameworkRepoClassifier(),
    SingleSaasRoutedClassifier(),
    UniversalResidualClassifier(),
)


# ── Dispatcher ─────────────────────────────────────────────────────────


def classify_repo_shape(
    ctx: "ScanContext",
    classifiers: Sequence[ShapeClassifier] | None = None,
) -> ClassificationResult:
    """Run classifiers in priority order; first to clear MIN_CONFIDENCE wins.

    Args:
        ctx: ``ScanContext`` with Stage 0 + Stage 0.5 fields populated.
            ``ctx.run_dir`` is read but only WRITTEN when not ``None``
            (CLI mode passes ``None`` to keep the function side-effect-
            free).
        classifiers: optional override; defaults to ``_DEFAULT_CLASSIFIERS``.

    Returns:
        A :class:`ClassificationResult`. Always non-None — the
        :class:`UniversalResidualClassifier` is the safety net.

    Side effects:
        Writes ``06-stage-shape.json`` to ``ctx.run_dir`` when set.
        Otherwise pure.

    Idempotent: same ctx → same result. No LLM. No network.
    """
    classifier_list = list(classifiers) if classifiers is not None else list(
        _DEFAULT_CLASSIFIERS,
    )
    signals = ShapeSignals.collect(ctx)

    # Sort by (priority, name) for deterministic ordering.
    ordered = sorted(classifier_list, key=lambda c: (c.priority, c.name))
    evaluations: list[dict[str, Any]] = []
    winner: ClassificationResult | None = None
    fallback_used = False

    for clf in ordered:
        try:
            result = clf.classify(ctx, signals)
        except Exception as exc:  # noqa: BLE001 — degrade silently
            logger.warning(
                "shape_classifier_error name=%s err=%s", clf.name, exc,
            )
            evaluations.append(
                {"classifier": clf.name, "result": None, "error": str(exc)},
            )
            continue
        evaluations.append(
            {
                "classifier": clf.name,
                "result": _result_to_dict(result) if result is not None else None,
            },
        )
        if result is not None and result.confidence >= MIN_CONFIDENCE:
            winner = result
            break

    if winner is None:
        # No classifier (other than residual) cleared MIN_CONFIDENCE.
        # If residual is registered, find its evaluation result.
        for entry in evaluations:
            if (
                entry.get("classifier") == "universal-residual"
                and entry.get("result") is not None
            ):
                d = entry["result"]
                winner = ClassificationResult(
                    shape=d["shape"],
                    confidence=d["confidence"],
                    rationale=d["rationale"],
                    matched_signals=tuple(d.get("matched_signals", ())),
                )
                fallback_used = True
                break
        if winner is None:
            # Residual not registered — synthesize one.
            winner = UniversalResidualClassifier().classify(ctx, signals)
            evaluations.append(
                {
                    "classifier": "universal-residual",
                    "result": _result_to_dict(winner),
                    "note": "synthesized (residual not in registry)",
                },
            )
            fallback_used = True

    _maybe_write_artifact(
        ctx=ctx,
        signals=signals,
        evaluations=evaluations,
        winner=winner,
        fallback_used=fallback_used,
    )
    return winner


# ── Helpers ────────────────────────────────────────────────────────────


def _read_json_safe(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _pyproject_has_scripts(text: str | None) -> bool:
    """True when pyproject declares ``[project.scripts]`` (CLI entry points)."""
    return text is not None and "[project.scripts]" in text


def _detect_fastapi_factory(root: Path) -> bool:
    """Detect a top-level ``FastAPI()`` factory in common entry files.

    Scans only a small fixed set of candidates — never the whole tree.
    """
    candidates = (
        root / "main.py",
        root / "app.py",
        root / "app" / "main.py",
        root / "src" / "main.py",
    )
    for c in candidates:
        text = _read_text_safe(c)
        if text is None:
            continue
        if "FastAPI(" in text:
            return True
    return False


def _result_to_dict(r: ClassificationResult) -> dict[str, Any]:
    return {
        "shape": r.shape,
        "confidence": r.confidence,
        "rationale": r.rationale,
        "matched_signals": list(r.matched_signals),
    }


def _maybe_write_artifact(
    *,
    ctx: "ScanContext",
    signals: ShapeSignals,
    evaluations: list[dict[str, Any]],
    winner: ClassificationResult,
    fallback_used: bool,
) -> None:
    """Write ``06-stage-shape.json`` if ``ctx.run_dir`` is set."""
    run_dir = getattr(ctx, "run_dir", None)
    if run_dir is None:
        return
    try:
        from faultline.pipeline_v2.stage_7_output import write_stage_artifact
    except ImportError:
        return
    payload = {
        "stage": "0.6-shape-classifier",
        "run_id": getattr(ctx, "run_id", None),
        "signals": asdict(signals),
        "evaluations": evaluations,
        "winner": _result_to_dict(winner),
        "min_confidence": MIN_CONFIDENCE,
        "fallback_used": fallback_used,
    }
    try:
        write_stage_artifact(
            ctx.repo_path,
            stage_index=6,
            stage_name="shape",
            payload=payload,
            run_dir=run_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage_0_6_shape: failed to write artifact: %s", exc)


__all__ = [
    "MIN_CONFIDENCE",
    "FALLBACK_CONFIDENCE",
    "ShapeSignals",
    "ClassificationResult",
    "ShapeClassifier",
    "TurborepoMonorepoClassifier",
    "OssLibraryClassifier",
    "BackendMonolithClassifier",
    "CliToolClassifier",
    "FrameworkRepoClassifier",
    "SingleSaasRoutedClassifier",
    "UniversalResidualClassifier",
    "classify_repo_shape",
]
