"""Guard the name-quality scorer (eval/membership/score_names.py).

Synthetic fixtures only — no live repo, no LLM. Verifies the
tokenizer, the token-F1 metric, the REUSE of score_membership's
matching, and that judge mode is hard-gated behind --judge +
ANTHROPIC_API_KEY (no anthropic import on the default path).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

MEMBERSHIP_DIR = Path(__file__).resolve().parents[2] / "eval" / "membership"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"nameeval_{name}", MEMBERSHIP_DIR / f"{name}.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sn = _load("score_names")


# ── tokenizer ───────────────────────────────────────────────────────


def test_tokenize_kebab_camel_and_stopwords() -> None:
    assert sn.tokenize("document-management") == ["document"]  # filler dropped
    assert sn.tokenize("ReplyZero") == ["reply", "zero"]
    assert sn.tokenize("bulk_unsubscribe") == ["bulk", "unsubscribe"]
    assert sn.tokenize("the auth and billing") == ["auth", "billing"]


def test_tokenize_singularizes_and_dedupes() -> None:
    assert sn.tokenize("templates") == ["template"]
    assert sn.tokenize("categories") == ["category"]
    assert sn.tokenize("webhooks webhook") == ["webhook"]
    assert sn.tokenize("2 a") == []  # pure numbers + 1-char tokens dropped


# ── token F1 ────────────────────────────────────────────────────────


def test_token_f1_exact_and_partial() -> None:
    assert sn.token_f1("Reply Zero", "reply-zero") == 1.0
    # {cold, email, blocker} vs {cold, email, blocker} via filler-drop
    assert sn.token_f1("cold-email-blocker", "Cold Email Blocker") == 1.0
    # {document} vs {document, signing}: P=1, R=0.5 → F1 = 2/3
    assert sn.token_f1("documents", "Document Signing") == pytest.approx(2 / 3)
    assert sn.token_f1("i18n-strings", "Billing") == 0.0
    # prefix-stem tolerance (≥4 chars), same rule as the naming validator
    assert sn.token_f1("auth", "Authentication") == 1.0
    assert sn.token_f1("embed", "Embedding") == 1.0
    # short fragments must NOT stem-match ("o" vs "Organisations")
    assert sn.token_f1("ai", "Folders") == 0.0


def test_best_f1_uses_aliases() -> None:
    f1, ref = sn.best_f1("reply-tracker", "Reply Zero", ["Reply Tracker"])
    assert f1 == 1.0
    assert ref == "Reply Tracker"


# ── matching reuse + end-to-end ─────────────────────────────────────

TRUTH_DOC = {
    "features": [
        {
            "name": "cases",
            "display_name": "Case Management",
            "aliases": ["Cases"],
            "files": [
                "backend/routers/cases.py",
                "backend/services/case_autocreator.py",
            ],
        },
        {
            "name": "webhooks",
            "display_name": "Webhooks",
            "files": ["backend/services/webhook_dispatcher.py"],
        },
        {
            "name": "ghost",
            "display_name": "Ghost Feature",
            "files": ["backend/services/nowhere.py"],
        },
    ]
}

SCAN = {
    "developer_features": [
        {
            "name": "case-handling",
            "paths": [
                "backend/routers/cases.py",
                "backend/services/case_autocreator.py",
            ],
        },
        {"name": "outbound-webhooks", "paths": ["backend/services/webhook_dispatcher.py"]},
    ],
    "product_features": [],
}


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    truth_path = tmp_path / "truth.yaml"
    truth_path.write_text(yaml.safe_dump(TRUTH_DOC))
    scan_path = tmp_path / "scan.json"
    scan_path.write_text(json.dumps(SCAN))
    return scan_path, truth_path


def test_end_to_end_scoring(tmp_path: Path) -> None:
    scan_path, truth_path = _write_fixture(tmp_path)
    truth = sn.load_name_truth(truth_path)
    truth_files = {n: e["files"] for n, e in truth.items()}
    matched = sn.match_features(SCAN, truth_files, "dev", None)

    # matching comes from score_membership's greedy Jaccard
    assert matched["cases"] == "case-handling"
    assert matched["webhooks"] == "outbound-webhooks"
    assert matched["ghost"] is None

    result = sn.score_names(truth, matched)
    rows = {r["truth_feature"]: r for r in result["per_feature"]}
    # "case-handling" {case, handling} vs "Case Management" {case}
    # ("management" is a filler stop-word): P=.5 R=1 → 2/3; the alias
    # "Cases" ties, and ties keep the display_name as best_reference.
    assert rows["cases"]["token_f1"] == pytest.approx(2 / 3, abs=1e-4)
    assert rows["cases"]["best_reference"] == "Case Management"
    # "outbound-webhooks" {outbound, webhook} vs {webhook} → 2/3
    assert rows["webhooks"]["token_f1"] == pytest.approx(2 / 3, abs=1e-4)
    # unmatched truth feature scores 0
    assert rows["ghost"]["token_f1"] == 0.0
    assert rows["ghost"]["matched_scan_feature"] is None
    assert result["n_matched"] == 2
    assert result["mean_token_f1"] == pytest.approx((2 / 3 + 2 / 3 + 0) / 3, abs=1e-3)


def test_cli_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scan_path, truth_path = _write_fixture(tmp_path)
    rc = sn.main([str(scan_path), str(truth_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["n_truth_features"] == 3
    assert "judge" not in out  # deterministic by default


# ── judge gating ────────────────────────────────────────────────────


def test_judge_refuses_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    scan_path, truth_path = _write_fixture(tmp_path)
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        sn.main([str(scan_path), str(truth_path), "--judge"])


def test_default_mode_never_imports_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The $0 default path must not even import the SDK."""
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import would fail loudly
    scan_path, truth_path = _write_fixture(tmp_path)
    assert sn.main([str(scan_path), str(truth_path), "--json"]) == 0


def test_evidence_keywords_strip_generic_path_tokens() -> None:
    kws = sn.evidence_keywords(
        ["apps/web/utils/reply-tracker/generate-draft.ts",
         "apps/web/app/api/reply-tracker/route.ts"]
    )
    assert "reply" in kws and "tracker" in kws
    assert "apps" not in kws and "ts" not in kws
