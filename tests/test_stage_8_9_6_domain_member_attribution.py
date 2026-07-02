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
