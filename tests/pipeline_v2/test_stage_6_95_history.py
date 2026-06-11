"""Unit tests for Stage 6.95 — per-entity git-history timeline.

Covers: ISO-week bucketing on a synthetic commit list, sparse-week
omission, events (birth / first_test / test_wave / hotspot_emerged) on
crafted fixtures, the test-efficacy verdict incl. the median activity
gate, history_confidence, determinism, and old-JSON rehydration.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import (
    Commit,
    EntityHistory,
    Feature,
    FeatureMap,
    Flow,
    UserFlow,
)
from faultline.pipeline_v2.stage_6_95_history import (
    HOTSPOT_COMMITS_MIN,
    compute_entity_history,
    stage_6_95_history,
)


def _commit(
    sha: str,
    iso_year: int,
    iso_week: int,
    files: list[str],
    *,
    bug: bool = False,
    weekday: int = 1,
) -> Commit:
    d = datetime.fromisocalendar(iso_year, iso_week, weekday).replace(
        tzinfo=timezone.utc,
    )
    return Commit(
        sha=sha,
        message="fix: x" if bug else "feat: x",
        author="dev",
        date=d,
        files_changed=files,
        is_bug_fix=bug,
    )


def _feature(name: str, paths: list[str], **kw) -> Feature:
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
        **kw,
    )


def _flow(name: str, paths: list[str], *, uuid: str = "",
          test_files: list[str] | None = None) -> Flow:
    return Flow(
        name=name,
        paths=paths,
        authors=["dev"],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
        health_score=100.0,
        uuid=uuid,
        test_files=list(test_files or []),
    )


# ── Bucketing ───────────────────────────────────────────────────────────


def test_weekly_bucketing_and_sparse_omission() -> None:
    commits = [
        _commit("a", 2024, 1, ["src/billing/a.ts"]),
        _commit("b", 2024, 1, ["src/billing/a.ts"], bug=True),
        # week 2..4 silent
        _commit("c", 2024, 5, ["src/billing/b.ts"]),
        # untracked file — never attributed
        _commit("d", 2024, 6, ["src/other/x.ts"]),
    ]
    h = compute_entity_history(
        ["src/billing/a.ts", "src/billing/b.ts"], [], commits,
    )
    assert h is not None
    assert h.birth_week == "2024-W01"
    assert [p.week for p in h.weekly] == ["2024-W01", "2024-W05"]
    w1, w5 = h.weekly
    assert (w1.commits, w1.bug_fixes, w1.test_commits) == (2, 1, 0)
    assert w1.bugfix_share == 0.5
    assert w1.files_touched == 1
    assert (w5.commits, w5.bug_fixes) == (1, 0)


def test_deleted_file_attributed_via_parent_dir_fallback() -> None:
    commits = [
        _commit("a", 2024, 1, ["src/billing/deleted-long-ago.ts"]),
    ]
    h = compute_entity_history(["src/billing/a.ts"], [], commits)
    assert h is not None
    assert h.weekly[0].commits == 1


def test_no_attributed_commits_yields_none() -> None:
    commits = [_commit("a", 2024, 1, ["src/other/x.ts"])]
    assert compute_entity_history(["src/billing/a.ts"], [], commits) is None


# ── Events ──────────────────────────────────────────────────────────────


def test_birth_and_first_test_events() -> None:
    commits = [
        _commit("a", 2024, 1, ["src/billing/a.ts"]),
        _commit("b", 2024, 3, ["src/billing/a.test.ts"]),
    ]
    h = compute_entity_history(
        ["src/billing/a.ts"], ["src/billing/a.test.ts"], commits,
    )
    assert h is not None
    kinds = {e.kind: e.week for e in h.events}
    assert kinds["birth"] == "2024-W01"
    assert kinds["first_test"] == "2024-W03"


def test_test_commit_via_directory_neighbourhood() -> None:
    """A HISTORICAL test file (deleted from HEAD, so absent from the
    mapper's test_files) is still attributed via the dir rule:
    src/billing/__tests__/* maps to the src/billing member dir."""
    commits = [
        _commit("a", 2024, 1, ["src/billing/a.ts"]),
        _commit("b", 2024, 2, ["src/billing/__tests__/old.test.ts"]),
    ]
    h = compute_entity_history(["src/billing/a.ts"], [], commits)
    assert h is not None
    assert any(e.kind == "first_test" and e.week == "2024-W02" for e in h.events)


def test_root_level_test_tree_not_attributed() -> None:
    """tests/ at repo root does not mirror the member dir — deliberately
    not attributed (cross-entity bleed is worse)."""
    commits = [
        _commit("a", 2024, 1, ["src/billing/a.ts"]),
        _commit("b", 2024, 2, ["tests/unrelated.test.ts"]),
    ]
    h = compute_entity_history(["src/billing/a.ts"], [], commits)
    assert h is not None
    assert not any(e.kind == "first_test" for e in h.events)
    assert all(p.test_commits == 0 for p in h.weekly)


def test_test_wave_exceeds_own_p75() -> None:
    # nonzero weekly test_commits: 1,1,1,1,5 → P75 = 1 → only the
    # 5-test week strictly exceeds it.
    commits = []
    for wk in (1, 2, 3, 4):
        commits.append(
            _commit(f"t{wk}", 2024, wk,
                    ["src/billing/a.ts", "src/billing/a.test.ts"]),
        )
    for i in range(5):
        commits.append(
            _commit(f"wave{i}", 2024, 10,
                    ["src/billing/a.ts", "src/billing/a.test.ts"]),
        )
    h = compute_entity_history(
        ["src/billing/a.ts"], ["src/billing/a.test.ts"], commits,
    )
    assert h is not None
    waves = [e for e in h.events if e.kind == "test_wave"]
    assert [e.week for e in waves] == ["2024-W10"]
    assert waves[0].detail == "test_commits=5"


def test_test_wave_uniform_series_has_no_wave() -> None:
    commits = [
        _commit(f"t{wk}", 2024, wk,
                ["src/billing/a.ts", "src/billing/a.test.ts"])
        for wk in (1, 2, 3)
    ]
    h = compute_entity_history(
        ["src/billing/a.ts"], ["src/billing/a.test.ts"], commits,
    )
    assert h is not None
    assert not any(e.kind == "test_wave" for e in h.events)


def test_contiguous_wave_weeks_collapse_to_one_event() -> None:
    commits = []
    for wk in (1, 2, 3, 4):
        commits.append(
            _commit(f"t{wk}", 2024, wk,
                    ["src/billing/a.ts", "src/billing/a.test.ts"]),
        )
    # two CONSECUTIVE burst weeks → one event at the run start
    for wk in (10, 11):
        for i in range(4):
            commits.append(
                _commit(f"w{wk}-{i}", 2024, wk,
                        ["src/billing/a.ts", "src/billing/a.test.ts"]),
            )
    h = compute_entity_history(
        ["src/billing/a.ts"], ["src/billing/a.test.ts"], commits,
    )
    assert h is not None
    waves = [e for e in h.events if e.kind == "test_wave"]
    assert [e.week for e in waves] == ["2024-W10"]


def test_hotspot_emerged_first_crossing_week() -> None:
    # one file, 5 commits of which 3 bug fixes → ratio 0.6 ≥ 0.40 at
    # the 5th commit (week 5), where total first reaches the minimum.
    commits = [
        _commit("a", 2024, 1, ["src/billing/hot.ts"]),
        _commit("b", 2024, 2, ["src/billing/hot.ts"], bug=True),
        _commit("c", 2024, 3, ["src/billing/hot.ts"], bug=True),
        _commit("d", 2024, 4, ["src/billing/hot.ts"], bug=True),
        _commit("e", 2024, 5, ["src/billing/hot.ts"]),
    ]
    assert HOTSPOT_COMMITS_MIN == 5  # guard: thresholds reused from Stage 6
    h = compute_entity_history(["src/billing/hot.ts"], [], commits)
    assert h is not None
    hs = [e for e in h.events if e.kind == "hotspot_emerged"]
    assert len(hs) == 1
    assert hs[0].week == "2024-W05"
    assert hs[0].detail == "src/billing/hot.ts"


def test_no_hotspot_below_min_commits() -> None:
    commits = [
        _commit(f"c{i}", 2024, i + 1, ["src/billing/hot.ts"], bug=True)
        for i in range(HOTSPOT_COMMITS_MIN - 1)
    ]
    h = compute_entity_history(["src/billing/hot.ts"], [], commits)
    assert h is not None
    assert not any(e.kind == "hotspot_emerged" for e in h.events)


# ── test_efficacy ───────────────────────────────────────────────────────


def test_efficacy_improved() -> None:
    commits = []
    # before first_test: 8 commits, 6 bug fixes (share 0.75)
    for i in range(8):
        commits.append(
            _commit(f"b{i}", 2024, 1 + i % 3, ["src/billing/a.ts"], bug=i < 6),
        )
    # first test at week 10
    commits.append(
        _commit("t", 2024, 10, ["src/billing/a.ts", "src/billing/a.test.ts"]),
    )
    # after: 9 more commits, 1 bug fix
    for i in range(9):
        commits.append(
            _commit(f"a{i}", 2024, 11 + i % 3, ["src/billing/a.ts"], bug=i == 0),
        )
    h = compute_entity_history(
        ["src/billing/a.ts"], ["src/billing/a.test.ts"], commits,
    )
    assert h is not None
    eff = h.test_efficacy
    assert eff.verdict == "improved"
    assert eff.pivot_week == "2024-W10"
    assert eff.bugfix_share_before == 0.75
    assert eff.commits_before == 8
    assert eff.commits_after == 10  # pivot week included in AFTER
    assert eff.bugfix_share_after == 0.1


def test_efficacy_worsened() -> None:
    commits = []
    for i in range(10):
        commits.append(
            _commit(f"b{i}", 2024, 1 + i % 3, ["src/billing/a.ts"], bug=False),
        )
    commits.append(
        _commit("t", 2024, 10, ["src/billing/a.ts", "src/billing/a.test.ts"]),
    )
    for i in range(9):
        commits.append(
            _commit(f"a{i}", 2024, 11 + i % 3, ["src/billing/a.ts"], bug=True),
        )
    h = compute_entity_history(
        ["src/billing/a.ts"], ["src/billing/a.test.ts"], commits,
    )
    assert h is not None
    assert h.test_efficacy.verdict == "worsened"


def test_efficacy_no_change_within_standard_error() -> None:
    commits = []
    for i in range(4):
        commits.append(
            _commit(f"b{i}", 2024, 1, ["src/billing/a.ts"], bug=i % 2 == 0),
        )
    commits.append(
        _commit("t", 2024, 5, ["src/billing/a.ts", "src/billing/a.test.ts"]),
    )
    for i in range(3):
        commits.append(
            _commit(f"a{i}", 2024, 6, ["src/billing/a.ts"], bug=i % 2 == 0),
        )
    h = compute_entity_history(
        ["src/billing/a.ts"], ["src/billing/a.test.ts"], commits,
    )
    assert h is not None
    assert h.test_efficacy.verdict == "no_change"


def test_efficacy_insufficient_without_tests() -> None:
    commits = [_commit("a", 2024, 1, ["src/billing/a.ts"])]
    h = compute_entity_history(["src/billing/a.ts"], [], commits)
    assert h is not None
    eff = h.test_efficacy
    assert eff.verdict == "insufficient_data"
    assert eff.reason is not None and "no test commit" in eff.reason


def test_efficacy_insufficient_empty_before_window() -> None:
    # tests arrive in the birth week → before window empty
    commits = [
        _commit("t", 2024, 1, ["src/billing/a.ts", "src/billing/a.test.ts"]),
        _commit("a", 2024, 2, ["src/billing/a.ts"]),
    ]
    h = compute_entity_history(
        ["src/billing/a.ts"], ["src/billing/a.test.ts"], commits,
    )
    assert h is not None
    eff = h.test_efficacy
    assert eff.verdict == "insufficient_data"
    assert eff.reason is not None and "empty window" in eff.reason


def test_activity_gate_below_median_is_insufficient() -> None:
    """The full stage gates entities below the median total-commit count
    of their kind: 3 PFs with totals 1 / 20 / 21 → median 20 → the
    1-commit PF is gated even though it has a clean before/after split."""
    pf_small = _feature("small", ["src/small/a.ts"])
    pf_mid = _feature("mid", ["src/mid/a.ts"])
    pf_big = _feature("big", ["src/big/a.ts"])
    commits = [
        _commit("s1", 2024, 1, ["src/small/a.ts"]),
    ]
    for i in range(20):
        commits.append(_commit(f"m{i}", 2024, 1 + i % 9, ["src/mid/a.ts"]))
    for i in range(21):
        commits.append(_commit(f"g{i}", 2024, 1 + i % 9, ["src/big/a.ts"]))
    telemetry = stage_6_95_history(
        [pf_small, pf_mid, pf_big], [], [], [], commits,
    )
    assert pf_small.history is not None
    assert pf_small.history.test_efficacy.verdict == "insufficient_data"
    assert pf_small.history.test_efficacy.reason is not None
    assert "median" in pf_small.history.test_efficacy.reason
    assert telemetry["product_features_gated"] == 1
    assert telemetry["product_features_scored"] == 3
    # mid + big are NOT gated (>= median) — their verdicts come from data
    assert pf_mid.history is not None
    assert "median" not in (pf_mid.history.test_efficacy.reason or "")


# ── history_confidence ──────────────────────────────────────────────────


def test_history_confidence_late_files_lower_share() -> None:
    # a.ts touched at birth (week 1); late.ts first touched at week 40
    # of a 1..41 span → cutoff = 1 + ceil(40*0.25) = week 11 → 1/2 files.
    commits = [
        _commit("a", 2024, 1, ["src/billing/a.ts"]),
        _commit("b", 2024, 41, ["src/billing/late.ts"]),
    ]
    h = compute_entity_history(
        ["src/billing/a.ts", "src/billing/late.ts"], [], commits,
    )
    assert h is not None
    assert h.history_confidence == 0.5


def test_history_confidence_untouched_head_files_count_as_existing() -> None:
    commits = [
        _commit("a", 2024, 1, ["src/billing/a.ts"]),
        _commit("b", 2024, 41, ["src/billing/a.ts"]),
    ]
    h = compute_entity_history(
        ["src/billing/a.ts", "src/billing/pre-window.ts"], [], commits,
    )
    assert h is not None
    assert h.history_confidence == 1.0


# ── Full-stage wiring: PF + UF resolution ───────────────────────────────


def test_stage_resolves_uf_members_and_pf_test_files() -> None:
    flow_a = _flow(
        "checkout-flow", ["src/billing/a.ts"],
        uuid="uuid-a", test_files=["src/billing/a.test.ts"],
    )
    dev = _feature("billing-dev", ["src/billing/a.ts"])
    dev.layer = "developer"
    dev.product_feature_id = "billing"
    dev.flows = [flow_a]
    pf = _feature("billing", ["src/billing/a.ts"])
    uf = UserFlow(
        id="UF-001", name="checkout", intent="execute",
        resource="order", member_flow_ids=["uuid-a"],
    )
    commits = [
        _commit("a", 2024, 1, ["src/billing/a.ts"], bug=True),
        _commit("t", 2024, 3, ["src/billing/a.test.ts"]),
    ]
    telemetry = stage_6_95_history([pf], [uf], [flow_a], [dev], commits)
    assert telemetry["product_features_scored"] == 1
    assert telemetry["user_flows_scored"] == 1
    for entity in (pf, uf):
        assert entity.history is not None
        assert entity.history.birth_week == "2024-W01"
        assert any(e.kind == "first_test" for e in entity.history.events)


def test_stage_handles_empty_inputs() -> None:
    telemetry = stage_6_95_history([], [], [], [], [])
    assert telemetry["product_features_scored"] == 0
    assert telemetry["user_flows_scored"] == 0


# ── Determinism ─────────────────────────────────────────────────────────


def test_determinism_same_input_identical_output() -> None:
    def build() -> tuple[list[Feature], list[Commit]]:
        pfs = [
            _feature("billing", ["src/billing/a.ts", "src/billing/b.ts"]),
            _feature("auth", ["src/auth/a.ts"]),
        ]
        commits = [
            _commit(f"c{i}", 2024, 1 + i % 20,
                    [f"src/{'billing' if i % 2 else 'auth'}/a.ts"],
                    bug=i % 3 == 0)
            for i in range(60)
        ] + [
            _commit("t", 2024, 9,
                    ["src/billing/a.ts", "src/billing/__tests__/a.test.ts"]),
        ]
        return pfs, commits

    pfs1, commits1 = build()
    pfs2, commits2 = build()
    t1 = stage_6_95_history(pfs1, [], [], [], commits1)
    t2 = stage_6_95_history(pfs2, [], [], [], commits2)
    t1.pop("elapsed_sec")
    t2.pop("elapsed_sec")
    assert t1 == t2
    for a, b in zip(pfs1, pfs2):
        assert a.history is not None and b.history is not None
        assert a.history.model_dump() == b.history.model_dump()


# ── Old-JSON rehydration ────────────────────────────────────────────────


def test_old_json_rehydrates_with_history_none() -> None:
    raw = {
        "repo_path": "/tmp/x",
        "analyzed_at": "2024-01-01T00:00:00Z",
        "total_commits": 1,
        "date_range_days": 365,
        "features": [
            {
                "name": "billing",
                "paths": ["src/billing/a.ts"],
                "authors": ["dev"],
                "total_commits": 1,
                "bug_fixes": 0,
                "bug_fix_ratio": 0.0,
                "last_modified": "2024-01-01T00:00:00Z",
                "health_score": 100.0,
                "layer": "product",
            },
        ],
        "user_flows": [
            {
                "id": "UF-001",
                "name": "checkout",
                "intent": "execute",
                "resource": "order",
            },
        ],
    }
    fm = FeatureMap.model_validate(raw)
    assert fm.features[0].history is None
    assert fm.user_flows[0].history is None
    # and a populated history round-trips
    fm.features[0].history = EntityHistory(
        birth_week="2024-W01",
        test_efficacy={"verdict": "insufficient_data"},  # type: ignore[arg-type]
    )
    dumped = fm.model_dump()
    again = FeatureMap.model_validate(dumped)
    assert again.features[0].history is not None
    assert again.features[0].history.birth_week == "2024-W01"


# ── Edge-branch coverage: helper internals ──────────────────────────────


class TestHelperEdgeBranches:
    """Pin the rare branches not reachable through the happy-path
    fixtures above: zero-activity health, missing UF member flows,
    non-test historical paths, and empty member sets."""

    def test_health_lite_zero_commits_is_perfect(self) -> None:
        from faultline.pipeline_v2.stage_6_95_history import _health_lite

        assert _health_lite(0, 0) == 100.0
        # Negative guard (defensive — bucketing never produces it).
        assert _health_lite(-1, 0) == 100.0

    def test_health_lite_monotone_in_bug_share(self) -> None:
        from faultline.pipeline_v2.stage_6_95_history import _health_lite

        healthy = _health_lite(40, 0)
        buggy = _health_lite(40, 30)
        assert healthy > buggy

    def test_user_flow_with_unknown_member_flow_id_is_skipped(self) -> None:
        """A UF pointing at a flow id absent from the map must not crash
        and contributes no paths from the dangling member."""
        commits = [
            _commit("a1", 2026, 10, ["src/real/file.py"], bug=False),
        ]
        flow = _flow("real-flow", ["src/real/file.py"], uuid="uuid-real")
        uf = UserFlow(
            id="uf-1", name="checkout", intent="execute",
            resource="order",
            member_flow_ids=["missing-flow", "uuid-real"],
        )
        stage_6_95_history([], [uf], [flow], [], commits)
        # The UF resolves only the existing member; no exception raised.
        assert uf.history is not None
        assert uf.history.weekly[0].commits == 1

    def test_is_local_test_rejects_non_test_and_foreign_test_paths(self) -> None:
        from faultline.pipeline_v2.stage_6_95_history import _EntityAccumulator

        acc = _EntityAccumulator(
            0, "product_feature", object(),
            source_paths=["src/billing/charge.py"],
            test_files=["tests/billing/test_charge.py"],
        )
        # Exact member of the declared test set.
        assert acc.is_local_test("tests/billing/test_charge.py") is True
        # Source file: not a test path at all → early False branch.
        assert acc.is_local_test("src/billing/charge.py") is False
        # A test path whose stripped dir does NOT descend from any
        # member dir → foreign suite, not attributed.
        assert acc.is_local_test("tests/unrelated/test_other.py") is False
        # A test path that mirrors the member dir → attributed via the
        # ancestor-chain rule.
        assert acc.is_local_test("src/billing/tests/test_more.py") is True

    def test_history_confidence_empty_source_set_is_zero(self) -> None:
        from faultline.pipeline_v2.stage_6_95_history import (
            _EntityAccumulator,
            _history_confidence,
        )

        acc = _EntityAccumulator(
            0, "product_feature", object(), source_paths=[], test_files=[],
        )
        assert _history_confidence(acc, [0, 4]) == 0.0
