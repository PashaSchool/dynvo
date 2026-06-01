<div align="center">

# 🗺️ Faultlines

### Your codebase has a map. You just can't see it yet.

**Faultlines turns raw git history into a living map of every _feature_ and _user flow_ in your repo — with bug hotspots, test coverage, and the precise context your AI coding agent actually needs.**

No Jira. No annotations. No manual tagging. Just `git log` and your code.

[![PyPI](https://img.shields.io/pypi/v/faultlines?color=6E56CF&label=pip%20install%20faultlines)](https://pypi.org/project/faultlines/)
[![Python](https://img.shields.io/pypi/pyversions/faultlines?color=6E56CF)](https://pypi.org/project/faultlines/)
[![License: MIT](https://img.shields.io/badge/License-MIT-6E56CF.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-ready-6E56CF)](https://modelcontextprotocol.io)

[**Website**](https://faultlines.dev) · [**Quick start**](#-quick-start) · [**For AI agents**](#-built-for-ai-coding-agents) · [**How it works**](#-how-it-works)

</div>

---

## The problem

Every engineer rediscovers the same thing on a new codebase:

> *"Which files actually implement checkout?"*
> *"What breaks if I change this?"*
> *"Who owns the billing flow?"*
> *"Where are the bugs hiding?"*

Your **issue tracker** doesn't know — it's full of aspirational tickets.
**Static analysis** sees imports, not intent.
And your **AI agent** just `grep`s 15 files and burns your context window guessing.

The answers were in your **git history** the whole time. Faultlines reads them.

## What Faultlines does

```
$ faultlines scan-v2 ./my-app --llm --flows --symbols

  ✓ 472 commits analysed · 1,284 files mapped

  FEATURE                  HEALTH  COVERAGE  HOTSPOTS  FLOWS
  ─────────────────────────────────────────────────────────
  Billing & Subscriptions    62      48%        3        7
  Authentication             88      91%        0        4
  Document E-Sign            41 ⚠     22%        5        9
  Team & Permissions         79      66%        1        5
  …

  → Highest blast radius:  src/payments/charge.ts (touches 4 features)
  → Riskiest flow:         e-sign/finalize  (low coverage · 5 recent bug-fixes)
```

A **two-layer feature map**:

- **Developer features** — code-grounded, the units your engineers actually work in.
- **Product features** — the customer-facing capabilities those roll up into.

…each broken into **flows** (real user journeys), scored, attributed down to the **function and line range**, and served to humans *and* AI agents.

## ✨ Features

- 🧭 **Feature & flow detection** — from git history + code structure, on **any stack** (Next.js, Rails, Django, FastAPI, Express, Spring, Laravel, Phoenix, and more).
- 🔥 **Bug hotspots & health scores** — find what's rotting before it pages you.
- 🎯 **Behavioral test coverage** — coverage *per user flow*, inferred from history even when there's no `lcov`.
- 💥 **Change-impact / blast radius** — "if I touch these files, here's what breaks and who to add as reviewer."
- 🔬 **Symbol-level attribution** — functions, classes and **line ranges** per flow, not just file lists.
- 👥 **Ownership & bus-factor** — who maintains each feature, and where the knowledge is dangerously concentrated.
- 🤖 **MCP server for AI agents** — 13 typed tools your coding agent calls instead of grepping.
- 📡 **Runtime overlays** — map **Sentry** errors and **PostHog** usage onto features (which features actually fail and get used).
- 🔒 **Local-first & private** — runs on your machine; your source code never has to leave it.

## 🚀 Quick start

```bash
pip install faultlines

# Scan a repo — deterministic by default, add --llm for richer naming
faultlines scan-v2 /path/to/your/repo --llm --flows --symbols
```

That writes a versioned **feature-map JSON** to `~/.faultline/`. Explore it, diff it across runs, ship it to CI, or hand it to your AI agent (below).

## 🤖 Built for AI coding agents

This is the wedge. Install the companion MCP server and your agent stops guessing:

```bash
pip install faultlines-mcp
```

```jsonc
// ~/.cursor/mcp.json  (or: claude mcp add faultlines -- faultlines-mcp)
{
  "mcpServers": {
    "faultlines": { "command": "faultlines-mcp" }
  }
}
```

Now Cursor / Claude Code / Cline / Windsurf can call **13 tools**:

| | Tools |
|---|---|
| 🔎 **Discover** | `list_features` · `find_feature` · `get_repo_summary` |
| 📁 **Files & symbols** | `get_feature_files` · `get_flow_files` · `find_symbols_in_flow` · `find_symbols_for_feature` |
| ⚠️ **Risk & impact** | `get_hotspots` · `get_feature_owners` · `analyze_change_impact` · `get_regression_risk` |
| 📡 **Runtime** | `get_feature_errors` (Sentry) · `get_feature_pageviews` (PostHog) |

> Typical result: **~90% fewer tokens** per query than a naive grep-and-read loop — your agent reads the *right* functions, with line ranges, on the first try.

## 📊 The metrics — and why they matter

| Metric | What it tells you | Why you care |
|---|---|---|
| **Health score** | Composite of churn, bug-fixes, coverage & ownership | One number to triage what to refactor next |
| **Bug-fix ratio** | Share of commits that fix bugs | High = fragile, defect-prone code |
| **Churn** | How often a feature changes | Hotspot detection; instability signal |
| **Impact score** | Structural blast radius − coverage | What a change here actually endangers |
| **Coverage** | Behavioral test coverage **per flow** | Find untested user journeys, not just untested lines |
| **Ownership / bus factor** | Who holds the knowledge | Spot single-points-of-failure before they leave |

## 🧠 How it works

```
 git history ─┐
              ├─▶  deterministic extractors  ─┐
 code/config ─┘    (routes · MVC · schema ·   │
                    package · stack patterns)  ├─▶  feature & flow map
                                               │     + metrics + symbols
        optional LLM pass (naming · flows) ────┘            │
                                                            ▼
                              feature-map JSON  ──▶  CLI · CI · dashboard · MCP
```

**Deterministic-first.** The structure comes from your routing conventions, configs, schemas and git co-change patterns. An optional LLM pass adds human-readable names and flow detection. The output is a single versioned JSON — the stable contract every consumer reads.

## 🔌 Integrations

- **GitHub** — PR comments with risk, coverage gaps and runtime signal on the exact features a diff touches.
- **Sentry** — production errors mapped to features.
- **PostHog** — real usage & traffic per feature.
- **Slack** — weekly digest of top risks, coverage gaps and hotspots.

## 🆚 Why not just…

- **…grep / read the files?** Burns context and misses cross-boundary, runtime and historical coupling that static analysis can't see.
- **…SonarQube / linters?** Great for line-level issues; blind to *features*, *flows* and *blast radius*.
- **…your issue tracker?** Describes intent, not reality. Faultlines is grounded in what the code and history actually say.

Faultlines is the only layer that joins **structure + git history + runtime** into one map — and serves it to your AI agent.

## 🗺️ Roadmap

- [x] Two-layer feature/flow map on any stack
- [x] Behavioral test coverage & health scoring
- [x] Symbol-level attribution
- [x] MCP server (13 tools) — Local · Hosted · VPC
- [x] Sentry + PostHog runtime overlays
- [x] Incremental, sub-second re-scans on every commit
- [x] Native plugins for more agents & IDEs

## 🤝 Contributing

Issues, ideas and PRs are welcome. Faultlines is built to map *any* codebase — if it mis-reads your stack, that's a bug we want to hear about.

## 📄 License

MIT — see [LICENSE](LICENSE).

<div align="center">

**[⭐ Star this repo](https://github.com/PashaSchool/faultlines)** if Faultlines helps you (or your agent) understand a codebase faster.

Made for engineers and the AI agents that work alongside them · [faultlines.dev](https://faultlines.dev)

</div>
