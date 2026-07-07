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
