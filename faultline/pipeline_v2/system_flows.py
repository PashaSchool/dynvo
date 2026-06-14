"""Deterministic system / background-flow classification (Stage 6.8 helper).

A route — and through it each ``user_flow`` — is a SYSTEM flow when it is
triggered by a scheduler, a queue/worker, or an inbound webhook rather than by a
person navigating the product. The patterns live in ``system-flow-patterns.yaml``
(authoring copy ``eval/system-flow-patterns.yaml``; runtime copy
``faultline/pipeline_v2/data/system-flow-patterns.yaml`` — kept byte-identical).
Per stack-pattern-library, anything hardcoded in Python is a bug; this module
only *applies* the patterns.

No LLM, no network. SELF-DETECTING: a repo with no cron manifest and no job
markers classifies every route ``interactive`` (a clean no-op), so non-job repos
are byte-identical.

Trigger vocabulary: ``scheduled`` | ``queue`` | ``webhook`` | ``interactive``.
The coarse axis (system vs interactive) is what downstream consumers gate on;
the sub-trigger is best-effort provenance.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from faultline.pipeline_v2.data import load_yaml

_PATTERNS_FILE = "system-flow-patterns.yaml"

TRIGGER_INTERACTIVE = "interactive"
# Precedence order — first match wins (scheduled is the most specific signal).
_SYSTEM_TRIGGERS = ("scheduled", "queue", "webhook")

# Bytes of a handler file scanned for a content marker. Job handlers declare
# their trigger near the top (imports + middleware wrapper); cap the read so a
# large generated handler can't dominate the pass.
_MARKER_SCAN_BYTES = 4096


def load_patterns() -> dict[str, Any]:
    """Load the runtime pattern file (``{}`` if absent → classifier no-ops)."""
    try:
        return load_yaml(_PATTERNS_FILE) or {}
    except FileNotFoundError:
        return {}


def _norm_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    return ("/" + u.strip("/")).rstrip("/") or "/"


def _file_to_url(file_path: str) -> str:
    """Derive an App-Router URL from a route file path.

    ``apps/web/app/api/watch/all/route.ts`` -> ``/api/watch/all``. Route groups
    ``(group)`` are dropped. Returns "" for a path with no ``app/`` segment.
    """
    parts = file_path.replace("\\", "/").split("/")
    if "app" not in parts:
        return ""
    idx = len(parts) - 1 - parts[::-1].index("app")  # last "app" segment
    segs = [
        s for s in parts[idx + 1:]
        if not (s.startswith("(") and s.endswith(")"))
    ]
    if segs and re.match(r"(route|page)\.[A-Za-z]+$", segs[-1]):
        segs = segs[:-1]
    return _norm_url("/".join(segs))


class SystemFlowClassifier:
    """Classify a route handler's ``trigger`` from the pattern config.

    ``repo_path`` is optional: without it, cron-manifest and content-marker
    signals are unavailable (path-glob classification still works).
    """

    def __init__(self, repo_path: Any = None, patterns: dict | None = None) -> None:
        self.repo = Path(repo_path) if repo_path else None
        cfg = patterns if patterns is not None else load_patterns()
        self._path_res: dict[str, list[re.Pattern[str]]] = {}
        for trig in _SYSTEM_TRIGGERS:
            compiled: list[re.Pattern[str]] = []
            for rx in (cfg.get("path_globs", {}) or {}).get(trig) or []:
                try:
                    compiled.append(re.compile(rx))
                except re.error:
                    continue
            self._path_res[trig] = compiled
        self._markers: dict[str, list[str]] = {
            trig: [m.lower() for m in ((cfg.get("content_markers", {}) or {}).get(trig) or [])]
            for trig in _SYSTEM_TRIGGERS
        }
        self._cron_urls = self._load_cron_urls(cfg.get("cron_manifests") or {})
        self._head_cache: dict[str, str] = {}

    def _load_cron_urls(self, manifests: dict[str, Any]) -> set[str]:
        urls: set[str] = set()
        if not self.repo:
            return urls
        for spec in manifests.values():
            if not isinstance(spec, dict):
                continue
            fg = spec.get("file_glob")
            ck = spec.get("crons_key", "crons")
            pk = spec.get("path_key", "path")
            if not fg:
                continue
            for mf in self.repo.glob(fg):
                try:
                    doc = json.loads(mf.read_text(encoding="utf-8", errors="ignore"))
                except (ValueError, OSError):
                    continue
                for c in (doc.get(ck) or []) if isinstance(doc, dict) else []:
                    if isinstance(c, dict) and c.get(pk):
                        urls.add(_norm_url(str(c[pk])))
        return urls

    def _read_head(self, file_path: str) -> str:
        if file_path in self._head_cache:
            return self._head_cache[file_path]
        text = ""
        if self.repo:
            try:
                raw = (self.repo / file_path).read_text(encoding="utf-8", errors="ignore")
                text = raw[:_MARKER_SCAN_BYTES].lower()
            except OSError:
                text = ""
        self._head_cache[file_path] = text
        return text

    def classify(self, file_path: str, route_pattern: str | None = None) -> str:
        """Return the trigger for a route handler file (default ``interactive``)."""
        # 1. cron manifest — authoritative (a cron handler may carry no marker)
        if self._cron_urls:
            for u in (_norm_url(route_pattern or ""), _file_to_url(file_path)):
                if u and u in self._cron_urls:
                    return "scheduled"
        # 2. path-segment conventions
        for trig in _SYSTEM_TRIGGERS:
            if any(rx.search(file_path) for rx in self._path_res.get(trig, ())):
                return trig
        # 3. handler content markers
        if file_path and any(self._markers.values()):
            head = self._read_head(file_path)
            if head:
                for trig in _SYSTEM_TRIGGERS:
                    if any(m in head for m in self._markers.get(trig, ())):
                        return trig
        return TRIGGER_INTERACTIVE

    def is_system(self, file_path: str, route_pattern: str | None = None) -> bool:
        return self.classify(file_path, route_pattern) != TRIGGER_INTERACTIVE


def classify_routes(routes_index: list[dict[str, Any]], repo_path: Any = None) -> dict[str, int]:
    """Stamp ``trigger`` onto each routes_index entry in place.

    Returns a small telemetry counter ``{trigger: count}`` (system triggers
    only) for scan_meta.
    """
    clf = SystemFlowClassifier(repo_path)
    counts: dict[str, int] = {}
    for entry in routes_index:
        trig = clf.classify(str(entry.get("file") or ""), entry.get("pattern"))
        entry["trigger"] = trig
        if trig != TRIGGER_INTERACTIVE:
            counts[trig] = counts.get(trig, 0) + 1
    return counts


__all__ = ["SystemFlowClassifier", "classify_routes", "load_patterns", "TRIGGER_INTERACTIVE"]
