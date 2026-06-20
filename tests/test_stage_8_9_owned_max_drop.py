"""Regression guard: Stage 8.9 must actually MOVE the real blob gate.

The independent blob audit (memory/finding-coldeval-blob-broken-2026-06-19
→ "Blob audit RESET 2026-06-20") flagged that the 45 existing Stage-8.9
unit tests assert ``role == "shared"`` after a split but NONE assert that
``eval/cold_eval.py:g3_blob``'s ``owned_max_feature_share`` actually
DROPS. A future regression where de-owning stops moving the metric — or
where a fully de-owned source is mis-counted as still owning its files —
would pass silently.

These tests close that hole by exercising the END-TO-END production path
through the REAL gate (not a hand-rolled owned-set reconstruction):

  1. build an oversized feature with real domain subdirs,
  2. run the REAL ``subdecompose_oversized_features`` (mutates roles +
     appends sub-features exactly as the pipeline does),
  3. serialise the mutated features to dicts (``model_dump(mode="json")``,
     the same shape the scan JSON carries),
  4. run the REAL ``cold_eval.g3_blob`` on those dicts and assert
     ``owned_max_feature_share`` STRICTLY decreases, and
  5. assert a fully de-owned source contributes 0 owned files (not its
     full member_files) — the exact ``cold_eval`` e28ef47 contract.

Synthetic, neutral fixture names only (rule-no-repo-specific-paths).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    subdecompose_oversized_features,
)

# Import the REAL blob gate from the faultlines-app eval dir (it lives in the
# app repo, not the engine repo). Skip — never error — if it is unavailable
# (e.g. CI without the app checkout); the regression guard is meaningless
# without the actual gate, and a hard import error would be a false failure.
_EVAL_DIR = os.environ.get(
    "FAULTLINE_EVAL_DIR", "/Users/pkuzina/workspace/faultlines-app/eval"
)
if _EVAL_DIR not in sys.path:
    sys.path.insert(0, _EVAL_DIR)
cold_eval = pytest.importorskip(
    "cold_eval",
    reason=(
        "real eval/cold_eval.py not importable; set FAULTLINE_EVAL_DIR to the "
        "faultlines-app/eval dir to run the blob-gate regression guard"
    ),
)


# ── fixtures (mirror tests/test_stage_8_9_anchor_subdecompose.py) ─────────

_WS = "[package] workspace anchor {0!r} from monorepo package {0!r}"


def _feat(name: str, paths, *, description=None, uuid="") -> Feature:
    return Feature(
        name=name,
        description=description,
        paths=list(paths),
        authors=[],
        total_commits=7,
        bug_fixes=1,
        bug_fix_ratio=0.1,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        layer="developer",
        uuid=uuid,
    )


def _owned_member_feat(name: str, paths, *, uuid="") -> Feature:
    """A feature that OWNS its files via ``member_files`` (role=anchor,
    primary=True) — the real engine shape ``cold_eval`` reads first."""
    f = _feat(name, paths, description=_WS.format(name), uuid=uuid)
    f.member_files = [
        MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
        for p in paths
    ]
    return f


def _peers(n: int = 6) -> list[Feature]:
    """*n* small grain-peer features (2 owned files each) → repo median 2, so a
    fat feature is genuinely oversized. They own via member_files so the gate
    metric (which reads member_files first) sees a stable owned grain."""
    return [
        _owned_member_feat(f"peer-{i}", [f"peerpkg{i}/x.ts", f"peerpkg{i}/y.ts"])
        for i in range(n)
    ]


def _scan(features: list[Feature]) -> dict:
    """The exact dict shape the scan JSON carries + cold_eval consumes."""
    return {"developer_features": [f.model_dump(mode="json") for f in features]}


def _owned_max(features: list[Feature]) -> float:
    return cold_eval.g3_blob(_scan(features))["owned_max_feature_share"]


# ── owned_max DROPS through the real gate ─────────────────────────────────


def test_owned_max_strictly_drops_after_real_split() -> None:
    # One fat anchor that owns 3 domains of 4 files each (12 owned) among
    # 8 two-file peers (median 2). It dominates the owned set before the split.
    anchor = _owned_member_feat(
        "frontend",
        [
            "modules/network/a.ts", "modules/network/b.ts",
            "modules/network/c.ts", "modules/network/d.ts",
            "modules/threats/a.ts", "modules/threats/b.ts",
            "modules/threats/c.ts", "modules/threats/d.ts",
            "modules/reporting/a.ts", "modules/reporting/b.ts",
            "modules/reporting/c.ts", "modules/reporting/d.ts",
            "lib/util.ts", "index.ts",  # residual (non-domain → shared)
        ],
        uuid="anchor",
    )
    feats = [*_peers(8), anchor]

    before = _owned_max(feats)
    res = subdecompose_oversized_features(feats)
    after = _owned_max(feats)

    # The stage must have actually fired (not a vacuous pass).
    assert res.features_split == 1
    assert res.subfeatures_created == 3
    # The REAL gate's owned_max must STRICTLY decrease — the whole point.
    assert after < before, f"owned_max did not drop: {before} -> {after}"
    # And meaningfully: the 12-file owner is gone; the biggest owner is now a
    # 4-file sub-domain, so the share roughly quarters.
    assert after <= before / 2


def test_fully_deowned_source_contributes_zero_owned_files() -> None:
    # When EVERY owned file moves into a domain, zero-path protection keeps the
    # smallest domain on the source. But the residual files it keeps as
    # role=shared must contribute 0 to owned_max — not their full member set.
    # Build a source whose residual is ALL non-domain (loose) files so they are
    # de-owned (role=shared) and the source owns only what zero-path protection
    # leaves it.
    anchor = _owned_member_feat(
        "core",
        [
            "modules/alpha/a.ts", "modules/alpha/b.ts", "modules/alpha/c.ts",
            "modules/beta/a.ts", "modules/beta/b.ts", "modules/beta/c.ts",
            "modules/gamma/a.ts", "modules/gamma/b.ts", "modules/gamma/c.ts",
            # loose, non-domain residual → de-owned to role=shared, owns nothing
            "boot.ts", "wire.ts",
        ],
        uuid="core",
    )
    feats = [*_peers(8), anchor]
    subdecompose_oversized_features(feats)

    # The source now OWNS none of its de-owned residual: its member_files that
    # remain are role=shared / primary=False, which cold_eval._owned_file_set
    # must exclude → the source's owned set is empty.
    src = next(f for f in feats if f.uuid == "core")
    src_dict = src.model_dump(mode="json")
    owned = cold_eval._owned_file_set(src_dict)
    # The loose residual files are present in member_files but NOT owned.
    member_paths = {m["path"] for m in src_dict["member_files"]}
    assert {"boot.ts", "wire.ts"} <= member_paths  # kept, not dropped
    assert "boot.ts" not in owned and "wire.ts" not in owned
    # The de-owned residual contributes 0 — owned is empty, not the full set.
    assert owned == set(), f"de-owned source still claims owned files: {owned}"


def test_owned_max_drop_holds_with_member_file_ownership_shape() -> None:
    # Belt-and-braces on the schema the gate actually reads: ownership via
    # member_files (not bare paths). Two 5-file domains + small residual.
    anchor = _owned_member_feat(
        "dashboard",
        [
            *[f"modules/billing/f{i}.ts" for i in range(5)],
            *[f"modules/audit/f{i}.ts" for i in range(5)],
            "config.ts",  # residual
        ],
        uuid="dash",
    )
    feats = [*_peers(8), anchor]
    before = _owned_max(feats)
    res = subdecompose_oversized_features(feats)
    after = _owned_max(feats)
    assert res.features_split == 1
    assert after < before
    # Sub-features OWN their domain files via member_files (so the gate moves).
    subs = {f.name: f for f in feats if f.split_from == "dash"}
    assert set(subs) == {"billing", "audit"}
    for name, sub in subs.items():
        sd = sub.model_dump(mode="json")
        owned = cold_eval._owned_file_set(sd)
        assert len(owned) == 5  # each owns exactly its 5 domain files
        assert all(f"modules/{name}/" in p for p in owned)


def test_no_split_means_no_metric_movement() -> None:
    # Negative control: a small cohesive feature (at the repo grain) is NOT
    # oversized → no split → owned_max is byte-identical before and after. This
    # guards against a future change that splits features it should not (which
    # would ALSO be caught as a spurious owned_max change here).
    small = _owned_member_feat(
        "widgets",
        ["modules/a/x.ts", "modules/a/y.ts", "modules/b/x.ts"],
        uuid="w",
    )
    feats = [*_peers(8), small]
    before = _owned_max(feats)
    res = subdecompose_oversized_features(feats)
    after = _owned_max(feats)
    assert res.features_split == 0
    assert after == before
