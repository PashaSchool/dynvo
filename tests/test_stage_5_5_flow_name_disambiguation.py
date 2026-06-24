"""Tests for Stage 5.5 — deterministic flow-name disambiguation (Step 0.6).

Stage 3 names every flow ``<kebab-verb-phrase>-flow`` with NO cross-flow
uniqueness, so genuinely-distinct flows (different entry point / owning
feature) routinely share a generic name and dominate ``dup_flow_rate``. The
byte-identical collapses (Steps 0 / 0.5) correctly leave these alone — they
are NOT the same flow. Step 0.6 RENAMES (never merges) every genuine collision
by inserting the flow's distinguishing context BEFORE the ``-flow`` suffix.

These tests pin the behavioural contract called out in the sprint:
  * collision → context-disambiguated AND rule-valid (kebab, verb-start, -flow)
  * a unique name is byte-untouched
  * same-feature-same-name → ordinal fallback, still rule-valid + unique
  * a context insertion that would re-collide with a pre-existing name resolves
  * the verb-led head (first token) is never changed
  * the pass is idempotent
  * the entry-point domain is used when no primary feature is attributed
  * scale-invariant: tiny / medium / large synthetic inputs all behave

Pure unit tests — no LLM, no git, no filesystem. Per ``rule-no-repo-specific
-paths`` the fixtures use neutral synthetic names, not slices from real repos.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from faultline.models.types import Feature, Flow
from faultline.pipeline_v2.stage_5_5_bipartite import (
    _disambiguate_colliding_flow_names,
    stage_5_5_bipartite,
)

# The structural flow-name validator from Stage 3 (kebab, ends in -flow).
_KEBAB_FLOW = re.compile(r"^[a-z0-9][a-z0-9-]*-flow$")


# ── Helpers ──────────────────────────────────────────────────────────────


def _flow(
    name: str,
    *,
    primary: str | None = None,
    entry_file: str | None = None,
    entry_line: int | None = None,
    paths: list[str] | None = None,
) -> Flow:
    fl = Flow(
        name=name,
        paths=paths or ([entry_file] if entry_file else []),
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
    )
    fl.primary_feature = primary
    fl.entry_point_file = entry_file
    fl.entry_point_line = entry_line
    return fl


def _feat(name: str, flows: list[Flow]) -> Feature:
    return Feature(
        name=name,
        paths=[],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=flows,
        layer="developer",
    )


def _all_names(features: list[Feature]) -> list[str]:
    return [fl.name for f in features for fl in f.flows]


def _assert_all_valid(names: list[str]) -> None:
    for n in names:
        assert _KEBAB_FLOW.match(n), f"{n!r} is not a valid kebab -flow name"


def _assert_unique(names: list[str]) -> None:
    keys = [n.strip().lower() for n in names]
    assert len(set(keys)) == len(keys), f"duplicate names remain: {names}"


# ── Case 1 — genuine collision → context-disambiguated + rule-valid ───────


def test_collision_disambiguated_by_primary_feature_and_valid():
    """Three distinct flows sharing ``search-cases-flow`` but owned by
    different features each get their primary feature inserted before
    ``-flow``; all names become unique, valid, and verb-led."""
    feats = [
        _feat("a", [_flow("search-cases-flow", primary="api-cases", entry_line=1)]),
        _feat("b", [_flow("search-cases-flow", primary="cases-list", entry_line=2)]),
        _feat("c", [_flow("search-cases-flow", primary="quick-search", entry_line=3)]),
    ]

    tel = _disambiguate_colliding_flow_names(feats)
    names = _all_names(feats)

    assert tel["naming_collision_groups"] == 1
    assert tel["flows_disambiguated_by_context"] == 3
    assert tel["flows_disambiguated_by_ordinal"] == 0
    _assert_all_valid(names)
    _assert_unique(names)
    # verb-led head preserved.
    for n in names:
        assert n.startswith("search-"), n
    # context is actually carried (meaningful, not just an ordinal).
    assert any("list" in n for n in names)
    assert any("quick" in n or "search" in n for n in names)


# ── Case 2 — a unique name is byte-untouched ──────────────────────────────


def test_unique_name_is_untouched():
    """A flow whose name is unique across the scan is not modified at all."""
    feats = [
        _feat("a", [_flow("export-report-flow", primary="reporting")]),
        _feat("b", [_flow("invite-member-flow", primary="team")]),
    ]

    tel = _disambiguate_colliding_flow_names(feats)
    names = _all_names(feats)

    assert tel["naming_collision_groups"] == 0
    assert tel["flows_disambiguated_by_context"] == 0
    assert tel["flows_disambiguated_by_ordinal"] == 0
    assert sorted(names) == ["export-report-flow", "invite-member-flow"]


# ── Case 3 — same feature + same name → ordinal fallback ──────────────────


def test_same_feature_same_name_falls_back_to_ordinal():
    """Two near-identical flows under the SAME feature with the SAME name
    cannot be separated by context, so the later one (by entry line) gets a
    minimal stable ordinal — still valid and unique."""
    feats = [
        _feat(
            "billing",
            [
                _flow("charge-card-flow", primary="billing", entry_line=10),
                _flow("charge-card-flow", primary="billing", entry_line=20),
            ],
        ),
    ]

    tel = _disambiguate_colliding_flow_names(feats)
    names = sorted(_all_names(feats))

    # Context (the shared primary feature) is inserted first — it cannot
    # separate same-feature flows — so the ordinal tier then makes them unique.
    assert tel["flows_disambiguated_by_ordinal"] == 1
    _assert_all_valid(names)
    _assert_unique(names)
    # The lower-entry-line flow keeps the context form; the other gets -2.
    assert names == ["charge-card-billing-2-flow", "charge-card-billing-flow"]


def test_ordinal_uses_entry_domain_when_no_primary():
    """When neither flow has a primary feature, the entry-point domain
    (parent directory) supplies context; identical domains then fall to the
    ordinal tier. All names end up valid + unique."""
    feats = [
        _feat("x", [_flow("send-message-flow", entry_file="backend/main.py", entry_line=5)]),
        _feat("y", [_flow("send-message-flow", entry_file="backend/db.py", entry_line=9)]),
        _feat("z", [_flow("send-message-flow", entry_file="frontend/Chat.tsx", entry_line=3)]),
    ]

    _disambiguate_colliding_flow_names(feats)
    names = _all_names(feats)

    _assert_all_valid(names)
    _assert_unique(names)
    # frontend flow is separated by domain; the two backend flows differ by ordinal.
    assert any("frontend" in n for n in names)
    assert sum(1 for n in names if "backend" in n) == 2


# ── Case 4 — disambiguation that would re-collide is resolved ─────────────


def test_context_collision_with_preexisting_name_is_resolved():
    """If inserting context yields a name that already exists elsewhere, the
    ordinal tier still guarantees global uniqueness."""
    feats = [
        # Pre-existing unique flow whose name equals the would-be disambiguation.
        _feat("a", [_flow("open-ticket-support-flow", primary="other")]),
        _feat("b", [_flow("open-ticket-flow", primary="support", entry_line=1)]),
        _feat("c", [_flow("open-ticket-flow", primary="support", entry_line=2)]),
    ]

    _disambiguate_colliding_flow_names(feats)
    names = _all_names(feats)

    _assert_all_valid(names)
    _assert_unique(names)
    assert "open-ticket-support-flow" in names  # the pre-existing one survives


# ── Case 5 — verb-start preserved on every rename ─────────────────────────


def test_verb_start_preserved_for_every_rename():
    feats = [
        _feat("a", [_flow("create-invoice-flow", primary="billing")]),
        _feat("b", [_flow("create-invoice-flow", primary="orders")]),
        _feat("c", [_flow("delete-invoice-flow", primary="billing")]),
        _feat("d", [_flow("delete-invoice-flow", primary="orders")]),
    ]

    _disambiguate_colliding_flow_names(feats)

    for f in feats:
        for fl in f.flows:
            head = fl.name.split("-")[0]
            assert head in {"create", "delete"}, fl.name
    _assert_unique(_all_names(feats))


# ── Case 6 — idempotence ──────────────────────────────────────────────────


def test_disambiguation_is_idempotent():
    feats = [
        _feat("a", [_flow("view-order-flow", primary="orders", entry_line=1)]),
        _feat("b", [_flow("view-order-flow", primary="admin", entry_line=2)]),
        _feat("c", [_flow("view-order-flow", primary="orders", entry_line=3)]),
    ]

    _disambiguate_colliding_flow_names(feats)
    first = _all_names(feats)
    tel2 = _disambiguate_colliding_flow_names(feats)
    second = _all_names(feats)

    assert first == second
    assert tel2["flows_disambiguated_by_context"] == 0
    assert tel2["flows_disambiguated_by_ordinal"] == 0


# ── Case 7 — scale invariance (no magic numbers) ──────────────────────────


def test_scale_invariant_tiny_medium_large():
    """The collision rule is structural (>1 distinct flow with a name), so it
    behaves identically regardless of how many flows collide."""
    for n in (2, 12, 120):
        feats = [
            _feat(f"feat-{i}", [_flow("process-job-flow", primary=f"queue-{i}", entry_line=i)])
            for i in range(n)
        ]
        tel = _disambiguate_colliding_flow_names(feats)
        names = _all_names(feats)
        assert tel["naming_collision_groups"] == 1
        assert len(names) == n  # count unchanged — nothing merged
        _assert_all_valid(names)
        _assert_unique(names)


# ── Case 8 — count unchanged, nothing merged/lost ─────────────────────────


def test_flow_count_unchanged_nothing_merged():
    flows_a = [_flow("sync-data-flow", primary="a", entry_line=i) for i in range(3)]
    flows_b = [_flow("sync-data-flow", primary="b", entry_line=i) for i in range(2)]
    feats = [_feat("a", flows_a), _feat("b", flows_b)]
    before = sum(len(f.flows) for f in feats)

    _disambiguate_colliding_flow_names(feats)
    after = sum(len(f.flows) for f in feats)

    assert before == after == 5
    _assert_unique(_all_names(feats))


# ── Case 9 — end-to-end through the public stage entry point ──────────────


def test_stage_5_5_emits_unique_flow_names_in_projection():
    """The full Stage 5.5 entry point disambiguates before id stamping, so the
    top-level ``flows[]`` projection carries unique names and ids, and the
    telemetry surfaces the new counters."""
    feats = [
        _feat(
            "api-cases",
            [_flow("search-cases-flow", primary=None, entry_file="api/cases.py", entry_line=1, paths=["api/cases.py"])],
        ),
        _feat(
            "cases-page",
            [_flow("search-cases-flow", primary=None, entry_file="ui/cases.tsx", entry_line=2, paths=["ui/cases.tsx"])],
        ),
    ]

    result = stage_5_5_bipartite(feats)

    proj_names = [f.name for f in result.flows]
    _assert_all_valid(proj_names)
    _assert_unique(proj_names)
    # id is minted from the disambiguated name → ids unique too, and each id's
    # slug half equals the slugified disambiguated name (id derives from name).
    ids = [f.id for f in result.flows]
    assert len(set(ids)) == len(ids)
    for f in result.flows:
        assert f.id is not None
        assert f.id.split("::", 1)[1] == f.name  # name is already a clean slug
    assert result.telemetry["naming_collision_groups"] == 1
    assert result.telemetry["flows_disambiguated_by_context"] == 2
    # count preserved: 2 features × 1 flow each.
    assert len(result.flows) == 2
