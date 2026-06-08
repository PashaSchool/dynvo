"""Stage 0 — repo intake.

Cheap, LLM-free read of the repository: git history, tracked files,
stack detection (single-app or monorepo with per-workspace stacks),
monorepo enumeration. The result, ``ScanContext``, is the only thing
Stage 1 needs to begin.

Design notes:

  - Reuses ``faultline.analyzer.git`` for repo/commits/tracked-files
    (do not duplicate git logic here).
  - Reuses ``faultline.analyzer.workspace.detect_workspace`` for
    monorepo enumeration (it already covers pnpm/npm/yarn/turbo/nx/
    lerna/cargo/go.work).
  - Stack detection is *new*: existing ``analyzer/repo_classifier``
    only classifies layout (feature/layer/monorepo) and library-vs-app.
    Neither maps to a stack slug. Stage 0 owns that mapping.
  - Stack slugs are deliberately conservative — when we see strong
    signals (e.g. ``next.config.*`` + ``app/`` dir) we return
    ``"next-app-router"``; when we see only one of those signals we
    drop a tier; when we see nothing we return ``None`` and let the
    unknown-stack handler take over downstream.

No LLM calls. No network calls. Pure file-system + git reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faultline.cache.backend import CacheBackend
    from faultline.pipeline_v2.stage_0_6_shape import ClassificationResult

from faultline.analyzer.git import get_commits, get_tracked_files, load_repo
from faultline.analyzer.workspace import (
    WorkspaceInfo,
    WorkspacePackage,
    detect_workspace,
)
from faultline.models.types import Commit


# ── Public types ────────────────────────────────────────────────────────────


@dataclass
class Workspace:
    """One package inside a monorepo, with its own detected stack.

    ``package_json`` is the parsed ``package.json`` contents when the
    workspace is JS/TS (the v2 extractors need fast access to the
    dependency list and the ``scripts`` block). ``None`` when the
    workspace isn't a JS/TS package — Rust crates, Go modules, etc.
    will get their own manifest fields in later stages; right now
    only JS/TS is consumed downstream so anything else is allowed
    to remain ``None``.
    """

    name: str
    path: str  # relative to repo root, e.g. "apps/web"
    package_json: dict[str, object] | None = None
    stack: str | None = None
    files: list[str] = field(default_factory=list)


@dataclass
class ScanContext:
    """Output of Stage 0 — input to Stage 1.

    Carries everything later stages need without re-walking the
    filesystem or re-reading git. Workspaces is populated only when
    ``monorepo`` is True; single-app repos get ``workspaces=None``
    and ``stack`` on the context itself.

    ``run_id`` and ``run_dir`` isolate every scan run on disk so
    consecutive runs of the same repo don't overwrite each other's
    artifacts. The orchestrator (or a CLI override) assigns them;
    Stage 0 populates them with sensible defaults so tests + ad-hoc
    callers also get isolation without extra plumbing.

    Sprint A3 — Stage 0.5 auditor fields
    ====================================

    ``audited_stack`` / ``secondary_stacks`` / ``extractor_hints`` /
    ``auditor_confidence`` are populated by the Stage 0.5 auditor when
    its confidence ≥ ``MIN_CONFIDENCE_TO_APPLY``. They are *additive*
    — Stage 0's ``stack`` field is NEVER mutated. Downstream consumers
    can use ``ctx.audited_stack or ctx.stack`` to pick whichever has
    been blessed for this run.
    """

    repo_path: Path
    stack: str | None
    monorepo: bool
    workspaces: list[Workspace] | None
    tracked_files: list[str]
    commits: list[Commit]
    # Detection telemetry — surfaced under ``FeatureMap.scan_meta``
    # so we can debug stack misdetections without re-running.
    stack_signals: list[str] = field(default_factory=list)
    workspace_manager: str | None = None  # "pnpm" / "turbo" / "cargo" / etc.
    # Per-run isolation. ``run_id`` is the directory name under
    # ``~/.faultline/logs/<slug>/<run_id>/``; ``run_dir`` is the
    # resolved absolute path (cached so stages don't recompute).
    run_id: str | None = None
    run_dir: Path | None = None
    # Sprint A3 — auditor additions. None / empty until Stage 0.5 runs
    # and its confidence clears MIN_CONFIDENCE_TO_APPLY (0.5).
    audited_stack: str | None = None
    secondary_stacks: tuple[str, ...] = ()
    extractor_hints: tuple[str, ...] = ()
    auditor_confidence: float | None = None
    # Sprint S6.1 — Stage 0.6 shape classifier additions. ``repo_shape``
    # is a deterministic architecture tag (e.g. ``turborepo-monorepo``,
    # ``oss-library``, ``backend-monolith``, ``single-saas-routed``,
    # ``cli-tool``, ``framework-repo``, ``universal-residual``).
    # Populated by ``stage_0_6_shape.classify_repo_shape``; ``None``
    # until Stage 0.6 runs. Stage 8's flow-rollup dispatcher uses this
    # to pick the correct rollup strategy.
    repo_shape: str | None = None
    shape_confidence: float = 0.0
    shape_rationale: str = ""
    # Pluggable cache backend (spec: encrypted-db-cache-backend). The
    # orchestrator (``pipeline_v2/run.py``) constructs it once via
    # ``faultline.cache.get_cache_backend`` and threads it here so cache
    # call sites route through it instead of hardcoding ``~/.faultline``.
    # ``None`` → those call sites fall back to the env-selected default
    # (preserves CLI / test behaviour). NOT a global singleton.
    cache_backend: "CacheBackend | None" = None

    def with_shape(self, result: "ClassificationResult") -> "ScanContext":
        """Return a NEW ScanContext with the shape-classification fields populated.

        Mirrors the ``with_audited_stack`` pattern — pure copy, no mutation
        of the source instance. Imported lazily to avoid a circular
        import (the classifier module imports ScanContext).
        """
        return ScanContext(
            repo_path=self.repo_path,
            stack=self.stack,
            monorepo=self.monorepo,
            workspaces=list(self.workspaces) if self.workspaces else self.workspaces,
            tracked_files=self.tracked_files,
            commits=self.commits,
            stack_signals=list(self.stack_signals),
            workspace_manager=self.workspace_manager,
            run_id=self.run_id,
            run_dir=self.run_dir,
            audited_stack=self.audited_stack,
            secondary_stacks=self.secondary_stacks,
            extractor_hints=self.extractor_hints,
            auditor_confidence=self.auditor_confidence,
            repo_shape=result.shape,
            shape_confidence=result.confidence,
            shape_rationale=result.rationale,
            cache_backend=self.cache_backend,
        )

    def with_audited_stack(
        self,
        *,
        audited_stack: str,
        secondary_stacks: tuple[str, ...] = (),
        extractor_hints: tuple[str, ...] = (),
        auditor_confidence: float = 1.0,
    ) -> "ScanContext":
        """Return a NEW ScanContext with the auditor fields populated.

        Stage 0's original ``stack`` value is preserved on the returned
        instance — the auditor is purely additive. Implemented as a
        deep-ish copy so the orchestrator's downstream stages can't
        retroactively mutate the original ctx.
        """
        return ScanContext(
            repo_path=self.repo_path,
            stack=self.stack,
            monorepo=self.monorepo,
            workspaces=list(self.workspaces) if self.workspaces else self.workspaces,
            tracked_files=self.tracked_files,
            commits=self.commits,
            stack_signals=list(self.stack_signals),
            workspace_manager=self.workspace_manager,
            run_id=self.run_id,
            run_dir=self.run_dir,
            audited_stack=audited_stack,
            secondary_stacks=tuple(secondary_stacks),
            extractor_hints=tuple(extractor_hints),
            auditor_confidence=auditor_confidence,
            repo_shape=self.repo_shape,
            shape_confidence=self.shape_confidence,
            shape_rationale=self.shape_rationale,
            cache_backend=self.cache_backend,
        )


# ── Stack detection ─────────────────────────────────────────────────────────

# Strong stack signals — when one fires we have high confidence about
# the framework. Order matters: more specific stacks (app-router) come
# before more general ones (next).
_JS_FRAMEWORK_DEPS = (
    # (dep_name_or_prefix, stack_slug)
    ("next", "next"),
    ("@remix-run/", "remix"),
    ("@sveltejs/kit", "sveltekit"),
    ("nuxt", "nuxt"),
    ("astro", "astro"),
    ("@tanstack/react-router", "tanstack-router"),
    ("react-router", "react-router"),
    ("vite", "vite"),
    ("@nestjs/core", "nestjs"),
    ("express", "express"),
    ("fastify", "fastify"),
    ("hono", "hono"),
)

_PY_FRAMEWORK_DEPS = (
    ("django", "django"),
    ("fastapi", "fastapi"),
    ("flask", "flask"),
    ("starlette", "starlette"),
)


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _detect_js_stack(
    root: Path,
    files: list[str],
    pkg: dict[str, object] | None,
) -> tuple[str | None, list[str]]:
    """Return (stack_slug, signals) for a JS/TS-shaped directory.

    Looks at:
      - package.json RUNTIME deps (dependencies + peerDependencies) for
        app-framework classification; devDependencies never make a repo
        an app of that framework (vite branch consults dev_deps only for
        an EXACT ``vite`` entry, never ``vitest``)
      - presence of framework config files (next.config.*, etc.)
      - presence of routing convention dirs (app/, pages/, src/routes/)
    """
    signals: list[str] = []

    # Separate RUNTIME deps (dependencies + peerDependencies) from
    # DEV deps. App-framework classification keys on runtime deps ONLY:
    # a library that dev-depends on express (for its own test server) or
    # on vitest (for its tests) is NOT an express/vite APP. Conflating
    # devDeps mis-tags pure libraries (got→express, yup→vite) and
    # suppresses JsLibraryExtractor downstream.
    runtime_deps: dict[str, object] = {}
    dev_deps: dict[str, object] = {}
    if pkg:
        for key in ("dependencies", "peerDependencies"):
            block = pkg.get(key)
            if isinstance(block, dict):
                runtime_deps.update(block)
        dev_block = pkg.get("devDependencies")
        if isinstance(dev_block, dict):
            dev_deps.update(dev_block)

    def _matches(deps: dict[str, object], name: str) -> bool:
        """Exact match, or scoped-prefix match only when *name* ends in '/'.

        Exact-or-scoped-prefix avoids the substring trap: ``vite`` must
        NOT match ``vitest`` and ``express`` must NOT match
        ``express-rate-limit``. A trailing slash (``@remix-run/``) opts
        into prefix matching for npm scopes.
        """
        for d in deps:
            if d == name:
                return True
            if name.endswith("/") and d.startswith(name):
                return True
        return False

    has_dep = lambda name: _matches(runtime_deps, name)  # noqa: E731

    has_next = has_dep("next")
    if has_next:
        signals.append("package.json depends on next")
    has_next_config = any(
        (root / f"next.config.{ext}").exists()
        for ext in ("js", "mjs", "ts", "cjs")
    )
    if has_next_config:
        signals.append("next.config.* present")
    has_app_dir = (root / "app").is_dir() or (root / "src" / "app").is_dir()
    has_pages_dir = (root / "pages").is_dir() or (root / "src" / "pages").is_dir()

    if has_next or has_next_config:
        # Distinguish App Router vs Pages Router. Prefer App Router
        # when the marker dirs co-exist (Next supports both but most
        # of the user-facing routes go through app/).
        if has_app_dir:
            signals.append("app/ directory present (Next App Router)")
            return "next-app-router", signals
        if has_pages_dir:
            signals.append("pages/ directory present (Next Pages Router)")
            return "next-pages", signals
        return "next", signals

    # Remix
    if has_dep("@remix-run/"):
        signals.append("package.json depends on @remix-run/*")
        return "remix", signals

    # SvelteKit
    if has_dep("@sveltejs/kit") or (root / "svelte.config.js").exists():
        signals.append("SvelteKit signals (dep or svelte.config.js)")
        return "sveltekit", signals

    # Nuxt
    if has_dep("nuxt") or any(
        (root / f"nuxt.config.{ext}").exists() for ext in ("js", "ts", "mjs")
    ):
        signals.append("Nuxt signals (dep or nuxt.config.*)")
        return "nuxt", signals

    # Astro
    if has_dep("astro") or any(
        (root / f"astro.config.{ext}").exists() for ext in ("js", "mjs", "ts")
    ):
        signals.append("Astro signals (dep or astro.config.*)")
        return "astro", signals

    # Generic React with TanStack Router
    if has_dep("@tanstack/react-router"):
        signals.append("@tanstack/react-router dep")
        return "tanstack-router", signals

    # Backend frameworks
    for dep_name, slug in (
        ("@nestjs/core", "nestjs"),
        ("express", "express"),
        ("fastify", "fastify"),
        ("hono", "hono"),
    ):
        if has_dep(dep_name):
            signals.append(f"package.json depends on {dep_name}")
            return slug, signals

    # Vite-based SPA (last resort for JS). Accept an EXACT ``vite`` dep
    # (runtime or dev) or a ``vite.config.*`` file — but NEVER ``vitest``
    # (that is a test runner, not an app-bundler signal). ``has_dep`` is
    # already exact-match on runtime deps; we add the exact dev-deps case
    # explicitly so a vitest-only devDep can't prefix-match into "vite".
    has_vite_config = any(
        (root / f"vite.config.{ext}").exists()
        for ext in ("ts", "js", "mjs", "cjs", "mts", "cts")
    )
    if has_dep("vite") or ("vite" in dev_deps) or has_vite_config:
        signals.append("Vite signals (exact vite dep or vite.config.*)")
        return "vite", signals

    # JS/TS but no recognised framework
    if pkg is not None:
        signals.append("package.json present but no known framework dep")
        return "js-generic", signals

    return None, signals


# Substrings that, when present in a ``settings.py``, prove it is a
# Django settings module rather than an arbitrary module named
# ``settings.py``. Scale-invariant (no per-repo magic) — these are
# Django framework identifiers, true on every Django project.
_DJANGO_SETTINGS_MARKERS = (
    "INSTALLED_APPS",
    "DJANGO_SETTINGS_MODULE",
    "django.",
)


def _is_django_repo(root: Path, files: list[str], dep_haystack: str) -> bool:
    """``True`` only when a genuine Django marker is present.

    Order is cheapest-first: dependency manifest substring → root
    ``manage.py`` → a ``settings.py`` containing Django config markers.
    Vendored ``settings.py`` under ``site-packages`` / ``node_modules``
    are ignored; an arbitrary ``tool_settings.py`` is ignored (the
    filename must be exactly ``settings.py``).
    """
    if "django" in dep_haystack:
        return True
    if (root / "manage.py").is_file():
        return True
    for rel in files:
        norm = rel.replace("\\", "/")
        leaf = norm.rsplit("/", 1)[-1]
        if leaf != "settings.py":
            continue
        if "/site-packages/" in f"/{norm}" or "/node_modules/" in f"/{norm}":
            continue
        blob = _read_text(root / rel)
        if blob and any(marker in blob for marker in _DJANGO_SETTINGS_MARKERS):
            return True
    return False


def _detect_python_stack(
    root: Path,
    files: list[str],
) -> tuple[str | None, list[str]]:
    """Return (stack_slug, signals) for a Python-shaped directory."""
    signals: list[str] = []

    # Read deps from pyproject.toml / requirements.txt without
    # importing tomllib for portability — we only need substring match.
    dep_text_blobs: list[str] = []
    for fname in ("pyproject.toml", "requirements.txt", "Pipfile", "setup.py"):
        blob = _read_text(root / fname)
        if blob is not None:
            dep_text_blobs.append(blob.lower())
            signals.append(f"{fname} present")
    haystack = "\n".join(dep_text_blobs)

    # Django — require a REAL Django marker, not a loose filename match.
    #
    # The previous heuristic fired on ANY tracked file whose name ended
    # in ``settings.py`` — which false-positives on FastAPI/Flask repos
    # that have a router/module named ``tool_settings.py`` (or vendored
    # ``…/site-packages/*/settings.py``). Django is now recognised only
    # when at least one of these holds:
    #   1. ``django`` is in the dependency manifests, OR
    #   2. a ``manage.py`` exists at the repo root (the canonical Django
    #      project entry point), OR
    #   3. a ``settings.py`` actually contains Django config markers
    #      (``INSTALLED_APPS`` / ``DJANGO_SETTINGS_MODULE`` /
    #      ``django.`` import).
    if _is_django_repo(root, files, haystack):
        signals.append("django dep, manage.py, or django-config settings.py")
        return "django", signals

    # FastAPI
    if "fastapi" in haystack:
        signals.append("fastapi in deps")
        return "fastapi", signals

    # Flask
    if "flask" in haystack:
        signals.append("flask in deps")
        return "flask", signals

    # Starlette
    if "starlette" in haystack:
        signals.append("starlette in deps")
        return "starlette", signals

    # Plain Python package
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        signals.append("Python package manifest present, no known web framework")
        return "python-lib", signals

    return None, signals


def _detect_rust_stack(root: Path) -> tuple[str | None, list[str]]:
    if (root / "Cargo.toml").exists():
        return "rust", ["Cargo.toml present"]
    return None, []


def _detect_go_stack(root: Path) -> tuple[str | None, list[str]]:
    if (root / "go.mod").exists():
        return "go", ["go.mod present"]
    return None, []


def _detect_ruby_stack(root: Path) -> tuple[str | None, list[str]]:
    if (root / "Gemfile").exists() or (root / "config.ru").exists():
        signals = ["Gemfile or config.ru present"]
        # Rails — config/application.rb is the canonical marker
        if (root / "config" / "application.rb").exists():
            signals.append("config/application.rb present (Rails)")
            return "rails", signals
        return "ruby", signals
    return None, []


def detect_stack(
    root: Path,
    files: list[str],
) -> tuple[str | None, list[str]]:
    """Detect the framework / stack of a directory.

    Returns ``(stack_slug, signals)`` where ``stack_slug`` is one of:
      - ``"next-app-router"``, ``"next-pages"``, ``"next"``
      - ``"remix"``, ``"sveltekit"``, ``"nuxt"``, ``"astro"``,
        ``"tanstack-router"``, ``"vite"``
      - ``"nestjs"``, ``"express"``, ``"fastify"``, ``"hono"``
      - ``"django"``, ``"fastapi"``, ``"flask"``, ``"starlette"``,
        ``"python-lib"``
      - ``"rust"``, ``"go"``, ``"rails"``, ``"ruby"``
      - ``"js-generic"`` (package.json with no known framework)
      - ``None`` (no signal — caller falls back to unknown-stack handling)

    Signals is a human-readable list of what was found, exposed via
    ``ScanContext.stack_signals`` for debugging misdetections.
    """
    pkg = _read_json(root / "package.json")
    stack, signals = _detect_js_stack(root, files, pkg)
    if stack:
        return stack, signals

    py_stack, py_signals = _detect_python_stack(root, files)
    signals.extend(py_signals)
    if py_stack:
        return py_stack, signals

    rust_stack, rust_signals = _detect_rust_stack(root)
    signals.extend(rust_signals)
    if rust_stack:
        return rust_stack, signals

    go_stack, go_signals = _detect_go_stack(root)
    signals.extend(go_signals)
    if go_stack:
        return go_stack, signals

    ruby_stack, ruby_signals = _detect_ruby_stack(root)
    signals.extend(ruby_signals)
    if ruby_stack:
        return ruby_stack, signals

    return None, signals


# ── Workspace upgrade ──────────────────────────────────────────────────────


def _enrich_workspaces(
    repo_path: Path,
    info: WorkspaceInfo,
) -> list[Workspace]:
    """Turn the legacy ``WorkspaceInfo.packages`` into the v2
    ``Workspace`` shape with per-package stack detection.
    """
    out: list[Workspace] = []
    for pkg in info.packages:
        pkg_root = repo_path / pkg.path
        pkg_json_data = _read_json(pkg_root / "package.json")
        stack, _signals = detect_stack(pkg_root, pkg.files)
        out.append(
            Workspace(
                name=pkg.name,
                path=pkg.path,
                package_json=pkg_json_data,
                stack=stack,
                files=list(pkg.files),
            ),
        )
    return out


# ── Public entry point ─────────────────────────────────────────────────────


def stage_0_intake(
    repo_path: str | Path,
    *,
    days: int = 365,
    skip_git: bool = False,
    run_id: str | None = None,
) -> ScanContext:
    """Run Stage 0 against ``repo_path`` and return a ``ScanContext``.

    Args:
        repo_path: Filesystem path to the repository root.
        days: How many days of history to load (default 365 — same
            as the legacy pipeline).
        skip_git: When True, skip ``load_repo`` / ``get_commits`` /
            ``get_tracked_files`` and walk the filesystem instead.
            Used by tests that build fixture repos without ``git init``.
        run_id: Override the auto-generated run id. When ``None``,
            Stage 0 generates ``<utc-ts>-<sha8>`` and creates
            ``~/.faultline/logs/<slug>/<run_id>/``. CLI users pass
            ``--run-id baseline`` to label A/B experiment runs.
    """
    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repo_path is not a directory: {repo_path}")

    if skip_git:
        tracked_files = _walk_tracked_files(repo_path)
        commits: list[Commit] = []
    else:
        repo = load_repo(str(repo_path))
        tracked_files = get_tracked_files(repo)
        commits = get_commits(repo, days=days)

    # Run-id assignment lives here (Stage 0 is the only place that
    # owns "this run started"). Import lazily to keep the stage's
    # legacy callers free of a new transitive dep.
    from faultline.pipeline_v2.run_dir import (
        generate_run_id,
        run_artifact_dir,
        sanitize_run_id,
    )

    resolved_run_id = (
        sanitize_run_id(run_id) if run_id else generate_run_id(repo_path)
    )
    resolved_run_dir = run_artifact_dir(repo_path, resolved_run_id)

    # Detect monorepo + enumerate workspaces (reuses analyzer/workspace).
    ws_info = detect_workspace(str(repo_path), tracked_files)

    if ws_info.detected:
        workspaces = _enrich_workspaces(repo_path, ws_info)
        # Root stack — the monorepo itself usually carries one too
        # (e.g. a Turbo monorepo where the root has next-config-only
        # tooling). Don't fail if there's no recognisable root stack.
        root_stack, root_signals = detect_stack(repo_path, tracked_files)
        return ScanContext(
            repo_path=repo_path,
            stack=root_stack,
            monorepo=True,
            workspaces=workspaces,
            tracked_files=tracked_files,
            commits=commits,
            stack_signals=root_signals,
            workspace_manager=ws_info.manager,
            run_id=resolved_run_id,
            run_dir=resolved_run_dir,
        )

    stack, signals = detect_stack(repo_path, tracked_files)
    return ScanContext(
        repo_path=repo_path,
        stack=stack,
        monorepo=False,
        workspaces=None,
        tracked_files=tracked_files,
        commits=commits,
        stack_signals=signals,
        workspace_manager=None,
        run_id=resolved_run_id,
        run_dir=resolved_run_dir,
    )


def _walk_tracked_files(root: Path) -> list[str]:
    """Fixture-friendly fallback that imitates ``get_tracked_files``
    without requiring a git repo. Skips the same noisy directories
    the real helper does.
    """
    skip_dirs = {
        "node_modules", "vendor", "venv", ".venv", ".git",
        "dist", "build", "out", "target", ".next", ".turbo",
        "__pycache__", ".pytest_cache", ".mypy_cache",
    }
    skip_exts = {
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
        ".pdf", ".zip", ".tar", ".gz", ".lock", ".sum",
        ".woff", ".woff2", ".ttf", ".eot", ".map",
    }
    out: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if path.suffix.lower() in skip_exts:
            continue
        out.append(str(rel))
    return out


__all__ = [
    "ScanContext",
    "Workspace",
    "detect_stack",
    "stage_0_intake",
]
