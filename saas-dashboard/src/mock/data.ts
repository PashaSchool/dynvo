import type {
  AnalyticsConnection,
  ApiKeys,
  BillingPlan,
  Feature,
  FeatureMap,
  GitHubCommentConfig,
  ImpactScore,
  ProjectSettings,
  TeamMember,
} from "../types";

/* ── Mock Feature Map ── */

export const mockFeatureMap: FeatureMap = {
  repo_path: "/app/ecommerce-platform",
  remote_url: "https://github.com/acme/ecommerce-platform",
  analyzed_at: "2026-03-25T14:30:00Z",
  total_commits: 1847,
  date_range_days: 365,
  features: [
    {
      name: "Checkout",
      description: "Payment processing, cart management, and order completion flow",
      paths: ["src/checkout/", "src/cart/", "src/payments/"],
      authors: ["alice", "bob", "charlie"],
      total_commits: 342,
      bug_fixes: 89,
      bug_fix_ratio: 0.26,
      last_modified: "2026-03-24T10:00:00Z",
      health_score: 32,
      coverage_pct: 64,
      flows: [
        {
          name: "payment-processing",
          description: "Handles Stripe payment intents and confirmation",
          paths: ["src/checkout/PaymentForm.tsx", "src/payments/stripe.ts", "src/payments/webhooks.ts"],
          authors: ["alice", "bob"],
          total_commits: 156,
          bug_fixes: 47,
          bug_fix_ratio: 0.30,
          last_modified: "2026-03-24T10:00:00Z",
          health_score: 28,
          test_file_count: 3,
          bus_factor: 1,
          health_trend: -0.05,
          hotspot_files: ["src/payments/stripe.ts"],
          coverage_pct: 52,
        },
        {
          name: "cart-management",
          description: "Add/remove items, quantity updates, promo codes",
          paths: ["src/cart/CartProvider.tsx", "src/cart/CartDrawer.tsx", "src/cart/hooks.ts"],
          authors: ["alice", "charlie"],
          total_commits: 98,
          bug_fixes: 21,
          bug_fix_ratio: 0.21,
          last_modified: "2026-03-20T08:00:00Z",
          health_score: 45,
          test_file_count: 5,
          bus_factor: 2,
          health_trend: 0.03,
          coverage_pct: 78,
        },
      ],
      bug_fix_prs: [
        { number: 892, url: "https://github.com/acme/ecommerce-platform/pull/892", title: "fix: PaymentIntent timeout on slow connections", author: "alice", date: "2026-03-22T09:00:00Z" },
        { number: 887, url: "https://github.com/acme/ecommerce-platform/pull/887", title: "fix: cart total not updating after promo code", author: "charlie", date: "2026-03-18T14:00:00Z" },
      ],
    },
    {
      name: "User Authentication",
      description: "Login, registration, password reset, OAuth flows",
      paths: ["src/auth/"],
      authors: ["bob", "diana"],
      total_commits: 187,
      bug_fixes: 23,
      bug_fix_ratio: 0.12,
      last_modified: "2026-03-15T12:00:00Z",
      health_score: 71,
      coverage_pct: 82,
      flows: [
        {
          name: "oauth-login",
          description: "GitHub and Google OAuth login flows",
          paths: ["src/auth/OAuthCallback.tsx", "src/auth/providers.ts"],
          authors: ["bob"],
          total_commits: 67,
          bug_fixes: 12,
          bug_fix_ratio: 0.18,
          last_modified: "2026-03-15T12:00:00Z",
          health_score: 62,
          test_file_count: 4,
          bus_factor: 1,
          health_trend: 0.02,
          coverage_pct: 85,
        },
      ],
      bug_fix_prs: [],
    },
    {
      name: "Dashboard Analytics",
      description: "Charts, metrics, reporting for merchant dashboard",
      paths: ["src/dashboard/", "src/analytics/"],
      authors: ["charlie", "diana", "eve"],
      total_commits: 256,
      bug_fixes: 18,
      bug_fix_ratio: 0.07,
      last_modified: "2026-03-23T16:00:00Z",
      health_score: 88,
      coverage_pct: 91,
      flows: [],
      bug_fix_prs: [],
    },
    {
      name: "Product Catalog",
      description: "Product listing, search, filtering, and detail pages",
      paths: ["src/products/", "src/search/"],
      authors: ["alice", "eve", "frank"],
      total_commits: 412,
      bug_fixes: 56,
      bug_fix_ratio: 0.14,
      last_modified: "2026-03-25T09:00:00Z",
      health_score: 65,
      coverage_pct: 73,
      flows: [
        {
          name: "product-search",
          description: "Elasticsearch-powered product search with filters",
          paths: ["src/search/SearchBar.tsx", "src/search/filters.ts", "src/search/api.ts"],
          authors: ["eve", "frank"],
          total_commits: 145,
          bug_fixes: 28,
          bug_fix_ratio: 0.19,
          last_modified: "2026-03-25T09:00:00Z",
          health_score: 55,
          test_file_count: 6,
          bus_factor: 2,
          health_trend: -0.02,
          coverage_pct: 68,
        },
      ],
      bug_fix_prs: [],
    },
    {
      name: "Notifications",
      description: "Email, push, and in-app notification system",
      paths: ["src/notifications/"],
      authors: ["frank"],
      total_commits: 89,
      bug_fixes: 4,
      bug_fix_ratio: 0.04,
      last_modified: "2026-02-28T11:00:00Z",
      health_score: 94,
      coverage_pct: 88,
      flows: [],
      bug_fix_prs: [],
    },
    {
      name: "Admin Panel",
      description: "Internal admin tools, user management, system config",
      paths: ["src/admin/"],
      authors: ["bob", "diana"],
      total_commits: 134,
      bug_fixes: 31,
      bug_fix_ratio: 0.23,
      last_modified: "2026-03-21T14:00:00Z",
      health_score: 41,
      coverage_pct: 38,
      flows: [],
      bug_fix_prs: [],
    },
  ],
};

/* ── Mock Impact Scores ── */

export const mockImpactScores: ImpactScore[] = [
  { flow_name: "payment-processing", health_score: 28, pageviews: 48291, error_count: 847, impact_level: "critical", score: 12 },
  { flow_name: "product-search", health_score: 55, pageviews: 31205, error_count: 234, impact_level: "high", score: 35 },
  { flow_name: "cart-management", health_score: 45, pageviews: 22150, error_count: 156, impact_level: "high", score: 42 },
  { flow_name: "oauth-login", health_score: 62, pageviews: 12004, error_count: 23, impact_level: "medium", score: 58 },
  { flow_name: "Dashboard Analytics", health_score: 88, pageviews: 8441, error_count: 5, impact_level: "healthy", score: 84 },
  { flow_name: "Notifications", health_score: 94, pageviews: 312, error_count: 2, impact_level: "healthy", score: 92 },
];

/* ── Mock Analytics Connections ── */

export const mockAnalyticsConnections: AnalyticsConnection[] = [
  {
    provider: "posthog",
    is_connected: true,
    api_key: "phx_1a2b3c4d...redacted",
    project_id: "12345",
    host: "https://app.posthog.com",
    last_synced: "2026-03-25T14:00:00Z",
  },
  {
    provider: "sentry",
    is_connected: true,
    api_key: "sntrys_abc...redacted",
    project_id: "ecommerce-platform",
    host: "https://sentry.io",
    organization: "acme",
    last_synced: "2026-03-25T14:00:00Z",
  },
  {
    provider: "ga4",
    is_connected: false,
    api_key: "",
    project_id: "",
    host: "https://analyticsdata.googleapis.com",
  },
  {
    provider: "amplitude",
    is_connected: false,
    api_key: "",
    project_id: "",
    host: "https://amplitude.com",
  },
  {
    provider: "mixpanel",
    is_connected: false,
    api_key: "",
    project_id: "",
    host: "https://mixpanel.com",
  },
  {
    provider: "plausible",
    is_connected: false,
    api_key: "",
    project_id: "",
    host: "https://plausible.io",
  },
];

/* ── Mock GitHub Comment Config ── */

export const mockGitHubCommentConfig: GitHubCommentConfig = {
  is_enabled: true,
  show_health_score: true,
  show_bug_fix_ratio: true,
  show_top_risks: true,
  show_flows: true,
  show_analytics: true,
  show_impact_score: true,
  show_coverage: true,
  show_authors: false,
  max_features_in_comment: 5,
  comment_style: "detailed",
};

/* ── Mock Project Settings ── */

export const mockProjectSettings: ProjectSettings = {
  id: "proj_abc123",
  repo_name: "acme/ecommerce-platform",
  repo_url: "https://github.com/acme/ecommerce-platform",
  default_branch: "main",
  src_path: "src/",
  analysis_days: 365,
  max_commits: 5000,
  llm_enabled: true,
  flows_enabled: true,
  auto_analyze_on_push: true,
  auto_analyze_on_pr: true,
  schedule_cron: "0 2 * * 1",
};

/* ── Mock API Keys ── */

export const mockApiKeys: ApiKeys = {
  anthropic_key: "sk-ant-...redacted",
  anthropic_model: "claude-haiku-4-5-20251001",
  ollama_url: "",
  ollama_model: "llama3.1:8b",
  provider_preference: "anthropic",
};

/* ── Mock Team ── */

export const mockTeamMembers: TeamMember[] = [
  { id: "1", name: "Pasha Kuzina", email: "pasha@acme.com", role: "owner", avatar_url: "" },
  { id: "2", name: "Alice Chen", email: "alice@acme.com", role: "admin", avatar_url: "" },
  { id: "3", name: "Bob Smith", email: "bob@acme.com", role: "member", avatar_url: "" },
  { id: "4", name: "Diana Park", email: "diana@acme.com", role: "viewer", avatar_url: "" },
];

/* ── Mock Billing ── */

export const mockBilling: BillingPlan = {
  name: "team",
  price_per_seat: 22,
  seats_used: 4,
  seats_total: 10,
  current_period_end: "2026-04-25T00:00:00Z",
};
