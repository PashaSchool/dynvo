"""B30 — deterministic verb+resource flow naming (``flow_name_v2``).

Covers the mechanism (verb from method / handler kind; resource from
meaningful path segments), the anti-cases (already-verb-named flows
stay; symbol-named flows unaffected; honest fallback keeps a dir-only
name), the collision ladder (feature token BEFORE ordinal), the name-
mirror fields, and the kill-switch flag parsing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Flow, FlowEntryPoint
from faultline.pipeline_v2.flow_name_v2 import (
    FLOW_NAME_V2_ENV,
    apply_flow_name_v2,
    flow_name_v2_enabled,
)


def _flow(
    name: str,
    *,
    entry_file: str = "",
    symbol: str | None = None,
    description: str = "",
    primary_feature: str = "feat",
    uuid: str | None = None,
) -> Flow:
    now = datetime.now(timezone.utc)
    return Flow(
        name=name,
        description=description or None,
        entry_point_file=entry_file or None,
        entry_point=(
            FlowEntryPoint(path=entry_file, symbol=symbol, line=1)
            if entry_file else None
        ),
        paths=[entry_file] if entry_file else [],
        authors=["a"],
        total_commits=5,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=now,
        health_score=90.0,
        uuid=uuid if uuid is not None else f"uuid-{name}",
        id=f"{primary_feature}::{name}",
        primary_feature=primary_feature,
        display_name=name,
        short_label=name[:-5] if name.endswith("-flow") else name,
    )


def _apply(flows, routes_index=None, repo_path=None):
    return apply_flow_name_v2(flows, routes_index or [], repo_path)


# ── flag parsing ────────────────────────────────────────────────────────


def test_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLOW_NAME_V2_ENV, raising=False)
    assert flow_name_v2_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "False"])
def test_flag_kill_switch(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv(FLOW_NAME_V2_ENV, val)
    assert flow_name_v2_enabled() is False


# ── route-derived renames ───────────────────────────────────────────────


def test_post_route_echo_renames_to_create() -> None:
    fl = _flow(
        "post-api-auth-verify-code-flow",
        entry_file="app/api/auth/verify-code/route.ts",
        symbol="POST",
        description="POST /api/auth/verify-code",
    )
    tele = _apply([fl])
    assert fl.name == "create-auth-verify-code-flow"
    assert tele["renamed_route"] == 1


def test_page_route_echo_renames_to_view() -> None:
    fl = _flow(
        "branding-flow",
        entry_file="pages/branding.tsx",
        symbol="Branding",
        description="/branding",
    )
    _apply([fl])
    assert fl.name == "view-branding-flow"


def test_get_collection_reads_as_browse() -> None:
    fl = _flow(
        "get-api-teams-flow",
        entry_file="app/api/teams/route.ts",
        symbol="GET",
        description="GET /api/teams",
    )
    _apply([fl])
    assert fl.name == "browse-teams-flow"


def test_get_instance_terminal_param_becomes_by() -> None:
    fl = _flow(
        "get-api-teams-id-flow",
        entry_file="app/api/teams/[id]/route.ts",
        symbol="GET",
        description="GET /api/teams/:id",
    )
    _apply([fl])
    assert fl.name == "view-team-by-id-flow"


def test_interior_param_drops_and_singularizes_parent() -> None:
    fl = _flow(
        "get-api-teams-teamid-tags-flow",
        entry_file="app/api/teams/[teamId]/tags/route.ts",
        symbol="GET",
        description="GET /api/teams/:teamId/tags",
    )
    _apply([fl])
    assert fl.name == "browse-team-tags-flow"


def test_delete_and_update_verbs() -> None:
    fl_del = _flow(
        "delete-api-keys-id-flow",
        entry_file="app/api/keys/[id]/route.ts",
        symbol="DELETE",
        description="DELETE /api/keys/:id",
    )
    fl_patch = _flow(
        "patch-api-keys-id-flow",
        entry_file="app/api/keys/[id]/route.ts",
        symbol="PATCH",
        description="PATCH /api/keys/:id",
    )
    _apply([fl_del, fl_patch])
    assert fl_del.name == "delete-key-by-id-flow"
    assert fl_patch.name == "update-key-by-id-flow"


def test_version_and_boilerplate_segments_drop() -> None:
    fl = _flow(
        "post-api-v2-webhooks-flow",
        entry_file="src/routers/webhooks.py",
        description="POST /api/v2/webhooks",
    )
    _apply([fl])
    assert fl.name == "create-webhooks-flow"


def test_catchall_param_drops() -> None:
    fl = _flow(
        "api-auth-nextauth-flow",
        entry_file="pages/api/auth/[...nextauth].ts",
        symbol="handler",
        description="/api/auth/:nextauth",
    )
    # No repo on disk → method sniff degrades → honest "manage".
    _apply([fl])
    assert fl.name == "manage-auth-flow"


def test_params_only_path_uses_entry_symbol() -> None:
    # The route has no static resource — the author's component name is
    # the honest capability label.
    fl = _flow(
        "workspaceslug-flow",
        entry_file="pages/[workspaceSlug].tsx",
        symbol="WorkspaceBoard",
        description="/:workspaceSlug",
    )
    _apply([fl])
    assert fl.name == "workspace-board-flow"


def test_params_only_path_keeps_old_name_without_symbol() -> None:
    fl = _flow(
        "workspaceslug-flow",
        entry_file="pages/[workspaceSlug].tsx",
        symbol="default",  # noise symbol — nothing honest to use
        description="/:workspaceSlug",
    )
    tele = _apply([fl])
    assert fl.name == "workspaceslug-flow"
    assert tele["kept_honest_fallback"] == 1


def test_flow_flow_root_route_uses_entry_symbol() -> None:
    # The Stage-3 empty-basis literal (root page "/") — the wave-14
    # absurd exhibit. The entry symbol rescues it.
    fl = _flow(
        "flow-flow",
        entry_file="client/src/app/page.tsx",
        symbol="Home",
        description="/",
    )
    _apply([fl])
    assert fl.name == "home-flow"


def test_flow_flow_root_route_without_symbol_kept() -> None:
    fl = _flow(
        "flow-flow",
        entry_file="apps/website/src/app/page.tsx",
        symbol="Page",  # noise symbol
        description="/",
    )
    tele = _apply([fl])
    assert fl.name == "flow-flow"
    assert tele["kept_honest_fallback"] == 1


def test_routes_index_provenance_without_description() -> None:
    fl = _flow(
        "api-account-passkeys-flow",
        entry_file="pages/api/account/passkeys.ts",
        symbol="handler",
    )
    routes = [{
        "pattern": "/api/account/passkeys",
        "method": "PAGE",
        "file": "pages/api/account/passkeys.ts",
    }]
    _apply([fl], routes_index=routes)
    assert fl.name == "manage-account-passkeys-flow"


def test_ordinal_suffixed_echo_still_eligible() -> None:
    fl = _flow(
        "api-account-passkeys-2-flow",
        entry_file="pages/api/account/passkeys.ts",
        symbol="handler",
        description="/api/account/passkeys",
    )
    _apply([fl])
    assert fl.name == "manage-account-passkeys-flow"


# ── method sniffing (handler kind) ──────────────────────────────────────


def test_sniff_single_method_maps_verb(tmp_path: Path) -> None:
    handler = tmp_path / "pages" / "api" / "upload.ts"
    handler.parent.mkdir(parents=True)
    handler.write_text(
        'export default async function handler(req, res) {\n'
        '  if (req.method !== "POST") return res.status(405).end();\n'
        '}\n',
    )
    fl = _flow(
        "api-upload-flow",
        entry_file="pages/api/upload.ts",
        symbol="handler",
        description="/api/upload",
    )
    _apply([fl], repo_path=tmp_path)
    assert fl.name == "create-upload-flow"


def test_sniff_multi_method_maps_manage(tmp_path: Path) -> None:
    handler = tmp_path / "pages" / "api" / "passkeys.ts"
    handler.parent.mkdir(parents=True)
    handler.write_text(
        'export default async function handler(req, res) {\n'
        '  if (req.method === "GET") { /* list */ }\n'
        '  if (req.method === "DELETE") { /* remove */ }\n'
        '}\n',
    )
    fl = _flow(
        "api-passkeys-flow",
        entry_file="pages/api/passkeys.ts",
        symbol="handler",
        description="/api/passkeys",
    )
    _apply([fl], repo_path=tmp_path)
    assert fl.name == "manage-passkeys-flow"


def test_verb_leaf_file_names_the_method() -> None:
    fl = _flow(
        "api-api-keys-id-patch-flow",
        entry_file="apps/api/v1/pages/api/api-keys/[id]/_patch.ts",
        symbol="patchHandler",
        description="/api/api-keys/:id/_patch",
    )
    routes = [{
        "pattern": "/api/api-keys/:id",
        "method": "PATCH",
        "file": "apps/api/v1/pages/api/api-keys/[id]/_patch.ts",
    }]
    _apply([fl], routes_index=routes)
    assert fl.name == "update-api-key-by-id-flow"


# ── anti-cases: semantic names are untouched ────────────────────────────


def test_llm_verb_name_stays() -> None:
    fl = _flow(
        "send-daily-dataroom-digest-flow",
        entry_file="app/api/cron/dataroom-digest/daily/route.ts",
        symbol="POST",
        description="POST /api/cron/dataroom-digest/daily",
    )
    tele = _apply([fl])
    assert fl.name == "send-daily-dataroom-digest-flow"
    assert tele["renamed_total"] == 0


def test_symbol_named_flow_unaffected() -> None:
    fl = _flow(
        "create-checkout-session-flow",
        entry_file="src/billing/checkout.ts",
        symbol="createCheckoutSession",
    )
    tele = _apply([fl])
    assert fl.name == "create-checkout-session-flow"
    assert tele["renamed_total"] == 0


def test_flow_without_entry_file_untouched() -> None:
    fl = _flow("api-orphan-flow")
    tele = _apply([fl])
    assert fl.name == "api-orphan-flow"
    assert tele["renamed_total"] == 0


def test_flow_without_uuid_untouched() -> None:
    fl = _flow(
        "api-account-flow",
        entry_file="pages/api/account/index.ts",
        symbol="handler",
        description="/api/account",
        uuid="",
    )
    _apply([fl])
    assert fl.name == "api-account-flow"


# ── file-echo (dir-token) class ─────────────────────────────────────────


def test_file_echo_renames_from_symbol() -> None:
    fl = _flow(
        "flow-flow",
        entry_file="apps/web/src/components/flow.tsx",
        symbol="FlowCanvas",
    )
    tele = _apply([fl])
    assert fl.name == "flow-canvas-flow"
    assert tele["renamed_symbol"] == 1


def test_file_echo_honest_fallback_keeps_name() -> None:
    # Only a dir/file is known (noise symbol) — keep, do not invent.
    fl = _flow(
        "partner-utils-flow",
        entry_file="apps/web/src/pages/api/partner/_utils.ts",
        symbol="default",
    )
    tele = _apply([fl])
    assert fl.name == "partner-utils-flow"
    assert tele["kept_honest_fallback"] == 1


# ── collision ladder: feature token BEFORE ordinal ──────────────────────


def test_collision_qualified_by_feature_token() -> None:
    fl_a = _flow(
        "api-webhook-flow",
        entry_file="src/notification-slack/api/webhook.ts",
        symbol="handler",
        description="POST /api/webhook",
        primary_feature="notification-slack",
        uuid="u-a",
    )
    fl_b = _flow(
        "api-webhook-2-flow",
        entry_file="src/notification-discord/api/webhook.ts",
        symbol="handler",
        description="POST /api/webhook",
        primary_feature="notification-discord",
        uuid="u-b",
    )
    tele = _apply([fl_a, fl_b])
    assert fl_a.name == "create-webhook-notification-slack-flow"
    assert fl_b.name == "create-webhook-notification-discord-flow"
    assert tele["collision_feature_qualified"] == 2
    assert tele["collision_ordinal"] == 0


def test_collision_same_feature_falls_to_ordinal() -> None:
    fl_a = _flow(
        "api-webhook-flow",
        entry_file="src/hooks/api/webhook.ts",
        symbol="handler",
        description="POST /api/webhook",
        primary_feature="hooks",
        uuid="u-a",
    )
    fl_b = _flow(
        "api-webhook-2-flow",
        entry_file="src/hooks/api/webhook2.ts",
        symbol="handler",
        description="POST /api/webhook",
        primary_feature="hooks",
        uuid="u-b",
    )
    _apply([fl_a, fl_b])
    names = {fl_a.name, fl_b.name}
    assert names == {
        "create-webhook-hooks-flow", "create-webhook-hooks-2-flow",
    }


def test_collision_with_untouched_flow_name() -> None:
    untouched = _flow(
        "browse-teams-flow",
        entry_file="src/teams/list.ts",
        symbol="listTeams",
        primary_feature="teams",
        uuid="u-keep",
    )
    echo = _flow(
        "get-api-teams-flow",
        entry_file="app/api/teams/route.ts",
        symbol="GET",
        description="GET /api/teams",
        primary_feature="teams-api",
        uuid="u-echo",
    )
    tele = _apply([untouched, echo])
    assert untouched.name == "browse-teams-flow"
    # The feature token ("teams-api") adds nothing new over the base
    # tokens, so the ladder honestly falls through to the ordinal tier.
    assert echo.name == "browse-teams-2-flow"
    assert tele["collision_ordinal"] == 1


# ── name-mirror fields + join keys ──────────────────────────────────────


def test_mirrors_updated_and_join_keys_untouched() -> None:
    fl = _flow(
        "api-account-passkeys-flow",
        entry_file="pages/api/account/passkeys.ts",
        symbol="handler",
        description="/api/account/passkeys",
        primary_feature="account",
    )
    old_id, old_uuid = fl.id, fl.uuid
    _apply([fl])
    assert fl.name == "manage-account-passkeys-flow"
    assert fl.display_name == "manage-account-passkeys-flow"
    assert fl.short_label == "manage-account-passkeys"
    assert fl.id == old_id
    assert fl.uuid == old_uuid


def test_deterministic_across_runs() -> None:
    def build():
        return [
            _flow(
                "api-webhook-flow",
                entry_file="src/a/api/webhook.ts",
                symbol="handler",
                description="POST /api/webhook",
                primary_feature="alpha",
                uuid="u-1",
            ),
            _flow(
                "api-webhook-2-flow",
                entry_file="src/b/api/webhook.ts",
                symbol="handler",
                description="POST /api/webhook",
                primary_feature="beta",
                uuid="u-2",
            ),
            _flow(
                "boards-flow",
                entry_file="pages/boards.tsx",
                symbol="Boards",
                description="/boards",
                primary_feature="boards",
                uuid="u-3",
            ),
        ]

    run1 = build()
    run2 = build()
    _apply(run1)
    _apply(run2)
    assert [f.name for f in run1] == [f.name for f in run2]
