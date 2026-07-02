"""Stage 0.5 — Stack Auditor.

One LLM call (Haiku, structured JSON output) inserted between Stage 0
(deterministic heuristic stack detection) and Stage 1 (extractors).
The auditor's job is to *classify* the repository's stack with enough
context window to distinguish:

  - library vs server (axios is a JS client lib, NOT express)
  - app vs framework (fastapi the repo IS the framework, not an app
    that uses it)
  - polyglot monorepos (infisical = js + python)
  - workspace flavours (meilisearch = Cargo workspace, not generic Rust)

Stage 0 is a small set of "if package.json depends on next and an
app/ dir exists then it's next-app-router" rules. That gets most SaaS
repos right but mislabels OSS libraries and polyglot monorepos. The
auditor reads structural signals + git activity (NOT README, ever)
and writes back to ``ScanContext`` so Stage 1 extractors can consult
``ctx.audited_stack`` and ``ctx.extractor_hints``.

Hard rule — README is FORBIDDEN
================================

Per ``CLAUDE.md`` and ``memory/rule-no-readme.md``: the auditor must
NEVER read README.md or any in-repo prose doc. The input builder
strips ``.md`` paths and only walks whitelisted structural sources:

  - File-system paths from ``git log -n 50 --name-only`` (skipping .md)
  - Manifest excerpts — names ONLY, no descriptions, no prose fields
  - Recent commit subjects from ``git log --pretty=%s -n 100``
  - Stage 0's own heuristic verdict (stack + signals)

A unit test asserts the built context contains zero ``.md`` paths.

Fallback / cost ceiling
=======================

If the Anthropic client is unavailable or the call fails, the auditor
returns a verdict that mirrors Stage 0's heuristic with ``confidence=1.0``
(Stage 0 is reliable for the cases it handles — the auditor is purely
additive). A $0.01 per-call cap defends against runaway responses;
exceeding it triggers fallback with ``auditor_fallback_used=True``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from faultline.llm.cost import CostTracker, deterministic_params
from faultline.pipeline_v2.llm_health import LlmHealth

if TYPE_CHECKING:
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)
from faultline.llm.model_gateway import resolve_model as gateway_model


# ── Tunables ────────────────────────────────────────────────────────────────

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 800
# Per-call defensive cap. Expected ~$0.002 for small repos and
# ~$0.005-$0.02 for large monorepos (trigger.dev measured $0.018 in
# A3 validation — many workspace manifests inflate the input token
# count). $0.05 keeps the auditor well under 2% of a typical $0.30
# scan even on the largest monorepo while still catching runaway
# malformed responses.
COST_CAP_USD = 0.05

# ── Stage 0.5 content-keyed LLM cache (determinism, 2026-07-02) ─────────────
# The auditor prompt is deterministic for an identical repo state, but
# Anthropic temp=0 is NOT bit-deterministic — an uncached auditor re-rolled
# its prose ``extractor_hints`` every run, and those hints are embedded in
# the stage-8 analyst payload (= its llm-cache key), silently re-rolling all
# of Layer 2 on an unchanged repo. Cache the RAW response text keyed on
# (version + model + system + user); only successful parses are stored.
_AUDITOR_CACHE_VERSION = "v1"


def _auditor_cache_enabled() -> bool:
    return os.environ.get("FAULTLINE_STAGE_0_5_CACHE", "1") != "0"


def _auditor_cache_key(model: str, system: str, user: str) -> str:
    import hashlib
    payload = json.dumps(
        {"v": _AUDITOR_CACHE_VERSION, "model": model,
         "system": system, "user": user},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _auditor_cache_get(cache: Any | None, key: str) -> str | None:
    # EXPLICIT backend only — never the global one (a unit test with a fake
    # client must not read/write the developer's real ~/.faultline).
    if cache is None or not _auditor_cache_enabled():
        return None
    try:
        from faultline.cache.backend import CacheKind
        cached = cache.get(CacheKind.LLM_AUDITOR.value, key)
    except Exception:  # noqa: BLE001 — cache is best-effort, never fatal
        return None
    if isinstance(cached, dict) and isinstance(cached.get("text"), str):
        return cached["text"]
    return None


def _auditor_cache_put(cache: Any | None, key: str, text: str) -> None:
    if cache is None or not _auditor_cache_enabled():
        return
    try:
        from faultline.cache.backend import CacheKind
        cache.set(
            CacheKind.LLM_AUDITOR.value, key,
            {"v": _AUDITOR_CACHE_VERSION, "text": text},
        )
    except Exception:  # noqa: BLE001
        pass
MAX_RECENT_PATHS = 50
MAX_RECENT_COMMITS = 100
MIN_CONFIDENCE_TO_APPLY = 0.5

# Whitelisted manifest filenames at the repo root. NEVER includes any
# .md / .rst / .txt prose doc. Order is informational only; we read all
# that exist.
_MANIFEST_FILES = (
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pyproject.toml",
    "requirements.txt",
    "Gemfile",
    "composer.json",
    "mix.exs",
    "build.gradle",
    "pom.xml",
)

# Directory prefixes excluded from the recent-paths sample. We never
# want vendored / built / cached output dirs in the prompt.
_EXCLUDED_PATH_PREFIXES = (
    "node_modules/",
    "vendor/",
    "target/",
    "dist/",
    "build/",
    "out/",
    ".next/",
    ".turbo/",
    "__pycache__/",
    ".venv/",
    "venv/",
    ".git/",
)

# Path suffixes excluded — primarily prose docs, which are forbidden
# as a detection signal per ``rule-no-readme``.
_EXCLUDED_PATH_SUFFIXES = (
    ".md",
    ".markdown",
    ".rst",
    ".adoc",
    ".txt",  # READMEs sometimes live as plain .txt; treat as prose.
)


# ── Public types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuditorVerdict:
    """The structured verdict the auditor returns to the orchestrator.

    Attributes:
        primary_stack: kebab-case stack tag; may differ from
            ``ctx.stack`` (Stage 0's heuristic guess).
        secondary_stacks: additional stack tags for polyglot repos /
            monorepos with mixed workspaces. Empty tuple for simple
            single-stack repos.
        confidence: 0..1 self-rated confidence. Values below
            :data:`MIN_CONFIDENCE_TO_APPLY` trigger orchestrator
            fallback to Stage 0's heuristic.
        extractor_hints: short directives consumed by Stage 1
            extractors when they ship in A4. Example:
            ``"go-http-router via chi.Router{} in *.go"``.
        reasoning: one-paragraph natural-language explanation for
            telemetry / debugging. NEVER consumed by downstream stages.
        cost_usd: actual USD cost of the underlying Haiku call.
        fallback_used: ``True`` when the orchestrator should treat
            this verdict as a Stage-0 echo (because the LLM was
            unavailable, errored, or exceeded the cost cap).
        corrections: list of structural overrides applied by Sprint
            S3.1's :func:`correct_auditor_verdict`. Each entry is
            ``{"original": str, "corrected": str, "reason": str}``.
            Empty tuple when no correction fired. Surfaced in
            ``scan_meta.auditor_corrections`` for telemetry.
    """

    primary_stack: str
    secondary_stacks: tuple[str, ...] = ()
    confidence: float = 1.0
    extractor_hints: tuple[str, ...] = ()
    reasoning: str = ""
    cost_usd: float = 0.0
    fallback_used: bool = False
    corrections: tuple[dict, ...] = ()


# ── Anthropic client protocol (for tests / IoC) ─────────────────────────────


def _default_client_factory() -> Any | None:  # pragma: no cover - IO
    """Lazy Anthropic client builder. Returns ``None`` when SDK or
    API key are absent — the orchestrator then falls back to the
    Stage 0 heuristic without erroring.
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


# ── Input builder (deterministic, README-FORBIDDEN) ─────────────────────────


def _is_md_path(path: str) -> bool:
    """README guard — any path matching a prose-doc suffix is rejected.

    Centralised so the test that asserts ``.md not in built context``
    has one helper to monkey-check.
    """
    lower = path.lower()
    return any(lower.endswith(suffix) for suffix in _EXCLUDED_PATH_SUFFIXES)


def _is_excluded_path(path: str) -> bool:
    """Combined exclusion: prose suffixes + vendored/built dirs."""
    if _is_md_path(path):
        return True
    norm = path.replace("\\", "/")
    return any(norm.startswith(prefix) or f"/{prefix}" in norm
               for prefix in _EXCLUDED_PATH_PREFIXES)


def recent_modified_paths(
    ctx: "ScanContext",
    *,
    limit: int = MAX_RECENT_PATHS,
) -> list[str]:
    """Return the top ``limit`` most-recently-modified tracked files
    (newest commit first), with prose docs and vendored dirs stripped.

    Falls back to ``ctx.tracked_files[:limit]`` when ``ctx.commits``
    is empty (fixture repos without git history).
    """
    seen: set[str] = set()
    out: list[str] = []
    for commit in ctx.commits:
        # ``Commit.files_changed`` is the canonical attribute name on
        # the legacy ``Commit`` dataclass.
        files = getattr(commit, "files_changed", None) or getattr(
            commit, "files", None,
        ) or []
        for f in files:
            if not isinstance(f, str):
                continue
            if f in seen:
                continue
            if _is_excluded_path(f):
                continue
            seen.add(f)
            out.append(f)
            if len(out) >= limit:
                return out
    # Fallback when git history is empty (e.g. fixture repos): pull
    # from tracked_files in directory-order, still stripping prose.
    if not out:
        for f in ctx.tracked_files:
            if _is_excluded_path(f):
                continue
            out.append(f)
            if len(out) >= limit:
                break
    return out


def recent_commit_subjects(
    ctx: "ScanContext",
    *,
    limit: int = MAX_RECENT_COMMITS,
) -> list[str]:
    """Return up to ``limit`` recent commit subject lines.

    Commit messages are NOT prose docs — they describe code change
    intent and are a legitimate structural signal. The README rule
    targets in-repo authored prose (README.md / docs/*.md / etc.),
    not commit metadata.
    """
    out: list[str] = []
    for commit in ctx.commits[:limit]:
        subject = (getattr(commit, "message", "") or "").splitlines()
        if subject:
            line = subject[0].strip()
            if line:
                out.append(line)
    return out


def _parse_package_json(text: str) -> dict[str, Any]:
    """Extract dependency NAMES + workspaces + script NAMES only.

    NEVER returns ``description`` / ``keywords`` / ``author`` / any
    prose field.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    # Dependency name lists.
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = data.get(key)
        if isinstance(block, dict):
            out[key] = sorted(block.keys())
    # Workspaces — surface presence + members (whether array or object).
    ws = data.get("workspaces")
    if isinstance(ws, list):
        out["workspaces"] = [w for w in ws if isinstance(w, str)]
    elif isinstance(ws, dict):
        packages = ws.get("packages")
        if isinstance(packages, list):
            out["workspaces"] = [w for w in packages if isinstance(w, str)]
    # Script names (NOT their command strings — those can be prose).
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        out["scripts"] = sorted(scripts.keys())
    # Top-level "main" / "types" / "bin" — single-key markers that
    # disambiguate library-vs-app without leaking prose.
    for marker in ("main", "module", "types", "bin", "exports", "type"):
        if marker in data:
            out[f"has_{marker}"] = True
    return out


_CARGO_SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$", re.MULTILINE)
_CARGO_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*=", re.MULTILINE)


def _parse_cargo_toml(text: str) -> dict[str, Any]:
    """Best-effort Cargo.toml parse without a TOML dep.

    Surfaces:
      - ``[workspace] members``  — list of strings
      - ``[dependencies]``       — dep names only
      - presence of ``[lib]`` / ``[[bin]]`` (library vs binary signal)
    """
    out: dict[str, Any] = {}
    sections: dict[str, str] = {}
    matches = list(_CARGO_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        section = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[section] = text[start:end]

    if "lib" in sections:
        out["has_lib"] = True
    if any(s == "bin" or s.startswith("[bin]") or s == "[[bin]]"
           for s in sections):
        out["has_bin"] = True
    # ``[[bin]]`` shows up as a section literally; double-check.
    if re.search(r"^\[\[bin\]\]", text, re.MULTILINE):
        out["has_bin"] = True

    if "workspace" in sections:
        ws_body = sections["workspace"]
        # naive parse of `members = ["a", "b"]`
        mem = re.search(
            r'members\s*=\s*\[([^\]]*)\]', ws_body, re.DOTALL,
        )
        if mem:
            members = re.findall(r'"([^"]+)"', mem.group(1))
            out["workspace_members"] = members

    if "dependencies" in sections:
        out["dependencies"] = sorted(set(
            _CARGO_KEY_RE.findall(sections["dependencies"]),
        ))
    if "dev-dependencies" in sections:
        out["dev_dependencies"] = sorted(set(
            _CARGO_KEY_RE.findall(sections["dev-dependencies"]),
        ))
    return out


def _parse_go_mod(text: str) -> dict[str, Any]:
    """Surface module path + top-level require module names."""
    out: dict[str, Any] = {}
    mod = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
    if mod:
        out["module"] = mod.group(1)
    # require (\n  foo/bar v1.0.0\n  baz vX.Y.Z\n)
    block = re.search(r"require\s*\(([^)]*)\)", text, re.DOTALL)
    requires: list[str] = []
    if block:
        for line in block.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            parts = line.split()
            if parts:
                requires.append(parts[0])
    else:
        # single-line `require foo/bar v1.0.0`
        for m in re.finditer(r"^require\s+(\S+)\s+\S+", text, re.MULTILINE):
            requires.append(m.group(1))
    if requires:
        out["require"] = sorted(set(requires))
    return out


_PYPROJECT_SECTION_RE = re.compile(
    r"^\[([^\]]+)\]\s*$", re.MULTILINE,
)


def _parse_pyproject_toml(text: str) -> dict[str, Any]:
    """Naive pyproject parse — extract dependency NAMES and project
    metadata flags that disambiguate library vs application.

    We deliberately DROP ``description`` / ``readme`` references —
    those can leak prose into the prompt.
    """
    out: dict[str, Any] = {}
    sections: dict[str, str] = {}
    matches = list(_PYPROJECT_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        section = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[section] = text[start:end]

    project = sections.get("project", "")
    if project:
        name_m = re.search(r'^name\s*=\s*"([^"]+)"', project, re.MULTILINE)
        if name_m:
            out["project_name"] = name_m.group(1)
        # PEP 621 dependencies = ["foo>=1", "bar"]
        deps_block = re.search(
            r'dependencies\s*=\s*\[([^\]]*)\]', project, re.DOTALL,
        )
        if deps_block:
            names: list[str] = []
            for raw in re.findall(r'"([^"]+)"', deps_block.group(1)):
                # Strip version specifiers / extras to leave just the name.
                pkg = re.split(r"[<>=!~;\[\s]", raw, maxsplit=1)[0].strip()
                if pkg:
                    names.append(pkg)
            out["project_dependencies"] = sorted(set(names))

    scripts = sections.get("project.scripts", "")
    if scripts:
        script_names = re.findall(
            r'^([A-Za-z0-9_-]+)\s*=', scripts, re.MULTILINE,
        )
        if script_names:
            out["project_scripts"] = sorted(set(script_names))

    poetry_deps = sections.get("tool.poetry.dependencies", "")
    if poetry_deps:
        names = re.findall(
            r'^([A-Za-z0-9_.-]+)\s*=', poetry_deps, re.MULTILINE,
        )
        out["poetry_dependencies"] = sorted(
            {n for n in names if n.lower() != "python"},
        )

    setup_packages = sections.get("tool.setuptools.packages", "") or \
        sections.get("tool.setuptools.packages.find", "")
    if setup_packages:
        out["has_setuptools_packages"] = True

    return out


def _parse_requirements_txt(text: str) -> dict[str, Any]:
    names: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("-"):
            continue
        pkg = re.split(r"[<>=!~;\[\s]", s, maxsplit=1)[0].strip()
        if pkg:
            names.append(pkg)
    return {"requirements": sorted(set(names))} if names else {}


def _parse_gemfile(text: str) -> dict[str, Any]:
    gems = re.findall(r"^\s*gem\s+['\"]([^'\"]+)['\"]", text, re.MULTILINE)
    return {"gems": sorted(set(gems))} if gems else {}


def _parse_composer_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("require", "require-dev"):
        block = data.get(key)
        if isinstance(block, dict):
            out[key] = sorted(block.keys())
    return out


def _parse_generic_manifest(name: str, text: str) -> dict[str, Any]:
    """Stub for manifests we don't parse fully — just surface presence."""
    return {"present": True, "size_bytes": len(text)}


_MANIFEST_PARSERS: dict[str, Callable[[str], dict[str, Any]]] = {
    "package.json": _parse_package_json,
    "Cargo.toml": _parse_cargo_toml,
    "go.mod": _parse_go_mod,
    "pyproject.toml": _parse_pyproject_toml,
    "requirements.txt": _parse_requirements_txt,
    "Gemfile": _parse_gemfile,
    "composer.json": _parse_composer_json,
}


def read_manifest_excerpts(ctx: "ScanContext") -> dict[str, Any]:
    """Read whitelisted manifests at the repo root + each monorepo
    workspace root. Returns a flat dict ``{relpath: excerpt}``.

    SKIPS .md / vendored / built dirs. Each parser surfaces ONLY
    dependency NAMES + structural markers — never prose fields like
    ``description`` / ``readme``.
    """
    out: dict[str, Any] = {}
    roots: list[Path] = [ctx.repo_path]
    for ws in (ctx.workspaces or []):
        ws_root = ctx.repo_path / ws.path
        if ws_root.is_dir():
            roots.append(ws_root)

    for root in roots:
        for manifest in _MANIFEST_FILES:
            mpath = root / manifest
            if not mpath.is_file():
                continue
            try:
                # 256 KB cap — keeps the prompt small and defends
                # against bizarre 50MB lockfiles slipping through.
                raw = mpath.read_text(encoding="utf-8", errors="ignore")[
                    :256_000
                ]
            except OSError:
                continue
            parser = _MANIFEST_PARSERS.get(manifest)
            try:
                if parser is None:
                    excerpt = _parse_generic_manifest(manifest, raw)
                else:
                    excerpt = parser(raw)
            except Exception as exc:  # noqa: BLE001
                logger.debug("manifest parse failed for %s: %s", mpath, exc)
                continue
            if not excerpt:
                continue
            try:
                rel = str(mpath.relative_to(ctx.repo_path))
            except ValueError:
                rel = str(mpath)
            # README guard, last-defence: if any path key looks like
            # a prose doc, drop it. NOTE: requirements.txt is a
            # legitimate manifest — we explicitly check against the
            # MARKDOWN suffixes here (.md / .markdown / .rst / .adoc),
            # never .txt, because .txt is a valid manifest extension.
            lower = rel.lower()
            if lower.endswith((".md", ".markdown", ".rst", ".adoc")):
                continue
            out[rel] = excerpt
    return out


def build_auditor_context(ctx: "ScanContext") -> dict[str, Any]:
    """Assemble the JSON payload that becomes the auditor user prompt.

    Pure structural — README is forbidden. Verifiable via the unit
    test ``test_build_auditor_context_has_no_md_paths``.
    """
    paths = recent_modified_paths(ctx)
    manifests = read_manifest_excerpts(ctx)
    commits = recent_commit_subjects(ctx)

    workspaces_summary: list[dict[str, Any]] = []
    for ws in (ctx.workspaces or []):
        workspaces_summary.append({
            "name": ws.name,
            "path": ws.path,
            "stage_0_stack": ws.stack,
        })

    return {
        "stage_0_stack": ctx.stack,
        "stage_0_signals": list(ctx.stack_signals or []),
        "monorepo": ctx.monorepo,
        "workspace_manager": ctx.workspace_manager,
        "workspaces": workspaces_summary,
        "recent_paths": paths,
        "manifests": manifests,
        "recent_commits": commits,
    }


# ── Prompt ──────────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You are a stack classifier for a code-feature-detection pipeline. "
    "Given STRUCTURAL signals (file paths, manifest dependency lists, "
    "recent commit subjects), classify the repository's stack so that "
    "downstream extractors enable the right rules.\n\n"
    "Output STRICT JSON only — no prose, no markdown fences, no commentary.\n\n"
    "Schema:\n"
    "{\n"
    '  "primary_stack": "<kebab-case-stack-tag>",\n'
    '  "secondary_stacks": ["<tag>", ...],\n'
    '  "confidence": 0.0,\n'
    '  "extractor_hints": ["<short directive>", ...],\n'
    '  "reasoning": "<one paragraph>"\n'
    "}\n\n"
    "Stack tags use kebab-case nouns. Examples (not exhaustive — invent "
    "new tags when needed):\n"
    "  next-app-router, next-pages, remix, astro, sveltekit, nuxt, vue-spa,\n"
    "  fastapi, django, flask, python-library, python-cli,\n"
    "  rails-app, laravel, express, hono, fastify, nestjs,\n"
    "  go-server, go-library, go-cli, go-http-router-lib,\n"
    "  rust-binary, rust-library, rust-workspace,\n"
    "  js-client-library, ts-library, monorepo-polyglot, js-monorepo\n\n"
    "Rules:\n"
    "- Distinguish library vs application. A repo whose package.json has "
    "no app entry (no `app/`, no `pages/`, no server start script) and "
    "whose primary purpose is to be imported is a LIBRARY, not a server.\n"
    "- Distinguish framework vs application. The fastapi/fastapi repo IS "
    "the framework library — tag it `python-library`, NOT `fastapi`.\n"
    "- For polyglot monorepos (e.g. js apps + python apps), set "
    "`primary_stack` to the dominant stack and list the others in "
    "`secondary_stacks`. Use `monorepo-polyglot` only when no single "
    "stack dominates.\n"
    "- `extractor_hints` are SHORT machine-readable directives (one line "
    "each) that tell Stage 1 which patterns to enable. Examples:\n"
    '    "go-http-router via chi.Router{} in *.go"\n'
    '    "python-library — no app entry; parse __init__.py exports"\n'
    '    "rust-workspace with N member crates under crates/"\n'
    "- When the repo IS the framework itself (its own manifest declares "
    "the framework name — e.g. fastapi/fastapi's pyproject says "
    '``name = "fastapi"``, vercel/next.js publishes the "next" package), '
    'emit the literal token "framework-self" as one of the extractor_hints. '
    "Stage 0.6 (shape classifier) requires this exact hint to route the "
    "repo through the framework-repo strategy instead of the generic "
    "library / app strategies.\n"
    "- Do NOT make claims about WHAT the product does. Only classify "
    "the technical stack. Marketing/product semantics are a separate "
    "stage and out of scope here.\n"
    "- `confidence` is your honest self-assessment 0..1. Return <0.5 "
    "when signals are weak or contradictory."
)


def _build_user_prompt(ctx_payload: dict[str, Any]) -> str:
    """Render the auditor context as a compact JSON user message."""
    return (
        "Classify this repository. Return STRICT JSON matching the schema.\n\n"
        + json.dumps(ctx_payload, sort_keys=True, default=str)
    )


# ── LLM call ────────────────────────────────────────────────────────────────


def _parse_verdict_json(text: str) -> dict[str, Any] | None:
    """Tolerant JSON parser: strips fences, salvages first ``{...}``."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _verdict_from_json(
    data: dict[str, Any],
    stage_0_stack: str | None,
    cost_usd: float,
) -> AuditorVerdict:
    """Coerce a parsed JSON dict into an :class:`AuditorVerdict` with
    defensive defaults — never propagate ``None`` into typed fields.
    """
    primary = (data.get("primary_stack") or "").strip().lower()
    if not primary:
        # Fall back to Stage 0; never emit empty primary.
        primary = (stage_0_stack or "unknown").strip().lower()

    secondary_raw = data.get("secondary_stacks") or []
    if not isinstance(secondary_raw, list):
        secondary_raw = []
    secondary = tuple(
        s.strip().lower() for s in secondary_raw if isinstance(s, str) and s.strip()
    )

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    hints_raw = data.get("extractor_hints") or []
    if not isinstance(hints_raw, list):
        hints_raw = []
    # Cap hint length so a runaway model can't bloat scan_meta.
    hints = tuple(
        h.strip()[:240] for h in hints_raw
        if isinstance(h, str) and h.strip()
    )

    reasoning = (data.get("reasoning") or "").strip()
    if len(reasoning) > 2000:
        reasoning = reasoning[:2000] + "…"

    return AuditorVerdict(
        primary_stack=primary,
        secondary_stacks=secondary,
        confidence=confidence,
        extractor_hints=hints,
        reasoning=reasoning,
        cost_usd=round(cost_usd, 6),
        fallback_used=False,
    )


# ── Sprint S3.1 — deterministic correction layer ─────────────────────────────


# Penalty applied to verdict confidence when a correction fires. The
# auditor was demonstrably wrong, so downstream consumers should treat
# the corrected verdict as somewhat less certain than the original LLM
# self-rating. 0.1 keeps most corrected verdicts above the 0.5 apply
# threshold (LLM verdicts cluster ~0.9-0.95).
_CORRECTION_CONFIDENCE_PENALTY: float = 0.1

# Recognised frontend framework dependency tags. Order matters — first
# match wins. Captures the "auditor said next-app-router but no next dep"
# correction surface for every common SPA / SSR stack we know about.
_FRONTEND_FRAMEWORK_DEPS: tuple[tuple[str, str], ...] = (
    # (package.json dep name, corrected stack tag)
    ("next", "next-app-router"),
    ("@remix-run/react", "remix"),
    ("@remix-run/node", "remix"),
    ("@sveltejs/kit", "sveltekit"),
    ("nuxt", "nuxt"),
    ("astro", "astro"),
    ("@tanstack/react-router", "tanstack-router"),
    ("@tanstack/router", "tanstack-router"),
    ("react-router-dom", "react-spa-router"),
    ("react-router", "react-spa-router"),
    ("vite", "vite"),
    ("vue", "vue-spa"),
)


def _collect_all_package_deps(ctx: "ScanContext") -> set[str]:
    """Return the union of dep names across the root + every workspace
    ``package.json``. Read directly from disk (Stage 0 surfaces parsed
    dicts on ``Workspace.package_json`` already, but the root manifest
    isn't carried on ``ScanContext`` — read it here).
    """
    out: set[str] = set()

    def _harvest(pkg: dict[str, Any] | None) -> None:
        if not isinstance(pkg, dict):
            return
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            block = pkg.get(key)
            if isinstance(block, dict):
                out.update(str(k) for k in block.keys())

    # Root package.json
    try:
        root_pkg = ctx.repo_path / "package.json"
        if root_pkg.is_file():
            data = json.loads(root_pkg.read_text(encoding="utf-8", errors="ignore"))
            _harvest(data if isinstance(data, dict) else None)
    except (OSError, json.JSONDecodeError):
        pass

    for ws in (ctx.workspaces or []):
        _harvest(ws.package_json)

    return out


def _has_any_dep(deps: set[str], names: tuple[str, ...]) -> bool:
    """True if any of ``names`` is present in ``deps``.

    Direct membership only — we do NOT do prefix matching here because
    deps like ``@tanstack/react-router`` and ``@tanstack/react-query``
    share a scope but are different packages.
    """
    return any(n in deps for n in names)


def _has_app_marker(ctx: "ScanContext") -> bool:
    """True when the repo has a Next.js App Router marker present.

    Specifically: ``next.config.*`` OR an ``app/`` directory at the
    repo root or under ``src/``. Mirrors the Stage 0 heuristic.
    """
    root = ctx.repo_path
    for ext in ("js", "mjs", "ts", "cjs"):
        if (root / f"next.config.{ext}").is_file():
            return True
    if (root / "app").is_dir() or (root / "src" / "app").is_dir():
        return True
    # Also accept the marker living inside any workspace — common for
    # monorepos where the Next app is at ``apps/web/``.
    for ws in (ctx.workspaces or []):
        ws_root = root / ws.path
        for ext in ("js", "mjs", "ts", "cjs"):
            if (ws_root / f"next.config.{ext}").is_file():
                return True
        if (ws_root / "app").is_dir() or (ws_root / "src" / "app").is_dir():
            return True
    return False


def _has_manage_py(ctx: "ScanContext") -> bool:
    """True when ``manage.py`` is present at the root — Django marker."""
    return (ctx.repo_path / "manage.py").is_file()


# Non-committal primary stacks a decisive Rails-marker set should OVERRIDE
# outright. Anything NOT in this set (a real framework like ``next`` or
# ``react-spa``) is treated as the frontend of a Rails polyglot instead, so
# rails-app is added as a SECONDARY rather than replacing the primary. Generic,
# scale-invariant — these are catch-all labels, not corpus-fitted values.
_RAILS_OVERRIDABLE_PRIMARIES = frozenset({
    "js-generic", "ts-generic", "generic", "unknown",
    "javascript", "typescript", "node", "node-app",
})


def _has_rails_markers(ctx: "ScanContext") -> bool:
    """True when the repo carries the decisive Rails STRUCTURAL markers — a
    ``Gemfile`` AND ``config/routes.rb`` AND an ``app/controllers`` directory.

    All three together are unambiguously Rails: no JS or Python stack produces
    this triple. They are therefore strong enough to override a misleading
    dev-tooling-ONLY ``package.json`` (biome/eslint/prettier), which is the
    common reason the LLM auditor mislabels a Rails app as ``js-generic`` and
    suppresses the Rails extractors (routes/models/jobs/views/stimulus)."""
    root = ctx.repo_path
    return (
        (root / "Gemfile").is_file()
        and (root / "config" / "routes.rb").is_file()
        and (root / "app" / "controllers").is_dir()
    )


def _read_repo_self_name(ctx: "ScanContext") -> str | None:
    """Return the repo's own package-manifest name, if any.

    Reads (in order) root ``package.json#name`` then
    ``pyproject.toml#project.name`` then ``Cargo.toml#package.name``.
    Used by Sprint S9 ``framework-self`` detection — when a repo's own
    manifest declares a name matching a known framework, the repo IS
    that framework (e.g. ``fastapi/fastapi``'s pyproject says
    ``name = "fastapi"``; ``vercel/next.js``'s root packages publish
    ``"next"`` as their package name).

    Returns the LOWERCASED name (``next``, ``fastapi``, ``react``,
    ``swr``, ``axios``). Returns None when no manifest is readable.
    Never raises — this is best-effort.
    """
    root = ctx.repo_path

    # package.json#name (root only — workspaces have their own names
    # which don't tell us the repo's identity)
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
            n = data.get("name") if isinstance(data, dict) else None
            if isinstance(n, str) and n.strip():
                norm = n.strip().lower()
                # Scoped names like ``@nestjs/core``: return the SCOPE
                # token (``@nestjs/core`` → ``nestjs``) so the
                # framework-self check can match on scope-as-framework
                # without false-positives on generic suffixes like
                # ``core`` / ``common`` / ``server``. The framework set
                # vs scope set are distinct (see
                # ``_KNOWN_FRAMEWORK_SELF_SCOPES``).
                if norm.startswith("@") and "/" in norm:
                    scope = norm[1:].split("/", 1)[0]
                    return scope
                return norm
        except (OSError, json.JSONDecodeError):
            pass

    # pyproject.toml [project] name
    py = root / "pyproject.toml"
    if py.is_file():
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")
            # Look for ``name = "..."`` in the [project] table only —
            # ``[tool.poetry]`` also has a name but tooling sections are
            # historical and Poetry has shifted to PEP 621.
            in_project = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("["):
                    in_project = stripped in ("[project]",)
                    continue
                if in_project and stripped.startswith("name"):
                    # name = "fastapi" or name='fastapi'
                    m = re.match(
                        r'name\s*=\s*[\'"]([^\'"]+)[\'"]', stripped,
                    )
                    if m:
                        return m.group(1).strip().lower()
        except OSError:
            pass

    # Cargo.toml [package] name
    cargo = root / "Cargo.toml"
    if cargo.is_file():
        try:
            text = cargo.read_text(encoding="utf-8", errors="ignore")
            in_package = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("["):
                    in_package = stripped == "[package]"
                    continue
                if in_package and stripped.startswith("name"):
                    m = re.match(
                        r'name\s*=\s*[\'"]([^\'"]+)[\'"]', stripped,
                    )
                    if m:
                        return m.group(1).strip().lower()
        except OSError:
            pass

    return None


# Sprint S9 — known framework names. When a repo's self-name matches
# one of these, the repo IS that framework, not an app built on it.
# Mapping value is informational only; the boolean of "is a framework"
# is what drives the ``framework-self`` hint emission.
_KNOWN_FRAMEWORK_SELF_NAMES: frozenset[str] = frozenset({
    # JS/TS frameworks + libraries that publish themselves as the named pkg
    "next", "remix", "react", "vue", "svelte", "sveltekit",
    "nuxt", "astro", "solid-js", "solid", "qwik",
    "express", "fastify", "hono", "koa", "nestjs", "nest",
    "vite", "rollup", "webpack", "esbuild", "parcel",
    "swr", "axios", "react-router", "tanstack-router",
    # Python frameworks
    "fastapi", "django", "flask", "starlette", "tornado",
    "pyramid", "bottle", "sanic", "litestar",
    # Rust frameworks
    "axum", "actix-web", "rocket", "warp", "tide",
    # Go is harder (no package.json equivalent) — skip Go in this rule;
    # Go-self-framework detection lives in shape classifier signals.
})


# Scoped npm packages where the SCOPE itself identifies the framework
# (regardless of the unscoped suffix). ``@nestjs/core``,
# ``@nestjs/common`` etc. all mean "NestJS framework". Without this,
# scoped publishers would slip past the framework-self check.
_KNOWN_FRAMEWORK_SELF_SCOPES: frozenset[str] = frozenset({
    "nestjs", "remix-run", "sveltejs", "tanstack",
    "nuxt", "astrojs", "vue", "solidjs", "qwik",
})


def _has_fastapi_call(ctx: "ScanContext") -> bool:
    """True when a top-level ``.py`` file instantiates ``FastAPI()``.

    Bounded scan: only the first 20 top-level ``.py`` files are
    inspected (the marker, when present, lives in ``main.py`` /
    ``app.py`` / a top-level package entry — not in deep submodules).
    """
    root = ctx.repo_path
    checked = 0
    for entry in sorted(root.iterdir()):
        if not entry.is_file() or entry.suffix != ".py":
            continue
        checked += 1
        if checked > 20:
            break
        try:
            text = entry.read_text(encoding="utf-8", errors="ignore")[:64_000]
        except OSError:
            continue
        if "FastAPI(" in text:
            return True
    return False


def correct_auditor_verdict(
    verdict: AuditorVerdict,
    ctx: "ScanContext",
) -> tuple[AuditorVerdict, list[dict[str, str]]]:
    """Run deterministic post-pass corrections on ``verdict``.

    The auditor LLM is wrong in a small but predictable set of cases.
    This pass applies structural checks (no LLM, no magic numbers) and
    overrides the verdict when the structural signal flatly contradicts
    the LLM's primary stack.

    Returns:
        A tuple ``(corrected_verdict, corrections)``. ``corrections``
        is a list of ``{original, corrected, reason}`` dicts — one
        entry per correction that fired (typically 0 or 1). When no
        correction fires the original verdict is returned unchanged.

    Rules (all structural, no per-repo paths):
      1. ``next-app-router`` + no ``next`` dep + ``@tanstack/react-router``
         dep → corrected to ``tanstack-router``.
      2. ``next-app-router`` + no ``next`` dep + no App Router marker →
         corrected to whichever frontend framework dep IS present
         (vite / remix / astro / sveltekit / nuxt / vue / react-router).
      3. ``express`` + ``fastify`` dep present → corrected to
         ``fastify``.
      4. ``react-spa`` + ``react-router`` dep + ``pages/`` or
         ``routes/`` dir → corrected to ``react-spa-router``.
      5. ``python-library`` + ``manage.py`` present → corrected to
         ``django-app``.
      6. ``python-library`` + ``FastAPI()`` call in any top-level py
         file → corrected to ``fastapi-app``.
    """
    corrections: list[dict[str, str]] = []
    primary = verdict.primary_stack.lower()
    deps = _collect_all_package_deps(ctx)

    def _emit(new_primary: str, reason: str) -> AuditorVerdict:
        corrections.append({
            "original": verdict.primary_stack,
            "corrected": new_primary,
            "reason": reason,
        })
        new_conf = max(0.0, verdict.confidence - _CORRECTION_CONFIDENCE_PENALTY)
        return AuditorVerdict(
            primary_stack=new_primary,
            secondary_stacks=verdict.secondary_stacks,
            confidence=new_conf,
            extractor_hints=verdict.extractor_hints,
            reasoning=verdict.reasoning,
            cost_usd=verdict.cost_usd,
            fallback_used=verdict.fallback_used,
            corrections=tuple(corrections),
        )

    # Rule 1+2: next-app-router corrections — both fire only when ``next``
    # is genuinely absent. We check ``next`` itself (not prefix) since
    # ``@next/*`` scoped packages don't imply the framework on their own.
    if primary == "next-app-router" and "next" not in deps:
        # Rule 1: TanStack Router takes precedence over the generic
        # vite fallback because it is the more specific signal.
        if _has_any_dep(deps, ("@tanstack/react-router", "@tanstack/router")):
            return _emit(
                "tanstack-router",
                "no `next` dep but @tanstack/react-router dep present",
            ), corrections
        # Rule 2: pick any known frontend framework dep present.
        for dep_name, corrected_tag in _FRONTEND_FRAMEWORK_DEPS:
            if dep_name == "next":
                continue
            if dep_name in deps:
                return _emit(
                    corrected_tag,
                    f"no `next` dep and no App Router marker; "
                    f"`{dep_name}` dep present",
                ), corrections
        # No competing framework dep AND no App Router marker → drop
        # the next-app-router claim. Fall through to ``vue-spa`` only
        # if vue is hinted, else degrade to ``unknown``.
        if not _has_app_marker(ctx):
            return _emit(
                "unknown",
                "no `next` dep, no App Router marker, no other "
                "frontend framework dep — verdict unsupported",
            ), corrections

    # Rule 3: express vs fastify — fastify dep wins when both auditor
    # said express AND fastify is in the manifest.
    if primary == "express" and "fastify" in deps:
        return _emit(
            "fastify",
            "auditor said express but `fastify` dep present",
        ), corrections

    # Rule 4: react-spa upgrade when router is wired up.
    if primary == "react-spa" and _has_any_dep(
        deps, ("react-router", "react-router-dom"),
    ):
        root = ctx.repo_path
        has_pages = (root / "pages").is_dir() or (root / "src" / "pages").is_dir()
        has_routes = (root / "routes").is_dir() or (root / "src" / "routes").is_dir()
        if has_pages or has_routes:
            return _emit(
                "react-spa-router",
                "react-router dep + pages/ or routes/ directory present",
            ), corrections

    # Rule 5: python-library → django-app when ``manage.py`` present.
    if primary == "python-library" and _has_manage_py(ctx):
        return _emit(
            "django-app",
            "auditor said python-library but manage.py present",
        ), corrections

    # Rule 6: python-library → fastapi-app when ``FastAPI()`` call
    # appears in a top-level py file.
    if primary == "python-library" and _has_fastapi_call(ctx):
        return _emit(
            "fastapi-app",
            "auditor said python-library but FastAPI() call present "
            "in a top-level .py file",
        ), corrections

    # Rule 7: Rails markers (Gemfile + config/routes.rb + app/controllers) are
    # DECISIVE — no JS/Python stack produces all three. A Rails app routinely
    # ships a dev-tooling-ONLY package.json (biome/eslint/prettier) that
    # misleads the LLM auditor to ``js-generic``, which suppresses the Rails
    # extractors (routes/models/jobs/...) → 0 routes → collapsed UF recall.
    # When the markers are present we force ``rails-app`` into the stack:
    #   • a non-committal primary (js-generic/unknown/node) is OVERRIDDEN, but
    #   • a real frontend framework primary (a Rails + JS-SPA polyglot) is kept,
    #     with rails-app added as a SECONDARY so the backend is still extracted.
    _secondary_lower = {s.strip().lower() for s in verdict.secondary_stacks}
    if (
        _has_rails_markers(ctx)
        and primary != "rails-app"
        and "rails-app" not in _secondary_lower
    ):
        if primary in _RAILS_OVERRIDABLE_PRIMARIES:
            return _emit(
                "rails-app",
                f"Gemfile + config/routes.rb + app/controllers present — Rails "
                f"(overrides non-committal `{primary}`; a dev-tooling-only "
                f"package.json must not mask the Rails backend)",
            ), corrections
        # Real framework primary → Rails + JS-frontend polyglot: keep the
        # frontend primary, add rails-app as a secondary so the Rails
        # extractors activate on the backend without dropping the frontend.
        corrections.append({
            "original": verdict.primary_stack,
            "corrected": f"{verdict.primary_stack} (+rails-app secondary)",
            "reason": (
                "Rails markers (Gemfile + config/routes.rb + app/controllers) "
                f"present alongside `{primary}` frontend — polyglot backend"
            ),
        })
        return AuditorVerdict(
            primary_stack=verdict.primary_stack,
            secondary_stacks=(*verdict.secondary_stacks, "rails-app"),
            confidence=verdict.confidence,
            extractor_hints=verdict.extractor_hints,
            reasoning=verdict.reasoning,
            cost_usd=verdict.cost_usd,
            fallback_used=verdict.fallback_used,
            corrections=tuple(corrections),
        ), corrections

    # Sprint S9 — framework-self extractor hint.
    #
    # FrameworkRepoClassifier (stage_0_6_shape.py) gates on the boolean
    # ``framework-self`` being present in extractor_hints. Without it,
    # repos that ARE the framework (fastapi/fastapi, vercel/next.js,
    # vercel/swr, react itself) fall through to oss-library shape which
    # uses member_flows semantics that fit applications, not frameworks.
    #
    # The auditor's prompt encourages it to tag the framework repos as
    # ``python-library`` / ``ts-library`` (correct), but rarely thinks
    # to emit the special ``framework-self`` directive in hints. We add
    # it deterministically when the repo's own package-manifest name
    # matches one of the recognised framework names.
    #
    # This is purely additive — it doesn't change primary_stack, just
    # appends the hint when missing. No confidence penalty (we're
    # augmenting evidence, not correcting an error).
    self_name = _read_repo_self_name(ctx)
    is_framework_self = bool(
        self_name and (
            self_name in _KNOWN_FRAMEWORK_SELF_NAMES
            or self_name in _KNOWN_FRAMEWORK_SELF_SCOPES
        )
    )
    if (
        is_framework_self
        and "framework-self" not in verdict.extractor_hints
    ):
        new_hints = (*verdict.extractor_hints, "framework-self")
        corrections.append({
            "original": "(no framework-self hint)",
            "corrected": "framework-self hint added",
            "reason": (
                f"repo self-name `{self_name}` matches a known framework — "
                "FrameworkRepoClassifier requires the explicit hint to fire"
            ),
        })
        return AuditorVerdict(
            primary_stack=verdict.primary_stack,
            secondary_stacks=verdict.secondary_stacks,
            confidence=verdict.confidence,  # no penalty — additive
            extractor_hints=new_hints,
            reasoning=verdict.reasoning,
            cost_usd=verdict.cost_usd,
            fallback_used=verdict.fallback_used,
            corrections=tuple(corrections),
        ), corrections

    return verdict, corrections


def _fallback_verdict(
    ctx: "ScanContext",
    *,
    reason: str,
    cost_usd: float = 0.0,
) -> AuditorVerdict:
    """Emit an echo-of-Stage-0 verdict. ``confidence=1.0`` because the
    orchestrator treats this as "keep Stage 0 as-is" — Stage 0 IS
    reliable for the cases it handles; auditor is purely additive.

    Sprint S9 extension: even on the fallback path (no API key / parse
    fail / cost cap) we still apply the deterministic
    ``framework-self`` hint rule. The auditor LLM is not the only
    source of truth here — when the repo's own manifest says
    ``name = "fastapi"`` we still want FrameworkRepoClassifier to fire
    regardless of whether the auditor itself ran.
    """
    primary = (ctx.stack or "unknown").strip().lower()
    base = AuditorVerdict(
        primary_stack=primary,
        secondary_stacks=(),
        confidence=1.0,
        extractor_hints=(),
        reasoning=f"auditor fallback: {reason}",
        cost_usd=round(cost_usd, 6),
        fallback_used=True,
    )
    # Apply ONLY the S9 framework-self hint deterministically. We do
    # NOT run the other corrections on the fallback path — those are
    # designed to fix Sonnet hallucinations, not Stage 0 outputs, and
    # would falsely flip the verdict when the user has Stage 0 reporting
    # ``next-app-router`` but their package.json hasn't yet been
    # populated (as happens in tests).
    self_name = _read_repo_self_name(ctx)
    if (
        self_name
        and (
            self_name in _KNOWN_FRAMEWORK_SELF_NAMES
            or self_name in _KNOWN_FRAMEWORK_SELF_SCOPES
        )
    ):
        return AuditorVerdict(
            primary_stack=base.primary_stack,
            secondary_stacks=base.secondary_stacks,
            confidence=base.confidence,
            extractor_hints=("framework-self",),
            reasoning=base.reasoning + " [+S9 framework-self hint]",
            cost_usd=base.cost_usd,
            fallback_used=True,
        )
    return base


def _call_haiku(
    client: Any,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    llm_health: LlmHealth | None = None,
) -> tuple[str, int, int]:
    """One Haiku call. Returns ``(text, in_tokens, out_tokens)``.

    Consults the shared :class:`LlmHealth`: after the first auth-class
    failure anywhere in the scan the call is skipped (dead key).
    """
    if llm_health is not None and not llm_health.should_call():
        return "", 0, 0
    try:
        msg = client.messages.create(
            model=gateway_model(model),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal at scan-time
        if llm_health is not None and llm_health.record_failure(
            exc, stage="stack_auditor",
        ):
            logger.error(
                "stack_auditor: LLM authentication failed — skipping all "
                "remaining LLM calls this scan: %s", exc,
            )
        else:
            logger.warning("stack_auditor: Haiku call failed: %s", exc)
        return "", 0, 0
    if llm_health is not None:
        llm_health.record_success()
    try:
        text_parts = []
        for block in msg.content:
            t = getattr(block, "text", None)
            if t:
                text_parts.append(t)
        text = "\n".join(text_parts)
    except Exception:  # noqa: BLE001
        text = ""
    in_tokens = int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0)
    out_tokens = int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)
    return text, in_tokens, out_tokens


# ── Public entry point ──────────────────────────────────────────────────────


def run_stack_auditor(
    ctx: "ScanContext",
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    cost_tracker: CostTracker | None = None,
    log: "StageLogger | None" = None,
    llm_health: LlmHealth | None = None,
    cache: Any | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> AuditorVerdict:
    """Run Stage 0.5 against ``ctx``.

    Args:
        ctx: Stage 0 output. Read-only.
        client: pre-built Anthropic-like client. Tests pass a fake.
        model: Haiku model id. Defaults to :data:`DEFAULT_MODEL`.
        cost_tracker: shared :class:`CostTracker`. When ``None`` a
            local tracker is created (still recorded into the returned
            verdict's ``cost_usd``).
        log: optional :class:`StageLogger` for structured warnings.
        _client_factory: injection hook for the default client builder.

    Returns:
        An :class:`AuditorVerdict`. Always returns; never raises for
        IO failures (falls back to Stage 0 instead).
    """
    if client is None:
        client = _client_factory()
    if client is None:
        verdict = _fallback_verdict(ctx, reason="no_anthropic_client")
        if log is not None:
            log.warn(
                "auditor: no Anthropic client; falling back to Stage 0",
                stage_0_stack=ctx.stack,
            )
        return verdict

    payload = build_auditor_context(ctx)
    user_prompt = _build_user_prompt(payload)

    cache_key = _auditor_cache_key(model, _SYSTEM_PROMPT, user_prompt)
    cached_text = _auditor_cache_get(cache, cache_key)
    if cached_text is not None:
        # Replay: identical repo state → identical hints → stable stage-8
        # analyst cache key. $0, no tokens; same parse path as live below.
        text, in_tok, out_tok = cached_text, 0, 0
        if log is not None:
            log.info("auditor: llm-cache hit — verdict replayed at $0")
    else:
        text, in_tok, out_tok = _call_haiku(
            client,
            model=model,
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=DEFAULT_MAX_TOKENS,
            llm_health=llm_health,
        )

    # Record cost regardless of parse outcome (we paid for it).
    call_cost = 0.0
    if in_tok or out_tok:
        tracker = cost_tracker or CostTracker(max_cost=None)
        entry = tracker.record(
            provider="anthropic",
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            label="stage-0.5-auditor",
        )
        call_cost = float(getattr(entry, "cost_usd", 0.0) or 0.0)

    if call_cost > COST_CAP_USD:
        verdict = _fallback_verdict(
            ctx,
            reason=f"cost_cap_exceeded: ${call_cost:.4f} > ${COST_CAP_USD}",
            cost_usd=call_cost,
        )
        if log is not None:
            log.warn(
                f"auditor: cost cap ${COST_CAP_USD} exceeded "
                f"(${call_cost:.4f}); falling back to Stage 0",
            )
        return verdict

    if not text:
        verdict = _fallback_verdict(
            ctx, reason="llm_empty_or_failed", cost_usd=call_cost,
        )
        if log is not None:
            log.warn("auditor: empty LLM response; falling back to Stage 0")
        return verdict

    data = _parse_verdict_json(text)
    if data is None:
        verdict = _fallback_verdict(
            ctx, reason="json_parse_failed", cost_usd=call_cost,
        )
        if log is not None:
            log.warn("auditor: JSON parse failed; falling back to Stage 0")
        return verdict

    # Persist ONLY a successfully-parsed response (never failures) — and
    # only when it came from a live call.
    if cached_text is None:
        _auditor_cache_put(cache, cache_key, text)

    verdict = _verdict_from_json(data, ctx.stack, call_cost)

    # ── Sprint S3.1: deterministic correction post-pass ──
    # Run structural overrides AFTER the LLM verdict lands, BEFORE the
    # orchestrator folds it back into ScanContext. Mutates only when a
    # rule fires; the original verdict is returned otherwise. The
    # correction list is logged here and re-derived by the orchestrator
    # (which calls ``correct_auditor_verdict`` on the original verdict
    # to surface the per-correction telemetry into ``scan_meta``).
    corrected, corrections = correct_auditor_verdict(verdict, ctx)
    if corrections and log is not None:
        for corr in corrections:
            log.warn(
                f"auditor_correction: "
                f"{corr['original']!r} → {corr['corrected']!r} "
                f"({corr['reason']})",
            )

    verdict = corrected
    if log is not None:
        log.info(
            f"auditor: primary={verdict.primary_stack} "
            f"secondary={list(verdict.secondary_stacks)} "
            f"confidence={verdict.confidence:.2f} "
            f"hints={len(verdict.extractor_hints)} "
            f"cost=${verdict.cost_usd:.4f} "
            f"corrections={len(corrections)}",
        )
    return verdict


__all__ = [
    "AuditorVerdict",
    "build_auditor_context",
    "correct_auditor_verdict",
    "read_manifest_excerpts",
    "recent_commit_subjects",
    "recent_modified_paths",
    "run_stack_auditor",
    "COST_CAP_USD",
    "DEFAULT_MODEL",
    "MIN_CONFIDENCE_TO_APPLY",
]
