# Changelog

All notable changes to **Faultlines** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This is a curated history of how Faultlines evolved from a single-file CLI into a
deterministic-first, two-layer feature-mapping engine with an AI-agent toolkit.

## [Unreleased]

## [1.0.0] – 2026-06-01

First stable release. The engine and the AI-agent MCP server are now separate,
independently versioned packages.

### Changed
- **Monorepo split.** The repository is now a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/)
  with two independently published packages: the `faultlines` engine and the
  standalone `faultlines-mcp` server.
- **MCP extracted to its own engine-free package.** `faultlines-mcp` no longer
  depends on the engine — it reads only the feature-map JSON, so a local install
  stays small and fast. The engine **no longer ships the MCP SDK** (the
  `faultlines[mcp]` extra is gone; `pip install faultlines-mcp` instead).

### Added
- Native `hotspot_files` emitted on every feature, flow and product feature.
- Rewritten project README and this changelog.

### Removed
- Legacy `benchmarks/` harness.

## [0.11.0]

### Changed
- Pinned the LLM analyst to `claude-sonnet-4-6` across the pipeline for stable,
  reproducible naming and flow detection.

### Fixed
- Hermetic runtime-data loading via `importlib.resources` (no more reliance on
  the working directory at scan time).
- Stage 1 now always merges built-in extractors with entry-point plugins, so a
  stale `entry_points.txt` can never silently drop a built-in extractor.

## [0.10.0]

### Added
- **Stage 8.5 — deterministic path-overlap member backfill.** Attaches files to
  the features they belong to by path overlap, lifting attachment coverage
  substantially without an LLM call.
- **Stage 6.9 — test-file output strip.** Test files are detected (token-based
  predicate, catches `*.e2e-spec.ts`, `*.spec`, etc.) and stripped from the
  feature output tree while still feeding coverage.

### Fixed
- FastAPI route extraction and a Django false-positive in Stage 0/1.

## [0.9.0] – [0.9.1]

### Added
- **Stack-aware extraction.** Per-stack pattern library (`eval/stacks/*.yaml`)
  drives route / MVC / schema / package extractors so detection adapts to
  Next.js, Rails, Django, FastAPI, Express, Spring, Laravel, Phoenix and more.
- Flow `short_label` (kebab name without the `-flow` suffix).

### Fixed
- Sonnet model alias corrected to a valid id (was a 404-ing placeholder).

## [0.8.0]

### Added
- **`pipeline_v2` — the two-layer rebuild.** Output now carries
  `developer_features[]` (code-grounded) and `product_features[]`
  (marketing-grounded clusters), plus a top-level `flows[]` array and typed
  feature↔flow edges.
- Deterministic-first staged architecture: intake → parallel extractors →
  anchor reconciliation → flow detection → LLM fallback (residual only) →
  post-process → metrics → output.

## [0.7.0]

### Added
- Scanner flow-detection upgrades: app-layer catch-all splitting, flow dedup,
  noise dropping, CRUD-gap detection and explicit entry-point handling.
- Health, bug-fix-ratio, churn, impact, behavioral coverage and
  ownership / bus-factor metrics wired into the scan output.

## [0.6.0]

### Added
- CLI surface: `--symbols`, `--push`, `refresh`, the human-readable reporter,
  coverage output and the first CI workflow.

## [0.5.0]

### Added
- Infrastructure modules: content-hash LLM cache, change-impact / blast-radius,
  the first MCP server, cloud/replay registry and `--watch` mode.

## [0.4.0]

### Added
- **Symbol-level attribution.** Flows are attributed down to functions and
  classes with line ranges and deterministic role classification
  (handler / validator / type / …), with CRUD-gap enrichment.

## [0.3.0]

### Added
- New Sonnet-based detection pipeline, library (importable) mode and parallel
  workspace scanning.

## [0.2.0] – [0.2.1]

### Added
- Improved feature and flow detection.
- First test suite.

## [0.1.0]

### Added
- Initial Faultlines CLI: map features in any codebase from git history alone.

---

[Unreleased]: https://github.com/PashaSchool/faultlines/compare/main...HEAD
[1.0.0]: https://pypi.org/project/faultlines/1.0.0/
[0.11.0]: https://pypi.org/project/faultlines/0.11.0/
[0.10.0]: https://pypi.org/project/faultlines/0.10.0/
[0.9.1]: https://pypi.org/project/faultlines/0.9.1/
[0.9.0]: https://pypi.org/project/faultlines/0.9.0/
[0.8.0]: https://pypi.org/project/faultlines/0.8.0/
[0.7.0]: https://pypi.org/project/faultlines/0.7.0/
[0.6.0]: https://pypi.org/project/faultlines/0.6.0/
[0.5.0]: https://pypi.org/project/faultlines/0.5.0/
[0.4.0]: https://pypi.org/project/faultlines/0.4.0/
[0.3.0]: https://pypi.org/project/faultlines/0.3.0/
[0.2.1]: https://pypi.org/project/faultlines/0.2.1/
[0.2.0]: https://pypi.org/project/faultlines/0.2.0/
[0.1.0]: https://pypi.org/project/faultlines/0.1.0/
