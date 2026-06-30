"""Deterministic PRODUCT-CAPABILITY anchor extraction (Phase 1).

Research proved that product-grain capability names already exist in
code-grounded sources the engine does NOT currently read — locale
files, navigation components, analytics event names, and test titles.
Ingesting them covers 76-84% of the golden product-feature corpus; in
combination with the code-grounded developer features, the corpus
recall ceiling is ~95%.

This module is the EXTRACT half of that bet (Phase 1). It produces a
per-repo *product-capability anchor list* — a deduplicated, bounded,
fully-deterministic list of :class:`ProductAnchor`. A later phase
(Stage 6.7d ALIGN, deferred) will steer the LLM journey/product
abstraction toward these anchors instead of free-generating.

Hard rules honoured here:

* **NO README / in-repo prose .md** is ever read — only structured
  code/config sources (locale ``.json``, nav ``.tsx``, test files,
  analytics call sites). See ``rule-no-readme`` / project ``CLAUDE.md``.
* **Deterministic, no network.** Traversal order is sorted, output is
  sorted, dedup is stable — same repo tree → byte-identical output.
  The external "docs" source (marketing site / public docs) is OUT OF
  SCOPE for Phase 1 — :func:`extract_docs_anchors` is a TODO stub.
* **No magic tuned thresholds.** Every literal below is a list-size or
  string-length *bound* (cap), not a per-repo tuned knob.

No LLM. Pure local-disk reads. A failure in any single source is
swallowed (returns ``[]`` for that source) so anchor extraction can
never crash a scan.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

logger = logging.getLogger(__name__)

AnchorSource = Literal["i18n", "nav", "analytics", "test", "docs"]


# ── Bounds (list-size / string-length caps — NOT tuned thresholds) ──────────
#: Max files visited per directory-walk pattern (performance ceiling).
MAX_FILES_PER_PATTERN = 4000
#: Max locale files parsed for i18n (the rest are near-duplicate locales).
MAX_LOCALE_FILES = 60
#: Max distinct strings collected from a single grep pass (memory ceiling).
MAX_GREP_STRINGS = 2000
#: Max analytics event names collected (memory ceiling).
MAX_ANALYTICS_STRINGS = 500
#: Max nav labels collected (memory ceiling).
MAX_NAV_STRINGS = 400
#: Total anchor-list cap — bounds downstream prompt size.
MAX_TOTAL_ANCHORS = 800

#: Source trust ordering (NOT a tuned number — a fixed categorical
#: priority). Analytics events + nav labels are the maintainer's own
#: product vocabulary (highest trust); i18n namespaces are curated
#: section labels; i18n leaf values + test titles are dense but noisier.
#: This ordering decides (a) which source wins a cross-source text
#: collision in dedup and (b) which anchors survive the MAX_TOTAL_ANCHORS
#: cap — so the cap keeps the strongest product signal, not the
#: alphabetically-first long tail.
_SOURCE_PRIORITY: dict[AnchorSource, int] = {
    "analytics": 0,
    "nav": 1,
    "docs": 2,
    "i18n": 3,
    "test": 4,
}

#: Source-string length window (chars). Below/above are noise (ids / blobs).
_MIN_VALUE_LEN = 3
_MAX_VALUE_LEN = 60
_MIN_GREP_LEN = 4
_MAX_GREP_LEN = 80

#: Directories never descended into.
_EXCLUDE_DIRS = frozenset({"node_modules", ".git", "dist", "build", ".next"})

#: Path segments that mark a locale/i18n directory.
_I18N_DIR_MARKERS = (
    "/locales/",
    "/i18n/",
    "/messages/",
    "/lang/",
    "/translations/",
)
#: Preferred English locale basenames (lowercased).
_PREFERRED_LOCALE_BASENAMES = frozenset(
    {"en.json", "en-us.json", "common.json", "messages.json"},
)
#: Filename substrings that mark a navigation component.
_NAV_FILENAME_MARKERS = ("nav", "sidebar", "menu", "tabs")
#: Path substrings (lowercased) that mark a test file.
_TEST_PATH_MARKERS = (
    ".test.",
    ".spec.",
    "/__tests__/",
    "/e2e/",
    "/cypress/",
    "/tests/",
    "/test/",
)

_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".py")

# ── Compiled grep patterns (group 1 = the captured capability string) ───────
_NAV_RX = re.compile(r"""(?:label|title|name)\s*[:=]\s*['"]([A-Z][^'"]{2,40})['"]""")
_ANALYTICS_RX = (
    re.compile(r"""(?:track|capture|logEvent|trackEvent)\(\s*['"]([^'"]{3,60})['"]"""),
    re.compile(r"""analytics\.\w+\(\s*['"]([^'"]{3,60})['"]"""),
)
_TEST_RX = re.compile(r"""(?:describe|it|test)\(\s*['"`]([^'"`]{4,80})['"`]""")
#: camelCase / kebab / snake word-boundary splitter for humanising keys.
_WORD_SPLIT_RX = re.compile(r"[_\-\s]+|(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True)
class ProductAnchor:
    """One product-capability string extracted from a code-grounded source.

    Attributes:
        text: the capability string, verbatim for grepped values or
            humanised for i18n namespaces (e.g. ``"User Settings"``).
        source: which extractor produced it — used later to weight
            trust (analytics/test events are stronger product signal
            than a stray nav label).
        locator: file path (repo-relative POSIX) or ``path#key`` for
            i18n namespaces — provenance for trust/debugging later.
    """

    text: str
    source: AnchorSource
    locator: str


# ── Filesystem walk (deterministic: sorted dirs + files) ────────────────────


def _walk(
    root: Path,
    suffixes: tuple[str, ...],
    must: Callable[[str], bool] | None = None,
    *,
    cap: int = MAX_FILES_PER_PATTERN,
) -> list[str]:
    """Return up to ``cap`` files under ``root`` matching ``suffixes``.

    Traversal is sorted (dirs + files) so the capped subset is stable
    across machines — a precondition for deterministic output. Excludes
    :data:`_EXCLUDE_DIRS`. ``must`` is an optional per-path predicate
    (receives the absolute path).
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded dirs in place; sort the rest for stable order.
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDE_DIRS)
        for fn in sorted(filenames):
            if not fn.endswith(suffixes):
                continue
            full = os.path.join(dirpath, fn)
            if must is not None and not must(full):
                continue
            out.append(full)
            if len(out) >= cap:
                return out
    return out


def _relpath(path: str, root: Path) -> str:
    """Repo-relative POSIX path for use as a provenance locator."""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        rel = path
    return rel.replace(os.sep, "/")


def _is_english(text: str) -> bool:
    """True when ``text`` is ASCII and contains at least one letter.

    Drops multilingual locale noise (non-Latin scripts) and pure
    punctuation/number tokens without conditioning on any word list.
    """
    return text.isascii() and any(c.isalpha() for c in text)


def _humanise(key: str) -> str:
    """``user_settings`` / ``api-keys`` / ``apiKeys`` → ``Api Keys`` words."""
    words = [w for w in _WORD_SPLIT_RX.split(key) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _grep_strings(
    files: list[str],
    patterns: tuple[re.Pattern[str], ...],
    *,
    cap: int = MAX_GREP_STRINGS,
) -> list[tuple[str, str]]:
    """Return ``(captured_string, relpath)`` pairs from ``files``.

    Filters obvious non-capability tokens (urls / paths / dotted ids)
    and the configured length window. Stops once ``cap`` distinct
    strings are seen. ``files`` MUST already be sorted for determinism.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for full in files:
        try:
            txt = Path(full).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx in patterns:
            for m in rx.finditer(txt):
                s = m.group(1).strip()
                if not (_MIN_GREP_LEN <= len(s) <= _MAX_GREP_LEN):
                    continue
                if s.startswith(("http", "/", ".")):
                    continue
                if s in seen:
                    continue
                seen.add(s)
                out.append((s, full))
        if len(seen) >= cap:
            break
    return out


# ── Per-source extractors (each robust to the source being absent) ──────────


def extract_i18n_anchors(repo_root: Path) -> list[ProductAnchor]:
    """Locale JSON → top-level namespaces (humanised) + leaf UI values.

    Prefers English locale files (``/en`` in path or a preferred
    basename). Namespaces become humanised capability labels; leaf
    string values that look like English UI copy become anchors too.
    Both pass the :func:`_is_english` filter to drop multilingual noise.
    """
    def _is_locale(path: str) -> bool:
        low = path.lower()
        if not any(marker in low for marker in _I18N_DIR_MARKERS):
            return False
        return "/en" in low or os.path.basename(low) in _PREFERRED_LOCALE_BASENAMES

    files = _walk(repo_root, (".json",), _is_locale)[:MAX_LOCALE_FILES]
    anchors: list[ProductAnchor] = []
    for full in files:
        rel = _relpath(full, repo_root)
        try:
            data = json.loads(Path(full).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        def _recurse(obj: object, depth: int = 0) -> None:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if depth == 0 and isinstance(key, str) and _is_english(key):
                        label = _humanise(key)
                        if len(label) >= 2:
                            anchors.append(
                                ProductAnchor(label, "i18n", f"{rel}#{key}"),  # noqa: B023
                            )
                    _recurse(value, depth + 1)
            elif isinstance(obj, str):
                s = obj.strip()
                if (
                    _MIN_VALUE_LEN <= len(s) <= _MAX_VALUE_LEN
                    and s[:1].isupper()
                    and _is_english(s)
                ):
                    anchors.append(ProductAnchor(s, "i18n", rel))  # noqa: B023

        _recurse(data)
    return anchors


def extract_nav_anchors(repo_root: Path) -> list[ProductAnchor]:
    """Navigation components → ``label`` / ``title`` / ``name`` props.

    Only files whose basename contains nav/sidebar/menu/tabs are read,
    and only uppercase-initial string props (explicit author intent).
    """
    def _is_nav(path: str) -> bool:
        base = os.path.basename(path).lower()
        return any(marker in base for marker in _NAV_FILENAME_MARKERS)

    files = _walk(repo_root, _SOURCE_SUFFIXES, _is_nav)
    pairs = _grep_strings(files, (_NAV_RX,), cap=MAX_NAV_STRINGS)
    return [
        ProductAnchor(text, "nav", _relpath(full, repo_root))
        for text, full in pairs
    ]


def extract_analytics_anchors(repo_root: Path) -> list[ProductAnchor]:
    """Analytics call sites → event-name string literals.

    Greps ``track()`` / ``capture()`` / ``logEvent()`` / ``trackEvent()``
    and ``analytics.<method>()`` across source files. Event names are
    the maintainer's own product vocabulary — strong product signal.
    """
    files = _walk(repo_root, _SOURCE_SUFFIXES)
    pairs = _grep_strings(files, _ANALYTICS_RX, cap=MAX_ANALYTICS_STRINGS)
    return [
        ProductAnchor(text, "analytics", _relpath(full, repo_root))
        for text, full in pairs
    ]


def extract_test_anchors(repo_root: Path) -> list[ProductAnchor]:
    """Test titles → ``describe`` / ``it`` / ``test`` string arguments.

    Test titles describe behaviour in product language ("creates a
    booking", "rejects an expired invite") and are a dense capability
    source. Only files under recognised test paths are read.
    """
    def _is_test(path: str) -> bool:
        low = path.lower()
        return any(marker in low for marker in _TEST_PATH_MARKERS)

    files = _walk(repo_root, _SOURCE_SUFFIXES, _is_test)
    pairs = _grep_strings(files, (_TEST_RX,))
    return [
        ProductAnchor(text, "test", _relpath(full, repo_root))
        for text, full in pairs
    ]


def extract_docs_anchors(repo_root: Path) -> list[ProductAnchor]:
    """External marketing/docs surfaces → product capabilities.

    OUT OF SCOPE for Phase 1: this would fetch the product's public
    marketing site / docs sidebar (NOT the in-repo README) over the
    network and is intentionally deferred. Returns ``[]``.

    TODO(phase-2): wire an authorised WebFetch source behind a flag,
    reusing the ``uf-spec-curator`` public-surface logic. Must stay
    opt-in so the default scan remains network-free and deterministic.
    """
    return []


# ── Aggregator ──────────────────────────────────────────────────────────────

#: Registry of Phase-1 (local, deterministic, network-free) sources.
_EXTRACTORS: tuple[Callable[[Path], list[ProductAnchor]], ...] = (
    extract_i18n_anchors,
    extract_nav_anchors,
    extract_analytics_anchors,
    extract_test_anchors,
)


def extract_product_anchors(repo_root: Path) -> list[ProductAnchor]:
    """Run every local source, dedup by lowercased text, cap, and sort.

    Fully deterministic: identical repo tree → identical output. Each
    source is guarded — one source raising never aborts the others nor
    the scan.

    Ordering is ``(source priority, lowercased text, locator)`` so the
    output is sorted, dedup resolves a cross-source text collision to
    the higher-trust source, and the ``MAX_TOTAL_ANCHORS`` cap keeps the
    strongest product signal (analytics / nav) rather than the
    alphabetically-first long tail of test titles.
    """
    repo_root = Path(repo_root)
    collected: list[ProductAnchor] = []
    for extractor in _EXTRACTORS:
        try:
            collected.extend(extractor(repo_root))
        except Exception as exc:  # noqa: BLE001 — a source must never abort the scan
            logger.warning(
                "anchor_extractors: %s failed: %s", extractor.__name__, exc,
            )

    collected.sort(
        key=lambda a: (_SOURCE_PRIORITY.get(a.source, 99), a.text.lower(), a.locator),
    )

    deduped: list[ProductAnchor] = []
    seen_lower: set[str] = set()
    for anchor in collected:
        key = anchor.text.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        deduped.append(anchor)
        if len(deduped) >= MAX_TOTAL_ANCHORS:
            break
    return deduped


def anchor_telemetry(anchors: list[ProductAnchor]) -> dict[str, object]:
    """Compact ``scan_meta.product_anchors`` payload (counts + small sample).

    NEVER embeds the full anchor list — only per-source counts, the
    total, and the first ~15 texts (sorted-stable) for eyeballing.
    """
    by_source: dict[str, int] = {}
    for anchor in anchors:
        by_source[anchor.source] = by_source.get(anchor.source, 0) + 1
    return {
        "total": len(anchors),
        "by_source": dict(sorted(by_source.items())),
        "sample": [a.text for a in anchors[:15]],
    }


__all__ = [
    "AnchorSource",
    "ProductAnchor",
    "anchor_telemetry",
    "extract_analytics_anchors",
    "extract_docs_anchors",
    "extract_i18n_anchors",
    "extract_nav_anchors",
    "extract_product_anchors",
    "extract_test_anchors",
]
