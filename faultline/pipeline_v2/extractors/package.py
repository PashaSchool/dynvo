"""PackageAnchorExtractor — dependency manifest → feature anchor.

Per the ``package-anchor-extractor`` skill, certain dependencies are
strong, near-binary signals for product capability:

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

from faultline.pipeline_v2.extractors._util import (
    posix,
    read_text,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# (match_predicate, anchor_slug, rationale_template). The predicate
# receives the dependency name and returns ``True`` when it matches.
# We use predicates rather than a flat dict so we can match families
# like ``@stripe/*`` without enumerating every package.

_DepMatch = tuple[str, str]
"""(matcher_token, anchor_slug). matcher_token is matched in two ways:

   - exact equality with the dep name
   - dep name starts with ``matcher_token + "/"`` (covers @scope/* families)
   - dep name starts with ``matcher_token + "-"`` (covers ``next-auth-*``)
"""

_JS_DEP_ANCHORS: tuple[_DepMatch, ...] = (
    # ── Billing ──
    ("stripe", "billing"),
    ("@stripe", "billing"),
    ("@paddle", "billing"),
    ("paddle-sdk", "billing"),
    ("lemonsqueezy.js", "billing"),

    # ── Auth ──
    ("next-auth", "auth"),
    ("@auth", "auth"),
    ("better-auth", "auth"),
    ("lucia", "auth"),
    ("@clerk", "auth"),
    ("clerk", "auth"),
    ("@workos-inc", "auth"),
    ("@kinde-oss", "auth"),

    # ── Email ──
    ("resend", "email"),
    ("@sendgrid", "email"),
    ("postmark", "email"),
    ("nodemailer", "email"),
    ("@aws-sdk/client-ses", "email"),

    # ── Background jobs ──
    ("inngest", "background-jobs"),
    ("bullmq", "background-jobs"),
    ("bull", "background-jobs"),
    ("@trigger.dev", "background-jobs"),
    ("agenda", "background-jobs"),

    # ── File uploads / storage ──
    ("@uploadthing", "file-uploads"),
    ("uploadthing", "file-uploads"),
    ("@aws-sdk/client-s3", "file-uploads"),
    ("@vercel/blob", "file-uploads"),

    # ── Analytics ──
    ("posthog-node", "analytics"),
    ("posthog-js", "analytics"),
    ("mixpanel", "analytics"),
    ("@segment", "analytics"),

    # ── Realtime ──
    ("socket.io", "realtime"),
    ("ws", "realtime"),
    ("@pusher", "realtime"),
    ("pusher", "realtime"),
    ("ably", "realtime"),

    # ── AI ──
    ("openai", "ai"),
    ("anthropic", "ai"),
    ("@anthropic-ai", "ai"),
    ("@google/generative-ai", "ai"),
    ("ai", "ai"),

    # ── i18n ──
    ("next-i18next", "i18n"),
    ("i18next", "i18n"),
    ("react-i18next", "i18n"),
    ("@lingui", "i18n"),
)

_PY_DEP_ANCHORS: tuple[_DepMatch, ...] = (
    ("stripe", "billing"),
    ("django-allauth", "auth"),
    ("authlib", "auth"),
    ("python-jose", "auth"),
    ("celery", "background-jobs"),
    ("rq", "background-jobs"),
    ("dramatiq", "background-jobs"),
    ("openai", "ai"),
    ("anthropic", "ai"),
    ("langchain", "ai"),
    ("boto3", "file-uploads"),
    ("sendgrid", "email"),
)


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
    """For each anchor matcher firing, group the dep names that fired."""
    hits: dict[str, list[str]] = defaultdict(list)
    for dep in deps:
        for token, slug in matchers:
            if _dep_matches(dep, token):
                hits[slug].append(dep)
                break  # one anchor per dep is enough
    return hits


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
        return out


# ── private helpers (re-exported from _util for symmetry) ──────────────────

def _read_json(path: Path) -> dict | None:
    """Local wrapper so callers don't have to import from _util."""
    from faultline.pipeline_v2.extractors._util import read_json
    result = read_json(path)
    return result if isinstance(result, dict) else None


__all__ = ["PackageAnchorExtractor"]
