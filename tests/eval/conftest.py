"""Shared fixtures for the deterministic detection-regression eval.

Exposes:
  * ``fixture_names`` — session list of every fixture with a history plan.
  * ``materialized`` — a factory that builds a fixture's git repo into the
    test's ``tmp_path`` and runs the deterministic, LLM-free scan, returning
    the normalized output. Results are cached per (fixture, session) so the
    several tests that share a fixture don't each re-run the pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from tests.eval._make_fixtures import available_fixtures, materialize_fixture
from tests.eval._runner import normalize, run_deterministic_scan

EVAL_DIR = Path(__file__).resolve().parent
GOLDEN_DIR = EVAL_DIR / "golden"
TRUTH_DIR = EVAL_DIR / "truth"


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize any test that asks for a ``fixture_name`` argument."""
    if "fixture_name" in metafunc.fixturenames:
        names = available_fixtures()
        metafunc.parametrize("fixture_name", names, ids=names)


# Session cache: fixture name → (raw feature_map, normalized view).
_SCAN_CACHE: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}


@pytest.fixture(scope="session")
def scan_fixture(
    tmp_path_factory: pytest.TempPathFactory,
) -> Callable[[str], tuple[dict[str, Any], dict[str, Any]]]:
    """Return a function that builds + scans a fixture (cached)."""

    def _run(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if name in _SCAN_CACHE:
            return _SCAN_CACHE[name]
        base = tmp_path_factory.mktemp(f"eval-{name}")
        repo = materialize_fixture(name, base / name)
        home = base / "home"
        feature_map = run_deterministic_scan(repo, home=home)
        normalized = normalize(feature_map)
        _SCAN_CACHE[name] = (feature_map, normalized)
        return _SCAN_CACHE[name]

    return _run
