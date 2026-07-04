"""Stage 6.7e — UF grain lattice unit tests (grain-lattice spec 2026-07-04).

Covers: deterministic grouping (input-order + hash-seed invariance),
structural post-validation paths (inconsistent node regroup, duplicate-claim
dissolve, orphan rescue), degenerate/single-leaf nodes, surjectivity/no
orphans, the fake-client LLM namer hook, env gating, and the Stage 7 wiring
(additive output — leaves byte-untouched).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from faultline.models.types import UfCapability, UserFlow
from faultline.pipeline_v2.stage_6_7e_uf_lattice import (
    ENV_FLAG,
    _group_leaves,
    _validate_and_regroup,
    build_uf_lattice,
    lattice_enabled,
    make_llm_namer,
)


def _uf(uf_id: str, name: str, *, resource: str = "link", intent: str = "author",
        members: list[str] | None = None, routes: list[str] | None = None) -> UserFlow:
    members = members if members is not None else [f"{uf_id}-flow"]
    return UserFlow(
        id=uf_id, name=name, intent=intent, resource=resource,
        member_flow_ids=members, member_count=len(members),
        routes=routes if routes is not None else [f"/{resource}"],
    )


def _leaves() -> list[UserFlow]:
    return [
        _uf("UF-001", "Create a link", resource="link", intent="author",
            members=["f1", "f2"]),
        _uf("UF-002", "Browse links", resource="links", intent="browse",
            members=["f3"]),
        _uf("UF-003", "Archive a link", resource="link", intent="lifecycle",
            members=["f4"]),
        _uf("UF-004", "Export link analytics", resource="link", intent="export",
            members=["f5"]),
        _uf("UF-005", "Connect Stripe", resource="integration", intent="execute",
            members=["f6"]),
        _uf("UF-006", "Sign a document", resource="", intent="execute",
            members=["f7"]),
    ]


# ── Grouping + shape ────────────────────────────────────────────────────────


def test_manage_family_folds_crud_and_browse_by_resource() -> None:
    caps, tele = build_uf_lattice(_leaves())
    by_name = {c.name: c for c in caps}
    manage = by_name["Manage links"]
    assert set(manage.member_uf_ids) == {"UF-001", "UF-002", "UF-003"}
    assert manage.resource == "link"
    assert manage.intent == "manage"
    # grounding = union of children's evidence (heaviest child first:
    # UF-001 [f1,f2], then name order UF-003 [f4], UF-002 [f3])
    assert manage.member_flow_ids == ["f1", "f2", "f4", "f3"]
    assert manage.routes == ["/link", "/links"]
    assert tele["capabilities_count"] == len(caps)
    assert tele["multi_leaf_count"] == 1


def test_action_families_stay_separate() -> None:
    caps, _ = build_uf_lattice(_leaves())
    names = {c.name for c in caps}
    # export leaf must NOT fold into the manage node
    assert "Export link analytics" in names
    assert "Connect Stripe" in names


def test_degenerate_nodes_keep_leaf_name_and_intent() -> None:
    caps, tele = build_uf_lattice(_leaves())
    deg = {c.name: c for c in caps if c.member_count == 1}
    assert deg["Export link analytics"].intent == "export"
    assert deg["Connect Stripe"].resource == "integration"
    # no-resource leaf → own degenerate node, never lumped
    assert deg["Sign a document"].resource == ""
    assert tele["degenerate_count"] == len(deg)


def test_surjective_no_orphans_every_leaf_exactly_once() -> None:
    leaves = _leaves()
    caps, tele = build_uf_lattice(leaves)
    claimed = [uid for c in caps for uid in c.member_uf_ids]
    assert sorted(claimed) == sorted(u.id for u in leaves)  # exactly once
    assert tele["orphans"] == 0
    assert tele["leaves"] == len(leaves)


def test_empty_input() -> None:
    caps, tele = build_uf_lattice([])
    assert caps == [] and tele["capabilities_count"] == 0


def test_ids_are_content_sorted_and_stable() -> None:
    caps, _ = build_uf_lattice(_leaves())
    assert [c.id for c in caps] == [f"UFC-{i:03d}" for i in range(1, len(caps) + 1)]
    assert [c.name.lower() for c in caps] == sorted(c.name.lower() for c in caps)


# ── Determinism ─────────────────────────────────────────────────────────────


def test_grouping_is_input_order_invariant() -> None:
    leaves = _leaves()
    a, _ = build_uf_lattice(leaves)
    b, _ = build_uf_lattice(list(reversed(leaves)))
    assert [c.model_dump() for c in a] == [c.model_dump() for c in b]


def test_grouping_is_hash_seed_invariant() -> None:
    """PYTHONHASHSEED-varied subprocess runs must produce identical JSON."""
    script = (
        "import json;"
        "from faultline.models.types import UserFlow;"
        "from faultline.pipeline_v2.stage_6_7e_uf_lattice import build_uf_lattice;"
        "ufs=[UserFlow(id=f'UF-{i:03d}',name=f'n{i} thing',intent=['author','browse','execute','export'][i%4],"
        "resource=['link','domain','webhook',''][i%4],member_flow_ids=[f'f{i}'],member_count=1,routes=[f'/r{i}'])"
        " for i in range(23)];"
        "caps,tele=build_uf_lattice(ufs);"
        "print(json.dumps([c.model_dump() for c in caps],sort_keys=True))"
    )
    outs = set()
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, "-c", script], env=env,
                           capture_output=True, text=True, check=True)
        outs.add(r.stdout.strip())
    assert len(outs) == 1


# ── Structural post-validation ──────────────────────────────────────────────


def test_inconsistent_node_dissolves_to_degenerates() -> None:
    leaves = [
        _uf("UF-001", "Create a link", resource="link", intent="author"),
        _uf("UF-002", "Browse domains", resource="domain", intent="browse"),
    ]
    # Hand-build a BAD node mixing two resources (simulates a future LLM
    # grouping violation — deterministic grouping can't produce this).
    bad = [{"resource": "link", "family": "manage", "children": leaves}]
    nodes, regroups = _validate_and_regroup(bad, leaves)
    assert regroups == 1
    assert all(len(n["children"]) == 1 for n in nodes)
    claimed = sorted(c.id for n in nodes for c in n["children"])
    assert claimed == ["UF-001", "UF-002"]


def test_orphan_leaf_gets_degenerate_node() -> None:
    leaves = [
        _uf("UF-001", "Create a link"),
        _uf("UF-002", "Browse links", intent="browse"),
    ]
    nodes = _group_leaves([leaves[0]])  # UF-002 deliberately orphaned
    fixed, regroups = _validate_and_regroup(nodes, leaves)
    assert regroups == 1
    claimed = sorted(c.id for n in fixed for c in n["children"])
    assert claimed == ["UF-001", "UF-002"]


def test_duplicate_claim_dissolved_not_double_counted() -> None:
    leaf = _uf("UF-001", "Create a link")
    other = _uf("UF-002", "Browse links", intent="browse")
    nodes = [
        {"resource": "link", "family": "manage", "children": [leaf]},
        {"resource": "link", "family": "manage", "children": [leaf, other]},
    ]
    fixed, regroups = _validate_and_regroup(nodes, [leaf, other])
    assert regroups >= 1
    claimed = [c.id for n in fixed for c in n["children"]]
    assert sorted(claimed) == ["UF-001", "UF-002"]  # exactly once each


def test_regroup_count_zero_on_healthy_grouping() -> None:
    _, tele = build_uf_lattice(_leaves())
    assert tele["regroup_count"] == 0


# ── Namer hook (fake client) ────────────────────────────────────────────────


class _FakeMsg:
    def __init__(self, text: str) -> None:
        self.content = [type("B", (), {"text": text})()]


def _fake_client(payload: str, raise_exc: bool = False):
    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            self._outer.calls.append(kwargs)
            if raise_exc:
                raise RuntimeError("boom")
            return _FakeMsg(payload)

    class _Client:
        def __init__(self):
            self.calls: list[dict] = []
            self.messages = _Messages(self)

    return _Client()


def _ambiguous_leaves() -> list[UserFlow]:
    return [
        _uf("UF-001", "Send a document", resource="document", intent="execute",
            members=["f1", "f2"]),
        _uf("UF-002", "Sign a document", resource="document", intent="execute",
            members=["f3"]),
    ]


def test_fake_client_namer_renames_ambiguous_node() -> None:
    client = _fake_client('{"names": {"0": "Send and Sign Documents"}}')
    namer = make_llm_namer(client, model="claude-haiku-4-5-20251001")
    caps, tele = build_uf_lattice(_ambiguous_leaves(), namer=namer)
    assert tele["llm_named_count"] == 1
    assert any(c.name == "Send and Sign Documents" for c in caps)
    assert len(client.calls) == 1
    assert client.calls[0]["temperature"] == 0


def test_namer_failure_falls_back_to_heaviest_child_name() -> None:
    client = _fake_client("", raise_exc=True)
    namer = make_llm_namer(client, model="m")
    caps, tele = build_uf_lattice(_ambiguous_leaves(), namer=namer)
    assert tele["llm_named_count"] == 0
    # heaviest child (2 member flows) names the node deterministically
    assert any(c.name == "Send a document" and c.member_count == 2 for c in caps)


def test_namer_never_called_without_ambiguous_groups() -> None:
    calls: list = []

    def namer(groups):  # pragma: no cover - must not run
        calls.append(groups)
        return {}

    build_uf_lattice(_leaves()[:3], namer=namer)  # manage-family only
    assert calls == []


def test_manage_multi_leaf_never_llm_named() -> None:
    client = _fake_client('{"names": {"0": "HIJACK"}}')
    namer = make_llm_namer(client, model="m")
    caps, _ = build_uf_lattice(_leaves(), namer=namer)
    manage = next(c for c in caps if c.resource == "link" and c.intent == "manage")
    assert manage.name == "Manage links"


# ── LLM grouping pass (fake client / fake cache) ────────────────────────────


class _FakeCache:
    def __init__(self):
        self.store: dict = {}

    def get(self, kind, key):
        return self.store.get((kind, key))

    def set(self, kind, key, value):
        self.store[(kind, key)] = value


def _grouping_leaves() -> list[UserFlow]:
    return [
        _uf("UF-001", "Create content pages", resource="page", intent="author"),
        _uf("UF-002", "Bulk delete page types", resource="delete-page-type", intent="lifecycle"),
        _uf("UF-003", "Connect Stripe", resource="integration", intent="execute"),
        _uf("UF-004", "Browse channels", resource="channel", intent="browse"),
    ]


def test_llm_grouping_groups_cross_resource_and_validates() -> None:
    from faultline.pipeline_v2.stage_6_7e_uf_lattice import build_uf_lattice_llm

    payload = json.dumps({"capabilities": [
        {"name": "Manage content pages", "resource": "page", "intent": "manage",
         # UF-003 is INCONSISTENT (no token overlap with the node) → evicted;
         # "UF-999" unknown → ignored; UF-004 never placed → orphan rescue.
         "member_ids": ["UF-001", "UF-002", "UF-003", "UF-999"]},
    ]})
    client = _fake_client(payload)
    caps, tele = build_uf_lattice_llm(_grouping_leaves(), client=client)
    by_name = {c.name: c for c in caps}
    assert tele["grouping"] == "llm"
    node = by_name["Manage content pages"]
    assert node.member_uf_ids == ["UF-001", "UF-002"] or set(node.member_uf_ids) == {"UF-001", "UF-002"}
    assert tele["regroup_count"] == 1          # UF-003 evicted
    assert tele["orphan_rescued"] == 2         # UF-003 + UF-004 → degenerates
    claimed = sorted(uid for c in caps for uid in c.member_uf_ids)
    assert claimed == ["UF-001", "UF-002", "UF-003", "UF-004"]
    assert by_name["Connect Stripe"].member_count == 1


def test_llm_grouping_failure_falls_back_deterministic() -> None:
    from faultline.pipeline_v2.stage_6_7e_uf_lattice import build_uf_lattice_llm

    client = _fake_client("", raise_exc=True)
    caps, tele = build_uf_lattice_llm(_grouping_leaves(), client=client)
    assert tele["grouping"] == "deterministic"
    assert tele["grouping_fallback"] == "grouping_call_failed"
    det, _ = build_uf_lattice(_grouping_leaves())
    assert [c.model_dump() for c in caps] == [c.model_dump() for c in det]


def test_llm_grouping_no_client_falls_back() -> None:
    from faultline.pipeline_v2.stage_6_7e_uf_lattice import build_uf_lattice_llm

    caps, tele = build_uf_lattice_llm(
        _grouping_leaves(), _client_factory=lambda: None)
    assert tele["grouping_fallback"] == "no_client"
    assert caps  # deterministic lattice still emitted


def test_llm_grouping_cache_roundtrip_identical_no_second_call() -> None:
    from faultline.pipeline_v2.stage_6_7e_uf_lattice import build_uf_lattice_llm

    payload = json.dumps({"capabilities": [
        {"name": "Manage content pages", "resource": "page", "intent": "manage",
         "member_ids": ["UF-001", "UF-002"]},
        {"name": "Connect Stripe", "resource": "integration", "intent": "execute",
         "member_ids": ["UF-003"]},
        {"name": "Browse channels", "resource": "channel", "intent": "manage",
         "member_ids": ["UF-004"]},
    ]})
    cache = _FakeCache()
    c1 = _fake_client(payload)
    caps1, t1 = build_uf_lattice_llm(_grouping_leaves(), client=c1, cache=cache)
    assert not t1["cache_hit"] and len(c1.calls) == 1
    c2 = _fake_client("SHOULD NOT BE CALLED", raise_exc=True)
    caps2, t2 = build_uf_lattice_llm(_grouping_leaves(), client=c2, cache=cache)
    assert t2["cache_hit"] and len(c2.calls) == 0
    assert [c.model_dump() for c in caps1] == [c.model_dump() for c in caps2]


def test_llm_grouping_mega_parent_children_evicted_by_token_check() -> None:
    from faultline.pipeline_v2.stage_6_7e_uf_lattice import build_uf_lattice_llm

    payload = json.dumps({"capabilities": [
        {"name": "Manage everything in the workspace", "resource": "workspace",
         "intent": "manage",
         "member_ids": ["UF-001", "UF-002", "UF-003", "UF-004"]},
    ]})
    caps, tele = build_uf_lattice_llm(_grouping_leaves(), client=_fake_client(payload))
    # none of the leaves shares a token with "everything/workspace" → all
    # evicted → the blob node vanishes, every leaf lands degenerate
    assert all(c.member_count == 1 for c in caps)
    assert len(caps) == 4
    assert tele["regroup_count"] == 4


# ── Env gate + Stage 7 wiring ───────────────────────────────────────────────


def test_lattice_enabled_default_on(monkeypatch) -> None:
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert lattice_enabled()
    monkeypatch.setenv(ENV_FLAG, "0")
    assert not lattice_enabled()
    monkeypatch.setenv(ENV_FLAG, "false")
    assert not lattice_enabled()
    monkeypatch.setenv(ENV_FLAG, "1")
    assert lattice_enabled()


def test_feature_map_carries_lattice_and_leaves_untouched() -> None:
    from faultline.pipeline_v2.stage_7_output import build_feature_map

    leaves = _leaves()
    before = [u.model_dump() for u in leaves]
    caps, _ = build_uf_lattice(leaves)

    class _Ctx:
        repo_path = "/tmp/nonexistent-lattice-fixture"
        commits: list = []

    fm = build_feature_map([], _Ctx(), {}, user_flows=leaves,
                           uf_capabilities=caps)
    dumped = fm.model_dump(mode="json")
    assert len(dumped["uf_capabilities"]) == len(caps)
    assert [u.model_dump() for u in leaves] == before  # leaves byte-untouched
    # additive: legacy rehydration path — a map built WITHOUT the field
    fm2 = build_feature_map([], _Ctx(), {}, user_flows=leaves)
    assert fm2.model_dump(mode="json")["uf_capabilities"] == []


def test_uf_capability_rehydrates_from_json() -> None:
    caps, _ = build_uf_lattice(_leaves())
    raw = json.loads(json.dumps([c.model_dump() for c in caps]))
    again = [UfCapability(**c) for c in raw]
    assert [c.model_dump() for c in again] == [c.model_dump() for c in caps]
