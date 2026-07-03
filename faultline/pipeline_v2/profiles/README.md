# Framework profiles — isolation contract & two-speed CI

This package is the **Framework Knowledge Layer**: one module per
stack, selected exclusively per scan-unit, machine-proven unable to
affect any other stack. Spec: `docs/specs/stack-profile-architecture.md`
(faultlines-app repo), approved 2026-07-03.

## The four guarantees (all enforced by the test suite)

| Gate | What it proves | Enforced by |
| --- | --- | --- |
| **G1** exclusive activation | Highest `detects()` wins; ties break by profile name (lexicographic); no match → `DefaultProfile`. Selection is order-independent. | `ProfileRegistry.select` + `tests/test_framework_profile_registry.py` + per-pinned-repo fixtures in `tests/test_profile_selection_fixtures.py` |
| **G2** no cross-profile imports | A concrete profile imports only `profiles/base`, `profiles/_*` helpers, and `pipeline_v2/extractors/*`. Trunk never imports a concrete profile (registry-only access). | `tests/test_profiles_import_lint.py` (AST, no execution) |
| **G3** trunk purity | Shared stages carry NO stack conditionals (`if stack == ...`, framework literals, profile-name branching). Legacy occurrences live in a shrinking allowlist. | `tests/test_trunk_purity_lint.py` + `tests/data/trunk_purity_allowlist.json` |
| **G4** inertness | Registering a profile changes NOTHING (byte-identical normalized output + identical cache key) for repos it does not win. | template `tests/_profile_inertness.py`, wired in `tests/test_profile_inertness.py` |

## Snapshot gate

`snapshots.lock.json` pins, per profile, 1-N LOCAL corpus clones
(exact commit SHA) and the SHA256 of the **normalized deterministic-only
scan JSON** (no `ANTHROPIC_API_KEY` in the child env → every LLM stage
takes its deterministic fallback; all LLM caches off; `days` pinned to
cover the whole clone).

```bash
# full matrix (trunk changes)
python -m faultline.tools.snapshot_gate --check

# one profile's snapshot set (profile-scoped changes)
python -m faultline.tools.snapshot_gate --check --profile next-app-router

# re-pin after an INTENDED behaviour change (review the diff!)
python -m faultline.tools.snapshot_gate --update
```

Normalization (`faultline/tools/normalize_scan.py`) strips only:
run-scoped fields (`analyzed_at`, `run_id`, timings/cost, `*_sample`
debug telemetry, `stage_artifact_dir`) and the two wall-clock-decayed
health fields (`health_score`, `symbol_health_score` age-weight commits
against `now()`, so they can never be byte-stable on a frozen clone).
Everything else — features, flows, edges, indexes, routes — is in the
digest.

## Two-speed CI rule

* **PR touches only `profiles/<x>`** (one profile module + its tests):
  run G1 selection fixtures (they cover ALL pinned repos — cheap) +
  `snapshot_gate --check --profile <x>`. Every other stack is provably
  unaffected: G2 says x can't reach other profiles' code, G3 says trunk
  can't branch on x, G4 says a non-winning x is inert, and the G1
  fixtures prove x didn't steal anyone's selection.
* **PR touches trunk** (anything in `pipeline_v2/` outside `profiles/`):
  run the FULL snapshot matrix (`snapshot_gate --check`).

The gate needs the local corpus clones (paths in the lock file); boxes
without them still run G1-G4 (the selection fixtures skip per missing
clone). LLM-stage behaviour is validated separately by the keyed eval
harness (uf_score vs golden) — the snapshot gate scopes exactly what
profiles can influence: the deterministic skeleton.

## Adding a profile (Phase B checklist)

1. **One module** `profiles/<stack>.py` implementing the
   `FrameworkProfile` Protocol (`base.py` — FROZEN). Fold the stack's
   existing extractors by importing them; never rewrite, never touch
   trunk stages, never touch prompts/LLM stages.
2. **Register** in BOTH places: `_registry._load_default_profiles`
   (one `_try(...)` line) and pyproject
   `[project.entry-points."faultlines.profiles"]` (one line).
3. **`detects()` fixtures** — unit tests for the fingerprint grades
   (strong / structural / zero) with synthetic `ScanContext`s.
4. **G4 inertness test** in `tests/test_profile_inertness.py` against a
   fixture repo the profile must NOT match (template does the rest).
5. **Snapshot set** — pin 2-3 LOCAL corpus repos for the stack in
   `snapshots.lock.json` (`--update --only <slug>` after adding the
   entry with the clone path + HEAD sha + expected profile).
6. **Selection fixtures auto-extend** from the lock file — just check
   `pytest tests/test_profile_selection_fixtures.py` passes, proving
   your profile won its repos AND flipped nobody else's.
7. **Full matrix green**: `snapshot_gate --check` — every OTHER
   profile's digests must be byte-unchanged. That IS the two-speed
   gate proving itself.

Rules that still apply here (CLAUDE.md): stack knowledge is framework
*convention*, never corpus-repo paths; no README parsing; no tuned
magic numbers; profiles are deterministic — NO LLM, NO network.
