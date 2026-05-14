"""MVC controller extractor — Rails (Phase 3b proof-of-concept).

Per the mvc-controller-extractor skill (faultlines-app repo,
``.claude/skills/mvc-controller-extractor/SKILL.md``). The "controller"
pattern recurs across Rails / Django CBV / Laravel / Phoenix / Spring /
ASP.NET; each ships its own filename + binding mechanism but the
output shape is identical: controller class → set of actions = flow
candidates.

Phase 3b PoC ships only Rails. Other frameworks follow the same shape
(one parser strategy each, ≤80 lines).

What it emits:
    Signal(kind="controller-action", source="mvc-controller-extractor",
      payload={
        "framework": "rails",
        "controller_file": "app/controllers/billing_controller.rb",
        "controller_name": "BillingController",
        "action": "create",
        "http_method": None,    # Rails binds in routes.rb, not the file
        "path_hint": None,
      })
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.signals import Signal

_CONTROLLER_GLOB = "app/controllers/**/*_controller.rb"

# Ruby class declaration: optionally inherits from another class.
_CLASS_DECL_RE = re.compile(
    r"^\s*class\s+([A-Z][A-Za-z0-9_:]*)\s*(?:<\s*([A-Z][A-Za-z0-9_:]*))?\s*$",
    re.MULTILINE,
)
# Action = public def. We approximate by parsing every `def name(...)`
# and discarding ones inside a `private` / `protected` block.
_DEF_RE = re.compile(
    r"^(\s*)def\s+([a-z_][a-zA-Z0-9_!?=]*)", re.MULTILINE,
)
_PRIVATE_RE = re.compile(r"^\s*(private|protected)\s*$", re.MULTILINE)

# Base classes whose actions we never count (abstract framework bases).
_BASE_CLASSES_TO_IGNORE = frozenset({
    "ApplicationController", "ActionController::API",
    "ActionController::Base", "AdminController",
})

# Skip controllers whose class name matches these (admin-only / synthetic).
_SKIP_CONTROLLER_NAMES = frozenset({
    "ApplicationController",
})


@dataclass(frozen=True, slots=True, kw_only=True)
class RailsControllerAction:
    """One parsed controller action (= flow candidate)."""

    controller_file: str    # repo-relative
    controller_name: str    # "BillingController"
    parent_class: str | None
    action: str             # "create", "show", "index", ...


def is_rails_repo(repo_root: Path) -> bool:
    """Loose Rails detection: Gemfile mentions 'rails' OR
    config/routes.rb exists OR app/controllers/ exists."""
    gemfile = repo_root / "Gemfile"
    if gemfile.exists():
        try:
            text = gemfile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "rails" in text.lower():
            return True
    if (repo_root / "config" / "routes.rb").exists():
        return True
    if (repo_root / "app" / "controllers").is_dir():
        return True
    return False


def collect_rails_controllers(repo_root: Path) -> list[RailsControllerAction]:
    """Walk app/controllers and parse each *_controller.rb."""
    if not is_rails_repo(repo_root):
        return []
    out: list[RailsControllerAction] = []
    for path in repo_root.glob(_CONTROLLER_GLOB):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(repo_root)).replace("\\", "/")
        for cls_name, parent in _parse_class_decls(text):
            if cls_name in _SKIP_CONTROLLER_NAMES:
                continue
            if parent in _BASE_CLASSES_TO_IGNORE and cls_name == parent:
                continue
            for action in _public_actions(text):
                out.append(RailsControllerAction(
                    controller_file=rel,
                    controller_name=cls_name,
                    parent_class=parent,
                    action=action,
                ))
            # First class declaration in a file is canonical; ignore others
            # (Ruby allows reopening; the first match is what defines the
            # actions we want).
            break
    return out


def _parse_class_decls(text: str) -> list[tuple[str, str | None]]:
    return [(m.group(1), m.group(2)) for m in _CLASS_DECL_RE.finditer(text)]


def _public_actions(text: str) -> list[str]:
    """Return public def names in declaration order.

    Heuristic: anything after a top-level `private` or `protected`
    line (no leading-whitespace indent) is private.
    """
    private_starts = [m.start() for m in _PRIVATE_RE.finditer(text)]
    private_cutoff = private_starts[0] if private_starts else len(text)
    out: list[str] = []
    for m in _DEF_RE.finditer(text):
        if m.start() >= private_cutoff:
            break
        name = m.group(2)
        # Skip Rails internal callbacks
        if name.startswith("_") or name in {"initialize", "to_s", "inspect"}:
            continue
        out.append(name)
    return out


# ── Extractor wrapper (Protocol-conforming) ─────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class RailsControllerExtractor:
    """Phase 3b extractor for Rails controller actions."""

    name: str = "mvc-controller-extractor:rails"

    def applicable(self, repo_root: Path) -> bool:
        return is_rails_repo(repo_root)

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files
        actions = collect_rails_controllers(repo_root)
        return [
            Signal(
                kind="controller-action",
                source=self.name,
                payload={
                    "framework": "rails",
                    "controller_file": a.controller_file,
                    "controller_name": a.controller_name,
                    "parent_class": a.parent_class,
                    "action": a.action,
                    "http_method": None,
                    "path_hint": None,
                },
            )
            for a in actions
        ]


__all__ = [
    "RailsControllerAction",
    "RailsControllerExtractor",
    "collect_rails_controllers",
    "is_rails_repo",
]
