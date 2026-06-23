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
    _confirmed_remix_routes_dirs,
    _domain_key,
    _flat_route_domain,
    _flat_route_scan,
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
    # flat-route fast-path does. Each MARKER-bearing flat-route FILE keys by its
    # route domain — the ``_app`` / ``$`` / ``@`` markers SELF-confirm the dir as
    # Remix (no explicit confirmed-set needed), so these resolve standalone.
    for start in (0, 2):
        assert _domain_key(
            "apps/web/app/routes/_app.orgs.$org.projects.$p.alerts.new.ts", start,
        ) == "apps/web/app/routes/orgs"
        assert _domain_key(
            "apps/web/app/routes/@.runs.$runParam.ts", start,
        ) == "apps/web/app/routes/runs"
        # A MARKER-LESS flat-route file (``account.tokens`` — no ``_``/``$``/``@``)
        # is genuinely ambiguous with Express ``auth.routes.ts`` in ISOLATION, so
        # it does NOT self-confirm. It parses only once its dir is CONFIRMED Remix
        # by a sibling marker — modelled here by passing the confirmed-set the
        # per-feature pre-pass builds. This is the universal-safety contract: a
        # marker-less name keys by domain iff its dir is proven Remix.
        confirmed = frozenset({"apps/web/app/routes"})
        assert _domain_key(
            "apps/web/app/routes/account.tokens.ts", start, confirmed,
        ) == "apps/web/app/routes/account"
        # …and WITHOUT confirmation it falls through to the legacy descent (the
        # marker-less name alone cannot be distinguished from a non-Remix file).
        assert _domain_key(
            "apps/web/app/routes/account.tokens.ts", start,
        ) is None


def test_domain_key_flat_route_directory_keys_by_virtual_domain() -> None:
    # A dot-name DIRECTORY (route + colocated files inside) keys the SAME way —
    # but a MARKER-LESS folder name (``account.tokens``, no ``$``/``@``) only
    # parses once its routes dir is CONFIRMED Remix (the universal-safety
    # contract; ``route.tsx`` co-location is NOT a confirmation signal because
    # ``route.ts`` is also Next.js/Express's own filename).
    confirmed = frozenset({"apps/web/app/routes"})
    for start in (0, 2):
        assert _domain_key(
            "apps/web/app/routes/account.tokens/route.tsx", start, confirmed,
        ) == "apps/web/app/routes/account"
        # A ``$``-param dotted directory SELF-confirms (no external set needed).
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


# ── UNIVERSAL-SAFETY MISFIRE REGRESSIONS (2026-06-23) ────────────────────────
#
# The old gate decided "is this a flat route?" by a DENYLIST
# (``_NON_ROUTE_DOT_SUFFIXES``): a dotted name FIRED unless one of its segments
# was an enumerated type/role token. Wrong polarity — anything NOT enumerated
# misfired. ``src/routes/auth.routes.ts`` → ``[auth, routes]``, ``routes`` not in
# the denylist → parsed as a flat route → minted an ``auth`` domain on a NON-Remix
# Express repo; the entire NestJS/Angular file-suffix vocabulary
# (``.controller`` ``.service`` ``.dto`` ``.guard`` ``.resolver`` ``.entity`` …)
# misfired the same way → CHANGED non-Remix decomposition (novu/NestJS at risk).
#
# The fix: a dir is Remix flat-routes ONLY by a POSITIVE marker
# (``_app`` / ``$slug`` / ``@`` / co-located ``route.tsx``) — markers Express /
# NestJS / Angular filenames NEVER carry. These tests PROVE the fixed
# ``_domain_key`` is byte-IDENTICAL to the legacy directory descent for every
# such non-Remix path (the flat-route branch never fires), and that an adversarial
# NestJS/Express ``api`` feature does NOT split.

# The full NestJS / Angular file-role suffix vocabulary. A ``routes/`` dir holding
# only ``<thing>.<suffix>.ts`` files (no Remix marker) must NEVER be confirmed
# Remix → every such file is byte-identical to the legacy descent. Universal
# framework vocabulary, no repo-specific name (rule-no-repo-specific-paths).
_NESTJS_ANGULAR_SUFFIXES = (
    "controller", "service", "dto", "guard", "middleware", "resolver",
    "entity", "gateway", "repository", "interceptor", "pipe", "filter",
    "handler", "module", "decorator", "strategy", "schema", "validator",
    "subscriber", "command", "query", "event", "saga", "component",
    "directive", "model", "config",
)


def _legacy_domain_key(path: str, start: int = 0) -> str | None:
    """The pre-fix directory descent — the byte-identity ORACLE. This is a
    faithful copy of the ORDINARY (non-flat-route) walk that runs whenever the
    flat-route branch returns ``False``; a non-Remix path MUST resolve to exactly
    this. We copy it here (rather than reaching into the module) so the test pins
    the legacy contract independently of how the flat-route fast-path is wired."""
    from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
        _DEPTH_CAP, _is_component_name, _is_terminal, _is_transparent,
    )
    segs = path.split("/")
    i, depth = start, 0
    while i < len(segs) - 1 and depth < _DEPTH_CAP:
        seg = segs[i]
        if _is_terminal(seg):
            return None
        if not _is_transparent(seg):
            if _is_component_name(seg):
                return None
            return "/".join(segs[: i + 1])
        i += 1
        depth += 1
    return None


def test_domain_key_nestjs_controller_in_routes_dir_not_split() -> None:
    # NestJS ``routes/users.controller.ts`` (and friends) have NO Remix marker →
    # the ``routes`` dir is NOT confirmed → byte-identical to the legacy descent.
    for start in (0, 2):
        for p in (
            "src/routes/users.controller.ts",
            "src/routes/auth.controller.ts",
            "apps/api/routes/orders.controller.ts",
        ):
            assert _domain_key(p, start) == _legacy_domain_key(p, start), p


def test_domain_key_express_routes_file_in_routes_dir_not_split() -> None:
    # Express ``src/routes/auth.routes.ts`` → ``[auth, routes]``; ``routes`` is
    # NOT a Remix marker → dir unconfirmed → byte-identical to legacy descent.
    for start in (0, 2):
        for p in (
            "src/routes/auth.routes.ts",
            "src/routes/users.routes.ts",
            "src/routes/index.routes.ts",
            "apps/server/routes/billing.routes.ts",
        ):
            assert _domain_key(p, start) == _legacy_domain_key(p, start), p


def test_domain_key_full_nestjs_angular_vocabulary_under_routes_not_flat() -> None:
    # The ENTIRE NestJS/Angular file-role vocabulary, each as ``<thing>.<suffix>``
    # under a ``routes/`` dir, must be byte-identical to the legacy descent — none
    # carries a Remix marker, so none confirms the dir.
    for suffix in _NESTJS_ANGULAR_SUFFIXES:
        for p in (
            f"src/routes/auth.{suffix}.ts",
            f"modules/billing/routes/billing.{suffix}.ts",
        ):
            for start in (0, 2):
                assert _domain_key(p, start) == _legacy_domain_key(p, start), p
            # AND the flat-route scan itself must report "not a flat route".
            assert _flat_route_scan(p.split("/"), 0) is False, p


def test_domain_key_module_subdir_routes_keeps_module_domain() -> None:
    # A NestJS module-subdir ``modules/auth/routes/auth.routes.ts``: the ``routes``
    # dir is unconfirmed (marker-less), so the descent keeps the MODULE domain
    # (``modules`` is transparent → first real domain is ``auth``), exactly as the
    # legacy behaviour — NOT a flat-route ``routes/auth`` split.
    p = "modules/auth/routes/auth.routes.ts"
    assert _domain_key(p, 0) == _legacy_domain_key(p, 0) == "modules/auth"
    p2 = "src/modules/payments/routes/payments.controller.ts"
    assert _domain_key(p2, 0) == _legacy_domain_key(p2, 0) == "src/modules/payments"


def test_confirmed_set_excludes_express_nestjs_routes_dirs() -> None:
    # The pre-pass must NOT confirm any Express/NestJS ``routes/`` dir — they hold
    # only marker-less files. (Direct test of the positive-signal pre-pass.)
    owned = [
        "src/routes/auth.routes.ts",
        "src/routes/users.routes.ts",
        "src/routes/auth.controller.ts",
        "modules/auth/routes/auth.service.ts",
        "modules/billing/routes/billing.dto.ts",
    ]
    assert _confirmed_remix_routes_dirs(owned) == frozenset()


def test_confirmed_set_includes_only_dirs_with_a_marker() -> None:
    # A Remix dir (has ``_app`` / ``$`` markers) is confirmed; a sibling Express
    # ``routes`` dir on the SAME repo (marker-less) is NOT — the confirmation is
    # PER-DIR, not global.
    owned = [
        "apps/web/app/routes/_app.orgs.$slug.tsx",   # Remix marker → confirms
        "apps/web/app/routes/account.tokens.tsx",    # marker-less sibling
        "apps/api/src/routes/auth.routes.ts",        # Express → no marker
        "apps/api/src/routes/auth.controller.ts",
    ]
    assert _confirmed_remix_routes_dirs(owned) == frozenset(
        {"apps/web/app/routes"}
    )


def test_confirmed_remix_dir_resolves_markerless_sibling() -> None:
    # Inside a CONFIRMED Remix dir, a marker-less sibling (``account.tokens``)
    # keys by its route domain — the dir is proven Remix by the ``_app`` file.
    owned = [
        "apps/web/app/routes/_app.orgs.$slug.tsx",
        "apps/web/app/routes/account.tokens.tsx",
    ]
    confirmed = _confirmed_remix_routes_dirs(owned)
    assert _domain_key(
        "apps/web/app/routes/account.tokens.tsx", 2, confirmed,
    ) == "apps/web/app/routes/account"


# ── End-to-end: an adversarial NestJS/Express anchor must NOT decompose ──────


def _nestjs_express_api_anchor(uuid: str = "n") -> Feature:
    """SYNTHETIC adversarial NestJS/Express ``api`` feature (no real paths): an
    oversized feature whose files live under a ``routes/`` dir but are ALL
    conventional ``*.routes.ts`` / ``*.controller.ts`` / ``*.service.ts`` modules
    — i.e. the exact shape the old denylist gate misfired on (it split this into
    ``routes/auth`` + ``routes/users``). The fix must leave it UNTOUCHED by the
    flat-route branch (it has no directory-tree domains either, so it does not
    decompose at all)."""
    paths: list[str] = []
    for dom in ("auth", "users", "billing", "orders"):
        paths += [
            f"src/routes/{dom}.routes.ts",
            f"src/routes/{dom}.controller.ts",
            f"src/routes/{dom}.service.ts",
        ]
    return _owned("api", paths, uuid=uuid)


def test_nestjs_express_api_anchor_does_not_flat_route_split() -> None:
    # The adversarial NestJS/Express anchor is oversized but has NO Remix marker
    # anywhere → the flat-route branch never fires. The decomposition is whatever
    # the LEGACY directory descent produces (here: no domains — everything is a
    # marker-less file directly under a transparent ``routes`` dir → residual),
    # i.e. byte-identical to pre-fix. Crucially it must NOT mint ``auth``/``users``
    # route domains the way the buggy denylist did.
    anchor = _nestjs_express_api_anchor("n")
    feats = [*_peers(8), anchor]
    res = subdecompose_oversized_features(feats)
    minted = {f.name for f in feats if f.split_from == "n"}
    assert "auth" not in minted, minted
    assert "users" not in minted, minted
    assert "billing" not in minted and "orders" not in minted, minted
    # No flat-route split happened: the pre-pass confirmed ZERO Remix dirs.
    assert _confirmed_remix_routes_dirs(_owned_paths(anchor)) == frozenset()


def test_nestjs_express_api_domain_key_byte_identical_to_legacy() -> None:
    # Every owned file of the adversarial anchor resolves byte-identically to the
    # legacy descent — the strongest universal-safety statement.
    anchor = _nestjs_express_api_anchor("n")
    owned = _owned_paths(anchor)
    confirmed = _confirmed_remix_routes_dirs(owned)
    from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import _common_segments
    start = _common_segments(owned)
    for p in owned:
        assert _domain_key(p, start, confirmed) == _legacy_domain_key(p, start), p


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


def test_strong_marker_only_confirms_no_underscore_or_route_module_leak() -> None:
    """REGRESSION (re-audit 2026-06-23): confirmation requires a ``$``-param or
    ``@``-escape marker in a DOTTED name — a ``_``-prefix alone (shared with TS
    barrels / NestJS bases / private modules) and a bare ``route.ts`` (Next.js's
    own handler filename) must NOT confirm a non-Remix ``routes/`` dir, or the
    Express/NestJS misfire reopens."""
    leak_vectors = {
        "express_lone__index": ["src/routes/auth.controller.ts", "src/routes/_index.ts"],
        "express_route_module": ["src/routes/auth.controller.ts", "src/routes/orders/route.ts"],
        "nestjs__private_service": [
            "modules/auth/routes/auth.routes.ts",
            "modules/auth/routes/_private.service.ts",
        ],
        "nestjs__base_controller": ["src/routes/users.controller.ts", "src/routes/_base.controller.ts"],
        "deep_route_module_ancestor": [
            "pkg/routes/sub/routes/things/route.ts",
            "pkg/routes/auth.controller.ts",
        ],
        "next__app_standalone": ["src/routes/_app.tsx", "src/routes/users.controller.ts"],
    }
    for label, paths in leak_vectors.items():
        assert _confirmed_remix_routes_dirs(paths) == frozenset(), f"LEAK: {label}"


def test_dollar_param_confirms_genuine_remix_dir() -> None:
    """A genuine Remix dir (any one ``$``-param dynamic route) confirms; its
    marker-less static siblings then parse."""
    paths = [
        "apps/webapp/app/routes/_app.orgs.$organizationSlug.projects.tsx",
        "apps/webapp/app/routes/account.tokens.tsx",  # marker-less sibling
    ]
    assert _confirmed_remix_routes_dirs(paths) == frozenset({"apps/webapp/app/routes"})
