"""W4.2 Fix 1 — technology-instrument detector (operator exhibit: typebot
``Prisma``; principle: *mechanisms, not dictionaries*).

THE CLASS: a monorepo workspace package (or feature-dir) that wraps a
declared TECHNOLOGY — the ORM package (``packages/prisma`` /
``packages/db``), the design-system kit (``packages/ui``), the job
runner (``packages/jobs``), the cache / logger / email / telemetry
wrapper, the tsconfig/eslint config package — is a development
instrument, not a product capability. 13 such PFs sat on the wave-4
board (~113K LOC, all 0 flows). They must land in the
platform-infrastructure lane as ``dev_tooling`` surfaces, never mint.

THE MECHANISM (no name dictionary decides alone — every signal is
grounded in the repo's OWN manifests, import graph, or route surface):

Candidates — workspace-package dirs under a shared-package container
(``packages/`` …, mirroring the mint's ``_LANE_SHARED_PKG_ROOTS``;
``apps/*`` are never candidates: an app is not an instrument).

Hard vetoes (any ⇒ product):
  * **V1 route surface** — any ``routes_index`` file inside the unit
    (a package with pages/routes is a product surface; validator-I20
    grain);
  * **V2 published CLI** — manifest declares ``bin`` and is not
    ``private`` (operator doctrine: midday ``packages/cli`` is a
    product customers install);
  * **V3 nested family** — the unit's parent dir is itself inside the
    container (``packages/forge/blocks/anthropic``,
    ``packages/embeds/js``): integration catalogs and shipped SDK
    families are product by doctrine (integration = its own PF);
  * **V4 hub inside** — a detected connector-hub family lives in the
    unit's subtree (midday ``packages/banking`` providers): the unit
    hosts product integrations.

Signals (S3 = V1-pass is the hard prerequisite; ≥1 of S1/S2 decides):
  * **S1a ecosystem root marker** — ``{tok}.config.*`` / ``.{tok}rc*``
    at the unit root where *tok* is a NON-AMBIENT dependency declared
    in the unit's own manifest (midday ``packages/jobs`` +
    ``trigger.config.ts``). Ambient = declared across ≥ max(3, N/3)
    of the repo's manifests — a repo-wide toolchain dep (vitest, zod)
    never marks a wrapper.
  * **S1b schema-tool formats** — ``schema.prisma`` with prisma
    declared, or a ``migrations/`` tree whose db-tool is the unit's
    dominant declared import (documenso/typebot ``packages/prisma``,
    midday ``packages/db`` + drizzle).
  * **S1c config-only unit** — ≤2 source files, config-majority
    content, AND either imported by nobody (``packages/tsconfig``;
    ``eslint-config`` referenced via an ``extends`` STRING, no import
    edge) OR consumed ONLY through config channels — every importer is a
    ``*.config.*`` / ``.*rc*`` file (a root ``prettier.config.cjs`` =
    ``module.exports = require('@scope/prettier-config')`` re-export;
    B1, kill-switch ``FAULTLINE_CONFIG_LANE``). Either way the unit is a
    settings artifact, not a library consumed by product code. The
    package name is never the trigger.
  * **S1d dominant-dependency wrapper** — ≥50% of the unit's source
    files import ONE non-ambient declared dependency and the unit
    imports ≤1 other in-repo unit (midday ``packages/email`` 87%
    react-email, ``packages/logger`` 100% pino).
  * **S1e thin transitive wrapper** — 1-2 distinct non-ambient
    external imports whose in-unit transitive reach covers ≥ 1/3 of
    the unit, ≤1 domain unit imported, ≥5 in-repo importer files, AND
    an infra-noun name corroboration (midday ``packages/cache`` —
    every file rides ``redis-client.ts`` → Bun's Redis). Name vocab
    is corroboration ONLY, per the operator amendment.
  * **S1f design-system workspace** — the unit's name-key is a
    UI-class workspace key (reusing Stage 6.55's ``_WS_UI_KEYS``, the
    W2b import-provenance vocabulary, minus the product-appearance
    words ``theme``/``styles``) and ≥5 files across ≥2 units import
    it (the four ``packages/ui`` kits).
  * **S2 corroborated broad asymmetry** — imported by ≥5 files across
    ≥3 units while importing ≤1 non-instrument unit itself
    (fixed-point), corroborated by name == a declared dependency
    token (midday ``packages/supabase``) or an infra-noun (typebot
    ``packages/telemetry``). Breadth + corroboration keeps domain
    cores (documenso ``packages/lib``: a heavy importer of domain whose
    name matches NO external dependency) product.
  * **S2 transport prong (B19)** — the fan-out guard is WAIVED for the
    name==dep corroboration when ``FAULTLINE_TECH_TRANSPORT_LANE`` is on:
    a broadly-imported ws-package NAMED after its OWN external dependency
    family (documenso ``packages/trpc`` → ``@trpc/*``) re-routes domain by
    construction, so a HIGH domain fan-out is its transport signature, not a
    domain-core tell. (This reverses the pre-B19 note that kept ``trpc``
    product: the operator-audit and PM-recognizability both say a transport
    is plumbing, not a capability. The ``packages/lib`` anti-case is safe
    because its name matches no external dep.)

    **OPEN QUESTION — default OFF pending a journey re-home (B19 keyed A/B,
    2026-07-10).** The classifier lanes ``trpc`` cleanly (documenso PFs
    18→17, laned away = ``['tRPC']``, zero domain-core collateral), BUT the
    journeys that were HOMED to the ``trpc`` PF are not re-homed — they
    DISSOLVE. On documenso ~20 real product journeys ("Browse and audit
    documents", "Export signed envelopes", "Manage embedded documents,
    templates and envelopes") vanish into ``Uncovered: … routes`` markers
    (79→42 UFs). The validator "improves" (12→7 violations) ONLY because the
    misattached journeys are gone — coverage LOSS disguised as cleanup, not a
    real gain. Laning a transport must be COUPLED with a path_index-aware
    journey re-home (B20 — redistribute the transport's journeys to the
    product PFs they serve, "ride role=shared on consumers") BEFORE this
    prong defaults ON. Until B20 lands, ``FAULTLINE_TECH_TRANSPORT_LANE``
    stays OFF (the classifier is correct; the coverage handoff is missing).

Fixed point: "imports no DOMAIN" ignores edges into already-classified
instruments (cache → logger/db), recomputed until stable.

**Satellite rule (fdir grain)** — a feature-dir anchor whose key equals
an instrument unit's name and that imports that unit shares the verdict
(typebot ``apps/builder/src/features/telemetry`` → the
``packages/telemetry`` wrapper's builder-side face).

Deterministic, $0 LLM, IO = manifests + the Stage 6.3/8.8 import
resolvers over tracked files. Kill-switch:
``FAULTLINE_TECH_INSTRUMENTS=0``.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "TECH_INSTRUMENTS_ENV",
    "CONFIG_LANE_ENV",
    "TRANSPORT_LANE_ENV",
    "WS_LIBRARY_LANE_ENV",
    "tech_instruments_enabled",
    "config_lane_enabled",
    "transport_lane_enabled",
    "ws_library_lane_enabled",
    "detect_technology_instruments",
]

TECH_INSTRUMENTS_ENV = "FAULTLINE_TECH_INSTRUMENTS"
#: B19 transport-package lane (design-review, default OFF). See
#: :func:`transport_lane_enabled`.
TRANSPORT_LANE_ENV = "FAULTLINE_TECH_TRANSPORT_LANE"
#: B48 ws-library lane (default OFF). See :func:`ws_library_lane_enabled`.
WS_LIBRARY_LANE_ENV = "FAULTLINE_WS_LIBRARY_LANE"
#: B1 kill-switch — the config-channel relaxation of S1c (below). Default
#: ON; ``FAULTLINE_CONFIG_LANE=0`` keeps the strict ``inf == 0`` guard so
#: output is byte-identical to pre-B1 main.
CONFIG_LANE_ENV = "FAULTLINE_CONFIG_LANE"

#: Shared-package container roots a candidate must live under (mirrors
#: ``stage_6_86_anchored_mint._LANE_SHARED_PKG_ROOTS`` — kept literal to
#: avoid an import cycle; ``apps/`` intentionally absent).
_CANDIDATE_ROOTS = ("packages", "libs", "internal", "tooling", "config",
                    "modules")

#: Infra-noun corroboration vocabulary (S1e / S2). Weak-signal ONLY —
#: never sufficient by itself, per the operator amendment ("name-vocab
#: is corroboration, not a signal"). Runtime-infrastructure nouns; the
#: journey-grain ``_INFRA_PACKAGE_SEGMENTS`` vocabulary is unioned in at
#: call time (single source of truth for the db/email/i18n class).
_INSTRUMENT_NOUNS = frozenset({
    "cache", "caching", "kv", "redis",
    "queue", "queues", "worker-queue",
    "logger", "logging",
    "telemetry", "analytics", "metrics", "monitoring", "tracing",
    "observability",
})

#: db-tool tokens for the migrations prong (S1b) — normalized dep names.
_DB_TOOLS = frozenset({
    "prisma", "drizzle", "drizzleorm", "kysely", "typeorm", "sequelize",
    "knex", "alembic", "atlas",
})

#: Body floor for the config-only prong (S1c): a "settings artifact"
#: carries no executable body. SAME calibration constant as the Stage
#: 6.86 vendor-husk floor (``_HUB_HUSK_LOC_FLOOR``, valsem4 H9 — kept
#: literal to avoid an import cycle): a 1,100-LOC single-file package is
#: a capability candidate (the W2b F1 contract), never "config-only".
_BODY_LOC_FLOOR = 150

_SRC_EXT = frozenset({
    "ts", "tsx", "js", "jsx", "mts", "cts", "mjs", "cjs", "py",
    "vue", "svelte",
})
_CONFIG_EXT = frozenset({
    "json", "yaml", "yml", "toml", "rc", "md", "mdx", "txt", "prisma",
    "sql", "lock", "snap", "svg", "png", "css", "cjs",
})

_MARKER_CONFIG_RE = re.compile(r"^\.?([A-Za-z0-9_.-]+?)\.config\.[a-z]+$")
_MARKER_RC_RE = re.compile(r"^\.([A-Za-z0-9]+?)rc(\.[a-z]+)?$")


def tech_instruments_enabled() -> bool:
    """Default ON; ``FAULTLINE_TECH_INSTRUMENTS=0`` disables."""
    return os.environ.get(TECH_INSTRUMENTS_ENV, "1").strip().lower() not in {
        "0", "false",
    }


def config_lane_enabled() -> bool:
    """B1 config-lane relaxation. Default ON; ``FAULTLINE_CONFIG_LANE=0``
    restores byte-identical output to main.

    OFF keeps the S1c ``inf == 0`` guard strict, so a config-only package
    consumed only through a config-channel re-export (a root
    ``prettier.config.cjs`` = ``module.exports = require('@scope/prettier-
    config')``) mints as a PF exactly as pre-B1. ON lanes it with the
    ``eslint-config`` / ``tsconfig`` class it belongs to."""
    return (os.environ.get(CONFIG_LANE_ENV, "1") or "1").strip().lower() \
        not in {"0", "false"}


def transport_lane_enabled() -> bool:
    """B19 — transport-package lane (design-review). A ws-package whose
    name-key matches its OWN declared external dependency family (the S2
    ``name-dep`` corroboration — a package NAMED after the library it wraps,
    e.g. ``packages/trpc`` -> ``@trpc/*``) is a transport/adapter instrument
    EVEN WHEN it fans out broadly into domain (a transport re-routes domain
    by construction). ON waives the ``len(dou) <= 1`` fan-out guard for the
    name-dep prong ONLY.

    Default **OFF** — the CLASSIFIER is ratified (a transport is plumbing),
    but the keyed A/B (2026-07-10) showed that laning a transport DISSOLVES
    the real product journeys homed to it (documenso 79→42 UFs, ~20 real
    journeys lost) because there is no journey re-home yet. It stays OFF
    until B20's path_index-aware re-home redistributes the transport's
    journeys to their consuming product PFs (see the S2 transport-prong
    docstring). ``FAULTLINE_TECH_TRANSPORT_LANE=1`` opts in for that coupled
    work. OFF is byte-identical to pre-B19."""
    return os.environ.get(TRANSPORT_LANE_ENV, "0").strip().lower() in {
        "1", "true",
    }


def ws_library_lane_enabled() -> bool:
    """B48 — corroboration-free ws-package library / name-dep transport lane.

    Default **OFF**. The existing S2 prong already lanes a broadly-imported
    zero-surface ws-package that imports ≤1 in-repo unit, BUT only when its
    name matches a dependency token or an infra noun. Compound-named
    (``twenty-ui`` → ``twentyui``) and generically-named (``shared``,
    ``dal``, ``framework``) libraries carry no such vocabulary and mint as
    fake product tiles. ON drops the name-vocab requirement — the HARD S3
    (no route/page surface via the route veto; not nav-confirmed) plus the
    import-direction library shape (or the B19 name-dep transport signature)
    are the mechanism. Candidates ride the B19/B22 transport-handoff channel
    for journey conservation (never mint-time laning). ``=1``/``true`` opts
    in; OFF is byte-identical to pre-B48."""
    return os.environ.get(WS_LIBRARY_LANE_ENV, "0").strip().lower() in {
        "1", "true",
    }


def _norm(tok: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(tok).lower())


def _dep_tokens(dep: str) -> set[str]:
    """Normalized token(s) of one dependency name.

    ``@prisma/client`` → {prisma}; ``@types/bun`` → {bun};
    ``trigger.dev`` → {triggerdev, trigger} (dotted vendor names index
    their pre-dot stem too — the config-file stem convention).
    """
    out: set[str] = set()
    if dep.startswith("@"):
        scope, _, suffix = dep[1:].partition("/")
        if scope == "types":
            out.add(_norm(suffix))
        else:
            out.add(_norm(scope.split(".")[0]))
    else:
        out.add(_norm(dep))
        if "." in dep:
            out.add(_norm(dep.split(".")[0]))
    out.discard("")
    return out


def _is_src(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    return ext in _SRC_EXT and not base.endswith(".d.ts")


def _is_config_class(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    if base.startswith("."):
        return True
    ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    return ext in _CONFIG_EXT


def _is_config_channel_file(path: str) -> bool:
    """A file that consumes shared config through a CONFIG channel: its
    basename is an ecosystem config entry-point (``*.config.*``) or an rc
    file (``.*rc*``). Reuses the S1a marker regexes so "config channel"
    has ONE definition across the module.

    B1: a config-only package imported SOLELY by such files (the root
    ``prettier.config.cjs`` re-export shim) is a settings artifact
    consumed by a config channel — not a library consumed by product
    code. Structure/file-system grounded; the importer's NAME is never
    the trigger."""
    base = path.rsplit("/", 1)[-1]
    return bool(_MARKER_CONFIG_RE.match(base) or _MARKER_RC_RE.match(base))


#: B28 S1g — a line that makes a module RUNTIME code: any import that is
#: not ``import type``, any export that is not a type-position export
#: (``export type`` / ``export interface`` / ``export declare``),
#: CommonJS surface, or a require call. Conservative direction: a mixed
#: ``import {type A, B}`` disqualifies (never lane on a maybe).
_TYPE_RUNTIME_RE = re.compile(
    r"^\s*(?:"
    r"import\s+(?!type\b)"
    r"|export\s+(?!type\b|interface\b|declare\b)"
    r"|module\.exports\b"
    r"|exports\.[A-Za-z_$]"
    r"|require\s*\("
    r")"
)

_COMMENT_LINE_RE = re.compile(r"^\s*(?://|/\*|\*)")


def _is_type_only_source(text: str) -> bool:
    """True when every import/export in *text* is type-position (the
    openapi-typescript / generated-types shape: ``import type`` +
    ``export type`` / ``export interface`` only). Comment-leading lines
    are skipped; any runtime-shaped line disqualifies."""
    for line in text.splitlines():
        if _COMMENT_LINE_RE.match(line):
            continue
        if _TYPE_RUNTIME_RE.match(line):
            return False
    return True


def _nonproduct_scope_enabled() -> bool:
    """B28 master flag (canonical constant lives in surface_taxonomy —
    one flag gates S1g + the P-D mark here AND the emission-side
    consumption). Default ON; ``FAULTLINE_NONPRODUCT_SCOPE=0`` off."""
    from faultline.pipeline_v2.surface_taxonomy import (
        nonproduct_scope_enabled,
    )

    return nonproduct_scope_enabled()


def detect_technology_instruments(
    repo_path: Path,
    tracked_files: Iterable[str],
    routes_index: Iterable[Mapping[str, Any]] | None,
    fdir_units: Iterable[str] = (),
    hub_dirs: Iterable[str] = (),
    source_cache: Any | None = None,
    nav_prefixes: Iterable[str] = (),
) -> dict[str, Any]:
    """Classify workspace units; returns telemetry + the instrument dirs.

    Output keys: ``instruments`` (unit → signal), ``satellites``
    (fdir → signal), ``vetoed`` (unit → veto), ``dirs`` (sorted union —
    the set every consumer keys on), ``rounds``, ``enabled``.

    ``source_cache`` (perf wave 2, R4): an optional pre-populated
    ``_SourceCache`` for ``repo_path`` — the caller passes the
    ctx-shared one; ``None`` constructs a local cache (unchanged
    behaviour, same content either way).
    """
    tele: dict[str, Any] = {
        "enabled": tech_instruments_enabled(),
        "instruments": {}, "satellites": {}, "vetoed": {},
        "dirs": [], "rounds": 0,
    }
    if not tele["enabled"]:
        return tele

    from faultline.analyzer.tsconfig_paths import build_path_alias_map
    from faultline.pipeline_v2.stage_6_3_import_tree import (
        _PY_EXTS,
        _SourceCache,
        _TS_EXTS,
        _is_vendor_or_test,
        _suffix,
    )
    from faultline.pipeline_v2.stage_6_55_page_interior import _WS_UI_KEYS
    from faultline.pipeline_v2.stage_6_7_user_flows import (
        _INFRA_PACKAGE_SEGMENTS,
    )
    from faultline.pipeline_v2.stage_8_8_shared_members import _resolve_one

    tracked = [str(p) for p in tracked_files]
    tracked_set = frozenset(tracked)
    nouns = frozenset(
        {_norm(n) for n in _INSTRUMENT_NOUNS}
        | {_norm(n) for n in _INFRA_PACKAGE_SEGMENTS}
    )
    ui_keys = frozenset(
        {_norm(k) for k in _WS_UI_KEYS} - {_norm("theme"), _norm("styles")}
    )

    # ── manifests ─────────────────────────────────────────────────────
    manifests: dict[str, dict] = {}
    for rel in tracked:
        if rel.rsplit("/", 1)[-1] == "package.json":
            try:
                doc = json.loads(
                    (repo_path / rel).read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    manifests[rel] = doc
            except (OSError, ValueError):
                continue
    internal_names = sorted(
        {str(d.get("name") or "") for d in manifests.values()} - {""},
        key=len, reverse=True)
    internal_scopes = {
        n.split("/")[0] for n in internal_names if n.startswith("@")}
    name_to_unit: dict[str, str] = {}
    unit_manifest: dict[str, dict] = {}
    for rel, doc in manifests.items():
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if not d:
            continue
        unit_manifest[d] = doc
        nm = str(doc.get("name") or "")
        if nm:
            name_to_unit.setdefault(nm, d)

    def _ws_unit_of_spec(spec: str) -> str | None:
        """O(1) workspace-name channel: ``@scope/pkg[/subpath]`` or a
        bare internal package name resolves to its unit dir."""
        segs = spec.split("/")
        if spec.startswith("@") and len(segs) >= 2:
            return name_to_unit.get("/".join(segs[:2]))
        return name_to_unit.get(segs[0])

    def _external(dep: str, spec: Any) -> bool:
        return (dep not in internal_names
                and not str(spec).startswith("workspace:")
                and dep.split("/")[0] not in internal_scopes)

    repo_ext_tokens: set[str] = set()
    declared_in: dict[str, int] = defaultdict(int)  # token → #manifests
    for doc in manifests.values():
        toks: set[str] = set()
        for block in ("dependencies", "devDependencies"):
            for dep, spec in (doc.get(block) or {}).items():
                if isinstance(dep, str) and _external(dep, spec):
                    toks |= _dep_tokens(dep)
        repo_ext_tokens |= toks
        for t in toks:
            declared_in[t] += 1
    ambient_floor = max(3, (len(manifests) + 2) // 3)
    ambient = {t for t, n in declared_in.items() if n >= ambient_floor}
    tele["ambient_tokens"] = sorted(ambient)[:20]

    # ── units + file mapping (fdirs ride as pseudo-units) ────────────
    units: dict[str, str] = {}  # dir → grain
    for d in unit_manifest:
        units[d] = "ws-pkg"
    for f in fdir_units:
        fd = str(f).strip("/")
        if fd and fd not in units:
            units[fd] = "fdir"
    if not units:
        return tele
    unit_dirs = sorted(units, key=len, reverse=True)
    unit_of_file: dict[str, str] = {}
    files_by_unit: dict[str, list[str]] = defaultdict(list)
    for t in tracked:
        for u in unit_dirs:
            if t.startswith(u + "/"):
                unit_of_file[t] = u
                files_by_unit[u].append(t)
                break

    route_files = {
        str(e.get("file") or "") for e in (routes_index or [])
        if isinstance(e, Mapping) and e.get("file")
    }

    # ── one import walk ───────────────────────────────────────────────
    cache = source_cache or _SourceCache(repo_path)
    # W6-AST Hook B (M4): graph-backed provenance (S2); None → legacy.
    from faultline.pipeline_v2.ts_ast.adapter import repo_provenance
    prov = repo_provenance(str(repo_path), tracked)
    try:
        alias_map = build_path_alias_map(repo_path)
    except Exception:  # noqa: BLE001 — resolver is best-effort
        alias_map = None
    in_files: dict[str, set[str]] = defaultdict(set)
    in_units: dict[str, set[str]] = defaultdict(set)
    in_edges: dict[str, int] = defaultdict(int)
    out_units: dict[str, set[str]] = defaultdict(set)
    ext_files: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set))
    local_edges: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set))
    src_by_unit: dict[str, list[str]] = defaultdict(list)
    for rel in sorted(tracked):
        if _suffix(rel) not in (_TS_EXTS | _PY_EXTS) or _is_vendor_or_test(rel):
            continue
        su = unit_of_file.get(rel)
        if su and _is_src(rel):
            src_by_unit[su].append(rel)
        if prov is not None and rel in prov.files:
            specs = prov.spec_occurrences(rel)  # W6-AST Hook B (M4)
        else:
            try:
                specs = list(cache.imports(rel).values())
            except Exception:  # noqa: BLE001 — unreadable file → no imports
                continue
        for spec in specs:
            tu: str | None = _ws_unit_of_spec(spec)
            if tu is None:
                tgt = (prov.resolve(rel, spec)
                       if prov is not None and rel in prov.files
                       else _resolve_one(rel, spec, alias_map, tracked_set))
                if tgt is not None:
                    tu = unit_of_file.get(tgt)
                    if su is not None and tu == su:
                        local_edges[su][rel].add(tgt)
                elif not spec.startswith("."):
                    if su is not None:
                        seg0 = spec.split("/")[0]
                        if seg0 not in internal_scopes:
                            if spec.startswith("@"):
                                dep = "/".join(spec.split("/")[:2])
                            else:
                                # protocol imports (node:crypto, bun:test)
                                # ground on the runtime name.
                                dep = seg0.split(":")[0] or seg0
                            for tok in _dep_tokens(dep):
                                ext_files[su][tok].add(rel)
                    continue
                else:
                    continue
            if tu is None or tu == su:
                continue
            in_edges[tu] += 1
            in_files[tu].add(rel)
            in_units[tu].add(su or "<app>")
            if su is not None:
                out_units[su].add(tu)

    # ── per-unit facts ────────────────────────────────────────────────
    def _facts(u: str) -> dict[str, Any]:
        fs = files_by_unit.get(u, [])
        man = unit_manifest.get(u) or {}
        declared: set[str] = set()
        for block in ("dependencies", "devDependencies"):
            for dep, spec in (man.get(block) or {}).items():
                if isinstance(dep, str) and _external(dep, spec):
                    declared |= _dep_tokens(dep)
        src = src_by_unit.get(u, [])
        n_src = len(src)
        share: dict[str, float] = {}
        for tok, fset in ext_files.get(u, {}).items():
            if tok in declared and tok not in ambient:
                share[tok] = len(fset) / max(n_src, 1)
        top_tok, top_share = "", 0.0
        if share:
            top_tok = max(share, key=lambda k: (share[k], k))
            top_share = share[top_tok]
        # transitive tech reach — BFS within the unit from files with any
        # external import.
        direct = {f for fset in ext_files.get(u, {}).values() for f in fset}
        reach = set(direct)
        edges = local_edges.get(u, {})
        changed = True
        while changed:
            changed = False
            for f, targets in edges.items():
                if f not in reach and targets & reach:
                    reach.add(f)
                    changed = True
        non_ambient_ext = {
            t for t in ext_files.get(u, {}) if t not in ambient
        }
        markers: list[str] = []
        for p in fs:
            base = p.rsplit("/", 1)[-1]
            if "/" in p[len(u) + 1:]:
                continue  # unit root only
            for rx in (_MARKER_CONFIG_RE, _MARKER_RC_RE):
                m = rx.match(base)
                if m and _norm(m.group(1)) in declared and _norm(
                        m.group(1)) not in ambient:
                    markers.append(_norm(m.group(1)))
        schema_prisma = any(
            p.rsplit("/", 1)[-1] == "schema.prisma" for p in fs
        ) and ("prisma" in declared or "prisma" in repo_ext_tokens)
        has_migrations = any("/migrations/" in "/" + p for p in fs)
        n_cfg = sum(1 for p in fs if _is_config_class(p))
        # Source body of NEAR-CONFIG units only (≤2 src files — bounded
        # IO); unreadable counts as a large body (never config-only).
        src_loc = 0
        if 0 < n_src <= 2:
            for rel in src:
                try:
                    text = (repo_path / rel).read_text(
                        encoding="utf-8", errors="ignore")
                    src_loc += sum(1 for ln in text.splitlines()
                                   if ln.strip())
                except OSError:
                    src_loc += _BODY_LOC_FLOOR
        base = u.rsplit("/", 1)[-1]
        man_suffix = str(man.get("name") or "").split("/")[-1]
        # B28 S1g facts — runtime dependency count + TS manifest entry (a
        # zero-runtime-dep package whose entry is TypeScript is the
        # generated-types shape; the type-only body check runs lazily).
        entry_ts = ""
        entry_spec = str(man.get("main") or man.get("types") or "")
        if entry_spec.endswith((".ts", ".tsx", ".mts", ".cts")):
            entry_ts = u + "/" + entry_spec.lstrip("./")
        return {
            "files": len(fs), "src": n_src, "src_loc": src_loc,
            "declared": declared,
            "dep_share": share,
            "top_tok": top_tok, "top_share": top_share,
            "tech_reach": len(reach & set(src)) / max(n_src, 1),
            "non_ambient_ext": non_ambient_ext,
            "markers": markers,
            "schema_prisma": schema_prisma,
            "migrations": has_migrations,
            "cfg_share": n_cfg / max(len(fs), 1),
            "name_keys": {_norm(base), _norm(man_suffix)} - {""},
            "raw_base": base.lower(),
            "bin": bool(man.get("bin")),
            "private": man.get("private"),
            "runtime_deps": len(man.get("dependencies") or {}),
            "entry_ts": entry_ts,
        }

    facts = {u: _facts(u) for u in units if files_by_unit.get(u)}

    # B28 S1g — lazy type-only body check (each candidate unit's sources
    # read at most once; only zero-runtime-dep TS-entry units reach it).
    _types_only_cache: dict[str, bool] = {}

    def _types_only_unit(u: str) -> bool:
        cached = _types_only_cache.get(u)
        if cached is not None:
            return cached
        rels = list(src_by_unit.get(u, []))
        entry = facts[u]["entry_ts"]
        if entry and entry not in rels and entry in tracked_set:
            rels.append(entry)
        verdict = bool(rels)
        for rel in rels:
            try:
                text = (repo_path / rel).read_text(
                    encoding="utf-8", errors="ignore")
            except OSError:
                verdict = False
                break
            if not _is_type_only_source(text):
                verdict = False
                break
        _types_only_cache[u] = verdict
        return verdict

    hub_list = [str(h).strip("/") for h in hub_dirs if h]

    def _vetoes(u: str) -> str | None:
        f = facts[u]
        if any(rf == u or rf.startswith(u + "/") for rf in route_files):
            return "route_surface"
        if f["bin"] and f["private"] is not True:
            return "published_cli"
        segs = u.split("/")
        if segs[0] not in _CANDIDATE_ROOTS:
            return "not_shared_container"
        if len(segs) > 2:
            return "nested_family"
        if any(h == u or h.startswith(u + "/") for h in hub_list):
            return "hosts_hub_family"
        return None

    instruments: dict[str, str] = {}
    config_lane = config_lane_enabled()  # B1 kill-switch (S1c relaxation)
    transport_lane = transport_lane_enabled()  # B19 (design-review, def OFF)
    nonproduct_scope = _nonproduct_scope_enabled()  # B28 (S1g + P-D mark)

    def _dou(u: str) -> set[str]:
        return {t for t in out_units.get(u, ()) if t not in instruments}

    def _round() -> bool:
        changed = False
        for u in sorted(facts):
            if u in instruments or units[u] != "ws-pkg":
                continue
            veto = _vetoes(u)
            if veto:
                if u not in tele["vetoed"] and veto not in (
                        "not_shared_container",):
                    tele["vetoed"][u] = veto
                continue
            f = facts[u]
            dou = _dou(u)
            inf = len(in_files.get(u, ()))
            inu = len(in_units.get(u, ()))
            # B1 — config-channel consumption. True iff the unit HAS
            # importers and every importer file is a config-channel file
            # (``*.config.*`` / ``.*rc*``). Gated by the kill-switch:
            # flag-OFF short-circuits to False so S1c stays byte-identical
            # (``inf == 0`` only). Pure bool; no set escapes into output.
            cfg_consumed = (
                config_lane
                and inf > 0
                and all(_is_config_channel_file(p)
                        for p in in_files.get(u, ()))
            )
            sig: str | None = None
            # S1a alignment is STRICT: the marker's token must itself be
            # a WIDELY-IMPORTED dep of the unit (midday `workbench` /
            # `mcp-apps` carry vite/postcss build markers whose only import
            # the config file itself — a toolchain marker never marks a
            # product package; midday `jobs`' trigger config + 75% task
            # imports does).
            aligned = sorted(
                tok for tok in set(f["markers"])
                if f["dep_share"].get(tok, 0.0) >= 0.34
            )
            if aligned:
                sig = "S1a-marker:" + aligned[0]
            elif f["schema_prisma"]:
                sig = "S1b-schema-prisma"
            elif (f["migrations"] and f["top_tok"] in _DB_TOOLS
                  and f["top_share"] >= 0.5):
                sig = "S1b-migrations:" + f["top_tok"]
            elif (f["src"] <= 2 and f["cfg_share"] >= 0.5
                  and f["src_loc"] < _BODY_LOC_FLOOR
                  and (inf == 0 or cfg_consumed)):
                # inf==0: settings artifact imported by nobody (``tsconfig``;
                # ``eslint-config`` referenced via an `extends` STRING — no
                # import edge). cfg_consumed (B1): imported ONLY by
                # config-channel files (a ``prettier.config.cjs`` re-export)
                # — still a settings artifact, never product code.
                sig = ("S1c-config-only" if inf == 0
                       else "S1c-config-consumed")
            elif (f["top_share"] >= 0.5 and len(dou) <= 1 and inf >= 3):
                sig = "S1d-dep:" + f["top_tok"]
            elif (1 <= len(f["non_ambient_ext"]) <= 2
                  and f["tech_reach"] >= 0.34 and len(dou) <= 1
                  and inf >= 5
                  and (f["name_keys"] & nouns
                       or _norm(f["raw_base"]) in nouns)):
                sig = "S1e-thin-wrapper"
            elif (f["name_keys"] & ui_keys) and inf >= 5 and inu >= 2:
                sig = "S1f-design-system"
            elif (nonproduct_scope and f["runtime_deps"] == 0
                  and f["entry_ts"] and _types_only_unit(u)):
                # B28 S1g — types-only package: the manifest declares ZERO
                # runtime dependencies and a TypeScript entry, and every
                # import/export in the unit's sources is type-position
                # (supabase packages/api-types: openapi-typescript codegen,
                # index.ts of pure `import type`/`export interface`). A
                # zero-dep generated-types package defeats S1a-e (no dep
                # evidence) and S2 (no name-dep/infra-noun corroboration)
                # — manifest + source structure ARE the mechanism here, no
                # name vocabulary involved.
                sig = "S1g-types-only"
            elif inf >= 5 and inu >= 3 and (
                    len(dou) <= 1
                    # B19 transport prong (design-review, default OFF): a
                    # broadly-imported package NAMED after its own external
                    # dependency family re-routes domain by construction, so a
                    # high domain fan-out is its signature, not a domain-core
                    # tell. Waive the fan-out guard for the name-dep prong ONLY
                    # (the weaker infra-noun prong still requires len(dou)<=1).
                    or (transport_lane
                        and (f["name_keys"] & repo_ext_tokens))):
                if f["name_keys"] & repo_ext_tokens:
                    sig = ("S2-asymmetry:name-dep" if len(dou) <= 1
                           else "S2-transport:name-dep-fanout")
                elif f["name_keys"] & nouns:  # reachable only when dou<=1
                    sig = "S2-asymmetry:infra-noun"
            if sig:
                instruments[u] = sig
                changed = True
        return changed

    rounds = 0
    while _round():
        rounds += 1
        if rounds > 8:  # pragma: no cover — defensive cap
            break
    tele["rounds"] = rounds

    # B22 — transport-lane handoff: the S2 transport verdict becomes a
    # candidate MARK, not an instrument dir. The fixed point above is
    # UNCHANGED (candidates influenced sibling classification exactly as
    # under B19 mint-time laning); only the EMISSION changes: candidates
    # leave ``instruments``/``dirs`` (so the unit mints normally and its
    # journeys mint normally — 6.86 sees no instrument dir) and ride out
    # in ``transport_candidates`` for Stage 6.985 to resolve AFTER the
    # journey layer settles. Popped BEFORE the satellite pass so a
    # candidate's fdir satellites stay product alongside it. The key is
    # emitted only when non-empty: handoff-OFF (or transport-OFF) output
    # stays byte-identical to HEAD.
    if transport_lane and instruments:
        from faultline.pipeline_v2.transport_handoff import (
            transport_handoff_enabled,
        )
        if transport_handoff_enabled():
            transport_candidates: dict[str, str] = {}
            for u in sorted(instruments):
                if instruments[u].startswith("S2-transport:"):
                    transport_candidates[u] = instruments.pop(u)
            if transport_candidates:
                tele["transport_candidates"] = transport_candidates

    # B48 — ws-library / name-dep transport lane (default OFF). The
    # existing S2 prong lanes a broadly-imported zero-surface package that
    # imports <=1 in-repo unit ONLY when its name matches a dep token or an
    # infra noun; compound-named (``twenty-ui``) and generically-named
    # (``shared``/``dal``/``framework``) libraries carry no such vocabulary
    # and mint fake product tiles. This post-round fixed-point pass drops
    # the name-vocab requirement, compensated by the HARD S3 (no route/page
    # surface — the ``route_surface`` veto — AND not nav-confirmed). It
    # emits into the SAME ``transport_candidates`` channel B22's Stage 6.985
    # handoff consumes, so a laned package's journeys re-home first
    # (all-or-nothing conservation; never mint-time laning — the I9 law).
    # Every anti-case (route-anchored surfaces, integrations, domain cores,
    # published CLIs, nested SDK families) is already protected by the
    # vetoes + the dou>1 / import-breadth guards below. Byte-identical to
    # pre-B48 when OFF (the pass is skipped).
    if ws_library_lane_enabled():
        from faultline.pipeline_v2.transport_handoff import (
            transport_handoff_enabled,
        )
        if transport_handoff_enabled():
            nav_pfx = tuple(sorted(str(p).strip("/") for p in nav_prefixes if p))

            def _nav_confirmed(u: str) -> bool:
                # S3 (nav): the unit is blocked when IT or an ANCESTOR is a
                # nav-declared anchor prefix — the author's own IA places the
                # unit inside a product area. A nav match on a DEEP DESCENDANT
                # subtree does NOT block (cal.com forensic, 2026-07-12): a
                # transport package's router dirs are named after the nav
                # features they serve by construction (packages/trpc/server/
                # routers/viewer/apiKeys ↔ the 'api-keys' nav cluster), so
                # every transport would self-veto on its own attribution
                # echo. Domain cores with nav-echoing internals stay
                # protected by the dou>1 guard regardless (packages/lib:
                # nav-echo AND dou=5 — two independent rails).
                return any(
                    u == p or u.startswith(p + "/") for p in nav_pfx
                )

            b48: dict[str, str] = {}

            def _b48_dou(u: str) -> set[str]:
                return {t for t in out_units.get(u, ())
                        if t not in instruments and t not in b48}

            changed = True
            while changed:
                changed = False
                for u in sorted(facts):
                    if (u in instruments or u in b48
                            or units.get(u) != "ws-pkg"):
                        continue
                    if _vetoes(u):  # route_surface / published_cli /
                        continue    # nested_family / hosts_hub_family / …
                    if _nav_confirmed(u):  # S3 nav
                        continue
                    fx = facts[u]
                    inf = len(in_files.get(u, ()))
                    inu = len(in_units.get(u, ()))
                    if inf < 5 or inu < 3:  # import-breadth (same as S2)
                        continue
                    sig: str | None = None
                    if fx["name_keys"] & repo_ext_tokens:
                        # S1 name-dep transport: NAMED after its own external
                        # dependency family (waives the dou fan-out guard —
                        # a transport re-routes domain by construction).
                        sig = "B48:name-dep"
                    elif len(_b48_dou(u)) <= 1:
                        # S2 library: imports <=1 non-instrument in-repo unit
                        # (fixed point over instruments + B48 candidates).
                        sig = "B48:library"
                    if sig:
                        b48[u] = sig
                        changed = True
            if b48:
                tc = dict(tele.get("transport_candidates") or {})
                for u, sig in b48.items():
                    tc.setdefault(u, sig)
                tele["transport_candidates"] = dict(sorted(tc.items()))
                tele["b48_library_candidates"] = dict(sorted(b48.items()))

    # B28 P-D — hub-fixture MARK (mark-only, the B22 candidate-marking
    # shape: no instrument dir, no lane at 6.86 — the unit mints normally
    # and the EMISSION taxonomy consumes the mark behind the R1/R2 rails,
    # so its journeys ride into the lane with it). A hub-child ws-pkg
    # whose only importers are uniform hub BARRELS (a consumer importing
    # a strict majority of ALL siblings dispatches the family, it does
    # not choose this unit — cal.com ``apps.*.generated.ts``) has no real
    # product consumer; inside an integration hub structure alone cannot
    # separate a test fixture from a barrel-dispatched real integration
    # (the zoomvideo counterfactual), so the dev-artifact TOKEN
    # (YAML data, ``dev_artifact_tokens``) is REQUIRED corroboration per
    # the mechanisms-doctrine (integrations = own PF is a binding law —
    # never lane a real integration on structure alone).
    if nonproduct_scope:
        try:
            from faultline.pipeline_v2.surface_taxonomy import (
                load_patterns as _load_scope_patterns,
            )
            _da_tokens = frozenset(
                str(t).lower()
                for t in (_load_scope_patterns().get("dev_artifact_tokens")
                          or [])
            )
        except Exception:  # noqa: BLE001 — patterns are best-effort data
            _da_tokens = frozenset()
        fixture_units: dict[str, str] = {}
        if _da_tokens:
            kids_by_parent: dict[str, list[str]] = defaultdict(list)
            for u in units:
                if units[u] != "ws-pkg" or "/" not in u:
                    continue
                parent = u.rsplit("/", 1)[0]
                if parent in units:  # the hub itself is a ws unit
                    kids_by_parent[parent].append(u)
            taken = set(instruments) | set(
                tele.get("transport_candidates") or {})
            for parent, kids in sorted(kids_by_parent.items()):
                if len(kids) < 3:  # hub bar (the 3-cluster class)
                    continue
                root_pref = parent + "/"
                for k in sorted(kids):
                    if k in taken:
                        continue
                    # "no visible product consumer": the hub's own
                    # ROOT-LEVEL dispatch files are the hub's machinery,
                    # never consumers (cal.com's generated
                    # ``apps.*.generated.ts`` AND the category-scoped
                    # ``payment.services.generated.ts`` all live directly
                    # at the hub root); dynamic dispatch
                    # (``import("./x/api")`` object literals) yields ZERO
                    # import specs, so zero-importer children are the
                    # same structural fact. Anything else — an outside
                    # file or a sibling package — is a REAL consumer.
                    imps = in_files.get(k, set())
                    if any(not (imp.startswith(root_pref)
                                and "/" not in imp[len(root_pref):])
                           for imp in imps):
                        continue  # a real consumer exists
                    toks = {
                        t for t in re.split(
                            r"[^a-z0-9]+", k.rsplit("/", 1)[-1].lower())
                        if t
                    }
                    man_name = str(
                        (unit_manifest.get(k) or {}).get("name") or "")
                    toks |= {
                        t for t in re.split(r"[^a-z0-9]+", man_name.lower())
                        if t
                    }
                    if toks & _da_tokens:
                        fixture_units[k] = "P-D:hub-fixture-barrel-only"
        if fixture_units:
            tele["dev_artifact_units"] = dict(sorted(fixture_units.items()))

    # satellite fdirs
    satellites: dict[str, str] = {}
    for u in sorted(units):
        if units[u] != "fdir" or u not in facts:
            continue
        key = _norm(u.rsplit("/", 1)[-1])
        for iu in sorted(instruments):
            fi = facts.get(iu) or {}
            if (key and key in (fi.get("name_keys") or set())
                    and iu in out_units.get(u, ())):
                satellites[u] = f"satellite:{iu}"
                break

    tele["instruments"] = dict(sorted(instruments.items()))
    tele["satellites"] = dict(sorted(satellites.items()))
    tele["dirs"] = sorted(set(instruments) | set(satellites))
    return tele
