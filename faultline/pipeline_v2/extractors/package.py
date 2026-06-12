"""PackageAnchorExtractor — dependency manifest → feature anchor.

Two emission modes (both deterministic, no LLM):

1) **Dependency-category anchors** (the historical mode). Certain
   dependencies are strong, near-binary signals for product capability:

   - ``stripe`` / ``@stripe/...``     → ``billing``
   - ``next-auth``, ``@auth/core``,
     ``better-auth``, ``lucia``       → ``auth``
   - ``resend``, ``@sendgrid/mail``,
     ``postmark``, ``nodemailer``     → ``email``
   - ``inngest``, ``bullmq``,
     ``trigger.dev``                  → ``background-jobs``
   - ``@uploadthing/react``,
     ``@aws-sdk/client-s3``           → ``file-uploads``
   - ``posthog-node``, ``mixpanel``,
     ``segment``                      → ``analytics``
   - ``socket.io``, ``ws``, ``pusher`` → ``realtime``
   - ``openai``, ``anthropic``,
     ``@google/generative-ai``, ``ai`` (Vercel) → ``ai``
   - ``next-i18next``, ``i18next``    → ``i18n``

   The token tables live in the ``stage1_anchors`` section of
   ``eval/dependency-anchors.yaml`` (runtime copy packaged at
   ``faultline/pipeline_v2/data/``) — per the hard rule, dep-anchor
   tables live in YAML, never hardcoded in Python.

2) **Workspace-package anchors** (Sprint D3, 2026-05-20). Each
   declared monorepo workspace IS its own feature surface — the
   maintainer literally created a package boundary around it. We
   emit one anchor per ``ctx.workspaces[*]`` whose slug is the
   ``package.json#name`` last segment (scope stripped) or the
   workspace directory name as fallback. The anchor's ``paths`` are
   the workspace's full file list, so Stage 2's path attribution
   binds every file inside the workspace to the feature.

   This emission is what made generic-named packages
   (``packages/ui``, ``packages/types``, ``packages/utils``,
   ``packages/services``, ``packages/logger``, etc.) silently
   disappear from Stage 1 output across every monorepo we scanned —
   none of them imported a ``stripe``-class dependency, so mode 1
   produced zero anchors and the workspace boundary itself was
   never recorded.

   Workspace anchors do NOT depend on the dependency-token table.
   They are universal across stacks: any manifest-declared workspace
   (``package.json``, ``Cargo.toml``, ``pyproject.toml``,
   ``go.mod``, ``Gemfile``, ``composer.json``) qualifies.

For monorepos we read the per-workspace ``package.json`` (already in
``ctx.workspaces[*].package_json``) plus the root manifest.

The ``paths`` list for each anchor is the workspace directory the
dependency lives in (not the file list — that would require
content-grep). Stage 2 reconciliation uses these paths as a hint when
attributing files to features, but other extractors (route, mvc,
schema) typically claim files more concretely.

No LLM. No network. Pure manifest parsing.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_yaml
from faultline.pipeline_v2.extractors._util import (
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace


# (matcher_token, anchor_slug) pairs. We use token-prefix matching
# rather than a flat dict so we can match families like ``@stripe/*``
# without enumerating every package.

_DepMatch = tuple[str, str]
"""(matcher_token, anchor_slug). matcher_token is matched in two ways:

   - exact equality with the dep name
   - dep name starts with ``matcher_token + "/"`` (covers @scope/* families)
   - dep name starts with ``matcher_token + "-"`` (covers ``next-auth-*``)
"""


def _load_stage1_anchors(lang: str) -> tuple[_DepMatch, ...]:
    """Read the Stage-1 anchor token table from dependency-anchors.yaml.

    The ``stage1_anchors`` section is the verbatim externalization of
    the historical in-Python tables. Order is preserved from the YAML
    list — ``_match_anchors`` stops at the first token that matches a
    dep, so list order carries first-match semantics.

    Hermetic: resolves via ``importlib.resources`` (see
    ``faultline.pipeline_v2.data``). A missing data file raises — a
    packaging bug, never a silently-tolerated condition.
    """
    section = load_yaml("dependency-anchors.yaml").get("stage1_anchors") or {}
    entries = section.get(lang) if isinstance(section, dict) else None
    out: list[_DepMatch] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        token = entry.get("token")
        slug = entry.get("slug")
        if isinstance(token, str) and isinstance(slug, str) and token and slug:
            out.append((token, slug))
    return tuple(out)


_JS_DEP_ANCHORS: tuple[_DepMatch, ...] = _load_stage1_anchors("js")
_PY_DEP_ANCHORS: tuple[_DepMatch, ...] = _load_stage1_anchors("python")


def _dep_matches(dep: str, token: str) -> bool:
    """``True`` if dependency ``dep`` is matched by ``token``."""
    if dep == token:
        return True
    if dep.startswith(token + "/"):
        return True
    if dep.startswith(token + "-"):
        return True
    return False


def _collect_js_deps(pkg: dict | None) -> set[str]:
    """Union of dependencies, devDependencies, peerDependencies names."""
    if not pkg or not isinstance(pkg, dict):
        return set()
    out: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = pkg.get(key)
        if isinstance(block, dict):
            out.update(str(k) for k in block.keys())
    return out


def _collect_py_deps(pyproject_text: str | None) -> set[str]:
    """Extract dep names from a pyproject.toml-ish text blob.

    We don't import ``tomllib`` here to avoid a hard dep on it for
    callers stuck on Python <3.11 (the project targets 3.11+ but
    keeping the manifest parsing tolerant costs nothing). Substring
    detection is sufficient since the matcher set is small and the
    names are distinctive.
    """
    if not pyproject_text:
        return set()
    # Pull anything that looks like ``"name"`` or ``name = "..."`` —
    # this is intentionally permissive; false positives cost ≤ 1 anchor.
    found: set[str] = set()
    lowered = pyproject_text.lower()
    for tok, _slug in _PY_DEP_ANCHORS:
        if tok.lower() in lowered:
            found.add(tok)
    return found


def _match_anchors(
    deps: set[str],
    matchers: tuple[_DepMatch, ...],
) -> dict[str, list[str]]:
    """For each anchor matcher firing, group the dep names that fired.

    Iterates ``deps`` SORTED: dep names arrive as a set, and the
    insertion order of ``hits`` flows all the way into the emitted
    ``developer_features[]`` order (Stage 2 preserves candidate order;
    Stage 6.3's per-feature caps are order-sensitive). Unsorted
    iteration made identical scans differ across runs via
    PYTHONHASHSEED.
    """
    hits: dict[str, list[str]] = defaultdict(list)
    for dep in sorted(deps):
        for token, slug in matchers:
            if _dep_matches(dep, token):
                hits[slug].append(dep)
                break  # one anchor per dep is enough
    return hits


# ── Sprint D3 — workspace package anchor helpers ──────────────────────────


def _slug_from_package_name(pkg_name: object) -> str | None:
    """Return the slug derived from a ``package.json#name`` value.

    Handles scoped names like ``@plane/ui`` → ``ui`` (last path
    segment after stripping the ``@scope/`` prefix). Returns ``None``
    for missing / empty / non-string values.
    """
    if not isinstance(pkg_name, str):
        return None
    raw = pkg_name.strip()
    if not raw:
        return None
    # Strip a single leading scope token, e.g. ``@plane/ui`` →
    # ``ui``. Multi-segment names without scopes (very rare for
    # JS packages) keep their tail segment.
    if raw.startswith("@") and "/" in raw:
        raw = raw.split("/", 1)[1]
    # If the value still contains a slash (e.g. weird custom Cargo
    # convention), take the final segment.
    if "/" in raw:
        raw = raw.rsplit("/", 1)[1]
    slug = slugify(raw)
    return slug or None


def _slug_from_workspace_path(path: str) -> str | None:
    """Fallback slug source — last segment of the workspace path.

    ``packages/ui`` → ``ui``. ``apps/web`` → ``web``. The result is
    slugified to match the kebab-case convention.
    """
    if not path:
        return None
    tail = posix(path).rstrip("/").rsplit("/", 1)[-1]
    slug = slugify(tail)
    return slug or None


def _workspace_slug(ws: "Workspace") -> str | None:
    """Pick the best slug for a workspace anchor.

    Priority:
      1. ``package.json#name`` last segment — the maintainer's
         explicit name for the package (most stable signal).
      2. Workspace directory name — present on every workspace.

    Returns ``None`` only when both produce empty strings, which
    in practice means the workspace has no manifest AND its path
    is empty / root — never happens for declared workspaces.
    """
    pkg = ws.package_json
    if isinstance(pkg, dict):
        from_name = _slug_from_package_name(pkg.get("name"))
        if from_name:
            return from_name
    return _slug_from_workspace_path(ws.path)


class PackageAnchorExtractor:
    """Dependency manifest → feature-category anchor."""

    name = "package"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        # ``slug → {paths_set, contributing_deps_set}``
        # We coalesce across all workspaces because a "billing" anchor
        # is fundamentally one feature regardless of which workspace
        # imports stripe.
        anchors: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: {"paths": set(), "deps": set()},
        )

        # ── JS / TS manifests ──
        def _process_js(pkg: dict | None, scope_path: str) -> None:
            deps = _collect_js_deps(pkg)
            if not deps:
                return
            for slug, contributing in _match_anchors(deps, _JS_DEP_ANCHORS).items():
                anchors[slug]["paths"].add(posix(scope_path) or ".")
                anchors[slug]["deps"].update(contributing)

        if ctx.monorepo and ctx.workspaces:
            for ws in ctx.workspaces:
                _process_js(ws.package_json, ws.path)
            # Also consider the root package.json for monorepo-level deps.
            root_pkg = _read_json(ctx.repo_path / "package.json")
            if root_pkg is not None:
                _process_js(root_pkg, ".")
        else:
            root_pkg = _read_json(ctx.repo_path / "package.json")
            if root_pkg is not None:
                _process_js(root_pkg, ".")

        # ── Python pyproject.toml ──
        pyproject_text = read_text(ctx.repo_path / "pyproject.toml")
        if pyproject_text:
            py_deps = _collect_py_deps(pyproject_text)
            for slug, contributing in _match_anchors(py_deps, _PY_DEP_ANCHORS).items():
                anchors[slug]["paths"].add(".")
                anchors[slug]["deps"].update(contributing)

        out: list[AnchorCandidate] = []
        for slug, data in anchors.items():
            paths = tuple(sorted(data["paths"]))
            deps_list = sorted(data["deps"])
            # Package anchors are very high precision (a stripe import
            # is rarely an accident) so baseline confidence is high.
            confidence = min(0.8 + 0.03 * len(deps_list), 0.95)
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=paths,
                    source=self.name,
                    confidence_self=confidence,
                    rationale=f"package anchor {slug!r} "
                              f"from deps {deps_list!r}",
                ),
            )

        # ── Sprint D3 — workspace package anchors ──
        # Every declared workspace is its own deterministic feature.
        # We emit one anchor per workspace whose paths are the full
        # file list under that workspace. Stage 2's source priority
        # (``package`` > ``route``) means workspace anchors win
        # cross-feature path attribution, but Stage 2's zero-path
        # protection keeps per-route slugs alive (they end up sharing
        # ownership). The net effect is: every workspace becomes a
        # feature AND every per-route slug stays detectable.
        if ctx.monorepo and ctx.workspaces:
            seen_workspace_slugs: set[str] = set()
            for ws in ctx.workspaces:
                ws_slug = _workspace_slug(ws)
                if not ws_slug:
                    continue
                if ws_slug in seen_workspace_slugs:
                    # Two workspaces with the same slug — keep only the
                    # first. Stage 2 would merge them anyway, but
                    # skipping here keeps Stage 1 telemetry honest
                    # (extractor_hits["package"] == real anchor count).
                    continue
                seen_workspace_slugs.add(ws_slug)
                ws_paths = tuple(ws.files) if ws.files else (ws.path,)
                rationale = (
                    f"workspace anchor {ws_slug!r} from monorepo "
                    f"package {ws.path!r}"
                )
                if isinstance(ws.package_json, dict) and ws.package_json.get("name"):
                    rationale += f" (package.json name={ws.package_json['name']!r})"
                out.append(
                    AnchorCandidate(
                        name=ws_slug,
                        paths=ws_paths,
                        source=self.name,
                        confidence_self=0.95,
                        rationale=rationale,
                    ),
                )

        return out


# ── private helpers (re-exported from _util for symmetry) ──────────────────

def _read_json(path: Path) -> dict | None:
    """Local wrapper so callers don't have to import from _util."""
    from faultline.pipeline_v2.extractors._util import read_json
    result = read_json(path)
    return result if isinstance(result, dict) else None


__all__ = ["PackageAnchorExtractor"]
