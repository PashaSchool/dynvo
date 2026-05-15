"""Tests for the Sprint 6.3 zero-flow recovery aggregator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from faultline.aggregators.zero_flow_recovery import (
    ZeroFlowRecovery,
    _extract_callables_from_file,
    _humanize_callable,
)


def _w(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def _feat(name: str, paths: list[str], flows=None):
    from faultline.models.types import Feature
    return Feature(
        name=name, paths=paths, authors=[],
        total_commits=10, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(tz=timezone.utc),
        health_score=99.0, flows=flows or [],
    )


def _fm(features):
    from faultline.models.types import FeatureMap
    return FeatureMap(
        repo_path="/tmp/x",
        analyzed_at=datetime.now(tz=timezone.utc),
        total_commits=0,
        date_range_days=365,
        features=features,
    )


# ── _humanize_callable ────────────────────────────────────────────────


def test_humanize_camelcase_with_verb_prefix():
    assert _humanize_callable("createInvoice") == "create-invoice-flow"


def test_humanize_inserts_use_when_no_verb():
    """Names without verb prefix get a "use-" prefix."""
    assert _humanize_callable("Subscription") == "use-subscription-flow"


def test_humanize_snake_case():
    assert _humanize_callable("send_email_now") == "send-email-now-flow"


def test_humanize_strips_leading_underscores_safely():
    """Underscores treated as separators."""
    assert _humanize_callable("_private_helper") == "use-private-helper-flow"


def test_humanize_already_kebab():
    assert _humanize_callable("get-by-id") == "get-by-id-flow"


# ── _extract_callables_from_file ─────────────────────────────────────


def test_extracts_ts_export_function(tmp_path):
    _w(tmp_path, "x.ts", '''
export async function createInvoice() {}
export function listInvoices() {}
export const cancelInvoice = async () => {};
const internalHelper = () => {};
''')
    out = _extract_callables_from_file(tmp_path / "x.ts")
    assert set(out) == {"createInvoice", "listInvoices", "cancelInvoice"}


def test_extracts_ts_export_class(tmp_path):
    _w(tmp_path, "x.ts", '''
export class InvoiceService {
  charge() {}
  refund() {}
}
''')
    out = _extract_callables_from_file(tmp_path / "x.ts")
    assert "InvoiceService" in out


def test_skips_default_and_handler_names_in_ts(tmp_path):
    _w(tmp_path, "x.ts", '''
export default function Page() {}
export const GET = async () => {};
export const POST = async () => {};
export async function realAction() {}
''')
    out = _extract_callables_from_file(tmp_path / "x.ts")
    assert "Page" not in out  # in skip list
    assert "GET" not in out
    assert "POST" not in out
    assert "realAction" in out


def test_extracts_python_def_and_class(tmp_path):
    _w(tmp_path, "x.py", '''
def create_invoice(amount):
    pass

async def cancel_subscription():
    pass

def _private_helper():
    pass

class InvoiceService:
    pass
''')
    out = _extract_callables_from_file(tmp_path / "x.py")
    assert "create_invoice" in out
    assert "cancel_subscription" in out
    assert "InvoiceService" in out
    assert "_private_helper" not in out  # leading underscore = skip


def test_extracts_ruby_def_class_module(tmp_path):
    _w(tmp_path, "x.rb", '''
class InvoiceController
  def create
  end

  def index
  end

  def self.bulk_charge
  end
end

module Billable
end
''')
    out = _extract_callables_from_file(tmp_path / "x.rb")
    assert "InvoiceController" in out
    assert "create" in out
    assert "index" in out
    assert "bulk_charge" in out
    assert "Billable" in out


def test_unsupported_extension_returns_empty(tmp_path):
    _w(tmp_path, "x.md", "# Title\nexport function nope() {}")
    assert _extract_callables_from_file(tmp_path / "x.md") == []


def test_missing_file_returns_empty(tmp_path):
    assert _extract_callables_from_file(tmp_path / "missing.ts") == []


# ── ZeroFlowRecovery ────────────────────────────────────────────────


def test_recovery_skips_features_that_already_have_flows(tmp_path):
    from faultline.models.types import Flow
    _w(tmp_path, "src/x.ts", "export async function createX() {}")
    fm = _fm([_feat("x", ["src/x.ts"], flows=[
        Flow(
            name="existing-flow", paths=["src/x.ts"], authors=[],
            total_commits=5, bug_fixes=0, bug_fix_ratio=0.0,
            last_modified=datetime.now(tz=timezone.utc),
            health_score=99.0,
        ),
    ])])
    n_feat, n_flow = ZeroFlowRecovery().recover(fm, tmp_path)
    assert n_feat == 0 and n_flow == 0
    assert len(fm.features[0].flows) == 1
    assert fm.features[0].flows[0].name == "existing-flow"


def test_recovery_synthesises_flows_for_zero_flow_feature(tmp_path):
    _w(tmp_path, "src/billing.ts", '''
export async function createInvoice() {}
export async function cancelSubscription() {}
export const renewSubscription = async () => {};
''')
    fm = _fm([_feat("billing", ["src/billing.ts"])])
    n_feat, n_flow = ZeroFlowRecovery().recover(fm, tmp_path)
    assert n_feat == 1
    assert n_flow == 3
    names = {fl.name for fl in fm.features[0].flows}
    assert names == {
        "create-invoice-flow",
        "cancel-subscription-flow",
        "renew-subscription-flow",
    }


def test_recovery_caps_at_max(tmp_path):
    body = "\n".join(
        f"export async function createItem{i:02d}() {{}}"
        for i in range(20)
    )
    _w(tmp_path, "src/many.ts", body)
    fm = _fm([_feat("many", ["src/many.ts"])])
    ZeroFlowRecovery(max_flows_per_feature=4).recover(fm, tmp_path)
    assert len(fm.features[0].flows) == 4


def test_recovery_dedupes_callables_across_paths(tmp_path):
    _w(tmp_path, "a.ts", "export function getX() {}")
    _w(tmp_path, "b.ts", "export function getX() {}")  # same name!
    fm = _fm([_feat("x", ["a.ts", "b.ts"])])
    n_feat, n_flow = ZeroFlowRecovery().recover(fm, tmp_path)
    assert n_feat == 1
    assert n_flow == 1   # dedup'd


def test_recovery_handles_python_library_shape(tmp_path):
    """Library-mode case: a Python sub-package with public functions."""
    _w(tmp_path, "mylib/billing.py", '''
def charge_card(amount: int) -> None:
    pass

def refund(amount: int) -> None:
    pass

class Subscription:
    def renew(self):
        pass
''')
    fm = _fm([_feat("billing", ["mylib/billing.py"])])
    n_feat, n_flow = ZeroFlowRecovery().recover(fm, tmp_path)
    assert n_feat == 1
    assert n_flow >= 2
    names = {fl.name for fl in fm.features[0].flows}
    # charge_card → "use-charge-card-flow" (charge isn't in verb list)
    # OR matches "send/charge"... actually "charge" not in our verb_starts
    # so gets "use-" prefix. Adjust: "use-charge-card-flow", refund → "use-refund-flow"
    assert any("charge" in n for n in names)
    assert any("refund" in n for n in names)


def test_recovery_emits_fallback_when_no_callables_extracted(tmp_path):
    """Critique features sometimes cite paths that don't exist
    OR have no exported callables (config files, docs, SQL).
    The recovery falls back to ONE generic ``use-<name>-flow``
    so the dashboard never shows a feature with literally zero
    flows.
    """
    fm = _fm([_feat("ghost-feature", ["does-not-exist.ts"])])
    n_feat, n_flow = ZeroFlowRecovery().recover(fm, tmp_path)
    assert n_feat == 1
    assert n_flow == 1
    assert fm.features[0].flows[0].name == "use-ghost-feature-flow"


def test_recovery_default_has_no_cap():
    """Per memory/rule-no-magic-tuning, no hardcoded cap. Recovered
    count = whatever the source-file callable extraction yields.
    """
    assert ZeroFlowRecovery().max_flows_per_feature is None


def test_recovery_default_matches_consolidator_default():
    """Both should default to None — neither hardcodes a magic cap."""
    from faultline.aggregators.flow_consolidator import FlowConsolidator
    assert ZeroFlowRecovery().max_flows_per_feature == FlowConsolidator().max_flows_per_feature
