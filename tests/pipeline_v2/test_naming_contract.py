"""Stage 6.87 naming contract (Product-Spine §4.8, Wave 3) — laws,
candidates, keeper pin channel, identity-untouched hard law.

Fixtures mirror the wave2b1-out exhibits verbatim: the midday hub-vendor
titleize class (``Gocardless`` / ``Chatgpt Mcp``), the Soc0 ``Edr Core``
acronym class, the openstatus qualified-slug verbose class
(``Discord (Route Apps Web Src App Landing Redirect Discord)``), and the
H3 PF==UF twin class (backstop-synthesized journeys named exactly like
their capability — midday had 30).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2.naming_contract import (
    build_pf_candidates,
    build_uf_candidates,
    display_law_violations,
    hub_composition_display,
    humanize_anchor_display,
    load_naming_vocab,
    naming_contract_enabled,
    polish_display_casing,
    run_naming_contract,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _pf(slug: str, display: str, anchor_id: str | None = None,
        paths: list[str] | None = None) -> Feature:
    f = Feature(
        name=slug, display_name=display, layer="product",
        paths=paths or [], authors=["a"], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_NOW, health_score=100.0,
    )
    if anchor_id:
        f.anchor_id = anchor_id
    return f


def _uf(uid: str, name: str, pfid: str, *, synthesized: bool = False,
        members: list[str] | None = None, identity: dict | None = None) -> UserFlow:
    uf = UserFlow(
        id=uid, name=name, resource=pfid, domain=None,
        product_feature_id=pfid, intent="manage",
        member_flow_ids=members or [], member_count=len(members or []),
        synthesized=synthesized,
    )
    if identity:
        uf.identity = identity
    return uf


# ── Vocabulary hygiene (house patterns) ─────────────────────────────────


def test_vocab_drift_guard_packaged_equals_eval_copy() -> None:
    packaged = (
        _REPO_ROOT / "faultline" / "pipeline_v2" / "data"
        / "naming-contract-vocab.yaml"
    ).read_bytes()
    authoring = (_REPO_ROOT / "eval" / "naming-contract-vocab.yaml").read_bytes()
    assert packaged == authoring, (
        "naming-contract vocab drift: faultline/pipeline_v2/data/ and "
        "eval/ copies must stay byte-identical"
    )


def test_grep_guard_naming_module_never_writes_identity() -> None:
    """HARD LAW (§4.8): identity ≠ display. The naming module may write
    ``display_name`` (PF) and ``.name`` on USER FLOWS only — it must
    never assign canonical identity fields."""
    src = (
        _REPO_ROOT / "faultline" / "pipeline_v2" / "naming_contract.py"
    ).read_text(encoding="utf-8")
    forbidden = re.findall(
        r"\.(product_feature_id|anchor_id|uuid|resource|intent|id|"
        r"member_flow_ids|paths)\s*=[^=]",
        src,
    )
    assert not forbidden, (
        f"naming_contract.py assigns identity fields: {forbidden}"
    )
    # ``<feature>.name = …`` would rewrite a canonical PF slug — the only
    # legal ``.name`` assignment targets are UserFlow locals (named
    # ``uf`` / ``uf_obj`` by convention, enforced here).
    name_writes = [
        m.group(0) for m in re.finditer(r"(\w+)\.name\s*=[^=]", src)
    ]
    assert all(m.startswith(("uf.", "uf_obj.")) for m in name_writes), (
        f"non-UF .name assignment in naming_contract.py: {name_writes}"
    )


# ── Casing polish (law: acronym_case) ───────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "polished"),
    [
        ("Edr Core", "EDR Core"),
        ("Cli", "CLI"),
        ("Mcp Apps", "MCP Apps"),
        ("Gocardless", "GoCardless"),
        ("Chatgpt Mcp", "ChatGPT MCP"),
        ("Claroty (Iot Ot)", "Claroty (IoT OT)"),
        ("Trpc", "tRPC"),
        ("Browse and install CLI", "Browse and install CLI"),  # idempotent
        ("Manage settings", "Manage settings"),                # untouched
    ],
)
def test_polish_display_casing(raw: str, polished: str) -> None:
    v = load_naming_vocab()
    assert polish_display_casing(raw, v) == polished
    # Idempotence — polishing a polished display is a no-op.
    assert polish_display_casing(polished, v) == polished


def test_english_words_never_acronym_cased() -> None:
    """Dictionary guard: short English words ('it', 'apps', 'id') are
    not in the acronym list — the polish must not shout them."""
    v = load_naming_vocab()
    assert polish_display_casing("It Apps Id", v) == "It Apps Id"


# ── Display laws ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "law"),
    [
        ("P", "single_letter"),
        ("I", "single_letter"),
        ("$slug", "param"),
        ("[teamId]", "param"),
        ("{orgId}", "param"),
        ("schema.json", "file_stem"),
        ("Config settings.yml", "file_stem"),
        ("robots.txt", "file_stem"),
    ],
)
def test_display_laws_fire(text: str, law: str) -> None:
    assert law in display_law_violations(text, load_naming_vocab())


@pytest.mark.parametrize(
    "text",
    ["Settings", "EDR — Claroty", "Manage status reports",
     "Browse & filter monitors", "Discord (Redirect)"],
)
def test_display_laws_clean(text: str) -> None:
    assert display_law_violations(text, load_naming_vocab()) == []


def test_pf_uf_twin_law() -> None:
    v = load_naming_vocab()
    assert display_law_violations("Settings", v, pf_display="Settings") == [
        "pf_uf_twin"
    ]
    assert display_law_violations(
        "Manage settings", v, pf_display="Settings") == []
    # Case-insensitive (operator exhibit-6: Soc0 entra / Entra).
    assert "pf_uf_twin" in display_law_violations(
        "entra", v, pf_display="Entra")


# ── Candidate builders ──────────────────────────────────────────────────


def test_hub_composition_candidates_win_for_vendor_pfs() -> None:
    v = load_naming_vocab()
    assert hub_composition_display(
        "hub:backend/services/edr/claroty", "Claroty", v) == "EDR — Claroty"
    assert hub_composition_display(
        "hub:packages/banking/src/providers/gocardless", "Gocardless", v,
    ) == "Banking — GoCardless"
    assert hub_composition_display(
        "hub:packages/app-store/src/cal", "Cal", v) == "App Store — Cal"
    # Hub CORE anchors never compose (their display carries "Core").
    assert hub_composition_display(
        "hub:backend/services/edr", "Edr Core", v) is None

    pf = _pf("gocardless", "Gocardless",
             "hub:packages/banking/src/providers/gocardless")
    cands = build_pf_candidates(pf, v)
    assert cands[0] == "Banking — GoCardless"
    assert "GoCardless" in cands  # polished current is always present


def test_humanize_qualified_slug_verbose_display() -> None:
    """The openstatus exhibit: the collision-qualified mint display
    'Discord (Route Apps Web Src App Landing Redirect Discord)' →
    candidates lead with the humanized 'Discord' (+ local qualifier)."""
    v = load_naming_vocab()
    base, qual = humanize_anchor_display(
        "route:apps/web/src/app/(landing)/(redirect)/discord", v)
    assert (base, qual) == ("Discord", "Redirect")
    pf = _pf(
        "discord-(route-apps-web-src-app-landing-redirect-discord)",
        "Discord (Route Apps Web Src App Landing Redirect Discord)",
        "route:apps/web/src/app/(landing)/(redirect)/discord",
    )
    cands = build_pf_candidates(pf, v)
    assert cands[0] == "Discord"
    assert "Discord (Redirect)" in cands


def test_uf_candidates_twin_gets_journey_template_first() -> None:
    v = load_naming_vocab()
    pf = _pf("settings", "Settings", "route:app/settings")
    uf = _uf("UF-001", "Settings", "settings", synthesized=True,
             members=["edit-settings-flow"])
    cands = build_uf_candidates(uf, pf, v, ["edit-settings-flow"])
    assert cands[0] == "Manage settings"


def test_uf_candidates_vendor_journey_from_flow_evidence() -> None:
    v = load_naming_vocab()
    pf = _pf("gocardless", "Banking — GoCardless",
             "hub:packages/banking/src/providers/gocardless")
    uf = _uf("UF-002", "Banking — GoCardless", "gocardless",
             synthesized=True, members=["sync-transactions-flow"])
    cands = build_uf_candidates(
        uf, pf, v, ["sync-transactions-flow", "fetch-rates-flow"])
    # ingest verbs in member flows → vendor ingest template, on the
    # VENDOR half of the composed display.
    assert cands[0] == "Ingest data from GoCardless"

    uf2 = _uf("UF-003", "Claroty", "claroty", synthesized=True,
              members=["oauth-callback-flow"])
    pf2 = _pf("claroty", "EDR — Claroty", "hub:backend/services/edr/claroty")
    cands2 = build_uf_candidates(uf2, pf2, v, ["oauth-callback-flow"])
    assert cands2[0] == "Connect Claroty"


def test_uf_candidates_clean_existing_name_stays_first() -> None:
    """No churn of good journey names: a law-clean 6.7b/6.7d name leads
    its candidate list."""
    v = load_naming_vocab()
    pf = _pf("monitors", "Monitors", "route:apps/dashboard/monitors")
    uf = _uf("UF-004", "Create and manage monitors", "monitors",
             members=["create-monitor-flow"])
    cands = build_uf_candidates(uf, pf, v, ["create-monitor-flow"])
    assert cands[0] == "Create and manage monitors"


# ── Stage runner — the exhibit fixtures end-to-end ──────────────────────


def _midday_shape() -> tuple[list[Feature], list[UserFlow]]:
    pfs = [
        _pf("banking", "Banking", "ws:packages/banking"),
        _pf("gocardless", "Gocardless",
            "hub:packages/banking/src/providers/gocardless"),
        _pf("chatgpt-mcp", "Chatgpt Mcp",
            "hub:packages/app-store/src/chatgpt-mcp"),
        _pf("edr-core", "Edr Core", "hub:backend/services/edr"),
        _pf("settings", "Settings", "route:apps/dashboard/settings"),
    ]
    ufs = [
        _uf("UF-001", "Settings", "settings", synthesized=True,
            members=["edit-settings-flow"]),
        _uf("UF-002", "Gocardless", "gocardless", synthesized=True,
            members=["sync-transactions-flow"]),
        _uf("UF-003", "Manage banking connections", "banking",
            members=["connect-bank-flow"]),
    ]
    return pfs, ufs


def test_run_naming_contract_fixes_exhibit_classes() -> None:
    pfs, ufs = _midday_shape()
    tele = run_naming_contract(pfs, ufs, [])
    disp = {p.name: p.display_name for p in pfs}
    assert disp["gocardless"] == "Banking — GoCardless"
    assert disp["chatgpt-mcp"] == "App Store — ChatGPT MCP"
    assert disp["edr-core"] == "EDR Core"
    assert disp["settings"] == "Settings"
    # H3 twins dead: every UF display differs from its PF display.
    by_slug = {p.name: p.display_name for p in pfs}
    for uf in ufs:
        assert uf.name.strip().lower() != (
            by_slug.get(uf.product_feature_id) or "").strip().lower(), uf.name
    assert ufs[0].name == "Manage settings"
    assert ufs[1].name == "Ingest data from GoCardless"
    assert ufs[2].name == "Manage banking connections"  # untouched
    # UF-001 dies via the twin law; UF-002's PF display was composed in
    # Pass 1 (no longer an exact twin) and dies via the synth template.
    assert tele["uf_twins_resolved"] >= 1
    assert tele["uf_synth_named"] == 2


def test_run_naming_contract_identity_untouched() -> None:
    """HARD LAW fixture check: canonical ids/slugs/joins are byte-equal
    before and after the stage (identity ≠ display)."""
    pfs, ufs = _midday_shape()
    pf_identity = [
        (p.name, getattr(p, "anchor_id", None), p.layer, tuple(p.paths))
        for p in pfs
    ]
    uf_identity = [
        (u.id, u.resource, u.intent, u.product_feature_id,
         tuple(u.member_flow_ids or []))
        for u in ufs
    ]
    run_naming_contract(pfs, ufs, [])
    assert pf_identity == [
        (p.name, getattr(p, "anchor_id", None), p.layer, tuple(p.paths))
        for p in pfs
    ]
    assert uf_identity == [
        (u.id, u.resource, u.intent, u.product_feature_id,
         tuple(u.member_flow_ids or []))
        for u in ufs
    ]


def test_run_naming_contract_display_collision_qualifies() -> None:
    """Two PFs whose top candidates collide: the second (slug-sorted)
    takes the qualified form — never two identical displays."""
    pfs = [
        _pf("discord-(route-a)", "Discord (Route A)",
            "route:apps/web/src/app/(landing)/(redirect)/discord"),
        _pf("discord", "Discord", "route:apps/backend/channels/discord"),
    ]
    run_naming_contract(pfs, [], [])
    displays = [p.display_name for p in pfs]
    assert len({d.strip().lower() for d in displays}) == 2, displays
    assert "Discord" in displays


def test_law_violating_displays_replaced_deterministically() -> None:
    pfs = [
        _pf("schema-json", "Schema.json", "route:apps/api/schema.json"),
        _pf("slug-param", "$slug", None),
    ]
    tele = run_naming_contract(pfs, [], [])
    for p in pfs:
        assert not display_law_violations(
            p.display_name or "", load_naming_vocab()), p.display_name
    assert tele["laws_fixed"].get("file_stem", 0) >= 1
    assert tele["laws_fixed"].get("param", 0) >= 1


# ── Keeper pin channel (§4.8 — content-derived prev-scan join) ──────────


def _prev_scan() -> dict:
    return {
        "product_features": [
            {"name": "settings", "display_name": "Workspace Settings",
             "anchor_id": "route:apps/dashboard/settings"},
            {"name": "gocardless", "display_name": "Banking — GoCardless",
             "anchor_id": "hub:packages/banking/src/providers/gocardless"},
            {"name": "schema-json", "display_name": "schema.json",
             "anchor_id": "route:apps/api/schema.json"},
        ],
    }


def test_pf_display_pinned_from_prev_scan_by_anchor_id() -> None:
    pfs, ufs = _midday_shape()
    tele = run_naming_contract(pfs, ufs, [], prev_scan=_prev_scan(),
                               keeper_on=True)
    disp = {p.name: p.display_name for p in pfs}
    # Pinned displays win over fresh candidates (stability).
    assert disp["settings"] == "Workspace Settings"
    assert disp["gocardless"] == "Banking — GoCardless"
    assert tele["pf_pinned"] >= 2


def test_law_violating_pin_rejected() -> None:
    """LAW > PIN: a previous scan's 'schema.json' display never
    re-enters via the keeper."""
    pfs = [_pf("schema-json", "Api Schema", "route:apps/api/schema.json")]
    tele = run_naming_contract(pfs, [], [], prev_scan=_prev_scan(),
                               keeper_on=True)
    assert pfs[0].display_name == "API Schema"
    assert tele["pf_pin_rejected_law"] == 1


def test_keeper_kill_switch_disables_pinning() -> None:
    pfs, ufs = _midday_shape()
    tele = run_naming_contract(pfs, ufs, [], prev_scan=_prev_scan(),
                               keeper_on=False)
    disp = {p.name: p.display_name for p in pfs}
    assert disp["settings"] == "Settings"  # fresh, not pinned
    assert tele["pf_pinned"] == 0


def test_pinned_uf_name_respected_unless_law_violating() -> None:
    pfs = [_pf("settings", "Settings", "route:apps/dashboard/settings")]
    pinned_clean = _uf(
        "UF-001", "Manage workspace settings", "settings",
        members=["edit-settings-flow"],
        identity={"pinned_from": "prev", "prev_id": "UF-001"})
    pinned_twin = _uf(
        "UF-002", "Settings", "settings", members=["edit-settings-flow"],
        identity={"pinned_from": "prev", "prev_id": "UF-002"})
    tele = run_naming_contract(pfs, [pinned_clean, pinned_twin], [],
                               keeper_on=True)
    assert pinned_clean.name == "Manage workspace settings"  # stability
    assert pinned_twin.name == "Manage settings"             # law wins
    assert tele["uf_pin_overridden_by_law"] == 1


# ── Env gates ───────────────────────────────────────────────────────────


def test_naming_contract_env_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAULTLINE_NAMING_CONTRACT", raising=False)
    assert naming_contract_enabled() is True
    monkeypatch.setenv("FAULTLINE_NAMING_CONTRACT", "0")
    assert naming_contract_enabled() is False


def test_keeper_env_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from faultline.pipeline_v2.uf_identity_keeper import keeper_enabled
    monkeypatch.delenv("FAULTLINE_KEEPER", raising=False)
    assert keeper_enabled() is True
    monkeypatch.setenv("FAULTLINE_KEEPER", "0")
    assert keeper_enabled() is False


def test_new_flags_registered_for_scan_result_cache() -> None:
    from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS
    for flag in (
        "FAULTLINE_NAMING_CONTRACT",
        "FAULTLINE_KEEPER",
        "FAULTLINE_PERSONA_LABELER",
        "FAULTLINE_PERSONA_LABELER_MODEL",
        "FAULTLINE_PERSONA_ADJUDICATOR",
        "FAULTLINE_PERSONA_VERIFIER",
        "FAULTLINE_PERSONA_ESCALATION_MODEL",
    ):
        assert flag in ENV_OUTPUT_FLAGS, flag
