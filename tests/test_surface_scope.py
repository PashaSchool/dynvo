"""Unit tests for the deterministic surface-scope classifier (Lane C).

NOTE (2026-07-05): the classifier is committed UNWIRED — the MISSION-92R
pre-registered validation gates (G1 +8pp / G3 <=3% TRUE-misclassification)
failed on the recorded 24-draw claim table, so Stage 6.85 emission was NOT
added to the pipeline (no output-schema change, snapshot-inert). These tests
pin the classifier semantics for a potential future re-open.
"""
from __future__ import annotations

from faultline.pipeline_v2.surface_scope import (
    SURFACE_SCOPES,
    SurfaceScopeClassifier,
    load_patterns,
    tag_user_flows,
)

PATTERNS = {
    "route_groups": {"marketing": ["marketing", "landing"], "docs": ["docs"]},
    "url_segments": {
        "marketing": ["blog", "pricing", "legal"],
        "docs": ["docs", "guides"],
        "dev_tooling": ["mcp"],
    },
    "workspace_dirs": {"marketing": ["www"], "docs": ["docs"], "dev_tooling": ["cli"]},
}


def clf() -> SurfaceScopeClassifier:
    return SurfaceScopeClassifier(PATTERNS)


# ── lexicon determinism + path classification ─────────────────────


def test_route_group_detection():
    assert clf().classify_path("apps/web/app/(marketing)/pricing/page.tsx") == "marketing"
    assert clf().classify_path("apps/web/src/app/(landing)/blog/page.tsx") == "marketing"
    assert clf().classify_path("apps/web/app/(docs)/quickstart/page.tsx") == "docs"


def test_url_segments_only_after_routing_root():
    # 'blog' as a URL segment under app/ → marketing …
    assert clf().classify_path("app/blog/[slug]/page.tsx") == "marketing"
    # … but 'blog' in an arbitrary source dir is NOT a signal (a blog PRODUCT
    # must never be tagged marketing from its model/feature dirs).
    assert clf().classify_path("src/features/blog/model.ts") is None
    assert clf().classify_path("lib/legal/validators.ts") is None


def test_filename_stem_counts_but_neutral_stems_do_not():
    assert clf().classify_path("src/pages/pricing.tsx") == "marketing"
    assert clf().classify_path("src/pages/index.tsx") is None


def test_workspace_dirs_first_two_segments():
    assert clf().classify_path("apps/docs/src/components/Nav.tsx") == "docs"
    assert clf().classify_path("packages/cli/src/index.ts") == "dev_tooling"
    # container match is positional — a nested 'docs' dir alone is no signal
    assert clf().classify_path("src/docs/helpers.ts") is None


def test_precedence_dev_tooling_over_docs_over_marketing():
    # one path carrying several signals resolves by fixed precedence
    assert clf().classify_path("apps/docs/app/blog/page.tsx") == "docs"
    assert clf().classify_path("packages/cli/app/docs/page.tsx") == "dev_tooling"


def test_route_pattern_classification_skips_dynamic_segments():
    assert clf().classify_route("/blog/[slug]") == "marketing"
    assert clf().classify_route("/docs/:section") == "docs"
    assert clf().classify_route("/workspaces/[id]/settings") is None


def test_determinism_repeated_calls():
    c = clf()
    p = "apps/web/app/(marketing)/blog/page.tsx"
    assert len({c.classify_path(p) for _ in range(50)}) == 1


# ── UF-level aggregation ──────────────────────────────────────────


def test_any_product_vote_blocks_non_product():
    c = clf()
    votes = ["marketing", "product", "marketing"]
    assert c.classify_user_flow(votes) == "product"


def test_unanimous_non_product_majority_wins():
    c = clf()
    assert c.classify_user_flow(["marketing", "marketing", "docs"]) == "marketing"


def test_conservative_default_no_signal_is_product():
    c = clf()
    assert c.classify_user_flow([]) == "product"
    assert c.classify_user_flow([None, None]) == "product"


def test_stage_6_8b_category_passthrough():
    # system comes ONLY from the existing 6.8b aggregate verdict
    c = clf()
    assert c.classify_user_flow(["marketing"], uf_category="system") == "system"
    assert c.classify_user_flow([], uf_category="interactive") == "product"


def test_unmatched_uf_route_is_a_product_vote():
    c = clf()
    assert c.classify_user_flow(["marketing"], uf_routes=["/dashboard"]) == "product"
    assert c.classify_user_flow(["marketing"], uf_routes=["/blog"]) == "marketing"


def test_member_vote_route_entry_without_signal_votes_product():
    c = clf()
    assert c.member_vote("app/dashboard/page.tsx", entry_is_route=True) == "product"
    # non-route entry with no signal abstains
    assert c.member_vote("lib/utils.ts", entry_is_route=False) is None


# ── tag_user_flows wiring helper (dict + object inputs, no crash) ─


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_tag_user_flows_dicts_and_objects():
    flows = [
        {"uuid": "f1", "name": "blog flow",
         "entry_point_file": "app/(marketing)/blog/page.tsx", "paths": []},
        _Obj(uuid="f2", name="dash flow",
             entry_point_file="app/dashboard/page.tsx", paths=[]),
    ]
    ufs = [
        {"id": "UF-1", "member_flow_ids": ["f1"], "routes": [], "category": "interactive"},
        _Obj(id="UF-2", member_flow_ids=["f2"], routes=[], category="interactive"),
    ]
    counts = tag_user_flows(
        ufs, flows,
        routes_index=[{"file": "app/dashboard/page.tsx"}],
        patterns=PATTERNS,
    )
    assert ufs[0]["surface_scope"] == "marketing"
    assert ufs[1].surface_scope == "product"
    assert counts == {"marketing": 1, "product": 1}


def test_tag_user_flows_empty_and_missing_members_no_crash():
    assert tag_user_flows([], [], patterns=PATTERNS) == {}
    ufs = [{"id": "UF-1", "member_flow_ids": ["missing"], "routes": []}]
    counts = tag_user_flows(ufs, [], patterns=PATTERNS)
    assert ufs[0]["surface_scope"] == "product"  # conservative
    assert counts == {"product": 1}


def test_empty_patterns_no_op_everything_product():
    c = SurfaceScopeClassifier({})
    assert c.classify_path("app/(marketing)/blog/page.tsx") is None
    assert c.classify_user_flow(["product"]) == "product"


def test_pattern_file_matches_eval_authoring_copy():
    """Drift guard — same contract as test_pipeline_v2_data_hermetic."""
    from pathlib import Path

    from faultline.pipeline_v2.data import load_data_text

    repo_root = Path(__file__).resolve().parents[1]
    authoring = (repo_root / "eval" / "surface-scope-patterns.yaml").read_text(
        encoding="utf-8")
    load_data_text.cache_clear()
    packaged = load_data_text("surface-scope-patterns.yaml")
    assert packaged == authoring, (
        "DRIFT: faultline/pipeline_v2/data/surface-scope-patterns.yaml differs "
        "from eval/surface-scope-patterns.yaml. Re-sync the in-package copy."
    )


def test_runtime_pattern_file_loads_and_scopes_are_frozen():
    cfg = load_patterns()
    assert cfg, "runtime surface-scope-patterns.yaml must be packaged"
    assert SURFACE_SCOPES == ("product", "system", "dev_tooling", "docs", "marketing")
    for block in ("route_groups", "url_segments", "workspace_dirs"):
        for scope in cfg.get(block, {}):
            assert scope in SURFACE_SCOPES
