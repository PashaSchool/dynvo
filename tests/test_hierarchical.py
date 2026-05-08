"""Sprint 22 Day 1 — hierarchical scan unit tests (no LLM calls)."""

from __future__ import annotations

from faultline.llm.hierarchical import (
    Bucket,
    describe_buckets,
    plan_buckets,
    summarise_layout,
)


def test_summarise_layout_groups_workspace_dirs_two_deep():
    files = [
        "apps/web/page.tsx", "apps/web/layout.tsx",
        "apps/api/server.ts",
        "packages/auth/login.ts",
        "src/lib/x.ts",  # non-workspace gets just top-level
    ]
    summary, mapping = summarise_layout(files)
    assert "apps/web/" in summary
    assert "apps/api/" in summary
    assert "packages/auth/" in summary
    assert "src/" in summary
    # mapping keys should match what the summary references
    assert "apps/web" in mapping
    assert "apps/api" in mapping
    assert "packages/auth" in mapping


def test_summarise_layout_includes_file_counts():
    files = [f"a/{i}.ts" for i in range(50)] + ["b/c.ts"]
    summary, _ = summarise_layout(files)
    assert "(50 files)" in summary
    assert "(1 files)" in summary


def test_summarise_layout_orders_largest_first():
    files = ["small/a.ts"] + [f"big/{i}.ts" for i in range(20)]
    summary, _ = summarise_layout(files)
    # First non-empty line should reference 'big'
    first_line = next(l for l in summary.split("\n") if l.strip())
    assert "big" in first_line


def test_summarise_layout_caps_at_max_lines():
    # 70 distinct top-level dirs
    files = [f"dir{i}/file.ts" for i in range(70)]
    summary, _ = summarise_layout(files, max_lines=20)
    lines = summary.split("\n")
    # 20 dir lines + 1 "..." footer
    assert any("more dirs" in l for l in lines)


def test_plan_buckets_falls_back_when_no_api(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    files = ["src/auth.py", "src/billing.py"]
    buckets = plan_buckets(files, api_key=None)
    # Single shared-infra bucket containing all files
    assert len(buckets) == 1
    assert buckets[0].name == "shared-infra"
    assert set(buckets[0].files) == set(files)


def test_plan_buckets_empty_input():
    assert plan_buckets([]) == []


def test_describe_buckets_format():
    bs = [
        Bucket(name="auth", dirs=["a", "b"], files=["x.ts", "y.ts"]),
        Bucket(name="billing", dirs=["c"], files=["z.ts"]),
    ]
    text = describe_buckets(bs)
    assert "2 buckets" in text
    assert "auth" in text
    assert "billing" in text


def test_bucket_dataclass_defaults():
    b = Bucket(name="x")
    assert b.dirs == []
    assert b.files == []
