"""Unit tests for Stage 6.6 — Monorepo Assembly View.

All fixtures are SYNTHETIC (built in tmp_path), not slices from real corpus
repos — per memory rule-no-repo-specific-paths. Scale-invariance is covered
by tiny / medium / large fixtures (rule-no-magic-tuning): the rules must
behave identically at 2, ~10, and 200 projects with no tuned threshold.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from faultline.models.types import Feature, FeatureMap
from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace
from faultline.pipeline_v2.stage_6_6_monorepo_assembly import (
    GoDepEdgeExtractor,
    JsDepEdgeExtractor,
    PythonDepEdgeExtractor,
    RustDepEdgeExtractor,
    assign_features_to_projects,
    build_cross_project_graph,
    build_monorepo_assembly,
)
from faultline.pipeline_v2.stage_0_6_project_classifier import (
    ProjectClassification,
    classify_project,
    partition_monorepo,
)


# ── Synthetic-repo builders ─────────────────────────────────────────────


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ws(path: str, name: str, pkg: dict[str, Any] | None = None) -> Workspace:
    return Workspace(name=name, path=path, package_json=pkg, stack=None, files=[])


def _ctx(repo_root: Path, workspaces: list[Workspace] | None) -> ScanContext:
    """Minimal ScanContext for partition + assembly (no git needed)."""
    return ScanContext(
        repo_path=repo_root,
        stack=None,
        monorepo=bool(workspaces),
        workspaces=workspaces,
        tracked_files=[],
        commits=[],
    )


def _feat(name: str, paths: list[str], uuid: str) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=100.0,
        uuid=uuid,
    )


def _clf(name: str, path: str, ptype: str = "lib") -> ProjectClassification:
    return ProjectClassification(
        name=name, path=path, project_type=ptype, confidence=0.9, rationale=""
    )


# ── A small but realistic JS monorepo on disk ──────────────────────────


def _build_js_monorepo(root: Path) -> ScanContext:
    """web (app) -> @acme/ui, @acme/utils (libs); @acme/ui -> @acme/utils.

    Mirrors the dub/twenty shape WITHOUT copying their paths/names.
    """
    _write_json(
        root / "apps/web/package.json",
        {
            "name": "web",
            "dependencies": {
                "@acme/ui": "workspace:*",
                "@acme/utils": "workspace:*",
                "react": "^18",
                "next": "^14",
            },
        },
    )
    (root / "apps/web/app").mkdir(parents=True, exist_ok=True)
    _write_json(
        root / "packages/ui/package.json",
        {
            "name": "@acme/ui",
            "main": "index.ts",
            "dependencies": {"@acme/utils": "workspace:*"},
        },
    )
    _write_json(
        root / "packages/utils/package.json",
        {"name": "@acme/utils", "main": "index.ts"},
    )
    workspaces = [
        _ws("apps/web", "web"),
        _ws("packages/ui", "ui"),
        _ws("packages/utils", "utils"),
    ]
    # Re-read package_json via _enrich-equivalent (tests pass pkg explicitly):
    workspaces[0].package_json = json.loads((root / "apps/web/package.json").read_text())
    workspaces[1].package_json = json.loads((root / "packages/ui/package.json").read_text())
    workspaces[2].package_json = json.loads(
        (root / "packages/utils/package.json").read_text()
    )
    return _ctx(root, workspaces)


# ════════════════════════════════════════════════════════════════════════
# Phase 3 — edge extraction per ecosystem
# ════════════════════════════════════════════════════════════════════════


def test_js_edges_scoped_name_and_workspace_protocol(tmp_path: Path) -> None:
    ctx = _build_js_monorepo(tmp_path)
    plan = partition_monorepo(ctx)
    nodes, edges = build_cross_project_graph(tmp_path, plan.classifications)
    edge_pairs = {(e.from_project, e.to_project) for e in edges}
    # web -> ui, web -> utils (scoped-name + workspace:* match)
    assert ("apps/web", "packages/ui") in edge_pairs
    assert ("apps/web", "packages/utils") in edge_pairs
    # ui -> utils
    assert ("packages/ui", "packages/utils") in edge_pairs
    # external deps (react/next) never become edges
    assert all("react" not in e.via.lower() for e in edges)


def test_js_fan_in_surfaces_shared_lib(tmp_path: Path) -> None:
    ctx = _build_js_monorepo(tmp_path)
    plan = partition_monorepo(ctx)
    nodes, edges = build_cross_project_graph(tmp_path, plan.classifications)
    fan = {n.subpath: n.fan_in for n in nodes}
    # utils is depended on by BOTH web and ui => fan_in 2 (the shared lib).
    assert fan["packages/utils"] == 2
    # ui depended on by web only => 1.
    assert fan["packages/ui"] == 1
    # web depends on nothing-internal-points-to-it => 0.
    assert fan["apps/web"] == 0
    # nodes are sorted fan_in desc => utils first.
    assert nodes[0].subpath == "packages/utils"


def test_js_no_self_edge_even_if_name_self_refers(tmp_path: Path) -> None:
    # A package that (pathologically) lists itself in deps must NOT self-edge.
    _write_json(
        tmp_path / "packages/a/package.json",
        {"name": "@x/a", "main": "i.ts", "dependencies": {"@x/a": "workspace:*"}},
    )
    _write_json(tmp_path / "packages/b/package.json", {"name": "@x/b", "main": "i.ts"})
    ws = [
        _ws("packages/a", "a", json.loads((tmp_path / "packages/a/package.json").read_text())),
        _ws("packages/b", "b", json.loads((tmp_path / "packages/b/package.json").read_text())),
    ]
    nodes, edges = build_cross_project_graph(
        tmp_path, [_clf("a", "packages/a"), _clf("b", "packages/b")]
    )
    assert all(e.from_project != e.to_project for e in edges)


def test_go_edges_require_module_prefix_and_replace(tmp_path: Path) -> None:
    # backend (module acme.io/backend) requires acme.io/shared/log + has a
    # local replace to ./libs/db.
    _write_text(
        tmp_path / "backend/go.mod",
        "module acme.io/backend\n\n"
        "go 1.22\n\n"
        "require (\n\tacme.io/shared/log v0.0.0\n\tgithub.com/ext/pkg v1.2.3\n)\n\n"
        "replace acme.io/db => ./../libs/db\n",
    )
    _write_text(tmp_path / "shared/log/go.mod", "module acme.io/shared\n\ngo 1.22\n")
    _write_text(tmp_path / "libs/db/go.mod", "module acme.io/db\n\ngo 1.22\n")
    clfs = [
        _clf("backend", "backend", "service"),
        _clf("log", "shared/log"),
        _clf("db", "libs/db"),
    ]
    _, edges = build_cross_project_graph(tmp_path, clfs)
    pairs = {(e.from_project, e.to_project) for e in edges}
    # require acme.io/shared/log matches module acme.io/shared (prefix).
    assert ("backend", "shared/log") in pairs
    # replace => ./../libs/db matches libs/db by resolved path.
    assert ("backend", "libs/db") in pairs
    # external require never edges.
    assert all("ext/pkg" not in e.via for e in edges)


def test_rust_edges_path_dep(tmp_path: Path) -> None:
    _write_text(
        tmp_path / "crates/server/Cargo.toml",
        '[package]\nname = "server"\n\n'
        "[dependencies]\n"
        'core = { path = "../core" }\n'
        'serde = "1"\n',
    )
    _write_text(tmp_path / "crates/core/Cargo.toml", '[package]\nname = "core"\n')
    clfs = [_clf("server", "crates/server", "service"), _clf("core", "crates/core")]
    _, edges = build_cross_project_graph(tmp_path, clfs)
    pairs = {(e.from_project, e.to_project) for e in edges}
    assert ("crates/server", "crates/core") in pairs
    assert all("serde" not in e.via for e in edges)


def test_python_edges_uv_path_source(tmp_path: Path) -> None:
    _write_text(
        tmp_path / "services/api/pyproject.toml",
        "[project]\nname = \"api\"\ndependencies = [\"shared\"]\n\n"
        "[tool.uv.sources]\n"
        'shared = { path = "../../libs/shared" }\n',
    )
    _write_text(tmp_path / "libs/shared/pyproject.toml", '[project]\nname = "shared"\n')
    clfs = [_clf("api", "services/api", "service"), _clf("shared", "libs/shared")]
    _, edges = build_cross_project_graph(tmp_path, clfs)
    pairs = {(e.from_project, e.to_project) for e in edges}
    assert ("services/api", "libs/shared") in pairs


def test_edges_empty_for_no_internal_deps(tmp_path: Path) -> None:
    # Two unrelated packages — no edges, all fan_in 0.
    _write_json(tmp_path / "packages/a/package.json", {"name": "@x/a", "main": "i.ts"})
    _write_json(tmp_path / "packages/b/package.json", {"name": "@x/b", "main": "i.ts"})
    nodes, edges = build_cross_project_graph(
        tmp_path, [_clf("a", "packages/a"), _clf("b", "packages/b")]
    )
    assert edges == []
    assert all(n.fan_in == 0 for n in nodes)


def test_edge_extractor_malformed_manifest_degrades(tmp_path: Path) -> None:
    # A broken package.json must not crash extraction.
    _write_text(tmp_path / "packages/a/package.json", "{not valid json")
    _write_json(tmp_path / "packages/b/package.json", {"name": "@x/b", "main": "i.ts"})
    nodes, edges = build_cross_project_graph(
        tmp_path, [_clf("a", "packages/a"), _clf("b", "packages/b")]
    )
    assert isinstance(edges, list)  # no raise


# ════════════════════════════════════════════════════════════════════════
# Phase 4a — feature -> project grouping
# ════════════════════════════════════════════════════════════════════════


def test_longest_prefix_assignment_disambiguates_siblings() -> None:
    # apps/web vs apps/web-admin must NOT collide (segment-aware prefix).
    feats = [
        _feat("f-web", ["apps/web/app/page.tsx"], "u1"),
        _feat("f-admin", ["apps/web-admin/app/page.tsx"], "u2"),
    ]
    assigns = assign_features_to_projects(feats, ["apps/web", "apps/web-admin"])
    by_uuid = {a.feature_uuid: a.project_subpath for a in assigns}
    assert by_uuid["u1"] == "apps/web"
    assert by_uuid["u2"] == "apps/web-admin"


def test_longest_prefix_prefers_deepest_nested_project() -> None:
    # A file under apps/api/v1 attributes to the deeper apps/api/v1, not apps/api.
    feats = [_feat("f", ["apps/api/v1/handlers/x.ts"], "u1")]
    assigns = assign_features_to_projects(feats, ["apps/api", "apps/api/v1"])
    assert assigns[0].project_subpath == "apps/api/v1"


def test_spanning_feature_attributes_to_dominant_and_records_spanning() -> None:
    # 3 files in apps/web, 1 in packages/ui => dominant apps/web, spanning recorded.
    feats = [
        _feat(
            "f-span",
            [
                "apps/web/a.ts",
                "apps/web/b.ts",
                "apps/web/c.ts",
                "packages/ui/d.ts",
            ],
            "u1",
        )
    ]
    assigns = assign_features_to_projects(feats, ["apps/web", "packages/ui"])
    a = assigns[0]
    assert a.project_subpath == "apps/web"
    assert a.spanning == {"apps/web": 3, "packages/ui": 1}


def test_spanning_tie_break_is_lexicographic_smallest() -> None:
    # Equal file counts across two projects => smallest subpath wins (stable).
    feats = [_feat("f", ["apps/b/x.ts", "apps/a/y.ts"], "u1")]
    assigns = assign_features_to_projects(feats, ["apps/a", "apps/b"])
    assert assigns[0].project_subpath == "apps/a"


def test_unassigned_when_no_project_owns_files() -> None:
    # Repo-root tooling file matches no project subpath.
    feats = [_feat("f-root", ["scripts/release.ts"], "u1")]
    assigns = assign_features_to_projects(feats, ["apps/web", "packages/ui"])
    assert assigns[0].project_subpath is None


def test_member_files_extend_assignment_surface() -> None:
    # paths empty but member_files carries the path => still assigned.
    f = Feature(
        name="f",
        paths=[],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=100.0,
        uuid="u1",
        member_files=[
            {
                "path": "packages/ui/comp.tsx",
                "role": "anchor",
                "confidence": 1.0,
                "evidence": "x",
                "primary": True,
            }
        ],
    )
    assigns = assign_features_to_projects([f], ["packages/ui"])
    assert assigns[0].project_subpath == "packages/ui"
    assert assigns[0].file_count == 1


def test_conservation_every_feature_assigned_exactly_once() -> None:
    feats = [
        _feat("f1", ["apps/web/a.ts"], "u1"),
        _feat("f2", ["packages/ui/b.ts"], "u2"),
        _feat("f3", ["unknown/c.ts"], "u3"),  # unassigned
        _feat("f4", ["apps/web/d.ts", "packages/ui/e.ts"], "u4"),  # spanning
    ]
    assigns = assign_features_to_projects(feats, ["apps/web", "packages/ui"])
    # exactly one assignment per feature, uuids preserved 1:1.
    assert len(assigns) == len(feats)
    assert {a.feature_uuid for a in assigns} == {"u1", "u2", "u3", "u4"}
    assigned = [a for a in assigns if a.project_subpath is not None]
    unassigned = [a for a in assigns if a.project_subpath is None]
    assert len(assigned) + len(unassigned) == len(feats)
    assert len(unassigned) == 1 and unassigned[0].feature_uuid == "u3"


# ════════════════════════════════════════════════════════════════════════
# Top-level assembly + back-compat + conservation
# ════════════════════════════════════════════════════════════════════════


def test_build_monorepo_assembly_full_shape(tmp_path: Path) -> None:
    ctx = _build_js_monorepo(tmp_path)
    feats = [
        _feat("home", ["apps/web/app/page.tsx"], "u1"),
        _feat("button", ["packages/ui/button.tsx"], "u2"),
        _feat("format", ["packages/utils/format.ts"], "u3"),
        _feat("rootcfg", ["turbo.json"], "u4"),  # unassigned
    ]
    view = build_monorepo_assembly(ctx, feats)
    assert view["is_monorepo"] is True
    # projects carry feature_uuids + counts.
    by_sub = {p["subpath"]: p for p in view["projects"]}
    assert by_sub["apps/web"]["feature_uuids"] == ["u1"]
    assert by_sub["packages/ui"]["feature_count"] == 1
    assert by_sub["packages/utils"]["feature_count"] == 1
    # graph edges present.
    g = view["cross_project_graph"]
    assert {(e["from"], e["to"]) for e in g["edges"]} >= {
        ("apps/web", "packages/ui"),
        ("apps/web", "packages/utils"),
        ("packages/ui", "packages/utils"),
    }
    # fan_in surfaced on utils node.
    util_node = next(n for n in g["nodes"] if n["subpath"] == "packages/utils")
    assert util_node["fan_in"] == 2
    # unassigned recorded.
    assert {u["uuid"] for u in view["unassigned_features"]} == {"u4"}
    # stats conserve.
    s = view["stats"]
    assert s["feature_total"] == 4
    assert s["assigned"] + s["unassigned"] == 4
    assert s["assigned"] == 3


def test_assembly_conservation_no_feature_lost(tmp_path: Path) -> None:
    ctx = _build_js_monorepo(tmp_path)
    feats = [
        _feat("a", ["apps/web/x.ts"], "u1"),
        _feat("b", ["packages/ui/y.ts", "apps/web/z.ts"], "u2"),  # spanning
        _feat("c", ["nowhere/w.ts"], "u3"),  # unassigned
    ]
    view = build_monorepo_assembly(ctx, feats)
    # Every uuid appears EXACTLY once across project feature_uuids + unassigned.
    seen: list[str] = []
    for p in view["projects"]:
        seen.extend(p["feature_uuids"])
    seen.extend(u["uuid"] for u in view["unassigned_features"])
    assert sorted(seen) == ["u1", "u2", "u3"]
    assert len(seen) == len(set(seen))  # no double-assignment


def test_single_repo_backcompat_trivial_view(tmp_path: Path) -> None:
    # No workspaces => not a monorepo => trivial view.
    _write_json(tmp_path / "package.json", {"name": "solo", "main": "i.ts"})
    ctx = _ctx(tmp_path, None)
    feats = [_feat("a", ["src/a.ts"], "u1")]
    view = build_monorepo_assembly(ctx, feats)
    assert view == {"is_monorepo": False}


def test_single_workspace_is_not_a_monorepo(tmp_path: Path) -> None:
    # One declared workspace < MIN_WORKSPACES_FOR_PARTITION => trivial.
    _write_json(tmp_path / "packages/only/package.json", {"name": "@x/only", "main": "i.ts"})
    ws = [_ws("packages/only", "only", {"name": "@x/only", "main": "i.ts"})]
    ctx = _ctx(tmp_path, ws)
    view = build_monorepo_assembly(ctx, [_feat("a", ["packages/only/a.ts"], "u1")])
    assert view["is_monorepo"] is False


def test_existing_fields_byte_untouched_by_monorepo(tmp_path: Path) -> None:
    """The monorepo field is purely additive: features/developer_features dump
    identically whether or not a monorepo view is attached."""
    feats = [_feat("a", ["apps/web/a.ts"], "u1"), _feat("b", ["packages/ui/b.ts"], "u2")]
    base = FeatureMap(
        repo_path="/x",
        analyzed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        total_commits=0,
        date_range_days=0,
        developer_features=list(feats),
    )
    withmono = FeatureMap(
        repo_path="/x",
        analyzed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        total_commits=0,
        date_range_days=0,
        developer_features=list(feats),
        monorepo={"is_monorepo": True, "projects": [{"subpath": "apps/web"}]},
    )
    d_base = base.model_dump()
    d_mono = withmono.model_dump()
    # Every key EXCEPT monorepo is identical.
    for key in d_base:
        if key == "monorepo":
            continue
        assert d_base[key] == d_mono[key], f"field {key} changed"
    assert d_base["developer_features"] == d_mono["developer_features"]
    assert d_base["features"] == d_mono["features"]


# ════════════════════════════════════════════════════════════════════════
# Scale-invariance (rule-no-magic-tuning) — tiny / medium / large
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("n_libs", [1, 10, 200])
def test_scale_invariant_grouping_and_fan_in(tmp_path: Path, n_libs: int) -> None:
    """One app depending on N shared libs behaves identically at every N.

    No magic threshold: the app must get its feature, every lib must get its
    own feature, and the most-depended lib must show the right fan_in — for
    a 1-lib, 10-lib, AND 200-lib repo.
    """
    # App depends on every lib; lib_0 is also depended on by every OTHER lib
    # (so its fan_in == n_libs).
    app_deps = {f"@s/lib{i}": "workspace:*" for i in range(n_libs)}
    _write_json(
        tmp_path / "apps/web/package.json",
        {"name": "web", "dependencies": {**app_deps, "react": "^18"}},
    )
    (tmp_path / "apps/web/app").mkdir(parents=True, exist_ok=True)
    clfs = [_clf("web", "apps/web", "app")]
    feats = [_feat("home", ["apps/web/app/page.tsx"], "uapp")]
    for i in range(n_libs):
        deps = {"@s/lib0": "workspace:*"} if i != 0 else {}
        _write_json(
            tmp_path / f"packages/lib{i}/package.json",
            {"name": f"@s/lib{i}", "main": "i.ts", "dependencies": deps},
        )
        clfs.append(_clf(f"lib{i}", f"packages/lib{i}"))
        feats.append(_feat(f"feat{i}", [f"packages/lib{i}/m.ts"], f"u{i}"))

    nodes, edges = build_cross_project_graph(tmp_path, clfs)
    fan = {n.subpath: n.fan_in for n in nodes}
    # lib0: app + (n_libs-1) other libs depend on it.
    expected_lib0_fan = 1 + max(0, n_libs - 1)
    assert fan["packages/lib0"] == expected_lib0_fan

    assigns = assign_features_to_projects(feats, [c.path for c in clfs])
    # Conservation at every scale: each feature assigned exactly once, none lost.
    assert len(assigns) == len(feats)
    assert all(a.project_subpath is not None for a in assigns)
    # Each lib feature lands in its own lib.
    by_uuid = {a.feature_uuid: a.project_subpath for a in assigns}
    assert by_uuid["u0"] == "packages/lib0"
    assert by_uuid["uapp"] == "apps/web"


def test_classifier_reuse_no_divergent_enumeration(tmp_path: Path) -> None:
    """The assembly's project list IS the classifier's verdict list — same
    names, same paths, same types (no parallel re-enumeration)."""
    ctx = _build_js_monorepo(tmp_path)
    plan = partition_monorepo(ctx)
    view = build_monorepo_assembly(ctx, [])
    plan_paths = {c.path for c in plan.classifications}
    view_paths = {p["subpath"] for p in view["projects"]}
    assert plan_paths == view_paths
    # types match the classifier exactly.
    plan_types = {c.path: c.project_type for c in plan.classifications}
    for p in view["projects"]:
        assert p["type"] == plan_types[p["subpath"]]
