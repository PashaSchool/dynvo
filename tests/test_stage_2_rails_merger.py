"""Stage 2 H2 — cross-extractor Rails singular/plural merger.

When the Stage 0.5 auditor declares ``rails-app``, Stage 2 must collapse
anchors whose Rails canonical noun is identical (Address model +
addresses controller + addresses views all map to one feature).
"""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.stage_2_reconcile import stage_2_reconcile


def _ctx(
    *,
    audited_stack: str | None,
    tracked_files: list[str] | None = None,
    repo_path: Path = Path("/tmp/x"),
) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack="ruby" if audited_stack == "rails-app" else "js",
        monorepo=False,
        workspaces=None,
        tracked_files=tracked_files or [],
        commits=[],
        stack_signals=[],
        workspace_manager=None,
        audited_stack=audited_stack,
        secondary_stacks=(),
        extractor_hints=(),
        auditor_confidence=0.9,
    )


def test_rails_collapses_singular_plural_into_one_feature() -> None:
    """The canonical Rails resource trio merges into ONE feature."""
    candidates = {
        "rails-models": [
            AnchorCandidate(
                name="address",
                paths=("app/models/address.rb",),
                source="rails-models",
                confidence_self=0.85,
            ),
        ],
        "rails-routes": [
            AnchorCandidate(
                name="addresses",
                paths=("config/routes.rb",),
                source="rails-routes",
                confidence_self=0.95,
            ),
        ],
        "rails-views": [
            AnchorCandidate(
                name="addresses",
                paths=(
                    "app/views/addresses/index.html.erb",
                    "app/views/addresses/show.html.erb",
                ),
                source="rails-views",
                confidence_self=0.80,
            ),
        ],
        "mvc": [
            AnchorCandidate(
                name="addresses",
                paths=("app/controllers/addresses_controller.rb",),
                source="mvc",
                confidence_self=0.85,
            ),
        ],
    }
    ctx = _ctx(
        audited_stack="rails-app",
        tracked_files=[
            "app/models/address.rb",
            "config/routes.rb",
            "app/views/addresses/index.html.erb",
            "app/views/addresses/show.html.erb",
            "app/controllers/addresses_controller.rb",
        ],
    )
    result = stage_2_reconcile(candidates, ctx)
    # All four sources must collapse into ONE feature.
    assert len(result.features) == 1
    feature = result.features[0]
    # Canonical name is the singular form.
    assert feature.name == "address"
    # All four files attributed.
    assert "app/models/address.rb" in feature.paths
    assert "config/routes.rb" in feature.paths
    assert "app/views/addresses/index.html.erb" in feature.paths
    assert "app/controllers/addresses_controller.rb" in feature.paths


def test_non_rails_repo_does_not_collapse_singular_plural() -> None:
    """Without the rails-app audited tag, the merger is OFF.

    `address` and `addresses` have zero token overlap; Jaccard = 0;
    they remain separate.
    """
    candidates = {
        "schema": [
            AnchorCandidate(
                name="address",
                paths=("schema.sql",),
                source="schema",
                confidence_self=0.7,
            ),
        ],
        "route": [
            AnchorCandidate(
                name="addresses",
                paths=("app/api/addresses/route.ts",),
                source="route",
                confidence_self=0.9,
            ),
        ],
    }
    ctx = _ctx(
        audited_stack="next-app-router",
        tracked_files=["schema.sql", "app/api/addresses/route.ts"],
    )
    result = stage_2_reconcile(candidates, ctx)
    names = {f.name for f in result.features}
    assert "address" in names
    assert "addresses" in names
    assert len(result.features) == 2


def test_rails_strips_controller_suffix_for_canonical_match() -> None:
    """`users-controller` (from MVC extractor) merges with `user` model."""
    candidates = {
        "rails-models": [
            AnchorCandidate(
                name="user",
                paths=("app/models/user.rb",),
                source="rails-models",
                confidence_self=0.85,
            ),
        ],
        "mvc": [
            AnchorCandidate(
                name="users-controller",
                paths=("app/controllers/users_controller.rb",),
                source="mvc",
                confidence_self=0.85,
            ),
        ],
    }
    ctx = _ctx(
        audited_stack="rails-app",
        tracked_files=[
            "app/models/user.rb",
            "app/controllers/users_controller.rb",
        ],
    )
    result = stage_2_reconcile(candidates, ctx)
    assert len(result.features) == 1
    assert result.features[0].name == "user"
