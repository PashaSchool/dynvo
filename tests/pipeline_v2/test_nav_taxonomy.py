"""Tests for the in-repo nav taxonomy — extraction, matching, pinning.

Extraction (``product_strings.build_nav_taxonomy``): nested nav,
generic-label drop, dedupe, route-derived entries + the ≥2-page
structural guard, determinism, README exclusion unchanged.

Matching (``nav_taxonomy.match_features_to_taxonomy``): all three
channels (route / strings / tokens), channel priority, unmatched
fall-through.

Stage 6.5 rule: vendor label beats workspace + dep-anchor; telemetry +
``nav_taxonomy_map`` emitted.

Stage 8 pinning (``pin_nav_labels``): rename of a subset PF, creation
for unmapped matched devs, existing-slug pin, map consistency.

Rehydration: ``name_confidence`` survives a serialize → parse round
trip on product features.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from faultline.models.types import Feature
from faultline.pipeline_v2.nav_taxonomy import (
    match_features_to_taxonomy,
    pin_nav_labels,
)
from faultline.pipeline_v2.product_strings import (
    NavTaxonomyEntry,
    build_nav_taxonomy,
    collect_product_strings,
    normalize_href,
    route_path_for_file,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_6_5_product_clusterer import (
    run_product_clusterer,
)


def _write(repo: Path, rel: str, content: str) -> str:
    fp = repo / rel
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    return rel


def _feat(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=["alice"],
        total_commits=3,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=[],
        layer="developer",
    )


def _ctx(repo_path: Path) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack="next-app-router",
        monorepo=False,
        workspaces=None,
        tracked_files=[],
        commits=[],
    )


_NAV_TSX = """
export const nav = [
  { label: "Documents", href: "/documents" },
  { label: "Templates", href: "/templates" },
  { label: "Settings", href: "/settings" },
  { label: "Billing", href: "/settings/billing" },
  { label: "Webhooks", href: "/settings/webhooks" },
  { label: "Blog", href: "https://example.com/blog" },
];
"""


def _nav_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    paths = [
        _write(tmp_path, "src/components/sidebar.tsx", _NAV_TSX),
        _write(tmp_path, "app/(dash)/documents/page.tsx", "export default 1"),
        _write(tmp_path, "app/(dash)/documents/[id]/page.tsx", "export default 1"),
        _write(tmp_path, "app/(dash)/templates/page.tsx", "export default 1"),
        _write(tmp_path, "app/api/webhook/route.ts", "export const POST = 1"),
        _write(tmp_path, "app/api/teams/route.ts", "export const GET = 1"),
    ]
    return tmp_path, paths


# ── route helpers ───────────────────────────────────────────────────────


def test_route_path_for_file_app_router() -> None:
    assert (
        route_path_for_file("apps/web/src/app/(dash)/documents/[id]/page.tsx")
        == "/documents/[id]"
    )
    assert route_path_for_file("pages/settings/billing.tsx") == "/settings/billing"
    assert route_path_for_file("src/lib/utils.ts") is None
    assert route_path_for_file("app/@modal/photo/page.tsx") == "/photo"
    assert route_path_for_file("app/documents/page.test.tsx") is None


def test_route_path_for_file_remix_flat_routes() -> None:
    # remix-flat-routes: ``+`` folder suffix, ``$param`` dynamics,
    # dotted segments as separators, ``_layout`` organizational parts.
    assert (
        route_path_for_file("app/routes/_authenticated+/admin+/stats.tsx")
        == "/admin/stats"
    )
    assert (
        route_path_for_file("app/routes/sign.$token+/index.tsx") == "/sign"
    )


def test_trans_wrapped_nav_link_labels(tmp_path: Path) -> None:
    nav = (
        '<nav><Link href="/documents"><Trans>Documents</Trans></Link>'
        '<Link href="/templates"><Trans>Templates</Trans></Link></nav>'
    )
    rel = _write(tmp_path, "src/components/header.tsx", nav)
    idx = collect_product_strings(tmp_path, [rel])
    taxonomy = build_nav_taxonomy(idx, [rel])
    labels = {e.label for top in taxonomy for e in top.flatten()}
    assert labels == {"Documents", "Templates"}


def test_normalize_href() -> None:
    assert normalize_href("/Settings/Billing/?tab=1#x") == "/settings/billing"
    assert normalize_href("https://example.com/docs") is None
    # Dynamic template segments dropped (mirrors route_path_for_file's
    # ``$param`` handling); fully-dynamic hrefs carry no information.
    assert normalize_href("/teams/${id}") == "/teams"
    assert normalize_href("/t/${teamUrl}/documents") == "/t/documents"
    assert normalize_href("/${locale}") is None


def test_i18n_macro_labels_and_template_hrefs(tmp_path: Path) -> None:
    # lingui-style nav registry: msg`Label` macros + template hrefs.
    nav = """
    export const links = [
      { href: `/t/${teamUrl}/documents`, label: msg`Documents` },
      { href: `/t/${teamUrl}/templates`, label: msg`Templates` },
      { href: "/settings", label: msg`Settings` },
    ];
    """
    rel = _write(tmp_path, "src/components/app-nav-desktop.tsx", nav)
    idx = collect_product_strings(tmp_path, [rel])
    taxonomy = build_nav_taxonomy(idx, [rel])
    flat = {e.label: e for top in taxonomy for e in top.flatten()}
    assert "Documents" in flat and "Templates" in flat
    assert flat["Documents"].href == "/t/documents"
    assert "Settings" not in flat  # generic chrome still dropped


def test_react_router_to_links_with_nested_trans(tmp_path: Path) -> None:
    # react-router <Link to=…> with decorative wrappers between the
    # link and its <Trans> label (documenso settings-nav shape).
    nav = """
    <nav>
      <Link to="/settings/webhooks">
        <Button variant="ghost"><BellIcon className="h-4" />
          <Trans>Webhooks</Trans>
        </Button>
      </Link>
      <Link to="/settings/tokens">
        <Button variant="ghost"><KeyIcon className="h-4" />
          <Trans>API Tokens</Trans>
        </Button>
      </Link>
    </nav>
    """
    rel = _write(tmp_path, "src/components/settings-nav.tsx", nav)
    idx = collect_product_strings(tmp_path, [rel])
    taxonomy = build_nav_taxonomy(idx, [rel])
    flat = {e.label: e for top in taxonomy for e in top.flatten()}
    assert flat["Webhooks"].href == "/settings/webhooks"
    assert flat["API Tokens"].href == "/settings/tokens"


# ── extraction ──────────────────────────────────────────────────────────


def test_taxonomy_nested_nav_and_generic_drop(tmp_path: Path) -> None:
    repo, paths = _nav_repo(tmp_path)
    idx = collect_product_strings(repo, paths)
    taxonomy = build_nav_taxonomy(idx, paths)
    labels = {e.label for top in taxonomy for e in top.flatten()}

    # Generic chrome dropped; external href dropped.
    assert "Settings" not in labels
    assert "Blog" not in labels
    # Vendor surfaces kept.
    assert {"Documents", "Templates", "Billing", "Webhooks"} <= labels
    # "Billing" would nest under "Settings" but Settings is generic →
    # it stays top-level (no parent with /settings href survived).
    top_labels = {e.label for e in taxonomy}
    assert "Billing" in top_labels


def test_taxonomy_hierarchy_nests_under_parent_href(tmp_path: Path) -> None:
    nav = """
    export const items = [
      { label: "Workflows", href: "/workflows" },
      { label: "Run History", href: "/workflows/runs" },
    ];
    """
    rel = _write(tmp_path, "src/nav.ts", nav)
    idx = collect_product_strings(tmp_path, [rel])
    taxonomy = build_nav_taxonomy(idx, [rel])
    assert len(taxonomy) == 1
    assert taxonomy[0].label == "Workflows"
    assert [c.label for c in taxonomy[0].children] == ["Run History"]


def test_taxonomy_route_entries_require_two_pages(tmp_path: Path) -> None:
    repo, paths = _nav_repo(tmp_path)
    idx = collect_product_strings(repo, paths)
    taxonomy = build_nav_taxonomy(idx, paths)
    flat = [e for top in taxonomy for e in top.flatten()]
    by_label = {e.label: e for e in flat}
    # app/api has TWO route files → route-derived "Api" entry.
    assert "Api" in by_label
    assert by_label["Api"].source == "route"
    assert by_label["Api"].href == "/api"


def test_taxonomy_single_page_segment_not_emitted(tmp_path: Path) -> None:
    paths = [
        _write(tmp_path, "app/upload/page.tsx", "export default 1"),
        _write(tmp_path, "app/share/page.tsx", "export default 1"),
    ]
    idx = collect_product_strings(tmp_path, paths)
    taxonomy = build_nav_taxonomy(idx, paths)
    assert taxonomy == []  # leaf actions, not product surfaces


def test_taxonomy_dedupes_labels_and_is_deterministic(tmp_path: Path) -> None:
    repo, paths = _nav_repo(tmp_path)
    # Second nav file repeating a label must not duplicate the entry.
    paths.append(_write(repo, "src/components/topnav.tsx", _NAV_TSX))
    idx = collect_product_strings(repo, paths)
    t1 = build_nav_taxonomy(idx, paths)
    t2 = build_nav_taxonomy(idx, list(reversed(paths)))
    assert t1 == t2
    labels = [e.label for top in t1 for e in top.flatten()]
    assert len(labels) == len(set(labels))


def test_taxonomy_never_reads_readme(tmp_path: Path) -> None:
    readme = _write(tmp_path, "README.md", "# Features\n- Quantum Sync\n")
    nav = _write(tmp_path, "src/nav.tsx", _NAV_TSX)
    idx = collect_product_strings(tmp_path, [readme, nav])
    taxonomy = build_nav_taxonomy(idx, [readme, nav])
    labels = {e.label for top in taxonomy for e in top.flatten()}
    assert "Quantum Sync" not in labels


# ── matching channels ───────────────────────────────────────────────────


def _entries() -> list[NavTaxonomyEntry]:
    return [
        NavTaxonomyEntry("Documents", "/documents", "src/nav.tsx", "nav"),
        NavTaxonomyEntry("Billing", "/settings/billing", "src/nav.tsx", "nav"),
        NavTaxonomyEntry("Teams", "/teams", "src/nav.tsx", "nav"),
    ]


def test_match_via_route_anchor_serves_href() -> None:
    f = _feat("stripe", ["app/(dash)/settings/billing/page.tsx"])
    matches = match_features_to_taxonomy([f], _entries(), None)
    assert matches["stripe"].label == "Billing"
    assert matches["stripe"].via == "route"


def test_match_via_strings_page_title(tmp_path: Path) -> None:
    page = _write(
        tmp_path, "src/views/invoices.tsx",
        'export const metadata = { title: "Billing" }',
    )
    idx = collect_product_strings(tmp_path, [page])
    f = _feat("invoices", [page])
    matches = match_features_to_taxonomy([f], _entries(), idx)
    assert matches["invoices"].label == "Billing"
    assert matches["invoices"].via == "strings"


def test_match_via_token_containment() -> None:
    f = _feat("t-team-url", ["src/lib/team.ts"])
    matches = match_features_to_taxonomy([f], _entries(), None)
    assert matches["t-team-url"].label == "Teams"
    assert matches["t-team-url"].via == "tokens"


def test_match_priority_route_beats_tokens() -> None:
    # Feature name token-matches "Teams" but its anchor routes to
    # /documents — route channel wins.
    f = _feat("teams", ["app/documents/page.tsx"])
    matches = match_features_to_taxonomy([f], _entries(), None)
    assert matches["teams"].label == "Documents"
    assert matches["teams"].via == "route"


def test_coarse_route_prefix_demoted_below_tokens() -> None:
    # "Api" is a route-derived single-segment entry; the webhook
    # feature's anchors live under /api/** (prefix hit only) but its
    # name token-matches "Webhooks" — the specific content channel
    # must beat the coarse prefix.
    entries = [
        NavTaxonomyEntry("Api", "/api", "app/api/health/route.ts", "route"),
        NavTaxonomyEntry("Webhooks", "/settings/webhooks", "src/nav.tsx", "nav"),
    ]
    f = _feat("webhook", ["app/api/v1/webhook/route.ts"])
    matches = match_features_to_taxonomy([f], entries, None)
    assert matches["webhook"].label == "Webhooks"
    assert matches["webhook"].via == "tokens"
    # With no content match, the coarse prefix still catches it.
    g = _feat("health", ["app/api/health/route.ts"])
    matches = match_features_to_taxonomy([g], entries, None)
    # exact route == href? /api/health vs entry /api → prefix only;
    # no token/strings match for "health" → route-prefix fallback.
    assert matches["health"].label == "Api"
    assert matches["health"].via == "route-prefix"


def test_unmatched_feature_falls_through() -> None:
    f = _feat("background-jobs", ["src/jobs/worker.ts"])
    matches = match_features_to_taxonomy([f], _entries(), None)
    assert matches == {}


# ── Stage 6.5 integration ───────────────────────────────────────────────


def test_stage_6_5_nav_rule_beats_workspace_and_dep(tmp_path: Path) -> None:
    repo, _ = _nav_repo(tmp_path)
    feat_paths = [
        "app/(dash)/documents/page.tsx",
        "app/(dash)/documents/[id]/page.tsx",
        "src/components/sidebar.tsx",
    ]
    feat = _feat("docs-pages", feat_paths)
    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])

    assert mapping["docs-pages"] == ("Documents",)
    pf = next(p for p in products if p.name == "Documents")
    assert pf.name_confidence == "high"
    assert telemetry["nav_taxonomy_entries"] >= 4
    assert telemetry["nav_taxonomy_clusters_matched"] == 1
    assert telemetry["nav_taxonomy_matched_via"] == {"route": 1}
    assert telemetry["nav_taxonomy_map"] == {"docs-pages": "Documents"}
    assert telemetry["product_clusterer_votes_cast"]["nav-taxonomy"] == 1


def test_stage_6_5_unmatched_keeps_synthesis_path(tmp_path: Path) -> None:
    """A feature with no nav match still gets the workspace label."""
    repo = tmp_path
    for p in ("apps/worker/jobs.ts", "apps/worker/queue.ts"):
        _write(repo, p, "// x")
    feat = _feat("jobs", ["apps/worker/jobs.ts", "apps/worker/queue.ts"])
    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])
    assert telemetry["nav_taxonomy_clusters_matched"] == 0
    assert mapping["jobs"] == ("Worker",)


# ── Stage 8 pinning ─────────────────────────────────────────────────────


def _pf(name: str, display: str, paths: list[str]) -> Feature:
    f = _feat(name, paths)
    f.display_name = display
    f.layer = "product"
    return f


def test_pin_renames_subset_pf_to_vendor_label() -> None:
    devs = [_feat("doc-upload", ["a.ts"]), _feat("doc-sign", ["b.ts"])]
    pfs = [_pf("document-management", "Document Management", ["a.ts", "b.ts"])]
    dev_map: dict[str, tuple[str, ...]] = {
        "doc-upload": ("document-management",),
        "doc-sign": ("document-management",),
    }
    flows_map: dict[str, list[str]] = {"document-management": ["upload-flow"]}
    nav_map = {"doc-upload": "Documents", "doc-sign": "Documents"}

    pinned, tel = pin_nav_labels(pfs, dev_map, flows_map, nav_map, devs)

    assert tel["nav_pfs_renamed"] == 1
    assert pinned == {"documents"}
    assert pfs[0].name == "documents"
    assert pfs[0].display_name == "Documents"
    assert pfs[0].name_confidence == "high"
    assert dev_map["doc-upload"] == ("documents",)
    assert flows_map == {"documents": ["upload-flow"]}


def test_pin_creates_pf_for_unmapped_matched_devs() -> None:
    devs = [_feat("billing-api", ["pay.ts"])]
    pfs: list[Feature] = []
    dev_map: dict[str, tuple[str, ...]] = {}
    nav_map = {"billing-api": "Billing"}

    pinned, tel = pin_nav_labels(pfs, dev_map, {}, nav_map, devs)

    assert tel["nav_pfs_created"] == 1
    assert len(pfs) == 1 and pfs[0].name == "billing"
    assert pfs[0].paths == ["pay.ts"]
    assert dev_map["billing-api"] == ("billing",)
    assert pinned == {"billing"}


def test_pin_keeps_existing_vendor_named_pf_and_grows_membership() -> None:
    devs = [_feat("teams-core", ["t.ts"]), _feat("team-invites", ["i.ts"])]
    pfs = [_pf("teams", "Teams", ["t.ts"])]
    dev_map: dict[str, tuple[str, ...]] = {"teams-core": ("teams",)}
    nav_map = {"teams-core": "Teams", "team-invites": "Teams"}

    pinned, tel = pin_nav_labels(pfs, dev_map, {}, nav_map, devs)

    assert tel["nav_pfs_existing"] == 1
    assert pinned == {"teams"}
    assert dev_map["team-invites"] == ("teams",)
    assert set(pfs[0].paths) == {"t.ts", "i.ts"}


def test_pin_noop_without_nav_map() -> None:
    pfs = [_pf("x", "X", ["a.ts"])]
    pinned, tel = pin_nav_labels(pfs, {}, {}, {}, [])
    assert pinned == set()
    assert tel["nav_pfs_renamed"] == 0


# ── rehydration ─────────────────────────────────────────────────────────


def test_name_confidence_round_trips_serialization() -> None:
    pf = _pf("documents", "Documents", ["a.ts"])
    pf.name_confidence = "high"
    raw = json.loads(pf.model_dump_json())
    back = Feature.model_validate(raw)
    assert back.name_confidence == "high"
    assert back.display_name == "Documents"
    # Pre-nav scans (no field) rehydrate with the default.
    raw.pop("name_confidence")
    legacy = Feature.model_validate(raw)
    assert legacy.name_confidence == "high"
