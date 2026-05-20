"""Tests for :mod:`faultline.framework_linkers.nextjs_http_route` (Sprint C4)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.framework_linkers.nextjs_http_route import (
    NextjsHttpRouteLinker,
    _file_to_url_pattern,
    _normalise_url,
    _url_pattern_to_regex,
)
from faultline.models.types import Feature
from faultline.pipeline_v2.run_logger import StageLogger


# ── Helpers ──────────────────────────────────────────────────────────────


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _ctx(repo: Path, *, stack: str = "next-app-router", audited: str | None = "next-app-router") -> SimpleNamespace:
    tracked: list[str] = []
    for f in repo.rglob("*"):
        if f.is_file() and "/.git" not in str(f):
            try:
                tracked.append(f.relative_to(repo).as_posix())
            except ValueError:
                continue
    return SimpleNamespace(
        repo_path=repo,
        tracked_files=tuple(tracked),
        run_dir=None,
        stack=stack,
        audited_stack=audited,
        secondary_stacks=(),
        monorepo=False,
        workspaces=[],
    )


def _new_feature(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        paths=list(paths),
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        layer="developer",
    )


def _log(tmp_path: Path) -> StageLogger:
    return StageLogger(tmp_path, 6, "framework_enrich_test")


# ── is_active ────────────────────────────────────────────────────────────


def test_is_active_true_for_next_app_router(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, stack="next-app-router", audited="next-app-router")
    linker = NextjsHttpRouteLinker()
    assert linker.is_active(ctx) is True


def test_is_active_true_for_next_pages_via_audited(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, stack="unknown", audited="next-pages")
    linker = NextjsHttpRouteLinker()
    assert linker.is_active(ctx) is True


def test_is_active_false_for_remix(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, stack="remix", audited="remix")
    linker = NextjsHttpRouteLinker()
    assert linker.is_active(ctx) is False


def test_is_active_false_for_django(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, stack="django", audited="django")
    linker = NextjsHttpRouteLinker()
    assert linker.is_active(ctx) is False


# ── URL normalisation ───────────────────────────────────────────────────


def test_normalise_literal_url() -> None:
    out, had_slot, external = _normalise_url("/api/rules")
    assert out == "/api/rules"
    assert had_slot is False
    assert external is False


def test_normalise_dynamic_template_literal() -> None:
    out, had_slot, external = _normalise_url("/api/rules/${id}")
    # ${var} → slash-free sentinel so it matches a route's [^/]+ slot.
    assert out == "/api/rules/__DYN__"
    assert had_slot is True
    assert external is False


def test_normalise_external_skipped() -> None:
    out, _had_slot, external = _normalise_url("https://example.com/api/x")
    assert out is None
    assert external is True


def test_normalise_drops_query() -> None:
    out, _had, _ext = _normalise_url("/api/rules?foo=1&bar=2")
    assert out == "/api/rules"


def test_normalise_skips_non_api_url() -> None:
    out, _had, _ext = _normalise_url("/foo/bar")
    assert out is None


# ── File → URL pattern ──────────────────────────────────────────────────


def test_file_to_url_pattern_app_router_literal() -> None:
    assert _file_to_url_pattern("app/api/rules/route.ts") == "/api/rules"


def test_file_to_url_pattern_app_router_dynamic() -> None:
    assert _file_to_url_pattern("app/api/rules/[id]/route.ts") == "/api/rules/[id]"


def test_file_to_url_pattern_app_router_catchall() -> None:
    assert _file_to_url_pattern("app/api/auth/[...all]/route.ts") == "/api/auth/[...all]"


def test_file_to_url_pattern_app_router_nested_in_apps() -> None:
    assert _file_to_url_pattern("apps/web/app/api/rules/route.ts") == "/api/rules"


def test_file_to_url_pattern_pages_router() -> None:
    assert _file_to_url_pattern("pages/api/health.ts") == "/api/health"


def test_file_to_url_pattern_pages_router_dynamic() -> None:
    assert _file_to_url_pattern("apps/web/pages/api/users/[id].ts") == "/api/users/[id]"


def test_file_to_url_pattern_rejects_non_route() -> None:
    assert _file_to_url_pattern("app/page.tsx") is None
    assert _file_to_url_pattern("src/components/Button.tsx") is None


# ── Pattern → regex compilation + match ─────────────────────────────────


def test_pattern_to_regex_literal_matches_literal_only() -> None:
    rx = _url_pattern_to_regex("/api/rules")
    assert rx.fullmatch("/api/rules")
    assert not rx.fullmatch("/api/rules/1")


def test_pattern_to_regex_dynamic_single_segment_match() -> None:
    rx = _url_pattern_to_regex("/api/rules/[id]")
    assert rx.fullmatch("/api/rules/abc")
    assert not rx.fullmatch("/api/rules")
    # Must not span multiple segments.
    assert not rx.fullmatch("/api/rules/a/b")


def test_pattern_to_regex_catchall_matches_arbitrary_depth() -> None:
    rx = _url_pattern_to_regex("/api/auth/[...all]")
    assert rx.fullmatch("/api/auth/login")
    assert rx.fullmatch("/api/auth/oauth/google/callback")


# ── End-to-end: route map + scan ────────────────────────────────────────


def _seed_next_repo(tmp_path: Path) -> None:
    """Set up a minimal Next App-Router repo with one route + one caller."""
    _w(
        tmp_path / "app" / "api" / "rules" / "route.ts",
        "export async function GET(req: Request) { return new Response('ok'); }\n"
        "export async function POST(req: Request) { return new Response('ok'); }\n",
    )
    _w(
        tmp_path / "app" / "api" / "rules" / "[id]" / "route.ts",
        "export async function DELETE(req: Request) { return new Response('ok'); }\n",
    )
    _w(
        tmp_path / "app" / "rules" / "page.tsx",
        """
export default function RulesPage() {
  const onCreate = async () => {
    await fetch("/api/rules", { method: "POST", body: JSON.stringify({}) });
  };
  const onDelete = async (id: string) => {
    await fetch(`/api/rules/${id}`, { method: "DELETE" });
  };
  return null;
}
""".strip(),
    )


def test_link_for_feature_emits_literal_and_dynamic(tmp_path: Path) -> None:
    _seed_next_repo(tmp_path)
    ctx = _ctx(tmp_path)
    feature = _new_feature("rules", ["app/rules/page.tsx"])

    linker = NextjsHttpRouteLinker()
    assert linker.is_active(ctx)
    with _log(tmp_path) as log:
        links = linker.link_for_feature(feature, ctx, log)

    assert len(links) == 2
    by_method = {lk.target_symbol for lk in links}
    assert "POST" in by_method
    assert "DELETE" in by_method
    confidences = sorted(lk.confidence for lk in links)
    # One literal (1.0) + one dynamic (0.7).
    assert confidences == [0.7, 1.0]
    targets = {lk.target_file for lk in links}
    assert "app/api/rules/route.ts" in targets
    assert "app/api/rules/[id]/route.ts" in targets


def test_link_for_feature_returns_empty_when_no_routes(tmp_path: Path) -> None:
    """No app/api/ files → route map empty → no links emitted, no crash."""
    _w(tmp_path / "app" / "page.tsx", "export default function P() { return null; }\n")
    ctx = _ctx(tmp_path)
    feature = _new_feature("home", ["app/page.tsx"])
    linker = NextjsHttpRouteLinker()
    # is_active stays True (Next stack), but link_for_feature returns [].
    assert linker.is_active(ctx) is True
    with _log(tmp_path) as log:
        links = linker.link_for_feature(feature, ctx, log)
    assert links == []
    assert linker.telemetry.route_map_size == 0
