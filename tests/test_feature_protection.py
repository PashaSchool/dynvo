"""Tests for the Sprint 8g/8h feature-protection aggregator."""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.aggregators.feature_protection import (
    _is_structural_path,
    _slug_matches,
    mark_protected,
)


def _feat(name, paths=None, protected=False):
    from faultline.models.types import Feature
    return Feature(
        name=name,
        paths=paths or [],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=99.0,
        flows=[],
        protected=protected,
    )


def _fm(features):
    from faultline.models.types import FeatureMap
    return FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=datetime.now(tz=timezone.utc),
        total_commits=0, date_range_days=365,
        features=features,
    )


# ── _slug_matches ────────────────────────────────────────────────────


def test_slug_matches_exact():
    assert _slug_matches("templates", "templates")


def test_slug_matches_kebab_underscore():
    assert _slug_matches("api_tokens", "api-tokens")


def test_slug_matches_path_tail():
    assert _slug_matches("ee/billing", "billing")


def test_slug_matches_plural_singular():
    assert _slug_matches("template", "templates")
    assert _slug_matches("templates", "template")


def test_slug_matches_negative():
    assert not _slug_matches("templates", "documents")


# ── named patterns (slug must match) ────────────────────────────────


def test_trpc_router_protects():
    assert _is_structural_path(
        "templates", "packages/trpc/server/templates-router/router.ts",
    ) == "trpc-router"


def test_trpc_namespace_file_protects():
    """Sprint 8h widening: any file inside *-router/ counts when feature
    name matches the prefix.
    """
    assert _is_structural_path(
        "templates", "packages/trpc/server/templates-router/find-templates.ts",
    ) == "trpc-namespace"


def test_workspace_package_protects():
    assert _is_structural_path(
        "auth", "packages/auth/server/index.ts",
    ) == "workspace-package"


def test_route_folder_protects_remix():
    assert _is_structural_path(
        "billing", "apps/remix/app/routes/billing.tsx",
    ) == "route-folder"


def test_route_folder_handles_remix_underscore_folders():
    """Remix flat-routes use _authenticated+/billing.tsx — capture
    should target the leaf segment, not the prefix folder.
    """
    assert _is_structural_path(
        "billing", "apps/remix/app/routes/_authenticated+/billing.tsx",
    ) == "route-folder"


def test_server_only_domain_protects():
    """Sprint 8h widening: packages/<workspace>/server-only/<slug>/
    is a strong domain anchor (documenso pattern).
    """
    assert _is_structural_path(
        "templates",
        "packages/lib/server-only/templates/find-templates.ts",
    ) == "server-only-domain"


def test_plugin_folder_protects():
    assert _is_structural_path(
        "discord",
        "Apprise/plugins/discord/main.py",
    ) == "plugin-folder"


def test_service_folder_protects():
    assert _is_structural_path(
        "billing",
        "src/services/billing/charge.ts",
    ) == "service-folder"


def test_route_group_protects_next():
    assert _is_structural_path(
        "dashboard",
        "apps/web/app/(dashboard)/page.tsx",
    ) == "route-group"


# ── shield patterns (any feature owning these is protected) ─────────


def test_api_contract_shields_any_feature():
    assert _is_structural_path(
        "anything", "packages/api/v1/contract.ts",
    ) == "api-contract"


def test_prisma_schema_shields():
    assert _is_structural_path(
        "schema", "packages/prisma/schema.prisma",
    ) == "schema-file"


def test_auth_strategy_shields():
    assert _is_structural_path(
        "auth-providers",
        "packages/auth/strategies/passkey.ts",
    ) == "auth-strategy"


def test_webhook_hub_shields():
    assert _is_structural_path(
        "integrations",
        "packages/lib/webhooks/router.ts",
    ) == "webhook-hub"


def test_python_subpackage_protects():
    """Sprint 9a — fastapi-style ``fastapi/security/__init__.py``."""
    assert _is_structural_path(
        "security", "fastapi/security/__init__.py",
    ) == "python-subpackage"


def test_python_module_protects():
    """Sprint 9a — single-file Python module ``axios/cancel.py``."""
    assert _is_structural_path(
        "cancel", "axios/cancel.py",
    ) == "python-module"


def test_lib_directory_protects():
    """Sprint 9a — axios-style ``lib/adapters/...``."""
    assert _is_structural_path(
        "adapters", "lib/adapters/http.js",
    ) == "lib-directory"


def test_test_file_protects_python_naming():
    """Sprint 9a — pytest convention ``tests/test_security.py``."""
    assert _is_structural_path(
        "security", "tests/test_security.py",
    ) == "test-file"


def test_test_file_protects_jest_naming():
    """Sprint 9a — Jest convention ``__tests__/cancel-token.test.ts``."""
    assert _is_structural_path(
        "cancel-token", "__tests__/cancel-token.test.ts",
    ) == "test-file"


def test_unrelated_path_returns_none():
    assert _is_structural_path("templates", "src/utils/random.ts") is None


# ── mark_protected ───────────────────────────────────────────────────


def test_mark_protected_flips_flag_and_records_reason():
    f = _feat("templates", paths=[
        "packages/trpc/server/templates-router/router.ts",
        "packages/lib/server-only/templates/find.ts",
    ])
    fm = _fm([f])
    new_fm, reasons = mark_protected(fm)
    assert reasons == {"templates": "trpc-router"}
    # Sprint 10a — input untouched, mutation lives on returned new_fm
    assert f.protected is False
    assert new_fm.features[0].protected is True
    assert new_fm.features[0].protection_reason == "trpc-router"


def test_mark_protected_idempotent():
    f = _feat("billing", paths=["packages/billing/index.ts"], protected=True)
    fm = _fm([f])
    _, reasons = mark_protected(fm)
    assert reasons == {}


def test_mark_protected_skips_unanchored_feature():
    f = _feat("noise", paths=["src/utils/x.ts"])
    fm = _fm([f])
    new_fm, reasons = mark_protected(fm)
    assert reasons == {}
    assert f.protected is False
    assert new_fm.features[0].protected is False
    assert new_fm.features[0].protection_reason is None


# ── Sprint 9c — Go patterns ──────────────────────────────────────────


def test_go_module_protects_top_level_file():
    """Sprint 9c — chi/gin top-level ``mux.go``."""
    assert _is_structural_path("mux", "mux.go") == "go-module"


def test_go_subpackage_protects():
    """Sprint 9c — ``middleware/logger/logger.go`` → logger feature."""
    assert _is_structural_path(
        "logger", "middleware/logger/logger.go",
    ) == "go-subpackage"


def test_go_test_file_protects():
    """Sprint 9c — Go convention ``mux_test.go``."""
    assert _is_structural_path("mux", "mux_test.go") == "go-test-file"


# ── Sprint 9 — additional Python / lib patterns extension ─────────────


def test_nested_plugin_via_plugins_dir_protects_under_plugin_folder():
    """``plugins/<slug>/`` matches the broader ``plugin-folder`` pattern
    first (registered before ``nested-plugin``) — so this case is
    covered by ``plugin-folder``. ``nested-plugin`` exists for the
    ``modules`` / ``features`` / ``integrations`` flavours that
    ``plugin-folder`` doesn't catch.
    """
    assert _is_structural_path(
        "captcha",
        "packages/better-auth/src/plugins/captcha/index.ts",
    ) == "plugin-folder"


def test_nested_plugin_via_features_dir_protects():
    assert _is_structural_path(
        "billing",
        "packages/core/src/features/billing/index.ts",
    ) == "nested-plugin"


def test_nested_plugin_via_modules_dir_protects():
    """``services``/``modules`` already covered by service-folder; the
    nested-plugin pattern picks up workspace-scoped ``modules/``.
    """
    assert _is_structural_path(
        "auth",
        "packages/core/src/modules/auth/index.ts",
    ) == "service-folder"


def test_route_folder_remix_with_double_underscore_prefix():
    """Remix flat-routes ``_authenticated+/<leaf>.tsx`` → leaf is feature."""
    assert _is_structural_path(
        "documents",
        "apps/remix/app/routes/_authenticated+/documents.tsx",
    ) == "route-folder"
