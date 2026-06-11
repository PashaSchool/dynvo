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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── format tables (regexes + filename markers) ──────────────────────────────
#
# Loaded from ``stacks/schema-domains.yaml`` in the packaged data tree
# (authoring copy: ``eval/stacks/schema-domains.yaml``). Pattern strings
# live in YAML; the MULTILINE flags they historically compiled with stay
# here in code.


@dataclass(frozen=True)
class _Tables:
    """Compiled regexes + filename markers for every schema format."""

    prisma_model: re.Pattern[str]
    prisma_enum: re.Pattern[str]
    drizzle_table: re.Pattern[str]
    rails_create_table: re.Pattern[str]
    django_model_class: re.Pattern[str]
    prisma_suffixes: tuple[str, ...]
    drizzle_hints: tuple[str, ...]
    drizzle_dir_segment: str
    drizzle_dir_suffixes: tuple[str, ...]
    rails_suffixes: tuple[str, ...]
    django_suffixes: tuple[str, ...]


_TABLES_CACHE: _Tables | None = None


def _suffixes(block: dict, key: str = "file_suffixes") -> tuple[str, ...]:
    return tuple(s for s in (block.get(key) or []) if isinstance(s, str))


def _load_tables() -> _Tables:
    """Parse schema-domains.yaml once into the historical structures.

    Hermetic: resolves via ``importlib.resources`` (see
    ``faultline.pipeline_v2.data``).
    """
    global _TABLES_CACHE
    if _TABLES_CACHE is not None:
        return _TABLES_CACHE

    formats = load_stack_yaml("schema-domains").get("formats") or {}
    prisma = formats.get("prisma") or {}
    drizzle = formats.get("drizzle") or {}
    rails = formats.get("rails") or {}
    django = formats.get("django") or {}

    def _pat(block: dict, key: str) -> str:
        patterns = block.get("patterns") or {}
        raw = patterns.get(key)
        if not isinstance(raw, str) or not raw:
            raise ValueError(
                f"schema-domains.yaml missing pattern {key!r} — data bug"
            )
        return raw

    _TABLES_CACHE = _Tables(
        prisma_model=re.compile(_pat(prisma, "model"), re.MULTILINE),
        prisma_enum=re.compile(_pat(prisma, "enum"), re.MULTILINE),
        drizzle_table=re.compile(_pat(drizzle, "table")),
        rails_create_table=re.compile(_pat(rails, "create_table")),
        django_model_class=re.compile(
            _pat(django, "model_class"), re.MULTILINE,
        ),
        prisma_suffixes=_suffixes(prisma),
        drizzle_hints=_suffixes(drizzle),
        drizzle_dir_segment=str(drizzle.get("dir_segment") or "/schema/"),
        drizzle_dir_suffixes=_suffixes(drizzle, "dir_segment_suffixes"),
        rails_suffixes=_suffixes(rails),
        django_suffixes=_suffixes(django),
    )
    return _TABLES_CACHE


def _names_from_prisma(text: str, t: _Tables) -> list[str]:
    names = []
    for m in t.prisma_model.finditer(text):
        names.append(m.group(1))
    for m in t.prisma_enum.finditer(text):
        names.append(m.group(1))
    return names


def _names_from_drizzle(text: str, t: _Tables) -> list[str]:
    return [m.group(1) for m in t.drizzle_table.finditer(text)]


def _names_from_rails(text: str, t: _Tables) -> list[str]:
    return [m.group(1) for m in t.rails_create_table.finditer(text)]


def _names_from_django(text: str, t: _Tables) -> list[str]:
    return [m.group(1) for m in t.django_model_class.finditer(text)]


# ── extractor class ─────────────────────────────────────────────────────────


class SchemaDomainExtractor:
    """Domain models / tables → anchors. See module docstring."""

    name = "schema"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        repo_path = ctx.repo_path
        files = list(ctx.tracked_files)
        t = _load_tables()

        # source-file lists per format
        prisma_files: list[str] = []
        drizzle_files: list[str] = []
        rails_files: list[str] = []
        django_files: list[str] = []

        for raw in files:
            p = posix(raw)
            if any(p.endswith(s) for s in t.prisma_suffixes):
                prisma_files.append(p)
                continue
            if any(p.endswith(h) for h in t.drizzle_hints):
                drizzle_files.append(p)
                continue
            # Split-by-table Drizzle pattern: ``packages/db/src/schema/
            # monitors/monitor.ts``. Restricted to .ts/.js files to
            # avoid false positives on JSON schemas or markdown.
            if t.drizzle_dir_segment in p and any(
                p.endswith(s) for s in t.drizzle_dir_suffixes
            ):
                drizzle_files.append(p)
                continue
            if any(p.endswith(s) for s in t.rails_suffixes):
                rails_files.append(p)
                continue
            if any(p.endswith(s) for s in t.django_suffixes):
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
            for name in _names_from_prisma(text, t):
                slug = slugify(name)
                if slug:
                    buckets[slug].append(fp)
                    strong.add(slug)

        for fp in drizzle_files:
            text = read_text(repo_path / fp)
            if text is None:
                continue
            for name in _names_from_drizzle(text, t):
                slug = slugify(name)
                if slug:
                    buckets[slug].append(fp)
                    strong.add(slug)

        for fp in rails_files:
            text = read_text(repo_path / fp)
            if text is None:
                continue
            for name in _names_from_rails(text, t):
                # ``create_table "users"`` — already plural lowercase usually.
                slug = slugify(name)
                if slug:
                    buckets[slug].append(fp)
                    strong.add(slug)

        for fp in django_files:
            text = read_text(repo_path / fp)
            if text is None:
                continue
            for name in _names_from_django(text, t):
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
