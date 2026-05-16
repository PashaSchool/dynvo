"""Test-file extractor (Sprint 9a).

Library codebases lack the rich grounding signals that SaaS repos have
(no routes, no JSX nav, no controllers). What they DO have is a
test suite where each test file typically targets ONE public capability:
``tests/test_security.py`` → Security feature, ``tests/cancel.test.ts``
→ Cancel feature, ``__tests__/interceptors.test.ts`` → Interceptors.

This extractor walks the repo and emits one ``Signal(kind="test-anchor")``
per test file. Critique consumes these as candidate features the
primary scan may have missed.

**Safety gate:** the extractor returns ``[]`` when
``repo_structure.is_library`` is False. SaaS scans never see these
signals — only library scans benefit. This protects existing
SaaS-shaped scan behavior from regression.

Generic per ``rule-no-repo-specific-paths``: matches by file-shape
patterns (``tests/test_<X>.py``, ``__tests__/<X>.test.ts``,
``<X>.test.{ts,js,py}``), not by per-repo file names. The captured
``<X>`` becomes the candidate feature slug.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from faultline.signals import Signal

logger = logging.getLogger(__name__)


# ── File-name patterns ──────────────────────────────────────────────

# Python: tests/test_<slug>.py or tests/<slug>_test.py.
_PY_TEST_RE = re.compile(
    r"(?:^|/)tests?/(?:test_)?([a-zA-Z_][a-zA-Z0-9_]*?)(?:_test)?\.py$"
)

# JavaScript/TypeScript: tests/<slug>.test.{...} OR nested under
# tests/unit/<X>.test.js, tests/integration/<X>.spec.ts, etc.
# axios uses ``tests/unit/<feature>.test.js``.
_JS_TEST_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__)/(?:[^/]+/)?([a-zA-Z_][a-zA-Z0-9_-]*)\.(?:test|spec)\.(?:ts|tsx|js|jsx|mjs)$"
)

# Co-located tests: <anything>/<slug>.test.ts (excluding ``index``,
# ``util``, ``utils``, ``helpers``, ``common`` which are not features).
_COLOCATED_TEST_RE = re.compile(
    r"(?:^|/)([a-zA-Z_][a-zA-Z0-9_-]*)\.(?:test|spec)\.(?:ts|tsx|js|jsx|mjs)$"
)


# Slugs that are not real features — utility / scaffolding test files
# that would pollute critique with false-positive feature suggestions.
# Generic noise list, not repo-specific.
_NOISE_SLUGS = frozenset({
    "index", "main", "init", "setup", "teardown", "fixture", "fixtures",
    "helper", "helpers", "util", "utils", "common", "shared", "core",
    "test", "tests", "mock", "mocks", "stub", "stubs", "_test",
    "conftest", "smoke", "sanity", "example", "examples",
})


def _slug_is_noise(slug: str) -> bool:
    return slug.lower() in _NOISE_SLUGS


# ── Walker ──────────────────────────────────────────────────────────


_SKIP_DIRS = frozenset({
    "node_modules", "dist", "build", ".next", ".turbo", ".git", "coverage",
    "__pycache__", ".venv", "venv", "env", "target", "vendor",
})


def _walk_test_files(repo_root: Path):
    """Yield ``(relative_path, slug, kind_label)`` for every test
    file under repo_root. ``kind_label`` is the matching pattern
    (py-test, js-test, colocated) for debugging.
    """
    for root, dirs, files in _walk(repo_root):
        for fn in files:
            full = Path(root) / fn
            try:
                rel = str(full.relative_to(repo_root))
            except ValueError:
                continue
            for pattern, label in (
                (_PY_TEST_RE, "py-test"),
                (_JS_TEST_RE, "js-test"),
                (_COLOCATED_TEST_RE, "colocated"),
            ):
                m = pattern.search(rel)
                if not m:
                    continue
                slug = m.group(1)
                if _slug_is_noise(slug):
                    continue
                yield rel, slug, label
                break


def _walk(repo_root: Path):
    import os
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        yield root, dirs, files


# ── Extractor ───────────────────────────────────────────────────────


class TestFileExtractor:
    """Emits a ``test-anchor`` signal per non-noise test file."""

    name = "test-file-extractor"

    def applicable(self, repo_root: Path) -> bool:
        # Cheap pre-check; the extract() call still re-checks the
        # is_library safety gate on the ``repo_structure`` kwarg.
        # Returning True unconditionally keeps the extractor in the
        # discovery list — gating happens at extract() time once
        # repo_structure is available.
        _ = repo_root
        return True

    def extract(
        self,
        repo_root: Path,
        files=(),
        *,
        repo_structure=None,
    ) -> list[Signal]:
        """Walk ``repo_root`` for test files. Returns empty list when
        the caller didn't pass ``repo_structure`` or when the repo is
        not a library (safety gate to avoid affecting SaaS scans).
        """
        _ = files
        # Safety gate — only emit on library repos. SaaS scans have
        # stronger signals (routes, controllers, nav) and don't need
        # noisy test-name suggestions.
        if repo_structure is None or not getattr(repo_structure, "is_library", False):
            return []

        out: list[Signal] = []
        seen: set[str] = set()
        for rel, slug, label in _walk_test_files(repo_root):
            key = slug.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Signal(
                    kind="test-anchor",
                    source=self.name,
                    payload={
                        "file": rel,
                        "slug": slug,
                        "match_kind": label,
                    },
                ),
            )
        return out


__all__ = ["TestFileExtractor"]
