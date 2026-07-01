"""Tests for Phase 3 deterministic dual-evidence + confidence."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2.dual_evidence import attach_dual_evidence, match_anchors


def _anc(text, source="i18n"):
    return SimpleNamespace(text=text, source=source, locator="a.json#k")


def test_match_requires_distinctive_shared_token() -> None:
    anchors = [_anc("Investigation tabs", "nav"), _anc("AND"), _anc("All"),
               _anc("Access"), _anc("Ask me anything about your environment")]
    m = match_anchors("AI-Powered Security Investigations", "desc", anchors)
    texts = [a["text"] for a in m]
    assert "Investigation tabs" in texts       # distinctive "investigation" shared
    assert "AND" not in texts and "All" not in texts  # too short / generic
    assert "Access" not in texts               # no shared distinctive token
    assert "Ask me anything about your environment" not in texts  # sentence, > 6 words


def test_match_is_name_only_not_description() -> None:
    # description mentions "investigation" but the NAME is about chat → no match
    anchors = [_anc("Investigation tabs", "nav")]
    m = match_anchors("Chat Assistant", "lets analysts investigation the alerts", anchors)
    assert m == []


def test_match_stems_plurals() -> None:
    assert match_anchors("Security Cases Management", "", [_anc("Case Timeline")])
    assert match_anchors("Detector", "", [_anc("Built detectors", "nav")])


def test_confidence_tiers_by_source() -> None:
    def conf(anchors, name="Investigation Board", name_conf="high"):
        f = Feature(name=name, display_name=name, name_confidence=name_conf, paths=["a.ts"],
                    authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
                    last_modified=datetime.now(timezone.utc), health_score=90.0)
        attach_dual_evidence([f], [], anchors)
        return f.dual_evidence["confidence"]
    assert conf([]) == 0.5                                   # code-only
    assert conf([_anc("Investigation views", "i18n")]) == 0.7  # + i18n
    assert conf([_anc("Investigation tabs", "nav")]) == 0.9    # + nav
    assert conf([_anc("Investigation tabs", "nav")], name_conf="low") == 0.7  # low-name cap


def test_attach_sets_code_and_anchors_and_never_crashes() -> None:
    f = Feature(name="Detector Builder", display_name="Detector Builder", paths=["app/d1.ts", "app/d2.ts"],
                authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
                last_modified=datetime.now(timezone.utc), health_score=90.0)
    u = UserFlow(id="UF-001", name="Run detector", intent="execute", resource="detector",
                 member_flow_ids=[], member_count=0, routes=[])
    stats = attach_dual_evidence([f], [u], [_anc("Built detectors", "nav")])
    assert f.dual_evidence["code"] == ["app/d1.ts", "app/d2.ts"]
    assert f.dual_evidence["anchors"][0]["text"] == "Built detectors"
    assert f.dual_evidence["confidence"] == 0.9
    assert u.dual_evidence["anchors"][0]["text"] == "Built detectors"
    assert stats == {"pf": 1, "pf_corroborated": 1, "uf": 1, "uf_corroborated": 1}
    # empty / None inputs never raise
    assert attach_dual_evidence([], [], None) == {"pf": 0, "pf_corroborated": 0, "uf": 0, "uf_corroborated": 0}
