# faultlines-mcp

MCP server for the [Faultlines](https://faultlines.dev) feature map — serve
precise, structured codebase context to AI coding agents (Cursor, Claude Code,
Cline, Aider) instead of having them grep the whole repo.

The server reads a Faultlines feature-map JSON (features → flows → files, with
metrics like health, churn, ownership, and hotspots) and exposes it over the
Model Context Protocol as a small set of read-only tools. It is **fully
standalone**: it never imports the engine and has zero runtime dependency on it
(optional auto-refresh shells out to the `faultlines` CLI via subprocess).

## Install

```bash
# one-off, no install (recommended)
uvx faultlines-mcp

# or install into an environment
pip install faultlines-mcp
```

## Use with an agent

Point your MCP client at the `faultlines-mcp` command. Example (Claude Code /
Cursor `mcp` config):

```json
{
  "mcpServers": {
    "faultlines": {
      "command": "uvx",
      "args": ["faultlines-mcp"]
    }
  }
}
```

The server loads the feature-map JSON for the current repo (or the path you
configure) and answers tool calls against it.

## Tools (read-only)

- `list_features` — every detected feature
- `find_feature` — fuzzy-find a feature by query
- `get_feature_files` — files that make up a feature
- `get_flow_files` — files for a specific flow within a feature
- `get_repo_summary` — high-level repo overview
- `get_hotspots` — files/features with the highest bug-fix churn
- `get_feature_owners` — top contributors + bus-factor risk
- `get_regression_risk` — risk for a set of changed files
- `find_symbols_in_flow` / `find_symbols_for_feature` — symbol-level drill-down
- `get_feature_errors` — production errors per feature (Sentry, when the hosted
  connection supplies runtime data; otherwise a graceful "unavailable")
- `get_feature_pageviews` — product usage per feature (PostHog, same runtime
  rule)

## Modes

- **stdio** (default) — local use with an agent; pure stdlib + the MCP protocol
  library, no web framework pulled in.
- **HTTP service** (`pip install 'faultlines-mcp[http]'`, run `faultlines-mcp-serve`)
  — the shape the hosted Faultlines dashboard proxies to.

## License

MIT
