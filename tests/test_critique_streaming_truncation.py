"""Sprint 9f gap test: critique streaming + JSON truncation recovery.

The auditor flagged this as one of the highest-risk gaps:

  - JSON truncation has broken critique TWICE in production
  - The streaming path (Anthropic ``messages.stream``) is not exercised
    by hermetic tests
  - The recovery function ``_recover_truncated_critique_json`` lives in
    ``critique.py`` and is only tested via direct calls

This module tests the full chain: streaming chunk delivery → text
accumulation → JSON parsing → truncation recovery.
"""

from __future__ import annotations

import json

import pytest


def test_recover_returns_partial_when_array_truncated_mid_object():
    """Sprint 9c regression: truncation mid-object in 'missed' array
    should recover the leading complete entries, not return [].
    """
    from faultline.aggregators.critique import _recover_truncated_critique_json

    raw = (
        '{"missed": ['
        '{"feature_name": "Auth", "matched_category": "auth", '
        '"files": ["a.ts"], "rationale": "x"}, '
        '{"feature_name": "Billing", "matched_categ'  # truncated
    )
    recovered = _recover_truncated_critique_json(raw)
    assert recovered is not None
    assert len(recovered["missed"]) == 1
    assert recovered["missed"][0]["feature_name"] == "Auth"


def test_recover_returns_empty_missed_when_no_complete_object():
    """When truncation occurs before any object closes, we still
    return a well-formed dict with empty missed list — never crash.
    """
    from faultline.aggregators.critique import _recover_truncated_critique_json

    raw = '{"missed": [{"feature_name": "Auth", "matched_categ'
    recovered = _recover_truncated_critique_json(raw)
    assert recovered == {"missed": []}


def test_recover_handles_strings_with_brackets_inside():
    """Brackets inside string values must not confuse the
    bracket-balance walker.
    """
    from faultline.aggregators.critique import _recover_truncated_critique_json

    raw = (
        '{"missed": ['
        '{"feature_name": "Auth (sso)", "matched_category": "auth", '
        '"files": ["a[b].ts", "c}d.ts"], "rationale": "x"}, '
        '{"feature_name": "Bill'  # truncated
    )
    recovered = _recover_truncated_critique_json(raw)
    assert recovered is not None
    assert len(recovered["missed"]) == 1
    assert recovered["missed"][0]["feature_name"] == "Auth (sso)"


def test_recover_returns_none_when_no_missed_key():
    """If the JSON doesn't even contain ``"missed"`` we can't recover —
    return None and let the caller log + return [].
    """
    from faultline.aggregators.critique import _recover_truncated_critique_json

    raw = '{"other_field": ["a", "b"'
    assert _recover_truncated_critique_json(raw) is None


def test_recover_handles_escaped_quotes_in_string():
    """Backslash-escaped quote inside a string must not flip
    the in-string tracker.
    """
    from faultline.aggregators.critique import _recover_truncated_critique_json

    raw = (
        '{"missed": ['
        r'{"feature_name": "Auth \"new\"", "matched_category": "auth", '
        '"files": ["a.ts"], "rationale": "x"}, '
        '{"feature_name"'  # truncated
    )
    recovered = _recover_truncated_critique_json(raw)
    assert recovered is not None
    assert len(recovered["missed"]) == 1


# ── Streaming adapter (Sprint 9d) ────────────────────────────────────


class _FakeStream:
    """Mimics anthropic.MessageStreamManager — chunks then final."""
    def __init__(self, chunks, stop_reason="end_turn", usage=None):
        self._chunks = chunks
        self._stop_reason = stop_reason
        self._usage = usage or {"input_tokens": 100, "output_tokens": 50}
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    @property
    def text_stream(self):
        for c in self._chunks:
            yield c
    def get_final_message(self):
        class _Final:
            content = []
            stop_reason = self._stop_reason
            usage = type("U", (), self._usage)()
        # Pydantic-style attribute access for usage
        final = _Final()
        return final


class _FakeAnthropicClient:
    def __init__(self, chunks, stop_reason="end_turn"):
        self.messages = self
        self._chunks = chunks
        self._stop_reason = stop_reason
    def create(self, **kw):
        # non-streaming fallback path — wraps single text into a content
        # block. Used when max_tokens < 8192.
        class _Resp:
            content = [type("Block", (), {"text": "".join(self._chunks)})()]
            stop_reason = self._stop_reason
            usage = type("U", (), {"input_tokens": 100, "output_tokens": 50})()
        return _Resp()
    def stream(self, **kw):
        usage = {"input_tokens": 100, "output_tokens": sum(len(c) for c in self._chunks)}
        return _FakeStream(self._chunks, self._stop_reason, usage)


def test_streaming_client_accumulates_chunks_into_text():
    """The streaming adapter must concatenate text chunks correctly
    and return a single LlmResponse — no chunks lost.
    """
    from faultline.llm.providers.anthropic_client import AnthropicLlmClient

    fake = _FakeAnthropicClient(
        chunks=["chunk1 ", "chunk2 ", "final"],
    )
    client = AnthropicLlmClient(client=fake, model="claude-haiku-4-5")
    response = client.complete(
        system="sys", user="user", max_tokens=32_768,
    )
    assert response.text == "chunk1 chunk2 final"
    assert response.stop_reason == "end_turn"


def test_streaming_client_handles_truncation_stop_reason():
    """When the model hits max_tokens, stop_reason='max_tokens'
    bubbles up so callers can decide to retry/recover.
    """
    from faultline.llm.providers.anthropic_client import AnthropicLlmClient

    truncated_json = (
        '{"missed": ['
        '{"feature_name": "Auth", "matched_category": "auth", '
        '"files": ["a.ts"], "rationale": "x"}, '
        '{"feature_name": "Bill'  # truncated
    )
    fake = _FakeAnthropicClient(
        chunks=[truncated_json], stop_reason="max_tokens",
    )
    client = AnthropicLlmClient(client=fake, model="claude-haiku-4-5")
    response = client.complete(
        system="sys", user="user", max_tokens=32_768,
    )
    assert response.stop_reason == "max_tokens"
    assert "Auth" in response.text
    # The text the model emitted IS truncated — recovery happens at
    # the parse_critique_response layer, not at the adapter layer.
