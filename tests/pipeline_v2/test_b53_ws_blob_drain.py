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
    def __init__(self, repo="/repo", tracked=None):
        self.repo_path = repo
        self.tracked_files = list(tracked or [])


def _dirs_pf(tele):
    """matched_dirs projected to ``{dir: pf}`` (v2 carries per-dir stats)."""
    return {d: v["pf"] for d, v in tele["matched_dirs"].items()}


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
    assert _dirs_pf(tele) == {
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
    assert _dirs_pf(tele) == {
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
    assert _dirs_pf(tele) == {
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
    assert _dirs_pf(tele) == {
        f"{pkg}/src/engine/core-modules/messaging": "messaging"}
    assert tele["files_moved"] == 1


# ── Seg A v2 — implicit subtree drain + donor ancestor carve-out ────────────


def _write_lines(root: Path, rel: str, n: int) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(f"line{i}" for i in range(n)) + "\n",
                 encoding="utf-8")
    return rel


def _v2_world(tmp_path):
    """Real files on disk (6.97 authority): a donor whose ONLY claim is the
    ANCESTOR DIRECTORY ``<pkg>/src`` (the implicit-subtree shape measured on
    twenty-front), an object-record PF owning one explicit anchor, and an
    other-PF dev owning explicit files INSIDE the drained dir."""
    pkg = PKG
    t1 = _write_lines(tmp_path, f"{pkg}/src/modules/object-record/table.ts", 10)
    t2 = _write_lines(
        tmp_path, f"{pkg}/src/modules/object-record/hooks/use.ts", 20)
    ext = _write_lines(
        tmp_path, f"{pkg}/src/modules/object-record/keep/ext.ts", 5)
    shell = _write_lines(tmp_path, f"{pkg}/src/app/shell.ts", 7)
    anchor = _write_lines(tmp_path, f"{pkg}/src/or-anchor.ts", 3)
    tracked = [t1, t2, ext, shell, anchor]

    blob_dev = Feature(
        name="twenty-front-blob", paths=[f"{pkg}/src"], flows=[],
        product_feature_id="twenty-front", member_files=[],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )
    blob_dev.uuid = "u-blob"
    or_dev = _dev("object-record-owner", [anchor], pfid="object-record")
    or_dev.uuid = "u-or"
    other_dev = _dev("other-owner", [ext], pfid="other-pf")
    other_dev.uuid = "u-other"

    front_pf = _pf("twenty-front", [pkg], "ws:twenty-front")
    front_pf.uuid = "u-front-pf"
    or_pf = _pf("object-record", [anchor], "route:/object-record")
    other_pf = _pf("other-pf", [ext], "route:/other-pf")

    devs = [blob_dev, or_dev, other_dev]
    pfs = [front_pf, or_pf, other_pf]
    path_index = {
        anchor: {"feature_uuid": "u-or", "flow_uuids": []},
        ext: {"feature_uuid": "u-other", "flow_uuids": []},
    }
    ctx = _Ctx(repo=str(tmp_path), tracked=tracked)
    return devs, pfs, ctx, path_index, {
        "t1": t1, "t2": t2, "ext": ext, "shell": shell, "anchor": anchor,
        "blob": blob_dev, "or_dev": or_dev, "other_dev": other_dev,
        "front_pf": front_pf, "or_pf": or_pf, "other_pf": other_pf,
    }


def test_v2_subtree_unattributed_files_drain(tmp_path):
    """Files ABSENT from path_index under a matched dir move (the subtree
    rule); a file with a NON-donor path_index entry never does."""
    devs, pfs, ctx, pi, w = _v2_world(tmp_path)
    tele = run_ws_blob_domain_drain(devs, pfs, [], [], ctx, path_index=pi)
    assert tele["files_moved"] == 2                    # t1 + t2, NOT ext
    assert tele["files_moved_unattributed"] == 2
    assert tele["skipped_pi_foreign"] == 1             # ext (u-other entry)
    assert tele["loc_moved"] == 30                     # 10 + 20 real lines
    dirs = _dirs_pf(tele)
    assert dirs == {f"{PKG}/src/modules/object-record": "object-record"}
    # per-dir stats carried for the census report.
    stats = tele["matched_dirs"][f"{PKG}/src/modules/object-record"]
    assert stats["files"] == 2 and stats["loc"] == 30


def test_v2_ancestor_carveout_shifts_6_97_loc(tmp_path):
    """THE v1 shortfall reproduced against the REAL Stage 6.97 contest: the
    donor's ancestor-dir claim (``<pkg>/src``) would keep winning
    ``_primary`` via expansion; the carve-out splits it so the explicit
    carve dev becomes the sole claimant and the LOC actually shifts."""
    from faultline.pipeline_v2.stage_6_97_feature_loc import apply_feature_loc

    devs, pfs, ctx, pi, w = _v2_world(tmp_path)
    apply_feature_loc(list(devs), pfs, tmp_path)
    front_before, or_before = w["front_pf"].loc, w["or_pf"].loc
    other_before = w["other_pf"].loc
    assert front_before == 37   # t1+t2+shell (10+20+7) via src/ expansion
    assert or_before == 3       # anchor only (slug tiebreak vs blob dir)
    assert other_before == 5    # explicit ext.ts

    tele = run_ws_blob_domain_drain(devs, pfs, [], [], ctx, path_index=pi)
    assert tele["files_moved"] == 2
    # donor ancestor carve-out: src/ split into children minus the drained
    # subtree — deterministic sorted, drained dir gone (ext.ts inside it
    # stays with its EXPLICIT other-pf owner, not via donor coverage).
    assert w["blob"].paths == [
        f"{PKG}/src/app",
        f"{PKG}/src/or-anchor.ts",
    ]

    apply_feature_loc(list(devs), pfs, tmp_path)
    front_after, or_after = w["front_pf"].loc, w["or_pf"].loc
    assert or_after == or_before + 30          # gained t1 + t2
    assert front_after == front_before - 30    # lost exactly the moved mass
    assert w["other_pf"].loc == other_before   # bystander untouched
    # conservation of the moved mass.
    assert front_before + or_before == front_after + or_after


def test_v2_explicit_carve_no_theft_of_other_pf_files(tmp_path):
    """The ``_primary``-contest anti-case: an other-PF dev with EXPLICIT
    files inside the drained dir keeps them — the carve claims an explicit
    file list (never a dir), so no contest ever forms."""
    from faultline.pipeline_v2.stage_6_97_feature_loc import apply_feature_loc

    devs, pfs, ctx, pi, w = _v2_world(tmp_path)
    run_ws_blob_domain_drain(devs, pfs, [], [], ctx, path_index=pi)
    # ext.ts never entered any carve dev / the target PF.
    carves = [d for d in devs if WBD._DRAIN_MARKER in str(d.name)]
    assert carves and all(w["ext"] not in d.paths for d in carves)
    assert w["ext"] not in w["or_pf"].paths
    assert w["ext"] in w["other_dev"].paths
    apply_feature_loc(list(devs), pfs, tmp_path)
    assert w["other_pf"].loc == 5              # engine authority: no theft


def test_v2_nondonor_dev_claim_blocks_unattributed(tmp_path):
    """A file with NO path_index entry but a live non-donor dev claim is
    never drained (dev ledger = post-6.8 truth; keyed 8.9.6 idempotency)."""
    devs, pfs, ctx, pi, w = _v2_world(tmp_path)
    del pi[w["ext"]]  # no path_index entry — only the dev-ledger claim left
    tele = run_ws_blob_domain_drain(devs, pfs, [], [], ctx, path_index=pi)
    assert tele["files_moved"] == 2
    assert tele["skipped_nondonor_covered"] == 1
    assert w["ext"] in w["other_dev"].paths


def test_v2_idempotent_second_run(tmp_path):
    """Second run moves nothing: the carve dev's explicit claims make the
    drained files non-donor property (the 8.9.6 idempotency shape)."""
    devs, pfs, ctx, pi, w = _v2_world(tmp_path)
    tele1 = run_ws_blob_domain_drain(devs, pfs, [], [], ctx, path_index=pi)
    assert tele1["files_moved"] == 2
    tele2 = run_ws_blob_domain_drain(devs, pfs, [], [], ctx, path_index=pi)
    assert tele2["files_moved"] == 0


# ── Seg A v3 — PF-survival invariance (typebot 'popup' class) ───────────────
#
# Wave forensics (2026-07-13): typebot flag-ON emitted a 'popup' PF (0
# flows / 203 LOC) that does NOT exist on the OFF board — the v2 drain ran
# BEFORE the W4.2 post-UF vendor-husk fold and fattened a foldable husk,
# sparing a PF row. v3 relocates the drain AFTER the fold so its target
# set equals the OFF-emission survivor set by construction.


def _popup_world():
    """A donor blob + a FOLDABLE husk PF ('popup': hub-vendor child under
    integrations/, flowless, journey-uncited) + a legit surviving receiver
    (object-record). The fold must kill popup; the drain (running AFTER,
    v3 order) must then leave popup's domain-dir files in the blob while
    still draining the legit receiver's dir."""
    popup_file = f"{PKG}/src/modules/popup/Popup.tsx"
    blob_dev = _dev(
        "twenty-front-blob",
        [OR1, popup_file, SHELL2],
        pfid="twenty-front",
    )
    popup_dev = _dev("popup-embed",
                     [f"{PKG}/src/integrations/popup/embed.ts"],
                     pfid="popup")
    or_dev = _dev("object-record-owner", [OR_ANCHOR], pfid="object-record")

    front_pf = _pf("twenty-front", [OR1, popup_file, SHELL2],
                   "ws:twenty-front")
    popup_pf = _pf("popup", [f"{PKG}/src/integrations/popup/embed.ts"],
                   f"hub:{PKG}/src/integrations/popup")
    or_pf = _pf("object-record", [OR_ANCHOR], "route:/object-record")

    devs = [blob_dev, popup_dev, or_dev]
    pfs = [front_pf, popup_pf, or_pf]
    ufs = []  # popup journey-uncited (the fold's ruler)
    return devs, pfs, ufs, popup_file


def test_v3_foldable_husk_stays_dropped_under_flag_on():
    """The popup class: a PF the husk-fold drops must STAY dropped when
    the drain runs (v3 order: fold first) — the drain never sees it, never
    fattens it, its domain-dir files stay in the blob."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        fold_unreferenced_vendor_husks,
    )

    devs, pfs, ufs, popup_file = _popup_world()
    hf = fold_unreferenced_vendor_husks(devs, pfs, ufs)
    assert any(f["pf"] == "popup" for f in hf["folded"])
    assert "popup" not in {p.name for p in pfs}      # fold killed the row

    tele = run_ws_blob_domain_drain(devs, pfs, ufs, [], _Ctx())
    # popup never resurrects: no match, no carve dev, files stay in blob.
    assert "popup" not in {p.name for p in pfs}
    assert all("popup" not in d for d in _dirs_pf(tele))
    assert not any("popup" in str(d.name) for d in devs
                   if WBD._DRAIN_MARKER in str(d.name))
    blob = next(d for d in devs if d.name == "twenty-front-blob")
    assert popup_file in blob.paths
    # the legit receiver still drains (object-record exists OFF).
    assert _dirs_pf(tele) == {
        f"{PKG}/src/modules/object-record": "object-record"}


def test_v3_pf_nameset_invariance_off_vs_on():
    """Flag-ON emission PF name-set == flag-OFF name-set on a board with a
    foldable husk + a matched domain dir pointing at it — the drain only
    re-attributes members, it never changes which PFs survive."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        fold_unreferenced_vendor_husks,
    )

    # OFF path: fold only.
    devs_off, pfs_off, ufs_off, _ = _popup_world()
    fold_unreferenced_vendor_husks(devs_off, pfs_off, ufs_off)
    names_off = {p.name for p in pfs_off}

    # ON path: fold, THEN drain (the v3 order).
    devs_on, pfs_on, ufs_on, _ = _popup_world()
    fold_unreferenced_vendor_husks(devs_on, pfs_on, ufs_on)
    tele = run_ws_blob_domain_drain(devs_on, pfs_on, ufs_on, [], _Ctx())
    names_on = {p.name for p in pfs_on}

    assert names_on == names_off                     # survival invariance
    assert tele["files_moved"] >= 1                  # …while ink still moved


def test_v3_drain_wired_after_husk_fold_in_finalize():
    """Source-order guard: the Stage 6.885b drain call sits AFTER the W4.2
    husk-fold call and BEFORE the journey-lattice block in phase_finalize
    (the v3 relocation — PF-survival invariance by construction)."""
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[2]
           / "faultline" / "pipeline_v2" / "phase_finalize.py"
           ).read_text(encoding="utf-8")
    i_fold = src.index("fold_unreferenced_vendor_husks(")
    i_drain = src.index("run_ws_blob_domain_drain(")
    i_lattice = src.index("journey lattice (Product-Spine W5)")
    assert i_fold < i_drain < i_lattice


# ── Seg B — dev-artifact ws-packages off the product layer ───────────────────

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from faultline.pipeline_v2.technology_instruments import (  # noqa: E402
    detect_technology_instruments,
)


def _write(repo: Path, rel: str, text: str = "") -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel


def _manifest(repo, rel_dir, name, *, deps=None, dev_deps=None,
              private=None, bin_entry=None):
    doc: dict = {"name": name}
    if deps:
        doc["dependencies"] = deps
    if dev_deps:
        doc["devDependencies"] = dev_deps
    if private is not None:
        doc["private"] = private
    if bin_entry:
        doc["bin"] = {name.split("/")[-1]: bin_entry}
    rel = f"{rel_dir}/package.json" if rel_dir else "package.json"
    return _write(repo, rel, json.dumps(doc))


def _twenty_monorepo(repo: Path):
    """A twenty-shaped monorepo (v2 — REAL manifest facts from the rig
    forensics): three dev-artifact packages (docs site / private-unconsumed
    oxlint-rules / published scaffolder with an embedded template
    package.json) + three SACRED product packages (zapier integration,
    published sdk, Next website)."""
    tracked: list[str] = [
        # root lists workspaces only — twenty-oxlint-rules is dep-declared
        # NOWHERE in the repo (the rig fact that killed the v1 predicate).
        _manifest(repo, "", "twenty", private=True),
    ]
    # (1) docs-content — a docs SITE: mostly .mdx with a few theme sources
    # (so it is NOT the S1c pure-config-only shape; ≥80% markdown).
    tracked.append(_manifest(repo, "packages/twenty-docs", "twenty-docs",
                             private=True))
    for i in range(18):
        tracked.append(_write(repo, f"packages/twenty-docs/docs/page{i}.mdx",
                              f"# Doc {i}\n"))
    for i in range(3):
        tracked.append(_write(
            repo, f"packages/twenty-docs/src/theme/C{i}.tsx",
            f"export const C{i} = () => null;\n"))
    # (2b) private-unconsumed tooling — private:true, no bin, zero runtime
    # dependencies (only its OWN devDependency), consumed by nobody.
    tracked.append(_manifest(repo, "packages/twenty-oxlint-rules",
                             "twenty-oxlint-rules", private=True,
                             dev_deps={"@oxlint/plugins": "1.0.0"}))
    tracked.append(_write(repo, "packages/twenty-oxlint-rules/src/rule.ts",
                          "export function rule(ctx){ return ctx.report(); }\n"))
    tracked.append(_write(repo, "packages/twenty-oxlint-rules/src/rule2.ts",
                          "export function rule2(ctx){ return ctx.check(); }\n"))
    # (3) scaffolder — NOT private (published_cli veto fires — template
    # evidence overrides it), a RUNTIME workspace dep on the sdk
    # (scaffolders embed the SDK), and the template payload EMBEDS its own
    # package.json — which makes the template dir a nested ws-unit that
    # steals the files from files_by_unit (the rig miss).
    tracked.append(_manifest(repo, "packages/create-twenty-app",
                             "create-twenty-app", private=False,
                             deps={"twenty-sdk": "workspace:*"},
                             bin_entry="dist/cli.cjs"))
    tracked.append(_write(repo, "packages/create-twenty-app/src/cli.ts",
                          "export function main(){ scaffold(); }\n"))
    tracked.append(_manifest(
        repo, "packages/create-twenty-app/src/constants/template",
        "template-payload", private=True))
    tracked.append(_write(
        repo,
        "packages/create-twenty-app/src/constants/template/index.ts", "x\n"))
    # SACRED (a) integration = own PF — real .ts, not a devdep, no template.
    tracked.append(_manifest(repo, "packages/twenty-zapier", "twenty-zapier",
                             private=True,
                             deps={"zapier-platform-core": "15.0.0"}))
    tracked.append(_write(repo, "packages/twenty-zapier/src/index.ts",
                          'import zapier from "zapier-platform-core";\n'))
    # SACRED (b) published SDK — consumed as a RUNTIME dependency by web.
    tracked.append(_manifest(repo, "packages/twenty-sdk", "twenty-sdk",
                             private=False))
    tracked.append(_write(repo, "packages/twenty-sdk/src/client.ts",
                          "export class Client {}\n"))
    tracked.append(_manifest(repo, "apps/web", "@twenty/web", private=True,
                             deps={"twenty-sdk": "workspace:*"}))
    tracked.append(_write(repo, "apps/web/src/app.ts",
                          'import { Client } from "twenty-sdk";\n'))
    # SACRED (c) Next website — has a route surface.
    tracked.append(_manifest(repo, "packages/twenty-website", "twenty-website",
                             private=True))
    tracked.append(_write(repo, "packages/twenty-website/app/page.tsx",
                          "export default function Page(){ return null; }\n"))
    for i in range(6):
        tracked.append(_write(
            repo, f"packages/twenty-website/content/post{i}.mdx", "# Post\n"))
    routes = [{"file": "packages/twenty-website/app/page.tsx",
               "pattern": "/", "method": "PAGE"}]
    return tracked, routes


def _detect_b53(repo, tracked, routes):
    return detect_technology_instruments(repo, tracked, routes)


def test_segb_flag_off_byte_inert(tmp_path, monkeypatch):
    monkeypatch.delenv(WBD.WS_BLOB_DRAIN_ENV, raising=False)
    tracked, routes = _twenty_monorepo(tmp_path)
    tele = _detect_b53(tmp_path, tracked, routes)
    da = tele.get("dev_artifact_units") or {}
    assert "packages/twenty-docs" not in da
    assert "packages/twenty-oxlint-rules" not in da
    assert "packages/create-twenty-app" not in da
    assert "b53_dev_artifact" not in tele


def test_segb_docs_privateunconsumed_scaffolder_laned(tmp_path, monkeypatch):
    monkeypatch.setenv(WBD.WS_BLOB_DRAIN_ENV, "1")
    tracked, routes = _twenty_monorepo(tmp_path)
    tele = _detect_b53(tmp_path, tracked, routes)
    da = tele.get("dev_artifact_units") or {}
    assert da.get("packages/twenty-docs") == "B53:docs-content"
    # v2: dep-declared nowhere → private-unconsumed prong (the devdep-only
    # predicate can never fire on the real twenty-oxlint-rules).
    assert da.get("packages/twenty-oxlint-rules") == "B53:private-unconsumed"
    # v2: published (published_cli veto) + runtime sdk dep + template
    # payload with its own package.json — template evidence wins.
    assert da.get("packages/create-twenty-app") == "B53:scaffolder"


def test_segb_devdep_only_union_prong_survives(tmp_path, monkeypatch):
    """The v1 devdep-only prong stays in the union: a package the root
    manifest declares under devDependencies (and nothing else consumes)
    lanes via the original predicate."""
    monkeypatch.setenv(WBD.WS_BLOB_DRAIN_ENV, "1")
    tracked = [
        _manifest(tmp_path, "", "acme", private=True,
                  dev_deps={"@acme/lint-config": "workspace:*"}),
        _manifest(tmp_path, "packages/lint-config", "@acme/lint-config",
                  private=True, deps={"eslint-plugin-x": "1.0.0"}),
        _write(tmp_path, "packages/lint-config/src/a.ts",
               "export const rules = {};\n"),
        _write(tmp_path, "packages/lint-config/src/b.ts",
               "export const more = {};\n"),
        _write(tmp_path, "packages/lint-config/src/c.ts",
               "export const even = {};\n"),
    ]
    tele = _detect_b53(tmp_path, tracked, [])
    da = tele.get("dev_artifact_units") or {}
    assert da.get("packages/lint-config") == "B53:devdep-only"


def test_segb_cli_without_template_stays_product(tmp_path, monkeypatch):
    """midday-style packages/cli anti-case: bin + private, NO template dir
    → no prong fires (scaffolder needs template evidence; the
    private-unconsumed prong excludes bin-bearing packages)."""
    monkeypatch.setenv(WBD.WS_BLOB_DRAIN_ENV, "1")
    tracked = [
        _manifest(tmp_path, "", "acme", private=True),
        _manifest(tmp_path, "packages/cli", "@acme/cli", private=True,
                  deps={"commander": "12.0.0"}, bin_entry="dist/cli.js"),
        _write(tmp_path, "packages/cli/src/cli.ts",
               "export function main(){}\n"),
    ]
    tele = _detect_b53(tmp_path, tracked, [])
    assert "packages/cli" not in (tele.get("dev_artifact_units") or {})


def test_segb_sacred_anticases_survive(tmp_path, monkeypatch):
    monkeypatch.setenv(WBD.WS_BLOB_DRAIN_ENV, "1")
    tracked, routes = _twenty_monorepo(tmp_path)
    tele = _detect_b53(tmp_path, tracked, routes)
    da = tele.get("dev_artifact_units") or {}
    # integration = own PF; published SDK; Next website (route veto) — product.
    assert "packages/twenty-zapier" not in da
    assert "packages/twenty-sdk" not in da
    assert "packages/twenty-website" not in da
    # the website is protected by the existing route_surface veto.
    assert tele["vetoed"].get("packages/twenty-website") == "route_surface"


def test_segb_website_route_veto_protects(tmp_path, monkeypatch):
    """Even with markdown content, a package WITH a route surface is never
    laned as docs-content (marketing-vs-product deferred)."""
    monkeypatch.setenv(WBD.WS_BLOB_DRAIN_ENV, "1")
    tracked, routes = _twenty_monorepo(tmp_path)
    tele = _detect_b53(tmp_path, tracked, routes)
    assert "packages/twenty-website" not in (tele.get("dev_artifact_units") or {})
