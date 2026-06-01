"""Golden-snapshot regression gate for deterministic detection.

For every fixture we run the LLM-free pipeline, normalize the output to a
stable subset (stack + feature names + sorted paths + flow names), and
assert it equals the committed ``golden/<name>.json``.

Regenerating goldens
--------------------
An INTENTIONAL detection change is a deliberate one-liner::

    UPDATE_GOLDEN=1 python -m pytest tests/eval/test_golden_snapshots.py

That rewrites the goldens in place; review the diff before committing.
Without the env var, a drift is a hard failure (the whole point of the
gate).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from tests.eval.conftest import GOLDEN_DIR

UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


def _golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def _write_golden(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_golden_snapshot(
    fixture_name: str,
    scan_fixture: Callable[[str], tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    _raw, normalized = scan_fixture(fixture_name)
    gpath = _golden_path(fixture_name)

    if UPDATE:
        _write_golden(gpath, normalized)
        return

    assert gpath.exists(), (
        f"golden missing for {fixture_name!r}: {gpath}. "
        f"Run `UPDATE_GOLDEN=1 pytest tests/eval/` to create it."
    )
    expected = json.loads(gpath.read_text(encoding="utf-8"))
    assert normalized == expected, (
        f"detection drift for {fixture_name!r}.\n"
        f"expected: {json.dumps(expected, indent=2, sort_keys=True)}\n"
        f"actual:   {json.dumps(normalized, indent=2, sort_keys=True)}\n"
        f"If this change is intentional, run "
        f"`UPDATE_GOLDEN=1 pytest tests/eval/` and commit the new golden."
    )
