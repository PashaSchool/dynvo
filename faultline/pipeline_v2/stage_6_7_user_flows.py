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

import os
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


# ───────────────────────────────────────────────────────────────────────
# Non-journey UF filters (Stage 6.7 rollup ONLY — Layer-1 flows[] untouched)
#
# A User Flow is a PRODUCT-grain user JOURNEY (see flow-feature-concept).
# Shared rendering infrastructure (design-system primitives), per-connector
# plugin packages, and build/DI/normalization artifacts are NOT journeys, so
# they must not SEED a UF. The underlying flows[] and developer_features[]
# are left fully intact at Layer 1 — only `user_flows[]` changes here.
#
# All three filters are STRUCTURAL + scale-invariant (path-segment vocab,
# sibling-count RATIOS, universal infra-token set) — no cal.com paths and no
# magic counts (rule-no-repo-specific-paths, rule-no-magic-tuning). They are
# the UF-rollup siblings of the existing _STRUCTURAL_FEATURE_NAMES /
# _PHANTOM_CLUSTER_NAMES phantom work in Layer 1 / Layer 2.
# ───────────────────────────────────────────────────────────────────────

# Filter A — design-system / UI-primitive package locations. A directory
# segment literally named one of these is a shared rendering-infra package,
# not a product domain. Universal across React/Vue/Svelte design systems.
_PRIMITIVE_DIR_SEGMENTS = frozenset({
    "ui", "components", "component", "design-system", "design_system",
    "primitives", "primitive", "atoms", "atom",
})
# UI-primitive vocabulary — basenames (or barrel-export `index`) that name a
# rendering primitive rather than a feature. Universal HTML/ARIA widget nouns.
_PRIMITIVE_FILE_VOCAB = frozenset({
    "index", "button", "buttons", "badge", "avatar", "dialog", "modal",
    "input", "form", "forms", "checkbox", "radio", "select", "dropdown",
    "tooltip", "popover", "toast", "alert", "card", "icon", "icons",
    "label", "switch", "toggle", "tabs", "table", "list", "menu", "skeleton",
    "spinner", "loader", "divider", "accordion", "slider", "tag", "chip",
    "breadcrumb", "pagination", "drawer", "sheet", "scrollarea", "separator",
    "calendar", "datepicker", "textarea", "combobox", "command",
})

# UI-primitive / widget DOMAIN-TOKEN words. When the resolved domain token is
# built only from these (``atom``, ``component``, ``data_table`` →
# {data, table}, ``form_builder`` → {form, builder}, ``embed``/``embeddable``),
# the "domain" is a rendering-widget package, not a product journey. Universal
# widget vocabulary — same spirit as _PRIMITIVE_FILE_VOCAB, applied to the
# domain token rather than a filename.
_PRIMITIVE_DOMAIN_WORDS = frozenset({
    "atom", "atoms", "component", "components", "primitive", "primitives",
    "widget", "widgets", "data", "table", "datatable", "form", "forms",
    "builder", "embed", "embeddable", "embeds", "icon", "icons", "ui",
    "layout", "layouts", "theme", "themes", "style", "styles",
})

# Filter B — plugin / connector ROOT directory names. When many sibling
# child dirs under one of these each contribute flows, the parent is the
# integration domain, not each connector child.
_PLUGIN_ROOT_SEGMENTS = frozenset({
    "app-store", "app_store", "apps", "integrations", "integration",
    "connectors", "connector", "plugins", "plugin", "extensions",
})
# Canonical journey domain a collapsed plugin-root maps to.
_PLUGIN_DOMAIN = "integration"

# Filter C — universal build / DI / normalization infra tokens. A domain that
# resolves to one of these (or to a single char / pure number / bare version)
# is an extraction artifact, never a user journey. Same spirit as the Layer-1
# _STRUCTURAL_FEATURE_NAMES set — universal, not repo-specific.
_INFRA_DOMAIN_TOKENS = frozenset({
    "di", "dto", "ioc", "container",
    "util", "utils", "lib", "libs", "library", "libraries",
    "config", "configs", "configuration", "settings_config",
    "type", "types", "typing",
    "const", "constant", "constants", "enum", "enums",
    "helper", "helpers", "util_helper",
    "internal", "core", "common", "shared", "base", "misc",
    "prisma", "schema", "orm", "client",
    "key", "keys", "command", "commands", "api", "app", "apps",
    "no", "yes", "automated", "transactional", "platform_library",
    "internationalization", "i18n", "intl", "locale", "locales",
})
_VERSION_TOKEN_RE = re.compile(r"^v\d+$")

# Infra / shared-library / cross-cutting PACKAGE roots. When a flow's PRIMARY
# anchor lives in one of these packages, the flow is shared infrastructure
# (email dispatch, i18n, a barrel library) reused by many real journeys — not
# itself a journey. Universal package-name vocabulary, not repo-specific paths.
_INFRA_PACKAGE_SEGMENTS = frozenset({
    "lib", "libs", "library", "libraries", "utils", "util", "helpers",
    "config", "configs", "types", "typings", "constants", "internal",
    "prisma", "schema", "orm", "db", "database",
    "emails", "email", "mail", "mailer", "sms",
    "i18n", "intl", "internationalization", "locales", "translations",
    "di", "ioc",
})


# Workspace-root / monorepo marker segments that prefix a package path. The
# package-defining segment is the FIRST segment AFTER these markers. Universal
# across pnpm/turbo/nx/cargo/go-workspace layouts — directory names, not paths.
_WORKSPACE_ROOT_SEGMENTS = frozenset({
    "packages", "package", "apps", "app", "src", "lib", "libs", "modules",
    "platform", "internal", "pkg", "crates", "services", "projects",
})


def _path_segments(fp: str) -> list[str]:
    return [s for s in re.split(r"[\\/]+", fp.lower()) if s]


def _package_segments(segs: list[str]) -> list[str]:
    """Strip leading workspace-root markers so the FIRST remaining segment is
    the package/top-level-module name. ``packages/ui/components/form/index.ts``
    → ``['ui', 'components', 'form', 'index.ts']``;
    ``packages/platform/atoms/index.ts`` → ``['atoms', 'index.ts']``.

    Only leading markers are stripped (a workspace marker that appears deep,
    e.g. ``features/calendars/components/``, is NOT a package root)."""
    i = 0
    while i < len(segs) and segs[i] in _WORKSPACE_ROOT_SEGMENTS:
        i += 1
    return segs[i:]


def _is_ui_primitive_flow(flow: dict) -> bool:
    """Filter A — true when the flow's PRIMARY anchor lives in a design-system
    / UI-primitive PACKAGE (the primitive dir segment is the package root,
    shallow), not merely references one deep under a feature.

    We key off the PRIMARY anchor (``entry_point_file``, falling back to the
    first path), never secondary paths: real journeys routinely import shared
    primitives as *secondary* paths (a booking flow that pulls in
    ``packages/ui/components/icon/index.ts``) and a feature dir can legitimately
    nest a ``components/`` folder (``features/calendars/components/DatePicker``).
    The discriminator is SHALLOWNESS — the primitive segment must be the
    package-defining segment (first segment after the workspace-root markers),
    which is the structural signature of a design-system package, not a feature
    that happens to have a components/ subfolder. Structural dir-segment vocab
    only — no repo-specific paths (rule-no-repo-specific-paths).
    """
    anchor = flow.get("entry_point_file") or ""
    if not anchor:
        paths = flow.get("paths") or []
        anchor = paths[0] if paths else ""
    if not anchor:
        return False
    pkg = _package_segments(_path_segments(anchor))
    if len(pkg) < 2:
        return False
    # Shallow check: a primitive segment appears as the package root (idx 0)
    # or immediately inside it (idx 1) — i.e. this is a UI-primitive package,
    # not a feature with a nested components/ subfolder.
    head = pkg[:2]
    return any(seg in _PRIMITIVE_DIR_SEGMENTS for seg in head)


def _plugin_root_of(flow: dict) -> tuple[str, str] | None:
    """Return ``(plugin_root_segment, connector_child)`` when the flow's
    primary anchor lives under a plugin/connector root — i.e. the path
    contains ``<root>/<child>/...`` where ``<root>`` is a plugin-root name.
    Used by Filter B to detect, then collapse, per-connector sibling domains.
    """
    anchor = flow.get("entry_point_file") or ""
    if not anchor:
        paths = flow.get("paths") or []
        anchor = paths[0] if paths else ""
    segs = _path_segments(anchor)
    for i, seg in enumerate(segs[:-1]):
        if seg in _PLUGIN_ROOT_SEGMENTS:
            child = segs[i + 1]
            # The child must be a real connector dir, not a shared `_utils`
            # / `_components` helper folder under the plugin root.
            if child and not child.startswith((".", "_")):
                return seg, child
    return None


def _is_infra_domain(domain: str | None) -> bool:
    """Filter C — true when the resolved domain token is a build/DI/ORM/infra
    artifact, a single character, a pure number, or a bare version token."""
    if not domain:
        return False
    d = domain.strip().lower()
    if not d:
        return False
    if len(d) == 1:
        return True
    if d.isdigit():
        return True
    if _VERSION_TOKEN_RE.match(d):
        return True
    if d in _INFRA_DOMAIN_TOKENS:
        return True
    # Compound token whose HEAD (last underscore segment) is an infra word is
    # itself infra — ``platform_util`` → head ``util``, ``platform_enum`` →
    # head ``enum``. The head noun is the semantic core of the domain token.
    words = [w for w in re.split(r"[_\-]+", d) if w]
    if len(words) >= 2 and words[-1] in _INFRA_DOMAIN_TOKENS:
        return True
    return False


# Lead words that mark a domain token as a UI-component CONTAINER package
# (``components_card``, ``ui_button``, ``atoms_dialog`` …): the lead names the
# design-system package, the tail names the specific widget. Such a domain is
# a rendering-widget package regardless of the tail word.
_PRIMITIVE_CONTAINER_LEADS = frozenset({
    "components", "component", "atoms", "atom", "ui", "primitives",
    "primitive", "widgets", "widget", "designsystem", "design",
})


def _is_primitive_domain(domain: str | None) -> bool:
    """Filter A (domain-token arm) — true when the resolved domain token names
    a rendering-widget package rather than a product journey:

    * its LEAD word is a UI-component container (``components_card``,
      ``ui_button``, ``atoms_dialog``), OR
    * EVERY word is a UI-primitive / widget word (``atom``, ``component``,
      ``data_table`` → {data, table}, ``form_builder`` → {form, builder}).

    The all-words rule is conservative — a real domain that merely contains one
    primitive word (``form_response`` → {form, response}) is NOT dropped because
    ``response`` is not a primitive word and ``form`` is not a container lead."""
    if not domain:
        return False
    words = [w for w in re.split(r"[_\-]+", domain.strip().lower()) if w]
    if not words:
        return False
    if words[0] in _PRIMITIVE_CONTAINER_LEADS and len(words) >= 2:
        return True
    # All-words arm uses the NARROW domain-widget set only. The broader
    # filename vocab (_PRIMITIVE_FILE_VOCAB) deliberately is NOT unioned here:
    # several of its entries (``calendar``, ``table``, ``list``, ``menu``,
    # ``command``, ``tag``) are also legitimate PRODUCT domain nouns, so using
    # them as domain-token drops would filter real journeys (e.g. cal.com's
    # ``calendar``). Keep this arm conservative.
    return all(w in _PRIMITIVE_DOMAIN_WORDS for w in words)


def _is_infra_package_flow(flow: dict) -> bool:
    """Filter C (package-root arm) — true when the flow's PRIMARY anchor lives
    in a shared-infra PACKAGE (email/i18n/lib/prisma/db/...). These flows are
    cross-cutting infrastructure reused by real journeys, not journeys
    themselves. Keyed off the package-root segment (shallow), so a feature that
    merely imports infra deep in its tree is unaffected."""
    anchor = flow.get("entry_point_file") or ""
    if not anchor:
        paths = flow.get("paths") or []
        anchor = paths[0] if paths else ""
    if not anchor:
        return False
    pkg = _package_segments(_path_segments(anchor))
    if len(pkg) < 2:
        return False
    return pkg[0] in _INFRA_PACKAGE_SEGMENTS


def _singular(word: str) -> str:
    """Light singularisation for domain/resource tokens — kept in sync with
    ``naming_validator._singular`` and ``nav_taxonomy._singular``.

    Never strips ``-us`` / ``-is`` / ``-ss`` (status, focus, analysis,
    address are already singular — naive ``-s`` stripping produced
    ``statu`` / ``focu`` that no consumer matches); only collapses ``-es``
    to its stem when the stem is a sibilant (classes→class), so plain words
    keep their ``e`` (cases→case, not cas).
    """
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ss", "us", "is", "ous", "ius")):
        return word
    if word.endswith(("sses", "shes", "ches", "xes", "zzes")):
        return word[:-2]
    if word.endswith("s"):
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


def _detect_plugin_roots(
    flows: list[dict],
    domain_of: dict[int, str | None],
) -> set[str]:
    """Filter B detection (structural, scale-invariant).

    A plugin/connector ROOT is a directory under which MANY distinct sibling
    child dirs each contribute flows — the cal.com ``packages/app-store/<100
    connectors>`` shape, but also Inngest ``integrations/*``, Backstage
    ``plugins/*``, etc. We confirm a candidate root only when its sibling
    children are *predominantly small* per-child domains: the ratio of
    distinct children to total flows under the root is high (each child owns
    only a few flows), which is exactly the over-split signature. A handful of
    large children (a real multi-feature monorepo dir that merely happens to
    be named ``apps``) does NOT trip it.

    Returns the set of plugin-root path-segments to collapse. Thresholds are
    RATIOS over the root's own children (rule-no-magic-tuning): a root must
    have at least a minimal sibling fan-out AND its children must average
    near-singleton flow counts.
    """
    # Gather, per candidate root segment, the children that contribute flows.
    root_children: dict[str, Counter] = defaultdict(Counter)
    for idx, f in enumerate(flows):
        pr = _plugin_root_of(f)
        if pr is None:
            continue
        root, child = pr
        root_children[root][child] += 1

    roots: set[str] = set()
    for root, children in root_children.items():
        n_children = len(children)
        n_flows = sum(children.values())
        if n_children < 2 or n_flows == 0:
            continue
        # Fan-out signature: a plugin root has MANY children relative to its
        # flows (avg flows/child small) — i.e. the over-split is per-connector.
        # A monorepo `apps/` with 2 big apps (web, api) has few children with
        # many flows each → avg high → NOT collapsed.
        avg_flows_per_child = n_flows / n_children
        # Scale-invariant: collapse when the root has a broad sibling fan-out
        # (children are the dominant unit, each near-singleton). "Broad" =
        # children outnumber a small structural floor AND children-per-flow
        # density is high (>= half the flows are distinct children).
        children_ratio = n_children / n_flows
        if n_children >= 3 and avg_flows_per_child <= 8 and children_ratio >= 0.25:
            roots.add(root)
    return roots


def _dedup_by_name(flows: list[dict]) -> list[dict]:
    """Stage A — dedup by (NAME, owning feature), first-seen wins.

    Duplicate-flow rows share a name but carry distinct uuids, so a
    uuid-keyed dedup would not collapse them (see bug-duplicate-flow-keys).
    The key is scoped to the owning ``primary_feature`` because a journey
    name (``reschedule-booking-flow``) legitimately recurs across many
    DISTINCT features in a monorepo — a name-only key collapsed them
    globally (measured on cal.com: 705/1545 flows dropped, all from
    different features, 0 true duplicates), starving the UF rollup. Keying
    on (name, feature) still collapses genuine within-feature duplicate
    rows while letting distinct cross-feature flows survive to be grouped
    by the clusterer. Falls back to name-only when no feature is present.
    """
    seen: dict[tuple, dict] = {}
    for f in flows:
        key = (f.get("name"), f.get("primary_feature"))
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


def _pluralise(word: str) -> str:
    if word.endswith(("s", "x", "z")):
        return word
    if word.endswith("y") and len(word) > 2 and word[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def _primary_member(members: list[dict]) -> dict:
    """The cluster's PRIMARY flow — the highest-confidence member.

    Structural definition: most member paths (broadest grounded reach),
    ties broken by name ascending for determinism. Both name slots
    (verb + resource) are read from THIS flow only — never mixed across
    members (naming-evidence review №5: "Run admins" was verb-from-one
    endpoint + resource-from-another).
    """
    return sorted(
        members,
        key=lambda m: (-len(m.get("paths") or []), m.get("name") or ""),
    )[0]


def _slot_consistent_label(
    members: list[dict],
    product_strings: Any | None = None,
) -> tuple[str, bool]:
    """Resource label for the UF name, taken from ONE member flow.

    Slot-resolution fix (review №5): verb and resource must come from
    the SAME member flow. The cluster intent already classes the verb
    (every member shares it by cluster-key construction); the resource
    label here is derived ONLY from the primary member:

      1. product-string vocabulary of the primary flow's anchor file —
         a nav label / page title is the maintainer's own name for the
         surface (strongest);
      2. the noun span of the primary flow's own name (which Stage 3
         derived from that flow's route path + handler);
      3. last resort: the anchor file's basename stem.

    Returns ``(label, grounded)`` — ``grounded`` is False only on the
    basename last-resort with no product-string vocabulary, which the
    caller surfaces as ``name_confidence="low"``.
    """
    primary = _primary_member(members)
    anchor = primary.get("entry_point_file") or (
        (primary.get("paths") or [None])[0] or "")

    # 1. Nav label / page title on the primary anchor file.
    if product_strings is not None and anchor:
        rows = product_strings.strings_for_file(anchor)
        for row in rows:
            if row.source in ("nav", "title"):
                # Strip the "(href)" context suffix nav entries carry.
                label = re.sub(r"\s*\([^)]*\)\s*$", "", row.text).strip()
                if label:
                    return label, True

    # 2. Noun span of the primary flow's OWN name (same-flow slot).
    _, resource = _split_name(primary.get("name") or "")
    if resource and resource != "item":
        return _pluralise(resource.replace("-", " ")), True

    # 3. Anchor basename stem (weak — flagged low-confidence).
    if anchor:
        base = anchor.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0] if "." in base else base
        stem = re.sub(r"[_\-]+", " ", stem).strip()
        if stem and stem not in ("index", "page", "route", "layout"):
            return _pluralise(stem), False
    return "items", False


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


def _merge_same_name_clusters(
    clusters: dict[tuple, list],
    cluster_resources: dict[tuple, Counter],
) -> dict[tuple, list]:
    """Stage C-post-2 — collapse clusters that would render to the SAME UF
    name into one journey UF.

    Why this shape (structural, scale-invariant — see rule-no-magic-tuning):

    The UF NAME is the user-facing identity of a journey. ``_uf_name`` derives
    it from ``(domain, intent)`` whenever ``domain`` is present (the resource
    is collapsed into the domain noun, e.g. every ``(organization, <resource>,
    browse)`` cluster renders to "Browse & filter organizations"). So multiple
    multi-member ``(domain, resource, intent)`` clusters that share the same
    ``(domain, intent)`` emit DIFFERENT cluster keys but the SAME human name —
    e.g. cal.com's organizations product feature showed 11 separate
    "Browse & filter organizations" UFs. Two UFs a user cannot tell apart
    (identical name, same domain) are by definition one journey at product
    grain, so they must be a single UF.

    ``_merge_singleton_noise`` only folds 1-member clusters, so these
    multi-member name-collisions survive it. This pass closes that gap by
    keying directly on the NAME-DETERMINANT signature ``(domain, intent,
    name)`` rather than on member count: every cluster that yields the same
    rendered name is folded together. Distinct intents render distinct names
    (``Browse …`` vs ``Create & edit …``) so they are never merged — the pass
    cannot collapse genuinely different journeys.

    When ``domain is None`` the name falls back to the per-cluster ``resource``
    (``_uf_name`` then uses the resource label), so two such clusters only
    collide when their resource labels are already identical — folding them is
    still correct (same name = same journey). The conservative no-blind-merge
    spirit of finding-pathset-merge-refuted is preserved: we merge ONLY on an
    already-identical rendered name, never across differing names.

    Never discards flows; only re-assigns cluster membership. Recall-safe by
    construction — every member flow keeps a UF id.
    """
    name_buckets: dict[tuple, list[tuple]] = defaultdict(list)
    for key in clusters:
        domain, resource, intent = key
        counts = cluster_resources.get(key)
        label_resource = (
            counts.most_common(1)[0][0] if counts else (resource or str(domain))
        )
        name = _uf_name(domain, intent, label_resource)
        # Scope the merge to one journey identity: same rendered NAME AND same
        # code-grain domain. Keeping domain in the signature stops a None-domain
        # resource label from ever colliding with a real domain's label.
        name_buckets[(domain, intent, name)].append(key)

    merged: dict[tuple, list] = {}
    for (domain, intent, _name), keys in name_buckets.items():
        if len(keys) == 1:
            k = keys[0]
            merged[k] = list(clusters[k])
            continue
        agg_res: Counter = Counter()
        members_all: list = []
        for k in keys:
            members_all.extend(clusters[k])
            agg_res.update(cluster_resources[k])
        rep_resource = agg_res.most_common(1)[0][0] if agg_res else "item"
        rep_key = (domain, rep_resource, intent)
        merged.setdefault(rep_key, [])
        merged[rep_key].extend(members_all)
        cluster_resources[rep_key].update(agg_res)

    return merged


def _member_trigger(member: dict, file_trigger: dict[str, str]) -> str | None:
    """System trigger of a member flow via its entry file / paths, else None.

    A flow is system-triggered when the route it is anchored on was tagged
    scheduled/queue/webhook by Stage 6.8b. ``entry_point_file`` is the primary
    anchor; ``paths`` is the fallback for flows without an explicit entry.
    """
    ept = member.get("entry_point_file")
    if ept and ept in file_trigger:
        return file_trigger[ept]
    for p in (member.get("paths") or []):
        if p in file_trigger:
            return file_trigger[p]
    return None


# Framework / plumbing path segments dropped when naming a flow-less system
# route, so sibling routes of ONE journey collapse to the same resource
# (``cron/automation-jobs`` + ``automation-jobs/execute`` → ``automation-jobs``).
_SYS_ROUTE_DROP = frozenset({
    "api", "cron", "route", "all", "execute", "simple", "queue", "batch",
    "jobs", "job", "tasks", "task", "workers", "worker", "v1", "v2",
})


def _system_route_resource(file_path: str, pattern: str) -> str:
    """Resource slug (journey noun) for a system route that has no flow.

    Keeps a provider prefix on webhook/event handlers (``google/webhook`` →
    ``google-webhook``) so distinct integrations stay distinct; otherwise the
    last meaningful segment names the journey.
    """
    src = pattern or file_path.replace("apps/web/app/", "")
    raw = [s for s in src.split("/") if s]
    if raw and "." in raw[-1]:           # strip a trailing filename (route.ts)
        raw = raw[:-1]
    segs = [
        s for s in raw
        if s not in _SYS_ROUTE_DROP and not (s.startswith("(") and s.endswith(")"))
    ]
    if not segs:
        return "job"
    if segs[-1] in ("webhook", "webhooks", "events", "event", "watch") and len(segs) >= 2:
        return f"{segs[-2]}-{segs[-1]}"
    return segs[-1]


def _flowless_system_groups(
    anchored: set[str],
    dev_paths: list[tuple[str, list]],
    routes_index: list[dict] | None,
) -> dict[str, dict[str, Any]]:
    """Group flow-LESS system surfaces by journey resource.

    Two channels (the Stage 6.7d in-rollup synthesis AND the W3.2
    post-6.7d re-mint share THIS collector, so both paths agree
    byte-for-byte):

      * routes_index entries whose Stage 6.8b ``trigger`` is a system
        class and whose file no real flow covers;
      * BACKGROUND-JOB files (inngest / celery / tasks / workers) from
        developer-feature paths — these are NOT routes, so the route
        loop misses them entirely (Soc0's ``backend/inngest_functions/
        *.py`` jobs: 11 files, 0 flows, 0 UFs — the D9 canonical
        target).

    Returns ``resource → {"trig": Counter, "routes": set, "files":
    set}`` (``files`` carries the matched repo files so the post-6.7d
    re-mint can attribute a PF home from file ownership).
    """
    groups: dict[str, dict[str, Any]] = {}
    for r in (routes_index or []):
        trig = r.get("trigger")
        fp = str(r.get("file") or "")
        if not fp or not trig or trig == "interactive" or fp in anchored:
            continue  # interactive, or a real flow already covers this route
        resource = _system_route_resource(fp, str(r.get("pattern") or ""))
        g = groups.setdefault(
            resource, {"trig": Counter(), "routes": set(), "files": set()})
        g["trig"][trig] += 1
        g["routes"].add(str(r.get("pattern") or fp))
        g["files"].add(fp)
    for _name, paths in dev_paths:
        for p in paths:
            if not isinstance(p, str) or p in anchored or "test" in p.lower():
                continue
            m = _JOBS_DIR_RE.search(p)
            if not m:
                continue
            resource = m.group(1)
            if resource in ("__init__", "init", "base", "utils", "helpers",
                            "main"):
                continue
            g = groups.setdefault(
                resource, {"trig": Counter(), "routes": set(), "files": set()})
            g["trig"]["queue"] += 1
            g["routes"].add(p)
            g["files"].add(p)
    return groups


#: Tag on synthesized flow-less system journeys (verifier-reviewable;
#: eval scorers exclude synthesized UFs by tag — see the UserFlow model).
SYSTEM_RECALL_REASON = "system_flow_recall"


def _hub_of(
    anchor: str,
    hub_dirs: list[tuple[str, str]] | None,
) -> tuple[str, str | None] | None:
    """Product-Spine §4.4 — ``(hub_domain, vendor_child|None)`` when the
    flow's primary anchor lives under a detected connector hub, else
    ``None``. ``hub_dirs`` is ``[(hub_dir, hub_key), …]`` (longest dir
    wins when nested)."""
    if not hub_dirs or not anchor:
        return None
    norm = anchor.replace("\\", "/").strip("/")
    best: tuple[str, str] | None = None
    for hub_dir, hub_key in hub_dirs:
        prefix = hub_dir + "/"
        if norm.startswith(prefix) and (
            best is None or len(hub_dir) > len(best[0])
        ):
            best = (hub_dir, hub_key)
    if best is None:
        return None
    from faultline.pipeline_v2.hub_relation import vendor_of_segment

    child = norm[len(best[0]) + 1:].split("/", 1)[0]
    vendor = vendor_of_segment(child) if child else None
    domain = _norm_domain(best[1].replace("-", "_"))
    return domain, vendor


def cluster_user_flows(
    scan: dict,
    routes_index: list[dict] | None = None,
    product_strings: Any | None = None,
    hub_dirs: list[tuple[str, str]] | None = None,
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

    # Product-Spine §4.5 — conservation law (construction-time). The
    # majority-vote pfid below is checked against the members' spans /
    # entries over the REAL product features' file ownership; violators
    # resettle to the actual majority owner (never Shared Platform).
    # Kill-switch: FAULTLINE_SPINE_CONSERVATION=0.
    from faultline.pipeline_v2.conservation import (
        build_file_pf_owner,
        conservation_enabled,
        conserved_pfid,
    )

    _conserve = conservation_enabled()
    _file_pf_owner = (
        build_file_pf_owner(scan.get("developer_features") or [])
        if _conserve else {}
    )
    _conservation_resettled = 0

    # file -> system trigger (scheduled|queue|webhook), from the Stage 6.8b
    # route classification. Interactive routes are omitted, so a lookup miss
    # means "interactive". Lets each UF inherit system/background status from
    # the routes its member flows are anchored on.
    file_trigger = {
        str(r.get("file")): str(r.get("trigger") or "")
        for r in (routes_index or [])
        if r.get("file") and r.get("trigger") and r.get("trigger") != "interactive"
    }

    uniq = _dedup_by_name(flows)

    # Pre-pass — resolve every surviving flow's code-grain domain ONCE so the
    # plugin-root detector (Filter B) can see the global sibling fan-out.
    domain_of: dict[int, str | None] = {
        idx: _domain_of(f, df_by_name, routes_index) for idx, f in enumerate(uniq)
    }
    plugin_roots = _detect_plugin_roots(uniq, domain_of)

    # Stage B+C — cluster by (domain, resource, intent), applying the three
    # non-journey UF filters. EXCLUDED flows are still in flows[] (Layer 1 is
    # untouched); they simply do not SEED a user_flow.
    #   A. UI-primitive flows (design-system rendering infra) → excluded.
    #   B. Per-connector plugin flows → domain folded to the plugin root.
    #   C. Infra/DI/version/single-char/numeric artifact domains → excluded.
    # Distinct resources within the same domain + intent are separate UFs
    # (e.g. "create-detector-flow" and "create-suppression-rule-flow" are
    # different user tasks even though both are "author" intent).
    clusters: dict[tuple, list] = defaultdict(list)
    cluster_resources: dict[tuple, Counter] = defaultdict(Counter)
    # System/background-flow clusters live in a PARALLEL namespace so a system
    # journey (cron / queue / webhook) never merges into a same-domain
    # interactive UF — a billing webhook is a different journey from the billing
    # settings page. The interactive clustering path below stays unchanged.
    sys_clusters: dict[tuple, list] = defaultdict(list)
    sys_cluster_resources: dict[tuple, Counter] = defaultdict(Counter)
    excluded = {"ui_primitive": 0, "infra_domain": 0}
    plugin_collapsed = 0
    # Product-Spine §4.4 pre-pass — flows per (hub, vendor): a vendor child
    # with RECURRING flows earns its own journey space (vendor-qualified
    # domain below); single-flow vendors fold into the hub journey.
    hub_vendor_flows: Counter = Counter()
    if hub_dirs:
        for f in uniq:
            anchor0 = f.get("entry_point_file") or (
                (f.get("paths") or [None])[0] or "")
            hit0 = _hub_of(anchor0, hub_dirs)
            if hit0 is not None and hit0[1] is not None:
                hub_vendor_flows[hit0] += 1

    hub_clustered = 0
    for idx, f in enumerate(uniq):
        domain = domain_of[idx]
        anchor = f.get("entry_point_file") or (
            (f.get("paths") or [None])[0] or "")
        # Product-Spine §4.4 (precedence over Filter B) — a flow anchored
        # under an EXPLICIT connector hub clusters against the hub relation:
        # a vendor child with >= 2 flows gets a VENDOR-QUALIFIED domain
        # (``edr_crowdstrike``) → its own per-vendor journey; single-flow
        # vendors and shared hub plumbing keep the hub domain and fold into
        # one hub journey via the singleton-noise merge below. The hub
        # relation (not the statistical plugin-root collapse) is the source
        # of truth where it fires.
        hub_hit = _hub_of(anchor, hub_dirs)
        if hub_hit is not None:
            hub_domain, vendor = hub_hit
            verb, resource = _split_name(f["name"])
            intent = INTENT.get(verb, "other")
            if vendor is not None and hub_vendor_flows[hub_hit] >= 2:
                key: tuple[str | None, str, str] = (
                    f"{hub_domain}_{vendor}", vendor, intent)
            else:
                key = (hub_domain, vendor or resource, intent)
            clusters[key].append(f)
            cluster_resources[key][vendor or resource] += 1
            hub_clustered += 1
            continue
        # Filter B (precedence) — collapse per-connector sibling domains into
        # one integration journey when the flow sits under a detected plugin
        # root. Shared plugin-root helper dirs (``app-store/_utils``) also fold.
        in_plugin_root = any(
            seg in plugin_roots for seg in _path_segments(anchor)
        )
        if in_plugin_root:
            verb, resource = _split_name(f["name"])
            intent = INTENT.get(verb, "other")
            key = (_PLUGIN_DOMAIN, resource, intent)
            clusters[key].append(f)
            cluster_resources[key][resource] += 1
            plugin_collapsed += 1
            continue
        # System/background-flow routing (Stage 6.8b) — a flow anchored on a
        # scheduled / queue / webhook route is a real SYSTEM journey (a cron job,
        # a queue consumer, an inbound webhook). It goes into the PARALLEL system
        # cluster namespace, which (1) bypasses the infra-domain / UI-primitive
        # filters below (a cron handler under api/ would otherwise be dropped as
        # noise), and (2) keeps it from merging with a same-domain INTERACTIVE
        # journey. Weak path domains fall back to the resource / trigger so the
        # journey still names sensibly.
        if _member_trigger(f, file_trigger) is not None:
            verb, resource = _split_name(f["name"])
            intent = INTENT.get(verb, "other")
            sys_domain = (
                domain
                if domain and not _is_infra_domain(domain)
                and not _is_primitive_domain(domain)
                else (resource or _member_trigger(f, file_trigger))
            )
            key = (sys_domain, resource, intent)
            sys_clusters[key].append(f)
            sys_cluster_resources[key][resource] += 1
            continue
        # A domain is STRONG when it is a real product noun — not None, not a
        # primitive widget token, not an infra/version/artifact token. A strong
        # domain means the flow was grounded to a real product surface (usually
        # via its pfid), so an incidental structural anchor (a barrel file, a
        # top-level components/ file) must NOT drag the journey out. Anchor-based
        # exclusions (primitive package, infra package) fire only on WEAK
        # domains; domain-TOKEN exclusions always fire (they target the domain
        # itself). This is the load-bearing "never filter a real journey" guard.
        domain_strong = (
            domain is not None
            and not _is_primitive_domain(domain)
            and not _is_infra_domain(domain)
        )
        # Filter A — UI-component-library rendering infra. Primitive-only domain
        # token always excludes; primitive PACKAGE anchor excludes only when the
        # domain is weak.
        if _is_primitive_domain(domain) or (
            not domain_strong and _is_ui_primitive_flow(f)
        ):
            excluded["ui_primitive"] += 1
            continue
        # Filter C — infra / DI / version / numeric / artifact DOMAIN tokens are
        # never journeys (always excludes).
        if _is_infra_domain(domain):
            excluded["infra_domain"] += 1
            continue
        # Filter C (infra-package arm) — a shared-infra package anchor with NO
        # strong product domain signal is pure infrastructure.
        if not domain_strong and _is_infra_package_flow(f):
            excluded["infra_domain"] += 1
            continue
        verb, resource = _split_name(f["name"])
        intent = INTENT.get(verb, "other")
        key = (domain, resource, intent)
        clusters[key].append(f)
        cluster_resources[key][resource] += 1

    # Stage C-post — collapse singleton other-intent clusters into the
    # largest domain sibling so we don't emit one UF per unmapped verb.
    clusters = _merge_singleton_noise(clusters, cluster_resources)
    # Stage C-post-2 — collapse multi-member clusters that would render to the
    # SAME UF name (same domain + intent) into one journey. Closes the
    # name-collision gap _merge_singleton_noise leaves for multi-member
    # clusters (e.g. 11× "Browse & filter organizations" on cal.com).
    clusters = _merge_same_name_clusters(clusters, cluster_resources)
    # Same singleton / name-collision collapse for the system namespace (kept
    # separate so a system journey is never folded into an interactive one).
    sys_clusters = _merge_singleton_noise(sys_clusters, sys_cluster_resources)
    sys_clusters = _merge_same_name_clusters(sys_clusters, sys_cluster_resources)

    user_flows: list[dict] = []
    flow_to_uf: dict[str, str] = {}
    name_to_uf: dict[str, str] = {}
    # Emit interactive clusters first, then the parallel system clusters; each
    # section stamps its own ``category``. UF ids run sequentially across both.
    uf_seq = 0
    for section_clusters, section_resources, section_category in (
        (clusters, cluster_resources, "interactive"),
        (sys_clusters, sys_cluster_resources, "system"),
    ):
        ordered = sorted(
            section_clusters.items(),
            key=lambda kv: (str(kv[0][0]), str(kv[0][1]), str(kv[0][2])),
        )
        for (domain, resource, intent), members in ordered:
            uf_seq += 1
            uf_id = f"UF-{uf_seq:03d}"
            counts = section_resources[(domain, resource, intent)]
            label_resource = counts.most_common(1)[0][0] if counts else str(domain)
            # product_feature_id is the Layer-2 grouping LINK, resolved
            # independently of the (code-grain) domain by majority vote over
            # members. domain is the cluster key (a short code token);
            # product_feature_id is the marketing roll-up link — never the same.
            pfid = _pfid_of(members, df_by_name)
            if _conserve:
                # §4.5 — the vote is only a CANDIDATE: entries + span
                # majority must lie inside the chosen PF's dev closure,
                # else the journey resettles to the real majority owner.
                pfid, _moved = conserved_pfid(members, _file_pf_owner, pfid)
                if _moved:
                    _conservation_resettled += 1
            enriched = _enrich(members, pfid, df_by_name)
            for m in members:
                flow_to_uf[_flow_key(m)] = uf_id
                name_to_uf[m["name"]] = uf_id
            # Slot-resolution fix (naming review №5) — the RENDERED name takes
            # both slots from the SAME primary member flow (verb class via the
            # shared cluster intent, resource label via _slot_consistent_label),
            # never the cross-member domain vote.
            slot_label, slot_grounded = _slot_consistent_label(
                members, product_strings,
            )
            # System UFs carry the dominant member trigger as their sub-type.
            uf_trigger: str | None = None
            if section_category == "system":
                trigs = [
                    t for t in (_member_trigger(m, file_trigger) for m in members) if t
                ]
                if trigs:
                    uf_trigger = Counter(trigs).most_common(1)[0][0]
            user_flows.append({
                "id": uf_id,
                "name": NAME_TMPL[intent].format(r=slot_label),
                "name_confidence": "high" if slot_grounded else "low",
                "domain": domain,
                "product_feature_id": pfid,
                "intent": intent,
                "resource": label_resource,
                "member_flow_ids": [_flow_key(m) for m in members],
                "member_count": len(members),
                **enriched,
                "ui_tier": None,
                "category": section_category,
                "trigger": uf_trigger,
            })

    # ── Stage 6.7d (env-gated) — synthesise THIN system UFs for system routes
    # that Stage 3 left with NO flow. OUTPUT-ONLY: appends to user_flows[] and
    # NEVER touches flows[] / edges / features, so the flow graph (coverage,
    # symbols, health) is byte-identical and zero-risk. member_flow_ids=[] —
    # the 6.7b refiner and 6.95 history both no-op on empty members. Surfaces
    # background jobs (webhooks / crons / queues) that Stage 3's flow gate drops,
    # which would otherwise be invisible journeys. Kill-switch:
    # FAULTLINE_SEED_SYSTEM_UFS=0.
    if os.environ.get("FAULTLINE_SEED_SYSTEM_UFS", "1") != "0":
        anchored = {
            p
            for fl in flows
            for p in [fl.get("entry_point_file"), *(fl.get("paths") or [])]
            if p
        }
        groups = _flowless_system_groups(
            anchored,
            [(str(df.get("name") or ""), list(df.get("paths") or []))
             for df in (scan.get("developer_features") or [])],
            routes_index,
        )
        for resource, g in sorted(groups.items()):
            uf_seq += 1
            user_flows.append({
                "id": f"UF-{uf_seq:03d}",
                "name": NAME_TMPL["execute"].format(r=resource.replace("-", " ")),
                "name_confidence": "low",
                "domain": resource,
                "product_feature_id": None,
                "intent": "execute",
                "resource": resource,
                "member_flow_ids": [],
                "member_count": 0,
                "routes": sorted(g["routes"]),
                "ui_tier": "no-ui",
                "category": "system",
                "trigger": g["trig"].most_common(1)[0][0],
                # W3.2 D9 — verifier-reviewable tags (same contract as the
                # route-group seeds; eval excludes synthesized UFs by tag).
                "synthesized": True,
                "synthesis_reason": SYSTEM_RECALL_REASON,
            })
    return {
        "user_flows": user_flows,
        "flow_to_uf": flow_to_uf,
        "name_to_uf": name_to_uf,
        "unique_flows": len(uniq),
        "total_flows": len(flows),
        "dedup_dropped": len(flows) - len(uniq),
        "uf_filtered_ui_primitive": excluded["ui_primitive"],
        "uf_filtered_infra_domain": excluded["infra_domain"],
        "uf_plugin_collapsed": plugin_collapsed,
        "uf_plugin_roots": sorted(plugin_roots),
        "uf_hub_clustered": hub_clustered,
        "uf_hub_dirs": sorted(d for d, _ in (hub_dirs or [])),
        "uf_conservation_resettled": _conservation_resettled,
    }


def run_user_flow_rollup(
    flows: list["Flow"], features: list["Feature"],
    routes_index: list[dict] | None = None,
    product_strings: Any | None = None,
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
    from faultline.pipeline_v2.hub_relation import detect_hub_relations

    scan = {
        "flows": [_flow_view(f) for f in flows],
        "developer_features": [
            {"name": f.name, "product_feature_id": f.product_feature_id,
             "paths": list(f.paths),  # paths → Stage 6.7d job-file synthesis
             "role": getattr(f, "role", None)}  # facet marker (spine §4.1)
            for f in features
        ],
    }
    # Product-Spine §4.4 — the SAME hub relation the PF layer binds with
    # drives per-vendor journey clustering (replaces the statistical
    # plugin-root collapse wherever an explicit hub fires).
    hub_dirs = [
        (h.hub_dir, h.hub_key) for h in detect_hub_relations(features)
    ]
    result = cluster_user_flows(
        scan, routes_index=routes_index, product_strings=product_strings,
        hub_dirs=hub_dirs,
    )
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
        "uf_filtered_ui_primitive": result.get("uf_filtered_ui_primitive", 0),
        "uf_filtered_infra_domain": result.get("uf_filtered_infra_domain", 0),
        "uf_plugin_collapsed": result.get("uf_plugin_collapsed", 0),
        "uf_plugin_roots": result.get("uf_plugin_roots", []),
        # Product-Spine Wave 1 telemetry (§4.4 hub consumption + §4.5
        # construction-time conservation).
        "uf_hub_clustered": result.get("uf_hub_clustered", 0),
        "uf_hub_dirs": result.get("uf_hub_dirs", []),
        "uf_conservation_resettled": result.get(
            "uf_conservation_resettled", 0),
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
        # §4.5 conservation — span-majority voting reads the flow's own
        # line ranges (file-count fallback when a flow carries none).
        "line_ranges": [
            {"path": lr.path, "start_line": lr.start_line,
             "end_line": lr.end_line}
            for lr in (flow.line_ranges or [])
        ],
    }


# ── W3.2 D9 — the flow-less system mint SURVIVES the keyed path ─────────
#
# wave31 (keyed, 2026-07-07): route classification stamped
# scan_meta.system_flow_routes on 6/10 repos, yet system-category UFs in
# output = 0 CORPUS-WIDE. Root cause: the in-rollup synthesis above DOES
# fire, but Stage 6.7d's ``_finish`` rebuilds user_flows[] from the LLM's
# journey specs — thin member-less system UFs are not journeys the LLM
# re-emits, so the rewrite eats every one of them (route-group seeds
# survive for exactly one reason: they are appended in phase_finalize
# AFTER 6.7d). The two functions below give the D9 contract the same
# post-6.7d slot: re-mint what the rewrite dropped (dedup-aware, so the
# keyless path — where the rollup UFs survive verbatim — is a no-op),
# and re-stamp the deterministic trigger verdicts onto rebuilt journeys
# whose member flows ride system routes (w31x: tracecat's 10 schedule-*
# flows landed in UFs carrying trigger=None).


def resynthesize_system_ufs(
    user_flows: list["UserFlow"],
    flows: list["Flow"],
    features: list["Feature"],
    routes_index: list[dict] | None,
) -> dict[str, Any]:
    """Append flow-less system UFs missing from *user_flows* (in place).

    Same collector, gates and naming as the in-rollup synthesis; a
    resource already present as a system UF is skipped, so running this
    after a pipeline that KEPT the rollup output changes nothing.
    PF home: when every matched file of a group is primary-owned by
    devs of ONE product feature, the seed cites it (binding low);
    otherwise the journey stays an honest orphan (pfid None — lane-owned
    job files have no product home until Wave-4 tells their story).
    Kill-switch: shared with the rollup (``FAULTLINE_SEED_SYSTEM_UFS``).
    """
    tele: dict[str, Any] = {"enabled": True, "minted": 0, "skipped_existing": 0,
                            "seeds": []}
    if os.environ.get("FAULTLINE_SEED_SYSTEM_UFS", "1") == "0":
        tele["enabled"] = False
        return tele
    anchored = {
        str(p)
        for fl in (flows or [])
        for p in [getattr(fl, "entry_point_file", None),
                  *(getattr(fl, "paths", None) or [])]
        if p
    }
    dev_paths = [
        (str(getattr(f, "name", "") or ""),
         [str(p) for p in (getattr(f, "paths", None) or [])])
        for f in (features or [])
        if getattr(f, "layer", "developer") == "developer"
    ]
    groups = _flowless_system_groups(anchored, dev_paths, routes_index)
    if not groups:
        return tele
    existing = {
        str(getattr(uf, "resource", "") or "")
        for uf in user_flows
        if getattr(uf, "category", None) == "system"
    }
    existing_names = {
        (getattr(uf, "name", "") or "").strip().lower() for uf in user_flows
    }
    max_id = 0
    for uf in user_flows:
        m = re.match(r"^UF-(\d+)$", str(getattr(uf, "id", "") or ""))
        if m:
            max_id = max(max_id, int(m.group(1)))
    # file → owning dev's pfid (primary ownership channel; first dev wins
    # deterministically by features[] order, same as the validator's
    # path_index convention).
    file_pfid: dict[str, str | None] = {}
    for f in (features or []):
        if getattr(f, "layer", "developer") != "developer":
            continue
        pfid = getattr(f, "product_feature_id", None)
        for p in (getattr(f, "paths", None) or []):
            file_pfid.setdefault(str(p), pfid)

    from faultline.models.types import UserFlow

    for resource, g in sorted(groups.items()):
        if resource in existing:
            tele["skipped_existing"] += 1
            continue
        name = NAME_TMPL["execute"].format(r=resource.replace("-", " "))
        if name.strip().lower() in existing_names:
            tele["skipped_existing"] += 1
            continue
        owners = {file_pfid.get(fp) for fp in g["files"]}
        pf_home = owners.pop() if (len(owners) == 1 and None not in owners) else None
        max_id += 1
        user_flows.append(UserFlow(
            id=f"UF-{max_id:03d}",
            name=name,
            name_confidence="low",
            domain=resource,
            product_feature_id=pf_home,
            intent="execute",
            resource=resource,
            member_flow_ids=[],
            member_count=0,
            routes=sorted(g["routes"]),
            ui_tier="no-ui",
            category="system",
            trigger=g["trig"].most_common(1)[0][0],
            synthesized=True,
            synthesis_reason=SYSTEM_RECALL_REASON,
            binding_confidence="low" if pf_home else None,
        ))
        existing_names.add(name.strip().lower())
        tele["minted"] += 1
        if len(tele["seeds"]) < 25:
            tele["seeds"].append({"resource": resource, "pf": pf_home,
                                  "trigger": g["trig"].most_common(1)[0][0],
                                  "routes": len(g["routes"])})
    return tele


def restamp_system_triggers(
    user_flows: list["UserFlow"],
    flows: list["Flow"],
    routes_index: list[dict] | None,
) -> dict[str, Any]:
    """Stamp ``trigger``/``category`` on journeys whose member flows ride
    system routes (in place) — the 6.8b → UF wiring the keyed rewrite
    loses.

    Conservative by design: a journey is re-stamped ONLY when every
    member flow with a resolvable route verdict is system-triggered
    (mixed page+webhook journeys stay interactive). Deterministic, $0.
    """
    tele: dict[str, Any] = {"stamped": 0}
    file_trigger = {
        str(r.get("file")): str(r.get("trigger") or "")
        for r in (routes_index or [])
        if r.get("file") and r.get("trigger") and r.get("trigger") != "interactive"
    }
    if not file_trigger:
        return tele
    flow_by_key: dict[str, "Flow"] = {}
    for fl in flows or []:
        for key in (getattr(fl, "uuid", None), getattr(fl, "name", None)):
            if key:
                flow_by_key.setdefault(str(key), fl)

    def _trigger_of(fl: "Flow") -> str | None:
        ep = getattr(fl, "entry_point_file", None)
        if ep and str(ep) in file_trigger:
            return file_trigger[str(ep)]
        for p in (getattr(fl, "paths", None) or []):
            if str(p) in file_trigger:
                return file_trigger[str(p)]
        return None

    for uf in user_flows:
        if getattr(uf, "trigger", None) or not (
                getattr(uf, "member_flow_ids", None) or []):
            continue
        verdicts: list[str | None] = []
        for mid in uf.member_flow_ids:
            fl = flow_by_key.get(str(mid))
            if fl is not None:
                verdicts.append(_trigger_of(fl))
        counted = [v for v in verdicts if v]
        if not counted or len(counted) != len(verdicts):
            continue  # mixed or unresolvable — stays interactive
        uf.trigger = Counter(counted).most_common(1)[0][0]
        uf.category = "system"
        tele["stamped"] += 1
    return tele


__all__ = [
    "INTENT",
    "NAME_TMPL",
    "SYSTEM_RECALL_REASON",
    "cluster_user_flows",
    "restamp_system_triggers",
    "resynthesize_system_ufs",
    "run_user_flow_rollup",
]
