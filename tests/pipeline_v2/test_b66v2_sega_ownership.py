"""B66-v2 Seg A — module-subtree ownership for entry-mints.

EXHIBIT: hoppscotch — 12 backend route-anchors (team, user, team-invitation…)
each carry the SAME whole-backend member bag, so one fan-in winner takes every
file and the other ~11 collapse to loc=0 (the operator's trust bug: a
member-ful row that shows 0 LOC). Here two sibling route anchors (``team`` +
``user``) share one bag; ``team`` wins the slug tiebreak and would hoard
``user``'s module under OFF. Seg A gives each dev exclusive OWNED loc for its
own module subtree (the directory of its anchor). Attribution only:
membership / journeys untouched; each file keeps exactly one primary owner.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.ownership_v2 import OWNERSHIP_V2_ENV
from faultline.pipeline_v2.stage_6_97_feature_loc import apply_feature_loc

_BW = "packages/backend/src"

_ALL_BACKEND = [
    f"{_BW}/team/team.resolver.ts",
    f"{_BW}/team/team.service.ts",
    f"{_BW}/user/user.resolver.ts",
    f"{_BW}/user/user.service.ts",
    f"{_BW}/lib/util.ts",
]


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def _seg_a_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, f"{_BW}/team/team.resolver.ts", "const a = 1;\nconst b = 2;\nconst c = 3;\n")
    _write(root, f"{_BW}/team/team.service.ts", "const a = 1;\nconst b = 2;\n")
    _write(root, f"{_BW}/user/user.resolver.ts", "const a = 1;\nconst b = 2;\nconst c = 3;\nconst d = 4;\n")
    _write(root, f"{_BW}/user/user.service.ts", "const a = 1;\nconst b = 2;\nconst c = 3;\nconst d = 4;\nconst e = 5;\n")
    _write(root, f"{_BW}/lib/util.ts", "const a = 1;\n")
    return root


def _dev(name: str, anchor: str) -> Feature:
    """A route-anchor dev whose member bag is the WHOLE backend subtree (the
    hoppscotch fan-in shape): one anchor member in its own module, rest shared."""
    mfs = [MemberFile(path=anchor, role="anchor", confidence=1.0, primary=True)] + [
        MemberFile(path=p, role="shared", confidence=0.3)
        for p in _ALL_BACKEND
        if p != anchor
    ]
    return Feature(
        name=name,
        paths=list(_ALL_BACKEND),
        member_files=mfs,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        health_score=100.0,
    )


def _run_seg_a(tmp_path: Path):
    root = _seg_a_repo(tmp_path)
    team = _dev("team", f"{_BW}/team/team.resolver.ts")
    user = _dev("user", f"{_BW}/user/user.resolver.ts")
    apply_feature_loc([team, user], [], root)
    return team, user


def test_sega_off_collapses_sibling_to_zero_loc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disease, reproduced: OFF, the slug-tiebreak winner (``team``) hoards
    every file and ``user`` is a member-ful row with loc=0."""
    monkeypatch.delenv(OWNERSHIP_V2_ENV, raising=False)
    team, user = _run_seg_a(tmp_path)
    assert user.loc == 0
    assert team.loc == 3 + 2 + 4 + 5 + 1  # every backend file


def test_sega_on_each_dev_owns_its_module_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ON: ``user`` recovers OWNED loc for its own ``src/user/**`` module and
    ``team`` no longer annexes it — the operator's DRIVER."""
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")
    team, user = _run_seg_a(tmp_path)
    assert user.loc == 4 + 5  # src/user/** owned by user
    assert team.loc == 3 + 2 + 1  # src/team/** + the cross-module lib (tiebreak)
    assert user.loc > 0 and team.loc > 0


def test_sega_conservation_each_file_counted_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SACRED: total owned LOC conserved (each file has exactly one primary
    owner) — ON only REDISTRIBUTES, never invents/drops lines."""
    monkeypatch.delenv(OWNERSHIP_V2_ENV, raising=False)
    off_team, off_user = _run_seg_a(tmp_path)
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")
    on_team, on_user = _run_seg_a(tmp_path)
    total = 3 + 2 + 4 + 5 + 1
    assert off_team.loc + off_user.loc == total
    assert on_team.loc + on_user.loc == total


def test_sega_shared_lib_stays_single_owned_not_double_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SACRED anti-case: a genuine cross-module shared file (``src/lib/util.ts``,
    outside every module dir) is NOT owned by both siblings — one deterministic
    owner. Module ownership never double-counts."""
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")
    team, user = _run_seg_a(tmp_path)
    assert (team.loc - (3 + 2)) + (user.loc - (4 + 5)) == 1


def test_sega_membership_and_member_files_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SACRED: ownership is an ATTRIBUTION layer — it must not change any
    feature's membership (``paths`` / ``member_files`` set)."""
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")
    team, user = _run_seg_a(tmp_path)
    assert set(team.paths) == set(_ALL_BACKEND)
    assert set(user.paths) == set(_ALL_BACKEND)
    assert {m.path for m in team.member_files} == set(_ALL_BACKEND)
    assert {m.path for m in user.member_files} == set(_ALL_BACKEND)


def test_sega_dev_without_anchor_member_falls_back_to_tiebreak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dev with NO anchor member has no module root, so the rung is a no-op
    for it and the ordinary tiebreak decides (no crash, no phantom ownership)."""
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")
    root = _seg_a_repo(tmp_path)
    team = _dev("team", f"{_BW}/team/team.resolver.ts")
    plain = Feature(
        name="plain",
        paths=list(_ALL_BACKEND),
        member_files=[MemberFile(path=p, role="shared", confidence=0.3) for p in _ALL_BACKEND],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        health_score=100.0,
    )
    apply_feature_loc([team, plain], [], root)
    assert team.loc >= 3 + 2
    assert team.loc + plain.loc == 3 + 2 + 4 + 5 + 1
