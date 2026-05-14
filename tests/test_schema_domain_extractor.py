"""Tests for the universal schema-domain extractor (Phase 3c)."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.extractors.schema_domain import (
    SchemaDomainExtractor,
    collect_domain_models,
    parse_django_model,
    parse_drizzle,
    parse_prisma,
    parse_rails_schema,
)
from faultline.protocols import Extractor


# ── Format parsers ──────────────────────────────────────────────────


def test_parse_prisma_finds_models(tmp_path):
    schema = tmp_path / "schema.prisma"
    schema.write_text(
        "generator client { provider = \"prisma-client-js\" }\n\n"
        "model User {\n  id String @id\n  email String @unique\n}\n\n"
        "model Subscription {\n  id String @id\n}\n"
    )
    names = parse_prisma(schema)
    assert "User" in names and "Subscription" in names


def test_parse_rails_schema_finds_tables_and_singularises(tmp_path):
    schema = tmp_path / "schema.rb"
    schema.write_text(
        'create_table "users" do |t|\nend\n'
        'create_table "subscriptions" do |t|\nend\n'
        'create_table "categories" do |t|\nend\n'
    )
    names = parse_rails_schema(schema)
    assert "User" in names
    assert "Subscription" in names
    assert "Category" in names


def test_parse_django_model_finds_classes(tmp_path):
    f = tmp_path / "models.py"
    f.write_text(
        "from django.db import models\n\n"
        "class User(models.Model):\n    email = models.CharField()\n\n"
        "class Subscription(models.Model):\n    pass\n"
    )
    names = parse_django_model(f)
    assert "User" in names and "Subscription" in names


def test_parse_drizzle_finds_pgtable_calls(tmp_path):
    f = tmp_path / "schema.ts"
    f.write_text(
        "import { pgTable, text } from 'drizzle-orm/pg-core';\n"
        "export const users = pgTable('users', {\n  id: text('id'),\n});\n"
        "export const sessions = sqliteTable('sessions', {});\n"
    )
    names = parse_drizzle(f)
    assert "users" in names and "sessions" in names


# ── Walker ──────────────────────────────────────────────────────────


def test_collect_skips_node_modules(tmp_path):
    nm = tmp_path / "node_modules" / "lib"
    nm.mkdir(parents=True)
    (nm / "schema.prisma").write_text("model Buried { id String @id }\n")
    models = collect_domain_models(tmp_path)
    assert all("node_modules" not in m.file for m in models)


def test_collect_returns_empty_for_repo_without_schemas(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert collect_domain_models(tmp_path) == []


# ── Feature hints ───────────────────────────────────────────────────


def test_feature_hints_user_model_maps_to_users(tmp_path):
    (tmp_path / "schema.prisma").write_text("model User { id String @id }")
    models = collect_domain_models(tmp_path)
    assert models[0].feature_hint == "Users / Accounts"


def test_feature_hints_subscription_maps_to_billing(tmp_path):
    (tmp_path / "schema.prisma").write_text("model Subscription { id String @id }")
    models = collect_domain_models(tmp_path)
    assert models[0].feature_hint == "Billing"


def test_feature_hints_mfa_credential_maps_to_authentication(tmp_path):
    (tmp_path / "schema.prisma").write_text(
        "model MfaCredential { id String @id }"
    )
    models = collect_domain_models(tmp_path)
    assert models[0].feature_hint == "Authentication"


def test_feature_hints_audit_log_maps_to_audit(tmp_path):
    (tmp_path / "schema.prisma").write_text(
        "model AuditLog { id String @id }"
    )
    models = collect_domain_models(tmp_path)
    assert models[0].feature_hint == "Audit / Activity"


def test_plumbing_table_returns_no_hint(tmp_path):
    (tmp_path / "schema.prisma").write_text(
        "model Permission { id String @id }"
    )
    models = collect_domain_models(tmp_path)
    assert models[0].feature_hint is None


# ── Extractor wrapper ───────────────────────────────────────────────


def test_extractor_satisfies_protocol():
    e = SchemaDomainExtractor()
    assert isinstance(e, Extractor)


def test_extractor_emits_signals(tmp_path):
    (tmp_path / "schema.prisma").write_text(
        "model User { id String @id }\nmodel Subscription { id String @id }"
    )
    e = SchemaDomainExtractor()
    signals = e.extract(tmp_path, files=[])
    assert len(signals) == 2
    assert all(s.kind == "domain-model" for s in signals)
    sub = next(s for s in signals if s.payload["name"] == "Subscription")
    assert sub.payload["feature_hint"] == "Billing"
    assert sub.payload["source_format"] == "prisma"


def test_extractor_applicable_negative(tmp_path):
    """Empty repo → not applicable (no schemas anywhere)."""
    (tmp_path / "package.json").write_text("{}")
    e = SchemaDomainExtractor()
    assert not e.applicable(tmp_path)


def test_extractor_applicable_positive(tmp_path):
    (tmp_path / "schema.prisma").write_text("model X { id String @id }")
    e = SchemaDomainExtractor()
    assert e.applicable(tmp_path)
