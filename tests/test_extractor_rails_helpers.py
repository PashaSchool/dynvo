"""Unit tests for the shared Rails helper module (_rails.py).

Covers:
  - is_rails_app activation gate
  - singularize / pluralize idempotency for common Rails forms
  - rails_canonical_noun handling of plural / controller / job suffix
"""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors._rails import (
    is_rails_app,
    pluralize,
    rails_canonical_noun,
    singularize,
)


def _ctx(
    *,
    audited_stack: str | None = None,
    secondary_stacks: tuple[str, ...] = (),
    repo_path: Path = Path("/tmp/x"),
) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=[],
        commits=[],
        stack_signals=[],
        workspace_manager=None,
        audited_stack=audited_stack,
        secondary_stacks=secondary_stacks,
        extractor_hints=(),
        auditor_confidence=0.9,
    )


# ── Activation gate ───────────────────────────────────────────────────────


def test_is_rails_app_positive_primary() -> None:
    assert is_rails_app(_ctx(audited_stack="rails-app")) is True


def test_is_rails_app_positive_secondary() -> None:
    ctx = _ctx(audited_stack="ruby", secondary_stacks=("rails-app",))
    assert is_rails_app(ctx) is True


def test_is_rails_app_negative_unrelated() -> None:
    assert is_rails_app(_ctx(audited_stack="next-app-router")) is False


def test_is_rails_app_negative_no_audit() -> None:
    assert is_rails_app(_ctx(audited_stack=None)) is False


# ── singularize ───────────────────────────────────────────────────────────


def test_singularize_simple_s() -> None:
    assert singularize("users") == "user"
    assert singularize("posts") == "post"


def test_singularize_ies_to_y() -> None:
    assert singularize("categories") == "category"
    assert singularize("companies") == "company"


def test_singularize_sibilant_es() -> None:
    assert singularize("classes") == "class"
    assert singularize("boxes") == "box"
    assert singularize("addresses") == "address"


def test_singularize_irregular() -> None:
    assert singularize("people") == "person"
    assert singularize("children") == "child"


def test_singularize_idempotent_for_singular() -> None:
    # If passed an already-singular word, leave it alone (or near-it).
    assert singularize("user") == "user"
    assert singularize("address") == "address"


def test_singularize_uncountable() -> None:
    assert singularize("information") == "information"
    assert singularize("metadata") == "metadata"


def test_singularize_empty() -> None:
    assert singularize("") == ""


# ── pluralize ─────────────────────────────────────────────────────────────


def test_pluralize_simple() -> None:
    assert pluralize("user") == "users"
    assert pluralize("box") == "boxes"
    assert pluralize("category") == "categories"


def test_pluralize_irregular() -> None:
    assert pluralize("person") == "people"
    assert pluralize("child") == "children"


# ── rails_canonical_noun ──────────────────────────────────────────────────


def test_canonical_noun_singular_plural_collapse() -> None:
    assert (
        rails_canonical_noun("address")
        == rails_canonical_noun("addresses")
        == "address"
    )


def test_canonical_noun_strips_controller_suffix() -> None:
    # MVC controller anchor: stem may be "users-controller" if extractor
    # didn't pre-strip. The canonical form drops the suffix and
    # singularizes.
    assert rails_canonical_noun("users-controller") == "user"
    assert rails_canonical_noun("addresses-controller") == "address"


def test_canonical_noun_strips_job_suffix() -> None:
    assert rails_canonical_noun("welcome-job") == "welcome"
    assert rails_canonical_noun("welcomes-job") == "welcome"


def test_canonical_noun_multiword_resource() -> None:
    # Compound resource: "project-memberships" ↔ "project-membership"
    assert (
        rails_canonical_noun("project-memberships")
        == rails_canonical_noun("project-membership")
        == "project-membership"
    )


def test_canonical_noun_empty() -> None:
    assert rails_canonical_noun("") == ""
