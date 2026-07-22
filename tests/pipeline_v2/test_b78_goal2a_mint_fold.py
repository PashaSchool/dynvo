"""B78-it2 Goal 2a — dev-standalone mint-vs-fold discriminator D.

Probe canon (experimenter 2026-07-22, SHIP/medium): the six Soc0
exhibits are pinned BY NAME — {context-items, trial, suggestions} mint
via their OWN clauses (c1 / c2 / c1), {audit, network-mock} fold into
the PF owning their real consumer (activity / network-security via
dir-attribution), {audit-events} demotes (zero consumers). Anti-cases:
the dedicated-dir scope guard keeps every twenty/langfuse zero-UI shape
out of D's scope, and the R3 ≥2-flow standalone law lives untouched.

The disk fixture distills the REAL Soc0 frontend (2026-07-22 repo
measurements): '/api' base split from path literals, the shared
``client.ts`` wrapper-method hop (``getSuggestions``), App.tsx +
AuthContext bootstrap wiring importing the whole client family, the
Firestore-backed trial surface invisible to C1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.models.types import Feature, Flow, MemberFile
from faultline.pipeline_v2.dev_mint_discriminator import (
    DVerdict,
    _url_tails,
    d_scope_guard,
    discriminate_dev_mint,
)
from faultline.pipeline_v2.stage_6_86_anchored_mint import run_anchored_mint

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def flow(name: str, entry: str) -> Flow:
    return Flow(
        name=name, entry_point_file=entry, paths=[entry], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_NOW,
        health_score=100.0,
    )


def dev(name: str, paths: list[str], flows: list[Flow] | None = None,
        pfid: str | None = "old-pf", **kw) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        member_files=[
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
            for p in paths
        ],
        flows=flows or [],
        product_feature_id=pfid,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, **kw,
    )


def _evw(monkeypatch):
    monkeypatch.setenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT", "1")


# ── the distilled Soc0 repo on disk ──────────────────────────────────────────

_FILES: dict[str, str] = {
    # backend routers — one SHARED dir hosting every domain's file
    "backend/routers/audit.py": "router = APIRouter(prefix='/api/audit')\n",
    "backend/routers/audit_events.py":
        "router = APIRouter(prefix='/api/audit-events')\n",
    "backend/routers/context_items.py":
        "router = APIRouter(prefix='/api/context-items')\n",
    "backend/routers/network_mock.py":
        "router = APIRouter()\n",
    "backend/routers/suggestions.py":
        "router = APIRouter(prefix='/api/suggestions')\n",
    "backend/routers/trial.py":
        "router = APIRouter(prefix='/api/trial')\n",
    "backend/routers/shared.py": "helpers = 1\n",
    # bootstrap wiring — imports the WHOLE api client family (fan-out)
    "frontend/src/App.tsx": """\
import { api } from '@/api/client';
import { setAuditBaseUrl } from '@/api/audit';
import { setAuditEventsBaseUrl } from '@/api/audit-events';
import { setContextItemsBaseUrl } from '@/api/context-items';
export function App() {
  setAuditBaseUrl('/x'); setAuditEventsBaseUrl('/x');
  setContextItemsBaseUrl('/x');
  return null;
}
""",
    "frontend/src/context/AuthContext.tsx": """\
import { setAuditBaseUrl } from '@/api/audit';
import { setAuditEventsBaseUrl } from '@/api/audit-events';
import { setContextItemsBaseUrl } from '@/api/context-items';
export function AuthProvider() { return null; }
""",
    # shared wrapper client — the getSuggestions method hop
    "frontend/src/api/client.ts": """\
export class ApiError extends Error {}
async function request<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`);
  return res.json();
}
export const api = {
  getSuggestions: () => request<unknown[]>('/suggestions'),
  acceptSuggestion: (id: string) => request(`/suggestions/${id}`),
};
""",
    # audit client — activity-stemmed data exports, audit-stemmed wiring
    "frontend/src/api/audit.ts": """\
let _baseUrl = '/api';
export function setAuditBaseUrl(url: string): void { _baseUrl = url; }
export function useActivityLog(filters: unknown) {
  return fetch(`${_baseUrl}/audit/logs?limit=50`);
}
export function useActivityFacets() {
  return fetch(_baseUrl + '/audit/facets');
}
""",
    # audit-events client — DEAD data export (bootstrap wiring only)
    "frontend/src/api/audit-events.ts": """\
let _baseUrl = '/api';
export function setAuditEventsBaseUrl(url: string): void { _baseUrl = url; }
export function useAuditEvents(filters: unknown) {
  return fetch(`${_baseUrl}/audit-events?limit=50`);
}
""",
    # context-items client — multi-line api object (enclosing-decl grain)
    "frontend/src/api/context-items.ts": """\
let _baseUrl = '/api';
export function setContextItemsBaseUrl(url: string): void { _baseUrl = url; }
async function request<T>(path: string): Promise<T> {
  const res = await fetch(`${_baseUrl}${path}`);
  return res.json();
}
export const contextItemsApi = {
  async list() {
    return request<unknown[]>('/context-items');
  },
  async create(payload: unknown) {
    return request<unknown>('/context-items');
  },
};
""",
    # real consumers
    "frontend/src/pages/ActivityLogPage.tsx": """\
import { useActivityLog, useActivityFacets } from '@/api/audit';
export function ActivityLogPage() {
  const rows = useActivityLog({});
  const facets = useActivityFacets();
  return null;
}
""",
    "frontend/src/pages/SuggestionsPage.tsx": """\
import { api } from '@/api/client';
export function SuggestionsPage() {
  const data = api.getSuggestions();
  return null;
}
""",
    "frontend/src/components/chat/ContextMemoryPanel.tsx": """\
import { contextItemsApi } from '@/api/context-items';
export function ContextMemoryPanel() {
  const items = contextItemsApi.list();
  return null;
}
""",
    # trial surface — Firestore-backed, C1-invisible, trial-stemmed UI
    "frontend/src/components/shared/TrialBanner.tsx": """\
export function TrialBanner() { return null; }
""",
    # single-token trap: 'mock' UI must never C2-mint network-mock
    "frontend/src/components/shared/MockPanel.tsx": """\
export function MockPanel() { return null; }
""",
    # network-mock client + its query0 consumers
    "frontend/src/modules/network-security/services/query0/mockMode.ts": """\
let _flag = false;
export function setMockMode(v: boolean): void { _flag = v; }
export function getMockMode(): boolean {
  fetch('/api/mock-data/flag');
  return _flag;
}
""",
    "frontend/src/modules/network-security/services/query0/client.ts": """\
import { getMockMode, setMockMode } from './mockMode';
export function runQuery() {
  if (getMockMode()) { setMockMode(true); }
  return null;
}
""",
    "frontend/src/modules/network-security/services/query0/getTableData.ts":
        "export function getTableData() { return []; }\n",
    # healthy anchor pages
    "frontend/src/pages/CasesPage.tsx":
        "export function CasesPage() { return null; }\n",
    "frontend/src/pages/CaseDetail.tsx":
        "export function CaseDetail() { return null; }\n",
}

_ROUTES = [
    {"pattern": "/cases", "method": "PAGE",
     "file": "frontend/src/pages/CasesPage.tsx"},
    {"pattern": "/activity", "method": "PAGE",
     "file": "frontend/src/pages/ActivityLogPage.tsx"},
    {"pattern": "/network-security", "method": "PAGE",
     "file": "frontend/src/modules/network-security/services/query0/client.ts"},
    {"pattern": "/api/audit/logs", "method": "GET",
     "file": "backend/routers/audit.py"},
    {"pattern": "/api/audit/facets", "method": "GET",
     "file": "backend/routers/audit.py"},
    {"pattern": "/api/audit-events", "method": "GET",
     "file": "backend/routers/audit_events.py"},
    {"pattern": "/api/context-items", "method": "GET",
     "file": "backend/routers/context_items.py"},
    {"pattern": "/api/mock-data/flag", "method": "GET",
     "file": "backend/routers/network_mock.py"},
    {"pattern": "/api/mock-data/query", "method": "POST",
     "file": "backend/routers/network_mock.py"},
    {"pattern": "/api/suggestions", "method": "GET",
     "file": "backend/routers/suggestions.py"},
    {"pattern": "/api/suggestions", "method": "POST",
     "file": "backend/routers/suggestions.py"},
    {"pattern": "/api/trial/status", "method": "GET",
     "file": "backend/routers/trial.py"},
]


@pytest.fixture()
def soc0_repo(tmp_path: Path) -> SimpleNamespace:
    for rel, text in _FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return SimpleNamespace(
        workspaces=None, monorepo=False,
        repo_path=tmp_path,
        tracked_files=sorted(_FILES),
    )


def _soc0_devs() -> list[Feature]:
    return [
        dev("cases", ["frontend/src/pages/CasesPage.tsx",
                      "frontend/src/pages/CaseDetail.tsx"],
            flows=[flow("browse-cases-flow",
                        "frontend/src/pages/CasesPage.tsx")]),
        dev("activity", ["frontend/src/pages/ActivityLogPage.tsx"],
            flows=[flow("view-activity-flow",
                        "frontend/src/pages/ActivityLogPage.tsx")]),
        dev("network-security",
            ["frontend/src/modules/network-security/services/query0/client.ts",
             "frontend/src/modules/network-security/services/query0/getTableData.ts"],
            flows=[flow("run-query-flow",
                        "frontend/src/modules/network-security/services/query0/client.ts")]),
        # REAL Soc0 dev names carry the api- prefix + suffixes — the
        # census lesson: D's domain identity must be the WOULD-BE PF
        # name (slug of the elected anchor display), never the dev name
        # ("apicontextitems" matches no consumed symbol).
        dev("api-audit", ["backend/routers/audit.py"],
            flows=[flow("view-audit-flow", "backend/routers/audit.py"),
                   flow("filter-audit-flow", "backend/routers/audit.py")]),
        dev("api-audit-events", ["backend/routers/audit_events.py"],
            flows=[flow("view-audit-events-flow",
                        "backend/routers/audit_events.py"),
                   flow("filter-audit-events-flow",
                        "backend/routers/audit_events.py")]),
        dev("api-context-items", ["backend/routers/context_items.py"],
            flows=[flow("browse-items-flow",
                        "backend/routers/context_items.py"),
                   flow("edit-items-flow",
                        "backend/routers/context_items.py")]),
        dev("network-mock", ["backend/routers/network_mock.py"],
            flows=[flow("serve-mock-flow", "backend/routers/network_mock.py"),
                   flow("reset-mock-flow",
                        "backend/routers/network_mock.py")]),
        dev("api-suggestions", ["backend/routers/suggestions.py"],
            flows=[flow("browse-suggestions-flow",
                        "backend/routers/suggestions.py"),
                   flow("accept-suggestion-flow",
                        "backend/routers/suggestions.py")]),
        dev("api-trial-status", ["backend/routers/trial.py"],
            flows=[flow("check-trial-flow", "backend/routers/trial.py")]),
    ]


def _mint(devs, ctx):
    return run_anchored_mint(devs, list(_ROUTES), ctx,
                             extractor_signals=None, nav_keys=frozenset())


# ── the six exhibits, by name ────────────────────────────────────────────────


def test_exhibit_context_items_mints_via_c1(monkeypatch, soc0_repo):
    """contextItemsApi is consumed by ContextMemoryPanel — C1
    stem-consumption keeps the mint (the protected set lives)."""
    _evw(monkeypatch)
    devs = _soc0_devs()
    pfs, tele = _mint(devs, soc0_repo)
    items = next(d for d in devs if d.name == "api-context-items")
    assert items.product_feature_id == "context-items"
    assert any(p.name == "context-items" for p in pfs)
    rows = {r["dev"]: r for r in tele.get("walk_evidence_d_rows", [])}
    assert rows["api-context-items"]["verdict"] == "mint"
    assert rows["api-context-items"]["via"] == "c1:contextItemsApi"


def test_exhibit_suggestions_mints_via_c1_wrapper_hop(monkeypatch, soc0_repo):
    """The shared client.ts wrapper hop: getSuggestions (a same-line
    object method, not a top-level export) consumed by SuggestionsPage."""
    _evw(monkeypatch)
    devs = _soc0_devs()
    pfs, tele = _mint(devs, soc0_repo)
    sugg = next(d for d in devs if d.name == "api-suggestions")
    assert sugg.product_feature_id == "suggestions"
    rows = {r["dev"]: r for r in tele.get("walk_evidence_d_rows", [])}
    assert rows["api-suggestions"]["verdict"] == "mint"
    assert rows["api-suggestions"]["via"] == "c1:getSuggestions"


def test_exhibit_trial_mints_via_c2_stem_ui(monkeypatch, soc0_repo):
    """Trial's surface reads Firestore — C1 sees nothing; the
    trial-stemmed UI file (TrialBanner) is the honest evidence (C2)."""
    _evw(monkeypatch)
    devs = _soc0_devs()
    pfs, tele = _mint(devs, soc0_repo)
    trial = next(d for d in devs if d.name == "api-trial-status")
    assert trial.product_feature_id == "trial"
    rows = {r["dev"]: r for r in tele.get("walk_evidence_d_rows", [])}
    assert rows["api-trial-status"]["verdict"] == "mint"
    assert rows["api-trial-status"]["via"] == "c2:frontend/src/components/shared/TrialBanner.tsx"
    assert tele.get("walk_evidence_d_mint_c2") == 1


def test_exhibit_audit_folds_into_activity_owner(monkeypatch, soc0_repo):
    """audit's consumed symbols are activity-stemmed (useActivityLog →
    ActivityLogPage): no C1 mint, no audit-stem UI — the dev FOLDS into
    the PF owning its consumer, via dir-attribution (never membership)."""
    _evw(monkeypatch)
    devs = _soc0_devs()
    pfs, tele = _mint(devs, soc0_repo)
    audit = next(d for d in devs if d.name == "api-audit")
    assert audit.product_feature_id == "activity"
    assert (audit.anchor_id or "").startswith("fold:consumer-evidence")
    assert not any(p.name == "audit" for p in pfs)
    rows = {r["dev"]: r for r in tele.get("walk_evidence_d_rows", [])}
    assert rows["api-audit"]["verdict"] == "fold"
    assert rows["api-audit"].get("target")  # dir-attributed owner recorded
    # the fold landed on the SAME PF the activity dev mints under:
    act = next(d for d in devs if d.name == "activity")
    assert audit.product_feature_id == act.product_feature_id


def test_exhibit_network_mock_folds_into_network_security(
        monkeypatch, soc0_repo):
    """network-mock's client (mockMode.ts) is consumed inside query0/ —
    fold lands on network-security. The single-token trap is pinned:
    MockPanel.tsx exists, but 'mock' is not the compound stem
    'networkmock', so C2 never fires (probe risk 3)."""
    _evw(monkeypatch)
    devs = _soc0_devs()
    pfs, tele = _mint(devs, soc0_repo)
    mock = next(d for d in devs if d.name == "network-mock")
    assert mock.product_feature_id == "network-security"
    assert (mock.anchor_id or "").startswith("fold:consumer-evidence")
    assert not any(p.name == "network-mock" for p in pfs)
    rows = {r["dev"]: r for r in tele.get("walk_evidence_d_rows", [])}
    assert rows["network-mock"]["verdict"] == "fold"


def test_exhibit_audit_events_demotes(monkeypatch, soc0_repo):
    """audit-events: useAuditEvents has ZERO consumers (App.tsx and
    AuthContext import the client only as bootstrap wiring — the
    fan-out exclusion) — the dev demotes: NOT a PF, pre-gate
    disposition kept."""
    _evw(monkeypatch)
    devs = _soc0_devs()
    pfs, tele = _mint(devs, soc0_repo)
    ae = next(d for d in devs if d.name == "api-audit-events")
    assert not any(p.name == "audit-events" for p in pfs)
    assert ae.product_feature_id != "audit-events"
    rows = {r["dev"]: r for r in tele.get("walk_evidence_d_rows", [])}
    assert rows["api-audit-events"]["verdict"] == "demote"
    assert tele.get("walk_evidence_d_demote") == 1


def test_exhibit_protected_set_alive_demote_set_gone(monkeypatch, soc0_repo):
    """The headline 6/6: protected PFs live, demote-set PFs gone."""
    _evw(monkeypatch)
    devs = _soc0_devs()
    pfs, tele = _mint(devs, soc0_repo)
    names = {p.name for p in pfs}
    assert {"context-items", "suggestions", "trial"} <= names
    assert not names & {"audit", "audit-events", "network-mock"}
    assert tele.get("walk_evidence_d_candidates") == 6
    assert tele.get("walk_evidence_d_mint_c1") == 2
    assert tele.get("walk_evidence_d_mint_c2") == 1
    assert tele.get("walk_evidence_d_fold") == 2
    assert tele.get("walk_evidence_d_demote") == 1


# ── anti-cases ───────────────────────────────────────────────────────────────


def test_anticase_dedicated_dir_shapes_out_of_scope():
    """twenty/langfuse zero-UI shapes: every legit small PF carries a
    dedicated domain dir — the scope guard yields 0 candidates (probe:
    18 and 31 zero-UI PFs, 0 in scope after the guard)."""
    tracked = [
        "packages/twenty-server/src/modules/messaging/jobs/reimport.job.ts",
        "packages/twenty-server/src/modules/messaging/listeners/blocklist.listener.ts",
        "packages/twenty-server/src/modules/calendar/services/composer.service.ts",
        "packages/shared/src/features/blobstorage/gzipStream.ts",
        "packages/shared/src/features/blobstorage/handleJob.ts",
        "worker/src/features/deleted-mask-cleaner/helpers.ts",
        "worker/src/features/deleted-mask-cleaner/index.ts",
    ]
    exts = (".ts", ".py")
    # stem-containment dir ruler
    assert not d_scope_guard(
        "messaging",
        ["packages/twenty-server/src/modules/messaging/jobs/reimport.job.ts",
         "packages/twenty-server/src/modules/messaging/listeners/blocklist.listener.ts"],
        tracked, exts)
    assert not d_scope_guard(
        "blobstorage",
        ["packages/shared/src/features/blobstorage/gzipStream.ts",
         "packages/shared/src/features/blobstorage/handleJob.ts"],
        tracked, exts)
    assert not d_scope_guard(
        "deleted-mask-cleaner",
        ["worker/src/features/deleted-mask-cleaner/helpers.ts",
         "worker/src/features/deleted-mask-cleaner/index.ts"],
        tracked, exts)
    # first-token dir ruler (calendar under modules/calendar/)
    assert not d_scope_guard(
        "calendar-sync",
        ["packages/twenty-server/src/modules/calendar/services/composer.service.ts"],
        tracked, exts)


def test_anticase_scope_guard_floors():
    """>3 members, UI members, unverifiable shared-dir: all decline."""
    tracked = ["backend/routers/a.py", "backend/routers/b.py",
               "backend/routers/c.py", "backend/routers/d.py",
               "backend/routers/e.py", "src/ui/Widget.tsx",
               "src/ui/Other.tsx"]
    exts = (".py", ".tsx")
    # 4 members — out
    assert not d_scope_guard(
        "many", ["backend/routers/a.py", "backend/routers/b.py",
                 "backend/routers/c.py", "backend/routers/d.py"],
        tracked, exts)
    # own UI member — out
    assert not d_scope_guard(
        "widget", ["src/ui/Widget.tsx"], tracked, exts)
    # empty tracked listing — the shared-dir condition is unverifiable
    assert not d_scope_guard("solo", ["backend/routers/a.py"], [], exts)
    # sole file in its dir — not a shared dir
    assert not d_scope_guard(
        "lonely", ["lib/lonely/only.py"],
        ["lib/lonely/only.py", "backend/routers/a.py"], (".py",))
    # the honest in-scope shape still passes
    assert d_scope_guard(
        "audit", ["backend/routers/a.py"], tracked, exts)


def test_anticase_r3_standalone_with_dedicated_dir_lives(
        monkeypatch, soc0_repo, tmp_path):
    """R3 law: a ≥2-flow dev with its OWN dedicated dir is out of D's
    scope — the standalone mint stands even with D armed on a full
    repo context."""
    _evw(monkeypatch)
    (tmp_path / "backend/billing").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend/billing/router.py").write_text(
        "router = 1\n", encoding="utf-8")
    (tmp_path / "backend/billing/service.py").write_text(
        "svc = 1\n", encoding="utf-8")
    ctx = SimpleNamespace(
        workspaces=None, monorepo=False, repo_path=tmp_path,
        tracked_files=sorted(_FILES) + ["backend/billing/router.py",
                                        "backend/billing/service.py"],
    )
    devs = _soc0_devs() + [
        dev("billing", ["backend/billing/router.py"],
            flows=[flow("invoice-flow", "backend/billing/router.py"),
                   flow("refund-flow", "backend/billing/router.py")]),
    ]
    pfs, tele = _mint(devs, ctx)
    billing = next(d for d in devs if d.name == "billing")
    assert billing.product_feature_id == "billing"
    assert billing.shared_reason is None  # R3: never laned
    rows = {r["dev"]: r for r in tele.get("walk_evidence_d_rows", [])}
    assert "billing" not in rows  # out of scope — D never judged it


def test_anticase_url_boundary_audit_never_matches_audit_events(
        soc0_repo):
    """/api/audit tails never hit the audit-events client (route
    boundary guard) — audit's consumers are exactly ActivityLogPage."""
    def read(rel: str) -> str | None:
        p = Path(soc0_repo.repo_path) / rel
        return p.read_text(encoding="utf-8") if p.exists() else None

    v = discriminate_dev_mint(
        "audit", ["backend/routers/audit.py"],
        ["/api/audit/logs", "/api/audit/facets"],
        soc0_repo.tracked_files, read, (".py", ".ts", ".tsx"))
    assert v.kind == "fold"
    assert v.consumer_files == ("frontend/src/pages/ActivityLogPage.tsx",)


def test_url_tails_keep_half_the_segments():
    """Scale-invariant tail law: /api/audit/logs never yields the
    generic /logs; param segments are cut at the boundary."""
    assert _url_tails(["/api/audit/logs/{id}"]) == [
        "/api/audit/logs", "/audit/logs"]
    assert set(_url_tails(["/api/suggestions"])) == {
        "/api/suggestions", "/suggestions"}
    assert "/logs" not in _url_tails(
        ["/api/audit/logs", "/api/audit/facets"])
    assert _url_tails(["/"]) == []


def test_flag_off_keeps_d_silent(monkeypatch, soc0_repo):
    """Kill-switch law: unset == 0 — no D telemetry, no D dispositions
    (the walk-evidence gate itself is off)."""
    for env in (None, "0"):
        if env is None:
            monkeypatch.delenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT",
                               raising=False)
        else:
            monkeypatch.setenv("FAULTLINE_FOLD_EVIDENCE_WEIGHT", env)
        devs = _soc0_devs()
        pfs, tele = _mint(devs, soc0_repo)
        assert not any(k.startswith("walk_evidence_d_") for k in tele)
        for d in devs:
            assert not (d.anchor_id or "").startswith(
                "fold:consumer-evidence")


def test_dverdict_shape():
    v = DVerdict(kind="demote")
    assert v.via is None and v.consumer_files == ()
