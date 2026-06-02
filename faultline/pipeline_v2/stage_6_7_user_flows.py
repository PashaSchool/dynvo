"""Stage 6.7 — deterministic Flow → User-Flow (UF) rollup.

Rolls up the code-grain ``flows[]`` of a scan into product-grain
``user_flows[]``, mirroring the existing ``developer_features[]
.product_feature_id → product_features[]`` two-layer model but applied
to flows. Each member flow gets a back-pointer ``Flow.user_flow_id``.

This is a productionization of the validated prototype
``scripts/uf/stage1_cluster.py`` (faultlines-app). The clustering
algorithm is ported unchanged — do NOT retune grain, names, or the
intent table here.

$0 LLM — pure post-processing. Runs after the Layer-2 product
clusterer (Stage 6.5) and the bipartite flow store are populated, so
``product_feature_id`` (the domain) and ``secondary_features`` (the
cross-link signal) already exist. Stage 2 (separate, later) refines UF
names / drafts acceptance criteria via LLM; this stage stays
deterministic and byte-stable.

Spec: faultlines-app/docs/specs/flow-to-user-flow-rollup.md
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from faultline.models.types import Feature, Flow, UserFlow

# verb → intent class. A FIXED semantic table (scale-invariant, not a
# tuned threshold). Unmapped verbs fall to "other". Ported verbatim from
# the prototype — see rule-no-magic-tuning.
INTENT: dict[str, str] = {
    "create": "author", "add": "author", "new": "author", "author": "author",
    "update": "author", "edit": "author", "patch": "author",
    "configure": "author", "set": "author",
    "list": "browse", "view": "browse", "get": "browse", "show": "browse",
    "search": "browse", "filter": "browse", "browse": "browse",
    "inspect": "browse", "retrieve": "browse", "read": "browse",
    "open": "browse", "preview": "browse",
    "approve": "lifecycle", "reject": "lifecycle", "enable": "lifecycle",
    "disable": "lifecycle", "promote": "lifecycle", "publish": "lifecycle",
    "adopt": "lifecycle", "archive": "lifecycle", "resolve": "lifecycle",
    "close": "lifecycle",
    "run": "execute", "trigger": "execute", "execute": "execute",
    "generate": "execute", "send": "execute", "dispatch": "execute",
    "refresh": "execute", "sync": "execute", "revalidate": "execute",
    "rerun": "execute", "schedule": "execute", "monitor": "execute",
    "delete": "manage", "remove": "manage", "reset": "manage",
    "manage": "manage", "track": "manage", "assign": "manage",
    "tag": "manage", "link": "manage",
    "bulk": "bulk",
    "export": "export", "download": "export", "report": "export",
}

# Journey-language name templates, keyed by intent class.
NAME_TMPL: dict[str, str] = {
    "author": "Create & edit {r}",
    "browse": "Browse & filter {r}",
    "lifecycle": "Transition {r} through its lifecycle",
    "execute": "Run {r}",
    "manage": "Manage {r}",
    "bulk": "Bulk-manage {r}",
    "export": "Export {r}",
    "other": "{r}",
}

# Universal FastAPI/Flask convention: a route module ``routers/<X>.py``
# serves resource ``X``. NOT a repo-specific path — see
# rule-no-repo-specific-paths.
_ROUTER_RE = re.compile(r"routers?/([a-z0-9_]+)\.py")
_FOLDER_RE = re.compile(
    r"(?:^|/)(?:app|src|frontend/src|backend|services|jobs)/([a-z0-9_]+)"
)


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("ses") or word.endswith("xes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _split_name(name: str) -> tuple[str, str]:
    """``create-detector-flow`` → ``(verb, resource)``; resource = noun span."""
    base = re.sub(r"-flow$", "", name)
    parts = base.split("-")
    verb = parts[0] if parts else base
    rest = parts[1:] if len(parts) > 1 else []
    resource = "-".join(_singular(p) for p in rest) if rest else "item"
    return verb, resource


def _norm_domain(token: str) -> str:
    """Code-structural normalization only (strip version prefix + plural)
    so ``v1_investigations`` == ``investigations``. Never aligned to any
    external spec — see rule-ai-specs-validation-only."""
    token = re.sub(r"^v\d+_", "", token)
    return _singular(token)


def _domain_of(flow: dict, df_by_name: dict) -> str | None:
    """Code-grounded domain = the API resource the flow's code serves.

    Router file first (skip package ``__init__``), then the
    ``primary_feature``'s dev-feature ``product_feature_id``, then the
    top source folder. NEVER derived from any external spec.
    """
    files = [flow.get("entry_point_file") or "", *(flow.get("paths") or [])]
    for fp in files:
        m = _ROUTER_RE.search(fp)
        if m and m.group(1) != "__init__":
            return _norm_domain(m.group(1))
    dev = df_by_name.get(flow.get("primary_feature")) or {}
    if dev.get("product_feature_id"):
        return dev["product_feature_id"]
    for fp in files:
        m = _FOLDER_RE.search(fp)
        if m:
            return _norm_domain(m.group(1))
    return None


def _dedup_by_name(flows: list[dict]) -> list[dict]:
    """Stage A — dedup by canonical NAME (first-seen wins).

    Duplicate-flow rows share a name but carry distinct uuids, so a
    uuid-keyed dedup would not collapse them (see bug-duplicate-flow-keys).
    """
    seen: dict[str, dict] = {}
    for f in flows:
        key = f.get("name")
        if key not in seen:
            seen[key] = f
    return list(seen.values())


def _flow_key(flow: dict) -> str:
    """Stable member identifier — uuid when present, else name."""
    return flow.get("uuid") or flow["name"]


def _enrich(members: list[dict], domain: str | None, df_by_name: dict) -> dict:
    """Stage E — deterministic enrichment of a cluster's members."""
    routes: set[str] = set()
    cross: set[str] = set()
    tests = 0
    cov: list[float] = []
    for m in members:
        for p in m.get("paths") or []:
            if re.search(r"routers?/", p):
                routes.add(p)
        for sf in m.get("secondary_features") or []:
            dev = df_by_name.get(sf) or {}
            pf = dev.get("product_feature_id")
            if pf and pf != domain:
                cross.add(pf)
        if m.get("test_files"):
            tests += 1
        c = m.get("coverage_pct")
        if isinstance(c, (int, float)):
            cov.append(c)
    return {
        "routes": sorted(routes),
        "cross_links": sorted(cross),
        "ac_draft_count": tests,
        "coverage_pct": round(sum(cov) / len(cov), 1) if cov else None,
    }


def _uf_name(domain: str | None, intent: str, resource: str) -> str:
    """Journey label — domain noun pluralized when present, else resource."""
    label = str(domain).replace("_", " ") + "s" if domain else resource
    return NAME_TMPL[intent].format(r=label)


def cluster_user_flows(scan: dict) -> dict:
    """Core deterministic clusterer — dict in, dict out (mirrors prototype).

    Returns ``{user_flows, flow_to_uf, name_to_uf, unique_flows,
    total_flows, dedup_dropped}``. ``user_flows`` is a list of plain
    dicts in the ``UserFlow`` shape; ``flow_to_uf`` / ``name_to_uf`` map
    member identifiers to their UF id.
    """
    flows = scan.get("flows") or []
    df_by_name = {f["name"]: f for f in (scan.get("developer_features") or [])}

    uniq = _dedup_by_name(flows)

    # Stage B+C — cluster key is (domain, intent): every create/edit
    # endpoint of a domain is the same "author" journey, every list/view
    # the same "browse" journey, etc. The resource noun is the cluster's
    # LABEL, not a split key — splitting per noun collapses to ~1 UF per
    # flow. Grain comes from the key composition, not a cutoff.
    clusters: dict[tuple, list] = defaultdict(list)
    cluster_resources: dict[tuple, Counter] = defaultdict(Counter)
    for f in uniq:
        domain = _domain_of(f, df_by_name)
        verb, resource = _split_name(f["name"])
        intent = INTENT.get(verb, "other")
        clusters[(domain, intent)].append(f)
        cluster_resources[(domain, intent)][resource] += 1

    user_flows: list[dict] = []
    flow_to_uf: dict[str, str] = {}
    name_to_uf: dict[str, str] = {}
    ordered = sorted(clusters.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))
    for i, ((domain, intent), members) in enumerate(ordered):
        uf_id = f"UF-{i + 1:03d}"
        counts = cluster_resources[(domain, intent)]
        resource = counts.most_common(1)[0][0] if counts else str(domain)
        enriched = _enrich(members, domain, df_by_name)
        for m in members:
            flow_to_uf[_flow_key(m)] = uf_id
            name_to_uf[m["name"]] = uf_id
        user_flows.append({
            "id": uf_id,
            "name": _uf_name(domain, intent, resource),
            "product_feature_id": domain,
            "intent": intent,
            "resource": resource,
            "member_flow_ids": [_flow_key(m) for m in members],
            "member_count": len(members),
            **enriched,
            "ui_tier": None,
        })
    return {
        "user_flows": user_flows,
        "flow_to_uf": flow_to_uf,
        "name_to_uf": name_to_uf,
        "unique_flows": len(uniq),
        "total_flows": len(flows),
        "dedup_dropped": len(flows) - len(uniq),
    }


def run_user_flow_rollup(
    flows: list["Flow"], features: list["Feature"]
) -> tuple[list["UserFlow"], dict[str, Any]]:
    """Engine adapter — cluster typed Flow/Feature objects, set
    ``Flow.user_flow_id`` in place, and return ``(user_flows, telemetry)``.

    ``features`` is the Layer-1 developer-feature list (carrying
    ``product_feature_id`` from Stage 6.5). ``flows`` is the final
    bipartite flow store. Both are mutated only additively: each flow's
    ``user_flow_id`` is stamped from its cluster.
    """
    from faultline.models.types import UserFlow

    scan = {
        "flows": [_flow_view(f) for f in flows],
        "developer_features": [
            {"name": f.name, "product_feature_id": f.product_feature_id}
            for f in features
        ],
    }
    result = cluster_user_flows(scan)
    flow_to_uf = result["flow_to_uf"]
    name_to_uf = result["name_to_uf"]
    for f in flows:
        key = f.uuid or f.name
        f.user_flow_id = flow_to_uf.get(key) or name_to_uf.get(f.name)

    user_flows = [UserFlow(**uf) for uf in result["user_flows"]]
    domains = {uf.product_feature_id for uf in user_flows if uf.product_feature_id}
    intents: Counter = Counter(uf.intent for uf in user_flows)
    telemetry = {
        "total_flows": result["total_flows"],
        "unique_flows": result["unique_flows"],
        "dedup_dropped": result["dedup_dropped"],
        "user_flows": len(user_flows),
        "domains": len(domains),
        "unmapped_domain": sum(
            1 for uf in user_flows if uf.product_feature_id is None
        ),
        "by_intent": dict(sorted(intents.items(), key=lambda kv: -kv[1])),
        "uf_with_cross_links": sum(1 for uf in user_flows if uf.cross_links),
    }
    return user_flows, telemetry


def _flow_view(flow: "Flow") -> dict:
    """Minimal dict view of a Flow for the dict-based clusterer."""
    return {
        "name": flow.name,
        "uuid": flow.uuid,
        "entry_point_file": flow.entry_point_file,
        "paths": flow.paths,
        "primary_feature": flow.primary_feature,
        "secondary_features": flow.secondary_features,
        "test_files": flow.test_files,
        "coverage_pct": flow.coverage_pct,
    }


__all__ = [
    "INTENT",
    "NAME_TMPL",
    "cluster_user_flows",
    "run_user_flow_rollup",
]
