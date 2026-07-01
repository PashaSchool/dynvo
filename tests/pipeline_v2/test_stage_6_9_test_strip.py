"""Stage 6.9 — test-file output-tree strip tests.

Covers: predicate boundaries, per-field strip shape, loc_edges endpoint
drop, drop-empty feature, drop-empty flow, entry recompute, the KEY
invariant that metric scalars are untouched, dedupe of the shared Flow
object across containment + bipartite views, and telemetry. No LLM, no
network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import (
    Feature,
    Flow,
    FlowLocEdge,
    FlowLocNode,
    FlowSymbolAttribution,
    MemberFile,
    SymbolAttribution,
)
from faultline.pipeline_v2.stage_6_9_test_strip import (
    is_test_path,
    stage_6_9_enabled,
    strip_test_paths,
)

_NOW = datetime(2026, 5, 26, tzinfo=timezone.utc)


# ── predicate boundaries ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "src/foo.test.ts",
        "src/foo.spec.tsx",
        "src/foo.e2e.ts",
        "src/foo.cy.js",
        "pkg/foo_test.py",
        "pkg/foo_spec.rb",
        "app/__tests__/page.tsx",
        "app/__mocks__/db.ts",
        "tests/login.py",
        "test/login.py",
        "e2e/checkout.ts",
        "cypress/support/index.js",
        "playwright/auth.ts",
        "src/__fixtures__/data.json",
        "DEEP/Tests/Thing.cs",  # case-insensitive segment
        "A/B/C.TEST.TS",        # case-insensitive basename
        "apps/api/v2/src/app.e2e-spec.ts",          # NestJS hyphenated convention
        "apps/api/v2/src/x.controller.e2e-spec.ts",  # NestJS, multi-dot
        "apps/api/v2/jest-e2e.ts",                   # jest e2e runner (ends e2e)
        "apps/web/modules/test-setup.ts",            # test- prefix
        "src/foo-spec.ts",                           # hyphen spec suffix
    ],
)
def test_predicate_positive(path: str) -> None:
    assert is_test_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/foo.ts",
        "src/testing-utils.ts",      # "test" substring but not a segment
        "src/latest/foo.ts",         # "latest" not "test"
        "src/contest/foo.ts",        # "contest" not a test segment
        "app/page.tsx",
        "lib/spectrum.ts",           # contains "spec" but not ".spec."
        "apps/web/modules/webhooks/views/webhook-test-header.tsx",  # product file: "test" mid-name
        "components/TestimonialCard.tsx",  # "test" prefix substring, single token
        "src/manifest.ts",           # ends "fest" not a marker token
        "",
        None,
        123,
    ],
)
def test_predicate_negative(path) -> None:
    assert is_test_path(path) is False


def test_enabled_default_and_env(monkeypatch) -> None:
    monkeypatch.delenv("FAULTLINE_STAGE_6_9_TEST_STRIP", raising=False)
    assert stage_6_9_enabled() is True
    monkeypatch.setenv("FAULTLINE_STAGE_6_9_TEST_STRIP", "0")
    assert stage_6_9_enabled() is False
    monkeypatch.setenv("FAULTLINE_STAGE_6_9_TEST_STRIP", "1")
    assert stage_6_9_enabled() is True


# ── fixtures ──────────────────────────────────────────────────────────


def _feature(name: str, paths: list[str], **kw) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=_NOW,
        health_score=80.0,
        coverage_pct=42.5,
        **kw,
    )


def _flow(name: str, paths: list[str], **kw) -> Flow:
    return Flow(
        name=name,
        paths=paths,
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=_NOW,
        health_score=80.0,
        coverage_pct=55.0,
        **kw,
    )


# ── per-field strip shape ───────────────────────────────────────────────


def test_feature_paths_and_attributions_stripped() -> None:
    f = _feature(
        "auth",
        ["app/login.ts", "app/__tests__/login.test.ts"],
        symbol_attributions=[
            FlowSymbolAttribution(file="app/login.ts", symbol="login",
                                  line_start=1, line_end=5, role="entry"),
            FlowSymbolAttribution(file="app/login.test.ts", symbol="t",
                                  line_start=1, line_end=2, role="support"),
        ],
        shared_attributions=[
            SymbolAttribution(file_path="app/util.ts", symbols=["x"],
                              line_ranges=[(1, 2)], attributed_lines=2,
                              total_file_lines=10),
            SymbolAttribution(file_path="tests/util_test.py", symbols=["y"],
                              line_ranges=[(1, 2)], attributed_lines=2,
                              total_file_lines=10),
        ],
    )
    stats = strip_test_paths([f], [])
    assert f.paths == ["app/login.ts"]
    assert [a.file for a in f.symbol_attributions] == ["app/login.ts"]
    assert [a.file_path for a in f.shared_attributions] == ["app/util.ts"]
    assert stats["paths_removed"] == 3
    assert stats["features_dropped"] == 0


def test_feature_member_files_stripped() -> None:
    """member_files — the full-provenance ledger the dashboard / blob metrics
    read — must be swept too. Otherwise test files stripped from ``paths``
    linger here and inflate the feature's apparent size (the Soc0 ``backend``
    case leaked 336 test files: 315 real paths shown as a 653-file blob)."""
    f = _feature(
        "backend",
        ["backend/services/auth.py"],  # paths already test-free
        member_files=[
            MemberFile(path="backend/services/auth.py", role="anchor",
                       confidence=1.0, primary=True),
            MemberFile(path="backend/tests/test_auth.py", role="closure",
                       confidence=0.5),
            MemberFile(path="backend/services/util.py", role="closure",
                       confidence=0.5),
        ],
    )
    stats = strip_test_paths([f], [])
    kept = [m.path for m in f.member_files]
    assert kept == ["backend/services/auth.py", "backend/services/util.py"]
    # feature survives (paths non-empty) and metric scalars are untouched
    assert f.paths == ["backend/services/auth.py"]
    assert f.health_score == 80.0
    assert f.coverage_pct == 42.5
    assert stats["features_dropped"] == 0
    assert stats["paths_removed"] == 1  # the one leaked test MemberFile


def test_flow_attributions_and_loc_nodes_stripped() -> None:
    fl = _flow(
        "do-login",
        ["app/login.ts", "e2e/login.e2e.ts"],
        flow_symbol_attributions=[
            FlowSymbolAttribution(file="app/login.ts", symbol="login",
                                  line_start=1, line_end=5, role="entry"),
            FlowSymbolAttribution(file="cypress/login.ts", symbol="cy",
                                  line_start=1, line_end=2, role="support"),
        ],
        loc_nodes=[
            FlowLocNode(path="app/login.ts", symbol="login",
                        start_line=1, end_line=5, role="entry"),
            FlowLocNode(path="tests/login.test.ts", symbol="t",
                        start_line=1, end_line=2, role="called"),
        ],
    )
    stats = strip_test_paths([], [fl])
    assert fl.paths == ["app/login.ts"]
    assert [a.file for a in fl.flow_symbol_attributions] == ["app/login.ts"]
    assert [n.path for n in fl.loc_nodes] == ["app/login.ts"]
    assert stats["paths_removed"] == 3
    assert stats["flows_dropped"] == 0


def test_loc_edges_drop_if_either_endpoint_is_test() -> None:
    fl = _flow(
        "graph",
        ["app/a.ts", "app/b.ts"],
        loc_edges=[
            FlowLocEdge(from_path="app/a.ts", to_path="app/b.ts", kind="call"),
            FlowLocEdge(from_path="app/a.ts", to_path="tests/b.test.ts",
                        kind="call"),
            FlowLocEdge(from_path="__tests__/a.ts", to_path="app/b.ts",
                        kind="call"),
        ],
    )
    stats = strip_test_paths([], [fl])
    assert len(fl.loc_edges) == 1
    assert fl.loc_edges[0].to_path == "app/b.ts"
    assert stats["paths_removed"] == 2


# ── drop-empty feature / flow ────────────────────────────────────────────


def test_drop_feature_that_becomes_path_empty() -> None:
    keep = _feature("real", ["app/a.ts"])
    phantom = _feature("tests", ["tests/a.test.ts", "tests/b.spec.ts"])
    features = [keep, phantom]
    stats = strip_test_paths(features, [])
    assert [f.name for f in features] == ["real"]
    assert stats["features_dropped"] == 1


def test_drop_flow_that_becomes_empty() -> None:
    keep = _flow("real", ["app/a.ts"])
    phantom = _flow("all-test", ["tests/a.test.ts"])
    flows = [keep, phantom]
    stats = strip_test_paths([], flows)
    assert [fl.name for fl in flows] == ["real"]
    assert stats["flows_dropped"] == 1


def test_dropped_flow_removed_from_feature_containment() -> None:
    phantom = _flow("all-test", ["tests/a.test.ts"])
    f = _feature("auth", ["app/a.ts"], flows=[phantom])
    flows = [phantom]
    strip_test_paths([f], flows)
    assert f.flows == []
    assert flows == []


# ── entry recompute ─────────────────────────────────────────────────────


def test_entry_recompute_prefers_surviving_entry_loc_node() -> None:
    fl = _flow(
        "do-x",
        ["app/handler.ts", "e2e/do-x.e2e.ts"],
        entry_point_file="e2e/do-x.e2e.ts",
        loc_nodes=[
            FlowLocNode(path="app/util.ts", symbol="u", start_line=1,
                        end_line=2, role="called"),
            FlowLocNode(path="app/handler.ts", symbol="h", start_line=1,
                        end_line=9, role="entry"),
        ],
    )
    stats = strip_test_paths([], [fl])
    assert fl.entry_point_file == "app/handler.ts"
    assert stats["flow_entries_recomputed"] == 1


def test_entry_recompute_falls_back_to_top_path() -> None:
    fl = _flow(
        "do-y",
        ["__tests__/y.test.ts", "app/y.ts"],
        entry_point_file="__tests__/y.test.ts",
    )
    stats = strip_test_paths([], [fl])
    assert fl.entry_point_file == "app/y.ts"
    assert stats["flow_entries_recomputed"] == 1


def test_entry_no_survivor_drops_flow_and_no_recompute() -> None:
    fl = _flow(
        "all-test",
        ["__tests__/y.test.ts"],
        entry_point_file="__tests__/y.test.ts",
    )
    flows = [fl]
    stats = strip_test_paths([], flows)
    assert flows == []
    assert stats["flows_dropped"] == 1
    assert stats["flow_entries_recomputed"] == 0


def test_non_test_entry_is_untouched() -> None:
    fl = _flow("ok", ["app/a.ts"], entry_point_file="app/a.ts")
    stats = strip_test_paths([], [fl])
    assert fl.entry_point_file == "app/a.ts"
    assert stats["flow_entries_recomputed"] == 0


# ── KEY invariant: metric scalars untouched ──────────────────────────────


def test_metric_scalars_untouched() -> None:
    f = _feature("auth", ["app/a.ts", "app/a.test.ts"])
    f.coverage_pct = 42.5
    f.health_score = 73.1
    f.bug_fix_ratio = 0.25
    fl = _flow("do", ["app/a.ts", "e2e/do.e2e.ts"])
    fl.coverage_pct = 55.0
    fl.health_score = 60.0
    fl.bug_fix_ratio = 0.1
    f.flows = [fl]
    strip_test_paths([f], [fl])
    assert f.coverage_pct == 42.5
    assert f.health_score == 73.1
    assert f.bug_fix_ratio == 0.25
    assert fl.coverage_pct == 55.0
    assert fl.health_score == 60.0
    assert fl.bug_fix_ratio == 0.1


# ── dedupe shared flow object ────────────────────────────────────────────


def test_shared_flow_object_stripped_once() -> None:
    # Same Flow object lives in both Feature.flows and the top-level list.
    fl = _flow(
        "do",
        ["app/a.ts", "app/a.test.ts", "app/b.spec.ts"],
    )
    f = _feature("auth", ["app/a.ts"], flows=[fl])
    stats = strip_test_paths([f], [fl])
    # Two test paths removed from the single shared object — counted once.
    assert stats["paths_removed"] == 2
    assert fl.paths == ["app/a.ts"]


# ── telemetry shape ──────────────────────────────────────────────────────


def test_telemetry_keys() -> None:
    stats = strip_test_paths([], [])
    assert set(stats) == {
        "paths_removed",
        "features_dropped",
        "flows_dropped",
        "flow_entries_recomputed",
    }
    assert all(v == 0 for v in stats.values())


def test_tolerant_to_dict_and_string_entries() -> None:
    # loc_nodes as raw dicts, participants as objects, paths as strings.
    fl = _flow("mix", ["app/a.ts", "tests/a.test.ts"])
    fl.loc_nodes = [
        {"path": "app/a.ts", "role": "entry"},
        {"path": "tests/a.test.ts", "role": "called"},
    ]
    stats = strip_test_paths([], [fl])
    assert [n["path"] for n in fl.loc_nodes] == ["app/a.ts"]
    assert stats["paths_removed"] == 2


# ── finalize-phase Layer-2 reconcile (phantom PF after test-strip) ──────────


def test_phantom_pf_dropped_after_test_strip_empties_its_only_member() -> None:
    """A product feature whose ONLY developer member is a test-only feature
    becomes a phantom once Stage 6.9 strips it. The finalize phase re-applies
    the deterministic phantom drop so it doesn't reach output.

    Reproduces the infisical "Integrations" cluster (paths only under e2e/ +
    tests/): Stage 8.6's phantom drop ran BEFORE test-strip, so this case
    needs the post-strip pass.
    """
    from faultline.pipeline_v2.stage_8_6_nonsource_drop import (
        drop_phantom_product_features,
    )

    test_only = _feature(
        "integration-suite",
        ["e2e/tests/saml.spec.ts", "backend/tests/db_test.go"],
        product_feature_id="integrations",
    )
    real = _feature(
        "secrets-core",
        ["backend/src/secret.ts"],
        product_feature_id="secrets",
    )
    pf_integrations = _feature("integrations", ["e2e/tests/saml.spec.ts"], layer="product")
    pf_secrets = _feature("secrets", ["backend/src/secret.ts"], layer="product")

    features = [test_only, real]
    telem = strip_test_paths(features, [])
    # the test-only feature is gone; the real one survives
    assert telem["features_dropped"] == 1
    assert {f.name for f in features} == {"secrets-core"}

    # now the finalize reconcile drops the orphaned product feature
    product_features = [pf_integrations, pf_secrets]
    kept, dropped = drop_phantom_product_features(features, product_features)
    assert dropped == 1
    assert {pf.name for pf in kept} == {"secrets"}
