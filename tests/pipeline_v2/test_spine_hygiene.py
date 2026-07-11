"""Product-Spine §4.1 — bare-dir ban + concern-facet rule.

Covers, per feature: the happy path, the defensive guard, and the
kill-switch (``FAULTLINE_SPINE_BAREDIR`` / ``FAULTLINE_SPINE_FACETS``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.pipeline_v2 import (
    AnchorCandidate,
    ScanContext,
    Workspace,
    stage_2_reconcile,
)
from faultline.pipeline_v2.extractors.package import PackageAnchorExtractor
from faultline.pipeline_v2.spine_hygiene import (
    classify_concern_facets,
    concern_vocabulary,
    is_concern_name,
    is_facet,
    is_root_marker,
    strip_bare_dir_feature_paths,
    strip_bare_dir_paths,
    subtree_of,
)
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature


# ── helpers ────────────────────────────────────────────────────────────────


def _ctx(
    *,
    repo_path: Path,
    tracked_files: list[str] | None = None,
    monorepo: bool = False,
    workspaces: list[Workspace] | None = None,
) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack=None,
        monorepo=monorepo,
        workspaces=workspaces,
        tracked_files=tracked_files or [],
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _cand(name: str, source: str, paths: tuple[str, ...]) -> AnchorCandidate:
    return AnchorCandidate(
        name=name, source=source, paths=paths, confidence_self=0.8,
    )


def _dev(name: str, paths: tuple[str, ...]) -> DeveloperFeature:
    return DeveloperFeature(
        name=name, paths=paths, sources=["route"], confidence="medium",
    )


# ── bare-dir ban: source fix (package extractor) ───────────────────────────


def test_package_dep_anchor_carries_manifest_file_not_dir(tmp_path: Path) -> None:
    """Root JS manifest → anchor path is ``package.json``, never ``.``."""
    _write(tmp_path / "package.json",
           json.dumps({"dependencies": {"stripe": "^14"}}))
    ctx = _ctx(repo_path=tmp_path, tracked_files=["package.json"])
    cands = PackageAnchorExtractor().extract(ctx)
    billing = next(c for c in cands if c.name == "billing")
    assert billing.paths == ("package.json",)
    assert "." not in billing.paths


def test_package_dep_anchor_monorepo_manifest_paths(tmp_path: Path) -> None:
    """Workspace dep anchors carry ``<ws>/package.json``, not ``<ws>``."""
    web_pkg = {"name": "web", "dependencies": {"ai": "^3"}}
    _write(tmp_path / "apps" / "web" / "package.json", json.dumps(web_pkg))
    ws = Workspace(name="web", path="apps/web", package_json=web_pkg,
                   files=["apps/web/package.json"])
    ctx = _ctx(
        repo_path=tmp_path, monorepo=True, workspaces=[ws],
        tracked_files=["apps/web/package.json"],
    )
    cands = PackageAnchorExtractor().extract(ctx)
    ai = next(c for c in cands if c.name == "ai")
    assert ai.paths == ("apps/web/package.json",)


def test_package_python_anchor_carries_pyproject(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", 'dependencies = ["stripe"]')
    ctx = _ctx(repo_path=tmp_path, tracked_files=["pyproject.toml"])
    cands = PackageAnchorExtractor().extract(ctx)
    billing = next(c for c in cands if c.name == "billing")
    assert billing.paths == ("pyproject.toml",)


def test_package_extractor_kill_switch_restores_dir_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_SPINE_BAREDIR", "0")
    _write(tmp_path / "package.json",
           json.dumps({"dependencies": {"stripe": "^14"}}))
    ctx = _ctx(repo_path=tmp_path, tracked_files=["package.json"])
    cands = PackageAnchorExtractor().extract(ctx)
    billing = next(c for c in cands if c.name == "billing")
    assert billing.paths == (".",)  # legacy emission


# ── bare-dir ban: reconcile-time guard ─────────────────────────────────────


def test_guard_rejects_root_markers_and_provable_dirs() -> None:
    cands = [
        _cand("analytics", "package",
              (".", "frontend", "frontend/src/api/client.ts")),
    ]
    tele = strip_bare_dir_paths(
        cands, ["frontend/src/api/client.ts", "frontend/package.json"],
    )
    # "." (root marker) + "frontend" (provable dir) rejected; file kept.
    assert cands[0].paths == ("frontend/src/api/client.ts",)
    assert tele["paths_dropped"] == 2
    assert tele["candidates_touched"] == 1


def test_guard_keeps_unknown_nonprefix_paths() -> None:
    """A path that is neither tracked nor a dir-prefix of tracked files is
    NOT provably a directory — synthetic fixtures keep working."""
    cands = [_cand("billing", "config", ("billing.config.ts",))]
    tele = strip_bare_dir_paths(cands, ["app/page.tsx"])
    assert cands[0].paths == ("billing.config.ts",)
    assert tele["paths_dropped"] == 0


def test_guard_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_SPINE_BAREDIR", "0")
    cands = [_cand("analytics", "package", (".", "frontend"))]
    tele = strip_bare_dir_paths(cands, ["frontend/src/a.ts"])
    assert cands[0].paths == (".", "frontend")
    assert tele["paths_dropped"] == 0


def test_reconcile_end_to_end_drops_whole_repo_claim(tmp_path: Path) -> None:
    """A dep anchor claiming ONLY the repo root dies at zero-path drop; a
    same-named route feature keeps the real files."""
    cands = {
        "package": [_cand("analytics", "package", (".",))],
        "route": [_cand("dashboard", "route", ("app/dashboard/page.tsx",))],
    }
    ctx = _ctx(repo_path=tmp_path,
               tracked_files=["app/dashboard/page.tsx", "package.json"])
    result = stage_2_reconcile(cands, ctx)
    assert {f.name for f in result.features} == {"dashboard"}
    assert any("spine-baredir" in n for n in result.notes)


def test_emission_sweep_removes_root_markers() -> None:
    from faultline.models.types import MemberFile

    class _Feat:
        def __init__(self) -> None:
            self.paths = [".", "src/a.ts"]
            self.member_files = [
                MemberFile(path=".", role="anchor", confidence=1.0),
                MemberFile(path="src/a.ts", role="anchor", confidence=1.0),
            ]

    f = _Feat()
    dropped = strip_bare_dir_feature_paths([f])
    assert dropped == 2
    assert f.paths == ["src/a.ts"]
    assert [m.path for m in f.member_files] == ["src/a.ts"]


def test_emission_sweep_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_SPINE_BAREDIR", "0")

    class _Feat:
        paths = ["."]
        member_files: list = []

    assert strip_bare_dir_feature_paths([_Feat()]) == 0
    assert _Feat.paths == ["."]


def test_is_root_marker() -> None:
    assert is_root_marker(".")
    assert is_root_marker("")
    assert is_root_marker("..")
    assert is_root_marker("./")
    assert not is_root_marker("src")
    assert not is_root_marker("a/b.ts")


# ── concern-facet rule ─────────────────────────────────────────────────────


def test_concern_vocabulary_loads_and_matches() -> None:
    vocab = concern_vocabulary()
    assert "auth" in vocab and "i18n" in vocab and "billing" in vocab
    assert is_concern_name("auth") == "auth"
    assert is_concern_name("authentication") == "auth"     # alias
    assert is_concern_name("jobs-background") == "background-jobs"  # token set
    assert is_concern_name("AUTH") == "auth"               # case-folded
    # Compound vertical names never match (exact identity only).
    assert is_concern_name("admin-email-domain-management") is None
    assert is_concern_name("network-security") is None
    assert is_concern_name(None) is None


def test_subtree_of_workspace_and_route_groups() -> None:
    ws = ("apps/web", "apps/api", "packages/ui")
    assert subtree_of("apps/web/src/auth/login.ts", ws) == "apps/web"
    assert subtree_of("packages/ui/button.tsx", ws) == "packages/ui"
    # No workspace match → top-level dir, refined by a route group.
    assert subtree_of("app/(auth)/login/page.tsx", ()) == "app/(auth)"
    assert subtree_of("app/(dashboard)/settings/page.tsx", ()) == "app/(dashboard)"
    assert subtree_of("lib/auth.ts", ()) == "lib"
    assert subtree_of("package.json", ()) == "."


def test_classify_concern_facets_multi_subtree() -> None:
    devs = [
        # auth spans two workspaces → facet.
        _dev("auth", ("apps/web/src/auth/a.ts", "apps/api/src/auth/b.py")),
        # billing inside ONE subtree → vertical feature, not a facet.
        _dev("billing", ("apps/web/src/billing/a.ts",
                         "apps/web/src/billing/b.ts")),
        # non-concern name spanning subtrees → untouched.
        _dev("bookings", ("apps/web/src/b.ts", "apps/api/src/b.py")),
    ]
    tele = classify_concern_facets(devs, ("apps/web", "apps/api"))
    assert tele["facets"] == 1
    assert devs[0].role == "facet" and is_facet(devs[0])
    assert devs[1].role is None
    assert devs[2].role is None


def test_classify_root_manifest_does_not_count_as_subtree() -> None:
    """A root manifest (package.json → "." pseudo-subtree) plus ONE real
    subtree is still single-subtree — no facet demotion."""
    dev = _dev("billing", ("package.json", "app/billing/page.tsx"))
    tele = classify_concern_facets([dev], ())
    assert tele["facets"] == 0
    assert dev.role is None


def test_classify_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_SPINE_FACETS", "0")
    dev = _dev("auth", ("apps/web/a.ts", "apps/api/b.py"))
    tele = classify_concern_facets([dev], ("apps/web", "apps/api"))
    assert tele["facets"] == 0
    assert dev.role is None


def test_reconcile_end_to_end_marks_facet(tmp_path: Path) -> None:
    cands = {
        "route": [
            _cand("auth", "route",
                  ("app/(auth)/login/page.tsx", "lib/auth.ts",
                   "components/auth/form.tsx")),
            _cand("bookings", "route", ("app/bookings/page.tsx",)),
        ],
    }
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=[
            "app/(auth)/login/page.tsx", "lib/auth.ts",
            "components/auth/form.tsx", "app/bookings/page.tsx",
        ],
    )
    result = stage_2_reconcile(cands, ctx)
    by_name = {f.name: f for f in result.features}
    assert by_name["auth"].role == "facet"       # 3 subtrees
    assert by_name["bookings"].role is None
    assert any("spine-facets" in n for n in result.notes)


# ── facet exclusion wiring ─────────────────────────────────────────────────


def test_facet_excluded_from_6_5_clustering(tmp_path: Path) -> None:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.stage_6_5_product_clusterer import (
        run_product_clusterer,
    )

    def _feat(name: str, paths: list[str], role: str | None = None) -> Feature:
        from datetime import datetime, timezone
        return Feature(
            name=name, paths=paths, authors=[], total_commits=0,
            bug_fixes=0, bug_fix_ratio=0.0,
            last_modified=datetime.fromtimestamp(0, timezone.utc),
            health_score=80.0, layer="developer", role=role,
        )

    ws = Workspace(name="web", path="apps/web", package_json={"name": "web"},
                   files=["apps/web/src/a.ts", "apps/web/src/b.ts"])
    ctx = _ctx(repo_path=tmp_path, monorepo=True, workspaces=[ws],
               tracked_files=["apps/web/src/a.ts", "apps/web/src/b.ts"])
    facet = _feat("auth", ["apps/web/src/a.ts", "apps/api/src/b.py"],
                  role="facet")
    normal = _feat("web-app", ["apps/web/src/a.ts", "apps/web/src/b.ts"])
    _, mapping, _ = run_product_clusterer(ctx, [facet, normal])
    assert "auth" not in mapping  # facet received no votes / no membership


def test_facet_excluded_from_8_5_backfill() -> None:
    from faultline.pipeline_v2.stage_8_5_member_backfill import (
        run_stage_8_5_backfill,
    )

    class _F:
        def __init__(self, name: str, paths: list[str],
                     role: str | None = None) -> None:
            self.name = name
            self.paths = paths
            self.product_feature_id: str | None = None
            self.role = role

    facet = _F("email", ["apps/web/a.ts", "apps/api/b.py"], role="facet")
    normal = _F("dashboard", ["apps/web/a.ts", "apps/web/b.ts"])
    pf = _F("Web App", ["apps/web/a.ts", "apps/web/b.ts", "apps/api/b.py"])
    result = run_stage_8_5_backfill([facet, normal], [pf], enabled=True)
    assert facet.product_feature_id is None       # facet skipped
    assert normal.product_feature_id == "Web App"  # normal backfilled
    assert result.attached == 1


def test_facet_loses_primary_ownership_in_6_97(tmp_path: Path) -> None:
    """A file claimed by both a facet and a structural owner counts at the
    structural owner; the facet's lines land in loc_shared."""
    from datetime import datetime, timezone

    from faultline.models.types import Feature
    from faultline.pipeline_v2.stage_6_97_feature_loc import apply_feature_loc

    shared = tmp_path / "src" / "auth" / "helper.ts"
    _write(shared, "export const a = 1\nexport const b = 2\n")

    def _feat(name: str, role: str | None = None) -> Feature:
        return Feature(
            name=name, paths=["src/auth/helper.ts"], authors=[],
            total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
            last_modified=datetime.fromtimestamp(0, timezone.utc),
            health_score=80.0, layer="developer", role=role,
        )

    facet = _feat("auth", role="facet")
    owner = _feat("accounts")
    apply_feature_loc([facet, owner], [], tmp_path)
    # Structural owner is PRIMARY: the file's lines are its owned loc.
    assert owner.loc == 2 and owner.loc_shared == 0
    # The facet sees the file as SHARED (loc_shared); its ``loc`` may carry
    # the I2 visibility floor (largest counted file) but that floor is
    # dev-level display only — the PF rollup reads the disjoint primary-owned
    # set, so facet lines can never reach a product feature.
    assert facet.loc_shared == 2


def test_facet_role_serialization_roundtrip() -> None:
    """role omitted from dumps when None (byte-compat), present for facets."""
    from datetime import datetime, timezone

    from faultline.models.types import Feature

    kw = dict(
        paths=["a.ts"], authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0,
    )
    plain = Feature(name="x", **kw).model_dump()
    assert "role" not in plain
    assert "loc_flow" not in plain and "loc_flow_shared" not in plain
    facet = Feature(name="y", role="facet", **kw).model_dump()
    assert facet["role"] == "facet"
    # Rehydration keeps the marker.
    assert Feature(**facet).role == "facet"


# ── data drift guard (house pattern: eval/ authoring == packaged copy) ─────


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "eval").exists(),
    reason="eval/ is local/private-only (scrubbed 2026-07-11)",
)
def test_concern_facets_yaml_matches_eval_authoring_copy() -> None:
    from faultline.pipeline_v2.data import load_data_text

    repo_root = Path(__file__).resolve().parents[2]
    authoring = (repo_root / "eval" / "concern-facets.yaml").read_text(
        encoding="utf-8",
    )
    load_data_text.cache_clear()
    packaged = load_data_text("concern-facets.yaml")
    assert packaged == authoring, (
        "DRIFT: faultline/pipeline_v2/data/concern-facets.yaml differs from "
        "eval/concern-facets.yaml. Re-sync the in-package copy."
    )
