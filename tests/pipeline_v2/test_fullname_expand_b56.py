"""B56 — full-name display law for abbreviations (DISPLAY CHANNEL ONLY).

The class (operator 2026-07-13): a bare abbreviation tile ("Pbac", "Sso",
"Ooo", "I18n", "Wp") is opaque; an abbreviation must ALWAYS carry its full
name — GROUNDED in the repo (code identifiers, i18n KEY names, JSX labels,
package manifest, route segments), NEVER invented, NEVER from a locale VALUE
or a README/comment.

Every exhibit and every SACRED anti-case from the architect brief §7 has a
unit here, on SYNTHETIC fixtures (the mechanism must hold on a fixture, not a
live repo). Flagship honest-debt case: ``Pbac`` — its only spelling lives in a
JSDoc COMMENT, which is not an allowed source, so it is ``missing:expansion``,
never auto-expanded.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from faultline.models.types import Feature, MemberFile, UserFlow
from faultline.pipeline_v2 import fullname_expand as fe
from faultline.pipeline_v2.fullname_expand import (
    FULLNAME_LAW_ENV,
    apply_fullname_expansion,
    compose_display,
    expand_abbreviation,
    is_abbreviation_shape,
    load_fullname_whitelist,
    pf_fullname_law_enabled,
)
from faultline.pipeline_v2.naming_contract import run_naming_contract

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── fixtures / helpers ───────────────────────────────────────────────────


class _PF:
    """A minimal PF stub for the MECHANISM units (member_files as dicts)."""

    def __init__(self, anchor_id: str = "", rels: list[str] | None = None):
        self.anchor_id = anchor_id
        self.member_files = [
            {"path": r, "primary": True} for r in (rels or [])
        ]
        self.paths: list[str] = []


def _write(root: Path, rel: str, content: str) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return rel


def _pkg_json(root: Path, pkg_dir: str, doc: dict[str, Any]) -> None:
    (root / pkg_dir).mkdir(parents=True, exist_ok=True)
    (root / pkg_dir / "package.json").write_text(
        json.dumps(doc), encoding="utf-8")


def _config_json(root: Path, pkg_dir: str, doc: dict[str, Any]) -> None:
    (root / pkg_dir).mkdir(parents=True, exist_ok=True)
    (root / pkg_dir / "config.json").write_text(
        json.dumps(doc), encoding="utf-8")


@pytest.fixture()
def law_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FULLNAME_LAW_ENV, "1")


@pytest.fixture(autouse=True)
def _fresh_caches() -> Any:
    load_fullname_whitelist.cache_clear()
    fe._brand_casing.cache_clear()
    fe._workspace_package_dirs.cache_clear()
    yield
    load_fullname_whitelist.cache_clear()
    fe._brand_casing.cache_clear()
    fe._workspace_package_dirs.cache_clear()


# ── the SHAPE detector (P1-P4) + legit-word spare ────────────────────────


@pytest.mark.parametrize("token,prong", [
    ("Wp", "P1"), ("TLS", "P1"), ("Trpc", "P1"), ("HTTP", "P1"),
    ("i18n", "P2"), ("a11y", "P2"), ("k8s", "P2"), ("l10n", "P2"),
    ("Di", "P3"), ("Ee", "P3"), ("Ooo", "P3"), ("Sso", "P3"), ("Gen", "P3"),
    ("Pbac", "P4"), ("Htmltopdf", "P4"), ("Rbac", "P4"), ("Ldap", "P4"),
])
def test_shape_detector_flags_exhibits(token: str, prong: str) -> None:
    assert is_abbreviation_shape(token) == prong


@pytest.mark.parametrize("word", [
    # SACRED legit-word spare (brief §7) — a real word with a valid onset is
    # NEVER flagged (the P4 unpronounceable prong cannot fire on it).
    "Bulk", "Link", "Post", "Sign", "Form", "Send", "Poll", "Fact", "Cold",
    "Blob", "Rich", "Draft", "Query", "Share", "Trace",
])
def test_shape_detector_never_flags_legit_words(word: str) -> None:
    assert is_abbreviation_shape(word) is None


@pytest.mark.parametrize("word", ["api", "app", "ai", "ui", "auth", "sdk"])
def test_whitelist_product_words_not_flagged(word: str) -> None:
    assert "auth" in load_fullname_whitelist()  # B27 spare-law present
    assert is_abbreviation_shape(word) is None


def test_pbac_is_p4_not_p1_p2_p3() -> None:
    # Pbac has a vowel ('a') and len 4 — the literal spec prongs P1-P3 all
    # MISS it; only the unpronounceable prong P4 catches it. This is the gap
    # the design decision closes.
    assert is_abbreviation_shape("Pbac") == "P4"


def test_compose_display_form() -> None:
    assert compose_display("Single Sign-On", "Sso") == "Single Sign-On (SSO)"
    assert compose_display("Internationalization", "I18n") == \
        "Internationalization (I18N)"


# ── positive expansions (every flagship exhibit) ─────────────────────────


def test_ooo_from_code_identifier(tmp_path: Path) -> None:
    rel = _write(tmp_path, "ooo/_router.tsx",
                 "export async function outOfOfficeCreateOrUpdate(i){return i}")
    pf = _PF(rels=[rel])
    full, src = expand_abbreviation("Ooo", pf, tmp_path)
    assert full == "Out of Office"
    assert src.startswith("identifier:")


def test_ooo_from_i18n_key_not_value(tmp_path: Path) -> None:
    # The KEY name (out_of_office) is allowed; the VALUE is never read.
    rel = _write(tmp_path, "packages/i18n/locales/en/common.json",
                 json.dumps({"out_of_office": "Ausser Haus", "other": "x"}))
    pf = _PF(rels=[rel])
    full, src = expand_abbreviation("Ooo", pf, tmp_path)
    assert full == "Out of Office"
    assert src.startswith("i18n-key:")


def test_i18n_from_manifest_description(tmp_path: Path) -> None:
    _pkg_json(tmp_path, "packages/i18n", {
        "name": "@x/i18n",
        "description": "Internationalization (i18n) utilities for the app",
    })
    pf = _PF(anchor_id="ws:packages/i18n")
    full, src = expand_abbreviation("I18n", pf, tmp_path)
    assert full == "Internationalization"
    assert src.startswith("manifest:")
    assert compose_display(full, "I18n") == "Internationalization (I18N)"


def test_sso_from_jsx_label_gloss(tmp_path: Path, law_on: None) -> None:
    rel = _write(tmp_path, "auth/SignInUpWithSSO.tsx",
                 '<SignInUpWithSSO label="Single sign-on (SSO)" />')
    pf = _PF(rels=[rel])
    res = apply_fullname_expansion("Sso", pf, tmp_path)
    assert res.display == "Single Sign-On (SSO)"
    assert res.source.startswith("i18n-key:")
    assert res.abbr == "sso"


def test_sso_from_identifier_reconstruction(tmp_path: Path) -> None:
    # Identifier reconstruction yields the un-hyphenated form (honest: the
    # hyphen is not derivable from a camelCase identifier). The citing file
    # sits in the token's home dir (F4).
    rel = _write(tmp_path, "sso/saml.ts", "const singleSignOnServices = 1;")
    pf = _PF(rels=[rel])
    full, _src = expand_abbreviation("Sso", pf, tmp_path)
    assert full == "Single Sign On"


def test_wp_from_manifest_displayname_brand(tmp_path: Path) -> None:
    _pkg_json(tmp_path, "packages/embeds/wordpress", {
        "name": "@typebot.io/wordpress", "displayName": "WordPress",
    })
    pf = _PF(anchor_id="ws:packages/embeds/wordpress")
    full, src = expand_abbreviation("Wp", pf, tmp_path)
    assert full == "WordPress"
    assert src.startswith("manifest:")
    assert compose_display(full, "Wp") == "WordPress (WP)"


def test_edr_lead_multiword_tail_verbatim(tmp_path: Path, law_on: None) -> None:
    rel = _write(tmp_path, "edr/core.ts", "class EndpointDetectionResponse {}")
    pf = _PF(rels=[rel])
    res = apply_fullname_expansion("EDR Core", pf, tmp_path)
    # honest reconstruction — no invented "and"; the "Core" qualifier is kept
    # verbatim.
    assert res.display == "Endpoint Detection Response (EDR) Core"
    assert res.abbr == "edr"
    assert res.composed_lead == "Endpoint Detection Response (EDR)"


# ── SACRED anti-cases ────────────────────────────────────────────────────


def test_pbac_honest_debt_comment_is_not_a_source(
    tmp_path: Path, law_on: None,
) -> None:
    """FLAGSHIP: the full form 'Permission-Based Access Control' exists ONLY in
    a JSDoc comment (maintainer prose). Comments are stripped before any scan,
    so there is NO allowed evidence ⇒ display UNCHANGED + missing:expansion.
    Never invented."""
    rel = _write(tmp_path, "scripts/seed-pbac-organization.ts", (
        "/**\n"
        " * PBAC (Permission-Based Access Control) seeding script.\n"
        " * Builds a PermissionBasedAccessControl-shaped org.\n"
        " */\n"
        "export function seedPbac() { return 1; }\n"
    ))
    pf = _PF(rels=[rel])
    full, src = expand_abbreviation("Pbac", pf, tmp_path)
    assert full is None
    assert src == "missing:expansion"
    res = apply_fullname_expansion("Pbac", pf, tmp_path)
    assert res.display is None
    assert res.source == "missing:expansion"


def test_no_show_phrase_guard_untouched(tmp_path: Path, law_on: None) -> None:
    # Every token is a plain word ⇒ never flagged, and NOT missing.
    res = apply_fullname_expansion("No Show", _PF(), tmp_path)
    assert res.display is None
    assert res.source == "not-flagged"


def test_feature_opt_in_phrase_guard_untouched(
    tmp_path: Path, law_on: None,
) -> None:
    res = apply_fullname_expansion("Feature Opt In", _PF(), tmp_path)
    assert res.source == "not-flagged"


def test_vendor_gate_single_token(tmp_path: Path, law_on: None) -> None:
    _config_json(tmp_path, "packages/app-store/dub", {"name": "Dub"})
    pf = _PF(anchor_id="hub:packages/app-store/dub")
    res = apply_fullname_expansion("Dub", pf, tmp_path)
    assert res.display is None
    assert res.source == "vendor"


def test_vendor_qualifier_tail_untouched(tmp_path: Path, law_on: None) -> None:
    # "App Store — CrowdStrike" — lead 'App' is a product word; the vendor
    # qualifier is never a candidate. Phrase stays as-is.
    res = apply_fullname_expansion("App Store — CrowdStrike", _PF(), tmp_path)
    assert res.display is None


def test_ambiguous_two_distinct_expansions(tmp_path: Path, law_on: None) -> None:
    # Both citing files live in the token's home dir (F4), yet yield two
    # DISTINCT plain-worded expansions ⇒ ambiguous, do not expand.
    _write(tmp_path, "abc/a.ts", "class AppleBananaCherry {}")
    _write(tmp_path, "abc/b.ts", "class AlphaBravoCharlie {}")
    pf = _PF(rels=["abc/a.ts", "abc/b.ts"])
    res = apply_fullname_expansion("Abc", pf, tmp_path)
    assert res.display is None
    assert res.source == "ambiguous"


def test_no_evidence_real_acronym_missing(tmp_path: Path, law_on: None) -> None:
    res = apply_fullname_expansion("HTTP", _PF(), tmp_path)
    assert res.display is None
    assert res.source == "missing:expansion"


@pytest.mark.parametrize("word", ["Bulk", "Link", "Post", "Sign", "Form"])
def test_legit_word_not_flagged_orchestrator(
    tmp_path: Path, law_on: None, word: str,
) -> None:
    res = apply_fullname_expansion(word, _PF(), tmp_path)
    assert res.display is None
    assert res.source == "not-flagged"  # not flagged ⇒ NOT missing either


def test_locale_value_only_source_is_forbidden(
    tmp_path: Path, law_on: None,
) -> None:
    # The full form exists ONLY as a locale VALUE (key does not match). The
    # value is a forbidden source ⇒ missing:expansion.
    rel = _write(tmp_path, "app/locales/en.json",
                 json.dumps({"randomKey": "Permission-Based Access Control"}))
    pf = _PF(rels=[rel])
    full, src = expand_abbreviation("Pbac", pf, tmp_path)
    assert full is None
    assert src == "missing:expansion"


def test_flag_off_orchestrator_is_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default ON post-B62; pin OFF via X=0 ⇒ even with evidence present,
    # no work is done.
    monkeypatch.setenv(FULLNAME_LAW_ENV, "0")
    assert not pf_fullname_law_enabled()
    _write(tmp_path, "auth/saml.ts", "const singleSignOnServices = 1;")
    pf = _PF(rels=["auth/saml.ts"])
    res = apply_fullname_expansion("Sso", pf, tmp_path)
    assert res.display is None
    assert res.source == "not-flagged"


def test_never_crashes_on_missing_repo() -> None:
    # Bounded best-effort: a bogus repo_root or None never raises.
    assert expand_abbreviation("Sso", _PF(), None) == (None, "missing:expansion")
    assert expand_abbreviation("Sso", _PF(), "/no/such/dir/xyz") == (
        None, "missing:expansion")


# ── real-exhibit defect fixes (coordinator probe, 2026-07-13) ────────────


@pytest.mark.parametrize("doc_rel", [
    "packages/embeds/wordpress/README.md",
    "docs/overview.mdx",
    "trunk/README.txt",
    "docs/guide.rst",
    "docs/guide.adoc",
])
def test_prose_doc_member_is_never_a_source(
    tmp_path: Path, law_on: None, doc_rel: str,
) -> None:
    """Defect 1 (typebot Wp README leak): a README / prose-doc member spelling
    the full form must NOT expand — README grounding is a hard-rule FORBIDDEN
    source; the format gate skips the file wholesale ⇒ missing:expansion."""
    rel = _write(tmp_path, doc_rel,
                 "# WordPress\nThe WordPress plugin. wordPressEmbedLibrary\n")
    pf = _PF(rels=[rel])
    full, src = expand_abbreviation("Wp", pf, tmp_path)
    assert full is None
    assert src == "missing:expansion"


@pytest.mark.parametrize("pkg_dir,pkg_name,token", [
    ("packages/ee", "@documenso/ee", "Ee"),
    ("packages/js", "@typebot.io/js", "Js"),
])
def test_slug_package_name_is_not_vendor_identity(
    tmp_path: Path, law_on: None, pkg_dir: str, pkg_name: str, token: str,
) -> None:
    """Defect 2 (documenso Ee / typebot Js): a package.json name that is just
    the dir slug again (B27 authored test) establishes NO vendor identity —
    the token falls through to evidence search and lands honest debt."""
    _pkg_json(tmp_path, pkg_dir, {"name": pkg_name})
    pf = _PF(anchor_id=f"ws:{pkg_dir}")
    res = apply_fullname_expansion(token, pf, tmp_path)
    assert res.source == "missing:expansion"  # NOT "vendor"
    assert res.display is None


def test_wp_brand_cased_route_segment(tmp_path: Path, law_on: None) -> None:
    """Defect 3 (typebot Wp): the only legal source is the glued lowercase
    'wordpress' dir/route segment; brand_casing (corroboration YAML) reveals
    its word structure (WordPress → [Word, Press] → 'wp') and renders the
    brand verbatim."""
    _pkg_json(tmp_path, "packages/embeds/wordpress",
              {"name": "@typebot.io/wordpress"})  # slug name — NOT evidence
    pf = _PF(anchor_id="ws:packages/embeds/wordpress")
    res = apply_fullname_expansion("Wp", pf, tmp_path)
    assert res.display == "WordPress (WP)"
    assert res.source == "route"


def test_workspace_same_name_package_manifest(
    tmp_path: Path, law_on: None,
) -> None:
    """Defect 4 (cal.com I18n): a route-anchored PF whose token names a
    workspace package (root package.json workspaces globs) reads THAT
    package's manifest as an additional manifest source."""
    _write(tmp_path, "package.json",
           json.dumps({"name": "root", "workspaces": ["packages/*"]}))
    _pkg_json(tmp_path, "packages/i18n", {
        "name": "@calcom/i18n",
        "description":
            "Internationalization (i18n) utilities and translations",
    })
    pf = _PF(anchor_id="route:apps/web/pages/api/trpc/i18n")
    res = apply_fullname_expansion("I18n", pf, tmp_path)
    assert res.display == "Internationalization (I18N)"
    assert res.source.startswith("manifest:packages/i18n/package.json")


def test_workspace_same_name_package_pnpm(
    tmp_path: Path, law_on: None,
) -> None:
    """Defect 4 variant: pnpm-workspace.yaml packages globs work the same."""
    _write(tmp_path, "pnpm-workspace.yaml",
           "packages:\n  - 'packages/*'\n")
    _pkg_json(tmp_path, "packages/i18n", {
        "name": "@x/i18n",
        "description": "Internationalization (i18n) helpers",
    })
    pf = _PF(anchor_id="route:apps/web/api/i18n")
    res = apply_fullname_expansion("I18n", pf, tmp_path)
    assert res.display == "Internationalization (I18N)"
    assert res.source.startswith("manifest:packages/i18n/package.json")


def test_no_workspaces_config_no_package_lookup(
    tmp_path: Path, law_on: None,
) -> None:
    """Defect 4 anti-unit: without a workspaces config the same-name package
    is NEVER read (no path guessing — config-grounded only)."""
    _pkg_json(tmp_path, "packages/i18n", {
        "name": "@x/i18n",
        "description": "Internationalization (i18n) helpers",
    })
    pf = _PF(anchor_id="route:apps/web/api/i18n")
    res = apply_fullname_expansion("I18n", pf, tmp_path)
    assert res.display is None
    assert res.source == "missing:expansion"


# ── round-3 false-expansion filters F1-F4 (full-census false cases) ──────


def test_f1_self_echo_expansion_rejected(tmp_path: Path, law_on: None) -> None:
    """Census false #1 (Soc0 'EDR Detections' -> 'Edr Date Ranges (EDR)'):
    even in the token's HOME dir (F4 passes) an expansion whose first word IS
    the token ⇒ F1 rejects ⇒ honest debt."""
    rel = _write(tmp_path, "features/edr/EdrDetectionsPage.tsx",
                 "const edrDateRanges = getRanges();")
    res = apply_fullname_expansion("EDR Detections", _PF(rels=[rel]), tmp_path)
    assert res.display is None
    assert res.source == "missing:expansion"


def test_f2_error_literal_initials_rejected(
    tmp_path: Path, law_on: None,
) -> None:
    """Census false #5 (onyx 'MCP' -> 'Missing Code Parameter (MCP)'): an
    error-message literal in the token's own home dir (F4 passes!) — plain
    literals participate ONLY via the explicit gloss ⇒ F2 rejects."""
    rel = _write(tmp_path, "server/mcp/api.py",
                 'raise ValueError("Missing code parameter")\n')
    res = apply_fullname_expansion("MCP", _PF(rels=[rel]), tmp_path)
    assert res.display is None
    assert res.source == "missing:expansion"


def test_f2_f1_sentence_literal_self_echo_rejected(
    tmp_path: Path, law_on: None,
) -> None:
    """Census false #3 (infisical 'SSH Certificate Authority' ->
    'Successfully Ssh Host (SSH) …'): a toast sentence literal + self-echo.
    F2 kills the literal-initials path; F1/F3 would kill the phrase anyway."""
    rel = _write(tmp_path, "pages/ssh/SshHostGroupModal.tsx",
                 'toast("Successfully ssh host attached");')
    res = apply_fullname_expansion(
        "SSH Certificate Authority", _PF(rels=[rel]), tmp_path)
    assert res.display is None
    assert res.source == "missing:expansion"


def test_f2_f1_sse_subscription_error_rejected(
    tmp_path: Path, law_on: None,
) -> None:
    """Census false #6 (twenty 'Sse DB Event' -> 'Sse Subscription Error
    (SSE) …'): error literal + self-echo ⇒ rejected."""
    rel = _write(
        tmp_path, "modules/sse-db-event/useTriggerEventStreamCreation.ts",
        'throw new Error("Sse subscription error");')
    res = apply_fullname_expansion("Sse DB Event", _PF(rels=[rel]), tmp_path)
    assert res.display is None
    assert res.source == "missing:expansion"


def test_f3_nonword_fragment_rejected(tmp_path: Path, law_on: None) -> None:
    """Census false #2 (Soc0 'EDR — SentinelOne' -> 'Extract Dv Rows (EDR)'):
    the identifier sits in services/edr/ (F4 home passes!) but 'Dv' is a
    non-word fragment ⇒ F3 rejects ⇒ honest debt."""
    rel = _write(tmp_path, "services/edr/sentinelone.py",
                 "def extract_dv_rows(rows):\n    return rows\n")
    res = apply_fullname_expansion(
        "EDR — SentinelOne", _PF(rels=[rel]), tmp_path)
    assert res.display is None
    assert res.source == "missing:expansion"


def test_f4_identifier_outside_token_home_rejected(
    tmp_path: Path, law_on: None,
) -> None:
    """Census false #4 (infisical 'PAM Resource Discovery' -> 'Platform Actor
    Metadata (PAM) …'): a real, plain-worded identifier (F1/F3 pass) in a
    FOREIGN module — no 'pam' path segment / basename word ⇒ F4 rejects."""
    rel = _write(tmp_path, "services/audit-log/audit-log-types.ts",
                 "export interface PlatformActorMetadata { id: string }")
    res = apply_fullname_expansion(
        "PAM Resource Discovery", _PF(rels=[rel]), tmp_path)
    assert res.display is None
    assert res.source == "missing:expansion"


def test_f4_lowercase_kebab_fragment_is_not_home(
    tmp_path: Path, law_on: None,
) -> None:
    """Census residual (supabase 'Pg Meta' -> 'Privilege Grant (PG) Meta'):
    a coincidental zod object in pg-meta-column-privileges.ts. The lowercase
    kebab fragment 'pg' is NOT an uppercase-bounded acronym in the basename
    and no dir is named 'pg' ⇒ not home ⇒ F4 rejects ⇒ honest debt."""
    rel = _write(tmp_path,
                 "packages/pg-meta/src/pg-meta-column-privileges.ts",
                 "const privilegeGrant = z.object({ grantee: z.string() });")
    res = apply_fullname_expansion("Pg Meta", _PF(rels=[rel]), tmp_path)
    assert res.display is None
    assert res.source == "missing:expansion"


def test_f4_home_dir_identifier_survives(tmp_path: Path, law_on: None) -> None:
    """TRUE pair for F4 (better-auth 'sso'): SingleSignOnService inside
    packages/sso/src — whole path segment home ⇒ expands."""
    rel = _write(tmp_path, "packages/sso/src/index.ts",
                 "export class SingleSignOnService {}")
    res = apply_fullname_expansion("sso", _PF(rels=[rel]), tmp_path)
    assert res.display == "Single Sign On (SSO)"
    assert res.source.startswith("identifier:packages/sso/src/index.ts")


def test_f4_home_basename_identifier_survives(
    tmp_path: Path, law_on: None,
) -> None:
    """TRUE pair for F4 (cal.com Ooo): ONE identifier root inside
    PrismaOOORepository.ts — uppercase-bounded basename word is home; NO
    '>= 2 distinct identifiers' rule exists (it would kill this)."""
    rel = _write(tmp_path, "repositories/PrismaOOORepository.ts",
                 "const entry = prisma.outOfOfficeEntry;")
    res = apply_fullname_expansion("Ooo", _PF(rels=[rel]), tmp_path)
    assert res.display == "Out of Office (OOO)"
    assert res.source.startswith("identifier:")


def test_gloss_in_literal_survives_f2(tmp_path: Path, law_on: None) -> None:
    """TRUE pair for F2 (twenty Sso): the explicit author gloss inside a JSX
    label literal still expands — F2 removes only INITIALS-matching over
    literals, never the gloss mechanism."""
    rel = _write(tmp_path, "auth/components/SignInUpWithSSO.tsx",
                 '<Button label="Single sign-on (SSO)" />')
    res = apply_fullname_expansion("Sso", _PF(rels=[rel]), tmp_path)
    assert res.display == "Single Sign-On (SSO)"


# ── integration: run_naming_contract (the DISPLAY channel + identity) ────


def _pf_feature(slug: str, display: str, anchor_id: str,
                member_rel: str | None) -> Feature:
    f = Feature(
        name=slug, display_name=display, layer="product",
        paths=[member_rel] if member_rel else [], authors=["a"],
        total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0, anchor_id=anchor_id,
    )
    if member_rel:
        f.member_files = [MemberFile(path=member_rel, primary=True,
                                     role="anchor", confidence=1.0)]
    return f


def test_integration_pf_expands_display_only(
    tmp_path: Path, law_on: None,
) -> None:
    _write(tmp_path, "packages/sso/saml.ts",
           "const singleSignOnServices = 1;")
    pf = _pf_feature("sso", "Sso", "ws:packages/sso",
                     "packages/sso/saml.ts")
    tele = run_naming_contract([pf], [], repo_root=tmp_path)
    # DISPLAY changed …
    assert pf.display_name == "Single Sign On (SSO)"
    assert tele["pf_fullname_expanded"] == 1
    # … but EVERY identity field is untouched.
    assert pf.name == "sso"
    assert pf.anchor_id == "ws:packages/sso"
    assert [m.path for m in pf.member_files] == ["packages/sso/saml.ts"]
    assert pf.paths == ["packages/sso/saml.ts"]


def test_integration_pbac_missing_display_unchanged(
    tmp_path: Path, law_on: None,
) -> None:
    _write(tmp_path, "packages/pbac/seed.ts", (
        "/** PBAC (Permission-Based Access Control) */\n"
        "export function seedPbac(){return 1}\n"
    ))
    pf = _pf_feature("pbac", "Pbac", "ws:packages/pbac",
                     "packages/pbac/seed.ts")
    tele = run_naming_contract([pf], [], repo_root=tmp_path)
    assert pf.display_name == "Pbac"          # UNCHANGED
    assert tele["pf_fullname_missing"] == 1   # honest debt, measured
    assert tele["pf_fullname_expanded"] == 0


def test_integration_uf_inherits_expansion(
    tmp_path: Path, law_on: None,
) -> None:
    _write(tmp_path, "packages/sso/saml.ts",
           "const singleSignOnServices = 1;")
    pf = _pf_feature("sso", "Sso", "ws:packages/sso",
                     "packages/sso/saml.ts")
    uf = UserFlow(id="UF-1", name="Configure Sso", intent="manage",
                  resource="sso", product_feature_id="sso")
    tele = run_naming_contract(
        [pf], [uf], repo_root=tmp_path,
        uf_authored_names={"UF-1": ["Configure Sso"]})
    assert pf.display_name == "Single Sign On (SSO)"
    assert uf.name == "Configure Single Sign On (SSO)"
    assert tele["uf_fullname_inherited"] == 1


def test_integration_flag_off_byte_identical_scan_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Flag forced OFF (X=0; default ON post-B62) ⇒ no B56 telemetry keys, no
    # display expansion (byte-identical naming_contract telemetry vs pre-B56).
    monkeypatch.setenv(FULLNAME_LAW_ENV, "0")
    _write(tmp_path, "packages/sso/saml.ts",
           "const singleSignOnServices = 1;")
    pf = _pf_feature("sso", "Sso", "ws:packages/sso",
                     "packages/sso/saml.ts")
    uf = UserFlow(id="UF-1", name="Configure Sso", intent="manage",
                  resource="sso", product_feature_id="sso")
    tele = run_naming_contract([pf], [uf], repo_root=tmp_path)
    assert "pf_fullname_expanded" not in tele
    assert "pf_fullname_missing" not in tele
    assert "uf_fullname_inherited" not in tele
    assert "(SSO)" not in (pf.display_name or "")
    assert "Single Sign" not in uf.name
