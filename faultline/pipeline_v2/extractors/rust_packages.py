"""RustModuleExtractor — intra-crate ``src/`` module structure → anchors.

``RustWorkspaceExtractor`` already maps Cargo **workspace members**
(one anchor per member crate, claiming every file under the crate dir).
But it does NOT look *inside* a crate: a single big crate — or each
member of a workspace — encodes its feature boundaries as Rust modules
under ``src/``:

  * ``src/<module>.rs``        — a flat module file.
  * ``src/<module>/mod.rs``    — a module folder (with ``mod.rs`` or
                                 sub-files).
  * ``src/bin/<name>.rs``      — an extra binary target.

None of those surface from ``rust_workspace`` (it emits ONE anchor per
crate) nor from the route extractor (Rust has no decorator-route
surface this stage parses). So for a single-crate repo (ripgrep, tokio's
core crate) or the bulky crates of a workspace (meilisearch's
``meilisearch`` crate), the entire ``src/`` tree falls to the Stage-4
LLM residual path — meilisearch sits at ~36% ``llm_fallback_pct`` today.
This extractor closes that gap with ZERO LLM cost by emitting one anchor
per first-level Rust module derived purely from the ``src/`` layout.

Crate-root detection (documented choice):
  * A "crate root" is any directory that DIRECTLY holds a ``src/``
    subdirectory containing ``lib.rs`` / ``main.rs`` / a module file.
    For a single-crate repo that's the repo root (``src/...``). For a
    workspace it's each member dir (``crates/foo/src/...``). We discover
    crate roots structurally from ``tracked_files`` — no manifest parse,
    no filesystem walk — so this works whether or not ``rust_workspace``
    fired.
  * First-level modules ONLY. ``src/store/backend/lru.rs`` folds into
    the single ``store`` anchor (recursive ownership), never its own
    ``backend`` / ``lru`` micro-anchor. This keeps precision high and
    bounds the anchor count.
  * ``lib.rs`` / ``main.rs`` / ``mod.rs`` are crate/module *roots*, not
    features — they never become their own anchor.

Slug collisions: for a single-crate repo the module name IS the slug
(``auth``). For a workspace we prefix with the crate name when a bare
module slug would collide across crates (``meilisearch-auth`` vs
``proxy-auth``) — the same pragmatic dedup ``go_packages`` uses for
top-level packages. Non-colliding modules keep their bare slug.

Noise exclusion: ``target/``, ``tests/``, ``benches/``, ``examples/`` /
``example/``, ``vendor/``, ``external-crates/`` (the convention for
vendored third-party crates in meilisearch et al.), ``third_party/``,
``.git/``, ``node_modules/`` are dropped, as
are modules whose ONLY ``.rs`` file is a ``mod.rs`` with no siblings
(an empty re-export shell — handled implicitly: a ``mod.rs``-only dir
still anchors since ``mod.rs`` is the module's body, but a directory
with zero non-root ``.rs`` files never appears).

Activation gate: only fires on Rust-shaped repos (mirrors
``rust_workspace``'s Rust check — ``ctx.stack == "rust"``, an audited
``rust*`` stack, a ``rust`` secondary, or a root ``Cargo.toml``).
Returns ``[]`` on everything else — never raises for "doesn't apply".

No LLM. No network. Pure tracked-file index walk + path arithmetic.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._util import posix, read_text, slugify
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────

# Module anchors carry medium confidence — they reflect a real Rust
# module convention but, unlike a parsed route, no in-file signal
# confirms the module is a coherent feature.
_STRUCTURAL_CONFIDENCE = 0.7

# Module-root file names that are NEVER a feature of their own. A file
# named exactly one of these is the crate/module body, not a sibling
# module.
_ROOT_MODULE_FILES = frozenset({"lib.rs", "main.rs", "mod.rs"})

# Path segments that mark a file as noise — never part of a module unit.
_EXCLUDED_SEGMENTS = frozenset({
    "target",
    "tests",
    "benches",
    "bench",
    "examples",
    "example",
    "vendor",
    "vendored",
    "external-crates",  # meilisearch et al. vendor third-party crates here
    "third_party",
    "third-party",
    ".git",
    "node_modules",
})

# The directory that holds a crate's source tree.
_SRC_DIR = "src"

# The conventional binary subdirectory inside ``src/``.
_BIN_DIR = "bin"

# Past this anchor count we very likely over-split — log it, mirroring
# go_packages.
_OVERSPLIT_WARN_THRESHOLD = 60


# ── Activation gate (mirrors rust_workspace's Rust check) ───────────────────


def _is_rust_repo(ctx: "ScanContext") -> bool:
    """``True`` if any signal indicates this repo is Rust-shaped."""
    if (ctx.stack or "").lower() == "rust":
        return True
    audited = (ctx.audited_stack or "").lower()
    if audited.startswith("rust"):
        return True
    secondaries = tuple(s.lower() for s in (ctx.secondary_stacks or ()))
    if "rust" in secondaries or any(s.startswith("rust") for s in secondaries):
        return True
    # Fall back to a manifest probe so a Rust repo Stage 0 mislabelled
    # (or left ``None``) still activates — same defensive posture as
    # rust_workspace's ``[workspace]`` presence check.
    return read_text(ctx.repo_path / "Cargo.toml") is not None


# ── Path classification ─────────────────────────────────────────────────────


def _is_excluded_path(parts: tuple[str, ...]) -> bool:
    """``True`` if any path segment marks the file as noise."""
    return any(seg in _EXCLUDED_SEGMENTS for seg in parts)


def _module_unit(parts: tuple[str, ...]) -> tuple[str, str, str] | None:
    """Classify a ``.rs`` file path into ``(crate, module, rationale)``.

    ``parts`` is the POSIX-split repo-relative path. We locate the LAST
    ``src`` segment (the crate's source root) and read the FIRST segment
    after it as the module unit. Returns ``None`` when the file is not
    inside a ``src/`` tree, is a crate/module root file directly under
    ``src/`` (``src/lib.rs``), or holds no first-level module.

    ``crate`` is the directory chain leading up to ``src`` (``""`` for a
    repo-root crate, ``"crates/foo"`` for a workspace member). It is used
    only for collision-prefixing, never for ownership.
    """
    # Find the source root. ``rindex`` so a vendored ``.../src/...``
    # inside a member resolves to the member's own src, and a path with
    # no ``src`` segment is rejected outright.
    try:
        src_idx = max(i for i, seg in enumerate(parts) if seg == _SRC_DIR)
    except ValueError:
        return None

    after = parts[src_idx + 1:]
    if not after:
        return None  # ``.../src`` itself — not a file path we expect.

    crate = "/".join(parts[:src_idx])

    # ``src/bin/<name>.rs`` → a binary target feature, keyed on <name>.
    if after[0] == _BIN_DIR and len(after) >= 2:
        leaf = after[1]
        if leaf.endswith(".rs") and leaf not in _ROOT_MODULE_FILES:
            module = leaf[:-len(".rs")]
            return crate, module, "rust src/bin binary"
        return None

    head = after[0]

    # ``src/<module>.rs`` — a flat module file.
    if len(after) == 1:
        if head in _ROOT_MODULE_FILES or not head.endswith(".rs"):
            # ``src/lib.rs`` / ``src/main.rs`` — crate root, not a module.
            return None
        module = head[:-len(".rs")]
        return crate, module, "rust src module file"

    # ``src/<module>/...`` — a module folder. First-level dir is the
    # unit; everything beneath folds into it (recursive ownership).
    return crate, head, "rust src module folder"


# ── Extractor ───────────────────────────────────────────────────────────────


class RustModuleExtractor:
    """Rust ``src/`` module parser. One anchor per first-level module.

    Implements the :class:`AnchorExtractor` Protocol. Emits structural
    anchors for the first-level modules of every crate's ``src/`` tree
    (flat ``src/<m>.rs`` files, ``src/<m>/`` folders, and
    ``src/bin/<n>.rs`` binaries). Complements
    :class:`RustWorkspaceExtractor`, which only emits crate-level
    anchors. Pure structure — no file contents read, no LLM, no network.
    """

    name = "rust-module"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not _is_rust_repo(ctx):
            return []

        # (crate, module) → set of claimed paths. We key on the pair so
        # collision detection across crates is exact; the public slug is
        # decided afterwards.
        units: dict[tuple[str, str], dict] = defaultdict(
            lambda: {"paths": set(), "rationale": ""},
        )

        for rel_path in ctx.tracked_files:
            if not rel_path.endswith(".rs"):
                continue
            norm = posix(rel_path)
            parts = tuple(norm.split("/"))
            if _is_excluded_path(parts):
                continue

            unit = _module_unit(parts)
            if unit is None:
                continue
            crate, module, rationale = unit
            if not module:
                continue

            bucket = units[(crate, module)]
            bucket["paths"].add(norm)
            bucket["rationale"] = rationale

        return self._build_anchors(units)

    def _build_anchors(
        self,
        units: dict[tuple[str, str], dict],
    ) -> list[AnchorCandidate]:
        """Resolve slugs (collision-prefixing across crates) → anchors."""
        # A bare module slug collides when ≥2 distinct crates expose the
        # same module name. Those get a ``<crate>-<module>`` slug; all
        # others keep the bare module slug.
        module_crates: dict[str, set[str]] = defaultdict(set)
        for crate, module in units:
            module_crates[module].add(crate)

        out: list[AnchorCandidate] = []
        for (crate, module), data in units.items():
            collides = len(module_crates[module]) > 1
            if collides and crate:
                crate_leaf = crate.rsplit("/", 1)[-1]
                slug = slugify(f"{crate_leaf}-{module}")
            else:
                slug = slugify(module)
            if not slug:
                continue
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=tuple(sorted(data["paths"])),
                    source=self.name,
                    confidence_self=_STRUCTURAL_CONFIDENCE,
                    rationale=data["rationale"],
                ),
            )

        if len(out) > _OVERSPLIT_WARN_THRESHOLD:
            logger.warning(
                "rust-module emitted %d anchors (>%d) — possible over-split",
                len(out),
                _OVERSPLIT_WARN_THRESHOLD,
            )
        return out


__all__ = ["RustModuleExtractor"]
