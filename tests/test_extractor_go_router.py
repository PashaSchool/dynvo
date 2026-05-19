"""GoRouterExtractor unit tests.

Synthetic-repo fixtures: each test writes a handful of ``.go`` files
into ``tmp_path``, builds a ``ScanContext`` with the right activation
hints, and asserts the extractor emits the expected anchors.
"""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.go_router import (
    GoRouterExtractor,
    _route_to_slug,
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
