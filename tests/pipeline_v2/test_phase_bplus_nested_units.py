"""Phase B+ — nested workspace discovery + per-scan-unit profile selection.

Covers the four contracts of the polar-class hybrid work:

  1. Nested-manifest discovery (Tier A: nested pnpm-workspace.yaml /
     package.json workspaces; Tier B: colocated app roots at depth>=2)
     wired into Stage 0 intake as a fallback ONLY when root detection
     found nothing.
  2. Per-unit profile selection: each 0.6b unit gets its own profile;
     unrecognised units get the DefaultProfile; uniform assignments
     return the whole-repo winner UNCHANGED (identity — the G4 path).
  3. CompositeProfile dispatch mechanics (longest-prefix ownership,
     scoped flow entries, merged attribution rules, scoped Stage-1
     overrides).
  4. Per-unit repo_class refinement (confident product-app unit
     overrides a non-product whole-repo verdict; the fail-open
     residual verdict never does).

All fixtures live in tmp_path; intake runs with ``skip_git=True``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from faultline.pipeline_v2.nested_workspaces import discover_nested_workspaces
from faultline.pipeline_v2.profiles import (
    CompositeProfile,
    select_scan_profile,
)
from faultline.pipeline_v2.stage_0_intake import stage_0_intake


# ── fixture builders ─────────────────────────────────────────────────────


def _w(root: Path, rel: str, content: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def build_polar_shape(root: Path) -> None:
    """FastAPI backend at server/ + Next monorepo under clients/
    (pnpm-workspace.yaml NOT at the repo root)."""
    _w(root, "package.json", json.dumps({"private": True}))
    # backend
    _w(root, "server/pyproject.toml", textwrap.dedent("""\
        [project]
        name = "polarish"
        dependencies = ["fastapi>=0.100", "uvicorn[standard]"]
        """))
    _w(root, "server/app/main.py", textwrap.dedent("""\
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(None)
        """))
    _w(root, "server/app/checkout/endpoints.py", textwrap.dedent("""\
        from fastapi import APIRouter
        router = APIRouter(prefix="/checkout")

        @router.get("/session")
        async def get_session():
            return {}
        """))
    _w(root, "server/app/checkout/service.py", "def create():\n    return 1\n")
    # nested pnpm workspace
    _w(root, "clients/pnpm-workspace.yaml", "packages:\n  - \"apps/*\"\n")
    _w(root, "clients/apps/web/package.json", json.dumps({
        "name": "@polarish/web", "dependencies": {"next": "15", "react": "19"},
    }))
    _w(root, "clients/apps/web/app/dashboard/page.tsx",
       "export default function Page() { return null }\n")
    _w(root, "clients/apps/web/app/dashboard/panel.tsx", "export const P = 1\n")
    _w(root, "clients/apps/web/app/layout.tsx", "export default () => null\n")
    _w(root, "clients/apps/lib-only/package.json", json.dumps({
        "name": "@polarish/lib-only", "main": "index.js",
    }))
    _w(root, "clients/apps/lib-only/index.js", "module.exports = {}\n")


def build_dispatch_shape(root: Path) -> None:
    """FastAPI backend rooted at the repo + a Vue SPA colocated DEEP
    inside the source tree (depth 4)."""
    _w(root, "pyproject.toml", textwrap.dedent("""\
        [project]
        name = "dispatchish"
        dependencies = ["fastapi>=0.100"]
        """))
    _w(root, "src/dispatch/main.py", textwrap.dedent("""\
        from fastapi import FastAPI
        app = FastAPI()
        """))
    _w(root, "src/dispatch/incident/endpoints.py", textwrap.dedent("""\
        from fastapi import APIRouter
        router = APIRouter(prefix="/incidents")

        @router.get("")
        async def list_incidents():
            return []
        """))
    _w(root, "src/dispatch/incident/service.py", "def get():\n    return 1\n")
    spa = "src/dispatch/static/dispatch"
    _w(root, f"{spa}/package.json", json.dumps({
        "name": "dispatch-spa", "dependencies": {"vue": "3"},
    }))
    _w(root, f"{spa}/vite.config.js", "export default {}\n")
    _w(root, f"{spa}/index.html", "<html></html>\n")
    _w(root, f"{spa}/src/main.js", "import { createApp } from 'vue'\n")
    _w(root, f"{spa}/src/routes/index.js", "export default []\n")


def build_traefik_shape(root: Path) -> None:
    """Go daemon with a TOP-LEVEL (depth-1) SPA build dir — must NOT be
    discovered (the pinned-inertness class)."""
    _w(root, "go.mod", "module traefikish\n")
    _w(root, "main.go", "package main\nfunc main() {}\n")
    _w(root, "webui/package.json", json.dumps({
        "name": "webui", "dependencies": {"vue": "3"},
    }))
    _w(root, "webui/vite.config.js", "export default {}\n")
    _w(root, "webui/index.html", "<html></html>\n")
    _w(root, "webui/src/main.js", "import { createApp } from 'vue'\n")


def build_plane_shape(root: Path) -> None:
    """Django backend workspace + a Next Pages workspace under a ROOT
    pnpm workspace manifest (per-unit selection, no nested discovery)."""
    _w(root, "pnpm-workspace.yaml", "packages:\n  - \"apps/*\"\n")
    _w(root, "package.json", json.dumps({"private": True}))
    # root python manifest (the usual django-monorepo shape) — this is
    # also what keeps the whole-repo SPA grades of next-pages-react
    # disabled (non-JS root manifest), mirroring real plane where the
    # backend owns the repo-level story.
    _w(root, "requirements.txt", "-r apps/api/requirements.txt\n")
    # django backend app
    _w(root, "apps/api/requirements.txt", "django==5.0\n")
    _w(root, "apps/api/manage.py", textwrap.dedent("""\
        import os
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "conf.settings")
        from django.core.management import execute_from_command_line
        execute_from_command_line([])
        """))
    _w(root, "apps/api/conf/settings.py",
       "INSTALLED_APPS = [\n    'issues',\n]\n")
    _w(root, "apps/api/conf/urls.py", textwrap.dedent("""\
        from django.urls import path
        from issues import views
        urlpatterns = [path("issues/", views.index)]
        """))
    _w(root, "apps/api/issues/apps.py", "class IssuesConfig:\n    pass\n")
    _w(root, "apps/api/issues/models.py", "class Issue:\n    pass\n")
    _w(root, "apps/api/issues/views.py", "def index(request):\n    return None\n")
    # react-router (library mode) SPA frontend app — the class real
    # plane's web/space/admin belong to. Whole-repo, django's 0.9
    # (dep + grammar) outscores the SPA's 0.85; per-unit, the SPA tree
    # selects next-pages-react.
    _w(root, "apps/web/package.json", json.dumps({
        "name": "web",
        "dependencies": {"react": "19", "react-dom": "19",
                         "react-router-dom": "6"},
    }))
    _w(root, "apps/web/src/routes/index.tsx", textwrap.dedent("""\
        import { Routes, Route } from "react-router-dom";
        import Issues from "../views/Issues";
        export default function AppRoutes() {
          return (
            <Routes>
              <Route path="/issues" element={<Issues />} />
            </Routes>
          );
        }
        """))
    _w(root, "apps/web/src/views/Issues.tsx",
       "export default function Issues(){ return null }\n")


def build_uniform_next_monorepo(root: Path) -> None:
    """Two Next App-Router apps — per-unit selection must agree with the
    whole-repo winner and return it UNCHANGED (identity)."""
    _w(root, "pnpm-workspace.yaml", "packages:\n  - \"apps/*\"\n")
    _w(root, "package.json", json.dumps({"private": True}))
    for app in ("web", "admin"):
        _w(root, f"apps/{app}/package.json", json.dumps({
            "name": app, "dependencies": {"next": "15", "react": "19"},
        }))
        _w(root, f"apps/{app}/app/home/page.tsx", "export default ()=>null\n")
        _w(root, f"apps/{app}/app/home/view.tsx", "export const V=1\n")


# ── 1. nested-manifest discovery ─────────────────────────────────────────


class TestNestedDiscovery:
    def test_polar_shape_nested_pnpm_manifest_yields_workspaces(self, tmp_path):
        build_polar_shape(tmp_path)
        ctx = stage_0_intake(tmp_path, skip_git=True)
        assert ctx.monorepo is True
        assert ctx.workspace_manager == "pnpm"
        paths = {ws.path for ws in ctx.workspaces or []}
        assert "clients/apps/web" in paths
        web = next(ws for ws in ctx.workspaces if ws.path == "clients/apps/web")
        assert web.stack == "next-app-router"
        assert any(f.endswith("page.tsx") for f in web.files)
        # nested discovery is surfaced in the stack signals
        assert any("nested workspace manifest" in s for s in ctx.stack_signals)

    def test_dispatch_shape_colocated_spa_becomes_workspace(self, tmp_path):
        build_dispatch_shape(tmp_path)
        ctx = stage_0_intake(tmp_path, skip_git=True)
        assert ctx.monorepo is True
        assert ctx.workspace_manager == "colocated"
        paths = {ws.path for ws in ctx.workspaces or []}
        assert paths == {"src/dispatch/static/dispatch"}

    def test_traefik_shape_depth1_spa_not_discovered(self, tmp_path):
        build_traefik_shape(tmp_path)
        ctx = stage_0_intake(tmp_path, skip_git=True)
        assert ctx.monorepo is False
        assert ctx.workspaces is None

    def test_root_manifest_wins_nested_discovery_never_runs(self, tmp_path):
        build_uniform_next_monorepo(tmp_path)
        # plant a nested manifest that must be IGNORED (root declared).
        _w(tmp_path, "apps/web/inner/pnpm-workspace.yaml",
           "packages:\n  - \"x/*\"\n")
        ctx = stage_0_intake(tmp_path, skip_git=True)
        paths = {ws.path for ws in ctx.workspaces or []}
        assert paths == {"apps/web", "apps/admin"}

    def test_noise_segments_never_host_discovery(self, tmp_path):
        _w(tmp_path, "go.mod", "module x\n")
        _w(tmp_path, "examples/demo/pnpm-workspace.yaml",
           "packages:\n  - \"apps/*\"\n")
        _w(tmp_path, "examples/demo/apps/a/package.json", "{}")
        _w(tmp_path, "docs/site/app/package.json", json.dumps(
            {"dependencies": {"vue": "3"}}))
        _w(tmp_path, "docs/site/app/vite.config.js", "export default {}\n")
        ctx = stage_0_intake(tmp_path, skip_git=True)
        assert ctx.monorepo is False

    def test_discovery_is_input_order_invariant(self, tmp_path):
        build_polar_shape(tmp_path)
        ctx = stage_0_intake(tmp_path, skip_git=True)
        fwd, sig_f = discover_nested_workspaces(
            tmp_path, list(ctx.tracked_files),
        )
        rev, sig_r = discover_nested_workspaces(
            tmp_path, list(reversed(ctx.tracked_files)),
        )
        assert [(p.name, p.path, sorted(p.files)) for p in fwd.packages] == [
            (p.name, p.path, sorted(p.files)) for p in rev.packages
        ]
        assert sig_f == sig_r


# ── 2. per-unit profile selection ────────────────────────────────────────


class TestPerUnitSelection:
    def test_polar_shape_composite_backend_fastapi_frontend_next(self, tmp_path):
        build_polar_shape(tmp_path)
        ctx = stage_0_intake(tmp_path, skip_git=True)
        prof = select_scan_profile(ctx)
        assert isinstance(prof, CompositeProfile)
        assert prof.root_profile_name == "fastapi-family"
        assert ("clients/apps/web", "next-app-router") in prof.unit_assignments
        # per-path dispatch: frontend file → Next boundary slug,
        # backend file → FastAPI domain-package slug.
        assert prof.feature_of(
            "clients/apps/web/app/dashboard/page.tsx", ctx,
        ) == "dashboard"
        assert prof.feature_of(
            "server/app/checkout/service.py", ctx,
        ) == "checkout"

    def test_dispatch_shape_spa_unit_gets_default_profile(self, tmp_path):
        build_dispatch_shape(tmp_path)
        ctx = stage_0_intake(tmp_path, skip_git=True)
        prof = select_scan_profile(ctx)
        assert isinstance(prof, CompositeProfile)
        assert prof.root_profile_name == "fastapi-family"
        assert prof.unit_assignments == (
            ("src/dispatch/static/dispatch", "default"),
        )
        # the SPA unit's default profile claims nothing…
        assert prof.feature_of(
            "src/dispatch/static/dispatch/src/main.js", ctx,
        ) is None
        # …while the backend keeps its FastAPI domain attribution.
        assert prof.feature_of(
            "src/dispatch/incident/service.py", ctx,
        ) == "incident"

    def test_plane_shape_django_root_next_pages_unit(self, tmp_path):
        build_plane_shape(tmp_path)
        ctx = stage_0_intake(tmp_path, skip_git=True)
        prof = select_scan_profile(ctx)
        assert isinstance(prof, CompositeProfile)
        assert prof.root_profile_name == "django"
        assert ("apps/web", "next-pages-react") in prof.unit_assignments
        assert prof.feature_of("apps/web/src/views/Issues.tsx", ctx) == "issues"
        assert prof.feature_of("apps/api/issues/models.py", ctx) == "issues"

    def test_uniform_monorepo_returns_whole_repo_winner_unchanged(self, tmp_path):
        build_uniform_next_monorepo(tmp_path)
        ctx = stage_0_intake(tmp_path, skip_git=True)
        prof = select_scan_profile(ctx)
        assert not isinstance(prof, CompositeProfile)
        assert prof.name == "next-app-router"

    def test_single_package_repo_is_untouched(self, tmp_path):
        _w(tmp_path, "pyproject.toml",
           "[project]\nname='x'\ndependencies=['fastapi']\n")
        _w(tmp_path, "app/main.py",
           "from fastapi import FastAPI\napp = FastAPI()\n")
        ctx = stage_0_intake(tmp_path, skip_git=True)
        prof = select_scan_profile(ctx)
        assert not isinstance(prof, CompositeProfile)
        assert prof.name == "fastapi-family"

    def test_selection_deterministic_across_hash_seeds(self, tmp_path):
        build_polar_shape(tmp_path)
        snippet = textwrap.dedent("""\
            import json, sys
            from faultline.pipeline_v2.stage_0_intake import stage_0_intake
            from faultline.pipeline_v2.profiles import select_scan_profile
            ctx = stage_0_intake(sys.argv[1], skip_git=True)
            prof = select_scan_profile(ctx)
            print(json.dumps({
                "name": prof.name,
                "units": list(getattr(prof, "unit_assignments", ()) or ()),
                "workspaces": sorted(w.path for w in ctx.workspaces or []),
            }, sort_keys=True))
            """)
        outs = []
        for seed in ("0", "424242"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = subprocess.run(
                [sys.executable, "-c", snippet, str(tmp_path)],
                capture_output=True, text=True, env=env, check=True,
                timeout=120,
            )
            outs.append(proc.stdout.strip().splitlines()[-1])
        assert outs[0] == outs[1]
        assert "next-app-router" in outs[0]


# ── 3. composite mechanics (fake profiles) ──────────────────────────────


class _FakeProfile:
    def __init__(self, name, claim=None, entries=(), spec=None):
        from faultline.pipeline_v2.profiles import AttributionSpec

        self.name = name
        self._claim = claim or {}
        self._entries = list(entries)
        self._spec = spec or AttributionSpec()

    def detects(self, ctx):
        return 0.9

    def workspaces(self, ctx):
        return []

    def classify_file(self, path):
        from faultline.pipeline_v2.profiles import FileRole

        return FileRole.LIB if path in self._claim else FileRole.UNKNOWN

    def feature_of(self, path, ctx):
        return self._claim.get(path)

    def flow_entries(self, ctx):
        from faultline.pipeline_v2.profiles import FlowEntry

        return [
            FlowEntry(path=p, symbol=s, kind="page", route=r)
            for (p, s, r) in self._entries
            if p in set(ctx.tracked_files)
        ]

    def attribution_rules(self):
        return self._spec


def _mini_ctx(tmp_path, files):
    for f in files:
        _w(tmp_path, f)
    return stage_0_intake(tmp_path, skip_git=True)


class TestCompositeMechanics:
    def test_longest_prefix_dispatch_and_scoped_flow_entries(self, tmp_path):
        from faultline.pipeline_v2.profiles import AttributionSpec, FileRole

        ctx = _mini_ctx(tmp_path, ["a/x.py", "web/p.tsx", "web/deep/q.tsx"])
        root = _FakeProfile(
            "rootp",
            claim={"a/x.py": "alpha"},
            entries=[("a/x.py", "main", "/a"), ("web/p.tsx", "leak", "/leak")],
            spec=AttributionSpec(shared_roles=(FileRole.LIB,), max_fanout=5),
        )
        unit = _FakeProfile(
            "unitp",
            claim={"web/p.tsx": "pages"},
            entries=[("web/p.tsx", "P", "/p")],
            spec=AttributionSpec(shared_roles=(FileRole.HOOK,), max_fanout=3),
        )
        comp = CompositeProfile(root=root, units=(("web", unit),))
        assert comp.feature_of("web/p.tsx", ctx) == "pages"
        assert comp.feature_of("a/x.py", ctx) == "alpha"
        # the root profile NEVER sees unit files (its /leak entry is
        # filtered out because the residual scope excludes web/).
        routes = {e.route for e in comp.flow_entries(ctx)}
        assert routes == {"/p", "/a"}
        # merged policy: union of shared roles, most conservative cap.
        spec = comp.attribution_rules()
        assert set(spec.shared_roles) == {FileRole.LIB, FileRole.HOOK}
        assert spec.max_fanout == 3

    def test_scoped_stage1_overrides(self, tmp_path):
        ctx = _mini_ctx(tmp_path, ["a/x.py", "web/p.tsx"])

        class _Ext:
            name = "probe"

            def __init__(self):
                self.seen: list[list[str]] = []

            def extract(self, scoped):
                self.seen.append(sorted(scoped.tracked_files))
                return []

        inner = _Ext()
        unit = _FakeProfile("unitp")
        unit.stage_1_extractor_overrides = lambda c: [inner]  # type: ignore[attr-defined]
        comp = CompositeProfile(
            root=_FakeProfile("rootp"), units=(("web", unit),),
        )
        overrides = comp.stage_1_extractor_overrides(ctx)
        assert [o.name for o in overrides] == ["probe"]
        overrides[0].extract(ctx)
        assert inner.seen == [["web/p.tsx"]]


# ── 4. per-unit repo_class refinement ────────────────────────────────────


class TestRepoClassPerUnit:
    def _ctx(self, tmp_path):
        build_dispatch_shape(tmp_path)
        return stage_0_intake(tmp_path, skip_git=True)

    def test_confident_product_unit_overrides_non_product_whole(
        self, tmp_path, monkeypatch,
    ):
        from faultline.pipeline_v2 import stage_0_7_repo_class as s07

        ctx = self._ctx(tmp_path)
        whole_files = len(ctx.tracked_files)

        def fake_classify(c, *, classifiers=None, signals=None):
            if len(c.tracked_files) == whole_files:
                return s07.RepoClassVerdict(
                    repo_class=s07.REPO_CLASS_LIBRARY,
                    confidence=0.9, rationale="whole", matched_signals=(),
                )
            return s07.RepoClassVerdict(
                repo_class=s07.REPO_CLASS_PRODUCT_APP,
                confidence=0.95, rationale="unit", matched_signals=("u",),
            )

        monkeypatch.setattr(s07, "classify_repo_class", fake_classify)
        verdict = s07.classify_repo_class_per_unit(ctx)
        assert verdict.repo_class == s07.REPO_CLASS_PRODUCT_APP
        assert "per-unit" in verdict.matched_signals
        assert "unit:src/dispatch/static/dispatch" in verdict.matched_signals

    def test_residual_unit_verdict_never_overrides(self, tmp_path, monkeypatch):
        from faultline.pipeline_v2 import stage_0_7_repo_class as s07

        ctx = self._ctx(tmp_path)
        whole_files = len(ctx.tracked_files)

        def fake_classify(c, *, classifiers=None, signals=None):
            if len(c.tracked_files) == whole_files:
                return s07.RepoClassVerdict(
                    repo_class=s07.REPO_CLASS_LIBRARY,
                    confidence=0.9, rationale="whole", matched_signals=(),
                )
            return s07.RepoClassVerdict(
                repo_class=s07.REPO_CLASS_PRODUCT_APP,
                confidence=s07.CONF_RESIDUAL, rationale="residual",
                matched_signals=("residual",),
            )

        monkeypatch.setattr(s07, "classify_repo_class", fake_classify)
        verdict = s07.classify_repo_class_per_unit(ctx)
        assert verdict.repo_class == s07.REPO_CLASS_LIBRARY

    def test_product_whole_verdict_short_circuits(self, tmp_path):
        from faultline.pipeline_v2 import stage_0_7_repo_class as s07

        ctx = self._ctx(tmp_path)
        whole = s07.classify_repo_class(ctx)
        refined = s07.classify_repo_class_per_unit(ctx)
        if whole.repo_class == s07.REPO_CLASS_PRODUCT_APP:
            assert refined == whole


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
