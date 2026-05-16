"""Sprint 22 — Hierarchical (two-pass) scan.

Architecture
============

Replaces the monolith / per-package scan with a 2-pass dispatch where
Sonnet always sees a bounded payload:

  Pass 1 (rough):    Sonnet sees a directory tree summary (top-level
                     dirs + file counts). Output: 8-15 'buckets', each
                     a logical product domain mapped to a list of dirs.
                     Cost: ~$0.02-0.05 per scan.

  Pass 2 (refined):  For each bucket, separate deep_scan(package_mode)
                     call with ONLY that bucket's files. Sonnet returns
                     1-3 sub-features per bucket. Cost: ~$0.02 per call.

Per-bucket payload caps at ~800 files; nothing scales linearly with
repo size beyond that. Cal.com (10K files) goes from one timeout-prone
1500-LOC system prompt → 20 small bounded Sonnet calls.

Why hierarchical (vs S21 path-embeddings)
==========================================

S21 used Voyage embeddings to cluster paths algorithmically. Per-bucket
Sonnet calls then named the clusters. This produced same-looking
output as monolith because Sonnet collapsed semantic clusters back
into umbrella names.

Hierarchical reverses the order: Sonnet picks the buckets first
(using domain knowledge from path names + dir layout), then names
sub-features. Sonnet's domain reasoning runs at BOTH stages, so
each bucket is named with the right specificity.

Public surface
==============

    plan_buckets(top_level_summary, *, api_key, model) -> list[Bucket]
        Pass 1 — Sonnet picks 8-15 logical buckets from the dir tree.

    Bucket — dataclass: name, dirs (list[str]), files (list[str])

The pipeline integrates this via _run_hierarchical_scan() in
pipeline.py (similar shape to _run_single_call / deep_scan_workspace).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 32_768

# Pass 1 target: produce 8-15 buckets. Floor lets small repos stay
# coherent; ceiling prevents runaway buckets on big monorepos.
_BUCKET_FLOOR = 8
_BUCKET_CEILING = 20


@dataclass
class Bucket:
    """A Pass-1 bucket — a logical product domain mapped to dirs."""

    name: str
    dirs: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


# ── Top-level structure summarisation ────────────────────────────────


def summarise_layout(
    files: list[str], *, max_lines: int = 60,
) -> tuple[str, dict[str, list[str]]]:
    """Build a directory-tree summary suitable for Pass 1 input.

    For each top-level directory (or top-2 levels for ``apps/`` /
    ``packages/`` / ``crates/`` workspace-style layouts), emit a line
    with the path + file count. Returns ``(summary_text, dir_to_files)``.

    When a repo has fewer than 3 top-level dirs, automatically dives
    one level deeper so Sonnet still gets meaningful structure (e.g.
    apprise lives entirely under ``apprise/``; we want ``apprise/plugins``,
    ``apprise/decorators``, etc., not a single top dir).
    """
    workspace_prefixes = {"apps", "packages", "crates", "internal", "modules"}

    # First pass: top-level grouping with workspace 2-deep.
    bucket: dict[str, list[str]] = defaultdict(list)
    for f in files:
        parts = f.split("/")
        if not parts:
            continue
        if len(parts) >= 3 and parts[0] in workspace_prefixes:
            key = f"{parts[0]}/{parts[1]}"
        else:
            key = parts[0]
        bucket[key].append(f)

    # If too coarse — drill deeper. Two triggers:
    #  (a) repo has only 1-2 top-level dirs (small repos like apprise
    #      where everything lives under apprise/)
    #  (b) ONE top-level dir dominates (≥80% of files), so the OTHER
    #      few dirs are just bin/, packaging/, examples/. Drill into
    #      the dominant dir.
    needs_drill = False
    if files:
        if len(bucket) <= 2:
            needs_drill = True
        else:
            largest = max((len(v) for v in bucket.values()), default=0)
            if largest / max(len(files), 1) >= 0.8:
                needs_drill = True
    if needs_drill:
        deep: dict[str, list[str]] = defaultdict(list)
        for f in files:
            parts = f.split("/")
            if len(parts) >= 3:
                key = f"{parts[0]}/{parts[1]}"
            elif len(parts) == 2:
                key = parts[0]
            else:
                key = parts[0] if parts else "root"
            deep[key].append(f)
        if len(deep) >= 3:
            bucket = deep

    # Sort by file count desc; Sonnet sees biggest dirs first
    sorted_dirs = sorted(bucket.items(), key=lambda kv: -len(kv[1]))

    lines: list[str] = []
    for d, fs in sorted_dirs[:max_lines]:
        # Extract distinctive subdir tokens for naming hints
        sub_tokens = Counter()
        for f in fs[:20]:
            parts = f.split("/")
            if d in ("/".join(parts[:1]), "/".join(parts[:2])):
                # Capture the segment after the bucket prefix
                bucket_depth = len(d.split("/"))
                if len(parts) > bucket_depth:
                    sub_tokens[parts[bucket_depth]] += 1
        sub_hint = ", ".join(t for t, _ in sub_tokens.most_common(4))
        line = f"  {d}/  ({len(fs)} files)"
        if sub_hint:
            line += f"  [contains: {sub_hint}]"
        lines.append(line)
    if len(sorted_dirs) > max_lines:
        lines.append(f"  ... +{len(sorted_dirs) - max_lines} more dirs")

    summary = "\n".join(lines)
    return summary, dict(bucket)


# ── Pass 1: bucket planning via Sonnet ────────────────────────────────


_PASS1_SYSTEM_PROMPT = """\
You are a senior software architect. You will receive a directory \
tree summary of a software repo. Group the directories into 8-15 \
LOGICAL PRODUCT BUCKETS (a 'bucket' = one cohesive product domain).

Output rules:
  1. Each bucket has a kebab-case name describing the domain
     (e.g. ``authentication``, ``billing``, ``api-gateway``,
     ``shared-infra``).
  2. Each bucket maps to one or more dirs from the summary.
  3. Every dir from the input MUST appear in exactly one bucket.
  4. Aim for 8-15 buckets total. Less than 8 = too coarse for a
     dashboard; more than 15 = too fine for an executive summary.
  5. Use bucket names domain-specific to THIS repo, not generic
     placeholders. Avoid: "core", "common", "utils", "main", "lib",
     "shared" (unless truly the only choice for tooling/infra).
  6. The ``shared-infra`` bucket is OK for: build configs, CI files,
     tooling, deps, and tiny dirs (<5 files) without a clear domain.

Return ONLY this JSON shape (no prose, no markdown fence):

{
  "buckets": [
    {"name": "auth", "dirs": ["packages/auth", "apps/web/auth"]},
    {"name": "billing", "dirs": ["packages/billing"]},
    ...
  ]
}
"""


def _api_call_pass1(
    summary: str, *, api_key: str | None, model: str,
) -> dict | None:
    """Make Pass-1 Anthropic API call. Returns parsed JSON or None."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("hierarchical: no ANTHROPIC_API_KEY")
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        logger.warning("hierarchical: anthropic package missing")
        return None

    client = Anthropic(api_key=api_key)
    user_msg = f"Directory tree summary:\n\n{summary}"
    try:
        resp = client.messages.create(
            model=model,
            system=_PASS1_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=DEFAULT_MAX_TOKENS,
            temperature=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("hierarchical: pass-1 call failed (%s)", exc)
        return None

    text = ""
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", "") == "text":
            text += getattr(block, "text", "")

    # Strip code-fence if present
    text = re.sub(r"^```(?:json)?\s*\n", "", text.strip())
    text = re.sub(r"\n```\s*$", "", text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def plan_buckets(
    files: list[str],
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
) -> list[Bucket]:
    """Pass 1 — group ``files`` into logical buckets via Sonnet.

    Returns list of buckets, each carrying its name + dirs + files.
    Falls back to a single ``shared-infra`` bucket on API failure
    so the caller can still produce a (degraded) result.
    """
    if not files:
        return []
    summary, dir_to_files = summarise_layout(files)
    parsed = _api_call_pass1(summary, api_key=api_key, model=model)
    if not parsed or not isinstance(parsed.get("buckets"), list):
        logger.warning(
            "hierarchical: Pass-1 failed; falling back to single bucket",
        )
        return [Bucket(name="shared-infra", dirs=list(dir_to_files), files=files)]

    raw_buckets = parsed["buckets"]
    buckets: list[Bucket] = []
    seen_dirs: set[str] = set()
    for entry in raw_buckets:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip().lower()
        dirs = entry.get("dirs") or []
        if not name or not isinstance(dirs, list):
            continue
        bucket_files: list[str] = []
        for d in dirs:
            if not isinstance(d, str):
                continue
            d_stripped = d.rstrip("/")
            if d_stripped in seen_dirs:
                continue
            files_in_dir = dir_to_files.get(d_stripped, [])
            bucket_files.extend(files_in_dir)
            seen_dirs.add(d_stripped)
        if bucket_files:
            buckets.append(Bucket(name=name, dirs=dirs, files=bucket_files))

    # Catch any dirs the LLM forgot
    leftover_dirs = [d for d in dir_to_files if d not in seen_dirs]
    if leftover_dirs:
        leftover_files: list[str] = []
        for d in leftover_dirs:
            leftover_files.extend(dir_to_files[d])
        if leftover_files:
            existing = next(
                (b for b in buckets if b.name == "shared-infra"), None,
            )
            if existing:
                existing.dirs.extend(leftover_dirs)
                existing.files.extend(leftover_files)
            else:
                buckets.append(Bucket(
                    name="shared-infra",
                    dirs=leftover_dirs,
                    files=leftover_files,
                ))

    if not buckets:
        return [Bucket(name="shared-infra", dirs=list(dir_to_files), files=files)]

    n = len(buckets)
    if n < _BUCKET_FLOOR:
        logger.info(
            "hierarchical: pass-1 produced %d buckets (below floor %d)",
            n, _BUCKET_FLOOR,
        )
    elif n > _BUCKET_CEILING:
        logger.info(
            "hierarchical: pass-1 produced %d buckets (above ceiling %d)",
            n, _BUCKET_CEILING,
        )
    return buckets


# ── Diagnostic helper for debugging Pass 1 ────────────────────────────


def describe_buckets(buckets: list[Bucket]) -> str:
    lines = [f"{len(buckets)} buckets:"]
    for b in buckets:
        lines.append(
            f"  {b.name:<28} dirs={len(b.dirs):>3} files={len(b.files):>5}"
        )
    return "\n".join(lines)
