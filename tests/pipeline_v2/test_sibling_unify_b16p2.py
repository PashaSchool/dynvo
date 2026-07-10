"""B16 Part 1b (UF dev-grain name law) + Part 2 (sibling-anchor unification).

Part 1b: "View detections page" -> "View detections" when the home PF anchor is
a route:*-page leak; anti-cases ("Publish page", 2-word names, non-route homes)
survive.

Part 2: co-identity sibling route PFs (investigation / investigations-page /
investigation-flow) collapse to ONE; the over-unification guard keeps a bare
singular/plural pair (user/users) separate; devs + UFs re-point; loser PFs drop.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, MemberFile, UserFlow
from faultline.pipeline_v2.naming_contract import (
    UF_DEVGRAIN_NAME_ENV,
    _strip_uf_devgrain_suffix,
    load_naming_vocab,
    uf_devgrain_name_enabled,
)
from faultline.pipeline_v2.stage_6_88_sibling_unify import (
    SIBLING_UNIFY_ENV,
    _capability_identity,
    _has_devsuffix,
    sibling_unify_enabled,
    unify_sibling_anchors,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_VOCAB = load_naming_vocab()


# ── Part 1b — UF dev-grain strip ────────────────────────────────────────

def test_uf_strip_page_leak():
    assert _strip_uf_devgrain_suffix(
        "View detections page", "route:detections-page", _VOCAB
    ) == "View detections"
    assert _strip_uf_devgrain_suffix(
        "Manage prompts page", "route:prompts-page", _VOCAB
    ) == "Manage prompts"


def test_uf_strip_anticase_publish_page():
    # 2-word "Publish page" -> would strip to a bare verb -> arity guard blocks;
    # and its home (page-builder) is not route:*-page either.
    assert _strip_uf_devgrain_suffix(
        "Publish page", "route:page-builder", _VOCAB) is None
    assert _strip_uf_devgrain_suffix(
        "Publish page", "route:publish-page", _VOCAB) is None   # arity guard


def test_uf_strip_requires_page_home_anchor():
    # home PF is a plain resource route (not *-page) -> "flow"/"view" in the
    # name is a capability noun, not a leak.
    assert _strip_uf_devgrain_suffix(
        "Build bot flow", "route:editor", _VOCAB) is None
    assert _strip_uf_devgrain_suffix(
        "View anomalies", "fdir:frontend/src/features/anomalies", _VOCAB) is None


def test_uf_flag_toggle(monkeypatch):
    monkeypatch.setenv(UF_DEVGRAIN_NAME_ENV, "0")
    assert uf_devgrain_name_enabled() is False
    monkeypatch.setenv(UF_DEVGRAIN_NAME_ENV, "1")
    assert uf_devgrain_name_enabled() is True


# ── Part 2 — sibling unification ────────────────────────────────────────

def _pf(slug, anchor, nfiles):
    mfs = [MemberFile(path=f"{slug}/f{i}", role="anchor", confidence=1.0,
                      primary=False, loc=10) for i in range(nfiles)]
    return Feature(
        name=slug, display_name=slug, layer="product",
        paths=[f"{slug}/f{i}" for i in range(nfiles)], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_NOW,
        health_score=100.0, anchor_id=anchor, member_files=mfs)


def _dev(pfid):
    return Feature(
        name=f"dev-{pfid}", layer="developer", product_feature_id=pfid,
        paths=[], authors=["a"], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_NOW, health_score=100.0)


def _uf(pfid):
    return UserFlow(id=f"u-{pfid}", name="x", resource=pfid, domain=None,
                    product_feature_id=pfid, intent="manage",
                    member_flow_ids=[], member_count=0)


def test_capability_identity():
    assert _capability_identity("investigations-page") == "investigation"
    assert _capability_identity("investigation-flow") == "investigation"
    assert _capability_identity("investigation") == "investigation"
    assert _capability_identity("detections-page") == "detection"
    assert not _has_devsuffix("investigation")
    assert _has_devsuffix("investigations-page")


def test_investigations_triple_collapses():
    pfs = [_pf("investigation", "route:investigation", 30),
           _pf("investigations-page", "route:investigations-page", 5),
           _pf("investigation-flow", "route:investigation-flow", 3)]
    devs = [_dev("investigations-page"), _dev("investigation-flow")]
    ufs = [_uf("investigations-page"), _uf("investigation-flow")]
    tele = unify_sibling_anchors(ufs, devs, pfs)
    assert tele["merged_away"] == 2 and tele["clusters"] == 1
    assert [p.name for p in pfs] == ["investigation"]        # winner (largest, non-suffix)
    assert all(d.product_feature_id == "investigation" for d in devs)
    assert all(u.product_feature_id == "investigation" for u in ufs)
    assert len(pfs[0].member_files) == 38                    # 30 + 5 + 3 absorbed


def test_over_unification_guard_keeps_singular_plural():
    # user + users: same capability identity BUT neither carries a dev-suffix
    # -> NOT merged (rail 4).
    pfs = [_pf("user", "route:user", 10), _pf("users", "route:users", 8)]
    tele = unify_sibling_anchors([], [], pfs)
    assert tele["merged_away"] == 0
    assert {p.name for p in pfs} == {"user", "users"}


def test_different_parent_namespace_not_merged():
    # same terminal identity but different parent route -> NOT siblings.
    pfs = [_pf("a-page", "route:admin/settings-page", 5),
           _pf("setting", "route:user/setting", 5)]
    tele = unify_sibling_anchors([], [], pfs)
    assert tele["merged_away"] == 0


def test_solo_pf_untouched():
    pfs = [_pf("settings-page", "route:settings-page", 6)]
    assert unify_sibling_anchors([], [], pfs)["merged_away"] == 0


def test_flag_off_noop(monkeypatch):
    monkeypatch.setenv(SIBLING_UNIFY_ENV, "0")
    assert sibling_unify_enabled() is False
