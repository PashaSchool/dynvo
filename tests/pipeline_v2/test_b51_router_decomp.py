"""B51 — Stage 6.985 transport router-mega decomposition.

The class (FB-04 escalation, operator 2026-07-13): cal.com `trpc` is a
FLOW-BEARING transport monolith — one dev carrying 66 flows / 811 files
(the whole `packages/trpc/server/routers/**` tree). I9 correctly forbids
laning a flowful dev, so the B48/B22 transport-lane machinery keeps the
`trpc` product tile alive.

The mechanism (decomposition pass, flag-gated default OFF, BEFORE the
conservation gate): each sub-router group whose namespace token echoes an
EXISTING product PF (the SAME `NamespaceEcho` matcher as r2.6) has its
flows + routers-tree files carved into a product-owned chunk re-homed to
that PF (I22 marker). The carved files are LIFTED out of the lane so the
existing r1 ladder drains their journeys (no new rung). Residue (unmatched
sub-routers + non-routers `[trpc].ts` handler / middleware flows) stays
flowful and holds a REDUCED tile — the honest Option-B abstain, never
forced.

Anti-cases (spec §SACRED + §Гейти-1):
  * flag OFF ⇒ pass inert, output byte-identical.
  * a FLOWLESS candidate (documenso trpc) ⇒ no-op, byte-identical.
  * a full-drain candidate lanes; its journeys re-home to product PFs.
  * residue-flowful ⇒ candidate keeps its (reduced) tile — never forced.
  * an ambiguous sub-router token (>1 PF) ⇒ residue.
  * a generic token (no product PF) ⇒ residue.
  * conservation: Σflows / ΣUF identical (moved, never dropped or minted).
  * lineage: a re-homed flow keeps its uuid (flow-dup LAW untouched).
"""

from __future__ import annotations

import pytest

from faultline.pipeline_v2.transport_handoff import (
    TRANSPORT_ROUTER_DECOMP_ENV,
    TargetGrainIndex,
    _flow_subrouter_disc,
    run_transport_handoff,
    transport_router_decomp_enabled,
)

UNIT = "packages/trpc"


# ── scene stubs (mirror test_b49_namespace_echo) ─────────────────────────


class Dev:
    def __init__(self, name, pfid, paths, flows=()):
        from datetime import datetime, timezone
        self.name = name
        self.uuid = f"dev-{name}"
        self.layer = "developer"
        self.product_feature_id = pfid
        self.paths = list(paths)
        self.member_files = []
        self.flows = list(flows)
        self.shared_reason = None
        self.anchor_id = None
        self.authors = []
        self.total_commits = 0
        self.bug_fixes = 0
        self.coverage_pct = None
        self.last_modified = datetime.fromtimestamp(0, timezone.utc)
        self.health_score = 0.0


class PF:
    def __init__(self, name, anchor_id):
        self.name = name
        self.uuid = f"pf-{name}"
        self.layer = "product"
        self.anchor_id = anchor_id
        self.paths = []


class Fl:
    def __init__(self, uuid, ranges, ep=None, name=None):
        self.uuid = uuid
        self.name = name or uuid
        self.id = uuid
        self.primary_feature = "trpc"
        self.entry_point_file = ep
        self.line_ranges = [
            {"path": p, "start_line": 1, "end_line": n} for p, n in ranges
        ]
        self.paths = [p for p, _n in ranges]


class UF:
    def __init__(self, id, name, pfid, members=()):
        self.id = id
        self.name = name
        self.product_feature_id = pfid
        self.member_flow_ids = list(members)
        self.member_count = len(self.member_flow_ids)
        self.routes = []


class Ctx:
    repo_path = "."
    tracked_files = []


class NoConsumers:
    """r2 stub — nothing resolves through consumers (the typed-proxy
    reality: web code never imports the router files)."""

    cutoff = 10

    def importers_of(self, path):
        return frozenset()

    def unit_file_consumers(self, path):
        return frozenset()


_DEFAULT_PFS = [
    ("api-keys", "route:app/routes/settings/api-keys"),
    ("event-types", "route:app/routes/event-types"),
    ("webhooks", "route:app/routes/settings/webhooks"),
]


def _rf(domain, n=80):
    """A routers-tree file path under the cal.com viewer grouping router."""
    return f"{UNIT}/server/routers/viewer/{domain}/{domain}.ts"


def _handler(domain):
    """An `api/trpc/*/[trpc].ts` transport handler file (residue-by-spec)."""
    return f"apps/web/pages/api/trpc/{domain}/[trpc].ts"


def _scene(dev_flows, ufs, product_pfs=None, extra_pfs=(), extra_paths=()):
    pfs = [PF("trpc", f"ws:{UNIT}")]
    pfs += [PF(n, a) for n, a in (product_pfs or _DEFAULT_PFS)]
    pfs += list(extra_pfs)
    paths = set(extra_paths)
    for fl in dev_flows:
        paths.update(fl.paths)
    trpc_dev = Dev("trpc", "trpc", sorted(paths), flows=list(dev_flows))
    devs = [trpc_dev]
    grain = TargetGrainIndex(
        [], pfs, routes_index=[], excluded_units=[UNIT],
        candidate_pf_keys={"trpc"})
    return devs, pfs, list(ufs), list(dev_flows), grain, trpc_dev


def _run(devs, pfs, ufs, flows, grain):
    return run_transport_handoff(
        devs, pfs, ufs, flows, [], Ctx(),
        {UNIT: "S1-transport:name-dep"},
        grain_index=grain,
        consumer_index_factory=lambda unit: NoConsumers(),
    )


def _total_flows(devs):
    return sum(len(getattr(d, "flows", []) or []) for d in devs)


# ── flag ──────────────────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv(TRANSPORT_ROUTER_DECOMP_ENV, raising=False)
    assert transport_router_decomp_enabled() is False


def test_flag_on(monkeypatch):
    monkeypatch.setenv(TRANSPORT_ROUTER_DECOMP_ENV, "1")
    assert transport_router_decomp_enabled() is True


# ── grain: routers hole vs residue ────────────────────────────────────────


def test_grain_disc_for_routers_file():
    fl = Fl("f", [(_rf("apiKeys"), 80)], ep=_rf("apiKeys"))
    assert _flow_subrouter_disc(fl) == "apiKeys"


def test_grain_residue_for_handler_file():
    # `api/trpc/apiKeys/[trpc].ts` has no `routers` segment → residue.
    fl = Fl("f", [(_handler("apiKeys"), 1)], ep=_handler("apiKeys"))
    assert _flow_subrouter_disc(fl) is None


def test_grain_residue_when_span_two_subrouters():
    # deepest common hole across two sub-routers is transparent `viewer`
    # → no single discriminating hole → residue.
    fl = Fl("f", [(_rf("apiKeys"), 40), (_rf("webhook"), 40)])
    assert _flow_subrouter_disc(fl) is None


# ── full drain: all flows matched ⇒ trpc lanes ────────────────────────────


def test_full_drain_lanes_when_all_matched(monkeypatch):
    monkeypatch.setenv(TRANSPORT_ROUTER_DECOMP_ENV, "1")
    fa = Fl("f-apikeys", [(_rf("apiKeys"), 80)], ep=_rf("apiKeys"),
            name="manage-api-keys")
    fe = Fl("f-events", [(_rf("eventTypes"), 80)], ep=_rf("eventTypes"),
            name="manage-event-types")
    ua = UF("UF-1", "Manage API keys", "trpc", members=["f-apikeys"])
    ue = UF("UF-2", "Manage event types", "trpc", members=["f-events"])
    devs, pfs, ufs, flows, grain, dev = _scene([fa, fe], [ua, ue])
    tele = _run(devs, pfs, ufs, flows, grain)

    # trpc left the product layer (fully drained).
    assert [row["unit"] for row in tele["laned"]] == [UNIT]
    assert all(pf.name != "trpc" for pf in pfs)
    # journeys re-homed to their product PFs (drained via the r1 lift).
    assert {u.id: u.product_feature_id for u in ufs} == {
        "UF-1": "api-keys", "UF-2": "event-types"}
    # decomp telemetry: two matched sub-routers, no residue, nothing minted.
    rd = tele["router_decomp"][UNIT]
    assert rd["matched"] == {"api-keys": ["apiKeys"],
                             "event-types": ["eventTypes"]}
    assert rd["residue_flows"] == 0 and rd["flows_moved"] == 2
    assert tele["pfs_minted"] == 0


# ── residue flowful ⇒ ABSTAIN, byte-identical (all-or-nothing) ────────────


def test_residue_flowful_abstains_byte_identical(monkeypatch):
    """A candidate that does NOT fully drain (a matched sub-router + a
    residue `[trpc].ts` handler flow — cal.com's shape) ABSTAINS entirely:
    no carve, no telemetry, output byte-identical. A partial re-home would
    orphan a flowful product PF with no journey (validator I8) — refuted by
    the keyed gate — so the whole tile stays."""
    monkeypatch.setenv(TRANSPORT_ROUTER_DECOMP_ENV, "1")
    fa = Fl("f-apikeys", [(_rf("apiKeys"), 80)], ep=_rf("apiKeys"),
            name="manage-api-keys")
    fh = Fl("f-handler", [(_handler("admin"), 1)], ep=_handler("admin"),
            name="manage-admin-by-trpc")
    ua = UF("UF-1", "Manage API keys", "trpc", members=["f-apikeys"])
    uh = UF("UF-H", "Admin handler", "trpc", members=["f-handler"])
    devs, pfs, ufs, flows, grain, dev = _scene([fa, fh], [ua, uh])
    tele = _run(devs, pfs, ufs, flows, grain)

    # trpc STAYS a whole product tile; NOTHING moved (abstain).
    assert tele["laned"] == []
    assert any(pf.name == "trpc" for pf in pfs)
    assert "router_decomp" not in tele            # no telemetry on abstain
    # NO orphan chunk: the matched apiKeys flow was NOT re-homed (which
    # would have left `api-keys` flowful-but-journeyless → I8).
    assert not any("router-decomp" in str(d.name) for d in devs)
    assert {f.uuid for f in dev.flows} == {"f-apikeys", "f-handler"}
    assert ufs[0].product_feature_id == "trpc"


# ── ambiguous token ⇒ residue ─────────────────────────────────────────────


def test_ambiguous_disc_is_residue(monkeypatch):
    monkeypatch.setenv(TRANSPORT_ROUTER_DECOMP_ENV, "1")
    # two product PFs share the anchor-terminal identity `billing`
    # (`_core_identity` = {terminal, name}) → the token hits >1 PF →
    # unmatched → residue → the candidate does not fully drain → abstain.
    extra = [PF("billing", "route:app/billing"),
             PF("billing-legacy", "route:app/legacy/billing")]
    fb = Fl("f-billing", [(_rf("billing"), 80)], ep=_rf("billing"),
            name="manage-billing")
    ub = UF("UF-B", "Billing", "trpc", members=["f-billing"])
    devs, pfs, ufs, flows, grain, dev = _scene(
        [fb], [ub], extra_pfs=extra)
    tele = _run(devs, pfs, ufs, flows, grain)

    assert "router_decomp" not in tele               # abstain, no telemetry
    assert not any("router-decomp" in str(d.name) for d in devs)
    assert ufs[0].product_feature_id == "trpc"       # untouched
    assert any(pf.name == "trpc" for pf in pfs)


# ── generic token ⇒ residue ───────────────────────────────────────────────


def test_generic_disc_is_residue(monkeypatch):
    monkeypatch.setenv(TRANSPORT_ROUTER_DECOMP_ENV, "1")
    fg = Fl("f-gen", [(_rf("helpers"), 80)], ep=_rf("helpers"),
            name="helper")
    ug = UF("UF-G", "Generic", "trpc", members=["f-gen"])
    devs, pfs, ufs, flows, grain, dev = _scene([fg], [ug])
    tele = _run(devs, pfs, ufs, flows, grain)

    # generic token (no product PF) → unmatched → residue → abstain.
    assert "router_decomp" not in tele
    assert not any("router-decomp" in str(d.name) for d in devs)
    assert any(pf.name == "trpc" for pf in pfs)


# ── flowless candidate ⇒ no-op (documenso trpc shape) ─────────────────────


def test_flowless_candidate_noop(monkeypatch):
    monkeypatch.setenv(TRANSPORT_ROUTER_DECOMP_ENV, "1")
    # candidate trpc carries NO flows (documenso: 6 paths, 0 flows).
    pfs = [PF("trpc", f"ws:{UNIT}"), PF("api-keys",
              "route:app/routes/settings/api-keys")]
    dev = Dev("trpc", "trpc", [f"{UNIT}/server/context.ts"], flows=[])
    grain = TargetGrainIndex(
        [], pfs, routes_index=[], excluded_units=[UNIT],
        candidate_pf_keys={"trpc"})
    tele = run_transport_handoff(
        [dev], pfs, [], [], [], Ctx(),
        {UNIT: "S1-transport:name-dep"}, grain_index=grain,
        consumer_index_factory=lambda unit: NoConsumers())
    # decomp is a no-op → NO telemetry key at all (byte-identical to OFF).
    assert "router_decomp" not in tele


# ── flag OFF ⇒ inert, no mutation, no telemetry ───────────────────────────


def test_flag_off_is_inert(monkeypatch):
    monkeypatch.delenv(TRANSPORT_ROUTER_DECOMP_ENV, raising=False)
    fa = Fl("f-apikeys", [(_rf("apiKeys"), 80)], ep=_rf("apiKeys"),
            name="manage-api-keys")
    ua = UF("UF-1", "Manage API keys", "trpc", members=["f-apikeys"])
    devs, pfs, ufs, flows, grain, dev = _scene([fa], [ua])
    tele = _run(devs, pfs, ufs, flows, grain)

    assert "router_decomp" not in tele
    # the flow stays on the candidate dev; trpc keeps its tile (the r1
    # lift never fires → the abstaining seed cannot lane).
    assert dev.flows and dev.flows[0].uuid == "f-apikeys"
    assert ufs[0].product_feature_id == "trpc"
    assert any(pf.name == "trpc" for pf in pfs)


# ── conservation + lineage ────────────────────────────────────────────────


def test_conservation_flows_moved_not_dropped(monkeypatch):
    """Full-drain scene: ALL flows match (residue == 0) → carve applies,
    Σflows conserved (moved, not dropped or minted), lineage preserved."""
    monkeypatch.setenv(TRANSPORT_ROUTER_DECOMP_ENV, "1")
    fa = Fl("f-apikeys", [(_rf("apiKeys"), 80)], ep=_rf("apiKeys"),
            name="manage-api-keys")
    fe = Fl("f-events", [(_rf("eventTypes"), 80)], ep=_rf("eventTypes"),
            name="manage-event-types")
    ua = UF("UF-1", "Manage API keys", "trpc", members=["f-apikeys"])
    ue = UF("UF-2", "Manage event types", "trpc", members=["f-events"])
    devs, pfs, ufs, flows, grain, dev = _scene([fa, fe], [ua, ue])
    before = _total_flows(devs)
    tele = _run(devs, pfs, ufs, flows, grain)
    after = _total_flows(devs)

    # Σflows conserved (moved into chunks, none dropped or minted).
    assert before == after == 2
    assert len(ufs) == 2                              # ΣUF conserved
    # lineage: the re-homed flow keeps its uuid (flow-dup LAW untouched).
    chunk = next(d for d in devs if d.product_feature_id == "api-keys")
    assert {f.uuid for f in chunk.flows} == {"f-apikeys"}
    # the moved flow's id is re-stamped onto the chunk name (bipartite id).
    assert chunk.flows[0].id.startswith(chunk.name + "::")


def test_abstain_conserves_and_leaves_flows_in_place(monkeypatch):
    """When the pass abstains (residue > 0) NOTHING moves: Σflows conserved
    on the ORIGINAL dev, no chunk, no orphaned product PF."""
    monkeypatch.setenv(TRANSPORT_ROUTER_DECOMP_ENV, "1")
    fa = Fl("f-apikeys", [(_rf("apiKeys"), 80)], ep=_rf("apiKeys"),
            name="manage-api-keys")
    fh = Fl("f-handler", [(_handler("admin"), 1)], ep=_handler("admin"),
            name="manage-admin")
    ua = UF("UF-1", "Manage API keys", "trpc", members=["f-apikeys"])
    uh = UF("UF-H", "Admin", "trpc", members=["f-handler"])
    devs, pfs, ufs, flows, grain, dev = _scene([fa, fh], [ua, uh])
    tele = _run(devs, pfs, ufs, flows, grain)

    assert _total_flows(devs) == 2                    # nothing dropped
    assert {f.uuid for f in dev.flows} == {"f-apikeys", "f-handler"}
    # the matched `api-keys` PF gained NO flow → no I8 orphan.
    assert not any(d.product_feature_id == "api-keys" for d in devs)


def test_no_new_product_feature_minted(monkeypatch):
    monkeypatch.setenv(TRANSPORT_ROUTER_DECOMP_ENV, "1")
    fa = Fl("f-apikeys", [(_rf("apiKeys"), 80)], ep=_rf("apiKeys"),
            name="manage-api-keys")
    fe = Fl("f-events", [(_rf("eventTypes"), 80)], ep=_rf("eventTypes"),
            name="manage-event-types")
    ua = UF("UF-1", "Manage API keys", "trpc", members=["f-apikeys"])
    ue = UF("UF-2", "Manage event types", "trpc", members=["f-events"])
    devs, pfs, ufs, flows, grain, dev = _scene([fa, fe], [ua, ue])
    names_before = {pf.name for pf in pfs}
    tele = _run(devs, pfs, ufs, flows, grain)

    assert tele["pfs_minted"] == 0
    # only the candidate row left; no product PF was created.
    assert {pf.name for pf in pfs} == names_before - {"trpc"}


@pytest.fixture(autouse=True)
def _b62_pin_flowful_transport_lane(monkeypatch):
    """B62 flip isolation: FAULTLINE_FLOWFUL_TRANSPORT_LANE defaults ON since KEY_SCHEMA 29; this
    module tests the pre-B52 Option-B decomp world, so the flipped co-flag is pinned OFF
    (same mechanical pattern as the b50/b57/b61 rung-isolation fixtures)."""
    monkeypatch.setenv("FAULTLINE_FLOWFUL_TRANSPORT_LANE", "0")
