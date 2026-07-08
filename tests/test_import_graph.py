"""Tests for import-graph feature clustering."""
from __future__ import annotations

import pytest

from faultline.analyzer.ast_extractor import FileSignature
from faultline.analyzer.import_graph import (
    _UnionFind,
    _cluster_name,
    _feature_name_from_path,
    _finalize_clusters,
    _resolve_import,
    _try_extensions,
    build_import_clusters,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sig(path: str, imports: list[str] | None = None) -> FileSignature:
    """Shorthand to build a FileSignature with only imports populated."""
    return FileSignature(
        path=path,
        imports=imports or [],
    )


# ---------------------------------------------------------------------------
# _UnionFind
# ---------------------------------------------------------------------------


class TestUnionFind:
    def test_find_returns_self_for_new_node(self) -> None:
        uf = _UnionFind(["a", "b", "c"])
        assert uf.find("a") == "a"
        assert uf.find("b") == "b"

    def test_union_merges_two_nodes(self) -> None:
        uf = _UnionFind(["a", "b", "c"])
        uf.union("a", "b")
        assert uf.find("a") == uf.find("b")

    def test_union_is_transitive(self) -> None:
        uf = _UnionFind(["a", "b", "c"])
        uf.union("a", "b")
        uf.union("b", "c")
        assert uf.find("a") == uf.find("c")

    def test_union_same_node_is_noop(self) -> None:
        uf = _UnionFind(["a", "b"])
        uf.union("a", "a")
        assert uf.find("a") == "a"

    def test_groups_returns_connected_components(self) -> None:
        uf = _UnionFind(["a", "b", "c", "d"])
        uf.union("a", "b")
        uf.union("c", "d")
        groups = uf.groups()
        assert len(groups) == 2
        group_sets = [set(members) for members in groups.values()]
        assert {"a", "b"} in group_sets
        assert {"c", "d"} in group_sets

    def test_groups_singletons(self) -> None:
        uf = _UnionFind(["a", "b", "c"])
        groups = uf.groups()
        assert len(groups) == 3

    def test_path_compression(self) -> None:
        """After find, parent should point directly to root."""
        uf = _UnionFind(["a", "b", "c", "d"])
        uf.union("a", "b")
        uf.union("b", "c")
        uf.union("c", "d")
        root = uf.find("d")
        # After path compression, d's parent should be the root directly
        assert uf._parent["d"] == root

    def test_union_by_rank(self) -> None:
        """Union-by-rank keeps the higher-rank tree as root."""
        uf = _UnionFind(["a", "b", "c", "d", "e"])
        # Build a taller tree for a-b-c
        uf.union("a", "b")
        uf.union("a", "c")
        # Build a smaller tree for d-e
        uf.union("d", "e")
        # Union the two trees
        uf.union("a", "d")
        # All five should share the same root
        roots = {uf.find(n) for n in ["a", "b", "c", "d", "e"]}
        assert len(roots) == 1


# ---------------------------------------------------------------------------
# _try_extensions
# ---------------------------------------------------------------------------


class TestTryExtensions:
    def test_exact_match(self) -> None:
        file_set = {"auth/login.ts", "auth/signup.ts"}
        assert _try_extensions("auth/login.ts", file_set) == "auth/login.ts"

    def test_appends_ts_extension(self) -> None:
        file_set = {"auth/login.ts"}
        assert _try_extensions("auth/login", file_set) == "auth/login.ts"

    def test_appends_tsx_extension(self) -> None:
        file_set = {"components/Button.tsx"}
        assert _try_extensions("components/Button", file_set) == "components/Button.tsx"

    def test_appends_js_extension(self) -> None:
        file_set = {"utils/helpers.js"}
        assert _try_extensions("utils/helpers", file_set) == "utils/helpers.js"

    def test_resolves_index_file(self) -> None:
        file_set = {"auth/index.ts"}
        assert _try_extensions("auth", file_set) == "auth/index.ts"

    def test_resolves_index_tsx(self) -> None:
        file_set = {"components/index.tsx"}
        assert _try_extensions("components", file_set) == "components/index.tsx"

    def test_returns_none_when_not_found(self) -> None:
        file_set = {"auth/login.ts"}
        assert _try_extensions("auth/signup", file_set) is None

    def test_prefers_exact_match_over_extension(self) -> None:
        file_set = {"utils/config", "utils/config.ts"}
        assert _try_extensions("utils/config", file_set) == "utils/config"

    def test_prefers_ts_over_tsx(self) -> None:
        """Extension order: .ts is tried before .tsx."""
        file_set = {"auth/login.ts", "auth/login.tsx"}
        assert _try_extensions("auth/login", file_set) == "auth/login.ts"


# ---------------------------------------------------------------------------
# _resolve_import
# ---------------------------------------------------------------------------


class TestResolveImport:
    @pytest.fixture()
    def file_set(self) -> set[str]:
        return {
            "auth/login.ts",
            "auth/signup.ts",
            "auth/index.ts",
            "shared/utils.ts",
            "shared/types.ts",
            "src/api/users.ts",
            "api/users.ts",
            "components/Button.tsx",
        }

    def test_relative_same_dir(self, file_set: set[str]) -> None:
        result = _resolve_import("auth/login.ts", "./signup", file_set)
        assert result == "auth/signup.ts"

    def test_relative_parent_dir(self, file_set: set[str]) -> None:
        result = _resolve_import("auth/login.ts", "../shared/utils", file_set)
        assert result == "shared/utils.ts"

    def test_relative_directory_index(self, file_set: set[str]) -> None:
        result = _resolve_import("shared/utils.ts", "../auth", file_set)
        assert result == "auth/index.ts"

    def test_alias_at_sign(self, file_set: set[str]) -> None:
        result = _resolve_import("auth/login.ts", "@/shared/utils", file_set)
        assert result == "shared/utils.ts"

    def test_alias_tilde(self, file_set: set[str]) -> None:
        result = _resolve_import("auth/login.ts", "~/shared/utils", file_set)
        assert result == "shared/utils.ts"

    def test_alias_hash(self, file_set: set[str]) -> None:
        result = _resolve_import("auth/login.ts", "#/shared/utils", file_set)
        assert result == "shared/utils.ts"

    def test_alias_tries_src_prefix(self, file_set: set[str]) -> None:
        result = _resolve_import("auth/login.ts", "@/api/users", file_set)
        # Should find api/users.ts (no src prefix) first
        assert result == "api/users.ts"

    def test_alias_falls_back_to_src_prefix(self) -> None:
        file_set = {"src/api/users.ts"}
        result = _resolve_import("auth/login.ts", "@/api/users", file_set)
        assert result == "src/api/users.ts"

    def test_bare_import_returns_none(self, file_set: set[str]) -> None:
        """Third-party packages (no ./ or alias prefix) should be skipped."""
        assert _resolve_import("auth/login.ts", "react", file_set) is None
        assert _resolve_import("auth/login.ts", "lodash/merge", file_set) is None

    def test_relative_above_root_returns_none(self, file_set: set[str]) -> None:
        result = _resolve_import("auth/login.ts", "../../outside", file_set)
        assert result is None

    def test_self_import_not_returned(self) -> None:
        """_resolve_import does not filter self-imports; build_import_clusters does."""
        file_set = {"auth/login.ts"}
        # ./login resolves to auth/login.ts which is the importer itself.
        # _resolve_import returns it; the caller filters self-imports.
        result = _resolve_import("auth/login.ts", "./login", file_set)
        assert result == "auth/login.ts"


# ---------------------------------------------------------------------------
# _feature_name_from_path
# ---------------------------------------------------------------------------


class TestFeatureNameFromPath:
    def test_returns_first_meaningful_dir(self) -> None:
        assert _feature_name_from_path("auth/login.ts") == "auth"

    def test_skips_src_prefix(self) -> None:
        assert _feature_name_from_path("src/auth/login.ts") == "auth"

    def test_skips_app_prefix(self) -> None:
        assert _feature_name_from_path("app/dashboard/page.tsx") == "dashboard"

    def test_skips_multiple_generic_dirs(self) -> None:
        assert _feature_name_from_path("src/app/lib/payments/stripe.ts") == "payments"

    def test_returns_root_for_top_level_file(self) -> None:
        assert _feature_name_from_path("index.ts") == "root"

    def test_returns_root_when_all_dirs_generic(self) -> None:
        assert _feature_name_from_path("src/app/index.ts") == "root"

    def test_lowercases_dir_name(self) -> None:
        assert _feature_name_from_path("Auth/LoginForm.tsx") == "auth"

    def test_skips_components_dir(self) -> None:
        assert _feature_name_from_path("components/ui/Button.tsx") == "ui"

    def test_skips_features_dir(self) -> None:
        assert _feature_name_from_path("features/billing/Plan.tsx") == "billing"


# ---------------------------------------------------------------------------
# _cluster_name
# ---------------------------------------------------------------------------


class TestClusterName:
    def test_picks_most_common_dir(self) -> None:
        files = [
            "auth/login.ts",
            "auth/signup.ts",
            "auth/utils.ts",
            "shared/types.ts",
        ]
        assert _cluster_name(files) == "auth"

    def test_single_file(self) -> None:
        assert _cluster_name(["dashboard/page.tsx"]) == "dashboard"

    def test_tie_broken_by_max(self) -> None:
        """When counts are equal, max() returns one of them deterministically."""
        files = ["auth/login.ts", "billing/plan.ts"]
        result = _cluster_name(files)
        assert result in {"auth", "billing"}


# ---------------------------------------------------------------------------
# _finalize_clusters
# ---------------------------------------------------------------------------


class TestFinalizeClusters:
    def test_multi_file_cluster_gets_dir_name(self) -> None:
        raw = {"root1": ["auth/login.ts", "auth/signup.ts"]}
        result = _finalize_clusters(raw)
        assert "auth" in result
        assert set(result["auth"]) == {"auth/login.ts", "auth/signup.ts"}

    def test_singleton_absorbed_into_same_dir_cluster(self) -> None:
        raw = {
            "root1": ["auth/login.ts", "auth/signup.ts"],
            "root2": ["auth/utils.ts"],
        }
        result = _finalize_clusters(raw)
        assert "auth" in result
        assert "auth/utils.ts" in result["auth"]

    def test_singleton_creates_own_cluster_if_no_match(self) -> None:
        raw = {
            "root1": ["auth/login.ts", "auth/signup.ts"],
            "root2": ["billing/plan.ts"],
        }
        result = _finalize_clusters(raw)
        assert "billing" in result
        assert result["billing"] == ["billing/plan.ts"]

    def test_duplicate_name_gets_suffix(self) -> None:
        raw = {
            "root1": ["auth/login.ts", "auth/signup.ts"],
            "root2": ["src/auth/reset.ts", "src/auth/verify.ts"],
        }
        result = _finalize_clusters(raw)
        names = list(result.keys())
        assert "auth" in names
        assert "auth-2" in names

    def test_members_are_sorted(self) -> None:
        raw = {"root1": ["z/file.ts", "a/file.ts", "m/file.ts"]}
        result = _finalize_clusters(raw)
        name = list(result.keys())[0]
        assert result[name] == sorted(result[name])

    def test_empty_input(self) -> None:
        assert _finalize_clusters({}) == {}

    def test_all_singletons_bucketed_by_dir(self) -> None:
        raw = {
            "r1": ["auth/login.ts"],
            "r2": ["auth/signup.ts"],
            "r3": ["billing/plan.ts"],
        }
        result = _finalize_clusters(raw)
        # auth/login.ts and auth/signup.ts should be grouped together
        assert any(
            "auth/login.ts" in members and "auth/signup.ts" in members
            for members in result.values()
        )
        assert any("billing/plan.ts" in members for members in result.values())


# ---------------------------------------------------------------------------
# build_import_clusters — integration
# ---------------------------------------------------------------------------


class TestBuildImportClusters:
    def test_empty_files(self) -> None:
        assert build_import_clusters([], {}) == {}

    def test_files_connected_by_imports_same_cluster(self) -> None:
        files = ["auth/login.ts", "auth/signup.ts", "auth/utils.ts"]
        sigs = {
            "auth/login.ts": _sig("auth/login.ts", ["./utils"]),
            "auth/signup.ts": _sig("auth/signup.ts", ["./utils"]),
            "auth/utils.ts": _sig("auth/utils.ts"),
        }
        result = build_import_clusters(files, sigs)
        # All three should be in the same cluster
        all_files = set()
        for members in result.values():
            if "auth/login.ts" in members:
                all_files = set(members)
                break
        assert all_files == {"auth/login.ts", "auth/signup.ts", "auth/utils.ts"}

    def test_unrelated_files_stay_separate(self) -> None:
        files = [
            "auth/login.ts",
            "auth/signup.ts",
            "billing/plan.ts",
            "billing/invoice.ts",
        ]
        sigs = {
            "auth/login.ts": _sig("auth/login.ts", ["./signup"]),
            "auth/signup.ts": _sig("auth/signup.ts"),
            "billing/plan.ts": _sig("billing/plan.ts", ["./invoice"]),
            "billing/invoice.ts": _sig("billing/invoice.ts"),
        }
        result = build_import_clusters(files, sigs)
        # Find the cluster containing auth/login.ts
        auth_cluster = None
        billing_cluster = None
        for members in result.values():
            if "auth/login.ts" in members:
                auth_cluster = set(members)
            if "billing/plan.ts" in members:
                billing_cluster = set(members)
        assert auth_cluster is not None
        assert billing_cluster is not None
        assert auth_cluster & billing_cluster == set()

    def test_hub_file_does_not_merge_clusters(self) -> None:
        """A hub file imported by many features should not bridge them."""
        # Create a hub file imported by more than _BASE_IMPORT_FANIN (8) files
        hub = "shared/types.ts"
        feature_a_files = [f"auth/file{i}.ts" for i in range(5)]
        feature_b_files = [f"billing/file{i}.ts" for i in range(5)]
        files = feature_a_files + feature_b_files + [hub]

        sigs: dict[str, FileSignature] = {hub: _sig(hub)}
        for f in feature_a_files:
            sigs[f] = _sig(f, ["../shared/types"])
        for f in feature_b_files:
            sigs[f] = _sig(f, ["../shared/types"])

        # Also add intra-feature imports so auth and billing form their own clusters
        sigs[feature_a_files[0]] = _sig(
            feature_a_files[0], ["../shared/types", "./file1"]
        )
        sigs[feature_b_files[0]] = _sig(
            feature_b_files[0], ["../shared/types", "./file1"]
        )

        result = build_import_clusters(files, sigs)

        # auth and billing should NOT be in the same cluster
        auth_cluster = None
        billing_cluster = None
        for members in result.values():
            if feature_a_files[0] in members:
                auth_cluster = set(members)
            if feature_b_files[0] in members:
                billing_cluster = set(members)
        assert auth_cluster is not None
        assert billing_cluster is not None
        assert auth_cluster & billing_cluster == set()

    def test_oversized_cluster_split_by_directory(self) -> None:
        """Clusters exceeding _MAX_CLUSTER_FRACTION get split by dir."""
        # Create a chain of imports across many dirs that forms one giant cluster
        # With 20 files, max_size = max(20, int(20 * 0.25)) = 20
        # Need > 20 files to trigger split
        auth_files = [f"auth/file{i}.ts" for i in range(12)]
        billing_files = [f"billing/file{i}.ts" for i in range(12)]
        files = auth_files + billing_files

        sigs: dict[str, FileSignature] = {}
        # Chain them all together: auth0 -> auth1 -> ... -> billing0 -> billing1 -> ...
        for i, f in enumerate(files[:-1]):
            next_file = files[i + 1]
            # Build a relative import path
            sigs[f] = _sig(f, [f"../{next_file.replace('.ts', '')}"])
        sigs[files[-1]] = _sig(files[-1])

        result = build_import_clusters(files, sigs)
        # The cluster should have been split: no single cluster has all 24 files
        for members in result.values():
            assert len(members) < len(files)

    def test_signatures_not_in_file_set_ignored(self) -> None:
        """Signatures for files not in the files list should be skipped."""
        files = ["auth/login.ts"]
        sigs = {
            "auth/login.ts": _sig("auth/login.ts", ["./utils"]),
            "auth/utils.ts": _sig("auth/utils.ts"),
            "extra/ignored.ts": _sig("extra/ignored.ts", ["../auth/login"]),
        }
        result = build_import_clusters(files, sigs)
        all_files = set()
        for members in result.values():
            all_files.update(members)
        assert "extra/ignored.ts" not in all_files

    def test_self_import_skipped(self) -> None:
        """A file importing itself should not cause issues."""
        files = ["auth/login.ts"]
        sigs = {"auth/login.ts": _sig("auth/login.ts", ["./login"])}
        # Should not crash; login.ts resolves to itself and is skipped
        result = build_import_clusters(files, sigs)
        assert len(result) >= 1

    def test_alias_imports_connect_files(self) -> None:
        files = ["auth/login.ts", "shared/utils.ts"]
        sigs = {
            "auth/login.ts": _sig("auth/login.ts", ["@/shared/utils"]),
            "shared/utils.ts": _sig("shared/utils.ts"),
        }
        result = build_import_clusters(files, sigs)
        # Both files should be in the same cluster
        found = False
        for members in result.values():
            if "auth/login.ts" in members and "shared/utils.ts" in members:
                found = True
                break
        assert found, f"Expected both files in same cluster, got {result}"

    def test_singleton_absorbed_into_cluster(self) -> None:
        """A file with no imports in the same dir as a cluster gets absorbed."""
        files = [
            "auth/login.ts",
            "auth/signup.ts",
            "auth/constants.ts",
        ]
        sigs = {
            "auth/login.ts": _sig("auth/login.ts", ["./signup"]),
            "auth/signup.ts": _sig("auth/signup.ts"),
            "auth/constants.ts": _sig("auth/constants.ts"),
        }
        result = build_import_clusters(files, sigs)
        # constants.ts should be absorbed into the auth cluster
        auth_members = None
        for members in result.values():
            if "auth/login.ts" in members:
                auth_members = set(members)
                break
        assert auth_members is not None
        assert "auth/constants.ts" in auth_members

    def test_no_signatures_falls_back_to_dir_grouping(self) -> None:
        """Files with no signatures become singletons grouped by directory."""
        files = ["auth/login.ts", "auth/signup.ts", "billing/plan.ts"]
        sigs: dict[str, FileSignature] = {}
        result = build_import_clusters(files, sigs)
        # auth files should be grouped together as orphan singletons
        auth_members = None
        for members in result.values():
            if "auth/login.ts" in members:
                auth_members = set(members)
                break
        assert auth_members is not None
        assert "auth/signup.ts" in auth_members


# ---------------------------------------------------------------------------
# Workspace package map + scoped cross-package import resolution
# (universal pnpm / npm / yarn workspaces + tsconfig path aliases)
# ---------------------------------------------------------------------------

import json as _json_test
import os as _os_test

from faultline.analyzer.import_graph import (
    detect_workspace_package_map,
    _read_workspace_globs,
    _expand_workspace_glob,
    _resolve_workspace_package_import,
)


def _write(root: str, rel: str, content: str = "{}") -> None:
    p = _os_test.path.join(root, rel)
    _os_test.makedirs(_os_test.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


class TestWorkspaceGlobReading:
    def test_npm_yarn_workspaces_array(self, tmp_path) -> None:
        _write(str(tmp_path), "package.json",
               _json_test.dumps({"workspaces": ["packages/*", "apps/*"]}))
        assert _read_workspace_globs(str(tmp_path)) == ["packages/*", "apps/*"]

    def test_yarn_classic_object_shape(self, tmp_path) -> None:
        _write(str(tmp_path), "package.json",
               _json_test.dumps({"workspaces": {"packages": ["libs/*"]}}))
        assert _read_workspace_globs(str(tmp_path)) == ["libs/*"]

    def test_pnpm_workspace_yaml(self, tmp_path) -> None:
        _write(str(tmp_path), "pnpm-workspace.yaml",
               "packages:\n  - 'packages/*'\n  - \"apps/api/*\"\n  - tools/cli\n")
        globs = _read_workspace_globs(str(tmp_path))
        assert globs == ["packages/*", "apps/api/*", "tools/cli"]

    def test_no_config_returns_empty(self, tmp_path) -> None:
        assert _read_workspace_globs(str(tmp_path)) == []


class TestExpandWorkspaceGlob:
    def test_single_wildcard_lists_dirs(self, tmp_path) -> None:
        _os_test.makedirs(str(tmp_path / "packages" / "lib"))
        _os_test.makedirs(str(tmp_path / "packages" / "ui"))
        _write(str(tmp_path), "packages/readme.txt", "x")  # not a dir → ignored
        got = _expand_workspace_glob(str(tmp_path), "packages/*")
        assert got == ["packages/lib", "packages/ui"]

    def test_nested_wildcard(self, tmp_path) -> None:
        _os_test.makedirs(str(tmp_path / "packages" / "features" / "billing"))
        got = _expand_workspace_glob(str(tmp_path), "packages/features/*")
        assert got == ["packages/features/billing"]

    def test_literal_dir(self, tmp_path) -> None:
        _os_test.makedirs(str(tmp_path / "packages" / "app-store"))
        assert _expand_workspace_glob(str(tmp_path), "packages/app-store") == [
            "packages/app-store"
        ]

    def test_multi_wildcard_skipped_by_default(self, tmp_path) -> None:
        # Legacy byte-identity: ``**`` globs are skipped unless deep=True.
        _os_test.makedirs(str(tmp_path / "packages" / "emails"))
        _write(str(tmp_path), "packages/emails/package.json", "{}")
        assert _expand_workspace_glob(str(tmp_path), "packages/**/src") == []
        assert _expand_workspace_glob(str(tmp_path), "packages/**/*") == []
        assert _expand_workspace_glob(str(tmp_path), "packages/**") == []

    def test_deep_glob_walks_nested_manifest_dirs(self, tmp_path) -> None:
        # Track-A: the standard pnpm ``packages/**/*`` layout — packages
        # both DIRECTLY under packages/ and NESTED (notifications/email).
        _write(str(tmp_path), "packages/emails/package.json", "{}")
        _write(str(tmp_path), "packages/db/package.json", "{}")
        _write(str(tmp_path), "packages/notifications/email/package.json", "{}")
        _write(str(tmp_path), "packages/readme.md", "x")  # no manifest → skip
        got = _expand_workspace_glob(str(tmp_path), "packages/**/*", deep=True)
        assert got == [
            "packages/db",
            "packages/emails",
            "packages/notifications/email",
        ]
        # ``packages/**`` (no trailing /*) behaves identically.
        assert _expand_workspace_glob(
            str(tmp_path), "packages/**", deep=True) == got

    def test_deep_glob_prunes_node_modules(self, tmp_path) -> None:
        _write(str(tmp_path), "packages/ui/package.json", "{}")
        # a dependency manifest nested in node_modules must NEVER surface.
        _write(str(tmp_path),
               "packages/ui/node_modules/left-pad/package.json", "{}")
        _write(str(tmp_path), "packages/ui/dist/package.json", "{}")
        got = _expand_workspace_glob(str(tmp_path), "packages/**/*", deep=True)
        assert got == ["packages/ui"]

    def test_deep_glob_missing_prefix_is_empty(self, tmp_path) -> None:
        assert _expand_workspace_glob(
            str(tmp_path), "packages/**/*", deep=True) == []


class TestDetectWorkspacePackageMap:
    def _scaffold(self, root: str) -> None:
        _write(root, "package.json", _json_test.dumps(
            {"workspaces": ["packages/*", "packages/features/*", "apps/*"]}))
        _write(root, "packages/lib/package.json",
               _json_test.dumps({"name": "@scope/lib", "main": "index.ts"}))
        _write(root, "packages/lib/index.ts", "export const x = 1;")
        _write(root, "packages/lib/logger.ts", "export const log = 1;")
        _write(root, "packages/prisma/package.json",
               _json_test.dumps({"name": "@scope/prisma", "main": "index.ts"}))
        _write(root, "packages/prisma/index.ts", "export const db = 1;")
        _write(root, "packages/features/billing/package.json",
               _json_test.dumps({"name": "@scope/features-billing"}))
        _write(root, "packages/features/billing/charge.ts", "export const c = 1;")
        _write(root, "apps/web/package.json",
               _json_test.dumps({"name": "@scope/web"}))
        _write(root, "apps/web/lib/api.ts", "export const a = 1;")

    def test_name_to_dir_map(self, tmp_path) -> None:
        self._scaffold(str(tmp_path))
        m = detect_workspace_package_map(str(tmp_path))
        assert m["@scope/lib"] == "packages/lib"
        assert m["@scope/prisma"] == "packages/prisma"
        assert m["@scope/features-billing"] == "packages/features/billing"
        # package living under apps/ is mapped too
        assert m["@scope/web"] == "apps/web"

    def test_empty_when_no_workspaces(self, tmp_path) -> None:
        _write(str(tmp_path), "package.json", _json_test.dumps({"name": "solo"}))
        assert detect_workspace_package_map(str(tmp_path)) == {}

    def test_deep_pnpm_star_star_layout(self, tmp_path) -> None:
        # The measured openstatus/documenso/typebot shape: ``packages/**/*``
        # in pnpm-workspace.yaml. deep=False misses every packages/* pkg;
        # deep=True recovers them (the Track-A workspace-resolution fix).
        _write(str(tmp_path), "pnpm-workspace.yaml",
               "packages:\n  - 'apps/*'\n  - 'packages/**/*'\n")
        _write(str(tmp_path), "apps/web/package.json",
               _json_test.dumps({"name": "@scope/web"}))
        _write(str(tmp_path), "packages/emails/package.json",
               _json_test.dumps({"name": "@scope/emails"}))
        _write(str(tmp_path), "packages/notifications/email/package.json",
               _json_test.dumps({"name": "@scope/notification-emails"}))

        shallow = detect_workspace_package_map(str(tmp_path))
        assert shallow == {"@scope/web": "apps/web"}  # legacy: packages/** lost

        deep = detect_workspace_package_map(str(tmp_path), deep=True)
        assert deep["@scope/web"] == "apps/web"
        assert deep["@scope/emails"] == "packages/emails"
        assert deep["@scope/notification-emails"] == \
            "packages/notifications/email"


class TestResolveScopedWorkspaceImport:
    @pytest.fixture()
    def env(self, tmp_path):
        root = str(tmp_path)
        TestDetectWorkspacePackageMap()._scaffold(root)
        file_set = {
            "packages/lib/index.ts",
            "packages/lib/logger.ts",
            "packages/prisma/index.ts",
            "packages/features/billing/charge.ts",
            "apps/web/lib/api.ts",
        }
        m = detect_workspace_package_map(root)
        return root, file_set, m

    def test_bare_scoped_import_resolves_to_main(self, env) -> None:
        root, fs, m = env
        assert _resolve_workspace_package_import(
            "@scope/prisma", fs, m, root) == "packages/prisma/index.ts"

    def test_scoped_subpath(self, env) -> None:
        root, fs, m = env
        assert _resolve_workspace_package_import(
            "@scope/lib/logger", fs, m, root) == "packages/lib/logger.ts"

    def test_deep_subpath_under_nested_package(self, env) -> None:
        root, fs, m = env
        assert _resolve_workspace_package_import(
            "@scope/features-billing/charge", fs, m, root
        ) == "packages/features/billing/charge.ts"

    def test_package_under_apps_dir(self, env) -> None:
        root, fs, m = env
        assert _resolve_workspace_package_import(
            "@scope/web/lib/api", fs, m, root) == "apps/web/lib/api.ts"

    def test_third_party_scoped_returns_none(self, env) -> None:
        root, fs, m = env
        assert _resolve_workspace_package_import(
            "@sentry/node", fs, m, root) is None

    def test_empty_map_returns_none(self, env) -> None:
        root, fs, _ = env
        assert _resolve_workspace_package_import("@scope/lib", fs, {}, root) is None


class TestResolveImportWorkspaceFallbackIsAdditive:
    """The workspace fallback must NOT change behaviour for imports that
    already resolve via relative / alias / dir-name resolution."""

    def test_relative_still_wins_when_map_present(self, tmp_path) -> None:
        root = str(tmp_path)
        TestDetectWorkspacePackageMap()._scaffold(root)
        m = detect_workspace_package_map(root)
        fs = {"packages/lib/index.ts", "packages/lib/logger.ts"}
        # relative import resolves WITHOUT touching the workspace resolver
        got = _resolve_import(
            "packages/lib/index.ts", "./logger", fs,
            workspace_package_map=m, repo_root=root,
        )
        assert got == "packages/lib/logger.ts"

    def test_scoped_resolves_only_via_fallback(self, tmp_path) -> None:
        root = str(tmp_path)
        TestDetectWorkspacePackageMap()._scaffold(root)
        m = detect_workspace_package_map(root)
        fs = {"packages/lib/logger.ts"}
        # Without the map → None (legacy behaviour). With the map → resolves.
        assert _resolve_import("apps/web/x.ts", "@scope/lib/logger", fs) is None
        assert _resolve_import(
            "apps/web/x.ts", "@scope/lib/logger", fs,
            workspace_package_map=m, repo_root=root,
        ) == "packages/lib/logger.ts"
