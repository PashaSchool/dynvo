"""B33 — route/fdir devgrain-leaf mint gate (journey-step / plumbing leaves).

A ``route:``/``fdir:``-anchored candidate whose normalized leaf names a
plumbing screen or a journey STEP (welcome, getting-started, access-denied,
redirect-*, *-callback …) is journey-grain by construction — it must FOLD via
the existing ``_parent_fold`` rail instead of minting a flowless devgrain PF.

Live exhibits distilled here (wave16 dashboard, engine @2459f81):
  * papermark PF ``Welcome`` (route:welcome, 6982 LOC, 0 flows);
  * cal.com ``Getting Started`` (fdir:apps/web/modules/getting-started);
  * novu ``Access Denied`` (route:access-denied-page);
  * novu ``Redirect To Legacy Studio Auth`` (route:redirect-to-legacy-studio-auth).

Anti-cases that MUST survive (author-declared in the product IA): kan
``Onboarding``, midday ``Onboarding``, novu ``Error`` — all nav-confirmed;
the gate NEVER fires on a nav-confirmed anchor. See test_anti_cases_*.

Corroboration only: the token set never kills alone — the kill requires
token-match AND no nav declaration AND a readable board nav parse.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.spine_anchors import SpineAnchor, load_spine_vocab
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    FDIR_DEVGRAIN_GATE_ENV,
    _mint_bar,
    fdir_devgrain_gate_enabled,
    is_journey_step_leaf,
    journey_step_leaf_tokens,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_CODE_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".vue")


# ── Fixtures ─────────────────────────────────────────────────────────────


def _tokens() -> dict[str, frozenset[str]]:
    return journey_step_leaf_tokens(load_spine_vocab())


def _win() -> list[Feature]:
    """One synthetic winning dev — the bar returns ``no_winning_devs`` on an
    empty list, so every case needs at least one winner."""
    return [
        Feature(
            name="w", paths=["app/x/page.tsx"],
            member_files=[MemberFile(path="app/x/page.tsx", role="anchor",
                                     confidence=1.0, primary=True)],
            flows=[], product_feature_id=None,
            authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
            last_modified=_NOW, health_score=100.0,
        )
    ]


def _route_anchor(key: str, *, nav_confirmed: bool = False,
                  source: str = "route") -> SpineAnchor:
    """A route/fdir anchor that would OTHERWISE mint (carries page evidence +
    a real subtree prefix), so any bar it earns comes from the B33 prong."""
    prefix = f"app/{key}"
    page = frozenset({f"{prefix}/page.tsx"})
    return SpineAnchor(
        canonical_id=f"{source}:{prefix}", key=key, source=source,
        display=key.replace("-", " ").title(),
        prefixes=(prefix,), files=page, sources=frozenset({source}),
        nav_confirmed=nav_confirmed, page_route_files=page,
    )


def _bar(anchor: SpineAnchor, *, nav_readable: bool = True) -> str | None:
    return _mint_bar(
        anchor, _win(), {}, True, _CODE_EXTS, Path("."), {},
        devgrain_tokens=_tokens(), nav_readable=nav_readable,
    )


# ── Flag helper ──────────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FDIR_DEVGRAIN_GATE_ENV, raising=False)
    assert not fdir_devgrain_gate_enabled()
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    assert fdir_devgrain_gate_enabled()
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "0")
    assert not fdir_devgrain_gate_enabled()


# ── The 4 live exhibits bar (flag ON, nav readable, not nav-confirmed) ────


@pytest.mark.parametrize(
    ("key", "source"),
    [
        ("welcome", "route"),                          # papermark
        ("getting-started", "fdir"),                   # cal.com
        ("access-denied-page", "route"),               # novu (-page strip)
        ("redirect-to-legacy-studio-auth", "route"),   # novu (redirect-* prefix)
    ],
)
def test_exhibits_bar(monkeypatch: pytest.MonkeyPatch,
                      key: str, source: str) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    assert _bar(_route_anchor(key, source=source)) == "journey_step_leaf"


# ── Anti-cases: nav-confirmed leaves NEVER bar ───────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "onboarding",      # kan / midday Onboarding (route, NAV=true)
        "error",           # novu Error (route, NAV=true)
        "welcome",         # nav-declared welcome must also survive
        "getting-started",
    ],
)
def test_anti_cases_nav_confirmed_survive(
    monkeypatch: pytest.MonkeyPatch, key: str,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    # nav_confirmed=True → author declared it in the IA → product by their
    # word → the gate abstains and the anchor mints (bar is None).
    assert _bar(_route_anchor(key, nav_confirmed=True)) is None


# ── Compound forms bar; non-plumbing leaves do NOT ───────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "oauth-callback",              # *-callback suffix
        "aws-marketplace-onboarding",  # *-onboarding suffix
        "redirect-to-legacy-studio-auth",  # redirect-* prefix
        "enter-redirect",              # *-redirect suffix
        "logout",                      # exact
        "maintenance",                 # exact
    ],
)
def test_compound_and_exact_forms_bar(
    monkeypatch: pytest.MonkeyPatch, key: str,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    assert _bar(_route_anchor(key)) == "journey_step_leaf"


@pytest.mark.parametrize("key", ["welcome-tour", "edit", "new", "dashboard"])
def test_non_plumbing_leaves_do_not_bar(
    monkeypatch: pytest.MonkeyPatch, key: str,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    # These otherwise-mintable route surfaces must pass the gate untouched
    # (closed-set discipline: no fuzzy / substring matching).
    assert _bar(_route_anchor(key)) != "journey_step_leaf"
    assert _bar(_route_anchor(key)) is None


# ── Source discipline: only route/fdir anchors are eligible ──────────────


def test_ws_pkg_anchor_with_plumbing_key_never_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    ws = SpineAnchor(
        canonical_id="ws:packages/welcome", key="welcome", source="ws-pkg",
        display="Welcome", prefixes=("packages/welcome",),
        files=frozenset({"packages/welcome/index.ts"}),
        sources=frozenset({"ws-pkg"}),
    )
    # repo_has_pages False so the ws-pkg anchor mints cleanly; the B33 prong
    # must never fire because ws-pkg ∉ {route, fdir}.
    bar = _mint_bar(ws, _win(), {}, False, _CODE_EXTS, Path("."), {},
                    devgrain_tokens=_tokens(), nav_readable=True)
    assert bar != "journey_step_leaf"
    assert bar is None


# ── Board-wide abstain: empty nav parse → nothing bars ───────────────────


@pytest.mark.parametrize(
    ("key", "source"),
    [
        ("welcome", "route"),
        ("getting-started", "fdir"),
        ("access-denied-page", "route"),
        ("redirect-to-legacy-studio-auth", "route"),
    ],
)
def test_board_abstain_when_nav_unreadable(
    monkeypatch: pytest.MonkeyPatch, key: str, source: str,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    # nav_readable=False models an empty nav_keys board (Vue/keyless edge):
    # the exemption signal is unreadable, so the gate abstains board-wide.
    assert _bar(_route_anchor(key, source=source),
                nav_readable=False) is None


# ── Kill-switch: flag OFF → byte-identical to pre-change (bar is None) ────


@pytest.mark.parametrize(
    ("key", "source"),
    [
        ("welcome", "route"),
        ("getting-started", "fdir"),
        ("access-denied-page", "route"),
        ("redirect-to-legacy-studio-auth", "route"),
        ("oauth-callback", "route"),
        ("aws-marketplace-onboarding", "route"),
        ("enter-redirect", "route"),
        ("logout", "route"),
        ("maintenance", "route"),
    ],
)
@pytest.mark.parametrize("flag", [None, "0"])
def test_kill_switch_identical_to_pre_change(
    monkeypatch: pytest.MonkeyPatch, key: str, source: str,
    flag: str | None,
) -> None:
    if flag is None:
        monkeypatch.delenv(FDIR_DEVGRAIN_GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, flag)
    # With the gate disabled, every exhibit/compound anchor mints exactly as
    # it did before B33 existed (these anchors carry page evidence + a real
    # subtree, so the pre-B33 bar was None).
    assert _bar(_route_anchor(key, source=source)) is None


# ── Token matcher unit coverage (direct, flag-independent) ───────────────


def test_matcher_matches_required_keys() -> None:
    t = _tokens()
    for key in ("welcome", "getting-started", "access-denied-page",
                "redirect-to-legacy-studio-auth", "oauth-callback",
                "aws-marketplace-onboarding", "logout", "maintenance",
                "enter-redirect"):
        assert is_journey_step_leaf(key, t), key


def test_matcher_rejects_non_plumbing_keys() -> None:
    t = _tokens()
    for key in ("edit", "new", "dashboard", "welcome-tour", ""):
        assert not is_journey_step_leaf(key, t), key


# ── YAML block loads, is well-formed and deterministically sorted ────────


def test_yaml_block_loads_and_is_sorted() -> None:
    block = load_spine_vocab().get("journey_step_leaf_tokens")
    assert isinstance(block, dict)
    for sub in ("exact", "prefix", "suffix"):
        lst = block.get(sub)
        assert isinstance(lst, list) and lst, sub
        assert lst == sorted(lst), f"{sub} not sorted (determinism)"
        assert len(lst) == len(set(lst)), f"{sub} has duplicates"
    # The exhibits' anchoring tokens are present.
    assert "welcome" in block["exact"]
    assert "getting-started" in block["exact"]
    assert "access-denied" in block["exact"]
    assert "redirect" in block["prefix"]
    assert {"callback", "onboarding", "redirect"} <= set(block["suffix"])
