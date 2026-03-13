"""Orchestrator: runs the full validation pipeline.

Usage:
    python -m validation.run
    python -m validation.run --repos 3 --skip-research
    python -m validation.run --skip-clone
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import typer

# Load .env file from project root if it exists (for ANTHROPIC_API_KEY etc.)
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from .models import OverallSummary, PhaseStatus
from .phases.research import select_repos
from .phases.runner import run_analysis
from .phases.validator import validate_repo
from .progress import ProgressTracker
from .ui.terminal import LiveUI

app = typer.Typer()


def _compute_summary(tracker: ProgressTracker) -> OverallSummary:
    repos = tracker.progress.repos
    successful = [
        rp for rp in repos
        if rp.validation_result and rp.validation_result.precision > 0
    ]
    if not successful:
        return OverallSummary(total_repos=len(repos))

    return OverallSummary(
        total_repos=len(repos),
        successful_repos=len(successful),
        avg_precision=sum(r.validation_result.precision for r in successful) / len(successful),
        avg_recall=sum(r.validation_result.recall for r in successful) / len(successful),
        avg_f1=sum(r.validation_result.f1_score for r in successful) / len(successful),
        total_features_detected=sum(
            len(r.validation_result.detected_features) for r in successful
        ),
        total_features_expected=sum(
            len(r.validation_result.expected_features) for r in successful
        ),
    )


@app.command()
def main(
    repos: int = typer.Option(5, "--repos", "-r", help="Number of repos to validate"),
    skip_research: bool = typer.Option(False, "--skip-research", help="Use fallback repo list"),
    skip_clone: bool = typer.Option(False, "--skip-clone", help="Reuse existing clones"),
    api_key: str | None = typer.Option(None, "--api-key", help="Anthropic API key"),
) -> None:
    """Run the faultline validation pipeline."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    tracker = ProgressTracker()
    ui = LiveUI()
    tracker.on_update(ui.update)

    with ui:
        # Phase 1: Research
        tracker.set_phase("research")
        repo_targets = select_repos(count=repos, api_key=key)
        tracker.set_repos(repo_targets)

        # Phase 2: Run analysis (sequential — avoids API rate limits)
        tracker.set_phase("running")
        analysis_results: dict[str, Path | None] = {}

        use_llm = bool(key)
        for target in repo_targets:
            result_path = run_analysis(
                target, tracker, skip_clone=skip_clone,
                api_key=key, use_llm=use_llm,
            )
            analysis_results[target.name] = result_path

        # Phase 3: Validate (parallel — independent per repo)
        tracker.set_phase("validating")
        successful_analyses = {
            name: path for name, path in analysis_results.items()
            if path is not None
        }

        # Skip validation for failed analyses
        for target in repo_targets:
            if target.name not in successful_analyses:
                tracker.update_repo(
                    target.name, validate_status=PhaseStatus.skipped,
                )

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {}
            for target in repo_targets:
                if target.name not in successful_analyses:
                    continue
                future = executor.submit(
                    validate_repo,
                    target,
                    successful_analyses[target.name],
                    tracker,
                    api_key=key,
                )
                futures[future] = target.name

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    name = futures[future]
                    tracker.update_repo(
                        name,
                        validate_status=PhaseStatus.failed,
                        error=str(e),
                    )

        # Summary
        summary = _compute_summary(tracker)
        tracker.set_summary(summary)

    # Final print outside Live context
    _print_final(tracker)


def _print_final(tracker: ProgressTracker) -> None:
    """Prints final summary after Live UI stops."""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    s = tracker.progress.summary

    if not s:
        console.print("[red]No summary available.[/red]")
        return

    console.print()
    console.print(Panel(
        f"[bold]Validation Complete[/]\n\n"
        f"  Repos:     {s.successful_repos}/{s.total_repos} successful\n"
        f"  Precision: {s.avg_precision:.0%}\n"
        f"  Recall:    {s.avg_recall:.0%}\n"
        f"  F1 Score:  {s.avg_f1:.0%}\n"
        f"  Features:  {s.total_features_detected} detected / {s.total_features_expected} expected\n\n"
        f"  Results:   ~/.faultline/validation/progress.json",
        title="faultline validation",
        border_style="green" if s.avg_f1 >= 0.7 else "yellow" if s.avg_f1 >= 0.4 else "red",
        padding=(1, 3),
    ))

    # Print metric issues
    for rp in tracker.progress.repos:
        if rp.validation_result and rp.validation_result.metric_issues:
            console.print(f"\n[yellow]Metric issues in {rp.repo.name}:[/yellow]")
            for issue in rp.validation_result.metric_issues:
                console.print(f"  - {issue}")


if __name__ == "__main__":
    app()
