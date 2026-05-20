"""Marketing-site fetcher — discover + scrape a repo's PUBLIC product
surface for Stage 8 (Sprint E1).

Reads only EXTERNAL surfaces:

  - ``package.json#homepage`` — when present, this is the maintainer's
    canonical product URL.
  - ``package.json#repository.url`` — used as a hint to guess homepage
    convention when ``#homepage`` is absent.

NEVER reads README.md or any in-repo prose document (per the
project-wide hard rule codified in ``CLAUDE.md``).

Output is a small list of candidate product-feature labels suitable
for grounding a Haiku clustering call in
:mod:`faultline.pipeline_v2.stage_8_marketing_clusterer`.

No new dependencies — uses ``urllib`` from stdlib and a regex-only HTML
parser. The regex parser is intentionally conservative: it extracts
H1/H2/H3 text + obvious "Features" bullet lists, filters generic
navigation chrome, and returns.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ── Public types ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MarketingTaxonomy:
    """Public product surface extracted from marketing site / docs / pricing."""

    repo_slug: str
    source_url: str
    fetched_at: str  # ISO-8601 UTC
    product_features: tuple[str, ...]
    confidence: float
    notes: str


# ── Discovery ───────────────────────────────────────────────────────────


_GITHUB_REPO_RE = re.compile(
    r"github\.com[/:]([^/]+)/([^/.]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)

_VALID_HOST_RE = re.compile(r"^https?://[A-Za-z0-9.\-_]+(?:/.*)?$")


def _read_package_json(repo_path: Path) -> dict | None:
    """Read root ``package.json``; return parsed dict or None."""
    pj = repo_path / "package.json"
    if not pj.is_file():
        return None
    try:
        return json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("marketing_fetcher: cannot parse package.json: %s", exc)
        return None


def _normalise_url(url: str) -> str | None:
    """Strip trailing slashes and validate scheme. Returns None when
    the URL is not a real http(s) URL we want to fetch."""
    if not isinstance(url, str):
        return None
    url = url.strip().rstrip("/")
    if not url:
        return None
    if not _VALID_HOST_RE.match(url):
        return None
    # Block obviously non-marketing destinations.
    lowered = url.lower()
    for bad in (
        "github.com",
        "gitlab.com",
        "bitbucket.org",
        "npmjs.com",
        "pypi.org",
        "crates.io",
    ):
        if bad in lowered:
            return None
    return url


def discover_marketing_site(repo_path: Path) -> str | None:
    """Find the canonical marketing URL for the repo at ``repo_path``.

    Priority:
      1. ``package.json#homepage``
      2. ``package.json#repository.url`` → strip ``.git``, only
         accepted if it does NOT point at github/gitlab/bitbucket
         (those would point at the source-host page, not the marketing
         site).

    Returns ``None`` when no usable signal exists.
    """
    pj = _read_package_json(repo_path)
    if not pj:
        return None

    # 1. homepage
    homepage = pj.get("homepage")
    url = _normalise_url(homepage) if isinstance(homepage, str) else None
    if url:
        return url

    # 2. repository.url (only if it isn't a code host)
    repo = pj.get("repository")
    if isinstance(repo, dict):
        repo_url = repo.get("url")
        if isinstance(repo_url, str):
            cleaned = repo_url.removeprefix("git+").removesuffix(".git")
            url = _normalise_url(cleaned)
            if url:
                return url
    elif isinstance(repo, str):
        cleaned = repo.removeprefix("git+").removesuffix(".git")
        url = _normalise_url(cleaned)
        if url:
            return url

    return None


# ── Fetch ───────────────────────────────────────────────────────────────


_USER_AGENT = (
    "Faultlines/Layer2-Clusterer (+https://faultlines.dev) "
    "compatible scraper"
)


def fetch_page_text(url: str, *, timeout_s: int = 15) -> str | None:
    """Fetch ``url`` with a reasonable timeout + user agent.

    Returns the raw response body on success, ``None`` on any error
    (timeout, non-2xx status, network failure). Caller must handle
    HTML parsing.
    """
    if not _VALID_HOST_RE.match(url):
        return None
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if resp.status >= 400:
                return None
            # Cap to avoid downloading huge pages.
            data = resp.read(2_000_000)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.debug("marketing_fetcher: fetch failed for %s: %s", url, exc)
        return None
    try:
        return data.decode("utf-8", errors="ignore")
    except UnicodeDecodeError:
        return None


# ── HTML → product label candidates ─────────────────────────────────────


# Headings — H1/H2/H3 commonly carry top-level feature names on
# marketing pages. We intentionally ignore H4+ to avoid sub-feature
# noise.
_HEADING_RE = re.compile(
    r"<h([1-3])[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL,
)
# Anchor link text inside sidebar / nav lists — docs sites often
# expose the product taxonomy here.
_LI_A_RE = re.compile(
    r"<li[^>]*>\s*<a[^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
# Generic HTML-tag stripper.
_TAG_RE = re.compile(r"<[^>]+>")

# Words that are 100% chrome/nav noise on marketing pages.
_GENERIC_NAV_WORDS = frozenset({
    "pricing", "docs", "documentation", "blog", "changelog", "about",
    "contact", "support", "help", "faq", "login", "log in", "sign in",
    "sign up", "signup", "get started", "start free", "free trial",
    "demo", "book demo", "request demo", "discord", "github", "twitter",
    "linkedin", "youtube", "company", "team", "careers", "jobs",
    "privacy", "terms", "cookies", "legal", "press", "media", "menu",
    "home", "products", "product", "features", "feature", "solutions",
    "solution", "customers", "case studies", "resources", "developers",
    "api", "download", "install", "download now", "start", "next",
    "previous", "search", "newsletter", "subscribe", "open menu",
    "close menu", "skip to content", "loading", "error",
    "table of contents",
})


def _strip_html(text: str) -> str:
    """Strip tags + collapse whitespace."""
    no_tags = _TAG_RE.sub(" ", text)
    # Decode the most common HTML entities without a full parser.
    no_tags = (
        no_tags.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", no_tags).strip()


def _is_acceptable_label(label: str) -> bool:
    """Filter generic nav chrome and obviously non-feature strings."""
    if not label:
        return False
    if len(label) < 4 or len(label) > 80:
        return False
    lowered = label.lower()
    if lowered in _GENERIC_NAV_WORDS:
        return False
    # Drop labels that are mostly punctuation / digits.
    word_chars = sum(c.isalpha() for c in label)
    if word_chars < 4:
        return False
    # At least 2 word tokens (single-word labels like "Auth" are
    # usually nav chrome; multi-word labels like "Multi-Region Probing"
    # carry product-grain meaning).
    tokens = [t for t in re.split(r"[\s\-/]+", label) if t]
    if len(tokens) < 2:
        return False
    # Filter all-caps shouty marketing strings (often CTAs).
    if label.isupper():
        return False
    # Drop URLs and emails that slip through.
    if "://" in label or "@" in label:
        return False
    return True


def _dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def extract_product_taxonomy(html: str) -> tuple[list[str], float]:
    """From marketing-page HTML, extract candidate product features.

    Returns ``(candidates, confidence)`` where confidence is:

      - 0.9 when ≥6 acceptable headings were found
      - 0.75 when 3–5 were found
      - 0.5 when 1–2 were found
      - 0.0 on empty
    """
    if not html:
        return [], 0.0

    raw_candidates: list[str] = []
    for _level, body in _HEADING_RE.findall(html):
        text = _strip_html(body)
        if _is_acceptable_label(text):
            raw_candidates.append(text)

    # Sidebar / nav anchor lists (docs taxonomy).
    for body in _LI_A_RE.findall(html):
        text = _strip_html(body)
        if _is_acceptable_label(text):
            raw_candidates.append(text)

    unique = _dedupe_preserve_order(raw_candidates)
    # Cap at 30 — more than that and we're probably indexing the
    # entire page, which hurts Haiku precision.
    capped = unique[:30]

    if len(capped) >= 6:
        confidence = 0.9
    elif len(capped) >= 3:
        confidence = 0.75
    elif len(capped) >= 1:
        confidence = 0.5
    else:
        confidence = 0.0

    return capped, confidence


__all__ = [
    "MarketingTaxonomy",
    "discover_marketing_site",
    "fetch_page_text",
    "extract_product_taxonomy",
]
