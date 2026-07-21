"""B74 Seg A — SPA route-table extraction unit pack (Seg D of spa-router).

Probe canon 2026-07-20 (ledger §ПРОБА B74 SEG A, SHIP/HIGH) with the
FORM CORRECTION: candidacy = shape (>=3 route-like string values,
ratio >= 0.8; a leading '/' is a PRIOR, never a gate — the slashless
SettingsPath table MUST be taken); the EMISSION gate is load-bearing:
>=1 member consumed in path-position inside a router-marker file.

Named units (delegation gates):
  * twenty AppPath — 20 patterns, named owners /welcome -> SignInUp and
    /objects/:objectNamePlural -> RecordIndexPage; tasks/opportunities
    nav-aliases emit as pattern anchors WITHOUT an owner;
  * twenty SettingsPath — the consumption-primary slashless case;
  * novu ROUTES — const-object grammar, '@/pages' barrel resolution
    package-scoped (the playground/nextjs foreign-hit trap);
  * NAMED anti-cases (consumption gate): DOCUMENTATION_PATHS,
    BACKGROUND asset packs, AppBasePath, DOCS_REDIRECTS + skateshop
    ``redirects``, API path config consumed outside a router file;
  * flag default OFF; unset == explicit "0" byte-identical; no new
    extractor_hits source key when armed; determinism x2;
  * routes_index Pass C stamps kind="spa-page" on table rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from faultline.pipeline_v2.extractors.spa_router import (
    SPA_PAGE_SOURCE,
    SPA_ROUTE_TABLE_ENV,
    SPA_ROUTER_ENTRIES_ENV,
    SpaRouterExtractor,
    route_table_candidates,
    spa_route_table_enabled,
)
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_1_extractors import stage_1_extractors


def _ctx(repo: Path, files: list[str], **kw) -> ScanContext:
    return ScanContext(
        repo_path=repo,
        stack=kw.get("stack", "node"),
        monorepo=kw.get("monorepo", False),
        workspaces=kw.get("workspaces"),
        tracked_files=files,
        commits=[],
        secondary_stacks=kw.get("secondary_stacks", ()),
        audited_stack=kw.get("audited_stack"),
    )


def _extract(repo: Path, files: list[str], **kw):
    """Anchors through the REAL Stage-1 seam (extract() no longer runs
    the table arm — the repo-wide post-pass folds it into spa-page)."""
    out = stage_1_extractors(
        _ctx(repo, files, **kw), extractors=[SpaRouterExtractor()],
    )
    return out.get(SPA_PAGE_SOURCE) or []


@pytest.fixture
def table_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SPA_ROUTE_TABLE_ENV, "1")


def _write(tmp_path: Path, rel: str, body: str) -> str:
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    return rel


def _routes_of(anchors) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    for a in anchors:
        out.update(a.routes)
    return out


def _patterns_of(anchors) -> set[str]:
    return {pat for pat, _method, _file in _routes_of(anchors)}


def _pattern_files(anchors) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for pat, _method, file in _routes_of(anchors):
        out.setdefault(pat, set()).add(file)
    return out


# ── twenty-shaped fixture (AppPath + SettingsPath) ───────────────────────────

#: The full 20-entry AppPath table from the probe canon — 16 owned via
#: the root-router fixture below + 4 nav-alias/ownerless values.
_APP_PATH_TS = """
export enum AppPath {
  Index = '/',
  TasksPage = '/objects/tasks',
  OpportunitiesPage = '/objects/opportunities',
  Settings = 'settings',
  SignInUp = '/welcome',
  Invite = '/invite/:workspaceInviteHash',
  VerifyEmail = '/verify-email',
  ResetPassword = '/reset-password/:passwordResetToken',
  RecordIndexPage = '/objects/:objectNamePlural',
  RecordShowPage = '/object/:objectNameSingular/:objectRecordId',
  PageLayoutPage = '/page/:pageLayoutId',
  PlanRequired = '/plan-required',
  PlanRequiredSuccess = '/plan-required/payment-success',
  BookCall = '/book-call',
  Verify = '/verify',
  WorkspaceActivation = '/workspace-activation',
  CreateProfile = '/create/profile',
  SyncEmails = '/sync/emails',
  InstallApps = '/install-apps',
  InviteTeam = '/invite-team',
}
"""

#: (AppPath member, element component, page module tail) — the 16 owners.
_APP_ROUTES = [
    ("SignInUp", "SignInUp", "auth/SignInUp"),
    ("Invite", "SignInUp", "auth/SignInUp"),
    ("VerifyEmail", "VerifyEmail", "auth/VerifyEmail"),
    ("ResetPassword", "PasswordReset", "auth/PasswordReset"),
    ("RecordIndexPage", "RecordIndexPage", "object-record/RecordIndexPage"),
    ("RecordShowPage", "RecordShowPage", "object-record/RecordShowPage"),
    ("PageLayoutPage", "StandalonePageLayoutPage",
     "page-layout/StandalonePageLayoutPage"),
    ("PlanRequired", "ChooseYourPlan", "onboarding/ChooseYourPlan"),
    ("PlanRequiredSuccess", "PaymentSuccess", "onboarding/PaymentSuccess"),
    ("BookCall", "BookCall", "onboarding/BookCall"),
    ("Verify", "Verify", "onboarding/Verify"),
    ("WorkspaceActivation", "WorkspaceActivation",
     "onboarding/WorkspaceActivation"),
    ("CreateProfile", "CreateProfile", "onboarding/CreateProfile"),
    ("SyncEmails", "SyncEmails", "onboarding/SyncEmails"),
    ("InstallApps", "InstallApps", "onboarding/InstallApps"),
    ("InviteTeam", "InviteTeam", "onboarding/InviteTeam"),
]

_APP_TABLE = "packages/twenty-shared/src/types/AppPath.ts"
_APP_ROUTER = "packages/twenty-front/src/modules/app/hooks/useCreateRootAppRouter.tsx"


def _twenty_fixture(tmp_path: Path) -> list[str]:
    files = [_write(tmp_path, _APP_TABLE, _APP_PATH_TS)]
    imports = "\n".join(
        f"import {{ {comp} }} from '~/pages/{tail}';"
        for _m, comp, tail in _APP_ROUTES
    )
    routes = "\n".join(
        f"      <Route path={{AppPath.{member}}} element={{<{comp} />}} />"
        for member, comp, _t in _APP_ROUTES
    )
    files.append(_write(
        tmp_path, _APP_ROUTER,
        "import { createBrowserRouter, createRoutesFromElements, Route }"
        " from 'react-router-dom';\n"
        "import { AppPath } from 'twenty-shared/types';\n"
        f"{imports}\n"
        "export const useCreateRootAppRouter = () =>\n"
        "  createBrowserRouter(\n"
        "    createRoutesFromElements(\n"
        f"{routes}\n"
        "    ),\n"
        "  );\n",
    ))
    for _m, comp, tail in _APP_ROUTES:
        rel = f"packages/twenty-front/src/pages/{tail}.tsx"
        if rel not in files:
            files.append(_write(
                tmp_path, rel, f"export const {comp} = () => null;\n",
            ))
    return files


# ── flag / kill-switch ───────────────────────────────────────────────────────


def test_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # SEMANTIC flip migration (2026-07-21 pack №3, KEY_SCHEMA 34): unset
    # now arms the route-table arm (unset ≡ explicit-1).
    monkeypatch.delenv(SPA_ROUTE_TABLE_ENV, raising=False)
    assert spa_route_table_enabled() is True
    for falsy in ("", "0", "false", "no", "off"):
        monkeypatch.setenv(SPA_ROUTE_TABLE_ENV, falsy)
        assert spa_route_table_enabled() is False, falsy
    for truthy in ("1", "true", "yes", "on"):
        monkeypatch.setenv(SPA_ROUTE_TABLE_ENV, truthy)
        assert spa_route_table_enabled() is True, truthy


def test_unset_equals_one_and_zero_stays_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inverted kill-switch law (SEMANTIC flip migration, 2026-07-21
    pack №3, KEY_SCHEMA 34): unset == explicit '1' byte-identical; the
    explicit '0' world is the ONLY one with the table arm un-entered."""
    files = _twenty_fixture(tmp_path)

    monkeypatch.setenv(SPA_ROUTE_TABLE_ENV, "0")
    zero = _extract(tmp_path, files)
    assert zero == []  # no manifest -> Seg A/B inert too
    # The repo-wide pass itself is inert too (both dispatch paths).
    assert route_table_candidates(_ctx(tmp_path, files)) == []

    monkeypatch.setenv(SPA_ROUTE_TABLE_ENV, "1")
    armed = _extract(tmp_path, files)
    assert armed, "armed world must emit route-table candidates"
    monkeypatch.delenv(SPA_ROUTE_TABLE_ENV, raising=False)
    unset = _extract(tmp_path, files)
    assert unset == armed  # unset ≡ explicit-1 (the flip contract)
    monkeypatch.setenv(SPA_ROUTE_TABLE_ENV, "1")

    # Family kill-switch DOMINATES: SPA_ROUTER_ENTRIES=0 silences the
    # table arm even when armed (pre-B65-v3 world restoration).
    monkeypatch.setenv(SPA_ROUTER_ENTRIES_ENV, "0")
    assert route_table_candidates(_ctx(tmp_path, files)) == []


def test_no_new_extractor_hits_source_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arming the table flag must NOT register a new source key — the
    arm rides the existing ``spa-page`` source (B67 registry lesson,
    inherited fences law)."""
    from faultline.pipeline_v2.stage_1_extractors import (
        _load_default_extractors,
    )

    monkeypatch.delenv(SPA_ROUTE_TABLE_ENV, raising=False)
    monkeypatch.delenv(SPA_ROUTER_ENTRIES_ENV, raising=False)
    names_unset = sorted(e.name for e in _load_default_extractors())
    monkeypatch.setenv(SPA_ROUTE_TABLE_ENV, "1")
    names_armed = sorted(e.name for e in _load_default_extractors())
    assert names_unset == names_armed
    assert SPA_PAGE_SOURCE in names_armed


# ── twenty AppPath — named gate ──────────────────────────────────────────────


def test_twenty_apppath_20_patterns_named_owners(
    tmp_path: Path, table_on: None,
) -> None:
    anchors = _extract(tmp_path, _twenty_fixture(tmp_path))
    assert all(a.source == SPA_PAGE_SOURCE for a in anchors)
    patterns = _patterns_of(anchors)
    assert len(patterns) == 20, sorted(patterns)

    pfiles = _pattern_files(anchors)
    # Named owners (delegation gate)
    assert pfiles["/welcome"] == {
        "packages/twenty-front/src/pages/auth/SignInUp.tsx",
    }
    assert pfiles["/objects/:objectNamePlural"] == {
        "packages/twenty-front/src/pages/object-record/RecordIndexPage.tsx",
    }
    # Nav-alias values without a direct Route — pattern anchors WITHOUT
    # an owner, attributed to the FIRST consuming router file (IT2
    # container guard: the declaring ws-pkg is evidence, never a
    # capability).
    assert pfiles["/objects/tasks"] == {_APP_ROUTER}
    assert pfiles["/objects/opportunities"] == {_APP_ROUTER}
    # Slashless member emits raw (leading '/' is a prior, not a gate).
    assert "settings" in patterns
    # Ownerless root rides the table-name slug (app-path anchor) on the
    # consumer side.
    assert pfiles["/"] == {_APP_ROUTER}
    assert any(a.name == "app-path" for a in anchors)
    # Every route row is a PAGE row.
    assert {m for _p, m, _f in _routes_of(anchors)} == {"PAGE"}
    # IT2 container guard, explicit: NOTHING points at the declaring
    # table file — no anchor path, no route row.
    assert all(_APP_TABLE not in a.paths for a in anchors)
    assert all(f != _APP_TABLE for _p, _m, f in _routes_of(anchors))


def test_determinism_two_extracts(tmp_path: Path, table_on: None) -> None:
    files = _twenty_fixture(tmp_path)
    assert _extract(tmp_path, files) == _extract(tmp_path, files)


# ── twenty SettingsPath — consumption-primary slashless gate ─────────────────


def test_settingspath_slashless_table_is_taken(
    tmp_path: Path, table_on: None,
) -> None:
    """slash_ratio 0.0 (NO value starts with '/') MUST emit when consumed
    in path-position — the probe FORM correction; without it half of the
    twenty value is lost."""
    table = _write(
        tmp_path, "packages/twenty-shared/src/types/SettingsPath.ts",
        "export enum SettingsPath {\n"
        "  ProfilePage = 'profile',\n"
        "  Experience = 'experience',\n"
        "  Accounts = 'accounts',\n"
        "  AccountsEmails = 'accounts/emails',\n"
        "  AccountsCalendars = 'accounts/calendars',\n"
        "}\n",
    )
    router = _write(
        tmp_path,
        "packages/twenty-front/src/modules/app/components/SettingsRoutes.tsx",
        "import { Route, Routes } from 'react-router-dom';\n"
        "import { SettingsPath } from 'twenty-shared/types';\n"
        "import { SettingsProfile } from"
        " '~/pages/settings/profile/SettingsProfile';\n"
        "export const SettingsRoutes = () => (\n"
        "  <Routes>\n"
        "    <Route path={SettingsPath.ProfilePage}"
        " element={<SettingsProfile />} />\n"
        "  </Routes>\n"
        ");\n",
    )
    page = _write(
        tmp_path,
        "packages/twenty-front/src/pages/settings/profile/SettingsProfile.tsx",
        "export const SettingsProfile = () => null;\n",
    )
    anchors = _extract(tmp_path, [table, router, page])
    patterns = _patterns_of(anchors)
    assert patterns == {
        "profile", "experience", "accounts",
        "accounts/emails", "accounts/calendars",
    }
    assert _pattern_files(anchors)["profile"] == {page}


# ── novu ROUTES — const-object + package-scoped barrel resolution ────────────


def test_novu_routes_object_and_scoped_barrel(
    tmp_path: Path, table_on: None,
) -> None:
    """Const-object table consumed in a createBrowserRouter object array;
    '@/pages' resolves inside the consumer's OWN package (the
    playground/nextjs foreign-suffix trap must NOT win) and the
    multi-target star barrel is an honest no-hop entry."""
    files = [
        _write(tmp_path, "apps/dashboard/package.json", '{"name": "dash"}'),
        # Real-novu shape: SIGN_IN is the FIRST member. IT2 (coordinator
        # ruling 1): the fixed ``^\\s*`` branch parses it — the canon
        # numbers shifted 85->86 patterns (corpus re-probe: still
        # exactly-3 emitted tables, 0 false).
        _write(
            tmp_path, "apps/dashboard/src/utils/routes.ts",
            "export const ROUTES = {\n"
            "  SIGN_IN: '/auth/sign-in',\n"
            "  ROOT: '/',\n"
            "  AUTH_SSO: '/auth/sso',\n"
            "  WORKFLOWS: '/env/:environmentSlug/workflows',\n"
            "};\n",
        ),
        _write(
            tmp_path, "apps/dashboard/src/pages/index.ts",
            "export * from './sso-sign-in';\n"
            "export * from './sign-in';\n",
        ),
        _write(
            tmp_path, "apps/dashboard/src/pages/sso-sign-in.tsx",
            "export const SSOSignInPage = () => null;\n",
        ),
        _write(
            tmp_path, "apps/dashboard/src/pages/sign-in.tsx",
            "export const SignInPage = () => null;\n",
        ),
        _write(
            tmp_path, "apps/dashboard/src/dashboard-page.tsx",
            "export const DashboardPage = () => null;\n",
        ),
        _write(
            tmp_path, "apps/dashboard/src/main.tsx",
            "import { createBrowserRouter } from 'react-router-dom';\n"
            "import { SSOSignInPage, SignInPage } from '@/pages';\n"
            "import { DashboardPage } from './dashboard-page';\n"
            "import { ROUTES } from './utils/routes';\n"
            "export const router = createBrowserRouter([\n"
            "  { path: ROUTES.ROOT, element: <DashboardPage /> },\n"
            "  { path: ROUTES.AUTH_SSO, element: <SSOSignInPage /> },\n"
            "  { path: ROUTES.SIGN_IN, element: <SignInPage /> },\n"
            "]);\n",
        ),
        # The foreign trap: a DIFFERENT app whose pages/index.tsx would
        # win a repo-wide '/pages/index.tsx' suffix probe.
        _write(tmp_path, "playground/nextjs/package.json", '{"name": "pg"}'),
        _write(
            tmp_path, "playground/nextjs/src/pages/index.tsx",
            "export default function Home() { return null; }\n",
        ),
    ]
    anchors = _extract(tmp_path, files)
    patterns = _patterns_of(anchors)
    # IT2: '/auth/sign-in' (FIRST member) is parsed — the fixed regex
    # returns the S5b-H sign-in material.
    assert patterns == {
        "/", "/auth/sign-in", "/auth/sso",
        "/env/:environmentSlug/workflows",
    }
    pfiles = _pattern_files(anchors)
    # Scoped resolution: own-package barrel, NOT playground/nextjs.
    assert pfiles["/auth/sign-in"] == {"apps/dashboard/src/pages/index.ts"}
    assert pfiles["/auth/sso"] == {"apps/dashboard/src/pages/index.ts"}
    assert pfiles["/"] == {"apps/dashboard/src/dashboard-page.tsx"}
    # Nav-alias without a Route: the workflows pattern attributes to the
    # FIRST consuming router file (IT2 container guard), never the
    # declaring table file.
    assert pfiles["/env/:environmentSlug/workflows"] == {
        "apps/dashboard/src/main.tsx",
    }


# ── owner-walk filters (probe patches) ───────────────────────────────────────


def test_lazy_with_preload_bridge(tmp_path: Path, table_on: None) -> None:
    files = [
        _write(
            tmp_path, "src/paths.ts",
            "export enum Paths {\n"
            "  A = '/alpha',\n"
            "  B = '/beta',\n"
            "  C = '/gamma',\n"
            "}\n",
        ),
        _write(
            tmp_path, "src/router.tsx",
            "import { Route } from 'react-router-dom';\n"
            "const AlphaPage = lazyWithPreload(() =>"
            " import('./pages/AlphaPage'));\n"
            "export const r = (\n"
            "  <Route path={Paths.A} element={<AlphaPage />} />\n"
            ");\n",
        ),
        _write(
            tmp_path, "src/pages/AlphaPage.tsx",
            "export default function AlphaPage() { return null; }\n",
        ),
    ]
    anchors = _extract(tmp_path, files)
    assert _pattern_files(anchors)["/alpha"] == {"src/pages/AlphaPage.tsx"}


def test_wrapper_fallback_and_redirect_filters(
    tmp_path: Path, table_on: None,
) -> None:
    """``fallback={...}`` loader props strip; Suspense/ErrorBoundary
    wrappers never own; a Navigate-only element leaves the pattern
    OWNERLESS (still emitted — pattern-anchor law)."""
    table = _write(
        tmp_path, "src/paths.ts",
        "export enum Paths {\n"
        "  A = '/alpha',\n"
        "  B = '/beta',\n"
        "  C = '/gamma',\n"
        "}\n",
    )
    files = [
        table,
        _write(
            tmp_path, "src/router.tsx",
            "import { Route } from 'react-router-dom';\n"
            "import { AlphaPage } from './pages/AlphaPage';\n"
            "export const r = (<>\n"
            "  <Route path={Paths.A} element={\n"
            "    <Suspense fallback={<LoadingSpinner />}>\n"
            "      <ErrorBoundary><AlphaPage /></ErrorBoundary>\n"
            "    </Suspense>\n"
            "  } />\n"
            "  <Route path={Paths.B} element={<Navigate to='/alpha' />} />\n"
            "</>);\n",
        ),
        _write(
            tmp_path, "src/pages/AlphaPage.tsx",
            "export const AlphaPage = () => null;\n",
        ),
    ]
    anchors = _extract(tmp_path, files)
    pfiles = _pattern_files(anchors)
    # fallback + wrappers stripped -> the REAL page owns /alpha.
    assert pfiles["/alpha"] == {"src/pages/AlphaPage.tsx"}
    # Navigate is a wrapper, not an owner -> /beta emits ownerless,
    # attributed to the consuming router file (IT2 container guard).
    assert pfiles["/beta"] == {"src/router.tsx"}
    # /gamma has no Route at all -> nav-alias pattern, consumer-side.
    assert pfiles["/gamma"] == {"src/router.tsx"}


# ── NAMED anti-cases — the consumption gate is load-bearing ──────────────────


def _legit_plus(tmp_path: Path, extra: list[str]) -> list[str]:
    """A minimal legit table+consumer so the anti-case assertion is sharp
    (the fixture emits SOMETHING — just never the anti-case tables)."""
    return [
        _write(
            tmp_path, "src/paths.ts",
            "export enum Paths {\n"
            "  A = '/alpha',\n"
            "  B = '/beta',\n"
            "  C = '/gamma',\n"
            "}\n",
        ),
        _write(
            tmp_path, "src/router.tsx",
            "import { Route } from 'react-router-dom';\n"
            "import { AlphaPage } from './pages/AlphaPage';\n"
            "export const r = (\n"
            "  <Route path={Paths.A} element={<AlphaPage />} />\n"
            ");\n",
        ),
        _write(
            tmp_path, "src/pages/AlphaPage.tsx",
            "export const AlphaPage = () => null;\n",
        ),
        *extra,
    ]


def test_anticase_documentation_paths_never_emit(
    tmp_path: Path, table_on: None,
) -> None:
    """twenty DOCUMENTATION_PATHS (166 route-like doc links, slash 1.0):
    href-consumed, never path-position -> the consumption gate kills it."""
    extra = [
        _write(
            tmp_path, "src/constants/DocumentationPaths.ts",
            "export const DOCUMENTATION_PATHS = {\n"
            "  OBJECTS: '/user-guide/objects',\n"
            "  FIELDS: '/user-guide/fields',\n"
            "  API: '/developers/api',\n"
            "  WEBHOOKS: '/developers/webhooks',\n"
            "};\n",
        ),
        _write(
            tmp_path, "src/help-menu.tsx",
            "import { Route } from 'react-router-dom';\n"
            "export const Help = () =>"
            " <a href={DOCUMENTATION_PATHS.OBJECTS}>docs</a>;\n",
        ),
    ]
    anchors = _extract(tmp_path, _legit_plus(tmp_path, extra))
    patterns = _patterns_of(anchors)
    assert patterns == {"/alpha", "/beta", "/gamma"}
    assert not any("user-guide" in p or "developers" in p for p in patterns)


def test_anticase_background_asset_pack_never_emits(
    tmp_path: Path, table_on: None,
) -> None:
    """twenty BACKGROUND/DARK_BACKGROUND (15 CDN asset paths, slash 1.0):
    shape-candidate, zero router consumption -> never emitted."""
    extra = [
        _write(
            tmp_path, "src/feedback/Background.ts",
            "export const BACKGROUND = {\n"
            "  cell1: '/images/placeholders/light/cell-1.png',\n"
            "  cell2: '/images/placeholders/light/cell-2.png',\n"
            "  cell3: '/images/placeholders/light/cell-3.png',\n"
            "};\n",
        ),
    ]
    anchors = _extract(tmp_path, _legit_plus(tmp_path, extra))
    assert not any("images" in p for p in _patterns_of(anchors))


def test_anticase_appbasepath_interpolation_never_emits(
    tmp_path: Path, table_on: None,
) -> None:
    """twenty AppBasePath (3 slash-1.0 base URLs): consumed only inside
    template interpolation / URL building — never ``path=`` — so the
    path-position gate kills it even in a router file."""
    extra = [
        _write(
            tmp_path, "src/types/AppBasePath.ts",
            "export enum AppBasePath {\n"
            "  Root = '/',\n"
            "  Client = '/client',\n"
            "  Api = '/api-root',\n"
            "}\n",
        ),
        _write(
            tmp_path, "src/build-url.tsx",
            "import { Route } from 'react-router-dom';\n"
            "export const buildUrl = (p: string) =>"
            " `${AppBasePath.Client}/${p}`;\n",
        ),
    ]
    anchors = _extract(tmp_path, _legit_plus(tmp_path, extra))
    assert not any("client" in p or "api-root" in p for p in _patterns_of(anchors))


def test_anticase_redirect_maps_never_emit(
    tmp_path: Path, table_on: None,
) -> None:
    """DOCS_REDIRECTS (source/destination map) + skateshop ``redirects``
    (``redirect(redirects.toLogin)`` calls): no path-position use, and
    their consumers carry no router marker -> both die."""
    extra = [
        _write(
            tmp_path, "src/docs-redirects.ts",
            "export const DOCS_REDIRECTS = {\n"
            "  oldQuickstart: '/docs/quickstart-old',\n"
            "  oldSdk: '/docs/sdk-old',\n"
            "  oldApi: '/docs/api-old',\n"
            "};\n",
        ),
        _write(
            tmp_path, "next.config.mjs",
            "import { DOCS_REDIRECTS } from './src/docs-redirects';\n"
            "export default { redirects: async () => ([\n"
            "  { source: DOCS_REDIRECTS.oldQuickstart, destination: '/docs' },\n"
            "]) };\n",
        ),
        _write(
            tmp_path, "src/lib/constants.ts",
            "export const redirects = {\n"
            "  toLogin: '/signin',\n"
            "  toSignup: '/signup',\n"
            "  afterLogout: '/goodbye',\n"
            "};\n",
        ),
        _write(
            tmp_path, "src/lib/actions.ts",
            "import { redirects } from './constants';\n"
            "export const out = () => redirect(redirects.toLogin);\n",
        ),
    ]
    anchors = _extract(tmp_path, _legit_plus(tmp_path, extra))
    patterns = _patterns_of(anchors)
    assert patterns == {"/alpha", "/beta", "/gamma"}
    assert not any("docs" in p or "signin" in p for p in patterns)


def test_anticase_api_path_config_outside_router_file(
    tmp_path: Path, table_on: None,
) -> None:
    """Spec anti-scope survivor: an API path-config object consumed in
    path-position (``path: API_PATHS.USERS``) but in a NON-router file
    (an http client) -> the router-marker file gate kills it."""
    extra = [
        _write(
            tmp_path, "src/api/paths.ts",
            "export const API_PATHS = {\n"
            "  USERS: '/api/users',\n"
            "  TEAMS: '/api/teams',\n"
            "  BILLING: '/api/billing',\n"
            "};\n",
        ),
        _write(
            tmp_path, "src/api/client.ts",
            "import { API_PATHS } from './paths';\n"
            "export const fetchUsers = () =>"
            " request({ path: API_PATHS.USERS, method: 'GET' });\n",
        ),
    ]
    anchors = _extract(tmp_path, _legit_plus(tmp_path, extra))
    assert not any("api" in p for p in _patterns_of(anchors))


# ── shape gates ──────────────────────────────────────────────────────────────


def test_shape_gates_min_entries_and_ratio(
    tmp_path: Path, table_on: None,
) -> None:
    """<3 values -> no candidate; route-like ratio <0.8 -> no candidate
    (both consumed in path-position to prove it is the SHAPE that kills)."""
    files = [
        _write(
            tmp_path, "src/two.ts",
            "export enum TwoPaths {\n  A = '/a-page',\n  B = '/b-page',\n}\n",
        ),
        _write(
            tmp_path, "src/mixed.ts",
            "export const MIXED = {\n"
            "  a: '/real-path',\n"
            "  b: 'not a path at all',\n"
            "  c: 'also not a path!!',\n"
            "  d: 'nor this one, no',\n"
            "  e: 'free text value x',\n"
            "};\n",
        ),
        _write(
            tmp_path, "src/router.tsx",
            "import { Route } from 'react-router-dom';\n"
            "export const r = (<>\n"
            "  <Route path={TwoPaths.A} element={<X />} />\n"
            "  <Route path={MIXED.a} element={<Y />} />\n"
            "</>);\n",
        ),
    ]
    anchors = _extract(tmp_path, files)
    assert _patterns_of(anchors) == set()


# ── routes_index ride (Pass C) ───────────────────────────────────────────────


def test_routes_index_stamps_spa_page_kind(
    tmp_path: Path, table_on: None,
) -> None:
    from faultline.pipeline_v2.indexes import build_routes_index

    anchors = _extract(tmp_path, _twenty_fixture(tmp_path))
    rows = build_routes_index([], {SPA_PAGE_SOURCE: anchors})
    assert rows, "table rows must fold into routes_index"
    assert {r["method"] for r in rows} == {"PAGE"}
    assert {r.get("kind") for r in rows} == {"spa-page"}
    assert {r["pattern"] for r in rows} >= {
        "/welcome", "/objects/tasks", "settings",
    }


# ── IT2 container guard — declaring package vs owned capability ──────────────


def test_container_guard_declaring_pkg_gets_nothing_owned_capability_lives(
    tmp_path: Path, table_on: None,
) -> None:
    """IT2 ruling 2 pin (both sides): a ws-pkg that only DECLARES the
    table receives ZERO rows/paths (evidence, not a capability — the
    'twenty-shared' container-PF exhibit), while a blocklist-shaped
    OWNED member keeps its page-file anchor (real capability lives)."""
    table = _write(
        tmp_path, "packages/shared/src/types/SettingsPath.ts",
        "export enum SettingsPath {\n"
        "  Blocklist = 'accounts/blocklist',\n"
        "  ProfilePage = 'profile',\n"
        "  Experience = 'experience',\n"
        "}\n",
    )
    files = [
        table,
        _write(tmp_path, "packages/shared/package.json", '{"name": "shared"}'),
        _write(tmp_path, "apps/front/package.json", '{"name": "front"}'),
        _write(
            tmp_path, "apps/front/src/SettingsRoutes.tsx",
            "import { Route, Routes } from 'react-router-dom';\n"
            "import { SettingsBlocklist } from"
            " '~/pages/settings/SettingsBlocklist';\n"
            "export const SettingsRoutes = () => (\n"
            "  <Routes>\n"
            "    <Route path={SettingsPath.Blocklist}"
            " element={<SettingsBlocklist />} />\n"
            "  </Routes>\n"
            ");\n",
        ),
        _write(
            tmp_path, "apps/front/src/pages/settings/SettingsBlocklist.tsx",
            "export const SettingsBlocklist = () => null;\n",
        ),
    ]
    anchors = _extract(tmp_path, files)
    pfiles = _pattern_files(anchors)
    # The owned capability keeps its page anchor.
    assert pfiles["accounts/blocklist"] == {
        "apps/front/src/pages/settings/SettingsBlocklist.tsx",
    }
    # Ownerless members attribute to the consuming router file.
    assert pfiles["profile"] == {"apps/front/src/SettingsRoutes.tsx"}
    # The declaring package receives NOTHING — no path, no row.
    assert all(
        not p.startswith("packages/shared/") for a in anchors for p in a.paths
    )
    assert all(
        not f.startswith("packages/shared/")
        for _p, _m, f in _routes_of(anchors)
    )


# ── per-workspace dispatch — the cross-workspace join (KS forensics) ─────────


def test_per_workspace_dispatch_joins_cross_workspace_table(
    tmp_path: Path, table_on: None,
) -> None:
    """The twenty KS-forensics exhibit: the table lives in one workspace
    (twenty-shared) while its router consumers live in another
    (twenty-front). Scoped per-workspace passes can never join them —
    the repo-wide post-pass in ``run_stage_1_per_workspace`` MUST."""
    from faultline.pipeline_v2.stage_0_intake import Workspace
    from faultline.pipeline_v2.stage_1_per_workspace import (
        run_stage_1_per_workspace,
    )

    files = _twenty_fixture(tmp_path)
    shared = [f for f in files if f.startswith("packages/twenty-shared/")]
    front = [f for f in files if f.startswith("packages/twenty-front/")]
    ctx = _ctx(
        tmp_path, files, monorepo=True,
        workspaces=[
            Workspace(
                name="twenty-shared", path="packages/twenty-shared",
                stack="node", files=shared,
            ),
            Workspace(
                name="twenty-front", path="packages/twenty-front",
                stack="vite", files=front,
            ),
        ],
    )
    result = run_stage_1_per_workspace(
        ctx, extractors=[SpaRouterExtractor()],
    )
    anchors = result.stage1_out.get(SPA_PAGE_SOURCE) or []
    patterns = _patterns_of(anchors)
    assert "/welcome" in patterns
    assert len(patterns) == 20, sorted(patterns)
