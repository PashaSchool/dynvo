"""W1.1 — finalize donor re-cover (validator I8, W1 §E residual).

The finalize conservation pass resettles journeys by span-LOC majority
and can empty a flowful PF (supabase ×4 / midday 'Support' on the
2026-07-06 validation wave). ``_recover_uncovered_donors`` re-runs the
6.7d backstop AFTER that pass over the stamped dev→PF state; the
follow-up conservation recheck must be a fixpoint (nothing moves).
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, Flow, FlowLineRange, UserFlow
from faultline.pipeline_v2.conservation import apply_uf_conservation
from faultline.pipeline_v2.phase_finalize import _recover_uncovered_donors

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _flow(uuid: str, span_path: str, loc: int = 30) -> Flow:
    return Flow(
        name=f"{uuid}-flow", uuid=uuid, paths=[span_path],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0,
        line_ranges=[FlowLineRange(path=span_path, start_line=1,
                                   end_line=loc)],
    )


def _dev(name: str, pfid: str | None, paths: list[str],
         flows: list[Flow] | None = None) -> Feature:
    return Feature(
        name=name, display_name=name, paths=paths, authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="developer", product_feature_id=pfid,
        flows=flows or [],
    )


def _pf(slug: str, display: str) -> Feature:
    return Feature(
        name=slug, display_name=display, paths=[], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_TS,
        health_score=90.0, layer="product",
    )


def _uf(uf_id: str, name: str, pfid: str | None,
        members: list[str]) -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, resource=name.lower(), intent="manage",
        product_feature_id=pfid, member_flow_ids=members,
        member_count=len(members),
    )


def _supabase_world() -> tuple[list[Feature], list[Feature], list[UserFlow]]:
    """The supabase shape AFTER finalize conservation: the graphql PF's
    only journey was resettled to the docs PF (its spans live in docs
    files) — the donor ships uncovered unless re-covered."""
    graphql_dev = _dev(
        "graphql", "auto-generated-graphql-api",
        ["apps/studio/graphql/api.ts"],
        flows=[
            _flow("g1", "apps/docs/content/graphql/quickstart.mdx"),
            _flow("g2", "apps/docs/content/graphql/api.mdx"),
        ],
    )
    docs_dev = _dev(
        "docs", "documentation-site",
        ["apps/docs/content/graphql/quickstart.mdx",
         "apps/docs/content/graphql/api.mdx"],
        flows=[_flow("d1", "apps/docs/content/index.mdx")],
    )
    devs = [graphql_dev, docs_dev]
    pfs = [_pf("auto-generated-graphql-api", "Auto-Generated GraphQL API"),
           _pf("documentation-site", "Documentation Site")]
    ufs = [
        # conservation already resettled this one onto the docs PF
        _uf("UF-001", "Browse and run GraphQL queries",
            "documentation-site", ["g1", "g2"]),
        _uf("UF-002", "Read documentation", "documentation-site", ["d1"]),
    ]
    return devs, pfs, ufs


def test_donor_recovered_by_synthesis_and_recheck_is_fixpoint() -> None:
    devs, pfs, ufs = _supabase_world()
    tele = _recover_uncovered_donors(ufs, devs, pfs)
    assert tele is not None
    assert tele["uncovered"] == 1
    # Reassign is refused (conservation would undo it) → synthesize.
    assert tele["reassigned_ufs"] == 0
    assert tele["synthesized"] == 1
    synth = [u for u in ufs if u.synthesized]
    assert len(synth) == 1
    assert synth[0].product_feature_id == "auto-generated-graphql-api"
    # Continues the stable id numbering after the existing block.
    assert synth[0].id == "UF-003"
    # Termination proof at the unit level: the conservation recheck the
    # caller runs after this is a strict no-op.
    recheck = apply_uf_conservation(
        ufs, devs, pfs, null_shared_without_signal=True,
    )
    assert recheck["resettled"] == 0
    assert recheck["nulled_shared"] == 0
    assert recheck["donors_left_uncovered"] == 0
    # Every flowful PF is covered.
    covered = {u.product_feature_id for u in ufs}
    assert {"auto-generated-graphql-api", "documentation-site"} <= covered


def test_no_donors_is_a_no_op() -> None:
    devs, pfs, ufs = _supabase_world()
    ufs.append(_uf("UF-003", "Query GraphQL",
                   "auto-generated-graphql-api", ["g1"]))
    before = [(u.id, u.product_feature_id) for u in ufs]
    tele = _recover_uncovered_donors(ufs, devs, pfs)
    assert tele is not None
    assert tele["uncovered"] == 0
    assert tele["synthesized"] == 0
    assert [(u.id, u.product_feature_id) for u in ufs] == before


def test_backstop_kill_switch_returns_none(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_PF_UF_BACKSTOP", "0")
    devs, pfs, ufs = _supabase_world()
    assert _recover_uncovered_donors(ufs, devs, pfs) is None
    assert not any(u.synthesized for u in ufs)


def test_deterministic_ids_across_runs() -> None:
    def run() -> list[tuple[str, str, bool]]:
        devs, pfs, ufs = _supabase_world()
        _recover_uncovered_donors(ufs, devs, pfs)
        return [(u.id, u.name, bool(u.synthesized)) for u in ufs]

    assert run() == run()
