"""Deterministic ($0) validation harness for the Monorepo Assembly View.

Re-projects EXISTING cold-scan JSON (~/.faultline/cold/<slug>.json) + the
on-disk clone manifests through build_monorepo_assembly and prints a
per-repo report: projects, edges, fan-in top-3, % features assigned,
unassigned/spanning counts, and a hard conservation check.

NOT a test (lives outside tests/); a one-shot reporter run by the architect
during validation. No LLM, no network, no scan.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from faultline.models.types import Feature
from faultline.pipeline_v2.stage_0_intake import ScanContext, Workspace
from faultline.pipeline_v2.stage_6_6_monorepo_assembly import build_monorepo_assembly

COLD = Path.home() / ".faultline" / "cold"

# slug -> (cold json slug, clone dir override or None to use repo_path)
TARGETS: dict[str, tuple[str, str | None]] = {
    "twenty": ("twenty", None),
    "dub": ("dub", None),
    "supabase": ("supabase", None),
    # cal-com's scan used a /tmp clone now gone; point at the persistent clone.
    "cal-com": ("cal-com", "/Users/pkuzina/workspace/_faultlines-testrepos/cal.com"),
    "formbricks": ("formbricks", None),
    "infisical": ("infisical", None),
    "inbox-zero": ("inbox-zero", None),
}


def _features_from_json(d: dict) -> list[Feature]:
    raw = d.get("developer_features") or d.get("features") or []
    feats: list[Feature] = []
    for r in raw:
        feats.append(
            Feature(
                name=r.get("name", ""),
                paths=list(r.get("paths") or []),
                authors=[],
                total_commits=0,
                bug_fixes=0,
                bug_fix_ratio=0.0,
                last_modified=datetime.now(tz=timezone.utc),
                health_score=100.0,
                uuid=r.get("uuid") or f"synthetic-{len(feats)}",
                member_files=r.get("member_files") or [],
            )
        )
    return feats


def _ctx_from_json(d: dict, clone: str) -> ScanContext:
    ws_meta = (d.get("scan_meta") or {}).get("workspaces") or []
    workspaces = [
        Workspace(
            name=w.get("name", ""),
            path=w.get("path", ""),
            package_json=None,  # assembly re-reads from disk
            stack=w.get("stack"),
            files=[],
        )
        for w in ws_meta
    ]
    return ScanContext(
        repo_path=Path(clone),
        stack=d.get("scan_meta", {}).get("stack"),
        monorepo=bool(workspaces),
        workspaces=workspaces or None,
        tracked_files=[],
        commits=[],
    )


def _report(slug: str, json_slug: str, clone_override: str | None) -> dict:
    p = COLD / f"{json_slug}.json"
    if not p.exists():
        return {"slug": slug, "error": "no cold scan"}
    d = json.load(p.open())
    clone = clone_override or d.get("repo_path")
    if not clone or not Path(clone).is_dir():
        return {"slug": slug, "error": f"clone missing: {clone}"}

    feats = _features_from_json(d)
    ctx = _ctx_from_json(d, clone)
    view = build_monorepo_assembly(ctx, feats)

    if not view.get("is_monorepo"):
        return {"slug": slug, "is_monorepo": False, "feature_total": len(feats)}

    stats = view["stats"]
    nodes = view["cross_project_graph"]["nodes"]
    edges = view["cross_project_graph"]["edges"]
    units = [p for p in view["projects"] if p["type"] in ("app", "service")]
    fan_top3 = sorted(nodes, key=lambda n: -n["fan_in"])[:3]

    # Hard conservation check.
    seen: list[str] = []
    for proj in view["projects"]:
        seen.extend(proj["feature_uuids"])
    seen.extend(u["uuid"] for u in view["unassigned_features"])
    conserved = sorted(seen) == sorted(f.uuid for f in feats) and len(seen) == len(set(seen))

    return {
        "slug": slug,
        "is_monorepo": True,
        "clone": clone,
        "feature_total": stats["feature_total"],
        "project_count": stats["project_count"],
        "unit_count": len(units),
        "edge_count": stats["edge_count"],
        "assigned_pct": stats["assigned_pct"],
        "unassigned": stats["unassigned"],
        "spanning": stats["spanning"],
        "conserved": conserved,
        "fan_top3": [(n["name"], n["subpath"], n["fan_in"]) for n in fan_top3],
        "top_units": [
            (p["name"], p["subpath"], p["feature_count"], p["loc"])
            for p in units[:6]
        ],
        "sample_edges": [
            (e["from"], "->", e["to"], e["ecosystem"]) for e in edges[:8]
        ],
    }


def main() -> None:
    only = sys.argv[1:] or list(TARGETS)
    for slug in only:
        if slug not in TARGETS:
            print(f"!! unknown target {slug}")
            continue
        json_slug, clone = TARGETS[slug]
        r = _report(slug, json_slug, clone)
        print("=" * 72)
        print(json.dumps(r, indent=2, default=str))


if __name__ == "__main__":
    main()
