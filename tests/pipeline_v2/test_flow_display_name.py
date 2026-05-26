"""Phase 5 — deterministic ``Flow.display_name`` derivation tests.

Table-driven coverage of the three-tier priority (HTTP route > entry
symbol > fallback) plus the hard ADDITIVE invariant: the stable
``Flow.name`` id is never mutated by the derivation. No LLM, no network.
"""

from __future__ import annotations

import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import (
    Feature,
    Flow,
    FlowEntryPoint,
    FlowSummary,
    FlowSymbolAttribution,
)
from faultline.pipeline_v2.flow_expansion import expand_flows
from faultline.pipeline_v2.flow_expansion.flow_display_name import (
    _humanize_route,
    _humanize_symbol,
    derive_display_name,
)
from faultline.pipeline_v2.stage_0_intake import stage_0_intake


def _flow(
    name: str,
    *,
    entry_file: str | None = None,
    entry_symbol: str | None = None,
    summary_title: str | None = None,
    description: str | None = None,
) -> Flow:
    now = datetime.now(timezone.utc)
    fl = Flow(
        name=name,
        paths=[entry_file] if entry_file else [],
        authors=[],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=now,
        health_score=90.0,
        description=description,
    )
    if entry_file:
        fl.entry_point = FlowEntryPoint(
            path=entry_file, symbol=entry_symbol, line=1,
        )
    if summary_title:
        fl.summary = FlowSummary()
        # FlowSummary has no title field; emulate via description path,
        # which the fallback also reads. Title path is covered separately
        # by the kebab-name fallback case.
    return fl


# ── Tier 1: HTTP route (method + path) ──────────────────────────────
_ROUTE_CASES = [
    # (method, pattern, expected)
    ("POST", "/api/teams/:id/invite", "Create Team Invite"),
    ("GET", "/api/products", "List Products"),
    ("GET", "/api/products/:id", "View Product"),
    ("DELETE", "/api/sessions/:id", "Delete Session"),
    ("PATCH", "/api/users/:id/profile", "Update User Profile"),
    ("PAGE", "/dashboard/settings", "View Settings"),
]


@pytest.mark.parametrize(("method", "pattern", "expected"), _ROUTE_CASES)
def test_humanize_route(method: str, pattern: str, expected: str) -> None:
    assert _humanize_route(method, pattern) == expected


def test_route_tier_via_routes_index() -> None:
    fl = _flow("create-checkout-flow", entry_file="src/app/api/checkout/route.ts")
    routes = [{
        "pattern": "/api/checkout",
        "method": "POST",
        "file": "src/app/api/checkout/route.ts",
    }]
    assert derive_display_name(fl, routes) == "Create Checkout"


# ── Tier 2: entry_point symbol ──────────────────────────────────────
_SYMBOL_CASES = [
    ("createCheckoutSession", "Create Checkout Session"),
    ("handleSubmit", "Submit"),
    ("useTeamInvite", "Team Invite"),
    ("on_user_signup", "User Signup"),
    ("getApiKey", "Get API Key"),  # acronym stays upper
]


@pytest.mark.parametrize(("symbol", "expected"), _SYMBOL_CASES)
def test_humanize_symbol(symbol: str, expected: str) -> None:
    assert _humanize_symbol(symbol) == expected


def test_framework_noise_symbol_falls_through_to_name() -> None:
    # getServerSideProps carries no feature meaning → humanize the name.
    fl = _flow(
        "setup-alby-integration-flow",
        entry_file="src/pages/alby.tsx",
        entry_symbol="getServerSideProps",
    )
    assert derive_display_name(fl, routes=[]) == "Setup Alby Integration"


def test_symbol_tier_when_no_route() -> None:
    fl = _flow(
        "checkout-flow",
        entry_file="src/lib/checkout.ts",
        entry_symbol="createCheckoutSession",
    )
    # No matching route → falls through to symbol tier.
    assert derive_display_name(fl, routes=[]) == "Create Checkout Session"


# ── Tier 3: fallback (humanize kebab name) ──────────────────────────
def test_fallback_humanizes_kebab_name() -> None:
    fl = _flow("manage-billing-portal-flow")
    # No entry file / symbol / route → humanize name, drop trailing flow.
    assert derive_display_name(fl, routes=[]) == "Manage Billing Portal"


def test_fallback_humanizes_description() -> None:
    fl = _flow("x-flow", description="reset user password")
    assert derive_display_name(fl, routes=[]) == "Reset User Password"


# ── Hard invariant: stable id is never mutated ──────────────────────
def test_name_unchanged_by_derivation() -> None:
    fl = _flow(
        "create-checkout-flow",
        entry_file="src/lib/checkout.ts",
        entry_symbol="createCheckoutSession",
    )
    before = fl.name
    label = derive_display_name(fl, routes=[])
    assert fl.name == before == "create-checkout-flow"  # byte-identical
    assert label != fl.name  # display label is NOT the id
    assert "-" not in label  # no kebab in the human label


# ── Integration: end-to-end through expand_flows ────────────────────
def _multi_file_repo(repo: Path) -> None:
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "x.ts").write_text(textwrap.dedent("""
        import { helper } from './y';
        export function createCheckoutSession() {
          return helper();
        }
    """).lstrip("\n"))
    (repo / "src" / "y.ts").write_text(textwrap.dedent("""
        export function helper() {
          return 42;
        }
    """).lstrip("\n"))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)


def test_expand_flows_sets_display_name_and_preserves_name(tmp_path: Path) -> None:
    repo = tmp_path / "dn"
    repo.mkdir()
    _multi_file_repo(repo)
    ctx = stage_0_intake(repo, days=30)
    now = datetime.now(timezone.utc)
    flow = Flow(
        name="checkout-flow",
        entry_point_file="src/x.ts",
        entry_point_line=2,
        paths=["src/x.ts", "src/y.ts"],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=now, health_score=90.0, uuid="fl-checkout",
        flow_symbol_attributions=[FlowSymbolAttribution(
            file="src/x.ts", symbol="createCheckoutSession",
            line_start=2, line_end=4, role="entry",
        )],
    )
    feat = Feature(
        name="billing", paths=["src/x.ts"], authors=[], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=now, health_score=90.0,
        flows=[flow], uuid="feat-billing",
    )
    expand_flows([feat], ctx, routes_index=[])

    # display_name derived deterministically from the resolved entry symbol.
    assert flow.display_name == "Create Checkout Session"
    # Stable id byte-identical.
    assert flow.name == "checkout-flow"
