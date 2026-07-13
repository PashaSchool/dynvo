from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_serializer, model_validator

SCHEMA_VERSION: int = 1
"""Current FeatureMap JSON schema version, stamped on every new scan.

Bump policy — increment ONLY on **breaking** changes to the on-disk
schema:

  - removing or renaming a field,
  - changing the semantics of an existing field,
  - removing an enum/literal value consumers may switch on.

Additive optional fields (the normal evolution path — new fields with
defaults so old JSONs rehydrate unchanged) do NOT bump the version.

``FeatureMap.schema_version`` defaults to ``0``, meaning
"pre-versioning scan": any JSON written before this constant existed
deserializes with ``schema_version == 0``, distinguishable from a
current scan (``1``). Keep this an ``int`` — consumers compare with
``>=`` / ``==``, never parse it.
"""


class TimelinePoint(BaseModel):
    date: str          # ISO week label "YYYY-Www"
    total_commits: int
    bug_fix_commits: int
    test_commits: int


class Commit(BaseModel):
    sha: str
    message: str
    author: str
    date: datetime
    files_changed: list[str]
    is_bug_fix: bool = False
    pr_number: int | None = None


class PullRequest(BaseModel):
    number: int
    url: str          # full GitHub PR URL, empty string if remote unknown
    title: str        # first line of the commit message
    author: str
    date: datetime


class FileBlame(BaseModel):
    path: str
    authors: list[str]
    last_modified: datetime
    total_commits: int


class HotspotFile(BaseModel):
    """One source file inside an entity (feature / flow / product feature)
    whose commit history shows bug-fix churn above the universal
    hotspot thresholds.

    Emitted by ``stage_6_metrics`` on a per-entity basis. Thresholds
    are scale-invariant (ratio + minimum sample size) so the same
    rule applies regardless of repo size — see
    ``HOTSPOT_BUG_RATIO_MIN`` / ``HOTSPOT_COMMITS_MIN`` in that
    module. Sorting (ratio desc, then total_commits desc) lets
    renderers slice ``[:N]`` without re-sorting.
    """

    path: str
    bug_fix_ratio: float       # bug_fixes / total_commits, rounded to 3 dp
    bug_fixes: int             # commits whose Commit.is_bug_fix is True
    total_commits: int         # commits touching this file inside the entity's window


class Flow(BaseModel):
    name: str                  # "checkout-flow", "login-flow"
    display_name: str | None = None  # Title Case label for UI ("Checkout")
    # Clean kebab label WITHOUT the trailing "-flow"/"-flows" suffix, for compact
    # display in tables/PR comments ("checkout-flow" -> "checkout"). Additive.
    short_label: str = ""
    description: str | None = None
    participants: list["FlowParticipant"] = []
    # Sprint 4: tool-augmented flow detection grounds every flow in
    # a real route handler / event subscription. These fields point
    # at the file (and optional line) where that flow's user journey
    # begins. Both are None for flows produced by the legacy Haiku
    # detector (which doesn't record entry points).
    entry_point_file: str | None = None
    entry_point_line: int | None = None
    paths: list[str]           # files belonging to this flow
    authors: list[str]
    total_commits: int
    bug_fixes: int
    bug_fix_ratio: float
    last_modified: datetime
    health_score: float        # 0-100, higher is better
    bug_fix_prs: list[PullRequest] = []
    test_file_count: int = 0   # number of test files associated with this flow
    test_files: list[str] = []  # actual test file paths (Sprint 2 Day 10)
    weekly_points: list[TimelinePoint] = []  # weekly activity timeline
    bus_factor: int = 1                      # authors with ≥20% of flow commits
    health_trend: float | None = None        # first_half_bug_ratio - second_half; positive = improving
    hotspot_files: list[str] = []            # legacy: file paths with >40% bug_fix_ratio (≥3 commits); kept for back-compat with the legacy analyzer + MCP consumers
    # Sprint 2026-05-28 — richer dict-shape hotspots emitted by
    # pipeline_v2 Stage 6. Each entry carries path + ratio + bug_fixes
    # + total_commits so renderers (carousel, PR comments, MCP risk
    # surface) can show counts/ratios without re-walking git. Empty
    # for scans produced before this field existed and for entities
    # below the universal hotspot thresholds.
    hotspot_files_detail: list["HotspotFile"] = []
    coverage_pct: float | None = None        # avg line coverage % across source files; None if unavailable
    symbol_attributions: list["SymbolAttribution"] = []  # symbols (functions/classes) that belong to this flow — populated when --symbols is enabled
    # Sprint 12 Day 3.5 — multi-feature ownership. A flow can
    # legitimately participate in more than one feature ("Create
    # Organization" touches Auth + Billing + Notifications). The
    # PRIMARY owner is the parent ``Feature.flows`` it appears under;
    # ``secondary_features`` are additional feature names the flow
    # also belongs to. Dashboards render this as a "shared with: X, Y"
    # badge. Empty list = single-feature flow (most common case).
    secondary_features: list[str] = []
    # Sprint B1 (2026-05-19) — bipartite blast-radius surface.
    # ``id`` is a stable global identifier of the form
    # "{primary_feature}::{slug}". ``primary_feature`` is the canonical
    # owner used in the containment view (Feature.flows). The counts
    # below summarise the bipartite store so consumers don't need to
    # re-walk feature_flow_edges. ``cross_cutting`` is just shorthand
    # for ``shared_with_features_count > 0``.
    id: str | None = None
    primary_feature: str | None = None
    shared_with_flows_count: int = 0
    shared_with_features_count: int = 0
    cross_cutting: bool = False
    # Sprint C2 (2026-05-19) — per-flow line-level symbol attribution.
    # Distinct from the legacy ``symbol_attributions`` field above
    # (which is per-FILE / per-feature aggregate). This is the flat
    # per-symbol record-set described by ``flow-feature-concept``.
    # Populated by Stage 3's deterministic ``flow_symbols`` post-pass;
    # empty when entry-symbol detection fails (telemetry-flagged in
    # ``scan_meta.stage_3_entry_detection_failure_rate``).
    flow_symbol_attributions: list["FlowSymbolAttribution"] = []
    # Sprint 1 (2026-05-23) — stable lineage UUID. Empty string for
    # flows produced before lineage was wired (defensive — the
    # pipeline_v2 lineage pass should fill it in).
    uuid: str = ""
    previous_names: list[str] = []
    split_from: str | None = None
    merged_from: list[str] = []
    # Sprint 2 (2026-05-23) — flow-expansion surface (Stage 3.5).
    # ``entry`` is the canonical starting point of the flow (file +
    # symbol + lines). ``nodes`` + ``edges`` are the call graph
    # produced by T1 (intra-repo) + T2 (cross-stack HTTP). ``summary``
    # carries roll-up counters. All four default to empty/None so
    # legacy serialized scans rehydrate without loss; landing keeps
    # reading ``paths`` / ``participants`` / ``flow_symbol_attributions``
    # while MCP / agent context fetchers can read the richer graph.
    entry: dict[str, Any] | None = None
    nodes: list["FlowNode"] = []
    edges: list["FlowEdge"] = []
    summary: "FlowSummary | None" = None
    # Phase 5 (2026-05-26) — LOC-detail parity with Feature. These are
    # ADDITIVE projections derived deterministically from the already-
    # computed Stage 3.5 ``entry`` / ``nodes`` / ``edges`` graph and the
    # Stage 3 ``flow_symbol_attributions``. They give a Flow the same
    # line-level surface a Feature emits, in the shape the landing app
    # consumes (``path`` / ``start_line`` / ``end_line``). NOTHING above
    # is mutated — these fields default empty so legacy scans rehydrate
    # unchanged.
    #
    #   * ``entry_point``        — richer entry object {path, symbol, line};
    #                              the legacy ``entry_point_file`` /
    #                              ``entry_point_line`` / ``entry`` stay.
    #   * ``line_ranges``        — the flow's own span: one record per
    #                              (path, start_line, end_line) covering
    #                              every node with resolved lines, merged
    #                              per-file into non-overlapping spans.
    #   * ``loc_symbol_attributions`` — full per-participant records
    #                              {path, symbol, kind, start_line,
    #                              end_line} at parity with the Feature
    #                              symbol surface (widens the thin
    #                              ``flow_symbol_attributions``).
    #   * ``loc_nodes``          — call-graph nodes in the landing shape
    #                              {path, symbol, start_line, end_line,
    #                              role}.
    #   * ``loc_edges``          — caller→callee edges carrying the
    #                              call-site {path, line}.
    entry_point: "FlowEntryPoint | None" = None
    line_ranges: list["FlowLineRange"] = []
    loc_symbol_attributions: list["FlowLocSymbolAttribution"] = []
    loc_nodes: list["FlowLocNode"] = []
    loc_edges: list["FlowLocEdge"] = []
    # UF-Stage1 (2026-06-02) — Layer-2-for-flows pointer. Mirrors the
    # ``Feature.product_feature_id`` two-layer model: a code-grain flow
    # rolls up into one product-grain User Flow (see ``UserFlow``).
    # ``None`` for flows produced before the deterministic UF rollup
    # ran, or flows the rollup could not assign. Additive — never read
    # in place of any existing field.
    user_flow_id: str | None = None
    # W4 (Product-Spine §4.6) — cross-PF span ledger. Files this flow
    # traverses that are OWNED by a DIFFERENT product feature than the
    # flow's home PF are split out of ``paths`` (the primary
    # projection) into this labeled sharing surface — per
    # flow-feature-concept, sharing is legal and labeled; conservation:
    # no file is lost, it is re-labeled. Empty for scans produced
    # before W4 and when the anchored mint didn't run.
    shared_paths: list["FlowSharedPath"] = []
    # B11 (2026-07-09) — flow-level OWNED-vs-SHARED span LOC, a DISPLAY
    # partition (not an excision) of the flow's owned span footprint.
    # ``loc`` counts the lines of this flow's owned spans (the validator's
    # ``_spine_flow_loc_owned`` selection: nodes with a valid 2-int span,
    # excluding role="interior" + shared_paths-ledger files) that NO OTHER
    # flow covers — the story unique to this journey. ``loc_shared`` counts
    # the owned-span lines this flow SHARES with ≥1 sibling flow (the
    # blast-radius surface — a shared helper/layout legitimately belongs to
    # every flow that uses it, per flow-feature-concept). By construction
    # ``loc + loc_shared == union of this flow's owned spans`` (the same
    # figure ``_spine_flow_loc_owned`` yields) — conservation, so I13 loc
    # accounting is unmoved and I19's node-derived owned numerator is
    # untouched (these fields are additive, the node ledger is not mutated).
    # Without them the dashboard blends distinct flows sharing one file into
    # an identical file-grain LOC (the reactive-resume email trio all read
    # "113"); the split shows the honest ~13 unique / ~100 shared instead.
    # ``None`` (serializer-omitted) on scans produced before the stage or
    # with FAULTLINE_FLOW_LOC=0 → byte-identical to the pre-B11 engine.
    loc: int | None = None
    loc_shared: int | None = None

    @model_serializer(mode="wrap")
    def _omit_none_flow_loc(self, handler: Any) -> Any:
        """Drop the B11 owned/shared LOC fields from dumps when unset.

        A scan produced with ``FAULTLINE_FLOW_LOC=0`` (or a pre-B11 engine)
        leaves ``loc``/``loc_shared`` at their ``None`` default; popping them
        keeps the flow's serialized shape byte-identical to the pre-B11 engine
        (snapshot-gate digest contract). Mirrors ``UserFlow._omit_none_identity``
        for the UF-loc field. Every other field is emitted exactly as the
        default handler produces it (including pre-existing ``None`` fields such
        as ``health_trend``), so nothing else shifts.
        """
        data = handler(self)
        if isinstance(data, dict):
            for key in ("loc", "loc_shared"):
                if data.get(key) is None:
                    data.pop(key, None)
        return data


class SymbolRange(BaseModel):
    name: str              # exported symbol name, e.g. "FEATURE_FLAGS"
    start_line: int        # 1-indexed, inclusive
    end_line: int          # 1-indexed, inclusive
    kind: str = "const"    # "const", "function", "class", "type", "enum", "reexport", "method", "constructor"
    # When this symbol is a METHOD (or constructor / class field arrow-fn)
    # defined INSIDE a class, ``parent`` carries the enclosing class name.
    # Method-level indexing (added so a member call ``obj.findById()``
    # resolves to the SPECIFIC method body, not the whole enclosing class —
    # the whole-class-pulled-into-a-flow over-count). ``None`` for top-level
    # symbols. Purely additive: existing readers ignore it.
    parent: str | None = None


class FlowParticipant(BaseModel):
    """One file that participates in a flow's call-graph reach.

    Sprint 7 ``trace_flow_callgraph`` populates these from the
    symbol-import graph BFS + layer classifier. ``layer`` is one of
    ``ui`` / ``state`` / ``api-client`` / ``api-server`` /
    ``schema`` / ``support``.
    """

    path: str
    layer: str = "support"
    depth: int = 0
    side_effect_only: bool = False
    symbols: list[SymbolRange] = []
    role: str | None = None  # optional human-readable role hint


class SharedParticipant(BaseModel):
    """A file a feature USES but does not own (Sprint 8 / 9).

    When the aggregator classifier deletes a "shared-aggregator"
    feature (a multi-domain DTOs package, a shared-UI primitives lib),
    each of its files is redistributed as a ``SharedParticipant`` on
    every product feature that imports it. The same file can appear
    on N features — that's the point: a Button.tsx used by 10
    features should show up on all 10.

    Distinguished from ``Feature.paths`` (which stays 1:1 file→feature
    for owned source code, used by blame / commit-attribution).

    ``role``:
        - ``consumer``: feature imports the file from elsewhere
        - ``co-owner``: redistribution found multiple consumers and
          the file is essential to several
    ``line_weight`` (0.0–1.0) carries forward existing line-scoped
    attribution. Defaults to 1.0 when fine-grained data isn't
    available. ``origin_feature`` retains the deleted aggregator's
    name so the dashboard can show provenance ("from: Shared API
    Schemas") without losing it.
    """

    file_path: str
    role: str = "consumer"  # "consumer" | "co-owner"
    line_weight: float = 1.0
    origin_feature: str | None = None


class FlowSharedPath(BaseModel):
    """W4 (Product-Spine §4.6) — one cross-PF shared file of a flow.

    Emitted by the flow-span split (``flow_span_split.py``): the file
    stays part of the flow's STORY but its ownership lives in another
    product feature, so it is surfaced as labeled sharing instead of
    polluting the primary ``Flow.paths`` projection (validator I15's
    attach-overlap ruler divides by those).
    """

    path: str
    owner_product_feature: str | None = None  # PF key (id or name)
    owner_display: str | None = None
    reason: str = "cross_pf_span"


class SymbolAttribution(BaseModel):
    file_path: str                          # the shared file
    symbols: list[str]                      # symbol names attributed to this feature
    line_ranges: list[tuple[int, int]]      # merged non-overlapping (start, end) spans
    attributed_lines: int                   # total lines across all ranges
    total_file_lines: int                   # total lines in the file
    roles: dict[str, str] = {}              # {symbol_name: role}; role in {entry,handler,validator,data-fetch,state,loading-state,error-state,side-effect,ui-component,helper,type}
    # Sprint 2 Day 10: multi-attribution badge data — names of OTHER
    # flows (within the same feature) that also reference at least one
    # of these symbols. UI surfaces "shared with N flows" so a reader
    # knows the attributed code participates in multiple journeys.
    # The bug-ratio / coverage credit is NOT split — every flow gets
    # full credit per user spec ("a"). The badge is purely a hint.
    shared_with_flows: list[str] = []


class FlowNode(BaseModel):
    """One node in an expanded flow's call graph (Sprint 2).

    Emitted by Stage 3.5 (flow expansion). Each node represents a
    callable unit — typically a function/method/route-handler — or
    an aggregation marker (``deep_call_subtree``) when fan-out
    exceeds the per-flow cap.

    ``kind`` values:
      - ``entry``               — the flow's starting symbol.
      - ``function``            — intra-repo function reached via
                                  T1 import + identifier match.
      - ``route_handler``       — server-side endpoint reached via
                                  T2 cross-stack HTTP match.
      - ``fetch_call``          — client-side HTTP call site (T2
                                  source).
      - ``file``                — file-level fallback when no symbol
                                  resolution is possible (graceful
                                  degrade for unsupported stacks).
      - ``deep_call_subtree``   — aggregation marker emitted when
                                  ``len(nodes) >= max_nodes_per_flow``;
                                  ``count`` carries the dropped node
                                  count for telemetry.

    ``role`` values:
      - ``entry``                — exactly one per flow.
      - ``called``               — reached via T1 intra-repo edges.
      - ``support``              — non-callable participant.
      - ``cross_stack_client``   — T2 client-side ``fetch_call``.
      - ``cross_stack_server``   — T2 server-side ``route_handler``.

    ``confidence``:
      - ``high``    — symbol resolved deterministically.
      - ``medium``  — file-level resolution only.
      - ``low``     — parse/match failure; emitted defensively.
    """

    id: str                                # "<file>#<symbol>" or "<file>"
    kind: Literal[
        "entry", "function", "route_handler",
        "fetch_call", "file", "deep_call_subtree",
    ]
    file: str
    symbol: str | None = None
    lines: tuple[int, int] | None = None
    role: Literal[
        "entry", "called", "support", "shared",
        "cross_stack_client", "cross_stack_server", "interior",
    ]
    confidence: Literal["high", "medium", "low"] = "medium"
    count: int | None = None               # only set for deep_call_subtree
    fan_in: int | None = None              # only set for role=shared


class FlowEdge(BaseModel):
    """One directed edge in an expanded flow's call graph (Sprint 2).

    ``kind`` values:
      - ``import``            — A imports B (T1 file-level edge).
      - ``call``              — A's body references B by identifier
                                (T1 intra-repo call edge).
      - ``cross_stack_http``  — A is a ``fetch_call`` node whose URL
                                literal matched a route in
                                ``routes_index`` (T2 cross-stack edge).
    """

    # ``from`` is a Python keyword — pydantic supports it via alias.
    from_: str = Field(alias="from")
    to: str
    kind: Literal["import", "call", "cross_stack_http"]
    confidence: Literal["high", "medium", "low"] = "medium"

    model_config = {"populate_by_name": True}


class FlowSummary(BaseModel):
    """Roll-up counters for one expanded flow (Sprint 2)."""

    total_nodes: int = 0
    total_files: int = 0
    total_lines_touched: int = 0
    cross_stack_hops: int = 0
    max_depth: int = 0
    unsupported_stack: bool = False
    truncated: bool = False


class FlowEntryPoint(BaseModel):
    """Phase 5 — richer entry-point object for a Flow.

    Additive over the legacy scalar ``Flow.entry_point_file`` /
    ``Flow.entry_point_line`` (both preserved). Mirrors the
    ``{path, symbol, line}`` shape the landing app expects so a Flow's
    entry renders at parity with a Feature's.
    """

    path: str
    symbol: str | None = None
    line: int | None = None


class FlowLineRange(BaseModel):
    """Phase 5 — one (path, start_line, end_line) span a Flow covers.

    The flow's own line span, merged per file into non-overlapping
    ranges. Derived from the Stage 3.5 node ``lines``.
    """

    path: str
    start_line: int
    end_line: int


class FlowLocSymbolAttribution(BaseModel):
    """Phase 5 — full per-participant symbol attribution for a Flow.

    Same shape a Feature emits (``path`` / ``symbol`` / ``kind`` /
    ``start_line`` / ``end_line``). Widens the thin
    :class:`FlowSymbolAttribution` (which uses ``file`` / ``line_start``
    / ``line_end`` / ``role``) into the landing-app contract while the
    original field stays untouched. ``kind`` carries the node kind
    (``entry`` / ``function`` / ``route_handler`` / ``fetch_call`` /
    ``support`` / ``file``) so consumers can distinguish a resolved
    function from a file-level fallback.
    """

    path: str
    symbol: str | None = None
    kind: str = "function"
    start_line: int | None = None
    end_line: int | None = None
    role: str | None = None


class FlowLocNode(BaseModel):
    """Phase 5 — call-graph node in the landing-app shape.

    Projection of :class:`FlowNode` onto ``{path, symbol, start_line,
    end_line, role}``. The original :class:`FlowNode` (``id`` / ``kind``
    / ``file`` / ``lines`` / ``confidence``) is preserved on
    ``Flow.nodes``; this is the additive parity view.
    """

    path: str
    symbol: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    role: str


class FlowLocEdge(BaseModel):
    """Phase 5 — caller→callee edge carrying the call-site.

    Projection of :class:`FlowEdge` that resolves the abstract
    ``from``/``to`` node ids back to their files/symbols and attaches a
    best-effort call-site ``{path, line}`` (the caller node's file and
    its start line — the most precise deterministic anchor available
    without re-parsing the AST). The original :class:`FlowEdge` stays on
    ``Flow.edges``.
    """

    from_path: str
    from_symbol: str | None = None
    to_path: str
    to_symbol: str | None = None
    kind: str
    call_site: dict[str, Any] | None = None  # {"path": str, "line": int | None}


class MemberFile(BaseModel):
    """One file-membership claim on a feature (Stage 2.6, 2026-06).

    The ownership model is PRIMARY + SHARED: ``Feature.paths`` stays the
    exclusive primary-membership list (back-compat — metrics, scoring
    and commit attribution read it), while ``member_files`` carries the
    full provenance of every claim, including non-primary ones. A file
    may appear in many features' ``member_files`` but has at most ONE
    primary owner across the scan (``primary=True`` exactly on the
    feature whose ``paths`` carries it).

    Roles:
      - ``anchor``    — file attributed by a Stage 1/2 deterministic
                        extractor (the feature's own declared surface).
      - ``closure``   — file reached by the Stage 2.6 static-import BFS
                        from the feature's anchor files.
      - ``co-commit`` — file attached by the Stage 2.6 git co-commit
                        signal (changes land together with the anchors).
      - ``url-link``  — frontend file attached by the Stage 2.6
                        URL-literal linker: its fetch/axios/api-client
                        string literals match the feature's backend
                        route templates (cross-language — no import
                        edge exists).
      - ``shared``    — file whose import fan-in marks it as shared
                        infrastructure (claimed by many features); it is
                        recorded for provenance but NOT attached to any
                        claimant's ``paths``. URL-channel shared files
                        are shared API CLIENTS (they call many
                        features' routes).

    ``confidence`` ∈ (0, 1]: anchors are 1.0; closure claims decay with
    import depth (1 / (1 + depth)); co-commit claims carry the observed
    co-change share capped below the weakest direct-import claim;
    url-link claims are fixed at 0.4 (textual match — below a direct
    import AND below the co-commit cap).
    ``evidence`` is a short human/agent-readable justification.
    """

    path: str
    role: Literal["anchor", "closure", "co-commit", "url-link", "shared"]
    confidence: float
    evidence: str = ""
    primary: bool = False
    # Stage 6.97 (2026-07-05) — per-file executable-line count for THIS
    # file (test / generated / lockfile / binary → 0). Populated from the
    # scan-wide per-file LOC cache; ``None`` on scans produced before the
    # stage existed (old JSONs rehydrate unchanged). Provenance-level count:
    # the SAME file carries the same ``loc`` on every claimant's ledger — it
    # is NOT the feature's owned share (see ``Feature.loc`` for that).
    loc: int | None = None


class FlowSymbolAttribution(BaseModel):
    """One line-range attribution for a flow (Sprint C2) or feature
    (Sprint C3b — feature-level ``Feature.symbol_attributions``).

    Distinct from the legacy :class:`SymbolAttribution` (which is a
    per-file aggregate across multiple symbols, attached to
    ``Feature.shared_attributions``). This is the per-symbol surface
    spec from ``flow-feature-concept``: per file, the exact symbol +
    line range participating in this specific narrative.

    Roles:
      - ``entry``            — the flow's entry function (exactly one
                               per flow when entry detection succeeds).
      - ``called``           — function reached via import + identifier-
                               match from the entry-symbol body.
      - ``support``          — file in the C1 reach set without a
                               resolved symbol; line range covers the
                               whole file (line_start=1, line_end=LOC).
      - ``anchor-consumer``  — (C3b) reverse-import seed for a
                               package-anchor feature.
      - ``schema-consumer``  — (C3b) reverse-import seed for a
                               schema-source feature.
      - ``structural``       — (C3b) dominant-symbol fallback seed
                               for a feature with no flows and no
                               reverse-anchor rationale.
      - ``framework-link``   — (C4) deterministic cross-file edge
                               emitted by a Stage 6.4
                               :class:`framework_linkers.FrameworkLinker`
                               (Next.js fetch URL → route.ts handler,
                               Server Action call → ``"use server"``
                               file, etc.). The ``symbol`` field encodes
                               ``framework-link:<kind>:<target-symbol>``
                               so consumers can route on link kind
                               without growing the role enum further.
      - ``branch``           — (D2) intra-symbol conditional region
                               extracted via tree-sitter AST. The
                               ``symbol`` field encodes
                               ``branch:<kind>:<parent-symbol>__b<i>``
                               where ``kind`` ∈ ``{if, else, ternary,
                               switch_case, switch_default, try, catch,
                               finally, match_arm}``. The conditioning
                               text (e.g. ``role === 'admin'``) is
                               appended after the symbol via
                               ``::<condition>`` so consumers can route
                               on it without growing the schema.
      - ``shared``           — a direct callee that is SHARED
                               INFRASTRUCTURE: its symbol is called by
                               many DISTINCT flow entry-points across
                               the whole scan (high fan-in — e.g. a DB
                               session opener, a registry, a generic
                               validator/logger). Recorded so the
                               dashboard can render a shared-dependency
                               badge, but EXCLUDED from the flow's core
                               LOC. ``fan_in`` (on FlowNode) carries the
                               distinct-caller count. Per
                               ``flow-feature-concept``: sharing is
                               normal — surface it, don't delete it.
      - ``interior``         — (W4, Product-Spine §4.6) a PRODUCT
                               component the flow's entry PAGE renders,
                               attributed at the component's DEFINITION
                               span in its own source file (1-hop
                               imported product-module span). Emitted by
                               Stage 6.55 ``refine_flow_spans``;
                               design-system primitives never get one
                               (import-provenance filter).
    """

    file: str                  # repo-relative path
    symbol: str                # exported / local symbol name, or "<file>" for support
    line_start: int            # 1-indexed, inclusive
    line_end: int              # 1-indexed, inclusive
    role: Literal[
        "entry", "called", "support", "shared",
        "anchor-consumer", "schema-consumer", "structural",
        "framework-link", "branch", "interior",
    ]


class HistoryPoint(BaseModel):
    """One ISO-week bucket of an entity's git-history timeline (Stage 6.95).

    Sparse series — weeks with zero attributed commits are OMITTED from
    ``EntityHistory.weekly`` (the dashboard interpolates gaps). All
    counts are over the commits ATTRIBUTED to the entity's resolved
    file set (current member paths applied retroactively — a known and
    accepted approximation, see ``EntityHistory.history_confidence``).
    """

    week: str               # ISO week label "YYYY-Www" (same convention as TimelinePoint.date)
    commits: int            # attributed commits this week
    bug_fixes: int          # attributed commits with Commit.is_bug_fix
    bugfix_share: float     # bug_fixes / commits, rounded to 3 dp
    test_commits: int       # attributed commits touching >=1 of the entity's test files
    files_touched: int      # distinct entity files touched this week
    health_lite: float      # git-only trailing composite — see EntityHistory docstring


class CrossCutNote(BaseModel):
    """Correlation note (NOT causation) attached to a
    ``cross_cut_emerged`` event (Stage 6.95).

    Compares the entity's ``bugfix_share`` over attributed commits
    BEFORE the emergence week vs AFTER (emergence week inclusive in the
    after window), using the SAME pooled two-proportion standard-error
    band as ``TestEfficacy`` — sample-size-aware, no tuned threshold.
    Verdict values are deliberately descriptive ("the share moved"),
    never causal ("the cross-cut broke it"): the emergence week itself
    is a retroactive projection of TODAY's file sets, so this field
    only says "bug-fix share was higher / lower after that week",
    nothing more.

    ``insufficient_data`` discipline mirrors ``TestEfficacy``: an empty
    window on either side of the emergence week yields no verdict.
    Additive — events serialized before this field existed rehydrate
    with ``correlation_note=None``.
    """

    verdict: Literal[
        "bugfix_share_up", "bugfix_share_down", "no_change",
        "insufficient_data",
    ]
    bugfix_share_before: float | None = None
    bugfix_share_after: float | None = None
    commits_before: int = 0
    commits_after: int = 0
    reason: str | None = None         # populated for insufficient_data


class HistoryEvent(BaseModel):
    """A notable point on an entity's timeline (Stage 6.95 / 6.96).

    ``kind`` values:
      - ``birth``           — first week any current member file was touched.
      - ``first_test``      — first week with >=1 test commit.
      - ``test_wave``       — start week of a contiguous run of weeks whose
                              ``test_commits`` exceed the entity's OWN
                              75th percentile of nonzero weekly test
                              activity (scale-invariant by construction).
      - ``hotspot_emerged`` — first week a member file's cumulative
                              bug-fix ratio crosses the SAME universal
                              hotspot thresholds Stage 6 uses
                              (``HOTSPOT_BUG_RATIO_MIN`` /
                              ``HOTSPOT_COMMITS_MIN``).
      - ``coupling_spike`` / ``decoupled`` — Stage 6.96: the entity's
                              import-graph blast radius (``impact``
                              series) jumped / fell between two
                              consecutive snapshots by more than the
                              scan's OWN pooled distribution of
                              relative reach deltas allows — see
                              ``stage_6_96_impact`` for the exact
                              scale-invariant rule. ``detail`` carries
                              ``reach a->b``. Additive enum values:
                              old JSONs never contain them.
      - ``cross_cut_emerged`` — Stage 6.95: the week a TODAY-cross-cutting
                              flow's relationship with this entity
                              MATERIALIZED historically — the latest
                              first-touch week among the files the flow
                              shares with the entity (i.e. when the
                              shared file-set first fully existed; the
                              week that completes it is by construction
                              also a co-touch week). Shared files never
                              touched inside the scan window predate it
                              and resolve to the birth week — the same
                              convention ``history_confidence`` uses.
                              Retroactive projection of TODAY's
                              bipartite store (Stage 5.5), like
                              everything in history v1. ``detail``
                              names the flow; ``correlation_note``
                              carries the SE-gated before/after
                              bug-fix-share comparison. Additive enum
                              value: old JSONs never contain it.
    """

    kind: Literal[
        "birth", "first_test", "test_wave", "hotspot_emerged",
        "coupling_spike", "decoupled", "cross_cut_emerged",
    ]
    week: str
    detail: str | None = None
    # Stage 6.95 (2026-06-12) — populated ONLY for
    # ``cross_cut_emerged`` events. None for every other kind and for
    # events serialized before this field existed (additive).
    correlation_note: "CrossCutNote | None" = None


class ImpactPoint(BaseModel):
    """One historical snapshot of an entity's import-graph blast radius
    (Stage 6.96 — impact-over-time).

    ``reach`` = number of source files OUTSIDE the entity's member set
    that import >=1 member file, computed at the snapshot commit's file
    tree with the SAME lean resolver at every snapshot (consistency
    across the series matters more than absolute precision). The member
    set is TODAY's entity paths projected retroactively — consistent
    with Stage 6.95; ``members_present`` records how many of those
    member files actually exist at the snapshot (the honesty counter
    for that projection: members not yet born contribute nothing).
    """

    week: str               # ISO week label "YYYY-Www" of the snapshot commit
    reach: int              # external importer files at this snapshot
    members_present: int    # today's member files that exist at this snapshot


class TestEfficacy(BaseModel):
    """Correlation (NOT causation) between test introduction and the
    entity's bug-fix share (Stage 6.95).

    Compares ``bugfix_share`` over the attributed commits BEFORE the
    ``first_test`` week vs AFTER (pivot week inclusive in the after
    window). The verdict is gated scale-invariantly: entities below the
    median total-commit count among scored entities of the same kind
    (product feature vs user flow), or with an empty before/after
    window, emit ``insufficient_data``. ``improved`` / ``worsened``
    require the share delta to exceed the pooled two-proportion
    standard error — a sample-size-aware band instead of a tuned
    magic threshold. This field says "bug-fix share dropped after
    tests appeared", never "tests caused the drop".
    """

    verdict: Literal["improved", "worsened", "no_change", "insufficient_data"]
    bugfix_share_before: float | None = None
    bugfix_share_after: float | None = None
    commits_before: int = 0
    commits_after: int = 0
    pivot_week: str | None = None     # the first_test week the windows split on
    reason: str | None = None         # populated for insufficient_data


class EntityHistory(BaseModel):
    """Per-entity git-history timeline (Stage 6.95, v1 slice).

    Derived ENTIRELY from the single git pass already in memory
    (``ScanContext.commits``) — no historical checkouts, no LLM, no
    cross-run persistence (cold-scan rule). Attached to product
    features and user flows.

    ``weekly[*].health_lite`` is a GIT-ONLY composite and must not be
    confused with the full ``health_score``: it reuses the same
    logistic curve over bug-fix share (centred at 0.55, steepness 8)
    and the same activity damping, but computes them over a TRAILING
    13-ISO-week (one calendar quarter) window ending at each emitted
    week, and omits the full formula's scan-time age-decay (recency
    relative to "now" is meaningless for a historical point — the
    trailing window IS the recency element).

    ``history_confidence`` is the share of current member files whose
    FIRST touch in the scan window falls within the first quarter
    (birth → birth + 25% of the active span) of the timeline. Files
    present in HEAD but never touched inside the window predate it and
    count as existing. Low values mean the entity's current file set
    mostly did not exist early on, so early-timeline buckets are a
    retroactive approximation — read them with care.
    """

    birth_week: str
    weekly: list[HistoryPoint] = []
    events: list[HistoryEvent] = []
    test_efficacy: TestEfficacy
    history_confidence: float = 0.0
    # Stage 6.96 (2026-06-11) — impact-over-time: the entity's
    # import-graph blast radius at N historical git snapshots
    # (default 8, evenly spaced over the scan window). Empty for scans
    # produced before this field existed (old JSONs rehydrate
    # unchanged) and when the snapshot runner was skipped. Additive.
    impact: list[ImpactPoint] = []
    # First-vs-last drift of the ``impact`` series, banded by the
    # scan's own pooled per-step delta distribution (None when the
    # series has < 2 points or on pre-6.96 scans). Additive.
    impact_trend: Literal["growing", "shrinking", "stable"] | None = None


class Feature(BaseModel):
    name: str
    # Title Case display label derived from ``name`` (or set explicitly
    # by post-processing). Dashboards and reports show this; ``name``
    # stays a stable slug used for dedup / config lookups / IDs.
    display_name: str | None = None
    description: str | None = None  # LLM-generated semantic description
    paths: list[str]          # directories/files belonging to this feature
    authors: list[str]        # contributors
    total_commits: int
    bug_fixes: int            # number of bug fix commits
    bug_fix_ratio: float      # bug_fixes / total_commits
    last_modified: datetime
    health_score: float       # 0-100, higher is better
    # 2026-06 metric-honesty review — how much commit evidence backs
    # ``health_score``. The score stays numeric (dashboard + MCP read
    # it as a number), but a zero-commit feature scores 100.0 by
    # construction, which is NOT the same as a battle-tested 100.
    #   - "insufficient": 0 attributed commits — score is a placeholder
    #   - "low":  fewer commits than the repo's P25 of nonzero
    #     per-feature commit counts (scale-invariant floor, no magic N)
    #   - "high": at or above that floor
    # Defaults to "low" so scans serialized before this field existed
    # rehydrate without loss and without over-claiming confidence.
    health_confidence: Literal["high", "low", "insufficient"] = "low"
    flows: list[Flow] = []    # populated when --flows flag is used
    # Sprint 2026-05-28 — per-feature hotspot files (dict shape). Each
    # entry: path + bug_fix_ratio + bug_fixes + total_commits. Sorted
    # by ratio desc, then total_commits desc. Emitted by
    # ``stage_6_metrics`` for every layer (developer + product). Empty
    # when no file in this feature crosses the universal thresholds.
    hotspot_files: list[HotspotFile] = []
    bug_fix_prs: list[PullRequest] = []
    coverage_pct: float | None = None  # avg line coverage % across source files; None if unavailable
    shared_attributions: list[SymbolAttribution] = []  # symbol-scoped data for shared files
    # Sprint C3b (2026-05-20) — feature-level per-symbol attributions.
    # Stage 6.3's whole-import-tree enrichment surface, populated as
    # the union of every reached (file, symbol) pair for THIS feature
    # (whether the feature has flows or not). For flow-bearing features
    # this is the union of flow.flow_symbol_attributions + the
    # feature's own seed-based attributions; for flow-less features
    # (e.g. package-anchor Billing) this is the ONLY surface that
    # carries the enrichment payload — ``flows[*].flow_symbol_attributions``
    # is empty in that case. Per-symbol shape matches the existing
    # flow-level :class:`FlowSymbolAttribution` consumed by the
    # landing app (file / symbol / line_start / line_end / role).
    # ``shared_attributions`` (legacy per-file aggregate schema)
    # remains populated for back-compat.
    symbol_attributions: list["FlowSymbolAttribution"] = []
    # Refactor Day 1: participants — every file (with line ranges and
    # role) imported transitively from any of this feature's source
    # files. Built by analyzer.feature_participants.build_feature_participants
    # via SymbolGraph BFS. Replaces ``shared_attributions`` as the
    # primary attachment surface for line-scoped scoring; the older
    # field stays populated for back-compat callers but the
    # cross-feature gate ("file in 2+ features") no longer disables
    # symbol-scoped health and coverage.
    participants: list["FlowParticipant"] = []
    symbol_health_score: float | None = None           # health score weighted by symbol line ranges
    # Sprint 8/9: files this feature CONSUMES from a deleted aggregator
    # (DTO packages, shared-UI primitives, schema crates). Same file
    # can appear on multiple features. ``paths`` stays the 1:1
    # owned-file list for blame / commit-attribution; this list is
    # the additive N:M overlay.
    shared_participants: list[SharedParticipant] = []
    # Layer 1/2 split (introduced 2026-05-18 on agent/layer1-dev-features-v1):
    # every Feature is either a developer feature (code-grounded, Layer 1)
    # or a product feature (marketing/docs-grounded, Layer 2). All
    # legacy `features[]` entries default to ``"developer"`` so old
    # scans rehydrate as Layer 1 without loss.
    layer: Literal["developer", "product"] = "developer"
    # When this feature is a developer feature that rolls up under a
    # product feature, this is the parent ``Feature.name`` slug.
    # ``None`` for product features themselves and for orphan developer
    # features that have no Layer 2 parent.
    product_feature_id: str | None = None
    # Sprint 1 (2026-05-23) — stable lineage UUID + rename / split /
    # merge bookkeeping. See ``faultline.pipeline_v2.lineage``.
    # Defaults to "" so legacy serialized scans rehydrate without loss.
    uuid: str = ""
    previous_names: list[str] = []
    split_from: str | None = None
    merged_from: list[str] = []
    # Stage 6.95 (2026-06-11) — per-entity git-history timeline.
    # Populated ONLY on product (Layer 2) features by the history
    # stage; ``None`` on developer features, on product features with
    # zero attributed commits, and on every scan produced before this
    # field existed (old JSONs rehydrate unchanged). Additive.
    history: "EntityHistory | None" = None
    # Stage 2.6 (2026-06) — file-membership provenance. ``paths`` stays
    # the exclusive PRIMARY membership surface; this is the additive
    # per-file claim ledger (anchor / closure / co-commit / shared)
    # consumed by the dashboard + agents. Empty on scans produced
    # before the stage existed and on Stage-4 residual features.
    member_files: list["MemberFile"] = []
    # Naming-evidence core (2026-06) — how trustworthy ``name`` /
    # ``display_name`` are. "low" when the anti-hallucination validator
    # fell back to a deterministic slug, when the evidence bundle was
    # structurally poor, or when the scan ran LLM-degraded (see
    # ``scan_meta.llm_degraded``). Defaults to "high" so old JSONs
    # rehydrate unchanged — "low" is an explicit degradation marker.
    name_confidence: Literal["high", "low"] = "high"
    # Phase 3 dual-evidence (2026-07): {"code": [paths], "anchors": [{text,source,
    # locator}], "confidence": 0-1} — code + product-source corroboration, attached
    # deterministically by dual_evidence.py. Additive/optional; None when not computed.
    dual_evidence: dict[str, Any] | None = None
    # Stage 6.97 (2026-07-05) — deterministic feature-level LOC over OWNED
    # ``paths`` (executable-line convention from tools.line_completeness;
    # test / generated / lockfile / binary files excluded from the COUNT
    # but never removed from ``paths``). Product features carry the
    # member-dev rollup with shared files counted once. ``None`` on scans
    # produced before the stage existed (old JSONs rehydrate unchanged);
    # the dashboard prefers this flat field over the flow-span rollup.
    #
    # OWNED vs SHARED (2026-07-05 loc-truth fix): ``loc`` is the feature's
    # OWNED line count — files attributed ONLY to this feature PLUS the
    # shared files for which this feature is the deterministic PRIMARY
    # owner, each counted ONCE. Shared files owned by a sibling are NOT
    # summed here; their lines live in ``loc_shared`` (visible, not double
    # counted). This makes ``sum(product_features[].loc)`` a real repo-size
    # figure instead of the historical N× inflation.
    loc: int | None = None
    # Stage 6.97 (2026-07-05) — SHARED line count: lines in files this
    # feature references/touches but does NOT primarily own (they are the
    # primary owner's ``loc``). Visibility metric only — never summed into
    # ``loc``. ``None`` on scans produced before the split existed.
    loc_shared: int | None = None
    # Product-Spine §4.1 (2026-07-06) — cross-cutting role marker.
    # ``"facet"`` when this developer feature is a concern facet (a
    # horizontal view — auth/i18n/email/… spanning multiple route/workspace
    # subtrees): it keeps existence for dashboards, but is excluded from
    # product-feature membership and never carries LOC into a PF (see
    # ``pipeline_v2.spine_hygiene``). ``None`` for ordinary features and for
    # every scan produced before the field existed; omitted from dumps when
    # unset so pre-spine scans serialize byte-identically.
    role: str | None = None
    # Product-Spine §4.5 (2026-07-06) — conservation flow-LOC accounting
    # (product features only; stamped by Stage 6.97 when user flows are
    # available). ``loc_flow`` counts the UNION of member-journey flow spans
    # that lie INSIDE this PF's dev closure, per-file clipped at the file's
    # own LOC — so ``loc_flow <= loc`` (on-flow ≤ 100%) BY CONSTRUCTION.
    # ``loc_flow_shared`` counts span lines landing OUTSIDE the closure (the
    # shared channel — visible, never summed into ``loc_flow``). Both omitted
    # from dumps when unset (old scans / dev features serialize unchanged).
    loc_flow: int | None = None
    loc_flow_shared: int | None = None
    # Product-Spine §4.2 (Wave 2a, 2026-07-06) — product-surface taxonomy
    # tag: ``product | marketing | docs | legal | system | dev_tooling |
    # shell`` (see ``pipeline_v2.surface_taxonomy``). Stamped on every
    # developer AND product feature by Stage 6.85; validator I20 activates
    # on its presence (no PF in the product list may carry a non-product
    # scope — those rows move to the ``non_product_surfaces[]`` lane).
    # ``None`` (omitted from dumps) on scans that predate the field or run
    # with FAULTLINE_SURFACE_TAXONOMY=0.
    surface_scope: str | None = None
    # Product-Spine §4.3 (Wave 2a) — shared-resident explainability
    # (validator I22): every dev feature bound to the shared/platform
    # bucket carries a machine-readable residency reason
    # (``no_anchor_lineage | genuinely_shared_infra | facet_view |
    # awaiting_wave2_mint | non_product_surface | sub_mint_bar_surface |
    # shell_lineage_only``). ``None`` (omitted) everywhere else.
    shared_reason: str | None = None
    # Product-Spine §4.3/§4.8 (Wave 2b, 2026-07-06) — anchor lineage.
    # On a DEVELOPER feature: the canonical id of the anchor its
    # membership derives from (``route:/settings``, ``ws:apps/web``,
    # ``schema:invoice``, ``hub:backend/services/edr/cortex``,
    # ``fdir:frontend/src/features/anomalies``, …) or a fold provenance
    # (``fold:import->route:/invoices``). On a PRODUCT feature: the
    # anchor that minted it. ``None`` (omitted from dumps) on scans that
    # predate the field, on FAULTLINE_SPINE_ANCHORED_MINT=0 scans, and
    # on shared-bucket residents (their explanation is ``shared_reason``).
    anchor_id: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_spine_fields(self, handler: Any) -> Any:
        """Drop spine-era optional fields from dumps when unset.

        Scans that predate the Product-Spine fields (and every entity the
        spine passes leave untouched — e.g. non-facet features, dev-layer
        rows without flow accounting) must serialize byte-identically to
        engines that predate the fields (snapshot-gate digest contract).
        """
        data = handler(self)
        if isinstance(data, dict):
            for key in ("role", "loc_flow", "loc_flow_shared",
                        "surface_scope", "shared_reason", "anchor_id"):
                if data.get(key) is None:
                    data.pop(key, None)
        return data


class FeatureFlowEdge(BaseModel):
    """One edge in the bipartite feature ↔ flow graph (Sprint B1).

    Each flow has exactly ONE ``primary`` edge (mirroring
    ``Flow.primary_feature``) and zero-or-more ``secondary`` edges
    (cross-cutting attachments derived from path overlap).

    ``feature`` is a ``Feature.name`` slug; ``flow_id`` is the global
    ``Flow.id`` (``"{primary_feature}::{slug}"``). ``reason`` is
    informational for secondary edges and ``None`` for primary edges.
    """

    feature: str
    flow_id: str
    type: Literal["primary", "secondary"]
    reason: str | None = None


class UserFlow(BaseModel):
    """A product-grain User Flow (UF) — UF-Stage1 (2026-06-02).

    The Layer-2-for-flows projection: several code-grain ``Flow`` rows
    that share a ``(domain, intent)`` cluster key roll up into one
    user-facing journey ("Create & edit detectors"). Symmetric to the
    existing ``developer_features[].product_feature_id → product_features[]``
    model — each member ``Flow`` points back via ``Flow.user_flow_id``.

    Stage 6.7 names the UF from a deterministic template (no LLM).
    Stage 6.7b (additive Haiku refiner) overwrites ``name`` with a
    journey label, fills ``description`` / ``ui_tier``, resolves
    ``intent`` for "other" clusters, and drafts ``acceptance`` from
    test-reached members. Membership/grain are NOT changed — Stage 6.7
    stays the source of truth. ``refined`` flags a successful pass.
    """

    id: str                              # "UF-001" — stable within a scan
    name: str                            # journey label (template, or LLM-refined in 6.7b)
    description: str | None = None       # journey-grain description (Stage 6.7b LLM refiner)
    domain: str | None = None            # code-grain cluster key (router/folder/module token)
    product_feature_id: str | None = None  # Layer-2 grouping LINK (marketing product feature), member-majority vote — NOT the code-grain domain
    intent: str                          # author|browse|lifecycle|execute|manage|bulk|export|other
    resource: str                        # representative noun ("detector")
    member_flow_ids: list[str] = []      # composing code-flows (uuid or name)
    member_count: int = 0
    routes: list[str] = []               # union of members' router paths
    cross_links: list[str] = []          # other product_feature_ids touched
    ac_draft_count: int = 0              # # members with test_files (AC reach)
    acceptance: list[str] = []           # "AC-n" first-draft observable assertions (Stage 6.7b)
    coverage_pct: float | None = None    # mean of members' coverage_pct
    ui_tier: str | None = None           # full-page|panel|settings|admin|no-ui (Stage 6.7b)
    # Stage 6.8b (2026-06-14) — system/background-flow classification. A UF is
    # "system" when its member flows are predominantly triggered by a scheduler,
    # queue/worker, or inbound webhook rather than interactive navigation;
    # ``trigger`` carries the dominant system sub-type. Deterministic, from
    # eval/system-flow-patterns.yaml. Old JSONs rehydrate as "interactive".
    category: Literal["interactive", "system"] = "interactive"
    trigger: str | None = None           # scheduled|queue|webhook when category="system"
    refined: bool = False                # True when Stage 6.7b LLM refined this UF
    # Stage 6.95 (2026-06-11) — per-entity git-history timeline. None
    # for UFs with zero attributed commits and for scans produced
    # before this field existed (old JSONs rehydrate unchanged).
    history: "EntityHistory | None" = None
    # Naming-evidence core (2026-06) — see ``Feature.name_confidence``.
    # "low" when the UF name was assembled from a structurally poor
    # evidence bundle (no route / nav-label / product-string vocabulary
    # for the primary member flow), when the Stage 6.7b validator fell
    # back to the deterministic template name, or when the scan ran
    # LLM-degraded. "medium" (B5, 2026-07) marks a backstop journey whose
    # name was re-derived from a STRONG multi-member evidence agreement
    # (≥2 members concur on resource + action) in ``synth_quality`` —
    # confident enough to drop the "~" hedge, but code-derived rather than
    # authored. Additive tier: downstream consumers that only special-case
    # "low" (landing viewer, dashboard ``parseNameConfidence``) read
    # "medium" as confident. Defaults to "high" for old-JSON rehydration.
    name_confidence: Literal["high", "medium", "low"] = "high"
    # B40 (2026-07-11) — provenance audit trail for ``name_confidence``. The
    # ordered list of evidence RUNGS the naming rubric (naming_contract Law C +
    # synth_quality's multi-member agreement) actually fired for this journey's
    # name — e.g. ``["structural-route"]`` (member-flow resource+verb theorem),
    # ``["nav"]`` (author's nav label corroborated the resource),
    # ``["registry"]`` (all members are maintainer-declared dispatch mints),
    # ``["member-agreement"]`` (>=2 members concur on resource+action). A LOW
    # row instead lists the rungs that were MISSING (prefixed ``missing:``,
    # e.g. ``["missing:resource", "missing:verb"]`` / ``["missing:members"]``)
    # so every residual low is self-explaining. Confidence is a statement of
    # evidence: an uplift is stamped ONLY when its rung genuinely fired.
    # ``None`` (and OMITTED from the serialized JSON — see
    # ``_omit_none_identity``) unless ``FAULTLINE_NAME_EVIDENCE_RUNGS`` is on,
    # keeping default output byte-identical to engines that predate the field.
    name_evidence: list[str] | None = None
    # Phase 3 dual-evidence (2026-07): {"code": [paths], "anchors": [{text,source,
    # locator}], "confidence": 0-1} — code + product-source corroboration, attached
    # deterministically by dual_evidence.py. Additive/optional; None when not computed.
    dual_evidence: dict[str, Any] | None = None
    # Cross-scan identity keeper (2026-07-05, opt-in via an EXPLICIT
    # ``--prev-scan`` artifact — see ``uf_identity_keeper.py``). When this
    # UF was matched to a UF of the previous scan, carries
    # ``{"pinned_from": <prev run_id>, "prev_id": ..., "match_basis":
    # "member"|"route"|"resource-intent", "overlap": float,
    # "renamed_prevented": bool}``. ``None`` (and OMITTED from the
    # serialized JSON — see ``_omit_none_identity``) on every scan that
    # ran without a prev-scan input, keeping default output byte-identical.
    identity: dict[str, Any] | None = None
    # Stage 6.7d PF-UF backstop (2026-07-05) — ``True`` for a THIN journey
    # the deterministic backstop appended because a flowful product feature
    # ended up with ZERO journeys referencing it (the operator's "фіча без
    # юзер-фловів" anomaly, validator invariant I8). ``synthesis_reason``
    # carries the subclass ("promoted_capability_backstop" — capability
    # minted by the residual-guard tier-2 promotion AFTER the draw;
    # "uncovered_product_feature_backstop" — draw coverage gap). EVAL
    # INTEGRITY: scorers / the surfaced tier MUST be able to exclude these
    # by tag — they exist for the board's completeness, never for recall.
    # Both fields are OMITTED from dumps when default (see the serializer)
    # so pre-existing scans and non-backstop UFs serialize byte-identically.
    synthesized: bool = False
    synthesis_reason: str | None = None
    # Product-Spine §4.2 (Wave 2a, 2026-07-06) — product-surface taxonomy
    # tag for this journey (``product | marketing | docs | legal | system |
    # dev_tooling``; journeys are never ``shell``). Voted from member-flow
    # entry paths + route patterns (see ``pipeline_v2.surface_taxonomy``).
    # ``None`` (omitted from dumps) on pre-W2a scans / taxonomy off.
    surface_scope: str | None = None
    # Product-Spine Wave 2a — no-signal terminal home (validator I21): when
    # a journey's PF binding came from the argmax fallback rather than a
    # strict-majority conservation accept, it is tagged ``"low"`` so
    # consumers can rank/inspect weak bindings. ``None`` (omitted) for
    # bindings that passed the majority bar.
    binding_confidence: str | None = None
    # B52 (2026-07-13) — flow-bearing transport lane (Option A): a
    # transport-INTRINSIC journey (its member flows live on a laned
    # transport residual dev — no product surface serves it) stays in
    # ``user_flows[]`` with ``product_feature_id=None`` and ``lane_ref``
    # pointing at the lane row's uuid (the laned dev's uuid — the same
    # value ``platform_infrastructure[].uuid`` carries). Contract:
    # ``product_feature_id=None`` is LEGAL iff ``lane_ref`` is set
    # (emission_integrity enforces; uf_terminal_home skips these rows
    # instead of argmax-homing them). Stamped ONLY by Stage 6.985 under
    # ``FAULTLINE_FLOWFUL_TRANSPORT_LANE``; ``None`` (OMITTED from dumps
    # — see the serializer) everywhere else, keeping default output
    # byte-identical to engines that predate the field.
    lane_ref: str | None = None
    # B3 (2026-07-08) — journey-level LOC. UNION of the OWNED line-range
    # spans across this UF's member flows (per-file merged; role="interior"
    # + shared_paths-ledger nodes excluded — mirrors the validator's
    # ``_spine_flow_loc_owned`` selection). Stamped by Stage 6.97b
    # (``stage_6_97b_uf_loc``), gated by ``FAULTLINE_UF_LOC`` (default ON).
    # A journey with zero resolvable member spans (e.g. an mc=0
    # system-recall placeholder) carries an HONEST ``0``, never null.
    # ``None`` (OMITTED from dumps — see the serializer) when the stage did
    # not run (kill-switch off / old JSON), keeping default output
    # byte-identical to engines that predate the field.
    loc: int | None = None
    # B13 (2026-07-09) — machine-readable coverage-marker flag. ``True`` for a
    # member-LESS I8-cover SEED (``synthesized=True`` + ``member_count=0``):
    # a route/LOC-coverage placeholder that exists ONLY so a journey-worthy PF
    # is not "фіча без юзер-фловів" (validator I8), NOT a real user journey.
    # Any viewer MUST render these as a gap-band / coverage row, never a
    # journey row. Set (with an honest ``Uncovered: <PF> routes`` name) by
    # ``synth_quality.honest_coverage_markers`` behind
    # ``FAULTLINE_BACKSTOP_OWNED_COVER``. OMITTED from dumps when ``False``
    # (default) so non-seed UFs / kill-switch-off / old JSON stay
    # byte-identical.
    is_coverage_marker: bool = False
    # B23 (2026-07-10) — REAL code coordinates for a member-less coverage
    # marker: its trigger surface's files as whole-file ``(path, 1, loc)``
    # spans (honest for an UNCOVERED surface — no flow ever traced a finer
    # grain there). Attached by ``synth_quality.attach_marker_surface_coords``
    # behind ``FAULTLINE_MARKER_SURFACE_COORDS`` from the files the mint
    # sites resolved (e2e route-family files / system route-group files /
    # home-PF member files as fallback), restricted to files NO flow
    # already claims — a marker must never re-attribute covered code.
    # Stage 6.97b stamps ``loc`` from these spans when the UF has no member
    # spans. ``None`` (OMITTED from dumps — see the serializer) on
    # non-markers, markers whose surface never honestly resolved (they stay
    # loc=0), kill-switch off, and old JSON — those dumps stay byte-identical.
    surface_files: list["FlowLineRange"] | None = None
    # B23 — mint-side candidate ledger: the RAW resolver/trigger file paths
    # a mint site had in hand (BEFORE the claimed-file filter and the loc
    # measurement). Pipeline plumbing only: consumed and cleared by
    # ``attach_marker_surface_coords``; NEVER serialized (field-level
    # ``exclude=True`` plus a defensive pop in the serializer).
    surface_candidate_files: list[str] | None = Field(default=None, exclude=True)
    # B31 — mint-side authored journey label (Track-C e2e orphan mints: the
    # maintainer's own playwright label, e.g. "Bulk Actions"). Carried so the
    # Stage-6.98 recall-row naming pass
    # (``synth_quality.distinct_recall_row_names``) can restore it when a
    # downstream naming channel (keyed persona verifier / labeler) reverted
    # the display to a colliding generic template. Pipeline plumbing only —
    # NEVER serialized (field-level ``exclude=True`` plus a defensive pop in
    # the serializer).
    authored_label: str | None = Field(default=None, exclude=True)

    @model_serializer(mode="wrap")
    def _omit_none_identity(self, handler: Any) -> Any:
        """Drop ``identity`` / backstop tags from dumps when unset.

        The keeper is strictly opt-in; scans without ``--prev-scan`` must
        serialize byte-identically to engines that predate the field
        (snapshot-gate digests + absent-input byte-identity contract).
        Same contract for the 6.7d backstop tags: a non-synthesized UF
        must dump exactly as it did before the fields existed. Same for
        the Wave-2a ``surface_scope`` / ``binding_confidence`` tags.
        """
        data = handler(self)
        if isinstance(data, dict):
            if data.get("identity") is None:
                data.pop("identity", None)
            if data.get("synthesized") is False:
                data.pop("synthesized", None)
            if data.get("synthesis_reason") is None:
                data.pop("synthesis_reason", None)
            if data.get("surface_scope") is None:
                data.pop("surface_scope", None)
            if data.get("binding_confidence") is None:
                data.pop("binding_confidence", None)
            # B52 — the transport-lane back-reference exists only on
            # lane-resident journeys under FAULTLINE_FLOWFUL_TRANSPORT_LANE;
            # a None dump is byte-identical to pre-B52 output.
            if data.get("lane_ref") is None:
                data.pop("lane_ref", None)
            # B3 — journey LOC is only present when Stage 6.97b ran
            # (FAULTLINE_UF_LOC on). A computed 0 is a real value and
            # stays; only the uncomputed sentinel (None) is dropped so
            # kill-switch-off / old-JSON dumps are byte-identical.
            if data.get("loc") is None:
                data.pop("loc", None)
            # B13 — the coverage-marker flag is present only on member-less
            # I8-cover seeds under the FAULTLINE_BACKSTOP_OWNED_COVER arm; a
            # default-False dump is byte-identical to pre-B13 output.
            if data.get("is_coverage_marker") is False:
                data.pop("is_coverage_marker", None)
            # B40 — the name-evidence audit trail is stamped only under
            # FAULTLINE_NAME_EVIDENCE_RUNGS; a None dump is byte-identical to
            # pre-B40 output (same contract as the identity/backstop tags).
            if data.get("name_evidence") is None:
                data.pop("name_evidence", None)
            # B23 — surface coordinates exist only on markers whose trigger
            # surface honestly resolved under FAULTLINE_MARKER_SURFACE_COORDS;
            # a None dump is byte-identical to pre-B23 output. The candidate
            # ledger is pipeline plumbing and must NEVER serialize (the
            # field is exclude=True; this pop is defensive belt-and-braces).
            if data.get("surface_files") is None:
                data.pop("surface_files", None)
            data.pop("surface_candidate_files", None)
            # B31 — the authored-label carrier is pipeline plumbing and must
            # NEVER serialize (exclude=True; this pop is belt-and-braces).
            data.pop("authored_label", None)
        return data


class CoverageGap(BaseModel):
    """B45 (2026-07-11) — a typed coverage-gap admission.

    A member-LESS I8-cover seed (``synthesized=True`` + ``member_count=0``:
    the loc-worthy / owned-cover / system-route / e2e-orphan placeholders
    the recall layer mints so a journey-worthy product feature is never
    "фіча без юзер-фловів", validator I8) is NOT a user journey — it is a
    *gap claim*. Historically these lived inside ``user_flows[]`` wearing an
    ``Uncovered: <PF> routes`` name (or a preserved e2e authored label),
    mimicking real journeys on the board, in MCP, and in PR comments. B45
    segregates them into this dedicated top-level channel so a gap can never
    be mistaken for a journey.

    Built deterministically at Stage 6.98 by
    ``synth_quality.emit_coverage_gaps`` from the SAME member-less marker
    rows ``honest_coverage_markers`` types (identical ``_is_member_less_marker``
    predicate) — one gap per surviving marker (a strict bijection with the
    old rows). Emitted ONLY under ``FAULTLINE_COVERAGE_GAP_CHANNEL`` ∈
    {dual, full}; the ``off`` default never mints a gap and the top-level
    ``coverage_gaps`` key is then ENTIRELY ABSENT (byte-identity law). In
    dual/full the key is ALWAYS present — ``[]`` on a zero-gap board — so
    consumers can detect the channel by key presence.
    """

    #: ``GAP-<sha1(pf|kind|label)[:10]>`` — content-derived + rescan-stable
    #: (no ``Date.now`` / randomness), so the same board state re-mints the
    #: same id across scans.
    id: str
    #: The home product-feature key (``uf.product_feature_id``). ``None``
    #: only for a lane/unowned marker; ref-integrity (``emission_integrity``)
    #: drops a gap whose key matches no emitted PF.
    product_feature_id: str | None = None
    #: Derived from (mint-site, ``synthesis_reason``) — see ``_gap_kind``.
    #: ``adjudicated_noise`` (B57 Seg2) is minted ONLY by the Stage 6.7e
    #: adjudicator's verified demote verdicts (a journey row honestly
    #: re-typed as a gap claim — never a silent drop; the row's label /
    #: routes / surface evidence ride along).
    kind: Literal[
        "system_route", "e2e_orphan", "loc_worthy", "owned_cover",
        "adjudicated_noise",
    ]
    #: The gap's human label — the row's FINAL display name (board-unique by
    #: the B31 recall-row naming pass): ``Uncovered: <PF display> routes`` for
    #: the system kinds, the maintainer's playwright label for ``e2e_orphan``.
    label: str
    #: The maintainer's ORIGINAL authored label (``e2e_orphan`` only; the B23
    #: carve). ``None`` for the system kinds. Omitted from dumps when ``None``.
    authored_label: str | None = None
    #: The seed's routes (as minted). Empty for the 6.7d backstop arms.
    routes: list[str] = []
    #: The uncovered trigger surface as whole-file ``(path, 1, loc)`` spans —
    #: the SAME B38-gated spans the marker carried (a gap without spans is
    #: never emitted; it is recorded in ``scan_meta.synth_quality.suppressed_markers``).
    surface_files: list[FlowLineRange] = []
    #: Surface LOC — per-file UNION of ``surface_files`` (mirrors Stage 6.97b).
    loc: int = 0
    #: The originating seed's ``synthesis_reason`` — traceability to the old
    #: recall-row world (``system_flow_recall`` / ``e2e_journey_recall``).
    synthesis_reason: str | None = None

    @model_serializer(mode="wrap")
    def _omit_none_authored(self, handler: Any) -> Any:
        """Drop ``authored_label`` from dumps when ``None`` (the system
        kinds) — the codebase omit-when-default convention."""
        data = handler(self)
        if isinstance(data, dict) and data.get("authored_label") is None:
            data.pop("authored_label", None)
        return data


class FeatureMap(BaseModel):
    # On-disk schema version. Default 0 = "pre-versioning scan" so any
    # JSON produced before this field existed rehydrates as 0, which is
    # distinguishable from a freshly produced map (stamped with
    # SCHEMA_VERSION at Stage 7 output assembly). See SCHEMA_VERSION's
    # docstring for the bump policy (breaking changes only).
    schema_version: int = 0
    repo_path: str
    remote_url: str = ""      # GitHub base URL, e.g. https://github.com/org/repo
    analyzed_at: datetime
    total_commits: int
    date_range_days: int
    # Storage field — kept for backward compatibility with every
    # downstream consumer (landing app, replay scripts, cloud sync,
    # incremental loader). Contains BOTH developer and product features.
    # Use ``developer_features`` / ``product_features`` properties for
    # the layered view introduced 2026-05-18.
    features: list[Feature] = []
    # Layer 1/2 input-side aliases. When a caller constructs a
    # FeatureMap with ``developer_features=`` / ``product_features=``
    # (the v2 pipeline does), the validator below folds them into
    # ``features`` so the on-disk shape stays stable.
    developer_features: list[Feature] | None = None
    product_features: list[Feature] | None = None
    last_scanned_sha: str = ""               # git HEAD at scan time — used for incremental refresh
    file_hashes: dict[str, str] = {}         # {rel_path: sha256_of_content} — skip re-parse when file unchanged
    symbol_hashes: dict[str, dict[str, str]] = {}  # {rel_path: {symbol_name: sha256_of_body}} — per-symbol cache for incremental LLM skip
    # Pipeline telemetry — stage timings, stack/monorepo detection,
    # signal counts, model versions. Free-form so we can iterate
    # without schema churn. Always emitted (default empty dict).
    scan_meta: dict[str, Any] = {}
    # Sprint B1 (2026-05-19) — top-level bipartite storage. The
    # per-feature ``Feature.flows[]`` list is kept as the canonical
    # containment view (every flow appears under its PRIMARY feature);
    # the lists below expose the bipartite graph as the source of
    # truth. Default empty for callers building pre-B1 maps.
    flows: list[Flow] = []
    feature_flow_edges: list[FeatureFlowEdge] = []
    # UF-Stage1 (2026-06-02) — top-level product-grain User Flows,
    # rolled up deterministically from ``flows`` by the Stage 6.7
    # clusterer. Mirrors ``product_features`` (the Layer-2 view of
    # ``developer_features``). Default empty so legacy scans and
    # Layer-1-only callers rehydrate unchanged.
    user_flows: list[UserFlow] = []
    # Sprint 1 (2026-05-23) — additive lineage / incremental surfaces.
    # ``path_index`` is a deterministic projection of features + flows
    # for O(1) file → (feature_uuid, flow_uuids) lookup. ``routes_index``
    # is a flat route registry built from the route extractor. Both
    # default to empty so legacy scans rehydrate without loss.
    path_index: dict[str, dict[str, Any]] = {}
    routes_index: list[dict[str, Any]] = []
    # ``is_full_scan=True`` for cold scans (default). Set to False
    # only by the incremental ``--since`` path. ``base_scan_commit`` is
    # the ``--since`` sha; ``scan_commit`` is the current HEAD sha.
    # ``engine_version`` is stamped so consumers can refuse to merge
    # incrementals across major engine bumps.
    is_full_scan: bool = True
    base_scan_commit: str = ""
    scan_commit: str = ""
    engine_version: str = ""
    # Stage 0.7 (2026-07, StackProfile Phase C) — the deterministic
    # repo-class exit-gate verdict for this scan unit:
    # ``product-app | library | cli-tool | infra-daemon | framework``.
    # A confident non-product class suppresses UF synthesis (the scan
    # then carries ``user_flows: []`` + ``scan_meta.uf_suppressed_reason``).
    # Default ``""`` so legacy scans rehydrate unchanged; detail
    # (confidence, rationale, signals) lives in ``scan_meta.repo_class``.
    repo_class: str = ""
    # Stage 6.6 (2026-06) — MONOREPO ASSEMBLY VIEW. A deterministic, $0,
    # ADDITIVE re-projection of the flat ``developer_features[]`` into a
    # per-PROJECT structure plus the internal cross-project dependency
    # graph, built by
    # :mod:`faultline.pipeline_v2.stage_6_6_monorepo_assembly`. Free-form
    # (like ``scan_meta``) so it can iterate without schema churn. Emitted
    # ONLY for monorepos (``{"is_monorepo": True, "projects": [...],
    # "cross_project_graph": {...}, "unassigned_features": [...], ...}``);
    # a single repo gets the trivial ``{"is_monorepo": False}``. Defaults
    # to ``{}`` so every scan produced before this field existed (and every
    # non-monorepo scan that doesn't populate it) rehydrates unchanged.
    # NEVER mutates ``features`` / ``developer_features`` — purely a view.
    monorepo: dict[str, Any] = {}
    # Product-Spine §4.2 (Wave 2a, 2026-07-06) — the NON-PRODUCT surface
    # lane: marketing / docs / legal / dev_tooling / shell surfaces that
    # would previously have shipped as product features (evidence class C3:
    # 39 info-page PFs on the 10-scan board). Each row is a compact dict
    # {name, display_name, surface_scope, description, uuid, paths, loc,
    # loc_shared, member_devs, user_flows[], reason} — the member dev rows
    # stay in ``features[]`` with their ``product_feature_id`` pointing at
    # the lane row's name. ADDITIVE: defaults to [] so every scan produced
    # before this field existed rehydrates unchanged; the product
    # ``product_features[]`` list keeps ONLY product/system-scope
    # capabilities (validator I20).
    non_product_surfaces: list[dict[str, Any]] = []
    # Product-Spine §4.3 (Wave 2b, operator amendment 2026-07-06): the
    # platform-infrastructure lane — the anchored path's residual. One
    # row PER resident dev {name, display_name, shared_reason, uuid,
    # paths, loc, loc_shared, flows}; the dev rows stay in ``features[]``
    # (Layer-1 truth, zero-loss) with ``product_feature_id=None``. The
    # "Shared Platform" product feature NO LONGER EXISTS on this path —
    # genuinely shared files surface as role="shared" members on their
    # consumers instead. ``None`` (omitted from dumps) on scans that
    # predate the lane and on FAULTLINE_SPINE_ANCHORED_MINT=0 scans, so
    # the A/B=0 output stays byte-identical to pre-W2b engines.
    platform_infrastructure: list[dict[str, Any]] | None = None
    # B45 (2026-07-11) — typed coverage-gap channel. The member-less I8-cover
    # markers that used to ship as hollow ``user_flows[]`` rows are emitted
    # here instead (kind + label + surface spans), so a gap can never be
    # mistaken for a journey. ``None`` (and the key ENTIRELY OMITTED from the
    # dump — see the serializer) unless ``FAULTLINE_COVERAGE_GAP_CHANNEL`` is
    # dual/full, so the default ``off`` path serializes byte-identically to
    # pre-B45 engines (kill-switch / byte-identity law). KEY-PRESENCE
    # contract: in dual/full the pipeline passes a LIST — possibly EMPTY
    # (``"coverage_gaps": []`` on a zero-gap board) — because consumers
    # detect the gap-channel world by the key's presence ("coverage_gaps"
    # in scan: warden gap-channel-leak class, flowless-silent gap exemption).
    coverage_gaps: list[CoverageGap] | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_lane_fields(self, handler: Any) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            if data.get("platform_infrastructure") is None:
                data.pop("platform_infrastructure", None)
            # B45 — the gap channel is off by default: a None dump drops the
            # key entirely so the off path is byte-identical to pre-B45.
            if data.get("coverage_gaps") is None:
                data.pop("coverage_gaps", None)
        return data

    @model_validator(mode="after")
    def _merge_layer_inputs(self) -> "FeatureMap":
        """Fold ``developer_features`` / ``product_features`` inputs
        into ``features`` and stamp ``layer`` on each entry.

        Semantics:
            - If ONLY ``features`` is provided (legacy path): leave
              alone. Existing entries keep their declared ``layer``
              (default ``"developer"``).
            - If ``developer_features`` and/or ``product_features``
              are provided: they are the source of truth. Each entry
              gets its ``layer`` stamped accordingly, and the combined
              list replaces ``features``.
            - Mixing both forms is allowed but the explicit layered
              inputs win (they overwrite ``features``).
        """
        dev = self.developer_features
        prod = self.product_features
        if dev is None and prod is None:
            # Legacy path — nothing to merge. Clear the input-side
            # aliases so they don't get serialized.
            self.developer_features = None
            self.product_features = None
            return self
        merged: list[Feature] = []
        for f in dev or []:
            f.layer = "developer"
            merged.append(f)
        for f in prod or []:
            f.layer = "product"
            merged.append(f)
        self.features = merged
        # Aliases were inputs only; drop them from serialization.
        self.developer_features = None
        self.product_features = None
        return self

    def get_developer_features(self) -> list[Feature]:
        """Layer 1 view — code-grounded features."""
        return [f for f in self.features if f.layer == "developer"]

    def get_product_features(self) -> list[Feature]:
        """Layer 2 view — marketing/docs-grounded features."""
        return [f for f in self.features if f.layer == "product"]

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        """Override dump to emit ``developer_features`` and
        ``product_features`` as top-level arrays in the JSON output
        (in addition to ``features`` for back-compat) so v2 consumers
        can read the layered shape directly.
        """
        data = super().model_dump(**kwargs)
        # Strip the input-side aliases (always None post-validation).
        data.pop("developer_features", None)
        data.pop("product_features", None)
        # Re-derive the layered views on the dumped dicts so they
        # reflect any mutation to ``features`` post-construction.
        features_dump = data.get("features", [])
        data["developer_features"] = [
            f for f in features_dump if f.get("layer", "developer") == "developer"
        ]
        data["product_features"] = [
            f for f in features_dump if f.get("layer") == "product"
        ]
        return data

    def sorted_by_risk(self) -> list[Feature]:
        """Returns features sorted from highest to lowest risk."""
        return sorted(self.features, key=lambda f: f.bug_fix_ratio, reverse=True)
