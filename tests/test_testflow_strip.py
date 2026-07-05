"""Test-origin files must not produce feature anchors or flow candidates.

Regression suite for the Soc0 test-flow pollution bug (task #21):

``backend/tests/routers/test_admin.py`` matched the generic
RouteFileExtractor's FastAPI ``routers/<resource>.py`` convention and
minted a per-file ``test-admin`` feature (75 such anchors on Soc0).
Stage 3 then enumerated the pytest functions inside those files as flow
entry points → 191/1214 flows (15 %%) were test-origin, Stage 6.7 UF
clustering ran BEFORE the Stage 6.9 test-strip so entire UFs ("Sign in,
sign up, and manage sessions": 58/58 members) were built purely from
test flows, and Stage 6.9 could not drop them afterwards because
call-graph expansion had attached real production paths (tests import
app code) — zombie flows with recomputed entries survived to output.

The fix (this suite):
  1. ``is_test_file`` covers the pytest colocated conventions
     (``test_*.py`` prefix, ``conftest.py``) — same structural rule the
     django / fastapi_family profile boundary indexes already apply.
  2. RouteFileExtractor never emits anchors from test files (any stack).
  3. Stage 3 ``_enumerate_candidates_paths`` never enumerates exports /
     routes / symbols out of test files, so no flow can be born with a
     test entry point → nothing test-origin enters the UF digest.
  4. Test-file EVIDENCE survives: ``flow_test_mapper`` still attaches
     ``test_files`` / ``test_file_count`` telemetry (the two-layer
     coverage stack depends on tests as evidence, not as flows).
"""

from __future__ import annotations

from pathlib import Path

from faultline.analyzer.validation import is_test_file
from faultline.pipeline_v2.extractors.route import RouteFileExtractor
from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace
from faultline.pipeline_v2.stage_3_flows import _enumerate_candidates_paths


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ctx(
    tmp_path: Path,
    *,
    stack: str | None,
    files: list[str],
    workspaces: list[Workspace] | None = None,
) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=stack,
        monorepo=workspaces is not None,
        workspaces=workspaces,
        tracked_files=files,
        commits=[],
    )


def _anchor_names(ctx: ScanContext) -> set[str]:
    return {a.name for a in RouteFileExtractor().extract(ctx)}


# ── 1. is_test_file: pytest colocated conventions ──────────────────────────


class TestIsTestFilePytestConventions:
    def test_prefix_form_colocated(self) -> None:
        assert is_test_file("backend/services/test_runner_helpers.py")

    def test_prefix_form_in_routers_dir(self) -> None:
        assert is_test_file("backend/tests/routers/test_admin.py")

    def test_conftest_anywhere(self) -> None:
        assert is_test_file("backend/conftest.py")
        assert is_test_file("conftest.py")

    def test_suffix_form_still_detected(self) -> None:
        assert is_test_file("pkg/runner_test.py")
        assert is_test_file("pkg/handler_test.go")

    def test_no_substring_false_positives(self) -> None:
        # startswith("test_") / exact conftest only — never substring.
        assert not is_test_file("backend/services/contest.py")
        assert not is_test_file("backend/services/attest.py")
        assert not is_test_file("backend/services/latest_stats.py")
        assert not is_test_file("backend/routers/testimonials.py")

    def test_non_python_prefix_untouched(self) -> None:
        # The prefix rule is a *.py convention; a TS file named test_*
        # is unusual but not claimed by this rule (dir/suffix rules
        # still apply to JS).
        assert not is_test_file("src/pages/test_drive.tsx")


# ── 2. RouteFileExtractor: no anchors from test files ───────────────────────


class TestRouteExtractorSkipsTests:
    def test_soc0_shape_fastapi_colocated_tests(self, tmp_path: Path) -> None:
        """The exact Soc0 shape: prod routers + tests/routers twins."""
        files = [
            "backend/routers/admin.py",
            "backend/routers/insights.py",
            "backend/tests/routers/test_admin.py",
            "backend/tests/routers/test_insights.py",
            "backend/tests/conftest.py",
            "backend/tests/test_config.py",
        ]
        for f in files:
            _write(tmp_path / f, "# py\n")
        names = _anchor_names(_ctx(tmp_path, stack="fastapi", files=files))
        assert names == {"admin", "insights"}

    def test_colocated_prefix_tests_next_to_routers(self, tmp_path: Path) -> None:
        """Colocated pytest twin INSIDE routers/ (no tests/ dir at all)."""
        files = [
            "app/routers/findings.py",
            "app/routers/test_findings.py",
            "app/routers/conftest.py",
        ]
        for f in files:
            _write(tmp_path / f, "# py\n")
        names = _anchor_names(_ctx(tmp_path, stack="fastapi", files=files))
        assert names == {"findings"}

    def test_django_urls_in_tests_dir_skipped(self, tmp_path: Path) -> None:
        files = [
            "polls/urls.py",
            "polls/tests/urls.py",
        ]
        for f in files:
            _write(tmp_path / f, "# py\n")
        names = _anchor_names(_ctx(tmp_path, stack="django", files=files))
        assert names == {"polls"}

    def test_fs_routing_test_pages_skipped(self, tmp_path: Path) -> None:
        """JS side: a Pages-Router *.test.tsx / __tests__ page never anchors."""
        files = [
            "pages/dashboard.tsx",
            "pages/dashboard.test.tsx",
            "pages/__tests__/billing.tsx",
        ]
        for f in files:
            _write(tmp_path / f, "// tsx\n")
        names = _anchor_names(_ctx(tmp_path, stack="next-pages", files=files))
        assert names == {"dashboard"}

    def test_workspace_scoped_files_filtered(self, tmp_path: Path) -> None:
        """Monorepo branch: per-workspace file lists get the same guard."""
        files = [
            "apps/web/pages/settings.tsx",
            "apps/web/pages/settings.test.tsx",
        ]
        for f in files:
            _write(tmp_path / f, "// tsx\n")
        ws = Workspace(
            name="web",
            path="apps/web",
            stack="next-pages",
            files=files,
        )
        ctx = _ctx(tmp_path, stack=None, files=files, workspaces=[ws])
        names = _anchor_names(ctx)
        assert names == {"settings"}

    def test_prod_only_repo_unchanged(self, tmp_path: Path) -> None:
        """Tier-1 untouched-behaviour guard: without test files the
        anchor set is identical to the pre-fix behaviour."""
        files = [
            "backend/routers/admin.py",
            "backend/routers/cases.py",
            "polls/urls.py",
        ]
        for f in files:
            _write(tmp_path / f, "# py\n")
        names = _anchor_names(_ctx(tmp_path, stack="fastapi", files=files))
        assert names == {"admin", "cases", "polls"}


# ── 3. Stage 3 candidate enumeration: no symbols from test files ────────────


class TestStage3EnumerationSkipsTests:
    def test_pytest_functions_not_candidates(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "backend/routers/admin.py",
            "def list_admins():\n    return []\n",
        )
        _write(
            tmp_path / "backend/tests/routers/test_admin.py",
            "def test_list_admins_empty():\n    assert True\n",
        )
        exports, routes, symbol_to_loc, content_sig = _enumerate_candidates_paths(
            [
                "backend/routers/admin.py",
                "backend/tests/routers/test_admin.py",
            ],
            str(tmp_path),
        )
        assert "list_admins" in exports
        assert all(not s.startswith("test_") for s in exports)
        assert "backend/tests/routers/test_admin.py" not in content_sig
        assert all(
            not is_test_file(f) for f, _line in symbol_to_loc.values()
        )

    def test_colocated_prefix_test_not_candidates(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "svc/runner.py",
            "def run_job():\n    return 1\n",
        )
        _write(
            tmp_path / "svc/test_runner.py",
            "def test_run_job():\n    assert run_job() == 1\n",
        )
        exports, _routes, _map, content_sig = _enumerate_candidates_paths(
            ["svc/runner.py", "svc/test_runner.py"], str(tmp_path),
        )
        assert exports == ["run_job"]
        assert list(content_sig) == ["svc/runner.py"]

    def test_prod_only_enumeration_unchanged(self, tmp_path: Path) -> None:
        """No test paths in the feature → byte-identical enumeration
        (cache keys for clean features do not shift)."""
        _write(
            tmp_path / "backend/routers/admin.py",
            "def list_admins():\n    return []\n",
        )
        before = _enumerate_candidates_paths(
            ["backend/routers/admin.py"], str(tmp_path),
        )
        assert "list_admins" in before[0]
        assert list(before[3]) == ["backend/routers/admin.py"]


# ── 4. Telemetry survival: tests stay EVIDENCE ──────────────────────────────


class TestTestEvidenceSurvives:
    def test_flow_test_mapper_still_attaches_test_files(
        self, tmp_path: Path,
    ) -> None:
        """The coverage stack keeps seeing test files: a flow whose
        production file has a pytest twin still gets ``test_files`` +
        ``test_file_count`` telemetry."""
        from faultline.pipeline_v2.flow_reach import ReachContext
        from faultline.pipeline_v2.flow_test_mapper import (
            attach_flow_test_files,
        )

        _write(
            tmp_path / "backend/routers/admin.py",
            "def list_admins():\n    return []\n",
        )
        _write(
            tmp_path / "backend/tests/routers/test_admin.py",
            "from backend.routers.admin import list_admins\n"
            "def test_list_admins():\n    assert list_admins() == []\n",
        )
        rctx = ReachContext(
            repo_path=tmp_path,
            file_set=frozenset(
                {
                    "backend/routers/admin.py",
                    "backend/tests/routers/test_admin.py",
                }
            ),
            signatures={},
            alias_map={},
            monorepo_packages=set(),
            go_module_prefix=None,
        )

        class _Flow:
            name = "list-admins-flow"
            entry_point_file = "backend/routers/admin.py"
            entry = {"file": "backend/routers/admin.py", "symbol": "list_admins"}
            paths = ["backend/routers/admin.py"]
            nodes: list = []
            flow_symbol_attributions: list = []
            test_files: list[str] = []
            test_file_count = 0

        flow = _Flow()
        attach_flow_test_files([flow], rctx)  # type: ignore[arg-type]
        assert flow.test_files == ["backend/tests/routers/test_admin.py"]
        assert flow.test_file_count == 1


# ── 5. Whole test-support workspace packages (iteration-3, package-root) ───
#
# Measured miss (rallly board-refresh scan): PackageAnchorExtractor anchors
# every declared workspace as a feature with its FULL file list (Sprint D3
# design — see extractors/package.py), including a whole
# ``packages/test-helpers`` package (mailbox-polling / auth-code helpers
# consumed only by the Playwright e2e suite). None of its files match a
# structural test pattern — not ``test_*``, not under ``tests/`` — because
# the "this is test support" signal lives at the PACKAGE level. Stage 3's
# ``_enumerate_candidates_paths`` (line ~607) still filters via
# ``is_test_file`` before minting export/route candidates, so extending
# that single predicate closes the gap with no call-site changes.


class TestTestSupportPackageRootFlows:
    def test_rallly_shaped_package_yields_no_flow_candidates(
        self, tmp_path: Path,
    ) -> None:
        _write(
            tmp_path / "packages/test-helpers/src/index.ts",
            "export function authenticateWithEmail() { return true; }\n",
        )
        _write(
            tmp_path / "packages/test-helpers/src/email.ts",
            "export function retrieveAuthenticationCode() { return '1'; }\n",
        )
        exports, routes, symbol_to_loc, content_sig = _enumerate_candidates_paths(
            [
                "packages/test-helpers/src/index.ts",
                "packages/test-helpers/src/email.ts",
            ],
            str(tmp_path),
        )
        assert exports == []
        assert routes == []
        assert symbol_to_loc == {}
        assert content_sig == {}

    def test_sibling_production_package_unaffected(self, tmp_path: Path) -> None:
        """packages/ui is a real product package — the package-root rule
        is an exact-name allowlist, not a blanket ``packages/*`` drop."""
        _write(
            tmp_path / "packages/ui/src/button.tsx",
            "export function Button() { return null; }\n",
        )
        exports, _routes, _map, content_sig = _enumerate_candidates_paths(
            ["packages/ui/src/button.tsx"], str(tmp_path),
        )
        assert "Button" in exports
        assert "packages/ui/src/button.tsx" in content_sig

    def test_real_feature_named_testimonials_survives(self, tmp_path: Path) -> None:
        """Substring trap: 'testimonials' contains 'test' but is not in
        the curated test-support package-root name set."""
        _write(
            tmp_path / "packages/testimonials/src/index.ts",
            "export function renderTestimonials() { return []; }\n",
        )
        assert not is_test_file("packages/testimonials/src/index.ts")
        exports, _routes, _map, content_sig = _enumerate_candidates_paths(
            ["packages/testimonials/src/index.ts"], str(tmp_path),
        )
        assert "renderTestimonials" in exports
        assert "packages/testimonials/src/index.ts" in content_sig

    def test_package_root_convention_covers_tooling_container_too(self) -> None:
        assert is_test_file("tooling/e2e/playwright.config.ts")
        assert is_test_file("tooling/fixtures/seed.ts")
        assert not is_test_file("tooling/codegen/generate.ts")
