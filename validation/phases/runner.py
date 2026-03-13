"""Phase 2: Clone repos and run faultline analyze on each."""

import os
import subprocess
from pathlib import Path

from ..config import ANALYSIS_TIMEOUT_SEC, CLONE_DEPTH, MAX_COMMITS, REPOS_DIR, RESULTS_DIR
from ..models import PhaseStatus, RepoTarget
from ..progress import ProgressTracker


def run_analysis(
    repo: RepoTarget,
    progress: ProgressTracker,
    skip_clone: bool = False,
    api_key: str | None = None,
    use_llm: bool = True,
) -> Path | None:
    """Clones a repo and runs faultline analyze. Returns feature-map.json path or None."""
    slug = repo.name.replace("/", "--")
    repo_dir = REPOS_DIR / slug
    result_dir = RESULTS_DIR / slug
    result_dir.mkdir(parents=True, exist_ok=True)
    output_path = result_dir / "feature-map.json"

    # Clone
    if not skip_clone or not repo_dir.exists():
        progress.update_repo(repo.name, clone_status=PhaseStatus.running)
        try:
            if repo_dir.exists():
                subprocess.run(
                    ["rm", "-rf", str(repo_dir)],
                    check=True, timeout=60,
                )
            clone_cmd = ["git", "clone", "--single-branch"]
            if CLONE_DEPTH > 0:
                clone_cmd.extend(["--depth", str(CLONE_DEPTH)])
            clone_cmd.extend([repo.url, str(repo_dir)])
            subprocess.run(
                clone_cmd,
                check=True, timeout=300,
                capture_output=True, text=True,
            )
            progress.update_repo(repo.name, clone_status=PhaseStatus.completed)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            progress.update_repo(
                repo.name,
                clone_status=PhaseStatus.failed,
                error=f"Clone failed: {e}",
            )
            return None
    else:
        progress.update_repo(repo.name, clone_status=PhaseStatus.skipped)

    # Analyze
    progress.update_repo(repo.name, analyze_status=PhaseStatus.running)
    cmd = [
        "faultline", "analyze", str(repo_dir),
        "--output", str(output_path),
        "--days", "365",
        "--max-commits", str(MAX_COMMITS),
    ]
    if use_llm:
        cmd.extend(["--llm", "--flows"])
    if repo.src_filter:
        cmd.extend(["--src", repo.src_filter])

    # Pass API key to subprocess via env
    env = os.environ.copy()
    key = api_key or env.get("ANTHROPIC_API_KEY", "")
    if key:
        env["ANTHROPIC_API_KEY"] = key

    try:
        subprocess.run(
            cmd,
            check=True,
            timeout=ANALYSIS_TIMEOUT_SEC,
            capture_output=True, text=True,
            env=env,
        )
        if output_path.exists():
            progress.update_repo(
                repo.name,
                analyze_status=PhaseStatus.completed,
                feature_map_path=str(output_path),
            )
            return output_path
        else:
            progress.update_repo(
                repo.name,
                analyze_status=PhaseStatus.failed,
                error="Analysis produced no output file",
            )
            return None
    except subprocess.CalledProcessError as e:
        progress.update_repo(
            repo.name,
            analyze_status=PhaseStatus.failed,
            error=f"Analysis failed: {e.stderr[-500:] if e.stderr else str(e)}",
        )
        return None
    except subprocess.TimeoutExpired:
        progress.update_repo(
            repo.name,
            analyze_status=PhaseStatus.failed,
            error=f"Analysis timed out ({ANALYSIS_TIMEOUT_SEC}s)",
        )
        return None
