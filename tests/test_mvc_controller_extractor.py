"""Tests for the Rails MVC controller extractor (Phase 3b PoC)."""

from __future__ import annotations

from pathlib import Path

from faultline.extractors.mvc_controller import (
    RailsControllerExtractor,
    collect_rails_controllers,
    is_rails_repo,
)
from faultline.protocols import Extractor


REPO = Path(__file__).parent / "fixtures" / "tiny_rails"


def test_is_rails_repo_detects_fixture():
    assert is_rails_repo(REPO)


def test_is_rails_repo_negative(tmp_path):
    assert not is_rails_repo(tmp_path)


def test_extractor_satisfies_protocol():
    e = RailsControllerExtractor()
    assert isinstance(e, Extractor)


def test_application_controller_skipped():
    actions = collect_rails_controllers(REPO)
    controllers = {a.controller_name for a in actions}
    assert "ApplicationController" not in controllers


def test_billing_controller_actions_extracted():
    actions = collect_rails_controllers(REPO)
    billing = [a for a in actions if a.controller_name == "BillingController"]
    names = {a.action for a in billing}
    assert names == {"index", "show", "create", "update"}


def test_private_methods_excluded():
    actions = collect_rails_controllers(REPO)
    billing = [a for a in actions if a.controller_name == "BillingController"]
    names = {a.action for a in billing}
    assert "billing_params" not in names


def test_mfa_controller_detected():
    """The whole point of this extractor: catch domain controllers
    Sonnet's heuristic absorbs into 'Settings'."""
    actions = collect_rails_controllers(REPO)
    mfa = [a for a in actions if a.controller_name == "MfaController"]
    assert len(mfa) == 3
    assert {a.action for a in mfa} == {"new", "create", "verify"}


def test_namespaced_controller_supported():
    actions = collect_rails_controllers(REPO)
    admin = [a for a in actions if a.controller_name == "Admin::UsersController"]
    assert len(admin) == 2
    assert {a.action for a in admin} == {"index", "destroy"}


def test_extractor_emits_signals_per_action():
    e = RailsControllerExtractor()
    signals = e.extract(REPO, files=[])
    assert len(signals) >= 9    # 4 billing + 3 mfa + 2 admin
    assert all(s.kind == "controller-action" for s in signals)
    assert all(s.payload.get("framework") == "rails" for s in signals)


def test_signal_payload_fields():
    e = RailsControllerExtractor()
    signals = e.extract(REPO, files=[])
    mfa_signal = next(
        s for s in signals if s.payload.get("controller_name") == "MfaController"
    )
    assert mfa_signal.payload["controller_file"].endswith("mfa_controller.rb")
    assert mfa_signal.payload["http_method"] is None   # Rails binds in routes.rb
    assert mfa_signal.payload["parent_class"] == "ApplicationController"
