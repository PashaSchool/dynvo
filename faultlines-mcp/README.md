# faultlines-mcp

> **Give your AI coding agent precise codebase context instead of letting it grep the repo.**

`faultlines-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server that exposes a [Faultlines](https://github.com/PashaSchool/faultlines) feature map to AI coding agents (Cursor, Claude Code, Cline, Aider, Continue, …). Agents call typed tools to ask *"which files implement checkout?"* and get back the exact files, flows, hotspots and (when symbols were extracted) line ranges — instead of opening 15 files and burning your context window.

This package is the thin protocol surface over the engine. The engine itself ([`faultlines`](../README.md)) does the detection; this server just serves the JSON it produces.

---

## Install

You need a Faultlines scan first (`pip install faultlines && faultlines scan-v2 <repo>`). Then:

```bash
pip install faultlines-mcp
```

This installs the `faultlines-mcp` console script.

## Wire it into your AI agent

### Cursor (`~/.cursor/mcp.json`)

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

### Cline / Continue / generic MCP host

Configure a stdio MCP server with `command: faultlines-mcp`. No flags needed by default — it auto-discovers the most recent scan under `~/.faultline/`.

To pin a specific scan:

```bash
FAULTLINE_MAP_PATH=/path/to/feature-map.json faultlines-mcp
```

## Tools exposed to the agent

| Tool | What the agent gets back |
|---|---|
| `list_features` | All features sorted by risk — name, health, paths summary |
| `find_feature` | Full detail for a named feature — flows, authors, health, hotspots |
| `get_feature_files` | Exact file list for a feature |
| `get_flow_files` | Files in one user-facing flow |
| `get_feature_owners` | Top contributors for a feature |
| `get_hotspots` | The riskiest features (lowest health) across the repo |
| `get_repo_summary` | High-level stats — feature count, flow count, scan age |
| `find_symbols_in_flow` | Functions/classes in a flow with **line ranges + roles** (handler / validator / type / …) — only if the scan ran with `--symbols` |
| `find_symbols_for_feature` | Shared types/interfaces for a feature |
| `predict_impact` | Risk estimate for a proposed change — flows touched, blast radius, suggested reviewers |

When the scan ran with `--symbols`, agents get **function-level context with line ranges** instead of whole files — typical reduction is ~93% in tokens read per query.

Each response also carries a small `_savings_metadata` block (avg files an agent would have read without MCP, vs. what this response cost) — your local agent ignores it; the hosted dashboard uses it to aggregate token savings.

## Privacy & freshness

- The server only ever reads the **derived feature-map JSON** on your disk — never your source code.
- If the scan is older than 30 days, every response carries a `staleness_warning` so the agent knows to discount it. Run `faultlines refresh <repo>` to refresh incrementally.

## Engine boundary

This package depends on `faultlines` for exactly **one** symbol today: `faultline.impact.risk.predict_impact`. Everything else is read from the feature-map JSON. That's the intended public contract — the engine output schema (versioned via `schema_version` in the JSON) is the seam.

## Development (monorepo)

This package lives at `faultlines-mcp/` in the [faultlines monorepo](../README.md) alongside the engine. The repo uses a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/) — `uv sync` from the repo root installs both packages in editable mode:

```bash
uv sync                              # installs faultline + faultlines-mcp in one venv
.venv/bin/faultlines --help          # engine CLI
.venv/bin/faultlines-mcp             # MCP server (stdio)
.venv/bin/pytest tests/test_mcp_server.py
```

## License

MIT.
