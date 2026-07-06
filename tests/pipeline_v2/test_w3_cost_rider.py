"""W3 rider — anchored Call-1 cost accounting (chain4 finding).

The chain4 validation wave measured the CLI cost line reporting $0.0000
while 6.7d stage telemetry carried ``cost_usd=0.147`` (a fresh anchored
Sonnet draw). Root cause: ``run.py`` snapshots ``scan_meta["cost_usd"]``
/ ``["calls"]`` BEFORE ``run_finalize_phase`` — where every 6.7b / 6.7c
/ 6.7d (anchored included) LLM call lives — so the output JSON, the CLI
line, and the wave-runner ledger under-reported the whole finalize-phase
bill. The tracker + decision-log taps themselves were always wired on
the anchored path; these tests pin BOTH facts:

1. the anchored Call-1 records into the shared ``CostTracker`` and the
   Phase-0 decision log (the fine-tuning dataset tap) — regression
   guard for the taps;
2. the finalize phase re-snapshots the tracker's FULL bill into
   ``scan_meta`` before the Stage-7 output write, so a keyed scan whose
   only LLM spend happens in finalize reports ``cost_usd > 0``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.llm.cost import CostTracker
from faultline.models.types import Feature, Flow, MemberFile, UserFlow
from faultline.pipeline_v2 import run as run_module
from faultline.pipeline_v2.run import run_pipeline_v2
from faultline.pipeline_v2.stage_3_flows import Stage3Result
from faultline.pipeline_v2.stage_4_residual import Stage4Result

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── Shared minimal anchored fixture (mirrors test_spine_w2b_review_fixes) ──


def _mint_dev(name: str, paths: list[str], flows=None) -> Feature:
    return Feature(
        name=name, paths=list(paths),
        member_files=[MemberFile(path=p, role="anchor", confidence=1.0,
                                 primary=True) for p in paths],
        flows=flows or [], product_feature_id="old",
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def _anchored_inputs():
    """(user_flows, product_features, devs, routes) for one anchored PF."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import run_anchored_mint

    routes = [{"pattern": "/settings", "method": "PAGE",
               "file": "app/settings/page.tsx"}]
    dev = _mint_dev(
        "settings", ["app/settings/page.tsx"],
        flows=[Flow(name="edit-settings-flow",
                    entry_point_file="app/settings/page.tsx",
                    paths=["app/settings/page.tsx"], authors=["a"],
                    total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
                    last_modified=_NOW, health_score=100.0)])
    pfs, _ = run_anchored_mint([dev], routes, SimpleNamespace(
        workspaces=None, tracked_files=[], repo_path=Path("."),
        monorepo=False))
    uf = UserFlow(id="UF-001", name="Manage settings", resource="setting",
                  domain="settings", product_feature_id="settings",
                  intent="manage", member_flow_ids=["edit-settings-flow"],
                  member_count=1)
    return [uf], pfs, [dev], routes


_PAYLOAD = (
    '{"product_features":[{"name":"Settings","description":"ok"}],'
    '"user_flows":[{"name":"Manage settings","resource":"setting",'
    '"product_feature":"Settings","from_flows":["UF-001"],'
    '"from_dev_features":["settings"]}]}'
)


class _FakeClient:
    """Scripted client that always answers with a grounded anchored draw
    and reports real token usage (the tracker's raw material)."""

    def __init__(self) -> None:
        self.messages = self
        self.calls = 0

    def create(self, **kw):
        self.calls += 1
        return SimpleNamespace(
            content=[SimpleNamespace(text=_PAYLOAD)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


# ── 1. Anchored Call-1 → CostTracker (the brief-mandated guard) ─────────


def test_anchored_call1_records_to_cost_tracker() -> None:
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        run_journey_abstraction,
    )

    ufs, pfs, devs, routes = _anchored_inputs()
    tracker = CostTracker()
    client = _FakeClient()
    _ufs2, _pfs2, _map, tele = run_journey_abstraction(
        ufs, pfs, devs, routes, client=client, model="claude-haiku-4-5",
        cost_tracker=tracker, anchored=True)

    assert tele["applied"] is True
    assert client.calls >= 1
    assert tracker.call_count >= 1, (
        "REGRESSION (chain4 rider): the anchored Call-1 bypassed the "
        "shared CostTracker — its cost is invisible to scan_meta, the "
        "CLI line, and the decision-log dataset")
    rec = tracker.records[0]
    assert rec.label == "stage_6_7d"
    assert rec.input_tokens == 1000 and rec.output_tokens == 200
    assert rec.cost_usd > 0
    # Telemetry and tracker must agree on the bill (same estimator).
    assert abs(tele["cost_usd"] - tracker.total_cost_usd) < 1e-9


def test_anchored_call1_hits_decision_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Phase-0 decision log (fine-tuning dataset tap) carries BOTH
    the ``llm_call`` record (via CostTracker.record) and the parsed
    ``decision`` record for the anchored draw."""
    from faultline.llm import decision_log
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        run_journey_abstraction,
    )

    log_dir = tmp_path / "training"
    monkeypatch.setenv("FAULTLINE_DECISION_LOG", "1")
    monkeypatch.setenv("FAULTLINE_DECISION_LOG_DIR", str(log_dir))
    decision_log.begin_scan("w3-rider-test")
    try:
        ufs, pfs, devs, routes = _anchored_inputs()
        tracker = CostTracker()
        _r = run_journey_abstraction(
            ufs, pfs, devs, routes, client=_FakeClient(),
            model="claude-haiku-4-5", cost_tracker=tracker, anchored=True)
    finally:
        decision_log.end_scan()

    log_file = log_dir / "decisions-w3-rider-test.jsonl"
    assert log_file.is_file(), "no decision log written for anchored path"
    rows = [json.loads(line) for line in log_file.read_text().splitlines()]
    calls = [r for r in rows if r["kind"] == "llm_call"
             and r["role"] == "journey_abstraction_draw"]
    decisions = [r for r in rows if r["kind"] == "decision"
                 and r["role"] == "journey_abstraction_draw"]
    assert calls, f"anchored Call-1 missing from decision log: {rows}"
    assert calls[0]["input_tokens"] == 1000
    assert decisions, "anchored draw's parsed decision record missing"
    assert decisions[0]["decision"]["pf_names"] == ["Settings"]


# ── 2. Finalize-phase full-bill refresh (end-to-end) ────────────────────


class _ScriptedAnthropic:
    """Fake Anthropic client (constructor-compatible with the SDK):
    every consumer receives the same grounded JSON + token usage."""

    def __init__(self, *a, **kw):
        self.messages = self

    def create(self, **kw):
        payload = (
            '{"product_features":[{"name":"Foo","description":"x"}],'
            '"user_flows":[{"name":"Manage foo","resource":"foo",'
            '"product_feature":"Foo","from_flows":["UF-001"],'
            '"from_dev_features":["foo"]}]}'
        )
        return SimpleNamespace(
            content=[SimpleNamespace(text=payload)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )


def _flowful_stage_3(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_stage_3(features, ctx, *, model, cost_tracker, **_kw):
        from faultline.pipeline_v2.stage_3_flows import (
            FeatureWithFlows,
            FlowSpec,
        )
        out = []
        for f in features:
            flows = []
            if any(p.endswith("app/foo/page.tsx") for p in (f.paths or [])):
                flows = [FlowSpec(
                    name="edit-foo-flow",
                    description="edit foo",
                    entry_point_file="app/foo/page.tsx",
                    reach_paths=("app/foo/page.tsx",),
                    depth_reached=1,
                )]
            out.append(FeatureWithFlows(feature=f, flows=flows, rationale="t"))
        return Stage3Result(features_with_flows=out, cost_usd=0.0,
                            llm_calls=0, warnings=[])

    def _fake_stage_4(unattributed, ctx, existing, *, model, cost_tracker, **_kw):
        return Stage4Result(residual_features=[], cost_usd=0.0, llm_calls=0,
                            warnings=[], clusters_total=0, clusters_processed=0,
                            saturation_stopped=False, rejected_names=[])

    monkeypatch.setattr(run_module, "stage_3_flows", _fake_stage_3)
    monkeypatch.setattr(run_module, "stage_4_residual", _fake_stage_4)


def _git_init_with_one_commit(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: initial"], cwd=repo, check=True)


_FIXTURE = {
    "package.json": json.dumps({
        "name": "w3rider", "private": True,
        "dependencies": {"next": "14.0.0"},
    }),
    "next.config.js": "module.exports = {};\n",
    "app/foo/page.tsx": "export default function Page() { return null; }\n",
}


def test_finalize_refreshes_scan_meta_cost_with_full_bill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end keyed run whose ONLY LLM spend happens inside the
    finalize phase (stage 3/4 are $0 fakes): the output scan_meta must
    carry the tracker's full bill, not the stale pre-finalize $0
    snapshot (chain4: CLI cost line = $0 while 6.7d cost_usd = 0.147)."""
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    _flowful_stage_3(monkeypatch)
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_LLM_ABSTRACTION", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-w3rider")
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _ScriptedAnthropic)

    repo = tmp_path / "w3rider-app"
    _git_init_with_one_commit(repo, _FIXTURE)
    out = tmp_path / "scan.json"
    run_pipeline_v2(str(repo), days=3650, out_path=out, run_id="w3rider")
    doc = json.loads(out.read_text())
    meta = doc.get("scan_meta") or {}

    tele67d = meta.get("stage_6_7d_journey_abstraction") or {}
    assert tele67d.get("applied") is True, (
        "test-validity guard: 6.7d must APPLY for the finalize bill "
        f"to exist (got {tele67d.get('fallback')})")
    assert tele67d.get("cost_usd", 0) > 0

    assert meta.get("cost_usd", 0) > 0, (
        "REGRESSION (chain4 rider): scan_meta.cost_usd is the stale "
        "pre-finalize snapshot — finalize-phase LLM spend (anchored "
        "Call-1) is invisible to the output/CLI/ledger")
    assert meta.get("calls", 0) >= 1
    # The refreshed figure covers AT LEAST the 6.7d bill.
    assert meta["cost_usd"] >= round(tele67d["cost_usd"], 4)
