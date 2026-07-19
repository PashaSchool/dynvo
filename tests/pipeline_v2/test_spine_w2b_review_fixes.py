"""Wave 2b adversarial-review fixes (F1-F5) — regression tests.

F1/F5 [HIGH]: on the KEYED anchored path (mint applied + 6.7d applied),
``resolve_flowless_shells`` used to DEMOTE a large flowless anchored PF
into a freshly minted **"Shared Platform"** product feature (and its
JOIN rung re-homed minted-anchor devs against their own lineage) — the
exact bucket the 18:2x operator amendment abolished, surviving on a
keyed-only branch the keyless gates structurally could not reach. The
regression test drives the FULL pipeline (run_pipeline_v2: mint + a
scripted 6.7d client + phase_finalize sequence), not run_anchored_mint
alone.

F2: Call-1 citations of non-anchored capabilities (incl. the literal
"Shared Platform") are SCRUBBED, and the reshare ladder's PF-append
rungs (carve/docs) are forbidden in anchored mode — no anchor_id-less
PF can enter the fixed universe from those ladders.

F3: a cross-family vendor clash derives its slug FROM the qualified
display via ``canonical_slug`` — mint slug == 6.7d-rebuild slug, so hub
parity's lookup never silently skips the class.

F4: the platform_infrastructure lane admits ONLY the three amendment
reasons.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.models.types import Feature, Flow, MemberFile
from faultline.pipeline_v2 import run as run_module
from faultline.pipeline_v2.run import run_pipeline_v2
from faultline.pipeline_v2.stage_3_flows import Stage3Result
from faultline.pipeline_v2.stage_4_residual import Stage4Result

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _git_init_with_one_commit(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat: initial"], cwd=repo, check=True)


class _ScriptedAnthropic:
    """Fake Anthropic client: answers Call-1 with a grounded journey
    citing UF-001 + the `foo` dev; any other LLM consumer gets the same
    JSON and degrades gracefully on parse."""

    calls: list[str] = []

    def __init__(self, *a, **kw):
        self.messages = self

    def create(self, **kw):
        _ScriptedAnthropic.calls.append(str(kw.get("system", ""))[:60])
        payload = (
            '{"product_features":[{"name":"Foo","description":"x"}],'
            '"user_flows":[{"name":"Manage foo","resource":"foo",'
            '"product_feature":"Foo","from_flows":["UF-001"],'
            '"from_dev_features":["foo"]}]}'
        )
        return SimpleNamespace(
            content=[SimpleNamespace(text=payload)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=10),
        )


def _flowful_stage_3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage 3 double that gives the route feature ONE deterministic
    flow (so 6.7 emits UF-001 and 6.7d can ground the scripted draw);
    Stage 4 no-op."""

    def _fake_stage_3(features, ctx, *, model, cost_tracker, **_kw):
        from faultline.pipeline_v2.stage_3_flows import (
            FeatureWithFlows,
            FlowSpec,
        )
        out = []
        for f in features:
            flows = []
            if any(p.endswith("app/foo/page.tsx") for p in (f.paths or [])):
                flows = [FlowSpec(
                    name="edit-foo-flow",
                    description="edit foo",
                    entry_point_file="app/foo/page.tsx",
                    reach_paths=("app/foo/page.tsx",),
                    depth_reached=1,
                )]
            out.append(FeatureWithFlows(feature=f, flows=flows, rationale="t"))
        return Stage3Result(features_with_flows=out, cost_usd=0.0,
                            llm_calls=0, warnings=[])

    def _fake_stage_4(unattributed, ctx, existing, *, model, cost_tracker, **_kw):
        return Stage4Result(residual_features=[], cost_usd=0.0, llm_calls=0,
                            warnings=[], clusters_total=0, clusters_processed=0,
                            saturation_stopped=False, rejected_names=[])

    monkeypatch.setattr(run_module, "stage_3_flows", _fake_stage_3)
    monkeypatch.setattr(run_module, "stage_4_residual", _fake_stage_4)


_FIXTURE = {
    "package.json": json.dumps({
        "name": "w2brev", "private": True,
        "workspaces": ["packages/*"],
        "dependencies": {"next": "14.0.0"},
    }),
    "next.config.js": "module.exports = {};\n",
    "app/foo/page.tsx": "export default function Page() { return null; }\n",
    # The F1 trigger: a ws-pkg anchor owning >=1000 executable LOC with
    # ZERO flows — the class resolve_flowless_shells used to DEMOTE into
    # a freshly minted "Shared Platform" PF on the keyed path.
    "packages/emails/package.json": json.dumps({"name": "@t/emails"}),
    "packages/emails/src/big.ts": "".join(
        f"export const x{i} = {i};\n" for i in range(1100)
    ),
}


def test_f1_f5_keyed_full_sequence_never_resurrects_shared_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FULL phase_finalize sequence, anchored mint + 6.7d APPLIED: zero
    'shared-platform'/'platform' keys in product_features[]; the large
    flowless anchored PF survives with its lineage intact (F5: its dev
    is not re-homed); the flowless_shells stage did not run."""
    # MECHANICAL flip migration (2026-07-19 S*-pack, KEY_SCHEMA 32): the
    # scripted keyed sequence REQUIRES the 6.7d structural rewrite to APPLY
    # (its own validity guard); under the flipped FAULTLINE_UF_DET_AGGREGATION
    # default the structural LLM stages are skipped by design. Pin the
    # pre-S2 world (kill-switch stays valid forever).
    monkeypatch.setenv("FAULTLINE_UF_DET_AGGREGATION", "0")
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    _flowful_stage_3(monkeypatch)
    monkeypatch.setenv("FAULTLINE_STAGE_6_7D_LLM_ABSTRACTION", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-w2brev")
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", _ScriptedAnthropic)

    repo = tmp_path / "w2brev-app"
    _git_init_with_one_commit(repo, _FIXTURE)
    out = tmp_path / "scan.json"
    run_pipeline_v2(str(repo), days=3650, out_path=out, run_id="w2brev")
    doc = json.loads(out.read_text())

    tele67d = (doc.get("scan_meta") or {}).get(
        "stage_6_7d_journey_abstraction") or {}
    assert tele67d.get("applied") is True, (
        "test-validity guard: 6.7d must APPLY for the keyed sequence "
        f"to be exercised (got {tele67d.get('fallback')})")

    pfs = doc.get("product_features") or []
    keys = {p.get("id") or p.get("name") for p in pfs}
    assert "shared-platform" not in keys and "platform" not in keys, (
        "F1 REGRESSION: the abolished Shared Platform PF is back in the "
        f"keyed product list: {sorted(keys)}")

    emails = [p for p in pfs if p.get("name") == "emails"]
    assert emails, f"flowless anchored PF vanished: {sorted(keys)}"
    assert emails[0].get("anchor_id") == "ws:packages/emails"

    devs = [f for f in doc.get("features") or []
            if f.get("layer") == "developer"]
    email_devs = [f for f in devs
                  if any("packages/emails/" in p for p in (f.get("paths") or []))]
    assert email_devs
    for d in email_devs:
        assert d.get("product_feature_id") == "emails", (
            "F5 REGRESSION: minted-anchor dev re-homed to "
            f"{d.get('product_feature_id')}")

    assert "flowless_shells" not in (doc.get("scan_meta") or {}), (
        "flowless-shell resolution must be OFF on the anchored path")


def _mint_dev(name: str, paths: list[str], flows=None) -> Feature:
    return Feature(
        name=name, paths=list(paths),
        member_files=[MemberFile(path=p, role="anchor", confidence=1.0,
                                 primary=True) for p in paths],
        flows=flows or [], product_feature_id="old",
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def test_f2_anchored_scrubs_nonanchored_citations_and_never_appends_pfs():
    """A draw citing 'Shared Platform' (the retired sink) is scrubbed;
    no reshare rung may append an anchor_id-less PF in anchored mode."""
    from faultline.pipeline_v2.stage_6_86_anchored_mint import run_anchored_mint
    from faultline.pipeline_v2.stage_6_7d_llm_journey_abstraction import (
        run_journey_abstraction,
    )
    from faultline.models.types import UserFlow

    routes = [{"pattern": "/settings", "method": "PAGE",
               "file": "app/settings/page.tsx"}]
    d = _mint_dev("settings", ["app/settings/page.tsx"],
                  flows=[Flow(name="edit-settings-flow",
                              entry_point_file="app/settings/page.tsx",
                              paths=["app/settings/page.tsx"], authors=["a"],
                              total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
                              last_modified=_NOW, health_score=100.0)])
    pfs, _ = run_anchored_mint([d], routes, SimpleNamespace(
        workspaces=None, tracked_files=[], repo_path=Path("."), monorepo=False))
    uf = UserFlow(id="UF-001", name="Manage settings", resource="setting",
                  domain="settings", product_feature_id="settings",
                  intent="manage", member_flow_ids=["edit-settings-flow"],
                  member_count=1)
    payload = (
        '{"product_features":[{"name":"Settings","description":"ok"}],'
        '"user_flows":['
        '{"name":"Manage settings","resource":"setting",'
        '"product_feature":"Settings","from_flows":["UF-001"],'
        '"from_dev_features":["settings"]},'
        '{"name":"Do platform things","resource":"platform",'
        '"product_feature":"Shared Platform","from_flows":["UF-001"],'
        '"from_dev_features":["settings"]}]}'
    )
    class _Cli:
        def __init__(self): self.messages = self
        def create(self, **kw):
            return SimpleNamespace(
                content=[SimpleNamespace(text=payload)],
                usage=SimpleNamespace(input_tokens=5, output_tokens=5))
    ufs2, pfs2, dev_map, tele = run_journey_abstraction(
        [uf], pfs, [d], routes, client=_Cli(), model="m", anchored=True)
    assert tele["applied"]
    assert tele.get("anchored_uf_citations_scrubbed", 0) >= 1
    for u in ufs2:
        assert (u.product_feature_id or "") not in ("shared-platform",
                                                    "platform")
    for p in pfs2:
        assert p.anchor_id, f"anchor_id-less PF appended: {p.name}"


def test_f3_cross_family_clash_slug_equals_canonical_slug_of_display():
    """Mint slug == canonical_slug(display) for EVERY minted PF — the
    6.7d rebuild (name=_slug(display)) and hub parity stamps agree."""
    from faultline.pipeline_v2.emission_integrity import canonical_slug
    from faultline.pipeline_v2.stage_6_86_anchored_mint import run_anchored_mint

    def _f(name: str, entry: str) -> Flow:
        return Flow(name=name, entry_point_file=entry, paths=[entry],
                    authors=["a"], total_commits=1, bug_fixes=0,
                    bug_fix_ratio=0.0, last_modified=_NOW, health_score=100.0)

    # Parent-held per-vendor flows = the children's mint evidence (the
    # Soc0 shape; W3.1 D4's husk floor folds flowless 0-LOC children).
    edr_plumb = _mint_dev("edr", [
        "backend/services/edr/base.py", "backend/services/edr/factory.py",
        "backend/services/edr/normalizer.py"],
        flows=[_f("q-flow", "backend/services/edr/base.py"),
               _f("claroty-flow", "backend/services/edr/claroty.py"),
               _f("cortex-flow", "backend/services/edr/cortex.py"),
               _f("defender-flow", "backend/services/edr/defender.py")])
    kids = [
        _mint_dev("edr-claroty", ["backend/services/edr/claroty.py"]),
        _mint_dev("edr-cortex", ["backend/services/edr/cortex.py"]),
        _mint_dev("edr-defender", ["backend/services/edr/defender.py"]),
    ]
    iot = [
        _mint_dev("iot-claroty", ["backend/services/iot_ot/claroty.py"],
                  flows=[_f("iot-claroty-flow",
                            "backend/services/iot_ot/claroty.py")]),
        _mint_dev("iot-zscaler", ["backend/services/iot_ot/zscaler.py"],
                  flows=[_f("iot-zscaler-flow",
                            "backend/services/iot_ot/zscaler.py")]),
        _mint_dev("iot-crowdstrike", ["backend/services/iot_ot/crowdstrike.py"],
                  flows=[_f("iot-crowdstrike-flow",
                            "backend/services/iot_ot/crowdstrike.py")]),
    ]
    pfs, tele = run_anchored_mint(
        [edr_plumb, *kids, *iot], [], SimpleNamespace(
            workspaces=None, tracked_files=[], repo_path=Path("."),
            monorepo=False))
    assert len([p for p in pfs if (p.name or "").startswith("claroty")]) == 2
    for p in pfs:
        assert p.name == canonical_slug(p.display_name or p.name), (
            f"slug/display divergence (F3): {p.name!r} vs "
            f"{canonical_slug(p.display_name or p.name)!r}")
    # parity stamps must reference EXISTING pf names (never silently skip)
    stamps = tele["hub_family_stamps"]
    names = {p.name for p in pfs}
    for dev_name, slug in stamps.items():
        assert slug in names, (dev_name, slug)


def test_f4_lane_admits_only_the_three_amendment_reasons():
    from faultline.pipeline_v2.stage_6_86_anchored_mint import (
        build_platform_infrastructure_lane,
    )
    rows_in = []
    for reason in ("no_anchor_lineage", "sub_mint_bar_surface",
                   "shell_lineage_only", "non_product_surface",
                   "genuinely_shared_infra", "facet_view"):
        f = _mint_dev(f"d-{reason}", [f"lib/{reason}.ts"])
        f.product_feature_id = None
        f.shared_reason = reason
        rows_in.append(f)
    rows = build_platform_infrastructure_lane(rows_in)
    got = {r["shared_reason"] for r in rows}
    assert got == {"no_anchor_lineage", "sub_mint_bar_surface",
                   "shell_lineage_only"}, got
