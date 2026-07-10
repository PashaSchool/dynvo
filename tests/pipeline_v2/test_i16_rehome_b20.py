"""B20 — path_index-aware I16 journey re-home.

Required anti-cases (the fix's soul):
  (a) a MINORITY-foreign UF is left untouched;
  (b) a majority-foreign UF whose best entry-owner is a PLURALITY-but-not-
      strict-majority does NOT re-home (the churn guard);
  (c) flag=0 restores (no-op).
Plus: a strict-majority majority-foreign UF re-homes; lane/None owners are never
targets; the re-home clears I16 by construction.
"""

from __future__ import annotations

from faultline.pipeline_v2.stage_6_99_i16_rehome import (
    REHOME_ENV,
    i16_rehome_enabled,
    rehome_foreign_entry_ufs,
)


class F:  # dev feature
    def __init__(self, uuid, pfid, flows):
        self.uuid = uuid
        self.product_feature_id = pfid
        self.layer = "developer"
        self.flows = flows


class Fl:  # flow
    def __init__(self, uuid, ep):
        self.uuid = uuid
        self.entry_point_file = ep


class PF:
    def __init__(self, name):
        self.name = name
        self.uuid = f"pf-{name}"
        self.layer = "product"


class UF:
    def __init__(self, name, pfid, members):
        self.name = name
        self.product_feature_id = pfid
        self.member_flow_ids = members


def _scene(entry_owners):
    """entry_owners: list of (flow_uuid, entry_file, owner_pf) — builds a
    path_index + one dev per owner so file_owner_pf maps entry_file->owner_pf.
    Returns (user_flows_factory, features, pfs, path_index)."""
    pfs = {}
    devs = []
    pidx = {}
    flows_by_owner = {}
    for i, (fuid, ef, owner) in enumerate(entry_owners):
        pfs.setdefault(owner, PF(owner))
        fl = Fl(fuid, ef)
        d = F(f"dev-{i}", owner, [fl])
        devs.append(d)
        pidx[ef] = {"feature_uuid": d.uuid}
        flows_by_owner[fuid] = ef
    return devs, list(pfs.values()), pidx


def test_minority_foreign_untouched():
    # 3 entries: 2 home (A), 1 foreign (B) -> mis/chk = 1/3 <= 0.5 -> untouched.
    devs, pfs, pidx = _scene([
        ("f1", "a/1.ts", "A"), ("f2", "a/2.ts", "A"), ("f3", "b/1.ts", "B")])
    uf = UF("j", "A", ["f1", "f2", "f3"])
    tele = rehome_foreign_entry_ufs([uf], devs, pfs, pidx)
    assert tele["rehomed"] == 0
    assert uf.product_feature_id == "A"


def test_plurality_not_majority_does_not_rehome():
    # home A: 1; foreign B: 2, C: 2 (chk=5, mis=4 -> majority-foreign). Best
    # owner B=2/5=0.4 -> NOT strict majority -> churn guard blocks (distributed).
    devs, pfs, pidx = _scene([
        ("f1", "a/1.ts", "A"),
        ("f2", "b/1.ts", "B"), ("f3", "b/2.ts", "B"),
        ("f4", "c/1.ts", "C"), ("f5", "c/2.ts", "C")])
    uf = UF("j", "A", ["f1", "f2", "f3", "f4", "f5"])
    tele = rehome_foreign_entry_ufs([uf], devs, pfs, pidx)
    assert tele["rehomed"] == 0            # plurality (B=2) is not > 50%
    assert uf.product_feature_id == "A"


def test_strict_majority_rehomes_and_clears():
    # home A: 1; foreign B: 3 (chk=4, mis=3 majority-foreign). B=3/4=0.75 strict
    # majority -> re-home to B; new foreign = 1/4 < 0.5 (cleared).
    devs, pfs, pidx = _scene([
        ("f1", "a/1.ts", "A"),
        ("f2", "b/1.ts", "B"), ("f3", "b/2.ts", "B"), ("f4", "b/3.ts", "B")])
    uf = UF("j", "A", ["f1", "f2", "f3", "f4"])
    tele = rehome_foreign_entry_ufs([uf], devs, pfs, pidx)
    assert tele["rehomed"] == 1
    assert uf.product_feature_id == "B"
    m = tele["moves"][0]
    assert m["was_foreign"] == "3/4" and m["now_foreign"] == "1/4"


def test_lane_entry_never_a_target():
    # majority-foreign via lane entries; the lane owner is excluded -> the only
    # real PF owner is a minority -> NOT re-homed (lane-entry residual).
    devs, pfs, pidx = _scene([
        ("f1", "a/1.ts", "A"), ("f2", "b/1.ts", "B")])
    # add 3 lane-owned entries (a dev with pfid=None + lane uuid)
    class LaneDev:
        def __init__(s, uuid, flows): s.uuid=uuid; s.product_feature_id=None; s.layer="developer"; s.flows=flows
    lane_flows = [Fl(f"L{i}", f"lane/{i}.ts") for i in range(3)]
    lane_dev = LaneDev("lane-dev", lane_flows)
    for i in range(3):
        pidx[f"lane/{i}.ts"] = {"feature_uuid": "lane-dev"}
    devs.append(lane_dev)
    lane_rows = [{"uuid": "lane-dev"}]
    uf = UF("j", "A", ["f1", "f2", "L0", "L1", "L2"])
    tele = rehome_foreign_entry_ufs([uf], devs, pfs, pidx, lane_rows)
    # chk=5 (2 PF + 3 lane), mis=4 (B + 3 lane) -> majority-foreign; best PF
    # owner B=1/5=0.2 -> not strict majority -> untouched.
    assert tele["rehomed"] == 0
    assert uf.product_feature_id == "A"


def test_flag_off_noop(monkeypatch):
    monkeypatch.setenv(REHOME_ENV, "0")
    assert i16_rehome_enabled() is False


def test_flag_on_default(monkeypatch):
    monkeypatch.delenv(REHOME_ENV, raising=False)
    assert i16_rehome_enabled() is True
