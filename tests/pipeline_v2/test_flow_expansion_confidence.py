"""Sprint 2 — unit tests for confidence labeling (Stage 3.5).

Covers Gate 5 (confidence calibration) at the algorithm level. Reviews
of fixture-driven edges live in test_flow_expansion_call_graph + the
cross-stack tests; this module pins the categorical rules.
"""

from __future__ import annotations

from faultline.pipeline_v2.flow_expansion.confidence import (
    confidence_for_call,
    confidence_for_cross_stack,
    confidence_for_import,
)


class TestImportConfidence:
    def test_static_alias_monorepo_all_high(self):
        assert confidence_for_import("static") == "high"
        assert confidence_for_import("alias") == "high"
        assert confidence_for_import("monorepo") == "high"

    def test_regex_only_resolver_is_medium(self):
        assert confidence_for_import("regex") == "medium"

    def test_unknown_resolver_falls_back_to_low(self):
        assert confidence_for_import("speculative") == "low"
        assert confidence_for_import("") == "low"


class TestCallConfidence:
    def test_resolved_external_symbol_is_high(self):
        assert confidence_for_call(resolved_symbol=True, same_file=False) == "high"

    def test_same_file_resolution_is_medium(self):
        assert confidence_for_call(resolved_symbol=True, same_file=True) == "medium"

    def test_unresolved_is_low(self):
        assert confidence_for_call(resolved_symbol=False, same_file=False) == "low"
        assert confidence_for_call(resolved_symbol=False, same_file=True) == "low"


class TestCrossStackConfidence:
    def test_clean_literal_is_high(self):
        assert confidence_for_cross_stack(
            literal_match=True, template_interpolation=False,
        ) == "high"

    def test_template_interp_demotes_to_medium(self):
        assert confidence_for_cross_stack(
            literal_match=True, template_interpolation=True,
        ) == "medium"

    def test_no_match_is_low(self):
        assert confidence_for_cross_stack(
            literal_match=False, template_interpolation=False,
        ) == "low"
        assert confidence_for_cross_stack(
            literal_match=False, template_interpolation=True,
        ) == "low"
