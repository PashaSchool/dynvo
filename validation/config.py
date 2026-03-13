"""Constants and fallback configuration."""

from pathlib import Path

VALIDATION_DIR = Path.home() / ".faultline" / "validation"
REPOS_DIR = VALIDATION_DIR / "repos"
RESULTS_DIR = VALIDATION_DIR / "results"
PROGRESS_FILE = VALIDATION_DIR / "progress.json"

CLONE_DEPTH = 0  # 0 = full clone (shallow breaks git diff)
MAX_COMMITS = 500  # limit analysis to keep it fast
ANALYSIS_TIMEOUT_SEC = 600
VALIDATION_MODEL = "claude-sonnet-4-5-20250514"
RESEARCH_MODEL = "claude-sonnet-4-5-20250514"

FALLBACK_REPOS = [
    # ── Verified expected_features (researched from actual code) ──
    {
        "name": "paperless-ngx/paperless-ngx",
        "url": "https://github.com/paperless-ngx/paperless-ngx.git",
        "expected_features": [
            "documents", "tags", "correspondents", "document-types",
            "consumption", "search", "ocr", "classification",
            "custom-fields", "workflows", "email-ingestion",
            "sharing", "bulk-operations", "saved-views",
            "permissions", "storage-paths", "notes",
            "ai", "barcode-detection", "pdf-manipulation",
            "export", "duplicate-detection", "task-monitoring",
        ],
        "src_filter": "src/",
        "reason": "Document management system (Python/Django) with clear domains",
    },
    {
        "name": "dub-inc/dub",
        "url": "https://github.com/dubinc/dub.git",
        "expected_features": [
            "links", "analytics", "domains", "workspaces",
            "partners", "commissions", "webhooks", "integrations",
            "tags", "folders", "billing", "oauth",
            "saml", "audit-logs", "fraud", "import",
            "customers", "bounties", "campaigns", "referrals",
            "payouts", "qr-codes", "utm", "scim",
            "onboarding", "ai",
        ],
        "src_filter": "apps/web/",
        "reason": "Link management platform with clear SaaS domains",
    },
    {
        "name": "maybe-finance/maybe",
        "url": "https://github.com/maybe-finance/maybe.git",
        "expected_features": [
            "accounts", "transactions", "investments", "budgets",
            "net-worth", "import", "family", "ai-chat",
            "categories", "rules", "plaid-sync", "settings",
            "securities", "analytics", "export", "mfa",
            "merchants", "api",
        ],
        "src_filter": "app/",
        "reason": "Personal finance app with distinct financial domains",
    },
    # ── Unverified expected_features (guessed) ──
    {
        "name": "documenso/documenso",
        "url": "https://github.com/documenso/documenso.git",
        "expected_features": [
            "documents", "signing", "templates",
            "auth", "teams", "api",
        ],
        "src_filter": "apps/remix/app/",
        "reason": "E-signature app with clear document workflow features",
    },
    {
        "name": "formbricks/formbricks",
        "url": "https://github.com/formbricks/formbricks.git",
        "expected_features": [
            "surveys", "responses", "integrations",
            "auth", "teams", "api",
        ],
        "src_filter": "apps/web/app/",
        "reason": "Survey platform with well-separated feature modules",
    },
    {
        "name": "triggerdotdev/trigger.dev",
        "url": "https://github.com/triggerdotdev/trigger.dev.git",
        "expected_features": [
            "jobs", "triggers", "integrations",
            "auth", "organizations", "api",
        ],
        "src_filter": "apps/webapp/app/",
        "reason": "Background jobs platform with distinct workflow features",
    },
    {
        "name": "hoppscotch/hoppscotch",
        "url": "https://github.com/hoppscotch/hoppscotch.git",
        "expected_features": [
            "requests", "collections", "environments",
            "auth", "teams", "history",
        ],
        "src_filter": "packages/hoppscotch-common/src/",
        "reason": "API testing tool with clear request/collection domains",
    },
    {
        "name": "medusajs/medusa",
        "url": "https://github.com/medusajs/medusa.git",
        "expected_features": [
            "products", "orders", "cart",
            "customers", "payments", "shipping",
            "auth", "inventory",
        ],
        "src_filter": "packages/medusa/src/",
        "reason": "E-commerce platform with well-defined commerce domains",
    },
    # ── Medium (clear business domains, good variety) ──
    {
        "name": "calcom/cal.com",
        "url": "https://github.com/calcom/cal.com.git",
        "expected_features": [
            "booking", "availability", "calendar-sync",
            "teams", "webhooks", "payments", "auth",
        ],
        "src_filter": "apps/web/app/",
        "reason": "Scheduling platform with clear business domains",
    },
    {
        "name": "twentyhq/twenty",
        "url": "https://github.com/twentyhq/twenty.git",
        "expected_features": [
            "contacts", "companies", "pipeline",
            "tasks", "settings", "auth",
        ],
        "src_filter": "packages/twenty-front/src/",
        "reason": "Open-source CRM with modular frontend architecture",
    },
    {
        "name": "plausible/analytics",
        "url": "https://github.com/plausible/analytics.git",
        "expected_features": [
            "stats", "sites", "goals",
            "funnels", "auth", "billing",
        ],
        "src_filter": "lib/",
        "reason": "Privacy-focused analytics (Elixir) with clear metric domains",
    },
    {
        "name": "outline/outline",
        "url": "https://github.com/outline/outline.git",
        "expected_features": [
            "documents", "collections", "search",
            "auth", "teams", "comments", "api",
        ],
        "src_filter": "app/",
        "reason": "Wiki/knowledge base with document-centric domains",
    },
    {
        "name": "ghostfolio/ghostfolio",
        "url": "https://github.com/ghostfolio/ghostfolio.git",
        "expected_features": [
            "portfolio", "holdings", "transactions",
            "benchmarks", "auth", "api",
        ],
        "src_filter": "apps/api/src/app/",
        "reason": "Investment tracker with financial portfolio domains",
    },
    {
        "name": "immich-app/immich",
        "url": "https://github.com/immich-app/immich.git",
        "expected_features": [
            "photos", "albums", "search",
            "sharing", "auth", "jobs",
        ],
        "src_filter": "server/src/",
        "reason": "Photo management app with media-centric domains",
    },
    {
        "name": "logto-io/logto",
        "url": "https://github.com/logto-io/logto.git",
        "expected_features": [
            "sign-in", "connectors", "applications",
            "users", "roles", "organizations",
        ],
        "src_filter": "packages/core/src/",
        "reason": "Auth platform — every feature IS an auth concept",
    },
    # ── Larger repos (stress test for dir-collapse) ──
    {
        "name": "appwrite/appwrite",
        "url": "https://github.com/appwrite/appwrite.git",
        "expected_features": [
            "databases", "storage", "functions",
            "auth", "messaging", "teams",
        ],
        "src_filter": "src/Appwrite/",
        "reason": "BaaS platform (PHP) with clear backend service domains",
    },
    {
        "name": "nocodb/nocodb",
        "url": "https://github.com/nocodb/nocodb.git",
        "expected_features": [
            "tables", "views", "fields",
            "formulas", "auth", "api",
        ],
        "src_filter": "packages/nocodb/src/",
        "reason": "Airtable alternative with database/spreadsheet domains",
    },
    {
        "name": "langgenius/dify",
        "url": "https://github.com/langgenius/dify.git",
        "expected_features": [
            "apps", "workflows", "datasets",
            "models", "tools", "auth",
        ],
        "src_filter": "api/",
        "reason": "LLM app platform (Python) with AI workflow domains",
    },
    {
        "name": "supabase/supabase",
        "url": "https://github.com/supabase/supabase.git",
        "expected_features": [
            "database", "auth", "storage",
            "functions", "realtime", "api",
        ],
        "src_filter": "apps/studio/",
        "reason": "Firebase alternative — studio dashboard with clear service domains",
    },
    {
        "name": "mattermost/mattermost",
        "url": "https://github.com/mattermost/mattermost.git",
        "expected_features": [
            "channels", "messaging", "teams",
            "auth", "notifications", "integrations",
        ],
        "src_filter": "webapp/channels/src/",
        "reason": "Team messaging (Go+React) with collaboration domains",
    },
]
