"""Sprint 18 — Few-shot examples for ``deep_scan`` system prompt.

Modern LLMs (especially Sonnet 4.6) reliably absorb output-shape and
domain-vocabulary patterns from in-context examples. Showing the model
"here's how a Next.js monorepo gets broken into features" raises
accuracy more than refining instructions does.

Public surface
==============

    EXAMPLES_BY_STACK : dict[str, list[FewShotExample]]
        Stack tag → list of curated examples. Stack tags match
        ``ground_truth.json`` ``stack`` field.

    pick_examples(stack_hint, max_count=3, max_tokens=4000) -> str
        Render selected examples as a single ``<example>...</example>``
        block to splice into the system prompt. Picks examples whose
        ``stack`` matches first, then fills with ``mixed`` examples.

    selection diagnostics: returned with chosen example names so the
        scan log records which few-shots were used.

Curation rules (S18 Day 1, when filling these in)
==================================================

  1. Source: ground-truth maps from ``tests/eval/ground_truth.json``
     where the eval F1 is in the top quartile for that stack.
  2. Diversity: at most one example per repo, 5+ stacks represented.
  3. Trim: ``file_paths_sample`` capped at 12 paths per example so
     total prompt budget stays under +6K tokens (S18 acceptance gate).
  4. Output shape: each example shows the JSON shape Sonnet should
     return (merge/rename/remove/split/features keys).
  5. No leakage: never include an example whose repo is in the eval
     corpus AND will be re-scored during S18 A/B comparison —
     contamination inflates measured uplift.

Format
======

Each example is a ``FewShotExample`` dataclass with:
  - ``stack``: tag matching ground_truth.json (e.g. "next-monorepo")
  - ``repo``: source repo name (for traceability / dedup)
  - ``file_paths_sample``: 8-12 representative paths
  - ``expected_output``: JSON envelope showing the desired shape
  - ``rationale``: 1-2 lines on why this is a good teaching example

The rendered block looks like::

    <example>
    <repo>documenso</repo>
    <stack>next-monorepo</stack>
    <input_files>
    apps/web/src/app/(signed-in)/documents/...
    apps/web/src/app/(signed-in)/templates/...
    packages/lib/server-only/auth/...
    </input_files>
    <expected_output>
    {"merge": [...], "features": [...]}
    </expected_output>
    </example>

Multiple examples are joined with double newlines.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Data shape ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FewShotExample:
    """One curated input/output pair for in-context learning."""

    stack: str  # matches ground_truth.json stack tag
    repo: str
    file_paths_sample: list[str]
    expected_output: dict
    rationale: str = ""

    def render(self) -> str:
        """Return the example formatted for the system prompt."""
        paths = "\n".join(self.file_paths_sample)
        out_json = json.dumps(self.expected_output, indent=2, ensure_ascii=False)
        return (
            f"<example>\n"
            f"<repo>{self.repo}</repo>\n"
            f"<stack>{self.stack}</stack>\n"
            f"<input_files>\n{paths}\n</input_files>\n"
            f"<expected_output>\n{out_json}\n</expected_output>\n"
            f"</example>"
        )


# ── Curated examples ──────────────────────────────────────────────────
#
# Sprint 18 Day 1 task: replace these placeholders with real examples
# pulled from ground_truth.json. Each example below is a STRUCTURAL
# template — shape is correct, content is illustrative.
#
# When filling in:
#   1. Pick repo with eval F1 ≥ 70% in its stack class (run S17 eval first)
#   2. Pull ~12 representative file paths from its scan result
#   3. Pull the consolidated feature list from ground_truth.json
#   4. Verify rendered token count: each example ~800-1200 tokens
#   5. Run unit test ``test_pick_examples_under_budget``


_NEXT_MONOREPO_EXAMPLES: list[FewShotExample] = [
    FewShotExample(
        stack="next-monorepo",
        repo="documenso",
        file_paths_sample=[
            "apps/web/src/app/(signed-in)/documents/page.tsx",
            "apps/web/src/app/(signed-in)/documents/[id]/edit/page.tsx",
            "apps/web/src/app/(signed-in)/templates/page.tsx",
            "apps/web/src/app/(signed-in)/teams/[teamId]/settings/page.tsx",
            "packages/lib/server-only/auth/oauth.ts",
            "packages/lib/server-only/document/sign-document.ts",
            "packages/lib/server-only/recipient/get-recipient-by-token.ts",
            "packages/lib/server-only/template/duplicate-template.ts",
            "packages/lib/server-only/team/create-team.ts",
            "packages/lib/server-only/billing/stripe-webhook.ts",
            "packages/email/templates/document-pending.tsx",
            "packages/api/v1/openapi.ts",
        ],
        expected_output={
            "merge": [],
            "rename": [],
            "remove": [],
            "split": [],
            "features": [
                {
                    "name": "documents",
                    "description": "Document upload, signing workflow, recipient management, and audit trail.",
                    "flows": [
                        {"name": "upload-document-flow", "description": "User uploads a PDF to start a signing workflow."},
                        {"name": "send-for-signing-flow", "description": "Add recipients and send document for signature."},
                        {"name": "sign-document-flow", "description": "Recipient receives email and signs via tokenized link."},
                    ],
                },
                {
                    "name": "templates",
                    "description": "Reusable document templates with placeholder fields.",
                    "flows": [
                        {"name": "create-template-flow", "description": "Build a template with field placeholders."},
                        {"name": "duplicate-template-flow", "description": "Clone existing template as starting point."},
                    ],
                },
                {
                    "name": "teams",
                    "description": "Multi-user workspaces with shared documents.",
                    "flows": [
                        {"name": "create-team-flow", "description": "Owner creates a team workspace."},
                        {"name": "invite-team-member-flow", "description": "Send invitation email to teammate."},
                    ],
                },
                {
                    "name": "authentication",
                    "description": "Login, signup, OAuth via Google/GitHub.",
                    "flows": [
                        {"name": "sign-in-flow", "description": "Email/password or OAuth sign-in."},
                        {"name": "sign-up-flow", "description": "Account creation with email verification."},
                    ],
                },
                {
                    "name": "billing",
                    "description": "Stripe subscriptions and invoicing.",
                    "flows": [
                        {"name": "subscribe-flow", "description": "Convert free user to paid subscription."},
                        {"name": "process-stripe-webhook-flow", "description": "Handle Stripe events for subscription state."},
                    ],
                },
            ],
        },
        rationale=(
            "Clean turborepo split (apps/* + packages/*). Demonstrates: "
            "(a) folder = feature mapping, (b) shared packages don't get "
            "their own feature when they back a domain feature, "
            "(c) auth/billing are separate features even when small."
        ),
    ),
    # TODO S18 Day 1: add 1 more next-monorepo example (dub or trigger.dev
    #                 once S17 eval shows F1 ≥ 70%)
]


_VUE_SPA_EXAMPLES: list[FewShotExample] = [
    # CRITICAL stack: addresses the uptime-kuma 67% regression.
    # Vue SPAs have flat src/components/ — features must be inferred
    # from filename suffix patterns (TagsManager, BadgeDialog), not
    # directory structure.
    FewShotExample(
        stack="vue-spa",
        repo="uptime-kuma",
        file_paths_sample=[
            "src/components/TagsManager.vue",
            "src/components/Tag.vue",
            "src/components/TagEditDialog.vue",
            "src/components/BadgeLinkGeneratorDialog.vue",
            "src/components/StatusPage.vue",
            "src/components/MaintenanceList.vue",
            "src/components/CertificateInfo.vue",
            "src/pages/Manage2FA.vue",
            "src/pages/ManageAPIKey.vue",
            "server/prometheus.js",
            "server/notification.js",
            "server/monitor-types/http.js",
        ],
        expected_output={
            "merge": [],
            "rename": [],
            "remove": [],
            "split": [],
            "features": [
                {
                    "name": "monitors",
                    "description": "Monitor types (HTTP, TCP, ping, DNS, push) with retry and uptime tracking.",
                    "flows": [
                        {"name": "create-monitor-flow", "description": "Define a new monitor with type, target, interval."},
                        {"name": "view-monitor-history-flow", "description": "View uptime/response chart for a monitor."},
                    ],
                },
                {
                    "name": "tags",
                    "description": "Group monitors with tags for filtering and organization.",
                    "flows": [
                        {"name": "manage-tags-flow", "description": "Create/rename/delete tags for grouping."},
                        {"name": "tag-monitor-flow", "description": "Apply tags to a monitor."},
                    ],
                },
                {
                    "name": "badges",
                    "description": "Embeddable status badges (SVG/PNG) for external sites.",
                    "flows": [
                        {"name": "generate-badge-flow", "description": "Configure badge style and copy embed link."},
                    ],
                },
                {
                    "name": "certificate-monitoring",
                    "description": "TLS/SSL certificate expiry tracking and alerts.",
                    "flows": [
                        {"name": "view-certificate-info-flow", "description": "Inspect certificate expiry and chain."},
                    ],
                },
                {
                    "name": "prometheus-metrics",
                    "description": "Prometheus exporter endpoint for monitor metrics.",
                    "flows": [
                        {"name": "scrape-metrics-flow", "description": "External Prometheus scrapes /metrics."},
                    ],
                },
            ],
        },
        rationale=(
            "Vue SPA stress test. Files in flat src/components/ with NO "
            "feature folders. Model must learn: filename prefix patterns "
            "(Tag*, Badge*, Certificate*) ARE the features, even when only "
            "1-2 files exist. Don't fold them into bigger neighbors."
        ),
    ),
]


_PYTHON_FLAT_EXAMPLES: list[FewShotExample] = [
    # TODO S18 Day 1: pick best Python example after S17 eval.
    # Likely candidate: fastapi (library mode) or apprise (flat lib).
]


_GO_MODULAR_EXAMPLES: list[FewShotExample] = [
    # TODO S18 Day 1: pick best Go example after S17 eval.
    # Likely candidate: gitea (good package layout) or ollama.
]


_MIXED_FALLBACK_EXAMPLES: list[FewShotExample] = [
    # Generic example used when stack tag is unknown or a niche stack
    # (Rust, Rails, mixed). Should be neutral re: directory conventions.
    # TODO S18 Day 1: curate after S17 eval reveals neutral best repo.
]


EXAMPLES_BY_STACK: dict[str, list[FewShotExample]] = {
    "next-monorepo": _NEXT_MONOREPO_EXAMPLES,
    "next-app-router": _NEXT_MONOREPO_EXAMPLES,  # share examples; pattern is similar
    "vue-spa": _VUE_SPA_EXAMPLES,
    "vue-nuxt-monorepo": _VUE_SPA_EXAMPLES,
    "python-flat": _PYTHON_FLAT_EXAMPLES,
    "python-modules": _PYTHON_FLAT_EXAMPLES,
    "go-modular": _GO_MODULAR_EXAMPLES,
    "rust-modular": _MIXED_FALLBACK_EXAMPLES,
    "rails-app": _MIXED_FALLBACK_EXAMPLES,
    "node-monorepo": _NEXT_MONOREPO_EXAMPLES,
    "mixed": _MIXED_FALLBACK_EXAMPLES,
}


# ── Renderer / picker ─────────────────────────────────────────────────


# Conservative bound: 4 examples × ~1000 tokens each = 4K, leaving room
# for instruction prompt + per-call user message. Empirical: Sonnet 4.6
# has 200K context, but accuracy degrades past ~50K of system prompt.
_DEFAULT_MAX_EXAMPLES = 3
_DEFAULT_MAX_TOKENS = 4_000

# 1 token ≈ 3.5 chars for English+code mix; safe upper bound for budget.
_CHARS_PER_TOKEN_APPROX = 3.5


def _estimate_tokens(text: str) -> int:
    return int(len(text) / _CHARS_PER_TOKEN_APPROX) + 1


def pick_examples(
    stack_hint: str | None,
    *,
    max_count: int = _DEFAULT_MAX_EXAMPLES,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> tuple[str, list[str]]:
    """Render up to ``max_count`` examples for ``stack_hint``, capped by tokens.

    Selection order:
      1. Examples whose ``stack`` matches ``stack_hint``.
      2. Fill remaining slots from ``mixed`` fallback examples.
      3. Stop adding once cumulative token estimate exceeds ``max_tokens``.

    Returns:
      (rendered_block, picked_repo_names) — the second item is for
      logging / debug ("scan used few-shots from: documenso, uptime-kuma").
    """
    primary = list(EXAMPLES_BY_STACK.get(stack_hint or "mixed", []))
    fallback = [
        ex for ex in EXAMPLES_BY_STACK.get("mixed", [])
        if ex not in primary
    ]
    candidates = primary + fallback

    chosen: list[FewShotExample] = []
    used_tokens = 0
    for ex in candidates:
        if len(chosen) >= max_count:
            break
        rendered = ex.render()
        cost = _estimate_tokens(rendered)
        if used_tokens + cost > max_tokens:
            logger.debug(
                "few_shot: skipping %s (would overshoot budget %d > %d)",
                ex.repo, used_tokens + cost, max_tokens,
            )
            continue
        chosen.append(ex)
        used_tokens += cost

    if not chosen:
        return ("", [])

    block = "\n\n".join(ex.render() for ex in chosen)
    repos = [ex.repo for ex in chosen]
    logger.info(
        "few_shot: stack=%s picked=%s tokens=%d",
        stack_hint, repos, used_tokens,
    )
    return (block, repos)


# ── Wiring helper ─────────────────────────────────────────────────────


_FEW_SHOT_INTRO = """\
## Examples

Below are real-world examples showing how to decompose repos of \
different stacks into features. Each shows a sample of input file \
paths and the expected output JSON envelope. Match this style and \
shape — produce features at the same granularity, naming convention, \
and flow detail level.\
"""


def build_examples_block(stack_hint: str | None) -> tuple[str, list[str]]:
    """Build a complete ``## Examples`` section for splicing into the prompt.

    Returns ``("", [])`` when no examples are available for this stack
    (caller should skip the section entirely rather than emit a header
    with nothing under it).
    """
    rendered, repos = pick_examples(stack_hint)
    if not rendered:
        return ("", [])
    return (f"{_FEW_SHOT_INTRO}\n\n{rendered}\n", repos)
