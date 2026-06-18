"""Framework Knowledge Layer — the ``FrameworkProfile`` contract.

A ``FrameworkProfile`` is the engine's deep, per-framework understanding
of *how a framework assembles files into user-facing capabilities*:
its routing convention, where pages / api / components / hooks /
services / domain-models live, its monorepo-split rules, and how shared
files fan out. Attribution (file → feature → flow) becomes a
*consequence* of that structural model instead of generic path math —
which is what kills the monorepo / ``js-generic`` blob bug.

The pipeline depends on THIS interface, never on a concrete profile
(DIP). Adding a new framework = adding one module that implements the
Protocol + one entry-point line (OCP); the core never changes. A
:class:`~faultline.pipeline_v2.profiles.default.DefaultProfile` keeps
unknown stacks working (graceful degradation, LSP).

Design rules (mirrors :mod:`faultline.framework_linkers.base`):
  * The Protocol is FROZEN once Phase 1 ships. New profiles plug in via
    Python entry-points under the ``faultlines.profiles`` group.
  * Methods are small and single-purpose (SRP): detection,
    classification, attribution are separate calls.
  * Fully deterministic — NO LLM, NO network. (Tier-2 LLM
    "framework-reasoner" fallback for novel stacks is a *separate*
    later-phase profile that also implements this interface; the
    interface itself stays LLM-free.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace


class FileRole(str, Enum):
    """The structural role a file plays inside its framework.

    A ``str`` enum so it serialises straight into stage artifacts and
    compares equal to its value in tests. ``UNKNOWN`` is the honest
    answer when a profile cannot classify a path — never guess.
    """

    ROUTE = "route"
    PAGE = "page"
    API = "api"
    COMPONENT = "component"
    HOOK = "hook"
    SERVICE = "service"
    DOMAIN = "domain"
    CONFIG = "config"
    TEST = "test"
    LIB = "lib"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FlowEntry:
    """One user-/system-facing entry point the framework exposes.

    Entry points are the route handlers, page entries, server actions,
    and job triggers a profile knows how to recognise structurally.
    They seed Stage 3 flow detection. ``path`` is repo-relative POSIX.

    Attributes:
        path: the file that hosts the entry point.
        symbol: the exported handler / component / action symbol, or
            ``""`` when the file itself is the entry (filesystem
            routing).
        kind: free-form structural category — ``"page"``, ``"http"``,
            ``"server-action"``, ``"job"``, ... A profile chooses its
            own vocabulary; downstream consumers treat it as opaque.
        route: the URL/route pattern when one applies (``""`` for
            non-routed entries such as jobs).
    """

    path: str
    symbol: str = ""
    kind: str = ""
    route: str = ""


@dataclass(frozen=True)
class AttributionSpec:
    """How a profile wants shared / fan-out files attributed.

    A small declarative spec consumed by the attribution stages
    (Stage 2 / 5.5). It carries *policy*, not per-repo paths — keeping
    framework knowledge declarative and stack-pattern-driven rather
    than baking repo-specific names into Python.

    Attributes:
        colocate_roots: directory-role markers under which siblings are
            considered one feature (e.g. a route segment folder). Role
            names, not literal repo paths.
        shared_roles: file roles that are genuinely cross-cutting and
            should fan out (blast-radius) rather than collapse into a
            single owner — e.g. ``LIB``, ``COMPONENT``.
        max_fanout: cap on how many features a single shared file may
            be attributed to. ``None`` == unbounded (let the stage
            decide). Scale-invariant policy, not a tuned magic number.
    """

    colocate_roots: tuple[str, ...] = ()
    shared_roles: tuple[FileRole, ...] = ()
    max_fanout: int | None = None


@runtime_checkable
class FrameworkProfile(Protocol):
    """The Framework Knowledge Layer contract.

    One concrete profile per framework. The pipeline interacts with
    every profile identically through this interface (LSP); it never
    imports a concrete profile by name (DIP). All methods are pure
    with respect to ``ctx`` and deterministic.

    ``name`` is the profile's stable slug (``"next-app-router"``,
    ``"nestjs"``, ``"default"``, ...). It is the registry lookup key
    and the value surfaced in ``scan_meta``.

    Activation: the registry picks the profile whose :meth:`detects`
    returns the highest confidence for a given context. A profile that
    does not apply MUST return ``0.0`` from :meth:`detects` (never
    raise).
    """

    name: str

    def detects(self, ctx: "ScanContext") -> float:
        """Confidence in ``[0.0, 1.0]`` that this profile fits ``ctx``.

        ``0.0`` means "not my framework". The
        :class:`~faultline.pipeline_v2.profiles.default.DefaultProfile`
        returns a small positive floor so it always wins when no
        specific profile matches, and never otherwise.
        """
        ...

    def workspaces(self, ctx: "ScanContext") -> list["Workspace"]:
        """Return the workspaces to scan, scoped per attribution unit.

        For a monorepo this is one :class:`Workspace` per package; for
        a single-package repo it is exactly ``[root]`` (the whole repo
        as one workspace). Stage 1 extraction + Stage 2 attribution run
        PER returned workspace so a monorepo never collapses into one
        flat dump. Must be total — never raise, never return ``[]``.
        """
        ...

    def classify_file(self, path: str) -> FileRole:
        """Classify a single repo-relative path into a :class:`FileRole`.

        Returns :attr:`FileRole.UNKNOWN` when the profile cannot place
        the path — an honest unknown, not a guess.
        """
        ...

    def feature_of(self, path: str, ctx: "ScanContext") -> str | None:
        """The capability (kebab feature key) this file serves, or ``None``.

        ``None`` means "this profile has no opinion" — the path falls
        through to the deterministic extractors / residual clustering.
        """
        ...

    def flow_entries(self, ctx: "ScanContext") -> list[FlowEntry]:
        """Structural entry points (routes / pages / actions / jobs).

        Returning ``[]`` is the canonical "no recognisable entries"
        answer; it is NOT an error.
        """
        ...

    def attribution_rules(self) -> AttributionSpec:
        """Declarative policy for how shared / fan-out files attribute."""
        ...


__all__ = [
    "AttributionSpec",
    "FileRole",
    "FlowEntry",
    "FrameworkProfile",
]
