"""B57 Seg2 — Stage 6.7e Journey Evidence Adjudicator.

Every SACRED law from the spec has its own assertion, all against a
scripted mock client (no network):

  * fake citation → verdict rejected whole;
  * locale-VALUE citation (spaces, rung=i18n-key) → STRUCTURAL reject
    even when the string genuinely occurs in the file;
  * citation from a non-member file → reject;
  * merge: identical sets → union + lineage; overlap-not-subset →
    reject; ``PF=None`` shared sets (cal.com forensics — 7 authored
    rows) → ZERO merges + a dedicated counter;
  * demote → typed ``adjudicated_noise`` gap (never a drop; I8 holds —
    the PF stays covered by the gap); gap channel off ⇒ demote skipped;
  * rename on an e2e-authored row → structural reject; rename rides the
    B50 degrime/collision chain; uncited words are rejected;
  * Law C bar unchanged — a verdict with failed citations changes
    NOTHING (byte); confidence is written only by the Law C re-score;
  * keyless / no client ⇒ full no-op byte-identity;
  * unparseable JSON ⇒ whole batch rejected;
  * determinism — the same scripted mock replays to identical output.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from faultline.models.types import Feature, FlowLineRange, UserFlow
from faultline.pipeline_v2 import stage_6_7e_adjudicator as adj
from faultline.pipeline_v2.stage_6_7e_adjudicator import (
    ENV_FLAG,
    adjudicator_6_7e_enabled,
    run_stage_6_7e,
    select_candidates,
    verify_citations,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── fixtures ─────────────────────────────────────────────────────────────


def _pf(slug: str, display: str) -> Feature:
    return Feature(
        name=slug, display_name=display, layer="product", paths=[],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def _uf(uid: str, name: str, pfid: str | None, *, resource: str = "",
        members: list[str] | None = None, conf: str = "low",
        synthesis_reason: str | None = None) -> UserFlow:
    return UserFlow(
        id=uid, name=name, resource=resource or (pfid or "thing"),
        domain=None, product_feature_id=pfid, intent="manage",
        member_flow_ids=members or [], member_count=len(members or []),
        synthesized=False, category="interactive", name_confidence=conf,
        synthesis_reason=synthesis_reason,
    )


class _FL:
    """Minimal flow stand-in (uuid/name/paths/test_files/spans/backptr)."""

    def __init__(self, uuid: str, name: str, *,
                 paths: list[str] | None = None,
                 entry_point_file: str | None = None,
                 test_files: list[str] | None = None,
                 line_ranges: list[FlowLineRange] | None = None,
                 user_flow_id: str | None = None) -> None:
        self.uuid = uuid
        self.name = name
        self.description = ""
        self.paths = paths or []
        self.entry_point_file = entry_point_file
        self.test_files = test_files or []
        self.line_ranges = line_ranges or []
        self.user_flow_id = user_flow_id


class _FakeClient:
    """Scripted client: pops the next payload per call; records calls."""

    def __init__(self, payloads: list[str]) -> None:
        self.messages = self
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def create(self, **kw):
        self.calls.append(kw)
        payload = self.payloads.pop(0) if self.payloads else "{}"
        return SimpleNamespace(
            content=[SimpleNamespace(text=payload)],
            usage=SimpleNamespace(input_tokens=800, output_tokens=200),
        )


def _verdicts_payload(verdicts: list[dict]) -> str:
    return json.dumps({"verdicts": verdicts})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # B62: ENV_FLAG (adjudicator) and UF_RUNG_SOURCES_V2 default ON now, so
    # pin the clean OFF baseline with X=0; the model / gap-channel knobs are
    # not part of the flip and keep their unset defaults.
    monkeypatch.setenv(ENV_FLAG, "0")
    monkeypatch.delenv("FAULTLINE_STAGE_6_7E_MODEL", raising=False)
    monkeypatch.delenv("FAULTLINE_COVERAGE_GAP_CHANNEL", raising=False)
    monkeypatch.setenv("FAULTLINE_UF_RUNG_SOURCES_V2", "0")
    yield


def _widget_board(tmp_path):
    """One PF, one low UF whose member file carries a real i18n KEY and a
    locale VALUE sentence (the discriminator exhibit)."""
    app = tmp_path / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "widgets.tsx").write_text(
        "const t1 = t('widget_board');\n"
        'const copy = "Billing Overview Page";\n', encoding="utf-8")
    (app / "other.tsx").write_text(
        "const t2 = t('widget_board');\n", encoding="utf-8")
    pfs = [_pf("gadgets", "Gadgets")]
    ufs = [_uf("UF-1", "Browse widgets", "gadgets", resource="widgets",
               members=["f1"], conf="low")]
    flows = [_FL("f1", "list-things-flow", paths=["app/widgets.tsx"],
                 user_flow_id="UF-1")]
    return pfs, ufs, flows


def _run(ufs, flows, pfs, tmp_path, payloads, **kw):
    client = _FakeClient(payloads)
    tele, gaps = run_stage_6_7e(
        ufs, flows, pfs, repo_root=tmp_path, client=client, **kw)
    return tele, gaps, client


def _state(ufs):
    return [(u.id, u.name, u.name_confidence, u.name_evidence,
             tuple(u.member_flow_ids)) for u in ufs]


# ── keyless / no-client — byte-identity ─────────────────────────────────


class TestKeylessNoOp:

    def test_no_client_hard_noop(self, tmp_path):
        pfs, ufs, flows = _widget_board(tmp_path)
        before = _state(ufs)
        tele, gaps = run_stage_6_7e(
            ufs, flows, pfs, repo_root=tmp_path,
            client=None, _client_factory=lambda: None)
        assert tele["ran"] is False
        assert tele["skipped"] == "no-client"
        assert _state(ufs) == before          # zero mutations
        assert gaps == []
        assert flows[0].user_flow_id == "UF-1"

    def test_flag_default_on(self, monkeypatch):
        # B62 flip: default ON (KEY_SCHEMA 29). Unset ⇒ enabled; X=0 disables.
        monkeypatch.delenv(ENV_FLAG, raising=False)
        assert adjudicator_6_7e_enabled() is True
        monkeypatch.setenv(ENV_FLAG, "1")
        assert adjudicator_6_7e_enabled() is True
        monkeypatch.setenv(ENV_FLAG, "0")
        assert adjudicator_6_7e_enabled() is False


# ── citation verifier laws ───────────────────────────────────────────────


class TestCitationVerifier:

    def test_unselected_uid_rejected(self, tmp_path):
        """Defense-in-depth: a verdict for an on-board row the LLM was
        never asked about (high conf, not a dup candidate) is rejected
        even when its citation is genuine."""
        pfs, ufs, flows = _widget_board(tmp_path)
        ufs.append(_uf("UF-9", "Manage gizmos", "gadgets",
                       resource="gizmos", members=["f9"], conf="high"))
        flows.append(_FL("f9", "gizmo-flow", paths=["app/other.tsx"],
                         user_flow_id="UF-9"))
        before = _state(ufs)
        tele, gaps, _ = _run(ufs, flows, pfs, tmp_path, [_verdicts_payload([
            {"uf_id": "UF-9", "verdict": "rung_evidence",
             "citations": [{"file": "app/other.tsx",
                            "exact_string": "widget_board",
                            "rung": "i18n-key"}]},
        ])])
        assert tele["rejected_reasons"].get("verdict-unselected") == 1
        assert tele["verdicts"]["rung_evidence"] == 0
        assert _state(ufs)[1] == before[1]    # unselected row untouched
        assert gaps == []

    def test_accepted_citations_persisted_for_audit(self, tmp_path):
        """An ACCEPTED verdict's verified citation packet lands in
        tele['citations_applied'] — the operator's hand-audit trail."""
        pfs, ufs, flows = _widget_board(tmp_path)
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [_verdicts_payload([
            {"uf_id": "UF-1", "verdict": "rung_evidence",
             "citations": [{"file": "app/widgets.tsx",
                            "exact_string": "widget_board",
                            "rung": "i18n-key"}]},
        ])])
        assert tele["verdicts"]["rung_evidence"] == 1
        assert tele["citations_applied"] == [{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/widgets.tsx",
                           "exact_string": "widget_board",
                           "rung": "i18n-key"}],
        }]

    def test_fake_citation_rejected(self, tmp_path):
        pfs, ufs, flows = _widget_board(tmp_path)
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/widgets.tsx",
                           "exact_string": "no_such_string_here",
                           "rung": "i18n-key"}],
        }])
        before = _state(ufs)
        tele, gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["rejected_reasons"].get("citation-not-found") == 1
        assert tele["verdicts"]["rung_evidence"] == 0
        # Law C планка — nothing was earned, nothing changed (byte).
        assert _state(ufs) == before

    def test_locale_value_citation_structural_reject(self, tmp_path):
        # The sentence GENUINELY occurs in the member file — the substring
        # check would pass. The i18n-key SHAPE law rejects it anyway:
        # space-broken human copy is a translated VALUE, never a key.
        pfs, ufs, flows = _widget_board(tmp_path)
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/widgets.tsx",
                           "exact_string": "Billing Overview Page",
                           "rung": "i18n-key"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["rejected_reasons"].get("citation-i18n-value") == 1
        assert ufs[0].name_confidence == "low"

    def test_foreign_file_citation_rejected(self, tmp_path):
        # app/other.tsx exists AND contains the string — but it is not a
        # member file of UF-1, so the citation is foreign.
        pfs, ufs, flows = _widget_board(tmp_path)
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/other.tsx",
                           "exact_string": "widget_board",
                           "rung": "i18n-key"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["rejected_reasons"].get("citation-foreign-file") == 1
        assert ufs[0].name_confidence == "low"

    def test_verified_citation_uplifts_via_law_c(self, tmp_path):
        pfs, ufs, flows = _widget_board(tmp_path)
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/widgets.tsx",
                           "exact_string": "widget_board",
                           "rung": "i18n-key"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rung_evidence"] == 1
        # Confidence moved ONLY through the Law C re-score, with the
        # adjudicated provenance tag.
        assert ufs[0].name_confidence == "high"
        assert "adjudicated:i18n-key" in (ufs[0].name_evidence or [])
        assert "law_c_rescore" in tele

    def test_unit_verify_citations(self, tmp_path):
        pfs, ufs, flows = _widget_board(tmp_path)
        from faultline.pipeline_v2.naming_contract import _uf_flow_maps
        _, _, flow_by_id = _uf_flow_maps(flows)
        ok, _ = verify_citations(
            ufs[0], [{"file": "app/widgets.tsx",
                      "exact_string": "widget_board", "rung": "i18n-key"}],
            flow_by_id, tmp_path, {})
        assert ok
        ok, reason = verify_citations(
            ufs[0], [{"file": "app/widgets.tsx",
                      "exact_string": "widget_board", "rung": "nav"}],
            flow_by_id, tmp_path, {})
        assert not ok and reason == "citation-rung"


# ── merge laws ───────────────────────────────────────────────────────────


class TestMerge:

    def _dup_board(self, tmp_path, *, pf: str | None = "gadgets"):
        pfs = [_pf("gadgets", "Gadgets")]
        ufs = [
            _uf("UF-A", "Manage widgets", pf, resource="widgets",
                members=["f1", "f2"], conf="high"),
            _uf("UF-B", "Manage widget rows", pf, resource="widgets",
                members=["f1", "f2"], conf="high"),
        ]
        flows = [
            _FL("f1", "create-widget-flow", user_flow_id="UF-A"),
            _FL("f2", "delete-widget-flow", user_flow_id="UF-B"),
        ]
        return pfs, ufs, flows

    def test_identical_sets_merge_union_lineage(self, tmp_path):
        pfs, ufs, flows = self._dup_board(tmp_path)
        payload = _verdicts_payload([{
            "uf_id": "UF-B", "verdict": "merge", "citations": [],
            "target": "UF-A",
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["merge"] == 1
        ids = [u.id for u in ufs]
        assert ids == ["UF-A"]                       # smaller id survives
        assert set(ufs[0].member_flow_ids) == {"f1", "f2"}   # union — no loss
        assert ufs[0].member_count == 2
        # I14 — the dropped row's backpointer repointed, never dangling.
        assert flows[1].user_flow_id == "UF-A"
        m = tele["merge_map"][0]
        assert (m["dropped_id"], m["into_id"], m["relation"]) == (
            "UF-B", "UF-A", "identical")
        assert m["pf"] == "gadgets"

    def test_subset_merges_into_superset(self, tmp_path):
        pfs = [_pf("gadgets", "Gadgets")]
        ufs = [
            _uf("UF-A", "Manage widgets", "gadgets", members=["f1"],
                conf="high"),
            _uf("UF-B", "Manage all widgets", "gadgets",
                members=["f1", "f2"], conf="high"),
        ]
        flows = [_FL("f1", "create-widget-flow", user_flow_id="UF-A"),
                 _FL("f2", "delete-widget-flow", user_flow_id="UF-B")]
        payload = _verdicts_payload([{
            "uf_id": "UF-A", "verdict": "merge", "citations": [],
            "target": "UF-B",
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert [u.id for u in ufs] == ["UF-B"]        # superset survives
        assert set(ufs[0].member_flow_ids) == {"f1", "f2"}
        assert tele["merge_map"][0]["relation"] == "subset"

    def test_overlap_not_subset_rejected(self, tmp_path):
        pfs = [_pf("gadgets", "Gadgets")]
        ufs = [
            # low → selected via class (i); overlap pairs are never dup
            # candidates, so a high UF-A would be unselected and the
            # verdict would die earlier as verdict-unselected.
            _uf("UF-A", "Manage widgets", "gadgets", members=["f1", "f2"],
                conf="low"),
            _uf("UF-B", "Manage rows", "gadgets", members=["f2", "f3"],
                conf="high"),
            _uf("UF-Z", "Browse things", "gadgets", members=["f9"],
                conf="low"),
        ]
        flows = [_FL("f1", "a-flow"), _FL("f2", "b-flow"),
                 _FL("f3", "c-flow"), _FL("f9", "z-flow")]
        payload = _verdicts_payload([{
            "uf_id": "UF-A", "verdict": "merge", "citations": [],
            "target": "UF-B",
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["merge"] == 0
        assert tele["rejected_reasons"].get("merge-not-subset") == 1
        assert len(ufs) == 3                          # nothing dropped

    def test_pf_none_shared_sets_never_merge(self, tmp_path):
        # cal.com forensics: 7 authored rows share ONE member set with NO
        # PF — distinct authored intents, never duplication. Zero merges;
        # the dedicated counter fires; all 7 rows survive.
        pfs = [_pf("gadgets", "Gadgets")]
        ufs = [
            _uf(f"UF-{i}", f"Journey {i}", None, resource="account",
                members=["f1"], conf="low")
            for i in range(1, 8)
        ]
        flows = [_FL("f1", "shared-e2e-flow")]
        verdicts = [{
            "uf_id": f"UF-{i}", "verdict": "merge", "citations": [],
            "target": "UF-1",
        } for i in range(2, 8)]
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path,
                              [_verdicts_payload(verdicts)])
        assert tele["verdicts"]["merge"] == 0
        assert tele["rejected_pfless_merge"] == 6
        assert len(ufs) == 7                          # operator truth kept
        # … and the selector never proposed them as dup candidates either.
        cands, sel_tele = select_candidates(ufs, set())
        assert sel_tele["selected_dup"] == 0
        assert sel_tele["pfless_dup_groups"] == 1


# ── demote laws ──────────────────────────────────────────────────────────


class TestDemote:

    def _noise_board(self, tmp_path, *, reason: str | None = "backstop"):
        pfs = [_pf("gadgets", "Gadgets")]
        ufs = [
            _uf("UF-1", "Manage widget plumbing", "gadgets",
                members=["f1"], conf="low", synthesis_reason=reason),
            _uf("UF-2", "Browse widgets", "gadgets", members=["f2"],
                conf="high"),
            # low filler keeps the selection non-empty even when UF-1 is
            # authored-carved out of class (i).
            _uf("UF-Z", "Browse things", "gadgets", members=["f9"],
                conf="low"),
        ]
        ufs[0].routes = ["/api/widgets/plumbing"]
        flows = [
            _FL("f1", "plumbing-flow", user_flow_id="UF-1",
                line_ranges=[FlowLineRange(
                    path="app/plumbing.ts", start_line=1, end_line=40)]),
            _FL("f2", "list-widget-flow", user_flow_id="UF-2"),
            _FL("f9", "z-flow", user_flow_id="UF-Z"),
        ]
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "demote", "citations": [],
        }])
        return pfs, ufs, flows, payload

    def test_demote_becomes_typed_gap_not_drop(self, tmp_path):
        pfs, ufs, flows, payload = self._noise_board(tmp_path)
        tele, gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["demote"] == 1
        assert [u.id for u in ufs] == ["UF-2", "UF-Z"]   # row left user_flows
        assert len(gaps) == 1
        g = gaps[0]
        # Typed gap — everything rides along; I8 holds (PF covered by gap).
        assert g.kind == "adjudicated_noise"
        assert g.label == "Manage widget plumbing"
        assert g.product_feature_id == "gadgets"
        assert g.routes == ["/api/widgets/plumbing"]
        assert g.synthesis_reason == "backstop"
        assert g.loc == 40                            # member span carried
        assert g.surface_files[0].path == "app/plumbing.ts"
        assert flows[0].user_flow_id is None          # I14 — nulled
        assert tele["demote_map"][0]["gap_id"] == g.id

    def test_gap_channel_off_skips_demote(self, tmp_path, monkeypatch):
        # A silent drop is forbidden — with no gap channel the row STAYS.
        monkeypatch.setenv("FAULTLINE_COVERAGE_GAP_CHANNEL", "off")
        pfs, ufs, flows, payload = self._noise_board(tmp_path)
        tele, gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["demote"] == 0
        assert tele["demote_skipped_no_gap_channel"] == 1
        assert [u.id for u in ufs] == ["UF-1", "UF-2", "UF-Z"]
        assert gaps == []

    def test_authored_row_never_demoted(self, tmp_path):
        pfs, ufs, flows, _ = self._noise_board(
            tmp_path, reason="e2e_journey_recall")
        # Identical-set sibling → UF-1 is dup-SELECTED (class ii includes
        # authored rows — the documenso e2e class); the authored guard,
        # not the selection wall, must be what rejects the demote.
        ufs.insert(2, _uf("UF-1B", "Widget plumbing checks", "gadgets",
                          members=["f1"], conf="high",
                          synthesis_reason="e2e_journey_recall"))
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "demote", "citations": [],
        }])
        tele, gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["demote"] == 0
        assert tele["rejected_reasons"].get("demote-authored") == 1
        assert len(ufs) == 4 and gaps == []


# ── rename laws ──────────────────────────────────────────────────────────


class TestRename:

    def _board(self, tmp_path, *, reason: str | None = None):
        app = tmp_path / "app"
        app.mkdir(parents=True, exist_ok=True)
        (app / "widgets.tsx").write_text(
            "const t1 = t('widget_board');\n", encoding="utf-8")
        pfs = [_pf("gadgets", "Gadgets")]
        ufs = [
            _uf("UF-1", "Manage stuff", "gadgets", resource="widgets",
                members=["f1"], conf="low", synthesis_reason=reason),
            # low filler keeps the selection non-empty even when UF-1 is
            # authored-carved out of class (i).
            _uf("UF-Z", "Browse things", "gadgets", members=["f9"],
                conf="low"),
        ]
        flows = [_FL("f1", "list-things-flow", paths=["app/widgets.tsx"]),
                 _FL("f9", "z-flow")]
        return pfs, ufs, flows

    def _payload(self, target: str):
        return _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rename",
            "citations": [{"file": "app/widgets.tsx",
                           "exact_string": "widget_board",
                           "rung": "i18n-key"}],
            "target": target,
        }])

    def test_rename_from_cited_identifier(self, tmp_path):
        pfs, ufs, flows = self._board(tmp_path)
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path,
                              [self._payload("Widget board")])
        assert tele["verdicts"]["rename"] == 1
        assert ufs[0].name.lower() == "widget board"
        assert tele["renames"][0]["before"] == "Manage stuff"

    def test_uncited_words_rejected(self, tmp_path):
        pfs, ufs, flows = self._board(tmp_path)
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path,
                              [self._payload("Awesome widget board")])
        assert tele["verdicts"]["rename"] == 0
        assert tele["rejected_reasons"].get("rename-uncited-tokens") == 1
        assert ufs[0].name == "Manage stuff"

    def test_authored_row_never_renamed(self, tmp_path):
        # B23 carve — the maintainer's playwright label is untouchable.
        pfs, ufs, flows = self._board(tmp_path, reason="e2e_journey_recall")
        # Identical-set sibling → UF-1 is dup-SELECTED (class ii includes
        # authored rows); the authored guard must reject, not selection.
        ufs.append(_uf("UF-1B", "Widget board checks", "gadgets",
                       members=["f1"], conf="high",
                       synthesis_reason="e2e_journey_recall"))
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path,
                              [self._payload("Widget board")])
        assert tele["verdicts"]["rename"] == 0
        assert tele["rejected_reasons"].get("rename-authored") == 1
        assert ufs[0].name == "Manage stuff"

    def test_collision_safe_via_b50_plan(self, tmp_path):
        # Another row already wears the target display — the B50
        # collision-safe plan skips the rename (kan law: a rename never
        # creates a display dup).
        pfs, ufs, flows = self._board(tmp_path)
        ufs.append(_uf("UF-2", "Widget board", "gadgets",
                       members=["f2"], conf="high"))
        flows.append(_FL("f2", "view-board-flow"))
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path,
                              [self._payload("Widget board")])
        assert tele["verdicts"]["rename"] == 0
        assert tele["rejected_reasons"].get("rename-collision") == 1
        assert ufs[0].name == "Manage stuff"

    def test_value_citation_rejected_for_rename(self, tmp_path):
        pfs, ufs, flows = self._board(tmp_path)
        (tmp_path / "app" / "widgets.tsx").write_text(
            "const t1 = t('widget_board');\n"
            'const copy = "Widget Board Page";\n', encoding="utf-8")
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rename",
            "citations": [{"file": "app/widgets.tsx",
                           "exact_string": "Widget Board Page",
                           "rung": "member-noun"}],
            "target": "Widget board page",
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rename"] == 0
        assert tele["rejected_reasons"].get("rename-value-citation") == 1
        assert ufs[0].name == "Manage stuff"


# ── batch / parse / selection / determinism ─────────────────────────────


class TestBatchAndSelection:

    def test_unparseable_batch_rejected_whole(self, tmp_path):
        pfs, ufs, flows = _widget_board(tmp_path)
        before = _state(ufs)
        tele, gaps, _ = _run(ufs, flows, pfs, tmp_path,
                             ["utter nonsense, no json here"])
        assert tele["batches_rejected_parse"] == 1
        assert tele["verdicts"] == {"rung_evidence": 0, "rename": 0,
                                    "merge": 0, "demote": 0}
        assert _state(ufs) == before
        assert gaps == []

    def test_selection_classes(self, tmp_path):
        # (i) non-high; (ii) all-high dup group (documenso class);
        # authored rows excluded from (i); markers excluded entirely.
        pfs = [_pf("gadgets", "Gadgets")]
        marker = _uf("UF-M", "Uncovered: Gadgets routes", "gadgets",
                     members=[], conf="low")
        marker.synthesized = True
        ufs = [
            _uf("UF-1", "Browse widgets", "gadgets", members=["f1"],
                conf="low"),
            _uf("UF-2", "Manage widgets", "gadgets", members=["f2", "f3"],
                conf="high"),
            _uf("UF-3", "Manage widget rows", "gadgets",
                members=["f2", "f3"], conf="high"),
            _uf("UF-4", "Authored journey", "gadgets", members=["f4"],
                conf="low", synthesis_reason="e2e_journey_recall"),
            marker,
        ]
        cands, tele = select_candidates(ufs, set())
        ids = [str(u.id) for u in cands]
        assert "UF-1" in ids                     # class (i)
        assert "UF-2" in ids and "UF-3" in ids   # class (ii), both high
        assert "UF-4" not in ids                 # authored — carved out
        assert "UF-M" not in ids                 # marker — B45 business
        assert tele["selected_dup"] == 2

    def test_determinism_same_mock_identical_output(self, tmp_path):
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/widgets.tsx",
                           "exact_string": "widget_board",
                           "rung": "i18n-key"}],
        }])

        def go():
            pfs, ufs, flows = _widget_board(tmp_path)
            tele, gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
            return _state(ufs), [g.id for g in gaps], tele["verdicts"]

        assert go() == go()

    def test_verdict_for_unselected_row_is_ignored_safely(self, tmp_path):
        # A verdict about a row the packages never contained can still be
        # verified — but a HIGH row with no citations fails honestly.
        pfs, ufs, flows = _widget_board(tmp_path)
        payload = _verdicts_payload([{
            "uf_id": "UF-NOPE", "verdict": "rename", "citations": [],
            "target": "X",
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["rejected_reasons"].get("verdict-unknown-uf") == 1

    def test_cost_telemetry_present(self, tmp_path):
        pfs, ufs, flows = _widget_board(tmp_path)
        tele, _gaps, client = _run(ufs, flows, pfs, tmp_path,
                                   [_verdicts_payload([])])
        assert tele["ran"] is True
        assert tele["batches"] == 1
        assert tele["llm_calls"] == 1
        assert tele["cost_usd"] >= 0.0
        assert len(client.calls) == 1


# ── cache replay ─────────────────────────────────────────────────────────


class _DictCache:
    def __init__(self) -> None:
        self.store: dict = {}

    def get(self, kind, key):
        return self.store.get((kind, key))

    def set(self, kind, key, value):
        self.store[(kind, key)] = value


class TestCacheReplay:

    def test_second_run_replays_from_cache(self, tmp_path):
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/widgets.tsx",
                           "exact_string": "widget_board",
                           "rung": "i18n-key"}],
        }])
        cache = _DictCache()
        pfs, ufs, flows = _widget_board(tmp_path)
        tele1, _g1, _ = _run(ufs, flows, pfs, tmp_path, [payload],
                             cache=cache)
        assert tele1["llm_calls"] == 1 and tele1["cache_hits"] == 0
        pfs2, ufs2, flows2 = _widget_board(tmp_path)
        tele2, _g2, _ = _run(ufs2, flows2, pfs2, tmp_path, [],
                             cache=cache)
        assert tele2["llm_calls"] == 0 and tele2["cache_hits"] == 1
        # Replay produced the SAME board state ($0).
        assert _state(ufs2) == _state(ufs)
        assert adj._CACHE_VERSION == 1


# ══════════════════════════════════════════════════════════════════════════
# b57-seg2-iter — rename humanization / member_symbols / verb channel
# ══════════════════════════════════════════════════════════════════════════


class TestIterRenameHumanization:
    """A cited identifier/slug is EVIDENCE, not a display — the target is
    humanized deterministically before the B50 chain (the papermark
    ON-board raw-slug bug)."""

    def _board(self, tmp_path):
        app = tmp_path / "app"
        app.mkdir(parents=True, exist_ok=True)
        (app / "account.tsx").write_text(
            "const t1 = t('confirm-email-change');\n", encoding="utf-8")
        pfs = [_pf("account", "Account")]
        ufs = [_uf("UF-1", "Manage confirm email change", "account",
                   resource="email", members=["f1"], conf="low")]
        flows = [_FL("f1", "list-things-flow", paths=["app/account.tsx"])]
        return pfs, ufs, flows

    def test_slug_target_renders_human_display(self, tmp_path):
        pfs, ufs, flows = self._board(tmp_path)
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rename",
            "citations": [{"file": "app/account.tsx",
                           "exact_string": "confirm-email-change",
                           "rung": "member-noun"}],
            "target": "confirm-email-change",
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rename"] == 1
        # Never the raw slug — the deterministic humanization + polish.
        assert ufs[0].name == "Confirm email change"

    def test_humanization_never_smuggles_uncited_words(self, tmp_path):
        # The subset law still judges the RAW cited tokens.
        pfs, ufs, flows = self._board(tmp_path)
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rename",
            "citations": [{"file": "app/account.tsx",
                           "exact_string": "confirm-email-change",
                           "rung": "member-noun"}],
            "target": "confirm-email-change wizard",
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rename"] == 0
        assert tele["rejected_reasons"].get("rename-uncited-tokens") == 1


class TestIterMemberSymbols:
    """The papermark citation famine — declared symbol names become
    citable REAL strings in the evidence package (rung member-noun; the
    verifier is UNCHANGED)."""

    def _board(self, tmp_path):
        app = tmp_path / "app"
        app.mkdir(parents=True, exist_ok=True)
        (app / "board.ts").write_text(
            "export async function createWidgetBoard(input: In) {}\n"
            "const renderBoardRow = (r: Row) => r.id;\n"
            "function helperThing(x) { return x; }\n", encoding="utf-8")
        pfs = [_pf("gadgets", "Gadgets")]
        ufs = [_uf("UF-1", "Browse boards", "gadgets", resource="boards",
                   members=["f1"], conf="low")]
        flows = [_FL("f1", "list-things-flow", paths=["app/board.ts"])]
        return pfs, ufs, flows

    def test_package_carries_member_symbols(self, tmp_path):
        pfs, ufs, flows = self._board(tmp_path)
        from faultline.pipeline_v2.naming_contract import _uf_flow_maps
        _, _, flow_by_id = _uf_flow_maps(flows)
        pkgs = adj.build_evidence_packages(
            ufs, ufs, flow_by_id, {}, tmp_path, {})
        syms = pkgs[0]["member_symbols"]
        assert syms == [{"file": "app/board.ts", "symbols": sorted([
            "createWidgetBoard", "renderBoardRow", "helperThing"])}]

    def test_symbol_citation_verifies_and_uplifts(self, tmp_path):
        pfs, ufs, flows = self._board(tmp_path)
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/board.ts",
                           "exact_string": "createWidgetBoard",
                           "rung": "member-noun"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rung_evidence"] == 1
        assert "adjudicated:member-noun" in (ufs[0].name_evidence or [])

    def test_extractor_takes_no_garbage(self):
        text = (
            "import { a } from 'b';\n"
            "if (x) { return foo; }\n"
            "  const inner = () => {};\n"          # indented — not top-level
            "const x = () => {};\n"                 # 1-char name — skipped
            "export const API_ROUTES = buildRoutes();\n"
            "export default async function handleSubmit(req) {}\n"
        )
        assert adj._symbols_from_text(text) == [
            "API_ROUTES", "handleSubmit"]

    def test_extractor_python_and_caps(self):
        text = "\n".join(
            [f"def handler_{chr(97 + i)}(x):\n    pass" for i in range(12)])
        syms = adj._symbols_from_text(text)
        assert len(syms) == adj._MAX_SYMBOLS_PER_FILE
        assert syms[0] == "handler_a"               # document order kept


class TestIterVerbChannel:
    """b57-seg2-iter — the verb channel: the VERIFIER maps citation →
    verb family (Seg1 frozen method map / the engine's one action-family
    vocabulary) and requires agreement with the UF name's lead verb."""

    def _route_board(self, tmp_path, method_body: str):
        app = tmp_path / "app" / "api" / "widgets"
        app.mkdir(parents=True, exist_ok=True)
        (app / "route.ts").write_text(method_body, encoding="utf-8")
        pfs = [_pf("stuff", "Stuff")]
        # resource grounded by the member name ('widget'); verb NOT
        # (lead delete vs member family act — no Law B narrowing).
        ufs = [_uf("UF-1", "Delete widgets", "stuff", resource="widgets",
                   members=["f1"], conf="medium")]
        flows = [_FL("f1", "run-widget-flow",
                     paths=["app/api/widgets/route.ts"])]
        return pfs, ufs, flows

    def test_get_route_never_grounds_delete_lead(self, tmp_path):
        # Anti-case (а): the string 'GET' genuinely exists in the member
        # file — the FAMILY law rejects it, not the grep.
        pfs, ufs, flows = self._route_board(
            tmp_path, "export async function GET(req) {}\n")
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/api/widgets/route.ts",
                           "exact_string": "GET",
                           "rung": "verb-route-method"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rung_evidence"] == 0
        assert tele["rejected_reasons"].get("verb-family-mismatch") == 1
        assert ufs[0].name_confidence == "medium"    # honest residue

    def test_delete_method_grounds_delete_lead(self, tmp_path):
        pfs, ufs, flows = self._route_board(
            tmp_path, "export async function DELETE(req) {}\n")
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/api/widgets/route.ts",
                           "exact_string": "DELETE",
                           "rung": "verb-route-method"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rung_evidence"] == 1
        assert ufs[0].name_confidence == "high"
        assert "adjudicated:verb-route-method" in (ufs[0].name_evidence or [])

    def test_locale_value_verb_citation_structural_reject(self, tmp_path):
        # Anti-case (б): a space-broken human phrase can never be a
        # verb-i18n-key — even though it exists in the file verbatim.
        pfs, ufs, flows = self._route_board(
            tmp_path, 'const copy = "Delete The Widget Now";\n')
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/api/widgets/route.ts",
                           "exact_string": "Delete The Widget Now",
                           "rung": "verb-i18n-key"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rung_evidence"] == 0
        assert tele["rejected_reasons"].get("citation-i18n-value") == 1

    def test_non_verb_symbol_rejected(self, tmp_path):
        # Anti-case (в): WebhookForm is a noun symbol — no verb family.
        pfs, ufs, flows = self._route_board(
            tmp_path, "export class WebhookForm {}\n")
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/api/widgets/route.ts",
                           "exact_string": "WebhookForm",
                           "rung": "verb-symbol"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rung_evidence"] == 0
        assert tele["rejected_reasons"].get("verb-citation-not-verb") == 1

    def test_imperative_symbol_grounds_create_lead(self, tmp_path):
        # Anti-case (г): createBooking grounds 'Create booking'.
        app = tmp_path / "app"
        app.mkdir(parents=True, exist_ok=True)
        (app / "booking.ts").write_text(
            "export async function createBooking(input) {}\n",
            encoding="utf-8")
        pfs = [_pf("bookings", "Bookings")]
        ufs = [_uf("UF-1", "Create booking", "bookings", resource="booking",
                   members=["f1"], conf="medium")]
        flows = [_FL("f1", "run-booking-flow", paths=["app/booking.ts"])]
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/booking.ts",
                           "exact_string": "createBooking",
                           "rung": "verb-symbol"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rung_evidence"] == 1
        assert ufs[0].name_confidence == "high"
        assert "adjudicated:verb-symbol" in (ufs[0].name_evidence or [])

    def test_bdd_assertion_grounds_create_lead(self, tmp_path):
        # Anti-case (д): 'should create team' from a MAPPED test file.
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "team.spec.ts").write_text(
            "it('should create team', async () => {});\n", encoding="utf-8")
        pfs = [_pf("teams", "Teams")]
        ufs = [_uf("UF-1", "Create team", "teams", resource="team",
                   members=["f1"], conf="medium")]
        flows = [_FL("f1", "run-team-flow",
                     test_files=["tests/team.spec.ts"])]
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "tests/team.spec.ts",
                           "exact_string": "should create team",
                           "rung": "verb-test-assert"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rung_evidence"] == 1
        assert ufs[0].name_confidence == "high"
        assert "adjudicated:verb-test-assert" in (ufs[0].name_evidence or [])

    def test_verb_test_assert_requires_mapped_test_file(self, tmp_path):
        # The assertion string lives in a MEMBER SOURCE file — the
        # verb-test-assert rung demands a mapped TEST file.
        app = tmp_path / "app"
        app.mkdir(parents=True, exist_ok=True)
        (app / "team.ts").write_text(
            "const label = 'should create team';\n", encoding="utf-8")
        pfs = [_pf("teams", "Teams")]
        ufs = [_uf("UF-1", "Create team", "teams", resource="team",
                   members=["f1"], conf="medium")]
        flows = [_FL("f1", "run-team-flow", paths=["app/team.ts"])]
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/team.ts",
                           "exact_string": "should create team",
                           "rung": "verb-test-assert"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rung_evidence"] == 0
        assert tele["rejected_reasons"].get("verb-citation-not-test") == 1

    def test_verb_i18n_key_grounds(self, tmp_path):
        pfs, ufs, flows = self._route_board(
            tmp_path, "const k = t('delete_widget');\n")
        payload = _verdicts_payload([{
            "uf_id": "UF-1", "verdict": "rung_evidence",
            "citations": [{"file": "app/api/widgets/route.ts",
                           "exact_string": "delete_widget",
                           "rung": "verb-i18n-key"}],
        }])
        tele, _gaps, _ = _run(ufs, flows, pfs, tmp_path, [payload])
        assert tele["verdicts"]["rung_evidence"] == 1
        assert "adjudicated:verb-i18n-key" in (ufs[0].name_evidence or [])


# ══════════════════════════════════════════════════════════════════════════
# b57-iter2 — verb-composition conflict synergy in the 6.7e selection
# ══════════════════════════════════════════════════════════════════════════


class TestIter2ConflictSelection:
    """A name claiming a mutation family over a NON-EMPTY read-only
    member composition joins the selection (priority rename/demote
    candidate) and its package carries ``verb_composition_conflict``."""

    def _board(self, tmp_path, *, routes: bool):
        pfs = [_pf("gadgets", "Gadgets")]
        # HIGH confidence — class (i) would never select it; only the
        # conflict synergy can.
        ufs = [_uf("UF-1", "Delete widgets", "gadgets", resource="widgets",
                   members=["f1"], conf="high")]
        flows = [_FL("f1", "run-widget-flow",
                     paths=["app/api/widgets/route.ts"])]
        routes_index = ([{"pattern": "/api/widgets", "method": "GET",
                          "file": "app/api/widgets/route.ts"}]
                        if routes else [])
        return pfs, ufs, flows, routes_index

    def test_conflict_row_selected_and_flagged(self, tmp_path):
        pfs, ufs, flows, routes = self._board(tmp_path, routes=True)
        client = _FakeClient([_verdicts_payload([])])
        tele, _gaps = run_stage_6_7e(
            ufs, flows, pfs, repo_root=tmp_path, client=client,
            routes_index=routes)
        assert tele["selected_verb_conflict"] == 1
        assert tele["selected"] == 1
        # The evidence package the model saw carries the conflict flag.
        user_payload = json.loads(client.calls[0]["messages"][0]["content"])
        pkg = user_payload["user_flows"][0]
        assert pkg["uf_id"] == "UF-1"
        assert pkg["verb_composition_conflict"] is True

    def test_empty_composition_never_accuses(self, tmp_path):
        # Anti-case: no routes → no facts → no flag, and the high row is
        # not selected at all (stage no-ops with zero candidates).
        pfs, ufs, flows, routes = self._board(tmp_path, routes=False)
        client = _FakeClient([_verdicts_payload([])])
        tele, _gaps = run_stage_6_7e(
            ufs, flows, pfs, repo_root=tmp_path, client=client,
            routes_index=routes)
        assert tele["selected_verb_conflict"] == 0
        assert tele["selected"] == 0
        assert client.calls == []                    # zero LLM spend

    def test_read_lead_never_flagged(self, tmp_path):
        # A read-verb name over a read-only composition is CONSISTENT —
        # no conflict, no selection (row is high).
        pfs, ufs, flows, routes = self._board(tmp_path, routes=True)
        ufs[0].name = "Browse widgets"
        client = _FakeClient([_verdicts_payload([])])
        tele, _gaps = run_stage_6_7e(
            ufs, flows, pfs, repo_root=tmp_path, client=client,
            routes_index=routes)
        assert tele["selected_verb_conflict"] == 0
        assert tele["selected"] == 0

    def test_unflagged_selected_row_carries_no_key(self, tmp_path):
        # Key-presence law: a low row selected by class (i) with no
        # conflict has NO verb_composition_conflict key in its package.
        pfs, ufs, flows, routes = self._board(tmp_path, routes=False)
        ufs[0].name_confidence = "low"
        client = _FakeClient([_verdicts_payload([])])
        tele, _gaps = run_stage_6_7e(
            ufs, flows, pfs, repo_root=tmp_path, client=client,
            routes_index=routes)
        assert tele["selected"] == 1
        user_payload = json.loads(client.calls[0]["messages"][0]["content"])
        assert "verb_composition_conflict" not in user_payload["user_flows"][0]
