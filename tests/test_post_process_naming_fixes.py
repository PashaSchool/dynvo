"""Tests for naming-quality fixes in faultline.analyzer.post_process.

Covers the 4 deterministic engine fixes shipped after the prompt-A/B was
discarded (recall regressions):

- Fix A: drop features with empty / whitespace-only ``name``.
- Fix B: drop ``*/uncategorized`` even when ``protected=True``
  (including multi-slash variants like ``web/web/uncategorized``).
- Fix C: drop demo / references / examples / samples packages.
- Fix D: slugify final ``feature.name``, preserving ``display_name``
  when the original carried Title Case or whitespace.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.analyzer.post_process import (
    _slugify_names,
    drop_noise_features,
)
from faultline.models.types import Feature, FeatureMap, Flow


def _f(
    name: str,
    paths: list[str] | None = None,
    *,
    commits: int = 10,
    bug_fixes: int = 0,
    flows: list[Flow] | None = None,
    protected: bool = False,
    display_name: str | None = None,
) -> Feature:
    return Feature(
        name=name,
        display_name=display_name,
        paths=paths or ["src/foo.py", "src/bar.py", "src/baz.py"],
        authors=[],
        total_commits=commits,
        bug_fixes=bug_fixes,
        bug_fix_ratio=bug_fixes / max(commits, 1),
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=flows or [],
        protected=protected,
    )


def _flow(name: str) -> Flow:
    # Minimal valid Flow shape. Match existing Flow contract.
    return Flow(
        name=name,
        paths=["src/foo.py"],
        authors=[],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
    )


class TestEmptyName:
    def test_empty_name_dropped(self):
        feats = [_f("", paths=["src/x.py"]), _f("real-feature")]
        cleaned, dropped = drop_noise_features(feats)
        names = [f.name for f in cleaned]
        assert "" not in names
        assert "real-feature" in names
        assert any(reason == "empty name" for _, reason, _ in dropped)

    def test_whitespace_only_name_dropped(self):
        feats = [_f("   "), _f("real")]
        cleaned, _ = drop_noise_features(feats)
        assert all(f.name.strip() for f in cleaned)

    def test_empty_name_even_when_protected(self):
        feats = [_f("", protected=True, paths=["src/a.py"])]
        cleaned, dropped = drop_noise_features(feats)
        assert cleaned == []
        assert dropped and dropped[0][1] == "empty name"


class TestMultiSlashUncategorizedDropped:
    def test_multi_slash_uncategorized_dropped_even_when_protected(self):
        feats = [
            _f(
                "web/web/uncategorized",
                paths=["web/web/a.ts", "web/web/b.ts", "web/web/c.ts"],
                protected=True,
                flows=[_flow(f"flow-{i}") for i in range(5)],
            ),
            _f("auth"),
        ]
        cleaned, dropped = drop_noise_features(feats)
        names = [f.name for f in cleaned]
        assert "web/web/uncategorized" not in names
        assert "auth" in names
        assert any(
            reason == "uncategorized catch-all" for _, reason, _ in dropped
        )

    def test_plain_uncategorized_still_dropped(self):
        feats = [_f("uncategorized")]
        cleaned, _ = drop_noise_features(feats)
        assert cleaned == []


class TestDemoPackageDropped:
    @pytest.mark.parametrize(
        "name",
        [
            "references-hello-world",
            "examples-foo",
            "example-cli",
            "demo-bar",
            "references/sdk",
            "examples/quickstart",
            "samples-python",
        ],
    )
    def test_demo_dropped(self, name: str):
        feats = [_f(name), _f("real-product")]
        cleaned, dropped = drop_noise_features(feats)
        names = [f.name for f in cleaned]
        assert name not in names
        assert "real-product" in names
        assert any(reason == "demo/example package" for _, reason, _ in dropped)

    def test_legit_names_not_caught(self):
        # Should NOT eat names that merely contain the substring.
        feats = [
            _f("documentation"),
            _f("sample-collector"),  # not a demo prefix
            _f("referenced-data"),  # not "references-" prefix
        ]
        cleaned, _ = drop_noise_features(feats)
        names = [f.name for f in cleaned]
        assert "documentation" in names
        assert "sample-collector" in names
        assert "referenced-data" in names


class TestSlugify:
    def test_title_case_slugified_display_name_preserved(self):
        feats = [_f("Web App Shell & Onboarding")]
        out, dropped = _slugify_names(feats)
        assert dropped == []
        assert out[0].name == "web-app-shell-onboarding"
        assert out[0].display_name == "Web App Shell & Onboarding"

    def test_dotted_route_slugified(self):
        feats = [_f("webapp/resources.account.mfa.setup")]
        out, _ = _slugify_names(feats)
        assert out[0].name == "webapp-resources-account-mfa-setup"

    def test_collision_resolves_with_suffix(self):
        feats = [_f("Auth", paths=["src/a.py"]), _f("auth", paths=["src/b.py"])]
        out, _ = _slugify_names(feats)
        names = [f.name for f in out]
        # The plain-kebab "auth" hits the fast path and wins the bare
        # slug; the Title-Case "Auth" gets a numeric suffix. Order in
        # the input determines which one keeps the bare slug: "Auth"
        # comes first so it claims "auth", then plain "auth" collides
        # and becomes "auth-2".
        assert sorted(names) == ["auth", "auth-2"]

    def test_existing_kebab_unchanged(self):
        f = _f("surveys")
        out, _ = _slugify_names([f])
        assert out[0].name == "surveys"
        # display_name not synthesised for already-clean kebab names.
        assert out[0].display_name is None

    def test_pure_nonalnum_dropped(self):
        feats = [_f("---", paths=["src/x.py"]), _f("real")]
        out, dropped = _slugify_names(feats)
        names = [f.name for f in out]
        assert "real" in names
        assert len(dropped) == 1
        assert dropped[0][1] == "unslugifiable name"

    def test_uppercase_only_slugified_with_display_name(self):
        feats = [_f("Billing")]
        out, _ = _slugify_names(feats)
        assert out[0].name == "billing"
        assert out[0].display_name == "Billing"
