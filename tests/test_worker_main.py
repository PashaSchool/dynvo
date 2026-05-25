"""Unit tests for ``worker.main`` — helpers only, not the polling loop.

The polling loop touches Postgres and is exercised in integration via
the deployed Fly Machine + a staging pg-boss queue. Here we cover the
pure helpers we control: config parsing, log redaction, and the
HTTP/subprocess shapes.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

# Worker lives at the REPO ROOT (not inside the published `faultline`
# package) so we add the repo root to sys.path explicitly.
import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from worker import main as worker_main  # noqa: E402


# ------------------------------------------------------------ WorkerConfig


def _env(**overrides: str) -> dict[str, str]:
    base = {
        "DATABASE_URL": "postgres://u:p@h/db",
        "FAULTLINES_DASHBOARD_URL": "https://app.faultlines.dev/",
        "FAULTLINES_WORKER_TOKEN": "tok",
        "ANTHROPIC_API_KEY": "sk-ant",
    }
    base.update(overrides)
    return base


def test_config_from_env_strips_trailing_slash() -> None:
    with mock.patch.dict(os.environ, _env(), clear=True):
        cfg = worker_main.WorkerConfig.from_env()
    assert cfg.dashboard_url == "https://app.faultlines.dev"
    assert cfg.queue_name == "scan-jobs"
    assert cfg.timeout_sec == 1800
    assert cfg.poll_interval_sec == 5


def test_config_missing_required_raises() -> None:
    env = _env()
    env.pop("ANTHROPIC_API_KEY")
    with mock.patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit) as exc:
            worker_main.WorkerConfig.from_env()
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_config_honours_overrides() -> None:
    env = _env(
        WORKER_TIMEOUT_SEC="600",
        WORKER_QUEUE_NAME="incremental",
        WORKER_POLL_INTERVAL_SEC="1",
    )
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = worker_main.WorkerConfig.from_env()
    assert cfg.timeout_sec == 600
    assert cfg.queue_name == "incremental"
    assert cfg.poll_interval_sec == 1


# ----------------------------------------------------------------- _redact


def test_redact_strips_github_token_segment() -> None:
    url = "https://x-access-token:ghp_secretsecret@github.com/o/r.git"
    assert worker_main._redact(url) == "https://x-access-token:***@github.com/o/r.git"


def test_redact_passes_through_safe_strings() -> None:
    assert worker_main._redact("--output") == "--output"
    assert worker_main._redact("/tmp/scans/job-1/repo") == "/tmp/scans/job-1/repo"


# ---------------------------------------------------------- post_scan_to_dashboard


def test_post_scan_to_dashboard_sets_auth_and_metadata_headers() -> None:
    cfg = _make_cfg()
    captured: dict[str, object] = {}

    class _StubResp:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"stored": True}

    class _StubClient:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        def __enter__(self) -> "_StubClient":
            return self

        def __exit__(self, *a: object) -> None:
            pass

        def post(self, url: str, *, headers: dict[str, str], content: bytes) -> _StubResp:
            captured["url"] = url
            captured["headers"] = headers
            captured["content"] = content
            return _StubResp()

    with mock.patch.object(worker_main.httpx, "Client", _StubClient):
        ack = worker_main.post_scan_to_dashboard(
            cfg,
            org_id="org_1",
            repo_id="repo_42",
            commit_sha="deadbeef",
            is_full_scan=False,
            scan_blob=b"{}",
        )

    assert ack == {"stored": True}
    assert captured["url"] == "https://app.faultlines.dev/api/internal/scan-complete"
    headers = captured["headers"]  # type: ignore[assignment]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-Faultlines-Org-Id"] == "org_1"
    assert headers["X-Faultlines-Repo-Id"] == "repo_42"
    assert headers["X-Faultlines-Commit-Sha"] == "deadbeef"
    assert headers["X-Faultlines-Full-Scan"] == "0"


# ------------------------------------------------------------ _run wrapping


def test_run_invokes_subprocess_with_check_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _fake_run(cmd, *, env, check, timeout):  # type: ignore[no-untyped-def]
        seen["cmd"] = cmd
        seen["check"] = check
        seen["timeout"] = timeout
        return mock.Mock(returncode=0)

    monkeypatch.setattr(worker_main.subprocess, "run", _fake_run)
    worker_main._run(["echo", "hi"], timeout=10)
    assert seen["cmd"] == ["echo", "hi"]
    assert seen["check"] is True
    assert seen["timeout"] == 10


# ------------------------------------------------------------ test helpers


def _make_cfg(**overrides: object) -> worker_main.WorkerConfig:
    defaults: dict[str, object] = dict(
        database_url="postgres://u:p@h/db",
        dashboard_url="https://app.faultlines.dev",
        worker_token="tok",
        anthropic_api_key="sk-ant",
        scan_scratch_dir=Path("/tmp/scans"),
        queue_name="scan-jobs",
        timeout_sec=1800,
        poll_interval_sec=5,
    )
    defaults.update(overrides)
    return worker_main.WorkerConfig(**defaults)  # type: ignore[arg-type]
