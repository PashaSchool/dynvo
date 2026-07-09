"""B19 — transport-package lane (design-review, FAULTLINE_TECH_TRANSPORT_LANE).

Mechanism: a ws-package NAMED after its own external dependency family
('packages/trpc' -> dep '@trpc/*') that is broadly imported AND fans out into
domain lanes as a technology_instrument when the flag is ON — the S2 fan-out
guard (len(dou)<=1) is waived for the name-dep prong ONLY.

NOTE on coverage: the S2 asymmetry prong depends on a resolved CROSS-UNIT
import graph (in_units>=3), which the existing detector suite validates via
REAL-REPO probes, not synthetic tmp_path monorepos (no synthetic S2 test
exists in this module). B19 follows that precedent — the mechanism's gate is
the real-repo over-lane audit ($ARC/b19-audit): flag OFF vs ON on
documenso/midday/supabase, asserting ONLY transport packages lane away and
zero domain cores are disturbed. Here we cover the flag wiring + the strict
OFF no-op contract that the kill-switch gate rests on.
"""

from __future__ import annotations

from faultline.pipeline_v2.technology_instruments import (
    TRANSPORT_LANE_ENV,
    transport_lane_enabled,
)


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv(TRANSPORT_LANE_ENV, raising=False)
    assert transport_lane_enabled() is False   # default OFF (unratified)


def test_flag_off_explicit(monkeypatch):
    for v in ("0", "false", "False", ""):
        monkeypatch.setenv(TRANSPORT_LANE_ENV, v)
        assert transport_lane_enabled() is False


def test_flag_on(monkeypatch):
    for v in ("1", "true", "True"):
        monkeypatch.setenv(TRANSPORT_LANE_ENV, v)
        assert transport_lane_enabled() is True
