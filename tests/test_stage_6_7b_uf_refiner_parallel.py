"""Concurrency tests for Stage 6.7b — User-Flow LLM refiner.

Guards the EXECUTION-ONLY parallelisation of the per-DOMAIN refinement
LLM calls (``FAULTLINES_STAGE6_MAX_WORKERS`` thread pool). Contract: the
parallel run is BYTE-IDENTICAL to the sequential run for the same
per-domain LLM responses. Proven by running the SAME public function with
``max_workers=1`` (forced sequential) and ``max_workers=8`` and
deep-comparing the mutated ``user_flows[]`` + telemetry.

Also asserted:
  * ordering is INPUT (sorted-domain) order, not completion order — proven
    with a fake whose per-call latency is staggered so completion order is
    the REVERSE of the sorted-domain order;
  * concurrency is bounded by ``max_workers``;
  * a domain whose call errors degrades EXACTLY as the sequential path
    (deterministic name/intent kept, ``refined=False``, others refined);
  * the name-validation RETRY path accumulates identical telemetry
    (``validator_retries`` / recovered / fallback) under parallelism.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from faultline.models.types import Flow, UserFlow
from faultline.pipeline_v2.stage_6_7b_uf_refiner import refine_user_flows


# ── Fakes ───────────────────────────────────────────────────────────────────


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeBlock:
    text: str


class _FakeMsg:
    def __init__(self, text: str, in_tok: int = 400, out_tok: int = 150) -> None:
        self.content = [_FakeBlock(text=text)]
        self.usage = _FakeUsage(input_tokens=in_tok, output_tokens=out_tok)


def _flow(name: str, uuid: str, *, domain: str) -> Flow:
    # Path carries the domain so frontend/ui heuristics are stable + distinct.
    return Flow(
        name=name,
        uuid=uuid,
        paths=[f"backend/routers/{domain}.py"],
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        health_score=100.0,
        test_files=[],
    )


def _uf(uf_id: str, domain: str, members: list[str]) -> UserFlow:
    return UserFlow(
        id=uf_id, name=f"{domain}-detname", domain=domain,
        product_feature_id=domain, intent="other", resource=domain,
        member_flow_ids=members, member_count=len(members),
        routes=[f"/{domain}"],
    )


def _domains(n_domains: int) -> tuple[list[UserFlow], list[Flow]]:
    """One UF per domain, each with one member flow whose name is a stable
    in-vocabulary verb-noun so a refined name can pass evidence validation."""
    ufs: list[UserFlow] = []
    flows: list[Flow] = []
    for d in range(n_domains):
        dom = f"detector{d:02d}"
        member = f"create-{dom}-flow"
        flows.append(_flow(member, f"{dom}-f", domain=dom))
        ufs.append(_uf(f"UF-{dom}", dom, [f"{dom}-f"]))
    return ufs, flows


def _refine_response_for(text: str) -> str:
    """Per-domain response derived from the prompt's UF ids.

    For each id ``UF-detectorNN`` we echo a grounded name reusing the
    member-flow vocabulary (``create`` + the resource ``detectorNN``) so it
    survives the anti-hallucination validator. The response is CONTENT-DERIVED
    so a mis-indexed assembly across threads would corrupt it.
    """
    ids = re.findall(r"UF-detector\d{2}", text)
    rows = []
    seen = set()
    for uid in ids:
        if uid in seen:
            continue
        seen.add(uid)
        dom = uid.split("-", 1)[1]  # detectorNN
        rows.append({
            "id": uid,
            "name": f"Create {dom}",
            "description": f"User creates {dom}.",
            "intent": "author",
            "ui_tier": "no-ui",
            "acceptance": [],
        })
    return json.dumps({"user_flows": rows})


class _ContentClient:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

        class _Messages:
            def create(_self, **kw: Any) -> Any:
                with self._lock:
                    self.calls += 1
                user = kw["messages"][0]["content"]
                return _FakeMsg(_refine_response_for(user))

        self.messages = _Messages()


def _dump(ufs: list[UserFlow]) -> list[dict]:
    return [u.model_dump() for u in ufs]


# ── parallel == sequential (byte-identical) ─────────────────────────────────


def _run(workers: int, n_domains: int):
    ufs, flows = _domains(n_domains)
    out, tel = refine_user_flows(
        ufs, flows, client=_ContentClient(), max_workers=workers,
    )
    return _dump(out), tel


def test_parallel_identical_to_sequential_one_domain() -> None:
    assert _run(1, 1) == _run(8, 1)


def test_parallel_identical_to_sequential_several_domains() -> None:
    seq = _run(1, 4)
    par = _run(8, 4)
    assert seq[0] == par[0]  # user_flows[]
    assert seq[1] == par[1]  # telemetry (incl. cost_usd, domain counters)


def test_parallel_identical_to_sequential_many_domains() -> None:
    seq = _run(1, 20)
    par = _run(8, 20)
    assert seq == par
    # Sanity: it actually refined (not a degenerate no-op).
    assert seq[1]["domains_refined"] == 20
    assert seq[1]["uf_refined"] == 20
    assert seq[1]["cost_usd"] > 0.0


# ── ordering is INPUT (sorted-domain) order, not completion order ───────────


class _StaggeredClient:
    """Earliest sorted domain sleeps longest → completion order reversed."""

    def __init__(self, n_domains: int) -> None:
        self.n = n_domains

        class _Messages:
            def create(_self, **kw: Any) -> Any:
                user = kw["messages"][0]["content"]
                m = re.search(r"UF-detector(\d{2})", user)
                pos = int(m.group(1)) if m else 0
                time.sleep(0.02 * (self.n - pos))
                return _FakeMsg(_refine_response_for(user))

        self.messages = _Messages()


def test_output_order_and_telemetry_independent_of_completion_order() -> None:
    n = 5
    ufs, flows = _domains(n)
    out, tel = refine_user_flows(
        ufs, flows, client=_StaggeredClient(n), max_workers=n,
    )
    # user_flows[] is returned in the SAME object order it was passed in
    # (refine mutates in place + returns the same list) — the refined NAMES
    # must line up with each UF's own domain regardless of thread timing.
    for uf in out:
        dom = uf.id.split("-", 1)[1]
        assert uf.name == f"Create {dom}"
        assert uf.refined is True
    # Compare against a forced-sequential run: identical.
    ufs2, flows2 = _domains(n)
    out2, tel2 = refine_user_flows(
        ufs2, flows2, client=_ContentClient(), max_workers=1,
    )
    assert _dump(out) == _dump(out2)
    assert tel == tel2


# ── concurrency is bounded by max_workers ───────────────────────────────────


class _ConcurrencyProbeClient:
    def __init__(self) -> None:
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = threading.Lock()

        class _Messages:
            def create(_self, **kw: Any) -> Any:
                with self._lock:
                    self._in_flight += 1
                    self.max_in_flight = max(self.max_in_flight, self._in_flight)
                try:
                    time.sleep(0.02)
                    return _FakeMsg(_refine_response_for(kw["messages"][0]["content"]))
                finally:
                    with self._lock:
                        self._in_flight -= 1

        self.messages = _Messages()


def test_concurrency_bounded_by_pool_size() -> None:
    ufs, flows = _domains(16)
    probe = _ConcurrencyProbeClient()
    refine_user_flows(ufs, flows, client=probe, max_workers=4)
    assert probe.max_in_flight <= 4
    assert probe.max_in_flight >= 2  # genuinely parallel


# ── a domain whose call errors degrades exactly as sequential ───────────────


class _OneRaisesClient:
    def __init__(self, boom_domain: str) -> None:
        self.boom = boom_domain

        class _Messages:
            def create(_self, **kw: Any) -> Any:
                user = kw["messages"][0]["content"]
                if f"UF-{self.boom}" in user and user.count("UF-detector") == 1:
                    # The boom domain is the only one whose prompt mentions it.
                    raise RuntimeError("boom")
                return _FakeMsg(_refine_response_for(user))

        self.messages = _Messages()


def test_erroring_domain_degrades_parallel_matches_sequential() -> None:
    def run(workers: int):
        ufs, flows = _domains(4)
        out, tel = refine_user_flows(
            ufs, flows, client=_OneRaisesClient("detector02"), max_workers=workers,
        )
        return _dump(out), tel

    seq = run(1)
    par = run(8)
    assert seq == par
    # detector02 kept its deterministic name + not refined; degraded counted.
    out = {u["id"]: u for u in seq[0]}
    assert out["UF-detector02"]["name"] == "detector02-detname"
    assert out["UF-detector02"]["refined"] is False
    assert seq[1]["domains_degraded"] == 1
    assert seq[1]["domains_refined"] == 3


# ── name-validation RETRY path accumulates identical telemetry ──────────────


class _RetryClient:
    """First response per domain proposes a HALLUCINATED name (a token absent
    from the UF's evidence) → fails the validator. The retry proposes a
    grounded name → recovers. Exercises validator_retries / recovered counters
    under parallelism; must match the sequential accumulation exactly."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[str] = set()

        class _Messages:
            def create(_self, **kw: Any) -> Any:
                user = kw["messages"][0]["content"]
                uid = re.search(r"UF-detector\d{2}", user).group(0)
                dom = uid.split("-", 1)[1]
                is_retry = "Do not use" in user or "prohibited" in user.lower()
                with self._lock:
                    first_time = uid not in self._seen
                    self._seen.add(uid)
                if not is_retry and first_time:
                    # Hallucinated token "Zorblax" — not in any evidence.
                    body = {"user_flows": [{
                        "id": uid, "name": f"Zorblax {dom}", "intent": "author",
                        "ui_tier": "no-ui",
                    }]}
                    return _FakeMsg(json.dumps(body))
                # Retry (or any later call): grounded, validator-passing name.
                return _FakeMsg(_refine_response_for(user))

        self.messages = _Messages()


def test_retry_path_telemetry_parallel_matches_sequential() -> None:
    def run(workers: int):
        ufs, flows = _domains(6)
        out, tel = refine_user_flows(
            ufs, flows, client=_RetryClient(), max_workers=workers,
        )
        return _dump(out), tel

    seq = run(1)
    par = run(8)
    assert seq[0] == par[0]
    assert seq[1] == par[1]
    # The retry actually fired + recovered for every domain.
    assert seq[1]["validator_retries"] == 6
    assert seq[1]["uf_names_recovered_on_retry"] == 6
    assert seq[1]["uf_names_fallback"] == 0


# ── repeatability stress: parallel ALWAYS equals the sequential baseline ─────


def test_parallel_repeatability_stress() -> None:
    """Run the parallel path many times; it must match the sequential baseline
    on EVERY run (no latent race only visible under thread interleaving)."""
    baseline = _run(1, 12)
    for _ in range(25):
        assert _run(8, 12) == baseline
