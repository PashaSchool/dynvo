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
        ("Browse & filter API case case ids", "Browse & filter API case ids"),
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
        assert nc._resource_phrase("API case case ids", _vocab()) == (
            "API case ids")

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
