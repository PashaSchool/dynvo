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
