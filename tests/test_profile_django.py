"""DjangoProfile — detects() grades, app boundaries, URLConf entries.

StackProfile Phase B profile #2 (``profiles/django.py``). Fixtures are
SYNTHETIC framework-convention trees (never corpus paths): detection
fingerprints per grade (incl. the bare-tag guard — Stage 0's substring
probe mis-tags foreign repos as ``django``, so a tag without structural
confirmation must score 0.0), the app-boundary index (INSTALLED_APPS +
apps.py/models.py, shell rule, floor, deepest-wins, cross-layer name
mirror), the ``feature_of`` ↔ extractor-anchor name alignment contract,
URLConf view resolution (multiline calls, from-imports, package
re-exports, DRF register, wrapper fall-through), and the Stage-1
override merge seam with the route-file re-home.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.profiles.base import FileRole
from faultline.pipeline_v2.profiles.django import (
    DjangoAppExtractor,
    DjangoProfile,
    _ProfileActivatedDjangoRouteExtractor,
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


_MANAGE = (
    "import os\n"
    "import sys\n"
    "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'shop.settings')\n"
    "from django.core.management import execute_from_command_line\n"
    "execute_from_command_line(sys.argv)\n"
)

_SETTINGS = (
    "INSTALLED_APPS = [\n"
    "    'django.contrib.auth',\n"
    "    'django.contrib.contenttypes',\n"
    "    'shop.checkout',\n"
    "    'shop.catalog.apps.CatalogConfig',\n"
    "    'shop.graphql',\n"
    "    'shop.health',\n"
    "]\n"
)

_VIEWS = (
    "from django.views import View\n"
    "\n"
    "class CheckoutView(View):\n"
    "    def get(self, request):\n"
    "        return None\n"
    "\n"
    "def report(request):\n"
    "    return None\n"
    "\n"
    "def cached_list(request):\n"
    "    return None\n"
)


# ── detects() fingerprint grades ─────────────────────────────────────────────


def test_detects_tag_plus_structure_is_strongest(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"manage.py": _MANAGE}, stack="python",
               audited="django-app")
    assert DjangoProfile().detects(ctx) == pytest.approx(0.95)


def test_detects_bare_tag_without_structure_is_zero(tmp_path: Path) -> None:
    """The litestar guard: Stage 0's substring probe tags foreign repos
    ``django`` (``"DJ",  # flake8-django``); a tag with NO dep and NO
    source grammar must never win a selection."""
    ctx = _ctx(tmp_path, {
        "pyproject.toml": (
            "[project]\nname = 'x'\n"
            "[tool.ruff]\nselect = ['DJ']  # flake8-django\n"
        ),
        "src/x.py": "VALUE = 1\n",
    }, stack="django")
    assert DjangoProfile().detects(ctx) == 0.0


def test_detects_dep_plus_source_is_090(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "backend/pyproject.toml": (
            "[project]\nname = 'shop'\n"
            "dependencies = [\n    \"django[bcrypt]~=5.2\",\n]\n"
        ),
        "backend/shop/settings.py": _SETTINGS,
    })
    assert DjangoProfile().detects(ctx) == pytest.approx(0.9)


def test_detects_dep_only_is_075(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "requirements.txt": "Django==5.0\n",
        "src/tool.py": "X = 1\n",
    })
    assert DjangoProfile().detects(ctx) == pytest.approx(0.75)


def test_detects_source_only_is_070(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"backend/manage.py": _MANAGE})
    assert DjangoProfile().detects(ctx) == pytest.approx(0.7)


def test_detects_zero_on_flask_repo(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "requirements.txt": "flask\n",
        "src/app.py": "def create_app():\n    return None\n",
    }, stack="flask")
    assert DjangoProfile().detects(ctx) == 0.0


def test_detects_zero_on_next_repo(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "package.json": '{"dependencies": {"next": "15"}}',
        "app/page.tsx": "export default function Page() {}",
    }, stack="next-app-router")
    assert DjangoProfile().detects(ctx) == 0.0


def test_detects_immune_to_substring_trap(tmp_path: Path) -> None:
    """``django-filter`` / ``pytest-django`` ecosystem deps and ruff
    codes must NOT count as a Django dependency."""
    ctx = _ctx(tmp_path, {
        "pyproject.toml": (
            "[project]\nname = 'x'\n"
            "dependencies = ['django-filter~=25.1', 'pytest-django']\n"
            "[tool.ruff]\nselect = ['DJ']  # flake8-django\n"
        ),
        "src/x.py": "VALUE = 1\n",
    })
    assert DjangoProfile().detects(ctx) == 0.0


def test_detects_workspace_fraction(tmp_path: Path) -> None:
    from faultline.pipeline_v2.stage_0_intake import Workspace

    ctx = _ctx(tmp_path, {"a.py": ""})
    ctx.workspaces = [
        Workspace(name="api", path="api", stack="django-app"),
        Workspace(name="web", path="web", stack="next-app-router"),
    ]
    score = DjangoProfile().detects(ctx)
    assert score == pytest.approx(0.6 + 0.35 * 0.5)


# ── app boundaries (DjangoAppExtractor) ──────────────────────────────────────


def _app_fixture(tmp_path: Path) -> ScanContext:
    return _ctx(tmp_path, {
        # Project shell: settings + root urls + wsgi → never a boundary.
        "backend/manage.py": _MANAGE,
        "backend/shop/settings.py": _SETTINGS,
        "backend/shop/urls.py": (
            "from django.urls import include, path\n"
            "urlpatterns = [path('checkout/', include('shop.checkout.urls'))]\n"
        ),
        "backend/shop/wsgi.py": (
            "from django.core.wsgi import get_wsgi_application\n"
            "application = get_wsgi_application()\n"
        ),
        # INSTALLED_APPS app (plain dotted entry) + colocated support.
        "backend/shop/checkout/models.py": "class Order:\n    pass\n",
        "backend/shop/checkout/views.py": _VIEWS,
        "backend/shop/checkout/admin.py": "ADMIN = 1\n",
        "backend/shop/checkout/migrations/0001_init.py": "OP = 1\n",
        "backend/shop/checkout/templates/checkout/page.html": "<html/>",
        "backend/shop/checkout/tests/test_views.py": "def test_x():\n    pass\n",
        # AppConfig-style INSTALLED_APPS entry.
        "backend/shop/catalog/apps.py": "class CatalogConfig:\n    pass\n",
        "backend/shop/catalog/views.py": "def home(request):\n    return None\n",
        # Cross-layer mirror: the graphql app mirrors sibling app names.
        "backend/shop/graphql/api.py": "SCHEMA = 1\n",
        "backend/shop/graphql/mutations.py": "M = 1\n",
        "backend/shop/graphql/checkout/mutations.py": "CM = 1\n",
        # Below the 2-source-file floor → not a capability.
        "backend/shop/health/models.py": "H = 1\n",
        # Structural app OUTSIDE INSTALLED_APPS (apps.py marker).
        "backend/shop/billing/apps.py": "class BillingConfig:\n    pass\n",
        "backend/shop/billing/service.py": "S = 1\n",
        # Noise-named dir with models.py only → weak evidence rejected.
        "backend/shop/utils/models.py": "U = 1\n",
        "backend/shop/utils/text.py": "T = 1\n",
    })


def test_app_extractor_boundaries(tmp_path: Path) -> None:
    ctx = _app_fixture(tmp_path)
    anchors = {a.name: a for a in DjangoAppExtractor().extract(ctx)}

    assert set(anchors) == {"checkout", "catalog", "graphql", "billing"}
    # Colocated support artifacts belong to the app slice.
    assert "backend/shop/checkout/migrations/0001_init.py" in (
        anchors["checkout"].paths
    )
    assert "backend/shop/checkout/templates/checkout/page.html" in (
        anchors["checkout"].paths
    )
    assert "backend/shop/checkout/tests/test_views.py" in (
        anchors["checkout"].paths
    )
    # The mirror re-home: graphql/checkout/** serves the checkout app.
    assert "backend/shop/graphql/checkout/mutations.py" in (
        anchors["checkout"].paths
    )
    assert "backend/shop/graphql/checkout/mutations.py" not in (
        anchors["graphql"].paths
    )
    for a in anchors.values():
        assert isinstance(a, AnchorCandidate)
        assert a.source == "django-app"


def test_feature_of_aligns_with_anchor_names(tmp_path: Path) -> None:
    ctx = _app_fixture(tmp_path)
    profile = DjangoProfile()
    anchor_names = {a.name for a in DjangoAppExtractor().extract(ctx)}

    claimed = profile.feature_of("backend/shop/checkout/views.py", ctx)
    assert claimed == "checkout"
    assert claimed in anchor_names  # byte-equal alignment contract

    assert profile.feature_of(
        "backend/shop/graphql/checkout/mutations.py", ctx,
    ) == "checkout"
    assert profile.feature_of("backend/shop/graphql/api.py", ctx) == "graphql"
    assert profile.feature_of("backend/shop/billing/service.py", ctx) == (
        "billing"
    )
    # Shell files, floor-rejected apps, noise-named dirs → no opinion.
    assert profile.feature_of("backend/shop/urls.py", ctx) is None
    assert profile.feature_of("backend/shop/settings.py", ctx) is None
    assert profile.feature_of("backend/shop/health/models.py", ctx) is None
    assert profile.feature_of("backend/shop/utils/text.py", ctx) is None
    assert profile.feature_of("backend/manage.py", ctx) is None


def test_settings_package_parent_is_shell(tmp_path: Path) -> None:
    """A ``settings/`` PACKAGE marks its PARENT as the project shell."""
    ctx = _ctx(tmp_path, {
        "api/proj/settings/common.py": _SETTINGS.replace("shop.", "proj."),
        "api/proj/urls.py": "urlpatterns = []\n",
        "api/proj/asgi.py": "from django.core.asgi import get_asgi_application\n",
        "api/proj/models.py": "M = 1\n",  # would otherwise be structural
        "api/proj/celery.py": "C = 1\n",
        "api/proj/checkout/models.py": "O = 1\n",
        "api/proj/checkout/views.py": _VIEWS,
    })
    anchors = {a.name for a in DjangoAppExtractor().extract(ctx)}
    assert anchors == {"checkout"}
    assert DjangoProfile().feature_of("api/proj/celery.py", ctx) is None


def test_deepest_app_wins_nested_boundaries(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {
        "shop/store/apps.py": "class StoreConfig:\n    pass\n",
        "shop/store/views.py": _VIEWS,
        "shop/store/invoices/apps.py": "class InvoicesConfig:\n    pass\n",
        "shop/store/invoices/models.py": "I = 1\n",
    })
    anchors = {a.name: a for a in DjangoAppExtractor().extract(ctx)}
    assert set(anchors) == {"store", "invoices"}
    assert "shop/store/invoices/models.py" in anchors["invoices"].paths
    assert "shop/store/invoices/models.py" not in anchors["store"].paths


# ── flow entries (URLConf resolution) ────────────────────────────────────────


def _url_fixture(tmp_path: Path) -> ScanContext:
    return _ctx(tmp_path, {
        "shop/checkout/urls.py": (
            "import external_lib\n"
            "import shop.checkout.views\n"
            "from django.urls import include, path\n"
            "from django.views.decorators.cache import cache_page\n"
            "from rest_framework import routers\n"
            "from .views import (\n"
            "    CheckoutView,  # class-based\n"
            "    OrderViewSet,\n"
            ")\n"
            "router = routers.DefaultRouter()\n"
            "router.register('orders', OrderViewSet)\n"
            "urlpatterns = [\n"
            "    path(\n"
            "        'start/<int:pk>/',\n"
            "        CheckoutView.as_view(),\n"
            "        name='start',\n"
            "    ),\n"
            "    path('report/', shop.checkout.views.report, name='report'),\n"
            "    path('cached/', cache_page(60)(shop.checkout.views.cached_list)),\n"
            "    path('api/', include('shop.checkout.api')),\n"
            "    path('ext/', external_lib.view),\n"
            "]\n"
        ),
        "shop/checkout/views.py": _VIEWS + (
            "\nclass OrderViewSet:\n    pass\n"
        ),
        # A views PACKAGE re-export (one hop).
        "shop/catalog/urls.py": (
            "from django.urls import path\n"
            "from .views import CatalogHome\n"
            "urlpatterns = [path('', CatalogHome.as_view(), name='home')]\n"
        ),
        "shop/catalog/views/__init__.py": "from .pages import CatalogHome\n",
        "shop/catalog/views/pages.py": (
            "class CatalogHome:\n    pass\n"
        ),
        # A urls/ PACKAGE module (DRF resource-split convention).
        "shop/api/urls/project.py": (
            "from django.urls import path\n"
            "from shop.checkout.views import CheckoutView\n"
            "urlpatterns = [path('projects/', CheckoutView.as_view())]\n"
        ),
    })


def test_flow_entries_resolve_view_files(tmp_path: Path) -> None:
    ctx = _url_fixture(tmp_path)
    entries = DjangoProfile().flow_entries(ctx)
    got = {(e.path, e.symbol, e.route) for e in entries}

    # Multiline CBV via from-import (comment inside the paren list).
    assert ("shop/checkout/views.py", "CheckoutView", "/start/<int:pk>") in got
    # Dotted function reference via plain import.
    assert ("shop/checkout/views.py", "report", "/report") in got
    # Decorator wrapper falls through to the wrapped view.
    assert ("shop/checkout/views.py", "cached_list", "/cached") in got
    # DRF router registration.
    assert ("shop/checkout/views.py", "OrderViewSet", "/orders") in got
    # Package __init__ re-export followed one hop.
    assert ("shop/catalog/views/pages.py", "CatalogHome", "") in got
    # urls/ package module.
    assert ("shop/checkout/views.py", "CheckoutView", "/projects") in got
    # include() is a structural mount, never a leaf entry.
    assert not any(e.route == "/api" for e in entries)
    # Unresolvable reference falls back to the URLConf file itself.
    assert ("shop/checkout/urls.py", "", "/ext") in got
    for e in entries:
        assert e.kind == "http"


# ── classification ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("path", "role"), [
    ("app/checkout/urls.py", FileRole.API),
    ("app/checkout/views.py", FileRole.API),
    ("app/checkout/views/pages.py", FileRole.API),
    ("app/checkout/serializers.py", FileRole.API),
    ("app/checkout/models.py", FileRole.DOMAIN),
    ("app/checkout/models/order.py", FileRole.DOMAIN),
    ("app/checkout/tasks.py", FileRole.SERVICE),
    ("app/checkout/management/commands/sync.py", FileRole.SERVICE),
    ("app/checkout/admin.py", FileRole.CONFIG),
    ("app/checkout/apps.py", FileRole.CONFIG),
    ("app/checkout/migrations/0001_init.py", FileRole.CONFIG),
    ("proj/settings.py", FileRole.CONFIG),
    ("proj/settings/common.py", FileRole.CONFIG),
    ("app/checkout/templates/checkout/page.html", FileRole.COMPONENT),
    ("app/checkout/templatetags/tags.py", FileRole.COMPONENT),
    ("app/common/text.py", FileRole.LIB),
    ("app/checkout/tests/test_views.py", FileRole.TEST),
    ("app/checkout/flows.py", FileRole.UNKNOWN),
])
def test_classify_file(path: str, role: FileRole) -> None:
    assert DjangoProfile().classify_file(path) == role


# ── Stage-1 override merge seam ──────────────────────────────────────────────


class _StubExtractor:
    def __init__(self, name: str) -> None:
        self.name = name

    def extract(self, ctx: ScanContext) -> list[AnchorCandidate]:  # noqa: ARG002
        return []


def test_merge_profile_extractors_replaces_and_appends(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, {"a.py": ""})
    base = [_StubExtractor("django-route"), _StubExtractor("schema")]
    merged = merge_profile_extractors(base, DjangoProfile(), ctx)

    names = [e.name for e in merged]
    # Same-name override replaced IN PLACE; new extractor appended.
    assert names == ["django-route", "schema", "django-app"]
    assert merged[0] is not base[0]
    assert merged[0].is_active(ctx) is True


def test_route_override_rehomes_route_files(tmp_path: Path) -> None:
    """The profile-supplied route extractor re-homes each route tuple's
    file from the URLConf onto the file declaring the routed view (and
    adds it to the anchor's paths) — routes_index then agrees with the
    profile's flow entries."""
    ctx = _ctx(tmp_path, {
        "shop/checkout/urls.py": (
            "from django.urls import path\n"
            "from .views import CheckoutView\n"
            "urlpatterns = [\n"
            "    path('start/', CheckoutView.as_view(), name='start'),\n"
            "]\n"
        ),
        "shop/checkout/views.py": _VIEWS,
    })
    anchors = _ProfileActivatedDjangoRouteExtractor().extract(ctx)
    assert anchors, "route extractor must fire without a stack tag"
    routed = [a for a in anchors if a.routes]
    assert routed
    files = {f for a in routed for _p, _m, f in a.routes}
    assert files == {"shop/checkout/views.py"}
    assert all(
        "shop/checkout/views.py" in a.paths for a in routed
    )


def test_django_route_extractor_keeps_stack_tag_gate(tmp_path: Path) -> None:
    """The extractor still self-activates on tag-detected Django repos
    (no profile needed) — but no longer probes hints/source on
    inconclusive tags: that fold now lives in the profile."""
    from faultline.pipeline_v2.extractors.django import DjangoExtractor

    files = {
        "app/urls.py": (
            "from django.urls import path\n"
            "from .views import CheckoutView\n"
            "urlpatterns = [path('start/', CheckoutView.as_view())]\n"
        ),
        "app/views.py": _VIEWS,
    }
    tagged = _ctx(tmp_path / "a", dict(files), stack="django-app")
    assert DjangoExtractor().is_active(tagged) is True

    untagged = _ctx(tmp_path / "b", dict(files), stack="python-lib")
    assert DjangoExtractor().is_active(untagged) is False
