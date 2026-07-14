"""B68 — terminal 4-way classification of the coverage-gap band (Stage 6.995).

Operator doctrine (2026-07-14, verbatim essence): «uncovered surface /
adjudicated noise — таких речей бути НЕ ПОВИННО: або це не фіча, або це
частина іншої фічі, або власна продукт-фіча, або dev-інфраструктура».
The gap channel (B45 ``coverage_gaps[]``) is an INTERNAL pipeline state,
never a final board category. At emission — after every other stage has
spoken — each surviving gap row is decomposed BY MEMBERS (per surface
file, never the row as a whole: the documenso frankenstein exhibit mixed
an e2e label, prisma dev-infra files and a B27 PF fragment in ONE row)
into the four terminal fates:

  (1) NOT-A-FEATURE — e2e/test-authored labels (the B23-carve family:
      ``synthesis_reason == e2e_journey_recall`` / ``kind == e2e_orphan``
      / a carried ``authored_label``): the label is a maintainer's TEST
      artifact, not a product journey. Full audit trace in
      ``scan_meta.terminal_classification``; row off the board. The
      row's files are still classified for the trace fractions.
  (2) PART OF AN EXISTING FEATURE — a fragment whose surface
      echo-matches exactly ONE existing product PF
      (:class:`~faultline.pipeline_v2.transport_handoff.NamespaceEcho`
      identity index — the fifth reuse after B49 r2.6 / B51 / B53 /
      B58-v2; full normalized match, ambiguous → abstain, generic →
      abstain) or whose file carries LIVE flows owned by exactly one PF.
      The fragment's gap claim dissolves into that PF; the trace records
      the target. Self-matches (target == the gap's own home PF) dissolve
      only for ``adjudicated_noise`` rows — a demoted journey's surface
      genuinely IS its PF's product surface; for the B45 marker kinds a
      self-match cannot answer the "no attachable flow" claim, so the
      file stays residue for rung (5).
  (3) OWN PRODUCT FEATURE — evaluated under the STANDING worthiness laws
      only (B23/B33: member-less mints are forbidden — a mint requires
      real members + routes). A gap fragment's live-flow members are
      always already owned by an existing PF (path_index is total), so
      the positive branch is structurally empty at emission: this rung
      RECORDS the evaluation (``mint_evaluated`` / ``mint_candidates``)
      and never mints. A non-empty ``mint_candidates`` is a B68-v2
      question for the operator, not an improvised mint.
  (4) DEV-INFRASTRUCTURE — the EXISTING predicates, reused verbatim:
      the B58/B53-SegB ``dev_artifact_units`` channel + 6.86 instrument
      dirs, Stage 6.9 ``is_test_path``, 6.9b ``is_generated_path``, the
      B59 artifact-ink classes, lane-resident dev paths (B21
      lane-neutral doctrine) and B15 shared-leaf roles.
  (5) KNOWN LEXER HOLE — the ONLY legal residue on the board: the row
      stays in ``coverage_gaps[]`` carrying ``why_unresolved`` naming a
      B63-unseen class (``data/terminal-classification.yaml``). As the
      B65/B66/B67 family closes holes, this category mechanically → 0.
      A residue with NO known-hole signature gets the honest
      ``unmapped`` fallback — visible to the census gate as FAIL, never
      hidden.

SACRED — nothing is hidden and nothing moves: every removed row and
every per-file fate is recorded in ``scan_meta.terminal_classification``
(the full audit channel); file OWNERSHIP is never touched (path_index /
product_features / developer_features / user_flows are read-only here —
this stage judges gap CLAIMS, not membership), so member conservation
holds trivially. A row that survives is trimmed to its residue spans
(``loc`` recomputed via the 6.97b union) — the classified fractions'
original spans ride in the trace.

Kill-switch ``FAULTLINE_TERMINAL_CLASSIFICATION`` (default OFF) → the
stage never runs: ``coverage_gaps[]`` and ``scan_meta`` stay
byte-identical to main (the ``why_unresolved`` model field is omitted
from dumps when ``None``).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from faultline.pipeline_v2.data import load_yaml

TERMINAL_CLASSIFICATION_ENV = "FAULTLINE_TERMINAL_CLASSIFICATION"

#: Honest fallbacks — deliberately NOT in the known-hole YAML: the census
#: gate counts them as failures (a residue we cannot name is a finding,
#: never a hidden row).
WHY_UNMAPPED = "unmapped: no known lexer-hole signature"
WHY_NO_EVIDENCE = "unmapped: evidence-less gap row (no surface spans)"

_DATA_FILE = "terminal-classification.yaml"
_HEAD_BYTES = 65536

#: Per-file fate tags used in the trace rows.
FATE_DEV_INFRA = "dev_infrastructure"
FATE_REHOME = "part_of_existing_feature"
FATE_RESIDUE = "residue"


def terminal_classification_enabled() -> bool:
    """``True`` when ``FAULTLINE_TERMINAL_CLASSIFICATION`` is set truthy
    (default OFF). Unset/``0`` keeps the stage inert — every scan is
    byte-identical to pre-B68 (no rows touched, no scan_meta key, the
    ``why_unresolved`` field never stamped)."""
    return os.environ.get(
        TERMINAL_CLASSIFICATION_ENV, "0"
    ).strip().lower() not in {"", "0", "false", "no", "off"}


# ── known-hole taxonomy (category 5) ─────────────────────────────────────


@lru_cache(maxsize=1)
def _known_holes() -> tuple[dict[str, Any], ...]:
    """Load + normalize the packaged taxonomy. Order is law (first hit
    wins, extension-only classes first — see the YAML doctrine note)."""
    data = load_yaml(_DATA_FILE)
    out: list[dict[str, Any]] = []
    for row in data.get("known_lexer_holes") or []:
        if not isinstance(row, dict):
            continue
        hid = str(row.get("id") or "").strip()
        why = str(row.get("why") or "").strip()
        if not hid or not why:
            continue
        out.append({
            "id": hid,
            "why": why,
            "extensions": tuple(
                str(e).strip().lower()
                for e in (row.get("extensions") or []) if str(e).strip()
            ),
            "markers": tuple(
                str(m) for m in (row.get("markers") or []) if str(m)
            ),
        })
    return tuple(out)


def known_hole_whys() -> frozenset[str]:
    """The legal ``why_unresolved`` values (census/test surface)."""
    return frozenset(h["why"] for h in _known_holes())


def _read_head(repo_path: Path | None, rel: str) -> str:
    """First ``_HEAD_BYTES`` of *rel* under *repo_path* (empty string on
    any miss — a missing/binary file simply matches no marker)."""
    if repo_path is None:
        return ""
    try:
        with open(Path(repo_path) / rel, "rb") as fh:
            return fh.read(_HEAD_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return ""


def classify_known_hole(rel_path: str, repo_path: Path | None) -> str | None:
    """Map a residue file to its known-lexer-hole ``why`` (or ``None``).

    Deterministic ladder over the packaged taxonomy: extension classes
    match on the lowercased path suffix (compound extensions like
    ``.blade.php`` supported); marker classes additionally require ANY
    plain-substring hit on the file head. First hit wins."""
    low = rel_path.replace("\\", "/").lower()
    head: str | None = None  # lazily read once, shared across classes
    for hole in _known_holes():
        exts = hole["extensions"]
        if exts and not any(low.endswith(e) for e in exts):
            continue
        markers = hole["markers"]
        if not markers:
            if exts:  # extension-only class
                return str(hole["why"])
            continue
        if head is None:
            head = _read_head(repo_path, rel_path)
        if head and any(m in head for m in markers):
            return str(hole["why"])
    return None


# ── small dict/object-agnostic helpers (house pattern) ───────────────────


def _get(o: Any, name: str, default: Any = None) -> Any:
    if isinstance(o, Mapping):
        return o.get(name, default)
    return getattr(o, name, default)


def _set(o: Any, name: str, value: Any) -> None:
    if isinstance(o, Mapping):
        o[name] = value  # type: ignore[index]
    else:
        setattr(o, name, value)


# ── the classifier ───────────────────────────────────────────────────────


def _pf_key(pf: Any) -> str:
    return str(_get(pf, "id", None) or _get(pf, "name", "") or "")


def _dev_infra_indexes(
    developer_features: list[Any] | None,
) -> tuple[frozenset[str], dict[str, set[str]], dict[str, set[str]]]:
    """(lane-resident paths, member-file roles by path, live-flow PF
    owners by path) — one pass over the dev ledger."""
    lane_paths: set[str] = set()
    roles: dict[str, set[str]] = {}
    flow_owners: dict[str, set[str]] = {}
    for dev in developer_features or []:
        if _get(dev, "layer", "developer") != "developer":
            continue
        pfid = _get(dev, "product_feature_id", None)
        paths = [str(p) for p in (_get(dev, "paths", None) or []) if p]
        if not pfid:
            lane_paths.update(paths)  # B21 lane-neutral mass
        for mf in _get(dev, "member_files", None) or []:
            p = str(_get(mf, "path", "") or "")
            r = str(_get(mf, "role", "") or "")
            if p and r:
                roles.setdefault(p, set()).add(r)
        if pfid:
            for fl in _get(dev, "flows", None) or []:
                for lr in _get(fl, "line_ranges", None) or []:
                    p = str(_get(lr, "path", "") or "")
                    if p:
                        flow_owners.setdefault(p, set()).add(str(pfid))
    return frozenset(lane_paths), roles, flow_owners


def _under_any_unit(path: str, units: frozenset[str]) -> bool:
    return any(path == u or path.startswith(u.rstrip("/") + "/")
               for u in units if u)


def _dev_infra_evidence(
    path: str,
    dev_artifact_units: frozenset[str],
    instrument_dirs: frozenset[str],
    lane_paths: frozenset[str],
    roles_by_file: dict[str, set[str]],
) -> str | None:
    """Rung (4) — the existing dev-infrastructure predicates, in a fixed
    deterministic order. Returns the predicate name (trace evidence) or
    ``None``. Imports are local so the OFF path never pays them."""
    from faultline.pipeline_v2.artifact_ink import classify_artifact
    from faultline.pipeline_v2.stage_6_9_test_strip import is_test_path
    from faultline.pipeline_v2.stage_6_9b_generated_strip import (
        is_generated_path,
    )

    if _under_any_unit(path, dev_artifact_units):
        return "dev_artifact_unit"          # B58 / B53 Seg B / B28 P-D
    if _under_any_unit(path, instrument_dirs):
        return "technology_instrument"      # W4.2 Fix 1 channel
    if is_test_path(path):
        return "test_path"                  # Stage 6.9
    if is_generated_path(path):
        return "generated_path"             # Stage 6.9b
    ink = classify_artifact(path)
    if ink:
        return f"artifact_ink:{ink}"        # B59 classes
    if path in lane_paths:
        return "lane_resident"              # B21 lane-neutral doctrine
    if roles_by_file.get(path) == {"shared"}:
        return "shared_leaf"                # B15 family
    return None


def _echo_tokens(path: str) -> list[str]:
    """Candidate identity tokens for a surface file, per the B53/B58-v2
    container law: the segment immediately AFTER a known container
    segment (``features/<domain>/…``), plus the file stem. Container and
    generic tokens themselves never match (the NamespaceEcho abstain
    discipline)."""
    from faultline.pipeline_v2.spine_anchors import normalize_anchor_key
    from faultline.pipeline_v2.ws_blob_domain_drain import (
        _cap_containers,
        _containers,
    )

    containers = _cap_containers()
    generic = _containers()[1]
    segs = [s for s in path.replace("\\", "/").split("/") if s]
    if not segs:
        return []
    stem = segs[-1]
    dot = stem.rfind(".")
    if dot > 0:
        stem = stem[:dot]
    cands: list[str] = []
    for i, seg in enumerate(segs[:-1]):
        if seg.lower() in containers and i + 1 < len(segs) - 1:
            cands.append(segs[i + 1])
    cands.append(stem)
    out: list[str] = []
    seen: set[str] = set()
    for c in cands:
        k = normalize_anchor_key(c)
        if k and k not in generic and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _echo_target(
    path: str, pf_by_key: Mapping[str, frozenset[str]]
) -> str | None:
    """NamespaceEcho match laws over the identity index: full normalized
    match, exactly ONE distinct PF across the file's tokens, else
    abstain (0 → generic/no-surface; >1 → ambiguous)."""
    hits: set[str] = set()
    for tok in _echo_tokens(path):
        pfs = pf_by_key.get(tok)
        if pfs:
            hits |= set(pfs)
    if len(hits) != 1:
        return None
    return next(iter(hits))


def run_terminal_classification(
    coverage_gaps: list[Any],
    product_features: list[Any],
    developer_features: list[Any] | None,
    scan_meta: dict[str, Any],
    *,
    dev_artifact_units: frozenset[str] = frozenset(),
    instrument_dirs: frozenset[str] = frozenset(),
    repo_path: Path | None = None,
) -> dict[str, Any]:
    """Terminally classify every ``coverage_gaps[]`` row (mutates the list
    in place + stamps ``why_unresolved`` on survivors) and write the full
    audit trace to ``scan_meta["terminal_classification"]``.

    Caller guards on :func:`terminal_classification_enabled`; this stays
    a safe no-op if called with the flag off."""
    if not terminal_classification_enabled():
        return {"enabled": False}

    from faultline.pipeline_v2.stage_6_97b_uf_loc import union_span_len
    from faultline.pipeline_v2.synth_quality import E2E_RECALL_REASON
    from faultline.pipeline_v2.transport_handoff import NamespaceEcho

    echo = NamespaceEcho.build(product_features, frozenset())
    lane_paths, roles_by_file, flow_owners = _dev_infra_indexes(
        developer_features)

    tele: dict[str, Any] = {
        "enabled": True,
        "rows_in": len(coverage_gaps),
        "rows_removed": 0,
        "rows_kept_unresolved": 0,
        "counts": {
            "non_feature_rows": 0,
            "dev_infrastructure_files": 0,
            "rehomed_files": 0,
            "residue_files": 0,
        },
        "by_predicate": {},
        "by_why": {},
        "mint_evaluated": 0,
        "mint_candidates": [],
        "rows": [],
    }
    by_pred: dict[str, int] = {}
    by_why: dict[str, int] = {}

    kept: list[Any] = []
    for gap in coverage_gaps:
        gid = str(_get(gap, "id", "") or "")
        kind = str(_get(gap, "kind", "") or "")
        home_pf = str(_get(gap, "product_feature_id", None) or "")
        label = str(_get(gap, "label", "") or "")
        spans = list(_get(gap, "surface_files", None) or [])
        paths = sorted({
            str(_get(s, "path", "") or "") for s in spans
            if _get(s, "path", None)
        })

        row: dict[str, Any] = {
            "id": gid, "kind": kind, "pf": home_pf, "label": label,
            "original_loc": int(_get(gap, "loc", 0) or 0),
            "files": [],
        }

        # ── row-level rung (1): e2e/test-authored label (B23-carve) ──
        e2e_origin = (
            kind == "e2e_orphan"
            or str(_get(gap, "synthesis_reason", "") or "")
            == E2E_RECALL_REASON
            or bool(_get(gap, "authored_label", None))
        )

        # ── per-file 4-way ladder (franksteins decompose by files) ──
        residue: list[str] = []
        for p in paths:
            ev = _dev_infra_evidence(
                p, dev_artifact_units, instrument_dirs,
                lane_paths, roles_by_file)
            if ev:
                row["files"].append(
                    {"path": p, "fate": FATE_DEV_INFRA, "evidence": ev})
                tele["counts"]["dev_infrastructure_files"] += 1
                by_pred[ev] = by_pred.get(ev, 0) + 1
                continue
            target = _echo_target(p, echo.pf_by_key)
            ev_name = "namespace_echo"
            if target is None:
                owners = flow_owners.get(p) or set()
                if len(owners) == 1:
                    target = next(iter(owners))
                    ev_name = "live_flow_owner"
            if target is not None and (
                kind == "adjudicated_noise" or target != home_pf
            ):
                row["files"].append({
                    "path": p, "fate": FATE_REHOME,
                    "evidence": ev_name, "target_pf": target,
                })
                tele["counts"]["rehomed_files"] += 1
                by_pred[ev_name] = by_pred.get(ev_name, 0) + 1
                continue
            row["files"].append({"path": p, "fate": FATE_RESIDUE})
            residue.append(p)
        tele["counts"]["residue_files"] += len(residue)

        # ── rung (3): own-PF worthiness — evaluate, NEVER mint without
        # real members + routes (B23/B33). Live-flow members are always
        # already owned (path_index total), so a positive here is a
        # recorded candidate for an operator decision, not a mint.
        routes = [str(r) for r in (_get(gap, "routes", None) or []) if r]
        if residue and routes:
            tele["mint_evaluated"] += 1
            if any(flow_owners.get(p) for p in residue):
                tele["mint_candidates"].append({
                    "id": gid, "pf": home_pf, "routes": routes,
                    "files": residue,
                })

        if e2e_origin:
            # Fate (1): the LABEL is a test artifact — row off the
            # board, full trace (files classified above ride along).
            row["verdict"] = "non_feature"
            row["synthesis_reason"] = _get(gap, "synthesis_reason", None)
            row["authored_label"] = _get(gap, "authored_label", None)
            tele["counts"]["non_feature_rows"] += 1
            tele["rows_removed"] += 1
        elif not paths:
            # Evidence-less row (no spans to classify): stays, honestly
            # stamped — the census gate sees it.
            row["verdict"] = "unresolved"
            row["why_unresolved"] = WHY_NO_EVIDENCE
            by_why[WHY_NO_EVIDENCE] = by_why.get(WHY_NO_EVIDENCE, 0) + 1
            _set(gap, "why_unresolved", WHY_NO_EVIDENCE)
            tele["rows_kept_unresolved"] += 1
            kept.append(gap)
        elif not residue:
            # Every file classified (2)/(4) — the row dissolves.
            row["verdict"] = "dissolved"
            tele["rows_removed"] += 1
        else:
            # Fate (5): residue names a known lexer hole (first matched
            # class across the sorted residue), else the honest fallback.
            why = None
            why_by_file: dict[str, str] = {}
            for p in residue:
                w = classify_known_hole(p, repo_path)
                if w:
                    why_by_file[p] = w
                    if why is None:
                        why = w
            why = why or WHY_UNMAPPED
            row["verdict"] = "unresolved"
            row["why_unresolved"] = why
            if why_by_file:
                row["why_by_file"] = why_by_file
            by_why[why] = by_why.get(why, 0) + 1
            _set(gap, "why_unresolved", why)
            # Trim to the residue spans — the classified fractions left
            # the claim (their originals are in this trace row).
            res = set(residue)
            res_spans = [
                s for s in spans if str(_get(s, "path", "") or "") in res]
            loc_by_file: dict[str, list[tuple[int, int]]] = {}
            for s in res_spans:
                p = str(_get(s, "path", "") or "")
                st = _get(s, "start_line", None)
                en = _get(s, "end_line", None)
                if p and st is not None and en is not None:
                    loc_by_file.setdefault(p, []).append((int(st), int(en)))
            _set(gap, "surface_files", res_spans)
            _set(gap, "loc", sum(
                union_span_len(loc_by_file[p]) for p in sorted(loc_by_file)))
            row["residual_loc"] = int(_get(gap, "loc", 0) or 0)
            tele["rows_kept_unresolved"] += 1
            kept.append(gap)

        tele["rows"].append(row)

    coverage_gaps[:] = kept
    tele["by_predicate"] = dict(sorted(by_pred.items()))
    tele["by_why"] = dict(sorted(by_why.items()))
    scan_meta["terminal_classification"] = tele
    return tele


__all__ = [
    "TERMINAL_CLASSIFICATION_ENV",
    "WHY_UNMAPPED",
    "WHY_NO_EVIDENCE",
    "FATE_DEV_INFRA",
    "FATE_REHOME",
    "FATE_RESIDUE",
    "terminal_classification_enabled",
    "known_hole_whys",
    "classify_known_hole",
    "run_terminal_classification",
]
