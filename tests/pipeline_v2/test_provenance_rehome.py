"""Track-A A1 — provenance re-home (Stage 6.98b).

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
    # only reached when enabled, but ts_ast guard + no view also no-op it.
    monkeypatch.setattr(adapter, "ts_ast_enabled", lambda: False)
    tele = PR.run_provenance_rehome(ufs, devs, pfs, ctx)
    assert tele["applied"] is False
    assert len(devs) == before
