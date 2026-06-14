"""Tests for Stage 8.7 — workspace-anchor de-sink.

Synthetic, neutral fixture names only (per memory/rule-no-repo-specific-
paths). Verifies the de-sink predicate, zero-path protection, the
dep-category exclusion, path-keyed surface pruning vs N:M-overlay
preservation, product-feature resync, and the env toggle.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import (
    Feature,
    FlowSymbolAttribution,
    MemberFile,
    SymbolAttribution,
)
from faultline.pipeline_v2.stage_8_7_anchor_desink import (
    _is_workspace_anchor,
    desink_workspace_anchors,
)

_WS = "[package] workspace anchor {0!r} from monorepo package {0!r}"
_DEP = "[package] package anchor 'billing' from deps ['stripe']"
_ROUTE = "[route] route convention slug {0!r} derived from 1 routing file(s)"


def _feat(
    name: str,
    paths: list[str],
    *,
    description: str | None = None,
    layer: str = "developer",
    product_feature_id: str | None = None,
    symbol_attributions: list[FlowSymbolAttribution] | None = None,
    shared_attributions: list[SymbolAttribution] | None = None,
    member_files: list[MemberFile] | None = None,
) -> Feature:
    return Feature(
        name=name,
        description=description,
        paths=list(paths),
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=100.0,
        layer=layer,
        product_feature_id=product_feature_id,
        symbol_attributions=symbol_attributions or [],
        shared_attributions=shared_attributions or [],
        member_files=member_files or [],
    )


def _ws_anchor(name: str, paths: list[str], **kw) -> Feature:
    return _feat(name, paths, description=_WS.format(name), **kw)


# ── detection ────────────────────────────────────────────────────────────────


def test_detection_only_workspace_anchor() -> None:
    ws = _ws_anchor("backend", ["backend/a.py"])
    dep = _feat("billing", ["x/pay.ts"], description=_DEP)
    route = _feat("orders", ["x/orders.ts"], description=_ROUTE.format("orders"))
    plain = _feat("misc", ["x/misc.ts"], description=None)
    assert _is_workspace_anchor(ws) is True
    # The dep-category "package anchor" marker is NOT a workspace anchor.
    assert _is_workspace_anchor(dep) is False
    assert _is_workspace_anchor(route) is False
    assert _is_workspace_anchor(plain) is False


# ── core de-sink ─────────────────────────────────────────────────────────────


def test_releases_paths_claimed_by_specific_feature() -> None:
    blob = _ws_anchor(
        "backend",
        ["backend/a.py", "backend/b.py", "backend/c.py", "backend/di.py"],
    )
    specific = _feat("auth", ["backend/a.py", "backend/b.py"],
                     description=_ROUTE.format("auth"))
    res = desink_workspace_anchors([blob, specific], [])
    # blob keeps only the residual (paths no specific feature claims).
    assert set(blob.paths) == {"backend/c.py", "backend/di.py"}
    # specific feature is untouched.
    assert set(specific.paths) == {"backend/a.py", "backend/b.py"}
    assert res.anchors_desunk == 1
    assert res.paths_removed == 2
    assert res.anchors_protected == 0


def test_zero_path_protection_keeps_fully_claimed_anchor_whole() -> None:
    blob = _ws_anchor("backend", ["backend/a.py", "backend/b.py"])
    s1 = _feat("auth", ["backend/a.py"], description=_ROUTE.format("auth"))
    s2 = _feat("orders", ["backend/b.py"], description=_ROUTE.format("orders"))
    res = desink_workspace_anchors([blob, s1, s2], [])
    # Every path is claimed elsewhere → keep the anchor whole, do not empty.
    assert set(blob.paths) == {"backend/a.py", "backend/b.py"}
    assert res.anchors_desunk == 0
    assert res.anchors_protected == 1


def test_dep_category_anchor_is_never_desunk() -> None:
    # A dep-category "package anchor" legitimately owns its import-reachable
    # consumers even when a route feature shares one of its files.
    dep = _feat("billing", ["pay/stripe.ts", "pay/checkout.ts"], description=_DEP)
    route = _feat("checkout", ["pay/checkout.ts"], description=_ROUTE.format("checkout"))
    res = desink_workspace_anchors([dep, route], [])
    assert set(dep.paths) == {"pay/stripe.ts", "pay/checkout.ts"}
    assert res.anchors_total == 0
    assert res.anchors_desunk == 0


def test_noop_when_no_workspace_anchor() -> None:
    a = _feat("auth", ["x/a.ts"], description=_ROUTE.format("auth"))
    b = _feat("orders", ["x/a.ts", "x/b.ts"], description=_ROUTE.format("orders"))
    res = desink_workspace_anchors([a, b], [])
    assert res.anchors_total == 0
    assert res.anchors_desunk == 0
    assert b.paths == ["x/a.ts", "x/b.ts"]


def test_noop_when_anchor_paths_are_exclusive() -> None:
    blob = _ws_anchor("backend", ["backend/svc.py", "backend/di.py"])
    other = _feat("frontend-page", ["frontend/page.tsx"],
                  description=_ROUTE.format("frontend-page"))
    res = desink_workspace_anchors([blob, other], [])
    # No path on the anchor is claimed elsewhere → nothing released.
    assert set(blob.paths) == {"backend/svc.py", "backend/di.py"}
    assert res.anchors_desunk == 0


def test_two_anchors_do_not_desink_each_other() -> None:
    # Overlap between two workspace anchors is NOT a specific claim — only
    # a non-anchor claimer triggers release.
    a = _ws_anchor("backend", ["shared/x.py", "backend/y.py"])
    b = _ws_anchor("frontend", ["shared/x.py", "frontend/z.tsx"])
    res = desink_workspace_anchors([a, b], [])
    assert set(a.paths) == {"shared/x.py", "backend/y.py"}
    assert set(b.paths) == {"shared/x.py", "frontend/z.tsx"}
    assert res.anchors_desunk == 0


# ── surface pruning vs N:M overlay preservation ─────────────────────────────


def test_prunes_path_keyed_surfaces_but_keeps_nm_overlays() -> None:
    blob = _ws_anchor(
        "backend",
        ["backend/a.py", "backend/c.py"],
        symbol_attributions=[
            FlowSymbolAttribution(file="backend/a.py", symbol="fa",
                                  line_start=1, line_end=9, role="structural"),
            FlowSymbolAttribution(file="backend/c.py", symbol="fc",
                                  line_start=1, line_end=9, role="structural"),
        ],
        shared_attributions=[
            SymbolAttribution(file_path="backend/a.py", symbols=["fa"],
                              line_ranges=[(1, 9)], attributed_lines=9,
                              total_file_lines=9),
        ],
        # N:M overlay — a member-file legitimately stays even after the
        # primary path is released elsewhere.
        member_files=[MemberFile(path="backend/a.py", role="closure",
                                 confidence=0.5)],
    )
    specific = _feat("auth", ["backend/a.py"], description=_ROUTE.format("auth"))
    desink_workspace_anchors([blob, specific], [])
    assert set(blob.paths) == {"backend/c.py"}
    # path-keyed surfaces pruned to match `paths`
    assert {s.file for s in blob.symbol_attributions} == {"backend/c.py"}
    assert {s.file_path for s in blob.shared_attributions} == set()
    # N:M overlay preserved
    assert {m.path for m in blob.member_files} == {"backend/a.py"}


# ── product-feature resync ───────────────────────────────────────────────────


def test_resyncs_only_affected_product_feature_paths() -> None:
    blob = _ws_anchor("backend", ["backend/a.py", "backend/c.py"],
                      product_feature_id="API")
    specific = _feat("auth", ["backend/a.py"],
                     description=_ROUTE.format("auth"), product_feature_id="Auth")
    pf_api = _feat("API", ["backend/a.py", "backend/c.py"], layer="product")
    pf_auth = _feat("Auth", ["backend/a.py"], layer="product")
    pf_other = _feat("Other", ["unrelated/x.ts"], layer="product")
    res = desink_workspace_anchors(
        [blob, specific], [pf_api, pf_auth, pf_other],
    )
    # API owns the de-sunk anchor → its paths shrink to the union of members.
    assert set(pf_api.paths) == {"backend/c.py"}
    # Auth + Other are not affected → byte-stable.
    assert pf_auth.paths == ["backend/a.py"]
    assert pf_other.paths == ["unrelated/x.ts"]
    assert res.product_features_resynced == 1


# ── env toggle + telemetry ───────────────────────────────────────────────────


def test_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_8_7_DESINK", "0")
    blob = _ws_anchor("backend", ["backend/a.py", "backend/c.py"])
    specific = _feat("auth", ["backend/a.py"], description=_ROUTE.format("auth"))
    res = desink_workspace_anchors([blob, specific], [])
    assert res.enabled is False
    assert blob.paths == ["backend/a.py", "backend/c.py"]  # untouched
    assert res.anchors_desunk == 0


def test_telemetry_shape() -> None:
    blob = _ws_anchor("backend", ["backend/a.py", "backend/c.py"])
    specific = _feat("auth", ["backend/a.py"], description=_ROUTE.format("auth"))
    tele = desink_workspace_anchors([blob, specific], []).as_telemetry()
    assert set(tele) == {
        "enabled", "anchors_total", "anchors_desunk", "anchors_protected",
        "paths_removed", "product_features_resynced", "desunk_sample",
    }
    assert tele["anchors_desunk"] == 1
    assert tele["desunk_sample"][0]["feature"] == "backend"
