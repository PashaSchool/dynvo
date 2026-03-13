export interface FeatureMatch {
  expected: string;
  detected: string;
  confidence: number;
  files_overlap_pct: number;
  notes: string;
}

export interface ValidationResult {
  repo_name: string;
  detected_features: string[];
  expected_features: string[];
  matched_features: FeatureMatch[];
  missed_features: string[];
  spurious_features: string[];
  precision: number;
  recall: number;
  f1_score: number;
  metric_issues: string[];
  agent_reasoning: string;
}

export interface RepoTarget {
  name: string;
  url: string;
  expected_features: string[];
  src_filter: string | null;
  reason: string;
}

export type PhaseStatus = "pending" | "running" | "completed" | "failed" | "skipped";

export interface RepoProgress {
  repo: RepoTarget;
  clone_status: PhaseStatus;
  analyze_status: PhaseStatus;
  validate_status: PhaseStatus;
  feature_map_path: string | null;
  validation_result: ValidationResult | null;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface OverallSummary {
  total_repos: number;
  successful_repos: number;
  avg_precision: number;
  avg_recall: number;
  avg_f1: number;
  total_features_detected: number;
  total_features_expected: number;
}

export interface OverallProgress {
  phase: string;
  repos: RepoProgress[];
  started_at: string;
  updated_at: string;
  summary: OverallSummary | null;
}
