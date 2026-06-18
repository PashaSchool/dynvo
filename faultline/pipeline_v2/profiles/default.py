"""The default / no-op :class:`FrameworkProfile`.

Guarantees the universal property: *no repo ever falls through to a
crash or a hard error because its framework is unknown*. The default
profile understands nothing framework-specific, so it:

  * detects every repo with a tiny positive floor (wins only when no
    specific profile matches),
  * still splits monorepos via the shared workspace-splitter (workspace
    splitting is package-manager-driven, not framework-specific, so the
    default profile gets the biggest blob lever for free),
  * classifies nothing (:attr:`FileRole.UNKNOWN`), claims no features
    (``feature_of`` → ``None``), finds no entries — leaving the
    deterministic extractors + residual clustering fully in charge,
    exactly as today.

This is the Null Object for the Framework Knowledge Layer: substituting
it changes nothing about current behaviour (LSP), which is what makes
introducing the abstraction safe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from faultline.pipeline_v2.profiles._splitter import split_workspaces
from faultline.pipeline_v2.profiles.base import (
    AttributionSpec,
    FileRole,
    FlowEntry,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace


# Smallest positive confidence — the default always beats "no match"
# (0.0) but loses to any specific profile returning a real score.
_DEFAULT_FLOOR = 0.01


class DefaultProfile:
    """Null-object profile for unknown / unsupported stacks."""

    name = "default"

    def detects(self, ctx: "ScanContext") -> float:
        return _DEFAULT_FLOOR

    def workspaces(self, ctx: "ScanContext") -> list["Workspace"]:
        # Package-manager workspace splitting is framework-agnostic, so
        # even unknown stacks get the monorepo-split blob lever.
        return split_workspaces(ctx)

    def classify_file(self, path: str) -> FileRole:
        return FileRole.UNKNOWN

    def feature_of(self, path: str, ctx: "ScanContext") -> str | None:
        return None

    def flow_entries(self, ctx: "ScanContext") -> list[FlowEntry]:
        return []

    def attribution_rules(self) -> AttributionSpec:
        return AttributionSpec()


__all__ = ["DefaultProfile"]
