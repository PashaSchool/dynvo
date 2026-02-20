from pydantic import BaseModel
from datetime import datetime


class Commit(BaseModel):
    sha: str
    message: str
    author: str
    date: datetime
    files_changed: list[str]
    is_bug_fix: bool = False


class FileBlame(BaseModel):
    path: str
    authors: list[str]
    last_modified: datetime
    total_commits: int


class Feature(BaseModel):
    name: str
    paths: list[str]          # директорії/файли що належать до фічі
    authors: list[str]        # хто писав
    total_commits: int
    bug_fixes: int            # кількість bug fix комітів
    bug_fix_ratio: float      # bug_fixes / total_commits
    last_modified: datetime
    health_score: float       # 0-100, чим вище тим краще


class FeatureMap(BaseModel):
    repo_path: str
    analyzed_at: datetime
    total_commits: int
    date_range_days: int
    features: list[Feature]

    def sorted_by_risk(self) -> list[Feature]:
        """Повертає фічі відсортовані від найризикованіших"""
        return sorted(self.features, key=lambda f: f.bug_fix_ratio, reverse=True)
