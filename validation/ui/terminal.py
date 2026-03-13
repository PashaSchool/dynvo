"""Rich Live terminal UI for pipeline progress."""

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from ..models import OverallProgress, PhaseStatus

_STATUS_ICONS = {
    PhaseStatus.pending:   "[dim]○[/dim]",
    PhaseStatus.running:   "[yellow]◉[/yellow]",
    PhaseStatus.completed: "[green]✓[/green]",
    PhaseStatus.failed:    "[red]✗[/red]",
    PhaseStatus.skipped:   "[dim]–[/dim]",
}

_PHASE_LABELS = {
    "research":   "[bold cyan]Phase 1:[/] Selecting repositories...",
    "running":    "[bold cyan]Phase 2:[/] Cloning & analysing...",
    "validating": "[bold cyan]Phase 3:[/] Validating results...",
    "done":       "[bold green]Pipeline complete[/]",
}


def build_table(progress: OverallProgress) -> Table:
    """Builds the main progress table."""
    table = Table(
        title="faultline validation pipeline",
        title_style="bold",
        show_lines=False,
        padding=(0, 1),
    )
    table.add_column("Repository", style="bold white", min_width=28)
    table.add_column("Clone", justify="center", width=7)
    table.add_column("Analyze", justify="center", width=9)
    table.add_column("Validate", justify="center", width=10)
    table.add_column("Precision", justify="right", width=10)
    table.add_column("Recall", justify="right", width=10)
    table.add_column("F1", justify="right", width=8)

    for rp in progress.repos:
        vr = rp.validation_result
        precision = f"{vr.precision:.0%}" if vr else "—"
        recall = f"{vr.recall:.0%}" if vr else "—"
        f1 = f"{vr.f1_score:.0%}" if vr else "—"

        if vr and vr.f1_score >= 0.7:
            f1 = f"[green]{f1}[/green]"
        elif vr and vr.f1_score >= 0.4:
            f1 = f"[yellow]{f1}[/yellow]"
        elif vr:
            f1 = f"[red]{f1}[/red]"

        error_suffix = ""
        if rp.error:
            short = rp.error[:40] + "..." if len(rp.error) > 40 else rp.error
            error_suffix = f"\n  [dim red]{short}[/dim red]"

        table.add_row(
            rp.repo.name + error_suffix,
            _STATUS_ICONS.get(rp.clone_status, "?"),
            _STATUS_ICONS.get(rp.analyze_status, "?"),
            _STATUS_ICONS.get(rp.validate_status, "?"),
            precision,
            recall,
            f1,
        )

    return table


def build_summary_panel(progress: OverallProgress) -> Panel:
    """Builds the bottom summary panel."""
    s = progress.summary
    if s:
        text = (
            f"[bold]{s.successful_repos}/{s.total_repos}[/] repos validated  |  "
            f"Avg precision: [bold]{s.avg_precision:.0%}[/]  |  "
            f"Avg recall: [bold]{s.avg_recall:.0%}[/]  |  "
            f"Avg F1: [bold]{s.avg_f1:.0%}[/]  |  "
            f"Features: {s.total_features_detected} detected / {s.total_features_expected} expected"
        )
    else:
        phase_label = _PHASE_LABELS.get(progress.phase, progress.phase)
        done = sum(
            1 for rp in progress.repos
            if rp.validate_status in (PhaseStatus.completed, PhaseStatus.failed)
        )
        total = len(progress.repos)
        text = f"{phase_label}  [{done}/{total} repos]"

    return Panel(text, border_style="cyan", padding=(0, 2))


def build_display(progress: OverallProgress) -> Layout:
    """Builds the full terminal layout."""
    layout = Layout()
    layout.split_column(
        Layout(build_table(progress), name="table", ratio=4),
        Layout(build_summary_panel(progress), name="summary", size=3),
    )
    return layout


class LiveUI:
    """Manages Rich Live display, updates on progress changes."""

    def __init__(self) -> None:
        self._progress = OverallProgress()
        self._live: Live | None = None

    def start(self) -> None:
        self._live = Live(
            build_display(self._progress),
            refresh_per_second=4,
        )
        self._live.start()

    def stop(self) -> None:
        if self._live:
            self._live.stop()
            self._live = None

    def update(self, progress: OverallProgress) -> None:
        self._progress = progress
        if self._live:
            self._live.update(build_display(progress))

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
