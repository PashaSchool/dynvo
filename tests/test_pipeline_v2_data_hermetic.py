"""Hermeticity + drift-guard tests for the in-package runtime data loader.

These cover acceptance criterion (a): each extractor's loader returns its
parsed YAML via importlib.resources with NO dependence on a repo-root
``eval/`` sibling, and the in-package copies do not drift from the
human-authoring copies under repo-root ``eval/``.

The full-wheel hermeticity proof (criterion (b)) lives in
``test_wheel_hermetic.py`` (build + fresh-venv install), kept separate so
it can be skipped in fast unit runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.data import (
    load_data_text,
    load_stack_yaml,
    load_yaml,
)

# Repo root = three levels up from this test file's package data dir.
# tests/ is a sibling of faultline/ and eval/ at the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_EVAL_STACKS = _REPO_ROOT / "eval" / "stacks"
_EVAL_DEP_ANCHORS = _REPO_ROOT / "eval" / "dependency-anchors.yaml"

_STACK_NAMES = [
    "fastapi",
    "go-http-router",
    "js-library",
    "python-library",
    "rails-app",
    "rust-workspace",
]


@pytest.mark.parametrize("stack", _STACK_NAMES)
def test_stack_yaml_loads_as_mapping(stack: str) -> None:
    """Each packaged stack YAML parses to a non-empty dict via resources."""
    data = load_stack_yaml(stack)
    assert isinstance(data, dict)
    assert data, f"{stack}.yaml parsed empty — packaging or data bug"


def test_dependency_anchors_loads() -> None:
    data = load_yaml("dependency-anchors.yaml")
    assert isinstance(data, dict)
    assert data, "dependency-anchors.yaml parsed empty"


def test_missing_resource_is_hard_error() -> None:
    """A missing data file must raise, never silently return {}."""
    with pytest.raises(FileNotFoundError):
        load_data_text("stacks/does-not-exist.yaml")


@pytest.mark.parametrize("stack", _STACK_NAMES)
def test_no_eval_sibling_dependence(stack: str, monkeypatch, tmp_path) -> None:
    """Loader works with cwd moved away from the repo (no eval/ on path).

    Simulates the installed-wheel situation where there is no repo-root
    ``eval/`` sibling. importlib.resources resolves against the installed
    package, so changing the working directory must not affect the result.
    """
    monkeypatch.chdir(tmp_path)
    # Bust the lru_cache so the read genuinely re-resolves from this cwd.
    load_data_text.cache_clear()
    load_yaml.cache_clear()
    data = load_stack_yaml(stack)
    assert isinstance(data, dict) and data


# ── Drift guard: in-package data must be byte-identical to eval/ authoring ──


@pytest.mark.parametrize("stack", _STACK_NAMES)
def test_stack_yaml_matches_eval_authoring_copy(stack: str) -> None:
    """The packaged stack YAML is byte-identical to repo-root eval/stacks/.

    Authors edit ``eval/stacks/<stack>.yaml``; the in-package copy at
    ``faultline/pipeline_v2/data/stacks/`` is the RUNTIME source of truth.
    This test fails if someone edits one without syncing the other.
    """
    authoring = (_EVAL_STACKS / f"{stack}.yaml").read_text(encoding="utf-8")
    # Read via the same loader path the runtime uses.
    load_data_text.cache_clear()
    packaged = load_data_text(f"stacks/{stack}.yaml")
    assert packaged == authoring, (
        f"DRIFT: faultline/pipeline_v2/data/stacks/{stack}.yaml differs "
        f"from eval/stacks/{stack}.yaml. Re-sync the in-package copy."
    )


def test_dependency_anchors_matches_eval_authoring_copy() -> None:
    authoring = _EVAL_DEP_ANCHORS.read_text(encoding="utf-8")
    load_data_text.cache_clear()
    packaged = load_data_text("dependency-anchors.yaml")
    assert packaged == authoring, (
        "DRIFT: faultline/pipeline_v2/data/dependency-anchors.yaml differs "
        "from eval/dependency-anchors.yaml. Re-sync the in-package copy."
    )
