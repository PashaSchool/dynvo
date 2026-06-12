"""Tests for the anti-hallucination naming validator (naming-evidence core)."""

from __future__ import annotations

from faultline.pipeline_v2.naming_validator import (
    EvidenceBundle,
    retry_prohibition,
    tokenize_name,
    validate_name,
)


# ── tokenize_name ───────────────────────────────────────────────────────


def test_tokenize_kebab_camel_and_stopwords() -> None:
    assert tokenize_name("OAuth + Email-Auth") == ["oauth", "email", "auth"]
    assert tokenize_name("dataRoomSharing") == ["data", "room", "sharing"]
    # stop-words + plural-s + numbers dropped
    assert tokenize_name("Management of the Webhooks v2") == ["webhook", "v2"]
    assert tokenize_name("Create & edit detectors") == ["detector"]


def test_tokenize_dedupes_preserving_order() -> None:
    assert tokenize_name("auth-auth-tokens auth") == ["auth", "token"]


# ── validate_name basics ────────────────────────────────────────────────


def _bundle(files: dict[str, list[str]], *global_texts: str) -> EvidenceBundle:
    b = EvidenceBundle()
    for path, texts in files.items():
        b.add_file(path, *texts)
    if global_texts:
        b.add_global(*global_texts)
    return b


def test_grounded_name_passes() -> None:
    b = _bundle({"apps/web/documents/upload.ts": []})
    assert validate_name("Document Upload", b).ok


def test_hallucinated_token_fails() -> None:
    b = _bundle({"apps/web/documents/upload.ts": []})
    v = validate_name("Document Telemetry", b)
    assert not v.ok
    assert v.missing_tokens == ["telemetry"]


def test_product_string_evidence_grounds_name() -> None:
    b = _bundle({"app/rooms/page.tsx": ["Data Rooms"]})
    assert validate_name("Data Rooms", b).ok


def test_prefix_stem_matching_but_not_substring() -> None:
    b = _bundle({"src/authentication/session.ts": []})
    # "auth" stem-matches "authentication"
    assert validate_name("Auth Sessions", b).ok
    # "otel" must NOT pass via substring of "hotel"
    b2 = _bundle({"src/hotel/booking.ts": []})
    assert not validate_name("Otel Booking", b2).ok


def test_stopword_only_name_passes() -> None:
    b = _bundle({"src/x.ts": []})
    assert validate_name("Management & Support", b).ok


def test_global_evidence_counts() -> None:
    b = _bundle({"src/a.ts": []}, "fix: tighten webhook retries")
    assert validate_name("Webhook Retries", b).ok


def test_poor_bundle_flag() -> None:
    assert EvidenceBundle().is_poor
    assert not _bundle({"src/a.ts": []}).is_poor


# ── vendor-domination rule ──────────────────────────────────────────────


def test_vendor_single_import_fails() -> None:
    files = {f"src/f{i}.ts": [] for i in range(8)}
    files["src/f0.ts"] = ["import Stripe from 'stripe'"]
    v = validate_name("Stripe Billing", _bundle(files, "billing"))
    assert not v.ok
    assert v.vendor_violations == ["stripe"]


def test_vendor_dominating_share_passes() -> None:
    files: dict[str, list[str]] = {f"src/f{i}.ts": [] for i in range(8)}
    for i in range(3):  # 3 of 8 ≥ ceil(8/4)=2
        files[f"src/f{i}.ts"] = ["stripe checkout session"]
    v = validate_name("Stripe Billing", _bundle(files, "billing"))
    assert v.ok


def test_vendor_floor_of_two_files() -> None:
    # Single-file entity: vendor needs ≥2 files → always fails.
    v = validate_name(
        "Stripe Billing",
        _bundle({"src/stripe.ts": ["stripe"]}, "billing"),
    )
    assert not v.ok
    assert v.vendor_violations == ["stripe"]


def test_vendor_in_path_of_enough_files_passes() -> None:
    files = {
        "apps/billing/stripe/checkout.ts": [],
        "apps/billing/stripe/webhooks.ts": [],
    }
    assert validate_name("Stripe Billing", _bundle(files)).ok


# ── retry prohibition text ──────────────────────────────────────────────


def test_retry_prohibition_lists_names_and_tokens() -> None:
    text = retry_prohibition({
        "Document Telemetry": ["telemetry"],
        "Stripe Billing": ["stripe"],
    })
    assert '"Document Telemetry": prohibited words: telemetry' in text
    assert '"Stripe Billing": prohibited words: stripe' in text
    assert "Do NOT use the prohibited words" in text
