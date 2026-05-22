"""Tests for Stage 0.5 — Stack Auditor.

Covers:
  - Context builder strips all .md / vendored-dir paths (README rule).
  - Manifest parsers surface NAMES only, never prose fields.
  - Fallback behaviour (no client → echo-of-Stage-0 verdict).
  - LLM-success path (fake client) populates verdict correctly.
  - Low confidence triggers a fallback shape preserved verbatim.
  - Cost cap defends against runaway calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from faultline.pipeline_v2 import stack_auditor as sa
from faultline.pipeline_v2.stack_auditor import (
    AuditorVerdict,
    COST_CAP_USD,
    MIN_CONFIDENCE_TO_APPLY,
    build_auditor_context,
    read_manifest_excerpts,
    recent_modified_paths,
    run_stack_auditor,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace


# ── Fixtures ────────────────────────────────────────────────────────────────


@dataclass
class _FakeCommit:
    message: str
    files_changed: list[str]


def _make_ctx(
    tmp_path: Path,
    *,
    files: dict[str, str] | None = None,
    commits: list[_FakeCommit] | None = None,
    stack: str | None = "next-app-router",
    monorepo: bool = False,
    workspaces: list[Workspace] | None = None,
    workspace_manager: str | None = None,
    stack_signals: list[str] | None = None,
) -> ScanContext:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    files = files or {}
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    tracked = sorted(files.keys())
    return ScanContext(
        repo_path=repo,
        stack=stack,
        monorepo=monorepo,
        workspaces=workspaces,
        tracked_files=tracked,
        commits=commits or [],
        stack_signals=stack_signals or [],
        workspace_manager=workspace_manager,
        run_id="test-run",
        run_dir=tmp_path / "run",
    )


# ── README guard tests ─────────────────────────────────────────────────────


def test_build_auditor_context_has_no_md_paths(tmp_path: Path) -> None:
    """Hard rule from CLAUDE.md — no in-repo .md files in the prompt."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "README.md": "# Project",  # MUST NOT appear in context
            "docs/intro.md": "# Intro",  # MUST NOT appear in context
            "package.json": json.dumps({
                "name": "x",
                "dependencies": {"next": "14.0.0"},
            }),
            "app/page.tsx": "export default function P() {}",
            "src/lib/util.ts": "export const x = 1;",
        },
        commits=[
            _FakeCommit(
                message="feat: initial",
                files_changed=[
                    "README.md",  # MUST be filtered out
                    "docs/guide.md",
                    "app/page.tsx",
                    "src/lib/util.ts",
                ],
            ),
        ],
    )

    payload = build_auditor_context(ctx)
    serialized = json.dumps(payload).lower()

    # Recent paths must be free of .md / .markdown / .rst / .txt prose docs.
    assert all(
        not p.lower().endswith((".md", ".markdown", ".rst", ".adoc", ".txt"))
        for p in payload["recent_paths"]
    ), f"Found .md in recent_paths: {payload['recent_paths']}"
    assert "readme" not in serialized.replace("readme.md", ""), (
        f"README leaked into serialized payload: {serialized!r}"
    )
    # Manifests dict keys can include "package.json" but no .md docs.
    assert all(not k.lower().endswith(".md")
               for k in payload["manifests"].keys())


def test_recent_modified_paths_excludes_vendored_dirs(tmp_path: Path) -> None:
    ctx = _make_ctx(
        tmp_path,
        commits=[
            _FakeCommit(
                message="chore",
                files_changed=[
                    "node_modules/foo/index.js",  # excluded
                    "vendor/bar.go",  # excluded
                    "target/debug/main",  # excluded
                    "src/main.go",  # kept
                ],
            ),
        ],
        files={"src/main.go": "package main"},
    )
    paths = recent_modified_paths(ctx)
    assert paths == ["src/main.go"]


# ── Manifest parser tests ──────────────────────────────────────────────────


def test_read_manifest_excerpts_package_json_names_only(tmp_path: Path) -> None:
    """package.json excerpt must contain dep NAMES + script NAMES, never
    description / keywords / commands.
    """
    ctx = _make_ctx(
        tmp_path,
        files={
            "package.json": json.dumps({
                "name": "demo",
                "description": "SECRET PROSE that must not leak",
                "keywords": ["secret-keyword"],
                "author": "secret-author",
                "scripts": {
                    "build": "echo SECRET_COMMAND_STRING",
                    "test": "vitest",
                },
                "dependencies": {"next": "14.0.0", "react": "18.0.0"},
                "devDependencies": {"typescript": "5.0.0"},
            }),
        },
        stack="next-app-router",
    )
    excerpts = read_manifest_excerpts(ctx)
    pkg = excerpts.get("package.json")
    assert pkg is not None
    assert pkg["dependencies"] == ["next", "react"]
    assert pkg["devDependencies"] == ["typescript"]
    assert pkg["scripts"] == ["build", "test"]
    serialized = json.dumps(pkg).lower()
    assert "secret" not in serialized, (
        f"Prose leaked into excerpt: {serialized!r}"
    )
    assert "secret_command_string" not in serialized


def test_read_manifest_excerpts_cargo_workspace(tmp_path: Path) -> None:
    ctx = _make_ctx(
        tmp_path,
        files={
            "Cargo.toml": (
                "[workspace]\n"
                'members = ["crates/core", "crates/cli"]\n'
                "\n"
                "[dependencies]\n"
                "serde = \"1.0\"\n"
                "tokio = { version = \"1\", features = [\"full\"] }\n"
            ),
        },
        stack="rust",
    )
    excerpts = read_manifest_excerpts(ctx)
    cargo = excerpts.get("Cargo.toml")
    assert cargo is not None
    assert cargo.get("workspace_members") == ["crates/core", "crates/cli"]
    assert set(cargo.get("dependencies", [])) == {"serde", "tokio"}


def test_read_manifest_excerpts_go_mod(tmp_path: Path) -> None:
    ctx = _make_ctx(
        tmp_path,
        files={
            "go.mod": (
                "module github.com/example/foo\n"
                "\n"
                "go 1.21\n"
                "\n"
                "require (\n"
                "\tgithub.com/go-chi/chi/v5 v5.0.0\n"
                "\tgithub.com/stretchr/testify v1.8.0\n"
                ")\n"
            ),
        },
        stack="go",
    )
    excerpts = read_manifest_excerpts(ctx)
    go = excerpts.get("go.mod")
    assert go is not None
    assert go["module"] == "github.com/example/foo"
    assert "github.com/go-chi/chi/v5" in go["require"]


def test_read_manifest_excerpts_pyproject_strips_description(
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(
        tmp_path,
        files={
            "pyproject.toml": (
                "[project]\n"
                'name = "fastapi"\n'
                'description = "SECRET DESCRIPTION must not leak"\n'
                'readme = "README.md"\n'
                'dependencies = ["starlette>=0.36", "pydantic>=2"]\n'
                "\n"
                "[project.scripts]\n"
                'fastapi = "fastapi.cli:main"\n'
            ),
        },
        stack="fastapi",
    )
    excerpts = read_manifest_excerpts(ctx)
    py = excerpts.get("pyproject.toml")
    assert py is not None
    assert py["project_name"] == "fastapi"
    assert py["project_dependencies"] == ["pydantic", "starlette"]
    assert py["project_scripts"] == ["fastapi"]
    serialized = json.dumps(py).lower()
    assert "secret description" not in serialized
    assert "readme" not in serialized


# ── Fallback path tests ────────────────────────────────────────────────────


def test_run_stack_auditor_no_client_returns_fallback(tmp_path: Path) -> None:
    """No Anthropic client → echo Stage 0 with fallback_used=True."""
    ctx = _make_ctx(tmp_path, stack="next-app-router")
    verdict = run_stack_auditor(
        ctx,
        client=None,
        _client_factory=lambda: None,
    )
    assert verdict.fallback_used is True
    assert verdict.primary_stack == "next-app-router"
    assert verdict.confidence == 1.0
    assert verdict.cost_usd == 0.0


def test_run_stack_auditor_llm_failure_returns_fallback(
    tmp_path: Path,
) -> None:
    class _BoomClient:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                raise RuntimeError("api down")
        messages = _Messages()

    ctx = _make_ctx(tmp_path, stack="next-app-router")
    verdict = run_stack_auditor(ctx, client=_BoomClient())
    assert verdict.fallback_used is True
    assert verdict.primary_stack == "next-app-router"


def test_run_stack_auditor_empty_response_returns_fallback(
    tmp_path: Path,
) -> None:
    class _EmptyClient:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                return _FakeMsg(text="", in_tokens=10, out_tokens=0)
        messages = _Messages()

    ctx = _make_ctx(tmp_path, stack="next-app-router")
    verdict = run_stack_auditor(ctx, client=_EmptyClient())
    assert verdict.fallback_used is True


def test_run_stack_auditor_unparseable_json_returns_fallback(
    tmp_path: Path,
) -> None:
    class _GibberishClient:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                return _FakeMsg(
                    text="not json at all", in_tokens=10, out_tokens=5,
                )
        messages = _Messages()

    ctx = _make_ctx(tmp_path, stack="next-app-router")
    verdict = run_stack_auditor(ctx, client=_GibberishClient())
    assert verdict.fallback_used is True


# ── Success path ───────────────────────────────────────────────────────────


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeBlock:
    text: str


class _FakeMsg:
    def __init__(self, *, text: str, in_tokens: int, out_tokens: int) -> None:
        self.content = [_FakeBlock(text=text)]
        self.usage = _FakeUsage(
            input_tokens=in_tokens, output_tokens=out_tokens,
        )


def test_run_stack_auditor_happy_path(tmp_path: Path) -> None:
    response_json = json.dumps({
        "primary_stack": "go-library",
        "secondary_stacks": ["go-http-router"],
        "confidence": 0.82,
        "extractor_hints": [
            "go-http-router via chi.Router{} in *.go",
            "no main package — pure library",
        ],
        "reasoning": "Single go.mod at root, no cmd/ dir, exposes Router.",
    })

    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                return _FakeMsg(
                    text=response_json, in_tokens=500, out_tokens=200,
                )
        messages = _Messages()

    ctx = _make_ctx(
        tmp_path,
        stack="go",
        files={"go.mod": "module github.com/go-chi/chi\n"},
    )
    verdict = run_stack_auditor(ctx, client=_Client())

    assert verdict.fallback_used is False
    assert verdict.primary_stack == "go-library"
    assert verdict.secondary_stacks == ("go-http-router",)
    assert verdict.confidence == pytest.approx(0.82, abs=1e-3)
    assert len(verdict.extractor_hints) == 2
    assert verdict.cost_usd > 0.0
    assert verdict.cost_usd < COST_CAP_USD


def test_run_stack_auditor_low_confidence_still_returns_verdict(
    tmp_path: Path,
) -> None:
    """Confidence < 0.5 is NOT a fallback — orchestrator handles that.
    The auditor itself just returns the verdict honestly.
    """
    response_json = json.dumps({
        "primary_stack": "monorepo-polyglot",
        "secondary_stacks": [],
        "confidence": 0.3,
        "extractor_hints": [],
        "reasoning": "Signals are ambiguous.",
    })

    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                return _FakeMsg(
                    text=response_json, in_tokens=400, out_tokens=100,
                )
        messages = _Messages()

    ctx = _make_ctx(tmp_path, stack="js-generic")
    verdict = run_stack_auditor(ctx, client=_Client())
    assert verdict.fallback_used is False
    assert verdict.confidence == pytest.approx(0.3, abs=1e-3)
    assert verdict.confidence < MIN_CONFIDENCE_TO_APPLY


def test_run_stack_auditor_cost_cap_triggers_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force the recorded cost above COST_CAP_USD — orchestrator
    should treat the verdict as fallback even though parse succeeded.
    """
    response_json = json.dumps({
        "primary_stack": "rust-workspace",
        "confidence": 0.9,
        "extractor_hints": [],
        "reasoning": "test",
    })

    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                # Massive token count so even Haiku-cheap pricing
                # exceeds the $0.01 cap.
                return _FakeMsg(
                    text=response_json,
                    in_tokens=100_000_000,
                    out_tokens=10_000_000,
                )
        messages = _Messages()

    ctx = _make_ctx(tmp_path, stack="rust")
    verdict = run_stack_auditor(ctx, client=_Client())
    assert verdict.fallback_used is True
    # Stage 0 stack preserved.
    assert verdict.primary_stack == "rust"


# ── ScanContext.with_audited_stack() ───────────────────────────────────────


def test_with_audited_stack_returns_new_instance(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, stack="go")
    new_ctx = ctx.with_audited_stack(
        audited_stack="go-library",
        secondary_stacks=("go-http-router",),
        extractor_hints=("go-http-router via chi",),
        auditor_confidence=0.8,
    )
    # New instance — Stage 0 ctx untouched.
    assert new_ctx is not ctx
    assert ctx.audited_stack is None
    assert ctx.stack == "go"
    # New instance has Stage 0 stack preserved AND audited fields set.
    assert new_ctx.stack == "go"
    assert new_ctx.audited_stack == "go-library"
    assert new_ctx.secondary_stacks == ("go-http-router",)
    assert new_ctx.extractor_hints == ("go-http-router via chi",)
    assert new_ctx.auditor_confidence == 0.8


# ── Verdict coercion edge cases ────────────────────────────────────────────


def test_verdict_coerces_missing_fields(tmp_path: Path) -> None:
    """LLM may omit fields — verdict must never have None in typed slots."""
    response_json = json.dumps({"primary_stack": "next-app-router"})

    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                return _FakeMsg(text=response_json, in_tokens=10, out_tokens=5)
        messages = _Messages()

    # Provide structural ``next`` signal so Sprint S3.1's correction
    # post-pass leaves the verdict alone (the corrector would otherwise
    # downgrade the verdict because no ``next`` dep is in the manifest).
    ctx = _make_ctx(
        tmp_path,
        files={
            "package.json": json.dumps({
                "name": "app",
                "dependencies": {"next": "^15.0.0"},
            }),
            "next.config.mjs": "export default {}",
        },
        stack="next",
    )
    verdict = run_stack_auditor(ctx, client=_Client())
    assert verdict.primary_stack == "next-app-router"
    assert verdict.secondary_stacks == ()
    assert verdict.extractor_hints == ()
    assert verdict.confidence == 0.0  # default when LLM omits


def test_verdict_strips_markdown_fences(tmp_path: Path) -> None:
    response_text = (
        "```json\n"
        + json.dumps({
            "primary_stack": "fastapi",
            "confidence": 0.9,
        })
        + "\n```"
    )

    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                return _FakeMsg(text=response_text, in_tokens=10, out_tokens=5)
        messages = _Messages()

    ctx = _make_ctx(tmp_path, stack="fastapi")
    verdict = run_stack_auditor(ctx, client=_Client())
    assert verdict.primary_stack == "fastapi"
    assert verdict.fallback_used is False


# ─────────────── Sprint S3.1 — correction layer ────────────────


from faultline.pipeline_v2.stack_auditor import correct_auditor_verdict


def _verdict(primary: str, confidence: float = 0.95) -> AuditorVerdict:
    return AuditorVerdict(
        primary_stack=primary,
        secondary_stacks=(),
        confidence=confidence,
        extractor_hints=(),
        reasoning="",
        cost_usd=0.0,
        fallback_used=False,
    )


def test_correct_next_to_tanstack_router(tmp_path: Path) -> None:
    """next-app-router + no `next` dep + @tanstack/react-router → tanstack-router."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "package.json": json.dumps({
                "name": "frontend",
                "dependencies": {
                    "@tanstack/react-router": "^1.95.1",
                    "react": "^19.0.0",
                },
            }),
        },
        stack="next-app-router",
    )
    corrected, corrections = correct_auditor_verdict(
        _verdict("next-app-router"), ctx,
    )
    assert corrected.primary_stack == "tanstack-router"
    assert len(corrections) == 1
    assert corrections[0]["original"] == "next-app-router"
    assert corrections[0]["corrected"] == "tanstack-router"
    # Confidence reduced after correction.
    assert corrected.confidence < 0.95
    # corrections also surfaced on the verdict object itself.
    assert tuple(corrected.corrections) == tuple(corrections)


def test_correct_next_to_vite(tmp_path: Path) -> None:
    """next-app-router + no `next` dep + vite dep → vite."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "package.json": json.dumps({
                "name": "spa",
                "dependencies": {"react": "^19.0.0"},
                "devDependencies": {"vite": "^6.0.0"},
            }),
        },
        stack="next-app-router",
    )
    corrected, corrections = correct_auditor_verdict(
        _verdict("next-app-router"), ctx,
    )
    assert corrected.primary_stack == "vite"
    assert len(corrections) == 1


def test_correct_express_to_fastify(tmp_path: Path) -> None:
    """express + fastify dep present → fastify."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "package.json": json.dumps({
                "name": "api",
                "dependencies": {"fastify": "^4.29.1"},
            }),
        },
        stack="express",
    )
    corrected, corrections = correct_auditor_verdict(
        _verdict("express"), ctx,
    )
    assert corrected.primary_stack == "fastify"
    assert len(corrections) == 1


def test_correct_python_library_to_django(tmp_path: Path) -> None:
    """python-library + manage.py present → django-app."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "manage.py": "import django\n",
            "pyproject.toml": "[project]\nname = \"site\"\n",
        },
        stack="django",
    )
    corrected, corrections = correct_auditor_verdict(
        _verdict("python-library"), ctx,
    )
    assert corrected.primary_stack == "django-app"
    assert len(corrections) == 1
    assert "manage.py" in corrections[0]["reason"]


def test_correct_python_library_to_fastapi(tmp_path: Path) -> None:
    """python-library + FastAPI() call in top-level py → fastapi-app."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            "pyproject.toml": "[project]\nname = \"api\"\n",
        },
        stack="fastapi",
    )
    corrected, corrections = correct_auditor_verdict(
        _verdict("python-library"), ctx,
    )
    assert corrected.primary_stack == "fastapi-app"
    assert len(corrections) == 1


def test_correction_noop_when_signals_match(tmp_path: Path) -> None:
    """Verdict matches structural signals → no correction, returns unchanged."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "package.json": json.dumps({
                "name": "app",
                "dependencies": {"next": "^15.0.0", "react": "^19.0.0"},
            }),
            "next.config.mjs": "export default {}",
            "app/page.tsx": "export default () => null;",
        },
        stack="next-app-router",
    )
    original = _verdict("next-app-router")
    corrected, corrections = correct_auditor_verdict(original, ctx)
    assert corrected is original  # untouched
    assert corrections == []


# ─────────────── Sprint S9 — framework-self hint ─────────────────


def test_s9_framework_self_added_for_fastapi_repo(tmp_path: Path) -> None:
    """fastapi/fastapi repo: pyproject says ``name = "fastapi"`` →
    framework-self hint must be appended even when the LLM forgot."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "pyproject.toml": "[project]\nname = \"fastapi\"\n",
            "fastapi/__init__.py": "from .applications import FastAPI\n",
        },
        stack="python-library",
    )
    corrected, corrections = correct_auditor_verdict(
        _verdict("python-library"), ctx,
    )
    assert "framework-self" in corrected.extractor_hints
    # Hint addition is additive — no confidence penalty.
    assert corrected.confidence == 0.95
    # Correction is logged for telemetry.
    assert any(
        "framework-self" in c.get("corrected", "")
        for c in corrections
    )


def test_s9_framework_self_added_for_next_repo(tmp_path: Path) -> None:
    """vercel/next.js: root package.json#name == "next"."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "package.json": json.dumps({
                "name": "next",
                "dependencies": {"react": "^19.0.0"},
            }),
        },
        stack="ts-library",
    )
    corrected, corrections = correct_auditor_verdict(
        _verdict("ts-library"), ctx,
    )
    assert "framework-self" in corrected.extractor_hints


def test_s9_framework_self_noop_for_user_app(tmp_path: Path) -> None:
    """An app built on top of FastAPI (name = "my-api") must NOT
    receive the framework-self hint — that would route the whole
    Stage 6.6 + Stage 8 pipeline through the wrong strategy."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "pyproject.toml": "[project]\nname = \"my-api\"\n",
            "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        },
        stack="fastapi-app",
    )
    corrected, _ = correct_auditor_verdict(
        _verdict("fastapi-app"), ctx,
    )
    assert "framework-self" not in corrected.extractor_hints


def test_s9_framework_self_noop_when_already_present(tmp_path: Path) -> None:
    """If the auditor already emitted framework-self, the deterministic
    layer must not double-add it."""
    ctx = _make_ctx(
        tmp_path,
        files={"pyproject.toml": "[project]\nname = \"fastapi\"\n"},
        stack="python-library",
    )
    v = AuditorVerdict(
        primary_stack="python-library",
        secondary_stacks=(),
        confidence=0.9,
        extractor_hints=("framework-self",),
        reasoning="",
        cost_usd=0.0,
        fallback_used=False,
    )
    corrected, corrections = correct_auditor_verdict(v, ctx)
    # Exactly one framework-self in hints.
    assert corrected.extractor_hints.count("framework-self") == 1
    # No new correction was appended for hint addition.
    assert not any(
        "framework-self" in c.get("corrected", "")
        for c in corrections
    )


def test_s9_framework_self_scoped_pkg_name(tmp_path: Path) -> None:
    """Scoped npm names like ``@nestjs/core`` should match — we take
    the suffix when normalising."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "package.json": json.dumps({
                "name": "@nestjs/core",
                "dependencies": {},
            }),
        },
        stack="ts-library",
    )
    corrected, _ = correct_auditor_verdict(
        _verdict("ts-library"), ctx,
    )
    assert "framework-self" in corrected.extractor_hints


def test_s9_framework_self_for_cargo_repo(tmp_path: Path) -> None:
    """Rust framework repos: Cargo.toml [package] name = "axum"."""
    ctx = _make_ctx(
        tmp_path,
        files={
            "Cargo.toml": "[package]\nname = \"axum\"\nversion = \"0.7.0\"\n",
        },
        stack="rust-library",
    )
    corrected, _ = correct_auditor_verdict(
        _verdict("rust-library"), ctx,
    )
    assert "framework-self" in corrected.extractor_hints
