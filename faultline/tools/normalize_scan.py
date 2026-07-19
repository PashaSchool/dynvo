"""Normalize a FeatureMap scan JSON for byte-stable comparison.

The 2026-07-02 determinism work made an unchanged repo scan
content-identical EXCEPT for a small set of run-scoped fields:

* ``analyzed_at`` (top-level) — wall-clock stamp of Stage 7.
* ``scan_meta.run_id`` — ``<utc-ts>-<sha8>`` per-run isolation id.
* ``scan_meta`` timing / spend telemetry — ``elapsed_sec`` (top level
  and nested per-stage telemetry dicts), ``*_elapsed_sec``, ``cost_usd``
  and LLM ``calls`` counters.

``normalize_scan`` deep-copies the document and strips exactly those
fields; ``scan_digest`` hashes the canonical JSON serialisation of the
normalized document. Used by:

* the snapshot gate (``faultline.tools.snapshot_gate``) — the checked-in
  per-profile digests,
* the G4 inertness tests — "registering a non-matching profile changes
  nothing, byte-for-byte".

Also stripped: ``health_score`` / ``symbol_health_score`` — the ONLY
content fields whose value decays with wall-clock time by design
(commit-age weighting against ``datetime.now()``), so they cannot be
byte-stable for a frozen clone.

Deliberately NOT stripped: ``repo_path`` / ``remote_url`` (stable for a
pinned clone; a path change is a real fixture change), ``engine_version``
/ ``schema_version`` (a version bump SHOULD be a conscious
``--update``), and every other content field (features, flows, indexes).

No LLM, no network, no engine imports — safe for any harness context.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

#: Keys removed wherever they appear INSIDE ``scan_meta`` (pure
#: run-telemetry, never content). ``elapsed_sec`` appears both at
#: ``scan_meta`` top level and inside nested per-stage telemetry dicts;
#: ``sample_links`` / ``*_sample`` (per-stage debug samples) and
#: ``stage_6_3_cache_hits`` (memo counter) depend on parallel-worker
#: scheduling — measured drifting between back-to-back runs on dub
#: (sample_links, unmatched_sample order) while every content array
#: stayed identical across 6 runs (2026-07-03 calibration). Inside
#: ``scan_meta``, ``*_sample`` keys are illustrative debug telemetry by
#: convention, so they are scrubbed wholesale. ``stage_6_55_page_interior``
#: (AST-parse cache telemetry: cache_hits + parsed) is warmth-dependent —
#: a cold run parses what a warm run serves from cache, so BOTH counters
#: drift between cold/warm runs of identical code (B1 gate calibration,
#: 2026-07-08). ``router_files_parsed`` (nested per-linker at
#: ``stage_6_4/per_linker/<linker>/``) is a wall-clock-budget telemetry
#: counter — how many router files the linker managed to parse inside
#: its time budget — so it drifts under CPU load between otherwise
#: identical runs (B5 kill-switch gate calibration, 2026-07-08); it is
#: scrubbed at any depth like the per-linker ``sample_links`` /
#: ``*_sample`` debug fields. ``mutation_sites_found`` (nested per-linker
#: at ``stage_6_4/per_linker/store-mutation/``) is the same class: Stage
#: 6.4 runs (feature × linker) units on a ThreadPoolExecutor and the
#: store-mutation linker replays cached per-file outcomes into a shared
#: telemetry object, so the counter varies with worker scheduling under
#: CPU load while the deduped link set — and every content array — stays
#: byte-identical (cal.com 4-way calibration, 2026-07-19: 2118 vs 2310
#: in a simultaneous same-code pair with all content layers identical).
#: The scrub is deliberately
#: scoped to the ``scan_meta`` subtree so a same-named CONTENT field
#: elsewhere could never be masked.
_VOLATILE_IN_SCAN_META = frozenset(
    {
        "elapsed_sec",
        "elapsed",
        "cost_usd",
        "duration_s",
        "duration_sec",
        "duration_ms",
        "started_at",
        "finished_at",
        "sample_links",
        "stage_6_3_cache_hits",
        "stage_6_55_page_interior",
        "router_files_parsed",
        "mutation_sites_found",
        # S3 overturn arbiter (FAULTLINE_OVERTURN_ARBITER=1) — pure
        # run-forensics: the ledger census + post-freeze conflict census
        # "who wanted to throw". Present only when the flag is ON; stripped
        # so the ON==OFF byte-identity comparison sees content only (the
        # arbiter is byte-identical to OFF by construction — write-through).
        "overturns",
        "overturn_conflicts",
    }
)

_VOLATILE_SCAN_META_SUFFIXES = ("_elapsed_sec", "_sample")

#: Keys removed EVERYWHERE in the document: fields whose VALUE decays
#: with wall-clock time by design, so they can never be byte-stable for
#: a frozen clone. ``_calculate_health`` / ``_calculate_weighted_health``
#: age-weight each commit by ``datetime.now() - commit.date`` — measured
#: as a 95.5 → 95.4 drift on dub between two runs 30 minutes apart
#: (2026-07-03 calibration). Changes to the health FORMULA are trunk
#: changes reviewed via unit tests, not via the snapshot gate.
_VOLATILE_WALL_CLOCK_DECAYED = frozenset({"health_score", "symbol_health_score"})

#: Keys removed only at the document's top level.
_VOLATILE_TOP = frozenset({"analyzed_at"})

#: Keys removed only inside ``scan_meta``. ``stage_artifact_dir``
#: reflects the (per-run temp) ``FAULTLINES_RUN_DIR`` state dir, not
#: scan content.
_VOLATILE_SCAN_META = frozenset({"run_id", "calls", "stage_artifact_dir"})


def _scrub(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _scrub(value)
            for key, value in node.items()
            if key not in _VOLATILE_IN_SCAN_META
            and not (
                isinstance(key, str)
                and key.endswith(_VOLATILE_SCAN_META_SUFFIXES)
            )
        }
    if isinstance(node, list):
        return [_scrub(item) for item in node]
    return node


def _drop_decayed(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            key: _drop_decayed(value)
            for key, value in node.items()
            if key not in _VOLATILE_WALL_CLOCK_DECAYED
        }
    if isinstance(node, list):
        return [_drop_decayed(item) for item in node]
    return node


def normalize_scan(doc: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``doc`` with run-scoped fields removed."""
    out = copy.deepcopy(doc)
    for key in _VOLATILE_TOP:
        out.pop(key, None)
    scan_meta = out.get("scan_meta")
    if isinstance(scan_meta, dict):
        for key in _VOLATILE_SCAN_META:
            scan_meta.pop(key, None)
        out["scan_meta"] = _scrub(scan_meta)
    return _drop_decayed(out)


def canonical_json(doc: dict[str, Any]) -> str:
    """Canonical serialisation: sorted keys, tight separators, ensure_ascii."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def scan_digest(doc: dict[str, Any]) -> str:
    """``sha256:<hex>`` digest of the normalized document."""
    payload = canonical_json(normalize_scan(doc)).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = ["canonical_json", "normalize_scan", "scan_digest"]
