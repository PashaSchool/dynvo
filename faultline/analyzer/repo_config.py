"""Repo-level configuration loader.

Reads an optional ``.faultline.yaml`` (or ``.faultline.yml``) from the
analyzed repo's root and applies its rules to the post-dedup feature
map. Lets users encode their **own** product taxonomy without
touching faultline source code.

Schema (every key optional)::

    # .faultline.yaml — repo-level Faultlines config
    features:
      billing-and-subscriptions:
        description: Stripe-based billing and subscription management.
        variants:
          - lib/billing-and-subscriptions
          - ee/stripe-billing
          - trpc/enterprise-billing-and-identity

      embedded-signing:
        variants:
          - remix/embedded-signing-authoring
          - lib/embedded-signing

    skip_features:
      - tsconfig
      - tailwind-config

    force_merges:
      - into: design-system
        from:
          - ui/primitive-components
          - ui-primitives
        description: Reusable UI primitives shared across the app.

Behaviour:

  - Each ``features`` entry: any detected feature whose name matches
    one of ``variants`` is renamed to the canonical key. Optionally
    sets / overwrites the description.
  - ``skip_features``: detected features matching one of these names
    are dropped entirely.
  - ``force_merges``: behaves like a manual Sprint 2 dedup op; files
    are unioned into ``into``.

Failure mode: malformed config raises a clear error early. Missing
file is silent — config is opt-in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError as _exc:  # pragma: no cover
    raise ImportError(
        "PyYAML is required for repo_config; install with `pip install pyyaml`."
    ) from _exc


logger = logging.getLogger(__name__)


# Filenames searched, in priority order, at the repo root.
_CONFIG_FILENAMES: tuple[str, ...] = (
    ".faultline.yaml",
    ".faultline.yml",
    "faultline.config.yaml",
    "faultline.config.yml",
)


# Synthetic buckets — never auto-locked, never aliased away.
# Inlined here (rather than imported from llm.dedup) so this
# module stays free of an analyzer → llm dependency.
_PROTECTED_NAMES: frozenset[str] = frozenset({
    "documentation",
    "shared-infra",
    "examples",
})


# ── Dataclasses ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class FeatureRule:
    """One canonical-feature entry from the repo config."""

    canonical: str
    description: str = ""
    variants: tuple[str, ...] = ()


@dataclass(frozen=True)
class ForcedMerge:
    """One ``force_merges`` entry."""

    into: str
    sources: tuple[str, ...] = ()
    description: str = ""


@dataclass
class RepoConfig:
    """In-memory view of a repo's ``.faultline.yaml``.

    Every field defaults to empty so a partially-populated config is
    still useful. ``source_path`` records where the config was loaded
    from, for log lines.

    ``auto_aliases`` is engine-managed (Improvement #4): after each
    successful scan we write the names of stable detected features
    into a separate top-level ``auto_aliases:`` section. Subsequent
    runs lock those names against Sprint 5 critique renaming so the
    same feature keeps the same label scan-to-scan.
    """

    features: list[FeatureRule] = field(default_factory=list)
    skip_features: list[str] = field(default_factory=list)
    force_merges: list[ForcedMerge] = field(default_factory=list)
    auto_aliases: list[FeatureRule] = field(default_factory=list)
    source_path: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.features or self.skip_features
            or self.force_merges or self.auto_aliases
        )

    def all_canonical_names(self) -> frozenset[str]:
        """Names from BOTH user-managed features and auto_aliases.

        Used by the pipeline to lock Sprint 5 critique against
        renaming any stable name.
        """
        names: set[str] = {r.canonical for r in self.features}
        names.update(r.canonical for r in self.auto_aliases)
        return frozenset(names)


# ── Loader ───────────────────────────────────────────────────────────


def find_repo_config(repo_root: Path | str) -> Path | None:
    """Return the first existing config filename under ``repo_root``."""
    root = Path(repo_root)
    for name in _CONFIG_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def load_repo_config(repo_root: Path | str) -> RepoConfig | None:
    """Load the repo's ``.faultline.yaml`` if present.

    Returns ``None`` when no config file exists. Returns a populated
    :class:`RepoConfig` on success. Raises ``ValueError`` on malformed
    YAML so the user gets an early signal instead of silent drift.
    """
    path = find_repo_config(repo_root)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{path}: cannot read ({exc})") from exc

    data = yaml.safe_load(text)
    if data is None:
        return RepoConfig(source_path=str(path))
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: top-level must be a mapping, got {type(data).__name__}"
        )

    features = _parse_features(data.get("features"), path)
    skip = _parse_skip(data.get("skip_features"), path)
    forced = _parse_force_merges(data.get("force_merges"), path)
    auto = _parse_features(data.get("auto_aliases"), path)

    cfg = RepoConfig(
        features=features,
        skip_features=skip,
        force_merges=forced,
        auto_aliases=auto,
        source_path=str(path),
    )
    logger.info(
        "repo_config: loaded %s — %d user features, %d auto_aliases, "
        "%d skips, %d force-merges",
        path, len(features), len(auto), len(skip), len(forced),
    )
    return cfg


def _parse_features(raw, source: Path) -> list[FeatureRule]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ValueError(f"{source}: 'features' must be a mapping")

    out: list[FeatureRule] = []
    seen: set[str] = set()
    for canonical, body in raw.items():
        canonical = str(canonical).strip()
        if not canonical:
            raise ValueError(f"{source}: empty canonical feature name")
        if canonical in seen:
            raise ValueError(f"{source}: duplicate canonical feature {canonical!r}")
        seen.add(canonical)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise ValueError(
                f"{source}: feature {canonical!r} body must be a mapping"
            )
        variants_raw = body.get("variants") or []
        if not isinstance(variants_raw, list):
            raise ValueError(
                f"{source}: variants for {canonical!r} must be a list"
            )
        variants = tuple(
            v for v in (str(x).strip() for x in variants_raw) if v
        )
        out.append(FeatureRule(
            canonical=canonical,
            description=str(body.get("description") or "").strip(),
            variants=variants,
        ))
    return out


def _parse_skip(raw, source: Path) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{source}: 'skip_features' must be a list")
    return [s for s in (str(x).strip() for x in raw) if s]


def _parse_force_merges(raw, source: Path) -> list[ForcedMerge]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{source}: 'force_merges' must be a list")
    out: list[ForcedMerge] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"{source}: force_merges entry must be a mapping")
        into = str(entry.get("into") or "").strip()
        if not into:
            raise ValueError(f"{source}: force_merge missing 'into'")
        srcs_raw = entry.get("from") or []
        if not isinstance(srcs_raw, list):
            raise ValueError(f"{source}: force_merge 'from' must be a list")
        srcs = tuple(s for s in (str(x).strip() for x in srcs_raw) if s)
        if len(srcs) < 1:
            raise ValueError(
                f"{source}: force_merge into {into!r} needs at least 1 source"
            )
        out.append(ForcedMerge(
            into=into,
            sources=srcs,
            description=str(entry.get("description") or "").strip(),
        ))
    return out


# ── Auto-save (Improvement #4) ────────────────────────────────────────


# Features below this size are too noisy to lock in — they're often
# small ancillary helpers that legitimately get renamed across runs
# as the engine learns more about the codebase. Locking them too
# eagerly would freeze in early-iteration mistakes.
_AUTO_LOCK_MIN_FILES = 10


def auto_save_canonicals(
    repo_root: Path | str,
    detected: dict[str, list[str]],
    descriptions: dict[str, str] | None = None,
    *,
    write_if_missing: bool = False,
) -> int:
    """Write stable canonical feature names back to ``.faultline.yaml``.

    For each detected feature that is:
      - not synthetic (``documentation`` / ``shared-infra`` /
        ``examples``)
      - at or above :data:`_AUTO_LOCK_MIN_FILES` files
      - not already declared in user-managed ``features:``

    ...append a stub under ``auto_aliases:`` with the engine's
    description. Subsequent scans see the name in
    :meth:`RepoConfig.all_canonical_names`, lock it against Sprint
    5 critique, and the label sticks.

    Behaviour:
      - When the repo already has a ``.faultline.yaml`` the function
        merges into it (preserving all user content). Only the
        ``auto_aliases:`` block is rewritten.
      - When no config exists, no file is created unless
        ``write_if_missing=True`` (default False — we don't want to
        litter every scanned repo with a config file the user didn't
        ask for).
      - Returns the count of NEW canonicals written. ``0`` is the
        common steady-state once the repo has stabilised.

    Failure modes (write errors, malformed existing YAML) are logged
    and the function returns ``0`` — auto-save is best-effort, never
    blocks the scan.
    """
    if not detected:
        return 0

    repo_root = Path(repo_root)
    config_path = find_repo_config(repo_root)
    if config_path is None:
        if not write_if_missing:
            return 0
        config_path = repo_root / ".faultline.yaml"

    descriptions = descriptions or {}

    # Load existing content so we preserve user edits.
    try:
        existing = (
            yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if config_path.exists() else None
        ) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "repo_config: cannot read %s for auto-save (%s) — skipping",
            config_path, exc,
        )
        return 0
    if not isinstance(existing, dict):
        logger.warning(
            "repo_config: %s top-level is not a mapping — skipping auto-save",
            config_path,
        )
        return 0

    # Names the user has explicitly declared — never auto-write
    # over them.
    user_names: set[str] = set()
    raw_user = existing.get("features") or {}
    if isinstance(raw_user, dict):
        user_names.update(str(k).strip() for k in raw_user.keys())

    # Existing auto_aliases — preserve descriptions where the engine
    # didn't supply a fresh one this run.
    raw_auto = existing.get("auto_aliases") or {}
    prev_auto: dict[str, dict] = {}
    if isinstance(raw_auto, dict):
        prev_auto = {
            str(k).strip(): (v if isinstance(v, dict) else {})
            for k, v in raw_auto.items()
        }

    new_auto: dict[str, dict] = {}
    new_count = 0
    for name, files in detected.items():
        if name in _PROTECTED_NAMES:
            continue
        if name in user_names:
            continue
        if len(files) < _AUTO_LOCK_MIN_FILES:
            continue
        prev = prev_auto.get(name) or {}
        desc = descriptions.get(name) or prev.get("description") or ""
        entry: dict[str, object] = {}
        if desc:
            entry["description"] = desc
        # Preserve any user-curated variants that crept into this
        # auto entry on a previous run + manual edit.
        prev_variants = prev.get("variants")
        if isinstance(prev_variants, list) and prev_variants:
            entry["variants"] = list(prev_variants)
        new_auto[name] = entry
        if name not in prev_auto:
            new_count += 1

    # Rebuild the file: keep all user keys verbatim; overwrite only
    # ``auto_aliases``.
    output = dict(existing)
    if new_auto:
        output["auto_aliases"] = new_auto
    elif "auto_aliases" in output:
        del output["auto_aliases"]

    try:
        text = (
            "# Faultlines repo config — auto-generated after each scan.\n"
            "#\n"
            "# auto_aliases below = ALL feature names from the latest "
            "scan. Engine\n"
            "# locks these so the NEXT scan produces stable names with "
            "the SAME count.\n"
            "# Sub-features like ``foo/bar`` stay split — engine does "
            "NOT auto-collapse\n"
            "# them into a parent, so re-running ``analyze`` returns "
            "the same taxonomy.\n"
            "#\n"
            "# How to CONSOLIDATE sub-features into a parent (e.g. "
            "merge\n"
            "# ``network-detections/triage`` + "
            "``network-detections/playbooks`` into\n"
            "# a single ``network-detections``):\n"
            "#   1. Add the parent name (no slash) to the ``features:`` "
            "block above\n"
            "#   2. Delete the matching ``network-detections/...`` "
            "lines from auto_aliases\n"
            "#   3. Re-run ``dynvo scan``\n"
            "#\n"
            "# How to RENAME a feature without breaking flow attribution: "
            "edit the\n"
            "# canonical key in-place. The file → feature cache at\n"
            "# ``~/.faultline/assignments-<repo>.json`` will renormalize "
            "the next scan.\n"
            "#\n"
            "# Anything in ``features:`` is user-owned and preserved "
            "verbatim.\n\n"
        )
        text += yaml.safe_dump(
            output, sort_keys=False, allow_unicode=True, indent=2,
        )
        config_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "repo_config: cannot write %s for auto-save (%s) — skipping",
            config_path, exc,
        )
        return 0

    logger.info(
        "repo_config: auto-save wrote %d new canonical(s) to %s "
        "(total auto_aliases now: %d)",
        new_count, config_path, len(new_auto),
    )
    return new_count
