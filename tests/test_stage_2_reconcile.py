"""Tests for ``faultline.pipeline_v2.stage_2_reconcile``.

We construct :class:`AnchorCandidate` lists directly to exercise the
merge / priority / attribution / LLM-2nd-opinion paths in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2 import (
    AnchorCandidate,
    ScanContext,
    stage_2_reconcile,
)
from faultline.pipeline_v2.stage_2_reconcile import (
    DeveloperFeature,
    _jaccard,
    _slug_tokens,
)


def _ctx(tmp_path: Path, files: list[str]) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


def _cand(name: str, source: str, paths: tuple[str, ...] = (),
          confidence_self: float = 0.7) -> AnchorCandidate:
    return AnchorCandidate(
        name=name, source=source, paths=paths, confidence_self=confidence_self,
    )


# ── merge by name ──────────────────────────────────────────────────────────


def test_two_sources_same_name_merge_to_high_confidence(tmp_path: Path) -> None:
    """Route + package both say "billing" → one merged feature, sources
    listed in priority order (package first)."""
    cands = {
        "route":   [_cand("billing", "route", ("app/billing/page.tsx",))],
        "package": [_cand("billing", "package", (".",))],
    }
    ctx = _ctx(tmp_path, files=["app/billing/page.tsx", "package.json"])

    result = stage_2_reconcile(cands, ctx)

    assert len(result.features) == 1
    f = result.features[0]
    assert f.name == "billing"
    assert f.confidence == "high"
    # package outranks route per priority — listed first.
    assert f.sources == ["package", "route"]


def test_single_source_yields_medium_confidence(tmp_path: Path) -> None:
    # Single NON-schema source → medium confidence. (A schema-only
    # single source is now suppressed as a phantom — see
    # test_schema_only_feature_is_suppressed — so we use ``config`` here
    # to isolate the confidence rule from the suppression rule.)
    cands = {
        "config": [_cand("subscription", "config", ("billing.config.ts",))],
    }
    ctx = _ctx(tmp_path, files=["billing.config.ts"])
    result = stage_2_reconcile(cands, ctx)
    assert len(result.features) == 1
    assert result.features[0].confidence == "medium"
    assert result.features[0].sources == ["config"]


def test_conflicting_slugs_pick_by_priority(tmp_path: Path) -> None:
    """``users`` (route) and ``user-api`` (package) overlap on tokens.

    Default Jaccard for {"users"} vs {"user", "api"} is 0/3 = 0.0 —
    they will NOT merge under threshold 0.7. To exercise the priority
    rule we use slugs that DO share tokens: ``user-mgmt`` (mvc) vs
    ``user-mgmt-api`` (package). Jaccard = 2/3 = 0.67 → no merge at
    default threshold, but at threshold 0.66 they merge. So we pass
    a lower threshold here to force the merge and check priority wins.
    """
    cands = {
        "mvc":     [_cand("user-mgmt", "mvc", ("app/controllers/user_mgmt_controller.rb",))],
        "package": [_cand("user-mgmt-api", "package", (".",))],
    }
    ctx = _ctx(tmp_path, files=["app/controllers/user_mgmt_controller.rb", "package.json"])

    result = stage_2_reconcile(cands, ctx, jaccard_threshold=0.66)

    assert len(result.features) == 1
    f = result.features[0]
    # Package wins the slug because of priority
    assert f.name == "user-mgmt-api"
    assert f.confidence == "high"
    assert f.sources[0] == "package"


def test_disjoint_slugs_stay_separate(tmp_path: Path) -> None:
    # Package anchors carry the MANIFEST FILE since the Product-Spine §4.1
    # bare-dir ban (a bare "." claim is now rejected at claim time).
    cands = {
        "route":   [_cand("billing", "route", ("app/billing/page.tsx",))],
        "package": [_cand("auth",   "package", ("package.json",))],
    }
    ctx = _ctx(tmp_path, files=["app/billing/page.tsx", "package.json"])
    result = stage_2_reconcile(cands, ctx)
    assert {f.name for f in result.features} == {"billing", "auth"}
    # Both single-source ⇒ medium each
    assert all(f.confidence == "medium" for f in result.features)


# ── cross-feature path attribution ────────────────────────────────────────


def test_file_claimed_by_two_features_goes_to_higher_priority(tmp_path: Path) -> None:
    """``app/billing/handler.ts`` claimed by both ``billing`` (route)
    and ``payments`` (package). Package outranks → payments keeps it,
    billing drops it."""
    cands = {
        "route":   [_cand(
            "billing", "route",
            ("app/billing/handler.ts", "app/billing/page.tsx"),
        )],
        "package": [_cand(
            "payments", "package",
            ("app/billing/handler.ts",),
        )],
    }
    ctx = _ctx(
        tmp_path,
        files=["app/billing/handler.ts", "app/billing/page.tsx"],
    )

    result = stage_2_reconcile(cands, ctx)

    by_name = {f.name: f for f in result.features}
    assert set(by_name) == {"billing", "payments"}
    # payments wins the contested file
    assert "app/billing/handler.ts" in by_name["payments"].paths
    # billing dropped it but kept its other path
    assert "app/billing/handler.ts" not in by_name["billing"].paths
    assert "app/billing/page.tsx" in by_name["billing"].paths


def test_unattributed_paths_returned(tmp_path: Path) -> None:
    cands = {
        "route": [_cand("billing", "route", ("app/billing/page.tsx",))],
    }
    files = ["app/billing/page.tsx", "lib/random-util.ts", "README.md"]
    ctx = _ctx(tmp_path, files=files)
    result = stage_2_reconcile(cands, ctx)
    assert set(result.unattributed) == {"lib/random-util.ts", "README.md"}


def test_empty_candidates_yields_all_files_unattributed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, files=["a.ts", "b.ts"])
    result = stage_2_reconcile({}, ctx)
    assert result.features == []
    assert result.unattributed == ["a.ts", "b.ts"]


def test_errors_sentinel_is_ignored(tmp_path: Path) -> None:
    """Stage 2 must drop the ``_errors`` key without exploding."""
    cands = {
        "route": [_cand("auth", "route", ("app/auth/page.tsx",))],
        "_errors": {"boom": "RuntimeError: x"},  # type: ignore[dict-item]
    }
    ctx = _ctx(tmp_path, files=["app/auth/page.tsx"])
    result = stage_2_reconcile(cands, ctx)
    assert [f.name for f in result.features] == ["auth"]


# ── LLM 2nd-opinion ────────────────────────────────────────────────────────


def test_llm_reconcile_disabled_by_default_no_llm_calls(tmp_path: Path) -> None:
    """Default ``llm_reconcile=False`` MUST NOT invoke the LLM stub."""
    call_log: list[tuple] = []

    def _llm_stub(a, b):  # type: ignore[no-untyped-def]
        call_log.append((a.name, b.name))
        return "should-not-fire"

    cands = {
        # Two slugs in the ambiguous Jaccard band (0.3..0.6)
        "mvc":     [_cand("user-profile", "mvc", ("app/controllers/user_profile_controller.rb",))],
        "package": [_cand("user-account", "package", (".",))],
    }
    ctx = _ctx(tmp_path, files=["app/controllers/user_profile_controller.rb", "package.json"])

    # Force them into the same group by lowering threshold below their Jaccard:
    j = _jaccard(_slug_tokens("user-profile"), _slug_tokens("user-account"))
    assert 0.3 <= j <= 0.6  # sanity — this is exactly the ambiguous band
    result = stage_2_reconcile(
        cands, ctx, jaccard_threshold=j, _llm_call=_llm_stub,
    )

    # No LLM calls
    assert call_log == []
    # Priority rule wins → package's slug
    assert any(f.name == "user-account" for f in result.features)


def test_llm_reconcile_enabled_overrides_priority(tmp_path: Path) -> None:
    """When ``llm_reconcile=True`` and Jaccard is in the ambiguous band,
    the LLM stub's choice wins — even over the priority rule."""
    cands = {
        "mvc":     [_cand("user-profile", "mvc",
                          ("app/controllers/user_profile_controller.rb",))],
        "package": [_cand("user-account", "package", (".",))],
    }
    ctx = _ctx(tmp_path, files=["app/controllers/user_profile_controller.rb", "package.json"])
    j = _jaccard(_slug_tokens("user-profile"), _slug_tokens("user-account"))

    def _llm_stub(a, b):  # type: ignore[no-untyped-def]
        # Pick the LOWER-priority candidate's name to prove the 2nd
        # opinion overrides the priority rule.
        return "user-profile"

    result = stage_2_reconcile(
        cands, ctx,
        jaccard_threshold=j,
        llm_reconcile=True,
        _llm_call=_llm_stub,
    )
    assert any(f.name == "user-profile" for f in result.features)
    # The reconciliation note records the LLM decision
    assert any("llm picked" in n for n in result.notes)


def test_llm_reconcile_returns_none_falls_back_to_priority(tmp_path: Path) -> None:
    """When the LLM stub returns ``None`` (inconclusive), the priority
    rule still wins."""
    cands = {
        "mvc":     [_cand("user-profile", "mvc",
                          ("app/controllers/user_profile_controller.rb",))],
        "package": [_cand("user-account", "package", (".",))],
    }
    ctx = _ctx(tmp_path, files=["app/controllers/user_profile_controller.rb", "package.json"])
    j = _jaccard(_slug_tokens("user-profile"), _slug_tokens("user-account"))

    def _llm_stub(a, b):  # type: ignore[no-untyped-def]
        return None  # inconclusive

    result = stage_2_reconcile(
        cands, ctx,
        jaccard_threshold=j,
        llm_reconcile=True,
        _llm_call=_llm_stub,
    )
    # priority rule → package
    assert any(f.name == "user-account" for f in result.features)


# ── Sprint S4b — URL-ghost purge ──────────────────────────────────────────


def test_s4b_shared_route_file_zero_path_protection(tmp_path: Path) -> None:
    """Three Fastify routes share one source file (the real-world
    pattern from infisical's ``secret-scanner-v2.ts`` declaring
    ``/bitbucket`` + ``/gitlab`` + ``/github``).

    Without the Sprint S4b zero-path protection, two slugs would lose
    the contested file and end up with empty paths → "URL ghost"
    features. With the fix all three keep the file so each remains
    attributable downstream.
    """
    shared_file = "backend/src/server/plugins/secret-scanner-v2.ts"
    cands = {
        "route-fastify": [
            _cand("bitbucket", "route-fastify", (shared_file,)),
            _cand("gitlab", "route-fastify", (shared_file,)),
            _cand("github", "route-fastify", (shared_file,)),
        ],
    }
    ctx = _ctx(tmp_path, files=[shared_file])

    result = stage_2_reconcile(cands, ctx)

    by_name = {f.name: f for f in result.features}
    # All three survive (no zero-path drops).
    assert set(by_name) == {"bitbucket", "gitlab", "github"}
    # Each shares the file rather than one stealing it. Provenance
    # preserved for every slug.
    for name in ("bitbucket", "gitlab", "github"):
        assert shared_file in by_name[name].paths, (
            f"{name} lost its shared file"
        )
    assert result.zero_path_drops_count == 0
    assert result.zero_path_drops_sample == []


def test_s4b_path_contention_winner_still_wins_when_loser_has_other_paths(
    tmp_path: Path,
) -> None:
    """Zero-path protection must NOT regress the existing "loser
    drops contested path" semantics when the loser has fallback paths.

    Replays
    ``test_file_claimed_by_two_features_goes_to_higher_priority`` to
    verify the new pass-2 only fires when the loser would be orphaned.
    """
    cands = {
        "route":   [_cand(
            "billing", "route",
            ("app/billing/handler.ts", "app/billing/page.tsx"),
        )],
        "package": [_cand(
            "payments", "package",
            ("app/billing/handler.ts",),
        )],
    }
    ctx = _ctx(
        tmp_path,
        files=["app/billing/handler.ts", "app/billing/page.tsx"],
    )

    result = stage_2_reconcile(cands, ctx)

    by_name = {f.name: f for f in result.features}
    assert set(by_name) == {"billing", "payments"}
    # payments wins outright — the loser ('billing') has another path
    # to fall back on, so the strip applies normally.
    assert "app/billing/handler.ts" in by_name["payments"].paths
    assert "app/billing/handler.ts" not in by_name["billing"].paths
    assert "app/billing/page.tsx" in by_name["billing"].paths


def test_s4b_zero_path_features_dropped_with_telemetry(tmp_path: Path) -> None:
    """Defensive backstop: a candidate that somehow arrives with no
    paths gets dropped after reconciliation and surfaces in telemetry.

    Constructs a candidate with empty paths directly — simulating any
    future extractor that emits a name but loses path attribution.
    """
    cands = {
        "config": [_cand("phantom-feature", "config", ())],
        "route":  [_cand("billing", "route", ("app/billing.tsx",))],
    }
    ctx = _ctx(tmp_path, files=["app/billing.tsx"])

    result = stage_2_reconcile(cands, ctx)

    # billing survives; phantom-feature dropped.
    names = {f.name for f in result.features}
    assert names == {"billing"}
    assert result.zero_path_drops_count == 1
    assert "phantom-feature" in result.zero_path_drops_sample
    # Note logged for downstream telemetry consumers.
    assert any("zero-path" in n for n in result.notes)


def test_s4b_no_zero_path_drops_in_normal_case(tmp_path: Path) -> None:
    """Sanity: a healthy reconciliation reports zero-path-drops = 0."""
    # Manifest-file package anchor (post spine-§4.1; a bare "." would be
    # rejected by the bare-dir ban and dropped as zero-path).
    cands = {
        "route":   [_cand("billing", "route", ("app/billing/page.tsx",))],
        "package": [_cand("payments", "package", ("package.json",))],
    }
    ctx = _ctx(
        tmp_path,
        files=["app/billing/page.tsx", "package.json"],
    )

    result = stage_2_reconcile(cands, ctx)

    assert result.zero_path_drops_count == 0
    assert result.zero_path_drops_sample == []


# ── 2026-06 metric-honesty review: source-priority completeness ────────────


def test_every_builtin_extractor_source_has_explicit_priority() -> None:
    """Every built-in extractor's emitted ``source`` string must have an
    explicit entry in ``_SOURCE_PRIORITY``.

    Regression guard for the 2026-06 metric-honesty review:
    ``fastapi-route`` (and 9 other newer extractors) were missing from
    the dict, silently defaulted to priority 0, and lost every
    file-ownership conflict — even to ``config``. Extractors emit
    ``source=self.name`` (contract documented on ``AnchorExtractor``),
    so introspecting ``name`` covers the emitted source strings; if a
    future extractor ever emits a different source string, this test
    must be extended to map it.
    """
    from faultline.pipeline_v2.stage_1_extractors import (
        _load_default_extractors,
    )
    from faultline.pipeline_v2.stage_2_reconcile import _SOURCE_PRIORITY

    extractors = _load_default_extractors()
    assert extractors, "no built-in extractors loaded — broken test env"
    missing = sorted(
        ex.name for ex in extractors if ex.name not in _SOURCE_PRIORITY
    )
    assert not missing, (
        f"extractor sources without an explicit _SOURCE_PRIORITY entry: "
        f"{missing} — add them at the tier matching their semantics "
        f"(declared HTTP entry points = 4, manifests = 5, module "
        f"structure = 3) or they default to 0 and lose every "
        f"file-ownership conflict."
    )


# ── Cross-extractor fragment dedup (fix/stage2-dedup) ─────────────────────


from faultline.pipeline_v2.stage_2_reconcile import (  # noqa: E402
    _file_overlap_should_merge,
    _normalized_tokens,
    _should_merge,
    _token_containment,
)


def test_stop_prefix_normalization() -> None:
    """URL-structure tokens (api/internal/v1..v3) are stripped; a slug
    made ONLY of structural tokens falls back to its raw token set."""
    assert _normalized_tokens("api-org-knowledge") == frozenset({"org", "knowledge"})
    assert _normalized_tokens("internal-billing-v2") == frozenset({"billing"})
    # All-structural slug must NOT normalize to the empty set.
    assert _normalized_tokens("api-v1") == frozenset({"api", "v1"})


def test_should_merge_api_prefix_fragments() -> None:
    """``api-org-knowledge`` ≡ ``org-knowledge`` after normalization
    (raw Jaccard 0.667 < 0.7 — the original failure shape)."""
    assert _should_merge("api-org-knowledge", "org-knowledge")


def test_should_merge_token_containment() -> None:
    """≥2-token strict subset merges regardless of Jaccard; 1-token
    subsets do NOT (name-only containment is too weak)."""
    assert _should_merge("org-knowledge", "org-knowledge-base")
    assert not _should_merge("auth", "auth-tokens")
    assert not _token_containment(frozenset({"auth"}), frozenset({"auth", "tokens"}))
    assert _token_containment(
        frozenset({"auth"}), frozenset({"auth", "tokens"}), min_subset_tokens=1,
    )


def test_soc0_shaped_fragment_triple_merges(tmp_path: Path) -> None:
    """The Soc0 failure shape: fastapi-route emits ``api-org-knowledge``
    and route emits ``org-knowledge`` over the same router file. They
    must collapse into ONE feature named by the higher-priority claim,
    with merge lineage recorded."""
    shared = ("src/api/org_knowledge.py",)
    cands = {
        "fastapi-route": [_cand("api-org-knowledge", "fastapi-route", shared)],
        "route":         [_cand("org-knowledge", "route", shared)],
    }
    ctx = _ctx(tmp_path, files=list(shared))

    result = stage_2_reconcile(cands, ctx)

    assert len(result.features) == 1
    f = result.features[0]
    # Both sources are priority 4; ranking tie-breaks by name asc →
    # ``api-org-knowledge`` wins canonically; the loser is lineage.
    assert set(f.merged_from) | {f.name} == {"api-org-knowledge", "org-knowledge"}
    assert f.confidence == "high"
    assert set(f.sources) == {"fastapi-route", "route"}
    assert "merged_from:" in f.rationale


def test_file_overlap_schema_route_do_not_merge(tmp_path: Path) -> None:
    """Refuted-finding guard: a schema model and a route feature that
    share files (one schema.prisma holds every model) are NOT the same
    feature and must not merge via file overlap.

    Post-suppression: because the slugs differ (``document`` model vs
    ``document-editor`` route) they do NOT name-merge, so ``document``
    stays a bare schema-only feature and is then SUPPRESSED as a
    phantom. The load-bearing assertion for this guard is that they did
    not fuse into a single ``document-editor`` feature carrying the
    schema source — they were resolved independently. The route feature
    survives intact.
    """
    cands = {
        "schema": [_cand("document", "schema", ("prisma/schema.prisma",))],
        "route":  [_cand(
            "document-editor", "route",
            ("prisma/schema.prisma", "app/editor/page.tsx"),
        )],
    }
    ctx = _ctx(
        tmp_path,
        files=["prisma/schema.prisma", "app/editor/page.tsx"],
    )

    result = stage_2_reconcile(cands, ctx)

    # No merge: the route feature is alone (not fused with the schema
    # model), and the bare schema model is suppressed as a phantom.
    assert {f.name for f in result.features} == {"document-editor"}
    assert result.schema_only_suppressed_sample == ["document"]


def test_file_overlap_same_source_siblings_do_not_merge(tmp_path: Path) -> None:
    """Same-source fragments sharing a file are usually genuinely
    distinct routes declared in one module — never merged by overlap."""
    shared = ("src/webhooks.ts",)
    cands = {
        "route": [
            _cand("github-webhook", "route", shared),
            _cand("gitlab-webhook", "route", shared),
        ],
    }
    ctx = _ctx(tmp_path, files=["src/webhooks.ts"])

    result = stage_2_reconcile(cands, ctx)

    assert {f.name for f in result.features} == {
        "github-webhook", "gitlab-webhook",
    }


def test_file_overlap_cross_source_one_token_containment_merges(
    tmp_path: Path,
) -> None:
    """Cross-source + shared anchor files + 1-token containment →
    merge (the file evidence carries the weight the name lacks)."""
    cands = {
        "package": [_cand("auth", "package", ("packages/auth/index.ts",))],
        "route":   [_cand(
            "auth-tokens", "route",
            ("packages/auth/index.ts", "app/auth/tokens/page.tsx"),
        )],
    }
    ctx = _ctx(
        tmp_path,
        files=["packages/auth/index.ts", "app/auth/tokens/page.tsx"],
    )

    result = stage_2_reconcile(cands, ctx)

    assert len(result.features) == 1
    f = result.features[0]
    assert f.name == "auth"  # package outranks route
    assert f.merged_from == ["auth-tokens"]
    assert set(f.paths) == {
        "packages/auth/index.ts", "app/auth/tokens/page.tsx",
    }


def test_file_overlap_disjoint_names_do_not_merge() -> None:
    """File overlap alone is NOT enough — a name signal must agree
    (this is what distinguishes the rule from the refuted naive one)."""
    a = _cand("billing", "route", ("app/billing/handler.ts",))
    b = _cand("payments", "package", ("app/billing/handler.ts",))
    assert not _file_overlap_should_merge(a, b)


def test_file_overlap_requires_half_of_smaller_set() -> None:
    """Path-overlap guard is containment-oriented and scale-invariant:
    the shared files must cover ≥ half of the SMALLER path set."""
    small = _cand("org-export", "route", ("a.py", "b.py"))
    big = _cand(
        "org", "package",
        ("a.py", "x.py", "y.py", "z.py"),
    )
    # overlap=1, smaller=2 → 1*2 < 2 is False → passes the file guard,
    # and 1-token containment ({org} ⊂ {org,export}) supplies the name
    # signal → merges.
    assert _file_overlap_should_merge(small, big)
    tiny_overlap = _cand("org-export", "route", ("c.py", "d.py", "e.py", "f.py", "b.py"))
    # overlap=0 with big → no merge.
    assert not _file_overlap_should_merge(tiny_overlap, big)


def test_no_merge_without_file_overlap_or_name_signal(tmp_path: Path) -> None:
    """Cross-source candidates with weak-Jaccard names and NO shared
    files stay separate (regression guard: the new predicates must not
    loosen the disjoint case)."""
    cands = {
        "package": [_cand("workspace-settings", "package", ("packages/ws/index.ts",))],
        "route":   [_cand("workspace-billing-portal", "route", ("app/billing/page.tsx",))],
    }
    ctx = _ctx(tmp_path, files=["packages/ws/index.ts", "app/billing/page.tsx"])

    result = stage_2_reconcile(cands, ctx)

    assert {f.name for f in result.features} == {
        "workspace-settings", "workspace-billing-portal",
    }


# ── Schema-only phantom suppression (2026-06) ──────────────────────────────


def test_schema_only_feature_is_suppressed(tmp_path: Path) -> None:
    """A bare Prisma model with no owning code (anchor = only the shared
    schema file) must NOT be emitted as a standalone developer feature.

    This is the cal.com host-group/credential/... phantom-dup root
    cause: dozens of schema-only models that each spawn a feature and
    then get the same import-closure cloned onto them.
    """
    cands = {
        "schema": [
            _cand("host-group", "schema", ("packages/prisma/schema.prisma",)),
            _cand("credential", "schema", ("packages/prisma/schema.prisma",)),
            _cand("organization-settings", "schema",
                  ("packages/prisma/schema.prisma",)),
        ],
    }
    ctx = _ctx(tmp_path, files=["packages/prisma/schema.prisma"])

    result = stage_2_reconcile(cands, ctx)

    assert result.features == []
    assert result.schema_only_suppressed_count == 3
    assert set(result.schema_only_suppressed_sample) == {
        "host-group", "credential", "organization-settings",
    }
    assert any("schema-only phantom" in n for n in result.notes)


def test_schema_model_with_own_route_survives(tmp_path: Path) -> None:
    """A model that ALSO has its own route name-merges with the route
    candidate and gains a non-schema source → it survives.

    ``booking`` (Prisma model) + ``booking`` (route on a real page) is a
    real product feature, not a bare data entity.
    """
    cands = {
        "schema": [_cand("booking", "schema", ("packages/prisma/schema.prisma",))],
        "route":  [_cand("booking", "route",
                         ("apps/web/app/booking/[uid]/page.tsx",))],
    }
    ctx = _ctx(tmp_path, files=[
        "packages/prisma/schema.prisma",
        "apps/web/app/booking/[uid]/page.tsx",
    ])

    result = stage_2_reconcile(cands, ctx)

    assert len(result.features) == 1
    f = result.features[0]
    assert f.name == "booking"
    assert "route" in f.sources  # gained a code source → not a phantom
    assert result.schema_only_suppressed_count == 0


def test_schema_model_with_own_module_survives(tmp_path: Path) -> None:
    """A model with its own feature module (package source) survives —
    the package candidate carries the owning code, so the merged feature
    is not schema-only."""
    cands = {
        "schema":  [_cand("membership", "schema",
                          ("packages/prisma/schema.prisma",))],
        "package": [_cand("membership", "package",
                          ("packages/features/membership/index.ts",))],
    }
    ctx = _ctx(tmp_path, files=[
        "packages/prisma/schema.prisma",
        "packages/features/membership/index.ts",
    ])

    result = stage_2_reconcile(cands, ctx)

    assert {f.name for f in result.features} == {"membership"}
    assert result.schema_only_suppressed_count == 0


def test_rails_models_only_suppressed_but_with_routes_survives(
    tmp_path: Path,
) -> None:
    """``rails-models`` is the same schema-declaration class as Prisma.

    A models-only resource (no controller/route/view) is suppressed; a
    resource whose model name-merges with a ``rails-routes`` candidate
    survives.
    """
    bare = {
        "rails-models": [_cand("audit-log", "rails-models",
                              ("app/models/audit_log.rb",))],
    }
    ctx = _ctx(tmp_path, files=["app/models/audit_log.rb"])
    res_bare = stage_2_reconcile(bare, ctx)
    assert res_bare.features == []
    assert res_bare.schema_only_suppressed_count == 1

    real = {
        "rails-models": [_cand("invoice", "rails-models",
                              ("app/models/invoice.rb",))],
        "rails-routes": [_cand("invoice", "rails-routes",
                              ("app/controllers/invoices_controller.rb",))],
    }
    ctx2 = _ctx(tmp_path, files=[
        "app/models/invoice.rb",
        "app/controllers/invoices_controller.rb",
    ])
    res_real = stage_2_reconcile(real, ctx2)
    assert {f.name for f in res_real.features} == {"invoice"}
    assert res_real.schema_only_suppressed_count == 0


def test_schema_plus_js_library_not_suppressed(tmp_path: Path) -> None:
    """A Prisma ENUM re-exported through a js-library barrel carries a
    ``js-library`` source → NOT a schema-only phantom, so this rule
    leaves it alone (the Stage 8.8 barrel guard owns that fan-out)."""
    cands = {
        "schema":     [_cand("period-type", "schema",
                            ("packages/prisma/schema.prisma",))],
        "js-library": [_cand("period-type", "js-library",
                            ("packages/platform/libraries/index.ts",))],
    }
    ctx = _ctx(tmp_path, files=[
        "packages/prisma/schema.prisma",
        "packages/platform/libraries/index.ts",
    ])

    result = stage_2_reconcile(cands, ctx)

    assert {f.name for f in result.features} == {"period-type"}
    assert result.schema_only_suppressed_count == 0


def test_no_op_when_no_schema_only_features(tmp_path: Path) -> None:
    """A repo with only real (code-anchored) features is untouched — the
    suppression is a no-op and the count is 0."""
    cands = {
        "route":   [_cand("dashboard", "route", ("app/dashboard/page.tsx",))],
        "package": [_cand("billing", "package", ("packages/billing/index.ts",))],
    }
    ctx = _ctx(tmp_path, files=[
        "app/dashboard/page.tsx", "packages/billing/index.ts",
    ])

    result = stage_2_reconcile(cands, ctx)

    assert {f.name for f in result.features} == {"dashboard", "billing"}
    assert result.schema_only_suppressed_count == 0
    assert result.schema_only_suppressed_sample == []


def test_suppression_scale_invariant_tiny_and_large(tmp_path: Path) -> None:
    """No magic numbers: the rule fires identically for a 1-model schema
    and a 200-model schema. Structural, not count-based."""
    # Tiny: a single schema-only model is suppressed.
    tiny = {"schema": [_cand("widget", "schema", ("db/schema.prisma",))]}
    r_tiny = stage_2_reconcile(tiny, _ctx(tmp_path, files=["db/schema.prisma"]))
    assert r_tiny.features == []
    assert r_tiny.schema_only_suppressed_count == 1

    # Large: 200 schema-only models all suppressed; the one with a route
    # survives — proving the rule is per-feature structural, not a
    # corpus-tuned threshold.
    big_cands = {
        "schema": [
            _cand(f"model-{i}", "schema", ("db/schema.prisma",))
            for i in range(200)
        ],
        "route": [_cand("model-7", "route", ("app/model7/page.tsx",))],
    }
    ctx = _ctx(tmp_path, files=["db/schema.prisma", "app/model7/page.tsx"])
    r_big = stage_2_reconcile(big_cands, ctx)
    assert {f.name for f in r_big.features} == {"model-7"}
    assert r_big.schema_only_suppressed_count == 199
