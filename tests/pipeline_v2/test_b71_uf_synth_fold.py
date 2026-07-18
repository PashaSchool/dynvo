"""B71 Seg C — UF synth laws L-C1..L-C4 (``FAULTLINE_NAMING_PACK``).

Named units for every census-class-C exhibit + the MANDATORY anti-case-direction
(fold the echo INTO the rich canonical; the rich journey survives, never both).
Fixtures synthetic; the corpus name-census (67 echo/dup families) is the
operator's keyed A/B. Verb set = the engine's OWN template vocab (not a new list).
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Flow, FlowLineRange, UserFlow
from faultline.pipeline_v2.naming_contract import _verb_class_tokens, load_naming_vocab
from faultline.pipeline_v2.uf_synth_fold import (
    apply_uf_synth_fold,
    has_verb_stutter,
    is_malformed_phrase,
    is_pf_echo,
    plan_uf_synth,
    repair_stutter,
    spans_overlap,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_V = _verb_class_tokens(load_naming_vocab())


def _uf(uf_id: str, name: str, *, pf: str, resource: str = "", members: list[str] | None = None) -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, product_feature_id=pf, intent="manage",
        resource=resource or name.split()[-1].lower(),
        member_flow_ids=members or [uf_id + "-m"], member_count=1,
    )


def _pf(name: str, display: str | None = None) -> object:
    from types import SimpleNamespace
    return SimpleNamespace(name=name, display_name=display or name)


def _flow(uuid: str, ranges: list[tuple[str, int, int]]) -> Flow:
    fl = Flow(
        name=uuid, uuid=uuid, paths=[], authors=[], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_NOW, health_score=90.0,
    )
    fl.line_ranges = [FlowLineRange(path=p, start_line=s, end_line=e) for p, s, e in ranges]
    return fl


# ── L-C1 echo-fold + anti-case-direction ──────────────────────────────────────


def test_lc1_documenso_bare_noun_folds_into_rich() -> None:
    """documenso ``User`` (bare noun) + ``Manage users`` (verbful) under the Users
    PF: the bare noun FOLDS INTO ``Manage users``; the rich journey survives."""
    rich = _uf("UF-1", "Manage users", pf="users", resource="user")
    bare = _uf("UF-2", "User", pf="users", resource="user")
    plan = plan_uf_synth([rich, bare], [], [_pf("users", "Users")], _V)
    assert plan.fold == {"UF-2": "UF-1"}          # bare dies, rich survives
    assert plan.reasons["UF-2"] == "lc1_pf_echo"
    assert plan.survivors([rich, bare]) == [rich]


def test_lc1_teams_and_teams_team_fold() -> None:
    """documenso Teams-family: bare ``Teams`` folds into ``Manage teams``."""
    rich = _uf("UF-1", "Manage teams", pf="teams", resource="team")
    bare = _uf("UF-2", "Teams", pf="teams", resource="team")
    plan = plan_uf_synth([rich, bare], [], [_pf("teams", "Teams")], _V)
    assert plan.fold == {"UF-2": "UF-1"}


def test_lc1_plane_view_propel_and_cal_manage_di_fold() -> None:
    """plane ``View propel`` / cal ``Manage di`` are verbful echoes; each folds
    into a richer sibling of its PF when one exists."""
    for pf_slug, disp, echo_name in [("propel", "Propel", "View propel"),
                                     ("di", "Di", "Manage di")]:
        rich = _uf("UF-1", f"Configure {disp.lower()} pipeline", pf=pf_slug, resource=pf_slug)
        echo = _uf("UF-2", echo_name, pf=pf_slug, resource=pf_slug)
        plan = plan_uf_synth([rich, echo], [], [_pf(pf_slug, disp)], _V)
        assert plan.fold.get("UF-2") == "UF-1", echo_name


def test_lc1_lone_echo_kept_conservation() -> None:
    """ANTI-CASE: a PF whose ONLY journey is a lone echo KEEPS it — never kill the
    last journey (conservation)."""
    only = _uf("UF-1", "User", pf="users", resource="user")
    plan = plan_uf_synth([only], [], [_pf("users", "Users")], _V)
    assert plan.fold == {}
    assert plan.survivors([only]) == [only]


def test_lc1_never_folds_the_rich_canonical() -> None:
    """ANTI-CASE (census §4): ``Manage users`` / ``Complete document signing`` /
    ``Create & edit templates`` are canonical rich UFs — never folded."""
    rich_names = ["Manage users", "Complete document signing", "Create & edit templates"]
    ufs = [_uf(f"UF-{i}", nm, pf="docs", resource="document") for i, nm in enumerate(rich_names)]
    plan = plan_uf_synth(ufs, [], [_pf("docs", "Documents")], _V)
    for u in ufs:
        assert u.id not in plan.fold, u.name


# ── REFUTATION regression (live documenso census, 2026-07-16) ─────────────────


def test_echo_predicate_never_matches_own_resource() -> None:
    """ROOT CAUSE of the inverted fold: the UF's own ``resource`` ('user') is
    derived from its own name, so comparing against it made EVERY 'Manage
    <noun>' row an echo. The predicate reads the PF display ONLY."""
    assert is_pf_echo("Manage users", "Admin", _V) is False     # was True via res
    assert is_pf_echo("Manage users", "Users", _V) is True      # true PF echo
    assert is_pf_echo("Manage webhooks", "Settings", _V) is False
    assert is_pf_echo("User", "Users", _V) is True


def test_live_documenso_rich_names_survive_by_name() -> None:
    """MANDATE unit: the rich canonical rows the armed board LOST (live base
    shapes: PF 'admin+' display 'Admin', PF 'settings+' display 'Settings', PF
    't.$team-url+' display 'Team', with each row's OWN live resource) all
    survive by name; zero folds fire on these groups."""
    admin = [
        _uf("UF-004", "Enterprise Feature Restrictions", pf="admin+", resource="claim", members=["a1", "a2", "a3"]),
        _uf("UF-L-0a", "Manage documents", pf="admin+", resource="document", members=["d1", "d2", "d3"]),
        _uf("UF-L-0f", "Manage site settings", pf="admin+", resource="site setting", members=["s1"]),
        _uf("UF-L-52", "Manage teams", pf="admin+", resource="team id", members=["t1"]),
        _uf("UF-L-58", "Manage email domains", pf="admin+", resource="email domain", members=["e1", "e2"]),
        _uf("UF-L-ac", "Manage organisations", pf="admin+", resource="organisation", members=["o1", "o2", "o3", "o4"]),
        _uf("UF-L-bb", "Manage users", pf="admin+", resource="user", members=["u1", "u2"]),
    ]
    settings = [
        _uf("UF-023", "Envelope Expiration", pf="settings+", resource="billing", members=[f"b{i}" for i in range(10)]),
        _uf("UF-L-19", "Manage webhooks", pf="settings+", resource="webhook", members=["w1", "w2"]),
        _uf("UF-L-82", "Manage security", pf="settings+", resource="security", members=["s1", "s2", "s3", "s4", "s5"]),
    ]
    team = [
        _uf("UF-022", "Manage item", pf="t.$team-url+", resource="item", members=["i1", "i2"]),
        _uf("UF-021", "Browse & filter items", pf="t.$team-url+", resource="item", members=["i3"]),
    ]
    ufs = admin + settings + team
    pfs = [_pf("admin+", "Admin"), _pf("settings+", "Settings"), _pf("t.$team-url+", "Team")]
    # members carry DISJOINT spans (the L-C3 gate must not fire here)
    flows = [_flow(m, [(f"{m}.ts", 10 + 100 * i, 20 + 100 * i)])
             for i, m in enumerate(sorted({m for u in ufs for m in u.member_flow_ids}))]
    plan = plan_uf_synth(ufs, flows, pfs, _V)
    must_survive = [
        "Manage documents", "Manage email domains", "Manage item",
        "Manage organisations", "Manage security", "Manage site settings",
        "Manage users", "Manage webhooks", "Browse & filter items",
    ]
    survivors = {u.name for u in plan.survivors(ufs)}
    for nm in must_survive:
        assert nm in survivors, nm
    assert plan.fold == {}
    assert plan.rename == {}   # zero '&' mutations


def test_live_pair_bare_never_canonical() -> None:
    """MANDATE unit (live pair shapes): {'User' (bare), 'Manage users'} under a
    Users PF -> 'Manage users' survives; same for the 'Teams' pair — the bare
    row is NEVER canonical while a rich rival is alive, whatever the member
    counts."""
    for bare_nm, rich_nm, pf_slug, disp in [
        ("User", "Manage users", "users", "Users"),
        ("Teams", "Manage teams", "teams", "Teams"),
    ]:
        # bare row gets MORE members — member count must not out-rank the mandate
        bare = _uf("UF-1", bare_nm, pf=pf_slug, resource="x",
                   members=[f"m{i}" for i in range(13)])
        rich = _uf("UF-2", rich_nm, pf=pf_slug, resource="x", members=["r1"])
        plan = plan_uf_synth([bare, rich], [], [_pf(pf_slug, disp)], _V)
        assert plan.fold == {"UF-1": "UF-2"}, (bare_nm, rich_nm)
        assert plan.survivors([bare, rich]) == [rich]


def test_ampersand_verb_phrase_verbatim() -> None:
    """MANDATE unit: 'Browse & filter emails' passes armed UNCHANGED — a
    coordinated verb phrase is verbatim; '&' is part of the phrase, and the
    stutter law never fires across a conjunction."""
    assert has_verb_stutter("Browse & filter emails", _V) is False
    assert has_verb_stutter("Browse & filter GitHub forks", _V) is False
    assert has_verb_stutter("Create and edit templates", _V) is False
    uf = _uf("UF-1", "Browse & filter emails", pf="emails", resource="email")
    ufs = [uf]
    apply_uf_synth_fold(ufs, [], [_pf("emails", "Emails")], _V)
    assert uf.name == "Browse & filter emails"   # zero mutation


def test_live_twins_fold_into_ampersand_canonical() -> None:
    """Live twin shape (the 3 legal bare folds): 'GitHub forks' (bare) +
    'Browse & filter GitHub forks' ('&'-verbful, overlapping member spans) —
    the bare twin folds INTO the '&' canonical (never min-id, never reverse)
    and the canonical keeps its literal '&' name."""
    f_rich = _flow("m-rich", [("forks.tsx", 10, 60)])
    f_bare = _flow("m-bare", [("forks.tsx", 30, 50)])  # overlaps
    # bare twin gets the SMALLER id — the old min-id anchor would invert this
    bare = _uf("UF-010", "GitHub forks", pf="t", resource="github", members=["m-bare"])
    rich = _uf("UF-016", "Browse & filter GitHub forks", pf="t", resource="github", members=["m-rich"])
    ufs = [bare, rich]
    plan = plan_uf_synth(ufs, [f_rich, f_bare], [_pf("t", "Team")], _V)
    assert plan.fold == {"UF-010": "UF-016"}
    assert plan.reasons["UF-010"] == "lc3_family_overlap"
    apply_uf_synth_fold(ufs, [f_rich, f_bare], [_pf("t", "Team")], _V)
    assert [u.name for u in ufs] == ["Browse & filter GitHub forks"]
    assert set(ufs[0].member_flow_ids) == {"m-rich", "m-bare"}  # conservation


# ── L-C2 verb-phrase integrity ────────────────────────────────────────────────


def test_lc2_novu_broken_splices_repaired() -> None:
    """novu ``Browse up Slack`` / ``Create up inbox`` / ``Manage create topic`` —
    the split multiword-verb / stray verb is repaired."""
    cases = {
        "Browse up Slack": "Browse Slack",
        "Create up inbox": "Create inbox",
        "Manage create topic": "Manage topic",
    }
    for src, want in cases.items():
        assert has_verb_stutter(src, _V) is True
        assert repair_stutter(src, _V) == want


def test_lc2_multiword_verb_protected() -> None:
    """ANTI-CASE: a real multiword verb (``Set up Slack``) is NOT a stutter —
    ``Set up`` is one verb, never split."""
    assert has_verb_stutter("Set up Slack integration", _V) is False
    assert has_verb_stutter("Sign in to Twenty", _V) is False


def test_lc2_applied_as_rename() -> None:
    """The stutter repair lands as a UF rename (not a fold)."""
    uf = _uf("UF-1", "Manage create topic", pf="topics", resource="topic")
    plan = plan_uf_synth([uf], [], [_pf("topics", "Topics")], _V)
    assert plan.rename["UF-1"] == "Manage topic"


# ── L-C3 same-noun-head family fold, GATED by span overlap ────────────────────


def test_lc3_family_folds_only_on_span_overlap() -> None:
    """cal routing-forms-style family: same noun-head UFs fold ONLY when their
    member spans overlap (coordinates decide, not the name)."""
    f_a = _flow("m-a", [("routing.ts", 10, 40)])
    f_b = _flow("m-b", [("routing.ts", 20, 30)])   # overlaps m-a
    f_c = _flow("m-c", [("other.ts", 100, 120)])   # disjoint
    a = _uf("UF-1", "Create routing form", pf="routing", resource="form", members=["m-a"])
    b = _uf("UF-2", "Edit routing form", pf="routing", resource="form", members=["m-b"])
    c = _uf("UF-3", "Delete routing form", pf="routing", resource="form", members=["m-c"])
    plan = plan_uf_synth([a, b, c], [f_a, f_b, f_c], [_pf("routing", "Routing Forms")], _V)
    # b overlaps a -> folds; c is disjoint -> survives
    assert plan.fold.get("UF-2") == "UF-1"
    assert "UF-3" not in plan.fold
    assert plan.reasons["UF-2"] == "lc3_family_overlap"


def test_spans_overlap_helper() -> None:
    assert spans_overlap(_flow("x", [("a.ts", 10, 40)]), _flow("y", [("a.ts", 30, 50)])) is True
    assert spans_overlap(_flow("x", [("a.ts", 10, 20)]), _flow("y", [("a.ts", 30, 50)])) is False
    assert spans_overlap(_flow("x", [("a.ts", 10, 40)]), _flow("y", [("b.ts", 10, 40)])) is False


# ── PACK-BORN defect regressions (wave + keyed, 2026-07-16) ───────────────────


def test_keyed_pair_coordinated_verb_list_never_mutilated() -> None:
    """KEYED PAIR unit (documenso UF-045): the rename path must never build a
    name that lost its middle verb. Both live phrase shapes stay VERBATIM:
    'Browse and filter Stripe events' (conjunction second — protected) and
    'Browse filter and Stripe events' (coordinated verb LIST — the third word
    is the conjunction; the old repair dropped 'filter' and left the dangling
    'and' -> 'Browse and Stripe events')."""
    for nm in ("Browse and filter Stripe events",
               "Browse filter and Stripe events"):
        assert has_verb_stutter(nm, _V) is False, nm
        uf = _uf("UF-1", nm, pf="api+", resource="webhook")
        ufs = [uf]
        apply_uf_synth_fold(ufs, [], [_pf("api+", "API")], _V)
        assert uf.name == nm, nm    # verbatim — no middle token lost


def test_malformed_phrase_patterns() -> None:
    """Sanitary mechanism (no brand dictionary): the observed defect shape and
    its structural family are flagged; every live legal coordinated name
    passes."""
    malformed = [
        "Browse and Stripe events",        # the keyed defect verbatim
        "Manage and GitHub metrics",       # verb + conj + Proper noun
        "Browse and and filter events",    # doubled conjunction
        "and manage documents",            # leading conjunction
        "Manage documents and",            # trailing conjunction
    ]
    for nm in malformed:
        assert is_malformed_phrase(nm, _V) is True, nm
    legal = [
        "Browse and filter Stripe events",             # verb-and-verb + brand
        "Browse and enforce server limits",            # lowercase non-vocab verb
        "Search and discover resources",
        "Create and edit templates",
        "Manage themes and locale",                    # noun-noun coordination
        "Browse and manage team documents and folders",
        "Manage organisation members and groups",
        "Request and complete password reset",         # non-vocab lead verb
    ]
    for nm in legal:
        assert is_malformed_phrase(nm, _V) is False, nm


def test_rename_guard_never_adopts_malformed_result() -> None:
    """Belt-and-braces: even when a stutter genuinely fires, a repair whose
    result is malformed is skipped (name kept verbatim)."""
    # 'Manage view and Stripe events': third word 'and' -> coordinated list ->
    # not a stutter at all; and the hypothetical repair result
    # 'Manage and Stripe events' is malformed -> either guard keeps the name.
    nm = "Manage view and Stripe events"
    uf = _uf("UF-1", nm, pf="x", resource="event")
    plan = plan_uf_synth([uf], [], [_pf("x", "X")], _V)
    assert plan.rename == {}
    assert is_malformed_phrase("Manage and Stripe events", _V) is True


def test_no_pack_output_name_is_malformed() -> None:
    """MANDATE sanitary assert: across a representative board (every exhibit
    class), NO name the pack emits matches a malformed pattern."""
    ufs = [
        _uf("UF-1", "Manage users", pf="users", resource="user"),
        _uf("UF-2", "User", pf="users", resource="user"),
        _uf("UF-3", "Browse up Slack notifications", pf="slack", resource="slack"),
        _uf("UF-4", "Manage create topic", pf="topics", resource="topic"),
        _uf("UF-5", "Browse & filter emails", pf="emails", resource="email"),
        _uf("UF-6", "Browse filter and Stripe events", pf="api+", resource="event"),
        _uf("UF-7", "Set up Slack integration", pf="slack", resource="slack"),
    ]
    pfs = [_pf("users", "Users"), _pf("slack", "Slack"), _pf("topics", "Topics"),
           _pf("emails", "Emails"), _pf("api+", "API")]
    apply_uf_synth_fold(ufs, [], pfs, _V)
    for u in ufs:
        assert not is_malformed_phrase(u.name, _V), u.name


def test_lc4_wave_twenty_shape_qualifies_never_folds() -> None:
    """WAVE shape (twenty): a rename lands on 'Manage profile' while an existing
    row (mc=9, another PF) already carries it. Final L-C4 pass QUALIFIES the
    non-canonical row with its PF display in parens — never a cross-PF fold."""
    existing = _uf("UF-1", "Manage profile", pf="members", resource="profile",
                   members=[f"m{i}" for i in range(9)])
    # stutter-shaped row whose repair collides: 'Manage edit profile' -> 'Manage profile'
    renamed = _uf("UF-2", "Manage edit profile", pf="settings", resource="profile",
                  members=["s1", "s2"])
    ufs = [existing, renamed]
    tele = apply_uf_synth_fold(
        ufs, [], [_pf("members", "Members"), _pf("settings", "Settings")], _V)
    assert len(ufs) == 2                                  # NO fold
    names = sorted(u.name for u in ufs)
    assert names == ["Manage profile", "Manage profile (Settings)"]
    # the canonical (more members) kept the bare name
    assert existing.name == "Manage profile"
    assert tele["lc4_qualified"] == 1


def test_lc4_wave_openstatus_shape_two_singletons() -> None:
    """WAVE shape (openstatus): 'locale tokens' x2 (mc=1,1) pre-existing on the
    board — one row is qualified to uniqueness; both survive."""
    a = _uf("UF-1", "locale tokens", pf="i18n", resource="locale", members=["a"])
    b = _uf("UF-2", "locale tokens", pf="status-page", resource="locale", members=["b"])
    ufs = [a, b]
    tele = apply_uf_synth_fold(
        ufs, [], [_pf("i18n", "I18n"), _pf("status-page", "Status Page")], _V)
    assert len(ufs) == 2
    assert len({u.name for u in ufs}) == 2                # unique board
    assert tele["lc4_qualified"] == 1
    assert any("(" in u.name for u in ufs)                # parenthetical qualifier


def test_lc4_untouchable_partner_keeps_bare_name() -> None:
    """A synthesized/marker collision partner is NEVER renamed — the real row
    takes the qualifier."""
    real = _uf("UF-1", "Documents", pf="docs", resource="document", members=["r"])
    synth = _uf("UF-2", "Documents", pf="team", resource="document", members=["s"])
    synth.synthesized = True
    ufs = [real, synth]
    apply_uf_synth_fold(ufs, [], [_pf("docs", "Docs"), _pf("team", "Team")], _V)
    assert synth.name == "Documents"                      # untouched
    assert real.name == "Documents (Docs)"                # qualified


# ── L-C4 board uniqueness ──────────────────────────────────────────────────────


def test_lc4_cal_org_duplicate_detected() -> None:
    """cal ``Org`` x2 (no board-uniqueness constraint) is surfaced as a
    collision."""
    a = _uf("UF-1", "Org", pf="orgs", resource="org", members=["ma"])
    b = _uf("UF-2", "Org", pf="teams", resource="org", members=["mb"])
    plan = plan_uf_synth([a, b], [], [_pf("orgs", "Orgs"), _pf("teams", "Teams")], _V)
    assert ("UF-1", "UF-2") in plan.collisions


# ── driver + conservation ──────────────────────────────────────────────────────


def test_apply_unions_members_and_drops_loser() -> None:
    """Fold unions member_flow_ids into the winner (conservation) and drops the
    loser row; the rich survivor keeps + gains the echo's members."""
    rich = _uf("UF-1", "Manage users", pf="users", resource="user", members=["m1"])
    bare = _uf("UF-2", "User", pf="users", resource="user", members=["m2"])
    ufs = [rich, bare]
    tele = apply_uf_synth_fold(ufs, [], [_pf("users", "Users")], _V)
    assert ufs == [rich]                       # loser dropped
    assert set(rich.member_flow_ids) == {"m1", "m2"}   # members unioned
    assert tele["lc1_echo_folded"] == 1
    assert tele["total_folded"] == 1


# ── S2 Seg A rider — lattice-partition fold exemption (wave-gauntlet C2) ──────
#
# Under FAULTLINE_UF_DET_AGGREGATION the lattice's action children ('Browse
# findings' / 'View findings' / 'Create findings') share a noun-head + files
# with their sibling domain cluster BY DESIGN — L-C1/L-C3 read that shape as
# a foldable dup family and silently UNDO the split (Soc0 wave forensics:
# UF-022 7 -> 47, giant-catchall 2 -> 5). ``fold_exempt_ids`` bars them as
# fold SOURCES; an empty set is byte-identical planning (the OFF worlds).


def _lattice_family():
    """A domain cluster + two lattice action children over the SAME file
    (overlapping spans — the exact wave shape)."""
    parent = _uf("UF-022", "Browse & filter API findings", pf="findings",
                 resource="finding", members=["p1", "p2"])
    kid_b = _uf("UF-L-aaa1", "Browse findings", pf="findings",
                resource="finding", members=["k1", "k2"])
    kid_v = _uf("UF-L-bbb2", "View findings", pf="findings",
                resource="finding", members=["k3", "k4"])
    flows = [
        _flow("p1", [("backend/routers/findings.py", 1, 40)]),
        _flow("p2", [("backend/routers/findings.py", 45, 80)]),
        _flow("k1", [("backend/routers/findings.py", 10, 30)]),
        _flow("k2", [("backend/routers/findings.py", 50, 70)]),
        _flow("k3", [("backend/routers/findings.py", 15, 25)]),
        _flow("k4", [("backend/routers/findings.py", 60, 75)]),
    ]
    return parent, kid_b, kid_v, flows


def test_lattice_children_exempt_are_never_fold_sources() -> None:
    """With the exemption set (the Seg A world), the action children survive
    and the parent keeps its own members — the lattice split is not undone."""
    parent, kid_b, kid_v, flows = _lattice_family()
    ufs = [parent, kid_b, kid_v]
    exempt = frozenset({"UF-L-aaa1", "UF-L-bbb2"})
    plan = plan_uf_synth(ufs, flows, [_pf("findings", "Findings")], _V,
                         fold_exempt_ids=exempt)
    assert not (set(plan.fold) & exempt)       # children never fold away
    tele = apply_uf_synth_fold(ufs, flows, [_pf("findings", "Findings")], _V,
                               fold_exempt_ids=exempt)
    ids = {u.id for u in ufs}
    assert {"UF-L-aaa1", "UF-L-bbb2"} <= ids   # split intact
    assert set(parent.member_flow_ids) == {"p1", "p2"}  # no giant re-mint


def test_without_exemption_the_fold_eats_the_children_regression_lock() -> None:
    """The empty-set call (every OFF world) reproduces the wave defect shape —
    locked so the exemption's purpose stays measurable."""
    parent, kid_b, kid_v, flows = _lattice_family()
    ufs = [parent, kid_b, kid_v]
    plan = plan_uf_synth(ufs, flows, [_pf("findings", "Findings")], _V)
    assert set(plan.fold) & {"UF-L-aaa1", "UF-L-bbb2"}  # children folded away
