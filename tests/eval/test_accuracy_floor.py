"""Accuracy-floor gate: detection precision/recall must not regress.

For each fixture that ships a hand-written truth file under
``tests/eval/truth/<name>.yaml`` we score the deterministic detection
with the SAME scorer the production benchmark uses
(``faultline.benchmark.metrics``), which does stemming / token-set /
alias matching. We then assert precision + recall stay above pinned
floors.

The truth is derived from what each fixture's CODE implements (a
thoughtful product owner's naming), NOT from current engine output — so
these floors measure real detection quality, and a drop fails the gate.

Floors are set with a small margin below the current values so a genuine
regression fails while normal noise does not. Recall floors are 1.0 on
purpose: every fixture is small enough that the engine SHOULD find every
real feature, and a recall drop is exactly the regression we care about
most. Precision floors sit just under the current value to leave room for
the known infrastructure phantoms (``db``, ``next-config``, ``server``)
without masking new ones.

To intentionally move a floor, edit ``FLOORS`` below — a deliberate,
reviewable one-liner.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from faultline.benchmark.loader import load_expected_features
from faultline.benchmark.metrics import feature_precision, feature_recall
from tests.eval._runner import detected_feature_names
from tests.eval.conftest import TRUTH_DIR

# fixture name → (min_recall, min_precision)
FLOORS: dict[str, tuple[float, float]] = {
    "nextjs-shop": (1.0, 0.60),
    "fastapi-svc": (1.0, 0.90),
    "fastify-api": (1.0, 0.90),
}


def _truth_path(name: str):
    return TRUTH_DIR / f"{name}.yaml"


def test_accuracy_floor(
    fixture_name: str,
    scan_fixture: Callable[[str], tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    tpath = _truth_path(fixture_name)
    if not tpath.exists():
        pytest.skip(f"no truth file for {fixture_name}")
    if fixture_name not in FLOORS:
        pytest.skip(f"no pinned floor for {fixture_name}")

    _raw, normalized = scan_fixture(fixture_name)
    detected = detected_feature_names(normalized)
    expected = load_expected_features(tpath)

    recall = feature_recall(expected, detected)
    precision = feature_precision(expected, detected)
    min_recall, min_precision = FLOORS[fixture_name]

    assert recall >= min_recall, (
        f"{fixture_name}: recall {recall:.3f} < floor {min_recall:.3f}. "
        f"detected={detected}"
    )
    assert precision >= min_precision, (
        f"{fixture_name}: precision {precision:.3f} < floor "
        f"{min_precision:.3f}. detected={detected}"
    )
