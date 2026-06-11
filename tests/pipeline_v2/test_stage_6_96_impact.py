"""Unit tests for Stage 6.96 — impact-over-time.

Event-rule tests (coupling_spike / decoupled, min-points gate, pooled
scale-invariant thresholds, born-later exclusion, trend banding) run
against a stubbed snapshot runner — pure logic, no git. A real-git
end-to-end test exercises the full worktree → index → reach path.
Old-JSON rehydration of the additive schema is covered at the bottom.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from faultline.models.types import (
    Commit,
    EntityHistory,
    Feature,
    HistoryEvent,
    ImpactPoint,
    TestEfficacy,
    UserFlow,
)
from faultline.pipeline_v2 import stage_6_96_impact as mod
from faultline.pipeline_v2.stage_6_96_impact import stage_6_96_impact


def _commit(sha: str, week_offset: int) -> Commit:
    return Commit(
        sha=sha,
        message="feat: x",
        author="dev",
        date=datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
        + timedelta(weeks=week_offset),
        files_changed=["a.ts"],
    )


def _history() -> EntityHistory:
    return EntityHistory(
        birth_week="2024-W01",
        test_efficacy=TestEfficacy(verdict="insufficient_data"),
    )


def _pf(name: str, paths: list[str], *, with_history: bool = True) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=["dev"],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
        health_score=100.0,
        layer="product",
        history=_history() if with_history else None,
    )


def _stub_runner(
    monkeypatch: pytest.MonkeyPatch,
    reach_by_entity: dict[str, list[tuple[int, int]]],
    n_snapshots: int,
) -> list[Commit]:
    """Stub ``run_snapshots`` so each entity (keyed by its first member
    path) gets the given (reach, members_present) series — no git."""
    commits = [_commit(f"s{i}", i) for i in range(n_snapshots)]

    def fake_run(
        repo_path: Path,
        shas: list[str],
        compute: Any,
        **kw: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        results = {}
        for snap_i, sha in enumerate(shas):
            per_entity = [
                series[snap_i]
                for series in fake_run.ordered_series  # type: ignore[attr-defined]
            ]
            results[sha] = per_entity
        telem = {
            "impact_snapshots": len(shas),
            "impact_skipped_snapshots": 0,
            "impact_budget_exceeded": False,
            "budget_sec": 1.0,
            "planned_snapshots": len(shas),
        }
        return results, telem

    fake_run.ordered_series = list(reach_by_entity.values())  # type: ignore[attr-defined]
    monkeypatch.setattr(mod, "run_snapshots", fake_run)
    return commits


def _run_with_stub(
    monkeypatch: pytest.MonkeyPatch,
    reach_by_entity: dict[str, list[tuple[int, int]]],
    n_snapshots: int,
) -> tuple[list[Feature], dict[str, Any]]:
    commits = _stub_runner(monkeypatch, reach_by_entity, n_snapshots)
    pfs = [_pf(name, [f"{name}.ts"]) for name in reach_by_entity]
    telemetry = stage_6_96_impact(
        pfs, [], [], [], commits, Path("/nonexistent"),
        snapshot_count=n_snapshots,
    )
    return pfs, telemetry


# Background entities with small, UNIFORM moves so the pooled P90 /
# median thresholds are dominated by ordinary deltas, not the entity
# under test (the rule is relative to the scan's own distribution).
# Identical series → the P90 lands on the shared 5% step, and the
# STRICT ``>`` comparison keeps the background itself event-free.
_BACKGROUND: dict[str, list[tuple[int, int]]] = {
    f"bg{i}": [(100, 4), (105, 4), (110, 4), (115, 4)] for i in range(9)
}


def test_impact_points_attached_in_snapshot_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pfs, telemetry = _run_with_stub(
        monkeypatch, {"ai": [(10, 1), (20, 1), (30, 1)]}, 3,
    )
    impact = pfs[0].history.impact
    assert [(p.week, p.reach, p.members_present) for p in impact] == [
        ("2024-W01", 10, 1), ("2024-W02", 20, 1), ("2024-W03", 30, 1),
    ]
    assert telemetry["entities_with_impact"] == 1
    assert telemetry["impact_snapshots"] == 3


def test_coupling_spike_fires_above_pooled_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series = dict(_BACKGROUND)
    series["ai"] = [(100, 4), (104, 4), (500, 4), (505, 4)]
    pfs, telemetry = _run_with_stub(monkeypatch, series, 4)
    ai = next(p for p in pfs if p.name == "ai")
    spikes = [e for e in ai.history.events if e.kind == "coupling_spike"]
    assert len(spikes) == 1
    assert spikes[0].week == "2024-W03"
    assert spikes[0].detail == "reach 104->500"
    assert telemetry["coupling_spike_events"] == 1
    # Background entities' ordinary moves never fire.
    for p in pfs:
        if p.name != "ai":
            assert all(
                e.kind not in ("coupling_spike", "decoupled")
                for e in p.history.events
            )


def test_decoupled_fires_on_relative_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series = dict(_BACKGROUND)
    series["i18n"] = [(490, 4), (500, 4), (80, 4), (78, 4)]
    pfs, telemetry = _run_with_stub(monkeypatch, series, 4)
    i18n = next(p for p in pfs if p.name == "i18n")
    drops = [e for e in i18n.history.events if e.kind == "decoupled"]
    assert len(drops) == 1
    assert drops[0].detail == "reach 500->80"
    assert telemetry["decoupled_events"] == 1


def test_min_points_gate_blocks_two_point_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series = dict(_BACKGROUND)
    series["spiky"] = [(10, 2), (900, 2)]  # huge jump, only 2 points
    # Pad to 4 snapshots: the stub indexes by snapshot, so repeat last.
    series["spiky"] = [(10, 2), (900, 2), (900, 2), (900, 2)]
    pfs, _ = _run_with_stub(monkeypatch, series, 4)
    # Sanity: with 4 points the same jump DOES fire...
    spiky = next(p for p in pfs if p.name == "spiky")
    assert any(e.kind == "coupling_spike" for e in spiky.history.events)
    # ...and with only 2 points it must not.
    short = {"short": [(10, 2), (900, 2)]}
    pfs2, telemetry2 = _run_with_stub(monkeypatch, short, 2)
    assert all(
        e.kind not in ("coupling_spike", "decoupled")
        for e in pfs2[0].history.events
    )
    assert telemetry2["coupling_spike_events"] == 0


def test_born_later_steps_excluded_from_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series = dict(_BACKGROUND)
    # Entity doesn't exist at snapshot 1 (members_present=0): the
    # 0 → 400 step measures birth, never a coupling event.
    series["newborn"] = [(0, 0), (400, 4), (404, 4), (408, 4)]
    pfs, _ = _run_with_stub(monkeypatch, series, 4)
    newborn = next(p for p in pfs if p.name == "newborn")
    assert all(
        e.kind not in ("coupling_spike", "decoupled")
        for e in newborn.history.events
    )


def test_trend_growing_shrinking_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series = dict(_BACKGROUND)  # bg deltas ~4-12% per step → band ~ small
    series["up"] = [(100, 3), (200, 3), (430, 3), (760, 3)]
    series["down"] = [(700, 3), (400, 3), (200, 3), (100, 3)]
    series["flat"] = [(300, 3), (301, 3), (300, 3), (302, 3)]
    pfs, telemetry = _run_with_stub(monkeypatch, series, 4)
    by_name = {p.name: p for p in pfs}
    assert by_name["up"].history.impact_trend == "growing"
    assert by_name["down"].history.impact_trend == "shrinking"
    assert by_name["flat"].history.impact_trend == "stable"
    assert telemetry["trends"]["growing"] >= 1
    assert telemetry["trends"]["shrinking"] >= 1
    assert telemetry["trends"]["stable"] >= 1


def test_trend_none_when_single_living_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pfs, _ = _run_with_stub(monkeypatch, {"solo": [(0, 0), (50, 2)]}, 2)
    assert pfs[0].history.impact_trend is None


def test_entities_without_history_or_paths_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commits = _stub_runner(monkeypatch, {"x": [(1, 1)]}, 1)
    no_history = _pf("nohist", ["a.ts"], with_history=False)
    no_paths = _pf("nopaths", [])
    with_history = _pf("x", ["x.ts"])
    telemetry = stage_6_96_impact(
        [no_history, no_paths, with_history], [], [], [],
        commits, Path("/nonexistent"), snapshot_count=1,
    )
    assert telemetry["entities_total"] == 1
    assert no_history.history is None
    assert no_paths.history.impact == []
    assert with_history.history.impact != []


def test_skipped_when_no_entities_or_no_commits() -> None:
    telemetry = stage_6_96_impact(
        [], [], [], [], [_commit("a", 0)], Path("/nonexistent"),
    )
    assert telemetry["skipped"] is True
    pf = _pf("x", ["x.ts"])
    telemetry = stage_6_96_impact(
        [pf], [], [], [], [], Path("/nonexistent"),
    )
    assert telemetry["skipped"] is True
    assert pf.history.impact == []


def test_determinism_two_runs_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series = dict(_BACKGROUND)
    series["ai"] = [(100, 4), (104, 4), (500, 4), (505, 4)]

    def run_once() -> list[dict[str, Any]]:
        pfs, _ = _run_with_stub(monkeypatch, series, 4)
        return [p.history.model_dump() for p in pfs]

    assert run_once() == run_once()


def test_failed_snapshot_missing_from_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commits = [_commit(f"s{i}", i) for i in range(3)]

    def fake_run(
        repo_path: Path, shas: list[str], compute: Any, **kw: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        # Middle snapshot failed → absent from results.
        return (
            {shas[0]: [(5, 1)], shas[2]: [(9, 1)]},
            {
                "impact_snapshots": 2,
                "impact_skipped_snapshots": 1,
                "impact_budget_exceeded": False,
                "budget_sec": 1.0,
                "planned_snapshots": 3,
            },
        )

    monkeypatch.setattr(mod, "run_snapshots", fake_run)
    pf = _pf("x", ["x.ts"])
    telemetry = stage_6_96_impact(
        [pf], [], [], [], commits, Path("/nonexistent"), snapshot_count=3,
    )
    assert [(p.week, p.reach) for p in pf.history.impact] == [
        ("2024-W01", 5), ("2024-W03", 9),
    ]
    assert telemetry["impact_skipped_snapshots"] == 1


def test_all_snapshots_failed_leaves_impact_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commits = [_commit(f"s{i}", i) for i in range(2)]

    def fake_run(
        repo_path: Path, shas: list[str], compute: Any, **kw: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return {}, {
            "impact_snapshots": 0,
            "impact_skipped_snapshots": len(shas),
            "impact_budget_exceeded": False,
            "budget_sec": 1.0,
            "planned_snapshots": len(shas),
        }

    monkeypatch.setattr(mod, "run_snapshots", fake_run)
    pf = _pf("x", ["x.ts"])
    telemetry = stage_6_96_impact(
        [pf], [], [], [], commits, Path("/nonexistent"), snapshot_count=2,
    )
    assert pf.history.impact == []
    assert pf.history.impact_trend is None
    assert telemetry["entities_with_impact"] == 0


def test_user_flows_gain_impact_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from faultline.models.types import Flow

    commits = _stub_runner(monkeypatch, {"uf": [(7, 1)]}, 1)
    flow = Flow(
        name="checkout-flow",
        paths=["src/checkout.ts"],
        authors=["dev"],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
        health_score=100.0,
        uuid="u1",
    )
    uf = UserFlow(
        id="UF-001", name="Checkout", intent="execute", resource="order",
        member_flow_ids=["u1"], history=_history(),
    )
    stage_6_96_impact(
        [], [uf], [flow], [], commits, Path("/nonexistent"),
        snapshot_count=1,
    )
    assert [(p.reach, p.members_present) for p in uf.history.impact] == [
        (7, 1),
    ]


# ── Real-git end-to-end ─────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def test_end_to_end_reach_series_grows_with_importers(
    tmp_path: Path,
) -> None:
    """Real repo: lib/core.ts gains importers over 3 commits → the
    impact series records 1 → 2 external importers and the member's
    pre-birth absence."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")

    def commit_all(msg: str, date: str) -> tuple[str, datetime]:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", msg, "--date", date)
        sha = _git(repo, "rev-parse", "HEAD").strip()
        iso = datetime.fromisoformat(date)
        return sha, iso

    # c0: core doesn't exist yet.
    (repo / "readme.txt").write_text("hello\n")
    sha0, d0 = commit_all("c0", "2024-01-01T12:00:00+00:00")
    # c1: core + one importer.
    (repo / "lib").mkdir()
    (repo / "lib" / "core.ts").write_text("export const x = 1\n")
    (repo / "app.ts").write_text("import { x } from './lib/core'\n")
    sha1, d1 = commit_all("c1", "2024-01-08T12:00:00+00:00")
    # c2: a second importer appears.
    (repo / "admin.ts").write_text("import { x } from './lib/core'\n")
    sha2, d2 = commit_all("c2", "2024-01-15T12:00:00+00:00")

    commits = [
        Commit(sha=s, message="m", author="t", date=d, files_changed=[])
        for s, d in ((sha0, d0), (sha1, d1), (sha2, d2))
    ]
    pf = _pf("core", ["lib/core.ts"])
    telemetry = stage_6_96_impact(
        [pf], [], [], [], commits, repo, snapshot_count=3,
    )
    assert telemetry["impact_snapshots"] == 3
    assert [
        (p.reach, p.members_present) for p in pf.history.impact
    ] == [(0, 0), (1, 1), (2, 1)]
    # No worktrees left behind.
    listing = _git(repo, "worktree", "list", "--porcelain")
    assert listing.count("worktree ") == 1

    # Subpath scan: scan root = <worktree>/lib, members subtree-relative.
    # At c0 the subtree doesn't exist yet → (reach 0, members 0).
    pf_sub = _pf("core", ["core.ts"])
    stage_6_96_impact(
        [pf_sub], [], [], [], commits, repo, subpath="lib",
        snapshot_count=3,
    )
    assert [
        (p.reach, p.members_present) for p in pf_sub.history.impact
    ] == [(0, 0), (0, 1), (0, 1)]  # importers live outside lib/


# ── Old-JSON rehydration (additive schema) ──────────────────────────────


def test_old_json_rehydrates_without_impact_fields() -> None:
    old = {
        "birth_week": "2024-W01",
        "weekly": [],
        "events": [{"kind": "birth", "week": "2024-W01"}],
        "test_efficacy": {"verdict": "insufficient_data"},
        "history_confidence": 0.5,
    }
    history = EntityHistory.model_validate(old)
    assert history.impact == []
    assert history.impact_trend is None


def test_new_event_kinds_round_trip() -> None:
    e = HistoryEvent(kind="coupling_spike", week="2024-W05",
                     detail="reach 100->500")
    assert HistoryEvent.model_validate(e.model_dump()).kind == "coupling_spike"
    p = ImpactPoint(week="2024-W05", reach=3, members_present=2)
    assert ImpactPoint.model_validate(p.model_dump()).reach == 3
