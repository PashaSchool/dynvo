"""Tests for ``faultline.pipeline_v2.stage_4_residual`` (Sprint A2 + A2b).

Verifies:

  - Zero unattributed paths → empty Stage4Result, no LLM calls.
  - One Haiku call per NON-SINGLETON structural cluster.
  - Saturation stop after ``SAT_WINDOW`` consecutive empty clusters
    (over the non-singleton subset).
  - A2b: singletons handled deterministically — synthesized OR skipped
    without any LLM call.
  - A2b: local cost-cap fallback fires when the shared tracker has no
    cap of its own.
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
    assert result.singletons_synthesized == 0
    assert result.singletons_skipped == 0
    assert result.cost_cap_hit is False


def test_no_client_returns_empty_with_warning(tmp_path: Path) -> None:
    # A2b: with a single unattributed file, the singleton synthesizer
    # may emit a feature WITHOUT calling the client. To exercise the
    # "no client" path we need ≥1 NON-SINGLETON cluster (so the LLM
    # is actually consulted). Two paths under the same key.
    ctx = _ctx(tmp_path, files=["api/a-leaf.ts", "api/b-leaf.ts"])
    result = stage_4_residual(
        ["api/a-leaf.ts", "api/b-leaf.ts"], ctx, existing_features=[],
        client=None, _client_factory=lambda: None,
    )
    assert result.residual_features == []
    assert any("no Anthropic client" in w for w in result.warnings)


# ── A2b: singleton handling ────────────────────────────────────────────────


def test_singletons_are_synthesized_not_called(tmp_path: Path) -> None:
    """Three structurally distinct paths → three singletons → no LLM."""
    residual = [
        "api/user_handler.go",       # api/.go/shallow size=1 → singleton
        "web/admin_view.tsx",        # web/.tsx/shallow size=1 → singleton
        "lib/totp_token.ts",         # lib/.ts/shallow size=1 → singleton
    ]
    ctx = _ctx(tmp_path, files=residual)
    client = _FakeAnthropic(responses=[])  # any LLM call would crash test
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)

    assert result.clusters_total == 3
    assert result.llm_calls == 0
    assert result.singletons_synthesized == 3
    assert result.singletons_skipped == 0
    names = {f.name for f in result.residual_features}
    # ``lib`` is in the noise set → stripped, leaving just ``totp-token``.
    assert "api-user-handler" in names
    assert "web-admin-view" in names
    assert "totp-token" in names
    assert client.calls == []  # zero Haiku calls confirmed


def test_singleton_skip_path_skipped(tmp_path: Path) -> None:
    """Known manifests + root dotfiles skip without synthesis."""
    residual = [".gitignore", "package.json"]
    ctx = _ctx(tmp_path, files=residual)
    client = _FakeAnthropic(responses=[])
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)

    assert result.singletons_synthesized == 0
    assert result.singletons_skipped == 2
    assert result.residual_features == []
    assert result.llm_calls == 0


def test_singleton_dup_name_collision_handled(tmp_path: Path) -> None:
    """Two singletons whose synthesized names collide — first wins, second skipped.

    ``src/foo.ts`` and ``lib/foo.ts`` both strip their noise parent
    (``src`` and ``lib`` are both in the noise set) → both synthesize
    to the same name ``foo``. The clusterer puts them in DIFFERENT
    clusters (distinct top-level dirs), so the in-loop dup guard is
    what catches the collision.
    """
    residual = ["src/foo.ts", "lib/foo.ts"]
    ctx = _ctx(tmp_path, files=residual)
    client = _FakeAnthropic(responses=[])
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    assert result.clusters_total == 2
    assert result.singletons_synthesized == 1
    assert result.singletons_skipped == 1
    assert result.llm_calls == 0
    names = {f.name for f in result.residual_features}
    assert names == {"foo"}


# ── One call per non-singleton cluster ─────────────────────────────────────


def test_one_call_per_non_singleton_cluster(tmp_path: Path) -> None:
    """Three distinct keys with ≥2 paths each → 3 LLM clusters."""
    # 3 keys: ("api", ".go", "shallow"), ("lib", ".ts", "shallow"),
    #         ("web", ".tsx", "shallow"). Each holds ≥2 paths.
    residual = [
        "api/user_handler.go", "api/billing_routes.go",
        "lib/totp_token.ts", "lib/auth_session.ts",
        "web/admin_view.tsx", "web/dashboard_view.tsx",
    ]
    ctx = _ctx(tmp_path, files=residual)
    # Sorted order: api < lib < web.
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
    assert result.singletons_synthesized == 0
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


def test_mixed_singletons_and_clusters(tmp_path: Path) -> None:
    """Two singletons get synthesized, one multi-path cluster goes to LLM."""
    residual = [
        # singleton: ("api", ".go", "shallow")
        "api/health.go",
        # singleton: ("web", ".tsx", "mid")
        "web/admin/page.tsx",
        # cluster of 3: ("lib", ".ts", "shallow")
        "lib/totp.ts", "lib/jwt.ts", "lib/session.ts",
    ]
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({"features": [
        {"name": "auth-primitives", "paths": ["lib/totp.ts", "lib/jwt.ts"]},
    ]})
    client = _FakeAnthropic(responses=[canned])
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    assert result.clusters_total == 3
    assert result.singletons_synthesized == 2
    assert result.llm_calls == 1
    assert result.clusters_processed == 1
    names = {f.name for f in result.residual_features}
    assert "auth-primitives" in names
    # ``api/health.go`` → ``api-health`` (``api`` is NOT in noise set)
    assert "api-health" in names


# ── Saturation stop ────────────────────────────────────────────────────────


def test_saturation_stop_after_window_of_empty_clusters(tmp_path: Path) -> None:
    """After SAT_WINDOW consecutive empty responses on non-singleton
    clusters, the loop stops."""
    # 10 NON-SINGLETON clusters: each has 2 paths under a distinct
    # top-level dir, all .ts, all shallow.
    residual: list[str] = []
    for i in range(10):
        residual.append(f"dir{i}/file_a-leaf.ts")
        residual.append(f"dir{i}/file_b-leaf.ts")
    ctx = _ctx(tmp_path, files=residual)
    # First response yields a feature; the rest empty.
    responses = [
        json.dumps({"features": [
            {"name": "first-feature", "paths": ["dir0/file_a-leaf.ts"]},
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
    assert result.singletons_synthesized == 0


def test_no_saturation_when_every_cluster_emits(tmp_path: Path) -> None:
    # 5 non-singleton clusters, each size 2, each emits a unique name.
    residual: list[str] = []
    for i in range(5):
        residual.append(f"dir{i}/x_a-handler.ts")
        residual.append(f"dir{i}/x_b-handler.ts")
    ctx = _ctx(tmp_path, files=residual)
    responses = [
        json.dumps({"features": [
            {"name": f"feature-{i}", "paths": [f"dir{i}/x_a-handler.ts"]},
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
    residual: list[str] = []
    for i in range(5):
        residual.append(f"dir{i}/x_a-handler.ts")
        residual.append(f"dir{i}/x_b-handler.ts")
    ctx = _ctx(tmp_path, files=residual)
    # Every cluster re-emits the SAME name — the dedup means "no new".
    first = json.dumps({"features": [
        {"name": "shared-feature", "paths": ["dir0/x_a-handler.ts"]},
    ]})
    repeated = [json.dumps({"features": [
        {"name": "shared-feature", "paths": [f"dir{i}/x_a-handler.ts"]},
    ]}) for i in range(1, 5)]
    client = _FakeAnthropic(responses=[first] + repeated)
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    # First cluster emits; clusters 2,3,4 propose the same name → no new.
    # Counter hits SAT_WINDOW=3 after cluster 4.
    assert result.clusters_processed == 1 + SAT_WINDOW
    assert result.saturation_stopped is True
    assert {f.name for f in result.residual_features} == {"shared-feature"}


# ── A1 invariant: no share-cap truncation ──────────────────────────────────


def test_stage_4_no_share_cap(tmp_path: Path) -> None:
    """Even with zero deterministic features, all residual features survive."""
    # 1 non-singleton cluster so the test isn't tangled with saturation.
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
    residual = ["api/a-leaf.ts", "api/b-leaf.ts"]    # 1 non-singleton cluster
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
    # Two-path cluster (so we hit the LLM, not the singleton synth).
    residual = ["api/x-leaf.ts", "api/y-leaf.ts"]
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
    residual = ["a-leaf.ts", "b-leaf.ts"]   # 1 cluster, root, size 2
    ctx = _ctx(tmp_path, files=residual)
    client = _FakeAnthropic(responses=['{"features":[]}'], raise_on_call=1)
    result = stage_4_residual(residual, ctx, existing_features=[], client=client)
    assert isinstance(result, Stage4Result)
    assert result.residual_features == []
    assert result.llm_calls == 1


# ── Cost tracker ──────────────────────────────────────────────────────────


def test_cost_tracker_records_per_cluster(tmp_path: Path) -> None:
    """Two non-singleton clusters → two Haiku calls → two tracker records."""
    residual = [
        "api/x-handler.go", "api/y-handler.go",        # cluster A: api/.go/shallow
        "web/x-view.tsx", "web/y-view.tsx",            # cluster B: web/.tsx/shallow
    ]
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({"features": [{"name": "x-tools", "paths": ["api/x-handler.go"]}]})
    client = _FakeAnthropic(responses=[canned], in_tokens=500, out_tokens=200)
    tracker = CostTracker(max_cost=None)
    result = stage_4_residual(
        residual, ctx, existing_features=[],
        client=client, cost_tracker=tracker,
    )
    assert result.llm_calls == 2
    assert tracker.call_count == 2
    assert tracker.total_cost_usd > 0


def test_cost_cap_aborts_loop(tmp_path: Path) -> None:
    """Once the tracker spend exceeds the cap, remaining clusters skip."""
    # 5 non-singleton clusters under distinct top-level dirs.
    residual: list[str] = []
    for i in range(5):
        residual.append(f"dir{i}/x_a-handler.ts")
        residual.append(f"dir{i}/x_b-handler.ts")
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
    assert any("cost cap" in w.lower() for w in result.warnings)


# ── A2b: local cost-cap fallback ──────────────────────────────────────────


def test_local_cost_cap_fires_when_shared_tracker_unlimited(tmp_path: Path) -> None:
    """When the orchestrator hands in a tracker with ``max_cost=None``,
    Stage 4's local cap still bounds spend."""
    residual: list[str] = []
    for i in range(5):
        residual.append(f"dir{i}/x_a-handler.ts")
        residual.append(f"dir{i}/x_b-handler.ts")
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({"features": [
        {"name": "f", "paths": ["dir0/x_a-handler.ts"]},
    ]})
    # Big token counts → first call already blows a tiny local cap.
    client = _FakeAnthropic(responses=[canned], in_tokens=10_000_000, out_tokens=1_000_000)
    tracker = CostTracker(max_cost=None)
    result = stage_4_residual(
        residual, ctx, existing_features=[],
        client=client, cost_tracker=tracker, cost_cap_usd=0.01,
    )
    # First call records cost > $0.01, second loop iteration checks
    # the fallback cap and breaks → clusters_processed == 1.
    assert result.clusters_processed == 1
    assert result.cost_cap_hit is True
    assert any("stage_4_cost_cap_hit" in w for w in result.warnings)


def test_local_cost_cap_inactive_when_shared_tracker_has_cap(tmp_path: Path) -> None:
    """If the shared tracker has its own cap, the local fallback stays off."""
    residual = ["api/a-handler.ts", "api/b-handler.ts"]   # 1 cluster size 2
    ctx = _ctx(tmp_path, files=residual)
    canned = json.dumps({"features": [
        {"name": "tools", "paths": ["api/a-handler.ts"]},
    ]})
    client = _FakeAnthropic(responses=[canned], in_tokens=200, out_tokens=100)
    tracker = CostTracker(max_cost=10.0)  # shared cap is generous
    result = stage_4_residual(
        residual, ctx, existing_features=[],
        client=client, cost_tracker=tracker, cost_cap_usd=0.0001,
    )
    # Even though cost_cap_usd is below the call's spend, the local
    # fallback should NOT fire because the shared tracker carries
    # an explicit cap → its cap is authoritative.
    assert result.cost_cap_hit is False
    assert result.clusters_processed == 1
