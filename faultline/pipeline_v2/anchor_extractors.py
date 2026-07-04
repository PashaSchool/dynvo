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

AnchorSource = Literal["i18n", "nav", "docs_nav", "analytics", "test", "docs"]

# ── Grain tiers (align-v2, Phase 3.1) ────────────────────────────────────────
# Phase 3.0 (2026-07-02) proved anchor GRAIN — not pool cleanliness — decides
# whether Stage 6.7d alignment helps or strangles: journey/action-grain pools
# (supabase analytics+nav, cal-com fine i18n namespaces) won +7..+12 UF-F1;
# a domain-grain pool (formbricks: 11 coarse i18n namespaces) bounding 37
# journeys cost −9..−14.5. Every anchor therefore carries a grain TIER:
#
#   tier1_action  — the maintainer's own ACTION/journey-grain vocabulary:
#                   analytics event names, in-app nav labels, i18n NAMESPACE
#                   keys (structural author intent), external docs sidebar.
#                   ONLY tier-1 anchors count toward the 6.7d align gate and
#                   ONLY tier-1 texts are ever sent to the align prompt.
#   tier2_advisory — noisy prose: i18n leaf VALUES (the Soc0 lesson — aligning
#                   to UI copy took PF name-Jaccard 0.72 → 0.03), test titles,
#                   docs-SITE page-title nav. Extracted for telemetry / dual
#                   evidence, NEVER admitted to the gate or the align prompt.
AnchorTier = Literal["tier1_action", "tier2_advisory"]
TIER1_ACTION: AnchorTier = "tier1_action"
TIER2_ADVISORY: AnchorTier = "tier2_advisory"

#: Default tier by SOURCE (categorical, not tuned). ``i18n`` defaults to
#: tier-2 on purpose: within i18n only NAMESPACE-KEY anchors are action-grain
#: and the extractor stamps those ``tier1_action`` EXPLICITLY — so an i18n
#: anchor constructed without a tier (a leaf value, or any legacy caller) can
#: never count toward the gate. Leaf values must NEVER be tier-1.
_DEFAULT_TIER_BY_SOURCE: dict[str, AnchorTier] = {
    "analytics": TIER1_ACTION,
    "nav": TIER1_ACTION,
    "docs": TIER1_ACTION,
    "i18n": TIER2_ADVISORY,
    "test": TIER2_ADVISORY,
    "docs_nav": TIER2_ADVISORY,
}


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
#: Max i18n anchors (namespaces + leaf values) collected from ALL locale
#: files combined. A guard against a pathological locale tree (e.g.
#: cal.com ships ~215k leaf strings) allocating ~85MB of anchors before
#: the global cap ever applies — bounds memory + traversal, not accuracy
#: (the alignment pool only ever reads the first ~160 by trust). NOT a
#: per-repo tuned knob: a memory ceiling like every other MAX_* here.
MAX_I18N_VALUES = 3000
#: Total raw anchor-list cap (telemetry view, before pool curation).
MAX_RAW_ANCHORS = 5000
#: Total alignment-pool cap — bounds the prompt fed to Stage 6.7d.
MAX_TOTAL_ANCHORS = 800

#: Source trust ordering (NOT a tuned number — a fixed categorical
#: priority). Analytics events + app nav labels are the maintainer's own
#: product vocabulary (highest trust); i18n namespaces are curated
#: section labels; i18n leaf values + test titles are dense but noisier;
#: docs-site nav (page titles of the marketing/docs SITE, not the app)
#: is the weakest product signal of all. This ordering decides (a) which
#: source wins a cross-source text collision in dedup and (b) which
#: anchors survive a cap — so the cap keeps the strongest product signal,
#: not the alphabetically-first long tail.
_SOURCE_PRIORITY: dict[AnchorSource, int] = {
    "analytics": 0,
    "nav": 1,
    "docs": 2,
    "i18n": 3,
    "test": 4,
    "docs_nav": 5,
}

# ── Alignment-pool curation (which anchors actually feed Stage 6.7d) ─────────
#: Sources NEVER admitted to the alignment pool by default. Test titles are
#: dense + noisy (documenso shipped 795/800 test titles — describe/it blocks,
#: assertions, fixtures — drowning the real product vocabulary). Docs-site nav
#: is the marketing/docs SITE's page titles, not the APP's features (supabase
#: shipped 615 docs-site page titles). Both stay EXTRACTED (telemetry) but are
#: kept OUT of the pool that steers product/journey naming.
_POOL_EXCLUDED_SOURCES: frozenset[AnchorSource] = frozenset({"test", "docs_nav"})
#: Per-source cap inside the pool so no single source dominates (the cap is a
#: list-size bound, NOT a per-repo tuned threshold). Sources absent here are
#: excluded from the pool entirely (see ``_POOL_EXCLUDED_SOURCES``).
_POOL_PER_SOURCE_CAP: dict[AnchorSource, int] = {
    "analytics": 400,
    "nav": 300,
    "docs": 200,
    "i18n": 400,
}
#: When the repo has essentially NO product signal (i18n + app-nav + analytics
#: + docs all tiny — a library/CLI with only tests), test titles are admitted
#: as a LOW-TRUST FALLBACK so the pool is not empty. ``_FALLBACK_MIN_SIGNAL``
#: is the "essentially no signal" bound (categorical, not tuned); admitted test
#: anchors are themselves capped at ``_TEST_FALLBACK_CAP`` so even the fallback
#: can never produce a test-dominated blob.
_FALLBACK_MIN_SIGNAL = 12
_TEST_FALLBACK_CAP = 80

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
#: Path substrings (lowercased, POSIX) that mark a DOCS-SITE surface — the
#: marketing/documentation site, NOT the application. Nav extracted from here
#: is the site's page-title list (e.g. supabase ``apps/docs`` / ``apps/www``),
#: which is product *prose* navigation, not the app's feature surface, so it is
#: tagged ``docs_nav`` and excluded from the alignment pool.
_DOCS_SITE_PATH_MARKERS = (
    "/docs/",
    "/website/",
    "/content/",
    "/apps/docs/",
    "/apps/www/",
    "/documentation/",
    "docusaurus",
    "nextra",
)
#: Basename prefixes of known docs-site nav configs (docusaurus ``sidebars.*``,
#: nextra ``_meta.*``) — treated as docs-site nav regardless of their location.
_DOCS_NAV_BASENAME_PREFIXES = ("sidebars.", "_meta.")
# ── Widget-vocabulary demotion (align-v2 revival, MISSION-92 lever #3) ──────
# Root cause fixed here: design-system/ui-kit component files match the nav
# filename markers (``Tabs.stories.tsx``, ``DropdownMenu.tsx``, ``Sidebar.tsx``)
# and their ``label``/``title``/``name`` props are WIDGET nouns ("Tooltip",
# "Alert Dialog", "Hover Card"), which then classified as tier-1 nav anchors
# and steered ALIGN into the anchor-echo catastrophe (supabase F1 18.2/21.1).
# Two STRUCTURAL demotions (no per-repo tuning):
#
# 1. PATH demotion — any anchor extracted from a design-system / ui-kit /
#    storybook / component-library surface is ALWAYS tier-2: those packages
#    document WIDGETS, not product capabilities.
_DESIGN_SYSTEM_PATH_MARKERS = (
    "packages/ui/",
    "/design-system/",
    "design-system/",
    "/ui-kit/",
    "/component-library/",
    "/components-library/",
    ".stories.",          # storybook story files, wherever they live
    "/storybook/",
    ".storybook/",
)
# 2. VOCABULARY demotion — a tier-1 label whose ENTIRE text (separator- and
#    case-normalised) is a bare widget name is demoted to tier-2. The list is
#    FIXED and PUBLIC (structural, not per-repo tuning). Provenance:
#      (a) WAI-ARIA 1.2 widget, composite-widget and window role names
#          (W3C Recommendation, §5.3.2/5.3.3 — https://www.w3.org/TR/wai-aria-1.2/)
#      (b) WHATWG HTML interactive/form element names
#          (https://html.spec.whatwg.org/ — forms + interactive elements)
#      (c) Component-library primitive names from the two dominant public
#          TS design-system indexes: Radix UI Primitives (radix-ui.com/primitives)
#          and shadcn/ui components (ui.shadcn.com/docs/components)
#    Matching is EXACT on the normalised whole label ("Tabs", "Scroll-area",
#    "Hover Card" demote; "Tabs Settings" or "Form Builder" do NOT).
_WIDGET_VOCABULARY: frozenset[str] = frozenset({
    # (a) WAI-ARIA 1.2 widget + composite + window roles
    "alert", "alertdialog", "button", "checkbox", "combobox", "dialog",
    "feed", "grid", "gridcell", "link", "listbox", "log", "marquee", "menu",
    "menubar", "menuitem", "menuitemcheckbox", "menuitemradio", "meter",
    "option", "progressbar", "radio", "radiogroup", "scrollbar", "searchbox",
    "separator", "slider", "spinbutton", "status", "switch", "tab", "tablist",
    "tabpanel", "textbox", "timer", "toolbar", "tooltip", "tree", "treegrid",
    "treeitem",
    # (b) WHATWG HTML form / interactive element names
    "input", "select", "form", "label", "fieldset", "legend", "datalist",
    "output", "textarea", "details", "summary", "table",
    # (c) Radix UI Primitives + shadcn/ui component names (normalised:
    # separators removed, lowercased)
    "accordion", "aspectratio", "avatar", "badge", "breadcrumb", "calendar",
    "card", "carousel", "chart", "collapsible", "command", "contextmenu",
    "datatable", "datepicker", "drawer", "dropdownmenu", "hovercard",
    "inputotp", "navigationmenu", "onetimepassword", "pagination", "popover",
    "progress", "resizable", "scrollarea", "sheet", "sidebar", "skeleton",
    "sonner", "tabs", "toast", "toggle", "togglegroup", "toolbarbutton",
    "typography",
})
#: Separator characters removed by widget-label normalisation.
_WIDGET_NORM_RX = re.compile(r"[\s_\-./]+")


def _is_design_system_path(locator: str) -> bool:
    """True when the anchor's provenance path is a design-system surface."""
    low = locator.replace(os.sep, "/").lower()
    return any(marker in low for marker in _DESIGN_SYSTEM_PATH_MARKERS)


def is_widget_label(text: str) -> bool:
    """True when the WHOLE label is a bare widget noun from the fixed list.

    Normalisation removes separators and case so every author spelling of
    the same widget matches: ``Tabs`` / ``Scroll-area`` / ``Hover Card`` /
    ``hover_card`` / ``AlertDialog`` → ``tabs`` / ``scrollarea`` /
    ``hovercard`` / ``hovercard`` / ``alertdialog``.
    """
    return _WIDGET_NORM_RX.sub("", text.strip().lower()) in _WIDGET_VOCABULARY


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
        tier: grain tier (see :data:`AnchorTier`). Defaults from the
            SOURCE via ``_DEFAULT_TIER_BY_SOURCE`` when not given;
            i18n NAMESPACE-key extraction passes ``tier1_action``
            explicitly, so i18n leaf values can never be tier-1.
    """

    text: str
    source: AnchorSource
    locator: str
    tier: AnchorTier = ""  # type: ignore[assignment]  # sentinel → derived in __post_init__

    def __post_init__(self) -> None:
        if not self.tier:
            object.__setattr__(
                self,
                "tier",
                _DEFAULT_TIER_BY_SOURCE.get(self.source, TIER2_ADVISORY),
            )


def anchor_tier(anchor: object) -> AnchorTier:
    """Grain tier of *anchor*, robust to foreign/duck-typed objects.

    Reads ``anchor.tier`` when it is a recognised tier value; otherwise
    derives the tier from ``anchor.source`` (unknown source → tier-2, the
    safe direction: an unclassifiable anchor never counts toward the gate).
    """
    tier = getattr(anchor, "tier", "") or ""
    if tier in (TIER1_ACTION, TIER2_ADVISORY):
        return tier  # type: ignore[return-value]
    return _DEFAULT_TIER_BY_SOURCE.get(
        str(getattr(anchor, "source", "") or ""), TIER2_ADVISORY,
    )


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
                # Cap check INSIDE the match loop (F2): a per-file end-of-loop
                # check could overshoot ``cap`` by a whole file's worth of
                # matches; bail the instant the cap is reached.
                if len(seen) >= cap:
                    return out
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
        # Per-VALUE cap (F1): bail before recursing into the next file once the
        # combined namespace + leaf-value count hits the ceiling. A 215k-value
        # locale tree (cal.com) would otherwise allocate ~85MB of anchors before
        # the downstream global cap ever bites. The first ~3000 (sorted-stable)
        # already saturate the trust-ordered alignment pool.
        if len(anchors) >= MAX_I18N_VALUES:
            break
        rel = _relpath(full, repo_root)
        try:
            data = json.loads(Path(full).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        def _recurse(obj: object, depth: int = 0) -> None:
            # Stop allocating (and stop traversing) the instant the cap is hit;
            # the guard lives INSIDE the recursion so a single huge file cannot
            # blow past MAX_I18N_VALUES.
            if len(anchors) >= MAX_I18N_VALUES:  # noqa: B023
                return
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if depth == 0 and isinstance(key, str) and _is_english(key):
                        label = _humanise(key)
                        if len(label) >= 2:
                            # NAMESPACE KEY path (structural author intent) —
                            # the only i18n anchors that are action-grain.
                            anchors.append(
                                ProductAnchor(  # noqa: B023
                                    label, "i18n", f"{rel}#{key}",
                                    tier=TIER1_ACTION,
                                ),
                            )
                    _recurse(value, depth + 1)
                    if len(anchors) >= MAX_I18N_VALUES:  # noqa: B023
                        return
            elif isinstance(obj, str):
                s = obj.strip()
                if (
                    _MIN_VALUE_LEN <= len(s) <= _MAX_VALUE_LEN
                    and s[:1].isupper()
                    and _is_english(s)
                ):
                    # Leaf VALUE (UI copy) — advisory tier, NEVER tier-1
                    # (Soc0: aligning to leaf values → name-Jaccard 0.03).
                    anchors.append(
                        ProductAnchor(s, "i18n", rel, tier=TIER2_ADVISORY),  # noqa: B023
                    )

        _recurse(data)
    return anchors


def _is_docs_site(path: str) -> bool:
    """True when ``path`` belongs to a DOCS/MARKETING site, not the app.

    Used to split app navigation (real feature surface, ``nav``) from
    docs-site navigation (page-title lists, ``docs_nav``). Matches on a
    docs-site path segment or a known docs-site nav-config basename.
    """
    low = path.replace(os.sep, "/").lower()
    if any(marker in low for marker in _DOCS_SITE_PATH_MARKERS):
        return True
    base = os.path.basename(low)
    return base.startswith(_DOCS_NAV_BASENAME_PREFIXES)


def extract_nav_anchors(repo_root: Path) -> list[ProductAnchor]:
    """Navigation components → ``label`` / ``title`` / ``name`` props.

    Only files whose basename contains nav/sidebar/menu/tabs are read,
    and only uppercase-initial string props (explicit author intent).
    Each anchor is tagged ``nav`` (in-app navigation — real feature
    surface) or ``docs_nav`` (the marketing/docs SITE's page-title nav,
    which is excluded from the alignment pool — see
    :func:`build_alignment_pool`). Both are kept for telemetry.
    """
    def _is_nav(path: str) -> bool:
        base = os.path.basename(path).lower()
        return any(marker in base for marker in _NAV_FILENAME_MARKERS)

    files = _walk(repo_root, _SOURCE_SUFFIXES, _is_nav)
    pairs = _grep_strings(files, (_NAV_RX,), cap=MAX_NAV_STRINGS)
    return [
        ProductAnchor(
            text,
            "docs_nav" if _is_docs_site(full) else "nav",
            _relpath(full, repo_root),
        )
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


def demote_widget_anchors(
    anchors: list[ProductAnchor],
) -> tuple[list[ProductAnchor], int]:
    """Demote widget/design-system tier-1 anchors to tier-2 (structural).

    Returns ``(anchors, n_demoted)``. Applied at the aggregation chokepoint
    so EVERY source is covered: a tier-1 anchor is demoted when it was
    extracted from a design-system surface (:data:`_DESIGN_SYSTEM_PATH_MARKERS`)
    or its whole text is a bare widget noun (:func:`is_widget_label`).
    Demoted anchors stay in the extraction (telemetry / dual evidence) but
    can no longer arm the 6.7d align gate nor reach the align prompt —
    tier-2 never does either.
    """
    out: list[ProductAnchor] = []
    demoted = 0
    for a in anchors:
        if anchor_tier(a) == TIER1_ACTION and (
            _is_design_system_path(a.locator) or is_widget_label(a.text)
        ):
            out.append(
                ProductAnchor(a.text, a.source, a.locator, tier=TIER2_ADVISORY),
            )
            demoted += 1
        else:
            out.append(a)
    return out, demoted


# ── Aggregator ──────────────────────────────────────────────────────────────

#: Registry of Phase-1 (local, deterministic, network-free) sources.
_EXTRACTORS: tuple[Callable[[Path], list[ProductAnchor]], ...] = (
    extract_i18n_anchors,
    extract_nav_anchors,
    extract_analytics_anchors,
    extract_test_anchors,
)


def _sort_key(a: ProductAnchor) -> tuple[int, int, str, str]:
    """Stable ``(source priority, tier rank, lowercased text, locator)`` key.

    Tier rank sits between source trust and text so that WITHIN a source the
    action-grain anchors outrank advisory ones: i18n NAMESPACE keys win the
    dedup collision against a same-text leaf value (else the leaf's shorter
    locator would win the tie and silently demote a tier-1 anchor to tier-2),
    and the per-source pool caps keep namespaces before leaf values.
    """
    tier_rank = 0 if anchor_tier(a) == TIER1_ACTION else 1
    return (_SOURCE_PRIORITY.get(a.source, 99), tier_rank, a.text.lower(), a.locator)


def _dedup_capped(
    sorted_anchors: list[ProductAnchor], cap: int,
) -> list[ProductAnchor]:
    """Dedup by lowercased text (keeping the first = highest-trust), cap.

    ``sorted_anchors`` MUST already be ordered by :func:`_sort_key` so the
    surviving anchor of a cross-source text collision is the higher-trust one.
    """
    out: list[ProductAnchor] = []
    seen_lower: set[str] = set()
    for anchor in sorted_anchors:
        key = anchor.text.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        out.append(anchor)
        if len(out) >= cap:
            break
    return out


def extract_raw_anchors(repo_root: Path) -> list[ProductAnchor]:
    """Run every local source; return the FULL deduped, sorted extraction.

    This is the RAW telemetry view — every source (incl. ``test`` and
    ``docs_nav``) is present so per-source counts reflect what the repo
    actually contains. The curated subset that steers naming is produced
    by :func:`build_alignment_pool`. Fully deterministic; each source is
    guarded so one raising never aborts the others nor the scan.
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
    # Widget/design-system demotion BEFORE sort+dedup: demoted anchors carry
    # tier-2 rank, so a same-text genuine tier-1 anchor from another source
    # wins the dedup collision and the per-source pool caps keep real
    # action-grain vocabulary ahead of widget noise.
    collected, demoted = demote_widget_anchors(collected)
    if demoted:
        logger.info("anchor_extractors: %d widget/design-system labels demoted to tier-2", demoted)
    collected.sort(key=_sort_key)
    return _dedup_capped(collected, MAX_RAW_ANCHORS)


def build_alignment_pool(raw_anchors: list[ProductAnchor]) -> list[ProductAnchor]:
    """Curate the raw extraction into the pool that feeds Stage 6.7d ALIGN.

    Curation rules (all deterministic, all list-size bounds — no per-repo
    tuned thresholds):

    * ``test`` + ``docs_nav`` sources are EXCLUDED by default — they are
      dense/noisy and historically dominated the cap (documenso 795 test
      titles; supabase 615 docs-site page titles), regressing alignment.
    * Each admitted source is capped at :data:`_POOL_PER_SOURCE_CAP` so no
      single source dominates the pool.
    * FALLBACK: when the curated core signal is essentially empty
      (``< _FALLBACK_MIN_SIGNAL`` — a library/CLI with only tests), a low
      cap of ``test`` titles is admitted so the pool is not empty.
    * Final dedup keeps the highest-trust source on a text collision and
      bounds the result to :data:`MAX_TOTAL_ANCHORS`.

    Input is assumed sorted by :func:`_sort_key` (as :func:`extract_raw_anchors`
    returns); the per-source slices therefore keep a stable subset.
    """
    by_source: dict[str, list[ProductAnchor]] = {}
    for a in raw_anchors:
        by_source.setdefault(a.source, []).append(a)

    core: list[ProductAnchor] = []
    for source, items in by_source.items():
        if source in _POOL_EXCLUDED_SOURCES:
            continue
        core.extend(items[: _POOL_PER_SOURCE_CAP.get(source, MAX_TOTAL_ANCHORS)])

    # Curate (dedup + cap) the core first so the "is there signal?" decision
    # is made on de-duplicated anchors, not raw collision-padded counts.
    core.sort(key=_sort_key)
    pool = _dedup_capped(core, MAX_TOTAL_ANCHORS)

    if len(pool) < _FALLBACK_MIN_SIGNAL:
        # Essentially no product signal → admit test titles as a low-trust
        # fallback (capped) rather than ship an empty pool.
        fallback = by_source.get("test", [])[:_TEST_FALLBACK_CAP]
        if fallback:
            merged = pool + fallback
            merged.sort(key=_sort_key)
            pool = _dedup_capped(merged, MAX_TOTAL_ANCHORS)
    return pool


def extract_product_anchors(repo_root: Path) -> list[ProductAnchor]:
    """The CLEAN alignment pool for a repo — what Stage 6.7d consumes.

    Convenience composition of :func:`extract_raw_anchors` →
    :func:`build_alignment_pool`. Deterministic: identical repo tree →
    identical output.
    """
    return build_alignment_pool(extract_raw_anchors(repo_root))


def distinct_tier_counts(anchors: list[ProductAnchor]) -> tuple[int, int]:
    """Distinct (case-insensitive text) tier-1 / tier-2 counts over *anchors*.

    The 6.7d grain gate's input: ``tier1`` measures the size of the repo's
    ACTION-grain product vocabulary. Pure counting — no set ITERATION, so no
    determinism hazard (only ``len`` is read).
    """
    tier1: set[str] = set()
    tier2: set[str] = set()
    for a in anchors or []:
        text = (getattr(a, "text", "") or "").strip().lower()
        if not text:
            continue
        (tier1 if anchor_tier(a) == TIER1_ACTION else tier2).add(text)
    return len(tier1), len(tier2)


def anchor_telemetry(
    anchors: list[ProductAnchor],
    raw: list[ProductAnchor] | None = None,
) -> dict[str, object]:
    """Compact ``scan_meta.product_anchors`` payload (counts + small sample).

    ``anchors`` is the CURATED alignment pool (counts under ``total`` /
    ``by_source`` / ``sample``). When ``raw`` is supplied (the full
    pre-curation extraction) its per-source counts are added under
    ``raw_total`` / ``raw_by_source`` so telemetry shows BOTH the noisy
    extraction and the clean pool. NEVER embeds the full anchor list.
    """
    def _counts(xs: list[ProductAnchor]) -> dict[str, int]:
        by: dict[str, int] = {}
        for a in xs:
            by[a.source] = by.get(a.source, 0) + 1
        return dict(sorted(by.items()))

    t1, t2 = distinct_tier_counts(anchors)
    tel: dict[str, object] = {
        "total": len(anchors),
        "by_source": _counts(anchors),
        "tier1_distinct": t1,
        "tier2_distinct": t2,
        "sample": [a.text for a in anchors[:15]],
    }
    if raw is not None:
        tel["raw_total"] = len(raw)
        tel["raw_by_source"] = _counts(raw)
    return tel


__all__ = [
    "AnchorSource",
    "AnchorTier",
    "TIER1_ACTION",
    "TIER2_ADVISORY",
    "ProductAnchor",
    "anchor_telemetry",
    "anchor_tier",
    "distinct_tier_counts",
    "build_alignment_pool",
    "demote_widget_anchors",
    "is_widget_label",
    "extract_analytics_anchors",
    "extract_docs_anchors",
    "extract_i18n_anchors",
    "extract_nav_anchors",
    "extract_product_anchors",
    "extract_raw_anchors",
    "extract_test_anchors",
]
