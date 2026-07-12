"""B45 — coverage_gaps[] gap channel: gap admissions leave user_flows[].

The member-less I8-cover markers (``_is_member_less_marker``: synthesized +
member_count=0 — the loc-worthy / owned-cover / system-route / e2e-orphan
seeds) are segregated into a dedicated top-level ``coverage_gaps[]`` channel
so a gap can never be mistaken for a journey. Three modes:

  * off (default) — byte-identical to pre-B45 (no gaps, marker rows stay,
    the ``coverage_gaps`` key ENTIRELY absent from the FeatureMap dump);
  * dual — gaps emitted AND the marker rows stay (bijection instrument);
  * full — gaps emitted, the marker rows REMOVED from ``user_flows[]``.

Covered here: mode behaviour + off byte-identity, dual↔full gap identity,
1:1 bijection on (pf, synthesis_reason, label), e2e authored_label preserved
in both ``label`` and ``authored_label``, deterministic ordering, member-ful
recall rows untouched, the kind-mapping discriminators per mint site, the I8
reformulation (a gap is valid PF cover in full mode), the A3 Connectors
arbitration determinism, ref-integrity of orphan gaps, and the A3 docstring.
"""

from __future__ import annotations

import copy
import datetime
import os
import types
from typing import Any

import pytest

from faultline.models.types import CoverageGap, FeatureMap, FlowLineRange
from faultline.pipeline_v2.emission_integrity import enforce_gap_ref_integrity
from faultline.pipeline_v2.synth_quality import (
    COVERAGE_GAP_CHANNEL_ENV,
    MARKER_COORDS_REQUIRED_ENV,
    _gap_kind,
    coverage_gap_channel_mode,
    emit_coverage_gaps,
    marker_coords_required,
    run_synth_quality,
)


# ── env isolation ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_gap_env() -> Any:
    """Several helpers below set the gap-channel / marker env directly; restore
    both after every test so the mutation never leaks into unrelated tests (a
    stray FAULTLINE_COVERAGE_GAP_CHANNEL=full would strip e2e-orphan UFs from
    other suites' scans)."""
    keys = (COVERAGE_GAP_CHANNEL_ENV, MARKER_COORDS_REQUIRED_ENV)
    saved = {k: os.environ.get(k) for k in keys}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── fixture helpers ──────────────────────────────────────────────────────────


def _ns(**kw: Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(**kw)


def _marker(
    uid: str,
    name: str,
    reason: str,
    pf: str | None,
    *,
    trigger: str | None = None,
    routes: list[str] | None = None,
    authored: str | None = None,
    resource: str | None = None,
    surface: bool = True,
) -> dict[str, Any]:
    """A member-less coverage marker (dict form — the synth_quality
    accessors are dict/object-agnostic, mirroring the B38 test style)."""
    return {
        "id": uid, "name": name, "synthesized": True, "member_count": 0,
        "member_flow_ids": [], "synthesis_reason": reason,
        "product_feature_id": pf, "trigger": trigger, "routes": routes or [],
        "resource": resource, "authored_label": authored,
        "surface_files": (
            [{"path": f"{pf or 'x'}.py", "start_line": 1, "end_line": 20}]
            if surface else None
        ),
        "name_confidence": "low", "ui_tier": "no-ui", "category": "system",
    }


def _organic(uid: str, name: str, pf: str) -> dict[str, Any]:
    return {
        "id": uid, "name": name, "synthesized": False, "member_count": 3,
        "member_flow_ids": ["f1", "f2", "f3"], "product_feature_id": pf,
    }


def _memberful_recall(uid: str, name: str, pf: str) -> dict[str, Any]:
    """A route_group_recall row — synthesized but MEMBER-FUL, never a
    marker, never a gap, never touched by the channel."""
    return {
        "id": uid, "name": name, "synthesized": True, "member_count": 2,
        "member_flow_ids": ["f4", "f5"], "product_feature_id": pf,
        "synthesis_reason": "route_group_recall",
    }


def _scene() -> tuple[list[dict], list, list, list]:
    """Four member-less markers (one per kind) + one organic journey + one
    member-ful recall row. pf-owned is FLOWFUL (a dev with flows); pf-loc is
    FLOWLESS; each marker's PF has no other cover (so demote keeps it)."""
    pfs = [
        _ns(name="pf-sysroute", display_name="System Route", member_files=[]),
        _ns(name="pf-loc", display_name="Loc Worthy", member_files=[]),
        _ns(name="pf-owned", display_name="Owned Cover", member_files=[]),
        _ns(name="pf-e2e", display_name="E2E Home", member_files=[]),
        _ns(name="pf-real", display_name="Real Feature", member_files=[]),
    ]
    devs = [
        _ns(name="dev-owned", product_feature_id="pf-owned",
            flows=[_ns(uuid="fo")]),      # → pf-owned is FLOWFUL
        _ns(name="dev-loc", product_feature_id="pf-loc", flows=[]),  # FLOWLESS
    ]
    ufs = [
        _marker("UF-1", "Run x", "system_flow_recall", "pf-sysroute",
                trigger="queue", routes=["/api/x"], resource="x"),
        _marker("UF-2", "Loc Worthy", "system_flow_recall", "pf-loc",
                resource="loc"),
        _marker("UF-3", "Owned Cover", "system_flow_recall", "pf-owned",
                resource="owned"),
        _marker("UF-4", "Bulk Actions", "e2e_journey_recall", "pf-e2e",
                authored="Bulk Actions", routes=["/bulk"]),
        _organic("UF-5", "Manage widgets", "pf-real"),
        _memberful_recall("UF-6", "Browse widgets", "pf-real"),
    ]
    return ufs, [], pfs, devs


def _run(mode: str) -> tuple[list[dict], dict, Any]:
    """Run the full synth-quality pass at ``mode`` on a fresh scene.

    The third element is the tele ``coverage_gaps`` value: ``None`` in off
    (key absent from output), a list — possibly empty — in dual/full (the
    key-presence contract)."""
    import os
    os.environ[COVERAGE_GAP_CHANNEL_ENV] = mode
    ufs, flows, pfs, devs = copy.deepcopy(_scene())
    meta: dict[str, Any] = {}
    tele = run_synth_quality(ufs, flows, pfs, meta, developer_features=devs)
    return ufs, meta, tele["coverage_gaps"]


# ── mode helper ──────────────────────────────────────────────────────────────


def test_mode_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default FULL since the 2026-07-12 flip (KEY_SCHEMA v27): unset / "" /
    # unrecognised read "full"; explicit off ("off"/"0"/"false") is still a
    # valid kill-switch value; dual unchanged.
    monkeypatch.delenv(COVERAGE_GAP_CHANNEL_ENV, raising=False)
    assert coverage_gap_channel_mode() == "full"
    for v, exp in [("", "full"), ("0", "off"), ("off", "off"), ("OFF", "off"),
                   ("false", "off"), ("dual", "dual"), ("DUAL", "dual"),
                   ("full", "full"), ("garbage", "full"), ("1", "full")]:
        monkeypatch.setenv(COVERAGE_GAP_CHANNEL_ENV, v)
        assert coverage_gap_channel_mode() == exp, v


def test_unset_equals_explicit_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inverted kill-switch (post-flip): UNSET behaves byte-identically to an
    explicit ``full`` on the synthetic scene."""
    full_ufs, full_meta, full_gaps = _run("full")
    monkeypatch.delenv(COVERAGE_GAP_CHANNEL_ENV, raising=False)
    ufs, flows, pfs, devs = copy.deepcopy(_scene())
    meta: dict[str, Any] = {}
    tele = run_synth_quality(ufs, flows, pfs, meta, developer_features=devs)
    assert ufs == full_ufs
    assert meta == full_meta
    assert tele["coverage_gaps"] == full_gaps


# ── off = byte-identical ─────────────────────────────────────────────────────


def test_off_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    off_ufs, off_meta, off_gaps = _run("off")
    dual_ufs, _dual_meta, _dual_gaps = _run("dual")
    # off emits no gaps (None — the key stays ABSENT from the output, unlike
    # dual/full's always-present list) and leaves the marker rows exactly as
    # dual leaves them (dual keeps the rows) — user_flows[] is identical.
    assert off_gaps is None
    assert [u["name"] for u in off_ufs] == [u["name"] for u in dual_ufs]
    assert len(off_ufs) == 6  # nothing removed
    # off adds NO gap-channel telemetry (byte-identity of scan_meta.synth_quality)
    sq = off_meta.get("synth_quality", {})
    assert "gap_channel_mode" not in sq
    assert "gaps_emitted" not in sq
    assert "marker_rows_converted" not in sq


def test_off_featuremap_key_absent() -> None:
    fm = FeatureMap(repo_path="/x", analyzed_at=datetime.datetime.now(),
                    total_commits=0, date_range_days=1)
    assert "coverage_gaps" not in fm.model_dump()  # key ENTIRELY absent
    g = CoverageGap(id="GAP-1", product_feature_id="pf", kind="loc_worthy",
                    label="Uncovered: X routes")
    fm2 = FeatureMap(repo_path="/x", analyzed_at=datetime.datetime.now(),
                     total_commits=0, date_range_days=1, coverage_gaps=[g])
    assert "coverage_gaps" in fm2.model_dump()


# ── key-presence contract: dual/full ALWAYS carry the key, [] when empty ─────


@pytest.mark.parametrize("mode", ["dual", "full"])
def test_zero_gap_board_key_present(mode: str) -> None:
    """The keyless-papermark probe class: a board with ZERO member-less
    markers must still declare the gap channel — the pass returns an EMPTY
    LIST (never None) in dual/full, so the output carries
    ``"coverage_gaps": []``. Consumers (warden gap-channel-leak class,
    flowless-silent gap exemption) detect the world by KEY PRESENCE."""
    os.environ[COVERAGE_GAP_CHANNEL_ENV] = mode
    # No member-less markers at all — organic + member-ful recall only.
    ufs = [_organic("UF-1", "Manage widgets", "pf-real"),
           _memberful_recall("UF-2", "Browse widgets", "pf-real")]
    pfs = [_ns(name="pf-real", display_name="Real Feature", member_files=[])]
    devs = [_ns(name="d", product_feature_id="pf-real", flows=[_ns(uuid="f")])]
    meta: dict[str, Any] = {}
    tele = run_synth_quality(ufs, [], pfs, meta, developer_features=devs)
    gaps = tele["coverage_gaps"]
    assert gaps == [] and gaps is not None  # empty LIST, never None
    # The channel is declared in telemetry too (converted=0, emitted=0).
    sq = meta["synth_quality"]
    assert sq["gap_channel_mode"] == mode
    assert sq["gaps_emitted"] == 0
    assert sq["marker_rows_converted"] == 0
    # And the FeatureMap dump carries the key as [] (present), unlike off.
    fm = FeatureMap(repo_path="/x", analyzed_at=datetime.datetime.now(),
                    total_commits=0, date_range_days=1, coverage_gaps=gaps)
    assert fm.model_dump()["coverage_gaps"] == []


def test_off_tele_gaps_is_none() -> None:
    """off returns None in the tele (the finalize caller then attaches
    nothing → key absent), NOT an empty list — the None/[] distinction IS
    the key-presence contract."""
    _ufs, _meta, gaps = _run("off")
    assert gaps is None


# ── dual: rows AND gaps, 1:1 bijection ───────────────────────────────────────


def test_dual_rows_and_gaps_bijection() -> None:
    ufs, meta, gaps = _run("dual")
    # The four member-less markers still ride user_flows[] (bijection tool).
    marker_names = {u["name"] for u in ufs if u.get("member_count") == 0}
    assert marker_names == {
        "Uncovered: System Route routes", "Uncovered: Loc Worthy routes",
        "Uncovered: Owned Cover routes", "Bulk Actions",
    }
    assert len(gaps) == 4
    # 1:1 bijection on (pf, synthesis_reason, label): each surviving marker
    # maps to exactly one gap with the same triple.
    marker_triples = sorted(
        (u["product_feature_id"], u["synthesis_reason"], u["name"])
        for u in ufs if u.get("member_count") == 0
    )
    gap_triples = sorted(
        (g.product_feature_id, g.synthesis_reason, g.label) for g in gaps
    )
    assert marker_triples == gap_triples
    assert meta["synth_quality"]["gaps_emitted"] == 4
    assert meta["synth_quality"]["gap_channel_mode"] == "dual"
    assert meta["synth_quality"]["marker_rows_converted"] == 0  # rows stay


# ── full: rows gone, gaps identical to dual's ────────────────────────────────


def test_full_rows_gone_gaps_identical_to_dual() -> None:
    _dual_ufs, _dm, dual_gaps = _run("dual")
    full_ufs, meta, full_gaps = _run("full")
    # The four markers are GONE; only the organic + member-ful recall row stay.
    assert [u["name"] for u in full_ufs] == ["Manage widgets", "Browse widgets"]
    assert all(u.get("member_count", 0) != 0 for u in full_ufs)
    # gaps are IDENTICAL to dual's (built from the same post-B31 markers).
    assert [g.id for g in full_gaps] == [g.id for g in dual_gaps]
    assert [(g.kind, g.label, g.product_feature_id, g.loc) for g in full_gaps] \
        == [(g.kind, g.label, g.product_feature_id, g.loc) for g in dual_gaps]
    assert meta["synth_quality"]["marker_rows_converted"] == 4


# ── e2e authored_label preserved in gap.authored_label AND gap.label ─────────


def test_e2e_authored_label_preserved() -> None:
    _ufs, _meta, gaps = _run("full")
    e2e = [g for g in gaps if g.kind == "e2e_orphan"]
    assert len(e2e) == 1
    g = e2e[0]
    assert g.authored_label == "Bulk Actions"   # raw maintainer label
    assert g.label == "Bulk Actions"            # also carried in label
    assert g.synthesis_reason == "e2e_journey_recall"
    # System kinds carry no authored label (omitted from the dump).
    for sysg in (x for x in gaps if x.kind != "e2e_orphan"):
        assert sysg.authored_label is None
        assert "authored_label" not in sysg.model_dump()


# ── loc / surface spans on gaps ──────────────────────────────────────────────


def test_gap_carries_surface_spans_and_loc() -> None:
    _ufs, _meta, gaps = _run("dual")
    for g in gaps:
        assert len(g.surface_files) == 1
        assert isinstance(g.surface_files[0], FlowLineRange)
        assert g.loc == 20  # whole-file span (1..20) union length


# ── deterministic ordering (pf, id) + order-independence ─────────────────────


def test_deterministic_ordering() -> None:
    import os
    os.environ[COVERAGE_GAP_CHANNEL_ENV] = "full"
    ufs, flows, pfs, devs = copy.deepcopy(_scene())
    g1 = run_synth_quality(ufs, flows, pfs, {},
                           developer_features=devs)["coverage_gaps"]
    # sorted by (product_feature_id, id) — assert the emitted list is sorted.
    keys = [(g.product_feature_id or "", g.id) for g in g1]
    assert keys == sorted(keys)
    # Shuffle the input marker order → identical sorted gap set (arbitration is
    # a function of board STATE, not synthesis order).
    ufs2, flows2, pfs2, devs2 = copy.deepcopy(_scene())
    ufs2 = list(reversed(ufs2))
    g2 = run_synth_quality(ufs2, flows2, pfs2, {},
                           developer_features=devs2)["coverage_gaps"]
    assert [g.id for g in g1] == [g.id for g in g2]


def test_id_is_content_derived_and_stable() -> None:
    _ufs, _meta, gaps1 = _run("dual")
    _ufs2, _meta2, gaps2 = _run("dual")
    # Same board state → same ids across independent runs (no Date.now / rng).
    assert [g.id for g in gaps1] == [g.id for g in gaps2]
    for g in gaps1:
        assert g.id.startswith("GAP-") and len(g.id) == len("GAP-") + 10


# ── member-ful recall rows untouched in ALL modes ────────────────────────────


@pytest.mark.parametrize("mode", ["off", "dual", "full"])
def test_memberful_recall_untouched(mode: str) -> None:
    ufs, _meta, gaps = _run(mode)
    recall = [u for u in ufs if u.get("synthesis_reason") == "route_group_recall"]
    assert len(recall) == 1  # never removed, never a gap
    assert recall[0]["name"] == "Browse widgets"
    assert recall[0]["member_count"] == 2
    assert all(g.synthesis_reason != "route_group_recall" for g in gaps or [])


# ── kind mapping per mint site (unit — _gap_kind) ────────────────────────────


def test_kind_mapping_discriminators() -> None:
    flowful = {"pf-owned"}
    # e2e — unambiguous by reason.
    assert _gap_kind(_marker("e", "n", "e2e_journey_recall", "pf-e2e"),
                     flowful) == "e2e_orphan"
    # route-group site — the ONLY system_flow_recall producer with a trigger.
    assert _gap_kind(
        _marker("s", "n", "system_flow_recall", "pf", trigger="queue"),
        flowful) == "system_route"
    # route-group corroboration — non-empty routes also → system_route.
    assert _gap_kind(
        _marker("s", "n", "system_flow_recall", "pf", routes=["/a"]),
        flowful) == "system_route"
    # 6.7d owned-cover — no trigger/routes, FLOWFUL PF.
    assert _gap_kind(
        _marker("o", "n", "system_flow_recall", "pf-owned"),
        flowful) == "owned_cover"
    # 6.7d loc-worthy — no trigger/routes, FLOWLESS PF (honest default).
    assert _gap_kind(
        _marker("l", "n", "system_flow_recall", "pf-loc"),
        flowful) == "loc_worthy"
    # unknown/None PF, no positive flowful signal → loc_worthy superset.
    assert _gap_kind(
        _marker("u", "n", "system_flow_recall", None), flowful) == "loc_worthy"


# ── I8 reformulation: a gap is valid PF cover in full mode ───────────────────


def test_i8_gap_counts_as_cover_full() -> None:
    """pf-loc's SOLE cover is a member-less marker (demote KEEPS it, I8-safe).
    In full mode that marker leaves user_flows[] and becomes a gap — the PF is
    still covered (by the gap), so converting the sole cover is NOT a loss."""
    full_ufs, _meta, gaps = _run("full")
    # No user_flow references pf-loc any more...
    assert all(u.get("product_feature_id") != "pf-loc" for u in full_ufs)
    # ...but a gap does — I8 (≥1 UF ref OR ≥1 gap) is satisfied.
    assert any(g.product_feature_id == "pf-loc" for g in gaps)
    # And the sole-cover marker was NOT demoted to the side-channel (it
    # survived to become a gap).
    assert _meta.get("system_flow_seeds", []) == []


# ── A3: Connectors arbitration determinism (board-state, not synth order) ────


def test_a3_connectors_arbitration_determinism() -> None:
    """Model the midday Connectors case: a PF whose own-entry pool is empty
    mints a member-less gap; after devs re-point into it (pool non-empty) the
    gap disappears and a member-ful journey appears instead. Both directions
    are deterministic (identical across independent runs)."""
    import os
    os.environ[COVERAGE_GAP_CHANNEL_ENV] = "full"
    pfs = [_ns(name="pf-connectors", display_name="Connectors", member_files=[])]

    def state_starved() -> tuple[list[dict], list, list]:
        # empty own-entry pool → a member-less owned/loc gap seed
        ufs = [_marker("UF-1", "Connectors", "system_flow_recall",
                       "pf-connectors", resource="connectors")]
        devs = [_ns(name="d", product_feature_id="pf-connectors", flows=[])]
        return ufs, [], devs

    def state_repointed() -> tuple[list[dict], list, list]:
        # devs re-pointed in → a member-FUL journey, no member-less seed
        ufs = [_organic("UF-1", "Manage connectors", "pf-connectors")]
        devs = [_ns(name="d", product_feature_id="pf-connectors",
                    flows=[_ns(uuid="fx")])]
        return ufs, [], devs

    def run(state_fn: Any) -> tuple[list[str], list[str]]:
        ufs, flows, devs = copy.deepcopy(state_fn())
        gaps = run_synth_quality(ufs, flows, copy.deepcopy(pfs), {},
                                 developer_features=devs)["coverage_gaps"]
        return [u["name"] for u in ufs], [g.id for g in gaps]

    # Starved → exactly one gap on pf-connectors, deterministic across runs.
    names_a, gap_ids_a = run(state_starved)
    names_a2, gap_ids_a2 = run(state_starved)
    assert gap_ids_a == gap_ids_a2 and len(gap_ids_a) == 1
    assert names_a == []  # the seed left user_flows (converted to the gap)
    # Re-pointed → NO gap, a member-ful journey survives, deterministic.
    names_b, gap_ids_b = run(state_repointed)
    names_b2, gap_ids_b2 = run(state_repointed)
    assert gap_ids_b == gap_ids_b2 == []
    assert names_b == ["Manage connectors"]


# ── B37-ph2: the Connectors HOMING case (dispatch mint → owner) ──────────────


def test_a3_connectors_homing_determinism(monkeypatch: pytest.MonkeyPatch) -> None:
    """B37-ph2 extension of the Connectors family: the homing PASS itself (not
    a hand-built re-pointed state) produces the arbitration outcome, and it is
    a function of board STATE, not synthesis order.

    A dispatch mint targeting a file UNDER the Connectors anchor re-homes its
    member-ful UF into Connectors → the member-less marker demotes → NO gap.
    A mint targeting elsewhere leaves Connectors starved → its marker → a gap.
    Both directions identical across independent runs."""
    from faultline.pipeline_v2.dispatch_homing import home_dispatch_mints

    os.environ[COVERAGE_GAP_CHANNEL_ENV] = "full"
    monkeypatch.setenv("FAULTLINE_DISPATCH_HOMING_B37P2", "1")

    def _scene(target: str) -> tuple[list[Any], list[Any], list[Any]]:
        # A dispatch mint on an (unowned) dev, homed via its UF to pf-other.
        mint = _ns(uuid="m1", name="run-callback-flow", entry_point_file=target,
                   description="dispatch registry apps/api/registry.ts ['cb']")
        devs = [_ns(name="d", product_feature_id="pf-other", flows=[mint])]
        pfs = [
            _ns(name="pf-connectors", display_name="Connectors", id=None,
                anchor_id="fdir:apps/api/connectors", member_files=[]),
            _ns(name="pf-other", display_name="Other", id=None,
                anchor_id="fdir:apps/web/other", member_files=[]),
        ]
        # member-ful dispatch UF (attribute-settable — the real UserFlow type)
        # + a member-less loc-worthy marker on the (starved) Connectors PF
        # + a keeper journey on pf-other (so the move is not orphan-blocked).
        ufs = [
            _ns(id="UF-1", name="run-callback-flow", synthesized=False,
                member_count=1, member_flow_ids=["m1"],
                product_feature_id="pf-other"),
            _ns(id="UF-0", name="Manage other", synthesized=False,
                member_count=2, member_flow_ids=["k1", "k2"],
                product_feature_id="pf-other"),
            _ns(**_marker("UF-2", "Connectors", "system_flow_recall",
                          "pf-connectors", resource="connectors")),
        ]
        return ufs, devs, pfs

    def run(target: str) -> tuple[str | None, list[str]]:
        ufs, devs, pfs = copy.deepcopy(_scene(target))
        home_dispatch_mints(ufs, devs, pfs)
        gaps = run_synth_quality(
            ufs, [], pfs, {}, developer_features=devs)["coverage_gaps"]
        conn_gaps = [g.id for g in gaps if g.product_feature_id == "pf-connectors"]
        homed_uf = next(
            (u for u in ufs if getattr(u, "member_count", 0)), None)
        return (homed_uf.product_feature_id if homed_uf else None), conn_gaps

    # Direction 1 — mint targets a file under the Connectors anchor: the UF
    # re-homes into Connectors, the marker demotes, no gap. Deterministic.
    home1, gaps1 = run("apps/api/connectors/callback.ts")
    home1b, gaps1b = run("apps/api/connectors/callback.ts")
    assert home1 == home1b == "pf-connectors"
    assert gaps1 == gaps1b == []  # Connectors covered ⇒ its marker demoted

    # Direction 2 — mint targets elsewhere: Connectors stays starved and keeps
    # its coverage gap. Deterministic, and the UF is NOT hijacked into Connectors.
    home2, gaps2 = run("apps/web/other/handler.ts")
    home2b, gaps2b = run("apps/web/other/handler.ts")
    assert home2 == home2b == "pf-other"  # no-op: mint already owns pf-other
    assert gaps2 == gaps2b and len(gaps2) == 1


# ── ref-integrity: orphan gaps dropped with telemetry ────────────────────────


def test_gap_ref_integrity_drops_orphans() -> None:
    pfs = [_ns(name="pf-real", id=None)]
    gaps = [
        CoverageGap(id="GAP-a", product_feature_id="pf-real",
                    kind="loc_worthy", label="Uncovered: real routes"),
        CoverageGap(id="GAP-b", product_feature_id="pf-ghost",
                    kind="loc_worthy", label="Uncovered: ghost routes"),
        CoverageGap(id="GAP-c", product_feature_id=None,
                    kind="e2e_orphan", label="Anon"),
    ]
    kept, tele = enforce_gap_ref_integrity(gaps, pfs)
    assert [g.id for g in kept] == ["GAP-a"]
    assert tele["orphans_dropped"] == 2
    dropped_ids = {d["id"] for d in tele["dropped"]}
    assert dropped_ids == {"GAP-b", "GAP-c"}


def test_gap_ref_integrity_relinks_canonical() -> None:
    # A divergent-but-equivalent ref is relinked, not dropped (I12 parity).
    pfs = [_ns(name="Real Feature", id=None)]
    gaps = [CoverageGap(id="GAP-a", product_feature_id="real-feature",
                        kind="loc_worthy", label="x")]
    kept, tele = enforce_gap_ref_integrity(gaps, pfs)
    assert len(kept) == 1 and tele["relinked"] == 1


# ── suppression ledger in full (no-silent-gap law) ───────────────────────────


def test_full_suppression_ledger() -> None:
    """A member-less marker with NO surface coords is B38-suppressed (not a
    gap) but ALWAYS recorded in scan_meta.suppressed_markers — the gap is
    counted, never silently lost."""
    import os
    os.environ[COVERAGE_GAP_CHANNEL_ENV] = "full"
    os.environ[MARKER_COORDS_REQUIRED_ENV] = "1"
    pfs = [_ns(name="pf-x", display_name="X", member_files=[])]
    devs = [_ns(name="d", product_feature_id="pf-x", flows=[])]
    ufs = [
        _marker("UF-1", "With coords", "e2e_journey_recall", "pf-x",
                authored="With coords"),
        _marker("UF-2", "No coords", "e2e_journey_recall", "pf-x",
                authored="No coords", surface=False),
    ]
    meta: dict[str, Any] = {}
    gaps = run_synth_quality(ufs, [], pfs, meta,
                             developer_features=devs)["coverage_gaps"]
    # The coord-bearing marker becomes a gap; the coordless one is suppressed.
    assert len(gaps) == 1 and gaps[0].label == "With coords"
    sq = meta["synth_quality"]
    assert sq["markers_suppressed_no_coords"] == 1
    assert sq["gaps_suppressed_no_coords"] == 1  # surfaced in the gap telemetry
    ledger_ids = {r["id"] for r in sq["suppressed_markers"]}
    assert "UF-2" in ledger_ids  # the gap is counted in the ledger, not lost
    # Bijection audit: emitted + suppressed == surviving member-less markers (2).
    assert sq["gaps_emitted"] + sq["gaps_suppressed_no_coords"] == 2


# ── A3(a): the marker_coords_required docstring is honest (Default ON) ────────


def test_a3_marker_coords_docstring_matches_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The docstring now says "Default ON — an UNSET env resolves to '1'".
    monkeypatch.delenv(MARKER_COORDS_REQUIRED_ENV, raising=False)
    assert marker_coords_required() is True
    assert "Default ON" in (marker_coords_required.__doc__ or "")
    assert "Default OFF" not in (marker_coords_required.__doc__ or "")
