"""Tests for the Stage 6.7d residual-confirmation guard.

Call 2 EXPLICITLY assigning a dev feature to "Shared Platform" was trusted
blindly (Soc0 2026-07-05: 60/155 devs residual, incl. `edr` while an
"EDR Integrations" capability existed in the same response). The guard:
  tier 1 — STRONG token-subset match re-routes to a capability;
  tier 2 — feature-dir promotion mints an own capability for author-declared
           React feature-folder domains (features|modules/<domain>);
genuine platform containers (workspace anchors, structure-leak slugs) and
unmatchable devs stay residual. Kill-switch env restores old behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _RESIDUAL_CAP,
    _build_product_features,
    _feature_dir_capability,
    _strong_capability_match,
)

_KILL = "FAULTLINE_STAGE_6_7D_RESIDUAL_GUARD"


def _dev(name: str, paths: list[str], description: str | None = None) -> Feature:
    return Feature(
        name=name, display_name=name, description=description,
        paths=list(paths),
        member_files=[MemberFile(path=p, role="anchor", confidence=1.0,
                                 primary=True) for p in paths],
        authors=["a"], total_commits=3, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=90.0,
        layer="developer",
    )


def _specs(*names: str) -> list[dict]:
    return [{"name": n, "description": f"{n} capability"} for n in names]


def _cap_of(dev_to_product: dict, dev_name: str) -> str:
    return dev_to_product[dev_name][0]


# ── tier 1: strong token-subset match ───────────────────────────────────


def test_explicit_residual_rescued_by_strong_match():
    """`detections` → "Security Findings & Detections Feed" even though
    Call 2 explicitly said Shared Platform."""
    dev = _dev("detections", ["fe/src/features/detections/a.tsx"])
    pfs, d2p, _, tele = _build_product_features(
        _specs("Security Findings & Detections Feed"),
        {"detections": _RESIDUAL_CAP},
        [dev],
    )
    assert _cap_of(d2p, "detections") == "security-findings-&-detections-feed"
    assert tele["devs_residual_rescued_strong"] == 1
    assert tele["devs_residual"] == 0


def test_weak_single_token_does_not_rescue():
    """`network-security` shares only 'security' with "AI Security Chat
    Assistant" — the 1-token overlap must NOT re-route (the misroute class
    the old omit-rescue suffers from). No feature-dir footprint here → the
    dev honestly stays residual."""
    dev = _dev("network-security", ["fe/src/net/a.tsx", "fe/src/net/b.tsx"])
    _, d2p, _, tele = _build_product_features(
        _specs("AI Security Chat Assistant"),
        {"network-security": _RESIDUAL_CAP},
        [dev],
    )
    assert _cap_of(d2p, "network-security") == "shared-platform"
    assert tele["devs_residual_rescued_strong"] == 0
    assert tele["devs_residual"] == 1


def test_vendor_tokens_stripped_for_match():
    """`edr-crowdstrike` (a per-connector split child) matches the family
    capability "EDR Integrations" — the vendor token names the instance,
    the capability names the family."""
    dev = _dev("edr-crowdstrike", ["backend/services/edr/crowdstrike.py"])
    _, d2p, _, tele = _build_product_features(
        _specs("EDR Integrations"),
        {"edr-crowdstrike": _RESIDUAL_CAP},
        [dev],
    )
    assert _cap_of(d2p, "edr-crowdstrike") == "edr-integrations"
    assert tele["devs_residual_rescued_strong"] == 1


def test_pure_vendor_name_matches_on_vendor_token():
    """A dev named ONLY by a vendor ('teams') still matches the capability
    carrying that vendor token."""
    dev = _dev("teams", ["fe/src/features/teams/a.tsx"])
    _, d2p, _, _ = _build_product_features(
        _specs("Microsoft Teams Integration"),
        {"teams": _RESIDUAL_CAP},
        [dev],
    )
    assert _cap_of(d2p, "teams") == "microsoft-teams-integration"


# ── tier 2: feature-dir promotion ───────────────────────────────────────


def test_feature_dir_promotion_mints_own_capability():
    """`anomalies` fully under features/anomalies/ with no matching
    capability → its own "Anomalies" product feature."""
    dev = _dev("anomalies", [
        "frontend/src/features/anomalies/list.tsx",
        "frontend/src/features/anomalies/detail.tsx",
        "frontend/src/features/anomalies/api.ts",
    ])
    pfs, d2p, _, tele = _build_product_features(
        _specs("Security Cases Management"),
        {"anomalies": _RESIDUAL_CAP},
        [dev],
    )
    assert _cap_of(d2p, "anomalies") == "anomalies"
    assert tele["devs_residual_promoted"] == 1
    assert any(p.display_name == "Anomalies" for p in pfs)


def test_modules_container_promotes_too():
    dev = _dev("network-security", [
        "frontend/src/modules/network-security/a.tsx",
        "frontend/src/modules/network-security/b.tsx",
    ])
    _, d2p, _, tele = _build_product_features(
        _specs("AI Security Chat Assistant"),
        {"network-security": _RESIDUAL_CAP},
        [dev],
    )
    assert _cap_of(d2p, "network-security") == "network-security"
    assert tele["devs_residual_promoted"] == 1


def test_promotion_requires_domain_to_name_the_dev():
    """`dialogs` living under features/integrations/ (dir ≠ name) must NOT
    promote — the domain names a DIFFERENT feature."""
    dev = _dev("dialogs", [
        "frontend/src/features/integrations/dialogs/a.tsx",
        "frontend/src/features/integrations/dialogs/b.tsx",
    ])
    _, d2p, _, tele = _build_product_features(
        _specs("Security Integrations Hub"),
        {"dialogs": _RESIDUAL_CAP},
        [dev],
    )
    assert _cap_of(d2p, "dialogs") == "shared-platform"
    assert tele["devs_residual_promoted"] == 0


def test_promotion_requires_majority_footprint():
    """A scattered dev (minority under the matching feature dir) stays."""
    dev = _dev("home", [
        "frontend/src/features/home/a.tsx",
        "frontend/src/pages/HomePage.tsx",
        "frontend/src/components/chat/x.tsx",
    ])
    assert _feature_dir_capability(dev) is None


def test_promotion_skips_generic_domains():
    dev = _dev("hooks", [
        "frontend/src/features/hooks/a.tsx",
        "frontend/src/features/hooks/b.tsx",
    ])
    # 'hooks' is a structure-leak slug → guard returns residual well before
    # promotion, but the promotion helper itself must also refuse.
    assert _feature_dir_capability(dev) is None


# ── genuine platform stays residual ─────────────────────────────────────


def test_workspace_anchor_stays_residual():
    anchor = _dev(
        "backend", ["backend/app.py", "backend/services/edr/base.py"],
        description="workspace anchor 'backend' from monorepo package 'backend/'",
    )
    _, d2p, _, tele = _build_product_features(
        _specs("EDR Integrations"),
        {"backend": _RESIDUAL_CAP},
        [anchor],
    )
    assert _cap_of(d2p, "backend") == "shared-platform"
    assert tele["devs_residual"] == 1
    assert tele["devs_residual_rescued_strong"] == 0


def test_structure_leak_slug_stays_residual():
    dev = _dev("api", ["backend/api/routes.py"])
    _, d2p, _, tele = _build_product_features(
        _specs("API Key Management"),
        {"api": _RESIDUAL_CAP},
        [dev],
    )
    assert _cap_of(d2p, "api") == "shared-platform"


# ── kill-switch + unchanged neighbour behaviour ─────────────────────────


def test_kill_switch_restores_blind_trust(monkeypatch):
    monkeypatch.setenv(_KILL, "0")
    dev = _dev("detections", ["fe/src/features/detections/a.tsx"])
    _, d2p, _, tele = _build_product_features(
        _specs("Security Findings & Detections Feed"),
        {"detections": _RESIDUAL_CAP},
        [dev],
    )
    assert _cap_of(d2p, "detections") == "shared-platform"
    assert tele["devs_residual_rescued_strong"] == 0
    assert tele["devs_residual_promoted"] == 0


def test_omitted_dev_weak_rescue_unchanged():
    """The pre-existing omit path (weak 1-token rescue) is untouched."""
    dev = _dev("account-billing", ["src/billing/a.ts"])
    _, d2p, _, tele = _build_product_features(
        _specs("Billing"),
        {},  # omitted from the map entirely
        [dev],
    )
    assert _cap_of(d2p, "account-billing") == "billing"
    assert tele["devs_token_rescued"] == 1


def test_explicit_non_residual_assignment_untouched():
    """The guard only inspects explicit RESIDUAL assignments."""
    dev = _dev("detections", ["fe/a.tsx"])
    _, d2p, _, tele = _build_product_features(
        _specs("Security Findings & Detections Feed", "Threat Hunt Management"),
        {"detections": "Threat Hunt Management"},
        [dev],
    )
    assert _cap_of(d2p, "detections") == "threat-hunt-management"
    assert tele["devs_residual_rescued_strong"] == 0


def test_strong_match_prefers_more_specific_capability():
    caps = {
        "Billing": {"billing"},
        "Billing & Invoicing Platform": {"billing", "invoicing", "platform"},
    }
    dev = _dev("billing", ["src/billing/a.ts"])
    assert _strong_capability_match(dev, caps) == "Billing"


# ── tier 3: route-surface promotion (resettle, 2026-07-05) ──────────────


def _flow(name: str, uuid: str):
    from faultline.models.types import Flow
    return Flow(
        name=name, uuid=uuid, paths=[f"app/{name}.ts"], authors=["a"],
        total_commits=2, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=95.0,
    )


def _flowful(name: str, paths: list[str], flow_names: list[str]) -> Feature:
    dev = _dev(name, paths)
    dev.flows = [_flow(fn, f"fx-{name}-{i}") for i, fn in enumerate(flow_names)]
    return dev


def test_route_file_owner_promoted_to_own_capability():
    """`api-widget-library` owns a router file + real flows but matches no
    capability → tier 3 mints "Api Widget Library" instead of aggregating
    a user-facing surface into the shared bucket (validator I9)."""
    dev = _flowful(
        "api-widget-library",
        ["backend/routers/widget_library.py", "frontend/src/pages/WidgetLibraryPage.tsx"],
        ["view-widget-details-flow", "edit-widget-flow"],
    )
    routes = [{"pattern": "/api/widget-library", "method": "GET",
               "file": "backend/routers/widget_library.py"}]
    pfs, d2p, _, tele = _build_product_features(
        _specs("Security Cases Management"),
        {"api-widget-library": _RESIDUAL_CAP},
        [dev], routes,
    )
    assert _cap_of(d2p, "api-widget-library") == "api-widget-library"
    assert tele["devs_residual_promoted"] == 1
    assert "Api Widget Library" in tele["promoted_cap_names"]
    promoted = next(p for p in pfs if p.display_name == "Api Widget Library")
    # Description is grounded in the dev's own flows (the promotion evidence).
    assert "view-widget-details-flow" in (promoted.description or "")
    assert "resettled" in (promoted.description or "")


def test_route_uuid_owner_promoted():
    """Route ownership via routes_index feature_uuid attribution (no route
    file among owned paths needed — e.g. after de-sink path moves)."""
    dev = _flowful("home-page", ["frontend/src/pages/HomePage.tsx"],
                   ["view-home-page-flow"])
    dev.uuid = "u-home"
    routes = [{"pattern": "/HomePage", "method": "PAGE",
               "feature_uuid": "u-home", "file": "frontend/src/other.tsx"}]
    _, d2p, _, tele = _build_product_features(
        _specs("Security Cases Management"),
        {"home-page": _RESIDUAL_CAP},
        [dev], routes,
    )
    assert _cap_of(d2p, "home-page") == "home-page"
    assert tele["devs_residual_promoted"] == 1


def test_flowless_route_owner_stays_residual():
    """Routes without flows = no user-visible surface evidence — stays."""
    dev = _dev("api-diag", ["backend/routers/diag.py"])
    routes = [{"pattern": "/api/_diag", "method": "GET",
               "file": "backend/routers/diag.py"}]
    _, d2p, _, tele = _build_product_features(
        _specs("Security Cases Management"),
        {"api-diag": _RESIDUAL_CAP},
        [dev], routes,
    )
    assert _cap_of(d2p, "api-diag") == "shared-platform"
    assert tele["devs_residual_promoted"] == 0


def test_flowful_dev_without_routes_stays_residual():
    """Flows without a route surface (tier 2 already covers feature dirs) —
    tier 3 must NOT fire on route-less internals."""
    dev = _flowful("network-mock", ["frontend/src/mocks/net.ts"],
                   ["mock-net-flow"])
    _, d2p, _, tele = _build_product_features(
        _specs("Security Cases Management"),
        {"network-mock": _RESIDUAL_CAP},
        [dev], [],
    )
    assert _cap_of(d2p, "network-mock") == "shared-platform"
    assert tele["devs_residual_promoted"] == 0


def test_main_entry_module_stays_residual_despite_routes_and_flows():
    """`main` (backend/main.py app entry) owns diag/admin routes + flows on
    Soc0 yet is an infra anchor class — the structure-leak exemption must
    hold it in the residual (never a minted "Main" capability)."""
    dev = _flowful("main", ["backend/main.py"], ["run-migration-flow"])
    routes = [{"pattern": "/api/_admin/migrate", "method": "POST",
               "file": "backend/main.py"}]
    _, d2p, _, tele = _build_product_features(
        _specs("Security Cases Management"),
        {"main": _RESIDUAL_CAP},
        [dev], routes,
    )
    assert _cap_of(d2p, "main") == "shared-platform"
    assert tele["devs_residual_promoted"] == 0


def test_tier3_kill_switch(monkeypatch):
    monkeypatch.setenv(_KILL, "0")
    dev = _flowful("api-trial-status", ["backend/routers/trial.py"],
                   ["check-trial-status-flow"])
    routes = [{"pattern": "/api/trial/status", "method": "GET",
               "file": "backend/routers/trial.py"}]
    _, d2p, _, tele = _build_product_features(
        _specs("Security Cases Management"),
        {"api-trial-status": _RESIDUAL_CAP},
        [dev], routes,
    )
    assert _cap_of(d2p, "api-trial-status") == "shared-platform"
    assert tele["devs_residual_promoted"] == 0
