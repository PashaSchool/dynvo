"""R2 parity: the indexed ``_filename_match`` must return IDENTICAL
results to the legacy per-call ``pathlib`` scan, on every fixture shape
we rely on — including the quirky ones (semantics are replicated
exactly, even where they look buggy: e.g. the step-2 replacement only
rewrites the FIRST matching needle and still requires the file in
``/src/``'s mirrored directory).
"""

from __future__ import annotations

from pathlib import Path

from faultline.analyzer.test_mapper import (
    _filename_match,
    _strip_test_suffix,
    build_name_index,
)


def _legacy_step4(test_path: str, source_set: set[str]) -> str | None:
    """The pre-R2 step-4 semantics, verbatim (Path per source file)."""
    p_name = Path(test_path).name
    base, exts = _strip_test_suffix(p_name)
    if base is None or not exts:
        return None
    for ext in exts:
        target_name = f"{base}{ext}"
        matches = [src for src in source_set if Path(src).name == target_name]
        if matches:
            return min(matches)
    return None


FIXTURES: list[set[str]] = [
    # simple sibling
    {"src/utils.ts", "src/other.ts"},
    # nested dirs + same basename in many places (min() tiebreak)
    {
        "apps/web/src/config.ts",
        "apps/admin/src/config.ts",
        "packages/core/config.ts",
        "packages/core/index.ts",
    },
    # case sensitivity: Utils vs utils are DIFFERENT basenames
    {"src/Utils.ts", "lib/utils.ts"},
    # tests/ → src/ mirror
    {"src/parser.py", "src/lexer.py"},
    # rust convention
    {"src/parser.rs", "src/lib.rs"},
    # go
    {"pkg/auth/auth.go", "pkg/auth/token.go"},
    # jsx/tsx candidate-extension ordering
    {"src/widget.tsx", "src/widget.ts", "src/widget.jsx"},
    # empty
    set(),
]

TEST_PATHS = [
    "src/utils.test.ts",
    "utils.test.ts",
    "e2e/config.test.ts",
    "tests/config.test.ts",
    "__tests__/config.spec.ts",
    "tests/test_parser.py",
    "tests/parser_test.py",
    "tests/parser.rs",
    "pkg/auth/auth_test.go",
    "spec/widget.spec.tsx",
    "widget.test.js",
    "deep/nested/dir/utils.spec.ts",
    "src/Utils.test.ts",
    "utils.e2e.mjs",
    "not-a-test.ts",
    "test_noext",
]


def test_indexed_filename_match_parity_with_legacy() -> None:
    for source_set in FIXTURES:
        idx = build_name_index(source_set)
        for tp in TEST_PATHS:
            legacy = _filename_match(tp, set(source_set))
            indexed = _filename_match(tp, set(source_set), name_index=idx)
            assert indexed == legacy, (
                f"parity break: {tp!r} over {sorted(source_set)!r}: "
                f"indexed={indexed!r} legacy={legacy!r}"
            )


def test_step4_min_tiebreak_preserved_with_index() -> None:
    sources = {
        "zzz/aaa/config.ts",
        "aaa/zzz/config.ts",
        "mmm/config.ts",
    }
    idx = build_name_index(sources)
    got = _filename_match("e2e/config.test.ts", sources, name_index=idx)
    assert got == "aaa/zzz/config.ts"  # lexicographically smallest full path
    assert got == _legacy_step4("e2e/config.test.ts", sources)


def test_name_index_matches_path_name_semantics() -> None:
    sources = {
        "a/b/c.ts", "c.ts", "./d.ts", "x//e.ts", "weird\\name.ts",
    }
    idx = build_name_index(sources)
    for src in sources:
        assert src in idx[Path(src).name]
