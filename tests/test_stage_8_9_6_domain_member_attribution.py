"""Tests for Stage 8.9.6 — deterministic domain-dir member attribution.

Covers: the transfer (unowned hooks/api/<domain> member → same-named
feature, exact + singular tiers), every safety rail (owned files never
stolen, generic tokens skipped, ambiguous matches skipped, no self-
transfer), the OFF-by-default no-op, and telemetry.
"""

from __future__ import annotations

from datetime import datetime, timezone

from faultline.models.types import Feature, MemberFile
from faultline.pipeline_v2.stage_8_9_6_domain_member_attribution import (
    attribute_domain_members,
)

_ENV = "FAULTLINE_STAGE_8_9_6_DOMAIN_ATTRIBUTION"


def _feat(name, paths, members=None, uuid="u"):
    f = Feature(
        name=name, description=None, paths=list(paths), authors=[],
        total_commits=5, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=90.0,
        layer="developer", uuid=uuid,
    )
    if members is not None:
        f.member_files = [
            MemberFile(path=p, role="shared", confidence=0.5) for p in members
        ]
    return f


def _mnames(f):
    return [m.path for m in (f.member_files or [])]


def test_off_by_default_noop(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    anchor = _feat("frontend", ["fe/app.tsx"],
                   members=["fe/hooks/api/auth/h1.ts"])
    auth = _feat("auth", ["backend/auth/a.ts"], uuid="a")
    res = attribute_domain_members([anchor, auth])
    assert res.enabled is False
    assert res.files_transferred == 0
    assert _mnames(anchor) == ["fe/hooks/api/auth/h1.ts"]


def test_transfers_exact_and_singular(monkeypatch):
    """hooks/api/<domain> member-only files move to the same-named feature —
    exact slug (camelCase→kebab) and crude-singular tiers both work."""
    monkeypatch.setenv(_ENV, "1")
    anchor = _feat("frontend", ["fe/app.tsx"], members=[
        "fe/src/hooks/api/appConnections/h1.ts",   # singular → app-connection
        "fe/src/hooks/api/appConnections/h2.ts",
        "fe/src/hooks/api/pam/h3.ts",              # exact → pam
        "fe/src/hooks/useToggle.tsx",              # no domain match → stays
    ])
    conn = _feat("app-connection", ["backend/conn/c.ts"], uuid="c")
    pam = _feat("pam", ["backend/pam/p.ts"], uuid="p")
    res = attribute_domain_members([anchor, conn, pam])
    assert res.files_transferred == 3
    assert res.targets_enriched == 2
    assert _mnames(anchor) == ["fe/src/hooks/useToggle.tsx"]
    assert set(_mnames(conn)) == {
        "fe/src/hooks/api/appConnections/h1.ts",
        "fe/src/hooks/api/appConnections/h2.ts",
    }
    # ownership claimed (files were unowned)
    assert "fe/src/hooks/api/pam/h3.ts" in pam.paths


def test_owned_files_never_stolen(monkeypatch):
    """Rail 1: a member entry whose path is OWNED anywhere stays put."""
    monkeypatch.setenv(_ENV, "1")
    owned_path = "fe/src/hooks/api/pam/h1.ts"
    anchor = _feat("frontend", ["fe/app.tsx"], members=[owned_path])
    pam = _feat("pam", [owned_path], uuid="p")  # pam already OWNS it
    res = attribute_domain_members([anchor, pam])
    assert res.files_transferred == 0
    assert _mnames(anchor) == [owned_path]


def test_generic_tokens_never_match(monkeypatch):
    """Rail 2: 'api'/'ui'/... must not be read as a domain even when a
    feature carries that name."""
    monkeypatch.setenv(_ENV, "1")
    anchor = _feat("frontend", ["fe/app.tsx"],
                   members=["fe/src/hooks/api/x1.ts"])  # domain seg = 'api'? no deeper dir
    api = _feat("api", ["backend/api/a.ts"], uuid="a")
    res = attribute_domain_members([anchor, api])
    assert res.files_transferred == 0
    assert _mnames(anchor) == ["fe/src/hooks/api/x1.ts"]


def test_ambiguous_singular_skipped(monkeypatch):
    """Rail 3: two features collapsing to the same singular slug → skip."""
    monkeypatch.setenv(_ENV, "1")
    anchor = _feat("frontend", ["fe/app.tsx"],
                   members=["fe/src/hooks/api/certificates/h1.ts"])
    # TWO distinct features sharing the same slug "certificates" (a backend
    # and a frontend module) — the transfer target is ambiguous.
    c1 = _feat("certificates", ["b/c1.ts"], uuid="c1")
    c2 = _feat("certs-ui", ["b/c2.ts"], uuid="c2")
    c2.display_name = "Certificates"
    res = attribute_domain_members([anchor, c1, c2])
    assert res.files_transferred == 0
    assert res.ambiguous_skipped >= 1


def test_no_self_transfer(monkeypatch):
    """Rail 4: a feature holding its own domain-named member keeps it."""
    monkeypatch.setenv(_ENV, "1")
    pam = _feat("pam", ["backend/pam/p.ts"],
                members=["fe/src/hooks/api/pam/h1.ts"], uuid="p")
    res = attribute_domain_members([pam])
    assert res.files_transferred == 0
    assert _mnames(pam) == ["fe/src/hooks/api/pam/h1.ts"]


def test_components_container_also_matches(monkeypatch):
    """The components container works identically to hooks (same class)."""
    monkeypatch.setenv(_ENV, "1")
    anchor = _feat("frontend", ["fe/app.tsx"],
                   members=["fe/components/billing/B1.tsx"])
    billing = _feat("billing", ["backend/billing/b.ts"], uuid="b")
    res = attribute_domain_members([anchor, billing])
    assert res.files_transferred == 1
    assert "fe/components/billing/B1.tsx" in billing.paths


def test_deterministic_across_runs(monkeypatch):
    """Same input → same transfers, in order (no set-iteration leakage)."""
    monkeypatch.setenv(_ENV, "1")
    def build():
        anchor = _feat("frontend", ["fe/app.tsx"], members=[
            f"fe/src/hooks/api/pam/h{i}.ts" for i in range(5)
        ])
        pam = _feat("pam", ["backend/pam/p.ts"], uuid="p")
        return [anchor, pam]
    a = build(); b = build()
    attribute_domain_members(a)
    attribute_domain_members(b)
    assert [f.paths for f in a] == [f.paths for f in b]
    assert [_mnames(f) for f in a] == [_mnames(f) for f in b]


# ── audit fixes (2026-07-02): ownership registration, index tiers, dup claims ─


def test_transfer_registers_with_owned_paths(monkeypatch):
    """Audit #1 (CRITICAL): a target that ALREADY has anchor member rows (the
    real production shape) must see the transferred file in _owned_paths —
    the transfer mints a fresh primary/anchor MemberFile, not a stale
    shared-role copy."""
    from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import _owned_paths

    monkeypatch.setenv(_ENV, "1")
    anchor = _feat("frontend", ["fe/app.tsx"],
                   members=["fe/src/hooks/api/pam/h1.ts"])
    pam = _feat("pam", ["backend/pam/p.ts"], uuid="p")
    pam.member_files = [
        MemberFile(path="backend/pam/p.ts", role="anchor",
                   confidence=1.0, primary=True),
    ]
    res = attribute_domain_members([anchor, pam])
    assert res.files_transferred == 1
    assert "fe/src/hooks/api/pam/h1.ts" in _owned_paths(pam)
    moved = [m for m in pam.member_files
             if m.path == "fe/src/hooks/api/pam/h1.ts"]
    assert len(moved) == 1 and moved[0].primary and moved[0].role == "anchor"


def test_display_name_exact_not_shadowed_by_name_singular(monkeypatch):
    """Audit #3: name's singular reduction must not evict display_name's
    LITERAL slug from the exact index."""
    monkeypatch.setenv(_ENV, "1")
    anchor = _feat("frontend", ["fe/app.tsx"],
                   members=["fe/src/hooks/api/certificate/h1.ts"])
    f = _feat("certificates", ["b/c.ts"], uuid="c")
    f.display_name = "Certificate"   # literal singular — exact-tier entry
    res = attribute_domain_members([anchor, f])
    assert res.files_transferred == 1
    assert "fe/src/hooks/api/certificate/h1.ts" in f.paths


def test_cross_source_duplicate_claim_first_wins(monkeypatch):
    """Audit #4 (minor): the same member-only path in TWO sources — the first
    source (input order) transfers it; the second's copy stays put (it is now
    an owned path → Rail 1 protects it as a legit shared claim)."""
    monkeypatch.setenv(_ENV, "1")
    p = "fe/src/hooks/api/pam/h1.ts"
    s1 = _feat("frontend", ["fe/app.tsx"], members=[p], uuid="s1")
    s2 = _feat("frontend-v2", ["fe2/app.tsx"], members=[p], uuid="s2")
    pam = _feat("pam", ["backend/pam/p.ts"], uuid="p")
    res = attribute_domain_members([s1, s2, pam])
    assert res.files_transferred == 1
    assert pam.paths.count(p) == 1
    assert _mnames(s1) == []          # transferred away
    assert _mnames(s2) == [p]         # kept as shared claim on the new owner


def test_features_dir_container_transfers(monkeypatch):
    """2026-07-05 extension: the React feature-folder conventions
    (``features/<domain>``, ``modules/<domain>``) are domain containers too —
    member-only files under them transfer to the same-named feature."""
    monkeypatch.setenv(_ENV, "1")
    anchor = _feat("frontend", ["fe/app.tsx"], members=[
        "fe/src/features/anomalies/h1.ts",
        "fe/src/modules/detection-studio/h2.ts",
        "fe/src/features/common/h3.ts",       # generic domain → stays
    ])
    anomalies = _feat("anomalies", ["fe/src/features/anomalies/a.tsx"],
                      uuid="an")
    studio = _feat("detection-studio", ["fe/src/modules/detection-studio/s.tsx"],
                   uuid="ds")
    res = attribute_domain_members([anchor, anomalies, studio])
    assert res.files_transferred == 2
    assert "fe/src/features/anomalies/h1.ts" in anomalies.paths
    assert "fe/src/modules/detection-studio/h2.ts" in studio.paths
    assert _mnames(anchor) == ["fe/src/features/common/h3.ts"]


def test_dialog_modal_domains_are_generic(monkeypatch):
    """UI-widget container names (dialogs/modals/…) are never a product
    domain — same class as the pre-existing widgets/forms/icons tokens."""
    monkeypatch.setenv(_ENV, "1")
    anchor = _feat("frontend", ["fe/app.tsx"], members=[
        "fe/src/components/dialogs/h1.ts",
        "fe/src/features/modals/h2.ts",
    ])
    dialogs = _feat("dialogs", ["fe/x/d.tsx"], uuid="d")
    modals = _feat("modals", ["fe/x/m.tsx"], uuid="m")
    res = attribute_domain_members([anchor, dialogs, modals])
    assert res.files_transferred == 0
    assert len(_mnames(anchor)) == 2


def test_component_segs_frozen_for_8_9_5():
    """Contract: the 8.9.5 fan-out set is UNCHANGED — 8.9.6 extends via its
    own _CONTAINER_SEGS, never by mutating the split stage's vocabulary."""
    from faultline.pipeline_v2.stage_8_9_5_llm_component_split import (
        _COMPONENT_SEGS,
    )
    from faultline.pipeline_v2.stage_8_9_6_domain_member_attribution import (
        _CONTAINER_SEGS,
    )
    assert _COMPONENT_SEGS == frozenset(
        {"components", "component", "hooks", "hook"}
    )
    assert _CONTAINER_SEGS >= _COMPONENT_SEGS
    assert {"features", "modules"} <= _CONTAINER_SEGS
