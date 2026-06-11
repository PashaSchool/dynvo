"""Tests for ``run_pipeline_multi`` — engine(repo, subpaths[]) Phase 1.

Gates covered here:

  - E2E equivalence: a multi run over [sp1, sp2] produces FeatureMap
    JSONs identical to two independent ``run_pipeline_v2(subpath=spX)``
    runs, modulo run_id / timestamps / elapsed / artifact-dir / uuids /
    ``shared_git_pass``. Strict order-sensitive diff (the engine is
    order-stable since PR #36).
  - Git-call count: ``get_commits`` runs ONCE for a 3-subpath multi
    scan (vs 3 with the legacy loop).
  - Fail-loud: a bogus subpath errors its own entry; the others
    succeed; the error is recorded (keep-going semantics).

All runs are keyless (Stage 3/4 monkeypatched, Stage 0.5 auditor falls
back deterministically) with an isolated ``$HOME`` per run so no state
leaks between the runs being compared.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

import importlib

from faultline.analyzer import git as git_module
from faultline.pipeline_v2 import git_snapshot as snapshot_module
from faultline.pipeline_v2 import run as run_module
from faultline.pipeline_v2.multi import MultiScanResult, run_pipeline_multi
from faultline.pipeline_v2.run import run_pipeline_v2
from faultline.pipeline_v2.stage_3_flows import Stage3Result
from faultline.pipeline_v2.stage_4_residual import Stage4Result

# The package __init__ re-exports the ``stage_0_intake`` FUNCTION under
# the same name as the module, so ``from ... import stage_0_intake``
# yields the function — go through importlib for the module itself.
intake_module = importlib.import_module(
    "faultline.pipeline_v2.stage_0_intake",
)

DAYS = 3650


# ── Fixture repo ─────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, check=True,
    )


def _commit_files(root: Path, files: dict[str, str], msg: str) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", msg)


@pytest.fixture()
def monorepo(tmp_path: Path) -> Path:
    root = tmp_path / "mono"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")
    _commit_files(
        root,
        {
            "package.json": '{"name": "mono"}',
            "apps/web/package.json": json.dumps(
                {"name": "web", "dependencies": {"next": "14.0.0"}},
            ),
            "apps/web/next.config.js": "module.exports = {};\n",
            "apps/web/app/billing/page.tsx": "export default function P() { return null; }\n",
            "apps/web/app/auth/page.tsx": "export default function P() { return null; }\n",
            "apps/worker/package.json": '{"name": "worker"}',
            "apps/worker/src/index.ts": "console.log(1)\n",
            "apps/api/package.json": '{"name": "api"}',
            "apps/api/src/server.ts": "console.log(2)\n",
        },
        "feat: initial monorepo",
    )
    _commit_files(
        root,
        {"apps/web/app/billing/page.tsx": "export default function P() { return 2; }\n"},
        "fix: billing page",
    )
    _commit_files(
        root,
        {"apps/worker/src/jobs.ts": "export const j = 1\n"},
        "feat: worker jobs",
    )
    return root


# ── Keyless pipeline (no network) ────────────────────────────────────


@pytest.fixture()
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch Stage 3 + Stage 4 to canned results — deterministic, $0."""

    def _fake_stage_3(features: Any, ctx: Any, *, model: str, cost_tracker: Any, **_kw: Any) -> Stage3Result:
        from faultline.pipeline_v2.stage_3_flows import FeatureWithFlows
        return Stage3Result(
            features_with_flows=[
                FeatureWithFlows(feature=f, flows=[], rationale="patched")
                for f in features
            ],
            cost_usd=0.0,
            llm_calls=0,
            warnings=[],
        )

    def _fake_stage_4(unattributed: Any, ctx: Any, existing: Any, *, model: str, cost_tracker: Any, **_kw: Any) -> Stage4Result:
        return Stage4Result(
            residual_features=[],
            cost_usd=0.0,
            llm_calls=0,
            warnings=[],
            clusters_total=0,
            clusters_processed=0,
            saturation_stopped=False,
            rejected_names=[],
        )

    monkeypatch.setattr(run_module, "stage_3_flows", _fake_stage_3)
    monkeypatch.setattr(run_module, "stage_4_residual", _fake_stage_4)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _isolated_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))


# ── JSON normalization for the strict diff ───────────────────────────

_HEX32 = re.compile(r"^[0-9a-f]{32}$")

# Volatile scan_meta keys: per-run identifiers, wall-clock, artifact
# locations, and the additive shared-git-pass marker under test.
_VOLATILE_SCAN_META = {
    "run_id", "elapsed_sec", "stage_artifact_dir", "shared_git_pass",
    "stage_6_3_elapsed_sec",
}


def _normalize(doc: dict[str, Any]) -> str:
    """Serialize a FeatureMap dict with volatile fields canonicalized.

    uuid4s are minted fresh per scan even on the legacy path, so two
    independent legacy runs already differ in uuids; we canonicalize
    them in document-encounter order (the engine is order-stable, so
    equivalent scans yield the same encounter order).
    """
    doc = json.loads(json.dumps(doc))  # deep copy
    doc["analyzed_at"] = "T"
    sm = doc.get("scan_meta") or {}
    for key in _VOLATILE_SCAN_META:
        sm.pop(key, None)
    # Nested elapsed inside stage_6_3 / stage_6_4 / stage_6_6 blocks.
    for block in ("stage_6_3", "stage_6_4", "stage_6_6"):
        if isinstance(sm.get(block), dict):
            sm[block].pop("elapsed_sec", None)

    blob = json.dumps(doc, sort_keys=False)

    # Canonicalize uuids in encounter order.
    mapping: dict[str, str] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and _HEX32.match(v) and (
                    "uuid" in k or k in ("id", "feature_uuid", "flow_uuid")
                ):
                    mapping.setdefault(v, f"UUID-{len(mapping)}")
                else:
                    _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(doc)
    for raw, canon in mapping.items():
        blob = blob.replace(raw, canon)
    return blob


# ── E2E equivalence gate ─────────────────────────────────────────────


def test_multi_equals_independent_runs(
    monorepo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_llm: None,
) -> None:
    subpaths = ["apps/web", "apps/worker"]

    # Multi run — one shared snapshot, isolated HOME.
    with monkeypatch.context() as mp:
        _isolated_home(mp, tmp_path / "home-multi")
        results = run_pipeline_multi(monorepo, subpaths, days=DAYS)
    assert [r.subpath for r in results] == subpaths
    assert all(r.error is None for r in results), [r.error for r in results]
    multi_docs = {
        r.subpath: json.loads(Path(str(r.out_path)).read_text())
        for r in results
    }
    # Each multi result consumed the shared snapshot.
    for r in results:
        assert r.result is not None
        assert r.result.get("shared_git_pass") is True
        assert r.result.get("subpath") == r.subpath

    # Two independent legacy runs — fresh isolated HOME each.
    legacy_docs: dict[str, dict[str, Any]] = {}
    for sp in subpaths:
        with monkeypatch.context() as mp:
            _isolated_home(mp, tmp_path / f"home-legacy-{sp.replace('/', '_')}")
            res = run_pipeline_v2(monorepo, days=DAYS, subpath=sp)
        assert "shared_git_pass" not in res
        legacy_docs[sp] = json.loads(Path(res["path"]).read_text())

    for sp in subpaths:
        assert _normalize(multi_docs[sp]) == _normalize(legacy_docs[sp]), (
            f"FeatureMap divergence for subpath={sp}"
        )


# ── Git-call-count gate ──────────────────────────────────────────────


def test_multi_runs_get_commits_exactly_once(
    monorepo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_llm: None,
) -> None:
    _isolated_home(monkeypatch, tmp_path / "home")

    calls = {"count": 0}
    real_get_commits = git_module.get_commits

    def _spy(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        return real_get_commits(*args, **kwargs)

    # Patch every import site that could trigger a history parse.
    monkeypatch.setattr(snapshot_module, "get_commits", _spy)
    monkeypatch.setattr(intake_module, "get_commits", _spy)
    monkeypatch.setattr(git_module, "get_commits", _spy)

    results = run_pipeline_multi(
        monorepo, ["apps/web", "apps/worker", "apps/api"], days=DAYS,
    )
    assert all(r.error is None for r in results), [r.error for r in results]
    assert calls["count"] == 1, (
        f"expected ONE shared git pass, saw {calls['count']}"
    )


def test_legacy_loop_runs_get_commits_per_subpath(
    monorepo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_llm: None,
) -> None:
    """Control for the spy: 3 independent runs = 3 history parses."""
    _isolated_home(monkeypatch, tmp_path / "home")

    calls = {"count": 0}
    real_get_commits = git_module.get_commits

    def _spy(*args: Any, **kwargs: Any) -> Any:
        calls["count"] += 1
        return real_get_commits(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "get_commits", _spy)
    monkeypatch.setattr(intake_module, "get_commits", _spy)

    for sp in ("apps/web", "apps/worker", "apps/api"):
        run_pipeline_v2(monorepo, days=DAYS, subpath=sp)
    assert calls["count"] == 3


# ── Fail-loud / keep-going gate ──────────────────────────────────────


def test_multi_records_bogus_subpath_and_keeps_going(
    monorepo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_llm: None,
) -> None:
    _isolated_home(monkeypatch, tmp_path / "home")

    results = run_pipeline_multi(
        monorepo, ["apps/web", "apps/nope", "apps/worker"], days=DAYS,
    )
    by_sp = {r.subpath: r for r in results}
    assert by_sp["apps/web"].error is None
    assert by_sp["apps/worker"].error is None
    bogus = by_sp["apps/nope"]
    assert bogus.error is not None
    assert "SubpathScopeError" in bogus.error
    assert bogus.out_path is None and bogus.result is None


def test_multi_requires_subpaths(monorepo: Path) -> None:
    with pytest.raises(ValueError):
        run_pipeline_multi(monorepo, [])


def test_multi_progress_hooks_fire_in_order(
    monorepo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_llm: None,
) -> None:
    _isolated_home(monkeypatch, tmp_path / "home")

    events: list[tuple[str, str]] = []
    results = run_pipeline_multi(
        monorepo,
        ["apps/web", "apps/worker"],
        days=DAYS,
        on_subpath_start=lambda sp: events.append(("start", sp)),
        on_subpath_end=lambda r: events.append(("end", r.subpath)),
    )
    assert isinstance(results[0], MultiScanResult)
    assert events == [
        ("start", "apps/web"), ("end", "apps/web"),
        ("start", "apps/worker"), ("end", "apps/worker"),
    ]
