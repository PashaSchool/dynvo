"""Stage 4 residual guards (Sprint S2b).

Stage 4 (residual LLM-fallback + singleton synthesizer) historically
over-emitted *single-path phantoms*: features named after one file
that carries no real product signal — root config dot-files
(``prettier.config.js``), per-workspace boilerplate
(``apps/X/.gitignore``, ``apps/X/Dockerfile``, ``apps/X/README.md``),
test fixtures, generated diagrams, etc. On ``trigger.dev`` Stage 4
emitted 159 features against ~30 truth-equivalents — ~70 of them
visibly phantoms (the FRESH-S2b baseline scan, 2026-05-18).

This module is the deterministic, structural admission filter applied
to Stage 4's output BEFORE it leaves the stage.

Two guards
==========

* **Guard A — singleton admission.** A feature with exactly one path
  is admitted ONLY if at least one of the following is true:

    1. Path is a root-level product-config file with a structured
       extension (``.json|.yaml|.yml|.toml``) — these are the
       declarative product manifests Stage 1's
       ``config-as-product-extractor`` is designed for. Known
       dependency manifests have already been suppressed upstream
       by ``synthesize_singleton_feature``; what remains here is
       product-config (``tauri.conf.json``, ``app.json``,
       ``manifest.json``, ``vercel.json``...).

    2. The feature name shares at least one *non-generic* slug
       token with an existing Stage 2 anchor — proves the singleton
       is related to a deterministic feature, not an orphan.

    3. The path's leaf stem is a *distinct product noun* — i.e. not
       in the universal ``_GENERIC_FILE_STEMS`` set
       (``index``, ``main``, ``utils``, ``helpers``, ``types``,
       ``constants``, ``setup``, ``README``, ``LICENSE``, ``CHANGELOG``,
       and similar universal boilerplate). The stem comes from the
       path leaf, not from the synthesized feature name, so this is
       a structural test about the file, not the name.

  Otherwise: drop. The drop event is reported via ``DropEvent``.

* **Guard B — cluster cohesion.** A feature with ≥2 paths is admitted
  unchanged iff ALL paths share either:

    1. The same parent directory, OR
    2. The same top-2 path segments (e.g. ``apps/web/billing/...`` —
       a workspace-scoped concept).

  Otherwise the cluster is *incoherent*: the LLM has stitched
  unrelated files together (``apps/coordinator/Containerfile`` +
  ``apps/docker-provider/Containerfile`` + ``apps/supervisor/...`` —
  same filename, different workspaces, NOT one product feature).
  The cluster is split into per-path singletons and each is re-run
  through Guard A.

Both guards are STRUCTURAL — no per-corpus numeric tuning. They derive
from path shape, name shape, and Stage 2 anchor membership.

Telemetry
=========

Every drop / split returns ``DropEvent`` records the caller stitches
into ``scan_meta``:

* ``stage_4_singletons_dropped`` (Guard A drops)
* ``stage_4_incoherent_clusters_split`` (Guard B splits)
* ``stage_4_drops_sample`` (≤5 examples — name, reason, path)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature


# ── Constants ─────────────────────────────────────────────────────────


# Extensions of declarative product-config files. Combined with a
# root-depth check (parts == 1) to identify
# ``config-as-product-extractor``-shaped surfaces that escaped Stage 1.
_PRODUCT_CONFIG_EXTS: frozenset[str] = frozenset({
    ".json", ".yaml", ".yml", ".toml",
})

# Universal generic file stems — files whose leaf carries no product
# meaning regardless of stack. A singleton whose only signal is one of
# these has nothing to anchor a Layer-1 feature on. Kept UNIVERSAL:
# do NOT add stack-specific tokens (per memory/rule-no-magic-tuning).
_GENERIC_FILE_STEMS: frozenset[str] = frozenset({
    # Universal entry-point / index names
    "index", "main", "app", "root", "entry",
    # Boilerplate documentation surfaces
    "readme", "license", "licence", "changelog", "contributing",
    "code-of-conduct", "codeowners", "authors", "notice", "claude",
    "notes", "todo",
    # Universal helper bundles
    "utils", "util", "helpers", "helper", "common", "shared",
    "types", "type", "constants", "const", "enums", "enum",
    "interfaces", "interface", "models", "schema", "schemas",
    # Universal lifecycle / scaffolding
    "setup", "init", "bootstrap", "globals", "global",
    "config", "configs", "settings", "env", "example",
    "gitignore", "dockerignore", "helmignore", "npmignore",
    "editorconfig", "eslintrc", "prettierrc", "stylelintrc",
    "browserslistrc",
    # Test / CI / build tool config leaves — these are tooling
    # surfaces, NOT product features. Keep universal across stacks.
    "jest", "vitest", "playwright", "cypress", "karma", "mocha",
    "prettier", "eslint", "stylelint", "babel", "webpack", "rollup",
    "vite", "tsup", "swc", "tsc", "esbuild", "postcss", "tailwind",
    "changeset", "lerna", "nx", "turbo", "rush", "pnpm",
    "docker", "compose", "kubernetes", "helm",
    "ci", "cd",
    "dockerfile", "containerfile", "makefile", "rakefile", "gulpfile",
    "gemfile", "procfile",
    # Generated / artifact leaves
    "lock", "manifest", "build", "dist", "diagram",
})

# Slug tokens that carry no discriminative weight when used as the
# pivot for "does this singleton overlap an anchor?". This is the
# TOKEN-level twin of :data:`_GENERIC_FILE_STEMS` — anything that
# qualifies as boilerplate at the path-leaf level also qualifies as
# anchor-pool noise (e.g. ``env`` in ``env-example`` should NOT count
# as an anchor overlap just because the repo has an ``env`` route).
# Kept UNIVERSAL: do NOT add stack-specific tokens.
_GENERIC_SLUG_TOKENS: frozenset[str] = frozenset({
    # Universal path-layout vocabulary
    "app", "apps", "src", "lib", "libs", "core", "base", "main",
    "index", "root", "common", "shared", "util", "utils",
    "components", "pages", "routes", "api", "server", "client",
    "frontend", "backend", "config", "configs", "settings",
    "test", "tests", "docs", "doc", "scripts", "build", "dist",
    "internal", "packages", "package", "module", "modules",
    # Boilerplate-marker tokens (same vocabulary as _GENERIC_FILE_STEMS).
    # An ``env-example`` synth name should not anchor-overlap a real
    # ``env`` feature — both tokens are generic markers, not nouns.
    "env", "example", "examples", "readme", "license", "licence",
    "changelog", "notes", "todo", "claude", "gitignore",
    "dockerignore", "editorconfig", "eslintrc", "prettierrc",
    "stylelintrc", "browserslistrc",
    # Tool-config marker tokens.
    "jest", "vitest", "playwright", "cypress", "karma", "mocha",
    "prettier", "eslint", "stylelint", "babel", "webpack", "rollup",
    "vite", "tsup", "swc", "tsc", "esbuild", "postcss", "tailwind",
    "changeset", "lerna", "nx", "turbo", "rush", "pnpm",
    "docker", "compose", "kubernetes", "helm", "ci", "cd",
    "dockerfile", "containerfile", "makefile", "rakefile", "gulpfile",
    "gemfile", "procfile",
    # Universal helper-bundle markers.
    "helper", "helpers", "type", "types", "constant", "constants",
    "enum", "enums", "interface", "interfaces", "model", "models",
    "schema", "schemas", "global", "globals",
    "setup", "init", "bootstrap", "entry",
    "manifest", "lock", "diagram",
})


# ── Public data types ────────────────────────────────────────────────


@dataclass(frozen=True)
class DropEvent:
    """One feature dropped or split by the guards."""

    name: str
    reason: str         # ``singleton_no_signal`` | ``incoherent_cluster_split``
    path: str           # the file path (or a representative one)


@dataclass
class GuardResult:
    """Output of :func:`apply_stage_4_guards`."""

    kept: list["DeveloperFeature"]
    drops: list[DropEvent]
    singletons_dropped: int            # Guard A drops
    incoherent_clusters_split: int     # Guard B splits


# ── Helpers ──────────────────────────────────────────────────────────


def _slug_tokens(name: str) -> set[str]:
    """Tokenize a kebab-case slug, stripping universal generics."""
    if not name:
        return set()
    tokens = {t for t in name.split("-") if t}
    return {t for t in tokens if t not in _GENERIC_SLUG_TOKENS}


def _path_parts(path: str) -> list[str]:
    return [p for p in PurePosixPath(path).parts if p]


def _leaf_stem(path: str) -> str:
    """Lowercased filename without final extension and without leading dots.

    Examples:
        ``README.md`` -> ``readme``
        ``prettier.config.js`` -> ``prettier-config``
        ``.gitignore`` -> ``gitignore``
        ``Dockerfile`` -> ``dockerfile``
    """
    parts = _path_parts(path)
    if not parts:
        return ""
    leaf = parts[-1].lower()
    if leaf.startswith("."):
        leaf = leaf.lstrip(".")
    # Strip final extension only when there is one and the basename
    # has at least one non-extension token (so ``readme.md`` -> ``readme``
    # but ``dockerfile`` stays ``dockerfile``).
    if "." in leaf:
        leaf = leaf.rsplit(".", 1)[0]
    # Normalise dot/underscore-style names to a single token using
    # dashes (``prettier.config`` -> ``prettier-config``).
    out_chars: list[str] = []
    prev_dash = False
    for ch in leaf:
        if ch.isalnum():
            out_chars.append(ch)
            prev_dash = False
        elif ch in ("_", "-", "."):
            if out_chars and not prev_dash:
                out_chars.append("-")
                prev_dash = True
    while out_chars and out_chars[-1] == "-":
        out_chars.pop()
    return "".join(out_chars)


def _is_root_product_config(path: str) -> bool:
    """``True`` for a depth-1 file with a declarative-config extension."""
    parts = _path_parts(path)
    if len(parts) != 1:
        return False
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in _PRODUCT_CONFIG_EXTS


def _overlaps_anchor_tokens(
    feature_name: str,
    anchor_token_pool: frozenset[str],
) -> bool:
    """``True`` iff a non-generic token of ``feature_name`` is in the pool."""
    feature_tokens = _slug_tokens(feature_name)
    return bool(feature_tokens & anchor_token_pool)


def _is_distinct_product_noun(path: str) -> bool:
    """``True`` iff the path's leaf stem carries a distinct product noun.

    A "distinct product noun" is a token that is:
      * alphabetic (contains at least one a-z char — numeric / version
        suffixes like ``v1``, ``0``, ``2024`` don't qualify on their own),
      * NOT in :data:`_GENERIC_FILE_STEMS` (universal scaffolding /
        boilerplate / tool-config words),
      * NOT a single character (``a``, ``b`` — too thin to anchor a
        feature).
    """
    stem = _leaf_stem(path)
    if not stem:
        return False
    sub = [t for t in stem.split("-") if t]
    if not sub:
        return False
    for token in sub:
        if not any(c.isalpha() for c in token):
            continue
        if len(token) <= 1:
            continue
        if token in _GENERIC_FILE_STEMS:
            continue
        return True
    return False


def _is_admissible_singleton(
    feature: "DeveloperFeature",
    anchor_token_pool: frozenset[str],
) -> bool:
    """Apply Guard A's three-prong admission test to a 1-path feature.

    The feature is admitted if ANY prong passes.
    """
    if not feature.paths:
        return False
    path = feature.paths[0]
    # Prong 1: root-level declarative product-config file.
    if _is_root_product_config(path):
        return True
    # Prong 2: name token overlaps a Stage 2 anchor token.
    if _overlaps_anchor_tokens(feature.name, anchor_token_pool):
        return True
    # Prong 3: leaf stem is a distinct product noun (not boilerplate).
    if _is_distinct_product_noun(path):
        return True
    return False


def _is_cohesive_cluster(paths: tuple[str, ...]) -> bool:
    """``True`` iff a multi-path cluster shares structural locality.

    Cohesion rules:
      1. All paths share the same parent directory, OR
      2. All paths share the same top-2 path segments
         (e.g. ``apps/web/`` — workspace-scoped concept).
    """
    if len(paths) < 2:
        return True  # singleton handled by Guard A elsewhere
    parents: set[str] = set()
    top2s: set[str] = set()
    for p in paths:
        parts = _path_parts(p)
        if len(parts) <= 1:
            # A root-level file in an otherwise multi-path cluster is
            # automatically incoherent — root files don't share
            # locality with anything.
            return False
        parents.add("/".join(parts[:-1]))
        if len(parts) >= 2:
            top2s.add("/".join(parts[:2]))
    return len(parents) == 1 or len(top2s) == 1


def _build_anchor_token_pool(
    existing_features: Iterable["DeveloperFeature"],
) -> frozenset[str]:
    """Pool of non-generic slug tokens across every Stage 2 anchor name."""
    pool: set[str] = set()
    for f in existing_features:
        pool |= _slug_tokens(f.name)
    return frozenset(pool)


def _split_into_singletons(
    feature: "DeveloperFeature",
) -> list["DeveloperFeature"]:
    """Return one synthesized singleton feature per path of ``feature``.

    Falls back to :func:`synthesize_singleton_feature` so the resulting
    singletons obey the same naming / skip rules as a normally-emitted
    singleton (root dotfile rejection, manifest skip, etc.).
    """
    from faultline.pipeline_v2.residual_clusterer import (
        synthesize_singleton_feature,
    )

    out: list["DeveloperFeature"] = []
    seen_names: set[str] = set()
    for p in feature.paths:
        new_feat = synthesize_singleton_feature(p)
        if new_feat is None:
            continue
        if new_feat.name in seen_names:
            continue
        seen_names.add(new_feat.name)
        out.append(new_feat)
    return out


# ── Public entry point ──────────────────────────────────────────────


def apply_stage_4_guards(
    residual: list["DeveloperFeature"],
    existing_features: list["DeveloperFeature"],
    *,
    drop_sample_cap: int = 5,
    split_incoherent: bool = False,
) -> GuardResult:
    """Apply Guard A + Guard B to Stage 4's residual feature list.

    Args:
        residual: features emitted by Stage 4 (singleton-synth + LLM).
        existing_features: Stage 2 deterministic features. Their slug
            tokens form the anchor-overlap pool used by Guard A's
            prong 2.
        drop_sample_cap: how many drop events to retain in
            ``GuardResult.drops`` (telemetry). Matches the sprint
            target of 5 sample entries in ``scan_meta``.
        split_incoherent: when ``True``, an incoherent multi-path
            cluster is exploded into per-path singletons and each is
            re-checked through Guard A (admits any spawn that has a
            distinct product noun in its leaf). When ``False`` (the
            shipped default), the entire incoherent cluster is dropped.
            Splitting empirically added more spawned singletons than it
            removed phantom clusters on the validation corpus
            (``papermark`` 118 -> 166, +40%); ``split_incoherent=True``
            is retained as an opt-in for future deepening of Guard B.

    Returns:
        :class:`GuardResult` with the surviving features in their
        original order plus telemetry.
    """
    anchor_token_pool = _build_anchor_token_pool(existing_features)
    # Also include surviving residual feature names as we go so that
    # later residuals can anchor against earlier admitted residuals.
    running_token_pool: set[str] = set(anchor_token_pool)

    kept: list["DeveloperFeature"] = []
    drops: list[DropEvent] = []
    singletons_dropped = 0
    clusters_split = 0

    def _maybe_record_drop(name: str, reason: str, path: str) -> None:
        if len(drops) < drop_sample_cap:
            drops.append(DropEvent(name=name, reason=reason, path=path))

    for feat in residual:
        if len(feat.paths) <= 1:
            # ── Guard A path ──
            if _is_admissible_singleton(
                feat, frozenset(running_token_pool),
            ):
                kept.append(feat)
                running_token_pool |= _slug_tokens(feat.name)
            else:
                singletons_dropped += 1
                _maybe_record_drop(
                    name=feat.name,
                    reason="singleton_no_signal",
                    path=feat.paths[0] if feat.paths else "",
                )
            continue

        # ── Guard B path ──
        if _is_cohesive_cluster(feat.paths):
            kept.append(feat)
            running_token_pool |= _slug_tokens(feat.name)
            continue

        # Incoherent multi-path cluster.
        clusters_split += 1
        _maybe_record_drop(
            name=feat.name,
            reason="incoherent_cluster_split"
                   if split_incoherent
                   else "incoherent_cluster_dropped",
            path=feat.paths[0],
        )
        if not split_incoherent:
            # Conservative default: drop the whole incoherent cluster.
            # Spawned-singleton re-admission emitted more net features
            # than it suppressed on the S2b validation corpus.
            continue
        # Opt-in spawn path (kept for telemetry experiments).
        for spawn in _split_into_singletons(feat):
            if _is_admissible_singleton(
                spawn, frozenset(running_token_pool),
            ):
                kept.append(spawn)
                running_token_pool |= _slug_tokens(spawn.name)
            else:
                singletons_dropped += 1
                # Don't double-count this in the sample — the parent
                # split is already recorded.

    return GuardResult(
        kept=kept,
        drops=drops,
        singletons_dropped=singletons_dropped,
        incoherent_clusters_split=clusters_split,
    )


__all__ = [
    "DropEvent",
    "GuardResult",
    "apply_stage_4_guards",
]
