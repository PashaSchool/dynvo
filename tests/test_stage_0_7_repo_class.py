"""Stage 0.7 — repo-class exit gate (StackProfile spec, Phase C).

Three layers of coverage:

1. Signal-level unit tests — synthetic :class:`RepoClassSignals`
   fingerprints exercising every classifier rule + the fail-open
   ordering (product evidence beats library/binary shapes).
2. Gate-semantics tests — suppression threshold, kill-switch env,
   ``None``-verdict fail-open, scan_meta projection.
3. Corpus classification fixtures — real LOCAL clones (skip when the
   clone is missing, same convention as the G1 selection fixtures).
   Ship gate: ZERO product apps misclassified; the non-product fixture
   set classifies confidently non-product.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from faultline.pipeline_v2.stage_0_6_shape import ShapeSignals
from faultline.pipeline_v2.stage_0_7_repo_class import (
    GATE_ENV,
    NON_PRODUCT_CLASSES,
    REPO_CLASS_CLI_TOOL,
    REPO_CLASS_FRAMEWORK,
    REPO_CLASS_INFRA_DAEMON,
    REPO_CLASS_LIBRARY,
    REPO_CLASS_PRODUCT_APP,
    SUPPRESS_MIN_CONFIDENCE,
    RepoClassSignals,
    RepoClassVerdict,
    classify_repo_class,
    gate_enabled,
    scan_meta_block,
    should_suppress_user_flows,
    suppression_reason,
)


# ── Synthetic signal factories ───────────────────────────────────────────


def _mk_shape(**overrides: object) -> ShapeSignals:
    """A ShapeSignals with every signal ABSENT, then overrides."""
    defaults: dict[str, object] = {}
    for f in dataclasses.fields(ShapeSignals):
        if f.type in ("bool",):
            defaults[f.name] = False
        elif f.type in ("int",):
            defaults[f.name] = 0
        elif f.type.startswith("tuple"):
            defaults[f.name] = ()
        elif f.type in ("str",):
            defaults[f.name] = ""
        else:  # str | None and friends
            defaults[f.name] = None
    defaults.update(overrides)
    return ShapeSignals(**defaults)  # type: ignore[arg-type]


def _mk_signals(shape: ShapeSignals | None = None, **overrides: object) -> RepoClassSignals:
    defaults: dict[str, object] = {
        "shape": shape if shape is not None else _mk_shape(),
        "self_name_js": "",
        "self_name_py": "",
        "self_name_go": "",
        "self_name_rust": "",
        "py_server_framework_deps": (),
        "has_go_binary_entry": False,
        "has_rust_binary_entry": False,
        "rust_server_deps": (),
        "has_daemon_dir": False,
        "daemon_dir": None,
        "has_product_workspace": False,
        "product_workspace_sample": (),
    }
    defaults.update(overrides)
    return RepoClassSignals(**defaults)  # type: ignore[arg-type]


class _FakeCtx:
    """Minimal ctx for the signals=... injection path of classify."""

    repo_path = "/nonexistent"
    workspaces: list = []


def _classify(signals: RepoClassSignals) -> RepoClassVerdict:
    return classify_repo_class(_FakeCtx(), signals=signals)  # type: ignore[arg-type]


# ── 1. Signal-level classifier rules ─────────────────────────────────────


def test_js_library_shape() -> None:
    sig = _mk_signals(
        shape=_mk_shape(
            has_package_json=True,
            package_json_main_or_exports=True,
            package_json_no_app_entry=True,
        ),
    )
    v = _classify(sig)
    assert v.repo_class == REPO_CLASS_LIBRARY
    assert v.confidence >= SUPPRESS_MIN_CONFIDENCE


def test_python_library_without_server_dep() -> None:
    sig = _mk_signals(
        shape=_mk_shape(has_pyproject=True, pyproject_has_project_section=True),
    )
    assert _classify(sig).repo_class == REPO_CLASS_LIBRARY


def test_python_package_with_server_framework_dep_is_product_app() -> None:
    """dispatch/weblate shape: [project] package DEPENDING on a server
    framework is a deployable app, never a library."""
    sig = _mk_signals(
        shape=_mk_shape(has_pyproject=True, pyproject_has_project_section=True),
        py_server_framework_deps=("fastapi",),
    )
    v = _classify(sig)
    assert v.repo_class == REPO_CLASS_PRODUCT_APP
    assert not should_suppress_user_flows(v)


def test_framework_self_name_beats_library_shape() -> None:
    """litestar/fastapi clones: [project] exports AND self-name in the
    framework vocabulary -> framework, not library."""
    sig = _mk_signals(
        shape=_mk_shape(has_pyproject=True, pyproject_has_project_section=True),
        self_name_py="litestar",
    )
    v = _classify(sig)
    assert v.repo_class == REPO_CLASS_FRAMEWORK
    assert should_suppress_user_flows(v)


def test_framework_self_name_beats_own_server_dep() -> None:
    """fastapi depends on starlette — the self-name must win over the
    py-server-dep product rule (ordering: framework < product-app)."""
    sig = _mk_signals(
        shape=_mk_shape(has_pyproject=True, pyproject_has_project_section=True),
        self_name_py="fastapi",
        py_server_framework_deps=("starlette",),
    )
    assert _classify(sig).repo_class == REPO_CLASS_FRAMEWORK


def test_go_framework_self_name() -> None:
    sig = _mk_signals(
        shape=_mk_shape(has_go_mod=True, has_go_top_level_files=True),
        self_name_go="chi",
    )
    assert _classify(sig).repo_class == REPO_CLASS_FRAMEWORK


def test_go_binary_with_daemon_dir_is_infra_daemon() -> None:
    sig = _mk_signals(
        shape=_mk_shape(has_go_mod=True),
        has_go_binary_entry=True,
        has_daemon_dir=True,
        daemon_dir="server",
    )
    v = _classify(sig)
    assert v.repo_class == REPO_CLASS_INFRA_DAEMON
    assert should_suppress_user_flows(v)


def test_go_binary_without_daemon_dir_is_cli_tool() -> None:
    sig = _mk_signals(shape=_mk_shape(has_go_mod=True), has_go_binary_entry=True)
    assert _classify(sig).repo_class == REPO_CLASS_CLI_TOOL


def test_rust_binary_with_server_crate_is_infra_daemon() -> None:
    sig = _mk_signals(
        shape=_mk_shape(has_cargo_toml=True),
        has_rust_binary_entry=True,
        rust_server_deps=("actix-web",),
    )
    assert _classify(sig).repo_class == REPO_CLASS_INFRA_DAEMON


def test_rust_binary_without_server_crate_is_cli_tool() -> None:
    sig = _mk_signals(
        shape=_mk_shape(has_cargo_toml=True),
        has_rust_binary_entry=True,
    )
    assert _classify(sig).repo_class == REPO_CLASS_CLI_TOOL


def test_go_library_shape() -> None:
    sig = _mk_signals(
        shape=_mk_shape(has_go_mod=True, has_go_top_level_files=True),
    )
    assert _classify(sig).repo_class == REPO_CLASS_LIBRARY


def test_routed_app_beats_library_shape() -> None:
    """FAIL-OPEN ordering: a Next app that also publishes exports is a
    product app, never a library."""
    sig = _mk_signals(
        shape=_mk_shape(
            has_package_json=True,
            package_json_main_or_exports=True,
            package_json_no_app_entry=False,
            has_app_router_dir=True,
        ),
    )
    v = _classify(sig)
    assert v.repo_class == REPO_CLASS_PRODUCT_APP


def test_monorepo_product_workspace_beats_binary_shape() -> None:
    sig = _mk_signals(
        shape=_mk_shape(has_go_mod=True),
        has_go_binary_entry=True,
        has_product_workspace=True,
        product_workspace_sample=("apps/web",),
    )
    assert _classify(sig).repo_class == REPO_CLASS_PRODUCT_APP


def test_no_signal_fails_open_to_product_app() -> None:
    v = _classify(_mk_signals())
    assert v.repo_class == REPO_CLASS_PRODUCT_APP
    assert v.confidence < SUPPRESS_MIN_CONFIDENCE
    assert not should_suppress_user_flows(v)
    assert "residual" in v.matched_signals


# ── 2. Gate semantics ────────────────────────────────────────────────────


def _verdict(repo_class: str, confidence: float) -> RepoClassVerdict:
    return RepoClassVerdict(
        repo_class=repo_class, confidence=confidence, rationale="t",
    )


def test_suppression_requires_confident_non_product() -> None:
    assert should_suppress_user_flows(_verdict(REPO_CLASS_LIBRARY, 0.85))
    assert not should_suppress_user_flows(_verdict(REPO_CLASS_LIBRARY, 0.70))
    assert not should_suppress_user_flows(_verdict(REPO_CLASS_PRODUCT_APP, 0.99))
    assert not should_suppress_user_flows(None)


def test_every_non_product_class_can_suppress() -> None:
    for cls in NON_PRODUCT_CLASSES:
        assert should_suppress_user_flows(_verdict(cls, 0.9)), cls


def test_kill_switch_disables_suppression_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v = _verdict(REPO_CLASS_LIBRARY, 0.95)
    monkeypatch.setenv(GATE_ENV, "0")
    assert not gate_enabled()
    assert not should_suppress_user_flows(v)
    # The verdict + scan_meta block are still emitted (observability).
    block = scan_meta_block(v)
    assert block["class"] == REPO_CLASS_LIBRARY
    assert block["gate_enabled"] is False
    assert block["uf_suppression_eligible"] is False


def test_gate_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GATE_ENV, raising=False)
    assert gate_enabled()
    assert should_suppress_user_flows(_verdict(REPO_CLASS_LIBRARY, 0.95))


def test_suppression_reason_marker() -> None:
    assert (
        suppression_reason(_verdict(REPO_CLASS_INFRA_DAEMON, 0.9))
        == "repo_class:infra-daemon"
    )


def test_scan_meta_block_shape() -> None:
    block = scan_meta_block(_verdict(REPO_CLASS_CLI_TOOL, 0.85))
    assert set(block) == {
        "class",
        "confidence",
        "rationale",
        "matched_signals",
        "gate_enabled",
        "uf_suppression_eligible",
    }


def test_broken_classifier_degrades_gracefully() -> None:
    class Boom:
        repo_class = "boom"
        priority = 1

        def classify(self, signals: RepoClassSignals) -> RepoClassVerdict | None:
            raise RuntimeError("boom")

    v = classify_repo_class(
        _FakeCtx(),  # type: ignore[arg-type]
        signals=_mk_signals(),
        classifiers=[Boom()],
    )
    assert v.repo_class == REPO_CLASS_PRODUCT_APP  # residual fail-open


# ── 3. Corpus classification fixtures (real LOCAL clones) ───────────────
#
# Non-product fixtures: the expected class SET tolerates the honest
# cli-vs-daemon ambiguity of Go binaries (caddy, ollama) — either way
# they are confidently non-product, which is the gate's contract.
# Product fixtures: the class must be product-app, ALWAYS (fail-open
# ship gate — zero misclassifications tolerated).

_TESTREPOS = Path("/Users/pkuzina/workspace/_faultlines-testrepos")
_UNSEEN = Path("/Users/pkuzina/workspace/_faultlines-testrepos-unseen")
_FRESHBLOOD = Path("/Users/pkuzina/workspace/_faultlines-freshblood")
_TIER1 = Path("/Users/pkuzina/workspace/_faultlines-tier1")

_NON_PRODUCT_FIXTURES: list = [
    pytest.param(_TESTREPOS / "axios", {REPO_CLASS_LIBRARY}, id="axios"),
    pytest.param(
        _UNSEEN / "caddy",
        {REPO_CLASS_CLI_TOOL, REPO_CLASS_INFRA_DAEMON},
        id="caddy",
    ),
    pytest.param(
        _TESTREPOS / "ollama",
        {REPO_CLASS_CLI_TOOL, REPO_CLASS_INFRA_DAEMON},
        id="ollama",
    ),
    pytest.param(
        _TESTREPOS / "chi",
        {REPO_CLASS_FRAMEWORK, REPO_CLASS_LIBRARY},
        id="chi",
    ),
    pytest.param(_FRESHBLOOD / "traefik", {REPO_CLASS_INFRA_DAEMON}, id="traefik"),
    pytest.param(_FRESHBLOOD / "qdrant", {REPO_CLASS_INFRA_DAEMON}, id="qdrant"),
    pytest.param(_FRESHBLOOD / "litestar", {REPO_CLASS_FRAMEWORK}, id="litestar"),
    pytest.param(_TESTREPOS / "fastapi", {REPO_CLASS_FRAMEWORK}, id="fastapi"),
]

_PRODUCT_FIXTURES: list = [
    pytest.param(_TESTREPOS / "formbricks", id="formbricks"),
    pytest.param(_TESTREPOS / "dub", id="dub"),
    pytest.param(_TIER1 / "saleor", id="saleor"),
    pytest.param(_TIER1 / "weblate", id="weblate"),
    pytest.param(_TIER1 / "dittofeed", id="dittofeed"),
    pytest.param(_FRESHBLOOD / "opensign", id="opensign"),
    pytest.param(_TIER1 / "polar", id="polar"),
    pytest.param(_TIER1 / "dispatch", id="dispatch"),
    pytest.param(_TIER1 / "fastapi-template", id="fastapi-template"),
]


def _classify_clone(repo: Path) -> RepoClassVerdict:
    if not repo.is_dir():
        pytest.skip(f"corpus clone not present: {repo}")
    from faultline.pipeline_v2.stage_0_intake import stage_0_intake

    # days=7 keeps the git pass cheap — classification depends on the
    # file tree + manifests, never on history depth.
    ctx = stage_0_intake(repo, days=7)
    return classify_repo_class(ctx)


@pytest.mark.parametrize(("repo", "expected_classes"), _NON_PRODUCT_FIXTURES)
def test_non_product_fixture_confidently_non_product(
    repo: Path, expected_classes: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(GATE_ENV, raising=False)
    v = _classify_clone(repo)
    assert v.repo_class in expected_classes, v
    assert v.repo_class in NON_PRODUCT_CLASSES
    assert should_suppress_user_flows(v), v


@pytest.mark.parametrize("repo", _PRODUCT_FIXTURES)
def test_product_fixture_never_suppressed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(GATE_ENV, raising=False)
    v = _classify_clone(repo)
    assert v.repo_class == REPO_CLASS_PRODUCT_APP, v
    assert not should_suppress_user_flows(v), v
