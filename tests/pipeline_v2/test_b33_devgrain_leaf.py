"""B33 v2 — post-UF devgrain-leaf PF demote (Stage 6.987, devgrain_demote.py).

A ``route:``/``fdir:``-anchored PF whose leaf names a plumbing screen or a
journey STEP demotes AFTER the journey layer — iff its FINAL journey profile
is micro (≤2 UFs, every member_count ≤3). The v1 mint-time bar violated
journey conservation (twenty ``Onboarding``: 9 substantive wizard journeys
silently dropped, 66→58 UFs) — UFs do not exist at 6.86, so the discriminator
between disease and non-target is the post-journey-layer UF profile.

Ripe exhibits distilled here (wave16/wave-2, engine @2459f81):
  * papermark ``Welcome`` (route:welcome) — 1 UF mc1;
  * cal.com ``Getting Started`` (fdir:apps/web/modules/getting-started) — 1 UF mc1;
  * novu ``Access Denied`` (route:access-denied-page) — 2 UFs mc1+mc3;
  * novu ``Redirect To Legacy Studio Auth`` (route:redirect-to-legacy-studio-auth) — 1 UF mc1.

Anti-cases that MUST abstain (rich journey profiles — conservation):
twenty ``Onboarding`` (9 UFs mc2-3), typebot ``Onboarding`` (mc11),
supabase ``Logout`` (mc7), cal.com ``Onboarding`` (3 UFs). Nav-declared
leaves (kan/midday ``Onboarding``, novu ``Error``) skip even with a micro
profile — the author's IA word.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature, MemberFile, UserFlow
from faultline.pipeline_v2.devgrain_demote import (
    FDIR_DEVGRAIN_GATE_ENV,
    fdir_devgrain_gate_enabled,
    is_journey_step_leaf,
    journey_step_leaf_tokens,
    run_devgrain_demote,
)
from faultline.pipeline_v2.spine_anchors import load_spine_vocab

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ── Fixtures ─────────────────────────────────────────────────────────────


def _tokens() -> dict[str, frozenset[str]]:
    return journey_step_leaf_tokens(load_spine_vocab())


def dev(name: str, paths: list[str], pfid: str | None) -> Feature:
    return Feature(
        name=name, paths=list(paths),
        member_files=[
            MemberFile(path=p, role="anchor", confidence=1.0, primary=True)
            for p in paths
        ],
        flows=[], product_feature_id=pfid,
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )


def pf(name: str, anchor_id: str, paths: list[str] | None = None) -> Feature:
    row = Feature(
        name=name, paths=list(paths or []),
        authors=["a"], total_commits=1, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=_NOW, health_score=100.0,
    )
    row.layer = "product"
    row.anchor_id = anchor_id
    return row


_UF_SEQ = [0]


def uf(name: str, pfid: str, mc: int) -> UserFlow:
    _UF_SEQ[0] += 1
    return UserFlow(
        id=f"UF-{_UF_SEQ[0]:03d}", name=name, product_feature_id=pfid,
        intent="manage", resource="thing",
        member_flow_ids=[f"f{i}" for i in range(mc)], member_count=mc,
    )


_NAV = frozenset({"dashboard", "documents"})  # readable board nav


def run(devs, pfs, ufs, nav=_NAV):
    return run_devgrain_demote(devs, pfs, ufs, nav_keys=nav)


# ── Flag helper ──────────────────────────────────────────────────────────


def test_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default ON since the 2026-07-12 flip (KEY_SCHEMA v28) — unset reads
    # enabled; explicit off values stay a valid kill-switch forever.
    monkeypatch.delenv(FDIR_DEVGRAIN_GATE_ENV, raising=False)
    assert fdir_devgrain_gate_enabled()
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    assert fdir_devgrain_gate_enabled()
    for off in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, off)
        assert not fdir_devgrain_gate_enabled(), off


def test_inverted_kill_switch_unset_equals_explicit_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flip law: UNSET must behave byte-identically to an explicit ``=1`` —
    same demote decisions, same telemetry, same board mutation."""
    def scene():
        pfs = [pf("welcome", "route:app/welcome")]
        ufs = [uf("welcome-j", "welcome", 1)]
        d = dev("welcome-dev", ["app/welcome/index.tsx"], "welcome")
        return [d], pfs, ufs

    monkeypatch.delenv(FDIR_DEVGRAIN_GATE_ENV, raising=False)
    devs_u, pfs_u, ufs_u = scene()
    tele_u = run(devs_u, pfs_u, ufs_u)
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    devs_e, pfs_e, ufs_e = scene()
    tele_e = run(devs_e, pfs_e, ufs_e)
    assert tele_u == tele_e
    assert tele_u["enabled"] is True and tele_u["demoted"]  # it ACTED
    assert [p.name for p in pfs_u] == [p.name for p in pfs_e] == []
    assert [u.name for u in ufs_u] == [u.name for u in ufs_e] == []
    assert devs_u[0].product_feature_id == devs_e[0].product_feature_id


# ── The 4 ripe exhibits demote (flag ON, nav readable, not declared) ──────


@pytest.mark.parametrize(
    ("slug", "anchor", "profile"),
    [
        ("welcome", "route:app/welcome", [1]),                    # papermark
        ("getting-started",
         "fdir:apps/web/modules/getting-started", [1]),           # cal.com
        ("access-denied-page",
         "route:src/pages/access-denied-page", [1, 3]),           # novu
        ("redirect-to-legacy-studio-auth",
         "route:src/pages/redirect-to-legacy-studio-auth", [1]),  # novu
    ],
)
def test_ripe_exhibits_demote(
    monkeypatch: pytest.MonkeyPatch,
    slug: str, anchor: str, profile: list[int],
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    pfs = [pf(slug, anchor)]
    ufs = [uf(f"{slug}-journey-{i}", slug, mc) for i, mc in enumerate(profile)]
    d = dev(f"{slug}-dev", [f"{anchor.split(':', 1)[1]}/index.tsx"], slug)
    tele = run([d], pfs, ufs)
    assert [row["pf"] for row in tele["demoted"]] == [slug]
    assert pfs == []                      # PF row removed
    assert ufs == []                      # micro-UFs dropped
    assert slug in tele["dropped_ufs"]
    assert len(tele["dropped_ufs"][slug]) == len(profile)


# ── Anti-cases: rich journey profiles ABSTAIN (conservation) ─────────────


def test_twenty_onboarding_nine_journeys_abstains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The v1 lesson: twenty `Onboarding` (route, 9 substantive wizard
    journeys mc2-3) must NOT demote and its UFs must survive
    byte-identically."""
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    pfs = [pf("onboarding", "route:apps/web/pages/onboarding")]
    names = ["Manage create profile", "Manage invite team",
             "Manage sync emails", "Manage connect calendar",
             "Manage pick plan", "Manage import contacts",
             "Manage set signature", "Manage verify domain",
             "Manage finish setup"]
    ufs = [uf(n, "onboarding", 2 + (i % 2)) for i, n in enumerate(names)]
    before = [(u.id, u.name, u.product_feature_id, u.member_count)
              for u in ufs]
    tele = run([dev("onb-dev", ["apps/web/pages/onboarding/a.tsx"],
                    "onboarding")], pfs, ufs)
    assert tele["demoted"] == []
    assert [a["pf"] for a in tele["abstained"]] == ["onboarding"]
    assert tele["abstained"][0]["ufs"] == 9
    assert len(pfs) == 1                  # PF survives
    after = [(u.id, u.name, u.product_feature_id, u.member_count)
             for u in ufs]
    assert after == before                # journeys byte-identical


@pytest.mark.parametrize(
    ("slug", "anchor", "profile", "why"),
    [
        ("onboarding", "route:src/pages/onboarding", [11],
         "typebot Onboarding mc11"),
        ("logout", "route:apps/studio/pages/logout", [7],
         "supabase Logout mc7"),
        ("onboarding", "fdir:apps/web/modules/onboarding", [1, 2, 6],
         "cal.com Onboarding 3 UFs incl mc6"),
    ],
)
def test_rich_profiles_abstain(
    monkeypatch: pytest.MonkeyPatch,
    slug: str, anchor: str, profile: list[int], why: str,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    pfs = [pf(slug, anchor)]
    ufs = [uf(f"{slug}-j{i}", slug, mc) for i, mc in enumerate(profile)]
    n_ufs = len(ufs)
    tele = run([], pfs, ufs)
    assert tele["demoted"] == [], why
    assert [a["pf"] for a in tele["abstained"]] == [slug], why
    assert len(pfs) == 1 and len(ufs) == n_ufs, why


# ── Nav-declared leaves skip even with a micro profile ───────────────────


@pytest.mark.parametrize(
    ("slug", "anchor", "nav_key"),
    [
        # kan / midday Onboarding — nav-declared route leaf.
        ("onboarding", "route:app/onboarding", "onboarding"),
        # novu Error — nav-declared route leaf.
        ("error", "route:src/pages/error", "error"),
        # -page surface form declared by its bare nav href.
        ("access-denied-page", "route:src/pages/access-denied-page",
         "access-denied"),
    ],
)
def test_nav_declared_skips_even_micro(
    monkeypatch: pytest.MonkeyPatch,
    slug: str, anchor: str, nav_key: str,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    pfs = [pf(slug, anchor)]
    ufs = [uf(f"{slug}-j", slug, 1)]      # the ripest possible profile
    tele = run([], pfs, ufs, nav=frozenset({nav_key, "dashboard"}))
    assert tele["demoted"] == []
    assert tele["nav_declared_skipped"] == [slug]
    assert len(pfs) == 1 and len(ufs) == 1


# ── Board abstain: empty nav parse → no action at all ────────────────────


def test_board_abstain_when_nav_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    pfs = [pf("welcome", "route:app/welcome")]
    ufs = [uf("welcome-j", "welcome", 1)]
    tele = run([], pfs, ufs, nav=frozenset())
    assert tele.get("journey_step_leaf_abstained") is True
    assert tele["demoted"] == []
    assert len(pfs) == 1 and len(ufs) == 1


# ── Kill-switch: explicit off → pure no-op (post-flip: unset = ON) ───────


@pytest.mark.parametrize("flag", ["0", "false", "off"])
def test_kill_switch_no_op(
    monkeypatch: pytest.MonkeyPatch, flag: str,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, flag)
    pfs = [pf("welcome", "route:app/welcome")]
    ufs = [uf("welcome-j", "welcome", 1)]
    d = dev("welcome-dev", ["app/welcome/index.tsx"], "welcome")
    tele = run([d], pfs, ufs)
    assert tele["enabled"] is False
    assert len(pfs) == 1 and len(ufs) == 1
    assert d.product_feature_id == "welcome"


# ── Anchor-kind discipline: only route:/fdir: PFs are eligible ───────────


def test_non_route_fdir_anchors_never_touch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    pfs = [pf("welcome", "ws:packages/welcome"),
           pf("callback", "hub:packages/integrations/callback")]
    ufs = [uf("welcome-j", "welcome", 1), uf("callback-j", "callback", 1)]
    tele = run([], pfs, ufs)
    assert tele["eligible"] == 0
    assert tele["demoted"] == []
    assert len(pfs) == 2 and len(ufs) == 2


# ── Dev re-point to the nearest surviving ancestor PF ────────────────────


def test_devs_repoint_to_nearest_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    ancestor = pf("web-app", "ws:apps/web", paths=["apps/web/package.json"])
    coarse = pf("apps", "ws:apps")
    target = pf("getting-started",
                "fdir:apps/web/modules/getting-started",
                paths=["apps/web/modules/getting-started/index.tsx"])
    pfs = [ancestor, coarse, target]
    ufs = [uf("gs-j", "getting-started", 1)]
    d = dev("gs-dev", ["apps/web/modules/getting-started/index.tsx"],
            "getting-started")
    tele = run([d], pfs, ufs)
    # nearest = LONGEST containing anchor prefix: ws:apps/web beats ws:apps.
    assert d.product_feature_id == "web-app"
    assert d.anchor_id == (
        "fold:devgrain-demote->fdir:apps/web/modules/getting-started")
    assert d.shared_reason is None
    assert tele["devs_repointed"] == 1 and tele["devs_unowned"] == 0
    assert tele["demoted"][0]["into"] == "web-app"
    # the demoted PF's paths were conserved onto the target.
    assert "apps/web/modules/getting-started/index.tsx" in ancestor.paths
    assert [p.name for p in pfs] == ["web-app", "apps"]


def test_devs_without_ancestor_stay_l1_unowned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    pfs = [pf("welcome", "route:app/welcome"),
           pf("documents", "route:app/documents")]  # sibling, NOT ancestor
    ufs = [uf("welcome-j", "welcome", 1)]
    d = dev("welcome-dev", ["app/welcome/index.tsx"], "welcome")
    tele = run([d], pfs, ufs)
    # never-minted semantics: L1/unowned, honest shared_reason.
    assert d.product_feature_id is None
    assert d.anchor_id is None
    assert d.shared_reason == "sub_mint_bar_surface"
    assert tele["devs_unowned"] == 1 and tele["devs_repointed"] == 0
    assert tele["demoted"][0]["into"] is None
    assert [p.name for p in pfs] == ["documents"]


# ── UF-drop telemetry lists names ────────────────────────────────────────


def test_uf_drop_telemetry_lists_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    pfs = [pf("access-denied-page", "route:src/pages/access-denied-page")]
    ufs = [uf("Browse & filter denied pages", "access-denied-page", 1),
           uf("pages", "access-denied-page", 3),
           uf("real-journey", "documents", 5)]
    tele = run([], pfs, ufs)
    assert tele["dropped_ufs"]["access-denied-page"] == sorted(
        ["Browse & filter denied pages", "pages"])
    # foreign UFs untouched.
    assert [u.name for u in ufs] == ["real-journey"]


# ── Zero-UF flowless PF is the purest disease profile ────────────────────


def test_zero_uf_profile_demotes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    pfs = [pf("maintenance", "route:apps/studio/pages/maintenance")]
    tele = run([], pfs, [])
    assert [row["pf"] for row in tele["demoted"]] == ["maintenance"]
    assert pfs == []


# ── Determinism: demoted/abstained ordering is name-sorted ───────────────


def test_deterministic_sorted_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FDIR_DEVGRAIN_GATE_ENV, "1")
    pfs = [pf("welcome", "route:app/welcome"),
           pf("callback", "route:app/callback"),
           pf("logout", "route:app/logout")]
    ufs = [uf("welcome-j", "welcome", 1), uf("callback-j", "callback", 1),
           uf("logout-j", "logout", 7)]
    tele = run([], pfs, ufs)
    assert [row["pf"] for row in tele["demoted"]] == ["callback", "welcome"]
    assert [a["pf"] for a in tele["abstained"]] == ["logout"]


# ── Token matcher unit coverage (carried over from v1) ───────────────────


def test_matcher_matches_required_keys() -> None:
    t = _tokens()
    for key in ("welcome", "getting-started", "access-denied-page",
                "redirect-to-legacy-studio-auth", "oauth-callback",
                "aws-marketplace-onboarding", "logout", "maintenance",
                "enter-redirect"):
        assert is_journey_step_leaf(key, t), key


def test_matcher_rejects_non_plumbing_keys() -> None:
    t = _tokens()
    for key in ("edit", "new", "dashboard", "welcome-tour", ""):
        assert not is_journey_step_leaf(key, t), key


# ── YAML block loads, is well-formed and deterministically sorted ────────


def test_yaml_block_loads_and_is_sorted() -> None:
    block = load_spine_vocab().get("journey_step_leaf_tokens")
    assert isinstance(block, dict)
    for sub in ("exact", "prefix", "suffix"):
        lst = block.get(sub)
        assert isinstance(lst, list) and lst, sub
        assert lst == sorted(lst), f"{sub} not sorted (determinism)"
        assert len(lst) == len(set(lst)), f"{sub} has duplicates"
    # The exhibits' anchoring tokens are present.
    assert "welcome" in block["exact"]
    assert "getting-started" in block["exact"]
    assert "access-denied" in block["exact"]
    assert "redirect" in block["prefix"]
    assert {"callback", "onboarding", "redirect"} <= set(block["suffix"])


# ── Stage-slot guard: the pass sits post-journey-layer, pre-markers ──────


def test_slot_order_in_phase_finalize() -> None:
    """The demote must see the FINAL journey layer (after 6.986 B24) and
    run BEFORE the flowless-PF marker backstop (W5.1 loc-worthy) and
    emission integrity — the conservation discriminator only exists there."""
    src = (Path(__file__).resolve().parents[2]
           / "faultline" / "pipeline_v2" / "phase_finalize.py"
           ).read_text(encoding="utf-8")
    slot = src.index("run_devgrain_demote(")
    assert src.index("run_mega_pf_nav_rehome(") < slot          # after B24
    assert src.index("run_journey_lattice(") < slot             # after lattice
    assert slot < src.index("loc_worthy_backstop")              # pre-markers
    assert slot < src.index("enforce_emission_integrity(")      # pre-integrity
