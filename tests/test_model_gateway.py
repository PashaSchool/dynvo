"""Tests for the env-aware AI-Gateway model-name shim."""

from __future__ import annotations

import pytest

from faultline.llm.model_gateway import (
    gateway_mode_enabled,
    resolve_model,
    to_gateway_model,
)

# The authoritative mapping table, mirrored from the director's gateway query.
KNOWN_MAPPING = [
    ("claude-haiku-4-5", "anthropic/claude-haiku-4.5"),
    ("claude-haiku-4-5-20251001", "anthropic/claude-haiku-4.5"),
    ("claude-sonnet-4-6", "anthropic/claude-sonnet-4.6"),
    ("claude-sonnet-4-20250514", "anthropic/claude-sonnet-4"),
    ("claude-opus-4-6", "anthropic/claude-opus-4.6"),
    ("claude-opus-4-7", "anthropic/claude-opus-4.7"),
    ("claude-opus-4-20250514", "anthropic/claude-opus-4"),
]

GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"


@pytest.fixture
def _clean_env(monkeypatch):
    """Ensure both gateway env knobs are unset before each test."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("FAULTLINES_MODEL_NAMESPACE", raising=False)
    return monkeypatch


# ── to_gateway_model: authoritative table ───────────────────────────────


@pytest.mark.parametrize("engine_id,gateway_slug", KNOWN_MAPPING)
def test_known_table_maps_every_row(engine_id, gateway_slug):
    assert to_gateway_model(engine_id) == gateway_slug


# ── to_gateway_model: generic fallback ──────────────────────────────────


def test_generic_dash_to_dot_for_unknown():
    # Unknown id, dash version pair -> dot, anthropic/ prefix.
    assert to_gateway_model("claude-haiku-9-3") == "anthropic/claude-haiku-9.3"


def test_generic_strips_date_for_unknown():
    assert (
        to_gateway_model("claude-future-7-2-20991231")
        == "anthropic/claude-future-7.2"
    )


def test_generic_leaves_bare_major_as_is():
    # No minor component -> no dot conversion, just prefixed.
    assert to_gateway_model("claude-zeta-5") == "anthropic/claude-zeta-5"


def test_generic_strips_date_with_bare_major():
    assert to_gateway_model("claude-zeta-5-20300101") == "anthropic/claude-zeta-5"


def test_generic_warns_on_unknown(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        to_gateway_model("claude-haiku-9-3")
    assert any("unknown model id" in r.message for r in caplog.records)


def test_known_does_not_warn(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        to_gateway_model("claude-haiku-4-5")
    assert not any("unknown model id" in r.message for r in caplog.records)


# ── to_gateway_model: idempotency ───────────────────────────────────────


@pytest.mark.parametrize(
    "already_slug",
    [
        "anthropic/claude-haiku-4.5",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-opus-4",
    ],
)
def test_idempotent_on_namespaced_input(already_slug):
    assert to_gateway_model(already_slug) == already_slug


def test_double_application_is_stable():
    once = to_gateway_model("claude-sonnet-4-6")
    twice = to_gateway_model(once)
    assert once == twice == "anthropic/claude-sonnet-4.6"


def test_empty_string_passthrough():
    assert to_gateway_model("") == ""


# ── gateway_mode_enabled ────────────────────────────────────────────────


def test_gateway_disabled_when_env_unset(_clean_env):
    assert gateway_mode_enabled() is False


def test_gateway_enabled_via_vercel_base_url(_clean_env):
    _clean_env.setenv("ANTHROPIC_BASE_URL", GATEWAY_BASE_URL)
    assert gateway_mode_enabled() is True


def test_gateway_disabled_for_direct_anthropic_base_url(_clean_env):
    _clean_env.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    assert gateway_mode_enabled() is False


def test_gateway_enabled_via_namespace_env(_clean_env):
    _clean_env.setenv("FAULTLINES_MODEL_NAMESPACE", "anthropic")
    assert gateway_mode_enabled() is True


# ── resolve_model: gateway vs direct ────────────────────────────────────


@pytest.mark.parametrize("engine_id,gateway_slug", KNOWN_MAPPING)
def test_resolve_model_maps_in_gateway_mode(_clean_env, engine_id, gateway_slug):
    _clean_env.setenv("ANTHROPIC_BASE_URL", GATEWAY_BASE_URL)
    assert resolve_model(engine_id) == gateway_slug


@pytest.mark.parametrize("engine_id,_gateway_slug", KNOWN_MAPPING)
def test_resolve_model_noop_when_unset(_clean_env, engine_id, _gateway_slug):
    # Direct-Anthropic path: byte-identical passthrough of bare IDs.
    assert resolve_model(engine_id) == engine_id


def test_resolve_model_noop_for_direct_base_url(_clean_env):
    _clean_env.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    assert resolve_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"


def test_resolve_model_noop_preserves_unknown(_clean_env):
    assert resolve_model("some-custom-model-x") == "some-custom-model-x"
