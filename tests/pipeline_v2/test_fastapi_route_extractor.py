"""Unit tests for the FastAPI route extractor + tightened django classifier.

Covers:
  * @app / @router decorator route extraction
  * APIRouter(prefix=...) composition
  * app.include_router(..., prefix=...) composition
  * explicit ``routes`` tuples flowing into build_routes_index
  * django false-positive fix (settings.py without django config / no
    manage.py / no django dep → NOT django) + true case (manage.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.extractors.fastapi import FastApiRouteExtractor
from faultline.pipeline_v2.indexes import build_routes_index
from faultline.pipeline_v2.stage_0_intake import (
    ScanContext,
    _is_django_repo,
    detect_stack,
)


def _ctx(repo: Path, files: list[str], **kw) -> ScanContext:
    return ScanContext(
        repo_path=repo,
        stack=kw.get("stack", "fastapi"),
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
        secondary_stacks=kw.get("secondary_stacks", ()),
        audited_stack=kw.get("audited_stack"),
    )


# ── FastAPI extractor ───────────────────────────────────────────────────────


def test_router_decorator_with_prefix(tmp_path: Path) -> None:
    f = tmp_path / "routers" / "cases.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        'from fastapi import APIRouter\n'
        'router = APIRouter(prefix="/api/cases", tags=["cases"])\n'
        '@router.get("")\n'
        'def list_cases(): ...\n'
        '@router.get("/{case_id}")\n'
        'def get_case(case_id: str): ...\n'
        '@router.post("", status_code=201)\n'
        'def create_case(): ...\n'
        '@router.delete("/{case_id}", status_code=204)\n'
        'def delete_case(case_id: str): ...\n'
    )
    ctx = _ctx(tmp_path, ["routers/cases.py"])
    anchors = FastApiRouteExtractor().extract(ctx)
    assert anchors, "expected at least one anchor"
    routes = {(p, m) for a in anchors for (p, m, _f) in a.routes}
    assert ("/api/cases", "GET") in routes
    assert ("/api/cases/{case_id}", "GET") in routes
    assert ("/api/cases", "POST") in routes
    assert ("/api/cases/{case_id}", "DELETE") in routes


def test_typed_router_ctor_and_app_decorator(tmp_path: Path) -> None:
    f = tmp_path / "main.py"
    f.write_text(
        'from fastapi import FastAPI, APIRouter\n'
        'app = FastAPI()\n'
        'router: APIRouter = APIRouter(prefix="/api/admin")\n'
        '@app.get("/health")\n'
        'def health(): ...\n'
        '@router.put("/users/{uid}")\n'
        'def update_user(uid: str): ...\n'
    )
    ctx = _ctx(tmp_path, ["main.py"])
    anchors = FastApiRouteExtractor().extract(ctx)
    routes = {(p, m) for a in anchors for (p, m, _f) in a.routes}
    # @app has no prefix
    assert ("/health", "GET") in routes
    # typed APIRouter ctor prefix composed
    assert ("/api/admin/users/{uid}", "PUT") in routes


def test_include_router_extra_prefix(tmp_path: Path) -> None:
    (tmp_path / "routers").mkdir()
    (tmp_path / "routers" / "billing.py").write_text(
        'from fastapi import APIRouter\n'
        'router = APIRouter()\n'
        '@router.get("/invoices")\n'
        'def invoices(): ...\n'
    )
    (tmp_path / "main.py").write_text(
        'from routers import billing\n'
        'app.include_router(billing.router, prefix="/api/v2")\n'
    )
    ctx = _ctx(tmp_path, ["routers/billing.py", "main.py"])
    anchors = FastApiRouteExtractor().extract(ctx)
    routes = {(p, m) for a in anchors for (p, m, _f) in a.routes}
    assert ("/api/v2/invoices", "GET") in routes


def test_skips_vendored_and_tests(tmp_path: Path) -> None:
    (tmp_path / ".venv" / "site-packages" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "site-packages" / "lib" / "x.py").write_text(
        'router = APIRouter(prefix="/vendored")\n@router.get("/y")\ndef y(): ...\n'
    )
    (tmp_path / "real.py").write_text(
        'router = APIRouter(prefix="/real")\n@router.get("/z")\ndef z(): ...\n'
    )
    ctx = _ctx(
        tmp_path,
        [".venv/site-packages/lib/x.py", "real.py"],
    )
    anchors = FastApiRouteExtractor().extract(ctx)
    routes = {p for a in anchors for (p, _m, _f) in a.routes}
    assert "/real/z" in routes
    assert "/vendored/y" not in routes


def test_self_skips_on_non_fastapi(tmp_path: Path) -> None:
    (tmp_path / "x.go").write_text("package main")
    ctx = _ctx(tmp_path, ["x.go"], stack="go", audited_stack="go")
    assert FastApiRouteExtractor().extract(ctx) == []


def test_activates_via_secondary_stack(tmp_path: Path) -> None:
    (tmp_path / "r.py").write_text(
        'router = APIRouter(prefix="/api")\n@router.get("/a")\ndef a(): ...\n'
    )
    ctx = _ctx(
        tmp_path, ["r.py"], stack="django", secondary_stacks=("fastapi",),
    )
    anchors = FastApiRouteExtractor().extract(ctx)
    assert any("/api/a" == p for a in anchors for (p, _m, _f) in a.routes)


# ── routes_index integration ────────────────────────────────────────────────


def test_explicit_routes_flow_into_routes_index() -> None:
    from faultline.pipeline_v2.extractors.base import AnchorCandidate

    cand = AnchorCandidate(
        name="cases",
        paths=("backend/routers/cases.py",),
        source="fastapi-route",
        confidence_self=0.9,
        routes=(
            ("/api/cases", "GET", "backend/routers/cases.py"),
            ("/api/cases/{case_id}", "DELETE", "backend/routers/cases.py"),
        ),
    )
    features = [{"uuid": "u1", "paths": ["backend/routers/cases.py"]}]
    idx = build_routes_index(features, {"fastapi-route": [cand]})
    patterns = {(r["pattern"], r["method"]) for r in idx}
    assert ("/api/cases", "GET") in patterns
    assert ("/api/cases/{case_id}", "DELETE") in patterns
    # owner stamped from feature paths
    assert all(r["feature_uuid"] == "u1" for r in idx)


# ── django classifier tightening ────────────────────────────────────────────


def test_django_false_positive_settings_without_config(tmp_path: Path) -> None:
    # FastAPI repo with a router file named tool_settings.py + a plain
    # settings.py with no Django config → must NOT classify as django.
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies=['fastapi']\n")
    (tmp_path / "tool_settings.py").write_text("VALUE = 1\n")
    (tmp_path / "settings.py").write_text("DEBUG = True\nFOO = 'bar'\n")
    files = ["pyproject.toml", "tool_settings.py", "settings.py"]
    assert _is_django_repo(tmp_path, files, "fastapi") is False
    stack, _signals = detect_stack(tmp_path, files)
    assert stack == "fastapi"


def test_django_ignores_vendored_settings(tmp_path: Path) -> None:
    vendored = "backend/.venv/lib/python3.12/site-packages/x/settings.py"
    p = tmp_path / vendored
    p.parent.mkdir(parents=True)
    p.write_text("INSTALLED_APPS = []\n")  # even with markers, vendored is ignored
    assert _is_django_repo(tmp_path, [vendored], "fastapi") is False


def test_django_true_via_manage_py(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n")
    assert _is_django_repo(tmp_path, ["manage.py"], "") is True
    stack, _ = detect_stack(tmp_path, ["manage.py", "app/urls.py"])
    assert stack == "django"


def test_django_true_via_settings_config(tmp_path: Path) -> None:
    (tmp_path / "myproj").mkdir()
    (tmp_path / "myproj" / "settings.py").write_text(
        "INSTALLED_APPS = ['django.contrib.admin']\n"
    )
    assert _is_django_repo(
        tmp_path, ["myproj/settings.py"], "",
    ) is True


def test_django_true_via_dep(tmp_path: Path) -> None:
    assert _is_django_repo(tmp_path, [], "django==5.0") is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q", "--no-cov"]))
