"""G3 — trunk-purity lint (StackProfile spec, Phase A).

Shared pipeline stages (everything in ``faultline/pipeline_v2/`` outside
``profiles/``) must consume the normalized signal contract only. Stack
conditionals — profile-name string comparisons, ``if ... stack == ...``
branching, framework-name literals used for branching — are FORBIDDEN
outside ``profiles/``. This is the ``_STACK_GUIDANCE`` anti-pattern that
was already exorcised once; this lint keeps it dead.

Mechanism: dumb, greppable regex patterns over source lines + a seeded
allowlist of LEGACY occurrences (``tests/data/trunk_purity_allowlist.json``).
The allowlist is keyed by ``(relative file, stripped line content)`` so
line moves don't churn it but any *new* stack conditional — or an edit
to a legacy one — fails the suite and forces a conscious decision.

Legacy-occurrence classes deliberately allowlisted (Phase B folds most
of them into profiles):

* ``extractors/*`` stack-gated activation (``fastapi.py``, ``django.py``,
  ``express.py``, ``rust_*.py``, ``route.py``) — the PRE-profile
  activation mechanism the migration replaces.
* ``stage_0_intake.py`` / ``stack_auditor.py`` — stack *detection* is
  their job; comparisons there are definitional, not trunk branching.
* language-lexer dispatch (``flow_symbols.py``, ``stage_6_6``) — parsing
  by language, matched by the broad literal pattern.

Shrinking the allowlist is always safe; growing it requires editing the
JSON in the same PR (reviewable by construction).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_V2 = REPO_ROOT / "faultline" / "pipeline_v2"
PROFILES_DIR = PIPELINE_V2 / "profiles"
ALLOWLIST_PATH = Path(__file__).parent / "data" / "trunk_purity_allowlist.json"

# Framework / stack tags the engine uses anywhere (Stage 0 detection,
# auditor vocabulary, stack YAMLs). Branching on THESE literals in a
# shared stage is the anti-pattern.
_STACK_LITERALS = (
    "next-app-router|next-pages|nextjs|django|fastapi|litestar|rails|"
    "express|fastify|remix|sveltekit|nuxt|astro|laravel|spring|trpc|"
    "go|rust|react|vue|svelte|python-lib|python-library|js-generic"
)

PATTERNS: dict[str, re.Pattern[str]] = {
    # if/elif branching on a stack variable.
    "stack-branch": re.compile(
        r"(?:if|elif)\s+[^#]*\b(?:stack|audited_stack)\b\s*"
        r"(?:==|!=|\bin\b|\bnot\s+in\b)"
    ),
    # comparison against a framework-name literal (either side).
    "stack-literal-cmp": re.compile(
        r"(?:==|!=)\s*['\"](?:" + _STACK_LITERALS + r")['\"]"
    ),
    "stack-literal-cmp-rev": re.compile(
        r"['\"](?:" + _STACK_LITERALS + r")['\"]\s*(?:==|!=|\bin\s)"
    ),
    # attribute/variable comparison forms: ctx.stack == / ws.stack ==.
    "stack-attr-cmp": re.compile(r"\bstack\b[^#\n]{0,20}?(?:==|!=)\s*['\"]"),
    # branching on the selected profile's name — never allowed in trunk.
    "profile-name-cmp": re.compile(
        r"framework_profile(?:_name)?\s*(?:==|!=|\bin\b)"
    ),
}


def _iter_trunk_files() -> list[Path]:
    return sorted(
        p
        for p in PIPELINE_V2.rglob("*.py")
        if PROFILES_DIR not in p.parents and "__pycache__" not in p.parts
    )


def _scan() -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for path in _iter_trunk_files():
        rel = str(path.relative_to(REPO_ROOT))
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # pure comment lines are not code
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    hits.append(
                        {
                            "file": rel,
                            "pattern": name,
                            "line": stripped,
                            "lineno": str(lineno),
                        }
                    )
                    break  # one report per line is enough
    return hits


def _load_allowlist() -> set[tuple[str, str]]:
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return {(entry["file"], entry["line"]) for entry in data["entries"]}


def test_trunk_has_no_new_stack_conditionals() -> None:
    allow = _load_allowlist()
    new = [h for h in _scan() if (h["file"], h["line"]) not in allow]
    assert not new, (
        "G3 violation — new stack-conditional in a shared pipeline stage. "
        "Move the branch into a profile (faultline/pipeline_v2/profiles/) "
        "or, if it is genuinely detection/lexer code, add it to "
        f"{ALLOWLIST_PATH.name} in this same PR:\n"
        + "\n".join(f"  {h['file']}:{h['lineno']} [{h['pattern']}] {h['line']}" for h in new)
    )


def test_trunk_purity_allowlist_has_no_stale_entries() -> None:
    """Every allowlist entry must still exist — dead entries get pruned.

    This keeps the legacy debt honestly sized: when Phase B folds an
    extractor's stack gate into a profile, its allowlist row must be
    deleted in the same PR.
    """
    current = {(h["file"], h["line"]) for h in _scan()}
    stale = sorted(entry for entry in _load_allowlist() if entry not in current)
    assert not stale, (
        "G3 allowlist has stale entries (fixed or moved code) — prune them:\n"
        + "\n".join(f"  {f}: {line}" for f, line in stale)
    )
