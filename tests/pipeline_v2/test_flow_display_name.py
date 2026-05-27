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


def test_expand_flows_sets_display_name_to_kebab_name(tmp_path: Path) -> None:
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

    # REVERTED 2026-05-26: display_name now mirrors the kebab stable id
    # (the human-readable deriver is dormant; user wants kebab labels).
    assert flow.display_name == "checkout-flow"
    # Stable id byte-identical.
    assert flow.name == "checkout-flow"
    # short_label: kebab name without the "-flow" suffix (additive field).
    assert flow.short_label == "checkout"


# ── Fix 1 (2026-05-26): <file> sentinel leak ────────────────────────
def test_file_sentinel_symbol_falls_through_to_basename() -> None:
    # entry symbol is the literal "<file>" sentinel — humanizing it
    # verbatim produced display_name "<file>" (the #1 cal-com defect).
    # It must fall through to the humanized entry-file basename.
    fl = _flow(
        "apple-calendar-webhook-flow",
        entry_file="app/api/integrations/apple-calendar/webhook.ts",
        entry_symbol="<file>",
    )
    assert derive_display_name(fl, routes=[]) == "Apple Calendar Webhook"


def test_file_sentinel_via_entry_point_file_only() -> None:
    # No entry_point object, no symbol — only entry_point_file. Still
    # must produce a basename label, never "<file>".
    now = datetime.now(timezone.utc)
    fl = Flow(
        name="zoom-video-flow", entry_point_file="lib/integrations/zoom/video.ts",
        paths=["lib/integrations/zoom/video.ts"], authors=[], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=now, health_score=90.0,
    )
    label = derive_display_name(fl, routes=[])
    assert "<" not in label
    assert label == "Zoom Video"


def test_humanize_symbol_returns_empty_for_sentinels() -> None:
    assert _humanize_symbol("<file>") == ""
    assert _humanize_symbol("<deep:foo>") == ""
    assert _humanize_symbol("<anonymous>") == ""


# ── Fix 3 (2026-05-26): demote weak symbol labels ───────────────────
_WEAK_VERB_CASES = ["GET", "POST", "Put", "patch", "DELETE", "getHandler", "postHandler"]


@pytest.mark.parametrize("symbol", _WEAK_VERB_CASES)
def test_bare_verb_handler_demoted_to_fallback(symbol: str) -> None:
    # A bare HTTP-verb / *Handler symbol must NOT win the symbol tier;
    # falls through to the humanized name.
    fl = _flow(
        "create-team-invite-flow",
        entry_file="lib/teams/invite.ts",
        entry_symbol=symbol,
    )
    label = derive_display_name(fl, routes=[])
    assert label != symbol.title()
    assert "Handler" not in label
    # Name is richer than the file basename → name wins.
    assert label == "Create Team Invite"


def test_version_suffixed_dto_demoted() -> None:
    # CancelBookingOutput_2024_08_13 → trailing date tokens make a label
    # worse than the kebab name; demote to fallback.
    assert _humanize_symbol("CancelBookingOutput_2024_08_13") == ""
    fl = _flow(
        "cancel-booking-flow",
        entry_file="lib/booking/cancel.ts",
        entry_symbol="CancelBookingOutput_2024_08_13",
    )
    assert derive_display_name(fl, routes=[]) == "Cancel Booking"


def test_single_version_token_kept() -> None:
    # A single trailing numeric token is fine ("V2 Endpoint" reads ok);
    # only a run of >=2 (date/version) is demoted.
    assert _humanize_symbol("listBookings2") == "List Bookings2"


def test_dangling_conjunction_trimmed() -> None:
    # 6-word cap leaving a trailing "Or" / "With" → drop the dangler.
    out = _humanize_symbol("updateBillingCredentialsOrSubscriptionWith")
    assert not out.lower().endswith(" or")
    assert not out.lower().endswith(" with")
    assert out.split()[-1].lower() not in {"or", "and", "with"}
