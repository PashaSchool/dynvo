"""B37 — dispatch detector v2: then-member navigators (s1), block-execute
string dispatch (s3), default-import config registries (s2).

Anti-cases: single then-member is not a registry; if-in arms calling
non-imported locals are not a registry; default imports without literal
enumeration are not a registry; existing rails/anchor guard untouched.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

from faultline.pipeline_v2.dispatch_registry import (
    detect_ts_registries,
    mint_dispatch_seeds,
)
from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content).lstrip("\n"))


def _fwf(name: str, paths: list[str]):
    return FeatureWithFlows(
        feature=SimpleNamespace(name=name, paths=paths), flows=[],
    )


# ── s1: then-member navigator ───────────────────────────────────────────


def test_then_member_navigator_detected(tmp_path: Path) -> None:
    _write(tmp_path, "app/CronJobs/CronJobPage.tsx",
           "export function CronJobPage() { return null; }\n")
    _write(tmp_path, "app/Queues/QueuePage.tsx",
           "export function QueuePage() { return null; }\n")
    _write(tmp_path, "app/Landing/constants.tsx", """
        export const nav = ({ pageId }) => {
          switch (pageId) {
            case 'cron':
              return dynamic(
                () =>
                  import('../CronJobs/CronJobPage').then(
                    (mod) => mod.CronJobPage
                  ),
                { loading: L }
              )
            case 'queues':
              return dynamic(() => import('../Queues/QueuePage').then((mod) => mod.QueuePage), {
                loading: L,
              })
          }
        }
    """)
    files = ["app/CronJobs/CronJobPage.tsx", "app/Queues/QueuePage.tsx",
             "app/Landing/constants.tsx"]
    targets = [t for t in detect_ts_registries(tmp_path, files)
               if t.symbol in ("CronJobPage", "QueuePage")]
    assert {(t.symbol, t.target_file) for t in targets} == {
        ("CronJobPage", "app/CronJobs/CronJobPage.tsx"),
        ("QueuePage", "app/Queues/QueuePage.tsx"),
    }


def test_single_then_member_not_a_registry(tmp_path: Path) -> None:
    _write(tmp_path, "app/Heavy.tsx", "export function Heavy() {}\n")
    _write(tmp_path, "app/page.tsx", """
        const H = dynamic(() => import('./Heavy').then((m) => m.Heavy));
        export default function P() { return null; }
    """)
    targets = detect_ts_registries(tmp_path, ["app/Heavy.tsx",
                                              "app/page.tsx"])
    assert all(t.symbol != "Heavy" for t in targets)


# ── s3: block-execute string dispatch ───────────────────────────────────


def test_if_in_dispatch_detected(tmp_path: Path) -> None:
    _write(tmp_path, "blocks/chatwoot/executeChatwoot.ts",
           "export const executeChatwoot = (a) => a;\n")
    _write(tmp_path, "blocks/pixel/executePixel.ts",
           "export const executePixel = (a) => a;\n")
    _write(tmp_path, "utils/executeActions.ts", """
        import { executeChatwoot } from '../blocks/chatwoot/executeChatwoot';
        import { executePixel } from '../blocks/pixel/executePixel';

        export const executeClientSideAction = async ({ action }) => {
          if ("chatwoot" in action) {
            return executeChatwoot(action.chatwoot, {});
          }
          if ("pixel" in action) {
            await executePixel(action.pixel);
            return;
          }
        };
    """)
    files = ["blocks/chatwoot/executeChatwoot.ts",
             "blocks/pixel/executePixel.ts", "utils/executeActions.ts"]
    targets = [t for t in detect_ts_registries(tmp_path, files)
               if t.registry_file == "utils/executeActions.ts"]
    assert {(t.key, t.symbol) for t in targets} == {
        ("chatwoot", "executeChatwoot"), ("pixel", "executePixel"),
    }


def test_if_in_arms_calling_locals_not_a_registry(tmp_path: Path) -> None:
    _write(tmp_path, "utils/machine.ts", """
        export const step = (s) => {
          if ("a" in s) { return localA(s); }
          if ("b" in s) { return localB(s); }
        };
        function localA(x) { return x; }
        function localB(x) { return x; }
    """)
    assert detect_ts_registries(tmp_path, ["utils/machine.ts"]) == []


# ── s2: default-import config registry ──────────────────────────────────


def test_default_import_registry_detected(tmp_path: Path) -> None:
    _write(tmp_path, "store/quick-books/config.ts",
           "const app = { id: 'qb' };\nexport default app;\n")
    _write(tmp_path, "store/slack/config.ts",
           "const app = { id: 'slack' };\nexport default app;\n")
    _write(tmp_path, "store/index.ts", """
        import quickBooksApp from "./quick-books/config";
        import slackApp from "./slack/config";

        export const apps = [
          quickBooksApp,
          slackApp,
        ];
    """)
    files = ["store/quick-books/config.ts", "store/slack/config.ts",
             "store/index.ts"]
    targets = [t for t in detect_ts_registries(tmp_path, files)
               if t.registry_file == "store/index.ts"]
    assert {(t.key, t.target_file) for t in targets} == {
        ("quick-books", "store/quick-books/config.ts"),
        ("slack", "store/slack/config.ts"),
    }


def test_default_imports_without_enumeration_not_a_registry(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "a/config.ts", "export default 1;\n")
    _write(tmp_path, "b/config.ts", "export default 1;\n")
    _write(tmp_path, "use.ts", """
        import aApp from "./a/config";
        import bApp from "./b/config";
        export const merged = { ...aApp, ...bApp };
    """)
    files = ["a/config.ts", "b/config.ts", "use.ts"]
    targets = [t for t in detect_ts_registries(tmp_path, files)
               if t.registry_file == "use.ts"]
    assert targets == []


# ── mint integration: anchor guard governs s2 honestly ──────────────────


def test_s3_mints_anchored_s2_skips_exportless(tmp_path: Path) -> None:
    # s3 executors have named exports -> mint; s2 exportless configs are
    # honestly skipped by the anchor guard (no anchor, no flow).
    _write(tmp_path, "blocks/chatwoot/executeChatwoot.ts", """
        export const executeChatwoot = (options) => {
          return options;
        };
    """)
    _write(tmp_path, "blocks/pixel/executePixel.ts", """
        export const executePixel = (options) => {
          return options;
        };
    """)
    _write(tmp_path, "utils/executeActions.ts", """
        import { executeChatwoot } from '../blocks/chatwoot/executeChatwoot';
        import { executePixel } from '../blocks/pixel/executePixel';
        export const run = async ({ action }) => {
          if ("chatwoot" in action) {
            return executeChatwoot(action);
          }
          if ("pixel" in action) {
            return executePixel(action);
          }
        };
    """)
    _write(tmp_path, "store/qb/config.ts", "export default { id: 1 };\n")
    _write(tmp_path, "store/xero/config.ts", "export default { id: 2 };\n")
    _write(tmp_path, "store/index.ts", """
        import qbApp from "./qb/config";
        import xeroApp from "./xero/config";
        export const apps = [
          qbApp,
          xeroApp,
        ];
    """)
    files = ["blocks/chatwoot/executeChatwoot.ts",
             "blocks/pixel/executePixel.ts", "utils/executeActions.ts",
             "store/qb/config.ts", "store/xero/config.ts", "store/index.ts"]
    targets = detect_ts_registries(tmp_path, files)
    fwf = _fwf("integrations", files)
    tele = mint_dispatch_seeds([fwf], targets, tmp_path)
    names = sorted(f.name for f in fwf.flows)
    assert names == ["run-execute-chatwoot-flow", "run-execute-pixel-flow"]
    assert tele["skipped_no_anchor"] == 2  # the exportless configs
    assert tele["ordinal_fallback"] == 0
