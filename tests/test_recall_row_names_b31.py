"""Bug B31 — distinct display names for synthesized recall rows.

Covers, per the fix contract (FAULTLINE_RECALL_ROW_NAMES, default ON):
  * dup group → distinct names: the wave-14 documenso 'Manage tRPC' ×7 /
    supabase 'Manage projects' ×3 shapes resolve to per-board-unique
    displays derived from each row's OWN (authored label | intent+resource |
    route-terminal) evidence;
  * authored-first: a carried maintainer label is restored verbatim; a
    NON-generic (non-colliding) authored/current label is never touched;
  * organic journeys are never renamed (papermark shape: the organic
    mc=17 row keeps its name; only the synth twin moves);
  * markers stay gap-band-eligible STRUCTURALLY — ``is_coverage_marker`` /
    ``synthesis_reason`` untouched by the rename, and the flag is stamped
    by ``honest_coverage_markers`` independent of any name prefix;
  * kill-switch =0 restores today's names byte-identically (no renames,
    no new scan_meta key);
  * uniqueness by construction — identical-evidence twins fall through the
    ladder deterministically (route terminal → PF qualifier → honest keep,
    NEVER a numeric suffix);
  * dict duck-typing (offline validator-sim): dict rows rename the same
    way and never grow plumbing keys;
  * determinism + idempotency (second run is a no-op).
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

from faultline.models.types import UserFlow
from faultline.pipeline_v2.synth_quality import (
    BACKSTOP_REASON,
    E2E_RECALL_REASON,
    MARKER_SURFACE_COORDS_ENV,
    RECALL_QUAL_CASING_ENV,
    RECALL_ROW_NAMES_ENV,
    ROUTE_GROUP_REASON,
    distinct_recall_row_names,
    honest_coverage_markers,
    recall_qual_casing_enabled,
    recall_row_names_enabled,
    run_synth_quality,
)

# ── fixture builders (duck-typed namespaces — the helpers use getattr) ──


def _uf(uid, name, *, reason=None, synthesized=True, pf=None,
        resource=None, intent=None, routes=(), authored=None,
        members=(), marker=False, name_confidence="low"):
    return SimpleNamespace(
        id=uid, name=name, synthesis_reason=reason, synthesized=synthesized,
        product_feature_id=pf, resource=resource, intent=intent,
        routes=list(routes), authored_label=authored,
        member_flow_ids=list(members), member_count=len(members),
        is_coverage_marker=marker, name_confidence=name_confidence,
        category="interactive", trigger=None, surface_candidate_files=None,
        surface_files=None, loc=None,
    )


def _pf(key, display):
    return SimpleNamespace(id=key, name=key, display_name=display,
                           member_files=[])


def _names(ufs):
    return [u.name if not isinstance(u, dict) else u["name"] for u in ufs]


def _assert_unique(ufs):
    names = [str(n).strip().lower() for n in _names(ufs)]
    assert len(names) == len(set(names)), f"collision survived: {names}"


# ── dup group → distinct names ───────────────────────────────────────────────


def test_documenso_trpc_shape_goes_distinct_via_authored_labels():
    """The wave-14 'Manage tRPC' ×7 marker family: carried authored labels
    are restored (authored-first), names become per-board unique."""
    pfs = [_pf("trpc", "tRPC")]
    ufs = [
        _uf("UF-023", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", routes=["/sign/:param"],
            authored="Document Auth", marker=True),
        _uf("UF-024", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="export", routes=["/sign/:param"],
            authored="Download Envelope Images", marker=True),
        _uf("UF-025", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", routes=["/sign/:param"],
            authored="Signing Certificate Tests", marker=True),
        _uf("UF-026", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="manage", routes=["/sign/:param"],
            authored="Next Recipient Dictation", marker=True),
        _uf("UF-031", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="browse",
            routes=["/embed/direct/:param", "/sign/:param"],
            authored="PDF Viewer Rendering", marker=True),
        _uf("UF-033", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute",
            routes=["/sign/:param", "/sign/:param/complete"],
            authored="Field Placement Visual Regression", marker=True),
        _uf("UF-035", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="team", intent="author", routes=["/sign/:param"],
            authored="Teams", marker=True),
    ]
    sm: dict = {}
    tele = distinct_recall_row_names(ufs, pfs, sm)
    assert tele["renamed"] == 7
    assert tele["residual_collisions"] == 0
    _assert_unique(ufs)
    got = {u.id: u.name for u in ufs}
    # authored labels restored verbatim (rung 1)
    assert got["UF-023"] == "Document Auth"
    assert got["UF-024"] == "Download Envelope Images"
    assert got["UF-031"] == "PDF Viewer Rendering"
    assert got["UF-035"] == "Teams"
    # telemetry records every rename once (census law)
    recs = sm["synth_quality"]["recall_row_names"]["renamed"]
    assert len(recs) == 7
    assert all(r["before"] == "Manage tRPC" for r in recs)


def test_route_group_shape_composes_from_intent_and_resource():
    """supabase 'Manage projects' ×3 (real UFs, sub-family b): no authored
    label → deterministic ``<intent-verb> <resource>`` composition."""
    pfs = [_pf("projects", "Projects")]
    ufs = [
        _uf("UF-092", "Manage projects", reason=ROUTE_GROUP_REASON,
            pf="projects", resource="account", intent="browse",
            members=("f1", "f2")),
        _uf("UF-103", "Manage projects", reason=ROUTE_GROUP_REASON,
            pf="projects", resource="editor", intent="browse",
            members=("f3",)),
        _uf("UF-109", "Manage projects", reason=ROUTE_GROUP_REASON,
            pf="projects", resource="integrations", intent="browse",
            members=("f4",)),
    ]
    tele = distinct_recall_row_names(ufs, pfs, {})
    assert tele["renamed"] == 3
    _assert_unique(ufs)
    assert {u.name for u in ufs} == {
        "Browse & filter account",
        "Browse & filter editor",
        "Browse & filter integrations",
    }


def test_intent_other_composes_broad_manage_and_may_readopt():
    """'other' maps to the broad Manage verdict; when the group is all-synth
    the base name is vacated and the first row may honestly re-adopt it."""
    pfs = [_pf("replication", "Replication")]
    ufs = [
        _uf("UF-068", "Manage replication", reason=BACKSTOP_REASON,
            pf="replication", resource="replication", intent="other",
            members=("f1",)),
        _uf("UF-120", "Manage replication", reason=ROUTE_GROUP_REASON,
            pf="replication", resource="replication", intent="browse",
            members=("f2",)),
    ]
    tele = distinct_recall_row_names(ufs, pfs, {})
    _assert_unique(ufs)
    assert ufs[0].name == "Manage replication"      # re-adopted (kept)
    assert ufs[1].name == "Browse & filter replication"
    assert tele["kept"] == 1
    assert tele["renamed"] == 1


# ── authored-first / never-touch guarantees ──────────────────────────────────


def test_non_colliding_authored_label_preserved_verbatim():
    """A non-generic (unique) recall-row name is NEVER rewritten — the pass
    fires on collision only."""
    pfs = [_pf("team", "Team")]
    ufs = [
        _uf("UF-001", "Bulk Actions", reason=E2E_RECALL_REASON, pf="team",
            resource="documents", intent="bulk", authored="Bulk Actions",
            marker=True),
        _uf("UF-002", "Public Profile", reason=E2E_RECALL_REASON, pf="team",
            resource="public-profile", intent="author",
            authored="Public Profile", marker=True),
        _uf("UF-003", "Manage organisations", reason=E2E_RECALL_REASON,
            pf="team", resource="member", intent="manage", marker=True),
    ]
    tele = distinct_recall_row_names(ufs, pfs, {})
    assert tele["renamed"] == 0
    assert _names(ufs) == [
        "Bulk Actions", "Public Profile", "Manage organisations",
    ]


def test_cross_pf_authored_twins_qualify_by_pf_display():
    """Two maintainer journeys named 'Teams' on different PFs: the later id
    keeps the authored base + PF qualifier (rung 2), never a number."""
    pfs = [_pf("team", "Team"), _pf("trpc", "tRPC")]
    ufs = [
        _uf("UF-001", "Teams", reason=E2E_RECALL_REASON, pf="team",
            resource="team", intent="manage", authored="Teams", marker=True),
        _uf("UF-002", "Teams", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="manage", authored="Teams",
            marker=True),
    ]
    distinct_recall_row_names(ufs, pfs, {})
    _assert_unique(ufs)
    assert ufs[0].name == "Teams"
    assert ufs[1].name == "Teams (tRPC)"


def test_organic_rows_never_renamed():
    """papermark shape: the organic mc=17 journey holds the name; only the
    synth route-group twin moves."""
    pfs = [_pf("documents", "Documents")]
    organic = _uf("UF-031", "Manage documents", reason=None,
                  synthesized=False, pf="documents", resource="document",
                  intent="manage", members=("f1",), name_confidence="high")
    synth = _uf("UF-056", "Manage documents", reason=ROUTE_GROUP_REASON,
                pf="documents", resource="account", intent="browse",
                members=("f2",))
    ufs = [organic, synth]
    tele = distinct_recall_row_names(ufs, pfs, {})
    assert organic.name == "Manage documents"
    assert synth.name == "Browse & filter account"
    assert tele["renamed"] == 1
    _assert_unique(ufs)


def test_organic_collision_without_recall_rows_untouched():
    """Two ORGANIC rows sharing a name are the naming contract's problem,
    not this pass's — zero writes."""
    pfs = [_pf("docs", "Docs")]
    ufs = [
        _uf("UF-001", "Manage docs", reason=None, synthesized=False,
            pf="docs", resource="a", intent="manage", members=("f1",)),
        _uf("UF-002", "Manage docs", reason=None, synthesized=False,
            pf="docs", resource="b", intent="browse", members=("f2",)),
    ]
    tele = distinct_recall_row_names(ufs, pfs, {})
    assert tele["renamed"] == 0
    assert _names(ufs) == ["Manage docs", "Manage docs"]


# ── uniqueness by construction (ladder tail) ─────────────────────────────────


def test_identical_evidence_twins_fall_to_route_terminal_then_pf():
    """Same (intent, resource), no authored label: route terminal breaks the
    tie; a third identical row falls to the PF qualifier; a fourth with no
    remaining rung keeps its name (honest residual, never a suffix number)."""
    pfs = [_pf("trpc", "tRPC")]
    ufs = [
        _uf("UF-001", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", routes=["/sign/:param"],
            marker=True),
        _uf("UF-002", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", routes=["/sign/:param"],
            marker=True),
        _uf("UF-003", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", routes=["/sign/:param"],
            marker=True),
        _uf("UF-004", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", routes=["/sign/:param"],
            marker=True),
    ]
    tele = distinct_recall_row_names(ufs, pfs, {})
    names = _names(ufs)
    assert names[0] == "Run waiting"                 # composed
    assert names[1] == "Run waiting (sign)"          # + route terminal
    assert names[2] == "Run waiting (tRPC)"          # + PF qualifier
    # 4th: composed rungs exhausted -> current name + route terminal
    assert names[3] == "Manage tRPC (sign)"
    assert tele["residual_collisions"] == 0
    _assert_unique(ufs)
    # NEVER a numeric suffix
    assert not any(n.rstrip(")").rstrip("0123456789").endswith("(")
                   for n in names)


def test_route_terminal_skips_params_globs_and_dialect_glyphs():
    pfs = [_pf("p", "P")]
    ufs = [
        _uf("UF-001", "Run waiting", reason=E2E_RECALL_REASON, pf="p",
            resource="waiting", intent="execute",
            routes=["/t/:param/documents/envelope_.*"], marker=True),
        _uf("UF-002", "Run waiting", reason=E2E_RECALL_REASON, pf="p",
            resource="waiting", intent="execute",
            routes=["/sign/:param/**", "/x/$team"], marker=True),
    ]
    distinct_recall_row_names(ufs, pfs, {})
    _assert_unique(ufs)
    for n in _names(ufs):
        assert ":" not in n and "*" not in n and "$" not in n


# ── structural marker contract (part 2) ──────────────────────────────────────


def test_marker_flag_is_name_independent_and_survives_rename():
    """Gap-band eligibility is STRUCTURAL: ``honest_coverage_markers`` stamps
    the flag with the journey-looking authored name kept (no 'Uncovered:'
    prefix), and the B31 rename preserves flag + reason + mc."""
    pfs = [_pf("trpc", "tRPC")]
    ufs = [
        _uf("UF-001", "Document Auth", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute",
            authored="Document Auth", marker=False),
        _uf("UF-002", "Document Auth", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="export",
            authored="Document Auth", marker=False),
    ]
    sm: dict = {}
    honest_coverage_markers(ufs, pfs, sm)
    assert all(u.is_coverage_marker for u in ufs)
    assert not any(str(u.name).startswith("Uncovered:") for u in ufs)
    distinct_recall_row_names(ufs, pfs, sm)
    _assert_unique(ufs)
    for u in ufs:
        assert u.is_coverage_marker is True          # flag untouched
        assert u.synthesis_reason == E2E_RECALL_REASON
        assert u.member_count == 0


def test_system_uncovered_rows_left_alone_when_unique():
    pfs = [_pf("auth", "Auth"), _pf("labels", "Labels")]
    ufs = [
        _uf("UF-001", "Uncovered: Auth routes", reason="system_flow_recall",
            pf="auth", resource="auth", intent="execute", marker=True),
        _uf("UF-002", "Uncovered: Labels routes",
            reason="system_flow_recall", pf="labels", resource="labels",
            intent="execute", marker=True),
    ]
    tele = distinct_recall_row_names(ufs, pfs, {})
    assert tele["renamed"] == 0


def test_b23_off_keeps_e2e_markers_on_b13_template(monkeypatch):
    """Under FAULTLINE_MARKER_SURFACE_COORDS=0 the e2e markers are back on
    the B13 'Uncovered: <PF> routes' regime — B31 must not fight it."""
    monkeypatch.setenv(MARKER_SURFACE_COORDS_ENV, "0")
    pfs = [_pf("team", "Team")]
    ufs = [
        _uf("UF-001", "Uncovered: Team routes", reason=E2E_RECALL_REASON,
            pf="team", resource="member", intent="manage",
            authored="Teams", marker=True),
        _uf("UF-002", "Uncovered: Team routes", reason=E2E_RECALL_REASON,
            pf="team", resource="public-profile", intent="author",
            authored="Public Profile", marker=True),
    ]
    tele = distinct_recall_row_names(ufs, pfs, {})
    assert tele["renamed"] == 0
    assert _names(ufs) == [
        "Uncovered: Team routes", "Uncovered: Team routes",
    ]


# ── kill-switch / flag plumbing ──────────────────────────────────────────────


def test_flag_off_restores_names_byte_identically(monkeypatch):
    monkeypatch.setenv(RECALL_ROW_NAMES_ENV, "0")
    assert not recall_row_names_enabled()
    pfs = [_pf("trpc", "tRPC")]
    ufs = [
        _uf("UF-001", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", authored="Document Auth",
            marker=True),
        _uf("UF-002", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="export", authored="Teams",
            marker=True),
    ]
    sm: dict = {}
    tele = distinct_recall_row_names(ufs, pfs, sm)
    assert tele == {"renamed": 0}
    assert _names(ufs) == ["Manage tRPC", "Manage tRPC"]
    assert "synth_quality" not in sm                 # no new scan_meta key


def test_flag_default_is_on(monkeypatch):
    monkeypatch.delenv(RECALL_ROW_NAMES_ENV, raising=False)
    assert recall_row_names_enabled()


def test_env_output_flag_registered():
    from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS
    assert "FAULTLINE_RECALL_ROW_NAMES" in ENV_OUTPUT_FLAGS


# ── B70: route-terminal parenthetical casing (FAULTLINE_RECALL_QUAL_CASING) ──


def _twin_quad():
    """Four identical-evidence twins whose ladder falls to the route
    terminal '(sign)' then the PF qualifier (see the twins test above)."""
    pfs = [_pf("trpc", "tRPC")]
    ufs = [
        _uf(f"UF-00{i}", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", routes=["/sign/:param"],
            marker=True)
        for i in range(1, 5)
    ]
    return ufs, pfs


def test_qual_casing_default_off_keeps_lowercase(monkeypatch):
    """Default OFF ⇒ the route-terminal qualifier stays lowercase, exactly as
    the B31 twins test pins it (byte-identical live output)."""
    monkeypatch.delenv(RECALL_QUAL_CASING_ENV, raising=False)
    assert not recall_qual_casing_enabled()
    ufs, pfs = _twin_quad()
    distinct_recall_row_names(ufs, pfs, {})
    names = _names(ufs)
    assert names[1] == "Run waiting (sign)"      # lowercase terminal, unchanged
    assert names[3] == "Manage tRPC (sign)"


def test_qual_casing_on_capitalizes_route_terminal(monkeypatch):
    """Armed ⇒ the route-terminal qualifier is capitalized to match the
    proper-cased PF qualifier; the composed base and the PF qualifier are
    untouched ('(tRPC)' keeps its brand casing)."""
    monkeypatch.setenv(RECALL_QUAL_CASING_ENV, "1")
    assert recall_qual_casing_enabled()
    ufs, pfs = _twin_quad()
    distinct_recall_row_names(ufs, pfs, {})
    names = _names(ufs)
    assert names[0] == "Run waiting"             # composed base untouched
    assert names[1] == "Run waiting (Sign)"      # terminal capitalized
    assert names[2] == "Run waiting (tRPC)"      # PF brand qualifier untouched
    assert names[3] == "Manage tRPC (Sign)"
    _assert_unique(ufs)


def test_qual_casing_env_flag_registered():
    from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS
    assert "FAULTLINE_RECALL_QUAL_CASING" in ENV_OUTPUT_FLAGS


def test_clean_board_adds_no_scan_meta_key():
    pfs = [_pf("a", "A")]
    ufs = [
        _uf("UF-001", "Browse & filter logs", reason=ROUTE_GROUP_REASON,
            pf="a", resource="logs", intent="browse", members=("f1",)),
        _uf("UF-002", "Manage logs", reason=ROUTE_GROUP_REASON, pf="a",
            resource="logs", intent="manage", members=("f2",)),
    ]
    sm: dict = {}
    tele = distinct_recall_row_names(ufs, pfs, sm)
    assert tele["renamed"] == 0
    assert sm == {}


# ── run_synth_quality wiring ─────────────────────────────────────────────────


def test_run_synth_quality_wires_recall_names():
    pfs = [_pf("projects", "Projects")]
    ufs = [
        _uf("UF-001", "Manage projects", reason=ROUTE_GROUP_REASON,
            pf="projects", resource="account", intent="browse",
            members=("f1",)),
        _uf("UF-002", "Manage projects", reason=ROUTE_GROUP_REASON,
            pf="projects", resource="editor", intent="browse",
            members=("f2",)),
    ]
    flows = [
        SimpleNamespace(uuid="f1", name="view-account-flow",
                        entry_point_file=None, paths=[], shared_paths=[],
                        loc_nodes=[], user_flow_id=None),
        SimpleNamespace(uuid="f2", name="view-editor-flow",
                        entry_point_file=None, paths=[], shared_paths=[],
                        loc_nodes=[], user_flow_id=None),
    ]
    sm: dict = {}
    tele = run_synth_quality(ufs, flows, pfs, sm, developer_features=[])
    assert tele["recall_rows_renamed"] == 2
    _assert_unique(ufs)


# ── dict duck-typing (offline validator-sim) ─────────────────────────────────


def test_dict_rows_rename_and_never_grow_plumbing_keys():
    pfs = [{"id": "trpc", "name": "trpc", "display_name": "tRPC",
            "member_files": []}]
    ufs = [
        {"id": "UF-001", "name": "Manage tRPC", "synthesized": True,
         "synthesis_reason": E2E_RECALL_REASON, "product_feature_id": "trpc",
         "resource": "waiting", "intent": "execute",
         "routes": ["/sign/:param"], "member_count": 0,
         "is_coverage_marker": True},
        {"id": "UF-002", "name": "Manage tRPC", "synthesized": True,
         "synthesis_reason": E2E_RECALL_REASON, "product_feature_id": "trpc",
         "resource": "waiting", "intent": "export",
         "routes": ["/sign/:param"], "member_count": 0,
         "is_coverage_marker": True},
        {"id": "UF-003", "name": "Organic row", "member_count": 2},
    ]
    keys_before = [set(u.keys()) for u in ufs]
    tele = distinct_recall_row_names(ufs, pfs, {})
    assert tele["renamed"] == 2
    _assert_unique(ufs)
    assert [set(u.keys()) for u in ufs] == keys_before   # no new keys
    assert ufs[2]["name"] == "Organic row"


# ── serializer / model plumbing ──────────────────────────────────────────────


def test_authored_label_never_serializes():
    uf = UserFlow(
        id="UF-001", name="Document Auth", intent="execute",
        resource="waiting", synthesized=True,
        synthesis_reason=E2E_RECALL_REASON, authored_label="Document Auth",
    )
    data = uf.model_dump()
    assert "authored_label" not in data
    assert "surface_candidate_files" not in data


def test_userflow_rehydrates_old_json_without_authored_label():
    old = {"id": "UF-001", "name": "X", "intent": "manage", "resource": "x"}
    uf = UserFlow(**old)
    assert uf.authored_label is None


def test_pass_clears_authored_label_plumbing():
    pfs = [_pf("trpc", "tRPC")]
    ufs = [
        _uf("UF-001", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", authored="Document Auth",
            marker=True),
        _uf("UF-002", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="export", authored="Teams",
            marker=True),
        _uf("UF-003", "Untouched unique row", reason=E2E_RECALL_REASON,
            pf="trpc", resource="team", intent="manage", authored="Solo",
            marker=True),
    ]
    distinct_recall_row_names(ufs, pfs, {})
    assert all(u.authored_label is None for u in ufs)


# ── determinism / idempotency ────────────────────────────────────────────────


def _shape():
    pfs = [_pf("trpc", "tRPC"), _pf("projects", "Projects")]
    ufs = [
        _uf("UF-001", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", routes=["/sign/:param"],
            authored="Document Auth", marker=True),
        _uf("UF-002", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="export", routes=["/sign/:param"],
            authored="Teams", marker=True),
        _uf("UF-003", "Manage projects", reason=ROUTE_GROUP_REASON,
            pf="projects", resource="editor", intent="browse",
            members=("f1",)),
        _uf("UF-004", "Manage projects", reason=ROUTE_GROUP_REASON,
            pf="projects", resource="account", intent="browse",
            members=("f2",)),
    ]
    return pfs, ufs


def test_double_run_deterministic():
    pfs1, ufs1 = _shape()
    pfs2, ufs2 = _shape()
    distinct_recall_row_names(ufs1, pfs1, {})
    distinct_recall_row_names(ufs2, pfs2, {})
    assert _names(ufs1) == _names(ufs2)


def test_rerun_on_renamed_output_is_noop():
    pfs, ufs = _shape()
    distinct_recall_row_names(ufs, pfs, {})
    first = copy.deepcopy(_names(ufs))
    tele = distinct_recall_row_names(ufs, pfs, {})
    assert tele["renamed"] == 0
    assert _names(ufs) == first


# ── B69-v2 — pf_display echo-guard on the qualifier rungs ────────────────────


def test_b69v2_pf_display_echo_qualifier_skipped_when_armed(monkeypatch):
    """The papermark-ON exhibit: two same-(intent,resource) links seeds,
    the second's OWN PF display 'Links' echoes the composed base — armed,
    the ladder refuses the tautology ('Browse & filter links (Links)') and
    keeps the current name honestly (residual collision, never a lie)."""
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    pfs = [_pf("datarooms", "Datarooms"), _pf("links", "Links")]
    ufs = [
        _uf("UF-001", "Manage links", reason=ROUTE_GROUP_REASON,
            pf="datarooms", resource="links", intent="browse",
            members=("f1",)),
        _uf("UF-002", "Manage links", reason=ROUTE_GROUP_REASON,
            pf="links", resource="links", intent="browse",
            members=("f2",)),
    ]
    tele = distinct_recall_row_names(ufs, pfs, {})
    names = _names(ufs)
    assert "Browse & filter links (Links)" not in names
    # the non-echoing qualifier ('Datarooms' on a links base) is still legal
    assert names[0] == "Browse & filter links"
    assert names[1] in {"Manage links", "Manage links (Datarooms)"} or \
        "(Links)" not in names[1]
    assert not any("(Links)" in n for n in names)
    assert tele["residual_collisions"] >= 0  # honest ledger, no crash


def test_b69v2_pf_display_non_echo_qualifier_still_available(monkeypatch):
    """Anti-case: a DISTINGUISHING PF qualifier (no token echo) survives the
    guard — the rung still resolves cross-PF twins."""
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "1")
    pfs = [_pf("trpc", "tRPC")]
    ufs = [
        _uf("UF-001", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", routes=["/sign/:param"],
            marker=True),
        _uf("UF-002", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", routes=["/sign/:param"],
            marker=True),
        _uf("UF-003", "Manage tRPC", reason=E2E_RECALL_REASON, pf="trpc",
            resource="waiting", intent="execute", routes=["/sign/:param"],
            marker=True),
    ]
    distinct_recall_row_names(ufs, pfs, {})
    assert _names(ufs)[2] == "Run waiting (tRPC)"  # unchanged vs pre-B69v2


def test_b69v2_off_ladder_byte_identical(monkeypatch):
    """Kill-switch: flag off ⇒ the ladder (tautology included) is
    byte-identical to pre-B69-v2 behaviour."""
    # MECHANICAL (horizon-1 flip): explicit "0" (unset now defaults ON).
    monkeypatch.setenv("FAULTLINE_HOMING_HYGIENE", "0")
    pfs = [_pf("datarooms", "Datarooms"), _pf("links", "Links")]
    ufs = [
        _uf("UF-001", "Manage links", reason=ROUTE_GROUP_REASON,
            pf="datarooms", resource="links", intent="browse",
            members=("f1",)),
        _uf("UF-002", "Manage links", reason=ROUTE_GROUP_REASON,
            pf="links", resource="links", intent="browse",
            members=("f2",)),
    ]
    distinct_recall_row_names(ufs, pfs, {})
    names = _names(ufs)
    assert names[0] == "Browse & filter links"
    assert names[1] == "Browse & filter links (Links)"  # the old tautology
