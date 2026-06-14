"""Stage 8.6.7 — dependency-injection service attribution (deterministic).

Why this stage exists
=====================

Services wired through dependency injection — a registry the framework decorates
onto a request/app object (``server.services.secretService`` in Fastify) — are
referenced from feature code BY NAME, never by ``import``. Static import-following
(Stage 2.6 closure, Stage 6.3 import-tree) cannot cross that boundary, so every
service file falls to the workspace / platform bucket and inflates
``platform_share`` (the package-node-blob signal). Measured on infisical: 75% of
services are referenced by ≤2 route files, i.e. they DO have a clear owner — the
import graph just can't see it.

This stage follows the NAMED reference instead. Driven entirely by
``eval/di-patterns.yaml`` (per stack-pattern-library: patterns in YAML, not
Python), for each pattern that self-detects in the repo it:

  1. scans the pattern's ``consumer_glob`` files for ``reference_pattern`` and
     records, per service TOKEN, the distinct FEATURES whose own files reference
     it (consumer file → feature via the primary-path index);
  2. maps each token to its service file(s) via ``service_file_map``;
  3. attributes the service files to the DOMINANT referencing feature when the
     token's fan-in (distinct referencing features) is at or below a
     scale-invariant cap = ``max(floor, P{percentile})`` of the per-token fan-in
     distribution — a service referenced by more features than that is shared
     infrastructure and stays on the platform bucket;
  4. moves the attributed files OFF the platform anchor (adds to the feature's
     ``paths``, removes from the anchors', prunes the path-keyed attribution
     surfaces — same as Stage 8.7 de-sink).

Validated against ``eval/membership/infisical``: service-heavy features' recall
rises (machine-identities 0.17 → 0.62) with precision held, and
``platform_share`` drops ~5.6pp — the files land on the RIGHT features, not just
off the blob.

Adding a framework (NestJS, awilix, …) is a new ``di-patterns.yaml`` entry — no
Python change. Deterministic. No LLM. No network. Default ON; disable via
``FAULTLINE_STAGE_8_6_7_DI_ATTRIBUTION=0``.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.data import load_yaml
from faultline.pipeline_v2.stage_2_6_membership_closure import _nearest_rank
from faultline.pipeline_v2.stage_8_7_anchor_desink import (
    _is_workspace_anchor,
    _prune_surfaces,
)

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.stage_0_intake import ScanContext


_DI_PATTERNS_FILE = "di-patterns.yaml"
_MAX_READ_BYTES = 2_000_000


def _camel_to_kebab(token: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", token).lower()


def _glob_to_re(glob: str) -> re.Pattern[str]:
    """Tiny glob→regex: ``**`` = any chars, ``*`` = any non-slash chars."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


@dataclass
class _PatternOutcome:
    pattern: str
    detected: bool
    fan_in_threshold: int = 0
    recipient_cap: int = 0
    tokens_seen: int = 0
    tokens_attributed: int = 0
    files_moved: int = 0
    features_enriched: int = 0
    sample: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DiAttributionResult:
    enabled: bool = True
    files_moved: int = 0
    patterns: list[_PatternOutcome] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "files_moved": self.files_moved,
            "patterns": [
                {
                    "pattern": p.pattern,
                    "detected": p.detected,
                    "fan_in_threshold": p.fan_in_threshold,
                    "recipient_cap": p.recipient_cap,
                    "tokens_seen": p.tokens_seen,
                    "tokens_attributed": p.tokens_attributed,
                    "files_moved": p.files_moved,
                    "features_enriched": p.features_enriched,
                    "sample": p.sample[:20],
                }
                for p in self.patterns
            ],
        }


def _is_enabled() -> bool:
    return os.environ.get("FAULTLINE_STAGE_8_6_7_DI_ATTRIBUTION", "1") != "0"


def _collect_deps(repo: Path, tracked: frozenset[str]) -> set[str]:
    """Union of dependency names across every tracked package.json."""
    deps: set[str] = set()
    for rel in tracked:
        if not rel.endswith("package.json"):
            continue
        try:
            pkg = json.loads((repo / rel).read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError):
            continue
        if not isinstance(pkg, dict):
            continue
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            block = pkg.get(key)
            if isinstance(block, dict):
                deps.update(str(k) for k in block)
    return deps


def _detection_holds(detection: dict[str, Any], deps: set[str]) -> bool:
    dep = detection.get("dep")
    if dep and dep not in deps and not any(d == dep or d.startswith(dep + "/") for d in deps):
        return False
    none_prefix = detection.get("none_dep_prefix")
    if none_prefix and any(d.startswith(none_prefix) for d in deps):
        return False
    return True


def _read(repo: Path, rel: str) -> str | None:
    try:
        p = repo / rel
        if p.stat().st_size > _MAX_READ_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _apply_pattern(
    name: str,
    spec: dict[str, Any],
    repo: Path,
    tracked: frozenset[str],
    deps: set[str],
    specifics: list["Feature"],
    anchors: list["Feature"],
) -> tuple[_PatternOutcome, set[str]]:
    out = _PatternOutcome(pattern=name, detected=False)
    if not _detection_holds(spec.get("detection") or {}, deps):
        return out, set()

    ref_re = re.compile(str(spec["reference_pattern"]))
    consumer_re = _glob_to_re(str(spec.get("consumer_glob") or "**"))
    marker = (spec.get("detection") or {}).get("source_marker")

    # consumer file → feature (primary-path owner among specific features)
    file_feat: dict[str, str] = {}
    for f in specifics:
        for p in f.paths:
            file_feat[p] = f.name

    # token → Counter(feature → # consumer files referencing it). A consumer
    # file counts ONCE per token; the dominant feature is the one whose files
    # reference the token most.
    tok_feats: dict[str, Counter[str]] = defaultdict(Counter)
    marker_seen = marker is None
    for rel in tracked:
        if not consumer_re.match(rel):
            continue
        text = _read(repo, rel)
        if not text:
            continue
        if marker and marker in text:
            marker_seen = True
        feat = file_feat.get(rel)
        if feat is None:
            continue
        for token in {m.group(1) for m in ref_re.finditer(text)}:
            tok_feats[token][feat] += 1
    if not marker_seen or not tok_feats:
        return out, set()
    out.detected = True
    out.tokens_seen = len(tok_feats)

    # token → service files (dir-convention)
    sfm = spec.get("service_file_map") or {}
    suffix = str(sfm.get("file_suffix") or "")
    templates = list(sfm.get("dir_templates") or ["services/{dir}"])
    tok_files: dict[str, list[str]] = {}
    for token in tok_feats:
        d = _camel_to_kebab(token)
        segs = ["/" + t.replace("{dir}", d).strip("/") + "/" for t in templates]
        files = [
            p for p in tracked
            if p.endswith(suffix) and any(seg in ("/" + p) for seg in segs)
            and ".test." not in p and ".spec." not in p
        ]
        if files:
            tok_files[token] = files

    # scale-invariant fan-in cap over the per-token #referencing-features
    fan = spec.get("fan_in") or {}
    pct = float(fan.get("percentile", 90)) / 100.0
    floor = int(fan.get("floor", 3))
    counts = sorted(len(tok_feats[t]) for t in tok_files)
    cap = max(floor, _nearest_rank(counts, pct)) if counts else floor
    out.fan_in_threshold = cap

    by_name = {f.name: f for f in specifics}
    # Pass 1 — candidate (token → dominant feature) under the fan-in cap.
    candidates: list[tuple[str, str, list[str]]] = []
    dom_counts: Counter[str] = Counter()
    for token, files in tok_files.items():
        feat_counts = tok_feats[token]
        if len(feat_counts) > cap:  # shared across too many features → stays platform
            continue
        # dominant = feature referencing it in the most consumer files; ties
        # break deterministically (count desc, then name asc).
        dom = sorted(feat_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        candidates.append((token, dom, files))
        dom_counts[dom] += 1
    # Per-recipient cap (scale-invariant): a feature that is the dominant for
    # more than max(floor, P{pct}) tokens is a blob CONSUMER — a coarse
    # route-registration aggregate that owns many features' route files (e.g. a
    # single `ee-v1-routes` owning every enterprise router), so every service it
    # touches would pile onto it. Skip those; the services stay on the platform
    # bucket (honest residual) rather than form a new blob.
    recip_cap = max(floor, _nearest_rank(sorted(dom_counts.values()), pct)) if dom_counts else floor
    out.recipient_cap = recip_cap

    moved: set[str] = set()
    for token, dom, files in candidates:
        if dom_counts[dom] > recip_cap:
            continue
        new = [p for p in files if p not in by_name[dom].paths]
        if not new:
            continue
        by_name[dom].paths = list(by_name[dom].paths) + new
        moved.update(files)
        out.tokens_attributed += 1
        if len(out.sample) < 20:
            out.sample.append({"token": token, "feature": dom, "files": len(files)})

    out.files_moved = len(moved)
    out.features_enriched = len({s["feature"] for s in out.sample})
    # remove moved files from the platform anchors + keep surfaces consistent
    if moved:
        for a in anchors:
            kept = [p for p in a.paths if p not in moved]
            if len(kept) != len(a.paths):
                removed = set(a.paths) - set(kept)
                a.paths = kept
                _prune_surfaces(a, removed)
    return out, moved


def attribute_di_services(
    ctx: "ScanContext",
    features: list["Feature"],
) -> DiAttributionResult:
    """Attribute DI-referenced service files to their owning features.

    Mutates features in place (``paths`` of the dominant referencing feature,
    and the platform anchors' ``paths`` + surfaces). Returns telemetry.
    """
    result = DiAttributionResult(enabled=_is_enabled())
    if not result.enabled:
        return result

    anchors = [f for f in features if _is_workspace_anchor(f)]
    specifics = [f for f in features if not _is_workspace_anchor(f)]
    if not anchors or not specifics:
        return result

    try:
        cfg = load_yaml(_DI_PATTERNS_FILE)
    except (OSError, FileNotFoundError):
        return result
    patterns = (cfg or {}).get("patterns") or {}
    if not isinstance(patterns, dict):
        return result

    repo = Path(ctx.repo_path)
    tracked = frozenset(ctx.tracked_files)
    deps = _collect_deps(repo, tracked)

    for name, spec in patterns.items():
        if not isinstance(spec, dict):
            continue
        outcome, moved = _apply_pattern(
            name, spec, repo, tracked, deps, specifics, anchors,
        )
        result.patterns.append(outcome)
        result.files_moved += len(moved)

    return result


__all__ = [
    "DiAttributionResult",
    "attribute_di_services",
]
