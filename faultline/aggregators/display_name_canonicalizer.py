"""Display-name canonicalizer (Sprint 5).

Engines emit feature names that are stable internal slugs:
``notification-plugins``, ``user-management``, ``api-access``. Ground
truth — and customer dashboards — want customer-facing Title Case
phrases: ``Plugin Extensibility``, ``User Management``, ``API``.

This aggregator resolves the gap in two passes:

  Pass A (deterministic, no LLM):
    Index author-written nav labels from ``nav-link`` signals
    (``<Link href="/billing">Billing</Link>`` → label "Billing"
    associated with route "/billing"). Match each feature to the
    nav label whose href segment matches the feature's slug.
    Preserve any author-set display_name verbatim.

  Pass B (optional, one batched LLM call):
    For features that Pass A could not resolve, send a single batch
    Haiku call: "Given these slugs and a one-line description each,
    suggest a Title Case display name". Trust the model only when
    its suggestion is a clearly different (and cleaner) phrase.

Pass B is opt-in via ``FAULTLINE_LLM_CANONICALIZE=1`` so the default
scan stays free. Pass A always runs.

Generic per ``memory/rule-no-repo-specific-paths`` and
``memory/rule-no-magic-tuning`` — no per-repo names, no tuning
constants beyond a sensible default batch cap.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

from faultline.signals import Signal

logger = logging.getLogger(__name__)


# Tokens used to decide if an existing display_name was likely
# auto-generated from the slug rather than provided by the LLM.
# These are heuristic and structural — no per-repo names.
_AUTO_GEN_MARKERS = re.compile(r"[-_]")


# Single-word display names that are never acceptable — they're
# folder/tech names leaking through from the slug or paths.
# Generic per rule-no-repo-specific-paths: this is a structural
# vocabulary list, not a per-repo allowlist.
_STRUCTURAL_DISPLAY_BLOCKLIST = frozenset({
    "trpc", "apps", "app", "packages", "package", "remix", "next",
    "nextjs", "nuxt", "astro", "react", "vue", "svelte", "src",
    "lib", "libs", "modules", "examples", "scripts", "server",
    "client", "routes", "routers", "controllers", "models", "views",
    "tests", "vendor", "dist", "build", "bin", "ee", "ce",
    "internal", "common", "core", "shared", "utils", "helpers",
})


def _is_structural_display(name: str) -> bool:
    """True when ``name`` is a single forbidden structural word.
    Multi-word names that *contain* a structural word are fine —
    "API Server Infrastructure" stays even though "Server" alone
    would be rejected.
    """
    parts = re.split(r"\s+", name.strip())
    if len(parts) != 1:
        return False
    return parts[0].lower() in _STRUCTURAL_DISPLAY_BLOCKLIST


def _looks_engine_generated(display_name: str | None, slug: str) -> bool:
    """A display_name is "engine generated" when it's missing OR it's
    structurally identical to the slug (verbatim or Title-Cased).

    The deterministic ``_populate_display_names`` pass in cli.py
    typically Title-Cases the slug (``api-access`` → ``API Access``).
    Such names look fine but should be allowed to be replaced when
    a richer source (nav label / LLM suggestion) is available.

    Pre-display-name-pass features may have ``display_name`` equal
    to the raw slug — also "engine generated" since no transformation
    happened yet.
    """
    if not display_name:
        return True
    dn = display_name.strip()
    # Verbatim match against the slug (no transformation applied).
    if dn == slug.strip():
        return True
    # Title-cased rendering of the slug.
    title_from_slug = " ".join(
        w.capitalize() for w in re.split(r"[-_/\s]+", slug) if w
    )
    if dn.lower() == title_from_slug.lower():
        return True
    # Also flag display_name that is structurally equivalent to the
    # slug after stripping all separators (catches "ApiAccess" vs
    # "api-access" forms).
    if _normalise_token(dn) == _normalise_token(slug):
        return True
    return False


def is_enabled_llm_canonicalize() -> bool:
    return os.environ.get("FAULTLINE_LLM_CANONICALIZE", "").lower() in {
        "1", "true", "yes", "on",
    }


# ── Pass A: nav label match ───────────────────────────────────────────


def _normalise_token(s: str) -> str:
    return re.sub(r"[-_\s/]+", "", s.lower())


def build_nav_label_index(signals: Iterable[Signal]) -> dict[str, str]:
    """Return ``{normalised_segment: best_label}``.

    Walks every ``nav-link`` signal. For each link with a non-empty
    label, the last URL segment becomes the lookup key
    (``/billing/invoices`` → ``invoices``; ``/billing`` → ``billing``).
    Conflicts are resolved by preferring the SHORTEST label
    (presumed canonical) — and on tie alphabetically for
    determinism.
    """
    candidates: dict[str, list[str]] = {}
    for sig in signals:
        if sig.kind != "nav-link":
            continue
        label = sig.payload.get("label")
        href = sig.payload.get("href")
        if not isinstance(label, str) or not label.strip():
            continue
        if not isinstance(href, str) or not href.strip():
            continue
        seg = href.rstrip("/").rsplit("/", 1)[-1]
        key = _normalise_token(seg)
        if not key:
            continue
        candidates.setdefault(key, []).append(label.strip())

    out: dict[str, str] = {}
    for key, labels in candidates.items():
        labels.sort(key=lambda L: (len(L), L))
        out[key] = labels[0]
    return out


def _match_feature_to_nav(
    feature_name: str, nav_index: dict[str, str],
) -> str | None:
    """Try several normalisations of the feature slug against the
    nav label index. Returns the matching label or None.
    """
    # Try the whole slug.
    key = _normalise_token(feature_name)
    if key in nav_index:
        return nav_index[key]
    # Try each path segment of the slug separately (catches
    # "(dashboard)/billing" → "billing").
    for seg in re.split(r"[-_/\s]+", feature_name):
        k = _normalise_token(seg)
        if k and k in nav_index:
            return nav_index[k]
    return None


_PAGE_SUFFIX_RE = re.compile(
    r"\s+(?:Pages?|Screens?|Views?)\s*$", re.IGNORECASE,
)


def strip_page_suffix(feature_map):
    """Sprint 10a — pure-function. Returns ``(new_feature_map,
    changed_count)``. Input ``feature_map`` is NEVER mutated.

    Removes trailing "Page" / "Pages" / "Screen" / "View" from
    display_name (and feature.name when human-shaped). A page
    typically contains MULTIPLE product capabilities — naming a
    feature "Documents Page" hides that.
    """
    new_fm = feature_map.model_copy(deep=True)
    changed = 0
    for feat in new_fm.features:
        dn = getattr(feat, "display_name", None)
        if isinstance(dn, str):
            new_dn = _PAGE_SUFFIX_RE.sub("", dn).strip()
            if new_dn and new_dn != dn:
                feat.display_name = new_dn
                changed += 1
        nm = feat.name
        if " " in nm and not nm.islower():
            new_nm = _PAGE_SUFFIX_RE.sub("", nm).strip()
            if new_nm and new_nm != nm:
                feat.name = new_nm
    return new_fm, changed


def scrub_structural_displays(feature_map):
    """Sprint 10a — pure-function. Returns ``(new_feature_map,
    cleared_count)``. Input ``feature_map`` is NEVER mutated.

    Clears ``display_name`` on any feature whose display_name is a
    single structural folder/tech word (e.g. "Trpc", "Packages",
    "Apps") — those are LLM hallucinations from path content.
    """
    new_fm = feature_map.model_copy(deep=True)
    cleared = 0
    for feat in new_fm.features:
        dn = getattr(feat, "display_name", None)
        if isinstance(dn, str) and _is_structural_display(dn):
            feat.display_name = None
            cleared += 1
    return new_fm, cleared


def apply_nav_labels(
    feature_map, signals: Iterable[Signal],
) -> int:
    """Set ``feature.display_name`` from matching nav labels.

    Returns the number of features whose display_name was updated.
    Skips features whose existing display_name appears to be
    author/LLM-set (not slug-derived).
    """
    nav_index = build_nav_label_index(signals)
    if not nav_index:
        return 0
    updated = 0
    for feat in feature_map.features:
        if not _looks_engine_generated(feat.display_name, feat.name):
            continue
        label = _match_feature_to_nav(feat.name, nav_index)
        if not label:
            continue
        feat.display_name = label
        updated += 1
    return updated


# ── Pass B: LLM batch canonicalization ────────────────────────────────


_LLM_SYSTEM_PROMPT = """\
You produce BUSINESS-LANGUAGE display names for product features
detected by a code scanner. The audience is a non-technical
product manager, founder, or sales person who is reading a
dashboard. They will skim feature names and decide what each one
DOES for their customer.

Each input has a stable internal slug (lowercase, kebab- or
snake-case) and a short description.

Return JSON only — no prose, no markdown fences. Schema:
{
  "names": [
    {"slug": "<exact input slug>", "display_name": "Business Phrase"},
    ...
  ]
}

Rules:
- Display name is 1-4 words. Use the language a CEO/PM/marketing
  person would put on a pricing page or a feature comparison table.
- AVOID implementation jargon. Bad: "Layer", "Service", "Engine",
  "Manager", "Pipeline", "Provider", "Handler", "Adapter",
  "Framework", "Module", "Component", "Token", "Certificate"
  (when used as plumbing). These read as "internal code" not
  "product feature".
- NEVER output a display_name that is just a folder/tech name
  echoed from the path. Forbidden one-word names: "Trpc", "Apps",
  "App", "Packages", "Package", "Remix", "Next", "Nextjs", "Nuxt",
  "Astro", "React", "Vue", "Svelte", "Src", "Lib", "Libs",
  "Modules", "Examples", "Scripts", "Server", "Client", "Routes",
  "Routers", "Controllers", "Models", "Views", "Tests", "Vendor",
  "Dist", "Build", "Bin", "Ee", "Ce", "Internal", "Common",
  "Core", "Shared", "Utils", "Helpers". If the only name you can
  justify is one of these, return ``"display_name": null``.
- PREFER product / outcome words. Good: "Compliance", "Trust",
  "Audit Trail", "Sync", "Workflows", "Insights", "Notifications",
  "Onboarding", "Integrations", "Analytics", "Permissions".
- ONE concept per name. NEVER combine two concepts with "&" or
  "and". Each feature represents EXACTLY ONE product capability.
  Bad: "Compliance & Audit Trail", "Data & Storage", "Sign In &
  Registration", "Teams & Members". Good: "Compliance",
  "Storage", "Sign In", "Teams". If the slug genuinely covers
  two things, pick the dominant one or return null.
- NEVER use "Page", "Pages", "Screen", "View", "Section" as a
  suffix. A page typically contains MULTIPLE product capabilities;
  naming a feature "Documents Page" hides that. Bad:
  "Documents Page", "Settings Page", "Authentication Pages".
  Good: "Documents", "Settings", "Authentication". If the slug
  contains "page", strip it.
- Translate technical concepts into business language:
    "Document Certificate"             → "Compliance"
    "Background Job Queue"             → "Background Tasks"
    "Prisma Database Layer"            → "Storage"
    "RPC Framework"                    → "API"
    "File Storage Service"             → "File Storage"
    "Transactional Email"              → "Email Notifications"
    "AI Integration"                   → "AI Assistant"
- If the slug already reads as a business phrase ("Billing", "Auth",
  "Notifications") and you have no improvement, return
  ``"display_name": null`` for that slug — do NOT echo the input.
- Allowed abbreviations only when universal: API, OAuth, SSO, MFA,
  PDF, CSV, JSON, OTP. Anything else write out fully.
- Never invent capabilities the description doesn't support.
"""


def _build_llm_batch_user(
    items: list[tuple[str, str]],   # (slug, description)
) -> str:
    obj = {
        "features": [
            {"slug": slug, "description": (desc or "")[:240]}
            for slug, desc in items
        ],
    }
    return "Features needing Title Case display names:\n\n" + json.dumps(
        obj, indent=2, ensure_ascii=False,
    )


def _parse_llm_response(raw: str) -> dict[str, str]:
    """Parse the JSON-only response into ``{slug: display_name}``."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("display-canonicalizer: LLM produced invalid JSON")
        return {}
    out: dict[str, str] = {}
    for entry in data.get("names", []) or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        name = entry.get("display_name")
        if isinstance(slug, str) and isinstance(name, str) and name.strip():
            out[slug] = name.strip()
    return out


def apply_llm_canonicalization(
    feature_map, llm, max_batch: int = 30,
) -> int:
    """Optional batched Haiku pass. Only canonicalizes features
    whose display_name still looks engine-generated AFTER Pass A.
    Returns number of names updated.
    """
    pending = [
        f for f in feature_map.features
        if _looks_engine_generated(f.display_name, f.name)
    ]
    if not pending:
        return 0

    items = [(f.name, f.description or "") for f in pending[:max_batch]]
    user = _build_llm_batch_user(items)
    try:
        response = llm.complete(
            system=_LLM_SYSTEM_PROMPT,
            user=user,
            max_tokens=2048,
        )
    except Exception:  # noqa: BLE001 — opportunistic
        logger.warning("display-canonicalizer: LLM call failed; skipping")
        return 0

    names = _parse_llm_response(response.text)
    if not names:
        return 0

    updated = 0
    for feat in feature_map.features:
        suggestion = names.get(feat.name)
        if not suggestion:
            continue
        if not _looks_engine_generated(feat.display_name, feat.name):
            continue
        # Only adopt the suggestion when it's MEANINGFULLY different
        # from the slug — not just a re-spaced version.
        slug_norm = _normalise_token(feat.name)
        sugg_norm = _normalise_token(suggestion)
        if sugg_norm == slug_norm:
            continue
        if _is_structural_display(suggestion):
            continue
        feat.display_name = suggestion
        updated += 1
    return updated


__all__ = [
    "apply_llm_canonicalization",
    "apply_nav_labels",
    "build_nav_label_index",
    "is_enabled_llm_canonicalize",
    "scrub_structural_displays",
    "strip_page_suffix",
]
