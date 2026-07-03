"""G1 — profile-selection fixtures for every pinned snapshot repo.

The StackProfile spec's exclusive-activation guarantee: a new profile
can affect an existing repo ONLY by winning its selection. These
fixtures freeze which profile each pinned corpus repo selects, so a new
profile that flips any existing repo's selection fails the suite loudly
instead of silently re-routing the repo through different structural
knowledge.

Parametrized straight off ``profiles/snapshots.lock.json`` — pinning a
new snapshot repo automatically pins its selection fixture too. Repos
are LOCAL clones (not vendored); missing clones skip (CI boxes without
the corpus still run every other G1 test).

Selection is exercised at the same seam the orchestrator uses
(``stage_0_intake`` → ``select_profile``); the snapshot gate
additionally asserts the END-TO-END selection (``scan_meta.
framework_profile``) on every gate run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.pipeline_v2.profiles import select_profile
from faultline.pipeline_v2.stage_0_intake import stage_0_intake

_LOCK = (
    Path(__file__).resolve().parents[1]
    / "faultline"
    / "pipeline_v2"
    / "profiles"
    / "snapshots.lock.json"
)


def _pins() -> list[pytest.param]:
    lock = json.loads(_LOCK.read_text(encoding="utf-8"))
    return [
        pytest.param(pin["path"], pin["profile"], id=slug)
        for slug, pin in sorted(lock["repos"].items())
    ]


@pytest.mark.parametrize(("repo_path", "expected_profile"), _pins())
def test_pinned_repo_selects_expected_profile(
    repo_path: str, expected_profile: str
) -> None:
    repo = Path(repo_path)
    if not repo.is_dir():
        pytest.skip(f"pinned corpus clone not present: {repo}")
    # days=7 keeps the git pass cheap — selection depends on the file
    # tree + manifests (stack / workspaces), never on history depth.
    ctx = stage_0_intake(repo, days=7)
    assert select_profile(ctx).name == expected_profile


def test_lock_file_shape() -> None:
    """The lock file itself is part of the contract — keep it sane."""
    lock = json.loads(_LOCK.read_text(encoding="utf-8"))
    assert lock["version"] == 1
    assert int(lock["scan_config"]["days"]) >= 3650, (
        "snapshot days must cover the whole clone — a sliding window "
        "rots digests as commits age out of range"
    )
    for slug, pin in lock["repos"].items():
        assert set(pin) >= {"path", "commit_sha", "profile", "digest"}, slug
        assert len(pin["commit_sha"]) == 40, f"{slug}: pin full SHAs"
        assert pin["digest"].startswith("sha256:"), slug
