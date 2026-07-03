"""G4 — reusable inertness-proof template (StackProfile spec, Phase A).

A profile that does NOT win selection for a repo must be provably
unable to change ANYTHING about that repo's scan: registering it vs
not registering it yields byte-identical pipeline output (normalized
for the run-scoped fields the determinism work left volatile:
``analyzed_at`` / ``run_id`` / timings) AND an identical scan-result
cache key.

Every new profile ships one test built on this template::

    def test_my_profile_inert_on_foreign_repo(tmp_path, monkeypatch):
        repo = make_fixture_repo(tmp_path, NON_MATCHING_FIXTURE)
        assert_profile_inert(repo, MyProfile(), tmp_path, monkeypatch)

Mechanics: the pipeline discovers profiles via
``profiles._registry.discover_profiles`` (run.py → ``select_profile``
→ ``ProfileRegistry(None)``), so the template swaps that discovery
seam — the exact seam a real registration goes through.

Scans run in-process with LLM env stripped (every LLM stage degrades
deterministically without a key) and a fresh ``FAULTLINES_RUN_DIR`` per
run so no cache/state can bleed between the two variants.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from faultline.pipeline_v2.profiles import DefaultProfile
from faultline.pipeline_v2.profiles import _registry as registry_mod
from faultline.pipeline_v2.run import run_pipeline_v2
from faultline.tools.normalize_scan import scan_digest

#: A tiny NON-Next repo: plain Python service files. No ``next.config``,
#: no ``app/(page|route).tsx``, no ``next`` dependency — no concrete
#: profile should score it above the default floor.
NON_NEXT_FIXTURE: dict[str, str] = {
    "requirements.txt": "flask\n",
    "src/app.py": (
        "def create_app():\n"
        "    return {'routes': ['/health', '/users']}\n"
    ),
    "src/users.py": (
        "def list_users():\n"
        "    return []\n"
        "\n"
        "def create_user(name):\n"
        "    return {'name': name}\n"
    ),
    "src/util.py": "def slug(s):\n    return s.lower().replace(' ', '-')\n",
    "tests/test_users.py": (
        "from src.users import list_users\n"
        "def test_list():\n    assert list_users() == []\n"
    ),
}

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    # Fixed, well-in-the-past date: both scan variants see identical
    # history AND day-granular age math cannot flip mid-test.
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def make_fixture_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Materialise ``files`` as a one-commit git repo under ``tmp_path``."""
    repo = tmp_path / "fixture-repo"
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    import os

    env = {**os.environ, **_GIT_ENV}
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "fixture"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    return repo


def _scan_with_profiles(
    repo: Path,
    profiles: list[Any],
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """One deterministic in-process scan with a controlled profile set."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FAULTLINES_RUN_DIR", str(state_dir))
    monkeypatch.setenv("FAULTLINE_SCAN_CACHE", "0")
    monkeypatch.setenv("FAULTLINE_STAGE_0_5_CACHE", "0")
    monkeypatch.setenv("FAULTLINE_STAGE_6_7B_CACHE", "0")
    monkeypatch.setenv("FAULTLINE_STAGE_6_7C_CACHE", "0")
    monkeypatch.setenv("FAULTLINE_STAGE_8_CACHE", "0")
    # The discovery seam run.py's select_profile goes through.
    monkeypatch.setattr(
        registry_mod, "discover_profiles", lambda: list(profiles)
    )
    out = state_dir / "scan.json"
    run_pipeline_v2(repo, out_path=out, run_id="inertness")
    return json.loads(out.read_text(encoding="utf-8"))


def _scan_cache_key(repo: Path) -> str:
    """The top-level scan-result cache key for this repo + default config."""
    from faultline.pipeline_v2 import scan_result_cache as sc
    from faultline.pipeline_v2.run import (
        _IMPORT_TREE_MAX_DEPTH,
        DEFAULT_MODEL,
        resolve_model,
    )

    signature = sc.scan_config_signature(
        model=resolve_model(DEFAULT_MODEL),
        days=365,
        subpath=None,
        max_tree_depth=_IMPORT_TREE_MAX_DEPTH,
        llm_reconcile=False,
        feature_history=True,
    )
    return sc.compute_scan_cache_key(
        repo,
        engine_version=sc.engine_version(),
        config_signature=signature,
    )


def assert_profile_inert(
    repo: Path,
    profile: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    expect_selected: str = "default",
) -> None:
    """Scan ``repo`` with ``profile`` unregistered vs registered.

    Asserts:
      * both scans select ``expect_selected`` (the candidate never wins),
      * normalized output is byte-identical (``scan_digest`` equality —
        the same equality the snapshot gate enforces),
      * the scan-result cache key is identical (registration must not
        perturb cache identity).
    """
    without_dir = tmp_path / "state-without"
    with_dir = tmp_path / "state-with"
    without_dir.mkdir()
    with_dir.mkdir()

    key_before = _scan_cache_key(repo)
    doc_without = _scan_with_profiles(
        repo, [DefaultProfile()], without_dir, monkeypatch
    )
    doc_with = _scan_with_profiles(
        repo, [DefaultProfile(), profile], with_dir, monkeypatch
    )
    key_after = _scan_cache_key(repo)

    meta_without = doc_without.get("scan_meta") or {}
    meta_with = doc_with.get("scan_meta") or {}
    assert meta_without.get("framework_profile") == expect_selected
    assert meta_with.get("framework_profile") == expect_selected, (
        f"{getattr(profile, 'name', profile)!r} unexpectedly WON selection "
        "on the non-matching fixture — inertness cannot hold"
    )

    digest_without = scan_digest(doc_without)
    digest_with = scan_digest(doc_with)
    assert digest_without == digest_with, (
        f"registering {getattr(profile, 'name', profile)!r} changed the "
        f"pipeline output on a repo it does not match:\n"
        f"  without: {digest_without}\n  with:    {digest_with}"
    )
    assert key_before == key_after, "scan cache key drifted across registration"
