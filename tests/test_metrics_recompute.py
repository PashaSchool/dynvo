"""B76 — metrics recompute-on-emission units (``metrics_recompute``).

Named gates (spec docs/anchor-arc/fixb76-metrics-recompute-spec.md):

  * Alerts-shape — the forensics exhibit: PF with contributors
    v1-unified / api-v1 / alerts / alerts-excav whose mint-time
    contributor-sum reads 5 while the PF's OWN path-set carries
    63 commits / 4 bug fixes / 2 authors. Recompute target:
    ``total=63 / bf=4 / ratio~0.063 / authors=2``.
  * Factory null-state ×4 sites — subdecompose / lane excavation /
    provenance re-home / file-lane mints stamp the honest zero-state
    (no inherited authors / health / last_modified) when armed, and
    keep the legacy inheritance byte-for-byte when OFF (kill-switch).
  * Dedup — PF commits deduped on the PF's own path-set: the
    recomputed total never exceeds the repo commit count, while the
    legacy contributor-sum double-counts shared commits.
  * Invariant — ``total_commits==0 ⇒ authors==[] ∧
    health_confidence=="insufficient"`` over every emitted row.

SACRED anti-cases: membership (paths / member_files / flows) and
``hotspot_files`` are byte-untouched by the recompute; the pass is
deterministic and idempotent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from faultline.analyzer.features import _calculate_health
from faultline.models.types import Commit, Feature
from faultline.pipeline_v2.metrics_recompute import (
    METRICS_RECOMPUTE_ENV,
    metrics_recompute_enabled,
    mint_null_state,
    run_metrics_recompute,
)

_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _mk_commit(
    sha: str,
    files: list[str],
    *,
    is_bug_fix: bool = False,
    author: str = "maintainer-a",
    day: int = 0,
) -> Commit:
    return Commit(
        sha=sha,
        message="fix: x" if is_bug_fix else "feat: x",
        author=author,
        date=_T0 + timedelta(days=day),
        files_changed=files,
        is_bug_fix=is_bug_fix,
    )


def _mk_feature(
    name: str,
    paths: list[str],
    *,
    layer: str = "developer",
    total_commits: int = 0,
    bug_fixes: int = 0,
    authors: list[str] | None = None,
    health_score: float = 100.0,
    health_confidence: str = "insufficient",
    last_modified: datetime = _EPOCH,
) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=list(authors or []),
        total_commits=total_commits,
        bug_fixes=bug_fixes,
        bug_fix_ratio=(bug_fixes / total_commits) if total_commits else 0.0,
        last_modified=last_modified,
        health_score=health_score,
        health_confidence=health_confidence,
        flows=[],
        layer=layer,
        product_feature_id=None,
    )


# ── Alerts-shape fixture (the forensics exhibit, synthetic twin) ─────────


def _alerts_world() -> tuple[list[Feature], list[Feature], list[Commit]]:
    """The B76 exhibit shape.

    Four contributors of one 'alerts' PF; two of them are post-Stage-6
    mints (tc=0 with LEAKED authors — the impossible rows), two carry
    stale pre-surgery counts (2 + 3 = the broken PF sum of 5). The PF's
    OWN path union carries exactly 63 unique commits / 4 bug fixes by
    exactly 2 authors.
    """
    v1_unified = _mk_feature(
        "v1-unified",
        ["apps/api/v1_unified/alerts_router.py",
         "apps/api/v1_unified/alerts_service.py"],
        total_commits=0, authors=["ghost-a", "ghost-b"],
        health_score=91.0, health_confidence="high",
        last_modified=_T0,  # leaked identity — not epoch
    )
    api_v1 = _mk_feature(
        "api-v1", ["apps/api/v1/alerts_api.py"],
        total_commits=2, authors=["maintainer-a"],
        health_score=80.0, health_confidence="low", last_modified=_T0,
    )
    alerts = _mk_feature(
        "alerts",
        ["frontend/src/alerts/page.tsx", "frontend/src/alerts/list.tsx"],
        total_commits=3, bug_fixes=1, authors=["maintainer-a"],
        health_score=70.0, health_confidence="low", last_modified=_T0,
    )
    alerts_excav = _mk_feature(
        "alerts-excav",
        ["backend/alerts/engine.py", "backend/alerts/rules.py"],
        total_commits=0, authors=["ghost-a", "ghost-b", "ghost-c"],
        health_score=88.0, health_confidence="high", last_modified=_T0,
    )
    devs = [v1_unified, api_v1, alerts, alerts_excav]

    pf_paths: list[str] = []
    for f in devs:
        pf_paths.extend(f.paths)
    # The broken mint: sum-over-contributors (0 + 2 + 3 + 0 = 5) with
    # unioned ghost authors — the disease this cycle cures.
    pf = _mk_feature(
        "alerts", pf_paths, layer="product",
        total_commits=5, bug_fixes=1,
        authors=["maintainer-a", "ghost-a", "ghost-b", "ghost-c"],
        health_score=88.75, health_confidence="low", last_modified=_T0,
    )

    # 63 unique commits over the PF's own path-set: exactly 4 bug
    # fixes, exactly 2 authors, deterministic round-robin file spread.
    commits: list[Commit] = []
    for i in range(63):
        commits.append(_mk_commit(
            f"sha{i:03d}",
            [pf_paths[i % len(pf_paths)]],
            is_bug_fix=(i < 4),
            author="maintainer-a" if i % 2 == 0 else "maintainer-b",
            day=i,
        ))
    return devs, [pf], commits


# ── Gate 1a — Alerts-shape recompute target ──────────────────────────────


def test_alerts_shape_pf_recompute_hits_target():
    devs, pfs, commits = _alerts_world()
    pf = pfs[0]
    # Document the disease first (contributor-sum world).
    assert pf.total_commits == 5
    assert len(pf.authors) == 4

    run_metrics_recompute(devs, pfs, commits)

    # ціль total=63 / bf=4 / ratio~0.063 / authors=2
    assert pf.total_commits == 63
    assert pf.bug_fixes == 4
    assert pf.bug_fix_ratio == round(4 / 63, 3) == 0.063
    assert pf.authors == ["maintainer-a", "maintainer-b"]
    assert len(pf.authors) == 2
    # health from the PF's OWN aggregated commit list — no contributor
    # placeholder averaging.
    assert pf.health_score == _calculate_health(4 / 63, 63, commits)
    assert pf.health_confidence in {"high", "low"}
    assert pf.last_modified == max(c.date for c in commits)


def test_alerts_shape_dev_rows_recover_their_own_mass():
    devs, pfs, commits = _alerts_world()
    run_metrics_recompute(devs, pfs, commits)
    by_name = {f.name: f for f in devs}
    # Every dev row regains ITS OWN commit mass (round-robin spread:
    # ceil-partitioned by file count) — no impossible rows survive.
    assert by_name["v1-unified"].total_commits == 18
    assert by_name["api-v1"].total_commits == 9
    assert by_name["alerts"].total_commits == 18
    assert by_name["alerts-excav"].total_commits == 18
    assert sum(f.total_commits for f in devs) == 63  # exclusive paths
    for f in devs:
        assert f.authors == ["maintainer-a", "maintainer-b"]
        assert "ghost-a" not in f.authors


# ── Gate 1b — dedup: PF totals bounded by repo commits ───────────────────


def test_pf_dedup_dev_sum_bounded_by_repo_commits():
    """дедуп-юніт: dev-сума ≤ репо-комітів.

    Every commit here touches files of BOTH contributors of one PF —
    the legacy contributor-sum double-counts (20 > 10 repo commits);
    the own-path-set recompute counts each commit once per PF.
    """
    dev_a = _mk_feature("billing-api", ["src/billing/api.py"],
                        total_commits=10, authors=["maintainer-a"])
    dev_b = _mk_feature("billing-ui", ["src/billing/ui.tsx"],
                        total_commits=10, authors=["maintainer-a"])
    pf = _mk_feature(
        "billing", ["src/billing/api.py", "src/billing/ui.tsx"],
        layer="product",
        total_commits=20,  # the legacy double-count (sum over contributors)
        authors=["maintainer-a"],
    )
    commits = [
        _mk_commit(f"s{i}", ["src/billing/api.py", "src/billing/ui.tsx"],
                   author="maintainer-a", day=i)
        for i in range(10)
    ]
    assert pf.total_commits == 20 > len(commits)  # disease documented

    run_metrics_recompute([dev_a, dev_b], [pf], commits)

    assert pf.total_commits == 10
    assert pf.total_commits <= len(commits)


def test_pf_dedup_sum_over_disjoint_pfs_bounded():
    pf1 = _mk_feature("checkout", ["src/checkout/a.py"], layer="product")
    pf2 = _mk_feature("inbox", ["src/inbox/b.py"], layer="product")
    commits = (
        [_mk_commit(f"c{i}", ["src/checkout/a.py"], day=i) for i in range(6)]
        + [_mk_commit(f"i{i}", ["src/inbox/b.py"], day=i) for i in range(4)]
    )
    run_metrics_recompute([], [pf1, pf2], commits)
    assert pf1.total_commits == 6
    assert pf2.total_commits == 4
    assert pf1.total_commits + pf2.total_commits <= len(commits)


# ── Gate 1c — invariant: tc==0 ⇒ authors==[] ∧ insufficient ─────────────


def test_invariant_zero_commits_means_no_authors_insufficient():
    devs, pfs, commits = _alerts_world()
    # An extra minted row whose paths NO commit touches, wearing leaked
    # identity — the impossible-row class.
    orphan = _mk_feature(
        "orphan-mint", ["nowhere/else/file.py"],
        total_commits=0, authors=["ghost-z"],
        health_score=95.0, health_confidence="high", last_modified=_T0,
    )
    devs = devs + [orphan]
    run_metrics_recompute(devs, pfs, commits)
    for row in devs + pfs:
        if row.total_commits == 0:
            assert row.authors == []
            assert row.health_confidence == "insufficient"
            assert row.last_modified == _EPOCH
    # census: the impossible class is empty after the pass
    impossible = [
        f for f in devs + pfs
        if f.total_commits == 0 and len(f.authors) > 0
    ]
    assert impossible == []


def test_zero_evidence_pf_is_confidence_marked_not_silently_100():
    """zero-evidence ≠ бездоказова 100.0: the placeholder is explicitly
    confidence-marked ``insufficient`` with no authors — it can never
    read as a healthy battle-tested row (display law renders the
    confidence), and no contributor placeholder feeds any average."""
    pf = _mk_feature(
        "ghost-pf", ["never/touched.py"], layer="product",
        total_commits=0, authors=["ghost"], health_score=88.75,
        health_confidence="high", last_modified=_T0,
    )
    run_metrics_recompute([], [pf], [
        _mk_commit("s0", ["elsewhere/real.py"], day=0),
    ])
    assert pf.total_commits == 0
    assert pf.authors == []
    assert pf.health_confidence == "insufficient"
    assert pf.last_modified == _EPOCH


# ── Gate 1d — factory null-state ×4 sites ────────────────────────────────


def _rich_source(name: str = "mega-blob") -> Feature:
    f = _mk_feature(
        name,
        ["src/blob/a.py", "src/blob/b.py", "src/blob/c.py"],
        total_commits=999, bug_fixes=400,
        authors=["leak-a", "leak-b"],
        health_score=13.0, health_confidence="high",
        last_modified=_T0,
    )
    f.uuid = "feedfacefeedfacefeedfacefeedface"
    return f


def _assert_null_state(minted: Feature) -> None:
    assert minted.total_commits == 0
    assert minted.bug_fixes == 0
    assert minted.bug_fix_ratio == 0.0
    assert minted.authors == []
    assert minted.last_modified == _EPOCH
    assert minted.health_score == 100.0
    assert minted.health_confidence == "insufficient"


def test_factory_subdecompose_stamps_null_state(monkeypatch):
    monkeypatch.setenv(METRICS_RECOMPUTE_ENV, "1")
    from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
        _make_subfeature,
    )
    sub = _make_subfeature(
        _rich_source(), "billing", ["src/blob/a.py"], "mega-blob-billing",
    )
    _assert_null_state(sub)


def test_factory_lane_excavation_stamps_null_state(monkeypatch):
    monkeypatch.setenv(METRICS_RECOMPUTE_ENV, "1")
    from faultline.pipeline_v2.lane_excavation import _make_excav_dev
    chunk = _make_excav_dev(
        _rich_source("shared-shell"), "alerts", ["src/blob/b.py"],
        "alerts-excav",
    )
    _assert_null_state(chunk)


def test_factory_provenance_rehome_stamps_null_state(monkeypatch):
    monkeypatch.setenv(METRICS_RECOMPUTE_ENV, "1")
    from faultline.pipeline_v2.provenance_rehome import _make_rehome_dev
    pf = SimpleNamespace(id="alerts", name="alerts", anchor_id="route:alerts")
    rehome = _make_rehome_dev(
        _rich_source("lane-shell"), pf, ["src/blob/c.py"], set(),
    )
    _assert_null_state(rehome)


def test_factory_file_lane_stamps_null_state(monkeypatch):
    monkeypatch.setenv(METRICS_RECOMPUTE_ENV, "1")
    from faultline.pipeline_v2.file_lane import _make_lane_dev
    lane = _make_lane_dev(
        _rich_source("template"), "src/shared", ["src/shared/util.py"],
        120, set(),
    )
    _assert_null_state(lane)


def test_factories_keep_legacy_inheritance_when_off(monkeypatch):
    """Kill-switch anti-case: unset flag ⇒ the four factories keep the
    pre-B76 deep-copy inheritance byte-for-byte (the OFF world's boards
    must not move — KS 4-way law)."""
    monkeypatch.delenv(METRICS_RECOMPUTE_ENV, raising=False)
    assert not metrics_recompute_enabled()
    from faultline.pipeline_v2.file_lane import _make_lane_dev
    from faultline.pipeline_v2.lane_excavation import _make_excav_dev
    from faultline.pipeline_v2.provenance_rehome import _make_rehome_dev
    from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
        _make_subfeature,
    )

    sub = _make_subfeature(
        _rich_source(), "billing", ["src/blob/a.py"], "mega-blob-billing",
    )
    chunk = _make_excav_dev(
        _rich_source("shared-shell"), "alerts", ["src/blob/b.py"],
        "alerts-excav",
    )
    pf = SimpleNamespace(id="alerts", name="alerts", anchor_id="route:alerts")
    rehome = _make_rehome_dev(
        _rich_source("lane-shell"), pf, ["src/blob/c.py"], set(),
    )
    for minted in (sub, chunk, rehome):
        # the legacy identity leak, preserved verbatim in the OFF world
        assert minted.total_commits == 0
        assert minted.authors == ["leak-a", "leak-b"]
        assert minted.health_score == 13.0
        assert minted.last_modified == _T0

    lane = _make_lane_dev(
        _rich_source("template"), "src/shared", ["src/shared/util.py"],
        120, set(),
    )
    # file-lane already nulled authors/health pre-B76; its legacy leak
    # is last_modified only.
    assert lane.authors == []
    assert lane.health_confidence == "insufficient"
    assert lane.last_modified == _T0


# ── SACRED anti-cases — display-only, deterministic ──────────────────────


def test_recompute_never_touches_membership_or_hotspots():
    devs, pfs, commits = _alerts_world()
    sentinel_hotspots = ["frontend/src/alerts/page.tsx",
                         "frontend/src/alerts/list.tsx"]
    devs[2].hotspot_files = list(sentinel_hotspots)
    before = [
        (list(f.paths), list(f.member_files), list(f.flows),
         list(f.hotspot_files))
        for f in devs + pfs
    ]
    run_metrics_recompute(devs, pfs, commits)
    after = [
        (list(f.paths), list(f.member_files), list(f.flows),
         list(f.hotspot_files))
        for f in devs + pfs
    ]
    assert before == after
    assert devs[2].hotspot_files == sentinel_hotspots


def test_recompute_is_deterministic_and_idempotent():
    devs1, pfs1, commits = _alerts_world()
    devs2, pfs2, _ = _alerts_world()
    run_metrics_recompute(devs1, pfs1, commits)
    run_metrics_recompute(devs2, pfs2, commits)
    dump = lambda rows: [f.model_dump() for f in rows]  # noqa: E731
    assert dump(devs1) == dump(devs2)
    assert dump(pfs1) == dump(pfs2)
    # idempotent: a second pass over already-recomputed rows is a no-op
    run_metrics_recompute(devs1, pfs1, commits)
    assert dump(devs1) == dump(devs2)


def test_telemetry_reports_impossible_census():
    devs, pfs, commits = _alerts_world()
    tele = run_metrics_recompute(devs, pfs, commits)
    assert tele["dev_rows"] == 4
    assert tele["pf_rows"] == 1
    assert tele["impossible_dev_before"] == 2   # v1-unified + alerts-excav
    assert tele["impossible_dev_after"] == 0
    assert tele["impossible_pf_after"] == 0


def test_mint_null_state_shape():
    ns = mint_null_state()
    assert ns == {
        "total_commits": 0,
        "bug_fixes": 0,
        "bug_fix_ratio": 0.0,
        "authors": [],
        "last_modified": _EPOCH,
        "health_score": 100.0,
        "health_confidence": "insufficient",
    }
