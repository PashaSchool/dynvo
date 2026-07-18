"""S2 Seg C — canonical LLM batch composition (``FAULTLINE_LLM_BATCH_CANON``).

The resample class (probe 2026-07-18): 1 drifting flow of 980 flipped the
WHOLE-batch 6.7d cache key (sha256 over model+prompt) → full resample →
−26% UF. Part of the key surface is VOLATILE — pure counts that change with
no semantic content change: digest ``n_dev_features`` (a feature past the
digest cap changes ONLY this number), Call-2 per-row ``n_files``, and the
raw ``member_count`` rank in the digest's UF ordering. Under the canon those
leave the prompt canon and the ordering uses log2 buckets; a REAL content
change still flips the key (content-keyed law). Default OFF → digest,
prompts and keys byte-identical.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS
from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
    BATCH_CANON_ENV,
    MAX_DEV_FEATURES_DIGEST,
    _build_digest,
    _cache_key,
    _reattrib_dev_items,
    _weight_bucket,
    batch_canon_enabled,
)


def _feat(name: str, paths: list[str], commits: int = 3) -> Feature:
    return Feature(
        name=name, display_name=name, description=f"{name} module",
        paths=paths, authors=["a"], total_commits=commits, bug_fixes=1,
        bug_fix_ratio=0.33, last_modified=datetime.now(timezone.utc),
        health_score=90.0, layer="developer",
    )


def _uf(uf_id: str, members: int, *, resource: str = "thing") -> UserFlow:
    return UserFlow(
        id=uf_id, name=f"Manage {resource}", intent="manage",
        resource=resource, domain="dom",
        member_flow_ids=[f"{uf_id}-m{i}" for i in range(members)],
        member_count=members, routes=[],
    )


# ── kill-switch: OFF is byte-identical ──────────────────────────────────────


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BATCH_CANON_ENV, raising=False)
    assert batch_canon_enabled() is False


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_flag_kill_switch_values(
    monkeypatch: pytest.MonkeyPatch, val: str,
) -> None:
    monkeypatch.setenv(BATCH_CANON_ENV, val)
    assert batch_canon_enabled() is False


def test_off_digest_carries_legacy_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(BATCH_CANON_ENV, raising=False)
    d = _build_digest([_feat("alpha", ["a/x.ts"])], [], [_uf("UF-001", 3)], [])
    assert d["n_dev_features"] == 1
    rows, header = _reattrib_dev_items([_feat("alpha", ["a/x.ts", "a/y.ts"])])
    assert rows[0]["n_files"] == 2
    assert "file count" in header


def test_flag_registered_in_env_output_flags() -> None:
    assert "FAULTLINE_LLM_BATCH_CANON" in ENV_OUTPUT_FLAGS


# ── volatile-count exclusion: the key survives count-only drift ─────────────


def test_dev_count_drift_beyond_cap_does_not_flip_key_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dev feature past the digest cap changes ONLY n_dev_features. OFF:
    whole-batch key flips (the resample trigger shape). ON: key unchanged."""
    devs = [_feat(f"mod-{i:03d}", [f"m{i}/x.ts"], commits=1000 - i)
            for i in range(MAX_DEV_FEATURES_DIGEST)]
    extra = _feat("zzz-tail", ["zz/x.ts"], commits=1)  # sorts past the cap

    monkeypatch.setenv(BATCH_CANON_ENV, "1")
    d_on_a = _build_digest(devs, [], [], [])
    d_on_b = _build_digest(devs + [extra], [], [], [])
    assert "n_dev_features" not in d_on_a
    assert _cache_key(d_on_a, "m1", "m2") == _cache_key(d_on_b, "m1", "m2")

    monkeypatch.delenv(BATCH_CANON_ENV, raising=False)
    d_off_a = _build_digest(devs, [], [], [])
    d_off_b = _build_digest(devs + [extra], [], [], [])
    assert d_off_a["n_dev_features"] == MAX_DEV_FEATURES_DIGEST
    assert _cache_key(d_off_a, "m1", "m2") != _cache_key(d_off_b, "m1", "m2")


def test_member_count_drift_within_bucket_keeps_digest_stable_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ±1 member drift that does not cross a log2 boundary neither reorders
    the digest UFs nor changes any digest content → same key ON. OFF: the raw
    rank flips the order → key flips (the 1-of-980 exhibit shape)."""
    a, b = _uf("UF-00A", 11, resource="alpha"), _uf("UF-00B", 10, resource="beta")
    b_drift = _uf("UF-00B", 12, resource="beta")   # 10→12: same bucket 4 (8-15)

    monkeypatch.setenv(BATCH_CANON_ENV, "1")
    d1 = _build_digest([], [], [a, b], [])
    d2 = _build_digest([], [], [a, b_drift], [])
    assert _cache_key(d1, "m1", "m2") == _cache_key(d2, "m1", "m2")

    monkeypatch.delenv(BATCH_CANON_ENV, raising=False)
    d3 = _build_digest([], [], [a, b], [])
    d4 = _build_digest([], [], [a, b_drift], [])
    assert _cache_key(d3, "m1", "m2") != _cache_key(d4, "m1", "m2")


def test_real_content_change_still_flips_key_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canon never caches through a REAL change: a renamed resource
    (content) flips the key even under the canon (content-keyed law)."""
    monkeypatch.setenv(BATCH_CANON_ENV, "1")
    d1 = _build_digest([], [], [_uf("UF-001", 5, resource="alpha")], [])
    d2 = _build_digest([], [], [_uf("UF-001", 5, resource="renamed")], [])
    assert _cache_key(d1, "m1", "m2") != _cache_key(d2, "m1", "m2")


def test_reattrib_rows_omit_n_files_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BATCH_CANON_ENV, "1")
    rows, header = _reattrib_dev_items([
        _feat("beta", ["b/x.ts", "b/y.ts", "b/z.ts"]),
        _feat("alpha", ["a/x.ts"]),
    ])
    assert all("n_files" not in r for r in rows)
    assert [r["name"] for r in rows] == ["alpha", "beta"]  # stable name sort
    assert "file count" not in header


def test_one_flow_drift_leaves_other_refiner_batches_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-domain batch isolation (refiner): a drift in domain A's payload
    leaves domain B's content key untouched — locked as a regression gate for
    the canon world (the whole-batch victim was 6.7d; the refiner's
    composition must STAY isolated)."""
    from faultline.pipeline_v2.stage_6_7b_uf_refiner import _refine_cache_key

    monkeypatch.setenv(BATCH_CANON_ENV, "1")
    prompt_b = "DOMAIN: beta\nUSER_FLOWS: [stable payload]"
    key_b_before = _refine_cache_key("m", prompt_b)
    # domain A drifts (its own prompt changes) — B's inputs are untouched:
    key_b_after = _refine_cache_key("m", prompt_b)
    assert key_b_before == key_b_after
    assert _refine_cache_key("m", "DOMAIN: alpha\nv1") != _refine_cache_key(
        "m", "DOMAIN: alpha\nv2",
    )


# ── bucket arithmetic ───────────────────────────────────────────────────────


def test_weight_bucket_is_log2_and_scale_invariant() -> None:
    assert _weight_bucket(0) == 0
    assert _weight_bucket(1) == 1
    assert _weight_bucket(2) == _weight_bucket(3) == 2
    assert _weight_bucket(8) == _weight_bucket(15) == 4
    assert _weight_bucket(16) == 5
