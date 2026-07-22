"""B78 Seg H — dominant-evidence naming (FAULTLINE_DOMINANT_EVIDENCE_NAMING).

Named exhibits verbatim from the Soc0 cold board (B78 census):

  * UF-040 'Create, manage, and audit labels' — 'audit' backed by 1/11
    members (an audit-write rider) → 'Create and manage labels'.
  * UF-052 'Manage knowledges, orgs & files' — 'file' backed by 4/16
    members → the dominant resources ('Manage knowledges & orgs').

ANTI-CASES (must survive; the spec's survivors as assertions):

  * majority-supported tokens STAY — the medusa order-change class
    ('Create and manage order change actions') is untouched when its
    resource tokens carry family-level support;
  * a healthy composite of TWO true majority resources is untouched;
  * a journey ABOUT a side-effect family keeps its identity ('View audit
    logs' with resource='audit-log') — resource-grounded tokens are
    exempt;
  * flag unset ≡ explicit 0 ⇒ every site byte-identical (kill-switch).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from faultline.models.types import Flow, UserFlow
from faultline.pipeline_v2.data import load_yaml
from faultline.pipeline_v2.dominant_evidence import (
    DOMINANT_EVIDENCE_ENV,
    MEMBER_SUPPORT_FLOOR,
    dominant_evidence_naming_enabled,
    is_side_effect_flow,
    member_evidence_pairs,
    side_effect_verb_families,
    strip_display_tokens,
    unsupported_display_tokens,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _vocab() -> dict[str, Any]:
    return load_yaml("naming-contract-vocab.yaml")


# ── exhibit fixtures (Soc0 cold board, verbatim member sets) ────────────

_SOC0_LABELS_MEMBERS = [
    ("manage-labels-flow", "backend/routers/labels.py"),
    ("delete-label-flow", "backend/routers/labels.py"),
    ("audit-label-changes-flow", "backend/routers/labels.py"),
    ("browse-label-entities-flow", "backend/routers/labels.py"),
    ("search-label-entities-flow", "backend/routers/labels.py"),
    ("create-labels-create-activity-flow", "backend/routers/labels.py"),
    ("update-label-flow", "backend/routers/labels.py"),
    ("view-label-details-flow", "backend/routers/labels.py"),
    ("manage-labels-flow", "frontend/src/pages/LabelsPage.tsx"),
    ("preview-label-flow", "backend/routers/labels.py"),
    ("create-label-flow", "backend/routers/labels.py"),
]

_SOC0_KNOWLEDGE_MEMBERS = [
    ("browse-org-knowledge-revisions-flow", "backend/routers/org_knowledge.py"),
    ("access-knowledge-file-flow", "backend/routers/org_knowledge.py"),
    ("list-knowledge-entries-flow", "backend/routers/org_knowledge.py"),
    ("upload-knowledge-file-routers-flow", "backend/routers/org_knowledge.py"),
    ("manage-knowledge-entry-flow", "backend/routers/org_knowledge.py"),
    ("update-org-knowledge-my-preference-flow", "backend/routers/org_knowledge.py"),
    ("link-conversation-knowledge-flow", "backend/routers/org_knowledge.py"),
    ("create-knowledge-entry-routers-flow", "backend/routers/org_knowledge.py"),
    ("configure-knowledge-preference-flow", "backend/routers/org_knowledge.py"),
    ("delete-org-knowledge-by-entry-id-flow", "backend/routers/org_knowledge.py"),
    ("view-org-knowledge-file-text-flow", "backend/routers/org_knowledge.py"),
    ("extract-knowledge-from-conversation-flow", "backend/routers/org_knowledge.py"),
    ("create-org-knowledge-from-candidates-flow", "backend/routers/org_knowledge.py"),
    ("create-knowledge-from-message-flow", "backend/routers/org_knowledge.py"),
    ("remove-knowledge-file-flow", "backend/routers/org_knowledge.py"),
    ("create-org-knowledge-upload-flow", "backend/routers/org_knowledge.py"),
]


# ── flag law ────────────────────────────────────────────────────────────


def test_flag_default_off_unset_equals_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DOMINANT_EVIDENCE_ENV, raising=False)
    assert dominant_evidence_naming_enabled() is False
    monkeypatch.setenv(DOMINANT_EVIDENCE_ENV, "0")
    assert dominant_evidence_naming_enabled() is False
    monkeypatch.setenv(DOMINANT_EVIDENCE_ENV, "false")
    assert dominant_evidence_naming_enabled() is False
    monkeypatch.setenv(DOMINANT_EVIDENCE_ENV, "1")
    assert dominant_evidence_naming_enabled() is True


def test_support_floor_is_the_i15_ruler() -> None:
    """The floor is the validator's I15 attach floor reused (0.34), and it
    stays in lock-step with the lattice action-child mint copy."""
    from faultline.pipeline_v2.journey_lattice import _I15_ATTACH_FLOOR

    assert MEMBER_SUPPORT_FLOOR == 0.34 == _I15_ATTACH_FLOOR


# ── core ratio mechanism (named exhibits) ───────────────────────────────


def test_soc0_labels_exhibit_audit_1_of_11_stripped() -> None:
    """UF-040: 'audit' at 1/11 support (and side-effect-classed) drops;
    'Create, manage, and audit labels' → 'Create and manage labels'."""
    v = _vocab()
    drop = unsupported_display_tokens(
        "Create, manage, and audit labels", _SOC0_LABELS_MEMBERS,
        resource="label", vocab=v)
    assert drop == ["audit"]
    assert strip_display_tokens(
        "Create, manage, and audit labels", drop,
    ) == "Create and manage labels"


def test_soc0_knowledges_exhibit_reduces_to_dominant_resources() -> None:
    """UF-052: 'file' at 4/16 < 0.34 drops; the dominant resources
    (knowledge 16/16 via resource, org 16/16 via entry paths) stay."""
    v = _vocab()
    drop = unsupported_display_tokens(
        "Manage knowledges, orgs & files", _SOC0_KNOWLEDGE_MEMBERS,
        resource="knowledge", vocab=v)
    assert drop == ["file"]
    assert strip_display_tokens(
        "Manage knowledges, orgs & files", drop,
    ) == "Manage knowledges & orgs"


def test_anti_case_medusa_supported_composite_untouched() -> None:
    """'Create and manage order change actions' (medusa class): every
    resource token carries majority support ⇒ NOT touched."""
    v = _vocab()
    members = [
        ("create-order-change-flow",
         "packages/medusa/src/api/admin/order-changes/route.ts"),
        ("manage-order-change-actions-flow",
         "packages/medusa/src/api/admin/order-changes/route.ts"),
        ("update-order-change-actions-flow",
         "packages/medusa/src/api/admin/order-changes/route.ts"),
        ("delete-order-change-flow",
         "packages/medusa/src/api/admin/order-changes/route.ts"),
    ]
    assert unsupported_display_tokens(
        "Create and manage order change actions", members,
        resource="order-change", vocab=v) == []


def test_anti_case_two_true_majority_resources_untouched() -> None:
    """A healthy composite of two majority resources (posts 3/6,
    comments 3/6 — both ≥ 0.34) is never touched."""
    v = _vocab()
    members = [
        ("create-post-flow", "app/posts.py"),
        ("edit-post-flow", "app/posts.py"),
        ("delete-post-flow", "app/posts.py"),
        ("create-comment-flow", "app/comments.py"),
        ("edit-comment-flow", "app/comments.py"),
        ("moderate-comment-flow", "app/comments.py"),
    ]
    assert unsupported_display_tokens(
        "Manage posts & comments", members, resource="post", vocab=v) == []


def test_side_effect_family_never_gifts_even_at_high_share() -> None:
    """5/10 audit-write riders would pass a naive ratio (0.5 ≥ 0.34);
    the side-effect family exclusion still refuses the gift."""
    v = _vocab()
    members = (
        [("audit-change-flow", "backend/audit_log.py")] * 5
        + [("manage-user-flow", "backend/users.py")] * 5
    )
    drop = unsupported_display_tokens(
        "Manage and audit users", members, resource="user", vocab=v)
    assert drop == ["audit"]
    assert strip_display_tokens("Manage and audit users", drop) == "Manage users"


def test_anti_case_side_effect_journey_keeps_own_identity() -> None:
    """A journey ABOUT auditing keeps its words: resource-grounded tokens
    are exempt, so the family exclusion can never erase identity."""
    v = _vocab()
    members = [
        ("audit-request-flow", "backend/audit.py"),
        ("audit-change-flow", "backend/audit.py"),
        ("audit-export-flow", "backend/audit.py"),
    ]
    assert unsupported_display_tokens(
        "View audit logs", members, resource="audit-log", vocab=v) == []


def test_no_member_evidence_abstains() -> None:
    """The gate strips only on measured under-support — no members, no
    verdict (missing instrumentation must never rename)."""
    assert unsupported_display_tokens(
        "Create, manage, and audit labels", [], resource="label",
        vocab=_vocab()) == []


def test_strip_rejects_bare_verb_stump() -> None:
    """Dropping the only content word may not ship a verb stump — the
    caller must fall back to its deterministic channel."""
    assert strip_display_tokens(
        "Browse, filter, and manage cases", ["case"]) is None


def test_vocab_side_effect_families_closed_set() -> None:
    """Closed-set discipline + the YAML's own anti-cases: product verbs
    (track / record / monitor / trace) stay OUT of the families."""
    fams = side_effect_verb_families(_vocab())
    assert set(fams) == {"audit-write", "telemetry", "cache", "log"}
    all_toks = set().union(*fams.values())
    assert "audit" in all_toks and "log" in all_toks
    for product_verb in ("track", "record", "monitor", "trace"):
        assert product_verb not in all_toks
    assert is_side_effect_flow("audit-label-changes-flow", fams)
    assert not is_side_effect_flow("manage-labels-flow", fams)
    assert not is_side_effect_flow("track-shipment-flow", fams)


# ── site 1: refiner accept-gate ─────────────────────────────────────────


def _r_flow(name: str, entry: str) -> Flow:
    return Flow(
        name=name, uuid=name, paths=[entry], entry_point_file=entry,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def _r_uf(uid: str, name: str, members: list[str], *,
          resource: str = "label", domain: str = "label") -> UserFlow:
    return UserFlow(
        id=uid, name=name, domain=domain, product_feature_id=domain,
        intent="manage", resource=resource, member_flow_ids=members,
        member_count=len(members),
    )


class _FakeUsage:
    def __init__(self) -> None:
        self.input_tokens = 400
        self.output_tokens = 150


class _FakeMsg:
    def __init__(self, text: str) -> None:
        self.content = [type("B", (), {"text": text})()]
        self.usage = _FakeUsage()


def _client_returning(text: str) -> Any:
    class _Client:
        class _Messages:
            def create(self, **_kw: Any) -> Any:
                return _FakeMsg(text)

        messages = _Messages()

    return _Client()


def _labels_world() -> tuple[list[Flow], list[UserFlow], str]:
    flows = []
    seen: set[str] = set()
    members: list[str] = []
    for i, (nm, entry) in enumerate(_SOC0_LABELS_MEMBERS):
        uid = nm if nm not in seen else f"{nm}-{i}"
        seen.add(nm)
        fl = _r_flow(nm, entry)
        fl.uuid = uid
        flows.append(fl)
        members.append(uid)
    ufs = [_r_uf("UF-040", "Manage labels", members)]
    resp = json.dumps({"user_flows": [{
        "id": "UF-040",
        "name": "Create, manage, and audit labels",
        "description": "User manages labels.",
        "intent": "manage",
        "ui_tier": "full-page",
        "acceptance": [],
    }]})
    return flows, ufs, resp


def test_refiner_accept_gate_strips_smear_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows

    monkeypatch.setenv(DOMINANT_EVIDENCE_ENV, "1")
    flows, ufs, resp = _labels_world()
    out, tel = refine_user_flows(ufs, flows, client=_client_returning(resp))
    assert out[0].name == "Create and manage labels"
    assert out[0].name_confidence == "high"
    assert tel["uf_names_dominant_stripped"] == 1
    assert "uf_names_dominant_rejected" not in tel


def test_refiner_accept_gate_off_is_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows

    monkeypatch.delenv(DOMINANT_EVIDENCE_ENV, raising=False)
    flows, ufs, resp = _labels_world()
    out, tel = refine_user_flows(ufs, flows, client=_client_returning(resp))
    assert out[0].name == "Create, manage, and audit labels"
    assert "uf_names_dominant_stripped" not in tel
    assert "uf_names_dominant_rejected" not in tel


def test_refiner_unstrippable_composite_rejected_to_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A presence-passing name whose EVERY content token is under-supported
    (archive/report at 1/4 each) cannot be stripped into a lawful name —
    the row keeps its deterministic Stage-6.7 name via the existing
    ``name_ok=False`` channel and stamps low confidence."""
    from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows

    monkeypatch.setenv(DOMINANT_EVIDENCE_ENV, "1")
    flows = [
        _r_flow("manage-detector-flow", "backend/detectors.py"),
        _r_flow("edit-detector-flow", "backend/detectors.py"),
        _r_flow("view-detector-flow", "backend/detectors.py"),
        _r_flow("archive-report-flow", "backend/reports.py"),
    ]
    ufs = [_r_uf("UF-001", "Manage detectors",
                 [f.uuid for f in flows],
                 resource="detector", domain="detector")]
    resp = json.dumps({"user_flows": [{
        "id": "UF-001",
        "name": "Archive reports",
        "description": "d",
        "intent": "manage",
        "ui_tier": "no-ui",
        "acceptance": [],
    }]})
    out, tel = refine_user_flows(ufs, flows, client=_client_returning(resp))
    assert out[0].name == "Manage detectors"
    assert out[0].name_confidence == "low"
    assert tel["uf_names_dominant_rejected"] == 1


# ── site 2: lattice deterministic template ──────────────────────────────


def test_lattice_deterministic_name_gates_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from faultline.pipeline_v2.journey_lattice import _deterministic_name

    v = _vocab()
    ev = _SOC0_KNOWLEDGE_MEMBERS
    monkeypatch.setenv(DOMINANT_EVIDENCE_ENV, "1")
    name_on, _ = _deterministic_name(
        "manage", "org knowledge files", "Org Knowledge", v,
        member_evidence=ev)
    assert name_on == "Manage org knowledge"

    monkeypatch.delenv(DOMINANT_EVIDENCE_ENV, raising=False)
    name_off, _ = _deterministic_name(
        "manage", "org knowledge files", "Org Knowledge", v,
        member_evidence=ev)
    assert name_off == "Manage org knowledge files"
    # None-evidence call (the OFF caller shape) matches too
    name_none, _ = _deterministic_name(
        "manage", "org knowledge files", "Org Knowledge", v)
    assert name_none == name_off


# ── site 3: own-resource / generic template ─────────────────────────────


def _cand_uf() -> UserFlow:
    return UserFlow(
        id="UF-052", name="Knowledges, Orgs & Files", domain="org_knowledge",
        product_feature_id="network-security", intent="manage",
        resource="knowledge",
        member_flow_ids=[n for n, _ in _SOC0_KNOWLEDGE_MEMBERS[:6]],
        member_count=6, synthesized=True,
    )


def _cand_pf() -> Any:
    from faultline.models.types import Feature

    return Feature(
        name="network-security", display_name="Knowledges, Orgs & Files",
        layer="product", paths=[], authors=["a"], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=_NOW,
        health_score=100.0,
    )


def test_generic_template_gates_multi_resource_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from faultline.pipeline_v2.naming_contract import build_uf_candidates

    v = _vocab()
    members = [
        ("manage-knowledge-entry-flow", "backend/routers/org_knowledge.py"),
        ("create-knowledge-entry-flow", "backend/routers/org_knowledge.py"),
        ("delete-knowledge-entry-flow", "backend/routers/org_knowledge.py"),
        ("update-knowledge-entry-flow", "backend/routers/org_knowledge.py"),
        ("browse-knowledge-entries-flow", "backend/routers/org_knowledge.py"),
        ("upload-knowledge-file-flow", "backend/routers/org_knowledge.py"),
    ]
    names = [n for n, _ in members]

    monkeypatch.setenv(DOMINANT_EVIDENCE_ENV, "1")
    on = build_uf_candidates(
        _cand_uf(), _cand_pf(), v, names, member_evidence=members)
    # The TEMPLATE (leading — the row is a twin) is gated to the dominant
    # resources; the current display stays as a lower-ranked fallback
    # candidate (drop-only law gates what this site COMPOSES).
    assert on[0].lower().endswith("knowledges & orgs"), on
    assert "files" not in on[0].lower(), on

    monkeypatch.delenv(DOMINANT_EVIDENCE_ENV, raising=False)
    off = build_uf_candidates(
        _cand_uf(), _cand_pf(), v, names, member_evidence=members)
    off_none = build_uf_candidates(_cand_uf(), _cand_pf(), v, names)
    assert off == off_none
    assert any("knowledges, orgs & files" in c.lower() for c in off), off


# ── site 4: det-agg it5-1b qualifier-family join ────────────────────────


def _it51b_world() -> tuple[UserFlow, dict[str, Any]]:
    """The Soc0 keyless exhibit shape ('Manage org knowledges, entries &
    froms'): 16 org-knowledge members where the 'entry' family holds 4/16
    (0.25) and the 'from' family 3/16 (0.19) — both under the 0.34 floor."""
    names = (
        [f"view-org-knowledge-file-{i}-flow" for i in range(5)]
        + [f"browse-org-knowledge-revision-{i}-flow" for i in range(4)]
        + [f"create-org-knowledge-entry-{i}-flow" for i in range(4)]
        + [f"create-org-knowledge-from-message-{i}-flow" for i in range(3)]
    )
    flows = [_r_flow(n, "backend/routers/org_knowledge.py") for n in names]
    uf = UserFlow(
        id="UF-036", name="Manage org knowledges", domain="org_knowledge",
        product_feature_id="network-security", intent="manage",
        resource="api-org-knowledge-file-by-file-id-download",
        member_flow_ids=[f.uuid for f in flows], member_count=len(flows),
    )
    return uf, {f.uuid: f for f in flows}


def test_det_agg_qualifier_families_respect_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Site 4 (det-agg it5-1b): a qualifier family joins the display only
    with member support ≥ 0.34 — the 2-of-16 tails ('entries & froms'
    class) stop titling the journey."""
    from faultline.pipeline_v2.naming_contract import (
        _verb_class_tokens,
        load_naming_vocab,
    )
    from faultline.pipeline_v2.stage_6_7a_det_aggregation import (
        rename_raw_resource_rows,
    )

    verbs = _verb_class_tokens(load_naming_vocab())

    monkeypatch.setenv(DOMINANT_EVIDENCE_ENV, "1")
    uf, fbid = _it51b_world()
    tele = rename_raw_resource_rows([uf], fbid, verbs)
    assert tele["renamed"] == 1
    low = uf.name.lower()
    assert "from" not in low and "entrie" not in low and "entry" not in low, uf.name
    assert low.startswith("manage org knowledge"), uf.name

    # OFF: byte-identical to the banked composer (tails join as before)
    monkeypatch.delenv(DOMINANT_EVIDENCE_ENV, raising=False)
    uf2, fbid2 = _it51b_world()
    rename_raw_resource_rows([uf2], fbid2, verbs)
    assert "&" in uf2.name, uf2.name


def test_det_agg_majority_family_still_joins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anti-case: a qualifier family with true majority support (≥ 0.34)
    KEEPS its place in the composed display."""
    from faultline.pipeline_v2.naming_contract import (
        _verb_class_tokens,
        load_naming_vocab,
    )
    from faultline.pipeline_v2.stage_6_7a_det_aggregation import (
        rename_raw_resource_rows,
    )

    verbs = _verb_class_tokens(load_naming_vocab())
    names = (
        [f"create-threat-hunts-feed-poll-{i}-flow" for i in range(5)]
        + [f"browse-threat-hunts-feeds-{i}-flow" for i in range(4)]
        + [f"browse-threat-hunts-articles-{i}-flow" for i in range(6)]
        + [f"create-threat-hunts-hunt-run-{i}-flow" for i in range(2)]
    )
    flows = [_r_flow(n, "backend/routers/threat_hunts.py") for n in names]
    uf = UserFlow(
        id="UF-058", name="Create detectors", domain="threat_hunt",
        product_feature_id="threat-hunts", intent="manage",
        resource="api-threat-hunt-article-article-id-adopt-detector",
        member_flow_ids=[f.uuid for f in flows], member_count=len(flows),
    )
    monkeypatch.setenv(DOMINANT_EVIDENCE_ENV, "1")
    rename_raw_resource_rows([uf], {f.uuid: f for f in flows}, verbs)
    # feeds 9/17 (0.53) and articles 6/17 (0.35) both clear the floor
    assert uf.name == "Manage threat hunts, feeds & articles", uf.name


# ── member_evidence_pairs helper ────────────────────────────────────────


def test_member_evidence_pairs_reads_flow_objects() -> None:
    fl = _r_flow("manage-labels-flow", "backend/routers/labels.py")
    assert member_evidence_pairs([fl]) == [
        ("manage-labels-flow", "backend/routers/labels.py"),
    ]
