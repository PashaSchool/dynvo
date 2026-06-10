"""Unit tests for faultline.pipeline_v2.incremental."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from faultline.pipeline_v2.incremental import (
    carry_forward_metrics,
    changed_files_since,
    head_sha,
    load_base_scan,
    touched_feature_uuids,
)


# ── load_base_scan ─────────────────────────────────────────────────


def test_load_base_scan_reads_features_key(tmp_path):
    p = tmp_path / "scan.json"
    p.write_text(json.dumps({
        "features": [{"name": "a", "paths": ["x"]}],
        "scan_meta": {},
    }))
    data = load_base_scan(p)
    assert data["features"][0]["name"] == "a"


def test_load_base_scan_reads_developer_features_key(tmp_path):
    p = tmp_path / "scan.json"
    p.write_text(json.dumps({
        "developer_features": [{"name": "a", "paths": ["x"]}],
    }))
    data = load_base_scan(p)
    assert data["developer_features"][0]["name"] == "a"


def test_load_base_scan_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_base_scan(tmp_path / "missing.json")


def test_load_base_scan_raises_on_invalid_shape(tmp_path):
    p = tmp_path / "scan.json"
    p.write_text(json.dumps({"unrelated": "data"}))
    with pytest.raises(ValueError):
        load_base_scan(p)


# ── touched_feature_uuids ──────────────────────────────────────────


def test_touched_feature_uuids_maps_via_path_index():
    base = {
        "path_index": {
            "src/a.ts": {"feature_uuid": "A" * 32, "flow_uuids": []},
            "src/b.ts": {"feature_uuid": "B" * 32, "flow_uuids": []},
        }
    }
    touched = touched_feature_uuids(["src/a.ts", "src/b.ts"], base)
    assert touched == {"A" * 32, "B" * 32}


def test_touched_feature_uuids_skips_unknown_files():
    base = {"path_index": {"src/a.ts": {"feature_uuid": "A" * 32, "flow_uuids": []}}}
    touched = touched_feature_uuids(["src/a.ts", "new-file.ts"], base)
    assert touched == {"A" * 32}


def test_touched_feature_uuids_empty_when_no_path_index():
    assert touched_feature_uuids(["a"], {}) == set()


# ── carry_forward_metrics ──────────────────────────────────────────


def test_carry_forward_overwrites_untouched_features():
    base = [
        {"uuid": "A" * 32, "name": "a", "health_score": 80.0,
         "bug_fix_ratio": 0.1, "coverage_pct": 75.0},
    ]
    new = [
        {"uuid": "A" * 32, "name": "a", "health_score": 50.0,
         "bug_fix_ratio": 0.4, "coverage_pct": 30.0},
    ]
    carried = carry_forward_metrics(new, base, touched_uuids=set())
    assert carried == 1
    assert new[0]["health_score"] == 80.0
    assert new[0]["bug_fix_ratio"] == 0.1
    assert new[0]["coverage_pct"] == 75.0


def test_carry_forward_skips_touched_features():
    base = [{"uuid": "A" * 32, "name": "a", "health_score": 80.0}]
    new = [{"uuid": "A" * 32, "name": "a", "health_score": 50.0}]
    carried = carry_forward_metrics(new, base, touched_uuids={"A" * 32})
    assert carried == 0
    assert new[0]["health_score"] == 50.0


def test_carry_forward_skips_features_without_uuid_match():
    base = [{"uuid": "A" * 32, "name": "a", "health_score": 80.0}]
    new = [{"uuid": "B" * 32, "name": "b", "health_score": 50.0}]
    carried = carry_forward_metrics(new, base, touched_uuids=set())
    assert carried == 0
    assert new[0]["health_score"] == 50.0


# ── git helpers ────────────────────────────────────────────────────


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    (path / "a.ts").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    sha1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True,
    ).stdout.strip()
    (path / "b.ts").write_text("world")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "add b"], cwd=path, check=True)
    return sha1


def test_changed_files_since_real_repo(tmp_path):
    sha1 = _init_repo(tmp_path)
    changed = changed_files_since(tmp_path, sha1)
    assert changed == ["b.ts"]


def test_head_sha_real_repo(tmp_path):
    _init_repo(tmp_path)
    sha = head_sha(tmp_path)
    assert len(sha) == 40


def test_changed_files_since_returns_empty_on_bad_sha(tmp_path):
    _init_repo(tmp_path)
    changed = changed_files_since(tmp_path, "deadbeefdeadbeef")
    assert changed == []
