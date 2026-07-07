"""Product-Spine W3.1 — D4 vendor-husk floor (fb3 dossier / valsem4 H9).

midday shipped 27 app-store husk PFs (logo.tsx + config.ts, 27-34 LOC,
zero journeys) because the W2b hub-child mint bar accepted ANY code
file as evidence. The amendment's intent ("flowful OR substantial, else
fold under the hub core as dev-children") now has a floor: a flowless
child needs >= 150 owned LOC (the valsem4 H9 calibration bound — zero
false positives across 13 scans; google 286 / microsoft 557 / Soc0
sentinelone 1,258 spared). The comp `aws` + `aws-(integration)`
same-vendor dup-pair dies with it — the 0-LOC twin never mints, so the
pair never exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.models.types import Feature, Flow, MemberFile
from faultline.pipeline_v2.stage_6_86_anchored_mint import (
    run_anchored_mint,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def flow(name: str, entry: str) -> Flow:
    return Flow(
        name=name, entry_point_file=entry, paths=[entry], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0, last_modified=_NOW,
        health_score=100.0,
    )


def dev(name: str, paths: list[str], flows: list[Flow] | None = None,
        **kw) -> Feature:
    return Feature(
        name=name, paths=list(paths),
        member_files=[
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
            for p in paths
        ],
        flows=flows or [], product_feature_id="old-pf",
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, **kw,
    )


def _write(root: Path, rel: str, lines: int) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"const x{i} = {i};\n" for i in range(lines)))


def _app_store(tmp_path: Path):
    """midday app-store distilled: 3 husk vendors (logo + config) + one
    substantial vendor with real code, under packages/app-store/src."""
    root = tmp_path / "repo"
    devs = []
    for vendor in ("dropbox", "notion", "github"):
        files = [
            f"packages/app-store/src/{vendor}/assets/logo.tsx",
            f"packages/app-store/src/{vendor}/config.ts",
        ]
        for rel in files:
            _write(root, rel, 15)  # 30 LOC total — under the floor
        devs.append(dev(vendor, files))
    slack_files = [
        f"packages/app-store/src/slack/{n}.ts"
        for n in ("client", "sync", "webhook", "config")
    ]
    for rel in slack_files:
        _write(root, rel, 60)  # 240 LOC — above the floor
    devs.append(dev("slack-app", slack_files))
    ws = [SimpleNamespace(name="app-store", path="packages/app-store",
                          stack="ts")]
    ctx = SimpleNamespace(workspaces=ws, tracked_files=[],
                          repo_path=root, monorepo=True)
    return devs, ctx


def test_husk_children_never_mint_and_fold_under_the_family(tmp_path):
    routes = [{"pattern": "/apps", "method": "PAGE",
               "file": "apps/dashboard/src/app/apps/page.tsx"}]
    devs, ctx = _app_store(tmp_path)
    apps_page = dev("apps", ["apps/dashboard/src/app/apps/page.tsx"],
                    flows=[flow("browse-apps-flow",
                                "apps/dashboard/src/app/apps/page.tsx")])
    pfs, tele = run_anchored_mint([*devs, apps_page], routes, ctx)
    names = {p.name for p in pfs}
    for husk in ("dropbox", "notion", "github"):
        assert husk not in names, (
            f"D4 REGRESSION: husk vendor PF {husk!r} minted "
            f"(the midday app-store ×27 class)")
    # the substantial vendor still mints (google/microsoft class)
    assert "slack" in names
    # husk devs FOLD (a PF home or the honest lane — never a husk PF)
    for d in devs[:3]:
        assert d.product_feature_id != d.name
    assert tele.get("fold_hub_parent", 0) >= 1 or all(
        d.product_feature_id is None for d in devs[:3])


def test_flowful_husk_still_mints(tmp_path):
    """The floor applies to FLOWLESS children only — a tiny child whose
    journeys land in it is a real integration surface."""
    devs, ctx = _app_store(tmp_path)
    tiny = devs[0]  # dropbox, 30 LOC
    tiny.flows = [flow("connect-dropbox-flow",
                       "packages/app-store/src/dropbox/config.ts")]
    pfs, _tele = run_anchored_mint(devs, [], ctx)
    assert "dropbox" in {p.name for p in pfs}
