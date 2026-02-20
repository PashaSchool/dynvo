import typer
from pathlib import Path
from typing import Optional
from rich.console import Console
from rich import print as rprint

from featuremap.analyzer.git import load_repo, get_commits, get_tracked_files
from featuremap.analyzer.features import detect_features_from_structure, build_feature_map
from featuremap.output.reporter import print_report
from featuremap.output.writer import write_feature_map

app = typer.Typer(
    name="featuremap",
    help="Analyze git history to map features and track technical debt",
    add_completion=False,
)
console = Console()


@app.command()
def analyze(
    repo_path: str = typer.Argument(
        ".",
        help="Шлях до git репозиторію",
    ),
    days: int = typer.Option(
        365,
        "--days", "-d",
        help="Кількість днів для аналізу",
    ),
    output: Optional[str] = typer.Option(
        None,
        "--output", "-o",
        help="Шлях для збереження feature-map.json",
    ),
    save: bool = typer.Option(
        True,
        "--save/--no-save",
        help="Зберігати feature-map.json",
    ),
    top: int = typer.Option(
        3,
        "--top",
        help="Кількість топ ризиків для відображення",
    ),
):
    """
    Аналізує git репозиторій та будує feature map.

    Приклади:
        featuremap analyze
        featuremap analyze ./my-project
        featuremap analyze . --days 90
        featuremap analyze . --output ./reports/feature-map.json
    """
    repo_path = str(Path(repo_path).resolve())

    try:
        # 1. Завантажуємо репозиторій
        console.print(f"[blue]Аналізуємо:[/blue] {repo_path}")
        repo = load_repo(repo_path)

        # 2. Отримуємо коміти
        commits = get_commits(repo, days=days)
        if not commits:
            console.print("[yellow]Не знайдено комітів за вказаний період[/yellow]")
            raise typer.Exit(1)

        console.print(f"[green]✓[/green] Знайдено {len(commits)} комітів за {days} днів")

        # 3. Отримуємо файли та визначаємо фічі
        files = get_tracked_files(repo)
        console.print(f"[green]✓[/green] Знайдено {len(files)} файлів")

        feature_paths = detect_features_from_structure(files)
        console.print(f"[green]✓[/green] Визначено {len(feature_paths)} фіч")

        # 4. Будуємо feature map
        feature_map = build_feature_map(
            repo_path=repo_path,
            commits=commits,
            feature_paths=feature_paths,
            days=days,
        )

        # 5. Виводимо звіт
        print_report(feature_map)

        # 6. Зберігаємо файл
        if save:
            saved_path = write_feature_map(feature_map, output)
            console.print(f"[dim]Збережено: {saved_path}[/dim]")

    except ValueError as e:
        console.print(f"[red]Помилка:[/red] {e}")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Скасовано[/yellow]")
        raise typer.Exit(0)


@app.command()
def version():
    """Показує версію featuremap"""
    from featuremap import __version__
    rprint(f"featuremap [bold blue]v{__version__}[/bold blue]")


if __name__ == "__main__":
    app()
