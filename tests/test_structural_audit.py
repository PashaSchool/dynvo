"""Tests for the golden-free structural audit (eval/structural_audit.py)."""

from __future__ import annotations

from eval.structural_audit import (
    _gini,
    _is_container_name,
    _is_platform_feature,
    audit_scan,
)

_WS = "[package] workspace anchor 'frontend' from monorepo package 'frontend'"


def _scan(features: list[tuple[str, list[str]]], pfs: list[str] | None = None) -> dict:
    return {
        "developer_features": [
            {"name": name, "paths": paths, "product_feature_id": None} for name, paths in features
        ],
        "product_features": [{"name": p} for p in (pfs or [])],
    }


# ── _gini ───────────────────────────────────────────────────────────────────


def test_gini_even_distribution_is_low() -> None:
    assert _gini([10, 10, 10, 10]) == 0.0


def test_gini_concentrated_is_high() -> None:
    assert _gini([1, 1, 1, 100]) > 0.6


def test_gini_empty_and_singleton() -> None:
    assert _gini([]) == 0.0
    assert _gini([5]) == 0.0


# ── _is_container_name ──────────────────────────────────────────────────────


def test_container_name_detection() -> None:
    assert _is_container_name("backend", "backend")
    assert _is_container_name("frontend-v2", "frontend")  # versioned package root
    assert _is_container_name("src", "")
    # echoes its own dominant top dir
    assert _is_container_name("web", "web")
    # a real domain feature is NOT a container
    assert not _is_container_name("cert-manager", "frontend")
    assert not _is_container_name("billing", "backend")
    assert not _is_container_name("project", "frontend")


# ── audit_scan ──────────────────────────────────────────────────────────────


def test_blob_feature_flagged() -> None:
    # One package-named blob holding the whole frontend tree + small real ones.
    blob = ("frontend", [f"frontend/src/f{i}.ts" for i in range(60)])
    real_a = ("billing", ["frontend/src/billing/charge.ts", "frontend/src/billing/plan.ts"])
    real_b = ("auth", ["frontend/src/auth/login.ts"])
    audit = audit_scan(_scan([blob, real_a, real_b]))
    assert audit.blob_count == 1
    assert audit.blobs[0].name == "frontend"
    assert audit.max_feature_share > 0.8
    # gini high because one feature dwarfs the others
    assert audit.gini > 0.5


def test_large_domain_feature_is_not_a_blob() -> None:
    # A sizeable (~30%), path-concentrated feature with a DOMAIN name, among
    # many peers, is NOT a blob: it's oversized but not container-named and well
    # under the "too big to be one feature" severe bar.
    big = ("certificate-management", [f"frontend/src/pki/c{i}.ts" for i in range(30)])
    peers = [(f"feat-{i}", [f"frontend/src/feat{i}/a.ts", f"frontend/src/feat{i}/b.ts"]) for i in range(35)]
    audit = audit_scan(_scan([big, *peers]))
    assert audit.blob_count == 0
    assert 0.25 < audit.max_feature_share < 0.40


def test_top3_share_is_distinct_union_not_oversum() -> None:
    # Features SHARE a file; top3 must be the distinct union (≤ 100%).
    a = ("a", ["x.ts", "shared.ts"])
    b = ("b", ["y.ts", "shared.ts"])
    c = ("c", ["z.ts", "shared.ts"])
    audit = audit_scan(_scan([a, b, c]))
    # distinct files: x,y,z,shared = 4; union of all three = 4 → 100%, never >100
    assert audit.total_files == 4
    assert audit.top3_share == 1.0


def test_balanced_scan_has_low_concentration() -> None:
    feats = [(f"feat-{i}", [f"src/{i}/a.ts", f"src/{i}/b.ts"]) for i in range(10)]
    audit = audit_scan(_scan(feats))
    assert audit.blob_count == 0
    assert audit.max_feature_share <= 0.15
    assert audit.gini < 0.1


def test_pf_attribution_pct() -> None:
    scan = {
        "developer_features": [
            {"name": "a", "paths": ["x.ts"], "product_feature_id": "P1"},
            {"name": "b", "paths": ["y.ts"], "product_feature_id": None},
        ],
        "product_features": [{"name": "P1"}],
    }
    audit = audit_scan(scan)
    assert audit.dev_features_with_pf_pct == 0.5


# ── platform-bucket reframe ─────────────────────────────────────────────────


def test_is_platform_feature() -> None:
    assert _is_platform_feature({"description": _WS})
    assert _is_platform_feature(
        {"description": "[route] slug 'x' | [package] workspace anchor 'backend'"}
    )
    # dep-category package anchor is NOT a platform bucket
    assert not _is_platform_feature(
        {"description": "[package] package anchor 'billing' from deps ['stripe']"}
    )
    assert not _is_platform_feature({"description": "[route] route slug 'auth'"})
    assert not _is_platform_feature({"description": None})


def test_platform_bucket_excluded_from_concentration() -> None:
    # A workspace-anchor platform bucket holds the whole frontend tree; the real
    # features are small. The bucket must NOT be a blob and must NOT dominate
    # max_feature_share — it is reported as platform_share instead.
    scan = {
        "developer_features": [
            {"name": "frontend", "description": _WS,
             "paths": [f"frontend/src/f{i}.ts" for i in range(80)]},
            {"name": "billing", "description": "[route] slug 'billing'",
             "paths": ["frontend/src/billing/a.ts", "frontend/src/billing/b.ts"]},
            {"name": "auth", "description": "[route] slug 'auth'",
             "paths": ["frontend/src/auth/login.ts"]},
        ],
        "product_features": [],
    }
    a = audit_scan(scan)
    assert a.n_platform_features == 1
    assert a.n_dev_features == 2          # real features only
    assert a.blob_count == 0             # platform recognised, not a blob
    assert a.platform_share > 0.9        # 80 of ~83 files are platform
    # max_feature_share is over the REAL features (billing=2 of 3 real files)
    assert a.max_feature_share < 0.8
    assert a.largest_feature in {"billing", "auth"}
    assert a.total_files == 83           # real ∪ platform


def test_platform_share_zero_when_no_anchor() -> None:
    scan = {
        "developer_features": [
            {"name": "a", "description": "[route] slug 'a'", "paths": ["x.ts"]},
            {"name": "b", "description": "[route] slug 'b'", "paths": ["y.ts"]},
        ],
        "product_features": [],
    }
    a = audit_scan(scan)
    assert a.platform_share == 0.0
    assert a.n_platform_features == 0


# ── lever metrics (Soc0 audit) ───────────────────────────────────────────────


def test_service_residual_pct() -> None:
    # Service/model files held ONLY by the platform anchor (not a real feature)
    # are residual; a service file ALSO claimed by a real feature is not. main.py
    # is not a backend-layer file, so it doesn't count.
    scan = {
        "developer_features": [
            {"name": "backend", "description": _WS,
             "paths": ["backend/services/a.py", "backend/services/b.py",
                       "backend/models/c.py", "backend/main.py"]},
            {"name": "auth", "description": "[route] slug 'auth'",
             "paths": ["backend/services/a.py"]},
        ],
        "product_features": [],
    }
    a = audit_scan(scan)
    assert a.service_residual_pct == round(2 / 3, 4)  # b.py + c.py residual; a.py in a real feat


def test_largest_sink_share() -> None:
    # The biggest feature owns files no other feature touches → sink share = 1.0.
    scan = {
        "developer_features": [
            {"name": "blob", "description": _WS, "paths": [f"frontend/x{i}.ts" for i in range(10)]},
            {"name": "small", "description": "[route] slug 's'", "paths": ["frontend/shared.ts"]},
        ],
        "product_features": [],
    }
    assert audit_scan(scan).largest_sink_share == 1.0


def test_routes_in_platform_pct() -> None:
    scan = {
        "developer_features": [
            {"name": "backend", "uuid": "PLAT", "description": _WS, "paths": ["backend/main.py"]},
            {"name": "auth", "uuid": "REAL", "description": "[route] slug 'auth'", "paths": ["backend/auth.py"]},
        ],
        "product_features": [],
        "routes_index": [
            {"file": "backend/main.py", "pattern": "/a", "feature_uuid": "PLAT"},
            {"file": "backend/main.py", "pattern": "/b", "feature_uuid": "PLAT"},
            {"file": "backend/auth.py", "pattern": "/login", "feature_uuid": "REAL"},
        ],
    }
    assert audit_scan(scan).routes_in_platform_pct == round(2 / 3, 4)
