"""Standalone enrichment CLI — `faultlines-enrich`.

    faultlines-enrich path/to/feature-map.json \\
        --posthog-key phx_... --posthog-project 12345 \\
        --sentry-token sntrys_... --sentry-org acme --sentry-project web \\
        --out feature-map.enriched.json

When no provider credentials are passed, the command is a no-op and
exits 1 with a hint. Reads `POSTHOG_API_KEY` and `SENTRY_AUTH_TOKEN`
env vars as fallbacks.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .enrich import (
    enrich_feature_map,
    load_feature_map,
    save_feature_map,
)

app = typer.Typer(add_completion=False, help="Enrich Faultlines feature maps with analytics.")
console = Console()


@app.command()
def main(
    feature_map_path: str = typer.Argument(
        ..., help="Path to feature-map.json produced by `faultlines analyze`",
    ),
    out: Optional[str] = typer.Option(
        None, "--out", "-o",
        help="Output path. Defaults to <input>.enriched.json",
    ),
    days: int = typer.Option(30, "--days", help="Lookback window in days."),
    posthog_key: Optional[str] = typer.Option(
        None, "--posthog-key", envvar="POSTHOG_API_KEY",
    ),
    posthog_project: Optional[str] = typer.Option(None, "--posthog-project"),
    posthog_host: str = typer.Option("https://app.posthog.com", "--posthog-host"),
    sentry_token: Optional[str] = typer.Option(
        None, "--sentry-token", envvar="SENTRY_AUTH_TOKEN",
    ),
    sentry_org: Optional[str] = typer.Option(None, "--sentry-org"),
    sentry_project: Optional[str] = typer.Option(None, "--sentry-project"),
    sentry_host: str = typer.Option("https://sentry.io", "--sentry-host"),
) -> None:
    """Read a feature map, fetch analytics, write enriched JSON."""
    in_path = Path(feature_map_path)
    if not in_path.exists():
        console.print(f"[red]✗[/red] not found: {in_path}")
        raise typer.Exit(1)

    providers = []
    if posthog_key and posthog_project:
        from .posthog import PostHogProvider
        providers.append(PostHogProvider(
            api_key=posthog_key,
            project_id=posthog_project,
            host=posthog_host,
        ))
    if sentry_token and sentry_org and sentry_project:
        from .sentry import SentryProvider
        providers.append(SentryProvider(
            auth_token=sentry_token,
            organization=sentry_org,
            project=sentry_project,
            host=sentry_host,
        ))

    if not providers:
        console.print(
            "[yellow]No provider credentials supplied. "
            "Pass --posthog-key/--sentry-token (or env vars).[/yellow]"
        )
        raise typer.Exit(1)

    feature_map = load_feature_map(in_path)
    console.print(
        f"[blue]Enriching[/blue] {in_path.name} via "
        f"{', '.join(p.name for p in providers)} ({days}d)"
    )

    enriched = enrich_feature_map(feature_map, providers=providers, days=days)

    out_path = Path(out) if out else in_path.with_suffix(".enriched.json")
    save_feature_map(enriched, out_path)

    meta = enriched["analytics_meta"]
    console.print(
        f"[green]✓[/green] Wrote {out_path} — "
        f"{meta['scored_flows']} flows scored "
        f"({meta['page_metrics_count']} routes traffic, "
        f"{meta['error_metrics_count']} routes errors)"
    )

    _print_top_impact(enriched["impact_scores"])


def _print_top_impact(scores: list[dict], limit: int = 15) -> None:
    if not scores:
        return
    table = Table(title=f"Top {min(limit, len(scores))} most-urgent flows")
    table.add_column("Flow")
    table.add_column("Health", justify="right")
    table.add_column("Views", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Impact")
    for s in scores[:limit]:
        table.add_row(
            s["flow_name"],
            f"{s['health_score']:.0f}",
            f"{s['pageviews']:,}",
            f"{s['error_count']:,}",
            f"{s['score']:.0f}",
            s["impact_level"].upper(),
        )
    console.print(table)


if __name__ == "__main__":
    sys.exit(app())
