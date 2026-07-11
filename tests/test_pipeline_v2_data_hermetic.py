"""Hermeticity + drift-guard tests for the in-package runtime data loader.

These cover acceptance criterion (a): each extractor's loader returns its
parsed YAML via importlib.resources with NO dependence on a repo-root
``eval/`` sibling, and the in-package copies do not drift from the
human-authoring copies under repo-root ``eval/``.

The full-wheel hermeticity proof (criterion (b)) lives in
``test_wheel_hermetic.py`` (build + fresh-venv install), kept separate so
it can be skipped in fast unit runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.data import (
    load_data_text,
    load_stack_yaml,
    load_yaml,
)

# Repo root = three levels up from this test file's package data dir.
# tests/ is a sibling of faultline/ and eval/ at the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_EVAL_STACKS = _REPO_ROOT / "eval" / "stacks"
_EVAL_DEP_ANCHORS = _REPO_ROOT / "eval" / "dependency-anchors.yaml"

_STACK_NAMES = [
    "config-manifests",
    "django",
    "express",
    "fastapi",
    "filesystem-routing",
    "go-http-router",
    "js-library",
    "mvc-controllers",
    "python-library",
    "rails-app",
    "rust-workspace",
    "schema-domains",
]


@pytest.mark.parametrize("stack", _STACK_NAMES)
def test_stack_yaml_loads_as_mapping(stack: str) -> None:
    """Each packaged stack YAML parses to a non-empty dict via resources."""
    data = load_stack_yaml(stack)
    assert isinstance(data, dict)
    assert data, f"{stack}.yaml parsed empty — packaging or data bug"


def test_dependency_anchors_loads() -> None:
    data = load_yaml("dependency-anchors.yaml")
    assert isinstance(data, dict)
    assert data, "dependency-anchors.yaml parsed empty"


def test_missing_resource_is_hard_error() -> None:
    """A missing data file must raise, never silently return {}."""
    with pytest.raises(FileNotFoundError):
        load_data_text("stacks/does-not-exist.yaml")


@pytest.mark.parametrize("stack", _STACK_NAMES)
def test_no_eval_sibling_dependence(stack: str, monkeypatch, tmp_path) -> None:
    """Loader works with cwd moved away from the repo (no eval/ on path).

    Simulates the installed-wheel situation where there is no repo-root
    ``eval/`` sibling. importlib.resources resolves against the installed
    package, so changing the working directory must not affect the result.
    """
    monkeypatch.chdir(tmp_path)
    # Bust the lru_cache so the read genuinely re-resolves from this cwd.
    load_data_text.cache_clear()
    load_yaml.cache_clear()
    data = load_stack_yaml(stack)
    assert isinstance(data, dict) and data


# ── Drift guard: in-package data must be byte-identical to eval/ authoring ──

# eval/ is local/private-only (gitignored; scrubbed from history 2026-07-11):
# worktrees and fresh public clones don't have it — the drift guards below
# can only run where the authoring copies exist. Everything above stays
# hermetic (packaged data only) and runs everywhere.
_requires_eval = pytest.mark.skipif(
    not (_REPO_ROOT / "eval").exists(),
    reason="eval/ is local/private-only (scrubbed 2026-07-11)",
)


@_requires_eval
@pytest.mark.parametrize("stack", _STACK_NAMES)
def test_stack_yaml_matches_eval_authoring_copy(stack: str) -> None:
    """The packaged stack YAML is byte-identical to repo-root eval/stacks/.

    Authors edit ``eval/stacks/<stack>.yaml``; the in-package copy at
    ``faultline/pipeline_v2/data/stacks/`` is the RUNTIME source of truth.
    This test fails if someone edits one without syncing the other.
    """
    authoring = (_EVAL_STACKS / f"{stack}.yaml").read_text(encoding="utf-8")
    # Read via the same loader path the runtime uses.
    load_data_text.cache_clear()
    packaged = load_data_text(f"stacks/{stack}.yaml")
    assert packaged == authoring, (
        f"DRIFT: faultline/pipeline_v2/data/stacks/{stack}.yaml differs "
        f"from eval/stacks/{stack}.yaml. Re-sync the in-package copy."
    )


@_requires_eval
def test_dependency_anchors_matches_eval_authoring_copy() -> None:
    authoring = _EVAL_DEP_ANCHORS.read_text(encoding="utf-8")
    load_data_text.cache_clear()
    packaged = load_data_text("dependency-anchors.yaml")
    assert packaged == authoring, (
        "DRIFT: faultline/pipeline_v2/data/dependency-anchors.yaml differs "
        "from eval/dependency-anchors.yaml. Re-sync the in-package copy."
    )


# ── Parity guards: externalized extractor tables == historical values ──────
#
# These pin the YAML-loaded structures to the exact values that used to
# be hardcoded in Python, so the externalization stays byte-identical
# in behavior. If you intentionally change a table, update these pins.


def test_filesystem_routing_yaml_matches_historical_tables() -> None:
    """filesystem-routing.yaml reproduces route.py's historical tables."""
    from faultline.pipeline_v2.extractors import route

    stack_routing, markers = route._load_routing_tables()

    # Exact historical key set (+ react-router, the RRv7 framework-mode
    # successor to remix, added for nested-monorepo route roots).
    assert set(stack_routing) == {
        "next-app-router", "next-pages", "remix", "react-router",
        "astro", "sveltekit", "nuxt", "tanstack-router", "vite",
    }
    # RRv7 framework mode keeps the remix convention.
    assert stack_routing["react-router"] == stack_routing["remix"]
    # Pin exact entries (order matters for root/suffix matching).
    assert stack_routing["next-app-router"] == (
        ("app/", "src/app/"),
        ("/page.tsx", "/page.jsx", "/page.ts", "/page.js",
         "/route.ts", "/route.js"),
    )
    assert stack_routing["sveltekit"] == (
        ("src/routes/",),
        ("+page.svelte", "+server.ts", "+server.js", "+page.ts"),
    )
    assert stack_routing["nuxt"] == (("pages/", "src/pages/"), (".vue",))
    assert stack_routing["astro"] == (
        ("src/pages/", "pages/"),
        (".astro", ".tsx", ".ts", ".js"),
    )
    # Marker filenames in historical order.
    assert markers == ("urls.py", "router.py", "routers.py", "routes.py")


def test_stage1_dep_anchor_tables_match_historical_values() -> None:
    """stage1_anchors section reproduces package.py's historical tuples."""
    from faultline.pipeline_v2.extractors.package import (
        _JS_DEP_ANCHORS,
        _PY_DEP_ANCHORS,
    )

    # Exact historical Python table — order matters: _match_anchors
    # stops at the first matching token per dep.
    assert _PY_DEP_ANCHORS == (
        ("stripe", "billing"),
        ("django-allauth", "auth"),
        ("authlib", "auth"),
        ("python-jose", "auth"),
        ("celery", "background-jobs"),
        ("rq", "background-jobs"),
        ("dramatiq", "background-jobs"),
        ("openai", "ai"),
        ("anthropic", "ai"),
        ("langchain", "ai"),
        ("boto3", "file-uploads"),
        ("sendgrid", "email"),
    )

    assert len(_JS_DEP_ANCHORS) == 45
    # Pin head + a few order-sensitive entries.
    assert _JS_DEP_ANCHORS[0] == ("stripe", "billing")
    assert _JS_DEP_ANCHORS[1] == ("@stripe", "billing")
    # ``bullmq`` must come BEFORE ``bull`` (prefix-match first-wins).
    assert _JS_DEP_ANCHORS.index(("bullmq", "background-jobs")) < (
        _JS_DEP_ANCHORS.index(("bull", "background-jobs"))
    )
    # ``@clerk`` before ``clerk``; ``@uploadthing`` before ``uploadthing``.
    assert _JS_DEP_ANCHORS.index(("@clerk", "auth")) < (
        _JS_DEP_ANCHORS.index(("clerk", "auth"))
    )
    assert _JS_DEP_ANCHORS.index(("@uploadthing", "file-uploads")) < (
        _JS_DEP_ANCHORS.index(("uploadthing", "file-uploads"))
    )
    # Tail of the table — i18n block closes the historical tuple.
    assert _JS_DEP_ANCHORS[-4:] == (
        ("next-i18next", "i18n"),
        ("i18next", "i18n"),
        ("react-i18next", "i18n"),
        ("@lingui", "i18n"),
    )


def test_stage_6_5_loader_tolerates_stage1_anchors_section() -> None:
    """The Stage 6.5 clusterer must skip the stage1_anchors section.

    Its loader iterates ALL top-level keys as categories; entries
    without a ``product_label`` string are skipped, so the new section
    must never surface as a product-label cluster.
    """
    from faultline.pipeline_v2 import stage_6_5_product_clusterer as s65

    # Reset the module-level caches so this test sees a fresh parse.
    s65._DEP_ANCHORS_CACHE = None
    s65._DEP_ALIASES_CACHE = None
    try:
        anchors = s65._load_dep_anchors()
        labels = {label for _deps, label in anchors}
        assert "stage1_anchors" not in labels
        assert all("stage1" not in label.lower() for label in labels)
        # Sanity: real categories still load.
        assert "Billing" in labels
    finally:
        s65._DEP_ANCHORS_CACHE = None
        s65._DEP_ALIASES_CACHE = None


def test_mvc_controller_patterns_match_historical_table() -> None:
    """mvc-controllers.yaml reproduces mvc.py's historical tuple."""
    from faultline.pipeline_v2.extractors import mvc

    # Exact historical table — order matters: the FIRST suffix that
    # matches a basename wins in _controller_slug_from.
    assert mvc._load_controller_patterns() == (
        ("_controller.rb", "_controller.rb"),    # Rails
        ("Controller.php", "Controller.php"),    # Laravel
        ("_controller.ex", "_controller.ex"),    # Phoenix
        ("Controller.cs", "Controller.cs"),      # ASP.NET
        ("Controller.java", "Controller.java"),  # Spring
        ("Controller.kt", "Controller.kt"),      # Spring (Kotlin)
    )
    # Behavior spot-checks against historical outputs.
    assert mvc._controller_slug_from("UsersController.php") == "users"
    assert mvc._controller_slug_from("orders_controller.rb") == "orders"
    assert mvc._controller_slug_from("not_a_controller.py") is None


def test_schema_domain_tables_match_historical_values() -> None:
    """schema-domains.yaml reproduces schema.py's historical constants."""
    from faultline.pipeline_v2.extractors import schema

    t = schema._load_tables()

    # Exact historical regex strings (flags applied in code).
    assert t.prisma_model.pattern == (
        r"^\s*model\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{"
    )
    assert t.prisma_enum.pattern == (
        r"^\s*enum\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{"
    )
    assert t.drizzle_table.pattern == (
        r"export\s+const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
        r"(?:pgTable|mysqlTable|sqliteTable)\s*\("
    )
    assert t.rails_create_table.pattern == (
        "create_table\\s+[\"']([^\"']+)[\"']"
    )
    assert t.django_model_class.pattern == (
        r"^class\s+([A-Za-z_][A-Za-z0-9_]*)"
        r"\s*\(\s*[^)]*models\.Model[^)]*\)\s*:"
    )

    # Exact historical filename markers (order preserved).
    assert t.prisma_suffixes == ("schema.prisma",)
    assert t.drizzle_hints == (
        "/schema.ts", "/schema.js", "/db/schema.ts", "/db/schema.js",
    )
    assert t.drizzle_dir_segment == "/schema/"
    assert t.drizzle_dir_suffixes == (".ts", ".js")
    assert t.rails_suffixes == ("db/schema.rb",)
    assert t.django_suffixes == ("models.py",)

    # Behavior spot-checks against historical match semantics.
    assert schema._names_from_prisma("model User {\n}", t) == ["User"]
    assert schema._names_from_rails(
        'create_table "documents" do', t,
    ) == ["documents"]


def test_config_manifest_tables_match_historical_values() -> None:
    """config-manifests.yaml reproduces config.py's historical markers."""
    from faultline.pipeline_v2.extractors import config as config_mod

    t = config_mod._load_tables()

    assert t.confidence == 0.85
    assert t.tauri_filenames == ("tauri.conf.json",)
    assert t.tauri_parent_keys == ("app", "tauri")       # 2.x before 1.x
    assert t.tauri_window_label_keys == ("label", "title")
    assert t.expo_filenames == ("app.json", "app.config.json")
    assert t.expo_detect_keys == ("expo", "plugins", "scheme")
    assert t.expo_plugin_prefix_trim == "expo-"
    assert t.vscode_filenames == ("package.json",)
    assert t.vscode_engines_key == "vscode"
    assert t.chrome_filenames == ("manifest.json",)
    assert t.chrome_manifest_version == 3
    assert t.raycast_filenames == ("package.json",)
    assert t.raycast_required_keys == ("author", "commands")

    # Behavior spot-checks against historical outputs.
    assert config_mod._emit_expo_anchors(
        {"expo": {"plugins": ["expo-notifications"]}}, "app.json", t,
    ) == [("notifications", "app.json", "expo plugin 'expo-notifications'")]
    assert config_mod._emit_chrome_mv3_anchors(
        {"manifest_version": 2, "permissions": ["tabs"]},
        "manifest.json", t,
    ) == []
