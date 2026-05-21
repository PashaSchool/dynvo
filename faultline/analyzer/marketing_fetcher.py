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


# Repo name may contain dots (cal.com, trigger.dev). Old regex used
# ``[^/.]`` which silently rejected such repos. M1 fix: accept dots.
_GITHUB_REPO_RE = re.compile(
    r"github\.com[/:]([^/]+)/([A-Za-z0-9._\-]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)

_VALID_HOST_RE = re.compile(r"^https?://[A-Za-z0-9.\-_]+(?:/.*)?$")

# Repo names that themselves look like a TLD-bearing domain
# (cal.com / trigger.dev / plane.so) — the maintainer almost always
# owns ``https://<repo-name>/`` as the canonical product site, even
# when GitHub's homepage field points elsewhere (e.g. cal.com → cal.diy
# fork). M1 preflight: try the domain-shaped guess BEFORE GH API.
_DOMAIN_LIKE_REPO_RE = re.compile(
    r"^[a-z0-9][a-z0-9-]*\.[a-z]{2,}$", re.IGNORECASE,
)


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

    M1 preflight: if the repo name itself parses as a domain
    (``cal.com``, ``trigger.dev``, ``plane.so``), try
    ``https://<repo-name>/`` FIRST. Maintainers nearly always own the
    URL their repo is named after; the GH "homepage" field can point
    at a fork (cal.com → cal.diy) or be unset, so prefer the
    domain-shaped guess when it returns usable HTML (≥4KB).

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

    # Domain-shape preflight: cal.com → https://cal.com/, trigger.dev →
    # https://trigger.dev/. Validated by fetching once to confirm the
    # host actually serves HTML.
    repo_lower = repo.lower()
    if _DOMAIN_LIKE_REPO_RE.match(repo_lower):
        guess = f"https://{repo_lower}"
        probe = fetch_page_text(guess, timeout_s=10)
        if probe and len(probe) >= 4000:
            normalised = _normalise_url(guess)
            if normalised:
                return normalised

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
    # M1 — universal pitch-verb-prefix detector. Marketing H1s very
    # often open with an imperative verb ("Turn X into Y", "Wrangle
    # chaos", "Build faster"). Real product labels don't start with
    # these. Universal — these are common-English verb roots, not
    # repo-specific strings.
    first_token = tokens[0].lower() if tokens else ""
    if first_token in _PITCH_VERB_PREFIXES:
        return False
    return True


# Verb roots that overwhelmingly start a marketing pitch sentence
# (imperative form) rather than a product label. Universal list —
# no repo-specific strings.
_PITCH_VERB_PREFIXES = frozenset({
    "turn", "wrangle", "make", "build", "run", "see", "try",
    "find", "discover", "create", "enable", "start", "stop",
    "save", "ship", "scale", "transform", "deliver", "boost",
    "unlock", "automate", "streamline", "simplify", "accelerate",
    "avoid", "eliminate", "reduce", "increase", "close", "open",
    "join", "let", "watch", "listen", "read", "learn", "explore",
    "meet", "skip", "experience",
})


def _accept_single_word_section_header(header: str) -> bool:
    """Relaxed acceptance for section headers that are SINGLE words.

    Used only when the section has ≥``_SECTION_ROLLUP_MIN`` children —
    the structural cardinality proves the header is a real capability
    grouping. The default :func:`_is_acceptable_label` requires ≥2
    tokens which rejects perfectly good labels like ``Authentication``,
    ``Plugins``, ``Adapters``.

    Still rejects: obviously generic nav words ("Home", "Features"),
    overly long strings, all-caps, anything with punctuation.
    """
    if not header:
        return False
    if len(header) < 4 or len(header) > 40:
        return False
    lowered = header.lower()
    if lowered in _GENERIC_NAV_WORDS:
        return False
    # Single token must be alphabetic — no urls, no numbers.
    tokens = [t for t in re.split(r"[\s\-/]+", header) if t]
    if len(tokens) > 2:  # Caller already handled the ≥2 case via _is_acceptable_label
        return False
    if header.isupper():
        return False
    if "://" in header or "@" in header:
        return False
    if header.rstrip().endswith((".", "!", "?")):
        return False
    if "," in header or ";" in header or ":" in header:
        return False
    word_chars = sum(c.isalpha() for c in header)
    return word_chars >= 4


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


# ── Sitemap-driven URL discovery (M1) ───────────────────────────────────


_SITEMAP_LOC_RE = re.compile(r"<loc>([^<]+)</loc>", re.IGNORECASE)

# URL substrings that signal "not a product page" — blog posts,
# changelog entries, legal pages, asset files. Used to filter
# sitemap.xml entries before we try to fetch each one.
_SITEMAP_NOISE = (
    "/blog/", "/changelog/", "/news/", "/.well-known/", "/static/",
    "/assets/", "/api/", "/feed", "/rss", ".xml", ".js", ".css",
    ".png", ".jpg", ".svg", "/cdn-cgi/", "/legal", "/privacy",
    "/terms", "/cookie", "/_next/", "/wp-content/",
)

# Keywords in a URL path that suggest the page describes a product
# capability. Used to rank sitemap URLs so we fetch the most
# promising 8-10 first.
_PRODUCT_PATH_KEYWORDS = (
    "feature", "product", "capabilit", "pricing", "platform",
    "solutions", "use-case", "tour",
)


def fetch_sitemap_urls(
    primary: str,
    *,
    max_urls: int = 60,
) -> list[str]:
    """Return same-host product-page URLs harvested from sitemap.xml.

    Looks at ``<primary>/sitemap.xml``, ``/sitemap_index.xml``, and
    ``/sitemap-0.xml`` (covers Next.js, Hugo, Gatsby, Astro, Wordpress,
    custom). If the first URL is an index pointing at nested sitemaps,
    expand ONE level (avoids fan-out cost). Returns sitemap entries
    that match the primary host (or ``docs.<host>``) and don't contain
    obvious noise paths.

    Empty list on any failure.
    """
    if not _VALID_HOST_RE.match(primary):
        return []
    base = primary.rstrip("/")
    parsed_primary = primary.split("://", 1)[1]
    primary_host = parsed_primary.split("/", 1)[0].removeprefix("www.")

    sm_urls_to_try = [
        f"{base}/sitemap.xml",
        f"{base}/sitemap_index.xml",
        f"{base}/sitemap-0.xml",
    ]
    locs: list[str] = []
    for sm_url in sm_urls_to_try:
        text = fetch_page_text(sm_url, timeout_s=10)
        if not text:
            continue
        locs.extend(_SITEMAP_LOC_RE.findall(text))
        if locs:
            break
    if not locs:
        return []

    # Sitemap-index style: first <loc> entries point at nested
    # sitemaps. Expand the first one only.
    if all(u.endswith(".xml") for u in locs[:3]):
        text = fetch_page_text(locs[0], timeout_s=10)
        if text:
            locs = _SITEMAP_LOC_RE.findall(text)

    same_host: list[str] = []
    for u in locs:
        if "://" not in u:
            continue
        try:
            u_host = u.split("://", 1)[1].split("/", 1)[0].removeprefix("www.")
        except (IndexError, ValueError):
            continue
        if u_host != primary_host and u_host != f"docs.{primary_host}":
            continue
        lo = u.lower()
        if any(n in lo for n in _SITEMAP_NOISE):
            continue
        same_host.append(u)

    return same_host[:max_urls]


def rank_sitemap_urls_by_product_likelihood(urls: list[str]) -> list[str]:
    """Sort sitemap URLs so likely product pages come first.

    Universal heuristic: URLs whose path contains a product keyword
    (``/features``, ``/pricing``, ``/platform`` …) outrank others; ties
    broken by shorter paths (top-of-site pages are usually overviews).
    """
    def sort_key(u: str) -> tuple[int, int]:
        lo = u.lower()
        keyword_hit = 0 if any(k in lo for k in _PRODUCT_PATH_KEYWORDS) else 1
        depth = len(u.split("?", 1)[0].split("/")) - 3  # subtract scheme + host
        return (keyword_hit, max(depth, 0))

    return sorted(urls, key=sort_key)


__all__ = [
    "MarketingTaxonomy",
    "discover_marketing_site",
    "fetch_page_text",
    "extract_product_taxonomy",
    "fetch_sitemap_urls",
    "rank_sitemap_urls_by_product_likelihood",
    "fetch_llms_txt_urls",
    "parse_llms_txt",
    "extract_docs_sidebar_taxonomy",
]


# ── llms.txt / llms-full.txt discovery (Sprint v7-A) ────────────────────


# Minimum bullets under a section header before we collapse them to the
# header. ≥4 sibling bullets is a strong signal that the section IS the
# user-facing capability and the bullets are mere sub-options
# (e.g. "OAuth Providers" → discord/github/google/... → emit only the
# header). Structural, scale-invariant — no magic-number tuning.
_SECTION_ROLLUP_MIN = 4


def fetch_llms_txt_urls(primary: str) -> list[str]:
    """Return candidate llms.txt / llms-full.txt URLs to try.

    The llms.txt convention (https://llmstxt.org) puts a curated
    product taxonomy at predictable paths. Many docs sites
    (Anthropic, Vercel, Mintlify, Trigger) publish one; the file is
    plain markdown with ``## Section`` headers + bullets, which is
    the ideal shape for our Layer 2 grounding (no HTML parsing,
    sections naturally roll up).

    Order:
        1. ``<primary>/llms.txt``
        2. ``<primary>/llms-full.txt``
        3. ``https://docs.<host>/llms.txt``
        4. ``https://docs.<host>/llms-full.txt``
    """
    if not primary or not _VALID_HOST_RE.match(primary):
        return []
    base = primary.rstrip("/")
    # Prefer ``llms-full.txt`` over ``llms.txt`` when both exist —
    # llms-full.txt is the comprehensive expansion (full content) while
    # llms.txt is often a short summary or TOC. Spec calls for either,
    # but trigger.dev / Anthropic publish a 2KB summary at llms.txt and
    # the rich taxonomy is in llms-full.txt; better-auth's llms.txt IS
    # already comprehensive so either works there.
    out = [
        f"{base}/llms-full.txt",
        f"{base}/llms.txt",
    ]
    import urllib.parse as _urlparse
    parsed = _urlparse.urlparse(primary)
    host = parsed.netloc
    if host and not host.startswith("docs."):
        host_clean = host.removeprefix("www.")
        out.append(f"{parsed.scheme}://docs.{host_clean}/llms-full.txt")
        out.append(f"{parsed.scheme}://docs.{host_clean}/llms.txt")
    # Dedupe preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for u in out:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    return deduped


# Markdown section header (## / ### only — # is usually the page title
# which is too generic to be a product feature).
_MD_SECTION_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)
# Markdown bullet — captures the link-text when bullet has [text](url)
# shape; otherwise the entire bullet body. Designed to be tolerant of
# trailing description text after the link (``- [Apple](/p): provider``).
_MD_BULLET_LINK_RE = re.compile(
    r"^\s*[-*+]\s+\[([^\]]+)\]\([^)]+\)",
    re.MULTILINE,
)
_MD_BULLET_PLAIN_RE = re.compile(
    r"^\s*[-*+]\s+(?!\[)([^\n]+?)\s*$",
    re.MULTILINE,
)


def parse_llms_txt(text: str) -> tuple[list[str], float]:
    """Extract product-feature labels from llms.txt markdown content.

    Algorithm (universal, no per-repo tuning):
      1. Walk top-level sections (## / ###). Each section's body is
         scanned for bullet items.
      2. If a section has ≥ ``_SECTION_ROLLUP_MIN`` bullets, EMIT only
         the section header — the bullets are sub-options under one
         capability (the "35 OAuth providers" pattern).
      3. Otherwise emit the section header AND each acceptable
         bullet — the section is sparse so its leaves are themselves
         features.
      4. Bullet labels are passed through ``_is_acceptable_label``
         (re-used from the HTML harvester for filter symmetry).

    Returns ``(labels, confidence)``. Confidence uses the same ladder
    as ``extract_product_taxonomy``.
    """
    if not text:
        return [], 0.0

    # Split into section blocks. Use the regex to find each ## / ###
    # boundary plus the body between this match and the next.
    matches = list(_MD_SECTION_RE.finditer(text))
    if not matches:
        return [], 0.0

    raw_labels: list[str] = []

    for i, m in enumerate(matches):
        header = _strip_html(m.group(2)).strip()
        if not header:
            continue
        # Section body = text from end of this header to start of next
        # header (or end of text).
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]

        # Count ALL well-formed bullets (link or plain) so the rollup
        # decision is based on actual sibling cardinality, not on
        # whether each bullet's label looks like a 2-6-word phrase.
        # The label-acceptability filter only applies when EMITTING
        # leaves; counting uses raw structure.
        link_labels = [
            _strip_html(m.group(1)).strip()
            for m in _MD_BULLET_LINK_RE.finditer(body)
            if m.group(1)
        ]
        plain_labels = [
            _strip_html(m.group(1)).strip()
            for m in _MD_BULLET_PLAIN_RE.finditer(body)
            if m.group(1)
        ]
        bullet_count = len(link_labels) + len(plain_labels)
        emittable_leaves = [
            lbl for lbl in (link_labels + plain_labels)
            if lbl and _is_acceptable_label(lbl)
        ]

        # Rollup logic.
        if bullet_count >= _SECTION_ROLLUP_MIN:
            # Many siblings → emit the header only (per Sprint v7-A
            # spec — collapses 35 OAuth providers into one anchor).
            # Force-accept the header if the count is strong even when
            # the section name itself fails the acceptable-label gate
            # (it's a single word like "Authentication"), because the
            # structural signal is unambiguous.
            if _is_acceptable_label(header):
                raw_labels.append(header)
            elif _accept_single_word_section_header(header):
                raw_labels.append(header)
        else:
            # Sparse → emit each leaf (bullets carry the meaning) plus
            # the header when it's acceptable AND not already a bullet.
            if _is_acceptable_label(header):
                raw_labels.append(header)
            raw_labels.extend(emittable_leaves)

    unique = _dedupe_preserve_order(raw_labels)
    capped = unique[:30]
    if len(capped) >= 6:
        confidence = 0.95  # llms.txt is the maintainer's curated list
    elif len(capped) >= 3:
        confidence = 0.85
    elif len(capped) >= 1:
        confidence = 0.6
    else:
        confidence = 0.0

    return capped, confidence


# ── /docs HTML sidebar parser (Sprint v7-A) ─────────────────────────────


# Match navigation anchors regardless of <li> wrapping — Mintlify and
# Nextra commonly render sidebar items as flat <a> nodes inside
# <nav>/<aside>. We bound search to a sidebar / navigation block so we
# don't pick up footer links.
_SIDEBAR_BLOCK_RE = re.compile(
    r'<(?:nav|aside)[^>]*?(?:class|id)\s*=\s*["\'][^"\']*'
    r'(?:sidebar|nav|menu|toc|side|docs-nav|navigation)[^"\']*["\'][^>]*>'
    r'(.*?)</(?:nav|aside)>',
    re.IGNORECASE | re.DOTALL,
)
# A div with sidebar-ish class — Docusaurus uses this shape.
_SIDEBAR_DIV_RE = re.compile(
    r'<div[^>]*?class\s*=\s*["\'][^"\']*'
    r'(?:theme-doc-sidebar|sidebar|nav-tree|menu__list|docs-sidebar)'
    r'[^"\']*["\'][^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
# Sidebar section group — captures the section title (in <h2>/<h3> /
# <summary> / <button>) and the body that follows. Used to group child
# anchors under a parent header for the rollup decision.
_SIDEBAR_SECTION_RE = re.compile(
    r'<(?:h2|h3|h4|summary|button)[^>]*>(.*?)</(?:h2|h3|h4|summary|button)>'
    r'(.*?)(?=<(?:h2|h3|h4|summary|button)[^>]*>|$)',
    re.IGNORECASE | re.DOTALL,
)
_ANY_A_TEXT_RE = re.compile(r'<a[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)


def _harvest_sidebar_blocks(html: str) -> list[str]:
    """Return the textual content of every sidebar-ish block found."""
    blocks: list[str] = []
    blocks.extend(_SIDEBAR_BLOCK_RE.findall(html))
    blocks.extend(_SIDEBAR_DIV_RE.findall(html))
    return blocks


def extract_docs_sidebar_taxonomy(html: str) -> tuple[list[str], float]:
    """Extract product-feature labels from a docs page's sidebar nav.

    Algorithm:
      1. Locate ALL sidebar blocks (nav/aside/sidebar-class divs).
      2. Within each block, split into sections by their header
         element (h2/h3/h4/summary/button) → list of (header, body).
      3. Apply the SAME rollup rule as ``parse_llms_txt``: section
         with ≥ ``_SECTION_ROLLUP_MIN`` anchors → emit only the
         section header; otherwise emit each anchor + the header.
      4. Labels run through ``_is_acceptable_label``.

    Returns ``(labels, confidence)``. Sparse pages (where the regex
    finds no sidebar block) return ``([], 0.0)`` and the caller falls
    back to the generic ``extract_product_taxonomy``.
    """
    if not html:
        return [], 0.0

    blocks = _harvest_sidebar_blocks(html)
    if not blocks:
        return [], 0.0

    raw_labels: list[str] = []

    for block in blocks:
        sections = _SIDEBAR_SECTION_RE.findall(block)
        if not sections:
            # No headers — treat the whole block as one section without
            # a name; just emit all anchors (capped by _is_acceptable_label).
            for m in _ANY_A_TEXT_RE.findall(block):
                label = _strip_html(m)
                if _is_acceptable_label(label):
                    raw_labels.append(label)
            continue

        for header_raw, body in sections:
            header = _strip_html(header_raw).strip()
            anchor_count = 0
            emittable_anchors: list[str] = []
            for m in _ANY_A_TEXT_RE.findall(body):
                label = _strip_html(m).strip()
                if not label:
                    continue
                anchor_count += 1
                if _is_acceptable_label(label):
                    emittable_anchors.append(label)

            if anchor_count >= _SECTION_ROLLUP_MIN:
                if _is_acceptable_label(header):
                    raw_labels.append(header)
                elif _accept_single_word_section_header(header):
                    raw_labels.append(header)
            else:
                if _is_acceptable_label(header):
                    raw_labels.append(header)
                raw_labels.extend(emittable_anchors)

    unique = _dedupe_preserve_order(raw_labels)
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

