"""Product-Spine §4.3 (Wave 2b) — the anchor-candidate builder.

A **spine anchor** is a named, author-declared subtree of the repository
that a product capability can live in. PF candidates come ONLY from
these ranked sources (spec §4.3 + the 2026-07-06 calibration verdict,
GO θ=0.5) — an LLM never invents product-feature membership again:

  * ``route``      — route subtrees from the deterministic
                     ``routes_index``: filesystem routers contribute the
                     first meaningful URL segment's directory (route
                     groups / dynamic params / ``api``/``trpc`` always
                     transparent, ``v1``/``v2`` transparent when deeper
                     segments exist) at TWO grains — the top segment and
                     collection-descend children (``…/teams/[id]/docs``
                     → ``docs``, ≤3 hops; the papermark/supabase
                     mega-segment trap); central routers (FastAPI et
                     al.) contribute the handler FILE keyed by the first
                     meaningful URL segment.
  * ``ws-pkg`` /    — workspace packages from Stage-0 intake (plus
    ``ws-app``       manifest-dir unit discovery for repos without a
                     declared workspace list — the Soc0 backend/frontend
                     class). ``ws-app`` units are DEPLOYMENT SHELLS
                     (rider R1): they classify lineage but never mint.
                     Only ``example``-class workspaces are excluded —
                     ``type=tool`` holds real product surfaces (midday
                     ``packages/cli``, the published-CLI doctrine).
  * ``schema``      — schema-domain names (Stage-1 schema extractor +
                     ``models/``-dir file stems) NAME-MATCHED against
                     dirs and file stems repo-wide, guarded by the
                     STRUCTURAL-VOCABULARY STOPLIST (the calibration
                     trap: drizzle table ``apps`` claimed the ``apps/``
                     root at share 1.0) and the ``_singular`` js/us/is/
                     os/ss guards (``nextjs``→``nextj`` class).
  * ``fdir``        — authored feature-dirs ``<features|modules>/<domain>/``
                     (rider R1: papermark ``ee/features/*``, Soc0
                     ``frontend/src/features/*``) — explicit author
                     capability declarations; mint-eligible on their own.
  * ``svc``         — domain-service-dirs ``<services|service>/<domain>/``
                     (rider R1). LINEAGE-ONLY: a service dir never
                     standalone-mints (operator case: Soc0
                     ``widget-query``); it widens a same-key capability
                     via the cross-source merge, or its devs take the
                     fold ladder.
  * ``hub-vendor`` /— per-vendor connector grain (operator amendment
    ``hub-core``     2026-07-06, REPLACES the W1 children-inherit-hub
                     rule): a dir with ≥3 distinct vendor-named children
                     is a hub FAMILY; every vendor child (child dir, or
                     the token-matched file set for file-per-vendor
                     hubs) is its OWN anchor ``hub:<dir>/<vendor>``; the
                     plumbing remainder is the ``hub:<dir>`` core anchor.

Nav labels are RANKING CONFIRMERS only (the calibration measured nav
adds zero new subtrees — it confirms route keys), i18n namespaces are
deferred (measured pooled delta ≈ 0). Cross-source anchors are merged
by normalized key (route ``cases`` + schema ``case`` = ONE anchor);
workspace anchors join a key-merge only when their basename is unique
among workspaces (the ``e2e/studio`` vs ``apps/studio`` basename-
collision trap — identity is always the full relative path).

Deterministic, $0 LLM, scale-invariant; vocabulary lives in
``data/spine-anchor-vocab.yaml`` (authoring copy
``eval/spine-anchor-vocab.yaml``, drift-guarded).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from faultline.pipeline_v2.data import load_yaml
from faultline.pipeline_v2.naming_validator import _singular, _split_tokens
from faultline.pipeline_v2.hub_relation import vendor_of_segment

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

__all__ = [
    "SpineAnchor",
    "SOURCE_RANK",
    "build_spine_anchors",
    "normalize_anchor_key",
    "owned_paths_of",
    "load_spine_vocab",
]

# Fixed source rank for near-tie resolution (calibration §F: "10pp
# near-tie → fixed source-rank"). Lower = wins. Vendor grain outranks
# everything (the relation is explicit); routes are the finest product
# truth; author-declared dirs next; schema and workspace packages are
# coarser evidence.
SOURCE_RANK: dict[str, int] = {
    "hub-vendor": 0,
    "hub-core": 1,
    "route": 2,
    "fdir": 3,
    "svc": 4,
    "schema": 5,
    "ws-pkg": 6,
    "ws-app": 7,
}

_VOCAB_FILE = "spine-anchor-vocab.yaml"
_vocab_cache: dict[str, Any] | None = None


def load_spine_vocab() -> dict[str, Any]:
    """Packaged vocabulary (cached — pure data, read once per process)."""
    global _vocab_cache
    if _vocab_cache is None:
        _vocab_cache = load_yaml(_VOCAB_FILE)
    return _vocab_cache


# ── Key normalisation (shared by every source) ──────────────────────────


def _singular_guarded(word: str) -> str:
    """``_singular`` plus the calibration's ``js``/``os`` guards: the
    house singularizer protects ``ss/us/is`` (status/focus/analysis) but
    still strips ``nextjs``→``nextj`` and ``macos``→``maco`` — measured
    trap #2 (2026-07-06). Guarded HERE, not in ``naming_validator`` —
    changing the global helper would churn every name digest repo-wide."""
    if word.endswith(("js", "os")):
        return word
    return _singular(word)


def normalize_anchor_key(raw: str) -> str:
    """camel/snake/kebab split → lowercase kebab, last token singularised
    with the guarded singularizer (status→status, nextjs→nextjs)."""
    toks = _split_tokens(raw)
    if not toks:
        return ""
    # _split_tokens returns an ordered list of lowercase tokens.
    toks[-1] = _singular_guarded(toks[-1])
    return "-".join(toks)


def _stem(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[:dot] if dot > 0 else base


def owned_paths_of(f: Any) -> list[str]:
    """A dev feature's OWNED file set — primary member_files (the honest
    claim ledger) with ``paths`` as the legacy fallback. Mirrors the
    calibration's population definition exactly."""
    mfs = getattr(f, "member_files", None) or []
    owned = []
    for m in mfs:
        primary = m.get("primary") if isinstance(m, dict) else getattr(m, "primary", False)
        p = m.get("path") if isinstance(m, dict) else getattr(m, "path", None)
        if primary and p:
            owned.append(str(p))
    if owned:
        return owned
    return [str(p) for p in (getattr(f, "paths", None) or []) if p]


# ── Anchor dataclass ─────────────────────────────────────────────────────


@dataclass
class SpineAnchor:
    """One PF candidate: a named node over an existing L1 subtree."""

    canonical_id: str                      # "route:/settings", "ws:apps/web", …
    key: str                               # normalized merge key
    source: str                            # primary source class (min rank)
    display: str                           # display label (Wave-3 polishes)
    prefixes: tuple[str, ...] = ()         # dir-subtree membership
    files: frozenset[str] = frozenset()    # exact-file membership
    exclude_prefixes: tuple[str, ...] = () # carve-outs (hub core minus children)
    exclude_files: frozenset[str] = frozenset()
    sources: frozenset[str] = frozenset()  # every merged source class
    nav_confirmed: bool = False
    page_route_files: frozenset[str] = frozenset()
    api_route_files: frozenset[str] = frozenset()
    barred: str | None = None              # never-mint reason (single_letter | version_dir)
    hub_dir: str | None = None             # hub-family membership
    vendor: str | None = None              # hub-vendor child
    route_groups: frozenset[str] = frozenset()
    depth: int = 0                         # collection-descend depth
    # Hub child under a GENERIC/structural family parent (backend/routers,
    # backend/models): the container itself is never a capability (no core
    # anchor) and the child needs FLOW evidence to mint — a vendor-named
    # model/router file alone is not an integration PF.
    hub_parent_generic: bool = False

    @property
    def rank(self) -> int:
        return min((SOURCE_RANK.get(s, 99) for s in (self.sources or {self.source})),
                   default=SOURCE_RANK.get(self.source, 99))

    @property
    def shell(self) -> bool:
        return self.source == "ws-app"

    def matches(self, path: str) -> bool:
        """Subtree membership test for one repo-relative path."""
        if path in self.exclude_files:
            return False
        for ex in self.exclude_prefixes:
            if path.startswith(ex + "/") or path == ex:
                return False
        if path in self.files:
            return True
        for pre in self.prefixes:
            if path.startswith(pre + "/") or path == pre:
                return True
        return False

    def match_count(self, paths: Iterable[str]) -> int:
        return sum(1 for p in paths if self.matches(p))

    def matched_set(self, paths: Iterable[str]) -> frozenset[str]:
        return frozenset(p for p in paths if self.matches(p))


# ── Route anchors ────────────────────────────────────────────────────────

_GROUP_RE = re.compile(r"^\(.*\)$")
_DYNAMIC_RE = re.compile(r"^\[+\.{0,3}[^\]]*\]+$|^[:{]")
_PAGE_MARKERS = {"page", "layout", "index", "+page", "default", "route", "+server"}
_MAX_DESCEND = 3

# Routing-root component runs (mirrors indexes._ROUTE_ROOT_SEQS): the
# URL tree of a FILESYSTEM router begins after one of these; a key
# segment may only be located in dirs AFTER it. Without this constraint
# the ``/apps`` route on midday located the WORKSPACE ROOT ``apps/`` dir
# and a phantom mega-anchor swallowed the monorepo at share 1.0 — the
# same trap class the calibration caught for schema names (trap #1),
# reproduced live on the route side (2026-07-06 smoke).
_ROUTE_ROOT_SEQS: tuple[tuple[str, ...], ...] = (
    ("app", "routes"),
    ("src", "routes"),
    ("src", "app"),
    ("src", "pages"),
    ("app",),
    ("pages",),
    ("routes",),
)


def _route_root_end(segs: list[str]) -> int | None:
    """Index AFTER the first routing-root run, or ``None`` (central
    router — the handler FILE is the subtree)."""
    for i in range(len(segs)):
        for seq in _ROUTE_ROOT_SEQS:
            if tuple(segs[i:i + len(seq)]) == seq:
                return i + len(seq)
    return None


def _is_version_seg(seg: str, version_re: re.Pattern[str]) -> bool:
    return bool(version_re.match(seg.lower()))


def _pattern_key_chain(
    pattern: str,
    vocab: dict[str, Any],
    version_re: re.Pattern[str],
) -> list[str]:
    """Meaningful URL segments of a route pattern: the top segment plus
    collection-descend children (≤3 dynamic hops between). Route groups
    are already URL-invisible; params / ``api``/``trpc`` / version segs
    (when deeper segments exist) are transparent."""
    transparent = set(vocab.get("route_transparent_segments") or [])
    segs = [s for s in (pattern or "").split("/") if s]
    chain: list[str] = []
    hops = 0
    for i, seg in enumerate(segs):
        low = seg.lower()
        if _DYNAMIC_RE.match(seg):
            if chain:
                hops += 1
                if hops > _MAX_DESCEND:
                    break
            continue
        if seg.startswith("_"):
            # Convention-private / pathless-layout segments (TanStack
            # ``_layout``, Remix ``_index``) organise files, not URLs.
            continue
        if low in transparent:
            continue
        if _is_version_seg(low, version_re) and i < len(segs) - 1:
            continue
        chain.append(seg)
        hops = 0
        if len(chain) > _MAX_DESCEND:
            break
    return chain


def _route_chain(
    file_path: str,
    pattern: str,
    vocab: dict[str, Any],
    version_re: re.Pattern[str],
) -> list[tuple[str, str | None, int]]:
    """Anchor chain for one route entry — PATTERN-driven (the URL is the
    author's product declaration; the file path only LOCATES the subtree).

    For each meaningful pattern segment, the subtree is the file-path dir
    of the SAME name when one exists (filesystem routers — Next/Remix/
    SvelteKit: ``/settings/billing`` → ``…/settings/billing/`` dirs);
    otherwise the route FILE itself (central routers — FastAPI
    ``routes/items.py`` for ``/api/v1/items``; Pages-Router leaf files).
    Returns ``[(key_segment, dir_prefix_or_None, depth), …]``; a
    ``None`` prefix means an exact-file anchor. The old file-path-first
    derivation minted a phantom ``routes``-dir mega-anchor on central
    routers (fastapi-template smoke, 2026-07-06) — pattern-first kills
    that class.
    """
    keys = _pattern_key_chain(pattern, vocab, version_re)
    segs = [s for s in file_path.replace("\\", "/").split("/") if s]
    if not segs:
        return []
    dirs = segs[:-1]
    leaf_key = normalize_anchor_key(_stem(segs[-1]))

    # A key dir may only be located AFTER the routing root — otherwise
    # a workspace-root dir of the same name becomes a phantom
    # mega-anchor (midday ``apps/`` vs the ``/apps`` route). Central
    # routers (no routing root) never dir-locate: the handler file IS
    # the subtree.
    root_end = _route_root_end(segs)

    chain: list[tuple[str, str | None, int]] = []
    search_from = root_end if root_end is not None else len(dirs)
    for depth, key_seg in enumerate(keys):
        want = normalize_anchor_key(key_seg)
        prefix: str | None = None
        for j in range(search_from, len(dirs)):
            seg = dirs[j]
            # Route groups / dynamic params are URL-INVISIBLE — they can
            # never BE the key dir (midday: the ``(app)`` group dir
            # normalized equal to the ``/apps`` key and truncated the
            # prefix one level short). They still ride INSIDE the prefix.
            if _GROUP_RE.match(seg) or _DYNAMIC_RE.match(seg):
                continue
            if normalize_anchor_key(seg) == want:
                prefix = "/".join(segs[: j + 1])
                search_from = j + 1
                break
        chain.append((key_seg, prefix, depth))
    if not chain:
        # Pattern had no meaningful segment (``/`` root, pure-param) —
        # fall back to the leaf file stem when it is a real name.
        stem = _stem(segs[-1])
        if (stem and stem not in _PAGE_MARKERS and not stem.startswith("_")
                and leaf_key):
            chain.append((stem, None, 0))
    return chain


def _bar_reason(key: str, version_re: re.Pattern[str]) -> str | None:
    """Never-mint key classes (calibration traps): single-letter route
    keys (midday i/p/r/s) and version-dir keys (linkwarden v1/v2)."""
    alnum = re.sub(r"[^a-z0-9]+", "", key.lower())
    if len(alnum) <= 1:
        return "single_letter"
    if version_re.match(alnum):
        return "version_dir"
    return None


def _build_route_anchors(
    routes_index: list[dict[str, Any]],
    vocab: dict[str, Any],
) -> list[SpineAnchor]:
    version_re = re.compile(vocab.get("version_segment_pattern") or r"^v\d+$")
    # (key, prefix-or-None) → accumulation
    acc: dict[tuple[str, str | None], dict[str, Any]] = {}

    def _add(seg: str, prefix: str | None, depth: int, file: str,
             method: str, groups: list[str]) -> None:
        key = normalize_anchor_key(seg)
        if not key:
            return
        slot = acc.setdefault((key, prefix), {
            "seg": seg, "files": set(), "page": set(), "api": set(),
            "groups": set(), "depth": depth,
        })
        slot["files"].add(file)
        slot["groups"].update(groups)
        if (method or "").upper() == "PAGE":
            slot["page"].add(file)
        else:
            slot["api"].add(file)

    for entry in routes_index or []:
        file = str(entry.get("file") or "")
        if not file:
            continue
        method = str(entry.get("method") or "GET")
        groups = [str(g) for g in (entry.get("route_groups") or [])]
        chain = _route_chain(
            file, str(entry.get("pattern") or ""), vocab, version_re)
        for seg, prefix, depth in chain:
            _add(seg, prefix, depth, file, method, groups)

    out: list[SpineAnchor] = []
    for (key, prefix) in sorted(acc, key=lambda kp: (kp[0], kp[1] or "")):
        slot = acc[(key, prefix)]
        if prefix is not None:
            cid = f"route:{prefix}"
            prefixes: tuple[str, ...] = (prefix,)
            files: frozenset[str] = frozenset()
        else:
            cid = f"route:{key}"
            prefixes = ()
            files = frozenset(slot["files"])
        out.append(SpineAnchor(
            canonical_id=cid,
            key=key,
            source="route",
            display=_display_of(slot["seg"]),
            prefixes=prefixes,
            files=files,
            sources=frozenset({"route"}),
            page_route_files=frozenset(slot["page"]),
            api_route_files=frozenset(slot["api"]),
            barred=_bar_reason(key, version_re),
            route_groups=frozenset(slot["groups"]),
            depth=slot["depth"],
        ))
    return out


# ── Workspace anchors ────────────────────────────────────────────────────


def _display_of(raw: str) -> str:
    words = re.split(r"[-_\s]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", raw).strip())
    return " ".join(
        w if (w.isupper() and len(w) > 1) else w.capitalize() for w in words if w
    ) or raw


def _build_workspace_anchors(
    ctx: Any,
    all_owned: set[str],
    vocab: dict[str, Any],
) -> list[SpineAnchor]:
    shell_roots = set(vocab.get("workspace_shell_roots") or [])
    excluded = set(vocab.get("workspace_excluded_segments") or [])
    manifests = set(vocab.get("unit_manifest_files") or [])

    seen_paths: set[str] = set()
    out: list[SpineAnchor] = []

    def _add(path: str, name: str, cls: str) -> None:
        norm = path.replace("\\", "/").strip("/")
        if not norm or norm in seen_paths:
            return
        if any(seg.lower() in excluded for seg in norm.split("/")):
            return
        seen_paths.add(norm)
        out.append(SpineAnchor(
            canonical_id=f"ws:{norm}",
            key=normalize_anchor_key(norm.rsplit("/", 1)[-1]),
            source=cls,
            display=_display_of(name or norm.rsplit("/", 1)[-1]),
            prefixes=(norm,),
            sources=frozenset({cls}),
        ))

    stop = set(vocab.get("structural_stoplist") or [])
    for w in (getattr(ctx, "workspaces", None) or []):
        path = str(getattr(w, "path", "") or "")
        if not path:
            continue
        norm = path.replace("\\", "/").strip("/")
        first = norm.split("/")[0].lower()
        base_key = normalize_anchor_key(norm.rsplit("/", 1)[-1])
        # SHELL when under an app root (apps/web) OR the workspace IS a
        # structural unit (top-level ``frontend``/``backend``/``web`` —
        # the fastapi-template class: a top-level SPA unit is a
        # deployment shell, not a product package).
        cls = ("ws-app" if first in shell_roots or base_key in stop
               else "ws-pkg")
        _add(path, str(getattr(w, "name", "") or ""), cls)

    if not out:
        # Manifest-dir unit discovery (Soc0 backend/ + frontend/ class):
        # a top-level dir with its own manifest is a deployment unit —
        # a SHELL for lineage accounting (never mints).
        tracked = [str(p) for p in (getattr(ctx, "tracked_files", None) or [])]
        for t in sorted(tracked):
            segs = t.split("/")
            if len(segs) == 2 and segs[1] in manifests:
                unit = segs[0]
                if any(p.startswith(unit + "/") for p in all_owned):
                    _add(unit, unit, "ws-app")
    return out


# ── Schema anchors ───────────────────────────────────────────────────────


def _schema_keys(
    extractor_signals: dict[str, list[Any]] | None,
    all_owned: set[str],
    vocab: dict[str, Any],
) -> dict[str, str]:
    """``{normalized key: raw display source}`` from the Stage-1 schema
    extractor plus ``models/``-dir file stems (the FastAPI/pydantic class
    the extractor's Django arm cannot see — calibration Soc0 source)."""
    stop = set(vocab.get("structural_stoplist") or [])
    keys: dict[str, str] = {}

    def _add(raw: str) -> None:
        key = normalize_anchor_key(raw)
        alnum = re.sub(r"[^a-z0-9]+", "", key)
        if len(alnum) < 3:
            return
        # STRUCTURAL-VOCABULARY STOPLIST (calibration trap #1): a schema
        # name that IS framework vocabulary (`apps`, `page`) may never
        # become a name-match anchor — it would claim the framework tree.
        if key in stop or _singular_guarded(alnum) in stop or alnum in stop:
            return
        keys.setdefault(key, raw)

    for cand in (extractor_signals or {}).get("schema") or []:
        name = getattr(cand, "name", None)
        if isinstance(cand, dict):
            name = cand.get("name")
        if name:
            _add(str(name))
    for p in sorted(all_owned):
        segs = p.split("/")
        if len(segs) >= 2 and segs[-2].lower() in {"models", "model"}:
            stem = _stem(segs[-1])
            if stem and not stem.startswith("_"):
                _add(stem)
    return keys


def _build_schema_anchors(
    keys: dict[str, str],
    all_owned: set[str],
    vocab: dict[str, Any],
) -> list[SpineAnchor]:
    """Name-match each schema key against dirs + file stems repo-wide."""
    if not keys:
        return []
    # Pre-index: normalized dir-segment → dir prefixes; stem → files.
    dir_index: dict[str, set[str]] = defaultdict(set)
    stem_index: dict[str, set[str]] = defaultdict(set)
    for p in all_owned:
        segs = p.split("/")
        for i, seg in enumerate(segs[:-1]):
            dir_index[normalize_anchor_key(seg)].add("/".join(segs[: i + 1]))
        stem_index[normalize_anchor_key(_stem(segs[-1]))].add(p)

    out: list[SpineAnchor] = []
    for key in sorted(keys):
        prefixes = sorted(dir_index.get(key, ()))
        files = set(stem_index.get(key, ()))
        # Drop files already inside a matched dir (subtree covers them).
        files = {
            f for f in files
            if not any(f.startswith(pre + "/") for pre in prefixes)
        }
        if not prefixes and not files:
            continue
        out.append(SpineAnchor(
            canonical_id=f"schema:{key}",
            key=key,
            source="schema",
            display=_display_of(keys[key]),
            prefixes=tuple(prefixes),
            files=frozenset(files),
            sources=frozenset({"schema"}),
        ))
    return out


# ── Authored feature-dirs + domain-service-dirs ──────────────────────────


def _build_dir_anchors(
    all_owned: set[str],
    vocab: dict[str, Any],
) -> list[SpineAnchor]:
    fdir_containers = set(vocab.get("feature_dir_containers") or [])
    svc_containers = set(vocab.get("service_dir_containers") or [])
    stop = set(vocab.get("structural_stoplist") or [])

    found: dict[str, tuple[str, str]] = {}  # prefix → (class, domain)
    for p in sorted(all_owned):
        segs = p.split("/")
        for i in range(len(segs) - 2):
            low = segs[i].lower()
            cls = ("fdir" if low in fdir_containers
                   else "svc" if low in svc_containers else None)
            if cls is None:
                continue
            domain = segs[i + 1]
            dkey = normalize_anchor_key(domain)
            alnum = re.sub(r"[^a-z0-9]+", "", dkey)
            if len(alnum) < 3 or dkey in stop or alnum in stop:
                break
            prefix = "/".join(segs[: i + 2])
            found.setdefault(prefix, (cls, domain))
            break  # first container segment decides
    out: list[SpineAnchor] = []
    for prefix in sorted(found):
        cls, domain = found[prefix]
        out.append(SpineAnchor(
            canonical_id=f"{cls}:{prefix}",
            key=normalize_anchor_key(domain),
            source=cls,
            display=_display_of(domain),
            prefixes=(prefix,),
            sources=frozenset({cls}),
        ))
    return out


# ── Hub families (operator amendment 2026-07-06: per-vendor PF grain) ────


def _build_hub_anchors(
    developer_features: list["Feature"],
    all_owned: set[str],
    vocab: dict[str, Any],
) -> list[SpineAnchor]:
    """Per-vendor child anchors + a plumbing core anchor per hub family
    (operator amendment 2026-07-06: every integration = its own PF).

    A FAMILY exists where the DEV-GRAIN evidence proves per-vendor
    structure — mirroring how Stage 8.9.7 mints its children — never on
    raw file counts (a generic ``backend/services`` container holding a
    few vendor-named service files is NOT a hub — the Soc0 smoke
    overfire, 2026-07-06):

      * DEV ARM — ≥ 3 sibling VENDOR-PURE devs (every owned file names
        exactly one vendor in its path tokens) whose owned files share
        one deepest common parent dir (Soc0 ``edr-claroty`` /
        ``edr-cortex`` / … under ``backend/services/edr``);
      * LEXICON ARM — a dir whose own segment is a connector-container
        word (``connectors|integrations|providers|…``) with ≥ 3 distinct
        vendor-named children (midday ``packages/banking/src/providers``).

    Children of an established family:
      * every non-plumbing child DIR (dir-per-vendor hubs mint every
        real child — ``providers/teller`` counts even though ``teller``
        is not in the public vendor vocabulary);
      * per-vendor token-matched file sets for file-per-vendor hubs
        (``edr/cortex.py`` + ``edr/schema/cortex_baseline.py``).

    The core anchor is the family dir minus every child set — the
    shared plumbing (base/factory/normalizer class) the amendment
    routes to the parent ``<hub> core`` capability. Depth ≥ 2 (repo-top
    dirs are workspace roots, never hubs).
    """
    from faultline.pipeline_v2.hub_relation import HUB_PARENT_SEGMENTS

    plumbing = set(vocab.get("hub_plumbing_segments") or [])
    stop = set(vocab.get("structural_stoplist") or [])
    code_exts = tuple(vocab.get("code_extensions") or [])

    def _is_code(path: str) -> bool:
        return path.lower().endswith(code_exts)

    def _dev_vendor(owned: list[str]) -> str | None:
        """The single vendor EVERY owned file names, else ``None``."""
        common: set[str] | None = None
        for p in owned:
            toks: set[str] = set()
            for seg in p.split("/"):
                toks.update(_split_tokens(_stem(seg)))
            hits = {t for t in toks if vendor_of_segment(t)}
            common = hits if common is None else (common & hits)
            if not common:
                return None
        # Exactly ONE vendor across every file — two vendors in every
        # path is shared plumbing, not a vendor child.
        return sorted(common)[0] if common and len(common) == 1 else None

    def _common_dir(owned: list[str]) -> str | None:
        segs_list = [p.split("/")[:-1] for p in owned]
        if not segs_list:
            return None
        first = segs_list[0]
        k = len(first)
        for other in segs_list[1:]:
            j = 0
            while j < min(k, len(other)) and other[j] == first[j]:
                j += 1
            k = j
        return "/".join(first[:k]) if k >= 2 else None  # depth ≥ 2

    # ── DEV ARM: vendor-pure sibling devs → family dirs ─────────────
    fam_vendors: dict[str, set[str]] = defaultdict(set)
    for f in developer_features:
        owned = owned_paths_of(f)
        if not owned or not all(_is_code(p) for p in owned):
            continue
        v = _dev_vendor(owned)
        if v is None:
            continue
        parent = _common_dir(owned)
        if parent and vendor_of_segment(parent.rsplit("/", 1)[-1]) == v:
            # Dir-per-vendor layout: the dev's own common dir IS the
            # vendor dir (``providers/gocardless``) — the FAMILY parent
            # is one level up (``providers``).
            parent = parent.rsplit("/", 1)[0] if "/" in parent else None
            if parent and len(parent.split("/")) < 2:
                parent = None
        if parent:
            fam_vendors[parent].add(v)

    # ── LEXICON ARM: connector-container dirs w/ ≥3 vendor children ──
    children_by_dir: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list))
    for p in all_owned:
        segs = p.split("/")
        for i in range(1, len(segs) - 1):  # depth ≥ 2 for the family dir
            d = "/".join(segs[: i + 1])
            children_by_dir[d][segs[i + 1]].append(p)
    for d in sorted(children_by_dir):
        if d.rsplit("/", 1)[-1].lower() not in HUB_PARENT_SEGMENTS:
            continue
        vendors: set[str] = set()
        for child, child_paths in children_by_dir[d].items():
            if not any(_is_code(p) for p in child_paths):
                continue
            v = vendor_of_segment(child)
            if v:
                vendors.add(v)
        if len(vendors) >= 3:
            fam_vendors[d].update(vendors)

    family_dirs = sorted(d for d, vs in fam_vendors.items() if len(vs) >= 3)
    # Nested families collapse to the shallowest qualifying dir.
    kept_dirs: list[str] = []
    for d in family_dirs:
        if any(d.startswith(k + "/") for k in kept_dirs):
            continue
        kept_dirs.append(d)

    out: list[SpineAnchor] = []
    for d in kept_dirs:
        vendors = fam_vendors[d]
        kids = children_by_dir.get(d, {})
        base = d.rsplit("/", 1)[-1]
        hub_key = normalize_anchor_key(base)
        # Parent classes (Soc0 smoke, 2026-07-06):
        #   * STOPLISTED parent (backend/routers, backend/models — the
        #     structural vocabulary): per-vendor integration FILES are
        #     real evidence, but the container is never a capability —
        #     NO core anchor (a "Routers Core" PF would be the
        #     container-PF class again) AND children mint only on FLOW
        #     evidence (a vendor-named model file alone is not an
        #     integration).
        #   * LEXICON parent (providers/, integrations/, app-store/ —
        #     author-declared connector containers): children mint on
        #     the normal code-or-flow rule, and the core is ALSO
        #     suppressed — the enclosing ws-pkg / fdir / svc anchor is
        #     the natural plumbing home ("Banking", not "Providers
        #     Core").
        #   * capability-named parent (backend/services/edr): full
        #     family — children + the "<hub> Core" anchor (the operator
        #     amendment's Soc0 edr case).
        parent_generic = hub_key in stop
        suppress_core = parent_generic or base.lower() in HUB_PARENT_SEGMENTS
        subtree = {p for p in all_owned
                   if p.startswith(d + "/")}

        child_anchors: list[SpineAnchor] = []
        claimed_paths: set[str] = set()
        vendor_dirs = {
            child for child in kids
            if any(p != f"{d}/{child}" for p in kids[child])
            and vendor_of_segment(child)
        }
        if vendor_dirs:
            # dir-per-vendor: EVERY non-plumbing child dir is a child.
            for child in sorted(kids):
                child_paths = kids[child]
                is_dir = any(p != f"{d}/{child}" for p in child_paths)
                low = child.lower()
                if not is_dir or low in plumbing or low in stop:
                    continue
                if not any(_is_code(p) for p in child_paths):
                    continue
                prefix = f"{d}/{child}"
                child_anchors.append(SpineAnchor(
                    canonical_id=f"hub:{prefix}",
                    key=normalize_anchor_key(child),
                    source="hub-vendor",
                    display=_display_of(child),
                    prefixes=(prefix,),
                    sources=frozenset({"hub-vendor"}),
                    hub_dir=d,
                    vendor=vendor_of_segment(child) or child.lower(),
                    hub_parent_generic=parent_generic,
                ))
                claimed_paths.update(child_paths)
        else:
            # file-per-vendor: token-matched file sets across the subtree.
            by_vendor: dict[str, set[str]] = defaultdict(set)
            for p in subtree:
                rel = p[len(d) + 1:]
                toks = set(_split_tokens(_stem(rel)))
                for seg in rel.split("/")[:-1]:
                    toks.update(_split_tokens(seg))
                hits = toks & vendors
                if len(hits) == 1:
                    by_vendor[next(iter(hits))].add(p)
            for v in sorted(by_vendor):
                vfiles = by_vendor[v]
                child_anchors.append(SpineAnchor(
                    canonical_id=f"hub:{d}/{v}",
                    key=normalize_anchor_key(v),
                    source="hub-vendor",
                    display=_display_of(v),
                    files=frozenset(vfiles),
                    sources=frozenset({"hub-vendor"}),
                    hub_dir=d,
                    vendor=v,
                    hub_parent_generic=parent_generic,
                ))
                claimed_paths.update(vfiles)
        if not child_anchors:
            continue
        out.extend(child_anchors)
        if not suppress_core:
            out.append(SpineAnchor(
                canonical_id=f"hub:{d}",
                key=hub_key,
                source="hub-core",
                display=f"{_display_of(base)} Core",
                prefixes=(d,),
                exclude_files=frozenset(claimed_paths),
                sources=frozenset({"hub-core"}),
                hub_dir=d,
            ))
    return out


# ── Cross-source key-merge + nav confirmation ────────────────────────────


def _merge_anchors(anchors: list[SpineAnchor]) -> list[SpineAnchor]:
    """Merge same-key anchors across the domain-keyed classes
    (route/schema/fdir/svc; ws-pkg joins only when its basename is
    unique among workspace anchors — the basename-collision trap).
    hub-* and ws-app anchors never merge (identity anchors)."""
    mergeable = {"route", "schema", "fdir", "svc"}
    ws_pkg_by_key: dict[str, list[SpineAnchor]] = defaultdict(list)
    for a in anchors:
        if a.source == "ws-pkg":
            ws_pkg_by_key[a.key].append(a)

    groups: dict[str, list[SpineAnchor]] = defaultdict(list)
    passthrough: list[SpineAnchor] = []
    for a in anchors:
        if a.source in mergeable:
            groups[a.key].append(a)
        elif a.source == "ws-pkg" and len(ws_pkg_by_key[a.key]) == 1:
            groups[a.key].append(a)
        else:
            passthrough.append(a)

    merged: list[SpineAnchor] = []
    for key in sorted(groups):
        members = groups[key]
        if len(members) == 1:
            merged.append(members[0])
            continue
        # ROUTE anchors at different descend depths of the SAME key stay
        # separate only when their prefixes nest (specificity reduction
        # at classification handles nesting); same-key cross-source and
        # cross-workspace anchors union into ONE capability candidate.
        members.sort(key=lambda a: (a.rank, a.canonical_id))
        head = members[0]
        prefixes: list[str] = []
        files: set[str] = set()
        sources: set[str] = set()
        page: set[str] = set()
        api: set[str] = set()
        groups_meta: set[str] = set()
        barred: str | None = head.barred
        for m in members:
            prefixes.extend(m.prefixes)
            files.update(m.files)
            sources.update(m.sources)
            page.update(m.page_route_files)
            api.update(m.api_route_files)
            groups_meta.update(m.route_groups)
            if m.barred is None:
                barred = None  # any unbarred member lifts the bar
        merged.append(SpineAnchor(
            canonical_id=head.canonical_id,
            key=key,
            source=head.source,
            display=head.display,
            prefixes=tuple(dict.fromkeys(prefixes)),
            files=frozenset(files),
            sources=frozenset(sources),
            page_route_files=frozenset(page),
            api_route_files=frozenset(api),
            barred=barred,
            route_groups=frozenset(groups_meta),
            depth=head.depth,
        ))
    merged.extend(passthrough)
    merged.sort(key=lambda a: (a.rank, a.canonical_id))
    return merged


def build_spine_anchors(
    developer_features: list["Feature"],
    routes_index: list[dict[str, Any]] | None,
    ctx: Any,
    extractor_signals: dict[str, list[Any]] | None = None,
    nav_keys: frozenset[str] = frozenset(),
) -> list[SpineAnchor]:
    """The §4.3 candidate builder. Deterministic: sorted everywhere."""
    vocab = load_spine_vocab()
    all_owned: set[str] = set()
    for f in developer_features:
        if getattr(f, "layer", "developer") != "developer":
            continue
        all_owned.update(owned_paths_of(f))

    anchors: list[SpineAnchor] = []
    anchors.extend(_build_route_anchors(routes_index or [], vocab))
    anchors.extend(_build_workspace_anchors(ctx, all_owned, vocab))
    anchors.extend(_build_schema_anchors(
        _schema_keys(extractor_signals, all_owned, vocab), all_owned, vocab))
    anchors.extend(_build_dir_anchors(all_owned, vocab))
    anchors.extend(_build_hub_anchors(
        [f for f in developer_features
         if getattr(f, "layer", "developer") == "developer"],
        all_owned, vocab))

    merged = _merge_anchors(anchors)
    if nav_keys:
        for a in merged:
            if a.key in nav_keys:
                # Nav labels CONFIRM anchors (ranking evidence), never
                # create subtrees — the calibration's measured verdict.
                a.nav_confirmed = True
    return merged
