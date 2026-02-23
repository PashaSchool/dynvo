import typer
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich import print as rprint

from faultline.analyzer.git import load_repo, get_commits, get_tracked_files, estimate_commits, estimate_duration, DEFAULT_MAX_COMMITS
from faultline.analyzer.features import detect_features_from_structure, build_feature_map
from faultline.output.reporter import print_report
from faultline.output.writer import write_feature_map
from faultline.llm.detector import _DEFAULT_OLLAMA_HOST, _DEFAULT_OLLAMA_MODEL

app = typer.Typer(
    name="faultline",
    help="Analyze git history to map features and track technical debt",
    add_completion=False,
)
console = Console()


@app.command()
def analyze(
    repo_path: str = typer.Argument(
        ".",
        help="Path to the git repository",
    ),
    days: int = typer.Option(
        365,
        "--days", "-d",
        help="Number of days of history to analyze",
    ),
    output: Optional[str] = typer.Option(
        None,
        "--output", "-o",
        help="Path to save feature-map.json",
    ),
    save: bool = typer.Option(
        True,
        "--save/--no-save",
        help="Save feature-map.json to disk",
    ),
    top: int = typer.Option(
        3,
        "--top",
        help="Number of top risk features to highlight",
    ),
    llm: bool = typer.Option(
        False,
        "--llm",
        help="Use an LLM to semantically detect features from the file tree",
        is_flag=True,
    ),
    provider: str = typer.Option(
        "anthropic",
        "--provider",
        help="LLM provider: anthropic or ollama",
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="Model name override (default: claude-haiku-4-5 for anthropic, qwen2.5-coder:7b for ollama)",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        help="Anthropic API key (or set ANTHROPIC_API_KEY env var)",
    ),
    ollama_url: str = typer.Option(
        _DEFAULT_OLLAMA_HOST,
        "--ollama-url",
        help="Ollama server URL",
    ),
    src: Optional[str] = typer.Option(
        None,
        "--src",
        help="Subdirectory to focus analysis on, e.g. src/ or app/. Ignores everything outside.",
    ),
    max_commits: int = typer.Option(
        DEFAULT_MAX_COMMITS,
        "--max-commits",
        help="Maximum number of commits to analyze",
    ),
):
    """
    Analyzes a git repository and builds a feature map.

    Examples:
        faultline analyze
        faultline analyze ./my-project --days 90
        faultline analyze . --src src/
        faultline analyze . --llm --provider anthropic --api-key sk-ant-...
        faultline analyze . --llm --provider ollama --src src/
        faultline analyze . --llm --provider ollama --model llama3.2
    """
    repo_path = str(Path(repo_path).resolve())

    if llm and provider not in ("anthropic", "ollama"):
        console.print(f"[red]Unknown provider '{provider}'. Use: anthropic or ollama[/red]")
        raise typer.Exit(1)

    try:
        # 1. Load the repository
        console.print(f"[blue]Analyzing:[/blue] {repo_path}")
        repo = load_repo(repo_path)

        # 2. Validate LLM access early — before the long git analysis
        if llm:
            _validate_llm_access(provider, api_key, model, ollama_url)

        # 3. Pre-run estimate
        approx_count = estimate_commits(repo, days=days, max_commits=max_commits)
        if approx_count > 0:
            duration = estimate_duration(approx_count, use_llm=llm)
            console.print(f"[dim]~ {approx_count:,} commits in range → {duration}[/dim]")

        # 4. Fetch commits
        commits = get_commits(repo, days=days, max_commits=max_commits)
        if not commits:
            console.print("[yellow]No commits found for the specified period[/yellow]")
            raise typer.Exit(1)

        console.print(f"[green]✓[/green] Found {len(commits)} commits over {days} days")

        # 5. Detect files and map to features
        files = get_tracked_files(repo, src=src)
        if src:
            console.print(f"[green]✓[/green] Found {len(files)} files under [dim]{src}[/dim]")
        else:
            console.print(f"[green]✓[/green] Found {len(files)} files")

        # Strip --src prefix so LLM/heuristic sees clean relative paths (e.g. EDR/... not src/views/EDR/...)
        analysis_files, path_prefix = _strip_src_prefix(files, src)

        if llm:
            raw_mapping = _detect_with_llm(analysis_files, provider, api_key, model, ollama_url)
        else:
            raw_mapping = detect_features_from_structure(analysis_files)

        # Restore full paths so commit matching works against git history
        if path_prefix:
            feature_paths = {
                name: [path_prefix + f for f in paths]
                for name, paths in raw_mapping.items()
            }
        else:
            feature_paths = raw_mapping

        console.print(f"[green]✓[/green] Detected {len(feature_paths)} features")

        # 6. Build the feature map
        feature_map = build_feature_map(
            repo_path=repo_path,
            commits=commits,
            feature_paths=feature_paths,
            days=days,
        )

        # 7. Print the report
        print_report(feature_map)

        # 8. Save to disk
        if save:
            saved_path = write_feature_map(feature_map, output)
            console.print(f"[dim]Saved: {saved_path}[/dim]")

    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled[/yellow]")
        raise typer.Exit(0)


def _strip_src_prefix(
    files: list[str],
    src: str | None,
) -> tuple[list[str], str]:
    """
    Strips the --src prefix from file paths so LLM/heuristic sees clean relative paths.
    Returns (normalized_files, prefix_to_restore).

    Example:
        src/views/EDR/Page.tsx  →  EDR/Page.tsx  (prefix = "src/views/")
    """
    if not src:
        return files, ""
    prefix = src.rstrip("/") + "/"
    stripped = [f[len(prefix):] for f in files if f.startswith(prefix)]
    return stripped, prefix


def _validate_llm_access(
    provider: str,
    api_key: str | None,
    model: str | None,
    ollama_url: str,
) -> None:
    """Validates LLM connectivity before the long git analysis. Exits on failure."""
    if provider == "anthropic":
        from faultline.llm.detector import validate_api_key
        console.print("[dim]Validating Anthropic API key...[/dim]")
        is_valid, error_msg = validate_api_key(api_key=api_key)
        if not is_valid:
            console.print(f"[red]✗ {error_msg}[/red]")
            raise typer.Exit(1)
        console.print("[green]✓[/green] API key valid")

    elif provider == "ollama":
        from faultline.llm.detector import validate_ollama, _DEFAULT_OLLAMA_MODEL
        resolved_model = model or _DEFAULT_OLLAMA_MODEL
        console.print(f"[dim]Checking Ollama ({resolved_model})...[/dim]")
        is_valid, error_msg = validate_ollama(model=resolved_model, host=ollama_url)
        if not is_valid:
            console.print(f"[red]✗ {error_msg}[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓[/green] Ollama ready ({resolved_model})")


def _detect_with_llm(
    files: list[str],
    provider: str,
    api_key: str | None,
    model: str | None,
    ollama_url: str,
) -> dict[str, list[str]]:
    """Runs LLM feature detection with the chosen provider. Falls back to heuristic on failure."""
    if provider == "anthropic":
        from faultline.llm.detector import detect_features_llm
        console.print("[blue]Mapping features with Claude...[/blue]")
        feature_paths = detect_features_llm(files, api_key=api_key)

    elif provider == "ollama":
        from faultline.llm.detector import detect_features_ollama, _DEFAULT_OLLAMA_MODEL
        resolved_model = model or _DEFAULT_OLLAMA_MODEL
        console.print(f"[blue]Mapping features with Ollama ({resolved_model})...[/blue]")
        feature_paths = detect_features_ollama(files, model=resolved_model, host=ollama_url)

    else:
        feature_paths = {}

    if feature_paths:
        label = "Claude" if provider == "anthropic" else "Ollama"
        console.print(f"[green]✓[/green] {label} mapped {len(feature_paths)} features")
    else:
        console.print("[yellow]⚠ LLM detection failed — using heuristic fallback[/yellow]")
        feature_paths = detect_features_from_structure(files)

    return feature_paths


@app.command()
def version():
    """Shows the faultline version."""
    from faultline import __version__
    rprint(f"faultline [bold blue]v{__version__}[/bold blue]")


if __name__ == "__main__":
    app()
