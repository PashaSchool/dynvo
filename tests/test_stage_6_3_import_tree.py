"""Tests for :mod:`faultline.pipeline_v2.stage_6_3_import_tree` (Sprint C3)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from faultline.models.types import Feature, Flow
from faultline.pipeline_v2.stage_6_3_import_tree import (
    DEFAULT_MAX_DEPTH,
    build_artifact_payload,
    enrich_with_import_tree,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _ctx(repo: Path) -> SimpleNamespace:
    """Minimal ScanContext duck-type that the stage only reads from."""
    tracked: list[str] = []
    for f in repo.rglob("*"):
        if f.is_file() and "/.git" not in str(f):
            try:
                rel = f.relative_to(repo).as_posix()
                tracked.append(rel)
            except ValueError:
                continue
    return SimpleNamespace(
        repo_path=repo,
        tracked_files=tuple(tracked),
        run_dir=None,
        stack="next-app-router",
        monorepo=False,
        workspaces=[],
    )


def _new_feature(
    name: str,
    paths: Iterable[str],
    *,
    description: str | None = None,
    flows: list[Flow] | None = None,
) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        description=description,
        flows=flows or [],
        layer="developer",
    )


def _new_flow(name: str, entry_file: str, entry_line: int = 1) -> Flow:
    return Flow(
        name=name,
        entry_point_file=entry_file,
        entry_point_line=entry_line,
        paths=[entry_file],
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
    )


# ── Tests ────────────────────────────────────────────────────────────────


def test_forward_chain_page_component_hook(tmp_path: Path) -> None:
    """3-level page → component → hook chain attributes all three with
    real line ranges."""
    _w(tmp_path / "tsconfig.json", json.dumps({
        "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["./*"]},
        },
    }))
    _w(tmp_path / "app/page.tsx", """\
import { RuleForm } from "@/components/RuleForm";

export default function CreateRulePage() {
  return <RuleForm />;
}
""")
    _w(tmp_path / "components/RuleForm.tsx", """\
import { useRules } from "@/hooks/useRules";

export function RuleForm() {
  const rules = useRules();
  return <div>{rules.length}</div>;
}
""")
    _w(tmp_path / "hooks/useRules.tsx", """\
export function useRules() {
  return [1, 2, 3];
}
""")
    ctx = _ctx(tmp_path)
    flow = _new_flow("create-rule-flow", "app/page.tsx", entry_line=3)
    feature = _new_feature("rules", ["app/page.tsx"], flows=[flow])
    res = enrich_with_import_tree(ctx, [feature])
    assert res.total_seeds == 1
    assert res.total_files_reached >= 2
    assert "components/RuleForm.tsx" in feature.paths
    assert "hooks/useRules.tsx" in feature.paths
    # Flow level — entry symbol + 2 called symbols.
    assert any(a.symbol == "RuleForm" for a in flow.flow_symbol_attributions)
    assert any(a.symbol == "useRules" for a in flow.flow_symbol_attributions)


def test_reverse_package_anchor_seeds(tmp_path: Path) -> None:
    """Feature with no flows but ``package anchor 'billing' from deps
    ['stripe']`` description seeds reverse-find."""
    _w(tmp_path / "apps/web/billing.ts", """\
import Stripe from "stripe";

export function chargeCustomer(amount: number) {
  const s = new Stripe("key");
  return s.charges.create({amount});
}
""")
    _w(tmp_path / "apps/web/checkout.ts", """\
import { loadStripe } from "stripe";

export async function startCheckout() {
  const s = await loadStripe("pk");
  return s;
}
""")
    _w(tmp_path / "apps/web/unrelated.ts", """\
export const helper = () => 1;
""")
    ctx = _ctx(tmp_path)
    feature = _new_feature(
        "billing",
        ["apps/web"],
        description="[package] package anchor 'billing' from deps ['stripe']",
    )
    res = enrich_with_import_tree(ctx, [feature])
    assert res.total_seeds >= 2
    paths_after = set(feature.paths)
    assert "apps/web/billing.ts" in paths_after
    assert "apps/web/checkout.ts" in paths_after
    assert "apps/web/unrelated.ts" not in paths_after
    # Symbol attribution on the feature.
    files = {a.file_path for a in feature.shared_attributions}
    assert "apps/web/billing.ts" in files
    assert "apps/web/checkout.ts" in files


def test_reverse_schema_source_seeds(tmp_path: Path) -> None:
    """Schema-source feature seeds from model-name consumers."""
    _w(tmp_path / "schema/schema.prisma", """\
model User {
  id String @id
}

model Account {
  id String @id
}
""")
    _w(tmp_path / "lib/userService.ts", """\
import { User } from "schema/schema";

export function fetchUser(id: string): User {
  return {id} as User;
}
""")
    _w(tmp_path / "lib/accountService.ts", """\
import { Account } from "schema/schema";

export function fetchAccount(id: string): Account {
  return {id} as Account;
}
""")
    ctx = _ctx(tmp_path)
    feature = _new_feature(
        "data-models",
        ["schema/schema.prisma"],
        description="schema domain models for users + accounts",
    )
    res = enrich_with_import_tree(ctx, [feature])
    assert res.total_seeds >= 2
    paths_after = set(feature.paths)
    assert "lib/userService.ts" in paths_after
    assert "lib/accountService.ts" in paths_after


def test_config_as_product_does_not_expand(tmp_path: Path) -> None:
    """Config-as-product features stay at their manifest path."""
    _w(tmp_path / "tauri.conf.json", '{"build": {}}')
    ctx = _ctx(tmp_path)
    feature = _new_feature(
        "main-window",
        ["tauri.conf.json"],
        description="config-as-product anchor from tauri manifest",
    )
    res = enrich_with_import_tree(ctx, [feature])
    assert res.total_seeds == 0
    assert feature.paths == ["tauri.conf.json"]


def test_cycle_detection(tmp_path: Path) -> None:
    """A → B → A skips the second visit and increments the cycle counter."""
    _w(tmp_path / "tsconfig.json", json.dumps({
        "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}},
    }))
    _w(tmp_path / "a.ts", """\
import { b } from "@/b";

export function a() {
  return b();
}
""")
    _w(tmp_path / "b.ts", """\
import { a } from "@/a";

export function b() {
  return a();
}
""")
    ctx = _ctx(tmp_path)
    flow = _new_flow("ping", "a.ts", entry_line=3)
    feature = _new_feature("loop", ["a.ts"], flows=[flow])
    res = enrich_with_import_tree(ctx, [feature])
    assert res.cycles_detected >= 1


def test_depth_cap(tmp_path: Path) -> None:
    """A chain exceeding ``max_depth`` stops with depth_capped incremented."""
    _w(tmp_path / "tsconfig.json", json.dumps({
        "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}},
    }))
    # Build f0 → f1 → f2 → f3 (multi-line so the function regex
    # anchors to the start of a line).
    _w(tmp_path / "f0.ts", 'import {f1} from "@/f1";\nexport function f0() {\n  return f1();\n}\n')
    _w(tmp_path / "f1.ts", 'import {f2} from "@/f2";\nexport function f1() {\n  return f2();\n}\n')
    _w(tmp_path / "f2.ts", 'import {f3} from "@/f3";\nexport function f2() {\n  return f3();\n}\n')
    _w(tmp_path / "f3.ts", 'export function f3() {\n  return 1;\n}\n')
    ctx = _ctx(tmp_path)
    flow = _new_flow("chain", "f0.ts", entry_line=2)
    feature = _new_feature("c", ["f0.ts"], flows=[flow])
    res = enrich_with_import_tree(
        ctx, [feature],
        max_depth=2,  # only f0 → f1 → f2; f3 must be depth-capped.
    )
    assert res.depth_capped_events >= 1
    assert "f3.ts" not in feature.paths


def test_external_import_skipped(tmp_path: Path) -> None:
    """``from "react"`` doesn't recurse — it's external."""
    _w(tmp_path / "page.tsx", """\
import {useState} from "react";

export default function Page(){
  const [x] = useState(0);
  return x;
}
""")
    ctx = _ctx(tmp_path)
    flow = _new_flow("render", "page.tsx", entry_line=3)
    feature = _new_feature("pg", ["page.tsx"], flows=[flow])
    res = enrich_with_import_tree(ctx, [feature])
    # The flow's entry symbol is still attributed (depth 0).
    assert res.total_symbols_emitted >= 1
    # No external paths leaked into feature.paths.
    assert all("node_modules" not in p for p in feature.paths)


def test_backward_compat_no_seeds_no_change(tmp_path: Path) -> None:
    """A feature with no flows + no anchor description + no usable
    structural seed leaves paths unchanged."""
    # An empty schema file with no detectable symbols.
    _w(tmp_path / "data.json", '{"value": 1}')
    ctx = _ctx(tmp_path)
    feature = _new_feature("misc", ["data.json"])
    res = enrich_with_import_tree(ctx, [feature])
    # No expansion possible.
    assert feature.paths == ["data.json"]
    # We DO record the feature in per_feature with seeds_count=0.
    assert res.per_feature[0].seeds_count == 0


def test_cache_hits_on_shared_file(tmp_path: Path) -> None:
    """Two flows sharing a downstream import file → cache hits > 0."""
    _w(tmp_path / "tsconfig.json", json.dumps({
        "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}},
    }))
    _w(tmp_path / "shared.ts", "export function shared(){return 1;}")
    _w(tmp_path / "a.ts", 'import {shared} from "@/shared"; export function a(){return shared();}')
    _w(tmp_path / "b.ts", 'import {shared} from "@/shared"; export function b(){return shared();}')
    ctx = _ctx(tmp_path)
    flows = [_new_flow("a-flow", "a.ts", 1), _new_flow("b-flow", "b.ts", 1)]
    feature = _new_feature("multi", ["a.ts", "b.ts"], flows=flows)
    res = enrich_with_import_tree(ctx, [feature])
    assert res.cache_hits > 0


def test_path_alias_resolution_inside_traversal(tmp_path: Path) -> None:
    """A flow whose import uses an alias only declared in a workspace
    tsconfig still resolves."""
    _w(tmp_path / "apps/web/tsconfig.json", json.dumps({
        "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}},
    }))
    _w(tmp_path / "apps/web/page.tsx", """\
import {fetcher} from "@/lib/fetcher";

export default function Page(){
  return fetcher();
}
""")
    _w(tmp_path / "apps/web/lib/fetcher.ts", "export function fetcher(){return 1;}")
    ctx = _ctx(tmp_path)
    flow = _new_flow("show", "apps/web/page.tsx", entry_line=3)
    feature = _new_feature(
        "view", ["apps/web/page.tsx"], flows=[flow],
    )
    res = enrich_with_import_tree(ctx, [feature])
    assert "apps/web/lib/fetcher.ts" in feature.paths


def test_artifact_payload_has_required_fields(tmp_path: Path) -> None:
    """The artifact dict carries every key the orchestrator expects."""
    ctx = _ctx(tmp_path)
    res = enrich_with_import_tree(ctx, [])
    payload = build_artifact_payload(
        res,
        max_depth=DEFAULT_MAX_DEPTH,
        max_files_per_feature=100,
        max_symbols_per_feature=500,
    )
    assert payload["stage"] == "6.3-import-tree"
    assert "config" in payload
    assert "alias_map_size" in payload
    assert "features" in payload
    assert "aggregate" in payload
    for k in (
        "total_seeds", "total_files_reached", "total_symbols_emitted",
        "cycles_detected", "depth_capped_events", "external_skipped",
        "cache_hits",
    ):
        assert k in payload["aggregate"]


def test_inline_param_type_does_not_truncate_body(tmp_path: Path) -> None:
    """A function whose signature has ``(props: {...})`` is NOT
    truncated at the param brace — the body extension should still
    follow all imports referenced inside the actual function body.
    """
    _w(tmp_path / "tsconfig.json", json.dumps({
        "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}},
    }))
    _w(tmp_path / "components/Form.tsx", """\
export function Form() { return null; }
""")
    _w(tmp_path / "utils/cond.ts", """\
export function getEmptyCond() { return {}; }
""")
    _w(tmp_path / "page.tsx", """\
import { Form } from "@/components/Form";
import { getEmptyCond } from "@/utils/cond";

export default async function Page(props: {
  searchParams: Promise<{
    type?: string;
  }>;
}) {
  const sp = await props.searchParams;
  return <Form initial={[getEmptyCond()]} type={sp.type} />;
}
""")
    ctx = _ctx(tmp_path)
    flow = _new_flow("show", "page.tsx", entry_line=4)
    feature = _new_feature("p", ["page.tsx"], flows=[flow])
    res = enrich_with_import_tree(ctx, [feature])
    assert "components/Form.tsx" in feature.paths
    assert "utils/cond.ts" in feature.paths


def test_structural_fallback_picks_dominant_symbol(tmp_path: Path) -> None:
    """A route feature with no flow + no anchor uses the first
    non-trivial top-level function as a seed."""
    _w(tmp_path / "tsconfig.json", json.dumps({
        "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}},
    }))
    _w(tmp_path / "api/route.ts", """\
import {handler} from "@/lib/handler";

export async function POST(req: Request) {
  return handler(req);
}
""")
    _w(tmp_path / "lib/handler.ts", "export function handler(_: Request){return new Response('ok');}")
    ctx = _ctx(tmp_path)
    feature = _new_feature("api", ["api/route.ts"])  # no flows, no description
    res = enrich_with_import_tree(ctx, [feature])
    # Structural seed should have picked POST and expanded to handler.ts.
    assert res.total_seeds >= 1
    assert "lib/handler.ts" in feature.paths


def test_default_max_depth_is_8_sprint_c3b() -> None:
    """Sprint C3b raised the default BFS ceiling from 6 to 8."""
    assert DEFAULT_MAX_DEPTH == 8


def test_feature_symbol_attributions_populated_for_package_anchor(
    tmp_path: Path,
) -> None:
    """Sprint C3b — package-anchor feature with no flows must surface
    its enrichment on ``feature.symbol_attributions`` (per-symbol),
    not only on the legacy ``shared_attributions`` per-file aggregate.

    This is the Billing-on-inbox-zero regression the sprint targets:
    paths went 1→32 in C3 but feature-level per-symbol records were
    empty because flow-level was the only path the landing app could
    read.
    """
    _w(tmp_path / "apps/web/billing.ts", """\
import Stripe from "stripe";

export function chargeCustomer(amount: number) {
  const s = new Stripe("key");
  return s.charges.create({amount});
}
""")
    _w(tmp_path / "apps/web/checkout.ts", """\
import { loadStripe } from "stripe";

export async function startCheckout() {
  const s = await loadStripe("pk");
  return s;
}
""")
    ctx = _ctx(tmp_path)
    feature = _new_feature(
        "billing",
        ["apps/web"],
        description="[package] package anchor 'billing' from deps ['stripe']",
    )
    enrich_with_import_tree(ctx, [feature])
    # New C3b surface — feature-level per-symbol records.
    assert len(feature.symbol_attributions) >= 2
    files = {a.file for a in feature.symbol_attributions}
    assert "apps/web/billing.ts" in files
    assert "apps/web/checkout.ts" in files
    # Every record has all five spec fields populated.
    for a in feature.symbol_attributions:
        assert isinstance(a.file, str) and a.file
        assert isinstance(a.symbol, str) and a.symbol
        assert a.line_start >= 1
        assert a.line_end >= a.line_start
        assert a.role in {
            "entry", "called", "support",
            "anchor-consumer", "schema-consumer", "structural",
        }
    # At least one record carries the reverse-import role from the
    # anchor seed phase.
    assert any(a.role == "anchor-consumer" for a in feature.symbol_attributions)
    # Legacy per-file aggregate stays populated for back-compat.
    legacy_files = {a.file_path for a in feature.shared_attributions}
    assert "apps/web/billing.ts" in legacy_files


def test_feature_symbol_attributions_unions_flow_records(
    tmp_path: Path,
) -> None:
    """Sprint C3b — for flow-bearing features, feature.symbol_attributions
    should contain both the feature's own seed records AND the flow's
    called-symbol chain (de-duplicated by (file, symbol))."""
    _w(tmp_path / "tsconfig.json", json.dumps({
        "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}},
    }))
    _w(tmp_path / "apps/web/page.tsx", """\
import {Widget} from "@/components/Widget";

export default function Page() {
  return <Widget/>;
}
""")
    _w(tmp_path / "components/Widget.tsx", """\
export function Widget() {
  return <div>hi</div>;
}
""")
    ctx = _ctx(tmp_path)
    flow = _new_flow("render-page", "apps/web/page.tsx", entry_line=3)
    feature = _new_feature("home", ["apps/web/page.tsx"], flows=[flow])
    enrich_with_import_tree(ctx, [feature])
    # Feature-level records cover both the entry and the called widget.
    files = {a.file for a in feature.symbol_attributions}
    assert "apps/web/page.tsx" in files
    assert "components/Widget.tsx" in files
    # De-duplication: no two records share (file, symbol).
    seen: set[tuple[str, str]] = set()
    for a in feature.symbol_attributions:
        key = (a.file, a.symbol)
        assert key not in seen, f"duplicate (file, symbol) in feature.symbol_attributions: {key}"
        seen.add(key)
