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
      1. Root ``package.json#homepage``
      2. Root ``package.json#repository.url`` (only if NOT a code host)
      3. Nested workspace ``package.json#homepage`` — many monorepos
         leave the root manifest minimal and put the customer-facing
         URL on each published package. Scans ``apps/*``, ``packages/*``,
         ``services/*``, ``libs/*`` up to two levels deep.
      4. Git remote ``origin`` → GitHub REST API
         ``/repos/<owner>/<repo>#homepage`` (the "About" sidebar URL
         maintainers configure on GitHub). This is EXTERNAL structured
         metadata, not in-repo prose, so the README rule does not apply.

    Returns ``None`` when no usable signal exists.
    """
    pj = _read_package_json(repo_path)

    # 1. Root homepage
    if pj:
        homepage = pj.get("homepage")
        url = _normalise_url(homepage) if isinstance(homepage, str) else None
        if url:
            return url

        # 2. Root repository.url (only if it isn't a code host)
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

    # 3. Nested workspace package.json
    nested = _discover_from_nested_packages(repo_path)
    if nested:
        return nested

    # 4. GitHub API (.git/config remote → REST homepage field)
    gh = _discover_from_github_metadata(repo_path)
    if gh:
        return gh

    return None


# ── Nested workspace package.json scan ──────────────────────────────────


_WORKSPACE_PARENTS = ("apps", "packages", "services", "libs")


def _discover_from_nested_packages(repo_path: Path) -> str | None:
    """Walk apps/*/ packages/*/ services/*/ libs/*/ and look at each
    package.json for a homepage field. First match wins.

    Capped at the first level under each workspace parent dir for speed
    — going deeper would risk picking up a vendored sub-package URL.
    """
    for parent in _WORKSPACE_PARENTS:
        parent_dir = repo_path / parent
        if not parent_dir.is_dir():
            continue
        try:
            children = sorted(parent_dir.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            pj_path = child / "package.json"
            if not pj_path.is_file():
                continue
            try:
                data = json.loads(pj_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            homepage = data.get("homepage")
            url = _normalise_url(homepage) if isinstance(homepage, str) else None
            if url:
                return url
    return None


# ── GitHub remote-based discovery ───────────────────────────────────────


def _read_git_remote_origin(repo_path: Path) -> str | None:
    """Read .git/config and extract ``remote "origin"`` url, no shell-out."""
    cfg = repo_path / ".git" / "config"
    if not cfg.is_file():
        return None
    try:
        text = cfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    in_origin = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[remote") and '"origin"' in s:
            in_origin = True
            continue
        if s.startswith("[") and in_origin:
            in_origin = False
        if in_origin and s.startswith("url"):
            _, _, val = s.partition("=")
            return val.strip()
    return None


def _parse_github_owner_repo(remote_url: str) -> tuple[str, str] | None:
    """Return ``(owner, repo)`` for a github remote URL, else None."""
    m = _GITHUB_REPO_RE.search(remote_url)
    if not m:
        return None
    return m.group(1), m.group(2)


def _discover_from_github_metadata(repo_path: Path) -> str | None:
    """Fetch the GitHub repository's About URL via the public REST API.

    This is EXTERNAL structured metadata (a single field maintainers
    configure on their GitHub repo settings), NOT in-repo prose. The
    "no README" rule applies to repo-internal documents, not to data
    the maintainer has explicitly published as their product URL.

    Returns ``None`` on any error so callers fall back gracefully.
    """
    remote = _read_git_remote_origin(repo_path)
    if not remote:
        return None
    owner_repo = _parse_github_owner_repo(remote)
    if not owner_repo:
        return None
    owner, repo = owner_repo
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    text = fetch_page_text(api_url, timeout_s=10)
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    homepage = data.get("homepage") if isinstance(data, dict) else None
    return _normalise_url(homepage) if isinstance(homepage, str) else None


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
    """Filter generic nav chrome and obviously non-feature strings.

    Product taxonomy entries are short noun-phrases ("HTTP Uptime
    Monitoring", "Status Page Builder"). Marketing-page H2s often
    contain full pitch sentences ("Close deals faster with secure
    data rooms.") that look superficially like headings but make
    terrible cluster labels. We reject anything that resembles a
    sentence rather than a phrase.
    """
    if not label:
        return False
    # Slightly tighter upper bound — real product labels are short.
    if len(label) < 4 or len(label) > 60:
        return False
    lowered = label.lower()
    if lowered in _GENERIC_NAV_WORDS:
        return False
    # Drop labels that are mostly punctuation / digits.
    word_chars = sum(c.isalpha() for c in label)
    if word_chars < 4:
        return False
    # Token count cap: phrases are 2-6 words. More than 6 = sentence.
    tokens = [t for t in re.split(r"[\s\-/]+", label) if t]
    if len(tokens) < 2 or len(tokens) > 6:
        return False
    # Filter all-caps shouty marketing strings (often CTAs).
    if label.isupper():
        return False
    # Drop URLs and emails that slip through.
    if "://" in label or "@" in label:
        return False
    # Sentence-shape detectors — punctuation that doesn't appear in
    # a product label.
    if label.rstrip().endswith((".", "!", "?")):
        return False
    if "," in label or ";" in label or ":" in label:
        return False
    # Conjunctions in the middle of the string almost always indicate
    # a marketing pitch ("X and Y in seconds") rather than a feature
    # label ("Status Pages").
    lowered_padded = f" {lowered} "
    for conj in (" and ", " or ", " with ", " for ", " in ", " to ",
                 " from ", " your ", " our ", " the "):
        if conj in lowered_padded:
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
