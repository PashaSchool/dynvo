"""Concurrency tests for Stage 6.7c — mega-UF semantic split.

These guard the EXECUTION-ONLY parallelisation of the per-mega-UF LLM
calls (``FAULTLINES_STAGE6_MAX_WORKERS`` thread pool). The contract is:
the parallel run is BYTE-IDENTICAL to the sequential run for the same
per-UF LLM responses. We prove that by running the SAME public function
with ``max_workers=1`` (forced sequential) and ``max_workers=8`` and
deep-comparing the assembled ``user_flows[]`` + telemetry + the in-place
``Flow.user_flow_id`` stamps.

Also asserted:
  * ordering is INPUT order, never thread-completion order — proven with a
    fake whose per-call latency is staggered so completion order is the
    REVERSE of input order;
  * concurrency is bounded by ``max_workers`` (the pool is respected);
  * an item that errors degrades EXACTLY as the sequential path did
    (that mega-UF kept; the rest still split).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from faultline.models.types import Flow, UserFlow
from faultline.pipeline_v2.stage_6_7c_uf_splitter import split_mega_user_flows


# ── Fakes ───────────────────────────────────────────────────────────────────


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeBlock:
    text: str


class _FakeMsg:
    def __init__(self, text: str, in_tok: int = 300, out_tok: int = 120) -> None:
        self.content = [_FakeBlock(text=text)]
        self.usage = _FakeUsage(input_tokens=in_tok, output_tokens=out_tok)


def _flow(name: str, uuid: str) -> Flow:
    return Flow(
        name=name,
        uuid=uuid,
        paths=["backend/routers/x.py"],
        authors=["a"],
        total_commits=1,
        bug_fixes=0,
        bug_fix_ratio=0.0,
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        health_score=100.0,
    )


def _uf(uf_id: str, domain: str, members: list[str]) -> UserFlow:
    return UserFlow(
        id=uf_id, name=domain, domain=domain, product_feature_id=domain,
        intent="other", resource=domain,
        member_flow_ids=members, member_count=len(members),
    )


def _mega_domain(domain: str, n_names: int = 7) -> tuple[UserFlow, list[Flow]]:
    """One mega-UF in ``domain`` with ``n_names`` distinct journey names × 3."""
    flows: list[Flow] = []
    member_ids: list[str] = []
    for c in range(n_names):
        n = f"{domain}-journey-{c}-flow"
        for i in range(3):
            uid = f"{n}-{i}"
            flows.append(_flow(n, uid))
            member_ids.append(uid)
    return _uf(f"UF-{domain}", domain, member_ids), flows


def _multi_mega(n_domains: int) -> tuple[list[UserFlow], list[Flow]]:
    ufs: list[UserFlow] = []
    flows: list[Flow] = []
    for d in range(n_domains):
        uf, fs = _mega_domain(f"dom{d:02d}")
        ufs.append(uf)
        flows.extend(fs)
    return ufs, flows


def _journeys_response_for(text: str) -> str:
    """Deterministic per-UF response: split that UF's distinct names in half.

    The fake reads the prompt (which lists the UF's flow names) so each
    mega-UF gets a DISTINCT, content-derived partition — exactly the
    property that would break if results were mis-indexed across threads.
    """
    names = [
        ln[2:].strip()
        for ln in text.splitlines()
        if ln.startswith("- ")
    ]
    half = max(1, len(names) // 2)
    return json.dumps({"journeys": [
        {"name": "First journey", "members": names[:half]},
        {"name": "Second journey", "members": names[half:]},
    ]})


class _ContentClient:
    """Returns a partition derived from EACH call's own prompt content."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

        class _Messages:
            def create(_self, **kw: Any) -> Any:
                with self._lock:
                    self.calls += 1
                user = kw["messages"][0]["content"]
                return _FakeMsg(_journeys_response_for(user))

        self.messages = _Messages()


def _dump(ufs: list[UserFlow]) -> list[dict]:
    return [u.model_dump() for u in ufs]


def _stamp_map(flows: list[Flow]) -> dict[str, str | None]:
    return {f.uuid: f.user_flow_id for f in flows}


# ── parallel == sequential (byte-identical) ─────────────────────────────────


def _run(ufs: list[UserFlow], flows: list[Flow], workers: int):
    out, tel = split_mega_user_flows(
        ufs, flows, client=_ContentClient(), max_workers=workers,
    )
    return _dump(out), tel, _stamp_map(flows)


def test_parallel_identical_to_sequential_one_item() -> None:
    ufs1, flows1 = _multi_mega(1)
    ufs2, flows2 = _multi_mega(1)
    seq = _run(ufs1, flows1, workers=1)
    par = _run(ufs2, flows2, workers=8)
    assert seq == par


def test_parallel_identical_to_sequential_several_items() -> None:
    ufs1, flows1 = _multi_mega(4)
    ufs2, flows2 = _multi_mega(4)
    seq = _run(ufs1, flows1, workers=1)
    par = _run(ufs2, flows2, workers=8)
    assert seq[0] == par[0]  # user_flows[]
    assert seq[1] == par[1]  # telemetry
    assert seq[2] == par[2]  # Flow.user_flow_id stamps


def test_parallel_identical_to_sequential_many_items() -> None:
    ufs1, flows1 = _multi_mega(17)
    ufs2, flows2 = _multi_mega(17)
    seq = _run(ufs1, flows1, workers=1)
    par = _run(ufs2, flows2, workers=8)
    assert seq == par
    # And the output really did split every mega-UF (sanity: not a no-op).
    assert seq[1]["mega_split"] == 17
    assert len(seq[0]) == 17 * 2  # each mega → 2 sub-UFs


# ── ordering is INPUT order, not completion order ───────────────────────────


class _StaggeredClient:
    """Per-call latency DECREASES with input position, so completion order is
    the REVERSE of submission order. If the stage assembled results in
    completion order the output order would flip — this test fails iff that
    regression is introduced."""

    def __init__(self, n_domains: int) -> None:
        self.n = n_domains
        self._lock = threading.Lock()

        class _Messages:
            def create(_self, **kw: Any) -> Any:
                user = kw["messages"][0]["content"]
                # domain line: "domain: domXX"
                dom = user.splitlines()[0].split("domain:", 1)[1].strip()
                pos = int(dom.replace("dom", ""))
                # earliest input sleeps longest → reversed completion order
                time.sleep(0.02 * (self.n - pos))
                return _FakeMsg(_journeys_response_for(user))

        self.messages = _Messages()


def test_output_order_is_input_order_not_completion_order() -> None:
    n = 5
    ufs, flows = _multi_mega(n)
    out, _tel = split_mega_user_flows(
        ufs, flows, client=_StaggeredClient(n), max_workers=n,
    )
    # Sub-UF ids are derived from the parent mega-UF id (UF-domNN-*). The
    # output must list them in INPUT domain order regardless of which thread
    # finished first.
    parent_order = [u.id.rsplit("-", 1)[0] for u in out]
    expected = []
    for d in range(n):
        expected += [f"UF-dom{d:02d}", f"UF-dom{d:02d}"]
    assert parent_order == expected


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
                    user = kw["messages"][0]["content"]
                    return _FakeMsg(_journeys_response_for(user))
                finally:
                    with self._lock:
                        self._in_flight -= 1

        self.messages = _Messages()


def test_concurrency_bounded_by_pool_size() -> None:
    ufs, flows = _multi_mega(16)
    probe = _ConcurrencyProbeClient()
    split_mega_user_flows(ufs, flows, client=probe, max_workers=4)
    assert probe.max_in_flight <= 4
    assert probe.max_in_flight >= 2  # actually ran in parallel (not serialised)


# ── an erroring item degrades exactly as sequential ─────────────────────────


class _OneRaisesClient:
    """Raises for ONE specific domain's call; returns a valid split for all
    others. The raising mega-UF must be kept intact (recall-safe degrade),
    identical to the sequential path's per-call try/except."""

    def __init__(self, boom_domain: str) -> None:
        self.boom = boom_domain

        class _Messages:
            def create(_self, **kw: Any) -> Any:
                user = kw["messages"][0]["content"]
                dom = user.splitlines()[0].split("domain:", 1)[1].strip()
                if dom == self.boom:
                    raise RuntimeError("boom")
                return _FakeMsg(_journeys_response_for(user))

        self.messages = _Messages()


def test_erroring_item_kept_others_split_parallel_matches_sequential() -> None:
    def run(workers: int):
        ufs, flows = _multi_mega(4)
        out, tel = split_mega_user_flows(
            ufs, flows, client=_OneRaisesClient("dom02"), max_workers=workers,
        )
        return _dump(out), tel, _stamp_map(flows)

    seq = run(1)
    par = run(8)
    assert seq == par
    # dom02 kept as its single mega-UF; the other 3 each split into 2.
    out = seq[0]
    kept = [u for u in out if u["id"] == "UF-dom02"]
    assert len(kept) == 1
    assert seq[1]["mega_split"] == 3


# ── repeatability stress: parallel ALWAYS equals the sequential baseline ─────


def test_parallel_repeatability_stress() -> None:
    """Run the parallel path many times; it must match the sequential baseline
    on EVERY run (no latent race that only shows under thread interleaving)."""
    ufs0, flows0 = _multi_mega(12)
    baseline = _run(ufs0, flows0, workers=1)
    for _ in range(25):
        ufs, flows = _multi_mega(12)
        assert _run(ufs, flows, workers=8) == baseline
