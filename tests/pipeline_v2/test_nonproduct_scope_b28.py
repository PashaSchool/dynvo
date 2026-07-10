"""B28 — PFs anchored inside non-product apps are not product features.

Fixtures are DISTILLED from the real wave14 exhibits (supabase
apps/ui-library registry app / packages/api-types types package /
apps/docs guides anchors; cal.com mock-payment-app hub fixture) with
neutral names where the SHAPE, not the name, carries the signal — plus
the ratified anti-cases: cal.com ``event-types`` (route-anchored in a
product app → zero prongs), kan-mcp/midday-cli (published-CLI override),
the R1 registry-template edge, and the R2 shadcn-class dominant app.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.surface_taxonomy import (
    DOCS_REANCHOR_ENV,
    NONPRODUCT_SCOPE_ENV,
    SurfaceScopeClassifier,
    _is_registry_publisher,
    _majority_dir,
    apply_emission_taxonomy,
)
from faultline.pipeline_v2.technology_instruments import (
    detect_technology_instruments,
)


# ── fixture helpers (test_surface_taxonomy / test_technology_instruments
# conventions) ──────────────────────────────────────────────────────────


def _write(repo: Path, rel: str, text: str = "") -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel


def _manifest(repo: Path, rel_dir: str, name: str, *,
              deps: dict | None = None, dev_deps: dict | None = None,
              private: bool | None = None, bin_entry: str | None = None,
              main: str | None = None, scripts: dict | None = None,
              ) -> str:
    doc: dict = {"name": name}
    if deps is not None:
        doc["dependencies"] = deps
    if dev_deps:
        doc["devDependencies"] = dev_deps
    if private is not None:
        doc["private"] = private
    if bin_entry:
        doc["bin"] = {name.split("/")[-1]: bin_entry}
    if main:
        doc["main"] = main
    if scripts:
        doc["scripts"] = scripts
    rel = f"{rel_dir}/package.json" if rel_dir else "package.json"
    return _write(repo, rel, json.dumps(doc))


def _feature(name: str, paths: list[str], pfid: str | None = None,
             *, layer: str = "developer",
             anchor_id: str | None = None) -> Feature:
    return Feature(
        name=name, paths=paths, authors=[],
        total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0, layer=layer, product_feature_id=pfid,
        anchor_id=anchor_id, flows=[],
    )


def _flow(name: str, entry: str, paths: list[str] | None = None) -> Flow:
    return Flow(
        name=name, uuid=name, entry_point_file=entry,
        paths=paths or [entry], authors=[], total_commits=0, bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.fromtimestamp(0, timezone.utc),
        health_score=80.0,
    )


def _uf(uf_id: str, name: str, pfid: str | None,
        members: list[str]) -> UserFlow:
    return UserFlow(
        id=uf_id, name=name, intent="browse", resource="page",
        product_feature_id=pfid, member_flow_ids=members,
        member_count=len(members), routes=[], category="interactive",
    )


# ── S1g — types-only package (technology_instruments) ─────────────────


_TYPES_ONLY_INDEX = (
    "import type { paths as apiPaths } from './types/api'\n"
    "export type { webhooks } from './types/api'\n"
    "export interface paths extends apiPaths {}\n"
    "export type ApiComponents = { schemas: object }\n"
)


def _types_repo(repo: Path, *, index: str = _TYPES_ONLY_INDEX,
                deps: dict | None = None) -> list[str]:
    return [
        _manifest(repo, "", "root", private=True),
        _manifest(repo, "packages/gen-types", "gen-types",
                  deps=deps if deps is not None else {},
                  dev_deps={"openapi-typescript": "7.0.0"},
                  main="./index.ts"),
        _write(repo, "packages/gen-types/index.ts", index),
        _write(repo, "packages/gen-types/redocly.yaml", "apis: {}"),
        # the codegen output rides in the repo (real api-types shape —
        # these dilute cfg_share below the S1c settings-artifact bar).
        _write(repo, "packages/gen-types/types/api.d.ts",
               "export interface paths { '/x': object }\n"),
        _write(repo, "packages/gen-types/types/platform.d.ts",
               "export interface operations { op: object }\n"),
        _manifest(repo, "apps/web", "@acme/web",
                  deps={"react": "18.0.0"}, private=True),
        _write(repo, "apps/web/src/page.tsx",
               'import type { paths } from "gen-types";\n'),
    ]


def test_s1g_types_only_package_is_instrument(tmp_path: Path) -> None:
    """supabase packages/api-types shape: zero runtime deps, TS entry,
    every import/export type-position → S1g instrument."""
    tracked = _types_repo(tmp_path)
    tele = detect_technology_instruments(tmp_path, tracked, [])
    assert tele["instruments"].get("packages/gen-types") == "S1g-types-only"
    assert "packages/gen-types" in tele["dirs"]


def test_s1g_runtime_dep_disqualifies(tmp_path: Path) -> None:
    tracked = _types_repo(tmp_path, deps={"zod": "3.0.0"})
    tele = detect_technology_instruments(tmp_path, tracked, [])
    assert "packages/gen-types" not in tele["instruments"]


def test_s1g_runtime_export_disqualifies(tmp_path: Path) -> None:
    runtime = _TYPES_ONLY_INDEX + "export const VERSION = '1';\n"
    tracked = _types_repo(tmp_path, index=runtime)
    tele = detect_technology_instruments(tmp_path, tracked, [])
    assert "packages/gen-types" not in tele["instruments"]


def test_s1g_flag_off_restores(tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NONPRODUCT_SCOPE_ENV, "0")
    tracked = _types_repo(tmp_path)
    tele = detect_technology_instruments(tmp_path, tracked, [])
    assert "packages/gen-types" not in tele["instruments"]
    assert "dev_artifact_units" not in tele


# ── P-D — hub-fixture mark (technology_instruments, mark-only) ────────


def _hub_repo(repo: Path, *, fixture_name: str = "mock-pay",
              real_consumer_of: str | None = "alpha") -> list[str]:
    """cal.com app-store shape: a hub ws-package with >=3 children, a
    uniform generated barrel importing the REAL children (the fixture is
    dispatched dynamically — cal.com's ``import("./x/api")`` object
    literals yield zero import specs, so the fixture has NO visible
    importers), one child with a real product consumer, and token-less
    barrel-only children (the basecamp3/giphy class)."""
    tracked = [
        _manifest(repo, "", "root", private=True),
        _manifest(repo, "packages/hubstore", "@acme/hubstore", deps={}),
    ]
    kids = [fixture_name, "alpha", "beta", "gamma"]
    barrel_lines = []
    for kid in kids:
        tracked += [
            _manifest(repo, f"packages/hubstore/{kid}", f"@acme/{kid}",
                      deps={}),
            _write(repo, f"packages/hubstore/{kid}/index.ts",
                   "export const handler = () => 1;\n"),
        ]
        if kid != fixture_name:  # the fixture rides dynamic dispatch only
            barrel_lines.append(f'import "@acme/{kid}";')
    tracked.append(_write(
        repo, "packages/hubstore/apps.generated.ts",
        "\n".join(barrel_lines) + "\n"))
    if real_consumer_of:
        tracked += [
            _manifest(repo, "apps/web", "@acme/web",
                      deps={"react": "18.0.0"}, private=True),
            _write(repo, "apps/web/src/setup.ts",
                   f'import "@acme/{real_consumer_of}";\n'),
        ]
    return tracked


def test_pd_hub_fixture_marked_not_laned(tmp_path: Path) -> None:
    tracked = _hub_repo(tmp_path)
    tele = detect_technology_instruments(tmp_path, tracked, [])
    marks = tele.get("dev_artifact_units") or {}
    # the token-carrying no-visible-consumer child is MARKED (mark-only:
    # never an instrument dir — its PF and journeys mint normally at
    # 6.86; the emission lane executes behind the R1/R2 rails)…
    assert "packages/hubstore/mock-pay" in marks
    assert "packages/hubstore/mock-pay" not in tele["dirs"]
    # …the child with a real (non-barrel) product consumer never marks,
    # and token-less barrel-only children never mark (the zoomvideo /
    # basecamp3 counterfactuals — integrations = own PF is binding law).
    assert "packages/hubstore/alpha" not in marks
    assert "packages/hubstore/beta" not in marks
    assert "packages/hubstore/gamma" not in marks


def test_pd_flag_off_no_marks(tmp_path: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NONPRODUCT_SCOPE_ENV, "0")
    tele = detect_technology_instruments(tmp_path, _hub_repo(tmp_path), [])
    assert "dev_artifact_units" not in tele


# ── P-B — registry-publisher manifest separation ───────────────────────


def test_registry_publisher_vs_consumer(tmp_path: Path) -> None:
    _write(tmp_path, "apps/uikit/components.json", "{}")
    _write(tmp_path, "apps/uikit/registry/index.ts", "")
    _write(tmp_path, "apps/consumer/components.json", "{}")
    _write(tmp_path, "apps/dsonly/registry/index.ts", "")
    _manifest(tmp_path, "apps/scripted", "scripted",
              scripts={"build:registry": "shadcn build public/r.json"})
    _write(tmp_path, "apps/scripted/components.json", "{}")
    assert _is_registry_publisher(tmp_path, "apps/uikit") is True
    assert _is_registry_publisher(tmp_path, "apps/scripted") is True
    # components.json alone = shadcn CONSUMER (papermark / openstatus /
    # supabase packages/ui class); registry/ alone lacks the declaration.
    assert _is_registry_publisher(tmp_path, "apps/consumer") is False
    assert _is_registry_publisher(tmp_path, "apps/dsonly") is False
    assert _is_registry_publisher(tmp_path, "apps/absent") is False


# ── Shape E — emission lane end-to-end (journeys ride along) ───────────


def _demo_board(repo: Path):
    """Mini supabase: a registry-publisher app (P-B) with a demo PF +
    journey, and a bigger studio side so R2 never trips."""
    _write(repo, "apps/uikit/components.json", "{}")
    _write(repo, "apps/uikit/registry/index.ts", "")
    _manifest(repo, "apps/uikit", "uikit", private=True)
    login_paths = [
        "apps/uikit/app/example/login/page.tsx",
        "apps/uikit/registry/default/login-form.tsx",
    ]
    studio_paths = [
        "apps/studio/pages/projects.tsx",
        "apps/studio/pages/settings.tsx",
    ]
    pf_login = _feature("login", login_paths, layer="product",
                        anchor_id="route:apps/uikit/app/example/login")
    pf_studio = _feature("projects", studio_paths, layer="product",
                         anchor_id="route:apps/studio/pages/projects")
    devs = [
        _feature("login-dev", login_paths, "login"),
        _feature("projects-dev", studio_paths, "projects"),
    ]
    flows = [
        _flow("f-login", login_paths[0], login_paths),
        _flow("f-proj-1", studio_paths[0]),
        _flow("f-proj-2", studio_paths[1]),
    ]
    ufs = [
        _uf("UF-1", "Connect login", "login", ["f-login"]),
        _uf("UF-2", "Browse projects", "projects", ["f-proj-1"]),
        _uf("UF-3", "Manage settings", "projects", ["f-proj-2"]),
    ]
    return devs, [pf_login, pf_studio], ufs, flows


def test_shape_e_demo_pf_lanes_with_journeys(tmp_path: Path) -> None:
    devs, pfs, ufs, flows = _demo_board(tmp_path)
    n_ufs = len(ufs)
    tele, lane, product = apply_emission_taxonomy(
        devs, pfs, ufs, flows, [], repo_path=tmp_path)
    assert "apps/uikit" in (tele.get("dev_artifact") or {}).get(
        "applied", [])
    names = {p.name for p in product}
    assert "login" not in names and "projects" in names
    lane_row = next(e for e in lane if e["name"] == "login")
    moved = [u["name"] for u in lane_row["user_flows"]]
    assert moved == ["Connect login"]  # journeys ride along — never lost
    assert len(ufs) + len(lane_row["user_flows"]) == n_ufs  # conservation


def test_r1_rail_registry_template_edge(tmp_path: Path) -> None:
    """One studio-homed journey enters through a registry TEMPLATE copy
    (supabase 'AI Assistant' edge): a strict MINORITY of external
    contributions must not block the lane; parity or more must."""
    devs, pfs, ufs, flows = _demo_board(tmp_path)
    flows.append(_flow(
        "f-template",
        "apps/uikit/registry/default/platform-kit/api/route.ts"))
    ufs.append(_uf("UF-4", "AI assistant", "projects", ["f-template"]))
    tele, lane, product = apply_emission_taxonomy(
        devs, pfs, ufs, flows, [], repo_path=tmp_path)
    # uikit: internal 1 (Connect login) vs external 1 (template) → ext is
    # NOT a strict minority → R1 blocks; the PF stays product.
    da = tele.get("dev_artifact") or {}
    assert "apps/uikit" not in da.get("applied", [])
    assert "R1" in (da.get("blocked") or {}).get("apps/uikit", "")
    assert {p.name for p in product} >= {"login", "projects"}


def test_r1_rail_strict_minority_passes(tmp_path: Path) -> None:
    devs, pfs, ufs, flows = _demo_board(tmp_path)
    # internal = 2 (a second demo journey), external = 1 (template);
    # two extra studio journeys keep uikit clear of the R2 dominance bar.
    flows += [
        _flow("f-demo2", "apps/uikit/app/example/signup/page.tsx"),
        _flow("f-template",
              "apps/uikit/registry/default/platform-kit/api/route.ts"),
        _flow("f-proj-3", "apps/studio/pages/branches.tsx"),
        _flow("f-proj-4", "apps/studio/pages/logs.tsx"),
    ]
    ufs += [
        _uf("UF-4", "Try signup demo", "login", ["f-demo2"]),
        _uf("UF-5", "AI assistant", "projects", ["f-template"]),
        _uf("UF-6", "Browse branches", "projects", ["f-proj-3"]),
        _uf("UF-7", "Browse logs", "projects", ["f-proj-4"]),
    ]
    tele, lane, product = apply_emission_taxonomy(
        devs, pfs, ufs, flows, [], repo_path=tmp_path)
    assert "apps/uikit" in (tele.get("dev_artifact") or {}).get(
        "applied", [])
    assert "login" not in {p.name for p in product}


def test_r2_rail_blocks_dominant_registry_app(tmp_path: Path) -> None:
    """shadcn-class repo: the registry app IS the product — it holds the
    strict majority of the board's entries and must never lane."""
    _write(tmp_path, "apps/uikit/components.json", "{}")
    _write(tmp_path, "apps/uikit/registry/index.ts", "")
    paths = [f"apps/uikit/app/r/{i}/page.tsx" for i in range(3)]
    pf = _feature("registry", paths, layer="product",
                  anchor_id="route:apps/uikit/app/r")
    devs = [_feature("registry-dev", paths, "registry")]
    flows = [_flow(f"f-{i}", p) for i, p in enumerate(paths)]
    ufs = [_uf(f"UF-{i}", f"Browse {i}", "registry", [f"f-{i}"])
           for i in range(3)]
    tele, lane, product = apply_emission_taxonomy(
        devs, [pf], ufs, flows, [], repo_path=tmp_path)
    da = tele.get("dev_artifact") or {}
    assert (da.get("blocked") or {}).get("apps/uikit") == "R2:dominant-app"
    assert {p.name for p in product} == {"registry"}


# ── anti-cases: published CLI + product-app route anchor ───────────────


def test_published_cli_workspace_stays_product(tmp_path: Path) -> None:
    """kan packages/mcp / midday packages/cli: a dev_tooling-NAMED
    workspace whose manifest ships a bin (not private) is a shipped
    product CLI — the published-CLI override outranks the lexicon, so no
    B28 action may touch it (no lane, no re-anchor)."""
    _manifest(tmp_path, "packages/cli", "@acme/cli",
              deps={}, bin_entry="./dist/run.js", private=False)
    paths = ["packages/cli/src/main.ts", "packages/cli/src/auth.ts"]
    pf = _feature("cli", paths, layer="product",
                  anchor_id="ws:packages/cli")
    devs = [_feature("cli-dev", paths, "cli")]
    flows = [_flow("f-cli", paths[0])]
    ufs = [_uf("UF-1", "Authenticate via CLI", "cli", ["f-cli"])]
    tele, lane, product = apply_emission_taxonomy(
        devs, [pf], ufs, flows, [], repo_path=tmp_path)
    assert {p.name for p in product} == {"cli"}
    assert not lane
    assert pf.anchor_id == "ws:packages/cli"  # no re-anchor either


def test_event_types_shape_fires_zero_prongs(tmp_path: Path) -> None:
    """cal.com ``event-types`` anti-case: a route-anchored PF in a plain
    product app — no manifest prong, no mark, product scope → untouched."""
    _manifest(tmp_path, "apps/api", "@acme/api", deps={}, private=True)
    paths = ["apps/api/v1/pages/api/event-types/index.ts",
             "apps/api/v1/pages/api/event-types/[id].ts"]
    pf = _feature("event-types", paths, layer="product",
                  anchor_id="route:apps/api/v1/pages/api/event-types")
    devs = [_feature("et-dev", paths, "event-types")]
    flows = [_flow("f-et", paths[0])]
    ufs = [_uf("UF-1", "Manage event types", "event-types", ["f-et"])]
    tele, lane, product = apply_emission_taxonomy(
        devs, [pf], ufs, flows, [], repo_path=tmp_path)
    assert "dev_artifact" not in tele
    assert "docs_reanchor" not in tele
    assert {p.name for p in product} == {"event-types"}
    assert pf.anchor_id == "route:apps/api/v1/pages/api/event-types"


# ── Shape D — docs-anchored product PF re-anchors in place ─────────────


def _docs_anchored_board():
    """supabase ``auth`` shape: docs-guides anchor, studio body."""
    auth_paths = [
        "apps/studio/components/interfaces/Auth/Users.tsx",
        "apps/studio/components/interfaces/Auth/Policies.tsx",
        "apps/studio/components/interfaces/Auth/Providers.tsx",
        "apps/studio/data/auth/users-query.ts",
    ]
    pf = _feature("auth", list(auth_paths), layer="product",
                  anchor_id="route:apps/docs/app/guides/auth")
    devs = [_feature("auth-dev", list(auth_paths), "auth")]
    flows = [_flow("f-auth", auth_paths[3], auth_paths)]
    ufs = [_uf("UF-1", "Connect auth", "auth", ["f-auth"])]
    return devs, [pf], ufs, flows


def test_shape_d_docs_anchored_pf_reanchors(tmp_path: Path) -> None:
    devs, pfs, ufs, flows = _docs_anchored_board()
    pf = pfs[0]
    tele, lane, product = apply_emission_taxonomy(
        devs, pfs, ufs, flows, [], repo_path=tmp_path)
    # PF STAYS product (body abstains → conservative product) and its
    # anchor heals to the majority dir (3/4 under interfaces/Auth).
    assert {p.name for p in product} == {"auth"}
    assert pf.anchor_id == "fdir:apps/studio/components/interfaces/Auth"
    moves = (tele.get("docs_reanchor") or {}).get("reanchored") or []
    assert [m["pf"] for m in moves] == ["auth"]
    # journeys untouched — conservation trivial.
    assert [u.product_feature_id for u in ufs] == ["auth"]


def test_shape_d_route_lineage_fallback(tmp_path: Path) -> None:
    """No majority child below the workspace root → fall back to the
    common prefix of the outside ROUTE files (extension-stripped)."""
    paths = [
        "apps/studio/pages/sign-up.tsx",
        "apps/studio/components/SignUp/Form.tsx",
        "apps/studio/components/SignUp/Fields.tsx",
        "apps/studio/data/profile/create.ts",
        "apps/studio/data/profile/verify.ts",
        "apps/uikit/app/example/sign-up/page.tsx",
    ]
    pf = _feature("sign-up", list(paths), layer="product",
                  anchor_id="route:apps/uikit/app/example/sign-up")
    _write(tmp_path, "apps/uikit/components.json", "{}")
    _write(tmp_path, "apps/uikit/registry/index.ts", "")
    devs = [_feature("su-dev", list(paths), "sign-up"),
            _feature("studio-dev",
                     ["apps/studio/pages/projects.tsx"], "projects")]
    pf2 = _feature("projects", ["apps/studio/pages/projects.tsx"],
                   layer="product",
                   anchor_id="route:apps/studio/pages/projects")
    flows = [_flow("f-su", paths[0], paths),
             _flow("f-p", "apps/studio/pages/projects.tsx")]
    ufs = [_uf("UF-1", "Sign up for account", "sign-up", ["f-su"]),
           _uf("UF-2", "Browse projects", "projects", ["f-p"])]
    routes = [{"pattern": "/sign-up", "method": "PAGE",
               "file": "apps/studio/pages/sign-up.tsx"}]
    tele, lane, product = apply_emission_taxonomy(
        devs, [pf, pf2], ufs, flows, routes, repo_path=tmp_path)
    # walk stops at apps/studio (components 2 : pages 1 : data 2 of 5 —
    # no strict-majority child) → route-common fallback, ext-stripped.
    assert pf.anchor_id == "route:apps/studio/pages/sign-up"
    assert {p.name for p in product} >= {"sign-up", "projects"}


def test_shape_d_refusal_keeps_anchor(tmp_path: Path) -> None:
    paths = [
        "apps/studio/components/A/x.tsx",
        "apps/studio/components/B/y.tsx",
        "apps/studio/data/z.ts",
        "apps/studio/pages/w.tsx",
    ]
    pf = _feature("scattered", list(paths), layer="product",
                  anchor_id="route:apps/docs/app/guides/scattered")
    devs = [_feature("s-dev", list(paths), "scattered")]
    tele, lane, product = apply_emission_taxonomy(
        devs, [pf], [], [], [], repo_path=tmp_path)
    # components holds 2/4 — not a strict majority; no route files →
    # refusal, anchor untouched, telemetry says why.
    assert pf.anchor_id == "route:apps/docs/app/guides/scattered"
    refused = (tele.get("docs_reanchor") or {}).get("refused") or {}
    assert refused.get("scattered") == "no-deep-majority-dir"


def test_shape_d_flag_off_keeps_anchor(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DOCS_REANCHOR_ENV, "0")
    devs, pfs, ufs, flows = _docs_anchored_board()
    pf = pfs[0]
    tele, lane, product = apply_emission_taxonomy(
        devs, pfs, ufs, flows, [], repo_path=tmp_path)
    assert pf.anchor_id == "route:apps/docs/app/guides/auth"
    assert "docs_reanchor" not in tele


# ── flags restore + determinism ─────────────────────────────────────────


def test_both_flags_off_restore_baseline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NONPRODUCT_SCOPE_ENV, "0")
    monkeypatch.setenv(DOCS_REANCHOR_ENV, "0")
    devs, pfs, ufs, flows = _demo_board(tmp_path)
    tele, lane, product = apply_emission_taxonomy(
        devs, pfs, ufs, flows, [], repo_path=tmp_path)
    assert "dev_artifact" not in tele and "docs_reanchor" not in tele
    assert {p.name for p in product} == {"login", "projects"}
    assert not lane
    assert pfs[0].anchor_id == "route:apps/uikit/app/example/login"


def test_shape_e_deterministic_across_runs(tmp_path: Path) -> None:
    def run():
        devs, pfs, ufs, flows = _demo_board(tmp_path)
        tele, lane, product = apply_emission_taxonomy(
            devs, pfs, ufs, flows, [], repo_path=tmp_path)
        return (
            sorted(p.name for p in product),
            sorted(e["name"] for e in lane),
            json.dumps(tele.get("dev_artifact"), sort_keys=True),
        )

    assert run() == run()


# ── majority-dir election unit pins ────────────────────────────────────


def test_majority_dir_walk_pins() -> None:
    # descends while ONE child holds a strict majority; stops honestly.
    assert _majority_dir([
        "apps/studio/components/interfaces/Auth/a.tsx",
        "apps/studio/components/interfaces/Auth/b.tsx",
        "apps/studio/components/interfaces/Auth/c/d.tsx",
        "apps/studio/data/auth.ts",
    ]) == "apps/studio/components/interfaces/Auth"
    assert _majority_dir([
        "apps/studio/pages/a.tsx",
        "apps/studio/components/b.tsx",
    ]) == "apps/studio"
    assert _majority_dir([]) is None


def test_pd_marks_flow_into_emission(tmp_path: Path) -> None:
    """The 6.86 P-D mark, handed to the emission taxonomy via
    ``dev_artifact_units``, lanes the hub fixture with its journey."""
    fixture = "packages/hubstore/mock-pay"
    paths = [f"{fixture}/index.ts", f"{fixture}/api/pay.ts"]
    pf = _feature("mock-pay", paths, layer="product",
                  anchor_id=f"hub:{fixture}")
    pf2 = _feature("bookings", ["apps/web/pages/book.tsx",
                                "apps/web/pages/cancel.tsx"],
                   layer="product", anchor_id="route:apps/web/pages/book")
    devs = [_feature("mp-dev", paths, "mock-pay"),
            _feature("bk-dev", ["apps/web/pages/book.tsx",
                                "apps/web/pages/cancel.tsx"], "bookings")]
    flows = [_flow("f-mp", paths[0], paths),
             _flow("f-b1", "apps/web/pages/book.tsx"),
             _flow("f-b2", "apps/web/pages/cancel.tsx")]
    ufs = [_uf("UF-1", "Manage mock payment integration",
               "mock-pay", ["f-mp"]),
           _uf("UF-2", "Book a slot", "bookings", ["f-b1"]),
           _uf("UF-3", "Cancel booking", "bookings", ["f-b2"])]
    n_before = len(ufs)
    tele, lane, product = apply_emission_taxonomy(
        devs, [pf, pf2], ufs, flows, [], repo_path=tmp_path,
        dev_artifact_units=[fixture])
    assert fixture in (tele.get("dev_artifact") or {}).get("applied", [])
    assert "mock-pay" not in {p.name for p in product}
    row = next(e for e in lane if e["name"] == "mock-pay")
    assert [u["name"] for u in row["user_flows"]] == [
        "Manage mock payment integration"]
    assert len(ufs) + len(row["user_flows"]) == n_before
