"""Tests for faultline.pipeline_v2.e2e_truth (Stage 6.98).

Fixture repos are built in tmp_path; corpus-shaped snippets mirror the
real classes seen in recon: documenso ([TAG] titles + redirectPath +
options-object arg), supabase (describe > it + toUrl template gotos),
typebot (weak "should work as expected" titles + param-only URLs),
vitest lookalike (must NOT be discovered).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from faultline.pipeline_v2.e2e_truth import (
    E2E_TRUTH_ENV,
    _mask,
    e2e_truth_enabled,
    extract_e2e_journeys,
    run_e2e_truth,
    scan_meta_view,
    stitch_journeys,
)

PW = 'import { expect, test } from "@playwright/test";\n'


def w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def uf(id_: str, name: str, routes: list[str], resource: str = "",
       pf: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=id_, name=name, routes=routes, resource=resource,
        product_feature_id=pf,
    )


# -- flag ------------------------------------------------------------------

def test_flag_default_on(monkeypatch):
    monkeypatch.delenv(E2E_TRUTH_ENV, raising=False)
    assert e2e_truth_enabled()
    monkeypatch.setenv(E2E_TRUTH_ENV, "0")
    assert not e2e_truth_enabled()
    monkeypatch.setenv(E2E_TRUTH_ENV, "false")
    assert not e2e_truth_enabled()


# -- discovery -------------------------------------------------------------

def test_discovery_content_gated(tmp_path):
    w(tmp_path, "playwright.config.ts", "export default {};")
    w(tmp_path, "src/test/pay.spec.ts",
      PW + 'test("pays", async ({ page }) => { await page.goto("/pay"); });\n')
    # vitest unit spec — same suffix, no playwright import → rejected
    w(tmp_path, "src/unit/math.spec.ts",
      'import { it } from "vitest";\nit("adds", () => {});\n')
    # node_modules always skipped
    w(tmp_path, "node_modules/x/y.spec.ts", PW + 'test("no", () => {});\n')
    journeys, tele = extract_e2e_journeys(tmp_path)
    assert tele["spec_files"] == 1
    assert [j.file for j in journeys] == ["src/test/pay.spec.ts"]
    assert journeys[0].runner_project == "."


def test_discovery_cypress(tmp_path):
    w(tmp_path, "cypress.config.js", "module.exports = {};")
    w(tmp_path, "cypress/e2e/login.cy.ts",
      'describe("Login", () => {\n'
      '  it("logs in", () => { cy.visit("/login"); cy.get("#u").type("x");'
      ' });\n});\n')
    journeys, tele = extract_e2e_journeys(tmp_path)
    assert tele["runner_projects"]["."]["runner"] == "cypress"
    assert journeys[0].title_chain == ("Login", "logs in")
    assert journeys[0].urls_touched == ["/login"]


# -- parser ----------------------------------------------------------------

def test_nested_describe_chain_and_configure_skipped(tmp_path):
    w(tmp_path, "playwright.config.ts", "")
    w(tmp_path, "a.spec.ts", PW + (
        "test.describe.configure({ mode: 'serial' });\n"
        "test.describe('Database Webhooks', () => {\n"
        "  test.describe('editing', () => {\n"
        "    test('can edit a webhook', async ({ page }) => {\n"
        "      await page.goto('/project/abc/integrations/webhooks');\n"
        "    });\n"
        "  });\n"
        "  test('can view webhooks list page', async ({ page }) => {\n"
        "    await page.goto('/project/abc/integrations/webhooks/list');\n"
        "  });\n"
        "});\n"))
    journeys, _ = extract_e2e_journeys(tmp_path)
    chains = [j.title_chain for j in journeys]
    assert ("Database Webhooks", "editing", "can edit a webhook") in chains
    assert ("Database Webhooks", "can view webhooks list page") in chains
    assert all(c[0] != "configure" for c in chains)


def test_options_object_not_taken_as_body(tmp_path):
    w(tmp_path, "playwright.config.ts", "")
    w(tmp_path, "opt.spec.ts", PW + (
        "test('tagged', { tag: '@smoke' }, async ({ page }) => {\n"
        "  await page.goto('/billing');\n"
        "});\n"))
    journeys, _ = extract_e2e_journeys(tmp_path)
    assert journeys[0].urls_touched == ["/billing"]


def test_template_urls_and_redirect_and_bare_literals(tmp_path):
    w(tmp_path, "playwright.config.ts", "")
    w(tmp_path, "t.spec.ts", PW + (
        "test('share', async ({ page }) => {\n"
        "  await page.goto(`/${typebotId}-public`);\n"
        "  await page.goto(toUrl(`/project/${ref}/auth/users?show=${id}`));\n"
        "  await apiSignin({ page, redirectPath: `/t/${team.url}/documents` });\n"
        "  await expect(page).toHaveURL('/settings/profile');\n"
        "  await page.getByRole('button', { name: 'Delete' }).click();\n"
        "  await page.getByPlaceholder('Email').fill('a@b.c');\n"
        "});\n"))
    journeys, _ = extract_e2e_journeys(tmp_path)
    j = journeys[0]
    assert "/project/:param/auth/users" in j.urls_touched   # query stripped
    assert "/t/:param/documents" in j.urls_touched          # redirectPath
    assert "/settings/profile" in j.urls_touched            # bare literal
    assert "/:param-public" in j.urls_touched               # template goto
    kinds = [s["kind"] for s in j.steps]
    assert "goto" in kinds and "click" in kinds and "fill" in kinds
    click = next(s for s in j.steps if s["kind"] == "click")
    assert click["arg"] == "button"


def test_comment_and_string_masking(tmp_path):
    w(tmp_path, "playwright.config.ts", "")
    w(tmp_path, "m.spec.ts", PW + (
        "// test('commented out', () => {})\n"
        "/* it('also dead', () => {}) */\n"
        "test('real } brace in title', async ({ page }) => {\n"
        "  const s = 'a string with test(\"fake\") inside';\n"
        "  await page.goto('/real');\n"
        "});\n"))
    journeys, _ = extract_e2e_journeys(tmp_path)
    assert len(journeys) == 1
    assert journeys[0].title_chain == ("real } brace in title",)
    assert journeys[0].urls_touched == ["/real"]


def test_mask_preserves_length_and_code_braces():
    src = "const a = { b: `x${y}z` }; // {dead}\n"
    clean = _mask(src)
    assert len(clean) == len(src)
    assert clean.count("{") == 1 and clean.count("}") == 1  # only code braces


def test_asset_urls_dropped(tmp_path):
    w(tmp_path, "playwright.config.ts", "")
    w(tmp_path, "a.spec.ts", PW + (
        "test('x', async ({ page }) => {\n"
        "  await page.goto('/dash');\n"
        "  await page.setInput('/fixtures/doc.pdf');\n"
        "});\n"))
    journeys, _ = extract_e2e_journeys(tmp_path)
    assert journeys[0].urls_touched == ["/dash"]


# -- stitching -------------------------------------------------------------

def test_route_family_match_tenancy_transparent():
    j, _ = _journeys_of(
        "test('sends doc', async ({ page }) => {"
        " await page.goto('/t/:param/documents'); });")
    ufs = [
        uf("UF-001", "Send documents", ["/t/[teamUrl]/documents"], "document",
           pf="documents"),
        uf("UF-002", "Manage billing", ["/settings/billing"], "billing"),
    ]
    out = stitch_journeys(j, ufs)
    assert len(out["matched"]) == 1
    row = out["matched"][0]
    assert row["uf_id"] == "UF-001" and row["via"] == "route"
    assert row["product_feature_id"] == "documents"
    assert "UF-001" in out["uf_e2e_evidence"]


def test_routes_index_lane_matches_when_uf_routes_empty():
    """The real-corpus class: UserFlow.routes is EMPTY on cold scans
    (papermark 0/52, typebot 0/100) — matching must ride
    routes_index → handler file → flow → UF instead."""
    j, _ = _journeys_of(
        "test('can create a new webhook', async ({ page }) => {"
        " await page.goto('/project/abc/integrations/webhooks/new'); });")
    ufs = [uf("UF-009", "Manage webhooks", [], "webhook", pf="integrations")]
    routes_index = [{
        "pattern": "/project/[ref]/integrations/webhooks",
        "file": "apps/studio/pages/project/[ref]/integrations/webhooks.tsx",
        "method": "PAGE",
    }]
    flows = [SimpleNamespace(
        uuid="fl-1", name="create-webhook-flow",
        entry_point_file="apps/studio/pages/project/[ref]/integrations/webhooks.tsx",
        paths=["apps/studio/pages/project/[ref]/integrations/webhooks.tsx"],
        user_flow_id="UF-009",
    )]
    out = stitch_journeys(j, ufs, routes_index, flows)
    assert out["matched"] and out["matched"][0]["uf_id"] == "UF-009"
    assert out["matched"][0]["via"] == "route"


def test_routes_index_lane_via_member_flow_ids():
    """Flow lacks user_flow_id; UF claims it via member_flow_ids —
    reverse direction of the flow↔UF map."""
    j, _ = _journeys_of(
        "test('sees billing page', async ({ page }) => {"
        " await page.goto('/settings/billing'); });")
    ufs = [uf("UF-002", "Manage billing", [], "billing")]
    ufs[0].member_flow_ids = ["billing-flow"]
    routes_index = [{"pattern": "/settings/billing",
                     "file": "app/settings/billing/page.tsx",
                     "method": "PAGE"}]
    flows = [SimpleNamespace(
        uuid=None, name="billing-flow",
        entry_point_file="app/settings/billing/page.tsx",
        paths=["app/settings/billing/page.tsx"], user_flow_id=None)]
    out = stitch_journeys(j, ufs, routes_index, flows)
    assert out["matched"] and out["matched"][0]["uf_id"] == "UF-002"


def test_orphan_journey_reported():
    j, _ = _journeys_of(
        "test('exports audit log', async ({ page }) => {"
        " await page.goto('/audit-log/export'); });")
    out = stitch_journeys(j, [uf("UF-001", "Manage billing",
                                 ["/settings/billing"])])
    assert not out["matched"]
    assert [o.title_chain for o in out["orphans"]] == [("exports audit log",)]


def test_name_fallback_only_without_route_evidence():
    # param-only URL → no route families → falls back to token overlap
    j, _ = _journeys_of(
        "test('should work as expected', async ({ page }) => {"
        " await page.goto(`/${id}-public`); });", fname="payment.spec.ts")
    ufs = [uf("UF-007", "Collect payment", [], "payment")]
    out = stitch_journeys(j, ufs)
    assert out["matched"] and out["matched"][0]["via"] == "name"
    assert out["matched"][0]["uf_id"] == "UF-007"


def _journeys_of(body: str, fname: str = "x.spec.ts"):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        w(root, "playwright.config.ts", "")
        w(root, fname, PW + body + "\n")
        return extract_e2e_journeys(root)


# -- stage entry / payload --------------------------------------------------

def test_e2e_absent_repo(tmp_path):
    w(tmp_path, "src/app/page.tsx", "export default function P() {}")
    payload = run_e2e_truth(tmp_path, [])
    assert payload["e2e_absent"] is True
    assert payload["journeys"] == 0
    assert payload["counts"]["match_rate"] is None
    view = scan_meta_view(payload)
    assert view["e2e_absent"] is True and view["orphan_titles"] == []


def test_full_payload_and_naming_candidates(tmp_path):
    w(tmp_path, "playwright.config.ts", "")
    w(tmp_path, "e2e/webhooks.spec.ts", PW + (
        "test.describe('Database Webhooks', () => {\n"
        "  test('can create a new webhook', async ({ page }) => {\n"
        "    await page.goto('/project/abc/integrations/webhooks/new');\n"
        "  });\n"
        "});\n"))
    w(tmp_path, "e2e/misc.spec.ts", PW + (
        "test('should work as expected', async ({ page }) => {\n"
        "  await page.goto('/project/abc/integrations/webhooks');\n"
        "});\n"))
    ufs = [uf("UF-003", "Manage webhooks",
              ["/project/[ref]/integrations/webhooks"], "webhook", pf="integrations")]
    payload = run_e2e_truth(tmp_path, ufs)
    assert payload["counts"]["matched"] == 2
    cands = payload["naming_candidates"]
    assert len(cands) == 1 and cands[0]["key"] == "UF-003"
    assert "can create a new webhook" in cands[0]["candidates"]
    # weak title journey contributed its FILE STEM, not the boilerplate
    assert "should work as expected" not in cands[0]["candidates"]
    assert "misc" in cands[0]["candidates"]


def test_determinism_two_runs_identical(tmp_path):
    w(tmp_path, "playwright.config.ts", "")
    for name in ("b", "a", "c"):
        w(tmp_path, f"e2e/{name}.spec.ts", PW + (
            f"test('{name} journey', async ({{ page }}) => {{\n"
            f"  await page.goto('/{name}-zone');\n"
            f"}});\n"))
    ufs = [uf("UF-001", "A zone", ["/a-zone"], "zone")]
    p1 = run_e2e_truth(tmp_path, ufs)
    p2 = run_e2e_truth(tmp_path, ufs)
    assert json.dumps(p1, sort_keys=True) == json.dumps(p2, sort_keys=True)
    # file order is sorted regardless of creation order
    assert [o["file"] for o in p1["orphan_journeys"]] == [
        "e2e/b.spec.ts", "e2e/c.spec.ts"]


# -- Track C: orphan-journey → UF synthesis --------------------------------

from faultline.pipeline_v2.e2e_truth import (  # noqa: E402
    E2E_ORPHAN_REASON,
    E2E_ORPHAN_UF_ENV,
    KEYLESS_JOURNEY_RECALL_ENV,
    clean_route_pattern,
    _is_negative_journey,
    _journey_label,
    keyless_journey_recall_enabled,
    matched_authored_names,
    orphan_uf_enabled,
    synthesize_orphan_journeys,
)

# Remix flat-route file that a documenso journey /t/:id/documents navigates.
_DOCS_ROUTE = "apps/remix/app/routes/_authenticated+/t.$teamUrl+/documents._index.tsx"


def _devf(name, paths, pf, role=None):
    return SimpleNamespace(name=name, paths=paths, product_feature_id=pf,
                           role=role, layer="developer", flows=[])


def _pf(name):
    return SimpleNamespace(id=name, name=name, display_name=name.title(),
                           layer="product", paths=[])


def _orphan(title, urls):
    return {"title_chain": [title], "urls_touched": urls, "steps": [],
            "file": "e2e/x.spec.ts", "runner_project": "."}


def test_orphan_uf_flag_default_on(monkeypatch):
    monkeypatch.delenv(E2E_ORPHAN_UF_ENV, raising=False)
    assert orphan_uf_enabled()
    monkeypatch.setenv(E2E_ORPHAN_UF_ENV, "0")
    assert not orphan_uf_enabled()
    monkeypatch.setenv(E2E_ORPHAN_UF_ENV, "false")
    assert not orphan_uf_enabled()


def test_clean_route_pattern_remix_and_passthrough():
    assert clean_route_pattern(
        "/_authenticated+/t.$teamUrl+/documents._index") == "/t/documents"
    assert clean_route_pattern(
        "/_recipient+/sign.$token+/complete") == "/sign/complete"
    assert clean_route_pattern("/_unauthenticated+/signup") == "/signup"
    # dynamic glyphs dropped early (downstream keychain drops them anyway)
    assert clean_route_pattern("/t/[id]/documents") == "/t/documents"
    assert clean_route_pattern("/api/v1/users") == "/api/v1/users"


def test_is_negative_journey():
    assert _is_negative_journey(("[ADMIN]: cannot promote non-existent user",))
    assert _is_negative_journey(("should not allow access when required",))
    assert _is_negative_journey(("verify role hierarchy after promotion",))
    assert not _is_negative_journey(("[BULK_ACTIONS]: select multiple documents",))
    assert not _is_negative_journey(("upload a PDF document",))


def test_journey_label():
    assert _journey_label(("[BULK_ACTIONS]: can select multiple",)) == "BULK_ACTIONS"
    assert _journey_label(("Find Documents UI - Team Context",)) == "Find Documents UI"
    assert _journey_label(("simple title",)) == "simple title"


def test_orphan_synthesis_mints_grouped_pf_bound_uf():
    dev = _devf("documents-route", [_DOCS_ROUTE], "documents")
    routes_index = [{"pattern": "/_authenticated+/t.$teamUrl+/documents._index",
                     "file": _DOCS_ROUTE}]
    payload = {
        "orphan_journeys": [
            _orphan("[BULK_ACTIONS]: can select multiple documents",
                    ["/t/:param/documents"]),
            _orphan("[BULK_ACTIONS]: header checkbox selects all",
                    ["/t/:param/documents"]),
        ],
        "uf_e2e_evidence": {}, "matched": [],
    }
    res = synthesize_orphan_journeys(payload, [_pf("documents")], [dev],
                                     routes_index, [])
    minted = res["minted"]
    assert len(minted) == 1                       # grouped by (pf, tag)
    uf, titles = minted[0]
    assert uf.product_feature_id == "documents"   # I21-safe binding
    assert uf.synthesis_reason == E2E_ORPHAN_REASON
    assert uf.synthesized is True
    assert uf.member_flow_ids == []               # recall hole: no flow
    assert uf.category == "interactive"           # playwright drives a browser
    assert uf.name_confidence == "low"
    assert uf.binding_confidence == "low"
    assert uf.resource == "document"              # noun, not the /edit action
    assert uf.routes == ["/t/:param/documents"]   # evidence
    assert len(titles) == 2                        # both bulk tests grouped
    assert res["tele"]["minted"] == 1


def _flow(name, entry, paths=None, uuid=""):
    return SimpleNamespace(name=name, uuid=uuid, entry_point_file=entry,
                           paths=paths or [entry], shared_paths=[])


def _bulk_payload():
    return {
        "orphan_journeys": [
            _orphan("[BULK_ACTIONS]: can select multiple documents",
                    ["/t/:param/documents"]),
        ],
        "uf_e2e_evidence": {}, "matched": [],
    }


def _bulk_setup():
    dev = _devf("documents-route", [_DOCS_ROUTE], "documents")
    routes_index = [{"pattern": "/_authenticated+/t.$teamUrl+/documents._index",
                     "file": _DOCS_ROUTE}]
    return dev, routes_index


# -- B47 Arm B: keyless journey recall (member-ful orphan graduation) --------


def test_keyless_journey_recall_flag_default_off(monkeypatch):
    monkeypatch.delenv(KEYLESS_JOURNEY_RECALL_ENV, raising=False)
    assert not keyless_journey_recall_enabled()
    monkeypatch.setenv(KEYLESS_JOURNEY_RECALL_ENV, "1")
    assert keyless_journey_recall_enabled()
    monkeypatch.setenv(KEYLESS_JOURNEY_RECALL_ENV, "0")
    assert not keyless_journey_recall_enabled()


def test_orphan_graduates_member_ful_when_flow_covers_route(monkeypatch):
    # A LIVE flow whose entry_point_file IS the orphan's resolved route file:
    # the orphan graduates from a member-less gap to a real member-ful journey
    # (route-grounded → confidence low→medium), keeping the authored reason.
    monkeypatch.setenv(KEYLESS_JOURNEY_RECALL_ENV, "1")
    dev, routes_index = _bulk_setup()
    covering = _flow("browse-documents-flow", _DOCS_ROUTE, uuid="u-docs")
    res = synthesize_orphan_journeys(
        _bulk_payload(), [_pf("documents")], [dev], routes_index, [],
        flows=[covering],
    )
    uf, _titles = res["minted"][0]
    assert uf.member_flow_ids == ["u-docs"]           # route-matched member
    assert uf.member_count == 1
    assert uf.name_confidence == "medium"             # route grounding lifts
    assert uf.synthesis_reason == E2E_ORPHAN_REASON   # traceability preserved
    assert uf.synthesized is True
    assert res["tele"]["member_ful"] == 1
    assert res["tele"]["members_total"] == 1


def test_orphan_stays_honest_gap_when_no_flow_covers(monkeypatch):
    # ANTI-CASE (the sacred one): a flow that does NOT touch the resolved route
    # file must NEVER be attached — inventing a member would fabricate a
    # journey with no route/entry evidence. The orphan stays a member-less gap.
    monkeypatch.setenv(KEYLESS_JOURNEY_RECALL_ENV, "1")
    dev, routes_index = _bulk_setup()
    unrelated = _flow("billing-flow", "apps/remix/app/routes/settings.billing.tsx",
                      uuid="u-bill")
    res = synthesize_orphan_journeys(
        _bulk_payload(), [_pf("documents")], [dev], routes_index, [],
        flows=[unrelated],
    )
    uf, _titles = res["minted"][0]
    assert uf.member_flow_ids == []                   # honest gap — no evidence
    assert uf.member_count == 0
    assert uf.name_confidence == "low"
    assert res["tele"]["member_ful"] == 0


def test_orphan_member_attach_off_byte_identical(monkeypatch):
    # Flag OFF (default): even a covering flow is ignored — output byte-identical
    # to the pre-B47 member-less recall seed.
    monkeypatch.delenv(KEYLESS_JOURNEY_RECALL_ENV, raising=False)
    dev, routes_index = _bulk_setup()
    covering = _flow("browse-documents-flow", _DOCS_ROUTE, uuid="u-docs")
    res = synthesize_orphan_journeys(
        _bulk_payload(), [_pf("documents")], [dev], routes_index, [],
        flows=[covering],
    )
    uf, _titles = res["minted"][0]
    assert uf.member_flow_ids == []
    assert uf.member_count == 0
    assert uf.name_confidence == "low"


def test_orphan_negative_paths_filtered_not_minted():
    dev = _devf("documents-route", [_DOCS_ROUTE], "documents")
    routes_index = [{"pattern": "/_authenticated+/t.$teamUrl+/documents._index",
                     "file": _DOCS_ROUTE}]
    payload = {
        "orphan_journeys": [
            _orphan("[ADMIN]: cannot promote non-existent user",
                    ["/t/:param/documents"]),
        ],
        "uf_e2e_evidence": {}, "matched": [],
    }
    res = synthesize_orphan_journeys(payload, [_pf("documents")], [dev],
                                     routes_index, [])
    assert res["tele"]["filtered_negative"] == 1
    assert res["minted"] == []


def test_orphan_unbound_pf_dropped_i21_safe():
    # Route file is owned by NO product feature → journey must be dropped,
    # never emitted with a null product_feature_id (I21).
    routes_index = [{"pattern": "/_authenticated+/t.$teamUrl+/documents._index",
                     "file": _DOCS_ROUTE}]
    payload = {
        "orphan_journeys": [
            _orphan("[BULK_ACTIONS]: select documents", ["/t/:param/documents"]),
        ],
        "uf_e2e_evidence": {}, "matched": [],
    }
    res = synthesize_orphan_journeys(payload, [], [], routes_index, [])
    assert res["minted"] == []
    assert res["tele"]["dropped_unbound_pf"] == 1
    assert all(uf.product_feature_id for uf, _ in res["minted"])  # trivially true


def test_orphan_no_route_evidence_dropped():
    payload = {
        "orphan_journeys": [_orphan("[X]: does a thing", [])],  # no urls
        "uf_e2e_evidence": {}, "matched": [],
    }
    res = synthesize_orphan_journeys(payload, [_pf("documents")], [], [], [])
    assert res["minted"] == []
    assert res["tele"]["dropped_no_route_ev"] == 1


def test_orphan_synthesis_deterministic():
    dev = _devf("documents-route", [_DOCS_ROUTE], "documents")
    routes_index = [{"pattern": "/_authenticated+/t.$teamUrl+/documents._index",
                     "file": _DOCS_ROUTE}]
    payload = {
        "orphan_journeys": [
            _orphan("[BULK_ACTIONS]: a", ["/t/:param/documents"]),
            _orphan("[DOCUMENTS]: b", ["/t/:param/documents"]),
        ],
        "uf_e2e_evidence": {}, "matched": [],
    }
    a = synthesize_orphan_journeys(payload, [_pf("documents")], [dev],
                                   routes_index, [])
    b = synthesize_orphan_journeys(payload, [_pf("documents")], [dev],
                                   routes_index, [])
    assert [(u.name, u.product_feature_id, tuple(u.routes)) for u, _ in a["minted"]] == \
           [(u.name, u.product_feature_id, tuple(u.routes)) for u, _ in b["minted"]]


def test_matched_authored_names_route_only():
    # via="route" contributes; via="name" (weak fallback) is excluded.
    payload = {"matched": [
        {"via": "route", "uf_id": "UF-001",
         "title_chain": ["[TEAMS]: create folder"]},
        {"via": "name", "uf_id": "UF-002",
         "title_chain": ["Find Documents API"]},
        {"via": "route", "uf_id": "UF-003",
         "title_chain": ["cannot delete locked doc"]},  # negative → skip
    ], "uf_e2e_evidence": {}}
    out = matched_authored_names(payload)
    assert "UF-001" in out and out["UF-001"] == ["Teams"]
    assert "UF-002" not in out          # weak via=name excluded
    assert "UF-003" not in out          # negative excluded


# -- Track C: cross-process (PYTHONHASHSEED) determinism -------------------

import os as _os          # noqa: E402
import subprocess as _sp  # noqa: E402
import sys as _sys        # noqa: E402

# A fixture that STRESSES both set-based tie-breaks: one journey resolves to
# two EQUAL-LENGTH route families owned by two different PFs (dominant-fam tie
# AND owner-majority tie). Before the total-order fix the minted resource/PF
# drifted with PYTHONHASHSEED (4th non-det class). The driver prints a
# canonical signature of every minted UF.
_DET_DRIVER = r'''
from faultline.pipeline_v2.e2e_truth import synthesize_orphan_journeys
from types import SimpleNamespace

def devf(name, paths, pf):
    return SimpleNamespace(name=name, paths=paths, product_feature_id=pf,
                           role=None, layer="developer", flows=[])
def pf(name):
    return SimpleNamespace(id=name, name=name, display_name=name.title(),
                           layer="product", paths=[])

routes = [
    {"pattern": "/team/[id]/reports", "file": "app/team/reports.tsx"},
    {"pattern": "/team/[id]/exports", "file": "app/team/exports.tsx"},
    {"pattern": "/team/[id]/billing", "file": "app/team/billing.tsx"},
]
devs = [devf("reports", ["app/team/reports.tsx"], "reports"),
        devf("exports", ["app/team/exports.tsx"], "exports"),
        devf("billing", ["app/team/billing.tsx"], "billing")]
pfs = [pf("reports"), pf("exports"), pf("billing")]
payload = {"orphan_journeys": [
    {"title_chain": ["[DASH]: view team dashboards"],
     "urls_touched": ["/team/:id/reports", "/team/:id/exports"], "steps": []},
    {"title_chain": ["[DASH]: filter team dashboards"],
     "urls_touched": ["/team/:id/exports", "/team/:id/reports"], "steps": []},
    {"title_chain": ["[BILL]: open billing"],
     "urls_touched": ["/team/:id/billing"], "steps": []},
], "uf_e2e_evidence": {}, "matched": []}
res = synthesize_orphan_journeys(payload, pfs, devs, routes, [])
sig = "|".join(f"{u.name}:{u.product_feature_id}:{u.resource}:{u.id}:"
               f"{','.join(u.routes)}" for u, _ in res["minted"])
print(f"{res['tele']['minted']}||{sig}")
'''


def _run_seed(seed: str) -> str:
    env = dict(_os.environ)
    env["PYTHONHASHSEED"] = seed
    # ensure the worktree package is importable in the child
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    out = _sp.run([_sys.executable, "-c", _DET_DRIVER], env=env,
                  capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_orphan_synthesis_cross_process_determinism():
    """Minted set is IDENTICAL across PYTHONHASHSEED values (separate
    processes) — proves the resource/dominant/owner picks are total-ordered,
    not set-iteration-order dependent."""
    sigs = {_run_seed(s) for s in ("0", "1", "424242", "7")}
    assert len(sigs) == 1, f"non-deterministic across PYTHONHASHSEED: {sigs}"
    only = next(iter(sigs))
    assert only.startswith("2||"), only  # 2 groups (DASH, BILL) minted
