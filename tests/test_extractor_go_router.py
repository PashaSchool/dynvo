"""GoRouterExtractor unit tests.

Synthetic-repo fixtures: each test writes a handful of ``.go`` files
into ``tmp_path``, builds a ``ScanContext`` with the right activation
hints, and asserts the extractor emits the expected anchors.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.go_router import (
    GO_EXTRACTION_ENV,
    GoRouterExtractor,
    _is_route_path,
    _method_prefix,
    _route_to_slug,
    go_extraction_enabled,
)


def _ctx(
    *,
    repo_path: Path,
    tracked_files: list[str],
    audited_stack: str | None = "go-server",
    stack: str | None = None,
    secondary_stacks: tuple[str, ...] = (),
) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack=stack,
        monorepo=False,
        workspaces=None,
        tracked_files=tracked_files,
        commits=[],
        stack_signals=[],
        workspace_manager=None,
        audited_stack=audited_stack,
        secondary_stacks=secondary_stacks,
        extractor_hints=(),
        auditor_confidence=0.9 if audited_stack else None,
    )


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ── path → slug ────────────────────────────────────────────────────────────


def test_route_to_slug_root() -> None:
    assert _route_to_slug("/") == "root"
    assert _route_to_slug("") == "root"
    assert _route_to_slug("/*") == "root"


def test_route_to_slug_param_braces_and_colons() -> None:
    assert _route_to_slug("/users/{id}/posts") == "users-id-posts"
    assert _route_to_slug("/api/v1/orders/:id") == "api-v1-orders-id"
    assert _route_to_slug("/healthz") == "healthz"


# ── chi ────────────────────────────────────────────────────────────────────


def test_chi_pattern_matches(tmp_path: Path) -> None:
    src = """
    package main

    import (
        "net/http"
        "github.com/go-chi/chi/v5"
    )

    func main() {
        r := chi.NewRouter()
        r.Get("/users", handleUsers)
        r.Post("/users", createUser)
        r.Get("/healthz", healthz)
        http.ListenAndServe(":8080", r)
    }
    """.strip()
    _write(tmp_path / "main.go", src)
    ctx = _ctx(repo_path=tmp_path, tracked_files=["main.go"])

    cands = GoRouterExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert {"users", "healthz"}.issubset(names)
    for c in cands:
        assert c.source == "go-router"
        # constructor visible in same file → high confidence
        assert c.confidence_self == 0.9


# ── gin ────────────────────────────────────────────────────────────────────


def test_gin_pattern_matches(tmp_path: Path) -> None:
    src = """
    package main

    import "github.com/gin-gonic/gin"

    func main() {
        r := gin.Default()
        r.GET("/api/v1/items", listItems)
        r.POST("/api/v1/items", createItem)
        r.Run()
    }
    """.strip()
    _write(tmp_path / "server.go", src)
    ctx = _ctx(repo_path=tmp_path, tracked_files=["server.go"])

    cands = GoRouterExtractor().extract(ctx)
    names = {c.name for c in cands}
    # ``/api/v1/items`` slugifies to ``api-v1-items`` (both GET + POST
    # collapse to the same slug, so we expect a single anchor).
    assert "api-v1-items" in names


# ── stdlib net/http ────────────────────────────────────────────────────────


def test_stdlib_http_matches(tmp_path: Path) -> None:
    src = """
    package main

    import "net/http"

    func main() {
        mux := http.NewServeMux()
        mux.HandleFunc("/healthz", healthz)
        mux.HandleFunc("/readyz", readyz)
        http.ListenAndServe(":8080", mux)
    }
    """.strip()
    _write(tmp_path / "main.go", src)
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["main.go"],
        audited_stack="go-server",
    )
    cands = GoRouterExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert {"healthz", "readyz"}.issubset(names)


# ── exclusions ─────────────────────────────────────────────────────────────


def test_vendor_paths_excluded(tmp_path: Path) -> None:
    src = """
    package x
    func init() {
        r := chi.NewRouter()
        r.Get("/forbidden", handler)
    }
    """.strip()
    _write(tmp_path / "vendor" / "github.com" / "foo" / "bar.go", src)
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["vendor/github.com/foo/bar.go"],
    )
    cands = GoRouterExtractor().extract(ctx)
    assert cands == []


def test_test_files_excluded(tmp_path: Path) -> None:
    src = """
    package main
    func TestX(t *testing.T) {
        r := chi.NewRouter()
        r.Get("/forbidden", h)
    }
    """.strip()
    _write(tmp_path / "main_test.go", src)
    ctx = _ctx(repo_path=tmp_path, tracked_files=["main_test.go"])
    cands = GoRouterExtractor().extract(ctx)
    assert cands == []


# ── activation gate ────────────────────────────────────────────────────────


def test_skips_on_non_go_stack(tmp_path: Path) -> None:
    """Even with real Go content in tracked_files, when the stack
    is rust-workspace the extractor must stay silent."""
    src = """
    package main
    func main() {
        r := chi.NewRouter()
        r.Get("/users", h)
    }
    """.strip()
    _write(tmp_path / "main.go", src)
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["main.go"],
        audited_stack="rust-workspace",
        stack="rust",
    )
    cands = GoRouterExtractor().extract(ctx)
    assert cands == []


def test_activates_on_stack_eq_go(tmp_path: Path) -> None:
    src = "package main\nfunc main() { r := chi.NewRouter(); r.Get(\"/x\", h) }"
    _write(tmp_path / "x.go", src)
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["x.go"],
        audited_stack=None,
        stack="go",
    )
    cands = GoRouterExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "x" in names


def test_activates_on_secondary_go(tmp_path: Path) -> None:
    src = "package main\nfunc main() { r := chi.NewRouter(); r.Get(\"/y\", h) }"
    _write(tmp_path / "y.go", src)
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["y.go"],
        audited_stack="monorepo-polyglot",
        stack=None,
        secondary_stacks=("go", "typescript"),
    )
    cands = GoRouterExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "y" in names


def test_low_confidence_when_no_constructor_in_file(tmp_path: Path) -> None:
    """When a file calls .Get(...) but the chi.NewRouter() is in
    another file, confidence drops to 0.7."""
    src = """
    package routes
    func Register(r *chi.Mux) {
        r.Get("/posts", handler)
    }
    """.strip()
    _write(tmp_path / "routes.go", src)
    ctx = _ctx(repo_path=tmp_path, tracked_files=["routes.go"])
    cands = GoRouterExtractor().extract(ctx)
    names = {c.name for c in cands}
    assert "posts" in names
    posts = [c for c in cands if c.name == "posts"][0]
    assert posts.confidence_self == 0.7


# ══════════════════════════════════════════════════════════════════════════
# S4b — FAULTLINE_GO_EXTRACTION armed extraction (default OFF)
#
# Root defect (traefik VERIFIED): the chi/gin/echo ``route_call`` patterns
# match any bare ``.Get("s")`` / ``.Set("s")``, so ``req.Header.Get(
# "Content-Type")`` mints "content-type" as a feature (19/19 traefik
# go-router anchors were header garbage), while traefik's real ``/api/**``
# surface — gorilla/mux ``router.Methods(..).Path("/x").HandlerFunc(..)`` —
# is invisible. Armed = gorilla signature + ``route_must_be_path`` filter.
# ══════════════════════════════════════════════════════════════════════════


# The exact false-positive class harvested off traefik's real code — bare
# ``.Get`` / ``.Set`` on ``http.Header`` / ``url.Values``. These are the
# named survivors that armed extraction MUST drop.
_TRAEFIK_HEADER_GARBAGE = frozenset({
    "content-type", "x-forwarded-for", "accept", "vary", "origin",
    "x-request-id", "status", "search",
})


def _traefik_shape_src() -> str:
    """A synthetic slice of traefik ``pkg/api/handler.go``: a gorilla/mux
    fluent router registering real ``/api/**`` + ``/debug/**`` routes,
    interleaved with the header/JSON-key ``.Get``/``.Set`` calls that the
    shipped patterns mis-mint."""
    return """
    package api

    import (
        "net/http"
        "github.com/gorilla/mux"
    )

    func (h *Handler) createRouter() *mux.Router {
        router := mux.NewRouter().UseEncodedPath()
        router.Methods(http.MethodGet).Path("/api/rawdata").HandlerFunc(h.getRuntimeConfiguration)
        router.Methods(http.MethodGet).Path("/api/http/routers").HandlerFunc(h.getRouters)
        router.Methods(http.MethodGet).Path("/api/http/routers/{routerID}").HandlerFunc(h.getRouter)
        router.Methods(http.MethodGet).PathPrefix("/debug/pprof/").HandlerFunc(pprof.Index)

        // Header / JSON-key access — NOT routes. Shipped patterns mis-mint
        // these off the bare ``.Get("s")``; armed extraction must drop them.
        ct := req.Header.Get("Content-Type")
        prior := req.Header.Get("X-Forwarded-For")
        accept := req.Header.Get("Accept")
        vals.Get("status")
        vals.Get("search")
        return router
    }
    """.strip()


def test_armed_traefik_shape_drops_headers_and_finds_gorilla_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARMED: gorilla/mux routes surface; header/JSON-key garbage is gone."""
    monkeypatch.setenv(GO_EXTRACTION_ENV, "1")
    _write(tmp_path / "pkg" / "api" / "handler.go", _traefik_shape_src())
    ctx = _ctx(repo_path=tmp_path, tracked_files=["pkg/api/handler.go"])

    names = {c.name for c in GoRouterExtractor().extract(ctx)}
    # Real routes (the survivors that must appear):
    assert {
        "api-rawdata",
        "api-http-routers",
        "api-http-routers-router-id",
        "debug-pprof",
    }.issubset(names)
    # Header / key garbage (the named anti-case) must be absent:
    assert names.isdisjoint(_TRAEFIK_HEADER_GARBAGE)


def test_off_traefik_shape_keeps_shipped_header_garbage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KILL-SWITCH PAIR (OFF half): with the flag OFF the board is byte-
    identical to the shipped extractor — the header false positives are
    STILL minted and the gorilla routes are STILL invisible. This locks
    ``=0`` as a forever kill-switch and documents the pre-fix defect."""
    monkeypatch.setenv(GO_EXTRACTION_ENV, "0")
    _write(tmp_path / "pkg" / "api" / "handler.go", _traefik_shape_src())
    ctx = _ctx(repo_path=tmp_path, tracked_files=["pkg/api/handler.go"])

    names = {c.name for c in GoRouterExtractor().extract(ctx)}
    # Shipped behaviour: header garbage present …
    assert "content-type" in names
    assert "x-forwarded-for" in names
    # … and gorilla routes NOT found (no gorilla signature in base set).
    assert "api-rawdata" not in names


def test_unset_matches_explicit_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset ≡ explicit ``0`` — the shipped board, byte-for-byte."""
    _write(tmp_path / "pkg" / "api" / "handler.go", _traefik_shape_src())
    ctx = _ctx(repo_path=tmp_path, tracked_files=["pkg/api/handler.go"])

    monkeypatch.delenv(GO_EXTRACTION_ENV, raising=False)
    unset = sorted(
        (c.name, c.paths, c.confidence_self)
        for c in GoRouterExtractor().extract(ctx)
    )
    monkeypatch.setenv(GO_EXTRACTION_ENV, "0")
    off = sorted(
        (c.name, c.paths, c.confidence_self)
        for c in GoRouterExtractor().extract(ctx)
    )
    assert unset == off


def test_armed_nethttp_go122_method_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ARMED: Go 1.22 net/http ServeMux ``"GET /items/{id}"`` patterns are
    recognised as PATHS (method token stripped before slugifying)."""
    monkeypatch.setenv(GO_EXTRACTION_ENV, "1")
    src = """
    package main
    func main() {
        mux := http.NewServeMux()
        mux.HandleFunc("GET /items/{id}", getItem)
        mux.HandleFunc("/healthz", healthz)
    }
    """.strip()
    _write(tmp_path / "main.go", src)
    ctx = _ctx(repo_path=tmp_path, tracked_files=["main.go"])
    names = {c.name for c in GoRouterExtractor().extract(ctx)}
    assert {"items-id", "healthz"}.issubset(names)


# ── anti-cases ───────────────────────────────────────────────────────────


def test_armed_non_go_repo_stays_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTI-CASE: even armed, a non-Go stack (rust) mints nothing — the
    flag arms Go extraction, it never activates the extractor elsewhere."""
    monkeypatch.setenv(GO_EXTRACTION_ENV, "1")
    _write(tmp_path / "main.go", _traefik_shape_src())
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["main.go"],
        audited_stack="rust-workspace",
        stack="rust",
    )
    assert GoRouterExtractor().extract(ctx) == []


def test_armed_testdata_fixtures_not_minted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTI-CASE (dev-artifact law): a real gorilla route living under
    ``testdata/`` is a fixture, never a product route — armed excludes it."""
    monkeypatch.setenv(GO_EXTRACTION_ENV, "1")
    _write(tmp_path / "pkg" / "api" / "testdata" / "fixture.go",
           _traefik_shape_src())
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["pkg/api/testdata/fixture.go"],
    )
    assert GoRouterExtractor().extract(ctx) == []


def test_armed_examples_dir_not_minted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTI-CASE (dev-artifact law): ``examples/`` demo code is not a route
    surface."""
    monkeypatch.setenv(GO_EXTRACTION_ENV, "1")
    _write(tmp_path / "examples" / "demo.go", _traefik_shape_src())
    ctx = _ctx(repo_path=tmp_path, tracked_files=["examples/demo.go"])
    assert GoRouterExtractor().extract(ctx) == []


# ── path-mechanism units ─────────────────────────────────────────────────


def test_is_route_path_mechanism() -> None:
    # URL paths — accepted:
    assert _is_route_path("/api/rawdata")
    assert _is_route_path("/")
    assert _is_route_path("GET /items/{id}")
    assert _is_route_path("POST /users")
    # Non-paths (header names / JSON keys / query params) — rejected:
    assert not _is_route_path("Content-Type")
    assert not _is_route_path("X-Forwarded-For")
    assert not _is_route_path("status")
    assert not _is_route_path("search")
    # A method token WITHOUT a following path is not a route:
    assert not _is_route_path("GET")
    assert not _is_route_path("GETTER")


def test_method_prefix_only_strips_real_method_tokens() -> None:
    assert _method_prefix("GET /x") == "GET"
    assert _method_prefix("DELETE /x/{id}") == "DELETE"
    assert _method_prefix("/x") is None
    # A word that merely starts with a method token is not a method prefix:
    assert _method_prefix("GETTER /x") is None
    assert _method_prefix("Content-Type") is None


def test_flag_default_off() -> None:
    """Belt-and-braces: the flag reader defaults OFF and honours truthy set."""
    import os
    saved = os.environ.pop(GO_EXTRACTION_ENV, None)
    try:
        assert go_extraction_enabled() is False
        for on in ("1", "true", "on", "yes", "TRUE"):
            os.environ[GO_EXTRACTION_ENV] = on
            assert go_extraction_enabled() is True
        for off in ("0", "false", "off", "no", ""):
            os.environ[GO_EXTRACTION_ENV] = off
            assert go_extraction_enabled() is False
    finally:
        os.environ.pop(GO_EXTRACTION_ENV, None)
        if saved is not None:
            os.environ[GO_EXTRACTION_ENV] = saved
