"""Unit tests for Stage 6.95 ``cross_cut_emerged`` timeline events.

Covers: emergence-week computation on crafted commits+flows fixtures
(latest first-touch among shared files; untouched-in-window files →
birth week), the SE-gated correlation note (up / down / no_change /
insufficient_data), the structural per-entity cap, foreignness rules
(own flows + UF member flows excluded), absence when there are no
cross-cutting flows (keyless shape), determinism, old-JSON
rehydration, and one synthetic end-to-end pass through
``stage_6_95_history`` with mocked flows carrying
``secondary_features``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import (
    Commit,
    CrossCutNote,
    EntityHistory,
    Feature,
    FeatureMap,
    Flow,
    HistoryEvent,
    UserFlow,
)
from faultline.pipeline_v2.stage_6_95_history import (
    CROSS_CUT_EVENTS_CAP,
    stage_6_95_history,
)


def _commit(
    sha: str,
    iso_year: int,
    iso_week: int,
    files: list[str],
    *,
    bug: bool = False,
) -> Commit:
    d = datetime.fromisocalendar(iso_year, iso_week, 1).replace(
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


def _feature(name: str, paths: list[str], *, layer: str = "product",
             product_feature_id: str | None = None) -> Feature:
    f = Feature(
        name=name,
        paths=paths,
        authors=["dev"],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2024, 1, 1, tzinfo=timezone.utc),
        health_score=100.0,
        layer=layer,
    )
    f.product_feature_id = product_feature_id
    return f


def _xflow(
    name: str,
    paths: list[str],
    *,
    primary: str,
    secondary: list[str],
    uuid: str = "",
) -> Flow:
    """A flow as Stage 5.5 would emit it: bipartite fields populated."""
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
        id=f"{primary}::{name}",
        primary_feature=primary,
        secondary_features=secondary,
        shared_with_features_count=len(secondary),
        cross_cutting=bool(secondary),
    )


def _cross_cut_events(entity: Feature | UserFlow) -> list[HistoryEvent]:
    assert entity.history is not None
    return [e for e in entity.history.events if e.kind == "cross_cut_emerged"]


def _pf_with_foreign_flow(
    commits: list[Commit],
    *,
    flow_paths: list[str] | None = None,
) -> tuple[Feature, list[Feature], list[Flow]]:
    """PF "billing" + foreign cross-cutting flow from PF "auth"."""
    pf = _feature("billing", ["src/billing/a.ts", "src/billing/b.ts"])
    dev_billing = _feature(
        "billing-dev", ["src/billing/a.ts", "src/billing/b.ts"],
        layer="developer", product_feature_id="billing",
    )
    dev_auth = _feature(
        "auth-dev", ["src/auth/check.ts"],
        layer="developer", product_feature_id="auth",
    )
    flow = _xflow(
        "session-check",
        flow_paths or ["src/auth/check.ts", "src/billing/b.ts"],
        primary="auth-dev",
        secondary=["billing-dev"],
    )
    return pf, [dev_billing, dev_auth], [flow]


# ── Emergence week ──────────────────────────────────────────────────────


def test_emergence_week_is_latest_first_touch_of_shared_files() -> None:
    commits = [
        _commit("a", 2024, 1, ["src/billing/a.ts"]),
        _commit("b", 2024, 5, ["src/billing/b.ts"]),  # shared file born
        _commit("c", 2024, 8, ["src/billing/b.ts"], bug=True),
    ]
    pf, devs, flows = _pf_with_foreign_flow(commits)
    stage_6_95_history([pf], [], flows, devs, commits)
    events = _cross_cut_events(pf)
    assert len(events) == 1
    assert events[0].week == "2024-W05"
    assert events[0].detail is not None
    assert "session-check" in events[0].detail
    assert "began sharing 1 file(s) with this feature" in events[0].detail


def test_emergence_week_multi_shared_files_takes_the_max() -> None:
    """Shared set {a.ts, b.ts}: a.ts born W01, b.ts born W05 → the
    shared file-set first fully exists at W05."""
    commits = [
        _commit("a", 2024, 1, ["src/billing/a.ts"]),
        _commit("b", 2024, 5, ["src/billing/b.ts"]),
    ]
    pf, devs, flows = _pf_with_foreign_flow(
        commits,
        flow_paths=["src/auth/check.ts", "src/billing/a.ts", "src/billing/b.ts"],
    )
    stage_6_95_history([pf], [], flows, devs, commits)
    events = _cross_cut_events(pf)
    assert len(events) == 1
    assert events[0].week == "2024-W05"


def test_shared_file_untouched_in_window_resolves_to_birth_week() -> None:
    """b.ts exists in HEAD member sets but is never touched in the scan
    window → it predates the window → emergence = birth week
    (history_confidence convention)."""
    commits = [
        _commit("a", 2024, 3, ["src/billing/a.ts"]),
        _commit("c", 2024, 9, ["src/billing/a.ts"]),
    ]
    pf, devs, flows = _pf_with_foreign_flow(commits)
    stage_6_95_history([pf], [], flows, devs, commits)
    events = _cross_cut_events(pf)
    assert len(events) == 1
    assert events[0].week == "2024-W03"  # birth week


# ── Correlation note (SE gate) ──────────────────────────────────────────


def test_note_bugfix_share_up_after_emergence() -> None:
    commits = [
        _commit(f"pre{i}", 2024, 1 + i % 3, ["src/billing/a.ts"])
        for i in range(10)
    ]
    commits.append(_commit("born", 2024, 10, ["src/billing/b.ts"]))
    commits += [
        _commit(f"post{i}", 2024, 11 + i % 3, ["src/billing/a.ts"], bug=True)
        for i in range(9)
    ]
    pf, devs, flows = _pf_with_foreign_flow(commits)
    stage_6_95_history([pf], [], flows, devs, commits)
    (event,) = _cross_cut_events(pf)
    note = event.correlation_note
    assert note is not None
    assert note.verdict == "bugfix_share_up"
    assert note.bugfix_share_before == 0.0
    assert note.bugfix_share_after == 0.9
    assert note.commits_before == 10
    assert note.commits_after == 10  # emergence week inclusive in AFTER


def test_note_bugfix_share_down_after_emergence() -> None:
    commits = [
        _commit(f"pre{i}", 2024, 1 + i % 3, ["src/billing/a.ts"], bug=True)
        for i in range(10)
    ]
    commits.append(_commit("born", 2024, 10, ["src/billing/b.ts"]))
    commits += [
        _commit(f"post{i}", 2024, 11 + i % 3, ["src/billing/a.ts"])
        for i in range(9)
    ]
    pf, devs, flows = _pf_with_foreign_flow(commits)
    stage_6_95_history([pf], [], flows, devs, commits)
    (event,) = _cross_cut_events(pf)
    assert event.correlation_note is not None
    assert event.correlation_note.verdict == "bugfix_share_down"


def test_note_no_change_within_standard_error() -> None:
    commits = [
        _commit(f"pre{i}", 2024, 1, ["src/billing/a.ts"], bug=i % 2 == 0)
        for i in range(4)
    ]
    commits.append(_commit("born", 2024, 5, ["src/billing/b.ts"]))
    commits += [
        _commit(f"post{i}", 2024, 6, ["src/billing/a.ts"], bug=i % 2 == 0)
        for i in range(4)
    ]
    pf, devs, flows = _pf_with_foreign_flow(commits)
    stage_6_95_history([pf], [], flows, devs, commits)
    (event,) = _cross_cut_events(pf)
    assert event.correlation_note is not None
    assert event.correlation_note.verdict == "no_change"


def test_note_insufficient_data_when_emergence_is_birth_week() -> None:
    """Emergence at the birth week → BEFORE window empty → no verdict,
    same insufficient_data discipline as test_efficacy."""
    commits = [
        _commit("a", 2024, 1, ["src/billing/a.ts", "src/billing/b.ts"]),
        _commit("c", 2024, 4, ["src/billing/a.ts"], bug=True),
    ]
    pf, devs, flows = _pf_with_foreign_flow(commits)
    telemetry = stage_6_95_history([pf], [], flows, devs, commits)
    (event,) = _cross_cut_events(pf)
    note = event.correlation_note
    assert note is not None
    assert note.verdict == "insufficient_data"
    assert note.reason is not None and "empty window" in note.reason
    assert note.bugfix_share_before is None
    assert telemetry["cross_cut_note_verdicts"]["insufficient_data"] == 1


# ── Cap ─────────────────────────────────────────────────────────────────


def test_cap_keeps_top_n_by_shared_file_count() -> None:
    n_flows = CROSS_CUT_EVENTS_CAP + 2
    paths = [f"src/billing/f{i}.ts" for i in range(n_flows)]
    pf = _feature("billing", paths)
    dev_billing = _feature(
        "billing-dev", paths, layer="developer", product_feature_id="billing",
    )
    dev_other = _feature(
        "other-dev", ["src/other/x.ts"],
        layer="developer", product_feature_id="other",
    )
    # flow i shares (i+1) files with the entity → flow 0 shares the
    # least and is ranked last.
    flows = [
        _xflow(
            f"xcut-{i:02d}", ["src/other/x.ts"] + paths[: i + 1],
            primary="other-dev", secondary=["billing-dev"],
        )
        for i in range(n_flows)
    ]
    commits = [_commit(f"c{i}", 2024, 1 + i, [p]) for i, p in enumerate(paths)]
    telemetry = stage_6_95_history(
        [pf], [], flows, [dev_billing, dev_other], commits,
    )
    events = _cross_cut_events(pf)
    assert len(events) == CROSS_CUT_EVENTS_CAP
    assert telemetry["cross_cut_events_emitted"] == CROSS_CUT_EVENTS_CAP
    assert telemetry["cross_cut_capped_out"] == 2
    assert telemetry["cross_cut_entities_affected"] == 1
    # The two flows sharing the fewest files were the ones dropped.
    kept_names = {e.detail.split("'")[1] for e in events if e.detail}
    assert kept_names == {f"xcut-{i:02d}" for i in range(2, n_flows)}


# ── Foreignness rules ───────────────────────────────────────────────────


def test_own_flow_of_the_product_feature_is_not_an_event() -> None:
    """A cross-cutting flow whose PRIMARY dev feature belongs to this PF
    cross-cuts OUT of it, not into it — no event on the owner."""
    commits = [_commit("a", 2024, 1, ["src/billing/a.ts"])]
    pf = _feature("billing", ["src/billing/a.ts"])
    dev = _feature(
        "billing-dev", ["src/billing/a.ts"],
        layer="developer", product_feature_id="billing",
    )
    own_flow = _xflow(
        "charge", ["src/billing/a.ts"],
        primary="billing-dev", secondary=["auth-dev"],
    )
    telemetry = stage_6_95_history([pf], [], [own_flow], [dev], commits)
    assert _cross_cut_events(pf) == []
    assert telemetry["cross_cut_events_emitted"] == 0


def test_user_flow_member_flow_is_not_an_event_but_foreign_flow_is() -> None:
    commits = [
        _commit("a", 2024, 1, ["src/checkout/a.ts"]),
        _commit("b", 2024, 6, ["src/checkout/a.ts"]),
    ]
    member = _xflow(
        "checkout-flow", ["src/checkout/a.ts"],
        primary="checkout-dev", secondary=["audit-dev"], uuid="uuid-member",
    )
    foreign = _xflow(
        "audit-log", ["src/audit/log.ts", "src/checkout/a.ts"],
        primary="audit-dev", secondary=["checkout-dev"], uuid="uuid-foreign",
    )
    uf = UserFlow(
        id="UF-001", name="checkout", intent="execute",
        resource="order", member_flow_ids=["uuid-member"],
    )
    stage_6_95_history([], [uf], [member, foreign], [], commits)
    events = _cross_cut_events(uf)
    assert len(events) == 1
    assert events[0].detail is not None and "audit-log" in events[0].detail
    assert "with this user flow" in events[0].detail


# ── Absence (keyless shape) ─────────────────────────────────────────────


def test_no_flows_no_events_no_crash() -> None:
    """Keyless scans have no Stage 3 flows at all — the feature is
    simply absent: histories still computed, zero cross_cut events."""
    commits = [
        _commit("a", 2024, 1, ["src/billing/a.ts"]),
        _commit("b", 2024, 2, ["src/billing/a.ts"], bug=True),
    ]
    pf = _feature("billing", ["src/billing/a.ts"])
    telemetry = stage_6_95_history([pf], [], [], [], commits)
    assert pf.history is not None
    assert _cross_cut_events(pf) == []
    assert telemetry["cross_cut_events_emitted"] == 0
    assert telemetry["cross_cut_entities_affected"] == 0
    assert telemetry["cross_cut_capped_out"] == 0


def test_non_cross_cutting_flow_emits_nothing() -> None:
    commits = [_commit("a", 2024, 1, ["src/billing/a.ts"])]
    pf = _feature("billing", ["src/billing/a.ts"])
    dev_other = _feature(
        "other-dev", ["src/other/x.ts"],
        layer="developer", product_feature_id="other",
    )
    plain = _xflow(
        "plain", ["src/billing/a.ts"], primary="other-dev", secondary=[],
    )
    assert plain.cross_cutting is False
    stage_6_95_history([pf], [], [plain], [dev_other], commits)
    assert _cross_cut_events(pf) == []


# ── Determinism ─────────────────────────────────────────────────────────


def test_determinism_two_runs_identical_dumps() -> None:
    def build() -> tuple[Feature, list[Feature], list[Flow], list[Commit]]:
        commits = [
            _commit(f"c{i}", 2024, 1 + i % 12,
                    [f"src/billing/{'a' if i % 2 else 'b'}.ts"],
                    bug=i % 3 == 0)
            for i in range(40)
        ]
        return (*_pf_with_foreign_flow(commits), commits)

    pf1, devs1, flows1, commits1 = build()
    pf2, devs2, flows2, commits2 = build()
    t1 = stage_6_95_history([pf1], [], flows1, devs1, commits1)
    t2 = stage_6_95_history([pf2], [], flows2, devs2, commits2)
    t1.pop("elapsed_sec")
    t2.pop("elapsed_sec")
    assert t1 == t2
    assert pf1.history is not None and pf2.history is not None
    assert pf1.history.model_dump() == pf2.history.model_dump()
    assert _cross_cut_events(pf1)  # the fixture does produce events


# ── Old-JSON rehydration ────────────────────────────────────────────────


def test_old_event_without_note_rehydrates() -> None:
    raw = {"kind": "birth", "week": "2024-W01"}
    ev = HistoryEvent.model_validate(raw)
    assert ev.correlation_note is None


def test_cross_cut_event_round_trips() -> None:
    ev = HistoryEvent(
        kind="cross_cut_emerged",
        week="2024-W05",
        detail="flow 'x' began sharing 2 file(s) with this feature",
        correlation_note=CrossCutNote(
            verdict="no_change",
            bugfix_share_before=0.2,
            bugfix_share_after=0.25,
            commits_before=10,
            commits_after=12,
        ),
    )
    again = HistoryEvent.model_validate(ev.model_dump())
    assert again == ev
    assert again.correlation_note is not None
    assert again.correlation_note.verdict == "no_change"


def test_full_map_with_cross_cut_event_round_trips() -> None:
    history = EntityHistory(
        birth_week="2024-W01",
        test_efficacy={"verdict": "insufficient_data"},  # type: ignore[arg-type]
        events=[
            HistoryEvent(kind="birth", week="2024-W01"),
            HistoryEvent(
                kind="cross_cut_emerged", week="2024-W03",
                detail="flow 'y' began sharing 1 file(s) with this feature",
                correlation_note=CrossCutNote(verdict="insufficient_data"),
            ),
        ],
    )
    pf = _feature("billing", ["src/billing/a.ts"])
    pf.history = history
    fm = FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        total_commits=1,
        date_range_days=365,
        features=[pf],
    )
    again = FeatureMap.model_validate(fm.model_dump())
    kinds = [e.kind for e in again.features[0].history.events]  # type: ignore[union-attr]
    assert "cross_cut_emerged" in kinds


# ── Synthetic end-to-end through stage 6.95 ─────────────────────────────


def test_end_to_end_mocked_flows_with_secondary_features() -> None:
    """ONE synthetic end-to-end pass: PF + UF entities, mocked flows
    carrying ``secondary_features`` (as Stage 5.5 would populate them),
    a crafted commit history — a ``cross_cut_emerged`` event lands in
    BOTH EntityHistory objects at the week the shared file was first
    touched, with an SE-gated note attached."""
    # billing PF (a.ts + b.ts) and a checkout UF over checkout-flow.
    pf = _feature("billing", ["src/billing/a.ts", "src/billing/b.ts"])
    dev_billing = _feature(
        "billing-dev", ["src/billing/a.ts", "src/billing/b.ts"],
        layer="developer", product_feature_id="billing",
    )
    dev_export = _feature(
        "export-dev", ["src/export/pdf.ts"],
        layer="developer", product_feature_id="export",
    )
    member = _xflow(
        "checkout-flow", ["src/billing/a.ts"],
        primary="billing-dev", secondary=[], uuid="uuid-checkout",
    )
    export_pdf = _xflow(
        "export-pdf", ["src/export/pdf.ts", "src/billing/b.ts",
                       "src/billing/a.ts"],
        primary="export-dev", secondary=["billing-dev"], uuid="uuid-export",
    )
    uf = UserFlow(
        id="UF-001", name="checkout", intent="execute",
        resource="order", member_flow_ids=["uuid-checkout"],
    )
    commits = [
        _commit("a1", 2024, 1, ["src/billing/a.ts"]),
        _commit("a2", 2024, 2, ["src/billing/a.ts"]),
        _commit("b1", 2024, 7, ["src/billing/b.ts"]),  # b.ts born → PF emergence
        _commit("a3", 2024, 9, ["src/billing/a.ts"], bug=True),
    ]
    telemetry = stage_6_95_history(
        [pf], [uf], [member, export_pdf], [dev_billing, dev_export], commits,
    )

    # PF: shared set {a.ts, b.ts} → latest first-touch = W07.
    (pf_event,) = _cross_cut_events(pf)
    assert pf_event.week == "2024-W07"
    assert pf_event.detail is not None and "export-pdf" in pf_event.detail
    assert pf_event.correlation_note is not None
    assert pf_event.correlation_note.verdict in (
        "bugfix_share_up", "bugfix_share_down", "no_change",
        "insufficient_data",
    )

    # UF: source set = member flow paths {a.ts}; export-pdf is foreign
    # and shares a.ts → emergence at the UF's birth (a.ts first touch).
    (uf_event,) = _cross_cut_events(uf)
    assert uf_event.week == "2024-W01"

    assert telemetry["cross_cut_events_emitted"] == 2
    assert telemetry["cross_cut_entities_affected"] == 2
