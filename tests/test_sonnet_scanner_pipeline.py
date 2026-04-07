"""Unit tests for the pre/post-processing helpers in sonnet_scanner.

These cover deltas D1, D2, D3, D8, D9 from docs/rewrite/sonnet_scanner_delta.md
without calling the LLM. The helpers are intentionally extracted from
``deep_scan`` so the validation primitives can be verified here with no
network, no API key, and sub-millisecond runtime.

Scenarios are modeled on the real Day 1 baseline failures:
  - fastapi: 21 phantom features from docs_src/tutorial00N paths
  - trpc:    www/* split into 8 features, 43 library flows
  - cal.com: vitest-mocks leaked as a feature
  - gin:     "root" bucket leaked as a feature
"""

from __future__ import annotations

import faultline.llm.sonnet_scanner as scanner
from faultline.llm.sonnet_scanner import (
    SonnetFeature,
    SonnetFlow,
    SonnetOpsResponse,
    _clean_inputs,
    _finalize_result,
)


# ── _clean_inputs: D2, D3 ────────────────────────────────────────────────


class TestCleanInputs:
    def test_filters_test_files_from_files_list(self) -> None:
        files = [
            "src/auth/login.ts",
            "src/auth/login.test.ts",
            "tests/unit/parser.py",
            "README.md",
        ]
        cleaned_files, _, _ = _clean_inputs(files, {})
        assert "src/auth/login.ts" in cleaned_files
        assert "README.md" in cleaned_files
        assert "src/auth/login.test.ts" not in cleaned_files
        assert "tests/unit/parser.py" not in cleaned_files

    def test_partitions_docs_files(self) -> None:
        files = [
            "fastapi/routing.py",
            "docs_src/tutorial001_py310/main.py",
            "docs_src/tutorial002_py310/app.py",
            "www/blog/post-1.mdx",
        ]
        cleaned_files, _, docs_files = _clean_inputs(files, {})
        assert cleaned_files == ["fastapi/routing.py"]
        assert len(docs_files) == 3
        assert all(("docs_src" in f) or ("www/" in f) for f in docs_files)

    def test_fastapi_regression_all_tutorials_go_to_docs(self) -> None:
        """fastapi baseline: 21 docs_src/tutorial00N dirs must land in docs bucket."""
        files = [
            "fastapi/routing.py",
            "fastapi/applications.py",
        ] + [
            f"docs_src/tutorial00{i}_py310/main.py"
            for i in range(1, 10)
        ]
        cleaned_files, _, docs_files = _clean_inputs(files, {})
        assert set(cleaned_files) == {"fastapi/routing.py", "fastapi/applications.py"}
        assert len(docs_files) == 9
        for f in docs_files:
            assert f.startswith("docs_src/")

    def test_drops_test_feature_name_from_candidates(self) -> None:
        """cal.com regression: vitest-mocks must not survive as a candidate."""
        candidates = {
            "authentication": ["src/auth/login.ts", "src/auth/signup.ts"],
            "vitest-mocks": ["packages/embeds/vitest-mocks/handler.ts"],
            "__tests__": ["packages/core/__tests__/parser.ts"],
        }
        _, cleaned_candidates, _ = _clean_inputs([], candidates)
        assert "authentication" in cleaned_candidates
        assert "vitest-mocks" not in cleaned_candidates
        assert "__tests__" not in cleaned_candidates

    def test_removes_docs_paths_from_candidate_buckets(self) -> None:
        """A candidate that mixes code + docs must keep only code paths."""
        candidates = {
            "auth": [
                "src/auth/login.ts",
                "src/auth/signup.ts",
                "docs/auth/guide.md",         # should be stripped
                "examples/auth/demo.ts",       # should be stripped
            ],
        }
        _, cleaned, _ = _clean_inputs([], candidates)
        assert cleaned["auth"] == ["src/auth/login.ts", "src/auth/signup.ts"]

    def test_removes_test_paths_from_candidate_buckets(self) -> None:
        candidates = {
            "auth": [
                "src/auth/login.ts",
                "src/auth/login.test.ts",
                "src/auth/__tests__/signup.ts",
            ],
        }
        _, cleaned, _ = _clean_inputs([], candidates)
        assert cleaned["auth"] == ["src/auth/login.ts"]

    def test_drops_candidates_that_become_empty(self) -> None:
        """A candidate whose every path is test/docs is dropped entirely."""
        candidates = {
            "auth": ["src/auth/login.ts"],
            "ghost": [
                "src/ghost/a.test.ts",
                "src/ghost/b.test.ts",
            ],
        }
        _, cleaned, _ = _clean_inputs([], candidates)
        assert "auth" in cleaned
        assert "ghost" not in cleaned

    def test_preserves_ordering_within_buckets(self) -> None:
        """_clean_inputs must not shuffle paths within a candidate."""
        candidates = {
            "auth": [
                "src/auth/z-signup.ts",
                "src/auth/a-login.ts",
                "src/auth/m-session.ts",
            ],
        }
        _, cleaned, _ = _clean_inputs([], candidates)
        assert cleaned["auth"] == [
            "src/auth/z-signup.ts",
            "src/auth/a-login.ts",
            "src/auth/m-session.ts",
        ]

    def test_empty_inputs(self) -> None:
        cleaned_files, cleaned_candidates, docs_files = _clean_inputs([], {})
        assert cleaned_files == []
        assert cleaned_candidates == {}
        assert docs_files == []


# ── _finalize_result: D1, D8, D9 ─────────────────────────────────────────


def _make_ops(features: list[tuple[str, list[str]]]) -> SonnetOpsResponse:
    """Build a SonnetOpsResponse with named features and dummy flows."""
    return SonnetOpsResponse(
        features=[
            SonnetFeature(
                name=name,
                description=f"desc for {name}",
                flows=[SonnetFlow(name=f"{name}-flow", description=f"{name} action")],
            )
            for name, _ in features
        ]
    )


class TestFinalizeResult:
    def setup_method(self) -> None:
        # Reset the module-global side channel between tests
        scanner._last_scan_result = None

    def test_attaches_documentation_bucket(self) -> None:
        """D2: docs files partitioned in _clean_inputs get reattached here."""
        result = {"auth": ["src/auth/login.ts"]}
        docs = ["docs/guide.md", "examples/sample.ts"]
        cleaned = _finalize_result(result, docs_files=docs, is_library=False)
        assert "documentation" in cleaned
        assert cleaned["documentation"] == ["docs/guide.md", "examples/sample.ts"]
        assert cleaned["auth"] == ["src/auth/login.ts"]

    def test_merges_docs_into_existing_bucket(self) -> None:
        """If LLM already created a 'documentation' feature, merge instead of clobbering."""
        result = {"documentation": ["docs/existing.md"]}
        docs = ["docs/new.md"]
        cleaned = _finalize_result(result, docs_files=docs, is_library=False)
        assert cleaned["documentation"] == ["docs/existing.md", "docs/new.md"]

    def test_no_docs_bucket_when_no_docs_files(self) -> None:
        result = {"auth": ["src/auth/login.ts"]}
        cleaned = _finalize_result(result, docs_files=[], is_library=False)
        assert "documentation" not in cleaned

    def test_canonicalizes_root_to_shared_infra(self) -> None:
        """gin regression: 'root' must become 'shared-infra'."""
        result = {
            "binding": ["binding/a.go"],
            "root": ["main.go", "helpers.go"],
        }
        cleaned = _finalize_result(result, docs_files=[], is_library=True)
        assert "root" not in cleaned
        assert "shared-infra" in cleaned
        assert set(cleaned["shared-infra"]) == {"main.go", "helpers.go"}

    def test_merges_canonical_duplicates(self) -> None:
        """If both 'root' and 'init' appear, they both land in shared-infra."""
        result = {
            "auth": ["src/auth/a.ts"],
            "root": ["main.ts"],
            "init": ["bootstrap.ts"],
            "shared-infra": ["utils/time.ts"],
        }
        cleaned = _finalize_result(result, docs_files=[], is_library=False)
        assert "root" not in cleaned
        assert "init" not in cleaned
        assert sorted(cleaned["shared-infra"]) == sorted([
            "main.ts", "bootstrap.ts", "utils/time.ts",
        ])

    def test_drops_phantom_empty_feature(self) -> None:
        result = {
            "auth": ["src/auth/login.ts"],
            "ghost": [],
        }
        cleaned = _finalize_result(result, docs_files=[], is_library=False)
        assert "ghost" not in cleaned
        assert "auth" in cleaned

    def test_drops_phantom_test_named_feature(self) -> None:
        """Belt-and-braces: even if a test-named feature slips past
        _clean_inputs (e.g. added by the LLM), drop it here."""
        result = {
            "auth": ["src/auth/login.ts"],
            "__tests__": ["something.ts"],
        }
        cleaned = _finalize_result(result, docs_files=[], is_library=False)
        assert "__tests__" not in cleaned
        assert "auth" in cleaned

    def test_is_library_strips_flows_from_side_channel(self) -> None:
        """D1: library repos must have 0 flows regardless of what Sonnet returned."""
        scanner._last_scan_result = _make_ops([
            ("binding", ["b.go"]),
            ("router", ["r.go"]),
        ])
        result = {"binding": ["b.go"], "router": ["r.go"]}

        _finalize_result(result, docs_files=[], is_library=True)

        assert scanner._last_scan_result is not None
        for feat in scanner._last_scan_result.features:
            assert feat.flows == []

    def test_non_library_preserves_flows(self) -> None:
        scanner._last_scan_result = _make_ops([("auth", ["a.ts"])])
        result = {"auth": ["a.ts"]}

        _finalize_result(result, docs_files=[], is_library=False)

        assert scanner._last_scan_result is not None
        assert len(scanner._last_scan_result.features[0].flows) == 1

    def test_canonicalizes_side_channel_feature_names(self) -> None:
        """get_deep_scan_flows matches by name; canonicalized result must
        stay in sync with _last_scan_result feature names."""
        scanner._last_scan_result = _make_ops([("root", ["main.go"])])
        result = {"root": ["main.go"]}

        cleaned = _finalize_result(result, docs_files=[], is_library=False)

        assert "shared-infra" in cleaned
        assert scanner._last_scan_result is not None
        assert scanner._last_scan_result.features[0].name == "shared-infra"

    def test_works_without_side_channel(self) -> None:
        """_finalize_result must not crash when _last_scan_result is None
        (e.g. when the LLM call itself failed but we still want to clean)."""
        scanner._last_scan_result = None
        result = {"auth": ["a.ts"], "root": ["main.ts"]}
        cleaned = _finalize_result(result, docs_files=[], is_library=True)
        assert "auth" in cleaned
        assert "shared-infra" in cleaned


# ── End-to-end flow on the extracted pipeline ───────────────────────────


class TestEndToEndWithoutLLM:
    """Simulate the pipeline around the LLM call: _clean_inputs → fake ops
    application → _finalize_result. Verifies the two helpers compose."""

    def setup_method(self) -> None:
        scanner._last_scan_result = None

    def test_fastapi_shape_without_llm(self) -> None:
        """fastapi: 3 code files + 9 tutorial files → 1 code feature + 1 docs
        feature, regardless of what the (mocked) LLM would do."""
        files = [
            "fastapi/routing.py",
            "fastapi/applications.py",
            "fastapi/dependencies.py",
        ] + [f"docs_src/tutorial00{i}_py310/main.py" for i in range(1, 10)]
        candidates = {
            "fastapi": ["fastapi/routing.py", "fastapi/applications.py", "fastapi/dependencies.py"],
        }

        cleaned_files, cleaned_candidates, docs_files = _clean_inputs(files, candidates)
        assert len(cleaned_files) == 3
        assert len(docs_files) == 9
        assert "fastapi" in cleaned_candidates

        # Pretend the LLM returned the 'fastapi' feature as-is
        scanner._last_scan_result = _make_ops([("fastapi", cleaned_candidates["fastapi"])])
        fake_result = dict(cleaned_candidates)

        final = _finalize_result(fake_result, docs_files=docs_files, is_library=True)
        assert set(final.keys()) == {"fastapi", "documentation"}
        assert len(final["documentation"]) == 9
        # Library → flows stripped
        assert scanner._last_scan_result is not None
        assert scanner._last_scan_result.features[0].flows == []
