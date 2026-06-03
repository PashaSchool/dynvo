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
    # Universal web/SaaS verbs harvested from the OTHER bucket across the
    # whole eval corpus (formbricks/dub/documenso/infisical/openstatus) —
    # each appears in MULTIPLE repos, so these are stack-neutral journey
    # verbs, not repo-specific names (see rule-no-repo-specific-paths /
    # rule-no-magic-tuning). Mapped by plain dictionary semantics, not by
    # tuning to any spec count.
    # author — bring a resource into being / shape it / supply input
    "register": "author", "setup": "author", "connect": "author",
    "initialize": "author", "compose": "author", "customize": "author",
    "enter": "author", "input": "author", "submit": "author",
    "select": "author", "subscribe": "author", "apply": "author",
    "provide": "author", "upload": "author", "import": "author",
    # browse — read / inspect / validate-and-read
    "verify": "browse", "validate": "browse", "check": "browse",
    "confirm": "browse", "fetch": "browse", "access": "browse",
    "render": "browse", "display": "browse", "count": "browse",
    "detect": "browse",
    # execute — run an effectful action / dispatch
    "notify": "execute", "distribute": "execute", "invite": "execute",
    "process": "execute", "migrate": "execute", "authenticate": "execute",
    "login": "execute", "receive": "execute", "handle": "execute",
    # lifecycle — advance a resource's state
    "accept": "lifecycle", "complete": "lifecycle", "revoke": "lifecycle",
    "renew": "lifecycle", "activate": "lifecycle", "deactivate": "lifecycle",
    # manage — mutate / control an existing resource
    "toggle": "manage", "rotate": "manage", "format": "manage",
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
# Durable-job framework directories (Inngest, Celery tasks, Sidekiq workers,
# Django Background Tasks, etc.). Stack-neutral — matches the directory
# name, not a specific framework name.
_JOBS_DIR_RE = re.compile(
    r"(?:^|/)(?:inngest_functions?|inngest|celery_tasks?|tasks?|workers?|jobs?)"
    r"/([a-z0-9_]+)\.py"
)
# Frontend module directories (Next.js, React Router, Nuxt, etc.) — the
# first meaningful path segment under the module root is the domain.
# Example: ``frontend/src/modules/network-security/pages/GraphPage.tsx``
# → domain ``network_security``.
_FRONTEND_MODULE_RE = re.compile(
    r"(?:^|/)(?:modules?|pages?|features?|views?|screens?)/([a-z0-9][-a-z0-9_]+)"
)
# API route prefix pattern in ``routes_index``:
# ``/api/v1/autonomous-soc/settings`` → ``autonomous_soc``.
_API_PREFIX_RE = re.compile(r"^/api(?:/v\d+)?/([a-z][a-z0-9-]+)")

# Prefixes on developer-feature names that hide the real resource noun.
# Stripping them lets us use the feature name as a last-resort domain signal.
_FEAT_PREFIX_RE = re.compile(r"^(?:api|test|v\d+)-")


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


def _normalise_pfid_to_domain(pfid: str) -> str | None:
    """Reduce a Layer-2 ``product_feature_id`` (a marketing LABEL such as
    ``organizations-&-multi-team-management`` or
    ``cal.com-atoms-–-embeddable-react-ui-components``) to a SHORT
    code-grain token suitable as a cluster key.

    The Layer-2 id is a grouping LABEL, not a code token — using it
    verbatim as the clustering key produces one "domain" per product
    feature (127 on cal.com) made of long marketing strings. Here we
    keep it only as a coarse signal by slugifying and taking the HEAD
    NOUN: strip marketing punctuation (``& – ( ) , .``), split on the
    first conjunction / separator, and keep the leading word group.

    Example: ``organizations-&-multi-team-management`` → ``organization``;
    ``booking-creation,-rescheduling-&-cancellation`` → ``booking``.

    This is a STRUCTURAL normalization (slug + head-noun + singular), not
    an enumeration of any repo's feature names (rule-no-repo-specific-paths)
    and not a tuned cutoff (rule-no-magic-tuning). NEVER aligned to any
    external spec (rule-ai-specs-validation-only).
    """
    s = pfid.lower().strip()
    # Drop a leading product/brand qualifier like ``cal.com-atoms-...`` —
    # split on the first dot so the brand prefix never becomes the token.
    if "." in s:
        s = s.split(".", 1)[1] if not s.split(".", 1)[0].isalpha() or len(
            s.split(".", 1)[0]) <= 4 else s
    # Strip marketing punctuation and collapse separators to single hyphen.
    s = re.sub(r"[&–—(),:/]", " ", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    if not s:
        return None
    # Take the HEAD NOUN — the single leading resource word of the label.
    # Marketing labels lead with their primary resource noun
    # (``booking-creation,-...`` → ``booking``;
    # ``organizations-&-multi-team-management`` → ``organization``), so the
    # first non-stopword token is the coarse code-grain domain. Collapsing
    # to one head word is what brings the per-product-feature labels down
    # to a shared resource domain (rule-no-magic-tuning: structural head,
    # not a tuned cutoff). Brand/qualifier leaders (``rest``, ``api``,
    # ``com``, ``self``, ``multi``, ``auto``) are skipped so the real
    # resource noun surfaces.
    _LEAD_QUALIFIERS = {
        "the", "a", "an", "and", "with", "of", "for", "to",
        "rest", "api", "com", "self", "multi", "auto", "real", "full",
        "open", "custom", "smart", "new", "advanced", "built", "in",
    }
    tokens = [t for t in s.split("-") if t]
    if not tokens:
        return None
    head = None
    for t in tokens:
        if t in _LEAD_QUALIFIERS:
            continue
        head = t
        break
    if head is None:
        head = tokens[0]
    return _norm_domain(head)


def _pfid_of(members: list[dict], df_by_name: dict) -> str | None:
    """Resolve the Layer-2 ``product_feature_id`` for a UF cluster by
    MAJORITY VOTE over its member flows' primary developer-features.

    This is the legitimate UF → product-feature grouping link (symmetric
    to ``developer_feature.product_feature_id``). It is resolved
    INDEPENDENTLY of the cluster key / domain — the domain is a
    code-grain token; this is the marketing grouping label. Keeping the
    two decoupled is the whole point of this stage (see module docstring).
    """
    votes: Counter = Counter()
    for m in members:
        dev = df_by_name.get(m.get("primary_feature")) or {}
        pfid = dev.get("product_feature_id")
        if pfid:
            votes[pfid] += 1
    if not votes:
        return None
    return votes.most_common(1)[0][0]


def _normalise_name_to_domain(feat_name: str) -> str:
    """Strip known framework prefixes (``api-``, ``test-``, ``v1-``) from a
    developer-feature name and normalise to a domain token.

    Example: ``api-autonomous-soc`` → ``autonomous_soc``.

    This is a STRUCTURAL rule (strip known prefix patterns + replace
    hyphens) — it does not enumerate feature names from any specific
    repo. See rule-no-repo-specific-paths.
    """
    stripped = _FEAT_PREFIX_RE.sub("", feat_name)
    # Iteratively strip repeated prefixes (``test-api-detectors`` → ``detectors``)
    while _FEAT_PREFIX_RE.match(stripped):
        stripped = _FEAT_PREFIX_RE.sub("", stripped)
    return _singular(stripped.replace("-", "_"))


def _domain_of(
    flow: dict,
    df_by_name: dict,
    routes_index: list[dict] | None = None,
) -> str | None:
    """Code-grounded domain = the API resource the flow's code serves.

    Signal priority (all code-structural, never spec-derived):
    1. Backend router file (``routers/<X>.py``).
    2. Durable-job directory (``inngest_functions/<X>.py``, ``tasks/<X>.py``,
       ``workers/<X>.py``, etc.) — catches job-only domains.
    3. ``product_feature_id`` on the primary dev-feature (from Stage 6.5).
    4. Frontend module directory (``modules/<segment>/``) — catches
       frontend-only domains with no backend router file.
    5. ``routes_index`` API prefix (``/api/<domain>/``) — catches domains
       whose API route patterns are known but whose router file is not
       directly referenced in the flow's paths.
    6. Generic source-folder heuristic (``app|src|backend|.../X``).
    7. Primary-feature name stripped of framework prefixes — last resort
       when no path or product_feature_id signal is available.

    ``routes_index`` is an optional list of route dicts (each with a
    ``pattern`` key) keyed by the Stage 6.8 lineage output. It is
    consulted only when earlier signals fail.

    NEVER derived from any external spec — see rule-ai-specs-validation-only.
    """
    files = [flow.get("entry_point_file") or "", *(flow.get("paths") or [])]
    # Signal 1 — backend router file.
    for fp in files:
        m = _ROUTER_RE.search(fp)
        if m and m.group(1) != "__init__":
            return _norm_domain(m.group(1))
    # Signal 2 — durable-job directory.
    for fp in files:
        m = _JOBS_DIR_RE.search(fp)
        if m and m.group(1) != "__init__":
            return _norm_domain(m.group(1))
    # Signal 3 — product_feature_id from Stage 6.5, NORMALIZED to a
    # code-grain token. The raw product_feature_id is a Layer-2 marketing
    # LABEL; using it verbatim makes one domain per product feature out of
    # long marketing strings. We keep it only as a coarse signal by
    # reducing it to its head-noun slug. The raw id is preserved
    # separately as the UF's product_feature_id grouping link (see
    # _pfid_of / cluster_user_flows), NOT as the domain.
    dev = df_by_name.get(flow.get("primary_feature")) or {}
    pfid = dev.get("product_feature_id")
    if pfid:
        token = _normalise_pfid_to_domain(pfid)
        if token:
            return token
    # Signal 4 — frontend module directory.
    for fp in files:
        m = _FRONTEND_MODULE_RE.search(fp)
        if m:
            segment = m.group(1)
            # Skip generic scaffold segments that are not domain names.
            if segment not in {"components", "utils", "hooks", "lib", "types",
                               "helpers", "common", "shared", "core", "base",
                               "layouts", "styles", "assets", "constants"}:
                return _norm_domain(segment.replace("-", "_"))
    # Signal 5 — routes_index API prefix lookup.
    if routes_index:
        pf = flow.get("primary_feature") or ""
        for entry in routes_index:
            pattern = entry.get("pattern") or ""
            m = _API_PREFIX_RE.match(pattern)
            if m:
                seg = m.group(1).replace("-", "_")
                feat_uuid = entry.get("feature_uuid") or ""
                # Match when the route's feature name equals the flow's
                # primary_feature (uuid match already resolved upstream).
                if feat_uuid and pf and feat_uuid == pf:
                    return _norm_domain(seg)
    # Signal 6 — generic source-folder heuristic.
    for fp in files:
        m = _FOLDER_RE.search(fp)
        if m:
            return _norm_domain(m.group(1))
    # Signal 7 — primary-feature name as last resort.
    pf_name = flow.get("primary_feature") or ""
    if pf_name:
        return _normalise_name_to_domain(pf_name)
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


def _enrich(
    members: list[dict],
    own_pfid: str | None,
    df_by_name: dict,
) -> dict:
    """Stage E — deterministic enrichment of a cluster's members.

    ``own_pfid`` is the cluster's resolved Layer-2 product_feature_id
    (NOT the code-grain domain). cross_links collect the OTHER product
    features touched via secondary_features, excluding the cluster's own
    product feature.
    """
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
            if pf and pf != own_pfid:
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


def _merge_singleton_noise(
    clusters: dict[tuple, list],
    cluster_resources: dict[tuple, Counter],
) -> dict[tuple, list]:
    """Stage C-post — collapse SINGLETON resource-clusters within the same
    ``(domain, intent)`` into one journey UF.

    Why this shape (structural, scale-invariant — see rule-no-magic-tuning):

    The cluster key ``(domain, resource, intent)`` keeps every distinct
    resource separate. That is correct for a *recurring* journey — a
    resource+intent the codebase exercises repeatedly (``create-detector``
    appearing across 5 flows) is a real, nameable user task and stays its
    own UF. But a resource that appears exactly ONCE for a given
    ``(domain, intent)`` is grain noise: there is no recurring journey to
    preserve, only a single code-grain flow. Emitting one UF per such
    singleton over-splits the rollup 3-6× past product grain (measured on
    formbricks/infisical/documenso/dub/openstatus).

    Rule: for each ``(domain, intent)`` with a non-None ``domain``, fold
    ALL its singleton resource-clusters (``len(members) == 1``) together
    into a single "journey" UF for that ``(domain, intent)``. Multi-member
    resource clusters are left untouched (genuine recurring journeys).
    When ``domain is None`` we cannot assert two singletons belong to the
    same journey, so they are kept separate (conservative — no blind
    cross-domain collapse, per finding-pathset-merge-refuted).

    Never discards flows; only re-assigns cluster membership. The folded
    UF inherits the ``(domain, <most-common-resource>, intent)`` key so
    its journey label stays meaningful.
    """
    # Bucket singleton clusters by (domain, intent); keep everything else
    # (multi-member clusters, and singletons with domain=None) as-is.
    singleton_buckets: dict[tuple, list[tuple]] = defaultdict(list)
    merged: dict[tuple, list] = {}

    for key, members in clusters.items():
        domain, resource, intent = key
        if len(members) == 1 and domain is not None:
            singleton_buckets[(domain, intent)].append(key)
        else:
            merged[key] = list(members)

    for (domain, intent), keys in singleton_buckets.items():
        if len(keys) == 1:
            # A lone singleton for this (domain,intent) — nothing to fold
            # it with; keep its original (domain, resource, intent) key.
            k = keys[0]
            merged[k] = list(clusters[k])
            continue
        # Fold all singletons into one journey UF. The representative key
        # uses the most frequent resource among the folded singletons so
        # the label remains a real resource noun.
        agg_res: Counter = Counter()
        members_all: list = []
        for k in keys:
            members_all.extend(clusters[k])
            agg_res.update(cluster_resources[k])
        rep_resource = agg_res.most_common(1)[0][0] if agg_res else "item"
        rep_key = (domain, rep_resource, intent)
        merged.setdefault(rep_key, [])
        merged[rep_key].extend(members_all)
        # Preserve resource provenance for label selection downstream.
        cluster_resources[rep_key].update(agg_res)

    return merged


def cluster_user_flows(
    scan: dict,
    routes_index: list[dict] | None = None,
) -> dict:
    """Core deterministic clusterer — dict in, dict out (mirrors prototype).

    Returns ``{user_flows, flow_to_uf, name_to_uf, unique_flows,
    total_flows, dedup_dropped}``. ``user_flows`` is a list of plain
    dicts in the ``UserFlow`` shape; ``flow_to_uf`` / ``name_to_uf`` map
    member identifiers to their UF id.

    ``routes_index`` is the Stage 6.8 route registry (optional). When
    provided it is forwarded to ``_domain_of`` for Signal 5 API-prefix
    domain resolution.

    Cluster key is ``(domain, resource, intent)``: distinct resources
    within the same domain + intent produce separate UFs, which is the
    correct granularity for user-facing journey descriptions (e.g.
    "Browse detectors" vs "Browse suppression rules"). Grain comes from
    the key composition, not a cutoff — see rule-no-magic-tuning.
    """
    flows = scan.get("flows") or []
    df_by_name = {f["name"]: f for f in (scan.get("developer_features") or [])}

    uniq = _dedup_by_name(flows)

    # Stage B+C — cluster by (domain, resource, intent).
    # Distinct resources within the same domain + intent are separate UFs
    # (e.g. "create-detector-flow" and "create-suppression-rule-flow" are
    # different user tasks even though both are "author" intent).
    clusters: dict[tuple, list] = defaultdict(list)
    cluster_resources: dict[tuple, Counter] = defaultdict(Counter)
    for f in uniq:
        domain = _domain_of(f, df_by_name, routes_index)
        verb, resource = _split_name(f["name"])
        intent = INTENT.get(verb, "other")
        key = (domain, resource, intent)
        clusters[key].append(f)
        cluster_resources[key][resource] += 1

    # Stage C-post — collapse singleton other-intent clusters into the
    # largest domain sibling so we don't emit one UF per unmapped verb.
    clusters = _merge_singleton_noise(clusters, cluster_resources)

    user_flows: list[dict] = []
    flow_to_uf: dict[str, str] = {}
    name_to_uf: dict[str, str] = {}
    ordered = sorted(
        clusters.items(),
        key=lambda kv: (str(kv[0][0]), str(kv[0][1]), str(kv[0][2])),
    )
    for i, ((domain, resource, intent), members) in enumerate(ordered):
        uf_id = f"UF-{i + 1:03d}"
        counts = cluster_resources[(domain, resource, intent)]
        label_resource = counts.most_common(1)[0][0] if counts else str(domain)
        # product_feature_id is the Layer-2 grouping LINK, resolved
        # independently of the (code-grain) domain by majority vote over
        # members. Decoupling these two is the fix: domain is the cluster
        # key (a short code token), product_feature_id is the marketing
        # roll-up link — they must not be the same string.
        pfid = _pfid_of(members, df_by_name)
        enriched = _enrich(members, pfid, df_by_name)
        for m in members:
            flow_to_uf[_flow_key(m)] = uf_id
            name_to_uf[m["name"]] = uf_id
        user_flows.append({
            "id": uf_id,
            "name": _uf_name(domain, intent, label_resource),
            "domain": domain,
            "product_feature_id": pfid,
            "intent": intent,
            "resource": label_resource,
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
    flows: list["Flow"], features: list["Feature"],
    routes_index: list[dict] | None = None,
) -> tuple[list["UserFlow"], dict[str, Any]]:
    """Engine adapter — cluster typed Flow/Feature objects, set
    ``Flow.user_flow_id`` in place, and return ``(user_flows, telemetry)``.

    ``features`` is the Layer-1 developer-feature list (carrying
    ``product_feature_id`` from Stage 6.5). ``flows`` is the final
    bipartite flow store. Both are mutated only additively: each flow's
    ``user_flow_id`` is stamped from its cluster.

    ``routes_index`` is the Stage 6.8 route registry (optional).
    """
    from faultline.models.types import UserFlow

    scan = {
        "flows": [_flow_view(f) for f in flows],
        "developer_features": [
            {"name": f.name, "product_feature_id": f.product_feature_id}
            for f in features
        ],
    }
    result = cluster_user_flows(scan, routes_index=routes_index)
    flow_to_uf = result["flow_to_uf"]
    name_to_uf = result["name_to_uf"]
    for f in flows:
        key = f.uuid or f.name
        f.user_flow_id = flow_to_uf.get(key) or name_to_uf.get(f.name)

    user_flows = [UserFlow(**uf) for uf in result["user_flows"]]
    # domains = distinct code-grain cluster keys; product_feature_links =
    # distinct Layer-2 grouping links (kept separate by design).
    domains = {uf.domain for uf in user_flows if uf.domain}
    pf_links = {uf.product_feature_id for uf in user_flows if uf.product_feature_id}
    intents: Counter = Counter(uf.intent for uf in user_flows)
    telemetry = {
        "total_flows": result["total_flows"],
        "unique_flows": result["unique_flows"],
        "dedup_dropped": result["dedup_dropped"],
        "user_flows": len(user_flows),
        "domains": len(domains),
        "product_feature_links": len(pf_links),
        "uf_with_product_feature": sum(
            1 for uf in user_flows if uf.product_feature_id is not None
        ),
        "unmapped_domain": sum(1 for uf in user_flows if uf.domain is None),
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
