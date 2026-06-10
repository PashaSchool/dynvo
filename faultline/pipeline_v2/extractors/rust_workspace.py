"""RustWorkspaceExtractor — Cargo workspace members → anchors.

Reads the root ``Cargo.toml``'s ``[workspace]`` table and emits one
:class:`AnchorCandidate` per member crate. Each anchor claims every
source file under that crate's directory; Stage 2 reconciliation
handles overlap with other extractors using the standard priority.

This is the Cargo equivalent of ``PackageAnchorExtractor`` for the
specific case of multi-crate workspaces (meilisearch, libra, deno,
many large Rust OSS projects). It does NOT fire on single-crate Rust
projects — those should be handled by a future ``CargoCrateExtractor``
that reads ``[package]`` directly.

Member entries can be:
  - Explicit paths: ``"crates/meilisearch"``
  - Globs: ``"crates/*"``, ``"external-crates/*"``

Globs are expanded against ``ctx.tracked_files`` (no extra filesystem
walking — Stage 0 already enumerated). For each resolved member dir
we read ``<member_dir>/Cargo.toml`` to pick up ``[package].name``;
when that file is missing or unparseable we fall back to the
directory basename, which is conventional in Cargo workspaces.

No LLM. No network. ``tomllib`` (Python 3.11+ stdlib) does the parse.
"""

from __future__ import annotations

import fnmatch
import logging
import tomllib
from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from pathlib import Path

    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


def _load_config() -> dict:
    """Load rust-workspace.yaml from the packaged data tree (hermetic)."""
    return load_stack_yaml("rust-workspace")


# ── Activation gate ────────────────────────────────────────────────────────


def _is_rust_workspace(ctx: "ScanContext", root_manifest: Path) -> bool:
    """``True`` if Stage 0.5 labelled this rust-workspace OR Stage 0
    saw Rust AND the root Cargo.toml contains a [workspace] section.

    The [workspace] presence check guards single-crate Rust libs from
    accidentally emitting a single full-repo anchor.
    """
    if (ctx.audited_stack or "").lower() == "rust-workspace":
        return True
    secondaries = tuple(s.lower() for s in (ctx.secondary_stacks or ()))
    if "rust-workspace" in secondaries:
        return True
    is_rust = (
        (ctx.stack or "").lower() == "rust"
        or "rust" in secondaries
        or (ctx.audited_stack or "").lower().startswith("rust")
    )
    if not is_rust:
        return False
    text = read_text(root_manifest)
    if not text:
        return False
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    return isinstance(data.get("workspace"), dict)


# ── Member resolution ──────────────────────────────────────────────────────


def _resolve_members(
    raw_members: list[str],
    repo_root: Path,
    tracked_files: list[str],
) -> list[str]:
    """Expand workspace member entries into a list of repo-relative dirs.

    Glob entries (``crates/*``) are expanded against directories
    inferred from ``tracked_files`` — we never walk the filesystem.
    """
    # Build a sorted set of all directories that contain at least one
    # tracked file. We use this for glob expansion.
    dirs: set[str] = set()
    for f in tracked_files:
        p = posix(f)
        parts = p.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))

    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_members:
        if not isinstance(raw, str) or not raw.strip():
            continue
        entry = raw.strip().rstrip("/")
        if any(c in entry for c in "*?["):
            # Glob — match against discovered dirs. ``fnmatch`` does
            # not treat ``/`` specially, so we constrain matches to
            # the same path-depth as the glob (``crates/*`` must not
            # match ``crates/alpha/src``).
            target_depth = entry.count("/")
            for d in sorted(dirs):
                if d.count("/") != target_depth:
                    continue
                if fnmatch.fnmatch(d, entry):
                    if d not in seen:
                        seen.add(d)
                        out.append(d)
        else:
            if entry not in seen:
                seen.add(entry)
                out.append(entry)
    return out


def _crate_name(member_dir: str, repo_root: Path) -> str:
    """Read ``[package].name`` from member Cargo.toml.

    Falls back to the directory basename when the manifest is missing
    or unparseable — this matches the Cargo convention.
    """
    manifest = repo_root / member_dir / "Cargo.toml"
    text = read_text(manifest)
    if text:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            data = {}
        pkg = data.get("package") if isinstance(data, dict) else None
        if isinstance(pkg, dict):
            name = pkg.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    # Fallback: directory basename.
    return member_dir.rsplit("/", 1)[-1] or member_dir


# ── Extractor ──────────────────────────────────────────────────────────────


class RustWorkspaceExtractor:
    """Cargo workspace member → anchor."""

    name = "rust-workspace"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config if config is not None else _load_config()

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        root_manifest = ctx.repo_path / (
            self._config.get("root_manifest") or "Cargo.toml"
        )
        if not _is_rust_workspace(ctx, root_manifest):
            return []

        text = read_text(root_manifest)
        if not text:
            return []
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            logger.warning("rust-workspace: failed to parse %s: %s", root_manifest, exc)
            return []

        workspace = data.get("workspace") if isinstance(data, dict) else None
        if not isinstance(workspace, dict):
            return []
        raw_members = workspace.get("members") or []
        if not isinstance(raw_members, list):
            return []

        members = _resolve_members(
            [m for m in raw_members if isinstance(m, str)],
            ctx.repo_path,
            ctx.tracked_files,
        )
        if not members:
            return []

        # Pre-index tracked_files by directory for O(M+F) path lookup.
        by_dir: dict[str, list[str]] = defaultdict(list)
        suffixes = tuple(
            s for s in (self._config.get("source_file_suffixes") or [".rs", ".toml"])
            if isinstance(s, str)
        )
        excludes = tuple(
            e for e in (self._config.get("excludes") or [])
            if isinstance(e, str)
        )
        for f in ctx.tracked_files:
            p = posix(f)
            if not any(p.endswith(s) for s in suffixes):
                continue
            # Find the deepest directory this file lives in (registered
            # for lookup by ancestor membership).
            parts = p.split("/")
            for i in range(1, len(parts)):
                by_dir["/".join(parts[:i])].append(p)

        confidence = float(
            (self._config.get("confidence") or {}).get("manifest_member", 0.95),
        )

        out: list[AnchorCandidate] = []
        for member_dir in members:
            # Collect files under this member dir, respecting excludes.
            claimed: list[str] = []
            for p in by_dir.get(member_dir, []):
                if any(
                    f"/{ex}" in f"/{p}" or p.startswith(ex)
                    for ex in excludes
                ):
                    continue
                claimed.append(p)
            crate_name = _crate_name(member_dir, ctx.repo_path)
            slug = slugify(crate_name)
            if not slug:
                continue
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=tuple(sorted(set(claimed))),
                    source=self.name,
                    confidence_self=confidence,
                    rationale=(
                        f"cargo workspace member {crate_name!r} "
                        f"at {member_dir!r}"
                    ),
                ),
            )
        return out


__all__ = ["RustWorkspaceExtractor"]
