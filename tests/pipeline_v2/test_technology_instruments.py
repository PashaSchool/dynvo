"""W4.2 Fix 1 — technology-instrument detector tests.

Fixtures are DISTILLED from the real 2026-07-07 four-repo probe
(typebot Prisma / midday jobs-cache-email / the four packages/ui kits /
midday packages/cli anti-case) — mini-monorepos on tmp_path, neutral
names where the shape (not the name) carries the signal.
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2.technology_instruments import (
    CONFIG_LANE_ENV,
    TECH_INSTRUMENTS_ENV,
    config_lane_enabled,
    detect_technology_instruments,
    tech_instruments_enabled,
)


def _write(repo: Path, rel: str, text: str = "") -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel


def _manifest(repo: Path, rel_dir: str, name: str, *,
              deps: dict | None = None, dev_deps: dict | None = None,
              private: bool | None = None, bin_entry: str | None = None,
              ) -> str:
    doc: dict = {"name": name}
    if deps:
        doc["dependencies"] = deps
    if dev_deps:
        doc["devDependencies"] = dev_deps
    if private is not None:
        doc["private"] = private
    if bin_entry:
        doc["bin"] = {name.split("/")[-1]: bin_entry}
    rel = f"{rel_dir}/package.json" if rel_dir else "package.json"
    return _write(repo, rel, json.dumps(doc))


def _detect(repo: Path, tracked: list[str], routes=None, fdirs=(), hubs=()):
    return detect_technology_instruments(
        repo, tracked, routes or [], fdir_units=fdirs, hub_dirs=hubs)


# ── the ORM-package class (S1b — typebot/documenso Prisma shape) ─────────


def _prisma_repo(repo: Path) -> list[str]:
    tracked = [
        _manifest(repo, "", "root", private=True),
        _manifest(repo, "packages/ormkit", "@acme/ormkit",
                  deps={"@prisma/client": "5.0.0"},
                  dev_deps={"prisma": "5.0.0"}),
        _write(repo, "packages/ormkit/schema.prisma", "model User {}"),
        _write(repo, "packages/ormkit/migrations/0001_init/migration.sql",
               "CREATE TABLE users;"),
        _write(repo, "packages/ormkit/index.ts",
               'export { PrismaClient } from "@prisma/client";\n'),
        _manifest(repo, "apps/web", "@acme/web",
                  deps={"react": "18.0.0"}, private=True),
        _write(repo, "apps/web/src/app/page.tsx",
               'import { db } from "@acme/ormkit";\n'),
    ]
    return tracked


def test_schema_tool_package_is_an_instrument(tmp_path: Path) -> None:
    tracked = _prisma_repo(tmp_path)
    tele = _detect(tmp_path, tracked)
    assert "packages/ormkit" in tele["instruments"]
    assert tele["instruments"]["packages/ormkit"].startswith("S1b")
    assert "packages/ormkit" in tele["dirs"]


def test_route_surface_vetoes_the_verdict(tmp_path: Path) -> None:
    """Any ws-pkg WITH routes/pages survives — the hard S3 veto."""
    tracked = _prisma_repo(tmp_path)
    routes = [{"file": "packages/ormkit/index.ts", "pattern": "/db",
               "method": "PAGE"}]
    tele = _detect(tmp_path, tracked, routes=routes)
    assert "packages/ormkit" not in tele["instruments"]
    assert tele["vetoed"].get("packages/ormkit") == "route_surface"


def test_kill_switch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(TECH_INSTRUMENTS_ENV, "0")
    assert tech_instruments_enabled() is False
    tele = _detect(tmp_path, _prisma_repo(tmp_path))
    assert tele["enabled"] is False and not tele.get("dirs")


# ── the published-CLI anti-case (V2 — midday packages/cli doctrine) ──────


def test_published_cli_never_reclasses(tmp_path: Path) -> None:
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/cli", "@acme/cli",
                  deps={"commander": "11.0.0"}, bin_entry="dist/index.js"),
        _write(tmp_path, "packages/cli/src/index.ts",
               'import { program } from "commander";\n'),
        _write(tmp_path, "packages/cli/src/login.ts",
               'import { program } from "commander";\n'),
    ]
    tele = _detect(tmp_path, tracked)
    assert "packages/cli" not in tele["instruments"]
    assert tele["vetoed"].get("packages/cli") == "published_cli"


# ── the nested-family veto (V3 — typebot forge blocks / embeds) ──────────


def test_nested_family_member_never_reclasses(tmp_path: Path) -> None:
    """packages/forge/blocks/<vendor>: integration catalog = product,
    even when vendor-named after a declared dependency."""
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/forge/blocks/anthropic",
                  "@acme/anthropic-block",
                  deps={"@anthropic-ai/sdk": "0.20.0"}, private=True),
        _write(tmp_path, "packages/forge/blocks/anthropic/index.ts",
               'import Anthropic from "@anthropic-ai/sdk";\n'),
        _write(tmp_path, "packages/forge/blocks/anthropic/logo.tsx", "x"),
    ]
    tele = _detect(tmp_path, tracked)
    assert "packages/forge/blocks/anthropic" not in tele["instruments"]
    assert tele["vetoed"].get(
        "packages/forge/blocks/anthropic") == "nested_family"


# ── the hub-hosting veto (V4 — midday packages/banking providers) ────────


def test_hub_hosting_unit_never_reclasses(tmp_path: Path) -> None:
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/banking", "@acme/banking",
                  private=True),
        _write(tmp_path, "packages/banking/src/providers/alpha/api.ts"),
        _write(tmp_path, "packages/banking/src/providers/beta/api.ts"),
        _write(tmp_path, "packages/banking/src/providers/gamma/api.ts"),
    ]
    tele = _detect(
        tmp_path, tracked, hubs=["packages/banking/src/providers"])
    assert "packages/banking" not in tele["instruments"]
    assert tele["vetoed"].get("packages/banking") == "hosts_hub_family"


# ── the dominant-dep wrapper (S1d — midday email/logger shape) ───────────


def _wrapper_repo(tmp_path: Path) -> list[str]:
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/mailkit", "@acme/mailkit",
                  deps={"@post-mailer/components": "1.0.0"}, private=True),
        _manifest(tmp_path, "apps/web", "@acme/web", private=True),
        _manifest(tmp_path, "apps/api", "@acme/api", private=True),
    ]
    for i in range(4):
        tracked.append(_write(
            tmp_path, f"packages/mailkit/emails/tpl{i}.tsx",
            'import { Body } from "@post-mailer/components";\n'))
    for i in range(3):
        tracked.append(_write(
            tmp_path, f"apps/api/src/send{i}.ts",
            'import { tpl } from "@acme/mailkit";\n'))
    return tracked


def test_dominant_dep_wrapper_is_an_instrument(tmp_path: Path) -> None:
    tele = _detect(tmp_path, _wrapper_repo(tmp_path))
    assert tele["instruments"].get("packages/mailkit") == (
        "S1d-dep:postmailer")


def test_ambient_dep_never_dominates(tmp_path: Path) -> None:
    """A dep declared across >= max(3, N/3) manifests is the repo's
    lingua franca (typebot zod), never wrapper evidence — the domain
    package importing it everywhere stays product."""
    tracked = _wrapper_repo(tmp_path)
    # declare the same dep in 3 more manifests -> ambient (floor 3).
    tracked += [
        _manifest(tmp_path, "packages/a", "@acme/a",
                  deps={"@post-mailer/components": "1"}, private=True),
        _manifest(tmp_path, "packages/b", "@acme/b",
                  deps={"@post-mailer/components": "1"}, private=True),
        _manifest(tmp_path, "apps/web2", "@acme/web2",
                  deps={"@post-mailer/components": "1"}, private=True),
    ]
    tele = _detect(tmp_path, tracked)
    assert "packages/mailkit" not in tele["instruments"]


def test_domain_importing_package_stays_product(tmp_path: Path) -> None:
    """documenso packages/trpc shape: dominant dep + broad inbound, but
    the unit imports the DOMAIN heavily -> stays product."""
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/rpc", "@acme/rpc",
                  deps={"@rpc-kit/server": "1.0.0"}),
        _manifest(tmp_path, "packages/domain-a", "@acme/domain-a"),
        _manifest(tmp_path, "packages/domain-b", "@acme/domain-b"),
        _write(tmp_path, "packages/domain-a/src/index.ts", "export {};\n"),
        _write(tmp_path, "packages/domain-b/src/index.ts", "export {};\n"),
    ]
    for i in range(4):
        tracked.append(_write(
            tmp_path, f"packages/rpc/src/router{i}.ts",
            'import { t } from "@rpc-kit/server";\n'
            'import { a } from "@acme/domain-a";\n'
            'import { b } from "@acme/domain-b";\n'))
    for i in range(5):
        tracked.append(_write(
            tmp_path, f"apps/web/src/call{i}.ts",
            'import { api } from "@acme/rpc";\n'))
    tracked.append(_manifest(tmp_path, "apps/web", "@acme/web",
                             private=True))
    tele = _detect(tmp_path, tracked)
    assert "packages/rpc" not in tele["instruments"]


# ── config-only units (S1c — tsconfig / eslint-config shape) ─────────────


def test_config_only_package_is_an_instrument(tmp_path: Path) -> None:
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/tsconfig", "@acme/tsconfig",
                  private=True),
        _write(tmp_path, "packages/tsconfig/base.json", "{}"),
        _write(tmp_path, "packages/tsconfig/nextjs.json", "{}"),
    ]
    tele = _detect(tmp_path, tracked)
    assert tele["instruments"].get("packages/tsconfig") == "S1c-config-only"


# ── design-system workspace (S1f — the four packages/ui kits) ────────────


def test_ui_workspace_is_an_instrument(tmp_path: Path) -> None:
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/ui", "@acme/ui",
                  deps={"react": "18.0.0"}, private=True),
        # react is declared repo-wide (the real four-kit shape): AMBIENT,
        # so the dominant-dep prong abstains and the ws-ui key decides.
        _manifest(tmp_path, "apps/web", "@acme/web",
                  deps={"react": "18.0.0"}, private=True),
        _manifest(tmp_path, "apps/admin", "@acme/admin",
                  deps={"react": "18.0.0"}, private=True),
        _write(tmp_path, "packages/ui/src/button.tsx",
               'import * as React from "react";\n'),
        _write(tmp_path, "packages/ui/src/dialog.tsx",
               'import * as React from "react";\n'),
    ]
    for app in ("web", "admin"):
        for i in range(3):
            tracked.append(_write(
                tmp_path, f"apps/{app}/src/f{i}.tsx",
                'import { Button } from "@acme/ui";\n'))
    tele = _detect(tmp_path, tracked)
    assert tele["instruments"].get("packages/ui") == "S1f-design-system"


# ── S1a marker (midday jobs / trigger.dev shape, dotted dep name) ────────


def test_aligned_root_marker_is_an_instrument(tmp_path: Path) -> None:
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/jobs", "@acme/jobs",
                  deps={"runner.dev": "4.0.0"}, private=True),
        _write(tmp_path, "packages/jobs/runner.config.ts",
               'import { defineConfig } from "runner.dev";\n'),
        _write(tmp_path, "packages/jobs/src/tasks/sync.ts",
               'import { schedules } from "runner.dev";\n'
               'import { db } from "@acme/domain";\n'),
        _write(tmp_path, "packages/jobs/src/tasks/notify.ts",
               'import { schedules } from "runner.dev";\n'),
        _manifest(tmp_path, "packages/domain", "@acme/domain"),
        _write(tmp_path, "packages/domain/src/index.ts", "export {};\n"),
    ]
    tele = _detect(tmp_path, tracked)
    assert tele["instruments"].get("packages/jobs") == "S1a-marker:runner"


# ── satellite fdir (typebot builder telemetry shape) ─────────────────────


def test_fdir_satellite_of_an_instrument(tmp_path: Path) -> None:
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/telemetry", "@acme/telemetry",
                  deps={"eventhog-node": "1.0.0"}, private=True),
        _write(tmp_path, "packages/telemetry/src/track.ts",
               'import { EventHog } from "eventhog-node";\n'),
        _manifest(tmp_path, "apps/builder", "@acme/builder", private=True),
        _write(tmp_path,
               "apps/builder/src/features/telemetry/api/router.ts",
               'import { track } from "@acme/telemetry";\n'),
    ]
    for i in range(3):
        tracked.append(_write(
            tmp_path, f"apps/builder/src/features/f{i}.ts",
            'import { track } from "@acme/telemetry";\n'))
    tele = _detect(
        tmp_path, tracked,
        fdirs=["apps/builder/src/features/telemetry"])
    assert "packages/telemetry" in tele["instruments"]
    assert tele["satellites"].get(
        "apps/builder/src/features/telemetry"
    ) == "satellite:packages/telemetry"
    assert "apps/builder/src/features/telemetry" in tele["dirs"]


def test_unrelated_fdir_is_no_satellite(tmp_path: Path) -> None:
    """An fdir that neither shares an instrument's name nor imports one
    (the Soc0 `ticketing` shape) never rides the satellite rule."""
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/telemetry", "@acme/telemetry",
                  deps={"eventhog-node": "1.0.0"}, private=True),
        _write(tmp_path, "packages/telemetry/src/track.ts",
               'import { EventHog } from "eventhog-node";\n'),
        _write(tmp_path, "apps/web/src/features/ticketing/list.tsx",
               'import * as React from "react";\n'),
        _manifest(tmp_path, "apps/web", "@acme/web", private=True),
    ]
    for i in range(3):
        tracked.append(_write(
            tmp_path, f"apps/web/src/t{i}.ts",
            'import { track } from "@acme/telemetry";\n'))
    tele = _detect(tmp_path, tracked,
                   fdirs=["apps/web/src/features/ticketing"])
    assert not tele["satellites"]


# ── B1: config consumed through a config channel (prettier-config shape) ──
# The documenso exhibit: `packages/prettier-config` is a settings artifact
# (index.cjs = `module.exports = {...}`, tool declared as its dep) but the
# root `prettier.config.cjs` = `module.exports = require('@acme/prettier-
# config')` creates a REAL import edge, so the strict S1c `inf == 0` guard
# missed it and it minted as a PF — while `eslint-config` (referenced via an
# `extends` STRING, no edge) laned correctly. Kill-switch FAULTLINE_CONFIG_LANE.


def _prettier_config_repo(repo: Path) -> list[str]:
    """The prettier-config shape: a config-only package consumed ONLY by a
    root ``*.config.cjs`` re-export shim (a config channel)."""
    return [
        _manifest(repo, "", "root", private=True,
                  dev_deps={"prettier": "^3.6.2"}),
        _manifest(repo, "packages/prettier-config", "@acme/prettier-config",
                  deps={"prettier": "^3.6.2"}),
        _write(repo, "packages/prettier-config/index.cjs",
               "module.exports = {\n  printWidth: 100,\n"
               "  singleQuote: true,\n  semi: true,\n};\n"),
        # the ONLY consumer is a config-channel file (basename *.config.cjs)
        _write(repo, "prettier.config.cjs",
               "module.exports = require('@acme/prettier-config');\n"),
    ]


def test_config_consumed_via_channel_is_an_instrument(tmp_path: Path) -> None:
    """FLAG ON: the config-channel re-export does not make it product."""
    tele = _detect(tmp_path, _prettier_config_repo(tmp_path))
    assert (tele["instruments"].get("packages/prettier-config")
            == "S1c-config-consumed")
    assert "packages/prettier-config" in tele["dirs"]


def test_config_consumed_kill_switch_byte_identity(
        tmp_path: Path, monkeypatch) -> None:
    """FLAG OFF (=0): strict ``inf == 0`` restored — the package with a
    real importer mints as a PF exactly as pre-B1 main."""
    monkeypatch.setenv(CONFIG_LANE_ENV, "0")
    assert config_lane_enabled() is False
    tele = _detect(tmp_path, _prettier_config_repo(tmp_path))
    assert "packages/prettier-config" not in tele["instruments"]
    assert "packages/prettier-config" not in tele["dirs"]


def test_config_lane_flag_default_on(monkeypatch) -> None:
    monkeypatch.delenv(CONFIG_LANE_ENV, raising=False)
    assert config_lane_enabled() is True
    monkeypatch.setenv(CONFIG_LANE_ENV, "false")
    assert config_lane_enabled() is False


# ── Anti-case 1: the `extends`-STRING class stays laned, unchanged ───────
def test_eslint_extends_string_stays_config_only(tmp_path: Path) -> None:
    """`.eslintrc.cjs` references the package via an `extends` STRING (no
    import edge) -> inf == 0 -> classic S1c-config-only. B1 must not change
    this: `S1c-config-only`, never `S1c-config-consumed`."""
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/eslint-config", "@acme/eslint-config",
                  deps={"eslint": "^8.57.0"}),
        _write(tmp_path, "packages/eslint-config/index.cjs",
               "module.exports = { extends: ['next'] };\n"),
        # `extends` is a data STRING, NOT a require -> no import edge.
        _write(tmp_path, ".eslintrc.cjs",
               "module.exports = { extends: ['@acme/eslint-config'] };\n"),
    ]
    tele = _detect(tmp_path, tracked)
    assert (tele["instruments"].get("packages/eslint-config")
            == "S1c-config-only")


# ── Anti-case 2: a config-SHAPED unit consumed by PRODUCT code -> product ─
def test_config_shaped_but_consumed_by_product_code_stays_product(
        tmp_path: Path) -> None:
    """Same tiny config-only shape, but the sole importer is a product
    source file (`page.tsx`), NOT a config channel. cfg_consumed is False
    (not all importers are config channels) -> never demoted."""
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/settings-kit", "@acme/settings-kit",
                  deps={"zod": "3.0.0"}),
        _write(tmp_path, "packages/settings-kit/index.cjs",
               "module.exports = { flag: true };\n"),
        _manifest(tmp_path, "apps/web", "@acme/web", private=True),
        # imported by PRODUCT code (not a *.config.* / .*rc* file).
        _write(tmp_path, "apps/web/src/page.tsx",
               "import cfg from '@acme/settings-kit';\nexport default cfg;\n"),
    ]
    tele = _detect(tmp_path, tracked)
    assert "packages/settings-kit" not in tele["instruments"]


# ── Anti-case 2b: MIXED importers (one config channel + one product) ─────
def test_config_consumed_requires_all_importers_config_channel(
        tmp_path: Path) -> None:
    """If even ONE importer is product code, cfg_consumed is False."""
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/fmt-config", "@acme/fmt-config",
                  deps={"prettier": "3.0.0"}),
        _write(tmp_path, "packages/fmt-config/index.cjs",
               "module.exports = { printWidth: 80 };\n"),
        _write(tmp_path, "prettier.config.cjs",
               "module.exports = require('@acme/fmt-config');\n"),
        _manifest(tmp_path, "apps/web", "@acme/web", private=True),
        _write(tmp_path, "apps/web/src/page.tsx",
               "import fmt from '@acme/fmt-config';\nexport default fmt;\n"),
    ]
    tele = _detect(tmp_path, tracked)
    assert "packages/fmt-config" not in tele["instruments"]


# ── Anti-case 3: a config-NAMED package that ships product code -> product ─
def test_config_named_but_ships_product_code_stays_product(
        tmp_path: Path) -> None:
    """A `packages/prettier-config`-NAMED package that actually ships
    product source (4 code files, config-minority) is NOT demoted even with
    a config-channel importer. The name is never the trigger — the shape is
    (src > 2, cfg_share < 0.5)."""
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/prettier-config", "@acme/prettier-config",
                  private=True),
        _write(tmp_path, "prettier.config.cjs",
               "module.exports = require('@acme/prettier-config');\n"),
    ]
    for i in range(4):
        tracked.append(_write(
            tmp_path, f"packages/prettier-config/src/rule{i}.ts",
            f"export function computeRule{i}(x: number) "
            "{\n  return x * 2 + 1;\n}\n"))
    tele = _detect(tmp_path, tracked)
    assert "packages/prettier-config" not in tele["instruments"]
