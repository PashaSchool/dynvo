# Structural audit — golden-free regression gate

`eval/structural_audit.py` measures **structural pathologies that are wrong on
any repo regardless of ground truth**, so it runs on the whole corpus and
catches regressions a curated answer-key can't. It is the gate for attribution
changes (backward import-following, package-anchor de-sinking).

## The pathology it tracks

A single developer feature, named after a package root (`backend`,
`frontend-v2`, `dify-web`, `inbox-zero-ai`), absorbs a large share of the
repo's files into one blob — the engine never attributed the
service/model/page long-tail to the features that use it, so it fell to the
package node. The signal is **file-share concentration**.

## Metrics (per repo)

| metric | meaning | direction |
|---|---|---|
| `max_feature_share` | largest feature's share of all attributed files | ↓ better |
| `top3_share` | distinct union of the 3 biggest features | ↓ better |
| `gini` | inequality of feature sizes (0 even → 1 all-in-one) | ↓ better |
| `blob_count` | features that are oversized **and** path-concentrated under one top dir **and** (container-named or ≥40% — too big to be one feature) | ↓ better |
| `dev_features_with_pf_pct` | dev features that roll up into a product feature (keyed scans only) | ↑ better |

All thresholds are scale-invariant ratios (fair-share multiples, share floors),
not corpus-tuned constants — see `rule-no-magic-tuning`.

## Usage

```bash
# write the baseline (keyless, $0, deterministic)
eval/run_structural_corpus.sh --baseline
# gate a change: fails if any repo's concentration regresses past tolerance
eval/run_structural_corpus.sh --compare
# ad-hoc on any scan json(s)
python -m eval.structural_audit scan.json --json out.json
```

## Baseline (keyless, 2026-06-14) — after workspace-anchor de-sink (Stage 8.7)

| repo | files | feats | max% | top3% | gini | blobs |
|---|---|---|---|---|---|---|
| dify | 5102 | 83 | **76%** | 82% | 0.83 | dify-web (76%) |
| axios | 68 | 35 | 81% | 82% | 0.41 | (tiny flat library — no workspaces; known hard) |
| infisical | 6849 | 127 | **34%** | 69% | 0.58 | backend (34%), frontend-v2 (25%) |
| inbox-zero | 1914 | 172 | **26%** | 39% | 0.33 | — (blob cleared) |
| gin | 39 | 19 | 41% | 82% | 0.52 | — |
| fastapi | 379 | 33 | 32% | 60% | 0.42 | — |
| documenso | 2189 | 125 | **16%** | 39% | 0.42 | lib (11%) |

documenso is the well-decomposed control (16% max, unchanged). **Stage 8.7
workspace-anchor de-sink** (2026-06-14) released the files a workspace anchor
double-claims with a more-specific feature back to that feature, dropping the
concentration on every monorepo blob with no membership recall cost
(`eval/membership` precision rose, recall flat): infisical max 53%→34%,
inbox-zero 50%→26% (blob cleared), dify 81%→76% (only its import-reachable
slice — dify-web's remaining bulk is the dependency-injected / component
long-tail that needs DI-aware attribution, a separate lever). Flat libraries
with no declared workspaces (axios, fastapi, gin) have no workspace anchor and
are untouched. The remaining concentration is the genuine residual the next
levers (DI-service attribution, shared-scaffold filtering) must re-home.
