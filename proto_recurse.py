"""Throwaway prototype: validate deep-recursion domain detection on cold data.

NOT shipped. Proves the directory logic produces REAL domains (not junk
buckets) on caddy / inbox-zero / formbricks before writing the production
module. $0, deterministic, reads cold feature/file structure only.
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
from collections import defaultdict

# Pure layer/infra/test directory segments that must NOT mint a feature on
# their own (no domain identity). Reused vocabulary spirit from lever A
# (_DEOWN_SCAFFOLD_SEGMENTS) + test/build/infra. Structural, corpus-free.
_NON_DOMAIN_SEGMENTS = frozenset({
    # shared scaffold (lever A subset)
    "lib", "libs", "util", "utils", "helper", "helpers", "hook", "hooks",
    "type", "types", "constant", "constants", "config", "configs",
    "style", "styles", "shared", "common",
    # layer dirs (architectural, not domain)
    "src", "app", "pages", "components", "ui", "api", "server", "client",
    "core", "internal", "pkg", "cmd", "public", "static", "assets",
    "models", "model", "schemas", "schema", "views", "view", "controllers",
    "controller", "services", "service", "handlers", "handler", "routes",
    "router", "routers", "store", "stores", "providers", "provider",
    "middleware", "middlewares", "i18n", "intl", "locale", "locales",
    "styles", "css", "scss", "images", "img", "fonts", "icons",
    # test / build / infra
    "test", "tests", "__tests__", "spec", "specs", "e2e", "fixtures",
    "mocks", "__mocks__", "node_modules", "dist", "build", "out",
    "coverage", "vendor", "prisma", "migrations", "generated", "gen",
    "docs", "doc", "examples", "example",
})

_LEAF_DIRS_FOR_GROUP = frozenset()  # placeholder

_DEPTH_CAP = 4
_MIN_CHILDREN = 2


def _dir(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _common_dir(paths: list[str]) -> str:
    """Longest common DIRECTORY prefix (segment-aligned)."""
    if not paths:
        return ""
    split = [p.split("/") for p in paths]
    common: list[str] = []
    for segs in zip(*split):
        first = segs[0]
        if all(s == first for s in segs):
            common.append(first)
        else:
            break
    # common may include the filename if all paths identical; drop trailing
    # non-dir by checking: the common prefix is a dir prefix only up to the
    # last fully-shared segment that is followed by more segments somewhere.
    # Simplest: if every path == common joined, it's a single file; return its dir.
    cp = "/".join(common)
    if cp and any(len(s) > len(common) for s in split):
        return cp
    return _dir(cp)


def _strip_parens(seg: str) -> str:
    """Route-group folder (x) -> x; also (.)group -> group."""
    m = re.fullmatch(r"\((.*)\)", seg)
    return m.group(1) if m else seg


def _is_non_domain(seg: str) -> bool:
    return _strip_parens(seg).lower() in _NON_DOMAIN_SEGMENTS


def plan_split(owned: list[str], floor: int) -> tuple[dict[str, list[str]], list[str]]:
    """Recurse from common-prefix dir to the first level with >=2
    floor-clearing DOMAIN child dirs. Return {domain_key: files}, residual.
    """
    residual: list[str] = []
    base = _common_dir(owned)
    base_segs = [s for s in base.split("/") if s]
    # Files at/above base that have no deeper dir become residual immediately.
    pool = list(owned)

    level = len(base_segs)
    cur_prefix = base
    while level - len(base_segs) <= _DEPTH_CAP:
        # group pool by the child dir segment at index `level`
        groups: dict[str, list[str]] = defaultdict(list)
        flat: list[str] = []  # files that END at this level (no child dir)
        for p in pool:
            segs = p.split("/")
            if len(segs) <= level + 1:
                # file lives directly in cur_prefix (no child dir) -> flat
                flat.append(p)
            else:
                groups[segs[level]].append(p)
        # promotable DOMAIN children: >=floor files AND a domain name
        domain_children = {
            seg: files for seg, files in groups.items()
            if len(files) >= floor and not _is_non_domain(seg)
        }
        if len(domain_children) >= _MIN_CHILDREN:
            # SPLIT here
            out: dict[str, list[str]] = {}
            for seg, files in groups.items():
                key = (cur_prefix + "/" + seg) if cur_prefix else seg
                if seg in domain_children:
                    out[key] = files
                else:
                    residual.extend(files)  # non-domain or sub-floor child
            residual.extend(flat)
            return out, residual
        # not enough domain children at this level
        substantial = {
            seg: files for seg, files in groups.items() if len(files) >= floor
        }
        if len(substantial) == 1:
            # single substantial child (domain OR layer dir) -> descend into it
            only_seg = next(iter(substantial))
            # everything else (flat + other small groups) -> residual
            residual.extend(flat)
            for seg, files in groups.items():
                if seg != only_seg:
                    residual.extend(files)
            pool = substantial[only_seg]
            cur_prefix = (cur_prefix + "/" + only_seg) if cur_prefix else only_seg
            level += 1
            continue
        # 0 substantial children -> cannot decompose
        residual.extend(pool)
        return {}, residual
    residual.extend(pool)
    return {}, residual


def owned(f):
    mf = f.get("member_files")
    if mf and isinstance(mf[0], dict):
        o = [m["path"] for m in mf if (m.get("primary") or m.get("role") in ("anchor", "owner"))]
        if o:
            return o
    return f.get("paths") or []


def leaf_name(domain_key: str) -> str:
    seg = domain_key.rsplit("/", 1)[-1]
    return _strip_parens(seg).replace("_", "-").lower()


for slug in ["caddy", "inbox-zero", "formbricks"]:
    d = json.load(open(os.path.expanduser(f"~/.faultline/cold/{slug}.json")))
    feats = d.get("developer_features") or d.get("features") or []
    sizes = [len(owned(f)) for f in feats if owned(f)]
    median = max(2, int(statistics.median(sizes))) if sizes else 2
    total_owned = len(set(p for f in feats for p in owned(f)))
    oversized_cut = max(2 * median, math.ceil(0.15 * total_owned))
    floor = max(2, median)
    print(f"\n===== {slug}: median_owned={median} total_owned={total_owned} "
          f"oversized_cut={oversized_cut} floor={floor}")
    # blob before
    osets = [(set(owned(f)), f.get("name")) for f in feats]
    osets = [(s, n) for s, n in osets if s]
    tot = len(set().union(*[s for s, _ in osets])) or 1
    before_max = max(len(s) for s, _ in osets) / tot
    before_big = max(osets, key=lambda x: len(x[0]))[1]
    print(f"  BEFORE owned_max={before_max:.3f} ({before_big})")
    # apply to oversized features
    for f in feats:
        o = owned(f)
        if len(o) <= oversized_cut:
            continue
        domains, residual = plan_split(o, floor)
        if not domains:
            print(f"  [oversized {f.get('name')} owned={len(o)}] -> NO SPLIT")
            continue
        names = [leaf_name(k) for k in domains]
        print(f"  [{f.get('name')} owned={len(o)}] -> {len(domains)} subs, "
              f"residual={len(residual)}")
        print(f"     names: {sorted(names)[:20]}")
