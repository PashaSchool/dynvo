"""Sprint 24 — Gemini fallback unit tests (no live API)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from faultline.llm.gemini_fallback import (
    DEFAULT_GEMINI_FLASH,
    DEFAULT_GEMINI_PRO,
    GeminiClient,
    is_retriable,
    map_to_gemini,
    with_anthropic_fallback,
)


# ── is_retriable ─────────────────────────────────────────────────────


def test_is_retriable_429():
    exc = MagicMock()
    type(exc).__name__ = "APIStatusError"
    exc.status_code = 429
    assert is_retriable(exc)


def test_is_retriable_503():
    exc = MagicMock()
    type(exc).__name__ = "APIStatusError"
    exc.status_code = 503
    assert is_retriable(exc)


def test_is_retriable_connection_error():
    exc = MagicMock()
    type(exc).__name__ = "APIConnectionError"
    exc.status_code = None
    assert is_retriable(exc)


def test_is_retriable_overloaded_message():
    exc = Exception("Overloaded")
    assert is_retriable(exc)


def test_is_retriable_400_bad_request():
    exc = MagicMock()
    type(exc).__name__ = "BadRequestError"
    exc.status_code = 400
    assert not is_retriable(exc)


def test_is_retriable_401_auth():
    exc = MagicMock()
    type(exc).__name__ = "AuthenticationError"
    exc.status_code = 401
    assert not is_retriable(exc)


def test_is_retriable_normal_value_error():
    assert not is_retriable(ValueError("nope"))


# ── map_to_gemini ────────────────────────────────────────────────────


def test_map_haiku_to_flash():
    assert map_to_gemini("claude-haiku-4-5") == DEFAULT_GEMINI_FLASH


def test_map_sonnet_to_pro():
    assert map_to_gemini("claude-sonnet-4-6") == DEFAULT_GEMINI_PRO


def test_map_opus_to_pro():
    assert map_to_gemini("claude-opus-4") == DEFAULT_GEMINI_PRO


def test_map_unknown_to_pro():
    assert map_to_gemini("some-other-model") == DEFAULT_GEMINI_PRO


def test_map_none_to_pro():
    assert map_to_gemini(None) == DEFAULT_GEMINI_PRO


# ── GeminiClient ─────────────────────────────────────────────────────


def test_gemini_client_requires_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError):
        GeminiClient(api_key=None)


def test_gemini_client_picks_up_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    client = GeminiClient()
    assert client.api_key == "test-key"


# ── with_anthropic_fallback ──────────────────────────────────────────


def test_with_fallback_passes_through_on_success():
    """No fallback when primary succeeds."""
    primary = MagicMock(return_value="ok")
    result = with_anthropic_fallback("test", primary)
    assert result == "ok"
    primary.assert_called_once()


def test_with_fallback_reraises_non_retriable():
    """Non-retriable errors propagate without invoking fallback."""
    def primary():
        raise ValueError("logic bug")
    with pytest.raises(ValueError):
        with_anthropic_fallback("test", primary)


def test_with_fallback_propagates_when_no_google_key(monkeypatch):
    """Retriable error + no GOOGLE_API_KEY → original error propagates."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    def primary():
        exc = MagicMock()
        type(exc).__name__ = "APIConnectionError"
        exc.status_code = None
        raise exc
    with pytest.raises(Exception):
        with_anthropic_fallback("test", primary)


def test_with_fallback_attempts_gemini_when_key_set(monkeypatch):
    """With GOOGLE_API_KEY + retriable error + gemini_overrides → Gemini called."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")

    def primary():
        exc = type("APITimeoutError", (Exception,), {"status_code": None})("timeout")
        raise exc

    with patch("faultline.llm.gemini_fallback.GeminiClient") as mock_cls:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        mock_cls.return_value = mock_client

        result = with_anthropic_fallback(
            "test", primary,
            model="claude-haiku-4-5",
            gemini_overrides={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100},
        )
        assert result is mock_resp
        # Verify Gemini was called with mapped model
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == DEFAULT_GEMINI_FLASH


def test_with_fallback_no_overrides_reraises(monkeypatch):
    """Retriable + GOOGLE_API_KEY but caller didn't provide overrides → reraise."""
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")

    def primary():
        exc = type("APITimeoutError", (Exception,), {"status_code": None})("timeout")
        raise exc

    with pytest.raises(Exception):
        with_anthropic_fallback("test", primary, model="claude-haiku-4-5")
