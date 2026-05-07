"""Sprint 19.5 — feature_compaction unit tests."""

from __future__ import annotations

from faultline.analyzer.feature_compaction import (
    LAYER_DROP_THRESHOLD,
    compact,
)


def _ft(name: str, n_paths: int = 10, **kw) -> dict:
    paths = [f"src/{name}/file{i}.py" for i in range(n_paths)]
    return {"name": name, "paths": paths, **kw}


def test_drops_commit_verbs():
    feats = [
        _ft("auth"),
        _ft("improvement", 100),
        _ft("fix", 50),
        _ft("billing"),
    ]
    kept = compact(feats)
    names = {f["name"] for f in kept}
    assert names == {"auth", "billing"}


def test_drops_test_buckets():
    feats = [_ft("api-tests", 50), _ft("auth")]
    kept = compact(feats)
    assert len(kept) == 1 and kept[0]["name"] == "auth"


def test_drops_small_layer_features():
    feats = [
        _ft("ui", 100),       # below threshold — drop
        _ft("frontend", 50),  # below threshold — drop
        _ft("billing", 30),
    ]
    kept = compact(feats)
    names = {f["name"] for f in kept}
    assert names == {"billing"}


def test_keeps_large_layer_features():
    """A repo whose primary feature is literally 'ui' (e.g. design system)
    keeps it when the path count is above the layer-drop threshold."""
    feats = [
        _ft("ui", LAYER_DROP_THRESHOLD + 50),
        _ft("billing", 30),
    ]
    kept = compact(feats)
    names = {f["name"] for f in kept}
    assert names == {"ui", "billing"}


def test_does_not_mutate_input():
    feats = [_ft("auth"), _ft("improvement", 100)]
    original_count = len(feats)
    compact(feats)
    assert len(feats) == original_count


def test_returns_stats_when_requested():
    feats = [_ft("auth"), _ft("improvement", 100), _ft("fix", 50)]
    kept, stats = compact(feats, return_stats=True)
    assert stats["n_kept"] == 1
    assert stats["n_dropped"] == 2
    assert stats["paths_dropped"] == 150
    dropped_names = {d[0] for d in stats["dropped"]}
    assert dropped_names == {"improvement", "fix"}


def test_handles_empty_input():
    kept = compact([])
    assert kept == []


def test_drops_empty_name_features():
    feats = [{"name": "", "paths": ["x.py"]}, _ft("auth")]
    kept = compact(feats)
    assert len(kept) == 1 and kept[0]["name"] == "auth"


def test_case_insensitive_match():
    feats = [_ft("Improvement", 100), _ft("UTILS", 20), _ft("auth")]
    kept = compact(feats)
    assert {f["name"] for f in kept} == {"auth"}


# ── Reattribute tests ──────────────────────────────────────────────────


from faultline.analyzer.feature_compaction import (
    _merge_into, _similarity, _tokens, reattribute,
)


def test_tokens_strips_stopwords():
    assert _tokens("api-app-web") == set()
    assert _tokens("secret-blind-index") == {"secret", "blind", "index"}
    assert _tokens("user-auth") == {"user", "auth"}


def test_similarity_high_for_shared_token():
    a = {"name": "secret-blind-index", "paths": ["src/secrets/x.py"]}
    b = {"name": "secret-manager", "paths": ["src/secrets/y.py"]}
    assert _similarity(a, b) > 0.5


def test_similarity_low_for_unrelated():
    """Unrelated names with shared 'src/' top dir share half-prefix only;
    final score should still be modest (<0.3) because token_score=0."""
    a = {"name": "billing", "paths": ["src/billing/x.py"]}
    b = {"name": "auth", "paths": ["src/auth/y.py"]}
    assert _similarity(a, b) < 0.3


def test_merge_sums_paths_commits_bugs():
    target = {
        "name": "secrets", "paths": ["a.py"],
        "total_commits": 100, "bug_fixes": 10, "bug_fix_ratio": 0.1,
        "flows": [{"name": "f1"}],
    }
    source = {
        "paths": ["b.py"],
        "total_commits": 50, "bug_fixes": 5,
        "flows": [{"name": "f2"}, {"name": "f1"}],  # f1 dup
    }
    _merge_into(target, source)
    assert set(target["paths"]) == {"a.py", "b.py"}
    assert target["total_commits"] == 150
    assert target["bug_fixes"] == 15
    assert target["bug_fix_ratio"] == 15 / 150
    assert {f["name"] for f in target["flows"]} == {"f1", "f2"}


def test_reattribute_merges_similar_into_top_n():
    feats = [
        _ft("secret-manager", 100),
        _ft("auth", 80),
        _ft("billing", 50),
        _ft("secret-blind-index", 20),
        _ft("secret-rotations", 15),
        _ft("auth-helpers", 5),
    ]
    kept, stats = reattribute(feats, top_n=3)
    names = {k["name"] for k in kept}
    assert names == {"secret-manager", "auth", "billing"}
    # secret-blind-index + secret-rotations should merge into secret-manager
    secret = next(k for k in kept if k["name"] == "secret-manager")
    assert len(secret["paths"]) >= 100 + 20 + 15  # original + merged
    assert stats["merged"] >= 2


def test_reattribute_hard_drops_unrelated_tail():
    feats = [
        _ft("secret-manager", 100),
        _ft("auth", 80),
        _ft("zzz-unrelated-feature-xyz", 10),
    ]
    kept, stats = reattribute(feats, top_n=2, min_similarity=0.3)
    assert stats["hard_dropped"] == 1
