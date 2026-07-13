"""Stage 6.7e — Journey Evidence Adjudicator (B57 Seg2).

Operator mandate: every SHOWN journey row on a keyed board is a high
``name_confidence`` EARNED from evidence — or honestly merged / demoted.
Law C (naming_contract) stays the ONLY judge of confidence; this stage
COLLECTS evidence, it never assigns labels or writes ``name_confidence``
itself. Every LLM claim is verified DETERMINISTICALLY before it is
applied — a verdict whose citations fail verification is rejected whole.

Mechanism (one Sonnet batch chain, keyed scans only):

  1. SELECTION — (i) UFs with ``name_confidence != high`` after the
     Seg1 rungs (authored B23-carve rows excluded — the adjudicator does
     not touch them); (ii) dup candidates REGARDLESS of confidence: UFs
     whose member set is identical to / a strict subset of a same-PF
     neighbor's (``product_feature_id=None`` rows are NEVER dup
     candidates — shared member sets without a PF are distinct authored
     intents, the cal.com forensics law).
  2. EVIDENCE PACKAGE per UF (strict JSON, sorted): name, member files
     (+spans), routes, the owning PF's nav-cluster labels, i18n KEYS of
     member files (KEY names only — translated VALUES are a FORBIDDEN
     source, operator rule 2026-07-13), same-PF neighbors with member-set
     relations. NO README (law).
  3. LLM (Sonnet, batch; injectable client — no client ⇒ hard no-op
     BEFORE any mutation): strict JSON verdicts ∈ {rung_evidence,
     rename, merge, demote} with citations [{file, exact_string, rung}].
     Unparseable batch ⇒ whole batch rejected (telemetry).
  4. DETERMINISTIC VERIFIER — every citation is grep-checked (exact
     substring) inside the NAMED file, which must be a member file of
     the UF (or a mapped member test file); ``i18n-key`` citations must
     be identifier-shaped (the Seg1 discriminator — a locale VALUE with
     spaces is a STRUCTURAL reject); a fake citation rejects the verdict
     whole.
  5. APPLY — rung_evidence: verified rungs feed a Law C RE-SCORE
     (``naming_contract.rescore_uf_confidence``; ``adjudicated:*``
     evidence tags; bar unchanged). rename: from cited strings only,
     through the B50 degrime + collision-safe chain; authored rows are
     structurally rename-forbidden. merge: identical / strict-subset
     member sets on the SAME non-None PF only; survivor = superset (equal
     sets → smaller id), member union, flow backpointers repointed,
     ``merge_map`` lineage (dropped authored labels preserved). demote:
     the row leaves ``user_flows[]`` ONLY into a typed ``coverage_gaps[]``
     entry (``kind="adjudicated_noise"``; label / routes / surface spans /
     loc / synthesis_reason ride along) — a silent drop is forbidden; when
     the gap channel is off the demote is SKIPPED (row stays, counted).
  6. CACHE — content-keyed verdict batches in the llm-cache
     (``CacheKind.LLM_ADJUDICATOR``); cost telemetry + a hard stage
     budget (≤ $1.5/repo).

Flag: ``FAULTLINE_STAGE_6_7E_ADJUDICATOR`` (default **OFF**; registered
in ``scan_result_cache.ENV_OUTPUT_FLAGS``, no KEY_SCHEMA bump). Model:
``FAULTLINE_STAGE_6_7E_MODEL`` (default Sonnet). Additive-only: no
existing stage prompt is touched (Stage-8 additive law); this module
owns its own NEW prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Callable, Iterable, Mapping

from faultline.cache.backend import CacheKind
from faultline.llm.cost import CostTracker, deterministic_params
from faultline.llm.model_gateway import resolve_model as gateway_model
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.naming_contract import (
    _CAMEL_BOUNDARY_RE,
    _I18N_IDENT_RE,
    _RUNG_SOURCE_MAX_FILE_BYTES,
    _RUNG_SOURCE_MAX_FILES,
    _i18n_keys_from_text,
    _uf_flow_maps,
    degrime_rename_plan,
    display_law_violations,
    load_naming_vocab,
    nav_label_sets_for_pfs,
    polish_display_casing,
    rescore_uf_confidence,
)

logger = logging.getLogger(__name__)

#: Kill-switch (default **OFF**). Registered in
#: ``scan_result_cache.ENV_OUTPUT_FLAGS`` — no KEY_SCHEMA bump.
ENV_FLAG = "FAULTLINE_STAGE_6_7E_ADJUDICATOR"

#: Adjudicator model (Sonnet by default — the verdicts need the model the
#: mandate was validated on; DELIBERATELY decoupled from the scan's main
#: ``model_id``, the 6.7d precedent). Registered in ENV_OUTPUT_FLAGS.
MODEL_ENV = "FAULTLINE_STAGE_6_7E_MODEL"
DEFAULT_MODEL = "claude-sonnet-4-6"

#: Bumped whenever the prompt / verdict schema / verifier semantics change
#: in a way that would make a cached verdict batch stale.
_CACHE_VERSION = 1

#: Hard per-repo stage budget (the spec cost gate: ≤ $1.5/repo). Batches
#: beyond the budget are skipped and counted — never silently retried.
_MAX_STAGE_COST_USD = 1.5

#: Work bounds (bounds-of-work only, not tuned to any repo).
_BATCH_SIZE = 16
_MAX_SELECTED = 200
_MAX_MEMBER_FILES = 12
_MAX_SPANS_PER_FILE = 6
_MAX_I18N_KEYS = 24
_MAX_NEIGHBORS = 8
_MAX_CITATION_LEN = 200
_MAX_OUTPUT_TOKENS = 8000

#: Citation rungs the verifier accepts. ``nav`` is deliberately absent:
#: nav labels live in nav-component files that are NOT the UF's member
#: files, so a nav citation can never pass law (a) — nav grounding stays
#: the Seg1 deterministic channel.
_CITATION_RUNGS = frozenset({"i18n-key", "route", "member-noun", "test-assert"})

_VERDICTS = frozenset({"rung_evidence", "rename", "merge", "demote"})

#: e2e authored-recall reason (the B23 carve) — mirrors
#: ``synth_quality.E2E_RECALL_REASON`` without importing the module at
#: import time (synth_quality pulls heavier deps).
_E2E_RECALL_REASON = "e2e_journey_recall"


def adjudicator_6_7e_enabled() -> bool:
    """Default **OFF**; ``FAULTLINE_STAGE_6_7E_ADJUDICATOR=1`` arms the
    stage on keyed scans. Unset ⇒ the stage is never called — serialized
    output byte-identical."""
    return os.environ.get(ENV_FLAG, "0").strip().lower() in {"1", "true"}


def resolve_adjudicator_model() -> str:
    """Model for the verdict batches — :data:`MODEL_ENV`, defaulting to
    :data:`DEFAULT_MODEL` (Sonnet). Empty env falls back to the default."""
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


def _default_client_factory() -> Any | None:  # pragma: no cover - IO
    """Lazy Anthropic client — ``None`` when SDK / key absent (keyless),
    so the stage hard-no-ops before any mutation."""
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


# ── System prompt (NEW file-owned prompt; Stage-8 prompt untouched) ─────

_SYSTEM = """You are the journey-evidence adjudicator of a code-intelligence engine.
A deterministic scanner produced user-journey rows; some carry weak name
evidence or duplicate a neighbor. For EACH user flow in the input you may
return AT MOST ONE verdict:

  * "rung_evidence" — you found a string in a MEMBER FILE (or a mapped
    member test file) that grounds the journey's resource. Cite it.
  * "rename" — the current name misrepresents the evidence; propose a
    name derived ONLY from cited identifier strings (code identifiers,
    i18n KEY names, route segments). Never invent words.
  * "merge" — the flow duplicates a listed neighbor (identical or
    strict-subset member set on the SAME product feature). Set "target"
    to the neighbor's uf_id.
  * "demote" — the row is not a real user journey (bookkeeping noise);
    it should become a typed coverage gap.

Rules (violations are rejected by a deterministic verifier):
  * Every citation = {"file", "exact_string", "rung"} where "file" is one
    of the flow's listed member_files/test_files, "exact_string" occurs
    VERBATIM in that file, and "rung" is one of: i18n-key, route,
    member-noun, test-assert.
  * i18n evidence: cite the KEY (identifier, no spaces) — NEVER a
    translated value or any human sentence.
  * rename: also set "target" to the proposed display name; every word
    must come from your cited strings.
  * Omit flows that need no action. No prose, no markdown.

Return STRICT JSON only:
{"verdicts": [{"uf_id": "...", "verdict": "...",
               "citations": [{"file": "...", "exact_string": "...",
                              "rung": "..."}],
               "target": "..."}]}
"""


# ── Small shared helpers ─────────────────────────────────────────────────


def _norm_toks(text: str) -> set[str]:
    """Singular-folded word tokens (camel + namespace split) — the same
    normalization family Law C uses, applied to citation strings and
    rename targets so the token-subset law is glyph-robust."""
    spread = _CAMEL_BOUNDARY_RE.sub(" ", str(text or ""))
    out: set[str] = set()
    for t in re.split(r"[^a-z0-9]+", spread.lower()):
        if t:
            out.add(t[:-1] if (t.endswith("s") and len(t) > 3) else t)
    return out


def _member_file_set(uf: Any, flow_by_id: Mapping[str, Any]) -> set[str]:
    """Member flows' paths + entry files (the Seg1 ``_member_paths`` law)."""
    out: set[str] = set()
    for m in (getattr(uf, "member_flow_ids", None) or []):
        fl = flow_by_id.get(str(m))
        if fl is None:
            continue
        for p in (getattr(fl, "paths", None) or []):
            if p:
                out.add(str(p))
        ep = getattr(fl, "entry_point_file", None)
        if ep:
            out.add(str(ep))
    return out


def _member_test_file_set(uf: Any, flow_by_id: Mapping[str, Any]) -> set[str]:
    """MAPPED member test files (``flow.test_files`` — the B36
    member-overlap mapping; an unmapped test file is never citable)."""
    out: set[str] = set()
    for m in (getattr(uf, "member_flow_ids", None) or []):
        fl = flow_by_id.get(str(m))
        if fl is None:
            continue
        out.update(str(t) for t in (getattr(fl, "test_files", None) or []) if t)
    return out


def _is_authored(uf: Any, authored_ids: set[str]) -> bool:
    """The B23 carve — maintainer-authored rows (e2e recall reason or an
    authored-names entry). The adjudicator never renames / demotes them;
    dup-merge remains their only adjudicated fate (SACRED)."""
    if str(getattr(uf, "id", "") or "") in authored_ids:
        return True
    return str(getattr(uf, "synthesis_reason", "") or "") == _E2E_RECALL_REASON


def _is_marker_row(uf: Any) -> bool:
    """Member-less synthesized markers belong to the B45 gap channel —
    the adjudicator leaves them alone entirely."""
    if not bool(getattr(uf, "synthesized", False)):
        return False
    return not (getattr(uf, "member_flow_ids", None) or [])


def _set_relation(a: set[str], b: set[str]) -> str:
    """Member-set relation of candidate ``a`` vs neighbor ``b``."""
    if not a or not b:
        return "disjoint"
    if a == b:
        return "identical"
    if a < b:
        return "subset"
    if a > b:
        return "superset"
    return "overlap" if (a & b) else "disjoint"


# ── Selection ────────────────────────────────────────────────────────────


def select_candidates(
    user_flows: list[Any],
    authored_ids: set[str],
) -> tuple[list[Any], dict[str, Any]]:
    """(i) non-high rows (authored / marker rows excluded) + (ii) same-PF
    dup candidates regardless of confidence (``PF=None`` groups are NEVER
    dup candidates — counted, not selected). Deterministic order (id)."""
    tele: dict[str, Any] = {"selected_low_conf": 0, "selected_dup": 0,
                            "pfless_dup_groups": 0}
    by_id: dict[str, Any] = {}
    picked: dict[str, Any] = {}

    for uf in user_flows:
        by_id[str(getattr(uf, "id", "") or "")] = uf

    # (i) non-high after Seg1 — the honest med/low residue.
    for uf in user_flows:
        uid = str(getattr(uf, "id", "") or "")
        if _is_marker_row(uf) or _is_authored(uf, authored_ids):
            continue
        if not (getattr(uf, "member_flow_ids", None) or []):
            continue
        if str(getattr(uf, "name_confidence", "") or "") != "high":
            picked[uid] = uf
            tele["selected_low_conf"] += 1

    # (ii) dup candidates — identical / strict-subset member sets on the
    # SAME non-None PF (documenso class: all-high dup groups the non-high
    # selection never sees). PF=None shared sets = distinct authored
    # intents (cal.com forensics) — never candidates.
    sets_by_pf: dict[str, list[tuple[str, set[str]]]] = {}
    pfless_sets: dict[frozenset[str], int] = {}
    for uf in user_flows:
        if _is_marker_row(uf):
            continue
        members = {str(m) for m in (getattr(uf, "member_flow_ids", None) or [])}
        if not members:
            continue
        pfid = getattr(uf, "product_feature_id", None)
        uid = str(getattr(uf, "id", "") or "")
        if not pfid:
            pfless_sets[frozenset(members)] = (
                pfless_sets.get(frozenset(members), 0) + 1)
            continue
        sets_by_pf.setdefault(str(pfid), []).append((uid, members))
    tele["pfless_dup_groups"] = sum(1 for c in pfless_sets.values() if c >= 2)
    for pfid in sorted(sets_by_pf):
        rows = sorted(sets_by_pf[pfid])
        for i, (aid, aset) in enumerate(rows):
            for bid, bset in rows[i + 1:]:
                if aset == bset or aset < bset or bset < aset:
                    for did in (aid, bid):
                        if did not in picked:
                            picked[did] = by_id[did]
                            tele["selected_dup"] += 1

    ordered = [picked[k] for k in sorted(picked)]
    if len(ordered) > _MAX_SELECTED:
        tele["selection_truncated"] = len(ordered) - _MAX_SELECTED
        ordered = ordered[:_MAX_SELECTED]
    return ordered, tele


# ── Evidence packages ────────────────────────────────────────────────────


def _read_capped(repo_root: Any, rel: str, cache: dict[str, str]) -> str:
    if rel in cache:
        return cache[rel]
    text = ""
    if repo_root is not None:
        try:
            from pathlib import Path
            fp = Path(str(repo_root)) / rel
            if fp.is_file() and fp.stat().st_size <= _RUNG_SOURCE_MAX_FILE_BYTES:
                text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
    cache[rel] = text
    return text


def build_evidence_packages(
    candidates: list[Any],
    user_flows: list[Any],
    flow_by_id: Mapping[str, Any],
    nav_label_sets: Mapping[str, list[str]],
    repo_root: Any,
    read_cache: dict[str, str],
) -> list[dict[str, Any]]:
    """Deterministic per-UF evidence package (sorted; caps bound work).
    i18n evidence carries KEY NAMES ONLY (the Seg1 extractor — translated
    VALUES never enter the prompt)."""
    neighbors_by_pf: dict[str, list[Any]] = {}
    for uf in user_flows:
        pfid = getattr(uf, "product_feature_id", None)
        if pfid:
            neighbors_by_pf.setdefault(str(pfid), []).append(uf)

    pkgs: list[dict[str, Any]] = []
    for uf in candidates:
        uid = str(getattr(uf, "id", "") or "")
        members = {str(m) for m in (getattr(uf, "member_flow_ids", None) or [])}
        mfiles = sorted(_member_file_set(uf, flow_by_id))[:_MAX_MEMBER_FILES]
        tfiles = sorted(
            _member_test_file_set(uf, flow_by_id))[:_MAX_MEMBER_FILES]
        spans: dict[str, list[list[int]]] = {}
        for m in sorted(members):
            fl = flow_by_id.get(m)
            if fl is None:
                continue
            for lr in (getattr(fl, "line_ranges", None) or []):
                p = str(getattr(lr, "path", "") or "")
                if p and len(spans.get(p, ())) < _MAX_SPANS_PER_FILE:
                    spans.setdefault(p, []).append([
                        int(getattr(lr, "start_line", 0) or 0),
                        int(getattr(lr, "end_line", 0) or 0),
                    ])
        keys: list[str] = []
        seen_keys: set[str] = set()
        for p in mfiles[:_RUNG_SOURCE_MAX_FILES]:
            for k in _i18n_keys_from_text(_read_capped(repo_root, p, read_cache)):
                if k not in seen_keys:
                    seen_keys.add(k)
                    keys.append(k)
        pfid = str(getattr(uf, "product_feature_id", None) or "")
        neigh: list[dict[str, Any]] = []
        for other in neighbors_by_pf.get(pfid, []):
            oid = str(getattr(other, "id", "") or "")
            if oid == uid:
                continue
            oset = {str(m) for m in (getattr(other, "member_flow_ids", None) or [])}
            neigh.append({
                "uf_id": oid,
                "name": str(getattr(other, "name", "") or ""),
                "member_count": len(oset),
                "relation": _set_relation(members, oset),
            })
        neigh.sort(key=lambda n: str(n["uf_id"]))
        pkgs.append({
            "uf_id": uid,
            "name": str(getattr(uf, "name", "") or ""),
            "name_confidence": str(getattr(uf, "name_confidence", "") or ""),
            "product_feature_id": pfid or None,
            "member_count": len(members),
            "member_files": mfiles,
            "member_spans": {p: spans[p] for p in sorted(spans)},
            "test_files": tfiles,
            "routes": [str(r) for r in (getattr(uf, "routes", None) or []) if r],
            "nav_cluster_labels": list(nav_label_sets.get(pfid, ()))[:8],
            "i18n_keys": keys[:_MAX_I18N_KEYS],
            "neighbors": neigh[:_MAX_NEIGHBORS],
        })
    return pkgs


# ── LLM batch call (persona-batch conventions; content-keyed cache) ─────


def _call_llm(
    client: Any, *, model: str, system: str, user: str,
    llm_health: LlmHealth | None,
) -> tuple[str, int, int]:
    if llm_health is not None and not llm_health.should_call():
        return "", 0, 0
    try:
        msg = client.messages.create(
            model=gateway_model(model), max_tokens=_MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001 — non-fatal at scan time
        if llm_health is not None and llm_health.record_failure(
            exc, stage="stage_6_7e_adjudicator",
        ):
            logger.error("stage_6_7e: LLM auth failed — skipping: %s", exc)
        else:
            logger.warning("stage_6_7e: LLM call failed: %s", exc)
        return "", 0, 0
    if llm_health is not None:
        llm_health.record_success()
    try:
        text = "\n".join(
            t for block in msg.content if (t := getattr(block, "text", None))
        )
    except Exception:  # noqa: BLE001
        text = ""
    in_tok = int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0)
    out_tok = int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)
    return text, in_tok, out_tok


def _parse_json_obj(text: str) -> dict[str, Any] | None:
    """First brace-balanced JSON object (string-safe; personas mirror)."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except (ValueError, TypeError):
                    return None
                return obj if isinstance(obj, dict) else None
    return None


def _batch_verdicts(
    *,
    client: Any,
    model: str,
    pkgs: list[dict[str, Any]],
    cost_tracker: CostTracker,
    cache: Any | None,
    llm_health: LlmHealth | None,
    tele: dict[str, Any],
) -> list[dict[str, Any]]:
    """One cached, tracked verdict batch. Unparseable ⇒ [] + rejected tele."""
    user = json.dumps(
        {"user_flows": pkgs}, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(json.dumps({
        "v": _CACHE_VERSION, "model": model, "system": _SYSTEM, "user": user,
    }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    if cache is not None:
        try:
            cached = cache.get(CacheKind.LLM_ADJUDICATOR.value, key)
        except Exception:  # noqa: BLE001 — cache faults degrade to live
            cached = None
        if (isinstance(cached, dict) and cached.get("v") == _CACHE_VERSION
                and isinstance(cached.get("verdicts"), list)):
            tele["cache_hits"] += 1
            return list(cached["verdicts"])

    text, in_tok, out_tok = _call_llm(
        client, model=model, system=_SYSTEM, user=user, llm_health=llm_health)
    tele["llm_calls"] += 1
    if in_tok or out_tok:
        try:
            cost_tracker.record(
                model=model, input_tokens=in_tok, output_tokens=out_tok,
                label="adjudicator_6_7e",
            )
        except Exception:  # noqa: BLE001 — budget enforced by the stage cap
            pass
    parsed = _parse_json_obj(text)
    verdicts = parsed.get("verdicts") if isinstance(parsed, dict) else None
    if not isinstance(verdicts, list):
        tele["batches_rejected_parse"] += 1
        return []
    out = [v for v in verdicts if isinstance(v, dict)]
    if cache is not None:
        try:
            cache.set(CacheKind.LLM_ADJUDICATOR.value, key, {
                "v": _CACHE_VERSION, "verdicts": out,
            })
        except Exception:  # noqa: BLE001 — cache write faults never abort
            pass
    return out


# ── Deterministic citation verifier (the heart of the cycle) ─────────────


def verify_citations(
    uf: Any,
    citations: list[Any],
    flow_by_id: Mapping[str, Any],
    repo_root: Any,
    read_cache: dict[str, str],
) -> tuple[bool, str]:
    """``(ok, reject_reason)`` — a verdict applies ONLY when EVERY citation
    passes ALL laws:

      (a) the cited file IS a member file of the UF (or a MAPPED member
          test file) — a foreign-file citation is rejected;
      (b) ``exact_string`` occurs VERBATIM in that file (fake ⇒ reject);
      (c) ``rung`` ∈ the citation-rung vocabulary;
      (d) ``rung == "i18n-key"`` ⇒ identifier-shaped (the Seg1
          discriminator) — a locale VALUE (spaces / human copy) is a
          STRUCTURAL reject.
    """
    allowed = _member_file_set(uf, flow_by_id) | _member_test_file_set(
        uf, flow_by_id)
    for c in citations:
        if not isinstance(c, dict):
            return False, "citation-shape"
        f = str(c.get("file") or "")
        s = str(c.get("exact_string") or "")
        rung = str(c.get("rung") or "")
        if not f or not s or len(s) > _MAX_CITATION_LEN:
            return False, "citation-shape"
        if rung not in _CITATION_RUNGS:
            return False, "citation-rung"
        if f not in allowed:
            return False, "citation-foreign-file"
        if rung == "i18n-key" and not _I18N_IDENT_RE.fullmatch(s):
            return False, "citation-i18n-value"
        text = _read_capped(repo_root, f, read_cache)
        if not text or s not in text:
            return False, "citation-not-found"
    return True, ""


# ── Verdict application ──────────────────────────────────────────────────


def _apply_merge(
    uf: Any,
    target: Any,
    user_flows: list[Any],
    flows: list[Any],
    authored_names: Mapping[str, Iterable[str]],
    tele: dict[str, Any],
) -> bool:
    """Merge law: identical / strict-subset member sets on the SAME
    non-None PF (regardless of what the LLM claimed). Survivor =
    superset; equal sets → smaller id (the ``_merge_subset_duplicates``
    semantics). Union members (order-preserving), repoint flow
    backpointers (I14), record lineage in ``merge_map`` (dropped authored
    labels preserved — operator truth is never lost)."""
    a_pf = getattr(uf, "product_feature_id", None)
    b_pf = getattr(target, "product_feature_id", None)
    if not a_pf or not b_pf:
        tele["rejected_pfless_merge"] += 1
        return False
    if str(a_pf) != str(b_pf):
        _reject(tele, "merge-pf-mismatch")
        return False
    a_set = {str(m) for m in (getattr(uf, "member_flow_ids", None) or [])}
    b_set = {str(m) for m in (getattr(target, "member_flow_ids", None) or [])}
    if not a_set or not b_set:
        _reject(tele, "merge-empty-set")
        return False
    if a_set == b_set:
        relation = "identical"
        aid = str(getattr(uf, "id", "") or "")
        bid = str(getattr(target, "id", "") or "")
        dropped, survivor = (uf, target) if bid < aid else (target, uf)
    elif a_set < b_set:
        relation, dropped, survivor = "subset", uf, target
    elif b_set < a_set:
        relation, dropped, survivor = "subset", target, uf
    else:
        _reject(tele, "merge-not-subset")
        return False

    surv_members = list(getattr(survivor, "member_flow_ids", None) or [])
    have = {str(m) for m in surv_members}
    for m in (getattr(dropped, "member_flow_ids", None) or []):
        if str(m) not in have:
            surv_members.append(m)
            have.add(str(m))
    survivor.member_flow_ids = surv_members
    survivor.member_count = len(surv_members)

    dropped_id = str(getattr(dropped, "id", "") or "")
    survivor_id = str(getattr(survivor, "id", "") or "")
    for fl in flows:  # I14 — repoint, never dangle
        if str(getattr(fl, "user_flow_id", None) or "") == dropped_id:
            fl.user_flow_id = survivor_id
    user_flows[:] = [u for u in user_flows
                     if str(getattr(u, "id", "") or "") != dropped_id]

    authored = list(authored_names.get(dropped_id, ()) or ())
    entry: dict[str, Any] = {
        "dropped_id": dropped_id,
        "dropped_name": str(getattr(dropped, "name", "") or ""),
        "into_id": survivor_id,
        "into_name": str(getattr(survivor, "name", "") or ""),
        "pf": str(a_pf),
        "relation": relation,
    }
    if authored:
        entry["dropped_authored_labels"] = [str(a) for a in authored]
    al = getattr(dropped, "authored_label", None)
    if al and str(al) not in entry.get("dropped_authored_labels", []):
        entry.setdefault("dropped_authored_labels", []).append(str(al))
    tele["merge_map"].append(entry)
    return True


def _demote_to_gap(
    uf: Any,
    user_flows: list[Any],
    flows: list[Any],
    product_features: list[Any],
    flow_by_id: Mapping[str, Any],
    gap_mode: str,
    tele: dict[str, Any],
) -> Any | None:
    """Demote law: the row leaves ``user_flows[]`` ONLY into a typed
    ``coverage_gaps[]`` entry (``kind="adjudicated_noise"``); label /
    routes / surface spans / loc / synthesis_reason ride along. Gap
    channel off ⇒ SKIP (row stays; counted) — a silent drop is forbidden.
    The home PF stays covered by the gap itself (the B45 I8
    reformulation: a typed gap is valid PF cover)."""
    from faultline.models.types import CoverageGap, FlowLineRange
    from faultline.pipeline_v2.stage_6_97b_uf_loc import union_span_len

    if gap_mode == "off":
        tele["demote_skipped_no_gap_channel"] += 1
        return None
    pfid = str(getattr(uf, "product_feature_id", None) or "")
    pf_keys = {str(getattr(pf, "name", "") or "") for pf in product_features}
    if not pfid or pfid not in pf_keys:
        _reject(tele, "demote-unowned-pf")
        return None

    spans: list[Any] = []
    loc_by_file: dict[str, list[tuple[int, int]]] = {}

    def _add_span(p: str, s: int, e: int) -> None:
        spans.append(FlowLineRange(path=p, start_line=s, end_line=e))
        loc_by_file.setdefault(p, []).append((s, e))

    for rec in (getattr(uf, "surface_files", None) or []):
        p = getattr(rec, "path", None)
        s = getattr(rec, "start_line", None)
        e = getattr(rec, "end_line", None)
        if p is not None and s is not None and e is not None:
            _add_span(str(p), int(s), int(e))
    if not spans:
        for m in sorted(str(m) for m in
                        (getattr(uf, "member_flow_ids", None) or [])):
            fl = flow_by_id.get(m)
            if fl is None:
                continue
            for lr in (getattr(fl, "line_ranges", None) or []):
                p = str(getattr(lr, "path", "") or "")
                if p:
                    _add_span(p, int(getattr(lr, "start_line", 0) or 0),
                              int(getattr(lr, "end_line", 0) or 0))
    loc = sum(union_span_len(loc_by_file[p]) for p in sorted(loc_by_file))

    label = str(getattr(uf, "name", "") or "")
    gap = CoverageGap(
        id="GAP-" + hashlib.sha1(
            f"{pfid}|adjudicated_noise|{label}".encode("utf-8"),
        ).hexdigest()[:10],
        product_feature_id=pfid,
        kind="adjudicated_noise",
        label=label,
        routes=[str(r) for r in (getattr(uf, "routes", None) or []) if r],
        surface_files=spans,
        loc=loc,
        synthesis_reason=getattr(uf, "synthesis_reason", None),
    )

    uid = str(getattr(uf, "id", "") or "")
    for fl in flows:  # I14 — no dangling backpointer may survive
        if str(getattr(fl, "user_flow_id", None) or "") == uid:
            fl.user_flow_id = None
    if gap_mode == "full":
        user_flows[:] = [u for u in user_flows
                         if str(getattr(u, "id", "") or "") != uid]
    tele["demote_map"].append({
        "id": uid, "name": label, "pf": pfid, "gap_id": gap.id,
        "mode": gap_mode,
    })
    return gap


def _apply_rename(
    uf: Any,
    target_name: str,
    citations: list[Any],
    user_flows: list[Any],
    vocab: Mapping[str, Any],
    pf_display: str,
    tele: dict[str, Any],
) -> bool:
    """Rename law: the new display derives ONLY from cited strings —
    every citation must be whitespace-free (a locale VALUE citation is a
    structural reject) and every target word must come from citation
    tokens. The candidate then rides the FULL B50 chain: degrime →
    casing polish → display laws → collision-safe rename plan."""
    from faultline.pipeline_v2.naming_contract import _degrime_display

    cited: set[str] = set()
    for c in citations:
        s = str(c.get("exact_string") or "") if isinstance(c, dict) else ""
        if not s or re.search(r"\s", s):
            _reject(tele, "rename-value-citation")
            return False
        cited |= _norm_toks(s)
    if not cited:
        _reject(tele, "rename-no-citations")
        return False
    proposal = str(target_name or "").strip()
    if not proposal:
        _reject(tele, "rename-no-target")
        return False
    if not _norm_toks(proposal) <= cited:
        _reject(tele, "rename-uncited-tokens")
        return False

    cand = polish_display_casing(
        _degrime_display(" ".join(proposal.split())), vocab)
    if not cand or display_law_violations(
            cand, vocab, pf_display=pf_display or None):
        _reject(tele, "rename-display-law")
        return False
    uid = str(getattr(uf, "id", "") or "")
    cur_names = {
        str(getattr(u, "id", "") or ""): str(getattr(u, "name", "") or "")
        for u in user_flows
    }
    if uid not in degrime_rename_plan(cur_names, {uid: cand}):
        _reject(tele, "rename-collision")
        return False
    tele["renames"].append({
        "id": uid, "before": str(getattr(uf, "name", "") or ""),
        "after": cand,
    })
    uf.name = cand
    return True


def _reject(tele: dict[str, Any], reason: str) -> None:
    tele["rejected"] += 1
    tele["rejected_reasons"][reason] = (
        tele["rejected_reasons"].get(reason, 0) + 1)


# ── Stage entry ──────────────────────────────────────────────────────────


def run_stage_6_7e(
    user_flows: list[Any],
    flows: list[Any],
    product_features: list[Any],
    *,
    repo_root: Any,
    product_strings: Any = None,
    routes_index: Iterable[Mapping[str, Any]] | None = None,
    uf_authored_names: Mapping[str, Iterable[str]] | None = None,
    keeper_on: bool = True,
    model_id: str | None = None,
    cost_tracker: CostTracker | None = None,
    cache: Any | None = None,
    llm_health: LlmHealth | None = None,
    client: Any | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> tuple[dict[str, Any], list[Any]]:
    """Run the adjudicator over the post-Law-C board. Returns
    ``(telemetry, adjudicated_gaps)``; mutates ``user_flows`` /
    ``flows`` backpointers in place ONLY for verified verdicts.

    Keyless (no client) ⇒ ``{"ran": False, ...}`` and ZERO mutations —
    the byte-identity law. ``model_id`` is accepted for signature parity
    but the verdict model comes from :func:`resolve_adjudicator_model`
    (Sonnet default), the 6.7d decoupling precedent."""
    from faultline.pipeline_v2.synth_quality import (
        coverage_gap_channel_mode,
        synth_quality_enabled,
    )

    tele: dict[str, Any] = {
        "ran": False,
        "model": resolve_adjudicator_model(),
        "selected": 0,
        "batches": 0,
        "llm_calls": 0,
        "cache_hits": 0,
        "batches_rejected_parse": 0,
        "batches_skipped_budget": 0,
        "cost_usd": 0.0,
        "verdicts": {"rung_evidence": 0, "rename": 0, "merge": 0, "demote": 0},
        "rejected": 0,
        "rejected_reasons": {},
        "rejected_pfless_merge": 0,
        "demote_skipped_no_gap_channel": 0,
        "merge_map": [],
        "demote_map": [],
        "renames": [],
    }
    gaps: list[Any] = []

    live = client if client is not None else _client_factory()
    if live is None:
        # HARD no-op before any mutation — the keyless byte-identity law.
        tele["skipped"] = "no-client"
        return tele, gaps
    tele["ran"] = True

    _, _, flow_by_id = _uf_flow_maps(flows)
    authored_map: Mapping[str, Iterable[str]] = uf_authored_names or {}
    authored_ids = {str(k) for k in authored_map.keys()}

    candidates, sel_tele = select_candidates(user_flows, authored_ids)
    tele.update(sel_tele)
    tele["selected"] = len(candidates)
    if not candidates:
        return tele, gaps
    # Defense-in-depth: the model may only speak about rows it was ASKED
    # about — a verdict for any other uid is rejected by the verifier
    # below ("verdict-unselected"), no matter how valid its citations.
    selected_uids = {str(getattr(u, "id", "") or "") for u in candidates}

    routes_list = list(routes_index) if routes_index is not None else None
    nav_sets = nav_label_sets_for_pfs(
        product_features, product_strings, routes_list)
    read_cache: dict[str, str] = {}
    pkgs = build_evidence_packages(
        candidates, user_flows, flow_by_id, nav_sets, repo_root, read_cache)

    tracker = cost_tracker if cost_tracker is not None else CostTracker()
    model = resolve_adjudicator_model()

    def _stage_cost() -> float:
        return sum(
            float(getattr(r, "cost_usd", 0.0) or 0.0)
            for r in getattr(tracker, "records", [])
            if getattr(r, "label", "") == "adjudicator_6_7e"
        )

    verdicts: list[dict[str, Any]] = []
    for i in range(0, len(pkgs), _BATCH_SIZE):
        if _stage_cost() >= _MAX_STAGE_COST_USD:
            tele["batches_skipped_budget"] += 1
            continue
        tele["batches"] += 1
        verdicts.extend(_batch_verdicts(
            client=live, model=model, pkgs=pkgs[i:i + _BATCH_SIZE],
            cost_tracker=tracker, cache=cache, llm_health=llm_health,
            tele=tele,
        ))
    tele["cost_usd"] = round(_stage_cost(), 6)

    # ── Verify + apply (merge → demote → rename → rung_evidence) ─────
    by_id = {str(getattr(u, "id", "") or ""): u for u in user_flows}
    vocab = load_naming_vocab()
    pf_display = {
        str(getattr(pf, "name", "") or ""): str(
            getattr(pf, "display_name", None) or getattr(pf, "name", "") or "")
        for pf in product_features
    }
    gap_mode = (coverage_gap_channel_mode()
                if synth_quality_enabled() else "off")
    adjudicated_sources: dict[str, list[str]] = {}
    seen_uids: set[str] = set()
    ordered = sorted(
        (v for v in verdicts if isinstance(v, dict)),
        key=lambda v: (str(v.get("uf_id") or ""), str(v.get("verdict") or "")),
    )
    # Merges first (they consume rows), then demotes, renames, evidence.
    _phase = {"merge": 0, "demote": 1, "rename": 2, "rung_evidence": 3}
    ordered.sort(key=lambda v: _phase.get(str(v.get("verdict") or ""), 9))

    def _live_ids() -> set[str]:
        return {str(getattr(u, "id", "") or "") for u in user_flows}

    for v in ordered:
        uid = str(v.get("uf_id") or "")
        verdict = str(v.get("verdict") or "")
        _raw_citations = v.get("citations")
        citations: list[Any] = (
            list(_raw_citations) if isinstance(_raw_citations, list) else [])
        if verdict not in _VERDICTS:
            _reject(tele, "verdict-unknown")
            continue
        if uid in seen_uids:
            _reject(tele, "verdict-duplicate")
            continue
        uf = by_id.get(uid)
        if uf is None or uid not in _live_ids():
            _reject(tele, "verdict-unknown-uf")
            continue
        if uid not in selected_uids:
            # On-board row the LLM was never asked about (selection is
            # non-high + same-PF dup candidates) — never touched.
            _reject(tele, "verdict-unselected")
            continue
        ok, reason = verify_citations(
            uf, citations, flow_by_id, repo_root, read_cache)
        if not ok:
            _reject(tele, reason)
            continue

        if verdict == "merge":
            tid = str(v.get("target") or "")
            target = by_id.get(tid)
            if target is None or target is uf or tid not in _live_ids():
                _reject(tele, "merge-unknown-target")
                continue
            if _apply_merge(uf, target, user_flows, flows, authored_map, tele):
                tele["verdicts"]["merge"] += 1
                seen_uids.add(uid)
        elif verdict == "demote":
            if _is_authored(uf, authored_ids):
                _reject(tele, "demote-authored")
                continue
            gap = _demote_to_gap(
                uf, user_flows, flows, product_features, flow_by_id,
                gap_mode, tele)
            if gap is not None:
                gaps.append(gap)
                tele["verdicts"]["demote"] += 1
                seen_uids.add(uid)
        elif verdict == "rename":
            if _is_authored(uf, authored_ids):
                _reject(tele, "rename-authored")
                continue
            pfd = pf_display.get(
                str(getattr(uf, "product_feature_id", None) or ""), "")
            if _apply_rename(uf, str(v.get("target") or ""), citations,
                             user_flows, vocab, pfd, tele):
                tele["verdicts"]["rename"] += 1
                seen_uids.add(uid)
        else:  # rung_evidence
            if not citations:
                _reject(tele, "evidence-no-citations")
                continue
            rungs = sorted({
                str(c.get("rung") or "") for c in citations
                if isinstance(c, dict)
            })
            adjudicated_sources.setdefault(uid, [])
            for r in rungs:
                if r not in adjudicated_sources[uid]:
                    adjudicated_sources[uid].append(r)
            tele["verdicts"]["rung_evidence"] += 1
            seen_uids.add(uid)

    # ── Law C re-score — confidence is only ever written by Law C ────
    applied = (adjudicated_sources or tele["verdicts"]["merge"]
               or tele["verdicts"]["demote"] or tele["verdicts"]["rename"])
    if applied:
        rescore_tele = rescore_uf_confidence(
            product_features, user_flows, flows,
            product_strings=product_strings,
            routes_index=routes_list,
            uf_authored_names=uf_authored_names,
            keeper_on=keeper_on,
            repo_root=repo_root,
            adjudicated_sources=adjudicated_sources or None,
        )
        tele["law_c_rescore"] = {
            k: rescore_tele[k]
            for k in ("confidence_before", "confidence_after", "skipped")
            if k in rescore_tele
        }
    gaps.sort(key=lambda g: (
        str(getattr(g, "product_feature_id", "") or ""),
        str(getattr(g, "id", "") or "")))
    return tele, gaps
