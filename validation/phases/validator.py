"""Phase 3: Validation agents — verify faultline results via Claude."""

import json
import os
from pathlib import Path

import anthropic
from pydantic import BaseModel

from ..config import REPOS_DIR, VALIDATION_MODEL
from ..models import FeatureMatch, PhaseStatus, RepoTarget, ValidationResult
from ..progress import ProgressTracker

_SYSTEM_PROMPT = """\
You are a code analysis validation agent. You verify whether a feature detection \
tool correctly identified business features in a codebase.

You will receive:
1. The detected features (name, file count, health score, bug fix ratio)
2. The expected features the tool should have found
3. A sample of the repository's actual file tree

Your job:
- Match each detected feature to an expected feature (fuzzy name matching is OK)
- Identify missed features (expected but not detected)
- Identify spurious features (detected but don't represent real business domains)
- Flag suspicious metrics (e.g. health score inconsistent with bug fix ratio)
- Provide confidence scores (0-1) for each match

Be generous with matching: "user-auth" matches "auth", "payment-processing" matches "payments".
"""

_USER_PROMPT = """\
## Repository: {repo_name}
{reason}

## Expected features:
{expected_list}

## Detected features:
{detected_list}

## File tree sample (first 300 files):
{file_tree}

Match detected features to expected ones. Identify missed and spurious features.
Flag any metric inconsistencies.
"""


class _ValidationResponse(BaseModel):
    matched_features: list[FeatureMatch]
    missed_features: list[str]
    spurious_features: list[str]
    metric_issues: list[str]
    reasoning: str


def validate_repo(
    repo: RepoTarget,
    feature_map_path: Path,
    progress: ProgressTracker,
    api_key: str | None = None,
) -> ValidationResult:
    """Validates a single repo's feature-map.json against expectations."""
    progress.update_repo(repo.name, validate_status=PhaseStatus.running)
    name = repo.name

    try:
        data = json.loads(feature_map_path.read_text())
        features = data.get("features", [])
    except Exception as e:
        progress.update_repo(
            name, validate_status=PhaseStatus.failed,
            error=f"Failed to read feature map: {e}",
        )
        return _empty_result(name, repo.expected_features)

    detected = [
        {
            "name": f["name"],
            "files": len(f.get("paths", [])),
            "health": f.get("health_score", 0),
            "bug_ratio": f.get("bug_fix_ratio", 0),
            "commits": f.get("total_commits", 0),
            "flows": len(f.get("flows", [])),
        }
        for f in features
    ]

    # Metric sanity checks (no LLM needed)
    metric_issues = _check_metrics(features)

    # Get file tree for context
    slug = name.replace("/", "--")
    repo_dir = REPOS_DIR / slug
    file_tree = _get_file_tree(repo_dir, limit=300)

    # LLM validation
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        result = _heuristic_validation(name, repo, detected, metric_issues)
        progress.update_repo(
            name, validate_status=PhaseStatus.completed,
            validation_result=result,
        )
        return result

    try:
        client = anthropic.Anthropic(api_key=key)
        expected_list = "\n".join(f"- {f}" for f in repo.expected_features)
        detected_list = "\n".join(
            f"- {d['name']} ({d['files']} files, health={d['health']}, "
            f"bug_ratio={d['bug_ratio']:.1%}, {d['commits']} commits, "
            f"{d['flows']} flows)"
            for d in detected
        )

        response = client.messages.parse(
            model=VALIDATION_MODEL,
            max_tokens=4096,
            system=_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": _USER_PROMPT.format(
                    repo_name=name,
                    reason=repo.reason,
                    expected_list=expected_list,
                    detected_list=detected_list,
                    file_tree=file_tree,
                ),
            }],
            output_format=_ValidationResponse,
        )
        v = response.parsed_output

        all_issues = metric_issues + v.metric_issues
        matched = v.matched_features
        missed = v.missed_features
        spurious = v.spurious_features

        precision = len(matched) / len(detected) if detected else 0.0
        recall = len(matched) / len(repo.expected_features) if repo.expected_features else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

        result = ValidationResult(
            repo_name=name,
            detected_features=[d["name"] for d in detected],
            expected_features=repo.expected_features,
            matched_features=matched,
            missed_features=missed,
            spurious_features=spurious,
            precision=round(precision, 3),
            recall=round(recall, 3),
            f1_score=round(f1, 3),
            metric_issues=all_issues,
            agent_reasoning=v.reasoning,
        )
        progress.update_repo(
            name, validate_status=PhaseStatus.completed,
            validation_result=result,
        )
        return result

    except Exception as e:
        result = _heuristic_validation(name, repo, detected, metric_issues)
        result.agent_reasoning = f"LLM validation failed ({e}), used heuristic fallback"
        progress.update_repo(
            name, validate_status=PhaseStatus.completed,
            validation_result=result,
        )
        return result


def _check_metrics(features: list[dict]) -> list[str]:
    """Sanity-check feature metrics without LLM."""
    issues = []
    for f in features:
        name = f.get("name", "?")
        health = f.get("health_score", 0)
        ratio = f.get("bug_fix_ratio", 0)
        commits = f.get("total_commits", 0)

        if health > 80 and ratio > 0.3:
            issues.append(
                f"{name}: health {health} but bug ratio {ratio:.1%} — inconsistent"
            )
        if health < 30 and ratio < 0.1:
            issues.append(
                f"{name}: health {health} but bug ratio only {ratio:.1%} — suspicious"
            )
        if commits == 0:
            issues.append(f"{name}: 0 commits — possible mapping bug")

    return issues


def _get_file_tree(repo_dir: Path, limit: int = 300) -> str:
    """Returns a newline-separated list of source files in the repo."""
    if not repo_dir.exists():
        return "(repo directory not found)"
    files = []
    skip = {"node_modules", ".git", "dist", "build", ".next", "coverage", "vendor", "__pycache__"}
    for p in sorted(repo_dir.rglob("*")):
        if any(s in p.parts for s in skip):
            continue
        if p.is_file() and p.suffix in (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs"):
            files.append(str(p.relative_to(repo_dir)))
            if len(files) >= limit:
                break
    return "\n".join(files) if files else "(no source files found)"


def _fuzzy_score(expected: str, detected: str) -> float:
    """Score 0-1 based on word overlap and stem matching."""
    def words(s: str) -> set[str]:
        return {w for w in s.lower().replace("-", " ").replace("_", " ").split() if len(w) > 1}

    def stems(ws: set[str]) -> set[str]:
        return {w.rstrip("s").rstrip("ing").rstrip("tion").rstrip("ment") for w in ws}

    exp_words = words(expected)
    det_words = words(detected)

    # Exact word overlap
    if exp_words & det_words:
        return 0.9

    # Stem overlap (accounts ↔ account-management)
    if stems(exp_words) & stems(det_words):
        return 0.7

    # Substring of stems (auth ↔ user-auth, sync ↔ account-syncing)
    exp_stems = stems(exp_words)
    det_stems = stems(det_words)
    for es in exp_stems:
        for ds in det_stems:
            if es in ds or ds in es:
                return 0.5

    return 0.0


def _heuristic_validation(
    repo_name: str,
    repo: RepoTarget,
    detected: list[dict],
    metric_issues: list[str],
) -> ValidationResult:
    """Fuzzy name matching without LLM."""
    matched = []
    missed = []
    used_detected: set[int] = set()

    for expected in repo.expected_features:
        best_idx, best_score = -1, 0.0
        for i, d in enumerate(detected):
            if i in used_detected:
                continue
            score = _fuzzy_score(expected, d["name"])
            if score > best_score:
                best_score = score
                best_idx = i
        if best_idx >= 0 and best_score >= 0.7:
            used_detected.add(best_idx)
            matched.append(FeatureMatch(
                expected=expected, detected=detected[best_idx]["name"],
                confidence=round(best_score, 2),
                notes="heuristic name match",
            ))
        else:
            missed.append(expected)

    matched_detected = {m.detected for m in matched}
    spurious = [d["name"] for d in detected if d["name"] not in matched_detected]

    precision = len(matched) / len(detected) if detected else 0.0
    recall = len(matched) / len(repo.expected_features) if repo.expected_features else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    return ValidationResult(
        repo_name=repo_name,
        detected_features=[d["name"] for d in detected],
        expected_features=repo.expected_features,
        matched_features=matched,
        missed_features=missed,
        spurious_features=spurious,
        precision=round(precision, 3),
        recall=round(recall, 3),
        f1_score=round(f1, 3),
        metric_issues=metric_issues,
        agent_reasoning="Heuristic validation (no API key)",
    )


def _empty_result(repo_name: str, expected: list[str]) -> ValidationResult:
    return ValidationResult(
        repo_name=repo_name,
        expected_features=expected,
        missed_features=expected,
    )
