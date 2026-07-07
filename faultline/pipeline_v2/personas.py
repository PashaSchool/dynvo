"""Wave-3 LLM personas (Product-Spine §4.7) — narrow verify/label roles.

Three roles, none of which can move membership (invariant zero): every
persona output is a LABEL on an existing spine node, a SCOPE verdict
from the deterministic signal set, or an ACCEPT/REJECT on a draft — and
every output is validated deterministically before it is applied, with
the deterministic projection as the fallback (rejects never block the
pipeline).

* **PM Labeler** — picks each pending PF/UF display name from the
  naming-contract candidate set (§4.8); may COMPOSE only within the
  documented journey grammar, and a composed label must pass the
  token-evidence check (:mod:`naming_validator` style) — else the
  deterministic top choice stands.
* **Surface Adjudicator** — resolves ``surface_scope`` ONLY for
  features the deterministic classifier marked ambiguous (conflicting
  signals); the verdict must be a scope with nonzero deterministic
  signal on that item (or ``product``), else the deterministic verdict
  stands.
* **Draft Verifier** — accept/reject for (a) labeler-COMPOSED display
  names and (b) backstop-synthesized user flows; a reject triggers ONE
  retry (``FAULTLINE_PERSONA_ESCALATION_MODEL`` — Opus when set — fires
  only on that reject→retry), then the deterministic fallback.

Cost/observability contract (chain4 rider): every persona call goes
through the shared :class:`~faultline.llm.cost.CostTracker` (label
``persona_<role>``) and the Phase-0 decision log (``role=<role>`` with
the full candidate set recorded — the fine-tuning dataset), and is
content-keyed in the llm-cache (``CacheKind.LLM_PERSONA``) so unchanged
repos replay at $0.

Env (all registered in ``scan_result_cache.ENV_OUTPUT_FLAGS``):

* ``FAULTLINE_PERSONA_LABELER`` — default ON; ``0`` disables.
* ``FAULTLINE_PERSONA_LABELER_MODEL`` — labeler model override
  (default: the scan's model id — Haiku on a default scan).
* ``FAULTLINE_PERSONA_ADJUDICATOR`` — default ON; ``0`` disables.
* ``FAULTLINE_PERSONA_VERIFIER`` — default ON; ``0`` disables.
* ``FAULTLINE_PERSONA_ESCALATION_MODEL`` — default UNSET; when set,
  the verifier's post-reject retry runs on this model.

Keyless scans construct no client → every builder returns ``None`` and
the callers take their deterministic paths (snapshot-gate property).
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
    display_law_violations,
    load_naming_vocab,
)
from faultline.pipeline_v2.naming_validator import tokenize_name

logger = logging.getLogger(__name__)

__all__ = [
    "LABELER_ENV",
    "LABELER_MODEL_ENV",
    "ADJUDICATOR_ENV",
    "VERIFIER_ENV",
    "ESCALATION_MODEL_ENV",
    "PERSONA_CACHE_VERSION",
    "labeler_enabled",
    "adjudicator_enabled",
    "verifier_enabled",
    "escalation_model",
    "build_pm_labeler",
    "build_surface_adjudicator",
    "build_draft_verifier",
]

LABELER_ENV = "FAULTLINE_PERSONA_LABELER"
LABELER_MODEL_ENV = "FAULTLINE_PERSONA_LABELER_MODEL"
ADJUDICATOR_ENV = "FAULTLINE_PERSONA_ADJUDICATOR"
VERIFIER_ENV = "FAULTLINE_PERSONA_VERIFIER"
ESCALATION_MODEL_ENV = "FAULTLINE_PERSONA_ESCALATION_MODEL"

#: Bump when a persona's prompt/parse semantics change — old cache
#: entries silently stop matching.
PERSONA_CACHE_VERSION = 1

#: Batch bounds (structural — they bound work, not behavior; overflow
#: items keep their deterministic result and are counted in telemetry).
_MAX_ITEMS_PER_BATCH = 150
_MAX_OUTPUT_TOKENS = 8000


def _flag_on(env: str) -> bool:
    return os.environ.get(env, "1").strip().lower() not in {"0", "false"}


def labeler_enabled() -> bool:
    return _flag_on(LABELER_ENV)


def adjudicator_enabled() -> bool:
    return _flag_on(ADJUDICATOR_ENV)


def verifier_enabled() -> bool:
    return _flag_on(VERIFIER_ENV)


def escalation_model() -> str | None:
    """Post-reject retry model (default unset — retry reuses the role
    model; Opus when the operator sets it)."""
    v = os.environ.get(ESCALATION_MODEL_ENV, "").strip()
    return v or None


def _default_client_factory() -> Any | None:  # pragma: no cover - IO
    """Lazy Anthropic client — ``None`` when SDK / key absent (keyless),
    so every persona degrades to the deterministic path."""
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


# ── Shared batch-call plumbing ──────────────────────────────────────────


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
            exc, stage="personas",
        ):
            logger.error("personas: LLM auth failed — skipping: %s", exc)
        else:
            logger.warning("personas: LLM call failed: %s", exc)
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
    """First brace-balanced JSON object in ``text`` (string-safe)."""
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


def _persona_batch(
    *,
    role: str,
    client: Any,
    model: str,
    system: str,
    user: str,
    cost_tracker: CostTracker | None,
    cache: Any | None,
    llm_health: LlmHealth | None,
    candidates_for_log: Any = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """One cached, tracked, decision-logged persona batch call.

    Returns ``(parsed_json_or_None, telemetry)``. Every live call is
    recorded on the shared CostTracker (label ``persona_<role>``) — the
    chokepoint that also feeds the decision-log ``llm_call`` record —
    and the PARSED outcome is appended as a ``decision`` record with
    the candidate set (fine-tuning dataset contract, W2a Phase-0).
    """
    from faultline.llm.decision_log import digest_hash, log_decision

    tele: dict[str, Any] = {
        "role": role, "model": model, "cache_hit": False,
        "llm_calls": 0, "input_tokens": 0, "output_tokens": 0,
    }
    key = hashlib.sha256(json.dumps({
        "v": PERSONA_CACHE_VERSION,
        "role": role,
        "model": model,
        "system": system,
        "user": user,
    }, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    if cache is not None:
        try:
            cached = cache.get(CacheKind.LLM_PERSONA.value, key)
        except Exception:  # noqa: BLE001 — cache faults degrade to live
            cached = None
        if (isinstance(cached, dict)
                and cached.get("v") == PERSONA_CACHE_VERSION
                and isinstance(cached.get("parsed"), dict)):
            tele["cache_hit"] = True
            return dict(cached["parsed"]), tele

    in_hash = digest_hash(system, user)
    text, in_tok, out_tok = _call_llm(
        client, model=model, system=system, user=user, llm_health=llm_health)
    tele["llm_calls"] = 1
    tele["input_tokens"], tele["output_tokens"] = in_tok, out_tok
    if cost_tracker is not None and (in_tok or out_tok):
        try:
            cost_tracker.record(
                model=model, input_tokens=in_tok, output_tokens=out_tok,
                label=f"persona_{role}",
                decision_meta={"role": role, "input_digest_hash": in_hash},
            )
        except Exception:  # noqa: BLE001 — budget enforced elsewhere
            pass
    parsed = _parse_json_obj(text)
    try:  # decision tap — labels/verdicts only, never repo content
        log_decision(
            role=role, model=model, input_digest_hash=in_hash,
            candidates=candidates_for_log,
            decision=parsed if parsed is not None else {"parse_failed": True},
        )
    except Exception:  # noqa: BLE001 — the tap must never break a scan
        pass
    if parsed is None:
        tele["parse_failed"] = True
        return None, tele
    if cache is not None:
        try:
            cache.set(CacheKind.LLM_PERSONA.value, key, {
                "v": PERSONA_CACHE_VERSION, "parsed": parsed,
            })
        except Exception:  # noqa: BLE001 — cache write faults never abort
            pass
    return parsed, tele


# ── Composed-label validation (grammar + token evidence) ────────────────


def _thesis_tokens(thesis: Mapping[str, Any] | None) -> set[str]:
    """Evidence tokens from ``scan_meta.product_thesis`` (the reviewed
    W3 consumer seam: thesis vocabulary may inform NAMES, never
    membership)."""
    if not thesis:
        return set()
    toks: set[str] = set()
    for key in ("vertical", "audience", "sentence"):
        for w in re.split(r"[^a-z0-9]+", str(thesis.get(key) or "").lower()):
            if w:
                toks.add(w)
    for obj in (thesis.get("core_objects") or []):
        for w in re.split(r"[^a-z0-9]+", str(obj).lower()):
            if w:
                toks.add(w)
    return toks


def _grammar_verb_match(text: str, vocab: Mapping[str, Any]) -> str | None:
    """The journey verb phrase ``text`` starts with (longest first), or
    ``None`` — composed UF labels MUST be verb-led (journey grammar)."""
    phrases = sorted(
        (str(p) for p in (vocab.get("journey_verb_phrases") or [])),
        key=len, reverse=True,
    )
    for p in phrases:
        if text.startswith(p + " ") or text == p:
            return p
    return None


def _composed_label_ok(
    pick: str,
    *,
    kind: str,
    pf_display: str | None,
    evidence_tokens: set[str],
    vocab: Mapping[str, Any],
) -> bool:
    """Deterministic acceptance for a label the model COMPOSED (not a
    candidate): grammar bounds + display laws + token evidence. The
    grammar's own verb phrase is excused from evidence; every remaining
    content token must be evidenced (naming_validator semantics)."""
    text = " ".join((pick or "").split())
    if not text:
        return False
    max_words = int(vocab.get("compose_max_words") or 8)
    max_chars = int(vocab.get("compose_max_chars") or 60)
    if len(text) > max_chars or len(text.split()) > max_words:
        return False
    if display_law_violations(text, vocab, pf_display=pf_display):
        return False
    rest = text
    if kind == "uf":
        verb = _grammar_verb_match(text, vocab)
        if verb is None:
            return False
        rest = text[len(verb):].strip()
    if not rest:
        return kind == "uf"
    # Token evidence (naming_validator semantics WITHOUT the per-file
    # vendor-domination rule — the evidence here is the item's OWN
    # bounded context, where a vendor token is legitimate by
    # construction: it comes from the vendor PF's display/flows).
    for token in tokenize_name(rest):
        if token in evidence_tokens:
            continue
        if len(token) >= 4 and any(
            len(ev) >= 4 and (ev.startswith(token) or token.startswith(ev))
            for ev in evidence_tokens
        ):
            continue
        return False
    return True


def _item_evidence(item: Any, thesis_toks: set[str]) -> set[str]:
    """Evidence vocabulary for one pending naming item: its candidate
    set, current display, and context values (member flow names, anchor
    id words, pf display) + the thesis tokens."""
    toks: set[str] = set(thesis_toks)

    def _feed(text: str) -> None:
        for w in re.split(r"[^a-z0-9]+", str(text or "").lower()):
            if w:
                toks.add(w)

    _feed(getattr(item, "current", ""))
    for c in (getattr(item, "candidates", None) or []):
        _feed(c)
    ctx = getattr(item, "context", None) or {}
    for v in ctx.values():
        if isinstance(v, (list, tuple)):
            for x in v:
                _feed(str(x))
        elif v is not None:
            _feed(str(v))
    pfd = getattr(item, "pf_display", None)
    if pfd:
        _feed(pfd)
    return toks


# ── PM Labeler (§4.7 role 2 / §4.8 selection-not-generation) ────────────


_LABELER_SYSTEM = """\
You are a senior product manager naming the capabilities and user \
journeys of a software product for its feature map. For every item you \
receive, choose the BEST display name.

Rules (violations are discarded and the deterministic name is kept):
1. STRONGLY PREFER one of the item's listed candidates, verbatim.
2. You MAY compose a better label ONLY when every candidate is poor:
   - capability (kind=pf): a short noun phrase naming what the product \
does there — no file names, no route params, no single letters;
   - journey (kind=uf): MUST start with one of the allowed verb \
phrases and read as actor-intent-outcome ("Manage billing", "Connect \
Slack"); it must NOT equal the capability's own name; only use words \
grounded in the item's candidates/context.
3. Respect the product context you are given — name things the way \
this product's own vocabulary does.
4. Answer for every item id you received.

Output STRICT JSON only:
{"choices": {"<item id>": "<chosen display name>", ...}}
"""


def build_pm_labeler(
    *,
    model_id: str,
    cost_tracker: CostTracker | None = None,
    cache: Any | None = None,
    llm_health: LlmHealth | None = None,
    log: Any = None,
    thesis: Mapping[str, Any] | None = None,
    verifier: Callable[[list[dict[str, Any]]], dict[str, bool]] | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> Callable[[list[Any]], dict[str, Any]] | None:
    """Labeler callable for :func:`naming_contract.run_naming_contract`,
    or ``None`` (persona off / keyless) — the deterministic top choice
    is the display path then.

    The returned callable takes the stage's pending items and returns
    ``{"choices": {key: validated_label}, …telemetry}``; the naming
    stage APPLIES the choices (single display writer). Composed labels
    additionally pass the Draft Verifier when one is supplied (§4.7).
    """
    if not labeler_enabled():
        return None
    client = _client_factory()
    if client is None:
        return None
    model = os.environ.get(LABELER_MODEL_ENV, "").strip() or model_id
    vocab = load_naming_vocab()
    thesis_toks = _thesis_tokens(thesis)
    thesis_line = str((thesis or {}).get("sentence") or "").strip()

    def _labeler(pending: list[Any]) -> dict[str, Any]:
        items = list(pending)[:_MAX_ITEMS_PER_BATCH]
        overflow = max(0, len(pending) - len(items))
        rows = []
        for it in items:
            rows.append({
                "id": it.key,
                "kind": it.kind,
                "current": it.current,
                "candidates": list(it.candidates or []),
                "context": {
                    k: v for k, v in (it.context or {}).items() if v
                },
            })
        user = (
            (f"Product context: {thesis_line}\n\n" if thesis_line else "")
            + "Allowed journey verb phrases (kind=uf composition):\n"
            + json.dumps(
                [str(p) for p in (vocab.get("journey_verb_phrases") or [])],
                ensure_ascii=False)
            + "\n\nItems:\n"
            + json.dumps(rows, ensure_ascii=False)
            + "\n\nEmit the JSON now."
        )
        parsed, tele = _persona_batch(
            role="pm_labeler", client=client, model=model,
            system=_LABELER_SYSTEM, user=user,
            cost_tracker=cost_tracker, cache=cache, llm_health=llm_health,
            candidates_for_log=[
                {"id": r["id"], "candidates": r["candidates"]} for r in rows
            ],
        )
        out: dict[str, Any] = {
            **tele,
            "requested": len(items),
            "overflow_deterministic": overflow,
            "accepted_candidate": 0,
            "accepted_composed": 0,
            "rejected_validation": 0,
            "choices": {},
        }
        raw = (parsed or {}).get("choices")
        if not isinstance(raw, dict):
            out["no_choices"] = True
            return out

        composed_drafts: list[tuple[Any, str]] = []
        for it in items:
            pick = raw.get(it.key)
            if not isinstance(pick, str) or not pick.strip():
                continue
            pick = " ".join(pick.split())
            by_fold = {c.strip().lower(): c for c in (it.candidates or [])}
            hit = by_fold.get(pick.strip().lower())
            if hit is not None:
                out["choices"][it.key] = hit
                out["accepted_candidate"] += 1
                continue
            ev = _item_evidence(it, thesis_toks)
            if _composed_label_ok(
                pick, kind=it.kind, pf_display=it.pf_display,
                evidence_tokens=ev, vocab=vocab,
            ):
                composed_drafts.append((it, pick))
            else:
                out["rejected_validation"] += 1

        if composed_drafts and verifier is not None:
            verdicts = verifier([
                {
                    "id": it.key,
                    "kind": it.kind,
                    "draft": pick,
                    "current": it.current,
                    "pf_display": it.pf_display,
                    "context": {
                        k: v for k, v in (it.context or {}).items() if v
                    },
                }
                for it, pick in composed_drafts
            ]) or {}
            kept: list[tuple[Any, str]] = []
            for it, pick in composed_drafts:
                if verdicts.get(it.key, True):
                    kept.append((it, pick))
                else:
                    out["rejected_validation"] += 1
                    out["verifier_rejected"] = (
                        out.get("verifier_rejected", 0) + 1)
            composed_drafts = kept
        for it, pick in composed_drafts:
            out["choices"][it.key] = pick
            out["accepted_composed"] += 1
        if log is not None:
            try:
                log.info(
                    "pm_labeler: %d items -> %d candidate + %d composed, "
                    "%d rejected (model=%s cache_hit=%s)"
                    % (
                        out["requested"], out["accepted_candidate"],
                        out["accepted_composed"], out["rejected_validation"],
                        model, out.get("cache_hit"),
                    ),
                    feature=None,
                )
            except Exception:  # noqa: BLE001 — logging is best-effort
                pass
        return out

    return _labeler


# ── Surface Adjudicator (§4.7 role 1) — built in W3 commit N4 ───────────


_ADJUDICATOR_SYSTEM = """\
You classify pages/routes of a software product into surface scopes. \
For every item you receive, pick ONE scope from the item's own \
"allowed" list — these are the scopes its deterministic signals \
support; you are resolving the CONFLICT between them, not inventing \
new scopes.

Scope meanings: product = a capability of the product itself; \
marketing = promo/landing content; docs = documentation; legal = \
terms/policies; dev_tooling = internal developer tooling; system = \
background/scheduled/webhook machinery; shell = a bare container page.

Output STRICT JSON only:
{"scopes": {"<item id>": "<scope>", ...}}
"""


def build_surface_adjudicator(
    *,
    model_id: str,
    cost_tracker: CostTracker | None = None,
    cache: Any | None = None,
    llm_health: LlmHealth | None = None,
    log: Any = None,
    thesis: Mapping[str, Any] | None = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> Callable[[list[dict[str, Any]]], dict[str, str]] | None:
    """Adjudicator callable for the emission taxonomy, or ``None``
    (persona off / keyless). Input items:
    ``{"id", "name", "allowed": [scopes with signal], "signals",
    "paths"}``; returns ``{id: scope}`` for VALID verdicts only — a
    verdict outside the item's allowed set is dropped (the
    deterministic conservative verdict stands)."""
    if not adjudicator_enabled():
        return None
    client = _client_factory()
    if client is None:
        return None
    model = os.environ.get(LABELER_MODEL_ENV, "").strip() or model_id
    thesis_line = str((thesis or {}).get("sentence") or "").strip()

    def _adjudicate(items: list[dict[str, Any]]) -> dict[str, str]:
        batch = list(items)[:_MAX_ITEMS_PER_BATCH]
        if not batch:
            return {}
        user = (
            (f"Product context: {thesis_line}\n\n" if thesis_line else "")
            + "Items:\n" + json.dumps(batch, ensure_ascii=False)
            + "\n\nEmit the JSON now."
        )
        parsed, tele = _persona_batch(
            role="surface_adjudicator", client=client, model=model,
            system=_ADJUDICATOR_SYSTEM, user=user,
            cost_tracker=cost_tracker, cache=cache, llm_health=llm_health,
            candidates_for_log=[
                {"id": i.get("id"), "allowed": i.get("allowed")}
                for i in batch
            ],
        )
        raw = (parsed or {}).get("scopes")
        out: dict[str, str] = {}
        if isinstance(raw, dict):
            allowed_by_id = {
                str(i.get("id")): {str(a) for a in (i.get("allowed") or [])}
                for i in batch
            }
            for iid, scope in raw.items():
                if (isinstance(scope, str)
                        and scope in (allowed_by_id.get(str(iid)) or set())):
                    out[str(iid)] = scope
        if log is not None:
            try:
                log.info(
                    "surface_adjudicator: %d ambiguous -> %d verdicts "
                    "(model=%s cache_hit=%s)"
                    % (len(batch), len(out), model, tele.get("cache_hit")),
                    feature=None,
                )
            except Exception:  # noqa: BLE001
                pass
        return out

    return _adjudicate


# ── Draft Verifier (§4.7 role 3) — built in W3 commit N5 ────────────────


_VERIFIER_SYSTEM = """\
You review DRAFT display names and synthesized user journeys of a \
software product's feature map against their structural evidence. For \
every item, answer whether the draft is an honest, recognizable label \
for that evidence.

Reject (false) when: the draft misrepresents the evidence, names a \
dev artifact (file name, directory, parameter), duplicates the \
capability name at journey grain, or is not something a product \
manager would put in a customer-facing feature list. Accept (true) \
otherwise.

Items with kind="lattice_split" are PROPOSED CHILD JOURNEYS being \
split out of an oversized catch-all journey (the item names the \
catch-all parent, the child's member flows, and the structural \
evidence the child clusters on). Judge EACH child on its own: accept \
(true) when it reads as a distinct, recognizable user journey a \
product manager would list separately from the parent — this is the \
normal, desired outcome for a catch-all. Reject (false) ONLY a \
dishonest child: an arbitrary technical shard of what is clearly the \
parent's own journey, or a name that misrepresents its member flows. \
A rejected child's flows simply stay in the original catch-all.

Output STRICT JSON only:
{"verdicts": {"<item id>": true|false, ...}}
"""


def build_draft_verifier(
    *,
    model_id: str,
    cost_tracker: CostTracker | None = None,
    cache: Any | None = None,
    llm_health: LlmHealth | None = None,
    log: Any = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> Callable[[list[dict[str, Any]]], dict[str, bool]] | None:
    """Verifier callable, or ``None`` (persona off / keyless).

    Input items: ``{"id", "kind", "draft", …evidence}``. Returns
    ``{id: bool}``. Missing ids default to ACCEPT at the call site
    (rejects must be explicit; a parse failure never blocks). On a
    batch with rejects, ONE retry re-asks about the rejected items —
    on the escalation model when ``FAULTLINE_PERSONA_ESCALATION_MODEL``
    is set — and a repeated reject is final (deterministic fallback at
    the call site).
    """
    if not verifier_enabled():
        return None
    client = _client_factory()
    if client is None:
        return None
    model = os.environ.get(LABELER_MODEL_ENV, "").strip() or model_id

    def _ask(items: list[dict[str, Any]], use_model: str) -> dict[str, bool]:
        user = ("Items:\n" + json.dumps(items, ensure_ascii=False)
                + "\n\nEmit the JSON now.")
        parsed, _tele = _persona_batch(
            role="draft_verifier", client=client, model=use_model,
            system=_VERIFIER_SYSTEM, user=user,
            cost_tracker=cost_tracker, cache=cache, llm_health=llm_health,
            candidates_for_log=[
                {"id": i.get("id"), "draft": i.get("draft")} for i in items
            ],
        )
        raw = (parsed or {}).get("verdicts")
        out: dict[str, bool] = {}
        if isinstance(raw, dict):
            for iid, v in raw.items():
                if isinstance(v, bool):
                    out[str(iid)] = v
        return out

    def _verify(items: list[dict[str, Any]]) -> dict[str, bool]:
        batch = list(items)[:_MAX_ITEMS_PER_BATCH]
        if not batch:
            return {}
        verdicts = _ask(batch, model)
        rejected = [i for i in batch if verdicts.get(str(i.get("id"))) is False]
        if rejected:
            # Reject → ONE retry; the escalation model (when set) fires
            # ONLY here — after a reject, never on the first pass.
            retry_model = escalation_model() or model
            second = _ask(rejected, retry_model)
            for i in rejected:
                iid = str(i.get("id"))
                if second.get(iid) is True:
                    verdicts[iid] = True
        if log is not None:
            try:
                rej = sum(1 for v in verdicts.values() if v is False)
                log.info(
                    "draft_verifier: %d drafts, %d rejected (model=%s)"
                    % (len(batch), rej, model),
                    feature=None,
                )
            except Exception:  # noqa: BLE001
                pass
        return verdicts

    return _verify
