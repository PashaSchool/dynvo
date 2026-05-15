"""Tests for the Sprint 4 schema relations extractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.extractors.schema_relations import (
    SchemaRelationsExtractor,
    _cluster_models,
    _parse_drizzle_models_and_edges,
    _parse_prisma_models_and_edges,
    _pick_anchor,
    collect_schema_clusters,
)
from faultline.protocols import Extractor


def _w(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


# ── prisma parsing ────────────────────────────────────────────────────


_PRISMA_AUTH = """
generator client { provider = "prisma-client-js" }

model User {
  id        Int       @id @default(autoincrement())
  email     String    @unique
  sessions  Session[]
  accounts  Account[]
}

model Session {
  id        Int       @id @default(autoincrement())
  user      User      @relation(fields: [userId], references: [id])
  userId    Int
  expires   DateTime
}

model Account {
  id        Int       @id @default(autoincrement())
  user      User      @relation(fields: [userId], references: [id])
  userId    Int
  provider  String
}

model Post {
  id        Int       @id @default(autoincrement())
  title     String
  body      String
  published Boolean
}
"""


def test_prisma_parse_models_and_edges():
    models, edges = _parse_prisma_models_and_edges(_PRISMA_AUTH)
    assert set(models) == {"User", "Session", "Account", "Post"}
    # Session and Account both reference User; User references both back.
    assert ("Session", "User") in edges
    assert ("Account", "User") in edges
    assert ("User", "Session") in edges
    assert ("User", "Account") in edges


def test_prisma_skips_scalar_types_as_edges():
    models, edges = _parse_prisma_models_and_edges(_PRISMA_AUTH)
    # No edges to/from String, Int, DateTime, Boolean even though
    # those tokens follow field names.
    flat = {n for pair in edges for n in pair}
    assert "String" not in flat
    assert "DateTime" not in flat


# ── drizzle parsing ──────────────────────────────────────────────────


_DRIZZLE_BLOG = """
import { pgTable, integer, text } from "drizzle-orm/pg-core";

export const users = pgTable("users", {
  id: integer("id").primaryKey(),
  email: text("email").notNull(),
});

export const posts = pgTable("posts", {
  id: integer("id").primaryKey(),
  authorId: integer("author_id").references(() => users.id),
  title: text("title"),
});

export const comments = pgTable("comments", {
  id: integer("id").primaryKey(),
  postId: integer("post_id").references(() => posts.id),
  body: text("body"),
});
"""


def test_drizzle_parse_models_and_edges():
    models, edges = _parse_drizzle_models_and_edges(_DRIZZLE_BLOG)
    assert set(models) == {"users", "posts", "comments"}
    assert ("posts", "users") in edges
    assert ("comments", "posts") in edges


# ── union-find clustering ────────────────────────────────────────────


def test_clustering_groups_connected_models():
    models = ["User", "Session", "Account", "Post", "Comment"]
    edges = [
        ("Session", "User"), ("Account", "User"),  # auth cluster
        ("Comment", "Post"),                       # blog cluster
    ]
    clusters = _cluster_models(models, edges)
    cluster_sets = [set(c) for c in clusters]
    assert {"User", "Session", "Account"} in cluster_sets
    assert {"Post", "Comment"} in cluster_sets


def test_clustering_singletons_for_unrelated():
    models = ["A", "B", "C"]
    edges: list[tuple[str, str]] = []
    clusters = _cluster_models(models, edges)
    assert sorted(c[0] for c in clusters) == ["A", "B", "C"]
    assert all(len(c) == 1 for c in clusters)


def test_clustering_handles_chained_relations():
    models = ["A", "B", "C", "D"]
    edges = [("A", "B"), ("B", "C"), ("C", "D")]
    clusters = _cluster_models(models, edges)
    assert len(clusters) == 1
    assert set(clusters[0]) == {"A", "B", "C", "D"}


def test_pick_anchor_selects_most_connected():
    cluster = ["User", "Session", "Account"]
    edges = [
        ("Session", "User"), ("Account", "User"),
        ("User", "Session"), ("User", "Account"),
    ]
    assert _pick_anchor(cluster, edges) == "User"


def test_pick_anchor_singleton_returns_self():
    assert _pick_anchor(["LonelyModel"], []) == "LonelyModel"


# ── full collector with file walk ─────────────────────────────────────


def test_collect_clusters_on_prisma_file(tmp_path):
    _w(tmp_path, "prisma/schema.prisma", _PRISMA_AUTH)
    clusters = collect_schema_clusters(tmp_path)
    # 1 connected component (User+Session+Account) + 1 singleton (Post)
    assert len(clusters) == 2
    by_anchor = {c.anchor: c for c in clusters}
    assert "User" in by_anchor
    user_cluster = by_anchor["User"]
    assert set(user_cluster.members) == {"User", "Session", "Account"}
    assert user_cluster.source_format == "prisma"
    assert user_cluster.edge_count == 4  # 2 → User, User → 2


def test_collect_clusters_on_drizzle_file(tmp_path):
    _w(tmp_path, "src/db/schema.ts", _DRIZZLE_BLOG)
    clusters = collect_schema_clusters(tmp_path)
    # All 3 are connected via posts→users and comments→posts.
    assert len(clusters) == 1
    assert set(clusters[0].members) == {"users", "posts", "comments"}
    assert clusters[0].source_format == "drizzle"


def test_skips_test_dirs_and_node_modules(tmp_path):
    _w(tmp_path, "tests/prisma/schema.prisma", "model X {}")
    _w(tmp_path, "node_modules/lib/prisma/schema.prisma", "model Y {}")
    assert collect_schema_clusters(tmp_path) == []


def test_drizzle_only_qualifies_when_table_helpers_present(tmp_path):
    """A `.ts` file with `schema` in name is only considered Drizzle
    when the body actually has pgTable/mysqlTable/sqliteTable.
    """
    _w(tmp_path, "src/schemas.ts", '''
export const userSchema = z.object({ name: z.string() });
''')
    assert collect_schema_clusters(tmp_path) == []


# ── extractor wrapper ────────────────────────────────────────────────


def test_extractor_conforms_to_protocol():
    assert isinstance(SchemaRelationsExtractor(), Extractor)


def test_extractor_emits_schema_cluster_signals(tmp_path):
    _w(tmp_path, "prisma/schema.prisma", _PRISMA_AUTH)
    sigs = SchemaRelationsExtractor().extract(tmp_path, files=())
    assert all(s.kind == "schema-cluster" for s in sigs)
    assert all(s.source == "schema-relations-extractor" for s in sigs)
    assert {s.payload["anchor"] for s in sigs} == {"User", "Post"}


def test_extractor_applicable_false_without_schema_files(tmp_path):
    _w(tmp_path, "src/main.py", "print('hi')")
    assert SchemaRelationsExtractor().applicable(tmp_path) is False


def test_mega_clusters_above_size_cap_are_dropped(tmp_path):
    """Schemas where every model transitively links to a central
    hub produce mega-clusters that carry no feature granularity.
    Drop them before emission.
    """
    body_lines = ["model Hub { id Int @id }"]
    # Create 20 models all FK-pointing at Hub. With a hub-and-spoke
    # shape the connected component is 21 (Hub + 20).
    for i in range(20):
        body_lines.append(f"""
model M{i:02d} {{
  id Int @id
  hub Hub @relation(fields: [hubId], references: [id])
  hubId Int
}}
""")
    _w(tmp_path, "prisma/schema.prisma", "\n".join(body_lines))
    clusters = collect_schema_clusters(tmp_path)
    # Mega-cluster (size 21) silently dropped; no other clusters.
    assert clusters == []


def test_below_size_cap_clusters_still_emit(tmp_path):
    """Sanity: a cluster of size 5 (under the cap) still emits."""
    body_lines = ["model Root { id Int @id }"]
    for i in range(4):
        body_lines.append(f"""
model M{i} {{
  id Int @id
  root Root @relation(fields: [rootId], references: [id])
  rootId Int
}}
""")
    _w(tmp_path, "prisma/schema.prisma", "\n".join(body_lines))
    clusters = collect_schema_clusters(tmp_path)
    assert len(clusters) == 1
    assert len(clusters[0].members) == 5


def test_extractor_applicable_true_with_prisma(tmp_path):
    _w(tmp_path, "prisma/schema.prisma", "model X {}")
    assert SchemaRelationsExtractor().applicable(tmp_path) is True
