"""DjangoExtractor — Django / DRF URLConf + view parser → anchors.

Parses a Django project's URL routing and class-based / DRF views into
deterministic Stage-1 anchors. Three signal families are read:

  * ``urls.py`` ``urlpatterns`` entries — ``path()`` / ``re_path()`` /
    legacy ``url()`` — name the leaf URL, the HTTP-routed view symbol,
    and (via ``name=``) the route's reverse name. ``include(...)``
    stitches a sub-app's URLConf under a path prefix, so a route's
    full pattern is ``<include prefix> + <leaf>``.
  * Class-based views & DRF view classes (``ViewSet`` / ``APIView`` /
    the generic ``*APIView`` family / Django generic CBVs) in
    ``views.py`` / ``viewsets.py`` / ``api.py`` — these are the real
    entry-point symbols downstream flow detection (Stage 3) attributes
    behaviour to.
  * Serializers (``serializers.py``) + models (``models.py``) — counted
    as *supporting* evidence only (they confirm a real Django/DRF API
    and back the activation gate); we do NOT emit a standalone anchor
    per serializer/model to avoid exploding the feature count.

Like the FastAPI extractor (and unlike the filesystem ``route``
extractor) the URL pattern + view symbol live INSIDE the source, so each
:class:`AnchorCandidate` carries explicit ``routes`` tuples
``(pattern, method, file)`` that ``build_routes_index`` reads directly.
Django routes don't pin an HTTP verb at the URLConf level (the view
class dispatches by method), so the method slot carries the view symbol
reference instead — preserving the entry-point symbol for Stage 3 and
symbol attribution.

We use REGEX deliberately — not the Python AST — to match the style of
the other extractors and stay robust to partial/invalid files. Patterns
live in ``eval/stacks/django.yaml`` (per ``stack-pattern-library``);
this module only compiles + applies them.

Activation gate: the extractor fires when Stage 0 / the auditor
classified the repo (or the current workspace) as ``django-app`` /
``django`` (primary OR secondary). Structural activation for repos
whose stack TAG is inconclusive (hybrid monorepos tagged
``js-generic``, python repos with Django source) is folded into the
Django framework profile (StackProfile Phase B): the profile detects
the framework from manifests + source fingerprints and supplies an
always-active instance of this extractor through the Stage-1 override
seam. Self-skips to ``[]`` on non-Django repos.

No LLM. No network. Pure file-system scan + regex.
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    is_any_stack,
    is_noise,
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


def _load_config() -> dict:
    """Load django.yaml from the packaged data tree (hermetic)."""
    return load_stack_yaml("django")


# Compiled-regex cache keyed by id(config) so test reloads don't reuse
# stale patterns.
_COMPILED_CACHE: dict[int, "_Compiled"] = {}


class _Compiled:
    """Compiled regexes + scalar config for one django.yaml dict."""

    __slots__ = (
        "route_re",
        "include_re",
        "view_ref_re",
        "view_class_re",
        "view_base_markers",
        "serializer_re",
        "model_re",
        "urls_basenames",
        "view_basenames",
        "serializer_basenames",
        "model_basenames",
        "file_suffix",
        "excludes",
        "conf_route_with_view",
        "conf_route_only",
        "conf_viewset_only",
    )

    def __init__(self, config: dict) -> None:
        rx = config.get("route_extraction") or {}
        vx = config.get("view_extraction") or {}
        sx = config.get("support_extraction") or {}

        # Built-in defaults mirror the YAML so a missing/garbled file still
        # yields a working extractor (the YAML is the source of truth but
        # must not be a single point of failure).
        route = rx.get("route_pattern") or (
            r"\b(path|re_path|url)\(\s*[rR]?['\"]([^'\"]*)['\"]\s*,\s*([^\n]*)"
        )
        include = rx.get("include_pattern") or (
            r"include\(\s*\(?\s*[rR]?['\"]?([\w./]+)['\"]?"
        )
        view_ref = rx.get("view_ref_pattern") or (
            r"([A-Za-z_][\w.]*)\s*\.as_view\s*\(|name\s*=\s*['\"]([^'\"]*)['\"]"
        )
        view_class = vx.get("view_class_pattern") or (
            r"class\s+([A-Za-z_]\w*)\s*\(\s*([^)]*)\)\s*:"
        )
        serializer = sx.get("serializer_pattern") or (
            r"class\s+([A-Za-z_]\w*)\s*\(\s*[^)]*[Ss]erializer[^)]*\)\s*:"
        )
        model = sx.get("model_pattern") or (
            r"class\s+([A-Za-z_]\w*)\s*\(\s*[^)]*[Mm]odels?\.Model[^)]*\)\s*:"
        )

        self.route_re = re.compile(route)
        self.include_re = re.compile(include)
        self.view_ref_re = re.compile(view_ref)
        self.view_class_re = re.compile(view_class)
        self.serializer_re = re.compile(serializer)
        self.model_re = re.compile(model)

        self.view_base_markers = tuple(
            str(m) for m in (vx.get("view_base_markers") or ()) if isinstance(m, str)
        ) or (
            "ViewSet", "APIView", "GenericAPIView", "View",
        )

        self.urls_basenames = tuple(
            str(b) for b in (rx.get("urls_file_basenames") or ("urls.py",))
        )
        self.view_basenames = tuple(
            str(b) for b in (vx.get("file_basename_hints") or ("views.py",))
        )
        self.serializer_basenames = tuple(
            str(b) for b in (sx.get("serializer_basename_hints") or ("serializers.py",))
        )
        self.model_basenames = tuple(
            str(b) for b in (sx.get("model_basename_hints") or ("models.py",))
        )

        self.file_suffix = str(rx.get("file_suffix") or ".py")
        self.excludes = tuple(
            str(p) for p in (config.get("excludes") or []) if isinstance(p, str)
        )
        conf = config.get("confidence") or {}
        self.conf_route_with_view = float(conf.get("route_with_view", 0.9))
        self.conf_route_only = float(conf.get("route_only", 0.78))
        self.conf_viewset_only = float(conf.get("viewset_only", 0.72))


def _compile(config: dict) -> _Compiled:
    key = id(config)
    cached = _COMPILED_CACHE.get(key)
    if cached is None:
        cached = _Compiled(config)
        _COMPILED_CACHE[key] = cached
    return cached


# ── Activation gate ────────────────────────────────────────────────────────


def _is_django_app(ctx: "ScanContext") -> bool:
    """Stack-tag gate only — structural detection lives in the profile.

    The pre-profile fallbacks (auditor-hint substring match + probing
    .py source for Django markers when the Stage-0 tag was an
    inconclusive python flavour) moved into
    ``profiles/django.py`` (Phase B activation fold): the profile's
    ``detects()`` covers every structural case — including the
    hybrid-monorepo shapes the tag-based branch could never reach — and
    force-activates this extractor via the Stage-1 override seam.
    """
    if is_any_stack(ctx, "django-app", "django"):
        return True
    return (ctx.audited_stack or "").lower().startswith("django")


# ── Helpers ────────────────────────────────────────────────────────────────


def _join_path(*parts: str) -> str:
    """Join URL parts, collapsing duplicate slashes; keep ``<param>``."""
    joined = "/".join(p.strip("/") for p in parts if p)
    joined = re.sub(r"/{2,}", "/", joined)
    return "/" + joined if not joined.startswith("/") else joined


def _route_to_slug(route: str) -> str:
    """Slug from a Django URL pattern.

    Drops the leading anchors/escapes of ``re_path`` patterns
    (``^``, ``$``, ``\\``) and the named-group syntax
    ``(?P<pk>\\d+)`` so ``^posts/(?P<pk>\\d+)/$`` → ``posts``.
    """
    if not route:
        return "root"
    cleaned = re.sub(r"\(\?P<\w+>[^)]*\)", " ", route)
    cleaned = re.sub(r"[\^\$\\()?+*\[\]{}|]", " ", cleaned)
    cleaned = cleaned.replace("<", " ").replace(">", " ").replace(":", " ")
    head = cleaned.strip("/ ").split("/", 1)[0].strip()
    slug = slugify(head)
    return slug or "root"


# ── B2: apps-as-domains + URL-param transparency (FAULTLINE_DJANGO_APP_DOMAINS)
# Django URL captures (``<int:pk>`` / ``<slug:name>`` / ``<uuid:id>`` /
# ``(?P<pk>\d+)``) are IDENTITY params, not resources — the ts-side
# tenancy-transparency law applied to Python. Left in the pattern they
# leak into deterministic flow/UF names ("recruitment manager int mid
# int rid"). Stripped, the resource noun surfaces ("recruitment
# manager") and downstream naming matches the product vocabulary.

_PARAM_NAMED_GROUP_RE = re.compile(r"\(\?P<\w+>[^)]*\)")  # (?P<pk>\d+)
_PARAM_ANGLE_RE = re.compile(r"<[^>]*>")                  # <int:pk> / <slug>
_PARAM_BARE_GROUP_RE = re.compile(r"\([^)]*\)")           # bare (\d+) group
_MULTISLASH_RE = re.compile(r"/{2,}")


def _app_domains_on() -> bool:
    """Opt-in switch for the B2 Django URL-param transparency — default OFF.

    The transparency rewrites the anchor slug + the emitted route pattern,
    and the pattern is SERIALIZED into ``routes_index`` (scan output that
    ``normalize_scan`` does NOT strip) — so default-ON would drift the
    snapshot-gate SHA on every pinned Django repo (saleor / weblate).
    Shipping it OFF keeps the merge output-neutral (byte-identical to
    main) with zero re-pin; enable per-scan with
    ``FAULTLINE_DJANGO_APP_DOMAINS=1`` (unset / ``=0`` = legacy).
    """
    return (os.environ.get("FAULTLINE_DJANGO_APP_DOMAINS", "0")
            or "0").strip().lower() not in {"0", "false", "no", "off"}


def _strip_url_params(route: str) -> str:
    """Drop every URL-capture param + regex scaffolding from a pattern.

    ``employee/<int:pk>/edit`` → ``employee/edit`` ;
    ``^posts/(?P<pk>\\d+)/$`` → ``posts`` ;
    ``recruitment-manager/<int:mid>/<int:rid>/`` → ``recruitment-manager``.
    Static resource segments survive; identity params vanish.
    """
    r = _PARAM_NAMED_GROUP_RE.sub("", route)
    r = _PARAM_ANGLE_RE.sub("", r)
    r = _PARAM_BARE_GROUP_RE.sub("", r)
    r = re.sub(r"[\^\$\\?+*\[\]{}|]", "", r)
    r = _MULTISLASH_RE.sub("/", r)
    return r.strip("/ ")


def _transparent_pattern(route: str) -> str:
    """Param-transparent, joined route pattern (``/`` for an all-param route)."""
    seg = _strip_url_params(route)
    return _join_path(seg) if seg else "/"


def _route_to_slug_transparent(route: str) -> str:
    """Slug = first STATIC (non-param) path segment of the pattern.

    Unlike :func:`_route_to_slug` (which can yield ``int-pk`` when a
    pattern LEADS with a capture, e.g. ``<int:pk>/edit``), this skips
    param-only segments so the resource root always surfaces.
    """
    cleaned = _strip_url_params(route)
    for seg in cleaned.split("/"):
        seg = seg.strip()
        if seg:
            slug = slugify(seg)
            if slug:
                return slug
    return "root"


def _view_symbol(arg_blob: str, c: "_Compiled") -> str | None:
    """Extract the view symbol / reverse name from a route's arg blob."""
    m = c.view_ref_re.search(arg_blob)
    if not m:
        return None
    # group 1 = ``Foo.as_view`` symbol; group 2 = ``name=`` reverse name.
    return m.group(1) or m.group(2)


def _is_excluded(rel_path: str, excludes: tuple[str, ...]) -> bool:
    p = f"/{posix(rel_path)}"
    return any(ex and ex in p for ex in excludes)


def _basename_matches(rel_path: str, basenames: tuple[str, ...]) -> bool:
    base = Path(rel_path).name
    return any(base == b or base.endswith(b) for b in basenames)


def _is_http_view_class(bases: str, markers: tuple[str, ...]) -> bool:
    return any(marker in bases for marker in markers)


# ── Extractor ──────────────────────────────────────────────────────────────


class DjangoExtractor:
    """Django / DRF URLConf + view parser. Emits anchors + explicit routes.

    Implements the :class:`AnchorExtractor` Protocol. ``name`` is
    ``django-route`` so the source slug + ``scan_meta.extractor_hits``
    key both read ``django-route``.
    """

    name = "django-route"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config if config is not None else _load_config()

    def is_active(self, ctx: "ScanContext") -> bool:
        return _is_django_app(ctx)

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        c = _compile(self._config)
        if not self.is_active(ctx):
            return []
        app_domains = _app_domains_on()

        # ── Pass A: collect HTTP view classes per file (symbol → file) ──
        # Maps a view class name to the file that declares it, so a route
        # referencing ``UserViewSet`` can attribute to ``views.py``.
        view_class_files: dict[str, str] = {}
        viewset_classes: dict[str, str] = {}  # name → file (for residual anchors)

        # ── Pass B: collect routes per urls.py module ──
        # slug → {"routes": [(pattern, view_symbol_or_name, file)], "files": set}
        buckets: dict[str, dict] = defaultdict(
            lambda: {"routes": [], "files": set(), "has_view": False},
        )
        serializer_count = 0
        model_count = 0
        # View class names referenced by a parsed route (by bare class stem).
        routed_view_symbols: set[str] = set()

        # Pass A reads every file to populate ``view_class_files`` BEFORE
        # any route is parsed; route→view-file attribution in Pass B then
        # resolves regardless of the order tracked_files lists urls.py vs
        # views.py. Cache the read text so Pass B does not re-read.
        file_text: dict[str, str] = {}
        for rel_path in ctx.tracked_files:
            if not rel_path.endswith(c.file_suffix):
                continue
            if _is_excluded(rel_path, c.excludes):
                continue
            text = read_text(ctx.repo_path / rel_path)
            if not text:
                continue
            rel_posix = posix(rel_path)
            file_text[rel_path] = text

            # View classes (CBV / DRF) — any .py may declare them but we
            # only pay the regex on files that contain ``class``.
            if "class" in text and (
                _basename_matches(rel_path, c.view_basenames)
                or any(mk in text for mk in c.view_base_markers)
            ):
                for m in c.view_class_re.finditer(text):
                    cls_name = m.group(1)
                    bases = m.group(2) or ""
                    if _is_http_view_class(bases, c.view_base_markers):
                        view_class_files[cls_name] = rel_posix
                        if "ViewSet" in bases or "APIView" in bases:
                            viewset_classes[cls_name] = rel_posix

            # Supporting evidence — serializers + models (counts only).
            if _basename_matches(rel_path, c.serializer_basenames):
                serializer_count += len(c.serializer_re.findall(text))
            if _basename_matches(rel_path, c.model_basenames):
                model_count += len(c.model_re.findall(text))

        # ── Pass B: parse routes (view_class_files is now complete) ──
        for rel_path, text in file_text.items():
            rel_posix = posix(rel_path)

            # Routes — only urls.py-shaped files with urlpatterns.
            if not _basename_matches(rel_path, c.urls_basenames):
                continue
            if "urlpatterns" not in text:
                continue

            # Module-level include() prefixes: a ``path("api/", include(...))``
            # contributes its prefix to the included sub-URLConf. Cross-file
            # include resolution would require following module references;
            # we compose the include prefix onto the SAME file's nested
            # routes when present and otherwise treat each route leaf as a
            # top-level resource (the leaf segment is the feature root —
            # robust without resolving the dotted include target).
            for line in text.splitlines():
                stripped = line.split("#", 1)[0]
                if not stripped.strip():
                    continue
                for m in c.route_re.finditer(stripped):
                    func = m.group(1)
                    route_literal = m.group(2)
                    arg_blob = m.group(3) or ""

                    # An include() route is structural (mounts a sub-app);
                    # skip emitting it as its own leaf feature but keep its
                    # prefix as the resource name when meaningful.
                    is_include = "include(" in arg_blob
                    # B2 (FAULTLINE_DJANGO_APP_DOMAINS): param-transparent
                    # slug + route pattern so identity captures never leak
                    # into deterministic flow/UF names. Flag off → the
                    # legacy slug/pattern, byte-for-byte.
                    if app_domains:
                        slug = _route_to_slug_transparent(route_literal)
                        route_pattern = _transparent_pattern(route_literal)
                    else:
                        slug = _route_to_slug(route_literal)
                        route_pattern = _join_path(route_literal)
                    if not slug or slug == "root" or is_noise(slug):
                        if not is_include:
                            continue
                        # include at root/noise prefix → skip silently.
                        continue

                    view_sym = None if is_include else _view_symbol(arg_blob, c)
                    # The "method" slot carries the view symbol reference so
                    # downstream symbol attribution has the entry point.
                    method_slot = view_sym or func.upper()
                    buckets[slug]["routes"].append(
                        (route_pattern, method_slot, rel_posix),
                    )
                    buckets[slug]["files"].add(rel_posix)
                    if view_sym:
                        buckets[slug]["has_view"] = True
                        # Attribute the view's declaring file too, and record
                        # the view symbol so its class is not re-emitted as a
                        # residual (a routed view sharing a file with an
                        # unrouted one must NOT suppress the latter).
                        base_sym = view_sym.split(".")[-1]
                        routed_view_symbols.add(base_sym)
                        decl = view_class_files.get(base_sym)
                        if decl:
                            buckets[slug]["files"].add(decl)

        out: list[AnchorCandidate] = []

        for slug, data in buckets.items():
            routes = data["routes"]
            if not routes:
                continue
            files = data["files"]
            conf = (
                c.conf_route_with_view if data["has_view"] else c.conf_route_only
            )
            sample = ", ".join(
                f"{view} {pat}" for pat, view, _ in routes[:5]
            )
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=tuple(sorted(files)),
                    source=self.name,
                    confidence_self=conf,
                    rationale=f"django urls: {sample}",
                    routes=tuple(routes),
                ),
            )

        # Residual DRF/CBV view classes never wired into a parsed route
        # (e.g. router.register(...) auto-routing or includes we didn't
        # resolve) still represent real entry points — emit one anchor per
        # such viewset file, named by the class, so they aren't lost.
        residual_by_file: dict[str, set[str]] = defaultdict(set)
        for cls_name, file_str in viewset_classes.items():
            if cls_name in routed_view_symbols:
                continue
            residual_by_file[file_str].add(cls_name)

        for file_str, classes in residual_by_file.items():
            # Name by the shortest class stem (most resource-like), e.g.
            # ``UserViewSet`` → ``user``. ``classes`` is a SET — a bare
            # ``min(..., key=len)`` resolves length ties by set-iteration
            # order (PYTHONHASHSEED), which made the residual anchor NAME
            # nondeterministic (weblate flake 2026-07-03). Tie-break
            # lexicographically for a stable pick.
            best = min(classes, key=lambda n: (len(n), n))
            stem = re.sub(
                r"(ViewSet|APIView|View)$", "", best,
            ) or best
            slug = slugify(stem)
            if not slug or is_noise(slug):
                continue
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=(file_str,),
                    source=self.name,
                    confidence_self=c.conf_viewset_only,
                    rationale=(
                        f"django DRF view classes (unrouted): "
                        f"{', '.join(sorted(classes)[:5])}"
                    ),
                    routes=(),
                ),
            )

        if out:
            logger.debug(
                "django-route: %d anchors (serializers=%d models=%d)",
                len(out), serializer_count, model_count,
            )
        return out


__all__ = ["DjangoExtractor"]
