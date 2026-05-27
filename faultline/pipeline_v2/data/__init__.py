"""Hermetic runtime data loader for pipeline_v2.

The Stage-1 extractors and the Stage-6.5 product clusterer need a small
set of YAML data files (per-stack pattern definitions + the
dependency-anchor table). Historically these were resolved from a
repo-root ``eval/`` sibling via ``Path(__file__).parents[N] / "eval"``.
That walk breaks the moment the package is installed as a wheel — there
is no ``site-packages/eval/`` sibling — and the extractors silently
no-op (see ``bug-featuremap-eval-not-in-wheel``).

This module makes the in-package ``faultline/pipeline_v2/data/`` tree the
RUNTIME source of truth and loads it via ``importlib.resources`` so the
behavior is identical from the dev repo, an installed wheel, or the Fly
worker image — no dependency on filesystem layout.

Authoring note (drift guard): the canonical author-facing copies still
live at repo-root ``eval/stacks/*.yaml`` and ``eval/dependency-anchors.yaml``
for human editing. A test (``tests/test_pipeline_v2_data_hermetic.py``)
asserts the in-package copies are byte-identical, so editing one without
the other fails CI.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

import yaml

__all__ = ["load_data_text", "load_stack_yaml", "load_yaml"]

_PACKAGE = "faultline.pipeline_v2.data"


@lru_cache(maxsize=None)
def load_data_text(filename: str) -> str:
    """Return the raw text of a data file shipped inside the package.

    ``filename`` is a path relative to ``faultline/pipeline_v2/data``,
    e.g. ``"dependency-anchors.yaml"`` or ``"stacks/fastapi.yaml"``.

    Raises ``FileNotFoundError`` if the resource is missing — a missing
    data file is a packaging bug, never a silently-tolerated condition.
    """
    resource = files(_PACKAGE)
    for part in filename.split("/"):
        resource = resource / part
    if not resource.is_file():
        raise FileNotFoundError(
            f"pipeline_v2 data resource not packaged: {filename!r} "
            f"(expected inside {_PACKAGE})"
        )
    return resource.read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def load_yaml(filename: str) -> dict:
    """Parse a packaged YAML data file into a dict.

    Returns ``{}`` when the parsed document is not a mapping (matching
    the extractors' historical tolerance). Propagates ``FileNotFoundError``
    (missing file = packaging bug) and ``yaml.YAMLError`` (corrupt data).
    """
    data = yaml.safe_load(load_data_text(filename)) or {}
    return data if isinstance(data, dict) else {}


def load_stack_yaml(stack_name: str) -> dict:
    """Parse ``stacks/<stack_name>.yaml`` from the packaged data tree.

    ``stack_name`` is the bare stem, e.g. ``"fastapi"`` or
    ``"go-http-router"``.
    """
    return load_yaml(f"stacks/{stack_name}.yaml")
