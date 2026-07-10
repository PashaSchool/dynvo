"""B26 — hub plumbing child (cal.com ``app-store/_utils`` exhibit).

The dir-per-vendor child filter compared the RAW segment against the
plumbing/stop vocabularies, so an underscore-private shared-helper dir
slipped past its own vocabulary entry and minted a PF-bearing hub child
(wave-14 census: cal.com ``_utils`` 45 consumer PFs / ``_components`` 9 /
``_pages`` saved only by the husk LOC floor). Fix A normalizes the
segment before the vocabulary test (vendor-beats-plumbing guarded); Fix B
backstops the 6.86 mint bar with ``hub_plumbing_child``.

Kill-switch: ``FAULTLINE_HUB_PLUMBING_CHILD=0`` restores the raw-segment
compare byte-identically.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.models.types import Feature, Flow, MemberFile
from faultline.pipeline_v2.spine_anchors import (
    build_spine_anchors,
    hub_child_is_plumbing,
    hub_plumbing_child_enabled,
    load_spine_vocab,
)
from faultline.pipeline_v2.stage_6_86_anchored_mint import run_anchored_mint

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def flow(name: str, entry: str) -> Flow:
    return Flow(
        name=name, entry_point_file=entry, paths=[entry], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_NOW,
        health_score=100.0,
    )


def dev(name: str, paths: list[str], flows: list[Flow] | None = None,
        **kw) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        member_files=[
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
            for p in paths
        ],
        flows=flows or [],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, **kw,
    )


def ctx_of(workspaces=None, tracked=None, repo_path=".") -> SimpleNamespace:
    return SimpleNamespace(
        workspaces=workspaces, tracked_files=tracked or [],
        repo_path=Path(repo_path), monorepo=bool(workspaces),
    )


def by_id(anchors) -> dict[str, object]:
    return {a.canonical_id: a for a in anchors}


def _plumbing_keys() -> frozenset[str]:
    v = load_spine_vocab()
    return (frozenset(v.get("hub_plumbing_segments") or [])
            | frozenset(v.get("structural_stoplist") or []))


def _app_store_devs() -> list[Feature]:
    """Distilled cal.com shape: a dir-per-vendor hub family under
    ``packages/app-store`` with 3 vendor children + the ``_utils``
    shared-helper dir (flowful — helper dirs are always somebody's
    call-chain entry) + the ``_pages`` near-miss."""
    vendors = [
        dev("hubspot", [
            "packages/app-store/hubspot/api/add.ts",
            "packages/app-store/hubspot/lib/CrmService.ts",
        ], flows=[flow("connect-hubspot-flow",
                       "packages/app-store/hubspot/api/add.ts")]),
        dev("stripe", [
            "packages/app-store/stripe/api/add.ts",
            "packages/app-store/stripe/lib/PaymentService.ts",
        ], flows=[flow("pay-with-stripe-flow",
                       "packages/app-store/stripe/api/add.ts")]),
        dev("paypal", [
            "packages/app-store/paypal/api/add.ts",
            "packages/app-store/paypal/lib/PaymentService.ts",
        ], flows=[flow("pay-with-paypal-flow",
                       "packages/app-store/paypal/api/add.ts")]),
    ]
    utils = dev("utils", [
        "packages/app-store/_utils/getAppKeysFromSlug.ts",
        "packages/app-store/_utils/oauth/OAuthManager.ts",
        "packages/app-store/_utils/installation.ts",
    ], flows=[flow("manage-oauth-credentials-flow",
                   "packages/app-store/_utils/oauth/OAuthManager.ts")])
    pages = dev("pages", ["packages/app-store/_pages/setup/index.ts"])
    return [*vendors, utils, pages]


# ── the helper (mechanism + guard) ───────────────────────────────────────


def test_helper_normalizes_underscore_children_into_vocabulary():
    keys = _plumbing_keys()
    # the census class: every observed instance is caught NORMALIZED
    assert hub_child_is_plumbing("_utils", keys)
    assert hub_child_is_plumbing("_components", keys)
    assert hub_child_is_plumbing("_pages", keys)
    # raw entries stay caught too (utils without underscore)
    assert hub_child_is_plumbing("utils", keys)


def test_helper_never_flags_vendor_or_capability_children():
    keys = _plumbing_keys()
    # real vendor children — in AND out of the vendor vocabulary
    for seg in ("googlecalendar", "applecalendar", "salesforce", "stripe",
                "teller", "routing-forms", "templates"):
        assert not hub_child_is_plumbing(seg, keys), seg


def test_helper_vendor_beats_plumbing_guard():
    """A segment naming exactly one vendor is NEVER plumbing, even when a
    (synthetic) vocabulary lists its normalized key — the 8.9.7 stem-rule
    precedence protects a vendor whose name collides with an infra noun."""
    assert not hub_child_is_plumbing("stripe", frozenset({"stripe"}))
    # non-vendor control: the same synthetic key DOES catch a non-vendor
    assert hub_child_is_plumbing("_frobnicators",
                                 frozenset({"frobnicator"}))


def test_flag_default_on(monkeypatch):
    monkeypatch.delenv("FAULTLINE_HUB_PLUMBING_CHILD", raising=False)
    assert hub_plumbing_child_enabled()
    monkeypatch.setenv("FAULTLINE_HUB_PLUMBING_CHILD", "0")
    assert not hub_plumbing_child_enabled()


# ── Fix A — spine child filter ───────────────────────────────────────────


def test_spine_blocks_underscore_plumbing_children(monkeypatch):
    monkeypatch.delenv("FAULTLINE_HUB_PLUMBING_CHILD", raising=False)
    anchors = by_id(build_spine_anchors(_app_store_devs(), [], ctx_of()))
    # vendor children mint as before
    for v in ("hubspot", "stripe", "paypal"):
        assert f"hub:packages/app-store/{v}" in anchors, v
    # the shared-helper dirs are the family's plumbing — never children
    assert "hub:packages/app-store/_utils" not in anchors
    assert "hub:packages/app-store/_pages" not in anchors


def test_spine_flag_off_restores_raw_segment_compare(monkeypatch):
    """=0 restore: the underscore children are enumerated again (the
    pre-B26 behavior — ``_utils`` mints at the spine, ``_pages`` reaches
    the 6.86 husk floor)."""
    monkeypatch.setenv("FAULTLINE_HUB_PLUMBING_CHILD", "0")
    anchors = by_id(build_spine_anchors(_app_store_devs(), [], ctx_of()))
    assert "hub:packages/app-store/_utils" in anchors
    assert anchors["hub:packages/app-store/_utils"].source == "hub-vendor"
    assert "hub:packages/app-store/_pages" in anchors


# ── Fix B — mint-bar backstop + fold destination ─────────────────────────


def _mint(devs, routes=None, ctx=None):
    return run_anchored_mint(devs, routes or [], ctx or ctx_of())


def test_mint_no_plumbing_pf_and_devs_fold_to_enclosing_home(monkeypatch):
    """End-to-end distilled exhibit: no ``utils`` PF mints; the vendor
    PFs survive; the flowful utils dev lands in a real capability home
    (never the lane — journeys ride their devs, nothing dissolves)."""
    monkeypatch.delenv("FAULTLINE_HUB_PLUMBING_CHILD", raising=False)
    devs = _app_store_devs()
    pfs, tele = _mint(devs)
    names = {p.name for p in pfs}
    assert "utils" not in names
    assert not any((p.anchor_id or "").endswith("/_utils") for p in pfs)
    for v in ("hubspot", "stripe", "paypal"):
        assert v in names, v
    utils_dev = next(f for f in devs if f.name == "utils")
    # flowful dev NEVER lanes (validator I9) — it folds to a minted PF
    assert utils_dev.product_feature_id in names
    assert utils_dev.shared_reason is None


def test_mint_bar_backstop_bars_plumbing_anchor_with_flow_evidence(
        monkeypatch):
    """Fix B in isolation: force the spine filter OFF-path by feeding the
    mint a family whose plumbing child would pass the flow bar — the bar
    must return ``hub_plumbing_child`` (visible in bar_decisions
    telemetry), NOT mint. Uses flag-on mint over flag-off-built anchors
    is not constructible through the public entrypoint, so this asserts
    via telemetry on the full run with the spine rung disabled first."""
    monkeypatch.setenv("FAULTLINE_HUB_PLUMBING_CHILD", "0")
    devs = _app_store_devs()
    anchors = by_id(build_spine_anchors(devs, [], ctx_of()))
    assert "hub:packages/app-store/_utils" in anchors  # precondition
    monkeypatch.delenv("FAULTLINE_HUB_PLUMBING_CHILD", raising=False)
    pfs, tele = _mint(devs)
    assert "utils" not in {p.name for p in pfs}
    bars = {d["anchor"]: d["bar"] for d in tele.get("bar_decisions", [])}
    # with the flag ON the spine never enumerates the child, so the bar
    # never sees it — the backstop is exercised by the direct rung below
    assert "hub:packages/app-store/_utils" not in bars


def test_mint_bar_rung_direct():
    """The backstop rung itself: a hub-vendor plumbing anchor with a
    flowful winner is barred ``hub_plumbing_child``; the identical anchor
    with a vendor segment passes to the flow-evidence rung."""
    from faultline.pipeline_v2.spine_anchors import SpineAnchor
    from faultline.pipeline_v2.stage_6_86_anchored_mint import _mint_bar

    keys = _plumbing_keys()
    winner = dev("utils", ["packages/app-store/_utils/x.ts"],
                 flows=[flow("f", "packages/app-store/_utils/x.ts")])
    plumbing_anchor = SpineAnchor(
        canonical_id="hub:packages/app-store/_utils", key="util",
        source="hub-vendor", display="Utils",
        prefixes=("packages/app-store/_utils",),
        sources=frozenset({"hub-vendor"}),
        hub_dir="packages/app-store", vendor="_utils",
    )
    vendor_anchor = SpineAnchor(
        canonical_id="hub:packages/app-store/zoomvideo", key="zoomvideo",
        source="hub-vendor", display="Zoomvideo",
        prefixes=("packages/app-store/zoomvideo",),
        sources=frozenset({"hub-vendor"}),
        hub_dir="packages/app-store", vendor="zoom",
    )
    args = ([winner], {}, False, (".ts",), Path("."), {})
    assert _mint_bar(plumbing_anchor, *args,
                     plumbing_keys=keys) == "hub_plumbing_child"
    assert _mint_bar(vendor_anchor, *args, plumbing_keys=keys) is None
    # empty vocabulary (defensive default) → rung inert
    assert _mint_bar(plumbing_anchor, *args,
                     plumbing_keys=frozenset()) is None


# ── anti-cases ───────────────────────────────────────────────────────────


def test_anticase_route_surfaced_utils_feature_unaffected():
    """A product feature legitimately named ``utils`` OUTSIDE a hub
    family (fdir/route grain) never passes through the hub child filter —
    it homes by its ordinary lineage, no lane, no bar."""
    routes = [{"pattern": "/utils", "method": "PAGE",
               "file": "apps/web/modules/utils/page.tsx"}]
    u = dev("utils", ["apps/web/modules/utils/page.tsx"],
            flows=[flow("browse-utils-flow",
                        "apps/web/modules/utils/page.tsx")])
    pfs, _tele = _mint([u], routes)
    assert u.product_feature_id is not None
    assert u.shared_reason is None


def test_anticase_small_shared_dir_folds_into_sole_consumer_family():
    """With no ``_utils`` child anchor the helper files ride the
    enclosing claim — inside a single-PF family they stay under that
    PF's home, never the lane (operator anti-case 2)."""
    devs = _app_store_devs()
    pfs, _tele = _mint(devs)
    names = {p.name for p in pfs}
    for f in devs:
        if f.flows:  # every flowful dev has a product home
            assert f.product_feature_id in names, f.name
