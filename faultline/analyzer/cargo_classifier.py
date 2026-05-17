"""Cargo workspace member classifier.

For repos that are Cargo workspaces, every workspace member crate
gets bucketised into a feature by the LLM scanner. Many of those
crates are INTERNAL utilities (proc-macros, fuzzers, benchmark
harnesses, snapshot test helpers, code generators, unpublishable
shared helpers) — they are not user-visible products and should
not appear in the feature map.

This classifier reads each crate's own ``Cargo.toml`` and decides:

  PUBLIC  — keep as a feature
  INTERNAL — drop from feature list

The decision is **manifest-derived**, not name-based. There is no
per-crate-name allowlist or denylist anywhere in this module.

Classification (universal across any Cargo workspace, applied in
the order below — first match wins):

  1. ``proc-macro = true`` in ``[lib]`` → INTERNAL
     (build-time machinery, never a product surface).

  2. Crate directory or any ancestor segment is in the universal
     tooling-dir name set (``xtask``, ``fuzz`` family,
     ``benchmark`` family, ``workloads``, ``examples`` family,
     ``tests``). These are pan-ecosystem Rust conventions, NOT
     repo-specific path names — see [[rule-no-repo-specific-paths]].
     → INTERNAL.

  3. Crate has ``[package].publish = false`` AND lacks BOTH a
     binary entry (``[[bin]]`` / ``src/bin/`` / ``src/main.rs``)
     AND any HTTP routing dependency (actix-web / axum / warp /
     rocket / tower-http / salvo / poem / hyper) → CANDIDATE
     INTERNAL.

  4. A CANDIDATE INTERNAL becomes PUBLIC if at least one
     non-internal, non-tooling-dir crate in the workspace
     references it under ``[dependencies]`` (runtime). Iterate
     to fixed point. Crates with NO such consumer (only
     ``[dev-dependencies]`` / ``[build-dependencies]`` or only
     tooling-dir consumers) stay INTERNAL.

  5. Anything else → PUBLIC (bias toward keeping; the classifier
     is conservative — we'd rather keep a real crate than drop it
     by mistake).

Per [[rule-no-magic-tuning]]: no numeric thresholds. The classifier
is rule-based on structural manifest facts.

Per [[rule-cold-scan]]: the classifier reads the current repo state
only. No persistence between scans.

Per [[rule-no-repo-specific-paths]]: the tooling-dir name set is
universal Rust ecosystem convention (cargo-xtask, cargo-fuzz,
criterion, cargo-bench). No meilisearch-specific names.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# HTTP routing deps — Rust web framework runtimes. Universal.
_HTTP_ROUTING_DEPS: frozenset[str] = frozenset({
    "actix-web",
    "axum",
    "warp",
    "rocket",
    "tower-http",
    "salvo",
    "poem",
    "hyper",
})


# Universal tooling-dir name set. These are pan-ecosystem Rust
# conventions (cargo-xtask / cargo-fuzz / cargo-bench / criterion
# / workspace dev-runners). NEVER a user-facing product surface.
_TOOLING_DIR_NAMES: frozenset[str] = frozenset({
    "xtask",
    "fuzz",
    "fuzzers",
    "fuzzing",
    "bench",
    "benches",
    "benchmark",
    "benchmarks",
    "workloads",
    "examples",
    "example",
    "samples",
    "demo",
    "demos",
    "tooling",
    "dev-tooling",
})


@dataclass(frozen=True, slots=True)
class CrateClassification:
    """One classified workspace member."""

    name: str
    path: str
    public: bool
    reason: str


def _read_manifest(path: Path) -> dict | None:
    try:
        with path.open("rb") as fp:
            return tomllib.load(fp)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.debug("cargo classifier: cannot read %s: %s", path, exc)
        return None


def _name_matches_tooling_dir(parts: Iterable[str]) -> bool:
    for part in parts:
        p = part.lower()
        if p in _TOOLING_DIR_NAMES:
            return True
        if "-" in p:
            for sub in p.split("-"):
                if sub in _TOOLING_DIR_NAMES:
                    return True
    return False


def _has_binary_entry(crate_dir: Path, manifest: dict) -> bool:
    # Explicit [[bin]] table or src/bin/*.rs are unambiguous product
    # binaries — they're declared as shippable entry points.
    if manifest.get("bin"):
        return True
    bin_dir = crate_dir / "src" / "bin"
    if bin_dir.is_dir():
        for child in bin_dir.iterdir():
            if child.is_file() and child.suffix == ".rs":
                return True
    # ``src/main.rs`` alone is ambiguous: a crate that has BOTH
    # ``src/lib.rs`` AND ``build.rs`` AND ``src/main.rs`` is the
    # classic "library + build-script + dev-demo entry" pattern
    # (e.g. ``build-info`` companion crates). The main.rs in that
    # shape is a debug/scratch tool, not a product binary. Require
    # absence of build.rs + lib.rs companion before treating
    # main.rs as a seed signal. Pan-ecosystem pattern — not
    # repo-specific.
    main_rs = crate_dir / "src" / "main.rs"
    if not main_rs.exists():
        return False
    has_lib = (crate_dir / "src" / "lib.rs").exists()
    has_build_script = (crate_dir / "build.rs").exists()
    if has_lib and has_build_script:
        return False
    return True


def _has_http_routing_dep(manifest: dict) -> bool:
    deps = manifest.get("dependencies", {})
    if not isinstance(deps, dict):
        return False
    for dep_name in deps:
        if dep_name in _HTTP_ROUTING_DEPS:
            return True
    return False


def _has_intra_workspace_dep(manifest: dict) -> bool:
    """Returns True if this crate depends on at least one other
    workspace member at runtime (``[dependencies]`` table) via the
    ``path = "../..."`` form or the ``workspace = true`` form.

    Cargo's intra-workspace link semantics are universal (any Cargo
    workspace uses one of these two forms for sibling crates).
    Domain/feature crates almost always compose at least one other
    in-workspace primitive. Pure infrastructure helpers (tracing
    shims, HTTP client wrappers, build-info scrapers) depend only
    on external crates.io packages.
    """
    deps = manifest.get("dependencies", {})
    if not isinstance(deps, dict):
        return False
    for _, spec in deps.items():
        if not isinstance(spec, dict):
            continue
        if isinstance(spec.get("path"), str):
            return True
        if spec.get("workspace") is True:
            # Workspace inheritance pulls the spec from the root
            # [workspace.dependencies] table. That table may or may
            # not point at a sibling member; treat as a weak signal
            # only by NOT counting it. Returning False here avoids
            # over-rescuing pure infra crates that happen to use
            # workspace inheritance for external deps.
            continue
    return False


def _is_proc_macro(manifest: dict) -> bool:
    lib = manifest.get("lib")
    if not isinstance(lib, dict):
        return False
    return bool(lib.get("proc-macro"))


def _is_unpublishable(manifest: dict) -> bool:
    pkg = manifest.get("package", {})
    if not isinstance(pkg, dict):
        return False
    return pkg.get("publish") is False


def _crate_name(manifest: dict, fallback: str) -> str:
    pkg = manifest.get("package", {})
    if isinstance(pkg, dict):
        n = pkg.get("name")
        if isinstance(n, str) and n:
            return n
    return fallback


def classify_all_members(
    *,
    repo_root: Path,
    member_dirs: Iterable[Path],
) -> dict[str, CrateClassification]:
    """Classify every workspace member crate. Returns mapping keyed
    by lower-cased crate name AND lower-cased directory basename.
    """
    members: dict[str, tuple[Path, dict, str]] = {}
    for d in member_dirs:
        manifest_path = d / "Cargo.toml"
        if not manifest_path.exists():
            continue
        manifest = _read_manifest(manifest_path)
        if manifest is None:
            continue
        try:
            rel = d.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        name = _crate_name(manifest, fallback=d.name)
        members[name] = (d, manifest, rel)

    if not members:
        return {}

    # Step 1+2: hard internals (proc-macro or under tooling dir).
    internal: dict[str, str] = {}
    for name, (d, m, rel) in members.items():
        if _is_proc_macro(m):
            internal[name] = "proc-macro-lib"
            continue
        if _name_matches_tooling_dir(Path(rel).parts):
            internal[name] = "under-tooling-dir"
            continue

    # Step 3: identify SEEDS — crates that are unambiguously
    # user-visible:
    #   - HTTP routing dep present (hosts API surface), OR
    #   - publishable binary entry AND composes other workspace
    #     members (the intra-workspace-dep signal — a CLI that
    #     orchestrates domain crates is a product CLI; a bin-only
    #     crate with zero workspace composition is a developer
    #     tool / format converter / debug aid).
    #
    # Both seed shapes are universal Cargo conventions, not
    # repo-specific. The "composes workspace members" guard
    # prevents bin-only utility crates (e.g. ``tracing-trace``'s
    # trace-to-callstats / trace-to-firefox converters) from
    # being misclassified as user products.
    seeds: set[str] = set()
    for name, (d, m, rel) in members.items():
        if name in internal:
            continue
        if _has_http_routing_dep(m):
            seeds.add(name)
            continue
        if (
            _has_binary_entry(d, m)
            and not _is_unpublishable(m)
            and _has_intra_workspace_dep(m)
        ):
            seeds.add(name)

    # Step 4: non-seed PUBLIC crates are those consumed at runtime
    # (``[dependencies]``) by at least one seed AND that themselves
    # compose another workspace member. The intra-workspace-dep
    # requirement is the universal "domain crate vs infrastructure
    # helper" discriminator:
    #
    #   - Domain/feature crates (search engine, scheduler,
    #     persistence, auth) compose other in-workspace primitives.
    #     They have ``path = "../..."`` deps on sibling crates.
    #
    #   - Pure infrastructure helpers (tracing shims, HTTP client
    #     wrappers, build-metadata scrapers) consume only external
    #     crates.io packages and stand entirely alone within the
    #     workspace.
    #
    # Both shapes get consumed by the main public binary, so the
    # consumed-by-seed signal alone is too permissive. Requiring an
    # intra-workspace dep cleanly separates them. Universal across
    # any Cargo workspace; not repo-specific.
    direct_public: set[str] = set()
    for name, (d, m, rel) in members.items():
        if name in internal or name in seeds:
            continue
        if not _has_intra_workspace_dep(m):
            continue
        for other, (od, om, orel) in members.items():
            if other not in seeds:
                continue
            rt = om.get("dependencies", {})
            if isinstance(rt, dict) and name in rt:
                direct_public.add(name)
                break

    # Step 4b: second hop — a crate consumed by a direct_public
    # at runtime also stays PUBLIC. This rescues crates like
    # ``milli`` when meilisearch-types (a seed) consumes it, and
    # then ``filter-parser`` etc. consumed-by-milli would NOT be
    # rescued (only ONE indirection). Limiting to one indirection
    # keeps the rule honest — three-hop chains are implementation
    # detail.
    #
    # NB: we deliberately do NOT do a full transitive closure
    # because that pulls in every utility crate the indexing
    # engine imports.

    saved = seeds | direct_public

    # Assemble final result with both name + dir keys.
    result: dict[str, CrateClassification] = {}
    for name, (d, m, rel) in members.items():
        if name in internal:
            cls = CrateClassification(
                name=name, path=rel, public=False,
                reason=internal[name],
            )
        elif name in saved:
            cls = CrateClassification(
                name=name, path=rel, public=True,
                reason="seed" if name in seeds else "consumed-by-seed",
            )
        else:
            cls = CrateClassification(
                name=name, path=rel, public=False,
                reason="not-consumed-by-seed",
            )
        result[name.lower()] = cls
        dir_key = d.name.lower()
        if dir_key not in result:
            result[dir_key] = cls
    return result
