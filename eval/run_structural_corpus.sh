#!/usr/bin/env bash
# Keyless-scan a set of corpus repos and run the golden-free structural audit
# over the results, producing (or comparing against) a baseline.
#
# Keyless = $0, deterministic (no LLM): the blob/concentration metrics live in
# the deterministic Stage-1 extractors + membership, so they reproduce exactly
# run-to-run — ideal for a regression gate on attribution changes.
#
#   eval/run_structural_corpus.sh --baseline           # write eval/STRUCTURAL-BASELINE.json
#   eval/run_structural_corpus.sh --compare            # fail on regression vs the baseline
#
# Repos are read-only clones under $CORPUS; each is copied to an isolated temp
# tree + scanned with a throwaway $HOME so no ~/.faultline state leaks in.
set -euo pipefail

CORPUS="${FAULTLINES_CORPUS:-$HOME/workspace/_faultlines-testrepos}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mktemp -d)/scans"
mkdir -p "$OUT"
BASELINE="$HERE/eval/STRUCTURAL-BASELINE.json"

# Representative subset: TS monorepos (backend+frontend blob shape), Python
# layered backends, a Go service, and a pure library as a low-concentration
# control. Extend freely — the harness is repo-agnostic.
REPOS=(infisical documenso inbox-zero dify fastapi gin axios)

scan_one() {
  local repo="$1"
  local src="$CORPUS/$repo"
  [ -d "$src" ] || { echo "  skip $repo (not in corpus)"; return 0; }
  local work; work="$(mktemp -d)"
  cp -R "$src" "$work/repo" 2>/dev/null || return 0
  local iso; iso="$(mktemp -d)"
  echo "  scanning $repo (keyless)…"
  env HOME="$iso" ANTHROPIC_API_KEY= "$HERE/.venv/bin/python" -m faultline.cli scan-v2 \
      "$work/repo" -o "$OUT/$repo.json" >/dev/null 2>&1 || echo "    (scan-v2 nonzero for $repo)"
  rm -rf "$work" "$iso"
}

echo "Keyless corpus scan → $OUT"
for r in "${REPOS[@]}"; do scan_one "$r"; done

shopt -s nullglob
SCANS=("$OUT"/*.json)
[ ${#SCANS[@]} -gt 0 ] || { echo "no scans produced"; exit 1; }

case "${1:---print}" in
  --baseline)
    "$HERE/.venv/bin/python" -m eval.structural_audit "${SCANS[@]}" --json "$BASELINE"
    echo "baseline written → $BASELINE" ;;
  --compare)
    "$HERE/.venv/bin/python" -m eval.structural_audit "${SCANS[@]}" --compare "$BASELINE" ;;
  *)
    "$HERE/.venv/bin/python" -m eval.structural_audit "${SCANS[@]}" ;;
esac
