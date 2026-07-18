"""S1 owner-oracle — the single deterministic file→owner election.

Named exhibits are lifted verbatim from the 2026-07-18 census probe
(/private/tmp/s1-probe/results.json) — the three files whose owner the
first-claimant rules (R1 path_index / R2 build_file_pf_owner) got WRONG
relative to the Stage 6.97 election:

  * cal.com  ``apps/api/v2/src/ee/bookings/.../bookings.service.ts`` (1161 LOC)
    — R1 gave it ``attribute-sync`` (trpc, first-claimant); the election
    gives it ``bookings-excav`` (its own bookings module subtree).
  * novu     ``libs/dal/src/repositories/message/message.repository.ts``
    — R1 gave it ``message-template``; the election gives it ``message``.
  * documenso ``packages/trpc/server/template-router/router.ts`` (694 LOC)
    — R1 gave it ``organisation``; the election gives it ``template-router``.
    Both owners carry pfid=None, so the COVERAGE VIEW (R2) is None either way
    — the exhibit proves owner=template-router in path_index while the
    conservation-context visibility is None.

Anti-cases (SACRED negative controls):
  * order-shuffle — permuting the features list flips the OFF first-claimant
    owner (the disease) but leaves the ON election owner INVARIANT.
  * PYTHONHASHSEED 0/1 — the pure election is hash-seed independent.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.conservation import build_file_pf_owner
from faultline.pipeline_v2.indexes import build_path_index
from faultline.pipeline_v2.owner_oracle import (
    OWNER_ORACLE_ENV,
    build_owner_election,
    elect_primary_owners,
    owner_oracle_enabled,
)
from faultline.pipeline_v2.ownership_v2 import OWNERSHIP_V2_ENV

_U = {name: f"{i:x}" * 32 for i, name in enumerate(
    ["attribute-sync", "bookings-excav", "message-template", "message",
     "organisation", "template-router", "alpha", "zeta"], start=1)}


def _write(root: Path, rel: str, lines: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"const v{i} = {i};\n" for i in range(lines)))


def _dev(
    name: str,
    paths: list[str],
    anchors: list[str],
    *,
    pfid: str | None = None,
    role: str | None = None,
) -> Feature:
    return Feature(
        name=name,
        uuid=_U[name],
        paths=list(paths),
        product_feature_id=pfid,
        role=role,
        member_files=[
            MemberFile(path=a, role="anchor", confidence=1.0, primary=True)
            for a in anchors
        ],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        health_score=100.0,
    )


# ── Exhibit A — cal.com bookings.service.ts (module-subtree-override) ─────


def test_exhibit_cal_bookings_service_election_owner_is_bookings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The election gives ``bookings.service.ts`` to ``bookings-excav`` (its
    own module subtree) — NOT the first-listed ``attribute-sync``."""
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")  # module-subtree rung armed
    root = tmp_path / "cal"
    f = "apps/api/v2/src/ee/bookings/2024-08-13/services/bookings.service.ts"
    _write(root, f, 40)
    attr = _dev(
        "attribute-sync", [f], ["packages/trpc/server/routers.ts"], pfid="trpc",
    )
    book = _dev(
        "bookings-excav", [f],
        ["apps/api/v2/src/ee/bookings/2024-08-13/services/output.service.ts"],
        pfid="bookings",
    )
    # features-list order puts attribute-sync FIRST (the first-claimant trap).
    election = build_owner_election([attr, book], root)
    assert election.owner_uuid(f) == _U["bookings-excav"]
    # R1 path_index override → elected owner, NOT the first-claimant.
    idx = build_path_index(
        [{"uuid": attr.uuid, "paths": [f]}, {"uuid": book.uuid, "paths": [f]}],
        [], file_owner=election.file_owner_uuid_map(),
    )
    assert idx[f]["feature_uuid"] == _U["bookings-excav"]
    # OFF (no override) reproduces the disease: first-claimant wins.
    idx_off = build_path_index(
        [{"uuid": attr.uuid, "paths": [f]}, {"uuid": book.uuid, "paths": [f]}], [],
    )
    assert idx_off[f]["feature_uuid"] == _U["attribute-sync"]
    # R2 coverage view → bookings-excav's real pfid.
    assert election.owner_pfid(f, frozenset({"trpc", "bookings"})) == "bookings"


# ── Exhibit B — novu message.repository.ts (module-subtree-override) ──────


def test_exhibit_novu_message_repository_election_owner_is_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")
    root = tmp_path / "novu"
    f = "libs/dal/src/repositories/message/message.repository.ts"
    _write(root, f, 30)
    tmpl = _dev(
        "message-template", [f],
        ["libs/dal/src/repositories/message-template/message-template.entity.ts"],
    )
    msg = _dev(
        "message", [f],
        ["libs/dal/src/repositories/message/message.entity.ts"],
    )
    election = build_owner_election([tmpl, msg], root)
    assert election.owner_uuid(f) == _U["message"]


# ── Exhibit C — documenso template-router.ts (coverage-view None) ─────────


def test_exhibit_documenso_template_router_owner_and_coverage_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """path_index owner = template-router; conservation coverage-view = None
    (both owners carry pfid=None) — the exhibit's dual assertion."""
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "1")
    root = tmp_path / "documenso"
    f = "packages/trpc/server/template-router/router.ts"
    _write(root, f, 50)
    org = _dev("organisation", [f], ["packages/lib/organisation/index.ts"])
    tr = _dev(
        "template-router", [f],
        ["packages/trpc/server/template-router/schema.ts"],
    )
    election = build_owner_election([org, tr], root)
    # path_index: elected owner is template-router (not first-listed org).
    assert election.owner_uuid(f) == _U["template-router"]
    idx = build_path_index(
        [{"uuid": org.uuid, "paths": [f]}, {"uuid": tr.uuid, "paths": [f]}],
        [], file_owner=election.file_owner_uuid_map(),
    )
    assert idx[f]["feature_uuid"] == _U["template-router"]
    # conservation coverage-view: pfid None → the file has no PF owner (its
    # structural owner carries no product feature) — filtered, same owner.
    assert election.owner_pfid(f, None) is None
    assert f not in election.file_pf_owner_map(None)
    # and build_file_pf_owner fed the (empty-for-this-file) override agrees.
    r2 = build_file_pf_owner(
        [{"name": org.name, "paths": [f], "product_feature_id": None,
          "role": None},
         {"name": tr.name, "paths": [f], "product_feature_id": None,
          "role": None}],
        file_owner=election.file_pf_owner_map(None),
    )
    assert f not in r2


# ── Anti-case 1 — order-shuffle negative control ─────────────────────────


def test_order_shuffle_off_is_sensitive_on_is_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SACRED negative control: OFF (first-claimant) flips the owner when the
    features list is permuted; ON (election) is INVARIANT to permutation."""
    monkeypatch.setenv(OWNERSHIP_V2_ENV, "0")  # no module rung — pure tiebreak
    root = tmp_path / "shuf"
    f = "src/shared/util.ts"
    _write(root, f, 10)
    zeta = _dev("zeta", [f], [])
    alpha = _dev("alpha", [f], [])

    # OFF: order decides (the disease).
    off_ab = build_path_index(
        [{"uuid": zeta.uuid, "paths": [f]}, {"uuid": alpha.uuid, "paths": [f]}], [],
    )[f]["feature_uuid"]
    off_ba = build_path_index(
        [{"uuid": alpha.uuid, "paths": [f]}, {"uuid": zeta.uuid, "paths": [f]}], [],
    )[f]["feature_uuid"]
    assert off_ab != off_ba  # NEGATIVE CONTROL: OFF is order-sensitive
    assert off_ab == _U["zeta"] and off_ba == _U["alpha"]

    # ON: the election is a pure function of the per-dev signals (min slug),
    # so BOTH permutations elect the same owner.
    e_ab = build_owner_election([zeta, alpha], root)
    e_ba = build_owner_election([alpha, zeta], root)
    assert e_ab.owner_uuid(f) == e_ba.owner_uuid(f) == _U["alpha"]
    on_ab = build_path_index(
        [{"uuid": zeta.uuid, "paths": [f]}, {"uuid": alpha.uuid, "paths": [f]}],
        [], file_owner=e_ab.file_owner_uuid_map(),
    )[f]["feature_uuid"]
    on_ba = build_path_index(
        [{"uuid": alpha.uuid, "paths": [f]}, {"uuid": zeta.uuid, "paths": [f]}],
        [], file_owner=e_ba.file_owner_uuid_map(),
    )[f]["feature_uuid"]
    assert on_ab == on_ba == _U["alpha"]  # INVARIANT under permutation


# ── Anti-case 2 — PYTHONHASHSEED 0/1 determinism ─────────────────────────

_HASHSEED_SCRIPT = """
import sys
sys.path.insert(0, %r)
from faultline.pipeline_v2.owner_oracle import elect_primary_owners
# A contested multi-file / multi-dev universe exercising dict-iteration order.
file_to_devs = {f"src/d{i%%7}/f{i}.ts": [i %% 4, (i * 3 + 1) %% 4]
                for i in range(200)}
dev_is_facet = [0, 0, 0, 0]
dev_module_roots = []
dev_dircount = [{} for _ in range(4)]
dev_flowcount = [3, 3, 1, 1]
dev_slug = ["billing", "billing", "auth", "auth"]
res = elect_primary_owners(file_to_devs, dev_is_facet, dev_module_roots,
                           dev_dircount, dev_flowcount, dev_slug, False)
print("|".join(f"{k}={res[k]}" for k in sorted(res)))
"""


def _run_with_seed(seed: str, engine_root: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", _HASHSEED_SCRIPT % engine_root],
        capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
    )
    return out.stdout.strip()


def test_election_is_pythonhashseed_independent() -> None:
    # tests/pipeline_v2/test_owner_oracle.py → parents[2] = engine root (wt/).
    engine_root = str(Path(__file__).resolve().parents[2])
    seed0 = _run_with_seed("0", engine_root)
    seed1 = _run_with_seed("1", engine_root)
    assert seed0 == seed1 and seed0  # identical, non-empty


# ── Flag default + kill-switch ───────────────────────────────────────────


def test_flag_defaults_off_and_kill_switch(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(OWNER_ORACLE_ENV, raising=False)
    assert owner_oracle_enabled() is False  # unset → OFF
    monkeypatch.setenv(OWNER_ORACLE_ENV, "0")
    assert owner_oracle_enabled() is False  # explicit 0 → OFF
    monkeypatch.setenv(OWNER_ORACLE_ENV, "1")
    assert owner_oracle_enabled() is True
