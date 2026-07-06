"""Product-Spine Wave 2a — no-signal UF terminal home (validator I21).

A user journey must NEVER ship with a null ``product_feature_id`` (operator
doctrine: a journey is by definition a product journey — I21). Yet two
legal mechanisms produce nulls today:

  * the §4.5 conservation finalize pass NULLS a shared-platform binding
    when no real PF owns any of the journey's spans/entries
    (``null_shared_without_signal`` — a UF may never ship attached to
    Shared Platform, and the richer ladders found nothing better). The
    W1.1 validation wave shipped exactly this class: Soc0's "Browse
    aggregated EDR detections" + "Trigger and monitor background cron
    jobs" (I21 0→2);
  * emission integrity nulls a dangling ref whose canonical re-slug match
    fails (I12 repair).

This module supplies the deterministic TERMINAL ladder, run AFTER emission
integrity (so it assigns only surviving product-list keys and nothing can
re-null its work):

  (a) SYSTEM journeys (Stage 6.8b ``category == "system"`` / trigger
      cron|queue|webhook) prefer a **system-scope** product feature (the
      surface-taxonomy tag) — background jobs attach to the background-jobs
      capability, not to whatever code neighborhood is largest;
  (b) else (and for system journeys with no system-scope PF) the
      **span-argmax real PF even below majority** — first over direct
      file→dev→PF ownership votes (the §4.5 ruler), then over
      NEAREST-DIRECTORY ownership: each member file walks up its ancestor
      directories and votes for the real PFs owning files under the
      nearest populated level (deterministic, scale-invariant — no tuned
      constants);
  (c) never null, never Shared Platform.

Every binding minted here (and any argmax binding that failed the
strict-majority conservation bar) is tagged ``binding_confidence="low"`` so
consumers can rank/inspect weak attachments (UserFlow field, omitted when
unset).

Degenerate case (documented residual): a scan whose product list contains
NO real PF with owned paths keeps its nulls — there is nothing legal to
bind to; the repo-class gate suppresses journeys on non-product repos
before this can occur in practice.

Deterministic, $0 LLM. Kill-switch: ``FAULTLINE_SPINE_UF_TERMINAL_HOME=0``
(default ON).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from faultline.pipeline_v2.conservation import (
    build_file_pf_owner,
    dev_views_for,
    member_votes,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature, UserFlow

__all__ = [
    "UF_TERMINAL_HOME_ENV",
    "terminal_home_enabled",
    "assign_terminal_homes",
]

UF_TERMINAL_HOME_ENV = "FAULTLINE_SPINE_UF_TERMINAL_HOME"

_SHARED_PF_KEYS = frozenset(("shared-platform", "platform"))
_SYSTEM_TRIGGERS = frozenset(("scheduled", "queue", "webhook"))


def terminal_home_enabled() -> bool:
    """Default ON; ``FAULTLINE_SPINE_UF_TERMINAL_HOME=0`` disables."""
    return os.environ.get(UF_TERMINAL_HOME_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").strip().strip("/")


def _pf_key(pf: Any) -> str:
    return str(getattr(pf, "id", None) or getattr(pf, "name", "") or "")


def _is_system_uf(uf: Any) -> bool:
    if str(getattr(uf, "category", "") or "") == "system":
        return True
    return str(getattr(uf, "trigger", "") or "") in _SYSTEM_TRIGGERS


def _member_files(members: Iterable[Any]) -> list[str]:
    files: list[str] = []
    for m in members:
        entry = getattr(m, "entry_point_file", None)
        if entry:
            files.append(_norm(str(entry)))
        for p in getattr(m, "paths", None) or []:
            files.append(_norm(str(p)))
    return files


def _dir_vote(
    files: Iterable[str],
    owned_by_dir: Mapping[str, dict[str, int]],
) -> dict[str, int]:
    """Nearest-directory ownership votes.

    For each file, walk ancestor directories from the deepest up to the
    repo root (``""``); the FIRST level where any real PF owns files
    contributes that level's per-PF owned-file counts as votes. At the
    root, votes degrade to "the biggest real PF" — an honest weak signal,
    tagged low-confidence by the caller.
    """
    votes: dict[str, int] = {}
    for f in files:
        parts = f.split("/")
        for depth in range(len(parts) - 1, -1, -1):
            d = "/".join(parts[:depth])
            owners = owned_by_dir.get(d)
            if owners:
                for pf, n in owners.items():
                    votes[pf] = votes.get(pf, 0) + n
                break
    return votes


def assign_terminal_homes(
    user_flows: list["UserFlow"],
    developer_features: list["Feature"],
    product_features: list["Feature"],
) -> dict[str, Any]:
    """Assign every null-``product_feature_id`` journey a real PF, in place.

    Runs AFTER emission integrity — assigns only keys present in the
    surviving ``product_features`` list. Returns telemetry.
    """
    tele: dict[str, Any] = {
        "enabled": terminal_home_enabled(), "orphans": 0,
        "homed_votes": 0, "homed_system": 0, "homed_dir": 0,
        "unhomed": 0, "sample": [],
    }
    if not terminal_home_enabled() or not user_flows:
        return tele

    real_pf_keys = frozenset(
        _pf_key(pf) for pf in product_features
        if _pf_key(pf) and _pf_key(pf).strip().lower() not in _SHARED_PF_KEYS
    )
    if not real_pf_keys:
        tele["unhomed"] = sum(
            1 for uf in user_flows
            if not getattr(uf, "product_feature_id", None)
        )
        return tele  # degenerate: nothing legal to bind to (documented)

    system_pf_keys = frozenset(
        _pf_key(pf) for pf in product_features
        if _pf_key(pf) in real_pf_keys
        and str(getattr(pf, "surface_scope", "") or "") == "system"
    )

    file_pf_owner = build_file_pf_owner(
        dev_views_for(developer_features), real_pf_keys=real_pf_keys,
    )

    # Ancestor-directory ownership index: dir → {pf_key: owned files}.
    owned_by_dir: dict[str, dict[str, int]] = {}
    for f, pf in file_pf_owner.items():
        parts = f.split("/")
        for depth in range(len(parts) - 1, -1, -1):
            d = "/".join(parts[:depth])
            owned_by_dir.setdefault(d, {})
            owned_by_dir[d][pf] = owned_by_dir[d].get(pf, 0) + 1

    flow_by_id: dict[str, Any] = {}
    for dev in developer_features:
        for fl in getattr(dev, "flows", None) or []:
            for key in (getattr(fl, "uuid", None), getattr(fl, "name", None)):
                if key and str(key) not in flow_by_id:
                    flow_by_id[str(key)] = fl

    def _argmax(votes: Mapping[str, int]) -> str | None:
        cands = sorted(
            (k for k in votes if k in real_pf_keys),
            key=lambda k: (-votes[k], k),
        )
        return cands[0] if cands else None

    for uf in user_flows:
        if getattr(uf, "product_feature_id", None):
            continue
        tele["orphans"] += 1
        members = [
            flow_by_id[str(mid)]
            for mid in (getattr(uf, "member_flow_ids", None) or [])
            if str(mid) in flow_by_id
        ]
        chosen: str | None = None
        how = "homed_dir"  # overwritten by whichever rung decides

        # Rung 1 — direct ownership votes (the §4.5 ruler): argmax even
        # below majority (brief item 4b).
        if members:
            span_votes, entry_votes = member_votes(members, file_pf_owner)
            merged = dict(span_votes)
            for k, v in entry_votes.items():
                merged[k] = merged.get(k, 0) + v
            chosen = _argmax(merged)
            if chosen:
                how = "homed_votes"

        files = _member_files(members)

        # Rung 2 — system journeys prefer a system-scope capability.
        if chosen is None and _is_system_uf(uf) and system_pf_keys:
            sys_votes = {
                k: v for k, v in _dir_vote(files, owned_by_dir).items()
                if k in system_pf_keys
            }
            chosen = _argmax(sys_votes)
            if chosen is None:
                # No neighborhood signal — the sole/lexicographically-first
                # system capability is still the honest system home.
                chosen = sorted(system_pf_keys)[0]
            how = "homed_system"

        # Rung 3 — nearest-directory ownership argmax (never null).
        if chosen is None:
            chosen = _argmax(_dir_vote(files, owned_by_dir))
            if chosen:
                how = "homed_dir"

        if chosen is None:
            # No member files at all AND no dir signal — fall back to the
            # largest real PF (root-level dir vote covers every file, so
            # this only triggers for member-less journeys).
            root = owned_by_dir.get("", {})
            chosen = _argmax(root)
            how = "homed_dir"

        if chosen is None:
            tele["unhomed"] += 1  # pragma: no cover — real_pf_keys guard
            continue

        uf.product_feature_id = chosen
        uf.binding_confidence = "low"
        tele[how] += 1
        if len(tele["sample"]) < 20:
            tele["sample"].append({
                "uf": getattr(uf, "name", None), "pf": chosen, "via": how,
            })
    return tele
