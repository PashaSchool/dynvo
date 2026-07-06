"""Unit tests for the snapshot-gate parallel scheduler.

These tests exercise the scheduling / ordering / lock-write contract of
``faultline.tools.snapshot_gate`` WITHOUT running any real scan: the two
external seams — ``_head_sha`` (git) and ``_run_scan`` (subprocess) — are
mocked, so a "scan" is just writing a canned JSON doc (optionally after a
delay to force out-of-order completion).

The properties under test:

* console output + failure report are emitted in the canonical serial
  order (``sorted`` by slug) irrespective of which worker finishes first;
* ``--workers 1`` and ``--workers N`` produce byte-identical stdout, exit
  code, and (under ``--update``) byte-identical lock files;
* the lock is written exactly once, after every repo has finished;
* worker-count resolution honours flag > env > default with clamping;
* failure semantics (missing clone / HEAD drift / digest drift) are
  collected and reported, exit 1, in canonical order.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from faultline.tools import snapshot_gate as sg
from faultline.tools.normalize_scan import scan_digest


def _canned_doc(profile: str, slug: str) -> dict:
    """A minimal but content-bearing scan doc for one repo."""
    return {
        "schema_version": 1,
        "repo_path": f"/pinned/{slug}",
        "features": [{"name": slug, "paths": [f"{slug}.py"]}],
        "scan_meta": {"framework_profile": profile, "run_id": "run-x"},
    }


def _pinned_sha(slug: str) -> str:
    return (slug + "sha") + "0" * (40 - len(slug) - 3)


def _build_lock(tmp_path: Path, specs: list[tuple[str, str]]) -> tuple[Path, dict]:
    """Create real clone dirs + a lock file for ``specs`` = [(slug, profile)].

    The pinned ``digest`` is the digest the canned doc will produce, so a
    plain ``--check`` is green unless a test deliberately corrupts it.
    Returns ``(lock_path, docs)`` where ``docs[slug]`` is the canned doc.
    """
    repos: dict[str, dict] = {}
    docs: dict[str, dict] = {}
    for slug, profile in specs:
        clone = tmp_path / "clones" / slug
        clone.mkdir(parents=True)
        doc = _canned_doc(profile, slug)
        docs[slug] = doc
        repos[slug] = {
            "path": str(clone),
            "commit_sha": _pinned_sha(slug),
            "profile": profile,
            "digest": scan_digest(doc),
        }
    lock = {"scan_config": {"days": 3650, "llm": "hard-off"}, "repos": repos}
    lock_path = tmp_path / "snapshots.lock.json"
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return lock_path, docs


class _FakeScans:
    """Installable fakes for ``_head_sha`` and ``_run_scan``.

    Records the order scans *start* and *complete* so a test can assert
    that completion order was genuinely shuffled while stdout stayed
    canonical.
    """

    def __init__(
        self,
        docs: dict[str, dict],
        *,
        delays: dict[str, float] | None = None,
        head_override: dict[str, str] | None = None,
        fail_scan: set[str] | None = None,
    ) -> None:
        self.docs = docs
        self.delays = delays or {}
        self.head_override = head_override or {}
        self.fail_scan = fail_scan or set()
        self.started: list[str] = []
        self.completed: list[str] = []
        self._lock = threading.Lock()

    def head_sha(self, repo: Path) -> str:
        slug = Path(repo).name
        return self.head_override.get(slug, _pinned_sha(slug))

    def run_scan(self, repo: Path, days: int, out_json: Path, state_dir: Path) -> None:
        slug = Path(repo).name
        with self._lock:
            self.started.append(slug)
        delay = self.delays.get(slug, 0.0)
        if delay:
            time.sleep(delay)
        if slug in self.fail_scan:
            with self._lock:
                self.completed.append(slug)
            raise RuntimeError(f"deterministic scan failed for {repo} (rc=1)")
        Path(out_json).write_text(json.dumps(self.docs[slug]), encoding="utf-8")
        with self._lock:
            self.completed.append(slug)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "_FakeScans":
        monkeypatch.setattr(sg, "_head_sha", self.head_sha)
        monkeypatch.setattr(sg, "_run_scan", self.run_scan)
        return self


# --------------------------------------------------------------------------- #
# worker-count resolution
# --------------------------------------------------------------------------- #


def test_resolve_workers_flag_beats_env_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sg._WORKERS_ENV, "5")
    assert sg._resolve_workers(2, 11) == 2  # explicit flag wins over env
    assert sg._resolve_workers(None, 11) == 5  # env wins over default


def test_resolve_workers_default_is_min_three(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sg._WORKERS_ENV, raising=False)
    assert sg._resolve_workers(None, 11) == 3
    assert sg._resolve_workers(None, 2) == 2  # never more workers than repos


def test_resolve_workers_clamps_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sg._WORKERS_ENV, raising=False)
    assert sg._resolve_workers(20, 3) == 3  # clamp to #repos
    assert sg._resolve_workers(0, 11) == 3  # non-positive → default
    assert sg._resolve_workers(-4, 11) == 3
    monkeypatch.setenv(sg._WORKERS_ENV, "not-an-int")
    assert sg._resolve_workers(None, 11) == 3  # unparseable env → default


# --------------------------------------------------------------------------- #
# canonical ordering under shuffled completion
# --------------------------------------------------------------------------- #


def test_canonical_output_order_under_shuffled_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    specs = [("alpha", "p1"), ("bravo", "p2"), ("charlie", "p3"), ("delta", "p4")]
    lock_path, docs = _build_lock(tmp_path, specs)
    # Earliest-canonical slug sleeps longest → completes LAST.
    delays = {"alpha": 0.40, "bravo": 0.30, "charlie": 0.20, "delta": 0.10}
    fakes = _FakeScans(docs, delays=delays).install(monkeypatch)

    rc = sg._gate(lock_path, update=False, only=None, profile=None, workers=4)
    out = capsys.readouterr().out

    assert rc == 0
    # Completion order was genuinely reversed vs canonical...
    assert fakes.completed == ["delta", "charlie", "bravo", "alpha"]
    # ...yet the OK lines print in canonical (sorted) order.
    ok_slugs = [ln.split(":")[0] for ln in out.splitlines() if " OK (" in ln]
    assert ok_slugs == ["alpha", "bravo", "charlie", "delta"]


def test_workers_1_and_workers_n_produce_identical_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    specs = [("alpha", "p1"), ("bravo", "p2"), ("charlie", "p3")]
    # SAME lock + SAME clones for both runs (--check never mutates), so any
    # output difference is purely scheduling, not paths.
    lock_path, docs = _build_lock(tmp_path, specs)

    _FakeScans(docs, delays={"alpha": 0.2, "bravo": 0.1}).install(monkeypatch)
    rc_serial = sg._gate(lock_path, update=False, only=None, profile=None, workers=1)
    out_serial = capsys.readouterr().out

    _FakeScans(docs, delays={"alpha": 0.2, "bravo": 0.1}).install(monkeypatch)
    rc_parallel = sg._gate(lock_path, update=False, only=None, profile=None, workers=3)
    out_parallel = capsys.readouterr().out

    assert rc_serial == rc_parallel == 0
    assert out_serial == out_parallel  # byte-identical console stream


# --------------------------------------------------------------------------- #
# lock write: exactly once, after completion, byte-identical serialization
# --------------------------------------------------------------------------- #


def test_update_writes_lock_once_after_all_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = [("alpha", "p1"), ("bravo", "p2"), ("charlie", "p3")]
    lock_path, docs = _build_lock(tmp_path, specs)
    fakes = _FakeScans(docs, delays={"alpha": 0.2, "bravo": 0.1}).install(monkeypatch)

    calls: list[int] = []
    real_write = sg._write_lock

    def spy_write(lock: dict, path: Path) -> None:
        # Snapshot how many repos had finished at the moment of the write.
        calls.append(len(fakes.completed))
        real_write(lock, path)

    monkeypatch.setattr(sg, "_write_lock", spy_write)

    rc = sg._gate(lock_path, update=True, only=None, profile=None, workers=3)

    assert rc == 0
    assert len(calls) == 1  # written exactly once
    assert calls[0] == len(specs)  # ...and only after every repo finished


def test_update_lock_bytes_identical_serial_vs_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = [("alpha", "p1"), ("bravo", "p2"), ("charlie", "p3"), ("delta", "p4")]
    # One set of clones; two lock FILES with byte-identical content (same
    # embedded repo paths). Only the lock file's own path differs, which is
    # never stored inside the lock — so a correct write is byte-identical.
    base_lock, docs = _build_lock(tmp_path, specs)
    _corrupt_all_digests(base_lock)  # force --update to rewrite every digest
    content = base_lock.read_text(encoding="utf-8")
    lock_serial = tmp_path / "serial.lock.json"
    lock_serial.write_text(content, encoding="utf-8")
    lock_parallel = tmp_path / "parallel.lock.json"
    lock_parallel.write_text(content, encoding="utf-8")

    _FakeScans(docs).install(monkeypatch)
    sg._gate(lock_serial, update=True, only=None, profile=None, workers=1)
    bytes_serial = lock_serial.read_bytes()

    _FakeScans(docs, delays={"alpha": 0.2, "bravo": 0.1, "charlie": 0.05}).install(monkeypatch)
    sg._gate(lock_parallel, update=True, only=None, profile=None, workers=4)
    bytes_parallel = lock_parallel.read_bytes()

    assert bytes_serial == bytes_parallel  # byte-identical lock write
    # And it is valid indent=2 JSON with the fresh digests filled in.
    reloaded = json.loads(bytes_parallel)
    for slug, _ in specs:
        assert reloaded["repos"][slug]["digest"] == scan_digest(docs[slug])
    assert bytes_parallel == (json.dumps(reloaded, indent=2) + "\n").encode("utf-8")


def _corrupt_all_digests(lock_path: Path) -> None:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    for pin in lock["repos"].values():
        pin["digest"] = "sha256:stale"
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# failure semantics (collected, canonical order, exit 1)
# --------------------------------------------------------------------------- #


def test_digest_drift_is_collected_in_canonical_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    specs = [("alpha", "p1"), ("bravo", "p2"), ("charlie", "p3")]
    lock_path, docs = _build_lock(tmp_path, specs)
    # Corrupt bravo's pinned digest → drift on bravo only.
    lock = json.loads(lock_path.read_text())
    lock["repos"]["bravo"]["digest"] = "sha256:wrong"
    lock_path.write_text(json.dumps(lock, indent=2) + "\n")

    _FakeScans(docs, delays={"alpha": 0.2}).install(monkeypatch)
    rc = sg._gate(lock_path, update=False, only=None, profile=None, workers=3)
    out = capsys.readouterr().out

    assert rc == 1
    assert "alpha: OK" in out
    assert "charlie: OK" in out
    assert "bravo: digest drift" in out
    assert "snapshot-gate FAILURES:" in out
    assert "  - bravo: digest drift" in out


def test_missing_clone_and_head_drift_are_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    specs = [("alpha", "p1"), ("bravo", "p2"), ("charlie", "p3")]
    lock_path, docs = _build_lock(tmp_path, specs)
    # Remove alpha's clone dir → missing; drift charlie's HEAD.
    (tmp_path / "clones" / "alpha").rmdir()
    fakes = _FakeScans(docs, head_override={"charlie": "deadbeef" + "0" * 32})
    fakes.install(monkeypatch)

    rc = sg._gate(lock_path, update=False, only=None, profile=None, workers=3)
    out = capsys.readouterr().out

    assert rc == 1
    assert "bravo: OK" in out
    assert "alpha: pinned clone missing" in out
    assert "charlie: clone HEAD" in out and "refusing to compare" in out
    # charlie never scanned (HEAD drift short-circuits before the scan).
    assert "charlie" not in fakes.started


def test_worker_exception_surfaces_at_canonical_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    specs = [("alpha", "p1"), ("bravo", "p2"), ("charlie", "p3")]
    lock_path, docs = _build_lock(tmp_path, specs)
    # bravo's scan crashes; alpha (canonical-earlier) succeeds and prints,
    # then the crash surfaces — matching the serial crash-at-bravo behaviour.
    _FakeScans(docs, fail_scan={"bravo"}, delays={"charlie": 0.05}).install(monkeypatch)

    with pytest.raises(RuntimeError, match="deterministic scan failed"):
        sg._gate(lock_path, update=False, only=None, profile=None, workers=3)

    out = capsys.readouterr().out
    assert "alpha: OK" in out  # earlier repo already replayed
    assert "charlie: OK" not in out  # crash surfaces before charlie's block


def test_no_selected_repos_returns_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    lock_path, docs = _build_lock(tmp_path, [("alpha", "p1")])
    _FakeScans(docs).install(monkeypatch)
    rc = sg._gate(lock_path, update=False, only="nonexistent", profile=None, workers=3)
    out = capsys.readouterr().out
    assert rc == 2
    assert "no pinned repos match" in out
