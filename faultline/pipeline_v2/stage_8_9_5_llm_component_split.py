"""Stage 8.9.5 — LLM-semantic component-blob decomposition.

Deterministic Stage 8.9 stops at a ``components`` dir (``_TERMINAL_SEGMENTS``)
because its children are USUALLY bare UI primitives (``components/Button``,
``components/v2/Accordion``). But a large app routinely groups its components BY
PRODUCT AREA — ``apps/web/core/components/{issues,cycles,modules}`` (plane),
``apps/studio/components/interfaces/{Auth,Database,Storage}`` (supabase) — where
the children ARE real product domains. Those subtrees stay in the residual and
keep the feature a blob (plane ``web`` 0.39, supabase ``studio`` 0.28).

Casing / structure CANNOT separate the two cases: plane mixes lowercase product
domains (``issues``) with lowercase UI groupings (``dropdowns``/``icons``);
supabase's product areas are PascalCase (``Auth``). The deterministic split was
proven a measured dead-end here (the existing
``test_components_v2_accordion_does_not_mint_accordion`` guard correctly forbids
a casing-only rule). A small LLM call CAN make the semantic call: it reads the
child-directory NAMES (plus a couple of sample file names — NEVER file
contents) and labels each ``domain`` vs ``ui``. Product domains are split into
per-area sub-features (reusing the Stage 8.9 minting machinery verbatim); UI
groupings stay in the shared residual.

Why this is NOT the rejected metric-gaming (Stage 8.6 wide component-deown):
files are not removed into a scaffold sink — they are MOVED into REAL named
product features. Coverage is preserved (every owned file ends in exactly one
feature, never orphaned); ``owned_max`` can only drop because the split
universe is the source's own owned set (file conservation, same contract as
:func:`_split_one_feature`). It is precision-POSITIVE: plane's ``issues``
components become an ``issues`` feature instead of the ``web`` blob.

Cost & determinism: ONE LLM call per qualifying component blob — a directory
listing, not per file → cents across a corpus. The label is cached by
prompt-hash, so a re-scan reuses it (the only non-determinism is the cached
label; the gate and split are fully deterministic). Default OFF (opt-in):
``FAULTLINE_STAGE_8_9_5_LLM_COMPONENT_SPLIT=1``.

Fan-out depth (v2): :func:`_component_fanout` finds the product-area level
whether it sits directly under ``components`` (plane ``core/components/<area>``)
or one level deeper under a grouping dir (supabase
``components/interfaces/<Area>``) — it picks the prefix with the widest
distinct-child fan-out, so an intermediate grouping dir is descended through.

Known limitation (validated, honest no-harm):
  * **No husk cleanup.** Unlike Stage 8.9, this stage does NOT run
    ``_cleanup_husks`` after a split, so a source whose entire owned set was
    product-domain components ends as a (de-owned, role="shared") residual
    feature. This is deliberate: that residual is the genuine cross-cutting UI
    scaffold (plane ``web`` → dropdowns/icons/sidebar/assets), and dropping it
    would ORPHAN those shared claims (coverage loss). It does not regress
    ``owned_max`` or conservation; surfacing a thin shared-UI feature is the
    cost. Revisit before default-ON.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from faultline.llm.cost import deterministic_params
from faultline.llm.model_gateway import resolve_model as gateway_model
from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import (
    _MIN_DOMAINS,
    _OVERSIZED_MEDIAN_MULT,
    _OVERSIZED_SHARE,
    _deown_residual,
    _make_subfeature,
    _owned_paths,
    _slug,
)

if TYPE_CHECKING:
    from faultline.models.types import Feature

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "haiku"
_CACHE_KIND = "llm-component-split"

# A ``components`` dir worth an LLM call must fan out into at least this many
# distinct child dirs — reuse the universal split floor, never a tuned constant
# (rule-no-magic-tuning).
_MIN_COMPONENT_CHILDREN = _MIN_DOMAINS  # 2
# A LLM-vetted product-domain child promotes when it holds at least this many
# files — a minimal STRUCTURAL floor ("a directory of >=2 components"), NOT the
# repo-grain median: the LLM already vouched for product-relevance, so the
# grain floor (which exists to stop DETERMINISTIC over-shatter) would only
# discard small-but-real product areas (``license``, ``instance``) back into
# the blob. Scale-invariant, corpus-free (rule-no-magic-tuning).
_MIN_DOMAIN_FILES = 2
# Component-COLLECTION container tokens (the subset of _TERMINAL_SEGMENTS whose
# children can legitimately be product areas). Narrow + universal — NOT
# ``ui``/``widgets`` (their children are primitives, never domains).
# ``hooks`` added 2026-07-02 (A2 hooks-class): large apps group data/API hooks
# by product domain exactly like components (infisical
# ``frontend/src/hooks/api/{auth,certificates,secrets,…}`` ×108) — the v2
# fan-out descent finds the domain level through the ``api`` grouping dir.
_COMPONENT_SEGS = frozenset({"components", "component", "hooks", "hook"})
_SAMPLES_PER_CHILD = 2
# Bound the prompt on a pathological fan-out; the largest children win.
_MAX_CHILDREN_IN_PROMPT = 80
_MAX_TOKENS = 1024


def _is_enabled() -> bool:
    """Default OFF (opt-in).

    Enable via ``FAULTLINE_STAGE_8_9_5_LLM_COMPONENT_SPLIT=1``.
    """
    return (
        os.environ.get("FAULTLINE_STAGE_8_9_5_LLM_COMPONENT_SPLIT", "0") != "0"
    )


@dataclass
class LlmComponentSplitResult:
    """Per-scan telemetry for the LLM component-split stage."""

    enabled: bool = False
    features_examined: int = 0       # oversized features inspected
    candidates: int = 0              # ...with a real component fan-out
    llm_calls: int = 0
    cache_hits: int = 0
    features_split: int = 0
    subfeatures_created: int = 0
    paths_moved: int = 0
    domains_labelled: int = 0        # children the LLM called a product domain
    groupings_labelled: int = 0      # children the LLM called a UI grouping
    sample: list[dict[str, Any]] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "features_examined": self.features_examined,
            "candidates": self.candidates,
            "llm_calls": self.llm_calls,
            "cache_hits": self.cache_hits,
            "features_split": self.features_split,
            "subfeatures_created": self.subfeatures_created,
            "paths_moved": self.paths_moved,
            "domains_labelled": self.domains_labelled,
            "groupings_labelled": self.groupings_labelled,
            "sample": list(self.sample[:20]),
        }


# ── component fan-out detection ─────────────────────────────────────────────


def _component_fanout(
    owned: list[str],
) -> tuple[str, dict[str, list[str]]] | None:
    """Locate the product-area FAN-OUT level within *owned*'s ``components``
    subtree — the directory at or below a ``components`` segment with the most
    distinct child dirs. Returns ``(container_prefix, {child_dir: [files]})`` or
    ``None``.

    v2 — descend through an intermediate grouping dir. A child qualifies only
    when it is itself a DIRECTORY (a deeper segment follows), so a bare
    ``components/Button.tsx`` file never becomes a child. We consider EVERY
    prefix from the first ``components`` segment downward, then pick the one with
    the largest fan-out. That finds the real product areas whether they sit
    directly under ``components`` (plane: ``core/components/{issues,cycles,…}``,
    fan-out AT ``components``) OR one level deeper under a grouping dir (supabase:
    ``components/interfaces/{Auth,Database,…}``, fan-out at
    ``components/interfaces`` — which v1's immediate-child-only walk missed).
    The deeper-but-thinner intermediate level (``components`` with a handful of
    children) loses to the wider product-area level by the distinct-child count.
    """
    cand: dict[str, dict[str, list[str]]] = {}
    for p in owned:
        segs = p.split("/")
        # First ``components`` segment on the path (a nested
        # ``components/x/components/y`` keys under the OUTER one).
        comp_idx = next(
            (
                d
                for d in range(len(segs) - 1)
                if segs[d].lower() in _COMPONENT_SEGS
            ),
            None,
        )
        if comp_idx is None:
            continue
        # Every dir prefix AT or BELOW the components segment is a candidate
        # container; its child (segs[d + 1]) must itself be a directory.
        for d in range(comp_idx, len(segs) - 1):
            if d + 2 <= len(segs) - 1:
                prefix = "/".join(segs[: d + 1])
                child = segs[d + 1]
                cand.setdefault(prefix, {}).setdefault(child, []).append(p)
    if not cand:
        return None
    # The container with the MOST distinct children is the product-area fan-out.
    best = max(cand, key=lambda k: len(cand[k]))
    children = cand[best]
    if len(children) < _MIN_COMPONENT_CHILDREN:
        return None
    return best, children


# ── LLM classification (cached) ─────────────────────────────────────────────


def _build_prompt(
    repo_slug: str, container: str, children: dict[str, list[str]],
) -> str:
    """Build the classification prompt — child dir NAMES + a couple of sample
    file basenames each. No file contents (cheap + privacy-safe)."""
    # Largest children first; bound the count.
    ordered = sorted(children.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines: list[str] = []
    for child, files in ordered[:_MAX_CHILDREN_IN_PROMPT]:
        samples = [f.rsplit("/", 1)[-1] for f in files[:_SAMPLES_PER_CHILD]]
        lines.append(
            f"- {child}  ({len(files)} files; e.g. {', '.join(samples)})"
        )
    listing = "\n".join(lines)
    return (
        "You are labelling the immediate child directories of a "
        f"`{container}` directory in the `{repo_slug}` codebase.\n\n"
        "Each child is EITHER:\n"
        '  - "domain": a distinct PRODUCT capability / feature area '
        "(e.g. issues, billing, auth, storage, reports, settings, "
        "table-editor) — code grouped because it serves that "
        "product area.\n"
        '  - "ui": a generic PRESENTATIONAL or UTILITY grouping with no '
        "product meaning (e.g. dropdowns, icons, buttons, modals, layouts, "
        "primitives, ui, common, shared, forms, skeletons, utils, helpers).\n\n"
        "Children:\n"
        f"{listing}\n\n"
        'Return ONLY a JSON object mapping each child name to "domain" or '
        '"ui". No prose. Example: {"issues":"domain","icons":"ui"}.'
    )


def _parse_labels(
    text: str, children: dict[str, list[str]],
) -> dict[str, str]:
    """Parse the model's JSON map; keep only known children + valid labels."""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for child in children:
        label = data.get(child)
        if isinstance(label, str) and label.strip().lower() in {"domain", "ui"}:
            out[child] = label.strip().lower()
    return out


def _classify(
    client: Any | None,
    model: str,
    repo_slug: str,
    container: str,
    children: dict[str, list[str]],
    cache_backend: Any | None,
    result: LlmComponentSplitResult,
) -> dict[str, str]:
    """Return ``{child: 'domain'|'ui'}``. Cached by prompt-hash. Empty dict
    when the client is unavailable or the call/parse fails (→ no split)."""
    prompt = _build_prompt(repo_slug, container, children)
    key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    if cache_backend is not None:
        try:
            cached = cache_backend.get(_CACHE_KIND, key)
        except Exception:  # pragma: no cover — cache is best-effort
            cached = None
        if isinstance(cached, dict) and cached:
            result.cache_hits += 1
            # Re-validate against the CURRENT children (cache is structural).
            return {c: v for c, v in cached.items() if c in children}

    if client is None:
        return {}

    try:
        msg = client.messages.create(
            model=gateway_model(model),
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
            **deterministic_params(model),
        )
        text = "".join(
            getattr(b, "text", "") for b in (getattr(msg, "content", None) or [])
        )
    except Exception as exc:  # pragma: no cover — network/SDK failure
        logger.warning("stage_8_9_5: classify call failed: %s", exc)
        return {}

    result.llm_calls += 1
    labels = _parse_labels(text, children)
    if labels and cache_backend is not None:
        try:
            cache_backend.set(_CACHE_KIND, key, labels)
        except Exception:  # pragma: no cover
            pass
    return labels


# ── stage entry ─────────────────────────────────────────────────────────────


def _default_client_factory() -> Any | None:  # pragma: no cover — IO
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic.Anthropic(api_key=api_key)


def llm_component_split(
    features: list["Feature"],
    *,
    client: Any | None = None,
    model: str = DEFAULT_MODEL,
    cache_backend: Any | None = None,
    repo_slug: str = "repo",
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> LlmComponentSplitResult:
    """Split oversized component-blob features into per-product-area
    sub-features using a cached LLM label per blob. Mutates *features* in
    place (de-owns the split source, appends sub-features). Returns telemetry.

    No-op when disabled, when no client is available, or when no oversized
    feature carries a real ``components`` fan-out — safe to wire unconditionally.
    """
    result = LlmComponentSplitResult(enabled=_is_enabled())
    if not result.enabled:
        return result

    devs = [
        f for f in features
        if getattr(f, "layer", "developer") == "developer"
    ]
    if not devs:
        return result

    # Repo-grain thresholds — IDENTICAL definition to Stage 8.9
    # (rule-no-magic-tuning): floor = median feature size, oversized cut =
    # max(2x median, 15% of all owned files), anti-shatter cap = other-feature
    # count. Computed once over the current developer-feature set.
    owned_by = {id(f): _owned_paths(f) for f in devs}
    sizes = [len(v) for v in owned_by.values() if v]
    if not sizes:
        return result
    median = max(2, int(statistics.median(sizes)))
    total_owned = len({p for v in owned_by.values() for p in v})
    cut = max(
        _OVERSIZED_MEDIAN_MULT * median,
        math.ceil(_OVERSIZED_SHARE * total_owned),
    )
    max_domains = max(_MIN_DOMAINS, len(devs) - 1)

    if client is None:
        client = _client_factory()

    used_names = {f.name for f in features}

    for source in list(devs):
        owned = owned_by[id(source)]
        # Oversized when EITHER surface crosses the cut: the owned set (what
        # the split moves) OR the member_files ledger (the surface the blob
        # metric reads). A workspace anchor that Stage 8.7 de-sink already
        # de-owned can still be the repo's blob via its member ledger
        # (infisical frontend-v2: 762 owned vs 1565 members, 2026-07-02) —
        # gating on owned alone blind-spots exactly the features this stage
        # exists to fix. The split itself still operates on owned paths only.
        ledger = len(getattr(source, "member_files", None) or [])
        if len(owned) <= cut and ledger <= cut:
            continue  # not oversized on either surface
        result.features_examined += 1
        fan = _component_fanout(owned)
        if fan is None:
            continue
        container, children = fan
        result.candidates += 1

        labels = _classify(
            client, model, repo_slug, container, children, cache_backend, result,
        )
        if not labels:
            continue  # no usable labels → leave the feature untouched

        # Promote product-DOMAIN children (>= floor files) into sub-features;
        # UI groupings + sub-floor domains stay in the residual.
        domain_children = {
            c: f for c, f in children.items()
            if labels.get(c) == "domain"
        }
        result.domains_labelled += len(domain_children)
        result.groupings_labelled += len(children) - len(domain_children)

        domains: dict[str, list[str]] = {
            f"{container}/{c}": f
            for c, f in domain_children.items()
            if len(f) >= _MIN_DOMAIN_FILES
        }
        # Anti-shatter cap (same contract as Stage 8.9): keep the largest
        # ``max_domains`` domains; thinner ones fold back to the residual.
        if len(domains) > max_domains:
            ordered = sorted(
                domains.items(), key=lambda kv: (-len(kv[1]), kv[0]),
            )
            domains = dict(ordered[:max_domains])
        if len(domains) < _MIN_DOMAINS:
            continue  # not a real fan-out of product domains → no split

        # File conservation (mirror _split_one_feature): the split universe is
        # the OWNED set. moved = files entering a sub-feature; residual = the
        # rest (kept on the source as role="shared"), plus any path-only entries
        # that were never owned. moved ∪ residual == owned ∪ path_only, exactly.
        moved: set[str] = {p for files in domains.values() for p in files}
        path_only = [
            p for p in (getattr(source, "paths", None) or [])
            if p not in owned
        ]
        residual = (set(owned) - moved) | set(path_only)
        # Zero-path protection — never empty the source: if every owned file
        # moved into a domain, keep the smallest domain as the residual instead.
        if not residual:
            smallest = min(domains, key=lambda k: len(domains[k]))
            residual = set(domains.pop(smallest))
            moved -= residual
            if len(domains) < 1:
                continue

        minted: list["Feature"] = []
        for domain_key, files in domains.items():
            name = _slug(domain_key, used_names)
            minted.append(_make_subfeature(source, domain_key, files, name))

        _deown_residual(source, moved, residual)
        features.extend(minted)
        result.features_split += 1
        result.subfeatures_created += len(minted)
        result.paths_moved += len(moved)
        if len(result.sample) < 20:
            result.sample.append({
                "feature": source.name,
                "container": container,
                "domains": sorted(d.rsplit("/", 1)[-1] for d in domains),
                "groupings": sorted(
                    c for c in children if labels.get(c) != "domain"
                )[:25],
                "moved": len(moved),
            })

    return result


__all__ = ["LlmComponentSplitResult", "llm_component_split"]
