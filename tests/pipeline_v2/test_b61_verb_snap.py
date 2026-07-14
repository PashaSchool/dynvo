"""B61 Seg1 — evidence-born verb-snap (FAULTLINE_UF_VERB_SNAP).

A deterministic post-pass over the UF display channel: a name whose
LEADING verb's action-family is ABSENT from the member VERB-COMPOSITION
(B57 ``member_verb_composition`` — the HTTP-methods / page-kinds the
member flows structurally imply) has that lead verb REPLACED by the
canonical verb of the composition's DOMINANT family (mutation outranks
read). Runs BEFORE Law C so the snapped name is scored by the existing
``structural:verb-composition`` rung → earned high at $0.

SACRED laws proven here:
  * flag OFF (default) ⇒ name / name_confidence / name_evidence
    byte-identical, no ``uf_verb_snap`` telemetry key;
  * an EMPTY composition leaves the name UNCHANGED (no facts → no claim
    → honest ``missing:verb``);
  * a mutation verb is assigned ONLY over a mutation composition — a
    GET-only journey NEVER earns a create/update/delete name;
  * the resource remainder is preserved VERBATIM (only the lead token
    changes);
  * authored / pinned rows are exempt (snap never touches them);
  * a lead already grounded by the composition is left alone (no churn);
  * a generic lead ("Manage" — not a CRUD family) is never snapped
    (nothing to fold);
  * COLLISION-SAFE (B31) — two rows never snap to one display name;
  * the snap is IDEMPOTENT (a second pass is a no-op — the rescore seam);
  * the snapped verb ALWAYS folds back into the composition (earned-high
    by construction) and the result is display-law clean.

Fixtures are SYNTHETIC (the mechanism), never offline sims.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2 import naming_contract as nc

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_FLAG = "FAULTLINE_UF_VERB_SNAP"
_V2 = "FAULTLINE_UF_RUNG_SOURCES_V2"


# ── fixtures ─────────────────────────────────────────────────────────────


def _pf(slug: str, display: str, anchor_id: str | None = None) -> Feature:
    f = Feature(
        name=slug, display_name=display, layer="product",
        paths=[], authors=["a"], total_commits=1, bug_fixes=0,
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
    """Minimal flow stand-in (uuid + name + paths)."""

    def __init__(self, uuid: str, name: str, *,
                 paths: list[str] | None = None,
                 entry_point_file: str | None = None) -> None:
        self.uuid = uuid
        self.name = name
        self.description = ""
        self.paths = paths or []
        self.entry_point_file = entry_point_file
        self.test_files: list[str] = []


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with both flags UNSET (default OFF)."""
    monkeypatch.delenv(_FLAG, raising=False)
    monkeypatch.delenv(_V2, raising=False)
    monkeypatch.delenv(nc.UF_NAME_DEGRIME_ENV, raising=False)
    yield


def _apply(ufs, pfs, flows, routes, *, authored=frozenset(), keeper=False):
    vocab = nc.load_naming_vocab()
    pf_by_slug = {str(p.name): p for p in pfs}
    flow_name_by_id = {f.uuid: f.name for f in flows}
    flow_by_id = {f.uuid: f for f in flows}
    tele: dict = {}
    nc._apply_uf_name_laws(
        ufs, pf_by_slug, vocab, flow_name_by_id, tele,
        authored_ids=set(authored), keeper_on=keeper,
        nav_labels={}, flow_origin_by_id={}, flow_by_id=flow_by_id,
        nav_label_sets={}, routes_index=routes, repo_root=None)
    return tele


def _post(name: str, resource: str, uid: str = "UF-1") -> tuple:
    """A write-lead journey over a POST-only (mutation) composition."""
    f = _FL("f1", f"create {resource}", paths=[f"api/{resource}.ts"])
    r = [{"file": f"api/{resource}.ts", "method": "POST"}]
    u = _uf(uid, name, "wh", resource=resource, members=["f1"])
    return u, [_pf("wh", resource.title())], [f], r


# ── unit: dominant-family + lead-verb snap helpers ───────────────────────


class TestSnapHelpers:
    def test_dominant_mutation_outranks_read(self):
        assert nc._dominant_comp_family({"browse", "view", "create"}) == "create"
        assert nc._dominant_comp_family({"browse", "view", "update"}) == "update"
        assert nc._dominant_comp_family({"delete", "view"}) == "delete"

    def test_dominant_mutation_priority_order(self):
        # create > update > delete when several writes co-occur.
        assert nc._dominant_comp_family({"create", "update", "delete"}) == "create"
        assert nc._dominant_comp_family({"update", "delete"}) == "update"

    def test_dominant_read_when_no_mutation(self):
        assert nc._dominant_comp_family({"browse", "view"}) == "browse"

    def test_dominant_none_on_empty(self):
        assert nc._dominant_comp_family(set()) is None

    def test_snap_keeps_resource_and_folds_back(self):
        vocab = nc.load_naming_vocab()
        idx = nc._action_family_index(vocab)
        for name, fam, want_lead in [
            ("Delete webhooks", "create", "create"),
            ("Configure billing", "delete", "delete"),
            ("Remove members", "browse", "browse"),
        ]:
            snapped = nc._snap_lead_verb(name, fam, vocab)
            # resource remainder preserved verbatim (only lead token changed)
            assert snapped.split(None, 1)[1] == name.split(None, 1)[1]
            # snapped lead verb folds back into the target family
            assert nc._name_lead_family(snapped, idx) == want_lead


# ── SACRED: flag OFF byte-identical ──────────────────────────────────────


class TestFlagOff:
    def test_off_is_byte_identical(self, monkeypatch):
        monkeypatch.setenv(_V2, "1")  # rung on, snap OFF
        u, pfs, flows, r = _post("Delete webhooks", "webhooks")
        tele = _apply([u], pfs, flows, r)
        assert u.name == "Delete webhooks"          # name unchanged
        assert "uf_verb_snap" not in tele            # no telemetry key


# ── core: write-lead ∉ comp snaps + earns high via composition rung ──────


class TestCoreSnap:
    def test_post_only_snaps_to_create_and_earns_high(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        u, pfs, flows, r = _post("Delete webhooks", "webhooks")
        tele = _apply([u], pfs, flows, r)
        assert u.name == "Create webhooks"
        assert u.name_confidence == "high"
        assert "structural:verb-composition" in (u.name_evidence or [])
        snap = tele["uf_verb_snap"]
        assert snap["snapped"] == 1
        assert snap["families"] == {"create": 1}

    def test_patch_composition_snaps_to_update(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        f = _FL("f1", "edit profile", paths=["api/profile.ts"])
        r = [{"file": "api/profile.ts", "method": "PATCH"}]
        u = _uf("UF-1", "Delete profile", "wh", resource="profile", members=["f1"])
        tele = _apply([u], [_pf("wh", "Profile")], [f], r)
        assert u.name == "Update profile"
        assert u.name_confidence == "high"
        assert tele["uf_verb_snap"]["families"] == {"update": 1}


# ── SACRED anti-cases ────────────────────────────────────────────────────


class TestSacred:
    def test_empty_composition_leaves_name_unchanged(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        f = _FL("f1", "helper thing", paths=["lib/util.ts"])  # no route facts
        u = _uf("UF-1", "Delete webhooks", "wh", resource="webhooks",
                members=["f1"])
        tele = _apply([u], [_pf("wh", "Webhooks")], [f], [])
        assert u.name == "Delete webhooks"            # SACRED unchanged
        assert tele["uf_verb_snap"]["skipped_empty"] == 1
        assert tele["uf_verb_snap"]["snapped"] == 0

    def test_get_only_never_yields_mutation_name(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        # member verb "process" is unrecognized ⇒ Law B does NOT intercept;
        # the snap itself must refuse a mutation verb over a GET-only comp.
        f = _FL("f1", "process webhook", paths=["api/webhooks.ts"])
        r = [{"file": "api/webhooks.ts", "method": "GET"}]
        u = _uf("UF-1", "Delete webhooks", "wh", resource="webhooks",
                members=["f1"])
        _apply([u], [_pf("wh", "Webhooks")], [f], r)
        assert u.name == "Browse webhooks"
        assert not any(v in u.name for v in ("Create", "Update", "Delete"))

    def test_resource_part_preserved_verbatim(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        f = _FL("f1", "create api key", paths=["api/keys.ts"])
        r = [{"file": "api/keys.ts", "method": "POST"}]
        u = _uf("UF-1", "Delete api keys", "wh", resource="api keys",
                members=["f1"])
        _apply([u], [_pf("wh", "API Keys")], [f], r)
        # remainder tokens preserved verbatim (only the lead verb changed;
        # the mandated B50 chain applies canonical acronym casing — a display
        # polish, not a resource change: api → API).
        assert u.name.split(None, 1)[1].lower() == "api keys"
        assert u.name.split(None, 1)[0] == "Create"

    def test_authored_row_is_exempt(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        u, pfs, flows, r = _post("Delete webhooks", "webhooks")
        tele = _apply([u], pfs, flows, r, authored={"UF-1"})
        assert u.name == "Delete webhooks"            # authored untouched
        assert tele["uf_verb_snap"]["skipped_authored"] >= 1
        assert tele["uf_verb_snap"]["snapped"] == 0

    def test_pinned_row_is_exempt(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        u, pfs, flows, r = _post("Delete webhooks", "webhooks")
        u.identity = {"pinned_from": "prev-scan"}
        tele = _apply([u], pfs, flows, r, keeper=True)
        assert u.name == "Delete webhooks"            # pinned untouched
        assert tele["uf_verb_snap"]["snapped"] == 0

    def test_lead_already_grounded_no_change(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        # name already leads with create over a POST composition.
        u, pfs, flows, r = _post("Create webhooks", "webhooks")
        tele = _apply([u], pfs, flows, r)
        assert u.name == "Create webhooks"            # no churn
        assert tele["uf_verb_snap"]["snapped"] == 0

    def test_generic_manage_lead_not_snapped(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        # "Manage" is not a CRUD family ⇒ _name_lead_family is None ⇒ nothing
        # to fold; the generic lead is already grounded by any member action.
        u, pfs, flows, r = _post("Manage webhooks", "webhooks")
        tele = _apply([u], pfs, flows, r)
        assert u.name == "Manage webhooks"
        assert tele["uf_verb_snap"]["snapped"] == 0


# ── B31 collision-safety + idempotency ───────────────────────────────────


class TestCollisionAndIdempotency:
    def test_two_rows_snapping_to_one_name_both_kept(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        f = _FL("f1", "create alpha", paths=["api/alpha.ts"])
        r = [{"file": "api/alpha.ts", "method": "POST"}]
        # both lead with a write ∉ comp and would snap to "Create alpha".
        u1 = _uf("UF-1", "Delete alpha", "wh", resource="alpha", members=["f1"])
        u2 = _uf("UF-2", "Remove alpha", "wh", resource="alpha", members=["f1"])
        tele = _apply([u1, u2], [_pf("wh", "Alpha")], [f], r)
        assert u1.name == "Delete alpha"              # both keep their names
        assert u2.name == "Remove alpha"
        assert tele["uf_verb_snap"]["snapped"] == 0
        assert tele["uf_verb_snap"]["skipped_collision"] == 2

    def test_idempotent_second_pass_is_noop(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        u, pfs, flows, r = _post("Delete webhooks", "webhooks")
        _apply([u], pfs, flows, r)
        assert u.name == "Create webhooks"
        tele2 = _apply([u], pfs, flows, r)            # the rescore seam
        assert u.name == "Create webhooks"
        assert tele2["uf_verb_snap"]["snapped"] == 0  # no refire


# ── iter2: generic-lead composition grounding (same flag) ────────────────


class TestGenericCompositionGrounding:
    def test_manage_lead_grounds_from_composition(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        # 'Manage' folds to NO family; member flow names give no families
        # (mfams empty) — pre-iter2 this row was stuck missing:verb.
        f = _FL("f1", "process tag", paths=["api/tags.ts"])
        r = [{"file": "api/tags.ts", "method": "POST"}]
        u = _uf("UF-1", "Manage tags", "wh", resource="tags", members=["f1"])
        tele = _apply([u], [_pf("wh", "Tags")], [f], r)
        assert u.name == "Manage tags"                 # name NEVER touched
        assert u.name_confidence == "high"
        assert "structural:verb-composition-generic" in (u.name_evidence or [])
        assert tele["uf_verb_snap"]["generic_grounded"] == 1

    def test_editorial_lead_grounds_and_keeps_name(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        # 'Confirm' is not in the action-families vocab -> lead None.
        f = _FL("f1", "process request", paths=["api/email-change.ts"])
        r = [{"file": "api/email-change.ts", "method": "POST"}]
        u = _uf("UF-1", "Confirm email change", "wh",
                resource="email change", members=["f1"])
        _apply([u], [_pf("wh", "Email")], [f], r)
        assert u.name == "Confirm email change"        # editorial name kept
        assert u.name_confidence == "high"

    def test_empty_composition_stays_medium(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        f = _FL("f1", "helper thing", paths=["lib/util.ts"])
        u = _uf("UF-1", "Manage tags", "wh", resource="tags", members=["f1"])
        tele = _apply([u], [_pf("wh", "Tags")], [f], [])
        assert u.name_confidence == "medium"           # honest missing:verb
        assert "missing:verb" in (u.name_evidence or [])
        assert tele["uf_verb_snap"]["generic_grounded"] == 0

    def test_flag_off_no_generic_grounding(self, monkeypatch):
        monkeypatch.setenv(_V2, "1")                    # v2 on, snap OFF
        f = _FL("f1", "process tag", paths=["api/tags.ts"])
        r = [{"file": "api/tags.ts", "method": "POST"}]
        u = _uf("UF-1", "Manage tags", "wh", resource="tags", members=["f1"])
        tele = _apply([u], [_pf("wh", "Tags")], [f], r)
        assert u.name_confidence == "medium"           # byte-identical rubric
        assert "uf_verb_snap" not in tele

    def test_recognized_lead_never_takes_generic_rung(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        # 'Delete' lead over POST-only comp: the SNAP path owns it (renames
        # to Create); the generic rung must not fire for recognized leads.
        u, pfs, flows, r = _post("Delete webhooks", "webhooks")
        tele = _apply([u], pfs, flows, r)
        assert u.name == "Create webhooks"
        assert tele["uf_verb_snap"]["generic_grounded"] == 0
        assert "structural:verb-composition-generic" not in (u.name_evidence or [])


# ── iter3: never-worse mfams guard (real keyed-sample harm class) ────────


class TestMemberNamedGuard:
    def test_member_named_lead_never_snapped(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        # Page-only composition ({browse,view}) BUT a member flow NAME
        # witnesses the create lead ('create-dataroom-flow' class) — the
        # papermark 'Create and manage data rooms' harm case: the name is
        # TRUE, the composition merely under-represents it. Never snapped.
        f = _FL("f1", "create dataroom", paths=["app/datarooms/page.tsx"])
        r = [{"file": "app/datarooms/page.tsx", "method": "PAGE"}]
        u = _uf("UF-1", "Create and manage data rooms", "wh",
                resource="data rooms", members=["f1"])
        tele = _apply([u], [_pf("wh", "Data Rooms")], [f], r)
        assert u.name == "Create and manage data rooms"   # never-worse
        assert u.name_confidence == "high"                # base sources hold
        assert tele["uf_verb_snap"]["snapped"] == 0
        assert tele["uf_verb_snap"]["skipped_member_named"] == 1

    def test_unwitnessed_lead_still_snaps(self, monkeypatch):
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setenv(_V2, "1")
        # No member flow name witnesses 'configure' (update family), member
        # verbs are UNRECOGNIZED (mfams empty — else Law B's narrow floor
        # owns the row), and the composition is read-only -> the honest
        # born name is Browse.
        f = _FL("f1", "process branding", paths=["app/branding/page.tsx"])
        r = [{"file": "app/branding/page.tsx", "method": "PAGE"}]
        u = _uf("UF-1", "Configure custom branding", "wh",
                resource="custom branding", members=["f1"])
        tele = _apply([u], [_pf("wh", "Branding")], [f], r)
        assert u.name == "Browse custom branding"
        assert u.name_confidence == "high"
        assert tele["uf_verb_snap"]["snapped"] == 1
