"""Iteration-5 grain surgery — Stage 8.9.6 mega-anchor service-domain carve-out.

Case C (Soc0 2026-07-05): the shared ``backend`` anchor owned the flows for
``backend/services/investigation_playbook/*`` (8 flows), so the journey
"Manage investigation playbooks" was stuck on the shared/platform bucket
(validator I10). The carve-out lifts each service-domain subtree into its own
developer feature that carries those flows, so 6.7d resettles the journey.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, Flow, FlowLineRange
from faultline.pipeline_v2.stage_8_9_6_domain_member_attribution import (
    carve_service_domains,
)

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("FAULTLINE_STAGE_8_9_6_DOMAIN_ATTRIBUTION", "1")
    monkeypatch.setenv("FAULTLINE_STAGE_8_9_6_SERVICE_CARVE", "1")


def _flow(uuid: str, entry: str) -> Flow:
    return Flow(
        name=f"{uuid}-flow", uuid=uuid, entry_point_file=entry, paths=[entry],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0,
        line_ranges=[FlowLineRange(path=entry, start_line=1, end_line=20)],
    )


def _anchor(name: str, flows: list[Flow], paths: list[str] | None = None,
            workspace: bool = True) -> Feature:
    return Feature(
        name=name, display_name=name, paths=paths or ["backend/main.py"],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0, layer="developer",
        flows=flows,
        description=("workspace anchor 'backend' from monorepo package "
                     "'backend/'" if workspace else "app backend code"),
    )


def _svc_flows(domain: str, n: int, prefix: str = "backend/services") -> list[Flow]:
    return [_flow(f"{domain}{i}", f"{prefix}/{domain}/file{i}.py")
            for i in range(n)]


# ── core carve ───────────────────────────────────────────────────────────────


def test_carves_service_domain_and_moves_flows():
    ipb = _svc_flows("investigation_playbook", 5)
    edr = _svc_flows("edr", 4)
    anchor = _anchor("backend", ipb + edr)
    feats = [anchor]
    res = carve_service_domains(feats)
    assert res.enabled and res.anchors_carved == 1
    assert res.domains_carved == 2
    carved = {f.name: f for f in feats if f.name != "backend"}
    assert "investigation-playbook" in carved
    assert "edr" in carved
    # the carved dev CARRIES the domain's flows; the anchor no longer does
    assert len(carved["investigation-playbook"].flows) == 5
    assert anchor.flows == []
    # files claimed as owned paths (I1/I2 satisfied downstream)
    assert len(carved["investigation-playbook"].paths) == 5


def test_below_floor_domain_not_carved():
    anchor = _anchor("backend",
                     _svc_flows("investigation_playbook", 5)
                     + _svc_flows("tiny", 2))
    feats = [anchor]
    carve_service_domains(feats)
    names = {f.name for f in feats}
    assert "investigation-playbook" in names
    assert "tiny" not in names  # 2 files < floor of 3


def test_generic_and_infra_domains_skipped():
    anchor = _anchor("backend",
                     _svc_flows("investigation_playbook", 3)
                     + _svc_flows("mock_data", 4)
                     + _svc_flows("common", 4))
    feats = [anchor]
    carve_service_domains(feats)
    names = {f.name for f in feats}
    assert "investigation-playbook" in names
    assert "mock-data" not in names  # infra
    assert "common" not in names     # generic


def test_no_self_carve():
    """A domain that names the source dev itself is never carved."""
    anchor = _anchor("investigation-playbook",
                     _svc_flows("investigation_playbook", 4)
                     + _svc_flows("edr", 4))
    feats = [anchor]
    carve_service_domains(feats)
    # investigation-playbook stays whole (self); only edr carves
    names = [f.name for f in feats]
    assert names.count("investigation-playbook") == 1
    assert "edr" in names


def test_non_anchor_single_domain_not_carved():
    """A focused (non-workspace-anchor) dev with a single service domain keeps
    its grain — only mega-anchors (workspace anchor OR >= 2 domains) carve."""
    dev = _anchor("payments", _svc_flows("payments_gateway", 5),
                  workspace=False)
    feats = [dev]
    res = carve_service_domains(feats)
    assert res.anchors_carved == 0
    assert {f.name for f in feats} == {"payments"}


def test_non_anchor_multi_domain_carves():
    dev = _anchor("core", _svc_flows("investigation_playbook", 3)
                  + _svc_flows("edr", 3), workspace=False)
    feats = [dev]
    res = carve_service_domains(feats)
    assert res.anchors_carved == 1
    assert {"investigation-playbook", "edr"} <= {f.name for f in feats}


def test_mixed_flow_not_carved():
    """A flow whose files are NOT majority under one service domain stays with
    the anchor."""
    mixed = Flow(
        name="mixed-flow", uuid="m1",
        entry_point_file="backend/services/edr/a.py",
        paths=["backend/services/edr/a.py", "backend/routers/x.py",
               "backend/routers/y.py"],
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_TS, health_score=90.0,
        line_ranges=[FlowLineRange(path="backend/services/edr/a.py",
                                   start_line=1, end_line=5)],
    )
    anchor = _anchor("backend", _svc_flows("investigation_playbook", 3)
                     + [mixed])
    feats = [anchor]
    carve_service_domains(feats)
    # mixed flow stays on the anchor (edr had only the 1 minority file)
    assert any(f.uuid == "m1" for f in anchor.flows)
    assert "edr" not in {f.name for f in feats}


# ── discipline: disabled, determinism, scale ─────────────────────────────────


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setenv("FAULTLINE_STAGE_8_9_6_SERVICE_CARVE", "0")
    anchor = _anchor("backend", _svc_flows("investigation_playbook", 5))
    feats = [anchor]
    res = carve_service_domains(feats)
    assert not res.enabled
    assert {f.name for f in feats} == {"backend"}


def test_deterministic():
    def run():
        a = _anchor("backend", _svc_flows("investigation_playbook", 5)
                    + _svc_flows("edr", 4) + _svc_flows("threat_hunts", 3))
        feats = [a]
        carve_service_domains(feats)
        return [(f.name, sorted(f.paths), sorted(x.uuid for x in f.flows))
                for f in feats]
    assert run() == run()


@pytest.mark.parametrize("n_files", [3, 12, 200])
def test_scale_invariant_floor(n_files):
    """The >= 3 subsystem floor holds at tiny / medium / large domain sizes."""
    anchor = _anchor("backend", _svc_flows("investigation_playbook", n_files))
    feats = [anchor]
    res = carve_service_domains(feats)
    assert res.domains_carved == 1
    carved = next(f for f in feats if f.name == "investigation-playbook")
    assert len(carved.paths) == n_files
