import logging
import re
import typer
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich import print as rprint

from faultline.analyzer.git import load_repo, get_commits, get_tracked_files, DEFAULT_MAX_COMMITS
from faultline.output.reporter import print_report
from faultline.output.writer import write_feature_map

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="faultline",
    help=(
        "Map developer features from git history and code structure. "
        "Bare `faultline <repo>` runs the scan-v2 pipeline."
    ),
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


# Subcommand names that must NOT be swallowed by the default-command
# router. Anything else on a bare invocation is treated as scan-v2 input
# (a repo path and/or scan-v2 flags). Keep this in sync with the
# @app.command() definitions below.
_KNOWN_SUBCOMMANDS = frozenset({
    "update", "evolve", "refresh", "watch", "watch-status", "watch-stop",
    "pull", "suggest-config", "scan-v2", "classify-shape", "version",
})


def _route_default_to_scan_v2(argv: list[str]) -> list[str]:
    """Insert the implicit ``scan-v2`` subcommand for a bare invocation.

    Typer has no native "default command". The console script calls
    :func:`main`, which rewrites ``argv`` so a bare
    ``faultline <repo> [flags]`` is dispatched to the ``scan-v2`` command,
    re-using its exact option set. Explicit subcommands (including
    ``faultline scan-v2 …`` — the worker contract) and the top-level
    ``--help`` pass through untouched.
    """
    if not argv:
        return argv
    first = argv[0]
    if first in _KNOWN_SUBCOMMANDS:
        return argv
    if first in ("-h", "--help"):
        return argv
    # First token is a repo path (or a scan-v2 flag) → prepend scan-v2.
    return ["scan-v2", *argv]


def main() -> None:
    """Console-script entry point with default-command routing."""
    import sys

    sys.argv = [sys.argv[0], *_route_default_to_scan_v2(sys.argv[1:])]
    app()



@app.command()
def update(
    repo_path: str = typer.Argument(".", help="Path to the git repository"),
    scan: Optional[str] = typer.Option(None, "--scan", "-s", help="Path to existing feature-map JSON"),
    days: int = typer.Option(365, "--days", "-d", help="Days of history"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output path"),
    max_commits: int = typer.Option(DEFAULT_MAX_COMMITS, "--max-commits"),
):
    """Incremental update — refreshes an existing scan with new commits.

    Finds the latest feature-map for this repo and updates it with commits
    since the last analysis. No LLM calls — pure heuristic matching.

    Much faster and cheaper than a full re-scan.
    """
    from faultline.analyzer.incremental import incremental_update
    from faultline.models.types import FeatureMap
    import glob

    console.print(f"[bold]Incremental update:[/bold] {repo_path}")

    # 1. Load repo
    repo = load_repo(repo_path)
    if not repo:
        console.print("[red]✗[/red] Not a valid git repository")
        raise typer.Exit(1)

    # 2. Find existing scan
    if scan:
        scan_path = Path(scan)
    else:
        # Find latest scan for this repo in ~/.faultline/
        home = Path.home() / ".faultline"
        repo_name = Path(repo_path).resolve().name.lower()
        pattern = str(home / f"feature-map-{repo_name}-*.json")
        matches = sorted(glob.glob(pattern))
        if not matches:
            console.print(f"[red]✗[/red] No existing scan found for '{repo_name}' in {home}")
            console.print("[dim]Run 'faultline deep-scan' first to create initial scan[/dim]")
            raise typer.Exit(1)
        scan_path = Path(matches[-1])

    console.print(f"[dim]Base scan: {scan_path.name}[/dim]")

    # 3. Load existing feature map
    feature_map = FeatureMap.model_validate_json(scan_path.read_text())
    last_analyzed = feature_map.analyzed_at
    console.print(f"[dim]Last analyzed: {last_analyzed.strftime('%Y-%m-%d %H:%M')}[/dim]")
    console.print(f"[dim]Features: {len(feature_map.features)}, commits: {feature_map.total_commits}[/dim]")

    # 4. Get new commits since last analysis
    from datetime import timezone
    all_commits = get_commits(repo, days=days, max_commits=max_commits)
    new_commits = [c for c in all_commits if c.date > last_analyzed]

    if not new_commits:
        console.print("[green]✓[/green] Already up to date — no new commits")
        raise typer.Exit(0)

    bug_fixes = sum(1 for c in new_commits if c.is_bug_fix)
    console.print(f"[blue]{len(new_commits)} new commits[/blue] ({bug_fixes} bug fixes)")

    # 5. Run incremental update
    updated = incremental_update(feature_map, new_commits)

    # 6. Report changes
    changed = []
    for old_feat, new_feat in zip(
        sorted(feature_map.features, key=lambda f: f.name),
        sorted(updated.features, key=lambda f: f.name),
    ):
        if old_feat.name == new_feat.name:
            delta_commits = new_feat.total_commits - old_feat.total_commits
            delta_bugs = new_feat.bug_fixes - old_feat.bug_fixes
            delta_health = new_feat.health_score - old_feat.health_score
            if delta_commits > 0:
                changed.append((new_feat.name, delta_commits, delta_bugs, delta_health))

    if changed:
        console.print(f"\n[bold]Changed features ({len(changed)}):[/bold]")
        for name, dc, db, dh in sorted(changed, key=lambda x: x[3]):
            health_color = "red" if dh < -5 else "green" if dh > 5 else "dim"
            sign = "+" if dh >= 0 else ""
            console.print(f"  {name}: +{dc} commits, +{db} bugs, [{health_color}]{sign}{dh:.0f} health[/{health_color}]")

    # 7. Report & save
    print_report(updated)
    output_path = output or None
    saved = write_feature_map(updated, output_path)
    console.print(f"\n[green]Saved:[/green] {saved}")


@app.command()
def evolve(
    repo_path: str = typer.Argument(".", help="Path to the git repository"),
    scan: Optional[str] = typer.Option(None, "--scan", "-s", help="Path to existing feature-map JSON"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output path"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="Anthropic API key"),
):
    """Evolve — smart update that detects new features and flows.

    Compares current repo files with the last scan. New files matched to
    existing features by heuristics. New directories sent to Sonnet to
    determine: new feature, new flow, or addition to existing.

    Preserves existing feature map as source of truth.
    """
    from faultline.analyzer.evolve import detect_changes, evolve_with_llm, apply_simple_delta
    from faultline.models.types import FeatureMap
    import glob

    console.print(f"[bold]Evolving:[/bold] {repo_path}")

    # 1. Load repo
    repo = load_repo(repo_path)
    if not repo:
        console.print("[red]✗[/red] Not a valid git repository")
        raise typer.Exit(1)

    # 2. Find existing scan
    if scan:
        scan_path = Path(scan)
    else:
        home = Path.home() / ".faultline"
        repo_name = Path(repo_path).resolve().name.lower()
        # Find scans matching exact repo name (not prefix matches)
        pattern = str(home / f"feature-map-{repo_name}-*.json")
        matches = sorted(glob.glob(pattern))
        if not matches:
            console.print(f"[red]✗[/red] No existing scan found for '{repo_name}'")
            console.print("[dim]Run 'faultline deep-scan' first[/dim]")
            raise typer.Exit(1)
        scan_path = Path(matches[-1])

    console.print(f"[dim]Base scan: {scan_path.name}[/dim]")

    # 3. Load existing feature map
    feature_map = FeatureMap.model_validate_json(scan_path.read_text())
    console.print(f"[dim]Features: {len(feature_map.features)}, "
                  f"flows: {sum(len(f.flows) for f in feature_map.features)}[/dim]")

    # 4. Get current tracked files
    current_files = get_tracked_files(repo)
    console.print(f"[dim]Current files: {len(current_files)}[/dim]")

    # 5. Detect changes
    delta = detect_changes(feature_map, current_files)

    new_matched = sum(len(v) for v in delta.matched_files.values())
    console.print(f"[blue]Changes:[/blue] {len(delta.new_files) + new_matched + sum(1 for d in delta.new_directories)} new files, "
                  f"{len(delta.deleted_files)} deleted, "
                  f"{len(delta.new_directories)} new directories")

    if not delta.new_files and not delta.deleted_files and not delta.new_directories and not delta.matched_files:
        console.print("[green]✓[/green] No structural changes — feature map is up to date")
        raise typer.Exit(0)

    # 6. Apply changes
    if delta.needs_llm:
        console.print(f"[bold blue]New directories detected — calling Sonnet...[/bold blue]")
        updated = evolve_with_llm(feature_map, delta, current_files, api_key=api_key)
    else:
        console.print("[dim]No new directories — applying heuristic changes only[/dim]")
        updated = apply_simple_delta(feature_map, delta)

    # 7. Report
    old_feat_count = len(feature_map.features)
    new_feat_count = len(updated.features)
    old_flow_count = sum(len(f.flows) for f in feature_map.features)
    new_flow_count = sum(len(f.flows) for f in updated.features)

    if new_feat_count > old_feat_count:
        console.print(f"[green]✓ {new_feat_count - old_feat_count} new feature(s) added[/green]")
    if new_flow_count > old_flow_count:
        console.print(f"[green]✓ {new_flow_count - old_flow_count} new flow(s) added[/green]")
    if delta.matched_files:
        console.print(f"[dim]{new_matched} files added to existing features[/dim]")
    if delta.deleted_files:
        console.print(f"[dim]{len(delta.deleted_files)} deleted files cleaned up[/dim]")

    print_report(updated)

    output_path = output or None
    saved = write_feature_map(updated, output_path)
    console.print(f"\n[green]Saved:[/green] {saved}")


@app.command()
def refresh(
    repo_path: str = typer.Argument(".", help="Repo to refresh (must have a prior scan in ~/.faultline/)"),
    map_path: Optional[str] = typer.Option(
        None, "--map",
        help="Path to existing feature-map JSON. Defaults to the most recent ~/.faultline/feature-map-*.json",
    ),
    output: Optional[str] = typer.Option(
        None, "--output", "-o",
        help="Where to write the refreshed feature map. Defaults to ~/.faultline/ with a new timestamp.",
    ),
    check_only: bool = typer.Option(
        False, "--check",
        help="Report freshness without writing a refreshed map.",
    ),
    detect_new: bool = typer.Option(
        False, "--detect-new",
        help=(
            "After refresh, classify orphan files (not in any feature) via LLM. "
            "Proposes extensions of existing features or entirely new features. "
            "Requires ANTHROPIC_API_KEY."
        ),
    ),
    refresh_symbols: bool = typer.Option(
        False, "--refresh-symbols",
        help=(
            "Update symbol-level attributions for flows: clean up removed "
            "symbols and re-attribute newly added ones. Body-only changes "
            "are preserved. Requires ANTHROPIC_API_KEY only when new symbols "
            "appear."
        ),
    ),
    auto_apply: bool = typer.Option(
        False, "--auto-apply",
        help="With --detect-new, automatically apply high-confidence proposals to the map.",
    ),
    api_key: Optional[str] = typer.Option(
        None, "--api-key",
        help="Anthropic API key for --detect-new (defaults to ANTHROPIC_API_KEY env var)",
    ),
):
    """
    Incrementally update a feature map to match the current git HEAD.

    Runs the existing analyzer/incremental.py pipeline (no LLM calls)
    and updates content/symbol hashes. Orders of magnitude cheaper
    than a full --llm scan and preserves flow + symbol attributions
    on untouched features.

    Examples:
        faultlines refresh
        faultlines refresh /path/to/repo --check
        faultlines refresh . --output latest.json
    """
    import json as _json
    from faultline.cache.refresh import refresh_feature_map
    from faultline.cache.freshness import check_freshness
    from faultline.models.types import FeatureMap
    from faultline.output.writer import write_feature_map

    # Locate the map to refresh
    if map_path:
        map_file = Path(map_path).expanduser()
    else:
        home = Path.home() / ".faultline"
        candidates = sorted(home.glob("feature-map-*.json"))
        if not candidates:
            console.print(
                "[red]No feature map found.[/red] "
                "Run `faultlines analyze . --llm --flows` first."
            )
            raise typer.Exit(1)
        map_file = candidates[-1]

    if not map_file.exists():
        console.print(f"[red]Map not found:[/red] {map_file}")
        raise typer.Exit(1)

    console.print(f"[dim]Loading:[/dim] {map_file}")
    fm = FeatureMap.model_validate_json(map_file.read_text())

    if check_only:
        report = check_freshness(fm, repo_path)
        if not report.is_stale:
            console.print("[green]✓ Feature map is up to date with HEAD[/green]")
        else:
            console.print(
                f"[yellow]Stale:[/yellow] {report.commits_behind} commit(s) behind. "
                f"{report.changed_files_count} file(s) changed. "
                f"{'New files detected.' if report.has_new_files else ''}"
            )
        return

    result = refresh_feature_map(fm, repo_path)

    if not result.freshness_before.is_stale and not detect_new and not refresh_symbols:
        console.print("[green]✓ Already up to date — no refresh needed[/green]")
        return

    updated_map = result.updated_map

    # Symbol-level incremental (opt-in)
    if refresh_symbols:
        import os as _os
        _api_key = api_key or _os.environ.get("ANTHROPIC_API_KEY")
        from faultline.cache.symbols import refresh_symbol_attributions
        console.print("[blue]Refreshing symbol attributions...[/blue]")
        sym_report = refresh_symbol_attributions(
            feature_map=updated_map,
            repo_path=repo_path,
            api_key=_api_key,
        )
        console.print(f"[dim]{sym_report.summary()}[/dim]")
        if sym_report.symbols_added and not _api_key:
            console.print(
                "[yellow]New symbols detected but no ANTHROPIC_API_KEY — "
                "re-attribution skipped. Existing attributions preserved.[/yellow]"
            )

    # Orphan classification (opt-in, LLM-based)
    if detect_new and result.orphan_files:
        console.print(
            f"[blue]Classifying {len(result.orphan_files)} orphan file(s)...[/blue]"
        )
        from faultline.cache.discovery import discover_from_orphans, apply_report
        report = discover_from_orphans(
            orphan_files=result.orphan_files,
            feature_map=updated_map,
            api_key=api_key,
        )
        console.print(f"[dim]{report.summary()}[/dim]")

        # Pretty-print proposals
        if report.extensions:
            console.print("\n[bold]Extensions of existing features:[/bold]")
            for p in report.extensions:
                files_str = _trim_file_list(p.files)
                console.print(
                    f"  [green]→[/green] [bold]{p.extends_feature}[/bold] "
                    f"gains {len(p.files)} file(s) "
                    f"[dim]({p.confidence}, {p.reason})[/dim]"
                )
                console.print(f"    {files_str}")

        if report.new_features:
            console.print("\n[bold]Candidate new features:[/bold]")
            for p in report.new_features:
                files_str = _trim_file_list(p.files)
                console.print(
                    f"  [cyan]+[/cyan] [bold]{p.new_feature_name}[/bold] "
                    f"({len(p.files)} files, {p.confidence})"
                )
                if p.new_feature_description:
                    console.print(f"    [dim]{p.new_feature_description}[/dim]")
                console.print(f"    {files_str}")

        if auto_apply:
            applied = apply_report(updated_map, report, only_high_confidence=True)
            console.print(
                f"\n[green]✓ Auto-applied {applied} high-confidence proposal(s)[/green]"
            )
        elif report.extensions or report.new_features:
            console.print(
                "\n[dim]Review above. Re-run with --detect-new --auto-apply to apply "
                "high-confidence proposals, or run a full `faultlines analyze` for a "
                "fresh scan.[/dim]"
            )

    # Save updated map
    saved_path = write_feature_map(updated_map, output)

    console.print(f"\n[green]✓ Refresh complete[/green]")
    console.print(f"  Commits behind before: {result.freshness_before.commits_behind}")
    console.print(f"  Files modified: {result.files_truly_modified}")
    console.print(f"  Files added: {result.files_added}")
    console.print(f"  Files removed: {result.files_removed}")
    console.print(f"  LLM calls saved: ~{result.llm_calls_saved}")
    if result.orphan_files and not detect_new:
        console.print(
            f"  [yellow]⚠ {len(result.orphan_files)} orphan file(s) not mapped to any feature.[/yellow]"
        )
        console.print(
            f"    Run `faultlines refresh --detect-new` to classify them via LLM."
        )
    console.print(f"\n[dim]Saved:[/dim] {saved_path}")


def _trim_file_list(files: list[str], max_shown: int = 4) -> str:
    """Format a file list for terminal display."""
    if len(files) <= max_shown:
        return ", ".join(files)
    return ", ".join(files[:max_shown]) + f", +{len(files) - max_shown} more"


@app.command()
def watch(
    repo_path: str = typer.Argument(".", help="Repo to watch"),
    debounce: float = typer.Option(
        30.0, "--debounce",
        help="Seconds of silence after last file change before refreshing (default 30)",
    ),
    daemon: bool = typer.Option(
        False, "--daemon",
        help="Run in background (fork + detach). Use `faultlines watch-stop` to kill.",
    ),
    map_path: Optional[str] = typer.Option(
        None, "--map",
        help="Explicit feature-map JSON. Defaults to latest in ~/.faultline/.",
    ),
    verbose: bool = typer.Option(True, "--verbose/--quiet", help="Log refresh events"),
):
    """
    Watch a repo and auto-refresh the feature map on file changes.

    Foreground by default (Ctrl-C to stop). Use --daemon to detach.
    Only triggers metric refresh — no LLM calls, no cost.

    Examples:
        faultlines watch                         # foreground, current dir
        faultlines watch /path/to/repo --daemon  # background
        faultlines watch . --debounce 10         # react faster
    """
    from faultline.watch import run_watcher, start_daemon

    if daemon:
        try:
            pid = start_daemon(repo_path, debounce_seconds=debounce, map_path=map_path)
            console.print(f"[green]✓ Watcher started[/green] (pid {pid})")
            console.print(f"[dim]Stop with: faultlines watch-stop {repo_path}[/dim]")
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
    else:
        try:
            run_watcher(
                repo_path=repo_path,
                debounce_seconds=debounce,
                map_path=map_path,
                verbose=verbose,
            )
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)


@app.command(name="watch-status")
def watch_status(
    repo_path: str = typer.Argument(".", help="Repo to check"),
):
    """Check whether a watcher daemon is running for this repo."""
    from faultline.watch import watcher_status
    status = watcher_status(repo_path)
    if status.running:
        import datetime as _dt
        started = _dt.datetime.fromtimestamp(status.started_at or 0).strftime("%Y-%m-%d %H:%M")
        console.print(f"[green]✓ Running[/green] (pid {status.pid}, started {started})")
    else:
        console.print("[yellow]Not running[/yellow]")


@app.command(name="watch-stop")
def watch_stop(
    repo_path: str = typer.Argument(".", help="Repo whose watcher to stop"),
):
    """Stop a background watcher daemon."""
    from faultline.watch import stop_daemon
    if stop_daemon(repo_path):
        console.print("[green]✓ Stopped[/green]")
    else:
        console.print("[yellow]No watcher running[/yellow]")


@app.command(hidden=True)
def pull(
    repo: Optional[str] = typer.Argument(
        None,
        help="Repo slug (defaults to current directory's folder name). Example: 'soc0'",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Where to save the merged feature map. Default: ~/.faultline/feature-map-<slug>-cloud.json",
    ),
):
    """Pull the latest scan for a repo with user overrides applied.

    Overrides include custom feature names, aliases, and labels set in the
    dashboard. Requires FAULTLINE_API_KEY. MCP then matches queries
    against those overrides — if the team calls it 'labels', AI finds it
    under 'labels' even though the LLM originally named it 'tags'.
    """
    import json
    import os
    import re
    from faultline.cloud.sync import pull_feature_map

    if repo is None:
        repo = Path.cwd().name
    slug = re.sub(r"[^a-z0-9]+", "-", repo.lower())[:60]

    if not os.environ.get("FAULTLINES_EXPERIMENTAL"):
        rprint(
            "[yellow]`pull` is alpha and not yet available in public beta.[/yellow]\n"
            "[dim]Set FAULTLINES_EXPERIMENTAL=1 to opt in once the cloud dashboard launches.[/dim]"
        )
        raise typer.Exit(code=1)

    if not os.environ.get("FAULTLINE_API_KEY"):
        rprint("[red]FAULTLINE_API_KEY not set.[/red] Create a key at your dashboard → Settings → API keys.")
        raise typer.Exit(code=1)

    rprint(f"Pulling latest scan for [bold]{slug}[/bold]…")
    data = pull_feature_map(slug)
    if data is None:
        rprint(f"[yellow]No scan found for '{slug}'.[/yellow]")
        raise typer.Exit(code=1)

    target = output or (Path.home() / ".faultline" / f"feature-map-{slug}-cloud.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2))

    features = data.get("features", [])
    applied = data.get("_meta", {}).get("overrides_applied", 0)
    renamed = sum(1 for f in features if f.get("display_name") and f["display_name"] != f.get("original_name", f.get("name")))
    rprint(f"[green]✓[/green] Saved {len(features)} features to {target}")
    rprint(f"  {applied} override(s) available · {renamed} renamed")


@app.command(name="suggest-config")
def suggest_config(
    repo_path: str = typer.Argument(
        ".",
        help="Path to the git repository",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help=(
            "Write suggestions to .faultline.yaml instead of "
            "printing to stdout. Existing file is preserved — "
            "suggestions land under a fresh ``# Suggested by "
            "faultline suggest-config`` header."
        ),
        is_flag=True,
    ),
):
    """Suggest a starter ``.faultline.yaml`` from repo signals.

    Discovers canonical-feature names from:
      - Workspace package names (package.json, pyproject.toml,
        Cargo.toml).
      - CODEOWNERS team assignments (root, .github/, docs/).

    Prints the suggestions in YAML form so you can review and
    drop into ``.faultline.yaml`` (or pass ``--write`` to do it
    automatically).
    """
    from faultline.analyzer.auto_alias_discoverer import discover_aliases
    from faultline.analyzer.git import get_tracked_files, load_repo
    import yaml as _yaml

    repo_path_resolved = str(Path(repo_path).resolve())
    try:
        repo = load_repo(repo_path_resolved)
    except Exception as exc:
        console.print(f"[red]Error loading repo:[/red] {exc}")
        raise typer.Exit(1) from exc

    files = get_tracked_files(repo)
    rules = discover_aliases(repo_path_resolved, files)

    if not rules:
        console.print(
            "[yellow]No signals found.[/yellow] No workspace manifest "
            "and no CODEOWNERS file detected — there is nothing to "
            "suggest. You can hand-author a `.faultline.yaml` "
            "instead."
        )
        return

    yaml_block: dict = {"features": {}}
    for r in rules:
        entry: dict[str, object] = {}
        if r.description:
            entry["description"] = r.description
        if r.variants:
            entry["variants"] = list(r.variants)
        yaml_block["features"][r.canonical] = entry

    rendered = _yaml.safe_dump(
        yaml_block, sort_keys=False, allow_unicode=True, indent=2,
    )
    header = (
        "# Suggested by `faultline suggest-config` — review and edit "
        "before scanning.\n"
        "# Each feature below was derived from a workspace package "
        "name or a CODEOWNERS team.\n"
        "# Empty `variants` is fine; the engine fills in matches "
        "automatically when you run `faultline analyze`.\n\n"
    )

    if write:
        target = Path(repo_path_resolved) / ".faultline.yaml"
        if target.exists():
            console.print(
                f"[yellow]{target.name} already exists.[/yellow] "
                "Suggestions printed to stdout instead — merge "
                "manually."
            )
            console.print()
            console.print(header + rendered, end="")
        else:
            target.write_text(header + rendered, encoding="utf-8")
            console.print(
                f"[green]✓[/green] Wrote {len(rules)} suggested "
                f"canonical(s) to {target}"
            )
    else:
        console.print(header + rendered, end="")


@app.command(name="scan-v2")
def scan_v2(
    repo_path: str = typer.Argument(
        ".",
        help="Path to the git repository (default: cwd).",
    ),
    model: str = typer.Option(
        "haiku",
        "--model",
        help=(
            "LLM model to use for Stage 3 + Stage 4. Accepts the "
            "aliases 'haiku' / 'sonnet' or a fully-qualified Anthropic "
            "model id (e.g. claude-haiku-4-5-20251001)."
        ),
    ),
    llm_reconcile: bool = typer.Option(
        False,
        "--llm-reconcile/--no-llm-reconcile",
        help=(
            "Ask Haiku to break ties between near-duplicate feature "
            "names in Stage 2. Off by default — fully deterministic."
        ),
    ),
    days: int = typer.Option(
        365,
        "--days",
        "-d",
        help="Number of days of git history to load.",
    ),
    output: Optional[str] = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Output path for the FeatureMap JSON. Default: "
            "~/.faultline/feature-map-<slug>-<timestamp>.json"
        ),
    ),
    run_id: Optional[str] = typer.Option(
        None,
        "--run-id",
        help=(
            "Override the auto-generated run id (default: "
            "<utc-ts>-<sha8>). Useful for labelling A/B experiment "
            "runs — e.g. --run-id baseline then --run-id with-clustering. "
            "Run artifacts land under ~/.faultline/logs/<slug>/<run-id>/."
        ),
    ),
    max_tree_depth: int = typer.Option(
        8,
        "--max-tree-depth",
        help=(
            "Maximum BFS depth for Stage 6.3 import-tree enrichment. "
            "Default 8 covers page → component → hook → service → util "
            "→ primitives chain. Raise to 10-12 for enterprise monoliths "
            "with deeper layering; lower to 4-5 for quick scans."
        ),
    ),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help=(
            "Incremental scan: only re-extract features whose files "
            "changed since this git commit SHA. Requires --base-scan-path. "
            "When omitted (default), a full cold scan is run."
        ),
    ),
    base_scan_path: Optional[str] = typer.Option(
        None,
        "--base-scan-path",
        help=(
            "Path to a previous feature-map JSON. Required when --since "
            "is set. Also enables stable UUID lineage matching even on "
            "full scans — features that overlap with the base ≥ "
            "--lineage-jaccard-threshold keep their base UUID."
        ),
    ),
    lineage_jaccard_threshold: float = typer.Option(
        0.70,
        "--lineage-jaccard-threshold",
        help=(
            "Jaccard cutoff (0..1) for considering a new feature the "
            "same as a base feature in lineage matching. Default 0.70. "
            "Tune per stack — Rails monoliths may need 0.60; tiny "
            "libraries 0.80."
        ),
    ),
):
    """Run the Layer 1 pipeline v2 (deterministic extractors + Haiku flows).

    Pipeline v2 is the new code-grounded scanner introduced 2026-05-18.
    Stages 0..7 run in order; Stage 3 + Stage 4 are the only LLM calls.
    Output: a single FeatureMap JSON with developer_features populated
    and product_features empty (Layer 2 is deferred).
    """
    from faultline.pipeline_v2.run import run_pipeline_v2, resolve_model

    repo = Path(repo_path).resolve()
    if not repo.is_dir():
        rprint(f"[red]Error:[/red] {repo} is not a directory")
        raise typer.Exit(code=2)

    resolved_model = resolve_model(model)
    out_path = Path(output).resolve() if output else None

    rprint(
        f"[bold blue]faultline scan-v2[/bold blue] {repo} "
        f"(model={resolved_model}, llm_reconcile={llm_reconcile}, "
        f"days={days}, max_tree_depth={max_tree_depth})"
    )

    if since and not base_scan_path:
        rprint(
            "[red]Error:[/red] --since requires --base-scan-path "
            "(engine cannot match lineage without a previous scan)."
        )
        raise typer.Exit(code=2)

    try:
        result = run_pipeline_v2(
            repo,
            model=resolved_model,
            days=days,
            out_path=out_path,
            llm_reconcile=llm_reconcile,
            run_id=run_id,
            max_tree_depth=max_tree_depth,
            since=since,
            base_scan_path=Path(base_scan_path).resolve() if base_scan_path else None,
            lineage_jaccard_threshold=lineage_jaccard_threshold,
        )
    except Exception as exc:  # noqa: BLE001 — surface clean error to CLI user
        rprint(f"[red]Scan failed:[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=1) from exc

    rprint(
        f"[green]✓[/green] Wrote {result['path']}  "
        f"(run_id={result.get('run_id')}, "
        f"stack={result['stack']}, "
        f"cost=${result['cost_usd']:.4f}, "
        f"calls={result['calls']}, "
        f"elapsed={result['elapsed_sec']}s)"
    )
    if result.get("warnings"):
        for w in result["warnings"]:
            rprint(f"  [yellow]⚠[/yellow] {w}")


@app.command(name="classify-shape")
def classify_shape_cmd(
    repo_path: Path = typer.Argument(..., help="Path to the git repository to classify."),
    skip_auditor: bool = typer.Option(
        False,
        "--skip-auditor",
        help="Skip Stage 0.5 (no LLM); use Stage 0 heuristic stack only.",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        help="Write full report to this file in addition to stdout.",
    ),
) -> None:
    """Classify a repo's architectural SHAPE without running the full scan.

    Runs Stage 0 (intake) + Stage 0.5 (stack auditor, unless --skip-auditor)
    + Stage 0.6 (shape classifier). Cheap: $0.00-0.02, <2s. Pretty-prints
    the verdict + collected signals as JSON to stdout. Exit code is 0 on
    successful classification (including ``universal-residual``); non-zero
    only on I/O failure (bad path, not a git repo).
    """
    import json as _json
    import os as _os
    from dataclasses import asdict as _asdict

    from faultline.pipeline_v2.stack_auditor import (
        MIN_CONFIDENCE_TO_APPLY,
        run_stack_auditor,
    )
    from faultline.pipeline_v2.stage_0_6_shape import (
        ShapeSignals,
        classify_repo_shape,
    )
    from faultline.pipeline_v2.stage_0_intake import stage_0_intake

    repo_path = Path(repo_path).resolve()
    if not repo_path.is_dir():
        rprint(f"[red]error[/red]: repo_path is not a directory: {repo_path}")
        raise typer.Exit(code=2)

    # Avoid touching ~/.faultline artifacts during CLI classification —
    # the run_dir is set by stage_0_intake but we override it on the ctx
    # before classification to suppress artifact writes (CLI mode).
    ctx = stage_0_intake(repo_path)

    if not skip_auditor and _os.environ.get("ANTHROPIC_API_KEY"):
        try:
            verdict = run_stack_auditor(ctx, model="claude-haiku-4-5-20251001")
            if verdict.confidence >= MIN_CONFIDENCE_TO_APPLY:
                ctx = ctx.with_audited_stack(
                    audited_stack=verdict.primary_stack,
                    secondary_stacks=verdict.secondary_stacks,
                    extractor_hints=verdict.extractor_hints,
                    auditor_confidence=verdict.confidence,
                )
        except Exception as exc:  # noqa: BLE001 - degrade silently
            logger.warning("classify-shape: auditor failed (%s); continuing", exc)

    # Force CLI mode → suppress artifact writes by clearing run_dir.
    ctx.run_dir = None

    result = classify_repo_shape(ctx)
    signals = ShapeSignals.collect(ctx)

    report = {
        "repo_path": str(repo_path),
        "stage_0": {
            "stack": ctx.stack,
            "monorepo": ctx.monorepo,
            "workspace_count": len(ctx.workspaces or []),
        },
        "stage_0_5": {
            "audited_stack": ctx.audited_stack,
            "secondary_stacks": list(ctx.secondary_stacks or ()),
            "auditor_confidence": ctx.auditor_confidence,
        },
        "stage_0_6": {
            "shape": result.shape,
            "confidence": result.confidence,
            "rationale": result.rationale,
            "matched_signals": list(result.matched_signals),
        },
        "signals": _asdict(signals),
    }
    pretty = _json.dumps(report, indent=2, sort_keys=True, default=str)
    typer.echo(pretty)
    if output is not None:
        output.write_text(pretty)


@app.command()
def version():
    """Shows the faultline version."""
    from faultline import __version__
    rprint(f"faultline [bold blue]v{__version__}[/bold blue]")


if __name__ == "__main__":
    app()
