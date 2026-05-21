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


def test_domain_noun_fires_on_flat_repo_layout(tmp_path: Path) -> None:
    """Non-monorepo repos still get domain-noun labelling.

    Papermark-shape: no ``apps/<X>/`` workspace prefix. The flat-layout
    branch runs domain-noun from the repo root with prefix="" and emits
    a workspace+domain vote when a route group or first-non-generic
    dir token wins.
    """
    repo = tmp_path
    for p in (
        "app/(documents)/page.tsx",
        "app/(documents)/upload/page.tsx",
        "app/(documents)/share/page.tsx",
    ):
        _write(repo, p, "// empty")

    feat = _feat("documents-ui", paths=[
        "app/(documents)/page.tsx",
        "app/(documents)/upload/page.tsx",
        "app/(documents)/share/page.tsx",
    ])

    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])
    assert mapping["documents-ui"] == ("Documents",)
    assert telemetry["product_clusterer_votes_cast"]["workspace+domain"] == 1


def test_domain_noun_beats_dep_anchor_when_higher_confidence(tmp_path: Path) -> None:
    """Spec: domain-noun conf 0.85 beats dep-anchor conf 0.75.

    Sprint Rails H3 amends this: the dep-anchor only fires when the
    feature name/path basenames match the dep category's name aliases.
    A ``data-room-ui`` feature carries no billing/payment alias even
    though it imports stripe, so dep-anchor (Billing) does NOT vote.
    Domain-noun (Data Room) wins uncontested, but the source breakdown
    is now ``rule:workspace+domain`` rather than ``combined``.
    """
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

    # Domain-noun wins.
    assert mapping["data-room-ui"][0] == "Data Room"
    # Only the workspace+domain rule cast a vote (H3 blocked Billing).
    breakdown = telemetry["product_clusterer_source_breakdown"]
    assert breakdown.get("rule:workspace+domain") == 1
    assert breakdown.get("combined", 0) == 0


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


# ── Sprint E2 — phantom-cluster filter ──────────────────────────────────


def test_phantom_workspace_cluster_is_dropped(tmp_path: Path) -> None:
    """A workspace named ``packages/tsconfig/`` (Title-Cased to
    ``"Tsconfig"``) is a known phantom and must NOT emit a Layer 2
    product feature via Rule 1.

    Uses ``packages/tsconfig/`` rather than ``apps/ai/`` because the
    domain-noun extractor produces a more interesting label for the
    latter; ``Tsconfig`` is unambiguous structural junk that ONLY
    fires through the workspace fallback.
    """
    repo = tmp_path
    paths = [
        "packages/tsconfig/base.json",
        "packages/tsconfig/nextjs.json",
        "packages/tsconfig/package.json",
        "packages/tsconfig/react.json",
        "packages/tsconfig/node.json",
    ]
    for p in paths:
        _write(repo, p, "{}")

    feat = _feat("tsconfig", paths=paths)

    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])

    assert mapping == {}, (
        "phantom workspace label 'Tsconfig' must not produce a product "
        "feature via the workspace rule"
    )
    assert products == []
    assert telemetry["product_features_count"] == 0
    assert telemetry["developer_features_orphan_count"] == 1


def test_legitimate_workspace_cluster_still_emits(tmp_path: Path) -> None:
    """A workspace named ``apps/settings/`` (Title-Cased to "Settings")
    is NOT in the phantom set and must still emit.
    """
    repo = tmp_path
    paths = [
        "apps/settings/page.tsx",
        "apps/settings/team.tsx",
        "apps/settings/profile.tsx",
        "apps/settings/billing.tsx",
        "apps/settings/api.tsx",
    ]
    for p in paths:
        _write(repo, p, "// empty")

    feat = _feat("settings", paths=paths)

    products, mapping, telemetry = run_product_clusterer(_ctx(repo), [feat])

    assert "settings" in mapping, "non-phantom labels must still cluster"
    # Settings is the workspace fallback; domain-noun may or may not
    # promote the label depending on the path layout. Either name is
    # acceptable as long as a cluster fires.
    assert len(products) >= 1
    assert telemetry["product_features_count"] >= 1


def test_phantom_filter_uses_frozenset_as_sole_source_of_truth() -> None:
    """The phantom skip-list lives in ``_PHANTOM_CLUSTER_NAMES`` and is
    used by NO other path (no duplicate hard-coded checks elsewhere in
    the module). Verifying via direct module attribute import — if a
    future contributor adds an inline ``if label == "AI": continue``
    branch this test catches the duplication only by inspection, but
    we at least pin the contract that the constant exists and is a
    frozenset (so callers can't mutate it).
    """
    from faultline.pipeline_v2 import stage_6_5_product_clusterer as mod

    assert hasattr(mod, "_PHANTOM_CLUSTER_NAMES")
    assert isinstance(mod._PHANTOM_CLUSTER_NAMES, frozenset)
    # Sanity: must contain at least the four reference categories.
    for sample in ("Ai", "Packages", "Tsconfig", "All"):
        assert sample in mod._PHANTOM_CLUSTER_NAMES, (
            f"expected {sample!r} in _PHANTOM_CLUSTER_NAMES; "
            "the constant is the only source of truth for the skip-list"
        )
    # Sanity: must NOT contain plausible legitimate product names.
    # "Api" and "Web" are explicitly excluded — they often appear as
    # workspace folders that DO name real product surfaces (Dub API,
    # Web Analytics) and dropping them would suppress recall.
    for sample in ("Settings", "Billing", "Auth", "Email", "Admin", "Api", "Web"):
        assert sample not in mod._PHANTOM_CLUSTER_NAMES, (
            f"{sample!r} is a legitimate product feature name; "
            "adding it to the phantom set would suppress real surfaces"
        )


def test_phantom_set_contains_e2_v2_corpus_additions() -> None:
    """Sprint E2 v2 extended the phantom set with 21 corpus-evidence
    names harvested from a 17-repo audit. Pin the contract that all
    additions remain in the frozenset so a future contributor can't
    silently drop one. Names are grouped by the 4 existing category
    comments (infra / folder / build / catchall).
    """
    from faultline.pipeline_v2 import stage_6_5_product_clusterer as mod

    e2_v2_additions = {
        # infra
        "Logs", "Storage",
        # universal folders
        "Examples", "Templates", "Template", "Mocks", "Fixtures",
        "Demo", "Documentation", "Constants", "Hooks", "Schema",
        "Assets", "Configs",
        # build/CI
        "Builds", "Github", "Yarn", "Procfile",
        # catchalls
        "V1", "Sandbox", "Browser", "Old", "Defaults",
    }
    missing = e2_v2_additions - mod._PHANTOM_CLUSTER_NAMES
    assert not missing, (
        f"Sprint E2 v2 corpus-evidence additions missing from "
        f"_PHANTOM_CLUSTER_NAMES: {sorted(missing)}"
    )


def test_phantom_workspace_does_not_block_dep_anchor_fallback(tmp_path: Path) -> None:
    """When a dev feature both lives under a phantom workspace AND
    imports a known dep, the dep-anchor rule must still emit a
    legitimate cluster — the phantom filter applies ONLY to Rule 1.
    """
    repo = tmp_path
    # 5 paths under packages/tsconfig/ (phantom workspace) but ALL
    # importing stripe. Synthetic, but it exercises the rescue path.
    paths = [
        "packages/tsconfig/charge.ts",
        "packages/tsconfig/invoice.ts",
        "packages/tsconfig/refund.ts",
        "packages/tsconfig/subscription.ts",
        "packages/tsconfig/webhook.ts",
    ]
    for p in paths:
        _write(repo, p, 'import Stripe from "stripe";\nconst s = new Stripe();\n')

    feat = _feat("billing-engine", paths=paths)

    products, mapping, _ = run_product_clusterer(_ctx(repo), [feat])

    # Workspace label "Tsconfig" was suppressed by the phantom filter;
    # the dep-anchor rule still mapped the feature to "Billing".
    assert mapping.get("billing-engine") == ("Billing",)
    assert any(pf.name == "Billing" for pf in products)
    assert not any(pf.name == "Tsconfig" for pf in products)


# ── Sprint E5 — name-specificity tiebreaker ─────────────────────────────


def test_label_specificity_counts_non_stopword_tokens() -> None:
    """Sprint E5: specificity helper drops stopwords + counts the rest."""
    from faultline.pipeline_v2.stage_6_5_product_clusterer import _label_specificity

    assert _label_specificity("AI") == 1
    assert _label_specificity("AI Email Assistant") == 3
    assert _label_specificity("The Database") == 1   # 'the' is a stopword
    assert _label_specificity("Magic Link Sign-In") == 3
    assert _label_specificity("Billing & Subscriptions") == 2
    assert _label_specificity("") == 0


def test_label_specificity_tiebreaks_equal_confidence_and_weight() -> None:
    """Sprint E5: when two labels tie on (confidence, weight), the more
    specific multi-word label wins. Generic 'AI' loses to specific
    'AI Email Assistant'."""
    from faultline.pipeline_v2.stage_6_5_product_clusterer import (
        _resolve_votes,
        _Vote,
    )

    # Two votes for the same dev feature, both confidence=0.6, weight=1.0.
    # Without specificity tiebreaker, dict-iteration order decides
    # (unstable across Python versions). With E5, "AI Email Assistant"
    # (specificity=3) beats "AI" (specificity=1).
    votes = [
        _Vote(product_label="AI", rule="workspace",
              confidence=0.6, anchor_signal="apps/ai", weight=1.0),
        _Vote(product_label="AI Email Assistant", rule="workspace",
              confidence=0.6, anchor_signal="apps/ai-email-assistant", weight=1.0),
    ]
    winners, _ = _resolve_votes(votes)
    # Specificity must put the more-specific label first. (When
    # weights are exactly tied, ambiguous-lead may also surface the
    # generic one as a secondary — that's acceptable as long as the
    # specific label is the primary winner.)
    assert winners[0] == "AI Email Assistant"


def test_label_specificity_breaks_ties_when_weight_clear() -> None:
    """Sprint E5: when ONE label has a clear weight lead (≥2x), the
    other doesn't get surfaced — even if it's more specific.
    Specificity is a TIEBREAKER on equal (conf, weight), not an
    override on weight."""
    from faultline.pipeline_v2.stage_6_5_product_clusterer import (
        _resolve_votes,
        _Vote,
    )

    votes = [
        # Generic "AI" with 3x weight share — clear winner on weight alone.
        _Vote(product_label="AI", rule="workspace",
              confidence=0.6, anchor_signal="apps/ai/a", weight=1.0),
        _Vote(product_label="AI", rule="workspace",
              confidence=0.6, anchor_signal="apps/ai/b", weight=1.0),
        _Vote(product_label="AI", rule="workspace",
              confidence=0.6, anchor_signal="apps/ai/c", weight=1.0),
        # Specific "AI Email Assistant" with weight 1.0 — would lose
        # on the dominant weight even though it's more specific.
        _Vote(product_label="AI Email Assistant", rule="workspace",
              confidence=0.6, anchor_signal="apps/ai-email-assistant", weight=1.0),
    ]
    winners, _ = _resolve_votes(votes)
    assert winners == ["AI"]  # weight lead 3:1, no ambiguity, AI alone


def test_label_specificity_does_not_overturn_higher_confidence() -> None:
    """Sprint E5: specificity is a TIEBREAKER only — it never beats
    a higher-confidence rule. A specific workspace label still loses
    to a less-specific dep-anchor label when confidence differs."""
    from faultline.pipeline_v2.stage_6_5_product_clusterer import (
        _resolve_votes,
        _Vote,
    )

    votes = [
        # Generic dep-anchor label, higher confidence (0.8)
        _Vote(product_label="AI", rule="dep-anchor",
              confidence=0.8, anchor_signal="dep:openai", weight=0.5),
        # Specific workspace label, lower confidence (0.6)
        _Vote(product_label="AI Email Assistant Module", rule="workspace",
              confidence=0.6, anchor_signal="apps/ai-email-assistant", weight=1.0),
    ]
    winners, _ = _resolve_votes(votes)
    # dep-anchor wins on confidence regardless of specificity.
    assert winners[0] == "AI"
