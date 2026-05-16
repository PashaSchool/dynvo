"""Feature protection (Sprint 8g, simplified Sprint 8h).

Marks features whose paths come from a strong structural signal so
downstream mergers and noise filters leave them alone. Without this,
prompt-tightening in the LLM canonicalizer caused product capabilities
like Templates, Webhooks, Branding, Audit-Logs to be merged into bigger
neighbours simply because they had few owned paths — see
``memory/finding-merge-vs-recall.md``.

Protection is structural — patterns are folder-shape, not folder-name,
per ``rule-no-repo-specific-paths``. A feature is protected when at
least one of its owned paths matches one of the patterns below.

The protection ``reason`` (a short label) is recorded on
``Feature.protection_reason`` for debugging and dashboard surfacing.

Sprint 8h: dropped the subfeatures/lineage container — it never
activated in practice and added unnecessary nesting to the JSON model.
Protection alone proved sufficient to keep small-but-real features in
the top-level list.
"""

from __future__ import annotations

import re

# ── Pattern catalog ──────────────────────────────────────────────────
#
# Each pattern returns either a "shape match" (file existed in a known
# structural role and the captured slug name informs which feature owns
# it) or an "any-feature shield" (the file is so authoritative that
# whichever feature owns it must be protected regardless of name).

# 1. tRPC subrouter — packages/<workspace>/server/<slug>-router/router.ts
#    Captures <slug>; matches features named the same.
_TRPC_FILE_RE = re.compile(r"(?:^|/)([^/]+)-router/router\.ts$")

# 2. Workspace package — packages/<slug>/...
#    Captures <slug>; matches features whose name (or tail-segment)
#    equals the package directory.
_PACKAGE_RE = re.compile(r"^packages/([^/]+)/")

# 3. Single-file or folder route — apps/<app>/[app/]routes/<slug>(.ext|/)
#    Covers Remix flat-routes, Next App Router, plain Express router files.
_ROUTE_FILE_RE = re.compile(
    r"^apps/[^/]+/(?:app/)?routes/(?:_?[^/.]+\+/)?([^/.]+)(?:[./]|$)"
)

# 4. Server-only domain folder — packages/<workspace>/server-only/<slug>/...
#    Sprint 8h widening: covers documenso-style "packages/lib/server-only/
#    templates/...". Captures <slug>.
_SERVER_ONLY_RE = re.compile(
    r"^packages/[^/]+/server-only/([^/]+)/"
)

# 5. tRPC namespace folder (no router.ts file but obvious by name) —
#    packages/<workspace>/server/<slug>-router/<file>.ts. Matches when
#    feature name equals <slug>.
_TRPC_NS_RE = re.compile(
    r"(?:^|/)([^/]+)-router/[^/]+\.ts$"
)

# 6. Plugin / extension folder — packages/<slug>-plugin/... or
#    plugins/<slug>/... Generic plugin pattern.
_PLUGIN_RE = re.compile(
    r"(?:^|/)plugins?/([^/]+)/"
)

# 7. Service / module folder — services/<slug>/ or modules/<slug>/
_SERVICE_RE = re.compile(
    r"(?:^|/)(?:services|modules)/([^/]+)/"
)

# 8. Route group (Next App Router) — apps/<app>/app/(<group>)/...
_ROUTE_GROUP_RE = re.compile(
    r"^apps/[^/]+/app/\(([^)/]+)\)/"
)

# 9. Python package subdir — <pkg>/<slug>/__init__.py
#    Captures <slug>; covers fastapi-style flat-layout libraries
#    (``fastapi/security/__init__.py``, ``fastapi/dependencies/...``).
_PY_SUBPKG_RE = re.compile(
    r"^[a-zA-Z_][a-zA-Z0-9_]*/([a-zA-Z_][a-zA-Z0-9_]*)/__init__\.py$"
)

# 10. Python single-file module under a package root —
#     <pkg>/<slug>.py (e.g. ``axios/cancel.py``, ``requests/sessions.py``).
#     Lower-confidence than __init__.py but still structural.
_PY_MODULE_RE = re.compile(
    r"^[a-zA-Z_][a-zA-Z0-9_]*/([a-zA-Z_][a-zA-Z0-9_]*)\.py$"
)

# 11. Universal lib root — src/<slug>/ or lib/<slug>/ at top level.
#     Captures <slug>. Covers axios-style ``lib/cancel/...``,
#     ``lib/adapters/...``.
_LIB_DIR_RE = re.compile(
    r"^(?:src|lib)/([a-zA-Z_][a-zA-Z0-9_-]*)/"
)

# Go top-level file — ``<slug>.go`` directly under repo root or
# directly under a Go module folder. Generic for chi/gin/ollama.
_GO_TOP_FILE_RE = re.compile(
    r"^([a-zA-Z][a-zA-Z0-9_]*)\.go$"
)

# Sprint 9c — Go file noise list. ``main.go``/``doc.go``/``version.go``
# etc. are scaffolding, not capabilities. Guard against false-positive
# protection where a feature literally named "Main" would otherwise
# trip ``_GO_TOP_FILE_RE`` on ``main.go``.
_GO_NOISE_SLUGS = frozenset({
    "main", "doc", "init", "version", "constants", "errors",
})

# Go sub-package — ``<dir>/<slug>/<file>.go`` shape; capture <slug>.
_GO_SUBPKG_RE = re.compile(
    r"^[a-zA-Z][a-zA-Z0-9_/-]*/([a-zA-Z][a-zA-Z0-9_]*)/[a-zA-Z][a-zA-Z0-9_-]*\.go$"
)

# Go test file — ``<X>_test.go``; capture <X>.
_GO_TEST_RE = re.compile(
    r"(?:^|/)([a-zA-Z][a-zA-Z0-9_]*)_test\.go$"
)


# 13. Sub-plugin inside a workspace package — three-level structure
#     ``packages/<workspace>/src/(?:plugins?|modules|features)/<slug>/``.
#     better-auth uses ``packages/better-auth/src/plugins/captcha/...``
#     where each sub-plugin is a real product capability hidden beneath
#     the workspace package boundary. Capture <slug>.
_NESTED_PLUGIN_RE = re.compile(
    r"^packages/[^/]+/src/(?:plugins?|modules|features|integrations)/([^/]+)/"
)

# 12. Test file naming — tests/test_<slug>.py, tests/<slug>.test.ts,
#     __tests__/<slug>.test.{ts,js}. Test name often equals public
#     feature name in well-organised libraries.
_TEST_FILE_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__)/(?:test_)?([a-zA-Z_][a-zA-Z0-9_-]*)(?:\.test)?\.(?:py|ts|js|tsx|jsx|mjs)$"
)

# ── Any-feature shields (file presence alone protects host) ─────────

# 9. API contract / OpenAPI — packages/api/v<N>/(contract|openapi|schema|implementation).ts
_API_CONTRACT_RE = re.compile(
    r"^packages/api/v\d+/(?:contract|openapi|schema|implementation)\.ts$"
)

# 10. Prisma schema file — schema.prisma anywhere
_PRISMA_RE = re.compile(r"(?:^|/)(?:prisma/)?schema(?:/[^/]+)?\.prisma$")

# 11. Auth strategy file — *strategy*.ts under auth-related folder
_AUTH_STRATEGY_RE = re.compile(
    r"(?:^|/)auth(?:[a-zA-Z-]*)/.*?(?:strategy|provider|callback|passkey)\.ts$"
)

# 12. Webhook handler — *webhook*.ts as a folder hub
_WEBHOOK_HUB_RE = re.compile(
    r"(?:^|/)webhooks?/(?:index|router|handler|create)\.ts$"
)


_NAMED_PATTERNS = (
    (_TRPC_FILE_RE, "trpc-router"),
    (_PACKAGE_RE, "workspace-package"),
    (_ROUTE_FILE_RE, "route-folder"),
    (_SERVER_ONLY_RE, "server-only-domain"),
    (_TRPC_NS_RE, "trpc-namespace"),
    (_PLUGIN_RE, "plugin-folder"),
    (_SERVICE_RE, "service-folder"),
    (_ROUTE_GROUP_RE, "route-group"),
    (_PY_SUBPKG_RE, "python-subpackage"),
    (_PY_MODULE_RE, "python-module"),
    (_LIB_DIR_RE, "lib-directory"),
    (_NESTED_PLUGIN_RE, "nested-plugin"),
    (_TEST_FILE_RE, "test-file"),
    (_GO_TOP_FILE_RE, "go-module"),
    (_GO_SUBPKG_RE, "go-subpackage"),
    (_GO_TEST_RE, "go-test-file"),
)

_SHIELD_PATTERNS = (
    (_API_CONTRACT_RE, "api-contract"),
    (_PRISMA_RE, "schema-file"),
    (_AUTH_STRATEGY_RE, "auth-strategy"),
    (_WEBHOOK_HUB_RE, "webhook-hub"),
)


def _slug_tail(slug: str) -> str:
    if "/" in slug:
        return slug.rsplit("/", 1)[1]
    return slug


def _norm(s: str) -> str:
    return s.lower().replace("_", "-").strip("-")


def _slug_matches(slug: str, candidate: str) -> bool:
    if not slug or not candidate:
        return False
    a = _norm(slug)
    b = _norm(candidate)
    if a == b:
        return True
    if _norm(_slug_tail(slug)) == b:
        return True
    # Allow plural ↔ singular variance (templates ↔ template)
    if a.rstrip("s") == b.rstrip("s"):
        return True
    return False


def _is_structural_path(slug: str, path: str) -> str | None:
    """Return the protection reason when ``path`` structurally anchors
    a feature with name ``slug``. Else None.
    """
    # Named patterns: capture group must match the feature slug.
    for pat, reason in _NAMED_PATTERNS:
        m = pat.search(path) if pat.pattern.startswith("(?:^|/)") else pat.match(path)
        if not m:
            continue
        captured = m.group(1)
        # Sprint 9c — Go scaffolding files ``main.go``/``doc.go`` are
        # not capabilities; never protect features named after them.
        if reason == "go-module" and captured.lower() in _GO_NOISE_SLUGS:
            continue
        if _slug_matches(slug, captured):
            return reason

    # Shield patterns: file presence alone protects, regardless of name.
    for pat, reason in _SHIELD_PATTERNS:
        if pat.search(path):
            return reason

    return None


def mark_protected(feature_map):
    """Sprint 10a — pure-function. Returns ``(new_feature_map,
    reasons_dict)``. Input ``feature_map`` is NEVER mutated.

    The returned FeatureMap is a deep copy with ``protected`` and
    ``protection_reason`` set on the features that matched a
    structural anchor. ``reasons_dict`` maps newly protected
    feature names to the reason label.

    For backward-compat callers that expect ONLY the dict return,
    use ``mark_protected(fm)[1]``.
    """
    new_fm = feature_map.model_copy(deep=True)
    reasons: dict[str, str] = {}
    for feat in new_fm.features:
        if feat.protected:
            continue
        for p in feat.paths:
            reason = _is_structural_path(feat.name, p)
            if reason:
                feat.protected = True
                feat.protection_reason = reason
                reasons[feat.name] = reason
                break
    return new_fm, reasons


__all__ = [
    "mark_protected",
]
