"""Tests for :mod:`faultline.pipeline_v2.stage_6_4_framework_enrich` (Sprint C4)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.framework_linkers.base import FrameworkLink, FrameworkLinker
from faultline.models.types import Feature, FlowSymbolAttribution
from faultline.pipeline_v2.run_logger import StageLogger
from faultline.pipeline_v2.stage_6_4_framework_enrich import (
    EnrichmentResult,
    _attach_links_to_feature,
    _link_to_symbol_attribution,
    run_stage_6_4,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _ctx(tmp_path: Path, *, audited: str | None = "next-app-router") -> SimpleNamespace:
    return SimpleNamespace(
        repo_path=tmp_path,
        tracked_files=tuple(),
        run_dir=None,
        stack="next-app-router",
        audited_stack=audited,
        secondary_stacks=(),
        monorepo=False,
        workspaces=[],
    )


def _new_feature(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        layer="developer",
    )


def _log(tmp_path: Path) -> StageLogger:
    return StageLogger(tmp_path, 6, "framework_enrich_test")


# ── Fake linkers ─────────────────────────────────────────────────────────


class _FakeActiveLinker:
    """Always-active linker that emits ONE canned link per feature."""

    name = "fake-active"
    activation_keys: tuple[str, ...] = ("next-app-router",)

    def __init__(self, target_file: str = "app/api/x/route.ts") -> None:
        self._target_file = target_file

    def is_active(self, ctx) -> bool:  # noqa: ANN001
        return True

    def link_for_feature(self, feature, ctx, log) -> list[FrameworkLink]:  # noqa: ANN001
        return [
            FrameworkLink(
                source_file=feature.paths[0] if feature.paths else "<unknown>",
                source_symbol="<module>",
                source_line=1,
                target_file=self._target_file,
                target_symbol="POST",
                target_line_start=1,
                target_line_end=3,
                linker=self.name,
                link_kind="http-route",
                confidence=1.0,
                reason="fake",
            ),
        ]


class _FakeInactiveLinker:
    """Always-inactive linker — exercises the skipped_linkers path."""

    name = "fake-inactive"
    activation_keys: tuple[str, ...] = ("remix",)

    def is_active(self, ctx) -> bool:  # noqa: ANN001
        return False

    def link_for_feature(self, feature, ctx, log) -> list[FrameworkLink]:  # noqa: ANN001
        raise AssertionError("inactive linker must not be called")


class _RaisingLinker:
    """Linker whose link_for_feature raises — must NOT break the stage."""

    name = "raising"
    activation_keys: tuple[str, ...] = ("next-app-router",)

    def is_active(self, ctx) -> bool:  # noqa: ANN001
        return True

    def link_for_feature(self, feature, ctx, log) -> list[FrameworkLink]:  # noqa: ANN001
        raise RuntimeError("bang")


# ── Protocol satisfaction ───────────────────────────────────────────────


def test_fakes_satisfy_protocol() -> None:
    assert isinstance(_FakeActiveLinker(), FrameworkLinker)
    assert isinstance(_FakeInactiveLinker(), FrameworkLinker)


# ── _link_to_symbol_attribution + _attach_links_to_feature ──────────────


def test_link_to_symbol_attribution_encodes_kind_in_symbol() -> None:
    link = FrameworkLink(
        source_file="app/page.tsx",
        source_symbol="Page",
        source_line=10,
        target_file="app/api/rules/route.ts",
        target_symbol="POST",
        target_line_start=1,
        target_line_end=3,
        linker="nextjs-http-route",
        link_kind="http-route",
        confidence=1.0,
    )
    attr = _link_to_symbol_attribution(link)
    assert isinstance(attr, FlowSymbolAttribution)
    assert attr.file == "app/api/rules/route.ts"
    assert attr.symbol == "framework-link:http-route:POST"
    assert attr.role == "framework-link"
    assert (attr.line_start, attr.line_end) == (1, 3)


def test_attach_links_dedups_repeats() -> None:
    feature = _new_feature("rules", ["app/rules/page.tsx"])
    link = FrameworkLink(
        source_file="app/rules/page.tsx", source_symbol="<module>", source_line=1,
        target_file="app/api/rules/route.ts", target_symbol="POST",
        target_line_start=1, target_line_end=3,
        linker="nextjs-http-route", link_kind="http-route", confidence=1.0,
    )
    added_first = _attach_links_to_feature(feature, [link, link])
    assert added_first == 1
    # Re-running with the same link adds nothing.
    added_second = _attach_links_to_feature(feature, [link])
    assert added_second == 0
    assert len(feature.symbol_attributions) == 1


# ── run_stage_6_4 integration ───────────────────────────────────────────


def test_run_stage_6_4_no_linkers_returns_empty_telemetry(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    features = [_new_feature("a", ["app/a/page.tsx"])]
    with _log(tmp_path) as log:
        result = run_stage_6_4(ctx, features, log, linkers=[])
    assert isinstance(result, EnrichmentResult)
    assert result.active_linkers == []
    assert result.skipped_linkers == []
    assert result.links_emitted_total == 0
    assert result.enriched_features == features


def test_run_stage_6_4_active_linker_attaches_links(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    features = [_new_feature("rules", ["app/rules/page.tsx"])]
    linker = _FakeActiveLinker()
    with _log(tmp_path) as log:
        result = run_stage_6_4(ctx, features, log, linkers=[linker])
    assert result.active_linkers == ["fake-active"]
    assert result.skipped_linkers == []
    assert result.links_emitted_total == 1
    assert len(features[0].symbol_attributions) == 1
    assert features[0].symbol_attributions[0].role == "framework-link"
    assert "fake-active" in result.per_linker
    assert result.per_linker["fake-active"]["links_attached_to_features"] == 1


def test_run_stage_6_4_skips_inactive_linker(tmp_path: Path) -> None:
    """Inactive linker must NEVER have link_for_feature called."""
    ctx = _ctx(tmp_path)
    features = [_new_feature("a", ["app/a/page.tsx"])]
    inactive = _FakeInactiveLinker()
    with _log(tmp_path) as log:
        result = run_stage_6_4(ctx, features, log, linkers=[inactive])
    assert result.active_linkers == []
    assert [s["name"] for s in result.skipped_linkers] == ["fake-inactive"]
    assert result.links_emitted_total == 0
    assert features[0].symbol_attributions == []


def test_run_stage_6_4_isolates_raising_linker(tmp_path: Path) -> None:
    """A raising linker on one feature must not abort the stage."""
    ctx = _ctx(tmp_path)
    features = [
        _new_feature("a", ["app/a/page.tsx"]),
        _new_feature("b", ["app/b/page.tsx"]),
    ]
    raiser = _RaisingLinker()
    good = _FakeActiveLinker(target_file="app/api/y/route.ts")
    with _log(tmp_path) as log:
        result = run_stage_6_4(ctx, features, log, linkers=[raiser, good])
    # raiser is active but every per-feature call raises → 0 links from it.
    # good still emits one link per feature.
    assert "fake-active" in result.active_linkers
    assert "raising" in result.active_linkers
    assert result.links_emitted_total == 2  # one per feature from "good"
    assert features[0].symbol_attributions and features[1].symbol_attributions


def test_run_stage_6_4_backward_compat_when_no_linker_active(tmp_path: Path) -> None:
    """Non-Next stack with only an inactive linker → features unchanged."""
    ctx = _ctx(tmp_path, audited="remix")
    features = [_new_feature("a", ["app/a.tsx"])]
    inactive = _FakeInactiveLinker()
    with _log(tmp_path) as log:
        result = run_stage_6_4(ctx, features, log, linkers=[inactive])
    assert result.active_linkers == []
    assert result.links_emitted_total == 0
    assert result.enriched_features[0].symbol_attributions == []


def test_telemetry_serialisation_shape(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    features = [_new_feature("rules", ["app/rules/page.tsx"])]
    with _log(tmp_path) as log:
        result = run_stage_6_4(ctx, features, log, linkers=[_FakeActiveLinker()])
    tel = result.telemetry()
    assert tel["stage"] == "6.4-framework-enrich"
    assert set(tel.keys()) >= {
        "stage", "elapsed_sec", "active_linkers", "skipped_linkers",
        "per_linker", "links_emitted_total",
    }
