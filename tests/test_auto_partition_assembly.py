"""Tests for Phase 5b — ``build_partition_assembly`` (isolated-scan stitch).

Gates:
  - Non-monorepo plan -> trivial ``{"is_monorepo": False}`` (back-compat).
  - Project conservation: one assembled ``project`` per scan unit, in
    plan order, carrying that unit's name/type/subpath.
  - Each project's nested ``featuremap`` is its OWN isolated scan JSON
    (read off disk), projected to the project keys.
  - The cross-project graph is the REUSED Stage 6.6 extractor output
    (nodes for every classified project + dependency edges).
  - Per-project blob (``max_feature_share``) + top-level stats are
    computed from the isolated featuremaps.
  - Fail-quiet: a ``MultiScanResult`` carrying an ``error`` is recorded as
    a ``failed`` project (featuremap None) without breaking the assembly.
  - A missing / unreadable featuremap JSON degrades to ``failed`` (never
    raises).

All inputs are SYNTHETIC (no real repo slices — memory:
rule-no-repo-specific-paths). The dep graph runs over hand-written
manifests in a tmp tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from faultline.pipeline_v2.auto_partition import build_partition_assembly
from faultline.pipeline_v2.multi import MultiScanResult
from faultline.pipeline_v2.stage_0_6_project_classifier import (
    ExcludedProject,
    PartitionPlan,
    ProjectClassification,
    ScanUnit,
)


# ── Synthetic builders ───────────────────────────────────────────────


def _featuremap_json(
    *,
    feature_paths: dict[str, list[str]],
    user_flows: int = 0,
    flows: int = 0,
) -> dict[str, Any]:
    """A minimal but realistically-shaped FeatureMap JSON.

    ``feature_paths`` maps a feature name -> its ``paths`` (the membership
    field that drives the blob metric). Extra keys mirror the real output
    so the projection is exercised.
    """
    return {
        "schema_version": "2.0",
        "repo_path": "/should/be/dropped",
        "remote_url": "https://example.test/repo",
        "engine_version": "9.9.9",
        "analyzed_at": "2026-06-21T00:00:00Z",
        "total_commits": 3,
        "date_range_days": 365,
        "is_full_scan": True,
        "developer_features": [
            {"name": name, "paths": paths, "uuid": f"uuid-{name}"}
            for name, paths in feature_paths.items()
        ],
        "product_features": [],
        "user_flows": [{"name": f"uf-{i}"} for i in range(user_flows)],
        "flows": [{"name": f"flow-{i}"} for i in range(flows)],
        "feature_flow_edges": [],
        "routes_index": [],
        "path_index": {},
        "scan_meta": {"stack": "next-app-router", "subpath": "x"},
        "monorepo": {"is_monorepo": False},
    }


def _write_fm(tmp_path: Path, name: str, doc: dict[str, Any]) -> Path:
    p = tmp_path / f"feature-map-{name}.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _js_repo(tmp_path: Path) -> Path:
    """A tiny JS monorepo tree so the dep-edge extractor can see an edge.

    ``apps/web`` depends on ``packages/ui`` (workspace protocol) -> one
    cross-project edge web -> ui, giving ``ui`` fan_in 1.
    """
    root = tmp_path / "mono"
    (root / "apps/web").mkdir(parents=True)
    (root / "apps/api").mkdir(parents=True)
    (root / "packages/ui").mkdir(parents=True)
    (root / "apps/web/package.json").write_text(
        json.dumps(
            {"name": "web", "dependencies": {"@acme/ui": "workspace:*", "next": "14"}}
        ),
        encoding="utf-8",
    )
    (root / "apps/api/package.json").write_text(
        json.dumps({"name": "api", "dependencies": {"fastify": "4"}}),
        encoding="utf-8",
    )
    (root / "packages/ui/package.json").write_text(
        json.dumps({"name": "@acme/ui", "version": "1.0.0"}),
        encoding="utf-8",
    )
    return root


def _plan(*, web: str, api: str) -> PartitionPlan:
    """A monorepo plan with web+api as units and ui as a ride-along lib."""
    classifications = (
        ProjectClassification(
            name="web", path="apps/web", project_type="app",
            confidence=0.95, rationale="next dep",
        ),
        ProjectClassification(
            name="api", path="apps/api", project_type="service",
            confidence=0.95, rationale="fastify dep",
        ),
        ProjectClassification(
            name="@acme/ui", path="packages/ui", project_type="lib",
            confidence=0.80, rationale="published lib",
        ),
    )
    return PartitionPlan(
        is_monorepo=True,
        units=(
            ScanUnit(subpath="apps/web", project_type="app", name="web"),
            ScanUnit(subpath="apps/api", project_type="service", name="api"),
        ),
        excluded=(
            ExcludedProject(
                path="packages/ui", type="lib",
                reason="shared library — rides along inside an app/service unit",
            ),
        ),
        classifications=classifications,
        rationale="Monorepo: 2 scan unit(s) (app, service); 1 excluded/ride-along.",
    )


# ── Gate: non-monorepo short-circuit ─────────────────────────────────


def test_non_monorepo_returns_trivial_view(tmp_path: Path) -> None:
    plan = PartitionPlan(
        is_monorepo=False,
        units=(ScanUnit(subpath=None, project_type="repo", name="solo"),),
        excluded=(),
        classifications=(),
        rationale="Whole-repo scan; no partition.",
    )
    out = build_partition_assembly(tmp_path, plan, [])
    assert out == {"is_monorepo": False}


# ── Gate: conservation + isolated featuremap + graph reuse + stats ───


def test_conserves_projects_and_attaches_isolated_featuremaps(
    tmp_path: Path,
) -> None:
    root = _js_repo(tmp_path)
    web_doc = _featuremap_json(
        feature_paths={
            "billing": ["apps/web/app/billing/page.tsx"],
            "auth": ["apps/web/app/auth/page.tsx"],
        },
        user_flows=2,
        flows=3,
    )
    api_doc = _featuremap_json(
        feature_paths={"server": ["apps/api/src/server.ts"]},
        user_flows=1,
        flows=1,
    )
    web_out = _write_fm(tmp_path, "web", web_doc)
    api_out = _write_fm(tmp_path, "api", api_doc)

    plan = _plan(web="apps/web", api="apps/api")
    results = [
        MultiScanResult(subpath="apps/web", out_path=web_out, result={"path": str(web_out)}, error=None),
        MultiScanResult(subpath="apps/api", out_path=api_out, result={"path": str(api_out)}, error=None),
    ]

    out = build_partition_assembly(root, plan, results)

    # is_monorepo + partition marker.
    assert out["is_monorepo"] is True
    assert out["partition"] == "isolated-per-project"

    # One project per scan unit, IN PLAN ORDER, with unit metadata.
    assert [p["subpath"] for p in out["projects"]] == ["apps/web", "apps/api"]
    assert [p["type"] for p in out["projects"]] == ["app", "service"]
    assert [p["name"] for p in out["projects"]] == ["web", "api"]
    assert all(p["scan_status"] == "ok" for p in out["projects"])

    # Nested featuremap is the ISOLATED scan, projected to project keys
    # (repo-level scalars dropped, developer_features present).
    web_proj = out["projects"][0]
    fm = web_proj["featuremap"]
    assert "repo_path" not in fm and "engine_version" not in fm
    assert {f["name"] for f in fm["developer_features"]} == {"billing", "auth"}
    assert fm["scan_meta"]["stack"] == "next-app-router"

    # Per-project summary (blob = max single-feature file share).
    # web has 2 features each owning 1 of 2 distinct files -> 0.5.
    assert web_proj["summary"]["developer_feature_count"] == 2
    assert web_proj["summary"]["user_flow_count"] == 2
    assert web_proj["summary"]["flow_count"] == 3
    assert web_proj["summary"]["file_count"] == 2
    assert web_proj["summary"]["max_feature_share"] == 0.5
    # api: 1 feature owns the only file -> share 1.0.
    assert out["projects"][1]["summary"]["max_feature_share"] == 1.0

    # Cross-project graph is the REUSED Stage 6.6 extractor: a node per
    # classified project + the web->ui workspace edge.
    node_subpaths = {n["subpath"] for n in out["cross_project_graph"]["nodes"]}
    assert node_subpaths == {"apps/web", "apps/api", "packages/ui"}
    edges = out["cross_project_graph"]["edges"]
    assert any(
        e["from"] == "apps/web" and e["to"] == "packages/ui" for e in edges
    ), edges
    ui_node = next(n for n in out["cross_project_graph"]["nodes"] if n["subpath"] == "packages/ui")
    assert ui_node["fan_in"] == 1

    # Partition plan echoed (units + excluded ride-along lib).
    assert [u["subpath"] for u in out["partition_plan"]["units"]] == ["apps/web", "apps/api"]
    assert out["partition_plan"]["excluded"][0]["path"] == "packages/ui"

    # Stats roll up across projects.
    stats = out["stats"]
    assert stats["project_count"] == 2
    assert stats["scanned"] == 2
    assert stats["failed"] == 0
    assert stats["developer_feature_total"] == 3
    # worst per-project blob = api's 1.0.
    assert stats["max_project_blob_share"] == 1.0
    assert stats["edge_count"] == len(edges)


# ── Gate: fail-quiet ─────────────────────────────────────────────────


def test_failed_subpath_recorded_without_breaking(tmp_path: Path) -> None:
    root = _js_repo(tmp_path)
    web_doc = _featuremap_json(feature_paths={"billing": ["apps/web/app/billing/page.tsx"]})
    web_out = _write_fm(tmp_path, "web", web_doc)

    plan = _plan(web="apps/web", api="apps/api")
    results = [
        MultiScanResult(subpath="apps/web", out_path=web_out, result={"path": str(web_out)}, error=None),
        MultiScanResult(
            subpath="apps/api", out_path=None, result=None,
            error="SubpathScopeError: load_repo escaped repo root",
        ),
    ]

    out = build_partition_assembly(root, plan, results)

    web_proj, api_proj = out["projects"]
    assert web_proj["scan_status"] == "ok"
    assert api_proj["scan_status"] == "failed"
    assert api_proj["featuremap"] is None
    assert "SubpathScopeError" in api_proj["error"]
    assert api_proj["summary"]["developer_feature_count"] == 0

    assert out["stats"]["scanned"] == 1
    assert out["stats"]["failed"] == 1
    # Failed project does NOT contribute to the worst-blob (only web's 1.0).
    assert out["stats"]["max_project_blob_share"] == 1.0


def test_missing_featuremap_file_degrades_to_failed(tmp_path: Path) -> None:
    root = _js_repo(tmp_path)
    plan = _plan(web="apps/web", api="apps/api")
    # out_path points at a non-existent file -> read returns None.
    ghost = tmp_path / "feature-map-ghost.json"
    results = [
        MultiScanResult(subpath="apps/web", out_path=ghost, result={"path": str(ghost)}, error=None),
        MultiScanResult(subpath="apps/api", out_path=ghost, result={"path": str(ghost)}, error=None),
    ]
    out = build_partition_assembly(root, plan, results)
    assert all(p["scan_status"] == "failed" for p in out["projects"])
    assert out["stats"]["scanned"] == 0
    assert out["stats"]["failed"] == 2
    # No crash; empty blob.
    assert out["stats"]["max_project_blob_share"] == 0.0


# ── Gate: determinism ────────────────────────────────────────────────


def test_assembly_is_deterministic(tmp_path: Path) -> None:
    root = _js_repo(tmp_path)
    web_doc = _featuremap_json(feature_paths={"billing": ["apps/web/x.ts"]})
    api_doc = _featuremap_json(feature_paths={"server": ["apps/api/y.ts"]})
    web_out = _write_fm(tmp_path, "web", web_doc)
    api_out = _write_fm(tmp_path, "api", api_doc)
    plan = _plan(web="apps/web", api="apps/api")
    results = [
        MultiScanResult(subpath="apps/web", out_path=web_out, result={}, error=None),
        MultiScanResult(subpath="apps/api", out_path=api_out, result={}, error=None),
    ]
    a = build_partition_assembly(root, plan, results)
    b = build_partition_assembly(root, plan, results)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_feature_file_set_reads_member_files_first() -> None:
    """The per-project blob must key on ``member_files`` — the canonical field
    ``cold_eval._file_set`` reads — not ``paths``. Reading paths-only produced a
    DIFFERENT metric than the gate (finding-coldeval-blob-broken-2026-06-19),
    so the assembly's max_feature_share silently diverged from cold_eval's."""
    from faultline.pipeline_v2.auto_partition import _feature_file_set

    # member_files (real dict schema with role/primary) WINS over paths.
    feat = {
        "paths": ["a.ts", "b.ts", "c.ts"],  # 3 — would be used if paths-first
        "member_files": [
            {"path": "a.ts", "role": "anchor", "primary": True},
            {"path": "b.ts", "role": "shared"},
        ],
    }
    assert _feature_file_set(feat) == {"a.ts", "b.ts"}  # not the 3 paths

    # No member_files → fall back to paths.
    assert _feature_file_set({"paths": ["x.ts", "y.ts"]}) == {"x.ts", "y.ts"}
    # Bare-string member_files tolerated.
    assert _feature_file_set({"member_files": ["m.ts"]}) == {"m.ts"}
    # Empty everywhere → empty set (no crash).
    assert _feature_file_set({}) == set()


def test_blob_metric_matches_cold_eval_on_member_files_schema(tmp_path: Path) -> None:
    """End-to-end: a project whose features carry the real member_files dict
    schema gets a max_feature_share that keys on member_files (so the assembly
    summary equals what cold_eval would gate on)."""
    root = _js_repo(tmp_path)
    # web: feat-big owns 3 of 4 distinct member_files → 0.75 by member_files.
    fm = {
        "developer_features": [
            {
                "name": "big",
                "member_files": [
                    {"path": "apps/web/a.ts", "primary": True},
                    {"path": "apps/web/b.ts", "role": "shared"},
                    {"path": "apps/web/c.ts", "role": "shared"},
                ],
                "paths": ["apps/web/a.ts"],  # sparse paths must NOT be used
            },
            {
                "name": "small",
                "member_files": [{"path": "apps/web/d.ts", "primary": True}],
            },
        ],
        "user_flows": [],
        "flows": [],
        "scan_meta": {"stack": "next-app-router"},
    }
    api_fm = {
        "developer_features": [
            {"name": "srv", "member_files": [{"path": "apps/api/src/server.ts", "primary": True}]}
        ],
        "user_flows": [],
        "flows": [],
        "scan_meta": {"stack": "nestjs"},
    }
    web_out = _write_fm(tmp_path, "web", fm)
    api_out = _write_fm(tmp_path, "api", api_fm)
    plan = _plan(web="apps/web", api="apps/api")
    results = [
        MultiScanResult(subpath="apps/web", out_path=web_out, result={}, error=None),
        MultiScanResult(subpath="apps/api", out_path=api_out, result={}, error=None),
    ]
    out = build_partition_assembly(root, plan, results)
    web = out["projects"][0]["summary"]
    assert web["file_count"] == 4
    assert web["max_feature_share"] == 0.75  # 3/4 by member_files, not paths
