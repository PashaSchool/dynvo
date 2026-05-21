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

from faultline.pipeline_v2.domain_noun import extract_domain_noun

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


# ── Phantom-cluster name filter (Sprint E2) ─────────────────────────────
#
# Workspace folders whose Title-Cased names are NEVER legitimate
# customer-facing product features. Sourced from observed junk
# emissions across 15 repos in sprint-director-v3-results.md.
#
# Universal — these names denote infrastructure, build tooling,
# universal folder layout, or top-level catch-alls. They are NOT
# tuned to any single repo (per ``rule-no-magic-tuning`` and
# ``rule-no-repo-specific-paths``).
#
# This filter applies ONLY to the workspace rule (Rule 1). The
# dep-anchor rule (Rule 2) and customer-yaml rule (Rule 3) are NOT
# affected: a repo that imports a Database dep still gets a
# legitimate "Database" product feature via Rule 2 even though
# "Database" is in this set — what gets filtered is naming a
# product after the FOLDER ``apps/database/``, which is structural
# infrastructure rather than a customer-facing surface.
#
# Conservative — only names that are universally junk (folder,
# build, infra, catch-all). When in doubt, OMIT (false negatives
# are cheaper than false positives that suppress real surfaces).
#
# IMPORTANT — name forms must match exactly what ``_titleize`` produces
# for the corresponding folder slug. ``_titleize`` uses ``str.capitalize``
# on each whitespace-separated token, so ``apps/ai/`` → ``"Ai"`` (NOT
# ``"AI"``), ``packages/tsconfig/`` → ``"Tsconfig"``, and
# ``packages/config-eslint/`` → ``"Config Eslint"``. The handoff
# diagnostic lists prose-Title-Cased forms ("AI") which correspond to
# dep-anchor product_labels in ``eval/dependency-anchors.yaml`` —
# those are NOT filtered here. This set contains ONLY the
# workspace-rule emission forms.
_PHANTOM_CLUSTER_NAMES: frozenset[str] = frozenset({
    # Pure infrastructure — workspace folders that name plumbing,
    # not product surfaces. Dep-anchor still fires on imports.
    "Ai",
    "Realtime",
    "Database",
    "Cache",
    "Docker",
    "Hosting",
    "File Storage",
    # E2 audit additions (corpus-evidence: multi-repo or universal):
    "Logs",  # dub (singleton, but "logs/" folder is universal infra)
    "Storage",  # maybe (singleton, but storage/ folder is universal infra)
    # Universal folder names — every monorepo has these structural
    # buckets and they are never themselves a product surface.
    "Packages",
    "Docs",
    "Apps",
    "Internal Packages",
    "Routes",
    "Internationalization",
    "Frontend",
    "Backend",
    # E2 audit additions (corpus-evidence: universal scaffolding dirs):
    "Examples",  # cal-com, chi, ollama, supabase (4 repos)
    "Templates",  # cal-com (singleton, but templates/ is universal)
    "Template",  # ollama (singleton; same logic, singular form)
    "Mocks",  # cal-com (universal test-scaffold folder)
    "Fixtures",  # maybe (universal test-scaffold folder)
    "Demo",  # better-auth (universal example folder)
    "Documentation",  # meilisearch (synonym of "Docs")
    "Constants",  # cal-com (universal code-organisation folder)
    "Hooks",  # plane (universal React folder; not a product surface)
    "Schema",  # dub (universal DB-layout folder; dep-anchor still fires)
    "Assets",  # documenso (universal static-asset folder)
    "Configs",  # trigger.dev (plural of universal config folder)
    # Build tooling — config-only workspaces.
    "Tsconfig",
    "Prettier Config",
    "Config Prettier",
    "Config Eslint",
    "Config Typescript",
    "Tailwind",
    "Vite Plugins",
    "Codemods",
    "Docker Swarm",
    "Cloudformation",
    "Helm Charts",
    "Wasm",
    # E2 audit additions (corpus-evidence: build/CI scaffolding):
    "Builds",  # maybe (build-output folder)
    "Github",  # documenso (the .github/ folder, CI config)
    "Yarn",  # cal-com (.yarn/ folder)
    "Procfile",  # cal-com (Heroku deploy descriptor — single file, not a feature)
    # Top-level catch-alls — generic non-product names.
    "All",
    "Ee",
    "Tmp",
    "Bin",
    # E2 audit additions (corpus-evidence: generic catch-all labels):
    "V1",  # cal-com, inbox-zero (version folder; never a product surface)
    "Sandbox",  # axios (test-sandbox folder; was previously excluded
    #            conservatively — corpus evidence confirms it's junk in
    #            this corpus. If future Vercel-Sandbox-style products
    #            appear, they will route via dep-anchor (Rule 2), not
    #            the workspace rule that this set guards.)
    "Browser",  # axios (browser/ folder; conservative exclusion lifted
    #            on same logic as Sandbox)
    "Old",  # dub (legacy-code folder)
    "Defaults",  # axios (defaults/ folder; never a product surface)
})


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

# Sprint Rails H3 — alias map for semantic-name fallthrough guard.
_DEP_ALIASES_CACHE: dict[str, tuple[str, ...]] | None = None


def _load_dep_anchors() -> list[tuple[tuple[str, ...], str]]:
    """Read ``eval/dependency-anchors.yaml`` once, return the parsed map.

    Returns ``[]`` on any parse / file error — the clusterer degrades
    gracefully to Rule 1 + Rule 3 only.

    Side effect: populates ``_DEP_ALIASES_CACHE`` with per-label name
    alias substrings for the Sprint Rails H3 semantic-name guard.
    """
    global _DEP_ANCHORS_CACHE, _DEP_ALIASES_CACHE
    if _DEP_ANCHORS_CACHE is not None:
        return _DEP_ANCHORS_CACHE

    out: list[tuple[tuple[str, ...], str]] = []
    aliases: dict[str, tuple[str, ...]] = {}
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
        _DEP_ALIASES_CACHE = aliases
        return out

    if not isinstance(raw, dict):
        _DEP_ANCHORS_CACHE = out
        _DEP_ALIASES_CACHE = aliases
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
        label_clean = label.strip()
        out.append((clean, label_clean))

        # name_aliases — Sprint Rails H3. Optional in YAML.
        name_aliases_raw = entry.get("name_aliases") or []
        if isinstance(name_aliases_raw, list):
            clean_aliases = tuple(
                str(a).strip().lower()
                for a in name_aliases_raw
                if isinstance(a, str) and a.strip()
            )
            if clean_aliases:
                aliases[label_clean] = clean_aliases

    _DEP_ANCHORS_CACHE = out
    _DEP_ALIASES_CACHE = aliases
    return out


def _get_dep_aliases() -> dict[str, tuple[str, ...]]:
    """Return cached ``label → alias substrings``. Lazy-loads if needed."""
    if _DEP_ALIASES_CACHE is None:
        _load_dep_anchors()
    return _DEP_ALIASES_CACHE or {}


def _feature_matches_aliases(
    feature_name: str,
    feature_paths: list[str],
    aliases: tuple[str, ...],
) -> bool:
    """Sprint Rails H3 — semantic name match guard for dep-anchor.

    Return True when at least one alias substring appears in:
      - the feature slug (case-insensitive), OR
      - any file basename in ``feature_paths`` (case-insensitive)

    Empty alias tuple → permissive (True). Labels in
    ``dependency-anchors.yaml`` without an ``name_aliases`` block keep
    the legacy (pre-H3) fallthrough behaviour — opt-in by adding
    aliases to the YAML.
    """
    if not aliases:
        return True
    name_lower = (feature_name or "").lower()
    for alias in aliases:
        if alias in name_lower:
            return True
    for p in feature_paths:
        base = p.replace("\\", "/").rsplit("/", 1)[-1].lower()
        for alias in aliases:
            if alias in base:
                return True
    return False


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


# Sprint E5 — name-specificity penalty for vote resolution.
#
# Generic single-word product labels (``AI``, ``Auth``, ``Billing``,
# ``Realtime``, ``Cache``) lose to multi-word specific labels
# (``AI Email Assistant``, ``Magic Link Sign-In``) when both apply to
# the same developer feature. Specificity is computed as the count of
# non-stopword tokens in the label after splitting on whitespace —
# scale-invariant, no per-repo tuning (``rule-no-magic-tuning``).
#
# Applied as a tertiary tiebreaker AFTER (confidence, weight) so it
# can never overturn a higher-confidence rule. Stays purely additive:
# scoring sort goes (conf, weight, specificity) — labels tied on the
# first two now lose to the more-specific name instead of falling
# through to dict-order.

_LABEL_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "and", "or", "with", "to", "in",
    "on", "by", "as", "is", "it", "via",
})


def _label_specificity(label: str) -> int:
    """Count non-stopword tokens in a Title-Cased product label.

    "AI" → 1, "AI Email Assistant" → 3, "The Database" → 1
    (the+stopword=0, database=1). Used as a tertiary tiebreaker in
    :func:`_resolve_votes`.
    """
    if not label:
        return 0
    tokens = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9]*", label)]
    return sum(1 for t in tokens if t not in _LABEL_STOPWORDS)


# ── Rule 1 — workspace cluster ──────────────────────────────────────────


def _apply_workspace_rule(
    developer_features: list["Feature"],
    state: _ClustererState,
) -> tuple[int, int]:
    """Cast a vote for any dev feature whose paths concentrate ≥70%
    under a single ``apps/<X>/`` or ``packages/<X>/`` workspace.

    Sprint B3.1 — after identifying the workspace prefix, attempt
    deterministic domain-noun extraction. When a noun wins the
    structural vote, the cluster is labelled with the DOMAIN noun
    ("Documents", "Data Room") and the vote is tagged
    ``rule="workspace+domain"`` at the noun's own confidence (0.50–0.85).
    Otherwise the legacy workspace label is used (rule=``"workspace"``,
    confidence 0.6) so we never regress prior behaviour.

    Returns ``(total_votes, domain_refined_votes)`` for telemetry.
    """
    votes_cast = 0
    domain_votes = 0
    for f in developer_features:
        paths = list(f.paths or [])
        if not paths:
            continue
        per_ws: dict[str, int] = defaultdict(int)
        for p in paths:
            ws = _workspace_for_path(p)
            if ws is not None:
                per_ws[ws] += 1

        # Sprint B3.1 — branch by monorepo vs flat layout.
        # Both branches require domain-noun signal at >= 0.70 (route-group
        # or first-non-generic-dir). The 0.50 filename-stem fallback is
        # too noisy in production — it fires on randomly-named test
        # files and creates garbage labels like "Useinputstreamsend"
        # observed during validation. Per no-magic-tuning rule the
        # threshold is universal, not per-repo.
        _DOMAIN_NOUN_MIN_CONF = 0.70
        if per_ws:
            # ── Monorepo branch — workspace prefix detected ──
            ws_name, ws_count = max(per_ws.items(), key=lambda kv: kv[1])
            share = ws_count / len(paths)
            if share < _WORKSPACE_CONCENTRATION_MIN:
                continue
            workspace_prefix = _workspace_prefix_for(paths, ws_name)
            noun = extract_domain_noun(paths, workspace_prefix=workspace_prefix)
            fallback_label = _titleize(ws_name)
            anchor_workspace = f"workspace:{ws_name}"
            weight = share
        else:
            # ── Flat-layout branch — no apps/<X>/, run domain-noun from root.
            # We can't manufacture a fallback workspace label for flat
            # repos (the workspace IS the repo root), so we ONLY emit
            # a vote when domain-noun fires.
            noun = extract_domain_noun(paths, workspace_prefix="")
            if noun is None or noun.confidence < _DOMAIN_NOUN_MIN_CONF:
                continue
            fallback_label = None
            anchor_workspace = "workspace:root"
            weight = 1.0

        if noun is not None and noun.confidence >= _DOMAIN_NOUN_MIN_CONF:
            label = noun.label
            rule = "workspace+domain"
            # Vote confidence reflects domain-noun strength; floor at 0.6 so
            # workspace+domain is never WEAKER than the bare workspace vote.
            confidence = max(_CONF_WORKSPACE, noun.confidence)
            anchor_signal = f"{anchor_workspace}+domain:{noun.token}"
            domain_votes += 1
        else:
            # Only reachable in monorepo branch (flat branch already
            # continued when noun is None).
            assert fallback_label is not None
            label = fallback_label
            rule = "workspace"
            confidence = _CONF_WORKSPACE
            anchor_signal = anchor_workspace

        # ── Sprint E2 — phantom-cluster filter ──
        # Skip emission when the candidate label is a universally
        # junk name (infrastructure, folder-layout, build tooling, or
        # catch-all). Dep-anchor (Rule 2) is unaffected and can still
        # emit a legitimately-named cluster for the same dev feature.
        if label in _PHANTOM_CLUSTER_NAMES:
            continue

        state.votes[f.name].append(
            _Vote(
                product_label=label,
                rule=rule,
                confidence=confidence,
                anchor_signal=anchor_signal,
                weight=weight,
            ),
        )
        votes_cast += 1
    return votes_cast, domain_votes


def _workspace_prefix_for(paths: list[str], ws_name: str) -> str:
    """Return the actual prefix that matched (e.g. ``apps/web`` vs
    ``packages/web``) so the domain-noun extractor can strip it.

    We inspect the first path that sits under the dominant workspace
    name and return its monorepo prefix + workspace name.
    """
    for p in paths:
        norm = p.replace("\\", "/").lstrip("./").lstrip("/")
        m = _WORKSPACE_PREFIX_RE.match(norm)
        if m and m.group(1) == ws_name:
            # Reconstruct the full prefix from the regex match.
            return norm[:m.end()].rstrip("/")
    # Fallback — shouldn't hit since caller already confirmed the
    # workspace appeared in per_ws.
    return ws_name


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
    aliases_by_label = _get_dep_aliases()

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
            # Sprint Rails H3 — semantic name match guard. The dep
            # import alone is no longer sufficient: the feature's
            # name or one of its file basenames must carry at least
            # one of the label's name-alias substrings. Stops an
            # unrelated feature ("Addresses") from being claimed by
            # a category ("Realtime", "Billing") just because the
            # category's dependency happens to be in the project.
            aliases = aliases_by_label.get(label, ())
            if not _feature_matches_aliases(f.name, paths, aliases):
                continue
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

    def _label_score(label: str) -> tuple[float, float, int]:
        vs = by_label[label]
        # Score primary on confidence (highest rule wins), then on
        # accumulated weight, then on name specificity (Sprint E5).
        # Specificity is a TIEBREAKER ONLY — it never overturns a
        # higher-confidence rule, only resolves ties where generic
        # one-word labels (``AI``, ``Auth``) compete with specific
        # multi-word labels (``AI Email Assistant``).
        return (
            max(v.confidence for v in vs),
            sum(v.weight for v in vs),
            _label_specificity(label),
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
    workspace_votes, domain_refined_votes = _apply_workspace_rule(
        developer_features, state,
    )

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

    # Fraction of workspace votes that got domain-noun refinement.
    # Caps at 1.0; equals 0.0 when no workspace votes fired at all.
    domain_rate = (
        round(domain_refined_votes / workspace_votes, 3)
        if workspace_votes else 0.0
    )

    telemetry: dict[str, Any] = {
        "product_features_count": len(product_features_out),
        "developer_features_total": total,
        "developer_features_mapped_count": mapped,
        "developer_features_mapped_pct": round(mapped / total, 3) if total else 0.0,
        "developer_features_orphan_count": max(total - mapped, 0),
        "product_clusterer_source_breakdown": dict(source_breakdown),
        "product_clusterer_votes_cast": {
            "workspace": workspace_votes,
            "workspace+domain": domain_refined_votes,
            "dep-anchor": dep_votes,
            "customer-yaml": customer_votes,
        },
        "domain_noun_extraction_rate": domain_rate,
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
