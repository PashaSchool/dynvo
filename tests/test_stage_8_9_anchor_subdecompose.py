"""Tests for Stage 8.9 — oversized-feature sub-decomposition.

Synthetic, neutral fixture names only (rule-no-repo-specific-paths).
Verifies the DEPTH-RECURSE layer-transparent domain detector, the
terminal-container guard (asset/build/tooling/test/version dirs never
mint a feature), the version-segment transparency, the scale-invariant
oversized gate + grain/container floors, path + member_file conservation,
member_file de-own (so cold_eval.owned_max actually moves), sub-feature
provenance + non-re-entrancy, the dep-category exclusion, zero-path
protection, and the env toggle.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.stage_8_7_anchor_desink import _is_workspace_anchor
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    _domain_key,
    _is_terminal,
    _is_transparent,
    _owned_paths,
    _plan_split,
    subdecompose_oversized_features,
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


# ── _domain_key (NEW depth-recurse contract) ─────────────────────────────
#
# The rewrite changed the contract: _domain_key now returns the FULL path
# PREFIX up to and including the first DOMAIN segment (transparently
# recursing THROUGH any number of layer/version segments), or None when the
# subtree is pure scaffold / asset / tooling / test. The leaf of that prefix
# is what _slug() turns into the feature name. (The old depth-1 version
# returned a layer-relative two-segment key like "modules/network".)


def test_domain_key_layer_with_child_dir() -> None:
    # recurses THROUGH the layer chain to the first domain dir, returning the
    # full prefix (leaf = the feature name). web/src/modules are all layers.
    assert _domain_key("web/src/modules/network/page.tsx") == "web/src/modules/network"
    assert _domain_key("api/services/billing/svc.py") == "api/services/billing"


def test_domain_key_file_in_layer_root_is_residual() -> None:
    # child is a FILE (no further segment) → no domain DIR → residual
    assert _domain_key("api/services/util.py") is None
    assert _domain_key("api/models/user.py") is None


def test_domain_key_no_layer_is_residual() -> None:
    # only layers / a loose top-level file → no domain segment → residual
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


# ── subdecompose_oversized_features ───────────────────────────────────────
#
# The new gate is RELATIVE to the repo's grain: a feature is oversized iff it
# owns > max(2 * median_owned_feature_size, ceil(0.15 * total_owned)). So the
# fixture must include SMALL grain-peer features to pull the median down,
# otherwise a lone big feature is its own median and never gates. ``_peers()``
# supplies that grain (each owns 2 files in its own dir → median = 2).


def _peers(n: int = 6) -> list[Feature]:
    """*n* small grain-peer developer features (2 owned files each) so the
    repo median owned-feature size is 2 — the realistic many-small-features
    shape that makes one fat feature genuinely *oversized*."""
    return [
        _feat(f"peer-{i}", [f"peerpkg{i}/x.ts", f"peerpkg{i}/y.ts"])
        for i in range(n)
    ]


def _anchor_with_domains() -> Feature:
    """An OVERSIZED anchor: two 3-file domains under a layer + loose residual.
    With ``_peers()`` (median 2) the cut is max(4, ceil(0.15*total)); this
    anchor owns 8 files → gates."""
    paths = [
        "modules/network/a.tsx", "modules/network/b.tsx", "modules/network/c.tsx",
        "modules/threats/a.tsx", "modules/threats/b.tsx", "modules/threats/c.tsx",
        "lib/util.ts", "index.ts",  # residual (non-domain → shared)
    ]
    return _ws("frontend", paths, product_feature_id="pf-frontend", uuid="anchor-uuid")


def test_subdecompose_splits_anchor_into_domains() -> None:
    anchor = _anchor_with_domains()
    feats = [*_peers(), anchor]
    res = subdecompose_oversized_features(feats)
    assert res.features_split == 1
    assert res.anchors_split == 1            # back-compat alias still works
    assert res.subfeatures_created == 2
    # NEW description marker is "sub-domain" (no longer "workspace sub-domain")
    subs = [f for f in feats if "sub-domain" in (f.description or "").lower()]
    assert {f.name for f in subs} == {"network", "threats"}
    # anchor keeps only its (de-owned) residual as exclusive paths
    assert sorted(anchor.paths) == ["index.ts", "lib/util.ts"]


def test_subfeatures_inherit_pf_and_provenance_and_are_not_anchors() -> None:
    anchor = _anchor_with_domains()
    feats = [*_peers(), anchor]
    subdecompose_oversized_features(feats)
    subs = [f for f in feats if f.name in ("network", "threats")]
    assert len(subs) == 2  # guard: the split actually happened (not vacuous)
    for s in subs:
        assert s.product_feature_id == "pf-frontend"   # product path union conserved
        assert s.split_from == "anchor-uuid"
        assert s.uuid and s.uuid != "anchor-uuid"
        assert not _is_workspace_anchor(s)              # not re-entrant
        assert s.total_commits == 0 and s.bug_fix_ratio == 0.0  # thin metrics


def test_path_conservation_across_split() -> None:
    anchor = _anchor_with_domains()
    feats = [*_peers(), anchor]
    before: set[str] = set()
    for f in feats:
        before |= set(f.paths)
    res = subdecompose_oversized_features(feats)
    assert res.features_split == 1  # guard: not vacuous
    after: set[str] = set()
    for f in feats:
        after |= set(f.paths)
    assert after == before  # redistribution only — nothing lost or gained


def test_dep_category_anchor_untouched() -> None:
    # A [package] DEP anchor (not a workspace anchor) is still decomposable
    # under the NEW oversized gate IF it is oversized — the gate is no longer
    # restricted to workspace anchors. Keep it SMALL so it does not gate, to
    # prove a small cohesive feature is left alone regardless of category.
    dep = _feat(
        "billing",
        ["services/a/x.py", "services/a/y.py"],
        description=_DEP,
    )
    feats = [*_peers(), dep]
    res = subdecompose_oversized_features(feats)
    assert res.features_split == 0
    assert all(f.name != "a" for f in feats)  # no sub-feature minted


def test_zero_path_protection_keeps_anchor_nonempty() -> None:
    # every owned file lands in a domain → anchor must keep the smallest
    # domain as its residual so it never becomes a ghost (empty) feature.
    paths = [
        "modules/a/1.ts", "modules/a/2.ts", "modules/a/3.ts",
        "modules/b/1.ts", "modules/b/2.ts", "modules/b/3.ts",
    ]
    anchor = _ws("frontend", paths, uuid="u")
    feats = [*_peers(), anchor]
    res = subdecompose_oversized_features(feats)
    assert res.features_split == 1  # guard: it did split
    assert anchor.paths  # never emptied


def test_kill_switch_disables(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_8_9_SUBDECOMPOSE", "0")
    anchor = _anchor_with_domains()
    feats = [*_peers(), anchor]
    res = subdecompose_oversized_features(feats)
    assert res.enabled is False
    assert len(feats) == len(_peers()) + 1  # nothing minted
    assert len(anchor.paths) == 8  # untouched


def test_no_anchors_is_noop() -> None:
    feats = [_feat("a", ["x.py"]), _feat("b", ["y.py"])]
    res = subdecompose_workspace_anchors(feats)
    assert res.anchors_total == 0
    assert res.subfeatures_created == 0


# ── NEW depth-recurse behaviour ───────────────────────────────────────────


def _owned_member_feat(name, paths, **kw) -> Feature:
    """A feature whose ownership is expressed via ``member_files``
    (role=anchor, primary=True) — the real engine shape that ``cold_eval``
    reads first. ``paths`` mirrors them."""
    f = _ws(name, paths, **kw)
    f.member_files = [
        MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
        for p in paths
    ]
    return f


def test_depth2_recursion_splits_modules_children() -> None:
    # modules/X/{a,b,c} → leaf domains a, b, c (depth-2 below the layer).
    # A loose residual file keeps zero-path protection from consuming a domain.
    anchor = _ws("frontend", [
        "modules/a/1.ts", "modules/a/2.ts",
        "modules/b/1.ts", "modules/b/2.ts",
        "modules/c/1.ts", "modules/c/2.ts",
        "index.ts",  # loose residual (non-domain)
    ], uuid="u")
    feats = [*_peers(), anchor]
    res = subdecompose_oversized_features(feats)
    assert res.features_split == 1
    assert {f.name for f in feats if f.split_from == "u"} == {"a", "b", "c"}


def test_depth3_recursion_descends_through_two_layers() -> None:
    # apps/web/<domain> — two transparent layers (apps, web) then the domain.
    anchor = _ws("web", [
        "apps/web/billing/a.ts", "apps/web/billing/b.ts",
        "apps/web/reporting/a.ts", "apps/web/reporting/b.ts",
        "apps/web/index.ts",  # loose residual under the layer chain
    ], uuid="u")
    feats = [*_peers(), anchor]
    res = subdecompose_oversized_features(feats)
    assert res.features_split == 1
    assert {f.name for f in feats if f.split_from == "u"} == {"billing", "reporting"}


def test_layer_transparent_names_domain_not_layer() -> None:
    # src/lib/ai → 'ai' (NOT 'lib'); src/server/payments → 'payments'.
    anchor = _ws("core", [
        "src/lib/ai/a.ts", "src/lib/ai/b.ts",
        "src/server/payments/a.ts", "src/server/payments/b.ts",
        "src/index.ts",  # loose residual
    ], uuid="u")
    feats = [*_peers(), anchor]
    subdecompose_oversized_features(feats)
    minted = {f.name for f in feats if f.split_from == "u"}
    assert minted == {"ai", "payments"}
    assert "lib" not in minted and "server" not in minted and "src" not in minted


def test_oversized_non_anchor_feature_qualifies() -> None:
    # A go-package / route group (NOT a workspace anchor) that is oversized
    # MUST still decompose — the gate is size-relative, not category-gated.
    pkg = _feat("caddyhttp", [
        "modules/caddyhttp/reverseproxy/a.go", "modules/caddyhttp/reverseproxy/b.go",
        "modules/caddyhttp/encode/a.go", "modules/caddyhttp/encode/b.go",
        "modules/caddyhttp/headers/a.go", "modules/caddyhttp/headers/b.go",
        "modules/caddyhttp/caddyhttp.go",  # loose residual in the package root
    ], description="[go-package] group 'caddyhttp'", uuid="u")
    assert not _is_workspace_anchor(pkg)  # precondition: not a workspace anchor
    feats = [*_peers(), pkg]
    res = subdecompose_oversized_features(feats)
    assert res.features_split == 1
    assert {f.name for f in feats if f.split_from == "u"} == {
        "reverseproxy", "encode", "headers",
    }


def test_member_files_pruned_so_owned_share_moves() -> None:
    # The blob metric reads member_files (primary/anchor) FIRST. After a split
    # the source must OWN only its residual; moved files become owned by the
    # sub-features. This is what makes cold_eval.owned_max actually move.
    anchor = _owned_member_feat("frontend", [
        "modules/network/a.ts", "modules/network/b.ts",
        "modules/threats/a.ts", "modules/threats/b.ts",
        "lib/util.ts", "index.ts",  # residual
    ], uuid="u")
    feats = [*_peers(), anchor]
    res = subdecompose_oversized_features(feats)
    assert res.members_deowned >= 1
    # source now OWNS (primary/anchor) only the residual files
    src_owned = set(_owned_paths(anchor))
    assert src_owned == {"lib/util.ts", "index.ts"}
    # the moved domain files are OWNED by the sub-features (member_files)
    subs = {f.name: f for f in feats if f.split_from == "u"}
    assert set(_owned_paths(subs["network"])) == {
        "modules/network/a.ts", "modules/network/b.ts",
    }
    # global owned-file count is unchanged (conservation), but the biggest
    # single OWNER shrank from 4 → 2 (the blob moved).
    biggest_before = 4
    biggest_after = max(len(_owned_paths(f)) for f in feats)
    assert biggest_after < biggest_before


def test_residual_is_kept_as_role_shared_not_dropped() -> None:
    # Residual (sub-floor / non-domain) member files stay on the source but
    # flip to role=shared / primary=False — de-owned, not lost.
    anchor = _owned_member_feat("frontend", [
        "modules/network/a.ts", "modules/network/b.ts",
        "modules/threats/a.ts", "modules/threats/b.ts",
        "lib/util.ts", "index.ts",
    ], uuid="u")
    feats = [*_peers(), anchor]
    subdecompose_oversized_features(feats)
    by_path = {m.path: m for m in anchor.member_files}
    # residual present but de-owned
    for p in ("lib/util.ts", "index.ts"):
        assert p in by_path
        assert by_path[p].role == "shared"
        assert by_path[p].primary is False
    # moved files removed from the source ledger entirely (owned elsewhere now)
    assert "modules/network/a.ts" not in by_path


def test_small_cohesive_feature_is_untouched() -> None:
    # A feature at the repo's own grain (not oversized) is never split, even
    # though it HAS internal domain dirs.
    small = _ws("widgets", [
        "modules/a/x.ts", "modules/a/y.ts", "modules/b/x.ts",
    ], uuid="u")
    feats = [*_peers(), small]
    res = subdecompose_oversized_features(feats)
    assert res.features_split == 0
    assert sorted(small.paths) == ["modules/a/x.ts", "modules/a/y.ts", "modules/b/x.ts"]


def test_naming_guard_pure_layer_infra_asset_dir_mints_no_feature() -> None:
    # An oversized feature whose files live ENTIRELY under layer / asset /
    # build / tooling / test / version dirs must mint ZERO sub-features — its
    # files all fall to the SHARED residual (no junk buckets).
    junk = _ws("frontend", [
        # assets (terminal: public/<x>)
        "public/favicon/a.ico", "public/favicon/b.ico",
        "public/images/a.png", "public/images/b.png",
        # tooling (terminal: scripts/<x>)
        "scripts/docker/a.sh", "scripts/docker/b.sh",
        "scripts/openapi/a.ts", "scripts/openapi/b.ts",
        # test tree (terminal)
        "playwright/api/a.spec.ts", "playwright/api/b.spec.ts",
        # generated (terminal)
        "generated/client/a.ts", "generated/client/b.ts",
        # pure layer files (no domain dir)
        "lib/x.ts", "utils/y.ts",
    ], uuid="u")
    feats = [*_peers(), junk]
    res = subdecompose_oversized_features(feats)
    # it IS oversized (gate fires) but yields NO promotable domain → no split
    assert res.oversized_total == 1
    assert res.features_split == 0
    assert res.subfeatures_created == 0
    # no feature named after any asset/tooling/test child was minted
    minted = {f.name for f in feats} - {p.name for p in _peers()} - {"frontend"}
    assert minted == set()


def test_version_dir_is_transparent_real_domain_below_surfaces() -> None:
    # api/v1/<domain> and api/v2/<domain> → the version is invisible; the
    # domain BELOW it names the feature (no 'v1'/'v2' junk buckets).
    anchor = _ws("api", [
        "app/api/v1/billing/a.ts", "app/api/v1/billing/b.ts",
        "app/api/v2/billing/a.ts", "app/api/v2/billing/b.ts",
        "app/api/v2/reporting/a.ts", "app/api/v2/reporting/b.ts",
    ], uuid="u")
    feats = [*_peers(), anchor]
    subdecompose_oversized_features(feats)
    minted = {f.name for f in feats if f.split_from == "u"}
    assert "v1" not in minted and "v2" not in minted
    assert minted == {"billing", "reporting"}


# ── terminal-container vs transparent-layer classifier ────────────────────


def test_terminal_segments_stop_recursion() -> None:
    # asset / build / tooling / test / generated dirs are TERMINAL
    for seg in ("public", "assets", "static", "scripts", "bin", "tools",
                "dist", "build", "coverage", "node_modules", "generated",
                "tests", "__tests__", "e2e", "playwright", "cypress",
                "fixtures", "docs", "migrations", "prisma"):
        assert _is_terminal(seg), seg
        # a file beneath a terminal container (reached via a transparent
        # layer ``src``) is ALWAYS residual — recursion stops at the terminal.
        assert _domain_key(f"src/{seg}/whatever/file.ts") is None, seg


def test_transparent_layers_are_not_terminal() -> None:
    for seg in ("src", "app", "modules", "lib", "services", "components",
                "api", "server", "client", "v1", "v2", "v10"):
        assert not _is_terminal(seg), seg
        assert _is_transparent(seg), seg


def test_version_segment_regex_is_universal() -> None:
    # version dirs are transparent; plain words (even other v-words that are
    # real domains) are NOT — only a leading ``v`` + digit matches.
    assert _is_transparent("v1") and _is_transparent("v2") and _is_transparent("v10")
    assert _is_transparent("v1beta") and _is_transparent("v2alpha")
    assert not _is_transparent("version")  # a word, not a version dir
    assert not _is_transparent("vault")    # real domain word starting with v
    assert not _is_transparent("voice")    # real domain word starting with v


# ── scale-invariance (rule-no-magic-tuning) ───────────────────────────────


def _scaled_anchor(domains: int, files_per_domain: int, uuid: str) -> Feature:
    paths = [
        f"modules/d{d}/f{i}.ts"
        for d in range(domains)
        for i in range(files_per_domain)
    ]
    paths.append("index.ts")  # loose residual so zero-path protection is a
    #                           no-op and ALL domains promote at every scale
    return _ws("frontend", paths, uuid=uuid)


def test_scale_invariance_tiny_medium_large() -> None:
    # The SAME relative rule must fire across 3 orders of magnitude with NO
    # magic constant. In each repo the anchor is ~10x the grain peers.
    # Tiny repo (peers own 2 each → median 2): a 12-file anchor gates.
    tiny = [*_peers(8), _scaled_anchor(domains=3, files_per_domain=4, uuid="t")]
    rt = subdecompose_oversized_features(tiny)
    assert rt.features_split == 1
    assert rt.subfeatures_created == 3

    # Medium repo: peers own 5 each (median 5), anchor owns 60 across 6 dirs.
    med_peers = [
        _feat(f"m{i}", [f"mp{i}/f{j}.ts" for j in range(5)]) for i in range(10)
    ]
    med = [*med_peers, _scaled_anchor(domains=6, files_per_domain=10, uuid="m")]
    rm = subdecompose_oversized_features(med)
    assert rm.features_split == 1
    assert rm.subfeatures_created == 6

    # Large repo: 200 peers own 3 each, anchor owns 600 across 20 dirs.
    big_peers = [
        _feat(f"b{i}", [f"bp{i}/f{j}.ts" for j in range(3)]) for i in range(200)
    ]
    big = [*big_peers, _scaled_anchor(domains=20, files_per_domain=30, uuid="b")]
    rb = subdecompose_oversized_features(big)
    assert rb.features_split == 1
    assert rb.subfeatures_created == 20


def test_scale_invariance_grain_floor_folds_below_median() -> None:
    # A domain SMALLER than the repo median folds to residual at every scale —
    # the floor is the median, not a constant. Peers own 5 each (median 5);
    # a 2-file domain on the anchor is below the floor → residual.
    peers = [_feat(f"p{i}", [f"pp{i}/a.ts", f"pp{i}/b.ts", f"pp{i}/c.ts",
                             f"pp{i}/d.ts", f"pp{i}/e.ts"]) for i in range(10)]
    anchor = _ws("frontend", [
        *[f"modules/big/f{i}.ts" for i in range(8)],     # 8 ≥ floor 5 → domain
        *[f"modules/big2/f{i}.ts" for i in range(8)],    # 8 ≥ floor 5 → domain
        "modules/small/a.ts", "modules/small/b.ts",       # 2 < floor 5 → residual
    ], uuid="u")
    feats = [*peers, anchor]
    subdecompose_oversized_features(feats)
    minted = {f.name for f in feats if f.split_from == "u"}
    assert minted == {"big", "big2"}
    assert "small" not in minted
    assert "modules/small/a.ts" in set(anchor.paths)  # folded to residual
