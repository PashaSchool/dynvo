"""Tests for the sibling_router_collapse aggregator (Sprint D).

Three scales per [[rule-no-magic-tuning]]:
  - tiny: a small library (chi-shaped) with 2-3 sibling files — must
    NOT collapse.
  - medium: a SaaS-shaped folder with exactly the threshold count
    of siblings — should collapse.
  - large: a monolith with 12+ siblings — should collapse and union
    all paths.

Plus negatives: nested-children-with-shared-prefix; same-folder-only
groups; non-container directories.
"""

from __future__ import annotations

from faultline.aggregators.sibling_router_collapse import (
    MIN_DISTINCT_TAILS,
    MIN_SIBLINGS,
    _container_dir_and_subfolder,
    _display_name,
    _first_token,
    _tail,
    collapse_sibling_router_families,
)
from faultline.llm.sonnet_scanner import DeepScanResult


# ── helpers ──────────────────────────────────────────────────────────


def _result(**features: list[str]) -> DeepScanResult:
    return DeepScanResult(features=dict(features))


# ── token helpers ────────────────────────────────────────────────────


def test_first_token_handles_dash_and_slash() -> None:
    assert _first_token("identity-kubernetes-auth") == "identity"
    assert _first_token("pki-acme") == "pki"
    assert _first_token("foo/bar") == "foo"
    assert _first_token("single") == "single"


def test_tail_handles_single_token() -> None:
    assert _tail("identity-kubernetes-auth") == "kubernetes-auth"
    assert _tail("identity") == ""


def test_display_name_uppercases_short_prefix() -> None:
    assert _display_name("pki") == "PKI Management"
    assert _display_name("sso") == "SSO Management"
    assert _display_name("identity") == "Identity Management"
    assert _display_name("certificate") == "Certificate Management"


# ── container detection ─────────────────────────────────────────────


def test_container_detected_at_universal_segment() -> None:
    loc = _container_dir_and_subfolder(
        ["backend/src/services/identity-v2/foo.ts"],
        "identity-v2",
    )
    assert loc == ("backend/services", "identity-v2")


def test_ee_and_base_services_share_container_key() -> None:
    a = _container_dir_and_subfolder(
        ["backend/src/services/identity-v2/foo.ts"], "identity-v2",
    )
    b = _container_dir_and_subfolder(
        ["backend/src/ee/services/identity-auth-template/foo.ts"],
        "identity-auth-template",
    )
    assert a is not None and b is not None
    assert a[0] == b[0] == "backend/services"


def test_nested_child_not_sibling() -> None:
    """Feature ``external-infisical`` whose path is nested under
    ``services/app-connection/external-infisical/`` should NOT be
    treated as a sibling of other ``external-*`` services."""
    loc = _container_dir_and_subfolder(
        ["backend/src/services/app-connection/external-infisical/foo.ts"],
        "external-infisical",
    )
    # The sibling folder under ``services`` is ``app-connection``,
    # which does NOT start with ``external`` — so the feature is
    # NOT a true sibling-folder candidate.
    assert loc is None


def test_no_container_returns_none() -> None:
    assert (
        _container_dir_and_subfolder(["src/lib/foo.ts"], "foo")
        is None
    )


# ── tiny-scale: must not fire ────────────────────────────────────────


def test_tiny_repo_with_three_siblings_does_not_collapse() -> None:
    """chi-shaped: 3 middleware files. Below MIN_SIBLINGS → no collapse."""
    result = _result(
        **{
            "middleware-auth": ["backend/services/middleware-auth/auth.ts"],
            "middleware-cors": ["backend/services/middleware-cors/cors.ts"],
            "middleware-log": ["backend/services/middleware-log/log.ts"],
        }
    )
    stats = collapse_sibling_router_families(result)
    assert stats.families_collapsed == 0
    assert len(result.features) == 3


def test_min_siblings_floor_is_at_least_four() -> None:
    """Documents the structural floor so future tuners see the
    rationale: engineering-grain granularity is correct below 4."""
    assert MIN_SIBLINGS >= 4
    assert MIN_DISTINCT_TAILS >= 3


# ── medium-scale: borderline ─────────────────────────────────────────


def test_four_siblings_with_three_distinct_tails_collapses() -> None:
    result = _result(
        **{
            "cert-auth": ["backend/services/cert-auth/dal.ts"],
            "cert-issue": ["backend/services/cert-issue/dal.ts"],
            "cert-renew": ["backend/services/cert-renew/dal.ts"],
            "cert-revoke": ["backend/services/cert-revoke/dal.ts"],
            "unrelated": ["backend/services/unrelated/foo.ts"],
        }
    )
    stats = collapse_sibling_router_families(result)
    assert stats.families_collapsed == 1
    assert "Cert Management" in result.features
    assert "cert-auth" not in result.features
    assert "unrelated" in result.features
    assert len(result.features["Cert Management"]) == 4


# ── large-scale: monolith ────────────────────────────────────────────


def test_twelve_sibling_families_collapse_and_union_paths() -> None:
    siblings = {
        f"identity-{suffix}": [
            f"backend/src/services/identity-{suffix}/dal.ts",
            f"backend/src/services/identity-{suffix}/router.ts",
        ]
        for suffix in (
            "v2", "kubernetes-auth", "azure-auth", "oidc-auth",
            "aws-auth", "gcp-auth", "jwt-auth", "spiffe-auth",
            "oci-auth", "alicloud-auth", "ldap-auth", "tls-cert-auth",
        )
    }
    result = _result(**siblings)
    stats = collapse_sibling_router_families(result)
    assert stats.families_collapsed == 1
    assert "Identity Management" in result.features
    assert len(result.features["Identity Management"]) == 24


# ── negative cases ───────────────────────────────────────────────────


def test_same_subfolder_does_not_collapse() -> None:
    """Five features all living in the same sibling folder are NOT
    a sibling family — they're sub-features of one thing."""
    result = _result(
        **{
            "foo-a": ["backend/services/foo/a.ts"],
            "foo-b": ["backend/services/foo/b.ts"],
            "foo-c": ["backend/services/foo/c.ts"],
            "foo-d": ["backend/services/foo/d.ts"],
            "foo-e": ["backend/services/foo/e.ts"],
        }
    )
    stats = collapse_sibling_router_families(result)
    assert stats.families_collapsed == 0


def test_features_outside_container_are_ignored() -> None:
    result = _result(
        **{
            "foo-a": ["lib/foo-a.ts"],
            "foo-b": ["lib/foo-b.ts"],
            "foo-c": ["lib/foo-c.ts"],
            "foo-d": ["lib/foo-d.ts"],
        }
    )
    stats = collapse_sibling_router_families(result)
    assert stats.families_collapsed == 0


def test_collapse_preserves_flows_and_descriptions() -> None:
    result = DeepScanResult(
        features={
            f"foo-{s}": [f"backend/services/foo-{s}/dal.ts"]
            for s in ("a", "b", "c", "d")
        },
        flows={
            "foo-a": ["create-foo-a-flow"],
            "foo-b": ["create-foo-b-flow"],
        },
        descriptions={"foo-a": "the foo-a service"},
    )
    stats = collapse_sibling_router_families(result)
    assert stats.families_collapsed == 1
    assert "FOO Management" in result.features
    assert set(result.flows["FOO Management"]) == {
        "create-foo-a-flow", "create-foo-b-flow",
    }
    assert result.descriptions["FOO Management"] == "the foo-a service"
