"""Phase 5 Layer A — verify the discovery_method bypass mechanism.

Features whose ``discovery_method`` is in
``_PROTECTED_DISCOVERY_METHODS`` (currently ``"critique"``) skip the
two safety filters that exist to suppress primary-scan phantoms:

  - ``faultline.analyzer.features._drop_noise_features``
  - ``faultline.analyzer.post_process.drop_noise_features``

Tests use neutral synthetic Feature instances per the no-repo-
specific-paths rule.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.analyzer.features import (
    _PROTECTED_DISCOVERY_METHODS,
    _drop_noise_features,
    _is_protected_discovery,
)
from faultline.analyzer.post_process import drop_noise_features as pp_drop
from faultline.models.types import Feature


def _f(name: str, paths, commits, *, discovery="primary") -> Feature:
    return Feature(
        name=name, paths=paths, authors=[],
        total_commits=commits, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=100.0,
        discovery_method=discovery,
    )


def test_is_protected_discovery_true_for_critique():
    feat = _f("X", ["a.py"], 0, discovery="critique")
    assert _is_protected_discovery(feat)


def test_is_protected_discovery_false_for_primary():
    feat = _f("X", ["a.py"], 0)  # default = "primary"
    assert not _is_protected_discovery(feat)


def test_is_protected_discovery_false_for_unknown_value():
    feat = _f("X", ["a.py"], 0, discovery="speculative")
    assert not _is_protected_discovery(feat)


def test_protected_set_contains_critique():
    assert "critique" in _PROTECTED_DISCOVERY_METHODS


# ── _drop_noise_features ────────────────────────────────────────────


def test_drop_noise_features_drops_tiny_cold_primary():
    """Sanity baseline: a tiny+cold primary feature gets re-folded
    into a synthetic shared-infra bucket — i.e. dropped from the
    visible feature list under its original name.
    """
    feats = [_f("Tiny", ["a.py", "b.py"], 5)]  # 2 paths < 4, 5c < 30
    out = _drop_noise_features(feats)
    assert "Tiny" not in {f.name for f in out}
    assert "shared-infra" in {f.name for f in out}


def test_drop_noise_features_keeps_tiny_cold_critique():
    """Phase 5 Layer A: same shape with discovery_method=critique
    must survive the filter.
    """
    feats = [_f("Tiny", ["a.py", "b.py"], 0, discovery="critique")]
    out = _drop_noise_features(feats)
    assert len(out) == 1
    assert out[0].name == "Tiny"


def test_drop_noise_features_mixed_keeps_critique_drops_primary():
    feats = [
        _f("PrimaryTiny", ["a.py", "b.py"], 5),
        _f("CritTiny", ["x.py", "y.py"], 0, discovery="critique"),
        _f("PrimaryHot", ["c.py"], 100),  # cold by paths but hot by commits
    ]
    out = _drop_noise_features(feats)
    out_names = {f.name for f in out}
    assert "PrimaryTiny" not in out_names
    assert "CritTiny" in out_names
    assert "PrimaryHot" in out_names


# ── post_process.drop_noise_features ─────────────────────────────────


def test_post_process_drop_noise_drops_phantom_primary():
    """Sanity baseline: ≤2 files AND ≤1 commit = phantom, dropped."""
    feats = [_f("Phantom", ["a.py"], 1)]
    cleaned, dropped = pp_drop(feats)
    assert cleaned == []
    assert len(dropped) == 1


def test_post_process_drop_noise_keeps_phantom_critique():
    """Phase 5 Layer A: critique features bypass the phantom check."""
    feats = [_f("Phantom", ["a.py"], 1, discovery="critique")]
    cleaned, dropped = pp_drop(feats)
    assert len(cleaned) == 1
    assert cleaned[0].name == "Phantom"
    assert dropped == []


def test_post_process_drop_noise_keeps_critique_named_uncategorized():
    """Even a name like 'misc' (which would normally be dropped as
    uncategorized catch-all) survives if discovery_method=critique.
    The aggregator never produces such names — but if it did, the
    rule should still apply.
    """
    feats = [_f("misc", ["a.py", "b.py", "c.py"], 50, discovery="critique")]
    cleaned, _ = pp_drop(feats)
    assert len(cleaned) == 1
