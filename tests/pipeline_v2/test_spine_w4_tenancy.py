"""W4 — URL tenancy-transparency (the tracecat Workspaces class).

A pure scope word (workspace/org/team/tenant …) immediately followed by
a dynamic param is tenant ADDRESSING when deeper meaningful segments
exist: ``/workspaces/{ws_id}/tables`` keys ``tables`` so per-domain
anchors mint instead of one Workspaces blob (I23 gate cell 0.88-0.95
share / 33-35K out-LOC on tracecat). Literal index/detail routes keep
the scope word — workspace MANAGEMENT is a real surface.

Dialect matrix mirrors ``_DYNAMIC_RE``: Next ``[id]``, FastAPI ``{id}``,
Express/Rails ``:id``, Remix/TanStack ``$id``, Django ``<int:id>``.
"""

from __future__ import annotations

import re

import pytest

from faultline.pipeline_v2.spine_anchors import (
    _build_route_anchors,
    _pattern_key_chain,
    load_spine_vocab,
)

_VOCAB = load_spine_vocab()
_VERSION_RE = re.compile(_VOCAB.get("version_segment_pattern") or r"^v\d+$")


def _chain(pattern: str) -> list[str]:
    return _pattern_key_chain(pattern, _VOCAB, _VERSION_RE)


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        # FastAPI (tracecat's exact class)
        ("/workspaces/{workspace_id}/tables", ["tables"]),
        ("/workspaces/{workspace_id}/workflows/{workflow_id}", ["workflows"]),
        ("/workspaces/{workspace_id}/webhook", ["webhook"]),
        ("/workspaces/{workspace_id}/expressions", ["expressions"]),
        ("/workspaces/{workspace_id}/chat", ["chat"]),
        # Next App Router
        ("/workspaces/[workspaceId]/settings", ["settings"]),
        # Express / Rails / Vue
        ("/orgs/:orgId/billing", ["billing"]),
        ("/organizations/:id/members", ["members"]),
        # Remix / TanStack
        ("/teams/$teamId/settings/members", ["settings", "members"]),
        # Django
        ("/tenants/<int:tenant_id>/reports", ["reports"]),
        # Nested scopes collapse to the capability
        ("/orgs/{org_id}/workspaces/{ws_id}/tables", ["tables"]),
        # Scope + transparent api segment
        ("/api/workspaces/{ws_id}/secrets", ["secrets"]),
    ],
)
def test_scope_segment_transparent_before_param(pattern, expected) -> None:
    assert _chain(pattern) == expected


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        # Literal index — workspace MANAGEMENT is a real surface.
        ("/workspaces", ["workspaces"]),
        # Detail page without deeper segments — still the scope surface.
        ("/workspaces/{workspace_id}", ["workspaces"]),
        ("/orgs/[orgId]", ["orgs"]),
        # Scope word NOT followed by a param is a plain segment.
        ("/workspaces/general", ["workspaces", "general"]),
        # Non-scope resources with params keep their normal chain.
        ("/projects/{id}/settings", ["projects", "settings"]),
    ],
)
def test_scope_segment_kept_when_it_is_the_surface(pattern, expected) -> None:
    assert _chain(pattern) == expected


def test_central_router_anchors_mint_per_domain() -> None:
    """tracecat shape: workspace-scoped FastAPI routers under one URL
    top-segment must anchor per DOMAIN file, not one `route:workspace`
    file-anchor absorbing every router."""
    routes = [
        {"pattern": "/workspaces/{workspace_id}/tables",
         "method": "GET", "file": "tracecat/tables/router.py"},
        {"pattern": "/workspaces/{workspace_id}/tables/{table_id}",
         "method": "PATCH", "file": "tracecat/tables/router.py"},
        {"pattern": "/workspaces/{workspace_id}/workflows",
         "method": "GET", "file": "tracecat/workflow/router.py"},
        {"pattern": "/workspaces", "method": "GET",
         "file": "tracecat/workspaces/router.py"},
        {"pattern": "/workspaces/{workspace_id}", "method": "PATCH",
         "file": "tracecat/workspaces/router.py"},
    ]
    anchors = _build_route_anchors(routes, _VOCAB)
    by_key = {a.key: a for a in anchors}
    assert "table" in by_key
    assert by_key["table"].files == frozenset({"tracecat/tables/router.py"})
    assert "workflow" in by_key
    # The Workspaces anchor still exists — but holds ONLY the actual
    # workspace-management router, never the domain routers.
    ws = by_key["workspace"]
    assert ws.files == frozenset({"tracecat/workspaces/router.py"})


def test_filesystem_router_dir_located_past_scope_dirs() -> None:
    """Frontend variant: the key dir is located THROUGH the scope +
    param dirs (`app/workspaces/[id]/tables/page.tsx` → tables dir)."""
    routes = [
        {"pattern": "/workspaces/[workspaceId]/tables", "method": "PAGE",
         "file": "frontend/src/app/workspaces/[workspaceId]/tables/page.tsx"},
    ]
    anchors = _build_route_anchors(routes, _VOCAB)
    by_key = {a.key: a for a in anchors}
    assert by_key["table"].prefixes == (
        "frontend/src/app/workspaces/[workspaceId]/tables",
    )
