"""Tests for the pluggable LLM client factory (Sprint 8h)."""

from __future__ import annotations

import pytest

from faultline.llm.factory import resolve_role


def test_resolve_role_uses_built_in_default_when_no_env(monkeypatch):
    for var in list(monkeypatch._setitem):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("FAULTLINE_LLM_DEFAULT_PROVIDER", raising=False)
    monkeypatch.delenv("FAULTLINE_LLM_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("FAULTLINE_LLM_CRITIQUE_PROVIDER", raising=False)
    monkeypatch.delenv("FAULTLINE_LLM_CRITIQUE_MODEL", raising=False)
    provider, model = resolve_role("critique")
    assert provider == "anthropic"
    assert model == "claude-haiku-4-5"


def test_resolve_role_default_env_overrides_builtin(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LLM_DEFAULT_PROVIDER", "gemini")
    monkeypatch.delenv("FAULTLINE_LLM_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("FAULTLINE_LLM_CRITIQUE_PROVIDER", raising=False)
    monkeypatch.delenv("FAULTLINE_LLM_CRITIQUE_MODEL", raising=False)
    provider, model = resolve_role("critique")
    assert provider == "gemini"
    # When provider switches but no model env set, picks gemini default
    assert model == "gemini-2.5-flash"


def test_role_specific_env_wins_over_default(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LLM_DEFAULT_PROVIDER", "anthropic")
    monkeypatch.setenv("FAULTLINE_LLM_CANONICALIZER_PROVIDER", "gemini")
    monkeypatch.setenv("FAULTLINE_LLM_CANONICALIZER_MODEL", "gemini-2.5-flash")
    provider, model = resolve_role("canonicalizer")
    assert provider == "gemini"
    assert model == "gemini-2.5-flash"


def test_role_with_hyphen_normalises_to_underscore_env(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LLM_FLOW_CRITIQUE_PROVIDER", "anthropic")
    monkeypatch.setenv("FAULTLINE_LLM_FLOW_CRITIQUE_MODEL", "claude-sonnet-4-6")
    provider, model = resolve_role("flow-critique")
    assert provider == "anthropic"
    assert model == "claude-sonnet-4-6"


def test_default_model_falls_back_to_anthropic_when_unknown_provider(monkeypatch):
    monkeypatch.setenv("FAULTLINE_LLM_DEFAULT_PROVIDER", "frobnicator")
    monkeypatch.delenv("FAULTLINE_LLM_DEFAULT_MODEL", raising=False)
    provider, model = resolve_role("anything")
    # Provider stays as user requested (factory will reject at make_client)
    assert provider == "frobnicator"
    # Model falls back to anthropic default since no mapping exists
    assert model == "claude-haiku-4-5"
