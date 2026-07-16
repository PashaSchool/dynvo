"""B68 — terminal 4-way classification units (+ named anti-cases).

Covers: the OFF-gate + model-layer byte-identity, the documenso
frankenstein decomposition (three classes in ONE row), the NamespaceEcho
match laws (unique-only / ambiguous-abstain / generic-abstain / the
marker-kind self-match law), the live-flow-owner rung, the B23/B33
no-member-less-mint law, the known-lexer-hole taxonomy + honest
``unmapped`` fallback, span-trim honesty, no-silent-drop conservation,
and determinism.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from faultline.models.types import CoverageGap, FlowLineRange
from faultline.pipeline_v2.terminal_classification import (
    FATE_DEV_INFRA,
    FATE_REHOME,
    FATE_RESIDUE,
    TERMINAL_CLASSIFICATION_ENV,
    WHY_NO_EVIDENCE,
    WHY_UNMAPPED,
    classify_known_hole,
    known_hole_whys,
    run_terminal_classification,
    terminal_classification_enabled,
)


# ── fixtures ─────────────────────────────────────────────────────────────


def _pf(key: str, anchor: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=key, name=key, anchor_id=anchor or f"route:/{key}")


def _dev(
    pfid: str | None,
    paths: list[str],
    flow_paths: list[str] | None = None,
    member_roles: dict[str, str] | None = None,
) -> SimpleNamespace:
    flows = []
    for p in flow_paths or []:
        flows.append(SimpleNamespace(
            line_ranges=[SimpleNamespace(path=p, start_line=1, end_line=5)]))
    member_files = [
        SimpleNamespace(path=p, role=r)
        for p, r in (member_roles or {}).items()
    ]
    return SimpleNamespace(
        layer="developer", name=f"dev-{pfid or 'lane'}",
        product_feature_id=pfid, paths=paths,
        member_files=member_files, flows=flows)


def _gap(
    kind: str,
    pf: str,
    label: str = "Uncovered: X routes",
    files: list[tuple[str, int, int]] | None = None,
    routes: list[str] | None = None,
    synthesis_reason: str | None = None,
    authored_label: str | None = None,
) -> CoverageGap:
    spans = [
        FlowLineRange(path=p, start_line=s, end_line=e)
        for p, s, e in (files or [])
    ]
    return CoverageGap(
        id=f"GAP-{abs(hash((kind, pf, label))) % 10**10:010d}",
        product_feature_id=pf,
        kind=kind,  # type: ignore[arg-type]
        label=label,
        authored_label=authored_label,
        routes=routes or [],
        surface_files=spans,
        loc=sum(e - s + 1 for _, s, e in (files or [])),
        synthesis_reason=synthesis_reason,
    )


def _run(
    gaps: list[Any],
    pfs: list[Any],
    devs: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    **kw: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    monkeypatch.setenv(TERMINAL_CLASSIFICATION_ENV, "1")
    scan_meta: dict[str, Any] = {}
    tele = run_terminal_classification(gaps, pfs, devs, scan_meta, **kw)
    return tele, scan_meta


# ── flag discipline ──────────────────────────────────────────────────────


def test_flag_default_on_and_kill_switch(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # SEMANTIC (horizon-1 flip): unset now defaults ON.
    monkeypatch.delenv(TERMINAL_CLASSIFICATION_ENV, raising=False)
    assert terminal_classification_enabled() is True
    for off in ("0", "false", "off", "no", ""):
        monkeypatch.setenv(TERMINAL_CLASSIFICATION_ENV, off)
        assert terminal_classification_enabled() is False
    monkeypatch.setenv(TERMINAL_CLASSIFICATION_ENV, "1")
    assert terminal_classification_enabled() is True


def test_inverted_killswitch_terminal_classification(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Inverted kill-switch: unset ≡ explicit ``1`` (identical module
    behaviour), and explicit ``0``/``false`` == the pre-flip (old) behaviour."""
    monkeypatch.delenv(TERMINAL_CLASSIFICATION_ENV, raising=False)
    unset = terminal_classification_enabled()
    monkeypatch.setenv(TERMINAL_CLASSIFICATION_ENV, "1")
    assert terminal_classification_enabled() is unset is True
    monkeypatch.setenv(TERMINAL_CLASSIFICATION_ENV, "0")
    assert terminal_classification_enabled() is False
    monkeypatch.setenv(TERMINAL_CLASSIFICATION_ENV, "false")
    assert terminal_classification_enabled() is False


def test_off_gate_no_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTI-CASE (kill-switch): flag off → rows untouched, no scan_meta
    key, no why_unresolved stamped."""
    # MECHANICAL (horizon-1 flip): explicit "0" for the OFF-world assertion
    # (unset now defaults ON).
    monkeypatch.setenv(TERMINAL_CLASSIFICATION_ENV, "0")
    gap = _gap("loc_worthy", "executor",
               files=[("tracecat/executor/config.py", 1, 85)])
    gaps = [gap]
    before = gap.model_dump()
    scan_meta: dict[str, Any] = {}
    tele = run_terminal_classification(
        gaps, [_pf("executor")], [], scan_meta)
    assert tele == {"enabled": False}
    assert gaps == [gap]
    assert gap.model_dump() == before
    assert "terminal_classification" not in scan_meta


def test_model_dump_omits_why_unresolved_when_none() -> None:
    """Byte-identity smoke at the model layer: the new field never
    appears in dumps unless stamped (B45 omit-when-default law)."""
    gap = _gap("loc_worthy", "executor")
    dump = gap.model_dump()
    assert "why_unresolved" not in dump
    assert "authored_label" not in dump  # B45 law still intact
    gap.why_unresolved = WHY_UNMAPPED
    assert gap.model_dump()["why_unresolved"] == WHY_UNMAPPED


def test_env_output_flags_registered_union() -> None:
    """The flag keys the scan cache; the earlier B-cycle flags survive
    (union append — nothing removed)."""
    from faultline.pipeline_v2.scan_result_cache import ENV_OUTPUT_FLAGS
    assert "FAULTLINE_TERMINAL_CLASSIFICATION" in ENV_OUTPUT_FLAGS
    assert "FAULTLINE_JOBS_ENTRIES" in ENV_OUTPUT_FLAGS
    assert "FAULTLINE_SAMEUNIT_DOMAIN_CAP" in ENV_OUTPUT_FLAGS


# ── the documenso frankenstein (the spec's exhibit) ──────────────────────


def test_frankenstein_decomposes_by_members(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """THREE classes in ONE row dissolve: e2e label → non-feature trace
    (off the board), prisma files → dev-infrastructure fraction, the
    B27-fragment home PF link dies with the row."""
    gap = _gap(
        "adjudicated_noise", "team-verify-email",
        label="BrandingLogo — render signing certificate to PDF",
        files=[("packages/prisma/client.ts", 1, 40),
               ("packages/prisma/schema.prisma", 1, 200)],
        synthesis_reason="e2e_journey_recall",
        authored_label="BrandingLogo — render signing certificate to PDF",
    )
    gaps: list[Any] = [gap]
    tele, scan_meta = _run(
        gaps, [_pf("team-verify-email")], [], monkeypatch,
        dev_artifact_units=frozenset({"packages/prisma"}))
    assert gaps == []  # off the board
    assert tele["counts"]["non_feature_rows"] == 1
    row = tele["rows"][0]
    assert row["verdict"] == "non_feature"
    assert row["authored_label"] == gap.authored_label  # full trace
    fates = {f["path"]: f for f in row["files"]}
    assert fates["packages/prisma/client.ts"]["fate"] == FATE_DEV_INFRA
    assert fates["packages/prisma/client.ts"]["evidence"] == (
        "dev_artifact_unit")
    assert scan_meta["terminal_classification"] is tele


def test_e2e_orphan_kind_is_non_feature(
        monkeypatch: pytest.MonkeyPatch) -> None:
    gap = _gap("e2e_orphan", "billing", label="user can pay invoice",
               files=[("apps/web/e2e/pay.spec.ts", 1, 30)])
    gaps: list[Any] = [gap]
    tele, _ = _run(gaps, [_pf("billing")], [], monkeypatch)
    assert gaps == []
    assert tele["rows"][0]["verdict"] == "non_feature"


# ── rung (2): NamespaceEcho + live-flow owner ────────────────────────────


def test_echo_self_match_dissolves_adjudicated(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A demoted journey's surface under features/<home>/ is the home
    PF's own product surface — the claim dissolves (langfuse 'Connect
    setup')."""
    gap = _gap("adjudicated_noise", "setup", label="Connect setup",
               files=[("web/src/features/setup/hooks.ts", 1, 50)])
    gaps: list[Any] = [gap]
    tele, _ = _run(gaps, [_pf("setup")], [], monkeypatch)
    assert gaps == []
    row = tele["rows"][0]
    assert row["verdict"] == "dissolved"
    assert row["files"][0]["fate"] == FATE_REHOME
    assert row["files"][0]["target_pf"] == "setup"
    assert row["files"][0]["evidence"] == "namespace_echo"


def test_echo_foreign_unique_rehomes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    gap = _gap("loc_worthy", "admin-setup", label="Manage events",
               files=[("apps/web/src/features/event-types/data.ts", 1, 90)])
    gaps: list[Any] = [gap]
    tele, _ = _run(
        gaps, [_pf("admin-setup"), _pf("event-types")], [], monkeypatch)
    assert gaps == []
    row = tele["rows"][0]
    assert row["files"][0]["fate"] == FATE_REHOME
    assert row["files"][0]["target_pf"] == "event-types"


def test_echo_ambiguous_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTI-CASE (NamespaceEcho law): tokens hitting >1 distinct PF never
    guess — the file stays residue and the row survives stamped."""
    gap = _gap("loc_worthy", "setup",
               files=[("apps/web/src/features/setup/billing.ts", 1, 20)])
    gaps: list[Any] = [gap]
    pfs = [_pf("setup"), _pf("billing")]
    tele, _ = _run(gaps, pfs, [], monkeypatch)
    assert len(gaps) == 1
    assert gaps[0].why_unresolved == WHY_UNMAPPED
    assert tele["rows"][0]["files"][0]["fate"] == FATE_RESIDUE
    assert len(pfs) == 2  # nothing minted, nothing removed


def test_generic_token_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTI-CASE: generic tokens (constant/util/…) never echo-match —
    plane's packages/constants surface stays residue."""
    gap = _gap("adjudicated_noise", "project", label="Manage favorites",
               files=[("packages/constants/src/fetch-keys.ts", 1, 8)])
    gaps: list[Any] = [gap]
    tele, _ = _run(gaps, [_pf("project"), _pf("constant")], [], monkeypatch)
    assert len(gaps) == 1
    assert tele["rows"][0]["files"][0]["fate"] == FATE_RESIDUE


def test_marker_kind_self_match_stays_residue(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTI-CASE (kind law): a B45 marker (loc_worthy) whose file echoes
    its OWN home PF is NOT dissolved — a self-match cannot answer the
    'no attachable flow' claim; the row stays as (5) residue."""
    gap = _gap("loc_worthy", "executor",
               files=[("src/features/executor/config.py", 1, 40)])
    gaps: list[Any] = [gap]
    tele, _ = _run(gaps, [_pf("executor")], [], monkeypatch)
    assert len(gaps) == 1
    assert gaps[0].why_unresolved == WHY_UNMAPPED
    assert tele["rows"][0]["verdict"] == "unresolved"


def test_live_flow_owner_dissolves_adjudicated(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """plane's 'Track authentication…': the demoted journey's files carry
    LIVE flows owned by the home PF → the fragment dissolves back."""
    path = "packages/constants/src/event-tracker/core.ts"
    gap = _gap("adjudicated_noise", "authentication",
               label="Track authentication and product events",
               files=[(path, 1, 99)])
    gaps: list[Any] = [gap]
    devs = [_dev("authentication", [path], flow_paths=[path])]
    tele, _ = _run(gaps, [_pf("authentication")], devs, monkeypatch)
    assert gaps == []
    row = tele["rows"][0]
    assert row["files"][0]["evidence"] == "live_flow_owner"
    assert row["files"][0]["target_pf"] == "authentication"


# ── rung (4): dev-infrastructure predicates ──────────────────────────────


@pytest.mark.parametrize("path,evidence", [
    ("apps/web/__tests__/pay.test.ts", "test_path"),
    ("internal/db/models.pb.go", "generated_path"),
    ("packages/api/__generated__/client.ts", "artifact_ink:generated"),
    ("apps/web/public/locales/en/common.json", "artifact_ink:locale"),
])
def test_dev_infra_existing_predicates(
        path: str, evidence: str, monkeypatch: pytest.MonkeyPatch) -> None:
    gap = _gap("loc_worthy", "billing", files=[(path, 1, 10)])
    gaps: list[Any] = [gap]
    tele, _ = _run(gaps, [_pf("billing")], [], monkeypatch)
    assert gaps == []
    f = tele["rows"][0]["files"][0]
    assert f["fate"] == FATE_DEV_INFRA
    assert f["evidence"] == evidence


def test_lane_resident_and_shared_leaf(
        monkeypatch: pytest.MonkeyPatch) -> None:
    lane_path = "packages/tsconfig/base.ts"
    shared_path = "packages/ui/theme.ts"
    gap = _gap("loc_worthy", "billing",
               files=[(lane_path, 1, 5), (shared_path, 1, 5)])
    gaps: list[Any] = [gap]
    devs = [
        _dev(None, [lane_path]),                       # lane resident
        _dev("billing", [], member_roles={shared_path: "shared"}),
    ]
    tele, _ = _run(gaps, [_pf("billing")], devs, monkeypatch)
    assert gaps == []
    fates = {f["path"]: f["evidence"] for f in tele["rows"][0]["files"]}
    assert fates[lane_path] == "lane_resident"
    assert fates[shared_path] == "shared_leaf"


# ── rung (3): no member-less mint (B23/B33) ──────────────────────────────


def test_member_less_residue_never_mints(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTI-CASE (B23/B33): routes + residue WITHOUT live members →
    evaluation recorded, mint_candidates empty, product_features
    unchanged."""
    gap = _gap("system_route", "executor", routes=["/executor/run"],
               files=[("tracecat/executor/gateway.py", 1, 85)])
    gaps: list[Any] = [gap]
    pfs = [_pf("executor")]
    tele, _ = _run(gaps, pfs, [], monkeypatch)
    assert tele["mint_evaluated"] == 1
    assert tele["mint_candidates"] == []
    assert [_p.id for _p in pfs] == ["executor"]  # no mint, no removal
    assert len(gaps) == 1  # residue row survives as (5)


def test_mint_candidate_recorded_never_minted(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A residue file with AMBIGUOUS live-flow owners + routes is a
    recorded candidate (operator question), never an improvised mint."""
    path = "src/queues/dispatch.ts"
    gap = _gap("system_route", "jobs", routes=["/jobs/run"],
               files=[(path, 1, 30)])
    gaps: list[Any] = [gap]
    pfs = [_pf("jobs"), _pf("mail"), _pf("sync")]
    devs = [_dev("mail", [path], flow_paths=[path]),
            _dev("sync", [path], flow_paths=[path])]  # 2 owners → ambiguous
    tele, _ = _run(gaps, pfs, devs, monkeypatch)
    assert len(tele["mint_candidates"]) == 1
    assert tele["mint_candidates"][0]["files"] == [path]
    assert len(pfs) == 3  # STILL no mint
    assert len(gaps) == 1


# ── rung (5): known-hole taxonomy ────────────────────────────────────────


def test_known_hole_nestjs_content(
        tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.controller.ts").write_text(
        "import { Controller } from '@nestjs/common';\n"
        "@Controller('users')\nexport class UsersController {}\n",
        encoding="utf-8")
    gap = _gap("loc_worthy", "users",
               files=[("src/app.controller.ts", 1, 3)])
    gaps: list[Any] = [gap]
    _run(gaps, [_pf("users")], [], monkeypatch, repo_path=tmp_path)
    assert len(gaps) == 1
    assert "nestjs-class (B66)" in str(gaps[0].why_unresolved)
    assert gaps[0].why_unresolved in known_hole_whys()


def test_known_hole_language_beyond_ast_no_read() -> None:
    why = classify_known_hole("app/models/user.rb", None)
    assert why is not None and "language-beyond-ast" in why
    assert why in known_hole_whys()


def test_unmapped_fallback_is_honest(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback is stamped, visible, and NOT a known hole — the
    census gate must be able to fail on it."""
    gap = _gap("loc_worthy", "billing",
               files=[("apps/web/src/plaincode.ts", 1, 10)])
    gaps: list[Any] = [gap]
    _run(gaps, [_pf("billing")], [], monkeypatch)
    assert gaps[0].why_unresolved == WHY_UNMAPPED
    assert WHY_UNMAPPED not in known_hole_whys()


def test_evidence_less_row_stamped(
        monkeypatch: pytest.MonkeyPatch) -> None:
    gap = _gap("adjudicated_noise", "auth", label="ghost claim")
    gaps: list[Any] = [gap]
    tele, _ = _run(gaps, [_pf("auth")], [], monkeypatch)
    assert len(gaps) == 1
    assert gaps[0].why_unresolved == WHY_NO_EVIDENCE
    assert tele["rows"][0]["verdict"] == "unresolved"


# ── honesty + conservation + determinism ─────────────────────────────────


def test_residue_trim_recomputes_loc(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A partially-classified row keeps ONLY residue spans; loc is the
    6.97b union of what remains; the original rides in the trace."""
    gap = _gap("loc_worthy", "billing",
               files=[("packages/prisma/client.ts", 1, 100),
                      ("app/models/invoice.rb", 1, 10)])
    gaps: list[Any] = [gap]
    tele, _ = _run(gaps, [_pf("billing")], [], monkeypatch,
                   dev_artifact_units=frozenset({"packages/prisma"}))
    assert len(gaps) == 1
    kept = gaps[0]
    assert [s.path for s in kept.surface_files] == ["app/models/invoice.rb"]
    assert kept.loc == 10
    row = tele["rows"][0]
    assert row["original_loc"] == 110
    assert row["residual_loc"] == 10
    assert "language-beyond-ast" in str(kept.why_unresolved)


def test_no_silent_drop_conservation(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """SACRED: rows_in == removed + kept; every input row has a trace
    entry (nothing leaves the board without a written fate)."""
    gaps: list[Any] = [
        _gap("e2e_orphan", "a", label="e2e one"),
        _gap("loc_worthy", "b",
             files=[("web/src/features/b/x.ts", 1, 5)]),
        _gap("loc_worthy", "c",
             files=[("app/models/c.rb", 1, 5)]),
    ]
    ids = [g.id for g in gaps]
    tele, _ = _run(gaps, [_pf("a"), _pf("b"), _pf("c")], [], monkeypatch)
    assert tele["rows_in"] == 3
    assert tele["rows_in"] == tele["rows_removed"] + (
        tele["rows_kept_unresolved"])
    assert [r["id"] for r in tele["rows"]] == ids


# ── B68 Q2 slice (а) — five ratified marker classes + repo_hygiene ───────


@pytest.mark.parametrize("relpath,content,expect", [
    ("packages/emails/welcome-mail.tsx",
     'import { Html } from "@react-email/components";\n',
     "email-template (B63)"),
    ("packages/cli/bin/run-tool.ts",
     'import { Command } from "commander";\n',
     "cli-command (B63)"),
    ("packages/mcp/server-main.ts",
     'import { McpServer } from "@modelcontextprotocol/sdk/server";\n',
     "mcp-server (B63)"),
    ("packages/zap/index-app.ts",
     'const zapier = require("zapier-platform-core");\n',
     "integration-platform-app (B63)"),
    ("packages/desktop/main-boot.ts",
     'import { app } from "electron";\napp.whenReady().then(main);\n',
     "desktop-shell (B63)"),
])
def test_q2_marker_classes_positive(
        relpath: str, content: str, expect: str,
        tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each ratified class names its residue row (census-verified
    markers: react-email / commander / MCP SDK / zapier / electron)."""
    abs_path = tmp_path / relpath
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(content, encoding="utf-8")
    gap = _gap("loc_worthy", "billing", files=[(relpath, 1, 10)])
    gaps: list[Any] = [gap]
    _run(gaps, [_pf("billing")], [], monkeypatch, repo_path=tmp_path)
    assert len(gaps) == 1
    why = str(gaps[0].why_unresolved)
    assert expect in why
    assert gaps[0].why_unresolved in known_hole_whys()


def test_q2_marker_in_test_file_does_not_fire(
        tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTI-CASE: a taxonomy marker inside a TEST file never reaches the
    known-hole rung — rung (4) test_path classifies it first (dev-infra),
    the row dissolves, and no why_unresolved is ever stamped."""
    relpath = "packages/cli/__tests__/run-tool.test.ts"
    abs_path = tmp_path / relpath
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text('import { Command } from "commander";\n',
                        encoding="utf-8")
    gap = _gap("loc_worthy", "billing", files=[(relpath, 1, 5)])
    gaps: list[Any] = [gap]
    tele, _ = _run(gaps, [_pf("billing")], [], monkeypatch,
                   repo_path=tmp_path)
    assert gaps == []  # dissolved via dev-infra, not legalized as (5)
    f = tele["rows"][0]["files"][0]
    assert f["fate"] == FATE_DEV_INFRA
    assert f["evidence"] == "test_path"
    assert tele["by_why"] == {}


def test_q2_repo_hygiene_dotfile(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A dot-leading basename is repo tooling by convention — rung (4)
    ``repo_hygiene``, no file read needed."""
    gap = _gap("loc_worthy", "billing",
               files=[("packages/widget/.env.example", 1, 3)])
    gaps: list[Any] = [gap]
    tele, _ = _run(gaps, [_pf("billing")], [], monkeypatch)
    assert gaps == []
    f = tele["rows"][0]["files"][0]
    assert f["fate"] == FATE_DEV_INFRA
    assert f["evidence"] == "repo_hygiene"


def test_q2_non_dotfile_is_not_repo_hygiene(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTI-CASE: dots INSIDE the basename never trigger the dotfile
    convention — a regular source file stays on the honest ladder."""
    gap = _gap("loc_worthy", "billing",
               files=[("packages/widget/env.example.ts", 1, 3)])
    gaps: list[Any] = [gap]
    tele, _ = _run(gaps, [_pf("billing")], [], monkeypatch)
    assert len(gaps) == 1  # residue → (5) unmapped, NOT dev-infra
    f = tele["rows"][0]["files"][0]
    assert f["fate"] == FATE_RESIDUE
    assert gaps[0].why_unresolved == WHY_UNMAPPED


def test_determinism_two_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    def build() -> tuple[list[Any], list[Any], list[Any]]:
        gaps: list[Any] = [
            _gap("adjudicated_noise", "setup", label="Connect setup",
                 files=[("web/src/features/setup/hooks.ts", 1, 50)]),
            _gap("loc_worthy", "billing",
                 files=[("app/models/invoice.rb", 1, 10),
                        ("apps/web/__tests__/x.test.ts", 1, 5)]),
        ]
        return gaps, [_pf("setup"), _pf("billing")], []

    g1, p1, d1 = build()
    t1, m1 = _run(g1, p1, d1, monkeypatch)
    g2, p2, d2 = build()
    t2, m2 = _run(g2, p2, d2, monkeypatch)
    assert copy.deepcopy(t1) == copy.deepcopy(t2)
    assert [g.model_dump() for g in g1] == [g.model_dump() for g in g2]
