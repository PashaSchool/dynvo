"""B71 Seg A — PF display route-grammar (L-A1) + provenance ladder (L-A2).

Named units for every census-class-A/B exhibit and every §4 anti-case. Fixtures
are synthetic; they hold the MECHANISM (dictionary-free — provenance decides,
shape never bans a word). The corpus name-census (43 raw-token + 57 generic-leaf
-> targeted falls) is the operator's keyed A/B.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from faultline.pipeline_v2.naming_contract import naming_pack_enabled
from faultline.pipeline_v2.pf_display_provenance import (
    ProvenanceSources,
    apply_pf_display_provenance,
    clean_route_grammar_display,
    resolve_pf_display,
)

_GLYPH = re.compile(r"[$:{}\[\]<>*.]|\+(?=\s|$)")


def _src(**kw: str) -> ProvenanceSources:
    return ProvenanceSources(**kw)


# ── flag default OFF (byte-identity is the gated call site's; here the gate) ──


def test_naming_pack_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # SEMANTIC (horizon-1 flip): unset now defaults ON.
    monkeypatch.delenv("FAULTLINE_NAMING_PACK", raising=False)
    assert naming_pack_enabled() is True
    monkeypatch.setenv("FAULTLINE_NAMING_PACK", "0")
    assert naming_pack_enabled() is False
    monkeypatch.setenv("FAULTLINE_NAMING_PACK", "1")
    assert naming_pack_enabled() is True


def test_inverted_killswitch_naming_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inverted kill-switch: unset ≡ explicit ``1`` (default ON); explicit
    ``0``/``false`` == the pre-B71 OFF behaviour."""
    monkeypatch.delenv("FAULTLINE_NAMING_PACK", raising=False)
    unset = naming_pack_enabled()
    monkeypatch.setenv("FAULTLINE_NAMING_PACK", "1")
    assert naming_pack_enabled() is unset is True
    monkeypatch.setenv("FAULTLINE_NAMING_PACK", "0")
    assert naming_pack_enabled() is False
    monkeypatch.setenv("FAULTLINE_NAMING_PACK", "false")
    assert naming_pack_enabled() is False


# ── L-A1: route grammar never passes to display literally ─────────────────────


def test_la1_documenso_htmltopdf() -> None:
    """documenso ``[-htmltopdf]+`` — bracket-escape + flat-route ``+`` machinery
    removed."""
    out = clean_route_grammar_display("[-htmltopdf]+")
    assert out == "Htmltopdf"
    assert not _GLYPH.search(out)


def test_la1_documenso_team_verify_token() -> None:
    """documenso ``team.verify.email.$token`` — Remix dot separator reads as
    words; the ``$token`` opaque id drops."""
    out = clean_route_grammar_display("team.verify.email.$token")
    assert out == "Team Verify Email"
    assert not _GLYPH.search(out)


def test_la1_documenso_p_url_glyph_free() -> None:
    """documenso ``p.$url`` — glyphs gone (exact human token is ambiguous; the
    contract is only that no router machinery survives)."""
    out = clean_route_grammar_display("p.$url")
    assert not _GLYPH.search(out)


# ── L-A1 anchor-kind scoping of the trailing-'+' rung (horizon-1 ruling
# 2026-07-16, escalation #3: the older humanize law is RIGHT — in route
# slugs '+' is Remix flat-route syntax; on ws:/fdir:/hub: anchors '+' is a
# legitimate name character). Glyph rungs ($ : { } [ ] < > *) stay
# text-based for every anchor kind. ──────────────────────────────────────


def test_la1_route_anchor_trailing_plus_stripped() -> None:
    """documenso shape: a route:-anchored display carrying the Remix
    trailing-'+' IS residue — the law cleans it."""
    v = resolve_pf_display(
        "Admin+", _src(anchor_source="route", basename="admin+"))
    assert v.display == "Admin"
    assert v.changed is True


def test_la1_ws_anchor_plus_is_prose_untouched() -> None:
    """ANTI-CASE (the ruling's subject): 'Enterprise+' on a ws: anchor is a
    plan-tier name, not router machinery — preserved verbatim."""
    v = resolve_pf_display("Enterprise+", _src(anchor_source="ws"))
    assert v.display == "Enterprise+"
    assert v.changed is False


@pytest.mark.parametrize("anchor", ["fdir", "hub", ""])
def test_la1_nonroute_anchor_plus_preserved(anchor: str) -> None:
    """fdir:/hub:/unknown anchors get the same preservation — only route:
    arms the '+' rung; unknown shapes err to preservation."""
    v = resolve_pf_display("Notepad++ Tools", _src(anchor_source=anchor))
    assert v.display == "Notepad++ Tools"
    assert v.changed is False


def test_la1_glyph_rungs_stay_text_based_on_ws_anchor() -> None:
    """The ruling scopes ONLY the '+' rung: a real param glyph on a ws:
    anchor is still residue (those glyphs are never prose)."""
    v = resolve_pf_display(
        "Team $token", _src(anchor_source="ws", manifest="Teams"))
    assert v.display == "Teams"
    assert v.changed is True


# ── L-A2: provenance ladder upgrades a defective display ──────────────────────


def test_la2_btcpayserver_manifest_upgrade() -> None:
    """cal ``Btcpayserver`` (glued-lowercase bare basename) upgrades to the
    package manifest's declared name."""
    v = resolve_pf_display("Btcpayserver", _src(manifest="BTCPay Server", basename="btcpayserver"))
    assert v.display == "BTCPay Server"
    assert v.provenance == "package-manifest"
    assert v.changed is True


def test_la2_typo_passthrough_fixed_by_manifest() -> None:
    """cal ``App Store — Insihts`` — the source typo in the hub LEAF is corrected
    by the manifest tier (never by a spelling dictionary)."""
    v = resolve_pf_display("App Store — Insihts", _src(manifest="Insights", basename="insihts"))
    assert v.display == "App Store — Insights"
    assert v.provenance == "package-manifest"


def test_la2_bare_basename_no_higher_source_is_honest_debt() -> None:
    """A bare basename with NO higher-provenance source is KEPT (honest debt) —
    the law never invents a name, never bans the token."""
    v = resolve_pf_display("Btcpayserver", _src(basename="btcpayserver"))
    assert v.display == "Btcpayserver"
    assert v.changed is False
    assert v.provenance == "dir-basename"


# ── ANTI-CASES (census §4): identical shape, saved by provenance ──────────────


@pytest.mark.parametrize("name, src", [
    # hoppscotch — real shipped products, token-shape identical to disease
    ("CLI", _src(basename="cli")),
    ("Relay", _src(basename="relay")),
    # novu — literal product names / core surfaces
    ("Framework", _src(nav="Framework", basename="framework")),
    ("Provider", _src(nav="Provider", basename="provider")),
    ("Preferences", _src(nav="Preferences", basename="preferences")),
    # plane — real user-facing surfaces (echo is the disease, not the noun)
    ("API Tokens", _src(nav="API Tokens", basename="api-tokens")),
    ("Spaces", _src(nav="Spaces", basename="spaces")),
    # cal.com — real nav hubs / product areas
    ("Settings", _src(nav="Settings", basename="settings")),
    ("Auth", _src(nav="Auth", basename="auth")),
    ("Insights", _src(nav="Insights", basename="insights")),
])
def test_anticases_survive_unchanged(name: str, src: ProvenanceSources) -> None:
    v = resolve_pf_display(name, src)
    assert v.display == name
    assert v.changed is False


def test_anticase_propel_is_lane_not_rename() -> None:
    """plane ``Propel`` — the name is EXACT (a real package); its defect is a
    LANE (Seg B L-B2), NOT a rename. Seg A must leave it untouched."""
    v = resolve_pf_display("Propel", _src(basename="propel"))
    assert v.display == "Propel"
    assert v.changed is False


# ── driver: applies in place + records provenance telemetry ───────────────────


def test_apply_driver_mutates_and_records_tiers() -> None:
    pfs = [
        SimpleNamespace(name="btcpayserver", display_name="Btcpayserver", anchor_id="x"),
        SimpleNamespace(name="settings", display_name="Settings", anchor_id="y"),
        SimpleNamespace(name="htmltopdf", display_name="[-htmltopdf]+", anchor_id="z"),
    ]
    srcs = {
        "btcpayserver": _src(manifest="BTCPay Server", basename="btcpayserver"),
        "settings": _src(nav="Settings", basename="settings"),
        "htmltopdf": _src(basename="[-htmltopdf]+"),
    }
    tele = apply_pf_display_provenance(pfs, lambda pf: srcs[pf.name])
    assert pfs[0].display_name == "BTCPay Server"     # manifest upgrade
    assert pfs[1].display_name == "Settings"          # nav-confirmed, untouched
    assert pfs[2].display_name == "Htmltopdf"         # route-grammar cleaned
    assert tele["pf_provenance_upgraded"] == 1        # btcpay
    assert tele["pf_route_grammar_cleaned"] == 1      # htmltopdf
    assert tele["name_provenance"]["settings"] == "nav"
    assert tele["name_provenance"]["btcpayserver"] == "package-manifest"
