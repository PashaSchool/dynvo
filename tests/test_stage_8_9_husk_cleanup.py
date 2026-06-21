"""Stage 8.9 husk-cleanup (step 6) — the degenerate-husk fix.

The merged decomposer de-owns a feature's domain files to sub-features but
left the residual (sub-floor / scaffold) on the source as ``role="shared"``,
producing a DEGENERATE HUSK: a feature that OWNS 0 files yet LISTS hundreds as
shared (inbox-zero ``inbox-zero-ai``: 1701 members, all shared, owns 0). Those
shared listings double-count files already owned by the sub-features and inflate
a naive all-files ``max_feature_share``.

``_cleanup_husks`` runs once after the fixed-point loop and is STRICTLY
SUBTRACTIVE on each husk (memory/rule-stage-8-fixes-must-be-additive +
memory/project-blob-decomposer-shipped-2026-06-21's audited "fully de-owned
source owns 0" invariant):

  1. de-dup    — remove husk members claimed by ANY other feature (conserved
                 there; pure double-count on the husk);
  2. drop      — a husk whose every member is conserved elsewhere is an empty
                 shell → removed (n_features drops);
  3. keep      — a husk that solely-holds files keeps them as the de-duped
                 0-owned shared residual (cannot drop without losing them,
                 cannot own without re-blobbing owned_max).

These tests assert: de-owned-to-sub file is not on the residual; a residual that
solely-holds loose files survives (de-duped, conserved); a fully-conserved husk
is dropped; file conservation at the scan level (0 lost); owned_max is never
moved by the cleanup; and no degenerate husk that is PURELY a double-count
survives. Synthetic, neutral fixture names only (rule-no-repo-specific-paths).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    _owns_zero_files,
    subdecompose_oversized_features,
)


# ── fixtures (mirror the sibling Stage-8.9 test files) ────────────────────


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


def _owned(name: str, paths, *, uuid="") -> Feature:
    """A feature OWNING its files via member_files (role=anchor, primary)."""
    f = _feat(name, paths, uuid=uuid)
    f.member_files = [
        MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
        for p in paths
    ]
    return f


def _peers(n: int = 8) -> list[Feature]:
    """*n* two-file grain peers → repo median owned size 2."""
    return [
        _owned(f"peer-{i}", [f"peerpkg{i}/x.ts", f"peerpkg{i}/y.ts"])
        for i in range(n)
    ]


def _owned_member_paths(f: Feature) -> set[str]:
    return {
        m.path for m in (f.member_files or [])
        if m.primary or m.role in ("anchor", "owner")
    }


def _all_member_paths(f: Feature) -> set[str]:
    return {m.path for m in (f.member_files or [])}


def _scan_attributed_files(features: list[Feature]) -> set[str]:
    """Every file attributed to SOME feature (any role) — the conservation
    universe at the scan level."""
    out: set[str] = set()
    for f in features:
        out |= _all_member_paths(f)
        out |= set(f.paths or [])
    return out


# ── 1. a de-owned-to-sub file is NOT on the residual ──────────────────────


def test_deowned_to_sub_file_absent_from_residual() -> None:
    # core owns 3 domains (4 files each) + 2 loose root files. The domain files
    # move to sub-features; the residual (loose root) stays on core.
    core = _owned(
        "core",
        [
            "modules/alpha/a.ts", "modules/alpha/b.ts",
            "modules/alpha/c.ts", "modules/alpha/d.ts",
            "modules/beta/a.ts", "modules/beta/b.ts",
            "modules/beta/c.ts", "modules/beta/d.ts",
            "modules/gamma/a.ts", "modules/gamma/b.ts",
            "modules/gamma/c.ts", "modules/gamma/d.ts",
            "boot.ts", "wire.ts",
        ],
        uuid="core",
    )
    feats = [*_peers(8), core]
    subdecompose_oversized_features(feats)

    src = next(f for f in feats if f.uuid == "core")
    src_members = _all_member_paths(src)
    # The domain files moved to sub-features → they are OWNED by a sub, so the
    # cleanup de-dups them off the residual: none remain on core.
    moved = {
        "modules/alpha/a.ts", "modules/beta/a.ts", "modules/gamma/a.ts",
    }
    assert not (moved & src_members), (
        f"de-owned-to-sub files must not remain on the residual: "
        f"{moved & src_members}"
    )
    # A sub-feature owns each moved file.
    subs = [f for f in feats if "sub-domain" in (f.description or "")]
    sub_owned = set().union(*(_owned_member_paths(s) for s in subs))
    assert moved <= sub_owned


# ── 2. residual solely-holding loose files survives (de-duped, conserved) ─


def test_residual_owning_loose_files_survives() -> None:
    core = _owned(
        "core",
        [
            "modules/alpha/a.ts", "modules/alpha/b.ts", "modules/alpha/c.ts",
            "modules/beta/a.ts", "modules/beta/b.ts", "modules/beta/c.ts",
            "modules/gamma/a.ts", "modules/gamma/b.ts", "modules/gamma/c.ts",
            # loose root residual — held by no other feature → sole-held
            "boot.ts", "wire.ts",
        ],
        uuid="core",
    )
    feats = [*_peers(8), core]
    subdecompose_oversized_features(feats)

    # core survives (its loose residual is sole-held) and still LISTS boot/wire.
    src = next((f for f in feats if f.uuid == "core"), None)
    assert src is not None, "a residual that solely-holds files must survive"
    assert {"boot.ts", "wire.ts"} <= _all_member_paths(src)
    # It is a de-duped 0-owned shared residual (audited invariant: owns 0).
    assert _owns_zero_files(src)


# ── 3. a fully-conserved husk (every member owned elsewhere) is DROPPED ────


def test_fully_conserved_husk_is_dropped() -> None:
    # core's residual files are ALSO members of a peer (claimed elsewhere), so
    # after de-dup the husk holds nothing unique → it is dropped.
    shared_a, shared_b = "common/x.ts", "common/y.ts"
    core = _owned(
        "core",
        [
            "modules/alpha/a.ts", "modules/alpha/b.ts", "modules/alpha/c.ts",
            "modules/beta/a.ts", "modules/beta/b.ts", "modules/beta/c.ts",
            "modules/gamma/a.ts", "modules/gamma/b.ts", "modules/gamma/c.ts",
            shared_a, shared_b,
        ],
        uuid="core",
    )
    # A peer that also claims the residual files (so they are conserved).
    keeper = _owned("keeper", [shared_a, shared_b, "keeperpkg/z.ts"], uuid="keeper")
    feats = [*_peers(8), keeper, core]
    res = subdecompose_oversized_features(feats)

    names = {f.uuid for f in feats}
    assert "core" not in names, "a fully-conserved husk must be dropped"
    assert "keeper" in names
    assert res.husks_dropped >= 1
    # The residual files are STILL attributed (on keeper) — nothing lost.
    keeper_now = next(f for f in feats if f.uuid == "keeper")
    assert {shared_a, shared_b} <= _all_member_paths(keeper_now)


# ── 4. file conservation at the scan level (0 files lost) ─────────────────


def test_file_conservation_across_cleanup() -> None:
    core = _owned(
        "core",
        [
            "modules/alpha/a.ts", "modules/alpha/b.ts", "modules/alpha/c.ts",
            "modules/beta/a.ts", "modules/beta/b.ts", "modules/beta/c.ts",
            "modules/gamma/a.ts", "modules/gamma/b.ts", "modules/gamma/c.ts",
            "utils/helper.ts", "components/Button.tsx", "boot.ts",
        ],
        uuid="core",
    )
    # A peer that shares one of core's scaffold files (exercises de-dup).
    keeper = _owned("keeper", ["utils/helper.ts", "keeperpkg/z.ts"], uuid="keeper")
    feats = [*_peers(8), keeper, core]

    before = _scan_attributed_files(feats)
    subdecompose_oversized_features(feats)
    after = _scan_attributed_files(feats)

    assert after == before, (
        f"conservation broken: dropped={sorted(before - after)} "
        f"added={sorted(after - before)}"
    )


# ── 5. de-dup removes the double-count from the husk ──────────────────────


def test_dedup_removes_double_counted_members() -> None:
    shared = "utils/shared.ts"
    core = _owned(
        "core",
        [
            "modules/alpha/a.ts", "modules/alpha/b.ts", "modules/alpha/c.ts",
            "modules/beta/a.ts", "modules/beta/b.ts", "modules/beta/c.ts",
            "modules/gamma/a.ts", "modules/gamma/b.ts", "modules/gamma/c.ts",
            shared, "boot.ts",  # boot.ts sole-held; shared also on keeper
        ],
        uuid="core",
    )
    keeper = _owned("keeper", [shared, "keeperpkg/z.ts"], uuid="keeper")
    feats = [*_peers(8), keeper, core]
    res = subdecompose_oversized_features(feats)

    src = next(f for f in feats if f.uuid == "core")
    # The double-counted file is removed from the husk (it lives on keeper).
    assert shared not in _all_member_paths(src)
    # The sole-held loose file is retained.
    assert "boot.ts" in _all_member_paths(src)
    assert res.husk_members_deduped >= 1


# ── 6. the cleanup never moves owned_max (no residual file becomes owned) ──


def test_cleanup_never_promotes_residual_to_owned() -> None:
    core = _owned(
        "core",
        [
            "modules/alpha/a.ts", "modules/alpha/b.ts", "modules/alpha/c.ts",
            "modules/beta/a.ts", "modules/beta/b.ts", "modules/beta/c.ts",
            "modules/gamma/a.ts", "modules/gamma/b.ts", "modules/gamma/c.ts",
            "boot.ts", "wire.ts", "setup.ts",  # all loose root, sole-held
        ],
        uuid="core",
    )
    feats = [*_peers(8), core]
    subdecompose_oversized_features(feats)

    src = next((f for f in feats if f.uuid == "core"), None)
    assert src is not None
    # NOT ONE residual file is owned — the audited "fully de-owned → owns 0"
    # invariant is preserved (the cleanup is strictly subtractive).
    assert _owned_member_paths(src) == set()
    assert _owns_zero_files(src)


# ── 7. no PURE-double-count husk survives (only genuine residuals remain) ──


def test_no_pure_doublecount_husk_remains() -> None:
    # A husk whose residual is ENTIRELY claimed elsewhere must be gone; only
    # husks that solely-hold ≥1 file may remain (and those are honest residuals).
    s1, s2, s3 = "common/a.ts", "common/b.ts", "common/c.ts"
    core = _owned(
        "core",
        [
            "modules/alpha/a.ts", "modules/alpha/b.ts", "modules/alpha/c.ts",
            "modules/beta/a.ts", "modules/beta/b.ts", "modules/beta/c.ts",
            "modules/gamma/a.ts", "modules/gamma/b.ts", "modules/gamma/c.ts",
            s1, s2, s3,
        ],
        uuid="core",
    )
    keeper = _owned("keeper", [s1, s2, s3, "keeperpkg/z.ts"], uuid="keeper")
    feats = [*_peers(8), keeper, core]
    subdecompose_oversized_features(feats)

    # Every surviving 0-owned feature that this stage produced must STILL hold
    # at least one file no other feature claims (a real residual), never a pure
    # double-count.
    for f in feats:
        if not _owns_zero_files(f):
            continue
        mine = _all_member_paths(f)
        if not mine:
            continue
        others: set[str] = set()
        for g in feats:
            if g is f:
                continue
            others |= _all_member_paths(g)
        assert mine - others, (
            f"feature {f.name!r} is a pure double-count husk (every member "
            f"conserved elsewhere) and should have been dropped"
        )


# ── 8. clean input (no husk produced) is left untouched ───────────────────


def test_no_husk_input_is_noop_for_cleanup() -> None:
    # No oversized feature → nothing splits → no husk → cleanup is a no-op.
    feats = _peers(6)
    before = [f.model_dump(mode="json") for f in feats]
    res = subdecompose_oversized_features(feats)
    after = [f.model_dump(mode="json") for f in feats]
    assert res.husks_total == 0
    assert res.husks_dropped == 0
    assert res.husks_kept_residual == 0
    assert before == after


# ── 9. end-to-end through the REAL blob gate (no degenerate husk inflation) ─

_EVAL_DIR = os.environ.get(
    "FAULTLINE_EVAL_DIR", "/Users/pkuzina/workspace/faultlines-app/eval"
)
if _EVAL_DIR not in sys.path:
    sys.path.insert(0, _EVAL_DIR)
cold_eval = pytest.importorskip(
    "cold_eval",
    reason=(
        "real eval/cold_eval.py not importable; set FAULTLINE_EVAL_DIR to the "
        "faultlines-app/eval dir to run the blob-gate end-to-end guard"
    ),
)


def test_no_zero_owned_doublecount_feature_in_gate_view() -> None:
    # Build a workspace anchor whose residual is mostly scaffold ALSO claimed by
    # peers (double-count) + a couple sole-held loose files. After the stage the
    # gate must NOT see a 0-owned feature whose member list is purely a
    # double-count of files owned elsewhere.
    anchor = _owned(
        "web",
        [
            "modules/network/a.ts", "modules/network/b.ts",
            "modules/network/c.ts", "modules/network/d.ts",
            "modules/threats/a.ts", "modules/threats/b.ts",
            "modules/threats/c.ts", "modules/threats/d.ts",
            "modules/reporting/a.ts", "modules/reporting/b.ts",
            "modules/reporting/c.ts", "modules/reporting/d.ts",
            "utils/u1.ts", "utils/u2.ts",  # also on a peer (double-count)
            "boot.ts",  # sole-held loose
        ],
        uuid="web",
    )
    peer = _owned("shared-utils", ["utils/u1.ts", "utils/u2.ts", "sp/z.ts"])
    feats = [*_peers(8), peer, anchor]

    before_files = _scan_attributed_files(feats)
    subdecompose_oversized_features(feats)
    after_files = _scan_attributed_files(feats)
    # Conservation at the scan level.
    assert after_files == before_files

    scan = {"developer_features": [f.model_dump(mode="json") for f in feats]}
    g3 = cold_eval.g3_blob(scan)
    # The all-files biggest must be a REAL feature (one that owns ≥1 file), not
    # a husk. The gate already excludes 0-owned features from candidacy; this
    # asserts the engine did not leave a pure-double-count phantom that would
    # have been the biggest under a naive (no-husk-skip) all-files share.
    biggest = g3["biggest"]
    if biggest is not None:
        match = next(f for f in feats if f.name == biggest)
        assert _owned_member_paths(match), (
            f"all-files biggest {biggest!r} owns nothing — a degenerate husk "
            f"leaked into the blob candidacy"
        )
    # The double-counted utils files stay attributed on the peer.
    peer_now = next(f for f in feats if f.name == "shared-utils")
    assert {"utils/u1.ts", "utils/u2.ts"} <= _all_member_paths(peer_now)
