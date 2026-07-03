"""DjangoExtractor unit tests.

Covers the activation gate (fires on a Django app via audited_stack /
secondary stack; self-skips on a non-Django repo — the pre-profile
hint/source-probe fallbacks are FOLDED into ``profiles/django.py``,
Phase B) and extraction (path/re_path/url routes, include() traversal,
DRF ViewSet/APIView detection, residual unrouted viewsets, supporting
serializer/model evidence).
"""

from __future__ import annotations

from pathlib import Path

from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.django import DjangoExtractor


def _ctx(
    *,
    repo_path: Path,
    tracked_files: list[str],
    stack: str = "python",
    audited_stack: str | None = "django-app",
    secondary_stacks: tuple[str, ...] = (),
    extractor_hints: tuple[str, ...] = (),
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
        extractor_hints=extractor_hints,
        auditor_confidence=0.9,
    )


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


_URLS = """
from django.urls import path, re_path, include
from rest_framework import routers
from . import views

urlpatterns = [
    path('users/', views.UserList.as_view(), name='user-list'),
    path('users/<int:pk>/', views.UserDetail.as_view(), name='user-detail'),
    re_path(r'^posts/(?P<pk>\\d+)/$', views.PostDetail.as_view()),
    path('legacy/', include('legacy.urls')),
]
""".strip()

_VIEWS = """
from rest_framework import viewsets, generics
from rest_framework.views import APIView
from .models import User
from .serializers import UserSerializer


class UserList(generics.ListCreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer


class UserDetail(generics.RetrieveUpdateDestroyAPIView):
    queryset = User.objects.all()


class PostDetail(APIView):
    def get(self, request, pk):
        ...


class ProjectViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()


class PlainHelper:
    pass
""".strip()

_SERIALIZERS = """
from rest_framework import serializers


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = None
""".strip()

_MODELS = """
from django.db import models


class User(models.Model):
    name = models.CharField(max_length=120)
""".strip()


# ── Activation gate ────────────────────────────────────────────────────────


def test_skips_non_django_repo(tmp_path: Path) -> None:
    _write(tmp_path / "src/index.ts", "export const x = 1;\n")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["src/index.ts"],
        stack="next-app-router",
        audited_stack="next-app-router",
    )
    assert DjangoExtractor().extract(ctx) == []


def test_gate_fires_on_audited_django_app(tmp_path: Path) -> None:
    _write(tmp_path / "app/urls.py", _URLS)
    _write(tmp_path / "app/views.py", _VIEWS)
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["app/urls.py", "app/views.py"],
        audited_stack="django-app",
    )
    assert DjangoExtractor().extract(ctx)


def test_gate_fires_on_secondary_stack(tmp_path: Path) -> None:
    _write(tmp_path / "api/urls.py", _URLS)
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["api/urls.py"],
        audited_stack="monorepo-polyglot",
        secondary_stacks=("django-app",),
    )
    assert DjangoExtractor().extract(ctx)


def test_hint_gate_folded_into_profile(tmp_path: Path) -> None:
    """Auditor-hint activation moved to the Django profile (Phase B
    fold): a hint alone no longer activates the extractor — the profile
    detects the framework structurally and force-activates it via the
    Stage-1 override seam instead."""
    _write(tmp_path / "api/urls.py", _URLS)
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["api/urls.py"],
        audited_stack="monorepo-polyglot",
        extractor_hints=("apps/api is Django; enable Django extractor",),
    )
    assert DjangoExtractor().extract(ctx) == []


def test_source_probe_folded_into_profile(tmp_path: Path) -> None:
    """The inconclusive-stack source probe moved to the Django profile
    (Phase B fold): structural markers alone no longer activate the
    extractor without a Django stack tag."""
    _write(tmp_path / "manage.py", "import django\n")
    _write(tmp_path / "app/urls.py", _URLS)
    _write(tmp_path / "app/views.py", _VIEWS)
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["manage.py", "app/urls.py", "app/views.py"],
        stack="python",
        audited_stack=None,
    )
    assert DjangoExtractor().extract(ctx) == []


def test_gate_skips_python_without_markers(tmp_path: Path) -> None:
    _write(tmp_path / "pkg/core.py", "def helper():\n    return 1\n")
    ctx = _ctx(
        repo_path=tmp_path,
        tracked_files=["pkg/core.py"],
        stack="python-library",
        audited_stack="python-library",
    )
    assert DjangoExtractor().extract(ctx) == []


# ── Extraction ─────────────────────────────────────────────────────────────


def test_path_and_re_path_routes_emitted(tmp_path: Path) -> None:
    _write(tmp_path / "app/urls.py", _URLS)
    _write(tmp_path / "app/views.py", _VIEWS)
    out = DjangoExtractor().extract(
        _ctx(
            repo_path=tmp_path,
            tracked_files=["app/urls.py", "app/views.py"],
        ),
    )
    slugs = {a.name for a in out}
    assert "users" in slugs  # from path('users/', ...)
    assert "posts" in slugs  # from re_path(r'^posts/(?P<pk>\d+)/$')


def test_routes_carry_view_symbol_and_source(tmp_path: Path) -> None:
    _write(tmp_path / "app/urls.py", _URLS)
    _write(tmp_path / "app/views.py", _VIEWS)
    out = DjangoExtractor().extract(
        _ctx(
            repo_path=tmp_path,
            tracked_files=["app/urls.py", "app/views.py"],
        ),
    )
    users = next(a for a in out if a.name == "users")
    assert users.source == "django-route"
    assert users.routes  # explicit route tuples present
    # The view symbol is preserved in the "method" slot for attribution.
    symbols = {view for _pat, view, _file in users.routes}
    assert any("UserList" in s or "UserDetail" in s for s in symbols)
    # The view's declaring file is attributed to the anchor.
    assert "app/views.py" in users.paths


def test_include_is_not_emitted_as_leaf(tmp_path: Path) -> None:
    _write(tmp_path / "app/urls.py", _URLS)
    out = DjangoExtractor().extract(
        _ctx(repo_path=tmp_path, tracked_files=["app/urls.py"]),
    )
    slugs = {a.name for a in out}
    # ``path('legacy/', include('legacy.urls'))`` mounts a sub-app; the
    # leaf 'legacy' route carries an include() so it must not become a
    # standalone routed feature here.
    legacy = [a for a in out if a.name == "legacy"]
    for a in legacy:
        assert all("include" not in v for _p, v, _f in a.routes)


def test_residual_unrouted_viewset_emitted(tmp_path: Path) -> None:
    # ProjectViewSet is declared but never wired into urlpatterns above
    # (router.register would auto-route it). It must still surface.
    _write(tmp_path / "app/urls.py", _URLS)
    _write(tmp_path / "app/views.py", _VIEWS)
    out = DjangoExtractor().extract(
        _ctx(
            repo_path=tmp_path,
            tracked_files=["app/urls.py", "app/views.py"],
        ),
    )
    slugs = {a.name for a in out}
    assert "project" in slugs


def test_plain_class_not_treated_as_view(tmp_path: Path) -> None:
    _write(tmp_path / "app/urls.py", "urlpatterns = []\n")
    _write(tmp_path / "app/views.py", _VIEWS)
    out = DjangoExtractor().extract(
        _ctx(
            repo_path=tmp_path,
            tracked_files=["app/urls.py", "app/views.py"],
        ),
    )
    # PlainHelper has no view base — must not produce an anchor.
    assert "plain-helper" not in {a.name for a in out}


def test_serializers_and_models_do_not_explode_anchors(tmp_path: Path) -> None:
    _write(tmp_path / "app/urls.py", "urlpatterns = []\n")
    _write(tmp_path / "app/serializers.py", _SERIALIZERS)
    _write(tmp_path / "app/models.py", _MODELS)
    out = DjangoExtractor().extract(
        _ctx(
            repo_path=tmp_path,
            tracked_files=[
                "app/urls.py", "app/serializers.py", "app/models.py",
            ],
        ),
    )
    # No routes, no view classes → supporting-only evidence emits no
    # standalone anchors (serializers/models are counts only).
    assert out == []


def test_all_anchors_tagged_django_route(tmp_path: Path) -> None:
    _write(tmp_path / "app/urls.py", _URLS)
    _write(tmp_path / "app/views.py", _VIEWS)
    out = DjangoExtractor().extract(
        _ctx(
            repo_path=tmp_path,
            tracked_files=["app/urls.py", "app/views.py"],
        ),
    )
    assert out
    for a in out:
        assert a.source == "django-route"
        assert a.name and a.name == a.name.lower()
