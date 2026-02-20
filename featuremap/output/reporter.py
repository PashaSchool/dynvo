from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from featuremap.models.types import FeatureMap, Feature

console = Console()


def _health_color(score: float) -> str:
    if score >= 75:
        return "green"
    elif score >= 50:
        return "yellow"
    else:
        return "red"


def _health_icon(score: float) -> str:
    if score >= 75:
        return "✓"
    elif score >= 50:
        return "!"
    else:
        return "✗"


def print_summary(feature_map: FeatureMap) -> None:
    """Виводить загальний summary аналізу"""
    total_bugs = sum(f.bug_fixes for f in feature_map.features)
    avg_health = sum(f.health_score for f in feature_map.features) / len(feature_map.features) \
        if feature_map.features else 0

    console.print()
    console.print(Panel(
        f"[bold]Репозиторій:[/bold] {feature_map.repo_path}\n"
        f"[bold]Аналіз за:[/bold] останні {feature_map.date_range_days} днів\n"
        f"[bold]Всього комітів:[/bold] {feature_map.total_commits}\n"
        f"[bold]Фіч знайдено:[/bold] {len(feature_map.features)}\n"
        f"[bold]Bug fix комітів:[/bold] {total_bugs}\n"
        f"[bold]Середній health score:[/bold] [{_health_color(avg_health)}]{avg_health:.1f}/100[/]",
        title="[bold blue]FeatureMap Analysis[/bold blue]",
        border_style="blue",
    ))


def print_features_table(feature_map: FeatureMap) -> None:
    """Виводить таблицю з фічами відсортовану від найризикованіших"""
    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        title="Features by Risk",
        title_style="bold",
    )

    table.add_column("", width=3)
    table.add_column("Feature", style="bold", min_width=15)
    table.add_column("Health", justify="center", width=10)
    table.add_column("Commits", justify="right", width=10)
    table.add_column("Bug Fixes", justify="right", width=10)
    table.add_column("Bug %", justify="right", width=8)
    table.add_column("Authors", justify="right", width=10)
    table.add_column("Files", justify="right", width=8)

    for feature in feature_map.sorted_by_risk():
        color = _health_color(feature.health_score)
        icon = _health_icon(feature.health_score)
        bug_pct = f"{feature.bug_fix_ratio * 100:.1f}%"

        table.add_row(
            f"[{color}]{icon}[/]",
            feature.name,
            f"[{color}]{feature.health_score:.0f}[/]",
            str(feature.total_commits),
            f"[{color}]{feature.bug_fixes}[/]" if feature.bug_fixes > 0 else "0",
            f"[{color}]{bug_pct}[/]",
            str(len(feature.authors)),
            str(len(feature.paths)),
        )

    console.print()
    console.print(table)


def print_top_risks(feature_map: FeatureMap, top: int = 3) -> None:
    """Виводить топ найризикованіших фіч з деталями"""
    risky = [f for f in feature_map.sorted_by_risk() if f.bug_fixes > 0][:top]

    if not risky:
        console.print("\n[green]Жодних критичних зон не виявлено![/green]")
        return

    console.print(f"\n[bold red]Топ {top} зон ризику:[/bold red]")

    for i, feature in enumerate(risky, 1):
        color = _health_color(feature.health_score)
        console.print(
            f"  {i}. [bold {color}]{feature.name}[/bold {color}] — "
            f"{feature.bug_fixes} bug fixes з {feature.total_commits} комітів "
            f"({feature.bug_fix_ratio * 100:.1f}%)"
        )


def print_report(feature_map: FeatureMap) -> None:
    """Повний звіт в терміналі"""
    print_summary(feature_map)
    print_features_table(feature_map)
    print_top_risks(feature_map)
    console.print()
