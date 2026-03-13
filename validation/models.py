"""Pydantic models for the validation pipeline."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class PhaseStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


class RepoTarget(BaseModel):
    name: str
    url: str
    expected_features: list[str] = []
    src_filter: str | None = None
    reason: str = ""


class FeatureMatch(BaseModel):
    expected: str
    detected: str
    confidence: float = 0.0
    files_overlap_pct: float = 0.0
    notes: str = ""


class ValidationResult(BaseModel):
    repo_name: str
    detected_features: list[str] = []
    expected_features: list[str] = []
    matched_features: list[FeatureMatch] = []
    missed_features: list[str] = []
    spurious_features: list[str] = []
    precision: float = 0.0
    recall: float = 0.0
    f1_score: float = 0.0
    metric_issues: list[str] = []
    agent_reasoning: str = ""


class RepoProgress(BaseModel):
    repo: RepoTarget
    clone_status: PhaseStatus = PhaseStatus.pending
    analyze_status: PhaseStatus = PhaseStatus.pending
    validate_status: PhaseStatus = PhaseStatus.pending
    feature_map_path: str | None = None
    validation_result: ValidationResult | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class OverallSummary(BaseModel):
    total_repos: int = 0
    successful_repos: int = 0
    avg_precision: float = 0.0
    avg_recall: float = 0.0
    avg_f1: float = 0.0
    total_features_detected: int = 0
    total_features_expected: int = 0


class OverallProgress(BaseModel):
    phase: str = "research"
    repos: list[RepoProgress] = []
    started_at: datetime = Field(default_factory=lambda: datetime.now())
    updated_at: datetime = Field(default_factory=lambda: datetime.now())
    summary: OverallSummary | None = None
