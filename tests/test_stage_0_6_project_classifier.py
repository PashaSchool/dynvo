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

    def test_service_from_server_dep(self, tmp_path):
        ws = _make_workspace(
            tmp_path,
            "apps/backend",
            name="backend",
            package_json={"dependencies": {"express": "4"}},
        )
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

    def test_standalone_lib_monorepo_makes_libs_units(self, tmp_path):
        # No app/service anywhere -> each lib is its own unit (a
        # publishable multi-package library repo).
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
        assert {u.subpath for u in plan.units} == {"packages/a", "packages/b"}

    def test_nested_units_collapse_to_shallowest(self, tmp_path):
        # apps/api (service) + apps/api/v1 (also a unit) -> only apps/api
        # survives; v1 rides inside its tree (no double scan).
        parent = _make_workspace(
            tmp_path, "apps/api", name="api",
            package_json={"dependencies": {"express": "4"}},
        )
        child = _make_workspace(
            tmp_path, "apps/api/v1", name="api-v1",
            package_json={"dependencies": {"express": "4"}},
        )
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
        # "apps/api-v2".
        a = _make_workspace(
            tmp_path, "apps/api", name="api",
            package_json={"dependencies": {"express": "4"}},
        )
        b = _make_workspace(
            tmp_path, "apps/api-v2", name="api-v2",
            package_json={"dependencies": {"fastify": "4"}},
        )
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
