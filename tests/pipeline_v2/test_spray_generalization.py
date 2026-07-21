"""S2-A-v3 spray-generalization (``FAULTLINE_SPRAY_GENERALIZED``, OFF).

Named units for the probe exhibits (spray-generalization probe 2026-07-19,
SHIP/HIGH; twenty-b board = det-aggregation armed keyless world):

  * twenty-b exhibit — settings-PF 36 rows carry a 17-row TECH-DIR-SUFFIX
    spray in 3 (PF, prefix) groups:
      'Manage setting AI …'            components/constants/graphqls/hooks/
                                       types/utils            (6 rows)
      'Manage setting applications …'  components/constants/hooks/tabs/
                                       types/utils            (6 rows)
      'Manage setting data model …'    constants/news/stories/types/utils
                                                              (5 rows)
    Armed: 17/17 absorption (group-absorption carries the graphqls/news
    structural misses) -> 3 own-resource parents 'Manage AI settings' /
    'Manage application settings' / 'Manage data model settings';
    settings-PF 36 -> 22 rows.

Anti-cases (the spec's survivors — MUST stay untouched):
  * 'Manage billing' family — every bare 'Manage <resource>' sibling of the
    settings PF (billing/members/security/…) is G1-blocked structurally
    (prefix == intent alone);
  * paren families ('Manage settings (Setting)'/'(Settings)') are R5-2's
    qualifier-spray class — G0 skips them ALWAYS;
  * keyed resolver twins ('Manage workspace resolvers (Core)/(Metadata)')
    — same G0 class boundary, never annexed;
  * conservation — the member union of the 3 parents equals the member
    union of the 17 absorbed rows, zero flow loss, no dangling
    ``flow.user_flow_id`` (I14).

NO member-count clause: the spray carries 2-23 members (mc=23 'components'
absorbs; the probe REFUTED the R5-2 thin-row clause for this class).

SACRED: flag unset/=0 ⇒ user_flows[] + telemetry byte-identical (the
``spray_generalized`` tele key exists ONLY on armed worlds).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.naming_contract import (
    SPRAY_GENERALIZED_ENV,
    _spray_parent_name,
    load_naming_vocab,
    run_naming_contract,
    spray_generalized_enabled,
)
from faultline.pipeline_v2.spray_absorption import run_spray_generalization

VOCAB = load_naming_vocab()
_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

_SETTINGS_ROOT = "packages/twenty-front/src/pages/settings"

#: The twenty-b exhibit — (name, member_count, member leaf-dir chain) for
#: every settings-PF row. Spray rows put members under the group's shared
#: dir-chain + the tail's own leaf dir; the two STRUCTURAL MISSES
#: (graphqls / news) carry members whose leaf dir does NOT fold-match the
#: tail — group-absorption must carry them anyway.
_SPRAY_ROWS: list[tuple[str, str, int, str]] = [
    # (uf_id, name, mc, leaf-dir chain under _SETTINGS_ROOT)
    ("UF-L-rai01", "Manage setting AI components", 23, "ai/components"),
    ("UF-L-rai02", "Manage setting AI constants", 4, "ai/constants"),
    ("UF-L-rai03", "Manage setting AI graphqls", 2, "ai/graphql-operations"),
    ("UF-L-rai04", "Manage setting AI hooks", 4, "ai/hooks"),
    ("UF-L-rai05", "Manage setting AI types", 3, "ai/types"),
    ("UF-L-rai06", "Manage setting AI utils", 2, "ai/utils"),
    ("UF-L-rap01", "Manage setting applications components", 17,
     "applications/components"),
    ("UF-L-rap02", "Manage setting applications constants", 2,
     "applications/constants"),
    ("UF-L-rap03", "Manage setting applications hooks", 5,
     "applications/hooks"),
    ("UF-L-rap04", "Manage setting applications tabs", 19,
     "applications/tabs"),
    ("UF-L-rap05", "Manage setting applications types", 3,
     "applications/types"),
    ("UF-L-rap06", "Manage setting applications utils", 6,
     "applications/utils"),
    ("UF-L-rdm01", "Manage setting data model constants", 6,
     "data-model/constants"),
    ("UF-L-rdm02", "Manage setting data model news", 4,
     "data-model/__stories__"),
    ("UF-L-rdm03", "Manage setting data model stories", 6,
     "data-model/stories"),
    ("UF-L-rdm04", "Manage setting data model types", 3, "data-model/types"),
    ("UF-L-rdm05", "Manage setting data model utils", 6, "data-model/utils"),
]

_HEALTHY_ROWS: list[tuple[str, str, int, str]] = [
    ("UF-L-h01", "Manage API webhooks", 2, "developers/webhooks"),
    ("UF-L-h02", "Manage admin panel", 16, "admin-panel"),
    ("UF-L-h03", "Manage billing", 5, "billing"),
    ("UF-L-h04", "Manage communications", 4, "communications"),
    ("UF-L-h05", "Manage community", 1, "community"),
    ("UF-L-h06", "Manage developers", 16, "developers"),
    ("UF-L-h07", "Manage domains", 7, "domains"),
    ("UF-L-h08", "Manage enterprise", 2, "enterprise"),
    ("UF-L-h09", "Manage general", 2, "general"),
    ("UF-L-h10", "Manage layout", 7, "layout"),
    ("UF-L-h11", "Manage logic functions", 2, "logic"),
    ("UF-L-h12", "Manage members", 12, "members"),
    ("UF-L-h13", "Manage profile", 9, "profile"),
    ("UF-L-h14", "Manage security", 2, "security"),
    ("UF-L-h15", "Manage settings", 10, "root"),
    ("UF-L-h16", "Manage settings (Setting)", 9, "root"),
    ("UF-L-h17", "Manage settings (Settings)", 9, "root"),
    ("UF-L-h18", "Return to path after login", 12, "root"),
    ("UF-008", "accounts", 1, "accounts"),
]

_SPRAY_NAMES = {name for _, name, _, _ in _SPRAY_ROWS}
_PARENT_NAMES = {
    "Manage AI settings",
    "Manage application settings",
    "Manage data model settings",
}


def _pf(slug: str, display: str) -> Feature:
    return Feature(
        name=slug, display_name=display, layer="product",
        paths=[], authors=["a"], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_NOW, health_score=100.0,
    )


def _flow(uuid: str, name: str, paths: list[str]) -> Flow:
    return Flow(
        uuid=uuid, name=name, paths=paths, authors=["a"], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=_NOW,
        health_score=100.0, user_flow_id=None,
    )


def _uf(uid: str, name: str, members: list[str], pfid: str = "settings",
        conf: str = "high") -> UserFlow:
    return UserFlow(
        id=uid, name=name, resource=name.split()[-1].lower(), domain=None,
        product_feature_id=pfid, intent="manage",
        member_flow_ids=sorted(members), member_count=len(members),
        name_confidence=conf,  # type: ignore[arg-type]
    )


def _world() -> tuple[list[Feature], list[UserFlow], list[Flow]]:
    """The synthetic twenty-b settings-PF exhibit world (36 rows)."""
    pfs = [_pf("settings", "Settings")]
    ufs: list[UserFlow] = []
    flows: list[Flow] = []
    n = 0
    for uid, name, mc, chain in _SPRAY_ROWS + _HEALTHY_ROWS:
        members: list[str] = []
        for i in range(mc):
            n += 1
            fid = f"u-{n:04d}"
            if chain == "root":
                path = f"{_SETTINGS_ROOT}/File{n}.tsx"
            else:
                path = f"{_SETTINGS_ROOT}/{chain}/File{n}.tsx"
            flows.append(_flow(fid, f"flow-{n:04d}-flow", [path]))
            members.append(fid)
        conf = "low" if uid == "UF-008" else "high"
        ufs.append(_uf(uid, name, members, conf=conf))
        for fl in flows[-mc:]:
            fl.user_flow_id = uid
    return pfs, ufs, flows


def _run(pfs: list[Feature], ufs: list[UserFlow],
         flows: list[Flow]) -> dict | None:
    """The phase_finalize wiring: naming contract FIRST (final display
    names — the probe world), then the flag-gated absorption seam.
    Returns the spray telemetry (``None`` when the flag is off)."""
    run_naming_contract(
        pfs, ufs, flows, keeper_on=False,
        product_strings=None, routes_index=None,
        uf_authored_names={}, labeler=None, verifier=None, repo_root=None,
    )
    return run_spray_generalization(ufs, flows)


def _names(ufs: list[UserFlow]) -> set[str]:
    return {str(u.name) for u in ufs}


def _member_union(ufs: list[UserFlow]) -> set[str]:
    out: set[str] = set()
    for u in ufs:
        out.update(str(m) for m in (u.member_flow_ids or []))
    return out


# ── flag: default ON (pack-3 flip) + kill-switch + cache keying ──────────


def test_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # SEMANTIC flip migration (2026-07-21 pack №3, KEY_SCHEMA 34): unset
    # now arms the absorption pass (unset ≡ explicit-1).
    monkeypatch.delenv(SPRAY_GENERALIZED_ENV, raising=False)
    assert spray_generalized_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "off", "no", ""])
def test_flag_off_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, val)
    assert spray_generalized_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", " TRUE "])
def test_flag_on_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, val)
    assert spray_generalized_enabled() is True


def test_flag_registered_for_cache_keying() -> None:
    from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS
    assert SPRAY_GENERALIZED_ENV in ENV_OUTPUT_FLAGS


# ── OFF path: byte-identity (no pass, no telemetry key) ──────────────────


def test_off_world_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    # MECHANICAL flip migration (2026-07-21 pack №3, KEY_SCHEMA 34): the
    # OFF baseline is pinned with an explicit =0, not left unset.
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "0")
    pfs, ufs, flows = _world()
    spray = _run(pfs, ufs, flows)
    assert spray is None
    assert len(ufs) == 36
    assert _SPRAY_NAMES <= _names(ufs)
    assert not (_PARENT_NAMES & _names(ufs))


# ── twenty-b exhibit: 17/17 absorption, 36 -> 22, 3 parents ─────────────


def test_twenty_b_exhibit_17_of_17_absorption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs, ufs, flows = _world()
    spray = _run(pfs, ufs, flows)
    assert spray is not None
    assert spray["groups_fired"] == 3
    assert spray["rows_absorbed"] == 14          # 17 rows -> 3 survivors
    assert sorted(spray["parents"]) == sorted(_PARENT_NAMES)
    assert len(ufs) == 22                        # 36 - 17 + 3
    names = _names(ufs)
    assert _PARENT_NAMES <= names
    assert not (_SPRAY_NAMES & names)            # every spray name gone


def test_parent_names_are_own_resource_never_bare_prefix() -> None:
    assert _spray_parent_name(
        ("Manage", "setting", "AI"), VOCAB) == "Manage AI settings"
    assert _spray_parent_name(
        ("Manage", "setting", "applications"), VOCAB,
    ) == "Manage application settings"
    assert _spray_parent_name(
        ("Manage", "setting", "data", "model"), VOCAB,
    ) == "Manage data model settings"
    # NEVER the bare prefix ('Manage setting AI').
    assert _spray_parent_name(
        ("Manage", "setting", "AI"), VOCAB) != "Manage setting AI"


def test_structural_misses_ride_group_absorption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """graphqls/news tails do NOT leaf-match (15/17 direct) yet the whole
    group collapses (17/17) — the misses land in the parents' unions."""
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs, ufs, flows = _world()
    before = {u.id: set(u.member_flow_ids or []) for u in ufs}
    _run(pfs, ufs, flows)
    by_name = {str(u.name): u for u in ufs}
    ai = set(by_name["Manage AI settings"].member_flow_ids or [])
    dm = set(by_name["Manage data model settings"].member_flow_ids or [])
    assert before["UF-L-rai03"] <= ai            # graphqls absorbed
    assert before["UF-L-rdm02"] <= dm            # news absorbed


def test_no_member_count_clause_mc23_absorbs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spray carries 2-23 members — the mc=23 'components' row absorbs
    (the R5-2 thin-row clause is ABSENT here by probe ruling)."""
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs, ufs, flows = _world()
    before = {u.id: set(u.member_flow_ids or []) for u in ufs}
    assert len(before["UF-L-rai01"]) == 23
    _run(pfs, ufs, flows)
    by_name = {str(u.name): u for u in ufs}
    ai = set(by_name["Manage AI settings"].member_flow_ids or [])
    assert before["UF-L-rai01"] <= ai


# ── conservation: member union + I14 backpointers ────────────────────────


def test_conservation_member_union_zero_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs, ufs, flows = _world()
    union_before = _member_union(ufs)
    spray_union_before = _member_union(
        [u for u in ufs if str(u.name) in _SPRAY_NAMES])
    _run(pfs, ufs, flows)
    assert _member_union(ufs) == union_before
    parents = [u for u in ufs if str(u.name) in _PARENT_NAMES]
    assert _member_union(parents) == spray_union_before
    for u in parents:
        assert u.member_count == len(u.member_flow_ids or [])


def test_i14_no_dangling_flow_backpointers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs, ufs, flows = _world()
    _run(pfs, ufs, flows)
    live = {str(u.id) for u in ufs}
    for fl in flows:
        if fl.user_flow_id:
            assert str(fl.user_flow_id) in live


# ── anti-cases: the spec's survivors, by name ────────────────────────────


def test_anti_case_manage_billing_family_g1_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every bare 'Manage <resource>' sibling survives verbatim — G1 blocks
    the intent-only prefix structurally even when the leaf dir matches the
    tail ('Manage billing' members live under …/settings/billing/)."""
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs, ufs, flows = _world()
    healthy_before = {
        u.id: (str(u.name), tuple(u.member_flow_ids or []))
        for u in ufs if str(u.name) not in _SPRAY_NAMES
    }
    _run(pfs, ufs, flows)
    after = {u.id: (str(u.name), tuple(u.member_flow_ids or []))
             for u in ufs}
    for uid, snap in healthy_before.items():
        assert after.get(uid) == snap


def test_anti_case_paren_families_untouched_g0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'Manage settings (Setting)'/'(Settings)' are R5-2's class (G0) —
    present and unrenamed on the armed world."""
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs, ufs, flows = _world()
    _run(pfs, ufs, flows)
    names = _names(ufs)
    assert "Manage settings (Setting)" in names
    assert "Manage settings (Settings)" in names


def test_anti_case_keyed_resolver_twins_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keyed-world resolver twins wear paren qualifiers — G0 skips them
    even when their tails would leaf-match and 3+ siblings exist."""
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs = [_pf("server", "Server")]
    flows: list[Flow] = []
    ufs: list[UserFlow] = []
    for i, qual in enumerate(("Core", "Metadata", "Workspace"), start=1):
        fid = f"r-{i}"
        flows.append(_flow(
            fid, f"resolver-{i}-flow",
            [f"packages/server/src/{qual.lower()}/resolvers/R{i}.ts"]))
        ufs.append(_uf(
            f"UF-R-{i:02d}", f"Manage workspace resolvers ({qual})",
            [fid], pfid="server"))
    before = [(u.id, str(u.name)) for u in ufs]
    spray = _run(pfs, ufs, flows)
    assert spray is not None and spray["groups_fired"] == 0
    assert [(u.id, str(u.name)) for u in ufs] == before


def test_two_matching_siblings_never_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """>=3 tail-matching siblings are required — a 2-match group holds."""
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs = [_pf("settings", "Settings")]
    flows = [
        _flow("s-1", "s1-flow",
              ["app/settings/ai/hooks/A.ts"]),
        _flow("s-2", "s2-flow",
              ["app/settings/ai/types/B.ts"]),
        _flow("s-3", "s3-flow",
              ["app/settings/ai/Widget.tsx"]),   # no tail match
    ]
    ufs = [
        _uf("UF-X-01", "Manage setting AI hooks", ["s-1"]),
        _uf("UF-X-02", "Manage setting AI types", ["s-2"]),
        _uf("UF-X-03", "Manage setting AI widgets", ["s-3"]),
    ]
    spray = _run(pfs, ufs, flows)
    assert spray is not None and spray["groups_fired"] == 0
    assert len(ufs) == 3


def test_leaf_ratio_exactly_half_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly 50% member-path leaf hits pass (>= _SPRAY_LEAF_RATIO): a
    1-of-2 row is the 3rd matching sibling and the group fires."""
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs = [_pf("settings", "Settings")]
    flows: list[Flow] = []
    ufs: list[UserFlow] = []
    for idx, leaf in enumerate(("hooks", "types"), start=1):
        fid = f"c-{idx}"
        flows.append(_flow(fid, f"{fid}-flow",
                           [f"app/settings/ai/{leaf}/F.ts"]))
        ufs.append(_uf(f"UF-C-{idx:02d}", f"Manage setting AI {leaf}",
                       [fid]))
    flows.append(_flow("hh-1", "hh-1-flow", ["app/settings/ai/states/S.ts"]))
    flows.append(_flow("hh-2", "hh-2-flow", ["app/settings/ai/Other.tsx"]))
    ufs.append(_uf("UF-C-03", "Manage setting AI states", ["hh-1", "hh-2"]))
    spray = _run(pfs, ufs, flows)
    assert spray is not None
    assert spray["groups_fired"] == 1
    assert spray["rows_absorbed"] == 2           # whole 3-row group -> 1
    assert len(ufs) == 1
    assert str(ufs[0].name) == "Manage AI settings"


def test_leaf_ratio_below_half_never_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1-of-3 (33%) row is NOT a matching sibling — with only 2 clean
    siblings left the group never fires."""
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs = [_pf("settings", "Settings")]
    flows: list[Flow] = []
    ufs: list[UserFlow] = []
    for idx, leaf in enumerate(("hooks", "types"), start=1):
        fid = f"b-{idx}"
        flows.append(_flow(fid, f"{fid}-flow",
                           [f"app/settings/ai/{leaf}/F.ts"]))
        ufs.append(_uf(f"UF-B-{idx:02d}", f"Manage setting AI {leaf}",
                       [fid]))
    below = [
        _flow("bb-1", "bb-1-flow", ["app/settings/ai/states/S.ts"]),
        _flow("bb-2", "bb-2-flow", ["app/settings/ai/One.tsx"]),
        _flow("bb-3", "bb-3-flow", ["app/settings/ai/Two.tsx"]),
    ]
    flows.extend(below)
    ufs.append(_uf("UF-B-03", "Manage setting AI states",
                   ["bb-1", "bb-2", "bb-3"]))
    spray = _run(pfs, ufs, flows)
    assert spray is not None and spray["groups_fired"] == 0
    assert len(ufs) == 3


def test_no_new_dup_guard_keeps_survivor_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live same-PF row already wearing the parent name blocks the rename
    (the R5 no-new-dup law) — absorption still happens."""
    monkeypatch.setenv(SPRAY_GENERALIZED_ENV, "1")
    pfs = [_pf("settings", "Settings")]
    flows: list[Flow] = []
    ufs: list[UserFlow] = []
    for idx, leaf in enumerate(("hooks", "types", "utils"), start=1):
        fid = f"d-{idx}"
        flows.append(_flow(fid, f"{fid}-flow",
                           [f"app/settings/ai/{leaf}/F.ts"]))
        ufs.append(_uf(f"UF-D-{idx:02d}", f"Manage setting AI {leaf}",
                       [fid]))
    flows.append(_flow("d-9", "d-9-flow", ["app/settings/ai/Live.tsx"]))
    ufs.append(_uf("UF-D-99", "Manage AI settings", ["d-9"]))
    spray = _run(pfs, ufs, flows)
    assert spray is not None
    assert spray["groups_fired"] == 1
    assert spray["parent_name_dup_kept"] == 1
    names = sorted(_names(ufs))
    assert names.count("Manage AI settings") == 1   # never duplicated
    assert len(ufs) == 2                            # 3 -> 1 survivor + live row
