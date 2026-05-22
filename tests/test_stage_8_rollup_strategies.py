"""Tests for ``faultline.pipeline_v2.stage_8_rollup_strategies``.

Covers:
  - Each of the 7 rollup strategies (table-driven, 5+ cases each)
  - The dispatcher (registry lookup, fallback to universal-residual,
    payload cap, telemetry)
  - Hard rules: universal-residual never over-attaches; oss-library
    has no path fallback.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from faultline.models.types import Feature, Flow
from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace
from faultline.pipeline_v2.stage_8_rollup_strategies import (
    MAX_FLOWS_IN_PAYLOAD,
    UNIVERSAL_OVERLAP_THRESHOLD,
    BackendMonolithStrategy,
    CliToolStrategy,
    FrameworkRepoStrategy,
    OssLibraryStrategy,
    RollupResult,
    SHAPE_ROLLUPS,
    SingleSaasRoutedStrategy,
    TurborepoMonorepoStrategy,
    UniversalResidualStrategy,
    stage_8_rollup_flows,
    write_rollup_artifact,
)


# ── Fixtures ──────────────────────────────────────────────────────────


def _pf(name: str, paths: list[str], *, flows: list[Flow] | None = None) -> Feature:
    """Build a minimal product Feature for rollup testing."""
    return Feature(
        name=name,
        display_name=name,
        description=None,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2026, 5, 22, tzinfo=timezone.utc),
        health_score=100.0,
        flows=list(flows) if flows else [],
        coverage_pct=None,
        layer="product",
    )


def _flow(
    name: str,
    entry_point_file: str | None,
    *,
    paths: list[str] | None = None,
    flow_id: str | None = None,
) -> Flow:
    """Build a minimal Flow for rollup testing."""
    return Flow(
        name=name,
        display_name=name,
        description=None,
        participants=[],
        entry_point_file=entry_point_file,
        entry_point_line=None,
        paths=paths if paths is not None else [entry_point_file] if entry_point_file else [],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2026, 5, 22, tzinfo=timezone.utc),
        health_score=100.0,
        id=flow_id or f"::{name}",
        primary_feature=None,
    )


def _ctx(
    tmp_path: Path | None = None,
    *,
    workspaces: list[Workspace] | None = None,
    repo_shape: str | None = None,
) -> ScanContext:
    ctx = ScanContext(
        repo_path=Path(tmp_path) if tmp_path else Path("/tmp/__t__"),
        stack=None,
        monorepo=bool(workspaces),
        workspaces=workspaces,
        tracked_files=[],
        commits=[],
        run_dir=None,
    )
    if repo_shape:
        ctx.repo_shape = repo_shape
    return ctx


# ── TurborepoMonorepoStrategy ────────────────────────────────────────


class TestTurborepoMonorepoStrategy:
    strategy = TurborepoMonorepoStrategy()

    def test_workspace_match_basic(self):
        ws = [
            Workspace(name="web", path="apps/web"),
            Workspace(name="api", path="apps/api"),
        ]
        ctx = _ctx(workspaces=ws)
        pfs = [_pf("billing", ["apps/web/billing.ts", "apps/web/checkout.ts"])]
        flows = [_flow("billing-flow", "apps/web/billing.ts")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows[0].name == "billing-flow"
        assert result.total_attachments == 1
        rationale = result.per_pf_rationale["billing"][0]
        # Sprint S6.3: attach reason now stamped as combined gate.
        assert rationale[1] == "entry-point-in-paths+workspace:apps/web"

    def test_no_workspace_match_unattributed(self):
        ws = [Workspace(name="admin", path="apps/admin")]
        ctx = _ctx(workspaces=ws)
        pfs = [_pf("admin", ["apps/admin/x.ts"])]
        flows = [_flow("other-flow", "apps/web/x.ts")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows == []
        assert "other-flow" in result.unattributed_flows

    def test_multi_pf_attachment(self):
        """When the flow's entry-point lives in MULTIPLE PFs' paths AND
        all those PFs own the workspace, attach to each — this is the
        legitimate "shared file" case, distinct from the spam-attach
        pattern that S6.3 closes (where a flow attached to PFs that
        merely shared a workspace prefix but didn't carry the file).
        """
        ws = [Workspace(name="ui", path="packages/ui")]
        ctx = _ctx(workspaces=ws)
        pfs = [
            _pf("design-system", ["packages/ui/button.tsx"]),
            # Same entry-point listed in two PFs — both should attach.
            _pf("docs", ["packages/ui/button.tsx", "packages/ui/menu.tsx"]),
        ]
        flows = [_flow("ui-flow", "packages/ui/button.tsx")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert len(pfs[0].flows) == 1
        assert len(pfs[1].flows) == 1
        assert result.total_attachments == 2

    def test_flow_without_workspace_prefix_unattributed(self):
        ws = [Workspace(name="web", path="apps/web")]
        ctx = _ctx(workspaces=ws)
        pfs = [_pf("util", ["src/utils.ts"])]
        flows = [_flow("util-flow", "src/utils.ts")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows == []
        assert "util-flow" in result.unattributed_flows

    def test_empty_pf_paths(self):
        ws = [Workspace(name="web", path="apps/web")]
        ctx = _ctx(workspaces=ws)
        pfs = [_pf("empty", [])]
        flows = [_flow("any-flow", "apps/web/index.ts")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows == []
        assert "any-flow" in result.unattributed_flows


# ── SingleSaasRoutedStrategy ─────────────────────────────────────────


class TestSingleSaasRoutedStrategy:
    strategy = SingleSaasRoutedStrategy()

    def test_entry_point_match(self):
        ctx = _ctx()
        pfs = [_pf("billing", ["app/billing/page.tsx", "app/billing/route.ts"])]
        flows = [_flow("checkout-flow", "app/billing/page.tsx")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows[0].name == "checkout-flow"
        rationale = result.per_pf_rationale["billing"][0]
        assert rationale[1].startswith("entry-point-in-paths:")

    def test_no_match(self):
        ctx = _ctx()
        pfs = [_pf("auth", ["app/auth/page.tsx"])]
        flows = [_flow("billing-flow", "app/billing/page.tsx")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows == []
        assert "billing-flow" in result.unattributed_flows

    def test_multi_pf_match(self):
        ctx = _ctx()
        pfs = [
            _pf("home", ["app/page.tsx"]),
            _pf("layout", ["app/page.tsx", "app/layout.tsx"]),
        ]
        flows = [_flow("home-flow", "app/page.tsx")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert len(pfs[0].flows) == 1
        assert len(pfs[1].flows) == 1
        assert result.total_attachments == 2

    def test_flow_without_entry_point_unattributed(self):
        ctx = _ctx()
        pfs = [_pf("a", ["app/a/page.tsx"])]
        flows = [_flow("no-ep-flow", None)]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows == []
        assert "no-ep-flow" in result.unattributed_flows

    def test_empty_top_flows(self):
        ctx = _ctx()
        pfs = [_pf("a", ["app/a.ts"])]
        result = self.strategy.rollup(pfs, [], ctx)
        assert result.total_attachments == 0
        assert result.unattributed_flows == []


# ── OssLibraryStrategy ───────────────────────────────────────────────


class TestOssLibraryStrategy:
    strategy = OssLibraryStrategy()

    def test_sonnet_map_provided(self):
        ctx = _ctx()
        pfs = [_pf("http", ["src/http.ts"])]
        flows = [_flow("request-flow", "src/request.ts")]
        result = self.strategy.rollup(
            pfs, flows, ctx,
            sonnet_member_flows_map={"http": ["request-flow"]},
        )
        assert pfs[0].flows[0].name == "request-flow"
        rationale = result.per_pf_rationale["http"][0]
        assert rationale[1] == "sonnet-member-flows"

    def test_sonnet_map_absent_unattributes_all(self):
        ctx = _ctx()
        pfs = [_pf("http", ["src/http.ts"])]
        flows = [_flow("request-flow", "src/http.ts")]
        result = self.strategy.rollup(pfs, flows, ctx, sonnet_member_flows_map=None)
        # NO path fallback — even though entry-point IS in pf.paths.
        assert pfs[0].flows == []
        assert "request-flow" in result.unattributed_flows
        assert result.diagnostics.get("reason") == "no_sonnet_member_flows"

    def test_flow_name_not_in_map(self):
        ctx = _ctx()
        pfs = [_pf("http", ["src/http.ts"])]
        flows = [_flow("request-flow", "src/req.ts")]
        result = self.strategy.rollup(
            pfs, flows, ctx,
            sonnet_member_flows_map={"http": ["other-flow"]},
        )
        assert pfs[0].flows == []
        assert "request-flow" in result.unattributed_flows

    def test_unknown_pf_in_map(self):
        ctx = _ctx()
        pfs = [_pf("http", ["src/http.ts"])]
        flows = [_flow("request-flow", "src/req.ts")]
        result = self.strategy.rollup(
            pfs, flows, ctx,
            sonnet_member_flows_map={"NONEXISTENT": ["request-flow"]},
        )
        assert pfs[0].flows == []
        assert result.diagnostics.get("unknown_pf_references") == 1

    def test_no_path_fallback_even_with_overlap(self):
        """Critical contract: libraries must never get path-based attachment."""
        ctx = _ctx()
        pfs = [_pf("http", ["src/http.ts", "src/request.ts", "src/response.ts"])]
        flows = [_flow(
            "request-flow",
            "src/request.ts",
            paths=["src/request.ts", "src/http.ts", "src/response.ts"],
        )]
        result = self.strategy.rollup(pfs, flows, ctx, sonnet_member_flows_map=None)
        # 100% overlap, but no Sonnet map → still unattributed.
        assert pfs[0].flows == []
        assert "request-flow" in result.unattributed_flows


# ── BackendMonolithStrategy ──────────────────────────────────────────


class TestBackendMonolithStrategy:
    strategy = BackendMonolithStrategy()

    def test_rails_controller_match(self):
        ctx = _ctx()
        pfs = [_pf("users", ["app/controllers/users_controller.rb"])]
        flows = [_flow("show", "app/controllers/users_controller.rb")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows[0].name == "show"
        rationale = result.per_pf_rationale["users"][0]
        assert rationale[1] == "controller-match:users"

    def test_laravel_controller_match(self):
        ctx = _ctx()
        pfs = [_pf("users", ["app/Http/Controllers/UsersController.php"])]
        flows = [_flow("index", "app/Http/Controllers/UsersController.php")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows[0].name == "index"

    def test_non_controller_entry_falls_back_to_paths(self):
        ctx = _ctx()
        pfs = [_pf("settings", ["app/views/settings/index.html.erb"])]
        flows = [_flow("settings-flow", "app/views/settings/index.html.erb")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows[0].name == "settings-flow"
        rationale = result.per_pf_rationale["settings"][0]
        assert rationale[1].startswith("entry-point-in-paths:")

    def test_no_match_unattributed(self):
        ctx = _ctx()
        pfs = [_pf("users", ["app/controllers/users_controller.rb"])]
        flows = [_flow("orphan", "app/jobs/random_job.rb")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows == []
        assert "orphan" in result.unattributed_flows

    def test_multi_pf_controller_match(self):
        ctx = _ctx()
        pfs = [
            _pf("users", ["app/controllers/users_controller.rb"]),
            _pf("admin", ["app/controllers/users_controller.rb"]),
        ]
        flows = [_flow("show", "app/controllers/users_controller.rb")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert result.total_attachments == 2


# ── CliToolStrategy ──────────────────────────────────────────────────


class TestCliToolStrategy:
    strategy = CliToolStrategy()

    def test_go_cmd_command_match(self):
        ctx = _ctx()
        pfs = [_pf("serve-command", ["cmd/serve/main.go"])]
        flows = [_flow("serve-flow", "cmd/serve/main.go")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows
        rationale = result.per_pf_rationale["serve-command"][0]
        # Either command-match or entry-point-in-paths is fine.
        assert "command-match:" in rationale[1] or "entry-point-in-paths" in rationale[1]

    def test_js_bin_match(self):
        ctx = _ctx()
        pfs = [_pf("migrate", ["bin/migrate.js"])]
        flows = [_flow("migrate-run", "bin/migrate.js")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows[0].name == "migrate-run"

    def test_no_match(self):
        ctx = _ctx()
        pfs = [_pf("xx", ["src/xx.ts"])]
        flows = [_flow("yy-flow", "src/yy.ts")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows == []

    def test_command_name_substring_match(self):
        ctx = _ctx()
        pfs = [_pf("migrate-database", ["src/db/migrate.ts"])]
        flows = [_flow("flow-x", "bin/migrate.js")]  # cmd="migrate"
        result = self.strategy.rollup(pfs, flows, ctx)
        # "migrate" appears in "migrate-database" → command-match fires
        assert pfs[0].flows
        rationale = result.per_pf_rationale["migrate-database"][0]
        assert "command-match:migrate" == rationale[1]

    def test_empty_top_flows(self):
        ctx = _ctx()
        pfs = [_pf("a", ["bin/a.js"])]
        result = self.strategy.rollup(pfs, [], ctx)
        assert result.total_attachments == 0


# ── FrameworkRepoStrategy ────────────────────────────────────────────


class TestFrameworkRepoStrategy:
    strategy = FrameworkRepoStrategy()

    def test_sonnet_map_attachment(self):
        ctx = _ctx()
        pfs = [_pf("routing", ["src/routing.py"])]
        flows = [_flow("route-flow", "examples/app.py")]
        result = self.strategy.rollup(
            pfs, flows, ctx,
            sonnet_member_flows_map={"routing": ["route-flow"]},
        )
        assert pfs[0].flows[0].name == "route-flow"
        rationale = result.per_pf_rationale["routing"][0]
        assert rationale[1] == "sonnet-member-flows"

    def test_entry_point_fallback_when_no_sonnet_map(self):
        ctx = _ctx()
        pfs = [_pf("routing", ["src/routing.py", "examples/app.py"])]
        flows = [_flow("app-flow", "examples/app.py")]
        result = self.strategy.rollup(pfs, flows, ctx, sonnet_member_flows_map=None)
        assert pfs[0].flows[0].name == "app-flow"
        rationale = result.per_pf_rationale["routing"][0]
        assert rationale[1] == "entry-point-in-paths-fallback"
        assert result.diagnostics.get("fallback") is True

    def test_no_fallback_when_no_overlap(self):
        ctx = _ctx()
        pfs = [_pf("routing", ["src/routing.py"])]
        flows = [_flow("orphan", "tests/test_x.py")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows == []
        assert "orphan" in result.unattributed_flows

    def test_sonnet_map_plus_fallback(self):
        ctx = _ctx()
        pfs = [_pf("routing", ["src/routing.py", "examples/app.py"])]
        flows = [
            _flow("mapped-flow", "src/routing.py"),
            _flow("unmapped-flow", "examples/app.py"),
        ]
        result = self.strategy.rollup(
            pfs, flows, ctx,
            sonnet_member_flows_map={"routing": ["mapped-flow"]},
        )
        # Both should attach: mapped via sonnet, unmapped via fallback.
        assert len(pfs[0].flows) == 2
        reasons = [r[1] for r in result.per_pf_rationale["routing"]]
        assert "sonnet-member-flows" in reasons
        assert "entry-point-in-paths-fallback" in reasons

    def test_flow_without_entry_point_unattributed(self):
        ctx = _ctx()
        pfs = [_pf("routing", ["src/routing.py"])]
        flows = [_flow("no-ep", None)]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows == []
        assert "no-ep" in result.unattributed_flows


# ── UniversalResidualStrategy ────────────────────────────────────────


class TestUniversalResidualStrategy:
    strategy = UniversalResidualStrategy()

    def test_entry_point_in_paths_attaches(self):
        ctx = _ctx()
        pfs = [_pf("a", ["src/a.ts"])]
        flows = [_flow("a-flow", "src/a.ts")]
        result = self.strategy.rollup(pfs, flows, ctx)
        assert pfs[0].flows[0].name == "a-flow"
        rationale = result.per_pf_rationale["a"][0]
        assert rationale[1] == "entry-point-in-paths"

    def test_overlap_75pct_attaches(self):
        ctx = _ctx()
        pfs = [_pf("p", ["src/x.ts", "src/y.ts", "src/z.ts"])]
        flows = [_flow(
            "f", "src/other-ep.ts",
            paths=["src/x.ts", "src/y.ts", "src/z.ts", "src/other-ep.ts"],
        )]
        result = self.strategy.rollup(pfs, flows, ctx)
        # entry-point not in paths so pass 1 fails. paths overlap = 3/4 = 0.75
        assert pfs[0].flows[0].name == "f"
        rationale = result.per_pf_rationale["p"][0]
        assert rationale[1].startswith("path-overlap:")

    def test_overlap_50pct_attaches(self):
        ctx = _ctx()
        pfs = [_pf("p", ["src/a.ts", "src/b.ts"])]
        flows = [_flow(
            "f", "src/other-ep.ts",
            paths=["src/a.ts", "src/b.ts", "src/c.ts", "src/d.ts"],
        )]
        result = self.strategy.rollup(pfs, flows, ctx)
        # overlap = 2/4 = 0.50 → at threshold
        assert pfs[0].flows[0].name == "f"

    def test_overlap_25pct_does_not_attach(self):
        ctx = _ctx()
        pfs = [_pf("p", ["src/a.ts"])]
        flows = [_flow(
            "f", "src/other-ep.ts",
            paths=["src/a.ts", "src/b.ts", "src/c.ts", "src/d.ts"],
        )]
        result = self.strategy.rollup(pfs, flows, ctx)
        # overlap = 1/4 = 0.25 < 0.50
        assert pfs[0].flows == []

    def test_single_path_flow_never_overspams_in_pass_2(self):
        """The variant-A anti-test: 1-path flow doesn't trigger pass 2."""
        ctx = _ctx()
        pfs = [_pf("p", ["src/util.ts"])]
        # entry_point is "src/x.ts" (not in pf.paths), flow has 1 path.
        flows = [_flow("f", "src/x.ts", paths=["src/util.ts"])]
        result = self.strategy.rollup(pfs, flows, ctx)
        # entry-point miss + 1-path skip → unattributed.
        assert pfs[0].flows == []
        assert "f" in result.unattributed_flows

    def test_two_pass_no_double_attach(self):
        ctx = _ctx()
        pfs = [_pf("p", ["src/a.ts", "src/b.ts"])]
        flows = [_flow(
            "f", "src/a.ts",
            paths=["src/a.ts", "src/b.ts"],
        )]
        result = self.strategy.rollup(pfs, flows, ctx)
        # Pass 1 attaches; pass 2 must not double-attach.
        assert len(pfs[0].flows) == 1

    def test_overlap_threshold_is_fifty_percent(self):
        assert UNIVERSAL_OVERLAP_THRESHOLD == 0.50


# ── Dispatcher ───────────────────────────────────────────────────────


class TestStage8Dispatcher:
    def test_looks_up_strategy_by_shape(self):
        ctx = _ctx(repo_shape="single-saas-routed")
        pfs = [_pf("billing", ["app/billing/page.tsx"])]
        flows = [_flow("billing-flow", "app/billing/page.tsx")]
        result = stage_8_rollup_flows(pfs, flows, ctx)
        assert result.strategy_used == "single-saas-routed"
        assert pfs[0].flows[0].name == "billing-flow"

    def test_unknown_shape_falls_back_to_universal_residual(self):
        ctx = _ctx(repo_shape="made-up-shape")
        pfs = [_pf("p", ["src/p.ts"])]
        flows = [_flow("f", "src/p.ts")]
        result = stage_8_rollup_flows(pfs, flows, ctx)
        assert result.strategy_used == "universal-residual"

    def test_none_shape_falls_back_to_universal_residual(self):
        ctx = _ctx()
        # repo_shape stays None
        pfs = [_pf("p", ["src/p.ts"])]
        flows = [_flow("f", "src/p.ts")]
        result = stage_8_rollup_flows(pfs, flows, ctx)
        assert result.strategy_used == "universal-residual"

    def test_payload_cap_applied(self):
        ctx = _ctx(repo_shape="single-saas-routed")
        pfs = [_pf("big", ["src/big.ts"])]
        many_flows = [
            _flow(f"f-{i}", "src/big.ts", flow_id=f"id-{i}")
            for i in range(MAX_FLOWS_IN_PAYLOAD + 50)
        ]
        stage_8_rollup_flows(pfs, many_flows, ctx)
        assert len(pfs[0].flows) == MAX_FLOWS_IN_PAYLOAD

    def test_registry_has_all_shapes(self):
        """S6.2: registry covers the original 7 shapes plus Go/Rust aliases."""
        expected = {
            "turborepo-monorepo",
            "single-saas-routed",
            "oss-library",
            "backend-monolith",
            "cli-tool",
            "framework-repo",
            "universal-residual",
            # Sprint S6.2 — Go / Rust shape aliases.
            "go-server",
            "go-library",
            "rust-workspace",
        }
        assert set(SHAPE_ROLLUPS.keys()) == expected

    def test_go_server_routes_through_single_saas_routed(self):
        """S6.2: go-server reuses SingleSaasRoutedStrategy."""
        assert SHAPE_ROLLUPS["go-server"] is SHAPE_ROLLUPS["single-saas-routed"]

    def test_go_library_routes_through_oss_library(self):
        """S6.2: go-library reuses OssLibraryStrategy."""
        assert SHAPE_ROLLUPS["go-library"] is SHAPE_ROLLUPS["oss-library"]

    def test_rust_workspace_routes_through_oss_library(self):
        """S6.2: rust-workspace reuses OssLibraryStrategy."""
        assert SHAPE_ROLLUPS["rust-workspace"] is SHAPE_ROLLUPS["oss-library"]

    def test_custom_registry_used(self):
        class FakeStrategy:
            shape = "fake"
            def rollup(self, pfs, flows, ctx, *, sonnet_member_flows_map=None):
                return RollupResult(
                    strategy_used="fake",
                    pfs_attributed_count=0,
                    total_attachments=0,
                )

        ctx = _ctx(repo_shape="fake")
        custom_registry = {"fake": FakeStrategy(), "universal-residual": UniversalResidualStrategy()}
        pfs = [_pf("p", ["src/p.ts"])]
        flows = [_flow("f", "src/p.ts")]
        result = stage_8_rollup_flows(pfs, flows, ctx, registry=custom_registry)
        assert result.strategy_used == "fake"

    def test_diagnostics_includes_shape_confidence(self):
        ctx = _ctx(repo_shape="single-saas-routed")
        ctx.shape_confidence = 0.85
        pfs = [_pf("p", ["src/p.ts"])]
        flows = [_flow("f", "src/p.ts")]
        result = stage_8_rollup_flows(pfs, flows, ctx)
        assert result.diagnostics.get("shape_confidence") == 0.85

    def test_artifact_writes_when_run_dir_set(self, tmp_path):
        ctx = _ctx(tmp_path, repo_shape="single-saas-routed")
        ctx.run_dir = tmp_path
        pfs = [_pf("p", ["src/p.ts"])]
        flows = [_flow("f", "src/p.ts")]
        result = stage_8_rollup_flows(pfs, flows, ctx)
        write_rollup_artifact(ctx, pfs, result)
        artifact = tmp_path / "08-stage-rollup.json"
        assert artifact.exists()
        data = json.loads(artifact.read_text())
        assert data["strategy_used"] == "single-saas-routed"
        assert data["stats"]["total_attachments"] == 1
        assert "per_pf_rationale" in data

    def test_artifact_skipped_when_run_dir_none(self, tmp_path):
        ctx = _ctx(tmp_path, repo_shape="single-saas-routed")
        ctx.run_dir = None
        pfs = [_pf("p", ["src/p.ts"])]
        flows = [_flow("f", "src/p.ts")]
        result = stage_8_rollup_flows(pfs, flows, ctx)
        write_rollup_artifact(ctx, pfs, result)
        assert not (tmp_path / "08-stage-rollup.json").exists()
