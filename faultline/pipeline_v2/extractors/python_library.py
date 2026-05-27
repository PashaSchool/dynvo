"""PythonLibraryExtractor — Python package layout + __init__.py exports.

For Python repositories that are LIBRARIES (no FastAPI/Django app
entry point), the feature map is encoded in the package layout:

  - The top-level package directory (often named after the project,
    e.g. ``fastapi/``, ``requests/``, ``flask/``) is the root.
  - Each major submodule (e.g. ``fastapi/routing/``,
    ``fastapi/dependencies/``) is a feature.
  - Public exports re-exported from the root ``__init__.py`` (via
    ``from .x import Y, Z`` or ``__all__``) name the public surface.

This shape is distinct from a Python APP (Django/FastAPI server)
where features come from URL patterns / routers — those are handled
by ``RouteFileExtractor``. The activation gate below short-circuits
on any app-shaped Python repo so we don't double-count.

Patterns live in ``eval/stacks/python-library.yaml``. The Python code
just loads + applies them.

No LLM. No network. Read-only.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    is_noise,
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


def _load_config() -> dict:
    """Load python-library.yaml from the packaged data tree (hermetic)."""
    return load_stack_yaml("python-library")


# ── Activation gate ────────────────────────────────────────────────────────


# App-marker patterns — when ANY of these fire on the repo, the
# extractor stays silent. We don't want to double-count features for
# a FastAPI APP (which has both library shape AND routers) — its
# RouteFileExtractor anchors win.
_APP_FILE_MARKERS = ("manage.py", "wsgi.py", "asgi.py")
_APP_CALL_PATTERN = re.compile(
    r"^\s*(?:app|application)\s*=\s*(?:FastAPI|Flask|Starlette|Django)\(",
    re.MULTILINE,
)


def _is_python_library(ctx: "ScanContext") -> bool:
    """``True`` when the auditor labelled the repo python-library OR
    Stage 0 saw Python AND no app-entry markers are present."""
    audited = (ctx.audited_stack or "").lower()
    if audited == "python-library":
        return True
    secondaries = tuple(s.lower() for s in (ctx.secondary_stacks or ()))
    if "python-library" in secondaries:
        return True

    is_python = (
        (ctx.stack or "").lower() in ("python", "fastapi", "django", "flask")
        or audited.startswith("python")
        or any(s.startswith("python") for s in secondaries)
    )
    if not is_python:
        return False

    # Disqualify when the repo looks like a Python APP. We check
    # tracked_files for app-marker filenames AND grep a small set of
    # top-level files for `app = FastAPI()` patterns.
    tracked = set(posix(f) for f in ctx.tracked_files)
    for marker in _APP_FILE_MARKERS:
        if marker in tracked or any(t.endswith("/" + marker) for t in tracked):
            return False

    # Scan a small whitelist of probable app-entry files for the
    # `app = FastAPI()` pattern. We don't scan everything — that
    # would defeat the activation purpose.
    for candidate in ("main.py", "app.py", "server.py"):
        if candidate in tracked or any(
            t.endswith("/" + candidate) for t in tracked
        ):
            text = read_text(ctx.repo_path / candidate)
            if text and _APP_CALL_PATTERN.search(text):
                return False
            # Also try the first match in tracked files when nested.
            for t in tracked:
                if t.endswith("/" + candidate):
                    txt = read_text(ctx.repo_path / t)
                    if txt and _APP_CALL_PATTERN.search(txt):
                        return False
                    break

    # When stack is exactly ``fastapi`` / ``django`` / ``flask`` and
    # there's no app marker, treat as library — this handles the
    # CASE OF the framework's OWN repo (fastapi the project IS
    # the framework library, not an app built on it).
    return True


# ── Root package discovery ─────────────────────────────────────────────────


def _discover_root_package(ctx: "ScanContext") -> str | None:
    """Find the top-level package directory containing ``__init__.py``.

    Strategy:
      1. Prefer the package whose name matches ``[project].name`` (with
         hyphen → underscore) from pyproject.toml when present.
      2. Otherwise pick the directory under repo root that has the most
         tracked ``.py`` files AND contains an ``__init__.py``.
    """
    tracked = [posix(f) for f in ctx.tracked_files]
    init_dirs: dict[str, int] = defaultdict(int)
    for t in tracked:
        if not t.endswith(".py"):
            continue
        parts = t.split("/")
        if len(parts) < 2:
            continue
        top = parts[0]
        # The package candidate has its own __init__.py at top/__init__.py
        init_dirs[top] += 1

    # Filter to ones with __init__.py
    init_present = {
        d for d in init_dirs
        if f"{d}/__init__.py" in tracked
    }
    if not init_present:
        return None

    # Try to align with pyproject [project].name if available.
    proj_name = _read_project_name(ctx.repo_path / "pyproject.toml")
    if proj_name:
        norm = proj_name.replace("-", "_").lower()
        for d in init_present:
            if d.lower() == norm:
                return d

    # Largest by .py file count, excluding test/example dirs.
    bad = {"tests", "test", "examples", "docs", "scripts"}
    ranked = sorted(
        (d for d in init_present if d.lower() not in bad),
        key=lambda d: init_dirs[d],
        reverse=True,
    )
    return ranked[0] if ranked else None


def _read_project_name(pyproject: Path) -> str | None:
    text = read_text(pyproject)
    if not text:
        return None
    # Use a tolerant regex — full TOML parsing would work but the
    # signal is small and we don't want a hard dep here.
    m = re.search(
        r'(?m)^\s*name\s*=\s*"([^"]+)"', text,
    )
    return m.group(1) if m else None


# ── __init__.py parsing ────────────────────────────────────────────────────


_FROM_REL_SINGLE = re.compile(
    r"^from\s+\.([\w.]*)\s+import\s+(.+?)$", re.MULTILINE,
)
_FROM_REL_PAREN = re.compile(
    r"^from\s+\.([\w.]*)\s+import\s+\(([^)]*)\)",
    re.MULTILINE | re.DOTALL,
)
_DUNDER_ALL = re.compile(
    r"^__all__\s*=\s*\[([^\]]*)\]", re.MULTILINE | re.DOTALL,
)


def _parse_init_exports(text: str) -> tuple[set[str], set[str]]:
    """Parse a package __init__.py for re-exported names.

    Returns ``(submodule_names, symbol_names)``.

      - ``submodule_names`` are the ``X`` in ``from .X import ...``
        (the first capture group, which IS the submodule).
      - ``symbol_names`` are the listed names from ``from .X import a, b``
        AND any names in ``__all__``.
    """
    submodules: set[str] = set()
    symbols: set[str] = set()

    if not text:
        return submodules, symbols

    for m in _FROM_REL_SINGLE.finditer(text):
        # Skip if the line is actually parenthesised — paren regex will
        # catch it. Detect by checking the import segment.
        if "(" in m.group(2):
            continue
        sub = (m.group(1) or "").strip(".")
        if sub:
            submodules.add(sub.split(".")[0])
        # Extract names from the import list.
        for raw in m.group(2).split(","):
            name = raw.strip().split(" as ")[0].strip()
            if name and name.isidentifier():
                symbols.add(name)

    for m in _FROM_REL_PAREN.finditer(text):
        sub = (m.group(1) or "").strip(".")
        if sub:
            submodules.add(sub.split(".")[0])
        for raw in m.group(2).split(","):
            name = (
                raw.strip()
                .strip("\n")
                .strip(",")
                .split(" as ")[0]
                .strip()
            )
            if name and name.isidentifier():
                symbols.add(name)

    for m in _DUNDER_ALL.finditer(text):
        body = m.group(1)
        # Names are quoted; pull anything inside single or double quotes.
        for raw in re.findall(r"['\"]([\w.]+)['\"]", body):
            symbols.add(raw)

    return submodules, symbols


# ── Extractor ──────────────────────────────────────────────────────────────


class PythonLibraryExtractor:
    """Python package layout + __init__.py exports → feature anchors."""

    name = "python-library"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config if config is not None else _load_config()

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not _is_python_library(ctx):
            return []

        root_pkg = _discover_root_package(ctx)
        if not root_pkg:
            return []

        tracked = [posix(f) for f in ctx.tracked_files]
        excludes = tuple(
            e for e in (self._config.get("excludes") or [])
            if isinstance(e, str)
        )

        def _excluded(p: str) -> bool:
            return any(p.startswith(ex) or f"/{ex}" in f"/{p}" for ex in excludes)

        # Parse root __init__.py for declared submodules + symbols.
        init_path = ctx.repo_path / root_pkg / "__init__.py"
        init_text = read_text(init_path) or ""
        declared_subs, declared_symbols = _parse_init_exports(init_text)

        # Discover ACTUAL submodules from tracked files: any directory
        # ``<root_pkg>/<name>/`` that contains its own ``__init__.py``
        # OR any ``<root_pkg>/<name>.py`` single-file module that is
        # NOT noise.
        sub_dirs: set[str] = set()
        sub_files: dict[str, str] = {}  # name → file path
        prefix = f"{root_pkg}/"
        for t in tracked:
            if not t.startswith(prefix):
                continue
            if _excluded(t):
                continue
            rest = t[len(prefix):]
            if "/" in rest:
                top = rest.split("/", 1)[0]
                # Confirm it's a package (has its own __init__.py)
                if f"{prefix}{top}/__init__.py" in tracked:
                    sub_dirs.add(top)
            else:
                # Single-file module like ``fastapi/applications.py``
                if rest.endswith(".py") and rest != "__init__.py":
                    name = rest[:-3]
                    if name and not is_noise(name) and name not in sub_files:
                        sub_files[name] = t

        confidence = self._config.get("confidence") or {}
        sub_conf = float(confidence.get("submodule", 0.85))
        sym_conf = float(confidence.get("symbol_only", 0.6))

        anchors: list[AnchorCandidate] = []
        emitted_slugs: set[str] = set()

        # Submodule anchors (the strongest signal).
        for sub in sorted(sub_dirs):
            slug = slugify(sub)
            if not slug or is_noise(slug) or slug in emitted_slugs:
                continue
            paths = tuple(
                sorted(
                    t for t in tracked
                    if t.startswith(f"{prefix}{sub}/") and not _excluded(t)
                ),
            )
            if not paths:
                continue
            rationale = (
                f"python-library submodule {sub!r} "
                f"({'declared in __init__' if sub in declared_subs else 'directory'})"
            )
            anchors.append(
                AnchorCandidate(
                    name=slug,
                    paths=paths,
                    source=self.name,
                    confidence_self=sub_conf,
                    rationale=rationale,
                ),
            )
            emitted_slugs.add(slug)

        # Top-level single-file module anchors (e.g. fastapi/applications.py).
        # Only include those re-exported in __init__.py to avoid
        # spamming the map with internals.
        for name, file_path in sorted(sub_files.items()):
            if name in declared_subs:
                slug = slugify(name)
                if not slug or is_noise(slug) or slug in emitted_slugs:
                    continue
                anchors.append(
                    AnchorCandidate(
                        name=slug,
                        paths=(file_path,),
                        source=self.name,
                        confidence_self=sub_conf,
                        rationale=(
                            f"python-library single-file module {name!r}"
                        ),
                    ),
                )
                emitted_slugs.add(slug)

        # Symbol-only anchors — names listed in __all__ that don't
        # correspond to a submodule. Low confidence; Stage 2 may merge.
        residual_symbols = declared_symbols - {
            s.replace("-", "_") for s in emitted_slugs
        }
        for sym in sorted(residual_symbols):
            slug = slugify(sym)
            if not slug or is_noise(slug) or slug in emitted_slugs:
                continue
            anchors.append(
                AnchorCandidate(
                    name=slug,
                    paths=(f"{root_pkg}/__init__.py",),
                    source=self.name,
                    confidence_self=sym_conf,
                    rationale=f"python-library __all__ export {sym!r}",
                ),
            )
            emitted_slugs.add(slug)

        return anchors


__all__ = ["PythonLibraryExtractor"]
