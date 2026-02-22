from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from featuremap.models.types import Commit, Feature, FeatureMap


def detect_features_from_structure(files: list[str]) -> dict[str, list[str]]:
    """
    Detects features based on directory structure.
    This is a heuristic fallback — LLM analysis provides richer results.

    Examples:
        src/auth/login.py      → feature: "auth"
        src/payments/stripe.py → feature: "payments"
        src/api/users.py       → feature: "api"
    """
    features: dict[str, list[str]] = defaultdict(list)

    for file_path in files:
        parts = Path(file_path).parts
        feature_name = _extract_feature_name(parts)
        features[feature_name].append(file_path)

    return dict(features)


def _extract_feature_name(parts: tuple[str, ...]) -> str:
    """
    Extracts a feature name from a file path.

    Logic:
    - src/auth/login.py       → "auth"
    - app/api/payments/...    → "payments"
    - lib/utils/helpers.py    → "utils"
    - index.py                → "root"
    """
    # Skip common top-level wrapper directories (not feature names themselves)
    skip_prefixes = {
        # Generic source roots
        "src", "app", "lib", "pkg", "internal", "core",
        # Frontend structural directories — not business features
        "views", "pages", "screens", "routes", "containers",
        "components", "layouts", "features",
    }

    for i, part in enumerate(parts[:-1]):  # Exclude the filename
        if part.lower() not in skip_prefixes:
            return part.lower()

    # File is at the repo root
    return "root"


def build_feature_map(
    repo_path: str,
    commits: list[Commit],
    feature_paths: dict[str, list[str]],
    days: int,
) -> FeatureMap:
    """Builds a FeatureMap by joining commits with detected features."""

    feature_commits: dict[str, list[Commit]] = defaultdict(list)
    feature_authors: dict[str, set[str]] = defaultdict(set)
    feature_last_modified: dict[str, datetime] = {}

    for commit in commits:
        touched_features = set()

        for file_path in commit.files_changed:
            # Find which feature this file belongs to
            for feature_name, paths in feature_paths.items():
                if file_path in paths:
                    touched_features.add(feature_name)
                    break

        for feature_name in touched_features:
            feature_commits[feature_name].append(commit)
            feature_authors[feature_name].add(commit.author)

            # Track the most recent modification date
            if feature_name not in feature_last_modified or \
               commit.date > feature_last_modified[feature_name]:
                feature_last_modified[feature_name] = commit.date

    features = []
    for feature_name, paths in feature_paths.items():
        commits_for_feature = feature_commits.get(feature_name, [])
        total = len(commits_for_feature)
        bug_fixes = sum(1 for c in commits_for_feature if c.is_bug_fix)
        bug_fix_ratio = bug_fixes / total if total > 0 else 0.0

        features.append(Feature(
            name=feature_name,
            paths=paths,
            authors=list(feature_authors.get(feature_name, set())),
            total_commits=total,
            bug_fixes=bug_fixes,
            bug_fix_ratio=round(bug_fix_ratio, 3),
            last_modified=feature_last_modified.get(
                feature_name,
                datetime.now(tz=timezone.utc)
            ),
            health_score=_calculate_health(bug_fix_ratio, total),
        ))

    return FeatureMap(
        repo_path=repo_path,
        analyzed_at=datetime.now(tz=timezone.utc),
        total_commits=len(commits),
        date_range_days=days,
        features=features,
    )


def _calculate_health(bug_fix_ratio: float, total_commits: int) -> float:
    """
    Calculates a health score from 0 to 100.
    100 = healthy, 0 = high technical debt.

    Formula:
    - Base score decreases with bug fix ratio (ratio 0.5 → score 0)
    - Activity factor adds confidence for well-tested features
    """
    if total_commits == 0:
        return 100.0

    base_score = max(0.0, 100.0 - (bug_fix_ratio * 200))
    activity_factor = min(1.0, total_commits / 50)

    return round(base_score * activity_factor + base_score * (1 - activity_factor) * 0.8, 1)
