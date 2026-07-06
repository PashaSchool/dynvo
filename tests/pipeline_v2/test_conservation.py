"""Product-Spine §4.5 — conservation law UF ⊆ PF.

The ladder (accept / resettle / no-signal), the voting exclusions
(facet / shared / unowned files), the 6.7 construction-time wiring, the
typed pass, the 6.97 on-flow accounting, and the kill-switch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.conservation import (
    apply_uf_conservation,
    build_file_pf_owner,
    conserved_pfid,
)


def _devview(name: str, paths: list[str], pfid: str | None,
             role: str | None = None) -> dict:
    return {"name": name, "paths": paths, "product_feature_id": pfid,
            "role": role}


def _member(name: str, entry: str, paths: list[str],
            line_ranges: list[tuple[str, int, int]] | None = None) -> dict:
    return {
        "name": name,
        "uuid": name,
        "entry_point_file": entry,
        "paths": paths,
        "line_ranges": [
            {"path": p, "start_line": s, "end_line": e}
            for p, s, e in (line_ranges or [])
        ],
    }


_OWNER = build_file_pf_owner([
    _devview("bookings", ["src/bookings/api.py", "src/bookings/ui.tsx"],
             "bookings"),
    _devview("billing", ["src/billing/api.py"], "billing"),
    _devview("auth", ["src/auth/glue.py"], None, role="facet"),
    _devview("backend", ["src/core/db.py"], "shared-platform"),
])


# ── ownership resolution ───────────────────────────────────────────────────


def test_owner_map_excludes_facets_and_shared() -> None:
    assert _OWNER == {
        "src/bookings/api.py": "bookings",
        "src/bookings/ui.tsx": "bookings",
        "src/billing/api.py": "billing",
    }


# ── the ladder ─────────────────────────────────────────────────────────────


def test_incumbent_with_span_and_entry_majority_is_kept() -> None:
    members = [
        _member("create-booking-flow", "src/bookings/api.py",
                ["src/bookings/api.py"],
                [("src/bookings/api.py", 1, 100)]),
        _member("edit-booking-flow", "src/bookings/ui.tsx",
                ["src/bookings/ui.tsx"],
                [("src/bookings/ui.tsx", 1, 40)]),
    ]
    pfid, moved = conserved_pfid(members, _OWNER, "bookings")
    assert (pfid, moved) == ("bookings", False)


def test_violating_incumbent_resettles_to_span_majority_owner() -> None:
    """The 190x class: a journey attached to a PF that owns none of its
    code resettles to the real majority owner."""
    members = [
        _member("create-booking-flow", "src/bookings/api.py",
                ["src/bookings/api.py"],
                [("src/bookings/api.py", 1, 100)]),
    ]
    pfid, moved = conserved_pfid(members, _OWNER, "billing")
    assert (pfid, moved) == ("bookings", True)


def test_shared_incumbent_with_signal_moves_to_real_pf() -> None:
    members = [
        _member("f", "src/billing/api.py", ["src/billing/api.py"]),
    ]
    pfid, moved = conserved_pfid(members, _OWNER, "shared-platform")
    assert (pfid, moved) == ("billing", True)


def test_facet_and_shared_files_do_not_vote() -> None:
    """Entries/spans in facet-owned or shared-bucket files carry no vote —
    the remaining real-PF span decides."""
    members = [
        _member("f", "src/auth/glue.py",
                ["src/auth/glue.py", "src/core/db.py",
                 "src/billing/api.py"]),
    ]
    pfid, moved = conserved_pfid(members, _OWNER, None)
    assert (pfid, moved) == ("billing", True)


def test_no_signal_keeps_incumbent() -> None:
    members = [_member("f", "unowned/x.py", ["unowned/x.py"])]
    pfid, moved = conserved_pfid(members, _OWNER, "billing")
    assert (pfid, moved) == ("billing", False)


def test_no_signal_shared_incumbent_nulls_in_finalize_mode() -> None:
    members = [_member("f", "unowned/x.py", ["unowned/x.py"])]
    kept, moved_a = conserved_pfid(members, _OWNER, "shared-platform")
    assert (kept, moved_a) == ("shared-platform", False)
    nulled, moved_b = conserved_pfid(
        members, _OWNER, "shared-platform", null_shared_without_signal=True,
    )
    assert (nulled, moved_b) == (None, True)


def test_span_weight_beats_flow_count() -> None:
    """Span-LOC majority decides, not the member-count majority vote."""
    members = [
        _member("small-1", "src/billing/api.py", ["src/billing/api.py"],
                [("src/billing/api.py", 1, 5)]),
        _member("small-2", "src/billing/api.py", ["src/billing/api.py"],
                [("src/billing/api.py", 6, 10)]),
        _member("big", "src/bookings/api.py", ["src/bookings/api.py"],
                [("src/bookings/api.py", 1, 400)]),
    ]
    # Entry tally is 2:1 for billing, but span-LOC is 400:10 for bookings —
    # billing fails the span-majority prong and the journey resettles.
    pfid, moved = conserved_pfid(members, _OWNER, "billing")
    assert (pfid, moved) == ("bookings", True)


def test_deterministic_tiebreak() -> None:
    members = [
        _member("a", None, ["src/billing/api.py"],
                [("src/billing/api.py", 1, 10)]),
        _member("b", None, ["src/bookings/api.py"],
                [("src/bookings/api.py", 1, 10)]),
    ]
    # Equal spans, no entries → lexicographic key wins, twice the same.
    p1, _ = conserved_pfid(members, _OWNER, None)
    p2, _ = conserved_pfid(members, _OWNER, None)
    assert p1 == p2 == "billing"


# ── 6.7 construction-time wiring ───────────────────────────────────────────


def _scan_for_cluster() -> dict:
    return {
        "flows": [
            {
                "name": "create-booking-flow", "uuid": "u1",
                "entry_point_file": "src/bookings/api.py",
                "paths": ["src/bookings/api.py"],
                # primary dev is the MISATTRIBUTED one — the legacy vote
                # would follow it to 'billing'.
                "primary_feature": "billing-dev",
                "secondary_features": [], "test_files": [],
                "coverage_pct": None,
                "line_ranges": [
                    {"path": "src/bookings/api.py",
                     "start_line": 1, "end_line": 80},
                ],
            },
        ],
        "developer_features": [
            {"name": "billing-dev", "product_feature_id": "billing",
             "paths": ["src/billing/api.py"], "role": None},
            {"name": "bookings-dev", "product_feature_id": "bookings",
             "paths": ["src/bookings/api.py"], "role": None},
        ],
    }


def test_cluster_user_flows_resettles_pfid() -> None:
    from faultline.pipeline_v2.stage_6_7_user_flows import cluster_user_flows

    result = cluster_user_flows(_scan_for_cluster())
    assert result["uf_conservation_resettled"] == 1
    ufs = result["user_flows"]
    assert len(ufs) == 1
    # Legacy majority vote said 'billing' (primary dev's pfid); the span
    # lives in bookings-owned code → conserved to 'bookings'.
    assert ufs[0]["product_feature_id"] == "bookings"


def test_cluster_user_flows_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_SPINE_CONSERVATION", "0")
    from faultline.pipeline_v2.stage_6_7_user_flows import cluster_user_flows

    result = cluster_user_flows(_scan_for_cluster())
    assert result["uf_conservation_resettled"] == 0
    assert result["user_flows"][0]["product_feature_id"] == "billing"


# ── typed pass (6.7d / finalize) ───────────────────────────────────────────


def _typed_feature(name: str, paths: list[str], pfid: str | None,
                   *, layer: str = "developer", role: str | None = None,
                   flows: list[Flow] | None = None) -> Feature:
    return Feature(
        name=name, paths=paths, authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0, layer=layer, product_feature_id=pfid, role=role,
        flows=flows or [],
    )


def _typed_flow(name: str, entry: str, span: tuple[int, int]) -> Flow:
    return Flow(
        name=name, uuid=name, entry_point_file=entry, paths=[entry],
        authors=[], total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0,
        line_ranges=[
            {"path": entry, "start_line": span[0], "end_line": span[1]},
        ],
    )


def _uf(uf_id: str, name: str, pfid: str | None, members: list[str],
        synthesized: bool = False) -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, intent="author", resource="booking",
        product_feature_id=pfid, member_flow_ids=members,
        member_count=len(members), synthesized=synthesized,
    )


def test_apply_uf_conservation_resettles_and_skips_synthesized() -> None:
    fl = _typed_flow("create-booking-flow", "src/bookings/api.py", (1, 90))
    devs = [
        _typed_feature("bookings-dev", ["src/bookings/api.py"], "bookings",
                       flows=[fl]),
        _typed_feature("billing-dev", ["src/billing/api.py"], "billing"),
    ]
    pfs = [
        _typed_feature("bookings", ["src/bookings/api.py"], None,
                       layer="product"),
        _typed_feature("billing", ["src/billing/api.py"], None,
                       layer="product"),
    ]
    ufs = [
        _uf("UF-001", "Create bookings", "billing",
            ["create-booking-flow"]),
        _uf("UF-002", "Synthesized", "billing", ["create-booking-flow"],
            synthesized=True),
    ]
    tele = apply_uf_conservation(ufs, devs, pfs)
    assert tele["resettled"] == 1
    assert ufs[0].product_feature_id == "bookings"
    assert ufs[1].product_feature_id == "billing"  # synthesized skipped


def test_apply_uf_conservation_dev_to_product_override() -> None:
    """6.7d _finish passes its fresh map — the stamped pfids are stale."""
    fl = _typed_flow("f1", "src/bookings/api.py", (1, 50))
    devs = [
        _typed_feature("bookings-dev", ["src/bookings/api.py"],
                       "stale-old-pf", flows=[fl]),
    ]
    pfs = [_typed_feature("bookings", [], None, layer="product")]
    ufs = [_uf("UF-001", "X", "shared-platform", ["f1"])]
    tele = apply_uf_conservation(
        ufs, devs, pfs, dev_to_product={"bookings-dev": ("bookings",)},
    )
    assert tele["resettled"] == 1
    assert ufs[0].product_feature_id == "bookings"


def test_apply_uf_conservation_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_SPINE_CONSERVATION", "0")
    fl = _typed_flow("f1", "src/bookings/api.py", (1, 50))
    devs = [_typed_feature("bookings-dev", ["src/bookings/api.py"],
                           "bookings", flows=[fl])]
    pfs = [_typed_feature("bookings", [], None, layer="product")]
    ufs = [_uf("UF-001", "X", "billing", ["f1"])]
    tele = apply_uf_conservation(ufs, devs, pfs)
    assert tele["enabled"] is False
    assert ufs[0].product_feature_id == "billing"


# ── 6.97 on-flow accounting ────────────────────────────────────────────────


def _write(p: Path, lines: int) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(f"x = {i}" for i in range(lines)) + "\n",
                 encoding="utf-8")


def test_on_flow_accounting_clipped_by_construction(tmp_path: Path) -> None:
    from faultline.pipeline_v2.stage_6_97_feature_loc import apply_feature_loc

    _write(tmp_path / "src" / "bookings" / "api.py", 10)
    _write(tmp_path / "src" / "other" / "big.py", 30)

    fl = Flow(
        name="create-booking-flow", uuid="f1",
        entry_point_file="src/bookings/api.py",
        paths=["src/bookings/api.py", "src/other/big.py"],
        authors=[], total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0,
        line_ranges=[
            # Claims 60 span lines in a 10-LOC owned file → clipped to 10.
            {"path": "src/bookings/api.py", "start_line": 1, "end_line": 60},
            # 20 span lines in a file the PF does NOT own → shared channel.
            {"path": "src/other/big.py", "start_line": 1, "end_line": 20},
        ],
    )
    dev = _typed_feature("bookings-dev", ["src/bookings/api.py"],
                         "bookings", flows=[fl])
    other = _typed_feature("other-dev", ["src/other/big.py"], "other")
    pf = _typed_feature("bookings", ["src/bookings/api.py"], None,
                        layer="product")
    pf_other = _typed_feature("other", ["src/other/big.py"], None,
                              layer="product")
    ufs = [_uf("UF-001", "Create bookings", "bookings", ["f1"])]

    tele = apply_feature_loc(
        [dev, other], [pf, pf_other], tmp_path,
        user_flows=ufs, flows=[fl],
    )
    assert pf.loc == 10
    assert pf.loc_flow == 10          # 60 span lines clipped at file LOC
    assert pf.loc_flow_shared == 20   # foreign-file span → shared channel
    assert pf.loc_flow <= pf.loc      # on-flow ≤ 100% BY CONSTRUCTION
    acc = tele["loc_accounting"]
    assert acc["sum_pf_flow_on"] == 10
    assert acc["sum_pf_flow_shared"] == 20
    assert acc["on_flow_max_ratio"] <= 1.0


def test_on_flow_union_never_double_counts(tmp_path: Path) -> None:
    from faultline.pipeline_v2.stage_6_97_feature_loc import apply_feature_loc

    _write(tmp_path / "src" / "a.py", 100)
    mk = lambda n, s, e: Flow(  # noqa: E731 — local fixture builder
        name=n, uuid=n, entry_point_file="src/a.py", paths=["src/a.py"],
        authors=[], total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0,
        line_ranges=[{"path": "src/a.py", "start_line": s, "end_line": e}],
    )
    f1, f2 = mk("f1", 1, 40), mk("f2", 20, 60)  # overlapping spans
    dev = _typed_feature("a-dev", ["src/a.py"], "cap", flows=[f1, f2])
    pf = _typed_feature("cap", ["src/a.py"], None, layer="product")
    ufs = [_uf("UF-001", "X", "cap", ["f1", "f2"])]
    apply_feature_loc([dev], [pf], tmp_path, user_flows=ufs, flows=[f1, f2])
    assert pf.loc_flow == 60  # union(1-40, 20-60), not 40+41


def test_on_flow_accounting_kill_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAULTLINE_SPINE_CONSERVATION", "0")
    from faultline.pipeline_v2.stage_6_97_feature_loc import apply_feature_loc

    _write(tmp_path / "src" / "a.py", 5)
    dev = _typed_feature("a-dev", ["src/a.py"], "cap")
    pf = _typed_feature("cap", ["src/a.py"], None, layer="product")
    tele = apply_feature_loc([dev], [pf], tmp_path, user_flows=[], flows=[])
    assert pf.loc_flow is None and pf.loc_flow_shared is None
    assert "sum_pf_flow_on" not in tele["loc_accounting"]
