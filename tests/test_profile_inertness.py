"""G4 — inertness proofs for the shipped profiles (StackProfile Phase A).

Uses the reusable template in :mod:`tests._profile_inertness`. Every
future profile (Phase B: fastapi_family, django, next_pages_react)
adds one test here against a fixture repo it must NOT match.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.profiles import AttributionSpec, FileRole
from faultline.pipeline_v2.profiles.django import DjangoProfile
from faultline.pipeline_v2.profiles.fastapi_family import FastApiFamilyProfile
from faultline.pipeline_v2.profiles.next_app_router import NextAppRouterProfile
from faultline.pipeline_v2.profiles.next_pages_react import (
    NextPagesReactProfile,
)
from tests._profile_inertness import (
    NON_NEXT_FIXTURE,
    assert_profile_inert,
    make_fixture_repo,
)


def test_next_app_router_inert_on_non_next_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registering NextAppRouterProfile must not touch a non-Next scan."""
    repo = make_fixture_repo(tmp_path, NON_NEXT_FIXTURE)
    assert_profile_inert(repo, NextAppRouterProfile(), tmp_path, monkeypatch)


def test_fastapi_family_inert_on_non_fastapi_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4 (Phase B #1): FastApiFamilyProfile must not touch a scan it
    does not win — the flask fixture has no family dep, no family
    source fingerprints, so ``detects`` is 0.0 and registration must be
    byte-inert (including the Stage-1 extractor-override seam)."""
    repo = make_fixture_repo(tmp_path, NON_NEXT_FIXTURE)
    assert_profile_inert(repo, FastApiFamilyProfile(), tmp_path, monkeypatch)


def test_django_inert_on_non_django_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4 (Phase B #2): DjangoProfile must not touch a scan it does not
    win — the flask fixture has no Django dep and no project grammar
    (no manage.py / INSTALLED_APPS / urlpatterns), so ``detects`` is
    0.0 and registration must be byte-inert (including the Stage-1
    extractor-override seam)."""
    repo = make_fixture_repo(tmp_path, NON_NEXT_FIXTURE)
    assert_profile_inert(repo, DjangoProfile(), tmp_path, monkeypatch)


def test_next_pages_react_inert_on_non_react_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4 (Phase B #3): NextPagesReactProfile must not touch a scan it
    does not win — the flask fixture has no react/next dep, no pages
    tree, no router grammar, no render entry, so ``detects`` is 0.0 and
    registration must be byte-inert (including the Stage-1
    extractor-override seam)."""
    repo = make_fixture_repo(tmp_path, NON_NEXT_FIXTURE)
    assert_profile_inert(repo, NextPagesReactProfile(), tmp_path, monkeypatch)


class _NeverMatchingProfile:
    """Null-case candidate: scores 0.0 everywhere (like a foreign stack)."""

    name = "never-matching"

    def detects(self, ctx) -> float:  # noqa: ANN001
        return 0.0

    def workspaces(self, ctx):  # noqa: ANN001, ANN201
        return []

    def classify_file(self, path: str) -> FileRole:
        return FileRole.UNKNOWN

    def feature_of(self, path: str, ctx):  # noqa: ANN001, ANN201
        return None

    def flow_entries(self, ctx):  # noqa: ANN001, ANN201
        return []

    def attribution_rules(self) -> AttributionSpec:
        return AttributionSpec()


def test_default_profile_selection_machinery_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DefaultProfile wiring: an extra zero-scoring profile in the
    registry changes nothing about a default-profile scan — the
    selection machinery itself is inert, not just one concrete profile.
    """
    repo = make_fixture_repo(tmp_path, NON_NEXT_FIXTURE)
    assert_profile_inert(repo, _NeverMatchingProfile(), tmp_path, monkeypatch)
