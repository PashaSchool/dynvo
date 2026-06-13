"""Anti-hallucination naming validator — deterministic, post-LLM.

Names shipped by the LLM namers (Stage 8 analyst / haiku clusterer /
Stage 6.7b UF refiner) sometimes contain tokens with NO evidence in the
entity they name ("opentelemetry" for a feature whose files never
mention OTel). This module enforces a simple contract AFTER every LLM
naming call:

  every CONTENT token of the produced name must appear somewhere in
  that entity's evidence bundle (file paths, symbols, product strings,
  route paths, commit messages, external marketing labels).

Rules (all deterministic, documented here):

  * **Tokenization** — names are split on kebab/snake/space/camelCase
    boundaries, lowercased, singularized (plural ``s`` stripped), and
    stop-words dropped. Stop-words are grammatical glue + generic
    product fillers ("management", "support", "system") that any name
    may carry without evidence.
  * **Matching** — a name token matches when it equals an evidence
    token OR shares a prefix-stem with one (≥4 chars: "auth" matches
    "authentication", "embed" matches "embedding"). Substring matching
    is deliberately NOT used both ways — "otel" must not pass because
    "hotel" is in a path.
  * **Vendor rule** — known vendor/brand tokens (stripe, aws, slack…)
    pass ONLY when they dominate the entity's evidence: the vendor
    token must appear in the per-file evidence of at least
    ``max(2, ceil(len(files) * 1/4))`` member files. One stray import
    of an SDK does not make the feature "the Stripe feature". The 1/4
    share is structural (same spirit as Stage 8.5's majority-overlap
    threshold), not corpus-tuned; the floor of 2 keeps single-file
    entities honest.
  * **Failure protocol** — the caller makes ONE retry with an explicit
    prohibition appended to the prompt (:func:`retry_prohibition`);
    on a second failure it falls back to the deterministic slug and
    stamps ``name_confidence="low"``. This module only judges; the
    stages own the retry loop.

No LLM calls, no IO — pure functions over strings, so the unit tests
fully cover the validator even on keyless scans.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "EvidenceBundle",
    "NameValidation",
    "STOP_WORDS",
    "VENDOR_TOKENS",
    "retry_prohibition",
    "tokenize_name",
    "validate_name",
]


# Grammatical glue + generic product fillers any name may use without
# code evidence. Universal English, not tuned to any repo.
STOP_WORDS = frozenset({
    # articles / conjunctions / prepositions
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "of", "on", "or", "over", "per", "the", "their", "through", "to",
    "via", "with", "without", "your",
    # generic product fillers
    "core", "support", "management", "manage", "system", "service",
    "services", "feature", "features", "module", "tool", "tools",
    "platform", "engine", "integration", "integrations", "workflow",
    "workflows", "functionality", "capabilities", "experience",
    "builtin", "built", "advanced", "smart", "custom", "multi",
    "based", "full", "new", "general", "common", "misc", "other",
    # journey-template verbs the deterministic namers already emit
    "create", "edit", "browse", "filter", "view", "run", "transition",
    "lifecycle", "bulk", "export", "its",
})

# Known vendor / brand tokens — pass only under the domination rule.
# Universal SaaS-ecosystem vocabulary, not derived from any one repo.
VENDOR_TOKENS = frozenset({
    "stripe", "paypal", "paddle", "lemonsqueezy",
    "aws", "gcp", "azure", "cloudflare", "vercel", "netlify", "fly",
    "github", "gitlab", "bitbucket",
    "slack", "discord", "teams", "telegram", "whatsapp", "twilio",
    "google", "microsoft", "apple", "facebook", "meta", "linkedin",
    "okta", "auth0", "clerk", "keycloak",
    "sentry", "posthog", "datadog", "grafana", "prometheus",
    "opentelemetry", "otel", "newrelic",
    "openai", "anthropic", "claude", "gemini", "mistral", "ollama",
    "salesforce", "hubspot", "zendesk", "intercom", "crowdstrike",
    "redis", "kafka", "elasticsearch", "algolia", "supabase",
    "firebase", "mongodb", "postgres", "mysql", "sqlite", "prisma",
    "resend", "sendgrid", "mailgun", "postmark", "ses",
    "inngest", "temporal", "airflow",
    "plaid", "shopify", "zapier", "notion", "airtable", "figma",
})

# Vendor-domination share: vendor token must appear in the evidence of
# at least max(2, ceil(n_files / 4)) member files. Structural ratio —
# see module docstring.
_VENDOR_MIN_SHARE = 0.25
_VENDOR_MIN_FILES = 2

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_MIN_STEM_LEN = 4


def _singular(word: str) -> str:
    """Light, dependency-free singularisation for name-token comparison.

    Conservative on the cases that produced garbage tokens before:
      * ``-us`` / ``-is`` / ``-ss`` words are ALREADY singular — never strip
        (status→status, focus→focus, analysis→analysis, address→address).
        Naively stripping the trailing ``s`` gave ``statu`` / ``focu`` which
        no consumer matches.
      * ``-es`` only collapses to its stem when the stem ends in a sibilant
        (classes→class, boxes→box, matches→match); plain ``-?es`` words keep
        their ``e`` (cases→case, phases→phase). The old ``-ses`` rule wrongly
        ate the ``e`` (cases→cas).
    Kept in sync with the identical helper in ``nav_taxonomy._singular``.
    """
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"          # categories → category
    if word.endswith(("ss", "us", "is", "ous", "ius")):
        return word                     # status, focus, analysis, address
    if word.endswith(("sses", "shes", "ches", "xes", "zzes")):
        return word[:-2]                # classes → class, matches → match
    if word.endswith("s"):
        return word[:-1]                # cases → case, users → user, keys → key
    return word


def _split_tokens(text: str) -> list[str]:
    """Kebab/snake/space/camelCase split → lowercase tokens."""
    spaced = _CAMEL_RE.sub(" ", text)
    return [t.lower() for t in _SPLIT_RE.split(spaced) if t]


def tokenize_name(name: str) -> list[str]:
    """Content tokens of a produced name — split, singularized,
    stop-words and pure numbers dropped. Order-preserving, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for t in _split_tokens(name):
        t = _singular(t)
        if t in STOP_WORDS or t.isdigit() or len(t) < 2:
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _tokenize_evidence_text(text: str) -> set[str]:
    return {_singular(t) for t in _split_tokens(text)}


@dataclass
class EvidenceBundle:
    """Everything an entity's name may legitimately draw vocabulary from.

    ``file_evidence`` maps each member file to the tokens derived from
    that file (its path segments + its product strings + its symbols).
    The per-file granularity exists ONLY for the vendor-domination rule;
    plain tokens are matched against the flat union.

    ``global_evidence`` carries entity-level (not per-file) sources:
    commit messages, route patterns, member flow/feature names, and the
    external marketing taxonomy (an allowed grounding surface).
    """

    file_evidence: dict[str, set[str]] = field(default_factory=dict)
    global_evidence: set[str] = field(default_factory=set)

    # ── builders ────────────────────────────────────────────────────

    def add_file(self, path: str, *texts: str) -> None:
        toks = self.file_evidence.setdefault(path, set())
        toks.update(_tokenize_evidence_text(path))
        for t in texts:
            toks.update(_tokenize_evidence_text(t))

    def add_global(self, *texts: str) -> None:
        for t in texts:
            self.global_evidence.update(_tokenize_evidence_text(t))

    def add_file_tokens(self, path: str, tokens: set[str]) -> None:
        """Attach PRE-TOKENIZED evidence (e.g. commit-message tokens
        precomputed once per scan) to one member file."""
        toks = self.file_evidence.setdefault(path, set())
        toks.update(_tokenize_evidence_text(path))
        toks.update(tokens)

    # ── views ───────────────────────────────────────────────────────

    @property
    def all_tokens(self) -> set[str]:
        out = set(self.global_evidence)
        for toks in self.file_evidence.values():
            out |= toks
        return out

    @property
    def is_poor(self) -> bool:
        """Structurally poor bundle — nothing beyond bare tokens to name
        from. Definition: no per-file evidence at all (no member files
        contributed any vocabulary)."""
        return not self.file_evidence and not self.global_evidence

    def vendor_file_share(self, token: str) -> int:
        """Number of member files whose evidence carries ``token``."""
        return sum(1 for toks in self.file_evidence.values() if token in toks)


@dataclass
class NameValidation:
    """Outcome of one name check."""

    ok: bool
    missing_tokens: list[str] = field(default_factory=list)
    vendor_violations: list[str] = field(default_factory=list)

    @property
    def all_violations(self) -> list[str]:
        return self.missing_tokens + self.vendor_violations


def _token_matches(token: str, evidence: set[str]) -> bool:
    if token in evidence:
        return True
    if len(token) >= _MIN_STEM_LEN:
        for ev in evidence:
            if len(ev) >= _MIN_STEM_LEN and (
                ev.startswith(token) or token.startswith(ev)
            ):
                return True
    return False


def validate_name(name: str, bundle: EvidenceBundle) -> NameValidation:
    """Check every content token of ``name`` against the evidence bundle.

    Vendor tokens additionally require domination (see module docstring);
    a vendor token that fails domination is reported in
    ``vendor_violations`` even when it appears somewhere in the evidence.
    """
    evidence = bundle.all_tokens
    missing: list[str] = []
    vendor_bad: list[str] = []
    n_files = len(bundle.file_evidence)
    vendor_floor = max(
        _VENDOR_MIN_FILES, math.ceil(n_files * _VENDOR_MIN_SHARE),
    ) if n_files else _VENDOR_MIN_FILES

    for token in tokenize_name(name):
        if token in VENDOR_TOKENS:
            if bundle.vendor_file_share(token) >= vendor_floor:
                continue
            vendor_bad.append(token)
            continue
        if not _token_matches(token, evidence):
            missing.append(token)

    return NameValidation(
        ok=not missing and not vendor_bad,
        missing_tokens=missing,
        vendor_violations=vendor_bad,
    )


def retry_prohibition(violations_by_name: dict[str, list[str]]) -> str:
    """Prompt suffix for the single allowed retry — explicit prohibition
    of the unsupported tokens, per offending name."""
    lines = [
        "",
        "NAMING EVIDENCE VIOLATIONS — the following names contain words "
        "with NO evidence in the entity's files, symbols, product strings, "
        "routes, or commit history. Re-emit the SAME JSON shape, renaming "
        "ONLY these entries using words that appear in the provided "
        "evidence. Do NOT use the prohibited words:",
    ]
    for name in sorted(violations_by_name):
        toks = ", ".join(sorted(violations_by_name[name]))
        lines.append(f'- "{name}": prohibited words: {toks}')
    return "\n".join(lines)
