"""Product-Spine W3.1 — D1 tiny-anchor giant-body sink fixes.

The fb3 fresh-blood dossier (2026-07-07) named the single biggest trust
failure: a 2-3-file route anchor becomes the designated owner of a huge
ownerless mass (supabase `Claim Project` 115K LOC at 1.00 outside-share;
documenso `UI` 0.84/60K; tracecat `status` 1.00/106K; comp `security`
0.996/39K). Diagnosis (W3.1, wave3-out + FAULTLINE_MINT_DEBUG live
votes) found FOUR cooperating defects:

  (a) IMPORT-FOLD followed dependency direction: every app dev's import
      majority lands on the shared component library (documenso `o`
      476/706 votes → ws:packages/ui) — imports say what a dev USES,
      not what it IS;
  (b) SPAN-VOTE bound giant devs through resolvable SLIVERS (comp
      `mcp-server` 34.5K LOC → the 3-file `security` route PF on a
      2-file vote), because unresolvable span mass was dropped from the
      denominator;
  (c) the flowful-never-lane LAW force-bound WORKSPACE-ANCHOR devs
      (supabase `studio`, 99.5K LOC / 478 flows) although the law's own
      ruler (validator I9) exempts them and conservation.py's dev-rehome
      states why ("anchors never move");
  (d) 6.7d ``_propagate_dev_map`` resurrected LANED split subfeatures
      into their parent's (mis)folded PF — 110K LOC of
      ``shell_lineage_only`` studio subs rode into `claim-project`.

Fixtures are DISTILLED from those real scans.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.models.types import Feature, Flow, MemberFile
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    build_platform_infrastructure_lane,
    run_anchored_mint,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def flow(name: str, entry: str, paths: list[str] | None = None) -> Flow:
    return Flow(
        name=name, entry_point_file=entry, paths=paths or [entry],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def dev(name: str, paths: list[str], flows: list[Flow] | None = None,
        **kw) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        member_files=[
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
            for p in paths
        ],
        flows=flows or [],
        product_feature_id="old-pf",
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, **kw,
    )


def ctx_of(workspaces=None, tracked=None, repo_path=".") -> SimpleNamespace:
    return SimpleNamespace(
        workspaces=workspaces, tracked_files=tracked or [],
        repo_path=Path(repo_path), monorepo=bool(workspaces),
    )


# ── (a) import-fold self-evidence guard ─────────────────────────────────


def _library_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    """apps/remix routes importing packages/ui — the documenso shape."""
    repo = tmp_path / "repo"
    ui_files = [f"packages/ui/{n}.tsx" for n in ("button", "card", "input")]
    embed_files = [
        f"apps/remix/app/routes/embed/{n}.tsx" for n in ("a", "b", "c", "d")
    ]
    for rel in ui_files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("export const C = 1;\n")
    for rel in embed_files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            'import { C } from "../../../../../packages/ui/button";\n'
            "export default function R() { return C; }\n"
        )
    tracked = ui_files + embed_files
    return repo, tracked


def test_import_fold_guard_blocks_library_target(tmp_path: Path) -> None:
    """documenso-embed class: a multi-file route dev whose imports
    majority-target the UI library must NOT fold into the library PF —
    it has no self-evidence there (0 own files inside ws:packages/ui).
    Flowless → it lands in the honest lane instead."""
    repo, tracked = _library_repo(tmp_path)
    ws = [SimpleNamespace(name="ui", path="packages/ui", stack="ts"),
          SimpleNamespace(name="remix", path="apps/remix", stack="ts")]
    routes = [{"pattern": "/documents", "method": "PAGE",
               "file": "apps/remix/app/routes/documents/index.tsx"}]
    ui = dev("ui", [f"packages/ui/{n}.tsx" for n in ("button", "card", "input")])
    embed = dev("embed", [
        f"apps/remix/app/routes/embed/{n}.tsx" for n in ("a", "b", "c", "d")
    ])
    docs = dev("documents", ["apps/remix/app/routes/documents/index.tsx"],
               flows=[flow("browse-documents-flow",
                           "apps/remix/app/routes/documents/index.tsx")])
    pfs, tele = run_anchored_mint(
        [ui, embed, docs], routes, ctx_of(ws, tracked, str(repo)))
    assert "ui" in {p.name for p in pfs}
    # the guard blocked the library bind: embed is NOT a ui member
    assert embed.product_feature_id != "ui", (
        "D1 REGRESSION: import-fold bound a route dev to the component "
        "library it merely imports")
    assert tele.get("fold_import_guard_blocked", 0) >= 1
    # flowless residue lanes honestly
    assert embed.product_feature_id is None and embed.shared_reason


def test_import_fold_stub_exemption_keeps_midday_i_class(
    tmp_path: Path,
) -> None:
    """The rung's original W2b purpose survives: a <= 3-file page STUB
    whose only content IS its imports still follows them (midday `i` →
    Invoices)."""
    repo = tmp_path / "repo"
    inv_files = [f"packages/invoice/{n}.ts" for n in ("template", "render")]
    stub = "apps/dashboard/src/app/(public)/i/page.tsx"
    for rel in inv_files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("export const T = 1;\n")
    sp = repo / stub
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(
        'import { T } from "../../../../../../packages/invoice/template";\n'
        "export default function P() { return T; }\n"
    )
    tracked = inv_files + [stub]
    ws = [SimpleNamespace(name="invoice", path="packages/invoice", stack="ts"),
          SimpleNamespace(name="dashboard", path="apps/dashboard", stack="ts")]
    routes = [{"pattern": "/invoices", "method": "PAGE",
               "file": "apps/dashboard/src/app/(dashboard)/invoices/page.tsx"}]
    invoices = dev(
        "invoices",
        inv_files + ["apps/dashboard/src/app/(dashboard)/invoices/page.tsx"],
        flows=[flow("browse-invoices-flow",
                    "apps/dashboard/src/app/(dashboard)/invoices/page.tsx")])
    i_dev = dev("i", [stub])
    pfs, tele = run_anchored_mint(
        [invoices, i_dev], routes, ctx_of(ws, tracked, str(repo)))
    assert i_dev.product_feature_id == invoices.product_feature_id, (
        "stub exemption broken: the midday `i` class must still "
        "import-fold into its capability")
    assert (i_dev.anchor_id or "").startswith("fold:import")


# ── (b) span-vote coherence guard ────────────────────────────────────────


def test_span_vote_guard_blocks_sliver_target() -> None:
    """comp mcp-server class: a flowful dev whose span resolves only
    through a tiny sliver (1/10 files → the `tiny` route PF) must not
    span-bind there; the walk rung then grounds it in the surrounding
    mass instead."""
    routes = [
        {"pattern": "/tiny", "method": "PAGE",
         "file": "frontend/src/app/tiny/page.tsx"},
        {"pattern": "/editor", "method": "PAGE",
         "file": "frontend/src/app/editor/page.tsx"},
    ]
    tiny = dev("tiny", ["frontend/src/app/tiny/page.tsx"],
               flows=[flow("view-tiny-flow", "frontend/src/app/tiny/page.tsx")])
    editor_files = [f"frontend/src/app/editor/{n}.tsx"
                    for n in ("page", "panel", "toolbar", "canvas", "store",
                              "hooks", "utils", "state")]
    editor = dev("editor", editor_files,
                 flows=[flow("edit-flow", "frontend/src/app/editor/page.tsx")])
    # the sink candidate: 10 owned files in a generated tree; its ONE
    # resolvable span file points at `tiny` — a 1/10 sliver.
    gen_files = [f"services/gen/src/{n}.ts" for n in
                 ("a", "b", "c", "d", "e", "f", "g", "h", "i")]
    gen = dev(
        "gen", gen_files + ["frontend/src/app/tiny/page.tsx"],
        flows=[flow("gen-flow", "services/gen/src/a.ts",
                    paths=gen_files + ["frontend/src/app/tiny/page.tsx"])])
    pfs, tele = run_anchored_mint([tiny, editor, gen], routes, ctx_of())
    assert gen.product_feature_id != "tiny", (
        "D1 REGRESSION: span-vote bound a 10-file dev through a 1-file "
        "sliver into the tiny page PF")
    # the law still holds — the flowful dev IS bound (walk plurality)
    assert gen.product_feature_id is not None
    assert (gen.anchor_id or "").startswith("fold:walk")


# ── (c) workspace-anchor devs lane honestly ──────────────────────────────


def test_ws_anchor_dev_lanes_honestly() -> None:
    """supabase `studio` class: the workspace-anchor dev (its flow
    sample spans the whole app) must never be force-bound into one
    capability — it lanes with its flows visible (validator I9 exempts
    workspace anchors from the flowful-in-lane class)."""
    routes = [
        {"pattern": "/claim-project", "method": "PAGE",
         "file": "apps/studio/pages/claim-project.tsx"},
    ]
    claim = dev("claim-project", ["apps/studio/pages/claim-project.tsx"],
                flows=[flow("claim-flow",
                            "apps/studio/pages/claim-project.tsx")])
    studio_files = [f"apps/studio/components/grid/{n}.tsx"
                    for n in ("a", "b", "c", "d", "e", "f")]
    studio = dev(
        "studio", studio_files,
        flows=[flow("sort-table-data-flow",
                    "apps/studio/components/grid/a.tsx",
                    paths=studio_files)],
        description="[package] workspace anchor 'studio' from monorepo "
                    "package 'apps/studio' (package.json name='studio')",
    )
    ws = [SimpleNamespace(name="studio", path="apps/studio", stack="ts")]
    pfs, tele = run_anchored_mint([claim, studio], routes, ctx_of(ws))
    assert studio.product_feature_id is None, (
        "D1 REGRESSION: the workspace-anchor dev was force-bound into "
        f"{studio.product_feature_id!r} — the claim-project sink class")
    assert studio.shared_reason == "shell_lineage_only"
    assert tele.get("law_ws_anchor_laned", 0) == 1
    assert tele.get("law_flowful_in_lane", 0) == 0
    rows = build_platform_infrastructure_lane([claim, studio])
    assert [r["name"] for r in rows] == ["studio"]
    assert rows[0]["flows"] == 1  # flows stay VISIBLE on the lane row


# ── (d) anchored 6.7d never propagates onto laned subs ───────────────────


def test_anchored_67d_skips_split_propagation() -> None:
    """The mint classified every split sub independently; a sub ABSENT
    from the anchored map is a deliberate lane verdict. 6.7d must not
    re-attach it through its parent's capability (the 110K supabase
    `claim-project` channel)."""
    from faultline.models.types import UserFlow
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        run_journey_abstraction,
    )

    routes = [{"pattern": "/settings", "method": "PAGE",
               "file": "app/settings/page.tsx"}]
    parent = dev("studio", ["app/settings/page.tsx"],
                 flows=[flow("edit-settings-flow", "app/settings/page.tsx")])
    sub = dev("data", ["apps/studio/data/a.ts", "apps/studio/data/b.ts"],
              description="sub-domain 'apps/studio/data' of feature 'studio'")
    pfs, _ = run_anchored_mint([parent, sub], routes, ctx_of())
    # test-validity guard: the mint laned the sub, mapped the parent
    assert parent.product_feature_id is not None
    assert sub.product_feature_id is None and sub.shared_reason

    uf = UserFlow(id="UF-001", name="Manage settings", resource="setting",
                  domain="settings",
                  product_feature_id=parent.product_feature_id,
                  intent="manage", member_flow_ids=["edit-settings-flow"],
                  member_count=1)
    payload = (
        '{"product_features":[{"name":"Settings","description":"ok"}],'
        '"user_flows":[{"name":"Manage settings","resource":"setting",'
        '"product_feature":"Settings","from_flows":["UF-001"],'
        '"from_dev_features":["studio"]}]}'
    )

    class _Cli:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            return SimpleNamespace(
                content=[SimpleNamespace(text=payload)],
                usage=SimpleNamespace(input_tokens=5, output_tokens=5))

    ufs2, pfs2, dev_map, tele = run_journey_abstraction(
        [uf], pfs, [parent, sub], routes, client=_Cli(), model="m",
        anchored=True)
    assert tele["applied"]
    assert "studio" in dev_map
    assert "data" not in dev_map, (
        "D1 REGRESSION: 6.7d propagated the parent's capability onto a "
        "LANED split sub — the phase_finalize re-stamp would empty the "
        "platform lane into the parent's PF")


def test_entry_fold_defers_to_l1_for_multi_page_fine_anchor() -> None:
    """tracecat Workspaces class: a workspace-scoped router
    (/workspaces/[id]/...) mints the coarse `workspaces` anchor; devs
    whose entries sit in a FINER >=2-page route surface (tables) must
    NOT entry-fold into the coarse PF — the defer hands them to rung L1
    which mints their own anchor on demand. A single-page sub-surface
    (the supabase FDW wrappers amendment case) still folds under its
    hosting capability — covered by the existing amendment test."""
    ws_root = "frontend/src/app/workspaces"
    routes = [
        {"pattern": "/workspaces", "method": "PAGE",
         "file": f"{ws_root}/page.tsx"},
        {"pattern": "/workspaces/:id/tables", "method": "PAGE",
         "file": f"{ws_root}/[workspaceId]/tables/page.tsx"},
        {"pattern": "/workspaces/:id/tables/:tableId", "method": "PAGE",
         "file": f"{ws_root}/[workspaceId]/tables/[tableId]/page.tsx"},
    ]
    workspaces = dev("workspaces", [f"{ws_root}/page.tsx"],
                     flows=[flow("browse-workspaces-flow",
                                 f"{ws_root}/page.tsx")])
    # tables: lineage-starved (files split between app tree and
    # components) so its winner is NONE/shell — entries all inside the
    # fine tables anchor, which sits inside the minted workspaces one.
    tables = dev("tables", [
        f"{ws_root}/[workspaceId]/tables/page.tsx",
        f"{ws_root}/[workspaceId]/tables/[tableId]/page.tsx",
        "frontend/src/components/tables/grid.tsx",
        "frontend/src/components/tables/cell.tsx",
        "frontend/src/lib/tables/query.ts",
    ], flows=[
        flow("browse-tables-flow",
             f"{ws_root}/[workspaceId]/tables/page.tsx"),
        flow("edit-table-flow",
             f"{ws_root}/[workspaceId]/tables/[tableId]/page.tsx"),
    ])
    pfs, tele = run_anchored_mint([workspaces, tables], routes, ctx_of())
    assert tables.product_feature_id != "workspaces", (
        "D1 REGRESSION: entry-fold bound a multi-page capability into "
        "the coarse workspace-scoped PF (tracecat Workspaces 46K class)")
    assert tables.product_feature_id is not None
    assert (tables.anchor_id or "").startswith("mint:entry-route"), (
        tables.anchor_id)
    assert tele.get("entry_fold_deferred_to_l1", 0) >= 1
