"""Next.js-specific edge augmentations for Stage 3.5.

Next.js introduces a handful of "magic" cross-boundary patterns the
universal T1+T2 layer can miss:

  * **Server Actions** — a client component imports a function from a
    file that opens with ``"use server"``. The call edge is a
    cross-stack RPC even though the import looks intra-repo.
  * **RSC fetches via ``next/headers`` / ``next/cookies``** — these
    are server-only side effects; we tag the call with
    ``confidence: low`` since it doesn't traverse HTTP.
  * **Route handlers exporting GET/POST/...** — already covered by
    :mod:`faultline.pipeline_v2.indexes` (Sprint 1) — listed here for
    completeness so future contributors see the full inventory.

v1 ship: detection helpers below are pure functions; the expander
calls them after T1 has run. Currently surfaces Server Action edges
only — the others are tagged TODO with explicit acceptance criteria.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faultline.analyzer.ast_extractor import FileSignature

_RE_USE_SERVER = re.compile(r'^\s*[\'"]use\s+server[\'"]\s*;?', re.MULTILINE)


def is_server_action_module(sig: "FileSignature | None") -> bool:
    """True when the file's top of source contains ``"use server"``.

    Matches both single-line and multiline forms; the directive must
    appear within the first few non-empty lines to count (Next.js
    enforces this at the bundler level).
    """
    if sig is None or not sig.source:
        return False
    # Inspect first 5 non-empty lines.
    candidate = []
    for line in sig.source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        candidate.append(stripped)
        if len(candidate) >= 5:
            break
    head = "\n".join(candidate)
    return bool(_RE_USE_SERVER.search(head))


__all__ = ["is_server_action_module"]
