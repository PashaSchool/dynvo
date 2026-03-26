/* ── Feature Map types (from CLI) ── */

export interface TimelinePoint {
  date: string;
  total_commits: number;
  bug_fix_commits: number;
  test_commits: number;
}

export interface PullRequest {
  number: number;
  url: string;
  title: string;
  author: string;
  date: string;
}

export interface Flow {
  name: string;
  description?: string;
  paths: string[];
  authors: string[];
  total_commits: number;
  bug_fixes: number;
  bug_fix_ratio: number;
  last_modified: string;
  health_score: number;
  bug_fix_prs?: PullRequest[];
  test_file_count?: number;
  weekly_points?: TimelinePoint[];
  bus_factor?: number;
  health_trend?: number | null;
  hotspot_files?: string[];
  coverage_pct?: number | null;
}

export interface Feature {
  name: string;
  description?: string;
  paths: string[];
  authors: string[];
  total_commits: number;
  bug_fixes: number;
  bug_fix_ratio: number;
  last_modified: string;
  health_score: number;
  flows: Flow[];
  bug_fix_prs?: PullRequest[];
  coverage_pct?: number | null;
}

export interface FeatureMap {
  repo_path: string;
  remote_url?: string;
  analyzed_at: string;
  total_commits: number;
  date_range_days: number;
  features: Feature[];
}

/* ── Analytics types ── */

export interface PageMetrics {
  route: string;
  pageviews: number;
  unique_visitors: number;
  avg_session_duration_sec: number;
  bounce_rate?: number | null;
}

export interface ErrorEntry {
  title: string;
  count: number;
  url: string;
}

export interface ErrorMetrics {
  route: string;
  error_count: number;
  unique_errors: number;
  top_errors: ErrorEntry[];
}

export interface ImpactScore {
  flow_name: string;
  health_score: number;
  pageviews: number;
  error_count: number;
  impact_level: "critical" | "high" | "medium" | "low" | "healthy";
  score: number;
}

/* ── Settings types ── */

export type AnalyticsProviderType = "posthog" | "sentry" | "ga4" | "amplitude" | "mixpanel" | "plausible";

export interface AnalyticsConnection {
  provider: AnalyticsProviderType;
  is_connected: boolean;
  api_key: string;
  project_id: string;
  host: string;
  organization?: string;
  last_synced?: string;
}

export interface GitHubCommentConfig {
  is_enabled: boolean;
  show_health_score: boolean;
  show_bug_fix_ratio: boolean;
  show_top_risks: boolean;
  show_flows: boolean;
  show_analytics: boolean;
  show_impact_score: boolean;
  show_coverage: boolean;
  show_authors: boolean;
  max_features_in_comment: number;
  comment_style: "compact" | "detailed" | "minimal";
}

export interface ProjectSettings {
  id: string;
  repo_name: string;
  repo_url: string;
  default_branch: string;
  src_path: string;
  analysis_days: number;
  max_commits: number;
  llm_enabled: boolean;
  flows_enabled: boolean;
  auto_analyze_on_push: boolean;
  auto_analyze_on_pr: boolean;
  schedule_cron: string;
}

export interface ApiKeys {
  anthropic_key: string;
  anthropic_model: string;
  ollama_url: string;
  ollama_model: string;
  provider_preference: "anthropic" | "ollama";
}

export interface TeamMember {
  id: string;
  name: string;
  email: string;
  role: "owner" | "admin" | "member" | "viewer";
  avatar_url: string;
}

export interface BillingPlan {
  name: "free" | "team" | "business" | "enterprise";
  price_per_seat: number;
  seats_used: number;
  seats_total: number;
  current_period_end: string;
}
