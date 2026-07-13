"""B49 — Stage 6.985 r2.6 transport namespace-echo rung.

The class (operator, 2026-07-12): a name-dep tRPC transport candidate
whose homed journeys span ONLY in-lane router files cannot lane, because
tRPC is consumed through a typed proxy (``trpc.viewer.apiKeys.*``) — web
code never imports the router files, so the seeds abstain at r2
(``zero_product_votes``) and the all-or-nothing conservation gate keeps
the tech tile alive.

The mechanism (r2.6, between r2 and r3, flag-gated, default OFF): an
in-lane router seed that abstains at r2 votes its span mass for the
EXISTING product PF whose anchor-identity its namespace token echoes,
using the SAME ``normalize_anchor_key`` the S3-nav echo uses. Unique
match only; ambiguous / generic abstains; NEVER mints.

Anti-cases (spec §SACRED + §Гейти-1):
  * flag OFF ⇒ rung inert, ladder byte-identical to r1→r2→r3.
  * r1 (real span majority) is NEVER overridden by r2.6.
  * a token matching >1 PF (ambiguous) abstains — never guesses.
  * a generic token (no product PF) abstains.
  * r2.6 re-homes ONLY onto existing PFs — it mints nothing.
  * conservation stays all-or-nothing: when one journey still abstains
    (a genuine non-router orphan) the WHOLE candidate stays blocked
    even though r2.6 resolved its siblings.
"""

from __future__ import annotations

from faultline.pipeline_v2.spine_anchors import SpineAnchor
from faultline.pipeline_v2.transport_handoff import (
    TRANSPORT_NAMESPACE_ECHO_ENV,
    GrainTarget,
    NamespaceEcho,
    TargetGrainIndex,
    _ns_tokens,
    run_transport_handoff,
    transport_namespace_echo_enabled,
)

UNIT = "packages/trpc"


# ── scene stubs (mirror test_transport_handoff_b22) ──────────────────────


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
    def __init__(self, uuid, ranges, ep=None, paths=None):
        self.uuid = uuid
        self.entry_point_file = ep
        self.line_ranges = [
            {"path": p, "start_line": 1, "end_line": n} for p, n in ranges
        ]
        self.paths = list(paths) if paths is not None else \
            [p for p, _n in ranges]


class UF:
    def __init__(self, id, name, pfid, members=(), routes=()):
        self.id = id
        self.name = name
        self.product_feature_id = pfid
        self.member_flow_ids = list(members)
        self.member_count = len(self.member_flow_ids)
        self.routes = list(routes)


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


# Existing product PFs whose anchor-identity the namespace tokens echo:
#   apiKeys → api-key ← api-keys PF ; eventTypes → event-type ← event-types
_PRODUCT_PFS = [
    ("api-keys", "route:app/routes/settings/api-keys"),
    ("event-types", "route:app/routes/event-types"),
    ("teams", "route:app/routes/teams"),
]


def _anchors(pf_names):
    out = [
        SpineAnchor(canonical_id=f"ws:{UNIT}", key="trpc", source="ws-pkg",
                    display="Trpc", prefixes=(UNIT,),
                    sources=frozenset({"ws-pkg"})),
    ]
    for name, anc in _PRODUCT_PFS:
        if name in pf_names:
            root = anc.split(":", 1)[1]
            out.append(SpineAnchor(
                canonical_id=anc, key=name, source="route", display=name,
                prefixes=(root,), sources=frozenset({"route"})))
    return out


def _scene(homed_ufs, flows, cand_devs=(), product_pfs=None,
           extra_pfs=()):
    """Candidate ``trpc`` (ws:packages/trpc) + the existing product PFs.
    ``homed_ufs`` are homed to ``trpc``; ``flows`` back their spans."""
    names = [n for n, _ in (product_pfs or _PRODUCT_PFS)]
    pfs = [PF("trpc", f"ws:{UNIT}")]
    pfs += [PF(n, a) for n, a in (product_pfs or _PRODUCT_PFS)]
    pfs += list(extra_pfs)
    devs = [Dev(f"trpc-router", "trpc",
                [f"{UNIT}/server/router.ts"])] + list(cand_devs)
    grain = TargetGrainIndex(
        _anchors(names + [p.name for p in extra_pfs]), pfs,
        routes_index=[], excluded_units=[UNIT],
        candidate_pf_keys={"trpc"})
    return devs, pfs, list(homed_ufs), list(flows), grain


def _run(devs, pfs, ufs, flows, grain, nav_keys=frozenset()):
    return run_transport_handoff(
        devs, pfs, ufs, flows, [], Ctx(),
        {UNIT: "S1-transport:name-dep"},
        grain_index=grain,
        consumer_index_factory=lambda unit: NoConsumers(),
        nav_keys=nav_keys,
    )


def _apikeys_scene():
    """One journey spanning ONLY the in-lane apiKeys router file."""
    fl = Fl("f-apikeys",
            [(f"{UNIT}/server/routers/viewer/apiKeys/apiKeys.ts", 80)],
            ep=f"{UNIT}/server/routers/viewer/apiKeys/apiKeys.ts")
    uf = UF("UF-001", "Manage API keys", "trpc", members=["f-apikeys"])
    return _scene([uf], [fl])


# ── extraction (pure, code-grounded, no vocabulary) ──────────────────────


def test_ns_tokens_cal_com_routers_shape():
    assert _ns_tokens(
        "packages/trpc/server/routers/viewer/apiKeys/apiKeys.tsx") \
        == ["viewer", "apiKeys"]
    # bare routers/<file> (no namespace dir) → filename stem
    assert _ns_tokens("packages/trpc/server/routers/publicViewer.tsx") \
        == ["publicViewer"]


def test_ns_tokens_create_t3_and_documenso_shapes():
    assert _ns_tokens("apps/web/server/api/routers/billing/index.ts") \
        == ["billing"]
    assert _ns_tokens("packages/trpc/server/team-router/create-team.ts") \
        == ["team"]
    # non-router file inside the package → no namespace tokens
    assert _ns_tokens("packages/trpc/utils/logger.ts") == []


# ── flag ────────────────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv(TRANSPORT_NAMESPACE_ECHO_ENV, raising=False)
    assert transport_namespace_echo_enabled() is False


def test_flag_on(monkeypatch):
    monkeypatch.setenv(TRANSPORT_NAMESPACE_ECHO_ENV, "1")
    assert transport_namespace_echo_enabled() is True


# ── the rung resolves an otherwise-abstaining journey ────────────────────


def test_namespace_echo_lanes_when_on(monkeypatch):
    monkeypatch.setenv(TRANSPORT_NAMESPACE_ECHO_ENV, "1")
    devs, pfs, ufs, flows, grain = _apikeys_scene()
    tele = _run(devs, pfs, ufs, flows, grain)

    assert [row["unit"] for row in tele["laned"]] == [UNIT]
    assert tele["ufs_rehomed"] == 1
    assert ufs[0].product_feature_id == "api-keys"          # re-homed
    assert tele["rungs"][UNIT].get("r2.6-namespace") == 1
    # existing PF only — the candidate row left, nothing minted.
    assert tele["pfs_minted"] == 0
    assert all(pf.name != "trpc" for pf in pfs)
    assert {pf.name for pf in pfs} == {"api-keys", "event-types", "teams"}
    # telemetry move map records the seed → PF echo.
    ne = tele["namespace_echo"]
    assert ne["enabled"] is True and ne["seeds_matched"] == 1
    assert ne["moves"][
        f"{UNIT}/server/routers/viewer/apiKeys/apiKeys.ts"] == "api-keys"


def test_flag_off_is_inert_and_blocks(monkeypatch):
    """The SAME scene with the flag OFF: the seed abstains
    (zero_product_votes) and the all-or-nothing gate keeps trpc alive —
    proving the rung is inert, not a behavior baked into the ladder."""
    monkeypatch.delenv(TRANSPORT_NAMESPACE_ECHO_ENV, raising=False)
    devs, pfs, ufs, flows, grain = _apikeys_scene()
    tele = _run(devs, pfs, ufs, flows, grain)

    assert tele["laned"] == []
    assert tele["ufs_rehomed"] == 0
    assert "namespace_echo" not in tele          # nothing added when OFF
    blocked = tele["conservation_blocked"][UNIT]
    assert {b["uf"]: b["reason"] for b in blocked["blocked"]} == {
        "UF-001": "zero_product_votes"}
    assert ufs[0].product_feature_id == "trpc"   # untouched
    assert any(pf.name == "trpc" for pf in pfs)


# ── r1 priority: a real span majority is NEVER overridden by r2.6 ─────────


def test_r1_strict_not_overridden(monkeypatch):
    monkeypatch.setenv(TRANSPORT_NAMESPACE_ECHO_ENV, "1")
    # journey spans a NON-lane file owned by teams (r1 strict), NOT a
    # router file — even with echo ON, r1 wins and the home is teams.
    owner = Dev("web-teams", "teams", ["app/routes/teams/page.tsx"])
    fl = Fl("f-r1", [("app/routes/teams/page.tsx", 100)],
            ep="app/routes/teams/page.tsx")
    uf = UF("UF-R1", "Manage teams", "trpc", members=["f-r1"])
    devs, pfs, ufs, flows, grain = _scene([uf], [fl], cand_devs=[owner])
    tele = _run(devs, pfs, ufs, flows, grain)

    assert ufs[0].product_feature_id == "teams"
    assert tele["rungs"][UNIT].get("r1-strict") == 1
    assert "r2.6-namespace" not in tele["rungs"][UNIT]


# ── ambiguity: a seed hitting >1 PF abstains (never guesses) ──────────────


def test_ambiguous_seed_abstains(monkeypatch):
    monkeypatch.setenv(TRANSPORT_NAMESPACE_ECHO_ENV, "1")
    # one router path nesting TWO product namespaces → tokens
    # [apiKeys, teams] hit {api-keys, teams} → ambiguous → abstain.
    fl = Fl("f-ambig",
            [(f"{UNIT}/server/routers/apiKeys/teams/x.ts", 80)],
            ep=f"{UNIT}/server/routers/apiKeys/teams/x.ts")
    uf = UF("UF-AMB", "Ambiguous", "trpc", members=["f-ambig"])
    devs, pfs, ufs, flows, grain = _scene([uf], [fl])
    tele = _run(devs, pfs, ufs, flows, grain)

    assert tele["laned"] == []                    # blocked, not guessed
    assert ufs[0].product_feature_id == "trpc"
    assert {b["reason"] for b in
            tele["conservation_blocked"][UNIT]["blocked"]} == {
        "zero_product_votes"}


# ── generic token: no product PF → abstain ────────────────────────────────


def test_generic_token_abstains(monkeypatch):
    monkeypatch.setenv(TRANSPORT_NAMESPACE_ECHO_ENV, "1")
    fl = Fl("f-gen",
            [(f"{UNIT}/server/routers/viewer/helpers/util.ts", 80)],
            ep=f"{UNIT}/server/routers/viewer/helpers/util.ts")
    uf = UF("UF-GEN", "Generic", "trpc", members=["f-gen"])
    devs, pfs, ufs, flows, grain = _scene([uf], [fl])
    tele = _run(devs, pfs, ufs, flows, grain)

    assert tele["laned"] == []
    assert ufs[0].product_feature_id == "trpc"


# ── no-mint: r2.6 never creates a new PF ──────────────────────────────────


def test_no_mint_only_existing_pf(monkeypatch):
    monkeypatch.setenv(TRANSPORT_NAMESPACE_ECHO_ENV, "1")
    devs, pfs, ufs, flows, grain = _apikeys_scene()
    n_pf_before = len({pf.name for pf in pfs}) - 1   # minus the candidate
    tele = _run(devs, pfs, ufs, flows, grain)
    assert tele["pfs_minted"] == 0
    # exactly the pre-existing product PFs survive — none created.
    assert {pf.name for pf in pfs} == {"api-keys", "event-types", "teams"}
    assert len({pf.name for pf in pfs}) == n_pf_before


# ── conservation stays all-or-nothing ─────────────────────────────────────


def test_all_or_nothing_when_a_sibling_still_abstains(monkeypatch):
    monkeypatch.setenv(TRANSPORT_NAMESPACE_ECHO_ENV, "1")
    # journey A resolves via r2.6 (apiKeys); journey B spans a genuine
    # non-router orphan with no echo and no consumer → abstains. The
    # WHOLE candidate stays blocked (no journey moves).
    fa = Fl("f-a",
            [(f"{UNIT}/server/routers/viewer/apiKeys/x.ts", 80)],
            ep=f"{UNIT}/server/routers/viewer/apiKeys/x.ts")
    fb = Fl("f-b", [(f"{UNIT}/server/context.ts", 40)],
            ep=f"{UNIT}/server/context.ts")
    ua = UF("UF-A", "Manage API keys", "trpc", members=["f-a"])
    ub = UF("UF-B", "Internal plumbing", "trpc", members=["f-b"])
    devs, pfs, ufs, flows, grain = _scene([ua, ub], [fa, fb])
    tele = _run(devs, pfs, ufs, flows, grain)

    assert tele["laned"] == []
    assert tele["ufs_rehomed"] == 0
    assert [u.product_feature_id for u in ufs] == ["trpc", "trpc"]
    reasons = {b["uf"]: b["reason"]
               for b in tele["conservation_blocked"][UNIT]["blocked"]}
    assert reasons["UF-B"] == "zero_product_votes"


# ── the echo builder never targets the dissolving candidate ──────────────


def test_builder_excludes_candidate_pf():
    pfs = [PF("trpc", f"ws:{UNIT}"), PF("api-keys",
              "route:app/routes/settings/api-keys")]
    echo = NamespaceEcho.build(pfs, excluded_pf_keys=frozenset({"trpc"}))
    # the candidate's own name never becomes a re-home target.
    assert echo.target_for(
        f"{UNIT}/server/routers/viewer/apiKeys/x.ts") == \
        GrainTarget("pf", "api-keys")
    # a token echoing the candidate itself resolves to nothing.
    assert echo.target_for(f"{UNIT}/server/routers/trpc/x.ts") is None
