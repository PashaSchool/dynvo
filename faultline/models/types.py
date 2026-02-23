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


class Flow(BaseModel):
    name: str                  # "checkout-flow", "login-flow"
    description: str | None = None
    paths: list[str]           # files belonging to this flow
    authors: list[str]
    total_commits: int
    bug_fixes: int
    bug_fix_ratio: float
    last_modified: datetime
    health_score: float        # 0-100, higher is better


class Feature(BaseModel):
    name: str
    description: str | None = None  # LLM-generated semantic description
    paths: list[str]          # directories/files belonging to this feature
    authors: list[str]        # contributors
    total_commits: int
    bug_fixes: int            # number of bug fix commits
    bug_fix_ratio: float      # bug_fixes / total_commits
    last_modified: datetime
    health_score: float       # 0-100, higher is better
    flows: list[Flow] = []    # populated when --flows flag is used


class FeatureMap(BaseModel):
    repo_path: str
    analyzed_at: datetime
    total_commits: int
    date_range_days: int
    features: list[Feature]

    def sorted_by_risk(self) -> list[Feature]:
        """Returns features sorted from highest to lowest risk."""
        return sorted(self.features, key=lambda f: f.bug_fix_ratio, reverse=True)
