# Name-quality baseline — documenso + inbox-zero

Regression baseline for feature NAME quality, scored against the
reference `display_name`/`aliases` in
`eval/membership/<slug>/ground-truth.yaml` with
`eval/membership/score_names.py` (deterministic token-F1 mode — $0,
no LLM). The reference labels were derived from the SAME structural
sources cited in each feature's curation notes — in-app nav/page-title
strings parsed from source (`SideNav.tsx`, `app-nav-desktop.tsx`,
`<Trans>` settings-nav labels, `PageHeader title=`), route groups and
domain dir names. NO README, NO marketing prose.

Every naming change (product-string evidence, anti-hallucination
validator, namer prompt edits) must be scored against this and beat
it — no eyeballing.

## Run conditions

- **Engine**: faultlines v1.19.0 (branch point `1b4ef18`, origin/main —
  PRE PR #48; the naming-evidence work is exactly what this baseline
  exists to measure)
- **Date**: 2026-06-12
- **Repos**: local temp clones (`git clone --local`) of the read-only
  corpus clones (corpus untouched):
  - documenso @ `229cd2f7e9b01db0899c7fa4946abef571ed706b`
  - inbox-zero @ `91a78dffd6480d06e0730acc9bdd97237e33967c`
- **Command**: `python -m faultline.cli scan-v2 <clone>` with an
  isolated `$HOME` (cold scan) and **no ANTHROPIC_API_KEY** (keyless).
- **Keyless caveat — this is the honest floor**: with no key, the LLM
  namers never run; every feature carries its deterministic slug
  (route/dir-derived). These numbers measure how recognizable the
  deterministic slugs alone are. **LLM-named scans need a keyed run
  post-#48 merge** — that run becomes the first real measurement of
  the naming stack and must beat this floor.
- **Scoring**: `python eval/membership/score_names.py <scan.json>
  eval/membership/<slug>/ground-truth.yaml --repo <clone>`
  (matching REUSES score_membership's greedy 1:1 Jaccard; name metric
  is best token-F1 vs display_name+aliases, kebab/camel tokenizer with
  stop-words, light singularization and ≥4-char prefix-stem tolerance
  — same spirit as the engine's naming validator).

## Results (verbatim)

### documenso (125 dev features scanned, keyless)

```
truth features: 12  matched: 12  mean token-F1: 0.444

truth feature            display name              F1  matched scan feature
admin-panel              Admin Panel            0.667  admin
authentication           Authentication         1.000  auth
billing-subscription     Billing                0.000  stripe
document-management      Documents              0.000  trpc
embedding                Embedding              1.000  embed
folders                  Folders                0.000  ai
organisations            Organisations          0.000  o
public-api               Public API             1.000  api
recipient-signing        Document Signing       1.000  signing
teams                    Teams                  0.667  t-team-url
templates                Templates              0.000  account
webhooks                 Webhooks               0.000  lib
```

### inbox-zero (172 dev features scanned, keyless)

```
truth features: 11  matched: 11  mean token-F1: 0.667

truth feature            display name              F1  matched scan feature
ai-assistant-rules       Assistant              0.000  chat
billing-premium          Premium                0.000  stripe
bulk-unsubscribe         Bulk Unsubscribe       1.000  bulk-unsubscribe
cold-email-blocker       Cold Email Blocker     1.000  cold-email-blocker
email-analytics          Email Analytics        0.000  tinybird
email-cleaner            Deep Clean             1.000  clean
email-digest             Digest                 0.667  digest-preview
knowledge-base           Knowledge Base         1.000  knowledge
meeting-briefs           Meeting Briefs         0.667  onboarding-brief
reply-zero               Reply Zero             1.000  reply-zero
smart-categories         Sender Categories      1.000  smart-categories
```

| repo | truth feats | matched | mean token-F1 (deterministic) |
|---|---|---|---|
| documenso | 12 | 12 | 0.444 |
| inbox-zero | 11 | 11 | 0.667 |

Failure shape: zeros are of two kinds. (a) **membership failure, not
naming failure** — the best-Jaccard match is a junk-drawer/structural
feature (`trpc`, `lib`, `o`, `account`); the name can't be right when
the file-set is wrong, so these zeros will move with membership fixes,
not naming fixes. (b) **vocabulary mismatch** — the slug is honest but
infrastructure-flavored (`stripe` vs "Billing"/"Premium", `tinybird`
vs "Email Analytics", `chat` vs "Assistant"); exactly the gap the
LLM namer + product-string evidence (#48) is supposed to close
without hallucinating.

## LLM-judge mode

`--judge` adds one batched Haiku (`claude-haiku-4-5`) call scoring
each matched pair on PM-recognizability (0-2) and unsupported-claim
(boolean vs evidence keywords). It refuses to run without
ANTHROPIC_API_KEY; not part of this $0 baseline. Run it alongside the
keyed post-#48 scan.

## Reproduce

```bash
rm -rf /tmp/fl-names && mkdir -p /tmp/fl-names/home
for slug in documenso inbox-zero; do
  git clone -q --local \
    ~/workspace/_faultlines-testrepos/$slug /tmp/fl-names/$slug
  env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL HOME=/tmp/fl-names/home \
    .venv/bin/python -m faultline.cli scan-v2 /tmp/fl-names/$slug \
    -o /tmp/fl-names/$slug-scan.json
  .venv/bin/python eval/membership/score_names.py \
    /tmp/fl-names/$slug-scan.json eval/membership/$slug/ground-truth.yaml \
    --repo /tmp/fl-names/$slug
done
```
