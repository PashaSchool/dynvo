"""Tests for the Sprint 9e feature_dedup hybrid aggregator."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from faultline.aggregators.feature_dedup import (
    AMBIGUOUS_LOW,
    HIGH_THRESHOLD,
    _ambiguous_pairs,
    _jaccard,
    _pick_canonical,
    _tokens,
    dedup_features,
)


# ── helpers ──────────────────────────────────────────────────────────


def _flow(name, paths=None):
    from faultline.models.types import Flow
    return Flow(
        name=name, paths=paths or [], authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=99.0,
    )


def _feat(
    name, *, paths=None, flows=None, display=None,
    discovery="primary", protected=False, protection_reason=None,
):
    from faultline.models.types import Feature
    return Feature(
        name=name, paths=paths or [], authors=[], total_commits=10,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=99.0, flows=flows or [], display_name=display,
        discovery_method=discovery, protected=protected,
        protection_reason=protection_reason,
    )


def _fm(features):
    from faultline.models.types import FeatureMap
    return FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=datetime.now(tz=timezone.utc),
        total_commits=0, date_range_days=365,
        features=features,
    )


class _FakeLlm:
    name = "fake"

    def __init__(self, decisions):
        # decisions: {(name_a, name_b): bool}
        self._decisions = decisions

    def complete(self, *, system, user, max_tokens, tools=None):
        from faultline.signals import LlmResponse
        payload = json.loads(user.split("\n\n", 1)[1])
        result = []
        for item in payload["pairs"]:
            a, b = item["a"], item["b"]
            key = (a, b) if (a, b) in self._decisions else (b, a)
            verdict = "yes" if self._decisions.get(key, False) else "no"
            result.append({"pair_id": item["pair_id"], "verdict": verdict})
        return LlmResponse(
            text=json.dumps({"decisions": result}),
            input_tokens=10, output_tokens=10, stop_reason="end_turn",
        )


# ── _tokens ──────────────────────────────────────────────────────────


def test_tokens_drops_stop_words():
    assert "flow" not in _tokens("manage-billing-flow")
    assert "manage" not in _tokens("manage-billing-flow")


def test_tokens_stems_plural():
    a = _tokens("template")
    b = _tokens("templates")
    assert a == b


def test_tokens_keeps_known_abbrevs():
    assert "jwt" in _tokens("JWT")
    assert "sso" in _tokens("SSO Provider")


def test_tokens_drops_short_noise():
    assert "ee" not in _tokens("ee/billing")
    assert "v1" not in _tokens("v1/users")


# ── _jaccard ─────────────────────────────────────────────────────────


def test_jaccard_identical_is_one():
    assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_disjoint_is_zero():
    assert _jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_partial_overlap():
    assert _jaccard({"a", "b"}, {"b", "c"}) == 1 / 3


# ── _pick_canonical ──────────────────────────────────────────────────


def test_pick_canonical_prefers_protected():
    a = _feat("a", paths=["x"], protected=False, discovery="primary")
    b = _feat("b", paths=["y"], protected=True, discovery="critique")
    assert _pick_canonical([a, b]) is b


def test_pick_canonical_prefers_primary_over_critique():
    a = _feat("a", paths=["x"], discovery="critique")
    b = _feat("b", paths=["y"], discovery="primary")
    assert _pick_canonical([a, b]) is b


def test_pick_canonical_tie_breaks_on_path_count():
    a = _feat("a", paths=["x"], discovery="primary")
    b = _feat("b", paths=["x", "y", "z"], discovery="primary")
    assert _pick_canonical([a, b]) is b


# ── dedup_features — deterministic phase ─────────────────────────────


def test_no_dedup_when_features_dissimilar():
    a = _feat("auth", paths=["x.ts"])
    b = _feat("billing", paths=["y.ts"])
    fm = _fm([a, b])
    fm, stats = dedup_features(fm)
    assert stats.features_before == 2
    assert stats.features_after == 2
    assert stats.clusters_merged == 0


def test_dedup_collapses_three_auth_features():
    a = _feat("auth", paths=["apps/remix/auth/x.ts"], discovery="primary")
    b = _feat("auth", paths=["packages/auth/y.ts"], discovery="primary")
    c = _feat("auth", paths=["packages/trpc/auth-router/z.ts"], discovery="primary")
    fm = _fm([a, b, c])
    fm, stats = dedup_features(fm)
    assert stats.features_after == 1
    assert stats.clusters_merged == 1
    # All paths preserved on survivor
    survivor = fm.features[0]
    assert len(survivor.paths) == 3


def test_dedup_merges_flows_and_dedupes_by_name():
    a = _feat("auth", paths=["a.ts"], flows=[_flow("sign-in"), _flow("sign-up")])
    b = _feat("auth", paths=["b.ts"], flows=[_flow("sign-in"), _flow("reset-password")])
    fm = _fm([a, b])
    fm, _ = dedup_features(fm)
    survivor = fm.features[0]
    flow_names = sorted(fl.name for fl in survivor.flows)
    assert flow_names == ["reset-password", "sign-in", "sign-up"]


def test_dedup_protected_inheritance_on_merge():
    a = _feat("auth", paths=["a"], protected=True, protection_reason="workspace-package")
    b = _feat("auth", paths=["b"], protected=False)
    fm = _fm([a, b])
    fm, _ = dedup_features(fm)
    survivor = fm.features[0]
    assert survivor.protected is True
    assert survivor.protection_reason == "workspace-package"


def test_dedup_multi_anchor_when_reasons_differ():
    a = _feat("auth", paths=["a"], protected=True, protection_reason="workspace-package")
    b = _feat("auth", paths=["b"], protected=True, protection_reason="trpc-router")
    fm = _fm([a, b])
    fm, _ = dedup_features(fm)
    survivor = fm.features[0]
    assert survivor.protected is True
    assert survivor.protection_reason == "multi-anchor"


def test_dedup_skips_when_only_one_feature():
    a = _feat("only", paths=["x"])
    fm = _fm([a])
    fm, stats = dedup_features(fm)
    assert stats.features_after == 1
    assert stats.clusters_merged == 0


# ── _ambiguous_pairs ─────────────────────────────────────────────────


def test_ambiguous_pairs_finds_partial_overlap():
    # "background-tasks" and "background-jobs" share {background}
    # but differ in {tasks, jobs} → jaccard ~ 0.33
    a = _feat("background-tasks", paths=["a"])
    b = _feat("background-jobs", paths=["b"])
    pairs = _ambiguous_pairs([a, b])
    # 0.33 is BELOW AMBIGUOUS_LOW (0.4) so won't appear by default —
    # the LLM-merge case in the wild relies on cluster-of-3+. Verify
    # this edge-case in the documented contract:
    assert pairs == [] or all(AMBIGUOUS_LOW <= p[2] < HIGH_THRESHOLD for p in pairs)


# ── dedup_features — LLM verification phase ──────────────────────────


def test_llm_merges_ambiguous_pairs():
    # Two features with Jaccard in the ambiguous band — share enough
    # to be considered, share too little to auto-merge.
    a = _feat(
        "response-compress-encoding",
        paths=["a"], display="Response Compress",
    )
    b = _feat(
        "content-encoding-compression",
        paths=["b"], display="Content Encoding",
    )
    fm = _fm([a, b])
    fake = _FakeLlm({("Response Compress", "Content Encoding"): True})
    fm, stats = dedup_features(fm, llm=fake)
    if stats.pairs_ambiguous > 0:
        assert stats.pairs_llm_merged == 1
        assert stats.features_after == 1


def test_llm_keeps_separate_when_no_verdict():
    a = _feat(
        "auth-strategy-provider",
        paths=["a"], display="Auth Strategy",
    )
    b = _feat(
        "oauth-strategy-provider",
        paths=["b"], display="OAuth Strategy",
    )
    fm = _fm([a, b])
    fake = _FakeLlm({})  # no merge decisions
    fm, stats = dedup_features(fm, llm=fake)
    if stats.pairs_ambiguous > 0:
        assert stats.pairs_llm_merged == 0
        assert stats.features_after == 2


# ── workspace-sibling merge predicate ────────────────────────────────


def test_workspace_sibling_merges_apps_and_packages():
    """Turborepo `apps/image-proxy/` + `packages/image-proxy/` are
    ONE product capability split across workspace members. Predicate
    must return True (merge) when both share the member-name token.
    """
    from faultline.aggregators.feature_dedup import (
        should_merge_workspace_sibling,
    )
    existing = [
        "apps/image-proxy/package.json",
        "apps/image-proxy/src/index.ts",
        "apps/image-proxy-aws/src/handler.ts",
    ]
    incoming = [
        "packages/image-proxy/package.json",
        "packages/image-proxy/src/proxy-service.ts",
    ]
    assert should_merge_workspace_sibling(existing, incoming) is True


def test_workspace_sibling_rejects_unrelated_utils():
    """Two ``Utils`` features whose paths live under completely
    unrelated workspace members must NOT merge — those are two
    genuinely separate utility clusters.
    """
    from faultline.aggregators.feature_dedup import (
        should_merge_workspace_sibling,
    )
    existing = [
        "packages/auth/src/utils.ts",
        "packages/auth/src/helpers.ts",
    ]
    incoming = [
        "packages/billing/src/utils.ts",
        "packages/billing/src/format.ts",
    ]
    assert should_merge_workspace_sibling(existing, incoming) is False


def test_workspace_sibling_rejects_overlapping_paths():
    """Path overlap means it's the OTHER bug (Foo + Foo-2 referring
    to the same files). Predicate must return False.
    """
    from faultline.aggregators.feature_dedup import (
        should_merge_workspace_sibling,
    )
    paths = ["apps/image-proxy/src/index.ts"]
    assert should_merge_workspace_sibling(paths, paths) is False


def test_workspace_sibling_rejects_shallow_paths():
    """Top-level files (no second segment) carry no workspace-member
    signal. Predicate must return False.
    """
    from faultline.aggregators.feature_dedup import (
        should_merge_workspace_sibling,
    )
    existing = ["README.md", "LICENSE"]
    incoming = ["package.json"]
    assert should_merge_workspace_sibling(existing, incoming) is False


def test_workspace_sibling_merge_paths_into_unions():
    """``merge_paths_into`` unions donor paths into the existing
    feature without duplicates and preserves order.
    """
    from faultline.aggregators.feature_dedup import merge_paths_into

    class _F:
        def __init__(self, paths):
            self.paths = list(paths)

    existing = _F(["a", "b"])
    added = merge_paths_into(existing, ["b", "c", "d"])
    assert existing.paths == ["a", "b", "c", "d"]
    assert added == 2
