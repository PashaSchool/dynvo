"""Schema-domain extractor — universal across ORMs.

Per the schema-domain-extractor skill (faultlines-app repo,
``.claude/skills/schema-domain-extractor/SKILL.md``). Reads database
schema files for every supported ORM and emits one signal per
domain model with its name + a feature_hint guess from a heuristic
table.

Phase 3c PoC supports Prisma + Drizzle + Rails db/schema.rb +
Django models.py. Mongoose / Ent / sqlc / Laravel migrations follow
the same pattern (one parser strategy each).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from faultline.signals import Signal


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainModel:
    """One detected domain model."""

    name: str
    source_format: str          # "prisma" | "drizzle" | "rails-schema" | "django-model"
    file: str                   # repo-relative
    feature_hint: str | None    # heuristic guess (see _FEATURE_HINTS)


# Heuristic name → feature_hint table. Order matters — first match wins.
# Pattern is checked as a substring (case-insensitive) on the model name.
_FEATURE_HINTS: list[tuple[str, str]] = [
    # Auth + Account
    ("session", "Authentication"),
    ("verificationtoken", "Authentication"),
    ("oauthaccount", "Authentication"),
    ("mfacredential", "Authentication"),
    ("twofactor", "Authentication"),
    ("apikey", "API Access"),
    ("oauthclient", "API Access"),
    ("token", "Authentication"),
    ("user", "Users / Accounts"),
    ("account", "Users / Accounts"),
    ("profile", "Users / Accounts"),
    ("member", "Users / Accounts"),
    # Billing
    ("subscription", "Billing"),
    ("invoice", "Billing"),
    ("payment", "Billing"),
    ("stripecustomer", "Billing"),
    ("plan", "Billing"),
    ("coupon", "Billing"),
    ("creditbalance", "Billing"),
    ("usage", "Billing"),
    # Teams / Orgs
    ("organization", "Teams / Organizations"),
    ("team", "Teams / Organizations"),
    ("workspace", "Teams / Organizations"),
    ("membership", "Teams / Organizations"),
    ("invitation", "Invitations"),
    ("invite", "Invitations"),
    # Notifications + Webhooks
    ("notification", "Notifications"),
    ("emaillog", "Email"),
    ("webhookevent", "Webhooks"),
    ("webhook", "Webhooks"),
    # Audit
    ("auditlog", "Audit / Activity"),
    ("activity", "Audit / Activity"),
    ("event", "Audit / Activity"),
    # Storage + Files
    ("upload", "File Storage"),
    ("attachment", "File Storage"),
    ("file", "File Storage"),
    ("media", "File Storage"),
    # Tagging
    ("tag", "Tags / Categories"),
    ("label", "Tags / Categories"),
    ("category", "Tags / Categories"),
]

_PLUMBING_NAMES = frozenset({
    "permission", "rolepermission", "jointable", "_prismamigrations",
    "_drizzle_migrations", "ar_internal_metadata", "schema_migrations",
})


def _guess_feature_hint(name: str) -> str | None:
    n = name.lower().replace("_", "")
    if n in _PLUMBING_NAMES:
        return None
    for needle, hint in _FEATURE_HINTS:
        if needle in n:
            return hint
    return None


# ── Format parsers ───────────────────────────────────────────────────


_PRISMA_MODEL_RE = re.compile(r"^model\s+([A-Z][A-Za-z0-9_]*)\s*\{", re.MULTILINE)
_RAILS_TABLE_RE = re.compile(
    r'create_table\s+["\']([a-z_][a-z0-9_]*)["\']', re.MULTILINE,
)
_DJANGO_MODEL_RE = re.compile(
    r"^class\s+([A-Z][A-Za-z0-9_]*)\s*\(\s*models\.Model\s*\)", re.MULTILINE,
)
_DRIZZLE_TABLE_RE = re.compile(
    r"export\s+const\s+([a-zA-Z_][\w]*)\s*=\s*"
    r"(?:pgTable|sqliteTable|mysqlTable)\s*\(",
)


def parse_prisma(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _PRISMA_MODEL_RE.findall(text)


def parse_rails_schema(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Rails table names are usually plural snake_case; Title-Case the
    # singular form for the feature_hint heuristic to work.
    raw = _RAILS_TABLE_RE.findall(text)
    return [_singularise(t) for t in raw]


def parse_django_model(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _DJANGO_MODEL_RE.findall(text)


def parse_drizzle(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _DRIZZLE_TABLE_RE.findall(text)


def _singularise(plural: str) -> str:
    """Naive English singulariser sufficient for table → model name.

    Rules: trailing "ies" → "y", trailing "ses"/"xes"/"zes" → drop "es",
    trailing "s" → drop. Capitalise first letter.
    """
    s = plural
    if s.endswith("ies"):
        s = s[:-3] + "y"
    elif s.endswith(("ses", "xes", "zes")):
        s = s[:-2]
    elif s.endswith("s"):
        s = s[:-1]
    return s.replace("_", " ").title().replace(" ", "")


# ── Format dispatch ──────────────────────────────────────────────────


def is_prisma_schema(p: Path) -> bool:
    return p.suffix == ".prisma" or p.name == "schema.prisma"


def is_rails_schema(p: Path) -> bool:
    return p.name == "schema.rb" and "db" in p.parts


def is_django_models(p: Path) -> bool:
    return p.name == "models.py"


def is_drizzle_schema(p: Path) -> bool:
    """Drizzle schemas typically live under db/ or schema/ with .ts/.js
    extension. Heuristic: filename starts with `schema` and is .ts/.js.
    """
    return (
        p.suffix in {".ts", ".js"}
        and p.stem.startswith("schema")
        and any(seg in {"db", "drizzle", "schema"} for seg in p.parts)
    )


def collect_domain_models(repo_root: Path) -> list[DomainModel]:
    """Walk repo and parse every supported schema file."""
    out: list[DomainModel] = []
    seen: set[tuple[str, str]] = set()    # (name, file) dedup

    skip_dirs = {"node_modules", "target", "dist", "build", "vendor",
                 "venv", ".venv", ".next", ".turbo", "__pycache__"}
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(repo_root).parts
        if any(p in skip_dirs for p in rel_parts):
            continue

        names: list[str] = []
        fmt: str | None = None
        if is_prisma_schema(path):
            names = parse_prisma(path); fmt = "prisma"
        elif is_rails_schema(path):
            names = parse_rails_schema(path); fmt = "rails-schema"
        elif is_django_models(path):
            names = parse_django_model(path); fmt = "django-model"
        elif is_drizzle_schema(path):
            names = parse_drizzle(path); fmt = "drizzle"
        if not names or fmt is None:
            continue

        rel = str(path.relative_to(repo_root)).replace("\\", "/")
        for name in names:
            key = (name, rel)
            if key in seen:
                continue
            seen.add(key)
            hint = _guess_feature_hint(name)
            out.append(DomainModel(
                name=name, source_format=fmt, file=rel, feature_hint=hint,
            ))
    return out


# ── Extractor wrapper ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class SchemaDomainExtractor:
    """Universal schema → domain-model signals."""

    name: str = "schema-domain-extractor"

    def applicable(self, repo_root: Path) -> bool:
        # Cheap probe: any supported schema file anywhere?
        for fname in ("schema.prisma",):
            if any(repo_root.rglob(fname)):
                return True
        if (repo_root / "db" / "schema.rb").exists():
            return True
        if any(repo_root.rglob("models.py")):
            return True
        return False

    def extract(
        self, repo_root: Path, files: Iterable[Path],
    ) -> list[Signal]:
        _ = files
        models = collect_domain_models(repo_root)
        return [
            Signal(
                kind="domain-model",
                source=self.name,
                payload={
                    "name": m.name,
                    "source_format": m.source_format,
                    "file": m.file,
                    "feature_hint": m.feature_hint,
                },
            )
            for m in models
        ]


__all__ = [
    "DomainModel",
    "SchemaDomainExtractor",
    "collect_domain_models",
    "parse_prisma",
    "parse_rails_schema",
    "parse_django_model",
    "parse_drizzle",
]
