"""Stage 0.8 — product thesis: derivation, lexicon, gate, hard-law guards.

Covers (task gates):
  * vertical happy paths x4 on SYNTHETIC anchor fixtures — industry
    vocabulary only, no repo-specific names;
  * exact-tie and no-evidence fallbacks to ``generic-saas``;
  * dep evidence can corroborate but never establish a vertical
    (IS-vs-USES guard);
  * ``ThesisSignals.collect`` consumes Stage-1 anchors (schema nouns,
    package dep categories, explicit route tuples, filesystem routes,
    nav labels) without re-parsing schemas;
  * the runner gate (product-app only, kill-switch, legacy ``None``);
  * determinism (input order irrelevant, repeated runs byte-equal);
  * YAML drift guard (packaged copy == eval/ authoring copy);
  * HARD LAW — the thesis is write-only: grep guard (no pipeline module
    reads ``product_thesis``) + import allowlist (the stage cannot even
    import membership/attribution machinery).
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.pipeline_v2.data import load_data_text
from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.stage_0_7_repo_class import (
    CONF_RESIDUAL,
    CONF_STRONG,
    REPO_CLASS_LIBRARY,
    REPO_CLASS_PRODUCT_APP,
    RepoClassVerdict,
)
from faultline.pipeline_v2.stage_0_8_product_thesis import (
    GENERIC_VERTICAL,
    MIN_NOUN_FAMILIES,
    THESIS_ENV,
    ProductThesis,
    ThesisSignals,
    _collect_vendor_hits,
    _dep_category_slugs,
    derive_product_thesis,
    load_thesis_lexicon,
    run_stage_0_8,
    scan_meta_block,
    should_derive_thesis,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _verdict(repo_class: str, confidence: float = CONF_STRONG) -> RepoClassVerdict:
    return RepoClassVerdict(
        repo_class=repo_class,
        confidence=confidence,
        rationale="synthetic",
        matched_signals=("synthetic",),
    )


# ── Vertical happy paths (synthetic signals, industry vocabulary) ───────


def test_security_operations_vertical() -> None:
    signals = ThesisSignals(
        schema_nouns=("alert", "detection", "case", "playbook"),
        route_segments=("alerts", "alerts", "cases", "detections", "api"),
        nav_labels=("Alerts", "Cases"),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "security-operations"
    assert thesis.audience == "security & SOC teams"
    # Multi-channel nouns outrank single-channel ones.
    assert thesis.core_objects[0] in {"alert", "case"}
    assert "playbook" in thesis.core_objects
    assert thesis.sentence.startswith("Security operations platform around ")
    assert thesis.sentence.endswith("for security & SOC teams.")
    assert thesis.evidence["tie"] is False
    assert thesis.evidence["ranked"][0]["vertical"] == "security-operations"


def test_finance_invoicing_vertical_with_dep_corroboration() -> None:
    signals = ThesisSignals(
        schema_nouns=("invoice", "transaction", "bank-account"),
        route_segments=("invoices", "transactions", "settings"),
        dep_categories=("billing", "email"),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "finance-invoicing"
    top = thesis.evidence["ranked"][0]
    # ``billing`` corroborates (vertical lists it); ``email`` does not.
    assert top["dep_categories"] == ["billing"]
    assert top["score"] == len(top["noun_families"]) + 1


def test_scheduling_vertical_multipart_family() -> None:
    signals = ThesisSignals(
        # ``event-type`` fires only as a CONTIGUOUS compound.
        schema_nouns=("event-type", "booking", "availability"),
        nav_labels=("Bookings", "Availability"),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "scheduling"
    assert thesis.audience == "hosts & invitees"
    assert "event-type" in thesis.evidence["ranked"][0]["noun_families"]


def test_forms_surveys_vertical_route_only_signals() -> None:
    signals = ThesisSignals(
        route_segments=(
            "surveys", "surveys", "responses", "questions", "api",
        ),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "forms-surveys"
    # Every family here matched via a single channel.
    channels = thesis.evidence["ranked"][0]["channels"]
    assert all(v == ["route"] for v in channels.values())


def test_multipart_family_requires_contiguity() -> None:
    """Separate ``event`` and ``type`` tokens never fire ``event-type``."""
    signals = ThesisSignals(route_segments=("event", "type", "bookings"))
    thesis = derive_product_thesis(signals)
    ranked = {e["vertical"]: e for e in thesis.evidence["ranked"]}
    sched = ranked.get("scheduling")
    assert sched is not None and "event-type" not in sched["noun_families"]


# ── Fallbacks: tie, thin evidence, dep-only ─────────────────────────────


def test_exact_tie_falls_back_to_generic() -> None:
    # Two verticals, two schema families each — identical rank keys.
    signals = ThesisSignals(
        schema_nouns=("invoice", "expense", "survey", "question"),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == GENERIC_VERTICAL
    assert thesis.evidence["tie"] is True
    ranked_ids = {e["vertical"] for e in thesis.evidence["ranked"]}
    assert {"finance-invoicing", "forms-surveys"} <= ranked_ids
    # Core objects are still listed for the fallback.
    assert set(thesis.core_objects) == {"invoice", "expense", "survey", "question"}


def test_no_evidence_falls_back_to_generic() -> None:
    thesis = derive_product_thesis(ThesisSignals())
    assert thesis.vertical == GENERIC_VERTICAL
    assert thesis.core_objects == ()
    assert thesis.evidence["ranked"] == []
    assert thesis.sentence == "General SaaS platform for end users & teams."


def test_single_noun_family_is_not_a_thesis() -> None:
    """One domain noun (< MIN_NOUN_FAMILIES) never decides a vertical."""
    assert MIN_NOUN_FAMILIES >= 2
    signals = ThesisSignals(
        schema_nouns=("invoice",),
        route_segments=("invoices",),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == GENERIC_VERTICAL
    assert "invoice" in thesis.core_objects


def test_dep_categories_alone_never_establish_a_vertical() -> None:
    """IS-vs-USES: a stripe + resend import is not a finance product."""
    signals = ThesisSignals(dep_categories=("billing", "email", "realtime"))
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == GENERIC_VERTICAL
    assert thesis.evidence["ranked"] == []


def test_generic_chrome_never_ranks_as_core_object() -> None:
    signals = ThesisSignals(
        schema_nouns=("user", "organization", "session", "invoice"),
        route_segments=("api", "settings", "login", "invoices"),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.core_objects == ("invoice",)


# ── Determinism ─────────────────────────────────────────────────────────


def test_derivation_is_deterministic_and_order_invariant() -> None:
    a = ThesisSignals(
        schema_nouns=("alert", "detection", "case"),
        route_segments=("alerts", "cases", "detections"),
        nav_labels=("Alerts",),
        dep_categories=("auth",),
    )
    b = ThesisSignals(
        schema_nouns=("case", "alert", "detection"),
        route_segments=("detections", "alerts", "cases"),
        nav_labels=("Alerts",),
        dep_categories=("auth",),
    )
    block_a1 = scan_meta_block(derive_product_thesis(a))
    block_a2 = scan_meta_block(derive_product_thesis(a))
    block_b = scan_meta_block(derive_product_thesis(b))
    assert block_a1 == block_a2 == block_b


def test_scan_meta_block_shape_and_key_order() -> None:
    thesis = derive_product_thesis(
        ThesisSignals(schema_nouns=("alert", "detection")),
    )
    block = scan_meta_block(thesis)
    assert list(block) == [
        "vertical", "core_objects", "audience", "sentence", "evidence",
    ]
    assert isinstance(block["core_objects"], list)
    assert set(block["evidence"]) == {"signals", "ranked", "tie"}


# ── ThesisSignals.collect — consumes Stage-1 anchors ────────────────────


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(repo_path=tmp_path, run_id=None, run_dir=None)


def _synthetic_stage1_out() -> dict[str, list[AnchorCandidate]]:
    return {
        "schema": [
            AnchorCandidate(
                name="invoice", paths=("db/schema.prisma",),
                source="schema", confidence_self=0.6,
            ),
            AnchorCandidate(
                name="transaction", paths=("db/schema.prisma",),
                source="schema", confidence_self=0.6,
            ),
        ],
        "route": [
            AnchorCandidate(
                name="invoices", paths=("app/invoices/page.tsx",),
                source="route", confidence_self=0.9,
            ),
        ],
        "fastapi-route": [
            AnchorCandidate(
                name="transactions", paths=("backend/api/transactions.py",),
                source="fastapi-route", confidence_self=0.9,
                routes=(
                    ("/api/v1/transactions/{txn_id}", "GET",
                     "backend/api/transactions.py"),
                ),
            ),
        ],
        "package": [
            AnchorCandidate(
                name="billing", paths=("package.json",),
                source="package", confidence_self=0.7,
            ),
            # Workspace-name package anchor — NOT a dep category.
            AnchorCandidate(
                name="web-storefront", paths=("apps/web/package.json",),
                source="package", confidence_self=0.95,
            ),
        ],
        "_errors": {"mvc": "boom"},  # sentinel key must be ignored
    }


def test_collect_consumes_stage1_anchors(tmp_path: Path) -> None:
    signals = ThesisSignals.collect(_ctx(tmp_path), _synthetic_stage1_out())
    assert signals.schema_nouns == ("invoice", "transaction")
    # Explicit route tuple: /api/v1/transactions/{txn_id} — the version
    # prefix and the dynamic segment are dropped, concrete ones kept.
    assert "transactions" in signals.route_segments
    assert "v1" not in signals.route_segments
    assert not any("{" in s for s in signals.route_segments)
    # Filesystem route: app/invoices/page.tsx → /invoices.
    assert "invoices" in signals.route_segments
    # Dep categories: intersection with the stage1_anchors vocabulary.
    assert signals.dep_categories == ("billing",)


def test_collect_reads_nav_labels_from_nav_files(tmp_path: Path) -> None:
    nav = tmp_path / "src" / "components" / "sidebar.tsx"
    nav.parent.mkdir(parents=True)
    nav.write_text(
        'export const items = ['
        '{label: "Invoices", href: "/invoices"},'
        '{label: "Transactions", href: "/transactions"},'
        '];\n',
        encoding="utf-8",
    )
    stage1_out = {
        "route": [
            AnchorCandidate(
                name="invoices",
                paths=("src/components/sidebar.tsx",),
                source="route", confidence_self=0.9,
            ),
        ],
    }
    signals = ThesisSignals.collect(_ctx(tmp_path), stage1_out)
    assert "Invoices" in signals.nav_labels
    assert "Transactions" in signals.nav_labels


def test_collect_end_to_end_derives_vertical(tmp_path: Path) -> None:
    signals = ThesisSignals.collect(_ctx(tmp_path), _synthetic_stage1_out())
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "finance-invoicing"
    assert thesis.evidence["ranked"][0]["dep_categories"] == ["billing"]


# ── Runner gate (mirrors Stage 0.7 semantics) ───────────────────────────


def test_gate_product_app_only() -> None:
    assert should_derive_thesis(_verdict(REPO_CLASS_PRODUCT_APP)) is True
    # The fail-open residual (low confidence) is a product app too.
    assert should_derive_thesis(
        _verdict(REPO_CLASS_PRODUCT_APP, CONF_RESIDUAL),
    ) is True
    assert should_derive_thesis(_verdict(REPO_CLASS_LIBRARY)) is False
    assert should_derive_thesis(None) is False


def test_gate_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(THESIS_ENV, "0")
    assert should_derive_thesis(_verdict(REPO_CLASS_PRODUCT_APP)) is False
    monkeypatch.setenv(THESIS_ENV, "1")
    assert should_derive_thesis(_verdict(REPO_CLASS_PRODUCT_APP)) is True


def test_run_stage_0_8_skips_non_product(tmp_path: Path) -> None:
    result = run_stage_0_8(
        _ctx(tmp_path), _synthetic_stage1_out(), _verdict(REPO_CLASS_LIBRARY),
    )
    assert result is None


def test_run_stage_0_8_derives_for_product(tmp_path: Path) -> None:
    result = run_stage_0_8(
        _ctx(tmp_path),
        _synthetic_stage1_out(),
        _verdict(REPO_CLASS_PRODUCT_APP),
    )
    assert isinstance(result, ProductThesis)
    assert result.vertical == "finance-invoicing"


def test_run_stage_0_8_never_raises_on_broken_input(tmp_path: Path) -> None:
    result = run_stage_0_8(
        _ctx(tmp_path),
        {"schema": object()},  # type: ignore[dict-item] — hostile shape
        _verdict(REPO_CLASS_PRODUCT_APP),
    )
    # Hostile stage1_out values are ignored by collect (non-list) —
    # derivation still returns a (generic) thesis rather than raising.
    assert result is None or isinstance(result, ProductThesis)


# ── Lexicon sanity + drift guard (house pattern) ────────────────────────


def test_lexicon_shape_and_universality_conventions() -> None:
    lex = load_thesis_lexicon()
    assert len(lex.rules) == 11  # W3.1 D3: + compliance-grc
    assert lex.fallback_id == GENERIC_VERTICAL
    assert lex.core_object_stopwords
    seen: set[str] = set()
    for rule in lex.rules:
        assert rule.vertical_id not in seen
        seen.add(rule.vertical_id)
        assert rule.display and rule.audience
        # Enough families that MIN_NOUN_FAMILIES is reachable + slack.
        assert len(rule.noun_families) >= 3
        for family in rule.noun_families:
            # Families are stored normalized (lowercase singular parts).
            assert family
            assert all(p == p.lower() for p in family)
    # Dep references must exist in the dep-anchor vocabulary (typo guard).
    known = _dep_category_slugs()
    for rule in lex.rules:
        assert set(rule.dep_categories) <= known, rule.vertical_id


def test_expected_verticals_present() -> None:
    lex = load_thesis_lexicon()
    assert {r.vertical_id for r in lex.rules} == {
        "security-operations", "finance-invoicing", "document-workflow",
        "scheduling", "forms-surveys", "e-commerce", "analytics",
        "dev-tools", "communication",
        "enterprise-search",
        "compliance-grc",  # W3.1 D3 (fb3 comp — Vanta/Drata class)
    }


@pytest.mark.skipif(
    not (_REPO_ROOT / "eval").exists(),
    reason="eval/ is local/private-only (scrubbed 2026-07-11)",
)
def test_product_verticals_yaml_matches_eval_authoring_copy() -> None:
    """House drift guard: packaged copy == eval/ authoring copy."""
    authoring = (_REPO_ROOT / "eval" / "product-verticals.yaml").read_text(
        encoding="utf-8",
    )
    load_data_text.cache_clear()
    packaged = load_data_text("product-verticals.yaml")
    assert packaged == authoring, (
        "DRIFT: faultline/pipeline_v2/data/product-verticals.yaml differs "
        "from eval/product-verticals.yaml. Re-sync the in-package copy."
    )


# ── HARD LAW: the thesis is write-only (no membership influence) ────────

_PIPELINE_DIR = _REPO_ROOT / "faultline"

#: Files allowed to mention ``product_thesis`` inside ``faultline/``:
#: the stage itself, the runner that WRITES the scan_meta key, and the
#: W3 persona seam (the explicit, reviewed consumer): phase_finalize
#: THREADS ``scan_meta["product_thesis"]`` into the personas, which use
#: it as NAMING/adjudication context only — the iron guard (thesis
#: never influences membership) is preserved because the naming
#: contract writes ONLY the display channel (see
#: test_naming_contract grep-guard + identity-untouched fixtures). Any
#: OTHER module reading the thesis remains forbidden.
_ALLOWED_MENTIONS = {
    Path("pipeline_v2/stage_0_8_product_thesis.py"),
    Path("pipeline_v2/run.py"),
    Path("pipeline_v2/phase_finalize.py"),
    Path("pipeline_v2/personas.py"),
    # G5 replay harness: the stage registry REPLICATES the two seams
    # already allowlisted above, verbatim — the run.py Stage-0.8
    # invocation (its replay row calls run_stage_0_8, which writes the
    # artifact) and phase_finalize's persona threading
    # (``thesis=scan_meta.get("product_thesis")`` into the PM-labeler /
    # surface-adjudicator builders — naming/adjudication context only).
    # The registry introduces NO new consumption seam: it never reads
    # thesis fields for membership or any pipeline decision, and the
    # replay identity gate pins its copies to the orchestrator sites.
    Path("replay/registry.py"),
}


def test_grep_guard_no_pipeline_module_reads_product_thesis() -> None:
    offenders: list[str] = []
    for py in sorted(_PIPELINE_DIR.rglob("*.py")):
        rel = py.relative_to(_PIPELINE_DIR)
        if rel in _ALLOWED_MENTIONS:
            continue
        if "product_thesis" in py.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(rel))
    assert not offenders, (
        "HARD LAW violated: product_thesis is write-only scan_meta "
        f"telemetry, but these modules mention it: {offenders}. "
        "Consuming the thesis requires an explicit reviewed seam."
    )


# ── W2b.1 (e) — lexicon polish ──────────────────────────────────────────


def test_vendor_tokens_never_core_objects() -> None:
    """rallly 'stripes': Stripe webhook routes leaked the vendor token
    into the core objects — a vendor/brand token names an integration
    (the dep-anchor FAMILY carries it), never the product's own object.
    Chrome additions (control-panel/handler/manage/domain) same class."""
    signals = ThesisSignals(
        route_segments=(
            "polls", "polls", "poll", "calendar", "availability",
            "stripe", "stripe", "stripe", "slack-app",
            "control-panel", "control-panel", "handlers", "manage",
            "domains",
        ),
        dep_categories=("billing",),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "scheduling"
    assert "stripe" not in thesis.core_objects
    assert "slack-app" not in thesis.core_objects
    assert "control-panel" not in thesis.core_objects
    assert "handler" not in thesis.core_objects
    assert "manage" not in thesis.core_objects
    assert "domain" not in thesis.core_objects
    assert "poll" in thesis.core_objects


def test_enterprise_search_vertical_onyx_shape() -> None:
    """onyx: connector/embedding/indexing/chunk retrieval vocabulary +
    the ai dep beats communication's chat/channel/message (the wrong
    'Communication…senders & recipients' verdict on an enterprise-search
    product)."""
    signals = ThesisSignals(
        route_segments=(
            "chat", "chat", "channels", "message",
            "connector", "connector-docs", "cancel-new-embedding",
            "indexing", "chunk-info", "search",
        ),
        dep_categories=("ai", "email"),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "enterprise-search", thesis.evidence["ranked"]
    assert thesis.audience == "knowledge workers & IT teams"


def test_document_workflow_wins_papermark_shape() -> None:
    """papermark: dataroom/document/agreement/viewer/visitor/link +
    file-uploads outweigh the real-but-secondary chat/conversation/
    message surface (dataroom Q&A)."""
    signals = ThesisSignals(
        schema_nouns=(
            "Agreement", "Dataroom", "Document", "Link", "Viewer",
            "VisitorGroup", "Chat", "Conversation", "Message",
        ),
        route_segments=("documents", "datarooms", "links", "viewer",
                        "chat", "conversations", "emails"),
        nav_labels=("Documents", "Datarooms"),
        dep_categories=("file-uploads", "email", "billing"),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "document-workflow", thesis.evidence["ranked"]
    assert "agreement" in thesis.evidence["ranked"][0]["noun_families"]


def test_dev_tools_unaffected_openstatus_shape() -> None:
    """Regression guard: the new vertical/families must not flip a
    dev-tools repo (openstatus: api-key/cli/registry vs chat/email)."""
    signals = ThesisSignals(
        schema_nouns=("ApiKey", "Incident", "Monitor"),
        route_segments=("api-keys", "cli", "registry", "chat", "emails",
                        "monitors", "monitors"),
        nav_labels=("Monitors", "API Keys", "CLI"),
        dep_categories=("email", "ai"),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "dev-tools", thesis.evidence["ranked"]


#: The stage may import ONLY these faultline modules — signal loading,
#: string collectors, the 0.7 verdict vocabulary, and the artifact /
#: logging / replay plumbing. Everything that decides membership,
#: attribution, flows, or Layer 2 is structurally out of reach.
_IMPORT_ALLOWLIST = {
    "faultline.pipeline_v2.data",
    "faultline.pipeline_v2.product_strings",
    "faultline.pipeline_v2.stage_0_7_repo_class",
    "faultline.pipeline_v2.stage_0_intake",       # TYPE_CHECKING only
    "faultline.pipeline_v2.extractors.base",      # TYPE_CHECKING only
    "faultline.pipeline_v2.stage_7_output",       # artifact writer
    "faultline.pipeline_v2.run_logger",           # StageLogger
    "faultline.pipeline_v2.naming_validator",     # VENDOR_TOKENS vocabulary
    "faultline.replay.capture",                   # replay input capture
}

_FORBIDDEN_IMPORT_TOKENS = (
    "conservation", "membership", "reconcile", "dual_evidence",
    "stage_2", "stage_3", "stage_4", "stage_5", "stage_6", "stage_8",
)


def test_import_guard_stage_cannot_touch_membership_machinery() -> None:
    source = (
        _PIPELINE_DIR / "pipeline_v2" / "stage_0_8_product_thesis.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    faultline_imports = {m for m in imported if m.startswith("faultline")}
    assert faultline_imports <= _IMPORT_ALLOWLIST, (
        f"unexpected imports: {sorted(faultline_imports - _IMPORT_ALLOWLIST)}"
    )
    for module in faultline_imports:
        # stage_0_7 (the gate verdict) is the one sanctioned stage import.
        if module == "faultline.pipeline_v2.stage_0_7_repo_class":
            continue
        assert not any(tok in module for tok in _FORBIDDEN_IMPORT_TOKENS), module


# ── W3.1 D3 — fb3 thesis fix-family (plumbing / GRC / vendor cluster) ────


def test_plumbing_families_never_establish_without_schema() -> None:
    """fb3 tracecat: dev-tools scored 7 via api-key/secret ROUTE hits —
    plumbing chrome every SaaS has. Without schema confirmation they
    neither establish nor score."""
    signals = ThesisSignals(
        route_segments=("api-keys", "api-key", "secrets", "tokens"),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "generic-saas", thesis.evidence["ranked"]
    ranked = thesis.evidence["ranked"]
    if ranked:  # dev-tools may appear, but with zero counted families
        assert ranked[0]["noun_families"] == []
        assert "api-key" in ranked[0].get("plumbing_suppressed", [])


def test_plumbing_families_count_when_schema_declared() -> None:
    """The infisical/openstatus class keeps its signal: a product whose
    OWN schema declares ApiKey/Secret entities is about them."""
    signals = ThesisSignals(
        schema_nouns=("ApiKey", "Secret"),
        route_segments=("api-keys", "secrets", "cli", "sdk"),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "dev-tools", thesis.evidence["ranked"]


def test_compliance_grc_wins_comp_shape() -> None:
    """fb3 comp: a GRC platform must classify compliance-grc, not
    enterprise-search (its AI-copilot nouns are a supporting feature)."""
    signals = ThesisSignals(
        route_segments=(
            "frameworks", "controls", "policies", "policy", "evidence-forms",
            "risks", "risk", "vendors", "auditor", "questionnaire",
            "new_questionnaire", "isms", "soa", "statement-of-applicability",
            "penetration-tests", "tasks", "people", "knowledge-base", "chat",
        ),
        nav_labels=("Frameworks", "Controls", "Policies", "Risks", "Vendors"),
        dep_categories=("ai", "billing", "email"),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "compliance-grc", thesis.evidence["ranked"]
    assert "Compliance & GRC" in thesis.sentence


def test_security_vendor_cluster_lifts_soar_shape() -> None:
    """fb3 tracecat: a SOAR's security identity lives in its integration
    CATALOG (crowdstrike/splunk/panther/wazuh/virustotal/... under
    templates/tools + integrations/). Every 3 distinct security vendors
    = one family-equivalent; with the `case` noun the vertical clears
    dev-tools' honest residue."""
    vendor_paths = tuple(
        f"registry/integrations/{v}.py" for v in (
            "crowdstrike_falconpy", "splunk", "panther", "wazuh",
            "virustotal", "urlscan", "sentinel_one", "okta_sdk",
            "google_secops_soar",
        )
    ) + tuple(
        f"registry/templates/tools/{v}/action.yml" for v in (
            "abuseipdb", "hibp", "elastic_security", "tenable_sc",
            "threatstream", "gophish", "microsoft_sentinel", "misp",
            "opencti", "shodan", "snyk", "rapid7", "qualys", "greynoise",
            "hybrid_analysis", "leakcheck",
        )
    )
    signals = ThesisSignals(
        route_segments=("cases", "case-fields", "case-tags", "workflows",
                        "executions", "registry", "repositories", "commits",
                        "branches", "functions", "chat", "inbox", "messages",
                        "threads", "channels", "emails"),
        vendor_hits=_collect_vendor_hits(list(vendor_paths)),
        dep_categories=("ai",),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical == "security-operations", thesis.evidence["ranked"]
    top = thesis.evidence["ranked"][0]
    assert top["vendor_cluster"]["distinct_vendors"] >= 9
    assert top["vendor_cluster"]["category"] == "security"


def test_vendor_cluster_never_establishes_alone() -> None:
    """The n8n-class guard: an everything-catalog automation hub with
    ZERO security nouns is not a security product by catalog contents."""
    vendor_paths = [
        f"packages/integrations/{v}/index.ts" for v in (
            "crowdstrike", "splunk", "panther", "wazuh", "virustotal",
            "okta", "tenable",
        )
    ]
    signals = ThesisSignals(
        route_segments=("workflows", "executions", "credentials"),
        vendor_hits=_collect_vendor_hits(vendor_paths),
    )
    thesis = derive_product_thesis(signals)
    assert thesis.vertical != "security-operations", thesis.evidence["ranked"]


def test_vendor_hits_require_integration_context() -> None:
    """Brand tokens OUTSIDE integration-context dirs never count (a
    docs/comparisons/splunk.md page is not an integration)."""
    hits = _collect_vendor_hits([
        "docs/comparisons/splunk.md",
        "src/lib/crowdstrike-blog-post.ts",
    ])
    assert hits == ()


def test_auth_plumbing_never_core_objects() -> None:
    """fb3 pretalx: core_objects[0] was 'reset' (password-reset routes) —
    the board's first sentence led with auth plumbing."""
    signals = ThesisSignals(
        route_segments=("reset", "reset", "reset", "talks", "talk",
                        "events", "speakers", "cfp"),
    )
    thesis = derive_product_thesis(signals)
    assert "reset" not in thesis.core_objects, thesis.core_objects
    assert thesis.core_objects[0] in ("talk", "event", "speaker", "cfp")
