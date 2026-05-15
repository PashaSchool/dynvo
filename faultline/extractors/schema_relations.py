"""Schema relations extractor (Sprint 4 / Phase 5 Layer C).

Builds a model-to-model relation graph from Prisma schemas and
Drizzle table definitions, then groups tightly-linked models into
**schema clusters**. Each cluster is emitted as one signal.

Why clusters and not per-model: ground-truth feature lists describe
product domains, not individual database tables. Papermark has 65
Prisma models for ~19 features — emitting 65 signals over-decomposes.
A cluster (e.g. ``{User, Account, Session, VerificationToken}``)
maps onto ONE feature ("Authentication") much more cleanly.

Algorithm:

  1. For each schema file, extract (from_model, to_model) edges:
     - Prisma: ``@relation`` directives + typed fields whose type is
       another model name in the same file.
     - Drizzle: ``references(() => Foo.id)`` callsites.
  2. Union-find on the edges to compute connected components.
  3. Emit one ``schema-cluster`` signal per component, listing the
     member models and a representative name (the "anchor" — model
     with the most outgoing relations).

Generic per ``memory/rule-no-repo-specific-paths`` — no per-repo
filenames hardcoded.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.signals import Signal


# When a connected component exceeds this many models it almost
# certainly indicates a mega-cluster where every model transitively
# links through a central User / Account hub. Such clusters carry
# no feature granularity ("everything is one feature") so we drop
# them. Tuned 2026-05-15 against inbox-zero (61-model cluster) and
# papermark (15-model cluster — kept).
MAX_CLUSTER_SIZE = 12


_SKIP_DIR_NAMES = frozenset({
    "node_modules", "__pycache__", ".git", "dist", "build",
    ".next", ".turbo", ".venv", "venv", "env",
    "tests", "test", "spec", "specs", "fixtures",
    "stories", "storybook-static", ".storybook", "e2e", "__tests__",
})


# ── Prisma parsers ────────────────────────────────────────────────────


_PRISMA_MODEL_RE = re.compile(
    r"^\s*model\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{",
    re.MULTILINE,
)

# Inside a Prisma model body, a relation field looks like:
#   author      User      @relation(fields: [authorId], references: [id])
#   posts       Post[]    @relation(...)
#   subscription Subscription?
# We capture the type token (User, Post, Subscription) — model
# references to other models declared in the same schema file.
_PRISMA_FIELD_RE = re.compile(
    r"""
    ^\s*
    [A-Za-z_][A-Za-z0-9_]*   # field name
    \s+
    ([A-Za-z_][A-Za-z0-9_]*)  # type — the candidate model reference
    (?:\[\])?                  # array marker
    \??                        # optional marker
    \s
    """,
    re.VERBOSE | re.MULTILINE,
)

# Tokens that look like model names but are Prisma scalars or
# directives — never count them as model edges.
_PRISMA_SCALARS = frozenset({
    "Int", "BigInt", "Float", "Decimal", "String", "Boolean",
    "DateTime", "Json", "Bytes", "Unsupported",
})


def _parse_prisma_models_and_edges(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (model names list, [(from, to)] edges) from one Prisma
    schema file body.
    """
    models: list[str] = []
    model_set: set[str] = set()
    # First pass: collect model names.
    for m in _PRISMA_MODEL_RE.finditer(text):
        name = m.group(1)
        if name not in model_set:
            model_set.add(name)
            models.append(name)

    # Second pass: walk each model body, collect type-references that
    # point to another known model.
    edges: list[tuple[str, str]] = []
    model_positions = list(_PRISMA_MODEL_RE.finditer(text))
    for i, mm in enumerate(model_positions):
        from_name = mm.group(1)
        body_start = mm.end()
        body_end = (
            model_positions[i + 1].start()
            if i + 1 < len(model_positions)
            else len(text)
        )
        body = text[body_start:body_end]
        for fm in _PRISMA_FIELD_RE.finditer(body):
            type_token = fm.group(1)
            if type_token in _PRISMA_SCALARS:
                continue
            if type_token not in model_set:
                continue
            if type_token == from_name:
                continue
            edges.append((from_name, type_token))
    return models, edges


# ── Drizzle parsers ──────────────────────────────────────────────────


# Drizzle table declarations: export const X = pgTable("name", {...})
# or                          export const X = mysqlTable(...)
# or                          export const X = sqliteTable(...)
_DRIZZLE_TABLE_RE = re.compile(
    r"""
    \bexport\s+const\s+
    (?P<var>[A-Za-z_$][A-Za-z0-9_$]*)
    \s*=\s*
    (?:pgTable|mysqlTable|sqliteTable)\s*\(
    \s*["']([^"']+)["']
    """,
    re.VERBOSE,
)

# references(() => SomeTable.id) — capture the referenced table var.
_DRIZZLE_REF_RE = re.compile(
    r"""\.\s*references\s*\(\s*\(\)\s*=>\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\.""",
    re.VERBOSE,
)


def _parse_drizzle_models_and_edges(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    models: list[str] = []
    model_set: set[str] = set()
    table_positions = list(_DRIZZLE_TABLE_RE.finditer(text))
    for tm in table_positions:
        var = tm.group("var")
        if var not in model_set:
            model_set.add(var)
            models.append(var)

    edges: list[tuple[str, str]] = []
    for i, tm in enumerate(table_positions):
        from_name = tm.group("var")
        body_start = tm.end()
        body_end = (
            table_positions[i + 1].start()
            if i + 1 < len(table_positions)
            else len(text)
        )
        body = text[body_start:body_end]
        for rm in _DRIZZLE_REF_RE.finditer(body):
            target = rm.group(1)
            if target in model_set and target != from_name:
                edges.append((from_name, target))
    return models, edges


# ── Union-find clustering ────────────────────────────────────────────


class _UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self._parent: dict[str, str] = {x: x for x in items}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # path compression
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def clusters(self) -> list[list[str]]:
        groups: dict[str, list[str]] = {}
        for x in self._parent:
            r = self.find(x)
            groups.setdefault(r, []).append(x)
        return list(groups.values())


def _cluster_models(
    models: list[str], edges: list[tuple[str, str]],
) -> list[list[str]]:
    uf = _UnionFind(models)
    for a, b in edges:
        uf.union(a, b)
    out = uf.clusters()
    # Sort each cluster by name for determinism; sort clusters by
    # size desc then by first member.
    for c in out:
        c.sort()
    out.sort(key=lambda c: (-len(c), c[0] if c else ""))
    return out


def _pick_anchor(cluster: list[str], edges: list[tuple[str, str]]) -> str:
    """Anchor = model with the most relations involving it. Tiebreak
    alphabetically. Used as the cluster's display name.
    """
    if len(cluster) == 1:
        return cluster[0]
    members = set(cluster)
    counts: dict[str, int] = {m: 0 for m in cluster}
    for a, b in edges:
        if a in members and b in members:
            counts[a] = counts.get(a, 0) + 1
            counts[b] = counts.get(b, 0) + 1
    return max(cluster, key=lambda m: (counts[m], -ord(m[0]) if m else 0))


# ── Discovery + extractor wrapper ─────────────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class SchemaCluster:
    file: str
    members: tuple[str, ...]
    anchor: str
    edge_count: int
    source_format: str  # "prisma" / "drizzle"


def _walkable_files(repo_root: Path) -> Iterable[Path]:
    for p in repo_root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIR_NAMES for part in p.parts):
            continue
        yield p


def _collect_schema_files(repo_root: Path) -> list[tuple[Path, str]]:
    """Return (file, source_format) tuples for known schema files."""
    out: list[tuple[Path, str]] = []
    for p in _walkable_files(repo_root):
        if p.name == "schema.prisma":
            out.append((p, "prisma"))
        elif p.suffix == ".ts" and "schema" in p.stem.lower():
            # Heuristic: a Drizzle schema file ends with "schema.ts"
            # or contains "schema" in its stem and we'll inspect the
            # body for pgTable/mysqlTable/sqliteTable to confirm.
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if any(t in text for t in ("pgTable(", "mysqlTable(", "sqliteTable(")):
                out.append((p, "drizzle"))
    return out


def collect_schema_clusters(repo_root: Path) -> list[SchemaCluster]:
    """Scan the repo, parse every schema file, return clustered model
    groups. One cluster per connected component of model relations.
    Models with NO relations form singleton clusters.
    """
    out: list[SchemaCluster] = []
    for path, fmt in _collect_schema_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if fmt == "prisma":
            models, edges = _parse_prisma_models_and_edges(text)
        elif fmt == "drizzle":
            models, edges = _parse_drizzle_models_and_edges(text)
        else:
            continue
        if not models:
            continue
        rel = str(path.relative_to(repo_root))
        for cluster in _cluster_models(models, edges):
            if len(cluster) > MAX_CLUSTER_SIZE:
                # Mega-cluster — every model transitively links
                # through a central hub. No feature granularity,
                # skip emission rather than report a single "User"
                # cluster that spans the whole product.
                continue
            anchor = _pick_anchor(cluster, edges)
            n_internal_edges = sum(
                1 for a, b in edges
                if a in cluster and b in cluster
            )
            out.append(SchemaCluster(
                file=rel,
                members=tuple(cluster),
                anchor=anchor,
                edge_count=n_internal_edges,
                source_format=fmt,
            ))
    return out


@dataclass(frozen=True, slots=True, kw_only=True)
class SchemaRelationsExtractor:
    """Universal Prisma + Drizzle schema-relation cluster extractor."""

    name: str = "schema-relations-extractor"

    def applicable(self, repo_root: Path) -> bool:
        return bool(_collect_schema_files(repo_root))

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files
        return [
            Signal(
                kind="schema-cluster",
                source=self.name,
                payload={
                    "file": c.file,
                    "anchor": c.anchor,
                    "members": c.members,
                    "size": len(c.members),
                    "edge_count": c.edge_count,
                    "source_format": c.source_format,
                },
            )
            for c in collect_schema_clusters(repo_root)
        ]


__all__ = [
    "SchemaCluster",
    "SchemaRelationsExtractor",
    "collect_schema_clusters",
]
