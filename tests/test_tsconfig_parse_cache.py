"""R6 — content-hash cache on ``_parse_jsonc``.

Stage 2.6 and Stage 6.3 both build the tsconfig alias map, re-parsing
the same files with the pure-Python json5 PEG parser (6.5s profiled on
documenso). The cache is keyed by content hash, in-process only, and
must be MUTATION-SAFE: a caller mutating a returned dict must never see
its mutation reflected in a later cache hit.
"""

from __future__ import annotations

from faultline.analyzer.tsconfig_paths import (
    _parse_jsonc,
    _parse_jsonc_uncached,
)

JSONC = """
{
  // line comment
  "extends": "./base.json",
  "compilerOptions": {
    /* block comment */
    "baseUrl": ".",
    "paths": {"@/*": ["./src/*"],},
  },
}
"""


def test_cached_parse_equals_uncached() -> None:
    assert _parse_jsonc(JSONC) == _parse_jsonc_uncached(JSONC)


def test_cache_hit_returns_equal_but_independent_object() -> None:
    a = _parse_jsonc(JSONC)
    b = _parse_jsonc(JSONC)
    assert a == b
    assert a is not b
    # Mutation of one result must not poison the cache.
    a["compilerOptions"]["paths"]["@/*"] = ["POISON"]
    c = _parse_jsonc(JSONC)
    assert c["compilerOptions"]["paths"]["@/*"] == ["./src/*"]
    # Nested containers are independent too.
    assert c["compilerOptions"] is not b["compilerOptions"]


def test_parse_failure_is_cached_as_none() -> None:
    bad = "{ definitely not json5 ]]]"
    assert _parse_jsonc(bad) is None
    assert _parse_jsonc(bad) is None
