"""R5 phase-2 (``FAULTLINE_NAMING_WAVE_R5``) — member-evidence display
derivation: R5-3 rename + plural + conf-drop lanes, R5-4 compose
canonicalizer, R5-5-ext brand-echo rung.

Named units for the delegated exhibits (probe canon 2026-07-19,
/private/tmp/r5probe-work/probe2.py + out/{r53,r54,r55}.json):

  * split lane targets — twenty 'Manage pagelayouts' -> 'Manage page
    layouts'; documenso 'Signin'/'Signup' -> 'Sign in'/'Sign up';
    langfuse 'Blobstorage' family -> 'Blob storage' ×3; the api_keys
    snake runs.
  * split lane anti-cases — brand-camel 'Deepseek'/'Hitpay'/'Qrcode'
    NEVER split (separator-ratio rule: camel-only, non-verb-led);
    ALL-CAPS constants never vote (CONST-GUARD) and never re-spell;
    the acronym-by-symbols rung DOES NOT EXIST (refuted:
    dpa={Dpa:46, dpa:21, DPA:2}).
  * plural rung — 'Manage account setting' -> settings on >= 3x member
    evidence, anchor-source segment excluded; grammar guards.
  * conf-drop — 'Htmltopdf'/'Bgtasks' keep the name, cap high->medium,
    stamp ``shape:unresolved-dir-token``; healthy single-token displays
    with symbol life ('Signing') keep high.
  * brand-echo — 'Connect twenty SDK' demotes at ANY member count;
    'Connect Twenty to Zapier' / 'Manage notifications' /
    'Manage features' / 'Manage documents' / 'Run billing' KEEP.
  * R5-4 — the 7 probe heals byte-exact + identity on healthy composes.

SACRED: flag unset/=0 ⇒ names, confidences and telemetry byte-identical.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultline.models.types import Feature, Flow, FlowSymbolAttribution, UserFlow
from faultline.pipeline_v2.naming_contract import (
    NAMING_WAVE_R5_ENV,
    _apply_r5_confidence_caps,
    load_naming_vocab,
    run_naming_contract,
)
from faultline.pipeline_v2.naming_wave_r5 import (
    MemberEvidence,
    brand_echo_pkg,
    build_board_evidence,
    canonicalize_compose,
    derive_split_display,
    plural_rename,
    r5_vocab_sets,
    unresolved_dir_token,
)
from faultline.pipeline_v2.stage_6_86_anchored_mint import _r5_canonical_compose

VOCAB = load_naming_vocab()
SETS = r5_vocab_sets(VOCAB)
_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_EMPTY = MemberEvidence()


def _ev(symbols: list[str] = (), path_segs: list[str] = (),
        manifests: list[str] = ()) -> MemberEvidence:
    ev = MemberEvidence()
    for s in symbols:
        ev.add_symbol(s)
    for seg in path_segs:
        ev.add_path_seg(seg)
    for m in manifests:
        ev.add_manifest(m)
    return ev


def _pf(slug: str, display: str, paths: list[str] | None = None) -> Feature:
    return Feature(
        name=slug, display_name=display, layer="product",
        paths=paths or [], authors=["a"], total_commits=1, bug_fixes=0,
        bug_fix_ratio=0.0, last_modified=_NOW, health_score=100.0,
    )


def _flow(name: str, paths: list[str], symbols: list[str] = ()) -> Flow:
    return Flow(
        name=name, paths=paths, authors=["a"], total_commits=1,
        bug_fixes=0, bug_fix_ratio=0.0, last_modified=_NOW,
        health_score=100.0,
        flow_symbol_attributions=[
            FlowSymbolAttribution(
                file=paths[0], symbol=s, line_start=1, line_end=2,
                role="called")
            for s in symbols
        ],
    )


def _uf(uid: str, name: str, pfid: str, members: list[str],
        resource: str = "", confidence: str = "high") -> UserFlow:
    uf = UserFlow(
        id=uid, name=name, resource=resource or pfid, domain=None,
        product_feature_id=pfid, intent="manage",
        member_flow_ids=members, member_count=len(members),
    )
    uf.name_confidence = confidence  # type: ignore[assignment]
    return uf


def _run(pfs: list[Feature], ufs: list[UserFlow], flows: list[Flow],
         repo_root: str | None = None) -> dict:
    return run_naming_contract(
        pfs, ufs, flows, keeper_on=False,
        product_strings=None, routes_index=None,
        uf_authored_names={}, labeler=None, verifier=None,
        repo_root=repo_root,
    )


# ── split-core: exhibit targets ─────────────────────────────────────────


def test_pagelayouts_split_from_separator_path_runs() -> None:
    # twenty 'Manage pagelayouts' — maintainer spells page-layouts in paths.
    repo = _ev(path_segs=["page-layouts", "page-layouts", "widgets"])
    healed, srcs = derive_split_display(
        "Manage pagelayouts", _EMPTY, repo, SETS)
    assert healed == "Manage page layouts"
    assert srcs and srcs[0].startswith("split:")


def test_signin_signup_split_verb_led_camel() -> None:
    # documenso — camel-only evidence, but the glue is verb-led ('sign').
    repo = _ev(symbols=["SignIn", "signIn", "SignUp", "signUpHandler"])
    assert derive_split_display("Signin", _EMPTY, repo, SETS)[0] == "Sign in"
    assert derive_split_display("Signup", _EMPTY, repo, SETS)[0] == "Sign up"
    assert derive_split_display(
        "Manage signup", _EMPTY, repo, SETS)[0] == "Manage sign up"


def test_blobstorage_family_splits() -> None:
    # langfuse ×3 — separator-spelled path segs + camel symbols agree.
    repo = _ev(symbols=["BlobStorageIntegration", "getBlobStorage"],
               path_segs=["blob-storage", "blob-storage"])
    for src, want in [
        ("Blobstorage", "Blob storage"),
        ("Run blobstorage", "Run blob storage"),
        ("Manage blobstorage integration", "Manage blob storage integration"),
    ]:
        assert derive_split_display(src, _EMPTY, repo, SETS)[0] == want


def test_api_keys_snake_split() -> None:
    # 'Connect api_keys' census class — explicit snake boundary splits with
    # no evidence gate; casing polish is the caller's (YAML) jurisdiction.
    healed, srcs = derive_split_display(
        "Connect api_keys", _EMPTY, _EMPTY, SETS)
    assert healed == "Connect api keys"
    assert srcs == ["snake"]


# ── split-core: anti-cases (the separator-ratio rule) ───────────────────


def test_anticase_deepseek_brand_camel_not_split() -> None:
    # Camel-only evidence ('deepSeek' x3), non-verb lead — brand stays.
    repo = _ev(symbols=["deepSeekBlock", "DeepSeekLogo", "deepSeekApi"],
               path_segs=["deepseek", "deepseek", "deepseek"])
    healed, srcs = derive_split_display(
        "Configure Deepseek Block", _EMPTY, repo, SETS)
    assert healed == "Configure Deepseek Block"
    assert srcs == []


def test_anticase_hitpay_qrcode_brand_camel_not_split() -> None:
    repo = _ev(symbols=["HitPay", "hitPayProvider", "QrCode", "QrCodeBlock"])
    assert derive_split_display("Hitpay", _EMPTY, repo, SETS)[0] == "Hitpay"
    assert derive_split_display(
        "Qrcode Block", _EMPTY, repo, SETS)[0] == "Qrcode Block"


def test_anticase_run_vote_floor_single_vote_never_splits() -> None:
    # ONE separator-spelled occurrence is not corroboration (run-vote >= 2).
    repo = _ev(path_segs=["house-keeping"])
    healed, _ = derive_split_display(
        "Run the housekeeping job now quickly", _EMPTY, repo, SETS)
    assert "house keeping" not in healed


def test_anticase_const_guard_allcaps_never_votes() -> None:
    # CONST-GUARD: ALL-CAPS constant identifiers vote for nothing — neither
    # runs nor symbol-singles (the dpa={Dpa:46, dpa:21, DPA:2} refutation).
    repo = _ev(symbols=["PAGE_LAYOUTS_KEY", "PAGE_LAYOUTS_DEFAULT"])
    healed, srcs = derive_split_display(
        "Manage pagelayouts", _EMPTY, repo, SETS)
    assert healed == "Manage pagelayouts"
    assert srcs == []
    assert repo.sym_singles("page") == 0
    assert repo.runs.get("pagelayouts") is None


def test_anticase_displayed_constant_never_respelled() -> None:
    # A displayed ALL-CAPS constant is never snake-split (directus class).
    healed, srcs = derive_split_display(
        "when CACHE_AUTO_PURGE is true", _EMPTY, _EMPTY, SETS)
    assert healed == "when CACHE_AUTO_PURGE is true"
    assert srcs == []


def test_anticase_mixed_snake_never_mutilated() -> None:
    # teable 'DeleteComputed_B' — mixed-case snake parts stay verbatim.
    for name in ("DeleteComputed_B", "DeleteOutgoing_ManyOne_B"):
        assert derive_split_display(name, _EMPTY, _EMPTY, SETS)[0] == name


def test_anticase_sym_singles_block_split() -> None:
    # A token the repo spells as a standalone symbol word is a real word,
    # not glue ('Signing' class) — even with separator runs present.
    repo = _ev(symbols=["signing", "SigningOrder"],
               path_segs=["sign-ing", "sign-ing"])
    assert derive_split_display("Signing", _EMPTY, repo, SETS)[0] == "Signing"


def test_anticase_short_words_and_camel_boundary() -> None:
    # Runs with a 1-char word are rejected (the langfuse 'Eval'+'S' run),
    # and a run only matches on exact folded-boundary alignment.
    repo = MemberEvidence()
    repo.runs["evals"][("Eval", "S")] = 5
    repo.run_src["evals"] = "symbol"
    assert derive_split_display("Evals", _EMPTY, repo, SETS)[0] == "Evals"
    repo2 = _ev(symbols=["AuthOrg", "AuthOrgMember"])
    # fold('Auth'+'Org') == 'authorg' != 'author' — no mid-camel cut.
    assert derive_split_display("Author", _EMPTY, repo2, SETS)[0] == "Author"


def test_no_acronym_by_symbols_rung() -> None:
    # REFUTED rung must not exist: dominant styled symbol forms never
    # re-case a display ('Send cron' stays; 'Manage dev tools' stays).
    repo = _ev(symbols=["CRON", "CRON", "CRON", "DEV", "DEV", "DEV"])
    assert derive_split_display("Send cron", _EMPTY, repo, SETS)[0] == "Send cron"
    assert derive_split_display(
        "Manage dev tools", _EMPTY, repo, SETS)[0] == "Manage dev tools"


# ── plural rung ─────────────────────────────────────────────────────────


def test_manage_account_setting_pluralizes_on_3x_evidence() -> None:
    # twenty UF-041 — members spell 'settings' 15x; the anchor's own
    # singular seed is excluded by construction (never added to the pool).
    ev = _ev(path_segs=["settings"] * 15)
    assert plural_rename(
        "Manage account setting", ev, SETS) == "Manage account settings"


def test_plural_ratio_below_3x_keeps() -> None:
    ev = _ev(path_segs=["settings"] * 4 + ["setting"] * 2)
    assert plural_rename("Manage account setting", ev, SETS) is None


def test_plural_excludes_anchor_source_segment() -> None:
    # build_board_evidence must NOT seed uf.resource into the local pool —
    # the anchor's singular spelling never vetoes the members' plural.
    fl = _flow("f1", ["src/settings/a.ts", "src/settings/b.ts"])
    uf = _uf("UF-1", "Manage account setting", "settings", ["f1"],
             resource="account-setting")
    _, uf_ev, _ = build_board_evidence([uf], [], {"f1": fl}, None)
    ev = uf_ev["UF-1"]
    assert ev.local_total("setting") == 0  # anchor seed excluded
    assert ev.local_total("settings") >= 2


def test_anticase_write_lead_never_pluralized() -> None:
    ev = _ev(path_segs=["documents"] * 9)
    assert plural_rename("Create document", ev, SETS) is None


def test_anticase_proper_noun_phrase_never_pluralized() -> None:
    # 'Configure Deepseek Block' — the members' blocks/ package segs must
    # not pluralize the Title-cased product noun.
    ev = _ev(path_segs=["blocks"] * 9)
    assert plural_rename("Configure Deepseek Block", ev, SETS) is None


def test_anticase_article_grammar_guard() -> None:
    ev = _ev(path_segs=["partners"] * 9)
    assert plural_rename("Register and log in as a partner", ev, SETS) is None
    ev2 = _ev(path_segs=["accounts"] * 9)
    assert plural_rename("Register and log in to account", ev2, SETS) is None


# ── conf-drop lane (unresolved dir-token) ───────────────────────────────


def _dirtok_repo() -> MemberEvidence:
    return _ev(
        symbols=["signing", "SigningCard", "notificationsList"],
        path_segs=["htmltopdf", "htmltopdf", "bgtasks", "bgtasks",
                   "signing", "notifications", "notifications"],
    )


def test_htmltopdf_bgtasks_unresolved() -> None:
    repo = _dirtok_repo()
    assert unresolved_dir_token("Htmltopdf", repo, SETS) == "Htmltopdf"
    assert unresolved_dir_token("Bgtasks", repo, SETS) == "Bgtasks"


def test_anticase_signing_resolved_by_symbol_life() -> None:
    assert unresolved_dir_token("Signing", _dirtok_repo(), SETS) is None


def test_anticase_manage_notifications_keeps() -> None:
    # 'notifications' lives as a symbol word — resolved, never capped.
    assert unresolved_dir_token(
        "Manage notifications", _dirtok_repo(), SETS) is None


def test_anticase_prose_word_without_dir_never_capped() -> None:
    # An authored word that names no directory is prose, not a dir-token.
    repo = _ev(path_segs=["api"])
    assert unresolved_dir_token("Lifecycle", repo, SETS) is None


def test_anticase_multiword_prose_out_of_shape() -> None:
    # The lane is SHAPE-BOUNDED to the census classes — free tokens inside
    # authored prose names are never judged.
    repo = _ev(path_segs=["frontend", "frontend"])
    assert unresolved_dir_token(
        "Realtime token streaming to frontend", repo, SETS) is None


def test_anticase_brand_vocab_resolved() -> None:
    # A YAML-resolved brand/acronym is already handled by casing polish.
    repo = _ev(path_segs=["posthog", "posthog"])
    assert unresolved_dir_token("Posthog", repo, SETS) is None


def test_conf_drop_caps_and_stamps_via_law(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    uf = _uf("UF-1", "Bgtasks", "bgtasks", [], confidence="high")
    tele: dict = {}
    _apply_r5_confidence_caps(
        [uf], tele, rungs_on=True, repo_ev=_dirtok_repo(), sets=SETS,
        flow_by_id={}, repo_root=None)
    assert uf.name == "Bgtasks"                      # name KEPT
    assert uf.name_confidence == "medium"            # confidence dropped
    assert "shape:unresolved-dir-token" in (uf.name_evidence or [])
    assert tele.get("uf_dirtoken_capped") == 1


# ── brand-echo rung (R5-5-ext) ──────────────────────────────────────────

_TWENTY = "/repo/twenty"


def test_connect_twenty_sdk_demoted_any_member_count() -> None:
    # exact-template + own member paths + brand token — ANY member count
    # (the 1-2m cap was refuted by probe r55: 8-member rows demote too).
    for n_members in (1, 8):
        paths = [f"packages/twenty-sdk/src/f{i}.ts" for i in range(n_members)]
        assert brand_echo_pkg(
            "Connect twenty SDK", paths, _TWENTY, SETS) == "twenty-sdk"
    assert brand_echo_pkg(
        "Send twenty emails", ["packages/twenty-emails/src/i.tsx"],
        _TWENTY, SETS) == "twenty-emails"


def test_anticase_connect_twenty_to_zapier_keeps() -> None:
    # Not the exact template — the remainder is a sentence, not the pkg.
    assert brand_echo_pkg(
        "Connect Twenty to Zapier",
        ["packages/twenty-zapier/src/app.ts"], _TWENTY, SETS) is None


def test_anticase_no_brand_token_keeps() -> None:
    # 'Manage notifications' on novu — pkg has no repo-slug token.
    assert brand_echo_pkg(
        "Manage notifications",
        ["packages/notifications/src/a.ts"], "/repo/novu", SETS) is None


def test_anticase_healthy_product_word_pkgs_keep_high() -> None:
    for name, pkg, root in [
        ("Manage features", "features", "/repo/cal.com"),
        ("Manage documents", "documents", "/repo/midday"),
        ("Run billing", "billing", "/repo/typebot"),
    ]:
        assert brand_echo_pkg(
            name, [f"packages/{pkg}/src/x.ts"], root, SETS) is None


def test_anticase_token_equality_not_substring() -> None:
    # openstatus 'Browse status page' — 'status' is not the 'openstatus'
    # token; substring matching would wrongly demote it.
    assert brand_echo_pkg(
        "Browse status page", ["apps/status-page/src/p.tsx"],
        "/repo/openstatus", SETS) is None


def test_brand_echo_caps_via_law(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    fl = _flow("f1", ["packages/hoppscotch-desktop/src/a.ts"])
    uf = _uf("UF-1", "Manage hoppscotch desktop", "desktop", ["f1"],
             confidence="high")
    tele: dict = {}
    _apply_r5_confidence_caps(
        [uf], tele, rungs_on=True, repo_ev=MemberEvidence(), sets=SETS,
        flow_by_id={"f1": fl}, repo_root="/repo/hoppscotch")
    assert uf.name_confidence == "medium"
    assert "shape:brand-echo" in (uf.name_evidence or [])
    assert tele.get("uf_brand_echo_capped") == 1


# ── R5-4 compose canonicalizer (probe r54 canon: 7 heals, 0 anti-cases) ─


@pytest.mark.parametrize("src,want,rule", [
    ("Browse organization ( List)", "Browse organization (List)",
     "joint-restrip"),
    ("Upload File ( upload)", "Upload File (upload)", "joint-restrip"),
    ("Manage users (Auth (Interfaces))", "Manage users (Auth Interfaces)",
     "joint-restrip"),
    ("Manage poll (optional space) (legacy)",
     "Manage poll (optional space legacy)", "joint-restrip"),
    ("Edit (Monitors)", "Monitors — Edit", "noun-leads-inversion"),
    ("New (Optional Space)", "Optional Space — New", "noun-leads-inversion"),
    ("New (Studio)", "Studio — New", "noun-leads-inversion"),
    ("New (Webhooks)", "Webhooks — New", "noun-leads-inversion"),
])
def test_compose_heals(src: str, want: str, rule: str) -> None:
    got, fired = canonicalize_compose(src, SETS)
    assert (got, fired) == (want, rule)


def test_compose_optional_catch_all_residue() -> None:
    got, rule = canonicalize_compose("Manage poll ([[...space]])", SETS)
    assert got == "Manage poll (optional space)"
    assert rule == "residue-optional-catch-all"


def test_compose_residue_drop_and_orphan_promote() -> None:
    assert canonicalize_compose("Manage poll ( )", SETS) == (
        "Manage poll", "residue-drop")
    assert canonicalize_compose("(checkout)", SETS) == (
        "Checkout", "orphan-qual-promote")


@pytest.mark.parametrize("healthy", [
    "Manage links (API keys)",
    "App Store — Google Meet",
    "Analyze cohort retention",
    "Upload File (upload)",
    "Stripe (App Store)",
    "List (View data)",          # verb-led qualifier never inverts
    "Contact Creation Manager",
])
def test_compose_identity_on_healthy(healthy: str) -> None:
    assert canonicalize_compose(healthy, SETS) == (healthy, None)


def test_mint_compose_site_flag_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    # stage_6_86 site 3/3 — OFF passthrough, ON canonical.
    # MECHANICAL flip migration (2026-07-21 pack №2, KEY_SCHEMA 33):
    # the OFF baseline is pinned with an explicit =0, not left unset.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "0")
    assert _r5_canonical_compose("Edit (Monitors)") == "Edit (Monitors)"
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    assert _r5_canonical_compose("Edit (Monitors)") == "Monitors — Edit"
    assert _r5_canonical_compose("Claroty (Iot Ot)") == "Claroty (Iot Ot)"


# ── integration through run_naming_contract ─────────────────────────────


def _split_world() -> tuple[list[Feature], list[UserFlow], list[Flow]]:
    flows = [
        _flow("layouts-flow",
              ["src/modules/page-layouts/a.tsx",
               "src/modules/page-layouts/b.tsx"]),
    ]
    pfs = [
        _pf("signin", "Signin",
            paths=["apps/web/signin/page.tsx"]),
        _pf("htmltopdf", "Htmltopdf",
            paths=["apps/web/htmltopdf/gen.ts", "apps/web/htmltopdf/u.ts"]),
    ]
    pfs[0].symbol_attributions = [
        FlowSymbolAttribution(
            file="apps/web/signin/page.tsx", symbol=s, line_start=1,
            line_end=2, role="called")
        for s in ("SignIn", "signInAction")
    ]
    ufs = [_uf("UF-001", "Manage pagelayouts", "signin", ["layouts-flow"])]
    return pfs, ufs, flows


def test_run_contract_on_applies_split_and_confdrop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    monkeypatch.setenv("FAULTLINE_NAME_EVIDENCE_RUNGS", "1")
    pfs, ufs, flows = _split_world()
    tele = _run(pfs, ufs, flows)
    assert ufs[0].name == "Manage page layouts"
    disp = {p.name: p.display_name for p in pfs}
    assert disp["signin"] == "Sign in"
    assert disp["htmltopdf"] == "Htmltopdf"          # name KEPT
    by_slug = {p.name: p for p in pfs}
    assert by_slug["htmltopdf"].name_confidence == "medium"
    assert "shape:unresolved-dir-token" in (
        by_slug["htmltopdf"].name_evidence or [])
    assert by_slug["signin"].name_confidence == "high"
    assert tele.get("r5_split_renamed", 0) >= 1
    assert tele.get("r5_pf_split_renamed", 0) >= 1
    assert tele.get("r5_pf_dirtoken_capped", 0) == 1


def test_run_contract_off_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MECHANICAL flip migration (2026-07-21 pack №2, KEY_SCHEMA 33):
    # the OFF baseline is pinned with an explicit =0, not left unset.
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "0")
    pfs, ufs, flows = _split_world()
    tele = _run(pfs, ufs, flows)
    assert ufs[0].name == "Manage pagelayouts"
    disp = {p.name: p.display_name for p in pfs}
    assert disp["signin"] == "Signin"
    assert disp["htmltopdf"] == "Htmltopdf"
    by_slug = {p.name: p for p in pfs}
    assert by_slug["htmltopdf"].name_confidence == "high"
    assert by_slug["htmltopdf"].name_evidence is None
    for key in ("r5_split_renamed", "r5_pf_split_renamed",
                "r5_pf_dirtoken_capped", "r5_plural_renamed",
                "r5_compose_canonicalized"):
        assert key not in tele


def test_rescore_idempotent_over_healed_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The 6.7e rescore seam re-runs the same laws — a healed board must be
    # a fixed point (names + confidences unchanged on the second pass).
    monkeypatch.setenv(NAMING_WAVE_R5_ENV, "1")
    pfs, ufs, flows = _split_world()
    _run(pfs, ufs, flows)
    first = (
        [u.name for u in ufs],
        [(p.display_name, p.name_confidence) for p in pfs],
    )
    _run(pfs, ufs, flows)
    second = (
        [u.name for u in ufs],
        [(p.display_name, p.name_confidence) for p in pfs],
    )
    assert first == second


# ── Feature model: medium tier + evidence serialization ────────────────


def test_pf_medium_and_evidence_serialization() -> None:
    pf = _pf("htmltopdf", "Htmltopdf")
    assert pf.name_confidence == "high"
    dump = pf.model_dump()
    assert "name_evidence" not in dump            # None is OMITTED
    pf.name_confidence = "medium"
    pf.name_evidence = ["shape:unresolved-dir-token"]
    dump2 = pf.model_dump()
    assert dump2["name_confidence"] == "medium"
    assert dump2["name_evidence"] == ["shape:unresolved-dir-token"]
    # round-trip rehydration
    back = Feature.model_validate(dump2)
    assert back.name_confidence == "medium"
    assert back.name_evidence == ["shape:unresolved-dir-token"]
