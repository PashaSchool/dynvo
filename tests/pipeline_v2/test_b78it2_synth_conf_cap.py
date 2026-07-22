"""B78-it2 Goal 2b — synthesized-backstop confidence cap (Seg A rider).

Ruling (operator, 2026-07-22): a SYNTHESIZED backstop UF never wears
``name_confidence="high"`` — the row is a coverage instrument whose
resource/verb rungs echo the template the backstop itself composed (the
Soc0 UF-005 'API' exhibit: ``synthesized=True``,
``synthesis_reason=uncovered_product_feature_backstop``, yet
``high``/``['structural-route']`` on the keyed board). Armed by
``FAULTLINE_FOLD_EVIDENCE_WEIGHT`` (the Seg A flag, WITH REASON: the
uncovered-PF backstop row only exists because Seg A minted its PF), the
Law-C promotion chain skips synthesized rows: confidence caps at ``low``
and ``name_evidence`` carries the honest ``synthesized`` stamp.

Pins:
  1. Exhibit UF-005 shape — synthesized row that WOULD promote to high
     caps at low + ``['synthesized']`` under the flag.
  2. Anti-case — the SAME scene with ``synthesized=False`` still promotes
     to high with its real rungs (observed journeys are untouched).
  3. Kill-switch — flag unset/=0 ⇒ the synthesized row keeps the flagless
     verdict byte-identically (no cap, no stamp).
  4. Determinism — two armed runs identical.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, UserFlow
from faultline.pipeline_v2.naming_contract import (
    _apply_uf_name_laws,
    load_naming_vocab,
)
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    FOLD_EVIDENCE_WEIGHT_ENV,
)

_EPOCH = datetime.fromtimestamp(0, timezone.utc)


def _pf(slug: str, display: str) -> Feature:
    f = Feature(name=slug, paths=[], authors=[], total_commits=0, bug_fixes=0,
                bug_fix_ratio=0.0, last_modified=_EPOCH, health_score=80.0,
                layer="product")
    f.display_name = display
    return f


def _uf(uid: str, name: str, pfid: str, members: list[str], *,
        resource: str, synthesized: bool = False,
        reason: str | None = None) -> UserFlow:
    return UserFlow(
        id=uid, name=name, intent="manage", resource=resource,
        product_feature_id=pfid, member_flow_ids=members,
        member_count=len(members), domain=None, name_confidence="low",
        synthesized=synthesized, synthesis_reason=reason)


def _apply(ufs, pfs, flow_names):
    vocab = load_naming_vocab()
    pf_by_slug = {str(p.name): p for p in pfs}
    tele: dict = {}
    _apply_uf_name_laws(ufs, pf_by_slug, vocab, flow_names, tele,
                        authored_ids=set(), keeper_on=True,
                        nav_labels={}, flow_origin_by_id={})
    return tele


def _backstop_scene(*, synthesized: bool):
    """The UF-005 'API' shape at unit grain: members ground both resource
    ('invoice' echoes) and verb ('create'/'list' leads) — the flagless
    rubric promotes this scene to high + ['structural-route'] (the exact
    evidence the keyed exhibit wore)."""
    flow_names = {"c": "create-invoice-flow", "l": "list-invoices-flow"}
    ufs = [_uf("UF-005", "Manage invoices", "invoices", ["c", "l"],
               resource="invoice", synthesized=synthesized,
               reason="uncovered_product_feature_backstop"
               if synthesized else None)]
    pfs = [_pf("invoices", "Invoices")]
    return ufs, pfs, flow_names


# ── 1. exhibit: synthesized row caps at low + stamp ──────────────────────

def test_synthesized_backstop_caps_low_with_stamp(monkeypatch) -> None:
    monkeypatch.setenv(FOLD_EVIDENCE_WEIGHT_ENV, "1")
    ufs, pfs, fn = _backstop_scene(synthesized=True)
    _apply(ufs, pfs, fn)
    assert ufs[0].name_confidence == "low"
    assert ufs[0].name_evidence == ["synthesized"]


# ── 2. anti-case: an observed (non-synthesized) journey still promotes ───

def test_observed_journey_untouched(monkeypatch) -> None:
    monkeypatch.setenv(FOLD_EVIDENCE_WEIGHT_ENV, "1")
    ufs, pfs, fn = _backstop_scene(synthesized=False)
    _apply(ufs, pfs, fn)
    assert ufs[0].name_confidence == "high"
    assert ufs[0].name_evidence == ["structural-route"]


# ── 3. kill-switch: flag off ⇒ flagless verdict byte-identically ─────────

@pytest.mark.parametrize("off", [None, "0", "false"])
def test_kill_switch_off_is_flagless(monkeypatch, off) -> None:
    if off is None:
        monkeypatch.delenv(FOLD_EVIDENCE_WEIGHT_ENV, raising=False)
    else:
        monkeypatch.setenv(FOLD_EVIDENCE_WEIGHT_ENV, off)
    ufs, pfs, fn = _backstop_scene(synthesized=True)
    _apply(ufs, pfs, fn)
    # The flagless world promotes on the real rungs — no cap, no stamp.
    assert ufs[0].name_confidence == "high"
    assert ufs[0].name_evidence == ["structural-route"]


# ── 4. determinism ───────────────────────────────────────────────────────

def test_determinism(monkeypatch) -> None:
    monkeypatch.setenv(FOLD_EVIDENCE_WEIGHT_ENV, "1")

    def _run():
        ufs, pfs, fn = _backstop_scene(synthesized=True)
        _apply(ufs, pfs, fn)
        return [(u.name, u.name_confidence, u.name_evidence) for u in ufs]

    assert _run() == _run()
