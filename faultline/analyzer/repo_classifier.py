"""Classifies repository structure type to optimize feature detection strategy.

Detects whether a repo is organized by business features (e.g. src/auth/, src/payments/)
or by technical layers (e.g. src/server/, src/client/, src/lib/) so LLM prompts
can be adapted for better feature grouping.
"""

from pathlib import PurePosixPath


# Technical layer directory names — when >50% of top-level dirs match,
# the repo is likely layer-organized rather than feature-organized.
LAYER_KEYWORDS = frozenset({
    # Architecture layers
    "server", "client", "backend", "frontend", "web", "mobile",
    # Code organization
    "lib", "libs", "utils", "utilities", "helpers", "common", "shared",
    "core", "internal", "runtime", "vendor", "bundled", "compiled",
    # Technical concerns
    "types", "typings", "models", "schemas", "interfaces",
    "config", "configs", "configuration", "constants",
    "middleware", "interceptors", "guards", "decorators",
    "providers", "adapters", "drivers", "connectors",
    # Build / tooling
    "scripts", "tools", "tooling", "build", "dist", "out",
    "generated", "codegen", "proto", "protos",
    # Testing
    "test", "tests", "__tests__", "testing", "testutils",
    "fixtures", "mocks", "__mocks__",
    # Assets
    "assets", "static", "public", "media", "images", "fonts", "styles",
    # Docs
    "docs", "documentation", "examples", "samples",
})

# Monorepo markers — presence of these directories signals a monorepo.
MONOREPO_MARKERS = frozenset({
    "packages", "apps", "modules", "services", "workspaces", "projects",
})


class RepoStructure:
    """Result of repo structure classification."""

    def __init__(
        self,
        layout: str,
        top_dirs: list[str],
        layer_ratio: float,
        monorepo_root: str | None = None,
    ):
        self.layout = layout              # "feature" | "layer" | "monorepo"
        self.top_dirs = top_dirs          # top-level meaningful dirs
        self.layer_ratio = layer_ratio    # fraction of dirs matching LAYER_KEYWORDS
        self.monorepo_root = monorepo_root  # e.g. "packages" if monorepo

    def __repr__(self) -> str:
        return f"RepoStructure(layout={self.layout!r}, layer_ratio={self.layer_ratio:.0%}, dirs={len(self.top_dirs)})"


def classify_repo(files: list[str]) -> RepoStructure:
    """Classify repository structure from its file list.

    Args:
        files: List of file paths relative to analysis root (path_prefix already stripped).

    Returns:
        RepoStructure with layout type and metadata.
    """
    top_dirs = _extract_top_dirs(files)

    if not top_dirs:
        return RepoStructure(layout="feature", top_dirs=[], layer_ratio=0.0)

    # Check for monorepo markers first
    monorepo_root = _detect_monorepo(top_dirs)
    if monorepo_root:
        return RepoStructure(
            layout="monorepo",
            top_dirs=top_dirs,
            layer_ratio=0.0,
            monorepo_root=monorepo_root,
        )

    # Calculate layer ratio
    layer_dirs = [d for d in top_dirs if d.lower() in LAYER_KEYWORDS]
    layer_ratio = len(layer_dirs) / len(top_dirs) if top_dirs else 0.0

    layout = "layer" if layer_ratio > 0.50 else "feature"

    return RepoStructure(
        layout=layout,
        top_dirs=top_dirs,
        layer_ratio=layer_ratio,
    )


def _extract_top_dirs(files: list[str]) -> list[str]:
    """Get unique first-level directory names from file paths."""
    dirs: set[str] = set()
    for f in files:
        parts = PurePosixPath(f).parts
        if len(parts) > 1:
            dirs.add(parts[0])
    return sorted(dirs)


def _detect_monorepo(top_dirs: list[str]) -> str | None:
    """Check if any top-level dir is a monorepo marker."""
    for d in top_dirs:
        if d.lower() in MONOREPO_MARKERS:
            return d
    return None


def build_layer_context(structure: RepoStructure) -> str:
    """Build extra LLM prompt context for layer-organized repos.

    Returns a string to be appended to the LLM system prompt that instructs
    it to look for cross-cutting business features across technical layers.
    """
    if structure.layout == "feature":
        return ""

    if structure.layout == "monorepo":
        return (
            "\n\n## REPOSITORY STRUCTURE: MONOREPO\n"
            f"This repository is a monorepo with packages under `{structure.monorepo_root}/`. "
            "Each package may contain its own feature set. "
            "Group by business feature ACROSS packages when the same domain spans multiple packages. "
            "A package that is a standalone product should be treated as a single feature or "
            "split into sub-features based on its internal structure."
        )

    # layout == "layer"
    return (
        "\n\n## REPOSITORY STRUCTURE: TECHNICAL LAYERS\n"
        "This codebase is organized by TECHNICAL LAYERS (server/, client/, lib/, shared/, etc.) "
        "rather than by business features. The top-level directories represent architectural "
        "boundaries, NOT business domains.\n\n"
        "To find the real business features, you MUST:\n"
        "1. Look DEEPER — at 2nd and 3rd level subdirectories across all layers.\n"
        "   Example: server/router/ + client/router/ + shared/router/ = one feature \"routing\".\n"
        "2. Cross-cut across layers — files in server/auth/, client/auth/, shared/auth/ "
        "all belong to the same \"auth\" feature.\n"
        "3. Use subdirectory names as feature signals, not top-level directory names.\n"
        "4. Look at sample filenames for domain hints (e.g. image-optimizer.ts → \"image-optimization\").\n\n"
        "WRONG: \"server-runtime\" (technical layer as feature)\n"
        "RIGHT: \"app-router\", \"image-optimization\", \"middleware\", \"dev-overlay\" (business capabilities)"
    )
