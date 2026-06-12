"""Wiring tests — anti-hallucination validator + name_confidence in the
three LLM naming stages (Stage 8 analyst, Stage 6.7b UF refiner) and the
degraded-scan / rehydration contracts.

The LLM is ALWAYS mocked here. Keyless corpus scans cannot exercise
these paths (no client → deterministic fallbacks), so this file is the
coverage for the validator/bundle wiring — stated explicitly in the
sprint report.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from faultline.analyzer.marketing_fetcher import MarketingTaxonomy
from faultline.models.types import Feature, Flow, UserFlow
from faultline.pipeline_v2.llm_health import LlmHealth
from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows
from faultline.pipeline_v2.stage_0_intake import ScanContext
from faultline.pipeline_v2.stage_8_analyst import run_stage_8_analyst


# ── Fakes ───────────────────────────────────────────────────────────────


class _FakeUsage:
    def __init__(self) -> None:
        self.input_tokens = 500
        self.output_tokens = 200


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeMsg:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage()


class _SeqClient:
    """Scripted responses in order; repeats the last one when exhausted."""

    def __init__(self, *texts: str) -> None:
        outer = self

        class _Messages:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def create(self, **kw: Any) -> _FakeMsg:
                self.calls.append(kw)
                idx = min(len(self.calls) - 1, len(outer._texts) - 1)
                return _FakeMsg(outer._texts[idx])

        self._texts = list(texts)
        self.messages = _Messages()


class _Auth401(Exception):
    status_code = 401


def _feat(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name,
        paths=paths,
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=[],
        layer="developer",
    )


def _ctx(repo_path: Path) -> ScanContext:
    return ScanContext(
        repo_path=repo_path,
        stack="next",
        monorepo=False,
        workspaces=None,
        tracked_files=[],
        commits=[],
    )


def _patch_taxonomy(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tax = MarketingTaxonomy(
        repo_slug="r",
        source_url="https://example.com",
        fetched_at="2026-06-12T00:00:00+00:00",
        product_features=("Documents",),
        confidence=0.9,
        notes="t",
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_analyst.fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: fake_tax,
    )


def _analyst_response(name: str, member: str = "documents") -> str:
    return json.dumps({"product_features": [{
        "name": name,
        "description": "d",
        "member_dev_features": [member],
        "confidence": 0.9,
        "grounded_in": ["x"],
    }]})


# ── Stage 8 analyst: hallucination → retry → rename / fallback ─────────


def test_analyst_hallucinated_name_recovered_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_taxonomy(monkeypatch)
    feats = [_feat("documents", [
        "apps/web/documents/upload.ts", "apps/web/documents/sign.ts",
    ])]
    client = _SeqClient(
        _analyst_response("Document Telemetry"),  # "telemetry" unsupported
        json.dumps({"renames": [
            {"old": "Document Telemetry", "new": "Document Upload"},
        ]}),
    )
    repo = tmp_path / "r"
    repo.mkdir()
    result = run_stage_8_analyst(
        _ctx(repo), feats, [], client=client, cost_tracker=None,
    )
    assert len(client.messages.calls) == 2  # analyst + ONE validator retry
    pf = result.product_features[0]
    assert pf.display_name == "Document Upload"
    assert pf.name == "document-upload"
    assert pf.name_confidence == "high"
    assert result.telemetry["pf_names_invalid"] == 1
    assert result.telemetry["pf_names_renamed"] == 1
    assert result.telemetry["pf_names_fallback"] == 0
    assert result.telemetry["validator_retry_called"] is True
    # slug rename propagated into the dev → product map
    assert result.dev_to_product_map["documents"] == ("document-upload",)


def test_analyst_second_failure_falls_back_to_deterministic_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_taxonomy(monkeypatch)
    feats = [_feat("documents", [
        "apps/web/documents/upload.ts", "apps/web/documents/sign.ts",
    ])]
    client = _SeqClient(
        _analyst_response("Document Telemetry"),
        "not json at all",  # retry fails too
    )
    repo = tmp_path / "r"
    repo.mkdir()
    result = run_stage_8_analyst(
        _ctx(repo), feats, [], client=client, cost_tracker=None,
    )
    pf = result.product_features[0]
    # deterministic slug of the largest member dev feature; never synthesized
    assert pf.name == "documents"
    assert pf.name_confidence == "low"
    assert result.telemetry["pf_names_fallback"] == 1
    assert result.dev_to_product_map["documents"] == ("documents",)


def test_analyst_grounded_name_passes_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_taxonomy(monkeypatch)
    feats = [_feat("documents", [
        "apps/web/documents/upload.ts", "apps/web/documents/sign.ts",
    ])]
    client = _SeqClient(_analyst_response("Document Signing"))
    repo = tmp_path / "r"
    repo.mkdir()
    result = run_stage_8_analyst(
        _ctx(repo), feats, [], client=client, cost_tracker=None,
    )
    assert len(client.messages.calls) == 1
    assert result.product_features[0].name_confidence == "high"
    assert result.telemetry["pf_names_invalid"] == 0


def test_analyst_payload_carries_product_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_taxonomy(monkeypatch)
    nav = tmp_path / "r" / "apps" / "web" / "components"
    nav.mkdir(parents=True)
    (nav / "Sidebar.tsx").write_text(
        "const items = [{ label: 'Data Rooms', href: '/rooms' }]\n<nav/>\n",
        encoding="utf-8",
    )
    rel = "apps/web/components/Sidebar.tsx"
    feats = [_feat("rooms", [rel, "apps/web/rooms/page.tsx"])]
    client = _SeqClient(_analyst_response("Rooms", member="rooms"))
    result = run_stage_8_analyst(
        _ctx(tmp_path / "r"), feats, [], client=client, cost_tracker=None,
    )
    user_prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "Data Rooms (/rooms)" in user_prompt
    assert result.telemetry["product_strings_total"] >= 1


def test_analyst_degraded_scan_stamps_low_confidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_analyst.fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: None,
    )
    monkeypatch.setattr(
        "faultline.pipeline_v2.stage_8_marketing_clusterer."
        "fetch_marketing_taxonomy",
        lambda repo_path, slug, **_: None,
    )
    health = LlmHealth()
    health.record_failure(_Auth401("invalid x-api-key"), stage="stage_3")
    feats = [_feat("documents", ["apps/web/documents/upload.ts"])]
    pre = [Feature(
        name="documents-pf",
        display_name="documents-pf",
        paths=["apps/web/documents/upload.ts"],
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0,
        flows=[],
        layer="product",
    )]
    repo = tmp_path / "r"
    repo.mkdir()
    result = run_stage_8_analyst(
        _ctx(repo), feats, pre,
        client=_SeqClient("{}"), cost_tracker=None, llm_health=health,
    )
    # dead key → deterministic slugs kept, stamped low-confidence
    assert all(pf.name_confidence == "low" for pf in result.product_features)


# ── Stage 6.7b UF refiner: validation + retry + fallback ───────────────


def _member_flow() -> Flow:
    return Flow(
        name="list-detector-flow",
        uuid="f1",
        paths=["backend/routers/detectors.py"],
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=100.0,
    )


def _uf() -> UserFlow:
    return UserFlow(
        id="UF-001",
        name="Browse & filter detectors",
        domain="detector",
        intent="browse",
        resource="detector",
        member_flow_ids=["f1"],
        member_count=1,
    )


def _refine_response(name: str) -> str:
    return json.dumps({"user_flows": [{
        "id": "UF-001",
        "name": name,
        "description": "Browse detectors",
        "intent": "browse",
        "ui_tier": "full-page",
        "acceptance": [],
    }]})


def test_uf_refiner_hallucinated_name_recovered_on_retry() -> None:
    client = _SeqClient(
        _refine_response("Quantum Inspector"),  # unsupported tokens
        _refine_response("Detector List"),      # grounded rename
    )
    ufs = [_uf()]
    _, telemetry = refine_user_flows(ufs, [_member_flow()], client=client)
    assert ufs[0].name == "Detector List"
    assert ufs[0].name_confidence == "high"
    assert telemetry["uf_names_invalid"] == 1
    assert telemetry["uf_names_recovered_on_retry"] == 1
    assert telemetry["uf_names_fallback"] == 0
    assert telemetry["validator_retries"] == 1


def test_uf_refiner_second_failure_keeps_deterministic_name() -> None:
    client = _SeqClient(
        _refine_response("Quantum Inspector"),
        _refine_response("Quantum Inspector"),  # retry still hallucinated
    )
    ufs = [_uf()]
    _, telemetry = refine_user_flows(ufs, [_member_flow()], client=client)
    # deterministic Stage-6.7 name kept; low confidence; other fields apply
    assert ufs[0].name == "Browse & filter detectors"
    assert ufs[0].name_confidence == "low"
    assert ufs[0].description == "Browse detectors"
    assert telemetry["uf_names_fallback"] == 1


def test_uf_refiner_grounded_name_no_retry() -> None:
    client = _SeqClient(_refine_response("Browse detectors"))
    ufs = [_uf()]
    _, telemetry = refine_user_flows(ufs, [_member_flow()], client=client)
    assert ufs[0].name == "Browse detectors"
    assert ufs[0].name_confidence == "high"
    assert telemetry["validator_retries"] == 0
    assert len(client.messages.calls) == 1


def test_uf_refiner_degraded_scan_stamps_low_confidence() -> None:
    health = LlmHealth()
    health.record_failure(_Auth401("invalid x-api-key"), stage="stage_3")
    ufs = [_uf()]
    _, _ = refine_user_flows(
        ufs, [_member_flow()],
        client=_SeqClient("{}"), llm_health=health,
    )
    assert ufs[0].name == "Browse & filter detectors"  # never synthesized
    assert ufs[0].name_confidence == "low"


# ── name_confidence rehydration (additive contract) ─────────────────────


def test_feature_rehydrates_default_high() -> None:
    old = {
        "name": "billing",
        "paths": ["src/billing.ts"],
        "authors": ["a"],
        "total_commits": 1,
        "bug_fixes": 0,
        "bug_fix_ratio": 0.0,
        "last_modified": "2026-01-01T00:00:00+00:00",
        "health_score": 90.0,
    }
    f = Feature.model_validate(old)
    assert f.name_confidence == "high"
    # round-trips explicitly when set
    f2 = Feature.model_validate({**old, "name_confidence": "low"})
    assert f2.name_confidence == "low"


def test_user_flow_rehydrates_default_high() -> None:
    old = {
        "id": "UF-001",
        "name": "Browse & filter detectors",
        "intent": "browse",
        "resource": "detector",
    }
    uf = UserFlow.model_validate(old)
    assert uf.name_confidence == "high"
    uf2 = UserFlow.model_validate({**old, "name_confidence": "low"})
    assert uf2.name_confidence == "low"
