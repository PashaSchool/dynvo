"""Prototype v2: layer-transparent recursion.

Key change vs v1: a NON-DOMAIN layer dir (app/, modules/, src/, services/...)
is TRANSPARENT — we recurse THROUGH it to find the domain segment beneath,
instead of treating it as residual. The domain of a file = the FIRST path
segment (below the common prefix) that is NOT a non-domain layer/infra/test
segment. Route-group parens stripped. Files with no domain segment -> residual.
Then apply grain floor + container floor on the resulting domain buckets.
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
from collections import defaultdict

_NON_DOMAIN_SEGMENTS = frozenset({
    "lib", "libs", "util", "utils", "helper", "helpers", "hook", "hooks",
    "type", "types", "constant", "constants", "config", "configs",
    "style", "styles", "shared", "common",
    "src", "app", "apps", "pages", "components", "ui", "api", "server",
    "client", "core", "internal", "pkg", "cmd", "public", "static",
    "assets", "models", "model", "schemas", "schema", "views", "view",
    "controllers", "controller", "services", "service", "handlers",
    "handler", "routes", "route", "router", "routers", "store", "stores",
    "providers", "provider", "middleware", "middlewares", "i18n", "intl",
    "locale", "locales", "css", "scss", "images", "img", "fonts", "icons",
    "test", "tests", "__tests__", "spec", "specs", "e2e", "fixtures",
    "mocks", "__mocks__", "node_modules", "dist", "build", "out",
    "coverage", "vendor", "prisma", "migrations", "generated", "gen",
    "docs", "doc", "examples", "example", "scripts", "script", "bin",
    "modules", "module", "features", "feature", "domains", "domain",
    "packages", "plugins", "plugin", "integrations", "integration",
    "screens", "screen", "resources", "resource", "agents", "agent",
    "web", "frontend", "backend", "studio", "dashboard", "admin",
})

_DEPTH_CAP = 6


def _strip_parens(seg: str) -> str:
    m = re.fullmatch(r"\((.*)\)", seg)
    return m.group(1) if m else seg


def _is_non_domain(seg: str) -> bool:
    s = _strip_parens(seg).lower()
    return s in _NON_DOMAIN_SEGMENTS or s.startswith(".") or s.startswith("_")


def _common_segs(paths: list[str]) -> int:
    if not paths:
        return 0
    split = [p.split("/") for p in paths]
    n = 0
    for segs in zip(*split):
        if all(s == segs[0] for s in segs) and any(len(sp) > n + 1 for sp in split):
            n += 1
        else:
            break
    return n


def domain_of(path: str, start: int) -> str | None:
    """First DOMAIN segment at-or-after index `start` (skipping non-domain
    layer/infra dirs), provided a deeper segment exists (it's a dir).
    Returns the full domain-key prefix (so siblings stay distinct)."""
    segs = path.split("/")
    i = start
    depth = 0
    while i < len(segs) - 1 and depth < _DEPTH_CAP:
        seg = segs[i]
        if not _is_non_domain(seg):
            return "/".join(segs[: i + 1])
        i += 1
        depth += 1
    return None


def plan_split(owned: list[str], floor: int) -> tuple[dict[str, list[str]], list[str]]:
    start = _common_segs(owned)
    raw: dict[str, list[str]] = defaultdict(list)
    residual: list[str] = []
    for p in owned:
        k = domain_of(p, start)
        if k is None:
            residual.append(p)
        else:
            raw[k].append(p)
    promotable = {k: f for k, f in raw.items() if len(f) >= floor}
    if len(promotable) < 2:
        return {}, owned  # not a real decomposition
    for k, files in raw.items():
        if k not in promotable:
            residual.extend(files)
    return promotable, residual


def owned(f):
    mf = f.get("member_files")
    if mf and isinstance(mf[0], dict):
        o = [m["path"] for m in mf if (m.get("primary") or m.get("role") in ("anchor", "owner"))]
        if o:
            return o
    return f.get("paths") or []


def leaf_name(domain_key: str) -> str:
    return _strip_parens(domain_key.rsplit("/", 1)[-1]).replace("_", "-").lower()


for slug in ["caddy", "inbox-zero", "formbricks"]:
    d = json.load(open(os.path.expanduser(f"~/.faultline/cold/{slug}.json")))
    feats = d.get("developer_features") or d.get("features") or []
    sizes = [len(owned(f)) for f in feats if owned(f)]
    median = max(2, int(statistics.median(sizes))) if sizes else 2
    osets0 = [(set(owned(f)), f.get("name")) for f in feats]
    osets0 = [(s, n) for s, n in osets0 if s]
    total_owned = len(set().union(*[s for s, _ in osets0]))
    oversized_cut = max(2 * median, math.ceil(0.15 * total_owned))
    floor = max(2, median)
    tot = total_owned or 1
    before_max = max(len(s) for s, _ in osets0) / tot
    before_big = max(osets0, key=lambda x: len(x[0]))[1]
    print(f"\n===== {slug}: median={median} total_owned={total_owned} "
          f"cut={oversized_cut} floor={floor}  BEFORE owned_max={before_max:.3f} ({before_big})")
    # simulate full redistribution to recompute owned_max after
    new_owned: list[tuple[set, str]] = []
    for f in feats:
        o = owned(f)
        if len(o) <= oversized_cut:
            if o:
                new_owned.append((set(o), f.get("name")))
            continue
        domains, residual = plan_split(o, floor)
        if not domains:
            print(f"  [{f.get('name')} owned={len(o)}] -> NO SPLIT")
            if o:
                new_owned.append((set(o), f.get("name")))
            continue
        names = [leaf_name(k) for k in domains]
        print(f"  [{f.get('name')} owned={len(o)}] -> {len(domains)} subs residual={len(residual)}")
        print(f"     names: {sorted(set(names))[:25]}")
        # source keeps residual (as owned for metric purposes it becomes shared,
        # but for owned_max simulation residual stays owned on source minus moved)
        if residual:
            new_owned.append((set(residual), f.get("name") + "[resid]"))
        for k, files in domains.items():
            new_owned.append((set(files), leaf_name(k)))
    # NOTE: real impl de-owns residual to shared; here approximate by counting
    # residual as still-owned (upper bound on after_max).
    new_tot = len(set().union(*[s for s, _ in new_owned])) or 1
    after_max = max(len(s) for s, _ in new_owned) / new_tot
    after_big = max(new_owned, key=lambda x: len(x[0]))[1]
    print(f"  AFTER owned_max={after_max:.3f} ({after_big})  [residual still-owned upper bound]")
