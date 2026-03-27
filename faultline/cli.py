import typer
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich import print as rprint

from faultline.analyzer.git import load_repo, get_commits, get_tracked_files, estimate_commits, estimate_duration, get_remote_url, DEFAULT_MAX_COMMITS
from faultline.analyzer.features import detect_features_from_structure, build_feature_map, build_flows_metrics, split_large_features
from faultline.analyzer.repo_classifier import classify_repo, build_layer_context
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
        help="Use an LLM to assign semantic names to detected features (results are cached)",
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
        help=(
            "Model name override. "
            "Anthropic default: claude-haiku-4-5. "
            "Ollama default: llama3.1:8b (recommended). "
            "Other Ollama options: mistral-nemo:12b (best quality), qwen2.5:7b."
        ),
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
    flows: bool = typer.Option(
        False,
        "--flows",
        help="Detect user-facing flows within features (requires --llm)",
        is_flag=True,
    ),
    coverage: Optional[str] = typer.Option(
        None,
        "--coverage",
        help="Path to coverage report (lcov.info or coverage-summary.json). Auto-detected if omitted.",
    ),
    # ── Analytics integration ──
    posthog_key: Optional[str] = typer.Option(
        None,
        "--posthog-key",
        help="PostHog API key (or set POSTHOG_API_KEY env var)",
    ),
    posthog_project: Optional[str] = typer.Option(
        None,
        "--posthog-project",
        help="PostHog project ID",
    ),
    posthog_host: str = typer.Option(
        "https://app.posthog.com",
        "--posthog-host",
        help="PostHog host URL (for self-hosted or local mock)",
    ),
    sentry_token: Optional[str] = typer.Option(
        None,
        "--sentry-token",
        help="Sentry auth token (or set SENTRY_AUTH_TOKEN env var)",
    ),
    sentry_org: Optional[str] = typer.Option(
        None,
        "--sentry-org",
        help="Sentry organization slug",
    ),
    sentry_project: Optional[str] = typer.Option(
        None,
        "--sentry-project",
        help="Sentry project slug",
    ),
    sentry_host: str = typer.Option(
        "https://sentry.io",
        "--sentry-host",
        help="Sentry host URL (for self-hosted or local mock)",
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
        faultline analyze . --llm --flows
        faultline analyze . --llm --provider ollama --flows
        faultline analyze . --llm --flows --posthog-key phx_... --posthog-project 12345
        faultline analyze . --llm --flows --sentry-token sntrys_... --sentry-org my-org --sentry-project my-proj
    """
    repo_path = str(Path(repo_path).resolve())

    # --flows requires --llm
    if flows and not llm:
        llm = True

    if llm and provider not in ("anthropic", "ollama"):
        console.print(f"[red]Unknown provider '{provider}'. Use: anthropic or ollama[/red]")
        raise typer.Exit(1)

    if llm and provider == "ollama":
        try:
            import ollama as _ollama  # noqa: F401
        except ImportError:
            console.print(
                "[red]Ollama package not installed.[/red]\n"
                "Install with: [bold]pip install 'faultline[ollama]'[/bold]\n"
                "Or: [bold]pip install ollama[/bold]"
            )
            raise typer.Exit(1)

    try:
        # 1. Load the repository
        console.print(f"[blue]Analyzing:[/blue] {repo_path}")
        repo = load_repo(repo_path)
        remote_url = get_remote_url(repo)

        # 2. Validate LLM access early — before the long git analysis
        if llm:
            _validate_llm_access(provider, api_key, model, ollama_url)

        # 3. Pre-run estimate
        approx_count = estimate_commits(repo, days=days, max_commits=max_commits)
        if approx_count > 0:
            duration = estimate_duration(approx_count, use_llm=llm, use_flows=flows)
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

        # Classify repo structure to adapt LLM strategy
        repo_structure = classify_repo(analysis_files)
        layer_context = build_layer_context(repo_structure)
        if repo_structure.layout != "feature":
            console.print(f"[dim]Repo layout: {repo_structure.layout} (layer ratio: {repo_structure.layer_ratio:.0%})[/dim]")

        # Always extract AST signatures — needed for import graph clustering
        # and reused for flow detection when --flows is set.
        from faultline.analyzer.ast_extractor import extract_signatures
        extract_root = str(Path(str(repo.working_tree_dir)) / path_prefix) if path_prefix else str(repo.working_tree_dir)
        signatures = extract_signatures(analysis_files, extract_root)
        if signatures:
            console.print(f"[dim]Extracted signatures from {len(signatures)} files[/dim]")

        # Step 1 — Import graph clustering (primary, always deterministic)
        # Files connected through import chains form the same cluster.
        # Need meaningful number of TS/JS files — Python sigs are useful for flow detection
        # but not for import graph clustering which relies on JS/TS import statements.
        _TS_JS_EXTS = {".ts", ".tsx", ".js", ".jsx"}
        _MIN_SIGNATURES_FOR_IMPORT_GRAPH = 10
        ts_js_sig_count = sum(1 for f in signatures if Path(f).suffix.lower() in _TS_JS_EXTS) if signatures else 0
        if signatures and ts_js_sig_count >= _MIN_SIGNATURES_FOR_IMPORT_GRAPH:
            from faultline.analyzer.import_graph import build_import_clusters, scan_domains, load_tsconfig_paths, detect_monorepo_packages
            domains = scan_domains(analysis_files)
            domain_counts = {}
            for d in domains.values():
                if d != "__open__":
                    domain_counts[d] = domain_counts.get(d, 0) + 1
            if domain_counts:
                console.print(f"[dim]Domain boundaries: {len(domain_counts)} domains detected[/dim]")

            # Load tsconfig path aliases for better import resolution
            tsconfig_paths = load_tsconfig_paths(str(repo.working_tree_dir))
            if tsconfig_paths:
                console.print(f"[dim]tsconfig paths: {', '.join(tsconfig_paths.keys())}[/dim]")

            # Detect monorepo packages for bare import resolution
            monorepo_pkgs = detect_monorepo_packages(str(repo.working_tree_dir))
            if monorepo_pkgs:
                console.print(f"[dim]Monorepo packages: {len(monorepo_pkgs)} detected[/dim]")

            raw_mapping = build_import_clusters(
                analysis_files, signatures,
                tsconfig_paths=tsconfig_paths,
                monorepo_packages=monorepo_pkgs or None,
            )
            console.print(
                f"[dim]Import graph: {ts_js_sig_count} TS/JS files → {len(raw_mapping)} clusters[/dim]"
            )

            # Compute inter-cluster import connections for LLM context
            from faultline.analyzer.import_graph import compute_cluster_edges
            edges = compute_cluster_edges(
                raw_mapping, signatures,
                file_set=set(analysis_files),
                alias_map=tsconfig_paths,
                monorepo_packages=monorepo_pkgs or None,
            )
            if edges:
                total_cross = sum(sum(v.values()) for v in edges.values())
                console.print(f"[dim]Inter-cluster edges: {total_cross} cross-imports[/dim]")

            # Step 2a — LLM: merge related clusters into business features + name them
            if llm:
                raw_mapping = _merge_and_name_with_llm(
                    raw_mapping, provider, api_key, model, ollama_url,
                    commits=commits, layer_context=layer_context,
                    cluster_edges=edges,
                )
        elif llm:
            # No import graph (Python, Ruby, Go, etc.) — LLM does file-level detection
            # directly, which is much better than heuristic for monolith repos.
            console.print("[blue]No TS/JS files — using LLM file-level detection[/blue]")
            raw_mapping = _detect_with_llm(
                analysis_files, provider, api_key, model, ollama_url,
                commits=commits, path_prefix=path_prefix, signatures=signatures,
                layer_context=layer_context,
            )
        else:
            console.print("[dim]No TS/JS files — using directory heuristic[/dim]")
            raw_mapping = detect_features_from_structure(analysis_files)

        # Split oversized features — only for non-TS/JS repos (Django/Rails monoliths)
        # TS/JS repos already have fine-grained features from import graph + LLM merge
        if ts_js_sig_count < _MIN_SIGNATURES_FOR_IMPORT_GRAPH:
            raw_mapping = split_large_features(raw_mapping)

        # Restore full paths so commit matching works against git history
        if path_prefix:
            feature_paths = {
                name: [path_prefix + f for f in paths]
                for name, paths in raw_mapping.items()
            }
        else:
            feature_paths = raw_mapping

        console.print(f"[green]✓[/green] Detected {len(feature_paths)} features")

        # 5b. Symbol-level attribution for shared files (TS/JS only)
        shared_attributions = None
        if signatures and ts_js_sig_count >= _MIN_SIGNATURES_FOR_IMPORT_GRAPH:
            from faultline.analyzer.import_graph import resolve_symbol_imports, load_tsconfig_paths as _ltp
            from faultline.analyzer.shared_files import build_shared_attributions

            # Build signatures index with full paths (matching feature_paths)
            full_path_sigs = {}
            for rel, sig in signatures.items():
                full_key = (path_prefix + rel) if path_prefix else rel
                full_path_sigs[full_key] = sig

            tsconfig = load_tsconfig_paths(str(repo.working_tree_dir)) if not locals().get("tsconfig_paths") else tsconfig_paths
            symbol_imports = resolve_symbol_imports(
                full_path_sigs,
                alias_map=tsconfig,
                monorepo_packages=monorepo_pkgs if locals().get("monorepo_pkgs") else None,
            )
            shared_attributions = build_shared_attributions(
                feature_paths, symbol_imports, full_path_sigs,
            )
            if shared_attributions:
                shared_count = sum(len(v) for v in shared_attributions.values())
                console.print(f"[dim]Symbol attribution: {shared_count} shared file mappings across {len(shared_attributions)} features[/dim]")

        # 6. Build the feature map
        feature_map = build_feature_map(
            repo_path=repo_path,
            commits=commits,
            feature_paths=feature_paths,
            days=days,
            remote_url=remote_url,
            shared_attributions=shared_attributions,
        )

        # 6b. Read coverage data (if available)
        from faultline.analyzer.coverage import read_coverage
        coverage_data = read_coverage(str(repo.working_tree_dir), coverage_path=coverage)
        if coverage_data:
            console.print(f"[dim]Coverage data: {len(coverage_data)} files[/dim]")
            _apply_feature_coverage(feature_map, coverage_data, path_prefix)

        # 6c. Detect flows within each feature (optional)
        if flows:
            from faultline.llm.flow_detector import detect_e2e_anchors
            e2e_anchors = detect_e2e_anchors(analysis_files)
            if e2e_anchors:
                console.print(
                    f"[dim]E2E anchors: {len(e2e_anchors)} flows detected from test files[/dim]"
                )
            feature_map = _detect_flows(
                feature_map=feature_map,
                repo_path=str(repo.working_tree_dir),
                analysis_files=analysis_files,
                path_prefix=path_prefix,
                commits=commits,
                provider=provider,
                api_key=api_key,
                model=model,
                ollama_url=ollama_url,
                signatures=signatures,
                remote_url=remote_url,
                coverage_data=coverage_data,
                e2e_anchors=e2e_anchors,
            )

        # 6d. Analytics integration (optional)
        import os
        _posthog_key = posthog_key or os.environ.get("POSTHOG_API_KEY")
        _sentry_token = sentry_token or os.environ.get("SENTRY_AUTH_TOKEN")
        impact_scores = None

        if _posthog_key or _sentry_token:
            impact_scores = _run_analytics(
                feature_map=feature_map,
                posthog_key=_posthog_key,
                posthog_project=posthog_project,
                posthog_host=posthog_host,
                sentry_token=_sentry_token,
                sentry_org=sentry_org,
                sentry_project=sentry_project,
                sentry_host=sentry_host,
            )

        # 7. Print the report
        print_report(feature_map, impact_scores=impact_scores)

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


def _merge_and_name_with_llm(
    cluster_mapping: dict[str, list[str]],
    provider: str,
    api_key: str | None,
    model: str | None,
    ollama_url: str,
    commits=None,
    layer_context: str = "",
    cluster_edges: dict[str, dict[str, int]] | None = None,
) -> dict[str, list[str]]:
    """Merges import-graph clusters into business features and names them.

    Unlike simple naming, the LLM can merge N clusters → M features (M ≤ N),
    grouping clusters that serve the same business purpose even when they
    don't share direct import connections (e.g. a Redux slice + its page component).

    When commits are provided, top commit message keywords per cluster are injected
    into the prompt as semantic naming hints.

    Results are cached by cluster structure hash — same codebase → same output.
    Falls back to the original cluster_mapping on any LLM error.
    """
    if provider == "anthropic":
        from faultline.llm.detector import merge_and_name_clusters_llm
        console.print("[blue]Merging & naming features with Claude...[/blue]")
        named = merge_and_name_clusters_llm(
            cluster_mapping, api_key=api_key, commits=commits,
            layer_context=layer_context, cluster_edges=cluster_edges,
        )

    elif provider == "ollama":
        from faultline.llm.detector import merge_and_name_clusters_ollama, _DEFAULT_OLLAMA_MODEL
        resolved_model = model or _DEFAULT_OLLAMA_MODEL
        console.print(f"[blue]Merging & naming features with Ollama ({resolved_model})...[/blue]")
        named = merge_and_name_clusters_ollama(
            cluster_mapping, model=resolved_model, host=ollama_url, commits=commits, layer_context=layer_context
        )

    else:
        named = cluster_mapping

    label = "Claude" if provider == "anthropic" else "Ollama"
    console.print(f"[green]✓[/green] {label} merged → {len(named)} features")
    return named


def _detect_with_llm(
    files: list[str],
    provider: str,
    api_key: str | None,
    model: str | None,
    ollama_url: str,
    commits=None,
    path_prefix: str = "",
    signatures=None,
    layer_context: str = "",
) -> dict[str, list[str]]:
    """Sends files directly to LLM for feature detection (no import graph).

    Used for Python, Ruby, Go repos where import graph is unavailable.
    Falls back to directory heuristic on any LLM error.
    """
    if provider == "anthropic":
        from faultline.llm.detector import detect_features_llm
        result = detect_features_llm(
            files, api_key=api_key, commits=commits,
            path_prefix=path_prefix, signatures=signatures,
            layer_context=layer_context,
        )
    elif provider == "ollama":
        from faultline.llm.detector import detect_features_ollama, _DEFAULT_OLLAMA_MODEL
        resolved_model = model or _DEFAULT_OLLAMA_MODEL
        result = detect_features_ollama(
            files, model=resolved_model, host=ollama_url, commits=commits,
            path_prefix=path_prefix, signatures=signatures,
            layer_context=layer_context,
        )
    else:
        result = {}

    if result:
        label = "Claude" if provider == "anthropic" else "Ollama"
        console.print(f"[green]✓[/green] {label} detected {len(result)} features")
        return result

    # Fallback to heuristic
    console.print("[yellow]LLM detection failed — falling back to directory heuristic[/yellow]")
    return detect_features_from_structure(files)


def _apply_feature_coverage(
    feature_map: "FeatureMap",
    coverage_data: dict[str, float],
    path_prefix: str,
) -> None:
    """Computes average line coverage per feature from coverage report data.

    Mutates feature_map.features in place, setting coverage_pct on each feature
    that has matching files in the coverage report.
    """
    from faultline.analyzer.features import _is_test_file

    for feature in feature_map.features:
        coverages = []
        for file_path in feature.paths:
            if _is_test_file(file_path):
                continue
            # Try matching with and without path_prefix
            full_path = f"{path_prefix}{file_path}" if path_prefix else file_path
            pct = coverage_data.get(full_path) or coverage_data.get(file_path)
            if pct is not None:
                coverages.append(pct)
        if coverages:
            feature.coverage_pct = round(sum(coverages) / len(coverages), 1)


def _detect_flows(
    feature_map,
    repo_path: str,
    analysis_files: list[str],
    path_prefix: str,
    commits,
    provider: str,
    api_key: str | None,
    model: str | None,
    ollama_url: str,
    signatures: dict | None = None,
    remote_url: str = "",
    coverage_data: dict | None = None,
    e2e_anchors: dict | None = None,
):
    """
    Runs flow detection for each feature and attaches Flow objects to the FeatureMap.
    Returns the updated FeatureMap (features with .flows populated).
    """
    from faultline.llm.flow_detector import detect_flows_llm, detect_flows_ollama, _DEFAULT_OLLAMA_MODEL as _OLLAMA_MODEL
    from faultline.llm.flow_detector import _FlowFileMapping

    label = "Claude" if provider == "anthropic" else "Ollama"
    console.print(f"[blue]Detecting flows with {label}...[/blue]")

    # Reuse signatures from feature detection if provided; otherwise extract now.
    # analysis_files are stripped of path_prefix, so reconstruct the correct root:
    # git_root/src/ when --src src/ is used, or just git_root otherwise.
    if not signatures:
        from faultline.analyzer.ast_extractor import extract_signatures
        from pathlib import Path as _Path
        extract_root = str(_Path(repo_path) / path_prefix) if path_prefix else repo_path
        signatures = extract_signatures(analysis_files, extract_root)
        console.print(f"[dim]Extracted signatures from {len(signatures)} TS/JS files[/dim]")

    updated_features = []
    total_flows = 0

    for feature in feature_map.features:
        # Restore analysis-relative paths (strip prefix was applied earlier)
        if path_prefix:
            analysis_feature_files = [
                f[len(path_prefix):] for f in feature.paths
                if f.startswith(path_prefix)
            ]
        else:
            analysis_feature_files = list(feature.paths)

        if not analysis_feature_files:
            updated_features.append(feature)
            continue

        # Skip flow detection for features with very few commits — not enough
        # signal to split into meaningful flows
        _MIN_COMMITS_FOR_FLOWS = 5
        if feature.total_commits < _MIN_COMMITS_FOR_FLOWS:
            updated_features.append(feature)
            continue

        # Filter e2e anchors to only those relevant to this feature's files
        feature_file_set = set(analysis_feature_files)
        feature_e2e = {
            flow_name: [f for f in files if f in feature_file_set]
            for flow_name, files in (e2e_anchors or {}).items()
        }
        feature_e2e = {k: v for k, v in feature_e2e.items() if v}

        # Collect commits touching this feature (for co-change enrichment)
        feature_commit_files = set(feature.paths)
        feature_commits = [
            c for c in commits
            if any(f in feature_commit_files for f in c.files_changed)
        ]

        # Detect flows for this feature
        if provider == "anthropic":
            flow_mappings = detect_flows_llm(
                feature_name=feature.name,
                feature_files=analysis_feature_files,
                signatures=signatures,
                api_key=api_key,
                e2e_anchors=feature_e2e or None,
                commits=feature_commits,
            )
        else:
            resolved_model = model or _OLLAMA_MODEL
            flow_mappings = detect_flows_ollama(
                feature_name=feature.name,
                feature_files=analysis_feature_files,
                signatures=signatures,
                model=resolved_model,
                host=ollama_url,
                e2e_anchors=feature_e2e or None,
                commits=feature_commits,
            )

        if not flow_mappings:
            updated_features.append(feature)
            continue

        # Restore full paths in flow mappings for commit matching
        if path_prefix:
            flow_file_mappings = {
                m.flow_name: [path_prefix + f for f in m.files]
                for m in flow_mappings
            }
        else:
            flow_file_mappings = {m.flow_name: m.files for m in flow_mappings}

        # Build metrics for each flow using the feature's commits
        flows = build_flows_metrics(feature_commits, flow_file_mappings, remote_url=remote_url, coverage_data=coverage_data)

        # Filter out ghost flows (0 commits in the analyzed period)
        flows = [f for f in flows if f.total_commits > 0]

        total_flows += len(flows)

        updated_features.append(feature.model_copy(update={"flows": flows}))

    console.print(f"[green]✓[/green] Detected {total_flows} flows across {len(updated_features)} features")
    return feature_map.model_copy(update={"features": updated_features})


def _run_analytics(
    feature_map,
    posthog_key: str | None,
    posthog_project: str | None,
    posthog_host: str,
    sentry_token: str | None,
    sentry_org: str | None,
    sentry_project: str | None,
    sentry_host: str,
) -> list | None:
    """Fetches analytics data and computes impact scores."""
    import asyncio
    from faultline.integrations.base import PageMetrics, ErrorMetrics, compute_impact_scores

    traffic: list[PageMetrics] = []
    errors: list[ErrorMetrics] = []

    async def _fetch():
        nonlocal traffic, errors

        # PostHog
        if posthog_key and posthog_project:
            from faultline.integrations.posthog_provider import PostHogProvider
            ph = PostHogProvider(
                api_key=posthog_key,
                project_id=posthog_project,
                host=posthog_host,
            )
            console.print("[blue]Connecting to PostHog...[/blue]")
            if await ph.validate_connection():
                console.print("[green]✓[/green] PostHog connected")
                traffic = await ph.get_page_traffic(days=30)
                console.print(f"[dim]  {len(traffic)} routes with traffic data[/dim]")
                ph_errors = await ph.get_error_counts(days=30)
                if ph_errors:
                    errors.extend(ph_errors)
                    console.print(f"[dim]  {len(ph_errors)} routes with error data[/dim]")
            else:
                console.print("[yellow]✗ PostHog connection failed[/yellow]")
            await ph.close()

        # Sentry
        if sentry_token and sentry_org and sentry_project:
            from faultline.integrations.sentry_provider import SentryProvider
            sn = SentryProvider(
                auth_token=sentry_token,
                organization=sentry_org,
                project=sentry_project,
                host=sentry_host,
            )
            console.print("[blue]Connecting to Sentry...[/blue]")
            if await sn.validate_connection():
                console.print("[green]✓[/green] Sentry connected")
                sn_errors = await sn.get_error_counts(days=30)
                if sn_errors:
                    errors.extend(sn_errors)
                    console.print(f"[dim]  {len(sn_errors)} routes with error data[/dim]")
            else:
                console.print("[yellow]✗ Sentry connection failed[/yellow]")
            await sn.close()

    asyncio.run(_fetch())

    if not traffic and not errors:
        console.print("[yellow]No analytics data retrieved[/yellow]")
        return None

    # Build flow dicts for impact computation
    flows_data = []
    for feature in feature_map.features:
        if feature.flows:
            for flow in feature.flows:
                flows_data.append({
                    "name": flow.name,
                    "health_score": flow.health_score,
                    "paths": flow.paths,
                })
        else:
            flows_data.append({
                "name": feature.name,
                "health_score": feature.health_score,
                "paths": feature.paths,
            })

    scores = compute_impact_scores(flows_data, traffic, errors)
    console.print(f"[green]✓[/green] Computed {len(scores)} impact scores")

    return scores


@app.command()
def version():
    """Shows the faultline version."""
    from faultline import __version__
    rprint(f"faultline [bold blue]v{__version__}[/bold blue]")


if __name__ == "__main__":
    app()
