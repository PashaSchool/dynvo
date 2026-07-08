"""Unit tests for the snapshot-gate normalizer (faultline.tools.normalize_scan)."""

from __future__ import annotations

from faultline.tools.normalize_scan import (
    canonical_json,
    normalize_scan,
    scan_digest,
)


def _doc(**overrides):
    base = {
        "schema_version": 1,
        "repo_path": "/tmp/repo",
        "analyzed_at": "2026-07-03T05:00:00Z",
        "features": [
            {"name": "auth", "health_score": 95.5, "paths": ["a.py"]},
        ],
        "developer_features": [
            {"name": "auth", "symbol_health_score": 80.1, "paths": ["a.py"]},
        ],
        "user_flows": [{"name": "login", "entry": "a.py"}],
        "scan_meta": {
            "run_id": "20260703T050000Z-abc123",
            "elapsed_sec": 42.5,
            "cost_usd": 0.0,
            "calls": 0,
            "stage_artifact_dir": "/tmp/state/logs/repo/run",
            "stage_6_3_cache_hits": 46855,
            "stage_6_55_page_interior": {"cache_hits": 120, "parsed": 34},
            "stage_6_3_elapsed_sec": 3.2,
            "framework_profile": "default",
            "stage_6_4": {
                "per_linker": {
                    "nextjs-http-route": {
                        "matched": 10,
                        "sample_links": [{"source": "x.ts:1"}],
                        "unmatched_sample": [{"file": "y.ts"}],
                    }
                }
            },
        },
    }
    base.update(overrides)
    return base


def test_volatile_fields_are_stripped() -> None:
    norm = normalize_scan(_doc())
    assert "analyzed_at" not in norm
    meta = norm["scan_meta"]
    for key in (
        "run_id",
        "elapsed_sec",
        "cost_usd",
        "calls",
        "stage_artifact_dir",
        "stage_6_3_cache_hits",
        "stage_6_3_elapsed_sec",
        "stage_6_55_page_interior",
    ):
        assert key not in meta, key
    linker = meta["stage_6_4"]["per_linker"]["nextjs-http-route"]
    assert "sample_links" not in linker
    assert "unmatched_sample" not in linker
    assert linker["matched"] == 10  # content survives


def test_wall_clock_decayed_health_stripped_everywhere() -> None:
    norm = normalize_scan(_doc())
    assert "health_score" not in norm["features"][0]
    assert "symbol_health_score" not in norm["developer_features"][0]
    assert norm["features"][0]["name"] == "auth"


def test_content_fields_survive() -> None:
    norm = normalize_scan(_doc())
    assert norm["repo_path"] == "/tmp/repo"
    assert norm["schema_version"] == 1
    assert norm["user_flows"] == [{"name": "login", "entry": "a.py"}]
    assert norm["scan_meta"]["framework_profile"] == "default"


def test_digest_stable_across_volatile_churn() -> None:
    a = _doc()
    b = _doc(analyzed_at="2026-07-04T09:30:00Z")
    b["scan_meta"]["run_id"] = "other-run"
    b["scan_meta"]["elapsed_sec"] = 99.9
    b["scan_meta"]["stage_6_3_cache_hits"] = 1
    b["features"][0]["health_score"] = 95.4  # decayed a tick
    assert scan_digest(a) == scan_digest(b)


def test_digest_moves_on_content_change() -> None:
    a = _doc()
    b = _doc()
    b["features"][0]["paths"] = ["a.py", "b.py"]
    assert scan_digest(a) != scan_digest(b)


def test_normalize_does_not_mutate_input() -> None:
    doc = _doc()
    normalize_scan(doc)
    assert "analyzed_at" in doc
    assert "run_id" in doc["scan_meta"]


def test_canonical_json_is_key_order_independent() -> None:
    a = {"b": 1, "a": [1, 2]}
    b = {"a": [1, 2], "b": 1}
    assert canonical_json(a) == canonical_json(b)
