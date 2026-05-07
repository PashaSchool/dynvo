"""Sprint 18 — Few-shot examples for ``deep_scan`` system prompt.

STATUS (2026-05-07): S18 FAILED ITS ACCEPTANCE GATE.
==============================================================

A/B on 3 repos (apprise, rallly, inbox-zero) showed:
  - avg F1 delta: -9.9pp (gate required ≥ +3pp)
  - worst single regression: inbox-zero -28pp (gate required > -10pp)

Root cause: stack-tag → example mismatch contaminates decomposition.
inbox-zero (next-app-router, ~14 features) gets documenso example
(next-monorepo, 5 features in expected_output) → model copies
documenso's domain shape ('signed-in', 'teams') instead of inferring
inbox-zero's actual features (ai-rules, bulk-unsubscriber).

Sonnet 4.6 is strong enough that biased examples HURT more than the
shape-of-output signal helps. The intro fix (telling model to ignore
example count) recovers apprise but does NOT save inbox-zero where
the example DOMAIN bleeds into the output names.

Module is kept as scaffold:
  - --few-shot CLI flag remains, default OFF
  - S19 may reuse the registry for per-stack prompts that change
    INSTRUCTIONS (not just add examples)
  - Future S20+ may revisit if we can isolate the wiring (separate
    `<example>` markers, smaller examples, etc.)

DO NOT enable --few-shot in production scans. It will silently
degrade accuracy on next-app-router and next-monorepo cohorts.
==============================================================



Modern LLMs (especially Sonnet 4.6) reliably absorb output-shape and
domain-vocabulary patterns from in-context examples. Showing the model
"here's how a Next.js monorepo gets broken into features" raises
accuracy more than refining instructions does.

Public surface
==============

    EXAMPLES_BY_STACK : dict[str, list[FewShotExample]]
        Stack tag → list of curated examples. Stack tags match
        ``ground_truth.json`` ``stack`` field.

    pick_examples(stack_hint, max_count=3, max_tokens=4000) -> str
        Render selected examples as a single ``<example>...</example>``
        block to splice into the system prompt. Picks examples whose
        ``stack`` matches first, then fills with ``mixed`` examples.

    selection diagnostics: returned with chosen example names so the
        scan log records which few-shots were used.

Curation rules (S18 Day 1, when filling these in)
==================================================

  1. Source: ground-truth maps from ``tests/eval/ground_truth.json``
     where the eval F1 is in the top quartile for that stack.
  2. Diversity: at most one example per repo, 5+ stacks represented.
  3. Trim: ``file_paths_sample`` capped at 12 paths per example so
     total prompt budget stays under +6K tokens (S18 acceptance gate).
  4. Output shape: each example shows the JSON shape Sonnet should
     return (merge/rename/remove/split/features keys).
  5. No leakage: never include an example whose repo is in the eval
     corpus AND will be re-scored during S18 A/B comparison —
     contamination inflates measured uplift.

Format
======

Each example is a ``FewShotExample`` dataclass with:
  - ``stack``: tag matching ground_truth.json (e.g. "next-monorepo")
  - ``repo``: source repo name (for traceability / dedup)
  - ``file_paths_sample``: 8-12 representative paths
  - ``expected_output``: JSON envelope showing the desired shape
  - ``rationale``: 1-2 lines on why this is a good teaching example

The rendered block looks like::

    <example>
    <repo>documenso</repo>
    <stack>next-monorepo</stack>
    <input_files>
    apps/web/src/app/(signed-in)/documents/...
    apps/web/src/app/(signed-in)/templates/...
    packages/lib/server-only/auth/...
    </input_files>
    <expected_output>
    {"merge": [...], "features": [...]}
    </expected_output>
    </example>

Multiple examples are joined with double newlines.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Data shape ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FewShotExample:
    """One curated input/output pair for in-context learning."""

    stack: str  # matches ground_truth.json stack tag
    repo: str
    file_paths_sample: list[str]
    expected_output: dict
    rationale: str = ""

    def render(self) -> str:
        """Return the example formatted for the system prompt."""
        paths = "\n".join(self.file_paths_sample)
        out_json = json.dumps(self.expected_output, indent=2, ensure_ascii=False)
        return (
            f"<example>\n"
            f"<repo>{self.repo}</repo>\n"
            f"<stack>{self.stack}</stack>\n"
            f"<input_files>\n{paths}\n</input_files>\n"
            f"<expected_output>\n{out_json}\n</expected_output>\n"
            f"</example>"
        )


# ── Curated examples ──────────────────────────────────────────────────
#
# Sprint 18 Day 1 task: replace these placeholders with real examples
# pulled from ground_truth.json. Each example below is a STRUCTURAL
# template — shape is correct, content is illustrative.
#
# When filling in:
#   1. Pick repo with eval F1 ≥ 70% in its stack class (run S17 eval first)
#   2. Pull ~12 representative file paths from its scan result
#   3. Pull the consolidated feature list from ground_truth.json
#   4. Verify rendered token count: each example ~800-1200 tokens
#   5. Run unit test ``test_pick_examples_under_budget``


_NEXT_MONOREPO_EXAMPLES: list[FewShotExample] = [
    FewShotExample(
        stack="next-monorepo",
        repo="documenso",
        file_paths_sample=[
            "apps/web/src/app/(signed-in)/documents/page.tsx",
            "apps/web/src/app/(signed-in)/documents/[id]/edit/page.tsx",
            "apps/web/src/app/(signed-in)/templates/page.tsx",
            "apps/web/src/app/(signed-in)/teams/[teamId]/settings/page.tsx",
            "packages/lib/server-only/auth/oauth.ts",
            "packages/lib/server-only/document/sign-document.ts",
            "packages/lib/server-only/recipient/get-recipient-by-token.ts",
            "packages/lib/server-only/template/duplicate-template.ts",
            "packages/lib/server-only/team/create-team.ts",
            "packages/lib/server-only/billing/stripe-webhook.ts",
            "packages/email/templates/document-pending.tsx",
            "packages/api/v1/openapi.ts",
        ],
        expected_output={
            "merge": [],
            "rename": [],
            "remove": [],
            "split": [],
            "features": [
                {
                    "name": "documents",
                    "description": "Document upload, signing workflow, recipient management, and audit trail.",
                    "flows": [
                        {"name": "upload-document-flow", "description": "User uploads a PDF to start a signing workflow."},
                        {"name": "send-for-signing-flow", "description": "Add recipients and send document for signature."},
                        {"name": "sign-document-flow", "description": "Recipient receives email and signs via tokenized link."},
                    ],
                },
                {
                    "name": "templates",
                    "description": "Reusable document templates with placeholder fields.",
                    "flows": [
                        {"name": "create-template-flow", "description": "Build a template with field placeholders."},
                        {"name": "duplicate-template-flow", "description": "Clone existing template as starting point."},
                    ],
                },
                {
                    "name": "teams",
                    "description": "Multi-user workspaces with shared documents.",
                    "flows": [
                        {"name": "create-team-flow", "description": "Owner creates a team workspace."},
                        {"name": "invite-team-member-flow", "description": "Send invitation email to teammate."},
                    ],
                },
                {
                    "name": "authentication",
                    "description": "Login, signup, OAuth via Google/GitHub.",
                    "flows": [
                        {"name": "sign-in-flow", "description": "Email/password or OAuth sign-in."},
                        {"name": "sign-up-flow", "description": "Account creation with email verification."},
                    ],
                },
                {
                    "name": "billing",
                    "description": "Stripe subscriptions and invoicing.",
                    "flows": [
                        {"name": "subscribe-flow", "description": "Convert free user to paid subscription."},
                        {"name": "process-stripe-webhook-flow", "description": "Handle Stripe events for subscription state."},
                    ],
                },
            ],
        },
        rationale=(
            "Clean turborepo split (apps/* + packages/*). Demonstrates: "
            "(a) folder = feature mapping, (b) shared packages don't get "
            "their own feature when they back a domain feature, "
            "(c) auth/billing are separate features even when small."
        ),
    ),
    # TODO S18 Day 1: add 1 more next-monorepo example (dub or trigger.dev
    #                 once S17 eval shows F1 ≥ 70%)
]


_VUE_SPA_EXAMPLES: list[FewShotExample] = [
    # CRITICAL stack: addresses the uptime-kuma 67% regression.
    # Vue SPAs have flat src/components/ — features must be inferred
    # from filename suffix patterns (TagsManager, BadgeDialog), not
    # directory structure.
    FewShotExample(
        stack="vue-spa",
        repo="uptime-kuma",
        file_paths_sample=[
            "src/components/TagsManager.vue",
            "src/components/Tag.vue",
            "src/components/TagEditDialog.vue",
            "src/components/BadgeLinkGeneratorDialog.vue",
            "src/components/StatusPage.vue",
            "src/components/MaintenanceList.vue",
            "src/components/CertificateInfo.vue",
            "src/pages/Manage2FA.vue",
            "src/pages/ManageAPIKey.vue",
            "server/prometheus.js",
            "server/notification.js",
            "server/monitor-types/http.js",
        ],
        expected_output={
            "merge": [],
            "rename": [],
            "remove": [],
            "split": [],
            "features": [
                {
                    "name": "monitors",
                    "description": "Monitor types (HTTP, TCP, ping, DNS, push) with retry and uptime tracking.",
                    "flows": [
                        {"name": "create-monitor-flow", "description": "Define a new monitor with type, target, interval."},
                        {"name": "view-monitor-history-flow", "description": "View uptime/response chart for a monitor."},
                    ],
                },
                {
                    "name": "tags",
                    "description": "Group monitors with tags for filtering and organization.",
                    "flows": [
                        {"name": "manage-tags-flow", "description": "Create/rename/delete tags for grouping."},
                        {"name": "tag-monitor-flow", "description": "Apply tags to a monitor."},
                    ],
                },
                {
                    "name": "badges",
                    "description": "Embeddable status badges (SVG/PNG) for external sites.",
                    "flows": [
                        {"name": "generate-badge-flow", "description": "Configure badge style and copy embed link."},
                    ],
                },
                {
                    "name": "certificate-monitoring",
                    "description": "TLS/SSL certificate expiry tracking and alerts.",
                    "flows": [
                        {"name": "view-certificate-info-flow", "description": "Inspect certificate expiry and chain."},
                    ],
                },
                {
                    "name": "prometheus-metrics",
                    "description": "Prometheus exporter endpoint for monitor metrics.",
                    "flows": [
                        {"name": "scrape-metrics-flow", "description": "External Prometheus scrapes /metrics."},
                    ],
                },
            ],
        },
        rationale=(
            "Vue SPA stress test. Files in flat src/components/ with NO "
            "feature folders. Model must learn: filename prefix patterns "
            "(Tag*, Badge*, Certificate*) ARE the features, even when only "
            "1-2 files exist. Don't fold them into bigger neighbors."
        ),
    ),
]


_PYTHON_FLAT_EXAMPLES: list[FewShotExample] = [
    # apprise — F1 73.7%, prec 100%. Flat Python notification library.
    # Teaching value: filename suffix patterns matter more than directory.
    FewShotExample(
        stack="python-flat",
        repo="apprise",
        file_paths_sample=[
            "apprise/apprise.py",
            "apprise/cli.py",
            "apprise/apprise_config.py",
            "apprise/plugins/email/base.py",
            "apprise/plugins/google_chat.py",
            "apprise/plugins/rocketchat.py",
            "apprise/plugins/discord.py",
            "apprise/plugins/octopush.py",
            "apprise/plugins/clickatell.py",
            "apprise/plugins/webhook.py",
            "apprise/manager_config.py",
            "apprise/attachment.py",
        ],
        expected_output={
            "merge": [],
            "rename": [],
            "remove": [],
            "split": [],
            "features": [
                {"name": "chat-notifications", "description": "Notifications via Discord, Slack, Google Chat, Rocket.Chat, etc.", "flows": [
                    {"name": "send-to-discord-flow", "description": "Send a notification to a Discord webhook."},
                    {"name": "send-to-slack-flow", "description": "Send a notification via Slack incoming webhook."},
                ]},
                {"name": "sms-notifications", "description": "SMS via providers like Octopush, Clickatell, Twilio.", "flows": [
                    {"name": "send-sms-flow", "description": "Send an SMS via configured provider."},
                ]},
                {"name": "email-notifications", "description": "Email via SMTP including attachments and HTML.", "flows": [
                    {"name": "configure-smtp-flow", "description": "Set up SMTP connection details."},
                ]},
                {"name": "webhook-notifications", "description": "Generic JSON/form-encoded webhook posts.", "flows": [
                    {"name": "send-via-webhook-flow", "description": "POST notification payload to a custom URL."},
                ]},
                {"name": "configuration-files", "description": "YAML/text config to define notification destinations.", "flows": [
                    {"name": "load-config-file-flow", "description": "Parse config file and create notification objects."},
                ]},
                {"name": "cli", "description": "Command-line interface for sending notifications.", "flows": [
                    {"name": "send-via-cli-flow", "description": "Invoke `apprise` to dispatch to all configured targets."},
                ]},
            ],
        },
        rationale=(
            "Flat Python lib. Teaches: per-plugin files (discord.py, "
            "rocketchat.py, octopush.py) collapse into category features "
            "by transport (chat, sms, email, webhook), not 1-feature-per-"
            "plugin. Configuration/CLI are separate cross-cutting features."
        ),
    ),
]


_GO_MODULAR_EXAMPLES: list[FewShotExample] = [
    # ollama — F1 90%, prec 100%. Go LLM runtime, clean module layout.
    # Teaching value: Go directory = bounded domain, names are precise.
    FewShotExample(
        stack="go-modular",
        repo="ollama",
        file_paths_sample=[
            "server/routes.go",
            "server/download.go",
            "server/cloud_proxy.go",
            "kvcache/cache.go",
            "kvcache/causal.go",
            "model/model.go",
            "cmd/cmd.go",
            "api/client.go",
            "openai/openai.go",
            "discover/gpu.go",
            "envconfig/config.go",
            "auth/auth.go",
        ],
        expected_output={
            "merge": [],
            "rename": [],
            "remove": [],
            "split": [],
            "features": [
                {"name": "model-runtime", "description": "Inference runtime, KV cache, GPU dispatch.", "flows": [
                    {"name": "run-model-locally-flow", "description": "Load model weights and serve inference."},
                ]},
                {"name": "model-management", "description": "Pull, list, push, delete model artifacts.", "flows": [
                    {"name": "pull-model-flow", "description": "Download model from registry."},
                    {"name": "list-installed-models-flow", "description": "Show locally available models."},
                ]},
                {"name": "rest-api", "description": "HTTP API for chat / generate / embeddings.", "flows": [
                    {"name": "chat-with-model-flow", "description": "Send chat-format request, stream response."},
                    {"name": "generate-embeddings-flow", "description": "Compute vector embeddings from text."},
                ]},
                {"name": "cli", "description": "Command-line interface (`ollama run`, `ollama pull`).", "flows": [
                    {"name": "run-cli-command-flow", "description": "Dispatch CLI subcommand to API client."},
                ]},
                {"name": "modelfile", "description": "Modelfile DSL for custom model variants.", "flows": [
                    {"name": "create-custom-modelfile-flow", "description": "Author + build a custom modelfile."},
                ]},
                {"name": "openai-compat", "description": "OpenAI-compatible API endpoints.", "flows": [
                    {"name": "openai-chat-completion-flow", "description": "OpenAI-format chat completion."},
                ]},
                {"name": "gpu-acceleration", "description": "Detect and target NVIDIA/AMD/Metal GPUs.", "flows": [
                    {"name": "discover-gpu-flow", "description": "Probe system for accelerators."},
                ]},
            ],
        },
        rationale=(
            "Go modular layout. Teaches: top-level dirs (server/, model/, "
            "kvcache/, api/, cmd/, auth/) ARE features by Go convention. "
            "Don't merge them — keep granular. Cross-cutting concerns "
            "(openai, discover) get their own feature."
        ),
    ),
]


_RUST_MODULAR_EXAMPLES: list[FewShotExample] = [
    # meilisearch — F1 89%. Cargo workspace, search engine.
    FewShotExample(
        stack="rust-modular",
        repo="meilisearch",
        file_paths_sample=[
            "crates/meilisearch/src/routes/indexes/search.rs",
            "crates/meilisearch/src/routes/indexes/documents.rs",
            "crates/meilisearch/src/routes/indexes/settings/synonyms.rs",
            "crates/milli/src/search/hybrid.rs",
            "crates/milli/src/search/facet/mod.rs",
            "crates/milli/src/documents/builder.rs",
            "crates/index-scheduler/src/scheduler/autobatcher.rs",
            "crates/meilisearch/src/routes/api_key.rs",
            "crates/meilisearch-auth/src/lib.rs",
            "crates/milli/src/search/typo_tolerance.rs",
            "crates/milli/src/search/sort.rs",
            "crates/milli/src/search/geo.rs",
        ],
        expected_output={
            "merge": [],
            "rename": [],
            "remove": [],
            "split": [],
            "features": [
                {"name": "search", "description": "Core search query execution including hybrid/vector.", "flows": [
                    {"name": "search-with-filters-flow", "description": "Run a query against an index with filters applied."},
                    {"name": "perform-vector-search-flow", "description": "Hybrid keyword + vector search."},
                ]},
                {"name": "indexing", "description": "Document add/update/delete pipeline.", "flows": [
                    {"name": "create-index-flow", "description": "Provision a new index."},
                    {"name": "add-documents-flow", "description": "Bulk-ingest documents."},
                ]},
                {"name": "typo-tolerance", "description": "Fuzzy match settings + edit distance.", "flows": [
                    {"name": "configure-typo-flow", "description": "Tune typo thresholds per field."},
                ]},
                {"name": "filtering", "description": "Field-level filter expressions.", "flows": [
                    {"name": "filter-results-flow", "description": "Apply boolean filters to a search."},
                ]},
                {"name": "faceted-search", "description": "Faceted aggregations + filter UIs.", "flows": [
                    {"name": "compute-facets-flow", "description": "Return facet counts for a query."},
                ]},
                {"name": "synonyms", "description": "Per-index synonym groups.", "flows": [
                    {"name": "configure-synonyms-flow", "description": "Define synonym groups for a field."},
                ]},
                {"name": "api-keys", "description": "Tenant tokens + scoped key management.", "flows": [
                    {"name": "issue-api-key-flow", "description": "Mint a scoped API key."},
                ]},
            ],
        },
        rationale=(
            "Cargo workspace. Teaches: route files in routes/indexes/* "
            "indicate features (search, documents, settings/synonyms). "
            "Search algorithms in milli/src/search/* are sub-features of "
            "the search feature, not separate features."
        ),
    ),
]


_RAILS_APP_EXAMPLES: list[FewShotExample] = [
    # maybe — F1 75%. Personal-finance Rails app, MVC convention.
    FewShotExample(
        stack="rails-app",
        repo="maybe",
        file_paths_sample=[
            "app/controllers/accounts_controller.rb",
            "app/models/account.rb",
            "app/controllers/transactions_controller.rb",
            "app/controllers/transactions/bulk_deletions_controller.rb",
            "app/controllers/budgets_controller.rb",
            "app/controllers/holdings_controller.rb",
            "app/controllers/import/configurations_controller.rb",
            "app/controllers/import/cleans_controller.rb",
            "app/controllers/categories_controller.rb",
            "app/controllers/rules_controller.rb",
            "app/controllers/chats_controller.rb",
            "app/views/holdings/_cash.html.erb",
        ],
        expected_output={
            "merge": [],
            "rename": [],
            "remove": [],
            "split": [],
            "features": [
                {"name": "accounts", "description": "Bank/brokerage/loan account ledger.", "flows": [
                    {"name": "add-account-flow", "description": "User connects or manually creates an account."},
                ]},
                {"name": "transactions", "description": "Transaction CRUD, bulk operations, splits.", "flows": [
                    {"name": "categorize-transaction-flow", "description": "Assign category to a transaction."},
                    {"name": "bulk-delete-flow", "description": "Mass-delete transactions."},
                ]},
                {"name": "budgets", "description": "Per-category monthly budget tracking.", "flows": [
                    {"name": "create-budget-flow", "description": "Define a budget for a category and period."},
                ]},
                {"name": "investments", "description": "Holdings, positions, performance.", "flows": [
                    {"name": "track-investment-flow", "description": "Record a holding and value over time."},
                ]},
                {"name": "imports", "description": "CSV/OFX import with cleaning + mapping.", "flows": [
                    {"name": "import-transactions-flow", "description": "Upload + clean + map import file."},
                ]},
                {"name": "categories", "description": "Hierarchical category taxonomy.", "flows": [
                    {"name": "manage-categories-flow", "description": "Create/edit/delete category."},
                ]},
                {"name": "rules", "description": "Auto-categorization rules.", "flows": [
                    {"name": "create-rule-flow", "description": "Define an if/then auto-categorization rule."},
                ]},
                {"name": "ai-assistant", "description": "Conversational AI chat about finances.", "flows": [
                    {"name": "ask-ai-question-flow", "description": "Send chat message to AI assistant."},
                ]},
            ],
        },
        rationale=(
            "Rails app with strict MVC convention. Teaches: each "
            "app/controllers/<resource>_controller.rb = one feature. "
            "Sub-controllers in subdirs (transactions/bulk_deletions/, "
            "import/cleans/) are flows within the parent feature."
        ),
    ),
]


_PYTHON_LIBRARY_EXAMPLES: list[FewShotExample] = [
    # fastapi — F1 100% (library mode). Reference example for Python libs.
    FewShotExample(
        stack="python-library",
        repo="fastapi",
        file_paths_sample=[
            "fastapi/applications.py",
            "fastapi/routing.py",
            "fastapi/dependencies/utils.py",
            "fastapi/security/oauth2.py",
            "fastapi/security/api_key.py",
            "fastapi/openapi/utils.py",
            "fastapi/openapi/docs.py",
            "fastapi/websockets.py",
            "fastapi/background.py",
            "fastapi/exceptions.py",
            "fastapi/responses.py",
            "fastapi/encoders.py",
        ],
        expected_output={
            "merge": [],
            "rename": [],
            "remove": [],
            "split": [],
            "features": [
                {"name": "routing", "description": "Path operations, route registration, decorators.", "flows": [
                    {"name": "register-route-flow", "description": "Decorate a function as a path operation."},
                ]},
                {"name": "validation", "description": "Pydantic request body / query / path validation.", "flows": [
                    {"name": "validate-request-body-flow", "description": "Parse and validate JSON body."},
                ]},
                {"name": "dependency-injection", "description": "Depends() utility for shared resources.", "flows": [
                    {"name": "inject-dependency-flow", "description": "Resolve a dependency tree per request."},
                ]},
                {"name": "security-and-authentication", "description": "OAuth2, API key, HTTP basic, OpenID.", "flows": [
                    {"name": "authenticate-with-bearer-token-flow", "description": "Verify JWT bearer token."},
                ]},
                {"name": "automatic-docs", "description": "OpenAPI / Swagger UI / ReDoc auto-generation.", "flows": [
                    {"name": "generate-openapi-spec-flow", "description": "Render OpenAPI JSON from routes."},
                ]},
                {"name": "websocket-support", "description": "Native WebSocket endpoints.", "flows": [
                    {"name": "handle-websocket-message-flow", "description": "Accept connection and exchange messages."},
                ]},
                {"name": "background-tasks", "description": "Defer work after response is returned.", "flows": [
                    {"name": "schedule-background-task-flow", "description": "Queue a task to run after response."},
                ]},
                {"name": "exceptions", "description": "HTTPException + custom exception handlers.", "flows": [
                    {"name": "raise-http-exception-flow", "description": "Raise typed HTTP error from handler."},
                ]},
            ],
        },
        rationale=(
            "Python library reference. Teaches: top-level modules in "
            "fastapi/* map directly to library 'public modules' (not "
            "business features). Submodules like security/ split by "
            "auth scheme into sub-features. No 'business' framing."
        ),
    ),
]


_MIXED_FALLBACK_EXAMPLES: list[FewShotExample] = [
    # Use documenso as neutral fallback for mixed/unknown stacks.
    # Already shown via _NEXT_MONOREPO_EXAMPLES; reference it directly.
]
_MIXED_FALLBACK_EXAMPLES.extend(_NEXT_MONOREPO_EXAMPLES[:1])


EXAMPLES_BY_STACK: dict[str, list[FewShotExample]] = {
    "next-monorepo": _NEXT_MONOREPO_EXAMPLES,
    "next-app-router": _NEXT_MONOREPO_EXAMPLES,  # share — pattern is similar
    "vue-spa": _VUE_SPA_EXAMPLES,
    "vue-nuxt-monorepo": _VUE_SPA_EXAMPLES,
    "python-flat": _PYTHON_FLAT_EXAMPLES,
    "python-modules": _PYTHON_FLAT_EXAMPLES,
    "python-library": _PYTHON_LIBRARY_EXAMPLES,
    "go-modular": _GO_MODULAR_EXAMPLES,
    "rust-modular": _RUST_MODULAR_EXAMPLES,
    "rails-app": _RAILS_APP_EXAMPLES,
    "node-monorepo": _NEXT_MONOREPO_EXAMPLES,
    "js-library": _PYTHON_LIBRARY_EXAMPLES,  # closest neutral fit (axios)
    "mixed": _MIXED_FALLBACK_EXAMPLES,
}


# ── Renderer / picker ─────────────────────────────────────────────────


# Conservative bound: 4 examples × ~1000 tokens each = 4K, leaving room
# for instruction prompt + per-call user message. Empirical: Sonnet 4.6
# has 200K context, but accuracy degrades past ~50K of system prompt.
_DEFAULT_MAX_EXAMPLES = 3
_DEFAULT_MAX_TOKENS = 4_000

# 1 token ≈ 3.5 chars for English+code mix; safe upper bound for budget.
_CHARS_PER_TOKEN_APPROX = 3.5


def _estimate_tokens(text: str) -> int:
    return int(len(text) / _CHARS_PER_TOKEN_APPROX) + 1


def pick_examples(
    stack_hint: str | None,
    *,
    max_count: int = _DEFAULT_MAX_EXAMPLES,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> tuple[str, list[str]]:
    """Render up to ``max_count`` examples for ``stack_hint``, capped by tokens.

    Selection order:
      1. Examples whose ``stack`` matches ``stack_hint``.
      2. Fill remaining slots from ``mixed`` fallback examples.
      3. Stop adding once cumulative token estimate exceeds ``max_tokens``.

    Returns:
      (rendered_block, picked_repo_names) — the second item is for
      logging / debug ("scan used few-shots from: documenso, uptime-kuma").
    """
    primary = list(EXAMPLES_BY_STACK.get(stack_hint or "mixed", []))
    fallback = [
        ex for ex in EXAMPLES_BY_STACK.get("mixed", [])
        if ex not in primary
    ]
    candidates = primary + fallback

    chosen: list[FewShotExample] = []
    used_tokens = 0
    for ex in candidates:
        if len(chosen) >= max_count:
            break
        rendered = ex.render()
        cost = _estimate_tokens(rendered)
        if used_tokens + cost > max_tokens:
            logger.debug(
                "few_shot: skipping %s (would overshoot budget %d > %d)",
                ex.repo, used_tokens + cost, max_tokens,
            )
            continue
        chosen.append(ex)
        used_tokens += cost

    if not chosen:
        return ("", [])

    block = "\n\n".join(ex.render() for ex in chosen)
    repos = [ex.repo for ex in chosen]
    logger.info(
        "few_shot: stack=%s picked=%s tokens=%d",
        stack_hint, repos, used_tokens,
    )
    return (block, repos)


# ── Wiring helper ─────────────────────────────────────────────────────


_FEW_SHOT_INTRO = """\
## Examples

Below are real-world examples showing how to decompose repos of \
different stacks into features. Each shows a sample of input file \
paths and the expected output JSON envelope.

**CRITICAL — copy the STYLE, NOT the COUNT.** Examples show 5-8 \
features for brevity, but your output must follow the size guidance \
in the ## Target block above:
  - Repo-wide mode: 12-25 features (NOT 5-8)
  - Library mode: 5-15 modules
  - Per-package mode: 1 to (file_count // 8) features

Use the examples to learn:
  - Naming style (kebab-case domain names, never code structure)
  - Granularity per feature (one user-recognisable concept)
  - JSON envelope shape (merge / rename / remove / split / features)
  - Flow naming (lowercase, ends with -flow, describes user action)

Do NOT use the examples to set the feature count for the actual repo. \
Always derive count from the repo's actual size and structure.\
"""


def build_examples_block(stack_hint: str | None) -> tuple[str, list[str]]:
    """Build a complete ``## Examples`` section for splicing into the prompt.

    Returns ``("", [])`` when no examples are available for this stack
    (caller should skip the section entirely rather than emit a header
    with nothing under it).
    """
    rendered, repos = pick_examples(stack_hint)
    if not rendered:
        return ("", [])
    return (f"{_FEW_SHOT_INTRO}\n\n{rendered}\n", repos)
