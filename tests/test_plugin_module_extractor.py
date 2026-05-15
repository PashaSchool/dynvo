"""Tests for the universal plugin-system extractor (Phase 5 Layer C).

Uses neutral synthetic directory layouts per the
no-repo-specific-paths rule. Test fixtures are created on the fly
in tmp_path so no real repo's structure is encoded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.extractors.plugin_module import (
    MIN_SIBLINGS,
    PluginModuleExtractor,
    detect_plugin_dirs,
)
from faultline.protocols import Extractor


# ── Helpers ───────────────────────────────────────────────────────────


def _mk_plugin_dir(
    root: Path,
    rel_dir: str,
    *,
    n_peers: int = 12,
    base_name: str = "base",
    ext: str = ".py",
    prefix: str = "Notify",
) -> Path:
    """Create a synthetic plugin directory with n peers + base file."""
    d = root / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{base_name}{ext}").write_text(f"class {base_name.capitalize()}:\n    pass\n")
    for i in range(n_peers):
        (d / f"{prefix}{i:02d}{ext}").write_text(
            f"from .{base_name} import {base_name.capitalize()}\n"
            f"class {prefix}{i:02d}({base_name.capitalize()}):\n    pass\n"
        )
    return d


# ── detect_plugin_dirs ────────────────────────────────────────────────


def test_detects_plugin_dir_with_base_and_many_peers(tmp_path):
    _mk_plugin_dir(tmp_path, "src/notifiers", n_peers=12)
    out = detect_plugin_dirs(tmp_path)
    assert len(out) == 12
    assert all(m.plugin_dir == "src/notifiers" for m in out)
    assert {m.name for m in out} == {f"Notify{i:02d}" for i in range(12)}
    assert all(m.base_stem == "base" for m in out)
    assert all(m.extension == ".py" for m in out)


def test_skips_dir_below_min_siblings(tmp_path):
    """Below the threshold, the structural signature is too weak."""
    _mk_plugin_dir(tmp_path, "src/few", n_peers=MIN_SIBLINGS - 2)
    assert detect_plugin_dirs(tmp_path) == []


def test_skips_dir_without_base_module(tmp_path):
    """Many peers but no base/abstract → not a plugin dir."""
    d = tmp_path / "src/utils"
    d.mkdir(parents=True)
    for i in range(12):
        (d / f"helper_{i:02d}.py").write_text("pass\n")
    assert detect_plugin_dirs(tmp_path) == []


def test_recognises_alternate_base_stem(tmp_path):
    _mk_plugin_dir(tmp_path, "src/providers", base_name="abstract")
    out = detect_plugin_dirs(tmp_path)
    assert len(out) == 12
    assert out[0].base_stem == "abstract"


def test_skips_mixed_extension_dirs(tmp_path):
    """A dir with .py and .ts mixed at < 70% dominant ext is rejected."""
    d = tmp_path / "src/mixed"
    d.mkdir(parents=True)
    (d / "base.py").write_text("class Base: pass\n")
    for i in range(6):
        (d / f"plugin_{i}.py").write_text("pass\n")
    for i in range(6):
        (d / f"plugin_{i}.ts").write_text("export {}\n")
    assert detect_plugin_dirs(tmp_path) == []


def test_typescript_plugin_dir(tmp_path):
    _mk_plugin_dir(tmp_path, "src/adapters", ext=".ts", prefix="Adapter")
    out = detect_plugin_dirs(tmp_path)
    assert len(out) == 12
    assert all(m.extension == ".ts" for m in out)
    assert {m.name for m in out} == {f"Adapter{i:02d}" for i in range(12)}


def test_skips_node_modules_and_other_skip_dirs(tmp_path):
    _mk_plugin_dir(tmp_path, "node_modules/somepkg/src", n_peers=12)
    _mk_plugin_dir(tmp_path, "tests/fixtures/notifiers", n_peers=12)
    assert detect_plugin_dirs(tmp_path) == []


def test_skips_low_prefix_consistency(tmp_path):
    """Random unrelated names → fails the prefix consistency check."""
    d = tmp_path / "src/random"
    d.mkdir(parents=True)
    (d / "base.py").write_text("class Base: pass\n")
    # 12 totally unrelated names
    names = [
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
        "golf", "hotel", "india", "juliet", "kilo", "lima",
    ]
    for n in names:
        (d / f"{n}.py").write_text("pass\n")
    assert detect_plugin_dirs(tmp_path) == []


def test_drops_base_module_from_emitted_plugins(tmp_path):
    """The base file itself is not a plugin; it's the marker."""
    d = _mk_plugin_dir(tmp_path, "src/x", n_peers=12)
    out = detect_plugin_dirs(tmp_path)
    names = {m.name for m in out}
    assert "base" not in names
    assert "Base" not in names


def test_multiple_plugin_dirs_in_one_repo(tmp_path):
    _mk_plugin_dir(tmp_path, "src/notifiers", prefix="Notify", n_peers=12)
    _mk_plugin_dir(tmp_path, "src/storages", prefix="Storage", n_peers=12)
    out = detect_plugin_dirs(tmp_path)
    dirs = {m.plugin_dir for m in out}
    assert dirs == {"src/notifiers", "src/storages"}
    assert len(out) == 24


# ── Extractor wrapper ─────────────────────────────────────────────────


def test_extractor_conforms_to_protocol():
    ext = PluginModuleExtractor()
    assert isinstance(ext, Extractor)


def test_extractor_emits_one_signal_per_plugin_dir(tmp_path):
    """Phase 5 Layer C: extractor groups peers into ONE
    ``plugin-system`` signal per directory — not N per-file signals.
    Ground-truth feature lists describe plugin systems as ONE
    horizontal capability, so this matches the right abstraction.
    """
    _mk_plugin_dir(tmp_path, "src/integrations", n_peers=12)
    sigs = PluginModuleExtractor().extract(tmp_path, files=())
    assert len(sigs) == 1
    assert sigs[0].kind == "plugin-system"
    assert sigs[0].source == "plugin-module-extractor"
    payload = sigs[0].payload
    assert payload["plugin_dir"] == "src/integrations"
    assert payload["peer_count"] == 12
    assert payload["base_stem"] == "base"
    assert payload["extension"] == ".py"
    assert len(payload["sample_names"]) <= 8


def test_extractor_emits_one_signal_per_distinct_plugin_dir(tmp_path):
    _mk_plugin_dir(tmp_path, "src/notifiers", prefix="Notify", n_peers=12)
    _mk_plugin_dir(tmp_path, "src/storages", prefix="Storage", n_peers=12)
    sigs = PluginModuleExtractor().extract(tmp_path, files=())
    assert len(sigs) == 2
    dirs = {s.payload["plugin_dir"] for s in sigs}
    assert dirs == {"src/notifiers", "src/storages"}


def test_extractor_applicable_false_on_empty_repo(tmp_path):
    assert PluginModuleExtractor().applicable(tmp_path) is False


def test_extractor_applicable_true_when_plugin_dir_present(tmp_path):
    _mk_plugin_dir(tmp_path, "any/dir/here", n_peers=12)
    assert PluginModuleExtractor().applicable(tmp_path) is True
