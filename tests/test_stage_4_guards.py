"""Tests for Stage 4 structural admission guards (Sprint S2b).

Covers the six required scenarios:

  1. Root-level product-config singleton (``tauri.conf.json``) → admitted.
  2. Generic-stem singleton (``src/lib/utils.ts``) → dropped.
  3. Singleton whose name overlaps a Stage 2 anchor → admitted.
  4. Cohesive multi-path cluster (same parent dir) → admitted unchanged.
  5. Incoherent multi-path cluster (mixed parents AND mixed top-2
     segments) → split into singletons + each re-checked.
  6. Telemetry counts match drop events.

Plus targeted helper-level coverage for the structural primitives.
"""

from __future__ import annotations

from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature
from faultline.pipeline_v2.stage_4_guards import (
    DropEvent,
    GuardResult,
    _is_admissible_singleton,
    _is_cohesive_cluster,
    _is_distinct_product_noun,
    _is_root_product_config,
    _leaf_stem,
    _overlaps_anchor_tokens,
    _slug_tokens,
    apply_stage_4_guards,
)


def _feat(name: str, paths: tuple[str, ...], src: str = "llm-fallback") -> DeveloperFeature:
    return DeveloperFeature(
        name=name,
        paths=paths,
        sources=[src],
        confidence="low",
    )


# ── Helper primitives ───────────────────────────────────────────────


def test_leaf_stem_normalises_filename_variants() -> None:
    assert _leaf_stem("README.md") == "readme"
    assert _leaf_stem(".gitignore") == "gitignore"
    assert _leaf_stem("prettier.config.js") == "prettier-config"
    assert _leaf_stem("docker/Dockerfile") == "dockerfile"
    assert _leaf_stem("apps/web/utils.ts") == "utils"
    assert _leaf_stem("") == ""


def test_slug_tokens_strips_generics() -> None:
    # "billing" and "portal" survive; "api", "lib", "core" are stripped.
    assert _slug_tokens("billing-portal") == {"billing", "portal"}
    assert _slug_tokens("api-core-lib") == set()
    assert _slug_tokens("auth-session") == {"auth", "session"}
    assert _slug_tokens("") == set()


def test_is_root_product_config_only_at_root() -> None:
    assert _is_root_product_config("tauri.conf.json")
    assert _is_root_product_config("app.json")
    assert _is_root_product_config("vercel.json")
    assert _is_root_product_config("manifest.yaml")
    # Nested → not a root product config (Stage 2 should own those).
    assert not _is_root_product_config("apps/web/tauri.conf.json")
    # Wrong extension → not a config.
    assert not _is_root_product_config("README.md")


def test_is_distinct_product_noun_recognises_generic_stems() -> None:
    # Distinct product nouns.
    assert _is_distinct_product_noun("apps/auth/totp.ts")
    assert _is_distinct_product_noun("billing.tsx")
    # Universal generic stems → not distinct.
    assert not _is_distinct_product_noun("src/lib/utils.ts")
    assert not _is_distinct_product_noun("apps/api/index.ts")
    assert not _is_distinct_product_noun("README.md")
    assert not _is_distinct_product_noun("docker/Dockerfile")
    assert not _is_distinct_product_noun(".gitignore")


def test_overlaps_anchor_tokens() -> None:
    pool = frozenset({"billing", "auth", "checkout"})
    assert _overlaps_anchor_tokens("billing-webhook", pool)
    assert _overlaps_anchor_tokens("auth", pool)
    # ``api-core`` strips to empty token set → no overlap.
    assert not _overlaps_anchor_tokens("api-core", pool)
    # ``settings-page`` — neither token in pool.
    assert not _overlaps_anchor_tokens("settings-page", pool)


def test_is_cohesive_cluster_rules() -> None:
    # Same parent dir → cohesive.
    assert _is_cohesive_cluster((
        "apps/web/billing/checkout.ts",
        "apps/web/billing/invoice.ts",
        "apps/web/billing/subscription.ts",
    ))
    # Same top-2 segments, different parents → cohesive (workspace scope).
    assert _is_cohesive_cluster((
        "apps/web/billing/checkout.ts",
        "apps/web/auth/login.ts",
    ))
    # Different top-2 AND different parents → incoherent.
    assert not _is_cohesive_cluster((
        "apps/coordinator/Containerfile",
        "apps/docker-provider/Containerfile",
        "tooling/build/Containerfile",
    ))
    # A root-level file inside an otherwise multi-path cluster:
    # incoherent (root files share locality with nothing).
    assert not _is_cohesive_cluster((
        "README.md",
        "apps/web/page.tsx",
    ))


# ── Guard A — singleton admission (the 6 sprint cases, part 1) ─────


def test_root_product_config_singleton_admitted() -> None:
    """Case 1 — root-level Tauri/Expo/manifest singletons survive."""
    feat = _feat("tauri-conf", ("tauri.conf.json",))
    assert _is_admissible_singleton(feat, frozenset())


def test_generic_stem_singleton_dropped() -> None:
    """Case 2 — ``src/lib/utils.ts`` carries no product signal."""
    feat = _feat("utils-helper", ("src/lib/utils.ts",))
    # No anchor pool to overlap, not a root config, stem is generic.
    assert not _is_admissible_singleton(feat, frozenset())


def test_singleton_name_overlaps_anchor_admitted() -> None:
    """Case 3 — a singleton whose name token is in the anchor pool."""
    # Anchor pool simulates Stage 2 having emitted a ``billing`` feature.
    anchor_pool = frozenset({"billing"})
    # Path stem is ``page`` (generic) and not a root config — admission
    # comes solely from the name-overlap prong.
    feat = _feat("billing-page", ("apps/web/billing/page.tsx",))
    assert _is_admissible_singleton(feat, anchor_pool)


# ── Guard B — cluster cohesion (cases 4 + 5) ───────────────────────


def test_cohesive_cluster_admitted_unchanged() -> None:
    """Case 4 — three paths under one parent dir survive."""
    feat = _feat("billing", (
        "apps/web/billing/checkout.ts",
        "apps/web/billing/invoice.ts",
        "apps/web/billing/subscription.ts",
    ))
    result = apply_stage_4_guards([feat], existing_features=[])
    assert result.singletons_dropped == 0
    assert result.incoherent_clusters_split == 0
    assert len(result.kept) == 1
    assert result.kept[0].name == "billing"


def test_incoherent_cluster_dropped_by_default() -> None:
    """Case 5 (default) — mixed-parents cluster is dropped whole.

    The conservative default (split_incoherent=False) drops the
    cluster outright because spawning singletons admits net-new
    features the LLM never proposed individually, which inflates the
    feature count more than it removes phantoms.
    """
    feat = _feat("container-configuration", (
        "apps/coordinator/Containerfile",
        "apps/docker-provider/Containerfile",
        "apps/supervisor/Containerfile",
    ))
    result = apply_stage_4_guards([feat], existing_features=[])
    assert result.incoherent_clusters_split == 1
    assert result.singletons_dropped == 0  # cluster dropped, not split
    assert result.kept == []
    assert result.drops[0].reason == "incoherent_cluster_dropped"


def test_incoherent_cluster_split_admits_distinct_spawns_when_opted_in() -> None:
    """Opt-in split mode admits per-path singletons with distinct nouns."""
    feat = _feat("misc-handlers", (
        "apps/api/billing.ts",
        "apps/api/checkout.ts",
        "tooling/scripts/webhook.ts",
    ))
    result = apply_stage_4_guards(
        [feat], existing_features=[], split_incoherent=True,
    )
    assert result.incoherent_clusters_split == 1
    assert result.singletons_dropped == 0
    kept_paths = {f.paths[0] for f in result.kept}
    assert kept_paths == {
        "apps/api/billing.ts",
        "apps/api/checkout.ts",
        "tooling/scripts/webhook.ts",
    }


def test_incoherent_cluster_split_rejects_generic_spawns() -> None:
    """Opt-in split mode still rejects spawns with generic stems."""
    feat = _feat("container-configuration", (
        "apps/coordinator/Containerfile",
        "apps/docker-provider/Containerfile",
        "apps/supervisor/Containerfile",
    ))
    result = apply_stage_4_guards(
        [feat], existing_features=[], split_incoherent=True,
    )
    assert result.incoherent_clusters_split == 1
    # All three Containerfile spawns have the universal generic stem
    # ``dockerfile`` → Guard A rejects each one.
    assert result.singletons_dropped == 3
    assert result.kept == []


# ── Case 6 — telemetry counts match drop events ────────────────────


def test_telemetry_counts_and_drop_sample_align() -> None:
    """Case 6 — drop events match the singletons_dropped /
    incoherent_clusters_split counters."""
    features = [
        # Will drop: generic stem, no anchor overlap.
        _feat("prettier-config", ("prettier.config.js",)),
        # Will drop: README boilerplate.
        _feat("docs-readme", ("docs/README.md",)),
        # Will admit: root product-config.
        _feat("vercel-config", ("vercel.json",)),
        # Will split into three generic Containerfile spawns (all drop).
        _feat("container-configuration", (
            "apps/coordinator/Containerfile",
            "apps/docker-provider/Containerfile",
            "apps/supervisor/Containerfile",
        )),
    ]
    result = apply_stage_4_guards(features, existing_features=[])

    # Two top-level singleton drops; the incoherent cluster is dropped
    # whole (no spawned drops counted) under the default policy.
    assert result.singletons_dropped == 2
    assert result.incoherent_clusters_split == 1
    # ``vercel-config`` survives — root product-config admission.
    assert len(result.kept) == 1
    assert result.kept[0].name == "vercel-config"

    # Sample is capped at 5 entries. Each entry has a well-formed
    # ``name`` / ``reason`` / ``path`` triple.
    assert 1 <= len(result.drops) <= 5
    reasons = {d.reason for d in result.drops}
    assert reasons <= {
        "singleton_no_signal",
        "incoherent_cluster_dropped",
        "incoherent_cluster_split",
    }
    for d in result.drops:
        assert isinstance(d, DropEvent)
        assert d.name  # non-empty
        # path may be empty only for an all-empty singleton — none here.
        assert d.path


# ── Universal-scale invariants (per memory/rule-no-magic-tuning) ───


def test_empty_residual_returns_empty_result() -> None:
    result = apply_stage_4_guards([], existing_features=[])
    assert result.kept == []
    assert result.singletons_dropped == 0
    assert result.incoherent_clusters_split == 0
    assert result.drops == []


def test_singleton_with_no_paths_is_dropped_safely() -> None:
    # Defensive: shouldn't crash on a malformed singleton.
    feat = _feat("orphan", ())
    result = apply_stage_4_guards([feat], existing_features=[])
    assert result.kept == []
    assert result.singletons_dropped == 1


def test_anchor_pool_built_from_existing_features() -> None:
    """An anchor-overlap admission proves the pool wiring works."""
    # Stage 2 anchor pretends to be a real ``billing`` feature.
    anchor = DeveloperFeature(
        name="billing", paths=("apps/web/billing/page.tsx",),
        sources=["route"], confidence="medium",
    )
    # Stage 4 emits a singleton with a generic stem (``page``) — only
    # the anchor-overlap prong can admit it.
    residual = _feat("billing-page", ("apps/web/billing/admin.tsx",))
    result = apply_stage_4_guards([residual], existing_features=[anchor])
    assert result.singletons_dropped == 0
    assert len(result.kept) == 1


def test_drops_sample_cap_enforced() -> None:
    """The drops sample is bounded so it can be embedded in scan_meta."""
    # 10 generic-stem singletons → 10 drops, but only 5 sampled.
    many = [
        _feat(f"helper-{i}", (f"src/lib/helper-{i}.ts",))
        for i in range(10)
    ]
    result = apply_stage_4_guards(many, existing_features=[])
    assert result.singletons_dropped == 10
    assert len(result.drops) == 5


def test_guard_result_dataclass_shape() -> None:
    """Public type is a :class:`GuardResult` with the documented fields."""
    result = apply_stage_4_guards([], existing_features=[])
    assert isinstance(result, GuardResult)
    assert hasattr(result, "kept")
    assert hasattr(result, "drops")
    assert hasattr(result, "singletons_dropped")
    assert hasattr(result, "incoherent_clusters_split")
