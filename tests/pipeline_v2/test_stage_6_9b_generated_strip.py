"""Tests for Stage 6.9b — generated-code output strip."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.stage_6_9b_generated_strip import (
    STAGE_6_9B_ENV_FLAG,
    is_generated_path,
    stage_6_9b_enabled,
    strip_generated_paths,
)

_NOW = datetime.now(timezone.utc)


def _feat(name: str, paths: list[str], **kw) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=_NOW,
        health_score=80.0,
        coverage_pct=42.5,
        **kw,
    )


# ── recognizer ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("p", [
    "pkg/db/acme_challenge_insert.sql_generated.go",
    "api/v1/user.pb.go",
    "proto/user_pb2.py",
    "proto/user_pb2_grpc.py",
    "x/color_string.go",
    "zz_generated.deepcopy.go",
    "lib/model.g.dart",
    "lib/model.freezed.dart",
    "Form.designer.cs",
    "model.generated.cs",        # compiled-lang codegen suffix
    "api.gen.go",
])
def test_is_generated_positive(p):
    assert is_generated_path(p) is True


@pytest.mark.parametrize("p", [
    "pkg/db/queries.go",         # hand-written
    "lib/core/Axios.js",
    "components/Button.tsx",
    "latest.go",                 # 'latest' != generated
    "generated_report.go",       # 'generated' mid-name, not the suffix
    "my_generator.go",
    "helpers/string.go",         # 'string.go' != '_string.go'
    "test_foo.py",
    # web families are ambiguous (hand-maintained .gen/.generated exist) → NOT
    # stripped by the filename pass (content-marker is the safe follow-up).
    "schema.generated.ts",
    "user.generated.tsx",
    "random.gen.ts",
    "config.gen.json",
    "",
])
def test_is_generated_negative(p):
    assert is_generated_path(p) is False


# ── strip ───────────────────────────────────────────────────────────────────


def test_strip_removes_generated_keeps_handwritten():
    f = _feat("db", [
        "pkg/db/queries.go",
        "pkg/db/q1.sql_generated.go",
        "pkg/db/q2.sql_generated.go",
    ])
    stats = strip_generated_paths([f], [])
    assert stats["paths_removed"] == 2
    assert f.paths == ["pkg/db/queries.go"]
    assert stats["features_dropped"] == 0


def test_feature_all_generated_is_dropped():
    feats = [_feat("proto", ["api/a.pb.go", "api/b.pb.go"])]
    stats = strip_generated_paths(feats, [])
    assert stats["features_dropped"] == 1
    assert feats == []


def test_member_files_stripped_so_owned_max_drops():
    f = _feat(
        "db",
        ["pkg/db/queries.go", "pkg/db/q.sql_generated.go"],
        member_files=[
            MemberFile(path="pkg/db/queries.go", role="anchor",
                       confidence=1.0, primary=True),
            MemberFile(path="pkg/db/q.sql_generated.go", role="anchor",
                       confidence=1.0, primary=True),
        ],
    )
    strip_generated_paths([f], [])
    assert [m.path for m in f.member_files] == ["pkg/db/queries.go"]


def test_no_generated_is_noop():
    f = _feat("core", ["lib/core/a.go", "lib/core/b.go"])
    before = list(f.paths)
    stats = strip_generated_paths([f], [])
    assert stats == {"paths_removed": 0, "features_dropped": 0, "flows_dropped": 0}
    assert f.paths == before


def test_metric_scalars_untouched():
    """The strip is display hygiene — it must NEVER mutate a metric scalar
    (coverage / health are computed upstream WITH the files present)."""
    f = _feat(
        "db",
        ["pkg/db/queries.go", "pkg/db/q.sql_generated.go"],
        member_files=[
            MemberFile(path="pkg/db/queries.go", role="anchor",
                       confidence=1.0, primary=True),
            MemberFile(path="pkg/db/q.sql_generated.go", role="anchor",
                       confidence=1.0, primary=True),
        ],
    )
    strip_generated_paths([f], [])
    assert f.coverage_pct == 42.5
    assert f.health_score == 80.0
    assert f.bug_fix_ratio == 0.0


def test_enabled_default_and_env(monkeypatch):
    monkeypatch.delenv(STAGE_6_9B_ENV_FLAG, raising=False)
    assert stage_6_9b_enabled() is True
    monkeypatch.setenv(STAGE_6_9B_ENV_FLAG, "0")
    assert stage_6_9b_enabled() is False
