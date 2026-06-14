"""Tests for Stage 8.6.5 — shared-scaffold filter.

Synthetic, neutral fixture names (per memory/rule-no-repo-specific-paths).
Verifies the BOTH-conditions predicate (scaffold-location AND high-fan-in), the
domain-file protection (high-fan-in non-scaffold kept), the structural-floor cap,
that workspace anchors are untouched, and the env toggle.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature
from faultline.pipeline_v2.stage_8_6_5_scaffold_filter import (
    _is_scaffold_path,
    filter_shared_scaffold,
)

_WS = "[package] workspace anchor 'frontend' from monorepo package 'frontend'"
_ROUTE = "[route] route convention slug {0!r} derived from 1 routing file(s)"


def _feat(name, paths, *, description=None):
    return Feature(
        name=name, description=description, paths=list(paths), authors=[],
        total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=100.0,
    )


def _route(name, paths):
    return _feat(name, paths, description=_ROUTE.format(name))


def test_is_scaffold_path() -> None:
    assert _is_scaffold_path("packages/lib/format.ts")
    assert _is_scaffold_path("src/ui/Button.tsx")
    assert _is_scaffold_path("app/hooks/useX.ts")
    assert _is_scaffold_path("src/i18n/en.ts")
    assert _is_scaffold_path("frontend/src/components/Modal.tsx")
    # domain layers are NOT scaffold
    assert not _is_scaffold_path("backend/services/auth.ts")
    assert not _is_scaffold_path("backend/db/models/user.ts")
    assert not _is_scaffold_path("app/routes/secret.ts")


def test_demotes_shared_scaffold_only() -> None:
    # `lib/util.ts` is scaffold AND shared by 4 route features (>= floor 3) → demoted.
    # `services/core.ts` is shared by 4 too, but is a DOMAIN file → kept (recall).
    # `lib/local.ts` is scaffold but in only 1 feature → kept (not shared).
    feats = []
    for i in range(4):
        feats.append(_route(f"route-{i}", [
            f"src/routes/r{i}.ts", "lib/util.ts", "services/core.ts",
        ]))
    feats.append(_route("solo", ["src/routes/solo.ts", "lib/local.ts"]))
    res = filter_shared_scaffold(feats)
    assert res.fan_in_threshold >= 3  # structural floor respected
    assert res.shared_scaffold_files == 1  # only lib/util.ts
    for i in range(4):
        paths = feats[i].paths
        assert "lib/util.ts" not in paths          # shared scaffold → demoted
        assert "services/core.ts" in paths          # shared DOMAIN → kept
        assert f"src/routes/r{i}.ts" in paths        # own route → kept
    assert "lib/local.ts" in feats[-1].paths         # low-fan-in scaffold → kept


def test_workspace_anchor_untouched() -> None:
    anchor = _feat("frontend", ["lib/util.ts", "ui/Button.tsx"], description=_WS)
    routes = [_route(f"r{i}", [f"p{i}.ts", "lib/util.ts", "ui/Button.tsx"])
              for i in range(4)]
    before = list(anchor.paths)
    filter_shared_scaffold([anchor, *routes])
    # the anchor keeps the shared scaffold (it's the file's honest home)
    assert anchor.paths == before
    # the specific features were trimmed
    assert all("lib/util.ts" not in r.paths for r in routes)


def test_noop_when_no_shared_scaffold() -> None:
    feats = [_route(f"r{i}", [f"src/routes/r{i}.ts"]) for i in range(4)]
    res = filter_shared_scaffold(feats)
    assert res.shared_scaffold_files == 0
    assert res.paths_removed == 0


def test_disabled_via_env(monkeypatch) -> None:
    monkeypatch.setenv("FAULTLINE_STAGE_8_6_5_SCAFFOLD_FILTER", "0")
    feats = [_route(f"r{i}", [f"p{i}.ts", "lib/util.ts"]) for i in range(4)]
    res = filter_shared_scaffold(feats)
    assert res.enabled is False
    assert all("lib/util.ts" in f.paths for f in feats)


def test_telemetry_shape() -> None:
    feats = [_route(f"r{i}", [f"p{i}.ts", "lib/util.ts"]) for i in range(4)]
    tele = filter_shared_scaffold(feats).as_telemetry()
    assert set(tele) == {
        "enabled", "fan_in_threshold", "shared_scaffold_files",
        "paths_removed", "features_trimmed", "sample",
    }
    assert tele["shared_scaffold_files"] == 1
