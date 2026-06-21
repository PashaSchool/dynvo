"""Tests for Stage 0.6b — per-workspace project classifier + partition plan.

Synthetic fixtures ONLY (neutral names — see memory:
rule-no-repo-specific-paths). Every manifest + folder is materialized
under ``tmp_path`` so :meth:`ProjectSignals.collect` reads real files.
No slices from real corpus repos; no magic-number thresholds asserted.

Scale-invariance is checked explicitly (tiny / medium / large workspace
counts) per memory: rule-no-magic-tuning.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.pipeline_v2.stage_0_6_project_classifier import (
    MIN_WORKSPACES_FOR_PARTITION,
    AppClassifier,
    ExampleClassifier,
    LibClassifier,
    PartitionPlan,
    ProjectClassification,
    ProjectClassifier,
    ProjectSignals,
    ResidualClassifier,
    ServiceClassifier,
    ToolClassifier,
    classify_project,
    partition_monorepo,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace


# ── Synthetic repo builders ─────────────────────────────────────────────


def _write(root: Path, rel: str, content: str = "") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _pkg(name: str, **fields: object) -> str:
    data: dict[str, object] = {"name": name}
    data.update(fields)
    return json.dumps(data)


def _make_workspace(
    root: Path,
    path: str,
    *,
    name: str | None = None,
    package_json: dict[str, object] | None = None,
    files: list[str] | None = None,
) -> Workspace:
    """Materialize a workspace dir under ``root`` and return its Workspace.

    ``package_json`` is written to disk AND attached to the Workspace
    (mirroring how Stage 0 pre-parses it).
    """
    (root / path).mkdir(parents=True, exist_ok=True)
    if package_json is not None:
        _write(root, f"{path}/package.json", json.dumps(package_json))
    return Workspace(
        name=name if name is not None else path.split("/")[-1],
        path=path,
        package_json=package_json,
        stack=None,
        files=files or [],
    )


def _ctx(root: Path, workspaces: list[Workspace] | None) -> ScanContext:
    ctx = ScanContext(
        repo_path=Path(root),
        stack=None,
        monorepo=bool(workspaces and len(workspaces) >= MIN_WORKSPACES_FOR_PARTITION),
        workspaces=workspaces,
        tracked_files=[],
        commits=[],
        run_dir=None,
    )
    return ctx


def _classify(root: Path, ws: Workspace) -> str:
    return classify_project(Path(root), ws).project_type


# ── Per-project-type classification (synthetic manifests) ────────────────


class TestProjectTypeClassification:
    def test_app_requires_client_dep_and_route_dir(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            "apps/webapp",
            name="webapp",
            package_json={"dependencies": {"next": "14", "react": "18"}},
        )
        _write(tmp_path, "apps/webapp/app/page.tsx", "export default () => null")
        assert _classify(tmp_path, ws) == "app"

    def test_client_dep_without_routes_is_not_app(self, tmp_path):
        # A package that depends on react but has no routes dir is a
        # backend/lib, NOT an app (mirrors a NestJS server that renders
        # email templates with react).
        ws = _make_workspace(
            tmp_path,
            "packages/server",
            name="server",
            package_json={
                "dependencies": {"react": "18", "@nestjs/core": "10"},
            },
        )
        assert _classify(tmp_path, ws) == "service"

    def test_routes_without_client_dep_is_not_app(self, tmp_path):
        # A bare routes dir without a client framework dep does not
        # qualify as an app (avoids false-firing on a backend that
        # happens to have a ``routes/`` folder).
        ws = _make_workspace(
            tmp_path,
            "packages/thing",
            name="thing",
            package_json={"main": "index.js"},
        )
        _write(tmp_path, "packages/thing/routes/a.ts", "")
        assert _classify(tmp_path, ws) == "lib"

    def test_service_from_server_dep_with_entry(self, tmp_path):
        # A server-framework dep marks a service ONLY together with a real
        # server ENTRY POINT (src/main.ts). This is the express analogue of
        # the NestJS bootstrap: a private backend that boots a process.
        ws = _make_workspace(
            tmp_path,
            "apps/backend",
            name="backend",
            package_json={"dependencies": {"express": "4"}},
        )
        _write(tmp_path, "apps/backend/src/main.ts", "createServer().listen(3000)\n")
        assert _classify(tmp_path, ws) == "service"

    def test_server_dep_without_entry_is_not_service(self, tmp_path):
        # A framework dep with NO server entry is NOT a service: it is an
        # adapter/util library or a test harness that pulls the framework in
        # only as a devDependency. (Demotes trpc packages/tests + the
        # @trpc/server adapter lib from the old "not published => service"
        # heuristic.) A private, export-less package with fastify but no
        # bootstrap falls through to the residual lib ride-along.
        ws = _make_workspace(
            tmp_path,
            "packages/harness",
            name="harness",
            package_json={"private": True, "devDependencies": {"fastify": "4"}},
        )
        assert _classify(tmp_path, ws) != "service"

    def test_adapter_lib_with_framework_dep_and_bin_is_lib(self, tmp_path):
        # A published adapter library depends on a server framework AND ships
        # a small bin helper, but has NO server bootstrap — it must classify
        # as lib, not service (the trpc @trpc/server / @trpc/next case).
        ws = _make_workspace(
            tmp_path,
            "packages/adapter",
            name="server-adapter",
            package_json={
                "main": "./dist/index.js",
                "exports": {".": "./dist/index.js"},
                "bin": {"intent": "./bin/intent.js"},
                "dependencies": {"express": "4", "fastify": "4"},
            },
        )
        assert _classify(tmp_path, ws) == "lib"

    def test_service_with_compiled_main_and_entry_is_service(self, tmp_path):
        # A real backend service legitimately sets "main" to its compiled
        # output AND has a src/main.ts bootstrap (the infisical backend
        # shape). The "main" field must NOT demote it to a library — the
        # server entry is the decisive signal.
        ws = _make_workspace(
            tmp_path,
            "backend",
            name="backend",
            package_json={
                "main": "./dist/main.mjs",
                "dependencies": {"fastify": "4", "react": "18", "react-dom": "18"},
            },
        )
        _write(tmp_path, "backend/src/main.ts", "bootstrap()\n")
        assert _classify(tmp_path, ws) == "service"

    def test_service_from_nestjs_core_even_with_exports(self, tmp_path):
        # @nestjs/core is a definitive server-runtime dep: a service even
        # when the package also exposes library exports.
        ws = _make_workspace(
            tmp_path,
            "apps/api",
            name="api",
            package_json={
                "main": "dist/main.js",
                "dependencies": {"@nestjs/core": "10"},
            },
        )
        assert _classify(tmp_path, ws) == "service"

    def test_nestjs_common_only_is_lib_not_service(self, tmp_path):
        # @nestjs/common (decorators/DTOs) is a LIBRARY dep — a types
        # package that imports it but exports a public API is a lib, not
        # a service.
        ws = _make_workspace(
            tmp_path,
            "packages/types",
            name="platform-types",
            package_json={
                "main": "index.ts",
                "exports": {".": "./index.ts"},
                "dependencies": {"@nestjs/common": "10", "@nestjs/swagger": "7"},
            },
        )
        assert _classify(tmp_path, ws) == "lib"

    def test_service_from_fastapi_factory(self, tmp_path):
        ws = _make_workspace(tmp_path, "services/py", name="py-svc")
        _write(tmp_path, "services/py/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        assert _classify(tmp_path, ws) == "service"

    def test_service_from_go_cmd_main(self, tmp_path):
        ws = _make_workspace(tmp_path, "services/gosvc", name="gosvc")
        _write(tmp_path, "services/gosvc/cmd/server/main.go", "package main\nfunc main(){}\n")
        assert _classify(tmp_path, ws) == "service"

    def test_lib_from_exports(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            "packages/uikit",
            name="uikit",
            package_json={
                "main": "dist/index.js",
                "exports": {".": "./dist/index.js"},
                "dependencies": {"react": "18"},
            },
        )
        assert _classify(tmp_path, ws) == "lib"

    def test_lib_with_build_scripts_dir_stays_lib(self, tmp_path):
        # A published library that ships a ``scripts/`` build dir must
        # NOT be reclassified as a tool — exports win over a script dir.
        ws = _make_workspace(
            tmp_path,
            "packages/widgets",
            name="widgets",
            package_json={"exports": {".": "./index.js"}},
        )
        _write(tmp_path, "packages/widgets/scripts/build.js", "")
        assert _classify(tmp_path, ws) == "lib"

    def test_lib_with_codegen_bin_field_stays_lib(self, tmp_path):
        # An SDK that ships a codegen ``bin`` AND a library export surface
        # is primarily a library.
        ws = _make_workspace(
            tmp_path,
            "packages/sdk",
            name="my-sdk",
            package_json={
                "exports": {".": "./index.js"},
                "bin": {"gen": "./bin/gen.js"},
            },
        )
        assert _classify(tmp_path, ws) == "lib"

    def test_tool_from_bin_field_without_exports(self, tmp_path):
        # A pure CLI with a bin and NO library exports is a tool.
        ws = _make_workspace(
            tmp_path,
            "packages/runner",
            name="runner",
            package_json={"bin": {"run": "./cli.js"}},
        )
        assert _classify(tmp_path, ws) == "tool"

    def test_tool_from_config_name_even_with_exports(self, tmp_path):
        # A *-config package exports a config object but is tooling.
        ws = _make_workspace(
            tmp_path,
            "packages/tailwind-config",
            name="@org/tailwind-config",
            package_json={"main": "index.js", "exports": {".": "./index.js"}},
        )
        assert _classify(tmp_path, ws) == "tool"

    @pytest.mark.parametrize(
        "name",
        ["tsconfig", "eslint-config-acme", "@org/prettier-config", "thing-cli", "lint-rules"],
    )
    def test_tool_name_conventions(self, tmp_path, name):
        ws = _make_workspace(
            tmp_path,
            f"packages/{name.split('/')[-1]}",
            name=name,
            package_json={"exports": {".": "./i.js"}},
        )
        assert _classify(tmp_path, ws) == "tool"

    @pytest.mark.parametrize(
        "segment",
        ["examples", "example", "e2e", "fixtures", "demos", "playground", "templates", "sandbox"],
    )
    def test_example_path_segments(self, tmp_path, segment):
        ws = _make_workspace(
            tmp_path,
            f"{segment}/sample-app",
            name="sample-app",
            package_json={"dependencies": {"next": "14"}},
        )
        _write(tmp_path, f"{segment}/sample-app/app/page.tsx", "")
        # Even though it has app signals, the example segment wins.
        assert _classify(tmp_path, ws) == "example"

    def test_example_segment_must_be_whole_segment(self, tmp_path):
        # A dir merely CONTAINING the word "examples" is NOT an example —
        # the rule matches whole path segments only.
        ws = _make_workspace(
            tmp_path,
            "packages/examples-viewer",
            name="examples-viewer",
            package_json={"dependencies": {"next": "14"}},
        )
        _write(tmp_path, "packages/examples-viewer/app/page.tsx", "")
        assert _classify(tmp_path, ws) == "app"

    def test_residual_defaults_to_lib(self, tmp_path):
        # Manifest present but no exports/routes/server/tool signal.
        ws = _make_workspace(
            tmp_path,
            "packages/internal",
            name="internal",
            package_json={"private": True},
        )
        assert _classify(tmp_path, ws) == "lib"

    def test_residual_tool_name_defaults_to_tool(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            "packages/config",
            name="config",
            package_json={"private": True},
        )
        assert _classify(tmp_path, ws) == "tool"


# ── Partition plan ───────────────────────────────────────────────────────


class TestPartitionPlan:
    def test_single_app_plus_service_units(self, tmp_path):
        front = _make_workspace(
            tmp_path, "apps/web", name="web",
            package_json={"dependencies": {"next": "14", "react": "18"}},
        )
        _write(tmp_path, "apps/web/app/page.tsx", "")
        api = _make_workspace(
            tmp_path, "apps/api", name="api",
            package_json={"dependencies": {"@nestjs/core": "10"}},
        )
        lib = _make_workspace(
            tmp_path, "packages/ui", name="ui",
            package_json={"exports": {".": "./i.js"}},
        )
        cfg = _make_workspace(
            tmp_path, "packages/tsconfig", name="tsconfig",
            package_json={"private": True},
        )
        plan = partition_monorepo(_ctx(tmp_path, [front, api, lib, cfg]))
        assert plan.is_monorepo is True
        unit_paths = {u.subpath for u in plan.units}
        assert unit_paths == {"apps/web", "apps/api"}
        # lib + tool are excluded (ride-along / tooling)
        excluded_paths = {e.path for e in plan.excluded}
        assert "packages/ui" in excluded_paths
        assert "packages/tsconfig" in excluded_paths
        # subpaths() feeds run_pipeline_multi
        assert sorted(plan.subpaths()) == ["apps/api", "apps/web"]

    def test_examples_excluded_from_units(self, tmp_path):
        app = _make_workspace(
            tmp_path, "apps/web", name="web",
            package_json={"dependencies": {"next": "14"}},
        )
        _write(tmp_path, "apps/web/app/page.tsx", "")
        ex = _make_workspace(
            tmp_path, "examples/demo", name="demo",
            package_json={"dependencies": {"next": "14"}},
        )
        _write(tmp_path, "examples/demo/app/page.tsx", "")
        e2e = _make_workspace(
            tmp_path, "e2e/suite", name="e2e-suite",
            package_json={"dependencies": {"@playwright/test": "1"}},
        )
        plan = partition_monorepo(_ctx(tmp_path, [app, ex, e2e]))
        assert {u.subpath for u in plan.units} == {"apps/web"}
        ex_kinds = {e.path: e.type for e in plan.excluded}
        assert ex_kinds["examples/demo"] == "example"
        assert ex_kinds["e2e/suite"] == "example"

    def test_lib_rides_along_when_app_present(self, tmp_path):
        app = _make_workspace(
            tmp_path, "apps/web", name="web",
            package_json={"dependencies": {"next": "14"}},
        )
        _write(tmp_path, "apps/web/app/page.tsx", "")
        lib = _make_workspace(
            tmp_path, "packages/core", name="core",
            package_json={"exports": {".": "./i.js"}},
        )
        plan = partition_monorepo(_ctx(tmp_path, [app, lib]))
        assert {u.subpath for u in plan.units} == {"apps/web"}
        lib_ex = next(e for e in plan.excluded if e.path == "packages/core")
        assert lib_ex.type == "lib"
        assert "rides along" in lib_ex.reason

    def test_library_monorepo_no_app_service_collapses_to_whole_repo(self, tmp_path):
        # No app/service anywhere -> a publishable multi-package LIBRARY
        # monorepo collapses to a SINGLE whole-repo unit (subpath=None), it
        # does NOT explode into one-unit-per-lib. (Fix 2c: emitting N
        # lib-units was the inverse of the cal.com 219->3 win — 24 units on
        # meilisearch, 83 on lobe-chat.) The libs are recorded as ride-along.
        a = _make_workspace(
            tmp_path, "packages/a", name="a",
            package_json={"exports": {".": "./i.js"}},
        )
        b = _make_workspace(
            tmp_path, "packages/b", name="b",
            package_json={"exports": {".": "./i.js"}},
        )
        plan = partition_monorepo(_ctx(tmp_path, [a, b]))
        assert plan.is_monorepo is True
        assert {u.subpath for u in plan.units} == {None}
        assert plan.units[0].project_type == "repo"
        assert plan.subpaths() == []
        # both libs are recorded as ride-along (never silently dropped)
        ride = {e.path: e.reason for e in plan.excluded}
        assert set(ride) == {"packages/a", "packages/b"}
        assert all("ride" in r for r in ride.values())

    def test_nested_units_collapse_to_shallowest(self, tmp_path):
        # apps/api (service) + apps/api/v1 (also a unit) -> only apps/api
        # survives; v1 rides inside its tree (no double scan). Both express
        # packages carry a src/main.ts server entry so they qualify as
        # services under the entry-point rule.
        parent = _make_workspace(
            tmp_path, "apps/api", name="api",
            package_json={"dependencies": {"express": "4"}},
        )
        _write(tmp_path, "apps/api/src/main.ts", "listen()\n")
        child = _make_workspace(
            tmp_path, "apps/api/v1", name="api-v1",
            package_json={"dependencies": {"express": "4"}},
        )
        _write(tmp_path, "apps/api/v1/src/main.ts", "listen()\n")
        other = _make_workspace(
            tmp_path, "apps/web", name="web",
            package_json={"dependencies": {"next": "14"}},
        )
        _write(tmp_path, "apps/web/app/page.tsx", "")
        plan = partition_monorepo(_ctx(tmp_path, [parent, child, other]))
        unit_paths = {u.subpath for u in plan.units}
        assert unit_paths == {"apps/api", "apps/web"}
        assert "apps/api/v1" not in unit_paths
        nested_ex = next(e for e in plan.excluded if e.path == "apps/api/v1")
        assert "nested" in nested_ex.reason

    def test_sibling_paths_not_treated_as_nested(self, tmp_path):
        # apps/api and apps/api-v2 are SIBLINGS (segment-aware), both
        # units — the prefix-string "apps/api" must not swallow
        # "apps/api-v2". Both carry a server entry so they are services.
        a = _make_workspace(
            tmp_path, "apps/api", name="api",
            package_json={"dependencies": {"express": "4"}},
        )
        _write(tmp_path, "apps/api/src/main.ts", "listen()\n")
        b = _make_workspace(
            tmp_path, "apps/api-v2", name="api-v2",
            package_json={"dependencies": {"fastify": "4"}},
        )
        _write(tmp_path, "apps/api-v2/src/main.ts", "listen()\n")
        plan = partition_monorepo(_ctx(tmp_path, [a, b]))
        assert {u.subpath for u in plan.units} == {"apps/api", "apps/api-v2"}


# ── Single-repo back-compat (NOT a monorepo) ─────────────────────────────


class TestSingleRepoBackCompat:
    def test_no_workspaces_is_whole_repo(self, tmp_path):
        plan = partition_monorepo(_ctx(tmp_path, None))
        assert plan.is_monorepo is False
        assert len(plan.units) == 1
        assert plan.units[0].subpath is None
        # whole-repo plan feeds a single run_pipeline_v2 (subpath=None)
        assert plan.subpaths() == []

    def test_one_workspace_is_whole_repo(self, tmp_path):
        # A single declared workspace is below the plurality floor.
        ws = _make_workspace(
            tmp_path, "apps/only", name="only",
            package_json={"dependencies": {"next": "14"}},
        )
        plan = partition_monorepo(_ctx(tmp_path, [ws]))
        assert plan.is_monorepo is False
        assert plan.units[0].subpath is None

    def test_floor_is_plurality_not_magic(self):
        # The only numeric threshold is the definition of plurality.
        assert MIN_WORKSPACES_FOR_PARTITION == 2

    def test_monorepo_with_only_libs_and_no_app_still_scans(self, tmp_path):
        # Degenerate guard: if everything is a tool/example and there is
        # NO app/service/lib-unit, fall back to a whole-repo unit rather
        # than emit an empty plan.
        t1 = _make_workspace(
            tmp_path, "packages/tsconfig", name="tsconfig",
            package_json={"private": True},
        )
        t2 = _make_workspace(
            tmp_path, "packages/eslint-config", name="eslint-config",
            package_json={"private": True},
        )
        plan = partition_monorepo(_ctx(tmp_path, [t1, t2]))
        # All tools -> no units -> whole-repo fallback.
        assert len(plan.units) == 1
        assert plan.units[0].subpath is None


# ── Scale-invariance (tiny / medium / large) — rule-no-magic-tuning ──────


class TestScaleInvariance:
    def _build(self, tmp_path: Path, n_libs: int) -> ScanContext:
        wss = [
            _make_workspace(
                tmp_path, "apps/web", name="web",
                package_json={"dependencies": {"next": "14"}},
            )
        ]
        _write(tmp_path, "apps/web/app/page.tsx", "")
        for i in range(n_libs):
            wss.append(
                _make_workspace(
                    tmp_path, f"packages/lib{i}", name=f"lib{i}",
                    package_json={"exports": {".": "./i.js"}},
                )
            )
        return _ctx(tmp_path, wss)

    @pytest.mark.parametrize("n_libs", [1, 12, 200])
    def test_one_app_many_libs_yields_one_unit(self, tmp_path, n_libs):
        # Whether the repo has 1, 12, or 200 library packages, the unit
        # set is the single app — the rule does not scale with count
        # (this is the cal.com-style 200-package collapse).
        plan = partition_monorepo(self._build(tmp_path, n_libs))
        assert {u.subpath for u in plan.units} == {"apps/web"}
        # All libs are recorded as ride-along exclusions.
        lib_excl = [e for e in plan.excluded if e.type == "lib"]
        assert len(lib_excl) == n_libs


# ── Protocol / registry injection ────────────────────────────────────────


class TestRegistryInjection:
    def test_custom_classifier_registry(self, tmp_path):
        # A fake classifier substitutable for the real ones (LSP):
        # classify everything as "service".
        class AlwaysService:
            project_type = "service"
            priority = 1

            def classify(self, signals):
                return ProjectClassification(
                    name=signals.name,
                    path=signals.path,
                    project_type="service",
                    confidence=1.0,
                    rationale="fake",
                )

        ws = _make_workspace(
            tmp_path, "packages/x", name="x",
            package_json={"exports": {".": "./i.js"}},
        )
        v = classify_project(Path(tmp_path), ws, classifiers=[AlwaysService()])
        assert v.project_type == "service"

    def test_default_classifiers_satisfy_protocol(self):
        from faultline.pipeline_v2.stage_0_6_project_classifier import (
            _DEFAULT_CLASSIFIERS,
        )

        for clf in _DEFAULT_CLASSIFIERS:
            assert isinstance(clf, ProjectClassifier)

    def test_buggy_classifier_degrades_gracefully(self, tmp_path):
        class Boom:
            project_type = "boom"
            priority = 1

            def classify(self, signals):
                raise RuntimeError("boom")

        ws = _make_workspace(
            tmp_path, "packages/x", name="x",
            package_json={"exports": {".": "./i.js"}},
        )
        # Boom raises -> skipped; real registry tail still classifies.
        v = classify_project(
            Path(tmp_path),
            ws,
            classifiers=[Boom(), LibClassifier(), ResidualClassifier()],
        )
        assert v.project_type == "lib"


# ── Artifact write is opt-in (CLI-mode safe) ─────────────────────────────


class TestArtifactWrite:
    def test_write_partition_artifact_noop_without_run_dir(self, tmp_path):
        from faultline.pipeline_v2.stage_0_6_project_classifier import (
            write_partition_artifact,
        )

        plan = PartitionPlan(is_monorepo=False, units=(), excluded=())
        ctx = _ctx(tmp_path, None)
        ctx.run_dir = None
        # Must not raise and must not write anything.
        write_partition_artifact(ctx, plan)

    def test_write_partition_artifact_writes_with_run_dir(self, tmp_path):
        from faultline.pipeline_v2.stage_0_6_project_classifier import (
            write_partition_artifact,
        )

        app = _make_workspace(
            tmp_path, "apps/web", name="web",
            package_json={"dependencies": {"next": "14"}},
        )
        _write(tmp_path, "apps/web/app/page.tsx", "")
        lib = _make_workspace(
            tmp_path, "packages/ui", name="ui",
            package_json={"exports": {".": "./i.js"}},
        )
        ctx = _ctx(tmp_path, [app, lib])
        run_dir = tmp_path / "logs"
        run_dir.mkdir()
        ctx.run_dir = run_dir
        plan = partition_monorepo(ctx)
        write_partition_artifact(ctx, plan)
        out = run_dir / "06-stage-partition.json"
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["is_monorepo"] is True
        assert any(u["subpath"] == "apps/web" for u in data["units"])


# ── Audit fix 1: no repo-specific package names ──────────────────────────


class TestNoRepoSpecificToolNames:
    def test_tool_name_exact_has_no_repo_specific_names(self):
        # rule-no-repo-specific-paths: ``build-icons`` and ``dev-tools`` are
        # literal supabase package names, NOT industry conventions. They must
        # NOT be hardcoded as tooling-name markers.
        from faultline.pipeline_v2.stage_0_6_project_classifier import (
            _TOOL_NAME_EXACT,
        )

        assert "build-icons" not in _TOOL_NAME_EXACT
        assert "dev-tools" not in _TOOL_NAME_EXACT
        # genuine ecosystem conventions remain
        assert "tsconfig" in _TOOL_NAME_EXACT
        assert "eslint-config" in _TOOL_NAME_EXACT

    def test_build_icons_name_alone_is_not_classified_tool(self, tmp_path):
        # A package merely NAMED build-icons (with a product export, no
        # tooling structure) is NOT tooling — it classifies by its real
        # shape (a published lib here). The old hardcoded name forced it to
        # ``tool``; that repo-specific shortcut is gone.
        ws = _make_workspace(
            tmp_path, "packages/build-icons", name="build-icons",
            package_json={"exports": {".": "./i.js"}},
        )
        assert _classify(tmp_path, ws) == "lib"

    def test_dev_tools_name_alone_is_not_classified_tool(self, tmp_path):
        ws = _make_workspace(
            tmp_path, "packages/dev-tools", name="dev-tools",
            package_json={"main": "./index.js"},
        )
        assert _classify(tmp_path, ws) == "lib"

    def test_genuine_config_name_still_tool(self, tmp_path):
        # The generic ``*-config`` convention still classifies as tool —
        # only the repo-specific names were removed.
        ws = _make_workspace(
            tmp_path, "packages/eslint-config-acme", name="eslint-config-acme",
            package_json={"exports": {".": "./i.js"}},
        )
        assert _classify(tmp_path, ws) == "tool"


# ── Audit fix 2a: Rust binary + server framework -> service ──────────────


class TestRustBinaryService:
    def _crate(
        self,
        tmp_path: Path,
        path: str,
        *,
        cargo: str,
        main_rs: bool = False,
        lib_rs: bool = False,
    ) -> Workspace:
        ws = _make_workspace(tmp_path, path, name=path.split("/")[-1])
        _write(tmp_path, f"{path}/Cargo.toml", cargo)
        if main_rs:
            _write(tmp_path, f"{path}/src/main.rs", "fn main() {}\n")
        if lib_rs:
            _write(tmp_path, f"{path}/src/lib.rs", "pub fn x() {}\n")
        return ws

    def test_rust_crate_with_main_and_server_dep_is_service(self, tmp_path):
        # A crate with src/main.rs AND a server-framework crate dep
        # (actix-web) BOOTS a server -> service (the meilisearch
        # crates/meilisearch shape).
        ws = self._crate(
            tmp_path, "crates/server",
            cargo='[package]\nname = "server"\n\n[dependencies]\nactix-web = "4"\n',
            main_rs=True,
            lib_rs=True,
        )
        assert _classify(tmp_path, ws) == "service"

    def test_rust_crate_with_main_but_no_server_dep_is_not_service(self, tmp_path):
        # A crate with src/main.rs but NO server-framework dep is a
        # CLI/dev-tool/bench (meilisearch meilitool / xtask / openapi-gen) —
        # NOT a service. This is what stops the Rust path from re-creating
        # the lib-explosion (8 meilisearch crates have a main.rs).
        ws = self._crate(
            tmp_path, "crates/tool",
            cargo='[package]\nname = "tool"\n\n[dependencies]\nclap = "4"\n',
            main_rs=True,
            lib_rs=True,
        )
        assert _classify(tmp_path, ws) != "service"

    def test_rust_lib_crate_no_main_is_not_service(self, tmp_path):
        # A pure library crate (lib.rs, no main.rs) is never a service even
        # if it lists a server framework as a dependency.
        ws = self._crate(
            tmp_path, "crates/lib",
            cargo='[package]\nname = "lib"\n\n[dependencies]\nhyper = "1"\n',
            lib_rs=True,
        )
        assert _classify(tmp_path, ws) != "service"

    def test_rust_crate_with_bin_table_and_server_dep_is_service(self, tmp_path):
        # An explicit Cargo [[bin]] target counts as a binary entry even
        # without the conventional src/main.rs.
        ws = self._crate(
            tmp_path, "crates/svc",
            cargo=(
                '[package]\nname = "svc"\n\n'
                '[[bin]]\nname = "svc"\npath = "src/run.rs"\n\n'
                '[dependencies]\naxum = "0.7"\n'
            ),
            lib_rs=True,
        )
        assert _classify(tmp_path, ws) == "service"

    def test_rust_library_monorepo_collapses_to_whole_repo(self, tmp_path):
        # A Cargo workspace of library crates (each lib.rs, no server) with
        # NO running service collapses to a single whole-repo unit, NOT
        # one-unit-per-crate (the meilisearch 24->1 / excalidraw 6->1 fix).
        a = self._crate(
            tmp_path, "crates/a",
            cargo='[package]\nname = "a"\n', lib_rs=True,
        )
        b = self._crate(
            tmp_path, "crates/b",
            cargo='[package]\nname = "b"\n', lib_rs=True,
        )
        plan = partition_monorepo(_ctx(tmp_path, [a, b]))
        assert {u.subpath for u in plan.units} == {None}
        assert plan.subpaths() == []

    def test_rust_workspace_with_one_server_yields_one_unit(self, tmp_path):
        # 1 server crate among many library crates -> exactly one service
        # unit; the libs ride along (meilisearch -> crates/meilisearch).
        server = self._crate(
            tmp_path, "crates/api",
            cargo='[package]\nname = "api"\n\n[dependencies]\nactix-web = "4"\n',
            main_rs=True, lib_rs=True,
        )
        libs = [
            self._crate(
                tmp_path, f"crates/lib{i}",
                cargo=f'[package]\nname = "lib{i}"\n', lib_rs=True,
            )
            for i in range(5)
        ]
        plan = partition_monorepo(_ctx(tmp_path, [server, *libs]))
        assert {u.subpath for u in plan.units} == {"crates/api"}
        assert len([e for e in plan.excluded if e.type == "lib"]) == 5


# ── Audit fix 3: test-harness must not leak as a unit ────────────────────


class TestTestHarnessNotAUnit:
    def test_tests_path_segment_is_excluded(self, tmp_path):
        # A package under a ``tests`` path segment is never a scan unit even
        # if it pulls a server framework in (trpc packages/tests).
        app = _make_workspace(
            tmp_path, "apps/web", name="web",
            package_json={"dependencies": {"next": "14"}},
        )
        _write(tmp_path, "apps/web/app/page.tsx", "")
        harness = _make_workspace(
            tmp_path, "packages/tests", name="tests",
            package_json={"private": True, "devDependencies": {"fastify": "4"}},
        )
        plan = partition_monorepo(_ctx(tmp_path, [app, harness]))
        assert {u.subpath for u in plan.units} == {"apps/web"}
        assert "packages/tests" not in {u.subpath for u in plan.units}

    def test_test_harness_with_framework_devdep_not_service(self, tmp_path):
        # Defense beyond the path segment: even a package NOT under a tests/
        # segment, with a framework devDependency but NO server entry, is not
        # promoted to a service.
        ws = _make_workspace(
            tmp_path, "packages/itest", name="itest",
            package_json={"private": True, "devDependencies": {"fastify": "4"}},
        )
        assert _classify(tmp_path, ws) != "service"


# ── Audit fix 4: manifest-less split-fullstack synthesis ─────────────────


class TestSplitFullstackSynthesis:
    def _split_repo(self, tmp_path: Path) -> None:
        # frontend = react app with a routes dir; backend = fastify service
        # with a src/main.ts. NO workspace manifest at the root.
        _write(
            tmp_path, "frontend/package.json",
            _pkg("frontend", dependencies={"react": "18", "react-dom": "18"}),
        )
        _write(tmp_path, "frontend/src/pages/index.tsx", "")
        _write(
            tmp_path, "backend/package.json",
            _pkg("backend", main="./dist/main.mjs", dependencies={"fastify": "4"}),
        )
        _write(tmp_path, "backend/src/main.ts", "bootstrap()\n")

    def test_split_fullstack_synthesizes_two_units(self, tmp_path):
        # No workspace manager -> ctx.workspaces is None, but frontend/ +
        # backend/ each have a manifest -> synthesize them as units
        # (infisical: 1 whole-repo blob -> 2 units).
        self._split_repo(tmp_path)
        plan = partition_monorepo(_ctx(tmp_path, None))
        assert plan.is_monorepo is True
        units = {u.subpath: u.project_type for u in plan.units}
        assert units == {"frontend": "app", "backend": "service"}
        assert sorted(plan.subpaths()) == ["backend", "frontend"]

    def test_split_fullstack_fires_even_with_a_root_manifest(self, tmp_path):
        # infisical HAS a root package.json (no ``workspaces`` field) — the
        # rescue must still fire because detect_workspace enumerates nothing.
        self._split_repo(tmp_path)
        _write(tmp_path, "package.json", _pkg("root"))
        plan = partition_monorepo(_ctx(tmp_path, None))
        assert {u.subpath for u in plan.units} == {"frontend", "backend"}

    def test_non_split_single_project_stays_whole_repo(self, tmp_path):
        # A single-project repo (no workspaces, NOT split-fullstack) is
        # whole-repo — back-compat preserved (caddy).
        _write(tmp_path, "package.json", _pkg("solo", dependencies={"next": "14"}))
        _write(tmp_path, "app/page.tsx", "")
        plan = partition_monorepo(_ctx(tmp_path, None))
        assert plan.is_monorepo is False
        assert plan.units[0].subpath is None
        assert plan.subpaths() == []

    def test_split_requires_both_frontend_and_backend(self, tmp_path):
        # Only a frontend/ with a manifest (no backend/) is NOT a split
        # fullstack -> whole-repo (the gate is conservative).
        _write(
            tmp_path, "frontend/package.json",
            _pkg("frontend", dependencies={"react": "18"}),
        )
        _write(tmp_path, "frontend/src/pages/index.tsx", "")
        plan = partition_monorepo(_ctx(tmp_path, None))
        assert plan.is_monorepo is False
        assert plan.units[0].subpath is None

    def test_split_fullstack_does_not_override_real_workspaces(self, tmp_path):
        # When the repo DOES declare workspaces (>=2), the synthesis path is
        # skipped entirely — real enumerated workspaces win.
        a = _make_workspace(
            tmp_path, "apps/web", name="web",
            package_json={"dependencies": {"next": "14"}},
        )
        _write(tmp_path, "apps/web/app/page.tsx", "")
        b = _make_workspace(
            tmp_path, "packages/ui", name="ui",
            package_json={"exports": {".": "./i.js"}},
        )
        # Also lay down a frontend/backend split that would otherwise fire.
        self._split_repo(tmp_path)
        plan = partition_monorepo(_ctx(tmp_path, [a, b]))
        # Real workspaces used; synthesis NOT triggered.
        assert {u.subpath for u in plan.units} == {"apps/web"}
        assert "frontend" not in {u.subpath for u in plan.units}
