"""Tests for :mod:`faultline.analyzer.reverse_imports` (Sprint C3)."""

from __future__ import annotations

from pathlib import Path

from faultline.analyzer.reverse_imports import (
    find_consumers_of_module,
    find_symbols_in_file_using_module,
)


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_find_consumers_basic(tmp_path: Path) -> None:
    _w(tmp_path / "a.ts", 'import Stripe from "stripe";\nexport const x = 1;')
    _w(tmp_path / "b.ts", 'import * as S from "stripe";\nexport const y = 2;')
    _w(tmp_path / "c.ts", 'import { foo } from "./bar";\nexport const z = 3;')
    tracked = frozenset({"a.ts", "b.ts", "c.ts"})
    consumers = find_consumers_of_module(["stripe"], tmp_path, tracked)
    assert consumers == ["a.ts", "b.ts"]


def test_find_consumers_scoped_package(tmp_path: Path) -> None:
    _w(tmp_path / "a.ts", 'import { x } from "@stripe/stripe-js";\nexport const x = 1;')
    _w(tmp_path / "b.ts", 'import { y } from "@stripe/other";\nexport const y = 2;')
    tracked = frozenset({"a.ts", "b.ts"})
    consumers = find_consumers_of_module(
        ["@stripe/stripe-js"], tmp_path, tracked,
    )
    assert consumers == ["a.ts"]


def test_find_consumers_with_scope_prefix(tmp_path: Path) -> None:
    _w(tmp_path / "apps/web/a.ts", 'import S from "stripe";\nexport const x = 1;')
    _w(tmp_path / "apps/admin/b.ts", 'import S from "stripe";\nexport const y = 2;')
    tracked = frozenset({"apps/web/a.ts", "apps/admin/b.ts"})
    consumers = find_consumers_of_module(
        ["stripe"], tmp_path, tracked, scope_prefix="apps/web/",
    )
    assert consumers == ["apps/web/a.ts"]


def test_find_consumers_skips_test_files(tmp_path: Path) -> None:
    _w(tmp_path / "a.test.ts", 'import S from "stripe";\nexport const x = 1;')
    _w(tmp_path / "a.ts", 'import S from "stripe";\nexport const y = 2;')
    tracked = frozenset({"a.test.ts", "a.ts"})
    consumers = find_consumers_of_module(["stripe"], tmp_path, tracked)
    assert consumers == ["a.ts"]


def test_find_symbols_in_consumer_file(tmp_path: Path) -> None:
    text = """\
import Stripe from "stripe";

export function chargeCustomer(amount: number) {
  const s = new Stripe("key");
  return s.charges.create({amount});
}

export function unrelated() {
  return 42;
}
"""
    _w(tmp_path / "billing.ts", text)
    syms = find_symbols_in_file_using_module(
        "billing.ts", ["stripe"], tmp_path,
    )
    names = [s[0] for s in syms]
    assert "chargeCustomer" in names
    assert "unrelated" not in names


def test_find_symbols_handles_named_imports(tmp_path: Path) -> None:
    text = """\
import { loadStripe } from "@stripe/stripe-js";

export async function init() {
  const stripe = await loadStripe("pk_test");
  return stripe;
}

export const helper = () => "no-stripe-here";
"""
    _w(tmp_path / "init.ts", text)
    syms = find_symbols_in_file_using_module(
        "init.ts", ["@stripe/stripe-js"], tmp_path,
    )
    names = [s[0] for s in syms]
    assert "init" in names
    # helper doesn't touch loadStripe so it should NOT appear.
    assert "helper" not in names
