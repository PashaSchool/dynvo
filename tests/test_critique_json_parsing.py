"""JSON-parsing robustness tests for the recall-critique aggregator.

Yesterday's production incident: Sonnet returned a TRUNCATED JSON
because the response exceeded ``max_tokens``. ``json.loads`` raised
``JSONDecodeError`` and the entire critique pass returned 0 findings.
The mitigation was to bump ``DEFAULT_MAX_TOKENS`` (2K → 8K), but the
parser's GRACEFUL-DEGRADATION contract for malformed/truncated output
deserves its own dedicated test surface so the regression cannot
silently come back if the LLM ever returns a partial response again.

These tests focus on ``parse_critique_response`` only — pure parsing
behaviour, no LLM, no IO.
"""

from __future__ import annotations

import json

import pytest

from faultline.aggregators.critique import (
    ExpectedCategory,
    parse_critique_response,
)


def _missing_idx(*displays: str) -> dict[str, ExpectedCategory]:
    return {
        d.lower(): ExpectedCategory(
            category=d.lower(),
            display=d,
            evidence=(f"dep:{d}",),
            severity="must",
        )
        for d in displays
    }


# ── Happy path ────────────────────────────────────────────────────────


def test_valid_json_parses_to_findings():
    raw = json.dumps({
        "missed": [
            {"feature_name": "Two-Factor Auth",
             "matched_category": "mfa",
             "files": ["a.rb", "b.rb", "c.rb"],
             "rationale": "Dedicated MFA flow."},
        ],
        "covered": [],
    })
    findings = parse_critique_response(raw, missing_by_key=_missing_idx("mfa"))
    assert len(findings) == 1
    assert findings[0].feature_name == "Two-Factor Auth"


# ── Markdown fences (model wrapping) ──────────────────────────────────


def test_json_wrapped_in_markdown_fence_with_language_tag():
    body = json.dumps({"missed": [
        {"feature_name": "Billing", "matched_category": "billing",
         "files": ["x.ts"], "rationale": ""},
    ]})
    raw = f"```json\n{body}\n```"
    findings = parse_critique_response(
        raw, missing_by_key=_missing_idx("billing"),
    )
    assert len(findings) == 1


def test_json_wrapped_in_markdown_fence_without_language_tag():
    body = json.dumps({"missed": []})
    raw = f"```\n{body}\n```"
    assert parse_critique_response(raw, missing_by_key={}) == []


# ── Leading / trailing prose ──────────────────────────────────────────


def test_json_with_leading_and_trailing_prose_currently_fails_gracefully():
    """The current parser does NOT strip arbitrary prose around JSON —
    only ``````` fences. Anything else makes ``json.loads`` raise
    and the parser returns ``[]`` instead of crashing.

    GAP: a prose-tolerant parser (find first ``{`` / last ``}``) would
    be more robust. Documented here so the contract is explicit; not
    fixing in the test, only flagging.
    """
    raw = (
        "Sure, here's my analysis:\n\n"
        + json.dumps({"missed": []})
        + "\n\nLet me know if you need more."
    )
    # Today the parser swallows the error and returns [] — no crash.
    assert parse_critique_response(raw, missing_by_key={}) == []


# ── Truncation: the actual production bug ────────────────────────────


def test_truncated_json_mid_array_recovers_prefix():
    """Sprint 9c P1 fix — when LLM output is truncated mid-array, the
    parser recovers complete leading objects via bracket balancing
    instead of dropping everything. Real production hit on chi.
    """
    raw = (
        '{"missed": ['
        '{"feature_name": "Billing", "matched_category": "billing",'
        ' "files": ["a.ts", "b.ts", "c.ts"], "rationale": "x"},'
        '{"feature_name": "Auth", "matched_category": "auth",'
        ' "files": ["d.ts", "e.ts"'  # ← cut off mid-array, mid-list
    )
    findings = parse_critique_response(
        raw, missing_by_key=_missing_idx("billing", "auth"),
    )
    # Prefix object (Billing) recovered; truncated Auth dropped.
    assert len(findings) == 1
    assert findings[0].feature_name == "Billing"


def test_truncated_json_mid_object_returns_empty_not_crash():
    """Truncation BEFORE first complete object — recovery yields zero
    findings (no complete object to keep), but parser must not crash.
    """
    raw = (
        '{"missed": [{"feature_name": "Billing", "matched_categ'
        # ← cut mid key
    )
    findings = parse_critique_response(
        raw, missing_by_key=_missing_idx("billing"),
    )
    assert findings == []


def test_truncated_json_mid_string_value_returns_empty_not_crash():
    raw = (
        '{"missed": [{"feature_name": "Billing Refund Cycle Edge'
        # ← unterminated string
    )
    findings = parse_critique_response(
        raw, missing_by_key=_missing_idx("billing"),
    )
    assert findings == []


# ── Empty / malformed / garbage ──────────────────────────────────────


def test_empty_string_returns_empty():
    assert parse_critique_response("", missing_by_key={}) == []


def test_only_whitespace_returns_empty():
    assert parse_critique_response("   \n\n\t  ", missing_by_key={}) == []


def test_unclosed_brackets_returns_empty():
    assert parse_critique_response("{[", missing_by_key={}) == []
    assert parse_critique_response('{"missed":', missing_by_key={}) == []


def test_non_json_garbage_english_prose_returns_empty():
    raw = (
        "I reviewed the categories and found that all of them are "
        "already covered. No further action needed."
    )
    assert parse_critique_response(raw, missing_by_key={}) == []


def test_valid_json_but_missed_not_a_list_returns_empty():
    raw = json.dumps({"missed": {"oops": "should be list"}})
    assert parse_critique_response(raw, missing_by_key={}) == []


def test_valid_json_with_no_missed_key_returns_empty():
    """``parsed.get("missed", [])`` defaults to an empty list — fine."""
    raw = json.dumps({"covered": []})
    assert parse_critique_response(raw, missing_by_key={}) == []


def test_array_items_that_are_not_dicts_get_skipped():
    raw = json.dumps({"missed": [
        "a string instead of an object",
        42,
        None,
        {"feature_name": "Real",
         "matched_category": "mfa",
         "files": ["a.rb"],
         "rationale": ""},
    ]})
    findings = parse_critique_response(
        raw, missing_by_key=_missing_idx("mfa"),
    )
    assert len(findings) == 1
    assert findings[0].feature_name == "Real"


def test_files_field_not_a_list_gets_skipped():
    raw = json.dumps({"missed": [
        {"feature_name": "Bad", "matched_category": "mfa",
         "files": "not-a-list", "rationale": ""},
    ]})
    findings = parse_critique_response(
        raw, missing_by_key=_missing_idx("mfa"),
    )
    assert findings == []


def test_files_with_non_string_entries_get_filtered():
    raw = json.dumps({"missed": [
        {"feature_name": "Mfa", "matched_category": "mfa",
         "files": ["good.rb", 42, None, ""], "rationale": ""},
    ]})
    findings = parse_critique_response(
        raw, missing_by_key=_missing_idx("mfa"),
    )
    assert findings[0].files == ("good.rb",)
