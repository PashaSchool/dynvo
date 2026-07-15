"""Stage 1 — per-workspace extractor dispatch (S3).

Polyglot monorepos (NestJS backend + Next/Vite frontend + Rust WASM,
etc.) historically tripped over Stage 1: the global extractor pass
saw a top-level ``stack="js-generic"`` (or worse, "unknown") and
emitted zero deterministic anchors. Stage 4's LLM fallback then
synthesised 100% of features, blowing past the 50% warn threshold
and producing 0 deterministic flows.

This module wraps the existing Stage 1 extractors with a per-workspace
dispatcher that:

  1. Activates ONLY when the repo is a polyglot monorepo
     (audited_stack == "monorepo-polyglot" OR ctx.monorepo with ≥2
     workspaces whose ``stack`` slugs differ).
  2. For each workspace, builds a scoped ``ScanContext`` where
     ``tracked_files`` is the subset of files under that workspace's
     path, ``stack`` is the workspace's own inferred stack, and
     ``monorepo`` is False (so the extractors don't try to fan out
     further).
  3. Runs the registered extractors against each scoped context.
  4. Merges anchors across workspaces — when two workspaces emit the
     same slug, we keep them separate ONLY if their paths are fully
     disjoint AND each has ≥3 paths (signalling distinct features).
     Otherwise we coalesce, mirroring Stage 2's normal behaviour.

Workspace synthesis fallback
============================

Some polyglot repos (infisical, supabase) don't declare workspaces in
any package manager manifest — they just have ``backend/`` and
``frontend/`` directories side-by-side. Stage 0's
``detect_workspace`` returns ``detected=False`` for these. When the
auditor *does* identify the repo as polyglot, we synthesise
workspaces from a conservative set of conventional top-level directory
names (``backend``, ``frontend``, ``server``, ``client``, ``api``,
``web``, ``app``, ``apps``, ``packages``, ``services``, ``cli``,
``wasm``, ``ee``) — each one that contains a manifest file
(``package.json`` / ``Cargo.toml`` / ``pyproject.toml`` / ``go.mod``)
becomes a synthetic ``Workspace`` with its own per-package stack
detection. NO HEURISTIC NAMES — we always require a manifest as proof
the directory is actually its own package.

This module does NOT touch the global Stage 1 implementation. When it
activates the orchestrator skips the global pass; otherwise the global
pass runs as before.

No LLM calls. No network calls.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from faultline.pipeline_v2.extractors.base import (
    AnchorCandidate,
    AnchorExtractor,
)
from faultline.pipeline_v2.stage_0_intake import (
    ScanContext,
    Workspace,
    _read_json,
    detect_stack,
)
from faultline.pipeline_v2.stage_1_extractors import (
    _discover_extractors,
    _safe_extract,
    merge_profile_extractors,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Conservative whitelist of directory names we consider candidates for
# a synthetic workspace when the repo has no declared workspaces.
# ORDER doesn't matter — each candidate must additionally have a
# manifest file at its root to be promoted.
_SYNTHETIC_WORKSPACE_DIRS: tuple[str, ...] = (
    "backend",
    "frontend",
    "server",
    "client",
    "api",
    "web",
    "app",
    "apps",
    "packages",
    "services",
    "cli",
    "wasm",
    "ee",
    "core",
    "admin",
    "worker",
    "workers",
    "platform",
    "gateway",
    "sdk",
    "sdks",
)

# Manifest filenames that prove a directory is its own package.
_MANIFEST_FILENAMES: tuple[str, ...] = (
    "package.json",
    "Cargo.toml",
    "pyproject.toml",
    "go.mod",
    "Gemfile",
    "composer.json",
    "pom.xml",
    "build.gradle",
)


# ── Public data types ─────────────────────────────────────────────────────


@dataclass
class WorkspaceExtractionReport:
    """Telemetry for one workspace's per-workspace extractor pass."""

    name: str
    path: str
    inferred_stack: str | None
    extractors_fired: list[str]
    anchors_emitted: int


# ── Activation ────────────────────────────────────────────────────────────


_NOISE_STACKS = {"js-generic", "python-lib", "ruby", "unknown", ""}


def _distinct_interesting_stacks(workspaces: list[Workspace]) -> int:
    stacks = {
        (w.stack or "").lower()
        for w in workspaces
        if (w.stack or "").strip()
    }
    # Filter out the noise stacks that don't change extractor activation.
    return len(stacks - _NOISE_STACKS)


def should_activate_per_workspace(ctx: ScanContext) -> bool:
    """Return True when per-workspace dispatch should replace global Stage 1.

    Conditions (any one):
      1. Auditor flagged the repo as ``monorepo-polyglot``.
      2. ``ctx.monorepo`` is True AND ``ctx.workspaces`` contains ≥2
         workspaces with DIFFERENT non-empty stack slugs (proper
         polyglot monorepo even without auditor verdict).
      3. UNDECLARED multi-app monorepo (the infisical shape, 2026-06-12):
         no declared workspaces at all, but :func:`synthesise_workspaces`
         finds ≥2 conventional app dirs (backend/, frontend/, ...) with
         their own manifests AND ≥2 distinct non-noise stacks among them.
         Without this probe the synthesis fallback was unreachable —
         it lived inside the dispatch that this gate never enabled, so
         repos like infisical scanned with ZERO route/package anchors
         (and therefore no flows and no LOC downstream).
    """
    audited = (ctx.audited_stack or "").lower()
    if audited == "monorepo-polyglot":
        return True

    if ctx.workspaces:
        if not ctx.monorepo:
            return False
        return _distinct_interesting_stacks(list(ctx.workspaces)) >= 2

    # No declared workspaces — probe the conservative synthesis list.
    # Cheap: walks only whitelisted top-level dirs that carry manifests.
    synthetic = synthesise_workspaces(ctx)
    return len(synthetic) >= 2 and _distinct_interesting_stacks(synthetic) >= 2


# ── Workspace synthesis fallback ──────────────────────────────────────────


def _has_manifest(dir_path: Path) -> str | None:
    """Return the first manifest filename present at ``dir_path``, or None."""
    for fname in _MANIFEST_FILENAMES:
        if (dir_path / fname).is_file():
            return fname
    return None


def synthesise_workspaces(ctx: ScanContext) -> list[Workspace]:
    """Build a list of synthetic workspaces from conventional dir names.

    Walks the immediate children of ``ctx.repo_path`` whose name is in
    ``_SYNTHETIC_WORKSPACE_DIRS`` AND which contain a manifest file.
    For ``apps/`` and ``packages/`` (and ``services/``) we recurse one
    level deeper since those are container directories — each immediate
    child is treated as a workspace.

    Returns an empty list when nothing qualifies. Caller is responsible
    for deciding whether the synthesised list is preferable to the
    existing (possibly empty) ``ctx.workspaces``.
    """
    out: list[Workspace] = []
    seen_paths: set[str] = set()

    def _emit(rel_path: str, ws_root: Path) -> None:
        if rel_path in seen_paths:
            return
        seen_paths.add(rel_path)
        ws_files = [
            f for f in ctx.tracked_files
            if f == rel_path or f.startswith(rel_path + "/")
        ]
        if not ws_files:
            return
        pkg_json = _read_json(ws_root / "package.json")
        stack, _signals = detect_stack(ws_root, [
            # detect_stack expects workspace-relative paths
            f[len(rel_path) + 1:] if f.startswith(rel_path + "/") else f
            for f in ws_files
        ])
        out.append(
            Workspace(
                name=ws_root.name,
                path=rel_path,
                package_json=pkg_json,
                stack=stack,
                files=ws_files,
            ),
        )

    for child in sorted(ctx.repo_path.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name not in _SYNTHETIC_WORKSPACE_DIRS:
            continue

        # Container directories: treat each subchild as its own workspace.
        if name in ("apps", "packages", "services", "sdks", "workers"):
            for sub in sorted(child.iterdir()):
                if not sub.is_dir():
                    continue
                if _has_manifest(sub) is None:
                    continue
                rel = f"{name}/{sub.name}"
                _emit(rel, sub)
            continue

        # Direct workspace candidates.
        if _has_manifest(child) is None:
            continue
        _emit(name, child)

    return out


# ── Scoped ScanContext construction ───────────────────────────────────────


def _scoped_ctx(ctx: ScanContext, ws: Workspace) -> ScanContext:
    """Build a ScanContext narrowed to a single workspace's files.

    The scoped context tells the extractors:
      - ``stack`` is the workspace's own inferred stack
      - ``monorepo`` is True with a SINGLE-workspace list so
        existing extractors that branch on ``ctx.monorepo and
        ctx.workspaces`` (route, package) read THIS workspace's
        manifest + files, not the root.
      - ``tracked_files`` is the subset under the workspace path —
        also populated on the singleton Workspace.files so
        per-workspace extractors that iterate ctx.workspaces see
        consistent file scope.
      - ``audited_stack`` carries the workspace's own stack so
        gate-by-audited-stack extractors (rust-workspace,
        python-library, go-router) activate when appropriate.
    """
    files = list(ws.files) if ws.files else [
        f for f in ctx.tracked_files
        if f == ws.path or f.startswith(ws.path.rstrip("/") + "/")
    ]
    # Ensure workspace carries the file list for downstream extractors.
    if not ws.files:
        ws = Workspace(
            name=ws.name,
            path=ws.path,
            package_json=ws.package_json,
            stack=ws.stack,
            files=files,
        )
    return ScanContext(
        repo_path=ctx.repo_path,
        stack=ws.stack,
        monorepo=True,
        workspaces=[ws],
        tracked_files=files,
        commits=ctx.commits,
        stack_signals=[f"workspace={ws.name} stack={ws.stack}"],
        workspace_manager=ctx.workspace_manager,
        run_id=ctx.run_id,
        run_dir=ctx.run_dir,
        audited_stack=ws.stack,  # treat workspace stack as primary
        secondary_stacks=(),
        extractor_hints=(),
        auditor_confidence=None,
    )


# ── Anchor merging across workspaces ──────────────────────────────────────


def _paths_disjoint(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    return not (set(a) & set(b))


def _merge_anchors_across_workspaces(
    per_ws_results: list[tuple[str, dict[str, list[AnchorCandidate]]]],
) -> dict[str, list[AnchorCandidate]]:
    """Merge per-workspace extractor outputs into a single Stage-1 dict.

    Args:
        per_ws_results: list of ``(workspace_name, stage1_out_dict)``.

    Returns:
        A dict matching the global ``stage_1_extractors`` shape —
        keyed by extractor name (``route`` / ``mvc`` / ...) with a
        list of ``AnchorCandidate``.

    Coalescing rule per (source, slug):
      - If only one workspace emitted it → keep as-is.
      - If multiple workspaces emitted it AND paths are pairwise
        disjoint AND every emission has ≥3 paths → keep all (rename
        to ``<workspace>-<slug>`` to avoid Stage 2 collapsing them).
      - Otherwise → merge into one candidate whose paths are the union
        of all contributing paths, summing confidence (capped at 0.95).
    """
    # (source, slug) -> list[(ws_name, candidate)]
    grouped: dict[tuple[str, str], list[tuple[str, AnchorCandidate]]] = defaultdict(list)
    errors: dict[str, str] = {}

    for ws_name, stage1_out in per_ws_results:
        # "_errors" carries a dict[str, str] payload, not anchor candidates.
        ext_errs = cast("dict[str, str]", stage1_out.get("_errors") or {})
        for k, v in ext_errs.items():
            errors[f"{ws_name}:{k}"] = v
        for source, candidates in stage1_out.items():
            if source == "_errors":
                continue
            for cand in candidates:
                grouped[(source, cand.name)].append((ws_name, cand))

    merged: dict[str, list[AnchorCandidate]] = defaultdict(list)

    # B67 twin-slug routes preservation (flag-gated for OFF byte-identity).
    # Both rebuild paths below construct NEW AnchorCandidates and historically
    # DROPPED the explicit ``routes`` (and ``route_groups``) fields — any
    # same-(source, slug) candidates that coalesce lose their routes_index
    # rows silently. twenty's B67 forensics: every cron has a same-slug job
    # twin (crons/jobs/X.cron.job.ts + jobs/X.job.ts -> two 1-path candidates
    # -> coalesce -> routes gone; 22 of 27 dropped rows). The same hole eats
    # any DSL-routed extractor's twins on monorepos (FastAPI/Express) — but
    # restoring them un-gated would change OFF-world boards, so the general
    # cleanup rides the B67 flip; until then preservation is armed only by
    # FAULTLINE_JOBS_ENTRIES.
    from faultline.pipeline_v2.extractors.jobs_entries import (
        jobs_entries_enabled,
    )
    from faultline.pipeline_v2.extractors.server_api_entries import (
        server_api_entries_enabled,
    )
    # B66 rides the same armed path: NestJS/tRPC/GraphQL entries are heavily
    # monorepo (twenty packages/twenty-server, cal apps/api/v2), so same-slug
    # twins that coalesce here would otherwise LOSE their explicit routes before
    # they reach routes_index. Armed by EITHER flag; OFF-world (both unset) is
    # byte-identical.
    preserve_routes = jobs_entries_enabled() or server_api_entries_enabled()

    def _routes_union(
        cands: list[AnchorCandidate],
    ) -> tuple[tuple[str, str, str], ...]:
        out: list[tuple[str, str, str]] = []
        seen_r: set[tuple[str, str, str]] = set()
        for c in cands:
            for r in (c.routes or ()):
                if r not in seen_r:
                    seen_r.add(r)
                    out.append(r)
        return tuple(out)

    for (source, slug), items in grouped.items():
        if len(items) == 1:
            merged[source].append(items[0][1])
            continue

        # Check disjoint + chunky enough to keep separate.
        paths_lists = [c.paths for _, c in items]
        all_pairwise_disjoint = all(
            _paths_disjoint(paths_lists[i], paths_lists[j])
            for i in range(len(paths_lists))
            for j in range(i + 1, len(paths_lists))
        )
        all_chunky = all(len(p) >= 3 for p in paths_lists)

        if all_pairwise_disjoint and all_chunky:
            for ws_name, cand in items:
                renamed = AnchorCandidate(
                    name=f"{ws_name}-{cand.name}",
                    paths=cand.paths,
                    source=cand.source,
                    confidence_self=cand.confidence_self,
                    display_name=cand.display_name,
                    rationale=(
                        f"{cand.rationale} [workspace={ws_name}]"
                    ),
                    routes=cand.routes if preserve_routes else (),
                    route_groups=(
                        cand.route_groups if preserve_routes else ()
                    ),
                )
                merged[source].append(renamed)
        else:
            # Coalesce — union paths, average-cap confidence.
            paths_union: list[str] = []
            seen: set[str] = set()
            for c in (c for _, c in items):
                for p in c.paths:
                    if p not in seen:
                        seen.add(p)
                        paths_union.append(p)
            conf = min(
                max((c.confidence_self for _, c in items), default=0.5) + 0.05,
                0.95,
            )
            contributing_wss = sorted({ws for ws, _ in items})
            merged[source].append(
                AnchorCandidate(
                    name=slug,
                    paths=tuple(paths_union),
                    source=source,
                    confidence_self=conf,
                    rationale=(
                        f"per-workspace merged from "
                        f"workspaces={contributing_wss}"
                    ),
                    routes=(
                        _routes_union([c for _, c in items])
                        if preserve_routes else ()
                    ),
                ),
            )

    # Ensure all source keys appear (even if empty) for telemetry.
    for ws_name, stage1_out in per_ws_results:
        for source in stage1_out.keys():
            if source == "_errors":
                continue
            merged.setdefault(source, [])

    out: dict[str, list[AnchorCandidate]] = dict(merged)
    if errors:
        out["_errors"] = errors  # type: ignore[assignment]
    return out


# ── Public orchestration entry point ──────────────────────────────────────


@dataclass
class PerWorkspaceResult:
    stage1_out: dict[str, list[AnchorCandidate]]
    workspaces_processed: list[WorkspaceExtractionReport]
    workspaces_used: list[Workspace]
    synthesised_workspaces: bool
    leftover_files_scanned: int = 0


def run_stage_1_per_workspace(
    ctx: ScanContext,
    extractors: list[AnchorExtractor] | None = None,
    *,
    profile: object | None = None,
) -> PerWorkspaceResult:
    """Run Stage 1 extractors per workspace and merge.

    Args:
        ctx: Stage 0 output (post-auditor).
        extractors: optional explicit registry. ``None`` → discover.
        profile: the ACTIVE framework profile — its optional Stage-1
            extractor overrides are merged into the registry exactly as
            in the global pass (:func:`merge_profile_extractors`); a
            ``None`` / DefaultProfile is a strict no-op.

    Returns:
        ``PerWorkspaceResult`` containing the merged Stage-1 dict
        (matches ``stage_1_extractors`` shape) plus per-workspace
        telemetry. Caller decides whether to use ``stage1_out`` or
        ignore the per-workspace pass entirely (e.g. if no workspaces
        materialised).

    Raises nothing — failure of an individual extractor on one
    workspace is captured in ``stage1_out["_errors"]`` (namespaced
    ``<workspace>:<extractor>``) and does not kill the pass.
    """
    if extractors is None:
        extractors = _discover_extractors()
    extractors = merge_profile_extractors(extractors, profile, ctx)

    # Source the workspace list — declared first, synthesised second.
    workspaces = list(ctx.workspaces or [])
    synthesised = False
    if not workspaces:
        workspaces = synthesise_workspaces(ctx)
        synthesised = bool(workspaces)

    if not workspaces:
        # No workspaces at all — return an empty result so caller can
        # fall back to global Stage 1.
        return PerWorkspaceResult(
            stage1_out={},
            workspaces_processed=[],
            workspaces_used=[],
            synthesised_workspaces=False,
        )

    # Compute LEFTOVER files — tracked_files not under any workspace.
    # These need a separate pass so we don't lose anchors for code
    # that lives outside the declared workspace tree (e.g.
    # ``packages/db/`` when the workspace list only enumerated
    # ``apps/*`` because pnpm-workspace.yaml's ``packages/**/*`` glob
    # wasn't fully expanded by Stage 0).
    ws_prefixes = tuple(
        (w.path.rstrip("/") + "/") for w in workspaces if w.path
    )
    leftover_files = [
        f for f in ctx.tracked_files
        if not any(f == p[:-1] or f.startswith(p) for p in ws_prefixes)
    ]

    per_ws_results: list[tuple[str, dict[str, list[AnchorCandidate]]]] = []
    reports: list[WorkspaceExtractionReport] = []

    for ws in workspaces:
        scoped = _scoped_ctx(ctx, ws)
        if not scoped.tracked_files:
            reports.append(
                WorkspaceExtractionReport(
                    name=ws.name,
                    path=ws.path,
                    inferred_stack=ws.stack,
                    extractors_fired=[],
                    anchors_emitted=0,
                ),
            )
            continue

        ws_out: dict[str, list[AnchorCandidate]] = {}
        errors: dict[str, str] = {}
        fired: list[str] = []
        total_anchors = 0
        for ext in extractors:
            name, candidates, error = _safe_extract(ext, scoped)
            if error is not None:
                errors[name] = error
                ws_out[name] = []
                continue
            assert candidates is not None
            ws_out[name] = candidates
            if candidates:
                fired.append(name)
                total_anchors += len(candidates)
        if errors:
            ws_out["_errors"] = errors  # type: ignore[assignment]

        per_ws_results.append((ws.name, ws_out))
        reports.append(
            WorkspaceExtractionReport(
                name=ws.name,
                path=ws.path,
                inferred_stack=ws.stack,
                extractors_fired=fired,
                anchors_emitted=total_anchors,
            ),
        )

    # ── LEFTOVER pass: files outside any workspace ──
    # Run extractors against a ScanContext whose tracked_files is the
    # leftover set, with ``stack`` cleared and ``monorepo=False``.
    # We treat the leftover scope as a synthetic ``__leftover__``
    # workspace so its emissions go through the same merge logic
    # (potential name collisions with workspace-emitted anchors are
    # coalesced).
    if leftover_files:
        leftover_ctx = ScanContext(
            repo_path=ctx.repo_path,
            stack=ctx.stack,
            monorepo=False,
            workspaces=None,
            tracked_files=leftover_files,
            commits=ctx.commits,
            stack_signals=["leftover scope"],
            workspace_manager=None,
            run_id=ctx.run_id,
            run_dir=ctx.run_dir,
            audited_stack=None,
            secondary_stacks=(),
            extractor_hints=(),
            auditor_confidence=None,
        )
        leftover_out: dict[str, list[AnchorCandidate]] = {}
        leftover_errors: dict[str, str] = {}
        leftover_fired: list[str] = []
        leftover_total = 0
        for ext in extractors:
            name, candidates, error = _safe_extract(ext, leftover_ctx)
            if error is not None:
                leftover_errors[name] = error
                leftover_out[name] = []
                continue
            assert candidates is not None
            leftover_out[name] = candidates
            if candidates:
                leftover_fired.append(name)
                leftover_total += len(candidates)
        if leftover_errors:
            leftover_out["_errors"] = leftover_errors  # type: ignore[assignment]
        per_ws_results.append(("__leftover__", leftover_out))
        reports.append(
            WorkspaceExtractionReport(
                name="__leftover__",
                path="(uncovered files)",
                inferred_stack=None,
                extractors_fired=leftover_fired,
                anchors_emitted=leftover_total,
            ),
        )

    merged = _merge_anchors_across_workspaces(per_ws_results)

    return PerWorkspaceResult(
        stage1_out=merged,
        workspaces_processed=reports,
        workspaces_used=workspaces,
        synthesised_workspaces=synthesised,
        leftover_files_scanned=len(leftover_files),
    )


__all__ = [
    "PerWorkspaceResult",
    "WorkspaceExtractionReport",
    "run_stage_1_per_workspace",
    "should_activate_per_workspace",
    "synthesise_workspaces",
]
