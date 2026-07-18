"""S3 — overturn ledger + arbiter v1 (freeze-barrier forensics).

Every post-construction write of ``product_feature_id`` on a
:class:`~faultline.models.types.Feature` (dev→PF link) or
:class:`~faultline.models.types.UserFlow` (UF-home) is a *proposal* in a
cascade of ~13 passes (``phase_enrich`` → ``6.86-mint`` → ``transport`` →
``terminal-home`` → …). The S3 shadow-probe (2026-07-18,
``/private/tmp/s3-probe``) measured the cascade on three keyless repos and
found: freeze after ``6.86-mint``+``excavation`` absorbs 94.7–98.2 % of the
overturns, and **post-freeze multi-writer conflicts are ZERO on keyless**.

This module installs the ledger + arbiter behind
``FAULTLINE_OVERTURN_ARBITER`` (default **OFF** — unset/=0 is byte-identical
to main, the write path is untouched). When ON:

* An observer wraps ``Feature.__setattr__`` / ``UserFlow.__setattr__`` and
  RECORDS every ``product_feature_id`` overturn (kind, old, new, the writing
  pass = *rung*, and the writer frame) into a scan-scoped ledger. The
  original write ALWAYS runs (write-through) — so downstream reads see the
  same value they see today and the scan output is **byte-identical to OFF
  by construction**. This is the exact mechanism the design was closed on
  (``driver.py`` in the probe).
* The **arbiter** (:func:`finalize_arbiter`) runs once, after the last
  proposer and before Stage 7 output. Its rung-priority replay == the
  current pass order, so it reproduces the cascade byte-for-byte; it emits
  ``scan_meta.overturns`` (the census forensic — "who wanted to throw") and
  ``scan_meta.overturn_conflicts`` (post-freeze multi-writer divergence —
  zero on keyless; any appearance is a signal). Both keys are run-forensic
  telemetry and are stripped by ``normalize_scan`` so they never enter the
  byte-identity comparison.

Threading: the active ledger lives in a ``threading.local`` so concurrent
in-process scans (``run_pipeline_multi``) never cross-contaminate; a scan
with the flag OFF never sets it → pure passthrough.

Deferred write-suppression at the call sites, the I8-guard/conservation
relocation, and the Seg-C/Seg-D behavioural no-ops are NOT part of this v1
(they change output and need their own byte-identity + keyed proofs) — this
ships the ledger + single-point arbiter + telemetry + conflict detector
foundation only.
"""

from __future__ import annotations

import os
import sys
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any

__all__ = [
    "OVERTURN_ARBITER_ENV",
    "overturn_arbiter_enabled",
    "OverturnEntry",
    "OverturnLedger",
    "install_ledger",
    "uninstall_ledger",
    "finalize_arbiter",
    "rung_for_frames",
    "PASS_MAP",
    "POST_FREEZE_RUNGS",
]

OVERTURN_ARBITER_ENV = "FAULTLINE_OVERTURN_ARBITER"


def overturn_arbiter_enabled() -> bool:
    """Default OFF. ``FAULTLINE_OVERTURN_ARBITER=1`` installs the ledger.

    unset / ``0`` / ``false`` / ``off`` → the observer is never installed
    and the scan is byte-identical to main.
    """
    return os.environ.get(OVERTURN_ARBITER_ENV, "0").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


# ── Rung map — filename → pass name ──────────────────────────────────────
# Single source of truth for "who proposed": the writing pass is resolved
# from the innermost ``faultline`` frame on the stack at write time. Mirrors
# the probe's PASS_MAP so the emitted census reconciles with
# ``/private/tmp/s3-probe/out/analysis.json`` writer-for-writer.
PASS_MAP: dict[str, str] = {
    "stage_6_86_anchored_mint.py": "6.86-mint",
    "stage_6_88_sibling_unify.py": "6.88-unify",
    "phase_enrich.py": "phase_enrich",
    "hub_relation.py": "hub",
    "transport_handoff.py": "transport",
    "lane_excavation.py": "excavation",
    "lane_rehome.py": "lane_rehome",
    "devgrain_demote.py": "devgrain",
    "emission_integrity.py": "emission-I12",
    "stage_8_5_member_backfill.py": "8.5-backfill",
    "uf_terminal_home.py": "terminal-home",
    "conservation.py": "conservation",
    "stage_6_99_i16_rehome.py": "i16",
    "stage_6_99b_post_uf_rehome.py": "6.99b",
    "mega_pf_nav_rehome.py": "mega",
    "dispatch_homing.py": "dispatch",
    "stage_6_7d_llm_journey_abstraction.py": "6.7d",
    "surface_taxonomy.py": "taxonomy",
    "phase_finalize.py": "phase_finalize",
    "phase_layer2.py": "phase_layer2",
    "ws_blob_domain_drain.py": "ws_blob",
    "journey_lattice.py": "lattice",
}

# Passes that run AFTER the freeze barrier (6.86-mint + excavation). The
# probe measured ZERO post-freeze multi-writer conflicts on keyless: any
# entity written by ≥2 DISTINCT rungs from this set, with divergent values,
# is a conflict the arbiter must surface. Pre-freeze writers (phase_enrich,
# hub, 8.5-backfill, stage-8) write into the void that 6.86 overwrites and
# are NOT conflicts.
POST_FREEZE_RUNGS: frozenset[str] = frozenset({
    "transport", "devgrain", "lane_rehome", "6.88-unify",
    "emission-I12", "terminal-home", "conservation", "taxonomy",
    "phase_finalize", "i16", "6.99b", "mega", "dispatch",
})


def _basename(frame_str: str) -> str:
    # frame_str == "basename.py:func:lineno"
    return frame_str.split(":", 1)[0]


def rung_for_frames(frames: list[str]) -> str:
    """Resolve the writing pass from the innermost faultline frame."""
    if not frames:
        return "<unknown>"
    fn = _basename(frames[0])
    return PASS_MAP.get(fn, fn[:-3] if fn.endswith(".py") else fn)


# ── Ledger data model ────────────────────────────────────────────────────


@dataclass
class OverturnEntry:
    """One recorded ``product_feature_id`` overturn proposal."""

    kind: str            # "dev" | "uf"
    serial: int          # stable per-object identity within the scan
    eid: str | None
    ename: str | None
    layer: str | None
    old: str | None
    new: str | None
    rung: str
    writer: str          # "basename.py:func:lineno" of the writing frame


class OverturnLedger:
    """Scan-scoped journal of every ``product_feature_id`` overturn.

    Populated by the setattr observer; consumed once by
    :func:`finalize_arbiter`. Holds strong references to the written objects
    so ``id()`` never recycles a serial mid-scan (probe lesson).
    """

    def __init__(self) -> None:
        self.entries: list[OverturnEntry] = []
        self._serial_by_id: dict[int, int] = {}
        self._keep: list[Any] = []

    # -- recording -------------------------------------------------------
    def _serial(self, obj: Any) -> int:
        k = id(obj)
        s = self._serial_by_id.get(k)
        if s is None:
            s = len(self._keep)
            self._serial_by_id[k] = s
            self._keep.append(obj)
        return s

    def record(
        self, kind: str, obj: Any, old: str | None, new: str | None,
        frames: list[str],
    ) -> None:
        d = getattr(obj, "__dict__", {})
        self.entries.append(OverturnEntry(
            kind=kind,
            serial=self._serial(obj),
            eid=d.get("id") or d.get("name"),
            ename=d.get("name"),
            layer=d.get("layer"),
            old=old,
            new=new,
            rung=rung_for_frames(frames),
            writer=frames[0] if frames else "<unknown>",
        ))

    # -- grouping --------------------------------------------------------
    def _by_entity(self, kind: str) -> dict[int, list[OverturnEntry]]:
        out: dict[int, list[OverturnEntry]] = {}
        for e in self.entries:
            if e.kind == kind:
                out.setdefault(e.serial, []).append(e)
        return out

    # -- arbiter: rung-priority replay ----------------------------------
    def replay(self, kind: str) -> dict[int, str | None]:
        """Final value per entity = last proposal in rung (record) order.

        Record order == execution order == rung-priority at v1, so this
        reproduces the cascade's last-writer-wins result byte-for-byte.
        """
        final: dict[int, str | None] = {}
        for e in self.entries:
            if e.kind == kind:
                final[e.serial] = e.new
        return final

    def verify_replay(self, features: list[Any], user_flows: list[Any]) -> int:
        """Count entities whose live ``product_feature_id`` differs from the
        ledger replay-final. For write-through this is 0 for every object
        the ledger observed AND that is still alive & not re-minted by a
        non-observed path (SimpleNamespace carve / constructor). Reported,
        never asserted, on real scans (dropped/re-minted entities differ).
        """
        mism = 0
        for kind, live in (("dev", features), ("uf", user_flows)):
            final = self.replay(kind)
            id_to_serial = self._serial_by_id
            for obj in live:
                s = id_to_serial.get(id(obj))
                if s is None or s not in final:
                    continue
                if getattr(obj, "product_feature_id", None) != final[s]:
                    mism += 1
        return mism

    # -- conflict detector ----------------------------------------------
    def conflicts(self) -> list[dict[str, Any]]:
        """Post-freeze multi-writer divergence — zero on keyless by the
        probe; any row is a signal. An entity qualifies when ≥2 DISTINCT
        post-freeze rungs proposed ≥2 DISTINCT values for it.
        """
        out: list[dict[str, Any]] = []
        for kind in ("dev", "uf"):
            for serial, es in self._by_entity(kind).items():
                post = [e for e in es if e.rung in POST_FREEZE_RUNGS]
                rungs = {e.rung for e in post}
                vals = {e.new for e in post}
                if len(rungs) >= 2 and len(vals) >= 2:
                    out.append({
                        "kind": kind,
                        "eid": es[-1].eid,
                        "ename": es[-1].ename,
                        "writers": [e.rung for e in post],
                        "values": [e.new for e in post],
                    })
        return out

    # -- census (reconciles with the probe analysis.json) ----------------
    def census(self, kind: str) -> dict[str, Any]:
        ents = self._by_entity(kind)
        writes = [e for es in ents.values() for e in es]
        fills = [e for e in writes if e.old is None]
        overturns = [e for e in writes if e.old is not None]
        clears = [e for e in overturns if e.new is None]
        per_writer = Counter(e.rung for e in writes)
        per_writer_ot = Counter(e.rung for e in overturns)
        return {
            "entities_written": len(ents),
            "writes": len(writes),
            "fills(None->X)": len(fills),
            "overturns(X->Y)": len(overturns),
            "clears(X->None)": len(clears),
            "per_writer_all": dict(per_writer.most_common()),
            "per_writer_overturns": dict(per_writer_ot.most_common()),
        }

    def exhibits(self, kind: str, limit: int = 12) -> list[dict[str, Any]]:
        """Longest overturn chains — the forensic exhibits (probe shape)."""
        ents = self._by_entity(kind)
        ranked = sorted(
            ents.values(),
            key=lambda es: -len([e for e in es if e.old is not None]),
        )
        out: list[dict[str, Any]] = []
        for es in ranked[:limit]:
            if len(es) < 2:
                continue
            out.append({
                "eid": es[-1].eid,
                "ename": es[-1].ename,
                "seq": [{"w": e.rung, "old": e.old, "new": e.new} for e in es],
            })
        return out

    def scan_meta_payload(
        self, features: list[Any], user_flows: list[Any],
    ) -> dict[str, Any]:
        return {
            "journal_writes": len(self.entries),
            "replay_mismatches": self.verify_replay(features, user_flows),
            "dev": self.census("dev"),
            "uf": self.census("uf"),
            "dev_exhibits": self.exhibits("dev"),
            "uf_exhibits": self.exhibits("uf"),
        }


# ── Observer install / uninstall ─────────────────────────────────────────

_active = threading.local()
_patched = False
_orig_setattr: dict[type, Any] = {}


def _current_ledger() -> OverturnLedger | None:
    return getattr(_active, "ledger", None)


def _frames(limit: int = 4) -> list[str]:
    out: list[str] = []
    try:
        f: Any = sys._getframe(2)  # skip _frames + traced
    except ValueError:
        return out
    depth = 0
    while f is not None and depth < 30 and len(out) < limit:
        fn = f.f_code.co_filename
        if "faultline" in fn and "overturn_ledger" not in fn:
            out.append("%s:%s:%d" % (
                os.path.basename(fn), f.f_code.co_name, f.f_lineno,
            ))
        f = f.f_back
        depth += 1
    return out


def _install_class_patch() -> None:
    """Wrap ``Feature``/``UserFlow`` ``__setattr__`` ONCE per process.

    The wrapper is a pure passthrough whenever no ledger is active on the
    current thread — so a flag-OFF scan (which never sets the thread-local)
    is byte-identical to unpatched, and a class patched by a prior ON scan
    cannot leak into a concurrent OFF scan.
    """
    global _patched
    if _patched:
        return
    from faultline.models.types import Feature, UserFlow

    def _make(orig: Any, kind: str) -> Any:
        def traced(self: Any, name: str, value: Any) -> None:
            if name == "product_feature_id":
                led = _current_ledger()
                if led is not None:
                    old = self.__dict__.get("product_feature_id")
                    if old != value:
                        led.record(kind, self, old, value, _frames())
            orig(self, name, value)
        return traced

    for cls, kind in ((Feature, "dev"), (UserFlow, "uf")):
        orig = cls.__setattr__
        _orig_setattr[cls] = orig
        cls.__setattr__ = _make(orig, kind)  # type: ignore[method-assign]
    _patched = True


def install_ledger(ledger: OverturnLedger) -> None:
    """Activate ``ledger`` for the current thread's scan."""
    _install_class_patch()
    _active.ledger = ledger


def uninstall_ledger() -> None:
    """Deactivate the ledger for the current thread (class stays patched
    but reverts to passthrough)."""
    _active.ledger = None


# ── Arbiter — single application point ───────────────────────────────────


def finalize_arbiter(
    ledger: OverturnLedger,
    features: list[Any],
    user_flows: list[Any],
    scan_meta: dict[str, Any],
) -> None:
    """Run the arbiter once (after the last proposer, before Stage 7).

    Emits the census forensic + post-freeze conflict census. Rung-priority
    replay == current pass order → byte-identical cascade result (verified
    by ``replay_mismatches`` and the ON==OFF gate). Telemetry only — the
    two keys are stripped by ``normalize_scan``.
    """
    scan_meta["overturns"] = ledger.scan_meta_payload(features, user_flows)
    scan_meta["overturn_conflicts"] = ledger.conflicts()
