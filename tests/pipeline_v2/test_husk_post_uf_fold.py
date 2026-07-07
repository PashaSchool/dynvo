"""W4.2 Fix 3 — post-UF vendor-husk fold (midday ``Enable Banking`` I8).

Shapes distilled from the 2026-07-07 midday wave4-out artifact: a
providers-hub child with real LOC (above the D4 mint floor), zero flows
and zero journey citations folds under its enclosing minted capability
after the journey layer settles; the journey-cited sibling stays.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, MemberFile, UserFlow
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    fold_unreferenced_vendor_husks,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _feat(name: str, paths: list[str], *, layer: str = "developer",
          pfid: str | None = None, anchor: str | None = None,
          loc: int | None = None) -> Feature:
    f = Feature(
        name=name, paths=list(paths), authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=_NOW,
        health_score=100.0, layer=layer, product_feature_id=pfid, loc=loc,
        member_files=[MemberFile(path=p, role="anchor", primary=True,
                                 confidence=1.0) for p in paths],
    )
    f.anchor_id = anchor
    return f


def _uf(uid: str, name: str, pfid: str | None) -> UserFlow:
    return UserFlow(id=uid, name=name, resource="", intent="other",
                    product_feature_id=pfid, member_flow_ids=[])


def _family():
    hub = "pkg/finance/src/providers"
    banking_pf = _feat("banking", ["pkg/finance/src/index.ts"],
                       layer="product", anchor="ws:pkg/finance", loc=2800)
    husk_pf = _feat("alphabank", [f"{hub}/alphabank/api.ts"],
                    layer="product",
                    anchor=f"hub:{hub}/alphabank", loc=1100)
    cited_pf = _feat("betabank", [f"{hub}/betabank/api.ts"],
                     layer="product",
                     anchor=f"hub:{hub}/betabank", loc=1100)
    devs = [
        _feat("banking-core", ["pkg/finance/src/index.ts"], pfid="banking"),
        _feat("alphabank-api", [f"{hub}/alphabank/api.ts"],
              pfid="alphabank"),
        _feat("betabank-api", [f"{hub}/betabank/api.ts"], pfid="betabank"),
    ]
    ufs = [_uf("UF-001", "Connect a bank account", "betabank")]
    return devs, [banking_pf, husk_pf, cited_pf], ufs


def test_flowless_uncited_vendor_child_folds_into_enclosing() -> None:
    devs, pfs, ufs = _family()
    tele = fold_unreferenced_vendor_husks(devs, pfs, ufs)
    assert [x["pf"] for x in tele["folded"]] == ["alphabank"]
    assert tele["folded"][0]["into"] == "banking"
    assert {p.name for p in pfs} == {"banking", "betabank"}
    moved = next(d for d in devs if d.name == "alphabank-api")
    assert moved.product_feature_id == "banking"
    assert str(moved.anchor_id).startswith("fold:husk-post-uf->hub:")
    banking = next(p for p in pfs if p.name == "banking")
    assert "pkg/finance/src/providers/alphabank/api.ts" in banking.paths
    assert any(
        (m.path if hasattr(m, "path") else m.get("path"))
        == "pkg/finance/src/providers/alphabank/api.ts"
        for m in banking.member_files
    )


def test_journey_cited_child_survives() -> None:
    devs, pfs, ufs = _family()
    fold_unreferenced_vendor_husks(devs, pfs, ufs)
    assert any(p.name == "betabank" for p in pfs)


def test_flow_evidenced_child_survives() -> None:
    """D4's mint-time verdict stands: a child with flow evidence is a
    real integration PF even without journey citations."""
    from tests.pipeline_v2.test_stage_6_86_anchored_mint import flow

    devs, pfs, ufs = _family()
    husk_dev = next(d for d in devs if d.name == "alphabank-api")
    husk_dev.flows = [flow("sync-alphabank-flow",
                           "pkg/finance/src/providers/alphabank/api.ts")]
    tele = fold_unreferenced_vendor_husks(devs, pfs, ufs)
    assert tele["folded"] == []
    assert {p.name for p in pfs} == {"banking", "alphabank", "betabank"}


def test_no_enclosing_target_stays_minted() -> None:
    devs, pfs, ufs = _family()
    pfs = [p for p in pfs if p.name != "banking"]  # no enclosing capability
    tele = fold_unreferenced_vendor_husks(devs, pfs, ufs)
    assert tele["folded"] == [] and tele["no_target"] == 1
    assert any(p.name == "alphabank" for p in pfs)


def test_fix3_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_HUSK_POST_UF_FOLD", "0")
    devs, pfs, ufs = _family()
    tele = fold_unreferenced_vendor_husks(devs, pfs, ufs)
    assert tele["enabled"] is False
    assert {p.name for p in pfs} == {"banking", "alphabank", "betabank"}
