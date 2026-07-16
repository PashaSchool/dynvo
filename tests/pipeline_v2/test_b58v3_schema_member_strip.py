"""B58-v3 Seg C — Stage 6.9c schema-monolith member strip.

Exhibit (keyed documenso board, key_schema 29, verified 2026-07-15):
``team.verify.email.$token`` — a 152-LOC leaf route whose PF claims
1,202 LOC because the stage-2 ``route,schema`` anchor join annexed
``packages/prisma/schema.prisma`` (895 LOC, role=anchor) and the 6.97
primary tiebreak dumped the prisma package's shared plumbing
(``index.ts`` 74 / ``helper.ts`` 31 / ``utils/remember.ts`` 13) onto the
same PF. Six devs claim the monolith (organisation / reset-password /
team / webhook / audit-log / prisma); only ``prisma`` (dev_tooling) is
its home.

Fixtures are synthetic (authority = engine signal, not offline sims) —
they hold the MECHANISM; the fix-cycle gate numbers are the lead's
re-scored keyed A/B, not these.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2 import schema_member_strip as SMS
from faultline.pipeline_v2.schema_member_strip import (
    grain_wave_enabled,
    monolith_package_of,
    strip_schema_monolith_members,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _mf(path: str, role: str = "anchor", primary: bool = True) -> MemberFile:
    return MemberFile(
        path=path, role=role, confidence=1.0, primary=primary,
        evidence="fixture",
    )


def _dev(name, paths, *, member_files=None, pfid=None):
    return Feature(
        name=name, paths=list(paths), flows=[],
        product_feature_id=pfid,
        member_files=(member_files if member_files is not None
                      else [_mf(p) for p in paths]),
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


# ── exhibit: documenso team.verify.email.$token (Seg C canon) ────────────

ROUTE_OWN = "apps/remix/app/routes/_unauthenticated+/team.verify.email.$token.tsx"
DIALOG_A = "apps/remix/app/components/dialogs/team-inherit-member-enable-dialog.tsx"
DIALOG_B = "apps/remix/app/components/dialogs/team-inherit-member-disable-dialog.tsx"
PRISMA_SCHEMA = "packages/prisma/schema.prisma"
PRISMA_INDEX = "packages/prisma/index.ts"
PRISMA_HELPER = "packages/prisma/helper.ts"
PRISMA_REMEMBER = "packages/prisma/utils/remember.ts"
PRISMA_CLIENT = "packages/prisma/client.ts"
PRISMA_MIGRATION = (
    "packages/prisma/migrations/20230404095503_initial_migration/migration.sql"
)


def _documenso_scene():
    """The exhibit scene: the route dev annexed the prisma package; the
    prisma package dev (home, 100% inside) coexists."""
    team_verify = _dev(
        "team-verify-email-token",
        [ROUTE_OWN, DIALOG_A, DIALOG_B,
         PRISMA_SCHEMA, PRISMA_INDEX, PRISMA_HELPER, PRISMA_REMEMBER],
    )
    prisma_home = _dev(
        "prisma",
        [PRISMA_SCHEMA, PRISMA_INDEX, PRISMA_HELPER, PRISMA_REMEMBER,
         PRISMA_CLIENT, PRISMA_MIGRATION],
    )
    return team_verify, prisma_home


class TestExhibitDocumensoTeamVerify:
    """The named exhibit: foreign route dev sheds the prisma package."""

    def test_foreign_route_dev_sheds_all_prisma_package_claims(self):
        team_verify, prisma_home = _documenso_scene()
        features = [team_verify, prisma_home]
        tele = strip_schema_monolith_members(features)

        assert tele["monoliths"] == [PRISMA_SCHEMA]
        assert tele["packages"] == ["packages/prisma"]
        # 4 paths + 4 member_files entries leave the route dev.
        assert tele["features_stripped"] == 1
        assert tele["paths_removed"] == 8
        assert set(team_verify.paths) == {ROUTE_OWN, DIALOG_A, DIALOG_B}
        assert {m.path for m in team_verify.member_files} == {
            ROUTE_OWN, DIALOG_A, DIALOG_B,
        }
        # The route dev survives (152-LOC route code remains).
        assert team_verify in features

    def test_home_prisma_dev_keeps_every_claim(self):
        team_verify, prisma_home = _documenso_scene()
        before_paths = list(prisma_home.paths)
        before_members = [m.path for m in prisma_home.member_files]
        tele = strip_schema_monolith_members([team_verify, prisma_home])

        assert tele["homes"] == {"packages/prisma": ["prisma"]}
        assert prisma_home.paths == before_paths
        assert [m.path for m in prisma_home.member_files] == before_members

    def test_six_claimant_class_all_foreigners_stripped(self):
        """The board showed SIX devs claiming schema.prisma — every
        foreign one sheds it, the home keeps it."""
        foreigners = [
            _dev(name, [f"apps/remix/app/routes/{name}.tsx", PRISMA_SCHEMA])
            for name in ("organisation", "reset-password", "team",
                         "webhook", "audit-log")
        ]
        _, prisma_home = _documenso_scene()
        features = foreigners + [prisma_home]
        tele = strip_schema_monolith_members(features)

        assert tele["features_stripped"] == 5
        for f in foreigners:
            assert PRISMA_SCHEMA not in f.paths
        assert PRISMA_SCHEMA in prisma_home.paths


# ── SACRED anti-cases ────────────────────────────────────────────────────


class TestAntiCases:
    def test_drizzle_per_domain_schema_untouched(self):
        """S4 doctrine: a per-domain schema file (Drizzle
        ``<domain>/schema.ts``) is NOT a monolith — the billing feature
        keeps its own schema. Named anti-case: billing/schema.ts."""
        billing = _dev(
            "billing",
            ["packages/db/src/billing/schema.ts",
             "apps/web/app/billing/page.tsx"],
        )
        tele = strip_schema_monolith_members([billing])
        assert tele["packages"] == []
        assert "packages/db/src/billing/schema.ts" in billing.paths

    def test_django_per_app_models_untouched(self):
        """Per-app ``models.py`` is a per-domain schema, never a
        monolith."""
        invoices = _dev(
            "invoices",
            ["apps/invoices/models.py", "apps/invoices/views.py"],
        )
        tele = strip_schema_monolith_members([invoices])
        assert tele["packages"] == []
        assert invoices.paths == ["apps/invoices/models.py",
                                  "apps/invoices/views.py"]

    def test_root_level_monolith_degrades_to_noop(self):
        """A monolith at the repo root has no schema package — the whole
        repo is never 'the package' (safe degradation)."""
        app = _dev("app", ["schema.prisma", "src/index.ts"])
        tele = strip_schema_monolith_members([app])
        assert tele["packages"] == []
        assert "schema.prisma" in app.paths

    def test_preexisting_pathless_feature_never_dropped(self):
        """A row that was already path-less on entry (lane rows /
        markers) is not this pass's business — never compacted."""
        team_verify, prisma_home = _documenso_scene()
        laned = _dev("laned-row", [])
        features = [team_verify, prisma_home, laned]
        tele = strip_schema_monolith_members(features)
        assert laned in features
        assert tele["features_dropped"] == 0

    def test_shared_role_membership_does_not_confer_home(self):
        """A claimant whose only inside-claims are shared-role fan-in
        entries is still foreign (the exhibit's index.ts/helper.ts shape:
        role=shared, primary ownership dumped by the 6.97 tiebreak)."""
        route = _dev(
            "reset-password",
            ["apps/remix/app/routes/reset-password.tsx"],
            member_files=[
                _mf("apps/remix/app/routes/reset-password.tsx"),
                _mf(PRISMA_SCHEMA, role="anchor"),
                _mf(PRISMA_INDEX, role="shared", primary=False),
            ],
        )
        _, prisma_home = _documenso_scene()
        strip_schema_monolith_members([route, prisma_home])
        assert {m.path for m in route.member_files} == {
            "apps/remix/app/routes/reset-password.tsx",
        }


# ── Rails suffix conventions ─────────────────────────────────────────────


class TestRailsMonolith:
    def test_db_schema_rb_suffix_match_strips_foreign_model_feature(self):
        user_mgmt = _dev(
            "user-management",
            ["app/models/user.rb", "app/controllers/users_controller.rb",
             "db/schema.rb"],
        )
        tele = strip_schema_monolith_members([user_mgmt])
        assert tele["monoliths"] == ["db/schema.rb"]
        assert "db/schema.rb" not in user_mgmt.paths
        # No home feature exists for db/ — honest telemetry.
        assert tele["no_home"] == ["db"]

    def test_structure_sql_is_a_monolith(self):
        assert monolith_package_of("db/structure.sql") == "db"

    def test_random_sql_file_is_not(self):
        assert monolith_package_of("db/queries/report.sql") is None


# ── dropped-when-emptied (test-strip precedent, strip-scoped) ────────────


class TestEmptiedFeatureDrop:
    def test_schema_phantom_across_two_packages_strips_empty_and_drops(self):
        """A dev claiming ONLY schema-package files (across two
        packages, so it is home to neither under the strict all-inside
        bar) is the late schema-phantom shape — every claim strips and
        the emptied dev drops (test-strip precedent). The stage-2
        phantom suppressor catches the single-package variant; this is
        its cross-package twin."""
        other_schema = "services/db/schema.prisma"
        ghost = _dev("ghost", [PRISMA_SCHEMA, other_schema])
        _, prisma_home = _documenso_scene()
        db_home = _dev("db-home", [other_schema, "services/db/client.ts",
                                   "services/db/seed.ts"])
        features = [ghost, prisma_home, db_home]
        tele = strip_schema_monolith_members(features)
        assert tele["features_dropped"] == 1
        assert ghost not in features
        assert prisma_home in features and db_home in features

    def test_minimal_leak_shape_is_foreign_not_home(self):
        """THE fragile shape a fractional home bar would spare: a
        two-file dev (route + monolith, 50% inside) MUST strip — the
        strict all-inside bar makes it foreign by construction."""
        leaf = _dev("verify-leaf",
                    ["apps/web/routes/verify.tsx", PRISMA_SCHEMA])
        _, prisma_home = _documenso_scene()
        features = [leaf, prisma_home]
        tele = strip_schema_monolith_members(features)
        assert PRISMA_SCHEMA not in leaf.paths
        assert leaf in features  # route file survives — not dropped
        assert tele["features_stripped"] == 1
        assert tele["features_dropped"] == 0


# ── collateral fixation: typebot ESVE (wave iter-2, HONEST arithmetic) ───


class TestCollateralTypebotEsve:
    """Wave census collateral (GAP-a863d0abc0 'blocks-logic', ON-only):
    dev ``evaluate-set-variable-expression`` (96 paths, anchor
    ws:packages/blocks/logic) carried TWO prisma monoliths
    (packages/prisma/{mysql,postgresql}/schema.prisma) that denied it
    anchor lineage (BASE: shared_reason=no_anchor_lineage, pfid=None —
    parked in the platform lane, 3,709 LOC). The Seg C strip removed
    the dev-infra pollution → the dev homed to its structurally
    correct PF (blocks-logic) → the PF crossed loc-worthiness → a NEW
    gap row = MASKED DEBT MATERIALIZED, not a defect (no journey moved:
    the PF is flowless in BOTH worlds; the PF row exists in BOTH).
    This unit pins the mechanism: multi-monolith claims strip, the
    dev's real files survive."""

    def test_two_monolith_claims_strip_dev_survives(self):
        esve = _dev(
            "evaluate-set-variable-expression",
            ["packages/blocks/logic/src/evaluate.ts",
             "packages/blocks/logic/src/expression.ts",
             "apps/builder/src/features/analytics/api/handleGetStats.ts",
             "packages/prisma/mysql/schema.prisma",
             "packages/prisma/postgresql/schema.prisma"],
        )
        mysql_home = _dev(
            "prisma-mysql",
            ["packages/prisma/mysql/schema.prisma",
             "packages/prisma/mysql/client.ts"],
        )
        pg_home = _dev(
            "prisma-postgresql",
            ["packages/prisma/postgresql/schema.prisma",
             "packages/prisma/postgresql/client.ts"],
        )
        features = [esve, mysql_home, pg_home]
        tele = strip_schema_monolith_members(features)

        assert sorted(tele["packages"]) == [
            "packages/prisma/mysql", "packages/prisma/postgresql"]
        # BOTH monolith claims leave the dev; its real files stay.
        assert set(esve.paths) == {
            "packages/blocks/logic/src/evaluate.ts",
            "packages/blocks/logic/src/expression.ts",
            "apps/builder/src/features/analytics/api/handleGetStats.ts",
        }
        assert esve in features  # survives — never dropped
        # each monolith keeps its 100%-inside home.
        assert tele["homes"] == {
            "packages/prisma/mysql": ["prisma-mysql"],
            "packages/prisma/postgresql": ["prisma-postgresql"],
        }


# ── kill-switch law ──────────────────────────────────────────────────────


class TestKillSwitch:
    def test_default_on(self, monkeypatch):
        # SEMANTIC (horizon-1 flip): unset now defaults ON.
        monkeypatch.delenv(SMS.GRAIN_WAVE_ENV, raising=False)
        assert grain_wave_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "off", ""])
    def test_explicit_off_values(self, monkeypatch, val):
        monkeypatch.setenv(SMS.GRAIN_WAVE_ENV, val)
        assert grain_wave_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE"])
    def test_on_values(self, monkeypatch, val):
        monkeypatch.setenv(SMS.GRAIN_WAVE_ENV, val)
        assert grain_wave_enabled() is True

    def test_inverted_killswitch(self, monkeypatch):
        """Inverted kill-switch: unset ≡ explicit ``1``; ``0`` == old OFF."""
        monkeypatch.delenv(SMS.GRAIN_WAVE_ENV, raising=False)
        unset = grain_wave_enabled()
        monkeypatch.setenv(SMS.GRAIN_WAVE_ENV, "1")
        assert grain_wave_enabled() is unset is True
        monkeypatch.setenv(SMS.GRAIN_WAVE_ENV, "0")
        assert grain_wave_enabled() is False

    def test_off_world_scene_is_byte_identical(self, monkeypatch):
        """The finalize wiring only calls the pass when the flag is ON;
        the OFF contract at module level: enabled() False and the scene
        untouched when the caller honours it (the wiring test — the
        integration path is exercised by the suite's finalize tests)."""
        # MECHANICAL (horizon-1 flip): explicit "0" (unset now defaults ON).
        monkeypatch.setenv(SMS.GRAIN_WAVE_ENV, "0")
        team_verify, prisma_home = _documenso_scene()
        before = (
            team_verify.model_dump_json(), prisma_home.model_dump_json(),
        )
        if grain_wave_enabled():  # pragma: no cover — law violation guard
            strip_schema_monolith_members([team_verify, prisma_home])
        after = (
            team_verify.model_dump_json(), prisma_home.model_dump_json(),
        )
        assert before == after


# ── conventions / predicate table ────────────────────────────────────────


class TestMonolithPredicate:
    @pytest.mark.parametrize("path,pkg", [
        ("packages/prisma/schema.prisma", "packages/prisma"),
        ("prisma/schema.prisma", "prisma"),
        ("apps/api/db/schema.rb", "apps/api/db"),
        ("db/structure.sql", "db"),
    ])
    def test_monolith_and_package(self, path, pkg):
        assert monolith_package_of(path) == pkg

    @pytest.mark.parametrize("path", [
        "packages/db/src/billing/schema.ts",   # Drizzle per-domain
        "apps/invoices/models.py",             # Django per-app
        "packages/prisma/index.ts",            # plumbing, not the monolith
        "docs/schema.prisma.md",
        "",
    ])
    def test_non_monoliths(self, path):
        assert monolith_package_of(path) is None
