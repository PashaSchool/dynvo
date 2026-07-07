"""W4 rider — python-library gate: nested app markers always disqualify.

The old probe read whichever nested main.py a SET iteration yielded
first and broke — hash-seed-dependent extractor output (tracecat W4
smoke: 40 vs 0 candidates across identical runs). The gate must be
deterministic AND correct: an ``app = FastAPI()`` anywhere disqualifies.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from faultline.pipeline_v2.extractors.python_library import (
    _is_python_library,
)


def _ctx(tmp_path: Path, tracked: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        repo_path=tmp_path, tracked_files=tracked, stack="fastapi",
        audited_stack=None, secondary_stacks=(),
    )


def test_nested_app_marker_disqualifies_regardless_of_benign_twins(
    tmp_path: Path,
) -> None:
    # TWO nested main.py: a benign CLI one and a real FastAPI app.
    (tmp_path / "pkg" / "cli").mkdir(parents=True)
    (tmp_path / "pkg" / "api").mkdir(parents=True)
    (tmp_path / "pkg" / "cli" / "main.py").write_text("print('hi')\n")
    (tmp_path / "pkg" / "api" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n")
    tracked = ["pkg/cli/main.py", "pkg/api/main.py", "pkg/__init__.py"]
    # Both orderings must yield the SAME verdict: NOT a library.
    assert _is_python_library(_ctx(tmp_path, tracked)) is False
    assert _is_python_library(_ctx(tmp_path, list(reversed(tracked)))) is False


def test_library_without_app_markers_stays_library(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    tracked = ["pkg/__init__.py", "pkg/core.py"]
    assert _is_python_library(_ctx(tmp_path, tracked)) is True
