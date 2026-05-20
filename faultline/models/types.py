from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, model_validator


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


class Flow(BaseModel):
    name: str                  # "checkout-flow", "login-flow"
    display_name: str | None = None  # Title Case label for UI ("Checkout")
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
    hotspot_files: list[str] = []            # source files with >40% bug_fix_ratio (≥3 commits)
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


class SymbolRange(BaseModel):
    name: str              # exported symbol name, e.g. "FEATURE_FLAGS"
    start_line: int        # 1-indexed, inclusive
    end_line: int          # 1-indexed, inclusive
    kind: str = "const"    # "const", "function", "class", "type", "enum", "reexport"


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
    """

    file: str                  # repo-relative path
    symbol: str                # exported / local symbol name, or "<file>" for support
    line_start: int            # 1-indexed, inclusive
    line_end: int              # 1-indexed, inclusive
    role: Literal[
        "entry", "called", "support",
        "anchor-consumer", "schema-consumer", "structural",
        "framework-link", "branch",
    ]


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
    flows: list[Flow] = []    # populated when --flows flag is used
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


class FeatureMap(BaseModel):
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
