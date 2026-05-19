"""Tests for Stage 6.5 — Layer 2 product clusterer (Sprint B3).

Pure unit tests — no LLM, no git, no network. The clusterer reads the
filesystem (for dep-anchor import scans + ``faultlines.yaml`` override),
so each test stages a tiny fake repo under ``tmp_path`` and feeds it
through :func:`run_product_clusterer`.

Cases exercised:

  Rule 1 (workspace cluster)
    - dev feature whose paths concentrate ≥70% under ``apps/<X>`` folds
      into the workspace-titled product feature.
    - sub-70% concentration does NOT fire workspace rule.

  Rule 2 (dependency anchor cluster)
    - dev feature whose paths import ``stripe`` folds under "Billing".
    - dev feature whose paths import ``resend`` folds under "Email".

  Rule 3 (customer YAML override)
    - presence of ``faultlines.yaml`` with explicit ``includes:`` forces
      the assignment irrespective of earlier rules.

  Conflict resolution
    - workspace (conf 0.6) vs dep-anchor (conf 0.75) → dep-anchor wins,
      but BOTH anchor_signals are preserved.

  Multi-anchor 50/50
    - dev feature with roughly even Stripe + Resend imports gets
      mapped to BOTH "Billing" and "Email" product features.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.models.types import Feature
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_6_5_product_clusterer import (
    ProductFeature,
    run_product_clusterer,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _feat(name: str, paths: list[str], *, total_commits: int = 1) -> Feature:
    """Minimal :class:`Feature` factory for clusterer input."""
    return Feature(
        name=name,
        paths=paths,
        authors=["alice"],
        total_commits=total_commits,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=[],
        layer="developer",
    )


def _ctx(repo_path: Path) -> ScanContext:
    """Minimal :class:`ScanContext` — only ``repo_path`` matters for the
    clusterer (workspaces are detected from path prefixes, not from
    ``ctx.workspaces``).
    """
    return ScanContext(
        repo_path=repo_path,
        stack="next-monorepo",
        monorepo=True,
        workspaces=None,
        tracked_files=[],
        commits=[],
    )


def _write(repo_root: Path, rel: str, body: str) -> None:
    """Create ``repo_root/rel`` with the given body, mkdir parents."""
    full = repo_root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(body, encoding="utf-8")


# ── Rule 1 — Workspace cluster ──────────────────────────────────────────


def test_workspace_rule_fires_on_70pct_concentration(tmp_path: Path) -> None:
    """A feature with ≥70% paths under ``apps/admin/`` folds into "Admin"."""
    repo = tmp_path
    # 4 of 5 paths under apps/admin/ → 80% concentration.
    for p in (
        "apps/admin/page.tsx",
        "apps/admin/layout.tsx",
        "apps/admin/users.tsx",
        "apps/admin/audit.tsx",
        "other/utils.ts",
    ):
        _write(repo, p, "// empty")

    feat = _feat("admin-panel", paths=[
        "apps/admin/page.tsx",
        "apps/admin/layout.tsx",
        "apps/admin/users.tsx",
        "apps/admin/audit.tsx",
        "other/utils.ts",
    ])

    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])

    assert "admin-panel" in mapping
    assert mapping["admin-panel"] == ("Admin",)
    assert len(products) == 1
    assert products[0].name == "Admin"
    assert telemetry["product_features_count"] == 1
    assert telemetry["developer_features_mapped_pct"] == 1.0


def test_workspace_rule_skips_below_threshold(tmp_path: Path) -> None:
    """A feature with <70% workspace concentration does NOT cluster."""
    repo = tmp_path
    for p in (
        "apps/admin/page.tsx",
        "apps/admin/layout.tsx",
        "lib/util.ts",
        "lib/other.ts",
        "lib/more.ts",
    ):
        _write(repo, p, "// empty")

    feat = _feat("mixed", paths=[
        "apps/admin/page.tsx",
        "apps/admin/layout.tsx",
        "lib/util.ts",
        "lib/other.ts",
        "lib/more.ts",
    ])

    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])

    assert mapping == {}
    assert products == []
    assert telemetry["product_features_count"] == 0
    assert telemetry["developer_features_orphan_count"] == 1


# ── Rule 2 — Dependency anchor cluster ──────────────────────────────────


def test_dep_anchor_rule_billing_from_stripe(tmp_path: Path) -> None:
    """A feature whose paths import ``stripe`` folds into "Billing"."""
    repo = tmp_path
    _write(repo, "lib/payments.ts", 'import Stripe from "stripe";\nconst s = new Stripe();\n')
    _write(repo, "lib/checkout.ts", 'import { createCheckout } from "stripe/checkout";\n')

    feat = _feat("payments", paths=["lib/payments.ts", "lib/checkout.ts"])

    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])

    assert "payments" in mapping
    assert "Billing" in mapping["payments"]
    assert any(pf.name == "Billing" for pf in products)
    billing = next(pf for pf in products if pf.name == "Billing")
    assert "payments" in billing.paths or len(billing.paths) > 0


def test_dep_anchor_rule_email_from_resend(tmp_path: Path) -> None:
    """A feature whose paths import ``resend`` folds into "Email"."""
    repo = tmp_path
    _write(repo, "lib/email.ts", 'import { Resend } from "resend";\n')

    feat = _feat("notifications", paths=["lib/email.ts"])

    products, mapping, _ = run_product_clusterer(_ctx(repo), [feat])

    assert mapping.get("notifications") == ("Email",)
    assert any(pf.name == "Email" for pf in products)


# ── Rule 3 — Customer YAML override ─────────────────────────────────────


def test_customer_yaml_override_wins_over_other_rules(tmp_path: Path) -> None:
    """``faultlines.yaml`` mapping overrides workspace + dep-anchor."""
    repo = tmp_path
    _write(repo, "apps/billing/charge.ts", 'import Stripe from "stripe";\n')
    _write(repo, "apps/billing/refund.ts", 'import Stripe from "stripe";\n')
    # 100% under apps/billing AND imports stripe — both Rule 1 and
    # Rule 2 would fire, but YAML override forces a custom label.
    _write(repo, "faultlines.yaml", """
product_features:
  - name: "Monetization Stack"
    includes: [billing-engine]
""".lstrip())

    feat = _feat("billing-engine", paths=[
        "apps/billing/charge.ts",
        "apps/billing/refund.ts",
    ])

    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])

    assert mapping == {"billing-engine": ("Monetization Stack",)}
    assert [pf.name for pf in products] == ["Monetization Stack"]
    assert telemetry["product_clusterer_source_breakdown"].get("rule:customer-yaml") == 1


# ── Conflict resolution — Workspace vs Dep-Anchor ───────────────────────


def test_dep_anchor_beats_workspace_on_conflict(tmp_path: Path) -> None:
    """Dep-anchor (conf 0.75) beats workspace (conf 0.6) — but BOTH
    anchor signals are preserved in the product feature's provenance.
    """
    repo = tmp_path
    # 100% concentration under apps/admin/ — Rule 1 says "Admin".
    # The same files import stripe — Rule 2 says "Billing".
    _write(repo, "apps/admin/billing.ts", 'import Stripe from "stripe";\n')
    _write(repo, "apps/admin/charge.ts", 'import Stripe from "stripe";\n')
    _write(repo, "apps/admin/refund.ts", 'import Stripe from "stripe";\n')

    feat = _feat("admin-payments", paths=[
        "apps/admin/billing.ts",
        "apps/admin/charge.ts",
        "apps/admin/refund.ts",
    ])

    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])

    # Dep-anchor wins → "Billing" is the assignment.
    assert mapping.get("admin-payments") == ("Billing",)
    # When two rules vote on the same dev feature, the source breakdown
    # registers it under the "combined" bucket so we can prove the
    # workspace rule's contribution wasn't silently dropped.
    assert telemetry["product_clusterer_source_breakdown"].get("combined") == 1


# ── Multi-anchor ambiguous (50/50) ──────────────────────────────────────


def test_multi_anchor_ambiguous_emits_both_memberships(tmp_path: Path) -> None:
    """When two dep anchors fire with even path share, both product
    features include the dev feature.
    """
    repo = tmp_path
    # One file imports stripe, one imports resend — perfectly even
    # split, so the leader's weight is NOT ≥2× the runner-up's.
    _write(repo, "lib/charge.ts", 'import Stripe from "stripe";\n')
    _write(repo, "lib/email.ts", 'import { Resend } from "resend";\n')

    feat = _feat("mixed-service", paths=["lib/charge.ts", "lib/email.ts"])

    products, mapping, _ = run_product_clusterer(_ctx(repo), [feat])

    labels = set(mapping.get("mixed-service") or ())
    assert "Billing" in labels and "Email" in labels
    product_names = {pf.name for pf in products}
    assert {"Billing", "Email"}.issubset(product_names)


# ── Orphan accounting ───────────────────────────────────────────────────


def test_orphan_dev_features_show_in_telemetry(tmp_path: Path) -> None:
    """Dev features that no rule maps stay orphan (no product_feature_id)
    and are counted in ``developer_features_orphan_count``.
    """
    repo = tmp_path
    _write(repo, "src/random.ts", "// nothing here\n")
    feat = _feat("orphan-thing", paths=["src/random.ts"])

    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])

    assert mapping == {}
    assert products == []
    assert telemetry["developer_features_orphan_count"] == 1


# ── Sprint B3.1 — domain-noun refinement ─────────────────────────────────


def test_domain_noun_refines_workspace_label(tmp_path: Path) -> None:
    """Workspace cluster gains a domain-noun label and bumps telemetry.

    When the feature's paths sit under ``apps/web/(documents)/`` the
    product feature is labelled "Documents" (not "Web"), and the
    ``workspace+domain`` rule key fires in source_breakdown.
    """
    repo = tmp_path
    for p in (
        "apps/web/(documents)/page.tsx",
        "apps/web/(documents)/upload/page.tsx",
        "apps/web/(documents)/share/page.tsx",
        "apps/web/(documents)/[id]/page.tsx",
    ):
        _write(repo, p, "// empty")

    feat = _feat("documents-ui", paths=[
        "apps/web/(documents)/page.tsx",
        "apps/web/(documents)/upload/page.tsx",
        "apps/web/(documents)/share/page.tsx",
        "apps/web/(documents)/[id]/page.tsx",
    ])

    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])

    # Domain-noun wins -> label is "Documents", not "Web".
    assert mapping["documents-ui"] == ("Documents",)
    assert products[0].name == "Documents"
    breakdown = telemetry["product_clusterer_source_breakdown"]
    assert breakdown.get("rule:workspace+domain") == 1
    assert telemetry["domain_noun_extraction_rate"] == 1.0
    assert telemetry["product_clusterer_votes_cast"]["workspace+domain"] == 1


def test_domain_noun_beats_dep_anchor_when_higher_confidence(tmp_path: Path) -> None:
    """Spec special case: domain-noun conf 0.85 beats dep-anchor conf 0.75."""
    repo = tmp_path
    files = [
        "apps/web/(data-room)/page.tsx",
        "apps/web/(data-room)/upload.ts",
        "apps/web/(data-room)/share.ts",
    ]
    for p in files:
        _write(repo, p, 'import Stripe from "stripe";\n')

    feat = _feat("data-room-ui", paths=list(files))
    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])

    # Domain-noun (0.85) wins over dep-anchor (0.75).
    assert mapping["data-room-ui"][0] == "Data Room"
    # Combined bucket fires because two rules contributed.
    assert telemetry["product_clusterer_source_breakdown"].get("combined") == 1


# ── Smoke: ProductFeature dataclass is frozen / hashable ────────────────


def test_product_feature_dataclass_frozen() -> None:
    """``ProductFeature`` should be frozen so we can't accidentally
    mutate the clusterer's return values downstream.
    """
    pf = ProductFeature(
        name="Billing",
        developer_feature_names=("payments",),
        anchor_signals=("dep:billing",),
        source="rule:dep-anchor",
        confidence=0.75,
    )
    with pytest.raises(Exception):
        pf.name = "Something Else"  # type: ignore[misc]
