"""B74 Seg B — Stage 6.865 post-grain flow re-derivation (form F3′).

Named gates (spec §SEG B ПРОБ-КАНОН + §SEG B РЕ-ВХІД F3′):

  - cohort selector, twenty shape: ``object-record-2`` / ``activities``
    / ``workflow-2`` (stage-8 births, 0 flows) IN; healthy-density
    causal feature OUT; locale-births OUT via the EXISTING MIN_EXPORTS
    gate; re-membered rows at the MAX_FLOWS_PER_FEATURE cap OUT unless
    chunkable.
  - chunk-eligibility override: ratio trigger (exports/paths prompt
    caps) fires INDEPENDENT of the global oversized cut.
  - F3′ position: the re-derive runs AFTER the 6.86 mint window and
    BEFORE the Stage 6.7 UF rollup (source order + replay-registry
    order + functional mint-visibility through the rollup).
  - anti-cases: a board with NO post-stage-3 grain change (openstatus /
    kan shape) yields 0 qualifiers and a byte-identical armed board;
    papermark ``datarooms-2`` (birth, 6,210 loc / 0 flows on a healthy
    repo) QUALIFIES — the health-scoped law.
  - keyless law: no client → deterministic cohort selection +
    enumeration fully reported, zero flows derived.
"""

from __future__ import annotations

import copy
import threading
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from faultline.models.types import Feature, Flow
from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.flow_rederive import (
    FLOW_REDERIVE_ENV,
    flow_rederive_enabled,
    run_flow_rederive,
    select_rederive_cohort,
)
from faultline.pipeline_v2.stage_3_flows import MAX_FLOWS_PER_FEATURE

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── builders ────────────────────────────────────────────────────────────


def _ctx(tmp_path: Path, files: list[str] | None = None) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=files or [],
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


def _dev(name: str, paths: list[str], *, flows: list[Flow] | None = None,
         layer: str = "developer") -> Feature:
    f = Feature(
        name=name, paths=list(paths), flows=list(flows or []),
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )
    f.layer = layer
    return f


def _flow(name: str, entry: str, line: int = 1) -> Flow:
    return Flow(
        name=name, entry_point_file=entry, entry_point_line=line,
        paths=[entry], uuid=f"u-{name}",
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def _ts(tmp_path: Path, rel: str, exports: list[str], pad_lines: int = 0) -> str:
    """Write one TS file with the given exported symbols; returns rel."""
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        f"export function {sym}() {{ return {i}; }}"
        for i, sym in enumerate(exports)
    )
    if pad_lines:
        body += "\n" + "\n".join(
            f"const pad{i} = {i};" for i in range(pad_lines)
        )
    full.write_text(body + "\n", encoding="utf-8")
    return rel


def _locale(tmp_path: Path, rel: str) -> str:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text('{"key": "value"}\n', encoding="utf-8")
    return rel


class _FakeAnthropic:
    """Stage-3 fake client shape (SimpleNamespace content/usage)."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self._idx = 0
        self.call_count = 0
        self._lock = threading.Lock()
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, parent: "_FakeAnthropic") -> None:
            self._p = parent

        def create(self, **kwargs: Any) -> Any:
            with self._p._lock:
                self._p.call_count += 1
                if self._p._idx < len(self._p.responses):
                    text = self._p.responses[self._p._idx]
                    self._p._idx += 1
                else:
                    text = (
                        self._p.responses[-1]
                        if self._p.responses else '{"flows": []}'
                    )
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text=text)],
                usage=types.SimpleNamespace(input_tokens=10, output_tokens=5),
            )


class _FakeBackend:
    """Minimal CacheBackend duck-type (kind → key → value)."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}

    def get(self, kind: str, key: str) -> Any:
        return self.store.get(kind, {}).get(key)

    def set(self, kind: str, key: str, value: Any) -> None:
        self.store.setdefault(kind, {})[key] = value


# ── twenty-shape fixture ────────────────────────────────────────────────


def _twenty_board(tmp_path: Path) -> tuple[list[Feature], dict[str, list[str]], ScanContext]:
    """A miniature twenty: stage-3 units + a post-grain board.

    Snapshot (stage-3 time): ``twenty-front`` blob + two healthy small
    units. Board (finalize time): the blob decomposed into births
    ``object-record-2`` / ``activities`` / ``workflow-2`` + a locale
    birth + a healthy-density RE-MEMBERED unit + the untouched healthy
    units.
    """
    # Stage-8 birth targets (never seen by stage 3 under these names).
    orec = [
        _ts(tmp_path, f"front/record/{d}/C{i}.tsx",
            [f"RecordTable{d.capitalize()}{i}A", f"RecordTable{d.capitalize()}{i}B"])
        for d in ("table", "board", "cells")
        for i in range(8)
    ]  # 24 paths > MAX_PATHS_IN_PROMPT → ratio chunk trigger
    acts = [
        _ts(tmp_path, f"front/activities/A{i}.tsx",
            [f"TaskGroups{i}", f"TaskList{i}", f"NoteList{i}"])
        for i in range(4)
    ]  # 12 exports ≥ MIN_EXPORTS, no ratio trigger → single call
    wflw = [
        _ts(tmp_path, f"front/workflow/W{i}.tsx",
            [f"WorkflowEditAction{i}", f"WorkflowRun{i}"])
        for i in range(3)
    ]
    locale = [_locale(tmp_path, f"front/locales/{loc}.json")
              for loc in ("en", "de", "uk")]

    # Healthy flowful units (unchanged since stage 3 → NOT causal);
    # they define the repo's flowful median density.
    healthy_a = [_ts(tmp_path, "front/auth/SignIn.tsx",
                     ["SignInForm", "SignInEffect", "SignInHook"],
                     pad_lines=60)]
    healthy_b = [_ts(tmp_path, "front/settings/Settings.tsx",
                     ["SettingsPage", "SettingsForm", "SettingsHook"],
                     pad_lines=60)]

    # Healthy-density RE-MEMBERED unit: gained a path since stage 3 but
    # its flow density is at/above the repo median — secondary filter OUT.
    healthy_moved = [
        _ts(tmp_path, "front/billing/Billing.tsx",
            ["BillingPage", "BillingForm", "BillingHook"], pad_lines=20),
        _ts(tmp_path, "front/billing/Extra.tsx",
            ["BillingExtraA", "BillingExtraB", "BillingExtraC"], pad_lines=20),
    ]

    snapshot = {
        # the pre-decomposition blob unit (its path-set is gone from the board)
        "twenty-front": sorted(orec + acts + wflw + locale),
        "auth": sorted(healthy_a),
        "settings": sorted(healthy_b),
        "billing": [healthy_moved[0]],
    }

    features = [
        _dev("object-record-2", orec),
        _dev("activities", acts),
        _dev("workflow-2", wflw),
        _dev("locale-pack-2", locale),
        _dev("auth", healthy_a, flows=[
            _flow("sign-in-flow", healthy_a[0], 1),
            _flow("sign-out-flow", healthy_a[0], 2),
        ]),
        _dev("settings", healthy_b, flows=[
            _flow("manage-settings-flow", healthy_b[0], 1),
            _flow("update-settings-flow", healthy_b[0], 2),
        ]),
        _dev("billing", healthy_moved, flows=[
            _flow("manage-billing-flow", healthy_moved[0], 1),
            _flow("pay-invoice-flow", healthy_moved[0], 2),
            _flow("refund-flow", healthy_moved[0], 3),
        ]),
        # product-layer twin row wearing a cohort name — must be ignored
        _dev("object-record-2", orec, layer="product"),
    ]
    return features, snapshot, _ctx(tmp_path)


# ── flag default ────────────────────────────────────────────────────────


def test_flag_default_off(monkeypatch: Any) -> None:
    monkeypatch.delenv(FLOW_REDERIVE_ENV, raising=False)
    assert flow_rederive_enabled() is False
    monkeypatch.setenv(FLOW_REDERIVE_ENV, "0")
    assert flow_rederive_enabled() is False
    monkeypatch.setenv(FLOW_REDERIVE_ENV, "1")
    assert flow_rederive_enabled() is True


# ── cohort selector — twenty shape ──────────────────────────────────────


def test_cohort_selector_twenty_shape(tmp_path: Path) -> None:
    features, snapshot, ctx = _twenty_board(tmp_path)
    sel = select_rederive_cohort(features, snapshot, ctx)

    names = [c.feature.name for c in sel.candidates]
    # stage-8 births with starved flows are IN — the spec's survivors.
    assert "object-record-2" in names
    assert "activities" in names
    assert "workflow-2" in names
    # healthy-density causal (re-membered) unit is OUT — secondary filter.
    assert "billing" not in names
    assert sel.excluded_healthy_density == 1
    # locale-birth dies on the EXISTING MIN_EXPORTS flow gate.
    assert "locale-pack-2" not in names
    assert sel.excluded_flow_gate == 1
    assert "locale-pack-2" in sel.excluded_flow_gate_names
    # unchanged healthy units are not even causal.
    assert "auth" not in names and "settings" not in names
    # product-layer twin never enters (exactly one object-record-2 row).
    assert names.count("object-record-2") == 1
    # kinds: all three targets are births.
    kinds = {c.feature.name: c.kind for c in sel.candidates}
    assert kinds["object-record-2"] == "birth"
    assert sel.births >= 3
    assert sel.re_membered >= 1  # billing was causal before its exclusion


def test_chunk_elig_override_independent_of_global_cut(tmp_path: Path) -> None:
    """Ratio trigger (paths > MAX_PATHS_IN_PROMPT) chunks a candidate
    even though nothing here consults the global oversized cut; a small
    candidate under both prompt caps stays single-call."""
    features, snapshot, ctx = _twenty_board(tmp_path)
    sel = select_rederive_cohort(features, snapshot, ctx)
    by_name = {c.feature.name: c for c in sel.candidates}

    # 24 paths > 20 → chunk plan via the existing _plan_chunks fan-out,
    # capped at the prompt-window NEED (ceil(24/20) = 2 — exactly as
    # many chunks as visibility requires, not one more).
    orec = by_name["object-record-2"]
    assert orec.chunk_plan is not None
    assert orec.call_units == 2
    # 4 paths / 12 exports — under both caps → single call.
    acts = by_name["activities"]
    assert acts.chunk_plan is None
    assert acts.call_units == 1
    # telemetry-facing aggregate
    assert sum(c.call_units for c in sel.candidates) >= 4


def test_re_membered_at_cap_excluded_or_chunked(tmp_path: Path) -> None:
    """A re-membered row already AT the per-call flow cap is OUT unless
    the ratio trigger routes it through chunking. Both rows are padded
    to LOW flow-density so the cap rule (not the secondary density
    filter) is what fires."""
    # Healthy unchanged units define a HIGH median density ruler.
    ruler_a = [_ts(tmp_path, "pkg/docs/D.tsx",
                   ["DocA", "DocB", "DocC"], pad_lines=20)]
    ruler_b = [_ts(tmp_path, "pkg/help/H.tsx",
                   ["HelpA", "HelpB", "HelpC"], pad_lines=20)]
    # Unchunkable: few paths, few exports, at cap, LOW density (padded).
    small = [_ts(tmp_path, "pkg/side/S.tsx",
                 ["SideA", "SideB", "SideC"], pad_lines=6000)]
    capped_small = _dev("side-panel", small + [
        _ts(tmp_path, "pkg/side/S2.tsx", ["SideD"]),
    ], flows=[
        _flow(f"side-flow-{i}", small[0], i + 1)
        for i in range(MAX_FLOWS_PER_FEATURE)
    ])
    # Chunkable: >20 paths across a directory fan-out, at cap, LOW density.
    big_paths = [
        _ts(tmp_path, f"pkg/big/{d}/B{i}.tsx",
            [f"Big{d.capitalize()}{i}"], pad_lines=300)
        for d in ("one", "two", "three")
        for i in range(8)
    ]
    capped_big = _dev("page-layout-2", big_paths, flows=[
        _flow(f"layout-flow-{i}", big_paths[0], i + 1)
        for i in range(MAX_FLOWS_PER_FEATURE)
    ])
    snapshot = {
        "docs": sorted(ruler_a),
        "help": sorted(ruler_b),
        "side-panel": [small[0]],          # re-membered: gained S2.tsx
        "page-layout-2": [big_paths[0]],   # re-membered: gained 23 files
    }
    board = [
        _dev("docs", ruler_a, flows=[
            _flow("view-docs-flow", ruler_a[0], 1),
            _flow("search-docs-flow", ruler_a[0], 2),
        ]),
        _dev("help", ruler_b, flows=[
            _flow("view-help-flow", ruler_b[0], 1),
            _flow("contact-support-flow", ruler_b[0], 2),
        ]),
        capped_small,
        capped_big,
    ]
    sel = select_rederive_cohort(board, snapshot, _ctx(tmp_path))
    names = [c.feature.name for c in sel.candidates]
    assert "side-panel" not in names          # OUT — at cap, unchunkable
    assert sel.excluded_at_cap == 1
    assert "page-layout-2" in names           # chunked — cap may be the cause
    assert {c.feature.name: c for c in sel.candidates}[
        "page-layout-2"].chunk_plan is not None


def test_papermark_datarooms2_qualifies_health_scoped(tmp_path: Path) -> None:
    """The anti-case is HEALTH-scoped, not repo-scoped: on an otherwise
    healthy repo (flowful median exists) a flow-starved stage-8 birth
    (papermark ``datarooms-2``: 6,210 loc / 0 flows) still fires."""
    healthy = [_ts(tmp_path, "app/docs/Docs.tsx",
                   ["DocsPage", "DocsList", "DocsHook"], pad_lines=40)]
    dr_paths = [
        _ts(tmp_path, f"app/datarooms/D{i}.tsx",
            [f"Dataroom{i}A", f"Dataroom{i}B"], pad_lines=200)
        for i in range(4)
    ]
    features = [
        _dev("docs", healthy, flows=[
            _flow("view-docs-flow", healthy[0], 1),
            _flow("share-doc-flow", healthy[0], 2),
        ]),
        _dev("datarooms-2", dr_paths),  # birth: 0 flows, big loc
    ]
    snapshot = {"docs": sorted(healthy),
                "datarooms": sorted(dr_paths + ["app/datarooms/index.tsx"])}
    sel = select_rederive_cohort(features, snapshot, _ctx(tmp_path))
    assert [c.feature.name for c in sel.candidates] == ["datarooms-2"]
    assert sel.flowful_median_density > 0


# ── anti-case: no grain change ⇒ inert armed board ─────────────────────


def test_anticase_no_grain_change_zero_qualifiers_byte_ident(
    tmp_path: Path,
) -> None:
    """openstatus/kan shape: every non-test path-set matches a stage-3
    unit (incl. test-strip and removal-only deltas) → no causal
    candidates, ``None`` returned, stores untouched."""
    a = [_ts(tmp_path, "src/monitor/M.tsx", ["MonA", "MonB", "MonC"])]
    b = [_ts(tmp_path, "src/status/S.tsx", ["StA", "StB", "StC"])]
    features = [
        _dev("monitors", a, flows=[_flow("run-monitor-flow", a[0], 1)]),
        # test-strip delta only: stage-3 unit carried a test file.
        _dev("status-pages", b, flows=[_flow("view-status-flow", b[0], 1)]),
        # removal-only delta (generated strip / donor shed).
        _dev("alerts", ["src/alerts/A.tsx"]),
    ]
    snapshot = {
        "monitors": sorted(a),
        "status-pages": sorted(b + ["src/status/S.test.tsx"]),
        "alerts": ["src/alerts/A.tsx", "src/alerts/gen.ts"],
    }
    flows_store: list[Flow] = [
        fl for f in features for fl in f.flows
    ]
    edges_store: list[Any] = []
    before = [f.model_dump() for f in features]
    before_flows = [fl.model_dump() for fl in flows_store]

    tele = run_flow_rederive(
        features, flows_store, edges_store, _ctx(tmp_path),
        stage3_unit_snapshot=snapshot,
        model="claude-haiku-test",
        client=_FakeAnthropic(['{"flows": []}']),
    )
    assert tele is None  # inertness law — no scan_meta key on no-fire
    assert [f.model_dump() for f in features] == before
    assert [fl.model_dump() for fl in flows_store] == before_flows
    assert edges_store == []


def test_no_snapshot_is_inert(tmp_path: Path) -> None:
    features, _snapshot, ctx = _twenty_board(tmp_path)
    tele = run_flow_rederive(
        features, [], [], ctx,
        stage3_unit_snapshot=None,
        model="claude-haiku-test",
        client=_FakeAnthropic(['{"flows": []}']),
    )
    assert tele is None


# ── keyless law ─────────────────────────────────────────────────────────


def test_keyless_no_client_reports_cohort_derives_nothing(
    tmp_path: Path,
) -> None:
    features, snapshot, ctx = _twenty_board(tmp_path)
    flows_store: list[Flow] = []
    edges_store: list[Any] = []
    tele = run_flow_rederive(
        features, flows_store, edges_store, ctx,
        stage3_unit_snapshot=snapshot,
        model="claude-haiku-test",
        _client_factory=lambda: None,
    )
    assert tele is not None
    assert tele["no_client"] is True
    assert tele["flows_added"] == 0 and tele["llm_calls"] == 0
    # deterministic part fully reported — the armed-keyless census surface
    assert "object-record-2" in tele["cohort"]
    assert tele["cohort_selected"] == len(tele["cohort"])
    assert tele["call_units"] >= tele["cohort_selected"]
    assert tele["births"] >= 3
    assert flows_store == [] and edges_store == []


# ── writer identity + F3′ mint visibility ───────────────────────────────


def _single_call_board(tmp_path: Path) -> tuple[list[Feature], dict[str, list[str]], ScanContext]:
    acts = [
        _ts(tmp_path, f"front/activities/A{i}.tsx",
            [f"TaskGroups{i}", f"TaskList{i}", f"NoteList{i}"])
        for i in range(3)
    ]
    other = [_ts(tmp_path, "front/other/O.tsx", ["OtherA"])]
    features = [
        _dev("activities", acts),
        _dev("shared-owner", [acts[0]] + other),  # path-overlap secondary
    ]
    snapshot = {"twenty-front": sorted(acts + other),
                "shared-owner": sorted([acts[0]] + other)}
    return features, snapshot, _ctx(tmp_path)


def _canned_response() -> str:
    return (
        '{"flows": [{"name": "manage-task-groups-flow", '
        '"description": "Group tasks", "symbols": ["TaskGroups0"]}]}'
    )


def test_writer_identity_flows_and_bipartite_mirror(tmp_path: Path) -> None:
    features, snapshot, ctx = _single_call_board(tmp_path)
    acts_feature = features[0]
    flows_store: list[Flow] = []
    edges_store: list[Any] = []
    client = _FakeAnthropic([_canned_response()])

    tele = run_flow_rederive(
        features, flows_store, edges_store, ctx,
        stage3_unit_snapshot=snapshot,
        model="claude-haiku-test",
        client=client,
    )
    assert tele is not None and tele["applied"] is True
    assert tele["flows_added"] == 1
    assert tele["llm_calls"] == client.call_count

    # feature.flows[] containment view
    assert [fl.name for fl in acts_feature.flows] == [
        "manage-task-groups-flow",
    ]
    new = acts_feature.flows[0]
    # bipartite mirror shares the SAME object (Stage-5.5 identity)
    assert any(fl is new for fl in flows_store)
    # Stage-5.5 id form + content-derived uuid (32-hex, deterministic)
    assert new.id == "activities::manage-task-groups-flow"
    assert new.primary_feature == "activities"
    assert len(new.uuid) == 32 and int(new.uuid, 16) >= 0
    # entry grounded on the cited export symbol
    assert new.entry_point_file == "front/activities/A0.tsx"
    assert new.entry_point_line is not None
    # path-overlap secondary (shared-owner also owns A0.tsx)
    assert new.secondary_features == ["shared-owner"]
    assert new.cross_cutting is True
    # typed edges: one primary + one secondary
    kinds = sorted((e.feature, e.type) for e in edges_store)
    assert kinds == [("activities", "primary"), ("shared-owner", "secondary")]
    # top-level projection stays id-sorted (Stage-5.5 invariant)
    ids = [fl.id or "" for fl in flows_store]
    assert ids == sorted(ids)


def test_f3_position_mint_sees_flows_naturally(tmp_path: Path) -> None:
    """Functional F3′: the re-derived flow lands in the bipartite store
    BEFORE the Stage 6.7 rollup — running the rollup afterwards stamps
    its ``user_flow_id`` with zero extra channels."""
    from faultline.pipeline_v2.stage_6_7_user_flows import (
        run_user_flow_rollup,
    )

    features, snapshot, ctx = _single_call_board(tmp_path)
    flows_store: list[Flow] = []
    edges_store: list[Any] = []
    tele = run_flow_rederive(
        features, flows_store, edges_store, ctx,
        stage3_unit_snapshot=snapshot,
        model="claude-haiku-test",
        client=_FakeAnthropic([_canned_response()]),
    )
    assert tele is not None and tele["flows_added"] == 1

    user_flows, _uf_tele = run_user_flow_rollup(flows_store, features)
    new = features[0].flows[0]
    assert new.user_flow_id is not None
    assert any(
        new.user_flow_id == uf.id for uf in user_flows
    )


def test_f3_position_source_and_registry_order() -> None:
    """Structural F3′: phase_finalize calls the re-derive AFTER the 6.86
    mint window (lane excavation / span split) and BEFORE the 6.7
    rollup; the replay registry mirrors the same order."""
    import inspect

    from faultline.pipeline_v2 import phase_finalize
    from faultline.replay.registry import stage_by_key

    src = inspect.getsource(phase_finalize.run_finalize_phase)
    i_mint = src.index("run_anchored_mint(")
    i_exc = src.index("run_lane_excavation(")
    i_frd = src.index("run_flow_rederive(")
    i_rollup = src.index("run_user_flow_rollup(")
    assert i_mint < i_exc < i_frd < i_rollup

    assert (
        stage_by_key("anchored_mint").order
        < stage_by_key("flow_rederive").order
        < stage_by_key("user_flows").order
    )


# ── cache + determinism ─────────────────────────────────────────────────


def test_cache_fresh_keys_then_replay(tmp_path: Path) -> None:
    """Probe-canon pt.3: the first pass on a NEW grain misses the cache
    (fresh keys); a re-run of the SAME grain replays from cache at $0."""
    backend = _FakeBackend()

    def _run() -> tuple[dict[str, Any], list[Feature]]:
        features, snapshot, ctx = _single_call_board(tmp_path)
        ctx.cache_backend = backend  # type: ignore[attr-defined]
        client = _FakeAnthropic([_canned_response()])
        tele = run_flow_rederive(
            features, [], [], ctx,
            stage3_unit_snapshot=snapshot,
            model="claude-haiku-test",
            client=client,
        )
        assert tele is not None
        return tele, features

    tele1, _ = _run()
    assert tele1["llm_calls"] == 1 and tele1["cache_hits"] == 0
    tele2, _ = _run()
    assert tele2["llm_calls"] == 0 and tele2["cache_hits"] == 1
    assert tele2["flows_added"] == tele1["flows_added"] == 1


def test_deterministic_selection_and_attach(tmp_path: Path) -> None:
    """det ×2 at unit level: two identical runs produce identical
    cohort telemetry and identical attached rows (ids, names, order)."""
    def _run() -> tuple[dict[str, Any], list[str]]:
        features, snapshot, ctx = _twenty_board(tmp_path)
        flows_store: list[Flow] = []
        tele = run_flow_rederive(
            features, flows_store, [], ctx,
            stage3_unit_snapshot=snapshot,
            model="claude-haiku-test",
            _client_factory=lambda: None,
        )
        assert tele is not None
        return tele, [fl.id or "" for fl in flows_store]

    tele1, ids1 = _run()
    tele2, ids2 = _run()
    assert tele1 == tele2
    assert ids1 == ids2


def test_causal_all_excluded_still_reports(tmp_path: Path) -> None:
    """Causal class present but everything excluded → the telemetry key
    fires with honest exclusion counters (no silent drop)."""
    ruler = [_ts(tmp_path, "pkg/docs/D.tsx",
                 ["DocA", "DocB", "DocC"], pad_lines=20)]
    small = [_ts(tmp_path, "pkg/side/S.tsx",
                 ["SideA", "SideB", "SideC"], pad_lines=6000)]
    extra = _ts(tmp_path, "pkg/side/S2.tsx", ["SideD"])
    capped = _dev("side-panel", small + [extra], flows=[
        _flow(f"side-flow-{i}", small[0], i + 1)
        for i in range(MAX_FLOWS_PER_FEATURE)
    ])
    board = [
        _dev("docs", ruler, flows=[
            _flow("view-docs-flow", ruler[0], 1),
            _flow("search-docs-flow", ruler[0], 2),
        ]),
        capped,
    ]
    snapshot = {"docs": sorted(ruler), "side-panel": [small[0]]}
    tele = run_flow_rederive(
        board, [], [], _ctx(tmp_path),
        stage3_unit_snapshot=snapshot,
        model="claude-haiku-test",
        client=_FakeAnthropic(['{"flows": []}']),
    )
    assert tele is not None
    assert tele["cohort_selected"] == 0
    assert tele["excluded_at_cap"] == 1
    assert tele["flows_added"] == 0
