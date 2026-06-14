# Structural audit — golden-free regression gate

`eval/structural_audit.py` measures **structural pathologies that are wrong on
any repo regardless of ground truth**, so it runs on the whole corpus and
catches regressions a curated answer-key can't. It is the gate for attribution
changes (backward import-following, package-anchor de-sinking).

## The pathology it tracks

A single developer feature, named after a package root (`backend`,
`frontend-v2`, `dify-web`, `inbox-zero-ai`), absorbs a large share of the
repo's files — the engine never attributed the service/model/page long-tail to
the features that use it, so it fell to the package node. These package nodes
are **workspace anchors** (the `"workspace anchor"` rationale marker). They are
*recognised* as PLATFORM buckets and measured separately: their footprint is the
**`platform_share`** signal, while the **concentration metrics measure how well
the REAL (non-platform) product features are decomposed**.

## Metrics (per repo)

| metric | meaning | direction | gated? |
|---|---|---|---|
| `platform_share` | share of all files held by recognised platform buckets (workspace anchors) — the package-node-blob signal | ↓ better | tracked, **not gated** |
| `max_feature_share` | largest REAL feature's share of real files | ↓ better | ✅ |
| `top3_share` | distinct union of the 3 biggest REAL features | ↓ better | ✅ |
| `gini` | inequality of REAL feature sizes | — | informational, **not gated** |
| `blob_count` | REAL features that are oversized **and** path-concentrated **and** container-named-or-≥40% | ↓ better | reported |
| `dev_features_with_pf_pct` | dev features that roll up into a product feature (keyed scans only) | ↑ better | reported |

All thresholds are scale-invariant ratios (fair-share multiples, share floors),
not corpus-tuned constants — see `rule-no-magic-tuning`.

**Why `gini` is no longer gated (2026-06-14 reframe):** gini rewards feature-size
*evenness*, which shared-scaffold PADDING produces artificially — every feature
bloated with the same shared `lib`/`ui` files looks evenly-sized. A precision
pass that *de-pads* features (Stage 8.6.5 shared-scaffold filter) reveals their
true, naturally-unequal sizes and so RAISES gini even as attribution improves.
gini and precision are anti-correlated here, so gini is kept as an informational
signal only. `platform_share` is likewise tracked but not gated: a precision pass
that consolidates shared scaffold onto the anchor legitimately raises it, while
de-sink / DI-attribution lower it.

## Usage

```bash
# write the baseline (keyless, $0, deterministic)
eval/run_structural_corpus.sh --baseline
# gate a change: fails if any repo's concentration regresses past tolerance
eval/run_structural_corpus.sh --compare
# ad-hoc on any scan json(s)
python -m eval.structural_audit scan.json --json out.json
```

## Baseline (keyless, 2026-06-14) — platform-bucket reframe + shared-scaffold filter

`feats` is the REAL (non-platform) feature count; `max%`/`top3%` are over real
features; `plat%` is the recognised-platform footprint (the blob signal).

| repo | files | feats | plat% | max% | top3% | blobs |
|---|---|---|---|---|---|---|
| dify | 5090 | 77 | **81%** | 15% | 35% | datasets (1) |
| documenso | 2144 | 109 | **64%** | 20% | 44% | — |
| infisical | 6849 | 125 | **59%** | 24% | 56% | — |
| inbox-zero | 1879 | 162 | **34%** | 11% | 21% | — |
| axios | 56 | 32 | 0% | 77% | 79% | 16 (flat lib — no workspaces; known hard) |
| fastapi | 379 | 33 | 0% | 32% | 60% | — |
| gin | 39 | 19 | 0% | 41% | 82% | — |

**`plat%`** is the package-node-blob signal (replaces platform-inclusive
`max%`): the share of the repo held by recognised workspace anchors. de-sink
(Stage 8.7) lowers it (infisical 53→34 of platform-inclusive max, i.e. the
anchor shrank); the shared-scaffold filter (Stage 8.6.5) raises it slightly
(scaffold consolidates onto the anchor) — tracked, not gated. **`max%`/`top3%`**
now measure REAL-feature decomposition and are healthy everywhere a workspace
exists (dify 15%, infisical 24%, inbox-zero 11%), confirming the engine
decomposes the real product features well once the platform bucket is split out.
The genuine remaining work is **lowering `plat%`** — DI-service attribution
(re-home the service/db long-tail) — and curating ground truth for the flat-lib
case (axios). Blobs are now REAL-feature blobs only (platform excluded); flat
libraries with no workspace (axios/fastapi/gin) have no platform bucket.
