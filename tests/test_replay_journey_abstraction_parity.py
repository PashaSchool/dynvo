"""G-class parity — replay 6.7d invokes the live seam (B74 Seg C infra).

The live site (phase_finalize:1092) passes ``anchored=
anchored_mint_applied`` and ``interior_evidence=`` into
``run_journey_abstraction``. The replay runner omitted both, so a 6.7d
replay of an anchored scan degraded to the Call-2-era path (constrained
Call-1 cache namespace never matched; anchored maps never applied).
These tests pin the parity: the replay runner must forward BOTH kwargs,
derived from the recorded chain/scan_meta exactly like the live run.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import faultline.pipeline_v2.stage_6_55_page_interior as s655
import faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction as s67d
from faultline.replay.registry import ReplayEnv, _run_journey_abstraction


def _env(tmp_path: Path) -> ReplayEnv:
    run_dir = tmp_path / "new-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return ReplayEnv(run_dir=run_dir, run_id="replay-parity-test")


def _state(tmp_path: Path, chain: dict[str, Any] | None = None,
           scan_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "features": [],
        "user_flows": [],
        "product_features": [],
        "scan_meta": scan_meta if scan_meta is not None else {},
        "repo_path": str(tmp_path / "repo"),
        "routes_index": [],
        "model_id": "test-model",
    }
    if chain is not None:
        state["_chain"] = chain
    return state


def _capture(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _stub(user_flows, product_features, features, routes_index, **kw):
        calls.append(kw)
        return user_flows, product_features, None, {"applied": False}

    monkeypatch.setattr(s67d, "run_journey_abstraction", _stub)
    return calls


def test_anchored_chain_forwards_anchored_true(tmp_path, monkeypatch) -> None:
    calls = _capture(monkeypatch)
    state = _state(tmp_path, chain={
        "anchored_mint_applied": True,
        "interior_result": SimpleNamespace(active=False),
    })
    _run_journey_abstraction(_env(tmp_path), state)
    (kw,) = calls
    assert kw["anchored"] is True
    # 6.55 inactive on the recorded run -> live built no evidence either.
    assert kw["interior_evidence"] is None


def test_active_interior_result_builds_evidence(tmp_path, monkeypatch) -> None:
    calls = _capture(monkeypatch)
    sentinel = {"by_pf": {}, "pages": {}}
    built: list[tuple[Any, ...]] = []

    def _evidence_stub(interior_result, features, product_features):
        built.append((interior_result, features, product_features))
        return sentinel

    monkeypatch.setattr(s655, "build_interior_evidence", _evidence_stub)
    interior = SimpleNamespace(active=True)
    state = _state(tmp_path, chain={
        "anchored_mint_applied": True,
        "interior_result": interior,
    })
    _run_journey_abstraction(_env(tmp_path), state)
    (kw,) = calls
    assert kw["anchored"] is True
    assert kw["interior_evidence"] is sentinel
    # Built from the SAME triple the live site passes.
    assert built == [(interior, state["features"], state["product_features"])]


def test_unanchored_scan_keeps_legacy_call_shape(tmp_path, monkeypatch) -> None:
    """Call-2-era replays (no mint) keep anchored=False + no evidence."""
    calls = _capture(monkeypatch)
    state = _state(tmp_path)  # no chain, empty scan_meta -> mint not applied
    _run_journey_abstraction(_env(tmp_path), state)
    (kw,) = calls
    assert kw["anchored"] is False
    assert kw["interior_evidence"] is None


def test_scan_meta_fallback_detects_anchored_mint(tmp_path, monkeypatch) -> None:
    """Chain-less state (older capture): mint telemetry in scan_meta is
    the fallback authority — same rule ``_anchored_mint_applied`` uses."""
    calls = _capture(monkeypatch)
    state = _state(tmp_path, scan_meta={
        "stage_6_86_anchored_mint": {"applied": True},
    })
    _run_journey_abstraction(_env(tmp_path), state)
    (kw,) = calls
    assert kw["anchored"] is True


def test_replay_kwargs_are_live_signature_kwargs(tmp_path, monkeypatch) -> None:
    """Signature parity: every kwarg the replay forwards must exist on
    the live ``run_journey_abstraction`` signature, and the two seam
    kwargs the live site sets (anchored / interior_evidence) must BOTH
    be forwarded explicitly by the replay runner."""
    live_params = set(
        inspect.signature(s67d.run_journey_abstraction).parameters)
    calls = _capture(monkeypatch)
    state = _state(tmp_path, chain={"anchored_mint_applied": True})
    _run_journey_abstraction(_env(tmp_path), state)
    (kw,) = calls
    assert set(kw) <= live_params
    assert {"anchored", "interior_evidence"} <= set(kw)
