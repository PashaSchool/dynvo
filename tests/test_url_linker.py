"""Tests for the URL-literal frontend→backend linker.

Covers:
  - extraction: fetch / axios / template literals / concat tails /
    generic api-client receivers / URL consts / vue + svelte text;
    trpc skip; route-definition (handler-arg) skip; external-URL skip
  - normalization table (interpolations, query strings, base consts,
    absolute URLs, slash hygiene)
  - matching: exact, params, mount-prefix skew, unknown-prefix
    suffix alignment, catch-alls, zero-static refusal
  - route-table build: explicit extractor routes + filesystem-derived,
    PAGE exclusion, ownership resolution
  - attachment through Stage 2.6: synthetic FastAPI + React fixture
    (api module + components attach to the backend route feature),
    shared-client fan-in, determinism (scan twice)
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.stage_2_6_membership_closure import (
    run_membership_closure,
)
from faultline.pipeline_v2.stage_2_reconcile import DeveloperFeature
from faultline.pipeline_v2.url_linker import (
    RouteEntry,
    UrlRef,
    build_route_table,
    extract_url_refs,
    match_url,
    normalize_url,
    route_pattern_segments,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _templates(text: str) -> list[str]:
    return [r.template for r in extract_url_refs(text)]


def _ref(template: str, *, unknown_prefix: bool = False) -> UrlRef:
    return UrlRef(
        template=template,
        segments=tuple(s for s in template.split("/") if s),
        method=None,
        raw=template,
        unknown_prefix=unknown_prefix,
    )


def _entry(pattern: str, method: str = "GET", feature: str = "f") -> RouteEntry:
    segs = route_pattern_segments(pattern)
    assert segs is not None
    return RouteEntry(
        segments=segs, method=method, file="api/routes.py",
        feature=feature, pattern=pattern,
    )


# ── Extraction ───────────────────────────────────────────────────────────


def test_extract_fetch_string_literal() -> None:
    assert _templates('fetch("/api/teams")') == ["/api/teams"]


def test_extract_fetch_template_interpolation() -> None:
    assert _templates(
        "fetch(`/api/org-knowledge/${id}`)",
    ) == ["/api/org-knowledge/{param}"]


def test_extract_fetch_method_option() -> None:
    refs = extract_url_refs(
        'fetch("/api/teams", { method: "POST", body })',
    )
    assert [(r.template, r.method) for r in refs] == [("/api/teams", "POST")]


def test_extract_concat_tail_becomes_param() -> None:
    assert _templates(
        'fetch("/api/items/" + itemId)',
    ) == ["/api/items/{param}"]


def test_extract_axios_verbs() -> None:
    refs = extract_url_refs(
        'axios.post("/api/teams", body); axios.delete(`/api/teams/${id}`)',
    )
    assert [(r.template, r.method) for r in refs] == [
        ("/api/teams", "POST"),
        ("/api/teams/{param}", "DELETE"),
    ]


def test_extract_generic_api_client() -> None:
    refs = extract_url_refs(
        "apiClient.get('/api/widgets'); httpClient.put('/api/widgets/2')",
    )
    assert [(r.template, r.method) for r in refs] == [
        ("/api/widgets", "GET"),
        ("/api/widgets/2", "PUT"),
    ]


def test_extract_skips_trpc_chains() -> None:
    assert _templates(
        "trpc.documents.get('/api/nope'); trpcApi.get('/api/also-no')",
    ) == []


def test_extract_skips_route_definitions() -> None:
    # An Express router that happens to be called ``api`` — the literal
    # is a route DEFINITION (handler second arg), not an outgoing call.
    text = (
        'api.get("/api/users", (req, res) => res.json([]));\n'
        'api.post("/api/users", handler);\n'
        'api.delete("/api/users", async (req, res) => {});\n'
    )
    assert _templates(text) == []


def test_extract_skips_external_absolute_urls() -> None:
    assert _templates('fetch("https://api.stripe.com/v1/charges")') == []
    assert _templates('fetch("//cdn.example.com/x")') == []


def test_extract_base_const_resolves_template_head() -> None:
    text = (
        'const API_BASE = "/api/v1";\n'
        "fetch(`${API_BASE}/teams/${id}`);\n"
    )
    assert _templates(text) == ["/api/v1/teams/{param}"]


def test_extract_base_const_makes_absolute_host_relative() -> None:
    text = (
        'const SERVER_URL = "https://app.example.com/api";\n'
        "fetch(`${SERVER_URL}/billing`);\n"
    )
    assert _templates(text) == ["/api/billing"]


def test_extract_concat_head_const() -> None:
    text = (
        'const API_BASE = "/api";\n'
        'fetch(API_BASE + "/billing/" + id);\n'
    )
    assert _templates(text) == ["/api/billing/{param}"]


def test_extract_unknown_template_head_marks_unknown_prefix() -> None:
    refs = extract_url_refs("fetch(`${baseUrl}/usage/stats`)")
    assert [(r.template, r.unknown_prefix) for r in refs] == [
        ("/usage/stats", True),
    ]


def test_extract_url_const() -> None:
    assert _templates('export const TEAMS_URL = "/api/teams";') == ["/api/teams"]


def test_extract_skips_base_named_consts() -> None:
    # A *base* const is a URL head, not an endpoint reference.
    assert _templates('const API_BASE_URL = "/api";') == []


def test_extract_skips_non_url_consts_and_plain_strings() -> None:
    text = (
        'const NAME = "team";\n'
        'const STYLE_PATH = "color: red";\n'
        'fetch(someVariable);\n'
    )
    assert _templates(text) == []


def test_extract_swr_and_nuxt_idioms() -> None:
    refs = extract_url_refs(
        "useSWR('/api/usage'); $fetch('/api/things'); useFetch(`/api/x/${id}`)",
    )
    assert [r.template for r in refs] == [
        "/api/usage", "/api/things", "/api/x/{param}",
    ]


def test_extract_vue_sfc_text() -> None:
    text = (
        "<template><button @click=\"load\">Go</button></template>\n"
        "<script setup>\n"
        "const load = () => $fetch('/api/projects/' + props.id)\n"
        "</script>\n"
    )
    assert _templates(text) == ["/api/projects/{param}"]


def test_extract_svelte_text() -> None:
    text = (
        "<script>\n"
        "  export let id;\n"
        "  async function refresh() {\n"
        "    const res = await fetch(`/api/status/${id}`);\n"
        "  }\n"
        "</script>\n"
        "<p>{status}</p>\n"
    )
    assert _templates(text) == ["/api/status/{param}"]


def test_extract_dedupes_repeated_urls() -> None:
    assert _templates(
        'fetch("/api/teams"); fetch("/api/teams")',
    ) == ["/api/teams"]


def test_extract_order_is_deterministic() -> None:
    text = 'fetch("/api/b"); fetch("/api/a")'
    assert _templates(text) == ["/api/b", "/api/a"]
    assert extract_url_refs(text) == extract_url_refs(text)


# ── Normalization table ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "kwargs", "expected"),
    [
        ("/api/teams", {}, "/api/teams"),
        ("/api/teams/", {}, "/api/teams"),
        ("/api//teams", {}, "/api/teams"),
        ("/api/teams?page=2", {}, "/api/teams"),
        ("/api/teams#anchor", {}, "/api/teams"),
        ("/api/items/${id}", {"is_template": True}, "/api/items/{param}"),
        ("/api/items/", {"concat_tail": True}, "/api/items/{param}"),
        ("/api/items?id=", {"concat_tail": True}, "/api/items"),
        ("https://x.dev/v1", {}, None),          # absolute, no base const
        ("./relative", {}, None),                 # not root-relative
        ("plain words here", {}, None),
        ("/${id}", {"is_template": True}, None),  # zero static segments
        ("/", {}, None),
    ],
)
def test_normalize_url_table(
    raw: str, kwargs: dict, expected: str | None,
) -> None:
    ref = normalize_url(raw, **kwargs)
    assert (ref.template if ref else None) == expected


def test_normalize_unknown_head_const() -> None:
    ref = normalize_url("/teams/x", head_const="mystery")
    assert ref is not None and ref.unknown_prefix


# ── Backend pattern normalization ────────────────────────────────────────


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("/api/teams/:id", ("api", "teams", "{param}")),
        ("/api/teams/{team_id}", ("api", "teams", "{param}")),
        ("/users/<int:uid>/docs", ("users", "{param}", "docs")),
        ("/files/[fileId]", ("files", "{param}")),
        ("/files/[...slug]", ("files", "{**}")),
        ("/files/*", ("files", "{**}")),
        ("/{id}", None),            # zero static — refused
        ("", None),
    ],
)
def test_route_pattern_segments(
    pattern: str, expected: tuple[str, ...] | None,
) -> None:
    assert route_pattern_segments(pattern) == expected


# ── Matching ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("front", "back", "want"),
    [
        ("/api/teams", "/api/teams", True),
        ("/api/teams", "/api/gadgets", False),
        ("/api/teams/{param}", "/api/teams/:id", True),
        ("/api/teams/5", "/api/teams/:id", True),       # concrete value
        ("/api/teams/{param}", "/api/teams/settings", False),  # param≠static
        ("/api/teams", "/api/teams/:id", False),        # arity mismatch
        ("/api/v1/teams/{param}", "/teams/:id", True),  # mount prefix
        ("/api/files/a/b/c", "/api/files/[...slug]", True),
        ("/api/files", "/api/files/[...slug]", False),  # catch-all needs ≥1
    ],
)
def test_match_url_table(front: str, back: str, want: bool) -> None:
    assert match_url(_ref(front), _entry(back)) is want


def test_match_unknown_prefix_suffix_alignment() -> None:
    # `${base}/teams/${id}` → frontend suffix vs backend /api/teams/:id
    assert match_url(
        _ref("/teams/{param}", unknown_prefix=True), _entry("/api/teams/:id"),
    )
    # Without the unknown-prefix flag the same shape must NOT match.
    assert not match_url(_ref("/teams/{param}"), _entry("/api/teams/:id"))
    # A single-segment unknown-prefix ref is too unspecific.
    assert not match_url(
        _ref("/teams", unknown_prefix=True), _entry("/api/x/teams"),
    )


# ── Route-table build ────────────────────────────────────────────────────


def _feat(name: str, paths: tuple[str, ...], sources=None) -> DeveloperFeature:
    return DeveloperFeature(
        name=name, paths=paths,
        sources=sources or ["route"], confidence="high",
    )


def test_build_route_table_explicit_and_filesystem() -> None:
    backend = _feat(
        "org-knowledge", ("api/routers/knowledge.py",), ["fastapi-route"],
    )
    nextjs = _feat("teams", ("web/app/api/teams/route.ts",), ["route"])
    signals = {
        "fastapi": [AnchorCandidate(
            name="org-knowledge",
            paths=("api/routers/knowledge.py",),
            source="fastapi-route",
            confidence_self=0.9,
            routes=(
                ("/api/org-knowledge/{id}", "GET", "api/routers/knowledge.py"),
                ("/api/org-knowledge", "POST", "api/routers/knowledge.py"),
            ),
        )],
        "route": [AnchorCandidate(
            name="teams",
            paths=(
                "web/app/api/teams/route.ts",
                "web/app/(dash)/teams/page.tsx",  # PAGE → excluded
            ),
            source="route",
            confidence_self=0.9,
        )],
        "_errors": [],
    }
    table = build_route_table(signals, [backend, nextjs])
    got = {(e.pattern, e.method, e.feature) for e in table}
    assert got == {
        ("/api/org-knowledge/{id}", "GET", "org-knowledge"),
        ("/api/org-knowledge", "POST", "org-knowledge"),
        ("/api/teams", "GET", "teams"),
    }


def test_build_route_table_skips_unowned_and_pageless() -> None:
    signals = {
        "fastapi": [AnchorCandidate(
            name="x", paths=("api/x.py",), source="fastapi-route",
            confidence_self=0.9,
            routes=(("/api/x", "GET", "api/x.py"),),
        )],
    }
    # No feature owns api/x.py → no entry.
    assert build_route_table(signals, [_feat("y", ("api/y.py",))]) == []
    assert build_route_table(None, []) == []


def test_build_route_table_dir_prefix_ownership() -> None:
    feat = _feat("api", ("server/api",), ["fastapi-route"])
    signals = {
        "fastapi": [AnchorCandidate(
            name="api", paths=("server/api",), source="fastapi-route",
            confidence_self=0.9,
            routes=(("/api/things", "GET", "server/api/things.py"),),
        )],
    }
    table = build_route_table(signals, [feat])
    assert [(e.pattern, e.feature) for e in table] == [("/api/things", "api")]


# ── End-to-end through Stage 2.6 (synthetic FastAPI + React) ─────────────


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _ctx(repo: Path) -> SimpleNamespace:
    tracked = sorted(
        f.relative_to(repo).as_posix()
        for f in repo.rglob("*")
        if f.is_file() and "/.git" not in str(f)
    )
    return SimpleNamespace(
        repo_path=repo,
        tracked_files=tuple(tracked),
        commits=[],
        run_dir=None,
        stack="fastapi",
        monorepo=False,
        workspaces=[],
    )


def _fastapi_react_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    # Backend: FastAPI router for org-knowledge.
    _w(repo / "api/routers/knowledge.py", (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        '@router.get("/api/org-knowledge/{id}")\n'
        "def get_item(id: str): ...\n"
        '@router.post("/api/org-knowledge")\n'
        "def create_item(): ...\n"
    ))
    # Frontend api module — fetch calls, no import edge to the backend.
    _w(repo / "web/src/lib/knowledge-api.ts", (
        "export async function getItem(id: string) {\n"
        "  return fetch(`/api/org-knowledge/${id}`).then(r => r.json());\n"
        "}\n"
        "export async function createItem(body: unknown) {\n"
        '  return fetch("/api/org-knowledge", { method: "POST" });\n'
        "}\n"
    ))
    # Frontend component — calls the route directly with a concat tail.
    _w(repo / "web/src/components/KnowledgePanel.tsx", (
        "export function KnowledgePanel({ id }: { id: string }) {\n"
        '  const load = () => fetch("/api/org-knowledge/" + id);\n'
        "  return null;\n"
        "}\n"
    ))
    # Unrelated frontend file — no URL literals; must stay orphan.
    _w(repo / "web/src/components/Logo.tsx", (
        "export const Logo = () => null;\n"
    ))
    return repo


def _knowledge_signals() -> dict:
    return {
        "fastapi": [AnchorCandidate(
            name="org-knowledge",
            paths=("api/routers/knowledge.py",),
            source="fastapi-route",
            confidence_self=0.9,
            routes=(
                ("/api/org-knowledge/{id}", "GET", "api/routers/knowledge.py"),
                ("/api/org-knowledge", "POST", "api/routers/knowledge.py"),
            ),
        )],
    }


def test_e2e_frontend_files_attach_to_backend_feature(tmp_path: Path) -> None:
    repo = _fastapi_react_repo(tmp_path)
    feature = _feat(
        "org-knowledge", ("api/routers/knowledge.py",), ["fastapi-route"],
    )
    unattributed = [
        "web/src/lib/knowledge-api.ts",
        "web/src/components/KnowledgePanel.tsx",
        "web/src/components/Logo.tsx",
    ]
    result = run_membership_closure(
        [feature], unattributed, _ctx(repo),  # type: ignore[arg-type]
        extractor_signals=_knowledge_signals(),
    )
    feat = result.features[0]
    assert "web/src/lib/knowledge-api.ts" in feat.paths
    assert "web/src/components/KnowledgePanel.tsx" in feat.paths
    assert "web/src/components/Logo.tsx" not in feat.paths
    assert result.unattributed == ["web/src/components/Logo.tsx"]

    api_claim = next(
        m for m in feat.member_files
        if m.path == "web/src/lib/knowledge-api.ts"
    )
    assert api_claim.role == "url-link"
    assert api_claim.primary is True
    assert api_claim.confidence == 0.4
    assert "backend route" in api_claim.evidence

    t = result.telemetry
    assert t.backend_routes == 2
    assert t.urls_extracted >= 3
    assert t.urls_matched >= 3
    assert t.files_linked == 2
    assert t.shared_api_clients == 0


def test_e2e_shared_api_client_fan_in(tmp_path: Path) -> None:
    """A file calling MANY features' routes becomes role='shared'."""
    repo = tmp_path / "repo"
    feats = []
    routes = []
    for n in ("alpha", "beta", "gamma"):
        _w(repo / f"api/{n}.py", f'@router.get("/api/{n}")\ndef {n}(): ...\n')
        feats.append(_feat(n, (f"api/{n}.py",), ["fastapi-route"]))
        routes.append(AnchorCandidate(
            name=n, paths=(f"api/{n}.py",), source="fastapi-route",
            confidence_self=0.9,
            routes=((f"/api/{n}", "GET", f"api/{n}.py"),),
        ))
    _w(repo / "web/src/api-client.ts", (
        'export const a = () => fetch("/api/alpha");\n'
        'export const b = () => fetch("/api/beta");\n'
        'export const c = () => fetch("/api/gamma");\n'
    ))
    result = run_membership_closure(
        feats, ["web/src/api-client.ts"], _ctx(repo),  # type: ignore[arg-type]
        extractor_signals={"fastapi": routes},
    )
    # Stays orphan, every claimant records a shared provenance entry.
    assert result.unattributed == ["web/src/api-client.ts"]
    for f in result.features:
        assert "web/src/api-client.ts" not in f.paths
        claim = next(
            m for m in f.member_files if m.path == "web/src/api-client.ts"
        )
        assert claim.role == "shared"
        assert claim.primary is False
        assert "shared api-client" in claim.evidence
    assert result.telemetry.shared_api_clients == 1
    assert result.telemetry.files_linked == 0


def test_e2e_tied_election_attaches_nothing(tmp_path: Path) -> None:
    """1-vs-1 match tie between two same-priority features → skip."""
    repo = tmp_path / "repo"
    feats, routes = [], []
    for n in ("alpha", "beta"):
        _w(repo / f"api/{n}.py", "x = 1\n")
        feats.append(_feat(n, (f"api/{n}.py",), ["fastapi-route"]))
        routes.append(AnchorCandidate(
            name=n, paths=(f"api/{n}.py",), source="fastapi-route",
            confidence_self=0.9,
            routes=((f"/api/{n}", "GET", f"api/{n}.py"),),
        ))
    _w(repo / "web/both.ts", (
        'fetch("/api/alpha"); fetch("/api/beta");\n'
    ))
    result = run_membership_closure(
        feats, ["web/both.ts"], _ctx(repo),  # type: ignore[arg-type]
        extractor_signals={"fastapi": routes},
    )
    assert result.unattributed == ["web/both.ts"]
    for f in result.features:
        assert "web/both.ts" not in f.paths
        claim = next(m for m in f.member_files if m.path == "web/both.ts")
        assert claim.role == "url-link" and claim.primary is False
        assert "tied election" in claim.evidence
    assert result.telemetry.files_linked == 0


def test_e2e_no_signals_is_noop(tmp_path: Path) -> None:
    repo = _fastapi_react_repo(tmp_path)
    feature = _feat(
        "org-knowledge", ("api/routers/knowledge.py",), ["fastapi-route"],
    )
    result = run_membership_closure(
        [feature],
        ["web/src/lib/knowledge-api.ts"],
        _ctx(repo),  # type: ignore[arg-type]
    )
    assert result.telemetry.backend_routes == 0
    assert result.telemetry.files_linked == 0
    assert "web/src/lib/knowledge-api.ts" in result.unattributed


def test_e2e_determinism_scan_twice(tmp_path: Path) -> None:
    repo = _fastapi_react_repo(tmp_path)

    def run_once():
        feature = _feat(
            "org-knowledge", ("api/routers/knowledge.py",), ["fastapi-route"],
        )
        result = run_membership_closure(
            [feature],
            [
                "web/src/lib/knowledge-api.ts",
                "web/src/components/KnowledgePanel.tsx",
                "web/src/components/Logo.tsx",
            ],
            _ctx(repo),  # type: ignore[arg-type]
            extractor_signals=_knowledge_signals(),
        )
        telemetry = result.telemetry.as_dict()
        telemetry.pop("elapsed_sec")  # wall clock — not part of the output
        return (
            tuple(result.features[0].paths),
            tuple(
                (m.path, m.role, m.confidence, m.evidence, m.primary)
                for m in result.features[0].member_files
            ),
            tuple(result.unattributed),
            telemetry,
        )

    assert run_once() == run_once()
