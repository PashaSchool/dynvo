"""Phase 3 — DUAL-EVIDENCE + CONFIDENCE (deterministic, no LLM).

For every product feature (and user flow) attach TWO independent kinds of
evidence and a confidence score:

  * CODE evidence — the files/directories that back the item (from the feature's
    own member paths). Always present (the item is code-grounded).
  * PRODUCT-SOURCE evidence — the deterministic anchors (i18n label, navigation,
    analytics event) whose text CORROBORATES the item's name. A matching anchor
    means the maintainer's own product vocabulary independently names this
    capability. Non-matching anchors are simply ignored — so this is safe on a
    noisy anchor pool (unlike ALIGN, which forces names).

  * CONFIDENCE (0-1) — two-source agreement, tiered by anchor source trust:
      code-only .......... 0.50   (grounded, but no product-source corroboration)
      code + i18n anchor . 0.70   (a UI label agrees)
      code + nav/analytics 0.90   (an intentional product surface agrees)
    Capped at 0.70 when the engine's own name_confidence is "low". Tiers reflect
    source trust — NOT per-repo tuned numbers (rule-no-magic-tuning).

Pure, deterministic, best-effort: a match failure on any item is swallowed so
this can never crash a scan. Attaches to ``feature.dual_evidence`` /
``uf.dual_evidence`` as a plain dict (additive, back-compat).
"""
from __future__ import annotations

import os
import re
from typing import Any

__all__ = ["attach_dual_evidence", "match_anchors", "dual_evidence_enabled", "ENV_FLAG"]

ENV_FLAG = "FAULTLINE_DUAL_EVIDENCE"


def dual_evidence_enabled() -> bool:
    """Opt-in (default OFF for a first ship). Deterministic + additive, so safe to
    default ON once validated."""
    return os.environ.get(ENV_FLAG, "0").strip() not in {"0", "false", "False", ""}

# Generic tokens that must NOT alone establish a match (they corroborate nothing
# distinctive). A bounded stop-list, not a tuned knob.
_GENERIC = frozenset({
    "the", "and", "for", "with", "view", "list", "page", "detail", "details",
    "management", "manage", "settings", "setting", "integration", "integrations",
    "system", "data", "new", "edit", "create", "delete", "update", "add", "all",
})
_MIN_DISTINCTIVE_LEN = 4  # a (stemmed) shared token >= this length is distinctive
_SOURCE_TRUST = {"analytics": 3, "nav": 2, "docs": 2, "i18n": 1, "test": 0, "docs_nav": 1}
_MAX_ANCHORS_PER_ITEM = 3
_MAX_CODE_PATHS = 5
_MAX_ANCHOR_WORDS = 6  # a capability anchor is a short label, not a sentence/UI copy


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 4 and t.endswith("s") else t


def _tokens(text: str) -> set[str]:
    return {_stem(t) for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}


def _distinctive_tokens(tok: set[str]) -> set[str]:
    """The subset of tokens strong enough to establish a match on their own."""
    return {t for t in tok if len(t) >= _MIN_DISTINCTIVE_LEN and t not in _GENERIC}


def match_anchors(name: str, description: str, anchors: list[Any]) -> list[dict[str, str]]:
    """Return anchors that corroborate ``name``/``description``, best-first.

    An anchor corroborates when it shares a DISTINCTIVE token (stemmed, len >= 4,
    not generic) with the item — whole-token match, not raw substring, so short
    UI strings ("AND", "All") and pure-generic labels never match. Ranked by
    source trust, then #distinctive-tokens-shared, then stable by text; capped."""
    # Match on the NAME (the capability label) only — descriptions cross-link
    # unrelated features (a chat feature whose description mentions "investigate"
    # would wrongly match investigation anchors). Precision > recall for evidence.
    _ = description  # intentionally unused; kept for signature stability
    item_tok = _distinctive_tokens(_tokens(name))
    if not item_tok:
        return []
    scored: list[tuple[int, int, int, str, dict[str, str]]] = []
    for a in anchors or []:
        text = (getattr(a, "text", "") or "").strip()
        if not text:
            continue
        nwords = len(text.split())
        if nwords > _MAX_ANCHOR_WORDS:  # a sentence / UI copy, not a capability label
            continue
        a_dist = _distinctive_tokens(_tokens(text))
        shared = a_dist & item_tok
        if not shared:  # the anchor's own distinctive tokens must overlap the item
            continue
        source = str(getattr(a, "source", "") or "")
        # rank: source trust, then more shared tokens, then SHORTER (more
        # label-like), then stable by text.
        scored.append((_SOURCE_TRUST.get(source, 1), len(shared), -nwords, text.lower(),
                       {"text": text, "source": source,
                        "locator": str(getattr(a, "locator", "") or "")}))
    scored.sort(key=lambda s: (-s[0], -s[1], -s[2], s[3]))
    return [d for _, _, _, _, d in scored[:_MAX_ANCHORS_PER_ITEM]]


def _confidence(matched: list[dict[str, str]], name_conf: str) -> float:
    if not matched:
        conf = 0.5
    else:
        best = max((_SOURCE_TRUST.get(m["source"], 1) for m in matched), default=1)
        conf = 0.9 if best >= 2 else 0.7      # nav/analytics (>=2) vs i18n
    if name_conf == "low":
        conf = min(conf, 0.7)
    return round(conf, 2)


def _code_paths(item: Any) -> list[str]:
    raw = getattr(item, "paths", None) or getattr(item, "member_files", None) or []
    out: list[str] = []
    for x in raw:
        p = x if isinstance(x, str) else getattr(x, "path", None)
        if isinstance(p, str) and p not in out:
            out.append(p)
        if len(out) >= _MAX_CODE_PATHS:
            break
    return out


def attach_dual_evidence(
    product_features: list[Any],
    user_flows: list[Any],
    anchors: list[Any],
) -> dict[str, int]:
    """Attach ``dual_evidence`` (code + anchor + confidence) to each product
    feature and user flow, in place. Returns telemetry. Never raises."""
    stats = {"pf": 0, "pf_corroborated": 0, "uf": 0, "uf_corroborated": 0}
    for f in product_features or []:
        try:
            name = getattr(f, "display_name", None) or getattr(f, "name", "") or ""
            matched = match_anchors(name, getattr(f, "description", "") or "", anchors)
            conf = _confidence(matched, str(getattr(f, "name_confidence", "high")))
            f.dual_evidence = {"code": _code_paths(f), "anchors": matched, "confidence": conf}
            stats["pf"] += 1
            stats["pf_corroborated"] += 1 if matched else 0
        except Exception:  # noqa: BLE001 — evidence is best-effort, never fatal
            continue
    for u in user_flows or []:
        try:
            matched = match_anchors(getattr(u, "name", "") or "",
                                    getattr(u, "description", "") or "", anchors)
            conf = _confidence(matched, str(getattr(u, "name_confidence", "high")))
            u.dual_evidence = {"code": [], "anchors": matched, "confidence": conf}
            stats["uf"] += 1
            stats["uf_corroborated"] += 1 if matched else 0
        except Exception:  # noqa: BLE001
            continue
    return stats
