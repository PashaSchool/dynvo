# File-membership baseline — documenso + inbox-zero

Regression baseline for file-level feature membership, scored against the
hand-curated ground truth in `eval/membership/<slug>/ground-truth.yaml`
(golden-corpus methodology: code-grounded only — routes, workspace
manifests, domain-named server/util dirs, server actions; no README or
marketing prose). Every membership fix (import-closure, ownership model, …)
must be scored against this and beat it — no eyeballing.

## Run conditions

- **Engine**: faultlines v1.17.0 (branch point `f7f9478`, origin/main)
- **Date**: 2026-06-11
- **Repos**: local temp clones (`git clone --local`) of the read-only corpus
  clones, so the corpus stays byte-untouched:
  - documenso @ `229cd2f7e9b01db0899c7fa4946abef571ed706b`
  - inbox-zero @ `91a78dffd6480d06e0730acc9bdd97237e33967c`
- **Command**: `python -m faultline.cli scan-v2 <clone>` with an **isolated
  `$HOME`** (cold scan — no `assignments-*.json`, no llm-cache) and **no
  `ANTHROPIC_API_KEY`** (keyless).
- **Keyless caveat**: LLM stages are skipped — `flows=[]` everywhere, no
  residual scan; only deterministic stages run. That is acceptable for
  *file-membership of developer features* (membership is assembled by the
  deterministic stages), but the baseline does NOT speak to flow quality or
  LLM-derived Layer 2. Scans: documenso 111 dev features ($0.00, 0 LLM
  calls, 99.4 s); inbox-zero 195 dev features ($0.00, 0 LLM calls, 118.0 s).
- **Scoring**: `eval/membership/score_membership.py <scan.json>
  eval/membership/<slug>/ground-truth.yaml --repo <clone>`
  (layer `dev`, greedy 1:1 Jaccard matching — see scorer docstring).

## Results (verbatim)

### documenso (12 truth features)

```
truth features: 12  scan features (dev): 111  matched: 12
micro  file P=0.057 R=0.423
macro  file P=0.196 R=0.395
```

### inbox-zero (11 truth features)

```
truth features: 11  scan features (dev): 195  matched: 11
micro  file P=0.155 R=0.300
macro  file P=0.320 R=0.378
```

| repo | micro P | micro R | macro P | macro R |
|---|---|---|---|---|
| documenso | 0.057 | 0.423 | 0.196 | 0.395 |
| inbox-zero | 0.155 | 0.300 | 0.320 | 0.378 |

Failure shape: file recall sits at ~30–42%, and precision collapses because
many matched scan features are **directory-grained junk drawers** — e.g.
documenso truth `embedding` best-matches scan feature `i18n` whose paths
expand to 1,180 files, and `admin-panel` matches `analytics` at 1,042 files.
Conversely, several scan features carry only the seeding route/dir and miss
domain code in `packages/lib/server-only/*` / `apps/web/utils/*`
(`recipient-signing` matched a focused 8-file `signing` feature → P=0.75 but
R=0.13). Both directions are the target of the import-closure + ownership
work.

## 5 worst features by recall

### documenso

| truth feature | P | R | ∩ | truth files | matched scan feature |
|---|---|---|---|---|---|
| templates | 0.030 | 0.120 | 3 | 25 | account |
| recipient-signing | 0.750 | 0.133 | 6 | 45 | signing |
| organisations | 0.033 | 0.164 | 17 | 104 | remix |
| teams | 0.041 | 0.308 | 20 | 65 | lib |
| admin-panel | 0.034 | 0.343 | 35 | 102 | analytics |

### inbox-zero

| truth feature | P | R | ∩ | truth files | matched scan feature |
|---|---|---|---|---|---|
| email-digest | 1.000 | 0.111 | 2 | 18 | digest-preview |
| ai-assistant-rules | 0.257 | 0.193 | 26 | 135 | account |
| knowledge-base | 0.020 | 0.222 | 2 | 9 | knowledge |
| reply-zero | 0.080 | 0.222 | 8 | 36 | reply-zero |
| email-analytics | 1.000 | 0.229 | 11 | 48 | tinybird |

(All truth features matched at least one scan feature in both repos —
0 unmatched.)

## Reproduce

```bash
rm -rf /tmp/fl-mem && mkdir -p /tmp/fl-mem/home
for slug in documenso inbox-zero; do
  git clone -q --local \
    ~/workspace/_faultlines-testrepos/$slug /tmp/fl-mem/$slug
  env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL HOME=/tmp/fl-mem/home \
    .venv/bin/python -m faultline.cli scan-v2 /tmp/fl-mem/$slug \
    -o /tmp/fl-mem/$slug-scan.json
  .venv/bin/python eval/membership/score_membership.py \
    /tmp/fl-mem/$slug-scan.json eval/membership/$slug/ground-truth.yaml \
    --repo /tmp/fl-mem/$slug
done
```
