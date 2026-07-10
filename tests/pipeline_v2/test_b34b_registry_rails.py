"""B34-b — registry rails: UI micro-component skip + key qualifier.

Anti-cases: Soc0-style unique-symbol factory mints are byte-stable
(names unchanged, nothing skipped); unique-kind UI components still
mint; repeated-kind SERVER (.ts) targets are qualified, never skipped.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

from faultline.pipeline_v2.dispatch_registry import (
    RegistryTarget,
    _apply_registry_rails,
    detect_py_registries,
    detect_ts_registries,
    mint_dispatch_seeds,
)
from faultline.pipeline_v2.lazy_imports import collect_lazy_import_edges
from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content).lstrip("\n"))


def _fwf(name: str, paths: list[str]):
    return FeatureWithFlows(
        feature=SimpleNamespace(name=name, paths=paths), flows=[],
    )


def _t(reg: str, key: str, sym: str, target: str) -> RegistryTarget:
    return RegistryTarget(
        registry_file=reg, key=key, symbol=sym, target_file=target,
    )


# ── rail 1: UI micro-component skip ─────────────────────────────────────


def test_repeated_tsx_kind_skipped() -> None:
    targets = [
        _t("apps/reg.tsx", "alby", "", "apps/alby/components/Card.tsx"),
        _t("apps/reg.tsx", "paypal", "", "apps/paypal/components/Card.tsx"),
        _t("apps/reg.tsx", "zapier", "", "apps/zapier/components/Card.tsx"),
    ]
    kept, cores, ui_skipped = _apply_registry_rails(targets)
    assert kept == []
    assert ui_skipped == 3


def test_unique_tsx_kind_kept() -> None:
    # routing-forms class: distinct component kinds — real pages.
    targets = [
        _t("apps/rf.tsx", "form-edit", "", "apps/rf/FormEdit.tsx"),
        _t("apps/rf.tsx", "route-builder", "", "apps/rf/RouteBuilder.tsx"),
    ]
    kept, cores, ui_skipped = _apply_registry_rails(targets)
    assert len(kept) == 2 and ui_skipped == 0
    assert cores["apps/rf/FormEdit.tsx"] == "form-edit"


def test_repeated_server_kind_not_skipped_but_qualified() -> None:
    # <app>/api/index.ts x3 — server adapters: keep, qualify by key.
    targets = [
        _t("apps/srv.ts", "alby", "", "packages/app-store/alby/api/index.ts"),
        _t("apps/srv.ts", "paypal", "", "packages/app-store/paypal/api/index.ts"),
        _t("apps/srv.ts", "zoom", "", "packages/app-store/zoom/api/index.ts"),
    ]
    kept, cores, ui_skipped = _apply_registry_rails(targets)
    assert len(kept) == 3 and ui_skipped == 0
    assert cores["packages/app-store/alby/api/index.ts"] == "alby-api"
    assert cores["packages/app-store/zoom/api/index.ts"] == "zoom-api"


def test_duplicate_symbol_across_apps_qualified_by_key() -> None:
    # CalendarService x3 — same exported symbol, different apps.
    targets = [
        _t("cal.gen.ts", "google", "CalendarService",
           "packages/app-store/googlecalendar/lib/CalendarService.ts"),
        _t("cal.gen.ts", "office365", "CalendarService",
           "packages/app-store/office365calendar/lib/CalendarService.ts"),
        _t("cal.gen.ts", "zoom", "CalendarService",
           "packages/app-store/zoom/lib/CalendarService.ts"),
    ]
    kept, cores, _ = _apply_registry_rails(targets)
    assert len(kept) == 3
    assert cores[
        "packages/app-store/googlecalendar/lib/CalendarService.ts"
    ] == "google-calendar-service"
    assert cores[
        "packages/app-store/zoom/lib/CalendarService.ts"
    ] == "zoom-calendar-service"


def test_app_dir_fallback_token_when_no_key() -> None:
    targets = [
        _t("reg.ts", "", "VideoApiAdapter",
           "packages/app-store/zoomvideo/lib/VideoApiAdapter.ts"),
        _t("reg.ts", "", "VideoApiAdapter",
           "packages/app-store/dailyvideo/lib/VideoApiAdapter.ts"),
    ]
    kept, cores, _ = _apply_registry_rails(targets)
    assert cores[
        "packages/app-store/zoomvideo/lib/VideoApiAdapter.ts"
    ] == "zoomvideo-video-api-adapter"
    assert cores[
        "packages/app-store/dailyvideo/lib/VideoApiAdapter.ts"
    ] == "dailyvideo-video-api-adapter"


# ── anti-case: Soc0-style factory is byte-stable ────────────────────────


def test_unique_symbol_factory_unchanged(tmp_path: Path) -> None:
    _write(tmp_path, "svc/vendor_a.py", "class VendorA:\n    pass\n")
    _write(tmp_path, "svc/vendor_b.py", "class VendorB:\n    pass\n")
    _write(tmp_path, "svc/factory.py", """
        def make(kind):
            if kind == "a":
                from svc.vendor_a import VendorA
                return VendorA()
            if kind == "b":
                from svc.vendor_b import VendorB
                return VendorB()
            return None
    """)
    files = ["svc/vendor_a.py", "svc/vendor_b.py", "svc/factory.py"]
    edges = collect_lazy_import_edges(tmp_path, files)
    targets = detect_py_registries(tmp_path, edges)
    fwf = _fwf("edr", files)
    tele = mint_dispatch_seeds([fwf], targets, tmp_path)
    # Names identical to pre-rail B34 — no skip, no qualifier, no ordinal.
    assert [f.name for f in fwf.flows] == [
        "run-vendor-a-flow", "run-vendor-b-flow",
    ]
    assert tele["skipped_ui_component_kind"] == 0
    assert tele["qualified_by_registry_key"] == 0
    assert tele["ordinal_fallback"] == 0


def test_same_target_file_two_keys_counts_once() -> None:
    # Soc0 use-reports map: two keys -> ONE target file. Must not
    # trigger the qualifier (base counted per distinct file).
    targets = [
        _t("hooks/use-reports.ts", "payload", "", "src/api/reports.ts"),
        _t("hooks/use-reports.ts", "position", "", "src/api/reports.ts"),
    ]
    kept, cores, _ = _apply_registry_rails(targets)
    # Pre-rail stem-only base — Soc0's run-reports-flow stays byte-stable.
    assert cores["src/api/reports.ts"] == "reports"


# ── mint integration: qualified names, zero ordinals ────────────────────


def test_mint_qualified_no_ordinals(tmp_path: Path) -> None:
    _write(tmp_path, "packages/app-store/alby/api/index.ts", "export default 1;\n")
    _write(tmp_path, "packages/app-store/paypal/api/index.ts", "export default 1;\n")
    _write(tmp_path, "packages/app-store/reg.ts", """
        export const map = {
          alby: () => import('./alby/api/index'),
          paypal: () => import('./paypal/api/index'),
        };
    """)
    files = [
        "packages/app-store/alby/api/index.ts",
        "packages/app-store/paypal/api/index.ts",
        "packages/app-store/reg.ts",
    ]
    targets = detect_ts_registries(tmp_path, files)
    assert len(targets) == 2
    fwf = _fwf("app-store", files)
    tele = mint_dispatch_seeds([fwf], targets, tmp_path)
    assert sorted(f.name for f in fwf.flows) == [
        "run-alby-api-flow", "run-paypal-api-flow",
    ]
    # The marker-stem walk already embeds the app dir, so the names are
    # distinct WITHOUT the qualifier rung — and no ordinals anywhere.
    assert tele["qualified_by_registry_key"] == 0
    assert tele["ordinal_fallback"] == 0
