"""B78-it2 Goal 1 — orphan-flow gap stamp (Seg B rider, Stage 6.995).

Ruling (operator, 2026-07-22): the org-members regression VERDICT waits
for the confound-free keyed A/B; the deterministic MINIMUM ships now —
at the 6.995 conservation checkpoint, orphaned flows with page/product
evidence become a labeled ``coverage_gaps[]`` claim
(``kind="orphan_flow"``), never silence.

Pins (the org-members 3-flow shape + anti-cases):
  1. Exhibit — the Soc0 dissolution shape: 3 UF-less flows
     (manage-organization-members / process-member-requests /
     review-pending-invitations) with UI-component entries under
     ``features/organization-members/`` → ONE gap row with 3 surface
     files, honest loc, deterministic content-derived id.
  2. Anti-case claimed — flows a journey claims (member_flow_ids OR a
     live ``user_flow_id`` backpointer) never stamp.
  3. Anti-case plumbing — backend (non-UI-entry) orphans never stamp.
  4. Anti-case single-entry — one stray component is not journey
     material (R2 floor).
  5. Anti-case no-own-resource — a cohort whose flow names share no
     token with the feature-dir never stamps.
  6. B45 span law — a qualifying cohort with no span evidence is
     skipped and counted, never emitted span-less.
  7. PF ref — resolved only to an emitted PF key, else ``None``.
  8. Kill-switch — flag unset/=0 ⇒ ``([], tele)`` (byte-identity path).
  9. Determinism — two armed runs identical.
"""
from __future__ import annotations

import pytest

from faultline.pipeline_v2.conservation import (
    HOME_AFFINITY_GATE_ENV,
    build_orphan_flow_gaps,
)


# ── stubs (attribute-shaped; the builder reads via getattr only) ─────────


class LR:
    def __init__(self, path: str, start_line: int, end_line: int) -> None:
        self.path = path
        self.start_line = start_line
        self.end_line = end_line


class FlowStub:
    def __init__(self, name: str, entry: str, *, uuid: str | None = None,
                 user_flow_id: str | None = None,
                 line_ranges: list[LR] | None = None) -> None:
        self.name = name
        self.uuid = uuid or f"uuid-{name}"
        self.user_flow_id = user_flow_id
        self.entry_point_file = entry
        self.line_ranges = list(line_ranges or [])


class DevStub:
    def __init__(self, name: str, flows: list[FlowStub]) -> None:
        self.name = name
        self.flows = list(flows)


class UFStub:
    def __init__(self, uid: str, members: list[str]) -> None:
        self.id = uid
        self.member_flow_ids = list(members)


class PFStub:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = name


_DIR = "frontend/src/features/organization-members"


def _org_members_flows() -> list[FlowStub]:
    """The Soc0 exhibit shape: 3 orphaned UI flows, 2 with own-resource
    names (dir-token overlap) + 1 riding the cohort by directory."""
    return [
        FlowStub("manage-organization-members-flow",
                 f"{_DIR}/MembersTab.tsx",
                 line_ranges=[LR(f"{_DIR}/MembersTab.tsx", 10, 120)]),
        FlowStub("process-member-requests-flow",
                 f"{_DIR}/components/RequestsSubTab.tsx",
                 line_ranges=[
                     LR(f"{_DIR}/components/RequestsSubTab.tsx", 5, 80)]),
        FlowStub("review-pending-invitations-flow",
                 f"{_DIR}/components/InvitationsSubTab.tsx",
                 line_ranges=[
                     LR(f"{_DIR}/components/InvitationsSubTab.tsx", 8, 95)]),
    ]


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HOME_AFFINITY_GATE_ENV, "1")


# ── 8. kill-switch ───────────────────────────────────────────────────────


@pytest.mark.parametrize("off", [None, "0", "false"])
def test_flag_off_inert(monkeypatch, off) -> None:
    if off is None:
        monkeypatch.delenv(HOME_AFFINITY_GATE_ENV, raising=False)
    else:
        monkeypatch.setenv(HOME_AFFINITY_GATE_ENV, off)
    rows, tele = build_orphan_flow_gaps(
        [], [DevStub("soc0-frontend", _org_members_flows())], [])
    assert rows == []
    assert tele["enabled"] is False


# ── 1. exhibit: the org-members 3-flow shape stamps ONE cohort gap ───────


def test_org_members_three_flow_shape_stamps(gate_on) -> None:
    devs = [DevStub("soc0-frontend", _org_members_flows())]
    rows, tele = build_orphan_flow_gaps([], devs, [])
    assert len(rows) == 1
    gap = rows[0]
    assert gap.kind == "orphan_flow"
    assert gap.label == "Orphaned journey material: organization-members"
    assert gap.product_feature_id is None  # no such PF on this board
    assert gap.synthesis_reason == "orphan_flow_gap"
    assert len(gap.surface_files) == 3     # all 3 entry components ride in
    assert gap.loc == (120 - 10 + 1) + (80 - 5 + 1) + (95 - 8 + 1)
    # content-derived id — rescan-stable, no randomness
    assert gap.id.startswith("GAP-") and len(gap.id) == 14
    assert tele["cohorts"] == 1
    assert tele["flows"] == 3
    assert tele["rows"][0]["dir"] == "organization-members"


# ── 2. anti-case: journey-claimed flows never stamp ──────────────────────


def test_claimed_flows_never_stamp(gate_on) -> None:
    flows = _org_members_flows()
    # (a) claimed via a journey's member_flow_ids (uuid channel)
    ufs = [UFStub("UF-073", [f.uuid for f in flows])]
    rows, tele = build_orphan_flow_gaps(
        ufs, [DevStub("soc0-frontend", flows)], [])
    assert rows == [] and tele["cohorts"] == 0
    # (b) claimed via a live user_flow_id backpointer
    flows2 = _org_members_flows()
    for f in flows2:
        f.user_flow_id = "UF-073"
    rows2, _ = build_orphan_flow_gaps(
        [], [DevStub("soc0-frontend", flows2)], [])
    assert rows2 == []


# ── 3. anti-case: backend/plumbing orphans never stamp ───────────────────


def test_backend_orphans_never_stamp(gate_on) -> None:
    devs = [DevStub("api", [
        FlowStub("manage-audit-flow", "backend/features/audit/audit.py",
                 line_ranges=[LR("backend/features/audit/audit.py", 1, 50)]),
        FlowStub("list-audit-events-flow",
                 "backend/features/audit/events.py",
                 line_ranges=[LR("backend/features/audit/events.py", 1, 40)]),
    ])]
    rows, _ = build_orphan_flow_gaps([], devs, [])
    assert rows == []


# ── 4. anti-case: single-entry cohort never stamps (R2 floor) ────────────


def test_single_entry_cohort_never_stamps(gate_on) -> None:
    devs = [DevStub("soc0-frontend", [_org_members_flows()[0]])]
    rows, _ = build_orphan_flow_gaps([], devs, [])
    assert rows == []


# ── 5. anti-case: no own-resource evidence never stamps ──────────────────


def test_no_own_resource_never_stamps(gate_on) -> None:
    base = "frontend/src/features/widgets"
    devs = [DevStub("soc0-frontend", [
        FlowStub("highlight-code-flow", f"{base}/CodeBox.tsx",
                 line_ranges=[LR(f"{base}/CodeBox.tsx", 1, 30)]),
        FlowStub("customize-theme-flow", f"{base}/ThemePick.tsx",
                 line_ranges=[LR(f"{base}/ThemePick.tsx", 1, 25)]),
    ])]
    rows, _ = build_orphan_flow_gaps([], devs, [])
    assert rows == []


# ── 6. B45 span law: a span-less qualifying cohort is skipped, counted ───


def test_spanless_cohort_skipped_and_counted(gate_on) -> None:
    flows = [
        FlowStub("manage-organization-members-flow",
                 f"{_DIR}/MembersTab.tsx"),
        FlowStub("process-member-requests-flow",
                 f"{_DIR}/components/RequestsSubTab.tsx"),
    ]
    rows, tele = build_orphan_flow_gaps(
        [], [DevStub("soc0-frontend", flows)], [])
    assert rows == []
    assert tele["skipped_no_spans"] == 1


# ── 7. PF ref resolution — emitted key or honest None ────────────────────


def test_pf_ref_resolves_only_to_emitted_key(gate_on) -> None:
    devs = [DevStub("soc0-frontend", _org_members_flows())]
    rows, _ = build_orphan_flow_gaps([], devs, [PFStub("organization-members")])
    assert rows[0].product_feature_id == "organization-members"
    rows2, _ = build_orphan_flow_gaps([], devs, [PFStub("cases")])
    assert rows2[0].product_feature_id is None


# ── 9. determinism ───────────────────────────────────────────────────────


def test_determinism(gate_on) -> None:
    def _run():
        devs = [DevStub("soc0-frontend", _org_members_flows())]
        rows, tele = build_orphan_flow_gaps([], devs, [])
        return [(g.id, g.label, g.loc,
                 [(s.path, s.start_line, s.end_line)
                  for s in g.surface_files]) for g in rows], tele
    assert _run() == _run()
