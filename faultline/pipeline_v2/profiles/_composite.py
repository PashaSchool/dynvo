"""Composite (per-scan-unit) framework profile — StackProfile Phase B+.

A hybrid repo (polar: FastAPI backend + Next monorepo frontend;
plane: Django backend + react-router apps) has no single framework
truth: ONE whole-repo profile serves one tree well and leaves the
others to generic path math. The :class:`CompositeProfile` fixes that
WITHOUT touching the frozen :class:`FrameworkProfile` Protocol or the
pipeline trunk: it *is* a FrameworkProfile whose methods dispatch by
longest-prefix scan-unit ownership:

  * a path under a unit whose selected profile DIFFERS from the
    whole-repo winner → that unit's profile, queried with a
    unit-scoped context;
  * every other path (the "residue": the backend tree, ride-along
    libs, repo-root files) → the whole-repo winner, queried with the
    residue-scoped context.

The trunk keeps seeing exactly one profile object (LSP); downstream
consumers (Stage 1 overrides, Stage 2 re-home, Stage 2.6 fan-out,
Stage 3 flow seeding) need zero changes.

Construction happens only in
:func:`faultline.pipeline_v2.profiles._per_unit.select_scan_profile`;
the composite is never registered in the registry and its
:meth:`detects` is inert. Deterministic: units are ordered by
(depth desc, path), names sorted — no dict-order or hash-order leaks.

Deterministic — NO LLM, NO network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.profiles.base import (
    AttributionSpec,
    FileRole,
    FlowEntry,
)
from faultline.pipeline_v2.profiles._splitter import split_workspaces
from faultline.pipeline_v2.unit_scope import (
    residual_scoped_ctx,
    unit_scoped_ctx,
)

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace


def _posix(path: str) -> str:
    return path.replace("\\", "/")


class _ScopedOverrideExtractor:
    """Stage-1 extractor adapter that scopes inner extractors per unit.

    One adapter per override NAME. ``parts`` is an ordered list of
    ``(subpath_or_None, extractor_instance)`` — ``None`` scope means
    "the residue" (everything outside every unit). ``extract`` narrows
    the received context to each part's scope and concatenates the
    candidates, so a unit profile's extractor can never claim files
    outside its unit and the root profile's extractor never claims a
    unit it lost.
    """

    def __init__(
        self,
        name: str,
        parts: list[tuple[str | None, Any]],
        unit_subpaths: tuple[str, ...],
    ) -> None:
        self.name = name
        self._parts = list(parts)
        self._unit_subpaths = unit_subpaths

    def extract(self, ctx: "ScanContext") -> list[Any]:
        out: list[Any] = []
        for subpath, inner in self._parts:
            scoped = (
                unit_scoped_ctx(ctx, subpath)
                if subpath is not None
                else residual_scoped_ctx(ctx, self._unit_subpaths)
            )
            if not scoped.tracked_files:
                continue
            out.extend(inner.extract(scoped) or [])
        return out


class CompositeProfile:
    """Per-scan-unit dispatching :class:`FrameworkProfile`.

    ``units`` — ``((subpath, profile), ...)`` for every unit whose
    selected profile DIFFERS from ``root``; each ``profile`` is a
    FRESH instance (single-slot index memos inside concrete profiles
    must not be shared across scopes). ``root`` serves the residue.
    """

    def __init__(
        self,
        root: Any,
        units: tuple[tuple[str, Any], ...],
    ) -> None:
        self._root = root
        # Longest-prefix dispatch: deepest unit first, path tie-break.
        self._units = tuple(
            sorted(units, key=lambda t: (-t[0].count("/"), t[0]))
        )
        self._unit_subpaths = tuple(sp for sp, _ in self._units)
        names = sorted(
            {getattr(root, "name", "default")}
            | {getattr(p, "name", "default") for _, p in self._units}
        )
        self.name = "hybrid(" + "+".join(names) + ")"
        # Scoped-context memo, keyed by the identity of the incoming
        # ctx (stages hand the profile fresh deep copies; one rebuild
        # per stage). Values: subpath|"" → scoped ScanContext.
        self._ctx_key: tuple[int, int] | None = None
        self._scoped: dict[str, "ScanContext"] = {}

    # ── introspection (scan_meta telemetry) ─────────────────────────

    @property
    def unit_assignments(self) -> tuple[tuple[str, str], ...]:
        """``((subpath, profile_name), ...)`` sorted by subpath."""
        return tuple(
            sorted((sp, getattr(p, "name", "default")) for sp, p in self._units)
        )

    @property
    def root_profile_name(self) -> str:
        return str(getattr(self._root, "name", "default"))

    # ── scoped-ctx memo ──────────────────────────────────────────────

    def _scope(self, ctx: "ScanContext", subpath: str | None) -> "ScanContext":
        key = (id(ctx), len(ctx.tracked_files))
        if self._ctx_key != key:
            self._ctx_key = key
            self._scoped = {}
        slot = subpath if subpath is not None else ""
        if slot not in self._scoped:
            self._scoped[slot] = (
                unit_scoped_ctx(ctx, subpath)
                if subpath is not None
                else residual_scoped_ctx(ctx, self._unit_subpaths)
            )
        return self._scoped[slot]

    def _owner(self, path: str) -> tuple[str | None, Any]:
        p = _posix(path)
        for subpath, profile in self._units:
            if p == subpath or p.startswith(subpath.rstrip("/") + "/"):
                return subpath, profile
        return None, self._root

    # ── FrameworkProfile contract ────────────────────────────────────

    def detects(self, ctx: "ScanContext") -> float:  # noqa: ARG002 — contract
        """Never registered / never competes — selection built this."""
        return 0.0

    def workspaces(self, ctx: "ScanContext") -> list["Workspace"]:
        """Package-manager splitting is framework-agnostic — delegate."""
        return split_workspaces(ctx)

    def classify_file(self, path: str) -> FileRole:
        _subpath, profile = self._owner(path)
        return profile.classify_file(path)

    def feature_of(self, path: str, ctx: "ScanContext") -> str | None:
        subpath, profile = self._owner(path)
        return profile.feature_of(path, self._scope(ctx, subpath))

    def flow_entries(self, ctx: "ScanContext") -> list[FlowEntry]:
        entries: list[FlowEntry] = []
        seen: set[tuple[str, str, str, str]] = set()
        scopes: list[tuple[str | None, Any]] = [
            *((sp, p) for sp, p in sorted(self._units, key=lambda t: t[0])),
            (None, self._root),
        ]
        for subpath, profile in scopes:
            scoped = self._scope(ctx, subpath)
            if not scoped.tracked_files:
                continue
            for e in profile.flow_entries(scoped) or []:
                key = (e.path, e.symbol, e.kind, e.route)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(e)
        return entries

    def attribution_rules(self) -> AttributionSpec:
        """Merged declarative policy across the involved profiles.

        Shared roles union (a role any involved profile fans out, fans
        out); ``max_fanout`` = the most conservative non-``None`` cap;
        colocate roots union. Order-stable via sorting.
        """
        specs = [self._root.attribution_rules()] + [
            p.attribution_rules() for _, p in self._units
        ]
        shared = sorted(
            {role for s in specs for role in s.shared_roles},
            key=lambda r: r.value,
        )
        colocate = tuple(sorted({c for s in specs for c in s.colocate_roots}))
        caps = [s.max_fanout for s in specs if s.max_fanout is not None]
        return AttributionSpec(
            colocate_roots=colocate,
            shared_roles=tuple(shared),
            max_fanout=min(caps) if caps else None,
        )

    # ── optional contracts (duck-typed by the trunk) ─────────────────

    def synthesize_features(self, ctx: "ScanContext") -> list[Any]:
        """Concatenated per-scope synthesis (units first, then residue).

        The attribution wiring skips names that already exist, so a
        collision across scopes resolves first-writer-wins in the
        deterministic scope order.
        """
        out: list[Any] = []
        scopes: list[tuple[str | None, Any]] = [
            *((sp, p) for sp, p in sorted(self._units, key=lambda t: t[0])),
            (None, self._root),
        ]
        for subpath, profile in scopes:
            method = getattr(profile, "synthesize_features", None)
            if method is None:
                continue
            scoped = self._scope(ctx, subpath)
            if not scoped.tracked_files:
                continue
            out.extend(method(scoped) or [])
        return out

    def stage_1_extractor_overrides(self, ctx: "ScanContext") -> list[object]:
        """Per-scope Stage-1 overrides, multiplexed by extractor name.

        Every override an involved profile supplies is wrapped so it
        only ever sees its own scope. Same-name overrides from several
        scopes merge into ONE adapter (Stage 1's merge rule is
        replace-by-name; two entries with one name would drop one).
        """
        grouped: dict[str, list[tuple[str | None, Any]]] = {}
        order: list[str] = []
        scopes: list[tuple[str | None, Any]] = [
            *((sp, p) for sp, p in sorted(self._units, key=lambda t: t[0])),
            (None, self._root),
        ]
        for subpath, profile in scopes:
            method = getattr(profile, "stage_1_extractor_overrides", None)
            if method is None:
                continue
            scoped = self._scope(ctx, subpath)
            for inst in method(scoped) or []:
                name = getattr(inst, "name", None)
                if not name:
                    continue
                if name not in grouped:
                    grouped[name] = []
                    order.append(name)
                grouped[name].append((subpath, inst))
        return [
            _ScopedOverrideExtractor(name, grouped[name], self._unit_subpaths)
            for name in order
        ]


__all__ = ["CompositeProfile"]
