"""B16 — PF dev-grain suffix law (naming contract, display channel only).

Operator doctrine: 'there is no such thing as a page in product features'.
A route-dir-naming leak ('policy-page' -> 'Policy Page') is stripped to the
capability ('Policy'). Anchor-form-driven, NEVER token-blind: the trailing
display word is stripped ONLY when the route anchor dir ends '-<word>'.

Anti-cases: 'Landing Page Builder' (anchor '*-builder') untouched; a bare
'Page' kept (would strip to empty); non-route anchor untouched; 'flow'
stripped ONLY behind route:*-flow; post-strip COLLISION with a sibling PF
keeps the current display (the Part-2 unification signal); flag-off no-op.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature
from faultline.pipeline_v2.naming_contract import (
    PF_NAME_LAW_ENV,
    _anchor_terminal_segment,
    _apply_pf_devgrain_law,
    _strip_pf_devgrain_suffix,
    load_naming_vocab,
    pf_name_law_enabled,
    run_naming_contract,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_VOCAB = load_naming_vocab()


def _pf(slug: str, display: str, anchor_id: str) -> Feature:
    f = Feature(
        name=slug, display_name=display, layer="product", paths=[],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )
    f.anchor_id = anchor_id
    return f


# ── the strip primitive ────────────────────────────────────────────────

def test_strip_page_leak_single_word():
    assert _strip_pf_devgrain_suffix(
        "Policy Page", "route:policy-page", _VOCAB) == "Policy"


def test_strip_page_leak_multi_word():
    assert _strip_pf_devgrain_suffix(
        "API Docs Page", "route:api-docs-page", _VOCAB) == "API Docs"
    assert _strip_pf_devgrain_suffix(
        "Discover Article Profile Page",
        "route:discover-article-profile-page", _VOCAB,
    ) == "Discover Article Profile"


def test_flow_stripped_only_behind_flow_anchor():
    # anchor dir ends '-flow' -> the trailing 'Flow' is a leak.
    assert _strip_pf_devgrain_suffix(
        "Investigation Flow", "route:investigation-flow", _VOCAB
    ) == "Investigation"
    # SAME display, anchor NOT '*-flow' -> 'Flow' is a capability noun, kept.
    assert _strip_pf_devgrain_suffix(
        "Investigation Flow", "route:investigation", _VOCAB) is None


def test_over_rename_guard_landing_page_builder():
    # trailing word is 'Builder' (anchor '*-builder'); the middle 'Page' is
    # untouched — a product that BUILDS pages keeps its name.
    assert _strip_pf_devgrain_suffix(
        "Landing Page Builder", "route:landing-page-builder", _VOCAB) is None
    assert _strip_pf_devgrain_suffix(
        "Page Builder", "route:page-builder", _VOCAB) is None


def test_bare_devgrain_word_kept():
    # display is a single word that IS the token -> stripping empties it.
    assert _strip_pf_devgrain_suffix("Page", "route:page", _VOCAB) is None
    assert _strip_pf_devgrain_suffix("View", "route:view", _VOCAB) is None


def test_non_route_anchor_never_stripped():
    # fdir / hub / schema anchors are not route-dir leaks.
    assert _strip_pf_devgrain_suffix(
        "Detection Studio", "fdir:frontend/src/modules/detection-studio",
        _VOCAB) is None
    assert _strip_pf_devgrain_suffix(
        "Settings Page", "hub:backend/services/settings-page", _VOCAB) is None


def test_display_word_must_match_anchor_token():
    # anchor ends '-page' but the display's trailing word is NOT 'Page' (an
    # authored nav label won the display) -> nothing to strip.
    assert _strip_pf_devgrain_suffix(
        "Security Policies", "route:policy-page", _VOCAB) is None


def test_screen_and_view_anchor_gated():
    assert _strip_pf_devgrain_suffix(
        "Login Screen", "route:login-screen", _VOCAB) == "Login"
    assert _strip_pf_devgrain_suffix(
        "Board View", "route:board-view", _VOCAB) == "Board"
    # 'Board View' behind a non-view anchor -> kept.
    assert _strip_pf_devgrain_suffix(
        "Board View", "route:board", _VOCAB) is None


def test_terminal_segment_unwraps_groups_and_skips_params():
    assert _anchor_terminal_segment(
        "route:app/(dashboard)/policy-page") == "policy-page"
    assert _anchor_terminal_segment(
        "route:app/settings-page/[id]") == "settings-page"
    assert _anchor_terminal_segment("fdir:x/y") is None


def test_idempotent():
    once = _strip_pf_devgrain_suffix("Policy Page", "route:policy-page", _VOCAB)
    assert _strip_pf_devgrain_suffix(once, "route:policy-page", _VOCAB) is None


# ── collision handling (_apply_pf_devgrain_law) ─────────────────────────

def test_clean_strip_when_unique():
    tele: dict = {}
    chosen, stripped = _apply_pf_devgrain_law(
        "Policy Page", "route:policy-page", "policy-page", _VOCAB, {}, tele)
    assert (chosen, stripped) == ("Policy", True)
    assert tele["pf_devgrain_stripped"] == 1


def test_collision_keeps_current_and_flags_part2():
    # sibling PF 'Detections' (route:detection) already claimed the name.
    taken = {"detections": "detection"}
    tele: dict = {}
    chosen, stripped = _apply_pf_devgrain_law(
        "Detections Page", "route:detections-page", "detections-page",
        _VOCAB, taken, tele)
    assert (chosen, stripped) == ("Detections Page", False)  # unchanged
    assert tele["pf_devgrain_collision"] == 1
    sample = tele["pf_devgrain_collision_samples"][0]
    assert sample["would_be"] == "Detections"
    assert sample["collides_with"] == "detection"


# ── integration through run_naming_contract ─────────────────────────────

def _soc0_triple() -> list[Feature]:
    # the fragmented Investigations capability + a clean Page leak.
    return [
        _pf("investigation", "Investigations", "route:investigation"),
        _pf("investigations-page", "Investigations Page",
            "route:investigations-page"),
        _pf("investigation-flow", "Investigation Flow",
            "route:investigation-flow"),
        _pf("policy-page", "Policy Page", "route:policy-page"),
    ]


def test_run_contract_strips_clean_keeps_collision(monkeypatch):
    # MECHANICAL flip migration (2026-07-21 pack №2, KEY_SCHEMA 33): this
    # test pins the pre-R5 collision law — the default-ON R5 wave is
    # pinned off (R5 behavior is covered by its own test files).
    monkeypatch.setenv("FAULTLINE_NAMING_WAVE_R5", "0")
    monkeypatch.setenv(PF_NAME_LAW_ENV, "1")
    pfs = _soc0_triple()
    tele = run_naming_contract(pfs, [], [])
    by_slug = {p.name: p.display_name for p in pfs}
    assert by_slug["policy-page"] == "Policy"               # clean strip
    assert by_slug["investigation-flow"] == "Investigation"  # flow leak strip
    assert by_slug["investigation"] == "Investigations"      # untouched
    # collision loser keeps its display (never a duplicate, never a qualifier)
    assert by_slug["investigations-page"] == "Investigations Page"
    # no two identical PF displays
    disps = [p.display_name for p in pfs]
    assert len(disps) == len(set(disps))
    assert tele["pf_devgrain_stripped"] == 2
    assert tele["pf_devgrain_collision"] == 1
    assert not any("Policy Page" == d for d in disps)  # 'Page' gone from clean


def test_run_contract_flag_off_is_noop(monkeypatch):
    monkeypatch.setenv(PF_NAME_LAW_ENV, "0")
    assert not pf_name_law_enabled()
    pfs = _soc0_triple()
    tele = run_naming_contract(pfs, [], [])
    assert [p.display_name for p in pfs] == [
        "Investigations", "Investigations Page", "Investigation Flow",
        "Policy Page",
    ]
    # telemetry carries NO B16 keys when the law is off (byte-identical
    # scan_meta.naming_contract).
    assert "pf_devgrain_stripped" not in tele
    assert "pf_devgrain_collision" not in tele


def test_run_contract_no_page_survives_when_unique(monkeypatch):
    monkeypatch.setenv(PF_NAME_LAW_ENV, "1")
    pfs = [
        _pf("feeds-page", "Feeds Page", "route:feeds-page"),
        _pf("tools-page", "Tools Page", "route:tools-page"),
        _pf("webhooks-page", "Webhooks Page", "route:webhooks-page"),
    ]
    run_naming_contract(pfs, [], [])
    assert [p.display_name for p in pfs] == ["Feeds", "Tools", "Webhooks"]
