"""Sub-decomposition tests for the Next App Router profile (blob fix).

The workspace-anchor blob: a single-package Next app becomes ONE
``[package]`` workspace-anchor feature owning every file, because the
route / page extractors only surface URL segments — never the route
GROUPS (``app/(dashboard)``) or MODULE folders (``modules/billing``)
that are the author's real capability boundaries. This suite proves the
profile now SYNTHESISES those boundaries and re-homes their files off the
anchor — universally, with no corpus paths and no tuned magic numbers.

Synthetic fixtures only (neutral names), per ``rule-no-repo-specific-paths``.
Three scales per threshold-driven rule, per ``rule-no-magic-tuning``.
"""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.profiles._attribution import (
    apply_profile_attribution,
    synth_features,
)
from faultline.pipeline_v2.profiles.default import DefaultProfile
from faultline.pipeline_v2.profiles.next_app_router import (
    NextAppRouterProfile,
    SynthFeature,
    _owning_boundary,
)
from faultline.pipeline_v2.stage_2_reconcile import (
    DeveloperFeature,
    stage_2_reconcile,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _ctx(files: list[str], *, repo: Path | None = None) -> ScanContext:
    return ScanContext(
        repo_path=repo or Path("/tmp/fake"),
        stack="next-app-router",
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


def _make_feature(name: str, paths: tuple[str, ...]) -> DeveloperFeature:
    return DeveloperFeature(
        name=name, paths=paths, sources=["route"], confidence="medium",
    )


# ── boundary resolution ──────────────────────────────────────────────────────


def test_route_leaf_is_the_owner() -> None:
    # The deepest meaningful URL segment owns the file (leaf-conservative).
    b = _owning_boundary("apps/web/app/(dashboard)/settings/page.tsx")
    assert b is not None
    assert b.slug == "settings"
    assert b.kind == "route-segment"
    assert b.prefix == "apps/web/app/(dashboard)/settings"


def test_distinct_siblings_under_group_stay_distinct() -> None:
    # (shop)/cart and (shop)/products are DISTINCT capabilities — they must
    # NOT melt into one "shop" area blob.
    cart = _owning_boundary("app/(shop)/cart/page.tsx")
    products = _owning_boundary("app/(shop)/products/page.tsx")
    assert cart is not None and products is not None
    assert cart.slug == "cart"
    assert products.slug == "products"
    assert cart.prefix != products.prefix


def test_group_owns_only_group_level_files() -> None:
    # A group-level layout (no child segment) is owned by the group name.
    b = _owning_boundary("app/(marketing)/layout.tsx")
    assert b is not None
    assert b.slug == "marketing"
    assert b.kind == "route-group"


def test_noise_group_falls_through_to_segment() -> None:
    # A group whose inner name is a noise token (``(app)``) is NOT a
    # capability; the real route segment under it owns the file instead.
    a = _owning_boundary("app/(app)/billing/page.tsx")
    assert a is not None
    assert a.slug == "billing"
    assert a.kind == "route-segment"


def test_module_folder_is_the_owner() -> None:
    b = _owning_boundary("apps/web/modules/billing/components/Plan.tsx")
    assert b is not None
    assert b.slug == "billing"
    assert b.kind == "module"
    assert b.prefix == "apps/web/modules/billing"


def test_features_container_is_a_boundary() -> None:
    b = _owning_boundary("src/features/onboarding/lib/steps.ts")
    assert b is not None
    assert b.slug == "onboarding"
    assert b.kind == "module"


def test_top_level_route_segment_boundary() -> None:
    # A named route segment owns its page.
    b = _owning_boundary("app/integrations/page.tsx")
    assert b is not None
    assert b.slug == "integrations"
    assert b.kind == "route-segment"


def test_shared_files_have_no_boundary() -> None:
    # Top-level shared primitives must NOT be glued to a feature.
    assert _owning_boundary("components/Button.tsx") is None
    assert _owning_boundary("lib/utils.ts") is None
    assert _owning_boundary("hooks/use-toast.ts") is None
    # Root app shell (no segment) has no owner.
    assert _owning_boundary("app/layout.tsx") is None
    assert _owning_boundary("app/page.tsx") is None


def test_intercepting_marker_is_not_a_segment_owner() -> None:
    # (.)photo is an overlay route, not a capability; the real segment
    # (feed) owns the file instead.
    b = _owning_boundary("app/feed/(.)photo/page.tsx")
    assert b is not None
    assert b.slug == "feed"


# ── synthesis ────────────────────────────────────────────────────────────────


def test_synthesizes_leaf_and_module_features() -> None:
    files = [
        # (dashboard) group with two distinct multi-file leaves.
        "apps/web/app/(dashboard)/settings/page.tsx",
        "apps/web/app/(dashboard)/settings/form.tsx",
        "apps/web/app/(dashboard)/teams/page.tsx",
        "apps/web/app/(dashboard)/teams/list.tsx",
        # (marketing) group-level files (no child segment) → 'marketing'.
        "apps/web/app/(marketing)/page.tsx",
        "apps/web/app/(marketing)/layout.tsx",
        # module domain.
        "apps/web/modules/billing/service.ts",
        "apps/web/modules/billing/components/Plan.tsx",
        # shared → no synth.
        "apps/web/lib/db.ts",
        "apps/web/components/Button.tsx",
    ]
    profile = NextAppRouterProfile()
    feats = profile.synthesize_features(_ctx(files))
    names = {f.name for f in feats}
    # Distinct sibling leaves stay distinct; group-level files own the
    # group name; the module is its own feature.
    assert names == {"settings", "teams", "marketing", "billing"}
    billing = next(f for f in feats if f.name == "billing")
    assert "apps/web/modules/billing/service.ts" in billing.paths


def test_synthesis_floor_skips_singletons() -> None:
    # A capability folder with a single source file is below the floor.
    files = ["app/(solo)/page.tsx"]  # one file under the group
    feats = NextAppRouterProfile().synthesize_features(_ctx(files))
    assert feats == []
    # Two files → it clears the floor.
    files2 = ["app/(solo)/page.tsx", "app/(solo)/layout.tsx"]
    feats2 = NextAppRouterProfile().synthesize_features(_ctx(files2))
    assert {f.name for f in feats2} == {"solo"}


def test_synthesis_ignores_non_source_assets() -> None:
    files = [
        "app/(docs)/page.tsx",
        "app/(docs)/readme.md",
        "app/(docs)/data.json",
    ]
    # Only one SOURCE file under the group → below the floor.
    feats = NextAppRouterProfile().synthesize_features(_ctx(files))
    assert feats == []


def test_synthesis_scales_large_monorepo() -> None:
    # 50 module folders, each with 5 files → 50 synth features, no blob.
    files: list[str] = []
    for i in range(50):
        for j in range(5):
            files.append(f"apps/web/modules/domain{i}/file{j}.ts")
    feats = NextAppRouterProfile().synthesize_features(_ctx(files))
    assert len(feats) == 50
    assert all(len(f.paths) == 5 for f in feats)


# ── end-to-end re-home (kills the blob) ──────────────────────────────────────


def test_stage_2_subdecomposes_workspace_anchor() -> None:
    """The package anchor must NOT keep the route-leaf/module files."""
    files = [
        "apps/web/app/(dashboard)/settings/page.tsx",
        "apps/web/app/(dashboard)/settings/form.tsx",
        "apps/web/modules/billing/service.ts",
        "apps/web/modules/billing/plan.ts",
        "apps/web/lib/db.ts",
    ]
    # The workspace anchor 'web' owns EVERY file (the blob); no route
    # anchor surfaced these capability folders.
    cands = {
        "package": [
            AnchorCandidate(
                name="web", paths=list(files),
                source="package", confidence_self=0.95,
            ),
        ],
    }
    profile = NextAppRouterProfile()
    result = stage_2_reconcile(cands, _ctx(files), profile=profile)
    by_name = {f.name: f for f in result.features}

    # Sub-features were synthesised + own their files.
    assert "settings" in by_name
    assert "billing" in by_name
    assert "apps/web/app/(dashboard)/settings/page.tsx" in by_name["settings"].paths
    assert "apps/web/modules/billing/service.ts" in by_name["billing"].paths

    # The 'web' anchor no longer owns the re-homed files (blob killed).
    web_paths = set(by_name.get("web", _make_feature("web", ())).paths)
    assert "apps/web/app/(dashboard)/settings/page.tsx" not in web_paths
    assert "apps/web/modules/billing/service.ts" not in web_paths
    # Shared lib stays with the anchor (NOT a capability boundary).
    assert "apps/web/lib/db.ts" in web_paths


def test_colocated_component_inside_module_rehomes() -> None:
    """A component INSIDE a module folder is OWNED by it, not glued to anchor.

    Regression for the formbricks bug: ``modules/survey/components/x.tsx``
    classifies COMPONENT, but it is colocated inside the ``survey``
    capability and MUST re-home off the ``web`` anchor — colocation beats
    the generic shared-primitive fan-out rule. Only repo-level shared
    ``components/`` (no owning boundary) fans out.
    """
    files = [
        "apps/web/modules/survey/components/form-input.tsx",
        "apps/web/modules/survey/components/recall-select.tsx",
        "apps/web/modules/survey/service.ts",
        "apps/web/components/Button.tsx",  # repo-level shared → stays
    ]
    profile = NextAppRouterProfile()
    # The component IS classified COMPONENT (shared role)...
    from faultline.pipeline_v2.profiles.base import FileRole
    assert profile.classify_file(files[0]) == FileRole.COMPONENT
    # ...but feature_of still CLAIMS it for the module (ownership).
    assert profile.feature_of(files[0], _ctx(files)) == "survey"

    cands = {
        "package": [
            AnchorCandidate(
                name="web", paths=list(files),
                source="package", confidence_self=0.95,
            ),
        ],
    }
    result = stage_2_reconcile(cands, _ctx(files), profile=profile)
    by_name = {f.name: f for f in result.features}
    survey_paths = set(by_name["survey"].paths)
    assert "apps/web/modules/survey/components/form-input.tsx" in survey_paths
    assert "apps/web/modules/survey/components/recall-select.tsx" in survey_paths
    # The web anchor must NOT keep the survey component (blob killed).
    web_paths = set(by_name.get("web", _make_feature("web", ())).paths)
    assert "apps/web/modules/survey/components/form-input.tsx" not in web_paths
    # Repo-level shared component (no boundary) stays with the anchor.
    assert "apps/web/components/Button.tsx" in web_paths


def test_synth_wiring_noop_for_default_profile() -> None:
    # The wiring's synthesis hook is a strict no-op under DefaultProfile.
    assert synth_features(DefaultProfile(), _ctx(["a.ts"])) == []
    assert synth_features(None, _ctx(["a.ts"])) == []


def test_apply_attribution_without_make_feature_is_unchanged() -> None:
    # Older callers (no make_feature) keep exact behaviour: no synthesis.
    files = ["app/(area)/page.tsx", "app/(area)/layout.tsx"]
    feats = [_make_feature("web", tuple(files))]
    profile = NextAppRouterProfile()
    out = apply_profile_attribution(
        feats, profile, _ctx(files), rebuild=lambda f, p: DeveloperFeature(
            name=f.name, paths=p, sources=f.sources, confidence=f.confidence,
        ),
    )
    # No make_feature → 'area' was never created → nothing re-homed.
    assert {f.name for f in out} == {"web"}
    assert set(out[0].paths) == set(files)


def test_synth_feature_is_public_shape() -> None:
    # The synthesis contract type is importable + structurally typed.
    sf = SynthFeature(name="x", paths=("a.ts",), prefix="app/(x)")
    assert sf.name == "x"
    assert sf.paths == ("a.ts",)
