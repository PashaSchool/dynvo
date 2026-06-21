"""Stage 8.9 — recursion-to-fixed-point + file-conservation tests.

Covers the deep-recurse rewrite (2026-06-21):

  1. A nested oversized sub-feature is decomposed AGAIN to a fixed point
     (the depth-1 split is not the stopping point).
  2. Termination: a deliberately deep nested tree reaches a fixed point
     WITHOUT hitting the defensive iteration cap, and the cap (when
     artificially lowered) is reported via ``depth_cap_hit``.
  3. File conservation when ``paths`` is a SUPERSET of ``member_files``
     (path-only entries with no owning member row) — nothing dropped
     (the jsonhero-web 9-file-drop bug).
  4. The precision guard still holds AFTER recursion: no terminal /
     PascalCase-component sub-feature is minted at any depth.
  5. Idempotence: ``stage(stage(features)) == stage(features)`` — a
     re-run on already-decomposed output is a no-op on the source set.

Synthetic, neutral fixture names only (rule-no-repo-specific-paths).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2 import stage_8_9_anchor_subdecompose as st
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    _FIXED_POINT_ITER_CAP,
    subdecompose_oversized_features,
)

_WS = "[package] workspace anchor {0!r} from monorepo package {0!r}"
_NEW_MARK = re.compile(r"sub-domain '(.*)' of feature '")


def _owned_member_feat(name: str, paths, *, uuid="") -> Feature:
    f = Feature(
        name=name,
        description=_WS.format(name),
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
    f.member_files = [
        MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
        for p in paths
    ]
    return f


def _peers(n: int = 8) -> list[Feature]:
    """*n* two-file grain peers → repo median owned size = 2."""
    return [
        _owned_member_feat(f"peer-{i}", [f"peerpkg{i}/x.ts", f"peerpkg{i}/y.ts"])
        for i in range(n)
    ]


def _owned(f: Feature) -> set[str]:
    return set(st._owned_paths(f))


def _all_files(feats: list[Feature]) -> set[str]:
    s: set[str] = set()
    for f in feats:
        s |= {m.path for m in (f.member_files or [])}
        s |= set(f.paths or [])
    return s


def _fresh_subs(feats: list[Feature]) -> list[Feature]:
    return [f for f in feats if _NEW_MARK.search(f.description or "")]


# ── 1. recursion splits a nested oversized sub-feature ────────────────────


def _nested_oversized_anchor() -> Feature:
    """A blob whose depth-1 split yields a sub-feature that is ITSELF still
    oversized: ``mod/big`` holds two deep domains (``alpha``/``beta``) each big
    enough to re-split. Each level carries a loose NON-domain residual file
    (``*/loose.ts``) so zero-path protection never has to eat a domain — both
    domains at every level survive as their own sub-features. Depth-1 mints
    ``big`` (+ residual on the source); the fixed point then splits ``big``
    into ``alpha`` + ``beta``.
    """
    paths = []
    # mod/big/alpha/* (6) and mod/big/beta/* (6) — 'big' is a fat sub-domain
    for d in ("alpha", "beta"):
        paths += [f"mod/big/{d}/f{i}.ts" for i in range(6)]
    paths.append("mod/big/loose.ts")     # non-domain residual at the 'big' level
    # mod/other/* (4) — a second first-level domain so the top split has >=2
    paths += [f"mod/other/f{i}.ts" for i in range(4)]
    paths.append("mod/boot.ts")          # non-domain residual at the top level
    return _owned_member_feat("frontend", paths, uuid="anchor")


def test_recursion_splits_nested_oversized_subfeature() -> None:
    feats = [*_peers(8), _nested_oversized_anchor()]
    res = subdecompose_oversized_features(feats)
    # The recursion must have run more than one level.
    assert res.iterations >= 2
    names = {f.name for f in _fresh_subs(feats)}
    # depth-2 domains alpha/beta were minted — proof the 'big' sub-feature was
    # re-decomposed rather than left as a 12-file blob.
    assert {"alpha", "beta"} <= names
    # 'big' itself is fully de-owned (its files now owned by alpha/beta).
    big = next((f for f in feats if f.name == "big"), None)
    if big is not None:  # 'big' may persist only as a thin shared residual
        assert not _owned(big) & {f"mod/big/alpha/f{i}.ts" for i in range(6)}
    # the deepest owners are the depth-2 domains, each owning exactly 6 files.
    alpha = next(f for f in feats if f.name == "alpha")
    assert _owned(alpha) == {f"mod/big/alpha/f{i}.ts" for i in range(6)}


def test_recursion_reaches_fixed_point_no_oversized_remains() -> None:
    # After the stage, NO developer feature owns more than the repo's oversized
    # cut (the definition of the fixed point) — modulo zero-path residuals.
    feats = [*_peers(8), _nested_oversized_anchor()]
    subdecompose_oversized_features(feats)
    # recompute the same repo-grain cut the stage used
    import math
    import statistics
    devs = [f for f in feats if f.layer == "developer"]
    sizes = [len(_owned(f)) for f in devs if _owned(f)]
    median = max(2, int(statistics.median(sizes)))
    total = len({p for f in devs for p in _owned(f)})
    cut = max(2 * median, math.ceil(0.15 * total))
    still_oversized = [
        f.name for f in devs
        if len(_owned(f)) > cut and _NEW_MARK.search(f.description or "")
    ]
    # any remaining oversized feature must be NON-decomposable (a single big
    # domain) — none of our fixtures are, so the set is empty.
    assert still_oversized == [], f"not a fixed point: {still_oversized}"


# ── 2. termination / cap ──────────────────────────────────────────────────


def test_deep_tree_terminates_without_hitting_cap() -> None:
    # A 4-level-deep nested blob: mod/L1/L2/L3/<domain>. The recursion descends
    # several levels but the strict-descent invariant guarantees it stops well
    # before the defensive cap.
    paths = []
    for top in ("aa", "bb"):
        for sub in ("xx", "yy"):
            paths += [f"mod/{top}/{sub}/f{i}.ts" for i in range(4)]
    anchor = _owned_member_feat("core", paths, uuid="c")
    feats = [*_peers(8), anchor]
    res = subdecompose_oversized_features(feats)
    assert res.depth_cap_hit is False
    assert 1 < res.iterations < _FIXED_POINT_ITER_CAP


def test_iteration_cap_is_a_safety_bound(monkeypatch) -> None:
    # Artificially lower the cap to 1 → single-pass behaviour; the stage must
    # still terminate and (because nested blobs remain) report depth_cap_hit.
    monkeypatch.setattr(st, "_FIXED_POINT_ITER_CAP", 1)
    feats = [*_peers(8), _nested_oversized_anchor()]
    res = subdecompose_oversized_features(feats)
    assert res.iterations == 1
    # with the cap forced to 1, the still-oversized 'big' sub-feature is left
    # pending → the cap-hit flag fires (diagnostic, never silent).
    assert res.depth_cap_hit is True


def test_recursion_is_monotone_non_worse_than_single_pass() -> None:
    # The biggest single owner after full recursion must be <= the biggest
    # after a single pass (recursion only ever splits further, never merges).
    def biggest(feats: list[Feature]) -> int:
        return max((len(_owned(f)) for f in feats), default=0)

    base = [*_peers(8), _nested_oversized_anchor()]
    st._FIXED_POINT_ITER_CAP_was = st._FIXED_POINT_ITER_CAP
    import copy
    one = copy.deepcopy(base)
    # single pass
    saved = st._FIXED_POINT_ITER_CAP
    st._FIXED_POINT_ITER_CAP = 1
    try:
        subdecompose_oversized_features(one)
    finally:
        st._FIXED_POINT_ITER_CAP = saved
    full = copy.deepcopy(base)
    subdecompose_oversized_features(full)
    assert biggest(full) <= biggest(one)


# ── 3. file conservation with paths ⊋ member_files ────────────────────────


def _anchor_paths_superset_members() -> Feature:
    """Owned member rows for the domain files only; ``paths`` ADDITIONALLY
    carries path-only entries (no member row) — the jsonhero-web shape."""
    domain = (
        [f"mod/alpha/f{i}.ts" for i in range(4)]
        + [f"mod/beta/f{i}.ts" for i in range(4)]
    )
    path_only = [
        "components/Button.tsx",   # terminal subtree → residual, not a domain
        "components/Card.tsx",
        "utilities/colors.ts",     # loose util → residual
    ]
    f = Feature(
        name="frontend",
        description=_WS.format("frontend"),
        paths=[*domain, *path_only],   # SUPERSET of member_files
        authors=[],
        total_commits=7,
        bug_fixes=1,
        bug_fix_ratio=0.1,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        layer="developer",
        uuid="sup",
    )
    # member_files ONLY for the domain files → paths is a strict superset.
    f.member_files = [
        MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
        for p in domain
    ]
    return f


def test_path_only_files_are_conserved_not_dropped() -> None:
    anchor = _anchor_paths_superset_members()
    feats = [*_peers(8), anchor]
    before = _all_files(feats)
    res = subdecompose_oversized_features(feats)
    assert res.features_split == 1  # guard: not vacuous
    after = _all_files(feats)
    # NOTHING dropped, NOTHING invented.
    assert after == before, f"dropped={before - after} added={after - before}"
    # specifically the 3 path-only files survive — as SHARED members on the
    # de-owned source (they were never owned, never a domain).
    src = next(f for f in feats if f.uuid == "sup")
    by_path = {m.path: m for m in src.member_files}
    for p in ("components/Button.tsx", "components/Card.tsx", "utilities/colors.ts"):
        assert p in by_path, f"{p} dropped from member_files"
        assert by_path[p].role == "shared" and by_path[p].primary is False
        assert p in src.paths


def test_corpus_shape_total_file_count_conserved() -> None:
    # Synthetic many-feature repo with several oversized blobs + path-only
    # entries; assert the GLOBAL file universe is byte-identical pre/post (the
    # stage-wide conservation invariant the audit asked to assert).
    feats = [*_peers(10), _nested_oversized_anchor(), _anchor_paths_superset_members()]
    before = _all_files(feats)
    subdecompose_oversized_features(feats)
    after = _all_files(feats)
    assert after == before


# ── 4. precision guard holds after recursion ──────────────────────────────


def test_no_terminal_or_component_subfeature_after_recursion() -> None:
    # Build a blob mixing real deep domains (under the recognised ``modules``
    # layer so alpha/beta surface distinctly) with terminal/component subtrees
    # so the recursion has every chance to mint junk; assert it mints none.
    paths = []
    for d in ("alpha", "beta"):                        # real deep domains
        paths += [f"modules/big/{d}/f{i}.ts" for i in range(6)]
    paths.append("modules/big/loose.ts")               # residual at 'big' level
    paths += [f"modules/other/f{i}.ts" for i in range(4)]  # 2nd top domain
    paths += [f"modules/components/v2/Accordion/f{i}.ts" for i in range(8)]  # terminal
    paths += [f"public/icons/f{i}.svg" for i in range(8)]           # terminal
    paths += [f"pages/MfaSessionPage/f{i}.tsx" for i in range(8)]   # PascalCase
    paths.append("boot.ts")                            # top-level residual
    anchor = _owned_member_feat("frontend", paths, uuid="a")
    feats = [*_peers(8), anchor]
    subdecompose_oversized_features(feats)
    pascal = st._COMPONENT_NAME_RE
    for sub in _fresh_subs(feats):
        m = _NEW_MARK.search(sub.description or "")
        assert m
        leaf = st._strip_route_group(m.group(1).rsplit("/", 1)[-1])
        assert not st._is_terminal(leaf), f"terminal leaf minted: {sub.name}"
        assert not pascal.fullmatch(leaf), f"component leaf minted: {sub.name}"
    # and the real domains DID surface (recursion is not over-suppressing)
    names = {f.name for f in _fresh_subs(feats)}
    assert {"alpha", "beta"} <= names
    # the terminal/PascalCase subtrees did NOT mint their leaves
    assert not ({"accordion", "mfa-session-page", "icons", "components"} & names)


# ── 5. idempotence on re-run ───────────────────────────────────────────────


def test_stage_is_idempotent_on_already_decomposed_output() -> None:
    feats = [*_peers(8), _nested_oversized_anchor()]
    subdecompose_oversized_features(feats)
    snapshot_names = sorted(f.name for f in feats)
    snapshot_owned = {f.name: _owned(f) for f in feats}
    n_after_first = len(feats)

    # Re-run on the SAME (already-decomposed) feature list.
    res2 = subdecompose_oversized_features(feats)

    # No new features minted, no source re-split, ownership unchanged.
    assert len(feats) == n_after_first, "re-run minted new features (not idempotent)"
    assert res2.features_split == 0
    assert sorted(f.name for f in feats) == snapshot_names
    assert {f.name: _owned(f) for f in feats} == snapshot_owned
