# faultlines

> **Map features in any codebase from git history alone. No Jira required.**

Point `faultlines` at a git repo and get back a feature map — which parts of your product accumulate the most bug fixes, who owns what, and where the risk is hiding. Best on product applications and monorepos with real user-facing features; libraries are supported with module-level mapping (see [below](#tested-on-real-oss-repos)).

[![PyPI](https://img.shields.io/pypi/v/faultlines)](https://pypi.org/project/faultlines/)
[![Python 3.11+](https://img.shields.io/pypi/pyversions/faultlines)](https://pypi.org/project/faultlines/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/PashaSchool/featuremap)](https://github.com/PashaSchool/featuremap)

> **Monorepo (uv workspace):** this repository hosts two installable packages — the engine (`faultlines`, this folder) and the MCP server (`faultlines-mcp`, see [`faultlines-mcp/README.md`](faultlines-mcp/README.md)) for AI coding agents. They release independently.

## What's new

- **Pipeline v2 (`scan-v2`)** — new code-grounded detection pipeline. Stages 0–7 in order; only Stages 3 + 4 hit the LLM. Outputs both `developer_features[]` (engineering grain, attributed to files/symbols) and `product_features[]` (product grain, clustered). `analyze` is now backed by the same pipeline; pass `--legacy` for the old single-pass detector.
- **Stack-aware extractors** — Stage 1 deterministic extractors per stack: `route` (Next.js / Remix / SvelteKit / Astro / TanStack / FastAPI / Express / Hono / Fastify / Laravel / Phoenix / Spring), `mvc`, `schema`, `package`, `config`, `go_router` (chi/gin/echo/fiber/httprouter/stdlib), `rust_workspace`, `python_library`, `fastapi`, `_rails`. Patterns live in `eval/stacks/<stack>.yaml`.
- **Hermetic packaging** — runtime stack data (`pipeline_v2/data/{stacks/*.yaml, dependency-anchors.yaml}`) ships **inside the wheel** and is loaded via `importlib.resources`. Same wheel behaves identically in `pip install`, Docker, or a fresh venv — no sibling `eval/` required. Replaces the old `Path(__file__).parents[N]/eval/...` filesystem walks.
- **`scan-v2` model resolution fix** — `--model sonnet` / `claude-sonnet-4-6` now correctly resolves to the live `claude-sonnet-4-6` model id (the previously-pinned dated snapshot 404'd).
- **MCP server extracted** — the AI-agent surface is now its own package, [`faultlines-mcp`](faultlines-mcp/README.md). The engine no longer depends on the MCP SDK; install MCP separately when you need it.
- **Bipartite feature ↔ flow store** — output carries top-level `flows[]` + `feature_flow_edges[]` (primary / secondary) alongside the containment view. Cross-cutting flows are first-class.
- **Stage 0.5 Stack Auditor** — one Haiku call refines stack detection between heuristic intake (Stage 0) and deterministic extractors (Stage 1).
- **Two-grain accuracy on a 25-repo benchmark** — average product-feature recall ~85% and developer-feature recall ~92% on the open golden corpus (`eval/golden/`, `eval/developer-golden/`).

## Try it in 30 seconds

```bash
pip install faultlines
ANTHROPIC_API_KEY=sk-ant-... faultlines analyze . --llm --flows
```

That's it. You'll see something like:

```
╭──────────────────────────── FeatureMap Analysis ─────────────────────────────╮
│ Repository: /home/you/your-project                                           │
│ Features found: 22     Bug fix commits: 125     Health: 60.2/100             │
╰──────────────────────────────────────────────────────────────────────────────╯

                                Features by Risk
╭─────┬────────────────────────┬────────┬─────────┬───────────┬────────────────╮
│     │ Feature                │ Health │ Commits │ Bug Fixes │ Bug %          │
├─────┼────────────────────────┼────────┼─────────┼───────────┼────────────────┤
│ ✗   │ payments               │   23   │     112 │        38 │ 33.9%          │
│ ✗   │ booking-engine         │   29   │     487 │       151 │ 31.0%          │
│ !   │ auth                   │   54   │      48 │        12 │ 25.0%          │
│ ✓   │ dashboard              │   83   │      67 │         2 │  3.0%          │
╰─────┴────────────────────────┴────────┴─────────┴───────────┴────────────────╯
```

## What it does

1. **Reads your git history** — commits, blame, file changes over the last year
2. **Clusters files into features** — using Claude Sonnet (or local Ollama) to group by business domain, not folder names
3. **Scores each feature** — by bug-fix density, churn, bus factor, and age-weighted trends
4. **Detects user flows** — with `--flows`, breaks features into end-to-end user journeys (`checkout-flow`, `signup-flow`, `delete-user-flow`)
5. **Attributes symbols to flows** — with `--symbols`, maps which functions, components, and state hooks belong to each flow, with exact line ranges
6. **Outputs JSON** — `~/.faultlines/feature-map-*.json`, ready for dashboards, MCP servers, or CI

### Without LLM vs with LLM

```
Without --llm:  "components", "views", "hooks"           ← technical folders
With --llm:     "payments", "booking-engine", "auth"     ← business features
```

The LLM reads your file tree and commit messages to produce names an engineering manager would actually use.

## Tested on real OSS repos

Every number below is from a real `faultlines analyze --llm --flows` run. Reproduce any of them yourself.

| Repo | Files | Features | Flows | Time | What it found |
|------|------:|:--------:|:-----:|:----:|---------------|
| [cal.com](https://github.com/calcom/cal.com) | 10,463 | 282 | 725 | 23m | trpc/viewer, web/bookings, ee/billing, web/settings |
| [plane](https://github.com/makeplane/plane) | 4,932 | 134 | 408 | 12m | web/issues, editor/extensions, web/workspace, web/pages |
| [Ghost](https://github.com/TryGhost/Ghost) | 6,898 | 101 | 281 | 14m | admin-x, ghost/members, ghost/email, stats |
| [outline](https://github.com/outline/outline) | 2,390 | 22 | 188 | 6m | rich-text-editor, api-backend, dashboard, plugins |
| [documenso](https://github.com/documenso/documenso) | 2,530 | 49 | 191 | 8m | trpc/envelope, remix/document-signing, ee/billing |
| [formbricks](https://github.com/formbricks/formbricks) | 3,316 | 33 | 136 | 8m | web/survey, web/organization, web/auth |
| [excalidraw](https://github.com/excalidraw/excalidraw) | 1,225 | 15 | 28 | 4m | excalidraw/shared-ui, excalidraw/data, renderer |
| [trpc](https://github.com/trpc/trpc) | 1,573 | 14 | 37 | 1m | server/core, client/links, openapi, next-adapter |
| [gin](https://github.com/gin-gonic/gin) | 130 | 22 | — | 15s | binding, render, context, recovery, logger |
| [fastapi](https://github.com/fastapi/fastapi) | 2,981 | 14 | — | 80s | routing, dependencies, security, openapi, middleware |

Libraries (gin, fastapi) are auto-detected and show modules instead of business features. Flows are suppressed for libraries since they don't have end-user journeys.

## Installation

```bash
pip install faultlines
```

Requires Python 3.11+.

### With Ollama (local, free, private)

```bash
pip install 'faultlines[ollama]'
```

## Usage

### Basic (heuristic, no API key needed)

```bash
faultlines analyze .
faultlines analyze ./path/to/repo
faultlines analyze . --days 90 --top 5
```

### AI-powered (recommended)

```bash
# Claude (cloud) — best quality
ANTHROPIC_API_KEY=sk-ant-... faultlines analyze . --llm

# With flow detection
ANTHROPIC_API_KEY=sk-ant-... faultlines analyze . --llm --flows

# With symbol-level attribution (functions → flows)
ANTHROPIC_API_KEY=sk-ant-... faultlines analyze . --llm --flows --symbols

# Focus on source directory
faultlines analyze . --llm --flows --src src/

# Ollama (local, free)
ollama pull llama3.1:8b
faultlines analyze . --llm --provider ollama
```

### Monorepo support

Automatically detects and analyses workspace packages:

- pnpm (`pnpm-workspace.yaml`)
- npm/yarn (`package.json` workspaces)
- Turborepo, Nx, Lerna
- Cargo workspaces, Go workspaces

Large monorepos are scanned per-package in parallel (4 workers).

## Flow detection

With `--flows`, each feature is broken into user-facing flows — named action sequences like `checkout-flow` or `manage-team-flow`.

```
╭──────┬───────────────────────────┬────────┬─────────┬───────────╮
│      │ Feature / Flow            │ Health │ Commits │ Bug Fixes │
├──────┼───────────────────────────┼────────┼─────────┼───────────┤
│  ✗   │ web/settings              │   23   │     206 │       144 │
│      │   ├─ manage-org-roles     │   11   │      37 │        30 │
│      │   ├─ manage-oauth-clients │   15   │      13 │        10 │
│      │   ├─ delete-org           │   22   │       9 │         7 │
│      │   └─ manage-billing       │   50   │      24 │        12 │
│  ✓   │ auth/server               │   87   │      18 │         6 │
│      │   └─ login-flow           │   87   │      18 │         6 │
╰──────┴───────────────────────────┴────────┴─────────┴───────────╯
```

### CRUD coverage

Every CRUD operation gets its own flow when the code supports it. The LLM won't collapse `delete-user` into a generic `manage-users` — create, list, view, edit, and delete are tracked separately. A deterministic fallback catches missing CRUD flows (e.g. when a `DELETE /api/users/:id` route exists but the LLM missed it).

## Symbol-level attribution

With `--symbols`, each flow is broken down to the individual functions, components, and state hooks that participate in it. Every symbol gets a **role** and **line range**, so you know exactly where in the codebase each flow lives.

```
checkout-flow:
  Entry points     POST handler           src/api/orders/route.ts        L12-L45
  Handlers         handleCheckout         src/checkout/handler.ts        L8-L52
  Validators       validateCartItems      src/checkout/validation.ts     L3-L28
  Data fetching    useCreateOrderMutation src/hooks/useOrders.ts         L44-L67
  State            useCartStore           src/state/cart.ts              L1-L35
  Loading state    isSubmitting           src/checkout/CheckoutForm.tsx   L18-L18
  Error state      checkoutError          src/checkout/CheckoutForm.tsx   L19-L19
  UI components    CheckoutForm           src/checkout/CheckoutForm.tsx   L22-L89
                   OrderSummary           src/checkout/OrderSummary.tsx   L5-L41
```

Roles are classified deterministically from symbol names and file types — no extra LLM call. Types and interfaces are attributed at the feature level (shared across all flows).

Line ranges link directly to GitHub: `{repo_url}/blob/HEAD/{file}#L12-L45`.

## Incremental refresh

After a full scan, keep the feature map up to date without re-running the LLM:

```bash
# Check if your scan is stale
faultlines refresh . --check

# Update the map to match current HEAD (no LLM cost)
faultlines refresh .

# Also re-attribute symbols for changed files
faultlines refresh . --refresh-symbols

# Detect new features in files not yet in any feature
faultlines refresh . --detect-new
```

Refresh uses per-file content hashing — only changed files are re-analysed. Flows and symbol attributions on untouched features are preserved.

## Watch mode

Auto-refresh the feature map whenever files change:

```bash
# Start watcher daemon in background
faultlines watch .

# Check status
faultlines watch-status .

# Stop
faultlines watch-stop .
```

Requires `pip install 'faultlines[watch]'`.

## MCP server (for AI coding agents)

The MCP server is its own package, [`faultlines-mcp`](faultlines-mcp/README.md), so the engine can stay slim and the MCP surface can iterate independently for marketplaces (Cursor / Claude Code / Cline / Aider / Continue / …).

```bash
pip install faultlines-mcp     # depends on this engine
faultlines-mcp                 # stdio MCP server
```

Then point your agent at it (full configs in [`faultlines-mcp/README.md`](faultlines-mcp/README.md)). Ten tools: `list_features` · `find_feature` · `get_feature_files` · `get_flow_files` · `get_feature_owners` · `get_hotspots` · `get_repo_summary` · `find_symbols_in_flow` · `find_symbols_for_feature` · `predict_impact`. When the scan ran with `--symbols`, the agent gets **function-level context with line ranges** instead of whole files — typically ~93% fewer tokens read per query.

## Output format

Results save to `~/.faultlines/feature-map-{repo}-{timestamp}.json`:

```json
{
  "repo_path": "/path/to/repo",
  "remote_url": "https://github.com/org/repo",
  "features": [
    {
      "name": "payments",
      "description": "Stripe payment processing and subscription billing",
      "health_score": 23.0,
      "bug_fix_ratio": 0.339,
      "total_commits": 112,
      "bug_fixes": 38,
      "authors": ["alice", "bob"],
      "paths": ["src/payments/stripe.ts", "src/payments/webhooks.ts"],
      "shared_attributions": [
        {
          "file_path": "src/payments/types.ts",
          "symbols": ["PaymentMethod", "Invoice"],
          "roles": { "PaymentMethod": "type", "Invoice": "type" },
          "line_ranges": [[1, 12], [14, 28]]
        }
      ],
      "flows": [
        {
          "name": "checkout-flow",
          "health_score": 18.0,
          "total_commits": 67,
          "bug_fixes": 28,
          "bus_factor": 1,
          "hotspot_files": ["src/payments/charge.ts"],
          "symbol_attributions": [
            {
              "file_path": "src/payments/charge.ts",
              "symbols": ["handleCheckout", "validateCart"],
              "roles": { "handleCheckout": "handler", "validateCart": "validator" },
              "line_ranges": [[10, 45], [50, 72]],
              "attributed_lines": 58,
              "total_file_lines": 120
            }
          ]
        }
      ]
    }
  ]
}
```

## Health score

The health score (0-100) uses an **age-weighted sigmoid** based on bug-fix ratio:

| Bug fix % | Health | Status |
|:---------:|:------:|--------|
| 0-20% | 85-99 | Healthy — mostly feature work |
| 20-40% | 55-85 | Normal active development |
| 40-55% | 30-55 | Elevated — worth watching |
| 55-75% | 10-30 | High debt — maintenance-dominant |
| 75%+ | 0-10 | Critical — almost all bug fixes |

Recent bugs (< 30 days) weigh 2x more than older ones, so features that are actively getting worse score lower.

## CLI reference

### `faultlines analyze [REPO_PATH]`

| Flag | Default | Description |
|------|---------|-------------|
| `--llm` | off | AI-powered semantic feature detection |
| `--flows` | off | Detect user-facing flows (requires `--llm`) |
| `--symbols` | off | Attribute functions/classes to flows with line ranges (requires `--llm --flows`) |
| `--provider` | `anthropic` | LLM provider: `anthropic` or `ollama` |
| `--model` | auto | Model override (e.g. `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`) |
| `--api-key` | env | Anthropic API key |
| `--src` | — | Focus on subdirectory (e.g. `src/`) |
| `--days` | `365` | Days of git history |
| `--top` | `3` | Top risk zones to highlight |
| `--output` | `~/.faultlines/` | Output file path |
| `--no-save` | — | Don't save JSON |
| `--coverage` | auto | Path to `lcov.info` or `coverage-summary.json` |
| `--legacy` | — | Use pre-rewrite 5-strategy pipeline |
| `--ollama-url` | `localhost:11434` | Custom Ollama URL |

### `faultlines refresh [REPO_PATH]`

| Flag | Default | Description |
|------|---------|-------------|
| `--map` | latest | Path to existing feature-map JSON |
| `--check` | off | Report freshness without writing |
| `--detect-new` | off | Classify orphan files into features via LLM |
| `--refresh-symbols` | off | Re-attribute symbols for changed files |
| `--auto-apply` | off | Auto-apply high-confidence proposals from `--detect-new` |

### Other commands

| Command | Description |
|---------|-------------|
| `faultlines watch .` | Start file watcher daemon for auto-refresh |
| `faultlines watch-status .` | Check watcher status |
| `faultlines watch-stop .` | Stop watcher daemon |
| `faultlines deep-scan .` | Single Sonnet call for features + flows |
| `faultlines version` | Show version |

## How it works

1. `git log` + `git blame` — commit history with file attribution
2. Heuristic candidates from directory structure and import graph
3. Claude Sonnet merges/renames/splits candidates into business features via an operations-based prompt
4. Per-feature health scoring with sigmoid-weighted bug-fix ratio
5. Haiku detects user-facing flows per feature from file signatures and commit patterns
6. CRUD-gap enrichment catches missing create/read/update/delete flows deterministically
7. Symbol attribution maps functions/classes to specific flows with roles and line ranges
8. JSON output + terminal report

For monorepos, step 3 runs per-package in parallel (ThreadPoolExecutor, 4 workers). Libraries are auto-detected and skip flow/symbol attribution.

## Cost

| Repo size | Estimated LLM cost | Time |
|-----------|:------------------:|:----:|
| < 500 files | ~$0.01-0.05 | 15-30s |
| 500-2,000 | ~$0.10-0.30 | 1-5m |
| 2,000-5,000 | ~$0.30-0.70 | 5-12m |
| 5,000-10,000 | ~$0.70-2.00 | 10-25m |

`--symbols` adds ~$0.02-0.10 per feature with flows (one Sonnet call per feature).
Ollama is free (runs locally). Heuristic mode (no `--llm`) is free and instant.
`refresh` is free (no LLM) unless `--detect-new` or `--refresh-symbols` is used.

## Who is this for

- **Engineering managers** — see where technical debt actually lives, not where you think it lives
- **Tech leads** — prioritise refactoring with data, not gut feeling
- **New team members** — understand which parts of the codebase need the most care
- **AI agents** — connect via MCP to get precise function-level context per flow instead of reading whole files

## Repo layout (monorepo)

This repository is a **uv workspace** with two independently-releasable Python packages:

```
featuremap/
├── faultline/                   ← engine package (this README, ships as `faultlines` on PyPI)
│   ├── cli.py                   ← `faultlines` console
│   ├── pipeline_v2/             ← scan-v2 stages 0–7
│   │   ├── extractors/          ← deterministic Stage 1 extractors (go_router, fastapi, _rails, …)
│   │   ├── data/                ← runtime stack patterns + dep-anchors (importlib.resources)
│   │   └── stage_6_5_product_clusterer.py
│   ├── llm/                     ← LLM-backed stages (Sonnet/Haiku via pipeline_v2/run.py)
│   ├── impact/                  ← risk + blast-radius scoring
│   └── symbols/                 ← symbol-level attribution
├── faultlines-mcp/              ← MCP server package (ships as `faultlines-mcp` on PyPI)
│   ├── faultlines_mcp/          ← thin protocol surface, depends on `faultlines`
│   └── pyproject.toml
├── eval/                        ← golden corpus + stack pattern authoring + benchmark scripts
└── pyproject.toml               ← engine + `[tool.uv.workspace]` root
```

### Develop both packages (uv)

```bash
uv sync --all-packages           # installs both packages in editable mode in one venv
.venv/bin/faultlines --help      # engine CLI
.venv/bin/faultlines-mcp         # MCP stdio server
.venv/bin/pytest                 # full suite
```

### Develop without uv (pip only)

```bash
pip install -e .                 # engine
pip install -e ./faultlines-mcp  # MCP (depends on engine)
pip install pytest pytest-cov pytest-asyncio
pytest
```

### Release independently

Each package versions and publishes separately:

```bash
# Engine
hatch build -t wheel             # → dist/faultlines-X.Y.Z-py3-none-any.whl
# MCP
cd faultlines-mcp && hatch build -t wheel
```

## Contributing

Issues, PRs, and feedback welcome at [github.com/PashaSchool/featuremap](https://github.com/PashaSchool/featuremap).

## License

[MIT](LICENSE)
