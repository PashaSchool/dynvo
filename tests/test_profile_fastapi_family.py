"""FastApiFamilyProfile — detects() grades, domain boundaries, entries.

StackProfile Phase B profile #1 (``profiles/fastapi_family.py``).
Fixtures are SYNTHETIC framework-convention trees (never corpus paths):
detection fingerprints per grade, the domain-package boundary index
(deepest-wins, generic-container rejection, min-2-files floor), the
``feature_of`` ↔ extractor-anchor name alignment contract, flow-entry
handler symbols for both grammars, and the Stage-1 override merge seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.profiles.base import FileRole
from faultline.pipeline_v2.profiles.fastapi_family import (
    FastApiDomainExtractor,
    FastApiFamilyProfile,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_1_extractors import merge_profile_extractors


# ── fixture helpers ──────────────────────────────────────────────────────────


def _write(root: Path, files: dict[str, str]) -> list[str]:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return sorted(files)


def _ctx(
    root: Path,
    files: dict[str, str],
    *,
    stack: str | None = "js-generic",
    audited: str | None = None,
) -> ScanContext:
    tracked = _write(root, files)
    return ScanContext(
        repo_path=root,
        stack=stack,
        monorepo=False,
        workspaces=None,
        tracked_files=tracked,
        commits=[],
        audited_stack=audited,
    )


_FASTAPI_ROUTER = (
    "from fastapi import APIRouter\n"
    "router = APIRouter(prefix='/checkout')\n"
    "@router.get('/items')\n"
    "async def list_items():\n"
    "    return []\n"
    "@router.post('/items')\n"
    "async def create_item(payload: dict):\n"
    "    return payload\n"
)

_FASTAPI_MAIN = (
    "from fastapi import FastAPI\n"
    "app = FastAPI()\n"
)

_LITESTAR_HANDLER = (
    "from litestar import get, post\n"
    "@get('/books')\n"
    "async def list_books() -> list:\n"
    "    return []\n"
    "@post('/books')\n"
    "async def add_book(data: dict) -> dict:\n"
    "    return data\n"
)


# ── detects() fingerprint grades ─────────────────────────────────────────────


def test_detects_stack_tag_is_strongest(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"a.py": ""}, stack="fastapi")
    assert FastApiFamilyProfile().detects(ctx) == pytest.approx(0.95)


def test_detects_audited_litestar_tag(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"a.py": ""}, stack="python-lib", audited="litestar")
    assert FastApiFamilyProfile().detects(ctx) == pytest.approx(0.95)


def test_detects_dep_plus_source_is_090(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "backend/pyproject.toml": (
            "[project]\nname = 'shop'\n"
            "dependencies = [\n    \"fastapi[standard]>=0.115\",\n    \"uvicorn\",\n]\n"
        ),
        "backend/app/main.py": _FASTAPI_MAIN,
    })
    assert FastApiFamilyProfile().detects(ctx) == pytest.approx(0.9)


def test_detects_dep_only_is_075(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "requirements.txt": "fastapi==0.115.0\n",
        "README.md": "",
    })
    assert FastApiFamilyProfile().detects(ctx) == pytest.approx(0.75)


def test_detects_source_only_is_070(tmp_path: Path) -> None:
    # No manifest at all — vendored / unusual layout; source fingerprints
    # (framework import + app construction) still identify the family.
    ctx = _ctx(tmp_path, {"svc/api.py": _FASTAPI_MAIN})
    assert FastApiFamilyProfile().detects(ctx) == pytest.approx(0.7)


def test_detects_litestar_dep_and_bare_decorators(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "pyproject.toml": "[project]\nname = 'lib'\ndependencies = ['litestar>=2']\n",
        "src/books.py": _LITESTAR_HANDLER,
    })
    assert FastApiFamilyProfile().detects(ctx) == pytest.approx(0.9)


def test_detects_framework_self_repo_by_project_name(tmp_path: Path) -> None:
    # The framework's own clone: project name IS the framework.
    ctx = _ctx(tmp_path, {
        "pyproject.toml": '[project]\nname = "litestar"\n',
        "litestar/app.py": "from litestar import Router\nr = Router(path='/')\n",
    })
    assert FastApiFamilyProfile().detects(ctx) == pytest.approx(0.9)


def test_detects_zero_on_flask_repo(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "requirements.txt": "flask\n",
        "src/app.py": "def create_app():\n    return None\n",
    }, stack="flask")
    assert FastApiFamilyProfile().detects(ctx) == 0.0


def test_detects_zero_on_next_repo(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "package.json": '{"dependencies": {"next": "15"}}',
        "app/page.tsx": "export default function Page() {}",
    }, stack="next-app-router")
    assert FastApiFamilyProfile().detects(ctx) == 0.0


def test_detects_immune_to_substring_trap(tmp_path: Path) -> None:
    """``fastapi-utils``-style tokens / ruff codes must NOT count as deps.

    The exact failure class that mis-tagged litestar as django
    (``"DJ",  # flake8-django``): substring matching over manifests.
    """
    ctx = _ctx(tmp_path, {
        "pyproject.toml": (
            "[project]\nname = 'x'\ndependencies = ['fastapi-utils-fork']\n"
            "[tool.ruff]\nselect = ['DJ']  # flake8-fastapi\n"
        ),
        "src/x.py": "VALUE = 1\n",
    })
    assert FastApiFamilyProfile().detects(ctx) == 0.0


def test_detects_workspace_fraction(tmp_path: Path) -> None:
    from faultline.pipeline_v2.stage_0_intake import Workspace

    ctx = _ctx(tmp_path, {"a.py": ""})
    ctx.workspaces = [
        Workspace(name="api", path="api", stack="fastapi"),
        Workspace(name="web", path="web", stack="next-app-router"),
    ]
    score = FastApiFamilyProfile().detects(ctx)
    assert score == pytest.approx(0.6 + 0.35 * 0.5)


# ── domain-package boundaries (FastApiDomainExtractor) ───────────────────────


def _domain_fixture(tmp_path: Path) -> ScanContext:
    return _ctx(tmp_path, {
        # Domain package: router + colocated service + models → boundary.
        "backend/app/checkout/endpoints.py": _FASTAPI_ROUTER,
        "backend/app/checkout/service.py": "def total():\n    return 0\n",
        "backend/app/checkout/models.py": "class Order:\n    pass\n",
        # Nested domain package inside another domain package.
        "backend/app/billing/api.py": _FASTAPI_ROUTER,
        "backend/app/billing/service.py": "X = 1\n",
        "backend/app/billing/invoices/views.py": _FASTAPI_ROUTER,
        "backend/app/billing/invoices/render.py": "Y = 2\n",
        # Router module inside a GENERIC container → per-module case,
        # covered by the reused route extractor, NOT a dir boundary.
        "backend/app/api/routes/items.py": _FASTAPI_ROUTER,
        "backend/app/api/routes/users.py": _FASTAPI_ROUTER,
        # Shared core — no router → unowned.
        "backend/app/core/config.py": "SETTINGS = {}\n",
        # Single-file dir with a router → below the 2-source-file floor.
        "backend/app/health/probe.py": _FASTAPI_ROUTER,
        # Tests / migrations never create nor join boundaries.
        "backend/tests/checkout/test_endpoints.py": "def test_x():\n    pass\n",
        "backend/app/checkout/migrations/0001_init.py": "OP = 1\n",
    })


def test_domain_extractor_boundaries(tmp_path: Path) -> None:
    ctx = _domain_fixture(tmp_path)
    anchors = FastApiDomainExtractor().extract(ctx)
    by_name = {a.name: a for a in anchors}

    assert set(by_name) == {"checkout", "billing", "invoices"}
    assert by_name["checkout"].paths == (
        "backend/app/checkout/endpoints.py",
        "backend/app/checkout/models.py",
        "backend/app/checkout/service.py",
    )
    # Deepest-wins: invoices files belong to invoices, not billing.
    assert by_name["billing"].paths == (
        "backend/app/billing/api.py",
        "backend/app/billing/service.py",
    )
    assert by_name["invoices"].paths == (
        "backend/app/billing/invoices/render.py",
        "backend/app/billing/invoices/views.py",
    )
    for a in anchors:
        assert isinstance(a, AnchorCandidate)
        assert a.source == "fastapi-domain"


def test_feature_of_aligns_with_anchor_names(tmp_path: Path) -> None:
    ctx = _domain_fixture(tmp_path)
    profile = FastApiFamilyProfile()
    anchors = {a.name for a in FastApiDomainExtractor().extract(ctx)}

    claimed = profile.feature_of("backend/app/checkout/service.py", ctx)
    assert claimed == "checkout"
    assert claimed in anchors  # byte-equal alignment contract

    assert profile.feature_of(
        "backend/app/billing/invoices/render.py", ctx,
    ) == "invoices"
    # Unowned: shared core, generic-container modules, tests, migrations.
    assert profile.feature_of("backend/app/core/config.py", ctx) is None
    assert profile.feature_of("backend/app/api/routes/items.py", ctx) is None
    assert profile.feature_of(
        "backend/tests/checkout/test_endpoints.py", ctx,
    ) is None
    assert profile.feature_of(
        "backend/app/checkout/migrations/0001_init.py", ctx,
    ) is None


def test_app_shell_dir_is_never_a_boundary(tmp_path: Path) -> None:
    """A COMPOSITION-DOMINANT dir is the app shell, not a capability.

    Mirror of the Next profile's ownerless ``app/`` root: the package
    root hosting ``main.py`` (module-scope app construction) + the
    aggregator router (``include_router`` mounts outnumbering its own
    routes) must not become a boundary swallowing every residual file —
    including a colocated SPA tree — into one physical-container blob.
    """
    aggregator = (
        "from fastapi import APIRouter\n"
        "from .incident import views as incident_views\n"
        "api_router = APIRouter()\n"
        "api_router.include_router(incident_views.router)\n"
        "api_router.include_router(incident_views.router)\n"
    )
    ctx = _ctx(tmp_path, {
        "src/pkg/main.py": (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
        ),
        "src/pkg/api.py": aggregator,               # composition-dominant
        "src/pkg/enums.py": "E = 1\n",              # shell residual
        "src/pkg/static/spa/index.js": "render()\n",  # colocated frontend
        "src/pkg/incident/views.py": _FASTAPI_ROUTER,
        "src/pkg/incident/service.py": "S = 1\n",
    })
    anchors = {a.name: a for a in FastApiDomainExtractor().extract(ctx)}
    assert set(anchors) == {"incident"}
    profile = FastApiFamilyProfile()
    assert profile.feature_of("src/pkg/enums.py", ctx) is None
    assert profile.feature_of("src/pkg/static/spa/index.js", ctx) is None
    assert profile.feature_of("src/pkg/incident/service.py", ctx) == "incident"


def test_route_declaring_dir_survives_shell_rule(tmp_path: Path) -> None:
    """A dir with real routes of its own is a capability even when it
    also constructs a sub-app (hosted-page shape) or mounts children —
    and an INDENTED test-helper ``app = FastAPI()`` never counts as
    composition (the false positive that killed a real ``auth``
    capability on first contact)."""
    ctx = _ctx(tmp_path, {
        "src/pkg/checkout_link/app.py": (
            "from fastapi import FastAPI\n"
            "def build():\n"
            "    app = FastAPI()\n"   # indented — NOT composition
            "    return app\n"
        ),
        "src/pkg/checkout_link/endpoints.py": _FASTAPI_ROUTER,
        "src/pkg/checkout_link/service.py": "S = 1\n",
    })
    anchors = {a.name for a in FastApiDomainExtractor().extract(ctx)}
    assert anchors == {"checkout-link"}


# ── flow entries ─────────────────────────────────────────────────────────────


def test_flow_entries_fastapi_handler_symbols(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "app/checkout/endpoints.py": _FASTAPI_ROUTER,
        "app/checkout/service.py": "Z = 1\n",
    })
    entries = FastApiFamilyProfile().flow_entries(ctx)
    got = {(e.symbol, e.route, e.kind) for e in entries}
    # The router's declared prefix ('/checkout') is composed onto the
    # leaf path so the derived flow name carries the resource.
    assert ("list_items", "GET /checkout/items", "http") in got
    assert ("create_item", "POST /checkout/items", "http") in got


def test_flow_entries_litestar_bare_decorators(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "src/books/handlers_books.py": _LITESTAR_HANDLER,
        "src/books/service.py": "B = 1\n",
    })
    entries = FastApiFamilyProfile().flow_entries(ctx)
    got = {(e.symbol, e.route) for e in entries}
    assert ("list_books", "GET /books") in got
    assert ("add_book", "POST /books") in got


# ── classification ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("path", "role"), [
    ("app/checkout/endpoints.py", FileRole.API),
    ("app/routers/users.py", FileRole.API),
    ("app/checkout/models.py", FileRole.DOMAIN),
    ("app/schemas/order.py", FileRole.DOMAIN),
    ("app/checkout/service.py", FileRole.SERVICE),
    ("app/core/settings.py", FileRole.CONFIG),
    ("app/kit/pagination.py", FileRole.LIB),
    ("tests/test_checkout.py", FileRole.TEST),
    ("app/checkout/flows.py", FileRole.UNKNOWN),
])
def test_classify_file(path: str, role: FileRole) -> None:
    assert FastApiFamilyProfile().classify_file(path) == role


# ── Stage-1 override merge seam ──────────────────────────────────────────────


class _StubExtractor:
    def __init__(self, name: str) -> None:
        self.name = name

    def extract(self, ctx: ScanContext) -> list[AnchorCandidate]:  # noqa: ARG002
        return []


def test_merge_profile_extractors_none_profile_is_identity(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"a.py": ""})
    base = [_StubExtractor("route"), _StubExtractor("schema")]
    assert merge_profile_extractors(list(base), None, ctx) == base


def test_merge_profile_extractors_default_profile_is_identity(tmp_path: Path) -> None:
    from faultline.pipeline_v2.profiles import DefaultProfile

    ctx = _ctx(tmp_path, {"a.py": ""})
    base = [_StubExtractor("route")]
    assert merge_profile_extractors(list(base), DefaultProfile(), ctx) == base


def test_merge_profile_extractors_replaces_and_appends(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"a.py": ""})
    base = [_StubExtractor("fastapi-route"), _StubExtractor("schema")]
    merged = merge_profile_extractors(base, FastApiFamilyProfile(), ctx)

    names = [e.name for e in merged]
    # Same-name override replaced IN PLACE; new extractor appended.
    assert names == ["fastapi-route", "schema", "fastapi-domain"]
    # The replacement is the profile's always-active instance, not the stub.
    assert merged[0] is not base[0]
    assert merged[0].is_active(ctx) is True


def test_fastapi_route_extractor_keeps_stack_tag_gate(tmp_path: Path) -> None:
    """The extractor still self-activates on tag-detected FastAPI repos
    (no profile needed) — but no longer probes source on inconclusive
    python tags: that fold now lives in the profile."""
    from faultline.pipeline_v2.extractors.fastapi import FastApiRouteExtractor

    tagged = _ctx(tmp_path / "a", {"m.py": _FASTAPI_MAIN}, stack="fastapi")
    assert FastApiRouteExtractor().is_active(tagged) is True

    untagged = _ctx(tmp_path / "b", {"m.py": _FASTAPI_MAIN}, stack="python-lib")
    assert FastApiRouteExtractor().is_active(untagged) is False
