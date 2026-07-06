"""Product-Spine Wave 2a — no-signal UF terminal home (validator I21).

The deterministic ladder (ownership argmax → system-scope preference →
nearest-directory argmax), the binding_confidence tag, the never-null /
never-shared guarantees, the degenerate no-real-PF case, and the
kill-switch.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.uf_terminal_home import assign_terminal_homes


def _feature(name: str, paths: list[str], pfid: str | None = None,
             *, layer: str = "developer", flows: list[Flow] | None = None,
             surface_scope: str | None = None) -> Feature:
    f = Feature(
        name=name, paths=paths, authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0, layer=layer, product_feature_id=pfid,
        flows=flows or [],
    )
    if surface_scope:
        f.surface_scope = surface_scope
    return f


def _flow(name: str, entry: str, paths: list[str] | None = None) -> Flow:
    return Flow(
        name=name, uuid=name, entry_point_file=entry,
        paths=paths or [entry], authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0,
    )


def _uf(uf_id: str, name: str, pfid: str | None, members: list[str],
        category: str = "interactive", trigger: str | None = None) -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, intent="browse", resource="thing",
        product_feature_id=pfid, member_flow_ids=members,
        member_count=len(members), category=category, trigger=trigger,
    )


def test_orphan_with_ownership_signal_homes_by_argmax_even_below_majority() -> None:
    fl = _flow("f1", "src/edr/api.py",
               ["src/edr/api.py", "unowned/a.py", "unowned/b.py"])
    devs = [
        _feature("edr-dev", ["src/edr/api.py"], "detections", flows=[fl]),
    ]
    pfs = [
        _feature("detections", ["src/edr/api.py"], layer="product"),
        _feature("billing", ["src/billing/api.py"], layer="product"),
    ]
    orphan = _uf("UF-001", "Browse aggregated EDR detections", None, ["f1"])
    tele = assign_terminal_homes([orphan], devs, pfs)
    assert orphan.product_feature_id == "detections"
    assert orphan.binding_confidence == "low"
    assert tele["homed_votes"] == 1 and tele["unhomed"] == 0


def test_system_orphan_prefers_system_scope_pf() -> None:
    fl = _flow("cron-f", "unowned/jobs/cron.py", ["unowned/jobs/cron.py"])
    dev = _feature("jobs-dev", ["src/jobs/runner.py"], "background-jobs")
    devs = [dev, _feature("big-dev", ["src/big/" + f"f{i}.py" for i in range(9)],
                          "big-product")]
    pfs = [
        _feature("background-jobs", ["src/jobs/runner.py"], layer="product",
                 surface_scope="system"),
        _feature("big-product", devs[1].paths, layer="product",
                 surface_scope="product"),
    ]
    orphan = _uf("UF-001", "Trigger and monitor background cron jobs", None,
                 ["cron-f"], category="system", trigger="scheduled")
    # Attach the member flow to a dev so the lookup can resolve it.
    dev.flows = [fl]
    tele = assign_terminal_homes([orphan], devs, pfs)
    # No direct ownership votes (unowned files) → system rung: the
    # system-scope PF wins even though big-product owns 9x the files.
    assert orphan.product_feature_id == "background-jobs"
    assert orphan.binding_confidence == "low"
    assert tele["homed_system"] == 1


def test_no_signal_orphan_homes_to_nearest_directory_owner() -> None:
    fl = _flow("f1", "src/analytics/widgets/chart.py",
               ["src/analytics/widgets/chart.py"])
    dev_a = _feature("analytics-dev", ["src/analytics/api.py"], "analytics",
                     flows=[fl])
    dev_b = _feature("billing-dev", ["src/billing/api.py"], "billing")
    pfs = [
        _feature("analytics", ["src/analytics/api.py"], layer="product"),
        _feature("billing", ["src/billing/api.py"], layer="product"),
    ]
    orphan = _uf("UF-001", "Browse charts", None, ["f1"])
    tele = assign_terminal_homes([orphan], [dev_a, dev_b], pfs)
    # chart.py is unowned; walking up src/analytics/widgets → src/analytics
    # finds analytics' owned file → nearest-directory vote wins.
    assert orphan.product_feature_id == "analytics"
    assert orphan.binding_confidence == "low"
    assert tele["homed_dir"] == 1


def test_never_shared_and_bound_ufs_untouched() -> None:
    fl = _flow("f1", "src/a/x.py", ["src/a/x.py"])
    devs = [_feature("a-dev", ["src/a/x.py"], "alpha", flows=[fl])]
    pfs = [
        _feature("alpha", ["src/a/x.py"], layer="product"),
        _feature("shared-platform", ["lib/util.py"], layer="product"),
    ]
    bound = _uf("UF-001", "Bound journey", "alpha", ["f1"])
    orphan = _uf("UF-002", "Orphan journey", None, ["f1"])
    tele = assign_terminal_homes([bound, orphan], devs, pfs)
    assert bound.product_feature_id == "alpha"
    assert bound.binding_confidence is None  # untouched
    # The shared bucket is never a terminal home.
    assert orphan.product_feature_id == "alpha"
    assert tele["orphans"] == 1


def test_degenerate_no_real_pf_leaves_nulls_documented() -> None:
    pfs = [_feature("shared-platform", ["lib/util.py"], layer="product")]
    orphan = _uf("UF-001", "Orphan", None, [])
    tele = assign_terminal_homes([orphan], [], pfs)
    assert orphan.product_feature_id is None
    assert tele["unhomed"] == 1


def test_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_SPINE_UF_TERMINAL_HOME", "0")
    orphan = _uf("UF-001", "Orphan", None, [])
    tele = assign_terminal_homes(
        [orphan], [], [_feature("alpha", ["a.py"], layer="product")],
    )
    assert tele["enabled"] is False
    assert orphan.product_feature_id is None
    # Serialization contract: unset binding_confidence never dumps.
    assert "binding_confidence" not in orphan.model_dump()
