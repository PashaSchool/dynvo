"""Stage 6.9b — generated-code output-tree strip (deterministic, no LLM).

Sibling of the Stage 6.9 test-strip. Machine-GENERATED source (protobuf
``*.pb.go`` / ``*_pb2.py``, sqlc ``*.sql_generated.go``, stringer
``*_string.go``, k8s ``zz_generated.*``, dart ``*.g.dart`` …) is real code the
product compiles, but it is NOT a hand-authored product feature: a human curates
a golden by the capability (``API Keys``, ``Ratelimit``), never by "360
``*.sql_generated.go`` query files". Surfacing those as a feature both inflates
the ``owned_max`` blob (unkey ``pkg/db`` = 360 generated + 20 hand-written) and
hurts PF/UF precision (the engine's features stop matching human boundaries).

This pass removes generated-file entries from the OUTPUT TREE — exactly like the
test-strip, with the same invariants: it NEVER recomputes a metric scalar
(coverage / health / churn are computed UPSTREAM with the files present), and
features / flows that become empty are dropped. It reuses the test-strip's
predicate-independent helpers; only the *predicate* (:func:`is_generated_path`)
differs.

v1 is FILENAME-pattern based (no file I/O) — it catches the major codegen
conventions. The universal ``// Code generated … DO NOT EDIT.`` content marker
(Go spec) is a thorough follow-up that needs a file read at extraction time.

Disable via ``FAULTLINE_STAGE_6_9B_GENERATED_STRIP=0`` (default ON).
"""

from __future__ import annotations

import os
import re
from typing import Any

from faultline.pipeline_v2.stage_6_9_test_strip import (
    _FEATURE_LIST_ATTRS,
    _FLOW_EDGE_ATTRS,
    _FLOW_LIST_ATTRS,
    _endpoints_of,
    _feature_path_empty,
    _flow_is_empty,
    _iter_unique,
    _path_of,
)

__all__ = [
    "is_generated_path",
    "strip_generated_paths",
    "stage_6_9b_enabled",
    "STAGE_6_9B_ENV_FLAG",
    "GENERATED_CONTENT_ENV_FLAG",
    "generated_content_marker_enabled",
    "GeneratedContentProbe",
]

STAGE_6_9B_ENV_FLAG = "FAULTLINE_STAGE_6_9B_GENERATED_STRIP"
#: S5a-it2 (2026-07-18) — the "thorough follow-up" this module's docstring
#: promised: the universal content-marker banner. Default OFF (flag law).
GENERATED_CONTENT_ENV_FLAG = "FAULTLINE_GENERATED_CONTENT_MARKER"


# ── Generated-file predicate ────────────────────────────────────────────────
# HIGH-CONFIDENCE filename conventions only — every pattern is a near-universal
# codegen marker, never a hand-authored name. Structural, corpus-free
# (rule-no-repo-specific-paths). Matched on the lowercased basename.
_GENERATED_FILENAME_RE = re.compile(
    r"(?:"
    r"\.pb\.(?:go|cc|h|dart|rb|swift|ts|js)$"   # protobuf: *.pb.go, *.pb.cc …
    r"|\.pb\.gw\.go$"                            # grpc-gateway
    r"|_grpc\.pb\.(?:go|ts)$"                    # grpc stubs
    r"|_pb2(?:_grpc)?\.pyi?$"                    # protobuf python: *_pb2.py(i)
    # *_generated.go (sqlc) / *.generated.cs … — restricted to COMPILED-language
    # extensions, where this suffix is a near-universal codegen marker. The
    # ambiguous web families (.ts/.tsx/.js/.json/.sql) are deliberately EXCLUDED:
    # a hand-maintained `user.generated.ts` is plausible, and a default-ON strip
    # must not silently drop it (the // Code generated … DO NOT EDIT content
    # marker is the safe follow-up for those). sqlc's *.sql_generated.go still
    # matches via the `go` family.
    r"|[._]generated\.(?:go|cc|cpp|cxx|h|hpp|cs|swift|kt|dart|rb)$"
    r"|[._]gen\.(?:go|cc|cpp|cxx|h|hpp|cs|swift|kt|dart|rb)$"
    r"|^zz_generated[._].*\.go$"                 # k8s / controller-gen
    r"|_string\.go$"                             # stringer
    r"|\.g\.dart$|\.freezed\.dart$"             # dart build_runner / freezed
    r"|\.designer\.cs$"                          # C# designer
    r"|\.generated\.swift$"                      # swiftgen / sourcery
    r")",
    re.IGNORECASE,
)


def is_generated_path(path: str) -> bool:
    """``True`` when *path*'s basename matches a high-confidence machine-codegen
    filename convention (protobuf / sqlc / stringer / k8s-gen / dart / C# …)."""
    if not path or not isinstance(path, str):
        return False
    base = path.lower().replace("\\", "/").rsplit("/", 1)[-1]
    return bool(_GENERATED_FILENAME_RE.search(base))


def stage_6_9b_enabled() -> bool:
    """Default ON; ``FAULTLINE_STAGE_6_9B_GENERATED_STRIP=0`` disables."""
    return os.environ.get(STAGE_6_9B_ENV_FLAG, "1").strip() not in {
        "0",
        "false",
        "False",
    }


def generated_content_marker_enabled() -> bool:
    """S5a-it2 — default OFF; ``FAULTLINE_GENERATED_CONTENT_MARKER=1`` arms
    the content-marker banner probe inside the existing 6.9b channel.
    unset/=0 keeps the v1 filename-only predicate byte-identically."""
    return os.environ.get(
        GENERATED_CONTENT_ENV_FLAG, "0",
    ).strip().lower() in {"1", "true"}


# ── Content-marker probe (the docstring's "thorough follow-up") ──────────────
#: Sniff window — codegen banners are the FIRST thing in the file (Speakeasy /
#: orval / openapi-generator / graphql-codegen / protobuf all print them at
#: byte 0); 2 KiB tolerates a licence header above the banner.
_BANNER_SNIFF = 2048
#: The universal marker mechanisms (NOT a vendor vocabulary):
#:   * the Go-spec-formalized line class — "Code generated … DO NOT EDIT."
#:     (Speakeasy, sqlc, protoc-gen, oapi-codegen all emit this exact shape);
#:   * the ``@generated`` marker convention (Meta/Sapling class);
#:   * the two-line class — a "generated" declaration plus a separate
#:     do-not-edit/modify/change admonition inside the same banner window
#:     (orval "Generated by orval" + "Do not edit manually", openapi-generator
#:     "auto generated by OpenAPI Generator" + "Do not edit the class
#:     manually", graphql-codegen "automatically generated" + "should not be
#:     edited").
_GENERATED_LINE_RE = re.compile(
    r"(?i)\bcode generated by\b[^\n]*\bdo not edit\b")
_AT_GENERATED_RE = re.compile(r"@generated\b")
_GENERATED_WORD_RE = re.compile(
    r"(?i)\b(?:auto[- ]?|automatically )?generated\b")
_DO_NOT_EDIT_RE = re.compile(
    r"(?i)\b(?:do not|don'?t|should not be|never)\s+"
    r"(?:be\s+)?(?:edit|modif|chang)")


class GeneratedContentProbe:
    """Run-scoped, memoised banner sniff: ``True`` when the file's head
    carries a machine-codegen marker. Reads at most ``_BANNER_SNIFF`` bytes
    per DISTINCT path, once per run. Missing/unreadable files are ``False``
    (fail-open to hand-authored — a default-OFF armed probe must never
    strip on I/O doubt)."""

    def __init__(self, repo_root: Any) -> None:
        from pathlib import Path
        self._root: Any = None
        if repo_root:
            p = Path(str(repo_root))
            if p.is_dir():
                self._root = p
        self._memo: dict[str, bool] = {}

    @property
    def available(self) -> bool:
        return self._root is not None

    def is_generated(self, rel_path: str) -> bool:
        """Filename convention OR content banner (armed callers' ONE
        predicate)."""
        if is_generated_path(rel_path):
            return True
        return self.banner_generated(rel_path)

    def banner_generated(self, rel_path: str) -> bool:
        got = self._memo.get(rel_path)
        if got is not None:
            return got
        out = False
        if self._root is not None and rel_path:
            try:
                with open(self._root / rel_path, "rb") as fp:
                    head = fp.read(_BANNER_SNIFF)
                if b"\x00" not in head:
                    text = head.decode("utf-8", errors="replace")
                    out = bool(
                        _GENERATED_LINE_RE.search(text)
                        or _AT_GENERATED_RE.search(text)
                        or (_GENERATED_WORD_RE.search(text)
                            and _DO_NOT_EDIT_RE.search(text)))
            except OSError:
                out = False
        self._memo[rel_path] = out
        return out


# ── predicate-driven strip (mirrors the test-strip machinery) ────────────────


def _filter_seq(seq: Any, pred: Any) -> tuple[Any, int]:
    if not isinstance(seq, list):
        return seq, 0
    kept: list[Any] = []
    removed = 0
    for e in seq:
        p = _path_of(e)
        if p is not None and pred(p):
            removed += 1
            continue
        kept.append(e)
    return kept, removed


def _filter_edges(seq: Any, pred: Any) -> tuple[Any, int]:
    if not isinstance(seq, list):
        return seq, 0
    kept: list[Any] = []
    removed = 0
    for e in seq:
        fp, tp = _endpoints_of(e)
        if (fp and pred(fp)) or (tp and pred(tp)):
            removed += 1
            continue
        kept.append(e)
    return kept, removed


def _strip_attr(obj: Any, attr: str, pred: Any, *, edges: bool = False) -> int:
    cur = getattr(obj, attr, None)
    if cur is None:
        return 0
    new, removed = (_filter_edges(cur, pred) if edges
                    else _filter_seq(cur, pred))
    if removed:
        setattr(obj, attr, new)
    return removed


def strip_generated_paths(
    features: list[Any], flows: list[Any],
    repo_root: Any = None,
) -> dict[str, int]:
    """Strip generated-file entries from the feature / flow OUTPUT TREE in
    place; drop features / flows that become empty. NEVER touches metric
    scalars. Returns ``{paths_removed, features_dropped, flows_dropped}``.

    S5a-it2: when ``FAULTLINE_GENERATED_CONTENT_MARKER`` is armed AND
    ``repo_root`` is a real tree, the predicate widens to the content-marker
    banner probe (the novu ``libs/internal-sdk`` Speakeasy client: 923
    hand-named ``.ts`` files, every one carrying "Code generated by
    Speakeasy … DO NOT EDIT"). unset/=0 or no root → the v1 filename-only
    predicate, byte-identical."""
    stats = {"paths_removed": 0, "features_dropped": 0, "flows_dropped": 0}
    features = features if isinstance(features, list) else []
    flows = flows if isinstance(flows, list) else []
    pred: Any = is_generated_path
    if generated_content_marker_enabled():
        probe = GeneratedContentProbe(repo_root)
        if probe.available:
            pred = probe.is_generated
            stats["content_marker"] = 1

    # 1. Strip each flow exactly once (across the top-level list + containment).
    flow_objs = _iter_unique(
        list(flows)
        + [fl for f in features for fl in (getattr(f, "flows", None) or [])]
    )
    for fl in flow_objs:
        for attr in _FLOW_LIST_ATTRS:
            stats["paths_removed"] += _strip_attr(fl, attr, pred)
        for attr in _FLOW_EDGE_ATTRS:
            stats["paths_removed"] += _strip_attr(fl, attr, pred, edges=True)

    # 2. Drop flows emptied by the strip.
    drop_flow_ids = {id(fl) for fl in flow_objs if _flow_is_empty(fl)}
    stats["flows_dropped"] = len(drop_flow_ids)
    if drop_flow_ids:
        flows[:] = [fl for fl in flows if id(fl) not in drop_flow_ids]
        for f in features:
            fl_list = getattr(f, "flows", None)
            if isinstance(fl_list, list):
                fl_list[:] = [fl for fl in fl_list if id(fl) not in drop_flow_ids]

    # 3. Strip feature-level surfaces.
    for f in features:
        for attr in _FEATURE_LIST_ATTRS:
            stats["paths_removed"] += _strip_attr(f, attr, pred)
        # member_files carry the OWNED ledger the blob metric reads — strip them
        # too, else a de-owned generated file still counts toward owned_max.
        mf = getattr(f, "member_files", None)
        if isinstance(mf, list):
            kept = [m for m in mf if not pred(_path_of(m) or "")]
            if len(kept) != len(mf):
                stats["paths_removed"] += len(mf) - len(kept)
                f.member_files = kept

    # 4. Drop features that became path-empty.
    drop_feat_ids = {id(f) for f in features if _feature_path_empty(f)}
    stats["features_dropped"] = len(drop_feat_ids)
    if drop_feat_ids:
        features[:] = [f for f in features if id(f) not in drop_feat_ids]

    return stats
