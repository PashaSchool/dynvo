"""Tests for faultline/cloud/."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from faultline.cloud.sync import push_feature_map, send_mcp_events_batch


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("FAULTLINE_API_KEY", raising=False)
    monkeypatch.delenv("FAULTLINE_API_BASE", raising=False)
    yield


class TestPushFeatureMap:
    def test_no_op_without_api_key(self) -> None:
        result = push_feature_map({"repo_path": "/x", "features": []})
        assert result is None

    def test_pushes_dict_when_api_key_set(self, monkeypatch) -> None:
        monkeypatch.setenv("FAULTLINE_API_KEY", "fl_test")
        with patch("httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"ok": True, "scan_id": "abc"}
            mock_post.return_value = mock_response
            result = push_feature_map({"repo_path": "/x", "features": []})
        assert result == {"ok": True, "scan_id": "abc"}
        call = mock_post.call_args
        assert call.kwargs["headers"]["Authorization"] == "Bearer fl_test"

    def test_returns_none_on_4xx(self, monkeypatch) -> None:
        monkeypatch.setenv("FAULTLINE_API_KEY", "fl_test")
        with patch("httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 401
            mock_response.text = "Invalid API key"
            mock_post.return_value = mock_response
            result = push_feature_map({"repo_path": "/x"})
        assert result is None

    def test_uses_custom_api_base(self, monkeypatch) -> None:
        monkeypatch.setenv("FAULTLINE_API_KEY", "fl_test")
        monkeypatch.setenv("FAULTLINE_API_BASE", "http://localhost:3000/api/cloud")
        with patch("httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"ok": True}
            mock_post.return_value = mock_response
            push_feature_map({"repo_path": "/x"})
        url = mock_post.call_args.args[0]
        assert url.startswith("http://localhost:3000/api/cloud/scans")


class TestSendMcpEventsBatch:
    def test_empty_batch_no_op(self) -> None:
        assert send_mcp_events_batch([]) == 0

    def test_no_op_without_api_key(self) -> None:
        assert send_mcp_events_batch([{"tool_name": "x", "occurred_at": "2026-01-01"}]) == 0

    def test_sends_batch_and_returns_accepted_count(self, monkeypatch) -> None:
        monkeypatch.setenv("FAULTLINE_API_KEY", "fl_test")
        with patch("httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"accepted": 3}
            mock_post.return_value = mock_response
            count = send_mcp_events_batch([
                {"tool_name": "find_feature", "occurred_at": "2026-01-01"},
                {"tool_name": "get_hotspots", "occurred_at": "2026-01-01"},
                {"tool_name": "list_features", "occurred_at": "2026-01-01"},
            ])
        assert count == 3
        body = mock_post.call_args.kwargs["json"]
        assert "events" in body and len(body["events"]) == 3
