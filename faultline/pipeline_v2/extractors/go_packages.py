"""GoPackageExtractor — Go directory conventions → structural anchors.

The Go ecosystem encodes feature boundaries in the directory layout,
not in decorators or manifests. The de-facto "standard project layout"
gives us a deterministic map of the codebase:

  * ``cmd/<name>/...``      — one binary / entry-point per first-level dir.
  * ``internal/<name>/...`` — private application/library packages.
  * ``pkg/<name>/...``      — public library packages.
  * ``modules/<name>/...``  — plugin-style module groupings (caddy).
  * ``apps/<name>/...``     — app groupings in some monorepos.
  * top-level packages      — any repo-root directory that DIRECTLY
                              holds ≥1 non-test ``.go`` file and isn't
                              already covered above.

``GoRouterExtractor`` only sees the HTTP-route surface, so the bulk of a
Go repo (``cmd/``, ``internal/``, ``pkg/``, top-level packages) falls to
the Stage-4 LLM residual path — pushing Go ``llm_fallback_pct`` to ~50%.
This extractor closes that gap with ZERO LLM cost by emitting one anchor
per Go "feature unit" derived purely from directory structure.

Path-ownership rule (documented choice):
  * For the convention prefixes (``cmd``/``internal``/``pkg``/
    ``modules``/``apps``) we group at the FIRST level under the prefix
    and claim every ``.go`` file RECURSIVELY beneath that first-level
    dir. This keeps deep package trees (e.g. ``internal/auth/oauth/...``)
    coherent under a single ``auth`` anchor rather than exploding into
    micro-anchors.
  * For top-level repo-root packages we claim only the directory's OWN
    ``.go`` files (non-recursive). A repo-root dir is a Go package in
    its own right; its subdirectories are independent packages that, if
    they matter, surface as their own top-level/convention anchors. This
    bounds the anchor count and keeps each anchor a single coherent
    package.

Noise exclusion: ``vendor/``, ``testdata/``, ``examples/`` / ``example/``,
``docs/``, ``.git/``, ``third_party/``, ``node_modules/`` are dropped, as
are directories whose ONLY ``.go`` files are ``*_test.go`` (test-only) or
generated (``*.pb.go`` / ``*_gen.go``).

Activation gate: only fires on Go-shaped repos (same check as
``go_router``). Returns ``[]`` on everything else — never raises for the
"doesn't apply" case.

No LLM. No network. Pure file-system index walk + path arithmetic.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._util import is_any_stack, posix, slugify
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────

# Structural anchors carry medium confidence — they reflect a real Go
# convention but, unlike a parsed route, no in-file signal confirms the
# directory is a coherent feature.
_STRUCTURAL_CONFIDENCE = 0.7

# Directory-name prefixes that group at their FIRST level. Order matters
# only for the rationale label; ownership is unambiguous (a file lives
# under exactly one prefix).
_CONVENTION_PREFIXES: tuple[tuple[str, str], ...] = (
    ("cmd", "go cmd/ binary"),
    ("internal", "go internal package"),
    ("pkg", "go pkg/ package"),
    ("modules", "go module group"),
    ("apps", "go app group"),
)

# Path segments that mark a directory as noise — never a feature.
_EXCLUDED_SEGMENTS = frozenset({
    "vendor",
    "testdata",
    "examples",
    "example",
    "docs",
    ".git",
    "third_party",
    "node_modules",
})


# ── Activation gate (mirrors go_router._is_go_repo) ─────────────────────────


def _is_go_repo(ctx: "ScanContext") -> bool:
    """``True`` if any signal indicates this repo is Go-shaped."""
    if is_any_stack(ctx, "go"):
        return True
    if (ctx.audited_stack or "").lower().startswith("go-"):
        return True
    secondaries = tuple(s.lower() for s in (ctx.secondary_stacks or ()))
    return any(s.startswith("go-") for s in secondaries)


# ── Path classification ─────────────────────────────────────────────────────


def _is_excluded_path(parts: tuple[str, ...]) -> bool:
    """``True`` if any path segment marks the file as noise."""
    return any(seg in _EXCLUDED_SEGMENTS for seg in parts)


def _is_test_file(leaf: str) -> bool:
    """``True`` for Go test files (``*_test.go``)."""
    return leaf.endswith("_test.go")


def _is_generated_file(leaf: str) -> bool:
    """``True`` for common generated Go files (protobuf / codegen)."""
    return leaf.endswith(".pb.go") or leaf.endswith("_gen.go")


def _bucket_key(parts: tuple[str, ...]) -> tuple[str, str, str] | None:
    """Classify a ``.go`` file path into ``(slug, leaf_name, rationale)``.

    ``parts`` is the POSIX-split repo-relative path (``("internal",
    "auth", "oauth", "x.go")``). Returns ``None`` when the path holds no
    coherent package directory (e.g. a bare repo-root ``.go`` file, which
    is handled by the caller's root bucket rule — see below).
    """
    if len(parts) < 2:
        # Repo-root ``.go`` file — no directory. Handled separately so it
        # never silently vanishes; the caller maps it to ``root``.
        return None

    head = parts[0]
    for prefix, rationale in _CONVENTION_PREFIXES:
        if head == prefix and len(parts) >= 3:
            # cmd/<name>/.../file.go → group on <name>, recursive.
            leaf = parts[1]
            return slugify(leaf), leaf, rationale

    # Top-level repo-root package: own files only (non-recursive). Such a
    # file has exactly two parts: ("<dir>", "file.go").
    if len(parts) == 2:
        leaf = parts[0]
        return slugify(leaf), leaf, "go top-level package"

    # Deeper file under a non-convention top-level dir — owned by the
    # nested package, which surfaces (if at all) via its own top-level
    # entry. We intentionally do NOT claim it to keep anchors coherent.
    return None


# ── Extractor ───────────────────────────────────────────────────────────────


class GoPackageExtractor:
    """Go directory-convention parser. One anchor per Go feature unit.

    Implements the :class:`AnchorExtractor` Protocol. Emits structural
    anchors for ``cmd/``, ``internal/``, ``pkg/``, ``modules/``,
    ``apps/`` first-level dirs plus top-level repo-root packages. Pure
    structure — no file contents are read, no LLM, no network.
    """

    name = "go-package"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not _is_go_repo(ctx):
            return []

        # slug → {"paths": set, "leaf": str, "rationale": str, "real": bool}
        # ``real`` flips True once we see a non-test, non-generated file so
        # test-only / codegen-only dirs are dropped at the end.
        buckets: dict[str, dict] = defaultdict(
            lambda: {"paths": set(), "leaf": "", "rationale": "", "real": False},
        )

        for rel_path in ctx.tracked_files:
            if not rel_path.endswith(".go"):
                continue
            norm = posix(rel_path)
            parts = tuple(norm.split("/"))
            if _is_excluded_path(parts):
                continue

            key = _bucket_key(parts)
            if key is None:
                continue
            slug, leaf, rationale = key
            if not slug:
                continue

            bucket = buckets[slug]
            bucket["paths"].add(norm)
            bucket["leaf"] = leaf
            bucket["rationale"] = rationale
            leaf_name = parts[-1]
            if not _is_test_file(leaf_name) and not _is_generated_file(leaf_name):
                bucket["real"] = True

        out: list[AnchorCandidate] = []
        for slug, data in buckets.items():
            if not data["real"]:
                # Directory whose only .go files are tests or generated —
                # never a feature.
                continue
            paths = tuple(sorted(data["paths"]))
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=paths,
                    source=self.name,
                    confidence_self=_STRUCTURAL_CONFIDENCE,
                    rationale=data["rationale"],
                ),
            )

        if len(out) > 60:
            logger.warning(
                "go-package emitted %d anchors (>60) — possible over-split",
                len(out),
            )
        return out


__all__ = ["GoPackageExtractor"]
