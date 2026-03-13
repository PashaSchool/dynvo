"""Generate and post a FeatureMap PR comment via the GitHub REST API.

Reads the feature-map.json produced by ``faultline analyze``, filters
features to those touched by the PR, renders a Markdown comment with
health table, hotspot warnings, and bus-factor alerts, then posts or
updates a single comment on the pull request.

Environment variables (injected by action.yml):
    FEATURE_MAP_PATH   -- path to feature-map.json
    CHANGED_FILES      -- colon-separated PR changed file paths
    GITHUB_TOKEN       -- GitHub token for API auth
    GITHUB_REPOSITORY  -- "owner/repo"
    PR_NUMBER          -- pull request number
    REMOTE_URL         -- repository HTML URL
    FAIL_BELOW         -- (optional) minimum health score threshold
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMMENT_MARKER = "<!-- featuremap-pr-comment -->"
GITHUB_API = "https://api.github.com"

_GREEN_THRESHOLD = 75.0
_YELLOW_THRESHOLD = 50.0
_BUS_FACTOR_WARN = 2
_TREND_THRESHOLD = 0.05


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_feature_map(path: str) -> dict:
    """Load and return the raw feature-map JSON."""
    return json.loads(Path(path).read_text())


def parse_changed_files(raw: str) -> set[str]:
    """Parse colon-separated file list into a set of paths."""
    return {f for f in raw.split(":") if f.strip()}


def filter_touched_features(
    features: list[dict],
    changed: set[str],
) -> list[dict]:
    """Return features whose paths overlap with the PR changed files."""
    touched = []
    for feature in features:
        if set(feature.get("paths", [])) & changed:
            touched.append(feature)
    return touched


# ---------------------------------------------------------------------------
# Per-feature metric helpers
# ---------------------------------------------------------------------------

def _feature_bus_factor(feature: dict) -> int:
    """Minimum bus factor across flows, or author-count based estimate."""
    factors = [
        fl["bus_factor"]
        for fl in feature.get("flows", [])
        if "bus_factor" in fl
    ]
    if factors:
        return min(factors)
    authors = feature.get("authors", [])
    return min(len(authors), 3) if authors else 1


def _feature_health_trend(feature: dict) -> float | None:
    """Average health trend from flows; None when unavailable."""
    trends = [
        fl["health_trend"]
        for fl in feature.get("flows", [])
        if fl.get("health_trend") is not None
    ]
    if not trends:
        return None
    return round(sum(trends) / len(trends), 3)


def _collect_hotspot_files(
    features: list[dict],
    changed: set[str],
) -> list[dict]:
    """Collect PR-changed files that are hotspots in touched features.

    Returns a list of dicts with ``path``, ``feature``, ``bug_fix_ratio``.
    """
    hotspots: list[dict] = []
    seen: set[str] = set()

    for feature in features:
        for flow in feature.get("flows", []):
            bug_ratio = flow.get("bug_fix_ratio", 0)
            for path in flow.get("hotspot_files", []):
                if path in changed and path not in seen:
                    seen.add(path)
                    hotspots.append({
                        "path": path,
                        "feature": feature["name"],
                        "bug_fix_ratio": bug_ratio,
                    })

        if not feature.get("flows"):
            ratio = feature.get("bug_fix_ratio", 0)
            if ratio < 0.4:
                continue
            for path in feature.get("paths", []):
                if path in changed and path not in seen:
                    seen.add(path)
                    hotspots.append({
                        "path": path,
                        "feature": feature["name"],
                        "bug_fix_ratio": ratio,
                    })

    return sorted(hotspots, key=lambda h: h["bug_fix_ratio"], reverse=True)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _health_icon(score: float) -> str:
    if score >= _GREEN_THRESHOLD:
        return "\U0001f7e2"  # green circle
    if score >= _YELLOW_THRESHOLD:
        return "\U0001f7e1"  # yellow circle
    return "\U0001f534"  # red circle


def _trend_icon(trend: float | None) -> str:
    if trend is None:
        return "\u2014"
    if trend > _TREND_THRESHOLD:
        return "\u2191"
    if trend < -_TREND_THRESHOLD:
        return "\u2193"
    return "\u2192"


def _sanitize_md(text: str) -> str:
    """Escape pipe and newline characters for Markdown table cells."""
    return text.replace("|", "\\|").replace("\n", " ").replace("\r", "")


def _render_features_table(features: list[dict]) -> str:
    """Render a Markdown table of features sorted by risk."""
    lines = [
        "| Feature | Health | Bug % | Commits | Authors | Bus Factor | Trend |",
        "|---------|--------|-------|---------|---------|------------|-------|",
    ]
    for f in sorted(features, key=lambda x: x["bug_fix_ratio"], reverse=True):
        icon = _health_icon(f["health_score"])
        trend = _trend_icon(_feature_health_trend(f))
        bus = _feature_bus_factor(f)
        bug_pct = f'{f["bug_fix_ratio"] * 100:.1f}%'
        name = _sanitize_md(f["name"])
        lines.append(
            f"| {name} "
            f"| {icon} {f['health_score']:.0f} "
            f"| {bug_pct} "
            f"| {f['total_commits']} "
            f"| {len(f.get('authors', []))} "
            f"| {bus} "
            f"| {trend} |"
        )
    return "\n".join(lines)


def _render_hotspots(
    hotspots: list[dict],
    remote_url: str,
) -> str:
    """Render the hotspot files section."""
    if not hotspots:
        return ""

    lines = ["### Hotspot files in this PR", ""]
    for h in hotspots[:10]:
        pct = f'{h["bug_fix_ratio"] * 100:.0f}%'
        if remote_url:
            link = f'[`{h["path"]}`]({remote_url}/blob/HEAD/{h["path"]})'
        else:
            link = f'`{h["path"]}`'
        lines.append(
            f"- {link} \u2014 {pct} bug fix ratio "
            f"(feature: **{h['feature']}**)"
        )
    return "\n".join(lines)


def _render_bus_factor_warnings(features: list[dict]) -> str:
    """Render bus factor warnings for at-risk features."""
    warnings: list[str] = []
    for f in features:
        bus = _feature_bus_factor(f)
        if bus < _BUS_FACTOR_WARN:
            authors = f.get("authors", [])
            who = f'(`{"`, `".join(authors[:3])}`)'  if authors else ""
            warnings.append(
                f"\u26a0\ufe0f **{_sanitize_md(f['name'])}**: only {bus} author(s) "
                f"know this feature {who} \u2014 knowledge concentration risk"
            )
    if not warnings:
        return ""
    return "### Bus factor warnings\n\n" + "\n\n".join(warnings)


def render_comment(
    fm: dict,
    touched: list[dict],
    changed: set[str],
    remote_url: str,
) -> str:
    """Render the full PR comment Markdown."""
    features = fm.get("features", [])
    total_commits = fm.get("total_commits", 0)
    days = fm.get("date_range_days", 365)

    summary = (
        f"> Analyzed **{total_commits:,}** commits over **{days}** days "
        f"\u00b7 **{len(features)}** features detected "
        f"\u00b7 **{len(touched)}** touched by this PR"
    )

    if touched:
        touched_table = _render_features_table(touched)
    else:
        touched_table = "_No tracked features were touched by this PR._"

    hotspots = _collect_hotspot_files(touched, changed)
    hotspots_section = _render_hotspots(hotspots, remote_url)
    bus_section = _render_bus_factor_warnings(touched)

    all_table = _render_features_table(features)

    parts = [
        COMMENT_MARKER,
        "## FeatureMap Analysis",
        "",
        summary,
        "",
        "### Features touched by this PR",
        "",
        touched_table,
    ]

    if hotspots_section:
        parts += ["", hotspots_section]

    if bus_section:
        parts += ["", bus_section]

    parts += [
        "",
        "---",
        "<details>",
        f"<summary>All {len(features)} features</summary>",
        "",
        all_table,
        "",
        "</details>",
        "",
        '<sub>Generated by <a href="https://github.com/pkuzina/faultline">FeatureMap</a></sub>',
    ]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

def _github_request(
    method: str,
    url: str,
    token: str,
    body: dict | None = None,
) -> dict | list:
    """Make a GitHub API request and return parsed JSON."""
    data = json.dumps(body).encode() if body else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode(errors="replace")
        print(f"GitHub API error {exc.code}: {err_body}", file=sys.stderr)
        raise


def find_existing_comment(repo: str, pr: int, token: str) -> int | None:
    """Find the featuremap comment on the PR by marker. Returns comment ID."""
    page = 1
    while True:
        url = (
            f"{GITHUB_API}/repos/{repo}/issues/{pr}/comments"
            f"?per_page=100&page={page}"
        )
        comments = _github_request("GET", url, token)
        if not isinstance(comments, list) or not comments:
            break
        for comment in comments:
            if COMMENT_MARKER in comment.get("body", ""):
                return comment["id"]
        if len(comments) < 100:
            break
        page += 1
    return None


def post_or_update_comment(
    repo: str,
    pr: int,
    token: str,
    body: str,
    comment_id: int | None,
) -> None:
    """Create a new comment or update the existing one."""
    if comment_id:
        url = f"{GITHUB_API}/repos/{repo}/issues/comments/{comment_id}"
        _github_request("PATCH", url, token, {"body": body})
        print(f"Updated existing comment #{comment_id}")
    else:
        url = f"{GITHUB_API}/repos/{repo}/issues/{pr}/comments"
        result = _github_request("POST", url, token, {"body": body})
        print(f"Created comment #{result.get('id', '?')}")


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------

def check_quality_gate(
    touched: list[dict],
    threshold: float,
) -> list[dict]:
    """Return features that fail the health threshold."""
    return [f for f in touched if f["health_score"] < threshold]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    feature_map_path = os.environ.get("FEATURE_MAP_PATH", "")
    changed_raw = os.environ.get("CHANGED_FILES", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number_raw = os.environ.get("PR_NUMBER", "")
    fail_below_raw = os.environ.get("FAIL_BELOW", "").strip()
    remote_url = os.environ.get("REMOTE_URL", "").rstrip("/")

    if not feature_map_path or not Path(feature_map_path).exists():
        print(f"Feature map not found: {feature_map_path}", file=sys.stderr)
        sys.exit(1)

    fm = load_feature_map(feature_map_path)
    changed = parse_changed_files(changed_raw)
    features = fm.get("features", [])
    touched = filter_touched_features(features, changed)

    print(f"PR touches {len(touched)} of {len(features)} features")

    if token and repo and pr_number_raw.isdigit():
        pr = int(pr_number_raw)
        comment_body = render_comment(fm, touched, changed, remote_url)
        comment_id = find_existing_comment(repo, pr, token)
        post_or_update_comment(repo, pr, token, comment_body, comment_id)
    else:
        print("Skipping PR comment (not in PR context or missing token)")

    if fail_below_raw:
        try:
            threshold = float(fail_below_raw)
        except ValueError:
            print(
                f"Invalid fail-on-health-below: {fail_below_raw!r}",
                file=sys.stderr,
            )
            sys.exit(1)

        failing = check_quality_gate(touched, threshold)
        if failing:
            names = ", ".join(f["name"] for f in failing)
            scores = ", ".join(f'{f["health_score"]:.0f}' for f in failing)
            print(
                f"::error::Quality gate failed: features [{names}] "
                f"have health [{scores}] below threshold {threshold}",
            )
            sys.exit(1)

    print("FeatureMap analysis complete")


if __name__ == "__main__":
    main()
