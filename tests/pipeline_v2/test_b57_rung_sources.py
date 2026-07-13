"""B57 Seg1 — Law C rung-source expansion (FAULTLINE_UF_RUNG_SOURCES_V2).

Four ADDITIONAL deterministic evidence sources for the EXISTING
resource/verb rungs — nav-cluster / i18n-key / route-method /
test-assert — each an OR-source at the UNCHANGED Law C bar (the B50
Seg3 precedent), each stamping a provenance tag into ``name_evidence``.

SACRED laws proven here:
  * flag OFF (default) ⇒ name / name_confidence / name_evidence
    byte-identical, no telemetry key;
  * i18n KEYS only — a translated VALUE (space-broken human copy) can
    NEVER ground (operator rule 2026-07-13);
  * member-less floor (``missing:members``) and the Law B narrowed floor
    (``missing:verb``) are untouchable;
  * a UF with no new match keeps its confidence EXACTLY (bar unchanged);
  * a GET route never grounds a 'Delete X' lead (families must agree);
  * a foreign PF's nav label never grounds;
  * UF NAMES are byte-stable under ON (confidence/evidence channel only);
  * an UNMAPPED test file (absent from ``flow.test_files``) is never
    read.

Fixtures are SYNTHETIC (the mechanism), never offline sims.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2 import naming_contract as nc

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

_FLAG = "FAULTLINE_UF_RUNG_SOURCES_V2"


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
        members: list[str] | None = None) -> UserFlow:
    return UserFlow(
        id=uid, name=name, resource=resource or pfid, domain=None,
        product_feature_id=pfid, intent="manage",
        member_flow_ids=members or [], member_count=len(members or []),
        synthesized=False, category="interactive",
    )


class _FL:
    """Minimal flow stand-in (uuid + name + paths + test_files)."""

    def __init__(self, uuid: str, name: str, *,
                 paths: list[str] | None = None,
                 entry_point_file: str | None = None,
                 test_files: list[str] | None = None) -> None:
        self.uuid = uuid
        self.name = name
        self.description = ""
        self.paths = paths or []
        self.entry_point_file = entry_point_file
        self.test_files = test_files or []


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with the B57 flag UNSET (default OFF) and the
    sibling display flags at their defaults."""
    monkeypatch.delenv(_FLAG, raising=False)
    monkeypatch.delenv("FAULTLINE_UF_RESOURCE_RUNG", raising=False)
    monkeypatch.delenv(nc.UF_NAME_DEGRIME_ENV, raising=False)
    yield


def _apply(ufs, pfs, flows, *, nav_sets=None, routes=None, repo=None,
           authored=frozenset()):
    vocab = nc.load_naming_vocab()
    pf_by_slug = {str(p.name): p for p in pfs}
    flow_name_by_id = {f.uuid: f.name for f in flows}
    flow_by_id = {f.uuid: f for f in flows}
    tele: dict = {}
    nc._apply_uf_name_laws(
        ufs, pf_by_slug, vocab, flow_name_by_id, tele,
        authored_ids=set(authored), keeper_on=False,
        nav_labels={}, flow_origin_by_id={}, flow_by_id=flow_by_id,
        nav_label_sets=nav_sets or {}, routes_index=routes,
        repo_root=repo)
    return tele


def _state(ufs):
    return [(u.name, u.name_confidence, u.name_evidence) for u in ufs]


# ── extractor units (pure text → evidence) ──────────────────────────────


class TestI18nKeyExtractor:
    """The structural KEY discriminator: identifier-shaped, NO spaces."""

    def test_reference_patterns(self):
        text = (
            "const a = t('billing_overview');\n"
            'const b = i18n.t("out_of_office");\n'
            "<Trans i18nKey='settings.profile.title' />\n"
            "i18nKey={'nav.sidebar-item'}\n"
            "const c = getTranslation('team:members.list');\n"
        )
        assert nc._i18n_keys_from_text(text) == [
            "billing_overview", "out_of_office",
            "settings.profile.title", "nav.sidebar-item",
            "team:members.list",
        ]

    def test_value_with_spaces_rejected(self):
        # A translated VALUE is human copy — space-broken — and is the
        # FORBIDDEN source (operator rule 2026-07-13). Even when it is
        # passed through a t() call it never becomes evidence.
        text = 't("Billing Overview Page"); t(\'A Human Sentence here\')'
        assert nc._i18n_keys_from_text(text) == []

    def test_word_tail_t_never_matches(self):
        # format( / at( / assert( — the 't(' lookbehind keeps word tails out.
        text = "format('billing'); at('overview'); getFmt('x')"
        assert nc._i18n_keys_from_text(text) == []

    def test_vue_dollar_t_matches(self):
        # Vue canonical reference forms (hoppscotch-class corpora):
        # template interpolation + options-API `this.$t`.
        text = (
            "{{ $t('request.duration') }}\n"
            'this.$t("workspace.new_collection")\n'
        )
        assert nc._i18n_keys_from_text(text) == [
            "request.duration", "workspace.new_collection",
        ]

    def test_dollar_t_word_tail_and_value_rejected(self):
        # foo$t( is an identifier tail, not a Vue i18n reference …
        assert nc._i18n_keys_from_text("foo$t('billing')") == []
        # … and a space-broken $t VALUE is still structurally rejected.
        assert nc._i18n_keys_from_text('$t("Billing Overview Page")') == []

    def test_plain_locale_json_values_never_match(self):
        # A locale catalog carries VALUES ('"title": "Billing Overview"')
        # — none of the reference patterns fire on it.
        text = '{"billing": {"title": "Billing Overview Page"}}'
        assert nc._i18n_keys_from_text(text) == []


class TestTestAssertionExtractor:

    def test_js_and_python_labels(self):
        text = (
            "it('creates a widget', () => {});\n"
            'test("renders the board", ...);\n'
            "describe.skip('Widget board', () => {});\n"
            "it.only(`updates a widget`, () => {});\n"
            "def test_delete_widget(self):\n"
            "def helper(x):\n"
        )
        assert nc._test_assertion_labels(text) == [
            "creates a widget", "renders the board", "Widget board",
            "updates a widget", "delete widget",
        ]


class TestNavLabelSets:
    """nav_label_sets_for_pfs — ALL voted labels per PF, beside (never
    instead of) the one-top-label nav_labels_for_pfs channel."""

    class _PS:
        nav_pairs_by_file = {
            "components/nav.tsx": [
                ("Reports", "/reports"),
                ("Reports", "/reports"),
                ("Boards", "/reports"),
            ],
        }

    _ROUTES = [{"pattern": "/reports", "method": "PAGE",
                "file": "app/reports/page.tsx"}]

    def _pfs(self):
        return [_pf("reports", "Reports", paths=["app/reports/page.tsx"])]

    def test_all_voted_labels_returned_sorted(self):
        out = nc.nav_label_sets_for_pfs(self._pfs(), self._PS(), self._ROUTES)
        assert out == {"reports": ["Boards", "Reports"]}

    def test_top_label_channel_unbroken(self):
        # The B40 single-label channel keeps its most-votes winner.
        out = nc.nav_labels_for_pfs(self._pfs(), self._PS(), self._ROUTES)
        assert out == {"reports": "Reports"}


# ── anti-case 1 + 4 — kill-switch / bar unchanged ────────────────────────


class TestKillSwitch:

    def _no_match_fixture(self, tmp_path):
        # Resource ungrounded low: member evidence names nothing the UF
        # name claims; the member file carries an i18n key that does NOT
        # match either.
        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "page.tsx").write_text(
            "const a = t('unrelated_thing');", encoding="utf-8")
        pfs = [_pf("gadgets", "Gadgets")]
        ufs = [_uf("UF-1", "Browse widgets", "gadgets", resource="widgets",
                   members=["f1"])]
        flows = [_FL("f1", "list-things-flow", paths=["app/page.tsx"])]
        nav = {"gadgets": ["Dashboards"]}
        routes = [{"pattern": "/x", "method": "POST", "file": "app/page.tsx"}]
        return ufs, pfs, flows, nav, routes

    def test_flag_off_no_telemetry_and_low(self, tmp_path):
        ufs, pfs, flows, nav, routes = self._no_match_fixture(tmp_path)
        tele = _apply(ufs, pfs, flows, nav_sets=nav, routes=routes,
                      repo=tmp_path)
        assert "rung_sources_v2_fired" not in tele    # scan_meta untouched
        assert ufs[0].name_confidence == "low"
        assert "missing:resource" in (ufs[0].name_evidence or [])

    def test_on_without_matches_identical_to_off(self, tmp_path, monkeypatch):
        # Anti-case 1 + 4 (Law C планка): the OFF run and an ON run with
        # ZERO matching sources produce byte-identical name / confidence /
        # evidence — the bar moved for nobody.
        ufs_off, pfs, flows, nav, routes = self._no_match_fixture(tmp_path)
        _apply(ufs_off, pfs, flows, nav_sets=nav, routes=routes,
               repo=tmp_path)
        monkeypatch.setenv(_FLAG, "1")
        ufs_on, pfs2, flows2, nav2, routes2 = self._no_match_fixture(tmp_path)
        tele_on = _apply(ufs_on, pfs2, flows2, nav_sets=nav2, routes=routes2,
                         repo=tmp_path)
        assert _state(ufs_on) == _state(ufs_off)
        assert tele_on["rung_sources_v2_fired"] == {
            "nav-cluster": 0, "i18n-key": 0, "route-verb": 0,
            "test-assert": 0,
        }

    def test_off_run_twice_identical(self, tmp_path):
        ufs_a, pfs, flows, nav, routes = self._no_match_fixture(tmp_path)
        _apply(ufs_a, pfs, flows, nav_sets=nav, routes=routes, repo=tmp_path)
        ufs_b, pfs2, flows2, nav2, routes2 = self._no_match_fixture(tmp_path)
        _apply(ufs_b, pfs2, flows2, nav_sets=nav2, routes=routes2,
               repo=tmp_path)
        assert _state(ufs_a) == _state(ufs_b)


# ── anti-case 2 — i18n KEY grounds, VALUE never ──────────────────────────


class TestI18nKeyRung:

    def _fixture(self, tmp_path, file_body: str):
        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "billing.tsx").write_text(
            file_body, encoding="utf-8")
        pfs = [_pf("money", "Money")]
        # verb grounded by the member (browse lead + list member);
        # resource 'billing' grounded by NOTHING but the i18n channel.
        ufs = [_uf("UF-1", "Browse billing", "money", resource="billing",
                   members=["f1"])]
        flows = [_FL("f1", "list-items-flow", paths=["app/billing.tsx"])]
        return ufs, pfs, flows

    def test_key_grounds_resource(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        ufs, pfs, flows = self._fixture(
            tmp_path,
            "const t1 = t('billing_overview');\n"
            'const copy = "Billing Overview Page";\n')
        tele = _apply(ufs, pfs, flows, repo=tmp_path)
        assert ufs[0].name_confidence == "high"
        assert "resource:i18n-key" in (ufs[0].name_evidence or [])
        assert tele["rung_sources_v2_fired"]["i18n-key"] == 1

    def test_value_never_grounds(self, tmp_path, monkeypatch):
        # The SAME phrase, but only as VALUES (a t()-wrapped human string
        # and a locale literal) — space-broken copy is rejected
        # structurally, so the resource stays ungrounded.
        monkeypatch.setenv(_FLAG, "1")
        ufs, pfs, flows = self._fixture(
            tmp_path,
            't("Billing Overview Page");\n'
            'const copy = "Billing Overview Page";\n')
        tele = _apply(ufs, pfs, flows, repo=tmp_path)
        assert ufs[0].name_confidence == "low"
        assert "missing:resource" in (ufs[0].name_evidence or [])
        assert tele["rung_sources_v2_fired"]["i18n-key"] == 0

    def test_key_verb_token_never_grounds_resource(self, tmp_path,
                                                   monkeypatch):
        # A key made ONLY of action verbs ('delete_remove') shares the
        # NAME's verb token but carries no resource noun — the resource
        # channel must not be grounded by a verb echo.
        monkeypatch.setenv(_FLAG, "1")
        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "x.tsx").write_text(
            "t('delete_remove');", encoding="utf-8")
        pfs = [_pf("stuff", "Stuff")]
        ufs = [_uf("UF-1", "Delete widgets", "stuff", resource="widgets",
                   members=["f1"])]
        flows = [_FL("f1", "run-thing-flow", paths=["app/x.tsx"])]
        _apply(ufs, pfs, flows, repo=tmp_path)
        assert ufs[0].name_confidence == "low"
        assert "resource:i18n-key" not in (ufs[0].name_evidence or [])

    def test_camel_and_namespace_keys_tokenize(self, tmp_path, monkeypatch):
        # settings.profileTitle → {setting, profile, title} — the
        # namespace + camelCase split reaches the UF resource tokens.
        monkeypatch.setenv(_FLAG, "1")
        ufs, pfs, flows = self._fixture(
            tmp_path, "getTranslation('money.billingOverview');")
        _apply(ufs, pfs, flows, repo=tmp_path)
        assert ufs[0].name_confidence == "high"
        assert "resource:i18n-key" in (ufs[0].name_evidence or [])


# ── anti-case 3 — floors untouchable ─────────────────────────────────────


class TestFloorsUntouched:

    def test_member_less_never_uplifts(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        pfs = [_pf("money", "Money")]
        ufs = [_uf("UF-1", "Browse billing", "money", resource="billing",
                   members=[])]
        nav = {"money": ["Billing"]}      # would match — must not be reached
        _apply(ufs, pfs, [], nav_sets=nav, repo=tmp_path)
        assert ufs[0].name_confidence == "low"
        assert ufs[0].name_evidence == ["missing:members"]

    def test_law_b_narrowed_floor_unchanged(self, tmp_path, monkeypatch):
        # A narrowed write-claim stays an honest low ('missing:verb') —
        # the rungs never resurrect the claimed verb.
        monkeypatch.setenv(_FLAG, "1")
        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "w.tsx").write_text(
            "t('widget_board');", encoding="utf-8")
        pfs = [_pf("gadgets", "Gadgets")]
        ufs = [_uf("UF-1", "Create widgets", "gadgets", resource="widgets",
                   members=["f1"])]
        flows = [_FL("f1", "view-widget-flow", paths=["app/w.tsx"])]
        _apply(ufs, pfs, flows, repo=tmp_path)
        assert ufs[0].name_confidence == "low"
        assert ufs[0].name_evidence == ["missing:verb"]


# ── anti-case 5 — route-method family agreement ──────────────────────────


class TestRouteVerbRung:

    def _fixture(self, method: str):
        pfs = [_pf("stuff", "Stuff")]
        # resource grounded by the member name ('widget'); verb NOT:
        # lead 'delete', member family 'act' (run) — no read-narrowing,
        # no base_verb.
        ufs = [_uf("UF-1", "Delete widgets", "stuff", resource="widgets",
                   members=["f1"])]
        flows = [_FL("f1", "run-widget-flow",
                     paths=["app/api/widgets/route.ts"])]
        routes = [{"pattern": "/api/widgets", "method": method,
                   "file": "app/api/widgets/route.ts"}]
        return ufs, pfs, flows, routes

    def test_get_never_grounds_delete_lead(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        ufs, pfs, flows, routes = self._fixture("GET")
        tele = _apply(ufs, pfs, flows, routes=routes)
        assert ufs[0].name_confidence == "medium"     # resource only
        assert "verb:route-method" not in (ufs[0].name_evidence or [])
        assert tele["rung_sources_v2_fired"]["route-verb"] == 0

    def test_delete_method_grounds_delete_lead(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        ufs, pfs, flows, routes = self._fixture("DELETE")
        tele = _apply(ufs, pfs, flows, routes=routes)
        assert ufs[0].name_confidence == "high"
        assert "verb:route-method" in (ufs[0].name_evidence or [])
        assert tele["rung_sources_v2_fired"]["route-verb"] == 1

    def test_page_pseudo_method_never_grounds(self, monkeypatch):
        # Filesystem 'PAGE' rows declare no author action — no family.
        monkeypatch.setenv(_FLAG, "1")
        ufs, pfs, flows, routes = self._fixture("PAGE")
        _apply(ufs, pfs, flows, routes=routes)
        assert ufs[0].name_confidence == "medium"

    def test_foreign_route_file_never_grounds(self, monkeypatch):
        # The DELETE route lives on a file NO member touches.
        monkeypatch.setenv(_FLAG, "1")
        ufs, pfs, flows, _ = self._fixture("DELETE")
        routes = [{"pattern": "/api/other", "method": "DELETE",
                   "file": "app/api/other/route.ts"}]
        _apply(ufs, pfs, flows, routes=routes)
        assert ufs[0].name_confidence == "medium"


# ── anti-case 6 — nav-cluster ownership ──────────────────────────────────


class TestNavClusterRung:

    def _fixture(self):
        pfs = [_pf("gadgets", "Gadgets")]
        # verb grounded (browse lead + list member); resource ungrounded.
        ufs = [_uf("UF-1", "Browse widgets", "gadgets", resource="widgets",
                   members=["f1"])]
        flows = [_FL("f1", "list-things-flow")]
        return ufs, pfs, flows

    def test_own_cluster_non_top_label_grounds(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        ufs, pfs, flows = self._fixture()
        nav = {"gadgets": ["Boards", "Widgets"]}     # 'Widgets' NOT top-voted
        tele = _apply(ufs, pfs, flows, nav_sets=nav)
        assert ufs[0].name_confidence == "high"
        assert "resource:nav-cluster" in (ufs[0].name_evidence or [])
        assert tele["rung_sources_v2_fired"]["nav-cluster"] == 1

    def test_foreign_cluster_label_never_grounds(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        ufs, pfs, flows = self._fixture()
        nav = {"gadgets": ["Dashboards"], "other-pf": ["Widgets"]}
        tele = _apply(ufs, pfs, flows, nav_sets=nav)
        assert ufs[0].name_confidence == "low"
        assert "resource:nav-cluster" not in (ufs[0].name_evidence or [])
        assert tele["rung_sources_v2_fired"]["nav-cluster"] == 0


# ── anti-case 8 + test-assert rung ───────────────────────────────────────


class TestTestAssertRung:

    def _repo(self, tmp_path, body: str):
        (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
        (tmp_path / "tests" / "gadget.spec.ts").write_text(
            body, encoding="utf-8")
        return tmp_path

    def test_assertion_grounds_resource(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        repo = self._repo(tmp_path, "it('renders the gadget list', ...);")
        pfs = [_pf("stuff", "Stuff")]
        ufs = [_uf("UF-1", "Browse gadgets", "stuff", resource="gadgets",
                   members=["f1"])]
        flows = [_FL("f1", "list-things-flow",
                     test_files=["tests/gadget.spec.ts"])]
        tele = _apply(ufs, pfs, flows, repo=repo)
        assert ufs[0].name_confidence == "high"
        assert "resource:test-assert" in (ufs[0].name_evidence or [])
        assert tele["rung_sources_v2_fired"]["test-assert"] == 1

    def test_assertion_lead_verb_grounds_family(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        repo = self._repo(tmp_path, "it('deletes the widget board', ...);")
        pfs = [_pf("stuff", "Stuff")]
        ufs = [_uf("UF-1", "Delete widgets", "stuff", resource="widgets",
                   members=["f1"])]
        flows = [_FL("f1", "run-widget-flow",
                     test_files=["tests/gadget.spec.ts"])]
        tele = _apply(ufs, pfs, flows, repo=repo)
        assert ufs[0].name_confidence == "high"
        assert "verb:test-assert" in (ufs[0].name_evidence or [])
        assert tele["rung_sources_v2_fired"]["test-assert"] == 1

    def test_wrong_family_assertion_never_grounds_verb(self, tmp_path,
                                                       monkeypatch):
        # 'renders …' (read family) can not stand in for a Delete lead.
        monkeypatch.setenv(_FLAG, "1")
        repo = self._repo(tmp_path, "it('renders the widget board', ...);")
        pfs = [_pf("stuff", "Stuff")]
        ufs = [_uf("UF-1", "Delete widgets", "stuff", resource="widgets",
                   members=["f1"])]
        flows = [_FL("f1", "run-widget-flow",
                     test_files=["tests/gadget.spec.ts"])]
        _apply(ufs, pfs, flows, repo=repo)
        assert ufs[0].name_confidence == "medium"     # resource via member
        assert "verb:test-assert" not in (ufs[0].name_evidence or [])

    def test_unmapped_test_file_never_read(self, tmp_path, monkeypatch):
        # Anti-case 8: the assertion file EXISTS in the repo and would
        # match — but it is absent from flow.test_files (no B36
        # member-overlap mapping), so the rung never reads it.
        monkeypatch.setenv(_FLAG, "1")
        repo = self._repo(tmp_path, "it('browses the gadget list', ...);")
        pfs = [_pf("stuff", "Stuff")]
        ufs = [_uf("UF-1", "Browse gadgets", "stuff", resource="gadgets",
                   members=["f1"])]
        flows = [_FL("f1", "list-things-flow", test_files=[])]
        tele = _apply(ufs, pfs, flows, repo=repo)
        assert ufs[0].name_confidence == "low"
        assert tele["rung_sources_v2_fired"]["test-assert"] == 0


# ── anti-case 7 — UF names byte-stable under ON ──────────────────────────


class TestNameStability:

    def _full_fixture(self, tmp_path):
        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "billing.tsx").write_text(
            "const t1 = t('billing_overview');", encoding="utf-8")
        pfs = [_pf("money", "Money", anchor_id="route:money")]
        ufs = [
            _uf("UF-1", "Browse billing", "money", resource="billing",
                members=["f1"]),
            _uf("UF-2", "Manage invoices", "money", resource="invoice",
                members=["f2"]),
        ]
        flows = [
            _FL("f1", "list-items-flow", paths=["app/billing.tsx"]),
            _FL("f2", "update-invoice-flow"),
        ]
        return pfs, ufs, flows

    def test_names_identical_off_vs_on(self, tmp_path, monkeypatch):
        # Full contract run — the flag may move CONFIDENCE only (B40 law).
        pfs_off, ufs_off, flows_off = self._full_fixture(tmp_path)
        nc.run_naming_contract(pfs_off, ufs_off, flows_off, keeper_on=False,
                               repo_root=tmp_path)
        names_off = [u.name for u in ufs_off]
        conf_off = [u.name_confidence for u in ufs_off]

        monkeypatch.setenv(_FLAG, "1")
        pfs_on, ufs_on, flows_on = self._full_fixture(tmp_path)
        nc.run_naming_contract(pfs_on, ufs_on, flows_on, keeper_on=False,
                               repo_root=tmp_path)
        assert [u.name for u in ufs_on] == names_off      # byte-stable names
        # … and the armed run genuinely uplifted UF-1 (proof ON was live).
        assert ufs_off[0].name_confidence == "low"
        assert ufs_on[0].name_confidence == "high"
        assert conf_off[1] == ufs_on[1].name_confidence   # no-match row equal

    def test_identity_untouched_under_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        pfs, ufs, flows = self._full_fixture(tmp_path)
        before = [(u.resource, u.product_feature_id, list(u.member_flow_ids))
                  for u in ufs]
        nc.run_naming_contract(pfs, ufs, flows, keeper_on=False,
                               repo_root=tmp_path)
        after = [(u.resource, u.product_feature_id, list(u.member_flow_ids))
                 for u in ufs]
        assert after == before


# ── determinism ──────────────────────────────────────────────────────────


class TestDeterminism:

    def test_uf_order_independent(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "billing.tsx").write_text(
            "t('billing_overview');", encoding="utf-8")

        def build():
            pfs = [_pf("money", "Money")]
            ufs = [
                _uf("UF-1", "Browse billing", "money", resource="billing",
                    members=["f1"]),
                _uf("UF-2", "Delete widgets", "money", resource="widgets",
                    members=["f2"]),
            ]
            flows = [
                _FL("f1", "list-items-flow", paths=["app/billing.tsx"]),
                _FL("f2", "run-widget-flow",
                    paths=["app/api/widgets/route.ts"]),
            ]
            routes = [{"pattern": "/api/widgets", "method": "DELETE",
                       "file": "app/api/widgets/route.ts"}]
            return pfs, ufs, flows, routes

        pfs, ufs, flows, routes = build()
        _apply(ufs, pfs, flows, routes=routes, repo=tmp_path)
        fwd = {u.id: (u.name_confidence, tuple(u.name_evidence or []))
               for u in ufs}
        pfs2, ufs2, flows2, routes2 = build()
        ufs2.reverse()
        _apply(ufs2, pfs2, flows2, routes=routes2, repo=tmp_path)
        rev = {u.id: (u.name_confidence, tuple(u.name_evidence or []))
               for u in ufs2}
        assert fwd == rev
