"""B2 — route-template humanization of anchor-derived product-feature
display names (``FAULTLINE_HUMANIZE_ROUTE_NAMES``).

Router file-system conventions leak dialect glyphs into product names a
PM cannot read — Remix flat-routes ``$param`` / trailing ``+`` / ``_layout``
prefix / ``[escaped]`` literal / dot-separator, plus Next/Svelte/Astro
``[param]`` and ``(group)``. Fixtures mirror the wave8 documenso exhibits
verbatim (engine 5eb4caf): ``T.$team URL+ (Authenticated+)``, ``Admin+``,
``Settings+``, ``API+``, ``Internal+`` (``[__htmltopdf]`` dropped, layout
kept — backwards), ``P.$URL``.

Every rule is generic to the router dialect — never a repo-specific
literal (rule-no-repo-specific-paths). The kill-switch ``=0`` restores the
pre-B2 (legacy) anchor humanization byte-identically.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2.naming_contract import (
    _has_route_template_residue,
    _param_noun,
    _peel_edge_single_letters,
    _route_dialect,
    _strip_display_residue,
    build_pf_candidates,
    display_law_violations,
    humanize_anchor_display,
    humanize_route_names_enabled,
    load_naming_vocab,
    run_naming_contract,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _pf(slug: str, display: str, anchor_id: str) -> Feature:
    f = Feature(
        name=slug, display_name=display, layer="product",
        paths=[], authors=["a"], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_NOW, health_score=100.0,
    )
    f.anchor_id = anchor_id
    return f


@pytest.fixture()
def vocab() -> dict:
    return load_naming_vocab()


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAULTLINE_HUMANIZE_ROUTE_NAMES", "1")


# ── kill-switch ─────────────────────────────────────────────────────────


def test_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAULTLINE_HUMANIZE_ROUTE_NAMES", raising=False)
    assert humanize_route_names_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "FALSE"])
def test_flag_off_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("FAULTLINE_HUMANIZE_ROUTE_NAMES", val)
    assert humanize_route_names_enabled() is False


def test_flag_off_restores_legacy_route_names(
    monkeypatch: pytest.MonkeyPatch, vocab: dict,
) -> None:
    """With the kill-switch off the anchor words are the pre-B2 (dirty)
    forms byte-identically — proof the fix is fully gated."""
    monkeypatch.setenv("FAULTLINE_HUMANIZE_ROUTE_NAMES", "0")
    base, qual = humanize_anchor_display(
        "route:apps/remix/app/routes/_authenticated+/admin+", vocab)
    assert base == "Admin+"
    assert qual == "Authenticated+"


# ── router-dialect capability detection (no stack-name literals) ────────


@pytest.mark.parametrize("path", [
    "apps/remix/app/routes/_authenticated+/admin+",   # trailing '+'
    "app/routes/_authenticated+/t.$teamUrl+",         # '$' param
    "app/routes/settings/billing",                    # app|src/routes root
])
def test_dialect_flat_routes_bracket_escape(path: str) -> None:
    d = _route_dialect(path)
    assert d.flat_nesting is True
    assert d.bracket_escape is True     # '[x]' is a literal escape here


@pytest.mark.parametrize("path", [
    "app/(marketing)/[id]/page",                       # bracket-dynamic dialect
    "src/pages/settings/[slug]",
    "check-email",
])
def test_dialect_bracket_dynamic(path: str) -> None:
    d = _route_dialect(path)
    assert d.flat_nesting is False
    assert d.bracket_escape is False    # '[x]' is a dynamic param → drop


# ── Remix per-rule (trailing '+', layout prefix, escape, dot, tenancy) ──


def test_remix_trailing_plus_and_layout_prefix(vocab: dict) -> None:
    base, qual = humanize_anchor_display(
        "route:apps/remix/app/routes/_authenticated+/admin+", vocab)
    assert base == "Admin"                 # '+' stripped
    assert qual != "Authenticated+"        # '_authenticated' layout dropped
    assert not _has_route_template_residue(base or "")


def test_remix_settings_plus(vocab: dict) -> None:
    base, _ = humanize_anchor_display(
        "route:apps/remix/app/routes/_authenticated+/settings+", vocab)
    assert base == "Settings"


def test_remix_api_terminal_kept_as_acronym(vocab: dict) -> None:
    # 'api' is transparent-when-deeper, but as the terminal capability it
    # keys its own surface and renders as the known acronym.
    base, _ = humanize_anchor_display(
        "route:apps/remix/app/routes/api+", vocab)
    assert base == "API"


def test_remix_escaped_literal_unwrapped_not_dropped(vocab: dict) -> None:
    # '[__htmltopdf]' is a Remix ESCAPE (literal), not a dynamic param:
    # it must survive (unwrapped), and the '_internal' LAYOUT must drop.
    base, _ = humanize_anchor_display(
        "route:apps/remix/app/routes/_internal+/[__htmltopdf]+", vocab)
    assert base == "Htmltopdf"
    assert "internal" not in (base or "").lower()   # layout leak gone
    assert not _has_route_template_residue(base or "")


def test_remix_dotnotation_tenancy_noun(vocab: dict) -> None:
    # '/t/$teamUrl' — single-char scaffold 't' drops, tenancy param
    # '$teamUrl' surfaces its noun 'team' (addressing suffix 'Url' drops).
    base, _ = humanize_anchor_display(
        "route:apps/remix/app/routes/_authenticated+/t.$teamUrl+", vocab)
    assert base == "Team"


# ── Next / generic dialect ('[param]' drop, '(group)' unwrap) ───────────


def test_next_dynamic_param_dropped(vocab: dict) -> None:
    base, _ = humanize_anchor_display("route:app/[id]/settings", vocab)
    assert base == "Settings"


def test_next_route_group_unwrapped(vocab: dict) -> None:
    base, _ = humanize_anchor_display("route:app/(marketing)/pricing", vocab)
    assert base == "Pricing"


# ── tenancy transparency anti-case (NOT promoted when addressing) ───────


def test_tenancy_scope_transparent_when_deeper(vocab: dict) -> None:
    # '/workspaces/{id}/tables' keys TABLES, never a Workspaces blob
    # (mirrors spine_anchors tenancy-transparency).
    base, _ = humanize_anchor_display(
        "route:app/routes/workspaces/$id/tables", vocab)
    assert base == "Tables"


def test_tenancy_scope_terminal_keys_surface(vocab: dict) -> None:
    # A literal '/workspaces' index still keys the Workspaces surface.
    base, _ = humanize_anchor_display("route:app/workspaces", vocab)
    assert base == "Workspaces"


# ── _param_noun unit ────────────────────────────────────────────────────


@pytest.mark.parametrize("param,expected", [
    ("$teamUrl", "team"),
    ("$documentId", "document"),
    ("$id", None),
    ("$slug", None),
    ("$token", None),
    (":workspaceId", "workspace"),
    ("{orgSlug}", "org"),
])
def test_param_noun(vocab: dict, param: str, expected: str | None) -> None:
    assert _param_noun(param, vocab) == expected


# ── _peel_edge_single_letters unit ──────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("P URL", "URL"),
    ("Team Verify Email Token", "Team Verify Email Token"),
    ("HTML To PDF", "HTML To PDF"),
    ("A", "A"),                       # all-single-letter kept (never nuke)
    ("Check Email", "Check Email"),
])
def test_peel_edge_single_letters(text: str, expected: str) -> None:
    assert _peel_edge_single_letters(text) == expected


# ── residue detector ────────────────────────────────────────────────────


@pytest.mark.parametrize("text,dirty", [
    ("Admin+", True),
    ("API+", True),
    ("P.$URL", True),
    ("T.$team URL+ (Authenticated+)", True),
    ("_internal", True),
    ("Admin", False),
    ("Check Email", False),
    ("Team", False),
])
def test_route_template_residue(text: str, dirty: bool) -> None:
    assert _has_route_template_residue(text) is dirty


# ── full contract over the documenso exhibit set ────────────────────────


_DOCUMENSO = [
    ("t.$team-url+", "T.$team URL+ (Authenticated+)",
     "route:apps/remix/app/routes/_authenticated+/t.$teamUrl+", "Team"),
    ("admin+", "Admin+",
     "route:apps/remix/app/routes/_authenticated+/admin+", "Admin"),
    ("settings+", "Settings+",
     "route:apps/remix/app/routes/_authenticated+/settings+", "Settings"),
    ("api+", "API+", "route:apps/remix/app/routes/api+", "API"),
    ("[-htmltopdf]+", "Internal+",
     "route:apps/remix/app/routes/_internal+/[__htmltopdf]+", "Htmltopdf"),
    ("p.$url", "P.$URL", "route:p-url", "URL"),
    ("team.verify.email.$token", "Team Verify Email Token",
     "route:team-verify-email-token", "Team Verify Email Token"),
]


def test_documenso_exhibits_humanized_and_slug_stable() -> None:
    pfs = [_pf(s, d, a) for s, d, a, _ in _DOCUMENSO]
    slugs_before = [p.name for p in pfs]
    anchors_before = [p.anchor_id for p in pfs]
    run_naming_contract(pfs, [], [])
    for pf, (slug, _d, anchor, expected) in zip(pfs, _DOCUMENSO):
        assert pf.display_name == expected, (
            f"{slug}: got {pf.display_name!r}, want {expected!r}")
        assert not _has_route_template_residue(pf.display_name or "")
    # HARD LAW: identity (name / anchor_id slug refs) never mutated.
    assert [p.name for p in pfs] == slugs_before
    assert [p.anchor_id for p in pfs] == anchors_before


def test_documenso_deterministic_double_run() -> None:
    def once() -> list[str]:
        pfs = [_pf(s, d, a) for s, d, a, _ in _DOCUMENSO]
        run_naming_contract(pfs, [], [])
        return [p.display_name for p in pfs]
    assert once() == once()


# ── anti-cases: never over-rewrite good / non-route names ───────────────


def test_non_route_anchor_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace-package PF ('ws:' anchor) is not a route — the route
    humanizer never fires; a good display is preserved verbatim."""
    pfs = [
        _pf("auth", "Auth", "ws:packages/auth"),
        _pf("email", "Email", "route:email"),
    ]
    run_naming_contract(pfs, [], [])
    assert pfs[0].display_name == "Auth"
    assert pfs[1].display_name == "Email"


def test_legit_plus_in_prose_on_non_route_preserved() -> None:
    """The route-residue rule is scoped to 'route:' anchors: a legitimate
    '+' in a NON-route display ('Enterprise+' plan tier) is never
    stripped (it is not router-nesting machinery)."""
    pfs = [_pf("enterprise-plus", "Enterprise+", "ws:packages/billing")]
    run_naming_contract(pfs, [], [])
    assert pfs[0].display_name == "Enterprise+"


def test_clean_route_names_not_churned() -> None:
    """Already-human route displays keep their exact name (no churn)."""
    pfs = [
        _pf("check-email", "Check Email", "route:check-email"),
        _pf("forgot-password", "Forgot Password", "route:forgot-password"),
    ]
    run_naming_contract(pfs, [], [])
    assert pfs[0].display_name == "Check Email"
    assert pfs[1].display_name == "Forgot Password"


def test_tenancy_param_not_promoted_to_feature_name(vocab: dict) -> None:
    """A pure-addressing tenancy param ('$id' after a scope word) is never
    promoted; the capability after it names the surface."""
    base, _ = humanize_anchor_display(
        "route:app/routes/teams/$teamId/members", vocab)
    assert base == "Members"
    assert "team" not in (base or "").lower()


def test_strip_display_residue_last_resort(vocab: dict) -> None:
    out = _strip_display_residue("T.$team URL+", vocab)
    assert out is not None
    assert not _has_route_template_residue(out)
