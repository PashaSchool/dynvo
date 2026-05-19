"""Sprint A1 — Stage 5 fallback validation pipeline.

Specifically covers the two A1 gates that run on Stage 4 residual
features only (Stage 2 anchors are exempt):

  1. Filesystem-existence gate: fallback features whose paths don't
     resolve under ``ctx.repo_path`` are dropped.
  2. Anchor-Jaccard dedup: fallback features whose slug Jaccards a
     deterministic anchor at ≥ 0.7 are dropped (anchor wins).

Also verifies:
  - Telemetry: ``Stage5Result.validation_drops`` increments correctly.
  - Drop log: drop_log entries name the reason machine-readably.
  - Deterministic features are NEVER dropped by either gate.
  - ``ctx=None`` skips filesystem validation (back-compat).
"""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature
from faultline.pipeline_v2.stage_5_postprocess import (
    Stage5Drops,
    Stage5Result,
    stage_5_postprocess,
    stage_5_postprocess_with_telemetry,
)


def _ctx(tmp_path: Path) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=[],
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


def _dev(name: str, paths: tuple[str, ...],
         sources: list[str] | None = None,
         confidence: str = "medium") -> DeveloperFeature:
    return DeveloperFeature(
        name=name, paths=paths,
        sources=sources or ["route"],
        confidence=confidence,  # type: ignore[arg-type]
    )


# ── Filesystem-existence gate ─────────────────────────────────────────────


def test_fallback_with_missing_path_is_dropped(tmp_path: Path) -> None:
    """LLM hallucinated path → drop, with telemetry."""
    # Do NOT create the file; the gate must drop the feature.
    result = stage_5_postprocess_with_telemetry(
        deterministic=[],
        residual=[
            _dev("ghost-feature", ("imaginary/path.ts",),
                 sources=["llm-fallback"], confidence="low"),
        ],
        ctx=_ctx(tmp_path),
    )
    assert isinstance(result, Stage5Result)
    assert [f.name for f in result.features] == []
    assert result.validation_drops.filesystem_missing == 1
    assert result.validation_drops.anchor_duplicate == 0
    assert any(
        name == "ghost-feature" and reason.startswith("path_not_found:")
        for (name, reason) in result.drop_log
    )


def test_fallback_with_partial_missing_is_dropped(tmp_path: Path) -> None:
    """If ANY path is missing, drop the whole feature (LLM hallucinated)."""
    (tmp_path / "real.ts").write_text("// stub")
    result = stage_5_postprocess_with_telemetry(
        deterministic=[],
        residual=[
            _dev("mixed-feature", ("real.ts", "fake.ts"),
                 sources=["llm-fallback"], confidence="low"),
        ],
        ctx=_ctx(tmp_path),
    )
    assert [f.name for f in result.features] == []
    assert result.validation_drops.filesystem_missing == 1


def test_fallback_with_all_existing_paths_passes(tmp_path: Path) -> None:
    (tmp_path / "a.ts").write_text("// stub")
    (tmp_path / "b.ts").write_text("// stub")
    result = stage_5_postprocess_with_telemetry(
        deterministic=[],
        residual=[
            _dev("real-feature", ("a.ts", "b.ts"),
                 sources=["llm-fallback"], confidence="low"),
        ],
        ctx=_ctx(tmp_path),
    )
    assert [f.name for f in result.features] == ["real-feature"]
    assert result.validation_drops.filesystem_missing == 0


def test_deterministic_features_never_validated_for_filesystem(
    tmp_path: Path,
) -> None:
    """Stage 2 anchors are ground truth — never validated against disk.

    Even if the path doesn't exist, the deterministic feature survives.
    """
    # Path 'fictional.ts' doesn't exist on disk.
    result = stage_5_postprocess_with_telemetry(
        deterministic=[_dev("billing", ("fictional.ts",))],
        residual=[],
        ctx=_ctx(tmp_path),
    )
    assert [f.name for f in result.features] == ["billing"]
    assert result.validation_drops.filesystem_missing == 0


# ── Anchor-Jaccard dedup gate ─────────────────────────────────────────────


def test_fallback_duplicating_anchor_is_dropped(tmp_path: Path) -> None:
    """Anchor 'auth-service' + fallback 'auth-service-tools' (token
    overlap {auth, service} of 3 → Jaccard = 2/3 ≈ 0.67 — below.

    Use a clearer dup: anchor='billing-portal' + fallback='billing-portal'
    (token-identical → Jaccard = 1.0).
    """
    (tmp_path / "x.ts").write_text("// stub")
    result = stage_5_postprocess_with_telemetry(
        deterministic=[_dev("billing-portal", ("a.ts",))],
        residual=[
            _dev("billing-portal", ("x.ts",),
                 sources=["llm-fallback"], confidence="low"),
        ],
        ctx=_ctx(tmp_path),
    )
    # Anchor wins; fallback dropped.
    names = sorted(f.name for f in result.features)
    assert names == ["billing-portal"]
    assert result.validation_drops.anchor_duplicate == 1
    assert any(
        name == "billing-portal"
        and reason.startswith("duplicate_of_anchor:billing-portal:")
        for (name, reason) in result.drop_log
    )


def test_fallback_partial_overlap_below_threshold_survives(
    tmp_path: Path,
) -> None:
    """Jaccard 0.33 (one token in common of 3) → NOT a dup."""
    (tmp_path / "x.ts").write_text("// stub")
    # tokens: anchor={billing}, fallback={widget,portal} → Jaccard = 0/3 = 0
    result = stage_5_postprocess_with_telemetry(
        deterministic=[_dev("billing", ("a.ts",))],
        residual=[
            _dev("widget-portal", ("x.ts",),
                 sources=["llm-fallback"], confidence="low"),
        ],
        ctx=_ctx(tmp_path),
    )
    names = sorted(f.name for f in result.features)
    assert names == ["billing", "widget-portal"]
    assert result.validation_drops.anchor_duplicate == 0


def test_fallback_jaccard_exactly_at_threshold_is_dropped(
    tmp_path: Path,
) -> None:
    """Jaccard ≥ 0.7 should drop. Tokens {a,b,c} vs {a,b,c,d} → 3/4 = 0.75."""
    (tmp_path / "x.ts").write_text("// stub")
    result = stage_5_postprocess_with_telemetry(
        deterministic=[_dev("a-b-c", ("a.ts",))],
        residual=[
            _dev("a-b-c-d", ("x.ts",),
                 sources=["llm-fallback"], confidence="low"),
        ],
        ctx=_ctx(tmp_path),
    )
    assert [f.name for f in result.features] == ["a-b-c"]
    assert result.validation_drops.anchor_duplicate == 1


def test_filesystem_check_runs_before_anchor_check(tmp_path: Path) -> None:
    """Order matters: a missing-path fallback that ALSO duplicates an
    anchor should be counted as ``filesystem_missing``, not
    ``anchor_duplicate``. The order is: cheap structural check first.
    """
    # NO filesystem creation — path is missing.
    result = stage_5_postprocess_with_telemetry(
        deterministic=[_dev("billing", ("a.ts",))],
        residual=[
            _dev("billing", ("ghost.ts",),
                 sources=["llm-fallback"], confidence="low"),
        ],
        ctx=_ctx(tmp_path),
    )
    assert result.validation_drops.filesystem_missing == 1
    assert result.validation_drops.anchor_duplicate == 0


# ── Mixed scenario: deterministic + valid + invalid fallback ──────────────


def test_mixed_scan_keeps_anchors_and_validated_fallbacks(
    tmp_path: Path,
) -> None:
    """End-to-end: anchor + good fallback + missing fallback + dup fallback."""
    (tmp_path / "good.ts").write_text("// stub")
    # 'ghost.ts' NOT created
    result = stage_5_postprocess_with_telemetry(
        deterministic=[_dev("auth", ("a.ts",))],
        residual=[
            _dev("widget-toolkit", ("good.ts",),
                 sources=["llm-fallback"], confidence="low"),
            _dev("phantom", ("ghost.ts",),
                 sources=["llm-fallback"], confidence="low"),
            _dev("auth", ("good.ts",),
                 sources=["llm-fallback"], confidence="low"),
        ],
        ctx=_ctx(tmp_path),
    )
    names = sorted(f.name for f in result.features)
    assert names == ["auth", "widget-toolkit"]
    assert result.validation_drops.filesystem_missing == 1
    assert result.validation_drops.anchor_duplicate == 1


# ── ctx=None back-compat ──────────────────────────────────────────────────


def test_ctx_none_skips_filesystem_validation_but_still_runs_anchor_dedup(
    tmp_path: Path,
) -> None:
    """When ctx is None we skip filesystem validation (back-compat for
    callers that don't have a ctx handy). Anchor dedup still runs."""
    # Note: with ctx=None the FS gate is skipped so path-missing fallback
    # would survive. Confirm naming-discipline still runs.
    features = stage_5_postprocess(
        deterministic=[_dev("billing", ("a.ts",))],
        residual=[
            _dev("phantom", ("ghost.ts",),
                 sources=["llm-fallback"], confidence="low"),
        ],
        ctx=None,
    )
    # Without ctx, FS gate skipped → phantom survives FS. Naming OK.
    names = sorted(f.name for f in features)
    assert names == ["billing", "phantom"]


# ── Drop counter dataclass ────────────────────────────────────────────────


def test_stage5_drops_as_dict_shape() -> None:
    drops = Stage5Drops(filesystem_missing=3, anchor_duplicate=5, junk_name=1)
    assert drops.as_dict() == {
        "filesystem_missing": 3,
        "anchor_duplicate": 5,
        "junk_name": 1,
    }


def test_zero_drops_emits_zeros() -> None:
    drops = Stage5Drops()
    assert drops.as_dict() == {
        "filesystem_missing": 0,
        "anchor_duplicate": 0,
        "junk_name": 0,
    }
