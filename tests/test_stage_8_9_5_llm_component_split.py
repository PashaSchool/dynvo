"""Tests for Stage 8.9.5 — LLM-semantic component-blob decomposition.

Covers the gate (oversized + component fan-out), the LLM-label → split
(product domains become sub-features, UI groupings stay residual), file
conservation, caching, and the OFF-by-default / no-client no-ops.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature
from faultline.pipeline_v2.stage_8_9_5_llm_component_split import (
    _build_prompt,
    _component_fanout,
    _parse_labels,
    llm_component_split,
)

_ENV = "FAULTLINE_STAGE_8_9_5_LLM_COMPONENT_SPLIT"


def _feat(name, paths, *, uuid="u"):
    return Feature(
        name=name,
        description=None,
        paths=list(paths),
        authors=[],
        total_commits=7,
        bug_fixes=1,
        bug_fix_ratio=0.1,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        layer="developer",
        product_feature_id=None,
        uuid=uuid,
    )


# ── fake Anthropic client ───────────────────────────────────────────────────


class _FakeMsg:
    def __init__(self, text: str) -> None:
        self.content = [type("B", (), {"text": text})()]


class _FakeClient:
    """Returns a fixed label map; counts calls."""

    def __init__(self, labels: dict, *, raw: str | None = None) -> None:
        self.calls = 0
        outer = self

        class _Messages:
            def create(self, **_kw):
                outer.calls += 1
                return _FakeMsg(raw if raw is not None else json.dumps(labels))

        self.messages = _Messages()


class _DictCache:
    def __init__(self) -> None:
        self.store: dict[tuple, object] = {}

    def get(self, kind, key):
        return self.store.get((kind, key))

    def set(self, kind, key, value):
        self.store[(kind, key)] = value


# ── fixtures: a plane-like component blob + small peer features ──────────────


def _plane_like():
    blob_paths = (
        [f"apps/web/core/components/issues/I{i}.tsx" for i in range(30)]
        + [f"apps/web/core/components/cycles/C{i}.tsx" for i in range(10)]
        + [f"apps/web/core/components/modules/M{i}.tsx" for i in range(10)]
        + [f"apps/web/core/components/dropdowns/D{i}.tsx" for i in range(5)]
        + [f"apps/web/core/components/icons/Ic{i}.tsx" for i in range(5)]
    )
    web = _feat("web", blob_paths, uuid="web")
    # Small peer features → median grain ~3 so `web` is clearly oversized.
    peers = [
        _feat(f"peer{i}", [f"packages/p{i}/a.ts", f"packages/p{i}/b.ts",
                           f"packages/p{i}/c.ts"], uuid=f"p{i}")
        for i in range(8)
    ]
    return [web] + peers


_LABELS = {
    "issues": "domain", "cycles": "domain", "modules": "domain",
    "dropdowns": "ui", "icons": "ui",
}


# ── gate / no-op tests ──────────────────────────────────────────────────────


def test_disabled_is_noop(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    feats = _plane_like()
    before = len(feats)
    res = llm_component_split(feats, client=_FakeClient(_LABELS))
    assert res.enabled is False
    assert res.features_split == 0
    assert len(feats) == before  # nothing minted


def test_no_client_no_split(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    feats = _plane_like()
    before = len(feats)
    res = llm_component_split(
        feats, client=None, _client_factory=lambda: None,
    )
    assert res.enabled is True
    assert res.llm_calls == 0
    assert res.features_split == 0
    assert len(feats) == before


def test_not_oversized_is_skipped(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    # A small components feature (not oversized) must NOT be split.
    feats = [
        _feat("small", [f"src/components/issues/I{i}.tsx" for i in range(3)]
              + [f"src/components/icons/Ic{i}.tsx" for i in range(2)]),
        _feat("a", ["x/a.ts", "x/b.ts", "x/c.ts", "x/d.ts"]),
        _feat("b", ["y/a.ts", "y/b.ts", "y/c.ts", "y/d.ts"]),
    ]
    res = llm_component_split(feats, client=_FakeClient(_LABELS))
    assert res.features_split == 0


# ── main split test ─────────────────────────────────────────────────────────


def test_splits_domains_keeps_ui_residual(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    feats = _plane_like()
    original_paths = {p for f in feats for p in f.paths}
    client = _FakeClient(_LABELS)

    res = llm_component_split(feats, client=client, repo_slug="plane")

    assert res.enabled is True
    assert client.calls == 1                 # ONE call for the blob
    assert res.features_split == 1
    assert res.subfeatures_created == 3      # issues, cycles, modules
    assert res.paths_moved == 50             # 30 + 10 + 10
    assert res.domains_labelled == 3
    assert res.groupings_labelled == 2

    names = {f.name for f in feats}
    # The three product domains became their own features…
    assert any("issues" in n for n in names)
    assert any("cycles" in n for n in names)
    assert any("modules" in n for n in names)

    # …and the source `web` shed them, keeping only the UI groupings.
    web = next(f for f in feats if f.uuid == "web")
    web_owned = set(web.paths)
    assert all("dropdowns" in p or "icons" in p for p in web_owned), web_owned
    assert not any("/issues/" in p for p in web_owned)

    # File conservation — nothing lost across the whole feature set.
    after_paths = {p for f in feats for p in f.paths}
    assert original_paths <= after_paths


def test_caching_avoids_second_call(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    cache = _DictCache()
    client = _FakeClient(_LABELS)
    # First scan populates the cache.
    llm_component_split(_plane_like(), client=client, cache_backend=cache,
                        repo_slug="plane")
    assert client.calls == 1
    # Second scan with the SAME structure reuses the cached label — no new call.
    res2 = llm_component_split(_plane_like(), client=client,
                               cache_backend=cache, repo_slug="plane")
    assert client.calls == 1          # unchanged
    assert res2.cache_hits == 1
    assert res2.features_split == 1   # still splits from the cached label


# ── unit: fan-out detection ─────────────────────────────────────────────────


def test_component_fanout_finds_children():
    owned = [
        "apps/web/core/components/issues/a.tsx",
        "apps/web/core/components/issues/b.tsx",
        "apps/web/core/components/cycles/c.tsx",
        "apps/web/app/page.tsx",                 # not under components
    ]
    fan = _component_fanout(owned)
    assert fan is not None
    container, children = fan
    assert container == "apps/web/core/components"
    assert set(children) == {"issues", "cycles"}


def test_component_fanout_descends_to_nested_grouping():
    """v2: product areas under an intermediate grouping dir
    (supabase ``components/interfaces/<Area>``) are found at the DEEPER
    fan-out level, not lumped as a single ``interfaces`` child."""
    areas = ["Auth", "Database", "Storage", "Settings", "Reports"]
    owned = [
        f"apps/studio/components/interfaces/{a}/X{i}.tsx"
        for a in areas
        for i in range(3)
    ]
    # shallow siblings directly under components/ (fewer than the area fan-out)
    owned += [f"apps/studio/components/ui/U{i}.tsx" for i in range(2)]
    owned += [f"apps/studio/components/grid/G{i}.tsx" for i in range(2)]
    fan = _component_fanout(owned)
    assert fan is not None
    container, children = fan
    assert container == "apps/studio/components/interfaces"
    assert set(children) == set(areas)


def test_component_fanout_none_for_single_child():
    owned = ["pkg/components/Button/index.tsx", "pkg/components/Button/x.tsx"]
    assert _component_fanout(owned) is None  # one child < _MIN_COMPONENT_CHILDREN


def test_component_fanout_none_without_components():
    owned = ["src/lib/a.ts", "src/utils/b.ts"]
    assert _component_fanout(owned) is None


def test_component_fanout_tie_resolves_to_shallower():
    """On a distinct-child-count TIE, the SHALLOWER (components) level wins —
    the v1-safe direction (max() keeps the first-inserted key, and the shallow
    prefix is reached first on every path)."""
    owned = (
        [f"app/components/grouping/x/f{i}.tsx" for i in range(2)]
        + [f"app/components/grouping/y/f{i}.tsx" for i in range(2)]
        + [f"app/components/grouping/z/f{i}.tsx" for i in range(2)]
        + [f"app/components/a/f{i}.tsx" for i in range(2)]
        + [f"app/components/b/f{i}.tsx" for i in range(2)]
    )
    # components children {grouping, a, b} = 3; components/grouping {x,y,z} = 3 → tie
    container, children = _component_fanout(owned)
    assert container == "app/components"
    assert set(children) == {"grouping", "a", "b"}


def test_internals_descent_labelled_ui_does_not_split(monkeypatch):
    """v2 may NOMINATE a single component's internal subdirs as the fan-out
    (``components/DataTable/{hooks,utils,parts,…}``), but the LLM labels them
    ``ui`` → no domains → no junk split. The descent only nominates; the
    semantic gate disposes."""
    monkeypatch.setenv(_ENV, "1")
    internals = ("hooks", "utils", "parts", "styles", "context")
    blob_paths = [
        f"src/components/DataTable/{sub}/f{i}.tsx"
        for sub in internals
        for i in range(6)
    ]
    feats = [_feat("data-table", blob_paths, uuid="dt")]
    feats += [
        _feat(f"p{i}", [f"pkg/p{i}/a.ts", f"pkg/p{i}/b.ts", f"pkg/p{i}/c.ts"])
        for i in range(8)
    ]
    client = _FakeClient({s: "ui" for s in internals})
    res = llm_component_split(feats, client=client, repo_slug="x")
    assert res.candidates == 1       # the internals WERE nominated as a fan-out
    assert res.features_split == 0   # …but all-ui → no junk sub-features


# ── unit: label parsing ─────────────────────────────────────────────────────


def test_parse_labels_filters_and_strips_fence():
    children = {"issues": [], "icons": [], "cycles": []}
    text = '```json\n{"issues":"domain","icons":"ui","ghost":"domain"}\n```'
    out = _parse_labels(text, children)
    assert out == {"issues": "domain", "icons": "ui"}  # ghost dropped


def test_parse_labels_bad_json_empty():
    assert _parse_labels("not json", {"issues": []}) == {}


# ── blast-radius / edge labels ──────────────────────────────────────────────


def test_all_ui_labels_no_split(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    feats = _plane_like()
    before = len(feats)
    res = llm_component_split(
        feats,
        client=_FakeClient({c: "ui" for c in (
            "issues", "cycles", "modules", "dropdowns", "icons",
        )}),
    )
    assert res.features_split == 0      # nothing is a domain → no split
    assert len(feats) == before


def test_all_domain_conserves_and_keeps_residual(monkeypatch):
    """Bad-model worst case: every child labelled domain. Zero-path
    protection must keep the source non-empty and conserve every file."""
    monkeypatch.setenv(_ENV, "1")
    feats = _plane_like()
    original = {p for f in feats for p in f.paths}
    res = llm_component_split(
        feats,
        client=_FakeClient({c: "domain" for c in (
            "issues", "cycles", "modules", "dropdowns", "icons",
        )}),
    )
    assert res.features_split == 1
    # All five children are domains; the source keeps the SMALLEST as residual.
    web = next(f for f in feats if f.uuid == "web")
    assert len(web.paths) >= 1          # never emptied
    after = {p for f in feats for p in f.paths}
    assert original <= after            # conservation


def test_zero_path_protection_keeps_smallest_domain(monkeypatch):
    """A blob whose owned files are ALL product domains (no UI residual):
    every file would move, so the smallest domain is kept as the residual."""
    monkeypatch.setenv(_ENV, "1")
    blob = _feat(
        "web",
        [f"apps/web/components/issues/I{i}.tsx" for i in range(30)]
        + [f"apps/web/components/cycles/C{i}.tsx" for i in range(10)]
        + [f"apps/web/components/modules/M{i}.tsx" for i in range(8)],
        uuid="web",
    )
    peers = [
        _feat(f"p{i}", [f"pkg/p{i}/a.ts", f"pkg/p{i}/b.ts", f"pkg/p{i}/c.ts"])
        for i in range(8)
    ]
    feats = [blob] + peers
    original = {p for f in feats for p in f.paths}
    res = llm_component_split(
        feats,
        client=_FakeClient(
            {"issues": "domain", "cycles": "domain", "modules": "domain"},
        ),
    )
    assert res.features_split == 1
    assert res.subfeatures_created == 2   # 3 domains − 1 kept as residual
    web = next(f for f in feats if f.uuid == "web")
    assert len(web.paths) == 8            # smallest domain (modules) kept
    after = {p for f in feats for p in f.paths}
    assert original <= after              # conservation, nothing lost
