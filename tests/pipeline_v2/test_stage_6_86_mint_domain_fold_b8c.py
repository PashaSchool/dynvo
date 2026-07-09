"""B8c — mint-time domain-fold rail tests.

The rail re-homes a flowful router dev that the fold ladder folds into a
DISTINCT-domain plurality host PF onto its OWN ``<domain>-page`` surface PF,
when that surface already exists (never mints). Fixtures mirror the real Soc0
topology (``wave10-out/Soc0.json``): ``api-webhooks`` + ``webhook-detail-page``
reunite on ``webhooks-page``; ``tool-settings`` on ``settings-page``; a
``detection`` router folding into ``detections`` (SAME domain) and a
surface-less ``admin`` router (no page PF) both stay folded.

Gate coverage: the two structural rails (distinct-from-host + existing surface
PF), the PF-count-cannot-rise invariant (binds only to existing targets), the
anti-cases (same-domain / surface-less / flowless / non-fold provenance stay
put), determinism, and the ``FAULTLINE_MINT_DOMAIN_FOLD_V2`` kill-switch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from faultline.pipeline_v2.spine_anchors import SpineAnchor
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    MINT_DOMAIN_FOLD_ENV,
    _domain_family,
    _mint_domain_fold_rebinds,
    mint_domain_fold_enabled,
)


# ── helpers ──────────────────────────────────────────────────────────────

def _route(cid: str, key: str, *, pages: int = 0, apis: int = 0) -> SpineAnchor:
    return SpineAnchor(
        canonical_id=cid, key=key, source="route", display=key,
        page_route_files=frozenset(f"pg{i}" for i in range(pages)),
        api_route_files=frozenset(f"api{i}" for i in range(apis)),
    )


def _dev(name: str, *, flows: int = 1) -> SimpleNamespace:
    return SimpleNamespace(name=name, flows=[object()] * flows)


def _soc0_like():
    """A distilled Soc0 mint state: the fold host ``detections`` (route:detection,
    api-only), the ``event-routing`` host, three page surfaces, and the api
    domain anchors that bar and fold."""
    anchors = {
        "route:detection": _route("route:detection", "detection", apis=3),
        "route:event-routing": _route("route:event-routing", "event-routing",
                                       apis=1),
        "route:webhook": _route("route:webhook", "webhook", apis=1),
        "route:webhooks-page": _route("route:webhooks-page", "webhooks-page",
                                       pages=1),
        "route:setting": _route("route:setting", "setting", apis=3),
        "route:settings-page": _route("route:settings-page", "settings-page",
                                       pages=1),
        "route:admin": _route("route:admin", "admin", apis=2),  # surface-less
    }
    winner_by_dev = {
        "api-webhooks": anchors["route:webhook"],
        "webhook-detail-page": anchors["route:webhook"],
        "tool-settings": anchors["route:setting"],
        "api-detections": anchors["route:detection"],   # SAME-domain
        "api-admin": anchors["route:admin"],             # surface-less domain
        "webhooks-page": anchors["route:webhooks-page"],  # native lineage
        "settings-page": anchors["route:settings-page"],  # native lineage
    }
    dev_by_name = {n: _dev(n) for n in winner_by_dev}
    assignment = {
        "api-webhooks": ("route:detection", "fold:walk->route:webhook"),
        "webhook-detail-page": ("route:event-routing",
                                "fold:import->route:webhook"),
        "tool-settings": ("route:detection", "fold:walk->route:setting"),
        "api-detections": ("route:detection", "fold:walk->route:detection"),
        "api-admin": ("route:detection", "fold:walk->route:admin"),
        "webhooks-page": ("route:webhooks-page", "lineage"),
        "settings-page": ("route:settings-page", "lineage"),
    }
    return assignment, winner_by_dev, anchors, dev_by_name


# ── _domain_family normalization ─────────────────────────────────────────

@pytest.mark.parametrize("key,expected", [
    ("webhooks-page", "webhook"),
    ("settings-page", "setting"),
    ("prompts-page", "prompt"),
    ("suggestions-page", "suggestion"),
    ("detections-page", "detection"),
    ("webhook", "webhook"),        # api-side, already singular
    ("setting", "setting"),
    ("detection", "detection"),
    ("event-routing", "event-routing"),  # multi-token, not a surface
    ("", ""),                      # degenerate empty → passthrough
])
def test_domain_family(key: str, expected: str) -> None:
    assert _domain_family(key) == expected


# ── the rail: positive rebinds ───────────────────────────────────────────

def test_rebinds_the_three_distinct_domain_surface_devs() -> None:
    assignment, winner_by_dev, anchors, dev_by_name = _soc0_like()
    rebinds = _mint_domain_fold_rebinds(
        assignment, winner_by_dev, anchors, dev_by_name)
    got = {name: (cid, prov) for name, cid, prov in rebinds}
    assert got == {
        "api-webhooks": ("route:webhooks-page", "fold:surface->route:webhook"),
        "webhook-detail-page": ("route:webhooks-page",
                                 "fold:surface->route:webhook"),
        "tool-settings": ("route:settings-page", "fold:surface->route:setting"),
    }


def test_rebinds_are_sorted_by_dev_name_deterministic() -> None:
    assignment, winner_by_dev, anchors, dev_by_name = _soc0_like()
    a = _mint_domain_fold_rebinds(assignment, winner_by_dev, anchors,
                                  dev_by_name)
    b = _mint_domain_fold_rebinds(assignment, winner_by_dev, anchors,
                                  dev_by_name)
    assert a == b
    assert [n for n, _c, _p in a] == sorted(n for n, _c, _p in a)


def test_binds_only_to_existing_targets_never_mints() -> None:
    # Every surface a rebind points at must ALREADY be an assignment target
    # (an existing PF) — the PF-count-cannot-rise invariant.
    assignment, winner_by_dev, anchors, dev_by_name = _soc0_like()
    existing = {cid for cid, _prov in assignment.values()}
    rebinds = _mint_domain_fold_rebinds(
        assignment, winner_by_dev, anchors, dev_by_name)
    assert rebinds  # sanity
    for _name, cid, _prov in rebinds:
        assert cid in existing


# ── anti-cases: these MUST stay folded ───────────────────────────────────

def test_same_domain_router_stays_folded() -> None:
    _, winner_by_dev, anchors, dev_by_name = _soc0_like()
    assignment, *_ = _soc0_like()
    rebinds = _mint_domain_fold_rebinds(
        assignment, winner_by_dev, anchors, dev_by_name)
    assert "api-detections" not in {n for n, _c, _p in rebinds}


def test_surface_less_domain_stays_folded() -> None:
    assignment, winner_by_dev, anchors, dev_by_name = _soc0_like()
    # `admin` has no <admin>-page surface PF → never qualifies.
    rebinds = _mint_domain_fold_rebinds(
        assignment, winner_by_dev, anchors, dev_by_name)
    assert "api-admin" not in {n for n, _c, _p in rebinds}


def test_flowless_dev_never_rehomed() -> None:
    assignment, winner_by_dev, anchors, dev_by_name = _soc0_like()
    assignment["api-webhooks"] = ("route:detection", "fold:walk->route:webhook")
    dev_by_name["api-webhooks"] = _dev("api-webhooks", flows=0)  # flowless
    rebinds = _mint_domain_fold_rebinds(
        assignment, winner_by_dev, anchors, dev_by_name)
    assert "api-webhooks" not in {n for n, _c, _p in rebinds}


def test_non_fold_provenance_never_rehomed() -> None:
    # A dev bound by lineage (or any non-``fold:`` provenance) is left alone,
    # even if it is distinct-domain with an existing surface.
    assignment, winner_by_dev, anchors, dev_by_name = _soc0_like()
    assignment["api-webhooks"] = ("route:detection", "lineage")
    rebinds = _mint_domain_fold_rebinds(
        assignment, winner_by_dev, anchors, dev_by_name)
    assert "api-webhooks" not in {n for n, _c, _p in rebinds}


def test_non_route_winner_never_rehomed() -> None:
    # The domain must be a route-family domain; a schema/hub/ws winner is out.
    assignment, winner_by_dev, anchors, dev_by_name = _soc0_like()
    winner_by_dev["api-webhooks"] = SpineAnchor(
        canonical_id="schema:webhook", key="webhook", source="schema",
        display="webhook")
    rebinds = _mint_domain_fold_rebinds(
        assignment, winner_by_dev, anchors, dev_by_name)
    assert "api-webhooks" not in {n for n, _c, _p in rebinds}


def test_api_only_page_named_route_is_not_a_surface() -> None:
    # A `<domain>-page` cid with NO page_route_files (api-only) is not a
    # genuine frontend surface → does not become a rebind target.
    assignment, winner_by_dev, anchors, dev_by_name = _soc0_like()
    anchors["route:webhooks-page"] = _route(
        "route:webhooks-page", "webhooks-page", pages=0, apis=2)
    rebinds = _mint_domain_fold_rebinds(
        assignment, winner_by_dev, anchors, dev_by_name)
    names = {n for n, _c, _p in rebinds}
    assert "api-webhooks" not in names
    assert "webhook-detail-page" not in names


def test_surface_not_yet_a_pf_is_not_a_target() -> None:
    # webhooks-page anchor exists but NO dev is assigned to it → it is not an
    # existing PF → the rail must not create it (never mint).
    assignment, winner_by_dev, anchors, dev_by_name = _soc0_like()
    del assignment["webhooks-page"]  # surface no longer an assignment target
    rebinds = _mint_domain_fold_rebinds(
        assignment, winner_by_dev, anchors, dev_by_name)
    names = {n for n, _c, _p in rebinds}
    assert "api-webhooks" not in names
    assert "webhook-detail-page" not in names
    # settings surface still exists → tool-settings still rebinds
    assert "tool-settings" in names


# ── kill-switch ──────────────────────────────────────────────────────────

def test_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MINT_DOMAIN_FOLD_ENV, raising=False)
    assert mint_domain_fold_enabled()


@pytest.mark.parametrize("val", ["0", "false", "FALSE", " 0 "])
def test_flag_off(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv(MINT_DOMAIN_FOLD_ENV, val)
    assert not mint_domain_fold_enabled()
