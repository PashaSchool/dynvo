"""Recall-critique aggregator (Phase 5 of the 90-recall roadmap).

After the primary Sonnet scan produces a feature map, this aggregator
asks a second LLM pass: "Here is what was detected. Here are features
the codebase is KNOWN to have, from explicit out-of-band signals
(package deps, MVC controllers, domain models, route groups). Did the
primary scan miss any?"

The signals come from existing extractors:

  ``expected-feature`` (package-anchor-extractor)
      payload: ``feature_category``, ``severity``, ``evidence``,
      ``manifest``. Severity ``"must"`` deps are high-confidence
      anchors (Stripe → Billing, NextAuth → Auth).

  ``controller-action`` (mvc-controller-extractor:rails)
      payload: ``controller_name``, ``action``, ``controller_file``.
      Each unique controller is one expected category.

  ``domain-model`` (schema-domain-extractor)
      payload: ``name``, ``feature_hint``, ``file``. ``feature_hint``
      (when non-None) is the heuristic guess at which product feature
      owns the model.

  ``route`` (route-file-extractor:*)
      payload: ``parent_hint``, ``path``, ``framework``,
      ``handler_file``. ``parent_hint`` (Next route groups like
      ``(dashboard)`` / ``(auth)`` / ``(marketing)``) is treated as a
      feature category when present.

The pure-Python diff (``derive_expected_categories``,
``find_missing_categories``) is testable without an LLM and is the
load-bearing part of recall. The LLM step
(``CritiqueAggregator.run``) confirms or rejects each missing
category and produces a feature name + files. The aggregator is
opt-in: callers pass a real ``LlmClient`` only when they want the
second pass to run.

Why this exists vs. ``faultline.llm.critique`` (which already exists):
that module is the Sprint-5 NAMING critique — it renames weak feature
names. This one is the RECALL critique — it adds features that were
missed entirely. Orthogonal concerns, different prompts, different
inputs.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from faultline.signals import Signal

logger = logging.getLogger(__name__)


# ── Data shapes ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectedCategory:
    """One feature category the codebase is KNOWN to have from a
    signal source, used as ground-truth input to the recall critique.

    ``category`` is the canonical lowercased token used for matching
    against detected features. ``display`` keeps the original casing
    for the prompt. ``evidence`` is an ordered tuple of short strings
    suitable for the LLM prompt (e.g. ``"dep:stripe (package.json,
    severity=must)"``, ``"controller:MfaController"``,
    ``"model:Subscription (prisma)"``, ``"route-group:(dashboard)"``).
    ``severity`` collapses the source signal's confidence into one of
    ``"must"`` / ``"should"`` / ``"heuristic"``.
    """

    category: str
    display: str
    evidence: tuple[str, ...]
    severity: str
    source_kinds: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True, kw_only=True)
class CritiqueFinding:
    """One feature the critique pass confirmed was missed by the
    primary scan. Becomes a new entry in the DeepScanResult features
    dict with ``discovery_method=critique`` provenance.
    """

    feature_name: str
    files: tuple[str, ...]
    rationale: str
    matched_categories: tuple[str, ...]


# ── Pure derivation (no LLM, no IO) ──────────────────────────────────


_SEVERITY_RANK = {"heuristic": 0, "should": 1, "must": 2}

# Tokens that are too generic to drive a match on their own — they
# appear in nearly every product and would let an unrelated detected
# feature "satisfy" any expected category.
_STOP_TOKENS = frozenset({
    "the", "a", "an", "of", "for", "and", "to", "in", "on",
    "feature", "features", "service", "services", "module", "modules",
    "system", "systems", "page", "pages", "view", "views", "manager",
    "management", "controller", "controllers", "model", "models",
    "app", "apps", "api", "apis", "data",
})

_CONTROLLER_SUFFIX = re.compile(r"controller$", re.IGNORECASE)


def _tokenise(s: str) -> set[str]:
    """Split a feature/category string into lowercase content tokens.

    Splits on whitespace, ``-``, ``_``, ``/``, ``.`` and CamelCase
    boundaries. Filters out stop tokens.
    """
    # Insert spaces at CamelCase boundaries first.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    spaced = re.sub(r"[-_/.]+", " ", spaced)
    raw = spaced.lower().split()
    return {t for t in raw if t and t not in _STOP_TOKENS and len(t) > 1}


def _normalise_category(raw: str) -> str:
    """Canonical form for matching: tokens joined by ``-``, sorted.

    Sorted so ``"User Management"`` and ``"management-user"`` match.
    """
    toks = sorted(_tokenise(raw))
    return "-".join(toks)


def derive_expected_categories(
    signals: Iterable[Signal],
) -> list[ExpectedCategory]:
    """Reduce extractor signals to a deduplicated list of expected
    feature categories.

    Same category emitted from multiple signals (e.g. a Billing dep AND
    a Subscription model AND a SubscriptionsController) merges into a
    single ExpectedCategory with combined evidence and the highest
    severity across sources.
    """
    buckets: dict[str, dict] = {}

    for sig in signals:
        cat = _category_from_signal(sig)
        if cat is None:
            continue
        display, evidence_item, severity = cat
        key = _normalise_category(display)
        if not key:
            continue

        bucket = buckets.setdefault(key, {
            "display": display,
            "evidence": [],
            "severity": "heuristic",
            "kinds": set(),
        })
        if evidence_item not in bucket["evidence"]:
            bucket["evidence"].append(evidence_item)
        if _SEVERITY_RANK[severity] > _SEVERITY_RANK[bucket["severity"]]:
            bucket["severity"] = severity
            # Prefer the must-severity display when promoting.
            bucket["display"] = display
        bucket["kinds"].add(sig.kind)

    out: list[ExpectedCategory] = []
    for key, b in buckets.items():
        out.append(ExpectedCategory(
            category=key,
            display=b["display"],
            evidence=tuple(b["evidence"]),
            severity=b["severity"],
            source_kinds=frozenset(b["kinds"]),
        ))
    # Deterministic order: severity desc, then alphabetical key.
    out.sort(key=lambda e: (-_SEVERITY_RANK[e.severity], e.category))
    return out


def _category_from_signal(
    sig: Signal,
) -> tuple[str, str, str] | None:
    """Returns (display_name, evidence_string, severity) or None if
    the signal carries no usable category hint.
    """
    p = sig.payload

    if sig.kind == "expected-feature":
        cat = p.get("feature_category")
        if not isinstance(cat, str) or not cat:
            return None
        sev_raw = p.get("severity", "heuristic")
        severity = sev_raw if sev_raw in _SEVERITY_RANK else "heuristic"
        evidence_tuple = p.get("evidence", ())
        dep_label = ""
        if isinstance(evidence_tuple, tuple) and evidence_tuple:
            dep_label = str(evidence_tuple[0])
        manifest = p.get("manifest", "")
        ev = f"{dep_label} ({manifest}, severity={severity})" if dep_label \
            else f"package-anchor (severity={severity})"
        return cat, ev, severity

    if sig.kind == "controller-action":
        name = p.get("controller_name")
        if not isinstance(name, str) or not name:
            return None
        display = _CONTROLLER_SUFFIX.sub("", name).strip()
        if not display:
            return None
        f = p.get("controller_file", "")
        ev = f"controller:{name} ({f})" if f else f"controller:{name}"
        return display, ev, "should"

    if sig.kind == "domain-model":
        hint = p.get("feature_hint")
        if not isinstance(hint, str) or not hint:
            return None
        name = p.get("name", "")
        f = p.get("file", "")
        ev = f"model:{name} ({f})" if f else f"model:{name}"
        return hint, ev, "heuristic"

    if sig.kind == "route":
        hint = p.get("parent_hint")
        if not isinstance(hint, str) or not hint:
            return None
        # Strip Next.js route-group parens: "(dashboard)" → "dashboard".
        display = hint.strip("()")
        if not display:
            return None
        f = p.get("handler_file", "")
        framework = p.get("framework", "")
        ev = f"route-group:{hint} ({framework}: {f})" if f \
            else f"route-group:{hint}"
        return display, ev, "heuristic"

    if sig.kind == "server-actions-file":
        # One signal per Next.js Server Actions file. The file's
        # parent dir often names the feature (app/billing/actions.ts
        # → "billing"). Use the parent dir as the category display;
        # if that's a generic name like "actions", fall back to the
        # grandparent.
        f = p.get("file")
        if not isinstance(f, str) or not f:
            return None
        parts = Path(f).parts
        # Walk up until we find a non-generic segment.
        generic = {"actions", "server", "lib", "src", "app", "api", "pages"}
        display = ""
        for seg in reversed(parts[:-1]):  # skip the file itself
            if seg.lower() not in generic:
                display = seg
                break
        if not display:
            return None
        sample = p.get("sample_names", ())
        count = p.get("action_count", 0)
        ev = f"server-actions:{f} ({count} actions: {list(sample)[:4]})"
        return display, ev, "should"

    if sig.kind == "trpc-router-file":
        # tRPC router file. The router basename is the canonical
        # category name in tRPC convention (billing.ts → Billing
        # router).
        basename = p.get("router_basename")
        if not isinstance(basename, str) or not basename:
            return None
        # Skip generic basenames like "index", "router", "_app".
        if basename.lower() in {"index", "router", "_app", "root", "trpc"}:
            return None
        sample = p.get("sample_procedures", ())
        count = p.get("procedure_count", 0)
        ev = f"trpc-router:{basename} ({count} procedures: {list(sample)[:4]})"
        return basename, ev, "should"

    if sig.kind == "nav-link":
        # JSX nav-component output. The author wrote this label, so
        # it's a high-confidence customer-facing surface name. When
        # both label and href are present, prefer label as the
        # display (it IS the customer-facing phrase). When label is
        # empty (icon-only nav), fall back to the route's last
        # segment so we still have something.
        label = p.get("label")
        href = p.get("href", "")
        f = p.get("file", "")
        display: str | None = None
        if isinstance(label, str) and label.strip():
            display = label.strip()
        elif isinstance(href, str) and href:
            seg = href.rstrip("/").rsplit("/", 1)[-1]
            if seg:
                display = seg
        if not display:
            return None
        ev = f"nav-link:{display!r} → {href} ({f})" if f else f"nav-link:{display!r} → {href}"
        return display, ev, "should"

    if sig.kind == "plugin-system":
        # ONE signal per plugin directory describes the architectural
        # feature ("plugin extensibility", "notification delivery",
        # "provider integrations") — not N per-plugin features.
        # Ground-truth feature lists for plugin-based libraries
        # typically describe this as ONE horizontal capability.
        plugin_dir = p.get("plugin_dir")
        peer_count = p.get("peer_count", 0)
        if not isinstance(plugin_dir, str) or not plugin_dir:
            return None
        # Display: take the last path segment as the category name
        # (e.g. "apprise/plugins" → "plugins"). The aggregator's
        # token matcher uses "plugins" as a token; ground-truth
        # entries with "plugin" in name/aliases will match.
        leaf = plugin_dir.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if not leaf:
            return None
        # Peer count anchors the description so the LLM can decide
        # whether to add a new feature or confirm coverage.
        ev = f"plugin-system:{plugin_dir} ({peer_count} modules)"
        return leaf, ev, "should"

    return None


# ── Matching ──────────────────────────────────────────────────────────


def _detected_token_index(detected: Iterable[str]) -> dict[str, set[str]]:
    """Build ``{detected_feature_name: token_set}`` for the loose-match
    check.
    """
    return {name: _tokenise(name) for name in detected}


def is_category_covered(
    expected: ExpectedCategory,
    detected_tokens: dict[str, set[str]],
) -> str | None:
    """Return the detected feature name that covers ``expected``, or
    ``None`` if none does.

    Coverage rule (loose, intentionally generous to avoid false
    "missing" reports flooding the critique prompt):

    1. Exact normalised-category match wins.
    2. Otherwise, a detected feature covers the expected category if
       its tokens contain ALL non-stop tokens of the expected
       category. Singletons require an exact token match — a
       single-token expected category is only "covered" by a detected
       feature that explicitly mentions that token.
    """
    exp_tokens = _tokenise(expected.display)
    if not exp_tokens:
        return None

    for name, det_tokens in detected_tokens.items():
        if _normalise_category(name) == expected.category:
            return name
        if exp_tokens.issubset(det_tokens):
            return name
    return None


def find_missing_categories(
    expected: Iterable[ExpectedCategory],
    detected: Iterable[str],
) -> list[ExpectedCategory]:
    """Return the subset of ``expected`` not covered by any detected
    feature name. Order preserved from input (which is severity-then-
    alphabetical by ``derive_expected_categories``).
    """
    det_idx = _detected_token_index(detected)
    return [e for e in expected if is_category_covered(e, det_idx) is None]


# ── Prompt ────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """\
You are reviewing a feature map produced by a primary code scan. The
scan analysed source files and produced a list of features. Out-of-band
signal extractors (package dependencies, MVC controllers, ORM models,
route groups) independently identified categories the codebase is
KNOWN to have.

Your job: for each KNOWN category, decide whether the primary scan
captured it under some name, OR missed it entirely. Be conservative —
only confirm a "missed" feature when the evidence files clearly show
code that no detected feature plausibly covers.

OUTPUT: valid JSON only, no prose, no markdown fences. Schema:
{
  "missed": [
    {
      "feature_name": "Title Case Feature Name",
      "matched_category": "<exact category key from input>",
      "files": ["path/to/controller.rb", "path/to/model.rb", ...],
      "rationale": "one short sentence"
    }
  ],
  "covered": [
    {
      "matched_category": "<exact category key from input>",
      "covered_by": "<exact detected feature name>"
    }
  ]
}

Rules:
- Every input category MUST appear in either ``missed`` or ``covered``.
- ``feature_name`` must be Title Case, 2-4 words, product-facing.
- ``files`` MUST list AT LEAST 3 source files that together implement
  the feature — typically the controller + model + view + job for
  Rails, or page route + API route + model for Next.js. Aim for the
  full implementation footprint, not a single representative file.
- File paths must come from the evidence strings provided OR be
  reasonable sibling paths in the same directory tree (e.g. if
  evidence cites ``app/controllers/mfa_controller.rb`` you may also
  list ``app/models/mfa.rb`` and ``app/views/mfa/show.html.erb``).
- NEVER invent paths in completely unrelated directories.
- If a category's evidence cannot support 3 distinct files, put it in
  ``covered`` with ``covered_by`` set to ``"insufficient-evidence"``.
"""


def build_critique_prompt(
    *,
    detected_features: Iterable[str],
    missing: Iterable[ExpectedCategory],
) -> tuple[str, str]:
    """Return ``(system, user)`` prompt strings. Pure; no IO."""
    detected_list = sorted(set(detected_features))
    missing_list = list(missing)

    user_obj = {
        "detected_features": detected_list,
        "candidate_missing_categories": [
            {
                "category": e.category,
                "display": e.display,
                "severity": e.severity,
                "evidence": list(e.evidence),
            }
            for e in missing_list
        ],
    }
    user = (
        "Detected features and candidate missing categories follow as "
        "JSON.\n\n"
        + json.dumps(user_obj, indent=2, sort_keys=True)
    )
    return _SYSTEM_PROMPT, user


# ── LLM response parsing ─────────────────────────────────────────────


def parse_critique_response(
    raw: str,
    *,
    missing_by_key: dict[str, ExpectedCategory],
    repo_root: Path | None = None,
) -> list[CritiqueFinding]:
    """Parse the model's JSON output into ``CritiqueFinding``s.

    ``missing_by_key`` maps category key → the originating
    ``ExpectedCategory`` so we can validate that ``matched_category``
    came from the input set (model hallucinations get dropped).
    Malformed JSON or unknown keys are logged and skipped — the caller
    treats this as "critique returned nothing useful" and proceeds
    with the original result.

    ``repo_root``, when supplied, filters out finding files that don't
    exist on disk under the repo. Keeps the model honest about the
    evidence list. When ``None`` (test path), no filesystem check
    runs — tests stay offline.
    """
    text = raw.strip()
    # Tolerate accidental markdown fences ("```json ... ```").
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("critique: model output was not valid JSON")
        return []

    missed = parsed.get("missed", [])
    if not isinstance(missed, list):
        logger.warning("critique: 'missed' was not a list")
        return []

    findings: list[CritiqueFinding] = []
    for item in missed:
        if not isinstance(item, dict):
            continue
        name = item.get("feature_name")
        cat_key = item.get("matched_category")
        files = item.get("files", [])
        rationale = item.get("rationale", "")
        if not isinstance(name, str) or not name.strip():
            continue
        if cat_key not in missing_by_key:
            logger.info(
                "critique: dropping finding with unknown category %r",
                cat_key,
            )
            continue
        if not isinstance(files, list):
            continue
        candidate_files = [
            f for f in files
            if isinstance(f, str) and f
        ]
        if repo_root is not None:
            candidate_files = [
                f for f in candidate_files
                if (repo_root / f).exists()
            ]
        file_tuple = tuple(candidate_files)
        findings.append(CritiqueFinding(
            feature_name=name.strip(),
            files=file_tuple,
            rationale=rationale if isinstance(rationale, str) else "",
            matched_categories=(cat_key,),
        ))
    return findings


# ── Orchestrator ──────────────────────────────────────────────────────


# Default cap on how many missing categories we send to the LLM in one
# critique pass. The candidate list is sorted severity-desc so the
# ``"must"`` deps land first. Big-route repos (papermark = 355 routes)
# can otherwise overflow the token budget — see
# finding-prompt-injection memory.
DEFAULT_MAX_CANDIDATES = 12
DEFAULT_MAX_TOKENS = 2_048


@dataclass(slots=True)
class CritiqueAggregator:
    """Recall-critique orchestrator.

    Composition:
      1. ``derive_expected_categories(signals)``  → expected list
      2. ``find_missing_categories(expected, detected)`` → diff
      3. Trim to ``max_candidates`` most-severe entries
      4. ``build_critique_prompt(...)`` → (system, user)
      5. ``llm.complete(...)`` → raw response
      6. ``parse_critique_response(...)`` → findings
      7. Return findings (caller decides how to merge into result)

    Steps 1-3 are pure and tested without an LLM. Step 5 is the only
    side-effecting line in the module.
    """

    max_candidates: int = DEFAULT_MAX_CANDIDATES
    max_tokens: int = DEFAULT_MAX_TOKENS

    def run(
        self,
        *,
        detected_features: Iterable[str],
        signals: Iterable[Signal],
        llm,
        repo_root: Path | None = None,
    ) -> list[CritiqueFinding]:
        """Run the critique pass. ``llm`` is an ``LlmClient`` (duck-typed
        — anything with a ``complete(system=, user=, max_tokens=)``
        method works, which keeps tests trivial).

        Returns an empty list when there's nothing to ask the LLM about
        (no missing categories) — the LLM is NOT called in that case,
        saving the API spend.
        """
        detected_list = list(detected_features)
        expected = derive_expected_categories(signals)
        missing = find_missing_categories(expected, detected_list)
        if not missing:
            logger.info("critique: no missing categories; skipping LLM call")
            return []

        candidates = missing[: self.max_candidates]
        system, user = build_critique_prompt(
            detected_features=detected_list,
            missing=candidates,
        )
        missing_by_key = {e.category: e for e in candidates}

        try:
            response = llm.complete(
                system=system,
                user=user,
                max_tokens=self.max_tokens,
            )
        except Exception:  # pragma: no cover - transport failure path
            logger.exception("critique: LLM call failed; skipping")
            return []

        return parse_critique_response(
            response.text,
            missing_by_key=missing_by_key,
            repo_root=repo_root,
        )


def apply_findings_to_deepscan(
    result,
    findings: Iterable[CritiqueFinding],
) -> None:
    """Merge critique findings into a ``DeepScanResult`` in place.

    LEGACY entry — used by the original Stage 1.96 pipeline wire and
    by older tests. New callers should prefer
    ``apply_findings_to_feature_map`` which runs AFTER
    ``build_feature_map`` and consequently bypasses the noise / merge
    safety filters that exist for primary-scan content.

    Adds each finding as a new entry in ``result.features`` (mapped to
    its file tuple) and writes the rationale into ``result.descriptions``
    with a ``[critique]`` provenance prefix.

    Skips findings whose feature_name already exists in the result —
    we don't overwrite primary-scan content. Skips findings with no
    files.
    """
    for f in findings:
        if not f.files:
            continue
        if f.feature_name in result.features:
            logger.info(
                "critique: feature %r already present; skipping merge",
                f.feature_name,
            )
            continue
        result.features[f.feature_name] = list(f.files)
        prefix = "[critique] "
        result.descriptions[f.feature_name] = prefix + (
            f.rationale or "Recovered via recall critique pass."
        )


def apply_findings_to_feature_map(
    feature_map,
    findings: Iterable[CritiqueFinding],
) -> int:
    """Append critique findings to a built ``FeatureMap`` as new
    ``Feature`` objects with ``discovery_method="critique"``.

    Phase 5 Layer A — replaces the pre-Layer-A behaviour of mutating
    DeepScanResult.features inside ``pipeline.run`` (which left the
    findings vulnerable to ``_merge_small_features`` /
    ``_drop_noise_features``). Now critique runs AFTER
    ``build_feature_map`` and the safety filters in cli.py, so the
    findings reach the final JSON intact.

    Path co-ownership is allowed: a critique feature may share file
    paths with one or more primary features. This is correct because
    a single source file (e.g. an MFA controller) can legitimately
    belong to BOTH a broad bucket (Settings) and a specific feature
    (MFA). The papermark-style regression that motivated an earlier
    "anti-cannibalisation" filter was specific to the
    ``file_to_feature`` dict-overwrite inside ``build_feature_map``;
    that path no longer applies since critique runs after the
    FeatureMap is built and primary features already have their
    commits attributed in their Feature objects. Adding new
    Feature instances cannot perturb the primaries.

    Returns the number of new features actually added (skipped only
    when a finding has no files at all OR its name collides with an
    existing feature).
    """
    from datetime import datetime, timezone
    from faultline.models.types import Feature

    existing_names = {f.name for f in feature_map.features}

    added = 0
    now = datetime.now(tz=timezone.utc)

    for finding in findings:
        if not finding.files:
            continue
        if finding.feature_name in existing_names:
            logger.info(
                "critique: feature %r already present; skipping",
                finding.feature_name,
            )
            continue
        prefix = "[critique] "
        description = prefix + (
            finding.rationale or "Recovered via recall critique pass."
        )
        feature_map.features.append(Feature(
            name=finding.feature_name,
            description=description,
            paths=list(finding.files),
            authors=[],
            total_commits=0,
            bug_fixes=0,
            bug_fix_ratio=0.0,
            last_modified=now,
            health_score=100.0,
            discovery_method="critique",
        ))
        existing_names.add(finding.feature_name)
        added += 1

    return added


__all__ = [
    "ExpectedCategory",
    "CritiqueFinding",
    "CritiqueAggregator",
    "derive_expected_categories",
    "find_missing_categories",
    "is_category_covered",
    "build_critique_prompt",
    "parse_critique_response",
    "apply_findings_to_deepscan",
    "apply_findings_to_feature_map",
]
