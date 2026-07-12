"""B50 — UF/PF naming de-grime + earned-confidence resource rung.

Three segments, all flag-gated and DISPLAY-ONLY (Seg1/Seg2 mutate only
``UserFlow.name`` / ``UserFlow.name_confidence`` / ``Feature.display_name``;
Seg3 mutates only ``UserFlow.name_confidence`` / ``UserFlow.name_evidence``):

  * Seg1 (``FAULTLINE_UF_NAME_DEGRIME``) — adjacent-echo discriminator.
  * Seg2 (``FAULTLINE_UF_NAME_DEGRIME``) — glyph-less raw-param display law.
  * Seg3 (``FAULTLINE_UF_RESOURCE_RUNG``) — earned resource-grounding rung.

Fixtures are SYNTHETIC (the mechanism), never offline sims. Every SACRED
anti-case from the spec + every exhibit has its own assertion. The
kill-switch law is proven per-segment: flag OFF ⇒ byte-identical.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2 import naming_contract as nc

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── fixtures ─────────────────────────────────────────────────────────────


def _pf(slug: str, display: str, anchor_id: str | None = None,
        paths: list[str] | None = None) -> Feature:
    f = Feature(
        name=slug, display_name=display, layer="product",
        paths=paths or [], authors=["a"], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_NOW, health_score=100.0,
    )
    if anchor_id:
        f.anchor_id = anchor_id
    return f


def _uf(uid: str, name: str, pfid: str, *, resource: str = "",
        members: list[str] | None = None, synthesized: bool = False,
        intent: str = "manage", category: str = "interactive") -> UserFlow:
    return UserFlow(
        id=uid, name=name, resource=resource or pfid, domain=None,
        product_feature_id=pfid, intent=intent,
        member_flow_ids=members or [], member_count=len(members or []),
        synthesized=synthesized, category=category,
    )


class _FL:
    """Minimal flow stand-in (uuid + name + description + paths)."""

    def __init__(self, uuid: str, name: str, *, description: str = "",
                 paths: list[str] | None = None,
                 entry_point_file: str | None = None,
                 test_files: list[str] | None = None) -> None:
        self.uuid = uuid
        self.name = name
        self.description = description
        self.paths = paths or []
        self.entry_point_file = entry_point_file
        self.test_files = test_files or []


@pytest.fixture(autouse=True)
def _clean_degrime_env(monkeypatch):
    """Every test starts with both B50 flags UNSET (default OFF)."""
    monkeypatch.delenv(nc.UF_NAME_DEGRIME_ENV, raising=False)
    monkeypatch.delenv("FAULTLINE_UF_RESOURCE_RUNG", raising=False)
    yield


def _vocab():
    return nc.load_naming_vocab()


# ══════════════════════════════════════════════════════════════════════════
# Segment 1 — echo discriminator
# ══════════════════════════════════════════════════════════════════════════


class TestSeg1EchoTokens:
    """The structural echo rule at the token level."""

    @pytest.mark.parametrize("tokens,expected", [
        (["ingest", "ingest"], ["ingest"]),          # Ingest ingest
        (["view", "views"], ["view"]),               # View views
        (["case", "case"], ["case"]),                # case case
        (["conversation", "conversation"], ["conversation"]),
        (["control", "control"], ["control"]),
        (["tool", "tool"], ["tool"]),
        (["chat", "chatids"], ["chat"]),             # glued tail-echo (b)
        (["team", "teamids"], ["team"]),
        (["document", "documentids"], ["document"]),
        (["dashboard", "dashboardids"], ["dashboard"]),
        (["workflow", "workflowids"], ["workflow"]),
        (["case", "caseid"], ["case"]),
    ])
    def test_echo_collapses(self, tokens, expected):
        assert nc._deglue_echo_tokens(tokens) == expected

    def test_short_core_never_collapses(self):
        # core < 3 chars is not a structural noun dup.
        assert nc._deglue_echo_tokens(["ab", "ab"]) == ["ab", "ab"]


class TestSeg1SacredAntiCases:

    def test_b46_one_token_char_prefix_is_linguistic(self):
        # 'auth-authorize' must NEVER strip to 'orize' — a partial character
        # prefix is linguistic, not a structural echo.
        assert nc._deglue_echo_tokens(["auth", "authorize"]) == [
            "auth", "authorize"]
        assert nc._deglue_echo_tokens(["auth", "authorizes"]) == [
            "auth", "authorizes"]
        # The pair is left intact — never re-derived to a stripped 'orize'.
        assert nc._degrime_display("auth authorize") == "auth authorize"
        assert nc._deglue_echo_tokens(["auth", "authorize"]) != ["orize"]

    def test_two_different_tokens_survive(self):
        # 'Ingest ingest' -> 'Ingest' ONLY on a real glued dup; two genuinely
        # different tokens after normalization must remain.
        assert nc._deglue_echo_tokens(["ingest", "data"]) == ["ingest", "data"]
        assert nc._degrime_display("Ingest data") == "Ingest data"

    def test_crm_object_names_untouched(self):
        # twenty 'Companies' / 'People' — single-token product surfaces.
        assert nc._degrime_display("Companies") == "Companies"
        assert nc._degrime_display("People") == "People"


class TestSeg1DisplayString:
    """String-level de-grime preserves connectives + parens."""

    @pytest.mark.parametrize("text,expected", [
        ("Ingest ingest", "Ingest"),
        ("View views", "View"),
        ("Send send test email", "Send test email"),
        # '&' preserved; dup 'case' collapses and the trailing standalone
        # 'ids' addressing token drops (Seg2 composes into _degrime_words).
        ("Browse & filter API case case ids", "Browse & filter API case"),
        ("Teams (Team)", "Teams"),
    ])
    def test_display(self, text, expected):
        assert nc._degrime_display(text) == expected


class TestSeg1QualifierEcho:

    @pytest.mark.parametrize("base,qual,echoes", [
        ("Teams", "Team", True),
        ("Manage links", "link", True),
        ("Manage links", "links", True),
        ("Manage settings", "Settings", True),
        ("Manage tRPC", "tRPC", True),
        ("Manage links", "file", False),          # distinguishing — keep
        ("Manage settings", "API keys", False),   # distinguishing — keep
        ("Manage links", "Datarooms", False),
    ])
    def test_qualifier_echoes_base(self, base, qual, echoes):
        assert nc._qualifier_echoes_base(base, qual) is echoes


class TestSeg1ResourcePhrase:

    def test_flag_on_deglues(self, monkeypatch):
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        # dup 'case' collapses; trailing standalone 'ids' drops (Seg2).
        assert nc._resource_phrase("API case case ids", _vocab()) == "API case"

    def test_flag_off_byte_identical(self):
        # Default OFF ⇒ the resource phrase is unchanged (the doubled 'case'
        # survives; 'API' keeps its acronym casing per the existing rubric).
        assert nc._resource_phrase("API case case ids", _vocab()) == (
            "API case case ids")


class TestSeg1Candidates:
    """The current-name discriminator re-derives an echo name."""

    def test_echo_current_yields_to_template(self, monkeypatch):
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        pf = _pf("api-cases", "API cases", anchor_id="route:api/cases")
        uf = _uf("UF-1", "Manage API case case ids", "api-cases",
                 resource="api-case-case-id")
        cands = nc.build_uf_candidates(
            uf, pf, _vocab(), member_flow_names=["create-case-flow"])
        assert cands[0] == "Manage API cases"
        assert "Manage API case case ids" not in cands[:1]

    def test_template_verb_echo_collapses(self, monkeypatch):
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        pf = _pf("ingest", "Ingest", anchor_id="route:ingest")
        uf = _uf("UF-2", "Ingest ingest", "ingest", resource="ingest",
                 synthesized=True)
        cands = nc.build_uf_candidates(
            uf, pf, _vocab(), member_flow_names=["ingest-events-flow"])
        assert cands[0] == "Ingest"

    def test_flag_off_keeps_echo(self):
        pf = _pf("api-cases", "API cases", anchor_id="route:api/cases")
        uf = _uf("UF-1", "Manage API case case ids", "api-cases",
                 resource="api-case-case-id")
        cands = nc.build_uf_candidates(
            uf, pf, _vocab(), member_flow_names=["create-case-flow"])
        # Pre-B50: the echo current display leads (law-clean).
        assert cands[0] == "Manage API case case ids"


class TestSeg1LawAQualifierIntegration:
    """Full run: echo qualifier suppressed, distinguishing one kept."""

    def _run(self, degrime: bool, monkeypatch):
        if degrime:
            monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        pfs = [_pf("links", "Manage links", anchor_id="route:links")]
        ufs = [
            _uf("UF-1", "Manage links", "links", resource="download",
                members=["f1"]),
            _uf("UF-2", "Manage links", "links", resource="link",
                members=["f2"]),
            _uf("UF-3", "Manage links", "links", resource="file",
                members=["f3"]),
        ]
        flows = [
            _FL("f1", "update-download-flow"),
            _FL("f2", "update-link-flow"),
            _FL("f3", "update-file-flow"),
        ]
        nc.run_naming_contract(pfs, ufs, flows, keeper_on=False)
        return {u.id: u.name for u in ufs}

    def test_echo_qualifier_suppressed(self, monkeypatch):
        names = self._run(True, monkeypatch)
        # The 'Manage manage links' template echo is collapsed …
        assert names["UF-1"] == "Manage links"
        # … the '(link)' echo qualifier never appears …
        assert "(link)" not in names["UF-2"]
        # … but the distinguishing '(file)' qualifier is kept.
        assert names["UF-3"] == "Manage links (file)"

    def test_flag_off_preserves_pre_b50(self, monkeypatch):
        names = self._run(False, monkeypatch)
        # Pre-B50 output carries the template + qualifier echoes verbatim.
        assert names["UF-1"] == "Manage manage links"
        assert names["UF-3"] == "Manage manage links (file)"


class TestSeg1IdentityUntouched:
    """DISPLAY-ONLY: resource / product_feature_id / membership stable."""

    def test_identity_stable_under_degrime(self, monkeypatch):
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        pfs = [_pf("api-cases", "API cases", anchor_id="route:api/cases")]
        uf = _uf("UF-1", "Manage API case case ids", "api-cases",
                 resource="api-case-case-id", members=["f1"])
        before_res = uf.resource
        before_pfid = uf.product_feature_id
        before_members = list(uf.member_flow_ids)
        nc.run_naming_contract(
            pfs, [uf], [_FL("f1", "create-case-flow")], keeper_on=False)
        assert uf.resource == before_res
        assert uf.product_feature_id == before_pfid
        assert uf.member_flow_ids == before_members


# ══════════════════════════════════════════════════════════════════════════
# Segment 2 — raw-param display law
# ══════════════════════════════════════════════════════════════════════════


class TestSeg2DeparamWord:
    """Glyph-less ``<noun><addr-suffix>`` slug reduces to its noun core.

    Suffixes come from the FROZEN unambiguous subset {id, ids, url, uuid,
    guid, slug, pk} — NEVER the full vocab route_addressing_suffixes
    (executor ruling on the B46 lesson: linguistic ≠ structural)."""

    @pytest.mark.parametrize("word,core", [
        ("teamurl", "team"),        # from $teamUrl
        ("boardid", "board"),       # from boardId
        ("chatid", "chat"),
        ("chatids", "chat"),
        ("documentids", "document"),
        ("cardids", "card"),
        ("boardids", "board"),
        ("workflowids", "workflow"),
        ("teamid", "team"),
        ("linkid", "link"),
        ("dashboardids", "dashboard"),
        ("runids", "run"),
    ])
    def test_deparam(self, word, core):
        assert nc._deparam_word(word) == core

    def test_pure_addressing_token_untouched(self):
        assert nc._deparam_word("url") == "url"
        assert nc._deparam_word("id") == "id"

    @pytest.mark.parametrize("word", [
        # SACRED anti-cases (executor ruling): vocab addressing suffixes
        # like 'name'/'code'/'key' are real word-endings — the glyph-less
        # glued rule must NEVER truncate a linguistic compound.
        "username",     # NOT 'user' ('name' is vocab-only, not frozen subset)
        "filename",     # NOT 'file'
        "barcode",      # NOT 'bar'
        "webhook",      # ends in 'hook' — not a suffix at all
        "handle",       # product noun — 'handle' removed from frozen subset
        "handles",
    ])
    def test_linguistic_compounds_survive(self, word):
        assert nc._deparam_word(word) == word


class TestSeg2DeparamDisplay:

    @pytest.mark.parametrize("text,expected", [
        ("teamurl documents", "team documents"),        # $teamUrl leak
        ("boardid card cardids", "board card"),          # boardId + glued echo
        ("boardids", "board"),
        ("dashboard dashboardids", "dashboard"),
        ("Browse & filter API AI chat chatids", "Browse & filter API AI chat"),
        ("Manage API case id", "Manage API case"),       # standalone id drop
    ])
    def test_deparam_display(self, text, expected):
        assert nc._deparam_display(text) == expected

    def test_crm_names_untouched(self):
        # twenty CRM objects — no addressing suffix, no drop.
        assert nc._deparam_display("Companies") == "Companies"
        assert nc._deparam_display("People") == "People"

    def test_linguistic_compounds_untouched_in_display(self):
        # Executor anti-cases at display level — byte-identical.
        assert nc._deparam_display("Manage usernames") == "Manage usernames"
        assert nc._deparam_display(
            "Developer story webhook settings") == (
            "Developer story webhook settings")
        assert nc._deparam_display("Manage handles") == "Manage handles"


class TestSeg2CollisionSafePlan:
    """degrime_rename_plan — the pure two-phase collision-safe rename plan
    (kan forensics: a degrime rename must NEVER create a display dup)."""

    def test_kan_pair_both_skip(self):
        # Two rows mapping to the SAME target — skip BOTH (keep originals).
        plan = nc.degrime_rename_plan(
            {"UF-001": "boardids", "UF-012": "boardslugs"},
            {"UF-001": "Manage boards", "UF-012": "Manage boards"},
        )
        assert plan == set()

    def test_unique_target_applies(self):
        plan = nc.degrime_rename_plan(
            {"UF-001": "boardids", "UF-012": "Manage cards"},
            {"UF-001": "board"},
        )
        assert plan == {"UF-001"}

    def test_target_taken_by_existing_row_skips(self):
        # Target already worn by ANOTHER row's current name — skip.
        plan = nc.degrime_rename_plan(
            {"U1": "boardids", "U2": "board"},
            {"U1": "board"},
        )
        assert plan == set()

    def test_determinism_input_order_independent(self):
        cur_a = {"U1": "boardids", "U2": "boardslugs", "U3": "chat chatids"}
        prop_a = {"U1": "Manage boards", "U2": "Manage boards", "U3": "chat"}
        # Same maps, reversed insertion order.
        cur_b = dict(reversed(list(cur_a.items())))
        prop_b = dict(reversed(list(prop_a.items())))
        assert nc.degrime_rename_plan(cur_a, prop_a) == \
            nc.degrime_rename_plan(cur_b, prop_b) == {"U3"}
        # Idempotent — same verdict on a second run.
        assert nc.degrime_rename_plan(cur_a, prop_a) == {"U3"}


class TestSeg2CollisionSafeIntegration:
    """The kan defect end-to-end: degrime must never create a UF name dup."""

    def _kan_pair(self):
        pf = _pf("boards", "Boards", anchor_id="route:boards")
        uf1 = _uf("UF-001", "boardids", "boards", resource="boardid",
                  members=["f1"])
        uf2 = _uf("UF-012", "boardslugs", "boards", resource="boardslug",
                  members=["f2"])
        flows = [_FL("f1", "update-boardid-flow"),
                 _FL("f2", "update-boardslug-flow")]
        return pf, uf1, uf2, flows

    def test_kan_pair_names_unchanged_no_dup(self, monkeypatch):
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        pf, uf1, uf2, flows = self._kan_pair()
        tele = nc.run_naming_contract([pf], [uf1, uf2], flows, keeper_on=False)
        # Both deparam targets collide ('board') — BOTH keep their honest
        # old grime; distinct rows; uf-dup-names stays 0.
        assert uf1.name == "boardids"
        assert uf2.name == "boardslugs"
        assert uf1.name.lower() != uf2.name.lower()
        assert tele.get("uf_degrime_collision_skipped", 0) >= 2

    def test_non_colliding_sibling_still_degrimes(self, monkeypatch):
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        pf = _pf("boards", "Boards", anchor_id="route:boards")
        uf1 = _uf("UF-001", "boardids", "boards", resource="boardid",
                  members=["f1"])
        uf2 = _uf("UF-012", "Manage cards", "boards", resource="card",
                  members=["f2"])
        flows = [_FL("f1", "update-boardid-flow"),
                 _FL("f2", "update-card-flow")]
        tele = nc.run_naming_contract([pf], [uf1, uf2], flows, keeper_on=False)
        # Unique target — the deparam applies normally.
        assert uf1.name == "board"
        assert uf2.name == "Manage cards"
        assert tele.get("uf_name_degrimed", 0) == 1

    def test_determinism_input_order(self, monkeypatch):
        # Same board, UF list order reversed ⇒ identical final names.
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        pf, uf1, uf2, flows = self._kan_pair()
        nc.run_naming_contract([pf], [uf1, uf2], flows, keeper_on=False)
        fwd = {uf1.id: uf1.name, uf2.id: uf2.name}
        pf_b, uf1_b, uf2_b, flows_b = self._kan_pair()
        nc.run_naming_contract(
            [pf_b], [uf2_b, uf1_b], flows_b, keeper_on=False)
        rev = {uf1_b.id: uf1_b.name, uf2_b.id: uf2_b.name}
        assert fwd == rev


class TestSeg2MintCollisionSafe:
    """Mint-side (_slot_consistent_label) degrime is param-driven so the
    cluster_user_flows caller can two-phase it (the kan root: 'boardid' and
    'boardslug' both deparam to 'board' pre-pluralise ⇒ identical labels)."""

    def _members(self, primary_name: str):
        return [{
            "name": primary_name,
            "entry_point_file": "app/boards/route.ts",
            "paths": ["app/boards/route.ts"],
        }]

    def test_degrime_false_is_pre_b50(self):
        from faultline.pipeline_v2.stage_6_7_user_flows import (
            _slot_consistent_label,
        )
        label, grounded = _slot_consistent_label(
            self._members("manage-boardid-flow"))
        assert (label, grounded) == ("boardids", True)

    def test_degrime_true_deparams_label(self):
        from faultline.pipeline_v2.stage_6_7_user_flows import (
            _slot_consistent_label,
        )
        label, grounded = _slot_consistent_label(
            self._members("manage-boardid-flow"), degrime=True)
        assert (label, grounded) == ("boards", True)
        # The kan twin resource maps to the SAME degrimed label — proof the
        # caller MUST collision-gate (both would mint 'Manage boards').
        label2, _g2 = _slot_consistent_label(
            self._members("manage-boardslug-flow"), degrime=True)
        assert label2 == "boards"

    def test_degrime_env_alone_does_not_change_function(self, monkeypatch):
        # The env flag no longer reaches inside the function — only the
        # caller's paired degrime=True call does (two-phase contract).
        from faultline.pipeline_v2.stage_6_7_user_flows import (
            _slot_consistent_label,
        )
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        label, _g = _slot_consistent_label(
            self._members("manage-boardid-flow"))
        assert label == "boardids"


class TestSeg2PurePFParam:
    """SACRED: '/p/$url' PF resolves to a member domain noun, never 'P'."""

    def test_url_pf_resolves_member_noun(self, monkeypatch):
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        paths = [
            "apps/remix/app/routes/(profile)/PublicProfilePage.tsx",
            "apps/remix/app/routes/(profile)/profile-card.tsx",
        ]
        pf = _pf("p.$url", "URL", anchor_id="route:p-url", paths=paths)
        cands = nc.build_pf_candidates(pf, _vocab())
        assert cands[0] == "Profile"
        assert "P" not in cands            # never the route letter
        assert "URL" != cands[0]

    def test_url_pf_selected_via_full_run(self, monkeypatch):
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        paths = ["apps/remix/app/routes/(profile)/PublicProfilePage.tsx"]
        pf = _pf("p.$url", "URL", anchor_id="route:p-url", paths=paths)
        nc.run_naming_contract([pf], [], [], keeper_on=False)
        assert pf.display_name == "Profile"

    def test_flag_off_keeps_url(self):
        paths = ["apps/remix/app/routes/(profile)/PublicProfilePage.tsx"]
        pf = _pf("p.$url", "URL", anchor_id="route:p-url", paths=paths)
        nc.run_naming_contract([pf], [], [], keeper_on=False)
        assert pf.display_name == "URL"     # pre-B50 byte-identical


class TestSeg2UFIntegration:

    def test_teamurl_leak_cleaned(self, monkeypatch):
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        pf = _pf("documents", "Documents", anchor_id="route:documents")
        uf = _uf("UF-1", "teamurl documents", "documents",
                 resource="teamurl-document-f-folderid", members=["f1"])
        nc.run_naming_contract(
            [pf], [uf], [_FL("f1", "browse-documents-flow")], keeper_on=False)
        assert uf.name == "team documents"
        assert "teamurl" not in uf.name

    def test_flag_off_keeps_teamurl(self):
        pf = _pf("documents", "Documents", anchor_id="route:documents")
        uf = _uf("UF-1", "teamurl documents", "documents",
                 resource="teamurl-document-f-folderid", members=["f1"])
        nc.run_naming_contract(
            [pf], [uf], [_FL("f1", "browse-documents-flow")], keeper_on=False)
        assert uf.name == "teamurl documents"   # pre-B50 byte-identical

    def test_identity_untouched(self, monkeypatch):
        monkeypatch.setenv(nc.UF_NAME_DEGRIME_ENV, "1")
        pf = _pf("documents", "Documents", anchor_id="route:documents")
        uf = _uf("UF-1", "teamurl documents", "documents",
                 resource="teamurl-document-f-folderid", members=["f1"])
        before = (uf.resource, uf.product_feature_id, list(uf.member_flow_ids))
        nc.run_naming_contract(
            [pf], [uf], [_FL("f1", "browse-documents-flow")], keeper_on=False)
        assert (uf.resource, uf.product_feature_id,
                list(uf.member_flow_ids)) == before
