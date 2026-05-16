"""Pipeline stage snapshot tests (Sprint 9f refactor guard).

These tests pin the **current** end-to-end output of each pipeline
stage so the upcoming Sprint 9f refactor cannot change observable
behaviour without an explicit, reviewed update of the on-disk
fixtures.

How the snapshot loop works
---------------------------

1. Each test computes the stage's output for a small synthetic input.
2. The output is normalised (timestamps replaced with placeholders,
   pydantic models dumped via ``model_dump(mode="json")``) and
   compared against a JSON fixture under
   ``tests/fixtures/stage-snapshots/``.
3. If the fixture file does not yet exist OR the env var
   ``FAULTLINE_UPDATE_SNAPSHOTS=1`` is set, the test WRITES the
   current output as the new fixture and passes. Otherwise it READS
   the fixture and asserts equality.

Updating fixtures intentionally
-------------------------------

When you intentionally change a stage's behaviour:

    cd /Users/pkuzina/workspace/github-playground/featuremap
    FAULTLINE_UPDATE_SNAPSHOTS=1 ./.venv/bin/python -m pytest \
        tests/test_pipeline_stage_snapshots.py -v --no-cov -p no:warnings
    git diff tests/fixtures/stage-snapshots/

Review the diff carefully before committing — every line is a
behaviour change.

Selective run:

    pytest -m snapshot

Stages NOT covered (documented gaps)
------------------------------------

- Critique loop (``faultline.aggregators.critique``) and the LLM
  recall-critique runner: both REQUIRE a live Claude/Gemini call.
  Mocking them produces a snapshot that pins the mock, not the
  pipeline behaviour.
- ``feature_dedup`` Phase 2 (LLM verification of ambiguous pairs):
  same reason — only the deterministic Phase 1 (Jaccard cluster
  merge) is snapshotted here.
- ``apply_llm_canonicalization``: opt-in behind
  ``FAULTLINE_LLM_CANONICALIZE=1`` and LLM-bound; the
  deterministic ``apply_nav_labels`` + ``strip_page_suffix`` passes
  ARE covered.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest


# ── Configuration ─────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "stage-snapshots"
UPDATE_MODE = os.environ.get("FAULTLINE_UPDATE_SNAPSHOTS", "").lower() in {
    "1", "true", "yes", "on",
}

# Marker so the suite can be selected/excluded in isolation.
pytestmark = pytest.mark.snapshot


# ── Helpers ───────────────────────────────────────────────────────────


def _now() -> datetime:
    # Fixed timestamp — fixtures normalise it anyway, but keeping it
    # constant makes mid-run repr() debugging easier.
    return datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _normalise(obj: Any) -> Any:
    """Recursively replace non-deterministic fields with placeholders.

    - ISO 8601 datetimes → ``"<timestamp>"``
    - Absolute paths under ``/tmp``, ``/var``, ``/private`` (pytest
      tmp_path) → ``"<tmp>/..."``
    - ``MappingProxyType`` → plain dict
    """
    if isinstance(obj, MappingProxyType):
        obj = dict(obj)
    if isinstance(obj, dict):
        return {k: _normalise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalise(v) for v in obj]
    if isinstance(obj, str):
        # Pydantic ISO timestamps
        if len(obj) >= 19 and obj[4] == "-" and obj[7] == "-" and obj[10] in {"T", " "}:
            try:
                datetime.fromisoformat(obj.replace("Z", "+00:00"))
                return "<timestamp>"
            except ValueError:
                pass
        # tmp_path leakage
        for prefix in ("/private/var/folders/", "/var/folders/", "/tmp/"):
            if obj.startswith(prefix):
                # Keep only the trailing repo-relative remainder if any
                tail = obj[len(prefix):]
                # drop leading random pytest dir component(s)
                parts = tail.split("/", 2)
                return "<tmp>/" + (parts[-1] if len(parts) > 1 else "")
        return obj
    return obj


def _dump_pydantic(model) -> dict[str, Any]:
    return _normalise(model.model_dump(mode="json"))


def _dump_signal(sig) -> dict[str, Any]:
    return {
        "kind": sig.kind,
        "source": sig.source,
        "payload": _normalise(dict(sig.payload)),
    }


def _check_snapshot(name: str, payload: Any) -> None:
    """Write or compare a JSON fixture."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / f"{name}.json"
    serialised = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    if UPDATE_MODE or not path.exists():
        path.write_text(serialised + "\n", encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8").rstrip("\n")
    assert serialised == expected, (
        f"Snapshot drift for {name!r}. Re-run with "
        f"FAULTLINE_UPDATE_SNAPSHOTS=1 if the change is intentional."
    )


def _feat(name, *, paths=None, flows=None, display_name=None,
          discovery_method="primary", protected=False,
          protection_reason=None, description=None):
    from faultline.models.types import Feature
    return Feature(
        name=name, display_name=display_name, description=description,
        paths=list(paths or []), authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_now(), health_score=99.0,
        flows=list(flows or []), discovery_method=discovery_method,
        protected=protected, protection_reason=protection_reason,
    )


def _flow(name, paths):
    from faultline.models.types import Flow
    return Flow(
        name=name, paths=list(paths), authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=_now(),
        health_score=99.0,
    )


def _fm(features):
    from faultline.models.types import FeatureMap
    return FeatureMap(
        repo_path="/tmp/x", analyzed_at=_now(),
        total_commits=0, date_range_days=365, features=features,
    )


# ── Synthetic repo fixture ────────────────────────────────────────────


@pytest.fixture
def synthetic_repo(tmp_path: Path) -> Path:
    """Build a small, deterministic on-disk repo covering Remix routes,
    a tRPC router, a workspace package, Go top-level + subpackage files,
    a Python subpackage, and test files in two languages.
    """
    root = tmp_path / "repo"
    root.mkdir()

    files = {
        # Remix routes (flat-routes folder syntax)
        "apps/web/app/routes/_authenticated+/inbox.tsx":
            "export default function Inbox() { return null }\n",
        "apps/web/app/routes/_authenticated+/documents._index.tsx":
            "export default function Documents() { return null }\n",
        # tRPC router
        "packages/trpc/server/templates-router/router.ts":
            "export const templatesRouter = router({});\n",
        # Workspace package
        "packages/auth/server/index.ts":
            "export const auth = {};\n",
        "packages/auth/package.json":
            '{"name": "@repo/auth", "version": "0.0.1"}\n',
        # Go files
        "mux.go": "package main\n",
        "middleware/logger.go": "package middleware\n",
        "middleware/recoverer.go": "package middleware\n",
        # Python subpackage
        "fastapi/__init__.py": "",
        "fastapi/security/__init__.py": "from .oauth import *\n",
        # Tests
        "tests/billing.test.ts": "test('billing', () => {});\n",
        "tests/test_security.py": "def test_security(): pass\n",
        # Manifest at root so workspace package extractor fires
        "package.json": '{"name": "synthetic", "workspaces": ["packages/*"]}\n',
    }
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


# ── 1. Extractors snapshot ────────────────────────────────────────────


def test_extractors_snapshot(synthetic_repo: Path):
    """gather_signals(repo) must produce a stable Signal list across
    refactors. Snapshot pins kinds + payloads emitted by every
    applicable extractor for the synthetic repo.
    """
    from faultline.llm.recall_critique_runner import gather_signals

    signals = gather_signals(synthetic_repo)
    payload = sorted(
        (_dump_signal(s) for s in signals),
        key=lambda d: (d["source"], d["kind"], json.dumps(d["payload"], sort_keys=True)),
    )
    # Strip the absolute synthetic_repo prefix from any payload value.
    repo_str = str(synthetic_repo)
    def _strip_paths(node):
        if isinstance(node, dict):
            return {k: _strip_paths(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_strip_paths(v) for v in node]
        if isinstance(node, str) and repo_str in node:
            return node.replace(repo_str, "<repo>")
        return node
    payload = _strip_paths(payload)
    _check_snapshot("01_extractors", payload)


# ── 2. Feature protection snapshot ────────────────────────────────────


def test_feature_protection_snapshot():
    """``mark_protected`` must continue to flip the same features for
    the same structural-anchor patterns. Five candidates exercise
    five distinct pattern families.
    """
    from faultline.aggregators.feature_protection import mark_protected

    feats = [
        _feat("templates", paths=[
            "packages/trpc/server/templates-router/router.ts"]),
        _feat("auth", paths=["packages/auth/server/index.ts"]),
        _feat("inbox", paths=[
            "apps/web/app/routes/_authenticated+/inbox.tsx"]),
        _feat("security", paths=["fastapi/security/__init__.py"]),
        _feat("documenso-noise", paths=["random/file/elsewhere.ts"]),
    ]
    fm = _fm(feats)
    new_fm, reasons = mark_protected(fm)
    payload = {
        "reasons": reasons,
        "features": [_dump_pydantic(f) for f in new_fm.features],
    }
    _check_snapshot("02_feature_protection", payload)


# ── 3. Feature dedup (deterministic only) snapshot ────────────────────


def test_feature_dedup_snapshot():
    """Phase 1 deterministic dedup: 3 auth duplicates collapse, 2
    unrelated features survive untouched. LLM phase NOT exercised
    (llm=None) — see module docstring for why.
    """
    from faultline.aggregators.feature_dedup import dedup_features

    feats = [
        _feat("authentication", paths=["packages/auth/server/a.ts"]),
        _feat("auth-system", paths=["packages/auth/server/b.ts"]),
        _feat("auth-management", paths=["packages/auth/server/c.ts"]),
        _feat("billing", paths=["packages/billing/index.ts"]),
        _feat("templates", paths=["packages/templates/index.ts"]),
    ]
    fm = _fm(feats)
    fm, stats = dedup_features(fm, llm=None)
    payload = {
        "stats": {
            "pairs_high": stats.pairs_high,
            "pairs_ambiguous": stats.pairs_ambiguous,
            "pairs_llm_merged": stats.pairs_llm_merged,
            "features_before": stats.features_before,
            "features_after": stats.features_after,
            "clusters_merged": stats.clusters_merged,
        },
        "features": sorted(
            (_dump_pydantic(f) for f in fm.features),
            key=lambda d: d["name"],
        ),
    }
    _check_snapshot("03_feature_dedup", payload)


# ── 4. Auto-split snapshot ────────────────────────────────────────────


def test_auto_split_snapshot():
    """A 50-path 'authenticated' bucket with three rich sub-segments
    (billing/, profile/, settings/) must split into three children.
    """
    from faultline.aggregators.auto_split import split_oversized_features

    paths: list[str] = []
    for seg in ("billing", "profile", "settings"):
        paths.extend(
            f"apps/web/routes/dashboard/{seg}/page{i:02d}.tsx"
            for i in range(15)
        )
    # Pad with five extra noise paths under a fourth segment that
    # falls below the per-segment threshold so we also pin the
    # "small group rejected" behaviour.
    paths.extend(
        f"apps/web/routes/dashboard/misc/page{i}.tsx" for i in range(5)
    )
    flows = [_flow(f"f-{i:03d}", [paths[i]]) for i in range(len(paths))]
    feat = _feat("authenticated", paths=paths, flows=flows)
    fm = _fm([feat])
    fm, stats = split_oversized_features(fm)
    payload = {
        "stats": {
            "features_split": stats.features_split,
            "new_features": stats.new_features,
        },
        "features": sorted(
            ({"name": f.name,
              "display_name": f.display_name,
              "path_count": len(f.paths),
              "flow_count": len(f.flows),
              "protected": f.protected}
             for f in fm.features),
            key=lambda d: d["name"],
        ),
    }
    _check_snapshot("04_auto_split", payload)


# ── 5. Display-name canonicalizer (deterministic) snapshot ────────────


def test_display_name_canonicalizer_snapshot():
    """Pin the deterministic Pass A: nav-label match + strip_page_suffix.
    A "Settings Page" label must canonicalise to "Settings".
    """
    from faultline.aggregators.display_name_canonicalizer import (
        apply_nav_labels, strip_page_suffix,
    )
    from faultline.signals import Signal

    feats = [
        _feat("settings", display_name="Settings Page"),
        _feat("billing", display_name="Billing"),
        _feat("inbox", display_name="inbox"),  # engine-generated
    ]
    fm = _fm(feats)
    nav_signals = [
        Signal(kind="nav-link", source="jsx-nav",
               payload={"href": "/settings", "label": "Settings"}),
        Signal(kind="nav-link", source="jsx-nav",
               payload={"href": "/billing", "label": "Billing"}),
        Signal(kind="nav-link", source="jsx-nav",
               payload={"href": "/inbox", "label": "Inbox"}),
    ]
    nav_updates = apply_nav_labels(fm, nav_signals)
    fm, suffix_updates = strip_page_suffix(fm)
    payload = {
        "nav_updates": nav_updates,
        "suffix_updates": suffix_updates,
        "features": sorted(
            ({"name": f.name, "display_name": f.display_name}
             for f in fm.features),
            key=lambda d: d["name"],
        ),
    }
    _check_snapshot("05_display_name_canonicalizer", payload)
