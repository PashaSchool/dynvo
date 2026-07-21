"""B77 — residual citability (default OFF).

``FAULTLINE_RESIDUAL_CITABILITY``: the RESIDUAL-CITABILITY MASS-TRANSFER
class (forensics 2026-07-21, ledger §ФОРЕНЗИКА 502M). The 6.7c mega-split's
recall-safe residual bucket keeps the parent's journey-like name, Call-1
cites it via ``from_flows``, and Pass-1 inherits the WHOLE bucket with no
content gate ('Create and run logic functions' 502m inherited 778 ids from
two cited buckets; same class ×3: 502m/278m/216m).

Four flag-gated mechanisms, unit-pinned here:
  Seg 1 — 6.7c stamps ``UserFlow.residual=True`` on the leftover bucket;
          6.7d Pass-1 REFUSES wholesale from_flows inheritance from it
          (token-gated grounding/backstop only); the bucket row survives
          with its unclaimed members (no-orphan).
  Seg 2 — Pass-1 container-inherit passes a member ONLY on Pass-2a's own
          ``& utok`` content-token overlap (symmetry law).
  Seg 3 — mint-side domain carve: a built UF majority-voting for >1 real
          (non-container) PF home with no common strict majority is carved
          per home via the existing ``member_votes`` mechanism.
  Seg 4 — a ws-container PF is not a valid conservation resettle target.

Named anti-cases (the spec's survivors):
  - twenty 'Sign in and authenticate' — client+server container journey
    STAYS whole (affinity passes on content overlap; the carve never
    counts containers as domains).
  - Seg-C cited-dev (Pass-2a) rescue channel is untouched.
  - a genuine composite with a strict-majority home ('Create and edit
    records' / tasks proxy) is never carved.
  - legitimate citations of NON-residual rows inherit exactly as before.
  - unset / =0 => byte-behavior everywhere (kill-switch law).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.conservation import (
    apply_uf_conservation,
    conserved_pfid,
)
from faultline.pipeline_v2.stage_6_7c_uf_splitter import (
    _split_one,
    residual_citability_enabled,
)
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _build_user_flows,
    _carve_multi_domain_ufs,
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)
_B77 = "FAULTLINE_RESIDUAL_CITABILITY"
_SEGC = "FAULTLINE_HOME_PURE_CONTAINER_INHERIT"


def _flow(name: str, entry: str) -> Flow:
    return Flow(
        name=name, uuid=f"uuid-{name}", paths=[entry], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, entry_point_file=entry,
    )


def _dev(name: str, pfid: str | None, paths: list[str],
         flows: list[Flow] | None = None) -> Feature:
    return Feature(
        name=name, display_name=name, paths=paths, authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="developer", product_feature_id=pfid,
        flows=flows or [],
    )


def _pf(name: str, display: str, anchor: str | None = None) -> Feature:
    return Feature(
        name=name, display_name=display, paths=[], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="product", anchor_id=anchor,
    )


def _uf(uid: str, name: str, pfid: str | None,
        member_ids: list[str], resource: str = "record") -> UserFlow:
    return UserFlow(
        id=uid, name=name, resource=resource, domain=None,
        product_feature_id=pfid, intent="manage",
        member_flow_ids=member_ids, member_count=len(member_ids),
    )


# ── Seg 1 — the 6.7c residual marker ─────────────────────────────────────


def _split_world():
    """A mega-UF of 4 members; the LLM places 2 → 1 journey sub + a
    2-member residual bucket."""
    flows = [
        _flow("create-story-flow", "src/stories/create.ts"),
        _flow("edit-story-flow", "src/stories/edit.ts"),
        _flow("orphan-one-flow", "src/misc/one.ts"),
        _flow("orphan-two-flow", "src/misc/two.ts"),
    ]
    fbk = {f.uuid: f for f in flows}
    parent = _uf("UF-128", "found story notfound stories", "stories",
                 [f.uuid for f in flows], resource="story")
    journeys = [
        {"name": "Author stories",
         "members": ["create-story-flow", "edit-story-flow"]},
    ]
    return parent, journeys, fbk


def test_residual_marker_stamped_when_armed(monkeypatch) -> None:
    monkeypatch.setenv(_B77, "1")
    assert residual_citability_enabled()
    parent, journeys, fbk = _split_world()
    subs = _split_one(parent, journeys, fbk)
    assert [s.name for s in subs] == [
        "Author stories", "found story notfound stories"]
    assert subs[0].residual is False          # journey sub — never marked
    assert subs[1].residual is True           # the leftover bucket
    assert subs[1].member_count == 2          # recall-safe: nothing dropped
    assert "residual" in subs[1].model_dump()  # serialized on armed rows


def test_residual_marker_off_is_byte_identical(monkeypatch) -> None:
    monkeypatch.delenv(_B77, raising=False)
    parent, journeys, fbk = _split_world()
    subs = _split_one(parent, journeys, fbk)
    assert all(s.residual is False for s in subs)
    # The dump carries NO "residual" key — pre-B77 bytes.
    assert all("residual" not in s.model_dump() for s in subs)
    monkeypatch.setenv(_B77, "0")
    parent2, journeys2, fbk2 = _split_world()
    subs2 = _split_one(parent2, journeys2, fbk2)
    assert [s.model_dump() for s in subs] == [s.model_dump() for s in subs2]


# ── Seg 1 — Pass-1 wholesale refusal + no-orphan bucket keep ─────────────


def _giant_world():
    """The 502m shape at unit scale: a residual bucket of 10 (3 logic-core
    members riding dev 'logic functions' + 7 story members riding dev
    'stories'), cited by ONE journey via from_flows AND from_dev_features."""
    core = [
        _flow(f"run-logic-function-{i}-flow", f"src/logic/f{i}.ts")
        for i in range(3)
    ]
    story = [
        _flow(f"found-story-{i}-flow", f"src/stories/s{i}.ts")
        for i in range(7)
    ]
    d_logic = _dev("logic functions", "logic-functions",
                   [f.entry_point_file for f in core], core)
    d_story = _dev("stories", "stories",
                   [f.entry_point_file for f in story], story)
    members = [f.uuid for f in core] + [f.uuid for f in story]
    bucket = _uf("UF-128-9", "found story notfound stories",
                 "logic-functions", members, resource="story")
    bucket.residual = True
    spec = {
        "name": "Create and run logic functions", "resource": "logic-function",
        "product_feature": "Logic Functions",
        "from_flows": ["UF-128-9"], "from_dev_features": ["logic functions"],
    }
    return [d_logic, d_story], bucket, spec, core, story


def test_pass1_wholesale_refusal_and_bucket_keep(monkeypatch) -> None:
    monkeypatch.setenv(_B77, "1")
    devs, bucket, spec, core, story = _giant_world()
    ufs, tele = _build_user_flows([spec], [bucket], devs, [])
    assert tele["uf_residual_cited_refused"] == 1
    assert tele["uf_residual_buckets_kept"] == 1
    by_name = {u.name: u for u in ufs}
    journey = by_name["Create and run logic functions"]
    kept = by_name["found story notfound stories"]
    # The journey holds ONLY the token-matched logic core (Pass-2a cited-dev
    # channel), never the wholesale 10 — the 481→core affinity shape.
    assert journey.member_flow_ids == [f.uuid for f in core]
    assert journey.member_count == 3
    # No-orphan: the bucket ROW survives with every unclaimed member.
    assert kept.residual is True
    assert kept.member_flow_ids == [f.uuid for f in story]
    # Conservation of the member universe: union preserved, homes disjoint.
    assert set(journey.member_flow_ids) | set(kept.member_flow_ids) == \
        set(bucket.member_flow_ids)
    assert not set(journey.member_flow_ids) & set(kept.member_flow_ids)


def test_pass1_flag_off_reproduces_mass_transfer(monkeypatch) -> None:
    """Kill-switch: OFF keeps the defect byte-exactly (wholesale inherit of
    all 10, no bucket row, no new telemetry keys) — the fallback path the
    flag protects is alive."""
    monkeypatch.delenv(_B77, raising=False)
    devs, bucket, spec, core, story = _giant_world()
    ufs, tele = _build_user_flows([spec], [bucket], devs, [])
    (journey,) = ufs
    assert journey.member_count == 10          # the mass transfer itself
    assert "uf_residual_cited_refused" not in tele
    assert "uf_residual_buckets_kept" not in tele
    monkeypatch.setenv(_B77, "0")
    d2, b2, s2, _c2, _s2 = _giant_world()
    ufs2, tele2 = _build_user_flows([s2], [b2], d2, [])
    assert [u.model_dump() for u in ufs] == [u.model_dump() for u in ufs2]
    assert tele == tele2


def test_nonresidual_citation_inherits_unchanged(monkeypatch) -> None:
    """Legitimate from_flows citations of NON-residual rows are untouched
    by the armed flag — wholesale inherit exactly as shipped."""
    monkeypatch.setenv(_B77, "1")
    devs, bucket, spec, _core, _story = _giant_world()
    bucket.residual = False                   # same row, not a bucket
    ufs, tele = _build_user_flows([spec], [bucket], devs, [])
    (journey,) = ufs
    assert journey.member_count == 10
    assert "uf_residual_cited_refused" not in tele
    assert "uf_residual_buckets_kept" not in tele


# ── Seg 2 — Pass-1 container-affinity gate (utok symmetry) ───────────────


def _container_world():
    """5 members homed at ws-container 'twenty-front' on a cited UF: 2
    share content tokens with the journey ('task'), 3 do not."""
    hit = [
        _flow("create-task-flow", "packages/twenty-front/src/t/create.tsx"),
        _flow("edit-task-flow", "packages/twenty-front/src/t/edit.tsx"),
    ]
    miss = [
        _flow("render-widget-flow", "packages/twenty-front/src/w/w1.tsx"),
        _flow("toggle-theme-flow", "packages/twenty-front/src/w/w2.tsx"),
        _flow("resize-panel-flow", "packages/twenty-front/src/w/w3.tsx"),
    ]
    flows = hit + miss
    d_front = _dev("front-shell", "twenty-front",
                   [f.entry_point_file for f in flows], flows)
    old = _uf("UF-010", "Front shell", "twenty-front",
              [f.uuid for f in flows], resource="task")
    spec = {
        "name": "Manage tasks", "resource": "task",
        "product_feature": "Tasks",
        "from_flows": ["UF-010"], "from_dev_features": [],
    }
    return [d_front], old, spec, hit, miss


def test_pass1_container_affinity_gate(monkeypatch) -> None:
    monkeypatch.setenv(_SEGC, "1")
    monkeypatch.setenv(_B77, "1")
    devs, old, spec, hit, miss = _container_world()
    ufs, tele = _build_user_flows(
        [spec], [old], devs, [], home_pure=True,
        container_pf_keys=frozenset({"twenty-front"}))
    (journey,) = ufs
    assert journey.member_flow_ids == [f.uuid for f in hit]
    assert tele["uf_container_affinity_blocked"] == 3
    assert tele["uf_home_container_inherited"] == 2
    # Blocked-by-affinity is a packaging reservation, NOT foreignness.
    assert tele["uf_home_filtered"] == 0


def test_container_inherit_without_b77_is_untouched(monkeypatch) -> None:
    """The B74 Seg C baseline survives: with B77 unset the container
    inherit stays ungated (all 5 members) and no affinity key appears."""
    monkeypatch.setenv(_SEGC, "1")
    monkeypatch.delenv(_B77, raising=False)
    devs, old, spec, hit, miss = _container_world()
    ufs, tele = _build_user_flows(
        [spec], [old], devs, [], home_pure=True,
        container_pf_keys=frozenset({"twenty-front"}))
    (journey,) = ufs
    assert journey.member_count == 5
    assert "uf_container_affinity_blocked" not in tele
    assert tele["uf_home_container_inherited"] == 5


def test_anticase_sign_in_and_authenticate_survives(monkeypatch) -> None:
    """ANTI-CASE (spec survivor): the twenty client+server journey — every
    member content-overlaps the journey ('sign'/'auth'), so the affinity
    gate passes all 11 and the B74 rescue shape is intact under B77."""
    monkeypatch.setenv(_SEGC, "1")
    monkeypatch.setenv(_B77, "1")
    front = [
        _flow(f"sign-in-step-{i}-flow",
              f"packages/twenty-front/src/auth/step{i}.tsx")
        for i in range(8)
    ]
    server = [
        _flow(f"auth-api-{i}-flow",
              f"packages/twenty-server/src/auth/api{i}.ts")
        for i in range(3)
    ]
    d_auth = _dev("auth", "twenty-front",
                  [f.entry_point_file for f in front], front)
    d_server = _dev("twenty-server", "twenty-server",
                    [f.entry_point_file for f in server], server)
    flows = front + server
    old = _uf("UF-001", "Sign in", "twenty-front",
              [f.uuid for f in flows], resource="auth")
    spec = {
        "name": "Sign in and authenticate", "resource": "auth",
        "product_feature": "Auth",
        "from_flows": ["UF-001"], "from_dev_features": ["auth"],
    }
    ufs, tele = _build_user_flows(
        [spec], [old], [d_auth, d_server], [], home_pure=True,
        container_pf_keys=frozenset({"twenty-front", "twenty-server"}))
    (uf,) = ufs
    assert uf.name == "Sign in and authenticate"
    assert uf.member_flow_ids == [f.uuid for f in flows]
    assert uf.member_count == 11
    assert "uf_container_affinity_blocked" not in tele
    assert tele["uf_home_filtered"] == 0


def test_anticase_cited_dev_channel_untouched(monkeypatch) -> None:
    """ANTI-CASE: the Seg-C Pass-2a cited-dev rescue (novu 'Authenticate
    CLI device session' shape) fires identically under armed B77 — an
    empty-from_flows journey still grounds via its cited dev's
    token-overlapping unclaimed flows."""
    monkeypatch.setenv(_SEGC, "1")
    monkeypatch.setenv(_B77, "1")
    fl = _flow("authenticate-cli-device-flow",
               "packages/cli/src/device-auth.ts")
    d_cli = _dev("cli-auth", "twenty-front", [fl.entry_point_file], [fl])
    spec = {
        "name": "Authenticate CLI device session", "resource": "device",
        "product_feature": "CLI",
        "from_flows": [], "from_dev_features": ["cli-auth"],
    }
    ufs, tele = _build_user_flows(
        [spec], [], [d_cli], [], home_pure=True,
        container_pf_keys=frozenset({"twenty-front"}))
    (uf,) = ufs
    assert uf.member_flow_ids == [fl.uuid]
    assert tele["uf_dev_grounded"] == 1


# ── Seg 3 — mint-side domain carve ───────────────────────────────────────


def _carve_world(billing_files: int = 2, report_files: int = 2):
    b_flows = [
        _flow(f"pay-invoice-{i}-flow", f"billing/f{i}.ts")
        for i in range(billing_files)
    ]
    r_flows = [
        _flow(f"export-report-{i}-flow", f"reports/g{i}.ts")
        for i in range(report_files)
    ]
    orphan = _flow("misc-helper-flow", "zother/helper.ts")
    d_b = _dev("billing", "billing",
               [f.entry_point_file for f in b_flows], b_flows + [orphan])
    d_r = _dev("reports", "reports",
               [f.entry_point_file for f in r_flows], r_flows)
    devs = [d_b, d_r]
    dev_to_product = {"billing": ("billing",), "reports": ("reports",)}
    pfs = [_pf("billing", "Billing"), _pf("reports", "Reports")]
    members = ([f.uuid for f in b_flows] + [f.uuid for f in r_flows]
               + [orphan.uuid])
    uf = _uf("UF-001", "Manage records", "billing", members)
    return devs, dev_to_product, pfs, uf, b_flows, r_flows, orphan


def test_domain_carve_splits_split_vote_uf(monkeypatch) -> None:
    monkeypatch.setenv(_B77, "1")
    devs, d2p, pfs, uf, b_flows, r_flows, orphan = _carve_world()
    new_ufs = [uf]
    tele: dict = {}
    _carve_multi_domain_ufs(new_ufs, devs, pfs, d2p, frozenset(), tele)
    assert tele["uf_domain_carved"] == 1
    assert tele["uf_domain_carve_children"] == 1
    assert len(new_ufs) == 2
    # Dominant home keeps the row (tie 2-2 → lexicographic: billing) and
    # the vote-less orphan stays with it (no-orphan), original order kept.
    assert uf.member_flow_ids == [f.uuid for f in b_flows] + [orphan.uuid]
    child = new_ufs[1]
    assert child.member_flow_ids == [f.uuid for f in r_flows]
    assert child.product_feature_id == "reports"
    assert child.name == uf.name


def test_anticase_majority_home_never_carved(monkeypatch) -> None:
    """ANTI-CASE ('Create and edit records' 86m / tasks 34m proxy): a
    composite with a strict-majority home is a journey, not a
    mass-transfer — untouched."""
    monkeypatch.setenv(_B77, "1")
    devs, d2p, pfs, uf, b_flows, r_flows, _o = _carve_world(
        billing_files=3, report_files=1)
    before = list(uf.member_flow_ids)
    new_ufs = [uf]
    tele: dict = {}
    _carve_multi_domain_ufs(new_ufs, devs, pfs, d2p, frozenset(), tele)
    assert new_ufs == [uf]
    assert uf.member_flow_ids == before
    assert "uf_domain_carved" not in tele


def test_anticase_container_homes_never_carve(monkeypatch) -> None:
    """ANTI-CASE ('Sign in and authenticate' client+server shape): homes
    that are ws-CONTAINERS are packaging, not domains — a journey spanning
    two containers is never split."""
    monkeypatch.setenv(_B77, "1")
    devs, d2p, pfs, uf, *_rest = _carve_world()
    before = list(uf.member_flow_ids)
    new_ufs = [uf]
    tele: dict = {}
    _carve_multi_domain_ufs(
        new_ufs, devs, pfs, d2p, frozenset({"billing", "reports"}), tele)
    assert new_ufs == [uf]
    assert uf.member_flow_ids == before
    assert "uf_domain_carved" not in tele


def test_carve_exempts_residual_buckets_and_synth(monkeypatch) -> None:
    monkeypatch.setenv(_B77, "1")
    devs, d2p, pfs, uf, *_rest = _carve_world()
    uf.residual = True
    synth = _uf("UF-002", "Backstop row", "billing", [])
    synth.synthesized = True
    new_ufs = [uf, synth]
    tele: dict = {}
    _carve_multi_domain_ufs(new_ufs, devs, pfs, d2p, frozenset(), tele)
    assert len(new_ufs) == 2
    assert "uf_domain_carved" not in tele


# ── Seg 4 — container is not a valid resettle target ─────────────────────


def _member(paths: list[str], entry: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        nodes=None, line_ranges=None, paths=paths,
        entry_point_file=entry or paths[0],
    )


def test_conserved_pfid_container_not_a_target() -> None:
    owner = {"c1": "twenty-front", "c2": "twenty-front", "c3": "twenty-front",
             "l1": "logic-functions"}
    members = [_member(["c1", "c2", "c3"]), _member(["l1"])]
    # Shipped ladder (no container set): the container wins the argmax —
    # the documented 502M bypass.
    chosen, moved = conserved_pfid(members, owner, "logic-functions")
    assert (chosen, moved) == ("twenty-front", True)
    # B77 Seg 4: the container is excluded from the rung-3 pool — the
    # journey stays on the real PF its own code supports.
    chosen, moved = conserved_pfid(
        members, owner, "logic-functions",
        container_pf_keys=frozenset({"twenty-front"}))
    assert (chosen, moved) == ("logic-functions", False)


def test_conserved_pfid_all_container_falls_to_rung4() -> None:
    owner = {"c1": "twenty-front", "c2": "twenty-front"}
    members = [_member(["c1", "c2"])]
    keys = frozenset({"twenty-front"})
    # Incumbent kept for the richer downstream ladders.
    assert conserved_pfid(
        members, owner, "logic-functions", container_pf_keys=keys,
    ) == ("logic-functions", False)
    # Finalize mode nulls a shared incumbent (existing doctrine).
    assert conserved_pfid(
        members, owner, "shared-platform",
        null_shared_without_signal=True, container_pf_keys=keys,
    ) == (None, True)


def test_apply_uf_conservation_gates_containers_under_flag(
        monkeypatch) -> None:
    m1 = _flow("front-blob-flow", "packages/twenty-front/src/a.tsx")
    m1b = _flow("front-blob-2-flow", "packages/twenty-front/src/b.tsx")
    m2 = _flow("run-logic-flow", "src/logic/run.ts")
    d_front = _dev("front-blob", "twenty-front",
                   [m1.entry_point_file, m1b.entry_point_file], [m1, m1b])
    d_logic = _dev("logic", "logic-functions", [m2.entry_point_file], [m2])
    pf_cont = _pf("twenty-front", "Twenty Front",
                  anchor="ws:packages/twenty-front")
    pf_real = _pf("logic-functions", "Logic Functions")

    def _world():
        return _uf("UF-001", "Create and run logic functions",
                   "logic-functions", [m1.uuid, m1b.uuid, m2.uuid])

    # Flag OFF — the ladder resettles onto the ws-container (the defect).
    monkeypatch.delenv(_B77, raising=False)
    uf_off = _world()
    tele_off = apply_uf_conservation(
        [uf_off], [d_front, d_logic], [pf_cont, pf_real])
    assert uf_off.product_feature_id == "twenty-front"
    assert tele_off["resettled"] == 1
    # Flag ON — the container is not a valid target; incumbent stands.
    monkeypatch.setenv(_B77, "1")
    uf_on = _world()
    tele_on = apply_uf_conservation(
        [uf_on], [d_front, d_logic], [pf_cont, pf_real])
    assert uf_on.product_feature_id == "logic-functions"
    assert tele_on["resettled"] == 0
