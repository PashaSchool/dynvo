"""LLM provider adapters with a unified ``LlmClient`` contract.

All adapters expose ``complete(*, system, user, max_tokens)`` returning
``faultline.signals.LlmResponse``. Callers should depend on the
``LlmClient`` Protocol from ``faultline.llm.client`` and obtain
instances via ``faultline.llm.factory.make_client(role)``.
"""
