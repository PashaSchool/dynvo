"""B53 — ws-app blob domain drain.

Seg A (this file's first half): a ws-blob donor's internal domain-dir members
(``<pkg>/<container>/<domain>/**``) re-attribute to the EXISTING PF whose
identity the domain name echoes — NO mints, coordinates unchanged, moved at
the dev level so Stage 6.97 LOC + the path_index rebuild + Stage 6.99 I16
journey re-home follow for free.

Seg B (second half): dev-artifact ws-packages (docs-content / devDependency-
only tooling / scaffolder) leave the product layer via the dev_artifact_units
channel; SACRED product anti-cases (zapier integration, published SDK, Next
website with routes) survive.

Fixtures are synthetic (authority = engine signal, not offline sims), distilled
from the twenty panel review (2026-07-12).
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, Flow, MemberFile, UserFlow
from faultline.pipeline_v2 import ws_blob_domain_drain as WBD
from faultline.pipeline_v2.ws_blob_domain_drain import (
    run_ws_blob_domain_drain,
    ws_blob_domain_drain_enabled,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

PKG = "twenty-front"


# ── builders ──────────────────────────────────────────────────────────────


def _mf(path: str, evidence: str = "orig") -> MemberFile:
    return MemberFile(path=path, role="anchor", confidence=1.0, primary=True,
                      evidence=evidence)


def _dev(name, paths, *, pfid=None, flows=None):
    f = Feature(
        name=name, paths=list(paths), flows=flows or [],
        product_feature_id=pfid, member_files=[_mf(p) for p in paths],
        shared_reason=None,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )
    return f


def _pf(slug, paths, anchor_id):
    f = Feature(
        name=slug, paths=list(paths), flows=[],
        member_files=[_mf(p) for p in paths],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, loc=10,
    )
    f.layer = "product"
    f.anchor_id = anchor_id
    return f


def _flow(name, entry, uuid):
    return Flow(
        name=name, entry_point_file=entry, paths=[entry], uuid=uuid,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def _uf(uid, name, pfid, member_ids):
    return UserFlow(
        id=uid, name=name, intent="manage", resource="record",
        product_feature_id=pfid, member_flow_ids=list(member_ids),
        member_count=len(member_ids),
    )


class _Ctx:
    def __init__(self, repo="/repo"):
        self.repo_path = repo


# domain-dir member files inside the blob package.
OR1 = f"{PKG}/src/modules/object-record/RecordTable.tsx"
OR2 = f"{PKG}/src/modules/object-record/hooks/useRecord.ts"
MSG1 = f"{PKG}/src/modules/messaging/MessageThread.tsx"
COMP1 = f"{PKG}/src/modules/components/Button.tsx"        # generic
SHELL1 = f"{PKG}/src/app/App.tsx"
SHELL2 = f"{PKG}/src/main.tsx"
# anchor files the domain PFs already own inside the same package.
OR_ANCHOR = f"{PKG}/src/anchors/object-record.ts"
MSG_ANCHOR = f"{PKG}/src/anchors/messaging.ts"


def _twenty_world():
    """A twenty-front blob holding object-record + messaging + a generic
    components dir + app shell, with the real object-record / messaging PFs
    existing (each owning an anchor file inside twenty-front)."""
    blob_dev = _dev(
        "twenty-front-blob",
        [OR1, OR2, MSG1, COMP1, SHELL1, SHELL2],
        pfid="twenty-front",
        flows=[_flow("record-table", OR1, "F-OR1"),
               _flow("record-hook", OR2, "F-OR2"),
               _flow("msg-thread", MSG1, "F-MSG1")],
    )
    or_dev = _dev("object-record-owner", [OR_ANCHOR], pfid="object-record")
    msg_dev = _dev("messaging-owner", [MSG_ANCHOR], pfid="messaging")

    front_pf = _pf("twenty-front",
                   [OR1, OR2, MSG1, COMP1, SHELL1, SHELL2], "ws:twenty-front")
    or_pf = _pf("object-record", [OR_ANCHOR], "route:/object-record")
    msg_pf = _pf("messaging", [MSG_ANCHOR], "route:/messaging")

    devs = [blob_dev, or_dev, msg_dev]
    pfs = [front_pf, or_pf, msg_pf]
    ufs = [
        _uf("UF-1", "Manage object record", "twenty-front", ["F-OR1", "F-OR2"]),
        _uf("UF-2", "Manage messaging", "twenty-front", ["F-MSG1"]),
    ]
    flows = list(blob_dev.flows)
    return devs, pfs, ufs, flows


# ── flag semantics ──────────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv(WBD.WS_BLOB_DRAIN_ENV, raising=False)
    assert ws_blob_domain_drain_enabled() is False
    monkeypatch.setenv(WBD.WS_BLOB_DRAIN_ENV, "1")
    assert ws_blob_domain_drain_enabled() is True
    monkeypatch.setenv(WBD.WS_BLOB_DRAIN_ENV, "0")
    assert ws_blob_domain_drain_enabled() is False


def test_empty_product_features_is_noop():
    tele = run_ws_blob_domain_drain([], [], [], [], _Ctx())
    assert tele["files_moved"] == 0
    assert tele["donors"] == []


# ── Seg A — the drain ───────────────────────────────────────────────────────


def test_full_match_drains_domain_dir_to_existing_pf():
    devs, pfs, ufs, flows = _twenty_world()
    tele = run_ws_blob_domain_drain(devs, pfs, ufs, flows, _Ctx())

    # object-record + messaging domain dirs matched; components is generic.
    assert tele["matched_dirs"] == {
        f"{PKG}/src/modules/messaging": "messaging",
        f"{PKG}/src/modules/object-record": "object-record",
    }
    assert tele["files_moved"] == 3           # OR1, OR2, MSG1 (NOT COMP1)
    assert tele["skipped_generic"] == 1       # components

    # object-record PF now OWNS the drained members (scope widened).
    or_pf = next(p for p in pfs if p.name == "object-record")
    assert OR1 in or_pf.paths and OR2 in or_pf.paths
    # twenty-front blob no longer owns them.
    front_pf = next(p for p in pfs if p.name == "twenty-front")
    assert OR1 not in front_pf.paths and MSG1 not in front_pf.paths
    # generic + shell mass stays in the blob (app-shell survives).
    assert COMP1 in front_pf.paths and SHELL1 in front_pf.paths


def test_drained_members_owned_by_target_pf_dev():
    """path_index (built from dev paths) must see the drained files under a
    dev whose product_feature_id is the target — the Stage-6.99 I16 hook."""
    devs, pfs, ufs, flows = _twenty_world()
    run_ws_blob_domain_drain(devs, pfs, ufs, flows, _Ctx())

    def _owner(path):
        for d in devs:
            if d.layer == "developer" and path in [
                    m.path for m in d.member_files if m.primary]:
                return d.product_feature_id
        return None

    assert _owner(OR1) == "object-record"
    assert _owner(OR2) == "object-record"
    assert _owner(MSG1) == "messaging"


def test_provenance_tag_stamped():
    devs, pfs, ufs, flows = _twenty_world()
    run_ws_blob_domain_drain(devs, pfs, ufs, flows, _Ctx())
    or_pf = next(p for p in pfs if p.name == "object-record")
    drained_mf = [m for m in or_pf.member_files if m.path == OR1]
    assert drained_mf and WBD._DRAIN_MARKER in (drained_mf[0].evidence or "")


def test_no_mint():
    devs, pfs, ufs, flows = _twenty_world()
    before = len(pfs)
    run_ws_blob_domain_drain(devs, pfs, ufs, flows, _Ctx())
    assert len(pfs) == before  # never mints a new PF


def test_journey_rehome_projection():
    """UF-1 'Manage object record' has BOTH member-flow entries drained →
    projected to re-home (the Stage-6.99 I16 move)."""
    devs, pfs, ufs, flows = _twenty_world()
    tele = run_ws_blob_domain_drain(devs, pfs, ufs, flows, _Ctx())
    assert tele["ufs_rehomed"] >= 1


def test_plural_domain_dir_matches_singular_pf():
    blob = _dev("blob", [f"{PKG}/src/modules/object-records/Table.tsx",
                         f"{PKG}/src/main.tsx"], pfid=PKG)
    or_dev = _dev("or", [OR_ANCHOR], pfid="object-record")
    pfs = [_pf(PKG, list(blob.paths), "ws:" + PKG),
           _pf("object-record", [OR_ANCHOR], "route:/object-record")]
    tele = run_ws_blob_domain_drain([blob, or_dev], pfs, [], [], _Ctx())
    assert tele["matched_dirs"] == {
        f"{PKG}/src/modules/object-records": "object-record"}


def test_ambiguous_match_skipped():
    blob = _dev("blob", [f"{PKG}/src/modules/billing/Invoice.tsx",
                         f"{PKG}/src/main.tsx"], pfid=PKG)
    b1 = _dev("b1", [f"{PKG}/src/a.ts"], pfid="billing")
    b2 = _dev("b2", [f"{PKG}/src/b.ts"], pfid="billings")
    # two PFs whose identity normalizes to the same key → ambiguous.
    pfs = [_pf(PKG, list(blob.paths), "ws:" + PKG),
           _pf("billing", [f"{PKG}/src/a.ts"], "route:/billing"),
           _pf("billings", [f"{PKG}/src/b.ts"], "route:/billings")]
    tele = run_ws_blob_domain_drain([blob, b1, b2], pfs, [], [], _Ctx())
    assert tele["files_moved"] == 0
    assert tele["skipped_ambig"] == 1


def test_generic_token_dir_stays_in_blob():
    for gen in ("components", "utils", "lib", "shared", "hooks", "types",
                "state", "ui", "constants", "common"):
        blob = _dev("blob", [f"{PKG}/src/modules/{gen}/x.ts",
                             f"{PKG}/src/main.tsx"], pfid=PKG)
        # a decoy PF whose name matches the generic dir — must STILL skip.
        pfs = [_pf(PKG, list(blob.paths), "ws:" + PKG),
               _pf(gen, [f"{PKG}/src/{gen}-anchor.ts"], f"route:/{gen}")]
        tele = run_ws_blob_domain_drain([blob], pfs, [], [], _Ctx())
        assert tele["files_moved"] == 0, gen
        assert tele["skipped_generic"] >= 1, gen


def test_no_surface_domain_stays_in_blob():
    """A domain dir with NO existing PF is left in the blob (honest, no mint)."""
    blob = _dev("blob", [f"{PKG}/src/modules/telemetry/probe.ts",
                         f"{PKG}/src/main.tsx"], pfid=PKG)
    pfs = [_pf(PKG, list(blob.paths), "ws:" + PKG)]
    tele = run_ws_blob_domain_drain([blob], pfs, [], [], _Ctx())
    assert tele["files_moved"] == 0
    assert tele["skipped_generic"] == 0 and tele["skipped_ambig"] == 0


def test_cross_unit_boundary_blocks_drain():
    """B22a: a twenty-front file may NOT move to a PF that owns code only in
    twenty-server (a different workspace package)."""
    blob = _dev("blob", [f"{PKG}/src/modules/workflow/Engine.tsx",
                         f"{PKG}/src/main.tsx"], pfid=PKG)
    # workflow PF lives entirely in twenty-server → cross-unit.
    wf_dev = _dev("wf", ["twenty-server/src/workflow.ts"], pfid="workflow")
    pfs = [_pf(PKG, list(blob.paths), "ws:" + PKG),
           _pf("workflow", ["twenty-server/src/workflow.ts"],
               "route:/workflow")]
    tele = run_ws_blob_domain_drain([blob, wf_dev], pfs, [], [], _Ctx())
    assert tele["files_moved"] == 0  # blocked by cross-unit guard


def test_idempotent_second_run_moves_nothing():
    devs, pfs, ufs, flows = _twenty_world()
    run_ws_blob_domain_drain(devs, pfs, ufs, flows, _Ctx())
    tele2 = run_ws_blob_domain_drain(devs, pfs, ufs, flows, _Ctx())
    assert tele2["files_moved"] == 0  # already product-homed → untouched


def test_already_homed_file_untouched():
    """A file under a domain dir that is PRIMARY-owned by a NON-blob dev is
    never touched (idempotence with keyed 8.9.6)."""
    homed = f"{PKG}/src/modules/object-record/AlreadyHomed.tsx"
    blob = _dev("blob", [f"{PKG}/src/main.tsx"], pfid=PKG)
    or_dev = _dev("or", [homed, OR_ANCHOR], pfid="object-record")
    pfs = [_pf(PKG, [f"{PKG}/src/main.tsx"], "ws:" + PKG),
           _pf("object-record", [homed, OR_ANCHOR], "route:/object-record")]
    tele = run_ws_blob_domain_drain([blob, or_dev], pfs, [], [], _Ctx())
    assert tele["files_moved"] == 0
    # still owned by its original dev, unchanged.
    assert homed in [m.path for m in or_dev.member_files]


# ── SACRED anti-cases ────────────────────────────────────────────────────────


def test_sacred_named_pf_module_never_touched():
    """auth PF already holds its full module (owned by ITS OWN dev, not the
    blob) — the drain only re-attributes FROM the donor blob."""
    auth_files = [f"{PKG}/src/modules/auth/AuthService.ts",
                  f"{PKG}/src/modules/auth/useAuth.ts"]
    blob = _dev("blob", [OR1, f"{PKG}/src/main.tsx"], pfid=PKG,
                flows=[])
    auth_dev = _dev("auth-owner", auth_files, pfid="auth")
    or_dev = _dev("or", [OR_ANCHOR], pfid="object-record")
    pfs = [_pf(PKG, [OR1, f"{PKG}/src/main.tsx"], "ws:" + PKG),
           _pf("auth", auth_files, "route:/auth"),
           _pf("object-record", [OR_ANCHOR], "route:/object-record")]
    run_ws_blob_domain_drain([blob, auth_dev, or_dev], pfs, [], [], _Ctx())
    # auth dev + PF unchanged (never re-attributed between domain PFs).
    assert auth_dev.product_feature_id == "auth"
    assert set(m.path for m in auth_dev.member_files) == set(auth_files)


def test_sacred_sibling_pfs_not_merged():
    """navigation vs navigation-menu-item stay TWO PFs — B53 never merges."""
    blob = _dev("blob", [f"{PKG}/src/modules/navigation/Nav.tsx",
                         f"{PKG}/src/main.tsx"], pfid=PKG)
    nav_dev = _dev("nav", [f"{PKG}/src/nav.ts"], pfid="navigation")
    item_dev = _dev("item", [f"{PKG}/src/item.ts"],
                    pfid="navigation-menu-item")
    pfs = [_pf(PKG, list(blob.paths), "ws:" + PKG),
           _pf("navigation", [f"{PKG}/src/nav.ts"], "route:/navigation"),
           _pf("navigation-menu-item", [f"{PKG}/src/item.ts"],
               "route:/navigation-menu-item")]
    before = len(pfs)
    tele = run_ws_blob_domain_drain(
        [blob, nav_dev, item_dev], pfs, [], [], _Ctx())
    assert len(pfs) == before  # no merge, no mint
    assert tele["matched_dirs"] == {
        f"{PKG}/src/modules/navigation": "navigation"}
    assert {p.name for p in pfs} == {
        PKG, "navigation", "navigation-menu-item"}


def test_sacred_plane_core_not_a_ws_anchor_untouched():
    """plane 'Core' (a non-ws anchor sub-class) is never a donor and never a
    match target that steals blob files."""
    core_pf = _pf("Core", ["plane/apiserver/core.py"], "route:/core")
    blob = _dev("blob", [f"{PKG}/src/modules/pages/x.ts",
                         f"{PKG}/src/main.tsx"], pfid=PKG)
    pfs = [core_pf, _pf(PKG, list(blob.paths), "ws:" + PKG)]
    tele = run_ws_blob_domain_drain([blob], pfs, [], [], _Ctx())
    assert "Core" not in tele["donors"]
    assert core_pf.paths == ["plane/apiserver/core.py"]  # untouched


def test_core_modules_container_at_depth():
    """twenty-server shape: `src/engine/core-modules/<domain>/` — a container
    at depth, drained onto the existing domain PF."""
    pkg = "twenty-server"
    f1 = f"{pkg}/src/engine/core-modules/messaging/MessageService.ts"
    blob = _dev("blob", [f1, f"{pkg}/src/main.ts"], pfid=pkg)
    msg_dev = _dev("msg", [f"{pkg}/src/anchors/messaging.ts"], pfid="messaging")
    pfs = [_pf(pkg, list(blob.paths), "ws:" + pkg),
           _pf("messaging", [f"{pkg}/src/anchors/messaging.ts"],
               "route:/messaging")]
    tele = run_ws_blob_domain_drain([blob, msg_dev], pfs, [], [], _Ctx())
    assert tele["matched_dirs"] == {
        f"{pkg}/src/engine/core-modules/messaging": "messaging"}
    assert tele["files_moved"] == 1
