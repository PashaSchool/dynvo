"""Tests for ``faultline.pipeline_v2.llm_health`` — fail-loud LLM auth.

Covers (per the 2026-06-06 gateway-429 and 2026-06-10 dead-key
incidents):

  - helper unit behaviour (classification, sanitization, state machine,
    ``stamp_llm_degraded``);
  - per-stage short-circuit: an auth-class error on the FIRST call
    stops every remaining LLM call in that stage (call-count proof);
  - cross-stage propagation: one shared ``LlmHealth`` flips scan-wide;
  - e2e through ``run_pipeline_v2``: a dead key yields
    ``scan_meta.llm_degraded`` + the warning while the scan still
    SUCCEEDS (artifact written);
  - healthy scans carry NO ``llm_degraded`` key (additive contract).
"""

from __future__ import annotations

import json
import subprocess
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.llm_health import (
    LLM_AUTH_WARNING,
    LLM_RATE_LIMIT_WARNING,
    LlmHealth,
    is_auth_error,
    is_rate_limit_error,
    sanitize_detail,
    stamp_llm_degraded,
)
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature


# ── Fakes ───────────────────────────────────────────────────────────────────


class _AuthError(Exception):
    """Auth-shaped exception (HTTP 401 carried as ``status_code``)."""

    status_code = 401


class _ForbiddenError(Exception):
    status_code = 403


class _RateLimitError(Exception):
    status_code = 429


class _ServerError(Exception):
    status_code = 500


class _FailingClient:
    """Anthropic-shaped client whose every call raises ``exc_factory()``."""

    def __init__(self, exc_factory: Any = _AuthError) -> None:
        self.call_count = 0
        self._exc_factory = exc_factory
        self._lock = threading.Lock()
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, parent: "_FailingClient") -> None:
            self._p = parent

        def create(self, **_kw: Any) -> Any:
            with self._p._lock:
                self._p.call_count += 1
            raise self._p._exc_factory("invalid x-api-key")

    # The CLI/e2e path constructs ``Anthropic(api_key=...)`` — accept it.
    def __call__(self, *args: Any, **kwargs: Any) -> "_FailingClient":
        return self


def _ctx(tmp_path: Path, files: list[str]) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


# ── Classification helpers ──────────────────────────────────────────────────


def test_is_auth_error_matches_401_and_403_status() -> None:
    assert is_auth_error(_AuthError("invalid x-api-key"))
    assert is_auth_error(_ForbiddenError("permission denied"))
    assert not is_auth_error(_RateLimitError("too many requests"))
    assert not is_auth_error(_ServerError("overloaded"))
    assert not is_auth_error(RuntimeError("plain failure"))


def test_is_auth_error_matches_anthropic_sdk_exceptions() -> None:
    anthropic = pytest.importorskip("anthropic")
    httpx = pytest.importorskip("httpx")
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    auth = anthropic.AuthenticationError(
        "invalid x-api-key",
        response=httpx.Response(401, request=req),
        body=None,
    )
    perm = anthropic.PermissionDeniedError(
        "forbidden",
        response=httpx.Response(403, request=req),
        body=None,
    )
    rate = anthropic.RateLimitError(
        "rate limited",
        response=httpx.Response(429, request=req),
        body=None,
    )
    assert is_auth_error(auth)
    assert is_auth_error(perm)
    assert not is_auth_error(rate)
    assert is_rate_limit_error(rate)
    assert not is_rate_limit_error(auth)


def test_is_auth_error_reads_httpx_style_response_attribute() -> None:
    exc = RuntimeError("gateway said no")
    exc.response = types.SimpleNamespace(status_code=401)  # type: ignore[attr-defined]
    assert is_auth_error(exc)


def test_is_rate_limit_error_matches_429_status() -> None:
    assert is_rate_limit_error(_RateLimitError("slow down"))
    assert not is_rate_limit_error(_AuthError("nope"))


# ── sanitize_detail ─────────────────────────────────────────────────────────


def test_sanitize_detail_redacts_api_key_material() -> None:
    raw = "401 invalid x-api-key: sk-ant-api03-AbC123_xYz provided"
    out = sanitize_detail(raw)
    assert "sk-ant-api03-AbC123_xYz" not in out
    assert "sk-ant-[REDACTED]" in out


def test_sanitize_detail_caps_length() -> None:
    assert len(sanitize_detail("x" * 10_000)) <= 300


# ── LlmHealth state machine ─────────────────────────────────────────────────


def test_healthy_state_allows_calls_and_reports_none() -> None:
    health = LlmHealth()
    assert health.should_call()
    assert not health.auth_failed
    assert health.degraded() is None
    assert health.warning() is None


def test_first_auth_failure_flips_flag_and_wins() -> None:
    health = LlmHealth()
    flipped = health.record_failure(
        _AuthError("invalid x-api-key sk-ant-secret123"), stage="stage_3_flows",
    )
    assert flipped is True
    assert not health.should_call()
    assert health.auth_failed
    # A second auth failure in a later stage does NOT overwrite first_stage.
    health.record_failure(_AuthError("still dead"), stage="stage_4_residual")
    payload = health.degraded()
    assert payload is not None
    assert payload["reason"] == "auth_error"
    assert payload["first_stage"] == "stage_3_flows"
    assert "sk-ant-secret123" not in payload["detail"]
    assert health.warning() == LLM_AUTH_WARNING


def test_non_auth_failures_never_short_circuit() -> None:
    health = LlmHealth()
    assert health.record_failure(_ServerError("boom"), stage="s") is False
    assert health.record_failure(_RateLimitError("429"), stage="s") is False
    assert health.should_call()


def test_rate_limit_storm_with_zero_successes_stamps_rate_limited() -> None:
    health = LlmHealth()
    for _ in range(5):
        health.record_failure(_RateLimitError("429"), stage="stage_3_flows")
    payload = health.degraded()
    assert payload is not None
    assert payload["reason"] == "rate_limited"
    assert payload["first_stage"] == "stage_3_flows"
    assert health.warning() == LLM_RATE_LIMIT_WARNING


def test_rate_limit_with_any_success_stays_silent() -> None:
    health = LlmHealth()
    health.record_failure(_RateLimitError("429"), stage="stage_3_flows")
    health.record_success()
    assert health.degraded() is None
    assert health.warning() is None


def test_auth_error_wins_over_rate_limited() -> None:
    health = LlmHealth()
    health.record_failure(_RateLimitError("429"), stage="stage_3_flows")
    health.record_failure(_AuthError("401"), stage="stage_4_residual")
    payload = health.degraded()
    assert payload is not None
    assert payload["reason"] == "auth_error"
    assert payload["first_stage"] == "stage_4_residual"


# ── stamp_llm_degraded ──────────────────────────────────────────────────────


def test_stamp_is_a_noop_on_healthy_scan() -> None:
    health = LlmHealth()
    health.record_success()
    scan_meta: dict[str, Any] = {"warnings": []}
    stamp_llm_degraded(scan_meta, health)
    assert "llm_degraded" not in scan_meta
    assert scan_meta["warnings"] == []


def test_stamp_writes_payload_and_warning_once() -> None:
    health = LlmHealth()
    health.record_failure(_AuthError("401"), stage="stage_8_analyst")
    scan_meta: dict[str, Any] = {"warnings": ["pre-existing"]}
    stamp_llm_degraded(scan_meta, health)
    stamp_llm_degraded(scan_meta, health)  # idempotent
    assert scan_meta["llm_degraded"]["reason"] == "auth_error"
    assert scan_meta["llm_degraded"]["first_stage"] == "stage_8_analyst"
    assert scan_meta["warnings"].count(LLM_AUTH_WARNING) == 1


# ── Per-stage short-circuit: Stage 3 (flows) ───────────────────────────────


def _make_ts_file(tmp_path: Path, rel: str) -> None:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        "export function A() { return 1; }\n"
        "export function B() { return 2; }\n"
        "export async function GET(req: Request) { return null; }\n",
        encoding="utf-8",
    )


def test_stage_3_auth_failure_short_circuits_remaining_features(
    tmp_path: Path,
) -> None:
    from faultline.pipeline_v2.stage_3_flows import stage_3_flows

    rels = [f"app/feat{i}/route.ts" for i in range(3)]
    for rel in rels:
        _make_ts_file(tmp_path, rel)
    features = [
        DeveloperFeature(
            name=f"feat{i}", paths=(rel,), sources=["route"], confidence="medium",
        )
        for i, rel in enumerate(rels)
    ]
    ctx = _ctx(tmp_path, files=rels)
    client = _FailingClient(_AuthError)
    health = LlmHealth()

    result = stage_3_flows(
        features, ctx, client=client, max_workers=1, llm_health=health,
    )

    # FIRST call 401s → every remaining feature is skipped, not retried.
    assert client.call_count == 1
    assert health.auth_failed
    assert health.degraded() == {
        "reason": "auth_error",
        "first_stage": "stage_3_flows",
        "detail": "invalid x-api-key",
    }
    # Stage still returns a result (degrade, don't abort).
    assert len(result.features_with_flows) == len(features)


# ── Per-stage short-circuit: Stage 4 (residual) ────────────────────────────


def test_stage_4_auth_failure_stops_cluster_loop(tmp_path: Path) -> None:
    from faultline.pipeline_v2.stage_4_residual import stage_4_residual

    # 3 non-singleton clusters (api/.go, lib/.ts, web/.tsx).
    residual = [
        "api/user_handler.go", "api/billing_routes.go",
        "lib/totp_token.ts", "lib/auth_session.ts",
        "web/admin_view.tsx", "web/dashboard_view.tsx",
    ]
    ctx = _ctx(tmp_path, files=residual)
    client = _FailingClient(_AuthError)
    health = LlmHealth()

    result = stage_4_residual(
        residual, ctx, existing_features=[], client=client, llm_health=health,
    )

    assert result.clusters_total == 3
    assert client.call_count == 1  # 2 remaining clusters never called
    assert health.auth_failed
    assert any("auth failure" in w for w in result.warnings)


def test_stage_4_skips_loop_entirely_when_flag_already_flipped(
    tmp_path: Path,
) -> None:
    from faultline.pipeline_v2.stage_4_residual import stage_4_residual

    residual = ["lib/totp_token.ts", "lib/auth_session.ts"]
    ctx = _ctx(tmp_path, files=residual)
    client = _FailingClient(_AuthError)
    health = LlmHealth()
    health.record_failure(_AuthError("dead earlier"), stage="stage_3_flows")

    result = stage_4_residual(
        residual, ctx, existing_features=[], client=client, llm_health=health,
    )

    assert client.call_count == 0  # cross-stage propagation
    assert result.clusters_processed == 0


# ── Per-stage: stack auditor (Stage 0.5) ────────────────────────────────────


def test_stack_auditor_records_auth_failure_and_falls_back(
    tmp_path: Path,
) -> None:
    from faultline.pipeline_v2.stack_auditor import run_stack_auditor

    ctx = _ctx(tmp_path, files=["app/page.tsx"])
    client = _FailingClient(_AuthError)
    health = LlmHealth()

    verdict = run_stack_auditor(ctx, client=client, llm_health=health)

    assert client.call_count == 1
    assert health.auth_failed
    payload = health.degraded()
    assert payload is not None and payload["first_stage"] == "stack_auditor"
    assert verdict.fallback_used


# ── Per-stage: Stage 6.7b UF refiner ────────────────────────────────────────


def test_uf_refiner_auth_failure_short_circuits_remaining_domains() -> None:
    from faultline.models.types import UserFlow
    from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows

    def _uf(uf_id: str, domain: str) -> UserFlow:
        return UserFlow(
            id=uf_id,
            name=f"{domain} journey",
            domain=domain,
            product_feature_id=domain,
            intent="author",
            resource=domain,
            member_flow_ids=[],
            member_count=0,
            routes=[],
        )

    ufs = [_uf("UF-001", "billing"), _uf("UF-002", "auth"), _uf("UF-003", "teams")]
    client = _FailingClient(_AuthError)
    health = LlmHealth()

    out, _telemetry = refine_user_flows(ufs, [], client=client, llm_health=health)

    assert client.call_count == 1  # remaining domains skipped
    assert health.auth_failed
    assert len(out) == 3  # degrade: deterministic UFs kept


# ── Per-stage: Stage 8 analyst + marketing clusterer call helpers ──────────


def test_stage_8_analyst_call_records_failure_then_skips() -> None:
    from faultline.pipeline_v2.stage_8_analyst import _call_sonnet

    client = _FailingClient(_AuthError)
    health = LlmHealth()

    text, in_t, out_t, _ = _call_sonnet(
        client, model="claude-sonnet-4-6", system="s", user="u",
        llm_health=health,
    )
    assert (text, in_t, out_t) == ("", 0, 0)
    assert health.auth_failed
    assert health.degraded()["first_stage"] == "stage_8_analyst"  # type: ignore[index]

    # Second attempt never reaches the client.
    _call_sonnet(
        client, model="claude-sonnet-4-6", system="s", user="u",
        llm_health=health,
    )
    assert client.call_count == 1


def test_stage_8_marketing_clusterer_call_records_failure_then_skips() -> None:
    from faultline.pipeline_v2.stage_8_marketing_clusterer import _call_haiku

    client = _FailingClient(_AuthError)
    health = LlmHealth()

    out = _call_haiku(
        client, model="claude-haiku-4-5-20251001", system="s", user="u",
        max_tokens=10, llm_health=health,
    )
    assert out == ("", 0, 0)
    assert health.auth_failed
    assert health.degraded()["first_stage"] == "stage_8_marketing_clusterer"  # type: ignore[index]

    _call_haiku(
        client, model="claude-haiku-4-5-20251001", system="s", user="u",
        max_tokens=10, llm_health=health,
    )
    assert client.call_count == 1


# ── Per-stage: Stage 2 llm_reconcile path ───────────────────────────────────


def _stage_2_ambiguous_inputs(tmp_path: Path) -> tuple[dict, ScanContext, float]:
    from faultline.pipeline_v2.extractors.base import AnchorCandidate
    from faultline.pipeline_v2.stage_2_reconcile import _jaccard, _slug_tokens

    cands = {
        "mvc": [AnchorCandidate(
            name="user-profile", source="mvc",
            paths=("app/controllers/user_profile_controller.rb",),
            confidence_self=0.7,
        )],
        "package": [AnchorCandidate(
            name="user-account", source="package", paths=(".",),
            confidence_self=0.7,
        )],
    }
    ctx = _ctx(
        tmp_path,
        files=["app/controllers/user_profile_controller.rb", "package.json"],
    )
    j = _jaccard(_slug_tokens("user-profile"), _slug_tokens("user-account"))
    return cands, ctx, j


def test_stage_2_skips_llm_second_opinion_after_auth_failure(
    tmp_path: Path,
) -> None:
    from faultline.pipeline_v2.stage_2_reconcile import stage_2_reconcile

    cands, ctx, j = _stage_2_ambiguous_inputs(tmp_path)
    health = LlmHealth()
    health.record_failure(_AuthError("dead key"), stage="stack_auditor")

    calls: list[tuple[str, str]] = []

    def _llm_stub(a: Any, b: Any) -> str | None:
        calls.append((a.name, b.name))
        return "user-profile"

    result = stage_2_reconcile(
        cands, ctx,
        jaccard_threshold=j,
        llm_reconcile=True,
        llm_health=health,
        _llm_call=_llm_stub,
    )

    assert calls == []  # 2nd opinion skipped — priority rule resolves
    assert any(f.name == "user-account" for f in result.features)


def test_stage_2_llm_second_opinion_still_fires_when_healthy(
    tmp_path: Path,
) -> None:
    from faultline.pipeline_v2.stage_2_reconcile import stage_2_reconcile

    cands, ctx, j = _stage_2_ambiguous_inputs(tmp_path)
    health = LlmHealth()
    calls: list[tuple[str, str]] = []

    def _llm_stub(a: Any, b: Any) -> str | None:
        calls.append((a.name, b.name))
        return "user-profile"

    result = stage_2_reconcile(
        cands, ctx,
        jaccard_threshold=j,
        llm_reconcile=True,
        llm_health=health,
        _llm_call=_llm_stub,
    )

    assert len(calls) == 1
    assert any(f.name == "user-profile" for f in result.features)


# ── E2E through run_pipeline_v2 ─────────────────────────────────────────────


def _git_init_with_one_commit(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True,
    )
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat: initial"], cwd=repo, check=True,
    )


_REPO_FILES = {
    "package.json": json.dumps(
        {"name": "demo", "dependencies": {"next": "14.0.0"}},
    ),
    "app/billing/page.tsx": "export default function Page() { return null; }\n",
    "app/auth/page.tsx": "export default function Page() { return null; }\n",
    "next.config.js": "module.exports = {};\n",
}


def test_run_pipeline_v2_dead_key_degrades_visibly_but_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-06-10 incident shape: every LLM call 401s.

    The scan must still produce an artifact (degrade, don't abort) and
    must stamp ``scan_meta.llm_degraded`` + the warning. Exactly ONE
    real call is attempted scan-wide (the first one flips the flag).
    """
    import anthropic

    from faultline.pipeline_v2.run import run_pipeline_v2

    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "demo-app"
    _git_init_with_one_commit(repo, _REPO_FILES)

    # Every stage's client factory does ``Anthropic(api_key=...)`` —
    # swap the SDK class for a client whose calls all raise 401.
    failing = _FailingClient(_AuthError)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-invalid")
    monkeypatch.setattr(anthropic, "Anthropic", failing)

    out_path = tmp_path / "feature-map.json"
    result = run_pipeline_v2(repo, model="haiku", out_path=out_path)

    # Scan SUCCEEDED — artifact exists, deterministic output intact.
    assert out_path.exists()
    assert result["path"] == str(out_path)

    # Machine-readable degradation stamp.
    degraded = result["llm_degraded"]
    assert degraded["reason"] == "auth_error"
    # The stack auditor fires the scan's first LLM call.
    assert degraded["first_stage"] == "stack_auditor"
    assert "sk-ant-test-invalid" not in degraded["detail"]

    # Prominent human-readable warning.
    assert LLM_AUTH_WARNING in result["warnings"]

    # Short-circuit proof: ONE doomed call scan-wide, not hundreds.
    assert failing.call_count == 1

    # The stamp is persisted in the written FeatureMap too.
    on_disk = json.loads(out_path.read_text())
    assert on_disk["scan_meta"]["llm_degraded"]["reason"] == "auth_error"
    assert LLM_AUTH_WARNING in on_disk["scan_meta"]["warnings"]


def test_run_pipeline_v2_healthy_scan_has_no_llm_degraded_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Additive contract: ``llm_degraded`` is ABSENT on healthy scans."""
    from faultline.pipeline_v2.run import run_pipeline_v2

    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # No key → stages run without LLM clients (healthy-deterministic).
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    repo = tmp_path / "demo-app"
    _git_init_with_one_commit(repo, _REPO_FILES)

    out_path = tmp_path / "feature-map.json"
    result = run_pipeline_v2(repo, model="haiku", out_path=out_path)

    assert out_path.exists()
    assert "llm_degraded" not in result
    assert LLM_AUTH_WARNING not in result.get("warnings", [])
    on_disk = json.loads(out_path.read_text())
    assert "llm_degraded" not in on_disk["scan_meta"]
