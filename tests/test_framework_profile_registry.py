"""Phase 1 — FrameworkProfile abstraction + registry.

Deterministic tests on the Protocol contract, the default profile, and
the register / lookup / select / discover registry. No real corpus
paths, no LLM, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.profiles import (
    AttributionSpec,
    DefaultProfile,
    FileRole,
    FlowEntry,
    FrameworkProfile,
    ProfileRegistry,
    discover_profiles,
    select_profile,
)
from faultline.pipeline_v2.profiles import _registry as registry_mod
from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── helpers ─────────────────────────────────────────────────────────────────


def _ctx(tmp_path: Path, *, stack: str | None = None) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=stack,
        monorepo=False,
        workspaces=None,
        tracked_files=["src/index.ts"],
        commits=[],
    )


class _FakeProfile:
    """A minimal concrete profile for register/lookup/select tests."""

    def __init__(self, name: str, score: float) -> None:
        self.name = name
        self._score = score

    def detects(self, ctx: ScanContext) -> float:
        return self._score

    def workspaces(self, ctx: ScanContext):
        return []

    def classify_file(self, path: str) -> FileRole:
        return FileRole.UNKNOWN

    def feature_of(self, path: str, ctx: ScanContext) -> str | None:
        return None

    def flow_entries(self, ctx: ScanContext):
        return []

    def attribution_rules(self) -> AttributionSpec:
        return AttributionSpec()


# ── Protocol / default profile ──────────────────────────────────────────────


def test_default_profile_satisfies_protocol() -> None:
    assert isinstance(DefaultProfile(), FrameworkProfile)


def test_fake_profile_satisfies_protocol() -> None:
    assert isinstance(_FakeProfile("x", 0.5), FrameworkProfile)


def test_default_profile_is_null_object(tmp_path: Path) -> None:
    p = DefaultProfile()
    ctx = _ctx(tmp_path)
    assert p.name == "default"
    assert 0.0 < p.detects(ctx) < 0.5  # positive floor, never strong
    assert p.classify_file("a/b/c.tsx") is FileRole.UNKNOWN
    assert p.feature_of("a/b/c.tsx", ctx) is None
    assert p.flow_entries(ctx) == []
    assert p.attribution_rules() == AttributionSpec()


def test_default_profile_workspaces_returns_root_for_single_repo(tmp_path: Path) -> None:
    ws = DefaultProfile().workspaces(_ctx(tmp_path))
    assert len(ws) == 1
    assert ws[0].name == "."
    assert ws[0].files == ["src/index.ts"]


# ── dataclasses ─────────────────────────────────────────────────────────────


def test_flow_entry_defaults() -> None:
    fe = FlowEntry(path="app/page.tsx")
    assert fe.symbol == "" and fe.kind == "" and fe.route == ""


def test_attribution_spec_defaults() -> None:
    spec = AttributionSpec()
    assert spec.colocate_roots == ()
    assert spec.shared_roles == ()
    assert spec.max_fanout is None


def test_filerole_serialises_as_str() -> None:
    assert FileRole.SERVICE == "service"
    assert FileRole.DOMAIN.value == "domain"


# ── registry: register / lookup ─────────────────────────────────────────────


def test_registry_lookup_default_present_by_default() -> None:
    reg = ProfileRegistry([])
    assert reg.get("default") is not None
    assert reg.default.name == "default"


def test_registry_register_and_get() -> None:
    reg = ProfileRegistry([])
    fake = _FakeProfile("nestjs", 0.9)
    reg.register(fake)
    assert reg.get("nestjs") is fake


def test_registry_register_rejects_non_protocol() -> None:
    reg = ProfileRegistry([])
    with pytest.raises(TypeError):
        reg.register(object())  # type: ignore[arg-type]


def test_registry_register_no_replace_keeps_existing() -> None:
    reg = ProfileRegistry([])
    first = _FakeProfile("dup", 0.1)
    second = _FakeProfile("dup", 0.9)
    reg.register(first)
    reg.register(second, replace=False)
    assert reg.get("dup") is first


def test_registry_unknown_name_returns_none() -> None:
    assert ProfileRegistry([]).get("does-not-exist") is None


# ── registry: selection ─────────────────────────────────────────────────────


def test_select_highest_detects_wins(tmp_path: Path) -> None:
    weak = _FakeProfile("weak", 0.2)
    strong = _FakeProfile("strong", 0.8)
    chosen = select_profile(_ctx(tmp_path), [weak, strong])
    assert chosen.name == "strong"


def test_select_falls_back_to_default_when_all_zero(tmp_path: Path) -> None:
    zero = _FakeProfile("zero", 0.0)
    chosen = select_profile(_ctx(tmp_path), [zero])
    assert chosen.name == "default"


def test_select_specific_beats_default_floor(tmp_path: Path) -> None:
    # 0.05 is tiny but still above the default's 0.01 floor.
    barely = _FakeProfile("barely", 0.05)
    chosen = select_profile(_ctx(tmp_path), [barely])
    assert chosen.name == "barely"


def test_select_tie_breaks_lexicographic_regardless_of_order(
    tmp_path: Path,
) -> None:
    """G1 — equal scores resolve by profile name, never insertion order."""
    alpha = _FakeProfile("alpha", 0.8)
    zulu = _FakeProfile("zulu", 0.8)
    assert select_profile(_ctx(tmp_path), [zulu, alpha]).name == "alpha"
    assert select_profile(_ctx(tmp_path), [alpha, zulu]).name == "alpha"


def test_select_is_order_independent_for_distinct_scores(tmp_path: Path) -> None:
    """G1 — permuting the registration order never changes the winner."""
    import itertools

    profiles = [
        _FakeProfile("a-weak", 0.2),
        _FakeProfile("m-strong", 0.9),
        _FakeProfile("z-mid", 0.5),
    ]
    for perm in itertools.permutations(profiles):
        assert select_profile(_ctx(tmp_path), list(perm)).name == "m-strong"


def test_select_returns_default_when_no_positive_score(tmp_path: Path) -> None:
    """G1 guard — a floorless registry still resolves to the default."""

    class _ZeroFloorDefault(_FakeProfile):
        pass

    reg = ProfileRegistry([_FakeProfile("nothing", 0.0)])
    chosen = reg.select(_ctx(tmp_path))
    assert chosen.name == "default"


def test_select_tie_with_default_floor_prefers_default_lexicographically(
    tmp_path: Path,
) -> None:
    """G1 — a profile that only EQUALS the default floor does not win.

    'default' sorts before almost any profile slug; matching the floor
    (0.01) is not a real detection signal, so the null-object keeps the
    repo. Beating the floor (see test_select_specific_beats_default_floor)
    is what flips selection.
    """
    barely = _FakeProfile("not-really-detected", 0.01)
    chosen = select_profile(_ctx(tmp_path), [barely])
    assert chosen.name == "default"


# ── discovery ───────────────────────────────────────────────────────────────


def test_discover_always_includes_default() -> None:
    names = [p.name for p in discover_profiles()]
    assert "default" in names


def test_discover_merges_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    class _EP:
        name = "custom-fw"

        @staticmethod
        def load():
            return lambda: _FakeProfile("custom-fw", 0.7)

    def _fake_entry_points(group: str | None = None):
        assert group == "faultlines.profiles"
        return [_EP()]

    monkeypatch.setattr(registry_mod, "entry_points", _fake_entry_points)
    names = [p.name for p in discover_profiles()]
    assert "default" in names
    assert "custom-fw" in names


def test_discover_builtin_wins_over_colliding_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = _FakeProfile("default", 0.99)  # tries to shadow the builtin

    class _EP:
        name = "default"

        @staticmethod
        def load():
            return lambda: sentinel

    monkeypatch.setattr(
        registry_mod, "entry_points", lambda group=None: [_EP()]
    )
    default_profile = next(p for p in discover_profiles() if p.name == "default")
    assert default_profile is not sentinel
    assert isinstance(default_profile, DefaultProfile)


def test_discover_tolerates_broken_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BadEP:
        name = "broken"

        @staticmethod
        def load():
            raise ImportError("boom")

    monkeypatch.setattr(
        registry_mod, "entry_points", lambda group=None: [_BadEP()]
    )
    names = [p.name for p in discover_profiles()]
    # broken entry-point skipped; built-ins survive (default + the
    # in-tree deterministic profiles registered in _load_default_profiles).
    assert "default" in names
    assert "broken" not in names
    assert "next-app-router" in names
