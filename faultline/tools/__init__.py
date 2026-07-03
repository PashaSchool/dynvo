"""Developer / CI tooling that sits BESIDE the pipeline, not inside it.

Nothing in this package is imported by scan stages — these are
harness entry points (``python -m faultline.tools.<tool>``) and shared
helpers for tests. Keeping them under ``faultline.tools`` (instead of a
loose ``scripts/`` dir) gives them import access to the engine plus
normal packaging, linting, and typing.
"""
