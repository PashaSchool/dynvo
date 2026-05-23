"""Sprint 2 — adapter tests (Next.js Server Actions detection)."""

from __future__ import annotations

from faultline.analyzer.ast_extractor import FileSignature
from faultline.pipeline_v2.flow_expansion.adapters import (
    express,
    fastapi,
    nextjs,
    react,
)


def _make_sig(source: str) -> FileSignature:
    return FileSignature(path="x.ts", source=source)


class TestNextjsServerActionDetection:
    def test_use_server_at_top_of_file(self):
        sig = _make_sig('"use server";\nexport async function x() {}')
        assert nextjs.is_server_action_module(sig) is True

    def test_single_quotes(self):
        sig = _make_sig("'use server';\nexport async function x() {}")
        assert nextjs.is_server_action_module(sig) is True

    def test_with_leading_comment(self):
        sig = _make_sig(
            '// generated\n"use server";\nexport function y() {}',
        )
        assert nextjs.is_server_action_module(sig) is True

    def test_missing_directive(self):
        sig = _make_sig("export async function x() {}")
        assert nextjs.is_server_action_module(sig) is False

    def test_directive_too_deep_is_ignored(self):
        # 10 lines of irrelevant code then "use server" — bundler
        # wouldn't honour this, so we don't either.
        body = "\n".join(f"const x{i} = {i};" for i in range(10))
        sig = _make_sig(f'{body}\n"use server";')
        assert nextjs.is_server_action_module(sig) is False

    def test_none_input(self):
        assert nextjs.is_server_action_module(None) is False

    def test_empty_source(self):
        assert nextjs.is_server_action_module(_make_sig("")) is False


class TestStubAdapters:
    def test_react_stub_is_inert(self):
        assert react.detect_react_specific_edges() == []

    def test_express_stub_is_inert(self):
        assert express.detect_express_specific_edges() == []

    def test_fastapi_stub_is_inert(self):
        assert fastapi.detect_fastapi_specific_edges() == []
