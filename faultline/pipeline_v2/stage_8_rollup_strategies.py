"""Stage 8 — Flow Rollup Strategies (Sprint S6.1).

Attaches the deduplicated top-level ``flows[]`` to ``product_features[]``
using a strategy chosen by ``ctx.repo_shape`` (populated by Stage 0.6).
Replaces the single-algorithm Stage 8 logic that either over-attaches
(variant A, ``overlap >= 1``) or under-attaches on libraries (variant B
without Sonnet hints).

Design
======

  - One strategy per shape, each implementing :class:`FlowRollupStrategy`.
  - ``SHAPE_ROLLUPS`` is the single source of truth registry.
  - The dispatcher ``stage_8_rollup_flows`` looks up
    ``SHAPE_ROLLUPS[ctx.repo_shape]`` and falls back to
    ``universal-residual`` for unknown shapes.
  - Each strategy MUTATES ``pf.flows`` in place; ``flow_ids`` field is
    NOT used because the codebase's ``Feature`` model already carries
    ``flows: list[Flow]`` (containment).
  - Strategies NEVER call LLMs. ``OssLibraryStrategy`` /
    ``FrameworkRepoStrategy`` consume an already-produced
    ``sonnet_member_flows_map`` when available.
  - Universal-residual NEVER over-attaches: no ``overlap >= 1`` rule;
    threshold is ``UNIVERSAL_OVERLAP_THRESHOLD = 0.50``.

Telemetry
=========

Returns a :class:`RollupResult` with per-PF rationale, unattributed
flows, and diagnostics. The dispatcher writes ``08-stage-rollup.json``
artifact via :func:`write_stage_artifact`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:
    from faultline.models.types import Feature, Flow
    from faultline.pipeline_v2.stage_0_intake import ScanContext

logger = logging.getLogger(__name__)


# ── Universal constants ────────────────────────────────────────────────

# Per-PF flow attachment ceiling for landing UI sanity. Universal.
MAX_FLOWS_IN_PAYLOAD: int = 200

# Fraction of flow.paths that must live inside pf.paths to attach in
# the universal-residual pass 2. 0.50 = "majority of files the flow
# touches live in this PF". Never use overlap>=1 (variant-A overspam).
UNIVERSAL_OVERLAP_THRESHOLD: float = 0.50

# Fallback registry key when ctx.repo_shape is None or unknown.
_FALLBACK_KEY: str = "universal-residual"


# ── Data types ─────────────────────────────────────────────────────────


@dataclass
class RollupResult:
    """Outcome of running one Stage 8 flow-rollup strategy."""

    strategy_used: str
    pfs_attributed_count: int
    total_attachments: int
    per_pf_rationale: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    unattributed_flows: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class FlowRollupStrategy(Protocol):
    """Strategy interface for one repo-shape's flow attribution policy."""

    shape: str

    def rollup(
        self,
        product_features: list["Feature"],
        top_flows: list["Flow"],
        ctx: "ScanContext",
        *,
        sonnet_member_flows_map: dict[str, list[str]] | None = None,
    ) -> RollupResult:
        """Attach flows to ``product_features`` in place via ``pf.flows``.

        Returns a :class:`RollupResult` capturing per-PF rationale for
        the artifact. Pure function modulo the in-place mutation.
        """


# ── Helpers ────────────────────────────────────────────────────────────


def _workspace_prefix(path: str, ctx: "ScanContext") -> str | None:
    """Return the workspace prefix for ``path`` (e.g. ``apps/web``) or None.

    Uses ``ctx.workspaces`` when present. Falls back to a simple
    ``apps/<x>``/``packages/<x>`` heuristic for monorepos that didn't
    enumerate workspaces.
    """
    if not path:
        return None
    workspaces = ctx.workspaces or []
    # Prefer explicit workspace declarations.
    candidates: list[str] = sorted(
        (w.path.strip("/") for w in workspaces if w.path),
        key=len,
        reverse=True,
    )
    norm = path.replace("\\", "/").lstrip("/")
    for ws in candidates:
        if norm == ws or norm.startswith(ws + "/"):
            return ws
    # Heuristic fallback for monorepos without declared workspaces.
    parts = norm.split("/")
    if len(parts) >= 2 and parts[0] in {"apps", "packages"}:
        return parts[0] + "/" + parts[1]
    return None


def _pf_workspaces(pf: "Feature", ctx: "ScanContext") -> set[str]:
    """Union of workspace prefixes over ``pf.paths``. Empty when no
    workspace can be derived (paths live at repo root)."""
    out: set[str] = set()
    for p in pf.paths or []:
        ws = _workspace_prefix(p, ctx)
        if ws is not None:
            out.add(ws)
    return out


def _attach(pf: "Feature", flow: "Flow") -> None:
    """Append ``flow`` to ``pf.flows`` if not already attached.

    Dedup is keyed by ``Flow.id`` when present (Sprint B1 stable IDs)
    or ``Flow.name`` otherwise.
    """
    key = lambda f: f.id or f.name  # noqa: E731
    target = key(flow)
    if any(key(f) == target for f in pf.flows):
        return
    pf.flows.append(flow)


def _controller_name(path: str | None) -> str | None:
    """Heuristic: extract the controller base from a controller path.

    Examples:
      ``app/controllers/users_controller.rb`` → ``users``
      ``app/Http/Controllers/UsersController.php`` → ``users``
      ``users/views.py`` → None (not a controller convention)
    """
    if not path:
        return None
    norm = path.replace("\\", "/").lower()
    # Rails / Phoenix
    for marker in ("/controllers/", "/_controller."):
        pass  # placeholder for readability
    # Rails: ``..._controller.rb`` / ``..._controller.ex``
    last = norm.rsplit("/", 1)[-1]
    for suffix in ("_controller.rb", "_controller.ex"):
        if last.endswith(suffix):
            return last[: -len(suffix)]
    # Laravel / .NET: ``UsersController.php`` / ``UsersController.cs``
    for suffix in ("controller.php", "controller.cs", "controller.java"):
        if last.endswith(suffix):
            return last[: -len(suffix)]
    return None


def _command_name(flow: "Flow") -> str | None:
    """Heuristic: extract the CLI command name from a flow entry-point.

    Examples:
      ``cmd/serve/main.go`` → ``serve``
      ``cmd/migrate/main.go`` → ``migrate``
      ``bin/migrate.js`` → ``migrate``
      ``src/cli/serve.ts`` → ``serve``
    """
    ep = flow.entry_point_file
    if not ep:
        return None
    norm = ep.replace("\\", "/")
    parts = norm.split("/")
    # cmd/<cmd>/main.go
    if "cmd" in parts:
        idx = parts.index("cmd")
        if idx + 1 < len(parts) - 0 and len(parts) > idx + 1:
            return parts[idx + 1].split(".")[0]
    # bin/<cmd>.js
    if parts and parts[0] == "bin" and len(parts) >= 2:
        return parts[1].split(".")[0]
    # src/cli/<cmd>.ts
    if "cli" in parts:
        idx = parts.index("cli")
        if idx + 1 < len(parts):
            return parts[idx + 1].split(".")[0]
    return None


def _slug_substr(needle: str, haystack: str) -> bool:
    """Case-insensitive kebab-normalized substring match."""
    n = needle.lower().replace("_", "-").strip("-")
    h = haystack.lower().replace("_", "-").strip("-")
    return bool(n) and n in h


# ── Concrete strategies ────────────────────────────────────────────────


class TurborepoMonorepoStrategy:
    """Attach flow → PF when their workspaces match.

    In a Turborepo, the workspace boundary IS the product boundary.
    """

    shape: str = "turborepo-monorepo"

    def rollup(
        self,
        product_features: list["Feature"],
        top_flows: list["Flow"],
        ctx: "ScanContext",
        *,
        sonnet_member_flows_map: dict[str, list[str]] | None = None,
    ) -> RollupResult:
        per_pf_rationale: dict[str, list[tuple[str, str]]] = defaultdict(list)
        unattributed: list[str] = []
        pf_ws_map: dict[str, set[str]] = {
            pf.name: _pf_workspaces(pf, ctx) for pf in product_features
        }

        attachments = 0
        for flow in top_flows:
            ws = _workspace_prefix(flow.entry_point_file or "", ctx)
            if ws is None:
                unattributed.append(flow.name)
                continue
            matched = False
            for pf in product_features:
                if ws in pf_ws_map[pf.name]:
                    _attach(pf, flow)
                    per_pf_rationale[pf.name].append(
                        (flow.name, f"workspace-match:{ws}"),
                    )
                    attachments += 1
                    matched = True
            if not matched:
                unattributed.append(flow.name)

        pfs_attributed = sum(1 for pf in product_features if pf.flows)
        return RollupResult(
            strategy_used=self.shape,
            pfs_attributed_count=pfs_attributed,
            total_attachments=attachments,
            per_pf_rationale=dict(per_pf_rationale),
            unattributed_flows=unattributed,
        )


class SingleSaasRoutedStrategy:
    """Attach flow → PF when ``flow.entry_point_file in pf.paths``.

    Validated on dub at 84% PF coverage with no over-attach. Routes ARE
    the product surface for single-package routed SaaS apps.
    """

    shape: str = "single-saas-routed"

    def rollup(
        self,
        product_features: list["Feature"],
        top_flows: list["Flow"],
        ctx: "ScanContext",
        *,
        sonnet_member_flows_map: dict[str, list[str]] | None = None,
    ) -> RollupResult:
        per_pf_rationale: dict[str, list[tuple[str, str]]] = defaultdict(list)
        unattributed: list[str] = []
        pf_path_sets: dict[str, set[str]] = {
            pf.name: set(pf.paths or []) for pf in product_features
        }
        attachments = 0
        for flow in top_flows:
            ep = flow.entry_point_file
            if not ep:
                unattributed.append(flow.name)
                continue
            matched = False
            for pf in product_features:
                if ep in pf_path_sets[pf.name]:
                    _attach(pf, flow)
                    per_pf_rationale[pf.name].append(
                        (flow.name, f"entry-point-in-paths:{ep}"),
                    )
                    attachments += 1
                    matched = True
            if not matched:
                unattributed.append(flow.name)
        return RollupResult(
            strategy_used=self.shape,
            pfs_attributed_count=sum(1 for pf in product_features if pf.flows),
            total_attachments=attachments,
            per_pf_rationale=dict(per_pf_rationale),
            unattributed_flows=unattributed,
        )


class OssLibraryStrategy:
    """Defer to Sonnet's ``member_flows`` map; NO path-fallback.

    For libraries, paths don't carry product-surface semantics, so a
    wrong attachment is worse than an empty one. Returns all flows
    unattributed when ``sonnet_member_flows_map`` is missing.
    """

    shape: str = "oss-library"

    def rollup(
        self,
        product_features: list["Feature"],
        top_flows: list["Flow"],
        ctx: "ScanContext",
        *,
        sonnet_member_flows_map: dict[str, list[str]] | None = None,
    ) -> RollupResult:
        if not sonnet_member_flows_map:
            logger.warning(
                "OssLibraryStrategy: no sonnet_member_flows_map provided "
                "(repo=%s) — no flows will be attached; libraries need "
                "semantic matching",
                ctx.repo_path.name,
            )
            return RollupResult(
                strategy_used=self.shape,
                pfs_attributed_count=0,
                total_attachments=0,
                per_pf_rationale={},
                unattributed_flows=[f.name for f in top_flows],
                diagnostics={"reason": "no_sonnet_member_flows"},
            )

        per_pf_rationale: dict[str, list[tuple[str, str]]] = defaultdict(list)
        flows_by_name: dict[str, "Flow"] = {f.name: f for f in top_flows}
        attachments = 0
        attributed_names: set[str] = set()
        pfs_by_name: dict[str, "Feature"] = {pf.name: pf for pf in product_features}
        unknown_pfs = 0

        for pf_name, member_flow_names in sonnet_member_flows_map.items():
            pf = pfs_by_name.get(pf_name)
            if pf is None:
                unknown_pfs += 1
                logger.warning(
                    "OssLibraryStrategy: sonnet_member_flows_map references "
                    "unknown product_feature=%s — skipping",
                    pf_name,
                )
                continue
            for flow_name in member_flow_names or ():
                flow = flows_by_name.get(flow_name)
                if flow is None:
                    continue
                _attach(pf, flow)
                per_pf_rationale[pf.name].append(
                    (flow.name, "sonnet-member-flows"),
                )
                attachments += 1
                attributed_names.add(flow_name)

        unattributed = [
            f.name for f in top_flows if f.name not in attributed_names
        ]
        return RollupResult(
            strategy_used=self.shape,
            pfs_attributed_count=sum(1 for pf in product_features if pf.flows),
            total_attachments=attachments,
            per_pf_rationale=dict(per_pf_rationale),
            unattributed_flows=unattributed,
            diagnostics={"unknown_pf_references": unknown_pfs},
        )


class BackendMonolithStrategy:
    """Controller-class as the product-feature group.

    Rails/Django/Laravel product surfaces map onto controllers
    (e.g. ``users_controller.rb`` → ``users``). Falls back to
    entry-point-in-paths when the entry-point isn't a controller.
    """

    shape: str = "backend-monolith"

    def rollup(
        self,
        product_features: list["Feature"],
        top_flows: list["Flow"],
        ctx: "ScanContext",
        *,
        sonnet_member_flows_map: dict[str, list[str]] | None = None,
    ) -> RollupResult:
        # Build per-PF controller groups: set of controller base names
        # extracted from each pf's paths.
        per_pf_rationale: dict[str, list[tuple[str, str]]] = defaultdict(list)
        controller_groups: dict[str, set[str]] = {}
        pf_path_sets: dict[str, set[str]] = {}
        for pf in product_features:
            groups: set[str] = set()
            for p in pf.paths or []:
                c = _controller_name(p)
                if c:
                    groups.add(c)
            controller_groups[pf.name] = groups
            pf_path_sets[pf.name] = set(pf.paths or [])

        unattributed: list[str] = []
        attachments = 0
        for flow in top_flows:
            ep = flow.entry_point_file
            fc = _controller_name(ep)
            matched = False
            if fc is not None:
                for pf in product_features:
                    if fc in controller_groups[pf.name]:
                        _attach(pf, flow)
                        per_pf_rationale[pf.name].append(
                            (flow.name, f"controller-match:{fc}"),
                        )
                        attachments += 1
                        matched = True
            else:
                # Fallback: entry-point-in-paths
                if ep:
                    for pf in product_features:
                        if ep in pf_path_sets[pf.name]:
                            _attach(pf, flow)
                            per_pf_rationale[pf.name].append(
                                (flow.name, f"entry-point-in-paths:{ep}"),
                            )
                            attachments += 1
                            matched = True
            if not matched:
                unattributed.append(flow.name)
        return RollupResult(
            strategy_used=self.shape,
            pfs_attributed_count=sum(1 for pf in product_features if pf.flows),
            total_attachments=attachments,
            per_pf_rationale=dict(per_pf_rationale),
            unattributed_flows=unattributed,
        )


class CliToolStrategy:
    """Map flow → PF via CLI command name or entry-point-in-paths."""

    shape: str = "cli-tool"

    def rollup(
        self,
        product_features: list["Feature"],
        top_flows: list["Flow"],
        ctx: "ScanContext",
        *,
        sonnet_member_flows_map: dict[str, list[str]] | None = None,
    ) -> RollupResult:
        per_pf_rationale: dict[str, list[tuple[str, str]]] = defaultdict(list)
        unattributed: list[str] = []
        attachments = 0
        pf_path_sets = {pf.name: set(pf.paths or []) for pf in product_features}
        for flow in top_flows:
            cmd = _command_name(flow)
            ep = flow.entry_point_file
            matched = False
            for pf in product_features:
                if cmd and _slug_substr(cmd, pf.name):
                    _attach(pf, flow)
                    per_pf_rationale[pf.name].append(
                        (flow.name, f"command-match:{cmd}"),
                    )
                    attachments += 1
                    matched = True
                elif ep and ep in pf_path_sets[pf.name]:
                    _attach(pf, flow)
                    per_pf_rationale[pf.name].append(
                        (flow.name, f"entry-point-in-paths:{ep}"),
                    )
                    attachments += 1
                    matched = True
            if not matched:
                unattributed.append(flow.name)
        return RollupResult(
            strategy_used=self.shape,
            pfs_attributed_count=sum(1 for pf in product_features if pf.flows),
            total_attachments=attachments,
            per_pf_rationale=dict(per_pf_rationale),
            unattributed_flows=unattributed,
        )


class FrameworkRepoStrategy:
    """Like OssLibrary but with an entry-point-in-paths fallback.

    Framework repos sometimes have demo apps with route files; safe-ish
    to attach by path when LLM is unavailable.
    """

    shape: str = "framework-repo"

    def rollup(
        self,
        product_features: list["Feature"],
        top_flows: list["Flow"],
        ctx: "ScanContext",
        *,
        sonnet_member_flows_map: dict[str, list[str]] | None = None,
    ) -> RollupResult:
        per_pf_rationale: dict[str, list[tuple[str, str]]] = defaultdict(list)
        unattributed: list[str] = []
        attachments = 0
        used_fallback = False
        attributed: set[str] = set()
        pf_path_sets = {pf.name: set(pf.paths or []) for pf in product_features}

        if sonnet_member_flows_map:
            flows_by_name = {f.name: f for f in top_flows}
            pfs_by_name = {pf.name: pf for pf in product_features}
            for pf_name, member_flow_names in sonnet_member_flows_map.items():
                pf = pfs_by_name.get(pf_name)
                if pf is None:
                    continue
                for flow_name in member_flow_names or ():
                    flow = flows_by_name.get(flow_name)
                    if flow is None:
                        continue
                    _attach(pf, flow)
                    per_pf_rationale[pf.name].append(
                        (flow.name, "sonnet-member-flows"),
                    )
                    attachments += 1
                    attributed.add(flow_name)

        # Pass 2 — entry-point-in-paths fallback ONLY for flows that
        # the Sonnet map didn't cover.
        for flow in top_flows:
            if flow.name in attributed:
                continue
            ep = flow.entry_point_file
            if not ep:
                unattributed.append(flow.name)
                continue
            matched = False
            for pf in product_features:
                if ep in pf_path_sets[pf.name]:
                    _attach(pf, flow)
                    per_pf_rationale[pf.name].append(
                        (flow.name, "entry-point-in-paths-fallback"),
                    )
                    attachments += 1
                    matched = True
                    used_fallback = True
            if not matched:
                unattributed.append(flow.name)

        return RollupResult(
            strategy_used=self.shape,
            pfs_attributed_count=sum(1 for pf in product_features if pf.flows),
            total_attachments=attachments,
            per_pf_rationale=dict(per_pf_rationale),
            unattributed_flows=unattributed,
            diagnostics={"fallback": used_fallback},
        )


class UniversalResidualStrategy:
    """Two-pass deterministic fallback: entry-point then high-overlap.

    Pass 1: ``flow.entry_point_file in pf.paths`` → attach with
    ``reason="entry-point-in-paths"``.

    Pass 2 (only for flows not attributed in pass 1, requires
    ``len(flow.paths) >= 2``): compute ``overlap = |flow.paths ∩ pf.paths|
    / len(flow.paths)``; if ``overlap >= UNIVERSAL_OVERLAP_THRESHOLD``,
    attach with ``reason=f"path-overlap:{overlap:.2f}"``.

    NEVER uses ``overlap >= 1`` (variant-A over-attach trap).
    """

    shape: str = "universal-residual"

    def rollup(
        self,
        product_features: list["Feature"],
        top_flows: list["Flow"],
        ctx: "ScanContext",
        *,
        sonnet_member_flows_map: dict[str, list[str]] | None = None,
    ) -> RollupResult:
        per_pf_rationale: dict[str, list[tuple[str, str]]] = defaultdict(list)
        attachments = 0
        attributed: set[str] = set()
        pf_path_sets = {pf.name: set(pf.paths or []) for pf in product_features}

        # Pass 1 — entry-point-in-paths.
        for flow in top_flows:
            ep = flow.entry_point_file
            if not ep:
                continue
            matched = False
            for pf in product_features:
                if ep in pf_path_sets[pf.name]:
                    _attach(pf, flow)
                    per_pf_rationale[pf.name].append(
                        (flow.name, "entry-point-in-paths"),
                    )
                    attachments += 1
                    matched = True
            if matched:
                attributed.add(flow.name)

        # Pass 2 — high-overlap secondary; ONLY for unattributed flows
        # with len(paths) >= 2 (single-path flows can't satisfy the rule).
        for flow in top_flows:
            if flow.name in attributed:
                continue
            flow_paths = flow.paths or []
            if len(flow_paths) < 2:
                continue
            flow_set = set(flow_paths)
            for pf in product_features:
                pf_set = pf_path_sets[pf.name]
                if not pf_set:
                    continue
                overlap = len(flow_set & pf_set) / len(flow_paths)
                if overlap >= UNIVERSAL_OVERLAP_THRESHOLD:
                    _attach(pf, flow)
                    per_pf_rationale[pf.name].append(
                        (flow.name, f"path-overlap:{overlap:.2f}"),
                    )
                    attachments += 1
                    attributed.add(flow.name)

        unattributed = [f.name for f in top_flows if f.name not in attributed]
        return RollupResult(
            strategy_used=self.shape,
            pfs_attributed_count=sum(1 for pf in product_features if pf.flows),
            total_attachments=attachments,
            per_pf_rationale=dict(per_pf_rationale),
            unattributed_flows=unattributed,
        )


# ── Registry — single source of truth ──────────────────────────────────


SHAPE_ROLLUPS: dict[str, FlowRollupStrategy] = {
    "turborepo-monorepo": TurborepoMonorepoStrategy(),
    "single-saas-routed": SingleSaasRoutedStrategy(),
    "oss-library": OssLibraryStrategy(),
    "backend-monolith": BackendMonolithStrategy(),
    "cli-tool": CliToolStrategy(),
    "framework-repo": FrameworkRepoStrategy(),
    "universal-residual": UniversalResidualStrategy(),
}


# ── Dispatcher ─────────────────────────────────────────────────────────


def stage_8_rollup_flows(
    product_features: list["Feature"],
    top_flows: list["Flow"],
    ctx: "ScanContext",
    *,
    sonnet_member_flows_map: dict[str, list[str]] | None = None,
    registry: dict[str, FlowRollupStrategy] | None = None,
) -> RollupResult:
    """Pick a strategy based on ``ctx.repo_shape`` and run it.

    Falls back to ``universal-residual`` when ``ctx.repo_shape`` is
    ``None`` or unknown. Always applies the
    :data:`MAX_FLOWS_IN_PAYLOAD` per-PF cap on exit.
    """
    reg = registry if registry is not None else SHAPE_ROLLUPS
    shape = getattr(ctx, "repo_shape", None) or _FALLBACK_KEY
    strategy = reg.get(shape) or reg[_FALLBACK_KEY]

    result = strategy.rollup(
        product_features,
        top_flows,
        ctx,
        sonnet_member_flows_map=sonnet_member_flows_map,
    )

    # Universal payload cap — same on every strategy.
    capped: list[str] = []
    for pf in product_features:
        if len(pf.flows) > MAX_FLOWS_IN_PAYLOAD:
            pf.flows[:] = pf.flows[:MAX_FLOWS_IN_PAYLOAD]
            capped.append(pf.name)
    result.diagnostics.setdefault("capped_pfs", []).extend(capped)
    result.diagnostics["shape_confidence"] = getattr(
        ctx, "shape_confidence", 0.0,
    )
    return result


def write_rollup_artifact(
    ctx: "ScanContext",
    product_features: list["Feature"],
    result: RollupResult,
) -> None:
    """Write ``08-stage-rollup.json`` artifact when ``ctx.run_dir`` is set."""
    run_dir = getattr(ctx, "run_dir", None)
    if run_dir is None:
        return
    try:
        from faultline.pipeline_v2.stage_7_output import write_stage_artifact
    except ImportError:
        return
    payload = {
        "stage": "8-flow-rollup",
        "run_id": getattr(ctx, "run_id", None),
        "shape": getattr(ctx, "repo_shape", None),
        "strategy_used": result.strategy_used,
        "stats": {
            "product_features_total": len(product_features),
            "pfs_attributed_count": result.pfs_attributed_count,
            "pfs_empty_count": len(product_features) - result.pfs_attributed_count,
            "total_attachments": result.total_attachments,
            "unattributed_flow_count": len(result.unattributed_flows),
        },
        "per_pf_rationale": {
            pf_name: [list(t) for t in rationale]
            for pf_name, rationale in result.per_pf_rationale.items()
        },
        "unattributed_flows": result.unattributed_flows,
        "diagnostics": dict(result.diagnostics),
    }
    try:
        write_stage_artifact(
            ctx.repo_path,
            stage_index=8,
            stage_name="rollup",
            payload=payload,
            run_dir=run_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("stage_8_rollup_strategies: artifact write failed: %s", exc)


__all__ = [
    "MAX_FLOWS_IN_PAYLOAD",
    "UNIVERSAL_OVERLAP_THRESHOLD",
    "RollupResult",
    "FlowRollupStrategy",
    "TurborepoMonorepoStrategy",
    "SingleSaasRoutedStrategy",
    "OssLibraryStrategy",
    "BackendMonolithStrategy",
    "CliToolStrategy",
    "FrameworkRepoStrategy",
    "UniversalResidualStrategy",
    "SHAPE_ROLLUPS",
    "stage_8_rollup_flows",
    "write_rollup_artifact",
]
