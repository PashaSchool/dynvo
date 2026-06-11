"""Tests for ``faultline.pipeline_v2.extractors.express.ExpressRouteExtractor``.

Code-based Express route extractor (``route-express``). Activates when
the auditor / Stage 0 / a package.json RUNTIME dep signals express —
but never on NestJS repos (Nest wraps Express; its decorators own the
routes). Emits one anchor per first-segment URL slug pulled from
``app.get("/users", ...)`` style calls (including aliased
``express.Router()`` variables), ``app.route("/x")`` chains, and
``app.use("/prefix", router)`` mount points.

We build synthetic repos under ``tmp_path``, write the source files
the extractor will grep, and assert the emitted slugs match the
expected URL → feature mapping.
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2 import stage_0_intake
from faultline.pipeline_v2.extractors.express import ExpressRouteExtractor


# ─────────────── Helpers ────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    _write(path, json.dumps(data, indent=2))


def _build_ctx(repo: Path) -> object:
    return stage_0_intake(repo, skip_git=True)


def _express_pkg(extra_deps: dict | None = None) -> dict:
    deps = {"express": "^4.21.0"}
    deps.update(extra_deps or {})
    return {"name": "api", "dependencies": deps}


# ─────────────── Activation ────────────────


def test_express_extractor_skips_when_no_signal(tmp_path: Path) -> None:
    """Repo with no express dep / audited stack → extractor self-skips."""
    _write_json(tmp_path / "package.json", {
        "name": "plain-react",
        "dependencies": {"react": "^19.0.0"},
    })
    _write(tmp_path / "src" / "App.tsx", "export const App = () => null;")

    ctx = _build_ctx(tmp_path)
    assert ExpressRouteExtractor().extract(ctx) == []


def test_express_extractor_skips_nestjs(tmp_path: Path) -> None:
    """NestJS repo (even with express in deps via platform-express) →
    inactive. Nest's decorators own the routes; extracting from the
    underlying Express engine would double-count."""
    _write_json(tmp_path / "package.json", {
        "name": "nest-api",
        "dependencies": {
            "@nestjs/core": "^10.0.0",
            "@nestjs/platform-express": "^10.0.0",
            "express": "^4.21.0",
        },
    })
    _write(
        tmp_path / "src" / "main.ts",
        """
        const app = await NestFactory.create(AppModule);
        app.get("/health", handler);
        """,
    )

    ctx = _build_ctx(tmp_path)
    assert ExpressRouteExtractor().extract(ctx) == []


def test_express_extractor_skips_dev_dependency_only(tmp_path: Path) -> None:
    """express in devDependencies = a library's test server, NOT an
    Express app → inactive (narrower gate than fastify's, on purpose)."""
    _write_json(tmp_path / "package.json", {
        "name": "some-lib",
        "devDependencies": {"express": "^4.21.0"},
    })
    _write(
        tmp_path / "src" / "index.ts",
        'app.get("/users", handler);',
    )

    ctx = _build_ctx(tmp_path)
    assert ExpressRouteExtractor().extract(ctx) == []


def test_express_extractor_activates_via_package_json(tmp_path: Path) -> None:
    """express in the root package.json runtime deps → extractor activates."""
    _write_json(tmp_path / "package.json", _express_pkg())
    _write(
        tmp_path / "src" / "index.js",
        """
        const express = require("express");
        const app = express();
        app.get("/users", (req, res) => res.json([]));
        app.post("/users/:id/secrets", (req, res) => res.json({}));
        """,
    )

    ctx = _build_ctx(tmp_path)
    out = ExpressRouteExtractor().extract(ctx)
    names = {c.name for c in out}
    assert "users" in names


# ─────────────── Method-call patterns ────────────────


def test_express_method_calls_extract_first_segment(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", _express_pkg())
    _write(
        tmp_path / "src" / "routes.ts",
        """
        app.get("/users", handler);
        app.post("/users/:id", handler);
        router.delete("/projects/:id", handler);
        router.patch("/secrets/:name", handler);
        server.put("/auth/tokens", handler);
        api.all("/webhooks/stripe", handler);
        """,
    )

    ctx = _build_ctx(tmp_path)
    out = ExpressRouteExtractor().extract(ctx)
    names = {c.name for c in out}
    assert {"users", "projects", "secrets", "auth", "webhooks"} <= names


def test_express_aliased_router_variables(tmp_path: Path) -> None:
    """``const v1 = express.Router()`` / ``const r = Router()`` aliases
    are detected per-file, so routes on them are extracted — the known
    fastify-extractor weakness this extractor fixes."""
    _write_json(tmp_path / "package.json", _express_pkg())
    _write(
        tmp_path / "src" / "v1.ts",
        """
        import express from "express";
        const v1 = express.Router();
        v1.get("/billing/invoices", handler);
        v1.post("/billing/charges", handler);
        """,
    )
    _write(
        tmp_path / "src" / "admin.ts",
        """
        import { Router } from "express";
        const adminRoutes: Router = Router({ mergeParams: true });
        adminRoutes.get("/organizations/:orgId", handler);
        """,
    )

    ctx = _build_ctx(tmp_path)
    out = ExpressRouteExtractor().extract(ctx)
    names = {c.name for c in out}
    assert "billing" in names
    assert "organizations" in names


def test_express_unrelated_receivers_do_not_match(tmp_path: Path) -> None:
    """Arbitrary ``foo.get(...)`` calls (maps, caches, next's useRouter)
    are NOT routes — only known receivers + aliased routers count."""
    _write_json(tmp_path / "package.json", _express_pkg())
    _write(
        tmp_path / "src" / "cache.ts",
        """
        const nav = useRouter();
        cache.get("/should-not-appear");
        store.post("/also-not-here", payload);
        app.get("/users", handler);
        """,
    )

    ctx = _build_ctx(tmp_path)
    out = ExpressRouteExtractor().extract(ctx)
    names = {c.name for c in out}
    assert "users" in names
    assert "should-not-appear" not in names
    assert "also-not-here" not in names


def test_express_settings_getters_are_not_routes(tmp_path: Path) -> None:
    """``app.get("port")`` / ``app.set(...)`` reads an Express setting —
    no leading slash → never a route anchor."""
    _write_json(tmp_path / "package.json", _express_pkg())
    _write(
        tmp_path / "src" / "index.ts",
        """
        app.get("port");
        app.get("view engine");
        app.get("/users", handler);
        """,
    )

    ctx = _build_ctx(tmp_path)
    out = ExpressRouteExtractor().extract(ctx)
    names = {c.name for c in out}
    assert "users" in names
    assert "port" not in names
    assert "view-engine" not in names


def test_express_multiline_route_call(tmp_path: Path) -> None:
    """URL literal on its own line after the open paren still matches."""
    _write_json(tmp_path / "package.json", _express_pkg())
    _write(
        tmp_path / "src" / "routes.ts",
        """
        app.post(
            "/documents/:id/sign",
            authenticate,
            async (req, res) => res.json({}),
        );
        """,
    )

    ctx = _build_ctx(tmp_path)
    out = ExpressRouteExtractor().extract(ctx)
    names = {c.name for c in out}
    assert "documents" in names


# ─────────────── route("/x") chains + use("/prefix") mounts ────────────────


def test_express_route_chain(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", _express_pkg())
    _write(
        tmp_path / "src" / "books.ts",
        """
        app.route("/books")
            .get(listBooks)
            .post(createBook);
        """,
    )

    ctx = _build_ctx(tmp_path)
    out = ExpressRouteExtractor().extract(ctx)
    names = {c.name for c in out}
    assert "books" in names


def test_express_mount_prefixes(tmp_path: Path) -> None:
    """``app.use("/prefix", router)`` mount points emit route-prefix
    anchors; bare middleware ``app.use(express.json())`` does not."""
    _write_json(tmp_path / "package.json", _express_pkg())
    _write(
        tmp_path / "src" / "app.ts",
        """
        import secretsRouter from "./secrets";
        app.use(express.json());
        app.use("/api/v1/secrets", secretsRouter);
        app.use("/auth", authRouter);
        """,
    )

    ctx = _build_ctx(tmp_path)
    out = ExpressRouteExtractor().extract(ctx)
    names = {c.name for c in out}
    assert "auth" in names
    # "/api/v1/secrets" → "api" is noise, "v1" is the first non-noise
    # segment (same documented behavior as the fastify extractor).
    assert "json" not in names


# ─────────────── Noise / dynamic-segment skips ────────────────


def test_express_skips_dynamic_and_noise_segments(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", _express_pkg())
    _write(
        tmp_path / "src" / "routes.ts",
        """
        app.get("/api/users", handler);
        app.get("/:id", handler);
        app.get("/", handler);
        app.get("/*", handler);
        """,
    )

    ctx = _build_ctx(tmp_path)
    out = ExpressRouteExtractor().extract(ctx)
    names = {c.name for c in out}
    # "api" is noise → first meaningful segment for "/api/users" is "users"
    assert "users" in names
    # "/:id", "/" and "/*" yield no slug → no anchor
    assert "id" not in names


# ─────────────── Bucketing + confidence ────────────────


def test_express_buckets_by_slug_and_scales_confidence(tmp_path: Path) -> None:
    """Multiple routes under one first segment → ONE anchor whose
    confidence grows with the match count (base 0.6 + 0.05/match)."""
    _write_json(tmp_path / "package.json", _express_pkg())
    _write(
        tmp_path / "src" / "users.ts",
        """
        router.get("/users", handler);
        router.post("/users", handler);
        router.get("/users/:id", handler);
        """,
    )
    _write(
        tmp_path / "src" / "users-admin.ts",
        'router.delete("/users/:id", handler);',
    )

    ctx = _build_ctx(tmp_path)
    out = ExpressRouteExtractor().extract(ctx)
    users = [c for c in out if c.name == "users"]
    assert len(users) == 1
    anchor = users[0]
    assert set(anchor.paths) == {"src/users.ts", "src/users-admin.ts"}
    assert anchor.source == "route-express"
    assert abs(anchor.confidence_self - 0.8) < 1e-9  # 0.6 + 0.05 * 4


# ─────────────── Skip test/dist files ────────────────


def test_express_skips_test_and_dist_files(tmp_path: Path) -> None:
    _write_json(tmp_path / "package.json", _express_pkg())
    # Real source
    _write(
        tmp_path / "src" / "routes.ts",
        'app.get("/users", handler);',
    )
    # Test file — must be skipped
    _write(
        tmp_path / "src" / "routes.test.ts",
        'app.get("/should-not-appear", handler);',
    )
    # dist/ — must be skipped (also won't be tracked, but defensive)
    _write(
        tmp_path / "dist" / "routes.js",
        'app.get("/also-not-here", handler);',
    )

    ctx = _build_ctx(tmp_path)
    out = ExpressRouteExtractor().extract(ctx)
    names = {c.name for c in out}
    assert "users" in names
    assert "should-not-appear" not in names
    assert "also-not-here" not in names
