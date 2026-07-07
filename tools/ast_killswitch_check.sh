#!/bin/bash
# W6-AST kill-switch byte-identity rig (M5) — template for the integrator gate.
#
# Runs TWO keyless deterministic-only scans of the same pinned clone with
# FAULTLINE_TS_AST=<A> vs FAULTLINE_TS_AST=<B> and byte-compares the
# NORMALIZED outputs (faultline.tools.normalize_scan — same normalization
# the snapshot gate pins), plus the sha256 digests.
#
#   tools/ast_killswitch_check.sh <repo> [flagA] [flagB] [days]
#
# Defaults: flagA=0 flagB=0 days=3650.
#   * =0 vs =0  — validates the rig itself + double-run determinism law
#                 (spec §5: double-run x2 byte-identical). This is the mode
#                 M5 ships; it must PASS on main today.
#   * =0 vs =1  — the integrator's kill-switch gate once M1-M4 land: the
#                 master flag OFF must reproduce the regex path
#                 byte-identically... compare =0 output against a MAIN
#                 (pre-AST) run of this same script to prove that.
#
# Environment scrub mirrors faultline.tools.snapshot_gate (_ENV_STRIP +
# _ENV_SET): no API key, no LLM caches, scan-result cache bypassed, stage
# budgets pinned high, fresh FAULTLINES_RUN_DIR per run. $0, no network.
# bash-3.2 compatible (macOS /bin/bash): no assoc arrays, no ${var,,}.

set -eu

usage() {
  echo "usage: $0 <repo_path> [flagA=0] [flagB=0] [days=3650]" >&2
  exit 2
}

[ $# -ge 1 ] || usage
REPO=$1
FLAG_A=${2:-0}
FLAG_B=${3:-0}
DAYS=${4:-3650}

[ -d "$REPO" ] || { echo "error: repo not a directory: $REPO" >&2; exit 2; }

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ENGINE_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
PY=${FAULTLINE_PY:-$ENGINE_ROOT/.venv/bin/python}
if [ ! -x "$PY" ]; then
  PY=$(command -v python3) || { echo "error: no python3" >&2; exit 2; }
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/ast-killswitch.XXXXXX")
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

# One keyless deterministic scan: $1=flag-value $2=out.json $3=state-dir
run_scan() {
  mkdir -p "$3"
  env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL \
    FAULTLINES_CACHE_BACKEND=fs \
    FAULTLINE_SCAN_CACHE=0 \
    FAULTLINE_SCAN_CACHE_BYPASS=1 \
    FAULTLINE_STAGE_0_5_CACHE=0 \
    FAULTLINE_STAGE_6_7B_CACHE=0 \
    FAULTLINE_STAGE_6_7C_CACHE=0 \
    FAULTLINE_STAGE_8_CACHE=0 \
    FAULTLINE_STAGE_6_3_BUDGET_SEC=100000 \
    FAULTLINE_STAGE_6_4_BUDGET_SEC=100000 \
    FAULTLINE_STAGE_6_6_BUDGET_SEC=100000 \
    FAULTLINE_IMPACT_BUDGET_SEC=100000 \
    FAULTLINES_RUN_DIR="$3" \
    FAULTLINE_TS_AST="$1" \
    "$PY" -m faultline.tools.snapshot_gate \
      --scan-one "$REPO" --days "$DAYS" --out "$2"
}

# Normalize + digest: $1=in.json $2=out.normalized.json  (prints digest)
normalize() {
  "$PY" - "$1" "$2" <<'PYEOF'
import json, sys
from faultline.tools.normalize_scan import canonical_json, normalize_scan, scan_digest
doc = json.loads(open(sys.argv[1], encoding="utf-8").read())
open(sys.argv[2], "w", encoding="utf-8").write(canonical_json(normalize_scan(doc)))
print(scan_digest(doc))
PYEOF
}

echo "== ast_killswitch_check: repo=$REPO FAULTLINE_TS_AST=$FLAG_A vs =$FLAG_B days=$DAYS"
cd "$ENGINE_ROOT"

echo "-- run A (FAULTLINE_TS_AST=$FLAG_A)"
run_scan "$FLAG_A" "$WORK/scan-A.json" "$WORK/state-A"
echo "-- run B (FAULTLINE_TS_AST=$FLAG_B)"
run_scan "$FLAG_B" "$WORK/scan-B.json" "$WORK/state-B"

DIGEST_A=$(normalize "$WORK/scan-A.json" "$WORK/norm-A.json")
DIGEST_B=$(normalize "$WORK/scan-B.json" "$WORK/norm-B.json")
echo "digest A: $DIGEST_A"
echo "digest B: $DIGEST_B"

if cmp -s "$WORK/norm-A.json" "$WORK/norm-B.json"; then
  echo "PASS: normalized outputs byte-identical (FAULTLINE_TS_AST=$FLAG_A vs =$FLAG_B)"
  exit 0
fi

echo "FAIL: normalized outputs differ (FAULTLINE_TS_AST=$FLAG_A vs =$FLAG_B)" >&2
KEEP="${TMPDIR:-/tmp}/ast-killswitch-diff.$$"
mkdir -p "$KEEP"
cp "$WORK/norm-A.json" "$WORK/norm-B.json" "$KEEP/"
echo "normalized outputs kept for forensics: $KEEP" >&2
exit 1
