"""B11 — line-grain coordinate reverse/forward lookup (the coordinates contract).

The acceptance test the operator specified: on a reactive-resume-shaped scan
(three email flows sharing the ``AuthEmailLayout`` span [36,135], each with its
own 13-line entry span), a ``(path, line)`` reverse lookup must:

  * for a line inside the SHARED layout [36,135] → return ALL THREE email flows
    plus their UF / PF chain;
  * for a line inside ONE flow's entry span → return ONLY that flow.

Plus the FORWARD direction: an entity's coordinate set is addressable at every
level (flow / user_flow / product_feature / dev_feature).
"""

from __future__ import annotations

from faultline.pipeline_v2.line_coordinates import (
    build_line_coordinate_index,
)

AUTH = "packages/email/src/templates/auth.tsx"


def _flow(name, entry_span, *, uuid=None, pf_dev="email", uf="UF-021"):
    """A flow whose line_ranges are the shared layout [36,135] + a unique
    entry span, mirroring the reactive-resume email trio shape."""
    return {
        "name": name,
        "uuid": uuid or name,
        "primary_feature": pf_dev,
        "user_flow_id": uf,
        "line_ranges": [
            {"path": AUTH, "start_line": 36, "end_line": 135},
            {"path": AUTH, "start_line": entry_span[0], "end_line": entry_span[1]},
        ],
    }


def _scan():
    return {
        "flows": [
            _flow("verify-email-flow", (159, 171)),
            _flow("verify-email-change-flow", (179, 191)),
            _flow("reset-password-templates-flow", (141, 153)),
        ],
        "developer_features": [
            {"name": "email", "product_feature_id": "email"},
        ],
        "product_features": [
            {"name": "email"},
        ],
        "user_flows": [
            {
                "id": "UF-021", "name": "Manage account email",
                "product_feature_id": "email",
                "member_flow_ids": [
                    "verify-email-flow", "verify-email-change-flow",
                    "reset-password-templates-flow",
                ],
            },
        ],
    }


# ── reverse: (path, line) → chain ────────────────────────────────────────

def test_shared_layout_line_returns_all_three_flows():
    idx = build_line_coordinate_index(_scan())
    r = idx.lookup(AUTH, 100)  # inside [36,135]
    assert r["flows"] == [
        "reset-password-templates-flow",
        "verify-email-change-flow",
        "verify-email-flow",
    ]
    assert r["dev_features"] == ["email"]
    assert r["user_flows"] == ["UF-021"]
    assert r["product_features"] == ["email"]


def test_entry_line_returns_only_its_flow():
    idx = build_line_coordinate_index(_scan())
    assert idx.lookup(AUTH, 145)["flows"] == ["reset-password-templates-flow"]
    assert idx.lookup(AUTH, 165)["flows"] == ["verify-email-flow"]
    assert idx.lookup(AUTH, 185)["flows"] == ["verify-email-change-flow"]


def test_boundary_lines_inclusive():
    idx = build_line_coordinate_index(_scan())
    # inclusive endpoints of the shared span
    assert set(idx.lookup(AUTH, 36)["flows"]) == {
        "reset-password-templates-flow", "verify-email-change-flow",
        "verify-email-flow",
    }
    assert set(idx.lookup(AUTH, 135)["flows"]) == {
        "reset-password-templates-flow", "verify-email-change-flow",
        "verify-email-flow",
    }
    # one line past the shared span, before any entry → nothing
    assert idx.lookup(AUTH, 136)["flows"] == []


def test_miss_returns_empty_chain():
    idx = build_line_coordinate_index(_scan())
    r = idx.lookup(AUTH, 5)  # above every span
    assert r == {
        "flows": [], "dev_features": [], "user_flows": [],
        "product_features": [],
    }
    assert idx.lookup("nonexistent.ts", 10)["flows"] == []


# ── forward: entity → coordinate set ─────────────────────────────────────

def test_flow_coordinate_set():
    idx = build_line_coordinate_index(_scan())
    assert idx.flow_coordinates("reset-password-templates-flow") == [
        (AUTH, 36, 135), (AUTH, 141, 153),
    ]


def test_user_flow_coordinate_set_is_union_of_members():
    idx = build_line_coordinate_index(_scan())
    uf = _scan()["user_flows"][0]
    coords = idx.user_flow_coordinates(uf)
    # union: the shared [36,135] once + each of the 3 unique entry spans
    assert coords == [
        (AUTH, 36, 135),
        (AUTH, 141, 153),
        (AUTH, 159, 171),
        (AUTH, 179, 191),
    ]


def test_product_feature_coordinate_set_contains_shared_and_entries():
    idx = build_line_coordinate_index(_scan())
    coords = idx.product_feature_coordinates("email")
    assert (AUTH, 36, 135) in coords          # the shared layout
    assert (AUTH, 141, 153) in coords          # reset-password entry
    assert (AUTH, 159, 171) in coords          # verify-email entry


def test_dev_feature_coordinate_set():
    idx = build_line_coordinate_index(_scan())
    coords = idx.dev_feature_coordinates("email")
    assert (AUTH, 36, 135) in coords
    assert len(coords) == 4  # shared + 3 unique entries, deduped


# ── shape robustness ─────────────────────────────────────────────────────

def test_nodes_fallback_when_line_ranges_absent():
    # a flow with no line_ranges falls back to nodes (file + lines)
    scan = {
        "flows": [{
            "name": "n-flow", "uuid": "n", "primary_feature": None,
            "user_flow_id": None, "line_ranges": [],
            "nodes": [{"file": "a.ts", "lines": [10, 20], "role": "entry"}],
        }],
        "developer_features": [], "product_features": [], "user_flows": [],
    }
    idx = build_line_coordinate_index(scan)
    assert idx.lookup("a.ts", 15)["flows"] == ["n-flow"]
    assert idx.lookup("a.ts", 21)["flows"] == []


def test_layered_features_shape():
    # a FeatureMap-style scan with features[] by layer (no developer_features)
    scan = {
        "flows": [_flow("f", (100, 110), pf_dev="d1")],
        "features": [
            {"name": "d1", "layer": "developer", "product_feature_id": "P1"},
            {"name": "P1", "layer": "product"},
        ],
        "user_flows": [],
    }
    idx = build_line_coordinate_index(scan)
    r = idx.lookup(AUTH, 105)
    assert r["flows"] == ["f"]
    assert r["dev_features"] == ["d1"]
    assert r["product_features"] == ["P1"]
