"""Stage 6.97 — deterministic feature-level LOC.

Operator invariant (validate_scan.py I2): every dev feature with >=1
non-empty non-test owned file must emit ``loc > 0``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.stage_6_97_feature_loc import (
    STAGE_6_97_ENV_FLAG,
    apply_feature_loc,
    count_file_loc,
    stage_6_97_enabled,
)


def _feature(name: str, paths: list[str], **kw) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=[],
        total_commits=0,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        health_score=100.0,
        **kw,
    )


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "app/auth/login.ts", "// comment\nconst a = 1;\n\nexport {a};\n")
    _write(root, "app/auth/login.test.ts", "test('x', () => {});\n" * 50)
    _write(root, "app/i18n/messages.json", '{\n "hello": "world"\n}\n')
    _write(root, "app/empty.ts", "")
    _write(root, "pkg/db/queries_generated.go", "package db\n" * 100)
    _write(root, "pnpm-lock.yaml", "lockfileVersion: 9\n" * 100)
    _write(root, "assets/bundle.min.js", "var a=1;" * 200)
    (root / "assets").mkdir(exist_ok=True)
    (root / "assets/logo.png").write_bytes(b"\x89PNG\x00\x00binary")
    _write(root, "app/billing/invoice.ts", "export const x = 1;\nexport const y = 2;\n")
    _write(root, "app/billing/shared.ts", "export const s = 1;\n")
    return root


# ── per-file counting ───────────────────────────────────────────────────


def test_counts_executable_lines_not_comments(tmp_path):
    root = _repo(tmp_path)
    n = count_file_loc(root / "app/auth/login.ts", "app/auth/login.ts")
    assert n == 2  # comment + blank excluded


def test_test_generated_lockfile_minified_binary_excluded(tmp_path):
    root = _repo(tmp_path)
    for rel in (
        "app/auth/login.test.ts",
        "pkg/db/queries_generated.go",
        "pnpm-lock.yaml",
        "assets/bundle.min.js",
        "assets/logo.png",
        "app/empty.ts",
        "does/not/exist.ts",
    ):
        assert count_file_loc(root / rel, rel) == 0, rel


def test_unknown_text_ext_counts_nonblank_lines(tmp_path):
    # config-as-product guarantee: .json/.yaml features still get loc>0
    root = _repo(tmp_path)
    n = count_file_loc(root / "app/i18n/messages.json", "app/i18n/messages.json")
    assert n == 3


# ── feature-level emission ──────────────────────────────────────────────


def test_flowless_feature_with_paths_gets_positive_loc(tmp_path):
    root = _repo(tmp_path)
    feat = _feature("auth", ["app/auth/login.ts", "app/auth/login.test.ts"])
    assert feat.flows == []  # flowless — the I2 bug class
    telemetry = apply_feature_loc([feat], [], root)
    assert feat.loc == 2  # test file listed but NOT counted
    assert feat.paths == ["app/auth/login.ts", "app/auth/login.test.ts"]
    assert telemetry["features_with_loc"] == 1
    assert telemetry["features_zero_loc_with_paths"] == 0


def test_feature_with_only_excluded_paths_gets_zero(tmp_path):
    root = _repo(tmp_path)
    feat = _feature("test-only", ["app/auth/login.test.ts", "assets/logo.png"])
    telemetry = apply_feature_loc([feat], [], root)
    assert feat.loc == 0
    assert telemetry["features_zero_loc_with_paths"] == 1


def test_directory_path_counts_recursively(tmp_path):
    root = _repo(tmp_path)
    feat = _feature("auth-dir", ["app/auth"])
    apply_feature_loc([feat], [], root)
    assert feat.loc == 2  # login.ts only; the test twin is excluded


def test_missing_paths_are_silently_zero(tmp_path):
    root = _repo(tmp_path)
    feat = _feature("ghost", ["app/removed-in-refactor.ts", "app/auth/login.ts"])
    apply_feature_loc([feat], [], root)
    assert feat.loc == 2


# ── product-feature rollup ──────────────────────────────────────────────


def test_pf_rollup_dedups_shared_files(tmp_path):
    root = _repo(tmp_path)
    d1 = _feature(
        "billing-invoices",
        ["app/billing/invoice.ts", "app/billing/shared.ts"],
        product_feature_id="billing",
    )
    d2 = _feature(
        "billing-shared",
        ["app/billing/shared.ts"],
        product_feature_id="billing",
    )
    pf = _feature("billing", [], layer="product")
    apply_feature_loc([d1, d2], [pf], root)
    assert d1.loc == 3
    assert d2.loc == 0  # W3.1 D11: pure-sharer owns nothing (no floor)
    assert d2.loc_shared == 1
    assert pf.loc == 3  # shared.ts counted ONCE, not d1.loc + d2.loc == 4


def test_pf_without_members_falls_back_to_own_paths(tmp_path):
    root = _repo(tmp_path)
    pf = _feature("standalone", ["app/billing/invoice.ts"], layer="product")
    apply_feature_loc([], [pf], root)
    assert pf.loc == 2


# ── stage plumbing ──────────────────────────────────────────────────────


# ── OWNED vs SHARED split (loc-truth fix 2026-07-05) ────────────────────


def test_shared_file_counted_once_at_primary_owner(tmp_path):
    # invoice.ts is sole-owned by d1; shared.ts is claimed by both. d1 has
    # more sibling files in app/billing → d1 is the primary owner of the
    # shared file; d2 (pure sharer) records it as loc_shared, not loc.
    root = _repo(tmp_path)
    d1 = _feature("billing-invoices",
                  ["app/billing/invoice.ts", "app/billing/shared.ts"])
    d2 = _feature("billing-consumer", ["app/billing/shared.ts"])
    apply_feature_loc([d1, d2], [], root)
    assert d1.loc == 3            # invoice(2) + primary shared(1)
    assert d1.loc_shared == 0
    # d2 owns nothing exclusively; the shared file's lines land in loc_shared
    assert d2.loc_shared == 1
    # W3.1 D11: NO owned floor for pure-sharers — the old
    # `owned = max(files)` floor double-counted the file into the W2b
    # platform-lane sums (pretalx I13 trip, 88,903 > repo 88,784).
    # loc_shared > 0 is what keeps I2 honest ("has code" via the
    # shared channel).
    assert d2.loc == 0


def test_pure_sharer_lane_conservation_no_double_count(tmp_path):
    """W3.1 D11 regression (pretalx I13): one urls.py primary-claimed by
    EIGHT devs must count ONCE across the owned ledger — the seven
    pure-sharer error-page devs report 0 owned LOC (shared only), so PF
    sums + platform-lane per-dev sums stay inside repo_loc."""
    root = tmp_path / "repo"
    _write(root, "src/app/urls.py", "u = 1\n" * 39)
    _write(root, "src/app/views.py", "v = 1\n" * 10)
    orga = _feature("orga", ["src/app/urls.py", "src/app/views.py"])
    orga.product_feature_id = "orga"
    claimants = []
    for name in ("400", "403", "404", "500", "redirect", "debug", "root"):
        d = _feature(name, ["src/app/urls.py"])
        d.product_feature_id = None
        claimants.append(d)
    pf = _feature("orga", [])
    apply_feature_loc([orga, *claimants], [pf], root)
    assert orga.loc == 49          # urls(39, primary by dir-count) + views(10)
    for d in claimants:
        assert d.loc == 0, d.name  # pure sharers own NOTHING
        assert d.loc_shared == 39  # the shared channel keeps them code-ful
    # the lane-aware conservation sum: PF owned + per-dev lane loc
    lane_sum = sum(d.loc or 0 for d in claimants)
    assert (pf.loc or 0) + lane_sum == 49  # counted once, not 8x


def test_primary_owner_tiebreak_by_flow_count_then_slug(tmp_path):
    # Two features share ONE file and neither has sibling files in that dir
    # (equal dir-count 1). Tiebreak 2 = flow count → the flowful one wins.
    root = _repo(tmp_path)
    hi = _feature("zeta", ["app/billing/shared.ts"])
    hi.flows.append(object())  # bump flow count (list mutation is unvalidated)
    lo = _feature("alpha", ["app/billing/shared.ts"])
    apply_feature_loc([hi, lo], [], root)
    assert hi.loc == 1            # flowful → primary
    assert lo.loc_shared == 1
    # Now with equal flows, the smaller slug wins (deterministic).
    a = _feature("alpha", ["app/billing/shared.ts"])
    z = _feature("zeta", ["app/billing/shared.ts"])
    apply_feature_loc([a, z], [], root)
    assert a.loc == 1 and a.loc_shared == 0
    assert z.loc_shared == 1


def test_dirwalk_skips_build_dirs_and_nonsource_ext(tmp_path):
    # A bare directory path must NOT pull build output (dist/) or data
    # blobs (.json) — the 13x inflation vector.
    root = tmp_path / "repo"
    _write(root, "frontend/src/app.tsx", "export const A = 1;\n")
    _write(root, "frontend/dist/bundle.js", "var x=1;\n" * 5000)  # build output
    _write(root, "frontend/coverage/lcov.info", "DA:1\n" * 5000)  # coverage
    _write(root, "frontend/data/huge.json", '{"k":1}\n' * 5000)   # data blob
    feat = _feature("frontend-app", ["frontend"])
    apply_feature_loc([feat], [], root)
    assert feat.loc == 1  # ONLY frontend/src/app.tsx counted


def test_explicit_json_file_still_counts(tmp_path):
    # config-as-product: an EXPLICITLY listed .json file bypasses the
    # source-ext dir-walk gate.
    root = _repo(tmp_path)
    feat = _feature("i18n", ["app/i18n/messages.json"])
    apply_feature_loc([feat], [], root)
    assert feat.loc == 3


def test_member_files_loc_populated(tmp_path):
    root = _repo(tmp_path)
    feat = _feature(
        "auth",
        ["app/auth/login.ts"],
        member_files=[
            MemberFile(path="app/auth/login.ts", role="anchor", confidence=1.0),
            MemberFile(path="app/auth/login.test.ts", role="closure", confidence=0.5),
        ],
    )
    apply_feature_loc([feat], [], root)
    assert feat.member_files[0].loc == 2   # login.ts executable lines
    assert feat.member_files[1].loc == 0   # test file → 0


def test_loc_accounting_sanity_meta(tmp_path):
    root = _repo(tmp_path)
    d1 = _feature("billing-invoices",
                  ["app/billing/invoice.ts", "app/billing/shared.ts"],
                  product_feature_id="billing")
    d2 = _feature("billing-consumer", ["app/billing/shared.ts"],
                  product_feature_id="billing")
    pf = _feature("billing", [], layer="product")
    telemetry = apply_feature_loc([d1, d2], [pf], root)
    acct = telemetry["loc_accounting"]
    assert acct["repo_loc"] == 3          # invoice(2) + shared(1), each once
    assert acct["sum_pf_owned"] == 3
    # sanity invariant (validator I13)
    assert acct["sum_pf_owned"] <= acct["repo_loc"]


def test_product_layer_dup_in_features_mirrors_rollup(tmp_path):
    # A product feature that ALSO appears in features[] (the two-layer
    # dup) must be EXCLUDED from ownership and mirror its PF rollup.
    root = _repo(tmp_path)
    dev = _feature("billing-invoices", ["app/billing/invoice.ts"],
                   product_feature_id="billing")
    pf_dup = _feature("billing", ["app/billing/invoice.ts"], layer="product",
                      product_feature_id=None)
    telemetry = apply_feature_loc([dev, pf_dup], [pf_dup], root)
    # invoice.ts counted ONCE — repo_loc is 2, not 4
    assert telemetry["loc_accounting"]["repo_loc"] == 2
    assert dev.loc == 2
    assert pf_dup.loc == 2  # mirrors the rollup, does not re-own


def test_repo_root_path_contributes_zero_loc(tmp_path):
    # papermark corruption: a feature carrying the repo-root marker "."
    # (or "" / "./") must NOT count the entire repo.
    root = _repo(tmp_path)
    for marker in (".", "", "./", ".."):
        feat = _feature(f"root-{marker or 'empty'}", [marker, "app/auth/login.ts"])
        apply_feature_loc([feat], [], root)
        assert feat.loc == 2, marker  # only login.ts, NOT the whole repo


def test_root_anchor_member_file_contributes_zero(tmp_path):
    # {"path": ".", "role": "anchor"} is a structural marker — 0 loc, and
    # it must not inflate the feature's owned loc either.
    root = _repo(tmp_path)
    feat = _feature(
        "auth",
        ["app/auth/login.ts"],
        member_files=[
            MemberFile(path=".", role="anchor", confidence=1.0),
            MemberFile(path="app/auth/login.ts", role="anchor", confidence=1.0),
        ],
    )
    apply_feature_loc([feat], [], root)
    assert feat.loc == 2  # login.ts only
    assert feat.member_files[0].loc == 0  # "." root anchor → 0
    assert feat.member_files[1].loc == 2


def test_loc_shared_field_defaults_none():
    feat = _feature("legacy", ["a.ts"])
    assert feat.loc_shared is None
    assert feat.model_dump()["loc_shared"] is None


def test_env_kill_switch(monkeypatch):
    monkeypatch.delenv(STAGE_6_97_ENV_FLAG, raising=False)
    assert stage_6_97_enabled()
    monkeypatch.setenv(STAGE_6_97_ENV_FLAG, "0")
    assert not stage_6_97_enabled()


def test_loc_field_defaults_none_for_old_scans():
    feat = _feature("legacy", ["a.ts"])
    assert feat.loc is None  # pre-stage scans rehydrate unchanged
    dumped = feat.model_dump()
    assert dumped["loc"] is None
