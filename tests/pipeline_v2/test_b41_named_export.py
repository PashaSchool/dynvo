"""B41 — pages-surface named-export fallback (novu fresh-blood class)."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.profiles._pages_surface import (
    NAMED_EXPORT_FALLBACK_ENV,
    default_export_symbol,
)


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_default_export_still_wins(tmp_path: Path) -> None:
    _write(tmp_path, "src/pages/a.tsx",
           "export default function HomePage() { return null; }\n"
           "export function Helper() { return null; }\n")
    assert default_export_symbol(tmp_path, "src/pages/a.tsx") == "HomePage"


def test_named_export_fallback(tmp_path: Path) -> None:
    # novu shape: react-router page, named export only.
    _write(tmp_path, "src/pages/access-denied-page.tsx",
           "import { X } from 'lucide-react';\n"
           "export function AccessDeniedPage() {\n  return null;\n}\n")
    assert default_export_symbol(
        tmp_path, "src/pages/access-denied-page.tsx",
    ) == "AccessDeniedPage"


def test_named_const_export_fallback(tmp_path: Path) -> None:
    _write(tmp_path, "src/pages/feed.tsx",
           "export const ActivityFeed = () => {\n  return null;\n};\n")
    assert default_export_symbol(tmp_path, "src/pages/feed.tsx") \
        == "ActivityFeed"


def test_lowercase_exports_never_match(tmp_path: Path) -> None:
    # Utility exports are NOT components — PascalCase convention only.
    _write(tmp_path, "src/pages/util.ts",
           "export function buildRoute() { return 1; }\n"
           "export const routes = {};\n")
    assert default_export_symbol(tmp_path, "src/pages/util.ts") == ""


def test_kill_switch_restores_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAMED_EXPORT_FALLBACK_ENV, "0")
    _write(tmp_path, "src/pages/b.tsx",
           "export function SomePage() { return null; }\n")
    assert default_export_symbol(tmp_path, "src/pages/b.tsx") == ""


def test_wrapped_default_export_unwraps(tmp_path: Path) -> None:
    # B43: supabase studio shape — export default withAuth(Page).
    _write(tmp_path, "src/pages/authorize.tsx",
           "const APIAuthorizationPage = () => null;\n"
           "export default withAuth(APIAuthorizationPage)\n")
    assert default_export_symbol(
        tmp_path, "src/pages/authorize.tsx",
    ) == "APIAuthorizationPage"


def test_plain_default_ident_still_wins(tmp_path: Path) -> None:
    _write(tmp_path, "src/pages/promo.tsx",
           "const AWSReInvent2025 = () => null;\n"
           "export default AWSReInvent2025\n")
    assert default_export_symbol(
        tmp_path, "src/pages/promo.tsx",
    ) == "AWSReInvent2025"
