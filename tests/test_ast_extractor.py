"""Tests for the regex-based TypeScript/JavaScript signature extractor."""
import textwrap
from pathlib import Path
import pytest

from faultline.analyzer.ast_extractor import (
    extract_signatures,
    FileSignature,
    _parse_file,
)


# ---------------------------------------------------------------------------
# Unit tests for _parse_file (no filesystem needed)
# ---------------------------------------------------------------------------

def test_extracts_named_function_export():
    source = "export function LoginForm() { return null; }"
    sig = _parse_file("components/LoginForm.tsx", source)
    assert "LoginForm" in sig.exports


def test_extracts_async_function_export():
    source = "export async function fetchUser(id: string) {}"
    sig = _parse_file("api/users.ts", source)
    assert "fetchUser" in sig.exports


def test_extracts_const_export():
    source = "export const useAuth = () => { return {}; };"
    sig = _parse_file("hooks/useAuth.ts", source)
    assert "useAuth" in sig.exports


def test_extracts_class_export():
    source = "export class AuthService { login() {} }"
    sig = _parse_file("services/auth.ts", source)
    assert "AuthService" in sig.exports


def test_extracts_default_function_export():
    source = "export default function CheckoutPage() { return null; }"
    sig = _parse_file("pages/checkout.tsx", source)
    assert "CheckoutPage" in sig.exports


def test_extracts_reexport_block():
    source = "export { LoginForm, useAuth, AuthService as Auth };"
    sig = _parse_file("index.ts", source)
    assert "LoginForm" in sig.exports
    assert "useAuth" in sig.exports
    assert "Auth" in sig.exports        # re-export alias


def test_extracts_nextjs_app_router_methods():
    source = textwrap.dedent("""\
        export async function GET(request: Request) {}
        export async function POST(request: Request) {}
    """)
    sig = _parse_file("app/api/auth/login/route.ts", source)
    assert any("GET" in r for r in sig.routes)
    assert any("POST" in r for r in sig.routes)


def test_extracts_nextjs_page_data_fetchers():
    source = textwrap.dedent("""\
        export async function getServerSideProps(context) { return { props: {} }; }
    """)
    sig = _parse_file("pages/dashboard.tsx", source)
    assert "getServerSideProps" in sig.routes


def test_extracts_express_routes():
    source = textwrap.dedent("""\
        router.get('/users', getUsers);
        router.post('/users', createUser);
        app.delete('/users/:id', deleteUser);
    """)
    sig = _parse_file("routes/users.ts", source)
    route_strs = " ".join(sig.routes)
    assert "GET" in route_strs and "/users" in route_strs
    assert "POST" in route_strs
    assert "DELETE" in route_strs


def test_extracts_relative_imports():
    source = textwrap.dedent("""\
        import { useAuth } from './useAuth';
        import { api } from '@/lib/api';
        import React from 'react';
    """)
    sig = _parse_file("components/Login.tsx", source)
    assert "./useAuth" in sig.imports
    assert "@/lib/api" in sig.imports
    assert "react" not in sig.imports       # node_modules excluded


def test_collects_scoped_workspace_imports():
    """Scoped specifiers ('@scope/pkg[/sub]') are retained so the
    downstream workspace resolver can map them to real files. Unscoped
    bare specifiers ('next/server') stay excluded."""
    source = textwrap.dedent("""\
        import { prisma } from '@calcom/prisma';
        import logger from '@calcom/lib/logger';
        import { Svc } from '@calcom/features/webhooks/lib/Svc';
        import { NextResponse } from 'next/server';
        import React from 'react';
    """)
    sig = _parse_file("apps/web/route.ts", source)
    assert "@calcom/prisma" in sig.imports
    assert "@calcom/lib/logger" in sig.imports
    assert "@calcom/features/webhooks/lib/Svc" in sig.imports
    assert "next/server" not in sig.imports   # unscoped bare → excluded
    assert "react" not in sig.imports


def test_python_file_returns_empty_signature():
    sig = _parse_file("services/auth.py", "def login(): pass")
    assert sig.is_empty()


def test_non_supported_file_skipped_by_extract_signatures(tmp_path):
    rs_file = tmp_path / "auth.rs"
    rs_file.write_text("fn login() {}")
    result = extract_signatures(["auth.rs"], str(tmp_path))
    assert "auth.rs" not in result


def test_python_file_included_by_extract_signatures(tmp_path):
    py_file = tmp_path / "auth.py"
    py_file.write_text("def login(): pass")
    result = extract_signatures(["auth.py"], str(tmp_path))
    assert "auth.py" in result
    assert "login" in result["auth.py"].exports


def test_extract_signatures_reads_real_file(tmp_path):
    ts_file = tmp_path / "LoginForm.tsx"
    ts_file.write_text("export function LoginForm() { return null; }")
    result = extract_signatures(["LoginForm.tsx"], str(tmp_path))
    assert "LoginForm.tsx" in result
    assert "LoginForm" in result["LoginForm.tsx"].exports


def test_missing_file_is_silently_skipped(tmp_path):
    result = extract_signatures(["nonexistent.ts"], str(tmp_path))
    assert result == {}


def test_file_with_no_exports_excluded_from_result(tmp_path):
    ts_file = tmp_path / "types.ts"
    ts_file.write_text("interface User { id: string; name: string; }")
    result = extract_signatures(["types.ts"], str(tmp_path))
    # No exports/routes/imports → excluded from results
    assert "types.ts" not in result


def test_to_prompt_line_formats_correctly():
    sig = FileSignature(
        path="api/auth.ts",
        exports=["login", "logout"],
        routes=["POST /api/login"],
    )
    line = sig.to_prompt_line()
    assert "api/auth.ts" in line
    assert "login" in line
    assert "POST /api/login" in line


# ── Ruby parser tests ─────────────────────────────────────────────────
# Without these, Rails repos (every .rb file) returned 0 exports and
# Stage 3 short-circuited at MIN_EXPORTS_FOR_FLOW_DETECTION=3 → maybe
# (Rails) emitted 0 flows across its entire corpus.

def test_ruby_file_included_by_extract_signatures(tmp_path):
    rb = tmp_path / "user.rb"
    rb.write_text(
        "class User < ApplicationRecord\n"
        "  has_many :posts\n"
        "  scope :active, -> { where(active: true) }\n"
        "  def full_name\n"
        "    \"#{first_name} #{last_name}\"\n"
        "  end\n"
        "  def self.lookup(id)\n"
        "    find_by(id: id)\n"
        "  end\n"
        "end\n"
    )
    result = extract_signatures(["user.rb"], str(tmp_path))
    assert "user.rb" in result
    sig = result["user.rb"]
    assert "User" in sig.exports
    assert "full_name" in sig.exports
    assert "lookup" in sig.exports
    assert "posts" in sig.exports
    assert "active" in sig.exports


def test_ruby_module_constants_extracted(tmp_path):
    rb = tmp_path / "config.rb"
    rb.write_text(
        "module Billing\n"
        "  PRICE_TIERS = [9, 29, 99]\n"
        "  MAX_RETRIES = 3\n"
        "  def self.process(amount)\n"
        "    amount * 100\n"
        "  end\n"
        "end\n"
    )
    result = extract_signatures(["config.rb"], str(tmp_path))
    assert "config.rb" in result
    sig = result["config.rb"]
    assert "Billing" in sig.exports
    assert "PRICE_TIERS" in sig.exports
    assert "MAX_RETRIES" in sig.exports
    assert "process" in sig.exports


def test_ruby_routes_dsl_extracted(tmp_path):
    rb = tmp_path / "config" / "routes.rb"
    rb.parent.mkdir(parents=True)
    rb.write_text(
        "Rails.application.routes.draw do\n"
        "  get '/health' => 'monitoring#health'\n"
        "  post '/login' => 'sessions#create'\n"
        "  delete '/sessions' => 'sessions#destroy'\n"
        "  scope :api do\n"
        "    get '/users' => 'api/users#index'\n"
        "  end\n"
        "end\n"
    )
    result = extract_signatures(["config/routes.rb"], str(tmp_path))
    assert "config/routes.rb" in result
    sig = result["config/routes.rb"]
    assert "GET /health" in sig.routes
    assert "POST /login" in sig.routes
    assert "DELETE /sessions" in sig.routes
    assert "GET /users" in sig.routes


def test_ruby_rake_extension_supported(tmp_path):
    rake = tmp_path / "lib" / "tasks" / "cleanup.rake"
    rake.parent.mkdir(parents=True)
    rake.write_text(
        "namespace :cleanup do\n"
        "  desc 'Purge stale sessions'\n"
        "  task :purge_sessions => :environment do\n"
        "    Session.expired.destroy_all\n"
        "  end\n"
        "end\n"
        "class CleanupJob\n"
        "  def perform; end\n"
        "end\n"
    )
    result = extract_signatures(["lib/tasks/cleanup.rake"], str(tmp_path))
    assert "lib/tasks/cleanup.rake" in result
    sig = result["lib/tasks/cleanup.rake"]
    assert "CleanupJob" in sig.exports
    assert "perform" in sig.exports
