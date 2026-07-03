"""G2 — CI import lint for the Framework Knowledge Layer (StackProfile spec).

Machine-enforced isolation contract for ``faultline/pipeline_v2/profiles/``:

* A **concrete profile module** (``next_app_router.py``, ``default.py``,
  any future ``fastapi_family.py`` — every non-underscore module except
  ``base.py``) may import ONLY:
    - the standard library,
    - ``faultline.pipeline_v2.profiles.base``,
    - private profile helpers ``faultline.pipeline_v2.profiles._<x>``
      (the ``_util`` tier: ``_splitter``, ``_attribution``, ...),
    - ``faultline.pipeline_v2.extractors[.*]`` (extractor REUSE is the
      sanctioned path — profiles fold extractors, never rewrite them),
    - ``faultline.pipeline_v2.stage_0_intake`` **under TYPE_CHECKING
      only** (the ``ScanContext`` / ``Workspace`` type names).
  In particular a concrete profile may NEVER import another concrete
  profile (cross-profile coupling) nor any shared pipeline stage at
  runtime.

* A **helper module** (``_splitter.py`` etc., and ``base.py``) may import
  trunk utilities but never a concrete profile — helpers are shared by
  all profiles, so a profile import there would smuggle cross-profile
  coupling in through the back door.

* ``_registry.py`` is selection infrastructure: it may additionally
  import ``default`` (the injected null-object) and lazily import
  concrete profiles inside ``_load_default_profiles`` (string-based
  ``__import__`` — invisible to this AST lint by design: the built-in
  fallback list is the ONE sanctioned registration point).
  ``__init__.py`` is the public facade and may re-export anything in
  the package.

* Nothing OUTSIDE ``profiles/`` may import a concrete profile module —
  the pipeline reaches profiles exclusively through the registry (DIP).

Pure AST analysis — no imports are executed.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PROFILES_PKG = "faultline.pipeline_v2.profiles"
REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = REPO_ROOT / "faultline" / "pipeline_v2" / "profiles"
PIPELINE_V2_DIR = REPO_ROOT / "faultline" / "pipeline_v2"

_STDLIB = set(sys.stdlib_module_names) | {"__future__"}

# Infrastructure modules exempt from the concrete-profile rules.
_INFRA = {"__init__", "_registry"}


def _profile_modules() -> list[Path]:
    return sorted(PROFILES_DIR.glob("*.py"))


def _concrete_profile_names() -> set[str]:
    """Stems of concrete profile modules (the rule's protected class)."""
    return {
        p.stem
        for p in _profile_modules()
        if not p.stem.startswith("_") and p.stem not in {"base"}
    }


def _collect_imports(path: Path) -> list[tuple[str, bool]]:
    """Return ``(dotted_module, is_type_checking_only)`` for every import.

    ``from X import y`` records ``X``; relative imports are resolved
    against the profiles package. TYPE_CHECKING-only imports are those
    nested (at any depth) inside an ``if TYPE_CHECKING:`` block.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _is_type_checking_test(test: ast.expr) -> bool:
        return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )

    out: list[tuple[str, bool]] = []

    def _walk(nodes: list[ast.stmt], type_checking: bool) -> None:
        for node in nodes:
            if isinstance(node, ast.Import):
                out.extend((alias.name, type_checking) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative → resolve against the package
                    base = PROFILES_PKG.split(".")
                    if node.level > 1:
                        base = base[: -(node.level - 1)]
                    mod = ".".join(base + ([node.module] if node.module else []))
                else:
                    mod = node.module or ""
                out.append((mod, type_checking))
            elif isinstance(node, ast.If):
                nested_tc = type_checking or _is_type_checking_test(node.test)
                _walk(node.body, nested_tc)
                _walk(node.orelse, type_checking)
            elif isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                 ast.With, ast.Try, ast.For, ast.While),
            ):
                for attr in ("body", "orelse", "finalbody", "handlers"):
                    children = getattr(node, attr, [])
                    for child in children:
                        if isinstance(child, ast.ExceptHandler):
                            _walk(child.body, type_checking)
                        elif isinstance(child, ast.stmt):
                            _walk([child], type_checking)

    _walk(tree.body, False)
    return out


def _is_allowed_for_concrete(mod: str, type_checking: bool) -> bool:
    top = mod.split(".")[0]
    if top in _STDLIB:
        return True
    if mod == f"{PROFILES_PKG}.base" or mod == PROFILES_PKG:
        return True
    if mod.startswith(f"{PROFILES_PKG}._"):
        return True  # private helper tier (_splitter, _attribution, ...)
    if mod == "faultline.pipeline_v2.extractors" or mod.startswith(
        "faultline.pipeline_v2.extractors."
    ):
        return True  # sanctioned extractor reuse
    if type_checking and mod == "faultline.pipeline_v2.stage_0_intake":
        return True  # ScanContext / Workspace type names only
    return False


def test_concrete_profiles_import_only_base_helpers_and_extractors() -> None:
    concrete = _concrete_profile_names()
    violations: list[str] = []
    for path in _profile_modules():
        if path.stem not in concrete:
            continue
        for mod, type_checking in _collect_imports(path):
            if not _is_allowed_for_concrete(mod, type_checking):
                violations.append(f"{path.name}: imports {mod!r}")
    assert not violations, (
        "G2 violation — concrete profile modules may import only stdlib, "
        "profiles.base, profiles._* helpers, and pipeline_v2.extractors.*:\n"
        + "\n".join(violations)
    )


def test_no_cross_profile_imports() -> None:
    """No profile module (concrete OR helper) imports a concrete profile."""
    concrete = _concrete_profile_names()
    forbidden = {f"{PROFILES_PKG}.{stem}" for stem in concrete}
    # _registry may import `default` — the injected null-object.
    registry_allowed = {f"{PROFILES_PKG}.default"}
    violations: list[str] = []
    for path in _profile_modules():
        if path.stem == "__init__":
            continue  # public facade — re-exports are its job
        for mod, _tc in _collect_imports(path):
            if mod in forbidden:
                if path.stem == "_registry" and mod in registry_allowed:
                    continue
                if mod == f"{PROFILES_PKG}.{path.stem}":
                    continue  # self (impossible, but harmless)
                violations.append(f"{path.name}: imports {mod!r}")
    assert not violations, (
        "G2 violation — cross-profile import detected:\n" + "\n".join(violations)
    )


def test_trunk_never_imports_a_concrete_profile() -> None:
    """Outside profiles/, concrete profiles are reachable ONLY via registry."""
    concrete = _concrete_profile_names()
    needles = [f"{PROFILES_PKG}.{stem}" for stem in concrete]
    violations: list[str] = []
    for path in sorted((REPO_ROOT / "faultline").rglob("*.py")):
        if PROFILES_DIR in path.parents or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in needles:
            if needle in text:
                violations.append(f"{path.relative_to(REPO_ROOT)}: references {needle}")
    assert not violations, (
        "G2 violation — trunk references a concrete profile module by name "
        "(use the registry):\n" + "\n".join(violations)
    )
