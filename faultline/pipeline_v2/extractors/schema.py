"""SchemaDomainExtractor — domain models → anchors.

Per the ``schema-domain-extractor`` skill, persistent-storage schemas
encode the product's nouns. Each domain model becomes a low-to-mid
confidence anchor on its own — the *real* lift comes from Stage 2,
where a model name that ALSO appears as a route slug (``User`` ↔
``users/``) yields a high-confidence merged feature.

Supported source formats:

  - Prisma   : ``prisma/schema.prisma`` → ``model Foo { ... }``
  - Drizzle  : ``**/schema.ts`` → ``export const foo = pgTable(...)`` /
               ``mysqlTable`` / ``sqliteTable``
  - Rails    : ``db/schema.rb`` → ``create_table "foos"`` (also picks up
               ``app/models/*.rb``)
  - Django   : ``**/models.py`` → ``class Foo(models.Model):``

Mongoose / Ent / sqlc are documented in the skill but parked for a
later iteration — they require deeper parsing.

We never read README.md or any prose doc for grounding (CLAUDE.md
hard rule). Schema files are structured manifest data, which is the
canonical product-noun source.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._util import (
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── regex matchers ──────────────────────────────────────────────────────────

_PRISMA_MODEL = re.compile(r"^\s*model\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{", re.MULTILINE)
_PRISMA_ENUM = re.compile(r"^\s*enum\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{", re.MULTILINE)
_DRIZZLE_TABLE = re.compile(
    r"export\s+const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?:pgTable|mysqlTable|sqliteTable)\s*\(",
)
_RAILS_CREATE_TABLE = re.compile(r"create_table\s+[\"']([^\"']+)[\"']")
_DJANGO_MODEL_CLASS = re.compile(
    r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*[^)]*models\.Model[^)]*\)\s*:",
    re.MULTILINE,
)

# Filename markers for each source format.
_PRISMA_SUFFIX = "schema.prisma"
# Drizzle file conventions:
#   - single-file:    db/schema.ts, db/schema.js
#   - barrel file:    src/db/schema.ts
#   - split-by-table: any *.ts under a ``/schema/`` directory (the
#                     openstatus / cal.com / next-saas-starter pattern)
_DRIZZLE_HINTS = ("/schema.ts", "/schema.js", "/db/schema.ts", "/db/schema.js")
_DRIZZLE_DIR_SEGMENT = "/schema/"
_RAILS_SCHEMA = "db/schema.rb"
_DJANGO_MODELS = "models.py"


def _names_from_prisma(text: str) -> list[str]:
    names = []
    for m in _PRISMA_MODEL.finditer(text):
        names.append(m.group(1))
    for m in _PRISMA_ENUM.finditer(text):
        names.append(m.group(1))
    return names


def _names_from_drizzle(text: str) -> list[str]:
    return [m.group(1) for m in _DRIZZLE_TABLE.finditer(text)]


def _names_from_rails(text: str) -> list[str]:
    return [m.group(1) for m in _RAILS_CREATE_TABLE.finditer(text)]


def _names_from_django(text: str) -> list[str]:
    return [m.group(1) for m in _DJANGO_MODEL_CLASS.finditer(text)]


# ── extractor class ─────────────────────────────────────────────────────────


class SchemaDomainExtractor:
    """Domain models / tables → anchors. See module docstring."""

    name = "schema"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        repo_path = ctx.repo_path
        files = list(ctx.tracked_files)

        # source-file lists per format
        prisma_files: list[str] = []
        drizzle_files: list[str] = []
        rails_files: list[str] = []
        django_files: list[str] = []

        for raw in files:
            p = posix(raw)
            if p.endswith(_PRISMA_SUFFIX):
                prisma_files.append(p)
                continue
            if any(p.endswith(h) for h in _DRIZZLE_HINTS):
                drizzle_files.append(p)
                continue
            # Split-by-table Drizzle pattern: ``packages/db/src/schema/
            # monitors/monitor.ts``. Restricted to .ts/.js files to
            # avoid false positives on JSON schemas or markdown.
            if _DRIZZLE_DIR_SEGMENT in p and (p.endswith(".ts") or p.endswith(".js")):
                drizzle_files.append(p)
                continue
            if p.endswith(_RAILS_SCHEMA):
                rails_files.append(p)
                continue
            if p.endswith(_DJANGO_MODELS):
                django_files.append(p)
                continue

        buckets: dict[str, list[str]] = defaultdict(list)
        # Track names that came from the strong sources (Prisma model /
        # Rails table / Django model class) so we can boost confidence.
        strong: set[str] = set()

        for fp in prisma_files:
            text = read_text(repo_path / fp)
            if text is None:
                continue
            for name in _names_from_prisma(text):
                slug = slugify(name)
                if slug:
                    buckets[slug].append(fp)
                    strong.add(slug)

        for fp in drizzle_files:
            text = read_text(repo_path / fp)
            if text is None:
                continue
            for name in _names_from_drizzle(text):
                slug = slugify(name)
                if slug:
                    buckets[slug].append(fp)
                    strong.add(slug)

        for fp in rails_files:
            text = read_text(repo_path / fp)
            if text is None:
                continue
            for name in _names_from_rails(text):
                # ``create_table "users"`` — already plural lowercase usually.
                slug = slugify(name)
                if slug:
                    buckets[slug].append(fp)
                    strong.add(slug)

        for fp in django_files:
            text = read_text(repo_path / fp)
            if text is None:
                continue
            for name in _names_from_django(text):
                slug = slugify(name)
                if slug:
                    buckets[slug].append(fp)
                    strong.add(slug)

        out: list[AnchorCandidate] = []
        for slug, paths in buckets.items():
            unique_paths = tuple(sorted(set(paths)))
            # Schema signals are valuable but ambiguous (a model named
            # ``User`` doesn't yet prove there's a User *feature* — the
            # routes/controllers will confirm). Mid baseline confidence.
            base = 0.65 if slug in strong else 0.55
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=unique_paths,
                    source=self.name,
                    confidence_self=min(base + 0.03 * len(unique_paths), 0.9),
                    rationale=f"domain model {slug!r} "
                              f"declared in {len(unique_paths)} schema file(s)",
                ),
            )
        return out


__all__ = ["SchemaDomainExtractor"]
