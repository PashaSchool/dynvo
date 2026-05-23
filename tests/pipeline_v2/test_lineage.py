"""Unit tests for faultline.pipeline_v2.lineage."""
from __future__ import annotations

import pytest

from faultline.pipeline_v2.lineage import (
    RELATED_THRESHOLD,
    RENAME_THRESHOLD,
    assign_feature_lineage,
    assign_flow_lineage,
)


def _feat(name: str, paths: list[str], uuid: str = "") -> dict:
    return {"name": name, "paths": paths, "uuid": uuid}


def test_cold_scan_assigns_fresh_uuids():
    new = [_feat("billing", ["a", "b"]), _feat("auth", ["c", "d"])]
    records, stats = assign_feature_lineage(new, None)
    assert len(records) == 2
    assert stats.fresh == 2
    assert stats.base_count == 0
    assert all(len(r.uuid) == 32 for r in records)
    assert len({r.uuid for r in records}) == 2


def test_carry_forward_identical_paths_and_name():
    base = [_feat("billing", ["a", "b", "c"], uuid="b1" * 16)]
    new = [_feat("billing", ["a", "b", "c"])]
    records, stats = assign_feature_lineage(new, base)
    assert records[0].uuid == "b1" * 16
    assert records[0].previous_names == []
    assert stats.carried_forward == 1
    assert stats.renamed == 0
    assert stats.fresh == 0


def test_rename_preserves_uuid_and_records_previous_name():
    base = [_feat("subscriptions", ["x", "y", "z"], uuid="u" * 32)]
    new = [_feat("billing", ["x", "y", "z"])]  # identical files, renamed
    records, stats = assign_feature_lineage(new, base)
    assert records[0].uuid == "u" * 32
    assert records[0].previous_names == ["subscriptions"]
    assert stats.renamed == 1


def test_pure_new_mints_new_uuid_when_no_overlap():
    base = [_feat("billing", ["a", "b"], uuid="b" * 32)]
    new = [_feat("notifications", ["m", "n", "o"])]
    records, stats = assign_feature_lineage(new, base)
    assert records[0].uuid != "b" * 32
    assert stats.fresh == 1
    assert records[0].split_from is None


def test_split_marks_secondary_features_with_split_from():
    # Base feature with files a..f. Two new features each take half;
    # the higher-overlap one keeps the UUID.
    base = [_feat("user", ["a", "b", "c", "d"], uuid="U" * 32)]
    new = [
        _feat("user-profile", ["a", "b", "c"]),  # 3/4 overlap = 0.75 (above rename)
        _feat("user-billing", ["a", "b"]),       # 2/4 overlap = 0.5 (above related)
    ]
    records, stats = assign_feature_lineage(new, base)
    # First wins UUID
    assert records[0].uuid == "U" * 32
    # Second is split-from
    assert records[1].uuid != "U" * 32
    assert records[1].split_from == "U" * 32
    assert stats.split == 1


def test_merge_records_merged_from_when_one_new_absorbs_two_bases():
    # New feature paths cover BOTH base features above related threshold.
    base = [
        _feat("billing-stripe", ["s1", "s2"], uuid="S" * 32),
        _feat("billing-paypal", ["p1", "p2"], uuid="P" * 32),
    ]
    # Make first base higher-overlap so it wins.
    new = [_feat("billing", ["s1", "s2", "p1", "p2"])]
    records, stats = assign_feature_lineage(new, base)
    # Winner: whichever base has higher jaccard. Both have jaccard 2/4=0.5
    # which is BELOW rename_threshold (0.70). So no rename win → fresh
    # UUID + split_from. Make a stronger version: extend one base.
    base = [
        _feat("billing-stripe", ["s1", "s2", "s3", "s4"], uuid="S" * 32),
        _feat("billing-paypal", ["p1", "p2"], uuid="P" * 32),
    ]
    new = [_feat("billing", ["s1", "s2", "s3", "s4", "p1", "p2"])]
    # jaccard with stripe = 4/6 = 0.667 (below rename) — adjust threshold.
    records, stats = assign_feature_lineage(
        new, base, rename_threshold=0.6, related_threshold=0.3,
    )
    # stripe jaccard 0.667 wins (>0.6), paypal jaccard 0.333 (>0.3 related)
    assert records[0].uuid == "S" * 32
    assert "P" * 32 in records[0].merged_from
    assert stats.merged == 1


def test_uuid_uniqueness_invariant_holds():
    base = [_feat("a", ["1"], uuid="A" * 32), _feat("b", ["2"], uuid="B" * 32)]
    new = [_feat("a", ["1"]), _feat("b", ["2"]), _feat("c", ["3"])]
    records, _ = assign_feature_lineage(new, base)
    assert len({r.uuid for r in records}) == 3


def test_base_features_without_uuid_get_minted_uuid():
    # Legacy base scan with no UUIDs — algorithm still produces stable
    # matches via name + path (UUID gets minted internally; new feature
    # picks it up).
    base = [_feat("billing", ["a", "b"], uuid="")]
    new = [_feat("billing", ["a", "b"])]
    records, _ = assign_feature_lineage(new, base)
    # UUID was minted for base + reused for new
    assert records[0].uuid != ""
    assert len(records[0].uuid) == 32


def test_default_thresholds_match_spec():
    assert RENAME_THRESHOLD == 0.70
    assert RELATED_THRESHOLD == 0.40


def test_flow_lineage_uses_same_algorithm():
    base = [{"name": "checkout-flow", "paths": ["a", "b"], "uuid": "F" * 32}]
    new = [{"name": "checkout-flow", "paths": ["a", "b"]}]
    records, _ = assign_flow_lineage(new, base)
    assert records[0].uuid == "F" * 32


def test_empty_new_features_returns_empty():
    records, stats = assign_feature_lineage([], None)
    assert records == []
    assert stats.new_count == 0


def test_empty_paths_doesnt_crash():
    # A feature with no paths can't match anything; should become fresh.
    base = [_feat("a", ["x"], uuid="X" * 32)]
    new = [_feat("a", [])]
    records, stats = assign_feature_lineage(new, base)
    assert records[0].uuid != "X" * 32
    assert stats.fresh == 1
