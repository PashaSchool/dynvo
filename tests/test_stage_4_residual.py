"""Tests for ``faultline.pipeline_v2.stage_4_residual`` (Sprint A2).

Verifies:

  - Zero unattributed paths → empty Stage4Result, no LLM calls.
  - One Haiku call per structural cluster (not per chunk of N paths).
  - Saturation stop after ``SAT_WINDOW`` consecutive empty clusters.
  - Bad names (folder paths, Title Case, empty) are rejected.
  - Paths the LLM invents (not in this cluster) are stripped.
  - Cost tracker accumulates per cluster.
  - LLM failure does not crash the orchestrator.
  - Sprint A1 invariant: no share-cap truncation.
"""

from __future__ import annotations

import json
import threading
import types
from pathlib import Path
from typing import Any

from faultline.llm.cost import CostTracker
from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature
from faultline.pipeline_v2.stage_4_residual import (
    SAT_WINDOW,
    Stage4Result,
    _is_acceptable_name,
    stage_4_residual,
)


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


def _feat(name: str, paths: tuple[str, ...]) -> DeveloperFeature:
    return DeveloperFeature(
        name=name, paths=paths, sources=["route"], confidence="medium",
    )


class _FakeAnthropic:
    """Records Haiku calls and replays canned responses."""

    def __init__(
        self,
        responses: list[str],
        *,
        in_tokens: int = 200,
        out_tokens: int = 100,
        raise_on_call: int | None = None,
    ) -> None:
        self.responses = responses
        self._idx = 0
        self.calls: list[dict[str, Any]] = []
        self.in_tokens = in_tokens
        self.out_tokens = out_tokens
        self.raise_on_call = raise_on_call
        self.messages = self._Messages(self)
        self._lock = threading.Lock()

    class _Messages:
        def __init__(self, parent: "_FakeAnthropic") -> None:
            self._p = parent

        def create(self, **kwargs: Any) -> Any:
            with self._p._lock:
                self._p.calls.append(kwargs)
                if self._p.raise_on_call == len(self._p.calls):
                    raise RuntimeError("simulated outage")
                if self._p._idx < len(self._p.responses):
                    text = self._p.responses[self._p._idx]
                    self._p._idx += 1
                else:
                    text = self._p.responses[-1] if self._p.responses else '{"features":[]}'
            content = [types.SimpleNamespace(text=text)]
            usage = types.SimpleNamespace(
                input_tokens=self._p.in_tokens,
                output_tokens=self._p.out_tokens,
            )
            return types.SimpleNamespace(content=content, usage=usage)


# ── Unit: helpers ──────────────────────────────────────────────────────────


def test_is_acceptable_name_rules() -> None:
    assert _is_acceptable_name("billing-portal")
    assert _is_acceptable_name("auth")
    assert not _is_acceptable_name("app")
    assert not _is_acceptable_name("src")
    assert not _is_acceptable_name("lib")
    assert not _is_acceptable_name("utils")
    assert not _is_acceptable_name("BillingPortal")
    assert not _is_acceptable_name("Billing-Portal")
    assert not _is_acceptable_name("app/billing")
    assert not _is_acceptable_name("billing.portal")
    assert not _is_acceptable_name("billing portal")
    assert not _is_acceptable_name("")
    assert not _is_acceptable_name("   ")


# ── Orchestrator: zero residual ────────────────────────────────────────────


def test_zero_unattributed_no_llm_calls(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, files=[])
    result = stage_4_residual(
        [], ctx, existing_features=[], client=_FakeAnthropic(responses=[]),
    )
    assert isinstance(result, Stage4Result)
    assert result.residual_features == []
    assert result.llm_calls == 0
    assert result.cost_usd == 0.0
    assert result.clusters_total == 0
    assert result.saturation_stopped is False


def test_no_client_returns_empty_with_warning(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, files=["a.ts"])
    result = stage_4_residual(
        ["a.ts"], ctx, existing_features=[],
        client=None, _client_factory=lambda: None,
    )
    assert result.residual_features == []
    assert any("no Anthropic client" in w for w in result.warnings)


# ── One call per cluster ───────────────────────────────────────────────────


def test_one_call_per_cluster(tmp_path: Path) -> None:
    """3 structurally distinct paths → 3 clusters → 3 Haiku calls."""
    residual = [
        "api/user_handler.go",       # api/handler/.go/shallow
        "web/admin_view.tsx",        # web/view/.tsx/shallow
        "lib/totp_token.ts",         # lib/token/.ts/shallow
    ]
    ctx = _ctx(tmp_path, files=residual)
    # Each cluster sees one canned response; clusters are processed in
    # sorted-key order (api < lib < web).
    responses = [
        json.dumps({"features": [
            {"name": "user-handler-api", "paths": ["api/user_handler.go"]},
        ]}),
        json.dumps({"features": [
            {"name": "totp-token", "paths": ["lib/totp_token.ts"]},
        ]}),
        json.dumps({"features": [
            {"name": "admin-view", "paths": ["web/admin_view.tsx"]},
        ]}),
    ]
    client = _FakeAnthropic(responses=responses)
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    assert result.clusters_total == 3
    assert result.clusters_processed == 3
    assert result.llm_calls == 3
    assert {f.name for f in result.residual_features} == {
        "user-handler-api", "totp-token", "admin-view",
    }
    assert result.saturation_stopped is False


def test_large_cluster_passed_through_no_size_cap(tmp_path: Path) -> None:
    """A single cluster with 1000 paths still gets ONE call — not chunked."""
    residual = [f"api/svc-{i:04d}-handler.go" for i in range(1000)]
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({"features": [
        {"name": "svc-handlers", "paths": residual[:5]},
    ]})
    client = _FakeAnthropic(responses=[canned])
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    assert result.clusters_total == 1
    assert result.llm_calls == 1
    assert len(result.residual_features) == 1
    # Sample is capped, so the LLM only sees ≤15 of the 1000 — prompt
    # token budget stays sane.


# ── Saturation stop ────────────────────────────────────────────────────────


def test_saturation_stop_after_window_of_empty_clusters(tmp_path: Path) -> None:
    """After SAT_WINDOW consecutive empty responses, the loop stops."""
    # 10 structurally distinct clusters (distinct top-level dirs).
    residual = [f"dir{i}/file-thing-leaf.ts" for i in range(10)]
    ctx = _ctx(tmp_path, files=residual)
    # First response yields a feature; the rest empty.
    responses = [
        json.dumps({"features": [
            {"name": "first-feature", "paths": ["dir0/file-thing-leaf.ts"]},
        ]}),
    ] + [json.dumps({"features": []})] * 20
    client = _FakeAnthropic(responses=responses)
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)

    assert result.clusters_total == 10
    # cluster 1 emits → counter resets to 0.
    # clusters 2,3,4 are empty → counter hits SAT_WINDOW=3 → stop after #4.
    assert result.clusters_processed == 1 + SAT_WINDOW
    assert result.saturation_stopped is True
    assert len(result.residual_features) == 1


def test_no_saturation_when_every_cluster_emits(tmp_path: Path) -> None:
    residual = [f"dir{i}/x-handler.ts" for i in range(5)]
    ctx = _ctx(tmp_path, files=residual)
    responses = [
        json.dumps({"features": [
            {"name": f"feature-{i}", "paths": [f"dir{i}/x-handler.ts"]},
        ]})
        for i in range(5)
    ]
    client = _FakeAnthropic(responses=responses)
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    assert result.clusters_processed == 5
    assert result.saturation_stopped is False
    assert len(result.residual_features) == 5


def test_repeated_names_dont_reset_saturation(tmp_path: Path) -> None:
    """A name already emitted by an earlier cluster is NOT a "new" name."""
    residual = [f"dir{i}/x-handler.ts" for i in range(5)]
    ctx = _ctx(tmp_path, files=residual)
    # Every cluster re-emits the SAME name — the dedup means "no new".
    same_name = json.dumps({"features": [
        {"name": "shared-feature", "paths": []},   # paths invalid → cluster will not see this
    ]})
    # Use cluster-valid paths for the first cluster only to get one accepted.
    first = json.dumps({"features": [
        {"name": "shared-feature", "paths": ["dir0/x-handler.ts"]},
    ]})
    repeated = [json.dumps({"features": [
        {"name": "shared-feature", "paths": [f"dir{i}/x-handler.ts"]},
    ]}) for i in range(1, 5)]
    client = _FakeAnthropic(responses=[first] + repeated)
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    # First cluster emits; clusters 2,3,4 propose the same name → no new.
    # Counter hits SAT_WINDOW=3 after cluster 4.
    assert result.clusters_processed == 1 + SAT_WINDOW
    assert result.saturation_stopped is True
    assert {f.name for f in result.residual_features} == {"shared-feature"}
    _ = same_name


# ── A1 invariant: no share-cap truncation ──────────────────────────────────


def test_stage_4_no_share_cap(tmp_path: Path) -> None:
    """Even with zero deterministic features, all residual features survive."""
    # Use 1 cluster so the test isn't tangled with saturation behaviour.
    residual = [f"a/widget-{i}-leaf.ts" for i in range(3)]
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({
        "features": [
            {"name": f"resid-{i}", "paths": [residual[i]]} for i in range(3)
        ],
    })
    client = _FakeAnthropic(responses=[canned])
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    assert len(result.residual_features) == 3
    assert not any("share" in w.lower() for w in result.warnings)


# ── Naming-discipline rejection (per-cluster) ──────────────────────────────


def test_bad_names_rejected_within_cluster(tmp_path: Path) -> None:
    residual = ["api/a-leaf.ts", "api/b-leaf.ts"]    # 1 cluster
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({
        "features": [
            {"name": "app", "paths": ["api/a-leaf.ts"]},        # generic folder
            {"name": "Billing", "paths": ["api/a-leaf.ts"]},    # Title Case
            {"name": "x/y", "paths": ["api/b-leaf.ts"]},        # slash
            {"name": "good-feature", "paths": ["api/b-leaf.ts"]},
        ],
    })
    client = _FakeAnthropic(responses=[canned])
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    names = [f.name for f in result.residual_features]
    assert names == ["good-feature"]
    rejected = result.rejected_names
    assert any("app" in r for r in rejected)
    assert any("billing" in r.lower() for r in rejected)


def test_invented_paths_stripped(tmp_path: Path) -> None:
    """The LLM proposed paths not in this cluster — strip them."""
    residual = ["api/x-leaf.ts"]   # 1 cluster
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({
        "features": [
            {"name": "phantom-feature", "paths": ["api/x-leaf.ts", "imaginary/y.ts"]},
            {"name": "all-fake", "paths": ["totally/imaginary.ts"]},
        ],
    })
    client = _FakeAnthropic(responses=[canned])
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    names = [f.name for f in result.residual_features]
    assert names == ["phantom-feature"]
    assert result.residual_features[0].paths == ("api/x-leaf.ts",)
    assert any("all-fake" in r for r in result.rejected_names)


# ── LLM failure handling ──────────────────────────────────────────────────


def test_llm_failure_does_not_crash(tmp_path: Path) -> None:
    residual = ["a-leaf.ts", "b-leaf.ts"]
    ctx = _ctx(tmp_path, files=residual)
    # 1 cluster (same top-level "", suffix "leaf"), one call that raises.
    client = _FakeAnthropic(responses=['{"features":[]}'], raise_on_call=1)
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    assert isinstance(result, Stage4Result)
    assert result.residual_features == []
    assert result.llm_calls == 1


# ── Cost tracker ──────────────────────────────────────────────────────────


def test_cost_tracker_records_per_cluster(tmp_path: Path) -> None:
    # 2 distinct clusters → 2 Haiku calls.
    residual = ["api/x-handler.go", "web/y-view.tsx"]
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({"features": [{"name": "x-tools", "paths": ["api/x-handler.go"]}]})
    client = _FakeAnthropic(responses=[canned], in_tokens=500, out_tokens=200)
    tracker = CostTracker(max_cost=None)
    result = stage_4_residual(
        residual, ctx, existing_features=[],
        client=client, cost_tracker=tracker,
    )
    # Two clusters → two LLM calls. First emits, second emits same canned (deduped).
    assert result.llm_calls == 2
    assert tracker.call_count == 2
    assert tracker.total_cost_usd > 0


def test_cost_cap_aborts_loop(tmp_path: Path) -> None:
    """Once the tracker spend exceeds the cap, remaining clusters skip."""
    # 5 clusters; tracker pre-loaded near the cap so the cap fires immediately.
    residual = [f"dir{i}/x-handler.ts" for i in range(5)]
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({"features": []})
    client = _FakeAnthropic(responses=[canned], in_tokens=10_000_000, out_tokens=1_000_000)
    tracker = CostTracker(max_cost=0.01)
    result = stage_4_residual(
        residual, ctx, existing_features=[],
        client=client, cost_tracker=tracker, cost_cap_usd=0.01,
    )
    # First call blows the cap → subsequent clusters bail before calling.
    assert result.clusters_processed <= 2
    assert any("cost cap" in w for w in result.warnings)
