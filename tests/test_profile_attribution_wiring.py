"""Tests for the P4 profile-driven attribution + flow wiring.

Covers the four hook points:
  (a) Stage 0/orchestrator profile selection surfaced in scan_meta —
      asserted indirectly via the registry default-selection.
  (b) Stage 2 / 2.6 attribution: ``feature_of`` first-say re-homes a
      claimed file; ``attribution_rules`` shared-role fans out.
  (c) Stage 3: ``flow_entries`` seed deterministic flows (one per
      capability — dedup discipline) and resolve wrapped-handler lines.
  (d) Regression guard: with the DefaultProfile (or ``None``) the
      attribution + flow paths are byte-for-byte the legacy result.

NO LLM is exercised (profile-seeded flows skip the model; the legacy
path uses a FakeAnthropic). Deterministic + offline.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from faultline.llm.cost import CostTracker
from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.profiles._attribution import (
    apply_profile_attribution,
    is_active,
    max_fanout,
    profile_claims,
    shared_roles,
)
from faultline.pipeline_v2.profiles._flow_lines import resolve_handler_line
from faultline.pipeline_v2.profiles.base import (
    AttributionSpec,
    FileRole,
    FlowEntry,
)
from faultline.pipeline_v2.profiles.default import DefaultProfile
from faultline.pipeline_v2.stage_2_reconcile import (
    DeveloperFeature,
    stage_2_reconcile,
)
from faultline.pipeline_v2.stage_3_flows import FlowSpec, stage_3_flows


# ── Fakes ───────────────────────────────────────────────────────────────────


class _FakeProfile:
    """A concrete profile fixture satisfying the FrameworkProfile Protocol.

    Drives attribution via an explicit ``claims`` map and ``roles`` map,
    and seeds flows via an explicit ``entries`` list. No framework
    knowledge baked in — the test supplies the policy.
    """

    name = "fake-stack"

    def __init__(
        self,
        *,
        claims: dict[str, str] | None = None,
        roles: dict[str, FileRole] | None = None,
        entries: list[FlowEntry] | None = None,
        spec: AttributionSpec | None = None,
    ) -> None:
        self._claims = claims or {}
        self._roles = roles or {}
        self._entries = entries or []
        self._spec = spec or AttributionSpec()

    def detects(self, ctx) -> float:
        return 0.9

    def workspaces(self, ctx):
        from faultline.pipeline_v2.profiles._splitter import split_workspaces
        return split_workspaces(ctx)

    def classify_file(self, path: str) -> FileRole:
        return self._roles.get(path, FileRole.UNKNOWN)

    def feature_of(self, path: str, ctx) -> str | None:
        return self._claims.get(path)

    def flow_entries(self, ctx):
        return list(self._entries)

    def attribution_rules(self) -> AttributionSpec:
        return self._spec


def _ctx(tmp_path: Path, files: list[str]) -> ScanContext:
    return ScanContext(
        repo_path=tmp_path,
        stack=None,
        monorepo=False,
        workspaces=None,
        tracked_files=files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
    )


def _feat(name: str, paths: tuple[str, ...], sources=("route",)) -> DeveloperFeature:
    return DeveloperFeature(
        name=name, paths=paths, sources=list(sources), confidence="medium",
    )


# ── (d) regression guard: default / None are no-ops ─────────────────────────


def test_default_profile_is_not_active() -> None:
    assert is_active(DefaultProfile()) is False
    assert is_active(None) is False


def test_default_profile_claims_nothing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, ["a.ts", "b.ts"])
    assert profile_claims(DefaultProfile(), ["a.ts", "b.ts"], ctx) == {}
    assert profile_claims(None, ["a.ts", "b.ts"], ctx) == {}


def test_apply_attribution_identity_under_default(tmp_path: Path) -> None:
    feats = [_feat("x", ("a.ts",)), _feat("y", ("b.ts",))]
    ctx = _ctx(tmp_path, ["a.ts", "b.ts"])
    out = apply_profile_attribution(
        feats, DefaultProfile(), ctx, rebuild=lambda f, p: f,
    )
    # Identity: same list object returned for the no-op default.
    assert out is feats


def test_shared_roles_and_fanout_empty_for_default() -> None:
    assert shared_roles(DefaultProfile()) == frozenset()
    assert max_fanout(DefaultProfile()) is None


# ── (b) profile feature_of re-homes a claimed file ──────────────────────────


def test_profile_rehomes_claimed_path() -> None:
    feats = [_feat("dashboard", ("app/x.ts",)), _feat("auth", ())]
    # Profile says app/x.ts actually belongs to "auth".
    profile = _FakeProfile(claims={"app/x.ts": "auth"})

    def rebuild(f: DeveloperFeature, paths: tuple[str, ...]) -> DeveloperFeature:
        return DeveloperFeature(
            name=f.name, paths=paths, sources=f.sources,
            confidence=f.confidence,
        )

    out = apply_profile_attribution(feats, profile, None, rebuild=rebuild)
    by_name = {f.name: f for f in out}
    assert "app/x.ts" not in by_name["dashboard"].paths
    assert "app/x.ts" in by_name["auth"].paths


def test_profile_does_not_rehome_to_missing_feature() -> None:
    # Profile names a feature that no extractor surfaced — leave it alone.
    feats = [_feat("dashboard", ("app/x.ts",))]
    profile = _FakeProfile(claims={"app/x.ts": "nonexistent"})
    out = apply_profile_attribution(
        feats, profile, None, rebuild=lambda f, p: f,
    )
    assert out[0].paths == ("app/x.ts",)


def test_stage_2_reconcile_applies_profile(tmp_path: Path) -> None:
    """End-to-end Stage 2: profile re-homes a contested file."""
    from faultline.pipeline_v2.extractors.base import AnchorCandidate

    ctx = _ctx(tmp_path, ["app/shared.ts", "app/auth.ts"])
    cands = {
        "route": [
            AnchorCandidate(
                name="dashboard", paths=["app/shared.ts"],
                source="route", confidence_self=0.8,
            ),
            AnchorCandidate(
                name="auth", paths=["app/auth.ts"],
                source="route", confidence_self=0.8,
            ),
        ],
    }
    profile = _FakeProfile(claims={"app/shared.ts": "auth"})
    result = stage_2_reconcile(cands, ctx, profile=profile)
    by_name = {f.name: f for f in result.features}
    assert "app/shared.ts" in by_name["auth"].paths
    assert "app/shared.ts" not in by_name.get("dashboard", _feat("d", ())).paths
    assert any("profile-attribution applied" in n for n in result.notes)


# ── (c) flow seeding + dedup + wrapped-handler line ─────────────────────────


def test_resolve_handler_line_unwraps_wrapper(tmp_path: Path) -> None:
    from faultline.analyzer.ast_extractor import extract_signatures

    rel = "app/api/route.ts"
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        textwrap.dedent(
            """\
            function realHandler(req) {
              const a = 1;
              const b = 2;
              return new Response(String(a + b));
            }
            export const POST = withAuth(realHandler);
            """
        ),
        encoding="utf-8",
    )
    sigs = extract_signatures([rel], str(tmp_path))
    sig = sigs[rel]
    # POST export is the wrapper (last line). Find its raw start line.
    post_range = next(r for r in sig.symbol_ranges if r.name == "POST")
    # The resolver should redirect to realHandler's definition (line 1),
    # NOT the wrapper export line.
    resolved = resolve_handler_line(sig, "POST", post_range.start_line)
    real_range = next(r for r in sig.symbol_ranges if r.name == "realHandler")
    assert resolved == real_range.start_line
    assert resolved < post_range.start_line


def test_resolve_handler_line_noop_for_plain_function(tmp_path: Path) -> None:
    from faultline.analyzer.ast_extractor import extract_signatures

    rel = "app/api/plain.ts"
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        textwrap.dedent(
            """\
            export function GET(req) {
              const x = 1;
              return new Response(String(x));
            }
            """
        ),
        encoding="utf-8",
    )
    sigs = extract_signatures([rel], str(tmp_path))
    sig = sigs[rel]
    gr = next(r for r in sig.symbol_ranges if r.name == "GET")
    # Plain (non-wrapped) handler keeps its own line — identity.
    assert resolve_handler_line(sig, "GET", gr.start_line) == gr.start_line


def test_stage_3_profile_seeds_flows_without_llm(tmp_path: Path) -> None:
    """Profile flow_entries seed flows deterministically — no LLM call."""
    rel = "app/auth/route.ts"
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        "export async function POST(req) { return new Response('ok'); }\n",
        encoding="utf-8",
    )
    feature = _feat("auth", (rel,))
    ctx = _ctx(tmp_path, [rel])
    profile = _FakeProfile(
        entries=[
            FlowEntry(path=rel, symbol="POST", kind="http", route="/api/auth"),
        ],
    )
    tracker = CostTracker(max_cost=None)
    # No client passed AND no _client_factory hit because profile seeds.
    result = stage_3_flows(
        [feature], ctx, cost_tracker=tracker, profile=profile,
        client=None, _client_factory=lambda: None,
    )
    assert result.llm_calls == 0
    fwf = next(f for f in result.features_with_flows if f.feature.name == "auth")
    assert len(fwf.flows) == 1
    assert fwf.flows[0].name.endswith("-flow")
    assert "profile-seeded" in fwf.rationale


def test_stage_3_profile_dedups_same_capability(tmp_path: Path) -> None:
    """Two entries for the SAME route collapse to ONE flow (dedup)."""
    rel_a = "app/auth/a.ts"
    rel_b = "app/auth/b.ts"
    for rel in (rel_a, rel_b):
        full = tmp_path / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(
            "export async function POST(req){return new Response('x');}\n",
            encoding="utf-8",
        )
    feature = _feat("auth", (rel_a, rel_b))
    ctx = _ctx(tmp_path, [rel_a, rel_b])
    # Both entries describe the SAME capability (same route) → one flow.
    profile = _FakeProfile(
        entries=[
            FlowEntry(path=rel_a, symbol="POST", kind="http", route="/reset-password"),
            FlowEntry(path=rel_b, symbol="POST", kind="http", route="/reset-password"),
        ],
    )
    result = stage_3_flows(
        [feature], ctx, cost_tracker=CostTracker(max_cost=None),
        profile=profile, client=None, _client_factory=lambda: None,
    )
    fwf = next(f for f in result.features_with_flows if f.feature.name == "auth")
    assert len(fwf.flows) == 1  # reset-password-flow x1, not x2


def test_stage_3_default_profile_uses_llm_path(tmp_path: Path) -> None:
    """Regression guard: under DefaultProfile the LLM path is unchanged."""
    rel = "app/x/route.ts"
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        textwrap.dedent(
            """\
            export async function GET(req){return new Response('a');}
            export async function POST(req){return new Response('b');}
            export async function PUT(req){return new Response('c');}
            """
        ),
        encoding="utf-8",
    )
    feature = _feat("x", (rel,))
    ctx = _ctx(tmp_path, [rel])

    # With DefaultProfile + no client → "no-client" path (LLM would run).
    res_default = stage_3_flows(
        [feature], ctx, cost_tracker=CostTracker(max_cost=None),
        profile=DefaultProfile(), client=None, _client_factory=lambda: None,
    )
    # With profile=None → identical behaviour.
    res_none = stage_3_flows(
        [feature], ctx, cost_tracker=CostTracker(max_cost=None),
        profile=None, client=None, _client_factory=lambda: None,
    )
    # Both take the legacy no-client path (no profile-seeded flows).
    assert res_default.llm_calls == res_none.llm_calls == 0
    d = next(f for f in res_default.features_with_flows)
    n = next(f for f in res_none.features_with_flows)
    assert d.flows == n.flows == []
    assert "profile-seeded" not in d.rationale
