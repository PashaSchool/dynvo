# Replay v2 — isolated stage experiments from recorded runs

Workstream 1 of the deterministic-foundation program. Every
`pipeline_v2` stage persists a deep copy of its exact INPUT next to
the output artifact it already wrote, and a `dynvo replay` command
re-executes any stage (or a downstream chain) with the CURRENT code —
so a hypothesis about ONE pipeline step is testable in seconds from
stored artifacts instead of a full rescan.

## On-disk contract

Per run, under `~/.faultline/logs/<slug>/<run_id>/`:

```
NN-stage-<name>-input.json[.gz]   # NEW — deep copy of the stage input
NN-stage-<name>.json              # output artifact (unchanged)
NN-stage-<name>.log               # StageLogger JSONL (unchanged)
```

* Inputs are named-state dicts (`features`, `ctx`, `bipartite_flows`,
  `scan_meta`, …) encoded by `faultline.replay.serialize` (allowlist
  `__type__` tags for pydantic models / dataclasses, sets stored
  sorted — deterministic bytes for deterministic state).
* Gzip when the serialized document exceeds 10 MB (`.json.gz`,
  `mtime=0` so identical content is byte-identical on disk).
* Kill-switch: `FAULTLINE_STAGE_INPUTS=0` disables capture. Capture
  failures are logged and NEVER fail a scan.
* Input capture adds files only — output artifacts, cache keys and
  the scan JSON are untouched (the Phase-A snapshot gate pins this).

Three *connector* steps have input-only artifacts (they write no
output artifact in a live run, but the replay chain needs their
inputs): `08-stage-pf_hotspots`, `06-stage-lineage`,
`06-stage-dual_evidence` (the latter only when its env gate is on).

### What is NOT captured (reconstructed at replay)

* `ScanContext.cache_backend` — a live handle; rebuilt via
  `get_cache_backend()`.
* `ctx.run_id` / `ctx.run_dir` — overridden to the replay run dir.
* Framework profile — re-derived deterministically via
  `select_profile(ctx)`.
* `CostTracker` / `LlmHealth` / `StageLogger` / LLM clients — fresh
  service objects.
* The product-string index (Stage 6.7/6.7b) — recomputed
  deterministically from the pinned clone.
* Incremental (`--since`) base scans — captured as a PATH reference
  and reloaded; replay targets full/cold runs first.

The identity ship-gate defines "good enough": replaying every stage
of a formbricks baseline with unchanged code reproduces every output
artifact byte-identically after normalization.

## The workflow: artifact → edit code → replay → validate downstream

```bash
# 0. Record a baseline run (input capture is always on):
./eval/scrub-faultline-cache.sh formbricks       # cache hygiene (app repo)
dynvo scan-v2 ~/workspace/_faultlines-testrepos/formbricks --run-id ws1-baseline

# 1. Edit the stage code you are experimenting on
#    (e.g. faultline/pipeline_v2/stage_5_3_sibling_collapse.py).

# 2. Replay JUST that stage from the recorded input:
dynvo replay --run formbricks/ws1-baseline --stage sibling_collapse

# 3. Chain the normal downstream stages to see the end-to-end effect:
dynvo replay --run formbricks/ws1-baseline --stage sibling_collapse --through output

# 4. Compare the new run dir against the baseline:
python - <<'EOF'
from pathlib import Path
from faultline.replay.compare import load_artifact, diff_summary
base = Path.home()/".faultline/logs/formbricks/ws1-baseline"
rep  = sorted((base.parent).glob("replay-ws1-baseline-sibling_collapse-*"))[-1]
for a in sorted(base.glob("*-stage-*.json")):
    if "-input" in a.name: continue
    t = rep/a.name
    if t.exists():
        d = diff_summary(load_artifact(a), load_artifact(t))
        if d: print(a.name, d)
EOF
```

`--stage` accepts the artifact stage name (`flows`, `uf_refiner`) or
the numbered form (`03-flows`). `--env K=V` (repeatable) applies an
environment override for the duration — the knob for gate/threshold
experiments (`--env FAULTLINE_STAGE_5_4_CROSS_FLOW_DEDUP=1`).

### Chaining semantics (`--through`)

Each downstream stage starts from ITS OWN recorded input, overlaid
with the state keys the replayed upstream stages produced. A change
in stage N therefore propagates exactly as in a live scan, while
state N does not influence stays pinned to the recorded run — deltas
appear ONLY downstream of N (the mutation ship-gate,
`tests/test_replay_mutation_formbricks.py`). `scan_meta` telemetry of
stages that were not re-run stays the recorded run's.

Presence-gated stages (`journey_abstraction`, `dual_evidence`) replay
iff their input artifact exists in the source run; mid-chain they are
skipped otherwise, and requesting one directly fails with the
artifact name. Any other missing input artifact fails loudly the same
way (old runs recorded before replay v2 are not replayable).

The replayed run lands in a NEW run dir
(`<slug>/replay-<source_run_id>-<stage>-<seq>/`) containing
`replay-meta.json`, mirrored input captures (a replay is itself
replayable), the stage output artifacts, and — when the chain reaches
Stage 7 — a feature map whose `scan_meta.replayed_from` names the
source run.

## LLM stages + `--fresh-llm`

LLM stages replay against the content-keyed llm-cache by default:
identical inputs hit the warm cache and reproduce the recorded
answers at $0. `--fresh-llm` deletes ONLY the target stage kind's
cache subdir before replaying (the align-v2
`rm -rf llm-cache/abstraction` protocol, formalized):

| stage                | llm-cache subdir       |
| -------------------- | ---------------------- |
| auditor              | `auditor`              |
| flows                | `flows`                |
| residual             | `residual`             |
| marketing_clusterer  | `product-cluster`      |
| uf_splitter          | `uf-split`             |
| uf_refiner           | `uf-refine`            |
| journey_abstraction  | `abstraction`          |
| llm_component_split  | `llm-component-split`  |

Other stages' caches are untouched. Keyless tip: with NO
`ANTHROPIC_API_KEY` at all, LLM stages short-circuit before the cache
lookup ("no Anthropic client"); to replay against the warm cache
without spending, export a dummy key (e.g.
`ANTHROPIC_API_KEY=sk-ant-replay-cache-only`) — cache hits are served,
cache misses fail auth at $0 and degrade visibly.

## Normalized comparison

`faultline.replay.compare.normalize_stage_artifact` reuses the
Phase-A scan normalizer (`faultline.tools.normalize_scan`) and applies
its volatile-key catalogue document-wide (stage artifacts carry
`elapsed_sec` / `cost_usd` / `*_sample` at arbitrary depths), plus
stage-artifact-only keys (`run_id`, `cache_hits`, `llm_calls`,
`elapsed_ms`, the `replayed_from` stamp) and Anthropic `req_…` ids
inside degradation detail strings.

## Ship gates (all green 2026-07-03)

* `tests/test_replay_serialize.py` / `tests/test_replay_capture.py`
  — round-trip + capture units.
* `tests/test_replay_identity_formbricks.py` — identity replay for
  EVERY recorded stage on the pinned formbricks clone.
* `tests/test_replay_mutation_formbricks.py` — constants of Stage 6.9
  mutated → full-chain replay → deltas only downstream.
* `python -m faultline.tools.snapshot_gate --check` — unchanged.
