"""Constants and fallback configuration."""

from pathlib import Path

VALIDATION_DIR = Path.home() / ".faultline" / "validation"
REPOS_DIR = VALIDATION_DIR / "repos"
RESULTS_DIR = VALIDATION_DIR / "results"
PROGRESS_FILE = VALIDATION_DIR / "progress.json"

CLONE_DEPTH = 0  # 0 = full clone (shallow breaks git diff)
MAX_COMMITS = 500  # limit analysis to keep it fast
ANALYSIS_TIMEOUT_SEC = 900
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
    # ── Verified expected_features (researched from actual code) ──
    {
        "name": "documenso/documenso",
        "url": "https://github.com/documenso/documenso.git",
        "expected_features": [
            "documents", "signing", "templates", "recipients",
            "auth", "teams", "organizations", "billing",
            "webhooks", "api", "api-tokens", "branding",
            "sso", "audit-logs", "embed", "admin",
            "folders", "document-fields", "direct-links",
            "mfa", "dashboard", "inbox", "email-templates",
        ],
        "src_filter": "apps/remix/app/",
        "reason": "E-signature app with clear document workflow features",
    },
    {
        "name": "formbricks/formbricks",
        "url": "https://github.com/formbricks/formbricks.git",
        "expected_features": [
            "surveys", "survey-editor", "survey-templates",
            "responses", "analysis", "contacts", "segments",
            "integrations", "webhooks", "auth", "organizations",
            "billing", "api-keys", "branding", "tags",
            "languages", "notifications", "sso", "onboarding",
            "workflows", "actions",
        ],
        "src_filter": "apps/web/app/",
        "reason": "Survey platform with well-separated feature modules",
    },
    {
        "name": "triggerdotdev/trigger.dev",
        "url": "https://github.com/triggerdotdev/trigger.dev.git",
        "expected_features": [
            "runs", "schedules", "deployments", "queues",
            "batches", "alerts", "api-keys",
            "environment-variables", "logs", "errors",
            "integrations", "billing", "organizations",
            "projects", "teams", "settings", "concurrency",
            "waitpoints", "dashboards", "mfa", "regions",
        ],
        "src_filter": "apps/webapp/app/",
        "reason": "Background jobs platform with distinct workflow features",
    },
    {
        "name": "hoppscotch/hoppscotch",
        "url": "https://github.com/hoppscotch/hoppscotch.git",
        "expected_features": [
            "requests", "collections", "environments",
            "auth", "teams", "history", "graphql",
            "websocket", "mqtt", "socket-io", "sse",
            "share", "import-export", "settings",
            "workspace", "access-tokens", "organizations",
            "pre-request-scripts", "mock-server", "realtime",
        ],
        "src_filter": "packages/hoppscotch-common/src/",
        "reason": "API testing tool with clear request/collection domains",
    },
    {
        "name": "medusajs/medusa",
        "url": "https://github.com/medusajs/medusa.git",
        "expected_features": [
            "products", "orders", "cart", "customers",
            "payments", "shipping", "auth", "inventory",
            "promotions", "regions", "tax", "fulfillment",
            "returns", "exchanges", "gift-cards", "collections",
            "sales-channels", "notifications", "pricing",
            "customer-groups", "reservations", "draft-orders",
            "claims", "campaigns", "users", "api-keys",
            "invites", "currencies", "stores", "uploads",
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
            "event-types", "routing-forms", "workflows",
            "organizations", "apps-integrations", "analytics",
            "embed", "api", "sso", "round-robin",
            "rescheduling", "billing",
        ],
        "src_filter": "apps/web/app/",
        "reason": "Scheduling platform with clear business domains",
    },
    {
        "name": "twentyhq/twenty",
        "url": "https://github.com/twentyhq/twenty.git",
        "expected_features": [
            "accounts", "activities", "ai", "billing",
            "calendar", "companies", "dashboards", "emails",
            "favorites", "messaging", "notes", "opportunities",
            "people", "tasks", "views", "workflow",
        ],
        "src_filter": "packages/twenty-front/src/",
        "reason": "Open-source CRM with modular frontend architecture",
    },
    # ── New repos (FastAPI, NestJS, Django, Rails, Python) ──
    {
        "name": "polarsource/polar",
        "url": "https://github.com/polarsource/polar.git",
        "expected_features": [
            "products", "subscriptions", "checkout", "customers",
            "payments", "invoices", "transactions", "payouts",
            "discounts", "refunds", "benefits", "license-keys",
            "webhooks", "organizations", "auth", "metrics",
            "files", "campaigns", "custom-fields", "email",
        ],
        "src_filter": "server/polar/",
        "reason": "Developer monetization platform (FastAPI) with clear SaaS domains",
    },
    {
        "name": "ever-co/ever-gauzy",
        "url": "https://github.com/ever-co/ever-gauzy.git",
        "expected_features": [
            "employees", "time-tracking", "invoices", "expenses",
            "payments", "candidates", "organizations", "tasks",
            "projects", "pipelines", "contacts", "auth",
            "integrations", "roles", "email", "import-export",
            "approval", "notifications", "equipment", "goals",
        ],
        "src_filter": "packages/core/src/lib/",
        "reason": "ERP/HRM platform (NestJS) with enterprise HR domains",
    },
    {
        "name": "makeplane/plane",
        "url": "https://github.com/makeplane/plane.git",
        "expected_features": [
            "issues", "projects", "cycles", "modules",
            "pages", "inbox", "workspaces", "analytics",
            "notifications", "integrations", "auth", "labels",
            "views", "estimates", "webhooks", "api",
        ],
        "src_filter": "apps/api/plane/",
        "reason": "Project management (Django+Next.js) with clear PM domains",
    },
    {
        "name": "chatwoot/chatwoot",
        "url": "https://github.com/chatwoot/chatwoot.git",
        "expected_features": [
            "conversations", "contacts", "inboxes", "messages",
            "agents", "teams", "campaigns", "labels",
            "automation", "bots", "webhooks", "auth",
            "notifications", "attachments", "accounts", "reports",
        ],
        "src_filter": "app/",
        "reason": "Customer support platform (Rails) with clear CRM domains",
    },
    {
        "name": "zulip/zulip",
        "url": "https://github.com/zulip/zulip.git",
        "expected_features": [
            "messages", "streams", "topics", "users",
            "auth", "notifications", "bots", "integrations",
            "webhooks", "search", "presence", "reactions",
            "subscriptions", "realms", "settings", "uploads",
            "scheduled-messages", "user-groups",
        ],
        "src_filter": "zerver/",
        "reason": "Team chat (Python/Django) with well-structured messaging domains",
    },
]
