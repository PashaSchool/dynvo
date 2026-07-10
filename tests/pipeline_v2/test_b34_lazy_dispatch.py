"""B34 — lazy-import edges (Tier 1) + dispatch-registry seeds (Tier 2).

Covers the spec's anti-cases: TYPE_CHECKING excluded; optional deps
marked not minted; cycle-breaking imports never become registries;
dead-code (undeclared) connectors never revive; covered targets are
never duplicated; no owner → no seed; both flags default OFF.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.pipeline_v2.dispatch_registry import (
    DISPATCH_REGISTRY_ENV,
    detect_py_registries,
    detect_ts_registries,
    dispatch_registry_enabled,
    mint_dispatch_seeds,
)
from faultline.pipeline_v2.lazy_imports import (
    LAZY_IMPORT_EDGES_ENV,
    collect_lazy_import_edges,
    lazy_import_edges_enabled,
)
from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows, FlowSpec


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content).lstrip("\n"))


# ── flags ───────────────────────────────────────────────────────────────


def test_flags_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LAZY_IMPORT_EDGES_ENV, raising=False)
    monkeypatch.delenv(DISPATCH_REGISTRY_ENV, raising=False)
    assert lazy_import_edges_enabled() is False
    assert dispatch_registry_enabled() is False


def test_flags_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LAZY_IMPORT_EDGES_ENV, "1")
    monkeypatch.setenv(DISPATCH_REGISTRY_ENV, "1")
    assert lazy_import_edges_enabled() is True
    assert dispatch_registry_enabled() is True


# ── Tier 1: python lazy edges ───────────────────────────────────────────


def _py_repo(tmp_path: Path) -> list[str]:
    _write(tmp_path, "svc/vendor_a.py", "class VendorA:\n    pass\n")
    _write(tmp_path, "svc/vendor_b.py", "class VendorB:\n    pass\n")
    _write(tmp_path, "svc/helper.py", "def util():\n    return 1\n")
    _write(tmp_path, "svc/factory.py", """
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from svc.helper import util  # type-only — never an edge

        def make(kind):
            if kind == "a":
                from svc.vendor_a import VendorA
                return VendorA()
            if kind == "b":
                from svc.vendor_b import VendorB
                return VendorB()
            return None

        def optional_dep():
            try:
                from svc.helper import util
            except ImportError:
                util = None
            return util
    """)
    _write(tmp_path, "svc/cycle_breaker.py", """
        def late():
            from svc.helper import util  # break an import cycle
            value = util()
            return value + 1
    """)
    return [
        "svc/vendor_a.py", "svc/vendor_b.py", "svc/helper.py",
        "svc/factory.py", "svc/cycle_breaker.py",
    ]


def test_py_lazy_edges_collected(tmp_path: Path) -> None:
    files = _py_repo(tmp_path)
    edges = collect_lazy_import_edges(tmp_path, files)
    targets = {(e.src, e.target_file) for e in edges}
    assert ("svc/factory.py", "svc/vendor_a.py") in targets
    assert ("svc/factory.py", "svc/vendor_b.py") in targets
    assert ("svc/cycle_breaker.py", "svc/helper.py") in targets


def test_py_type_checking_excluded(tmp_path: Path) -> None:
    files = _py_repo(tmp_path)
    edges = collect_lazy_import_edges(tmp_path, files)
    # The TYPE_CHECKING import of svc.helper in factory.py is NOT an
    # edge; the only factory edges are the two vendor branches + the
    # optional-dep helper import inside optional_dep().
    factory_edges = [e for e in edges if e.src == "svc/factory.py"]
    helper_edges = [e for e in factory_edges
                    if e.target_file == "svc/helper.py"]
    assert len(helper_edges) == 1  # from optional_dep(), not TYPE_CHECKING
    assert helper_edges[0].optional is True


def test_py_optional_marked(tmp_path: Path) -> None:
    files = _py_repo(tmp_path)
    edges = collect_lazy_import_edges(tmp_path, files)
    opt = [e for e in edges if e.optional]
    assert [e.target_file for e in opt] == ["svc/helper.py"]


def test_py_relative_import_resolves(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/impl.py", "class Impl:\n    pass\n")
    _write(tmp_path, "pkg/entry.py", """
        def get():
            from .impl import Impl
            return Impl()
    """)
    edges = collect_lazy_import_edges(
        tmp_path, ["pkg/__init__.py", "pkg/impl.py", "pkg/entry.py"],
    )
    assert [(e.src, e.target_file) for e in edges] == [
        ("pkg/entry.py", "pkg/impl.py"),
    ]


# ── Tier 1: ts lazy edges ───────────────────────────────────────────────


def test_ts_dynamic_import_edge(tmp_path: Path) -> None:
    _write(tmp_path, "src/widget.ts", "export const W = 1;\n")
    _write(tmp_path, "src/page.ts", """
        import { A } from './static-dep';
        export async function load() {
          const mod = await import('./widget');
          return mod.W;
        }
    """)
    _write(tmp_path, "src/static-dep.ts", "export const A = 2;\n")
    edges = collect_lazy_import_edges(
        tmp_path, ["src/widget.ts", "src/page.ts", "src/static-dep.ts"],
    )
    assert [(e.src, e.target_file) for e in edges] == [
        ("src/page.ts", "src/widget.ts"),
    ]


# ── Tier 2: python registry detection ───────────────────────────────────


def test_py_registry_detected(tmp_path: Path) -> None:
    files = _py_repo(tmp_path)
    edges = collect_lazy_import_edges(tmp_path, files)
    targets = detect_py_registries(tmp_path, edges)
    assert [(t.key, t.symbol, t.target_file) for t in targets] == [
        ("a", "VendorA", "svc/vendor_a.py"),
        ("b", "VendorB", "svc/vendor_b.py"),
    ]
    assert all(t.registry_file == "svc/factory.py" for t in targets)


def test_cycle_breaker_is_not_a_registry(tmp_path: Path) -> None:
    files = _py_repo(tmp_path)
    edges = collect_lazy_import_edges(tmp_path, files)
    targets = detect_py_registries(tmp_path, edges)
    assert all(t.registry_file != "svc/cycle_breaker.py" for t in targets)


def test_single_branch_is_not_a_registry(tmp_path: Path) -> None:
    _write(tmp_path, "one/only.py", "class Only:\n    pass\n")
    _write(tmp_path, "one/f.py", """
        def make(kind):
            if kind == "only":
                from one.only import Only
                return Only()
            return None
    """)
    files = ["one/only.py", "one/f.py"]
    edges = collect_lazy_import_edges(tmp_path, files)
    assert detect_py_registries(tmp_path, edges) == []


# ── Tier 2: ts registry detection ───────────────────────────────────────


def test_ts_switch_registry_detected(tmp_path: Path) -> None:
    _write(tmp_path, "proc/alpha.ts", "export const alphaProc = 1;\n")
    _write(tmp_path, "proc/beta.ts", "export const betaProc = 1;\n")
    _write(tmp_path, "proc/serialize.ts", """
        import { alphaProc } from './alpha';
        import { betaProc } from './beta';

        export function processorFor(kind: string) {
          switch (kind) {
            case 'alpha_agent':
              return alphaProc;
            case 'beta_agent':
              return betaProc;
            default:
              return null;
          }
        }
    """)
    targets = detect_ts_registries(
        tmp_path, ["proc/alpha.ts", "proc/beta.ts", "proc/serialize.ts"],
    )
    assert [(t.key, t.symbol, t.target_file) for t in targets] == [
        ("alpha_agent", "alphaProc", "proc/alpha.ts"),
        ("beta_agent", "betaProc", "proc/beta.ts"),
    ]


def test_ts_dynamic_map_registry_detected(tmp_path: Path) -> None:
    _write(tmp_path, "apps/appa/Setup.tsx", "export default () => null;\n")
    _write(tmp_path, "apps/appb/Setup.tsx", "export default () => null;\n")
    _write(tmp_path, "apps/registry.tsx", """
        export const SetupMap = {
          appa: dynamic(() => import('./appa/Setup')),
          appb: dynamic(() => import('./appb/Setup')),
        };
    """)
    targets = detect_ts_registries(
        tmp_path,
        ["apps/appa/Setup.tsx", "apps/appb/Setup.tsx", "apps/registry.tsx"],
    )
    got = {(t.key, t.target_file) for t in targets}
    assert got == {
        ("appa", "apps/appa/Setup.tsx"),
        ("appb", "apps/appb/Setup.tsx"),
    }


def test_two_unrelated_dynamic_imports_not_a_registry(tmp_path: Path) -> None:
    # Route-level code splitting: const decls, not a keyed map.
    _write(tmp_path, "src/a.tsx", "export default 1;\n")
    _write(tmp_path, "src/b.tsx", "export default 1;\n")
    _write(tmp_path, "src/page.tsx", """
        const A = dynamic(() => import('./a'));
        const B = dynamic(() => import('./b'));
        export default function Page() { return null; }
    """)
    targets = detect_ts_registries(
        tmp_path, ["src/a.tsx", "src/b.tsx", "src/page.tsx"],
    )
    assert targets == []


# ── Tier 2: minting ─────────────────────────────────────────────────────


def _fwf(name: str, paths: list[str], flows: list[FlowSpec] | None = None):
    return FeatureWithFlows(
        feature=SimpleNamespace(name=name, paths=paths),
        flows=list(flows or []),
    )


def test_mint_uncovered_target(tmp_path: Path) -> None:
    files = _py_repo(tmp_path)
    edges = collect_lazy_import_edges(tmp_path, files)
    targets = detect_py_registries(tmp_path, edges)
    fwf = _fwf("edr", ["svc/factory.py", "svc/vendor_a.py",
                       "svc/vendor_b.py"])
    tele = mint_dispatch_seeds([fwf], targets, tmp_path)
    assert tele["minted"] == 2
    names = [f.name for f in fwf.flows]
    assert names == ["run-vendor-a-flow", "run-vendor-b-flow"]
    assert all(f.description.startswith("dispatch registry svc/factory.py")
               for f in fwf.flows)
    assert fwf.flows[0].entry_point_file == "svc/vendor_a.py"


def test_covered_target_not_duplicated(tmp_path: Path) -> None:
    files = _py_repo(tmp_path)
    edges = collect_lazy_import_edges(tmp_path, files)
    targets = detect_py_registries(tmp_path, edges)
    existing = FlowSpec(name="sync-vendor-a-flow",
                        entry_point_file="svc/vendor_a.py")
    fwf = _fwf("edr", ["svc/factory.py", "svc/vendor_a.py",
                       "svc/vendor_b.py"], [existing])
    tele = mint_dispatch_seeds([fwf], targets, tmp_path)
    assert tele["minted"] == 1
    assert tele["skipped_covered"] == 1
    assert [f.name for f in fwf.flows] == [
        "sync-vendor-a-flow", "run-vendor-b-flow",
    ]


def test_no_owner_no_seed(tmp_path: Path) -> None:
    files = _py_repo(tmp_path)
    edges = collect_lazy_import_edges(tmp_path, files)
    targets = detect_py_registries(tmp_path, edges)
    fwf = _fwf("unrelated", ["other/file.py"])
    tele = mint_dispatch_seeds([fwf], targets, tmp_path)
    assert tele["minted"] == 0
    assert tele["skipped_no_owner"] == 2
    assert fwf.flows == []


def test_dead_code_connector_not_revived(tmp_path: Path) -> None:
    # vendor_c is lazily importable in principle but NOT declared by any
    # registry — minting is declaration-driven, so it never revives.
    files = _py_repo(tmp_path)
    _write(tmp_path, "svc/vendor_c.py", "class VendorC:\n    pass\n")
    files = files + ["svc/vendor_c.py"]
    edges = collect_lazy_import_edges(tmp_path, files)
    targets = detect_py_registries(tmp_path, edges)
    assert all(t.target_file != "svc/vendor_c.py" for t in targets)
    fwf = _fwf("edr", ["svc/factory.py", "svc/vendor_a.py",
                       "svc/vendor_b.py", "svc/vendor_c.py"])
    mint_dispatch_seeds([fwf], targets, tmp_path)
    assert all(f.entry_point_file != "svc/vendor_c.py" for f in fwf.flows)


def test_mint_deterministic(tmp_path: Path) -> None:
    files = _py_repo(tmp_path)
    edges = collect_lazy_import_edges(tmp_path, files)
    targets = detect_py_registries(tmp_path, edges)

    def build():
        fwf = _fwf("edr", ["svc/factory.py", "svc/vendor_a.py",
                           "svc/vendor_b.py"])
        mint_dispatch_seeds([fwf], targets, tmp_path)
        return [f.name for f in fwf.flows]

    assert build() == build()
