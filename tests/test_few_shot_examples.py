"""Sprint 18 scaffold — few-shot example picker tests.

These verify the structural contract; example content gets filled in
during S18 Day 1 from the S17 ground-truth corpus, and corpus-specific
tests live in tests/eval/.
"""

from __future__ import annotations

from faultline.llm.few_shot_examples import (
    EXAMPLES_BY_STACK,
    FewShotExample,
    _estimate_tokens,
    build_examples_block,
    pick_examples,
)


def test_examples_by_stack_keys_align_with_ground_truth():
    """Every stack tag we use in ground_truth.json must have an entry."""
    expected_keys = {
        "next-monorepo", "next-app-router", "node-monorepo",
        "vue-spa", "vue-nuxt-monorepo",
        "python-flat", "python-modules",
        "go-modular", "rust-modular", "rails-app",
        "mixed",
    }
    assert expected_keys.issubset(set(EXAMPLES_BY_STACK))


def test_pick_examples_returns_string_and_list():
    block, repos = pick_examples("next-monorepo")
    assert isinstance(block, str)
    assert isinstance(repos, list)


def test_pick_examples_falls_back_to_mixed_when_stack_unknown():
    block, repos = pick_examples("unknown-stack")
    # Mixed fallback list is empty in scaffold — that's OK, picker
    # should not crash.
    assert block == ""
    assert repos == []


def test_pick_examples_respects_token_budget():
    block, repos = pick_examples("next-monorepo", max_tokens=100)
    # 100-token budget is way below any single example — should pick none.
    assert block == ""
    assert repos == []


def test_pick_examples_respects_max_count():
    # Force a stack that has at least one example, ensure picker stops at 1.
    block, repos = pick_examples("vue-spa", max_count=1)
    assert len(repos) <= 1


def test_few_shot_example_render_includes_repo_and_stack_tags():
    ex = FewShotExample(
        stack="test-stack",
        repo="test-repo",
        file_paths_sample=["a.ts"],
        expected_output={"features": []},
    )
    out = ex.render()
    assert "<repo>test-repo</repo>" in out
    assert "<stack>test-stack</stack>" in out
    assert "<input_files>" in out
    assert "<expected_output>" in out
    assert "</example>" in out


def test_build_examples_block_returns_empty_when_no_examples():
    block, repos = build_examples_block("rails-app")  # mixed fallback empty
    assert block == ""
    assert repos == []


def test_build_examples_block_includes_intro_when_examples_exist():
    block, repos = build_examples_block("vue-spa")
    if repos:  # vue-spa has uptime-kuma example
        assert "## Examples" in block
        assert "uptime-kuma" in block


def test_estimate_tokens_is_monotonic():
    short = _estimate_tokens("hello world")
    long = _estimate_tokens("hello world" * 100)
    assert long > short
    assert short >= 1
