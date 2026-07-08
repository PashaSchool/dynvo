"""Track-A A1 — provenance re-home (Stage 6.885).

Fixtures distilled from the openstatus w6ast diagnosis: a THIN route-PF
(``email``, 9 owned LOC on ``api/internal/email``) whose journey recruits a
LANE sender (``apps/workflows/src/cron/emails.ts``) — the sender and the PF's
own code both import the first-party domain package ``@openstatus/emails``.
The re-home is confirmation-gated: provenance AND the journey layer must
BOTH agree, so it can only turn a foreign entry native (I16 ↓), never the
reverse (the regression class of a pre-UF re-home).
"""

from __future__ import annotations

from datetime import datetime, timezone

import faultline.pipeline_v2.ts_ast.adapter as adapter
import faultline.pipeline_v2.py_ast.adapter as py_adapter
from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2 import provenance_rehome as PR
from faultline.pipeline_v2.ts_ast.adapter import ProvenanceView
from faultline.pipeline_v2.ts_ast.shapes import ResolvedEdge

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

EMAIL_PKG = "packages/emails/src/client.tsx"
EMAIL_PF_FILE = "packages/notifications/email/src/index.ts"
SENDER = "apps/workflows/src/cron/emails.ts"


def _flow(name, entry, uuid, paths=None):
    return Flow(
        name=name, entry_point_file=entry, paths=paths or [entry], uuid=uuid,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def _dev(name, paths, *, pfid=None, flows=None, reason="shell_lineage_only"):
    return Feature(
        name=name, paths=list(paths), flows=flows or [],
        product_feature_id=pfid, shared_reason=(None if pfid else reason),
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def _pf(slug, paths, anchor_id):
    f = Feature(
        name=slug, paths=list(paths), flows=[],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, loc=9,
    )
    f.layer = "product"
    f.anchor_id = anchor_id
    return f


def _uf(uid, pfid, member_ids):
    return UserFlow(
        id=uid, name=f"Journey {uid}", intent="manage", resource="item",
        product_feature_id=pfid, member_flow_ids=list(member_ids),
        member_count=len(member_ids),
    )


def _view(edges_by_src: dict[str, list[tuple[str, str]]]) -> ProvenanceView:
    """Build a ProvenanceView from ``{src: [(target, resolution), ...]}``."""
    by_src: dict[str, dict[str, list[ResolvedEdge]]] = {}
    for src, edges in edges_by_src.items():
        slot = by_src.setdefault(src, {})
        for tgt, res in edges:
            slot.setdefault(tgt, []).append(ResolvedEdge(
                src_file=src, raw_target=tgt, target_file=tgt,
                resolution=res, via_barrels=(), names=("X",), kind="named",
            ))
    files = frozenset(by_src)
    return ProvenanceView(
        tracked_key=files, files=files, _by_src=by_src, _weights={})


class _Ctx:
    def __init__(self, tracked, repo="/repo"):
        self.tracked_files = list(tracked)
        self.repo_path = repo


def _patch(monkeypatch, view):
    monkeypatch.setattr(adapter, "ts_ast_enabled", lambda: True)
    monkeypatch.setattr(
        adapter, "repo_provenance", lambda root, tracked: view)


def _base_world(monkeypatch, *, extra_importer_edges=None):
    """The email world; return (ufs, devs, pfs, ctx). ``extra_importer_edges``
    lets a test add a SECOND PF importing the domain package (ambiguity)."""
    email_pf = _pf("email", [EMAIL_PF_FILE], "route:apps/web/api/internal/email")
    email_dev = _dev("email-owner", [EMAIL_PF_FILE], pfid="email")
    sender_flow = _flow("send-emails-flow", SENDER, "F1")
    lane_dev = _dev("workflows", [SENDER, "apps/workflows/src/cron/x.ts"],
                    flows=[sender_flow])
    edges = {
        EMAIL_PF_FILE: [(EMAIL_PKG, "workspace")],
        SENDER: [(EMAIL_PKG, "workspace"),
                 ("packages/db/src/index.ts", "workspace")],  # db = ambiguous
    }
    devs = [email_dev, lane_dev]
    pfs = [email_pf]
    if extra_importer_edges:
        edges.update(extra_importer_edges[0])
        devs.extend(extra_importer_edges[1])
        pfs.extend(extra_importer_edges[2])
    view = _view(edges)
    _patch(monkeypatch, view)
    ctx = _Ctx(list(edges) + ["packages/db/src/index.ts"])
    ufs = [_uf("UF-1", "email", ["F1"])]
    return ufs, devs, pfs, ctx


def test_lane_entry_rehomes_on_provenance_and_journey(monkeypatch) -> None:
    ufs, devs, pfs, ctx = _base_world(monkeypatch)
    tele = PR.run_provenance_rehome(ufs, devs, pfs, ctx)
    assert tele["entries_rehomed"] == 1
    assert tele["pfs_widened"] == 1
    # a new member dev under email owns the sender; lane dev released it
    rehomed = [d for d in devs if d.product_feature_id == "email"
               and SENDER in (d.paths or [])]
    assert rehomed, "sender not re-homed to email PF"
    lane = next(d for d in devs if d.name == "workflows")
    assert SENDER not in (lane.paths or []), "lane dev still owns sender"
    assert "apps/workflows/src/cron/x.ts" in (lane.paths or []), \
        "unrelated lane file must stay (conservation, only the entry moves)"
    # PF widened
    email_pf = next(p for p in pfs if p.name == "email")
    assert SENDER in (email_pf.paths or [])


def test_journey_conflict_blocks_rehome(monkeypatch) -> None:
    # the SAME entry is a member of two journeys on different PFs → abstain.
    ufs, devs, pfs, ctx = _base_world(monkeypatch)
    other = _pf("billing", ["packages/billing/x.ts"], "route:billing")
    pfs.append(other)
    ufs.append(_uf("UF-2", "billing", ["F1"]))
    tele = PR.run_provenance_rehome(ufs, devs, pfs, ctx)
    assert tele["entries_rehomed"] == 0
    assert tele["skipped_journey_conflict"] == 1


def test_ambiguous_domain_package_abstains(monkeypatch) -> None:
    # a SECOND PF's owned code also imports @openstatus/emails → the package
    # is no longer single-PF domain evidence → no attraction.
    extra = (
        {"packages/other/src/x.ts": [(EMAIL_PKG, "workspace")]},
        [_dev("other-owner", ["packages/other/src/x.ts"], pfid="other")],
        [_pf("other", ["packages/other/src/x.ts"], "route:other")],
    )
    ufs, devs, pfs, ctx = _base_world(monkeypatch, extra_importer_edges=extra)
    tele = PR.run_provenance_rehome(ufs, devs, pfs, ctx)
    assert tele["entries_rehomed"] == 0


def test_entry_owned_by_a_pf_is_not_contested(monkeypatch) -> None:
    ufs, devs, pfs, ctx = _base_world(monkeypatch)
    # give the sender a primary PF owner (billing) — must not be stolen.
    billing = _pf("billing", ["packages/billing/x.ts"], "route:billing")
    billing_dev = _dev("billing-owner", [SENDER], pfid="billing")
    pfs.append(billing)
    devs.append(billing_dev)
    tele = PR.run_provenance_rehome(ufs, devs, pfs, ctx)
    assert tele["entries_rehomed"] == 0
    assert tele["skipped_owned"] == 1


def test_only_workspace_resolution_counts(monkeypatch) -> None:
    # if the shared import resolves RELATIVE (same-app local), it is not a
    # cross-package domain signal → no attraction.
    email_pf = _pf("email", [EMAIL_PF_FILE], "route:email")
    email_dev = _dev("email-owner", [EMAIL_PF_FILE], pfid="email")
    sender_flow = _flow("send-emails-flow", SENDER, "F1")
    lane_dev = _dev("workflows", [SENDER], flows=[sender_flow])
    view = _view({
        EMAIL_PF_FILE: [(EMAIL_PKG, "relative")],   # NOT workspace
        SENDER: [(EMAIL_PKG, "relative")],
    })
    _patch(monkeypatch, view)
    ctx = _Ctx([EMAIL_PF_FILE, SENDER])
    tele = PR.run_provenance_rehome(
        [_uf("UF-1", "email", ["F1"])], [email_dev, lane_dev], [email_pf], ctx)
    assert tele["entries_rehomed"] == 0


def test_shared_infra_package_root_breaks_the_tie(monkeypatch) -> None:
    # The exact openstatus abstain: the sender imports @scope/emails (a DOMAIN
    # package, 1 PF) AND a FILE inside the SHARED @scope/db package that is
    # coincidentally imported by only one OTHER PF. File-level evidence ties
    # (email 1 : billing 1) → abstain. Package-root aggregation sees @scope/db
    # imported by MANY PFs (ambiguous) → only @scope/emails survives → email.
    import faultline.analyzer.import_graph as ig
    monkeypatch.setattr(
        ig, "detect_workspace_package_map",
        lambda root, deep=False: {
            "@scope/emails": "packages/emails", "@scope/db": "packages/db"})
    DBFILE = "packages/db/src/schema/integration.ts"
    email_pf = _pf("email", [EMAIL_PF_FILE], "route:email")
    email_dev = _dev("email-owner", [EMAIL_PF_FILE], pfid="email")
    billing_pf = _pf("billing", ["packages/billing/x.ts"], "route:billing")
    billing_dev = _dev("billing-owner", ["packages/billing/x.ts"], pfid="billing")
    monitors_pf = _pf("monitors", ["packages/monitors/x.ts"], "route:monitors")
    monitors_dev = _dev("monitors-owner", ["packages/monitors/x.ts"], pfid="monitors")
    sender_flow = _flow("send-emails-flow", SENDER, "F1")
    lane_dev = _dev("workflows", [SENDER], flows=[sender_flow])
    view = _view({
        EMAIL_PF_FILE: [(EMAIL_PKG, "workspace")],
        "packages/billing/x.ts": [(DBFILE, "workspace")],
        "packages/monitors/x.ts": [("packages/db/src/index.ts", "workspace")],
        SENDER: [(EMAIL_PKG, "workspace"), (DBFILE, "workspace")],
    })
    _patch(monkeypatch, view)
    ctx = _Ctx([EMAIL_PF_FILE, SENDER, DBFILE, "packages/billing/x.ts",
                "packages/monitors/x.ts", "packages/db/src/index.ts"])
    tele = PR.run_provenance_rehome(
        [_uf("UF-1", "email", ["F1"])],
        [email_dev, billing_dev, monitors_dev, lane_dev],
        [email_pf, billing_pf, monitors_pf], ctx)
    assert tele["entries_rehomed"] == 1, (
        "package-root aggregation should resolve the shared-db tie to email")
    assert any(d.product_feature_id == "email" and SENDER in (d.paths or [])
               for d in [email_dev, billing_dev, monitors_dev, lane_dev]
               ) or SENDER in (email_pf.paths or [])


def test_kill_switch_no_op(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_PROV_ATTACH", "0")
    assert PR.prov_attach_enabled() is False
    ufs, devs, pfs, ctx = _base_world(monkeypatch)
    before = len(devs)
    # the stage is skipped by the CALLER on flag off; the function itself is
    # only reached when enabled, but BOTH providers off also no-op it.
    monkeypatch.setattr(adapter, "ts_ast_enabled", lambda: False)
    monkeypatch.setattr(py_adapter, "py_ast_enabled", lambda: False)
    tele = PR.run_provenance_rehome(ufs, devs, pfs, ctx)
    assert tele["applied"] is False
    assert tele["enabled"] is False
    assert len(devs) == before


# ── Track-B integration: Python (py_ast) provenance re-home ──────────────
#
# The re-home now dispatches provenance by entry-file language: ``.py`` entries
# read the py_ast import graph (absolute first-party module imports), TS/JS the
# ts_ast graph — through ONE ``ProvenanceView`` interface. Python's domain
# "package" is the target's immediate directory (dir-is-package), mirroring the
# ts_ast pnpm package-root aggregation.

REC_MODEL = "apps/recruitment/models.py"
REC_SVC = "apps/recruitment/service.py"
JOB_TASK = "apps/jobs/tasks.py"          # lane entry in a SIBLING app dir


def _py_patch(monkeypatch, view, *, py_on=True) -> None:
    """py-only world: ts_ast off, py_ast on with a stubbed view."""
    monkeypatch.setattr(adapter, "ts_ast_enabled", lambda: False)
    monkeypatch.setattr(py_adapter, "py_ast_enabled", lambda: py_on)
    monkeypatch.setattr(
        py_adapter, "repo_provenance", lambda root, tracked: view)


def _py_world(monkeypatch, *, py_on=True):
    """A Django-shaped world: the ``recruitment`` PF's own service imports the
    recruitment domain models; a LANE job task (sibling ``apps/jobs``) recruited
    by the recruitment journey imports the SAME recruitment models."""
    rec_pf = _pf("recruitment", [REC_SVC], "route:apps/recruitment")
    rec_dev = _dev("recruitment-owner", [REC_SVC], pfid="recruitment")
    task_flow = _flow("run-recruitment-job", JOB_TASK, "F1")
    lane_dev = _dev("jobs", [JOB_TASK, "apps/jobs/other.py"], flows=[task_flow])
    view = _view({
        REC_SVC: [(REC_MODEL, "workspace")],
        JOB_TASK: [(REC_MODEL, "workspace")],
    })
    _py_patch(monkeypatch, view, py_on=py_on)
    ctx = _Ctx([REC_SVC, REC_MODEL, JOB_TASK, "apps/jobs/other.py"])
    ufs = [_uf("UF-1", "recruitment", ["F1"])]
    return ufs, [rec_dev, lane_dev], [rec_pf], ctx


def test_python_lane_entry_rehomes_via_py_ast(monkeypatch) -> None:
    ufs, devs, pfs, ctx = _py_world(monkeypatch)
    tele = PR.run_provenance_rehome(ufs, devs, pfs, ctx)
    assert tele["entries_rehomed"] == 1
    assert tele.get("confirmed_py") == 1
    assert tele.get("confirmed_ts", 0) == 0
    assert tele["providers"] == {"ts": False, "py": True}
    rehomed = [d for d in devs if d.product_feature_id == "recruitment"
               and JOB_TASK in (d.paths or [])]
    assert rehomed, "python lane entry not re-homed to its journey PF"
    lane = next(d for d in devs if d.name == "jobs")
    assert JOB_TASK not in (lane.paths or []), "lane dev still owns the entry"
    assert "apps/jobs/other.py" in (lane.paths or []), \
        "conservation: only the entry moves, sibling lane file stays"
    rec_pf = next(p for p in pfs if p.name == "recruitment")
    assert JOB_TASK in (rec_pf.paths or []), "PF not widened by the py re-home"


def test_python_kill_switch_py_ast_off(monkeypatch) -> None:
    # py_ast off (+ ts off) → no view built → byte-identical no-op.
    ufs, devs, pfs, ctx = _py_world(monkeypatch, py_on=False)
    before = len(devs)
    tele = PR.run_provenance_rehome(ufs, devs, pfs, ctx)
    assert tele["applied"] is False
    assert tele["enabled"] is False
    assert len(devs) == before


def test_python_journey_conflict_blocks_rehome(monkeypatch) -> None:
    # the SAME python entry is a member of journeys on two PFs → abstain.
    ufs, devs, pfs, ctx = _py_world(monkeypatch)
    other = _pf("payroll", ["apps/payroll/service.py"], "route:apps/payroll")
    pfs.append(other)
    ufs.append(_uf("UF-2", "payroll", ["F1"]))
    tele = PR.run_provenance_rehome(ufs, devs, pfs, ctx)
    assert tele["entries_rehomed"] == 0
    assert tele["skipped_journey_conflict"] == 1


def test_python_shared_package_dir_is_ambiguous(monkeypatch) -> None:
    # A shared python package DIRECTORY imported by TWO PFs is not single-PF
    # domain evidence — the immediate-dir package-root makes ``apps/common``
    # ambiguous, so a lane entry importing ONLY it abstains (ts 949114c parity).
    SHARED = "apps/common/db.py"
    rec_pf = _pf("recruitment", [REC_SVC], "route:apps/recruitment")
    rec_dev = _dev("recruitment-owner", [REC_SVC], pfid="recruitment")
    pay_pf = _pf("payroll", ["apps/payroll/service.py"], "route:apps/payroll")
    pay_dev = _dev("payroll-owner", ["apps/payroll/service.py"], pfid="payroll")
    task_flow = _flow("run-job", JOB_TASK, "F1")
    lane_dev = _dev("jobs", [JOB_TASK], flows=[task_flow])
    view = _view({
        REC_SVC: [(SHARED, "workspace")],
        "apps/payroll/service.py": [(SHARED, "workspace")],
        JOB_TASK: [(SHARED, "workspace")],   # lane entry imports ONLY shared
    })
    _py_patch(monkeypatch, view)
    ctx = _Ctx([REC_SVC, "apps/payroll/service.py", JOB_TASK, SHARED])
    tele = PR.run_provenance_rehome(
        [_uf("UF-1", "recruitment", ["F1"])],
        [rec_dev, pay_dev, lane_dev], [rec_pf, pay_pf], ctx)
    assert tele["entries_rehomed"] == 0, \
        "a shared package dir (2 PF importers) must not be single-PF evidence"


def test_python_relative_import_is_not_domain_evidence(monkeypatch) -> None:
    # py_ast classifies same-package PEP-328 imports as ``relative`` — those
    # are same-app locals, not cross-module domain evidence → no attraction.
    rec_pf = _pf("recruitment", [REC_SVC], "route:apps/recruitment")
    rec_dev = _dev("recruitment-owner", [REC_SVC], pfid="recruitment")
    task_flow = _flow("run-job", JOB_TASK, "F1")
    lane_dev = _dev("jobs", [JOB_TASK], flows=[task_flow])
    view = _view({
        REC_SVC: [(REC_MODEL, "relative")],   # NOT workspace
        JOB_TASK: [(REC_MODEL, "relative")],
    })
    _py_patch(monkeypatch, view)
    ctx = _Ctx([REC_SVC, REC_MODEL, JOB_TASK])
    tele = PR.run_provenance_rehome(
        [_uf("UF-1", "recruitment", ["F1"])], [rec_dev, lane_dev], [rec_pf], ctx)
    assert tele["entries_rehomed"] == 0


def test_mixed_repo_dispatches_each_entry_to_its_graph(monkeypatch) -> None:
    # A full-stack repo: the TS sender re-homes through ts_ast, the Python job
    # through py_ast — one stage, two graphs, dispatched by entry language.
    ts_view = _view({
        EMAIL_PF_FILE: [(EMAIL_PKG, "workspace")],
        SENDER: [(EMAIL_PKG, "workspace")],
    })
    py_view = _view({
        REC_SVC: [(REC_MODEL, "workspace")],
        JOB_TASK: [(REC_MODEL, "workspace")],
    })
    monkeypatch.setattr(adapter, "ts_ast_enabled", lambda: True)
    monkeypatch.setattr(adapter, "repo_provenance", lambda root, tracked: ts_view)
    monkeypatch.setattr(py_adapter, "py_ast_enabled", lambda: True)
    monkeypatch.setattr(
        py_adapter, "repo_provenance", lambda root, tracked: py_view)
    email_pf = _pf("email", [EMAIL_PF_FILE], "route:email")
    email_dev = _dev("email-owner", [EMAIL_PF_FILE], pfid="email")
    ts_flow = _flow("send-emails", SENDER, "F-TS")
    ts_lane = _dev("workflows", [SENDER], flows=[ts_flow])
    rec_pf = _pf("recruitment", [REC_SVC], "route:recruitment")
    rec_dev = _dev("recruitment-owner", [REC_SVC], pfid="recruitment")
    py_flow = _flow("run-job", JOB_TASK, "F-PY")
    py_lane = _dev("jobs", [JOB_TASK], flows=[py_flow])
    ctx = _Ctx([EMAIL_PF_FILE, SENDER, REC_SVC, REC_MODEL, JOB_TASK])
    ufs = [_uf("UF-TS", "email", ["F-TS"]), _uf("UF-PY", "recruitment", ["F-PY"])]
    tele = PR.run_provenance_rehome(
        ufs, [email_dev, ts_lane, rec_dev, py_lane], [email_pf, rec_pf], ctx)
    assert tele["entries_rehomed"] == 2
    assert tele["confirmed_ts"] == 1
    assert tele["confirmed_py"] == 1
    assert tele["providers"] == {"ts": True, "py": True}
