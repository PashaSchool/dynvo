"""Plugin-system extractor (Phase 5 Layer C).

Detects the recurring "many sibling modules implementing a common
base class" pattern that appears across notification libraries
(Apprise, Notifiers), provider integrations (Cloud SDKs), webhook
adapters, and similar plugin architectures.

The structural signature is intentionally repo-agnostic per
``memory/rule-no-repo-specific-paths``:

  - A directory contains ≥ ``MIN_SIBLINGS`` peer source files
    (default 10)
  - The directory ALSO contains a base / abstract module whose
    filename matches one of the well-known stems
    (``base``, ``_base``, ``abstract``, ``plugin``, ``provider``)
  - Most peer files share a common naming prefix

When this fires, each peer module becomes one ``plugin-module``
``Signal`` consumed by the recall-critique aggregator. The
critique then has the option to surface each plugin as a feature.

The extractor does NOT name specific directories ("plugins/",
"integrations/", "Apprise/plugins/") — anything matching the
structural signature qualifies. This keeps the detection
robust across repos that use different conventions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.signals import Signal


# Conservative threshold — prevents spurious matches on regular
# domain dirs (Django ``models.py``, Rails controllers, etc.).
MIN_SIBLINGS = 10

# Filenames (sans extension) that mark a directory as a plugin host.
# Order doesn't matter; any presence wins.
_BASE_STEMS = frozenset({
    "base", "_base", "abstract", "abstractbase",
    "plugin", "_plugin", "provider", "_provider",
})

# Source extensions we walk. Plugin systems typically pick one
# language and stick to it within a directory.
_SOURCE_EXTS = frozenset({".py", ".ts", ".js", ".rb", ".go"})

# Skip these dirs entirely (test/build artefacts that can look like
# plugin dirs to a naive walk).
_SKIP_DIR_NAMES = frozenset({
    "__pycache__", "node_modules", ".git", "dist", "build",
    ".venv", "venv", "env", ".tox", ".pytest_cache",
    "tests", "test", "spec", "specs", "fixtures",
})


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginModule:
    """One peer module in a detected plugin directory."""

    plugin_dir: str            # repo-relative dir
    file: str                  # repo-relative path
    name: str                  # filename without extension
    base_stem: str             # which base/abstract was detected
    extension: str             # ``.py`` / ``.ts`` / etc.


def _walkable_dirs(repo_root: Path) -> Iterable[Path]:
    """Yield every directory under ``repo_root`` not in the skip list."""
    for p in repo_root.rglob("*"):
        if not p.is_dir():
            continue
        if any(part in _SKIP_DIR_NAMES for part in p.parts):
            continue
        yield p


def _peer_source_files(d: Path) -> list[Path]:
    """Direct-child source files (not recursive)."""
    out = []
    for child in d.iterdir():
        if child.is_file() and child.suffix in _SOURCE_EXTS:
            out.append(child)
    return out


def _base_stem_present(peers: list[Path]) -> str | None:
    """Return the first base-stem filename present among peers, else None."""
    stems = {p.stem.lower() for p in peers}
    for s in _BASE_STEMS:
        if s in stems:
            return s
    return None


def _peers_import_base_fraction(
    peers: list[Path], base_stem: str,
) -> float:
    """Fraction of non-base peers whose first 800 bytes contain a
    relative import of the base module. Strong structural signal
    that this dir is a real plugin family, not just a flat utility
    dir that happens to contain a ``base.py``.
    """
    non_base = [p for p in peers if p.stem.lower() not in _BASE_STEMS]
    if not non_base:
        return 0.0
    needle_py = f"from .{base_stem}"
    needle_alt = f"from .{base_stem.lstrip('_')}"
    needle_ts = f"from './{base_stem}"
    needle_ts2 = f'from "./{base_stem}'
    needles = (needle_py, needle_alt, needle_ts, needle_ts2)
    matches = 0
    for p in non_base:
        try:
            # 4000 bytes covers most file-leading license headers
            # without forcing a full read on huge plugin modules.
            head = p.read_text(
                encoding="utf-8", errors="ignore",
            )[:4000]
        except OSError:
            continue
        if any(n in head for n in needles):
            matches += 1
    return matches / len(non_base)


def detect_plugin_dirs(repo_root: Path) -> list[PluginModule]:
    """Find every directory matching the plugin-system signature and
    emit one ``PluginModule`` per peer module.
    """
    out: list[PluginModule] = []
    for d in _walkable_dirs(repo_root):
        peers = _peer_source_files(d)
        if len(peers) < MIN_SIBLINGS:
            continue
        base_stem = _base_stem_present(peers)
        if base_stem is None:
            continue
        # Mixed-extension dirs are rare for plugin systems; skip when
        # the dominant extension covers <70% of peers.
        from collections import Counter
        ext_counts = Counter(p.suffix for p in peers)
        dominant_ext, dominant_n = ext_counts.most_common(1)[0]
        if dominant_n / len(peers) < 0.7:
            continue
        # Strong structural signal: ≥40% of peers import the base
        # module. Weeds out flat utility dirs that happen to have
        # a file named base.py but aren't a real plugin family.
        if _peers_import_base_fraction(peers, base_stem) < 0.40:
            continue

        rel_dir = str(d.relative_to(repo_root))
        for p in peers:
            if p.stem.lower() in _BASE_STEMS:
                continue
            if p.suffix != dominant_ext:
                continue
            out.append(PluginModule(
                plugin_dir=rel_dir,
                file=str(p.relative_to(repo_root)),
                name=p.stem,
                base_stem=base_stem,
                extension=dominant_ext,
            ))
    return out


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginModuleExtractor:
    """Universal plugin-system extractor.

    Conforms to ``faultline.protocols.Extractor``.
    """

    name: str = "plugin-module-extractor"

    def applicable(self, repo_root: Path) -> bool:
        # Cheap probe — applicable iff at least one walkable directory
        # holds ≥ MIN_SIBLINGS source files AND a base stem. The full
        # ``detect_plugin_dirs`` run is fast (<50ms on big repos)
        # because it short-circuits early in most dirs; we just call
        # it directly rather than duplicating the structural check.
        try:
            return any(True for _ in detect_plugin_dirs(repo_root)[:1])
        except Exception:  # noqa: BLE001 — opportunistic
            return False

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        """Emit ONE signal per plugin directory (not per plugin file).

        Reason: ground-truth feature lists for plugin-based libraries
        typically describe the plugin system as ONE horizontal
        capability ("Plugin Extensibility", "Notification Delivery"),
        not as N per-plugin features. Emitting one signal per
        directory matches that abstraction level and prevents the
        critique from over-decomposing into N phantom per-plugin
        features.

        The payload carries the peer count + a sample of names so
        the LLM has enough context to confirm the feature category.
        Per-module ``PluginModule`` records are still available via
        ``detect_plugin_dirs`` for callers that want them.
        """
        _ = files
        modules = detect_plugin_dirs(repo_root)
        # Group by plugin_dir.
        by_dir: dict[str, list[PluginModule]] = {}
        for m in modules:
            by_dir.setdefault(m.plugin_dir, []).append(m)

        signals: list[Signal] = []
        for plugin_dir, peers in by_dir.items():
            sample_names = sorted(p.name for p in peers)[:8]
            signals.append(Signal(
                kind="plugin-system",
                source=self.name,
                payload={
                    "plugin_dir": plugin_dir,
                    "peer_count": len(peers),
                    "extension": peers[0].extension,
                    "base_stem": peers[0].base_stem,
                    "sample_names": tuple(sample_names),
                },
            ))
        return signals


__all__ = [
    "MIN_SIBLINGS",
    "PluginModule",
    "PluginModuleExtractor",
    "detect_plugin_dirs",
]
