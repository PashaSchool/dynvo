import re
from datetime import datetime, timezone
from pathlib import Path

from git import Repo, InvalidGitRepositoryError
from rich.progress import Progress, SpinnerColumn, TextColumn

from featuremap.models.types import Commit, FileBlame

# Патерни які вказують що коміт це bug fix
BUG_FIX_PATTERNS = [
    r"\bfix\b", r"\bbug\b", r"\bhotfix\b", r"\bpatch\b",
    r"\brevert\b", r"\bregression\b", r"\bcrash\b", r"\berror\b",
    r"\bbroken\b", r"\bissue\b", r"\bdefect\b",
]

BUG_FIX_REGEX = re.compile("|".join(BUG_FIX_PATTERNS), re.IGNORECASE)


def is_bug_fix(message: str) -> bool:
    return bool(BUG_FIX_REGEX.search(message))


def load_repo(path: str) -> Repo:
    try:
        return Repo(path, search_parent_directories=True)
    except InvalidGitRepositoryError:
        raise ValueError(f"'{path}' не є git репозиторієм")


def get_commits(repo: Repo, days: int = 365) -> list[Commit]:
    """Отримує всі коміти за останні N днів"""
    commits = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task("Читаємо git history...", total=None)

        for commit in repo.iter_commits(max_count=5000):
            commit_date = datetime.fromtimestamp(
                commit.committed_date, tz=timezone.utc
            )

            # Фільтруємо по даті
            age_days = (datetime.now(tz=timezone.utc) - commit_date).days
            if age_days > days:
                break

            files_changed = list(commit.stats.files.keys())

            commits.append(Commit(
                sha=commit.hexsha[:8],
                message=commit.message.strip(),
                author=str(commit.author.name),
                date=commit_date,
                files_changed=files_changed,
                is_bug_fix=is_bug_fix(commit.message),
            ))

    return commits


def get_file_blame(repo: Repo, file_path: str) -> FileBlame | None:
    """Отримує blame інформацію для файлу"""
    try:
        blame = repo.blame("HEAD", file_path)
        authors = set()
        latest_date = None

        for blame_commit, _ in blame:
            authors.add(str(blame_commit.author.name))
            commit_date = datetime.fromtimestamp(
                blame_commit.committed_date, tz=timezone.utc
            )
            if latest_date is None or commit_date > latest_date:
                latest_date = commit_date

        return FileBlame(
            path=file_path,
            authors=list(authors),
            last_modified=latest_date or datetime.now(tz=timezone.utc),
            total_commits=len(blame),
        )
    except Exception:
        return None


def get_tracked_files(repo: Repo) -> list[str]:
    """Повертає список всіх файлів у репозиторії"""
    skip_extensions = {
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
        ".pdf", ".zip", ".tar", ".gz", ".lock", ".sum",
    }
    skip_dirs = {
        "node_modules", ".git", "dist", "build", "__pycache__",
        ".next", "vendor", "venv", ".venv",
    }

    files = []
    for item in repo.tree().traverse():
        if item.type != "blob":
            continue

        path = Path(item.path)

        # Пропускаємо непотрібні файли
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix.lower() in skip_extensions:
            continue

        files.append(item.path)

    return files
