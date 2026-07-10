"""B27 — package-manifest PF display names (S1 dependency-manifest
grounding). The repo declares its own names: a package-dir-anchored PF
(``hub:``-vendor / ``ws:``) takes its display from the package's OWN
metadata (config.json name / metadata-module name / package.json
displayName / authored name), composing with the existing hub decoration
("App Store — Stripe"); a mechanical letter/digit word-split of the dir
slug is the rung strictly below. Display channel ONLY (B16 pattern);
``FAULTLINE_PF_MANIFEST_NAME=0`` restores pre-B27 byte-identically.

Fixtures mirror the cal.com wave14 exhibits verbatim (exchange2013calendar,
stripepayment, office365calendar, wipemycalother) plus the mandated
anti-cases: route-anchored PFs never fire; an npm scope-slug name
(@calcom/stripepayment) is NOT an authored display name; missing /
malformed manifests fall through, never crash; deterministic across runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature
from faultline.pipeline_v2 import manifest_display as md
from faultline.pipeline_v2.manifest_display import (
    manifest_display_name,
    package_dir_of_anchor,
    pf_manifest_name_enabled,
    word_split_slug,
)
from faultline.pipeline_v2.naming_contract import (
    build_pf_candidates,
    load_naming_vocab,
    run_naming_contract,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_manifest_cache():
    md._manifest_display_name_cached.cache_clear()
    yield
    md._manifest_display_name_cached.cache_clear()


def _pf(slug: str, display: str, anchor_id: str | None = None) -> Feature:
    f = Feature(
        name=slug, display_name=display, layer="product",
        paths=[], authors=["a"], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_NOW, health_score=100.0,
    )
    if anchor_id:
        f.anchor_id = anchor_id
    return f


def _mk_pkg(root: Path, rel: str, *, config_name: str | None = None,
            metadata_ts_name: str | None = None,
            pkg_json: dict | None = None) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    if config_name is not None:
        (d / "config.json").write_text(
            json.dumps({"name": config_name, "slug": rel.rsplit("/", 1)[-1]}))
    if metadata_ts_name is not None:
        (d / "_metadata.ts").write_text(
            'import type { AppMeta } from "@x/types";\n\n'
            "export const metadata = {\n"
            f'  name: "{metadata_ts_name}",\n'
            '  description: "d",\n'
            "} as AppMeta;\n"
        )
    if pkg_json is not None:
        (d / "package.json").write_text(json.dumps(pkg_json))


# ── flag + anchor-shape gates ───────────────────────────────────────────


def test_flag_default_on_and_kill_switch(monkeypatch) -> None:
    monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
    assert pf_manifest_name_enabled()
    monkeypatch.setenv(md.PF_MANIFEST_NAME_ENV, "0")
    assert not pf_manifest_name_enabled()


def test_flag_registered_in_env_output_flags() -> None:
    from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS
    assert "FAULTLINE_PF_MANIFEST_NAME" in ENV_OUTPUT_FLAGS


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        ("hub:packages/app-store/stripepayment", "packages/app-store/stripepayment"),
        ("ws:packages/app-store/exchangecalendar", "packages/app-store/exchangecalendar"),
        # multi-segment hub path — potential vendor dir (the id shape is
        # ambiguous; hub CORE protection is the " Core" display guard)
        ("hub:packages/app-store", "packages/app-store"),
        ("hub:apps", None),                         # 1-seg hub CORE — never
        ("route:apps/web/pages/api/stripe", None),  # route anchor — never
        ("fdir:apps/web/modules/videos", None),
        ("ws:", None),
        ("", None),
        ("hub:packages/../../etc/passwd", None),    # traversal disqualifies
    ],
)
def test_package_dir_of_anchor(anchor: str, expected: str | None) -> None:
    assert package_dir_of_anchor(anchor) == expected


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("exchange2013calendar", "exchange-2013-calendar"),
        ("office365video", "office-365-video"),
        ("huddle01video", "huddle-01-video"),
        ("stripepayment", None),   # no boundary — nothing mechanical to add
        ("app-store", None),
        ("", None),
    ],
)
def test_word_split_slug(slug: str, expected: str | None) -> None:
    assert word_split_slug(slug) == expected


# ── manifest rung order ─────────────────────────────────────────────────


def test_config_json_name_wins(tmp_path: Path) -> None:
    _mk_pkg(tmp_path, "packages/app-store/ga4", config_name="Google Analytics",
            pkg_json={"name": "@calcom/ga4", "displayName": "Ignored"})
    assert manifest_display_name(tmp_path, "packages/app-store/ga4") \
        == "Google Analytics"


def test_metadata_module_name_second(tmp_path: Path) -> None:
    _mk_pkg(tmp_path, "packages/app-store/stripepayment",
            metadata_ts_name="Stripe",
            pkg_json={"name": "@calcom/stripepayment"})
    assert manifest_display_name(
        tmp_path, "packages/app-store/stripepayment") == "Stripe"


def test_package_json_displayname_third(tmp_path: Path) -> None:
    _mk_pkg(tmp_path, "packages/tool",
            pkg_json={"name": "@acme/tool", "displayName": "Acme Toolbox"})
    assert manifest_display_name(tmp_path, "packages/tool") == "Acme Toolbox"


def test_npm_scope_slug_is_not_authored(tmp_path: Path) -> None:
    """@calcom/stripepayment == dir 'stripepayment' modulo case/hyphens —
    NOT an authored display name; nothing else declared -> None."""
    _mk_pkg(tmp_path, "packages/app-store/stripepayment",
            pkg_json={"name": "@calcom/stripepayment"})
    assert manifest_display_name(
        tmp_path, "packages/app-store/stripepayment") is None


def test_unscoped_authored_package_name_qualifies(tmp_path: Path) -> None:
    """cal.com wipemycalother: package.json name 'WipeMyCal' differs from
    the dir slug — the maintainer authored it."""
    _mk_pkg(tmp_path, "packages/app-store/wipemycalother",
            pkg_json={"name": "WipeMyCal"})
    assert manifest_display_name(
        tmp_path, "packages/app-store/wipemycalother") == "WipeMyCal"


def test_npm_path_slug_is_not_authored(tmp_path: Path) -> None:
    """cal.com platform packages: '@calcom/platform-enums' for dir
    packages/platform/enums is the PATH slug re-joined — not authored."""
    _mk_pkg(tmp_path, "packages/platform/enums",
            pkg_json={"name": "@calcom/platform-enums"})
    assert manifest_display_name(tmp_path, "packages/platform/enums") is None


def test_slug_cased_authored_name_titleizes(tmp_path: Path,
                                            monkeypatch) -> None:
    """A slug-cased authored name ('report-studio') is authored NAMING,
    not authored CASING — it must ship titleized, never lowercase."""
    monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
    d = tmp_path / "packages/reports"
    d.mkdir(parents=True)
    (d / "composer.json").write_text(json.dumps({"name": "acme/report-studio"}))
    vocab = load_naming_vocab()
    pf = _pf("reports", "Reports", "ws:packages/reports")
    cands = build_pf_candidates(pf, vocab, repo_root=tmp_path)
    assert cands[0] == "Report Studio"
    assert "report-studio" not in cands


def test_composer_and_pyproject_rungs(tmp_path: Path) -> None:
    d = tmp_path / "packages/reports"
    d.mkdir(parents=True)
    (d / "composer.json").write_text(json.dumps({"name": "acme/report-studio"}))
    assert manifest_display_name(tmp_path, "packages/reports") == "report-studio"
    d2 = tmp_path / "packages/ingest"
    d2.mkdir(parents=True)
    (d2 / "pyproject.toml").write_text(
        '[project]\nname = "acme-ingest-worker"\n')
    assert manifest_display_name(tmp_path, "packages/ingest") \
        == "acme-ingest-worker"
    # pyproject name equal to the dir slug is NOT authored
    d3 = tmp_path / "packages/worker"
    d3.mkdir(parents=True)
    (d3 / "pyproject.toml").write_text('[project]\nname = "worker"\n')
    assert manifest_display_name(tmp_path, "packages/worker") is None


def test_missing_or_malformed_manifests_never_crash(tmp_path: Path) -> None:
    # missing dir
    assert manifest_display_name(tmp_path, "packages/ghost") is None
    # malformed config.json falls through to package.json displayName
    _mk_pkg(tmp_path, "packages/broken",
            pkg_json={"displayName": "Broken But Named"})
    (tmp_path / "packages/broken/config.json").write_text("{not json!!")
    assert manifest_display_name(tmp_path, "packages/broken") \
        == "Broken But Named"
    # binary blob at a manifest path is skipped
    _mk_pkg(tmp_path, "packages/binary")
    (tmp_path / "packages/binary/config.json").write_bytes(b"\xff\xfe\x00\x01")
    assert manifest_display_name(tmp_path, "packages/binary") is None
    # non-string / whitespace / letterless names are unusable
    _mk_pkg(tmp_path, "packages/weird", config_name="   ")
    assert manifest_display_name(tmp_path, "packages/weird") is None
    _mk_pkg(tmp_path, "packages/nums", config_name="2013")
    assert manifest_display_name(tmp_path, "packages/nums") is None


def test_deterministic_across_calls(tmp_path: Path) -> None:
    _mk_pkg(tmp_path, "packages/app-store/x2go", config_name="X2Go Connect")
    first = manifest_display_name(tmp_path, "packages/app-store/x2go")
    md._manifest_display_name_cached.cache_clear()
    second = manifest_display_name(tmp_path, "packages/app-store/x2go")
    assert first == second == "X2Go Connect"


# ── candidate channel (display only) ────────────────────────────────────


def test_hub_candidates_lead_with_manifest_composition(tmp_path: Path,
                                                       monkeypatch) -> None:
    monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
    _mk_pkg(tmp_path, "packages/app-store/stripepayment",
            metadata_ts_name="Stripe",
            pkg_json={"name": "@calcom/stripepayment"})
    vocab = load_naming_vocab()
    pf = _pf("stripepayment", "Stripepayment",
             "hub:packages/app-store/stripepayment")
    cands = build_pf_candidates(pf, vocab, repo_root=tmp_path)
    assert cands[0] == "App Store — Stripe"
    # the pre-B27 composition stays available as the next rung
    assert "App Store — Stripepayment" in cands


def test_wordsplit_is_strictly_below_manifest(tmp_path: Path,
                                              monkeypatch) -> None:
    monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
    # no manifest name anywhere -> the mechanical letter/digit split leads
    _mk_pkg(tmp_path, "packages/app-store/exchange2013calendar",
            pkg_json={"name": "@calcom/exchange2013calendar"})
    vocab = load_naming_vocab()
    pf = _pf("exchange2013calendar", "Exchange2013calendar",
             "hub:packages/app-store/exchange2013calendar")
    cands = build_pf_candidates(pf, vocab, repo_root=tmp_path)
    assert cands[0] == "App Store — Exchange 2013 Calendar"
    # with a manifest name present the split never leads
    _mk_pkg(tmp_path, "packages/app-store/exchange2016calendar",
            metadata_ts_name="Microsoft Exchange 2016 Calendar",
            pkg_json={"name": "@calcom/exchange2016calendar"})
    pf2 = _pf("exchange2016calendar", "Exchange2016calendar",
              "hub:packages/app-store/exchange2016calendar")
    cands2 = build_pf_candidates(pf2, vocab, repo_root=tmp_path)
    assert cands2[0] == "App Store — Microsoft Exchange 2016 Calendar"


def test_ws_anchor_gets_bare_manifest_name(tmp_path: Path,
                                           monkeypatch) -> None:
    monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
    _mk_pkg(tmp_path, "packages/app-store/exchangecalendar",
            config_name="Microsoft Exchange")
    vocab = load_naming_vocab()
    pf = _pf("exchangecalendar", "Exchangecalendar",
             "ws:packages/app-store/exchangecalendar")
    cands = build_pf_candidates(pf, vocab, repo_root=tmp_path)
    assert cands[0] == "Microsoft Exchange"
    assert "Exchangecalendar" in cands  # never-worse: current still present


def test_hub_core_display_never_takes_manifest(tmp_path: Path,
                                               monkeypatch) -> None:
    """A hub CORE (designed '<Family> Core' display) keeps its name even
    when the family dir itself declares a manifest name."""
    monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
    _mk_pkg(tmp_path, "backend/services/edr", config_name="Endpoint Suite")
    vocab = load_naming_vocab()
    pf = _pf("edr-core", "EDR Core", "hub:backend/services/edr")
    assert build_pf_candidates(pf, vocab, repo_root=tmp_path) \
        == build_pf_candidates(pf, vocab)


def test_route_anchor_is_untouched_anti_case(tmp_path: Path,
                                             monkeypatch) -> None:
    """A route-anchored product PF keeps its route-derived name even when
    a same-named package dir with a manifest exists in the repo."""
    monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
    _mk_pkg(tmp_path, "apps/web/pages/api/stripe", config_name="Not Used")
    vocab = load_naming_vocab()
    pf = _pf("stripe", "Stripe", "route:apps/web/pages/api/stripe")
    assert build_pf_candidates(pf, vocab, repo_root=tmp_path) \
        == build_pf_candidates(pf, vocab)


def test_manifest_name_equal_to_dir_slug_gains_only_wordsplit(
        tmp_path: Path, monkeypatch) -> None:
    """Anti-case: '@calcom/huddle01video' is the dir slug again — the
    authored test rejects it and the PF gains only the word-split rung."""
    monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
    _mk_pkg(tmp_path, "packages/app-store/huddle01video",
            pkg_json={"name": "@calcom/huddle01video"})
    vocab = load_naming_vocab()
    pf = _pf("huddle01video", "Huddle01video",
             "hub:packages/app-store/huddle01video")
    cands = build_pf_candidates(pf, vocab, repo_root=tmp_path)
    assert cands[0] == "App Store — Huddle 01 Video"  # mechanical split only


def test_flag_off_candidates_byte_identical(tmp_path: Path,
                                            monkeypatch) -> None:
    _mk_pkg(tmp_path, "packages/app-store/stripepayment",
            metadata_ts_name="Stripe")
    vocab = load_naming_vocab()
    pf = _pf("stripepayment", "Stripepayment",
             "hub:packages/app-store/stripepayment")
    monkeypatch.setenv(md.PF_MANIFEST_NAME_ENV, "0")
    assert build_pf_candidates(pf, vocab, repo_root=tmp_path) \
        == build_pf_candidates(pf, vocab)


# ── driver (display channel only; identity untouched) ───────────────────


def test_driver_renames_display_only_and_counts_telemetry(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
    _mk_pkg(tmp_path, "packages/app-store/stripepayment",
            metadata_ts_name="Stripe",
            pkg_json={"name": "@calcom/stripepayment"})
    _mk_pkg(tmp_path, "packages/app-store/exchange2013calendar",
            pkg_json={"name": "@calcom/exchange2013calendar"})
    pf1 = _pf("stripepayment", "Stripepayment",
              "hub:packages/app-store/stripepayment")
    pf2 = _pf("exchange2013calendar", "Exchange2013calendar",
              "hub:packages/app-store/exchange2013calendar")
    pf3 = _pf("policies", "Policies", "route:app/policies")
    tele = run_naming_contract([pf1, pf2, pf3], [], repo_root=tmp_path)
    assert pf1.display_name == "App Store — Stripe"
    assert pf2.display_name == "App Store — Exchange 2013 Calendar"
    assert pf3.display_name == "Policies"
    assert tele["pf_manifest_named"] == 1
    assert tele["pf_wordsplit_named"] == 1
    # identity untouched — slugs and anchors never move
    assert (pf1.name, pf1.anchor_id) == (
        "stripepayment", "hub:packages/app-store/stripepayment")
    assert pf2.name == "exchange2013calendar"


def test_driver_flag_off_and_no_root_byte_identical(tmp_path: Path,
                                                    monkeypatch) -> None:
    _mk_pkg(tmp_path, "packages/app-store/stripepayment",
            metadata_ts_name="Stripe")

    def _run(root, flag):
        if flag is None:
            monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
        else:
            monkeypatch.setenv(md.PF_MANIFEST_NAME_ENV, flag)
        pf = _pf("stripepayment", "Stripepayment",
                 "hub:packages/app-store/stripepayment")
        tele = run_naming_contract([pf], [], repo_root=root)
        return pf.display_name, tele

    disp_off, tele_off = _run(tmp_path, "0")       # kill-switch
    disp_none, tele_none = _run(None, None)        # no repo root (pre-B27 callers)
    disp_on, tele_on = _run(tmp_path, None)        # armed
    assert disp_off == disp_none == "App Store — Stripepayment"
    assert disp_on == "App Store — Stripe"
    assert "pf_manifest_named" not in tele_off     # telemetry byte-parity
    assert "pf_manifest_named" not in tele_none
    assert tele_off == tele_none
    assert tele_on["pf_manifest_named"] == 1


def test_driver_fallback_qualifier_composes_with_manifest(
        tmp_path: Path, monkeypatch) -> None:
    """Chosen-is-None fallback: every candidate dirty/colliding — the
    '(Qualifier)' decoration composes with the manifest word
    ('Stripe (App Store)', not 'Stripepay2go (App Store)')."""
    monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
    _mk_pkg(tmp_path, "packages/app-store/stripepay2go",
            config_name="Stripe")
    taken = _pf("stripe", "Stripe", "route:apps/web/pages/api/stripe")
    # current display is single-letter (law-dirty) so candidates exhaust:
    # bare manifest 'Stripe' collides with the route PF's display.
    pf = _pf("stripepay2go", "S", "ws:packages/app-store/stripepay2go")
    run_naming_contract([taken, pf], [], repo_root=tmp_path)
    assert taken.display_name == "Stripe"
    assert pf.display_name == "Stripe (App Store)"


def test_driver_deterministic_across_two_runs(tmp_path: Path,
                                              monkeypatch) -> None:
    monkeypatch.delenv(md.PF_MANIFEST_NAME_ENV, raising=False)
    _mk_pkg(tmp_path, "packages/app-store/dailyvideo",
            config_name="Cal Video")

    def _once() -> list[str]:
        md._manifest_display_name_cached.cache_clear()
        pfs = [
            _pf("dailyvideo", "Dailyvideo",
                "hub:packages/app-store/dailyvideo"),
            _pf("office365video", "Office365video",
                "hub:packages/app-store/office365video"),
        ]
        run_naming_contract(pfs, [], repo_root=tmp_path)
        return [p.display_name for p in pfs]

    assert _once() == _once() == [
        "App Store — Cal Video",
        "App Store — Office 365 Video",   # no manifest -> word-split rung
    ]
