"""Guard the deterministic path against nondeterminism creeping in.

Builds each fixture TWICE in independent temp dirs (so the git repos are
materialized from scratch each time) and asserts the normalized scan
output is byte-identical. If a stage starts depending on dict iteration
order, the wall clock, an absolute path, or a hash seed, this trips.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.eval._make_fixtures import materialize_fixture
from tests.eval._runner import normalize, run_deterministic_scan


def test_scan_is_deterministic(fixture_name: str, tmp_path: Path) -> None:
    norms = []
    for i in range(2):
        base = tmp_path / f"run-{i}"
        repo = materialize_fixture(fixture_name, base / fixture_name)
        feature_map = run_deterministic_scan(repo, home=base / "home")
        norms.append(normalize(feature_map))

    assert norms[0] == norms[1], (
        f"non-deterministic detection for {fixture_name!r}:\n"
        f"run 0: {norms[0]}\nrun 1: {norms[1]}"
    )
