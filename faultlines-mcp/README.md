# faultlines-mcp

> **Give your AI coding agent precise codebase context instead of letting it grep the repo.**

`faultlines-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io)
server that hands AI coding agents (Cursor, Claude Code, Cline, Windsurf, Aider,
Continue, …) a structured map of your codebase. Your agent calls typed tools —
*"which files implement checkout?"*, *"what breaks if I touch these files?"*,
*"who owns billing?"* — and gets back exact files, flows, owners, hotspots, and
(when available) function-level line ranges, instead of opening 15 files and
burning your context window.

It is a **standalone, lightweight package** — its only dependency is the MCP
protocol library. It does **not** pull in the Faultlines engine. It reads the
*feature‑map JSON* the engine produces on disk and serves it over MCP. Small,
fast, and safe to run anywhere your agent runs.

```
 ┌─────────────┐    scans once     ┌──────────────────────┐    reads (engine-free)   ┌──────────────┐
 │  your repo  │ ───────────────▶  │  feature-map JSON     │ ──────────────────────▶  │ faultlines-  │
 │  (git)      │   faultlines      │  ~/.faultline/*.json  │                          │ mcp (this)   │
 └─────────────┘   engine          └──────────────────────┘                          └──────┬───────┘
                                                                                            │ MCP (stdio)
                                                                                     ┌──────▼───────┐
                                                                                     │  your agent  │
                                                                                     │ Cursor/Claude│
                                                                                     └──────────────┘
```

---

## Install

```bash
pip install faultlines-mcp
```

That installs the `faultlines-mcp` console script (a stdio MCP server). It has
no heavy dependencies.

### Prerequisite: a feature map

The MCP server reads a feature map; it doesn't create one. Generate it once with
the Faultlines engine (a **separate** install — you only need it to scan, not to
run the MCP):

```bash
pip install faultlines
faultlines scan-v2 /path/to/your/repo --llm --flows --symbols
# → writes ~/.faultline/feature-map-<repo>-<timestamp>.json
```

Run `--symbols` if you want function/class‑level answers (line ranges) instead
of whole‑file answers. Re‑scan (or `faultlines refresh <repo>`) whenever the code
moves on.

---

## Wire it into your agent

### Cursor — `~/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "faultlines": {
      "command": "faultlines-mcp"
    }
  }
}
```

### Claude Code

```bash
claude mcp add faultlines -- faultlines-mcp
```

### Cline / Windsurf / Continue / any MCP host

Register a **stdio** server with `command: faultlines-mcp`. No flags are needed
by default — it auto‑discovers the most recent scan under `~/.faultline/`.

### Pin a specific map

```bash
FAULTLINE_MAP_PATH=/abs/path/to/feature-map.json faultlines-mcp
```

**Map resolution order:** `$FAULTLINE_MAP_PATH` → the newest
`~/.faultline/feature-map-*.json`. If none is found, the server returns a clear
error telling the agent to run a scan first.

---

## The 13 tools

Every tool returns a short human `summary` plus a structured `details` payload.
Grouped by what your agent is trying to do:

### 🔎 Discover the map
| Tool | Args | Returns |
|---|---|---|
| `list_features` | — | Every feature with health score, path count, coverage — the menu to start from. |
| `find_feature` | `query` | Fuzzy‑matches one feature by name / alias / description; returns its paths, flows, health, coverage, impact. |
| `get_repo_summary` | — | High‑level stats: feature & flow counts, file count, average coverage, top hotspots, scan age. |

### 📁 Files & symbols
| Tool | Args | Returns |
|---|---|---|
| `get_feature_files` | `feature_name` | The exact file list for a feature. |
| `get_flow_files` | `feature_name`, `flow_name` | Files for one user‑facing flow inside a feature. |
| `find_symbols_in_flow` | `feature_name`, `flow_name` | Precise **functions/classes** for a flow, grouped by file, with **line ranges + roles** (handler / validator / type / …) and deep‑links. Falls back to file paths if the scan had no symbols. |
| `find_symbols_for_feature` | `feature_name` | The feature's shared symbols (types, interfaces, enums) aggregated across its flows. |

> With a `--symbols` scan, agents read **function‑level context with line ranges**
> instead of whole files — typically ~90%+ fewer tokens per query.

### ⚠️ Risk & impact
| Tool | Args | Returns |
|---|---|---|
| `get_hotspots` | `limit=5` | The riskiest features (low health, high bug‑fix ratio / churn) — the refactor / on‑call priority list. |
| `get_feature_owners` | `feature_name` | Top maintainers / bus‑factor for a feature — who to ask or add as reviewer. |
| `analyze_change_impact` | `changed_files[]` | **Blast radius** for a set of files you're about to change: which features they touch, total impact, files that historically co‑change but are *missing* from your change set, risk level, and recommendations. Engine‑free (path‑overlap over the scan). |
| `get_regression_risk` | `changed_files[]` | A quick `low / medium / high / critical` verdict weighted by the bug‑fix history of the features you're touching. |

### 📡 Runtime (hosted)
| Tool | Args | Returns |
|---|---|---|
| `get_feature_errors` | `feature_name`, `window="24h"` | Production **errors (Sentry)** mapped to a feature. |
| `get_feature_pageviews` | `feature_name`, `window="24h"` | Product **usage / pageviews (PostHog)** for a feature. |

> The two runtime tools need a connected Sentry / PostHog integration, which
> lives in the **hosted** Faultlines deployment. In the local package they are
> still registered (so the toolkit is identical across modes) but return a
> graceful `{ "available": false, "reason": "connect a Sentry/PostHog integration" }`.

---

## Deployment modes — same toolkit everywhere

All 13 tools have identical names and shapes in every mode:

- **Local** — this package, on your machine, reading your on‑disk map. Your code
  and map never leave the laptop. (Runtime tools are hosted‑only.)
- **Hosted** — Faultlines runs the MCP in our cloud; your agent calls it over
  HTTPS with an API key. Runtime tools (Sentry/PostHog) light up.
- **VPC** — the whole stack (engine + dashboard + MCP) runs inside your network.

---

## Privacy & freshness

- The server only ever reads the **derived feature‑map JSON** on disk — never
  your source code.
- If the scan is older than 30 days, every response carries a
  `staleness_warning` so the agent discounts it. Re‑scan or
  `faultlines refresh <repo>` to update.
- Each response carries a tiny `_savings_metadata` block (files an agent would
  have read without MCP vs. what this response cost). Your local agent ignores
  it; the hosted dashboard aggregates it into token‑savings stats.

---

## How it works

1. The engine scans your git history + code and writes a versioned feature‑map
   JSON (`schema_version` in the file is the contract).
2. This server loads that JSON and exposes the 13 tools over MCP (stdio).
3. Tools are **pure reads** of the JSON — no engine import, no live git, no
   network (except the hosted runtime tools). That's why the package is small
   and fast.

The engine‑output schema is the seam between the two packages: the engine can
evolve its internals freely as long as the JSON shape holds.

---

## Development (monorepo)

This package lives at `faultlines-mcp/` in the
[faultlines monorepo](../README.md). The repo uses a
[uv workspace](https://docs.astral.sh/uv/concepts/workspaces/):

```bash
uv sync                       # installs faultlines-mcp (and the engine, for scanning)
.venv/bin/faultlines-mcp      # run the MCP server (stdio)
.venv/bin/pytest              # tests
```

To prove the MCP is engine‑free, install just this package in a clean venv and
confirm it imports and lists 13 tools without `faultlines` present.

---

## License

MIT.
