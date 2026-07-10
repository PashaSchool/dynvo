"""Bug B23 — real code coordinates + authored labels for coverage markers.

Covers, per the fix contract (FAULTLINE_MARKER_SURFACE_COORDS, default ON):
  * e2e marker gets surface coordinates (mint-carried resolver files →
    whole-file spans) AND keeps its maintainer-authored playwright label;
  * system marker: mint-carried route-group files, home-PF member_files
    fallback for the 6.7d arms (candidates absent);
  * honesty gates — fully-claimed surface attaches NOTHING (the cal.com
    thin-shim class stays an honest residual), unmeasured files are never
    guessed, an e2e marker never inherits a whole-PF fallback surface;
  * dedup-by-construction — two e2e markers on one PF keep DISTINCT
    authored labels (flag off documents the old 'Uncovered:' collapse);
  * kill-switch =0 restores today's markers byte-identically (B13 rename
    incl. e2e, no spans, no new scan_meta keys);
  * Stage 6.97b stamps loc from surface spans (member-less only; member
    spans always win; per-file union);
  * serializer — surface_files omitted when None, the candidate ledger
    NEVER serializes;
  * determinism (same-input double run) + idempotency (re-run on already
    attached output is a no-op).
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

from faultline.models.types import UserFlow
from faultline.pipeline_v2.stage_6_97b_uf_loc import apply_uf_loc
from faultline.pipeline_v2.synth_quality import (
    E2E_RECALL_REASON,
    MARKER_SURFACE_COORDS_ENV,
    SYSTEM_RECALL_REASON,
    attach_marker_surface_coords,
    honest_coverage_markers,
    marker_surface_coords_enabled,
    run_synth_quality,
)


# ── fixture builders (duck-typed namespaces — the helpers use getattr) ──


def _uf(uid, name, *, reason=None, members=(), synthesized=False,
        pf=None, resource=None, candidates=None, surface_files=None,
        name_confidence="low"):
    return SimpleNamespace(
        id=uid, name=name, synthesis_reason=reason,
        member_flow_ids=list(members), member_count=len(members),
        synthesized=synthesized, product_feature_id=pf, resource=resource,
        routes=[], category="interactive", trigger=None,
        name_confidence=name_confidence, is_coverage_marker=False,
        surface_candidate_files=candidates, surface_files=surface_files,
        loc=None,
    )


def _flow(uuid, *, entry=None, paths=(), shared=()):
    return SimpleNamespace(
        uuid=uuid, name=uuid, entry_point_file=entry, paths=list(paths),
        shared_paths=[SimpleNamespace(path=p) for p in shared],
        nodes=[], user_flow_id=None,
    )


def _mf(path, loc):
    return SimpleNamespace(path=path, loc=loc)


def _pf(key, display, member_files=()):
    return SimpleNamespace(id=key, name=key, display_name=display,
                           member_files=list(member_files))


def _dev(*member_files):
    return SimpleNamespace(member_files=list(member_files))


# ── coordinates + label preservation ────────────────────────────────────────


def test_e2e_marker_gets_coords_and_keeps_authored_label():
    uf = _uf("UF-001", "Bulk Actions", reason=E2E_RECALL_REASON,
             synthesized=True, pf="team",
             candidates=["apps/t/page.tsx", "apps/t/claimed.tsx"])
    flows = [_flow("f1", entry="apps/t/claimed.tsx",
                   paths=["apps/t/claimed.tsx"])]
    pfs = [_pf("team", "Team")]
    devs = [_dev(_mf("apps/t/page.tsx", 250))]
    meta = {}
    honest_coverage_markers([uf], pfs, meta)
    tele = attach_marker_surface_coords([uf], flows, pfs, devs, meta)

    assert uf.name == "Bulk Actions"          # authored label preserved
    assert uf.is_coverage_marker is True      # marker typing still applies
    assert tele["attached"] == 1
    spans = [(s.path, s.start_line, s.end_line) for s in uf.surface_files]
    # claimed.tsx is flow-covered → filtered; page.tsx gets a whole-file span
    assert spans == [("apps/t/page.tsx", 1, 250)]
    assert uf.surface_candidate_files is None  # plumbing consumed


def test_system_marker_keeps_uncovered_name_and_carried_files():
    uf = _uf("UF-002", "Ingest hooks", reason=SYSTEM_RECALL_REASON,
             synthesized=True, pf="hooks",
             candidates=["svc/hooks/route.ts"])
    pfs = [_pf("hooks", "Hooks")]
    devs = [_dev(_mf("svc/hooks/route.ts", 40))]
    meta = {}
    honest_coverage_markers([uf], pfs, meta)
    tele = attach_marker_surface_coords([uf], [], pfs, devs, meta)

    assert uf.name == "Uncovered: Hooks routes"  # system seeds keep B13 name
    assert tele["attached"] == 1
    assert [(s.path, s.end_line) for s in uf.surface_files] == \
        [("svc/hooks/route.ts", 40)]


def test_system_marker_home_pf_fallback_when_no_candidates():
    # The 6.7d flowless/no-own-entry arms mint without a carried set —
    # the marker's trigger surface is its home PF's member files.
    uf = _uf("UF-003", "EDR — SentinelOne", reason=SYSTEM_RECALL_REASON,
             synthesized=True, pf="sentinelone")
    pfs = [_pf("sentinelone", "EDR — SentinelOne",
               member_files=[_mf("svc/s1/a.py", 100), _mf("svc/s1/b.py", 35)])]
    meta = {}
    honest_coverage_markers([uf], pfs, meta)
    tele = attach_marker_surface_coords([uf], [], pfs, [], meta)

    assert tele["attached"] == 1
    assert [(s.path, s.end_line) for s in uf.surface_files] == \
        [("svc/s1/a.py", 100), ("svc/s1/b.py", 35)]


def test_e2e_marker_never_inherits_whole_pf_fallback():
    # An e2e marker without carried candidates must NOT fall back to its
    # home PF surface (a mega-PF home would hand one journey 1000+ files).
    uf = _uf("UF-004", "Insights", reason=E2E_RECALL_REASON,
             synthesized=True, pf="trpc")
    pfs = [_pf("trpc", "tRPC",
               member_files=[_mf(f"packages/trpc/f{i}.ts", 50)
                             for i in range(5)])]
    meta = {}
    tele = attach_marker_surface_coords([uf], [], pfs, [], meta)
    assert tele["attached"] == 0
    assert tele["residual_no_surface"] == 1
    assert uf.surface_files is None


# ── honesty gates ────────────────────────────────────────────────────────────


def test_fully_claimed_surface_is_honest_residual():
    # cal.com thin-shim class: every resolved file already flow-claimed →
    # NO spans forced through covered code; the row stays loc=0 + counted.
    uf = _uf("UF-005", "Login", reason=E2E_RECALL_REASON, synthesized=True,
             pf="trpc", candidates=["pages/api/trpc/auth/[trpc].ts"])
    flows = [_flow("f1", paths=["pages/api/trpc/auth/[trpc].ts"])]
    devs = [_dev(_mf("pages/api/trpc/auth/[trpc].ts", 3))]
    meta = {}
    tele = attach_marker_surface_coords([uf], flows, [], devs, meta)
    assert tele["attached"] == 0
    assert tele["residual_claimed"] == 1
    assert uf.surface_files is None
    assert meta["synth_quality"]["surface_coords"]["residual_claimed"] == 1


def test_shared_path_counts_as_claimed():
    uf = _uf("UF-006", "Docs", reason=E2E_RECALL_REASON, synthesized=True,
             candidates=["lib/shared.ts"])
    flows = [_flow("f1", shared=("lib/shared.ts",))]
    devs = [_dev(_mf("lib/shared.ts", 90))]
    tele = attach_marker_surface_coords([uf], flows, [], devs, {})
    assert tele["residual_claimed"] == 1
    assert uf.surface_files is None


def test_unmeasured_files_never_guessed():
    uf = _uf("UF-007", "Orphan", reason=E2E_RECALL_REASON, synthesized=True,
             candidates=["apps/unknown.tsx"])
    tele = attach_marker_surface_coords([uf], [], [], [], {})
    assert tele["attached"] == 0
    assert tele["residual_unmeasured"] == 1
    assert uf.surface_files is None


def test_member_ful_uf_never_touched():
    uf = _uf("UF-008", "Real journey", members=("f1",), synthesized=True,
             reason=E2E_RECALL_REASON, candidates=["a.ts"])
    tele = attach_marker_surface_coords([uf], [], [], [_dev(_mf("a.ts", 9))], {})
    assert tele["attached"] == 0
    assert uf.surface_files is None
    assert uf.surface_candidate_files is None  # plumbing still cleared


# ── dedup by construction ───────────────────────────────────────────────────


def test_two_e2e_markers_on_one_pf_stay_distinct_rows():
    a = _uf("UF-010", "Bulk Actions", reason=E2E_RECALL_REASON,
            synthesized=True, pf="team")
    b = _uf("UF-011", "Public Profile", reason=E2E_RECALL_REASON,
            synthesized=True, pf="team")
    pfs = [_pf("team", "Team")]
    honest_coverage_markers([a, b], pfs, {})
    assert a.name == "Bulk Actions" and b.name == "Public Profile"
    assert a.name != b.name  # the 13x/18x 'Uncovered:' collapse is gone


def test_flag_off_documents_the_old_collapse(monkeypatch):
    monkeypatch.setenv(MARKER_SURFACE_COORDS_ENV, "0")
    a = _uf("UF-010", "Bulk Actions", reason=E2E_RECALL_REASON,
            synthesized=True, pf="team")
    b = _uf("UF-011", "Public Profile", reason=E2E_RECALL_REASON,
            synthesized=True, pf="team")
    pfs = [_pf("team", "Team")]
    honest_coverage_markers([a, b], pfs, {})
    assert a.name == b.name == "Uncovered: Team routes"  # today's behavior


# ── kill-switch byte-identical restore ──────────────────────────────────────


def test_kill_switch_restores_b13_markers(monkeypatch):
    monkeypatch.setenv(MARKER_SURFACE_COORDS_ENV, "0")
    assert not marker_surface_coords_enabled()
    uf = _uf("UF-020", "Webhooks", reason=E2E_RECALL_REASON,
             synthesized=True, pf="team", candidates=["apps/t/w.tsx"])
    pfs = [_pf("team", "Team")]
    devs = [_dev(_mf("apps/t/w.tsx", 120))]
    meta = {}
    honest_coverage_markers([uf], pfs, meta)
    tele = attach_marker_surface_coords([uf], [], pfs, devs, meta)

    assert uf.name == "Uncovered: Team routes"  # B13 rename incl. e2e
    assert uf.is_coverage_marker is True
    assert tele == {"attached": 0}
    assert uf.surface_files is None
    assert meta == {"synth_quality": {"coverage_markers": 1}}  # no new keys


def test_run_synth_quality_wires_coords(monkeypatch):
    monkeypatch.delenv(MARKER_SURFACE_COORDS_ENV, raising=False)
    uf = _uf("UF-030", "Direct Templates", reason=E2E_RECALL_REASON,
             synthesized=True, pf="team", candidates=["apps/t/tpl.tsx"])
    pfs = [_pf("team", "Team")]
    devs = [_dev(_mf("apps/t/tpl.tsx", 77))]
    meta = {}
    tele = run_synth_quality([uf], [], pfs, meta, developer_features=devs)
    assert tele["surface_coords_attached"] == 1
    assert tele["coverage_markers"] == 1
    assert uf.name == "Direct Templates"
    assert [(s.path, s.end_line) for s in uf.surface_files] == \
        [("apps/t/tpl.tsx", 77)]


# ── Stage 6.97b loc stamping ────────────────────────────────────────────────


def _span(path, start, end):
    return SimpleNamespace(path=path, start_line=start, end_line=end)


def test_6_97b_stamps_loc_from_surface_spans():
    marker = _uf("UF-040", "Bulk Actions", reason=E2E_RECALL_REASON,
                 synthesized=True,
                 surface_files=[_span("a.ts", 1, 100), _span("b.ts", 1, 35)])
    tele = apply_uf_loc([marker], [])
    assert marker.loc == 135
    assert tele["user_flows_surface_loc"] == 1


def test_6_97b_surface_union_per_file():
    marker = _uf("UF-041", "X", reason=SYSTEM_RECALL_REASON, synthesized=True,
                 surface_files=[_span("a.ts", 1, 60), _span("a.ts", 40, 100)])
    apply_uf_loc([marker], [])
    assert marker.loc == 100  # overlapping spans union, never double-count


def test_6_97b_member_spans_always_win():
    flow = SimpleNamespace(
        uuid="f1", name="f1", shared_paths=[],
        nodes=[SimpleNamespace(file="real.ts", lines=(1, 10), role="entry")],
    )
    uf = _uf("UF-042", "Real", members=("f1",),
             surface_files=[_span("noise.ts", 1, 999)])
    apply_uf_loc([uf], [flow])
    assert uf.loc == 10  # member-ful UFs never read the fallback


def test_6_97b_marker_without_spans_stays_honest_zero():
    marker = _uf("UF-043", "Thin", reason=E2E_RECALL_REASON, synthesized=True)
    tele = apply_uf_loc([marker], [])
    assert marker.loc == 0
    # additive tele key absent when no marker took the fallback (pre-B23
    # artifact shape preserved byte-identically)
    assert "user_flows_surface_loc" not in tele


# ── serializer contract (real pydantic model) ───────────────────────────────


def test_serializer_omits_none_and_never_dumps_candidates():
    plain = UserFlow(id="UF-050", name="Plain", intent="manage", resource="x")
    d = plain.model_dump(mode="json")
    assert "surface_files" not in d
    assert "surface_candidate_files" not in d

    marker = UserFlow(
        id="UF-051", name="Bulk Actions", intent="manage", resource="team",
        synthesized=True, synthesis_reason=E2E_RECALL_REASON,
        surface_candidate_files=["apps/t/page.tsx"],
    )
    d = marker.model_dump(mode="json")
    assert "surface_candidate_files" not in d  # plumbing NEVER serializes
    assert "surface_files" not in d            # None → omitted

    attach_marker_surface_coords(
        [marker], [], [], [_dev(_mf("apps/t/page.tsx", 250))], {})
    d = marker.model_dump(mode="json")
    assert d["surface_files"] == [
        {"path": "apps/t/page.tsx", "start_line": 1, "end_line": 250}]
    assert "surface_candidate_files" not in d


def test_userflow_rehydrates_old_json_without_surface_fields():
    d = {"id": "UF-060", "name": "Old", "intent": "manage", "resource": "x"}
    uf = UserFlow(**d)
    assert uf.surface_files is None
    assert uf.surface_candidate_files is None


# ── determinism + idempotency ───────────────────────────────────────────────


def _mixed_board():
    ufs = [
        _uf("UF-070", "Bulk Actions", reason=E2E_RECALL_REASON,
            synthesized=True, pf="team",
            candidates=["apps/t/b.tsx", "apps/t/a.tsx"]),
        _uf("UF-071", "Login", reason=E2E_RECALL_REASON, synthesized=True,
            pf="trpc", candidates=["pages/api/trpc/auth/[trpc].ts"]),
        _uf("UF-072", "EDR", reason=SYSTEM_RECALL_REASON, synthesized=True,
            pf="s1"),
        _uf("UF-073", "Real", members=("f1",)),
    ]
    flows = [_flow("f1", paths=["pages/api/trpc/auth/[trpc].ts"])]
    pfs = [_pf("team", "Team"), _pf("trpc", "tRPC"),
           _pf("s1", "S1", member_files=[_mf("svc/s1/a.py", 100)])]
    devs = [_dev(_mf("apps/t/a.tsx", 10), _mf("apps/t/b.tsx", 20),
                 _mf("pages/api/trpc/auth/[trpc].ts", 3))]
    return ufs, flows, pfs, devs


def _snapshot(ufs):
    return [
        (u.id, u.name,
         [(s.path, s.start_line, s.end_line) for s in (u.surface_files or [])],
         u.surface_candidate_files)
        for u in ufs
    ]


def test_double_run_deterministic():
    a = _mixed_board()
    b = copy.deepcopy(a)
    meta_a, meta_b = {}, {}
    attach_marker_surface_coords(a[0], a[1], a[2], a[3], meta_a)
    attach_marker_surface_coords(b[0], b[1], b[2], b[3], meta_b)
    assert _snapshot(a[0]) == _snapshot(b[0])
    assert meta_a == meta_b
    # spans are path-sorted (deterministic regardless of candidate order)
    assert [s.path for s in a[0][0].surface_files] == \
        ["apps/t/a.tsx", "apps/t/b.tsx"]


def test_rerun_on_attached_output_is_noop():
    ufs, flows, pfs, devs = _mixed_board()
    meta = {}
    attach_marker_surface_coords(ufs, flows, pfs, devs, meta)
    snap = _snapshot(ufs)
    tele2 = attach_marker_surface_coords(ufs, flows, pfs, devs, {})
    assert _snapshot(ufs) == snap          # spans untouched
    assert tele2["attached"] == 0          # idempotent — no re-derivation
