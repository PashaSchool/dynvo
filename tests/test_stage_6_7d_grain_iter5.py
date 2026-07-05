"""Iteration-5 grain surgery — Stage 6.7d join-over-mint, flowful requirement,
and the container-page guard.

Cases (Soc0 2026-07-05 board audit):
  A  Detection Studio = thin duplicate shell: a flowless ``detection-studio``
     dev minted its own PF while the SAME product area lived in "Custom
     Detector Builder". Fix 1 (family join) folds it in; fix 2 (flowful
     requirement) forbids the mint outright.
  B  Home Page = container minted as a PF: a ``home-page`` dev (landing route
     + hosted flows) minted "Home Page". Fix 3 guards the mint and
     redistributes its hosted flows to the features that own them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, Flow, FlowLineRange
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    _RESIDUAL_CAP,
    _build_product_features,
    _family_capability_match,
    _family_stems,
    _is_container_page,
    _stem,
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _flow(uuid: str, entry: str | None = None, paths: list[str] | None = None,
          loc: int = 10) -> Flow:
    p = paths or ([entry] if entry else [f"src/{uuid}.py"])
    return Flow(
        name=f"{uuid}-flow", uuid=uuid, entry_point_file=entry or p[0],
        paths=p, authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0,
        line_ranges=[FlowLineRange(path=p[0], start_line=1, end_line=loc)],
    )


def _dev(name: str, paths: list[str], flows: list[Flow] | None = None,
         display: str | None = None) -> Feature:
    return Feature(
        name=name, display_name=display or name, paths=paths,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0, layer="developer",
        flows=flows or [],
    )


# ── stemming / family stems ─────────────────────────────────────────────────


def test_stem_folds_derivations():
    assert _stem("detection") == _stem("detector") == _stem("detect")
    assert _stem("suggestions") == _stem("suggestion") == _stem("suggest")
    assert _stem("recommendations") == "recommend"
    # idempotent on suffix-free stems + safe on already-singular sibilants
    assert _stem("studio") == "studio"
    assert _stem("status") == "status"


def test_family_stems_drop_generic_and_vendor():
    # "page"/"home" are container-surface tokens, never family drivers
    assert _family_stems("Home Page") == set()
    assert "detect" in _family_stems("Detection Studio")
    assert "detect" in _family_stems("Custom Detector Builder")


# ── fix 1: join over mint (family match + gravitational-mass tie-break) ──────


def test_family_match_prefers_largest_established_home():
    """detection-studio shares the 'detect' family with two capabilities; it
    joins the LARGER established home (Custom Detector Builder), never the
    narrower Detections Page — structural, no repo list."""
    dev = _dev("detection-studio", ["modules/detection-studio/a.ts"])
    ctx = {
        "Custom Detector Builder": {"stems": {"custom", "detect", "build"},
                                    "members": 4, "flows": 63, "paths": 103},
        "Detections Page": {"stems": {"detect"},
                            "members": 3, "flows": 8, "paths": 73},
    }
    assert _family_capability_match(dev, ctx) == "Custom Detector Builder"


def test_family_match_skips_own_self_capability():
    dev = _dev("detection-studio", ["modules/detection-studio/a.ts"])
    ctx = {"Detection Studio": {"stems": {"detect"}, "members": 1,
                                "flows": 0, "paths": 1}}
    assert _family_capability_match(dev, ctx) is None


def test_family_match_none_without_shared_stem():
    dev = _dev("billing", ["src/billing/a.ts"])
    ctx = {"Custom Detector Builder": {"stems": {"custom", "detect", "build"},
                                       "members": 4, "flows": 63, "paths": 103}}
    assert _family_capability_match(dev, ctx) is None


def test_detection_studio_joins_not_mints():
    """End-to-end: a flowless detection-studio dish sent to the residual JOINS
    Custom Detector Builder instead of minting a "Detection Studio" PF."""
    devs = [
        _dev("api-detectors", ["backend/routers/detectors.py"],
             [_flow("d1"), _flow("d2")]),
        _dev("detectors-page", ["frontend/pages/DetectorsPage.tsx"],
             [_flow("d3")]),
        _dev("detection-studio", ["modules/detection-studio/a.ts",
                                  "modules/detection-studio/b.ts"]),  # flowless
    ]
    dev_map = {
        "api-detectors": "Custom Detector Builder",
        "detectors-page": "Custom Detector Builder",
        "detection-studio": _RESIDUAL_CAP,  # LLM gave up → residual guard runs
    }
    pf_specs = [{"name": "Custom Detector Builder", "description": "x"}]
    pfs, d2p, _files, tele, _ovr = _build_product_features(
        pf_specs, dev_map, devs)
    names = {p.display_name for p in pfs}
    assert "Detection Studio" not in names
    assert d2p["detection-studio"] == ("custom-detector-builder",)
    assert tele.get("devs_residual_family_joined", 0) >= 1


def test_flowless_dev_never_mints_without_family(monkeypatch):
    """Fix 2: a flowless residual dev with no family match stays in the
    residual — it never mints a standalone PF."""
    devs = [_dev("anomalies", ["modules/anomalies/a.ts",
                               "modules/anomalies/b.ts"])]  # flowless
    dev_map = {"anomalies": _RESIDUAL_CAP}
    pfs, d2p, _f, _t, _o = _build_product_features([], dev_map, devs)
    assert d2p["anomalies"] == ("shared-platform",)
    assert "Anomalies" not in {p.display_name for p in pfs}


def test_flowful_feature_dir_dev_still_mints():
    """Fix 2 does not touch a FLOWFUL feature-dir dev — real granularity is
    preserved (rule-engineering-granularity-is-correct)."""
    devs = [_dev("network-security",
                 ["frontend/src/features/network-security/a.ts",
                  "frontend/src/features/network-security/b.ts"],
                 [_flow("n1", "frontend/src/features/network-security/a.ts")])]
    dev_map = {"network-security": _RESIDUAL_CAP}
    pfs, d2p, _f, _t, _o = _build_product_features([], dev_map, devs)
    assert d2p["network-security"] == ("network-security",)
    assert "Network Security" in {p.display_name for p in pfs}


# ── fix 3: container-page guard + hosted-flow redistribution ─────────────────


def test_is_container_page():
    assert _is_container_page(_dev("home-page", ["p.tsx"]))
    assert _is_container_page(_dev("landing", ["p.tsx"]))
    assert _is_container_page(_dev("index-page", ["p.tsx"]))
    assert not _is_container_page(_dev("detections-page", ["p.tsx"]))
    assert not _is_container_page(_dev("home-lab", ["p.tsx"]))


def test_container_page_guard_redistributes_flows():
    """A home-page container never mints; its hosted flow's ownership follows
    the non-container dev owning the flow's directory (inline-suggestions)."""
    hosted = _flow(
        "h1", entry="frontend/src/features/inline-suggestions/ghost.ts",
        paths=["frontend/src/features/inline-suggestions/ghost.ts"])
    devs = [
        _dev("inline-suggestions",
             ["frontend/src/features/inline-suggestions/hook.ts"],
             [_flow("i1", "frontend/src/features/inline-suggestions/hook.ts")]),
        _dev("home-page",
             ["frontend/src/pages/HomePage.tsx",
              "frontend/src/features/inline-suggestions/ghost.ts"],
             [hosted]),
    ]
    dev_map = {
        "inline-suggestions": "Inline Suggestions",
        "home-page": "Home Page",  # LLM emitted a container cap
    }
    pf_specs = [{"name": "Inline Suggestions", "description": "x"}]
    pfs, d2p, _f, tele, override = _build_product_features(
        pf_specs, dev_map, devs)
    assert "Home Page" not in {p.display_name for p in pfs}
    assert d2p["home-page"] == ("shared-platform",)
    # hosted flow redistributed to inline-suggestions' capability
    assert override.get("h1") == "inline-suggestions"
    assert tele.get("container_pages_guarded") == 1


def test_container_guard_kill_switch(monkeypatch):
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_CONTAINER_GUARD", "0")
    devs = [_dev("home-page", ["p.tsx"], [_flow("h1", "p.tsx")])]
    dev_map = {"home-page": "Home Page"}
    pfs, _d, _f, _t, _o = _build_product_features(
        [{"name": "Home Page"}], dev_map, devs)
    assert "Home Page" in {p.display_name for p in pfs}
