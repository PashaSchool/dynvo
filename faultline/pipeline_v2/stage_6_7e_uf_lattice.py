"""Stage 6.7e — UF grain lattice (capability rollup nodes over journey leaves).

Spec: faultlines-app ``docs/specs/grain-lattice.md`` (approved 2026-07-04,
MISSION-92 cycle-3). A flat ``user_flows[]`` list forces ONE grain choice and
no single grain is right — golden specs mix journey-grain and capability-grain
even within a repo. The lattice makes the output TWO-level without touching
the leaves:

  * Leaves: ``user_flows[]`` stays byte-identical — every downstream consumer
    is unaffected.
  * Capability nodes: NEW top-level ``uf_capabilities[]`` — each with name,
    intent, resource, ``member_uf_ids`` (leaf children), ``member_flow_ids`` /
    ``routes`` (union of the children's grounding). Every leaf belongs to
    exactly ONE capability (surjective, no orphans); single-leaf (degenerate)
    nodes are legal.

Design choice (mission directive "deterministic-first, LLM only where
ambiguous — measured"): grouping is FULLY deterministic — leaves group by
``(normalized resource, intent family)``. Intent families fold the judge-
sanctioned aggregations: {author, manage, lifecycle, browse} → ``manage``
(CRUD aggregation + same-surface view==manage are both MATCH-true classes in
the validated pairwise judge), while execute / bulk / export / other stay
separate (different action families are MATCH-false there). Naming is
deterministic by default — "Manage <resource plural>" for multi-leaf manage
nodes, the heaviest child's own name for other multi-leaf nodes, the leaf
name verbatim for degenerate nodes. An injectable ``namer`` hook exists for
the ambiguous class (multi-leaf non-manage nodes) so a cheap Haiku naming
call can be wired later (band revival); the default pipeline makes ZERO LLM
calls here — stable, $0, replay-identical.

Structural post-validation (spec-mandated, reject-and-regroup):
  * surjectivity — every leaf id in exactly one capability;
  * parent/child consistency — every child's (resource, family) equals the
    node's; grounding unions must equal the children's unions.
A violating node is dissolved into degenerate per-leaf nodes (never dropped
— no orphans by construction); ``regroup_count`` telemetry records it.

Env: ``FAULTLINE_UF_LATTICE`` — default ON (additive output). Set ``0`` to
disable.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from faultline.models.types import UfCapability, UserFlow
    from faultline.pipeline_v2.llm_health import LlmHealth

ENV_FLAG = "FAULTLINE_UF_LATTICE"

#: leaf intent → capability intent family. The manage family folds exactly
#: the aggregations the validated pairwise judge scores MATCH=true on one
#: resource (CRUD aggregation; same-surface view/manage). Everything else
#: keeps its own family — merging across action families would build nodes
#: the judge (correctly) rejects, and it needlessly risks the mega-parent
#: guard. Unknown intents fall to "other".
_INTENT_FAMILY = {
    "author": "manage",
    "manage": "manage",
    "lifecycle": "manage",
    "browse": "manage",
    "execute": "execute",
    "bulk": "bulk",
    "export": "export",
    "other": "other",
}

#: The ambiguous-naming class: multi-leaf nodes of these families have no
#: safe deterministic template ("Manage X" would misname an execute bundle).
_AMBIGUOUS_FAMILIES = frozenset({"execute", "bulk", "export", "other"})

#: A namer receives the ambiguous groups
#: ``[{"idx": int, "resource": str, "family": str, "leaf_names": [str]}]``
#: and returns ``{idx: name}`` for any subset (missing / falsy entries fall
#: back to the deterministic name). Returning ``None`` = total failure →
#: deterministic fallback for every group.
Namer = Callable[[list[dict[str, Any]]], "dict[int, str] | None"]


def lattice_enabled() -> bool:
    """Default ON — the lattice is additive output."""
    return os.environ.get(ENV_FLAG, "1").strip().lower() not in {"0", "false"}


# ── Deterministic helpers ───────────────────────────────────────────────────


def _norm_resource(resource: str | None) -> str:
    """Lowercased, crudely singularised resource key ("" = no resource)."""
    r = re.sub(r"\s+", " ", (resource or "").strip().lower())
    if len(r) > 3 and r.endswith("s") and not r.endswith("ss"):
        r = r[:-1]
    return r


def _plural(resource: str) -> str:
    if not resource:
        return resource
    return resource if resource.endswith("s") else resource + "s"


def _family(intent: str | None) -> str:
    return _INTENT_FAMILY.get((intent or "").strip().lower(), "other")


def _leaf_sort_key(leaf: "UserFlow") -> tuple:
    """Content-stable ordering for children inside a node: heaviest
    (most-supported) journey first, then name, then id."""
    return (
        -(leaf.member_count or 0),
        (leaf.name or "").lower(),
        leaf.id or "",
    )


def _template_name(resource: str, family: str, children: list["UserFlow"]) -> str:
    """Deterministic node name. Degenerate nodes keep the leaf name verbatim
    (maximum specificity — the leaf IS the capability). Multi-leaf manage
    nodes take the canonical capability phrasing; other multi-leaf families
    are named after their heaviest child (grounded — never an invented
    label)."""
    if len(children) == 1:
        return children[0].name
    if family == "manage" and resource:
        return f"Manage {_plural(resource)}"
    return children[0].name  # children pre-sorted heaviest-first


# ── Grouping ────────────────────────────────────────────────────────────────


def _group_leaves(user_flows: list["UserFlow"]) -> list[dict[str, Any]]:
    """Deterministic grouping pass → raw node dicts (pre-naming).

    A leaf with an empty resource is its own degenerate node — unknown
    mergeability is treated conservatively (never lump no-resource leaves
    into a fake shared bucket)."""
    grouped: dict[tuple[str, str], list["UserFlow"]] = defaultdict(list)
    singletons: list["UserFlow"] = []
    for uf in user_flows:
        res = _norm_resource(uf.resource)
        if not res:
            singletons.append(uf)
            continue
        grouped[(res, _family(uf.intent))].append(uf)

    nodes: list[dict[str, Any]] = []
    for (res, family), children in grouped.items():
        children = sorted(children, key=_leaf_sort_key)
        nodes.append({"resource": res, "family": family, "children": children})
    for uf in singletons:
        nodes.append({
            "resource": "",
            "family": _family(uf.intent),
            "children": [uf],
        })
    # Deterministic node order (input-order independent): by resource, then
    # family, then first child's key.
    nodes.sort(key=lambda n: (
        n["resource"],
        n["family"],
        _leaf_sort_key(n["children"][0]),
    ))
    return nodes


# ── Structural post-validation (reject-and-regroup) ─────────────────────────


def _node_consistent(node: dict[str, Any]) -> bool:
    """Parent/child consistency: every child's (resource, family) must equal
    the node's key. Degenerate no-resource nodes are consistent by
    construction (their key IS the child's)."""
    res, family = node["resource"], node["family"]
    for child in node["children"]:
        child_res = _norm_resource(child.resource)
        if res and (child_res != res or _family(child.intent) != family):
            return False
        if not res and len(node["children"]) != 1:
            return False  # no-resource nodes must be degenerate
    return True


def _validate_and_regroup(
    nodes: list[dict[str, Any]], user_flows: list["UserFlow"],
) -> tuple[list[dict[str, Any]], int]:
    """Enforce the spec's structural contract; returns (nodes, regroup_count).

    * an INCONSISTENT node dissolves into degenerate per-leaf nodes;
    * a leaf claimed by 2+ nodes keeps its FIRST (sorted-order) node, later
      claims dissolve likewise;
    * an ORPHAN leaf (in no node) gets its own degenerate node.
    Never drops a leaf — surjectivity holds by construction afterwards."""
    regroups = 0
    out: list[dict[str, Any]] = []
    seen_leaf_ids: set[str] = set()

    def _degenerate(leaf: "UserFlow") -> dict[str, Any]:
        return {
            "resource": _norm_resource(leaf.resource),
            "family": _family(leaf.intent),
            "children": [leaf],
        }

    for node in nodes:
        fresh = [c for c in node["children"] if (c.id or "") not in seen_leaf_ids]
        dropped_dupes = len(node["children"]) - len(fresh)
        if not fresh:
            regroups += 1
            continue
        node = {**node, "children": fresh}
        if dropped_dupes or not _node_consistent(node):
            regroups += 1
            for leaf in fresh:
                seen_leaf_ids.add(leaf.id or "")
                out.append(_degenerate(leaf))
            continue
        for leaf in fresh:
            seen_leaf_ids.add(leaf.id or "")
        out.append(node)

    for uf in user_flows:  # orphan rescue — every leaf MUST have a parent
        if (uf.id or "") not in seen_leaf_ids:
            regroups += 1
            seen_leaf_ids.add(uf.id or "")
            out.append(_degenerate(uf))
    return out, regroups


# ── Materialisation ─────────────────────────────────────────────────────────


def _percentile(sorted_vals: list[int], q: float) -> int:
    """Nearest-rank percentile on a pre-sorted list (deterministic, no numpy)."""
    if not sorted_vals:
        return 0
    idx = max(0, min(len(sorted_vals) - 1,
                     int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def build_uf_lattice(
    user_flows: list["UserFlow"],
    *,
    namer: Namer | None = None,
) -> tuple[list["UfCapability"], dict[str, Any]]:
    """Group the journey leaves into capability nodes.

    Returns ``(uf_capabilities, telemetry)``. Pure and deterministic for a
    fixed input (and for any input ORDER — grouping keys and sort keys are
    content-derived); the optional ``namer`` may rename ambiguous multi-leaf
    nodes, with per-node deterministic fallback on any failure.
    """
    from faultline.models.types import UfCapability

    tele: dict[str, Any] = {
        "enabled": True,
        "leaves": len(user_flows),
        "capabilities_count": 0,
        "leaves_per_capability_p50": 0,
        "leaves_per_capability_p90": 0,
        "regroup_count": 0,
        "degenerate_count": 0,
        "multi_leaf_count": 0,
        "llm_named_count": 0,
        "orphans": 0,  # always 0 post-validation — asserted by construction
    }
    if not user_flows:
        return [], tele

    nodes = _group_leaves(user_flows)
    nodes, regroups = _validate_and_regroup(nodes, user_flows)
    tele["regroup_count"] = regroups

    # Deterministic names first; ambiguous nodes may be renamed by the hook.
    names = [_template_name(n["resource"], n["family"], n["children"])
             for n in nodes]
    if namer is not None:
        ambiguous = [
            {"idx": i, "resource": n["resource"], "family": n["family"],
             "leaf_names": [c.name for c in n["children"]]}
            for i, n in enumerate(nodes)
            if len(n["children"]) > 1 and n["family"] in _AMBIGUOUS_FAMILIES
        ]
        if ambiguous:
            try:
                proposed = namer(ambiguous) or {}
            except Exception:  # noqa: BLE001 — naming is best-effort
                proposed = {}
            for g in ambiguous:
                name = proposed.get(g["idx"])
                if isinstance(name, str) and name.strip():
                    names[g["idx"]] = name.strip()
                    tele["llm_named_count"] += 1

    caps: list["UfCapability"] = []
    for name, node in zip(names, nodes):
        children = node["children"]
        member_flow_ids: list[str] = []
        seen_m: set[str] = set()
        routes: set[str] = set()
        for c in children:
            for mid in c.member_flow_ids:
                if mid not in seen_m:
                    seen_m.add(mid)
                    member_flow_ids.append(mid)
            routes.update(c.routes or [])
        intent = (node["family"] if len(children) > 1
                  else (children[0].intent or "other"))
        caps.append(UfCapability(
            id="UFC-000",  # provisional — renumbered after the content sort
            name=name,
            intent=intent,
            resource=node["resource"],
            member_uf_ids=[c.id for c in children],
            member_flow_ids=member_flow_ids,
            routes=sorted(routes),
            member_count=len(children),
        ))

    # Content-stable output order + ids (same convention as 6.7d leaves).
    caps.sort(key=lambda c: (c.name.lower(), c.resource, c.member_uf_ids))
    for i, c in enumerate(caps, start=1):
        c.id = f"UFC-{i:03d}"

    sizes = sorted(c.member_count for c in caps)
    tele.update({
        "capabilities_count": len(caps),
        "leaves_per_capability_p50": _percentile(sizes, 0.50),
        "leaves_per_capability_p90": _percentile(sizes, 0.90),
        "degenerate_count": sum(1 for c in caps if c.member_count == 1),
        "multi_leaf_count": sum(1 for c in caps if c.member_count > 1),
    })
    return caps, tele


# ── LLM grouping pass (the spec's "cheap Haiku grouping call") ──────────────
# MEASURED (2026-07-04, mission-92 replays): the deterministic (resource,
# family) grouping alone is a structural NO-OP on 6.7d-abstracted leaves —
# the abstraction already emits ~one leaf per distinct resource (dub 40
# leaves / 40 degenerate nodes, saleor 56/56, supabase 52 leaves → 3 multi
# nodes), so a deterministic-only lattice equals the flat list and cannot
# move coverage. The spec sanctions exactly this fork: "same draw or cheap
# Haiku grouping call over the emitted leaves — architect's choice,
# measured". The LLM pass groups semantically-same-capability leaves that
# carry DIFFERENT resource nouns ("page", "page-type", "delete-page-type"),
# then the DETERMINISTIC post-validation below enforces the spec's
# structural contract (surjectivity, parent/child token consistency,
# reject-and-regroup). On any failure — no key, call error, bad JSON — the
# deterministic lattice above is the never-worse fallback.

GROUPING_MODEL_ENV = "FAULTLINE_UF_LATTICE_MODEL"
DEFAULT_GROUPING_MODEL = "claude-haiku-4-5-20251001"
GROUPING_MAX_TOKENS = 8000
#: bump to invalidate cached groupings when the prompt/build changes.
LATTICE_CACHE_VERSION = "lattice-1"

_GROUPING_SYSTEM = """You organise the user journeys of one software product into product CAPABILITIES.

Rules, in priority order:
 1. Every journey id must appear in EXACTLY ONE capability. Never drop or
    invent ids.
 2. A capability groups journeys that serve the SAME capability of the SAME
    primary resource / feature area: CRUD + browse variants of one resource,
    its settings/configuration, lifecycle steps of one pipeline, list+detail
    views. Example: "Create content pages" + "Bulk delete page types" +
    "Reorder pages" -> one "Manage content pages" capability.
 3. NEVER create broad multi-resource buckets ("Manage everything",
    "Workspace administration", "Platform management"). A capability spans
    ONE resource / feature area. When in doubt, keep journeys separate.
 4. Singleton capabilities (one journey) are normal and common — a journey
    with no true siblings stays alone, keeping its own name.
 5. Each capability: short Title Case verb-phrase `name` grounded in its
    journeys' wording; one lowercase `resource` (the shared primary noun);
    `intent` one of manage|execute|bulk|export|other.

Return STRICT JSON only, no prose:
{"capabilities":[{"name":"...","resource":"...","intent":"manage",
  "member_ids":["UF-001", ...]}, ...]}"""


def _default_client_factory() -> Any | None:  # pragma: no cover - IO
    try:
        from anthropic import Anthropic
    except ImportError:
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return Anthropic()


def _leaf_digest(user_flows: list["UserFlow"]) -> list[dict[str, Any]]:
    return [
        {"id": u.id, "name": u.name, "resource": u.resource, "intent": u.intent}
        for u in user_flows
    ]


def _grouping_cache_key(digest: list[dict[str, Any]], model: str) -> str:
    import hashlib
    import json as _json

    payload = _json.dumps(
        {"v": LATTICE_CACHE_VERSION, "model": model, "leaves": digest},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _llm_group_specs(
    client: Any, model: str, digest: list[dict[str, Any]],
    llm_health: "LlmHealth | None" = None,
) -> tuple[list[dict[str, Any]] | None, int, int]:
    """One grouping call → ``(capability specs | None, in_tok, out_tok)``."""
    import json as _json

    from faultline.llm.cost import deterministic_params
    from faultline.llm.model_gateway import resolve_model as gateway_model

    user = ("User journeys (id, name, resource, intent):\n"
            + _json.dumps(digest, ensure_ascii=False)
            + "\nGroup them into capabilities. Return the JSON now.")
    try:
        msg = client.messages.create(
            model=gateway_model(model), max_tokens=GROUPING_MAX_TOKENS,
            system=_GROUPING_SYSTEM,
            messages=[{"role": "user", "content": user}],
            **deterministic_params(model),
        )
    except Exception as exc:  # noqa: BLE001 — fallback path handles it
        if llm_health is not None and llm_health.record_failure(
            exc, stage="stage_6_7e_uf_lattice",
        ):
            import logging
            logging.getLogger(__name__).error(
                "stage_6_7e: LLM auth failed — skipping remaining calls: %s", exc,
            )
        return None, 0, 0
    if llm_health is not None:
        llm_health.record_success()
    text = "\n".join(t for b in msg.content if (t := getattr(b, "text", None)))
    in_tok = int(getattr(getattr(msg, "usage", None), "input_tokens", 0) or 0)
    out_tok = int(getattr(getattr(msg, "usage", None), "output_tokens", 0) or 0)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None, in_tok, out_tok
    try:
        parsed = _json.loads(text[start:end + 1])
    except (ValueError, _json.JSONDecodeError):
        return None, in_tok, out_tok
    specs = parsed.get("capabilities")
    if not isinstance(specs, list) or not specs:
        return None, in_tok, out_tok
    return specs, in_tok, out_tok


def _tokens_of(*texts: str | None) -> set[str]:
    """Content tokens for the consistency check — same convention as the
    6.7d grounding tokens (nouns matter, glue doesn't)."""
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        _content_tokens,
    )
    return _content_tokens(*texts)


def _build_from_llm_specs(
    specs: list[dict[str, Any]], user_flows: list["UserFlow"],
) -> tuple[list["UfCapability"], dict[str, Any]]:
    """Deterministic reconstruction + the spec's structural post-validation.

    * unknown / duplicate member ids: first claim wins, later dropped;
    * TOKEN CONSISTENCY (parent/child): a child in a multi-leaf node must
      share >=1 content token with the node's name+resource — an
      inconsistent child is EVICTED to its own degenerate node
      (reject-and-regroup, ``regroup_count`` telemetry);
    * ORPHANS (leaves the model never placed): own degenerate node;
    * node intent: the model's when valid, else majority child family;
    * empty / nameless specs are skipped (their members become orphans).
    """
    from faultline.models.types import UfCapability

    by_id = {u.id: u for u in user_flows}
    claimed: set[str] = set()
    regroups = 0
    built: list[tuple[str, str, str, list["UserFlow"]]] = []

    for spec in specs:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        resource = _norm_resource(str(spec.get("resource") or ""))
        members: list["UserFlow"] = []
        for mid in spec.get("member_ids") or []:
            leaf = by_id.get(mid) if isinstance(mid, str) else None
            if leaf is None or leaf.id in claimed:
                continue
            claimed.add(leaf.id)
            members.append(leaf)
        if not members:
            continue
        if len(members) > 1:
            node_tok = _tokens_of(name, resource)
            kept: list["UserFlow"] = []
            for leaf in members:
                if _tokens_of(leaf.name, leaf.resource) & node_tok:
                    kept.append(leaf)
                else:  # evict — parent/child inconsistency; the orphan
                    # pass below re-adds it as a degenerate node
                    regroups += 1
                    claimed.discard(leaf.id)
            members = kept
            if not members:
                continue
        members = sorted(members, key=_leaf_sort_key)
        families = [_family(leaf.intent) for leaf in members]
        intent = str(spec.get("intent") or "").strip().lower()
        if intent not in set(_INTENT_FAMILY.values()):
            intent = max(sorted(set(families)), key=families.count)
        if len(members) == 1:
            intent = members[0].intent or "other"
        built.append((name, resource, intent, members))

    orphans = 0
    for uf in user_flows:
        if uf.id not in claimed:
            orphans += 1
            built.append((
                uf.name,
                _norm_resource(uf.resource),
                uf.intent or "other",
                [uf],
            ))

    caps: list["UfCapability"] = []
    for name, resource, intent, members in built:
        member_flow_ids: list[str] = []
        seen_m: set[str] = set()
        routes: set[str] = set()
        for c in members:
            for mid in c.member_flow_ids:
                if mid not in seen_m:
                    seen_m.add(mid)
                    member_flow_ids.append(mid)
            routes.update(c.routes or [])
        caps.append(UfCapability(
            id="UFC-000",
            name=name,
            intent=intent,
            resource=resource,
            member_uf_ids=[c.id for c in members],
            member_flow_ids=member_flow_ids,
            routes=sorted(routes),
            member_count=len(members),
        ))
    caps.sort(key=lambda c: (c.name.lower(), c.resource, c.member_uf_ids))
    for i, c in enumerate(caps, start=1):
        c.id = f"UFC-{i:03d}"

    tele = {
        "regroup_count": regroups,
        "orphan_rescued": orphans,
    }
    return caps, tele


def build_uf_lattice_llm(
    user_flows: list["UserFlow"],
    *,
    client: Any | None = None,
    model: str | None = None,
    cache: Any | None = None,
    llm_health: "LlmHealth | None" = None,
    _client_factory: Callable[[], Any | None] = _default_client_factory,
) -> tuple[list["UfCapability"], dict[str, Any]]:
    """LLM-grouped lattice with deterministic validation + never-worse
    fallback to :func:`build_uf_lattice`. Content-cached (byte-identical
    re-scan of an unchanged repo at $0)."""
    from faultline.cache.backend import CacheKind
    from faultline.llm.cost import estimate_call_cost

    model = model or os.environ.get(GROUPING_MODEL_ENV, "").strip() or DEFAULT_GROUPING_MODEL

    def _fallback(reason: str) -> tuple[list["UfCapability"], dict[str, Any]]:
        caps, tele = build_uf_lattice(user_flows)
        tele["grouping"] = "deterministic"
        tele["grouping_fallback"] = reason
        return caps, tele

    if not user_flows:
        return _fallback("no_leaves")

    digest = _leaf_digest(user_flows)
    key = _grouping_cache_key(digest, model)
    specs: list[dict[str, Any]] | None = None
    cache_hit = False
    cost = 0.0
    if cache is not None:
        try:
            cached = cache.get(CacheKind.LLM_UF_LATTICE.value, key)
        except Exception:  # noqa: BLE001
            cached = None
        if (isinstance(cached, dict) and cached.get("v") == LATTICE_CACHE_VERSION
                and isinstance(cached.get("capabilities"), list)):
            specs = cached["capabilities"]
            cache_hit = True

    if specs is None:
        # Scan-wide auth kill-switch: after a dead-key failure anywhere in
        # the scan, no further doomed LLM calls — deterministic fallback.
        if llm_health is not None and not llm_health.should_call():
            return _fallback("llm_unhealthy")
        cli = client if client is not None else _client_factory()
        if cli is None:
            return _fallback("no_client")
        specs, in_tok, out_tok = _llm_group_specs(cli, model, digest, llm_health)
        cost = estimate_call_cost(model, in_tok, out_tok) if (in_tok or out_tok) else 0.0
        if specs is None:
            return _fallback("grouping_call_failed")
        if cache is not None:
            try:
                cache.set(CacheKind.LLM_UF_LATTICE.value, key, {
                    "v": LATTICE_CACHE_VERSION, "capabilities": specs,
                })
            except Exception:  # noqa: BLE001
                pass

    caps, vtele = _build_from_llm_specs(specs, user_flows)
    if not caps:
        return _fallback("empty_reconstruction")

    sizes = sorted(c.member_count for c in caps)
    tele: dict[str, Any] = {
        "enabled": True,
        "grouping": "llm",
        "grouping_model": model,
        "cache_hit": cache_hit,
        "cost_usd": round(cost, 6),
        "leaves": len(user_flows),
        "capabilities_count": len(caps),
        "leaves_per_capability_p50": _percentile(sizes, 0.50),
        "leaves_per_capability_p90": _percentile(sizes, 0.90),
        "degenerate_count": sum(1 for c in caps if c.member_count == 1),
        "multi_leaf_count": sum(1 for c in caps if c.member_count > 1),
        "llm_named_count": sum(1 for c in caps if c.member_count > 1),
        "orphans": 0,  # post-validation rescues every orphan
        **vtele,
    }
    return caps, tele


# ── Optional LLM namer (band-revival hook — NOT wired by default) ───────────

_NAMER_SYSTEM = """You name product capability groups. Each item groups several
user journeys of one software product that share a resource and action family.
For EACH item, produce one short Title Case capability name (a verb phrase)
that covers ALL its journeys — grounded in their wording, never broader than
the listed journeys. Return STRICT JSON only:
{"names": {"<idx>": "<name>", ...}}"""


def make_llm_namer(client: Any, *, model: str) -> Namer:
    """A cheap one-call batched namer for the ambiguous class. Failure of the
    call (or of any single item) falls back to the deterministic name — the
    lattice never depends on this hook succeeding."""
    import json as _json

    def _namer(groups: list[dict[str, Any]]) -> dict[int, str] | None:
        payload = _json.dumps(
            [{"idx": g["idx"], "resource": g["resource"],
              "journeys": g["leaf_names"]} for g in groups],
            ensure_ascii=False,
        )
        try:
            msg = client.messages.create(
                model=model, max_tokens=2000, temperature=0,
                system=_NAMER_SYSTEM,
                messages=[{"role": "user", "content": payload}],
            )
            text = "\n".join(
                t for block in msg.content if (t := getattr(block, "text", None))
            )
            start, end = text.find("{"), text.rfind("}")
            raw = _json.loads(text[start:end + 1]).get("names") or {}
            out: dict[int, str] = {}
            for k, v in raw.items():
                try:
                    out[int(k)] = v
                except (TypeError, ValueError):
                    continue
            return out
        except Exception:  # noqa: BLE001 — best-effort by contract
            return None

    return _namer


__all__ = [
    "ENV_FLAG",
    "Namer",
    "build_uf_lattice",
    "build_uf_lattice_llm",
    "lattice_enabled",
    "make_llm_namer",
]
