"""Tests for Stage 8.9 — workspace-anchor sub-decomposition.

Synthetic, neutral fixture names only (rule-no-repo-specific-paths).
Verifies the domain detector, the grain/container floors, path
conservation, sub-feature provenance + non-re-entrancy, the dep-category
exclusion, zero-path protection, and the env toggle.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature
from faultline.pipeline_v2.stage_8_7_anchor_desink import _is_workspace_anchor
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    _domain_key,
    _plan_split,
    subdecompose_workspace_anchors,
)

_WS = "[package] workspace anchor {0!r} from monorepo package {0!r}"
_DEP = "[package] package anchor 'billing' from deps ['stripe']"


def _feat(name, paths, *, description=None, product_feature_id=None, uuid=""):
    return Feature(
        name=name,
        description=description,
        paths=list(paths),
        authors=[],
        total_commits=7,
        bug_fixes=1,
        bug_fix_ratio=0.1,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        layer="developer",
        product_feature_id=product_feature_id,
        uuid=uuid,
    )


def _ws(name, paths, **kw):
    return _feat(name, paths, description=_WS.format(name), **kw)


# ── _domain_key ──────────────────────────────────────────────────────────


def test_domain_key_layer_with_child_dir() -> None:
    assert _domain_key("web/src/modules/network/page.tsx") == "modules/network"
    assert _domain_key("api/services/billing/svc.py") == "services/billing"


def test_domain_key_file_in_layer_root_is_residual() -> None:
    # child is a FILE (no further segment) → not a domain
    assert _domain_key("api/services/util.py") is None
    assert _domain_key("api/models/user.py") is None


def test_domain_key_no_layer_is_residual() -> None:
    assert _domain_key("api/main.py") is None
    assert _domain_key("web/src/lib/helpers.ts") is None


# ── _plan_split (floors + conservation) ──────────────────────────────────


def test_plan_split_promotes_and_conserves() -> None:
    paths = [
        "services/a/x.py", "services/a/y.py",      # domain a (2)
        "services/b/x.py", "services/b/y.py",      # domain b (2)
        "models/user.py", "main.py",               # residual (2)
    ]
    domains, residual = _plan_split(paths, floor=2)
    assert set(domains) == {"services/a", "services/b"}
    assert len(domains["services/a"]) == 2
    # path conservation: nothing lost or duplicated
    flat = [p for fs in domains.values() for p in fs] + residual
    assert sorted(flat) == sorted(paths)


def test_plan_split_floor_folds_small_domains() -> None:
    paths = [
        "services/big/a.py", "services/big/b.py", "services/big/c.py",
        "services/big2/a.py", "services/big2/b.py", "services/big2/c.py",
        "services/tiny/a.py",  # 1 file < floor 3 → residual
    ]
    domains, residual = _plan_split(paths, floor=3)
    assert set(domains) == {"services/big", "services/big2"}
    assert "services/tiny/a.py" in residual


def test_plan_split_container_floor_needs_two_domains() -> None:
    # only ONE domain in the layer → not a container → all residual
    paths = ["services/solo/a.py", "services/solo/b.py", "services/solo/c.py"]
    domains, residual = _plan_split(paths, floor=2)
    assert domains == {}
    assert sorted(residual) == sorted(paths)


# ── subdecompose_workspace_anchors ───────────────────────────────────────


def _anchor_with_domains() -> Feature:
    paths = [
        "modules/network/a.tsx", "modules/network/b.tsx",
        "modules/threats/a.tsx", "modules/threats/b.tsx",
        "lib/util.ts", "index.ts",  # residual
    ]
    return _ws("frontend", paths, product_feature_id="pf-frontend", uuid="anchor-uuid")


def test_subdecompose_splits_anchor_into_domains() -> None:
    anchor = _anchor_with_domains()
    feats = [anchor]
    res = subdecompose_workspace_anchors(feats)
    assert res.anchors_split == 1
    assert res.subfeatures_created == 2
    subs = [f for f in feats if "workspace sub-domain" in (f.description or "").lower()]
    assert {f.name for f in subs} == {"network", "threats"}
    # anchor keeps only its residual
    assert sorted(anchor.paths) == ["index.ts", "lib/util.ts"]


def test_subfeatures_inherit_pf_and_provenance_and_are_not_anchors() -> None:
    anchor = _anchor_with_domains()
    feats = [anchor]
    subdecompose_workspace_anchors(feats)
    subs = [f for f in feats if f.name in ("network", "threats")]
    for s in subs:
        assert s.product_feature_id == "pf-frontend"   # product path union conserved
        assert s.split_from == "anchor-uuid"
        assert s.uuid and s.uuid != "anchor-uuid"
        assert not _is_workspace_anchor(s)              # not re-entrant
        assert s.total_commits == 0 and s.bug_fix_ratio == 0.0  # thin metrics


def test_path_conservation_across_split() -> None:
    anchor = _anchor_with_domains()
    before = set(anchor.paths)
    feats = [anchor]
    subdecompose_workspace_anchors(feats)
    after: set[str] = set()
    for f in feats:
        after |= set(f.paths)
    assert after == before  # redistribution only — nothing lost or gained


def test_dep_category_anchor_untouched() -> None:
    dep = _feat(
        "billing",
        ["services/a/x.py", "services/a/y.py", "services/b/x.py", "services/b/y.py"],
        description=_DEP,
    )
    feats = [dep]
    res = subdecompose_workspace_anchors(feats)
    assert res.anchors_split == 0
    assert len(feats) == 1  # no sub-features minted


def test_zero_path_protection_keeps_anchor_nonempty() -> None:
    # every path lands in a domain → anchor must keep the smallest as residual
    paths = [
        "modules/a/1.ts", "modules/a/2.ts", "modules/a/3.ts",
        "modules/b/1.ts", "modules/b/2.ts",
    ]
    anchor = _ws("frontend", paths, uuid="u")
    feats = [anchor]
    subdecompose_workspace_anchors(feats)
    assert anchor.paths  # never emptied


def test_kill_switch_disables(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_8_9_SUBDECOMPOSE", "0")
    anchor = _anchor_with_domains()
    feats = [anchor]
    res = subdecompose_workspace_anchors(feats)
    assert res.enabled is False
    assert len(feats) == 1
    assert len(anchor.paths) == 6  # untouched


def test_no_anchors_is_noop() -> None:
    feats = [_feat("a", ["x.py"]), _feat("b", ["y.py"])]
    res = subdecompose_workspace_anchors(feats)
    assert res.anchors_total == 0
    assert res.subfeatures_created == 0
