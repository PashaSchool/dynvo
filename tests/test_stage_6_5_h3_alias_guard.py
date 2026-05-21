"""Stage 6.5 H3 — semantic-name guard on dep-anchor rule.

Verifies that a developer feature whose name does NOT semantically
match a dep-anchor category's alias list cannot be claimed by that
category, even when the dep is imported somewhere in the repo.

This is the universal fix for the maybe-Addresses-in-Realtime bug
documented in memory/next-sprint-rails-extractor-suite.md.
"""

from __future__ import annotations

from faultline.pipeline_v2.stage_6_5_product_clusterer import (
    _feature_matches_aliases,
)


# ── Negative case (the bug from the spec) ─────────────────────────────────


def test_addresses_does_not_match_realtime_aliases() -> None:
    realtime_aliases = (
        "realtime", "websocket", "channel", "stream",
        "subscription", "presence", "broadcast",
    )
    assert (
        _feature_matches_aliases(
            "addresses",
            ["app/models/address.rb", "app/views/properties/address.html.erb"],
            realtime_aliases,
        )
        is False
    )


def test_addresses_does_not_match_billing_aliases() -> None:
    billing_aliases = (
        "billing", "payment", "subscription", "invoice",
        "checkout", "charge", "plan", "stripe",
    )
    assert (
        _feature_matches_aliases(
            "addresses",
            ["app/models/address.rb"],
            billing_aliases,
        )
        is False
    )


# ── Positive cases ────────────────────────────────────────────────────────


def test_realtime_channel_matches_realtime_aliases() -> None:
    realtime_aliases = ("realtime", "channel", "websocket")
    # Feature named after a channel
    assert (
        _feature_matches_aliases(
            "presence-channel",
            ["app/channels/presence_channel.rb"],
            realtime_aliases,
        )
        is True
    )


def test_subscription_matches_billing_aliases() -> None:
    billing_aliases = ("billing", "subscription", "stripe")
    assert (
        _feature_matches_aliases(
            "subscription",
            ["app/models/subscription.rb"],
            billing_aliases,
        )
        is True
    )


def test_path_basename_can_satisfy_match() -> None:
    """Feature slug doesn't carry the alias but a file basename does."""
    auth_aliases = ("auth", "session", "login")
    assert (
        _feature_matches_aliases(
            "core",
            ["app/services/login_service.rb"],
            auth_aliases,
        )
        is True
    )


def test_empty_alias_tuple_is_permissive() -> None:
    """Labels in YAML without name_aliases keep legacy behaviour."""
    assert (
        _feature_matches_aliases("anything", ["any/path.rb"], ())
        is True
    )


def test_case_insensitive_match() -> None:
    realtime_aliases = ("websocket",)
    assert (
        _feature_matches_aliases(
            "WebSocketChannel",
            [],
            realtime_aliases,
        )
        is True
    )
