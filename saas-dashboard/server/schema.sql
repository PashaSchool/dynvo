-- FeatureMap SaaS — PostgreSQL schema
-- Better Auth tables + application tables
-- Run: psql $DATABASE_URL < server/schema.sql

-- ═══════════════════════════════════════════════
-- Better Auth tables (auto-created by better-auth,
-- shown here for reference and for adding indexes)
-- ═══════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS "user" (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    image           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS "session" (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    token       TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    ip_address  TEXT,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_session_user_id ON "session"(user_id);
CREATE INDEX idx_session_token ON "session"(token);

CREATE TABLE IF NOT EXISTS "account" (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    account_id          TEXT NOT NULL,
    provider_id         TEXT NOT NULL,  -- "github", "google", "credential"
    access_token        TEXT,
    refresh_token       TEXT,
    access_token_expires_at  TIMESTAMPTZ,
    refresh_token_expires_at TIMESTAMPTZ,
    scope               TEXT,
    id_token            TEXT,
    password            TEXT,  -- hashed, only for email+password
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_account_user_id ON "account"(user_id);
CREATE UNIQUE INDEX idx_account_provider ON "account"(provider_id, account_id);

CREATE TABLE IF NOT EXISTS "verification" (
    id          TEXT PRIMARY KEY,
    identifier  TEXT NOT NULL,  -- email
    value       TEXT NOT NULL,  -- token
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ═══════════════════════════════════════════════
-- Better Auth — Organization plugin tables
-- ═══════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS "organization" (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,  -- used for subdomain: acme.featuremap.dev
    logo        TEXT,
    metadata    JSONB DEFAULT '{}',   -- { plan, seats_total, stripe_customer_id, ... }
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_organization_slug ON "organization"(slug);

CREATE TABLE IF NOT EXISTS "member" (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES "organization"(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'member',  -- owner, admin, member, viewer
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_member_org ON "member"(organization_id);
CREATE INDEX idx_member_user ON "member"(user_id);
CREATE UNIQUE INDEX idx_member_org_user ON "member"(organization_id, user_id);

CREATE TABLE IF NOT EXISTS "invitation" (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES "organization"(id) ON DELETE CASCADE,
    email           TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'member',
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending, accepted, canceled
    inviter_id      TEXT NOT NULL REFERENCES "user"(id),
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_invitation_org ON "invitation"(organization_id);
CREATE INDEX idx_invitation_email ON "invitation"(email);

-- ═══════════════════════════════════════════════
-- FeatureMap application tables
-- All tenant-scoped tables have organization_id FK
-- ═══════════════════════════════════════════════

-- Projects (repos) linked to an organization
CREATE TABLE IF NOT EXISTS "project" (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    organization_id TEXT NOT NULL REFERENCES "organization"(id) ON DELETE CASCADE,
    repo_name       TEXT NOT NULL,       -- "acme/ecommerce-platform"
    repo_url        TEXT NOT NULL,       -- "https://github.com/acme/ecommerce-platform"
    default_branch  TEXT NOT NULL DEFAULT 'main',
    src_path        TEXT NOT NULL DEFAULT '',
    settings        JSONB NOT NULL DEFAULT '{}',
    -- settings: { analysis_days, max_commits, llm_enabled, flows_enabled,
    --             auto_analyze_on_push, auto_analyze_on_pr, schedule_cron }
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_project_org ON "project"(organization_id);
CREATE UNIQUE INDEX idx_project_org_repo ON "project"(organization_id, repo_url);

-- API keys stored per organization (encrypted at rest)
CREATE TABLE IF NOT EXISTS "api_key" (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    organization_id TEXT NOT NULL REFERENCES "organization"(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,  -- "anthropic", "ollama", "posthog", "sentry", etc.
    encrypted_key   TEXT NOT NULL,  -- AES-256-GCM encrypted
    config          JSONB NOT NULL DEFAULT '{}',
    -- config: { model, host, project_id, ... }
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_api_key_org ON "api_key"(organization_id);
CREATE UNIQUE INDEX idx_api_key_org_provider ON "api_key"(organization_id, provider);

-- Analysis runs (scan history)
CREATE TABLE IF NOT EXISTS "analysis_run" (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    project_id      TEXT NOT NULL REFERENCES "project"(id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL REFERENCES "organization"(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending, running, completed, failed
    trigger         TEXT NOT NULL DEFAULT 'manual',   -- manual, push, pr, schedule
    feature_map     JSONB,            -- full FeatureMap JSON on completion
    impact_scores   JSONB,            -- ImpactScore[] from analytics
    commit_sha      TEXT,
    pr_number       INT,
    duration_sec    FLOAT,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX idx_analysis_project ON "analysis_run"(project_id);
CREATE INDEX idx_analysis_org ON "analysis_run"(organization_id);
CREATE INDEX idx_analysis_created ON "analysis_run"(created_at DESC);

-- GitHub App comment configuration per project
CREATE TABLE IF NOT EXISTS "github_comment_config" (
    project_id      TEXT PRIMARY KEY REFERENCES "project"(id) ON DELETE CASCADE,
    is_enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    config          JSONB NOT NULL DEFAULT '{}',
    -- config: { show_health_score, show_flows, show_analytics, comment_style, ... }
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Analytics connections per organization
CREATE TABLE IF NOT EXISTS "analytics_connection" (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::TEXT,
    organization_id TEXT NOT NULL REFERENCES "organization"(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,  -- "posthog", "sentry", "ga4", etc.
    encrypted_key   TEXT NOT NULL,
    config          JSONB NOT NULL DEFAULT '{}',
    -- config: { project_id, host, organization_slug, ... }
    last_synced_at  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_analytics_org ON "analytics_connection"(organization_id);
CREATE UNIQUE INDEX idx_analytics_org_provider ON "analytics_connection"(organization_id, provider);
