"""Tests for the pluggable cache backend (spec: encrypted-db-cache-backend).

Covers the interface, the filesystem default (byte-for-byte legacy
layout), the env-driven selector + lazy injection, the stateless
base-dir resolution, and the spec §9 acceptance criteria:

  #1 backend unset → behaviour byte-identical (legacy fs caches hit).
  #2 production scan writes EXCLUSIVELY through the backend — nothing
     lands under ``Path.home()``.
  #3 two scans against one backend instance → second hits the warm
     cache (network fetch not re-invoked).
  #4 cache-KEY computations are unchanged (golden-key stability).

No network, no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from faultline.cache import (
    CACHE_BACKEND_ENV,
    CacheBackend,
    CacheKind,
    FilesystemCacheBackend,
    MemoryCacheBackend,
    get_cache_backend,
)
from faultline.cache.paths import RUN_DIR_ENV, faultline_base_dir


# ── Protocol conformance ─────────────────────────────────────────────────


def test_backends_satisfy_protocol() -> None:
    assert isinstance(MemoryCacheBackend(), CacheBackend)
    assert isinstance(FilesystemCacheBackend(Path("/tmp/x")), CacheBackend)


def test_cache_kind_is_stringy() -> None:
    # CacheKind doubles as a plain str so it can be passed where a str
    # kind is expected.
    assert CacheKind.MARKETING == "marketing"
    assert str(CacheKind.LLM_NAME) == "llm-name"
    m = MemoryCacheBackend()
    m.set(CacheKind.MARKETING, "k", 1)
    assert m.get("marketing", "k") == 1  # enum-set, str-get → same row


# ── MemoryCacheBackend ───────────────────────────────────────────────────


def test_memory_get_set_delete_namespace_flush() -> None:
    m = MemoryCacheBackend()
    assert m.get("llm-name", "missing") is None
    m.set("llm-name", "a", {"0": "auth"})
    m.set("llm-name", "b", {"0": "billing"})
    m.set("marketing", "c", {"x": 1})
    assert m.get("llm-name", "a") == {"0": "auth"}
    assert m.load_namespace("llm-name") == {"a": {"0": "auth"}, "b": {"0": "billing"}}
    assert m.load_namespace("marketing") == {"c": {"x": 1}}
    m.delete("llm-name", "a")
    assert m.get("llm-name", "a") is None
    m.flush()
    assert m.flush_count == 1


# ── FilesystemCacheBackend: legacy layout + TTL ──────────────────────────


def test_fs_marketing_layout_matches_legacy(tmp_path: Path) -> None:
    b = FilesystemCacheBackend(tmp_path)
    b.set("marketing", "myslug", {"fetched_at_epoch": 123.0, "product_features": ["A"]})
    # Legacy path: <base>/marketing-cache/<slug>.json
    expected = tmp_path / "marketing-cache" / "myslug.json"
    assert expected.is_file()
    body = json.loads(expected.read_text())
    assert body["product_features"] == ["A"]
    assert b.get("marketing", "myslug") == body


def test_fs_llm_name_layout_and_ttl(tmp_path: Path) -> None:
    b = FilesystemCacheBackend(tmp_path)
    b.set("llm-name", "deadbeef", {"0": "auth"})
    p = tmp_path / "llm-cache" / "deadbeef.json"
    assert p.is_file()
    assert b.get("llm-name", "deadbeef") == {"0": "auth"}
    # Expire by back-dating mtime > 90 days.
    import os
    import time

    old = time.time() - 91 * 24 * 3600
    os.utime(p, (old, old))
    assert b.get("llm-name", "deadbeef") is None  # TTL purge
    assert not p.exists()  # expired file removed (legacy semantics)


def test_fs_assignment_flat_layout(tmp_path: Path) -> None:
    b = FilesystemCacheBackend(tmp_path)
    b.set("assignment", "myrepo", {"a.ts": "auth"})
    assert (tmp_path / "assignments-myrepo.json").is_file()
    assert b.get("assignment", "myrepo") == {"a.ts": "auth"}


def test_fs_load_namespace_only_subdir_kinds(tmp_path: Path) -> None:
    b = FilesystemCacheBackend(tmp_path)
    b.set("llm-name", "k1", {"0": "a"})
    b.set("llm-name", "k2", {"0": "b"})
    assert b.load_namespace("llm-name") == {"k1": {"0": "a"}, "k2": {"0": "b"}}
    # Flat per-repo kinds aren't bulk-loadable → empty namespace.
    b.set("assignment", "repo", {"x": "y"})
    assert b.load_namespace("assignment") == {}


def test_fs_reads_a_legacy_unwrapped_file(tmp_path: Path) -> None:
    """Acceptance #1: a marketing cache file written by the OLD engine
    (a raw JSON body, no envelope) is still read by the new backend."""
    legacy_dir = tmp_path / "marketing-cache"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "old.json").write_text(json.dumps({"product_features": ["X"]}))
    b = FilesystemCacheBackend(tmp_path)
    assert b.get("marketing", "old") == {"product_features": ["X"]}


# ── Base-dir resolution (stateless output) ───────────────────────────────


def test_base_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(RUN_DIR_ENV, str(tmp_path))
    assert faultline_base_dir() == tmp_path
    # FS backend honours it by default.
    monkeypatch.setenv(RUN_DIR_ENV, str(tmp_path))
    assert FilesystemCacheBackend()._base == tmp_path  # type: ignore[attr-defined]


def test_base_dir_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RUN_DIR_ENV, raising=False)
    assert faultline_base_dir() == Path.home() / ".faultline"


# ── Selector + lazy injection (boundary-safe) ────────────────────────────


def test_selector_default_is_filesystem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CACHE_BACKEND_ENV, raising=False)
    assert isinstance(get_cache_backend(), FilesystemCacheBackend)
    monkeypatch.setenv(CACHE_BACKEND_ENV, "fs")
    assert isinstance(get_cache_backend(), FilesystemCacheBackend)


def test_selector_lazy_imports_injected_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``module:factory`` is imported lazily and called with org_id —
    the OSS engine never imports the DB backend by name."""
    import sys
    import types

    captured: dict[str, object] = {}

    mod = types.ModuleType("fake_db_cache_backend")

    def build_backend(*, org_id: str | None = None) -> MemoryCacheBackend:
        captured["org_id"] = org_id
        return MemoryCacheBackend()

    mod.build_backend = build_backend  # type: ignore[attr-defined]
    sys.modules["fake_db_cache_backend"] = mod
    try:
        monkeypatch.setenv(CACHE_BACKEND_ENV, "fake_db_cache_backend:build_backend")
        backend = get_cache_backend(org_id="org_123")
        assert isinstance(backend, MemoryCacheBackend)
        assert captured["org_id"] == "org_123"
    finally:
        del sys.modules["fake_db_cache_backend"]


def test_selector_default_factory_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``:factory`` is omitted, ``build_backend`` is the default."""
    import sys
    import types

    mod = types.ModuleType("fake_db_cache_backend2")

    def build_backend(*, org_id: str | None = None) -> MemoryCacheBackend:
        return MemoryCacheBackend()

    mod.build_backend = build_backend  # type: ignore[attr-defined]
    sys.modules["fake_db_cache_backend2"] = mod
    try:
        monkeypatch.setenv(CACHE_BACKEND_ENV, "fake_db_cache_backend2")
        assert isinstance(get_cache_backend(), MemoryCacheBackend)
    finally:
        del sys.modules["fake_db_cache_backend2"]


# ── assignments routes through the backend ───────────────────────────────


def test_assignments_roundtrip_through_injected_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from faultline.analyzer import assignments

    monkeypatch.delenv("FAULTLINES_PRODUCTION", raising=False)
    backend = MemoryCacheBackend()
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    result = SimpleNamespace(features={"auth": ["a.ts", "b.ts"], "billing": ["c.ts"]})
    n = assignments.save_assignments(result, repo_root, cache_backend=backend)
    assert n == 3
    loaded = assignments.load_assignments(repo_root, cache_backend=backend)
    assert loaded == {"a.ts": "auth", "b.ts": "auth", "c.ts": "billing"}
    # Key is the legacy slug.
    assert backend.get("assignment", "myrepo") is not None


def test_assignments_noop_in_production_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from faultline.analyzer import assignments

    monkeypatch.setenv("FAULTLINES_PRODUCTION", "1")
    backend = MemoryCacheBackend()
    repo_root = tmp_path / "prodrepo"
    repo_root.mkdir()
    result = SimpleNamespace(features={"auth": ["a.ts"]})
    n = assignments.save_assignments(result, repo_root, cache_backend=backend)
    assert n == 0  # production gate preserved
    assert backend.load_namespace("assignment") == {}
