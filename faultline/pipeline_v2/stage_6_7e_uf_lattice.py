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
    "lattice_enabled",
    "make_llm_namer",
]
