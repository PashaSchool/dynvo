"""Orchestrator: runs the full validation pipeline.

Usage:
    python -m validation.run
    python -m validation.run --repos 3 --skip-research
    python -m validation.run --skip-clone
    python -m validation.run main --skip-analyze    # reuse existing feature-map.json
    python -m validation.run baseline save           # save current results as baseline
    python -m validation.run baseline compare        # compare current vs baseline
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

from .config import RESULTS_DIR
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
    skip_analyze: bool = typer.Option(False, "--skip-analyze", help="Reuse existing feature-map.json"),
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
            # Skip analysis if feature-map.json already exists
            if skip_analyze:
                slug = target.name.replace("/", "--")
                existing = RESULTS_DIR / slug / "feature-map.json"
                if existing.exists():
                    tracker.update_repo(
                        target.name,
                        clone_status=PhaseStatus.skipped,
                        analyze_status=PhaseStatus.skipped,
                        feature_map_path=str(existing),
                    )
                    analysis_results[target.name] = existing
                    continue

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


@app.command()
def report(
    output: str | None = typer.Option(None, "--output", "-o", help="Output HTML path"),
    no_open: bool = typer.Option(False, "--no-open", help="Don't open in browser"),
) -> None:
    """Open the validation report in a browser."""
    from .ui.html_report import write_report

    path = write_report(
        output_path=Path(output) if output else None,
        open_browser=not no_open,
    )
    typer.echo(f"Report: {path}")


_BASELINE_DIR = RESULTS_DIR.parent / "baseline"


@app.command()
def baseline(
    action: str = typer.Argument("compare", help="save | compare"),
) -> None:
    """Save or compare baseline results.

    save    — copy current feature-map.json + progress.json as baseline
    compare — show delta between current and baseline
    """
    import json
    import shutil
    from rich.console import Console
    from rich.table import Table

    from .config import PROGRESS_FILE

    console = Console()

    if action == "save":
        _BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        # Copy all feature-map.json files
        if RESULTS_DIR.exists():
            for slug_dir in sorted(RESULTS_DIR.iterdir()):
                fm = slug_dir / "feature-map.json"
                if fm.exists():
                    dest = _BASELINE_DIR / slug_dir.name
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(fm, dest / "feature-map.json")
        # Copy progress.json
        if PROGRESS_FILE.exists():
            shutil.copy2(PROGRESS_FILE, _BASELINE_DIR / "progress.json")
        console.print(f"[green]Baseline saved to {_BASELINE_DIR}[/green]")
        return

    if action == "compare":
        baseline_progress = _BASELINE_DIR / "progress.json"
        if not baseline_progress.exists():
            console.print("[red]No baseline found. Run 'baseline save' first.[/red]")
            return
        if not PROGRESS_FILE.exists():
            console.print("[red]No current results. Run validation first.[/red]")
            return

        base = json.loads(baseline_progress.read_text())
        curr = json.loads(PROGRESS_FILE.read_text())

        base_repos = {r["repo"]["name"]: r for r in base.get("repos", [])}
        curr_repos = {r["repo"]["name"]: r for r in curr.get("repos", [])}

        table = Table(title="Baseline vs Current")
        table.add_column("Repo", style="bold")
        table.add_column("Base F1", justify="right")
        table.add_column("Curr F1", justify="right")
        table.add_column("Delta", justify="right")
        table.add_column("Base det", justify="right")
        table.add_column("Curr det", justify="right")

        all_names = sorted(set(base_repos) | set(curr_repos))
        total_base = total_curr = count = 0

        for name in all_names:
            br = base_repos.get(name, {}).get("validation_result")
            cr = curr_repos.get(name, {}).get("validation_result")
            short = name.split("/")[-1][:20]

            bf1 = int(br["f1_score"] * 100) if br else 0
            cf1 = int(cr["f1_score"] * 100) if cr else 0
            bdet = len(br["detected_features"]) if br else 0
            cdet = len(cr["detected_features"]) if cr else 0
            delta = cf1 - bf1
            sign = "+" if delta > 0 else ""
            color = "green" if delta > 0 else "red" if delta < 0 else "dim"

            table.add_row(
                short,
                f"{bf1}%" if br else "–",
                f"{cf1}%" if cr else "–",
                f"[{color}]{sign}{delta}[/{color}]",
                str(bdet) if br else "–",
                str(cdet) if cr else "–",
            )
            if br and cr:
                total_base += bf1
                total_curr += cf1
                count += 1

        if count:
            avg_delta = (total_curr - total_base) // count
            sign = "+" if avg_delta > 0 else ""
            color = "green" if avg_delta > 0 else "red" if avg_delta < 0 else "dim"
            table.add_row(
                "[bold]Average[/bold]",
                f"{total_base // count}%",
                f"{total_curr // count}%",
                f"[bold {color}]{sign}{avg_delta}[/bold {color}]",
                "", "",
            )

        console.print(table)
        return

    console.print(f"[red]Unknown action '{action}'. Use 'save' or 'compare'.[/red]")


if __name__ == "__main__":
    app()
