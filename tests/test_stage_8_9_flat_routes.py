"""Tests for Stage 8.9 — Remix flat-routes domain-keying.

Some file-system routers (Remix / React-Router "flat-routes", the
``remix-flat-routes`` package, the ``app/routes.ts`` flat config) encode the
WHOLE route hierarchy in a single DOT-SEPARATED NAME inside ONE ``routes/``
directory, instead of in nested sub-directories:

    app/routes/_app.orgs.$organizationSlug.projects.$projectParam.alerts.new.ts
    app/routes/account.tokens/route.tsx          (dot-name as a DIRECTORY)
    app/routes/@.runs.$runParam.ts

The directory-tree decomposer (:func:`_domain_key`) used to resolve every such
file to the SHARED residual (the route hierarchy is in the dot-NAME, not in
folders), so a Remix workspace anchor stayed a blob (trigger.dev's ``webapp``
owned 39% of the repo this way). These tests cover the flat-route parser that
keys each file by its VIRTUAL route domain parsed from the dot-name.

Universal-safety is the crux: the flat-route branch must fire ONLY on genuine
flat-route names (dot-separated route segments under a ``routes`` dir) and NEVER
on ordinary dotted files (``Button.test.tsx`` / ``foo.server.ts`` / ``index.css``)
or on subdirectory routers (Next.js App Router), which must stay byte-identical.

Synthetic, neutral fixture names only (rule-no-repo-specific-paths). The single
"trigger.dev-shaped" fixture below is a SYNTHETIC reconstruction of the flat-route
SHAPE (no real paths copied), per the rule.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    _domain_key,
    _flat_route_domain,
    _is_clean_domain_part,
    _is_flat_route_name,
    _owned_paths,
    subdecompose_oversized_features,
)

# Production reality: a workspace anchor's owned files share the package prefix
# (``apps/webapp``), which ``_common_segments`` strips → the decomposer calls
# ``_domain_key`` with start≈2. The flat-route fast-path is start-INDEPENDENT, so
# these unit tests pin behaviour at a representative start AND at start=0 to prove
# robustness to the outlier-shrunk-prefix case (the real trigger.dev bug).
_WS = "[package] workspace anchor {0!r} from monorepo package {0!r}"


def _feat(name, paths, *, description=None, uuid=""):
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
        uuid=uuid,
    )


def _ws(name, paths, **kw):
    return _feat(name, paths, description=_WS.format(name), **kw)


def _peers(n: int = 6) -> list[Feature]:
    """Small grain-peer features (2 owned files each) so the repo median owned
    size is 2 and one fat anchor genuinely gates as oversized."""
    return [
        _feat(f"peer-{i}", [f"peerpkg{i}/x.ts", f"peerpkg{i}/y.ts"])
        for i in range(n)
    ]


def _owned(name, paths, **kw) -> Feature:
    f = _ws(name, paths, **kw)
    f.member_files = [
        MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
        for p in paths
    ]
    return f


# ── _is_clean_domain_part ────────────────────────────────────────────────────


def test_clean_domain_part_accepts_literal_words() -> None:
    for p in ("orgs", "runs", "account", "alerts", "secret-sync", "api", "v2"):
        assert _is_clean_domain_part(p), p


def test_clean_domain_part_rejects_markers_and_brackets() -> None:
    # layout / param / index / escape markers and bracket forms are not domains
    for p in ("_app", "_index", "_layout", "$slug", "$organizationSlug",
              "@", "index", "[id]", "[.]", "sitemap[", "]xml", "[[...all]]"):
        assert not _is_clean_domain_part(p), p


# ── _flat_route_domain ───────────────────────────────────────────────────────


def test_flat_route_domain_first_literal_segment() -> None:
    # the DOMAIN is the first segment that is none of layout/param/index/escape
    assert _flat_route_domain("_app.orgs.$organizationSlug.projects") == "orgs"
    assert _flat_route_domain("@.runs.$runParam") == "runs"
    assert _flat_route_domain("account.tokens") == "account"
    assert _flat_route_domain("resources.account.mfa.setup") == "resources"
    assert _flat_route_domain("_app.@.orgs.$organizationSlug.$") == "orgs"
    assert _flat_route_domain("_app.api.runs") == "api"  # 'api' is a real URL seg


def test_flat_route_domain_layout_or_index_only_is_none() -> None:
    # a name that is ALL layout/index/param/escape markers has no domain
    assert _flat_route_domain("_app") is None
    assert _flat_route_domain("@") is None
    assert _flat_route_domain("_app._index") is None
    assert _flat_route_domain("@.$param") is None
    # escaped-literal-dot single route file (SvelteKit/SolidStart ``[.]``)
    assert _flat_route_domain("sitemap[.]xml") is None


# ── _is_flat_route_name (the universal-safety gate) ──────────────────────────


def test_is_flat_route_name_requires_two_route_segments() -> None:
    assert _is_flat_route_name("account.tokens")
    assert _is_flat_route_name("_app.orgs")
    assert _is_flat_route_name("@.runs")
    assert _is_flat_route_name("account._index")
    # a single segment (no dots after ext-strip) is NOT a flat route
    assert not _is_flat_route_name("healthcheck")
    assert not _is_flat_route_name("login")


def test_is_flat_route_name_rejects_colocated_non_route_suffix() -> None:
    # dotted FILES whose dots are a TYPE/ROLE suffix, not URL segments
    for name in ("Button.test", "Modal.spec", "foo.server", "bar.client",
                 "index.css", "styles.scss", "schema.d", "data.json",
                 "config.yaml", "story.stories", "readme.md"):
        assert not _is_flat_route_name(name), name


# ── _domain_key end-to-end: flat-route FILES (basename under routes/) ────────


def test_domain_key_flat_route_file_keys_by_virtual_domain() -> None:
    # The loop bound ``i < len(segs)-1`` never inspects a file basename; the
    # flat-route fast-path does. Each flat-route FILE keys by its route domain.
    for start in (0, 2):
        assert _domain_key(
            "apps/web/app/routes/_app.orgs.$org.projects.$p.alerts.new.ts", start,
        ) == "apps/web/app/routes/orgs"
        assert _domain_key(
            "apps/web/app/routes/@.runs.$runParam.ts", start,
        ) == "apps/web/app/routes/runs"
        assert _domain_key(
            "apps/web/app/routes/account.tokens.ts", start,
        ) == "apps/web/app/routes/account"


def test_domain_key_flat_route_directory_keys_by_virtual_domain() -> None:
    # A dot-name DIRECTORY (route + colocated files inside) keys the SAME way.
    for start in (0, 2):
        assert _domain_key(
            "apps/web/app/routes/account.tokens/route.tsx", start,
        ) == "apps/web/app/routes/account"
        assert _domain_key(
            "apps/web/app/routes/_app.orgs.$o.projects.$p.sessions.$s/loader.ts", start,
        ) == "apps/web/app/routes/orgs"


def test_domain_key_layout_and_index_shells_are_residual() -> None:
    # layout / index-only route shells carry no domain → shared residual (None)
    for start in (0, 2):
        assert _domain_key("apps/web/app/routes/_app.tsx", start) is None
        assert _domain_key("apps/web/app/routes/@.ts", start) is None
        assert _domain_key("apps/web/app/routes/_app._index.ts", start) is None


# ── UNIVERSAL SAFETY: must NOT misfire on non-route dotted files ─────────────


def test_domain_key_colocated_non_route_file_in_routes_dir_is_residual() -> None:
    # A test/server/style file colocated under ``routes/`` is NOT a flat route;
    # it resolves like any ordinary file with no domain dir → residual.
    for start in (0, 2):
        assert _domain_key("apps/web/app/routes/Button.test.tsx", start) is None
        assert _domain_key("apps/web/app/routes/loader.server.ts", start) is None
        assert _domain_key("apps/web/app/routes/styles.css", start) is None
        assert _domain_key("apps/web/app/routes/types.d.ts", start) is None


def test_domain_key_single_segment_file_in_routes_dir_is_residual() -> None:
    # a single-word route file (no hierarchy) is not decomposable → residual
    for start in (0, 2):
        assert _domain_key("apps/web/app/routes/healthcheck.ts", start) is None
        assert _domain_key("apps/web/app/routes/login.ts", start) is None


def test_domain_key_escaped_dot_route_does_not_mint_junk() -> None:
    # ``sitemap[.]xml`` is the single route ``/sitemap.xml`` (escaped literal
    # dot), NOT a ``sitemap`` → ``xml`` hierarchy. Must NOT mint ``sitemap[``.
    assert _domain_key("apps/web/src/routes/sitemap[.]xml.ts", 0) is None


def test_domain_key_dotted_file_outside_routes_dir_unchanged() -> None:
    # the flat-route branch is gated on a ``routes``/``route`` parent dir; a
    # dotted file elsewhere keeps ordinary behaviour (file → no domain → None).
    assert _domain_key("apps/web/app/components/Button.test.tsx", 0) is None
    assert _domain_key("apps/web/app/services/billing.server.ts", 0) is None


# ── Next.js subdirectory router: BYTE-IDENTICAL (no flat-route misfire) ──────


def test_domain_key_next_app_router_subdir_unchanged() -> None:
    # Next App Router route dirs have NO dots → the flat-route branch never
    # fires; the ordinary directory descent resolves them exactly as before.
    assert _domain_key("apps/web/app/(dashboard)/billing/page.tsx", 0) == (
        "apps/web/app/(dashboard)/billing"
    )
    # first DOMAIN below the layer chain is ``webhooks`` (byte-identical to the
    # pre-fix behaviour — the flat-route branch never fires on a non-dotted dir).
    assert _domain_key("apps/web/app/api/webhooks/stripe/route.ts", 0) == (
        "apps/web/app/api/webhooks"
    )
    # a subdir literally named ``routes`` but with NON-dotted children is a
    # normal directory router — its child is an ordinary domain dir.
    assert _domain_key("src/routes/billing/index.tsx", 0) == "src/routes/billing"


# ── End-to-end: a trigger.dev-SHAPED flat-routes workspace decomposes ────────


def _flat_routes_anchor(uuid: str = "u") -> Feature:
    """SYNTHETIC reconstruction of the Remix flat-routes SHAPE (no real paths):
    one workspace package, ALL route files in a single ``app/routes/`` dir, the
    hierarchy encoded in dot-names across several domains, PLUS a few colocated
    non-route files (components/server/test) that must fall to the residual.

    Crucially includes a SINGLE owned file OUTSIDE the package prefix (the real
    trigger.dev bug: one stray import-closure file collapsed ``_common_segments``
    to 0, making the workspace dir a spurious single domain). The start-
    independent flat-route scan must still decompose correctly.
    """
    routes = "wspkg/app/routes"
    paths: list[str] = []
    # 4 route domains, each with several flat-route files (≥ floor 2)
    for dom, n in (("orgs", 6), ("projects", 5), ("runs", 4), ("alerts", 3)):
        for i in range(n):
            paths.append(f"{routes}/_app.{dom}.$slug.page{i}.ts")
    # layout + index shells → residual
    paths += [f"{routes}/_app.tsx", f"{routes}/@.ts", f"{routes}/_app._index.ts"]
    # colocated non-route files under routes/ → residual (must not mint domains)
    paths += [f"{routes}/Button.test.tsx", f"{routes}/loader.server.ts"]
    # a UI components subtree (terminal) → residual
    paths += ["wspkg/app/components/v2/Accordion/Accordion.tsx",
              "wspkg/app/components/v2/Button/Button.tsx"]
    # THE outlier: one owned file outside the wspkg/ prefix (collapses the
    # naive common-prefix to 0 — exactly the trigger.dev shape).
    paths.append("internal/testcontainers/src/wspkg.ts")
    return _owned("wspkg", paths, uuid=uuid)


def test_flat_routes_workspace_decomposes_into_route_domains() -> None:
    anchor = _flat_routes_anchor("u")
    feats = [*_peers(8), anchor]
    res = subdecompose_oversized_features(feats)
    assert res.features_split == 1, "the flat-routes anchor must decompose"
    minted = {f.name for f in feats if f.split_from == "u"}
    # the four real route domains MUST surface (the flat-route contribution).
    assert {"orgs", "projects", "runs", "alerts"} <= minted, minted
    # NO per-component / view junk: the ``components/v2/<Name>`` subtree is
    # terminal, so no Accordion/Button feature is minted, and no layout/index/
    # test/server route shell becomes a domain.
    assert "accordion" not in minted and "button" not in minted, minted
    assert "_app" not in minted and "tsx" not in minted, minted
    # (the package-dir residual bucket ``wspkg-2`` from the non-route component
    # files + the cross-package outlier is ORTHOGONAL pre-existing directory
    # behaviour, not part of the flat-route contract, so it is not asserted on.)


def test_flat_routes_split_conserves_files_and_deowns_residual() -> None:
    anchor = _flat_routes_anchor("u")
    feats = [*_peers(8), anchor]
    before: set[str] = set()
    for f in feats:
        before |= set(f.paths)
    res = subdecompose_oversized_features(feats)
    assert res.features_split == 1
    after: set[str] = set()
    for f in feats:
        after |= set(f.paths)
    assert after == before  # nothing lost or gained — pure redistribution
    # the anchor OWNS only its (de-owned) residual now; the route files moved
    src_owned = set(_owned_paths(anchor))
    assert not any("/_app.orgs." in p for p in src_owned)
    # the moved route files are OWNED by the sub-features
    subs = {f.name: f for f in feats if f.split_from == "u"}
    assert all("/_app.orgs." in p for p in _owned_paths(subs["orgs"]))


def test_flat_routes_blob_metric_moves() -> None:
    # The biggest single OWNER must shrink after the flat-routes split — the
    # whole point (cold_eval.owned_max actually moves on a Remix repo).
    anchor = _flat_routes_anchor("u")
    feats = [*_peers(8), anchor]
    biggest_before = max(len(_owned_paths(f)) for f in feats)
    subdecompose_oversized_features(feats)
    biggest_after = max(len(_owned_paths(f)) for f in feats)
    assert biggest_after < biggest_before
