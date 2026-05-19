"""Stage 6.5 — Layer 2 product-feature clusterer (deterministic).

Groups Layer 1 ``developer_features`` into customer-facing
``product_features`` WITHOUT any LLM call by combining three rules:

  Rule 1 — workspace cluster
      Developer features whose primary paths concentrate (≥70%) under
      ``apps/<X>/**`` or ``packages/<X>/**`` fold into a product feature
      named after that workspace.  Confidence 0.6 — low because a
      single workspace can host multiple unrelated product surfaces.

  Rule 2 — dependency anchor cluster
      Developer features whose paths import a known dependency
      (Stripe, NextAuth, Resend, Inngest, …) fold under the canonical
      product label declared in ``eval/dependency-anchors.yaml``.
      Confidence 0.75 — higher than workspace because a dep import is
      a near-binary signal of capability presence.

  Rule 3 — customer YAML override
      If the scanned repo has a ``faultlines.yaml`` at its root with a
      ``product_features:`` block, that mapping wins absolutely
      (confidence 1.0) and overrides any earlier rule decision.

Conflict resolution
===================

When rules 1+2 disagree (workspace says "Admin Dashboard" but the
dep-anchor says "Billing"), the higher-confidence rule wins
(dep-anchor 0.75 > workspace 0.6) but BOTH anchor signals are
preserved in ``ProductFeature.anchor_signals``.

When multiple dep anchors fire on the same developer feature with
roughly even path share (≤2× lead), the feature is assigned to BOTH
product features (bipartite extension — natural follow-on from B1).
``DeveloperFeature.product_feature_ids`` is a tuple, not a single
slug.

Output contract
===============

``run_product_clusterer`` returns ``(product_features, mapping)`` where
``mapping`` is ``{developer_feature_name: tuple[product_feature_name, ...]}``.
The orchestrator stamps the IDs onto each :class:`Feature` and Stage 7
re-serialises the product layer to the FeatureMap output.

NO LLM. NO network. Pure manifest + path + YAML parsing.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── YAML config loader (cached on first read) ───────────────────────────


_ANCHOR_YAML_PATH = (
    Path(__file__).resolve().parents[2] / "eval" / "dependency-anchors.yaml"
)

# Path-concentration threshold for Rule 1: a feature's paths must
# concentrate at least this fraction under a single workspace for the
# workspace rule to fire. Scale-invariant (a ratio, not a count).
_WORKSPACE_CONCENTRATION_MIN = 0.70

# Confidence levels assigned to each rule's emissions. Set in code so
# rule precedence is auditable; tweaking them shifts conflict winners
# without touching the YAML.
_CONF_WORKSPACE = 0.6
_CONF_DEP_ANCHOR = 0.75
_CONF_CUSTOMER_YAML = 1.0

# When multiple dep anchors fire on the same dev feature, the
# dominant anchor wins iff its path share is at least this multiple
# of the runner-up. Below this ratio we treat them as "ambiguous"
# and emit BOTH memberships.
_AMBIGUOUS_LEAD_THRESHOLD = 2.0


# ── Public types ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProductFeature:
    """One Layer 2 product feature — a cluster of Layer 1 dev features."""

    name: str                                  # canonical product label
    developer_feature_names: tuple[str, ...]   # which Layer 1 features fold in
    anchor_signals: tuple[str, ...]            # what triggered the clustering
    source: str                                # "rule:workspace" / "rule:dep-anchor" / "rule:customer-yaml" / "rule:workspace+rule:dep-anchor"
    confidence: float                          # 0..1 — higher when multiple rules agree


@dataclass
class _Vote:
    """Internal — one rule's opinion about which product label a dev
    feature belongs to.  Multiple votes per dev feature are reconciled
    in :func:`_resolve_votes`.
    """

    product_label: str
    rule: str                                  # "workspace" / "dep-anchor" / "customer-yaml"
    confidence: float
    anchor_signal: str                         # human-readable origin (workspace path, dep name, etc.)
    weight: float = 1.0                        # path-share within feature for dep-anchor ties


@dataclass
class _ClustererState:
    """Mutable accumulator while we apply Rules 1→2→3 in order."""

    # dev_feature_name → list of votes from any rule that fired
    votes: dict[str, list[_Vote]] = field(default_factory=lambda: defaultdict(list))


# ── Loader for dependency-anchors.yaml ──────────────────────────────────


_DEP_ANCHORS_CACHE: list[tuple[tuple[str, ...], str]] | None = None
"""Cached ``[(dep_patterns, product_label), …]`` after first successful load."""


def _load_dep_anchors() -> list[tuple[tuple[str, ...], str]]:
    """Read ``eval/dependency-anchors.yaml`` once, return the parsed map.

    Returns ``[]`` on any parse / file error — the clusterer degrades
    gracefully to Rule 1 + Rule 3 only.
    """
    global _DEP_ANCHORS_CACHE
    if _DEP_ANCHORS_CACHE is not None:
        return _DEP_ANCHORS_CACHE

    out: list[tuple[tuple[str, ...], str]] = []
    try:
        text = _ANCHOR_YAML_PATH.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "stage_6_5_product_clusterer: cannot read %s (%s) — "
            "Rule 2 (dep-anchor) will be skipped",
            _ANCHOR_YAML_PATH, exc,
        )
        _DEP_ANCHORS_CACHE = out
        return out

    if not isinstance(raw, dict):
        _DEP_ANCHORS_CACHE = out
        return out

    for _slug, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        deps = entry.get("deps") or []
        label = entry.get("product_label")
        if not isinstance(label, str) or not isinstance(deps, list):
            continue
        clean = tuple(str(d).strip() for d in deps if isinstance(d, str))
        if not clean:
            continue
        out.append((clean, label.strip()))

    _DEP_ANCHORS_CACHE = out
    return out


def _dep_pattern_matches(dep_name: str, pattern: str) -> bool:
    """``True`` when ``dep_name`` matches ``pattern``.

    Patterns supported:
      - exact:           ``stripe``        matches only ``stripe``
      - scope wildcard:  ``@stripe/*``     matches ``@stripe/anything``
      - prefix-:         family ``next-auth`` matches ``next-auth-something``
                         (handled implicitly by ``@scope`` rule above)

    Mirrors the ``_dep_matches`` logic in
    :mod:`faultline.pipeline_v2.extractors.package` for consistency.
    """
    if pattern == dep_name:
        return True
    if pattern.endswith("/*"):
        prefix = pattern[:-2]
        return dep_name == prefix or dep_name.startswith(prefix + "/")
    # Token-prefix family match — same semantics as the package extractor.
    if dep_name.startswith(pattern + "/"):
        return True
    if dep_name.startswith(pattern + "-"):
        return True
    return False


# ── Helpers — workspace path attribution ────────────────────────────────


_WORKSPACE_PREFIX_RE = re.compile(r"^(?:apps|packages|services|libs)/([^/]+)/")


def _workspace_for_path(path: str) -> str | None:
    """Return the workspace bucket name for ``path``, or ``None``.

    Recognises the four common monorepo top-level dirs. We do NOT read
    ``ctx.workspaces`` for this rule because some repos declare workspaces
    via ``pnpm-workspace.yaml`` globs that don't cleanly match a single
    folder; the explicit ``apps/<X>/`` prefix is a higher-precision
    structural signal.
    """
    m = _WORKSPACE_PREFIX_RE.match(path.replace("\\", "/"))
    if m:
        return m.group(1)
    return None


def _titleize(slug: str) -> str:
    """Convert kebab / snake / camel to Title Case for product labels."""
    cleaned = re.sub(r"[_\-]+", " ", slug)
    # Split camelCase into separate words too.
    cleaned = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cleaned)
    return " ".join(w.capitalize() for w in cleaned.split() if w)


# ── Rule 1 — workspace cluster ──────────────────────────────────────────


def _apply_workspace_rule(
    developer_features: list["Feature"],
    state: _ClustererState,
) -> int:
    """Cast a vote for any dev feature whose paths concentrate ≥70%
    under a single ``apps/<X>/`` or ``packages/<X>/`` workspace.

    Returns the number of votes cast (for telemetry).
    """
    votes_cast = 0
    for f in developer_features:
        paths = list(f.paths or [])
        if not paths:
            continue
        per_ws: dict[str, int] = defaultdict(int)
        for p in paths:
            ws = _workspace_for_path(p)
            if ws is not None:
                per_ws[ws] += 1
        if not per_ws:
            continue
        # Pick the dominant workspace; require concentration ≥ threshold.
        ws_name, ws_count = max(per_ws.items(), key=lambda kv: kv[1])
        share = ws_count / len(paths)
        if share < _WORKSPACE_CONCENTRATION_MIN:
            continue
        label = _titleize(ws_name)
        state.votes[f.name].append(
            _Vote(
                product_label=label,
                rule="workspace",
                confidence=_CONF_WORKSPACE,
                anchor_signal=f"workspace:{ws_name}",
                weight=share,
            ),
        )
        votes_cast += 1
    return votes_cast


# ── Rule 2 — dependency anchor cluster ──────────────────────────────────


def _scan_imports_for_deps(
    repo_root: Path,
    paths: list[str],
    dep_patterns_by_label: dict[str, tuple[str, ...]],
) -> dict[str, int]:
    """Grep each path's content for any dep pattern.

    Returns ``{product_label: hit_count}`` aggregated across paths.

    Implementation: substring grep over file text — no AST. Fast and
    cheap; the precision risk (e.g. ``stripe`` appearing in a comment)
    is acceptable because anchor confidence is already capped at 0.75.
    """
    hits: dict[str, int] = defaultdict(int)
    if not paths:
        return hits

    # Pre-build (pattern_base, label) tuples for raw substring matching.
    # We strip "@scope/*" → "@scope" and "name-*" → "name" so a substring
    # grep can fire. False positives are accepted; same conservatism as
    # the PackageAnchorExtractor.
    pattern_keys: list[tuple[str, str]] = []
    for label, patterns in dep_patterns_by_label.items():
        for p in patterns:
            key = p[:-2] if p.endswith("/*") else p
            pattern_keys.append((key, label))

    for rel in paths:
        try:
            full = repo_root / rel
            if not full.is_file():
                continue
            # Cap read size to avoid choking on giant assets.
            text = full.read_text(encoding="utf-8", errors="ignore")[:200_000]
        except OSError:
            continue
        if not text:
            continue
        # Only inspect import-like lines to lift signal:noise. Cheap regex
        # scan rather than full AST parse.
        # We restrict matching to lines containing 'import', 'require',
        # 'from ', '@import', or yaml/toml-style refs.
        for line in text.splitlines():
            if not (
                "import" in line
                or "require" in line
                or line.lstrip().startswith("from ")
            ):
                continue
            for key, label in pattern_keys:
                if key and key in line:
                    hits[label] += 1
    return hits


def _apply_dep_anchor_rule(
    ctx: "ScanContext",
    developer_features: list["Feature"],
    state: _ClustererState,
) -> int:
    """Cast votes for any dev feature whose paths import known anchor deps.

    Each (dev feature, product label) pair gets one vote with weight
    proportional to the matched-line count (so the dominant anchor
    wins ties in :func:`_resolve_votes`).
    """
    anchors = _load_dep_anchors()
    if not anchors:
        return 0
    patterns_by_label: dict[str, tuple[str, ...]] = {label: pats for pats, label in anchors}

    votes_cast = 0
    for f in developer_features:
        paths = list(f.paths or [])
        if not paths:
            continue
        label_hits = _scan_imports_for_deps(ctx.repo_path, paths, patterns_by_label)
        if not label_hits:
            continue
        total = sum(label_hits.values()) or 1
        for label, hits in label_hits.items():
            # Record the dep name(s) that fired for human-readable provenance.
            dep_signal = f"dep:{label.lower()}"
            state.votes[f.name].append(
                _Vote(
                    product_label=label,
                    rule="dep-anchor",
                    confidence=_CONF_DEP_ANCHOR,
                    anchor_signal=dep_signal,
                    weight=hits / total,
                ),
            )
            votes_cast += 1
    return votes_cast


# ── Rule 3 — customer YAML override ─────────────────────────────────────


def _load_customer_overrides(repo_path: Path) -> dict[str, str]:
    """Read ``<repo>/faultlines.yaml`` if present.

    Expected shape:
        product_features:
          - name: "HTTP Uptime Monitoring"
            includes: [checker, monitors, status-fetcher]
          - name: "Status Pages"
            includes: [status-page, public, badge]

    Returns ``{developer_feature_name: product_label}`` mapping (a
    developer feature can only override into a SINGLE product label;
    customer override takes precedence over rules 1+2 conflict logic).
    """
    cfg_path = repo_path / "faultlines.yaml"
    if not cfg_path.is_file():
        return {}
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "stage_6_5_product_clusterer: cannot parse %s (%s) — "
            "Rule 3 (customer-yaml) skipped",
            cfg_path, exc,
        )
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("product_features") or []
    if not isinstance(entries, list):
        return {}
    mapping: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        label = entry.get("name")
        includes = entry.get("includes") or []
        if not isinstance(label, str) or not isinstance(includes, list):
            continue
        for inc in includes:
            if isinstance(inc, str):
                mapping[inc] = label.strip()
    return mapping


def _apply_customer_yaml_rule(
    ctx: "ScanContext",
    state: _ClustererState,
) -> int:
    """Override any prior votes with the customer's explicit mapping."""
    overrides = _load_customer_overrides(ctx.repo_path)
    if not overrides:
        return 0
    for dev_name, label in overrides.items():
        # Customer rule fully OWNS the dev_name → product mapping.
        # Wipe prior votes; insert a sentinel vote at confidence 1.0.
        state.votes[dev_name] = [
            _Vote(
                product_label=label,
                rule="customer-yaml",
                confidence=_CONF_CUSTOMER_YAML,
                anchor_signal="faultlines.yaml",
                weight=1.0,
            ),
        ]
    return len(overrides)


# ── Vote resolution ─────────────────────────────────────────────────────


def _resolve_votes(
    votes: list[_Vote],
) -> tuple[list[str], list[_Vote]]:
    """Decide which product labels a dev feature belongs to.

    Returns ``(winning_labels, winning_votes)``:
      - ``winning_labels`` is the list of product labels the feature
        is assigned to (usually 1; possibly 2 when dep-anchor weights
        are ambiguous; capped at 2 to keep the bipartite view sane).
      - ``winning_votes`` is the underlying vote records, retained so
        the caller can build ``anchor_signals`` and pick a ``source``
        string that reflects every contributing rule.
    """
    if not votes:
        return [], []

    # Customer-yaml short-circuits everything.
    customer = [v for v in votes if v.rule == "customer-yaml"]
    if customer:
        return [customer[0].product_label], list(customer)

    # Group votes by product_label, summing weights per label.
    by_label: dict[str, list[_Vote]] = defaultdict(list)
    for v in votes:
        by_label[v.product_label].append(v)

    def _label_score(label: str) -> tuple[float, float]:
        vs = by_label[label]
        # Score primary on confidence (highest rule wins) then on
        # accumulated weight as a tiebreaker.
        return (
            max(v.confidence for v in vs),
            sum(v.weight for v in vs),
        )

    ranked = sorted(by_label.keys(), key=_label_score, reverse=True)
    top_label = ranked[0]
    top_score = _label_score(top_label)

    winners = [top_label]
    contributing = list(by_label[top_label])

    # If a second label is within the ambiguous-lead window AND is
    # in the same confidence tier as the winner (so it isn't a weak
    # workspace vote drowning a strong dep-anchor), surface BOTH
    # memberships.
    if len(ranked) > 1:
        runner = ranked[1]
        runner_score = _label_score(runner)
        if runner_score[0] >= top_score[0]:
            top_weight = top_score[1] or 1e-9
            runner_weight = runner_score[1]
            # When the leader's path share is NOT at least
            # 2× the runner's share, treat as ambiguous.
            if top_weight < _AMBIGUOUS_LEAD_THRESHOLD * runner_weight:
                winners.append(runner)
                contributing.extend(by_label[runner])

    # Preserve LOSING-rule provenance for audit + telemetry. When a
    # dev feature has votes from BOTH workspace AND dep-anchor rules
    # (different labels), the dep-anchor wins but the workspace
    # signal is retained as a contributing vote so the source
    # breakdown reports "combined" and downstream consumers can see
    # which workspace the feature lived in. The losing vote's
    # ``product_label`` field is preserved verbatim — callers that
    # group by label know to ignore non-winning labels.
    for label, vs in by_label.items():
        if label in winners:
            continue
        contributing.extend(vs)

    return winners, contributing


# ── Public entry point ──────────────────────────────────────────────────


def run_product_clusterer(
    ctx: "ScanContext",
    developer_features: list["Feature"],
    log: "StageLogger | None" = None,
) -> tuple[list["Feature"], dict[str, tuple[str, ...]], dict[str, Any]]:
    # ProductFeature dataclasses are exposed via the artifact dict
    # written into ``scan_meta`` so callers + tests can introspect the
    # anchor signals without traversing the per-label group.
    """Cluster Layer 1 ``developer_features`` into Layer 2 product features.

    Args:
        ctx:    Stage 0 context — provides ``repo_path`` for the
                YAML override + workspace fallback.
        developer_features: the Stage 6 enriched dev feature list. Read
                only — we do not mutate the inputs.
        log:    optional :class:`StageLogger` — when provided, each
                emit/vote/conflict is logged for replay.

    Returns:
        ``(product_features, mapping, telemetry)`` where:
          * ``product_features`` is a list of :class:`Feature` rows
            with ``layer="product"`` ready to attach to
            ``FeatureMap.product_features``.
          * ``mapping`` is ``{dev_name: tuple[product_label, …]}`` —
            the orchestrator stamps ``Feature.product_feature_id`` on
            each developer feature from this.
          * ``telemetry`` is the dict folded into ``scan_meta``:
            product_features_count, mapped_pct, orphan_count,
            source_breakdown, etc.
    """
    # Lazy import — keeps the module test-friendly (the Feature class
    # carries a Pydantic validator chain we don't want at import time).
    from faultline.models.types import Feature

    state = _ClustererState()

    # ── Rule 1 — workspace (low confidence baseline) ──
    workspace_votes = _apply_workspace_rule(developer_features, state)

    # ── Rule 2 — dep-anchor (higher confidence) ──
    dep_votes = _apply_dep_anchor_rule(ctx, developer_features, state)

    # ── Rule 3 — customer YAML override (absolute) ──
    customer_votes = _apply_customer_yaml_rule(ctx, state)

    # ── Resolve per-feature votes into final assignments ──
    # mapping: dev_name → tuple[product_label, …]
    mapping: dict[str, tuple[str, ...]] = {}
    # winning_votes_per_dev: dev_name → list[_Vote] of contributing votes
    winning_votes_per_dev: dict[str, list[_Vote]] = {}
    for dev_name, votes in state.votes.items():
        labels, contributing = _resolve_votes(votes)
        if labels:
            mapping[dev_name] = tuple(labels)
            winning_votes_per_dev[dev_name] = contributing
            if log is not None:
                log.emit(
                    dev_name,
                    f"product_feature_ids={list(labels)}",
                    rules=sorted({v.rule for v in contributing}),
                )

    # ── Build product feature aggregates ──
    # product_label → {dev_names, anchor_signals, rules, max_conf}
    pf_accum: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "dev_names": [],
        "signals": set(),
        "rules": set(),
        "max_conf": 0.0,
    })
    for dev_name, labels in mapping.items():
        contributing = winning_votes_per_dev.get(dev_name, [])
        for label in labels:
            entry = pf_accum[label]
            entry["dev_names"].append(dev_name)
            # Filter the contributing votes to only those that pointed
            # at THIS label — the dev feature may have voted for two.
            for v in contributing:
                if v.product_label == label:
                    entry["signals"].add(v.anchor_signal)
                    entry["rules"].add(v.rule)
                    entry["max_conf"] = max(entry["max_conf"], v.confidence)

    # Materialize ProductFeature dataclasses (used downstream by tests
    # and serialised into Feature shape for FeatureMap).
    product_dataclasses: list[ProductFeature] = []
    for label, entry in pf_accum.items():
        rules = sorted(entry["rules"])
        source = "+".join(f"rule:{r}" for r in rules)
        product_dataclasses.append(ProductFeature(
            name=label,
            developer_feature_names=tuple(sorted(set(entry["dev_names"]))),
            anchor_signals=tuple(sorted(entry["signals"])),
            source=source,
            confidence=round(float(entry["max_conf"]), 3),
        ))

    # ── Render product features as Feature rows (Layer 2) ──
    # We synthesise the union of paths/authors/health from the contributing
    # developer features so the existing landing app sees something
    # plausible without a separate pipeline pass.
    dev_by_name = {f.name: f for f in developer_features}
    product_features_out: list[Feature] = []
    for pf in product_dataclasses:
        contrib = [dev_by_name[n] for n in pf.developer_feature_names if n in dev_by_name]
        if not contrib:
            continue
        merged_paths: list[str] = []
        seen_paths: set[str] = set()
        for c in contrib:
            for p in c.paths:
                if p not in seen_paths:
                    merged_paths.append(p)
                    seen_paths.add(p)
        authors: list[str] = []
        seen_authors: set[str] = set()
        for c in contrib:
            for a in (c.authors or []):
                if a not in seen_authors:
                    authors.append(a)
                    seen_authors.add(a)
        total_commits = sum(c.total_commits for c in contrib)
        bug_fixes = sum(c.bug_fixes for c in contrib)
        bug_fix_ratio = (bug_fixes / total_commits) if total_commits else 0.0
        last_modified = max(
            (c.last_modified for c in contrib),
            default=datetime.now(timezone.utc),
        )
        # Health is averaged across contributing dev features. We do
        # not invent a fresh signal — the product feature inherits its
        # constituents' aggregate health.
        health_score = (
            sum(c.health_score for c in contrib) / len(contrib)
        )
        # Coverage: average over contributors that have a value.
        cov_vals = [c.coverage_pct for c in contrib if c.coverage_pct is not None]
        coverage_pct = (sum(cov_vals) / len(cov_vals)) if cov_vals else None
        product_features_out.append(Feature(
            name=pf.name,
            display_name=pf.name,
            description=f"Product feature clustered from {len(contrib)} developer features by {pf.source}.",
            paths=merged_paths,
            authors=authors,
            total_commits=total_commits,
            bug_fixes=bug_fixes,
            bug_fix_ratio=bug_fix_ratio,
            last_modified=last_modified,
            health_score=round(health_score, 2),
            flows=[],
            coverage_pct=coverage_pct,
            layer="product",
        ))

    # ── Telemetry ──
    total = len(developer_features)
    mapped = len(mapping)
    source_breakdown: dict[str, int] = defaultdict(int)
    for dev_name, votes_for in winning_votes_per_dev.items():
        rules = sorted({v.rule for v in votes_for})
        if len(rules) == 1:
            source_breakdown[f"rule:{rules[0]}"] += 1
        else:
            source_breakdown["combined"] += 1

    telemetry: dict[str, Any] = {
        "product_features_count": len(product_features_out),
        "developer_features_total": total,
        "developer_features_mapped_count": mapped,
        "developer_features_mapped_pct": round(mapped / total, 3) if total else 0.0,
        "developer_features_orphan_count": max(total - mapped, 0),
        "product_clusterer_source_breakdown": dict(source_breakdown),
        "product_clusterer_votes_cast": {
            "workspace": workspace_votes,
            "dep-anchor": dep_votes,
            "customer-yaml": customer_votes,
        },
    }

    if log is not None:
        log.info(
            f"product_features={len(product_features_out)} "
            f"mapped={mapped}/{total} "
            f"(workspace_votes={workspace_votes} "
            f"dep_votes={dep_votes} customer_votes={customer_votes})",
        )

    return product_features_out, mapping, telemetry


# Public re-exports — keep this list tight; everything else is internal.
__all__ = [
    "ProductFeature",
    "run_product_clusterer",
]
