"""Package-anchor extractor — universal across stacks.

Per the package-anchor-extractor skill (faultlines-app repo,
``.claude/skills/package-anchor-extractor/SKILL.md``). Reads dependency
manifests for every supported language and emits "expected feature"
signals: Stripe → Billing, NextAuth → Authentication, Resend → Email,
Inngest → Background Jobs.

The mapping table lives in
``faultlines-app/eval/stacks/_dep-anchors.yaml`` so it's editable
without changing Python. The extractor loads it once at construction
time.

Severity values:
    must   — feature MUST exist if dep present (recall miss otherwise)
    should — feature should exist (mild downgrade if missing)
    may    — informational; no recall expectation
    ignore — pure infra; suppress signal entirely
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from faultline.signals import Signal

# Default location for the YAML mapping. Overridable via env var
# ``FAULTLINE_DEP_ANCHORS_PATH`` (used by tests + custom corpora).
_DEFAULT_ANCHORS_PATH = Path(
    os.environ.get(
        "FAULTLINE_DEP_ANCHORS_PATH",
        "/Users/pkuzina/workspace/faultlines-app/eval/stacks/_dep-anchors.yaml",
    )
)

# Severity ranks for ordering / aggregation
_SEVERITY_RANK = {"must": 3, "should": 2, "may": 1, "ignore": 0}


@dataclass(frozen=True, slots=True, kw_only=True)
class DepAnchor:
    """One detected dep → expected-feature anchor."""

    dep_name: str
    feature_category: str       # YAML key; e.g. "billing"
    severity: str               # "must" | "should" | "may"
    manifest: str               # repo-relative manifest path


# ── Manifest parsers ─────────────────────────────────────────────────


def _read_package_json(path: Path) -> set[str]:
    """Return the set of dep names from package.json (deps + peerDeps).

    devDependencies are deliberately excluded — they're plumbing.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return set()
    out: set[str] = set()
    for key in ("dependencies", "peerDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            out.update(section.keys())
    return out


def _read_pyproject_toml(path: Path) -> set[str]:
    """Best-effort: read [tool.poetry.dependencies] + [project.dependencies]
    sections. Pure-stdlib (no toml dep) — uses simple regex; misses
    exotic shapes but covers ~95% of real-world manifests.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    out: set[str] = set()
    # PEP 621 — match any quoted string starting with a package-shaped
    # token followed by a version specifier or list/string boundary.
    for m in re.finditer(r'"([a-zA-Z][a-zA-Z0-9_\-.]+)(?=[<>=!~ ,\[\"\]])', text):
        token = m.group(1)
        if "/" in token or ":" in token:
            continue
        out.add(token)
    # Poetry block:  foo = "^1.0"
    in_poetry_deps = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_poetry_deps = stripped in (
                "[tool.poetry.dependencies]",
                "[tool.poetry.dev-dependencies]",
                "[tool.poetry.group.dev.dependencies]",
            )
            continue
        if in_poetry_deps:
            m = re.match(r"^([a-zA-Z][\w\-]*)\s*=", stripped)
            if m:
                out.add(m.group(1))
    return out


def _read_requirements_txt(path: Path) -> set[str]:
    out: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # Take everything before the first version specifier or extras
            name = re.split(r"[<>=!~\[\s]", line, 1)[0]
            if name:
                out.add(name)
    except OSError:
        pass
    return out


def _read_gemfile(path: Path) -> set[str]:
    out: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for m in re.finditer(r"""^\s*gem\s+['"]([\w_\-/]+)['"]""", text, re.MULTILINE):
        out.add(m.group(1))
    return out


def _read_cargo_toml(path: Path) -> set[str]:
    out: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    in_deps_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_deps_section = stripped in (
                "[dependencies]", "[dev-dependencies]",
                "[build-dependencies]",
            )
            continue
        if in_deps_section:
            m = re.match(r"^([a-zA-Z][\w\-]*)\s*=", stripped)
            if m:
                out.add(m.group(1))
    return out


def _read_composer_json(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return set()
    require = data.get("require") or {}
    if isinstance(require, dict):
        return set(require.keys())
    return set()


def _read_go_mod(path: Path) -> set[str]:
    out: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    in_require = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "require (":
            in_require = True
            continue
        if stripped == ")":
            in_require = False
            continue
        if in_require:
            parts = stripped.split()
            if parts:
                out.add(parts[0])
        elif stripped.startswith("require "):
            parts = stripped.split()
            if len(parts) >= 2:
                out.add(parts[1])
    return out


_MANIFEST_PARSERS = {
    "package.json": _read_package_json,
    "pyproject.toml": _read_pyproject_toml,
    "requirements.txt": _read_requirements_txt,
    "Gemfile": _read_gemfile,
    "Cargo.toml": _read_cargo_toml,
    "composer.json": _read_composer_json,
    "go.mod": _read_go_mod,
}


# ── YAML loader ──────────────────────────────────────────────────────


def load_anchor_map(yaml_path: Path = _DEFAULT_ANCHORS_PATH) -> dict[str, dict[str, str]]:
    """Return ``{dep_name: {"category": str, "severity": str}}`` map.

    Categories whose YAML lists deps under ``ignore:`` are dropped
    from the output entirely (those deps emit no signal). When the
    same dep appears in multiple categories, the highest-severity
    wins (must > should > may).
    """
    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            "PyYAML required for package-anchor-extractor; "
            "install with `pip install pyyaml`"
        ) from exc

    if not yaml_path.exists():
        return {}
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    out: dict[str, dict[str, str]] = {}
    ignored: set[str] = set()
    for category, by_severity in raw.items():
        if not isinstance(by_severity, dict):
            continue
        for severity, deps in by_severity.items():
            if severity == "ignore":
                ignored.update(deps or [])
                continue
            if severity not in _SEVERITY_RANK:
                continue
            for dep in deps or []:
                if not isinstance(dep, str):
                    continue
                existing = out.get(dep)
                if existing is None or _SEVERITY_RANK[severity] > _SEVERITY_RANK[existing["severity"]]:
                    out[dep] = {"category": category, "severity": severity}
    # Strip ignored deps from any earlier mapping
    for dep in ignored:
        out.pop(dep, None)
    return out


# ── Walker ───────────────────────────────────────────────────────────


def find_manifests(repo_root: Path) -> list[Path]:
    """Locate every supported manifest in repo + workspace packages."""
    out: list[Path] = []
    for filename in _MANIFEST_PARSERS:
        for path in repo_root.rglob(filename):
            # Skip noise dirs
            rel_parts = path.relative_to(repo_root).parts
            if any(p in {"node_modules", "target", "dist", "build",
                          ".next", ".turbo", "vendor", "venv", ".venv"}
                   for p in rel_parts):
                continue
            out.append(path)
    return out


def collect_anchors(
    repo_root: Path,
    *,
    anchor_map: dict[str, dict[str, str]] | None = None,
) -> list[DepAnchor]:
    """Walk every manifest, parse deps, match against anchor_map."""
    amap = anchor_map if anchor_map is not None else load_anchor_map()
    if not amap:
        return []
    out: list[DepAnchor] = []
    seen: set[tuple[str, str]] = set()  # (dep_name, manifest_path) dedup
    for manifest in find_manifests(repo_root):
        parser = _MANIFEST_PARSERS.get(manifest.name)
        if parser is None:
            continue
        deps = parser(manifest)
        rel = str(manifest.relative_to(repo_root)).replace("\\", "/")
        for dep in deps:
            spec = amap.get(dep)
            if spec is None:
                continue
            key = (dep, rel)
            if key in seen:
                continue
            seen.add(key)
            out.append(DepAnchor(
                dep_name=dep,
                feature_category=spec["category"],
                severity=spec["severity"],
                manifest=rel,
            ))
    return out


# ── Extractor wrapper ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class PackageAnchorExtractor:
    """Universal package-deps → expected-feature signals."""

    name: str = "package-anchor-extractor"
    anchor_map: dict[str, dict[str, str]] | None = None

    def applicable(self, repo_root: Path) -> bool:
        # Always applicable — every modern repo has at least one
        # supported manifest.
        return any(
            (repo_root / m).exists() or any(repo_root.rglob(m))
            for m in _MANIFEST_PARSERS
        )

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files
        anchors = collect_anchors(repo_root, anchor_map=self.anchor_map)
        return [
            Signal(
                kind="expected-feature",
                source=self.name,
                payload={
                    "feature_category": a.feature_category,
                    "evidence": (f"dep:{a.dep_name}",),
                    "manifest": a.manifest,
                    "severity": a.severity,
                },
            )
            for a in anchors
        ]


__all__ = [
    "DepAnchor",
    "PackageAnchorExtractor",
    "collect_anchors",
    "find_manifests",
    "load_anchor_map",
]
